# Phase 1 复核指南：`300bf33` 之后的一切

> 面向 Codex（或 Raphael）：这不是验收结论，是一份**复核路线图**——把 `300bf33`（最后一个 Codex
> 验收门，`phase1: accept D3D and open D3E`）之后的全部提交按依赖顺序分组，逐组列出"要核对什么"
> 与"哪些测试证明它"，并把这段时间里做出的架构决定按授权来源分开列出。
> 起草者：Claude Code（Sonnet 5），批次 G3-D，依 Raphael 2026-09-24 的持续执行授权。
> 配套文档：[`2026-09-25-phase1-close-evidence.md`](2026-09-25-phase1-close-evidence.md)（roadmap #1～#21
> 逐项对应表，本指南是它的"怎么复核"补充，两者应对照读）。

## 0. 怎么用这份指南

`git log --oneline 300bf33..HEAD` 有 90 余个提交；不建议逐个 commit 单独复核，因为很多是同一批次的
返修（`-R1`/`-R2`/…）或纯 docs 状态记录。下面把它们按**功能依赖顺序**折成 18 组（G3E ~ G2-R2），每组：

- **依赖**：这组假设哪些更早的组（或已验收的 D0～D3D）已经正确；
- **commit**：属于这组的全部提交，按时间顺序；
- **要核对什么**：这组声称做到了什么、复核时应重点验证的断言；
- **证明它的测试**：具体测试文件 / 函数名（`grep` 已核实存在，通过性以各自 commit message 与本文档
  为准，本指南未重新跑全量）；
- **状态**：本指南起草时，`300bf33` 之后没有实现验收门；Codex 于 2026-09-27 另行接受 D3E（含 R1～R3 与跨日修复），记录见
  [`2026-09-27-d3e-acceptance.md`](2026-09-27-d3e-acceptance.md)。除此之外，后续实现组仍无 Codex 验收门。

组的顺序就是建议的复核顺序：前面的组是后面的组的地基，先通过前面的组，后面的组的复核才有意义。

---

## 1. G-D3E — REST 响应 / 元素 revision store + 跨通道 reconciler

- **依赖**：D3A～D3D（已验收：ADR-0027、REST 纯规则、decoder、collector）。
- **commit**：`21e31f5`（D3E：`RestRevisionStore` + `ChannelReconciler` 初版）、`52f7477`（D3E-R1：核对已存行的 provenance）、`c326434`（D3E-R2：reconciler 与 store 用同一套核对）、`7e9e084`（D3E-R3：持久化行绑定到不可变来源）、`69f0bf0`（跨日 edge provenance 修复）、`9baad12` / `50cdc49`（PIT 按 edge ID 去重）。
- **要核对什么**：REST 响应 / 元素两级 Raw revision 的身份与幂等重放；REST `arrival_seq` 的区间守卫（`[2**62, 2**63)`）不与归档序号碰撞；D-33 跨通道等价比较只在逐字段完全相同时记一条"归档优先"证据边，否则不记边（继续 fail closed）；崩溃恢复路径；R1/R2/R3 三轮返修关闭的具体缺陷（原样采用来源伪造的元素行、时间/政策漂移的竞争响应被当成普通竞争、持久化行未绑定不可变来源）是否真的关上了。
- **证明它的测试**：`tests/infrastructure/revision/test_rest_store_unit.py`、`test_rest_store_postgres.py`、`test_channel_reconcile.py`。
- **状态**：✅ D3E 已由 Codex 于 2026-09-27 接受（见验收记录）。**这是整段复核的地基**：G-E0 起的一切仍按各自批次待复核；验收 D3E 不会自动验收其上实现。

## 2. G-D4 — WebSocket live tail 延期门（docs-only）

- **依赖**：G-D3E（gap reconciliation 是启用 WebSocket 的前提之一，见 roadmap #14）。
- **commit**：`a536ef3`。
- **要核对什么**：`docs/reviews/2026-09-25-d4-live-tail-gate.md` 给出的"不启用"结论是否确实由 ADR-0022 与
  roadmap #14 与 ADR-0022 的前置要求；D3E 现已验收，因此需要重新核对无实时消费者等剩余理由。仓库当前确实没有 WebSocket 代码。
