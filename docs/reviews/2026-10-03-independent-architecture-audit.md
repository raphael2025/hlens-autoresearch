# HLENS 独立架构、研究逻辑与可维护性审查

日期：2026-10-03。范围：Phase 1 重构前审查；不实施架构变更、不改冻结契约、不调整验证阈值。

审查基线：`e8e3a4260ef2593110a7087e4bd970a4bd0888c3`，包含本次审查期间另一会话提交的 ADR-0111、REFACTOR_TARGET 与 Action Plan。Python 实现与开始审查时的 `ed3d1b9a` 相同。下文对新方案的批评已基于这些正式文件，不能视为对旧聊天摘要的审查。

## 1. 结论

**维护困难有客观依据。当前最严重的问题是研究闭环尚未完成，平台规模和维护负担已经远超当前需要；同时存在计算复杂度、交易语义与研究验证方法上的具体风险。不能把它们统一归因为“状态爆炸”。**

推荐保留“冻结未来阶段、建立小型列式研究路径”的方向，但在执行 ADR-0111 的新路径前补齐 PIT、数据版本、现货现金约束和复现定义；在批量归档前核对依赖闭包，并保留独立参考计算的测试价值。

现有项目不是全部无效：领域边界、确定性校验、失败实验记录、OOS 开封登记、成本测试、信息流测试和独立参考回测器都有复用价值。大量测试通过，说明实现对既定规则的一致性较好；规则是否适合实际研究，仍需单独证明。

**没有发现足以证明“大量硬编码行情阈值导致策略已经过拟合”的证据。** 大量阈值已有 Spec 和哈希绑定。确有 Regime 硬开关和组合搜索风险，但 148 份 Schema、生命周期状态机、解析器分支本身不等于 148 种行情状态，也不能由 `if` 数量推导样本外必然失败。

## 2. 审查方法与边界

- 全仓跟踪 Python 文件的 AST、规模、模块导入关系扫描；按依赖检查拟归档模块。
- 深读数据解析/发布/版本选择、特征/状态/事件 runner、策略与路由、回测、验证统计、实验登记与持久化、API/worker 边界和前端构建配置。
- 阅读项目权威文件、研究宪法、相关架构文档与 ADR；审查 ADR-0111 的新设计及迁移计划。
- 实跑选定的合同、因果性、策略、回测、验证与跨阶段测试，数据/PIT 集成测试，以及 lint、类型检查、前端测试与构建。运行记录见 §9。
- 用合成小样本复现性能增长、负现金/卖空语义、有效样本量和归档导入问题。性能探针不读取行情、不启动旧 K 轴探针。

这不是逐行形式证明，也不是实盘或策略收益验收。未跑完整历史数据回填、生产 PostgreSQL Catalog 验收、全部 Python 测试或浏览器联调；无真实市场样本外有效性结论。检查到的本地行情为 BTC/ETH 各两日的四个 K 线归档 ZIP，不能据此推断其他磁盘或历史 Catalog 从未存过其他数据。

### 规模快照

统计为跟踪 `.py` 文件的物理行数，含注释和文档字符串；不把行数当作质量评分。

| 区域 | 文件 | 行数 |
|---|---:|---:|
| apps | 32 | 6,918 |
| core | 35 | 10,729 |
| infrastructure | 161 | 73,662 |
| plugins | 37 | 8,847 |
| research | 128 | 48,948 |
| 生产 Python 合计 | 393 | 149,104 |
| tests | 491 | 148,116 |

导出模型注册表为 148 项。单文件热点包括 `research/loop/durable.py` 4,090 行、`infrastructure/canonical/normalizer.py` 3,562 行、`infrastructure/dataset/builder.py` 2,558 行。问题在于跨阶段生命周期、兼容性、存储与研究职责同时需要维护，不能仅靠拆小文件解决。

## 3. 优先处理的发现

P1 表示会阻碍当前研究闭环，或足以使新路径的结果含义/可信性发生变化；P2 表示后续维护或重新开放对应模块前需要处理。不是实盘事故等级。

