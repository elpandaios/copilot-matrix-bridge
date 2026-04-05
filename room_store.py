"""Persist room state (project path, copilot session ID, mode) in local SQLite."""

import sqlite3
import uuid
from pathlib import Path
from dataclasses import dataclass
from typing import Optional


@dataclass
class RoomState:
    room_id: str
    project_path: Optional[str] = None
    session_id: Optional[str] = None
    mode: str = "chat"  # chat | plan | auto


class RoomStore:
    def __init__(self, db_path: str = "bridge.db"):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._init_db()

    def _init_db(self):
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS rooms (
                room_id TEXT PRIMARY KEY,
                project_path TEXT,
                session_id TEXT,
                mode TEXT DEFAULT 'chat'
            )
        """)
        self._conn.commit()

    def get(self, room_id: str) -> RoomState:
        row = self._conn.execute(
            "SELECT room_id, project_path, session_id, mode FROM rooms WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        if row:
            return RoomState(*row)
        return RoomState(room_id=room_id)

    def ensure_session(self, room_id: str) -> RoomState:
        """Get room state, creating a copilot session ID if needed."""
        state = self.get(room_id)
        if not state.session_id:
            state.session_id = str(uuid.uuid4())
            self._upsert(state)
        return state

    def set_project(self, room_id: str, project_path: str) -> RoomState:
        state = self.get(room_id)
        state.project_path = project_path
        # New project = new copilot session
        state.session_id = str(uuid.uuid4())
        self._upsert(state)
        return state

    def set_mode(self, room_id: str, mode: str) -> RoomState:
        state = self.get(room_id)
        state.mode = mode
        self._upsert(state)
        return state

    def reset_session(self, room_id: str) -> RoomState:
        """Clear copilot session so next message starts fresh."""
        state = self.get(room_id)
        state.session_id = None
        self._upsert(state)
        return state

    def _upsert(self, state: RoomState):
        self._conn.execute(
            """INSERT INTO rooms (room_id, project_path, session_id, mode)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(room_id) DO UPDATE SET
                 project_path = excluded.project_path,
                 session_id = excluded.session_id,
                 mode = excluded.mode""",
            (state.room_id, state.project_path, state.session_id, state.mode),
        )
        self._conn.commit()
