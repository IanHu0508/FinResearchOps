# TradingAgents 与基本面审核接入

进度与实测记录只见 [status.md](status.md)。本文件描述接口、运行方式和限制。

## 两条入口

- `tradingagents-baseline` 使用固定版本的原生图，启用 fundamentals 和 market。它保留上游研究、交易提案及风险讨论，输出原生报告。此报告明确标为未经过本项目财务审核。
- `research-security` 使用精简的 LangGraph 图及 TradingAgents 的模型客户端，先核验盈利与经营现金流，再形成经营分析、独立质疑和综合判断。它是定制产品路线，不是原论文收益复现。

核心仍为标准库 Module；两条入口都经过 `FinResearchOps.handle()`，保存的 Case 都经 `read_case()` 重开。新的 `FundamentalEvidenceTask` 仍经过 `FinAuditGate.run/replay`，复用原有现金流分析，不加载逐题 gold，也不修改旧现金流记录格式。

## 运行环境

从本仓库目录执行，下列依赖只安装到单独的集成环境：

```bash
uv venv --python .venv/bin/python ../tmp/tradingagents-runtime
uv pip install --python ../tmp/tradingagents-runtime/bin/python -r requirements/tradingagents.lock
```

锁文件包含上游固定提交的源码 URL 和解析后的依赖版本。原 `.venv` 与 `pyproject.toml` 的标准库核心依赖不受影响。固定上游来源为 [TradingAgents v0.4.0](https://github.com/TauricResearch/TradingAgents/commit/2448d0a12576f9b2ddcd5980a0630833423d1e1b)。

DeepSeek 密钥可设置为当前进程的 `DEEPSEEK_API_KEY`，或者放在与工作树同级的私有目录中的配置文件，再使用 `--env-file`。文件仅允许 `DEEPSEEK_API_KEY=...` 和注释，须由当前用户拥有且无组/其他用户权限，例如权限 600。不要把密钥放进命令参数、报告或 Git。

## 原生报告

```bash
PYTHONPATH=src ../tmp/tradingagents-runtime/bin/python -m finauditgate.cli \
  --artifact-root <absolute-private-run-root> \
  tradingagents-baseline --symbol NTES --as-of 2026-09-05 \
  --env-file <absolute-private-env-file> --max-spend-cny 50
```

日期应替换为实际研究日期。原生入口会联网获取公开供应商数据，并将所需内容发送给所选模型。默认基本面供应商的历史可用时点未被本项目修复，因此不要用任意历史日期输出宣称无前视回测。

原生策略图没有被改写；外层增加请求次数、输入大小、输出长度、超时、费用预留和私有日志。记忆与 checkpoint 在首个基线中关闭。LangSmith 自动跟踪在运行上下文中关闭，避免将材料额外发送给跟踪服务。

DeepSeek 的 Chat Completions 要求使用 `max_tokens`。当前 SDK 会把该字段改写为 `max_completion_tokens`，因此本地 HTTP Adapter 在真正发送前恢复正确字段、重算 Content-Length，并记录无凭据的实际参数摘要。该修复只作用于 DeepSeek。[官方兼容说明](https://api-docs.deepseek.com/quick_start/agent_integrations/oh_my_pi/)

## 基于已获取财报的研究

```bash
PYTHONPATH=src ../tmp/tradingagents-runtime/bin/python -m finauditgate.cli \
  --artifact-root <absolute-private-run-root> \
  research-security --source-manifest <absolute-acquisition-manifest> \
  --comparison-end 2024-12-31 --cutoff 2026-09-05 --currency CNY \
  --symbol NTES --question '盈利变化是否有持续现金转化支持？' \
  --horizon-months 12 --env-file <absolute-private-env-file> --max-spend-cny 50 \
  --max-output-tokens 16384 --reasoning-effort low --synthesis-effort high
```

来源清单字段及路径要求与 [现金流任务](cashflow-investigation.md) 相同；本入口使用 `--strategy rules`，因为后续附注方向由新的研究步骤提出。调用云模型时，传出的是这份资料中进入研究包的事实、位置和段落；配置费用额度不会替代使用者对材料范围的决定。

后续更新增加 `--previous-case-ref <case-ref>`，并沿用同一个私有运行根目录。旧 Case 必须属于同一来源主体、声明代码、问题和期限，且研究日期不能晚于本次。代码与 CIK 的对应关系尚未独立验证，声明代码只作任务标签，不用于连接行情或每股估值；报告明确展示来源主体。

重开和核心重放使用原有入口，不需要模型环境或密钥：

```bash
PYTHONPATH=src .venv/bin/python -m finauditgate.cli \
  --artifact-root <absolute-private-run-root> inspect-case --case-ref <case-ref>
PYTHONPATH=src .venv/bin/python -m finauditgate.cli \
  --artifact-root <absolute-private-run-root> replay --run-id <core-run-id>
```

## 实际信息传递

1. 首次核心调用核验财务来源，生成事实与披露概览，不执行研究请求的附注补查。核心没有足够的同口径数值时，不调用研究模型。
2. 经营分析与独立质疑按顺序执行，但构造的输入相同：任务与初始证据。第二份初稿不读取第一份内容，也不读取旧 Case。
3. 两份初稿各可提出不超过两个允许的 driver ID。程序按核心已有的财务重要性顺序选择至多两个不同 ID，调用一次补查核心；模型不能选择任意文件、URL 或执行代码。
4. 综合节点只接收核验后的财务视图、补查结果和必须审阅的证据 ID，不接收两份初稿的结论或论证文字。它必须说明指定证据能够和不能支持什么；若只有政策或背景命中，应解释为何不能据此判断本期原因。输出的是判断、假设和后续证据条件，不是无数据支持的多因素预测。
5. 程序校验输出结构、引用集合、补查预算及传递记录，先保存新判断，再读取旧 Case 做字段对照。旧总结不会回流到本轮模型上下文。
6. Case 保存后，重开会重新验证核心记录、上下文传递、结论对照及 HTML 内容，不重新请求模型。

## 数字、判断与费用的限制

本路线目前研究年度合并盈利、经营现金流及其调节项目。收入、债务、资本开支、市场价格和完整估值尚不在新路线的核验覆盖内；输出经营观点，不生成买卖评级、目标价、仓位和止损。

模型的工作方法还包含一般会计约束：同比归因用同比变化额；非现金加回不直接创造现金；预收款可以是正常经常性收款，但合同负债净变动不等于客户总收款；已收预付款对应的未来收入确认不能再次计算为收款；低基数、一次性、现金税变化等判断需要证据。质疑节点重点检查这些关系，而非重复财务摘要。这些约束不规定某家公司的看多或看空答案。[现金流与净利润的关系](https://www.sec.gov/about/reports-publications/beginners-guide-financial-statements)

关键数值表格、现金与利润比率、同比差额及明确披露金额的量纲归一由程序生成。模型只输出定性文字和独立的证据引用数组，不能在文字中复写金额、日期、百分比、比率值或数值预测；常见数值表达会被拒绝。这样可以避免正文把 billion 错写为亿元，或自行编造预测区间。引用存在仍不证明模型的转述、假设或因果解释正确；报告是待人工财务复核的草稿。

披露金额归一只处理带明确目标币种、数字和 billion/million/thousand 单位的文本。记录原文表达、原文段落引用和金额，不自动判定其科目、正负经济含义、适用期间或预测是否实现。现金与利润比率仅在两期利润均为正时显示，也不视为质量评分。

当前研究证据格式为 v2，Case 格式为 v5；旧开发文件及报告保留为历史记录，不作为当前可重开格式。原有现金流任务和原生 TradingAgents 基线格式不受影响。旧论点目前做字段展示对照，不自动认定语义上哪些假设已发生改变。

模型输入采用精简财务视图：明确区分利润/实际经营现金流总额、间接法调节项、经营性资产负债净时点调整；不把所有调节额命名为实际现金影响。原始定位、哈希和完整记录仍由核心保存，模型只得到分析所需的数值、期间、引用 ID 和披露文字。重开时会从核心记录重建该视图并核对模型输入记录。

该视图同时标明没有提供匹配的现金纳税额与所得税费用勾稽，不能从递延税调节项推断二者高低；也未提供投资处置现金流，不能把既往减值视为未来支付义务。这是输入覆盖边界，不是对公司投资方向的预设答案。

初稿仍用于选择补查，并作为可能含错的候选分析保存供人审阅；它们不会作为最终判断的文字输入。这个设计减少一种观点直接传递路径，但不能证明模型固有偏好已消失，补查选择与最终解释仍需要业务检验。

默认模型为 `deepseek-v4-pro`，可明确选择 `deepseek-v4-flash`。产品路线最多三个模型请求，原生路线最多二十四个；SDK 自动重试关闭。产品路线使用 DeepSeek JSON Output，随后本地检查约定结构、引用与文字限制，不再借函数调用承载报告。[JSON Output 官方说明](https://api-docs.deepseek.com/guides/json_mode/)

`--max-output-tokens` 默认 8192，最多 16384；额度包含模型生成的推理与正文。`--reasoning-effort low/high/max` 设置默认推理强度，`--synthesis-effort` 可单独提高最终证据判断的推理强度。未填写则保留服务端默认值。SDK 不传递的 DeepSeek 专用配置由同一 HTTP Adapter 写入实际请求，逐次调用的显式值优先。返回用量超过额度、或回复已截断时，会保留返回并停止后续调用，不能让截断触发额外付费回退。

每个请求在发出前按 UTF-8 输入字节加协议余量、最大输出和配置单价预留费用；另有独立的请求次数和输入上限。`--max-spend-cny` 是单次运行的本地预留上限，不是账户级扣费设置。价格来自已配置的高峰未缓存费率；用量缺失则不显示零费用，估算不等于供应商账单。多个运行的总额度仍需按各自收据累计。

## 验证命令

标准库环境的完整离线检查：

```bash
PYTHONPATH=src .venv/bin/python -W error::ResourceWarning -m unittest discover -s tests
```

实际 LangGraph 的单独集成检查（所有回复与资料均为合成，零 API 调用）：

```bash
PYTHONPATH=src ../tmp/tradingagents-runtime/bin/python -W error::ResourceWarning -m unittest discover -s integration_tests
```

这些检查覆盖来源冲突、未来资料、补查限制、伪造引用、反证遗漏、初稿隔离、旧论点后置及重开完整性；不能替代真实模型报告质量或去偏效果评价。
