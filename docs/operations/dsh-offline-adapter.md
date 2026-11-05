# DSH 离线 NDJSON Adapter — M0.2c Phase 1（Issue #21）

本模块是纯离线 parser/normalizer，消费调用者已经持有的内存 bytes/str 和独立 runner
元数据，不启动进程、不读取日志/会话文件、不读取凭据、不发起模型或通知请求。
基于 Issue #20 CTO Review、Issue #21 最新证据补充及 PR #23 CTO Review 的脱敏摘要；
完整私有报告、原始 stdout、真实会话与路径未访问、未复制。
Python 3.11+，仅标准库。M0.2a 公共接口、schema v1、状态机、去重和 synthetic 通知门禁未修改。

## 证据与信任边界

| 证据等级 | 内容 |
|---|---|
| Observed（Founder/DeepSeek 报告，CTO 审脱敏摘要） | DSH 0.2.2-rc.1 单次模型调用前认证失败，exit=1；session→status*→final；final.reason.kind=error；final.reason.error.code=MISSING_CREDENTIAL |
| Observed（同一次失败） | session/final.sessionId 为字符串，session.cwd 为私人字符串，final.text 为空字符串，reason.error.message 为私人字符串；没有独立 error 事件 |
| Synthetic | 本仓库从必要字段信息重新构造的 fixtures、runner 元数据和全部 parser/分类测试；并非原始探测输出 |
| Documented-only | exit0+completed 成功条件、独立 error 事件的 fixture profile，以及其它暂未实测 code 的 provisional 映射 |
| Unknown | 其余原生 schema/status 阶段键、Retry-After、除已见失败外的 Headless auth、取消/退出时序与 session 恢复 |
| 已报告的 Codex 只读补充 | Issue #21 CTO 补充称 codex-cli 0.166.0-alpha.6 及顶层 exec 存在；exec --json 的 schema/行为、原生错误与 resume 仍 Unknown；本模块不实现 Codex adapter |

上述 Observed 是单次失败路径的归因证据，Codex 没有亲自运行或独立重放真实输出。
PR #23 的后续成功探测在工具隔离 preflight 停止，没有第二次模型调用；成功完成仍未观测。

**Phase 1 所有输出固定 source=synthetic**。不能把 raw text/普通 dict 直接转成真实
adapter 来源，不能通过传入 `source="adapter"`、JSON 内 `verified=true` 或错误码文本放行。
带类型的 fixture runner 仅模拟未来可信 runner 的职责，不是生产 runner 身份认证。
本模块没有真实来源升级开关、真实运行入口、CLI 或通知入口；M0.2a 原始事件来源门禁仍适用。
未来真实 worker 必须独立审查并证明进程归属、stdout 来源、退出状态与错误码路径，不能
把本 fixture 类型改名为“已认证 runner”。M0.2a 本身的 adapter 字符串也不是身份认证。

## 保守 fixture profile（不是已观测的完整原生 schema）

已知的失败路径字段在本轮明确支持；其余原生协议仍只接受保守 fixture profile，
不声称完整兼容真实 DSH stdout。text/thinking 的 text 字段及未核验工具 payload 仍为
测试 profile 选择；不通过猜测补齐原生字段或自动检测实际安装版本。
- 每行一个 JSON object，UTF-8 严格解码，LF 或 CRLF，每行必须有终止换行。
  空流、空行、缺终止换行、单 CR、pretty-print 多行对象均拒绝。
- 第一条且只能一条 session：`{"type":"session","sessionId":"<opaque>"}`。
  可选 cwd 必须为字符串；完成资源限制检查后明确丢弃，既不解析路径也不输出。
- 中间可为 status、text/thinking（text 必须为字符串）、tool_result 或 error。
  status 只作为有界、不解释的进度数据。已报告阶段值为 turn_start/step_start/step_end/turn_end，
  但具体键路径未公开确认，故不要求、不搜索 phase/stage 等猜测字段，也不据阶段判成功或分类。
  测试只重建四条最小 `{"type":"status"}`，不声称已经核验阶段字段的完整结构。
  tool/error 的 payload 形状仍 Unknown，作为有界、不解释的 opaque JSON 处理；
  不是声称已校验原生工具协议。任一中间事件若携带 sessionId，必须与当前会话完全一致。
