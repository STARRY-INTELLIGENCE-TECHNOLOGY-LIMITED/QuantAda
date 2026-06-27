import importlib
import inspect
import sys
import time
import traceback
from types import SimpleNamespace

import pandas as pd

import config
from alarms.manager import AlarmManager
from common.log import extract_order_execution_dt, format_dt
from data_providers.base_provider import BaseDataProvider
from data_providers.manager import DataManager
from live_trader.adapters.base_broker import BaseLiveBroker
from live_trader.data_bridge.data_warm import SchedulePlanner
from live_trader.data_bridge.provider_bridge import _DataManagerProvider, _DataManagerProxy
from run import get_class_from_name


def _format_market_scope(selection=None, symbols=None):
    """格式化市场范围上下文，便于报警消息区分运行实例。"""
    if selection:
        return f"selector={selection}"

    symbol_list = [str(s).strip() for s in (symbols or []) if str(s).strip()]
    if symbol_list:
        return f"symbols={','.join(symbol_list)}"
    return "symbols=N/A"


class _RiskControlChain:
    """
    将多个风控模块串联为一个统一对象，兼容现有单风控调用路径。
    任一子风控触发 SELL 即返回 SELL。
    """

    def __init__(self, controls):
        self.controls = list(controls or [])
        self.exit_triggered = set()

    def check(self, data):
        for control in self.controls:
            try:
                if control.check(data) == 'SELL':
                    return 'SELL'
            except Exception as e:
                broker = getattr(control, 'broker', None)
                msg = f"[Risk Chain] {control.__class__.__name__}.check failed: {e}"
                if broker and hasattr(broker, 'log'):
                    broker.log(msg)
                else:
                    print(msg)
        return None

    def notify_order(self, order):
        for control in self.controls:
            try:
                control.notify_order(order)
            except Exception as e:
                broker = getattr(control, 'broker', None)
                msg = f"[Risk Chain] {control.__class__.__name__}.notify_order failed: {e}"
                if broker and hasattr(broker, 'log'):
                    broker.log(msg)
                else:
                    print(msg)

    def mark_exit_trigger(self, symbol: str):
        self.exit_triggered.add(symbol)
        for control in self.controls:
            if hasattr(control, 'exit_triggered') and isinstance(control.exit_triggered, set):
                control.exit_triggered.add(symbol)

    def clear_exit_trigger(self, symbol: str):
        self.exit_triggered.discard(symbol)
        for control in self.controls:
            if hasattr(control, 'exit_triggered') and isinstance(control.exit_triggered, set):
                control.exit_triggered.discard(symbol)


