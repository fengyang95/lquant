"""涨停池 / 炸板池 / 跌停池。

东财的涨停池接口**只提供当日数据**，没有历史回溯。
当天不采，这笔数据就永远消失了 —— 所以它是整个看板里唯一
"失败必须告警"的采集任务，且要在收盘后尽快跑（15:05 左右）。

字段口径（东财 getTopicZTPool，已按 akshare 源码与真实返回对账）：
- p     价格，放大 1000 倍
- zdp   涨跌幅，已是百分数
- amount 成交额（元）—— fund 是**封板资金**（封住涨停所需买单金额），
         两者量级常差 10 倍以上，混用会系统性歪曲「封板强度 vs 热度」
- fund  封板资金（元）→ 本采集器落 seal_amount 列
- fbt/lbt 首次/最后封板时间，HHMMSS 整数（92503 = 09:25:03），不是时间戳
- lbc   连板数
- zbc   炸板次数
- hs    换手率，放大 100 倍

历史数据：修复前 ``fbt``/``lbt`` 被当成 epoch 秒解析，已入库的
``first_limit_time`` / ``last_limit_time`` 是错的（92500 → "01:41:40"）。
这些历史行不会被自动修正，需重跑对应交易日的涨停/炸板池采集才会覆盖；
东财涨停池只有当日数据，早于修复日的错误值无法回补，只能作废。
"""
from __future__ import annotations

from datetime import date, datetime

import polars as pl

from lquant.core.errors import DataUnavailable
from lquant.core.types import now_cn, today_cn
from lquant.market.em_client import em_get

__all__ = ["fetch_limit_up_pool", "fetch_limit_down_pool", "fetch_broken_pool"]

_UT = "7eea3edcaed734bea9cbfc24409ed989"
_BASE = "https://push2ex.eastmoney.com"


def _ymd(d: date | str | None) -> str:
    # 缺省交易日用 today_cn()：东财涨跌停池按「业务日」取数，
    # 服务器时区非 Asia/Shanghai 时 date.today() 会错位一天
    if d is None:
        return today_cn().strftime("%Y%m%d")
    if isinstance(d, str):
        return d.replace("-", "")
    return d.strftime("%Y%m%d")


def _parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y%m%d").date()


def _fetch_pool(kind: str, trade_date: date | str | None = None,
                pagesize: int = 500) -> list[dict]:
    """kind: ZT(涨停) / ZB(炸板) / DT(跌停)。

    翻页取全：接口默认单页，2015 年级别的大行情单日涨停家数超过 1000，
    单页固定 pagesize 会静默截断 —— 涨停家数/炸板率等情绪统计直接失真。
    """
    all_rows: list[dict] = []
    page = 0
    while True:
        url = (f"{_BASE}/getTopic{kind}Pool?ut={_UT}&dpt=wz.ztzt"
               f"&Pageindex={page}&pagesize={pagesize}&sort=fbt%3Aasc"
               f"&date={_ymd(trade_date)}")
        resp = em_get(url)
        try:
            payload = resp.json()
        except Exception as e:  # noqa: BLE001
            raise DataUnavailable("eastmoney", f"涨停池响应非 JSON: {e}") from e
        if not payload or not payload.get("data"):
            break
        data = payload["data"]
        pool = data.get("pool") or []
        all_rows.extend(pool)
        # 终止：本页不满（最后一页）/ 已取满接口给的 total（tc）/ 兜底页数上限
        if len(pool) < pagesize:
            break
        tc = data.get("tc")
        if tc is not None and len(all_rows) >= int(tc):
            break
        page += 1
        if page >= 20:                 # 20 页 = 1 万条，纯防御
            break
    return all_rows


