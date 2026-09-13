"""core.env 加载器测试：不覆盖已有 env、多文件优先级、坏行容忍。"""
from __future__ import annotations

import os

import pytest

from lquant.core.env import discover_env_files, load_env_files


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # 本文件专门测 .env 加载：解除 conftest 的全局跳过
    monkeypatch.delenv("LQ_ENV_SKIP", raising=False)
    monkeypatch.setattr("lquant.core.env._LOADED", set())
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LQ_TEST_KEY", raising=False)
    monkeypatch.delenv("LQ_TEST_OTHER", raising=False)


def test_loads_key_value_and_ignores_comments(tmp_path) -> None:
    p = tmp_path / ".env"
    p.write_text(
        "# comment\nLQ_TEST_KEY=abc\n\nexport LQ_TEST_OTHER='x y'\nBADLINE\n=\n",
        encoding="utf-8",
    )
    loaded = load_env_files(p)
    assert loaded == [str(p.resolve())]
    assert os.environ["LQ_TEST_KEY"] == "abc"
    assert os.environ["LQ_TEST_OTHER"] == "x y"


def test_existing_env_wins(tmp_path) -> None:
    (tmp_path / ".env").write_text("LQ_TEST_KEY=from_file\n", encoding="utf-8")
    os.environ["LQ_TEST_KEY"] = "from_env"
    load_env_files(tmp_path / ".env")
    assert os.environ["LQ_TEST_KEY"] == "from_env"


def test_first_file_wins(tmp_path) -> None:
    a = tmp_path / "a.env"
    b = tmp_path / "b.env"
    a.write_text("LQ_TEST_KEY=a\n", encoding="utf-8")
    b.write_text("LQ_TEST_KEY=b\nLQ_TEST_OTHER=b\n", encoding="utf-8")
    load_env_files(a, b)
    assert os.environ["LQ_TEST_KEY"] == "a"
    assert os.environ["LQ_TEST_OTHER"] == "b"  # 第一个文件没设的 key 继续生效


def test_missing_file_skipped(tmp_path) -> None:
    assert load_env_files(tmp_path / "nope.env") == []


def test_same_file_only_parsed_once(tmp_path) -> None:
    p = tmp_path / ".env"
    p.write_text("LQ_TEST_KEY=abc\n", encoding="utf-8")
    load_env_files(p)
    os.environ.pop("LQ_TEST_KEY")
    load_env_files(p)  # 二次调用被 _LOADED 短路，不会重新写入
    assert "LQ_TEST_KEY" not in os.environ


def test_discover_cwd_first(tmp_path) -> None:
    got = discover_env_files()
    assert got[0] == tmp_path / ".env"
