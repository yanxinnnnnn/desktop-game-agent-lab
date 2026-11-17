# M0.3 单次 Worker：合成验收说明

对应 #30，基于合并 #29 的 `main`。这里验收的是 GitHub 到本机 Worker、Supervisor SQLite、再到原 Issue 的自动交接。执行器为固定合成 READY 子进程；`synthetic: true` 及账本 `source=synthetic` 不代表真实 DSH、模型或游戏成功。

## 离线验收

在仓库根目录用 Python 3.11+ 运行：

```powershell
python -m unittest discover -s tests/supervisor -v
```

测试使用 fake GitHub、合成输入和临时 SQLite，覆盖领取、完成、终态缺失/错配和非零退出、超时、并发/重启去重、回写已提交但回执丢失、回写状态未知和晚到回执。固定子进程不联网；没有使用真实 DSH、模型、浏览器、游戏或发送通知。原有 Supervisor 测试仅显式关闭直接打开的 SQLite 连接，以便 Windows 清理临时目录，断言不变。

## Windows GitHub 合成闭环

CTO 指定 #31。只处理显式提供的单个仓库探针编号；复用本机已认证的 `gh`，缺认证返回 `github_auth`，不要求复制凭据。探针正文可有说明，但只接受唯一的 JSON 代码块：

```json
{"schema_version":1,"task_type":"ready_probe","mode":"synthetic"}
```

选择一个仓库之外的稳定本机账本目录。同一台验收机只用这一份账本；不要为同一目标另开账本或并行使用第二台机器。启动一次：

```powershell
$ledger = Join-Path $env:LOCALAPPDATA 'desktop-game-agent-lab/worker'
python -m tools.supervisor.worker --issue 31 --base-dir $ledger --github-probe
```

无 `--github-probe` 时不启动 `gh`。主动指定 `--mode real` 也会在启动任何执行器和 `gh` 之前返回 `real_dsh_disabled`。`--github-probe` 仅用于 CTO 指定的探针，勿用于规格 Issue #30。

验收时对照命令返回的 `task`、GitHub #31 上唯一的结果标记和以下账本查询：

```powershell
python -m tools.supervisor --base-dir $ledger check --task-id <task>
```

通过信号：退出码 0、完整且相同会话的 `completed` 终态、Supervisor `SUCCEEDED`，且 `started` / `finished` 都是 `synthetic`。CTO 在 #31 直接读到一次结果评论；不人工转发。`FAIL` 表示退出或终态不满足；合成子进程无法启动或超时为 `BLOCKED`。原始正文、stdout/stderr、异常、会话 ID、路径、身份、凭据不进入评论或账本。

## 中断和回执

Worker 在任何执行前通过 SQLite 事务领取唯一稳定任务 ID，再记录 `started`。解析完成后按现有 Supervisor 语义记账，并在同一数据库的 `worker_publications` 表预约唯一 POST。只有预约者会发送。响应不明确时只按稳定标记、当前 gh 作者和完整预期内容查询；重复启动也只读对账，不重跑、不补发。评论消失、标记冲突、扫描达到上限或查询失败时保留不确定状态。

首次领取后但尚未预约回写就中断时，返回 `already_claimed`；预约后无法证实提交时，返回 `publication_unknown`。保留账本，由 CTO/Founder 核对状态，不删除账本、不人工补写成 Worker 结果。没有跨机器协调、自动恢复、自动重试或自动通知。既有 Supervisor `RESUMABLE` 状态保留，Worker 不调用恢复。

回滚：停止调用 Worker，未合并时关闭 PR；如果已由 Founder 合并则使用普通 revert PR。保留本地账本与 GitHub 记录作为验收证据；既有 Supervisor 不依赖 Worker 的表，也没有更改原表版本。

## 真实执行边界

真实模式尚未实现或运行。#23 的旧授权已消耗；真实测试需要另一个探针和 Founder 单次授权。启用前先核对 `dsh_session_log` 等附带数据路径、有效配置与工具/标题/遥测禁用、进程环境、官方端点和零应用层重试。只有真实 runner 自身的退出码 0 与完整、匹配的结构化 DSH `completed` 能证明真实通过；不得复用合成标签或证据。
