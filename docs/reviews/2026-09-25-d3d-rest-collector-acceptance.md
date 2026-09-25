# Phase 1 D3D 验收记录：可重放 REST collector

| 字段 | 值 |
|---|---|
| 日期 | 2026-09-25 |
| 复核者 | Codex（Raphael 2026-09-24 “授权所有”的持续授权范围内） |
| 实现者 | Claude Code（Opus） |
| 复核对象 | D3D `61dd9bf`（退回）；D3D-R1 `c06b9fa`（接受） |
| 结论 | **PASS — D3D ACCEPTED; D3E OPEN** |

## 1. 复核经过

- `61dd9bf` 交付 `binance.spot.public-rest@1.0.0`：结构化端点 allowlist、D3C 驱动的确定性分页、
  有界 HTTP / 重试 / 限流、不可变正文与 page / collection checkpoint、崩溃恢复和同
  `request_id` 离线重放。
- Codex 接受门复现一项安全边界缺陷：两个构造入口都能注入完整 `httpx.Client`。该 client 的默认
  Cookie、header 与 request hook 在 URL allowlist 之后运行，实测可加入 `cookie`、
  `authorization`，并把已校验的 `/api/v3/aggTrades` 改写到
  `https://evil.example/api/v3/account`；MockTransport 收到了改写后的外域账户请求。
- `c06b9fa` 删除完整 client 注入，只保留 `http_transport` 确定性测试 seam；collector 永远自建并
  拥有 client，固定 `auth=None`、空 hooks、`trust_env=False`、不跟随重定向、拒收一切 Cookie，
  并在发送前再次核对 method、完整 URL 与凭据形状 header。返修同时关闭环境代理与
  `Set-Cookie` 跨请求回放两条同根路径。

## 2. ADR-0027 验收矩阵对照

| ADR-0027 # | D3D 证据 |
|---|---|
| #10 | aggTrades 只按 decoder 给出的 `fromId = last + 1` 续页；klines 只按 `startTime = last closed open + 60s` 续页；四种终止、页预算与不前进守卫有正反例 |
| #11～#13（collector 部分） | 对象覆盖裁剪到请求窗口；空页、短页、overshoot、未结束 K 线和 decoder 拒绝分别产生确定性结果或稳定失败；失败不伪装为缺口 |
| #14 | 同实例、重建实例及来源第二次返回不同字节时均重放首次承诺；同 `request_id` 不同请求在联网前拒绝；既有 Collector contract suite 通过 |
| #15（c1 / c2） | c1 后 orphan 不构成承诺；c2 后逐页严格读回、重新解码并从下一未承诺页续取；c3 结果重放零网络 |
| #16 | 预算、429 / `Retry-After`、418、5xx、传输错误、截断、超限和 4xx 全部有界失败，不产生成功 checkpoint 或 `SOURCE_ABSENT` |
| #17～#18 | origin、path、query 与最终 outgoing request 双层核对；不接受完整 client；无账户 / 订单 / 签名端点与密钥读取 |
| #19 | 四个设置字段默认值和上下界已验证；HTTP timeout / retries / UA / market-data base 继续复用既有设置 |

D3D 不写 Iceberg、不分配 `arrival_seq`、不生成 response / element revision 或跨通道 precedence
edge；这些仍是 D3E 的唯一范围。

## 3. Codex 独立验证

- 原攻击脚本在返修后得到 `TypeError`，敌意 client 的 transport 收到零请求；两个公开入口签名均无
  `http_client` 或可吞入它的可变参数。
- 自建 client 独立检查：无 auth / hooks / base URL / 环境代理 / Cookie / redirect；`close()` 幂等。
- D3D-R1 安全与静态聚焦：`26 passed in 0.10s`；collector + settings：
  `240 passed in 1.16s`。
- 真实 PostgreSQL 全量：`3587 passed in 34.67s`，无 skip。
- `ruff check`、`ruff format --check`（212 files）、mypy strict（119 files）、
  `uv lock --check` 与 `git diff --check` 全绿。
- 独立只读公共 smoke：market-data-only base 的 aggTrades / klines 各一页 HTTP 200 并由 D3C 接受；
  时间字段为 13 位毫秒，1m K 线 `close-open = 59999`；随后替换为拒绝一切请求的 transport，
  两种数据均从 checkpoint 离线重放，零网络。

## 4. 冻结边界

- 归档身份：`fc5f6f082554243c5ead89d389dc862f9c5b2b38d97a140bc8afdcd47b9b40aa`。
- REST 身份：`01f93537457b00ad572cc6771d12156743efe07896cee3476eb1ba46ee5a74ee`。
- REST decoder：`ef9dae77b5d9d53211f99e4e7408d88f4ab9903f88fb7c625c3db86d7937c128`。
- delivery-channel policy：`399513973e6bb22e9e2c74a84ad226a220adf3b6035e8ca4290d26c19cdf1a85`。
- `core/`、Schema、Constitution、ADR / architecture、依赖锁、12 张表定义、归档 collector、
  parser、D2 store 与 `infrastructure/revision/**` 均未改。

## 5. 残余边界与 D3E 义务

- REST 正文和 checkpoint 已可恢复，但尚未成为 Raw response / element revision；D3E 完成前任何
  REST 数据仍不得进入数据集。
- D3E 必须从持久化正文重新验证 payload，decoder 拒绝也要写 response revision 与质量事件；
  accepted 页才写元素 revision。
- REST `arrival_seq` 必须落在 `[2**62, 2**63)`，并在单个跨通道 graph 内执行范围与碰撞 guard；
  不得修改 D2 归档分配器或 `identity.py`。
- reconciler 必须支持两种到达顺序、四段 `knowledge_cutoff`，只在规范投影逐字段相等时向独立
  evidence 表追加“归档 → REST”项目政策边；不可比较、payload 不符或缺对侧均 fail closed。

## 6. 结论与下一步

**PASS — D3D ACCEPTED; D3E OPEN。** 只开放 D3E：实现 REST response / element revision store、
REST 序号分配与恢复、跨通道 reconciler、证据表幂等 checkpoint 和 graph range guard。
WebSocket / D4、Canonical / E 与 Phase 0.5 保持关闭。
