"""源站契约探针（data/contract.py）—— 全部离线，响应样本取自实测。

样本来源（2026-10-08 实测，字段集合原样拷贝）：
- `RPT_MUTUAL_DEAL_HISTORY`：北向成交额，`NET_DEAL_AMT` 自 2024-08-19 起为 null；
- `RPT_DAILYBILLBOARD_DETAILSNEW`：龙虎榜（注意**不返回 TRADE_DATE**，
  日期是调用方传的，契约里不能要求它）；
- `getTopicZTPool`：涨停池，字段是单字母，且**从不返回 h/l**；
- `push2 clist`：个股资金流，`data.diff[0]`。
"""
from __future__ import annotations

import pytest

from lquant.data.contract import (
    CONTRACTS,
    RULE_DRIFT,
    RULE_NEW_FIELD,
    RULE_NO_DATA,
    RULE_PROBE_FAILED,
    RULE_SHAPE,
    Contract,
    check_payload,
    list_contracts,
    probe_all,
    probe_contract,
)

# ---- 实测样本 ----

_HSGT_DEAL = {"success": True, "result": {"data": [{
    "TRADE_DATE": "2026-10-08 00:00:00", "MUTUAL_TYPE": "001",
    "DEAL_AMT": 134535.72, "DEAL_NUM": 6824276,
    "NET_DEAL_AMT": None, "BUY_AMT": None, "SELL_AMT": None,
}]}}

_DRAGON = {"success": True, "result": {"data": [{
    "SECURITY_CODE": "600825", "SECURITY_NAME_ABBR": "新华传媒",
    "CLOSE_PRICE": 6.5, "CHANGE_RATE": 10.05, "BILLBOARD_NET_AMT": 1.2e8,
    "BILLBOARD_BUY_AMT": 2e8, "BILLBOARD_SELL_AMT": 8e7, "EXPLANATION": "日涨幅偏离值达7%",
}]}}

_LIMIT_UP = {"data": {"pool": [{
    "c": "002058", "m": 0, "n": "紫竹高科", "p": 22220, "zdp": 10.0,
    "amount": 104419028, "ltsz": 3185588787.04, "tshare": 3187421937.04,
    "hs": 3.29, "lbc": 3, "fbt": 92500, "lbt": 93042, "fund": 142263550,
    "zbc": 1, "hybk": "电池", "zttj": {"days": 3, "ct": 3},
}], "qdate": 20261008}}

_MONEY_FLOW = {"data": {"total": 5000, "diff": [{
    "f12": "000001", "f14": "平安银行", "f2": 11.5, "f3": 1.2, "f62": 1.2e8,
    "f184": 3.4, "f66": 8e7, "f69": 2.1, "f72": 4e7, "f75": 1.1,
    "f78": 1e7, "f81": 0.3, "f84": -3e7, "f87": -0.8,
}]}}


def _c(name: str) -> Contract:
    return next(c for c in CONTRACTS if c.name == name)


def test_contract_names_unique_and_described():
    names = [c.name for c in CONTRACTS]
    assert len(names) == len(set(names))
    described = list_contracts()
    assert len(described) == len(CONTRACTS)
    assert all(d["required"] for d in described)


@pytest.mark.parametrize("name,payload", [
    ("eastmoney.hsgt_deal_history", _HSGT_DEAL),
    ("eastmoney.dragon_tiger", _DRAGON),
    ("eastmoney.limit_up_pool", _LIMIT_UP),
    ("eastmoney.money_flow_clist", _MONEY_FLOW),
])
def test_real_samples_have_no_drift(name, payload):
    """实测样本必须零告警 —— 否则契约本身写错了（会天天误报）。

    `RPT_MUTUAL_TOP10DEAL` 不在样本里：它按业务日返回，非交易日无行。
    """
    assert check_payload(_c(name), payload) == []


