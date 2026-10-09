"""扩展点契约化：窄契约 + 版本号 + 装载失败隔离 + 删目录即卸载。

为什么要有这个模块
------------------
lquant 已有 EP-1..EP-12 的扩展点清单（``docs/EXTENSION_POINTS.md``），但「扩展」
一直是「往主仓加文件」：没有版本号、没有失败隔离。一个扩展 import 出错或
``setup`` 抛异常，就能让整个应用起不来；也没有明确的装载/卸载语义。

这里把四条机制固化成底座（参考 tick-stock-panel ``backend/app/extensions/``，MIT，
按 lquant 风格重写）：

1. **窄契约**：扩展只能拿到 :class:`ExtensionContext`（``data_dir`` + 只读仓储），
   拿不到 app / settings / service / db。契约窄是特性 —— 以后加宽容易，收窄会
   破坏所有人。``frozen + slots`` 让「顺手塞个 app 进去」当场报错而不是悄悄变宽。
2. **版本号**：``EXTENSION_API_VERSION``，扩展声明同名常量；不符即跳过 + warning，
   不抛异常。
3. **失败隔离**：import / setup / startup 的任何异常都收敛成报告里的 ``failed``，
   不影响应用启动，也不影响其他扩展。
4. **删目录即卸载**：每次 :func:`load_extensions` 都从目录重新扫描、重新 import，
   注册表是**本次调用的返回值**而不是进程级全局单例；同时清掉本机制用过的
   ``sys.modules`` 缓存条目，保证「目录现状 == 装载结果」。装载采用 staging +
   commit：失败不提交，不会留下半成品。

日志用 stdlib ``logging``：``lquant.core.logging.InterceptHandler`` 会把它转发进
统一的 loguru 管道，同时 pytest 的 ``caplog`` 能直接断言 warning。
"""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import logging
import os
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from lquant.core.config import find_root

logger = logging.getLogger(__name__)

EXTENSION_API_VERSION = 1
"""扩展契约版本。扩展模块必须声明同名整数常量，否则按 fail-closed 跳过。"""

EXTENSIONS_DIRNAME = "extensions"
"""仓库约定的扩展目录名（``<仓库根>/extensions``）。"""

EXTENSION_MODULE_PREFIX = "lquant_ext_"
"""扩展模块在 ``sys.modules`` 里的前缀 —— 卸载时按它精准清理。"""

_TEMPLATE_PREFIXES = (".", "_")

__all__ = [
    "EXTENSION_API_VERSION",
    "EXTENSIONS_DIRNAME",
    "ExtensionContext",
    "ExtensionLoadReport",
    "ExtensionRegistrar",
    "ExtensionRegistry",
    "ExtensionRoute",
    "FailedExtension",
    "LoadedExtension",
    "ReadOnlyRepository",
    "SkippedExtension",
    "default_extensions_dir",
    "load_extensions",
]


# ── 窄契约 ────────────────────────────────────────────────────────────────


class ReadOnlyRepository(Protocol):
    """扩展可见的仓储面：**只读**。

    故意不提供 write / update / delete —— 扩展要写数据必须走核心的显式入口，
    否则「删目录即卸载」会因为扩展留下的数据而名不副实。

    这里只声明两个最小读取能力；真实实现由调用方注入（loader 不依赖 data/ 包，
    避免 core 被数据层反向耦合）。新增方法 = 加宽契约，是安全方向。
    """

    def list_securities(self) -> Sequence[str]: ...

    def get_security(self, symbol: str) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True)
class ExtensionContext:
    """扩展在 ``startup`` 拿到的**全部**核心能力：刻意窄。

    ``frozen`` + 手写 ``__slots__``：任何赋值（含未知字段）都抛
    ``FrozenInstanceError``，读未知字段抛 ``AttributeError`` —— 想往上下文里塞
    app / settings / service 会当场报错，而不是让契约悄悄变宽。

    为什么手写 ``__slots__`` 而不是 ``slots=True``：``dataclasses`` 在
    ``frozen=True, slots=True`` 时会重建类，给未知字段赋值会抛出语义混乱的
    ``TypeError: super(type, obj)``。手写 slots 后所有写入路径统一为
    FrozenInstanceError。
    """

    __slots__ = ("api_version", "data_dir", "repository")

    api_version: int
    data_dir: Path
    repository: ReadOnlyRepository | None


