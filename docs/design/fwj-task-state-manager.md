# 梦幻西游任务状态管理器 — 架构设计（Issue #7）

| 项目 | 内容 |
|---|---|
| 状态 | **设计稿（design-only）**，无实现代码、无实时游戏交互 |
| 关联 Issue | [#7 Design a vision-first task state manager for Fantasy Westward Journey](https://github.com/yanxinnnnnn/desktop-game-agent-lab/issues/7) |
| 依据 | POC-001（截图/识别/输入兼容性）、POC-002（连续视觉决策，15 步）、POC-003（提权来源）、POC-005（Desktop Engine） |
| 本稿范围 | 数据契约、状态模型、循环、适配器边界、恢复策略、离线评估、实现拆分 |

---

## 1. 目标与非目标

### 1.1 目标

让 Agent 在《梦幻西游》里**持久地知道自己处于任务的哪一步**，并能：
- 用截图理解当前场景/UI/任务目标；
- 维护跨多步、跨多轮对话的任务状态；
- 在人工执行输入的前提下给出**单步、可验证**的指令；
- 对每一次操作给出"成功 / 失败 / 存疑"的判定，存疑时停下而不是猜。

### 1.2 非目标（本 Issue 明确不做）

- 不做实时游戏操作实验；
- 不解决 High IL / Medium IL 的输入隔离（见 §2）；
- 不写像素级识别实现（只在接口层约定输出）；
- 不触碰游戏客户端、启动器、反作弊、内存或网络协议。

---

## 2. 已知约束（必须在设计中显式保留）

| 编号 | 约束 | 证据 | 状态 |
|---|---|---|---|
| C1 | 游戏进程为 **High IL**，Harness 为 **Medium IL**；合成输入无法可靠作用到游戏 | POC-001：`SetCursorPos` 在游戏前台时 4/4 失败；POC-003：`mhtab`/`mhmain` = ELEVATED | **Observed** |
| C2 | 上述失败由 **UIPI** 解释是**强推断，未被严格证明** | POC-001 报告明确标注"未证明" | **Inferred** |
| C3 | POC-002 的 15 步输入全部由**人类执行**，Agent 只做观察与决策 | POC-002 §2.2 | **Observed** |
| C4 | 输入通道在可预见的未来仍是"人工执行"或"经批准的同权限执行器" | POC-003 结论 | **Observed** |
| C5 | 原始截图/日志不随仓库分发，仓库内只有文字记录 | `docs/evidence/README.md` | **Observed** |

**设计含义**：本架构把"执行"抽象成一个可替换的执行器（HumanExecutor / 未来的同权限 Executor），
状态机与判定逻辑不依赖执行器是谁。**不允许**为了绕开 C1 去提权、改启动器或修改安全设置。

---

## 3. 分层架构

```
┌───────────────────────────────────────────────────────────┐
│  Task State Manager（游戏无关）                             │
│  · 维护 TaskState / revision                               │
│  · Plan：选下一个 ActionIntent                             │
│  · UpdateState：应用 VerificationResult                    │
│  · 停止规则 / 幂等 / 恢复编排                               │
└───────────────▲───────────────────────────┬───────────────┘
                │ Observation（结构化）       │ ActionIntent（语义）
┌───────────────┴───────────────────────────▼───────────────┐
│  FWJ Adapter（梦幻西游专属）                                │
│  · 场景/坐标/任务追踪/对话框/面板/悬浮提示 解析              │
│  · 语义目标 → 屏幕坐标（基于当帧锚点）                      │
│  · 游戏键位与已知控件语义                                  │
└───────────────▲───────────────────────────┬───────────────┘
                │ 截图 / 窗口信息            │ 点击 / 按键
┌───────────────┴───────────────────────────▼───────────────┐
│  Desktop Engine（`engine/Desktop.psm1`，游戏无关）          │
│  Get-WindowInfo / Get-WindowScreenshot / Set-TargetWindowFocus │
│  Invoke-KeyPress / Invoke-MouseClick / ConvertTo-ScreenPoint    │
└───────────────────────────────────────────────────────────┘
```

**三条硬边界**

1. `Desktop.psm1` 不得出现任何游戏名词、任务概念或 2048 规则（沿用 Issue #7 的要求）。
2. FWJ Adapter 不得直接调用 `SendInput`，只能产出 `ActionIntent` 交给执行器。
3. Task State Manager 不得读取像素，只能消费 Adapter 产出的结构化 `Observation`。

---

## 4. 数据契约（versioned, typed）

所有对象带 `schema_version`。字段分三级证据：`observed` / `inferred` / `unknown`。

### 4.1 `Observation`

一次观察的完整快照。

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | string | 例如 `"1.0"` |
| `observation_id` | string | ULID/UUID |
| `captured_at` | ISO8601 | 采集时刻 |
| `window` | object | `{hwnd, title, pid, elevation, x, y, width, height}` —— 来自 `Get-WindowInfo` |
| `screenshot` | object | `{sha256, width, height, local_ref}`；`local_ref` 为本地路径，**不入库** |
| `ui_mode` | enum | `world` / `dialog` / `panel` / `modal` / `loading` / `unknown` |
| `scene` | object | `{name, coords:{x,y}, confidence}`，例如 `建邺城 [195,23]` |
| `quest` | object | `{quest_id, title, objective_text, target_name, confidence}` |
| `dialog` | object? | `{speaker, text, options[], continue_affordance, confidence}` |
| `panels` | array | `[{name, confidence}]`，例如 `宠物`、`召唤兽属性` |
| `interactables` | array | `[{semantic_id, label, region, confidence}]` |
| `anomalies` | array | 检测到的异常：`unknown_modal` / `page_scrolled` / `focus_lost` |
| `confidence` | enum | 整体置信度，取各字段最低档 |

**约定**：`objective_text` 必须逐字保留游戏文案（如"设置召唤兽参战，再与人族使者对话"），
因为它是唯一由游戏写入、可作权威校验的状态源（POC-002 §2.1）。

### 4.2 `TaskState`

Agent 长期维护的"我认为现在处于哪一步"。

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | string | `"1.0"` |
| `session_id` | string | 一次会话 |
| `task_id` | string | 例如 `新手试炼` |
| `stage` | string | 任务阶段标识，如 `talk_to_envoy` / `set_pet_active` / `equip_gear` |
| `objective` | object | `{text, target_name, evidence: observed\|inferred\|unknown}` |
| `location` | object | `{scene, coords}` |
| `ui_expectation` | enum | 期望的 UI 模式（`world` / `dialog` / `panel`） |
| `subgoals` | array | 未完成子目标队列 |
| `flags` | object | 布尔事实，如 `pet_active=true`、`dialog_open=true` |
| `pending_decision` | object? | 需人类批准的不可逆选择 `{question, options[], risk}` |
| `revision` | int | 每次成功更新 +1 |
| `confidence` | enum | `observed` / `inferred` / `unknown` |
| `open_questions` | array | 已知不确定项（必须随状态一起展示） |
| `evidence_refs` | array | 支撑该状态的 `observation_id` 列表 |

**关键约定**

- `revision` 单调递增；执行器在执行前必须回传它看到的 `revision`，防止"基于旧状态执行"。
- `objective.evidence` 为 `observed` 仅当本帧任务追踪文本被成功读取；若沿用上一帧，则为 `inferred`。

### 4.3 `ActionIntent`

一条**语义化**指令，不含像素坐标以外的游戏逻辑。

| 字段 | 类型 | 说明 |
|---|---|---|
| `intent_id` | string | 幂等键 |
| `kind` | enum | `click_semantic` / `key` / `hover_probe` |
| `target` | object | `{semantic_id, label, resolved_point}`；坐标为当帧换算结果 |
| `key` | string? | 例如 `Down`、`Tab`、`Alt+P` |
| `preconditions` | array | 执行前必须成立的断言（如 `ui_mode=dialog`） |
| `expected_postcondition` | object | 可判定的期望（如 `quest.text` 变化、`panels` 含 `召唤兽属性`） |
| `risk` | enum | `reversible` / `irreversible` |
| `requires_human_approval` | bool | 不可逆操作必须为 `true` |
| `state_revision` | int | 生成该指令时的状态版本 |
| `ttl_ms` | int | 超时后作废，需重新观察 |

### 4.4 `VerificationResult`

| 字段 | 类型 | 说明 |
|---|---|---|
| `intent_id` | string | 对应指令 |
| `verdict` | enum | `confirmed` / `rejected` / `ambiguous` |
| `checks` | array | `[{name, expected, actual, pass}]` |
| `state_delta` | object | 建议的状态补丁（仅在 `confirmed` 时应用） |
| `evidence` | object | `{before_observation_id, after_observation_id, diff_summary}` |
| `error_code` | string? | 失败分类：`E_NO_CHANGE` / `E_UNEXPECTED_UI` / `E_LOW_CONFIDENCE` / `E_FOCUS_LOST` / `E_BLOCKED_INPUT` |
| `confidence` | enum | 判定本身的置信度 |

**判定三角**（缺一不可）：
1. **执行器回执**（人："完成" / 执行器：返回值）—— **不作为成功依据**；
2. **前后帧差异**（UI 是否按期望变化）；
3. **规则/文案校验**（例如任务追踪文案是否推进）。

---

## 5. 主循环

```
OBSERVE → INTERPRET → PLAN → EXECUTE(authorized) → VERIFY → UPDATE_STATE
   ↑                                                            │
   └──────────────── 需要新证据 / 恢复时回到起点 ────────────────┘
```

| 阶段 | 输入 | 输出 | 失败处理 |
|---|---|---|---|
| Observe | 窗口句柄 | `Observation`（含截图哈希） | 截图失败 → 有限重试 1 次，仍失败则停 |
| Interpret | `Observation` | 结构化场景（Adapter） | 关键字段低置信 → `ambiguous`，不进入 Plan |
| Plan | `TaskState` + 场景 | 一个 `ActionIntent` | 无合法动作 → `blocked`，请求人工 |
| Execute | `ActionIntent` | 执行回执 | 前台不符/输入被拒 → `E_BLOCKED_INPUT`，停 |
| Verify | 执行前后两帧 | `VerificationResult` | `ambiguous` → 重观察一次，仍歧义则停 |
| UpdateState | `VerificationResult` | 新 `TaskState`（revision+1） | `rejected` → 进入恢复策略 |

### 5.1 幂等与去重

- `ActionIntent.intent_id` 由 `(state_revision, semantic_id, kind)` 派生；
- 执行前检查 `state_revision` 是否仍等于当前值，不等则**丢弃指令并重新观察**；
- 执行前若 `expected_postcondition` 已成立（例如面板已打开），**直接跳过执行**，避免重复点击。

### 5.2 停止规则（任一触发即停）

| 编号 | 条件 | 理由 |
|---|---|---|
| S1 | 连续 2 次 `ambiguous` 判定 | 无法确认结果，继续就是盲操作 |
| S2 | 连续 3 步 `revision` 未变（无进展） | 状态机卡住 |
| S3 | 出现未知 `modal` / 登录提示 / 页面跳转 | POC-004 的教训 |
| S4 | 关键字段（任务文案/场景）连续 2 帧低置信 | 识别不可靠 |
| S5 | 需要不可逆操作且无人工批准 | 安全边界 |
| S6 | 输入被拒且原因无法消除（UIPI/权限） | C1 约束 |
| S7 | 窗口身份与 `TaskState.window` 不符 | 防止操作错窗口 |

### 5.3 人类执行器协议

在 C1/C3 未解决前，执行器是**人**：

1. Agent 输出**一个** `ActionIntent`（含语义目标 + 屏幕坐标 + 预期结果）；
2. 人执行后回复固定词（如"完成"）；
3. Agent **必须重新截图**验证，不得把"完成"当作成功；
4. 若人回复"没反应/点错了"，视为 `rejected`，进入恢复。

---

## 6. 任务状态机（以 POC-002「新手试炼」为例）

> 以下阶段名来自 POC-002 实际观察到的任务追踪文案；属于**当时截图观测到的记录**，
> 仓库中不再保留原始截图（见 C5），因此本仓库内只能作为 **documented observation** 引用。

```
[unknown]
   │ 观察：任务追踪 = 「人族使者似乎有事找自己」
   ▼
STAGE_1 talk_to_envoy
   │ 点击「人族使者」→ 对话打开（3 选项）
   │ 选「主线任务」→ 对话正文
   ▼
STAGE_1_DIALOG  (线性对话，点击正文推进)
   │ 出现 4 选项（初始召唤兽）
   ▼
DECISION_PENDING ──(不可逆，需批准)──► 选「大海龟」
   │ 获得召唤兽；任务追踪首次变化
   ▼
STAGE_2 set_pet_active
   │ objective = 「设置召唤兽参战，再与人族使者对话」
   │ 打开 宠物 → 召唤兽 → 参战
   │ 按钮由「参战」变为「休息」+ 宠物血条出现
   ▼
STAGE_2_DONE_PENDING_TURNIN
   │ 再与人族使者对话 → 选「主线任务」
   ▼
STAGE_3 equip_gear
   │ objective = 「穿好武器与衣服，再与人族使者对话」
   ▼
(本 Issue 不设计 STAGE_3 之后的流程)
```

**状态不变量**

- `stage` 只能由 `quest.objective_text` 的变化驱动，不能由"我点了按钮"推断。
- `flags.pet_active` 为 `true` 需要**两条独立证据**之一：按钮文案为「休息」，或角色头像旁出现宠物血条（POC-002 第 13 步同时观测到两者）。
- `objective.evidence=observed` 仅在当前帧读到任务追踪时成立。

---

## 7. 任务状态转换示例（POC-002 逐步映射）

> 说明：下表把 POC-002 的 15 次操作映射为状态机事件。
> "观测"列来自 POC-002 报告的文字记录；**截图本身未入库**，故本表是 **transcript-level** 证据，非像素级 ground truth。

| 步 | 观测（记录） | 状态变化 | 验证方式 | revision |
|---|---|---|---|---|
| 0 | 任务追踪：「人族使者似乎有事找自己」 | `stage=talk_to_envoy` | 文案读取 | 0 |
| 1 | 点击后对话框弹出，坐标 `[183,25]→[195,23]` | `ui_mode=dialog`, `flags.dialog_open=true` | 前后帧 + 坐标变化 | 1 |
| 2 | 选「主线任务」 | 对话进入正文 | 文本变化 | 2 |
| 3–6 | 正文逐句推进 | 无状态字段变化（仍在 `dialog`） | 文本变化 | 2（不变） |
| 7 | 出现 4 选项 | `flags.options_visible=true`，`pending_decision=初始召唤兽` | 选项列表 | 3 |
| 8 | 选「大海龟」 | `loop` 暂停 → 人工批准 → 追踪首次变化 | 任务文案变化 | 4 |
| 9 | 点「×」关闭对话 | `ui_mode=world`, `flags.dialog_open=false` | 对话框消失 | 5 |
| 10 | 悬停老虎头 → tooltip「宠物(Alt+P)」 | `interactables` 学习：`open_pets` | tooltip 文本 | 5（不变） |
| 11 | 打开「宠物」面板 | `panels=[宠物]` | 面板标题 | 6 |
| 12 | 点「召唤兽」→「召唤兽属性」 | `panels=[宠物,召唤兽属性]` | 面板标题 + 大海龟 0 级 | 7 |
| 13 | 点「参战」 | `flags.pet_active=true`（按钮→休息 + 血条） | 双证据 | 8 |
| 14 | 再点「人族使者」，对话重开 | `ui_mode=dialog` | 对话框 | 9 |
| 15 | 选「主线任务」 | `stage=equip_gear`，objective 变为「穿好武器与衣服…」 | **任务文案推进** | 10 |

**这个例子要说明的三件事**

1. 15 次操作只产生了 10 次状态 revision —— 中间多步是"同一状态内的对话推进"，不应触发状态机迁移。
2. 第 8 步是**唯一需要人工批准**的不可逆选择。
3. 第 13 步的"设置成功"与"任务完成"是两件事：`flags.pet_active=true` 但任务没推进，必须回到 NPC 交付（第 14–15 步）——这正是 `stage` 与 `flags` 必须分开建模的原因。

---

## 8. 异常恢复策略

| 症状 | 检测 | 处理 | 上限 |
|---|---|---|---|
| 窗口不在前台 | `Get-WindowInfo().IsForeground == false` | `Set-TargetWindowFocus` → 重新观察 | 1 次 |
| 画面被遮挡/截图异常 | 截图哈希异常、尺寸不符 | 重新聚焦 + 重截 | 1 次 |
| 页面/场景滚动或视角变化 | 锚点（任务追踪面板）位置偏移 > 阈值 | 重新定位锚点；失败则停 | 1 次 |
| 未知弹窗 | 出现非预期 `modal` 且文案不匹配已知集合 | **立即停**，不点任何按钮 | 0 |
| 对话未按预期推进 | 前后帧任务/对话文本均未变 | 重观察一次；仍不变 → `E_NO_CHANGE` | 1 次 |
| 低置信识别 | 关键字段 `confidence=unknown` | 重观察；连续 2 次 → 停 | 2 次 |
| 连续无进展 | `revision` 3 步未变 | 停，请求人工 | — |
| 输入被拒 | 执行器返回焦点/权限错误 | **不重试**，报告 C1 约束 | 0 |
| 任务文案意外跳变 | 新 objective 不属于已知阶段集合 | 停，转人工确认 | 0 |

**恢复的硬规则**：任何"重试"都必须伴随**一次新观察**，不允许"再点一次试试"。

---

## 9. 与 Desktop Engine 的接口边界

### 9.1 可直接复用（`engine/Desktop.psm1`）

| 能力 | 接口 | 复用理由 |
|---|---|---|
| 窗口定位与身份 | `Get-WindowInfo -TitleLike` | 已含 PID/完整性级别/矩形，且每次重新查询 |
| 截图 | `Get-WindowScreenshot` | 返回 Bitmap + 当帧原点，便于坐标换算 |
| 前台恢复 | `Set-TargetWindowFocus` | 有限重试 + 结构化返回 |
| 键盘 | `Invoke-KeyPress` | 输入前校验前台 |
| 鼠标 | `Invoke-MouseClick` | 越界拒绝 + 前台校验 |
| 坐标换算 | `ConvertTo-ScreenPoint` | 截图坐标 → 屏幕物理坐标 |

### 9.2 必须新建（FWJ Adapter，`adapters/fwj/`）

| 组件 | 职责 | 不得依赖 |
|---|---|---|
| `QuestTrackerParser` | 从固定区域读出任务标题/目标/红色关键词 | 不得写死在 `Desktop.psm1` |
| `DialogParser` | 说话人、正文、选项、继续控件 | 不得直接点击 |
| `PanelDetector` | 识别「宠物」「召唤兽属性」等面板标题与层级 | — |
| `TooltipProbe` | 悬停读取 tooltip，作为控件语义 | 悬停不算操作 |
| `SemanticTargetResolver` | 名称/标签 → 当帧屏幕坐标 | 不得跨帧缓存坐标 |
| `FwjKeymap` | 已确认的键位（如 `Tab` 地图、`宠物(Alt+P)`） | 未验证的键位不得写入 |
| `TaskMachine` | 阶段定义与迁移规则 | 不得调用输入 |

### 9.3 明确禁止

- 在 `Desktop.psm1` 里加游戏分支；
- 在 Adapter 里调用 `SendInput`；
- 把 2048 的 `Game2048.psm1` 规则/计分逻辑引入 Adapter（Issue #7 明确要求）；
- 用"坐标写死"代替语义目标解析。

---

## 10. 证据等级与不确定性（必须随设计一起交付）

| 结论 | 等级 | 说明 |
|---|---|---|
| 任务追踪文案是可靠状态源 | **Observed**（POC-002 当时） | 但原始截图未入库，仓库内为文字记录 |
| 15 步操作均产生可观测变化 | **Observed**（POC-002 当时） | 同上 |
| 「宠物」与「召唤兽」是两套面板 | **Observed** | 第 11–12 步 |
| `pet_active` 可由按钮文案 + 血条双证据确认 | **Observed** | 第 13 步 |
| 输入无法作用于游戏是 UIPI 导致 | **Inferred** | POC-001/003 明确未证明 |
| 任务阶段可推广到其他任务 | **Unknown** | 只有「新手试炼」一个样本 |
| 对话「‖」控件的语义 | **Unknown** | POC-002 回避了它 |
| 状态机能承受对话分支/失败重试 | **Unknown** | POC-002 是"一次过"的顺利路径 |

> 本设计不声称上述任何 Unknown 已被解决；实现阶段的验收必须补充真实样本。

---

## 11. 后续实现拆分

| 里程碑 | 内容 | 产出 | 风险 |
|---|---|---|---|
| **M1** | 数据契约落地（JSON Schema + 校验） | `adapters/fwj/schema/*.json`，字段校验测试 | 字段过早固化 |
| **M2** | 离线转录回放（POC-002 15 步） | 状态机 + 回放测试 | 转录非像素级，覆盖不足 |
| **M3** | FWJ Adapter 只读识别（任务追踪/对话框/面板） | 纯解析函数 + 样本图（需批准） | 无样本图则只能合成 |
| **M4** | 人工执行器闭环（Observe→…→Verify） | 端到端脚本（人执行） | 依赖人工，吞吐低 |
| **M5** | 受控输入通道评估（不绕过安全边界） | 评估报告，非实现 | 受 C1 限制，可能长期无解 |

**建议**：M1–M2 可以**完全离线**完成，不依赖游戏；M3 需要获批的脱敏样本图；M4 需要人工在场；M5 单独立项。

---

## 12. 风险与未决问题

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | UIPI 未证实且短期无解 | 只能人工执行，无法全自动 | 架构上隔离执行器，不阻塞其余部分 |
| R2 | 只有单一任务样本 | 状态机可能过拟合 | M2 用转录回放，M3 起补充新任务样本 |
| R3 | 无像素级 ground truth 入库 | 识别精度无法回归 | 离线评估基于转录 + 合成图，明确标注局限 |
| R4 | 游戏 UI 更新 | 解析器全部失效 | 解析器与状态机解耦，失败即停 |
| R5 | 不可逆操作误触 | 账号/资产风险 | `risk=irreversible` 强制人工批准 |

---

## 13. 与既有文档的关系

- POC-002 的 15 步是本设计唯一的真实任务样本；
- POC-001 / POC-003 提供输入与权限约束（C1–C4）；
- POC-005 提供可复用的 Desktop Engine 与"不信任 SendInput 返回值"的验证范式；
- 离线评估方案见 [`fwj-offline-eval-plan.md`](fwj-offline-eval-plan.md)。