- 最后一条且只能一条 final：
  `{"type":"final","sessionId":"<same opaque>","reason":{"kind":"completed"}}`。
  session/final 除这些键及可选 truncated 外，仅增加已知的 session.cwd / final.text 字符串字段；
  final.text 经资源检查后丢弃，不能建立完成或失败分类。
  reason.kind=error 时必须有 error 对象，包含非空字符串 code、可选字符串 message；
  message 明确丢弃，其余未声明字段拒绝。缺少/非字符串/空 code 返回 E_DSH_SCHEMA。
  其它 reason 仍只接受非空字符串 kind；completed 的成功路径尚未实测。
- final 后任何事件、重复/冲突 final、重复 session、跨会话或未知事件类型均拒绝。
  当前只支持上述七类，其他原生类别待核验；不会把它们静默当作成功证据。
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

**已观测终止路径**：仅在合法 final.reason.kind=error 下读取精确的
`final.reason.error.code`。仅 **MISSING_CREDENTIAL + 独立 runner.exit_code=1 +
runner.os_signal=None** 的组合映射 auth_failed。退出码为 0、Unknown/None、其它非零码
或存在任意 OS signal 时均为 unknown_failure，不凭终止码扩展尚未观测的 runner 结果。
缺失、非字符串或空 code 拒绝；其它字符串一律 unknown_failure，包括 QUOTA/RATE_LIMIT/AUTH/TIMEOUT 等尚未
在此路径观测的码，不因名称出现在 provisional 表中就扩大映射。未知终止码不能被额外
fixture 证明提升为额度/限流；已知终止认证错误与独立错误证明冲突时拒绝。

终止 kind=error 永远不是成功，即使外部 exit_code=0、status 或工具声称完成。
只观测过 exit=1 且无 OS signal 的该认证失败；其它退出/信号组合只是防误判的 synthetic
测试，不是新增运行证据。exit=0 + completed 的成功路径仍为 documented-only，未实测。
message、final.text、thinking、tool/status payload、任意 code/error.code 或其它 JSON
字段均不能提供分类依据；除了上述精确路径，默认不读取 JSON 错误码或 HTTP/Retry-After。

**Provisional fixture 映射**保持原样，仅用于独立传入
`FixtureCodeEvidence(event_index, code, CodeOrigin.FIXTURE_HARNESS)` 的合成测试。
该证明仍必须绑定独立 type=error 事件，不能拿它把未知原生终止码标成已验证。
独立 error 事件的原生路径尚未观测，以下表格不代表这些失败路径已经实测：

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
这是 fixture harness 的显式来源断言，不是除上述终止认证路径以外的原生字段验证。

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
新增回归覆盖已观测形状的认证失败、status 不建立完成、终止缺失/非法/未知 code、
三类敏感字段在结果/repr/日志/SQLite 中无 Canary 泄漏；保留完成条件、顺序/会话冲突、
截断、JSONL/Unicode、资源边界、provisional closed code 映射、
prose 注入、隐私、native session 不能恢复，以及 Supervisor 状态/门禁兼容性。
缺解释器需报告 SKIP，零测试不满足验收。没有 Windows/Headless 实机运行或视觉验证。

## 仍为 Unknown 与回滚

成功完成、完整原生 schema、status 阶段值所在的具体字段、其他原生 code 路径/含义、
真实来源认证、Retry-After、Windows 取消与退出时序、除单次缺凭据之外的 Headless auth、
工作目录/preset/subagent 下的 native session 生命周期及安全恢复映射仍为 Unknown。
只读版本/顶层 exec 的已报告补充不证明 Codex exec --json、原生错误或 resume 可用；
本轮不实现、不探测这些能力。单次前置失败证据不外推到成功、所有认证场景或其它版本。

后续真实探测还须先确认可信、强制的工具隔离；read-only 或 approval-policy=never 本身
不能证明工具禁用，不能靠提示词替代隔离。当前修复没有新的真实调用授权。
未来 Phase 2 的最小真实 smoke test 必须另有 Issue、Founder 对调用/最小 token 成本/
凭据方案/停止条件的明确授权，不能据本 PR 或 typed fixture 类自行开始。
Windows UI observer、自动 worker/GitHub 轮询/CTO Work、游戏操作均不在本轮。

回滚通过独立 revert PR 移除本适配器、其测试与本文档；不迁移 schema、不删除检查点，
M0.2a 状态/去重/通知门禁及 M0.1 均保持原样。Founder 保留唯一手动 Merge 权限。
