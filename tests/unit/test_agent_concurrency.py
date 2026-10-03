"""全局并发账本与上限：不能记账漏、不能记账留。

这一层存在的理由是「一次齐发能把本机打死」：每个回答都是一个全自主权限的
CLI 子进程。用例覆盖三件最容易写错的事：

1. 上限生效（而且**不排队**，直接 429）；
2. ``cancel()`` 路径不放掉记账的话，上限会被慢慢吃光（现象只是「用着用着就
   开始 429」，极难定位）；
3. 「运行中」列表能跨 provider 汇总 —— 只数当前实例会漏掉 A2A 那边的进程。
"""
from __future__ import annotations

import asyncio

import pytest

from lquant.agent.concurrency import (
    MAX_RUNS_MAX,
    MAX_RUNS_MIN,
    register_run,
    release_run,
    reset_running,
    running_count,
)
from lquant.agent.errors import AgentError
from lquant.agent.service import AgentService


@pytest.fixture(autouse=True)
def _clean_ledger():
    reset_running()
    yield
    reset_running()


@pytest.fixture()
def tmp_root(tmp_path, monkeypatch):
    """临时仓库根：``LQ_ROOT`` + 一份能用的 config/app.yaml。

    上限是从 ``config/app.yaml`` 派生 + ``app_setting`` 覆盖，所以「不落一份
    yaml」会让默认值直接吃 ``AgentConfig`` 的 4 —— 那样断言上限就测不出
    「配置真的被读到了」。
    """
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "app.yaml").write_text(
        "agent:\n  max_concurrent_runs: 8\n", encoding="utf-8")
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


class _FakeService:
    """只借给账本用的身份对象（WeakKeyDictionary 的键按对象身份分组）。"""


def test_bounds_are_shared_constants():
    assert (MAX_RUNS_MIN, MAX_RUNS_MAX) == (1, 64)


def test_register_and_release_are_symmetric():
    a, b = _FakeService(), _FakeService()
    register_run(a, "s1")
    register_run(b, "s1")  # 不同 service 上同名 sid 各算一条
    assert running_count() == 2
    release_run(a, "s1")
    assert running_count() == 1
    # 重复释放不该把别人的额度也扣掉
    release_run(a, "s1")
    assert running_count() == 1
    release_run(b, "s1")
    assert running_count() == 0


class _Svc(AgentService):
    """最小 AgentService 子类：只保留 _claim / _release / running_runs。

    直接复用真实实现（而不是抄一份 claim/release 逻辑），是为了让「记账挂在
    哪」这件事真的被测到 —— 抄一份的话，源码里把 ``register_run`` 挪走都不会
    有测试变红。
    """

    provider = "fake"

    def __init__(self) -> None:  # noqa: D107 - 绕开 SessionStore 依赖
        self.store = None  # type: ignore[assignment]
        self._tasks: dict = {}
        self._run_meta: dict = {}

    async def create_session(self, context, agent_config=None, *, title=None):
        raise NotImplementedError

    async def list_sessions(self):
        raise NotImplementedError

    async def delete_session(self, sid):
        raise NotImplementedError

    async def get_messages(self, sid):
        raise NotImplementedError

    async def send_message(self, sid, content, on_event, user_msg=None):
        raise NotImplementedError

    async def cancel(self, sid):
        raise NotImplementedError


async def _claim(svc: _Svc, sid: str) -> None:
    """在独立任务里占槽位（_claim 会把 current_task 记进 _tasks）。"""

    async def body():
        svc._claim(sid)
        try:
            await asyncio.sleep(30)
        finally:
            svc._release(sid)

    task = asyncio.create_task(body())
    await asyncio.sleep(0)  # 让 body 跑到 sleep
    return task


async def test_cap_rejects_without_queueing(tmp_root):
    from lquant.core.settings_store import SettingsStore

    SettingsStore().put("agent.max_concurrent_runs", "1")
    a, b = _Svc(), _Svc()
    ta = await _claim(a, "s1")
    assert running_count() == 1
    # 第二个**不同会话**也被拒：上限是全局的，不是 per-service 的
    with pytest.raises(AgentError) as ei:
        b._claim("s2")
    assert ei.value.status_code == 429
    assert "上限" in str(ei.value)
    ta.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert running_count() == 0


async def test_cancel_path_does_not_leak_ledger(tmp_root):
    """``cancel()`` 先 pop 掉任务再 cancel，记账仍必须被放掉。

    这是最容易漏的一条：``_release`` 里的身份判断（``_tasks.get(sid) is
    current_task()``）在取消路径上**恒为假**，若把 release_run 写在那个 if
    里面，账本就会留下永久记录。
    """
    svc = _Svc()
    task = await _claim(svc, "s1")
    assert running_count() == 1
    svc._tasks.pop("s1", None)  # 模拟 cancel() 的先 pop
    task.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert running_count() == 0
    assert svc._run_meta == {}


async def test_running_runs_reports_elapsed_and_meta(tmp_root):
    svc = _Svc()
    task = await _claim(svc, "s1")
    svc.set_run_info("s1", pid=4242, workspace="/tmp/ws")
    rows = svc.running_runs()
    assert [r["session_id"] for r in rows] == ["s1"]
    assert rows[0]["pid"] == 4242
    assert rows[0]["workspace"] == "/tmp/ws"
    assert rows[0]["provider"] == "fake"
    assert rows[0]["elapsed_seconds"] >= 0
    task.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert svc.running_runs() == []


async def test_running_runs_all_sees_every_service(tmp_root):
    """跨 provider 汇总：只看某一个实例会漏掉「A2A 那边正在跑」的那部分。"""
    from lquant.agent import service as svc_mod

    a, b = _Svc(), _Svc()
    ta, tb = await _claim(a, "sa"), await _claim(b, "sb")
    svc_mod._ALL_SERVICES.add(a)
    svc_mod._ALL_SERVICES.add(b)
    try:
        ids = {r["session_id"] for r in svc_mod.running_runs_all()}
        assert {"sa", "sb"} <= ids
    finally:
        ta.cancel()
        tb.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