- **证明它的测试**：无专门测试（门的内容是"不做"）；`tests/test_architecture_boundaries.py` 间接保证没有引入长连接依赖。
- **状态**：✅ Codex 于 2026-09-27 接受 D4 关闭、不启用 WebSocket；真实规模 backfill 与实时消费者仍是未来重新考虑的前提。

## 3. G-E0 — 双 Raw → Canonical lineage 设计（ADR-0028）

- **依赖**：G-D3E（Canonical 要同时绑定归档与 REST 两条 Raw 谱系）。
- **commit**：`ca48a36`（E0 设计草案）、`e2951e4`（E0-R1，修正 ADR-0028 列、availability 与示例缺口）、`3ee0519`（**Raphael 本人**批准方案 B，开 E1）。
- **要核对什么**：`3ee0519` 是这段历史里**唯一**由 Raphael 亲自确认的一环——复核时应确认它确实只批准了
  "Canonical 行可以同时绑定归档谱系与 REST 谱系"这一**设计**（ADR-0028 文档本身），而**不是**对 E1 之后
  任何一行实现代码的批准；`docs/adr/0028-dual-raw-canonical-lineage.md` 的状态字段与本组结论一致。
- **证明它的测试**：docs-only 门，无实现测试（E1 起才有实现测试，见组 4）。
- **状态**：设计层面已获 Raphael 批准；**实现**（组 4 起）仍是 REVIEW_PENDING。

## 4. G-E1 — Canonical normalizer + 容量重写（G3-S 系列）

- **依赖**：G-E0（ADR-0028 语义）。
- **commit**：`9b8674a`（E1）、`65aedd6`（E1-R1）、`f6c29c9`（E1-R2）、`8c9109b`（E1-R3，批次号即规范化计划）、`42530a4`（E1-R4，拒绝复用早于所述内容提交的报告）、`d816bab`（决定记录）；容量重写 `bd1d941`（G3-S：按批次窗口规范化，不整读单元）、`f92afdf`（G3-S-R1：按 rank 切片、归档单元仍整体）。
- **要核对什么**：一个 Raw source revision 是一个单元，`PersistedRowVerifier` 证明全部 Raw 行后才一一映射为 Canonical 行；独立 `arrival_seq` 块、一次时钟读数；G3-S 之后内存随批大小而非单元大小增长（30 万行新增内存约 0.8 GB，未在生产规模复测，见 `2026-09-25-phase1-close-evidence.md` §5）；批次号是否真的锁定了"单元行数 + 批大小"从而让续跑只按已提交切分推进。
- **证明它的测试**：`tests/infrastructure/canonical/test_normalizer.py`、`test_normalizer_postgres.py::test_dual_lineage_normalization_recovery_and_mapping_on_postgres`、`test_a_forged_raw_row_is_refused_on_postgres`。
- **状态**：REVIEW_PENDING。Codex 于 2026-09-27 复核发现 E1-CAP-1：`_positions`、`_committed_times` 与最终块核对保留 O(N) 单元状态，故 G3-S“内存只随 microbatch 增长”的承诺尚未成立；定向测试 `test_normalizer.py` 75 passed，但现有窗口测试只限制宽行扫描，不覆盖这些窄列与 Python 集合。修复与有界内存证据见 [`2026-09-27-e1-review.md`](2026-09-27-e1-review.md)。

## 5. G-F1 — PIT 选择器 + 容量重写（G3-S2）+ 规模性能（G3-P 的 PIT 部分）

