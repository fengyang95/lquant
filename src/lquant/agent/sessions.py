"""会话/消息 SQLite 持久化（aiosqlite）。落库消息是事实源。

`a2a_tasks` 表也在这里：A2A 的 `contextId` 就是 `ask_sessions.id`，
两边共用同一会话事实源（外部 Agent 通过 A2A 问的问题，在 /ask 页面能看到），
所以任务记录与消息放同一个库、同一个连接，不开第二个 aiosqlite 连接。
"""
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
    created_at   TEXT NOT NULL,
    claude_session_id TEXT NOT NULL DEFAULT ''
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
CREATE TABLE IF NOT EXISTS a2a_tasks (
    task_id    TEXT PRIMARY KEY,
    context_id TEXT NOT NULL,
    state      TEXT NOT NULL,
    message_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_a2a_tasks_ctx ON a2a_tasks(context_id, created_at);
"""


async def _migrate(db: aiosqlite.Connection) -> None:
    """幂等迁移：老库缺列则补上（新建库由 _DDL 直接带列）。"""
    try:
        await db.execute(
            "ALTER TABLE ask_sessions ADD COLUMN claude_session_id TEXT NOT NULL DEFAULT ''")
        await db.commit()
    except aiosqlite.OperationalError as e:
        if "duplicate column name" not in str(e):
            raise  # 列已存在属预期；其他迁移错误向上抛


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
                await _migrate(db)
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
        # 会话是一致性边界：A2A 任务记录随会话一起清，别留孤儿行
        await con.execute("DELETE FROM a2a_tasks WHERE context_id=?", (sid,))
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

    async def get_claude_session_id(self, sid: str) -> str | None:
        con = await self._conn()
        cur = await con.execute(
            "SELECT claude_session_id FROM ask_sessions WHERE id=?", (sid,))
        r = await cur.fetchone()
        if r is None:
            return None
        return r[0] or None

    async def set_claude_session_id(self, sid: str, claude_sid: str) -> None:
        con = await self._conn()
        await con.execute(
            "UPDATE ask_sessions SET claude_session_id=? WHERE id=?", (claude_sid, sid))
        await con.commit()

    # ---- A2A 任务记录（薄 SQL 层；状态机语义在 agent/a2a/tasks.py） -------

    async def create_a2a_task(self, task_id: str, context_id: str, state: str,
                              message_id: str = "") -> None:
        con = await self._conn()
        now = _now()
        await con.execute(
            "INSERT INTO a2a_tasks "
            "(task_id, context_id, state, message_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (task_id, context_id, state, message_id, now, now))
        await con.commit()

    async def get_a2a_task(self, task_id: str) -> dict | None:
        con = await self._conn()
        cur = await con.execute(
            "SELECT task_id, context_id, state, message_id, created_at, updated_at "
            "FROM a2a_tasks WHERE task_id=?", (task_id,))
        r = await cur.fetchone()
        if r is None:
            return None
        return {"task_id": r[0], "context_id": r[1], "state": r[2],
                "message_id": r[3], "created_at": r[4], "updated_at": r[5]}

    async def set_a2a_task_state(self, task_id: str, state: str) -> None:
        con = await self._conn()
        await con.execute(
            "UPDATE a2a_tasks SET state=?, updated_at=? WHERE task_id=?",
            (state, _now(), task_id))
        await con.commit()

    async def list_a2a_tasks(self, context_id: str, limit: int = 50) -> list[dict]:
        con = await self._conn()
        cur = await con.execute(
            "SELECT task_id, context_id, state, message_id, created_at, updated_at "
            "FROM a2a_tasks WHERE context_id=? ORDER BY created_at DESC LIMIT ?",
            (context_id, int(limit)))
        return [{"task_id": r[0], "context_id": r[1], "state": r[2],
                 "message_id": r[3], "created_at": r[4], "updated_at": r[5]}
                for r in await cur.fetchall()]
