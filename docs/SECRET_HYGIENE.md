# 密钥与敏感信息卫生

面向「把私有仓库公开为 GitHub public 仓库」这件事的门禁与操作手册。

和 [`SECURITY.md`](SECURITY.md) 的分工：那篇讲**运行时**安全边界（谁能访问 API
谁就能在本机执行代码）；这篇讲**仓库内容**不要泄露 —— 两者互不覆盖。

## 一句话

公开仓库暴露的是**全部历史**，不是当前文件。所以只加一个 pre-push 钩子是不够的：
必须先做一次全历史审计，且出事后**先轮换凭证、再清理历史**。

## 四层机制

| 层 | 位置 | 扫什么 | 能不能被绕过 |
|---|---|---|---|
| 1. 提交前 | `scripts/githooks/pre-commit` | 暂存区 + 内部文件 | `git commit --no-verify` 可绕过 |
| 2. 推送前 | `scripts/githooks/pre-push` | **本次推送的提交范围** | `git push --no-verify` 可绕过 |
| 3. CI | `.github/workflows/secret-scan.yml` | PR/push 增量 + 每周全历史 | 服务端，绕不过 |
| 4. GitHub | Secret scanning + Push protection | 服务端全量 | 绕不过（见下方清单） |

第 1、2 层是**快速反馈**（改起来便宜），第 3、4 层才是**真正的闸**。
前两层用 `--no-verify` 能过，这是 git 的固有行为，不要指望钩子能防住有意绕过的人 ——
它们的价值是拦住"手滑"，而 CI 拦住"绕过"。

三层本地/CI 共用同一份实现（`scripts/secret_scan.sh`）和同一份规则
（`.gitleaks.toml`）。这一点是刻意的：口径不一致会让人养成加 `--no-verify` 的习惯，
门禁就废了。

## 日常用法

```bash
make secrets-install    # 装 gitleaks（免 sudo，装一次所有 worktree 共用）
make secrets            # 扫暂存区（pre-commit 跑的就是这个）
make secrets-history    # 全历史 + 所有分支（公开前必跑）
make secrets-dir        # 工作区已跟踪文件快照
make public-ready       # 公开前体检 = 全历史 + 工作区 + 卫生（严格）
```

也可以直接调脚本，模式更全：

```bash
scripts/secret_scan.sh staged          # 暂存区
scripts/secret_scan.sh push <reflog>   # 推送范围（pre-push 用）
scripts/secret_scan.sh range A..B      # 任意提交范围
scripts/secret_scan.sh history         # 全历史
scripts/secret_scan.sh dir             # 工作区已跟踪文件
scripts/secret_scan.sh hygiene         # 内部文件 / 本机路径（不需要 gitleaks）
scripts/secret_scan.sh public          # 公开前体检
scripts/secret_scan.sh all             # 以上全部
```

退出码：`0` 通过 / `1` 发现风险 / `2` 环境不满足（缺 gitleaks、版本过旧、缺配置）。

### 几个刻意的设计取舍

- **缺 gitleaks 时失败关闭**（exit 2），不静默放行。「扫描器没跑起来」和「扫了没问题」
  是两件事，混淆它们等于没有门禁。临时放行要显式写
  `LQ_SECRET_SCAN_ALLOW_MISSING=1`。
- **`LQ_SKIP_PUSH_CHECK=1` 只跳过测试与覆盖率，不跳过密钥扫描**。跳过测试是日常操作，
  不该顺带让人在不知情下裸奔推送。要跳密钥扫描得显式写 `LQ_SKIP_SECRET_SCAN=1`。
- **CI 里不打印密钥片段**。公开仓的 CI 日志是公开可见的；gitleaks 的 `-v` 会把
  密钥尾部片段带进上下文。所以脚本检测到 `CI`/`GITHUB_ACTIONS` 时自动关掉 `-v`，
  只输出「文件:行 + 规则名」，值一律隐藏。
- **`history` 带 `--diff-merges=on`**。`git log -p` 默认不输出 merge 提交的 diff，
  于是「evil merge」（两个父提交都没有、只在 merge 里出现的内容）是盲区。
  实测本仓：433 个提交 → 485 个，扫描量 47 MB → 59 MB。
- **白名单按「值」放行，不按目录跳过**。跳过 `tests/` 是最省事的做法，代价是
  「测试里塞了真密钥」这条最高频的泄露路径失去保护。加白名单必须写明理由。

## 出事了怎么办

**顺序不能反：先轮换，再清理。**

### 1. 先撤销 / 轮换凭证

只要密钥进过公开仓库，就必须**假设它已经泄露**。公开仓库会被爬虫在分钟级抓取，
改写历史**不能**让已经流出的值失效 —— 你删掉的是仓库里的副本，不是别人手里的副本。

- Tushare：后台重置 token，更新 `src/lquant/.env`
- 同花顺 / 其他数据源：重置对应 API key
- GitHub PAT / deploy key：立即 revoke 并重建
- `LQ_A2A_TOKEN`：`openssl rand -hex 24` 重新生成

### 2. 再清理历史

