"""研报 → 因子提案（借鉴 RD-Agent 的「研报 → 因子假设」自动化环节）。

RD-Agent（Microsoft）把「读研报 → 提出因子假设 → 形式化 → 回测验证」做成
自动循环。本模块取其中**最小可行的一环**：研报/资讯文本 → LLM → lquant DSL
因子提案 JSONL → 接入既有 ``lq factor mine --generator proposals`` 门禁管线。

**LLM 只产表达式，计算与验证全归平台**（与 ``factors/mining/llm.py`` 同一哲学）：
  1. prompt 动态注入真实算力边界（``op_catalog()`` 的已注册算子 +
     ``NUMERIC_FIELDS`` 的字段白名单）—— LLM 只能用真实存在的积木，
     幻觉算子/字段过不了 G0，与其让验证环节兜底，不如先缩小生成空间；
  2. 产出后逐条过 ``g0_static``（parse + 白名单 + 未来函数 + 算子合法性，
     平台的第一道门禁，语义不分叉），不合格的进 ``rejected`` 并带原因
     —— 淘汰明细可见，不静默；LLM 用错键名/漏 ``expr``/返回非对象的条目
     同样进 ``rejected``（带结构原因），全部不可解析时直接抛错说明真实原因，
     不冒充「G0 淘汰」。

**密钥卫生**：``LQ_LLM_API_KEY`` 只从 env 读取，不落盘、不进日志；
HTTP 错误消息只带状态码，不带请求头。

环境变量：
  - ``LQ_LLM_API_KEY``  必填（缺失显式报错，不静默降级）
  - ``LQ_LLM_API_BASE`` 缺省 ``https://api.openai.com/v1``（OpenAI 兼容，
    DeepSeek/Qwen/GLM 等换 base 即可）
  - ``LQ_LLM_MODEL``    缺省 ``gpt-4o-mini``
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

DEFAULT_API_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT = 120.0

__all__ = ["extract_proposals", "write_proposals", "capability_prompt"]

_SYSTEM_PROMPT = (
    "你是 A 股量化研究员。从研报/资讯文本中提取可形式化的选股因子逻辑，"
    "并用给定的 DSL 写成因子表达式。规则：\n"
    "1. 只用给出的字段（$开头）与算子，不要发明；\n"
    "2. 窗口参数 n 为正整数；\n"
    "3. 表达式必须是横截面可比的信号（对每只股票每日算一个数）；\n"
    "4. 每条提案给一句中文 note 说明经济学直觉与出处；\n"
    '5. 只输出 JSON：{{"proposals": [{{"expr": "...", "note": "..."}}]}}，'
    "最多 {max} 条，宁缺毋滥。"
)


def capability_prompt() -> str:
    """算子 + 字段清单。真实注册表驱动，不硬编码 —— 注册表新增算子自动进 prompt。"""
    from lquant.factors.fields import NUMERIC_FIELDS
    from lquant.factors.ops.catalog import op_catalog

    ops = "\n".join(
        f"  - {o['name']}（{o['label']}，min_window={o['min_window']}）" for o in op_catalog()
    )
    fields = ", ".join(f"${f}" for f in NUMERIC_FIELDS)
    return (
        f"可用字段：{fields}\n"
        f"可用算子（函数调用形式，如 Ts_Mean($close, 20)）：\n{ops}\n"
        "表达式示例：Rank(Ts_Mean($close, 5) / $close - 1)"
    )


def _env_llm(timeout: float = DEFAULT_TIMEOUT):
    """env 驱动的默认 LLM 调用器（OpenAI 兼容 /chat/completions，urllib 零依赖）。"""
    key = os.environ.get("LQ_LLM_API_KEY")
    if not key:
        raise RuntimeError(
            "LLM 未配置：请设置 LQ_LLM_API_KEY（可选 LQ_LLM_API_BASE / LQ_LLM_MODEL）"
        )
    base = os.environ.get("LQ_LLM_API_BASE", DEFAULT_API_BASE).rstrip("/")
    model = os.environ.get("LQ_LLM_MODEL", DEFAULT_MODEL)

    def fn(system: str, user: str) -> str:
        body = json.dumps(
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
            }
        ).encode()
        req = urllib.request.Request(
            f"{base}/chat/completions",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            # 只带状态码：错误体可能回显请求内容，密钥绝不能进异常链
            raise RuntimeError(f"LLM 请求失败: HTTP {e.code}") from e
        return data["choices"][0]["message"]["content"]

    return fn


def _raw_snippet(raw, n: int = 200) -> str:
    """LLM 原文前 N 字的可读片段。

    非字符串（content 为 None / 数字 / 数组）用 repr 兜底：报错路径本身
    绝不能再抛一次 TypeError，否则逃出 CLI 后只剩不可读的裸异常。
    """
    return raw[:n] if isinstance(raw, str) else repr(raw)[:n]


def _strip_code_fence(s: str) -> str:
    """剥掉 ```` ```json ... ``` ```` 围栏：LLM 常无视「只输出 JSON」这句。"""
    m = re.search(r"```[A-Za-z0-9_+-]*\s*(.*?)```", s, re.DOTALL)
    return (m.group(1) if m else s).strip()


def _balanced_slice(s: str, open_ch: str, close_ch: str) -> str | None:
    """取第一个括号配平的 ``{...}`` / ``[...]`` 片段（字符串内的括号不计数）。"""
    start = s.find(open_ch)
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


_UNSET = object()


def _parse_llm_json(raw) -> list:
    """LLM 返回 → 提案**原始条目**列表（字段校验交给 extract_proposals）。

    容错但**不静默**：先剥代码块围栏，再退化到第一个括号配平的
    ``{...}`` / ``[...]``。彻底解析不了、或 content 为 None/数字等非字符串
    时显式报错 —— 错误串带原文前 200 字，能分清是模型跑偏还是上游把
    ``message.content`` 拿成了 null（旧实现在这里裸抛 TypeError）。
    """
    snippet = _raw_snippet(raw)
    if not isinstance(raw, str):
        raise ValueError(f"LLM 返回不是字符串（{type(raw).__name__}）；原文前 200 字: {snippet!r}")
    text = _strip_code_fence(raw)
    parsed: object = _UNSET
    last_err: Exception | None = None
    for cand in (text, _balanced_slice(text, "{", "}"), _balanced_slice(text, "[", "]")):
        if not cand:
            continue
        try:
            parsed = json.loads(cand)
            break
        except (json.JSONDecodeError, TypeError) as e:  # TypeError 兜底：非 str 入参
            last_err = e
    if parsed is _UNSET:
        raise ValueError(f"LLM 返回不是合法 JSON: {last_err}；原文前 200 字: {snippet!r}")
    items = parsed.get("proposals") if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        raise ValueError(f"LLM 返回缺少 proposals 数组；原文前 200 字: {snippet!r}")
    return items


def _normalize_item(it) -> tuple[dict, str | None]:
    """单条 LLM 条目 → (规范化的 ``{expr, note}``, 不可用原因 or None)。

    LLM 用 ``expression``/``factor``/``formula`` 等键名、漏 ``expr``、或把
    ``expr`` 写成数字时，旧实现用列表推导静默过滤 —— 条目直接消失，全丢时
    CLI 还会把「一条都没进 G0」误报成「G0 淘汰」。这里把不可用条目显式收回
    并给出原因（含实际键名/原值片段），保证淘汰明细可见。
    """
    if not isinstance(it, dict):
        return {"expr": "", "note": ""}, (
            f"LLM 条目不是 JSON 对象（{type(it).__name__}: {_raw_snippet(it, 80)}）"
        )
    expr = it.get("expr")
    if not isinstance(expr, str) or not expr.strip():
        if expr is None:
            alt_key = next(
                (
                    k
                    for k in ("expression", "factor", "formula", "signal")
                    if isinstance(it.get(k), str) and it[k].strip()
                ),
                None,
            )
            actual = (
                f"（实际键名 {alt_key}={it[alt_key][:60]!r}）"
                if alt_key is not None
                else f"（实际键: {', '.join(str(k) for k in list(it)[:6])}）"
            )
        else:
            actual = f"（expr 实际为 {type(expr).__name__}）"
        return {"expr": "", "note": str(it.get("note", ""))}, f"LLM 条目缺少可用的 expr 字段{actual}"
    return {"expr": expr.strip(), "note": str(it.get("note", ""))}, None


def extract_proposals(
    text: str, *, llm_fn=None, max_proposals: int = 10, allowed_fields=None
) -> dict:
    """研报文本 → 提案。返回 ``{"accepted": [...], "rejected": [...]}``。

    accepted 元素为 ``{"expr", "note"}``（可直接写 JSONL 喂
    ``lq factor mine --generator proposals``）；rejected 元素多一个
    ``reason``（G0 原因码，或「LLM 条目缺少可用的 expr 字段」这类结构原因），
    淘汰明细永远可见 —— 不再有被列表推导静默吃掉的条目。

    ``llm_fn(system, user) -> str`` 可注入（测试用 FakeLLM；缺省走 env 配置）。

    Raises:
        ValueError: 研报文本为空；LLM 返回不是字符串/不是合法 JSON/缺
            ``proposals`` 数组；``proposals`` 为空数组；或**全部条目结构
            不可用**（一条都没进过 G0 —— 这时报「G0 淘汰」会误导排障方向，
            所以直接抛错并带上真实原因与原文片段）。
    """
    from lquant.factors.fields import NUMERIC_FIELDS
    from lquant.factors.mining.gates import g0_static

    if not text.strip():
        raise ValueError("研报文本为空，无从提取")
    llm_fn = llm_fn or _env_llm()
    system = _SYSTEM_PROMPT.format(max=max_proposals)
    user = f"{capability_prompt()}\n\n--- 研报文本 ---\n{text}"
    raw = llm_fn(system, user)
    fields = allowed_fields if allowed_fields is not None else set(NUMERIC_FIELDS)

    accepted: list[dict] = []
    rejected: list[dict] = []
    items = _parse_llm_json(raw)
    if not items:
        # 「宁缺毋滥」产空数组也是合法的，但静默返回空集合会让 CLI 把
        # 「LLM 没给提案」误报成「G0 淘汰了 0 条」。说清真实原因。
        raise ValueError(
            f"LLM 返回的 proposals 为空数组（0 条提案）；原文前 200 字: {_raw_snippet(raw)!r}"
        )
    malformed = 0
    for it in items:
        # 结构不可用的条目收进 rejected（带原因），不再用列表推导静默过滤
        item, bad = _normalize_item(it)
        if bad is not None:
            malformed += 1
            rejected.append({**item, "reason": bad})
            continue
        expr = item["expr"]
        gate = g0_static(expr, allowed_fields=fields)
        if gate.passed and len(accepted) < max_proposals:
            accepted.append(item)
        else:
            reason = gate.reason_code if not gate.passed else "超过 max_proposals 上限"
            rejected.append({**item, "reason": reason})
    if not accepted and malformed == len(items):
        # 一条都没进过 G0：再报「没有通过 G0」会把排障方向带偏（差的是 LLM
        # 输出结构，不是表达式质量），这里直接抛出带真实原因与原文片段的错误
        reasons = "；".join(r["reason"] for r in rejected)
        raise ValueError(
            f"LLM 返回的 {len(items)} 条提案全部无法解析（未进入 G0）：{reasons}；"
            f"原文前 200 字: {_raw_snippet(raw)!r}"
        )
    return {"accepted": accepted, "rejected": rejected}


def write_proposals(items: list[dict], path: str) -> int:
    """写 JSONL（``{expr, note}``，与 ``load_proposals`` 消费端同一契约）。返回条数。

    只写文件不打日志 —— CLI 层负责回显，库模块不依赖 click。
    """
    with open(path, "w", encoding="utf-8") as f:
        for it in items:
            f.write(
                json.dumps({"expr": it["expr"], "note": it.get("note", "")}, ensure_ascii=False)
                + "\n"
            )
    return len(items)
