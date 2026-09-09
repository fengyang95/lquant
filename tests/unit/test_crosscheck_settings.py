"""crosscheck_peers 设置项 + 对拍读运行时配置（Task 6）。

覆盖两点：
  1. SettingsStore.put 对 crosscheck_peers 的 capability 校验：
     只接受已注册且声明 daily / etf_daily 的源，非法 → ValueError（API 层转 422）
  2. run_crosscheck 的配置解析 _cfg()：SettingsStore 覆盖 > providers.yaml
     （peers = settings crosscheck_peers 非空优先；主源 = providers_order 首位）

隔离方式沿用 T3 报告结论：LQ_ROOT env + chdir + cache_clear，
不 patch get_settings 模块属性（会污染懒加载绑定）。
"""
from __future__ import annotations

import pytest

from lquant.core.settings_store import SETTING_DEFS, SettingsStore
from lquant.data.ingest.crosscheck import _cfg


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    """LQ_ROOT + chdir + cache_clear 隔离（不 patch get_settings 模块属性）。

    额外把真实 config/providers.yaml 拷进临时根 —— _cfg 的 yaml 回退
    与 providers_order 的 config 派生都读它，内容即回退断言的依据。
    """
    import shutil

    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    (tmp_path / "config").mkdir()
    shutil.copy2(root / "config" / "providers.yaml", tmp_path / "config" / "providers.yaml")
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ------------------------------------------------------- 设置项定义 + 校验


def test_crosscheck_peers_def_registered():
    d = SETTING_DEFS["crosscheck_peers"]
    assert d.ty == "list"
    assert d.default == ()
    assert "对拍" in d.label


def test_crosscheck_peers_put_accepts_daily_capable_sources(fake_settings):
    out = SettingsStore().put("crosscheck_peers", "tushare,akshare")
    assert out["value"] == ["tushare", "akshare"]
    items = {i["key"]: i["value"] for i in SettingsStore().all()}
    assert items["crosscheck_peers"] == ["tushare", "akshare"]


def test_crosscheck_peers_put_rejects_unknown_source(fake_settings):
    with pytest.raises(ValueError, match="nope"):
        SettingsStore().put("crosscheck_peers", "tushare,nope")


def test_crosscheck_peers_put_rejects_source_without_daily(fake_settings):
    # tencent 只声明 realtime/reference；sina 只声明 adj_factor
    with pytest.raises(ValueError, match="daily"):
        SettingsStore().put("crosscheck_peers", "tencent,sina")


def test_other_list_settings_not_capability_checked(fake_settings):
    # providers_order 不做 capability 校验（保持原语义）
    assert SettingsStore().put("providers_order", "tencent,sina")["value"] == [
        "tencent", "sina"]


# ------------------------------------------------------- 对拍配置解析


def test_run_crosscheck_cfg_defaults_fallback_to_yaml(fake_settings):

    cfg = _cfg()
    # 未配置 crosscheck_peers → 回退 providers.yaml crosscheck.peers
    assert cfg["peers"] == ["akshare"]
    # 主源 = providers_order 首位（config 派生：启用源顺序首位 baostock）
    assert cfg["primary"] == "baostock"
    # tolerance / fields / enabled 仍读 yaml
    assert cfg["tolerance_pct"] == 0.1
    assert "close" in cfg["fields"]
    assert cfg["enabled"] is True


def test_run_crosscheck_cfg_settings_override(fake_settings):
    store = SettingsStore()
    store.put("crosscheck_peers", "tushare")
    store.put("providers_order", "tushare,baostock")

    cfg = _cfg()
    assert cfg["peers"] == ["tushare"]
    assert cfg["primary"] == "tushare"
    assert cfg["tolerance_pct"] == 0.1
