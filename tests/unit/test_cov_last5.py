"""最后冲刺第五波：cli paper 全命令（service 层打桩）。"""
from __future__ import annotations

import json

from click.testing import CliRunner

import lquant.cli.commands.paper as paper_cli


def _run(args, mp):
    runner = CliRunner()
    return runner.invoke(paper_cli.paper, args, obj={})


def test_paper_cli_all_commands(monkeypatch):
    calls = []

    def fake(fn, ret):
        def _f(*a, **kw):
            calls.append(fn)
            return ret

        return _f

    monkeypatch.setattr("lquant.paper.service.create_account",
                        fake("create", {"name": "a"}))
    monkeypatch.setattr("lquant.paper.service.submit_order",
                        fake("order", {"order_id": "P1"}))
    monkeypatch.setattr("lquant.paper.service.tick", fake("tick", {"nav": 1.0}))
    monkeypatch.setattr("lquant.paper.service.cancel_order",
                        fake("cancel", {"canceled": True}))
    monkeypatch.setattr("lquant.paper.service.day_close",
                        fake("close", {"date": "2026-09-17"}))
    monkeypatch.setattr("lquant.paper.service.status",
                        fake("status", {"nav": 1.0}))
    monkeypatch.setattr("lquant.paper.service.nav_history",
                        fake("nav", [{"nav": 1.0}]))
    r = CliRunner().invoke(paper_cli.paper, [
        "create", "a", "--cash", "100000", "--universe", "600000.SH,510300.SH",
    ])
    assert r.exit_code == 0 and json.loads(r.output)["name"] == "a"
    r = CliRunner().invoke(paper_cli.paper, [
        "order", "a", "--symbol", "600000.SH", "--side", "buy", "--qty", "100"])
    assert r.exit_code == 0
    r = CliRunner().invoke(paper_cli.paper, ["tick", "a"])
    assert r.exit_code == 0
    r = CliRunner().invoke(paper_cli.paper, [
        "cancel", "a", "--order-id", "P1"])
    assert r.exit_code == 0
    r = CliRunner().invoke(paper_cli.paper, ["close", "a", "--date", "2026-09-17"])
    assert r.exit_code == 0
    r = CliRunner().invoke(paper_cli.paper, ["status", "a"])
    assert r.exit_code == 0
    r = CliRunner().invoke(paper_cli.paper, ["nav", "a", "--source", "official"])
    assert r.exit_code == 0
    assert set(calls) == {"create", "tick", "order", "cancel", "close",
                          "status", "nav"}


def test_paper_cli_default_source_omitted(monkeypatch):
    seen = {}

    def fake_nav(name, source):
        seen["source"] = source
        return []

    monkeypatch.setattr("lquant.paper.service.nav_history", fake_nav)
    r = CliRunner().invoke(paper_cli.paper, ["nav", "a"])
    assert r.exit_code == 0 and seen["source"] is None
