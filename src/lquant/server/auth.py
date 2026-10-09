"""开放接口 Token 鉴权 —— 规则表即唯一契约源。

## 为什么要有这个模块

lquant 有 183+ 条 API 路径，此前**完全没有鉴权**：``paper/service.py`` 的注释
自己就写着「/paper/accounts 是无鉴权 HTTP 端点」。本地单机自用可以接受，但
一旦要把能力开放给别的程序（或跑在共享机器上），任何本机进程都能改数据湖、
下模拟盘单、拉起 agent —— 这就是一个实打实的安全缺口。

## 三条设计红线（重点在「为什么不那样做」）

1. **默认拒绝（fail-closed）**：规则表里没有 (方法, 路径) 组合 = 不对外开放。
   反过来做（默认放行）的话，以后每新增一个端点都会**静默**变成公开面 ——
   漏一次就是一次泄漏；默认拒绝把出错方向调转过来：新端点默认关闭，想开放
   必须显式加规则，并同步 ``tests/unit/test_server_auth_contract.py`` 的快照。
2. **只认 ``Authorization: Bearer`` 与 ``X-API-Token`` 头，绝不接受 ``?token=``**。
   查询串会被 uvicorn/nginx 的 access log、浏览器历史、Referer 原样记下来 ——
   把长期凭证写进日志是最常见也最难回收的泄漏路径。参考实现支持 query 传参，
   那是为桌面单机（日志不出本机）设计的，这里刻意不抄。
3. **只存 SHA-256 哈希**：token 库文件被读走也无法反推明文，明文只在「创建」
   响应里出现一次。这里不做加盐/迭代 KDF —— 凭证是 32 字节密码学随机数，
   没有字典空间可爆破，成本花在「过期 + 吊销」上收益更大。

## 向后兼容（硬要求）

**没有 token 库文件 = 鉴权整体旁路**，行为与加这个模块之前完全一致
（既有 183+ 条端点的测试必须全绿）。token 文件一旦存在（哪怕里面是空列表），
就进入强制模式：受保护路径无凭证 401、scope 不足 403、未匹配 403。
「创建第一个 token」这一步就是开关本身 —— 少一个「开关开了但没配 token」
的中间态，就不会出现「以为开了其实没开」。

## 规则表顺序

``_RULES`` **顺序敏感，具体规则在前**。例如 ``POST /api/factors/evaluate``
（触发计算，run:backtest）必须排在宽泛的 ``POST /api/factors``（写因子定义，
write:data）之前，否则一个只有 write:data 的 token 就能触发集群计算。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from lquant.core.types import TZ, now_cn

logger = logging.getLogger(__name__)

# ── scope 全集 ────────────────────────────────────────────────────────────
# 划分依据是**副作用半径**，不是前端页面：
#   read:market   只读公开市场/参考数据 —— 泄露成本最低，可以给最不信任的调用方
#   read:factors  只读研究资产（因子/策略/回测/ML 结果）—— 是平台的核心产出
#   write:data    改数据湖/配置（采集、补数、同步、注册因子、改策略、改设置）
#   run:backtest  触发计算任务（回测/因子评价/挖掘/训练）—— 不写共享数据但耗资源
#   paper:trade   模拟盘读写一体：账本读与下单写分档没有实际收益，只会让调用方
#                 为了看持仓而拿到下单权，所以合成单一档
#   admin         管理面（token 管理 / 配置 / 监控 / 任务控制）—— 永不隐含，
#                 必须显式授予
SCOPES: tuple[str, ...] = (
    "read:market",
    "read:factors",
    "write:data",
    "run:backtest",
    "paper:trade",
    "admin",
)

# 仅需「持有有效 Token」、不校验具体 scope。只给存活探针用：探活不该顺带
# 授予任何数据读取权（最小权限）。
SCOPE_ANY = "*"

TOKEN_PREFIX = "lq_"

# (方法, 路径前缀, 所需 scope)；方法 "*" = 任意方法。
# scope 为 None = 显式不开放（用于把个别子路径从宽泛前缀里挖掉）。
_RULES: list[tuple[str, str, str | None]] = [
    # ── admin：管理面 ────────────────────────────────────────────────────
    # token 管理端点自身只能是 admin —— 普通 token 永远进不去管理面，
    # 否则「给自己提权」就是一条两步走的漏洞。
    ("*", "/api/auth", "admin"),
    ("*", "/api/settings", "admin"),
    # 监控含应用日志/错误堆栈，属运维面而非业务面
    ("*", "/api/monitor", "admin"),
    # 通用任务控制（cancel/retry）会掐掉别人正在跑的计算
    ("POST", "/api/tasks", "admin"),

    # ── paper:trade：模拟盘读写一体（读+写共用一档）────────────────────
    ("*", "/api/paper", "paper:trade"),

    # ── run:backtest：触发计算（会生成新产物 / 占资源）─────────────────
    ("POST", "/api/backtests", "run:backtest"),
    ("POST", "/api/factors/evaluate", "run:backtest"),
    ("POST", "/api/factors/mine", "run:backtest"),
    ("POST", "/api/factors/analyze", "run:backtest"),
    ("POST", "/api/factors/synthesize", "run:backtest"),
    ("POST", "/api/portfolio", "run:backtest"),
    ("POST", "/api/qlib", "run:backtest"),
    ("POST", "/api/analyses", "run:backtest"),
    ("POST", "/api/ml/train", "run:backtest"),
    ("POST", "/api/ml/retrain", "run:backtest"),
    ("POST", "/api/ml/predict", "run:backtest"),
    ("POST", "/api/ml/jobs", "run:backtest"),

    # ── read:factors：用 POST 传 body 但**无副作用**的只读计算 ──────────
    # 必须排在下面的宽泛 POST /api/factors（write:data）之前：AST/求值追踪/
    # 校验都不落库，为一次纯校验申请写权限是权限放大。
    ("POST", "/api/factors/ast", "read:factors"),
    ("POST", "/api/factors/trace", "read:factors"),
    ("POST", "/api/factors/validate", "read:factors"),
    ("POST", "/api/strategies/validate", "read:factors"),

    # ── write:data：写数据湖 / 改配置 ──────────────────────────────────
    ("POST", "/api/market/collect", "write:data"),
    ("POST", "/api/market/backfill", "write:data"),
    ("POST", "/api/data", "write:data"),
    ("DELETE", "/api/data", "write:data"),
    # 扩展数据表（BYO 数据）：写/上传/回补/注册因子都改数据语义 → write:data。
    # 显式登记而不是靠宽泛前缀兜底：否则一旦将来加了别的前缀规则，
    # 这些端点会静默变成「默认拒绝」，调用方只会看到 401 而不知道为什么。
    ("POST", "/api/ext-data", "write:data"),
    ("DELETE", "/api/ext-data", "write:data"),
    ("POST", "/api/news", "write:data"),
    ("POST", "/api/sync", "write:data"),
    ("DELETE", "/api/sync", "write:data"),
    ("POST", "/api/fundamental", "write:data"),
    ("POST", "/api/notify", "write:data"),
    ("PATCH", "/api/notify", "write:data"),
    ("DELETE", "/api/notify", "write:data"),
    ("POST", "/api/watchlist", "write:data"),
    ("DELETE", "/api/watchlist", "write:data"),
    ("POST", "/api/strategies", "write:data"),
    ("PUT", "/api/strategies", "write:data"),
    ("DELETE", "/api/strategies", "write:data"),
    ("PUT", "/api/analyses", "write:data"),
    ("DELETE", "/api/analyses", "write:data"),
    ("POST", "/api/factors", "write:data"),
    ("PUT", "/api/factors", "write:data"),
    ("DELETE", "/api/factors", "write:data"),
    # 模型晋级/回滚是改变生产指向的写操作，不能混进「跑一次训练」这一档
    ("POST", "/api/ml/models", "write:data"),
    ("PUT", "/api/qlib", "write:data"),

    # ── read:factors：研究资产只读 ─────────────────────────────────────
    ("GET", "/api/backtests", "read:factors"),
    ("GET", "/api/factors", "read:factors"),
    ("GET", "/api/strategies", "read:factors"),
    ("GET", "/api/analyses", "read:factors"),
    ("GET", "/api/portfolio", "read:factors"),
    ("GET", "/api/qlib", "read:factors"),
    ("GET", "/api/ml", "read:factors"),
    ("GET", "/api/security", "read:factors"),
    ("GET", "/api/notify", "read:factors"),
    ("GET", "/api/tasks", "read:factors"),

    # ── read:market：行情与参考数据只读 ────────────────────────────────
    ("*", "/api/health", SCOPE_ANY),
    ("GET", "/api/market", "read:market"),
    ("GET", "/api/data", "read:market"),
    ("GET", "/api/ext-data", "read:market"),
    ("GET", "/api/etf", "read:market"),
    ("GET", "/api/news", "read:market"),
    ("GET", "/api/fundamental", "read:market"),
    ("GET", "/api/watchlist", "read:market"),
    ("GET", "/api/sync", "read:market"),
]

# 刻意**不放进规则表**（= 默认拒绝）的面：
#   /api/agent/*、/api/ask/*  —— 会拉起本机 CLI 执行代码，等于远程命令执行入口，
#                                不是「一个 API token」应该打开的东西
#   /a2a、/.well-known/*      —— A2A 有自己的 Bearer 校验（api/a2a.py），网关
#                                再拦一层会改变对外 A2A 契约
#   /ws/*                     —— WebSocket，本中间件只处理 http scope


# ── token 库位置 ──────────────────────────────────────────────────────────


def _data_dir() -> Path:
    """数据根目录（与 config/app.yaml 的 ``${LQ_DATA_DIR:./data}`` 同源）。

    相对路径一律锚定到仓库根：lquant 有过「CWD 相对路径导致长出第二棵树/
    第二个库」的事故记录（docs/DATA_ROBUSTNESS_AUDIT-2026-10-03.md），
    凭证文件尤其不能因为换目录启动就"找不到 → 静默变成无鉴权"。
    """
    env = os.getenv("LQ_DATA_DIR")
    if env:
        raw = Path(env).expanduser()
        return raw if raw.is_absolute() else _root() / raw
    try:
        from lquant.core.config import get_settings

        cfg = get_settings()
        d = Path(str((cfg.raw.get("paths", {}) or {}).get("data_dir", "./data"))).expanduser()
        return d if d.is_absolute() else cfg.root / d
    except Exception as e:  # noqa: BLE001 - 配置读不到也要给出确定路径
        logger.warning("数据根解析失败，回退 <root>/data: %s", e)
        return _root() / "data"


def _root() -> Path:
    try:
        from lquant.core.config import find_root

        return find_root()
    except Exception:  # noqa: BLE001
        return Path.cwd()


def tokens_path() -> Path:
    """token 库路径：``LQ_API_TOKENS_PATH`` 优先（测试/多实例隔离），
    否则 ``<数据根>/api_tokens.json``。"""
    explicit = os.getenv("LQ_API_TOKENS_PATH")
    if explicit:
        return Path(explicit).expanduser()
    return _data_dir() / "api_tokens.json"


# ── 规则裁决（单一入口）──────────────────────────────────────────────────


def _under(path: str, prefix: str) -> bool:
    """路径前缀匹配，且必须落在 ``/`` 段边界上。

    不然 ``/api/data`` 会连带命中 ``/api/database-evil`` —— 前缀匹配不按段
    切分是最经典的鉴权绕过。
    """
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def _is_stream_endpoint(method: str, path: str) -> bool:
    """SSE 流端点显式不开放。

    浏览器的 ``EventSource`` 不能自定义请求头，规则表放它进来只会得到一个
    「看起来开放、实际永远 401」的假开放面。要做的话得先有「短期票据」机制
    （参见 TSP 的 /api/events/ticket），那是后续的事，这里先诚实地关掉。
    """
    return method.upper() == "GET" and path.startswith("/api/data/tasks/") \
        and path.endswith("/events")


def required_scope(method: str, path: str) -> str | None:
    """(方法, 路径) → 所需 scope；``None`` = 不对外开放（默认拒绝）。

    返回值可能是 :data:`SCOPE_ANY`（仅需有效 Token）。
    这是开放面的**唯一**判定函数：OpenAPI 契约视图与快照测试都调它，
    保证「规则表 = 契约」只有一个源。
    """
    method = method.upper()
    if _is_stream_endpoint(method, path):
        return None
    for m, prefix, scope in _RULES:
        if m != "*" and m != method:
            continue
        if _under(path, prefix):
            return scope
    return None


# ── Token 记录与存储 ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class TokenRecord:
    id: str
    name: str
    token_hash: str
    scopes: tuple[str, ...]
    created_at: str = ""
    expires_at: str | None = None
    revoked: bool = False

    def active(self, now: datetime) -> bool:
        """吊销 / 过期一律失效；过期字段解析不出来也按失效处理（fail-closed）。"""
        if self.revoked:
            return False
        if not self.expires_at:
            return True
        exp = _parse_ts(self.expires_at)
        return exp is not None and exp > now


def _parse_ts(raw: str) -> datetime | None:
    """ISO 8601 → tz-aware。无时区的按平台时区（Asia/Shanghai）解释。

    仓库硬约束是「时间统一 ISO 8601 + Asia/Shanghai」，所以 naive 值按本平台
    时区补齐；补 UTC 会让「今晚 24 点过期」实际晚 8 小时。
    """
    try:
        dt = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=TZ)


@dataclass(frozen=True)
class TokenStore:
    enabled: bool          # 库文件是否存在 = 鉴权开关
    records: tuple[TokenRecord, ...]
    path: Path


def load_store() -> TokenStore:
    """读 token 库。

    **不做进程内缓存**：吊销/过期必须在下一个请求立刻生效，「缓存的凭证」
    是安全模块最不该省的一次磁盘读。文件通常只有几 KB，代价可接受。
    解析失败 → enabled=True + 零有效凭证 = 全部 401（宁可直接不可用，
    也不要在凭证文件损坏时静默放行）。
    """
    p = tokens_path()
    if not p.exists():
        return TokenStore(enabled=False, records=(), path=p)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        items = raw.get("tokens", []) if isinstance(raw, dict) else []
        records = tuple(_to_record(x) for x in items if isinstance(x, dict))
    except Exception as e:  # noqa: BLE001 - 坏文件按最严处理
        logger.error("token 库 %s 解析失败，鉴权按最严处理（全部拒绝）: %s", p, e)
        return TokenStore(enabled=True, records=(), path=p)
    return TokenStore(enabled=True, records=records, path=p)


def _to_record(raw: dict) -> TokenRecord:
    scopes = raw.get("scopes") or []
    return TokenRecord(
        id=str(raw.get("id", "")),
        name=str(raw.get("name", "")),
        token_hash=str(raw.get("token_hash", "")),
        scopes=tuple(str(s) for s in scopes),
        created_at=str(raw.get("created_at", "")),
        expires_at=raw.get("expires_at"),
        revoked=bool(raw.get("revoked", False)),
    )


def _hash(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _match_token(records: tuple[TokenRecord, ...], plaintext: str,
                 now: datetime) -> TokenRecord | None:
    """按哈希查记录，用 compare_digest 定时比较（不给旁路计时信号）。"""
    digest = _hash(plaintext)
    for r in records:
        if hmac.compare_digest(r.token_hash, digest) and r.active(now):
            return r
    return None


# ── 裁决结果 ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Decision:
    """status=None 放行；401 未认证；403 已认证但无权（含未开放路径）。"""

    status: int | None
    detail: str = ""
    record: TokenRecord | None = None
    required: str | None = None

    @property
    def allowed(self) -> bool:
        return self.status is None


def extract_token(headers: Headers | dict) -> str | None:
    """从请求头取凭证：``Authorization: Bearer <t>`` 或 ``X-API-Token: <t>``。

    **不看 query string** —— 见模块头的红线 2。
    """
    h = Headers(headers=headers) if isinstance(headers, dict) else headers
    auth = h.get("authorization") or ""
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip() or None
    x = (h.get("x-api-token") or "").strip()
    return x or None


def evaluate(path: str, method: str, token: str | None, *,
             store: TokenStore | None = None) -> Decision:
    """唯一裁决函数：``evaluate(path, method, token) -> Decision``。

    顺序刻意是「先认证、再鉴权」：未带凭证的探测不该知道某个路径是否存在
    规则（否则 403/401 的差别就是一个免费的路径枚举 oracle）。
    """
    st = store if store is not None else load_store()
    if not st.enabled:
        # 未配置 token：整体旁路，行为与加鉴权之前逐字节一致。
        return Decision(None)

    if not token:
        return Decision(401, "缺少 API Token（请用 Authorization: Bearer <token> "
                             "或 X-API-Token: <token> 请求头）")

    record = _match_token(st.records, token, now_cn())
    if record is None:
        # 无效 / 吊销 / 过期合并成同一句话：区分它们对攻击者是免费信息。
        return Decision(401, "API Token 无效、已吊销或已过期")

    need = required_scope(method, path)
    if need is None:
        return Decision(403, "该端点未对外开放", record=record)
    if need != SCOPE_ANY and need not in record.scopes:
        # 只回「需要什么 scope」（开放契约的一部分，快照里公开可见），
        # 不回 token 的 id/名称/已持有 scope —— 那些是内部细节。
        return Decision(403, f"权限不足：需要 scope {need}", record=record, required=need)
    return Decision(None, record=record, required=need)


# ── 管理面：token 生成 / 吊销 / 列出 ──────────────────────────────────────

_LOCK = threading.Lock()  # 读-改-写互斥（进程内；单机单进程部署足够）


def _save(records: list[dict]) -> None:
    """原子落盘：半写的凭证文件 = 全量失联或全量放行，两者都不可接受。

    同目录临时文件 + ``os.replace``（同文件系统内原子），并尽力收紧到 0600。
    """
    p = tokens_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"tokens": records}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError as e:  # Windows/特殊 FS 权限模型不同，尽力而为
        logger.debug("token 库 chmod 0600 失败（忽略）: %s", e)


def _read_raw() -> list[dict]:
    p = tokens_path()
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return [x for x in raw.get("tokens", []) if isinstance(x, dict)]
    except Exception as e:  # noqa: BLE001 - 读失败按空处理；鉴权侧仍会 fail-closed
        logger.error("token 库读取失败: %s", e)
        return []


def list_tokens() -> list[dict]:
    """管理视图：**永不回显哈希**，更不回显明文。"""
    return [{
        "id": r.id,
        "name": r.name,
        "scopes": list(r.scopes),
        "created_at": r.created_at,
        "expires_at": r.expires_at,
        "revoked": r.revoked,
    } for r in (_to_record(x) for x in _read_raw())]


def create_token(name: str, scopes: list[str], expires_at: str | None = None) -> tuple[dict, str]:
    """创建 token，返回 (管理视图, 明文)。**明文只此一次**，调用方负责展示。"""
    bad = sorted({s for s in scopes if s not in SCOPES})
    if bad:
        raise ValueError(f"未知 scope: {bad}（可用: {list(SCOPES)}）")
    if not scopes:
        # 无 scope 的 token 永远 403，是纯粹的无效凭证 —— 直接拒绝创建，
        # 免得调用方拿到一把打不开任何门的钥匙还以为配错了服务。
        raise ValueError("至少要给一个 scope，否则该 Token 无法访问任何端点")
    if expires_at is not None and _parse_ts(expires_at) is None:
        raise ValueError(f"expires_at 不是合法 ISO 8601 时间: {expires_at!r}")

    plaintext = TOKEN_PREFIX + secrets.token_hex(32)
    record = {
        "id": f"tok_{secrets.token_hex(6)}",
        "name": (name or "").strip() or "未命名",
        "token_hash": _hash(plaintext),
        "scopes": sorted(set(scopes)),
        "created_at": now_cn().isoformat(timespec="seconds"),
        "expires_at": expires_at,
        "revoked": False,
    }
    with _LOCK:
        raw = _read_raw()
        raw.append(record)
        _save(raw)
    return {k: v for k, v in record.items() if k != "token_hash"}, plaintext


def revoke_token(token_id: str) -> bool:
    """吊销（打标记而非删记录：留痕，且哈希仍在库里能被审计到）。"""
    with _LOCK:
        raw = _read_raw()
        hit = False
        for r in raw:
            if r.get("id") == token_id:
                r["revoked"] = True
                hit = True
        if hit:
            _save(raw)
    return hit


# ── 中间件：一行接线，不改任何端点 ────────────────────────────────────────


class ApiTokenMiddleware:
    """纯 ASGI 中间件。

    为什么不用 ``BaseHTTPMiddleware``：仓库里 MonitorMiddleware 已经因为
    「流式响应 / 后台任务语义」明确避开了它，鉴权拦在更外层，同样不能把
    SSE 或流式下载搅成一次性读取。
    """

    def __init__(self, app) -> None:  # noqa: ANN001 - ASGI callable
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method", "GET")).upper()
        # CORS 预检（OPTIONS）不带凭证，浏览器也不会给它附 Authorization；
        # 拦下来会让所有跨域请求一起挂掉。预检本身不触达业务逻辑。
        if method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        store = load_store()
        if not store.enabled:
            await self.app(scope, receive, send)
            return

        token = extract_token(Headers(scope=scope))
        decision = evaluate(str(scope.get("path", "")), method, token, store=store)
        if decision.allowed:
            await self.app(scope, receive, send)
            return

        headers = {"WWW-Authenticate": "Bearer"} if decision.status == 401 else {}
        response = JSONResponse({"detail": decision.detail},
                                status_code=decision.status or 403, headers=headers)
        await response(scope, receive, send)


# ── 管理端点（同样由上面的规则表保护：/api/auth 需要 admin）──────────────


router = APIRouter(prefix="/auth", tags=["auth"])


class TokenCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    scopes: list[str]
    expires_at: str | None = None


@router.get("/tokens")
def list_tokens_api() -> dict:
    """列出 token（只回 id/name/scopes/时间戳，绝不回显哈希或明文）。"""
    st = load_store()
    return {"auth_enabled": st.enabled, "path": str(st.path), "tokens": list_tokens()}


@router.post("/tokens", status_code=201)
def create_token_api(req: TokenCreateRequest) -> dict:
    """创建 token。响应里的 ``token`` 是明文，**只出现这一次**。

    引导语义：还没有 token 库时本端点开放（否则永远拿不到第一个凭证）；
    库一旦存在，本端点就只认 admin —— 即「创建第一个 token」就是开启鉴权。
    """
    try:
        view, plaintext = create_token(req.name, req.scopes, req.expires_at)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return {"token": plaintext,
            "note": "明文只在此响应出现一次，请立即保存；服务端只存 SHA-256 哈希",
            **view}


@router.delete("/tokens/{token_id}")
def revoke_token_api(token_id: str) -> dict:
    if not revoke_token(token_id):
        raise HTTPException(404, f"未找到 Token: {token_id}")
    return {"revoked": True, "id": token_id}