- **依赖**：G-E1（Canonical 行）。
- **commit**：`5ce7d3a`（F1）、`f45fb78`（F1-R1，未绑定证据读作无边）；`1964e33`（G3-S2：只证明读到的批次、窗口不再限定整天）；`dcfe8b7`（G3-P，key closure 读取路径重写，**纯性能**）。
- **要核对什么**：双截止 + maximal-head 选择规则 `hlens.pit.maximal-head@1.0.0`；同输入按位一致、各类 fail-closed 情形（competing head、precedence 证据缺失、policy 版本未登记、universe 历史缺失、manifest 不完整）；G3-P 重写 key closure 读取后结果集合是否与旧的单个 `In()` 扫描逐行相同（这是复核 G3-P 时最该盯的点：改的是读取路径，不该改选择语义）。
- **证明它的测试**：`tests/infrastructure/pit/test_selector.py`（`test_the_four_cutoffs_select_as_adr_0028_section_4`、`test_an_old_spec_reproduces_its_result_after_new_evidence`、`test_selection_is_deterministic`、`test_a_mismatch_stays_a_conflict`、`test_a_forged_canonical_row_is_refused`、`test_every_revision_is_owned_by_exactly_one_window` 等）、`test_selector_postgres.py::test_bound_snapshots_decide_on_postgres`、**新增** `test_selector_scale.py`（`test_the_key_closure_equals_the_single_in_scan`、`test_a_selection_does_not_depend_on_how_the_read_is_fetched`、`test_wanted_rows_are_exactly_the_member_rows_of_the_range`）。
- **状态**：REVIEW_PENDING。

## 6. G-E3 — 质量报告 + 分段重写（G3-S3 系列）

- **依赖**：G-F1（报告内部复用 F1 的已验证图判定"竞争记录"）。
- **commit**：`bd7b60d`（E3）、`4587473`（E3-R1）；`7a70fbd`（G3-S3：成交按小时分段证明）、`0fe7471`（G3-S3-R1：同一观察键、相邻不超一天的全部 revision 一起参与选择，闭包）、`cb4bedd`（G3-S3-R2：一个窗口拥有每个 key，报告 id 绑定全部规则）、`f4c5075`（G3-S3-R3：传递闭包收尾，memo 走自己的 head）；`d75304b`（注册 PIT / quality / resample 规则标识符）。
- **要核对什么**：质量规则集 `hlens.quality.canonical-partition@1.0.0`（不含数值阈值）；报告按分区（表 × 标的 × UTC 日）一行，report_id 由输入快照决定；G3-S3 系列分段后，报告内容是否与"整天一次性证明"逐位相同（README 与测试应证明这一点）；键闭包传递收敛的正确性与已知边界（相邻不超过一天，见 `2026-09-25-phase1-close-evidence.md` §5"键闭包可达范围"）。
- **证明它的测试**：`tests/infrastructure/quality/test_reporter.py`（20 个用例，含 `test_a_bar_partition_report_lists_gaps_inputs_and_evidence_gaps`）。
- **状态**：REVIEW_PENDING。

## 7. G-E4 — 高周期 K 线重采样

- **依赖**：G-F1（只从 F1 选中的 1m bar 派生）。
- **commit**：`8d69ef6`（E4）、`3c32e2e`（E4-R1，格式修正）。
- **要核对什么**：只接受能整除一天的周期、UTC 对齐、不补缺、精确十进制运算（不精确即拒绝）；`available_time` 不早于区间结束；纯函数、不新建表。
- **证明它的测试**：`tests/infrastructure/canonical/test_resample.py`（10 个用例）。
- **状态**：REVIEW_PENDING。

## 8. G-E2 — exchangeInfo 快照与 listing 历史（ADR-0029）

- **依赖**：G-D3E（复用已验收的 D3D wire）、G-E0（Canonical lineage 语义）。
- **commit**：`4d3e6d8`（证据收集 + ADR-0029 提案）、`3b6038b`（E2 实现）、`1e3d325`（E2-R1，只数模拟时刻可见的 episode）；决定记录 `d816bab`、`33c6bce`、`801432f`（索引 ADR-0029/0030 为 Accepted）、`29a4c54`、`b8fd734`。
- **要核对什么**：**ADR-0029 本身是 Claude 依 Raphael 授权接受的架构决定**（见 §3），复核时应把"这个决定是否在授权范围内"与"E2 的实现是否正确落实了这个决定"分开评估；`TRADING`→listed、`HALT`/`BREAK`/`END_OF_DAY`/`CANCEL_ONLY`→suspended、未知状态/缺失 symbol 不推断、永不产生 `delisted`；`tradable_from` 是"本机观察下界"而非真实上市日（ADR-0029 明确声明的开放义务）。
- **证明它的测试**：`tests/infrastructure/canonical/test_listings.py`（24 个用例）、`tests/infrastructure/collector/test_binance_exchange_info.py`、`tests/infrastructure/parser/test_binance_exchange_info_decoder.py`、`tests/infrastructure/revision/test_exchange_info_rules.py`（12 个用例）、`test_exchange_info_store.py`；跨阶段补充：`tests/infrastructure/redteam/test_rt_listings.py`。
- **状态**：ADR-0029 已 Accepted（架构决定层面）；**实现** REVIEW_PENDING。

