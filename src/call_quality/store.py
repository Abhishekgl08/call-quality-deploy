"""SQLite persistence for the local customer-call-quality MVP."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DATABASE_PATH = Path("data") / "call_quality.sqlite3"


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    finally:
        db.close()


def init_db() -> None:
    with connection() as db:
        db.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS calls (
          id TEXT PRIMARY KEY, source_filename TEXT NOT NULL, source_path TEXT NOT NULL,
          sha256 TEXT NOT NULL UNIQUE, call_date TEXT, agent_id TEXT, caller_id TEXT,
          team TEXT, product TEXT, source_system TEXT, channel_mapping TEXT,
          duration_seconds REAL, state TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL,
          completed_at TEXT, warnings_json TEXT NOT NULL DEFAULT '[]', analysis_json TEXT
        );
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES calls(id), state TEXT NOT NULL,
          stage TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0, timings_json TEXT NOT NULL DEFAULT '{}',
          warnings_json TEXT NOT NULL DEFAULT '[]', error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transcript_segments (
          id TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES calls(id), start_seconds REAL NOT NULL,
          end_seconds REAL NOT NULL, speaker TEXT NOT NULL, role TEXT, role_confidence REAL,
          raw_text TEXT NOT NULL, redacted_text TEXT NOT NULL, asr_confidence REAL
        );
        CREATE TABLE IF NOT EXISTS detections (
          id TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES calls(id), kind TEXT NOT NULL,
          category TEXT NOT NULL, severity TEXT NOT NULL, confidence REAL NOT NULL,
          start_seconds REAL, end_seconds REAL, evidence TEXT NOT NULL, source TEXT NOT NULL,
          source_version TEXT NOT NULL, review_status TEXT NOT NULL DEFAULT 'needs_review'
        );
        CREATE TABLE IF NOT EXISTS issue_records (
          id TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES calls(id), taxonomy_id TEXT NOT NULL,
          state TEXT NOT NULL, severity TEXT NOT NULL, urgency TEXT NOT NULL, resolution_state TEXT NOT NULL,
          confidence REAL NOT NULL, evidence TEXT NOT NULL, start_seconds REAL, end_seconds REAL,
          cluster_id TEXT, review_status TEXT NOT NULL DEFAULT 'needs_review'
        );
        CREATE TABLE IF NOT EXISTS quality_metrics (
          id TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES calls(id), metric_id TEXT NOT NULL,
          value REAL NOT NULL, calculation_version TEXT NOT NULL, evidence TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conduct_signals (
          id TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES calls(id), agent_id TEXT,
          category TEXT NOT NULL, severity TEXT NOT NULL, confidence REAL NOT NULL,
          start_seconds REAL, end_seconds REAL, evidence TEXT NOT NULL, source TEXT NOT NULL,
          source_version TEXT NOT NULL, review_status TEXT NOT NULL DEFAULT 'needs_review'
        );
        CREATE TABLE IF NOT EXISTS reviews (
          id TEXT PRIMARY KEY, detection_id TEXT NOT NULL REFERENCES detections(id), reviewer TEXT NOT NULL,
          verdict TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS compliance_rules (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, version TEXT NOT NULL, speaker_scope TEXT NOT NULL,
          pattern TEXT NOT NULL, severity TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS taxonomy (
          id TEXT PRIMARY KEY, parent_id TEXT, label TEXT NOT NULL, synonyms_json TEXT NOT NULL DEFAULT '[]',
          active INTEGER NOT NULL DEFAULT 1, version TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_log (
          id TEXT PRIMARY KEY, action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT,
          detail_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
        );
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(calls)").fetchall()}
        if "analysis_json" not in columns:
          db.execute("ALTER TABLE calls ADD COLUMN analysis_json TEXT")
        for column, definition in [("caller_id", "TEXT"), ("team", "TEXT"), ("product", "TEXT"), ("source_system", "TEXT")]:
          if column not in columns:
            db.execute(f"ALTER TABLE calls ADD COLUMN {column} {definition}")
        if not db.execute("SELECT 1 FROM compliance_rules LIMIT 1").fetchone():
            db.executemany("INSERT INTO compliance_rules VALUES (?,?,?,?,?,?,?)", [
                ("recording_notice", "Recording notice", "v1", "agent", r"\b(recorded|recording)\b", "warning", 1),
                ("refund_policy", "Refund wording review", "v1", "agent", r"\b(refund|money back)\b", "warning", 1),
            ])
        # Repair the early MVP seed's escaped word-boundary patterns without changing user rules.
        db.execute("UPDATE compliance_rules SET pattern=? WHERE id='recording_notice' AND pattern=?", (r"\b(recorded|recording)\b", r"\\b(recorded|recording)\\b"))
        db.execute("UPDATE compliance_rules SET pattern=? WHERE id='refund_policy' AND pattern=?", (r"\b(refund|money back)\b", r"\\b(refund|money back)\\b"))
        if not db.execute("SELECT 1 FROM taxonomy LIMIT 1").fetchone():
            db.executemany("INSERT INTO taxonomy VALUES (?,?,?,?,?,?)", [
                ("travel_disruption", None, "Travel disruption", "[\"missed flight\", \"delayed flight\"]", 1, "v1"),
                ("refund", None, "Refund request", "[\"refund\", \"money back\"]", 1, "v1"),
                ("cancellation", None, "Cancellation request", "[\"cancel\", \"cancellation\"]", 1, "v1"),
                ("login_access", None, "Login/access issue", "[\"login\", \"sign in\", \"password\"]", 1, "v1"),
                ("billing", None, "Billing issue", "[\"charged\", \"billing\", \"payment\"]", 1, "v1"),
                ("escalation", None, "Escalation", "[\"manager\", \"supervisor\", \"escalate\"]", 1, "v1"),
            ])


def recover_interrupted_jobs() -> None:
    """Mark jobs left active by a stopped process so uploads can retry them."""
    active = ("queued", "normalising", "transcribing", "diarizing", "analysing", "indexing")
    with connection() as db:
        placeholders = ",".join("?" * len(active))
        db.execute(f"UPDATE jobs SET state='failed',stage='interrupted',progress=100,error='Worker stopped before completion',updated_at=datetime('now') WHERE state IN ({placeholders})", active)
        db.execute(f"UPDATE calls SET state='failed',error='Worker stopped before completion' WHERE state IN ({placeholders})", active)


def rows(query: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connection() as db:
        return [dict(row) for row in db.execute(query, parameters).fetchall()]


def one(query: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    found = rows(query, parameters)
    return found[0] if found else None


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(value: str | None, default: Any) -> Any:
    return json.loads(value) if value else default
