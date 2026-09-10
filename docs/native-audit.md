# 原生链路审核材料接入

这是独立保留的受限审核路线。轻量化主入口见 [thesis-research](thesis-research.md)，
不经过本页的前置审核、财务投影或固定 Hold 兼容输出。

当前完成程度和验证记录只见 [status.md](status.md)。本文件说明原生链路的离线与真实数据入口。

## 怎样接入

调用 `FinResearchOps.handle(RunAuditedNativeResearch(...))`。Application 先通过
`FinAuditGate.run(FundamentalEvidenceTask(...))` 取得来源事实、计算、问题和复核状态，
再把共同研究任务、覆盖和限制组成固定材料交给 Native Adapter。

Adapter 在实例子类的 `resolve_instrument_context()` 返回值中追加审核材料。
它直接调用原生 `GraphSetup` 和 `propagate()`，不替换节点、删除交易员/风险团队，
也没有生产路径中的全局 monkey patch。首片选择基本面和技术入口，保留全部下游决策链。

原生各节点读取共同的证券上下文，所以研究经理、交易员、风险团队和组合经理都直接
得到材料，不必依赖上一个模型是否在摘要中转抄。原生消息清理节点也会保留上下文，
因此某些后续请求会重复包含材料；真实路径记录实际消息、用量和 HTTP 输出参数。

真实路径还通过 `FinAuditGate.run(SecurityMarketTask(...))` 将官方申报的 CIK、ADS
类别/换算比率、证券代码和交易所与报价元数据匹配，并核对最近交易行、价格及市场快照。
日历日期须提供可追溯的核对来源；这不是通用交易日历引擎。行情是 Yahoo Finance
复权数据，指标由固定上游计算，尚不是独立交易所数据核对。

财务工具沿用原生名称、参数和节点，执行原供应商函数并保留原始返回。实例中的
ToolNode 将给模型的返回限定为官方资料已核对的项目；原始供应商 P/E、P/B、预测、
股数及其他未核对期间不能经工具返回进入讨论。模型消息中的返回与核心投影逐条核对，
原始返回、输入、摘要哈希和投影均存于私有运行目录。工具供应商返回可追溯不代表其指标获准使用。

中期真实路径补取合并至归母净利润的带符号调整，包括可赎回少数股东权益增值，
与两期合并利润核对。没有 EPS、汇率和股数换算时不生成估值倍数。缺少组合与执行
约束时，原生交易员仍参与研究，但只能提交待复核提案。

## 证明什么

以实际回调内容核对，不接受单独的“已传递”标记：

- 每个模型请求的消息正文都包含完整、未改动的审核材料。消息 ID 或其他元数据不算正文。
- 全部工作节点的实际输入保留同一上下文；增强前后图节点、边及条件路由与固定结构一致。
- 模型发出的数据工具请求与工具完成记录按 ID、名称和参数逐一对应；错误状态、未完成或整条丢失均拒收。
- 来源限定路径还逐条核对后续模型消息实际收到的工具内容，不能只凭回调收据断言材料已送达。省略的报表频率仅按固定上游默认值归一化，显式频率仍须相符。
- 五份报告与各对应原生节点最后一次输出一致；信号与本片验证的原生结构化评级标题一致。
- 报告独立展示核心 `HUMAN_REVIEW`、分析问题与未覆盖部分。原生信号不会把 Case 升级为批准。

这证明了输入传递与记录的一致性，不证明模型采纳了证据、金融解释正确或消除了偏向。
本片没有调整多空讨论顺序，A/B/C 判断机制对照仍需后续实现。

## 论点校验模式

在真实命令中增加 `--check-judgments`，或构造
`RunAuditedNativeResearch(..., check_judgments=True)`。完成程度与真实验证结果只见
[status.md](status.md)。

该模式在原生实例的节点执行位置接入论点校验，保留原生图、工具、状态工厂及路由。
分析员仍运行原生工具和草稿生成，随后提交结构化论点。其余角色使用统一的举证协议
生成论点，原生状态工厂继续维护历史、轮数及发言者；原来的说服性角色提示不发送给模型。
这属于增强配置，不能冒充未改动提示词的原生基线。

依赖重放对每条引用边检查来源与节点先后关系；缓存命中只复用已核验节点内容，
不能跳过当前这条依赖关系的检查。

财务规则位于 `FinAuditGate.run(ReviewNativeJudgment(...))` 后的核心模块。核心从
两份可重放来源记录生成目录：数值、期间、币种、报表角色、最近行情的具体字段以及
已知缺项。模型只选择来源引用、计量类型与关系；数字和可进入结论的文字由核心生成。

- 事实必须与来源类型一致。余额下降不等于转负；收盘不能改称日内最低价。
- 旧预收收入确认与新增现金的关系单独核验；未知持仓不可改成零持仓。
- 经营持续性、现金质量、合同负债原因、投资损益重复性和价格趋势属于有限的假设主题，必须带相关来源与待核条件；通过类型检查仍然是假设。
- 研究经理逐条裁决多空论点；组合经理逐条裁决研究经理、交易员和风险团队论点。模型选择与核心结果分别保留，未经支持的论点不能被选择采用；漏答会暂缓并记录问题。
- 未校验草稿不进入下游模型或报告结论。原始草稿、结构化请求/返回、拒绝记录和核心引用都保留；重开检查实际输入、实际返回、核心重放及报告内容的一致性。

