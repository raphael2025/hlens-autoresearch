# Phase 1 D3B 验收记录：REST 四表、身份与纯 policy

| 字段 | 值 |
|---|---|
| 日期 | 2026-09-25 |
| 复核者 | Codex（Raphael 2026-09-24 “授权所有”的持续授权范围内） |
| 实现者 | Claude Code（Opus） |
| 复核对象 | D3B `3b267a0`（退回）；D3B-R1 `02c0418`（接受） |
| 结论 | **PASS — D3B ACCEPTED; D3C OPEN** |

## 1. 复核经过

- `3b267a0` 交付四张 additive REST 表、独立 REST 身份规则、REST availability / precedence policy 与
  `binance.spot.delivery-channel@1.0.0` 纯函数；没有 HTTP、store 或写入实现。
- Codex 对抗复核发现两项 fail-closed 缺陷：极端 `Decimal` 会泄漏 `decimal.InvalidOperation`；
  `build_channel_edge()` 信任调用方传入的伪造或过期比较结果，可能为不同观察或已变化的行生成边。
- `02c0418` 修复两项缺陷：投影先做量级检查并把精确的 `InvalidOperation` 映射为 `INCOMPARABLE`；
  revision 行在构造时快照化，生成边前重新比较并逐项核对比较 kind、摘要与原因，不依赖 `assert`。

## 2. 验收矩阵对照

| ADR-0027 # | D3B 证据 |
|---|---|
| #1 | 四张表均为 additive；真实 PostgreSQL 可创建 12 张表并在重启后幂等；原八张表定义顺序与哈希不变 |
| #7（纯函数部分） | REST 页、元素、边身份与通道比较均为确定性纯函数；`edge_id` 不含时间；REST 与归档元素 `observation_key` 一致 |
| #9 | availability / precedence policy binding 与证据缺口行为有正反测试，无法证明时 fail closed |
| #20（policy） | 相等、字段不等、缺字段、超定义域、毫秒/微秒 kline 等价、aggTrade 亚毫秒不等均覆盖；伪造与过期比较不能产生边 |
| #21 | REST `arrival_seq` 区间常量与边界检查已实现；归档区间和 D2 分配代码未改 |

## 3. 冻结边界与身份哈希

- 12 张表按冻结顺序建立；原八张表的定义哈希逐字节不变。
- 归档身份规则 `IDENTITY_HASH`：
  `fc5f6f082554243c5ead89d389dc862f9c5b2b38d97a140bc8afdcd47b9b40aa`。
- REST 身份规则哈希：
  `01f93537457b00ad572cc6771d12156743efe07896cee3476eb1ba46ee5a74ee`。
- delivery-channel policy 哈希：
  `399513973e6bb22e9e2c74a84ad226a220adf3b6035e8ca4290d26c19cdf1a85`。
- `core/`、Schema、Constitution、依赖、settings、D0 collector、D1 parser、D2 `identity.py` / `store.py` 均未改。

## 4. Codex 独立验证

- 对抗脚本在 `python -O` 下通过：极端十进制数返回 `INCOMPARABLE`；跨 observation key、伪造摘要、
  外部行突变与强制内部行替换均不能生成错误证据；输出 `independent-negative-checks: PASS`。
- 聚焦测试：`199 passed in 2.21s`。
- 真实 PostgreSQL 全量：`3252 passed in 29.91s`，无 skip。
- `ruff check`、`ruff format --check`、mypy strict、`uv lock --check` 全绿；docs 一致性 `7 passed`。
- 本 docs-only 接受门在相同基线再次运行真实 PostgreSQL 全量：`3252 passed in 33.40s`。

## 5. 残余风险与后续义务

- REST 1.0.0 为毫秒、2025 年后的归档为微秒；带非零亚毫秒位的 aggTrade 无法证明相等，按 D-33 保持 competing heads 并 fail closed。
- D3E 必须从持久化行重算 payload 完整性，并把不一致提升为 `CatalogIntegrityError`；D3B 的纯比较函数不替代该 I/O 义务。
- D3B 只完成表、身份与纯 policy 基础。D3C decoder、D3D collector、D3E store / reconciler 尚未实现，REST 数据现在不能进入任何数据集。

## 6. 结论与下一步

**PASS — D3B ACCEPTED; D3C OPEN。** 只开放 D3C：实现严格、无 I/O 的
`binance.spot.rest.decoder@1.0.0`。D3D、D3E、WebSocket / D4 与 Canonical / E 保持关闭。
