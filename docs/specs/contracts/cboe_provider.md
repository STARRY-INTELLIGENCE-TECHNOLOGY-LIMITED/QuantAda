# CBOE 日频指数 Provider

`data_providers.cboe_provider.CboeDataProvider` 读取 CBOE 官方公开的日频指数 CSV：

```text
https://cdn-api.cboe.com/api/global/us_indices/daily_prices/{filename}
```

它只提供历史指数行情，不提供股票行情、期权链、Greeks、买卖盘口或可执行价格。
期权历史继续使用 ThetaData，正股和实盘行情继续使用 Futu/IBKR。
这些指数代码是研究指标，不应直接放入下单标的池；策略应把它们作为独立条件数据，并在期权展开时排除。

## 代码规范

推荐始终使用 `US.<CODE>`。Provider 为兼容已有调用，也接受裸代码、`<CODE>.US` 和
`CBOE.<CODE>`；网页地址中的 `.VIX-US` 不是 QuantAda 标的代码。

### 完整日频 OHLC

| QuantAda 代码 | CBOE 文件 | 用途示例 |
| --- | --- | --- |
| `US.VIX` | `VIX_History.csv` | 标普 500 30 日隐含波动率 |
| `US.VIX9D` | `VIX9D_History.csv` | 9 日短期波动率 |
| `US.VIX3M` | `VIX3M_History.csv` | 3 个月期限结构 |
| `US.VIX6M` | `VIX6M_History.csv` | 6 个月期限结构 |
| `US.VIX1Y` | `VIX1Y_History.csv` | 1 年期限结构 |
| `US.VXN` | `VXN_History.csv` | Nasdaq-100 波动率 |
| `US.RVX` | `RVX_History.csv` | Russell 2000 波动率 |
| `US.VXD` | `VXD_History.csv` | Dow Jones 波动率 |
| `US.VXEEM` | `VXEEM_History.csv` | 新兴市场波动率 |
| `US.VXEFA` | `VXEFA_History.csv` | 发达市场波动率 |
| `US.VXAPL` | `VXAPL_History.csv` | Apple 波动率 |
| `US.VXAZN` | `VXAZN_History.csv` | Amazon 波动率 |
| `US.VXGDX` | `VXGDX_History.csv` | Gold Miners ETF 波动率 |
| `US.VXGOG` | `VXGOG_History.csv` | Alphabet 波动率 |
| `US.VXGS` | `VXGS_History.csv` | Goldman Sachs 波动率 |
| `US.VXIBM` | `VXIBM_History.csv` | IBM 波动率 |

### 只有日终值的指数

| QuantAda 代码 | CBOE 文件 | 用途示例 |
| --- | --- | --- |
| `US.VVIX` | `VVIX_History.csv` | VIX 的波动率 |
| `US.OVX` | `OVX_History.csv` | 原油 ETF 波动率 |
| `US.GVZ` | `GVZ_History.csv` | 黄金 ETF 波动率 |
| `US.SKEW` | `SKEW_History.csv` | 标普 500 尾部风险 |

这些文件没有真实的日内开、高、低字段。为保持 `BaseDataProvider` 的 OHLCV 契约，返回值会将
`open/high/low` 设为 `close`，并设置：

```python
frame.attrs["cboe_value_kind"] == "close_only"
frame.attrs["cboe_ohlc_derived_from_close"] is True
```

为保证写入 `market_cache` 后仍能识别语义，返回表还会保留
`cboe_value_kind` 和 `cboe_ohlc_derived_from_close` 两列。

因此 close-only 指数只能用于收盘指标和状态过滤，不得用其 `high/low` 计算 ATR、盘中波动区间或
成交价。需要这类计算时应等待 CBOE 提供真实 OHLC 数据或使用其它数据源。

## 调用方式

```python
frame = data_manager.get_data(
    "US.VIX3M",
    "20200101",
    "20261005",
    specified_sources="cboe",
    timeframe="Days",
    compression=1,
)
```

Provider 只支持 `Days/1`。同一 Provider 实例对同一指数每天最多下载一次；`CACHE_DATA=True` 时，
DataManager 负责把结果写入 `market_cache`。CBOE 文件是美东日终数据，最新一根通常是前一个已完成的
美股交易日。

需要股票历史优先走 Futu、期权历史走 Theta、VIX 走 CBOE 时，使用混合 Provider 加 CBOE 回退：

```text
--data_source theta+futu,cboe
```

`theta+futu` 内部将正股历史交给 Futu、期权历史和历史链交给 ThetaData；追加 `,cboe` 后，
非期权的 `US.VIX` 等 CBOE 指数在 Futu 无法提供时才回退到 CBOE。单独使用 `theta+futu` 不包含
CBOE 回退，不能保证取得 VIX。

当 CBOE 接管某个指数时，日志会输出 `Fallback active ... using official CBOE daily data. No action is required.`。
这表示前置券商源不支持该指数但回退成功，不是训练失败；只有 CBOE 自身也返回空数据时才需要排查。

## 新增代码的规则

CBOE 文件必须先在 `CboeDataProvider.OHLC_FILES` 或 `CLOSE_ONLY_FILES` 中显式登记，再加入本表和
测试；不得根据用户输入拼接任意文件名。若新增文件不是 OHLC，应保持 close-only 标记，不能静默填充
盘中价格语义。
