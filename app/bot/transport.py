"""Everything that touches Telegram, and nothing that decides anything (T2).

`BotService` holds the logic — routing, streaming, approvals, rate limits — and
talks to this. Two implementations: the real one over `python-telegram-bot`, and
a fake one in `tests/test_bot.py`. That split is why the service is testable
with no network, no token and no library installed, the same shape
`app/agent/browser.py` uses for a missing Playwright.

The library is a genuine exception to the offline rule and is treated like one:
`telegram_enabled` ships **off**, the import is lazy, and its absence produces a
loud `install_hint()` rather than a silent no-op. Nothing about generation
reaches the network either way — the model stays local. What does leave the
machine is the conversation itself, which is what a chat front-end IS, and the
README says so.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: Returned by `ask` when nobody answered in time.
TIMED_OUT = "__timeout__"


def available() -> bool:
    try:
        import telegram  # noqa: F401
    except Exception:
        return False
    return True


def install_hint() -> str:
    return (
        "The Telegram bot needs python-telegram-bot, which is not installed:\n"
        "    pip install 'python-telegram-bot>=21'\n"
        "This is the one dependency that talks to the network. The model, the "
        "index and every file operation stay local."
    )


@runtime_checkable
class Transport(Protocol):
    """The four things the service needs a chat platform to do."""

    async def send(self, chat_id: int, html: str) -> int: ...

    async def edit(self, chat_id: int, message_id: int, html: str) -> None: ...

    async def typing(self, chat_id: int) -> None: ...

    async def ask(
        self,
        chat_id: int,
        html: str,
        options: list[tuple[str, str]],
        timeout: float,
    ) -> str: ...


class TelegramTransport:
    """`Transport` over python-telegram-bot's `Bot`."""

    def __init__(self, bot) -> None:
        self._bot = bot
        # Pending inline-keyboard questions: callback token -> Future.
        self._pending: dict[str, asyncio.Future] = {}
        self._seq = 0

    # ── sending ───────────────────────────────────────────────────────────

    async def send(self, chat_id: int, html: str) -> int:
        message = await self._bot.send_message(
            chat_id=chat_id,
            text=html,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return message.message_id

    async def edit(self, chat_id: int, message_id: int, html: str) -> None:
        try:
            await self._bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=html,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            # "message is not modified" is the common one and is not a failure:
            # the stream ticked with no new tokens. Never let a cosmetic edit
            # failure end a turn that is doing real work.
            logger.debug("edit failed: %s", exc)

    async def typing(self, chat_id: int) -> None:
        try:
            await self._bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception as exc:
            logger.debug("typing action failed: %s", exc)

    # ── asking ────────────────────────────────────────────────────────────

    async def ask(
        self,
        chat_id: int,
        html: str,
        options: list[tuple[str, str]],
        timeout: float,
    ) -> str:
        """Post a question with buttons and wait for one to be pressed.

        Returns `TIMED_OUT` if nobody answers. The caller decides what that
        means — for an approval it means DENY, which is the only safe reading:
        an unanswered write must never proceed on the theory that nobody
        objected.
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        self._seq += 1
        token = f"q{self._seq}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[token] = future

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(label, callback_data=f"{token}:{value}")
                    for label, value in options
                ]
            ]
        )
        message = await self._bot.send_message(
            chat_id=chat_id,
            text=html,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return TIMED_OUT
        finally:
            self._pending.pop(token, None)
            try:
                await self._bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=message.message_id, reply_markup=None
                )
            except Exception as exc:
                logger.debug("could not clear keyboard: %s", exc)

    def resolve(self, callback_data: str) -> bool:
        """Deliver a pressed button to whoever is waiting. True if anyone was."""
        token, _, value = (callback_data or "").partition(":")
        future = self._pending.get(token)
        if future is None or future.done():
            return False
        future.set_result(value)
        return True