### F1 — P1：重复重放历史导致平方级计算量，换读取引擎不能单独解决

证据：`infrastructure/feature/runner.py:83` 的 `run_feature` 对每个时点重新截取可见前缀、构造请求、调用 provider 并校验；`core/contracts/feature.py:244,261` 对前缀序列化/哈希、过滤/排序；`plugins/features/bars.py:104,172` 再构造历史 bar 序列。单行缓存没有消除前缀总长度随 T² 增长的问题。

相似模式存在于 state/event runner，以及 `research/strategies/zscore_reversion.py:127,177`、`research/strategies/donchian_breakout.py:126,173` 的逐时点历史重放。`research/validation/stats.py:56` 的 `ordered[index + 1:]` 即使很快 break，也已复制整个后缀。

实测：现有 `BarVolumeSumProvider.spec(10)`，常数成交量、连续合成分钟 bar，检查前 9 个值为空、之后均为 100：

| T | 耗时（秒） | runner 输入前缀总行数 |
|---:|---:|---:|
| 250 | 0.098855 | 31,375 |
| 500 | 0.433251 | 125,250 |
| 1,000 | 1.808367 | 500,500 |
| 2,000 | 7.445337 | 2,001,000 |

每次翻倍约需四倍时间。500 行的独立 cProfile 样本中，内容哈希约占 0.351 秒、bar 重建约 0.238 秒、Pydantic validate 约 0.134 秒；这些累计时间有嵌套，不能直接相加。首要问题不是单独的 Decimal 或 Pydantic，而是重复做了随历史长度增长的工作。单次测量且有并行测试负载，不作为生产吞吐承诺，不外推全年耗时。

建议：受信内置特征改为一次排序、一次批量计算；路径依赖仓位用单次递推；边界验证数据批次、Spec 和结果。保留小样本参考计算与因果性测试。验收检查增长曲线、时间对齐与结果，而不只检查是否出现 Polars 或 Python `for`。

### F2 — P1：新方案的单轴过滤不足以替代既有双轴 PIT

证据：`REFACTOR_TARGET.md` §3 仅列 `available_time`，入口 `load(..., asof)` 使用一个全局截止时点；ADR-0111 “后果”称写入断言、asof 与截断不变性提供等价保障，但没有定义修订版本在何时进入研究者知识集合，也未明确每个决策时点的可用性传播。

两个不同的泄漏路径：

1. 在结束日期加载全部数据，只过滤 `available_time <= asof`，早期决策仍可能用到晚于该决策时点的数据。逐时点因果性仍须由运算、对齐和输入依赖保证。
2. 后来收到的历史更正具有旧事件时间；若重新赋值为“事件时间 + Δ”，历史决策就能读取后来才知道的修订。对这份已更正数据做前缀测试仍可能全部通过。

建议首个切片明确限定为**固定、不可变数据版本下的历史研究**：记录来源 CHECKSUM、下载/获知时间、数据版本、availability 假设；至少在 manifest 层保留 knowledge time，并强制数据版本符合运行的 knowledge cutoff。第一版拒绝混合修订和版本冲突，可以不实现完整修订图，但不能取消知识时间轴。由于历史公开时刻仍依赖 Δ 假设，不把它宣称为历史实际可见信息的完整重建；也不能宣称已支持旧选择器全部能力。

还需明确：特征可用时间不早于其所有输入的最晚可用时间；按 available time 截断、延迟数据和修订数据分别测试；禁止不同版本文件通过通配扫描混入同一运行。

细节：现有 Binance parser 将 K 线原始 close time 解释为区间最后一 tick，不能直接等同于独占的 bar 结束时间。新定义须使用规范化区间终点再加 Δ。分钟聚合的 aggTrades 也应在聚合区间结束及声明的到达延迟后才可用。

### F3 — P1：新数据布局和复现元组仍有身份缺口

证据：`REFACTOR_TARGET.md` §3 固定 `part-0.parquet` 与 R8“更正写新文件”没有明确协调；原子 rename 保证发布完整性，不自动保证不覆盖。默认仅核对大小与行数、`--verify` 才核对 SHA256，无法发现相同尺寸的内容替换。§5 的 symbol、时间范围、asof 是独立输入，但没有明确要求全部进入 Spec 哈希。

