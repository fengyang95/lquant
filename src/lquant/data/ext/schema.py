"""扩展表的 schema 自动发现、列名归一与编码处理。

为什么这些要单独一层：用户自有数据的形态完全不可控（中文列名、GBK 编码、
带时间戳的括号后缀、代码写成 600000 或 sh.600000）。让这些差异渗进存储层，
每一处消费方都得重复处理一遍，且必然分叉。统一在这里收敛成一套口径。
"""

from __future__ import annotations

import codecs
import re
from pathlib import Path

import polars as pl

from lquant.data.ext.models import ExtField

#: polars dtype → 扩展字段类型。未知/复杂类型一律 string：
#: 把数组或结构体硬当数值会算出无意义的因子，退化成字符串至少是显式可见的。
_DTYPE_TO_EXT: tuple[tuple[object, str], ...] = (
    (pl.Boolean, "bool"),
    (pl.Int8, "int"), (pl.Int16, "int"), (pl.Int32, "int"), (pl.Int64, "int"),
    (pl.UInt8, "int"), (pl.UInt16, "int"), (pl.UInt32, "int"), (pl.UInt64, "int"),
    (pl.Float32, "float"), (pl.Float64, "float"),
)

_EXT_TO_POLARS: dict[str, object] = {
    "string": pl.Utf8, "int": pl.Int64, "float": pl.Float64, "bool": pl.Boolean,
}


def polars_dtype(dtype: str) -> object:
    """扩展字段类型 → polars dtype（未知类型 fail-loud，不静默退回 string）。"""
    if dtype not in _EXT_TO_POLARS:
        raise ValueError(f"未知字段类型: {dtype!r}（可选 {tuple(_EXT_TO_POLARS)}）")
    return _EXT_TO_POLARS[dtype]


def infer_dtype(series: pl.Series) -> str:
    """从一个 polars Series 推断扩展字段类型。"""
    dt = series.dtype
    for pl_type, ext_type in _DTYPE_TO_EXT:
        if dt == pl_type:
            return ext_type
    if dt.is_numeric():  # Decimal 等数值类型
        return "float"
    return "string"


def ext_column_name(table_id: str, field_name: str) -> str:
    """扩展字段在面板/因子里的列名：``ext_{table_id}_{field}``。

    保留中日韩文字（``\\w`` 含 unicode 字母）—— 自有数据的字段名大量是中文
    （所属概念/热度分），全部折叠成 ASCII 会互相碰撞（"热度分" 与 "热度值"
    都变 "___"）。非单词字符转下划线。
    """
    sanitized = re.sub(r"[^\w]+", "_", str(field_name), flags=re.UNICODE).strip("_") or "f"
    return f"ext_{table_id}_{sanitized}"


def infer_fields(df: pl.DataFrame) -> list[ExtField]:
    """从 DataFrame 自动发现字段与类型（写入前未显式声明时的默认路径）。

    为什么字符串字段不注册为因子（见 factors/ext_bridge）：因子评价是 IC/排序
    口径，对字符串做排序没有金融含义；而字符串的价值在「条件筛选」（概念/行业
    归属），那是另一条通道。两条通道分开是刻意的。
    """
    return [ExtField(name=c, dtype=infer_dtype(df[c]), label=c) for c in df.columns]


# ---------------------------------------------------------------------------
# 列名归一
# ---------------------------------------------------------------------------

# 行情软件导出的表头常带时间戳/单位后缀，如 "收盘价(元)"、"涨跌幅(2024-01-01)"。
# 不去掉的话同一份数据不同日期导出会得到不同列名 → 配置对不上、因子名漂移。
_PAREN_RE = re.compile(r"[(（][^)）]*[)）]")


