# 梦幻西游任务状态 — 离线评估方案（Issue #7）

| 项目 | 内容 |
|---|---|
| 状态 | 设计稿（design-only），不含实现 |
| 关联 | [#7](https://github.com/yanxinnnnnn/desktop-game-agent-lab/issues/7)、[fwj-task-state-manager.md](fwj-task-state-manager.md) |
| 版本 | 第二版（回应 PR #8 审查：修正 T3/T7/T10/T13，收紧评估口径） |

---

## 1. 评估边界

### 1.1 能离线评估

- 合同语义：`revision` / `attempt_no` / `stage` 的分离是否自洽；
- `ActionIntent` → 后置条件 → `VerificationResult` 的判定逻辑；
- 幂等、TTL/窗口校验、停止规则、恢复策略；
- 给一串观察，最终 `stage` 与 `flags` 是否符合不变量。

### 1.2 不能离线评估

| 项 | 原因 |
|---|---|
| 像素识别准确率（任务/对话/面板） | 无原始截图入库，且单任务样本不足以代表分辨率/字体/布局变化 |
| 对其他任务的泛化 | 只有「新手试炼」一个成功路径 |
| 时序与动画稳定时间 | 需要连续帧 |
| UIPI / 输入通道 | 需要真实游戏进程 |

> **本方案的结论不能外推**：它只说明"契约与判定逻辑在该 fixture 集上自洽"，
> 既不证明 OCR/视觉识别准确率，也不证明对其他任务可迁移。

---

## 2. 数据来源、证据等级与标注流程

| 来源 | 内容 | 标注 |
|---|---|---|
| POC-002 报告 §2 的 15 步表 | 每步"观察 / 指令 / 验证结果" | `documented`（转录级） |
| POC-002 §2.1 | 任务文案的三次演变 | `documented` |
| 人工构造的异常场景 | 覆盖失败路径 | `synthetic`（必须显式标注） |

**POC-002 当时有截图，但截图未随仓库分发**（C5）。因此仓库内样本一律是转录级。
任何"识别精度已被离线验证"的表述都是越界的。

### 2.1 标注流程（采纳 CTO 对 Q2 的决策）

1. **DeepSeek 准备初步标注**（含 `source`、`label_confidence`、逐条理由）；
2. **Founder 校验关键 ground-truth 标签**（对照本地截图），确认或驳回；
3. **CTO 审核标注 schema、冲突项与覆盖率**；
4. 无法确认的标签保持 `unknown`，**不得编造**。

---

## 3. 数据集格式

JSONL，一行一条观察；字段与 `Observation` 同构，但**不含像素**，只留哈希与可选本地引用。

```json
{
  "schema_version": "1.1",
  "sample_id": "poc002-step-01",
  "source": "poc002-documented",
  "label_confidence": "documented",
  "annotated_by": "deepseek-preliminary",
  "validated_by": "pending-founder",
  "observation": {
    "observation_id": "obs-0001",
    "observation_seq": 1,
    "ui_mode": "world",
    "scene": { "name": "建邺城", "coords": { "x": 183, "y": 25 } },
    "quest": {
      "quest_id": "新手试炼",
      "objective_text": "熟悉的建邺城，人族使者似乎有事找自己",
      "target_name": "人族使者",
      "confidence": "observed"
    },
    "dialog": null,
    "panels": [],
    "anomalies": [],
    "observation_quality": "sufficient"
  },
  "expected": { "stage": "talk_to_envoy", "stage_source": "observed" }
}
```

**脱敏**：角色名 `测试角色`、服务器 `[测试服务器]`、无 UID、无本机路径、无原始像素。

---

## 4. 用例目录

### 4.1 来自 POC-002 转录（documented）

| ID | 场景 | 输入 | 期望 |
|---|---|---|---|
| **T1** | 阶段识别 | 三帧任务文案 ①②③ | 分别映射 `talk_to_envoy` / `set_pet_active` / `equip_gear`，`stage_source=observed` |
| **T2** | 选项选择 | `ui_mode=dialog`，options=[主线任务, 我要做其他事情, 我点错了] | 生成 `click_semantic(主线任务)`，`risk=reversible`，后置条件为"对话文本变化" |
| **T3** | 对话推进 | 连续 4 次对话推进，**每次 `dialog.text_hash` 变化**；中间夹 1 帧内容未变 | 每次 hash 变化 → `revision +1`；**内容未变的重复帧不 +1**；`stage` 全程不变；不得判为"卡住" |
| **T4** | 不可逆门禁 | 出现 4 个初始召唤兽选项 | `pending_decision` 置位；未批准时 **不得产出可执行 intent** |
| **T5** | 面板层级 | `panels=[宠物]` + 面板内「召唤兽」按钮 | 点击后期望 `panels` 含 `召唤兽属性`；仍只有「宠物」→ `rejected` |
| **T6** | `pet_active` 分级（修正） | 三种输入：仅按钮变「休息」／按钮+血条／都不可见 | 分别 → `inferred` / `observed` / `unknown`；**不允许多证据缺失时标 observed** |
| **T7** | 幂等（修正） | 同一 `intent_id` 提交两次；另一次**不同** `attempt_no` 的"继续"点击 | 第一次执行；同 id 第二次 `E_DUPLICATE_ATTEMPT`；不同 attempt 的点击**必须允许** |
| **T13** | 15 步回放 | POC-002 的 15 条观察 | 仅断言不变量：`stage` 只第 8、15 步迁移；`revision` 严格递增；**第 10 步 tooltip 学习（`interactables` 变化）计 1 次 revision**；`pet_active` 第 13 步前非 observed；最终 `stage=equip_gear`。**不断言 revision 的具体数值** |

### 4.2 合成异常场景（synthetic）

| ID | 场景 | 期望 |
|---|---|---|
| **T8** | 关键字段不可用 | 第 1 帧 `unusable`，第 2 帧仍 `unusable` | 第 1 帧 → `ambiguous`、**不产出动作**、重新观察（**不停止**）；连续第 2 帧 → 停止（S4） |
| **T9** | 未知弹窗 | 立即停止（S3），**不点击任何按钮** |
| **T10** | 无进展（修正） | 连续 3 次尝试均未产生**期望的可观测进展** → 停止（S2）；**不以 revision 是否变化为判据** |
| **T11** | 终帧无变化（修正，分两支） | (a) 区域清晰且后置条件明确为假 → `rejected`；(b) 区域被遮挡/低置信 → `ambiguous` |
| **T12** | 窗口身份不符 | 停止（S7） |
| **T14** | 陈旧 intent（两子例） | (a) 观察与执行之间窗口移动；(b) 期间 `TaskState.revision` 已前进 | 两者均返回 `E_STALE_INTENT`，重新观察，**不重试原 intent** |
| **T15** | 任务追踪缺失 | `stage` 保持不变、`stage_source=inferred`、写入 `open_questions`；**不得**把推断阶段标为 observed |

### 4.3 用例分布说明

- T1–T7、T13 来自**单一成功路径**的转录；
- T8–T12、T14–T15 为**人工构造**；
- 没有任何用例验证"跨任务泛化"或"像素识别精度"。

---

## 5. 指标（回归目标，非泛化证据）

| 指标 | 定义 | 目标 | 说明 |
|---|---|---|---|
| 契约不变量通过率 | T1–T15 中不变量断言通过比例 | **100%** | 硬门槛 |
| `false-confirm` | 无证据却判 `confirmed` 的比例 | **0** | 硬门槛 |
| 拒绝正确率 | 不可逆/未知弹窗/陈旧 intent 场景正确拒绝 | **100%** | 硬门槛 |
| 阶段分类准确率 | 标注样本上 `stage` 正确率 | ≥ 95% | **仅对该 fixture 集**，不代表泛化 |
| 恢复正确率 | T8–T12、T14–T15 处理与策略一致 | 100% | — |

> 上述 95% 是**该 fixture 集上的回归目标**，不是 OCR 准确率、也不是跨任务泛化指标。
> 由于样本量极小（1 个任务、15 步、若干合成样例），任何统计意义上的置信度都不成立。

---

## 6. 运行方式

- 纯函数：JSONL 进，判定与最终状态出；
- 不依赖窗口/位图/游戏进程，可在 CI 运行；
- 输出 JSON 报告，便于版本对比；
- 每次运行须记录 fixture 的 `source` 与 `label_confidence` 分布。

---

## 7. Q1–Q3 的决策与落地（采纳 CTO 结论）

| 问题 | 决策 | 落地要求 |
|---|---|---|
| **Q1**：能否用 POC-002 截图做解析器回归 | **有条件同意** | 仅接受**逐张审核过的、裁剪去标识**的私有 fixture；不得批量上传原始截图；不得含 UI/用户/账号信息；本地保留来源映射；**不得自动上传** |
| **Q2**：标注由谁负责 | DeepSeek 初标 → Founder 校验关键标签 → CTO 审 schema/冲突/覆盖 | 无法确认的标签保持 `unknown` |
| **Q3**：是否需要第二个任务 | **需要**，但**不阻塞 M1–M2** | 在 M3/M4 声称可迁移前必须补充 |

---

## 8. Q4–Q6：CTO 建议（记录，不阻塞本 PR）

| # | 问题 | CTO 建议 | 落地要求 |
|---|---|---|---|
| Q4 | 裁剪与脱敏标准 | 裁剪到**最小任务 UI 区域**；抹除角色 / 用户 / 服务器 / 账号 / 机器标识；私有上传前**逐张检查**并经 Founder 批准 | 审核记录与 fixture 一并保存，可追溯 |
| Q5 | 第二个任务 | 优先选**不同的交互模式**，而不是同一新手链里更多对话 | 在声称泛化前完成 |
| Q6 | 标注冲突 | Founder 依可用证据做最终裁定；CTO 审一致性，未解决项记为 Unknown | 冲突记录保留，不强行归一 |

> 以上为方向性建议，执行细节留待 M3/M4 的独立 Issue；本 PR 不因此扩大范围。