```bash
# 推荐 git-filter-repo（比 filter-branch 快且不易出错）
brew install git-filter-repo
git filter-repo --path <泄露文件> --invert-paths
# 或按内容替换：
# git filter-repo --replace-text <(echo '旧密钥==>REDACTED')

git push --force-with-lease origin main
```

清理后**所有协作者必须重新 clone**（旧克隆里仍有泄露的提交对象）。
提醒他们：`git fetch` 不够，本地 reflog 里还留着。

### 3. 如果仓库已经公开

改写历史**不会**撤回已经被抓取的副本。可选：

- 联系 GitHub Support 请求清除缓存的视图；
- 最彻底：删库重建（`Settings → Delete this repository`），用清理干净的历史重新推。

无论哪种，第 1 步的轮换都是必须的 —— 这也是为什么它排在前面。

## GitHub 网页操作清单

> 仓库公开后，以下设置才可用/才有意义。`gh` CLI 的 token 当前已失效，
> 所以这里给网页路径。

公开前：

1. **确认 `make public-ready` 通过**（本仓当前有 16 个文件含本机路径，见下）。
2. `Settings → General → Danger Zone → Change repository visibility → Make public`。
   公开瞬间历史即对外可见，**没有回退**（改回私有不会收回别人已 clone 的内容）。

公开后立即（`Settings → Code security and analysis`）：

3. **Secret scanning** → Enable。公开仓免费，会对全历史持续扫描并在发现时告警。
4. **Push protection** → Enable。这是服务端的 pre-receive 钩子，能在密钥**推送时**
   直接拒绝 —— 本地钩子被 `--no-verify` 绕过后的最后一道闸。公开仓免费。
5. **Dependabot alerts / security updates** → Enable。

另外建议：

6. `Settings → Actions → General`：公开仓会接受 **fork 的 PR**。确认
   「Fork pull request workflows」需要审批（默认需要），避免陌生人 PR 直接消耗
   Actions 额度或触及 secrets。
7. `Settings → Secrets and variables → Actions`：确认没有把生产凭证放进来。
   公开仓的 workflow 日志人人可见。
8. `Settings → Branches`（或 Rulesets）：把 `Secret scan / 密钥扫描（gitleaks）`
   设为 main 的必需检查，否则 CI 失败也能合。
9. `Settings → Collaborators and teams`、`Settings → Deploy keys`、Webhooks：
   过一遍，去掉不再需要的。
10. **提交邮箱**：公开后所有历史提交的作者邮箱都可见（本仓是
    `yuanpengli@zju.edu.cn`）。想避免暴露就开 `Settings → Emails → Keep my email
    addresses private`，但**已提交的历史不会改变**，要改需重写历史。
11. 可选：`Settings → Code security → Private vulnerability reporting` 打开，
    让外部研究者能私下报漏洞。

## 本仓当前审计结论

2026-10-03，基于 `worktree-secret-guard` 分支（基于当时 main）：

| 检查 | 范围 | 结论 |
|---|---|---|
| 全历史 | 488 个提交 / 173 个 ref，485 个实际扫描（含 merge diff），59.21 MB | ✅ 未发现密钥 |
| 工作区 | 已跟踪文件快照，6.74 MB | ✅ 未发现密钥 |
| 卫生 | 内部文件 / 嵌套仓 / `.env` 跟踪 | ✅ 三项通过 |
| 卫生 | 本机绝对路径 | ⚠️ 16 个文件含 `/Users/lyp` |

历史里命中过但已确认**不是密钥**、已进白名单的只有两类：

- 东方财富公开 `ut` 常量（`7eea3edc…`、`f057cbcb…`、`b2884a39…`）：akshare /
  efinance 内置的公共请求参数，不是个人凭证；
- `tests/unit/test_redact.py` 里的假密钥（`AKIAIOSFODNN7EXAMPLE` 是 AWS 官方
  文档示例键）。

**关于那 16 个本机路径**：不是密钥，但公开后会暴露用户名与目录结构。属于
"要不要处理"的判断题，所以默认只告警（`hygiene`），`make public-ready` 才按失败处理。
想清掉的话：`docs/` 里的大多是设计文档里的示例路径，改成相对路径或
`/path/to/...` 即可；`deploy/launchd/com.lquant.sync.plist` 和
`scripts/gen_launchd_plist.py` 是 launchd 模板，需要保留绝对路径语义，
可以改成 `__HOME__` 之类的占位符。

## 维护约定

- **加白名单**：只加确认不是密钥的**具体值**，并在 `.gitleaks.toml` 里写明理由。
  不要加目录级 `paths` 例外（除非该目录确实不可能有凭证，比如锁文件）。
- **升 gitleaks 版本**：三处一起改 ——
  `scripts/install_gitleaks.sh` 的默认值、`.github/workflows/secret-scan.yml`
  的 `GITLEAKS_VERSION`、本文档。只改一处会导致「本地过了 CI 挂」。
- **新增凭证面**：如果引入了新的数据源/服务凭证，在 `.gitleaks.toml` 补一条自定义规则
  （参照 `lquant-tushare-token`），否则裸十六进制/无前缀的 token 内置规则抓不到。
- **不要**为了让 push 过而用 `--no-verify`。真密钥要轮换，假阳性要进白名单并说明理由 ——
  绕过一次，下次就没人看告警了。