def clean_column_names(df: pl.DataFrame) -> pl.DataFrame:
    """清洗列名：去 BOM/首尾空白、去括号内容、重名加序号。

    重名必须显式消歧而不是让 polars 抛 DuplicateError：真实导出的 CSV 里
    「涨跌幅」出现两次是常态，抛错会让用户完全无从下手；加后缀至少能继续导入，
    且改名结果可由 ``lq ext rows`` 看到。
    """
    renames: dict[str, str] = {}
    seen: dict[str, int] = {}
    for col in df.columns:
        base = str(col).lstrip("\ufeff").strip()
        base = _PAREN_RE.sub("", base).strip() or "col"
        if base in seen:
            seen[base] += 1
            renames[col] = f"{base}_{seen[base]}"
        else:
            seen[base] = 0
            renames[col] = base
    return df.rename(renames)


# ---------------------------------------------------------------------------
# 标的归一
# ---------------------------------------------------------------------------

_SYMBOL_TAIL_RE = re.compile(r"\.(SH|SZ|BJ)$", re.IGNORECASE)


def normalize_symbol(value: object) -> str:
    """把各种代码写法归一为 lquant 的 ``600000.SH`` 口径。

    走 ``core.types.parse_symbol`` 保证与全平台同一套规则（含北交所）；
    解析失败时**原样返回**而不是丢弃 —— 丢弃会让该行静默消失，而
    「代码解析不了」是必须让用户看到的事实（读出来还是原值，一眼能发现）。
    """
    from lquant.core.types import parse_symbol

    raw = "" if value is None else str(value).strip()
    if not raw:
        return ""
    try:
        return str(parse_symbol(raw))
    except ValueError:
        return raw


def normalize_symbol_column(df: pl.DataFrame, column: str) -> pl.DataFrame:
    """把指定列归一为 ``代码.交易所``（列不存在则原样返回）。"""
    if column not in df.columns:
        return df
    return df.with_columns(
        pl.col(column).cast(pl.Utf8).map_elements(normalize_symbol, return_dtype=pl.Utf8)
    )


# ---------------------------------------------------------------------------
# 编码
# ---------------------------------------------------------------------------

_CHUNK_BYTES = 1024 * 1024
#: 国内行情软件/中文 Windows Excel 导出的 CSV 常见编码，按命中概率排序。
_CN_ENCODINGS = ("gb18030", "gbk", "gb2312", "big5")


def _decodes_as(path: Path, encoding: str) -> bool:
    """整个文件能否按 encoding 完整解码（分块，不把文件读进内存）。"""
    decoder = codecs.getincrementaldecoder(encoding)()
    try:
        with path.open("rb") as src:
            while chunk := src.read(_CHUNK_BYTES):
                decoder.decode(chunk)
            decoder.decode(b"", True)  # 结尾半个字符也算失败
    except UnicodeDecodeError:
        return False
    return True


def _transcode_to_utf8(src: Path, dst: Path, encoding: str) -> bool:
    """按 encoding 转成 UTF-8；解码失败删除半成品并返回 False。

    用增量解码器跨块边界：GBK 汉字两字节可能正好被切成两半，
    逐块独立 decode 会误判为失败。
    """
    decoder = codecs.getincrementaldecoder(encoding)()
    try:
        with src.open("rb") as fin, dst.open("w", encoding="utf-8", newline="") as fout:
            while chunk := fin.read(_CHUNK_BYTES):
                fout.write(decoder.decode(chunk))
            fout.write(decoder.decode(b"", True))
    except UnicodeDecodeError:
        dst.unlink(missing_ok=True)
        return False
    return True


def ensure_utf8_csv(path: Path) -> Path:
    """保证 CSV 可按 UTF-8 读取；GBK 系编码则转写出 ``*.utf8`` 副本。

    polars.read_csv 默认按 UTF-8 解析，遇到同花顺/通达信导出的 GBK CSV 会抛
    "invalid utf-8 sequence"，而用户完全不知道为什么「同一个文件在 Excel 里
    明明能打开」。这里在交给 polars 之前做一次编码规范化。
    """
    if _decodes_as(path, "utf-8"):
        return path
    for enc in _CN_ENCODINGS:
        out = path.with_suffix(path.suffix + ".utf8")
        if _transcode_to_utf8(path, out, enc):
            from loguru import logger

            logger.info(f"扩展数据 CSV 编码转换 {path.name} → {out.name} ({enc})")
            return out
    # 都解不开：把原文件交回去，让 polars 抛更精确的原始错误。
    return path
