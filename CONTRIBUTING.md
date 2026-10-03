# 参与 HLENS-AutoResearch

项目处于研究原型和架构收缩阶段。当前工作范围见 [REFACTOR_TARGET.md](REFACTOR_TARGET.md)，已有问题和建议见 [独立架构审查](docs/reviews/2026-10-03-independent-architecture-audit.md)。

## 开始前

1. 阅读 [AGENTS.md](AGENTS.md)、[CLAUDE.md](CLAUDE.md) 和 [PROJECT_STATUS.md](PROJECT_STATUS.md)。
2. 使用 Python 3.13 和 `uv sync --locked`。数据与测试数据库由开发者自行配置。
3. 对现有问题提交最小、可复现的示例。冻结阶段的新能力、契约或研究规则变更先提出设计讨论。

## 提交变更

- 使用独立分支和 Pull Request，写清目的、所属 Phase、修改范围、验证结果以及相关 ADR。
- 不提交凭据、`.env`、行情原始数据、数据库或本地运行产物。
- 保留失败实验；不为改善回测结果调整验证规则，不为通过检查削弱测试。
- 有代码变更时，运行相应测试、`uv run ruff check .`、`uv run ruff format --check .`、`uv run mypy`。完整合并门禁按 CLAUDE.md 执行；如有未运行或跳过的检查，请明确说明。
- 算法变更需解释时间对齐、可用信息、费用和数值误差；性能改善不能替代正确性验证。

贡献按 [MIT License](LICENSE) 提供。请保留第三方代码的原有许可证与版权声明，并说明其来源。
