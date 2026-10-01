"""jq_import 静态扫描单元测试。"""

from __future__ import annotations

from lquant.research.dialect import jq_import
from lquant.research.dialect.mapping import UNSUPPORTED, WARNINGS


def _write(tmp_path, code: str):
    p = tmp_path / "strategy.py"
    p.write_text(code, encoding="utf-8")
    return str(p)


def test_clean_file(tmp_path):
    p = _write(tmp_path, "def on_bar(ctx):\n    return 1\n")
    assert jq_import.scan(p) == []


def test_unsupported_call_reported(tmp_path):
    p = _write(tmp_path, "def go():\n    run_daily(9)\n")
    probs = jq_import.scan(p)
    assert probs == ["L2: run_daily() 暂不支持（需改写成原生 on_bar）"]


def test_warning_call_reported(tmp_path):
    p = _write(tmp_path, "set_benchmark('000300.SH')\n")
    probs = jq_import.scan(p)
    assert probs == [
        f"L1: [警告] set_benchmark — {WARNINGS['set_benchmark']}"
    ]


def test_mixed_and_method_call_ignored(tmp_path):
    code = (
        "def go():\n"
        "    get_current_data()\n"
        "    obj.get_current_data()  # 方法调用不是 Name 调用，跳过\n"
        "    attribute_history('600519', 10)\n"
        "    other = 1\n"
    )
    probs = jq_import.scan(tmp_path and _write(tmp_path, code))
    # run 级别：unsupported + warning 两条；方法调用不报
    assert len(probs) == 2
    assert any(p.startswith("L2:") for p in probs)
    assert any(p.startswith("L4:") for p in probs)


def test_unsupported_names_covered(tmp_path):
    # 每个 UNSUPPORTED 名字都能被扫出
    for name in UNSUPPORTED:
        p = _write(tmp_path, f"{name}()\n")
        probs = jq_import.scan(p)
        assert len(probs) == 1 and name in probs[0]


def test_main_reports_problems(tmp_path, capsys):
    p = _write(tmp_path, "run_daily(1)\n")
    assert jq_import.main(["prog", p]) == 0
    out = capsys.readouterr().out
    assert p in out and "run_daily" in out


def test_main_clean_file_silent(tmp_path, capsys):
    p = _write(tmp_path, "x = 1\n")
    assert jq_import.main(["prog", p]) == 0
    assert capsys.readouterr().out == ""
