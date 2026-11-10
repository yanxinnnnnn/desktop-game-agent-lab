# PR #25 第三轮整改：离线预检与合成 Stub 工具

**DSH Offline Preflight + Guarded Dry-run + Synthetic Stub Testing Toolkit**

Refs #23。交接及本轮父提交：`bc39103e5c6afac2d4111c189cbd5516bbd3e55e`。
本轮由 Codex 接手实现；Founder 保留唯一手动 Merge 权限，等待 ChatGPT CTO 第三轮审查。

## 范围与架构审查

调用关系：`joint_experiment.ps1` → `launcher_guard.ps1` → 对临时配置副本运行
`no_tools_preflight.py` → 原子发布 Manifest。独立 `run` 调用重新读取 JSON 并验证全部摘要；
`dry` 到此返回，`stub` 仅执行当前 PowerShell 宿主加载的固定 `controlled_stub.ps1`。
`bounded_process.cs` 负责两个管道的并发消费与超时清理。两个原有 PowerShell 测试入口调用
`test_launcher.py`，后者创建独立临时目录、调用真实独立 PowerShell 进程并验证文件副作用。

| 必答问题 | 交接代码审查结论 | 本轮结果 |
|---|---|---|
| Preflight 是否检查全部要求的配置层？ | Home 参数未进入 cfg，实际漏检 | base → profile → Home → overlay 四层；Home 缺失显式视为无操作层；不证明真实安装包的有效来源已穷举 |
| Manifest 是否绑定成功预检、目录与快照？ | 仅记录 root/home/未验证的 base hash | 仅完整 PASS 发布 v2；固定 synthetic-only；绑定 root、home、config_dir、五个输入的路径/缺失状态/原始字节 SHA-256 |
| 变化后 dry-run 是否拒绝？ | 不拒绝 | dry/stub 均重读五个输入并比对；Home 新增/删除同样拒绝 |
| BLOCKED 是否非零且零启动？ | 缺少独立启动事实 | 运行门禁失败非零；测试断言无 stub Marker、计数为零；预检器进程不属于运行目标 |
| Stub 是否验证进程控制？ | 只有 echo，串行读管道 | 实际双管道大输出、墙钟超时及终止、非零退出、独立启动计数和环境验证 |
| 是否仍有真实 DSH 执行路径？ | real 与 Boolean 授权开关仍可达 | 已删除；只有 dry/stub，拒绝原 real/授权参数以及任意目标参数 |
| 测试是否验证行为？ | 部分只搜索 STARTED | 实际退出码、JSON 文件往返、Marker/计数、环境、存活 PID、超时与原配置字节；输出断言仅辅助验证状态契约 |

## 配置与 Manifest 契约

- 配置目录必须位于可丢弃实验根内。四层只接受明确的 `id/name/disabled` 列表；base 另可用单层 `insert` 容器。
  `[]` 是合法无操作 patch；空白/注释文件失败。拒绝未知字段、任意嵌套 payload、标签、表达式、锚点、重复键/ID、多个文档、歧义缩进及包替换。
  这是刻意保守的子集，复杂实际安装配置可能被拒绝，不能因此扩充未验证的解析语义。
- 24 个既有能力行固化为最低清单，capabilities 只能增加 inventory 要求，不能覆盖 CLI 路径，也不接受 enabled-tool 例外。
  所有挂载行最终必须禁用；任一非 base 层出现 `disabled: false` 直接失败，即使后续 overlay 再禁用。
- Home 固定为 `dsh-home/cordis.patch.yml`。缺失是显式无操作层，父链必须安全；存在则完整解析，目录/非法内容/重启用均失败。
- 输入最多各 1 MiB，JSON 拒绝重复键。基线 Hash 使用原始字节，换行或 BOM 改变同样视为配置变化。
- Preflight 在安全根检查及独占锁之后，先删除旧 Manifest，再加载 Guard/检查 Home、文件、Hash 与 Python。
  根本身或祖先为 reparse 时不沿别名删除外部文件，后续经同一不安全路径的 run 也拒绝。
- 在唯一临时目录中复制输入，只检查副本；发布前同时比对原件与副本摘要。
  Manifest 临时文件完整写入并 Flush，然后在同一目录 Move 发布；仅已完成的全部 PASS 状态可见。
  两个 Phase 共用独占文件锁，避免 run 消费正在重做的预检。
