"""``agent/spawn.py`` 单测：posix_spawn 子进程封装。

正常路径（起进程、读管道、等退出）由 provider 层用例间接覆盖，这里专门盯
两条**只在该出事时才走**的分支 —— 它们正是「fd 泄漏」与「孤儿进程」的来源：

- ``posix_spawn`` 抛错 → 已建好的四个管道 fd 必须全部关闭；
- 子进程被别处 ``waitpid`` 抢收 → ``returncode`` 不能炸（ChildProcessError 兜底）。
"""

from __future__ import annotations

import os
import sys

import pytest

from lquant.agent import spawn as spawn_mod
from lquant.agent.spawn import spawn_child

_PY = sys.executable


async def _drain(reader) -> str:  # noqa: ANN001 - asyncio.StreamReader
    """读到 EOF，避免子进程因管道写满而阻塞。"""
    chunks: list[bytes] = []
    while True:
        line = await reader.readline()
        if not line:
            break
        chunks.append(line)
    return b"".join(chunks).decode()


async def test_spawn_captures_stdout_and_reports_exit_code(tmp_path):
    child = spawn_child([_PY, "-c", "print('hello-spawn')"], cwd=str(tmp_path))
    await child.attach()
    out = await _drain(child.stdout)
    await _drain(child.stderr)

    assert child.pid > 0  # pid 属性
    assert "hello-spawn" in out
    assert await child.wait() == 0
    assert child.returncode == 0


async def test_nonzero_exit_code_is_preserved(tmp_path):
    child = spawn_child([_PY, "-c", "raise SystemExit(7)"], cwd=str(tmp_path))
    await child.attach()
    await _drain(child.stdout)
    await _drain(child.stderr)

    assert await child.wait() == 7
    assert child.returncode == 7


async def test_cwd_is_honoured_via_shell_wrapper(tmp_path):
    """posix_spawn 没有 chdir，靠 `sh -c 'cd "$1" && …'` 包一层 —— 别包错。"""
    child = spawn_child([_PY, "-c", "import os;print(os.getcwd())"],
                        cwd=str(tmp_path))
    await child.attach()
    out = await _drain(child.stdout)
    await _drain(child.stderr)
    await child.wait()

    assert os.path.realpath(out.strip()) == os.path.realpath(str(tmp_path))


async def test_stdin_is_devnull_so_cli_cannot_block_on_input(tmp_path):
    """stdin 必须接 /dev/null：codex 检测到打开的管道会再读一段 stdin 而挂死。"""
    child = spawn_child(
        [_PY, "-c", "import sys;print(len(sys.stdin.read()))"], cwd=str(tmp_path))
    await child.attach()
    out = await _drain(child.stdout)
    await _drain(child.stderr)
    assert await child.wait() == 0
    assert out.strip() == "0"  # 读到 EOF，不是「等输入」


async def test_terminate_and_reap_collects_hung_child(tmp_path):
    child = spawn_child([_PY, "-c", "import time;time.sleep(60)"],
                        cwd=str(tmp_path))
    await child.attach()
    await child.reap(grace=5.0)

    assert child.returncode is not None  # 已回收，不是孤儿


def test_spawn_failure_closes_every_pipe_fd(monkeypatch, tmp_path):
    """posix_spawn 抛错时四个管道 fd 必须全关 —— 否则每次失败泄漏 4 个 fd。"""
    created: list[int] = []
    closed: list[int] = []
    real_pipe, real_close = os.pipe, os.close

    def spy_pipe():
        r, w = real_pipe()
        created.extend([r, w])
        return r, w

    def spy_close(fd):
        closed.append(fd)
        return real_close(fd)

    def boom(*_args, **_kwargs):
        raise OSError("spawn 失败")

    monkeypatch.setattr(spawn_mod.os, "pipe", spy_pipe)
    monkeypatch.setattr(spawn_mod.os, "close", spy_close)
    monkeypatch.setattr(spawn_mod.os, "posix_spawn", boom)

    with pytest.raises(OSError, match="spawn 失败"):
        spawn_child(["/bin/echo", "x"], cwd=str(tmp_path))

    assert len(created) == 4
    assert set(created) <= set(closed)


async def test_returncode_tolerates_child_reaped_elsewhere(tmp_path):
    """被别处 waitpid 抢收后，returncode 走 ChildProcessError 兜底而不是抛异常。"""
    child = spawn_child([_PY, "-c", "pass"], cwd=str(tmp_path))
    try:
        os.waitpid(child.pid, 0)  # 抢在 returncode 之前回收
        assert child.returncode is None  # _waited 未记录，但不能炸
    finally:
        for fd in (child._out_r, child._err_r):  # noqa: SLF001 - 测试内收尾
            os.close(fd)