建议：

- 文件路径含内容摘要或不可复用版本；发布采用拒绝覆盖/同内容幂等的规则，manifest 最后发布。只从 manifest 中的明确文件清单读取。
- dataset ID 定义为规范化 manifest 正文哈希，明确排除其自身 ID、规范字段顺序、文件排序与时间格式；bars 与 trades 的依赖都进入根清单。
- 可信研究运行验证文件内容摘要，可在同一不可变读取生命周期内缓存；快速 listing 与已核验的研究运行区分开。
- 运行前保存完整解析后的 RunSpec：symbols、区间、asof/knowledge cutoff、全部数据版本、特征与聚合语义、availability 政策、资金/成本/成交模型、参数、训练区间及拟合结果来源、随机种子、代码与依赖环境。
- 未提交的代码不能仅用 git HEAD 标识；保存补丁摘要或要求研究运行使用干净提交。run ID 与确定性 Spec 哈希分开，每次尝试及失败均保留。
- R5 的“按位一致”应声明平台、线程/归约方式等适用环境。float64 不妨碍同环境确定性，但不能仅凭依赖版本承诺所有 CPU/平台的浮点结果完全一致。

这些是恢复可信实验身份所需的少数边界，不需要复制完整 Catalog 或新建一套通用 schema 平台。

### F4 — P1：通用回测器允许负现金和卖空，不能直接解释为无杠杆现货结果

证据：`plugins/backtest/bar.py:21` 明确允许负现金，`_simulate` 根据扣费前 equity/reference price 算目标数量；`research/strategies/time_series_momentum.py:58` 默认 `long_only=False`。新方案示例 `tanh` 趋势乘其他因子也可产生负仓位。部分组合器已有 SPOT 检查，但并未统一覆盖回测入口。

复现：3 根平价 100 的 bar，初始资金 1,000，手续费 0.001，滑点 0.0005：目标权重 +1 买入 10，首次成交后现金 -1.5005，末值 998.4995；权重 -1 卖出 10，现金 1,998.5005，末值 998.5005。这是当前通用模型的既定行为，不是被测算术偶发错误。

建议切片固定“无融资现货”：持仓非负、订单金额加费用不超过现金、多个标的共享预算；卖出不超过持仓。负的预测分数可以保留，但到可交易仓位的映射必须显式。若选择允许融资，应换成相应研究范围并建模借款/融券成本，不能仍称普通现货。

验收至少包括：下一可成交时点、费用后的现金守恒、多标的同步调仓、缺失 bar、最后未执行信号、零信号/买入持有/常价成本基准。bar 级成交只是研究假设，不能由 1m 数据证明逐笔可成交性。

### F5 — P1（重新开放正式验证前）：有效样本量实现需要独立校准

证据：`research/validation/stats.py:39` 将所有重叠持有区间的连通分量数定义为 effective sample size；`research/validation/pipeline.py:439` 用它执行样本门槛。每分钟生成一条、共 1,000 条标签时，持有 1 分钟得到 1,000；持有 2 分钟或 15 分钟均得到 1，因为连续重叠把整条历史连起来。

这是已文档化的保守计数，并非实现偏离测试；但它不能自动解释为一般意义的统计有效独立样本量。另一方面，不重叠也不保证没有序列相关。只按持有重叠决定 HAC lag 同样需要检验。

建议先定义统计单位：独立交易事件、持仓 episode，还是重叠收益窗口。使用已知空模型及注入效应的合成样本校准误报率与检验能力，之后才冻结 Profile。**不得根据某策略能否通过来调宽门槛。** 当前 Profile/晋升路径未完成，不等于已经实证证明全部策略没有 alpha。

### F6 — P1（归档执行前）：迁移清单遗漏依赖，且会提前移走有用的验证参照

证据：`plugins/backtest/__init__.py:32` 无条件导入拟归档的 `reference.py`，却未列入 REFACTOR_TARGET 的同步修改清单。用 import hook 模拟该模块缺失，导入保留的 `BarBacktester` 即报 `ModuleNotFoundError`。