- v1 Manifest 全部失效。v2 只接受 `preflight=PASS`、`trust=synthetic-only`；严格校验 base hash 格式、五个输入及快照摘要。
  本轮没有 installed 信任等级，也没有任何通向真实 DSH 的提升机制。
- **SHA-256 用于意外变化/损坏检测，不是防恶意篡改的身份认证。** 能重写代码或 Manifest 的主体不在该完整性检查的防护范围。

## 子进程与路径边界

- dry-run 在任何 `Process.Start` 或 C# 编译之前返回 `PREPARED_ONLY`。
- stub 不接受任意 executable/script 路径，只接受固定场景枚举。当前 PowerShell 控制台宿主通过内存加载运行仓库固定脚本，不修改执行策略。
- stdout/stderr 各由独立 Task 以 4096 字符缓冲区持续排空；stub 保留正文为零，仅统计长度。
  Python 预检器每流最多保留 65536 字符，超长结构化结果拒绝。
- 仅 Start 成功后输出 STARTED；实际 exit 0 才输出 COMPLETED。目标非零返回 3，墙钟超时返回 4，启动/门禁/异常返回 2。
  超时 Kill 后有界等待并回收进程；固定 stub 和预检器均不创建后代，不支持任意进程树。
- 子环境从空字典构造，仅保留 SystemRoot/windir 和指向实验目录的 Home/Temp 等必要值。
  无 API Key、Mock Key、代理、GitHub 凭据或继承的 PATH；PowerShell 宿主可能自行增加 PATH/PSModulePath。
  Marker 只记录合成目录、PID、场景和环境键名，不记录凭据或输出正文。
- 规范化时拒绝相对路径、`..`、控制字符和 Windows 非本地盘绝对路径；Windows 忽略大小写，其他平台精确比较。
  检查实验根及全部祖先、Home、配置文件和输出路径的 ReparsePoint；真实 `.dsh` 及其子目录在任何内容访问前被拒绝。
- 正常和失败结束清理临时副本/待发布文件/进程，测试在 finally 清理自己的全部目录。
  操作系统强制终止父进程无法执行 finally，可能留下无效临时副本或预检子进程；中断回归只清理本测试记录的 PID，确认没有有效 Manifest。

## CTO 原 Review 逐项处置

| Review comment ID | 处置与测试依据 |
|---|---|
| 4482592233 | 真实执行 Python 四层预检；仅完整 PASS 发布；Home/Hash 负向测试 |
| 4482592245 | 明确 dry PREPARED_ONLY 与 stub STARTED/COMPLETED/FAILED；real 已删除 |
| 4482592256 | 进程级非零退出；所有运行门禁负向测试验证 Marker 与零计数 |
| 4482592268 | Assert-Manifest 接受实际 ConvertFrom-Json 对象；真实文件往返测试 |
| 4482592284 | 根及祖先链检查；Linux symlink 已执行，Windows Junction 单列 SKIP |
| 4482592291 | 两个独立 Phase 进程，真实写入/读回、退出码和副作用集成测试 |
| 4482592305 | 固定 stub 自己写启动计数与 Marker；BLOCKED 场景零目标启动 |
| 4482723831 | CLI Home 正确传入；Home Tool/Provider 重启用失败；capabilities 路径覆写失败 |
| 4482723839 | 入口废止旧状态；缺文件/Hash/Home/Guard、预检器异常及中断后 run 均阻断 |
| 4482723847 | real 分支与授权开关删除；route.yml 与安装包绑定仍未解决，属于后续阶段 |
| 4482723857 | v2 快照、原始字节摘要、缺失状态与运行前复核；变化/非法/篡改 Hash 回归 |
| 4482723866 | 双管道并发消费，2 MiB stdout/stderr/两者，墙钟超时和 PID 清理测试 |
| 4482723877 | 不再以输出文字计数；正常一次、连续两次、失败零次分别检查计数文件 |
| 4482723888 | PASS/FAIL/SKIP 独立统计；平台不足明确说明，未执行不算通过 |

新增发现并修复：原解析器会跳过未知嵌套结构；能力清单与例外可削弱检查；Home 之外的后续层可引入未知 ID/包替换；
根/配置/输出/锁文件重定向风险；工具加载异常仍可能保留旧状态；两个 Phase 并发竞争。对应回归均使用合成输入。

