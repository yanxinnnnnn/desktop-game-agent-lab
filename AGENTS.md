# AGENTS.md

面向在本仓库工作的 AI Agent（以及人类协作者）的约定。**违反其中任何一条，PR 会被直接拒绝。**

---

## 1. 这个仓库是什么

桌面游戏自动化实验的归档 + 引擎仓库。内容分两类：

| 类型 | 位置 | 性质 |
|---|---|---|
| 历史实验报告 | `docs/pocs/` | **只读档案**。除脱敏错误外不得改写结论、数据、失败记录 |
| 可执行引擎 | `engine/` | 可维护代码。改动需对应 Issue、测试与说明 |

POC 报告记录的是"当时真实发生了什么"，包括失败与未解问题。**不许美化历史。**

---

## 2. 硬性禁止

1. **禁止提交**：原始截图、录屏、运行日志（`*.csv` / `summary-*.json` / `*.png`）——**现有例外**为 `tests/fixtures/2048/` 中已审查的 3 张截图；以后新增任何图片 fixture，必须先有独立 Issue、Founder 逐张批准及 CTO 安全审查，并在 PR 中显式增加受控的 `.gitignore` 例外；
   本机路径、Windows 用户名、主机名、账号 / 角色 / 服务器标识、令牌、Cookie、代理配置。
2. **禁止**修改 Windows 安全设置、执行策略、UAC、注册表安全项、系统代理。
3. **禁止**请求提权（管理员权限）运行任何脚本或工具。
4. **禁止**在 CI、迁移、文档任务中执行真实的游戏 / 浏览器自动化操作。
   `Run2048.ps1` 只能由人类在授权环境下手动运行。
5. **禁止**自动合并 PR、自动 push 到 `main`、强推（`force push`）、改写已推送历史。
6. **禁止**通过 DOM / JavaScript / 调试端口读取或修改页面状态来"帮助"识别。

---

## 3. 工作流

```
Issue（含验收标准） → 分支 → 实现 + 测试 → ready-for-review PR → Founder 人工合并
```

- 分支命名：`feat/<short-topic>`、`fix/<short-topic>`、`docs/<short-topic>`、`data/<short-topic>`（遗留 `poc-006/*` 不强制改名）
- PR 描述必须包含：动机、改动范围、测试结果、安全影响、回滚方式
- 首次 baseline 已由 Founder 审核并推送；后续开发必须始终通过功能分支 PR

---

## 4. 环境与运行

| 项 | 值 |
|---|---|
| Shell | Windows PowerShell **5.1**（不是 pwsh 7） |
| 依赖 | 仅 `System.Drawing` + `user32/dwmapi/kernel32/advapi32` |
| 执行策略 | 验证机上为 `Restricted` —— **不要修改它**，用内存加载 |

```powershell
$base = '<repo>\engine'
. ([scriptblock]::Create((Get-Content "$base\Desktop.psm1" -Raw -Encoding UTF8)))
. ([scriptblock]::Create((Get-Content "$base\Game2048.psm1" -Raw -Encoding UTF8)))
. ([scriptblock]::Create((Get-Content "$base\Tests.ps1"   -Raw -Encoding UTF8)))
```

**编码陷阱**：PS 5.1 的 `Get-Content` 默认按 ANSI/GBK 读取。仓库内所有 `.ps1` / `.psm1` / `.md`
均为 UTF-8，读取时必须显式 `-Encoding UTF8`，否则中文乱码并触发解析错误。

---

## 5. 测试

```powershell
# 从仓库根目录运行（fixtures 位于 tests/fixtures/2048/）
. ([scriptblock]::Create((Get-Content "$base\Tests.ps1" -Raw -Encoding UTF8)))
```

- 历史 19 项用例在原验证环境下为 18 PASS / 1 SKIP（含 3 张已审查图片 fixture）。
  **注意：部分 Desktop 用例依赖真实 Chrome 窗口，不能把现有 18 项统称为“无 GUI 的离线单测”**；见 Issue #3。
- 条件不满足时对应用例标记 **SKIP**，不是 FAIL。
- **不允许**为了让测试变绿而放宽断言、删除用例或伪造 fixture。

---

