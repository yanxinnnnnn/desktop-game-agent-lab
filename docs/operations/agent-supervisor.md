# M0.2a 离线 Agent Supervisor（Issue #18）

依赖已合并 M0.1 PR #17，直接消费 `tools.notifications.build_event` 和 `NotificationSender`，
不修改其协议。Python 3.11+，仅标准库（含 SQLite）。这是结构化结果的本地状态账本，
**不**启动、暂停或恢复真实 Codex/DeepSeek 进程，也不查询账户额度。

## 本轮与后续阶段

M0.2a 实现检查点、失败分类、受限的重试资格、跨重启告警去重及显式通知入口。
M0.2b 才由 DeepSeek 验证 Windows 只读 CLI 能力并提供具体适配器；M0.3 才讨论
Issue→worker→PR→CTO 的自动循环。目前没有 Issue 轮询、定时器、守护进程、
模型请求、原始日志抓取、充值、账号切换、游戏/浏览器操作、自动 Merge 或 Work 触发。
PC 关机时不运行，不能保证“过夜自动继续”。

## 本地目录与 CLI

从仓库根目录执行。检查点目录必须由本机操作者选择为 **Git 仓库以外的本地目录**。
不要用 Git 子目录、网络共享、UNC、映射网络盘、云同步目录或其他人可写目录。
代码解析实际路径、检测 Git 根/linked worktree/裸库，拒绝数据库 symlink/hardlink
及显式 UNC；无法通用识别所有映射网络盘，选择真实本地磁盘仍由操作者负责。
数据库固定为该目录内 `supervisor.sqlite3`，目录路径从不写进记录或输出。

POSIX 在本机将 `SUPERVISOR_STATE_DIR` 设置为上述目录后（不要提交其值）：

```sh
python3 -m tools.supervisor --base-dir "$SUPERVISOR_STATE_DIR" simulate --case rate-limit
python3 -m tools.supervisor --base-dir "$SUPERVISOR_STATE_DIR" status
python3 -m tools.supervisor --base-dir "$SUPERVISOR_STATE_DIR" check
```

Windows 可在 PowerShell 中使用系统的本地用户数据目录表达式，不输出展开后的路径：

```powershell
py -3.11 -m tools.supervisor --base-dir "$env:LOCALAPPDATA\DesktopGameAgentLab\Supervisor" simulate --case quota
py -3.11 -m tools.supervisor --base-dir "$env:LOCALAPPDATA\DesktopGameAgentLab\Supervisor" status
```

`simulate` 新建一个明确标记 synthetic 的任务和事件，可选 quota/rate-limit/auth/timeout/
unknown/success/founder-merge/founder-game；只记账、准备告警，不发送、不运行客户端。
`status` 显示任务和 `resume_ready`；`check` 在同一事务内读取任务、脱敏事件和投递账本，
不会自动消耗到期的重试或发送通知。首次使用空目录会初始化数据库，不扫描其他本地文件。

`pause --task-id <UUID4>` 仅将活动任务的账本置为 WAITING_FOUNDER；不会停止真实进程。
`resume --task-id <UUID4>` 仅在已到期的 RESUMABLE 状态返回 PENDING，并增加 attempt_no。
硬阻塞或人工暂停须显式 `resume ... --acknowledge`，表示本地操作者确认原因已处理，
不是程序已经独立验证额度/凭据恢复；新的人工尝试会重新开启重试窗口。
恢复前必须独立确认真实旧 worker 已停止/交接，不能把记账恢复当作进程控制。
成功任务不重新打开，新任务需新 UUID；迟到的旧 attempt_no 不能覆盖新尝试。

CLI 没有任意 shell、原始日志文件、prompt、密钥参数、动态插件路径或任意 URL 入口。
参数错误不会回显私有输入/目录。所有正常 API 使用 `{Ok, Code, Message, Data}`。

## 已有事件入口与可信适配器

`Supervisor.register(task_id, issue_url, agent, resume_token=None)` 注册任务；task_id、
event_id 均为规范小写 UUID4，agent 仅 codex/deepseek，链接仅本仓库规范 Issue URL。
`Supervisor.ingest(record, adapter=None)` 默认使用 `StructuredAdapter`；可注入实现
`EventAdapter.parse(record) -> Result` 的受审查本地适配器。其输出仍重做白名单校验，
不信任插件返回的分类、自由文本或错误消息；插件代码本身必须可信，不从 PR 下载执行。
当前没有 Codex 或 DeepSeek 原生适配器。

完整中立事件示例（**synthetic**，不表示真实厂商错误码）：

```json
{
  "schema_version": "1.0",
  "task_id": "00000000-0000-4000-8000-000000000001",
  "event_id": "00000000-0000-4000-8000-000000000002",
  "attempt_no": 0,
  "agent": "codex",
  "issue_url": "https://github.com/yanxinnnnnn/desktop-game-agent-lab/issues/18",
  "source": "synthetic",
  "kind": "finished",
  "exit_code": 1,
  "http_status": 429,
  "signal": "none",
  "retry_after_seconds": 60,
  "resume_token": null
}
```