## 本轮实际验证（2026-11-10）

环境：Debian 13 x86_64，Python 3.12.14；Microsoft 官方便携 PowerShell 7.4.13（Linux）。
**没有 Windows PowerShell 5.1 运行时，没有 Windows Junction 验证能力。pwsh 7 的语法解析/执行不等于 5.1 验证。**
C# 仅使用 .NET Framework 4.5 已有 API；5.1 兼容性仍需 Windows 本地执行确认。

在仓库根目录执行（`<pwsh>` 指已准备的补充测试运行时；Linux 使用任务临时 XDG cache/config/data 目录）：

```text
python3 -m unittest discover -s cu-test/tools -p test_no_tools_preflight.py -v
<pwsh> -NoProfile -Command '$td="<repo>/cu-test/tools"; & ([scriptblock]::Create((Get-Content -LiteralPath (Join-Path $td "test_launcher_isolation.ps1") -Raw -Encoding UTF8))) -ToolsDir $td'
<pwsh> -NoProfile -Command '$td="<repo>/cu-test/tools"; & ([scriptblock]::Create((Get-Content -LiteralPath (Join-Path $td "test_launcher_integration.ps1") -Raw -Encoding UTF8))) -ToolsDir $td'
python3 -m unittest discover -s tests/supervisor -p 'test_*.py' -v
PYTHONPATH=core python3 -m unittest discover -s tests/fwj_contracts -p 'test_*.py' -v
python3 -m unittest discover -s tests/notifications -p 'test_*.py' -v
git diff --check
```

| 最终套件（复跑不重复计数） | PASS | FAIL | SKIP |
|---|---:|---:|---:|
| Python 四层预检 | 16 | 0 | 0 |
| Guard / JSON 往返 | 9 | 0 | 1 |
| Launcher / Manifest / dry / stub 集成 | 29 | 0 | 4 |
| Supervisor / DSH adapter（模拟） | 94 | 0 | 0 |
| FWJ contracts | 48 | 0 | 0 |
| Notifications（模拟传输，未发送） | 26 | 0 | 0 |
| 合计 | **222** | **0** | **5** |

SKIP：Windows 大小写比较 1 项，Windows root/parent/Home Junction 3 项，Windows PowerShell 5.1 运行时验证 1 项。
Linux 的 root/Home/config/file symlink、锁及输出重定向已实际执行；它们不替代 Windows Junction 验证。
所有五个 PowerShell 文件在 pwsh 7 中语法解析通过；`git diff --check` 无输出、退出 0。
初轮发现宿主自行增加 PATH 的环境断言错误，修正后复测通过；契约测试初次缺少 PYTHONPATH 导致导入失败，按上述命令修正后 48 项通过。
上述 FAIL=0 指最终版本的最终套件结果，不隐去这两次已纠正的初轮失败。

Windows 可用相同内存加载入口执行两个套件；需要 Python 3.10+。
每个 unittest 方法按实际执行计数，方法内部 subTest 不额外冒充测试项。

## 剩余风险、回滚与交付边界

- 真实 DSH：**NOT RUN**；模型调用：**0**；通知发送：**0**；Merge：**NO**。
- **真实 DSH runtime isolation：NOT VERIFIED**。有效安装包配置来源、route.yml、Electron userData、工具空集、外网阻断、真实 NDJSON 成功路径仍未验证。
  真实 DSH 执行属于 Issue #23 后续独立授权阶段；本 PR 不能关闭该 Issue。
- 这是对已知合成输入的离线准备及受控进程行为测试，不是 OS sandbox；路径属性检查不能消除恶意并发替换/TOCTOU。
- 历史真实 Home 写入的具体 PID 归因仍未知；旧变量作用域 Bug 解释有静态代码支持，不据此声称已追溯历史进程。
- 回滚本轮追加提交即可恢复交接版本；恢复后也不能执行旧 real 模式。v1 Manifest 必须重新预检，不能迁移为有效 v2。
- 未读取私有原始证据、真实用户配置或凭据；未修改 Issue #21 实现、历史 POC、安全设置或代理；未构建容器或操作游戏/浏览器。
- 提交到原分支，更新原 PR，不自动 Resolve CTO Review Threads，停止等待第三轮 Code Review。
