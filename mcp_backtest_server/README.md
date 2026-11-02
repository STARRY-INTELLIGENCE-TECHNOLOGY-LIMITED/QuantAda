# QuantAda Backtest MCP Server

This package exposes the configured QuantAda CLI runner as a local, backtest-only MCP server over stdio. It supports MCP protocol version 2025-11-25 and negotiates 2025-06-18, 2025-03-26, and 2024-11-05 for compatible clients.

The server does not import the QuantAda runtime. It starts the configured CLI process, stores job metadata and logs, and returns structured MCP tool results. The server does not expose live trading, the Command Center, optimizer Study resumption, or framework training-kill controls.

## Run

Run: python -m mcp_backtest_server

Example client:

    $env:MCP_BACKTEST_SERVER_CMD = "python -m mcp_backtest_server"
    python mcp_backtest_server/example_client.py

The example negotiates 2025-11-25, starts a task-augmented backtest, polls tasks/get, and retrieves the completed tool result through tasks/result.

Minimal host configuration is in mcp_config.example.json.

## Protocol support

- stdio JSON-RPC transport
- Protocol version negotiation with 2025-11-25, 2025-06-18, 2025-03-26, and 2024-11-05
- tools/list and tools/call, including outputSchema, structuredContent, annotations, and paginated list responses
- Optional native MCP Tasks for backtest_run using tasks/get, tasks/cancel, and tasks/result
- JSON-RPC parse, initialization, method, and parameter errors

MCP Tasks were introduced in 2025-11-25 and are experimental in that specification. Task support is optional. Hosts using older negotiated protocol versions receive the normal backtest_run tool behavior and no Tasks capability.
The 2025-06-18 and 2025-11-25 versions receive structuredContent and outputSchema; older versions receive text tool content and compatible tool definitions.

This package intentionally does not implement Streamable HTTP, Resources, Prompts, client sampling, elicitation, or task status notifications.
Task listing is intentionally omitted because this local stdio server has no requestor identity or per-client authorization context.

## Tools

- backtest_run: starts a backtest, optimizer, or fetch-only CLI job. --connect, --config, UI flags, --train_resume, and --train_kill are always rejected. --no_plot is appended unless plotting is explicitly enabled.
- backtest_status: returns a job's current state.
- backtest_result: returns job metadata, the saved result summary, and bounded stdout/stderr tails.
- backtest_cancel: cancels a queued or running job.
- backtest_list: lists recent jobs.

backtest_run advertises optional task execution. A task starts in working; its result becomes available after the CLI job reaches a terminal state. Task IDs are server-generated and independent of client request_id values. Each Task also returns jobId so results remain queryable after the Task TTL expires.

## Configuration

Environment variables:

- MCP_BACKTEST_CMD_JSON: preferred runner command as a JSON string array, for example ["python", "E:\\Lin\\Github\\QuantAda\\run.py"].
- MCP_BACKTEST_CMD: runner command string, split using platform shell rules.
- MCP_BACKTEST_WORKDIR: working directory for the runner. Defaults to the repository root.
- MCP_BACKTEST_DATA_DIR: persistent job and task metadata directory. Defaults to .data/mcp_backtest_jobs.
- MCP_BACKTEST_ALLOWED_FLAGS: optional comma-separated restriction that can only narrow the built-in allowlist. The live, config, UI, resume, and kill flags remain forbidden even if added here.
- MCP_BACKTEST_DEDUPE: set to 1 to deduplicate identical calls by runner, working directory, arguments, and environment overrides.
- MCP_BACKTEST_MAX_CONCURRENT: maximum concurrent CLI processes; default 1. Excess jobs are queued up to 100.
- MCP_BACKTEST_SCHED_INTERVAL_SEC: queue scheduler interval, default 1.

The server also limits tools/call requests to 300 per minute per server process. These limits are fixed to bound resource use and are not additional configuration namespaces.

Per-call environment overrides are limited to BLAS thread-count variables (1–256), UTF-8 encoding controls, and PYTHONUTF8 (0 or 1). Put provider credentials and graphics-backend configuration in the MCP server's trusted host environment, not in tool arguments.

Task TTL defaults to 24 hours and is capped at 7 days. The server may remove task records after their TTL; the regular job result remains available through the backtest job tools.

If the MCP server exits or restarts, it does not reattach old processes. Queued and running jobs are marked failed so they cannot remain in a permanent working state. Cancellation and timeout cleanup includes optimizer worker descendants.

## Security and trust boundary

The server runs local subprocesses with the permissions of the MCP host process. It has no authentication or per-client authorization and is intended for a trusted local stdio host. Do not expose it as a network service or connect untrusted agents to it. Hosts should require user approval for job creation and cancellation, and operators should restrict the CLI flag allowlist further when a workflow needs a narrower surface.

Backtest jobs and task metadata are stored locally. Job result tools return at most 2,000 log lines and 1 MiB per stream, plus structured status; command paths and environment overrides are not returned in public job metadata.
