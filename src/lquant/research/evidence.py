"""AI 产出的证据约束栈：确定性压缩包 + 引用校验 + 阶段检查点 + 失败路径纪律。

C2 条目（``FEATURE-IDEAS.md:115``）指出 lquant 的 AI 结论缺「强制引用」与
「可重放约束」：回答不了「这个结论基于哪条快照、能否重放、模型换了还能不能
续跑」。本模块把四件套里的前三件落成**纯函数 + 少量状态**：

1. :func:`compress_evidence` —— 确定性证据压缩包。双上限（卡片数 + 正文总
   字节），来源编号在压缩前后**保持不变**（压缩只删内容，绝不重编号，否则
   引用会指错），并显式报告被截断/丢弃了哪些、压缩前后来源数与字节数。
2. :func:`validate_citations` —— 报告的每条论点必须引用**存在**的来源 id，
   引文必须能在对应来源正文中**命中**（规范化：去空白 + 全半角折叠）；失败
   逐条给原因并附**降级结果**，绝不静默通过。
3. :class:`ResearchCheckpoint` —— 阶段检查点记录「模型标识 + prompt 版本 +
   请求指纹 + 证据 hash」；恢复时四者任一变化即拒绝复用。检查点只保存私有
   未校验中间态；**未通过引用校验的中间态在类型层面无法变成最终报告**
   （``FinalReport.__post_init__`` 会二次校验，不是靠注释约束）。
4. :func:`guarded_repair` —— 失败路径纪律：连接错误 / 取消**不触发** JSON
   格式修复（修复只对「模型返回了内容但格式不对」生效）；失败输出限长 + 脱敏，
   且公共序列化不携带失败原文。

思路参考 easy-stock 的 ``research_evidence.go`` / ``research_checkpoint.go`` /
``research_validation.go`` / ``research_budget.go``（PolyForm Noncommercial）：
**只借结构，不复制代码**。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import re
import socket
import unicodedata
import urllib.error
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "COMPRESSION_VERSION",
    "CHECKPOINT_VERSION",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_CARDS",
    "DEFAULT_PER_SOURCE_BYTES",
    "DEFAULT_FAILURE_MAX_CHARS",
    "QUANT_SNAPSHOT_FALLBACK",
    "CheckpointIdentity",
    "CheckpointMismatchError",
    "CitationFailure",
    "CitationVerdict",
    "CompressionStats",
    "DroppedSource",
    "EvidenceCard",
    "EvidencePack",
    "EvidenceSource",
    "FinalReport",
    "IntermediateStage",
    "LLMCancelledError",
    "LLMFormatError",
    "LLMTransportError",
    "ResearchCheckpoint",
    "StageFailure",
    "TruncatedSource",
    "UnvalidatedIntermediateError",
    "compress_evidence",
    "compute_request_fingerprint",
    "content_hash",
    "evidence_digest",
    "guarded_repair",
    "is_cancellation",
    "is_transport_failure",
    "normalize_for_match",
    "open_checkpoint",
    "redact_secrets",
    "sanitize_failure_text",
    "should_attempt_json_repair",
    "validate_citations",
]

# 压缩版本随「压缩策略」变化递增：检查点按它拒绝跨策略复用旧中间态。
COMPRESSION_VERSION = "evidence-pack-v1"
CHECKPOINT_VERSION = 1

DEFAULT_MAX_CARDS = 24
DEFAULT_MAX_BYTES = 24000
DEFAULT_PER_SOURCE_BYTES = 2000
DEFAULT_TITLE_CHARS = 100
DEFAULT_FAILURE_MAX_CHARS = 2000

# 引用校验失败时的降级口径：AI 论点一律不可用，只信平台自己算的量化快照。
QUANT_SNAPSHOT_FALLBACK = "引用校验失败：降级为仅保留量化快照，未校验的 AI 论点不得作为结论"


# ---------------------------------------------------------------------------
# 证据来源与压缩包
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceSource:
    """一条候选证据。``id`` 是引用锚点，压缩前后必须逐字保持不变。"""

    id: str
    title: str = ""
    content: str = ""
    published_at: str = ""
    kind: str = "news"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "content": self.content,
            "published_at": self.published_at,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class EvidenceCard:
    """压缩后的证据卡片：id 原样保留，只压缩正文。"""

    id: str
    title: str
    kind: str
    published_at: str
    content: str
    exact: bool
    compression: str

    @property
    def content_bytes(self) -> int:
        return len(self.content.encode("utf-8"))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "published_at": self.published_at,
            "content": self.content,
            "exact": self.exact,
            "compression": self.compression,
            "content_bytes": self.content_bytes,
        }


@dataclass(frozen=True)
class DroppedSource:
    """被整体丢弃的来源：必须可见，否则「为什么这条没进 prompt」无从排障。"""

    id: str
    title: str
    reason: str

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "reason": self.reason}


@dataclass(frozen=True)
class TruncatedSource:
    """正文被裁剪的来源：记录裁剪前后字节数与裁剪方式。"""

    id: str
    original_bytes: int
    kept_bytes: int
    mode: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "original_bytes": self.original_bytes,
            "kept_bytes": self.kept_bytes,
            "mode": self.mode,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CompressionStats:
    """压缩前后计数：回答「模型实际看到了多少证据」。"""

    original_source_count: int
    selected_source_count: int
    original_content_bytes: int
    selected_content_bytes: int
    max_cards: int
    max_bytes: int

    def to_dict(self) -> dict:
        return {
            "original_source_count": self.original_source_count,
            "selected_source_count": self.selected_source_count,
            "original_content_bytes": self.original_content_bytes,
            "selected_content_bytes": self.selected_content_bytes,
            "max_cards": self.max_cards,
            "max_bytes": self.max_bytes,
        }


@dataclass(frozen=True)
class EvidencePack:
    """确定性证据压缩包。卡片的 id 集合是原来源 id 集合的子集，绝不重编号。"""

    version: str
    cards: tuple[EvidenceCard, ...]
    dropped: tuple[DroppedSource, ...]
    truncated: tuple[TruncatedSource, ...]
    stats: CompressionStats
    limitations: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(c.id for c in self.cards)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "stats": self.stats.to_dict(),
            "cards": [c.to_dict() for c in self.cards],
            "dropped": [d.to_dict() for d in self.dropped],
            "truncated": [t.to_dict() for t in self.truncated],
            "limitations": list(self.limitations),
        }

    def render_prompt(self) -> str:
        """渲染给 LLM 的证据包文本。编号即引用键，顺序确定。"""
        s = self.stats
        head = (
            f"证据包（版本 {self.version}）：纳入 {s.selected_source_count}/"
            f"{s.original_source_count} 条来源，正文 {s.selected_content_bytes}/"
            f"{s.original_content_bytes} 字节。\n"
            "引用规则：每条结论必须引用下列来源编号（source_ids），"
            "逐字引文（quote）必须是该来源正文的连续原文片段，不得改写或跨段拼接。"
        )
        lines = [head]
        for card in self.cards:
            meta = " | ".join(x for x in (card.kind, card.published_at, card.title) if x)
            lines.append(f"[{card.id}] {meta}".rstrip())
            lines.append(card.content)
        if self.dropped:
            lines.append(
                "未纳入的来源（不得引用）："
                + "；".join(f"{d.id}（{d.reason}）" for d in self.dropped)
            )
        return "\n".join(lines)


# 来源类型优先级：业务披露 > 指定来源 > 定向新闻 > 一般新闻 > 方法论。
_KIND_PRIORITY = {
    "disclosure": 100,
    "company_profile": 96,
    "filing": 96,
    "announcement": 82,
    "calculation": 90,
    "snapshot": 90,
    "research": 42,
    "opinion": 48,
    "news": 38,
    "methodology": 10,
}
_DEFAULT_KIND_PRIORITY = 30

# 业务披露识别词：经营/订单类事实最容易被资本事件类公告挤掉，需要优先级保护。
_IR_TITLE_MARKERS = ("投资者关系", "调研", "业绩说明", "经营情况", "业务进展", "产品进展")
_BUSINESS_TERMS = ("产品", "客户", "订单", "量产", "商业化", "出货", "研发", "收入构成", "产能", "供应链", "合作", "技术")

_SENTENCE_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")
_FACT_TERMS = ("营业总收入", "归母净利润", "扣非", "经营活动", "公告", "报告期", "订单", "中标", "回购", "同比", "环比")
_RISK_TERMS = ("风险", "下滑", "下降", "亏损", "减持", "诉讼", "问询", "澄清", "不确定", "减值", "商誉", "应收", "存货")

_MIN_CARD_BYTES = 80  # 剩余预算低于此值就别塞残句：切碎的引文只会制造假阴性

_SOURCE_ID_KEYS = ("id", "source_id", "sourceId", "sid", "evidence_id")
_SOURCE_TITLE_KEYS = ("title", "name", "headline")
_SOURCE_CONTENT_KEYS = ("content", "text", "body", "excerpt")
_SOURCE_DATE_KEYS = ("published_at", "published", "date", "report_date", "created_at")
_SOURCE_KIND_KEYS = ("kind", "source_type", "type", "category")


def _first_value(mapping: Mapping, keys: Sequence[str], default: str = "") -> str:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            value = mapping[key]
            return value if isinstance(value, str) else str(value)
    return default


def _coerce_source(raw: Any) -> EvidenceSource:
    if isinstance(raw, EvidenceSource):
        return raw
    if not isinstance(raw, Mapping):
        raise TypeError(f"证据条目必须是 mapping/EvidenceSource，实际是 {type(raw).__name__}")
    sid = _first_value(raw, _SOURCE_ID_KEYS)
    if not sid.strip():
        raise ValueError(f"证据条目缺少来源 id（可用键：{', '.join(_SOURCE_ID_KEYS)}）")
    return EvidenceSource(
        id=sid.strip(),
        title=_first_value(raw, _SOURCE_TITLE_KEYS),
        content=_first_value(raw, _SOURCE_CONTENT_KEYS),
        published_at=_first_value(raw, _SOURCE_DATE_KEYS),
        kind=(_first_value(raw, _SOURCE_KIND_KEYS, "news").strip().lower() or "news"),
    )


def coerce_sources(evidence: Iterable) -> list[EvidenceSource]:
    """归一化证据列表。**id 重复直接报错**：编号歧义会让引用校验失去意义。"""
    sources: list[EvidenceSource] = []
    seen: set[str] = set()
    for raw in evidence:
        src = _coerce_source(raw)
        if src.id in seen:
            raise ValueError(f"证据来源 id 重复：{src.id!r}（引用编号必须唯一）")
        seen.add(src.id)
        sources.append(src)
    if not sources:
        raise ValueError("证据列表为空，无法构建压缩包")
    return sources


def _is_business_disclosure(src: EvidenceSource) -> bool:
    if src.kind not in ("announcement", "disclosure", "filing"):
        return False
    if any(m in src.title for m in _IR_TITLE_MARKERS):
        return True
    return sum(1 for term in _BUSINESS_TERMS if term in src.content) >= 4


def _date_sort_key(published_at: str) -> int:
    """日期 → 可比整数（取数字位）。格式不一也不影响确定性排序。"""
    digits = "".join(ch for ch in published_at if ch.isdigit())
    return int(digits[:8]) if len(digits) >= 8 else 0


def _priority_score(src: EvidenceSource, priority_ids: frozenset[str]) -> int:
    score = 1000 if src.id in priority_ids else 0
    score += _KIND_PRIORITY.get(src.kind, _DEFAULT_KIND_PRIORITY)
    if _is_business_disclosure(src):
        score += 40
        if any(m in src.title for m in _IR_TITLE_MARKERS):
            score += 60
    if src.published_at:
        score += 6
    return score


def _sort_key(src: EvidenceSource, priority_ids: frozenset[str]):
    # ties 一律用 id 兜底：输入顺序被打乱后输出仍逐元素一致。
    return (-_priority_score(src, priority_ids), -_date_sort_key(src.published_at), src.id)


def _truncate_bytes(text: str, limit_bytes: int) -> str:
    """按 UTF-8 字节上限安全截断（不劈开多字节字符）。"""
    if limit_bytes <= 0:
        return ""
    used = 0
    out: list[str] = []
    for ch in text:
        size = len(ch.encode("utf-8"))
        if used + size > limit_bytes:
            break
        out.append(ch)
        used += size
    return "".join(out)


def _score_sentence(sentence: str, index: int) -> int:
    score = 0
    if any(t in sentence for t in _RISK_TERMS):
        score += 12
    if any(t in sentence for t in _FACT_TERMS):
        score += 8
    if any(ch.isdigit() for ch in sentence):
        score += 8
    if index == 0:
        score += 2  # 首句常是主题句，同分时优先保留
    return score


def _split_sentences(text: str) -> list[str]:
    return [m.group(0) for m in _SENTENCE_RE.finditer(text)]


def _compress_body(text: str, limit_bytes: int) -> tuple[str, str]:
    """正文 →（压缩后正文, 压缩方式）。

    先做**整句选择**（按风险/事实/数字打分，保留原序，非相邻片段用 ``…`` 隔开），
    这样引文仍是原文连续片段；整句装不下才退化为头部字节截断。
    """
    raw = text.strip()
    if not raw:
        return "", "empty"
    if len(raw.encode("utf-8")) <= limit_bytes:
        return raw, "no_compression"

    parts = [(i, s.strip()) for i, s in enumerate(_split_sentences(raw)) if s.strip()]
    if len(parts) >= 2:
        ranked = sorted(parts, key=lambda p: (-_score_sentence(p[1], p[0]), p[0]))
        chosen: list[tuple[int, str]] = []
        used = 0
        for idx, sentence in ranked:
            cost = len(sentence.encode("utf-8"))
            if used + cost > limit_bytes:
                continue
            chosen.append((idx, sentence))
            used += cost
        if chosen:
            chosen.sort()
            body = _join_sentences(chosen)
            if len(body.encode("utf-8")) <= limit_bytes:
                return body, "sentence_selection"

    return _truncate_bytes(raw, limit_bytes), "head_truncate"


def _join_sentences(chosen: Sequence[tuple[int, str]]) -> str:
    """按原序拼接，缺口用 ``…`` 标记 —— 跨缺口引用就不会误命中。"""
    out: list[str] = []
    prev = -2
    for idx, sentence in chosen:
        if out and idx != prev + 1:
            out.append("…")
        out.append(sentence)
        prev = idx
    return "".join(out)


def compress_evidence(
    evidence: Iterable,
    *,
    max_cards: int = DEFAULT_MAX_CARDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    per_source_bytes: int = DEFAULT_PER_SOURCE_BYTES,
    priority_ids: Iterable[str] = (),
) -> EvidencePack:
    """证据列表 → 确定性压缩包（双上限 + 优先级）。

    - **卡片数上限** ``max_cards``；**正文总字节上限** ``max_bytes``。
    - 排序由「指定来源 + 来源类型 + 业务披露 + 发布时间 + id」决定，与输入顺序无关。
    - 来源编号只做保留/丢弃，**从不重编号**；被丢弃/截断的逐条可见。
    - 同样输入 → 逐元素同样的输出（无 dict 顺序、无随机、无时间依赖）。
    """
    if max_cards <= 0:
        raise ValueError(f"max_cards 必须为正整数，实际 {max_cards}")
    if max_bytes <= 0:
        raise ValueError(f"max_bytes 必须为正整数，实际 {max_bytes}")
    if per_source_bytes <= 0:
        raise ValueError(f"per_source_bytes 必须为正整数，实际 {per_source_bytes}")

    sources = coerce_sources(evidence)
    priority = frozenset(str(p) for p in priority_ids)
    ordered = sorted(sources, key=lambda s: _sort_key(s, priority))

    original_bytes = sum(len(s.content.encode("utf-8")) for s in sources)
    cards: list[EvidenceCard] = []
    dropped: list[DroppedSource] = []
    truncated: list[TruncatedSource] = []
    used_bytes = 0

    for src in ordered:
        if len(cards) >= max_cards:
            dropped.append(DroppedSource(src.id, src.title, f"超出卡片数上限 max_cards={max_cards}"))
            continue

        remaining = max_bytes - used_bytes
        if remaining < _MIN_CARD_BYTES:
            dropped.append(DroppedSource(src.id, src.title, f"正文总字节预算耗尽（剩余 {remaining} 字节）"))
            continue

        original_own = len(src.content.encode("utf-8"))
        cap = min(per_source_bytes, remaining)
        body, mode = _compress_body(src.content, cap)
        reason = ""
        if mode == "empty":
            # 无正文时退化为标题：至少保留编号，让模型知道「这条只有标题、不能当真」
            body = _truncate_bytes(src.title.strip(), min(160, remaining))
            if not body:
                dropped.append(DroppedSource(src.id, src.title, "无正文且无标题，卡片无内容"))
                continue
            mode = "title_only"
            reason = "未取得正文，仅标题；不能据此确认业务细节"

        kept = len(body.encode("utf-8"))
        if kept < original_own:
            truncated.append(
                TruncatedSource(
                    id=src.id,
                    original_bytes=original_own,
                    kept_bytes=kept,
                    mode=mode,
                    reason=reason or f"按 {mode} 压缩至 {cap} 字节内",
                )
            )

        exact = mode in ("no_compression", "sentence_selection") and bool(src.content.strip())
        compression = {
            "no_compression": "原文全文",
            "sentence_selection": "原文整句摘录，省略处用…分隔；引文只能引用连续片段",
            "head_truncate": "原文头部截断；引文只能引用保留下来的连续片段",
            "title_only": "仅标题，未取得正文；不能据此确认业务细节",
        }.get(mode, mode)
        cards.append(
            EvidenceCard(
                id=src.id,
                title=_truncate_bytes(src.title.strip(), DEFAULT_TITLE_CHARS),
                kind=src.kind,
                published_at=src.published_at,
                content=body,
                exact=exact,
                compression=compression,
            )
        )
        used_bytes += kept

    limitations = _pack_limitations(cards, dropped, truncated)
    return EvidencePack(
        version=COMPRESSION_VERSION,
        cards=tuple(cards),
        dropped=tuple(dropped),
        truncated=tuple(truncated),
        stats=CompressionStats(
            original_source_count=len(sources),
            selected_source_count=len(cards),
            original_content_bytes=original_bytes,
            selected_content_bytes=used_bytes,
            max_cards=max_cards,
            max_bytes=max_bytes,
        ),
        limitations=limitations,
    )


def _pack_limitations(
    cards: Sequence[EvidenceCard],
    dropped: Sequence[DroppedSource],
    truncated: Sequence[TruncatedSource],
) -> tuple[str, ...]:
    notes: list[str] = []
    if dropped:
        notes.append(f"丢弃 {len(dropped)} 条来源（编号：{', '.join(d.id for d in dropped)}），不得引用")
    if truncated:
        notes.append(
            f"截断 {len(truncated)} 条来源正文（编号：{', '.join(t.id for t in truncated)}），"
            "截断处之外的原文不可引用"
        )
    if any(c.compression.startswith("仅标题") for c in cards):
        bad = ", ".join(c.id for c in cards if c.compression.startswith("仅标题"))
        notes.append(f"以下来源仅有标题、无正文（{bad}）：不能据此确认业务细节")
    return tuple(notes)


# ---------------------------------------------------------------------------
# 规范化与引用校验
# ---------------------------------------------------------------------------

_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2028\u2029\ufeff]")


def normalize_for_match(text: str) -> str:
    """引文命中判定用的规范化：全半角折叠（NFKC）+ 去空白 + 去零宽 + casefold。

    为什么必须折叠：模型复述中文数字/标点时全角半角会漂移，逐字相等会把
    正确引文误判为幻觉；但只折叠**无信息量**的差异，不做同义改写。
    """
    if not isinstance(text, str):
        raise TypeError(f"待规范化文本必须是 str，实际 {type(text).__name__}")
    folded = unicodedata.normalize("NFKC", text)
    folded = _ZERO_WIDTH.sub("", folded)
    return "".join(ch for ch in folded if not ch.isspace()).casefold()


@dataclass(frozen=True)
class CitationFailure:
    """单条引用失败。``detail`` 必须能直接回答「哪里错了、怎么改」。"""

    claim_index: int
    code: str
    source_id: str | None
    detail: str

    def to_dict(self) -> dict:
        return {
            "claim_index": self.claim_index,
            "code": self.code,
            "source_id": self.source_id,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CitationVerdict:
    """引用校验结果。``ok=False`` 时一定带降级口径，绝不静默通过。"""

    ok: bool
    checked_claims: int
    verified_claims: int
    failures: tuple[CitationFailure, ...]
    fallback: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checked_claims": self.checked_claims,
            "verified_claims": self.verified_claims,
            "fallback": self.fallback,
            "failures": [f.to_dict() for f in self.failures],
        }

    def reasons_for(self, claim_index: int) -> tuple[str, ...]:
        return tuple(f.detail for f in self.failures if f.claim_index == claim_index)


_CLAIM_TEXT_KEYS = ("text", "claim", "statement", "note", "conclusion", "thesis")
_CLAIM_SOURCE_KEYS = ("source_ids", "sourceIDs", "source_id", "sources", "evidence_ids")
_CLAIM_QUOTE_KEYS = ("quotes", "quote", "evidence_quotes")
_QUOTE_TEXT_KEYS = ("quote", "text", "quote_text", "excerpt")
_QUOTE_SOURCE_KEYS = ("source_id", "sourceId", "id", "source")


@dataclass(frozen=True)
class _Quote:
    source_id: str | None
    quote: str


@dataclass(frozen=True)
class _Claim:
    index: int
    text: str
    source_ids: tuple[str, ...]
    quotes: tuple[_Quote, ...]

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "text": self.text,
            "source_ids": list(self.source_ids),
            "quotes": [{"source_id": q.source_id, "quote": q.quote} for q in self.quotes],
        }


def _coerce_card(raw: Any) -> EvidenceCard:
    if isinstance(raw, EvidenceCard):
        return raw
    if not isinstance(raw, Mapping):
        raise TypeError(f"证据卡片必须是 mapping/EvidenceCard，实际是 {type(raw).__name__}")
    src = _coerce_source(raw)
    return EvidenceCard(
        id=src.id,
        title=src.title,
        kind=src.kind,
        published_at=src.published_at,
        content=src.content,
        exact=True,
        compression="",
    )


def coerce_cards(cards: Any) -> dict[str, EvidenceCard]:
    """卡片集合 → ``{id: card}``。id 重复直接报错，不覆盖也不重编号。"""
    if isinstance(cards, EvidencePack):
        items: Sequence = cards.cards
    elif isinstance(cards, Mapping):
        raise TypeError("cards 需要证据包或卡片序列，不是 mapping（避免误传 report）")
    else:
        items = list(cards)
    index: dict[str, EvidenceCard] = {}
    for raw in items:
        card = _coerce_card(raw)
        if card.id in index:
            raise ValueError(f"证据卡片 id 重复：{card.id!r}")
        index[card.id] = card
    return index


def _as_id_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, Mapping):
        return tuple(str(k) for k in value)
    if isinstance(value, Iterable):
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                if item.strip():
                    out.append(item)
            elif isinstance(item, Mapping):
                sid = _first_value(item, _QUOTE_SOURCE_KEYS)
                if sid.strip():
                    out.append(sid)
        return tuple(out)
    raise TypeError(f"source_ids 不支持的类型：{type(value).__name__}")


def _as_quotes(value: Any) -> tuple[_Quote, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (_Quote(None, value),)
    if isinstance(value, Mapping):
        return (_coerce_quote(value),)
    if isinstance(value, Iterable):
        return tuple(_coerce_quote(item) for item in value)
    raise TypeError(f"quotes 不支持的类型：{type(value).__name__}")


def _coerce_quote(raw: Any) -> _Quote:
    if isinstance(raw, str):
        return _Quote(None, raw)
    if isinstance(raw, Mapping):
        return _Quote(
            source_id=_first_value(raw, _QUOTE_SOURCE_KEYS) or None,
            quote=_first_value(raw, _QUOTE_TEXT_KEYS),
        )
    raise TypeError(f"单条引文必须是 str/mapping，实际是 {type(raw).__name__}")


def _coerce_claim(raw: Any, index: int) -> _Claim:
    if not isinstance(raw, Mapping):
        raise TypeError(f"第 {index} 条论点不是 mapping（{type(raw).__name__}），无法校验引用")
    text = _first_value(raw, _CLAIM_TEXT_KEYS)
    source_ids: list[str] = []
    for key in _CLAIM_SOURCE_KEYS:
        if key in raw:
            source_ids.extend(_as_id_list(raw[key]))
            break
    quotes: list[_Quote] = []
    for key in _CLAIM_QUOTE_KEYS:
        if key in raw:
            quotes.extend(_as_quotes(raw[key]))
            break
    # 引文里自带的 source_id 也算论点的引用面，避免「有引文却判无来源」。
    for quote in quotes:
        if quote.source_id and quote.source_id not in source_ids:
            source_ids.append(quote.source_id)
    return _Claim(index, text, tuple(source_ids), tuple(quotes))


def _extract_claims(report: Any) -> tuple[_Claim, ...]:
    if isinstance(report, Mapping):
        if "claims" not in report:
            raise ValueError("报告 mapping 缺少 claims 数组，无法校验引用")
        raw_claims = report["claims"]
    elif isinstance(report, (list, tuple)):
        raw_claims = report
    else:
        raise TypeError(f"报告必须是带 claims 的 mapping 或论点序列，实际 {type(report).__name__}")
    if not isinstance(raw_claims, Iterable) or isinstance(raw_claims, (str, bytes)):
        raise ValueError("报告的 claims 必须是数组")
    claims = tuple(_coerce_claim(item, i) for i, item in enumerate(raw_claims))
    if not claims:
        # 空报告通过引用校验＝静默通过，正是 C2 要治的病。
        raise ValueError("报告没有任何论点，引用校验无从谈起（拒绝空报告静默通过）")
    return claims


def _quote_hits(content: str, quote: str) -> bool:
    needle = normalize_for_match(quote)
    if not needle:
        return False
    return needle in normalize_for_match(content)


def validate_citations(report: Any, cards: Any) -> CitationVerdict:
    """校验报告引用：来源 id 必须存在，引文必须能在对应来源正文命中。

    ``report`` 为 ``{"claims": [...]}`` 或论点序列；每条论点可用
    ``source_ids``（别名 ``source_id``/``sources``）与 ``quote``/``quotes``
    声明引用。返回 :class:`CitationVerdict`，失败逐条给原因并带降级口径。
    """
    index = coerce_cards(cards)
    claims = _extract_claims(report)
    failures: list[CitationFailure] = []
    verified = 0

    for claim in claims:
        before = len(failures)
        known = [sid for sid in claim.source_ids if sid in index]
        for sid in claim.source_ids:
            if sid not in index:
                failures.append(
                    CitationFailure(
                        claim.index,
                        "UNKNOWN_SOURCE",
                        sid,
                        f"第 {claim.index} 条论点引用了不存在的来源 id {sid!r}；"
                        f"可用编号：{', '.join(sorted(index))}",
                    )
                )
        if not claim.source_ids:
            failures.append(
                CitationFailure(
                    claim.index,
                    "MISSING_SOURCE",
                    None,
                    f"第 {claim.index} 条论点未引用任何来源 id（source_ids 为空）",
                )
            )
        for quote in claim.quotes:
            if not quote.quote.strip():
                failures.append(
                    CitationFailure(claim.index, "EMPTY_QUOTE", quote.source_id, f"第 {claim.index} 条论点的引文为空字符串")
                )
                continue
            if quote.source_id is not None:
                candidates = [quote.source_id]
            else:
                candidates = list(claim.source_ids)
            if not candidates:
                failures.append(
                    CitationFailure(
                        claim.index,
                        "MISSING_SOURCE",
                        None,
                        f"第 {claim.index} 条论点的引文未指明来源，且论点也没有 source_ids",
                    )
                )
                continue
            hit = next((c for c in candidates if c in index and _quote_hits(index[c].content, quote.quote)), None)
            if hit is None:
                tried = ", ".join(candidates)
                failures.append(
                    CitationFailure(
                        claim.index,
                        "QUOTE_NOT_FOUND",
                        quote.source_id or candidates[0],
                        f"第 {claim.index} 条论点的引文在来源 {tried} 正文中命中不了：{quote.quote[:60]!r}",
                    )
                )
        if len(failures) == before and known:
            verified += 1

    ok = not failures
    return CitationVerdict(
        ok=ok,
        checked_claims=len(claims),
        verified_claims=verified,
        failures=tuple(failures),
        fallback=None if ok else QUANT_SNAPSHOT_FALLBACK,
    )


# ---------------------------------------------------------------------------
# 阶段检查点：可重放身份 + 私有未校验中间态
# ---------------------------------------------------------------------------

_IDENTITY_FIELDS = ("model_identity", "prompt_version", "request_fingerprint", "evidence_hash")


class CheckpointMismatchError(RuntimeError):
    """模型/提示词/请求/证据任一变化 → 拒绝复用旧检查点。"""


class UnvalidatedIntermediateError(RuntimeError):
    """未通过引用校验的中间态试图被当作最终报告。"""


@dataclass(frozen=True)
class CheckpointIdentity:
    """可重放身份四元组：任何一个变了，旧研究就不能续跑。"""

    model_identity: str
    prompt_version: str
    request_fingerprint: str
    evidence_hash: str

    def to_dict(self) -> dict:
        return {name: getattr(self, name) for name in _IDENTITY_FIELDS}

    @classmethod
    def from_dict(cls, payload: Mapping) -> CheckpointIdentity:
        missing = [name for name in _IDENTITY_FIELDS if name not in payload]
        if missing:
            raise ValueError(f"检查点身份缺少字段：{', '.join(missing)}")
        return cls(**{name: str(payload[name]) for name in _IDENTITY_FIELDS})

    def changed_fields(self, other: CheckpointIdentity) -> tuple[str, ...]:
        return tuple(name for name in _IDENTITY_FIELDS if getattr(self, name) != getattr(other, name))

    @property
    def digest(self) -> str:
        return content_hash(self.to_dict())


def content_hash(value: Any) -> str:
    """规范化 JSON 的 sha256：同一逻辑内容 → 同一 hash，与键序/空白无关。"""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def compute_request_fingerprint(request: Any) -> str:
    """研究请求指纹：请求体任意顺序 → 同一指纹。"""
    if isinstance(request, str):
        return content_hash({"request": request})
    if isinstance(request, Mapping):
        return content_hash({str(k): request[k] for k in sorted(request, key=str)})
    return content_hash({"request": repr(request)})


def evidence_digest(evidence: Any) -> str:
    """证据 hash：对**模型实际看到的压缩包**取指纹，而不是原始快照。

    压缩策略变更会改变模型输入，因此必须进入 hash —— 否则「换了压缩规则还
    复用旧中间态」会被误判为可重放。
    """
    if isinstance(evidence, EvidencePack):
        return content_hash(evidence.to_dict())
    sources = sorted(coerce_sources(evidence), key=lambda s: s.id)
    return content_hash([s.to_dict() for s in sources])


@dataclass(frozen=True)
class StageFailure:
    """失败诊断：``error`` 已限长脱敏，失败原文只留在私有字段。

    对外序列化（:meth:`to_dict`）**不携带**原始失败内容 —— 它可能回显 prompt
    或密钥，绝不能顺着公开 payload 泄露出去。
    """

    stage: str
    error: str
    stage_index: int = -1
    error_chars: int = 0
    _diagnostic_content: str = ""

    @classmethod
    def build(cls, stage: str, error: Any, content: Any = "", *, max_chars: int = DEFAULT_FAILURE_MAX_CHARS) -> StageFailure:
        message = sanitize_failure_text(error, max_chars=max_chars)
        diagnostic = sanitize_failure_text(content, max_chars=max_chars) if content else ""
        return cls(stage=stage, error=message, error_chars=len(message), _diagnostic_content=diagnostic)

    def to_dict(self) -> dict:
        return {"stage": self.stage, "error": self.error, "error_chars": self.error_chars}

    def __repr__(self) -> str:  # 防止 repr 顺着异常链把原文带出去
        return f"StageFailure(stage={self.stage!r}, error={self.error!r})"


@dataclass(frozen=True)
class IntermediateStage:
    """私有**未校验**中间态。类型上就不是 FinalReport，不能当结论用。"""

    stage: str
    value: Any

    def __post_init__(self) -> None:
        if isinstance(self.value, FinalReport):
            raise TypeError("最终报告不能回写成中间态")


@dataclass(frozen=True)
class FinalReport:
    """唯一合法的最终报告形态：构造即要求引用校验通过。"""

    identity: CheckpointIdentity
    claims: tuple[dict, ...]
    validation: CitationVerdict

    def __post_init__(self) -> None:
        if not isinstance(self.validation, CitationVerdict):
            raise UnvalidatedIntermediateError("最终报告必须携带 CitationVerdict 校验结果")
        if not self.validation.ok:
            raise UnvalidatedIntermediateError(
                f"引用校验未通过（{len(self.validation.failures)} 条失败）的中间态不能作为最终报告："
                f"{self.validation.fallback or ''}"
            )

    def to_dict(self) -> dict:
        return {
            "identity": self.identity.to_dict(),
            "claims": [dict(c) for c in self.claims],
            "validation": self.validation.to_dict(),
        }


class ResearchCheckpoint:
    """单次研究的阶段检查点：只保存私有未校验中间态与失败诊断。"""

    def __init__(
        self,
        identity: CheckpointIdentity,
        *,
        stages: Mapping[str, Any] | None = None,
        failures: Mapping[str, StageFailure] | None = None,
    ) -> None:
        self._identity = identity
        self._stages: dict[str, Any] = dict(stages or {})
        self._failures: dict[str, StageFailure] = dict(failures or {})

    @property
    def identity(self) -> CheckpointIdentity:
        return self._identity

    @property
    def stage_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._stages))

    def resume(self, identity: CheckpointIdentity) -> ResearchCheckpoint:
        """恢复前四元组校验：任一变化即拒绝复用并给出明确信号。"""
        changed = self._identity.changed_fields(identity)
        if changed:
            raise CheckpointMismatchError(
                f"模型标识/prompt 版本/研究请求/证据已变化（{', '.join(changed)}）："
                "请重新分析，不能继续旧研究"
            )
        return self

    def record(self, stage: str, value: Any) -> None:
        """写入中间态。最终报告严禁回写 —— 中间态与结论必须物理隔离。"""
        if not stage.strip():
            raise ValueError("stage 不能为空")
        if isinstance(value, FinalReport):
            raise TypeError("最终报告不能回写为检查点中间态")
        self._stages[stage] = value

    def intermediate(self, stage: str) -> IntermediateStage:
        if stage not in self._stages:
            raise KeyError(f"检查点没有阶段 {stage!r}（已有：{', '.join(self.stage_names) or '无'}）")
        return IntermediateStage(stage, self._stages[stage])

    def record_failure(self, stage: str, error: Any, content: Any = "") -> StageFailure:
        failure = StageFailure.build(stage, error, content)
        self._failures[stage] = failure
        return failure

    def public_failures(self) -> tuple[dict, ...]:
        """对外可见的失败明细：限长 + 脱敏 + 不带失败原文。"""
        return tuple(self._failures[name].to_dict() for name in sorted(self._failures))

    def finalize(self, report: Any, cards: Any) -> FinalReport:
        """引用校验通过后才产出最终报告；失败即抛 :class:`UnvalidatedIntermediateError`。"""
        verdict = validate_citations(report, cards)
        if not verdict.ok:
            self.record_failure("citation_validation", verdict.fallback or "引用校验失败")
            details = "；".join(f.detail for f in verdict.failures[:5])
            raise UnvalidatedIntermediateError(
                f"引用校验失败（{len(verdict.failures)} 条），降级结果："
                f"{verdict.fallback or QUANT_SNAPSHOT_FALLBACK}；明细：{details}"
            )
        claims = tuple(claim.to_dict() for claim in _extract_claims(report))
        return FinalReport(identity=self._identity, claims=claims, validation=verdict)

    def to_dict(self) -> dict:
        """序列化：只有身份 + 中间态 + 失败诊断，**永远不含最终报告**。"""
        return {
            "version": CHECKPOINT_VERSION,
            "identity": self._identity.to_dict(),
            "stages": {name: self._stages[name] for name in sorted(self._stages)},
            "failures": {name: self._failures[name].to_dict() for name in sorted(self._failures)},
        }

    @classmethod
    def from_dict(cls, payload: Mapping) -> ResearchCheckpoint:
        if int(payload.get("version", -1)) != CHECKPOINT_VERSION:
            raise ValueError(f"检查点版本不兼容：{payload.get('version')!r} != {CHECKPOINT_VERSION}")
        identity = CheckpointIdentity.from_dict(payload["identity"])
        stages = dict(payload.get("stages") or {})
        failures: dict[str, StageFailure] = {}
        for name, item in (payload.get("failures") or {}).items():
            # 已落盘的 error 是脱敏+限长后的产物，直接搬运，不再二次加截断后缀
            failures[name] = StageFailure(
                stage=name,
                error=str(item.get("error", "")),
                error_chars=int(item.get("error_chars", 0)),
            )
        if any(isinstance(v, FinalReport) for v in stages.values()):
            raise ValueError("检查点中不允许出现最终报告")
        return cls(identity, stages=stages, failures=failures)


def open_checkpoint(
    *,
    model_identity: str,
    prompt_version: str,
    request: Any,
    evidence: Any,
    previous: ResearchCheckpoint | None = None,
) -> ResearchCheckpoint:
    """打开/恢复检查点：四元组一致才复用，否则报错要求重新分析。"""
    if previous is not None:
        return previous.resume(
            CheckpointIdentity(
                model_identity=str(model_identity),
                prompt_version=str(prompt_version),
                request_fingerprint=compute_request_fingerprint(request),
                evidence_hash=evidence_digest(evidence),
            )
        )
    return ResearchCheckpoint(
        CheckpointIdentity(
            model_identity=str(model_identity),
            prompt_version=str(prompt_version),
            request_fingerprint=compute_request_fingerprint(request),
            evidence_hash=evidence_digest(evidence),
        )
    )


# ---------------------------------------------------------------------------
# 失败路径纪律：连接错误/取消不做 JSON 修复；失败输出限长脱敏
# ---------------------------------------------------------------------------


class LLMTransportError(RuntimeError):
    """连接层失败（DNS/连接/超时/HTTP 状态）。绝不等同于「格式不对」。"""


class LLMCancelledError(RuntimeError):
    """上游取消。取消是控制流信号，不是内容问题。"""


class LLMFormatError(ValueError):
    """模型**返回了内容但格式不对**。只有这一类才允许触发 JSON 修复。"""


_CANCEL_TYPES: tuple[type[BaseException], ...] = (
    KeyboardInterrupt,
    GeneratorExit,
    asyncio.CancelledError,
    concurrent.futures.CancelledError,
    LLMCancelledError,
)

_TRANSPORT_TYPES: tuple[type[BaseException], ...] = (
    LLMTransportError,
    urllib.error.URLError,  # HTTPError 是其子类：HTTP 状态错误同样不是格式错误
    TimeoutError,
    socket.timeout,
    ConnectionError,
    OSError,
)


def is_cancellation(error: BaseException) -> bool:
    return isinstance(error, _CANCEL_TYPES)


def is_transport_failure(error: BaseException) -> bool:
    return isinstance(error, _TRANSPORT_TYPES)


def should_attempt_json_repair(error: BaseException) -> bool:
    """格式修复白名单：先排除取消/连接失败，再要求是解析类错误。

    为什么用白名单而不是「不是取消就修」：未知异常（含程序 bug）必须原样
    冒泡，用一次额外的模型调用掩盖真实故障。
    """
    if is_cancellation(error) or is_transport_failure(error):
        return False
    return isinstance(error, (LLMFormatError, json.JSONDecodeError))


def guarded_repair(repair: Callable[[], Any], error: BaseException) -> Any:
    """失败路径闸门：只有格式错误才调用 ``repair``，否则把原错误抛回去。"""
    if not should_attempt_json_repair(error):
        raise error
    return repair()


_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"\bsk-[A-Za-z0-9._\-]{6,}"),
    re.compile(r"(?i)\b(api[_-]?key|apikey|access[_-]?token|auth[_-]?token|authorization|secret)\b\s*[:=]\s*[\"']?[^\s\"',}]+"),
)


def redact_secrets(text: str, *, extra_secrets: Iterable[str] = ()) -> str:
    """脱敏：常见密钥形态 + 调用方显式给出的字面值（如 ``LQ_LLM_API_KEY``）。"""
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(lambda m: f"{m.group(1)}=[REDACTED]" if m.groups() else "[REDACTED]", out)
    for secret in extra_secrets:
        if secret and len(secret) >= 6:
            out = out.replace(secret, "[REDACTED]")
    return out


def sanitize_failure_text(
    value: Any, *, max_chars: int = DEFAULT_FAILURE_MAX_CHARS, extra_secrets: Iterable[str] = ()
) -> str:
    """失败输出卫生：非 str 用 repr 兜底（报错路径不能再抛一次），限长 + 脱敏。"""
    if max_chars <= 0:
        raise ValueError(f"max_chars 必须为正整数，实际 {max_chars}")
    text = value if isinstance(value, str) else repr(value)
    text = redact_secrets(text, extra_secrets=extra_secrets)
    if len(text) > max_chars:
        text = text[:max_chars] + f"…[已截断，原长 {len(text)} 字符]"
    return text
