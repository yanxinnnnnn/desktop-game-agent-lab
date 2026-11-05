# DSH 离线 NDJSON Adapter — M0.2c Phase 1（Issue #21）

本模块是纯离线 parser/normalizer，消费调用者已经持有的内存 bytes/str 和独立 runner
元数据，不启动进程、不读取日志/会话文件、不读取凭据、不发起模型或通知请求。
基于 Issue #20 CTO Review 的脱敏摘要和 Issue #21；完整私有报告未访问、未复制。
Python 3.11+，仅标准库。M0.2a 公共接口、schema v1、状态机、去重和 synthetic 通知门禁未修改。

## 证据与信任边界

| 内容 | 已有证据等级 |
|---|---|
| DSH launcher、version 输出 `0.2.2-rc.1`、help 输出 | DeepSeek 报告 locally verified，CTO 只审摘要，Codex 未运行 |
| 上述两个 probe 的退出码 | Unknown，未采集 |
| Headless/NDJSON/session/final/原生错误名/0 与 1 的退出约定 | bundled-document descriptions，经脱敏摘要转述；非运行观测 |
| 本模块通过的 parser/状态映射用例 | 全部 synthetic，模拟 runner 与错误码来源 |
| 原生 `error.code` 字段的实际 JSON 路径、Retry-After、Headless auth、Windows 取消时序 | Unknown |
| Codex CLI 真实版本、flags、schema、错误、resume | Unknown；本模块不实现 Codex adapter |

**Phase 1 所有输出固定 source=synthetic**。不能把 raw text/普通 dict 直接转成真实
adapter 来源，不能通过传入 `source="adapter"`、JSON 内 `verified=true` 或错误码文本放行。
带类型的 fixture runner 仅模拟未来可信 runner 的职责，不是生产 runner 身份认证。
本模块没有真实来源升级开关、真实运行入口、CLI 或通知入口；M0.2a 原始事件来源门禁仍适用。
未来真实 worker 必须独立审查并证明进程归属、stdout 来源、退出状态与错误码路径，不能
把本 fixture 类型改名为“已认证 runner”。M0.2a 本身的 adapter 字符串也不是身份认证。

## 保守 fixture profile（不是已观测的完整原生 schema）

当前公开脱敏摘要没有给出全部事件的完整字段 schema。因此这里采用明确受限的测试
profile，不声称可无修改地消费真实安装 DSH 的任意 stdout。`type` selector、文本字段
`text`、控制对象的允许键及 final 上显式 sessionId 是本离线 fixture profile 的选择；
额外原生字段/具体 wire schema 须后续核验，不通过猜测扩大接受范围。

- 每行一个 JSON object，UTF-8 严格解码，LF 或 CRLF，每行必须有终止换行。
  空流、空行、缺终止换行、单 CR、pretty-print 多行对象均拒绝。
- 第一条且只能一条 session：`{"type":"session","sessionId":"<opaque>"}`。
- 中间可为 text/thinking（text 必须为字符串）、tool_result 或 error。
  tool/error 的 payload 形状仍 Unknown，作为有界、不解释的 opaque JSON 处理；
  不是声称已校验原生工具协议。任一中间事件若携带 sessionId，必须与当前会话完全一致。
- 最后一条且只能一条 final：
  `{"type":"final","sessionId":"<same opaque>","reason":{"kind":"completed"}}`。
  session/final 仅接受这些键和可选 truncated；reason 只接受非空字符串 kind。
  completed 是已描述的完成标签；其他值不推断其原生语义，统一作为非完成。
- final 后任何事件、重复/冲突 final、重复 session、跨会话或未知事件类型均拒绝。
  当前只支持上述六类，其他原生类别待核验；不会把它们静默当作成功证据。
- `FixtureRunnerResult.truncated=True` 或顶层布尔 truncated=true 均保守拒绝；
  顶层 truncated 是本 profile 的拒绝策略，不代表已独立核验其原生位置/语义。
  JSON 内任意 `truncated=false` 不会覆盖 runner 的截断标记。

