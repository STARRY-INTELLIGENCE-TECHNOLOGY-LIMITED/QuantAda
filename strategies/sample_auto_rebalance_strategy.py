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
        self.log("策略初始化：正在提前计算指标，让回测飞起来...")
        self.roc_signals = {}

        # 💡 为什么要在这里提前算？
        # 为了防止“看到未来”的作弊行为，也为了让回测速度提升百倍。
        # 我们在这里一口气用 MyTT 算出所有历史数据的 ROC（动量）涨幅。
        for data in self.broker.datas:
            df = data.p.dataname
            if isinstance(df, pd.DataFrame):
                # 用 MyTT 一行代码算出 20 日涨幅，并按日期存好
                roc_array, _ = mytt.ROC(df['close'].values, self.p.roc_period)
                self.roc_signals[data._name] = pd.Series(roc_array, index=df.index)

    def next(self):
        """
        执行阶段：回测或实盘中，每一天（或每根 K 线）都会执行一次这里。
        """
        # 获取“今天”的日期
        current_dt = self.broker.datetime.datetime(0).replace(tzinfo=None)

        # ==========================================
        # 第一步：打分选秀（只看可交易的池子）
        # ==========================================
        valid_candidates = []

        for data in self.broker.datas:
            try:
                # 拿到这只标的在“今天”的动量得分
                score = self.roc_signals[data._name].asof(current_dt)

                # 只要得分 > 0（代表处于上涨趋势），就有资格进入候选名单
                if pd.notna(score) and score > 0:
                    valid_candidates.append((data, score))
            except:
                pass  # 如果上市时间太短数据不足，直接跳过

        # ==========================================
        # 第二步：排出名次，选出大哥
        # ==========================================
        # 按照得分从高到低排序
        valid_candidates.sort(key=lambda x: x[1], reverse=True)
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
            rebalance_threshold=self.p.rebalance_threshold
        )
