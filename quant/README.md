# Quant research infrastructure

独立的 A 股日频 Quant V1：用个股 60 日价量路径，以及趋势、风险、
成交活跃度、流动性代理和当时市场状态，预测未来 20 交易日持有期收益的
每日完整股票池排名；标签用partial-identification区间表达未知结果影响。共享模型逐股评分，再对当日预定的完整合格股票池排名。

研究问题是：不同市场状态下，哪些量价路径更可能延续或反转，深度序列表达
能否提供超越人工特征和树模型的样本外增量。财务、估值、行业、历史股本与
稀疏事件不属于 V1 的数据依赖。当前状态只维护在[项目状态](../docs/status.md)。

数据契约、归一化与默认离线检查保持标准库运行；不导入 `finauditgate`、TradingAgents或供应商SDK。
可选XGBoost适配器在独立ML环境中延迟导入其依赖；不引入PyTorch。Agent主流程不会自动读取QuantSignal。
真实数据命令用于采集与研究诊断。运行前请先阅读[适用范围和数据限制](../docs/status.md)；
命令跑通不等于数据通过研究验收。

## 离线运行两个输入对照

在仓库根目录、现有 Python 3.12 环境运行：

```bash
PYTHONPATH=. .venv/bin/python -W error::ResourceWarning -m unittest discover -s quant/tests
PYTHONPATH=. .venv/bin/python -m quant smoke --ablation stock-only --artifact-root ../private/quant/stock-001
PYTHONPATH=. .venv/bin/python -m quant smoke --ablation stock+context --artifact-root ../private/quant/context-001
```

每次使用一个新私有目录，拒绝覆盖原运行。此 smoke CLI 禁用 Python socket 连接；
证券、行情和工作日日历均为合成例，不抓取或训练真实市场数据。

默认 `neighbors` 是可保存和恢复的标准库参考回归器，只检查数据/接口链路，
不代表 XGBoost、TCN/GRU 或有效投资模型。`--model mean` 是常数负向控制，
其 Rank IC 应未定义。`--view tabular|linear|sequence|hybrid` 声明特征视图；
`linear` 提供显式个股×市场交互设计矩阵，不代表已实现线性拟合器。

## 共用 Interface

```python
from quant.pipeline import prepare_dataset, run_experiment
from quant.models.baseline import NearestNeighborsModel

# Synthetic smoke only; real execution requires a frozen admission bundle.
dataset = prepare_dataset(data, spec)  # real label views bind explicit knowledge_cutoff
result = run_experiment(
    data, spec, window,
    NearestNeighborsModel(view="tabular", ablation="stock+context"),
    artifact_root=private_output_directory,
)
```

多个 walk-forward 窗口逐一调用同一 Interface，分别拟合、保存。比较
stock-only 与 stock+context 时使用同一数据集和日期切分。前者完全排除
市场状态、相对收益及交互项；市场预热数据不能被偷偷替换成当前存续股票池。

| Module | 负责的行为 |
|---|---|
| `data/` | 规范化市场记录、显式 BaoStock 原始采集与私有 SQLite 存储；规范化读取不联网 |
| `features/` | 共用 60 日序列、个股特征、历史市场状态及相对特征 |
| `labels/` | 下一交易日开盘至第 20 个后续交易日收盘的持有参考收益及完整池排名区间/知识截止 |
| `splits/` | 日期级切分、label-end/available-at purge、日期等权 |
| `models/` | fit/predict Interface、输入对照、仅训练集拟合的预处理 |
| `evaluation/` | 全池 Rank IC 保守界、日期等权选择、complete-day敏感性；日组合 P&L 独立评价 |
| `artifacts/` | 私有、不可覆盖的运行文件与内容校验 |
| `inference/` | 完整股票池覆盖、两种分位数及时间语义检查 |

输入字段、计算公式和缺失处理见 [CONTRACTS.md](CONTRACTS.md)。
公开信号格式为 [quant-signal.v3.schema.json](../schemas/quant-signal.v3.schema.json)。
“研究 V1”与格式版本不是同一体系：研究输入为v4、canonical store为v9，价格方法不变；目标rank-interval/v2、信号v3。
独立版本避免把旧行业相对实验或缺少已核退出参考的输入误读为当前数据。
旧私有证据保留，当前 reader 明确拒绝不匹配的格式。

## 真实数据与模型边界

[quant.lock](../requirements/quant.lock) 不引入第三方依赖；可选XGBoost使用独立的
[quant-ml.lock](../requirements/quant-ml.lock)。本公共模块不分发私有研究使用的Ridge/GRU拟合器及阶段运行脚本；
研究执行范围、结果与公开复现边界见[项目状态](../docs/status.md)。BaoStock只读采集使用公开匿名协议，
不安装 SDK，不调用用户提供的第三方代理或密钥。

经授权后，原始采集、规范化、固定因子评价分开执行。例如：

```bash
.venv/bin/python -m quant.data.acquisition --root ../private/quant/example/raw --start 2016-07-01 --end 2021-02-26
.venv/bin/python -m quant.data.market_store --raw-root ../private/quant/example/raw --identity-map ../private/quant/example/identity-map.json --quote-choices ../private/quant/example/reviewed-quote-choices.json --liquidation-exclusions ../private/quant/example/liquidation-exclusions.json --output ../private/quant/example/canonical-v9.sqlite
.venv/bin/python -m quant.real_baseline --store ../private/quant/example/canonical-v9.sqlite --panel-review ../private/quant/example/frozen-panel-review.json --output ../private/quant/example/factors --train-start 2017-01-01 --validation-start 2019-01-01 --validation-end 2019-12-31 --test-start 2020-01-01 --test-end 2020-12-31
```

