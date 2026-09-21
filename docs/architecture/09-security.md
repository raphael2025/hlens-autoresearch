# 09 — Security

## 1. 威胁模型（摘要）

| 资产 | 威胁 | 控制 |
|---|---|---|
| 交易所 API Key | 泄漏、误用下单 | 研究期只用**只读**密钥；交易密钥 Phase 13 前不存在于系统 |
| 研究结论 / 策略 | 被篡改以通过验证 | 追加式记录、审计日志、Constitution 版本锁定 |
| 数据完整性 | 静默损坏、泄漏 | Iceberg 快照 + 质量报告 + 哈希 |
| LLM 交互 | Prompt injection、数据外泄 | 见 §3 |
| 插件代码 | 恶意/错误代码 | 见 §4 |

## 2. 密钥管理

- 密钥永不进入 Git（`.gitignore` 已覆盖 `.env*`、`secrets/`）。
- 本地开发：环境变量或本地密钥文件（权限 600）。
- 服务化后：密钥管理器（具体实现待定，可替换）。
- 密钥按用途分离：`market-data-readonly`、`llm`、`storage`、（未来）`execution`。

## 3. LLM 安全

- 外部知识源（论文、网页、代码）是**不可信数据**，永不作为指令执行。
- LLM 输出只能是结构化数据（Hypothesis / ExperimentSpec），必须通过 Schema 校验。
- LLM 不能触发：代码执行、Lifecycle 转移、Constitution 修改、密钥访问。
- 数据外发策略：明确哪些数据可以发给外部 LLM（默认：不发送私有持仓/账户数据）。
- 记录全部 LLM 调用（provider、model、输入输出哈希）以供审计与复现。

## 4. 插件与生成代码

- Phase 0–6：插件均为人工审查代码。
- Phase 7+：若允许自动生成的变换/策略代码，必须在沙箱中运行（无网络、只读数据、资源限制），并且只能以组合算子 DSL 表达优先于任意代码。

## 5. 执行安全（Phase 13+）

- 实盘执行需独立服务、独立密钥、硬性风险限额（RiskProvider 之外的第二道防线）、Kill Switch。
- 研究平面永远无法直接调用执行服务。

## 6. 审计

所有 Lifecycle 转移、Constitution 版本变化、人工审批、OOS 开封事件写入追加式审计日志（Control Plane）。
