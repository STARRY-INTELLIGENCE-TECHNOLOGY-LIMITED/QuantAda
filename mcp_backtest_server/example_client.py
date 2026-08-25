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
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._lock = threading.Lock()
        self._next_id = 1
        self._responses = {}
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        if not self.proc.stdout:
            return
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            msg_id = msg.get("id")
            if msg_id is not None:
                with self._lock:
                    self._responses[msg_id] = msg

    def _send(self, payload):
        if not self.proc.stdin:
            raise RuntimeError("stdin closed")
        self.proc.stdin.write(_json_line(payload) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params=None):
        with self._lock:
            msg_id = self._next_id
            self._next_id += 1
        req = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            req["params"] = params
        self._send(req)
        # 简单的阻塞等待。
        start = time.time()
        while time.time() - start < 10:
            with self._lock:
                if msg_id in self._responses:
                    return self._responses.pop(msg_id)
            time.sleep(0.05)
        raise TimeoutError(f"timeout waiting for {method}")

    def notify(self, method, params=None):
        req = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            req["params"] = params
        self._send(req)

    def close(self):
        if self.proc.stdin:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
        try:
            self.proc.terminate()
        except Exception:
            pass


def main():
    # 示例：MCP_BACKTEST_SERVER_CMD="python -m mcp_backtest_server"。
    cmd_str = os.getenv("MCP_BACKTEST_SERVER_CMD", "python -m mcp_backtest_server")
    cmd = shlex.split(cmd_str, posix=(os.name != "nt"))
    client = MCPClient(cmd)

    # 初始化。
    print(client.request("initialize", {"protocolVersion": "2025-03-26"}))
    client.notify("notifications/initialized")

    # 列出工具。
    print(client.request("tools/list"))

    # 启动一次回测 run（示例参数）。
    run_args = {
        "name": "backtest_run",
        "arguments": {
            "args": ["sample_macd_cross_strategy", "--symbols=SHSE.600519", "--start_date=20240101"],
            "max_runtime_sec": 120,
        },
    }
    run_resp = client.request("tools/call", run_args)
    print(run_resp)

    # 轮询状态。
    job = None
    try:
        content = run_resp["result"]["content"][0]["text"]
        job = json.loads(content).get("job")
    except Exception:
        job = None

    if job:
        job_id = job["id"]
        while True:
            status_resp = client.request(
                "tools/call",
                {"name": "backtest_status", "arguments": {"job_id": job_id}},
            )
            payload = json.loads(status_resp["result"]["content"][0]["text"])
            print(payload)
            if payload["job"]["status"] in ("done", "failed", "canceled"):
                break
            time.sleep(1)

    client.close()


if __name__ == "__main__":
    main()
