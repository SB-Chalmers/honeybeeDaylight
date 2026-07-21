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
                    error    TEXT
                )
            ''')

    def create(self, study_id: str) -> StudyRecord:
        with self._lock, self._connect() as conn:
            conn.execute(
                'INSERT INTO studies (study_id, status) VALUES (?, ?)',
                (study_id, 'queued'),
            )
        return StudyRecord(study_id=study_id)

    def get(self, study_id: str) -> Optional[StudyRecord]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT study_id, status, stage, result, error '
                'FROM studies WHERE study_id = ?',
                (study_id,),
            ).fetchone()
        if not row:
            return None
        sid, status, stage, result_json, error = row
        return StudyRecord(
            study_id=sid,
            status=status,
            stage=stage,
            result=json.loads(result_json) if result_json else None,
            error=error,
        )

    def update(self, study_id: str, **kwargs) -> None:
        if 'result' in kwargs and isinstance(kwargs['result'], dict):
            kwargs['result'] = json.dumps(kwargs['result'])
        set_clause = ', '.join(f'{k} = ?' for k in kwargs)
        values = list(kwargs.values()) + [study_id]
        with self._lock, self._connect() as conn:
            conn.execute(
                f'UPDATE studies SET {set_clause} WHERE study_id = ?',
                values,
            )


# Singleton used across the app
store = StudyStore()
