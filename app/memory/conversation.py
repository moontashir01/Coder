import asyncio
import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from sqlalchemy import Column, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from app.database.sqlite_db import Base, AsyncSessionLocal, init_db
from config.settings import settings


class ConversationTurn(Base):
    __tablename__ = "conversation_turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))   # "human" | "ai"
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[str] = mapped_column(String(32))


class ConversationMemory:
    """Sliding-window conversation buffer backed by SQLite."""

    def __init__(self, session_id: str = "default", buffer_size: int | None = None) -> None:
        self.session_id = session_id
        self.buffer_size = buffer_size or settings.conversation_buffer_size
        self._buffer: deque[BaseMessage] = deque(maxlen=self.buffer_size)
        self._initialized = False

    async def _ensure_init(self) -> None:
        if not self._initialized:
            await init_db()
            await self._load_from_db()
            self._initialized = True

    async def _load_from_db(self) -> None:
        """Load the most recent `buffer_size` turns from SQLite into memory."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ConversationTurn)
                .where(ConversationTurn.session_id == self.session_id)
                .order_by(ConversationTurn.id.desc())
                .limit(self.buffer_size)
            )
            rows = list(reversed(result.scalars().all()))

        for row in rows:
            if row.role == "human":
                self._buffer.append(HumanMessage(content=row.content))
            else:
                self._buffer.append(AIMessage(content=row.content))

    async def add_human(self, content: str) -> None:
        await self._ensure_init()
        self._buffer.append(HumanMessage(content=content))
        await self._persist("human", content)

    async def add_ai(self, content: str) -> None:
        await self._ensure_init()
        self._buffer.append(AIMessage(content=content))
        await self._persist("ai", content)

    async def _persist(self, role: str, content: str) -> None:
        async with AsyncSessionLocal() as session:
            turn = ConversationTurn(
                session_id=self.session_id,
                role=role,
                content=content,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            session.add(turn)
            await session.commit()

    async def get_messages(self) -> list[BaseMessage]:
        await self._ensure_init()
        return list(self._buffer)

    async def clear(self) -> None:
        """Clear in-memory buffer (does not delete from DB — history is preserved)."""
        self._buffer.clear()

    async def clear_all(self, delete_db: bool = False) -> None:
        """Clear buffer and optionally wipe DB records for this session."""
        await self._ensure_init()
        self._buffer.clear()
        if delete_db:
            from sqlalchemy import delete as sa_delete
            async with AsyncSessionLocal() as session:
                await session.execute(
                    sa_delete(ConversationTurn).where(
                        ConversationTurn.session_id == self.session_id
                    )
                )
                await session.commit()

    async def recent_turns(self, n: int = 5) -> list[dict[str, str]]:
        """Return last n turns as plain dicts for display."""
        await self._ensure_init()
        msgs = list(self._buffer)[-n:]
        return [
            {"role": "human" if isinstance(m, HumanMessage) else "ai", "content": m.content}
            for m in msgs
        ]

    def format_for_prompt(self) -> str:
        """Format the current buffer as a compact conversation string."""
        lines = []
        for m in self._buffer:
            role = "User" if isinstance(m, HumanMessage) else "Assistant"
            lines.append(f"{role}: {m.content}")
        return "\n".join(lines)
