"""SQLite transcript log.

Every line the narrator speaks is written here with the facts that triggered
it. This is what the operator reads back afterwards to work out why a line
fired when it did, and which templates need rewriting.

Writes are best-effort: a logging failure must never take the stream down.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS lines (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT    NOT NULL,
    market_time  TEXT    NOT NULL,   -- adapter clock (virtual under replay)
    wall_time    TEXT    NOT NULL,   -- real clock
    template_id  TEXT    NOT NULL,
    source       TEXT    NOT NULL,   -- template | bridge | override
    priority     INTEGER NOT NULL,
    text         TEXT    NOT NULL,
    emote        TEXT,
    dry_run      INTEGER NOT NULL DEFAULT 0,
    facts        TEXT    NOT NULL    -- json snapshot at the moment of speaking
);
CREATE INDEX IF NOT EXISTS lines_run_idx ON lines(run_id);
CREATE INDEX IF NOT EXISTS lines_template_idx ON lines(template_id);

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    symbol      TEXT,
    mode        TEXT,
    config      TEXT
);
"""


class SpeechLog:
    def __init__(self, path: Path, run_id: str) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self._conn: sqlite3.Connection | None = None

    def open(self, *, symbol: str, mode: str, config_summary: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path)
            # Default rollback-journal + full fsync commits measured at 7ms
            # typical and 200ms worst case on this machine. Once Milestone 4
            # is pushing 60fps visemes off the same event loop, a 200ms stall
            # is a dozen dropped frames. WAL plus synchronous=NORMAL keeps
            # commits off the fsync path; the only exposure is losing the
            # last few transcript rows on a hard power cut, which is an
            # acceptable trade for a log.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT OR REPLACE INTO runs (run_id, started_at, symbol, mode, config)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    self.run_id,
                    datetime.now(UTC).isoformat(),
                    symbol,
                    mode,
                    json.dumps(config_summary, default=str),
                ),
            )
            self._conn.commit()
        except (sqlite3.Error, OSError) as exc:
            # OSError as well as sqlite3.Error: an unwritable directory raises
            # from mkdir long before sqlite is involved, and a logging failure
            # must never take the stream down.
            log.error("could not open the transcript log at %s: %s", self.path, exc)
            self._conn = None

    def write(
        self,
        *,
        market_time: datetime,
        template_id: str,
        source: str,
        priority: int,
        text: str,
        emote: str | None,
        facts: dict[str, Any],
        dry_run: bool,
    ) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT INTO lines (run_id, market_time, wall_time, template_id,"
                " source, priority, text, emote, dry_run, facts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.run_id,
                    market_time.isoformat(),
                    datetime.now(UTC).isoformat(),
                    template_id,
                    source,
                    priority,
                    text,
                    emote,
                    int(dry_run),
                    json.dumps(_jsonable(facts), default=str),
                ),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            log.warning("transcript log write failed: %s", exc)

    def close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(sqlite3.Error):
                self._conn.close()
            self._conn = None


def _jsonable(facts: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in facts.items():
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        elif isinstance(value, dict):
            out[key] = {str(k): v for k, v in value.items()}
        else:
            out[key] = value
    return out
