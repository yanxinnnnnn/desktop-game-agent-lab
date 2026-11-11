# Issue #28 Phase A2 合成 Mock 协议

Issue #28 的 Phase A2 规格以已安装客户端的源码检查为依据，记录了流式 Chat Completions 请求以及此前单个 JSON 响应失败的观察结果。下面是 Mock 的合成契约。DSH Headless 的 NDJSON 事件属于另一层接口。

- 所选配置的 `baseURL` 以 `/v1` 结尾，Mock 接受 `POST /v1/chat/completions`。请求使用 `application/json`、UTF-8、单一 `Content-Length`、布尔值 `stream: true`、固定模型名 `dsh-mock` 和非空文本消息数组。非空工具或函数定义、选择和调用记录被拒绝。Mock 不依赖认证值，也不根据 `stream_options` 改变响应。
- 成功响应为 HTTP 200 和 `text/event-stream; charset=utf-8`。三个 `data: <JSON>\n\n` 帧依次发送 assistant 角色、固定内容 `READY`、空 delta 和 `finish_reason: "stop"`。随后发送 `data: [DONE]\n\n`。拒绝响应使用空正文，错误类别写入 Journal。
- 监听地址固定为 `127.0.0.1`，显式提供端口。监听窗口最多 30 秒，每条连接只处理一条请求，单次连接截止时间最多 5 秒。默认一次成功预算，接受后其他有效请求头返回 429，最多记录八次连接尝试。请求头、请求体和 SSE 正文分别限于 8 KiB、64 KiB 和 4 KiB。
- Journal 位于操作系统临时目录下的独立目录中，文件名为 `summary-mock-journal.json`。发送前先持久化接受状态，断线仍消耗成功预算。每次更新先 flush/fsync 临时文件，再替换快照。仅记录序号、布尔标记、计数、状态、分类和耗时。`chunks_sent` 和 `done_sent` 表示本地输出已刷新，客户端是否收到需要另行验证。磁盘 I/O 由操作系统完成，连接截止时间无法中断阻塞的磁盘同步。

从 `cu-test` 运行 `python -B -X utf8 -m unittest discover -s mock_provider -p test_mock_provider.py -v`，测试使用内存流和临时 Journal。后续获准联调时，入口为 `python -m mock_provider --port 8765`，需先确认所用临时目录位于本机，并把端口及 `dsh-mock` 模型名写入合成配置。Phase A2 仅执行离线测试。

真实 SDK 对 Content-Type、`[DONE]` 缺失、`stream_options`、4xx 映射、提前断线和重试的处理仍未知。Phase B 需单独授权，待确认实际配置、有效工具禁用、用户目录隔离和网络出口控制。当前只能观测出口流量。
