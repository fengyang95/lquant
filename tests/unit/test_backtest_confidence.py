"""统计置信度套件（backtest/confidence.py）：PSR / DSR / E[maxSR] / CSCV-PBO。

「这个 Sharpe 是本事还是运气」的判据函数，测试用确定性种子构造
统计性质明确的场景，不断言精确小数（蒙特卡洛量），断言方向与区间。
"""

from __future__ import annotations

import json

import numpy as np
import pytest
from click.testing import CliRunner

# ---------------- PSR ----------------


def test_psr_zero_drift_is_coin_flip():
    from lquant.backtest.confidence import psr

    r = np.random.default_rng(21).normal(0.0, 0.01, 500)
    p = psr(r)
    assert 0.3 < p < 0.7  # 纯噪声 vs 零基准 ≈ 抛硬币（选统计上典型的 seed）


def test_psr_positive_drift_hugs_one():
    from lquant.backtest.confidence import psr

    r = np.random.default_rng(3).normal(0.001, 0.01, 500)
    assert psr(r) > 0.99


def test_psr_annual_benchmark_daily_units():
    from lquant.backtest.confidence import psr

    # 年化 5% 波动的日基准 ≈ 0.05/sqrt(252)：给日频口径，别被年化数骗
    r = np.random.default_rng(7).normal(0.001, 0.01, 500)
    assert 0.0 < psr(r, sr_benchmark=0.05 / np.sqrt(252)) < 1.0


def test_psr_rejects_short_and_dirty_input():
    from lquant.backtest.confidence import psr

    with pytest.raises(ValueError):
        psr([0.01, 0.02])  # T<3
    with pytest.raises(ValueError):
        psr([0.01, np.nan, 0.02, 0.01])  # NaN 不许静默丢
    with pytest.raises(ValueError):
        psr([0.0, 0.0, 0.0, 0.0])  # 零方差：报 inf 是在装


# ---------------- E[maxSR] / DSR ----------------


def test_expected_max_sharpe_grows_with_trials():
    from lquant.backtest.confidence import expected_max_sharpe

    e10 = expected_max_sharpe(10, sr_var=1e-4)
    e1000 = expected_max_sharpe(1000, sr_var=1e-4)
    assert 0 < e10 < e1000  # 试得越多，最好的噪声越大


def test_expected_max_sharpe_validates_inputs():
    from lquant.backtest.confidence import expected_max_sharpe

    with pytest.raises(ValueError):
        expected_max_sharpe(1, sr_var=1e-4)  # 单次试验没有选择偏差
    with pytest.raises(ValueError):
        expected_max_sharpe(10, sr_var=0.0)
    with pytest.raises(ValueError):
        expected_max_sharpe(10, sr_var=float("nan"))


def test_deflated_sharpe_deflates_with_more_trials():
    from lquant.backtest.confidence import deflated_sharpe

    r = np.random.default_rng(3).normal(0.0008, 0.01, 500)
    few = deflated_sharpe(r, n_trials=5)
    many = deflated_sharpe(r, n_trials=5000)
    assert few["dsr"] > many["dsr"]  # 试过 5000 次后同一观测更可疑
    assert 0.0 <= many["dsr"] <= 1.0
    assert many["expected_max_sharpe_daily"] > 0


def test_deflated_sharpe_var_fallback_is_finite():
    from lquant.backtest.confidence import deflated_sharpe

    r = np.random.default_rng(3).normal(0.0008, 0.01, 500)
    d = deflated_sharpe(r, n_trials=100)  # 不给 sr_trials_var → 解析近似
    assert np.isfinite(d["dsr"]) and np.isfinite(d["expected_max_sharpe_daily"])
    with pytest.raises(ValueError):
        deflated_sharpe(r, n_trials=1)


# ---------------- CSCV-PBO ----------------


