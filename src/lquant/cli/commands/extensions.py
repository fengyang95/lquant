"""lq extensions：只读列出扩展目录的装载情况。

「只读」指不写任何数据、不改注册状态：装载本身就是一次瞬时快照
（``core.extensions`` 没有进程级注册表单例）。
"""

from __future__ import annotations

import click


@click.group()
def extensions() -> None:
    """扩展（契约化装载，删目录即卸载）"""


@extensions.command("list")
@click.option("--root", default=None, help="扩展目录；默认 <仓库根>/extensions")
def list_extensions(root: str | None) -> None:
    """装载并列出扩展：loaded / skipped / failed，各自带原因。"""
    from lquant.core.extensions import load_extensions

    report = load_extensions(root)
    click.echo(report.summary())
    if not report.ok:
        # 「被隔离」不等于「应用起不来」，但退出码要如实反映，脚本才能发现坏扩展。
        raise SystemExit(1)