def _hhmmss_parts(v) -> tuple[int, int, int] | None:
    """HHMMSS 整数/数字字符串 → (时, 分, 秒)；缺失或非法 → None。

    只接受整数、整数值 float 与纯数字字符串（1..6 位，按 HHMMSS 字典序补零）；
    其余（None、""、非整数、0 哨兵、越界如 99999/240000、时分秒越界）一律返回
    None —— 绝不抛裸异常，也绝不把非法值"修"成一个看起来合法的假时间。
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        n = v
    elif isinstance(v, float):
        if not v.is_integer():
            return None
        n = int(v)
    else:
        try:
            n = int(str(v).strip())
        except (TypeError, ValueError):
            return None
    if not 0 <= n <= 235959:
        return None
    if n == 0:
        return None  # 0 是「无封板时间」哨兵，不是 00:00:00
    hh, mm, ss = n // 10000, (n // 100) % 100, n % 100
    if hh > 23 or mm > 59 or ss > 59:
        return None
    return hh, mm, ss


def _hhmmss_to_time(v) -> str | None:
    """东财 fbt/lbt（首次/最后封板时间）→ "HH:MM:SS"；非法/缺失 → None。

    上游契约是 **HHMMSS 整数**（92500 = 09:25:00），不是 epoch 秒 —— 旧实现
    ``datetime.fromtimestamp`` 把 92500 落成 "01:41:40" 之类的假时间。
    非法值显式落 NULL，不用伪造的合法时间顶替。

    历史数据：修复前已入库的 ``first_limit_time`` / ``last_limit_time`` 是错的，
    本函数不会回改旧行；需重跑对应交易日的涨停/炸板池采集才会被覆盖（东财涨停池
    只有当日数据，早于修复日的错误历史值无法回补，只能作废）。
    """
    parts = _hhmmss_parts(v)
    if parts is None:
        return None
    hh, mm, ss = parts
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def _norm_symbol(code: str) -> str:
    from lquant.core.types import parse_symbol
    try:
        return str(parse_symbol(str(code)))
    except ValueError:
        return str(code)


def fetch_limit_up_pool(trade_date: date | str | None = None, *,
                        demo: bool = False) -> pl.DataFrame:
    """涨停池。demo=True 时生成合成数据用于离线链路验证。"""
    d = _parse_ymd(_ymd(trade_date))
    if demo:
        return _demo_pool(d, "up")

    rows = []
    for it in _fetch_pool("ZT", trade_date):
        rows.append({
            "trade_date": d,
            "symbol": _norm_symbol(it.get("c")),
            "name": it.get("n"),
            "close": float(it.get("p", 0)) / 1000.0,
            "change_pct": float(it.get("zdp", 0)),
            "amount": float(it.get("amount", 0) or 0),
            "seal_amount": float(it.get("fund", 0) or 0),
            "turnover_rate": float(it.get("hs", 0) or 0) / 100.0,
            "first_limit_time": _hhmmss_to_time(it.get("fbt")),
            "last_limit_time": _hhmmss_to_time(it.get("lbt")),
            "open_count": int(it.get("zbc", 0) or 0),
            "limit_up_type": _limit_type(it),
            "industry": it.get("hybk"),
            "collected_at": now_cn(),
        })
    cols = ["trade_date", "symbol", "name", "close", "change_pct", "amount",
            "seal_amount", "turnover_rate", "first_limit_time", "last_limit_time",
            "open_count", "limit_up_type", "industry", "collected_at"]
    return pl.DataFrame(rows, schema={c: None for c in cols}, orient="row") if not rows \
        else pl.DataFrame(rows)


# 集合竞价 09:25 结束、连续竞价 09:30 开始（HHMMSS）。fbt 落在 [09:25, 09:30)
# 即「集合竞价/开盘即封板」，这是区分一字/T字/换手唯一可得的业务信号。
_OPEN_SEAL_LO = 9 * 3600 + 25 * 60  # 09:25:00
_OPEN_SEAL_HI = 9 * 3600 + 30 * 60  # 09:30:00


def _limit_type(it: dict) -> str:
    """一字板 / T 字板 / 换手板 —— 三者含义完全不同。

    涨停池接口**没有** h/l（实测 keys: amount,c,fbt,fund,hs,hybk,lbc,lbt,ltsz,m,n,p,
    tshare,zbc,zdp,zttj），旧实现拿 ``it["h"]/it["l"]`` 判「最高=最低」，恒得
    hi=lo=0 → 一字板分支永不可达，板型一律回落成换手板/T字板。

    可判定板型的字段是 fbt（首次封板时间，上游契约是 HHMMSS 整数）：
    - fbt ∈ [09:25, 09:30) → 集合竞价即封板；此时 zbc==0 是全天未开板（一字板），
      zbc>0 是开过板又回封（T字板）；
    - fbt ≥ 09:30 → 盘中拉升才封板（换手板）。

    fbt 缺失/不可解析时返回「未知板型」：宁可显式未知，也不回落成一个确定但错误
    （且 board.py 会按非 NULL 当真）的板型。
    """
    zbc = int(it.get("zbc", 0) or 0)
    parts = _hhmmss_parts(it.get("fbt"))
    if parts is None:
        return "未知板型"
    tod = parts[0] * 3600 + parts[1] * 60 + parts[2]
    if _OPEN_SEAL_LO <= tod < _OPEN_SEAL_HI:
        return "T字板" if zbc > 0 else "一字板"
    if tod >= _OPEN_SEAL_HI:
        return "换手板"
    return "未知板型"


def fetch_broken_pool(trade_date: date | str | None = None, *,
                      demo: bool = False) -> pl.DataFrame:
    """炸板池：曾涨停但收盘未封住。炸板率是情绪最重要的反向指标。"""
    d = _parse_ymd(_ymd(trade_date))
    if demo:
        return _demo_pool(d, "broken")
    rows = [{"trade_date": d, "symbol": _norm_symbol(it.get("c")), "name": it.get("n"),
             "close": float(it.get("p", 0)) / 1000.0,
             "change_pct": float(it.get("zdp", 0)),
             "amount": float(it.get("amount", 0) or 0),
             "seal_amount": float(it.get("fund", 0) or 0),
             "first_limit_time": _hhmmss_to_time(it.get("fbt")),
             "open_count": int(it.get("zbc", 0) or 0),
             "industry": it.get("hybk"),
             "collected_at": now_cn()}
            for it in _fetch_pool("ZB", trade_date)]
    return pl.DataFrame(rows) if rows else pl.DataFrame(
        schema={"trade_date": pl.Date, "symbol": pl.Utf8, "name": pl.Utf8,
                "close": pl.Float64, "change_pct": pl.Float64, "amount": pl.Float64,
                "seal_amount": pl.Float64,
                "first_limit_time": pl.Utf8, "open_count": pl.Int64,
                "industry": pl.Utf8, "collected_at": pl.Datetime("us")})


def fetch_limit_down_pool(trade_date: date | str | None = None, *,
                          demo: bool = False) -> pl.DataFrame:
    """跌停池。与涨停池的数量比，是判断市场极值情绪的快捷指标。"""
    d = _parse_ymd(_ymd(trade_date))
    if demo:
        return _demo_pool(d, "down")
    rows = [{"trade_date": d, "symbol": _norm_symbol(it.get("c")), "name": it.get("n"),
             "close": float(it.get("p", 0)) / 1000.0,
             "change_pct": float(it.get("zdp", 0)),
             "amount": float(it.get("amount", 0) or 0),
             "seal_amount": float(it.get("fund", 0) or 0),
             "industry": it.get("hybk"), "collected_at": now_cn()}
            for it in _fetch_pool("DT", trade_date)]
    return pl.DataFrame(rows) if rows else pl.DataFrame(
        schema={"trade_date": pl.Date, "symbol": pl.Utf8, "name": pl.Utf8,
                "close": pl.Float64, "change_pct": pl.Float64, "amount": pl.Float64,
                "seal_amount": pl.Float64,
                "industry": pl.Utf8, "collected_at": pl.Datetime("us")})


# 各池的 (SH 段, SZ 段) 基址，段之间互不相交：demo 下 up/broken 若共用同一套
# symbol 生成式，同一只票会同时出现在「涨停」和「炸板」两个池里，与真实实盘
# 「涨停池 ∩ 炸板池 = 空」矛盾 —— board.py 的 zt_streak 会据此在 demo 上得到假结论。
_DEMO_SYM_BASE: dict[str, tuple[int, int]] = {
    "up": (600000, 300000),
    "broken": (601000, 301000),
    "down": (603000, 2000),
}


def _demo_pool(d: date, kind: str) -> pl.DataFrame:
    """合成数据：格式与真实采集一致，用于离线验证看板链路。

    symbol 段按 kind 分开（见 ``_DEMO_SYM_BASE``）：真实实盘涨停池与炸板池的
    交集恒为 0，demo 必须复现这条事实，否则一只票会同时落进两个池。
    """
    import random

    random.seed(hash((d, kind)) % 10000)
    n = {"up": 42, "broken": 15, "down": 6}[kind]
    sh_base, sz_base = _DEMO_SYM_BASE[kind]
    rows = []
    for i in range(n):
        rows.append({
            "trade_date": d,
            "symbol": f"{sh_base + i * 7:06d}.SH" if i % 2 else f"{sz_base + i * 11:06d}.SZ",
            "name": f"样例{i:03d}",
            "close": round(random.uniform(5, 60), 2),
            "change_pct": 10.0 if kind == "up" else (-10.0 if kind == "down"
                                                     else round(random.uniform(2, 9), 2)),
            "amount": round(random.uniform(1e7, 3e9), 2),
            "seal_amount": round(random.uniform(5e6, 5e8), 2),
            "turnover_rate": round(random.uniform(0.5, 25), 2),
            "first_limit_time": f"{random.randint(9, 14):02d}:{random.randint(0, 59):02d}:00",
            "last_limit_time": "15:00:00",
            "open_count": random.choice([0, 0, 0, 1, 2, 3]),
            "limit_up_type": random.choice(["一字板", "换手板", "T字板"]),
            "industry": random.choice(["半导体", "电力设备", "医药生物", "券商", "食品饮料"]),
            "collected_at": now_cn(),
        })
    df = pl.DataFrame(rows)
    if kind == "down":
        return df.drop(["turnover_rate", "limit_up_type"])
    if kind == "broken":
        # 与真实 fetch_broken_pool 契约一致：炸板池**没有** limit_up_type（入库为
        # NULL）。board.py 的 zt_streak 正是按 ``limit_up_type IS NOT NULL`` 区分
        # 涨停/炸板 —— demo 若生成该列，离线验证「炸板不算涨停」会得出与线上相反
        # 的假结论（实测修复前该列 15/15 非空）。
        return df.drop(["limit_up_type"])
    return df