def _matrix(
    t: int = 200, n: int = 8, seed: int = 11, signal_cols=None, mu_signal: float = 0.002
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    m = rng.normal(0.0, 0.01, size=(t, n))
    for j in signal_cols if signal_cols is not None else range(n // 2):
        m[:, j] += mu_signal
    return m


def test_cscv_pbo_low_for_real_signal():
    from lquant.backtest.confidence import cscv_pbo

    out = cscv_pbo(_matrix(signal_cols=range(4)), n_partitions=8)
    # 一半配置有恒定正漂移：样本内冠军在样本外大概率还在中位以上
    assert out["pbo"] < 0.4
    assert out["n_combos"] == 70  # C(8,4)
    assert out["t_used"] == 200


def test_cscv_pbo_distinguishes_rank_flip_from_real_signal():
    """确定性对比构造：IS/OOS 排名反转 vs 真信号，PBO 必须方向分明。

    - 反转：各列块均值 = c_j·s_b（s_b 全局交替 ±，c_j 递增）——样本内
      冠军在样本外系统性掉队，手算/实测 PBO = 1.0（每个组合的 λ 都 ≤ 0）；
    - 真信号：各列块均值恒为 c_j —— 冠军恒定，PBO = 0。
    """
    from lquant.backtest.confidence import cscv_pbo

    k, block, n = 8, 10, 8
    c = np.array([0.01 + 0.001 * j for j in range(n)])
    s = np.array([1.0 if b % 2 == 0 else -1.0 for b in range(k)])
    flip = np.repeat(np.outer(s, c), block, axis=0)
    real = np.repeat(np.tile(c, (k, 1)), block, axis=0)
    assert cscv_pbo(flip, n_partitions=k)["pbo"] > 0.9
    assert cscv_pbo(real, n_partitions=k)["pbo"] < 0.1


def test_cscv_pbo_gold_case_is_one():
    """手算金标准：锁死 OOS 口径，错了就红。

    T=8、N=3、k=4（块长 2）：前 6 期全 0，后 2 期为 [0,0,2] →
    block_means = [[0,0,0],[0,0,0],[0,0,0],[0,0,1]]。逐组合手算：
    C(4,2)=6 个组合，无论抽中哪两个块，样本内冠军的样本外排名都 ≤ 中位
    （ω=1，λ=log(1/3)<0），故 PBO = 6/6 = 1.0。当前实现若返回别的值即回归。
    """
    from lquant.backtest.confidence import cscv_pbo

    m = np.zeros((8, 3))
    m[6:, :] = [0.0, 0.0, 2.0]
    out = cscv_pbo(m, n_partitions=4)
    assert out["n_combos"] == 6
    assert out["t_used"] == 8
    assert out["pbo"] == 1.0


def test_cscv_pbo_pure_noise_is_coin_flip():
    """纯噪声下 PBO 应 ≈ 0.5：防 OOS/IS 公式再次被写反（低报或高报）。

    单次 PBO 方差很大，取 30 个种子的均值做区间回归；样本量足够时均值
    应收敛到 0.5 附近。若公式里混入 (1-1/half)·IS 泄漏，均值会系统性跌到
    ~0.14，这条就会红。
    """
    from lquant.backtest.confidence import cscv_pbo

    pbos = [
        cscv_pbo(np.random.default_rng(seed).normal(0, 0.01, size=(160, 6)),
                 n_partitions=16)["pbo"]
        for seed in range(30)
    ]
    mean = sum(pbos) / len(pbos)
    assert 0.4 <= mean <= 0.6, f"纯噪声 PBO 均值 {mean:.3f} 偏离 0.5"


def test_cscv_pbo_rejects_indistinguishable_columns():
    """全常数（各列无差异）矩阵里没有可区分的配置：不许静默返回最坏值 1.0。

    修复前 ``cscv_pbo(np.full((64,4),0.001), n_partitions=8)`` → pbo=1.0
    且无任何告警（argmax 退化成 index 0，所有组合都判过拟合）；与 PSR 对
    零方差收益 raise 的口径对齐。
    """
    from lquant.backtest.confidence import cscv_pbo

    with pytest.raises(ValueError):
        cscv_pbo(np.full((64, 4), 0.001), n_partitions=8)
    # 各列被复制成同一条序列 → 同样不可区分
    col = np.random.default_rng(1).normal(0, 0.01, size=(64, 1))
    with pytest.raises(ValueError):
        cscv_pbo(np.repeat(col, 3, axis=1), n_partitions=8)


def test_cscv_pbo_rejects_absurd_partition_count():
    """C(k, k/2) 有上界校验：k=100 不再卡死在组合枚举上，而是显式 raise。"""
    import lquant.backtest.confidence as conf

    # 非退化矩阵：先过「各列可区分」检查，才能命中组合数上界
    m = np.random.default_rng(1).normal(0, 0.01, size=(100, 3))
    with pytest.raises(ValueError, match="n_partitions"):
        conf.cscv_pbo(m, n_partitions=100)


def test_cscv_pbo_chunking_does_not_change_result(monkeypatch):
    """分块累加与一次性物化同解（内存优化不能改口径）。

    直接把 ``_CSCV_CHUNK_CELLS`` 压到 1 → 每个组合一块（最碎的分块路径），
    与默认（单块）逐字段比对。修复前是 ``block_means[combos]`` 一次性物化
    (C, k/2, N)，k=16/万级配置时 GB 级内存 —— 分块后的结果必须一模一样。
    """
    import lquant.backtest.confidence as conf

    m = _matrix(signal_cols=range(4))
    baseline = conf.cscv_pbo(m, n_partitions=8)
    monkeypatch.setattr(conf, "_CSCV_CHUNK_CELLS", 1)
    chunked = conf.cscv_pbo(m, n_partitions=8)
    assert chunked == baseline


def test_cscv_pbo_validates_shape_and_finiteness():
    from lquant.backtest.confidence import cscv_pbo

    with pytest.raises(ValueError):
        cscv_pbo(np.zeros((3, 4)))  # T<4
    with pytest.raises(ValueError):
        cscv_pbo(np.zeros((100, 1)))  # N<2
    with pytest.raises(ValueError):
        cscv_pbo(np.zeros((100, 2)) * np.nan)
    with pytest.raises(ValueError):
        cscv_pbo(np.zeros(50))  # 一维不是矩阵


# ---------------- 试验台账 count_runs ----------------


@pytest.fixture
def runs_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    import duckdb

    from lquant.data.store.ddl import DDL_STATEMENTS

    (tmp_path / "data" / "duckdb").mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(get_settings().duckdb_path))
    for stmt in DDL_STATEMENTS:
        con.execute(stmt)
    con.close()
    yield
    get_settings.cache_clear()


def test_count_runs_is_the_ledger(runs_env):
    from lquant.backtest import runs

    runs.record_run("s1", {"p": 1}, {"sharpe": 1.0}, status="done")
    runs.record_run("s1", {"p": 2}, {"sharpe": -0.5}, status="abandoned")  # 放弃的也要进台账
    runs.record_run("s2", {"p": 9}, {"sharpe": 0.2}, status="done")
    assert runs.count_runs() == 3
    assert runs.count_runs(strategy="s1") == 2
    assert runs.count_runs(strategy="nope") == 0


# ---------------- CLI ----------------


def test_cli_confidence_psr_only_without_n_trials(runs_env):
    from lquant.cli.commands.backtest import backtest

    r = np.random.default_rng(7).normal(0.001, 0.01, 500)
    # runs_env 已 chdir 到隔离目录，CSV 直接写 cwd
    import pathlib

    p = pathlib.Path("returns.csv")
    p.write_text("ret\n" + "\n".join(str(float(x)) for x in r) + "\n")
    res = CliRunner().invoke(backtest, ["confidence", "--returns", str(p)])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["n_obs"] == 500
    assert 0.0 <= out["psr"] <= 1.0
    assert "dsr" not in out and "n-trials" in out["note"]


def test_cli_confidence_auto_counts_ledger(runs_env):
    from lquant.backtest import runs
    from lquant.cli.commands.backtest import backtest

    runs.record_run("mystrat", {"p": 1}, {"sharpe": 1.0}, status="done")
    runs.record_run("mystrat", {"p": 2}, {"sharpe": 0.3}, status="done")
    r = np.random.default_rng(7).normal(0.0008, 0.01, 500)
    import pathlib

    p = pathlib.Path("returns.csv")
    p.write_text("ret\n" + "\n".join(str(float(x)) for x in r) + "\n")
    res = CliRunner().invoke(
        backtest, ["confidence", "--returns", str(p), "--n-trials", "auto", "--strategy", "mystrat"]
    )
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["n_trials"] == 2
    assert 0.0 <= out["dsr"] <= 1.0
    assert "expected_max_sharpe_annual" in out


def test_cli_confidence_dirty_csv_reports_clean_error(runs_env):
    """CSV 含 NaN/Inf 是用户数据问题，不该吐整段 traceback。

    ``confidence.py`` 的输入校验抛 ``ValueError``；CLI 层必须转成
    ``click.ClickException``（"Error: …"、非 0 退出），与 show/diff 的
    缺 ID 路径风格一致 —— 修复前是裸栈 + exit 1。
    """
    import pathlib

    from lquant.cli.commands.backtest import backtest

    p = pathlib.Path("dirty.csv")
    p.write_text("ret\n0.01\n0.02\nnan\n0.01\n")
    res = CliRunner().invoke(backtest, ["confidence", "--returns", str(p)])
    assert res.exit_code != 0
    assert "Traceback" not in res.output
    assert "Error" in res.output
    assert "NaN/Inf" in res.output


def test_cli_confidence_zero_variance_reports_clean_error(runs_env):
    """零方差收益同样转成干净错误（fail-loudly 但不吐栈）。"""
    import pathlib

    from lquant.cli.commands.backtest import backtest

    p = pathlib.Path("flat.csv")
    p.write_text("ret\n" + "\n".join(["0.0"] * 10) + "\n")
    res = CliRunner().invoke(backtest, ["confidence", "--returns", str(p)])
    assert res.exit_code != 0
    assert "Traceback" not in res.output
    assert "零方差" in res.output
