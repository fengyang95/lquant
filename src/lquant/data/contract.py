"""源站契约探针：把「字段还在不在」变成一条可检索的质量问题。

**为什么需要它**

北向资金那次事故（PR 修的是它）不是算法问题，而是源站先停发字段、
再改响应结构，而采集器两条路都是静默的：先把停发字段的 `0.0` 当真值写库，
再在结构变化后 KeyError —— 而 `run_all()` 对非 critical 采集器只 `print` 一行
warning，没人看得见。契约探针把这两件事都搬到 `data_quality_issue` 里。

**探针怎么设计**

- **声明式**：每个源站接口写一条 `Contract`（URL + 必含字段 + 取值路径），
  探针请求 `pageSize=1` 拿最少的数据量做字段核对，不落业务数据。
- **纯函数内核**：`check_payload(contract, payload)` 不碰网络，因此可以
  用真实响应样本离线回归（测试里引用的就是实测样本）。
- **缺字段 = error，多字段 = info**：源站加列通常无害，但值得留痕 ——
  它往往意味着「又有一批字段我们没采」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from lquant.data.quality.issues import Issue, save_issues

__all__ = [
    "Contract",
    "CONTRACTS",
    "check_payload",
    "probe_contract",
    "probe_all",
    "run_contract_probes",
    "list_contracts",
    "RULE_DRIFT",
    "RULE_SHAPE",
    "RULE_NEW_FIELD",
    "RULE_PROBE_FAILED",
    "RULE_NO_DATA",
]

RULE_DRIFT = "CONTRACT_DRIFT"          # 必含字段消失（error）
RULE_NEW_FIELD = "CONTRACT_NEW_FIELD"  # 源站多了字段（info）
RULE_PROBE_FAILED = "CONTRACT_PROBE_FAILED"  # 请求/解析失败（warn）
RULE_SHAPE = "CONTRACT_SHAPE"          # 响应结构变了（error，取不到记录）
RULE_NO_DATA = "CONTRACT_NO_DATA"      # 非交易日/未公布，本次无法核对（info）


@dataclass(frozen=True)
class Contract:
    """一条源站接口契约。

    path : 从响应 JSON 根到「一条记录」的路径，元素是 dict 键或 list 下标。
    required : 必须存在的字段名。**只写采集器真正读的字段**，
               不写「顺手也在返回里」的字段，否则契约会天天误报。
    """
    name: str
    source: str
    dataset: str
    url: str
    required: tuple[str, ...]
    path: tuple[str | int, ...] = ("result", "data", 0)
    optional: tuple[str, ...] = ()
    note: str = ""
    empty_ok: bool = False
    """True：该接口按业务日取数，非交易日/尚未公布时「取不到记录」是正常空态，
    此时只记一条 info（未能核对），而不是报结构漂移的假警。"""

    def describe(self) -> dict:
        return {"name": self.name, "source": self.source, "dataset": self.dataset,
                "required": list(self.required), "path": list(map(str, self.path)),
                "empty_ok": self.empty_ok, "note": self.note}


def _ymd_today() -> str:
    from lquant.core.types import today_cn

    return today_cn().strftime("%Y%m%d")


_DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_PUSH2 = "https://push2.eastmoney.com/api/qt/clist/get"
_PUSH2EX = "https://push2ex.eastmoney.com"
_UT = "b2884a393a59ad64002292a3e90d46a5"

# 探针请求一律 pageSize=1：只核对字段，不搬数据。
CONTRACTS: tuple[Contract, ...] = (
    Contract(
        name="eastmoney.hsgt_deal_history",
        source="eastmoney",
        dataset="northbound_flow",
        url=(f"{_DC}?reportName=RPT_MUTUAL_DEAL_HISTORY"
             "&columns=TRADE_DATE,MUTUAL_TYPE,DEAL_AMT,DEAL_NUM,BUY_AMT,SELL_AMT,"
             "NET_DEAL_AMT&pageSize=1&pageNumber=1&sortColumns=TRADE_DATE"
             "&sortTypes=-1&source=WEB&client=WEB"),
        required=("TRADE_DATE", "MUTUAL_TYPE", "DEAL_AMT", "DEAL_NUM"),
        # NET_DEAL_AMT / BUY_AMT / SELL_AMT 明确「已可停发」，不算缺字段
        optional=("NET_DEAL_AMT", "BUY_AMT", "SELL_AMT"),
        note=("北向成交额；NET_DEAL_AMT 自 2024-08-19 起恒 null，故列 optional。"
              "这条契约为 P0-4 事故而建。"),
    ),
    Contract(
        name="eastmoney.hsgt_top10",
        source="eastmoney",
        dataset="northbound_top10",
        url=(f"{_DC}?reportName=RPT_MUTUAL_TOP10DEAL"
             "&columns=TRADE_DATE,MUTUAL_TYPE,SECURITY_CODE,DERIVE_SECURITY_CODE,"
             "SECURITY_NAME,RANK,CLOSE_PRICE,CHANGE_RATE,DEAL_AMT,MUTUAL_RATIO"
             "&pageSize=1&pageNumber=1&sortColumns=RANK&sortTypes=1"
             "&source=WEB&client=WEB"),
        required=("TRADE_DATE", "MUTUAL_TYPE", "SECURITY_CODE", "RANK", "DEAL_AMT",
                  "MUTUAL_RATIO"),
        optional=("DERIVE_SECURITY_CODE", "SECURITY_NAME", "CLOSE_PRICE", "CHANGE_RATE"),
    ),
    Contract(
        name="eastmoney.money_flow_clist",
        source="eastmoney",
        dataset="money_flow",
        url=(f"{_PUSH2}?fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f12,f14,f2,f3,"
             f"f62,f184,f66,f69,f72,f75,f78,f81,f84,f87&pn=1&pz=1&po=1&np=1"
             f"&fltt=2&invt=2&fid=f62&ut={_UT}"),
        required=("f12", "f14", "f2", "f3", "f62", "f184"),
        optional=("f66", "f69", "f72", "f75", "f78", "f81", "f84", "f87"),
        path=("data", "diff", 0),
        note="个股资金流前 N 只；f 编号是东财内部列号，改动即须改采集器。",
    ),
    Contract(
        name="eastmoney.limit_up_pool",
        source="eastmoney",
        dataset="limit_up_pool",
        url=(f"{_PUSH2EX}/getTopicZTPool?ut=7eea3edcaed734bea9cbfc24409ed989"
             f"&dpt=wz.ztzt&Pageindex=0&pagesize=1&sort=fbt%3Aasc&date={{date}}"),
        required=("c", "n", "p", "zdp", "fund", "hs", "fbt", "lbt", "zbc", "hybk",
                  "lbc", "zttj"),
        optional=("amount", "ltsz", "m", "tshare"),
        path=("data", "pool", 0),
        empty_ok=True,
        note=("涨停池字段是单字母，源站改动极难从语义上察觉。"
              "**已知缺口**：采集器 `_limit_type()` 读 h/l 判断一字板，"
              "而该接口从不返回 h/l（2026-10-08 全量 43 行核对）→ 「一字板」"
              "分支实际不可达，留待 P1-2 涨停池语义层修。"),
    ),
    Contract(
        name="eastmoney.dragon_tiger",
        source="eastmoney",
        dataset="dragon_tiger",
        url=(f"{_DC}?reportName=RPT_DAILYBILLBOARD_DETAILSNEW"
             "&columns=SECURITY_CODE,SECURITY_NAME_ABBR,CLOSE_PRICE,CHANGE_RATE,"
             "BILLBOARD_NET_AMT,BILLBOARD_BUY_AMT,BILLBOARD_SELL_AMT,EXPLANATION"
             "&pageSize=1&pageNumber=1&sortColumns=BILLBOARD_NET_AMT&sortTypes=-1"
             "&source=WEB&client=WEB"),
        required=("SECURITY_CODE", "EXPLANATION", "BILLBOARD_NET_AMT",
                  "BILLBOARD_BUY_AMT", "BILLBOARD_SELL_AMT"),
        optional=("SECURITY_NAME_ABBR", "CLOSE_PRICE", "CHANGE_RATE"),
    ),
)

_BY_NAME = {c.name: c for c in CONTRACTS}


def list_contracts() -> list[dict]:
    return [c.describe() for c in CONTRACTS]


def _dig(payload: Any, path: tuple[str | int, ...]) -> Any:
    cur = payload
    for key in path:
        if isinstance(cur, dict):
            if key not in cur:
                return None
            cur = cur[key]
        elif isinstance(cur, list):
            if not isinstance(key, int) or key >= len(cur):
                return None
            cur = cur[key]
        else:
            return None
    return cur


def check_payload(contract: Contract, payload: Any) -> list[Issue]:
    """纯函数内核：给定响应 JSON，判断字段是否齐全（不联网、不落库）。"""
    record = _dig(payload, contract.path)
    if not isinstance(record, dict):
        if contract.empty_ok:
            return [Issue(
                rule=RULE_NO_DATA, severity="info", dataset=contract.dataset,
                detail=(f"{contract.name}：本次取不到记录（非交易日或尚未公布），"
                        f"字段契约未能核对。"),
                extra={"contract": contract.name, "source": contract.source},
            )]
        return [Issue(
            rule=RULE_SHAPE, severity="error", dataset=contract.dataset,
            detail=(f"{contract.name}：响应结构变了，按 path "
                    f"{list(map(str, contract.path))} 取不到记录"
                    f"（拿到 {type(record).__name__}）。采集器会静默拿不到字段。"),
            extra={"contract": contract.name, "source": contract.source},
        )]
    keys = set(record)
    missing = [f for f in contract.required if f not in keys]
    extra = sorted(keys - set(contract.required) - set(contract.optional))
    issues: list[Issue] = []
    if missing:
        issues.append(Issue(
            rule=RULE_DRIFT, severity="error", dataset=contract.dataset,
            detail=(f"{contract.name}：源站响应缺字段 {missing}。"
                    f"这些字段是采集器直接读取的，缺了会静默写空值或直接抛错。"),
            extra={"contract": contract.name, "source": contract.source,
                   "missing": ",".join(missing)},
        ))
    if extra:
        issues.append(Issue(
            rule=RULE_NEW_FIELD, severity="info", dataset=contract.dataset,
            detail=(f"{contract.name}：源站多出字段 {extra}；"
                    f"若其中有值得入库的，需同步 curated schema（只增不改）。"),
            extra={"contract": contract.name, "source": contract.source,
                   "new_fields": ",".join(extra)},
        ))
    return issues


def probe_contract(contract: Contract, *, fetch=None, today: str | None = None) -> list[Issue]:
    """真跑一次探针（网络）。失败不外抛，落成 warn 级 issue —— 探针自身不该炸链路。"""
    if fetch is None:
        from lquant.market.em_client import em_get

        fetch = em_get
    url = contract.url
    if "{date}" in url:
        url = url.replace("{date}", today or _ymd_today())
    try:
        resp = fetch(url)
        payload = resp.json()
    except Exception as e:  # noqa: BLE001  探针请求失败：源站不可达/被限流都算
        return [Issue(
            rule=RULE_PROBE_FAILED, severity="warn", dataset=contract.dataset,
            detail=f"{contract.name}：探针请求失败（{type(e).__name__}: {e}）",
            extra={"contract": contract.name, "source": contract.source},
        )]
    if isinstance(payload, str):     # 极少数情况下源站返回 JSON 字符串
        try:
            payload = json.loads(payload)
        except ValueError:
            return [Issue(
                rule=RULE_SHAPE, severity="error", dataset=contract.dataset,
                detail=f"{contract.name}：响应不是 JSON 对象",
                extra={"contract": contract.name, "source": contract.source},
            )]
    if payload.get("success") is False:
        return [Issue(
            rule=RULE_PROBE_FAILED, severity="warn", dataset=contract.dataset,
            detail=(f"{contract.name}：源站声明失败 "
                    f"{payload.get('message') or payload.get('code')}"),
            extra={"contract": contract.name, "source": contract.source},
        )]
    return check_payload(contract, payload)


def probe_all(*, names: list[str] | None = None, fetch=None,
              today: str | None = None) -> list[Issue]:
    """按契约名探全部（或指定子集）。"""
    targets = CONTRACTS
    if names:
        unknown = [n for n in names if n not in _BY_NAME]
        if unknown:
            raise KeyError(f"未知契约: {unknown}；可用: {sorted(_BY_NAME)}")
        targets = tuple(_BY_NAME[n] for n in names)
    issues: list[Issue] = []
    for c in targets:
        issues.extend(probe_contract(c, fetch=fetch, today=today))
    return issues


def run_contract_probes(*, names: list[str] | None = None, fetch=None,
                        today: str | None = None, save: bool = True) -> dict:
    """探针 + 落 data_quality_issue。返回摘要供 API/CLI 直接展示。"""
    issues = probe_all(names=names, fetch=fetch, today=today)
    saved = save_issues(issues) if save else 0
    return {
        "checked": len(names) if names else len(CONTRACTS),
        "issues": len(issues),
        "saved": saved,
        "drift": [i.to_row(None)["detail"] for i in issues if i.rule == RULE_DRIFT],
        "detail": [
            {"contract": i.extra.get("contract"), "rule": i.rule,
             "severity": i.severity, "message": i.detail}
            for i in issues
        ],
    }
