"""跨 provider 的子进程启动：posix_spawn + 管道桥接。

为什么不用 ``asyncio.create_subprocess_exec``：它走 fork+exec。进程内
polars/pandas 已把 OpenBLAS 线程池拉起后，fork 的 pthread_atfork
prepare（``blas_thread_shutdown_``）会去 join 正在等活的 BLAS 线程 ——
竞态死锁且持有 GIL，整个 uvicorn 事件循环冻结（线上堆栈实锤：
fork → blas_thread_shutdown_ → _pthread_join，主线程 take_gil）。
posix_spawn 不经过 fork/atfork，整类问题根除。

实现要点：

- posix_spawn 不支持 chdir，用 ``/bin/sh -c 'cd "$1" && shift && exec "$@"'``
  包一层，exec 后 shell 被目标程序替换（同 pid，terminate/wait 语义不变）。
- stdout/stderr 用 ``os.pipe`` + file_actions DUP2 接到父进程，
  再 ``connect_read_pipe`` 桥成 StreamReader 供异步逐行读。
- **stdin 一律接到 /dev/null**（两个 provider 共用）：CLI 都是非交互式
  （prompt 走 argv），但 codex 检测到 stdin 是打开的管道时会尝试再读一段
  stdin 当 ``<stdin>`` 块（实测会打印 "Reading additional input from stdin..."）。
  若父进程（uvicorn）的 stdin 是被占住且不关闭的管道，子进程就会一直等输入
  而挂死 —— 这种挂死既不报错也不超时，最难查，直接从源头掐掉。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal

_DEVNULL = "/dev/null"


class SpawnedChild:
    """posix_spawn 起的子进程，适配成 asyncio 子进程的鸭子接口。

    只依赖 ``stdout`` / ``stderr``（StreamReader）、``returncode``、
    ``terminate()``、``wait()`` 这几个成员，便于 provider 层统一消费。
    """

    def __init__(self, argv: list[str], cwd: str) -> None:
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()
        try:
            self._pid = os.posix_spawn(
                "/bin/sh",
                ["/bin/sh", "-c", 'cd "$1" && shift && exec "$@"', "sh", cwd, *argv],
                dict(os.environ),
                file_actions=[
                    (os.POSIX_SPAWN_OPEN, 0, _DEVNULL, os.O_RDONLY, 0o666),
                    (os.POSIX_SPAWN_DUP2, out_w, 1),
                    (os.POSIX_SPAWN_DUP2, err_w, 2),
                ],
            )
        except BaseException:
            for fd in (out_r, out_w, err_r, err_w):
                with contextlib.suppress(OSError):
                    os.close(fd)
            raise
        # 子进程已拿到 dup2 副本，父进程侧写端立即关，否则 EOF 不来
        os.close(out_w)
        os.close(err_w)
        self._out_r, self._err_r = out_r, err_r
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self._waited: int | None = None  # waitpid 已回收后的退出码

    @property
    def pid(self) -> int:
        return self._pid

    async def attach(self) -> None:
        """把两个读端桥进事件循环（必须在事件循环线程内调用）。"""
        loop = asyncio.get_running_loop()
        for reader, fd in ((self.stdout, self._out_r),
                           (self.stderr, self._err_r)):
            protocol = asyncio.StreamReaderProtocol(reader)
            await loop.connect_read_pipe(
                lambda protocol=protocol: protocol,
                os.fdopen(fd, "rb", buffering=0))

    @property
    def returncode(self) -> int | None:
        if self._waited is not None:
            return self._waited
        try:
            pid, status = os.waitpid(self._pid, os.WNOHANG)
        except ChildProcessError:  # 已被别处回收
            return self._waited
        if pid == self._pid:
            self._waited = os.waitstatus_to_exitcode(status)
        return self._waited

    def terminate(self) -> None:
        os.kill(self._pid, signal.SIGTERM)

    async def wait(self) -> int:
        try:
            code = await asyncio.to_thread(os.waitpid, self._pid, 0)
        except ChildProcessError:
            # returncode 属性的 WNOHANG 轮询可能已抢先回收
            return self._waited if self._waited is not None else 0
        self._waited = os.waitstatus_to_exitcode(code[1])
        return self._waited

    async def reap(self, *, grace: float = 5.0) -> None:
        """terminate + waitpid 收干净（半启动的子进程必须收掉）。

        为什么单独暴露：``posix_spawn`` 成功、``attach()`` 才失败时，
        子进程已经带着「全自主权限」在跑了 —— 不回收就留下谁都管不到的孤儿。
        """
        with contextlib.suppress(ProcessLookupError, OSError):
            self.terminate()
        with contextlib.suppress(Exception):  # noqa: BLE001 - 回收失败不改写原始错误
            await asyncio.wait_for(self.wait(), timeout=grace)


def spawn_child(argv: list[str], cwd: str) -> SpawnedChild:
    """在 ``cwd`` 下启动 ``argv``（stdin=/dev/null，stdout/stderr 管道）。"""
    return SpawnedChild(argv, cwd)
