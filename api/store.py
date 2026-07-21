"""
api.store
─────────
SQLite-backed study store. Survives container restarts when ./data
is mounted as a Docker volume.

Override the database path with the STUDIES_DB environment variable.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from typing import Optional

DB_PATH = os.environ.get('STUDIES_DB', os.path.abspath('./data/studies.db'))


@dataclass
class StudyRecord:
    study_id: str
    status: str = 'queued'        # queued | running | complete | failed
    stage: Optional[str] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    request_hash: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class StudyStore:
    def __init__(self, db_path: str = DB_PATH):
        self._db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS studies (
                    study_id TEXT PRIMARY KEY,
                    status   TEXT NOT NULL DEFAULT "queued",
                    stage    TEXT,
                    result   TEXT,
                    error    TEXT,
                    request_hash TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cols = {row[1] for row in conn.execute('PRAGMA table_info(studies)')}
            if 'request_hash' not in cols:
                conn.execute('ALTER TABLE studies ADD COLUMN request_hash TEXT')
            if 'created_at' not in cols:
                conn.execute('ALTER TABLE studies ADD COLUMN created_at TEXT')
            if 'updated_at' not in cols:
                conn.execute('ALTER TABLE studies ADD COLUMN updated_at TEXT')
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_studies_request_hash_status '
                'ON studies (request_hash, status)'
            )

    def create(self, study_id: str, request_hash: Optional[str] = None) -> StudyRecord:
        with self._lock, self._connect() as conn:
            conn.execute(
                'INSERT INTO studies (study_id, status, request_hash, created_at, updated_at) '
                'VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)',
                (study_id, 'queued', request_hash),
            )
        return StudyRecord(study_id=study_id, request_hash=request_hash)

    def get(self, study_id: str) -> Optional[StudyRecord]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT study_id, status, stage, result, error, request_hash, created_at, updated_at '
                'FROM studies WHERE study_id = ?',
                (study_id,),
            ).fetchone()
        if not row:
            return None
        sid, status, stage, result_json, error, request_hash, created_at, updated_at = row
        return StudyRecord(
            study_id=sid,
            status=status,
            stage=stage,
            result=json.loads(result_json) if result_json else None,
            error=error,
            request_hash=request_hash,
            created_at=created_at,
            updated_at=updated_at,
        )

    def find_by_request_hash(self, request_hash: str) -> Optional[StudyRecord]:
        """Return most relevant existing study for identical input payload."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                '''
                SELECT study_id, status, stage, result, error, request_hash
                FROM studies
                WHERE request_hash = ?
                AND (
                    status = 'complete'
                    OR (
                        status IN ('running', 'queued')
                        AND updated_at IS NOT NULL
                        AND updated_at >= datetime('now', '-2 hours')
                    )
                )
                ORDER BY
                    CASE status
                        WHEN 'complete' THEN 0
                        WHEN 'running' THEN 1
                        WHEN 'queued' THEN 2
                        ELSE 9
                    END,
                    rowid DESC
                LIMIT 1
                ''',
                (request_hash,),
            ).fetchone()
        if not row:
            return None
        sid, status, stage, result_json, error, req_hash = row
        return StudyRecord(
            study_id=sid,
            status=status,
            stage=stage,
            result=json.loads(result_json) if result_json else None,
            error=error,
            request_hash=req_hash,
        )

    def update(self, study_id: str, **kwargs) -> None:
        if 'result' in kwargs and isinstance(kwargs['result'], dict):
            kwargs['result'] = json.dumps(kwargs['result'])
        set_clause = ', '.join(f'{k} = ?' for k in kwargs)
        values = list(kwargs.values()) + [study_id]
        with self._lock, self._connect() as conn:
            conn.execute(
                f'UPDATE studies SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE study_id = ?',
                values,
            )


# Singleton used across the app
store = StudyStore()
