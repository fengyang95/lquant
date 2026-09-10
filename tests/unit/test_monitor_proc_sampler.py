"""proc_sampler：样本构造、psutil 缺失降级、Redis SETEX 轮询。"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

from lquant.monitor import proc_sampler as ps


def test_sample_once_with_psutil():
    fake_psutil = MagicMock()
    fake_psutil.Process.return_value.cpu_percent.return_value = 12.5
    fake_psutil.Process.return_value.memory_info.return_value.rss = 64 * 1024 * 1024
    with patch.object(ps, "_psutil", fake_psutil):
        s = ps.sample_once("http")
    assert s.proc_name == "http"
    assert s.pid > 0
    assert s.cpu_pct == 12.5
    assert s.mem_rss_mb == 64.0
    assert s.current_job is None


def test_sample_once_without_psutil():
    with patch.object(ps, "_psutil", None):
        s = ps.sample_once("general-0", current_job_fn=lambda: "job-abc")
    assert s.cpu_pct is None
    assert s.mem_rss_mb is None
    assert s.current_job == "job-abc"


def test_build_payload_roundtrip():
    from lquant.monitor.types import ProcSample

    s = ProcSample(ts=time.time(), proc_name="backtest-1", pid=123,
                   cpu_pct=5.0, mem_rss_mb=128.0, current_job="job-x")
    payload = ps.build_payload(s)
    assert json.loads(json.dumps(payload))["proc_name"] == "backtest-1"


def test_push_once_setex():
    fake = MagicMock()
    s = ps.sample_once("http")
    ps._push(fake, s, ttl=15)
    args = fake.setex.call_args.args
    assert args[0] == "lquant:monitor:proc:http"
    assert args[1] == 15
    assert json.loads(args[2])["pid"] == s.pid


def test_push_error_swallowed():
    fake = MagicMock()
    fake.setex.side_effect = RuntimeError("down")
    ps._push(fake, ps.sample_once("http"), ttl=15)  # 不应抛出


def test_start_sampler_thread(monkeypatch):
    import time as time_mod

    fake = MagicMock()
    monkeypatch.setattr(ps, "_psutil", None)
    monkeypatch.setattr(ps, "_get_redis", lambda: fake)
    monkeypatch.setattr(ps, "SAMPLE_TTL_SEC", 15)
    t = ps.start_sampler("http", interval=0.02)
    assert t is not None and t.daemon
    time_mod.sleep(0.1)
    ps.stop_sampler()
    assert fake.setex.call_count >= 1
    # 停止后不再新增
    n = fake.setex.call_count
    time_mod.sleep(0.05)
    assert fake.setex.call_count == n
