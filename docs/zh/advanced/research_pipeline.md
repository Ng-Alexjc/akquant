# 本地持仓池研究流水线

复盘中心的研究按钮只使用 `.review_center_state.json` 中的 `positions`、`watchlist` 和 `manual_trades`，不会读取真实账户，也不会向券商下单。

打开 `signal_center.html` 后，可以执行：

- **回测**：对当前持仓池和观察池运行趋势、突破/回踩、动量/市场状态三类基线，并执行 Walk-Forward 参数评估；
- **更新标签**：为已经保存的 AI 分析补齐次日/第 5 个交易日结果；
- **更新指标**：刷新预测指标和训练数据准备状态；
- **训练评估**：训练 Logistic、Gradient Boosting 及多档正则强度的 Platt 校准 challenger；使用按日期分组、带标签清洗间隔的留出窗口，并优先选择同时满足 `AUC >= 0.55`、`Brier <= 0.25` 的候选，不修改线上模型；
- **运行全流程**：依次执行数据质量检查、快照、特征/标签、标签回填、三类回测、Walk-Forward、指标和候选训练评估。
- **每日自动交易**：独立的 `daily_trade` 只刷新当日行情与信号，读取已发布 Champion 参数和当前指标规则后执行本地纸面买卖；不运行回测、训练、优化或模型发布，也不调用 LLM/Agent。组合同时最多持有 4 只标的，按卖出优先、评分、上涨概率、当日池内板块强度排序。若最新行情日期不是当天（周末、节假日或行情未更新），自动跳过下单。
- **模型发布**：打开 `model_management.html` 查看 Champion/Challenger、逐项发布门槛、待审批申请和版本历史；发布和回滚只更新本地纸面模型注册表。

对应 API：

```text
GET  /api/research/status
GET  /api/research/runs?limit=20
POST /api/research/run
GET  /api/automation/status
GET  /api/automation/runs?limit=20
POST /api/automation/trade
GET  /api/models
GET  /api/models/release-gate?model_id=...
POST /api/models/release/request
POST /api/models/release/approve
POST /api/models/release/reject
POST /api/models/rollback
```

`POST /api/research/run` 的 `action` 仅支持 `backtest`、`labels`、`metrics`、`train`、`historical_train`、`optimize`、`full`，可选 `calendar_days`（30～180）和 `refresh`。每次运行都会写入 `research_runs` 表。

定时交易调用 `POST /api/automation/trade`，运行记录写入独立的 `automation_runs` 表。它不会进入 `research_runs`，不会注册、审批或发布任何模型，也不会发起 LLM/Agent 调用。

## P3 优化、CPCV 与发布

优化任务使用 Optuna 4.x 的 TPE sampler。为了同时使用 `MedianPruner`，优化器采用 Sharpe、收益、回撤和交易活跃度组成的保守复合目标；完成的 trial 再按 Sharpe、总收益和最大回撤生成 Pareto 前沿。该方式保留了真实剪枝能力和多目标候选筛选，不再使用确定性网格作为主优化器。

v6 会先将参数网格的首组稳健配置作为可审计基准 trial，再由 TPE 搜索其余空间，避免有限 trial 预算随机漏掉已验证基线。`trend_swing` 与 `breakout_pullback` 共享波动率上限、风险调整动量、ATR 移动止损、最长持仓和过热动量限制；突破回踩另有严格入场模式和趋势确认叠加模式。Pareto 回撤方向已修正为“负值越接近 0 越优”。

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

研究和模型升级按需独立触发。复盘中心服务内置的工作日 19:00 原生调度只执行 `daily_trade`，通过已发布 Champion、指标规则、冲突和人工复核门控后自动更新本地模拟持仓；它不启动 Codex/LLM/Agent。模型升级仍必须经过 Challenger 评估和发布审批。

服务内置调度按交易日期去重并写入独立 `automation_runs`。`scripts/run_daily_auto.py` 保留为命令行/故障恢复入口：它通过交易专用后台 API 创建唯一 `run_id` 并轮询任务；一旦服务已返回 `run_id`，即使后续轮询超时也不会启动第二套任务，从而避免重复交易。

Windows 当前用户登录后会通过 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\AKQuantReviewCenter` 静默启动复盘中心服务，以便内置 19:00 调度在重启后继续生效。该启动项只启动本地服务，不触发研究、模型发布或交易。
