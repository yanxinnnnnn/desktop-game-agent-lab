# FWJ M1 离线契约

依据已合并 PR #8 的两份 v1.1 设计（`docs/design/`）及 Issue #11。
这是候选 JSON 数据的结构/一致性校验，不是状态机、像素解析器、Planner、
实时 Guard 或执行器；不导入 PowerShell，不访问窗口、截图、游戏或网络。

## 安装与测试

Python 3.11+，从仓库根目录执行；不要求安装本项目为 Python 包：

```sh
python3 -m venv .venv-fwj
.venv-fwj/bin/python -m pip install -r requirements-fwj.txt
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=core .venv-fwj/bin/python -m unittest discover -s tests/fwj_contracts -v
.venv-fwj/bin/python -m pip check
```

虚拟环境属于本地产物，不要提交。Windows 可使用 `py -3.11 -m venv .venv-fwj`，
用 `.venv-fwj/Scripts/python.exe`，设置 `$env:PYTHONPATH='core'` 后运行同一 unittest 命令。
缺少解释器时运行方须报告 SKIP，不得声称通过；缺少 `jsonschema`/`referencing` 时
测试模块报告 SKIP，并要求先安装依赖。零用例/全 SKIP 不满足验收。
`requirements-fwj.txt` 固定唯一直接依赖 jsonschema 及其全部传递依赖；
没有其他运行时库。时间格式额外用 stdlib 检查，不依赖未声明的 format extras。
安装可联网，测试完全离线。

完整合成示例：`tests/fwj_contracts/examples.json` 的四个对象。
外层 `source=synthetic`、`label_confidence=synthetic` 是样本来源元数据，
不是观测证据等级。字段内的 `observed/inferred/unknown` 只是合成证据标签；
不代表实际视觉识别、成功操作或 POC-002 的转录。
未来 documented 样本必须保留其外层来源信息，不得改标 synthetic 或当作已核验截图。

## Schema 与边界

| 对象 | 文件（相对仓库根目录） |
|---|---|
| Observation | `schemas/fwj/v1.1/observation.schema.json` |
| TaskState | `schemas/fwj/v1.1/task-state.schema.json` |
| ActionIntent | `schemas/fwj/v1.1/action-intent.schema.json` |
| VerificationResult | `schemas/fwj/v1.1/verification-result.schema.json` |

共享定义在同目录 `common.schema.json`，避免窗口/证据/时间类型漂移。
使用 Draft 2020-12，`$id` 为 `urn:fwj:v1.1:<文件类型>`；引用由本地 Registry
注册，绝不通过网络获取。外部消费者也须注册五个 schema。
所有对象拒绝未知属性（明确允许的 flags、检查值、后置条件参数、回执除外）。
`schema_version` 固定为 `1.1`；阶段是开放的非空字符串，不枚举某个任务的阶段。

设计表没有完全规定 JSON 表示的地方，本实现约定如下，供 CTO 审阅：

- 观察/验证 ID 是大写规范 ULID，首字符 0–7；哈希是小写 64 位 SHA-256。
  窗口 `hwnd`/`pid` 用正整数，`title_hash` 用 SHA-256；不存原始窗口标题。
- 时间采用带时区 RFC3339，最多六位小数，不接收闰秒；以真实时刻比较时区偏移。
  `expires_at = created_at + ttl_ms`，TTL 正整数毫秒，边界 `now == expires_at` 合法。
  建议 3000–5000ms 不是强制范围。序号及版本允许从 0 开始，拒绝负值。
- 场景字段缺失用 null，confidence=unknown。`quest` 对象始终存在，
  **quest.confidence 只描述本帧 objective_text 的证据**；quest_id、title、target_name
  各自独立可为 null，不会使可读的目标文案降级，也不能用它们替代目标文案作为阶段证据。
  objective_text=null 强制 confidence=unknown，不使用空字符串冒充读到了任务。
  `dialog=null` 表示没有可表示的对话。
  `screenshot.local_ref` 可选且可为 null；示例不存本机路径，也不得把实际本地引用提交。
- 选项是唯一字符串列表；region/window.rect 是物理像素 `{x,y,w,h}`，
  坐标可负，宽高必须正。interactables 的 semantic_id、panels 的 name 不可重复。
- flag 为 `{value,evidence,observations}`；未知值用 null/unknown，
  observed 必须附至少一个观察 ULID。引用的全球唯一性和真实性需由未来存储层核验。
- precondition 表示为 `{path,expected}`，path 使用逻辑状态 JSON Pointer。
  `validate_attempt` 要求唯一 `/stage` 断言；其它条件只接受调用方重新解析得到的
  显式 `preconditions_hold` 布尔值。此函数本身不解析 UI 或后置条件。
  后置条件 `{type,parameters?}` 的 type 保持开放，为后续 Verifier 保留语义。
- key 动作要求非空 key，其余动作 key=null；所有动作保留语义 target、origin 和点坐标。
  本模块不将点坐标重绑定到 guard 帧，也不返回可执行 intent。