限制：每个解码后 JSON 字符串（包括 key）最多 **8192 UTF-8 字节**，单行最多
**32768 字节**（不含 CRLF/LF），总捕获最多 **1 MiB**、最多 **1024 条**、
嵌套深度最多 **16**、累计 key/value/container 节点最多 **16384**。字符串/行上限来自
所报告的文档描述，并非实际进程验证；其余总量边界为本实现的保守安全策略。
重复 JSON key、NaN/Infinity、非有限数字、无效 UTF-8/BOM、孤立 surrogate 都拒绝，
不修复文本后继续，也不输出错误片段。输入必须为已持有的 bytes/str，不接受文件路径、
生成器、stdin reader 或无限流；不做文件系统观察。

## 成功、进程状态与分类

成功同时需要：独立 fixture runner 报告**整数 exit_code=0**、没有 OS signal、完整合法
单会话流最后的 final.reason.kind=completed，且没有先前 error 冲突。
工具成功、正文声称完成、NDJSON 内 exit_code、进程退出 0 但缺 final 均不构成完成。
final completed 但 exit 非零/Unknown 或 OS signal 存在，输出 unknown_failure；
error 与 completed final 同时存在则拒绝，不能假设它是已恢复的 warning。

runner 的 exit_code、os_signal 和 truncated 都从 NDJSON 外传入；不从模型事件生成 OS
状态，也不把负退出码/信号猜成额度或超时。FixtureRunnerResult 无法从普通 JSON 自动创建。
输入元数据可信只是测试 harness 的约定，真实进程证明留给未来 worker。

原生结构化错误码路径 Unknown，因此默认**完全不读取** JSON 内 `code`、`error.code`、
HTTP status、Retry-After 或任意嵌套“证明”。具体映射仅在独立传入
`FixtureCodeEvidence(event_index, code, CodeOrigin.FIXTURE_HARNESS)` 时用于合成测试：

| fixture 证明中的精确 code | Supervisor signal / category |
|---|---|
| AUTH / MISSING_CREDENTIAL / INVALID_CREDENTIAL | auth_failed |
| RATE_LIMIT | provider_rate_limit（临时） |
| QUOTA / ACCOUNT_QUOTA | quota_exhausted（硬阻塞） |
| TIMEOUT | timeout |
| CONTEXT_WINDOW_EXCEEDED / NO_ADAPTER / EMPTY_RESPONSE / SERVER / TRANSPORT | sdk_fault / unknown_failure |
| 缺失、未知、非精确大小写或相互冲突的 code | sdk_fault / unknown_failure |

证明必须是独立的带类型对象，origin 必须是 Enum 值，event_index 从 **0** 起计且必须
指向实际 error 事件；同一索引不能重复。不接受从模型 text/thinking 中指向“QUOTA”的证明。
若有多个 error，必须全部得到证明且映射一致，否则 unknown_failure；未知 code 不升级。
这是 fixture harness 的显式来源断言，不是已验证的原生 JSON 字段路径。

通用 HTTP 429 不能推出 QUOTA；此 parser 对任意未核验的 JSON 429 位置连 transient
分类也不推断，保持 unknown_failure。M0.2a 原有可信中立 HTTP 429 规则未改变。
Retry-After 原生存在性/位置尚不知，输出 retry_after_seconds=None，不编造延迟值。

## 接口与 Supervisor 交接

```python
from tools.supervisor.adapters import (
    DshOfflineAdapter, OfflineCapture, TaskContext, FixtureRunnerResult,
)

context = TaskContext(
    task_id='00000000-0000-4000-8000-000000000021',
    event_id='00000000-0000-4000-8000-000000000022',
    issue_url='https://github.com/yanxinnnnnn/desktop-game-agent-lab/issues/21',
    attempt_no=0,
)
# 仅合成示例：不调用 dsh，不读取日志或凭据。
stream = (
    b'{"type":"session","sessionId":"synthetic-session"}\n'
    b'{"type":"final","sessionId":"synthetic-session","reason":{"kind":"completed"}}\n'
)
capture = OfflineCapture(stream, context, FixtureRunnerResult(exit_code=0))
result = DshOfflineAdapter().parse(capture)
```