# ── staging：扩展唯一的写入口 ─────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ExtensionRoute:
    """一条扩展路由的声明（接入方决定怎么挂，例如 FastAPI include_router）。"""

    method: str
    path: str
    handler: Callable[..., Any]
    name: str


class ExtensionRegistrar:
    """扩展 ``setup(registrar)`` 里唯一能拿到的写入口，而且**只写到 staging**。

    刻意不提供 ``commit()`` —— 提交权在装载器/注册表手里。扩展不能自己宣布成功，
    所以「注册到一半抛错」只会丢弃 staging，不会污染进程状态。
    """

    __slots__ = ("_api_version", "_name", "_routes")

    def __init__(self, name: str, api_version: int) -> None:
        self._name = name
        self._api_version = api_version
        self._routes: list[ExtensionRoute] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def api_version(self) -> int:
        return self._api_version

    @property
    def routes(self) -> tuple[ExtensionRoute, ...]:
        return tuple(self._routes)

    def add_route(
        self,
        path: str,
        *,
        handler: Callable[..., Any],
        method: str = "GET",
        name: str | None = None,
    ) -> ExtensionRoute:
        """登记一条路由（staging）。

        参数不合法直接 ``ValueError``：会被装载器收敛成该扩展的 ``failed``，
        只影响它自己 —— 这正是「fail-loudly 但不静默、也不牵连全局」。
        """
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError(f"扩展 {self._name!r} 的路由 path 必须以 / 开头：{path!r}")
        if any(ch.isspace() for ch in path):
            raise ValueError(f"扩展 {self._name!r} 的路由 path 不能含空白：{path!r}")
        if not callable(handler):
            raise ValueError(f"扩展 {self._name!r} 路由 {path!r} 的 handler 必须可调用")
        verb = str(method).strip().upper()
        if not verb:
            raise ValueError(f"扩展 {self._name!r} 路由 {path!r} 缺少 method")
        normalized = _normalize_path(path)
        route = ExtensionRoute(
            method=verb,
            path=normalized,
            handler=handler,
            name=name or f"{self._name}:{verb} {normalized}",
        )
        key = (route.method, route.path)
        if any((r.method, r.path) == key for r in self._routes):
            raise ValueError(f"扩展 {self._name!r} 内部重复登记路由 {verb} {normalized}")
        self._routes.append(route)
        return route


# ── 报告 ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LoadedExtension:
    name: str
    module: str
    api_version: int
    path: Path
    routes: tuple[ExtensionRoute, ...]


@dataclass(frozen=True, slots=True)
class SkippedExtension:
    """契约不符或冲突 —— 这是**预期**结果，不是故障（所以不抛、也不进 failed）。"""

    name: str
    reason: str


@dataclass(frozen=True, slots=True)
class FailedExtension:
    """扩展自己抛了异常。已被隔离：应用照常启动，其他扩展照常加载。"""

    name: str
    reason: str


