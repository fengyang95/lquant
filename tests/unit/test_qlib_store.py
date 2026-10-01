"""qlib_run sqlite 存储测试。"""
import pytest

from lquant.qlib_io import store


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch, tmp_path):
    monkeypatch.setenv("LQ_QLIB_DB", str(tmp_path / "qlib.db"))


def test_create_and_get():
    r = store.create_run("config/qlib/a.yaml", "top300", "exp1", "yaml-text")
    assert r["status"] == "queued" and r["metrics"] is None
    assert store.get_run(r["id"])["config"] == "config/qlib/a.yaml"


def test_update_lifecycle():
    r = store.create_run("a.yaml", None, "exp1", "y")
    store.update_run(r["id"], status="running")
    store.update_run(r["id"], status="finished",
                     metrics={"IC": 0.03}, log_path="/tmp/log.txt")
    got = store.get_run(r["id"])
    assert got["status"] == "finished"
    assert got["metrics"] == {"IC": 0.03}
    assert got["finished_at"]


def test_update_failed_sets_error():
    r = store.create_run("a.yaml", None, "exp", "y")
    store.update_run(r["id"], status="failed", error="boom")
    assert store.get_run(r["id"])["error"] == "boom"


def test_update_missing_raises():
    with pytest.raises(ValueError):
        store.update_run("nope", status="running")


def test_list_runs_filter_and_order():
    a = store.create_run("a.yaml", None, "e", "y")
    b = store.create_run("b.yaml", None, "e", "y")
    store.update_run(a["id"], status="finished")
    assert [x["id"] for x in store.list_runs(status="finished")] == [a["id"]]
    ids = [x["id"] for x in store.list_runs()]
    assert set(ids) == {a["id"], b["id"]}


def test_get_missing_none():
    assert store.get_run("nope") is None
