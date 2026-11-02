# 回测 MCP Server 契约

本文定义 mcp_backtest_server 将 QuantAda CLI 暴露给 MCP Host 的协议和安全边界。实现位于 mcp_backtest_server/；代码与测试是最终事实来源。

## 1. 范围

1. Server 通过本地 stdio 接收 MCP JSON-RPC 消息，并以配置的 CLI 子进程执行回测、优化或显式允许的取数任务。
2. Server 不导入 QuantAda runtime，不暴露实盘交易、配置覆盖、命令工作台、训练续传或训练终止入口。
3. 当前支持 MCP 2025-11-25，并兼容协商 2025-06-18、2025-03-26 与 2024-11-05。客户端请求不受支持的版本时，Server 返回自身最新支持版本；不得把版本差异直接当作初始化失败。
4. 仅在协商 2025-11-25 时声明 MCP Tasks。协商 2025-06-18 时支持 structuredContent 和 outputSchema，但不声明 Tasks；更早协议不返回 structuredContent、outputSchema 或 Task 扩展字段。
5. 当前传输范围仅为 stdio。不得宣称支持 Streamable HTTP、Resources、Prompts、Sampling、Elicitation 或 Task 状态通知。

## 2. 生命周期与 JSON-RPC

1. MCP initialize 必须是会话的第一个请求；成功后等待 notifications/initialized，再处理其它请求。
2. 请求和响应使用 JSON-RPC 2.0。stdout 只能写 MCP JSON 消息；诊断日志写 stderr。
3. 支持 initialize、notifications/initialized、ping、tools/list、tools/call，以及最新协议下的 tasks/get、tasks/cancel、tasks/result。
4. 无效 JSON 返回 -32700；无效 JSON-RPC 消息返回 -32600；未知方法返回 -32601；无效参数或 Task ID 返回 -32602；未初始化请求返回 -32002。
5. Notification 不返回响应。所有请求响应 ID 必须与请求 ID 一致。
6. MCP 当前连接为单 stdio Host。长时间等待的 tasks/result 不得阻塞后续输入消息；响应可以按 JSON-RPC ID 乱序到达。

## 3. 工具契约

1. backtest_run 启动子进程并立即返回 job 信息；图形输出默认关闭。它声明 execution.taskSupport=optional。
2. backtest_status、backtest_result、backtest_cancel 和 backtest_list 使用 CallToolResult；返回的 JSON 对象同时放在 structuredContent 和兼容旧 Host 的 TextContent。
3. 工具定义提供 inputSchema、outputSchema 和适当的注解。只读查询标记 readOnlyHint；取消任务标记 destructiveHint。
4. tools/list 使用标准不透明 cursor 分页；无更多内容时省略 nextCursor。无效 cursor 返回 -32602。
5. Task result 返回原始工具调用类型的 CallToolResult。Task 关联信息通过 io.modelcontextprotocol/related-task 元数据返回。

## 4. 原生 MCP Tasks

1. 仅 backtest_run 支持 Task augmentation。Task capability 声明 tasks.cancel 和 tasks.requests.tools.call，不声明 tasks.list。
2. Task ID 由 Server 生成，且和 client request_id、job ID 解耦。任务元数据持久化在 job 数据目录。Task 对象同时返回 jobId，TTL 到期后仍可用 backtest_result 查询 Job。
3. 创建任务时先记录 working 状态并立即返回 CreateTaskResult；队列中的任务也保持 working，并在 statusMessage 中说明排队状态。
4. CLI job queued/running 对应 Task working；done 对应 completed；非零退出和超时对应 failed；明确取消对应 cancelled。子进程启动失败发生在 Task 接收前，返回普通工具执行错误且不创建 Task。completed、failed、cancelled 是终态，不得迁移到其它状态。
5. Task 对象必须包含 taskId、jobId、status、createdAt、lastUpdatedAt、ttl；可提供 statusMessage 和 pollInterval。时间使用 ISO 8601 UTC。
6. 默认 TTL 为 24 小时，最大 7 天；客户端请求值必须为正整数并在最大值处截断。TTL 到期后 Task 记录可删除，常规 backtest job 记录仍保留。
7. tasks/get 返回任务状态；tasks/cancel 尝试停止对应子进程，并在响应前把任务转入 cancelled；tasks/result 等待任务进入终态后返回与原工具调用同类型的结果。
8. tasks/result 的等待不得阻止 stdio 消息读取。Server 对并发等待请求设有上限，超过时返回可理解的 JSON-RPC 错误。
9. 任务错误通过 failed 状态和 CallToolResult isError 表示；JSON-RPC 错误只用于协议错误，例如不存在/过期 Task ID 或对终态任务再次取消。
10. 任务状态通知是可选能力；当前实现依赖 polling，不声明或发送 notifications/tasks/status。
11. Server 重启或正常退出时不接管旧进程；queued 与 running 的旧 job 一律标记为 failed，避免 Task 永久停留在 working。取消和超时必须递归清理 CLI 及其 worker 进程树。

## 5. CLI 参数与环境安全

1. 子进程通过 argv 数组启动，禁止 shell=True 或 shell 拼接。
2. 默认使用内置保守 CLI allowlist。MCP_BACKTEST_ALLOWED_FLAGS 只允许从内置 allowlist 中进一步收窄；--connect、--force_schedule、--config、--train_resume、--train_kill、--ui、--ui_ip、--ui_port 始终拒绝。
3. --config 被拒绝，避免通过任意大写配置键绕过 UI/live 防护或改变框架路径。CLI option value（如 --n_jobs -1）按参数边界解析，不把负数值误判成独立 flag。
4. 每次调用的 env 覆盖只允许指定的数值库线程变量（1–256）和 UTF-8 编码变量。凭据与图形后端配置通过受信 MCP Host 的 Server 进程环境注入，不通过工具参数传输。
5. stdout/stderr tail 最多返回 2,000 行且每个 stream 不超过 1 MiB；公开 job 元数据不得返回本地命令、工作目录或日志文件绝对路径。
6. Server 使用本机 stdio，无认证和 per-client 授权，因此不声明 tasks.list，避免暴露当前 Server 进程的其它任务元数据；所有 tools/call 共用每进程每分钟 300 次上限，回测子进程默认并发上限为 1、队列上限为 100；仅连接可信的本地 MCP Host，不得将该实现作为网络服务开放。

## 6. 验证要求

修改 MCP runtime 时至少覆盖：版本协商、初始化顺序、未知/无效消息、工具 schema、structuredContent、默认安全 allowlist、环境变量 allowlist、Task 创建/持久化/轮询/结果/取消/TTL，以及 stdio stdout 只输出 JSON-RPC。
