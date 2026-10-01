# W1 全仓门禁修复记录（2026-10-01，`phase/1`）

## 结论

本轮在 `phase/1` 上收口四项主线代码缺口的候选与加固，并把全仓门禁（pytest / ruff check / ruff format / mypy）从首次实测的红色基线修到可复验状态。最终门禁数字见文末「最终门禁」节；PostgreSQL 相关用例在未设置 `HLENS_TEST_CATALOG_URI` 时按设计 skip，不计通过。本记录不代表 E1-CAP-1 容量结论或任何 Phase 验收。

## 首次实测基线（改动前，`phase/1@85c6abd` + 未提交的 Raw spool 初稿）

- `tests/infrastructure`：183 failed / 2885 passed / 143 skipped / 35 errors。其中 209 处同一根因：测试夹具仍经 `DatasetBuilder.build` 新建 v2 Dataset，而 ADR-0077 DQ-10 已禁止新建 v2。
- 其余 `tests/`：1 个收集错误（测试数据 `Ref` 名含连字符），修复后 65 failed / 6119 passed。
- ruff：57 errors、46 个文件未格式化；mypy：132 errors（全部在测试文件）。
- 抽样在干净 `main@b5f80fe` 上复现（例：bars / redteam / feature 抽样 37 failed；golden、event registry、indicator perturbation 失败同样复现），即这些是主线既有失败，不是本轮引入。

## 代码缺口收口

| 缺口 | 处理 | 证据 |
|---|---|---|
| E1-RAW-WINDOW-REUSE | `fe842b3`：单元首个 Raw 窗口仍是一次过滤扫描（窄 PIT 证明只读该窗口）；第二个窗口起把整个单元一次性 spool 到 canonical scratch（Arrow IPC，1024 行重切片 + SQLite position 索引），之后所有 proof / replay / write 窗口从 spool 读取，不再逐窗口重扫 Raw 表。行顺序、多 / 缺行错误、lineage 校验不变；schema 不符 fail closed；所有关闭路径释放 scratch。 | canonical + pit selector + revision `849 passed / 22 skipped`；新增 spool 单测与窄读回归 |
| E1-ARCHIVE-REUSE 加固 | `63d09a4`：非 frozen normalizer 的每次调用 pin 在成功 / 失败路径关闭 verifier 及其 archive spool（原先依赖 GC，重试循环的 traceback 会延长持有）。 | canonical + pit + revision `993 passed / 24 skipped` |
| E1-V2-REPLAY（DQ-10）加固 | `63d09a4`：v2 重放若重新派生的 manifest 与已持久化的唯一 manifest 不同，则在 persist 前拒绝（防止漂移写出新 v2 manifest）；同一 selection batch 出现在多个 snapshot 时 fail closed。 | dataset 套件通过；漂移测试在去掉守卫后失败（变异验证） |
| P7-CS-EXEC 加固 | `63d09a4`：单序列节点把横截面 FeatureSpec 作为直接引用输入时，在编译期拒绝（与 plan-node 消费一致）。 | research/hypotheses 通过 |

## 测试修复分类与依据（H3 / H4）

所有改动均不删除断言、不放宽容差、不跳过用例；每处预期变化都追溯到已接受的 ADR / 提交。

