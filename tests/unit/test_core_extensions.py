"""core.extensions 测试：窄契约 / 版本号 / 失败隔离 / 冲突跳过 / 删目录即卸载。

刻意用「临时扩展目录 + 现写模块源码」而不是 mock importer —— 要验的正是真实
import 路径下的行为（sys.modules 缓存、相对 import、删目录后再建）。
"""

from __future__ import annotations

import dataclasses
import logging
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

from lquant.core.extensions import (
    EXTENSION_API_VERSION,
    ExtensionContext,
    ExtensionRegistrar,
    default_extensions_dir,
    load_extensions,
)

# 正常扩展用「导入核心常量」声明版本（单一真相源），正是模板推荐写法。
_HEADER = "from lquant.core.extensions import EXTENSION_API_VERSION\n"

_LOGGER = "lquant.core.extensions"


def _write_pkg_ext(root: Path, name: str, body: str, *, declare_version: bool = True) -> Path:
    """写一个包式扩展 ``<root>/<name>/__init__.py``（body 按函数内缩进书写）。"""
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(_source(body, declare_version), encoding="utf-8")
    return pkg


def _write_file_ext(root: Path, name: str, body: str, *, declare_version: bool = True) -> Path:
    """写一个单文件扩展 ``<root>/<name>.py``。"""
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.py"
    path.write_text(_source(body, declare_version), encoding="utf-8")
    return path


def _source(body: str, declare_version: bool) -> str:
    return (_HEADER if declare_version else "") + textwrap.dedent(body).lstrip("\n")


# ── 正常路径 ──────────────────────────────────────────────────────────────


