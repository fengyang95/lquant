"""实时行情快照（/ws/market/ticks 的数据源）。

设计（6A 市场看板 / ws 契约）：易失数据必须即时落库、可降级。
- fetch_quotes 兜底从数据源 Provider 实时通路取值（mootdx/腾讯），失败抛 ConnectionError；
  前端收到 available=false 帧后用日线末价兜底，不打断看板。
- backend 可注入：单测传假源断言行结构；真实跑取 provider。
"""
from __future__ import annotations


class TicksError(Exception):
    """实时源不可用（断网/未配置实时 Provider/标的未知）。

    不用 @dataclass：Exception 子类挂 dataclass 会让 args 丢失，
    str(e) 为空、pickle 还原后 detail 也没了。
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail)
        self.detail = detail


def fetch_quotes(symbols: list[str], backend=None, *, timeout: float = 5.0) -> list[dict]:
    """取一组标的的实时快照。无实时源/失败 → 抛 TicksError（不静默回空）。

    返回行：{symbol, name?, price, change_pct, ts}，均校验过（symbol 非空、price 有限数）。
    timeout 是**整体预算**（上界，供调用方/上层超时用）；实时源内部另有自己的连接超时。
    """
    if not symbols:
        return []
    backend = backend or _realtime_backend
    timeout  # noqa: B018 - 透传给注入后端；真实后端忙于自身超时，此处仅作预算注释
    try:
        rows = backend(symbols, timeout=timeout)
    except Exception as e:  # noqa: BLE001 - 统一成 TicksError 让上层判断降级
        raise TicksError(f"{type(e).__name__}: {e}") from e
    validated = _validate_rows(rows)
    if not validated:
        raise TicksError("实时行情解析为空（无有效符号/价格）")
    return validated


def _validate_rows(rows: list[dict]) -> list[dict]:
    """信任边界收口：丢掉坏行（缺符号 / 价格非有限数），补齐 ts。"""
    import math

    out = []
    for r in rows:
        sym = r.get("symbol")
        price = r.get("price")
        if not sym or not isinstance(price, (int, float)) or not math.isfinite(float(price)):
            continue
        out.append({"symbol": str(sym), "name": r.get("name"),
                    "price": float(price), "change_pct": r.get("change_pct"),
                    "ts": r.get("ts")})
    return out


def _realtime_backend(symbols: list[str], *, timeout: float = 5.0) -> list[dict]:
    """从 Fallback 链取实时快照（由 config/providers.yaml 路由到 REALTIME 源）。"""
    from lquant.data.providers import get_provider

    df = get_provider().realtime(symbols)
    ts = __import__("datetime").datetime.now().isoformat(timespec="seconds")
    rows = []
    for r in df.to_dicts():
        rows.append({"symbol": r.get("symbol"), "name": r.get("name"),
                     "price": r.get("price"), "change_pct": r.get("change_pct"),
                     "ts": ts})
    return rows