1. **DQ-10 v2 夹具**：新增 `tests/infrastructure/dataset/dataset_support.py` 的 `seed_historical_v2` / `legacy_v2_build`。已有持久化 v2 manifest 时走公开 `DatasetBuilder.build` 重放（DQ-10 允许的路径）；否则以下层 writer 播种一个"截止前"的 v2 构建（沿用 `48c7d01` 的 `historical_v2_build` 先例，persist 校验与原 build 相同为 `_JustSelected`）。生产代码仍拒绝新建 v2。唯一"当前版本新建 v2"的用例改为同时断言公开路径被 DQ-10 拒绝。
2. **v3 夹具补 quality v3**：`c70c4d9` 起 v3 Dataset 必须绑定 `quality.data_quality_report_manifests`。`tests/infrastructure/bars/v3_support.py` 改为真实写出 `QualityReporterV3` 分区报告与 listing 报告，并向 `dataset_evidence_sources` 传入真实 `BoundedQualityEvidence`（无 mock）。
3. **契约信封漂移的固定值**：golden experiment（仅 `backtest.result_hash` 变化；在 2.2.0 scope 下导入并运行，旧记录逐位复现，按文件头既有流程重生）；`PRE_VOLUME_REQUEST_HASH` 保留原值，改为在其记录版本 2.2.0 下重建请求比对，并断言当前版本仅信封不同；schema 固定值在剔除 ADR-0088（`9925f0a`）新增可选字段后与原固定值逐字节一致。报告 / 路由类固定值的处理见下节。
4. **陈旧计数 / 名称**：registry 146→148（ADR-0094 `b6f9e11` 新增 2 个 PitConflict 模型）；`PHASE1_TABLES` 15→18（ADR-0077 新增 3 表）；schema `ResearchDatasetEvidenceManifest` 仅 docstring 变化，用项目导出工具重导出；`ExperimentStage._trial` 已在 `ca8bd57` 拆为 `_execute` / `_finish`，打桩点改为 `_execute`；`Ref` 名改为合法 snake_case。
5. **检查更严格而非更松**：indicator 因果扰动原为仿射 `2x+1`，RSI / %B / ADX 对仿射不变，"扰动必须有牙齿"检查正确失败；改为非单调、逐 bar 偏移并制造 inside bar，使扰动对所有指标生效。OpenAPI smoke 补测新增的 `/healthz`、`/readyz`。
6. **拒绝路径消息**：报告 DTO（ADR-0081 `c752d55`）在契约校验前以必填字段拒绝；sealed pair 在链路检查前以 dataset 表不同拒绝 v2/v3 混合。异常类型与"被拒绝"语义不变，匹配改为接受两种具体拒绝原因。
7. **文档一致性**：`test_remaining_b3_gaps_are_still_listed` 要求 `PROJECT_STATUS.md` 继续列出两个仍开放的 B3 缺口（传递依赖闭包、`LlmCall` 登记完整性）；状态压缩时被删，已补回 §7。
8. **静态检查**：ruff format 全仓；32 处超长行 / UP047 改写（字符串用隐式拼接保持运行时值逐字节不变）；mypy 测试类型修正（注解 / cast / 精确 ignore，不改逻辑）。
9. **报告 / 路由 / 合成校准固定值**：全部固定值记录于契约 2.2.0（`a8490bf` / `6d887b7`）。新增 `tests/contract_version_support.py::at_contract_version`，在全新解释器中先进入 `contract_schema_version_scope` 再导入（模块常量也按该版本构建），原固定值逐位复现，未改任何哈希；同一调用在 2.5.0 下失败（非空检验）。旧"B67 dataset-report hash"失败据此确认为信封漂移、非回归。robustness 用普通 scope 即可复现并加了仅信封不同的断言。`dual_momentum@1.0.0` 进入横截面声明集（ADR-0085，`24e3e79`），期望集合补齐。
10. **Research Loop 记录**：`ca8bd57`（ADR-0100 修订 2）让新运行把同源输入写入哈希记录，ADR 明示"记录值进入运行的内容哈希"；按测试文件头流程在同一提交重钉并注明原值。同时发现真实缺陷：实验行 `run_inputs` 把 `impact_coefficient` 以 float 写入哈希记录，违反"哈希记录无 float"不变量；已改为与相邻 `params` 一致的 `decimal_text`（`research/loop/trials.py`）。配置指纹在 2.2.0 下复现原值（仅信封）。

## 最终门禁（2026-10-01，`phase/1` 工作树，提交前）

| 检查 | 结果 |
|---|---|
| `pytest tests/infrastructure`（无 PostgreSQL URI） | 3108 passed / 143 skipped / 0 failed（1:04:17） |
| `pytest tests --ignore=tests/infrastructure` | 6205 passed / 1 skipped / 0 failed（32:34） |
| PostgreSQL 启用（`HLENS_TEST_CATALOG_URI` = 专用测试库，`*_postgres` + catalog + 真实数据 e2e + event 建表 + worker restart） | 首轮 346 passed / 3 failed（同类陈旧夹具：两处首建 v2、一处 15 表计数）；修复后该三文件 14 passed |
| `ruff check .` / `ruff format --check .` | 通过 / 1062 files formatted |
| `mypy` | 0 errors（803 source files） |

无 PostgreSQL 时的 skip 为 `HLENS_TEST_CATALOG_URI` 未设置的数据库集成用例（另有少量依赖 `/proc` 的 FD 计数）；PostgreSQL 用例已另行实跑，见上表。未运行：E1-CAP-1 正式容量矩阵、Web（npm）构建与浏览器验收。
