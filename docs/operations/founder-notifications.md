# M0.1 Founder 微信通知（Issue #16）

这是经 Founder 同意的可选 Server酱 Turbo 通道，与 ChatGPT Work/Dots 是否可用无关。
只实现通知接线，不是常驻监督器，不代表已经能观测 Codex/DeepSeek 的额度。
外发是第三方数据传输；仅允许以下模板中的最少告警元数据。

## Founder 配置与真实验收（仅合并后）

1. Founder 自行访问 <https://sct.ftqq.com/>，用微信登录并配置微信公众号通知通道。
   SendKey 应以 `SCT` 开头。订阅或付费由 Founder 自行决定，本工具不购买、不充值。
2. 在 GitHub 仓库 **Settings → Secrets and variables → Actions → Repository secrets**
   中保存 `SERVERCHAN_SENDKEY`。不要把值放进聊天、Issue、PR、代码、命令行、文件或日志。
   本任务不访问或创建该 secret，不要求把它注入开发云环境。
3. Founder 手动合并经过 CTO Review 的 PR。默认分支必须已受 branch protection 或适用
   ruleset 保护，GitHub 上 `github.ref_protected` 为 true；本任务不修改保护规则。
   如该先决条件尚未满足，由 Founder 决定仓库设置，不能移除工作流守卫来规避。
4. 到 **Actions → Founder notification manual test → Run workflow**，选择默认分支，
   由仓库所有者本人手动执行。无用户消息输入，不接受 PR 内容。非默认分支、未受保护
   分支或非仓库所有者触发的发送 job 会 **SKIP**，不代表通知通过。
5. 工作流先运行假环境/Mock HTTP 单元测试，再将 repository secret 仅注入发送步骤，
   执行一次固定 `NOTIFIER_TEST`。API 接受必须为 HTTP 2xx 且 JSON 整数 `code == 0`；
   非零、错误响应、超时或网络故障都会令步骤失败。不要以 job completion 代替查看发送步骤。
6. **API 接受不等于微信送达**。Founder 另行确认个人微信是否收到；允许在 Issue #16
   记录脱敏的“API 接受/微信已收/未收”结论，不贴原始响应、凭据或私人微信截图。
   未收到时先人工检查通道、额度和账户配置，不循环重跑刷消息。

真实验收不由 Codex 执行。未合并前不运行携带真实 secret 的工作流，不从 fork/PR
执行通知代码，不启用 `pull_request_target`，不增加自动 push/PR/定时发送事件。
workflow 仅有 `contents: read`；checkout 固定提交 SHA、不保留 Git 凭据，代码固定到
默认分支此次 dispatch 的 commit。工作流并发组仅串行排队，**不**提供每日配额保障。
这些检查约束本工作流，不替代仓库管理员对 workflow 写权限、Actions 权限和 secret 的治理。

官方配置参考：

- <https://sct.ftqq.com/docs/getting-started/sendkey/>
- <https://sct.ftqq.com/docs/integrations/github-actions/>

## 标准事件 v1.0

`build_event(event_type, event_id, github_url, agent)` 返回统一 `{Ok,Code,Message,Data}`。
成功 Data 为标准事件；无效输入的诊断只包含固定安全文本，不回显传入值。

| event_type | state | 人工下一步（摘要） |
|---|---|---|
| FOUNDER_MERGE_REQUIRED | review_pending | Founder 审阅关联 PR 并决定手动合并 |
| BLOCKED_QUOTA | blocked_quota | 人工检查额度/预算，不自动充值或换号 |
| BLOCKED_AUTH | blocked_auth | 人工检查授权，不在消息中传凭据 |
| BLOCKED_EXECUTION | blocked_execution | 查看关联任务并决定安全的下一步 |
| FOUNDER_GAME_ACTION_REQUIRED | human_action_required | 先确认实机授权范围和停止条件 |
| NOTIFIER_TEST | test | Founder 确认微信收件 |

字段为 `schema_version="1.0"`、规范小写 UUID4 `event_id`、上述 `event_type`、
`agent`（`codex/deepseek/cto/founder/system`）、`github_url`、模板派生的 `task`、
`state`、`next_human_action`、`title`。ID 由调用方在事件产生时分配；重复提交须复用 ID，
不要通过新建 ID 重试同一通知。这些事件是接口定义，不是探测器已经接线的事实。

链接只能是本仓库规范 `https://github.com/yanxinnnnnn/desktop-game-agent-lab/issues/<n>`
或 `/pull/<n>`；正整数 n 为 1–9 位，不接受其它仓库、用户信息、查询参数、fragment、
路径穿越或非 HTTPS。消息通过模板包含链接、Agent、任务、状态、下一项人工行动。
标题限制为 1–32 个可打印字符，description 固定为这些白名单字段的摘要。