Action Plan 的 A1 移走 research，但 A4 才处理的 `tests/infrastructure/tools/test_registry_audit.py:36`、`tests/infrastructure/migration/test_event_bus_migration.py:48` 仍导入 research。因此现有批次清单不能直接保证“每批全绿”。计划已有遇到依赖重划边界的兜底，但这些已知依赖应先进入批次设计。

Action Plan 风险表还写“新路径不与旧结果比对”。float 与 Decimal 不应强制逐位相同，却仍应比较共同适用域下的仓位时序、成交数量、成本及净值，使用事先说明的数值误差标准。独立参考实现应保留为测试参照，不能因为归属 Phase 14 就失去验证价值。

建议：先生成 retained 模块、包 re-export、entry point、动态 import、fixture 的依赖闭包；将依赖一起移动，或显式保留参考闭包。先做闭环验收，再批量收缩目录；冻结开发可以立即执行。新 archive 禁导入检查不应只匹配字面上的 `import archive`，还要发现旧路径残留。

### F7 — P2：语法禁令不能限制研究自由度，连续乘积也可重新制造组合复杂度

证据：`research/strategies/composite.py` 的状态 gate 和 `research/router/router.py` 的标签路由会硬切仓位。REFACTOR_TARGET S1/S7 改用 AST 数值字面量检测；阈值放进变量、数组比较或 `where` 即可绕过，且不能控制平滑函数数量和交互。

建议保留“优先连续、参数预登记”，用研究对象复杂度约束补充：本轮假设、自由参数数量、特征交互深度、搜索次数、消融结果、参数邻域稳定性、切换造成的换手与成本。提高 tanh 斜率可逼近硬阈值；多个连续因子相乘仍引入交互。Ridge 或 HMM 也不是自动防过拟合措施，首个闭环不需要新增状态模型。

允许必要的缺失值处理、预算上限与风险约束；控制面状态机、解析拒绝分支继续按确定性逻辑实现。不要把“代码没 if”当作统计验收证据。

### F8 — P2：插件声明与实际安装方式不一致

证据：`pyproject.toml:104` 设置 `tool.uv.package=false`，但注册了多组 project entry-points。当前环境中 feature group 的 `importlib.metadata.entry_points` 和实际 discover 均返回 0。现有显式 provider allowlist 可以工作，但不能据 entry-point 配置断言插件发现已部署。

