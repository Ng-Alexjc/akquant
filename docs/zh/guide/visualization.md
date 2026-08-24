# 可视化与报告

以下是 AKQuant 生成的交互式回测报告示例。您可以在此页面直接与图表进行交互，查看详细的回测数据。

<iframe src="../../assets/reports/akquant_report.html" width="100%" height="1000px" frameborder="0" style="border: 1px solid #eee; border-radius: 4px;"></iframe>

## 基准对比

`BacktestResult.viz.report` 支持直接传入基准收益率序列：

```python
benchmark_returns = (
    benchmark_df.set_index("date")["close"].pct_change().fillna(0.0)
)
result.viz.report(
    filename="akquant_report.html",
    benchmark=benchmark_returns,
    show=False,
)
```

报告会新增“基准对比 (Benchmark Comparison)”区块，提供累计超额收益、年化超额收益、跟踪误差、信息比率、Beta、Alpha 等指标，并展示策略/基准/超额三条累计收益曲线。

## 结构化 Benchmark Analysis

从当前版本开始，AKQuant 不再只有 HTML 报告里的基准对比区块，还提供可直接给前端、API 或离线分析复用的结构化 benchmark analysis：

```python
benchmark_returns = (
    benchmark_df.set_index("date")["close"].pct_change().fillna(0.0)
)

payload = result.benchmark_analysis(
    benchmark=benchmark_returns,
    curve_freq="D",
)

print(payload["schema_version"])
print(payload["summary"]["annual_excess"])
print(payload["series"][0])
```

返回 payload 主要包含：

- `schema_version`: 数据契约版本
- `available`: 当前 benchmark analysis 是否可用
- `reason`: 当 benchmark 无法对齐或输入非法时的原因
- `benchmark.label`: 基准显示名称
- `summary`: 汇总指标，如 `total_excess`、`annual_excess`、`tracking_error`、`information_ratio`、`beta`、`alpha`
- `series`: 对齐后的逐日序列，包含策略收益、基准收益、超额收益及三条累计收益曲线
- `meta`: 对齐样本数、起止日期、年化因子等元信息

推荐实践：

- 后端负责准备 benchmark 收益率序列并调用 `result.benchmark_analysis(...)`
- 前端直接消费 `summary + series + meta`
- `result.viz.report(..., benchmark=...)` 与前端页面应复用同一份 benchmark analysis 逻辑，而不是各自重新计算

## 导出给前端或归档

如果需要把 benchmark analysis 固化为回测产物，可以直接导出：

```python
result.export_benchmark_analysis(
    path="artifacts/benchmark_analysis.json",
    benchmark=benchmark_returns,
    format="json",
    curve_freq="D",
)
```

也支持 `format="parquet"`，会输出：

- `series.parquet`: 逐点时间序列
- `metadata.json`: 汇总指标与元信息

## LWC 交互式交易复盘

`result.viz.review()` 基于 [TradingView Lightweight Charts](https://github.com/tradingview/lightweight-charts) 生成**离线自包含**的单文件 HTML，在 K 线上标注买卖点，面向大数据量 / 日内的交易复盘。

它与 `result.viz.report()` 是**互补而非替代**：分析类图表（权益曲线、回撤、热力图、归因等）仍由 `report()` 的 plotly 负责；`review()` 只补足「交互式 K 线 + 买卖点」这一场景，适合逐笔复盘成交时机。

```python
# market_data 为单个 DataFrame 或 {symbol: df} 字典
path = result.viz.review(
    market_data=df,
    title="AKQuant 交易复盘",
    theme="dark",          # 初始主题 "light" / "dark"，页面内可即时切换
    filename="akquant_review.html",
    report_url="akquant_report.html",  # 可选：页头进入完整策略回测报告
    show=False,            # True 则自动打开浏览器
)
```

要点：

- 生成的 HTML 内联了 lightweight-charts，**无 CDN 依赖**，可离线打开与归档。
- 页面顶部有**明暗主题切换按钮**，`theme` 参数只决定初始主题；切换时即时重着色，无需重新生成文件。
- 传入 `report_url` 后，页面顶部显示“策略回测报告”入口，可跳转到 `result.viz.report()` 生成的完整绩效报告。
- 多标的行情（`{symbol: df}`）会在页面顶部提供标的切换下拉，`initial_symbol` 可指定初始展示标的。
- 行情列名大小写不敏感，并兼容中文列名（`开盘/最高/最低/收盘/成交量/日期` 等）。
- 日频数据用 `YYYY-MM-DD` 时间轴，日内数据自动切换为带时分的时间轴。
- 面向**大数据量 / 日内**优化:payload 用向量化构建、时间戳自动去重,数万根 K 线也能流畅复盘(这正是相对 plotly 分析图的核心优势)。

完整示例见 `examples/67_lwc_trade_review.py`。

## 本地复盘中心的选股与预测信号

仓库内的本地复盘中心可直接对观察票池和持仓票池运行日线分析：

```bash
python scripts/review_center_server.py --host 127.0.0.1 --port 8765 --root .
```

打开 `http://127.0.0.1:8765/akquant_review_center.html` 后，页面会通过
`/api/pools` 获取以下结构化结果：

- `selection_rank` / `selection_score`：20 日动量、MA20/MA60 趋势、RSI 与量能组成的票池排序；
- `up_probability`：使用最多 360 个历史样本训练的标准化 Logistic Regression 下一日上涨概率；
- `action`：使用分级阈值产生关注、买入、强势买入、加仓、卖出或观望；候选关注为评分 `60`/概率 `50%`，普通买入为 `65`/`54%`，强势买入为 `72`/`58%`，加仓为 `75`/`60%`；
- `stop_price` / `take_profit_price`：基于 ATR 与百分比下限生成的风险参考价；
- `execution_signal`：仅在明确买卖/加仓时返回，可直接映射为 `akquant.signal.Signal` 的字段。

例如把明确触发的建议交给 AKQuant 信号入口：

```python
import requests
from akquant.signal import QueueSignalSource, Signal

source = QueueSignalSource()
payload = requests.get("http://127.0.0.1:8765/api/pools", timeout=30).json()
for item in payload["signals"]:
    if item["execution_signal"]:
        source.put(Signal(**item["execution_signal"]))
```

`execution_signal` 是“可下发格式”，不代表页面会自动实盘下单。生产使用时仍应先在
`trading_mode="paper"` 验证，并按真实账户资金、A 股代码格式和券商柜台能力调整数量。
