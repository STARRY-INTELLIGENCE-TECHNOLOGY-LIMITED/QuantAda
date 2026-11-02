import json
import os
import shlex
import subprocess
import sys
import threading
import time


def _json_line(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class MCPClient:
    def __init__(self, cmd):
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._condition = threading.Condition()
        self._write_lock = threading.Lock()
        self._next_id = 1
        self._responses = {}
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        if not self.proc.stdout:
            return
        for line in self.proc.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg_id = message.get("id")
            if msg_id is not None:
                with self._condition:
                    self._responses[msg_id] = message
                    self._condition.notify_all()

    def _send(self, payload):
        if not self.proc.stdin:
            raise RuntimeError("MCP server stdin is closed")
        with self._write_lock:
            self.proc.stdin.write(_json_line(payload) + "\n")
            self.proc.stdin.flush()

    def request(self, method, params=None, timeout=30):
        with self._condition:
            msg_id = self._next_id
            self._next_id += 1
        request = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            request["params"] = params
        self._send(request)
        deadline = time.monotonic() + timeout
        with self._condition:
            while msg_id not in self._responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"timed out waiting for {method}")
                self._condition.wait(timeout=remaining)
            response = self._responses.pop(msg_id)
        if "error" in response:
            raise RuntimeError(f"{method} failed: {response['error']}")
        return response.get("result", {})

    def notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def close(self):
        if self.proc.stdin:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


def main():
    cmd_str = os.getenv("MCP_BACKTEST_SERVER_CMD", "python -m mcp_backtest_server")
    client = MCPClient(shlex.split(cmd_str, posix=(os.name != "nt")))
    try:
        initialized = client.request(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "quantada-example-client", "version": "1.0.0"},
            },
        )
        print(json.dumps(initialized, ensure_ascii=False, indent=2))
        client.notify("notifications/initialized")
        print(json.dumps(client.request("tools/list"), ensure_ascii=False, indent=2))

        created = client.request(
            "tools/call",
            {
                "name": "backtest_run",
                "arguments": {
                    "args": [
                        "sample_macd_cross_strategy",
                        "--symbols=SHSE.600519",
                        "--start_date=20240101",
                        "--no_plot",
                    ],
                    "max_runtime_sec": 3600,
                },
                "task": {"ttl": 2 * 60 * 60 * 1000},
            },
        )
        task_id = created["task"]["taskId"]
        print(f"Task accepted: {task_id} job={created['task']['jobId']}")

        while True:
            task = client.request("tasks/get", {"taskId": task_id})
            print(
                f"Task {task_id}: {task['status']} "
                f"{task.get('statusMessage', '')}"
            )
            if task["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(max(0.1, task.get("pollInterval", 1000) / 1000.0))

        result = client.request("tasks/result", {"taskId": task_id}, timeout=3600)
        print(json.dumps(result.get("structuredContent", result), ensure_ascii=False, indent=2))
    finally:
        client.close()


if __name__ == "__main__":
    main()