class ExtensionRegistry:
    """一次装载的结果注册表：名称 + 路由索引。

    **进程级没有单例** —— 删掉目录再 ``load_extensions`` 就是卸载，不会残留。
    """

    def __init__(
        self,
        *,
        reserved_names: Iterable[str] = (),
        reserved_routes: Iterable[tuple[str, str]] = (),
    ) -> None:
        self._reserved_names = frozenset(reserved_names)
        self._reserved_routes = {
            (str(m).strip().upper(), _normalize_path(p)) for m, p in reserved_routes
        }
        self._entries: dict[str, LoadedExtension] = {}
        self._route_owner: dict[tuple[str, str], str] = {}

    def names(self) -> list[str]:
        return sorted(self._entries)

    def all_routes(self) -> tuple[ExtensionRoute, ...]:
        """所有已装载路由，按扩展名排序 —— 接入方（server）据此挂载。"""
        return tuple(r for name in self.names() for r in self._entries[name].routes)

    def route_owner(self) -> dict[tuple[str, str], str]:
        """``(method, path) -> 扩展名`` 的索引快照（排查冲突时直接看它）。"""
        return dict(self._route_owner)

    def get(self, name: str) -> LoadedExtension:
        try:
            return self._entries[name]
        except KeyError as exc:
            raise KeyError(f"扩展 {name!r} 未装载，当前已装载：{self.names()}") from exc

    def conflict_reason(self, registrar: ExtensionRegistrar) -> str | None:
        """返回冲突原因；无冲突返回 ``None``。冲突 = 跳过后者，不是异常。"""
        name = registrar.name
        if name in self._reserved_names:
            return f"名称 {name!r} 是核心保留名，扩展不得占用"
        if name in self._entries:
            return f"名称 {name!r} 已被扩展 {self._entries[name].module} 占用"
        for route in registrar.routes:
            key = (route.method, route.path)
            if key in self._reserved_routes:
                return f"路由 {route.method} {route.path} 是核心保留路由，扩展不得占用"
            owner = self._route_owner.get(key)
            if owner is not None:
                return f"路由 {route.method} {route.path} 已被扩展 {owner!r} 占用"
        return None

    def commit(self, registrar: ExtensionRegistrar, *, module: str, path: Path) -> LoadedExtension:
        """提交一个已通过校验的 staging —— 这是唯一的「注册发生」时刻。"""
        entry = LoadedExtension(
            name=registrar.name,
            module=module,
            api_version=registrar.api_version,
            path=path,
            routes=registrar.routes,
        )
        self._entries[entry.name] = entry
        for route in entry.routes:
            self._route_owner[(route.method, route.path)] = entry.name
        return entry

    def describe(self) -> list[dict[str, Any]]:
        """自省：给 CLI / 前端枚举用。"""
        return [
            {
                "name": entry.name,
                "module": entry.module,
                "api_version": entry.api_version,
                "path": str(entry.path),
                "routes": [f"{r.method} {r.path}" for r in entry.routes],
            }
            for entry in (self._entries[n] for n in self.names())
        ]

    def __contains__(self, name: object) -> bool:
        return name in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        return iter(self.names())


@dataclass(frozen=True, slots=True)
class ExtensionLoadReport:
    """装载报告。字段即事实：loaded / skipped / failed 三态各自带原因。"""

    root: Path
    loaded: tuple[LoadedExtension, ...]
    skipped: tuple[SkippedExtension, ...]
    failed: tuple[FailedExtension, ...]
    # 注册表不参与相等比较：同样目录装载两次，报告应当相等。
    registry: ExtensionRegistry = field(compare=False, repr=False)

    @property
    def ok(self) -> bool:
        """只看 ``failed``：跳过是契约/冲突的预期结果；零扩展也算 ok（扩展是可选能力）。"""
        return not self.failed

    def names(self) -> list[str]:
        return [entry.name for entry in self.loaded]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "ok": self.ok,
            "loaded": [
                {
                    "name": e.name,
                    "module": e.module,
                    "api_version": e.api_version,
                    "path": str(e.path),
                    "routes": [f"{r.method} {r.path}" for r in e.routes],
                }
                for e in self.loaded
            ],
            "skipped": [{"name": e.name, "reason": e.reason} for e in self.skipped],
            "failed": [{"name": e.name, "reason": e.reason} for e in self.failed],
        }

    def summary(self) -> str:
        """人读摘要（CLI 直接打这个）。"""
        lines = [
            f"扩展目录: {self.root}",
            f"loaded({len(self.loaded)}) / skipped({len(self.skipped)}) / failed({len(self.failed)})",
        ]
        for item in self.loaded:
            routes = ", ".join(f"{r.method} {r.path}" for r in item.routes) or "-"
            lines.append(
                f"  + {item.name} api={item.api_version} module={item.module} routes=[{routes}]"
            )
        for skipped in self.skipped:
            lines.append(f"  - {skipped.name}: {skipped.reason}")
        for failed in self.failed:
            lines.append(f"  ! {failed.name}: {failed.reason}")
        return "\n".join(lines)