## 6. 代码约定

- 所有公开函数返回 `@{ Ok; Code; Message; Data }`，**不抛异常、不无限重试**。
- 坐标一律物理像素；进程启动时 `SetProcessDPIAware`。
- 不缓存窗口矩形；每轮重新查询。
- 输入前必须校验目标窗口在前台，否则返回 `E_NOTFOREGROUND`。
- 新增"游戏专属"逻辑只能进对应模块（如 `Game2048.psm1`），**不得污染 `Desktop.psm1`**。
- 注释与文档使用中文；代码标识符用英文。

---

## 7. 已知问题（不要"顺手修掉"）

| 问题 | 状态 |
|---|---|
| POC-005 报告记载 Gain=300 | **已调查**：原始棋盘记录重放合计 304，属于报告汇总数字错误；见 Issue #2（已关闭）及 PR #6 Erratum。开局 UI 分数未独立采集，严格 UI 首末对账仍未完成（Issue #5） |
| 字形模板冷启动：首次遇到 16/32/64 只靠颜色 | **Open**，已知限制 |
| 2 与 4 底色 RGB 差仅 23 | **Open**，字形通道为补偿手段 |
| `Find-2048Board` 依赖棋盘底色 `#bbada0` | **Open**，主题变更会失效 |

修以上问题需要单独 Issue + 证据，不允许在文档或迁移 PR 中夹带。

---

## 8. 提交前自检

- [ ] `git diff --cached` 中无绝对路径、用户名、账号标识、令牌
- [ ] 除 `tests/fixtures/` 下已批准并审查的 fixture 外，无 `*.png` / `*.csv` / `logs/` 进入暂存区
- [ ] 测试结果已附在 PR 描述中（含 SKIP 项说明）
- [ ] 未修改 `docs/pocs/` 中的历史结论
- [ ] 未触碰系统设置、执行策略、游戏文件

---

## 9. 多 Agent 角色与权限

| 角色 | 主要职责 | 权限边界 |
|---|---|---|
| **Founder（人类）** | 产品方向、优先级最终决定；样本/实机实验授权；本地验收；**手动 Merge** | 唯一可以决定是否合并、是否允许游戏实机操作；对关键标注作最终裁决 |
| **ChatGPT（CTO）** | 架构与模块边界；Issue 及验收标准；任务分派；PR 代码/证据 Review；追踪风险 | 可创建/更新 Issue、提交审核评论和按任务授权创建文档 PR；**不得代 Founder Merge** 或擅自批准实机操作 |
| **Codex（Implementation Developer）** | 版本化数据契约、确定性核心逻辑、自动化单测、实现型 PR | 只能在分配的 Issue/分支/文件范围内改代码；不得访问未授权的本地证据，不得因测试需要操作真实游戏 |
| **DeepSeek Harness（Windows Integration & QA）** | 保管/梳理本地 POC 证据、转录与初标、Windows 环境验证、集成反馈；按明确 Issue 完成指定工程任务 | 未经 Founder 授权不得将原始证据、截图或识别性信息上传；**不得自行启动实机游戏操作实验** |

**角色是默认分工，不是永久文件所有权**：具体任务以 Issue 中明确指定的 owner、允许修改路径、验收标准为准。Founder 可调整 owner；未经同意不得临时扩大范围。两 Agent 的回答、测试报告、代码和标注都是待验证产物，不是自动可信事实。

**正式产品方向**：2048 是受控桌面自动化试验场；《梦幻西游》任务理解、视觉观察和状态管理是主要产品方向。不得无 Issue 地继续堆叠 2048 专用功能。High IL / Medium IL 障碍仍然存在，不能假设 2048 可操作就代表游戏可操作。

---

## 10. 并行任务与 Git 隔离

