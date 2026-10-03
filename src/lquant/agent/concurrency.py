"""正在跑的 agent 回答的全局账本 + 并发上限。

**为什么需要一个全局账本，而不是各 service 自己数。** 运行期的执行体分布在
多个 ``AgentService`` 实例上（provider × store 各一份：claude_code / codex / mock，
A2A 执行器还自带一个 store 实例）。只看某一个实例的 ``_tasks`` 会漏掉别人 ——
于是「并发上限」在跨 provider 的场景下形同虚设（每个 provider 都能各开满上限），
而「查看运行中的 agent」也只看得见当前 provider 的那一半。

账本按 **service 实例**分组（``WeakKeyDictionary``，键是对象身份）：
``sid`` 只有在同一个 service 里才唯一（不同库的会话 id 理论上不会撞，但
``_tasks`` 的语义本来就是「这个实例上的槽位」，账本跟着它走才不会出现
「A 实例释放了 B 实例的槽位」）。用弱引用是为了不让账本把已经没人引用的
service 永久钉在内存里。

**记账的唯一入口是 ``AgentService._claim`` / ``_release``**：那两个方法已经是
「占槽位 / 放槽位」的唯一实现，账本挂在那里，就不会出现「跑了但没记账」
（漏记账 → 并发上限被绕过，而这是最不容易被发现的一类失效）。
"""
from __future__ import annotations

import weakref
from typing import Any

#: 并发上限的合法区间。**唯一一份**：``SettingsStore._validate_extra``（写入侧）、
#: ``agent.runtime._parse_override``（脏数据兜底）都引用它。
MAX_RUNS_MIN = 1
MAX_RUNS_MAX = 64

#: service 实例 → 该实例上正在跑的 sid 集合。
_running: weakref.WeakKeyDictionary[Any, set[str]] = weakref.WeakKeyDictionary()


def register_run(service: Any, sid: str) -> None:
    """占一个槽位（由 ``AgentService._claim`` 调用）。"""
    _running.setdefault(service, set()).add(sid)


def release_run(service: Any, sid: str) -> None:
    """放掉一个槽位（由 ``AgentService._release`` 调用）。

    只在自己确实占着的时候才放：``_release`` 的语义是「只释放自己的槽位」，
    别人接手后不能被我们 pop 掉（参见 ``AgentService._release``）。
    """
    held = _running.get(service)
    if held is not None:
        held.discard(sid)


def running_count() -> int:
    """当前全局正在跑的回答数（并发上限的判定依据）。"""
    return sum(len(v) for v in list(_running.values()))


def reset_running() -> None:
    """清空账本。**只给测试用**：生产路径的记账必须由 claim/release 配对完成，
    留一个可以随手清的口子很容易被当成「绕过并发上限」的捷径。"""
    _running.clear()
