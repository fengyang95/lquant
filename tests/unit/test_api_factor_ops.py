"""因子编辑画布的三个后端接口：算子目录 / 字段清单 / 表达式 AST。

画布把「算子语义」完全交给服务端（唯一真相源），所以这三个接口的正确性
就是画布口径的正确性 —— 尤其要钉死两件事：
  1. 元数自省（series_arity）不能错，错了画布会生成元数不对的调用；
  2. `/ops`、`/fields` 不能被 `/{name}` 路由捕获（路由顺序回归）。
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("LQ_SYNC_WORKER", "0")


# ---------------- 纯单元：自省与目录 ----------------

def test_op_catalog_arity_and_params():
    from lquant.factors.ops.catalog import op_catalog

    by_name = {o["name"]: o for o in op_catalog()}
    assert len(by_name) >= 34, "算子目录不应少于既有 34 个内置算子"

    # 元数：一序列 / 二序列 / 三序列
    assert by_name["Rank"]["series_arity"] == 1
    assert by_name["Ts_Mean"]["series_arity"] == 1
    assert by_name["Ts_Corr"]["series_arity"] == 2
    assert by_name["Greater"]["series_arity"] == 2
    assert by_name["If"]["series_arity"] == 3

    # 窗口参数：名为 n 的标量 → type=window，且无默认值（由前端给默认）
    ts_mean_params = {p["name"]: p for p in by_name["Ts_Mean"]["params"]}
    assert ts_mean_params["n"]["type"] == "window"
    assert ts_mean_params["n"]["required"] is True
    assert ts_mean_params["n"]["default"] is None

    # 额外标量参数：Ts_Quantile 的 q 有默认 0.8，类型是普通数值
    q_params = {p["name"]: p for p in by_name["Ts_Quantile"]["params"]}
    assert q_params["q"]["type"] == "number"
    assert q_params["q"]["required"] is False
    assert q_params["q"]["default"] == pytest.approx(0.8)

    # 元数据透传：category 与中文 label 必须来自注册表原文
    assert by_name["Ts_Mean"]["category"] == "TS"
    assert by_name["Rank"]["category"] == "CS"
    assert by_name["Abs"]["category"] == "EL"
    assert by_name["Ts_Mean"]["label"] == "时序均值"


def test_op_catalog_labels_are_server_truth():
    """画布照抄服务端 label —— 语义分歧（Greater 取大 vs 比较）靠这条不漂移。"""
    from lquant.factors.ops.catalog import op_catalog

    by_name = {o["name"]: o for o in op_catalog()}
    # Greater 在 lquant 是「逐元素取大」，不是比较；label 必须如实反映
    assert "取大" in by_name["Greater"]["label"]
    # Ts_ArgMax 的 0 基准必须写在 label 里（与 yinzi 画布的 1 基准不同）
    assert "0=" in by_name["Ts_ArgMax"]["label"] or "0" in by_name["Ts_ArgMax"]["label"]


def test_introspect_unknown_signature_returns_sentinel(monkeypatch):
    """拿不到签名时必须是哨兵，不能是 0 —— 0 会被画布当成零参算子。"""
    from lquant.factors.ops import catalog as catalog_mod

    def boom(*_args, **_kwargs):
        raise ValueError("no signature")

    monkeypatch.setattr(catalog_mod.inspect, "signature", boom)
    assert catalog_mod._introspect(lambda x: x) == (catalog_mod.UNKNOWN_ARITY, [])


def test_introspect_rejects_scalar_before_series():
    """画布按「序列在前、标量在后」拼位置实参；签名不符就标成不可用。"""
    import polars as pl

    from lquant.factors.ops.catalog import UNKNOWN_ARITY, _introspect

    def weird(n: int, x: pl.Expr) -> pl.Expr:  # noqa: ARG001
        return x

    assert _introspect(weird) == (UNKNOWN_ARITY, [])


def test_every_registered_op_is_introspectable():
    """现有算子必须全部可自省（序列在前、标量在后），否则画布会拒用它们。"""
    from lquant.factors.ops.catalog import op_catalog

    unusable = [o["name"] for o in op_catalog() if o["series_arity"] < 0]
    assert unusable == [], f"这些算子无法被画布使用: {unusable}"


def test_infix_catalog_is_grammar_truth():
    """中缀算子是 parser 内建的语法，不在 OPS 注册表 —— 但仍由服务端声明。

    前端画布靠它才知道只有 `+ - * / < >`，没有 `<=`/`==`/`%` 这些想当然的算子。
    """
    from lquant.factors.ops.catalog import infix_catalog, op_catalog

    infix = infix_catalog()
    assert [(i["token"], i["arity"]) for i in infix] == [
        ("+", 2), ("-", 2), ("*", 2), ("/", 2), (">", 2), ("<", 2), ("-", 1),
    ]
    assert all(i["label"] for i in infix)
    # 两边不重叠：中缀 token 不应出现在函数式算子目录里
    assert not ({i["token"] for i in infix} & {o["name"] for o in op_catalog()})


def test_field_catalog_excludes_keys_and_vwap():
    from lquant.factors.fields import field_catalog

    names = [f["name"] for f in field_catalog()]
    assert "close" in names and "float_mv" in names
    # 主键列不是因子输入；vwap 不在日线白名单里（引擎不认，画布就不能给）
    assert "trade_date" not in names
    assert "symbol" not in names
    assert "vwap" not in names
    # 每个字段都有中文名
    assert all(f["label"] for f in field_catalog())


def test_daily_fields_single_source():
    """校验侧白名单与字段目录同源，不允许两处硬编码漂移。"""
    from lquant.factors.fields import DAILY_FIELDS, NUMERIC_FIELDS, daily_fields
    from lquant.factors.mining.submit import _daily_fields

    assert _daily_fields() == daily_fields() == set(DAILY_FIELDS)
    assert set(NUMERIC_FIELDS).issubset(set(DAILY_FIELDS))


def test_ast_to_dict_shapes():
    from lquant.factors.dsl.json_ast import to_dict
    from lquant.factors.dsl.parser import parse

    root = parse("Rank(Ts_Mean($close,5)/$close-1)", "t").root
    tree = to_dict(root)
    assert tree["kind"] == "call" and tree["name"] == "Rank"
    inner = tree["args"][0]
    assert inner["kind"] == "binary" and inner["op"] == "-"
    assert inner["left"]["kind"] == "binary" and inner["left"]["op"] == "/"
    assert inner["right"]["kind"] == "number" and inner["right"]["value"] == pytest.approx(1.0)

    # 一元负号与字段
    neg = to_dict(parse("-$open", "t").root)
    assert neg == {"kind": "unary", "op": "-", "arg": {"kind": "field", "name": "open"}}


def test_unary_minus_is_canonical_stable():
    """画布必须支持一元负号的依据。

    `canonical` 不把 `0-x` 折叠成 `-x`，所以「打开已有因子时把 `-$close`
    改写成 `(0-$close)`」会破坏往返稳定性。支持一元负号节点才是无损的。
    """
    from lquant.factors.dsl.parser import parse
    from lquant.factors.dsl.printer import canonical, canonical_id

    assert canonical(parse("-$close", "t").root) == "-$close"
    # 画布输出的带括号形式与原式同一口径
    assert canonical(parse("(-$close)", "t").root) == "-$close"
    assert canonical_id("-$close") == canonical_id("(-$close)")
    # 反例：0-x 改写确实不等价于原口径
    assert canonical(parse("0-$close", "t").root) != "-$close"


def test_ast_to_dict_rejects_unknown_node():
    from lquant.factors.dsl.json_ast import to_dict

    with pytest.raises(TypeError):
        to_dict(object())  # type: ignore[arg-type]


# ---------------- 接口层：路由与响应 ----------------

@pytest.fixture(scope="module")
def api_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("api_factor_ops")
    prev_cwd = os.getcwd()
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    from lquant.core.db import writer
    from lquant.data.ingest.demo import generate_demo
    from lquant.data.store.ddl import DDL_STATEMENTS, ensure_factor_def_columns

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
        ensure_factor_def_columns(con)
    generate_demo(start="2025-01-01", end="2025-06-30")
    yield base
    os.chdir(prev_cwd)
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(api_env):
    from lquant.server.main import create_app

    with TestClient(create_app()) as c:
        yield c


def test_ops_endpoint_not_shadowed_by_name_route(client):
    """回归：/factors/ops 必须命中目录接口，不能被 /factors/{name} 吃掉。"""
    r = client.get("/api/factors/ops")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, dict)
    ops = body["ops"]
    assert isinstance(ops, list) and len(ops) >= 34
    assert {"name", "category", "label", "series_arity", "params"} <= set(ops[0])
    # 中缀算子也要一并下发，否则画布搭不出四则与比较
    assert [(i["token"], i["arity"]) for i in body["infix"]] == [
        ("+", 2), ("-", 2), ("*", 2), ("/", 2), (">", 2), ("<", 2), ("-", 1),
    ]


def test_fields_endpoint(client):
    r = client.get("/api/factors/fields")
    assert r.status_code == 200
    names = [f["name"] for f in r.json()]
    assert "close" in names and "vwap" not in names


def test_ast_endpoint_ok_and_error(client):
    ok = client.post("/api/factors/ast", json={"expression": "Rank(Ts_Mean($close,5)/$close-1)"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["ast"]["kind"] == "call" and body["ast"]["name"] == "Rank"

    bad = client.post("/api/factors/ast", json={"expression": "Nope($close)"})
    assert bad.status_code == 422
    assert "DSL 解析失败" in bad.json()["detail"]

    empty = client.post("/api/factors/ast", json={"expression": "   "})
    assert empty.status_code == 422


def test_every_catalog_op_roundtrips_through_validate(client):
    """目录里每个算子按自省出的元数拼一个调用，必须能过 /factors/validate。

    这条是画布的核心契约：目录驱动生成的表达式与引擎口径一致。
    """
    body = client.get("/api/factors/ops").json()
    for op in body["ops"]:
        args = ["$close"] * op["series_arity"]
        args += ["5" if p["type"] == "window" else str(p["default"] or 1)
                 for p in op["params"]]
        expr = f"{op['name']}({','.join(args)})"
        r = client.post("/api/factors/validate", json={"expression": expr})
        assert r.status_code == 200
        assert r.json()["ok"] is True, f"{expr} 校验失败: {r.json()['error']}"

    # 中缀算子同样要能过校验 —— 画布的四则/比较/取反积木靠它们
    for item in body["infix"]:
        expr = ("(-$close)" if item["arity"] == 1
                else f"($close {item['token']} $open)")
        r = client.post("/api/factors/validate", json={"expression": expr})
        assert r.status_code == 200
        assert r.json()["ok"] is True, f"{expr} 校验失败: {r.json()['error']}"


# ---------------- 历史 qlib 写法的兼容归一（打开 BETA10 那类老因子） ----------------

def test_normalize_unit():
    """normalize：DSL 原样返回；qlib 写法翻译；两者都不是则保留原始 DSL 报错。"""
    from lquant.core.errors import DSLParseError, FactorError
    from lquant.factors.dsl.normalize import normalize

    assert normalize("Rank(Ts_Mean($close,5)/$close-1)") == "Rank(Ts_Mean($close,5)/$close-1)"
    assert normalize("Slope($close,10)/$close") == "Ts_Slope($close,10)/$close"
    assert normalize("Mean($close,20)/$close") == "Ts_Mean($close,20)/$close"
    assert normalize("  ") == ""

    # 同名不同义：lquant 的 Rank(x) 是截面秩（1 参），qlib 的 Rank(x,n) 是时序秩
    # （2 参）。静态检查只认名字不认元数，必须靠元数把两者分开，否则老 RANK 因子
    # 会被当成截面秩，求值时炸 `rank() takes 1 positional argument`。
    assert normalize("Rank($close)") == "Rank($close)"
    assert normalize("Rank($close,5)") == "Ts_Rank($close,5)"
    assert normalize("Rank(Ts_Mean($close,5)/$close-1)") == "Rank(Ts_Mean($close,5)/$close-1)"

    # 可选参数：省略 / 给出都要接受
    assert normalize("Ts_Quantile($close,5)") == "Ts_Quantile($close,5)"
    assert normalize("Ts_Quantile($close,5,0.8)") == "Ts_Quantile($close,5,0.8)"

    # 元数写错要静态报错，而不是拖到求值才炸 TypeError（那是 500 不是 422）
    with pytest.raises(FactorError, match="参数个数不对"):
        normalize("Ts_Mean($close)")

    # qlib 专有字段 $vwap 在 lquant 面板里没有列 → 译成 amount/volume 代理
    assert normalize("$vwap/$close") == "$amount/$volume/$close"

    # 字段白名单**不在这里**校验：引擎会用真实面板列校验（合成路径会引用
    # `f` 这类派生列），这里只保证语法/算子/元数成立。
    assert normalize("Rank($no_such_col)") == "Rank($no_such_col)"

    # 未注册算子要报**DSL**的错，而不是 qlib 翻译的错 —— 否则用户会以为
    # 自己写的是 qlib 公式
    with pytest.raises(FactorError, match="未注册算子: Nope"):
        normalize("Nope($close)")
    with pytest.raises(DSLParseError):
        normalize("1 + ")

    # fail-soft（列表 / 详情这类展示路径）：坏表达式不抛，原样返回
    from lquant.factors.dsl.normalize import normalize_soft

    assert normalize_soft("Nope($close)") == "Nope($close)"
    assert normalize_soft("  ") == ""


def test_ast_endpoint_translates_legacy_qlib(client):
    """打开 BETA10：库里存的是 qlib 写法，/ast 必须先翻译再出树。

    回归：此前直接 parse+check，Slope 未注册 → 422，画布永远打不开。
    """
    r = client.post("/api/factors/ast", json={"expression": "Slope($close,10)/$close"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["translated"] is True
    assert body["expression"] == "Ts_Slope($close,10)/$close"
    assert body["ast"]["kind"] == "binary" and body["ast"]["op"] == "/"
    assert body["ast"]["left"]["name"] == "Ts_Slope"

    # 已经是 DSL 的表达式不标 translated
    ok = client.post("/api/factors/ast", json={"expression": "Ts_Slope($close,10)/$close"})
    assert ok.status_code == 200
    assert ok.json()["translated"] is False


def test_validate_endpoint_translates_legacy_qlib(client):
    """历史 qlib 写法也要能过校验（此前 Slope 未注册 → ok=False）。"""
    r = client.post("/api/factors/validate", json={"expression": "Slope($close,10)/$close"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "error": None}

    # 响应形状保持 {ok, error}：不接受 qlib 的坏算子仍如实报错
    bad = client.post("/api/factors/validate", json={"expression": "Nope($close)"})
    assert bad.status_code == 200
    assert bad.json()["ok"] is False
    assert "未注册算子: Nope" in bad.json()["error"]


def test_register_and_read_normalize_legacy_qlib(client):
    """落库即归一：注册 qlib 写法，读回来必须是 DSL（列表 / 详情同口径）。"""
    r = client.post("/api/factors", json={
        "name": "legacy_beta10", "expression": "Slope($close,10)/$close",
        "description": "历史 qlib 写法"})
    assert r.status_code == 200, r.text

    detail = client.get("/api/factors/legacy_beta10").json()
    assert detail["expression"] == "Ts_Slope($close,10)/$close"

    listed = {f["name"]: f for f in client.get("/api/factors?limit=500").json()}
    assert listed["legacy_beta10"]["expression"] == "Ts_Slope($close,10)/$close"