## 9. G-F4 — FeatureProvider 契约与首批特征（ADR-0030）

- **依赖**：G-E4（bar 输入）、G-F1（PIT 视图）。
- **commit**：`4472282`（ADR-0030 提案）、`bc558e3`（F4 实现）、`b5ffe98`（F4-R1，拒绝零输入值与 NaN 文本，一次调用一个时刻）；`bbfdbdf`（G3-T：容量探针工具 + 数据运行手册，为后续 G3-C/G3-P 打底）。
- **要核对什么**：**ADR-0030 同样是 Claude 依授权接受的决定**；执行器（runner）只把"历史可用且在知识截止之前"的输入交给 provider，且经因果扰动测试证明不可绕过（`test_a_leaky_provider_cannot_leak_through_the_runner`）；三个首批 provider（log-return、realized-vol、volume-sum）的精确十进制运算与"缺口不补"语义。
- **证明它的测试**：`tests/test_feature_contracts.py`；`tests/infrastructure/feature/test_feature_runner.py`（`test_the_provider_only_ever_sees_the_visible_set`、`test_a_leaky_provider_cannot_leak_through_the_runner`）、`test_feature_pipeline.py`；`tests/test_feature_contract_suite.py`（`test_feature_suite_kills_faulty_implementation`、`test_a_value_from_zero_inputs_cannot_be_built`）；`tests/plugins/features/test_bar_features.py`。
- **状态**：ADR-0030 已 Accepted；**实现** REVIEW_PENDING。

## 10. G-QG — 证据缺口独立表（ADR-0031）

- **依赖**：G-E3（质量报告要引用它）。
- **commit**：`e31ac04`（QG-1：`quality.availability_evidence_gaps` 表）、`b7e48f6`（QG-1-R1，分区 symbol 列读取修正）、`e9ff4ec`（QG-2：质量规则升 2.0.0，缺口流式写入独立表）、`0a535ab`（QG-R1：按批次逐批精确核对证据缺口）。
- **要核对什么**：**ADR-0031 同样是 Claude 依授权接受的决定**，起因是"成交一天 100～300 万条都有缺口、内嵌进报告行会到数 GB"；复核重点是报告行是否真的只存引用与计数、缺口表是否真的按批次流式写入而不是整表物化。
- **证明它的测试**：`tests/infrastructure/catalog/test_phase1_tables.py`（第 14 张表 golden）；`tests/infrastructure/quality/test_reporter.py`（缺口引用与计数）。
- **状态**：ADR-0031 已 Accepted；**实现** REVIEW_PENDING。

## 11. G-F2/F3 — 历史标的池 + Research Dataset / manifest

- **依赖**：G-E2（listing 历史）、G-F1（PIT 选择）、G-E3/G-QG（质量报告与缺口引用）。
- **commit**：`87a3d3e`（F2：`UniverseBuilder`）、`a167d87`（F3：`DatasetBuilder`）、`08913b1`（F3-I：与质量规则 2.0.0 集成）、`762643f`（F3-R1：区间部分跨越 fail closed，"先批次后清单"写入顺序成文）；`521c70b`（状态记录）、`7d4cdaa`（docs：记录存储时间与生效时间的区分）。
- **要核对什么**：F2 在 PIT 规格绑定的快照上经 `ListingDeriver.listing_at` 求成员/排除，缺 listing 绑定/政策一律 fail closed；F3 要求全部已登记、证据表有快照即须绑定，每个覆盖分区须有**恰在绑定快照上**的质量报告（`existing_only` 复算，不写不读时钟），一批写入 research 表（批次号 = `selection_id`），`ManifestStore` 以内容哈希幂等写入并复核。**这一组是组 16（G2 红队）RT-4/RT-5 两个发现的直接对象**，复核时应把 F3 原始实现与 G2-R1b（`17ff897`）的返修一起看，而不是只看 F3 首次提交。
- **证明它的测试**：`tests/infrastructure/dataset/test_universe.py`（10 个用例）、`test_dataset.py`、`test_dataset_postgres.py`。
- **状态**：REVIEW_PENDING。

