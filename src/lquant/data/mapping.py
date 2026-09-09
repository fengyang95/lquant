"""数据源字段映射：加载 / 校验 / 应用。

约定（唯一事实源是 schema.py 的 SCHEMAS dict）：
- 映射目标列必须 ∈ SCHEMAS[table]，否则 MappingError
- derive 表达式用 ast 白名单解析，杜绝任意代码执行
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl
import yaml

from lquant.core.config import get_settings
from lquant.core.errors import MappingError
from lquant.data.schema import SCHEMAS

_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div)
_ALLOWED_CALLS = ("concat", "strptime")
_EXPR_NODES = (ast.Name, ast.Constant, ast.BinOp, ast.Call, ast.Load)


@dataclass(frozen=True)
class DeriveRule:
    """一条派生列规则：expr 基于 from_cols 计算。"""

    expr: str
    from_cols: tuple[str, ...]


@dataclass(frozen=True)
class TableMapping:
    """一张表某个数据源的映射规则（不可变）。"""

    table: str
    rename: dict[str, str]
    derive: dict[str, DeriveRule]
    fill: dict[str, Any]
    required: tuple[str, ...]


def _parse_expr(expr: str) -> ast.Expression:
    try:
        return ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise MappingError(f"非法表达式 {expr!r}: {e}") from e


def _extract_names(expr: str) -> tuple[str, ...]:
    """提取表达式里引用的列名（ast.Name 节点，按出现顺序去重）。"""
    seen: list[str] = []
    for node in ast.walk(_parse_expr(expr)):
        if isinstance(node, ast.Name) and node.id not in seen:
            seen.append(node.id)
    return tuple(seen)


def _coerce_rule(rule: DeriveRule | str) -> DeriveRule:
    """兼容直接构造 TableMapping 时 derive 传字符串的写法。"""
    if isinstance(rule, DeriveRule):
        return rule
    return DeriveRule(expr=str(rule), from_cols=_extract_names(str(rule)))


def _check_targets(
    table: str,
    source: str,
    kind: str,
    targets: Any,
    schema_cols: set[str],
) -> None:
    """fail-fast：目标列不在 SCHEMAS[table] 中直接抛 MappingError。"""
    for col in targets:
        if col not in schema_cols:
            raise MappingError(
                f"{table}.yaml sources.{source}.{kind}: 目标列 {col!r}"
                f" 不在 SCHEMAS[{table}] 中"
            )


def _check_targets(
    table: str,
    source: str,
    kind: str,
    targets: Any,
    schema_cols: set[str],
) -> None:
    """fail-fast：目标列不在 SCHEMAS[table] 中直接抛 MappingError。"""
    for col in targets:
        if col not in schema_cols:
            raise MappingError(
                f"{table}.yaml sources.{source}.{kind}: 目标列 {col!r}"
                f" 不在 SCHEMAS[{table}] 中"
            )


def load_table_mapping(
    table: str,
    source: str,
    config_dir: Path | None = None,
) -> TableMapping:
    """读 `<config_dir>/schema/<table>.yaml` 的 `sources.<source>` 节。

    缺文件 / 缺 `sources.<source>` 节抛 MappingError。
    """
    if config_dir is None:
        config_dir = get_settings().config_dir
    path = Path(config_dir) / "schema" / f"{table}.yaml"
    if not path.exists():
        raise MappingError(f"映射配置不存在: {path}")
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sources = doc.get("sources") or {}
    if source not in sources:
        raise MappingError(f"{path} 缺少 sources.{source} 节")
    sec = sources[source] or {}

    schema_cols = set(SCHEMAS.get(table, {}))
    if not schema_cols:
        raise MappingError(f"未知表 {table!r}（不在 SCHEMAS 中）")

    rename = dict(sec.get("rename") or {})
    fill = dict(sec.get("fill") or {})
    required = tuple(sec.get("required") or ())

    # fail-fast：映射目标列必须 ∈ SCHEMAS[table]，否则最终 select 会静默丢列
    _check_targets(table, source, "rename", rename.values(), schema_cols)
    _check_targets(table, source, "fill", fill.keys(), schema_cols)
    _check_targets(table, source, "derive", (sec.get("derive") or {}).keys(), schema_cols)

    derive: dict[str, DeriveRule] = {}
    for target, raw in (sec.get("derive") or {}).items():
        expr = str(raw)
        derive[target] = DeriveRule(expr=expr, from_cols=_extract_names(expr))

    return TableMapping(
        table=table,
        rename=rename,
        derive=derive,
        fill=fill,
        required=required,
    )


def _compile_node(node: ast.AST, df: pl.DataFrame, target: str) -> pl.Expr:
    """把白名单表达式节点编译为 pl.Expr。非白名单节点抛 MappingError。"""
    if isinstance(node, ast.Name):
        col = node.id
        if col not in df.columns:
            raise MappingError(f"derive {target!r} 引用不存在的列 {col!r}")
        return pl.col(col)
    if isinstance(node, ast.Constant):
        return pl.lit(node.value)
    if isinstance(node, ast.BinOp):
        return _compile_binop(node, df, target)
    if isinstance(node, ast.Call):
        return _compile_call(node, df, target)
    raise MappingError(f"derive {target!r}: 不允许的语法节点 {type(node).__name__}")


def _compile_binop(node: ast.BinOp, df: pl.DataFrame, target: str) -> pl.Expr:
    if not isinstance(node.op, _ALLOWED_BINOPS):
        raise MappingError(
            f"derive {target!r}: 不支持的运算符 {type(node.op).__name__}"
        )
    left = _compile_node(node.left, df, target)
    right = _compile_node(node.right, df, target)
    if isinstance(node.op, ast.Add):
        return left + right
    if isinstance(node.op, ast.Sub):
        return left - right
    if isinstance(node.op, ast.Mult):
        return left * right
    return left / right


def _compile_call(node: ast.Call, df: pl.DataFrame, target: str) -> pl.Expr:
    if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_CALLS:
        raise MappingError(f"derive {target!r}: 只允许调用 concat / strptime")
    if node.keywords:
        raise MappingError(f"derive {target!r}: 不允许关键字参数")
    if node.func.id == "concat":
        return _compile_concat(node, df, target)
    return _compile_strptime(node, df, target)


def _compile_concat(node: ast.Call, df: pl.DataFrame, target: str) -> pl.Expr:
    args = [_compile_node(a, df, target) for a in node.args]
    if not args:
        raise MappingError(f"derive {target!r}: concat 需要至少一个参数")
    return pl.concat_str(args, separator="", ignore_nulls=True)


def _compile_strptime(node: ast.Call, df: pl.DataFrame, target: str) -> pl.Expr:
    if len(node.args) != 2:
        raise MappingError(f"derive {target!r}: strptime(col, fmt) 需要两个参数")
    col_arg, fmt_arg = node.args
    if not isinstance(col_arg, ast.Name):
        raise MappingError(f"derive {target!r}: strptime 第一参数必须是列名")
    if col_arg.id not in df.columns:
        raise MappingError(f"derive {target!r} 引用不存在的列 {col_arg.id!r}")
    if not isinstance(fmt_arg, ast.Constant) or not isinstance(fmt_arg.value, str):
        raise MappingError(f"derive {target!r}: strptime 的 fmt 必须是字符串字面量")
    fmt = fmt_arg.value
    col_expr = pl.col(col_arg.id)
    if "%H" in fmt:
        return col_expr.str.to_datetime(format=fmt)
    return col_expr.str.to_date(format=fmt)


def _apply_derive(df: pl.DataFrame, tm: TableMapping) -> pl.DataFrame:
    for target, rule in tm.derive.items():
        rule_obj = _coerce_rule(rule)
        expr = _compile_node(_parse_expr(rule_obj.expr).body, df, target)
        df = df.with_columns(expr.alias(target))
    return df


def apply_mapping(
    df: pl.DataFrame,
    tm: TableMapping,
    params: dict[str, Any] | None = None,
) -> pl.DataFrame:
    """rename -> derive -> fill（params 覆盖 fill）-> 按 SCHEMAS[table] 列序 select。

    schema 外列一律丢弃。tm.table 不在 SCHEMAS 中抛 MappingError。
    """
    schema = SCHEMAS.get(tm.table)
    if schema is None:
        raise MappingError(f"未知表 {tm.table!r}（不在 SCHEMAS 中）")

    _check_targets(tm.table, "(runtime)", "rename", tm.rename.values(), set(schema))
    _check_targets(tm.table, "(runtime)", "derive", tm.derive.keys(), set(schema))
    out = df.rename(dict(tm.rename))
    out = _apply_derive(out, tm)

    fills = {**tm.fill, **(params or {})}
    _check_targets(tm.table, "(runtime)", "fill", fills.keys(), set(schema))
    for col, value in fills.items():
        out = out.with_columns(pl.lit(value).alias(col))

    # 按 schema 列序 select；缺失列补 null，并统一 cast 到 schema dtype
    cols = []
    for c, dt in schema.items():
        if c in out.columns:
            cols.append(out[c].cast(dt, strict=False))
        else:
            cols.append(pl.lit(None, dtype=dt).alias(c))
    return out.select(cols)


def _walk_static(node: ast.AST, target: str, errors: list[str]) -> None:
    """递归校验表达式：仅放行 Name/Constant/BinOp(+, -, *, /)/Call(concat|strptime)。"""
    if not isinstance(node, _EXPR_NODES):
        kind = type(node).__name__
        errors.append(f"derive {target!r}: 不允许的语法节点 {kind}（{ast.dump(node)[:60]}）")
        return
    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, _ALLOWED_BINOPS):
            op = type(node.op).__name__
            errors.append(f"derive {target!r}: 不支持的运算符 {op}")
        _walk_static(node.left, target, errors)
        _walk_static(node.right, target, errors)
    elif isinstance(node, ast.Call):
        fname = getattr(node.func, "id", None)
        if fname not in _ALLOWED_CALLS:
            errors.append(f"derive {target!r}: 只允许调用 concat / strptime")
        for a in node.args:
            _walk_static(a, target, errors)


def _validate_source(
    table: str,
    prefix: str,
    sec: dict[str, Any],
    schema_cols: set[str],
    errors: list[str],
) -> None:
    for src_col, dst in (sec.get("rename") or {}).items():
        if schema_cols and dst not in schema_cols:
            errors.append(
                f"{prefix}.rename: 源列 {src_col!r} -> 目标列 {dst!r}"
                f" 不在 SCHEMAS[{table}] 中"
            )
    for dst, raw in (sec.get("derive") or {}).items():
        if schema_cols and dst not in schema_cols:
            errors.append(f"{prefix}.derive: 目标列 {dst!r} 不在 SCHEMAS[{table}] 中")
        expr = str(raw)
        try:
            tree = _parse_expr(expr).body
        except MappingError as e:
            errors.append(f"{prefix}.derive {dst!r}: {e}")
            continue
        _walk_static(tree, dst, errors)
    for dst in (sec.get("fill") or {}):
        if schema_cols and dst not in schema_cols:
            errors.append(f"{prefix}.fill: 目标列 {dst!r} 不在 SCHEMAS[{table}] 中")


def validate_table_config(table: str, path: Path) -> list[str]:
    """静态校验 `schema/<table>.yaml`，返回错误列表（空 = 合法）。

    检查：文件 stem = table；rename/derive/fill 目标 ∈ SCHEMAS[table]；
    derive 表达式 ast 白名单；from 列静态可判时判存在性（运行期由 apply 兜底）。
    """
    path = Path(path)
    if not path.exists():
        return [f"配置文件不存在: {path}"]
    errors: list[str] = []
    if path.stem != table:
        errors.append(f"文件 stem {path.stem!r} != 表名 {table!r}，文件名应与表名一致")
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        return errors + [f"YAML 解析失败: {e}"]
    if not isinstance(doc, dict):
        return errors + ["顶层必须是 mapping（含 sources 键）"]
    sources = doc.get("sources")
    if not isinstance(sources, dict) or not sources:
        return errors + ["缺少 sources 节或为空"]

    schema_cols = set(SCHEMAS.get(table, {}))
    if not schema_cols:
        errors.append(f"未知表 {table!r}（不在 SCHEMAS 中）")

    for src, sec in sources.items():
        safe_sec = sec if isinstance(sec, dict) else {}
        _validate_source(table, f"sources.{src}", safe_sec, schema_cols, errors)
    return errors