- 确定 verdict 必须有两帧不同 ID、sufficient 质量和非空 checks；confirmed 所有
  pass=true 且 error_code=null；rejected 至少一个 pass=false；ambiguous 可缺帧。
  只有 confirmed 可携带非空 state_patch，补丁仅允许 TaskState 的语义/证据字段。
  校验只检查声明的结构自洽性，不独立证明检查断言为真，不应用补丁。

## 纯接口

```python
from fwj_contracts import validate_observation
result = validate_observation(observation)  # JSON 解析后的 dict
# {"Ok": False, "Code": "E_SCHEMA", "Message": "...",
#  "Data": {"errors": [{"path": "/observation_seq", "reason": "...", "code": "E_SCHEMA"}]}}
```

所有公开接口返回 `{Ok, Code, Message, Data}`，不修改输入；成功通常 Data=null，
`make_intent_id` 成功时 Data 为哈希字符串。错误带 RFC6901 JSON Pointer 和原因。
无效 JSON 值/结构是 E_SCHEMA，跨字段不变量是 E_CONTRACT。
这些结果不构成真实执行授权。损坏/缺失的部署 schema 属于安装错误，需修复部署。

- `validate_observation(value)`、`validate_task_state(value)`：单对象结构与证据约束。
- `validate_action_intent(value, session_id=None)`：结构、时间关系；提供会话时校验 intent_id。
- `validate_verification_result(value)`：两帧、质量、checks/patch 一致性；完全不读取回执来判成功。
- `validate_observation_pair(before, after)`：相邻采集恰好 +1，新 ULID，时间不倒退。
- `validate_revision(before_state, after_state, before_observation, after_observation)`：
  对调用方已采纳的候选状态进行版本检查，不生成下一状态。每帧推进 observation_seq；
  stage/stage_source/objective.text/objective.evidence/ui_mode/面板集合/dialog.text_hash/
  选项集合/interactable 的语义 ID 或 label/flag 值或证据/pending_decision/open_questions
  发生语义变化才 revision+1，一帧多字段变化也只 +1。
  这按 PR #13 CTO Review B2 补足原设计 §6.1 的证据及操作资格语义，不新增状态回放。
  pending_decision 的新增、删除及任意内容变化均计入（包括问题、选项顺序、risk）；
  对象键顺序不计入。open_questions 当前没有操作资格分类，故保守地将所有问题的
  新增、删除、文本变化视为操作资格变化，按集合比较，单纯重排不计入。
  证据降级、出现人工决策或未决问题变化都会使旧 state_revision 的 intent 返回
  E_STALE_INTENT；同 stage 可以变化 revision；重复截图且全部语义相同则不增加。
  位置、截图哈希、观察引用，以及上述按集合比较字段的顺序变化不增加语义版本。
  任务追踪 unknown 时 stage 保持，来源降为 inferred（原 stage=unknown 则保持 unknown），
  open_questions 保留不确定项；不会把推断升级为 observed。
  observed 阶段和阶段迁移必须引用本帧 confidence=observed 的 objective_text，
  TaskState.objective.text 须与该文案一致且 objective.evidence=observed；迁移后的
  stage_source 必须为 observed。目标文案到阶段名称的语义映射仍由未来解析器提供，
  M1 不解析任务、不硬编码阶段枚举；元数据缺失不会阻止合法的 observed 迁移。
- `make_intent_id(session_id, attempt_no, kind, semantic_id, observation_id)`：
  SHA-256(UTF-8 紧凑 JSON 数组)，`ensure_ascii=False`，字段顺序如参数顺序。
  不使用拼接分隔符，避免歧义。attempt_no 独立于 revision，重复目标允许新尝试。
- `validate_attempt(intent,state,origin,guard,history=...,now=...,guard_stage=...,
  preconditions_hold=...,approval=None)`：纯数据关联校验。
  history 必须是同一会话此前全部已提交 intent，包含被丢弃的尝试；调用方管理存储及并发。
  相同 ID 为 E_DUPLICATE_ATTEMPT；新 attempt_no 必须为历史最大值 +1，origin 必须更新。
  初次无历史时允许调用者选择起始非负 attempt_no。
  now、guard_stage 和重解析条件由调用者提供，不能从此接口推断已经完成安全实时 Guard。
  approval 为 `{intent_id,decision:"approved",approved_by,approved_at,expires_at}`；
  必须绑定本次尝试、批准人非空、批准不早于提议，且批准时间 <= now <= 批准有效期。
  M1 不认证批准人、验证签名或颁发批准；信任边界留给后续人工审批系统。

分类顺序：结构/关联错误 → 重复 attempt → 非递增 attempt → 陈旧状态 → 人工审批。
陈旧包括 TTL、revision、origin、guard 新鲜度、窗口身份/精确几何、阶段、前置条件，
返回 `E_STALE_INTENT`；缺少/过期/错误绑定的批准返回 `E_APPROVAL_REQUIRED`。
这是 Issue #11 对设计 §6.3 笼统错误码的明确细分。
失效 intent 不应重试，需新观察和新的 attempt；本模块不执行/记录这种流程。
M2 回放、停止计数、Planner、视觉与实机输入均未实现，也未在测试中声称验证。
