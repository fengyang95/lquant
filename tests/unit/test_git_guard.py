"""部署前 git 守卫（scripts/lib/git_guard.sh）的行为回归。

防止两类事故：
1. 主仓 core.bare 被外部工具误置为 true，导致主目录无法切分支 / 部署；
2. lquant.sh update 在非 main / 脏树上静默部署旧代码。

用真实临时 git 仓库 + 子进程 source 守卫库来验证，避免只测到 mock。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "scripts" / "lib" / "git_guard.sh"


def _env(**extra: str) -> dict[str, str]:
    # 隔离全局/系统 git 配置，避免污染（需 git >= 2.32）
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    env.update(extra)
    return env


def _git(
    repo: Path, *args: str, check: bool = True, **extra: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, env=_env(**extra),
        capture_output=True, text=True, check=check,
    )


def _init_repo(path: Path, branch: str = "main") -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", branch, str(path)], env=_env(), check=True
    )
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "tester")
    (path / "f.txt").write_text("a\n")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-q", "-m", "init")


def _guard(repo: Path, call: str, **extra: str) -> subprocess.CompletedProcess[str]:
    script = f'set -euo pipefail\nsource "{LIB}"\n{call}\n'
    return subprocess.run(
        ["bash", "-c", script], cwd=repo, env=_env(**extra),
        capture_output=True, text=True,
    )


def _is_work_tree(repo: Path) -> bool:
    r = _git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    return r.stdout.strip() == "true"


def test_ensure_git_worktree_heals_bare(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    _git(repo, "config", "core.bare", "true")
    assert not _is_work_tree(repo)

    r = _guard(repo, "ensure_git_worktree")

    assert r.returncode == 0, r.stderr
    assert _is_work_tree(repo)
    assert _git(repo, "config", "core.bare").stdout.strip() == "false"
    # 自愈后主目录恢复可用：能读当前分支
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"


def test_ensure_git_worktree_noop_outside_git(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    r = _guard(plain, "ensure_git_worktree", GIT_CEILING_DIRECTORIES=str(plain))
    assert r.returncode == 0, r.stderr


def test_require_deploy_branch_accepts_main_clean(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    r = _guard(repo, "require_deploy_branch")
    assert r.returncode == 0, r.stderr


def test_require_deploy_branch_rejects_other_branch(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    _git(repo, "checkout", "-q", "-b", "feature")
    r = _guard(repo, "require_deploy_branch")
    assert r.returncode != 0
    assert "feature" in r.stderr


def test_require_deploy_branch_rejects_detached_head(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", sha)
    r = _guard(repo, "require_deploy_branch")
    assert r.returncode != 0
    assert "游离" in r.stderr


def test_require_deploy_branch_override(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    _git(repo, "checkout", "-q", "-b", "release")
    r = _guard(repo, "require_deploy_branch", LQ_DEPLOY_BRANCH="release")
    assert r.returncode == 0, r.stderr


def test_require_deploy_branch_empty_override_skips_branch_check(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    _git(repo, "checkout", "-q", "-b", "feature")
    r = _guard(repo, "require_deploy_branch", LQ_DEPLOY_BRANCH="")
    assert r.returncode == 0, r.stderr


def test_require_deploy_branch_rejects_dirty_tree(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    (repo / "f.txt").write_text("changed\n")

    r = _guard(repo, "require_deploy_branch")
    assert r.returncode != 0
    assert "未提交改动" in r.stderr

    r2 = _guard(repo, "require_deploy_branch", LQ_ALLOW_DIRTY="1")
    assert r2.returncode == 0, r2.stderr


def test_require_deploy_branch_rejects_staged_change(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    (repo / "f.txt").write_text("staged\n")
    _git(repo, "add", "f.txt")
    r = _guard(repo, "require_deploy_branch")
    assert r.returncode != 0


def test_require_deploy_branch_rejects_untracked_file(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    (repo / "g.txt").write_text("x\n")  # 未跟踪

    r = _guard(repo, "require_deploy_branch")
    assert r.returncode != 0
    assert "未跟踪" in r.stderr

    r2 = _guard(repo, "require_deploy_branch", LQ_ALLOW_DIRTY="1")
    assert r2.returncode == 0, r2.stderr


def test_require_deploy_branch_ignores_ignored_untracked(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    (repo / ".gitignore").write_text("cache/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    (repo / "cache").mkdir()
    (repo / "cache" / "x").write_text("y\n")

    r = _guard(repo, "require_deploy_branch")
    assert r.returncode == 0, r.stderr


def test_require_deploy_branch_many_untracked_files(tmp_path: Path) -> None:
    # 回归：曾用 `| head -1` 取首行，head 提前关管道让 git 收到 SIGPIPE(141)，
    # 在 pipefail 下中断整个脚本、看不到提示。用大量未跟踪文件触发这条路径。
    repo = tmp_path / "r"
    _init_repo(repo)
    prefix = "z" * 120
    for i in range(1500):
        (repo / f"{prefix}{i}.txt").write_text("x")

    r = _guard(repo, "require_deploy_branch")

    assert r.returncode == 1, (r.returncode, r.stderr)  # 1 = fail，而非 141 SIGPIPE
    assert "未跟踪" in r.stderr


def _copy_lquant_without_lib(tmp_path: Path, *, remote: bool) -> Path:
    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / "lquant.sh").write_text((REPO_ROOT / "lquant.sh").read_text())
    if remote:
        subprocess.run(["git", "init", "-q", "-b", "main", str(copy)], env=_env(), check=True)
        subprocess.run(
            ["git", "-C", str(copy), "remote", "add", "origin", "https://example.invalid/x.git"],
            env=_env(), check=True,
        )
    return copy


def test_lquant_sh_degrades_when_guard_lib_missing(tmp_path: Path) -> None:
    # 缺 scripts/lib/git_guard.sh 时，非部署子命令仍应可用（graceful degrade）
    copy = _copy_lquant_without_lib(tmp_path, remote=False)

    r = subprocess.run(
        ["bash", str(copy / "lquant.sh")], cwd=copy, env=_env(),
        capture_output=True, text=True,
    )

    assert r.returncode == 0, r.stderr
    assert "已跳过启动自愈" in (r.stdout + r.stderr)


def test_lquant_sh_degrades_update_refuses_without_lib(tmp_path: Path) -> None:
    # 部署路径在守卫不可用时必须拒绝，而非静默继续
    copy = _copy_lquant_without_lib(tmp_path, remote=True)

    r = subprocess.run(
        ["bash", str(copy / "lquant.sh"), "update"], cwd=copy, env=_env(),
        capture_output=True, text=True,
    )

    assert r.returncode != 0
    assert "拒绝在未校验状态下部署" in (r.stdout + r.stderr)


def test_lquant_sh_degrades_status_still_works(tmp_path: Path) -> None:
    # 应急子命令（status）在缺守卫库时仍可用
    copy = _copy_lquant_without_lib(tmp_path, remote=True)

    r = subprocess.run(
        ["bash", str(copy / "lquant.sh"), "status"], cwd=copy,
        env=_env(LQ_API_PORT="59999"), capture_output=True, text=True,
    )

    assert r.returncode == 0, r.stderr
    assert "服务状态" in r.stdout
