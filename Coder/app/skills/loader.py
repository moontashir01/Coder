"""Discover and load SKILL.md files from the skills directory."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from config.settings import settings


@dataclass
class SkillDefinition:
    name: str
    description: str
    trigger_keywords: list[str]
    instructions: str
    source_path: Path
    enabled: bool = True


def _parse_skill_md(path: Path) -> SkillDefinition | None:
    """Parse a SKILL.md file into a SkillDefinition."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None

    # Extract name from first heading or "# Skill Name: ..."
    name = path.parent.name   # fallback to directory name
    m = re.search(r"^#\s+Skill Name:\s*(.+)$", text, re.MULTILINE)
    if m:
        name = m.group(1).strip()
    else:
        m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        if m:
            name = m.group(1).strip()

    # Extract description (## Description section)
    description = ""
    m = re.search(r"^##\s+Description\s*\n(.*?)(?=^##|\Z)", text, re.MULTILINE | re.DOTALL)
    if m:
        description = m.group(1).strip()

    # Extract trigger keywords (## Trigger Keywords section)
    trigger_keywords: list[str] = []
    m = re.search(r"^##\s+Trigger Keywords\s*\n(.*?)(?=^##|\Z)", text, re.MULTILINE | re.DOTALL)
    if m:
        raw = m.group(1).strip()
        trigger_keywords = [k.strip() for k in re.split(r"[,\n]+", raw) if k.strip()]

    # Extract instructions (## Instructions section — everything after it)
    instructions = ""
    m = re.search(r"^##\s+Instructions\s*\n(.*?)(?=^##|\Z)", text, re.MULTILINE | re.DOTALL)
    if m:
        instructions = m.group(1).strip()

    if not description and not instructions:
        return None

    return SkillDefinition(
        name=name,
        description=description,
        trigger_keywords=trigger_keywords,
        instructions=instructions,
        source_path=path,
    )


class SkillLoader:
    def __init__(self, skills_dir: Path | None = None) -> None:
        self._dir = Path(skills_dir or settings.skills_dir)
        self._skills: dict[str, SkillDefinition] = {}

    def load_all(self) -> int:
        """Scan skills directory, parse all SKILL.md files. Returns count loaded."""
        self._skills.clear()
        if not self._dir.exists():
            return 0

        for skill_md in self._dir.rglob("SKILL.md"):
            skill = _parse_skill_md(skill_md)
            if skill:
                # Key by directory name for uniqueness; display name is skill.name
                key = skill_md.parent.name
                self._skills[key] = skill

        return len(self._skills)

    def list_skills(self) -> list[SkillDefinition]:
        return list(self._skills.values())

    def get(self, name_or_key: str) -> SkillDefinition | None:
        """Look up by directory key or display name (case-insensitive)."""
        if name_or_key in self._skills:
            return self._skills[name_or_key]
        lower = name_or_key.lower()
        for skill in self._skills.values():
            if skill.name.lower() == lower:
                return skill
        return None

    def enable(self, name_or_key: str) -> bool:
        skill = self.get(name_or_key)
        if skill:
            skill.enabled = True
            return True
        return False

    def disable(self, name_or_key: str) -> bool:
        skill = self.get(name_or_key)
        if skill:
            skill.enabled = False
            return True
        return False

    def enabled_skills(self) -> list[SkillDefinition]:
        return [s for s in self._skills.values() if s.enabled]
