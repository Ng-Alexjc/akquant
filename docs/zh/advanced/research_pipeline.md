# 本地持仓池研究流水线

复盘中心的研究按钮只使用 `.review_center_state.json` 中的 `positions`、`watchlist` 和 `manual_trades`，不会读取真实账户，也不会向券商下单。

打开 `signal_center.html` 后，可以执行：

- **回测**：对当前持仓池和观察池运行趋势、突破/回踩、动量/市场状态三类基线，并执行 Walk-Forward 参数评估；
- **更新标签**：为已经保存的 AI 分析补齐次日/第 5 个交易日结果；
- **更新指标**：刷新预测指标和训练数据准备状态；
- **训练评估**：训练 Logistic、Gradient Boosting 及 Platt 校准 challenger，返回样本外指标和特征重要性，不修改线上模型；
- **运行全流程**：依次执行数据质量检查、快照、特征/标签、标签回填、三类回测、Walk-Forward、指标和候选训练评估。
- **每日自动交易**：`daily_auto` 先运行全流程，再执行本地纸面买卖；买入不足时按卖出优先、评分、上涨概率、板块强度排序。若最新行情日期不是当天（周末、节假日或行情未更新），自动跳过下单。
- **模型发布**：打开 `model_management.html` 查看 Champion/Challenger、逐项发布门槛、待审批申请和版本历史；发布和回滚只更新本地纸面模型注册表。

对应 API：

```text
GET  /api/research/status
GET  /api/research/runs?limit=20
POST /api/research/run
GET  /api/models
GET  /api/models/release-gate?model_id=...
POST /api/models/release/request
POST /api/models/release/approve
POST /api/models/release/reject
POST /api/models/rollback
```

`POST /api/research/run` 的 `action` 支持 `backtest`、`labels`、`metrics`、`train`、`optimize`、`full`、`daily_auto`，可选 `calendar_days`（30～180）和 `refresh`。每次运行都会写入 `research_runs` 表，保存输入股票池、开始/结束时间、状态和结果摘要。

## P3 优化、CPCV 与发布

优化任务使用 Optuna 4.x 的 TPE sampler。为了同时使用 `MedianPruner`，优化器采用 Sharpe、收益、回撤和交易活跃度组成的保守复合目标；完成的 trial 再按 Sharpe、总收益和最大回撤生成 Pareto 前沿。该方式保留了真实剪枝能力和多目标候选筛选，不再使用确定性网格作为主优化器。

trial 数据保存在：

```text
research_artifacts/optuna_trials.sqlite3
```

study 名由数据版本与策略名组成。重复运行相同数据版本时会 `load_if_exists` 并只补足尚未完成的 trial，因此进程中断后可继续执行。

CPCV 将有序交易日切分为连续组，枚举所有测试组组合。每折会：

1. 删除标签窗口可能触达测试区间的训练样本；
2. 删除测试区间后的 embargo 样本；
3. 将不连续训练段和测试段独立回测，防止持仓跨越隔离边界；
4. 保存训练、测试、purge、embargo 的索引和日期范围。

默认发布门槛包括：至少 10 个完成 trial、至少 3 笔交易、CPCV 有效、CPCV 平均测试 Sharpe 不低于 0、正收益测试折比例不低于 50%、PBO 不高于 0.5，以及相对当前 Champion 的 Sharpe/回撤保护。审批时会重新计算门槛；未通过时只能在管理页勾选强制批准并填写原因。模型身份和申请状态不能强制绕过。

批准发布会归档旧 Champion、保存整个模型注册表快照并记录父版本。管理页可一键回滚到父版本，也可指定历史注册版本。所有发布申请、审批决定、发布和回滚记录均保存在本地 SQLite 数据库。

每次 `backtest`、`labels`、`metrics`、`full` 运行还会在项目根目录 `research_artifacts/` 生成同名 JSON 工件和 HTML 摘要；若环境有 `pyarrow`/`fastparquet`，会额外生成权益曲线 Parquet。工件内含数据版本哈希、特征/标签版本、数据质量和未来函数检查结果，可据此重放实验。

页面切换默认读取 `.review_center_pool_cache.json` 和 `.review_center_backtest_cache.json`，不再强制刷新外部行情。只有“刷新队列”、显式研究任务和每日 19:00 自动流程才访问行情源；本地持仓或交易状态变化时缓存自动失效。

建议先人工点击“运行全流程”积累数据。现有交易日 19:00 自动任务会执行 `daily_auto`，通过冲突和人工复核门控后自动更新本地模拟持仓；模型升级仍必须经过 Challenger 评估和发布审批。
