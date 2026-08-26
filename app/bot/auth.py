"""Who may use the bot, and what they may do (Phase T3).

The rule this file is built around: **a role is a projection onto enforcement
that already exists**, never a new gate of its own. `viewer` is not "the bot
refuses to write" — it is `fs:write`/`fs:delete`/`shell` in the executor's
denied-permission set, so the refusal happens in `Executor.execute` before the
tool is reached, and would still happen if every line of `service.py` were
wrong. `developer` is the approval gate the REPL already installs. What the bot
adds on top is only the *transport* of the question.

Consequences worth stating, because they are what makes the authorization real:

- No role can widen the path jail (`allow_outside_root` is a process flag, not a
  chat command), reach outside `sandbox_root`, or lift the shell denylist.
- The prompt layer is not part of this at all — an instruction file, a skill or
  a retrieved document cannot grant anything, because none of them is consulted
  by the executor.
- A remote caller is strictly LESS trusted than the terminal, never more.

Identity is the numeric Telegram `user_id`. A `@username` is reassignable, so an
allowlist of names is an impersonation surface.

Bootstrap is `settings.telegram_allowed_users` — implicit owners, and **empty
means nobody**: an unconfigured bot refuses everyone, including the person who
started it. Everyone else arrives by pairing: the person at the machine mints a
one-time code, and `/login <code>` binds it to a role. The bot never grants
access to itself.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Integer, String, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from app.database.sqlite_db import AsyncSessionLocal, Base, init_db
from config.settings import settings

logger = logging.getLogger(__name__)

VIEWER = "viewer"
DEVELOPER = "developer"
OWNER = "owner"
ROLES = (VIEWER, DEVELOPER, OWNER)

#: What each role is DENIED at the executor, by permission tag. `developer` and
#: `owner` deny nothing here — writing is gated by approval, not refused — and
#: `viewer` is refused before the tool runs.
_DENIED_BY_ROLE: dict[str, tuple[str, ...]] = {
    VIEWER: ("fs:write", "fs:delete", "shell"),
    DEVELOPER: (),
    OWNER: (),
}

#: Bot commands only an owner may run: they change what the session IS, rather
#: than working inside the project it is already pointed at.
OWNER_ONLY = frozenset({"load", "model", "mcp", "pair", "users", "revoke", "stack"})

#: No I/O/0/1 — a pairing code is read off one screen and typed into another.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8


def denied_permissions_for(role: str) -> list[str]:
    """The executor's deny list for a role. An unknown role gets the strictest."""
    return list(_DENIED_BY_ROLE.get(role, _DENIED_BY_ROLE[VIEWER]))


def may_run_command(role: str, command: str) -> bool:
    return command.lower() not in OWNER_ONLY or role == OWNER


def normalize_role(role: str | None) -> str:
    """Anything unrecognised becomes `viewer` — the least-privileged reading."""
    value = (role or "").strip().lower()
    return value if value in ROLES else VIEWER


# ── storage ─────────────────────────────────────────────────────────────────


class BotUser(Base):
    __tablename__ = "bot_users"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(16), default=VIEWER)
    label: Mapped[str] = mapped_column(String(128), default="")
    added_at: Mapped[str] = mapped_column(String(32), default="")
    added_by: Mapped[str] = mapped_column(String(64), default="")


