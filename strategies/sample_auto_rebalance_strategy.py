"""极简全自动轮动样例 SampleAutoRebalanceStrategy：按动量选 TopK，等权调仓。

复制执行。`--params` 与类默认参数一致。

回测:
python run.py sample_auto_rebalance_strategy \\
  --symbols SHSE.510300,SHSE.510500,SZSE.159915,SHSE.511880 --start_date 20230101 \\
  --params "{'selectTopK': 1, 'roc_period': 20, 'rebalance_threshold': 0.05, \\
'rebalance_when': 'daily'}"

实盘:
python run.py sample_auto_rebalance_strategy --connect futu_broker:sim \\
  --data_source futu --symbols HK.00700 \\
  --params "{'selectTopK': 1, 'roc_period': 20, 'rebalance_threshold': 0.05, \\
'rebalance_when': 'daily'}"
"""
import pandas as pd
from strategies.base_strategy import BaseStrategy
from common import mytt


class SampleAutoRebalanceStrategy(BaseStrategy):
    """
    小白专属：极简全自动轮动策略模板

    核心逻辑：每天看一眼所有标的的动量（涨幅），挑出涨得最好的全仓买入。
    亮点：你只需要负责“选股”，算钱、避开理财底仓、下单买卖，全部由框架自动搞定。
    """

    # 策略的初始设置
    params = {
        'selectTopK': 1,  # 每次只买排名第 1 的标的
        'roc_period': 20,  # 观察它过去 20 天的动量（涨幅）
        'rebalance_threshold': 0.05,  # 5% 缓冲带，防止微小波动导致频繁交易
        'rebalance_when': 'daily',  # 若想把现金注入延后到正式调仓日，可改为 weekly/monthly
    }

    def init(self):
        """
        准备阶段：游戏开始前，系统会调用一次这里。
        """
        self.log("策略初始化：预计算动量指标。实盘刷新行情后会在 next() 里按最新 DataFrame 重算。")
        self.roc_signals = {}
        self._sync_roc_signals()

    def _sync_roc_signals(self):
        """按当前行情重算 ROC，并注册到框架指标接口。

        回测通常一次加载完整历史，用 asof(当前K) 取值，不会看到未来。
        实盘引擎会原地替换 data.p.dataname；live 路径不会复用过期指标缓存。
        没有 DataFrame 的测试桩会跳过计算，只把已注入的 roc_signals 注册进去。
        """
        if not hasattr(self, 'roc_signals') or self.roc_signals is None:
            self.roc_signals = {}

        period = self.p.roc_period
        for data in self.broker.datas:
            df = getattr(getattr(data, 'p', None), 'dataname', None)
            if isinstance(df, pd.DataFrame) and not df.empty and 'close' in df.columns:
                def _compute(frame=df, roc_period=period):
                    roc_array, _ = mytt.ROC(frame['close'].values, roc_period)
                    return pd.Series(roc_array, index=frame.index)

                series = self._get_cached_indicator_series(
                    data,
                    'roc',
                    (period,),
                    _compute,
                )
                self.roc_signals[data._name] = series

            series = self.roc_signals.get(data._name)
            if series is not None:
                self.register_indicator(data._name, 'roc', series)

    def next(self):
        """
        执行阶段：回测或实盘中，每一天（或每根 K 线）都会执行一次这里。
        """
        # 获取“今天”的日期
        current_dt = self.broker.datetime.datetime(0)
        if getattr(current_dt, 'tzinfo', None) is not None:
            current_dt = current_dt.replace(tzinfo=None)

        # 实盘 refresh 会更新 dataname；每根 K 都按当前行情同步指标。
        self._sync_roc_signals()

        # ==========================================
        # 第一步：打分选秀（只看可交易的池子）
        # ==========================================
        valid_candidates = []

        for data in self.broker.datas:
            score = self.get_indicator(data, 'roc', current_dt)
            # 只要得分 > 0（代表处于上涨趋势），就有资格进入候选名单
            if score is None or pd.isna(score):
                continue
            score = float(score)
            if score > 0:
                valid_candidates.append((data, score))

        # ==========================================
        # 第二步：排出名次，选出大哥
        # ==========================================
        # 按照得分从高到低排序
        valid_candidates.sort(key=lambda item: item[1], reverse=True)
        self.publish_rankings(valid_candidates, title="ranked_symbols", dt=current_dt)

        # 挑出前 selectTopK 名（按照配置，这里会挑出第 1 名）
        targets = [item[0] for item in valid_candidates[:self.p.selectTopK]]

        # ==========================================
        # 第三步：一键执行，让框架去干脏活累活
        # ==========================================
        # 就像将军下令一样，你只需要指明进攻目标 (targets)。
        # 如果大盘暴跌没有标的满足条件，targets 就是空的，框架会自动帮你清仓防守。
        # 底层框架会自动算可用资金、扣除手续费、换算股数并发单；
        # rebalance_when 会阻止非正式调仓时点因现金注入/波动而立刻补仓。
        self.execute_rebalance(
            target_symbols=targets,
            top_k=self.p.selectTopK,
            rebalance_threshold=self.p.rebalance_threshold,
            rebalance_when=self.p.rebalance_when,
        )
