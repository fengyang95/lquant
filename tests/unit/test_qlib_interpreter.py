"""qlib 解释器探测公共函数测试。"""
from unittest import mock

from lquant.qlib_io.interpreter import find_qlib_python


def test_explicit_python_wins(tmp_path):
    p = tmp_path / "py"
    p.write_text("")
    assert find_qlib_python(str(p)) == str(p)


def test_env_var(monkeypatch, tmp_path):
    p = tmp_path / "envpy"
    p.write_text("")
    monkeypatch.setenv("LQ_QLIB_PYTHON", str(p))
    assert find_qlib_python(None) == str(p)


def test_inprocess_import(monkeypatch):
    monkeypatch.delenv("LQ_QLIB_PYTHON", raising=False)
    import builtins

    real = builtins.__import__
    fake = mock.MagicMock()

    def fake_import(name, *a, **k):
        if name == "qlib":
            return fake
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert find_qlib_python(None) is None


def test_not_found(monkeypatch, tmp_path):
    monkeypatch.delenv("LQ_QLIB_PYTHON", raising=False)
    monkeypatch.chdir(tmp_path)
    assert find_qlib_python(None) == ""