这些命令不是默认 smoke 的一部分。采集单连接串行、主动间隔至少 1 秒，
保留查询参数、实际捕获时刻、响应原字节及 SHA-256；完整缓存可校验续用。
错误不伪装成空表成功，原件不覆盖；未完成缓存不能构建最终 store。
全市场响应使用90秒总接收窗口、单次等待不超过30秒；完整响应不因最终检查
跨过时限而被丢弃。连接中断/超时携带已接收字节，便于私有运行保存诊断。
断在原字节与元数据之间的半份缓存需要检查，不自动删除或重新解释。

身份 JSON 按[契约](CONTRACTS.md#共用证券身份与供应商别名)填写已有证据支持的代码区间。
不传映射时全部代码身份为临时身份，不代表已经通过证券身份审计。
已核别名的等价行在股票池构建前去重；冲突保留并打断参考链。不会覆盖旧 store，
新reader仅接受声明的schema与价格方法；旧文件由归档实现重放。
没有已核查选择清单时省略`--quote-choices`；不能生成一个宽松容差清单绕过数据核查。
可选 `--suspension-repairs` 接受逐条已核全日停牌的零成交重建清单；
`--share-conversions` 接受已实施换股的退出参考清单。两者均须有明确证据，
不会自动抓公告或批量补缺值。退出参考只连接已有持股的终值，不补造开盘进入价。
当前资格通过 `--liquidation-exclusions` 传入已核公告/生效日期，统一排除已经进入退市整理期的
历史成员，并保留此前历史。缺少清单时使用不同的基础股票池ID，不能声称已应用这条规则。
具体日期可得性与覆盖边界见[资格契约](CONTRACTS.md#已知退市整理期的股票池资格)。

SQLite 按稳定或临时证券 ID 保存规范化日线与各日资格，并保留历史交易代码。
`MarketStore.read_day(t)` 统一投影到评分日有效代码，分块提供
同一 `ResearchData`，继续调用 `prepare_dataset`。未知缺行打断参考序列，
跨断点的未来参考结果不可用；未来缺失不缩小评分股票池。
首轮固定因子为 20 日动量、5 日反转、20 日低波动及三者分位数的等权组合，
不训练、不调符号、不按测试结果挑选。股票池、标签、purge 与评价沿用共用实现。
其分数是未校准的百分位代理，不通过训练接口伪装为拟合模型，也不发给 Agent。

每个日期保存样本标签、四项分数、日评价、共用数据集 ID 及压缩文件摘要；
原始 cache、store 元数据和代码版本支持重建。缺失日期与原因保留。
未来结果不全时仍对完整U给出预测和Rank IC保守界，unknown不进监督loss。
完整日期只作敏感性；模型能运行、区间很窄或指标有数值都不能代替数据/身份验收。

真实 pipeline 仍须取得多年日线、明确公司行动收益口径、历史证券资格、
真实交易日历及缺失/停牌/退市处理。财务与行业不再阻塞 V1，
但未验证的价格参考、未来筛股或不完整股票池不能被类型检查修复。

预测评价使用每日完整横截面，并区分预测目标分位与当前模型排名。
分组未来收益不是每日 P&L；在真实执行规则建立前，不产生可交易 Sharpe、
资金曲线或投资效果主张。模型比较使用同一信息集、数据版本、样本与标签。

## 可选XGBoost运行时

在独立环境安装`requirements/quant-ml.lock`，不要改FinAuditGate核心环境。
macOS需要兼容的OpenMP动态库；它可以单独保存在私有运行时的lib目录，并只为该进程设置
`DYLD_LIBRARY_PATH`。本仓库不分发本机运行库或真实数据；依赖和原生库的取得/校验记录保存在私有运行材料中。
应直接启动带该环境变量的Python进程；经macOS受保护的系统程序转发时，变量可能被清除。
需要防休眠时，可让独立的 `caffeinate -i -w <python_pid>` 监视已经启动的进程。

使用`quant.models.baseline.xgboost_model.XGBoostModel`传给共用实验接口；其`fit`和`predict`
与参考模型相同。大型实验可通过`fit_batches`逐日传入经过共用切分校验的训练批次。
独立运行时的离线合成检查为 `python -m unittest discover -s quant/ml_tests`；它不属于零依赖核心检查。
通过合成检查不表示真实面板已通过验收或真实模型已经训练。

磁盘实验入口为 `python -m quant.real_xgboost`，接收store、与其摘要一致的面板核查、
固定日期窗口，以及同源固定因子结果。它按日期批次拟合，并逐日保存验证/测试预测与回读摘要。
`--panel-review`指向冻结包描述符JSON（bundle_root、coverage_view、frozen_dataset_id）；
入口实际验证canonical/规则/代码/coverage/replay及A证据。旧allow-incomplete-dates布尔豁免已移除。
默认开发评价截止2024-01-01；最终测试另需C/D冻结与开启记录，不能用A冻结包打开。
固定因子磁盘入口同样需要冻结描述符。当前真实验收状态见项目状态。
