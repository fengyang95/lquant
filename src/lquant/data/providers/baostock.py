"""BaoStock 适配器（主源）。

免费、免注册、无速率限制、支持 ETF 日线/60分钟、财务带 stat_date + pub_date。
坑：批量连续请求会静默挂起 —— 所有网络调用必须经 watchdog 包一层。

实现约束（重要）：
- macOS 上 multiprocessing 默认 spawn，bound method / closure 都不可靠。
  所以所有"真正发请求"的函数都写成**模块级函数**，只吃基本类型参数，
  由 run_with_watchdog 在子进程里调用。
- 每个子进程自己 login/logout，绝不复用父进程的 socket（复用了必挂）。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import polars as pl

from lquant.core.errors import DataQualityError
from lquant.core.types import SecType, parse_symbol, today_cn
from lquant.data.capability import Capability
from lquant.data.normalize import normalize_60min_bounds, normalize_symbols
from lquant.data.providers import PROVIDERS
from lquant.data.providers._engine import MappingProvider

# baostock 代码格式：sh.600000 / sz.000001
_FREQ_MAP = {
    "5min": "5", "15min": "15", "30min": "30", "60min": "60",
    "1d": "d", "1w": "w", "1m": "m",
}
# query_stock_basic 的 type: 1=股票 2=指数 3=其它 4=ETF 5=LOF
_BS_TYPE_TO_SEC = {"1": "stock", "2": "index", "3": "other", "4": "etf", "5": "lof"}

# baostock query_trade_dates 对 >10 年区间静默挂起（watchdog 只能杀，拿不到数据）
_CAL_CHUNK_YEARS = 5


def _add_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:  # 2/29 → 平年
        return d.replace(year=d.year + n, day=28)


def _cal_chunks(start: date, end: date,
                years: int = _CAL_CHUNK_YEARS) -> list[tuple[date, date]]:
    """把 [start, end] 切成首尾相接的 ≤years 年区间。"""
    chunks: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        nxt = _add_years(cur, years)
        if nxt > end:
            chunks.append((cur, end))
            break
        chunks.append((cur, nxt - timedelta(days=1)))
        cur = nxt
    return chunks

# T+0 品种关键词：跨境(QDII)、债券、黄金、货币、商品 —— 名称命中即 T+0 可卖。
# 这是启发式；权威值以 rules/cn_a_share.yaml 的 sellable_after_days 覆盖为准。
_T0_KEYWORDS = (
    "纳指", "纳斯达克", "标普", "恒生", "港股", "中概", "日经", "德国", "法国",
    "东南亚", "沙特", "美国", "海外", "QDII", "国际", "亚太", "日本", "越南", "印度",
    "债", "国债", "政金", "城投", "短融", "可转债", "信用",
    "黄金", "商品", "豆粕", "有色", "能源", "原油",
    "货币", "现金", "保证金",
)

# 日线 17 字段：新增 pctChg + 估值四列（peTTM/pbMRQ/psTTM/pcfNcfTTM）
_DAILY_FIELDS = ("date,code,open,high,low,close,preclose,volume,amount,"
                 "turn,tradestatus,isST,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM")
_MINUTE_FIELDS = "date,time,code,open,high,low,close,volume,amount,adjustflag"


# --------------------------------------------------------------------------
# 模块级请求函数：只吃基本类型，保证 spawn 子进程可 pickle
# --------------------------------------------------------------------------
def _bs_login():  # pragma: no cover - 需要网络
    import baostock as bs

    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock login failed: {lg.error_msg}")
    return bs


def _bs_query(code: str, fields: str, start: str, end: str, freq: str,
              adjustflag: str = "3") -> list[list[str]]:  # pragma: no cover
    """K 线查询。adjustflag: 1=后复权 2=前复权 3=不复权。"""
    bs = _bs_login()
    try:
        f = _FREQ_MAP.get(freq)
        if f is None:
            raise ValueError(f"baostock 不支持 {freq}")
        rs = bs.query_history_k_data_plus(
            code, fields, start_date=start, end_date=end,
            frequency=f, adjustflag=adjustflag,
        )
        if rs.error_code != "0":
            raise RuntimeError(f"baostock {rs.error_code}: {rs.error_msg}")
        rows: list[list[str]] = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        return rows
    finally:
        bs.logout()


def _bs_all_stock(day: str) -> list[list[str]]:  # pragma: no cover
    """当日全部标的：code, tradeStatus, code_name。"""
    bs = _bs_login()
    try:
        rs = bs.query_all_stock(day=day)
        if rs.error_code != "0":
            raise RuntimeError(f"baostock all_stock {rs.error_code}: {rs.error_msg}")
        rows: list[list[str]] = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        return rows
    finally:
        bs.logout()


def _bs_basic(code: str) -> list[list[str]]:  # pragma: no cover
    """code, code_name, ipoDate, outDate, type, status。"""
    bs = _bs_login()
    try:
        rs = bs.query_stock_basic(code=code)
        if rs.error_code != "0":
            raise RuntimeError(f"baostock basic {rs.error_code}: {rs.error_msg}")
        rows: list[list[str]] = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        return rows
    finally:
        bs.logout()


def _bs_trade_dates(start: str, end: str) -> list[list[str]]:  # pragma: no cover
    """calendar_date, is_trading_day。"""
    bs = _bs_login()
    try:
        rs = bs.query_trade_dates(start_date=start, end_date=end)
        if rs.error_code != "0":
            raise RuntimeError(f"baostock calendar {rs.error_code}: {rs.error_msg}")
        rows: list[list[str]] = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        return rows
    finally:
        bs.logout()


def _bs_report(kind: str, code: str, year: int, quarter: int) -> list[list[str]]:  # pragma: no cover
    """季频报表，首行为字段名。kind: profit | balance | cashflow | dupont。"""
    bs = _bs_login()
    try:
        fn = {
            "profit": bs.query_profit_data,
            "balance": bs.query_balance_data,
            "cashflow": bs.query_cash_flow_data,
            "dupont": bs.query_dupont_data,
        }[kind]
        rs = fn(code=code, year=year, quarter=quarter)
        if rs.error_code != "0":
            raise RuntimeError(f"baostock {kind} {rs.error_code}: {rs.error_msg}")
        head = list(rs.fields)
        rows: list[list[str]] = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        return [head, *rows]
    finally:
        bs.logout()


def _bs_code(symbol: str) -> str:
    code, ex = symbol.split(".")
    return f"{ex.lower()}.{code}"


def _sec_type_of(s: str) -> SecType:
    try:
        return parse_symbol(s).sec_type
    except Exception:  # noqa: BLE001
        return SecType.STOCK


def _to_date(s: str) -> date | None:
    if not s or s in ("", "0", "None"):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _quarters(start: date, end: date):
    for y in range(start.year, end.year + 1):
        for q in (1, 2, 3, 4):
            yield y, q


def _year_slices(start: date, end: date):
    """分钟线按年切片，避免单次请求过大触发静默挂起。"""
    out = []
    y = start.year
    while y <= end.year:
        a = date(y, 1, 1) if y > start.year else start
        b = date(y, 12, 31) if y < end.year else end
        out.append((a, b))
        y += 1
    return out


_DAILY_RAW_SCHEMA = ["date", "code", "open", "high", "low", "close", "preclose",
                     "volume", "amount", "turn", "tradestatus", "isST",
                     "pctChg", "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM"]


def _map_daily_raw(rows: list[list[str]]) -> pl.DataFrame:
    """把 baostock 日线原始行转成**源列名** df（模块级，spawn 可 pickle）。

    只做 frames→DataFrame→cast：日期解析、量价 cast、turn=0/空串 → null
    （防 float_mv 除法产 inf）。列名对齐 schema 交给 mapping 引擎；
    停牌行**保留**：tradestatus=="0" 的取反语义映射白名单表达不了，
    在这里就地转成 is_suspended 布尔列（schema 同名，映射层透传）。
    """
    df = pl.DataFrame(rows, schema=_DAILY_RAW_SCHEMA, orient="row")
    return df.with_columns(
        pl.col("date").str.to_date("%Y-%m-%d"),
        pl.col(["open", "high", "low", "close", "preclose", "volume",
                "amount"]).cast(pl.Float64),
        # turn 兜底：cast 后为 0（含 "0"/"0.0000"）→ null，防 float_mv 除法产 inf
        # （float_mv derive 在 config/schema/daily_bar.yaml：turn 为 null 时除法结果自然为 null）
        pl.when(pl.col("turn").cast(pl.Float64, strict=False) == 0)
          .then(pl.lit(None, dtype=pl.Float64))
          .otherwise(pl.col("turn").cast(pl.Float64, strict=False))
          .alias("turn"),
        pl.col(["pctChg", "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM"])
          .cast(pl.Float64, strict=False),   # 空串 → null
        (pl.col("tradestatus").cast(pl.Utf8).str.strip_chars() == "0")
          .alias("is_suspended"),            # "0"=停牌 → True；行保留不丢
        (pl.col("isST").cast(pl.Utf8).str.strip_chars() == "1")
          .alias("is_st"),                   # "1"=ST → True
    ).drop("tradestatus", "isST")


def _attach_is_st(out: pl.DataFrame, raw: pl.DataFrame) -> pl.DataFrame:
    """把 fetch 侧的 is_st 列挂回引擎输出（映射层已产出非空 is_st 时跳过）。

    纯函数、无实例状态：长度不匹配 fail-fast；raw 无 is_st 时输出补全 null 列，
    保持旧 daily_bars 输出恒有该列。
    """
    if "is_st" in out.columns and out["is_st"].is_not_null().any():
        return out   # 映射层已产出 is_st（rename isST），不覆写
    if "is_st" not in out.columns:
        out = out.with_columns(pl.lit(None, dtype=pl.Boolean).alias("is_st"))
    if raw.is_empty() or "is_st" not in raw.columns:
        return out
    if len(raw) != len(out):
        raise DataQualityError(
            "is_st_attach",
            f"is_st 行数与映射输出不一致: raw={len(raw)} out={len(out)}",
        )
    return out.with_columns(
        raw["is_st"].cast(pl.Utf8).str.to_lowercase().str.strip_chars()
        .is_in(["1", "true"]).alias("is_st")
    )


def _guess_sellable_days(name: str, track_index: str | None = None) -> int:
    """推断 T+N：股票 ETF = T+1，跨境/债/金/货币 ETF = T+0。"""
    text = f"{name}{track_index or ''}"
    if any(k in text for k in _T0_KEYWORDS):
        return 0
    return 1


def _guess_track_index(name: str) -> str | None:
    """从 ETF 简称剥出跟踪指数名：'沪深300ETF' -> '沪深300'。"""
    if not name:
        return None
    n = name.replace(" ", "")
    for suf in ("ETF基金", "ETF", "指数基金", "LOF"):
        if n.endswith(suf) and len(n) > len(suf):
            return n[: -len(suf)]
    return None


@PROVIDERS.register("baostock", {"free": True, "need_token": False, "watchdog": True})
class BaoStockProvider(MappingProvider):
    name = "baostock"
    source = "baostock"
    capability = frozenset({
        Capability.DAILY, Capability.MINUTE_5, Capability.MINUTE_15,
        Capability.MINUTE_30, Capability.MINUTE_60, Capability.ETF_DAILY,
        Capability.ETF_MINUTE_60, Capability.INDEX_DAILY, Capability.FINANCIAL_PIT,
        Capability.CALENDAR, Capability.REFERENCE,
        Capability.ADJ_FACTOR, Capability.ETF_META,
    })

    def __init__(self, qps: float = 0, capability: frozenset[Capability] | None = None) -> None:
        self.capability = capability or self.capability

    # ------------------------------------------------- MappingProvider 引擎
    def _fetch_raw(self, table: str, **params: Any) -> pl.DataFrame:
        if table == "daily_bar":
            return self._fetch_daily(**params)
        if table == "minute_bar":
            return self._fetch_minute(**params)
        raise NotImplementedError(f"baostock 不支持表 {table!r}")

    def _fetch_daily(
        self, symbols: list[str], start: date, end: date
    ) -> pl.DataFrame:
        """watchdog 拉日线：产出**源列名** df，映射交给 engine。

        停牌行保留（is_suspended 由映射层得出），行级转换收敛到 _map_daily_raw。
        """
        from lquant.data.watchdog import run_with_watchdog

        frames = []
        for sym in symbols:
            # 单只也要走看门狗：静默挂起是逐请求发生的
            rows = run_with_watchdog(
                _bs_query, _bs_code(sym), _DAILY_FIELDS,
                start.isoformat(), end.isoformat(), "1d", "3",
            )
            if rows:
                frames.append(_map_daily_raw(rows))
        if not frames:
            return pl.DataFrame()
        # 停牌日 volume=0 —— 行保留，is_suspended 供门禁豁免/回测拒单
        return pl.concat(frames, how="diagonal")

    def _fetch_minute(
        self, symbols: list[str], start: date, end: date, freq: str = "60min"
    ) -> pl.DataFrame:
        """watchdog 拉分钟线：源列名 df，time → ts 解析留在 fetch 侧。"""
        from lquant.data.watchdog import run_with_watchdog

        if freq not in ("5min", "15min", "30min", "60min"):
            raise ValueError(f"baostock 分钟线不支持 {freq}")
        frames = []
        for sym in symbols:
            for y0, y1 in _year_slices(start, end):
                rows = run_with_watchdog(
                    _bs_query, _bs_code(sym), _MINUTE_FIELDS,
                    y0.isoformat(), y1.isoformat(), freq, "3",
                )
                if not rows:
                    continue
                frames.append(pl.DataFrame(
                    rows,
                    schema=["date", "time", "code", "open", "high", "low",
                            "close", "volume", "amount", "adjustflag"],
                    orient="row",
                ))
        if not frames:
            return pl.DataFrame()
        # time 形如 20220930103500000（含毫秒），取前 14 位拼时间戳
        return pl.concat(frames, how="diagonal").with_columns(
            pl.col(["open", "high", "low", "close", "volume", "amount"]).cast(pl.Float64),
            ts=pl.col("time").cast(pl.Utf8).str.slice(0, 14).str.to_datetime("%Y%m%d%H%M%S"),
        ).drop(["time", "date"])

    def daily_bars(
        self, symbols: list[str], start: date, end: date
    ) -> pl.DataFrame:
        raw = self._fetch_daily(symbols, start, end)
        out = self.request("daily_bar", _raw=raw)
        return _attach_is_st(out, raw)

    def minute_bars(
        self, symbols: list[str], start: date, end: date, freq: str = "60min"
    ) -> pl.DataFrame:
        """分钟线。BaoStock 单次返回有上限，长区间按年切片（_fetch_minute）。"""
        return self.request(
            "minute_bar", symbols=symbols, start=start, end=end, freq=freq
        )

    def _post_normalize(self, df: pl.DataFrame, table: str) -> pl.DataFrame:
        """引擎归一化后的 provider 侧钩子：符号归一、60min 边界、ingested_at。"""
        df = normalize_symbols(df)
        if table == "minute_bar":
            df = normalize_60min_bounds(df)
            df = df.with_columns(
                ingested_at=pl.lit(datetime.now(), dtype=pl.Datetime),
            )
        return df


    # ---------------------------------------------------------------- 复权
    def adj_factors(self, symbols: list[str], start: date, end: date) -> pl.DataFrame:
        """后复权因子 = 后复权收盘 / 不复权收盘。

        BaoStock 不直接给因子，只能两次拉取相除；前复权在读取时用最新因子归一。
        """
        from lquant.data.watchdog import run_with_watchdog

        out = []
        fields = "date,code,close"
        for sym in symbols:
            raw = run_with_watchdog(_bs_query, _bs_code(sym), fields,
                                    start.isoformat(), end.isoformat(), "1d", "3")
            hfq = run_with_watchdog(_bs_query, _bs_code(sym), fields,
                                    start.isoformat(), end.isoformat(), "1d", "1")
            if not raw or not hfq or len(raw) != len(hfq):
                continue
            out.append(pl.DataFrame(
                [[r[0], r[1], float(r[2]), float(h[2])] for r, h in zip(raw, hfq, strict=False)],
                schema=["trade_date", "symbol", "close", "hfq_close"], orient="row",
            ))
        if not out:
            return pl.DataFrame()
        df = pl.concat(out).with_columns(
            pl.col("trade_date").str.to_date("%Y-%m-%d"),
            factor=pl.when(pl.col("close") > 0)
                   .then(pl.col("hfq_close") / pl.col("close"))
                   .otherwise(1.0),
        )
        return normalize_symbols(df).select(
            "symbol", "trade_date", "factor", pl.lit("baostock").alias("source")
        )

    # ------------------------------------------------------------ 参考数据
    def securities(self, day: date | None = None) -> pl.DataFrame:
        """当日全市场标的清单（快路径）。

        只有 code/name/status，**不含 list_date** —— 上市/退市日期靠
        security_details() 逐只补（慢，必须配合 checkpoint 增量跑）。
        """
        from lquant.data.watchdog import run_with_watchdog

        day = day or today_cn()
        rows = run_with_watchdog(_bs_all_stock, day.isoformat())
        if not rows:
            return pl.DataFrame()
        df = pl.DataFrame(rows, schema=["symbol", "trade_status", "name"], orient="row")
        df = df.with_columns(
            symbol=pl.col("symbol").cast(pl.Utf8).map_elements(
                lambda s: str(parse_symbol(s)), return_dtype=pl.Utf8),
            is_st=pl.col("name").cast(pl.Utf8).str.contains(r"ST"),
        ).with_columns(
            sec_type=pl.col("symbol").map_elements(
                lambda s: _sec_type_of(s).value, return_dtype=pl.Utf8),
            board=pl.col("symbol").map_elements(
                lambda s: parse_symbol(s).board.value, return_dtype=pl.Utf8),
        )
        return df.select("symbol", "name", "sec_type", "board", "is_st", "trade_status")

    def security_details(self, symbols: list[str]) -> pl.DataFrame:
        """逐只补 ipoDate / outDate / type。慢，必须配 checkpoint。"""
        from lquant.data.watchdog import run_with_watchdog

        out = []
        for sym in symbols:
            try:
                rows = run_with_watchdog(_bs_basic, _bs_code(sym), timeout=30)
            except (TimeoutError, RuntimeError):
                continue
            if not rows or not rows[0]:
                continue
            r = rows[0]
            out.append({
                "symbol": str(parse_symbol(r[0])),
                "name": r[1],
                "list_date": _to_date(r[2]),
                "delist_date": _to_date(r[3]),
                "sec_type": _BS_TYPE_TO_SEC.get(r[4], "other"),
            })
        return pl.DataFrame(out) if out else pl.DataFrame()

    def trade_calendar(self, start: date, end: date) -> pl.DataFrame:
        from lquant.data.watchdog import run_with_watchdog

        # baostock 对 >10 年的区间会静默挂起（watchdog 超时），按 5 年分段拉取
        rows: list[list[str]] = []
        for s, e in _cal_chunks(start, end, _CAL_CHUNK_YEARS):
            rows.extend(run_with_watchdog(_bs_trade_dates, s.isoformat(), e.isoformat()))
        if not rows:
            return pl.DataFrame()
        df = pl.DataFrame(rows, schema=["trade_date", "is_open"], orient="row")
        return df.with_columns(
            trade_date=pl.col("trade_date").str.to_date("%Y-%m-%d"),
            is_open=pl.col("is_open").cast(pl.Utf8) == "1",
            exchange=pl.lit("SSE"),
        )

    def etf_meta(self, symbols: list[str] | None = None) -> pl.DataFrame:
        """ETF 元数据。

        BaoStock 不提供跟踪指数/费率/份额，这里只能给出 symbol/name/
        推断的 track_index/**sellable_after_days**；其余列留空，由
        ingest/etf_meta.py 用 akshare|东财 补全。
        """
        secs = self.securities()
        if not len(secs):
            return pl.DataFrame()
        etfs = secs.filter(pl.col("sec_type").is_in(["etf", "lof"]))
        if symbols:
            etfs = etfs.filter(pl.col("symbol").is_in(symbols))
        if not len(etfs):
            return pl.DataFrame()
        return etfs.select(
            "symbol", "name",
            pl.col("name").cast(pl.Utf8).map_elements(
                _guess_track_index, return_dtype=pl.Utf8).alias("track_index"),
            pl.lit(None, dtype=pl.Utf8).alias("fund_type"),
            pl.col("name").cast(pl.Utf8).map_elements(
                lambda n: _guess_sellable_days(n), return_dtype=pl.Int8,
            ).alias("sellable_after_days"),
            pl.lit(None, dtype=pl.Float64).alias("management_fee"),
            pl.lit(None, dtype=pl.Float64).alias("custody_fee"),
            pl.lit(None, dtype=pl.Float64).alias("fund_size"),
            pl.lit(None, dtype=pl.Float64).alias("share_outstanding"),
            pl.lit(today_cn(), dtype=pl.Date).alias("as_of"),
            pl.lit("baostock").alias("source"),
        )

    # ---------------------------------------------------------------- 财务
    def financial_pit(self, symbols: list[str], start: date, end: date,
                      kinds: tuple[str, ...] = ("profit", "balance", "cashflow"),
                      ) -> pl.DataFrame:
        """PIT 财务：stat_date（报告期）+ pub_date（公告日）双日期。

        缺 pub_date 就等于给未来函数开门 —— 宁可丢数据也不放行。
        BaoStock 按 (code, year, quarter) 单点查询无批量，只能逐只逐季，
        所以标的池必须受限（默认调用方传中证 800 或自选池）。
        """
        from lquant.data.watchdog import run_with_watchdog

        recs: list[dict] = []
        for sym in symbols:
            for (y, q) in _quarters(start, end):
                for kind in kinds:
                    try:
                        payload = run_with_watchdog(
                            _bs_report, kind, _bs_code(sym), y, q, timeout=30)
                    except (TimeoutError, RuntimeError):
                        continue
                    if len(payload) < 2:
                        continue
                    head, *rows = payload
                    if "statDate" not in head or "pubDate" not in head:
                        continue
                    si, pi = head.index("statDate"), head.index("pubDate")
                    ci = head.index("code") if "code" in head else None
                    for r in rows:
                        stat_d, pub_d = _to_date(r[si]), _to_date(r[pi])
                        if stat_d is None or pub_d is None:
                            continue  # 无公告日 → 无法防未来函数 → 丢弃
                        code = r[ci] if ci is not None else sym
                        for col, val in zip(head, r, strict=False):
                            if col in ("code", "statDate", "pubDate") or val in ("", None):
                                continue
                            try:
                                fv = float(val)
                            except (TypeError, ValueError):
                                continue
                            recs.append({
                                "symbol": str(parse_symbol(code)),
                                "stat_date": stat_d, "pub_date": pub_d,
                                "report_type": f"{y}Q{q}", "item": f"{kind}.{col}",
                                "value": fv, "unit": None,
                                "source": "baostock", "ingested_at": datetime.now(),
                            })
        return pl.DataFrame(recs) if recs else pl.DataFrame()