当前校验的是有限类型的结构化命题，不是任意自然语言语义识别，也不保证草稿中每句
话都被完整抽取。目录没有支持的内容不进入已核对结论。这个取舍减少了自由叙述和
未覆盖业务观点；收入/分部质量、估值与组合数据仍需单独补齐。

本模式写 Case/packet v5，核心论点记录为 `finauditgate.native-judgment/v1`。
报告明确显示投资依据不足；原生 `Hold` 只作为接口兼容字段，不是投资评级或持仓建议。
命令行也将其标成 `native_compatibility_signal`。

外部工具在一次原生执行内串行运行，避免并发初始化 yfinance 的 SQLite/CSV 缓存；
工具种类与调用记录保留。模型和金额预算规则与真实入口相同。

## 重现离线案例

使用已经按 [集成指南](tradingagents-research.md) 建立的独立环境，从仓库根目录执行：

```bash
PYTHONPATH=src ../tmp/tradingagents-runtime/bin/python scripts/native_offline_demo.py \
  --artifact-root <absolute-private-output-directory>
```

增加 `--partial-evidence` 可验证一个合成调整项缺少比较期数值的情况。输出 Case、报告、
核心记录及执行收据路径；所有输出保持在工作树旁的 `private/` 下。

脚本只替换外部模型、数据工具和身份解析入口，使用真实原生节点工厂、ToolNode、路由及
结构化报告渲染。模拟模型故意不在报告中复述审核材料，避免靠模型抄写制造“贯通”假象。
测试运行期间阻断常用外部网络路径，不加载凭据、不产生模型费用。标记
`native_offline=True` 只是测试模型标识，不能单独当作网络隔离证明。

这个演示仅证明工具名/签名与 ToolNode 的合成执行，不产生真实增强报告。
默认 Adapter 仍要求模拟模型标识，真实入口必须显式启用 live 并提供用量控制器。

## 真实入口

先在 `private/` 中准备官方财报及身份申报的 provenance 清单、行情快照与 OHLCV。
`--market-inputs` 使用 `finresearchops.live-market-input/v1`：包含 `quote_metadata`、
`expected_session`、`calendar_source`，以及 `snapshot`/`ohlcv` 两个各带 `filename`
和 `sha256` 的对象。文件名只能指向同目录文件。CLI 检查清单、字节哈希和私有路径；
核心继续检查证券/期间/币种/行情之间的关系。身份入口目前限于英文 Nasdaq ADS 20-F。

```bash
PYTHONPATH=src ../tmp/tradingagents-runtime/bin/python -m finauditgate.cli \
  --artifact-root <absolute-private-output-directory> research-native \
  --source-format interim-html --source-manifest <absolute-private-financial-manifest> \
  --identity-manifest <absolute-private-20f-manifest> \
  --market-inputs <absolute-private-market-manifest> \
  --comparison-end <YYYY-MM-DD> --cutoff <YYYY-MM-DD> --currency CNY \
  --symbol <ADS-symbol> --question <research-question> --horizon-months 12 \
  --env-file <absolute-private-env-file> --max-spend-cny unlimited \
  --max-output-tokens 32768 --reasoning-effort high
```

`unlimited` 取消金额上限；仍记录用量和按配置费率计算的估算，不能当供应商账单。
调用次数、输入/输出上限、超时与截断失败检查独立保留。请求和部分返回即时落盘，
失败也保存收据；图返回后、Application 验证前保留完整 `native-result.json`，为纯收据修复提供重开依据。不会自动反复重试、充值、下单或推送代码。

## 重开与验证

离线记录为 `finresearchops.native-audited-case/v1`，首个真实接入记录为 v2；
来源限定的审核包 v3 用于被拒收的运行，没有形成持久 Case v3；细化符号/税务/期限约束的新路径写 Case v4，同时绑定财务与证券行情两个核心记录。历史交付保留原字节及
原重放行为，不以修正版覆盖首次失败的金融解释。通过同一个 `read_case(case_ref)`
重开，会离线重放核心、重建审核材料并核对所有输入、工具记录和报告。不加载
TradingAgents、LangGraph、LangChain 或云模型即可重开：

```bash
PYTHONPATH=src .venv/bin/python -m finauditgate.cli \
  --artifact-root <same-private-output-directory> inspect-case --case-ref <case-ref>
```

CLI 重开只返回报告位置、信号与用量摘要，避免把全部节点轨迹倾倒到终端。

完整检查使用仓库规定的离线套件，以及独立环境中的 `integration_tests/`。
`test_native_audit.py` 通过 Application Interface 运行真实原生图，并覆盖材料移除、
元数据伪装、内容篡改、工具错误、回调缺失、拓扑变化、报告替换和旧记录重开等情况。

依赖锁文件指定上游提交；运行时检查包版本与图结构。记录中的提交号表示配置的代码基准，
不是对运行机器所加载源码做密码学认证。此片仅验证结构化原生评级路径，自由文本后备评级
路径未在本片支持或验收。