## 12. G-D-HIST — 历史归档事件时间可用性假设（ADR-0032，PIT 叠加层）

- **依赖**：G-F1（PIT 选择器要能识别绑定的假设）。
- **commit**：`03116ed`。
- **要核对什么**：**这是本段历史里唯一由 Raphael 本人（非委托）直接批准的架构决定**（`docs/adr/0032-archive-event-time-availability-assumption.md` 决策者字段为"Raphael（2026-09-25 对 D-HIST 回复"同意推荐方案""）；复核重点：不绑定该假设的规格结果是否逐位不变（向后兼容声明）；绑定后是否只改变 PIT 视图的"有效时间"，存储数据、证据缺口、知识轴是否确实分毫未动。
- **证明它的测试**：`tests/infrastructure/pit/test_selector.py::test_without_the_assumption_history_before_ingest_is_invisible`、`test_the_bound_assumption_makes_an_archive_trade_available_at_event_time_plus_latency`、`test_the_assumption_never_moves_a_rest_revision`、`test_another_version_or_hash_of_the_assumption_is_refused`；跨阶段补充 `tests/infrastructure/redteam/test_rt_assumption.py`。
- **状态**：ADR-0032 已 Accepted（Raphael 亲自批准）；**实现** REVIEW_PENDING（结构性不同于组 8/9/10/13：设计决定本身的批准人是 Raphael，但实现仍未经 Codex 复核）。

## 13. G-DS1 — Research Dataset 选择表登记为生产表（ADR-0033）

- **依赖**：G-F3（`selection.py` 的提议在这里转正）。
- **commit**：`6fba363`。
- **要核对什么**：**ADR-0033 是 Claude 依授权接受的决定**；表形状与 `infrastructure/dataset/selection.py` 原提议是否真的字段对字段一致（`selection.py` 现在反向从 `phase1_tables.py` 导入 schema，两处不应再重复定义）；前 14 张表的定义与哈希是否真的不变（`test_earlier_goldens_are_unchanged_by_ds1`）。
- **证明它的测试**：`tests/infrastructure/catalog/test_phase1_tables.py`（第 15 张表 golden、回归）；`tests/infrastructure/dataset/dataset_support.py` 使用生产表而非另建同形状测试表。
- **状态**：ADR-0033 已 Accepted；**实现** REVIEW_PENDING。

## 14. G1 — 首切片端到端验收（roadmap #20）

- **依赖**：组 1～13 全部（这是把它们首尾接起来的测试）。
- **commit**：`bf93cd2`。
- **要核对什么**：`docs/reviews/2026-09-25-phase1-e2e-acceptance.md` 逐条列出的六个子步骤是否真的覆盖 #20 的要求；未改动任何生产代码（只加测试 + 记录）；PostgreSQL 变体在 `HLENS_TEST_CATALOG_URI` 可用时是否真的跑通（本地环境目前是 SKIPPED）。
- **证明它的测试**：`tests/infrastructure/e2e/test_phase1_first_slice.py`（`test_phase1_first_slice_archive_to_representation`、`test_pit_selection_with_and_without_the_archive_assumption`）、`test_phase1_first_slice_postgres.py`。基线运行：19 passed、1 skipped（见验收记录）。
- **状态**：REVIEW_PENDING（测试证据已存在，等 Codex 结论）。

## 15. G3-C — 全链路容量探针 + 关闭证据草稿

- **依赖**：组 1～14（探针要 import 这些模块）。
- **commit**：`89d5070`、`3b81fa3`。
- **要核对什么**：`infrastructure/tools/capacity_probe.py` 只 import 生产模块、不 import `tests/`；`--rest`/`--dataset`/`--feature` 三个阶段的"读得诚实的边界"（单页 REST、单小时数据集、单一求值时刻）是否真的如实标注；这是**容量探针**，复核时不应把它的成功执行当成正确性证据。
- **证明它的测试**：`tests/infrastructure/tools/test_capacity_probe.py`。
- **状态**：REVIEW_PENDING（工具本身，不是需要"验收"的生产路径，但复核关闭证据文档时应确认这一点没被夸大）。

