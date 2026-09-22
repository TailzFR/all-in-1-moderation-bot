from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import config

log = logging.getLogger("manager.filters")

SEVERITY_ORDER = {"low": 0, "moderate": 1, "high": 2, "severe": 3}

_HOMOGLYPHS = str.maketrans(
    {
        "а": "a", "е": "e", "о": "o", "р": "p", "с": "c",
        "у": "y", "х": "x", "в": "b", "н": "h", "к": "k",
        "м": "m", "т": "t", "А": "a", "Е": "e", "О": "o",
        "α": "a", "β": "b", "ε": "e", "ι": "i", "ο": "o",
        "ρ": "p", "τ": "t", "υ": "u", "χ": "x",
        "​": "", "‌": "", "‍": "", "﻿": "", "⁠": "",
        "­": "",
    }
)

_LEET = str.maketrans(
    {
        "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "6": "g", "7": "t",
        "8": "b", "9": "g", "@": "a", "$": "s", "!": "i", "|": "l", "+": "t",
        "€": "e", "£": "l",
    }
)

_SUFFIXES = ("ing", "ers", "er", "ed", "es", "s", "y", "z")

_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_REPEAT = re.compile(r"(.)\1{%d,}" % (config.FILTER_MAX_REPEATED_CHARS - 1))


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalise(text: str) -> str:
    return strip_accents(text or "").casefold().translate(_HOMOGLYPHS)


def canon(text: str) -> str:
    letters = re.sub(r"[^a-z0-9]+", "", text or "")
    if not letters:
        return ""
    out = [letters[0]]
    for ch in letters[1:]:
        if ch != out[-1]:
            out.append(ch)
    return "".join(out)


def stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


@dataclass
class Hit:
    category: str
    severity: str
    terms: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class Verdict:
    hits: list[Hit] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        return bool(self.hits)

    @property
    def severity(self) -> str:
        if not self.hits:
            return "none"
        return max((h.severity for h in self.hits), key=lambda s: SEVERITY_ORDER[s])

    @property
    def primary(self) -> Hit | None:
        if not self.hits:
            return None
        return max(self.hits, key=lambda h: SEVERITY_ORDER[h.severity])

    @property
    def categories(self) -> list[str]:
        return [h.category for h in self.hits]

    def terms(self) -> list[str]:
        seen: list[str] = []
        for hit in self.hits:
            for term in hit.terms:
                if term not in seen:
                    seen.append(term)
        return seen