所有 parse 返回 `{Ok,Code,Message,Data}`。**Ok 只表示已解析且可交接的中立事件，
不是任务已经成功**；有效的错误结果也返回 Ok=true，其 Data.category 仍为失败类别。
任务成功仍由退出码与 final 的联合条件决定。无效/歧义捕获返回固定 E_DSH_*、Data=None，
没有部分成功记录。拒绝码包括 CONTEXT/LIMIT/FRAMING/JSON/SCHEMA/ORDER/SESSION/
FINAL_MISSING/CONFLICT/TRUNCATED/EVIDENCE/INTERNAL（均以 E_DSH_ 开头）。
异常字符串、原始行号内容、文本、thinking、tool payload、native sessionId、provider error
不出现在 Result 中。数据类禁用字段 repr，解析器无跨调用原始数据缓存；调用者仍不得
自行打印 capture/raw buffer，原始输入的保管责任不因 repr 抑制而消失。

成功解析输出符合现有 `normalize_event`：agent=deepseek、kind=finished、
schema_version=1.0，UUID4 task_id/event_id、规范 Issue URL、attempt_no 来自 TaskContext。
http_status=None、retry_after_seconds=None、source=synthetic、**resume_token 始终 None**。
即使 DSH sessionId 恰好看似 UUID4，也不复制、不哈希、不持久化为 resume_token，
真实恢复映射须另行审核。sessionId 只在解析期间用于精确相等关联；可选
runner.expected_session_id 仅额外核对关联，永不输出。

可将 capture 传给已登记且处于 RUNNING 的 Supervisor：
`supervisor.ingest(capture, adapter=DshOfflineAdapter())`。不会自动通知；若解析拒绝，
Supervisor 保持既有 E_EVENT_INVALID 行为，不保存原始捕获，也不偷偷更新任务状态。
有效失败可在本地准备 source=synthetic 的待处理项；它仍被既有 B1 通知门禁拒发。
**不要为“试运行”改写 source，也不要调用真实 notify。**

## 离线验证

从仓库根目录，Python 3.11+，无需安装依赖：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -S -m unittest discover -s tests/supervisor -p test_dsh_adapter.py -v
PYTHONDONTWRITEBYTECODE=1 python3 -S -m unittest discover -s tests/supervisor -v
PYTHONDONTWRITEBYTECODE=1 python3 -S -m unittest discover -s tests/notifications -v
git diff --check
```

新测试全部为内联合成数据、模拟独立 runner/typed code 证明和临时数据库；禁用 socket、
subprocess、Supervisor.notify 和 NotificationSender.send。新 DSH 测试不调用任何 notify。
既有 M0.2a/M0.1 全量回归继续使用 Mock sender/HTTP 和假 key，无真实发送。
覆盖完成条件、顺序/会话冲突、截断、JSONL/Unicode、资源边界、closed code 映射、
prose 注入、隐私、native session 不能恢复，以及 Supervisor 状态/门禁兼容性。
缺解释器需报告 SKIP，零测试不满足验收。没有 Windows/Headless 实机运行或视觉验证。

## 仍为 Unknown 与回滚

真实 Headless 事件完整字段、上述 fixture profile 与安装版本的 wire 兼容性、code 路径
及来源认证、Retry-After、实际退出/Windows cancellation 时序、Headless auth、
工作目录/preset/subagent 下的 native session 生命周期和安全恢复映射仍未验证。
Codex CLI 版本、flags、错误/schema/resume 均为 Unknown；不从二进制字符串猜测。

未来 Phase 2 的最小真实 smoke test 必须另有 Issue、Founder 对调用/最小 token 成本/
凭据方案/停止条件的明确授权，不能据本 PR 或 typed fixture 类自行开始。
Windows UI observer、自动 worker/GitHub 轮询/CTO Work、游戏操作均不在本轮。

回滚通过独立 revert PR 移除本适配器、其测试与本文档；不迁移 schema、不删除检查点，
M0.2a 状态/去重/通知门禁及 M0.1 均保持原样。Founder 保留唯一手动 Merge 权限。
