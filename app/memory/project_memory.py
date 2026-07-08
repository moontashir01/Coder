import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import String, Text, select
from sqlalchemy import delete as sa_delete
from sqlalchemy.orm import Mapped, mapped_column

from app.database.sqlite_db import Base, AsyncSessionLocal, init_db
from app.rag.chunker import LANGUAGE_MAP

logger = logging.getLogger(__name__)


class ProjectSummaryRow(Base):
    __tablename__ = "project_summaries"

    project_path: Mapped[str] = mapped_column(String(512), primary_key=True)
    summary_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(String(32))


def _scan_project(project_path: Path) -> dict[str, Any]:
    """Static scan — no LLM, just filesystem analysis."""
    languages: dict[str, int] = {}
    modules: list[str] = []
    dependencies: list[str] = []

    code_suffixes = set(LANGUAGE_MAP.keys())

    for f in project_path.rglob("*"):
        if not f.is_file():
            continue
        if any(part.startswith(".") for part in f.parts):
            continue
        if "__pycache__" in f.parts or "node_modules" in f.parts:
            continue

        suffix = f.suffix.lower()
        if suffix in code_suffixes:
            lang = LANGUAGE_MAP[suffix]
            languages[lang] = languages.get(lang, 0) + 1
            rel = str(f.relative_to(project_path))
            if suffix == ".py" and f.stem != "__init__":
                modules.append(rel)

        # Detect dependencies
        if f.name in ("requirements.txt", "pyproject.toml", "package.json", "Cargo.toml", "go.mod"):
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
                dependencies.append(f"{f.name}:\n{content[:500]}")
            except Exception:
                pass

    return {
        "project_path": str(project_path),
        "languages": languages,
        "modules": modules[:50],       # cap at 50 to stay compact
        "dependencies": dependencies,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }


def _summary_to_prompt(summary: dict[str, Any]) -> str:
    """Convert a summary dict into a concise context block for injection."""
    lines = [f"## Project: {summary['project_path']}"]
    if summary.get("languages"):
        lang_str = ", ".join(f"{l} ({n} files)" for l, n in summary["languages"].items())
        lines.append(f"Languages: {lang_str}")
    if summary.get("modules"):
        lines.append(f"Modules ({len(summary['modules'])}):")
        for m in summary["modules"][:20]:
            lines.append(f"  - {m}")
        if len(summary["modules"]) > 20:
            lines.append(f"  ... and {len(summary['modules']) - 20} more")
    if summary.get("dependencies"):
        lines.append("Dependencies detected:")
        for d in summary["dependencies"]:
            lines.append(f"  {d[:120]}")
    return "\n".join(lines)


class ProjectMemory:
    """Stores and retrieves project summaries from SQLite.
    Optionally watches the project dir for changes and refreshes automatically.
    """

    def __init__(self) -> None:
        self._initialized = False
        self._observer = None      # watchdog Observer
        self._watch_path: str | None = None

    async def _ensure_init(self) -> None:
        if not self._initialized:
            await init_db()
            self._initialized = True

    async def index_project(self, project_path: str | Path) -> dict[str, Any]:
        """Scan project and persist summary."""
        await self._ensure_init()
        path = Path(project_path).resolve()
        summary = _scan_project(path)

        async with AsyncSessionLocal() as session:
            row = ProjectSummaryRow(
                project_path=str(path),
                summary_json=json.dumps(summary),
                updated_at=summary["scanned_at"],
            )
            await session.merge(row)
            await session.commit()

        return summary

    async def get_summary(self, project_path: str | Path) -> dict[str, Any] | None:
        await self._ensure_init()
        path = str(Path(project_path).resolve())
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ProjectSummaryRow).where(ProjectSummaryRow.project_path == path)
            )
            row = result.scalar_one_or_none()
        if row:
            return json.loads(row.summary_json)
        return None

    async def get_prompt_block(self, project_path: str | Path) -> str:
        summary = await self.get_summary(project_path)
        if not summary:
            return ""
        return _summary_to_prompt(summary)

    async def delete(self, project_path: str | Path) -> None:
        await self._ensure_init()
        path = str(Path(project_path).resolve())
        async with AsyncSessionLocal() as session:
            await session.execute(
                sa_delete(ProjectSummaryRow).where(ProjectSummaryRow.project_path == path)
            )
            await session.commit()

    # ------------------------------------------------------------------
    # Watchdog integration — auto-refresh on file changes
    # ------------------------------------------------------------------

    def start_watching(self, project_path: str | Path, loop: asyncio.AbstractEventLoop) -> None:
        """Start a watchdog observer to refresh summary on file changes."""
        self.stop_watching()
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            watch_path = str(Path(project_path).resolve())
            pm = self

            class _Handler(FileSystemEventHandler):
                def on_any_event(self, event):
                    if event.is_directory:
                        return
                    asyncio.run_coroutine_threadsafe(
                        pm.index_project(watch_path), loop
                    )

            observer = Observer()
            observer.schedule(_Handler(), watch_path, recursive=True)
            observer.start()
            self._observer = observer
            self._watch_path = watch_path
        except Exception as e:
            logger.debug("project-summary watcher unavailable: %s", e)

    def stop_watching(self) -> None:
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2)
            except Exception as e:
                logger.debug("stopping project-summary watcher failed: %s", e)
            self._observer = None
            self._watch_path = None


# Module-level singleton
project_memory = ProjectMemory()