# ── 装载器 ────────────────────────────────────────────────────────────────


class _SkipExtension(Exception):
    """内部控制流：契约不符 / 冲突 —— 归入 skipped，不当作故障。"""


def default_extensions_dir() -> Path:
    """仓库约定的扩展目录 ``<仓库根>/extensions``。

    仓库根复用 ``core.config.find_root()``：只有一处定义，才不会出现
    「CLI 找 A 目录、服务找 B 目录」。
    """
    return find_root() / EXTENSIONS_DIRNAME


def load_extensions(
    root: str | Path | None = None,
    *,
    context: ExtensionContext | None = None,
    data_dir: str | Path | None = None,
    repository: ReadOnlyRepository | None = None,
    reserved_names: Iterable[str] = (),
    reserved_routes: Iterable[tuple[str, str]] = (),
) -> ExtensionLoadReport:
    """扫描并装载一个扩展目录，返回本次装载报告。

    - ``root`` 默认 :func:`default_extensions_dir`；相对路径锚定仓库根。
    - ``context`` 显式给出时忽略 ``data_dir`` / ``repository``；否则用它们构造。
    - ``reserved_names`` / ``reserved_routes`` 是核心保留位，扩展撞上即跳过。
    - 目录不存在或没有扩展 → 空报告，**不算失败**。
    - 每个扩展要么整体生效、要么整体不生效：setup/startup 抛错、名称/路由冲突
      都在提交前拦下，注册表不留半成品。
    - 在同一进程里重复调用，拿到的是**当前目录的快照** —— 这就是「删目录即卸载」。
    """
    ext_root = Path(root) if root is not None else default_extensions_dir()
    ext_root = ext_root.expanduser()
    if not ext_root.is_absolute():
        ext_root = (find_root() / ext_root).resolve()

    ctx = (
        context
        if context is not None
        else ExtensionContext(
            api_version=EXTENSION_API_VERSION,
            data_dir=Path(data_dir).expanduser() if data_dir is not None else _default_data_dir(),
            repository=repository,
        )
    )
    if ctx.api_version != EXTENSION_API_VERSION:
        # 这是调用方的编程错误（不是扩展的错），必须炸出来。
        raise ValueError(f"上下文契约版本 {ctx.api_version} != 核心支持的 {EXTENSION_API_VERSION}")

    registry = ExtensionRegistry(reserved_names=reserved_names, reserved_routes=reserved_routes)
    loaded: list[LoadedExtension] = []
    skipped: list[SkippedExtension] = []
    failed: list[FailedExtension] = []

    _purge_extension_modules()
    discovered = _discover(ext_root)
    if not discovered:
        logger.info("扩展目录 %s 不存在或为空：按零扩展启动", ext_root)

    for name, entry in discovered:
        # 每个扩展独立 try：一个坏扩展只能弄坏自己。
        try:
            if not name.isidentifier():
                raise _SkipExtension(
                    f"扩展名 {name!r} 不是合法 Python 标识符（把目录改成合法名字即可）"
                )
            module = _load_module(name, entry)
            declared = getattr(module, "EXTENSION_API_VERSION", None)
            if not isinstance(declared, int) or isinstance(declared, bool):
                raise _SkipExtension(
                    "未声明整数 EXTENSION_API_VERSION：无法确认契约版本，按 fail-closed 跳过"
                )
            if declared != EXTENSION_API_VERSION:
                raise _SkipExtension(f"契约版本 {declared} != 核心支持的 {EXTENSION_API_VERSION}")
            declared_id = getattr(module, "EXTENSION_ID", None)
            ext_name = (
                declared_id.strip()
                if isinstance(declared_id, str) and declared_id.strip()
                else name
            )
            setup = getattr(module, "setup", None)
            if not callable(setup):
                raise ValueError("扩展必须定义 setup(registrar)")
            registrar = ExtensionRegistrar(ext_name, declared)
            setup(registrar)
            conflict = registry.conflict_reason(registrar)
            if conflict is not None:
                raise _SkipExtension(conflict)
            startup = getattr(module, "startup", None)
            if callable(startup):
                startup(ctx)
            # 到这里才提交：前面任何一步失败，注册表都保持干净。
            loaded.append(registry.commit(registrar, module=module.__name__, path=entry))
        except _SkipExtension as exc:
            skipped.append(SkippedExtension(name, str(exc)))
            logger.warning("扩展 %s 已跳过：%s", name, exc)
        except Exception as exc:  # noqa: BLE001 - 隔离就是契约：坏扩展不许弄挂应用
            failed.append(FailedExtension(name, f"{type(exc).__name__}: {exc}"))
            logger.warning("扩展 %s 装载失败（已隔离，应用照常启动）：%s", name, exc, exc_info=True)

    return ExtensionLoadReport(
        root=ext_root,
        loaded=tuple(loaded),
        skipped=tuple(skipped),
        failed=tuple(failed),
        registry=registry,
    )


