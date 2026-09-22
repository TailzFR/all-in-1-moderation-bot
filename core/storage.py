from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("manager.storage")

BACKUP_INTERVAL_SECONDS = 15 * 60


def _default_state() -> dict[str, Any]:
    return {
        "version": 3,
        "created_at": time.time(),
        "counters": {"case": 0, "ticket": 0, "appeal": 0},
        "users": {},
        "tickets": {},
        "open_by_user": {},
        "channel_index": {},
        "cases": {},
        "appeals": {},
        "appeal_by_user": {},
        "schedules": [],
        "panel": {},
        "guild": {},
        "snippets": {},
        "stats": {},
    }


class JSONStore:
    def __init__(
        self,
        path: Path,
        backup_dir: Path,
        backup_keep: int = 40,
        flush_interval: float = 5.0,
    ) -> None:
        self.path = Path(path)
        self.backup_dir = Path(backup_dir)
        self.backup_keep = backup_keep
        self.flush_interval = flush_interval

        self._lock = threading.RLock()
        self._dirty = False
        self._closed = False
        self._flusher: asyncio.Task | None = None
        self._last_backup = 0.0

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        self.data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        candidates = [self.path]
        candidates.extend(sorted(self.backup_dir.glob("store-*.json"), reverse=True))

        primary_failed = False
        for candidate in candidates:
            if not candidate.exists() or candidate.stat().st_size == 0:
                continue
            try:
                with candidate.open("r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Could not read %s (%s); trying previous revision.", candidate.name, exc)
                if candidate == self.path:
                    primary_failed = True
                continue

            if candidate != self.path:
                log.warning("Recovered memory from backup %s.", candidate.name)
            return self._migrate(loaded)

        if primary_failed:
            quarantine = self.path.with_name(f"{self.path.stem}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}.json")
            try:
                shutil.move(str(self.path), str(quarantine))
                log.error(
                    "The memory file could not be parsed and no usable backup exists. "
                    "It has been set aside as %s and a fresh store was started.",
                    quarantine.name,
                )
            except OSError:
                log.error("The memory file is unreadable and could not be set aside.")
        else:
            log.info("No existing memory found. Starting a fresh store.")
        return _default_state()

    @staticmethod
    def _migrate(loaded: dict[str, Any]) -> dict[str, Any]:
        state = _default_state()
        for key, value in loaded.items():
            state[key] = value
        for key, value in _default_state().items():
            state.setdefault(key, value)
        state["version"] = 3
        return state

    def section(self, name: str) -> dict[str, Any]:
        with self._lock:
            node = self.data.get(name)
            if node is None:
                node = {}
                self.data[name] = node
            return node

    def mark_dirty(self) -> None:
        with self._lock:
            self._dirty = True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.data)

    def next_id(self, counter: str) -> int:
        with self._lock:
            counters = self.data.setdefault("counters", {})
            value = int(counters.get(counter, 0)) + 1
            counters[counter] = value
            self._dirty = True
            return value

    def bump_stat(self, name: str, amount: int = 1) -> int:
        with self._lock:
            stats = self.data.setdefault("stats", {})
            value = int(stats.get(name, 0)) + amount
            stats[name] = value
            self._dirty = True
            return value

    def flush(self, force: bool = False) -> bool:
        with self._lock:
            if not force and not self._dirty:
                return False
            payload = json.dumps(self.data, indent=2, ensure_ascii=False, default=str)
            self._dirty = False

        tmp_path = self.path.with_suffix(".json.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if self.path.exists():
                self._rotate_backup()
            os.replace(tmp_path, self.path)
        except OSError as exc:
            log.error("Failed to persist memory: %s", exc)
            with self._lock:
                self._dirty = True
            return False
        return True

    def _rotate_backup(self) -> None:
        now = time.time()
        if now - self._last_backup < BACKUP_INTERVAL_SECONDS:
            return
        self._last_backup = now

        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.backup_dir / f"store-{stamp}.json"
        try:
            if not target.exists():
                shutil.copy2(self.path, target)
        except OSError as exc:
            log.warning("Backup rotation failed: %s", exc)
            return

        backups = sorted(self.backup_dir.glob("store-*.json"), reverse=True)
        for stale in backups[self.backup_keep:]:
            try:
                stale.unlink()
            except OSError:
                pass

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        if self._flusher is not None:
            return
        loop = loop or asyncio.get_event_loop()
        self._flusher = loop.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self.flush_interval)
                await asyncio.to_thread(self.flush)
        except asyncio.CancelledError:
            pass

    async def close(self) -> None:
        self._closed = True
        if self._flusher is not None:
            self._flusher.cancel()
            try:
                await self._flusher
            except asyncio.CancelledError:
                pass
            self._flusher = None
        await asyncio.to_thread(self.flush, True)