def test_loads_normal_extension(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_pkg_ext(
        root,
        "demo",
        '''
        """正常扩展：注册一条路由，并在 startup 用窄上下文落一个标记文件。"""

        def setup(registrar):
            registrar.add_route("/ext/demo/ping/", handler=lambda: "pong", name="demo_ping")

        def startup(context):
            context.data_dir.mkdir(parents=True, exist_ok=True)
            (context.data_dir / "demo_ready").write_text(
                str(context.api_version), encoding="utf-8"
            )
        ''',
    )
    data_dir = tmp_path / "data"
    report = load_extensions(root, data_dir=data_dir)

    assert report.ok
    assert report.failed == ()
    assert report.skipped == ()
    assert report.names() == ["demo"]
    entry = report.registry.get("demo")
    assert entry.module == "lquant_ext_demo"
    assert [f"{r.method} {r.path}" for r in entry.routes] == ["GET /ext/demo/ping"]
    assert entry.routes[0].handler() == "pong"
    assert report.registry.route_owner() == {("GET", "/ext/demo/ping"): "demo"}
    # startup 拿到的 context 真的能用：窄契约不是空壳。
    assert (data_dir / "demo_ready").read_text(encoding="utf-8") == str(EXTENSION_API_VERSION)


def test_single_file_extension_supported(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_file_ext(
        root,
        "single",
        """
        def setup(registrar):
            registrar.add_route("/ext/single", handler=lambda: "s", method="post")
        """,
    )
    report = load_extensions(root)

    assert report.names() == ["single"]
    route = report.registry.get("single").routes[0]
    assert (route.method, route.path) == ("POST", "/ext/single")


def test_package_extension_supports_relative_import(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    pkg = _write_pkg_ext(
        root,
        "with_helper",
        """
        from . import helper

        def setup(registrar):
            registrar.add_route("/ext/helper", handler=helper.ping)
        """,
    )
    (pkg / "helper.py").write_text('def ping():\n    return "helped"\n', encoding="utf-8")

    report = load_extensions(root)

    assert report.names() == ["with_helper"]
    assert report.registry.get("with_helper").routes[0].handler() == "helped"


def test_missing_or_empty_dir_is_zero_extensions_not_failure(tmp_path: Path) -> None:
    missing = load_extensions(tmp_path / "nope")
    assert missing.ok
    assert (missing.loaded, missing.skipped, missing.failed) == ((), (), ())
    assert missing.registry.names() == []

    empty_root = tmp_path / "extensions"
    empty_root.mkdir()
    empty = load_extensions(empty_root)
    assert empty.ok
    assert empty.names() == []
    assert empty.registry.all_routes() == ()


def test_repeated_load_is_deterministic(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_pkg_ext(
        root,
        "demo",
        """
        def setup(registrar):
            registrar.add_route("/ext/demo", handler=lambda: "x")
        """,
    )
    first = load_extensions(root)
    second = load_extensions(root)

    assert first.names() == second.names()
    assert first.skipped == second.skipped
    assert first.failed == second.failed
    assert [f"{r.method} {r.path}" for r in first.registry.all_routes()] == [
        f"{r.method} {r.path}" for r in second.registry.all_routes()
    ]


# ── 版本号：不符 / 未声明 → 跳过 + warning，不抛 ─────────────────────────────


def test_version_mismatch_skips_with_warning(tmp_path: Path, caplog) -> None:
    root = tmp_path / "extensions"
    _write_pkg_ext(
        root,
        "future",
        """
        EXTENSION_API_VERSION = 999

        def setup(registrar):
            registrar.add_route("/ext/future", handler=lambda: None)
        """,
        declare_version=False,
    )

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        report = load_extensions(root)  # 不抛 = 应用照常启动

    assert report.loaded == ()
    assert report.failed == ()
    assert [s.name for s in report.skipped] == ["future"]
    assert "999" in report.skipped[0].reason
    assert str(EXTENSION_API_VERSION) in report.skipped[0].reason
    assert report.registry.names() == []
    assert any("future" in r.getMessage() for r in caplog.records)


def test_missing_version_is_fail_closed(tmp_path: Path, caplog) -> None:
    root = tmp_path / "extensions"
    _write_pkg_ext(
        root,
        "no_version",
        """
        def setup(registrar):
            registrar.add_route("/ext/nv", handler=lambda: None)
        """,
        declare_version=False,
    )

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        report = load_extensions(root)

    assert report.failed == ()
    assert [s.name for s in report.skipped] == ["no_version"]
    assert "EXTENSION_API_VERSION" in report.skipped[0].reason
    assert any("no_version" in r.getMessage() for r in caplog.records)


# ── 失败隔离：坏扩展只弄坏自己 ───────────────────────────────────────────────


def test_setup_failure_is_isolated(tmp_path: Path, caplog) -> None:
    root = tmp_path / "extensions"
    _write_pkg_ext(
        root,
        "a_ok",
        """
        def setup(registrar):
            registrar.add_route("/ext/a/ping", handler=lambda: "a")
        """,
    )
    _write_pkg_ext(
        root,
        "b_boom",
        """
        def setup(registrar):
            registrar.add_route("/ext/b/ping", handler=lambda: "b")
            raise RuntimeError("boom in setup")
        """,
    )
    _write_pkg_ext(
        root,
        "c_ok",
        """
        def setup(registrar):
            registrar.add_route("/ext/c/ping", handler=lambda: "c")
        """,
    )

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        report = load_extensions(root)  # 不抛：应用照常启动

    assert report.names() == ["a_ok", "c_ok"]  # 其他扩展照常加载
    assert [f.name for f in report.failed] == ["b_boom"]
    assert "RuntimeError" in report.failed[0].reason
    assert "boom in setup" in report.failed[0].reason
    assert not report.ok
    # b_boom 注册到一半的路由没有留下（staging 未提交）。
    assert report.registry.route_owner() == {
        ("GET", "/ext/a/ping"): "a_ok",
        ("GET", "/ext/c/ping"): "c_ok",
    }
    assert any("b_boom" in r.getMessage() for r in caplog.records)


def test_import_error_is_isolated(tmp_path: Path, caplog) -> None:
    root = tmp_path / "extensions"
    _write_pkg_ext(
        root,
        "a_ok",
        """
        def setup(registrar):
            registrar.add_route("/ext/a", handler=lambda: None)
        """,
    )
    broken = root / "b_syntax"
    broken.mkdir(parents=True)
    (broken / "__init__.py").write_text("def setup(:\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        report = load_extensions(root)

    assert report.names() == ["a_ok"]
    assert [f.name for f in report.failed] == ["b_syntax"]
    assert "SyntaxError" in report.failed[0].reason
    assert any("b_syntax" in r.getMessage() for r in caplog.records)


def test_failed_setup_rolls_back_registrations(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_pkg_ext(
        root,
        "a_partial",
        """
        def setup(registrar):
            registrar.add_route("/ext/shared", handler=lambda: "partial")
            raise RuntimeError("注册到一半就炸")
        """,
    )

    first = load_extensions(root)
    assert first.names() == []
    assert first.registry.names() == []
    assert first.registry.route_owner() == {}
    assert [f.name for f in first.failed] == ["a_partial"]

    # 再放一个用同一条路由的扩展：上一步若留下残留，这里会被判冲突而跳过。
    _write_pkg_ext(
        root,
        "b_ok",
        """
        def setup(registrar):
            registrar.add_route("/ext/shared", handler=lambda: "b")
        """,
    )
    second = load_extensions(root)
    assert second.names() == ["b_ok"]
    assert second.registry.route_owner() == {("GET", "/ext/shared"): "b_ok"}


def test_duplicate_route_inside_one_extension_fails_only_it(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_pkg_ext(
        root,
        "a_dup",
        """
        def setup(registrar):
            registrar.add_route("/ext/dup", handler=lambda: None)
            registrar.add_route("/ext/dup", handler=lambda: None)
        """,
    )
    _write_pkg_ext(
        root,
        "b_ok",
        """
        def setup(registrar):
            registrar.add_route("/ext/b", handler=lambda: None)
        """,
    )

    report = load_extensions(root)

    assert report.names() == ["b_ok"]
    assert [f.name for f in report.failed] == ["a_dup"]
    assert "重复登记" in report.failed[0].reason


# ── 名称 / 路由冲突：后者跳过 + warning ─────────────────────────────────────


def test_name_conflict_skips_later_with_warning(tmp_path: Path, caplog) -> None:
    root = tmp_path / "extensions"
    _write_pkg_ext(
        root,
        "a_first",
        """
        EXTENSION_ID = "same_id"

        def setup(registrar):
            registrar.add_route("/ext/a", handler=lambda: None)
        """,
    )
    _write_pkg_ext(
        root,
        "b_second",
        """
        EXTENSION_ID = "same_id"

        def setup(registrar):
            registrar.add_route("/ext/b", handler=lambda: None)
        """,
    )

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        report = load_extensions(root)

    assert report.names() == ["same_id"]
    assert [s.name for s in report.skipped] == ["b_second"]
    assert "same_id" in report.skipped[0].reason
    assert report.failed == ()
    assert any("b_second" in r.getMessage() for r in caplog.records)


def test_route_conflict_skips_later_with_warning(tmp_path: Path, caplog) -> None:
    root = tmp_path / "extensions"
    for dir_name in ("a_first", "b_second"):
        _write_pkg_ext(
            root,
            dir_name,
            """
            def setup(registrar):
                registrar.add_route("/ext/dup", handler=lambda: None)
            """,
        )

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        report = load_extensions(root)

    assert report.names() == ["a_first"]
    assert [s.name for s in report.skipped] == ["b_second"]
    assert "/ext/dup" in report.skipped[0].reason
    assert report.failed == ()
    assert any("b_second" in r.getMessage() for r in caplog.records)


def test_reserved_name_blocks_extension(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_pkg_ext(
        root,
        "clash",
        """
        EXTENSION_ID = "core_owned"

        def setup(registrar):
            registrar.add_route("/ext/clash", handler=lambda: None)
        """,
    )

    report = load_extensions(root, reserved_names=["core_owned"])

    assert report.names() == []
    assert [s.name for s in report.skipped] == ["clash"]
    assert "保留名" in report.skipped[0].reason


def test_reserved_route_blocks_extension(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_pkg_ext(
        root,
        "route_clash",
        """
        def setup(registrar):
            registrar.add_route("/api/health", handler=lambda: None, method="post")
        """,
    )

    report = load_extensions(root, reserved_routes=[("POST", "/api/health")])

    assert report.names() == []
    assert [s.name for s in report.skipped] == ["route_clash"]
    assert "保留路由" in report.skipped[0].reason


# ── 删目录即卸载 ──────────────────────────────────────────────────────────


def test_delete_dir_unloads_and_leaves_no_residue(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    pkg = _write_pkg_ext(
        root,
        "demo",
        """
        def setup(registrar):
            registrar.add_route("/ext/demo", handler=lambda: "v1")
        """,
    )
    assert load_extensions(root).names() == ["demo"]

    # 目录内容改了 → 装载结果必须跟着改，不能拿 sys.modules 里的旧模块。
    (pkg / "__init__.py").write_text(
        _source(
            """
            def setup(registrar):
                registrar.add_route("/ext/demo", handler=lambda: "v2")
            """,
            True,
        ),
        encoding="utf-8",
    )
    reloaded = load_extensions(root)
    assert reloaded.registry.get("demo").routes[0].handler() == "v2"

    # 删目录 = 卸载：报告、注册表、模块缓存里都不该再有它。
    shutil.rmtree(pkg)
    after = load_extensions(root)
    assert after.names() == []
    assert after.registry.names() == []
    assert after.registry.all_routes() == ()
    assert after.skipped == ()
    assert after.failed == ()
    assert after.ok
    assert "demo" not in after.registry
    assert "lquant_ext_demo" not in sys.modules

    # 再建回来（换一条路由）→ 干净地重新装载，不与历史注册冲突。
    _write_pkg_ext(
        root,
        "demo",
        """
        def setup(registrar):
            registrar.add_route("/ext/demo/v3", handler=lambda: "v3")
        """,
    )
    again = load_extensions(root)
    assert again.names() == ["demo"]
    assert again.registry.route_owner() == {("GET", "/ext/demo/v3"): "demo"}


def test_template_reference_extension_loads_and_unloads(tmp_path: Path) -> None:
    """模板目录本身不自动装载；复制改名后是正常扩展，删掉即消失且无残留。"""
    template = default_extensions_dir() / "_template"
    assert template.is_dir(), f"参考实现缺失：{template}"

    root = tmp_path / "extensions"
    root.mkdir()
    shutil.copytree(template, root / "template_demo")

    first = load_extensions(root)
    assert first.ok
    assert first.names() == ["template_demo"]
    assert [f"{r.method} {r.path}" for r in first.registry.get("template_demo").routes] == [
        "GET /ext/demo/hello"
    ]

    shutil.rmtree(root / "template_demo")
    after = load_extensions(root)
    assert after.names() == []
    assert after.registry.names() == []
    assert "lquant_ext_template_demo" not in sys.modules


def test_underscore_dirs_are_not_scanned(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_pkg_ext(
        root,
        "_template",
        """
        def setup(registrar):
            raise RuntimeError("下划线目录不该被装载")
        """,
    )
    _write_pkg_ext(
        root,
        "real_one",
        """
        def setup(registrar):
            registrar.add_route("/ext/real", handler=lambda: None)
        """,
    )

    report = load_extensions(root)

    assert report.names() == ["real_one"]
    assert report.failed == ()  # _template 被跳过，不是失败


# ── 契约必须窄 ────────────────────────────────────────────────────────────


def test_context_is_deliberately_narrow() -> None:
    ctx = ExtensionContext(
        api_version=EXTENSION_API_VERSION, data_dir=Path("/tmp/lq-ext"), repository=None
    )

    assert ctx.api_version == EXTENSION_API_VERSION
    assert ctx.repository is None
    assert {f.name for f in dataclasses.fields(ctx)} == {"api_version", "data_dir", "repository"}

    for forbidden in (
        "app",
        "settings",
        "service",
        "server",
        "db",
        "config",
        "registry",
        "router",
        "logger",
    ):
        assert not hasattr(ctx, forbidden), f"窄契约不该暴露 {forbidden}"

    # frozen + slots：改不了字段，也塞不进新字段（防止后续被「顺手」加宽）。
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.data_dir = Path("/tmp/other")  # type: ignore[misc]
    with pytest.raises(AttributeError):
        ctx.app = object()  # type: ignore[attr-defined]


def test_registrar_is_a_declaration_surface_only() -> None:
    registrar = ExtensionRegistrar("demo", EXTENSION_API_VERSION)

    assert registrar.name == "demo"
    assert registrar.routes == ()
    for forbidden in ("app", "context", "settings", "commit", "service"):
        assert not hasattr(registrar, forbidden)
    with pytest.raises(AttributeError):
        registrar.app = object()  # type: ignore[attr-defined]
    with pytest.raises(ValueError):
        registrar.add_route("ext/no-slash", handler=lambda: None)


def test_load_rejects_mismatched_context_api_version(tmp_path: Path) -> None:
    ctx = ExtensionContext(
        api_version=EXTENSION_API_VERSION + 1, data_dir=tmp_path, repository=None
    )
    with pytest.raises(ValueError, match="契约版本"):
        load_extensions(tmp_path / "extensions", context=ctx)


# ── 剩余契约面：非法声明 / 默认目录 / 自省 ────────────────────────────────────


def test_registrar_validation_fails_loudly() -> None:
    registrar = ExtensionRegistrar("demo", EXTENSION_API_VERSION)

    with pytest.raises(ValueError, match="以 / 开头"):
        registrar.add_route("no-slash", handler=lambda: None)
    with pytest.raises(ValueError, match="空白"):
        registrar.add_route("/a b", handler=lambda: None)
    with pytest.raises(ValueError, match="可调用"):
        registrar.add_route("/ok", handler="not-callable")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="method"):
        registrar.add_route("/ok", handler=lambda: None, method="   ")
    assert registrar.routes == ()  # 非法声明不留 staging


def test_missing_setup_fails_loudly(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_pkg_ext(
        root,
        "no_setup",
        """
        VALUE = 1
        """,
    )

    report = load_extensions(root)

    assert report.names() == []
    assert [f.name for f in report.failed] == ["no_setup"]
    assert "setup" in report.failed[0].reason


def test_non_identifier_dir_name_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_pkg_ext(
        root,
        "bad-name",
        """
        def setup(registrar):
            registrar.add_route("/ext/bad", handler=lambda: None)
        """,
    )

    report = load_extensions(root)

    assert report.names() == []
    assert [s.name for s in report.skipped] == ["bad-name"]
    assert "标识符" in report.skipped[0].reason
    assert report.failed == ()


def test_relative_root_is_anchored_to_repo_root() -> None:
    from lquant.core.config import find_root

    report = load_extensions("extensions")  # 相对路径必须锚定仓库根

    assert report.root == (find_root() / "extensions").resolve()


def test_data_dir_comes_from_env_then_repo_data(tmp_path: Path, monkeypatch) -> None:
    from lquant.core.config import find_root

    root = tmp_path / "extensions"
    _write_pkg_ext(
        root,
        "cap",
        """
        seen = []

        def setup(registrar):
            registrar.add_route("/ext/cap", handler=lambda: None)

        def startup(context):
            seen.append(context)
        """,
    )

    monkeypatch.setenv("LQ_DATA_DIR", "rel/data")
    load_extensions(root)
    assert sys.modules["lquant_ext_cap"].seen[-1].data_dir == (find_root() / "rel/data").resolve()

    monkeypatch.delenv("LQ_DATA_DIR", raising=False)
    load_extensions(root)
    ctx = sys.modules["lquant_ext_cap"].seen[-1]
    assert ctx.data_dir == find_root() / "data"
    assert ctx.repository is None


def test_registry_and_report_introspection(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_pkg_ext(
        root,
        "a_ok",
        """
        def setup(registrar):
            registrar.add_route("/ext/ok", handler=lambda: None)
            registrar.add_route("/ext/ok2", handler=lambda: None, method="post")
        """,
    )
    _write_pkg_ext(
        root,
        "b_bad",
        """
        def setup(registrar):
            raise RuntimeError("bad")
        """,
    )
    _write_pkg_ext(
        root,
        "c_old",
        """
        EXTENSION_API_VERSION = 7

        def setup(registrar):
            pass
        """,
        declare_version=False,
    )

    report = load_extensions(root)
    registry = report.registry

    assert len(registry) == 1
    assert list(registry) == ["a_ok"]
    assert "a_ok" in registry
    assert "b_bad" not in registry
    assert registry.describe() == [
        {
            "name": "a_ok",
            "module": "lquant_ext_a_ok",
            "api_version": EXTENSION_API_VERSION,
            "path": str(root / "a_ok" / "__init__.py"),
            "routes": ["GET /ext/ok", "POST /ext/ok2"],
        }
    ]
    with pytest.raises(KeyError, match="未装载"):
        registry.get("nope")

    payload = report.to_dict()
    assert payload["ok"] is False
    assert [e["name"] for e in payload["loaded"]] == ["a_ok"]
    assert payload["skipped"] == [
        {"name": "c_old", "reason": f"契约版本 7 != 核心支持的 {EXTENSION_API_VERSION}"}
    ]
    assert payload["failed"][0]["name"] == "b_bad"
    assert "RuntimeError: bad" in payload["failed"][0]["reason"]

    text = report.summary()
    assert "loaded(1) / skipped(1) / failed(1)" in text
    assert "+ a_ok api=1" in text
    assert "- c_old: 契约版本 7" in text
    assert "! b_bad: RuntimeError: bad" in text


def test_cli_lists_extensions(tmp_path: Path) -> None:
    """可选入口：``lq extensions list --root <dir>`` 只读地打出装载报告。"""
    from click.testing import CliRunner

    from lquant.cli.commands.extensions import extensions

    root = tmp_path / "extensions"
    _write_pkg_ext(
        root,
        "demo",
        """
        def setup(registrar):
            registrar.add_route("/ext/demo", handler=lambda: None)
        """,
    )
    _write_pkg_ext(
        root,
        "boom",
        """
        def setup(registrar):
            raise RuntimeError("bad")
        """,
    )

    result = CliRunner().invoke(extensions, ["list", "--root", str(root)])

    assert "loaded(1) / skipped(0) / failed(1)" in result.output
    assert "+ demo" in result.output
    assert "! boom: RuntimeError: bad" in result.output
    # 「被隔离」不等于「应用起不来」，但退出码要如实反映，脚本才能发现坏扩展。
    assert result.exit_code == 1