1. **一个实现型 Issue → 一个主要 Owner → 一个工作分支 → 一个 Ready for review PR**。允许 Review/提供建议，但其他 Agent 不得擅自向该分支追加提交或修改其工作目录。
2. Codex 与 DeepSeek **分别使用独立 clone 或 Git worktree**，不得同时对一个工作树执行 `checkout`、`reset`、`stash`、`clean`、写文件等操作。Worktree 共享同一 `.git` 存储；不得在一方工作时修改另一方的 branch/ref。
3. **从最新的远端 `main` 创建分支**；开发前复核 Issue、基线 Commit、文件写入范围与上游依赖。依赖 PR 未合并时，先做独立的证据整理/规划，不得自行假定接口已冻结。
4. 尽量按不重叠文件拆分并行工作。例如：Codex 做 FWJ 契约/校验器，DeepSeek 整理 POC-002 的**脱敏转录和人工标注候选**；后者必须遵循已合并的 Schema，不得另起一套契约。
5. 两个 Issue 若必须改同一文件，**默认串行**：先合并上游 PR，再从最新 `main` 启动下游；确需并行时由 CTO 在 Issue 中明确依赖与冲突解决人，Founder 决定集成顺序。
6. 不直接 `push main`，不 `force push`，不 `rebase`/改写已发布分支历史；不自动 Merge、不自动关闭无关 Issue。需要跨 Agent 同步时通过 Issue / PR 评论交接，而不是覆盖对方文件。
7. Agent 的 GitHub、Codex 或本地 Harness 连接**分别授权**；不得假设某个 Agent 能继承另一个的账户连接、权限或本地文件访问。禁止索取、传递或写入访问令牌。

---

## 11. 并行交付与 Review 门槛

**Issue 是 implementation specification 的权威来源**；发生范围/接口争议先在 Issue 中澄清。每条开发 Issue 至少写明：owner、上游依赖、允许修改路径、明确禁止项、输入/输出契约、验收测试、证据来源及保密边界。

每个 PR 必须：
- 基于分配的 Issue，保持 **Ready for review**，正文第一行使用 `Closes #<issue-number>`（仅在该 PR 合并即可完整完成 Issue 时）；否则使用 `Refs #<issue-number>`，避免误关仍需后续交付的 Issue。
- 说明变更动机、文件范围、所依赖的 Schema/Commit、测试命令与真实结果（PASS/FAIL/SKIP 分开）、安全影响、回滚方法及 Unknown。
- 审查是否触碰其他 Agent 拥有的文件、是否引入共享接口破坏变更。需要对方更新时，通过 PR 评论提出，不直接代改。
- 对离线样本标明 `documented / synthetic` 与 `observed / inferred / unknown`；不能把模拟测试说成真实视觉识别或自动控制游戏已成功。
- 在适当环境运行**不发送实际鼠标/键盘操作**的离线单测；GUI/Windows 集成测试需要单独报告环境、限制与授权。CI、文档审查和普通代码任务不得触发真实游戏操作。
- 提交前运行 `git diff --check`，检查路径/账号/敏感信息、图片/日志例外、工作区状态，并提供 Commit SHA 与 PR URL。

**审查/验收顺序**：实现 Agent 自测 → 另一 Agent 可交叉审阅（只读）→ CTO Review → Founder 本地验收（如适用）→ **Founder 手动 Merge**。即使 GitHub 显示 MERGEABLE 也不代表 CTO 已批准；CTO 通过评论记录意见，Founder 保留最终决定权。

---

## 12. 实机测试与证据上传门禁

- **默认不执行**真实游戏或浏览器操作实验；必须由 Founder 针对具体 Issue、目标窗口、操作范围和停止条件**提前授权**。HumanExecutor 模式依然需要逐步观察并验证，不能以“人说已完成”代替游戏状态证据。
- 对《梦幻西游》保持最小权限，不提权、不绕过启动器/反作弊/完整性边界，不修改系统安全设置；尚无合法、受支持的自动输入通道前，只使用只读观察或经授权的人类操作。
- 对 POC-002 图片仅允许**候选整理**；逐张裁剪到最小必要 UI 区域，遮蔽角色/账号/服务器/主机识别信息，由 Founder 审批、CTO 复核后，才能在单独 Issue/PR 中加入私有 fixture。没有逐张批准时不得批量导入、提交或上传。
- 任何转录、标注及研究结论必须可追溯到本地证据或明确标为合成/推断；截图不在仓库时不能宣称已独立验证像素级准确率。历史 `docs/pocs/` 仍只读，需要勘误则用 append-only Erratum 与独立 Issue/PR。