def _default_data_dir() -> Path:
    """默认数据根：``LQ_DATA_DIR``（与 ``config/app.yaml`` 同源），否则 ``<仓库根>/data``。

    相对路径锚定仓库根 —— lquant 有过「CWD 相对路径长出第二棵树」的事故，
    上下文里的路径必须确定。
    """
    env = os.getenv("LQ_DATA_DIR")
    if env:
        raw = Path(env).expanduser()
        return raw if raw.is_absolute() else (find_root() / raw).resolve()
    return find_root() / "data"


def _normalize_path(path: str) -> str:
    """去掉末尾斜杠（保留根 ``/``）：冲突判定用同一把尺子。"""
    return str(path).rstrip("/") or "/"


def _discover(root: Path) -> list[tuple[str, Path]]:
    """扫描扩展目录，返回 ``(扩展名, 入口文件)``，按名字排序（装载顺序确定）。

    约定：
    - ``<root>/<name>/__init__.py`` 包式扩展（可带同目录辅助模块，用相对 import）；
    - ``<root>/<name>.py``          单文件扩展；
    - ``.`` / ``_`` 开头一律不扫描 —— 于是 ``extensions/_template/`` 永远不会被
      自动装载（否则每个人都会看到一个叫 _template 的扩展），模板要生效必须
      先复制改名。
    """
    if not root.is_dir():
        return []
    found: list[tuple[str, Path]] = []
    for child in sorted(root.iterdir()):
        name = child.name
        if name.startswith(_TEMPLATE_PREFIXES):
            continue
        if child.is_dir():
            init = child / "__init__.py"
            if init.is_file():
                found.append((name, init))
        elif child.suffix == ".py":
            found.append((child.stem, child))
    return found


def _purge_extension_modules() -> None:
    """清掉本机制 import 过的全部模块（含扩展内部相对 import 的子模块）。

    为什么必须清：importlib 会缓存 ``sys.modules``。不清的话，删掉目录再新建同名
    扩展会拿到**上一次的旧模块**，删掉的扩展的子模块也还赖在缓存里 ——
    「删目录即卸载」就成了空话。每次装载前清空，保证「目录现状 == 装载结果」。
    """
    for key in [k for k in sys.modules if k.startswith(EXTENSION_MODULE_PREFIX)]:
        sys.modules.pop(key, None)
    # 文件增删后让子模块 finder（含下面自定义的 FileFinder）丢掉目录缓存。
    importlib.invalidate_caches()


