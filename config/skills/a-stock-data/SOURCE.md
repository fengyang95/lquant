# a-stock-data — 来源与许可

本目录的 `SKILL.md` **不是本仓库编写的**，是从上游 vendored 的第三方 skill
（Apache-2.0）。改名/重写它请改上游，或重跑下面的刷新脚本。

| 项 | 值 |
|---|---|
| 上游仓库 | https://github.com/simonlin1212/a-stock-data |
| 作者 | Simon 林（X [@linsizhen](https://x.com/linsizhen)） |
| 版本 | 3.10.0 |
| 许可 | Apache License 2.0（见上游仓库 `LICENSE`） |
| 快照 commit | f814dcfe209dd7958f4858f9d878d591ee85fb56 |
| 拉取日期 | 2026-10-03 |
| 大小 | 425008 字节 / 7509 行 |

## 更新

**不要手改 `SKILL.md`** —— 会被下一次刷新覆盖。升级到上游最新版：

```bash
scripts/update_astock_skill.sh
```

脚本重新拉取 `SKILL.md` 并把上面的版本 / commit / 日期写回本文件。

## 依赖

skill 内嵌的 Python 直连公开数据源（腾讯 / 东财 / 交易所等），需要
`mootdx requests pandas stockstats numpy baostock xlrd openpyxl`。
其中 `stockstats` 是 lquant 唯一未默认安装的一项，已在 `pyproject.toml` 的
`sources` 可选依赖里；装齐：`pip install -e '.[sources]'`。

## 与本地 `lquant-market` 的分工

`a-stock-data` 走**公开源直连**（覆盖面广，87 端点）；`lquant-market` 走
**lquant 本地数据湖**（快、离线、无封 IP 风险）。两者互补，见工作区 `CLAUDE.md`。
