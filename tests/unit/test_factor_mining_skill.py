"""因子挖掘 skill 的文档自检：reference.md 里的模板必须真的能过 G0。

文档 = 测试 = 脚本，三层同源（见 docs/FACTOR_VALIDATION.md）。
写 skill 最容易出的错就是文档里的表达式根本没跑过 —— 算子名记错、字段拼错、
语法用成 Python 写法。这条用例把 reference.md 里所有代码块中的表达式抓出来，
替换窗口占位后逐条过一遍静态校验，让文档跟着代码一起腐烂不了。
"""
from __future__ import annotations

import re
from pathlib import Path

from lquant.core.config import find_root
from lquant.factors.mining.gates import g0_static
from lquant.factors.mining.submit import _daily_fields

SKILL_DIR = Path("config/skills/factor-mining")
REFERENCE = SKILL_DIR / "reference.md"
SKILL = SKILL_DIR / "SKILL.md"

# 窗口占位的替换值：一长一短，保证 Ts_Corr 这类双窗口算子都能实例化
SUBST = {"{w1}": "20", "{w2}": "60", "{w}": "20", "{q}": "0.8"}
# 允许字段 = 日线白名单 ∪ 协变量（面板里确实会出现，虽然不推荐当因子用）
ALLOWED = _daily_fields() | {"cov_market_share_pct", "market_share_pct",
                             "cov_market_cap", "cov_industry_sw1", "cov_turnover_1m"}


def _repo_root() -> Path:
    return find_root()


def _extract_expressions(md_path: Path) -> list[str]:
    """抓出 fenced code block 里「像因子表达式」的行。

    只要同时含 `$字段` 和函数调用括号，且不是语法定义 / 表格 / 注释 / JSON / shell。
    """
    text = md_path.read_text(encoding="utf-8")
    out: list[str] = []
    for block in re.findall(r"```[a-z]*\n(.*?)```", text, re.S):
        for raw in block.splitlines():
            line = raw.strip()
            if "$" not in line or "(" not in line:
                continue
            if line.startswith(("#", "{", "lq ", "http")) or ":=" in line or "..." in line:
                continue
            out.append(line)
    return out


def test_reference_expressions_are_present():
    exprs = _extract_expressions(_repo_root() / REFERENCE)
    assert len(exprs) > 15, f"reference.md 里只抓到 {len(exprs)} 条表达式，提取逻辑或文档有问题"


def test_every_reference_template_passes_g0():
    """每条模板替换窗口后都必须过 G0 —— 否则 Agent 照抄就会撞 STATIC_FAIL。"""
    exprs = _extract_expressions(_repo_root() / REFERENCE)
    failures = []
    for expr in exprs:
        inst = expr
        for k, v in SUBST.items():
            inst = inst.replace(k, v)
        if "{" in inst:      # 还有没替换掉的占位 → 文档写错了占位名
            failures.append((expr, f"未识别的占位符: {inst}"))
            continue
        r = g0_static(inst, allowed_fields=ALLOWED)
        if not r.passed:
            failures.append((expr, f"{r.reason_code}: {r.hint}"))
    assert not failures, "以下模板过不了 G0：\n" + "\n".join(
        f"  {e}\n    → {why}" for e, why in failures)


def test_skill_documents_only_real_cli_commands():
    """skill 里提到的 `lq ...` 命令必须真的存在于 CLI 里（防止改名后文档失联）。"""
    from click.testing import CliRunner

    from lquant.cli.main import cli

    runner = CliRunner()
    mentioned = set(re.findall(r"lq (factor|data|agent|backtest) ([a-z-]+)",
                               (_repo_root() / SKILL).read_text(encoding="utf-8")))
    assert mentioned, "SKILL.md 里没有提到任何 lq 子命令"
    for group, sub in sorted(mentioned):
        r = runner.invoke(cli, [group, sub, "--help"])
        assert r.exit_code == 0, f"`lq {group} {sub}` 不存在或报错：{r.output[:200]}"


# 文档里出现的 `lq <group> <sub> ... --flag`：flag 必须真实存在
_CMD_FLAG_RE = re.compile(r"lq (factor|data|agent|backtest) ([a-z-]+)([^\n`\"']*)")


def _documented_commands_and_flags() -> dict[tuple[str, str], set[str]]:
    found: dict[tuple[str, str], set[str]] = {}
    for md in (SKILL, Path("docs/agent-skill/SKILL.md")):
        text = (_repo_root() / md).read_text(encoding="utf-8")
        for group, sub, tail in _CMD_FLAG_RE.findall(text):
            flags = set(re.findall(r"--[a-z][a-z0-9-]*", tail))
            found.setdefault((group, sub), set()).update(flags)
    return found


def test_documented_flags_exist_on_the_command():
    """文档写的 `--flag` 必须在对应子命令的 --help 里 —— 参数改名后文档要跟着改。"""
    from click.testing import CliRunner

    from lquant.cli.main import cli

    runner = CliRunner()
    bad = []
    for (group, sub), flags in sorted(_documented_commands_and_flags().items()):
        r = runner.invoke(cli, [group, sub, "--help"])
        assert r.exit_code == 0, f"`lq {group} {sub}` 不存在"
        for f in sorted(flags):
            if f not in r.output:
                bad.append(f"lq {group} {sub} 的文档提到了不存在的 {f}")
    assert not bad, "文档与 CLI 参数不一致：\n  " + "\n  ".join(bad)


def test_skill_commands_use_expression_not_factor_id():
    """audit/robust/report 收的是**表达式**，不是 factor_id —— 文档里别写 <fid>。"""
    for md in (SKILL, Path("docs/agent-skill/SKILL.md")):
        text = (_repo_root() / md).read_text(encoding="utf-8")
        for sub in ("audit", "robust", "report"):
            assert not re.search(rf"lq factor {sub} [<\"]?fid", text), (
                f"{md}: `lq factor {sub}` 的第一个参数是表达式，不是 fid")


def test_skill_referenced_files_exist():
    """SKILL.md 提到的同目录文件必须存在。"""
    skill_text = (_repo_root() / SKILL).read_text(encoding="utf-8")
    for name in re.findall(r"`([a-z-]+\.md)`", skill_text):
        assert (_repo_root() / SKILL_DIR / name).exists(), f"SKILL.md 引用了不存在的 {name}"
