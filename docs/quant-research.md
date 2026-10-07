# A股横截面排名、历史近邻与下行风险

量化模块检验过去的量价状态能否预测未来相对表现，并比较全局树模型与历史相似状态检索。
代码、接口和合成检查位于`quant/`；实际执行与结果只维护在[项目状态](status.md)。
NN在此表示nearest neighbours（最近邻），不表示神经网络。

## 共用任务与数据边界

排名目标为下一交易日开盘至第20个后续交易日收盘的参考持有收益，在当天完整合格股票池中的相对位置。
未知结果保留在评分和评价股票池中，已知成员使用完整池的排名区间；未知收益不填零，区间不取中点。
训练按标签终点和实际可得时间剔除未成熟信息，日期等权，尺度转换只使用当次训练历史。

V1的股票身份、历史资格、行情处理、标签、时间切分与XGBoost接口继续保留。
V2/V3消费同一批已准入的历史工件，检索解释、风险预测和融合在独立的可选数值环境中运行。
历史供应商版本、资格覆盖、未知结果和有限计算近似的限制仍适用。

## V2固定近邻与融合

`quant.models.analogues`用紧凑量价/市场输入构建日期平衡的精确历史检索。
每个查询只能使用查询时点之前已成熟的候选，邻居按照距离、日期和证券身份稳定排序，限制同一历史日期的集中度。
近邻预测最小化加权排名区间损失；可保留加权历史收益、亏损频率及定位示例，但它们不等同于未来收益或亏损概率。

`quant.analogue_study`提供缓存准备、计时检查和年度预测；中断后按模型与内容摘要复用已保存预测。
`quant.analogue_evaluation`比较固定距离/邻居数配置与融合权重，保留全部候选、年度结果和负结果。
开发选择和最终历史比较分别解释，默认路径不因某个最终年份成绩而重新选择。

## V3学习距离与独立风险目标

V3使用14个趋势、风险、流动性及市场状态输入。普通近邻是标准化数值距离基线；
RS用排名区间损失学习距离，RD用未来损失分量误差学习距离。
数值特征权重、趋势余弦配比及核带宽通过时间验证确定。
零或缺失趋势向量使用中性形态距离，双方缺失仍计惩罚。

风险目标为`max(-R20, 0)`的条件均值，单位是参考收益率的损失分量。
它不是亏损概率、路径最大回撤或账户损益。普通近邻、RD及同输入风险XGBoost保留相同的校准机会，
同时保存校准前后预测。常数预测的退化校准采用零斜率，不能伪装为有效线性校准。

推理对完整成熟历史库精确检索，保留边界并列，取64个邻居，每历史日期最多8个。
训练查询与候选有明确上限，有限迭代及未收敛尝试留痕；这些计算近似不改变最终评分股票池。

## 半年度折外预测与组合

每个历史半年度块在块首冻结基础拟合、参数选择及风险校准。
后续校准和组合训练只连接截止前已经成熟的折外结果。
G0依据当时市场状态与趋势学习检索配置权重，G1再加入已校准的条件风险。
条件风险不可用时，G1使用G0的分数和邻居权重。重复证券日期合并权重，亏损邻居不会按结果好坏删除。

2018—2022年用于结构和融合选择，2023年固定检查，2024—2025年用于完整历史比较。
这些时期此前已见过，不能重新称为盲测。
评价保留完整池Rank IC识别界、年度/月度、固定市场状态、缺失覆盖、回退、日期集中度及配对HAC20/60。
开发期默认选择与最终增量差异分别判定，保留没有胜出的候选。

## 代码入口

| 模块 | 职责 |
| --- | --- |
| `quant/state_data.py` | 原身份/池不变的14输入缓存、标签可得时间与截止监督 |
| `quant/models/state_metric.py` | 标准化、数值/余弦距离、精确检索、区间损失及距离学习 |
| `quant/models/state_models.py` | 检索与同输入XGBoost模型、内容绑定保存/恢复 |
| `quant/models/state_gate.py` | G0/G1组合、风险校准和条件风险缺失回退 |
| `quant/state_study.py`、`state_meta.py` | 年度执行、历史OOF调度、组合拟合及中断恢复 |
| `quant/state_evaluation.py` | 排名/风险评价、固定选择、共同日期与配对比较 |
| `quant/state_packet.py`、`inference/note.py` | 当前时点评分、成熟邻居及研究说明封包 |
| `quant/state_report.py`、`report_page.py` | 从已保存比较生成中文Markdown/HTML报告 |

报告渲染不依赖未发布的Agent协议。它只读取已保存比较，不训练模型或生成新预测。
核心wheel不包含`quant/`，需要保留完整源码checkout。

## 安装与检查

使用Python3.12，在独立环境安装已有锁定依赖，核心环境保持标准库。
macOS arm64另外需要合法取得的OpenMP动态库；`run_quant.py`在导入XGBoost之前设置库搜索路径。

```bash
python3.12 -m venv ../quant-runtime
../quant-runtime/bin/python -m pip install -r requirements/quant-ml.lock
python3.12 scripts/run_quant.py --python ../quant-runtime/bin/python --check
python3.12 scripts/run_quant.py --python ../quant-runtime/bin/python -- \
  -W error::ResourceWarning -m unittest discover -s quant/ml_tests
```

默认macOS库位置为`<Quant环境>/lib/libomp.dylib`；其他位置通过`--openmp-lib`指定。
标准库合成检查可直接执行：

```bash
PYTHONPATH=. python3.12 -S -W error::ResourceWarning -m unittest discover -s quant/tests
```

拥有已准入的完整study时，报告入口为：

```bash
python3.12 scripts/run_quant.py --python ../quant-runtime/bin/python -- \
  -m quant.state_report --study <完整修订study> --output <新的私有报告目录>
```

预测/研究入口接收显式输入与输出目录，具体参数通过`--help`查看：
`quant.analogue_study`、`quant.analogue_evaluation`、`quant.state_study`、`quant.state_packet`。
真实研究需要已有冻结数据、成员/标签/可得时间、模型和预测工件；仓库合成测试不能替代这些输入。

## 公开与私有材料

公开仓库包含原创量化代码、锁定依赖声明、合成测试、运行入口及经核查的结果摘要。
原始市场数据、逐股预测、真实近邻列表、模型权重、私有路径、原始模型调用和发行人材料继续留在私有工作区。
V1研究的Ridge/GRU阶段拟合脚本也仍属私有历史执行材料；本次公开新增的是V2/V3模块。
公开checkout可以运行合成检查；完整真实历史研究需要自行提供相容且有使用权限的数据工件。