class LiveTrader:
    """实盘交易引擎"""
    _LIVE_REFRESH_MAX_ATTEMPTS = 3
    _LIVE_REFRESH_RETRY_SLEEP_SECONDS = 1.0

    def __init__(self, config: dict):
        self.user_config = config
        platform = config.get('platform', 'gm')

        # 2. 动态加载对应的 Broker 和 DataProvider 类
        print(f"[Engine] Loading adapter for platform: {platform}...")
        self.BrokerClass, DataProviderClass = self._load_adapter_classes(platform)

        # 3. 实例化组件
        self.data_provider = DataProviderClass()

        self.strategy_class = None
        self.selector_class = None
        self.strategy = None
        self.broker = None
        self.config = None
        self.risk_control = None
        self._data_manager = None
        self._resolved_symbols = None
        # 分钟级数据的“每日全量重基准”执行记录（按 symbol 维度）
        self._intraday_rebase_done_on = {}
        # 每日隔日委托清理执行记录（按自然日）
        self._overnight_cleanup_done_on = None

    def _load_adapter_classes(self, platform: str):
        """
        根据平台名称动态加载对应的模块和类
        约定: platform='ib' -> 模块='live_trader.adapters.ib_broker'
        """
        # 处理模块名称约定 (例如 ib -> ib_broker)
        module_name = platform if platform.endswith('_broker') else f"{platform}_broker"

        try:
            # 动态导入模块 (相对于当前包)
            # 注意：engine.py 位于 live_trader 包下，adapters 也是同级子包
            module_path = f".adapters.{module_name}"
            mod = importlib.import_module(module_path, package=__package__)
        except ImportError as e:
            raise ValueError(
                f"无法加载平台 '{platform}' 的适配器模块 ({module_name}.py)。请确保文件存在于 adapters 目录下。\n错误信息: {e}")

        broker_cls = None
        provider_cls = None

        # 遍历模块成员，自动查找符合条件的类
        # 过滤条件: 必须是定义在该模块中的类 (排除 import 进来的)，且继承自基类
        for name, obj in inspect.getmembers(mod, inspect.isclass):
            # 查找 Broker 类
            if issubclass(obj, BaseLiveBroker) and obj is not BaseLiveBroker:
                # 确保只加载当前模块定义的，防止加载了从其他地方 import 的基类
                if obj.__module__ == mod.__name__:
                    broker_cls = obj

            # 查找 DataProvider 类
            # 注意：有些 Provider 可能是在 adapter 文件中定义的，也可能是 import 的
            # 这里我们放宽限制，只要该模块有这个类且符合接口即可
            if issubclass(obj, BaseDataProvider) and obj is not BaseDataProvider:
                if obj.__module__ == mod.__name__:
                    provider_cls = obj

        if not broker_cls:
            raise ValueError(f"在模块 {module_name} 中未找到继承自 BaseLiveBroker 的类。")

        if not provider_cls:
            # 如果 adapter 文件中没有定义 Provider，尝试容错或使用通用 Provider
            # 但通常我们要求 Adapter 必须提供配套的数据源封装
            raise ValueError(f"在模块 {module_name} 中未找到继承自 BaseDataProvider 的类。")

        print(f"[Engine] Adapter loaded: Broker={broker_cls.__name__}, Provider={provider_cls.__name__}")
        return broker_cls, provider_cls

    @staticmethod
    def _resolve_risk_params(raw_risk_params, risk_name: str):
        """
        风控参数兼容:
        - 单风控: risk_params 直接是 dict
        - 多风控: risk_params 可为 {risk_name: {...}} 结构
        """
        if not isinstance(raw_risk_params, dict):
            return {}
        scoped = raw_risk_params.get(risk_name)
        if isinstance(scoped, dict):
            return scoped
        return raw_risk_params

    @staticmethod
    def _normalize_data_source(raw_source):
        if raw_source is None:
            return None
        if isinstance(raw_source, str):
            cleaned = raw_source.strip()
            return cleaned if cleaned else None
        cleaned = str(raw_source).strip()
        return cleaned if cleaned else None

    @staticmethod
    def _is_default_live_source(platform: str, data_source: str) -> bool:
        plat = str(platform or '').strip().lower()
        ds = str(data_source or '').strip().lower()
        if not plat or not ds:
            return False
        if plat in {'ib', 'ibkr'} and ds in {'ib', 'ibkr'}:
            return True
        if plat in {'gm', 'gmi'} and ds in {'gm', 'gmi'}:
            return True
        return False

    def _maybe_override_live_data_provider(self):
        data_source = self._normalize_data_source(self.config.get('data_source'))
        if not data_source:
            return

        platform = self.config.get('platform', '')
        if self._is_default_live_source(platform, data_source):
            print(
                f"[Engine] Live data_source '{data_source}' matches default provider for {platform}. "
                "Using adapter provider."
            )
            return

        if self._data_manager is None:
            self._data_manager = DataManager()

        if not isinstance(self._data_manager, _DataManagerProxy):
            self._data_manager = _DataManagerProxy(self._data_manager, specified_sources=data_source)

        self.data_provider = _DataManagerProvider(self._data_manager, specified_sources=data_source)
        print(f"[Engine] Live data source override: {data_source}")

    def init(self, context):
        print("--- LiveTrader Engine Initializing ---")

        # 初始化报警器
        self.alarm_manager = AlarmManager()

        # 1. 静态调用 is_live_mode 来判断模式
        is_live = self.BrokerClass.is_live_mode(context)

        # 2. 根据模式决定配置合并策略
        if is_live:
            print("[Engine] Live Trading Mode Detected.")
            platform_config = {}
        else:
            print("[Engine] Platform Backtest Mode Detected.")
            platform_config = self.BrokerClass.extract_run_config(context)

        # 合并配置：平台配置为默认，用户配置有更高优先级
        self.config = {**platform_config, **self.user_config}

        # 从掘金 context 中动态提取 run() 方法传入的 token 并注入到 data_provider
        if hasattr(context, 'token') and context.token:
            # 掘金 SDK 内部的 context.token 默认带有 'bearer ' 前缀，需要清洗掉，否则 set_token 会报错
            raw_token = context.token
            if isinstance(raw_token, str) and raw_token.lower().startswith('bearer '):
                raw_token = raw_token[7:].strip()

            self.config['token'] = raw_token

            # 覆盖 DataProvider 的外部模式状态，使其获得正确的 Token
            if getattr(self.data_provider, 'is_external_mode', False) or getattr(self.data_provider, 'token',
                                                                                 None) == 'EXTERNAL_MODE':
                self.data_provider.token = raw_token
                self.data_provider.is_external_mode = False
                try:
                    from gm.api import set_token
                    set_token(raw_token)
                    print(f"[Engine] Token correctly loaded and cleaned from context: {raw_token[:6]}***")
                except ImportError:
                    pass

        # 兼容实盘 data_source 覆盖：允许使用指定数据源替换默认 Provider
        self._maybe_override_live_data_provider()

        data_source_cfg = self.config.get('data_source')
        data_source_label = data_source_cfg if data_source_cfg else 'default'
        provider_name = self.data_provider.__class__.__name__ if self.data_provider else 'N/A'
        print(f"[Engine] Data source setting: {data_source_label}; provider={provider_name}")

        # 3. 使用最终配置实例化所有组件
        self.strategy_class = get_class_from_name(self.config['strategy_name'], ['strategies'])
        if self.config.get('selection_name'):
            self.selector_class = get_class_from_name(self.config['selection_name'], ['stock_selectors'])

        # 后续流程使用 self.config
        self.broker = self.BrokerClass(context, cash_override=self.config.get('cash'),
                                       commission_override=self.config.get('commission'),
                                       slippage_override=self.config.get('slippage'))
        symbols = self._determine_symbols()
        if not symbols: raise ValueError("No symbols to trade.")

        # 获取 timeframe 和 compression
        timeframe = self.config.get('timeframe', 'Days')
        compression = self.config.get('compression', 1)
        print(f"[Engine] Using timeframe: {compression} {timeframe}")

        # 4. 传入 is_live 标志来获取数据
        datas = self._fetch_all_history_data(symbols, context, is_live=is_live, timeframe=timeframe, compression=compression)
        self.broker.set_datas(list(datas.values()))
        missing_symbols = [s for s in symbols if s not in datas]
        if missing_symbols:
            print(f"[Engine Warning] Data warm-up missing symbols: {missing_symbols}")
        if not self.broker.datas:
            print(
                "[Engine Warning] No data feed loaded during init. "
                "Engine will keep running and retry data recovery on each schedule."
            )
        params = self.config.get('params', {})
        self.strategy = self.strategy_class(broker=self.broker, params=params)
        self.strategy.init()

        # 发送启动死信/通知
        selection_name = self.config.get('selection_name')
        if selection_name:
            market_tag = f"选股: {selection_name}"
        else:
            display_syms = symbols[:3]
            sym_str = ",".join(display_syms)
            if len(symbols) > 3:
                sym_str += f" 等{len(symbols)}个标的"
            market_tag = f"标的: {sym_str}"

        # 发送带有市场标签的启动死信/通知
        start_msg = f"{self.config['strategy_name']} [{market_tag}]"
        if self.config.get('_suppress_start_alarm'):
            print("[Engine] STARTED alarm suppressed for broker session restart.")
        else:
            self.alarm_manager.push_start(start_msg)

        # 5. 加载风控模块 ---
        self.risk_control = None
        risk_name = self.config.get('risk')  # 对应 run.py 的 --risk
        risk_params = self.config.get('risk_params', {})  # 对应 --risk_params

        if risk_name:
            risk_names = [name.strip() for name in str(risk_name).split(',') if name.strip()]
            loaded_controls = []
            failed_controls = []

            for single_name in risk_names:
                try:
                    print(f"[Engine] Loading Risk Control: {single_name}")
                    risk_control_class = get_class_from_name(single_name, ['risk_controls', 'strategies'])
                    module_params = self._resolve_risk_params(risk_params, single_name)
                    loaded_controls.append(risk_control_class(broker=self.broker, params=module_params))
                except Exception as e:
                    failed_controls.append((single_name, e))
                    print(f"Warning: Failed to load risk control '{single_name}'. Error: {e}")

            if len(loaded_controls) == 1:
                self.risk_control = loaded_controls[0]
                print(f"[Engine] Risk Control loaded successfully: {loaded_controls[0].__class__.__name__}")
            elif len(loaded_controls) > 1:
                self.risk_control = _RiskControlChain(loaded_controls)
                loaded_names = [ctrl.__class__.__name__ for ctrl in loaded_controls]
                print(f"[Engine] Risk Control chain loaded successfully: {', '.join(loaded_names)}")
            else:
                self.risk_control = None

            if failed_controls and self.risk_control:
                failed_names = [name for name, _ in failed_controls]
                print(f"[Engine Warning] Some risk controls failed to load: {failed_names}")

        print("--- LiveTrader Engine Initialized Successfully ---")


    def run(self, context):
        print(f"--- LiveTrader Running at {context.now.strftime('%Y-%m-%d %H:%M:%S')} ---")
        self.broker.set_datetime(context.now)
        if self.broker.is_live:
            try:
                self._cleanup_overnight_orders_before_refresh(context)
            except Exception as e:
                warn_msg = (
                    f"Unexpected error in overnight cleanup hook: {e}. "
                    "Continue this run."
                )
                print(f"[Engine Warning] {warn_msg}")
                if hasattr(self, 'alarm_manager') and self.alarm_manager:
                    try:
                        self.alarm_manager.push_text(f"[Engine Warning] {warn_msg}", level='ERROR')
                    except Exception as alarm_err:
                        print(f"[Engine Warning] failed to push overnight cleanup hook alarm: {alarm_err}")

        # 顶层异常捕获，防止策略因单次错误而崩溃
        try:
            # --- 实盘数据热更新逻辑 ---
            # 只有在实盘模式下，每次 schedule 触发 run 时，才需要重新拉取数据
            if self.broker.is_live:
                print("[Engine] Live Mode: Refreshing data...")
                refresh_stats = self._refresh_live_data_with_retry(context)
                total_feeds = int(refresh_stats.get('total_feeds', 0))
                updated_feeds = int(refresh_stats.get('updated_feeds', 0))
                failed_feeds = int(refresh_stats.get('failed_feeds', 0))
                refresh_attempts = int(refresh_stats.get('attempts_used', 1) or 1)

                # 刷新质量门控:
                # - 只要有任一标的刷新失败，直接跳过本轮，避免缺失数据触发错误调仓。
                if total_feeds > 0 and failed_feeds > 0:
                    warn_msg = (
                        f"[Engine Error] Live data refresh incomplete: "
                        f"{updated_feeds}/{total_feeds} updated, {failed_feeds} failed "
                        f"after {refresh_attempts} attempts. Skipping this run."
                    )
                    print(warn_msg)
                    if hasattr(self, 'alarm_manager'):
                        self.alarm_manager.push_text(
                            (
                                f"实盘报错并跳过: 当轮刷新存在缺失 "
                                f"({updated_feeds}/{total_feeds} 成功, {failed_feeds} 失败)。"
                                f"已重试 {refresh_attempts} 次仍未恢复。"
                            ),
                            level='ERROR'
                        )
                    return

            # 初始化或临时网络故障下，可能出现 datas 为空。
            # 此时禁止继续执行策略，先尝试自愈拉数，避免“空仓/无目标”的误导性计划。
            if not self.broker.datas:
                recovered = self._recover_data_feeds(context)
                if not recovered:
                    print(
                        "[Engine Warning] No tradable data feeds available after recovery. "
                        "Skipping this run."
                    )
                    if hasattr(self, 'alarm_manager'):
                        self.alarm_manager.push_text(
                            "实盘跳过: 当前无可用数据源（已尝试恢复）。请检查选股器返回、IB数据权限或网络状态。"
                        )
                    return

            # 1. 执行风控检查（前置，避免被 pending-order gate 吞掉）
            if self.risk_control and self._check_risk_controls():
                print("[Engine] 🛡️ 发现风控动作。底层已自动物理上锁，策略流水线继续向下执行...")

            # 2. 检查策略是否有挂单
            strategy_order = getattr(self.strategy, 'order', None)

            # 若策略层残留了挂单，但柜台和 broker 内部都无在途状态，
            # 视为僵尸单并主动清理，防止无人值守时永久锁死。
            if strategy_order:
                has_real_pending = True
                if hasattr(self.broker, 'get_pending_orders'):
                    try:
                        has_real_pending = bool(self.broker.get_pending_orders())
                        if getattr(self.broker, '_last_pending_orders_fetch_failed', False):
                            has_real_pending = True
                    except Exception:
                        has_real_pending = True

                has_internal_pending = bool(
                    getattr(self.broker, '_pending_sells', set())
                    or getattr(self.broker, '_active_buys', {})
                )

                if (not has_real_pending) and (not has_internal_pending):
                    stale_oid = getattr(strategy_order, 'id', 'UNKNOWN')
                    print(f"[Engine Recovery] Stale strategy.order detected ({stale_oid}). Auto-clearing lock.")
                    self.strategy.order = None
                    strategy_order = None

            if strategy_order:
                print("[Engine] Strategy has a pending order. Notifying and skipping logic.")
                if self.risk_control:
                    self.risk_control.notify_order(strategy_order)
                self.strategy.notify_order(strategy_order)
                print("--- LiveTrader Run Finished (Pending Order) ---")
                return

            # 3. 执行策略的 'next'
            self.strategy.next()

            # 4. 通知策略的新订单
            strategy_order = getattr(self.strategy, 'order', None)  # 重新获取，策略可能已创建新订单
            if strategy_order:
                print("[Engine] New order created by strategy. Notifying...")
                if self.risk_control:
                    self.risk_control.notify_order(strategy_order)
                self.strategy.notify_order(strategy_order)

        except Exception as e:
            # 捕获所有异常，打印错误，然后安全退出当前bar
            # 这样策略在下一个bar才能继续运行
            self.broker.log(f"CRITICAL ERROR in engine.run: {e}", dt=context.now)
            import traceback
            self.broker.log(traceback.format_exc())
            # 即使出错，也打印 "Finished"，表示此bar安全退出
            # 推送带有市场上下文的异常报警
            if hasattr(self, 'alarm_manager'):
                selection_name = self.config.get('selection_name')
                market_tag = f"[{selection_name}]" if selection_name else "[自选标的]"
                self.alarm_manager.push_exception(f"Engine Main Loop {market_tag}", e)

        # 估算下一次运行时间（实际由底层的 schedule 调度器或行情 tick 决定）
        timeframe = self.config.get('timeframe', 'Days')
        compression = self.config.get('compression', 1)
        next_expected_str = "未知"

        try:
            import pandas as pd
            schedule_rule = self.config.get('schedule_rule')
            parsed_schedule = SchedulePlanner.parse_schedule_rule(schedule_rule) if schedule_rule else None
            if parsed_schedule:
                anchor_now = pd.Timestamp(context.now) + pd.Timedelta(seconds=1)
                next_expected = SchedulePlanner.resolve_next_schedule_slot(anchor_now, parsed_schedule)
                if next_expected is not None:
                    next_expected_str = pd.Timestamp(next_expected).strftime('%Y-%m-%d %H:%M:%S')
            elif timeframe == 'Minutes':
                next_expected = context.now + pd.Timedelta(minutes=compression)
                next_expected_str = next_expected.strftime('%Y-%m-%d %H:%M:%S')
            elif timeframe == 'Days':
                next_expected = context.now + pd.Timedelta(days=compression)
                # 日线通常只精确到天，但为了美观也格式化
                next_expected_str = next_expected.strftime('%Y-%m-%d %H:%M:%S')
            elif timeframe == 'Weeks':
                next_expected = context.now + pd.Timedelta(weeks=compression)
                next_expected_str = next_expected.strftime('%Y-%m-%d')
        except Exception:
            pass

        print("--- LiveTrader Run Finished ---")
        if self.broker.is_live:
            print(
                f"[Engine] ⏳ 实盘引擎保持运行中... 预计下一个 K线/调度时间约为: {next_expected_str} (以实际行情或定时任务时区为准)")

    def _cleanup_overnight_orders_before_refresh(self, context):
        """
        在拉取当轮数据前清理隔日遗留委托，缩短“算信号 -> 下单”间的流程噪声。
        仅在每个自然日首次 run 执行一次，保持无状态且避免误杀日内新单。
        """
        keep_overnight = bool(
            self.config.get('KEEP_OVERNIGHT_ORDERS', getattr(config, 'KEEP_OVERNIGHT_ORDERS', False))
        )
        if keep_overnight:
            return

        try:
            run_date = pd.Timestamp(context.now).date()
        except Exception:
            run_date = getattr(context, 'now', None)
            if hasattr(run_date, 'date'):
                run_date = run_date.date()

        if run_date is None:
            return
        if self._overnight_cleanup_done_on == run_date:
            return
        if not hasattr(self.broker, 'cleanup_overnight_orders'):
            self._overnight_cleanup_done_on = run_date
            return

        max_attempts = 5
        pending_cleared = False
        last_pending_snapshot = []

        for attempt in range(1, max_attempts + 1):
            try:
                summary = self.broker.cleanup_overnight_orders() or {}
            except Exception as e:
                print(f"[Engine Warning] overnight order cleanup failed (attempt {attempt}/{max_attempts}): {e}")
                summary = {'total': 0, 'canceled': 0, 'failed': 1, 'skipped': 0}

            total = int(summary.get('total', 0) or 0)
            canceled = int(summary.get('canceled', 0) or 0)
            failed = int(summary.get('failed', 0) or 0)
            skipped = int(summary.get('skipped', 0) or 0)

            print(
                "[Engine] Overnight order cleanup before refresh "
                f"(attempt {attempt}/{max_attempts}): "
                f"total={total}, canceled={canceled}, failed={failed}, skipped={skipped}"
            )

            if canceled > 0 and hasattr(self.broker, 'sync_balance'):
                try:
                    self.broker.sync_balance()
                except Exception as e:
                    print(f"[Engine Warning] sync_balance after overnight cleanup failed: {e}")

            # 短确认仍不通过则继续重试，但不中断本轮策略执行。
            pending_cleared = self._confirm_pending_orders_cleared(max_checks=2, sleep_seconds=0.5)
            if pending_cleared:
                break
            try:
                last_pending_snapshot = self.broker.get_pending_orders() or []
            except Exception:
                last_pending_snapshot = []

            if attempt < max_attempts:
                print("[Engine] Overnight cleanup barrier not cleared, retrying...")

        if not pending_cleared:
            pending_count = len(last_pending_snapshot)
            pending_preview = []
            for po in last_pending_snapshot[:5]:
                if not isinstance(po, dict):
                    continue
                oid = str(po.get('id', '') or '').strip()
                sym = str(po.get('symbol', '') or '').strip()
                direction = str(po.get('direction', '') or '').strip()
                size = po.get('size', '')
                pending_preview.append(f"{oid}:{sym}:{direction}:{size}")
            pending_preview_text = ", ".join(pending_preview) if pending_preview else "N/A"

            warn_msg = (
                f"Overnight cleanup not fully cleared after {max_attempts} attempts. "
                f"pending_count={pending_count}, pending_preview={pending_preview_text}. Continue this run."
            )
            print(f"[Engine Warning] {warn_msg}")
            if hasattr(self, 'alarm_manager') and self.alarm_manager:
                try:
                    self.alarm_manager.push_text(f"[Engine Warning] {warn_msg}", level='ERROR')
                except Exception as e:
                    print(f"[Engine Warning] failed to push overnight cleanup alarm: {e}")

        # 无状态优先：同一自然日只清理一次，避免分钟级重复干扰。
        self._overnight_cleanup_done_on = run_date
        return

    def _refresh_live_data_with_retry(self, context):
        try:
            max_attempts = max(1, int(self._LIVE_REFRESH_MAX_ATTEMPTS))
        except Exception:
            max_attempts = 1
        try:
            retry_sleep_seconds = max(0.0, float(self._LIVE_REFRESH_RETRY_SLEEP_SECONDS))
        except Exception:
            retry_sleep_seconds = 0.0

        last_stats = {
            'total_feeds': 0,
            'updated_feeds': 0,
            'failed_feeds': 0,
        }
        attempts_used = 0

        for attempt in range(1, max_attempts + 1):
            attempts_used = attempt
            refresh_stats = self._refresh_live_data(context) or {}
            total_feeds = int(refresh_stats.get('total_feeds', 0) or 0)
            updated_feeds = int(refresh_stats.get('updated_feeds', 0) or 0)
            failed_feeds = int(refresh_stats.get('failed_feeds', 0) or 0)
            last_stats = {
                'total_feeds': total_feeds,
                'updated_feeds': updated_feeds,
                'failed_feeds': failed_feeds,
            }

            if total_feeds <= 0 or failed_feeds <= 0:
                if attempt > 1:
                    print(
                        "[Engine] Live data refresh recovered "
                        f"on attempt {attempt}/{max_attempts}: "
                        f"{updated_feeds}/{total_feeds} updated, {failed_feeds} failed."
                    )
                break

            print(
                "[Engine Warning] Live data refresh incomplete "
                f"(attempt {attempt}/{max_attempts}): "
                f"{updated_feeds}/{total_feeds} updated, {failed_feeds} failed."
            )
            if attempt < max_attempts:
                print("[Engine] Retrying live data refresh...")
                if retry_sleep_seconds > 0:
                    time.sleep(retry_sleep_seconds)

        last_stats['attempts_used'] = attempts_used
        last_stats['max_attempts'] = max_attempts
        return last_stats

    def _confirm_pending_orders_cleared(self, max_checks=6, sleep_seconds=0.5):
        if not hasattr(self.broker, 'get_pending_orders'):
            return True

        try:
            checks = max(1, int(max_checks))
        except Exception:
            checks = 1
        try:
            wait_s = max(0.0, float(sleep_seconds))
        except Exception:
            wait_s = 0.0

        for idx in range(checks):
            try:
                pending = self.broker.get_pending_orders() or []
            except Exception as e:
                print(f"[Engine Warning] pending-order barrier check failed: {e}")
                return False

            if getattr(self.broker, '_last_pending_orders_fetch_failed', False):
                err = getattr(self.broker, '_last_pending_orders_fetch_error', None)
                print(f"[Engine Warning] pending-order barrier check untrusted: {err}")
                return False

            if len(pending) == 0:
                return True

            if idx < checks - 1 and wait_s > 0:
                time.sleep(wait_s)

        return False

    def notify_order(self, order):
        """
        [系统回调入口] 接收来自底层 Broker 的订单状态更新，并转发给策略和风控。
        """
        # 1. 转发给用户策略 (最重要)
        if self.strategy:
            self.strategy.notify_order(order)

        # 2. 转发给风控模块 (如果有)
        if self.risk_control:
            self.risk_control.notify_order(order)

    def _determine_symbols(self) -> list:
        """根据最终配置决定交易的标的列表"""
        if self._resolved_symbols is not None:
            return list(self._resolved_symbols)

        symbols = []
        if self.selector_class:
            if self._data_manager is None:
                self._data_manager = DataManager()

            selector_instance = self.selector_class(data_manager=self._data_manager)
            raw_symbols = selector_instance.run_selection()

            if isinstance(raw_symbols, pd.DataFrame):
                symbols = [str(s) for s in raw_symbols.index.tolist()]
            elif isinstance(raw_symbols, (pd.Index, list, tuple, set)):
                symbols = [str(s) for s in raw_symbols]
            else:
                raise ValueError(
                    f"Selector '{self.selector_class.__name__}' returned unsupported type: {type(raw_symbols).__name__}"
                )
            print(f"Selector selected symbols: {symbols}")
        else:
            configured_symbols = self.config.get('symbols', [])
            if isinstance(configured_symbols, str):
                symbols = [configured_symbols]
            elif isinstance(configured_symbols, (list, tuple, set, pd.Index)):
                symbols = [str(s) for s in configured_symbols]
            else:
                raise ValueError(f"Unsupported symbols type in config: {type(configured_symbols).__name__}")

        # 去重并过滤空值，保持原始顺序
        deduped = []
        seen = set()
        for sym in symbols:
            sym_norm = str(sym).strip()
            if not sym_norm or sym_norm in seen:
                continue
            deduped.append(sym_norm)
            seen.add(sym_norm)

        self._resolved_symbols = deduped
        return list(self._resolved_symbols)

    def _fetch_all_history_data(self, symbols: list, context, is_live: bool,
                                timeframe: str, compression: int) -> dict:
        """根据模式获取数据：实盘模式获取预热数据，回测模式获取全部历史"""
        datas = {}

        if is_live:
            # 实盘模式: 仅获取最近的预热数据，用于计算指标
            if timeframe == 'Minutes':
                end_date = context.now.strftime('%Y-%m-%d %H:%M:%S')
            else:
                end_date = context.now.strftime('%Y-%m-%d')
            # 默认使用年交易日以适应各种长周期指标，无需用户配置
            if timeframe == 'Minutes':
                start_date = (context.now - pd.Timedelta(days=config.ANNUAL_FACTOR)).strftime('%Y-%m-%d %H:%M:%S')
            else:
                start_date = (context.now - pd.Timedelta(days=config.ANNUAL_FACTOR)).strftime('%Y-%m-%d')
            print(f"[Engine] Live mode data fetch (warm-up): from {start_date} to {end_date}")
        else:
            # 平台回测模式: 默认从 start_date 往前预热，保证长周期指标可用
            raw_start_date = self.config.get('start_date')
            end_date = self.config.get('end_date')
            start_date = raw_start_date

            warmup_days = config.ANNUAL_FACTOR
            if raw_start_date and warmup_days > 0:
                try:
                    warmup_start_ts = pd.to_datetime(raw_start_date) - pd.Timedelta(days=warmup_days)
                    if timeframe == 'Minutes':
                        start_date = warmup_start_ts.strftime('%Y-%m-%d %H:%M:%S')
                    else:
                        start_date = warmup_start_ts.strftime('%Y-%m-%d')

                    print(
                        "[Engine] Backtest mode data fetch "
                        f"(warm-up {warmup_days}d): from {start_date} to {end_date} (run starts at {raw_start_date})"
                    )
                except Exception as e:
                    start_date = raw_start_date
                    print(
                        f"[Engine Warning] Backtest warm-up failed for start_date={raw_start_date}: {e}. "
                        "Fallback to raw start_date."
                    )
                    print(f"[Engine] Backtest mode data fetch: from {start_date} to {end_date}")
            else:
                print(f"[Engine] Backtest mode data fetch: from {start_date} to {end_date}")

        for symbol in symbols:
            df = self.data_provider.get_history(symbol, start_date, end_date,
                                                timeframe=timeframe, compression=compression)
            if df is not None and not df.empty:
                class DataFeedProxy:
                    def __init__(self, df, name):
                        self.p = SimpleNamespace(dataname=df)
                        self._name = name

                    def __repr__(self):
                        return self._name

                datas[symbol] = DataFeedProxy(df, symbol)
            else:
                print(
                    f"[Engine Warning] No history data for {symbol} "
                    f"({start_date} -> {end_date}, {compression} {timeframe})."
                )
        return datas

    def _refresh_live_data(self, context):
        """
        实盘数据刷新
        重新获取包含最新 K 线的数据，并【原地更新】策略中的 DataFeed 对象
        """
        # 获取配置
        timeframe = self.config.get('timeframe', 'Days')
        compression = self.config.get('compression', 1)

        # 重新计算时间窗口 (Warmup ~ Now)
        now_ts = pd.Timestamp(context.now)
        end_date = now_ts.strftime('%Y-%m-%d %H:%M:%S') if timeframe == 'Minutes' else now_ts.strftime('%Y-%m-%d')
        today_key = now_ts.strftime('%Y-%m-%d')

        def _align_to_index_tz(dt_input, index):
            dt = pd.Timestamp(dt_input)
            idx_tz = getattr(index, 'tz', None)
            if idx_tz is not None:
                if dt.tzinfo is None:
                    return dt.tz_localize(idx_tz)
                return dt.tz_convert(idx_tz)
            if dt.tzinfo is not None:
                return dt.tz_convert(None)
            return dt

        def _build_incremental_start(existing_df: pd.DataFrame) -> str:
            # 首次/空数据回退到预热窗口
            if existing_df is None or existing_df.empty:
                warmup_start = now_ts - pd.Timedelta(days=config.ANNUAL_FACTOR)
                return warmup_start.strftime('%Y-%m-%d %H:%M:%S') if timeframe == 'Minutes' else warmup_start.strftime('%Y-%m-%d')

            last_bar_ts = pd.Timestamp(existing_df.index[-1])
            if timeframe == 'Minutes':
                backoff = pd.Timedelta(minutes=max(1, int(compression)) * 3)
                start_ts = last_bar_ts - backoff
                return start_ts.strftime('%Y-%m-%d %H:%M:%S')

            # 日线模式保留少量回看，覆盖供应商延迟修订
            start_ts = last_bar_ts - pd.Timedelta(days=2)
            return start_ts.strftime('%Y-%m-%d')

        def _build_window_start() -> str:
            start_ts = now_ts - pd.Timedelta(days=config.ANNUAL_FACTOR)
            return start_ts.strftime('%Y-%m-%d %H:%M:%S') if timeframe == 'Minutes' else start_ts.strftime('%Y-%m-%d')

        # 遍历 Broker 中已有的 DataFeed
        total_feeds = 0
        updated_feeds = 0
        failed_feeds = 0
        for data_feed in self.broker.datas:
            total_feeds += 1
            symbol = data_feed._name
            old_df = None
            if hasattr(data_feed, 'p') and hasattr(data_feed.p, 'dataname'):
                old_df = data_feed.p.dataname

            # Days/Weeks 等低频：每次窗口全量替换，避免复权口径与增量拼接错位。
            force_window_rebase = timeframe != 'Minutes'
            # Minutes：保留增量，但每天首次做一次窗口全量重基准。
            if timeframe == 'Minutes':
                last_rebase_day = self._intraday_rebase_done_on.get(symbol)
                if last_rebase_day != today_key:
                    force_window_rebase = True

            start_date = _build_window_start() if force_window_rebase else _build_incremental_start(old_df)
            new_df = self.data_provider.get_history(symbol, start_date, end_date,
                                                    timeframe=timeframe, compression=compression)

            if new_df is not None and not new_df.empty:
                # 原地更新：不创建新对象，而是替换对象内部的 DataFrame
                # 这样 self.strategy.datas 中的引用会自动指向新数据
                # 假设 DataFeedProxy 使用 .p.dataname 存储数据 (参考 _fetch_all_history_data)
                if hasattr(data_feed, 'p') and hasattr(data_feed.p, 'dataname'):
                    if force_window_rebase:
                        refreshed_df = new_df.sort_index()
                        cutoff_ts = now_ts - pd.Timedelta(days=config.ANNUAL_FACTOR)
                        cutoff_ts = _align_to_index_tz(cutoff_ts, refreshed_df.index)
                        refreshed_df = refreshed_df[refreshed_df.index >= cutoff_ts]
                        if refreshed_df.empty:
                            print(f"  Warning: Rebased window is empty for {symbol}, keeping previous data.")
                            failed_feeds += 1
                            continue
                        data_feed.p.dataname = refreshed_df
                        if timeframe == 'Minutes':
                            self._intraday_rebase_done_on[symbol] = today_key
                        print(f"  Data rebased for {symbol}: {len(refreshed_df)} bars (Last: {refreshed_df.index[-1]})")
                        updated_feeds += 1
                    elif old_df is not None and not old_df.empty:
                        merged_df = pd.concat([old_df, new_df])
                        merged_df = merged_df[~merged_df.index.duplicated(keep='last')]
                        merged_df = merged_df.sort_index()

                        # 保持固定预热窗口，避免数据无限增长
                        cutoff_ts = now_ts - pd.Timedelta(days=config.ANNUAL_FACTOR)
                        cutoff_ts = _align_to_index_tz(cutoff_ts, merged_df.index)
                        merged_df = merged_df[merged_df.index >= cutoff_ts]
                        data_feed.p.dataname = merged_df
                        print(f"  Data refreshed for {symbol}: {len(merged_df)} bars (Last: {merged_df.index[-1]})")
                        updated_feeds += 1
                    else:
                        data_feed.p.dataname = new_df.sort_index()
                        print(f"  Data refreshed for {symbol}: {len(new_df)} bars (Last: {new_df.index[-1]})")
                        updated_feeds += 1
                else:
                    print(f"  Warning: Cannot update data for {symbol}. Structure mismatch.")
                    failed_feeds += 1
            else:
                print(f"  Warning: No new data fetched for {symbol} during refresh.")
                failed_feeds += 1

        return {
            'total_feeds': total_feeds,
            'updated_feeds': updated_feeds,
            'failed_feeds': failed_feeds,
        }

    # 风控检查辅助方法
    def _check_risk_controls(self) -> bool:
        current_dt = self.broker.datetime.datetime()
        if hasattr(current_dt, 'tzinfo') and current_dt.tzinfo is not None:
            current_dt = current_dt.replace(tzinfo=None)
        triggered_action = False

        # 1. 初始化风控订单跟踪字典 (如果尚未存在)
        if not hasattr(self, '_pending_risk_orders'):
            self._pending_risk_orders = {}

        for data_feed in self.broker.datas:
            data_name = data_feed._name

            # --- A. 仓位检查与状态重置 ---
            # 无论之前状态如何，只要当前仓位为 0，就说明风控已完成或无风险
            position = self.broker.getposition(data_feed)

            if not position.size:
                # 如果有遗留的风控状态，清理掉
                if data_name in self._pending_risk_orders:
                    self.broker.log(f"[Risk] Position is closed for {data_name}. Clearing pending risk status.")
                    del self._pending_risk_orders[data_name]

                # 仓位已清零，风控解除，解锁该标的，允许策略重新考察它
                if hasattr(self.broker, 'unlock_for_risk'):
                    self.broker.unlock_for_risk(data_name)

                # 同步清理 risk_control 内部可能存在的标记 (兼容旧有的 exit_triggered 逻辑)
                if hasattr(self.risk_control, 'clear_exit_trigger'):
                    self.risk_control.clear_exit_trigger(data_name)
                elif hasattr(self.risk_control, 'exit_triggered') and isinstance(self.risk_control.exit_triggered, set):
                    self.risk_control.exit_triggered.discard(data_name)
                continue

            # --- B. 检查是否存在正在进行的风控订单 (异步处理核心) ---
            if data_name in self._pending_risk_orders:
                pending_order = self._pending_risk_orders[data_name]

                # 使用 BaseOrderProxy 的标准接口检查状态
                # 注意：这里依赖 callback 或 broker 自动更新 pending_order 对象的内部状态

                # 终态优先判断，避免 is_accepted=True 的对象吞掉已撤单/拒单状态
                if pending_order.is_completed():
                    # 订单已完成
                    # 理论上仓位会在下一次循环被判定为 0，从而走入 A 步骤清理状态。
                    # 这里先移除追踪，允许逻辑继续
                    self.broker.log(f"[Risk] Exit order for {data_name} reported Completed.")
                    del self._pending_risk_orders[data_name]
                    # 不 return，允许本次循环继续检查（双重保险）

                elif pending_order.is_rejected() or pending_order.is_canceled():
                    # 订单失败或被撤销
                    # 清除状态，这样下一行代码就会重新执行 risk_control.check()
                    # 从而尝试再次发起平仓
                    self.broker.log(
                        f"[Risk] Exit order for {data_name} failed (Rejected/Canceled). Resetting to retry.")
                    del self._pending_risk_orders[data_name]

                elif pending_order.is_pending() or pending_order.is_accepted():
                    # 关键点：订单正在交易所排队或处理中。
                    # 此时绝对不能再次发送订单，也不能清除状态。
                    self.broker.log(f"[Risk] Pending exit order for {data_name} is active. Waiting for execution...")
                    triggered_action = True  # 标记为 True，告诉 engine 跳过 strategy.next()
                    continue

                else:
                    # 其他未知状态，保守起见视为 Pending
                    triggered_action = True
                    continue

            # --- C. 准备数据代理 (保持原逻辑) ---
            feed_proxy = None
            try:
                df = data_feed.p.dataname
                if df is None or df.empty:
                    continue

                current_dt_ts = pd.Timestamp(current_dt)
                idx_tz = getattr(df.index, 'tz', None)
                if idx_tz is not None:
                    if current_dt_ts.tzinfo is None:
                        current_dt_ts = current_dt_ts.tz_localize(idx_tz)
                    else:
                        current_dt_ts = current_dt_ts.tz_convert(idx_tz)
                elif current_dt_ts.tzinfo is not None:
                    current_dt_ts = current_dt_ts.tz_convert(None)

                # O(logN) 定位 <= 当前时间的最后一根K线，避免每次全表切片
                pos = df.index.searchsorted(current_dt_ts, side='right') - 1
                if pos < 0:
                    continue

                bar = df.iloc[pos]
                history_window = df.iloc[:pos + 1]

                class BtFeedProxy:
                    class _LineProxy:
                        def __init__(self, values):
                            self._values = list(values)

                        def __len__(self):
                            return len(self._values)

                        def __getitem__(self, idx):
                            if not isinstance(idx, int):
                                raise TypeError("Line index must be int.")
                            total = len(self._values)
                            if total <= 0:
                                raise IndexError("Line is empty.")

                            # 兼容 data.close[0] 当前值；并容忍正/负方向取历史值
                            if idx >= 0:
                                target = total - 1 - idx
                            else:
                                target = total - 1 + idx

                            if target < 0 or target >= total:
                                raise IndexError("Line index out of range.")
                            return self._values[target]

                        def get(self, ago=0, size=None):
                            total = len(self._values)
                            if total <= 0:
                                return []

                            try:
                                ago_int = int(ago)
                            except Exception:
                                ago_int = 0
                            ago_int = max(0, ago_int)

                            end = total - ago_int
                            if end <= 0:
                                return []

                            if size is None:
                                start = 0
                            else:
                                try:
                                    size_int = int(size)
                                except Exception:
                                    size_int = end
                                size_int = max(0, size_int)
                                start = max(0, end - size_int)

                            return self._values[start:end]

                    def __init__(self, name, bar_data, window_df):
                        self._name = name
                        # 单点访问（[0]）与序列访问（get）同时可用
                        self.close = self._LineProxy(window_df['close'].tolist())
                        self.open = self._LineProxy(window_df['open'].tolist())
                        self.high = self._LineProxy(window_df['high'].tolist())
                        self.low = self._LineProxy(window_df['low'].tolist())
                        # 保持当前bar时间一致，同时允许历史读取
                        dt_values = [pd.Timestamp(ts) for ts in window_df.index.tolist()]
                        self.datetime = self._LineProxy(dt_values)
                        self._bar = bar_data

                    def __len__(self):
                        return len(self.close)

                feed_proxy = BtFeedProxy(data_feed._name, bar, history_window)

            except Exception as e:
                self.broker.log(f"[Risk] Error creating bar proxy for {data_feed._name}: {e}")
                continue

            # --- D. 执行风控逻辑检查 ---
            # 只有在当前该标的没有 Pending 订单时，才执行检查
            if data_name not in self._pending_risk_orders:
                action = self.risk_control.check(feed_proxy)

                if action == 'SELL':
                    self.broker.log(f"Risk module triggered SELL for {data_feed._name}")

                    # 底层物理上锁，瞬间切断策略层买入该标的的可能
                    if hasattr(self.broker, 'lock_for_risk'):
                        self.broker.lock_for_risk(data_name)

                    # 执行平仓
                    order = self.broker.order_target_percent(data=data_feed, target=0.0)

                    if order:
                        # 记录订单对象，而不是依赖价格
                        self._pending_risk_orders[data_name] = order

                        triggered_action = True

                        # 记录到 risk_control 内部 (兼容)
                        if hasattr(self.risk_control, 'mark_exit_trigger'):
                            self.risk_control.mark_exit_trigger(data_name)
                        elif hasattr(self.risk_control, 'exit_triggered') and isinstance(self.risk_control.exit_triggered, set):
                            self.risk_control.exit_triggered.add(data_name)
                    else:
                        self.broker.log(f"[Risk] Error: Failed to submit sell order for {data_name}")

        return triggered_action

    def _recover_data_feeds(self, context) -> bool:
        """
        当 broker.datas 为空时，尝试按当前 symbols 重拉预热数据并重建 DataFeed。
        返回是否恢复成功。
        """
        symbols = self._determine_symbols()
        if not symbols:
            print("[Engine Warning] Recovery skipped: selector returned no symbols.")
            return False

        timeframe = self.config.get('timeframe', 'Days')
        compression = self.config.get('compression', 1)
        datas = self._fetch_all_history_data(
            symbols=symbols,
            context=context,
            is_live=True,
            timeframe=timeframe,
            compression=compression
        )
        self.broker.set_datas(list(datas.values()))
        if self.broker.datas:
            loaded = [d._name for d in self.broker.datas]
            print(f"[Engine] Data feeds recovered: {loaded}")
            return True
        return False