class _SourceOnlyLoader(importlib.machinery.SourceFileLoader):
    """始终从磁盘源码现编译的 loader，判据只有「文件内容」。

    为什么不能用默认 ``SourceFileLoader``：它的字节码缓存以
    ``(源文件 mtime 整秒, size)`` 判定 ``__pycache__/*.pyc`` 是否有效。
    同一秒内改写扩展、且新旧源码**等长**（很常见：改一个字符串字面量），
    旧 ``.pyc`` 会被判为「有效」而直接复用 —— 于是「改代码即时生效」在
    这一秒内失效，装载结果与磁盘内容脱节（CI 上就复现了这个 flaky）。
    这里绕开 ``.pyc`` 读路径，直接 ``compile`` 当前源码，使结果确定。
    """

    def get_code(self, fullname: str):
        return compile(self.get_data(self.path), self.path, "exec", dont_inherit=True)


_EXTENSION_FILE_FINDER = importlib.machinery.FileFinder.path_hook(
    (_SourceOnlyLoader, importlib.machinery.SOURCE_SUFFIXES),
    (importlib.machinery.SourcelessFileLoader, importlib.machinery.BYTECODE_SUFFIXES),
)
"""扩展目录专用的 FileFinder 工厂（子模块相对 import 也走源码现编译）。"""

_EXTENSION_PACKAGE_DIRS: set[str] = set()
"""已装载过的包式扩展目录。path hook 只对这些目录生效，不干扰全局 import。"""


def _extension_path_hook(path: str):
    """``sys.path_hooks`` 钩子：仅扩展包目录命中，其余路径抛 ImportError 让位。

    包式扩展的具体子模块（``from . import helpers``）由 import 机制在包的
    ``__path__`` 上走 ``sys.path_hooks`` 查找。默认 FileFinder 会给子模块配
    默认 ``SourceFileLoader``（又回到 ``.pyc`` 判据），所以这里必须把扩展目录
    的 finder 换掉，「整棵扩展目录都现编译」才成立。非扩展路径直接让位，
    不改变解释器其余部分的 import 行为。
    """
    if path in _EXTENSION_PACKAGE_DIRS:
        return _EXTENSION_FILE_FINDER(path)
    raise ImportError(path)


def _install_extension_path_hook() -> None:
    """把扩展 path hook 装到最前，幂等（重复调用不会重复插）。"""
    if _extension_path_hook not in sys.path_hooks:
        sys.path_hooks.insert(0, _extension_path_hook)


def _load_module(name: str, entry: Path) -> Any:
    """按文件路径 import 扩展模块。

    为什么不用 ``importlib.import_module(名字)``：扩展目录不在 ``sys.path`` 上，
    按名字找不到。包式扩展用 ``submodule_search_locations`` 让 ``from . import x``
    这种同目录相对 import 正常工作；先入 ``sys.modules`` 再 exec 是包式 import
    的硬要求（相对 import 会回查父模块缓存）。

    loader 一律用 :class:`_SourceOnlyLoader`（而不是默认来源 loader）：默认的
    ``.pyc`` 判据会让「同一秒内等长改写」读到旧字节码，装载结果必须只由
    **磁盘当前内容**决定。
    """
    module_name = EXTENSION_MODULE_PREFIX + name
    loader = _SourceOnlyLoader(module_name, str(entry))
    if entry.name == "__init__.py":
        _EXTENSION_PACKAGE_DIRS.add(str(entry.parent))
        _install_extension_path_hook()
        spec = importlib.util.spec_from_file_location(
            module_name, entry, loader=loader,
            submodule_search_locations=[str(entry.parent)],
        )
    else:
        spec = importlib.util.spec_from_file_location(module_name, entry, loader=loader)
    if spec is None or spec.loader is None:  # pragma: no cover - 扫描层只喂 .py，纯防御
        raise ImportError(f"扩展 {name!r} 无法创建模块规格：{entry}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # 半执行的模块不能留在缓存里，否则下次装载会拿到残缺对象。
        sys.modules.pop(module_name, None)
        raise
    return module
