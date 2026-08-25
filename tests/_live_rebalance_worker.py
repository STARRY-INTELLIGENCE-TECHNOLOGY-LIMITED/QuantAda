"""为 ``test_live_rebalance_smoke`` 提供干净解释器 worker；退出码 3 表示 broker 软件或账户不可用，由父测试转换为 pytest skip，
其他非零退出码表示真实 smoke 测试失败。
"""

from __future__ import annotations

import datetime as dt
import math
import os
import sys
import time
from types import SimpleNamespace


class BrokerUnavailable(RuntimeError):
    """可选的本地 paper endpoint 无法提供 smoke 测试。"""


class SmokeFailure(RuntimeError):
    """实盘 endpoint 已响应，但调仓契约校验失败。"""


class SmokeData:
    """供持仓快照映射使用的最小可哈希 data 对象。"""

    __hash__ = object.__hash__

    def __init__(self, name: str):
        self._name = name


def _truthy(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value > 0 else default


def _symbol_for(broker_name: str) -> str:
    default = "SHSE.510300" if broker_name == "gm_broker" else "SPY.ARCA"
    value = os.getenv(
        "QUANTADA_LIVE_REBALANCE_GM_SYMBOL"
        if broker_name == "gm_broker"
        else "QUANTADA_LIVE_REBALANCE_IB_SYMBOL",
        default,
    )
    symbol = str(value or "").strip()
    if not symbol:
        raise BrokerUnavailable("live smoke symbol is empty")
    return symbol


def _build_gm_broker(symbol: str, notional: float):
    import config

    conn = dict(getattr(config, "BROKER_ENVIRONMENTS", {}).get("gm_broker", {}).get("sim", {}) or {})
    strategy_id = os.getenv("QUANTADA_GM_STRATEGY_ID", conn.get("strategy_id", ""))
    strategy_id = str(strategy_id or "").strip()
    if strategy_id.lower() in {
        "",
        "xxx",
        "your_strategy_id_here",
        "your_strategy_id",
        "strategy_id",
        "quantada-live-smoke",
    }:
        raise BrokerUnavailable(
            "GM paper strategy ID is not configured; set QUANTADA_GM_STRATEGY_ID"
        )

    try:
        import gm.api as gm_api
        from gm.csdk.c_sdk import gmi_init, gmi_set_mode, py_gmi_set_strategy_id
        from gm.model.storage import context
        from live_trader.adapters.gm_broker import GmBrokerAdapter
    except Exception as exc:  # pragma: no cover - depends on optional SDK
        raise BrokerUnavailable(f"GM SDK is unavailable: {exc}") from exc

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
        raise BrokerUnavailable("GM paper token is not configured")

    try:
        if serv_addr:
            gm_api.set_serv_addr(serv_addr)
        gm_api.set_token(token_text)
        py_gmi_set_strategy_id(strategy_id)
        gmi_set_mode(gm_api.MODE_LIVE)
        context.mode = gm_api.MODE_LIVE
        context.strategy_id = strategy_id
        status = gmi_init()
        if status not in (None, 0):
            # 某些 SDK 版本在账户流尚未完全启动时会返回非零提示码；以下账户探针才是权威依据，因此不能仅凭该提示码拒绝会话。
            try:
                context._set_accounts()
            except Exception:
                pass
        else:
            context._set_accounts()
    except Exception as exc:
        raise BrokerUnavailable(f"GM session initialization failed: {exc}") from exc

    try:
        broker = GmBrokerAdapter(context, cash_override=notional)
        if not broker.is_account_snapshot_trusted():
            detail = getattr(broker, "_last_account_snapshot_fetch_error", "account snapshot unavailable")
            raise BrokerUnavailable(f"GM account snapshot unavailable: {detail}")
        broker.set_datas([SmokeData(symbol)])
        return broker, context
    except BrokerUnavailable:
        raise
    except Exception as exc:
        raise BrokerUnavailable(f"GM broker construction failed: {exc}") from exc


def _build_ib_broker(symbol: str, notional: float):
    try:
        from ib_insync import IB
        from live_trader.adapters.ib_broker import IBBrokerAdapter
    except Exception as exc:  # pragma: no cover - depends on optional SDK
        raise BrokerUnavailable(f"IBKR SDK is unavailable: {exc}") from exc

    import config

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
        client_id = int(
            os.getenv("QUANTADA_IBKR_TEST_CLIENT_ID")
            or os.getenv("IBKR_CLIENT_ID")
            or conn.get("client_id")
            or "991"
        )
    except (TypeError, ValueError) as exc:
        raise BrokerUnavailable(f"IBKR connection settings are invalid: {exc}") from exc

    ib = IB()
    try:
        order_account = os.getenv(
            "QUANTADA_IBKR_ORDER_ACCOUNT",
            getattr(config, "IBKR_ORDER_ACCOUNT", ""),
        )
        kwargs = {"clientId": client_id, "timeout": 2.0}
        if order_account:
            kwargs["account"] = order_account
        try:
            ib.connect(host, port, **kwargs)
        except TypeError:
            kwargs.pop("account", None)
            ib.connect(host, port, **kwargs)
    except Exception as exc:
        try:
            ib.disconnect()
        except Exception:
            pass
        raise BrokerUnavailable(f"IBKR connection failed: {exc}") from exc

    context = SimpleNamespace(ib_instance=ib, now=dt.datetime.now(), use_schedule=True)
    try:
        broker = IBBrokerAdapter(context, cash_override=notional)
        if not broker.is_account_snapshot_trusted():
            detail = getattr(broker, "_last_account_snapshot_fetch_error", "account snapshot unavailable")
            raise BrokerUnavailable(f"IBKR account snapshot unavailable: {detail}")
        broker.set_datas([SmokeData(symbol)])
        return broker, context
    except BrokerUnavailable:
        try:
            ib.disconnect()
        except Exception:
            pass
        raise
    except Exception as exc:
        try:
            ib.disconnect()
        except Exception:
            pass
        raise BrokerUnavailable(f"IBKR broker construction failed: {exc}") from exc


class SmokeStrategy:
    """围绕框架真实 ``BaseStrategy`` API 的最小 adapter。"""

    def __init__(self, broker):
        from strategies.base_strategy import BaseStrategy

        class _Strategy(BaseStrategy):
            def init(self):
                return None

            def next(self):
                return self.execute_rebalance(
                    target_symbols=[broker.datas[0]],
                    top_k=1,
                    rebalance_threshold=0.0,
                    rebalance_when="next",
                )

        self.instance = _Strategy(broker)


def _run_rebalance_smoke(broker_name: str, broker, symbol: str, notional: float):
    data = broker.datas[0]
    price = float(broker.get_current_price(data) or 0.0)
    if not math.isfinite(price) or price <= 0:
        raise BrokerUnavailable(f"{broker_name} returned no usable quote for {symbol}")

    pending = broker.get_pending_orders() or []
    if getattr(broker, "_last_pending_orders_fetch_failed", False):
        detail = getattr(broker, "_last_pending_orders_fetch_error", "pending snapshot unavailable")
        raise BrokerUnavailable(f"{broker_name} pending snapshot unavailable: {detail}")
    position = broker.get_position(data)
    position_size = float(getattr(position, "size", 0.0) or 0.0)
    if not math.isfinite(position_size) or position_size < 0:
        raise SmokeFailure(f"{broker_name} returned invalid position size: {position_size!r}")

    execute_orders = _truthy("QUANTADA_LIVE_REBALANCE_EXECUTE", False)
    if (
        execute_orders
        and broker_name == "gm_broker"
        and not _truthy("QUANTADA_LIVE_REBALANCE_ALLOW_UNFLAT_GM", False)
    ):
        # A-share T+1 结算使同一会话 cleanup SELL 不安全；除非 operator 明确接受持有一笔小额 paper 仓位到下个交易日，
        # 否则默认 integration 命令不产生副作用。
        raise BrokerUnavailable(
            "GM paper execution is disabled by default because A-share T+1 "
            "cannot be flattened in the same smoke run; set "
            "QUANTADA_LIVE_REBALANCE_ALLOW_UNFLAT_GM=1 to opt in"
        )
    if execute_orders and position_size > 0:
        raise BrokerUnavailable(
            f"{symbol} already has position {position_size}; choose an unused paper symbol "
            "before enabling QUANTADA_LIVE_REBALANCE_EXECUTE=1"
        )
    if execute_orders and pending:
        raise BrokerUnavailable(
            f"{broker_name} has existing pending orders; refusing to mix them with the paper smoke order"
        )

    calls = []
    original_order_target_value = broker.order_target_value
    original_get_cash = broker.get_cash
    original_get_rebalance_cash = getattr(broker, "get_rebalance_cash", None)
    original_runtime_config = getattr(broker, "_runtime_config", None)

    # Crypto contract 需要小数数量对齐；该覆盖只在 smoke 调用内生效，生产 run 仍使用 LiveTrader 的正常运行配置。
    if broker_name == "ib_broker" and symbol.upper().startswith("CRYPTO."):
        runtime_config = dict(original_runtime_config or {})
        runtime_config.setdefault("LOT_SIZE", 0.00000001)
        broker._runtime_config = runtime_config

    # ``IBBrokerAdapter.get_cash`` 有意返回账户真实 buying power，因此不使用 BaseLiveBroker 的 ``cash_override``。
    # live smoke 测试仍需硬性的测试内支出上限，所以只在本次调用中限制调仓 planner 和 BUY 路径看到的现金。
    # 不向 broker 或框架配置持久化任何内容。
    def capped_get_cash():
        return min(float(original_get_cash()), notional)

    def capped_get_rebalance_cash():
        if callable(original_get_rebalance_cash):
            return min(float(original_get_rebalance_cash()), notional)
        return capped_get_cash()

    broker.get_cash = capped_get_cash
    broker.get_rebalance_cash = capped_get_rebalance_cash

    if not execute_orders:
        def record_order(data=None, target=0.0, **kwargs):
            calls.append((getattr(data, "_name", ""), float(target)))
            return SimpleNamespace(id="LIVE_SMOKE_DRY_RUN", data=data)

        broker.order_target_value = record_order
    else:
        def execute_and_record(data=None, target=0.0, **kwargs):
            target_value = float(target)
            # 调用真实 adapter 前执行最终校验，防止 adapter 专属现金实现绕过测试内上限。
            if not math.isfinite(target_value) or target_value <= 0 or target_value > notional * 1.01:
                raise SmokeFailure(
                    f"{broker_name} paper target {target_value:.2f} exceeds configured "
                    f"notional {notional:.2f}"
                )
            result = original_order_target_value(data, target, **kwargs)
            calls.append((getattr(data, "_name", ""), target_value, result))
            return result

        broker.order_target_value = execute_and_record

    try:
        strategy = SmokeStrategy(broker).instance
        strategy.next()
    finally:
        broker.order_target_value = original_order_target_value
        broker.get_cash = original_get_cash
        if callable(original_get_rebalance_cash):
            broker.get_rebalance_cash = original_get_rebalance_cash
        else:
            try:
                del broker.get_rebalance_cash
            except AttributeError:
                pass
        if original_runtime_config is None:
            try:
                del broker._runtime_config
            except AttributeError:
                pass
        else:
            broker._runtime_config = original_runtime_config

    if execute_orders:
        # execute 模式 smoke run 有意保持显式且有界，只提交一次 paper 调仓，不保存跨 run 的 retry/queue。
        if not calls:
            raise SmokeFailure(f"{broker_name} produced no paper rebalance order")
        if not any(item[2] is not None for item in calls):
            raise SmokeFailure(f"{broker_name} did not submit the bounded paper rebalance")
        for called_symbol, target, _proxy in calls:
            if called_symbol != symbol or not math.isfinite(target) or target <= 0:
                raise SmokeFailure(
                    f"{broker_name} produced an invalid paper target: {called_symbol!r}, {target!r}"
                )
            if target > notional * 1.01:
                raise SmokeFailure(
                    f"{broker_name} paper target {target:.2f} exceeded configured notional {notional:.2f}"
                )

        buy_proxy = calls[0][2]
        if broker_name == "ib_broker":
            # 即使 BUY 超时也必须进入 cleanup 路径；IB 可能在撤单请求后才确认成交，竞态下省略 finally cleanup 会留下非预期的 paper 持仓。
            try:
                _wait_for_order_terminal(broker, buy_proxy, side="BUY")
                filled_position = _wait_for_position_size(broker, data, minimum=1e-9)
                if filled_position <= 0:
                    raise SmokeFailure(
                        f"{broker_name} BUY reported filled but the position snapshot stayed empty"
                    )
            finally:
                _cleanup_ib_position(
                    broker,
                    data,
                    symbol,
                    original_order_target_value,
                )
    elif calls:
        for called_symbol, target in calls:
            if called_symbol != symbol:
                raise SmokeFailure(
                    f"rebalance routed to unexpected symbol {called_symbol!r}; expected {symbol!r}"
                )
            if not math.isfinite(target) or target < 0:
                raise SmokeFailure(f"rebalance produced invalid target value: {target!r}")

    return {
        "broker": broker_name,
        "symbol": symbol,
        "price": price,
        "position_size": position_size,
        "pending_count": len(pending),
        "planned_orders": len(calls),
        "execute_orders": execute_orders,
    }


def _wait_for_order_terminal(broker, proxy, side: str, timeout_seconds: float = 12.0):
    """等待 IB paper order 进入终态，若仍在途则撤单。"""

    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    while time.monotonic() < deadline:
        if proxy.is_completed():
            return
        if proxy.is_rejected() or proxy.is_canceled():
            raise SmokeFailure(
                f"ib_broker {side} paper order ended as {getattr(proxy, 'status', 'terminal')}"
            )

        remaining = max(0.05, min(0.5, deadline - time.monotonic()))
        ib = getattr(broker, "ib", None)
        sleep_fn = getattr(ib, "sleep", None)
        if callable(sleep_fn):
            sleep_fn(remaining)
        else:
            time.sleep(remaining)

    if proxy.is_pending():
        try:
            broker.cancel_pending_order(proxy.id)
        except Exception:
            pass
        # 撤单与最终成交可能在 IB event stream 上交错到达；给予短暂宽限窗口，让调用方的强制 cleanup 观察到真实终态。
        grace_deadline = time.monotonic() + 5.0
        while time.monotonic() < grace_deadline:
            if proxy.is_completed():
                return
            if proxy.is_rejected() or proxy.is_canceled():
                break
            ib = getattr(broker, "ib", None)
            sleep_fn = getattr(ib, "sleep", None)
            if callable(sleep_fn):
                sleep_fn(0.25)
            else:
                time.sleep(0.25)
    raise SmokeFailure(
        f"ib_broker {side} paper order did not reach Filled before the smoke timeout; "
        f"status={getattr(proxy, 'status', 'unknown')}"
    )


def _cleanup_ib_position(broker, data, symbol, order_target_value):
    """成功或超时后清理残留的 IB paper 持仓。"""

    residual = _wait_for_position_size(broker, data, minimum=None, maximum=None, timeout_seconds=2.0)
    if residual <= 1e-9:
        return

    sell_proxy = order_target_value(data, 0.0)
    if sell_proxy is None:
        raise SmokeFailure(f"ib_broker could not submit the cleanup SELL for {symbol}")
    _wait_for_order_terminal(broker, sell_proxy, side="SELL")
    remaining_position = _wait_for_position_size(broker, data, maximum=1e-9)
    if remaining_position > 1e-9:
        raise SmokeFailure(
            f"ib_broker cleanup SELL completed but position remains {remaining_position}"
        )


def _wait_for_position_size(
    broker,
    data,
    minimum: float = None,
    maximum: float = None,
    timeout_seconds: float = 5.0,
) -> float:
    """读取最新 broker 持仓快照，直到满足指定边界。"""

    deadline = time.monotonic() + max(0.5, float(timeout_seconds))
    last_size = 0.0
    while time.monotonic() < deadline:
        position = broker.get_position(data)
        try:
            last_size = float(getattr(position, "size", 0.0) or 0.0)
        except (TypeError, ValueError):
            last_size = 0.0
        if minimum is not None and last_size >= minimum:
            return last_size
        if maximum is not None and last_size <= maximum:
            return last_size
        ib = getattr(broker, "ib", None)
        sleep_fn = getattr(ib, "sleep", None)
        if callable(sleep_fn):
            sleep_fn(0.25)
        else:
            time.sleep(0.25)
    return last_size


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in {"gm_broker", "ib_broker"}:
        print("SKIP: expected gm_broker or ib_broker", file=sys.stderr)
        return 3

    broker_name = args[0]
    try:
        symbol = _symbol_for(broker_name)
    except BrokerUnavailable as exc:
        print(f"SKIP: {exc}")
        return 3
    default_notional = 1000.0 if broker_name == "gm_broker" else 100.0
    notional = _float_env("QUANTADA_LIVE_REBALANCE_NOTIONAL", default_notional)

    broker = None
    context = None
    try:
        if broker_name == "gm_broker":
            broker, context = _build_gm_broker(symbol, notional)
        else:
            broker, context = _build_ib_broker(symbol, notional)

        summary = _run_rebalance_smoke(broker_name, broker, symbol, notional)
        print(f"PASS: {summary}")
        return 0
    except BrokerUnavailable as exc:
        print(f"SKIP: {exc}")
        return 3
    except SmokeFailure as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 4
    except Exception as exc:
        print(f"FAIL: unexpected live smoke error: {exc}", file=sys.stderr)
        return 4
    finally:
        if context is not None and hasattr(context, "ib_instance"):
            try:
                context.ib_instance.disconnect()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
