"""Wiring only: python-telegram-bot -> `BotService` (Phase T2).

Deliberately thin. Every decision — who may talk to it, what a message means,
what a timeout implies — lives in `service.py`, which has no Telegram import and
is tested with a fake transport. If a rule ends up in this file, it has ended up
in the one place the test suite cannot reach.

Long polling, not a webhook: a webhook needs an inbound public port and a
certificate, which is both a worse security story and undemonstrable on a
laptop.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.agent.sessions import SessionRegistry
from app.bot.service import BotService
from app.bot.transport import TelegramTransport, available, install_hint
from config.settings import settings

logger = logging.getLogger(__name__)


class CoderBot:
    """A running Telegram front-end over one `SessionRegistry`."""

    def __init__(
        self,
        registry: SessionRegistry,
        default_project: Path | str,
        token: str | None = None,
        on_activity=None,
    ) -> None:
        self.registry = registry
        self.default_project = Path(default_project)
        self.token = token or settings.telegram_token
        # The embedded mode prints bot turns into the REPL, so one screen
        # recording shows both front-ends working on the same project.
        self.on_activity = on_activity
        self._app = None
        self._service: BotService | None = None
        self._transport: TelegramTransport | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────

    def preflight(self) -> str:
        """Why the bot cannot start, or "" if it can.

        A reason, never a silent no-op: a front-end that quietly fails to come
        up is indistinguishable from one nobody is messaging.
        """
        if not settings.telegram_enabled:
            return "Telegram is off. Set TELEGRAM_ENABLED=true in .env."
        if not self.token:
            return "No TELEGRAM_TOKEN in .env — get one from @BotFather."
        if not available():
            return install_hint()
        if not settings.telegram_allowed_users:
            # Deny-by-default is right, but starting silently in that state is
            # not: nobody could pair, because minting a code is owner-only.
            return (
                "TELEGRAM_ALLOWED_USERS is empty, so the bot would refuse "
                "everyone — including you, and nobody could mint a pairing "
                "code. Put your numeric Telegram id in it."
            )
        return ""

    async def start(self) -> str:
        """Start polling. Returns "" on success, or the reason it did not."""
        reason = self.preflight()
        if reason:
            return reason

        from telegram.ext import (
            ApplicationBuilder,
            CallbackQueryHandler,
            MessageHandler,
            filters,
        )

        self._app = ApplicationBuilder().token(self.token).build()
        self._transport = TelegramTransport(self._app.bot)
        self._service = BotService(
            registry=self.registry,
            transport=self._transport,
            default_project=self.default_project,
        )
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message)
        )
        self._app.add_handler(MessageHandler(filters.COMMAND, self._on_message))
        self._app.add_handler(CallbackQueryHandler(self._on_button))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(
            timeout=int(settings.telegram_poll_timeout),
            drop_pending_updates=True,
        )
        return ""

    async def stop(self) -> None:
        if self._app is None:
            return
        try:
            if self._app.updater is not None:
                await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        except Exception:
            logger.warning("stopping the Telegram bot failed", exc_info=True)
        finally:
            self._app = None

    @property
    def running(self) -> bool:
        return self._app is not None

    # ── handlers ──────────────────────────────────────────────────────────

    async def _on_message(self, update, context) -> None:
        message = getattr(update, "effective_message", None)
        user = getattr(update, "effective_user", None)
        chat = getattr(update, "effective_chat", None)
        if message is None or user is None or chat is None:
            return
        text = message.text or ""
        if self.on_activity is not None:
            try:
                self.on_activity(user.id, user.username or "", text)
            except Exception:
                logger.debug("activity callback failed", exc_info=True)
        assert self._service is not None
        # Each message is its own task: a build turn runs for minutes, and the
        # poller must keep accepting messages (including the "who is holding
        # this project" answer) while it does.
        asyncio.create_task(self._service.handle(chat.id, user.id, text))

    async def _on_button(self, update, context) -> None:
        query = getattr(update, "callback_query", None)
        if query is None or self._transport is None or self._service is None:
            return
        user = getattr(update, "effective_user", None)
        # An approval is an authorization decision: a button pressed by someone
        # who may not use the bot is not an approval.
        if user is None or not await self._service.is_authorized(user.id):
            await query.answer("Not authorized.")
            return
        delivered = self._transport.resolve(query.data or "")
        await query.answer("Noted." if delivered else "That question expired.")