def test_missing_required_field_is_error():
    payload = {"success": True, "result": {"data": [{"TRADE_DATE": "2026-10-08",
                                                     "MUTUAL_TYPE": "001"}]}}
    issues = check_payload(_c("eastmoney.hsgt_deal_history"), payload)
    assert [i.rule for i in issues] == [RULE_DRIFT]
    assert issues[0].severity == "error"
    assert "DEAL_AMT" in issues[0].detail
    # 停发字段在 optional 里，不该被当成缺字段
    assert "NET_DEAL_AMT" not in issues[0].detail


def test_new_field_is_info_not_error():
    """源站加列：留痕但不阻断（往往意味着又有一批字段我们没采）。"""
    payload = {"success": True, "result": {"data": [{
        **_HSGT_DEAL["result"]["data"][0], "BRAND_NEW_COL": 1}]}}
    issues = check_payload(_c("eastmoney.hsgt_deal_history"), payload)
    assert [i.rule for i in issues] == [RULE_NEW_FIELD]
    assert issues[0].severity == "info"
    assert "BRAND_NEW_COL" in issues[0].detail


def test_shape_change_is_error():
    """响应结构从 list 变 dict —— 北向旧实现就是死在这里（KeyError: -1）。"""
    payload = {"success": True, "result": {"data": {"hk2sh": {"status": 3}}}}
    issues = check_payload(_c("eastmoney.hsgt_deal_history"), payload)
    assert [i.rule for i in issues] == [RULE_SHAPE]
    assert issues[0].severity == "error"


def test_empty_ok_contract_reports_info_not_error():
    """涨停池按业务日取数：非交易日取不到记录是正常空态。"""
    issues = check_payload(_c("eastmoney.limit_up_pool"), {"data": {}})
    assert [i.rule for i in issues] == [RULE_NO_DATA]
    assert issues[0].severity == "info"


def test_report_not_found_is_shape_error_without_empty_ok():
    issues = check_payload(_c("eastmoney.hsgt_deal_history"),
                           {"success": True, "result": None})
    assert issues[0].rule == RULE_SHAPE


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def test_probe_contract_uses_date_placeholder_and_ok():
    seen = {}

    def fetch(url):
        seen["url"] = url
        return _Resp(_LIMIT_UP)

    issues = probe_contract(_c("eastmoney.limit_up_pool"), fetch=fetch, today="20261008")
    assert issues == []
    assert "date=20261008" in seen["url"]
    assert "pageSize=1" in seen["url"] or "pagesize=1" in seen["url"].lower()


def test_probe_contract_network_failure_is_warn_only():
    def boom(url):
        raise RuntimeError("proxy 不通")

    issues = probe_contract(_c("eastmoney.money_flow_clist"), fetch=boom)
    assert [i.rule for i in issues] == [RULE_PROBE_FAILED]
    assert issues[0].severity == "warn"


def test_probe_contract_source_declared_failure_is_warn():
    def fetch(url):
        return _Resp({"success": False, "message": "服务器繁忙", "code": 9701})

    issues = probe_contract(_c("eastmoney.hsgt_top10"), fetch=fetch)
    assert issues[0].rule == RULE_PROBE_FAILED


def test_probe_contract_bad_json_is_warn():
    class Bad:
        def json(self):
            raise ValueError("not json")

    issues = probe_contract(_c("eastmoney.hsgt_top10"), fetch=lambda url: Bad())
    assert issues[0].rule == RULE_PROBE_FAILED


def test_probe_all_covers_every_contract():
    calls = []

    def fetch(url):
        calls.append(url)
        return _Resp(_HSGT_DEAL)

    issues = probe_all(fetch=fetch)
    assert len(calls) == len(CONTRACTS)
    # 只有 top10 / limit_up 这类结构不匹配的会报，其它按样本应干净
    rules = {i.rule for i in issues}
    assert rules <= {RULE_SHAPE, RULE_NO_DATA, RULE_DRIFT, RULE_NEW_FIELD}


def test_probe_all_unknown_name_raises():
    with pytest.raises(KeyError, match="未知契约"):
        probe_all(names=["nope.not.here"], fetch=lambda url: _Resp({}))
