"""validate_source：语法 / import 白名单 / 必须定义 initialize。"""
from lquant.backtest.validation import validate_source

GOOD = """
def initialize(context):
    set_benchmark('000300.SH')

def handle_data(context, data):
    order('600519.SH', 100)
"""

def test_good_code_passes():
    assert validate_source(GOOD) == []

def test_syntax_error_reports_line():
    errs = validate_source("def initialize(context)\n    pass")
    assert any("行 1" in e for e in errs)

def test_disallowed_import():
    errs = validate_source(GOOD + "\nimport os\n")
    assert any("import" in e.lower() and "os" in e for e in errs)

def test_import_inside_function_also_checked():
    errs = validate_source(GOOD + "\ndef f():\n    import subprocess\n")
    assert any("subprocess" in e for e in errs)

def test_missing_initialize():
    errs = validate_source("def handle_data(context, data):\n    pass")
    assert any("initialize" in e for e in errs)

def test_require_initialize_false():
    src = "def analyze(result):\n    return []\n"
    assert validate_source(src, require_initialize=False) == []
