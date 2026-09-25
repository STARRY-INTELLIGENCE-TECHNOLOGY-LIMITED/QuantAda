# 期权策略开发模板

期权滚动策略不要在 `next()` 中直接遍历全部 `self.broker.datas` 来寻找当前可交易合约。使用框架提供的候选行情入口，离线回测/优化会自动使用按交易日建立的有效报价索引，实盘则保持实时行情语义。

## 最小模板

```python
from strategies.base_strategy import BaseStrategy
from strategies.options.support import (
    iter_option_rows,
    matches_chain_window,
    position_size,
)


class MyPutStrategy(BaseStrategy):
    option_universe = ("PUT",)
    params = {
        "min_dte": 30,
        "max_dte": 45,
        "min_delta": -0.20,
        "max_delta": -0.08,
        "max_spread_pct": 0.15,
        "min_open_interest": 0.0,
        "rebalance_when": "daily",
    }

    def init(self):
        pass

    def next(self):
        current_dt = self.broker.datetime.datetime(0)
        candidates = []
        for data, row, meta, quote in iter_option_rows(
            self.broker, current_dt, {"PUT"}
        ):
            # 这里保留策略自己的持仓、DTE、Delta、IVP 和风险判断。
            if position_size(self.broker, data) != 0:
                continue
            if not matches_chain_window(meta, quote, current_dt, self.p):
                continue
            candidates.append((data, meta, quote))

        # 根据 candidates 生成目标或订单；不要在这里缓存现金、持仓或交易意图。
```

## 读取边界

- 当前交易日候选：`iter_option_rows()`。
- 单个合约当前报价：`visible_row(..., require_current_quote=True, cache_owner=self.broker)`。
- 已确认持仓：`iter_held_options()`、`reserved_underlying_keys()`。
- 同到期保护腿：`held_protective_put()`。
- 历史指标、估值带、底层趋势：直接读取 `data.p.dataname`，使用向量化计算。

`meta` 和 `quote` 只负责标准化数据访问，不替策略决定 DTE、Delta、IVP、资金、保护腿或订单效果。策略仍应声明 `option_universe` 和有限的 `min_dte`/`max_dte`，不要手写静态期权代码作为滚动标的池。

自定义 feed 至少要能由 `parse_option_symbol()` 解析，或提供 `option_type/right/cp/put_call` 与 `strike/expiry` 等标准元数据列。无法识别的合约会被候选入口安全跳过。
