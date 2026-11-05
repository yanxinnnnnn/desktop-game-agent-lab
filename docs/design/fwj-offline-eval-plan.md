# 梦幻西游任务状态 — 离线评估方案（Issue #7）

| 项目 | 内容 |
|---|---|
| 状态 | 设计稿（design-only），不含实现 |
| 关联 | [#7](https://github.com/yanxinnnnnn/desktop-game-agent-lab/issues/7)、[fwj-task-state-manager.md](fwj-task-state-manager.md) |
| 目标 | 在没有真实游戏、没有截图入库的前提下，用**可复现的转录数据**回归任务状态机与判定逻辑 |

---

## 1. 评估什么、不评估什么

### 1.1 能离线评估

- 任务阶段（stage）识别是否与标注一致；
- `ActionIntent` → 期望后置条件 → `VerificationResult` 的判定逻辑；
- 幂等、停止规则、不可逆操作门禁、恢复策略；
- 状态回放：给一串观察，最终状态是否等于标注终态。

### 1.2 不能离线评估（必须在真实环境做）

| 项 | 原因 |
|---|---|
| 像素识别准确率（任务追踪/对话框/面板） | 需要真实截图；仓库不存原始截图（`docs/evidence/README.md`） |
| 时序与动画（点击后多久稳定） | 需要连续帧 |
| UIPI / 输入通道 | 需要真实游戏进程（POC-001/003） |

> 因此本方案的结论只能支持"状态机与判定逻辑正确"，**不能**代替视觉识别验收。

---

## 2. 数据来源与证据等级

| 来源 | 内容 | 等级 |
|---|---|---|
| POC-002 报告 §2 的 15 步表 | 每步的"观察到的状态 / 指令 / 验证结果" | **documented observation**（转录级） |
| POC-002 §2.1 | 任务追踪文案的三次演变 | documented observation |
| 人工构造的异常场景（T8–T10、T12） | 为覆盖失败路径而编写 | **synthetic**（明确标注） |

**诚实声明**：POC-002 当时确实有截图，但截图未随仓库分发；仓库内只有文字记录。
因此本方案的所有样本都是**转录级**，不是像素级 ground truth。任何声称"识别精度已被离线验证"的结论都是越界的。

---

## 3. 数据集格式

建议 JSONL，一行一条观察，字段与 `Observation` 同构，但**去掉截图二进制**，只留 `screenshot.sha256`（可为占位）。

```json
{
  "schema_version": "1.0",
  "sample_id": "poc002-step-01",
  "source": "poc002-documented",
  "label_confidence": "documented",
  "observation": {
    "ui_mode": "world",
    "scene": { "name": "建邺城", "coords": { "x": 183, "y": 25 } },
    "quest": {
      "quest_id": "新手试炼",
      "title": "新手试炼",
      "objective_text": "熟悉的建邺城，人族使者似乎有事找自己",
      "target_name": "人族使者"
    },
    "dialog": null,
    "panels": [],
    "anomalies": []
  },
  "expected_state": {
    "stage": "talk_to_envoy",
    "ui_expectation": "world"
  }
}
```

**脱敏要求**

- 角色名统一 `测试角色`，服务器统一 `[测试服务器]`，UID 不出现；
- 不包含任何原始像素、窗口标题中的账号片段、本机路径；
- 样本入库前需 Founder 批准（与 `docs/evidence/README.md` 的流程一致）。

---

## 4. 用例目录

> 每条用例给出：输入 → 期望输出 → 判定要点。

### 4.1 来自 POC-002 转录（documented）

| ID | 场景 | 输入 | 期望 |
|---|---|---|---|
| **T1** | 阶段识别 | 三帧任务文案：①「…人族使者似乎有事找自己」②「设置召唤兽参战，再与人族使者对话」③「穿好武器与衣服，再与人族使者对话」 | 分别映射 `talk_to_envoy` / `set_pet_active` / `equip_gear`；`objective.evidence=observed` |
| **T2** | 对话选项选择 | `ui_mode=dialog`，options=[主线任务, 我要做其他事情, 我点错了] | 生成 `click_semantic(target=主线任务)`，`risk=reversible`，`expected_postcondition={dialog.text 变化}` |
| **T3** | 线性对话推进 | 连续 4 帧仅对话文本变化 | `revision` **不变**（不得因文本变化而迁移阶段） |
| **T4** | 不可逆门禁 | 出现 4 个初始召唤兽选项 | `pending_decision` 置位；**未获批准时不得产出可执行 intent**；批准后才允许 `click_semantic` |
| **T5** | 面板层级 | `panels=[宠物]`，面板内按钮「召唤兽」 | 点击后期望 `panels` 含 `召唤兽属性`；若仍只有 `宠物` → `rejected` |
| **T6** | 双证据确认 | 仅按钮「参战→休息」但无宠物血条 | `flags.pet_active` 最大只能为 `inferred`，**不得标 observed** |
| **T7** | 幂等 | 同一 `intent_id` 提交两次 | 第二次丢弃（postcondition 已成立），不得产生第二次点击 |

### 4.2 合成异常场景（synthetic）

| ID | 场景 | 输入 | 期望 |
|---|---|---|---|
| **T8** | 关键字段低置信 | `quest.objective_text` 为空且 `confidence=unknown` | `ambiguous` → 停止（S4），不生成 intent |
| **T9** | 未知弹窗 | `ui_mode=modal`，文案不在已知集合 | 立即停止（S3），**不得点击任何按钮** |
| **T10** | 无进展 | 连续 3 帧 `revision` 相同 | 停止（S2），报告卡住 |
| **T11** | 执行器回执不可信 | 人回复"完成"，但前后帧无差异 | `verdict=rejected`，`error_code=E_NO_CHANGE`，进入恢复 |
| **T12** | 窗口身份不符 | `observation.window.hwnd != task_state.window.hwnd` | 停止（S7），不发送输入 |

### 4.3 端到端回放

| ID | 场景 | 输入 | 期望 |
|---|---|---|---|
| **T13** | 15 步转录回放 | POC-002 的 15 条观察记录 | 终态：`stage=equip_gear`、`revision=10`、`flags.pet_active=true`、`open_questions` 含已知 Unknown |

> T13 是本方案的核心回归：它把"一次顺利路径"固化下来，任何状态机改动都必须仍能复现这 10 次迁移。

---

## 5. 判定与指标

### 5.1 指标

| 指标 | 定义 | 目标 |
|---|---|---|
| **State accuracy** | 标注样本上逐字段（stage / target_name / ui_mode / panels）正确率 | ≥ 95%（转录级） |
| **Transition verification rate** | 期望后置条件被正确判定（confirmed/rejected）的比例 | 100% |
| **False-confirm rate** | 无证据却判 `confirmed` 的比例 | **必须 0** |
| **Recovery correctness** | T8–T12 的处理是否与策略一致 | 100% |
| **Refusal correctness** | 不可逆/未知弹窗场景是否正确拒绝 | **100%** |
| **Human correction count** | 回放中需要人工改状态次数 | 0（离线） |

> `false-confirm = 0` 与 `refusal = 100%` 是**硬门槛**：宁可判 `ambiguous` 也不许误判成功。

### 5.2 运行方式

- 纯函数：输入 JSONL，输出判定结果与最终状态；
- 不依赖窗口、位图、游戏进程，可在 CI 中运行；
- 输出结构化报告（JSON），便于对比不同版本。

---

## 6. 与真实环境的衔接

| 阶段 | 数据 | 目的 |
|---|---|---|
| 离线（本方案） | 转录 + 合成 | 验证状态机与判定逻辑 |
| 半离线 | 获批的脱敏截图 + 人工标注 | 验证解析器（M3） |
| 在线（人工执行） | 真实画面 + 人执行 | 验证端到端闭环（M4） |

三层不可互相替代：离线全绿**不代表**识别可用；在线成功也**不能**反推状态机没有边界缺陷。

---

## 7. 入库前的合规要求

1. 样本不含账号、角色名、UID、服务器、本机路径、原始像素；
2. 每条样本标注 `source`（`poc002-documented` / `synthetic`）与 `label_confidence`；
3. 合成样本必须显式标记 `synthetic`，**不得**伪装成实测；
4. 新增样本需 Founder 批准，流程见 `docs/evidence/README.md`。

---

## 8. 未决问题

| # | 问题 | 影响 |
|---|---|---|
| Q1 | 是否允许把 POC-002 的**脱敏截图**作为解析器回归样本 | 决定 M3 能否离线验收 |
| Q2 | 转录样本的标注由谁复核 | 影响 T1/T13 的可信度 |
| Q3 | 是否需要第二个任务作为对照样本 | 单一任务样本存在过拟合风险 |
