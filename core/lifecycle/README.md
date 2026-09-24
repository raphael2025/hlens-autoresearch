# core/lifecycle

研究对象晋升状态机与 Experiment Run 状态机（07-validation.md §3、06-experiment.md §4）。只允许文档定义的转移。

> Phase 0：已有两套状态机代码。ADR-0011（D-17）已实施：`REVALIDATION → RETIRED` 需人工批准；
> 历史的主体归属与时间单调在直接构造时也校验；Risk Gate 与授权记录绑定主体、授权窗口必须覆盖变更时刻。
> 实盘开关**不在**契约层：自报的 `live_execution_enabled` 已删除，该红线由未来 Control Plane 与人类授权执行。
> ADR-0018 已实施：主体比较使用 `Ref.target_identity()`（`(kind, name, version)`），信封版本不同的同一目标不再被误拒。
> ADR-0019 已实施：每条 `LifecycleTransition` 必须带至少一项非空证据引用（`EvidenceRef`），不做自报职责分离、不核验证据存在性。
> 当前状态见 PROJECT_STATUS.md。