**隐私处理是拒绝未知输入，不是猜测性替换**：不支持自定义正文、任意标题、PR 标题/
正文、异常文本、机器/Windows 用户名、账号/角色/服务器、路径、日志、截图、headers
或额外 payload。外部事件必须与模板重建结果完全一致；私有日志留在已有授权边界内，
不要塞进字段以求“完整告警”。此策略保留最小元数据，不声称任意文本自动脱敏可靠。

## 发送接口、错误与限流边界

```python
from tools.notifications import NotificationSender, build_event

# uuid、URL 和角色仅表达安全事件元数据；不会探测游戏/客户端状态。
event = build_event(
    'BLOCKED_EXECUTION',
    '00000000-0000-4000-8000-000000000001',  # 合成示例 ID
    'https://github.com/yanxinnnnnn/desktop-game-agent-lab/issues/16',
    'codex',
)
sender = NotificationSender()  # 调用者须复用同一实例
# 默认关闭；不会读取凭据或联网。
result = sender.send(event['Data'])
```

生产调用须额外显式传 `enabled=True`，凭据仅从环境变量 `SERVERCHAN_SENDKEY` 读取，
没有 key 参数、配置文件、CLI key 选项、任意 endpoint 或可覆写 TLS 校验。
协议为标准库 HTTPS POST `https://sctapi.ftqq.com/<SendKey>.send`，表单字段为
`title` 与 `desp`；默认 TLS 校验，禁止跟随任何重定向，以防路径内凭据外泄。
响应最多读取 8193 字节，超过 8192 即拒绝，不回显服务端消息或响应体。

结果字段固定为 `{Ok,Code,Message,Data}`。成功 Data 中
`provider_accepted=true`、`wechat_receipt_confirmed=false`，不声称微信已经收到。
失败使用 `E_DISABLED`、`E_EVENT_INVALID`、`E_TITLE_INVALID`、`E_CREDENTIAL_MISSING`、
`E_CREDENTIAL_INVALID`、`E_PROVIDER`、`E_RESPONSE`、`E_HTTP`、`E_RATE_LIMIT`、
`E_TIMEOUT`、`E_TRANSPORT`、`E_INTERNAL`、`E_DUPLICATE_EVENT`、`E_COOLDOWN`、
`E_SEND_LIMIT`。诊断不返回完整 URL、请求头、key、原始异常或 traceback。
不要为排错启用 HTTP debug、shell tracing 或手动打印环境/Request 对象。

同一个 `NotificationSender` 实例拥有以下内存状态：

- 按 event_id 去重，失败/超时也保留 ID，避免重发可能已送达的通知。
- `BLOCKED_QUOTA` 告警每 300 秒最多尝试一次；其它类型不受这个类别冷却影响。
- 收到 HTTP 429 后所有事件冷却 300 秒，不自动绕过服务端限制。
- 最多 5 次 HTTP 发送尝试，失败也计数；每次请求 10 秒 timeout，**自动重试次数为 0**。
  socket timeout 是网络操作超时，不承诺整个外部调用的硬实时期限。

重新创建实例、换进程或新 Actions run 都会丢失这些计数；这**不是**跨实例/每日配额。
免费账户可能约 5 条/天，以服务商当时规则为准；Founder 须控制手动触发频率。
请求超时可能仍已被服务端接受，不能把“请求失败”当作“肯定未发送”。

## 离线验证与回滚

Python 3.11+，无第三方运行时依赖。从仓库根目录执行：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/notifications -v
python3 -m tools.notifications
```

第二条默认 dry-run，不读 SendKey、不联网；**不要加 `--send` 做本地实测**。
单元测试替换环境映射，仅提供明确的假 key，HTTP opener 全 Mock，socket 禁止联网；
不用真实账户/服务验证错误分支。工作流策略测试为静态检查，不冒充 Actions 平台实测。
缺少 Python 时报告 SKIP，不能报告通过；零测试不满足验收。

紧急停用：停止手动触发；调用方保持 `enabled=False`；如需彻底撤销，由 Founder 在
GitHub 设置删除/轮换 repository secret 或禁用该工作流。代码回滚通过独立 revert PR，
不修改引擎或 Windows 安全配置，不自动 Merge。

**M0.2 待办**：跨实例/跨进程事件持久化、真实每日预算、受控的投递回执与重试策略、
端到端额度监测、本地 Windows Agent 心跳及授权后的监督集成。M0.1 不自动发现
Codex/DeepSeek token 耗尽，不自主控制客户端，不触发 Work、不自动补额度/换账号、
不管理常驻进程、不修改分支保护，也不执行任何真实游戏或浏览器操作。
