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

**证据约束（C2）**：``evidence=`` 传入来源列表时，先经
``research/evidence.py`` 构造成**确定性压缩包**（双上限 + 编号不变），把带编号
的证据喂给 LLM，产出后对 accepted 逐条做**引用校验**：来源 id 必须存在、引文
必须能在对应来源正文中命中。校验不过 → 结果标记 ``status="degraded"``，提案
带 ``validation_failed`` 与逐条原因（保留原始提案但绝不冒充「已验证成功」）。
未传入证据时行为与旧版一致（无引用约束）。

**密钥卫生**：``LQ_LLM_API_KEY`` 只从 env 读取，不落盘、不进日志；
HTTP 错误消息只带状态码，不带请求头。

环境变量：
  - ``LQ_LLM_API_KEY``  必填（缺失显式报错，不静默降级）
  - ``LQ_LLM_API_BASE`` 缺省 ``https://api.openai.com/v1``（OpenAI 兼容，
    DeepSeek/Qwen/GLM 等换 base 即可）
  - ``LQ_LLM_MODEL``    缺省 ``gpt-4o-mini``
  - ``LQ_LLM_MAX_INPUT_CHARS`` 单次研报文本字符上限，缺省
    :data:`DEFAULT_MAX_INPUT_CHARS`；超限显式报错（见 ``extract_proposals``）
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

from lquant.research.evidence import (
    LLMFormatError,
    LLMTransportError,
    compress_evidence,
    guarded_repair,
    validate_citations,
)

DEFAULT_API_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT = 120.0
# 单次送进 LLM 的研报文本字符上限。取 3 万字：A 股研报正文多数在 1 万字
# 以内，留足余量；再长基本是整本 PDF/重复段落，继续塞只会让上下文溢出拿
# HTTP 400（且 400 不带「是文本太长」的信息，排障方向容易被带偏）。
# 超限行为见 extract_proposals：显式报错，不静默截断。
DEFAULT_MAX_INPUT_CHARS = 30_000

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

