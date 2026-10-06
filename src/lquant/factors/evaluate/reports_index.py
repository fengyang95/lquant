"""报告中心索引：报告文件的身份、版本、生成时间与陈旧判定。

**为什么抽成模块**：这段逻辑原先只长在 API 路由里（``/factors/reports``），
于是 CLI 无法复用 —— 而「旧口径报告要能被识别、被清理、被重算」是运维动作，
运维动作必须在 CLI 里做得到，不能只能靠 Web 点。

报告的版本判据是 ``<head>`` 里的两个 meta（见 docs/因子报告内容契约.md §6）：

- ``lquant-report-generator``：生成器版本；与当前版本不一致 → 陈旧
- ``lquant-report-generated-at``：生成时间

**缺版本号 = 版本号引入之前的产物 = 陈旧**。这是那批「修复前生成的错误报告」
唯一的机器可辨特征 —— 报告文件名不含版本，正文里也看不出口径。
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from lquant.factors.evaluate.report import REPORT_GENERATOR_VERSION

__all__ = [
    "REPORT_GENERATOR_VERSION",
    "list_reports",
    "read_report_expr",
    "read_report_meta",
    "report_row",
    "resolve_report_expression",
    "stale_reports",
]

_VERSION_RE = re.compile(r'name="lquant-report-generator" content="([^"]*)"')
_AT_RE = re.compile(r'name="lquant-report-generated-at" content="([^"]*)"')
#: v2 报告在「样本与口径」里内嵌了因子表达式 —— 重算的第一优先来源。
_EXPR_RE = re.compile(r"<dt>因子表达式</dt><dd>(.*?)</dd>")
_HEAD_BYTES = 4096
_EXPR_SCAN_BYTES = 200_000


def read_report_meta(path: Path) -> tuple[str | None, str | None]:
    """读「生成器版本 / 生成时间」。只读前 4KB（报告中心要列几百份）。"""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            head = f.read(_HEAD_BYTES)
    except OSError:
        return None, None
    mv, ma = _VERSION_RE.search(head), _AT_RE.search(head)
    return (mv.group(1) if mv else None), (ma.group(1) if ma else None)


def read_report_expr(path: Path) -> str | None:
    """读报告里内嵌的因子表达式（v2 报告有，旧报告没有）。"""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            body = f.read(_EXPR_SCAN_BYTES)
    except OSError:
        return None
    m = _EXPR_RE.search(body)
    if not m:
        return None
    import html as _html

    expr = _html.unescape(m.group(1)).strip()
    return expr or None


def report_row(path: Path) -> dict:
    """报告中心的一行：身份 + 版本 + 生成时间 + 是否陈旧。"""
    st = path.stat()
    ver, gen_at = read_report_meta(path)
    return {
        "name": path.stem,
        "path": str(path),
        "url": f"/api/factors/reports/{path.stem}",
        "size_kb": st.st_size // 1024,
        "generator_version": ver,
        "generated_at": gen_at or datetime.fromtimestamp(
            st.st_mtime).strftime("%Y-%m-%d %H:%M"),
        "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
        "current_version": REPORT_GENERATOR_VERSION,
        # 版本号缺失（修复前的产物）或对不上当前生成器 → 陈旧，口径可能已变
        "stale": ver != REPORT_GENERATOR_VERSION,
    }


def list_reports(report_dir: Path) -> list[dict]:
    """按修改时间倒序列出报告。``report_dir`` 不存在 → 空列表。"""
    if not report_dir.exists():
        return []
    return [report_row(p) for p in
            sorted(report_dir.glob("*.html"), key=lambda x: -x.stat().st_mtime)]


def stale_reports(report_dir: Path) -> list[dict]:
    return [r for r in list_reports(report_dir) if r["stale"]]


def resolve_report_expression(stem: str, path: Path | None = None) -> tuple[str | None, str]:
    """推断一份报告是用哪个表达式生成的。返回 ``(表达式, 依据说明)``。

    三条来源，按可信度排序（**只认精确匹配，绝不猜** —— 猜错等于用错口径
    覆盖掉一份报告，比留着旧报告更糟）：

    1. 报告正文内嵌的「因子表达式」（v2 报告）；
    2. ``factor_def.name == stem`` → 该因子的 expression；
    3. ``factor_<canonical_id>`` 形式 → 在 factor_def 里按 canonical_id 反查。

    都查不到时返回 ``(None, 原因)``：这类报告只能从它的产生地（因子库 /
    挖掘台账 / 手工重跑）重建。
    """
    if path is not None:
        expr = read_report_expr(path)
        if expr:
            return expr, "报告内嵌表达式"

    try:
        from lquant.core.db import reader

        with reader() as con:
            row = con.execute(
                "SELECT expression FROM factor_def WHERE name = ?", [stem]).fetchone()
    except Exception as e:  # noqa: BLE001 - 读库失败只降级到「无法推断」
        return None, f"因子注册表不可读（{type(e).__name__}）"

    if row and row[0]:
        return str(row[0]), f"factor_def.name == {stem}"

    if stem.startswith("factor_"):
        want = stem[len("factor_"):]
        try:
            from lquant.factors.dsl.printer import canonical_id

            with reader() as con:
                rows = con.execute(
                    "SELECT name, expression FROM factor_def WHERE expression IS NOT NULL"
                ).fetchall()
            for name, expression in rows:
                try:
                    if canonical_id(expression) == want:
                        return str(expression), f"canonical_id 反查（factor_def.name={name}）"
                except Exception:  # noqa: BLE001 - 个别表达式算不出 id，跳过
                    continue
        except Exception as e:  # noqa: BLE001
            return None, f"canonical_id 反查失败（{type(e).__name__}）"

    return None, "报告未内嵌表达式，且报告名不是已注册因子 —— 无法自动推断"
