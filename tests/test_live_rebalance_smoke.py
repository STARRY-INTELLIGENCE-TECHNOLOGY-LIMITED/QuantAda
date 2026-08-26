"""可选的 paper-account 调仓 smoke 测试。

普通单元测试必须独立于 GM/IB 桌面软件，因此测试要求 CI 或设置
``QUANTADA_RUN_LIVE_REBALANCE_TESTS=0`` 时跳过；本地运行时还必须能访问配置的
paper endpoint 并提供账户凭证。

worker 在子进程中运行，这是有意设计：单元测试可能安装 fake ``gm`` 模块，
而 live smoke 必须在干净解释器中加载真实 SDK。默认模式只生成真实账户调仓计划，
并用 recorder 替换最终发单；只有设置
``QUANTADA_LIVE_REBALANCE_EXECUTE=1`` 才会提交 paper order。

IBKR execution 执行有界往返；GM execution 只有在设置
``QUANTADA_LIVE_REBALANCE_ALLOW_UNFLAT_GM=1``、明确接受 A-share T+1 无法当日
清仓时才启用。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.live_integration


_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}
_PAPER_IB_PORTS = {7497, 4002}


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    return default


def _live_tests_enabled() -> bool:
    # GitHub Actions 会设置 CI=true；开发者可在任一环境显式启用或禁用测试，无需修改仓库配置。
    return _env_flag("QUANTADA_RUN_LIVE_REBALANCE_TESTS", default=not _env_flag("CI", False))


def _split_host_port(raw: str, default_host: str, default_port: int):
    value = str(raw or "").strip()
    if not value:
        return default_host, int(default_port)
    if ":" not in value:
        return value, int(default_port)
    host, port = value.rsplit(":", 1)
    try:
        return host.strip() or default_host, int(port)
    except (TypeError, ValueError):
        return default_host, int(default_port)


def _tcp_reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=0.5):
            return True
    except (OSError, ValueError, TypeError):
        return False


def _broker_endpoint(broker_name: str):
    import config

    if broker_name == "gm_broker":
        conn = dict(getattr(config, "BROKER_ENVIRONMENTS", {}).get("gm_broker", {}).get("sim", {}) or {})
        token = os.getenv("QUANTADA_GM_TOKEN") or os.getenv("GM_TOKEN")
        if token is None:
            token = conn.get("token") or getattr(config, "GM_TOKEN", "")
        serv_addr = (
            os.getenv("QUANTADA_GM_SERV_ADDR")
            or os.getenv("GM_SERV_ADDR")
            or conn.get("serv_addr", "")
        )
        token_text, separator, embedded_addr = str(token or "").partition("|")
        if separator and embedded_addr and not os.getenv("QUANTADA_GM_SERV_ADDR"):
            serv_addr = embedded_addr
        token_text = token_text.strip()
        if not token_text or token_text.lower() in {"xxx", "your_token_here"}:
            return None, "GM paper token is not configured"
        host, port = _split_host_port(serv_addr, "127.0.0.1", 7001)
        if not _tcp_reachable(host, port):
            return None, f"GM endpoint {host}:{port} is not reachable"
        return (host, port), None

    if broker_name == "ib_broker":
        conn = dict(getattr(config, "BROKER_ENVIRONMENTS", {}).get("ib_broker", {}).get("sim", {}) or {})
        host = (
            os.getenv("QUANTADA_IBKR_HOST")
            or os.getenv("IBKR_HOST")
            or conn.get("host")
            or getattr(config, "IBKR_HOST", "127.0.0.1")
        )
        try:
            port = int(
                os.getenv("QUANTADA_IBKR_PORT")
                or os.getenv("IBKR_PORT")
                or conn.get("port")
                or getattr(config, "IBKR_PORT", 7497)
            )
        except (TypeError, ValueError):
            return None, "IBKR paper port is invalid"
        if port not in _PAPER_IB_PORTS and not _env_flag(
            "QUANTADA_ALLOW_NONPAPER_LIVE_REBALANCE", False
        ):
            return None, (
                f"IBKR port {port} is not a known paper port; set "
                "QUANTADA_ALLOW_NONPAPER_LIVE_REBALANCE=1 to override"
            )
        if not _tcp_reachable(host, port):
            return None, f"IBKR endpoint {host}:{port} is not reachable"
        return (host, port), None

    return None, f"unsupported broker {broker_name!r}"


@pytest.mark.parametrize("broker_name", ["gm_broker", "ib_broker"])
def test_paper_account_rebalance_smoke(broker_name):
    """仅在 paper endpoint 存在时运行快速 live 调仓检查。"""

    if not _live_tests_enabled():
        pytest.skip("live paper-account smoke tests are disabled in CI")

    endpoint, reason = _broker_endpoint(broker_name)
    if endpoint is None:
        pytest.skip(reason)

    worker = Path(__file__).with_name("_live_rebalance_worker.py")
    env = os.environ.copy()
    repo_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(
        item for item in (repo_root, env.get("PYTHONPATH", "")) if item
    )

    command = [sys.executable, str(worker), broker_name]
    try:
        result = subprocess.run(
            command,
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"{broker_name} live smoke worker timed out: {exc}")

    output = "\n".join(item for item in (result.stdout, result.stderr) if item).strip()
    # GM C SDK 可能在解释器销毁期间终止 native session，并将进程状态归一为零；因此 worker 显式输出的 ``SKIP:`` 协议才是可用性判断依据。
    worker_reported_skip = any(
        line.strip().startswith("SKIP:")
        for line in (result.stdout or "").splitlines()
    )
    worker_reported_failure = any(
        line.strip().startswith("FAIL:")
        for line in (result.stdout or "").splitlines()
    ) or any(
        line.strip().startswith("FAIL:")
        for line in (result.stderr or "").splitlines()
    )
    if result.returncode == 3 or worker_reported_skip:
        pytest.skip(output or f"{broker_name} paper account is unavailable")
    assert result.returncode == 0 and not worker_reported_failure, (
        f"{broker_name} live rebalance smoke test failed (exit={result.returncode})\n"
        f"{output}"
    )
