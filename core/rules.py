from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import config
from core import duration as dur

log = logging.getLogger("manager.rules")


@dataclass(frozen=True)
class Punishment:
    action: str
    seconds: int | None
    label: str

    @property
    def is_permanent(self) -> bool:
        return self.seconds is None and self.action in {"mute", "ban"}


@dataclass(frozen=True)
class Clause:
    section_id: str
    section_title: str
    section_summary: str
    clause_id: str
    title: str
    note: str | None
    offenses: tuple[dict[str, Any], ...]

    @property
    def key(self) -> str:
        return f"{self.section_id}:{self.clause_id}"

    @property
    def reference(self) -> str:
        return f"{self.section_id}[{self.clause_id}]"

    @property
    def display(self) -> str:
        return f"{self.reference} {self.section_title} - {self.title}"

    def punishment(self, offense_number: int) -> Punishment:
        if not self.offenses:
            return Punishment("warn", None, "Warning")
        index = max(1, offense_number) - 1
        entry = self.offenses[min(index, len(self.offenses) - 1)]
        action = entry.get("action", "warn")
        raw = entry.get("duration")
        seconds = None if action in {"warn", "kick"} else dur.parse(raw)
        return Punishment(action, seconds, _label(action, seconds))

    @property
    def tier_labels(self) -> list[str]:
        labels = []
        for index, entry in enumerate(self.offenses, start=1):
            action = entry.get("action", "warn")
            seconds = None if action in {"warn", "kick"} else dur.parse(entry.get("duration"))
            labels.append(f"{_ordinal(index)} offense: {_label(action, seconds)}")
        return labels


def _ordinal(number: int) -> str:
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def _label(action: str, seconds: int | None) -> str:
    if action == "warn":
        return "Warning"
    if action == "kick":
        return "Kick"
    noun = "Mute" if action == "mute" else "Ban"
    if seconds is None:
        return f"Permanent {noun}"
    return f"{dur.span_label(seconds)} {noun}"


_PLACEHOLDERS = {
    "{community}": config.COMMUNITY_NAME,
    "{game}": config.GAME_NAME,
}


def _fill_placeholders(value: Any) -> Any:
    if isinstance(value, str):
        for token, replacement in _PLACEHOLDERS.items():
            value = value.replace(token, replacement)
        return value
    if isinstance(value, list):
        return [_fill_placeholders(item) for item in value]
    if isinstance(value, dict):
        return {key: _fill_placeholders(item) for key, item in value.items()}
    return value


class RuleBook:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or config.RULES_PATH)
        self.data: dict[str, Any] = {}
        self.clauses: dict[str, Clause] = {}
        self.load()

    def load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                self.data = _fill_placeholders(json.load(handle))
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Could not load the rulebook at %s: %s", self.path, exc)
            self.data = {"meta": {}, "sections": []}

        clauses: dict[str, Clause] = {}
        for section in self.data.get("sections", []):
            if section.get("informational"):
                continue
            for raw in section.get("clauses", []):
                clause = Clause(
                    section_id=section["id"],
                    section_title=section["title"],
                    section_summary=section.get("summary", ""),
                    clause_id=raw["id"],
                    title=raw["title"],
                    note=raw.get("note"),
                    offenses=tuple(raw.get("offenses", [])),
                )
                clauses[clause.key] = clause
        self.clauses = clauses
        log.info("Rulebook loaded: %d sections, %d clauses.", len(self.data.get("sections", [])), len(clauses))

    @property
    def meta(self) -> dict[str, Any]:
        return self.data.get("meta", {})

    def sections(self) -> list[dict[str, Any]]:
        return self.data.get("sections", [])

    def section(self, section_id: str) -> dict[str, Any] | None:
        for section in self.sections():
            if section["id"].casefold() == section_id.casefold():
                return section
        return None

    def get(self, key: str) -> Clause | None:
        if not key:
            return None
        normalised = key.strip().replace("[", ":").replace("]", "").upper()
        normalised = normalised.replace(" ", "")
        return self.clauses.get(normalised) or self.clauses.get(key.strip())

    def __iter__(self) -> Iterator[Clause]:
        return iter(self.clauses.values())

    def search(self, query: str, limit: int = 25) -> list[Clause]:
        needle = (query or "").strip().casefold()
        ordered = sorted(self.clauses.values(), key=lambda c: (c.section_id, c.clause_id))
        if not needle:
            return ordered[:limit]

        scored: list[tuple[int, Clause]] = []
        for clause in ordered:
            haystacks = (
                clause.reference.casefold(),
                clause.section_id.casefold(),
                clause.section_title.casefold(),
                clause.title.casefold(),
                clause.section_summary.casefold(),
            )
            score = 0
            if haystacks[0].startswith(needle) or haystacks[1].startswith(needle):
                score = 100
            elif haystacks[2].startswith(needle):
                score = 80
            elif needle in haystacks[2]:
                score = 60
            elif needle in haystacks[3]:
                score = 40
            elif needle in haystacks[4]:
                score = 20
            elif all(word in " ".join(haystacks) for word in needle.split()):
                score = 10
            if score:
                scored.append((score, clause))

        scored.sort(key=lambda pair: (-pair[0], pair[1].section_id, pair[1].clause_id))
        return [clause for _, clause in scored[:limit]]


rulebook = RuleBook()