所有字段必须存在；可选值用 null。source 为 synthetic/adapter，仅表示数据入口，
**adapter 不自动等于 observed 或已核验**。kind 为 started/finished/founder_merge_required/
founder_game_action_required。非 finished 的退出/HTTP/Retry-After 必须 null，signal=none。
category 由校验器计算；若传入该字段，必须与重新计算结果完全一致。
原始日志、provider message、stdout/stderr、用户名、路径、prompt、headers 和额外字段一律拒绝，
不写入数据库，也不在失败结果中回显。未知原文不能靠字符串搜索猜测额度。

| 确定信号 | category | 状态/策略 |
|---|---|---|
| 中立 signal=quota_exhausted | quota_exhausted | BLOCKED_QUOTA，无自动恢复 |
| HTTP 401/403 或 signal=auth_failed | auth_failed | BLOCKED_AUTH，无自动恢复 |
| 普通 HTTP 429 或 signal=provider_rate_limit | provider_rate_limit | 有界退避；**不是**硬额度耗尽 |
| signal=timeout | timeout | 有界退避，不声称远端操作未发生 |
| 非零/缺失退出状态、其他 HTTP 错误、signal=sdk_fault | unknown_failure | BLOCKED_EXECUTION，不发明厂商语义 |
| 明确 exit_code=0 且无错误信号、HTTP 缺失或为 2xx | null | SUCCEEDED |

显式错误信号优先于 exit_code=0；401/403 与非 auth 的显式信号冲突时拒绝输入。
`quota_exhausted` 是未来适配器在核对实际安装版本文档后才能给出的中立分类，
不是声称某个 Codex/DeepSeek 错误码已经验证。Retry-After 只接受 rate-limit 的
0–86400 整数秒；未来适配器应将有文档依据的 Retry-After 秒数/日期安全换算成秒，
不能把原始 headers 存进事件。适配器自身抛异常为 E_ADAPTER，不等同于已证明任务失败；
已知且可关联的 SDK 故障应提供 sdk_fault 事件。

## 状态、重试与恢复

```text
PENDING → RUNNING → SUCCEEDED
             ├→ BLOCKED_QUOTA / BLOCKED_AUTH / BLOCKED_EXECUTION
             └→ RESUMABLE（等待 retry_at）→ 显式 resume → PENDING（attempt_no+1）
PENDING / RUNNING → founder-required → WAITING_FOUNDER
活动状态 → 显式 pause → WAITING_FOUNDER
硬阻塞 / WAITING_FOUNDER → 人工 acknowledge 后 resume → PENDING
```

首次 attempt_no=0。每次恢复产生新的尝试号和 operator 事件；单纯重复日志不增加次数。
临时 rate-limit/timeout 最多安排 **3 次重试资格**，默认退避 30/60/120 秒；rate-limit
同时取 `max(默认退避, retry_after_seconds)`，不提前违背服务端延迟。
总窗口为登记任务或硬阻塞人工恢复时起 **3600 秒**，包含活动任务耗时。
超过预算或建议延迟将超窗时，BLOCKED_EXECUTION 并准备人工告警，category 仍保留
provider_rate_limit/timeout，不把它改成 quota_exhausted。
RESUMABLE 已超 deadline 时，check 只显示不可恢复；显式 resume 将其原子转为硬阻塞并准备告警，
此后须人工 acknowledge。所有时间是可信本机时钟的 Unix 秒，不调用 sleep，不做忙等循环。
壁钟跳变及进程停止会影响可用性，不能据此声称真实 Agent 已按时重启。

## SQLite 与通知的一致性

使用 stdlib SQLite，user_version=1，BEGIN IMMEDIATE、外键、quick_check 和约束。
任务、事件、待投递项的写入同事务提交；数据库锁定返回 E_DB_BUSY，损坏、缺表或未知版本
返回 E_DB_INVALID；不静默删除、补表、重建或重置去重记录。I/O 错误和目录错误为
E_STORAGE_IO/E_STORAGE_LOCATION；诊断不包含实际路径或底层异常。

只存任务 UUID/Issue URL/agent/state/error_category、当前 event UUID、尝试号、
重试计数/时刻/deadline、可选 opaque resume_token，以及最少审计/投递状态。
resume_token 仅允许本地产生的 **UUID4 引用**，不是 API key、原生 session token 或
已经验证可用的 CLI resume 参数；真实客户端恢复行为仍为 Unknown。
事件表记录合成/适配器/operator 来源、固定类别和数字退出状态，另存规范输入哈希，
不保存原始 JSON/日志、provider text、提示词、截图、用户名或路径。