def on_order_status_callback(context, raw_order):
    """
    实盘的订单状态回调函数。
    当订单状态发生变化（部成、全成、撤单、废单）时触发。
    """
    # 获取报警器单例
    alarm_manager = AlarmManager()

    if hasattr(context, 'strategy_instance') and context.strategy_instance:
        try:
            # 1. 获取 Broker 抽象实例
            strategy = context.strategy_instance
            broker = strategy.broker

            # --- 关键修改点 ---
            # 不再 import GmOrderProxy，而是让 broker 自己去“装箱”
            # 也不需要在 engine 里去遍历 datas 找 matched_data，这也应该是 broker 的责任
            order_proxy = broker.convert_order_proxy(raw_order)

            # 2. 喂给 Broker，让它去维护在途单
            if hasattr(broker, 'on_order_status'):
                broker.on_order_status(order_proxy)

            current_status = getattr(order_proxy, 'status', 'Unknown')
            order_id = str(getattr(order_proxy, 'id', '') or '')
            try:
                is_completed = bool(order_proxy.is_completed())
            except Exception:
                is_completed = str(current_status).strip().upper() == 'FILLED'

            def _extract_target_qty(proxy, fallback=0):
                qty = None
                if hasattr(proxy, 'trade') and hasattr(proxy.trade, 'order'):
                    qty = getattr(proxy.trade.order, 'totalQuantity', None)
                elif hasattr(proxy, 'raw_order'):
                    qty = getattr(proxy.raw_order, 'volume', None)
                elif hasattr(proxy, 'platform_order'):
                    qty = getattr(proxy.platform_order, 'volume', None)

                if qty in (None, ''):
                    qty = fallback

                try:
                    qty_f = float(qty)
                    if abs(qty_f - round(qty_f)) < 1e-9:
                        return int(round(qty_f))
                    return qty_f
                except Exception:
                    return qty

            try:
                exec_size = float(getattr(order_proxy.executed, 'size', 0) or 0.0)
            except Exception:
                exec_size = 0.0
            try:
                exec_price = float(getattr(order_proxy.executed, 'price', 0) or 0.0)
            except Exception:
                exec_price = 0.0
            exec_dt = extract_order_execution_dt(order_proxy, fallback=getattr(context, 'now', None))

            # 同一订单同一状态（含成交快照）重复回调去重，防止告警/日志刷屏。
            dedupe_key = order_id if order_id else f"raw:{id(raw_order)}"
            if not hasattr(strategy, '_order_callback_dedupe'):
                strategy._order_callback_dedupe = {}
            dedupe_cache = strategy._order_callback_dedupe
            event_signature = (current_status, round(exec_size, 8), round(exec_price, 8))
            if dedupe_cache.get(dedupe_key) == event_signature:
                # 重复状态静默丢弃，避免高频回调刷屏。
                return
            dedupe_cache[dedupe_key] = event_signature
            if len(dedupe_cache) > 5000:
                dedupe_cache.clear()

            # 调用策略通知
            strategy.notify_order(order_proxy)

            # 安全访问 statusMsg，防止 AttributeError
            # 掘金 order 对象可能是动态属性，statusMsg 不一定存在
            msg = getattr(raw_order, 'statusMsg', None)
            if not msg:
                msg = getattr(raw_order, 'ord_rej_reason_detail', '')  # 尝试获取拒单原因

            if is_completed:
                print(f"[Engine Callback] Notified strategy of order status: {current_status} ({msg})")
            # 如果状态是 "已提交" 但还没 "成交"，且未被拒绝，则推送一条消息
            if current_status in ['PreSubmitted', 'Submitted', 'PendingSubmit']:
                # 为了防止刷屏，只有当成交量为0时才推送这个"提交确认"
                # (如果成交量>0，下面的成交逻辑会接管)
                if order_proxy.executed.size == 0:
                    # 对外部遗留单（非本引擎本轮/本地状态跟踪）不推送“已提交”，避免误导。
                    is_tracked = True
                    if hasattr(broker, '_active_buys') and hasattr(broker, '_pending_sells'):
                        active_buys = getattr(broker, '_active_buys', {})
                        pending_sells = getattr(broker, '_pending_sells', set())
                        is_tracked = bool(order_id and (order_id in active_buys or order_id in pending_sells))
                    if not is_tracked:
                        print(f"[Engine Callback] Skip submitted alarm for untracked order ({order_id or 'UNKNOWN'}).")
                    else:
                        total_qty = _extract_target_qty(order_proxy, fallback=0)

                        action = "BUY" if order_proxy.is_buy() else "SELL"
                        symbol = order_proxy.data._name if order_proxy.data else "Unknown"

                        # 构造消息: ⏳ 代表等待/进行中
                        alarm_msg = f"⏳ 订单已提交 ({current_status}): {action} {total_qty} {symbol}"
                        # 使用 push_text 发送普通文本通知
                        alarm_manager.push_text(alarm_msg)


            # 报警通知
            # A. 交易成交推送 (仅终态 Filled 推送一次)
            trade_push_key = order_id if order_id else f"raw:{id(raw_order)}"
            if not hasattr(strategy, '_terminal_trade_push_dedupe'):
                strategy._terminal_trade_push_dedupe = set()
            terminal_trade_push_dedupe = strategy._terminal_trade_push_dedupe
            if is_completed and exec_size > 0:
                # 排除已被拒绝的废单(虽然废单size通常为0，为了严谨双重检查)
                if not order_proxy.is_rejected() and trade_push_key not in terminal_trade_push_dedupe:
                    trade_info = {
                        'symbol': order_proxy.data._name if order_proxy.data else "Unknown",
                        'action': 'BUY' if order_proxy.is_buy() else 'SELL',
                        'price': order_proxy.executed.price,
                        'size': _extract_target_qty(order_proxy, fallback=order_proxy.executed.size),
                        'value': order_proxy.executed.value,
                        'dt': format_dt(exec_dt),
                    }
                    alarm_manager.push_trade(trade_info)
                    terminal_trade_push_dedupe.add(trade_push_key)
                    if len(terminal_trade_push_dedupe) > 5000:
                        terminal_trade_push_dedupe.clear()

            # B. 异常状态推送 (拒单)
            if order_proxy.is_rejected():
                symbol = order_proxy.data._name if order_proxy.data else "Unknown"
                alarm_manager.push_text(f"⚠️ 订单被拒绝: {symbol} - {msg}", level='WARNING')

            # C. 撤单状态推送（含手动单/隔夜清理单）
            if order_proxy.is_canceled():
                total_qty = _extract_target_qty(order_proxy, fallback=0)

                action = "BUY" if order_proxy.is_buy() else "SELL"
                symbol = order_proxy.data._name if order_proxy.data else "Unknown"
                alarm_manager.push_text(f"🛑 订单已撤销 ({current_status}): {action} {total_qty} {symbol}")

            # 3. 如果卖单成交（有钱回笼），仅同步资金。
            # 无状态模式下不执行延迟队列重放。
            if order_proxy.is_sell() and is_completed and order_proxy.executed.size > 0:
                # 再次确认不是撤单导致的 size>0 (虽然撤单通常 size=0，但为了严谨)
                if not order_proxy.is_canceled() and not order_proxy.is_rejected():
                    print("[Engine] Sell filled. Waiting for cash settlement (1s)...")

                    if hasattr(broker, 'sync_balance'):
                        broker.sync_balance()
                        print(f"[Debug] Cash after sync: {broker.get_cash():.2f}")

        except Exception as e:
            # 记录所有未预期的异常，并确保主循环不退出即可。
            import traceback
            error_msg = f"[Engine Callback Error] Unexpected exception: {e}"

            # 打印醒目的错误日志
            print(f"\n{'=' * 40}")
            print(error_msg)
            print(f"{'=' * 40}")
            traceback.print_exc()

            # 极端防御：如果遇到未知的严重错误，也可以尝试盲调用一次重置
            # 这样即使是代码其他地方写的 Bug 导致状态脏了，也能在下一次心跳前恢复
            try:
                if hasattr(context, 'strategy_instance'):
                    strategy_instance = context.strategy_instance
                    strategy_instance.broker.force_reset_state()
                    if hasattr(strategy_instance, 'strategy') and getattr(strategy_instance.strategy, 'order', None):
                        strategy_instance.strategy.order = None
                        print("[Engine Callback Recovery] Cleared stale strategy.order after force reset.")
            except:
                # 不抛出异常，让程序继续运行
                # 这样下一个 Bar 到来时，Broker 会有机会再次自我修正
                pass

    else:
        print("[Engine Callback Warning] No strategy_instance found in context.")


