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
     —— 淘汰明细可见，不静默。

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


def _parse_llm_json(raw: str) -> list[dict]:
    """LLM 返回 → 提案列表。格式坏 = 显式报错带原文片段，不静默吞。"""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM 返回不是合法 JSON: {e}；原文前 200 字: {raw[:200]!r}") from e
    items = obj.get("proposals") if isinstance(obj, dict) else obj
    if not isinstance(items, list):
        raise ValueError(f"LLM 返回缺少 proposals 数组；原文前 200 字: {raw[:200]!r}")
    return [it for it in items if isinstance(it, dict) and it.get("expr")]


def extract_proposals(
    text: str, *, llm_fn=None, max_proposals: int = 10, allowed_fields=None
) -> dict:
    """研报文本 → 提案。返回 ``{"accepted": [...], "rejected": [...]}``。

    accepted 元素为 ``{"expr", "note"}``（可直接写 JSONL 喂
    ``lq factor mine --generator proposals``）；rejected 元素多一个
    ``reason``（G0 原因码），淘汰可见。

    ``llm_fn(system, user) -> str`` 可注入（测试用 FakeLLM；缺省走 env 配置）。
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
    for it in _parse_llm_json(raw):
        expr = str(it["expr"]).strip()
        item = {"expr": expr, "note": str(it.get("note", ""))}
        gate = g0_static(expr, allowed_fields=fields)
        if gate.passed and len(accepted) < max_proposals:
            accepted.append(item)
        else:
            reason = gate.reason_code if not gate.passed else "超过 max_proposals 上限"
            rejected.append({**item, "reason": reason})
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
