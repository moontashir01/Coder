"""What the CURRENT turn is allowed to touch (Phases T1, T3).

Two process-global settings decide a turn's blast radius: `sandbox_root` (the
path jail and the per-project backup directory) and `denied_permissions` (what
the executor refuses outright). Both were exactly right while there was one
front-end, one project and one caller.

With two front-ends there can be two turns in flight at once — that is the whole
point of `--bot-only` — and a single global cannot answer for both:

- **T1, measured.** With the registry's per-project lock already in place and a
  plain save/restore of `settings.sandbox_root`, two concurrent turns on two
  projects had the second turn's pin move the FIRST turn's jail while it was
  still writing. The lock cannot prevent it; different projects are supposed to
  overlap.
- **T3, the same shape with worse consequences.** A `viewer`'s turn and an
  `owner`'s turn can overlap, and a global deny list would mean whichever
  started last decides what BOTH may do — a privilege escalation produced by
  timing.

So both live in a `ContextVar`. Each asyncio Task copies the context it was
created in and mutates its own copy, which is the property a global cannot have.
The settings remain the fallback, so a single-front-end session, a library
import and every existing test behave exactly as they did.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from config.settings import settings


@dataclass(frozen=True)
class TurnScope:
    """The current turn's limits. `None` on a field means "use the setting"."""

    sandbox_root: Path | None = None
    denied_permissions: tuple[str, ...] | None = None


_scope: ContextVar[TurnScope] = ContextVar("coder_turn_scope", default=TurnScope())


def effective_sandbox_root() -> Path | None:
    """The jail root for the current turn, or the process-wide one.

    Every reader of `settings.sandbox_root` that runs *inside a turn* must go
    through this. Reading the setting directly is not a style preference: it is
    the difference between two concurrent turns having one jail and two.
    """
    scoped = _scope.get().sandbox_root
    return scoped if scoped is not None else settings.sandbox_root


def effective_denied_permissions() -> list[str]:
    """Permission tags the current turn's caller may not use at all.

    The union of the process setting and this turn's own list: a scope may only
    ever ADD refusals. A turn that could subtract them would be a way to ask for
    more privilege than the process was started with, which is the opposite of
    what a per-caller scope is for.
    """
    scoped = _scope.get().denied_permissions
    base = list(settings.denied_permissions)
    if not scoped:
        return base
    return sorted(set(base) | set(scoped))


def set_scope(
    root: Path | str | None = None,
    denied_permissions: list[str] | tuple[str, ...] | None = None,
):
    """Scope the current context. Returns a token for `reset_scope`."""
    return _scope.set(
        TurnScope(
            sandbox_root=None if root is None else Path(root).resolve(),
            denied_permissions=(
                None if denied_permissions is None else tuple(denied_permissions)
            ),
        )
    )


def reset_scope(token) -> None:
    _scope.reset(token)


def current_scope() -> TurnScope:
    return _scope.get()