uv 官方说明，非 package 项目不会作为项目包构建/安装；entry point 需要相应构建配置。重新开放插件机制时二选一：正确打包安装，或明确用受信静态注册表。首个切片建议直接组合少量已知函数，不增加插件框架。参见 [uv 项目配置](https://docs.astral.sh/uv/concepts/projects/config/)。

### F9 — P2：新探索模式与仍有效的宪法/Profile 义务尚未完全划界

证据：REFACTOR_TARGET 冻结依赖 Profile 的若干条款，但仍保留 C-P；Constitution C-P1/C-P4 要求绑定 Profile 并可恢复验证规则，A2/C-A3 同样要求运行前绑定。切片声明不产生晋升结论是正确的，但运行是否属于这里的正式 Experiment 仍需明确。

建议定义“管线诊断运行”：保存输入、参数、失败记录和数据来源，可输出描述性指标，不出具正式验证/晋升判断；之后进入正式实验时必须绑定已冻结 Profile 并遵守原规则。该边界需在批准的 ADR 中明确，不要让实现者自行解释为宪法自动豁免。

## 4. 其他架构判断

### 数据层：赞成缩小范围，但避免再次建立自制数据库

Iceberg 当前的困难真实存在：`infrastructure/catalog/iceberg_adapter.py` 对 PyIceberg 0.12.0 做版本门禁并使用内部实现；保留全部 snapshot 的复现/幂等设计使 metadata 随历史单元累积。这不是“只有两个 symbol 所以数据库一定错”，实际负担还取决于交易量、写入频率、版本和查询模式。对现阶段固定本地研究集，维护成本确实高于收益。

建议先用 Polars 承担列式 ETL/研究；需要 SQL 时再引入 DuckDB，NumPy 仅在必要的数值核使用。Parquet 文件、清单、一次性内容验证和运行目录足以起步；不引入自研 Catalog、复杂锁服务或完整 revision DAG。若第一版排除并发写入与修订合并，写清这个范围并拒绝冲突输入。

“直读 Parquet = 零拷贝极速”不准确：扫描仍要读盘、解压和解码，Arrow 与 Polars 部分共享内存路径可减少拷贝。性能取决于投影、分区、排序、文件大小和查询。官方依据：[DuckDB Parquet 扫描/下推](https://duckdb.org/docs/lts/data/parquet/overview)、[Polars 与 Arrow 互操作](https://docs.pola.rs/user-guide/misc/arrow/)。

aggTrades 应按日流式归一化并聚合成可复用的分钟特征，每个策略实验不必反复扫描全年逐笔数据。Binance aggTrades 是合并成交记录，不能替代逐笔订单簿；现有 K 线本身也含主动买入量，可用于第一步验证数据到信号链路。[Binance 官方市场数据定义](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market)

### 测试与控制面：保留关键保证，缩减当前需要理解的范围

148 份契约主要反映平台范围，不应仅按数量删除。首个切片可以不依赖阶段外契约，保留少数清楚的数据边界；Pydantic 适合校验 Spec、manifest 和结果，不适合对每个历史前缀重复构造逐行对象。“边界只校验两次”也不应成为跳过数据身份或批次不变量检查的理由。

`research/loop/dataset_operator.py:237` 已有 run/config/catalog 操作入口，并因冻结 Profile 等前置条件拒绝运行。因此“没有任何入口”过于绝对；准确表述是“没有已验收、可直接复现的真实行情研究闭环”。名为 real_data 的部分研究集成测试明确使用模拟来源生成的 Binance 格式样本，也不能作为真实市场落地证据。

现有测试擅长合同一致性、哈希、拒绝路径与状态恢复；新投入应优先覆盖真实数据质量、数值基准、时间可见性和运行解释。API/控制台可冻结为历史参考，首个闭环只需 CLI 和报告。当前前端测试与构建可通过，不代表需要继续扩展控制台。

worker 预算计量对声明值/报告值和实际耗时有区分，但不能代替进程级超时或资源中止。未来恢复无人值守运行时再补对应保障，当前不为此重建调度平台。CI 尚未配置；切片稳定后添加最小边界与数值校验流程即可。

## 5. 建议的目标结构

```text
官方归档 + CHECKSUM
        ↓ 严格解析、时间单位/主键/覆盖检查
不可变 Parquet + 固定版本 manifest
        ↓ 核验清单、选择版本/范围、明确 availability 假设
一次批量特征计算
        ↓ 因果性、训练区间、参数来源检查
目标仓位 → 现货资金约束 → 下一可成交时点的模拟
        ↓
结果/仓位/成本明细 + 完整 RunSpec + 成功/失败运行记录
```

一个 CLI、少量按职责分开的模块即可：ingest、lake/manifest、features、simulator、run/report。`mvp_slice.py` 作为入口，避免最终长成新的数千行总控。只为真实出现的第二种需求增加抽象。

必留：输入时间与数据版本、费用/现金/成交时序、实验身份与失败历史、独立数值参照。暂缓：LLM 自动研究、知识库、策略生态/路由、晋升/演化、跨引擎适配、通用 API/Web 控制台。保留旧代码与记录的可恢复版本，活跃开发默认不依赖它们。

## 6. 实施顺序和完成标准（提案，未执行）

| 步骤 | 做什么 | 可检查的完成标准 |
|---|---|---|
| 0. 补规格 | 在现有 ADR/目标文档补齐 F2/F3/F4/F9，修正 F6 批次依赖；不新建第二套总规范 | 明确数据版本语义、现货资金、完整 RunSpec、诊断/实验边界；导入闭包可解释 |
| 1. 小样本闭环 | 先复用现有四个 K 线归档，完成读取到报告；再接 BTC/ETH 各一日 aggTrades | 一条命令运行；结果能追溯到文件和参数；记录失败；不需 DB/worker/Web |
| 2. 正确性 | 用小样本与独立参考做比较，再做未来输入/修订/延迟/缺失数据扰动 | 未来数据不改变过去决策；同适用域数值一致；现金/费用/仓位守恒；全部质量问题有拒绝或显式政策 |
| 3. 代表性规模 | 分步扩大到预登记月份和所需分钟历史；逐日聚合 aggTrades | 报告冷/热缓存、行数/文件数、环境、耗时和峰值 RSS；重复测量增长曲线 |
| 4. 收缩维护面 | 按模块与测试的依赖闭包原子归档，保留参照实现与历史记录 | 保留代码不依赖归档；每批有效测试绿；能从恢复点还原；无残留入口 |
| 5. 正式研究 | 管线稳定后，另行校准并冻结验证 Profile，再进行受控假设研究 | 登记试验次数；固定训练/OOS 边界；报告成本与基准、参数稳定性；不由单次盈利判定有效 |

当前 100ms/5s 指标尚无目标机、真实文件布局下的测量依据。建议先约定“研究者十分钟内完成一次有记录、可解释的诊断闭环”，再用首次真实数据测量设定子步骤预算；这里的十分钟是交互目标，不代替统计验收。记录算法增长和绝对资源使用，避免再围绕未校准的毫秒指标展开平台工程。

两天数据只能证明流程可用，不能用于策略有效性结论。扩展数据的先后按研究需求决定，不要求第一步就下载一整年 aggTrades。

## 7. 防止再次变大、变乱

1. 每次迭代只服务一个可运行的研究问题；没有现实调用者的通用框架不进入活跃目录。
2. 每个实验把“假设是什么、输入是什么、什么结果会否定它”写入 RunSpec/说明；先定成本、切分和参数，再看结果。
3. 控制自由参数、交互与尝试次数；保留失败。修改变量名、改成连续函数不会重置试验次数。
4. 新特征先独立做因果性、缺失数据、单位与数值基准检查，再进入组合；先比较简单基准再增加复杂性。
5. 一个职责一个权威位置：规则在 Constitution/ADR，当前执行目标在 REFACTOR_TARGET，状态在 STATUS。报告只留证据，不再成为第二份指令。
6. 每个新增运行模块必须说明其现有用户、替代它的简单方案、验证方法和退出条件；不要用行数阈值或 Schema 数量替代判断。
7. 让人能够追踪“一条输入如何形成一笔模拟成交及最终收益”。这应成为可维护性验收的一部分。

## 8. ARCHITECTURE_DECISION_REQUIRED

以下是审查提出的实施前决策，**没有修改已接受的规则，也没有代替 Owner/Lead 作决定**。记录依据为 `CLAUDE.md` §7；审查本身已经完成，不因这些待决策项停止提供分析。

| ID | 冲突/问题 | 选项与建议 | 不决定的影响 |
|---|---|---|---|
| D-AUDIT-PIT | 新读取方案缺 knowledge 版本语义，却声称等价 PIT | 完整修订选择，或固定版本下的双轴约束并拒绝版本冲突；推荐首个切片选后者，明确 availability 假设 | 不应验收为原双轴等价保障 |
| D-AUDIT-SPOT | signed 仓位/负现金与现货范围不一致 | 无融资现货，或显式扩大到融资模型；推荐无融资现货 | 回测净值的经济含义不明确 |
| D-AUDIT-RUN | 不可变发布、复现输入、Profile 义务存在缺口 | 明确完整 RunSpec 与诊断运行边界，修订相关 ADR；推荐采纳 | 同身份可能对应不同运行/数据；规则解释不一致 |
| D-AUDIT-ARCHIVE | 阶段归档顺序不匹配实际依赖，参考引擎价值未保留 | 按依赖闭包重排批次，保留参照；推荐切片验收后批量归档 | 批次导入失败，或丢失新路径的独立校验 |

涉及 REFACTOR_TARGET 红线/范围或宪法解释的变更按现有授权交由 Raphael；批次和工程细节由现有 Lead 留下正式决定。F5 留待正式验证重新开放前处理，不能借此次切片修改门槛。

## 9. 验证记录

所有检查使用仓库已有 Python 3.13.15 虚拟环境；前端复用已有 node_modules，没有安装新依赖。测试对象为上述基线；审查分支仅新增报告/证据及待决策索引。

| 检查 | 实际结果 |
|---|---|
| 关键研究/合同/边界测试 | `1624 passed in 533.75s (0:08:53)` |
| 数据首切片、版本重放、red-team | `134 passed, 1 skipped in 1265.78s (0:21:05)` |
| 报告新增后的文档/架构检查（上组覆盖项的复跑） | `31 passed in 1.00s` |
| mypy（配置的生产和测试目录） | `Success: no issues found in 884 source files` |
| ruff check（新增审查文件后） | `All checks passed!` |
| ruff format --check（新增审查文件后） | **退出 1**：`1 file would be reformatted, 1160 files already formatted` |
| 前端 test:lib | `tests 135` / `pass 135` / `fail 0` |
| 前端 test:components | `tests 127` / `pass 127` / `fail 0` |
| 前端 build | 退出 0，TypeScript 检查通过，Vite `built in 2.32s` |
| 审查语义探针 | 退出 0；复现 F4/F5/F6，结果保存于证据 JSON |

Python 两组共 **1,758 passed、1 skipped**；31 项复跑不重复计入。跳过项为未设置 `HLENS_TEST_CATALOG_URI` 的 PostgreSQL 集成变体，不构成 PostgreSQL 验收证据。格式问题来自基线新增的 `REFACTOR_TARGET.md:95` Python 示例空格对齐，本审查未修改该正式规范；因此**不能声称全仓门禁全绿或可直接合并**。其余审查新增文件格式通过。


代表性 Python 检查命令（在审查 worktree，`PYTHONPATH=.`；`python` 指项目 `.venv/bin/python`）：

```bash
python -m pytest -q tests/test_architecture_boundaries.py tests/test_docs_consistency.py tests/test_information_flow.py tests/test_payload_immutability.py tests/test_feature_contract_suite.py tests/test_state_contract_suite.py tests/test_event_contract_suite.py tests/infrastructure/feature/test_feature_runner.py tests/infrastructure/state/test_state_runner.py tests/infrastructure/event/test_event_runner.py tests/plugins/backtest tests/plugins/features tests/plugins/states tests/research/validation tests/research/strategies tests/research/test_cross_phase_e2e.py
python -m pytest -q tests/infrastructure/e2e/test_phase1_first_slice.py tests/infrastructure/e2e/test_phase1_first_slice_v3.py tests/infrastructure/e2e/test_versioned_replay_first_slice.py tests/infrastructure/redteam --durations=8
python -m ruff check .
python -m ruff format --check .
python -m mypy
```

前端：在 `apps/web` 执行 `npm test` 与 `npm run build`。这不包含 `smoke:live`。

性能与语义探针的可复现脚本和数据在 [探针](artifacts/architecture-audit-20261003-probes.py)、[审查证据](artifacts/architecture-audit-20261003.json)。这些是审查工具，不是新运行服务或新增验证门槛。

## 10. 交付与验收对照

- 用户要求的架构/逻辑/维护性深审：完成静态全仓扫描、关键路径深读、代表性实跑和问题复现；覆盖边界见 §2。
- 用户要求的方案：给出目标结构、实施次序、验收标准与决策清单；没有擅自执行目录迁移或修改研究规则。
- 所属 Phase：Phase 1 的审查与重构准备；**不代表 Phase 1 已通过切片验收**。
- 改动：本报告、两个审查证据文件、PROJECT_STATUS §6 的待决策索引。
- 正式规则变更：无；关联 ADR-0111，仅提出修订建议。
- 未解决：§8 四项实施前决策；F5 统计校准、F8 插件部署和其他被冻结模块的问题留待相应范围重开。