## 16. G2 — 跨阶段红队套件 + 六个发现

- **依赖**：组 1～14（攻击的是这些阶段组合起来的行为）。
- **commit**：`ccb57bc`（套件本身，92 个用例，84 通过、6 个发现以 8 个严格 xfail 固定，未改生产代码）、`9b2124f`（把六个发现记入 `PROJECT_STATUS.md` §7，这是 `PROJECT_STATUS.md` 目前最后一次更新）。
- **要核对什么**：README（`tests/infrastructure/redteam/README.md`）里每一行"攻击 → 拒绝方"的映射是否真实存在于对应生产模块；6 个发现（RT-1～RT-6）的严重度判定是否合理；提交时确实没有夹带任何生产代码改动（`ccb57bc` commit message 自称，应核实 `git show --stat ccb57bc` 只有 `tests/infrastructure/redteam/`）。
- **证明它的测试**：`tests/infrastructure/redteam/`（九个模块，见 README 表格；本指南 §0 已列出对应文件名）。
- **状态**：REVIEW_PENDING（发现本身不是缺陷修复，是缺陷记录）。

## 17. G3-P — 规模性能（语义不变）

- **依赖**：组 5（PIT 选择器）、组 4/1（行证明）、组 9（feature runner）。
- **commit**：`dcfe8b7`。
- **要核对什么**：三处改动（PIT key closure 读取、行证明 `take` 替代逐行 slice、feature runner 未改代码只改文档）是否真的都有"改前/改后结果一致"的回归测试；commit message 里"需要改核心才能进一步优化 feature runner，不能靠削弱检查"这句话背后有没有被信守（即 `check_answers` / `content_hash` 没有被绕过或弱化）。
- **证明它的测试**：`tests/infrastructure/pit/test_selector_scale.py`（新文件）、`tests/infrastructure/revision/test_channel_reconcile.py`（新增用例）。
- **状态**：REVIEW_PENDING（纯性能改动，但仍需确认"语义不变"这一自我声明成立）。

## 18. G2-R1 / G2-R1-I / G2-R2 — 六个红队发现的返修

- **依赖**：组 16（G2 发现）、组 11（F2/F3，RT-2/RT-4/RT-5 的对象）、组 9（F4，RT-6 的对象）、组 4（E1，RT-1/RT-3 的对象）。
- **commit**：`aef4ce7`（G2-R1a：RT-1/RT-2/RT-3）、`17ff897`（G2-R1b：RT-4/RT-5）、`0477bce`（G2-R1c：RT-6，新增 `feature_request_from_dataset`）、`c756bbe`（G2-R1-I：把 R1b 与 R1c 接起来）、`ac2daef`（G2-R2：cursor-agent 只读复核发现并修复 RT-5 replay 路径的残留漏洞、feature 验证器来源收紧、一处全量门失败）。
- **要核对什么**：六个发现是否**真的**修复（README 里已无残留 `xfail`，`grep -rn xfail tests/infrastructure/redteam/*.py` 应为零命中，只在 README 说明文字里出现）；G2-R2 是"对返修的返修"，复核时应重点看它发现的两个问题（RT-5 的口子、feature 验证器来源）是否说明前几轮返修本身不够彻底；`_check_rest_unit` 的容量 follow-up（`ac2daef` commit message 里"(LOW-MED, documented)"）是否应该在这一轮一并解决，还是可以作为已记录的已知限制留到以后。
- **证明它的测试**：`tests/infrastructure/redteam/`（全部 9 个模块，xfail 移除、断言保留）；**新增** `tests/infrastructure/feature/test_feature_dataset.py`（7 个用例）。
- **状态**：REVIEW_PENDING（这是"组 1～17 是否真的没有被红队攻破"的最终状态，理论上应该是整个 `300bf33` 之后复核的收尾一组，而不是开头）。

---

## 2. 本段历史中的架构决定：授权来源分两类