class ChatFilter:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or config.FILTERS_PATH)
        self.data: dict[str, Any] = {}
        self._recent: dict[int, deque] = defaultdict(lambda: deque(maxlen=24))
        self.load()

    def load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                self.data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Could not load filter lists at %s: %s", self.path, exc)
            self.data = {}

        self.slurs: dict[str, list[str]] = self.data.get("slurs", {})
        self.slur_canon: list[tuple[str, str, str]] = []
        for category, terms in self.slurs.items():
            for term in terms:
                cterm = canon(normalise(term))
                if len(cterm) >= 3:
                    self.slur_canon.append((category, term, cterm))

        self.threat_canon = [(t, canon(normalise(t))) for t in self.data.get("threats", [])]
        self.strong = {normalise(w) for w in self.data.get("profanity_strong", [])}
        self.mild = {normalise(w) for w in self.data.get("profanity_mild", [])}
        self.sexual = {normalise(w) for w in self.data.get("sexual", [])}
        self.allow = {normalise(w) for w in self.data.get("allow", [])}
        self.allow_canon = {canon(w) for w in self.allow}
        self.rule_map: dict[str, dict[str, str]] = self.data.get("rule_map", {})
        self.invites = [re.compile(p, re.IGNORECASE) for p in self.data.get("invite_patterns", [])]

        log.info(
            "Chat filter loaded: %d slur terms, %d strong, %d mild, %d allowed words.",
            len(self.slur_canon), len(self.strong), len(self.mild), len(self.allow),
        )

    def rule_for(self, category: str) -> tuple[str, str] | None:
        entry = self.rule_map.get(category)
        if not entry:
            return None
        return entry.get("rule", ""), entry.get("clause", "")

    def scan(
        self,
        content: str,
        *,
        author_id: int | None = None,
        mention_count: int = 0,
        has_user_mention: bool = False,
        check_spam: bool = True,
    ) -> Verdict:
        verdict = Verdict()
        raw = content or ""
        if not raw.strip():
            return verdict

        base = normalise(raw)
        deleeted = base.translate(_LEET)
        spaced = re.sub(r"[^a-z0-9]+", " ", deleeted).strip()
        tokens = [t for t in spaced.split() if t]

        suspect = [t for t in tokens if t not in self.allow and canon(t) not in self.allow_canon]

        self._scan_slurs(verdict, raw, base, suspect, tokens)
        self._scan_threats(verdict, suspect, tokens)
        self._scan_profanity(verdict, tokens, has_user_mention)
        self._scan_sexual(verdict, tokens)
        self._scan_noise(verdict, raw, mention_count)
        if check_spam and author_id is not None:
            self._scan_spam(verdict, author_id, base)
        return verdict

    def _scan_slurs(
        self,
        verdict: Verdict,
        raw: str,
        base: str,
        suspect: Iterable[str],
        tokens: list[str],
    ) -> None:
        found: dict[str, list[str]] = defaultdict(list)
        evaded = False

        candidates = [canon(token) for token in suspect]
        candidates.extend(self._spaced_out_runs(tokens))

        for haystack in candidates:
            if len(haystack) < 3:
                continue
            for category, term, cterm in self.slur_canon:
                if cterm in haystack and term not in found[category]:
                    found[category].append(term)
                    if term not in base:
                        evaded = True

        for category, terms in found.items():
            verdict.hits.append(
                Hit(
                    category=category,
                    severity="severe",
                    terms=terms,
                    detail=f"Slur detected ({category}).",
                )
            )

        if found and evaded:
            verdict.hits.append(
                Hit(
                    category="bypass",
                    severity="severe",
                    terms=[],
                    detail="The term was disguised with spacing, symbols or repeated letters.",
                )
            )

    @staticmethod
    def _spaced_out_runs(tokens: list[str]) -> list[str]:
        runs: list[str] = []
        current: list[str] = []
        for token in tokens:
            if len(token) <= 2:
                current.append(token)
                continue
            if len(current) >= 3:
                runs.append(canon("".join(current)))
            current = []
        if len(current) >= 3:
            runs.append(canon("".join(current)))
        return runs

    def _scan_threats(self, verdict: Verdict, suspect: Iterable[str], tokens: list[str]) -> None:
        joined = canon("".join(tokens))
        found = [term for term, cterm in self.threat_canon if cterm and cterm in joined]
        if found:
            verdict.hits.append(
                Hit("threats", "severe", found, "Threatening language directed at a member.")
            )

    def _scan_profanity(self, verdict: Verdict, tokens: list[str], has_user_mention: bool) -> None:
        strong_hits: list[str] = []
        mild_hits: list[str] = []
        for token in tokens:
            if token in self.allow:
                continue
            root = stem(token)
            if token in self.strong or root in self.strong:
                strong_hits.append(token)
            elif token in self.mild or root in self.mild:
                mild_hits.append(token)

        if not strong_hits and not mild_hits:
            return

        score = len(strong_hits) * 2 + len(mild_hits)
        second_person = any(t in {"you", "u", "ur", "youre", "yourself", "urself"} for t in tokens)
        targeted = bool(strong_hits) and (has_user_mention or second_person)

        if targeted:
            verdict.hits.append(
                Hit(
                    "targeted_profanity",
                    "high",
                    strong_hits,
                    "Profanity aimed at another member.",
                )
            )
        elif len(strong_hits) >= config.FILTER_MAX_PROFANITY_PER_MESSAGE or score >= 6:
            verdict.hits.append(
                Hit(
                    "excessive_profanity",
                    "high",
                    strong_hits + mild_hits,
                    f"Profanity used {len(strong_hits) + len(mild_hits)} times in one message.",
                )
            )

    def _scan_sexual(self, verdict: Verdict, tokens: list[str]) -> None:
        found = [t for t in tokens if t in self.sexual or stem(t) in self.sexual]
        if found:
            verdict.hits.append(Hit("sexual", "moderate", found, "Sexual or NSFW content."))

    def _scan_noise(self, verdict: Verdict, raw: str, mention_count: int) -> None:
        stripped = _URL.sub("", raw)

        letters = [c for c in stripped if c.isalpha()]
        if len(letters) >= config.FILTER_MIN_CAPS_LENGTH:
            ratio = sum(1 for c in letters if c.isupper()) / len(letters)
            if ratio >= config.FILTER_MAX_CAPS_RATIO:
                verdict.hits.append(
                    Hit("caps", "low", [], f"{int(ratio * 100)} percent of the message is uppercase.")
                )

        if _REPEAT.search(stripped):
            verdict.hits.append(Hit("character_spam", "low", [], "Long runs of repeated characters."))

        if mention_count > config.FILTER_MAX_MENTIONS:
            verdict.hits.append(
                Hit("mass_mention", "moderate", [], f"{mention_count} mentions in one message.")
            )

        for pattern in self.invites:
            if pattern.search(raw):
                verdict.hits.append(
                    Hit("advertising", "moderate", [], "An external server invite was posted.")
                )
                break

    def _scan_spam(self, verdict: Verdict, author_id: int, base: str) -> None:
        now = time.monotonic()
        history = self._recent[author_id]
        history.append((now, base))

        window = [entry for entry in history if now - entry[0] <= config.FILTER_SPAM_WINDOW_SECONDS]
        if len(window) >= config.FILTER_SPAM_MESSAGE_COUNT:
            verdict.hits.append(
                Hit("spam", "moderate", [], f"{len(window)} messages in {config.FILTER_SPAM_WINDOW_SECONDS} seconds.")
            )
            return

        squashed = canon(base)
        if squashed:
            repeats = sum(1 for _, text in history if canon(text) == squashed)
            if repeats >= config.FILTER_DUPLICATE_THRESHOLD:
                verdict.hits.append(
                    Hit("spam", "moderate", [], f"The same message was sent {repeats} times.")
                )

    def forget(self, author_id: int) -> None:
        self._recent.pop(author_id, None)


chat_filter = ChatFilter()
