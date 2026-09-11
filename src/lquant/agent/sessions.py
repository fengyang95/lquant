"""会话/消息 SQLite 持久化（aiosqlite）。落库消息是事实源。"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from lquant.agent.schemas import Message, Session

_DDL = """
CREATE TABLE IF NOT EXISTS ask_sessions (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT '新会话',
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ask_messages (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES ask_sessions(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL DEFAULT '',
    tool_calls_json TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ask_messages_sid ON ask_messages(session_id, created_at);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class SessionStore:
    def __init__(self, path: str) -> None:
        self._path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db: aiosqlite.Connection | None = None

    async def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            db: aiosqlite.Connection | None = None
            try:
                db = await aiosqlite.connect(self._path)
                await db.execute("PRAGMA foreign_keys = ON")
                await db.executescript(_DDL)
                await db.commit()
            except BaseException:
                if db is not None:
                    await db.close()
                self._db = None
                raise
            self._db = db
        return self._db

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @staticmethod
    def _row_session(r) -> Session:
        return Session(id=r[0], title=r[1], context=json.loads(r[2]), created_at=r[3])

    @staticmethod
    def _row_message(r) -> Message:
        return Message(id=r[0], session_id=r[1], role=r[2], content=r[3],
                       tool_calls=json.loads(r[4]), created_at=r[5])

    async def create(self, context: dict | None) -> Session:
        con = await self._conn()
        ses = Session(id=uuid.uuid4().hex, context=context or {}, created_at=_now())
        await con.execute(
            "INSERT INTO ask_sessions (id, title, context_json, created_at) VALUES (?,?,?,?)",
            (ses.id, ses.title, json.dumps(ses.context, ensure_ascii=False), ses.created_at))
        await con.commit()
        return ses

    async def list(self) -> list[Session]:
        con = await self._conn()
        cur = await con.execute(
            "SELECT id,title,context_json,created_at FROM ask_sessions ORDER BY created_at DESC")
        return [self._row_session(r) for r in await cur.fetchall()]

    async def get(self, sid: str) -> Session | None:
        con = await self._conn()
        cur = await con.execute(
            "SELECT id,title,context_json,created_at FROM ask_sessions WHERE id=?", (sid,))
        r = await cur.fetchone()
        return self._row_session(r) if r else None

    async def delete(self, sid: str) -> None:
        con = await self._conn()
        await con.execute("DELETE FROM ask_messages WHERE session_id=?", (sid,))
        await con.execute("DELETE FROM ask_sessions WHERE id=?", (sid,))
        await con.commit()

    async def add_message(self, sid: str, role: str, content: str,
                          tool_calls: list[dict] | None = None) -> Message:
        con = await self._conn()
        m = Message(id=uuid.uuid4().hex, session_id=sid, role=role, content=content,
                    tool_calls=tool_calls or [], created_at=_now())
        await con.execute(
            "INSERT INTO ask_messages "
            "(id,session_id,role,content,tool_calls_json,created_at) VALUES (?,?,?,?,?,?)",
            (m.id, sid, role, content, json.dumps(m.tool_calls, ensure_ascii=False), m.created_at))
        await con.commit()
        return m

    async def messages(self, sid: str) -> list[Message]:
        con = await self._conn()
        cur = await con.execute(
            "SELECT id,session_id,role,content,tool_calls_json,created_at FROM ask_messages "
            "WHERE session_id=? ORDER BY created_at, rowid", (sid,))
        return [self._row_message(r) for r in await cur.fetchall()]

    async def append_assistant_delta(self, sid: str, mid: str, text: str) -> None:
        con = await self._conn()
        await con.execute(
            "UPDATE ask_messages SET content = content || ? WHERE id=? AND session_id=?",
            (text, mid, sid))
        await con.commit()

    async def finish_assistant(self, sid: str, mid: str) -> None:
        """当前为空操作：content 已随 delta 增量落库。保留钩子便于实现方加后处理。"""
        return None

    async def delete_messages(self, sid: str) -> None:
        con = await self._conn()
        await con.execute("DELETE FROM ask_messages WHERE session_id=?", (sid,))
        await con.commit()