class PairingCode(Base):
    """A one-time invitation, stored HASHED.

    Hashed for the same reason a password is: `.coder.db` travels with the
    project and is readable by anything that can read the folder, and a code
    sitting in it in plaintext is a live grant to whoever looks.
    """

    __tablename__ = "bot_pairings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code_hash: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16), default=DEVELOPER)
    created_at: Mapped[str] = mapped_column(String(32), default="")
    expires_at: Mapped[float] = mapped_column(default=0.0)
    used_at: Mapped[str] = mapped_column(String(32), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")


def hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()


def new_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


@dataclass(frozen=True)
class Grant:
    """The outcome of a `/login`. `role` is None when nothing was granted."""

    role: str | None
    reason: str


class UserStore:
    """The `bot_users` / `bot_pairings` tables, and the rules over them."""

    def __init__(self, session_factory: Any = None, now: Any = None) -> None:
        self._factory = session_factory
        # Injectable so expiry is tested without sleeping.
        self._now = now or (lambda: datetime.now(timezone.utc).timestamp())

    async def _session(self):
        if self._factory is None:
            await init_db()
            return AsyncSessionLocal()
        return self._factory()

    # ── roles ─────────────────────────────────────────────────────────────

    async def role_for(self, user_id: int) -> str | None:
        """This user's role, or None if they may not use the bot at all.

        `telegram_allowed_users` outranks the table: it is the bootstrap, it is
        edited by the person with the machine, and a paired user must never be
        able to demote the owner by acquiring a row.
        """
        if user_id in set(settings.telegram_allowed_users):
            return OWNER
        try:
            async with await self._session() as session:
                row = await session.get(BotUser, user_id)
        except Exception:
            logger.warning("could not read bot_users", exc_info=True)
            # A database we cannot read is not a reason to let someone in.
            return None
        return normalize_role(row.role) if row is not None else None

    async def grant(
        self, user_id: int, role: str, label: str = "", added_by: str = ""
    ) -> str:
        role = normalize_role(role)
        stamp = datetime.now(timezone.utc).isoformat()
        async with await self._session() as session:
            row = await session.get(BotUser, user_id)
            if row is None:
                session.add(
                    BotUser(
                        user_id=user_id,
                        role=role,
                        label=label[:128],
                        added_at=stamp,
                        added_by=added_by[:64],
                    )
                )
            else:
                row.role = role
                if label:
                    row.label = label[:128]
            await session.commit()
        return role

    async def revoke(self, user_id: int) -> bool:
        """Remove a paired user. Returns False if there was nothing to remove.

        Cannot revoke a bootstrap owner: that id is in `.env`, and pretending to
        remove it would report a change that did not happen.
        """
        if user_id in set(settings.telegram_allowed_users):
            return False
        async with await self._session() as session:
            result = await session.execute(
                delete(BotUser).where(BotUser.user_id == user_id)
            )
            await session.commit()
        return bool(result.rowcount)

    async def list_users(self) -> list[dict]:
        async with await self._session() as session:
            rows = list(
                (await session.execute(select(BotUser).order_by(BotUser.user_id)))
                .scalars()
                .all()
            )
        paired = [
            {
                "user_id": r.user_id,
                "role": normalize_role(r.role),
                "label": r.label,
                "added_at": r.added_at,
                "source": "paired",
            }
            for r in rows
        ]
        bootstrap = [
            {
                "user_id": uid,
                "role": OWNER,
                "label": "",
                "added_at": "",
                "source": "env",
            }
            for uid in settings.telegram_allowed_users
        ]
        return bootstrap + [
            p
            for p in paired
            if p["user_id"] not in set(settings.telegram_allowed_users)
        ]

    # ── pairing ───────────────────────────────────────────────────────────

    async def mint_code(
        self, role: str = DEVELOPER, ttl: float | None = None, created_by: str = "cli"
    ) -> tuple[str, float]:
        """Create a one-time code. Returns (code, seconds until it expires).

        The plaintext is returned ONCE, to be shown on the machine's own screen,
        and never stored.
        """
        role = normalize_role(role)
        ttl = settings.telegram_pairing_ttl if ttl is None else ttl
        code = new_code()
        async with await self._session() as session:
            session.add(
                PairingCode(
                    code_hash=hash_code(code),
                    role=role,
                    created_at=datetime.now(timezone.utc).isoformat(),
                    expires_at=self._now() + ttl,
                    created_by=created_by[:64],
                )
            )
            await session.commit()
        return code, ttl

    async def redeem(self, code: str, user_id: int, label: str = "") -> Grant:
        """Bind a code to a user.

        **Every failure returns the same message.** Distinguishing "expired"
        from "already used" from "never existed" tells someone guessing codes
        which guesses are close, and none of the three is actionable to a
        legitimate user beyond "ask for another one".
        """
        refusal = Grant(None, "That code is not valid. Ask for a new one.")
        if not (code or "").strip():
            return refusal
        digest = hash_code(code)
        try:
            async with await self._session() as session:
                row = (
                    (
                        await session.execute(
                            select(PairingCode).where(PairingCode.code_hash == digest)
                        )
                    )
                    .scalars()
                    .first()
                )
                if row is None or row.used_at or row.expires_at < self._now():
                    return refusal
                # Marked used BEFORE the grant: if the grant fails, a code that
                # was shown to someone must not stay live.
                row.used_at = datetime.now(timezone.utc).isoformat()
                role = normalize_role(row.role)
                await session.commit()
        except Exception:
            logger.warning("pairing failed", exc_info=True)
            return refusal
        await self.grant(user_id, role, label=label, added_by="pairing")
        return Grant(role, f"Paired as {role}.")

    async def purge_expired(self) -> int:
        """Drop spent and expired codes. Housekeeping, never authorization."""
        async with await self._session() as session:
            result = await session.execute(
                delete(PairingCode).where(PairingCode.expires_at < self._now())
            )
            await session.commit()
        return int(result.rowcount or 0)