# 只有传证据时才附加：强制引用编号，并要求逐字引文。缺省路径（无证据）不约束，
# 避免给旧调用方凭空增加校验失败面。
_CITATION_RULE = (
    "\n6. 每条提案必须引用「证据包」中的来源编号：加 "
    '"source_ids": ["<编号>"]，编号必须来自证据包，不得编造；\n'
    '7. 如做逐字引用，再加 "quote"，必须是该来源正文里的连续原文片段，'
    "不得改写、拼接或跨省略号引用。"
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
            # 只带状态码：错误体可能回显请求内容，密钥绝不能进异常链。
            # 用 LLMTransportError 分类：HTTP 状态错误是连接层失败，不是格式问题。
            raise LLMTransportError(f"LLM 请求失败: HTTP {e.code}") from e
        return data["choices"][0]["message"]["content"]

    return fn


def _max_input_chars() -> int:
    """研报文本字符上限。env 覆盖；非法值显式报错（不静默退回默认）。"""
    raw = os.getenv("LQ_LLM_MAX_INPUT_CHARS")
    if raw is None or not raw.strip():
        return DEFAULT_MAX_INPUT_CHARS
    try:
        n = int(raw)
    except ValueError as e:
        raise ValueError(f"LQ_LLM_MAX_INPUT_CHARS 须为整数，收到: {raw!r}") from e
    if n <= 0:
        raise ValueError(f"LQ_LLM_MAX_INPUT_CHARS 须为正整数，收到: {n}")
    return n


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


def _parse_llm_json_with_repair(raw, repair_fn):
    """解析 + 受闸门的格式修复。

    ``_parse_llm_json`` 只在「拿到了内容但 JSON/结构不对」时抛 ``ValueError``，
    所以这里把它包成 :class:`~lquant.research.evidence.LLMFormatError` 再交给
    ``guarded_repair`` 判断。连接错误/取消在 ``llm_fn`` 调用处就已抛出，走不到
    解析路径 —— 这正是「连接错误/取消不触发格式修复」的结构性保证。
    """
    try:
        return _parse_llm_json(raw)
    except ValueError as e:
        if repair_fn is None:
            raise
        error = LLMFormatError(str(e))
        return guarded_repair(lambda: _parse_llm_json(repair_fn(raw)), error)


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
    item = {"expr": expr.strip(), "note": str(it.get("note", ""))}
    # 引用字段原样透传（不做清洗）：校验环节需要看到模型的**原始**声明，
    # 在这里「顺手修好」会让引用校验失去意义。
    for key in ("source_ids", "quote", "quotes"):
        if key in it:
            item[key] = it[key]
    return item, None


def extract_proposals(
    text: str,
    *,
    llm_fn=None,
    max_proposals: int = 10,
    allowed_fields=None,
    max_input_chars: int | None = None,
    evidence=None,
    evidence_kwargs: dict | None = None,
    repair_fn=None,
) -> dict:
    """研报文本 → 提案。返回 ``{"accepted": [...], "rejected": [...], "status": ...}``。

    accepted 元素为 ``{"expr", "note"}``（可直接写 JSONL 喂
    ``lq factor mine --generator proposals``）；rejected 元素多一个
    ``reason``（G0 原因码，或「LLM 条目缺少可用的 expr 字段」这类结构原因），
    淘汰明细永远可见 —— 不再有被列表推导静默吃掉的条目。

    ``llm_fn(system, user) -> str`` 可注入（测试用 FakeLLM；缺省走 env 配置）。
    ``max_input_chars`` 缺省取 ``LQ_LLM_MAX_INPUT_CHARS`` / 内置默认。

    ``evidence`` 传入来源列表（``{id, title, content, published_at, kind}``）时
    启用 C2 引用约束：先 :func:`~lquant.research.evidence.compress_evidence`
    构造压缩包（编号稳定、双上限、裁剪明细可见），prompt 里带编号证据，产出后
    对 accepted 做 :func:`~lquant.research.evidence.validate_citations`。校验
    不过 → ``status="degraded"``、``fallback`` 给出降级口径，未通过的提案带
    ``validation_failed=True`` 与 ``validation_reasons``；**绝不把未校验结果
    当成功返回**。``repair_fn(raw) -> str`` 是可选格式修复：只在「模型返回了
    内容但格式不对」时被调用，连接错误/取消不触发。

    Raises:
        ValueError: 研报文本为空；研报文本超长（> ``max_input_chars``）；
            LLM 返回不是字符串/不是合法 JSON/缺
            ``proposals`` 数组；``proposals`` 为空数组；或**全部条目结构
            不可用**（一条都没进过 G0 —— 这时报「G0 淘汰」会误导排障方向，
            所以直接抛错并带上真实原因与原文片段）。
    """
    from lquant.factors.fields import NUMERIC_FIELDS
    from lquant.factors.mining.gates import g0_static

    if not text.strip():
        raise ValueError("研报文本为空，无从提取")
    # 长度门禁：超长文本送进 LLM 只会换回一个 HTTP 400（上下文超限），而
    # 400 本身不带「是输入太长」的信息，排障会被带偏。这里显式报错并给出
    # 可操作的出口（调大 env / 拆分研报）—— 截断是另一种选择，但那会让
    # 提案悄悄只覆盖研报前半段，研究结论可能因此失真，风险高于直接拒绝。
    cap = _max_input_chars() if max_input_chars is None else int(max_input_chars)
    if cap <= 0:
        raise ValueError(f"max_input_chars 须为正整数，收到: {cap}")
    if len(text) > cap:
        raise ValueError(
            f"研报文本超长：{len(text)} 字 > 上限 {cap} 字；"
            "直接送 LLM 会因上下文超限拿到 HTTP 400。"
            "请拆分/精简研报，或调大 LQ_LLM_MAX_INPUT_CHARS 后重试"
        )

    # 证据包先于 LLM 调用构造：压缩失败要在这里响亮报错，而不是让模型对着
    # 半截证据产结论。
    pack = compress_evidence(evidence, **(evidence_kwargs or {})) if evidence is not None else None

    llm_fn = llm_fn or _env_llm()
    system = _SYSTEM_PROMPT.format(max=max_proposals) + (_CITATION_RULE if pack is not None else "")
    sections = [capability_prompt()]
    if pack is not None:
        sections.append("--- 证据包（引用编号以此为准）---\n" + pack.render_prompt())
    sections.append(f"--- 研报文本 ---\n{text}")
    user = "\n\n".join(sections)
    raw = llm_fn(system, user)
    fields = allowed_fields if allowed_fields is not None else set(NUMERIC_FIELDS)

    accepted: list[dict] = []
    rejected: list[dict] = []
    items = _parse_llm_json_with_repair(raw, repair_fn)
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

    result: dict = {"accepted": accepted, "rejected": rejected, "status": "ok"}
    if pack is not None:
        result["evidence_pack"] = pack.to_dict()
        # accepted 为空时不跑引用校验：此时是「没有通过 G0」的问题，
        # 报「引用校验失败」会把排障方向带偏（与上游既有错误语义保持一致）。
        if accepted:
            verdict = validate_citations(accepted, pack.cards)
            result["citation_validation"] = verdict.to_dict()
            if not verdict.ok:
                # 降级而非丢弃：原始提案保留（便于人审与修 prompt），但明确标记
                # 未通过校验；消费端据 status/validation_failed 决定是否采信。
                result["status"] = "degraded"
                result["fallback"] = verdict.fallback
                for index, item in enumerate(accepted):
                    reasons = verdict.reasons_for(index)
                    if reasons:
                        item["validation_failed"] = True
                        item["validation_reasons"] = list(reasons)
    return result


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