def launch_live(broker_name: str, conn_name: str, strategy_path: str, params: dict, **kwargs):
    """
    通用实盘启动器
    动态加载 live_trader.adapters.{broker_name} 模块并执行其 launch 方法
    """

    # 1. 配置检查 (保持不变)
    if not hasattr(config, 'BROKER_ENVIRONMENTS'):
        print("[Error] 'BROKER_ENVIRONMENTS' missing in config.py")
        sys.exit(1)

    broker_conf = config.BROKER_ENVIRONMENTS.get(broker_name)
    if not broker_conf:
        print(f"[Error] Broker '{broker_name}' not in BROKER_ENVIRONMENTS")
        sys.exit(1)

    conn_cfg = broker_conf.get(conn_name)
    if not conn_cfg:
        print(f"[Error] Connection '{conn_name}' not found")
        sys.exit(1)

    # 2. 动态加载模块
    module_path = f"live_trader.adapters.{broker_name}"
    try:
        adapter_module = importlib.import_module(module_path)
    except Exception:
        print(f"[Error] Failed to import module '{module_path}':")
        traceback.print_exc()
        sys.exit(1)

    # 3. 自动发现 Broker 类
    broker_class = None
    for name, obj in inspect.getmembers(adapter_module):
        # 查找逻辑：是类 + 是BaseLiveBroker的子类 + 不是BaseLiveBroker本身
        if (inspect.isclass(obj)
                and issubclass(obj, BaseLiveBroker)
                and obj is not BaseLiveBroker
                and obj.__module__ == adapter_module.__name__):
            broker_class = obj
            break

    if not broker_class:
        print(f"[Error] No subclass of 'BaseLiveBroker' found in {module_path}")
        sys.exit(1)

    # 4. 执行协议 (Lazy Check)
    # 这里直接调用，如果用户没覆盖，会抛出基类定义的 NotImplementedError
    try:
        identity = conn_name
        if 'client_id' in conn_cfg:
            identity = str(conn_cfg['client_id'])
        elif 'strategy_id' in conn_cfg:
            # 掘金ID太长，截取前8位
            identity = str(conn_cfg['strategy_id'])[:8] + "..."

        market_scope = _format_market_scope(
            selection=kwargs.get('selection'),
            symbols=kwargs.get('symbols')
        )

        # 这里的 params 就是运行时透传的策略参数
        AlarmManager().set_runtime_context(
            broker=broker_name,
            conn_id=identity,
            strategy=strategy_path,
            params=params,
            market_scope=market_scope,
            schedule_rule=conn_cfg.get('schedule'),
            schedule_timezone=conn_cfg.get('timezone'),
            alarm_window=conn_cfg.get('alarm_window'),
        )

        # 净化 sys.argv
        sys.argv = [sys.argv[0]]

        broker_class.launch(conn_cfg, strategy_path, params, **kwargs)
    except NotImplementedError as e:
        print(f"[Error] Protocol not implemented: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[Crash] Launch execution failed:")
        AlarmManager().push_exception("Launcher Crash", str(e))
        traceback.print_exc()
        sys.exit(1)
