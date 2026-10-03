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
    claude_session_id TEXT NOT NULL DEFAULT '',
    agent_config_json TEXT NOT NULL DEFAULT '{}'
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


#: 幂等补列清单：(列名, DDL)。新建库由 _DDL 直接带列，老库靠这里补齐。
#:
#: **每列必须各自 try/except**：写在同一个 try 里的话，第一列抛 duplicate
#: 会直接跳到 except，后面的列永远补不上 —— 而「只缺后面那一列」恰恰是
#: 最常见的档位（每加一列，中间状态的老库就落在这一档）。
_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("claude_session_id",
     "ALTER TABLE ask_sessions ADD COLUMN claude_session_id TEXT NOT NULL DEFAULT ''"),
    ("agent_config_json",
     "ALTER TABLE ask_sessions ADD COLUMN agent_config_json TEXT NOT NULL DEFAULT '{}'"),
)


async def _migrate(db: aiosqlite.Connection) -> None:
    """幂等迁移：老库缺列则补上（新建库由 _DDL 直接带列）。"""
    for _column, ddl in _MIGRATIONS:
        try:
            await db.execute(ddl)
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

    @property
    def path(self) -> str:
        """库文件路径。service 的实例缓存按它区分库（见 service.get_agent_service）。"""
        return self._path

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
        return Session(id=r[0], title=r[1], context=json.loads(r[2]), created_at=r[3],
                       agent_config=json.loads(r[4] or "{}"))

    @staticmethod
    def _row_message(r) -> Message:
        return Message(id=r[0], session_id=r[1], role=r[2], content=r[3],
                       tool_calls=json.loads(r[4]), created_at=r[5])

    async def create(self, context: dict | None,
                     agent_config: dict | None = None) -> Session:
        """建会话。``agent_config`` 是会话级能力配置（provider/skills/mcp_tools）。

        provider 建后不可改：它决定 CLI 侧会话 id 的口径（claude 的 session_id /
        codex 的 thread_id 共用一列），中途换 provider 续接的就是别人的会话，
        所以 provider 在创建时锁定。skills / mcp_tools 可以会话内改，走
        :meth:`set_agent_config`（工作区每轮重建，下一轮生效）。
        """
        con = await self._conn()
        cfg = agent_config or {}
        ses = Session(id=uuid.uuid4().hex, context=context or {}, created_at=_now(),
                      agent_config=cfg)
        await con.execute(
            "INSERT INTO ask_sessions "
            "(id, title, context_json, created_at, agent_config_json) VALUES (?,?,?,?,?)",
            (ses.id, ses.title, json.dumps(ses.context, ensure_ascii=False), ses.created_at,
             json.dumps(cfg, ensure_ascii=False)))
        await con.commit()
        return ses

    async def list(self) -> list[Session]:
        con = await self._conn()
        cur = await con.execute(
            "SELECT id,title,context_json,created_at,agent_config_json FROM ask_sessions "
            "ORDER BY created_at DESC")
        return [self._row_session(r) for r in await cur.fetchall()]

    async def get(self, sid: str) -> Session | None:
        con = await self._conn()
        cur = await con.execute(
            "SELECT id,title,context_json,created_at,agent_config_json FROM ask_sessions "
            "WHERE id=?", (sid,))
        r = await cur.fetchone()
        return self._row_session(r) if r else None

    async def get_agent_config(self, sid: str) -> dict:
        """会话级能力配置；未设置（老会话 / A2A 建的）返回空 dict，由调用方回退全局默认。"""
        con = await self._conn()
        cur = await con.execute(
            "SELECT agent_config_json FROM ask_sessions WHERE id=?", (sid,))
        r = await cur.fetchone()
        if r is None:
            return {}
        return json.loads(r[0] or "{}")

    async def set_agent_config(self, sid: str, patch: dict) -> Session | None:
        """会话内改能力配置：``patch`` 与现有配置**浅合并**（只覆盖传进来的键）。

        返回值是**从库里读回**的 ``Session``（不是拼出来的），保证与落库一致；
        会话不存在返回 ``None``，由 API 层转 404。

        ``patch`` 里的 ``None`` 是有意义的值（= 该能力全开），所以合并时只按
        「键在不在」判断，不按值真假判断 —— ``{"skills": None}`` 必须真的把
        skills 覆盖成 None，而不是被当成「没传」。
        """
        con = await self._conn()
        cur = await con.execute(
            "SELECT id,title,context_json,created_at,agent_config_json FROM ask_sessions "
            "WHERE id=?", (sid,))
        r = await cur.fetchone()
        if r is None:
            return None
        # current 恒为 dict（落库时保证过：写成 "null" 会让整条会话读不出来）
        merged = {**json.loads(r[4] or "{}"), **(patch or {})}
        await con.execute(
            "UPDATE ask_sessions SET agent_config_json=? WHERE id=?",
            (json.dumps(merged, ensure_ascii=False), sid))
        await con.commit()
        cur = await con.execute(
            "SELECT id,title,context_json,created_at,agent_config_json FROM ask_sessions "
            "WHERE id=?", (sid,))
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

    # ---- CLI 侧会话 id（provider 无关；列名是历史遗留的 claude_session_id）----
    #
    # 语义：**当前 provider 的 CLI 会话 id**（claude 的 session_id / codex 的
    # thread_id）—— 一个 ask 会话同时只由配置里的那一个 provider 作答，
    # 所以两种 id 共用一列。列名带着 SQLite 无法直接改名的历史包袱，
    # 代码里一律用中性名 get/set_cli_session_id，别再新增第二个访问器
    # （两个名字读同一列正是「口径分叉」的温床）。

    async def get_cli_session_id(self, sid: str) -> str | None:
        con = await self._conn()
        cur = await con.execute(
            "SELECT claude_session_id FROM ask_sessions WHERE id=?", (sid,))
        r = await cur.fetchone()
        if r is None:
            return None
        return r[0] or None

    async def set_cli_session_id(self, sid: str, cli_sid: str) -> None:
        con = await self._conn()
        await con.execute(
            "UPDATE ask_sessions SET claude_session_id=? WHERE id=?", (cli_sid, sid))
        await con.commit()

    # 旧名保留（既有调用方与用例仍可用），实现只有上面一份
    get_claude_session_id = get_cli_session_id
    set_claude_session_id = set_cli_session_id

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