事件 UUID 全局唯一，同 ID 同内容为 E_DUPLICATE_EVENT，不同内容为 E_EVENT_CONFLICT。
同时强制 task_id/agent/Issue URL/attempt_no 相关性。每个
`(task_id, attempt_no, alert_type)` 只准备一个投递项，新日志 UUID 不能绕过去重。
硬额度/auth/不可恢复失败分别消费 M0.1 的 BLOCKED_* 模板；人工请求使用
FOUNDER_MERGE_REQUIRED/FOUNDER_GAME_ACTION_REQUIRED。人工恢复会取消旧尝试尚未发送的告警。

`notify --task-id <UUID4>` 默认 E_DISABLED，不读 SendKey、不联网。真实通知另需显式
`--enable-notifications`、可信发送器和本地环境变量 SERVERCHAN_SENDKEY；配置和私密输入
参见 [M0.1 runbook](founder-notifications.md)。**本轮不执行真实发送**，未来由 Founder
另行授权验收；不能把命令的存在当作已经批准实测。普通 status/check/simulate 无网络需求。

投递步骤是：

1. 事务内将 PREPARED 改为 RESERVED 并提交，再调用 M0.1。
2. code=OK 且 provider_accepted=true → ACCEPTED；微信确认仍为 false。
3. 超时、传输/未知异常或进程崩溃 → UNKNOWN 或保留 RESERVED，禁止重启后盲目重发。
4. 明确 provider/HTTP 拒绝 → FAILED，无自动 HTTP 重试。
5. M0.1 明确未发请求的缺凭据/禁用/实例冷却/实例上限，可回到 PREPARED；
   预检间隔 300 秒，最多 3 次预留，计数跨重启保留。缺少 sender 时保留未预留告警。

这是保守的“先记账、最多一次可能发出的尝试”，**不是**跨 SQLite 与第三方的 exactly-once
交付保证：预留后、请求前崩溃可能漏发。ACCEPTED 不代表微信收到；UNKNOWN/RESERVED
须 Founder 人工核对，当前没有自动重置/强制重发入口。已在途通知无法通过 pause/resume 撤回。
没有常驻推送器；notify 必须显式调用，不在 ingest/check 内隐式发送。
跨任务真实每日 provider 配额仍未实现，M0.1 内存预算不冒充持久化账户预算。

## Windows 只读能力探测交接（M0.2b）

| 能力 | 当前结论 |
|---|---|
| Windows Codex CLI 是否安装、真实版本 | Unknown，待 DeepSeek 本机核验 |
| DeepSeek Harness Headless 入口/版本 | Unknown，未臆造可执行名或 headless 参数 |
| 两客户端的安全结构化退出/额度/限流输出 | Unknown，目前只有中立合成格式 |
| native resume 参数及 session 生命周期 | Unknown，opaque UUID 不证明可真实恢复 |

本轮没有执行任一客户端。供 Windows 操作者的手动计划：

1. 先只查看已安装软件的受信文档；PowerShell 可使用
   `Get-Command codex -ErrorAction SilentlyContinue | Select-Object Name,CommandType,Version`
   做命令发现。它不启动模型；不要输出 Path/Source/Definition。这里的文件 Version
   不一定是客户端运行版本，未能证明时仍记 Unknown。
2. DeepSeek 入口只从本机安装说明获取，再用 Get-Command 查询该已知命令；不要猜测
   headless、额度或恢复 flags，不运行任意脚本。原始安装路径映射留在本地，不上传。
3. 只有安装文档明确保证“不发起请求”的版本/帮助操作，才在后续授权探测中手工执行；
   不提供 prompt、不读配置凭据、不执行模型请求、不改执行策略/防火墙/注册表/安全设置。
4. 记录经过审查的名称/真实版本、已核验安全命令的语义、结构化字段类型、退出状态及
   文档依据。不要上传原始 stdout、headers、账号或路径。没有证据的配额码、JSON 输出
   flags 和 resume 行为保持 Unknown。其后单独 Issue 实现具体适配器，用脱敏合成样本回归。

## 离线测试、停用与恢复

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/supervisor -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/notifications -v
```

测试使用 TemporaryDirectory/临时数据库、假时钟、合成 UUID/事件、Mock sender/HTTP，
禁用 socket 与子进程；真实 M0.1 联测也只使用明确的假 key，替换环境映射以免读取真实值。
没有 GitHub/Server酱/模型网络请求。SQLite 并发测试验证抢占和原子回滚；Windows 上如
未授权 symlink，相关平台用例明确 SKIP，不请求提权。缺解释器须报告 SKIP，零测试不算通过。
本次 Linux 测试不能替代 Windows 客户端与文件系统集成验收。

停用只需不调用 notify、保持默认关闭并停止本地 CLI，不改变真实 Agent 状态。
数据库损坏时保留本地原件，停止写入，由 Founder 离线恢复/核对；不要删除账本来“修复”
重复通知，也不要从陈旧备份盲目补发。备份须停止所有本地使用者后复制到本地非 Git 目录，
不得提交或上传 DB。代码回滚通过独立 revert PR；不自动 Merge、不删除检查点、不更改
Windows 安全配置。恢复损坏文件或处理未知投递由人决定，不提供自动修复越过幂等边界。
