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


#: 「另开会话」时转述历史的预算（字符）。为什么要有上限：一段跑了几十轮的
#: 会话原文远超 CLI 的输入上限，硬塞进去的结果是**整个请求直接报错**（不是
#: 截断），用户看到的是「换后端就崩了」。宁可只要最近的一段。
BRIEFING_MAX_CHARS = 12000


def render_briefing(messages: list[Message],
                    max_chars: int = BRIEFING_MAX_CHARS) -> str:
    """把一段对话渲染成「换后端」用的上下文简报。

    **从最近往前取**，取满预算为止，最后再翻回时间顺序 —— 用户接着问的几乎
    总是最后那几轮，而最早那几轮（往往是「你好」这类寒暄）丢掉毫无损失。
    反过来的写法（从头截取）会把最关键的最新上下文切掉，是最容易写错的一版。

    只带 user / assistant 两种角色：``system`` 行是运行时噪声（MCP 白名单之类的
    本机信息），转述给另一个后端既没用、又等于把本机配置写进别人家的 prompt。
    空的 assistant 消息也要跳过：它是「正在生成」的占位，转述出去就是一句
    「Assistant: 」。
    """
    parts: list[str] = []
    used = 0
    latest: tuple[str, str] | None = None
    for m in reversed(messages):
        if m.role not in ("user", "assistant"):
            continue
        text = (m.content or "").strip()
        if not text:
            continue
        who = "用户" if m.role == "user" else "助手"
        if latest is None:
            latest = (who, text)
        block = f"{who}：{text}"
        if used + len(block) > max_chars:
            break
        parts.append(block)
        used += len(block)
    if not parts and latest is not None:
        # 最近这一条本身就超预算：**截尾保留**。截头留尾是因为「最后说的那件事」
        # 才是接下来要接的话；整段丢掉等于换后端之后从零开始，用户会以为
        # 「这后端看不懂上下文」。
        who, text = latest
        head = f"{who}：…（前文略）"
        parts.append(head + text[-(max_chars - len(head)):])
    return "\n\n".join(reversed(parts))


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
                     agent_config: dict | None = None,
                     title: str | None = None) -> Session:
        """建会话。``agent_config`` 是会话级能力配置（provider/skills/mcp_tools）。

        provider 建后不可改：它决定 CLI 侧会话 id 的口径（claude 的 session_id /
        codex 的 thread_id 共用一列），中途换 provider 续接的就是别人的会话，
        所以 provider 在创建时锁定。skills / mcp_tools 可以会话内改，走
        :meth:`set_agent_config`（工作区每轮重建，下一轮生效）。

        ``title`` 只在「另开会话」（fork）时用得上：新会话是旧会话的延续，
        标题带上来源比多一条「新会话」好找得多。默认仍是 ``新会话``。
        """
        con = await self._conn()
        cfg = agent_config or {}
        ses = Session(id=uuid.uuid4().hex, context=context or {}, created_at=_now(),
                      agent_config=cfg)
        if title and title.strip():
            ses.title = title.strip()
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
                          tool_calls: list[dict] | None = None,
                          mid: str | None = None) -> Message:
        """落一条消息。

        ``mid`` 允许调用方**预先指定 id**：取消/超时路径需要在「消息已经在库里」
        之前就知道它的 id（取消可能正好落在 INSERT 的 await 上，那时拿不到返回
        值），先登记 id 才能保证后续补文案找得到目标行。
        """
        con = await self._conn()
        m = Message(id=mid or uuid.uuid4().hex, session_id=sid, role=role, content=content,
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

    async def get_message(self, sid: str, mid: str) -> Message | None:
        con = await self._conn()
        cur = await con.execute(
            "SELECT id,session_id,role,content,tool_calls_json,created_at FROM ask_messages "
            "WHERE id=? AND session_id=?", (mid, sid))
        r = await cur.fetchone()
        return self._row_message(r) if r else None

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

    async def delete_assistant_after(self, sid: str, message_id: str) -> int:
        """删掉 ``message_id`` **之后**的所有 assistant 消息，返回删除条数。

        给「重新生成」用：重跑同一个问题时，上一轮的答案必须先消失 —— 否则
        同一条问题下面会摞着两个答案，前端看起来像是「它答了两遍」，而落库
        的那份事实源也分不清哪个才是当前答案。

        为什么按 ``(created_at, rowid)`` 的排序位置比而不是比时间戳：同一毫秒内
        落库的两条消息时间戳完全相同（``_now`` 是毫秒精度），只比时间戳会漏删
        或误删。排序口径与 :meth:`messages` **一致**（那边也是 created_at, rowid）——
        两处不一致会导致「界面上看到的顺序」与「删的是哪几条」对不上。
        """
        con = await self._conn()
        cur = await con.execute(
            "DELETE FROM ask_messages WHERE session_id=? AND role='assistant' "
            "AND (created_at, rowid) > ("
            "  SELECT created_at, rowid FROM ask_messages WHERE id=? AND session_id=?"
            ")",
            (sid, message_id, sid))
        await con.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

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