| ADR | 标题 | 决策者 | 依据 |
|---|---|---|---|
| [0028](../adr/0028-dual-raw-canonical-lineage.md) | 双 Raw → Canonical lineage 语义 | **Raphael 本人**（`3ee0519`，"accept ADR-0028 (Raphael)"） | Raphael 明确批准方案 B |
| [0029](../adr/0029-listing-history-source.md) | 上市历史来源（E2） | Claude Code，依 Raphael 2026-09-25"一切都你自己决定"的授权 | ADR 决策者字段自述 |
| [0030](../adr/0030-feature-provider-contract.md) | FeatureProvider 契约（F4 前置） | Claude Code，同上授权 | 同上 |
| [0031](../adr/0031-quality-evidence-gap-table.md) | 证据缺口独立表（D-QGAP） | Claude Code，同上授权 | 同上 |
| [0032](../adr/0032-archive-event-time-availability-assumption.md) | 历史归档事件时间可用性假设（D-HIST） | **Raphael 本人**（对 D-HIST 回复"同意推荐方案"） | ADR 决策者字段自述；这是唯一一个 CLAUDE.md §0 明确列为"红线，需 Raphael 亲自决定"的项（触及宪法 C-L1 防泄漏前提） |
| [0033](../adr/0033-research-dataset-selection-table.md) | Research Dataset 选择表登记为生产表（DS-1） | Claude Code，依 Raphael 2026-09-25 同一份授权 | ADR 决策者字段自述 |

复核这六份 ADR 时建议分两条线：

1. **0028 与 0032（Raphael 本人决定）**：复核重点是"实现是否忠实落实了 Raphael 批准的那个具体方案"，
   决定本身不需要重新论证是否该由谁来做。
2. **0029 / 0030 / 0031 / 0033（Claude 依委托决定）**：CLAUDE.md §0 明确"Claude 可以在 Raphael 给出的
   项目目标与硬性规则内...识别风险与矛盾、起草 ADR"，但"Codex 仅可在 Raphael 明确委托的边界内作正式
   决定"——这四份 ADR 的决策者是 Claude 本人，依据的是 2026-09-25 那一次性授权（"一切都你自己决定，
   红线除外"）。复核时除了实现是否正确，也应确认：①这四个决定确实都不触碰 H1～H14 任何一条红线；
   ②它们确实都是 additive（不改变已发布契约、不删除历史）；③Raphael 那次授权的范围本身是否覆盖了这
   四类决定——这是唯一需要 Codex（而不是本文档）下判断的地方，本指南不代为结论。

---

## 3. 留给 Raphael 的未决事项（本指南不代为决定）

以下三项，按 CLAUDE.md §10.2 / §10.6，**必须**由 Raphael 本人明确批准，Codex 或 Claude 都不能代行：

1. **合并进 `main`**：当前 `phase/1` 上 `300bf33` 之后的全部工作（组 1～18，本指南列出的 18 组）都还
   没有 Codex 验收门。按 CLAUDE.md §10.2 第 3 条，"架构决定、ADR、Research Constitution、Lifecycle、
   验证架构、研究/生产边界"合并进 `main` 需要 Raphael 明确批准；本段历史包含六份 ADR（见 §2），因此
   即使 Codex 复核通过，合并进 `main` 这一步本身仍需要 Raphael 单独点头。
2. **打 `phase-1-complete` tag**：CLAUDE.md §10.6，tag 一律只加不改，且"Raphael 批准后才创建"。打这个
   tag 的前提是 roadmap Phase 1 验收矩阵 #1～#21 全部满足（见 `2026-09-25-phase1-close-evidence.md`），
   目前只有 #1～#12 与 #17 前半满足，其余仍是 REVIEW_PENDING，尚不满足打 tag 的条件。
3. **开启 Phase 2**：`PROJECT_STATUS.md` §10 把"Phase 1 关闭条件"定义为验收矩阵 #1～#21 全部满足；
   `PROJECT_STATUS.md` §6 的 D-P05 记录"Phase 1 关闭后再开 Phase 0.5，不并行"，Phase 2 同理应在 Phase 1
   正式关闭（而不是"REVIEW_PENDING 但看起来都测过了"）之后再开。这是否可以提前、或以什么条件提前，
   是 Raphael 的决定，不在本指南或 `close-evidence` 文档的建议范围内。

---

*本文件由 Claude Code（Sonnet 5）依 Raphael 2026-09-24 的持续执行授权起草，批次 G3-D。
草稿，供 Codex / Raphael 复核使用；不构成任何验收结论。*
