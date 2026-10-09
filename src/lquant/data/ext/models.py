"""扩展表（BYO 数据）的配置模型。

lquant 的数据湖长期是「官方数据集」形态：字段集合由平台预先定义（``daily_bar``
等），用户自有数据（热度、板块归属、舆情、景气度）无处安放。本模块把自有数据
提升为一等公民：一张扩展表 = 一份配置（字段/模式/拉取方式）+ 独立 parquet 区域。

为什么配置要做成显式模型而不是随手一个 dict：
- 扩展数据会参与因子计算与回测，**忘记声明类型**或**日期模式写错**都会变成
  静默的口径错误（把当日快照当成历史序列就是典型的未来函数）。所以这里对
  每个字段做 fail-loud 校验，非法输入在写入前就拒绝，而不是靠下游猜。
- 配置直接决定 parquet 分区形状与因子注册名，必须是可复现、可版本化的文本。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

#: 合法字段类型。f64 用作数值统一口径，避免上游 int/float 混用导致因子注册发散。
FIELD_DTYPES = ("string", "int", "float", "bool")
#: 数值类型 —— 会被注册为因子；string 只进信号条件通道，bool 不参与因子排序。
NUMERIC_DTYPES = ("int", "float")

TableMode = Literal["timeseries", "snapshot"]

# 表 id 直接拼进 `<数据根>/ext/<id>/` 路径，必须白名单校验：
# `id = "../daily"` 会让写入/删除离开扩展数据区域（TSP 参考实现同样在
# ExtConfigStore 里这么做，路径穿越是这类「用户自定义目录名」的标配漏洞）。
_ID_RE = re.compile(r"^[a-zA-Z0-9_]{1,64}$")


class ExtConfigError(ValueError):
    """扩展表配置非法（id/字段/模式）—— 调用方的错，必须显式报出来。"""


def _require_identifier(value: str, *, what: str) -> str:
    v = str(value or "").strip()
    if not _ID_RE.match(v):
        raise ExtConfigError(
            f"{what} 非法: {value!r}（只允许字母/数字/下划线，1-64 字符；"
            f"该值会拼进数据目录名，所以白名单是硬要求）"
        )
    return v


@dataclass(frozen=True)
class ExtField:
    """一个扩展字段。``label`` 只为展示，``dtype`` 决定它进哪条通道。"""

    name: str
    dtype: str = "string"
    label: str = ""

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise ExtConfigError("字段名不能为空")
        if self.dtype not in FIELD_DTYPES:
            raise ExtConfigError(
                f"字段 {self.name!r} 类型非法: {self.dtype!r}，可选 {FIELD_DTYPES}"
            )

    @property
    def numeric(self) -> bool:
        return self.dtype in NUMERIC_DTYPES

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "dtype": self.dtype, "label": self.label or self.name}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ExtField:
        if "name" not in raw:
            raise ExtConfigError(f"字段定义缺 name: {raw!r}")
        return cls(str(raw["name"]), str(raw.get("dtype", "string")),
                   str(raw.get("label", "") or ""))


@dataclass
class PullConfig:
    """HTTP 拉取配置（transport 层）。

    ``date_param`` / ``date_format`` / ``date_field`` 刻意放在 :class:`ExtConfig`
    顶层 —— 它们是「表的日期契约」，不是某一次 HTTP 请求的实现细节；写入路径
    （JSON/CSV 上传）也要用同一个日期契约做校验，放这里会逼调用方多穿一层。
    """

    url: str = ""
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None
    #: 响应里行数组的 dot-path（如 "data.list"）；空 = 响应本身就是数组。
    response_path: str = ""
    #: 外部字段名 → 配置字段名。
    field_map: dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = 30
    #: 分页：页码参数名（None=单次请求）。
    page_param: str = ""
    page_size_param: str = ""
    page_size: int = 0
    max_pages: int = 20
    enabled: bool = False
    schedule_minutes: int = 1440
    #: 拉取状态回写（供 CLI/API 展示「上次拉成什么样」）。
    last_run: str | None = None
    last_status: str | None = None
    last_message: str | None = None
    last_rows: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url, "method": self.method, "headers": self.headers,
            "body": self.body, "response_path": self.response_path,
            "field_map": self.field_map, "timeout_seconds": self.timeout_seconds,
            "page_param": self.page_param, "page_size_param": self.page_size_param,
            "page_size": self.page_size, "max_pages": self.max_pages,
            "enabled": self.enabled, "schedule_minutes": self.schedule_minutes,
            "last_run": self.last_run, "last_status": self.last_status,
            "last_message": self.last_message, "last_rows": self.last_rows,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> PullConfig:
        d = raw or {}
        method = str(d.get("method", "GET")).upper()
        if method not in ("GET", "POST"):
            raise ExtConfigError(f"拉取 method 非法: {method!r}（可选 GET/POST）")
        timeout = d.get("timeout_seconds", 30)
        if not isinstance(timeout, (int, float)) or not 5 <= timeout <= 300:
            # 手改配置写出非法值时归一为 30：宁可退回安全默认，也不要让
            # 一个 0 超时在夜里把整个回补任务拖死。
            timeout = 30
        return cls(
            url=str(d.get("url", "") or ""), method=method,
            headers=dict(d.get("headers") or {}), body=d.get("body"),
            response_path=str(d.get("response_path", "") or ""),
            field_map=dict(d.get("field_map") or {}),
            timeout_seconds=int(timeout),
            page_param=str(d.get("page_param", "") or ""),
            page_size_param=str(d.get("page_size_param", "") or ""),
            page_size=int(d.get("page_size", 0) or 0),
            max_pages=max(1, min(200, int(d.get("max_pages", 20) or 20))),
            enabled=bool(d.get("enabled", False)),
            schedule_minutes=int(d.get("schedule_minutes", 1440) or 1440),
            last_run=d.get("last_run"), last_status=d.get("last_status"),
            last_message=d.get("last_message"), last_rows=d.get("last_rows"),
        )


@dataclass
class ExtConfig:
    """一张扩展表的完整定义。

    - ``mode="timeseries"``：按交易日分区，值按 ``(symbol, date_field)`` 精确对齐。
    - ``mode="snapshot"``：只有「最新值」，只在当日/单日帧注入（见 pit.py）。

    ``market_level=True``：行 = 全市场每日一条（无 symbol 列），适合择时/情绪
    序列 —— 这类表不能按标的 join，必须按日期 join，否则会静默 join 不上或
    膨胀成笛卡尔积。
    """

    id: str
    label: str
    mode: TableMode
    fields: list[ExtField]
    description: str = ""
    #: 标的列名。market_level 表无标的列，必须为 None。
    symbol_field: str | None = "symbol"
    #: 日期列名（也是 HTTP 响应里用于日期校验的字段）。
    date_field: str = "date"
    #: 接口按日期查询的参数名；None = 接口只有当日快照（不可回补）。
    date_param: str | None = None
    #: date_param 的取值格式：iso=YYYY-MM-DD / compact=YYYYMMDD。
    date_format: str = "iso"
    market_level: bool = False
    created_at: str | None = None
    updated_at: str | None = None
    pull: PullConfig | None = None

    def __post_init__(self) -> None:
        self.id = _require_identifier(self.id, what="表 id")
        if not str(self.label).strip():
            raise ExtConfigError(f"扩展表 {self.id} 缺少显示名 label")
        if self.mode not in ("timeseries", "snapshot"):
            raise ExtConfigError(
                f"扩展表 {self.id} mode 非法: {self.mode!r}（可选 timeseries/snapshot）"
            )
        if not self.fields:
            raise ExtConfigError(f"扩展表 {self.id} 至少需要一个字段")
        names = [f.name for f in self.fields]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise ExtConfigError(f"扩展表 {self.id} 字段名重复: {sorted(dup)}")
        if self.date_format not in ("iso", "compact"):
            raise ExtConfigError(
                f"扩展表 {self.id} date_format 非法: {self.date_format!r}（可选 iso/compact）"
            )
        if self.market_level:
            # 市场级表没有标的维度：留一个 symbol_field 会让读者误以为能按标的
            # 过滤，也会让读取侧少一条明确的「无 symbol 也合法」判断。
            if self.symbol_field:
                raise ExtConfigError(
                    f"扩展表 {self.id} 标记 market_level=True 时不得设置 symbol_field"
                )
        else:
            self.symbol_field = _require_identifier(
                self.symbol_field or "", what=f"扩展表 {self.id} 的 symbol_field"
            )
        if not str(self.date_field).strip():
            raise ExtConfigError(f"扩展表 {self.id} date_field 不能为空")

    # ---------------------------------------------------------------- 派生视图
    def field(self, name: str) -> ExtField | None:
        return next((f for f in self.fields if f.name == name), None)

    @property
    def numeric_fields(self) -> list[ExtField]:
        return [f for f in self.fields if f.numeric]

    @property
    def string_fields(self) -> list[ExtField]:
        return [f for f in self.fields if f.dtype == "string"]

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id, "label": self.label, "mode": self.mode,
            "fields": [f.to_dict() for f in self.fields],
            "description": self.description, "symbol_field": self.symbol_field,
            "date_field": self.date_field, "date_param": self.date_param,
            "date_format": self.date_format, "market_level": self.market_level,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }
        if self.pull is not None:
            d["pull"] = self.pull.to_dict()
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ExtConfig:
        for key in ("id", "label", "mode"):
            if key not in raw:
                raise ExtConfigError(f"扩展表配置缺字段 {key!r}: {raw!r}")
        return cls(
            id=raw["id"], label=raw["label"], mode=raw["mode"],
            fields=[ExtField.from_dict(f) for f in raw.get("fields") or []],
            description=str(raw.get("description", "") or ""),
            symbol_field=raw.get("symbol_field", "symbol"),
            date_field=str(raw.get("date_field", "date") or "date"),
            date_param=raw.get("date_param"),
            date_format=str(raw.get("date_format", "iso") or "iso"),
            market_level=bool(raw.get("market_level", False)),
            created_at=raw.get("created_at"), updated_at=raw.get("updated_at"),
            pull=PullConfig.from_dict(raw["pull"]) if raw.get("pull") else None,
        )
