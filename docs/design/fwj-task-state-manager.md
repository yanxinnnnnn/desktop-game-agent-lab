# 梦幻西游任务状态管理器 — 架构设计（Issue #7）

| 项目 | 内容 |
|---|---|
| 状态 | **设计稿（design-only）**，无实现代码、无实时游戏交互 |
| 关联 Issue | [#7 Design a vision-first task state manager for Fantasy Westward Journey](https://github.com/yanxinnnnnn/desktop-game-agent-lab/issues/7) |
| 评审 | PR #8 第二轮修订（回应 CTO 审查意见：revision/幂等冲突、对象一致性、判定语义、Planner 边界、评估口径） |
| 依据 | POC-001（截图/识别/输入兼容性）、POC-002（连续视觉决策，15 步）、POC-003（提权来源）、POC-005（Desktop Engine） |

---

## 1. 目标与非目标

### 1.1 目标

让 Agent 在《梦幻西游》里**持久且可审计地知道**自己处于任务的哪一步，并能：
- 用截图理解场景 / UI / 任务目标；
- 维护跨多步、跨多轮对话的任务状态；
- 由**可替换的 LLM Planner** 提议下一步，由**确定性守卫**决定是否允许执行；
- 对每次操作给出"确认成功 / 确认失败 / 证据不足"的判定，证据不足时停下。

### 1.2 非目标

- 不做实时游戏操作实验；
- 不解决 High IL / Medium IL 的输入隔离（见 §2）；
- 不写像素识别实现；
- 不触碰客户端、启动器、反作弊、内存或网络协议。

---

## 2. 已知约束

| 编号 | 约束 | 证据 | 状态 |
|---|---|---|---|
| C1 | 游戏进程 **High IL**，Harness **Medium IL**；合成输入无法可靠作用到游戏 | POC-001：`SetCursorPos` 在游戏前台 4/4 失败；POC-003：`mhtab`/`mhmain` = ELEVATED | **Observed** |
| C2 | 该失败由 **UIPI** 解释是强推断，**未被严格证明** | POC-001 明确标注"未证明" | **Inferred** |
| C3 | POC-002 的 15 步输入全部由**人类执行** | POC-002 §2.2 | **Observed** |
| C4 | 输入通道仍将是"人工执行"或"经批准的同权限执行器" | POC-003 结论 | **Observed** |
| C5 | 原始截图/日志不随仓库分发 | `docs/evidence/README.md` | **Observed** |

**设计含义**：执行层被抽象成可替换的 `Executor`；状态机、守卫与验证逻辑不依赖执行者是谁。
**不允许**为绕开 C1 而提权、改启动器或修改安全设置。

---

## 3. 组件与职责

| 组件 | 性质 | 职责 | 明确不做 |
|---|---|---|---|
| **Desktop Engine**（`engine/Desktop.psm1`） | 通用、无游戏语义 | 窗口定位、截图、前台恢复、鼠标/键盘原语、坐标换算 | 不认识任务、不解析 UI |
| **FWJ Adapter** | 游戏专属、只读 | 把截图解析成 `Observation`（场景/任务/对话/面板/控件） | **不得调用任何输入原语** |
| **Task State Manager** | 确定性 | 维护 `TaskState`、应用验证结果、执行停止/恢复策略 | 不读像素、不提议动作 |
| **Planner**（LLM，可替换） | **不可信** | 基于 `TaskState` + 最近 `Observation` 提议 0..N 个 `ActionIntent` | 不得直接触达输入层 |
| **Policy Guard** | 确定性 | 校验风险等级、人工批准、TTL、窗口身份、前置条件、动作预算 | 不做语义推理 |
| **Executor**（人工 / 未来的同权限执行器） | 执行 | 只执行**经 Guard 批准**的 intent，返回回执 | 回执**不构成**成功证据 |
| **Verifier** | 确定性 | 用执行前后的两帧独立判定 `VerificationResult` | 不执行动作、不看回执 |

> **关键分离**：Planner 只"提议"，Guard 决定"能不能做"，Executor 只"做"，Verifier 只"判定"，
> Task State Manager 只"记账"。任何一方都不能替代另一方。

---

## 4. 架构图

```
                         ┌──────────────────────────────┐
                         │  Planner (LLM, replaceable)  │
                         │  in: TaskState + Observation │
                         │  out: ActionIntent 提议      │
                         └──────────────┬───────────────┘
                                        │ proposal（不可信）
                                        ▼
                         ┌──────────────────────────────┐
                         │  Policy Guard (deterministic)│
                         │  risk / approval / TTL /     │
                         │  window identity / precond.  │
                         └──────────────┬───────────────┘
                                        │ approved intent
                                        ▼
                         ┌──────────────────────────────┐
                         │  Executor (human or agent)   │
                         │  唯一允许触达输入层的组件     │
                         └──────────────┬───────────────┘
                                        │ receipt（仅作上下文）
                                        ▼
                         ┌──────────────────────────────┐
                         │  Desktop Engine (input side) │
                         └──────────────────────────────┘

   ┌────────────────┐  screenshot  ┌──────────────┐  Observation  ┌──────────────────────┐
   │ Desktop Engine │ ───────────► │ FWJ Adapter  │ ────────────► │ Task State Manager   │
   │ (capture side) │              │  (read-only) │               │ (deterministic)      │
   └────────────────┘              └──────┬───────┘               └──────────┬───────────┘
                                          │ frames                           │ state
                                          ▼                                  ▼
                                   ┌──────────────┐                 ┌──────────────────┐
                                   │  Verifier    │ ── result ────► │  TaskState v_n+1 │
                                   │ (before/after│                 └──────────────────┘
                                   │  两帧独立判定)│
                                   └──────────────┘
```

**边界规则**

1. Adapter 只产出 `Observation`，**永不带坐标去点击**；坐标只出现在 `ActionIntent` 里，由 Guard 校验。
2. Executor 只接受 Guard 批准的 intent；Planner 无法直接把 intent 交给 Executor。
3. Verifier 与 Executor 相互独立：Executor 的回执只是日志字段。

---

## 5. 数据契约（versioned, typed）

四个对象均带 `schema_version`。字段证据等级：`observed` / `inferred` / `unknown`。

### 5.1 `Observation`

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | string | `"1.1"` |
| `observation_id` | string | ULID，**全局唯一**，所有下游引用以它为准 |
| `observation_seq` | int | 会话内单调递增（每次截图 +1） |
| `captured_at` | ISO8601 | 采集时刻 |
| `window` | object | `{hwnd, pid, title_hash, rect:{x,y,w,h}, is_foreground, elevation}`，来自 `Get-WindowInfo` |
| `screenshot` | object | `{sha256, width, height, local_ref}`；`local_ref` 不入库 |
| `ui_mode` | enum | `world` / `dialog` / `panel` / `modal` / `loading` / `unknown` |
| `scene` | object | `{name, coords:{x,y}, confidence}` |
| `quest` | object | `{quest_id, title, objective_text, target_name, confidence}`；读不到时 `confidence=unknown` |
| `dialog` | object? | `{speaker, text, text_hash, options[], continue_affordance, confidence}` |
| `panels` | array | `[{name, confidence}]` |
| `interactables` | array | `[{semantic_id, label, region, confidence}]` |
| `anomalies` | array | `unknown_modal` / `scroll` / `focus_lost` / `occluded` |
| `observation_quality` | enum | `sufficient` / `degraded` / `unusable`（按目标区域是否清晰可读） |

**约定**：`objective_text` 逐字保留游戏文案——它是唯一由游戏写入的权威状态源（POC-002 §2.1）。

### 5.2 `TaskState`

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | string | `"1.1"` |
| `session_id` | string | 会话 |
| `task_id` | string | 如 `新手试炼` |
| `stage` | string | 任务阶段，如 `talk_to_envoy` / `set_pet_active` / `equip_gear` / `unknown` |
| `stage_source` | enum | `observed`（本帧任务文案确认）/ `inferred`（沿用上一帧或推断）/ `unknown` |
| `objective` | object | `{text, target_name, evidence}` |
| `location` | object | `{scene, coords}` |
| **`window`** | object | `{hwnd, pid, title_hash}` —— 供 S7 身份校验（此前缺失） |
| `ui_expectation` | enum | 期望 UI 模式 |
| `flags` | object | `{name: {value, evidence, observations[]}}`，例如 `pet_active` |
| `pending_decision` | object? | `{question, options[], risk}` |
| `revision` | int | **语义状态版本**，见 §6 |
| `observation_seq` | int | 已消化的最新观察序号 |
| `last_attempt` | object? | `{attempt_id, status, verification_id}` |
| `open_questions` | array | 已知不确定项 |
| `evidence_refs` | array | 支撑当前状态的 `observation_id` |

### 5.3 `ActionIntent`

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | string | `"1.1"` |
| `intent_id` | string | 每次**尝试**唯一（见 §6），不用于"同状态去重" |
| `attempt_no` | int | 会话内单调递增 |
| `proposed_by` | enum | `planner` / `operator` |
| `kind` | enum | `click_semantic` / `key` / `hover_probe` |
| `target` | object | `{semantic_id, label}` |
| `resolved_point` | object | `{x, y}`，属于 `origin_observation` 的坐标空间；**执行前必须以 guard 帧重新解析**，不复用旧坐标 |
| **`origin_observation`** | object | `{observation_id, observation_seq, captured_at, window:{hwnd, pid, title_hash, rect}}` |
| `key` | string? | 如 `Tab`、`Alt+P` |
| `preconditions` | array | 不可变断言列表（见 §6.3） |
| `expected_postcondition` | object | 可判定期望，如 `{type:"dialog_text_changed"}` |
| `risk` | enum | `reversible` / `irreversible` |
| `requires_human_approval` | bool | 不可逆必须 true |
| `created_at` | ISO8601 | 提议时刻 |
| `expires_at` | ISO8601 | **显式字段**：`created_at + ttl_ms`；Guard 必须校验 `now <= expires_at` |
| `ttl_ms` | int | 生存期上限（建议 3000–5000 ms，需按实测 UI 响应调整） |
| `state_revision` | int | 生成时的 `TaskState.revision`；**Guard 强制校验其等于当前值**，不等即 `E_STALE_INTENT` |

### 5.4 `VerificationResult`

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | string | `"1.1"` |
| `verification_id` | string | ULID |
| `intent_id` | string | 对应尝试 |
| `before_observation_id` / `after_observation_id` | string | 两帧 |
| `verdict` | enum | `confirmed` / `rejected` / `ambiguous`（语义见 §7） |
| `checks` | array | `[{name, expected, actual, pass}]` |
| `state_patch` | object | 建议的状态补丁（仅 `confirmed` 时应用） |
| `error_code` | string? | `E_NO_CHANGE` / `E_UNEXPECTED_UI` / `E_LOW_CONFIDENCE` / `E_OCCLUDED` / `E_WINDOW_CHANGED` / `E_BLOCKED_INPUT` |
| `observation_quality` | enum | 判定所依赖区域的清晰度 |
| `executor_receipt` | object? | **仅作上下文审计，不参与判定** |

---

## 6. 版本与序号模型（本次修订的核心）

此前设计把"状态版本"同时当作幂等键与无进展判据，导致两个错误：
连续对话会被去重、正常对话会触发"卡住"。现拆成**三个独立概念**。

| 概念 | 字段 | 何时变化 | 用途 |
|---|---|---|---|
| **观察序号** | `observation_seq` | 每次截图 +1 | 关联"这条指令基于哪一帧" |
| **状态版本** | `revision` | 任何**语义上可观测且被采纳**的状态字段变化时 +1 | 审计、防止基于旧状态更新 |
| **尝试序号** | `attempt_no` / `intent_id` | **每次提议执行 +1** | 幂等：同一次尝试绝不执行两次 |
| **阶段** | `stage` / `stage_source` | 任务阶段迁移时变（≠每次 revision） | 任务进度 |

### 6.1 什么算一次 `revision` 更新

`revision +1` 当且仅当以下任一字段的**语义值**发生变化且被采纳：

| 字段 | 变化判定 |
|---|---|
| `stage` | 阶段标识改变 |
| `objective.text` | 任务文案改变 |
| `ui_mode` | 世界 / 对话 / 面板 / 弹窗切换 |
| `panels` | 面板集合增删 |
| `dialog.text_hash` | 对话正文改变（**同一句重复观察不算**） |
| `dialog.options` | 选项集合改变 |
| `interactables` | 语义控件集合增删，或某条目 `semantic_id` / `label` 改变（**tooltip 学习属于此类**） |
| `flags.*` | 取值或证据等级改变 |

**不**因为"又截了一张图"而 +1：观察序号 `observation_seq` 负责逐帧记录，`revision` 只记录语义变化。

**因此**：对话每推进一句（`dialog.text_hash` 变化）→ `revision +1`，但 `stage` 不变。
连续 4 次"点击继续"会产生 4 次 revision，**不会被误判为卡住**。

### 6.2 幂等键

- `intent_id = hash(session_id, attempt_no, kind, semantic_id, origin_observation.observation_id)`；
- **同一次尝试**重复提交 → 丢弃（`E_DUPLICATE_ATTEMPT`）；
- **不同尝试**指向同一目标（例如连续点两次"继续"）→ **允许**，因为 `attempt_no` 不同。

> 旧设计用 `(state_revision, semantic_id, kind)` 作键，会在同一 revision 内把第二次"继续"吃掉——已废弃。

### 6.3 执行前校验（Guard 必做，顺序不可调换）

Guard 的流程固定为 A→B→C→D；任何一步不通过都返回 `E_STALE_INTENT` 并**丢弃本次尝试**（不重试原 intent，重新规划）。

**Step A — 取一帧 guard-time 观察**
Guard 必须**新截一帧**（得到 `observation_seq_guard`），不复用 intent 生成时的帧。

**Step B — 逐项比对**

| # | 校验 | 不通过时 |
|---|---|---|
| 1 | `now <= intent.expires_at` | 过期 → 丢弃 |
| 2 | `intent.state_revision == TaskState.revision` | 状态已变 → 丢弃（**强制项**） |
| 3 | `window.hwnd`、`pid`、`title_hash` 与 `intent.origin_observation.window` 一致 | 窗口身份变化 → 丢弃 |
| 4 | guard 帧的窗口矩形与 origin 的矩形**完全一致** | 窗口移动/缩放 → 丢弃并重新规划 |
| 5 | guard 帧解析出的 `stage` 与生成 intent 时一致 | 阶段已变 → 丢弃 |
| 6 | `preconditions` 在 guard 帧上重新解析后仍成立 | 前置条件失效 → 丢弃 |
| 7 | `risk=irreversible` 时存在有效人工批准记录 | 无批准 → 拒绝 |

**Step C — 坐标绑定**
只有 Step B 全部通过后，才用 **guard 帧**重新解析目标坐标；`resolved_point` 必须以 guard 帧的坐标空间为准，**不得**沿用 origin 帧的坐标去点击。

**Step D — 执行**
Executor 收到的 intent 携带 `origin_observation = guard 帧`；回执写入 `executor_receipt`。

> **关于窗口几何**：不做"2px 以内算安全"这类近似——不同 DPI 与 UI 布局下，同样的像素偏移含义不同。
> 几何一变就直接丢弃重规划；这比"容差"更保守，也更容易解释。
>
> **关系链**：`新观察 → 身份/几何/阶段/前置条件/版本比对 → 绑定到新帧 → 执行`。
> TTL 与 `state_revision` 只是这条链上的两道快速否决，**不能替代**对 guard 帧的重解析。

---

## 7. 判定语义（VerificationResult）

| verdict | 含义 | 成立条件 | 后续动作 |
|---|---|---|---|
| `confirmed` | 期望的状态变化**被观察到** | `expected_postcondition` 在 after 帧为真，且相关区域 `observation_quality=sufficient` | 应用 `state_patch`，`revision+1` |
| `rejected` | **确认没有发生** | 相关区域清晰可读（`sufficient`）且后置条件明确为假 | 进入恢复；连续 2 次则停 |
| `ambiguous` | **证据不足，无法判定** | 区域被遮挡 / 低置信 / 只拿到一帧 / 变化与期望无关 | 重新观察 1 次；仍歧义则停 |

**执行器回执**（含人工回复"完成"）只写入 `executor_receipt` 作为审计上下文，
**不参与** verdict 计算。判定完全由 before/after 两帧 + 后置条件决定。

> 旧文档同时说"回执不是成功依据"和"判定三角缺一不可"，自相矛盾——已改为：回执 = 上下文，证据 = 两帧。

---

## 8. 主循环

```
OBSERVE → INTERPRET → PLAN → GUARD → EXECUTE → VERIFY → UPDATE_STATE
   ↑                                                          │
   └──────────── 需要新证据 / 恢复 / 状态不符时回到起点 ────────┘
```

| 阶段 | 执行者 | 失败处理 |
|---|---|---|
| Observe | Desktop + Adapter | 截图失败 → 重试 1 次；仍失败则停 |
| Interpret | Adapter | 关键字段 `unusable` → `ambiguous`，不进 Plan |
| Plan | Planner（LLM） | 无合法动作 → `blocked`，请求人工 |
| Guard | Policy Guard | 任一校验失败 → `E_STALE_INTENT`，回 Observe |
| Execute | Executor | 前台不符/输入被拒 → `E_BLOCKED_INPUT`，停 |
| Verify | Verifier | 见 §7 |
| UpdateState | Task State Manager | `rejected` → 恢复策略 |

### 8.1 无进展判定（替换旧的"revision 不变"）

定义为：**连续 N 次尝试都没有产生"期望的可观测进展"**。

- 每次尝试都登记 `expected_postcondition`；
- 若连续 3 次 verdict 为 `rejected` 或 `ambiguous`（即期望变化始终没出现）→ 停（S2）；
- **不再**用 `revision` 计数，因为对话推进本身就是有进展（`revision` 会变），而卡住时 `revision` 才不变。

### 8.2 停止规则

| 编号 | 条件 | 说明 |
|---|---|---|
| S1 | 连续 2 次 `ambiguous` | 无法确认结果 |
| S2 | 连续 3 次未产生期望的可观测进展 | 见 §8.1 |
| S3 | 出现未知 `modal` / 登录提示 / 页面跳转 | POC-004 教训 |
| S4 | 关键字段连续 2 帧 `unusable` | 识别不可靠 |
| S5 | 不可逆操作无批准 | 安全边界 |
| S6 | 输入被拒且无法消除（C1） | 权限边界 |
| S7 | 当前 `window.hwnd/pid` ≠ `TaskState.window` | 防止操作错窗口 |
| S8 | 任务文案与当前 `stage` 冲突且无法调和 | 见 §9.3 |

---

## 9. 任务状态机

### 9.1 阶段迁移的驱动源

`stage` 的权威来源是**任务追踪文案**，但不再是"唯一允许的驱动"：

- 文案变化 → 解析出已知阶段 → `stage` 迁移，`stage_source=observed`；
- 文案缺失/不可读 → **保持 `stage` 不变**，`stage_source` 降为 `inferred`，写入 `open_questions`；
- 文案与当前阶段不符但无法映射 → `stage` 保持，`stage_source=unknown`，触发 S8 停止并请求人工；
- **禁止**把推断出来的阶段自动提升为 `observed`。

### 9.2 `pet_active` 的证据分级（统一规则）

| 等级 | 条件 |
|---|---|
| `observed` | **同时**满足：①「参战」按钮文案已变为「休息」；②角色头像旁出现宠物血条 |
| `inferred` | 只满足其中一条 |
| `unknown` | 两条都看不到 / 区域不可读 |

> 旧文档说"两条证据之一即可"，与 T6"仅按钮不能算 observed"矛盾——现统一为**两条都要**。
> 依据：POC-002 第 13 步当时**同时**观测到两者；单一证据不足以排除"按钮按下但未生效"。

### 9.3 阶段图（POC-002 样本）

```
[unknown]
   │ 文案：「人族使者似乎有事找自己」
   ▼
talk_to_envoy ──(对话/选项)──► set_pet_active
   │                              │ 文案：「设置召唤兽参战，再与人族使者对话」
   │                              │ 打开 宠物 → 召唤兽 → 参战
   │                              ▼
   │                        （pet_active=observed 后仍需回 NPC 交付）
   └──────────────► equip_gear
                     文案：「穿好武器与衣服，再与人族使者对话」
```

---

## 10. POC-002 回溯映射（建模约定，非观测事实）

> **重要**：下表的 `revision` 列是**本设计的建模约定**，不是 POC-002 当时的观测值。
> POC-002 只记录了操作、观察到的界面变化与判定；从未记录版本号。
> 表中 `stage` 与 `flags` 的变化有文字记录支撑（transcript-level），`revision` 仅供理解模型。

| 步 | 记录到的观察 | 建模变化 | stage | revision（约定） |
|---|---|---|---|---|
| 1 | 对话框弹出，坐标变化 | `ui_mode=dialog` | talk_to_envoy | +1 |
| 2 | 选「主线任务」→ 正文 | `dialog.text_hash` 变化 | talk_to_envoy | +1 |
| 3–6 | 正文逐句推进（3 次点击） | 每次 `dialog.text_hash` 变化 | talk_to_envoy | 每次 +1 |
| 7 | 出现 4 选项 | `dialog.options` 变化 | talk_to_envoy | +1 |
| 8 | 选「大海龟」 | 任务文案首次变化 | → set_pet_active | +1（含 stage） |
| 9 | 关闭对话 | `ui_mode=world` | set_pet_active | +1 |
| 10 | tooltip「宠物(Alt+P)」 | `interactables` 学习 | set_pet_active | +1 |
| 11 | 打开「宠物」 | `panels` 变化 | set_pet_active | +1 |
| 12 | 打开「召唤兽属性」 | `panels` 变化 | set_pet_active | +1 |
| 13 | 按钮→休息 + 血条 | `pet_active: unknown→observed` | set_pet_active | +1 |
| 14 | 对话重开 | `ui_mode=dialog` | set_pet_active | +1 |
| 15 | 选「主线任务」→ 文案推进 | 文案变化 | → equip_gear | +1（含 stage） |

**可断言的合同性不变量**（供测试使用，见离线方案 T13）：

1. `stage` 只在第 8、15 步发生迁移；
2. `revision` 严格单调递增；
3. `pet_active` 在第 13 步之前不为 `observed`；
4. 对话推进期间 `stage` 保持不变；
5. 最终 `stage=equip_gear`。

> 不变量不包含"revision 的绝对值"。

---

## 11. 与 Desktop Engine 的边界

### 11.1 复用（`engine/Desktop.psm1`）

`Get-WindowInfo`、`Get-WindowScreenshot`、`Set-TargetWindowFocus`、`Invoke-KeyPress`、`Invoke-MouseClick`、`ConvertTo-ScreenPoint`。

### 11.2 新建（`adapters/fwj/`）

`QuestTrackerParser`、`DialogParser`、`PanelDetector`、`TooltipProbe`、`SemanticTargetResolver`、`FwjKeymap`、`TaskMachine`。

### 11.3 禁止

- 在 `Desktop.psm1` 加游戏语义；
- Adapter 调用任何输入原语；
- 引入 `Game2048.psm1` 的规则/计分逻辑；
- Planner 直接把 intent 交给 Executor（必须经 Guard）。

---

## 12. 证据等级与未决项

| 结论 | 等级 |
|---|---|
| 任务追踪文案是可靠状态源 | **Observed**（POC-002 当时；截图未入库） |
| 15 步均产生可观测变化 | **Observed**（同上，transcript-level） |
| 宠物与召唤兽是两套面板 | **Observed** |
| `pet_active` 需按钮+血条双证据 | **Inferred**（来自单次观测的建模选择） |
| 输入失败是 UIPI 导致 | **Inferred** |
| 阶段模型可推广到其他任务 | **Unknown** |
| 对话「‖」控件语义 | **Unknown** |
| 状态机对失败/分支的耐受度 | **Unknown** |

---

## 13. 实现拆分

| 里程碑 | 内容 | 依赖 |
|---|---|---|
| **M1** | 契约落地（JSON Schema + 校验器 + revision/attempt 语义单测） | 无 |
| **M2** | 转录回放（POC-002 15 步 + 合成异常） | 无 |
| **M3** | 只读解析器（任务/对话/面板） | 需**逐张审核**的脱敏截图 |
| **M4** | 人工执行闭环 | 需人工在场 |
| **M5** | 输入通道评估（不绕过安全边界） | 单独立项 |

M1–M2 不依赖第二个任务；M3/M4 若要声称"可迁移"，需先补第二个任务样本（Q3）。

---

## 14. 风险

| # | 风险 | 缓解 |
|---|---|---|
| R1 | C1 短期无解 | 执行器可替换，不阻塞其余部分 |
| R2 | 单任务样本过拟合 | M2 只作回归，不作泛化声明 |
| R3 | 无像素级 ground truth | 明确标注 transcript-level |
| R4 | 游戏 UI 更新 | 解析器与状态机解耦，失败即停 |
| R5 | Planner 越权 | Guard 强制校验；Planner 无输入权限 |
