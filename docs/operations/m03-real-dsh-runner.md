# M0.3 Phase 2：真实 DSH Windows 单次验收

Refs #33、#30。当前只交付离线接线与 stub 证据。#31 原合成验收继续有效，不重跑。真实探针未分配、未获 Founder 本轮授权；[#23 的授权已消耗](https://github.com/yanxinnnnnn/desktop-game-agent-lab/issues/23#issuecomment-6943319985)。

## 离线验证

从仓库根目录运行 `python -m unittest discover -s tests/supervisor` 和 `python -m unittest discover -s tests/notifications`。使用假 GitHub、临时 SQLite、合成 NDJSON 和本地 stub；Windows 另测固定批处理参数、父进程系统目录伪造、双 home 隔离、超时、后代清理、输出限长及遇到工具事件停止。未启动 DSH、未使用真实凭据、未调用模型，未放宽 #25 的安全门禁。

## CTO 复审前：DeepSeek 离线核对

依据 [#23 已审方案](https://github.com/yanxinnnnnn/desktop-game-agent-lab/issues/23#issuecomment-6792606871) 准备本机配置。命令形状固定为 `dsh --profile headless --patch <临时审核副本> --json <固定 READY 提示>`；提示为 `Reply with exactly: READY. Do not call tools, open files, browse, modify data, or run commands.`。已审核的 `dsh.cmd` 由系统 `cmd.exe /d /v:off /s /c` 启动；原生 `dsh.exe` 不经过 shell。确认包装器准确传回 DSH 退出码、只启动一次，依赖能从审核目录和系统目录解析，且不依赖用户配置或环境。

Worker 用 `GetSystemWindowsDirectoryW` 和 `GetSystemDirectoryW` 构造子进程的 `SystemRoot`、`windir`、系统优先的 `PATH` 及 `ComSpec`。系统路径查询或校验失败时停止，不从父进程环境查找替代解释器。每次运行在同一临时根内建立两个独立目录，分别设置 `DSH_HOME` 和 `DSH_AGENTS_HOME`，不沿用父级 home。

审核实际加载的完整 base/profile/overlay、launcher 及传递依赖；只启用 `deepseek-official/deepseek-flash`，固定 base URL `https://api.deepseek.com/anthropic`，实际请求 `POST https://api.deepseek.com/anthropic/v1/messages`，`max_tokens=64`、应用层 `maxRetries=0`、拒绝重定向。确认临时 home/profile/runtime、无其他 provider、工具、Subagent、标题模型或遥测。**单独确认** `dsh_session_log`、`dsh_plugin_packages` 及其他请求附带数据是否启用并验证禁用方式；仅关闭 OTel 不足。遇到未知项，保持阻塞，勿标成通过。

清单是本机**人工审核证据与字节绑定**：Worker 校验固定策略投影和文件 SHA-256，不宣称自己解析了 DSH 的有效配置。CTO 必须对将要运行的安装版本、闭合依赖清单、Overlay 和实际环境完成复审。Job Object 限制本次进程树和生命期，不提供文件系统沙箱或内核级网络目标限制；实际 HTTP 请求次数也不能由本地 stub 证明。

仓库不提供已批准的真实配置。复审后在仓库外生成本机 JSON 清单：

| 字段 | 审核内容 |
| --- | --- |
| `schema_version`, `issue` | 整数 `1`，及 CTO 独立指定的真实探针编号；不能用 #23、#30、#31 或 #33 |
| `ledger`, `temp_parent` | 稳定的仓库外本机账本绝对路径、私有临时根的父目录；与实机命令保持一致 |
| `launcher`, `overlay`, `runtime_files` | 前两项及数组每项均为 `{ "path": "<本机绝对路径>", "sha256": "<小写哈希>" }`。对实际启动文件、完整有效配置与选定传递依赖逐一固定；不允许链接或从仓库/真实 `.dsh` 取配置 |
| `effective` | `version: "0.2.2-rc.1"`、`profile: "headless"`、`provider: "deepseek-official"`、`model: "deepseek-flash"`、`base_url` / `request_url` 如上、数字 `max_tokens: 64`、数字 `max_retries: 0`、`redirect: "error"` |
| `reviewed_checks` | `effective_configuration`、`isolated_home_and_runtime_paths`、`tools_and_subagents_disabled`、`other_providers_disabled`、`title_and_telemetry_disabled`、`session_log_and_plugin_packages_disabled`、`other_request_data_and_network_reviewed`、`launcher_exit_and_one_call_verified`。只有对实际版本证实的检查才能标为 `true` |

## 仅在 CTO 复审通过、Founder 明确授权之后

真实探针正文只接受唯一的 JSON 代码块 `{"schema_version":1,"task_type":"ready_probe","mode":"real"}`。GitHub 任务声明不会授予真实执行权。本机专用终端须已有本轮授权的进程级 `DEEPSEEK_API_KEY`；禁止展示、复制到命令参数、清单、文件或 PR。只在最终进程启动边界传入该凭据，不传给 `gh`，结束后关闭专用终端。

```powershell
python -m tools.supervisor.worker --mode real --github-probe --issue <探针编号> --base-dir <审核账本目录> --real-plan <审核清单路径> --authorize-real <已审核的清单SHA256>
```

不要用临时重新计算的哈希授权未经复审的清单。此参数绑定 Founder 本机的单次批准与清单字节，不证明身份或替代 CTO/Founder 的审查记录。缺少授权、凭据、预检证据或探针匹配均不会启动 DSH。Worker 先领取 SQLite 任务，使用全新临时根、定长 stdout 和最多 120 秒的 Job Object；不保存 stderr。到期或收到识别到的工具事件时只结束本次 Job。不要删除、更换或回滚账本来重跑。

通过要求：实际退出码 `0` 且有完整同会话 `completed`，Supervisor 记为 `SUCCEEDED` 并在同一探针仅回写一条 `synthetic: false` 的脱敏结果。启动失败、超时、非零退出、缺终态或会话错配均无 PASS。若回执不明，只核对稳定标记、作者及内容；不会重跑、补发或自动恢复。DSH 临时文件正常清除，意外中断若有残留仅由本机人工处理，不上传。回滚只需停用新路径或提交 revert PR；SQLite 表结构未变，保留既有验收记录。
