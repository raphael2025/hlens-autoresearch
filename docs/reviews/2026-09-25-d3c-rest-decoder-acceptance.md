# Phase 1 D3C 验收记录：严格 REST page decoder

| 字段 | 值 |
|---|---|
| 日期 | 2026-09-25 |
| 复核者 | Codex（Raphael 2026-09-24 “授权所有”的持续授权范围内） |
| 实现者 | Claude Code（Opus） |
| 复核对象 | D3C `643cf45`（退回）；D3C-R1 `6b9e670`（接受） |
| 结论 | **PASS — D3C ACCEPTED; D3D OPEN** |

## 1. 复核经过

- `643cf45` 交付纯函数 `binance.spot.rest.decoder@1.0.0`：完整正文字节、规范页查询、
  `retrieved_at`、目标窗口、上一页摘要与显式正文上限，得到不可变元素和确定性页摘要，或整页拒绝。
- Codex 对抗复核发现一项兼容性缺陷：合法 JSON 顶层值前后的 RFC 空白被错误拒绝；实现还把
  `leading_whitespace = reject` 写入 policy spec，冻结了任务与 ADR 没有批准的限制。
- `6b9e670` 只接受 RFC 8259 的四个框架空白字节（空格、HTAB、LF、CR）；非 RFC Unicode 空白与
  任何非空白尾随内容仍 fail closed。派生 decoder hash 随规则修正变化，其他冻结哈希不变。

## 2. ADR-0027 验收矩阵对照

| ADR-0027 # | D3C 证据 |
|---|---|
| #11（decoder） | 正文上限先于解析；严格 UTF-8、拒 BOM、顶层数组、重复键 / 非有限数 / 小数 JSON 数字 / 多余字段 / 截断 / 超长均整页拒绝，零元素泄漏 |
| #12（decoder） | aggTrade 与 kline 精确字段文法、`decimal(38,18)`、价量 / OHLC / taker / 分钟边界、页内与跨页顺序均有正反例 |
| #13（decoder） | answered 区间、四种终止原因、精确续页查询、目标窗口外合法元素保留、未结束 K 线与时间单位错误均按 ADR §6～§7 实现 |

D3C 只完成上述 decoder 部分。HTTP allowlist、重定向、限流、重试、预算、checkpoint 与重放不联网仍是 D3D 义务；任何写入、REST 序号和跨通道 reconcile 仍是 D3E 义务。

## 3. 冻结边界与 policy hash

- REST decoder：`binance.spot.rest.decoder@1.0.0`；派生 hash
  `ef9dae77b5d9d53211f99e4e7408d88f4ab9903f88fb7c625c3db86d7937c128`。
- 归档身份：`fc5f6f082554243c5ead89d389dc862f9c5b2b38d97a140bc8afdcd47b9b40aa`。
- REST 身份：`01f93537457b00ad572cc6771d12156743efe07896cee3476eb1ba46ee5a74ee`。
- delivery-channel policy：`399513973e6bb22e9e2c74a84ad226a220adf3b6035e8ca4290d26c19cdf1a85`。
- 归档 parser：`c2c3c375e6fa87982759549ccbc0779c546929a64454ed14c2063677212ac033`。
- `core/`、Schema、Constitution、依赖、settings、表定义、D0 collector、D1 parser 与 D2 store 均未改。

## 4. Codex 独立验证

- `python -O` 对抗探针覆盖 36 组合法前后空白组合、五类非 RFC 空白与六类非空白尾随内容：合法组合全部解码，非法组合分别稳定返回 `invalid_json` / `trailing_content`。
- 聚焦 decoder / pagination：`182 passed in 1.17s`；docs 一致性：`7 passed in 0.06s`。
- 真实 PostgreSQL 全量：`3434 passed in 31.57s`，无 skip。
- `ruff check`、`ruff format --check`（204 files）、mypy strict（112 files）、`uv lock --check` 与 `git diff --check` 全绿。

## 5. 残余风险与后续义务

- `RestPageSummary` 目前只是进程内不可变值；D3D 必须把 page / collection checkpoint 做成可恢复、可核验的不可变证据，并确保相同 `request_id` 重放不联网。
- decoder 只检查完整 entity body；流式截断、`Content-Length`、状态码、重定向、`Retry-After`、418 / 5xx 和页预算必须由 D3D fail closed。
- decoder 不持久化响应、元素、质量事实或 lineage；D3E 必须从持久化行重算完整性并执行 REST 序号区间与跨通道 graph guard。
- D3D 的只读公共 API smoke 只补环境事实，不能替代全 mock transport 的确定性验收，也不得访问账户或交易接口。

## 6. 结论与下一步

**PASS — D3C ACCEPTED; D3D OPEN。** 只开放 D3D：实现 REST collector、四项已冻结设置、
不可变 checkpoint、重放不联网和 HTTP / 重试 / 预算边界。D3E、WebSocket / D4、Canonical / E 与 Phase 0.5 保持关闭。
