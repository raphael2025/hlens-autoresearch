# 2026-10-03 独立公开发布记录

## 范围

依据 Raphael 本次直接指令及 [ADR-0112](../adr/0112-independent-publication-and-private-project-archives.md)，为现有独立仓库准备公开版本：补充 MIT LICENSE、README、贡献和安全说明，带上独立架构审查。仅修正重构示例的空格格式，不修改生产代码、研究宪法、契约、阈值或历史提交。

发布分支：`phase/1`。默认展示该当前开发分支；`main` 仍是既有工程基线。公开不构成任何阶段验收。

## 历史检查

- 扫描器：Gitleaks 8.30.1，来自官方 GitHub release，下载归档 SHA256 与同 release checksums 一致。
- 对全部本地 refs（已 fetch 远端）的 Git 历史扫描：1,538 个提交、约 116.55 MB。
- 8 个告警经逐项检查属于误报：`test_bounded_runs.py` 中三个 `key=_key` 排序参数；`test_content_hash_memo.py` 两个版本中的 golden 内容哈希；`test_adapter_contracts.py` 三个固定 Schema 哈希。
- 检查不以“包含 key/password 字样”直接判断凭据；例子中的数据库密码为 `CHANGE_ME_NOT_A_REAL_SECRET`。
- GitHub Actions 历史运行数为 0，Release 数为 0（发布准备时 API 核验）。
- 未发现需要撤销的实际凭据；扫描和人工核对不构成绝对无泄漏保证。

## 工程检查

本次实际运行：

- 文档一致性 / 架构边界：`31 passed in 0.96s`。
- `ruff check .`：`All checks passed!`。
- `ruff format --check .`：`1165 files already formatted`。
- mypy：`Success: no issues found in 884 source files`。
- 当前文件 Gitleaks 扫描：7 个告警，全部对应上述同类测试排序参数与固定哈希。
- GitHub Issue/PR 正文、Issue 评论和 review 评论导出扫描：约 236.57 KB，`no leaks found`。

未重新运行完整 Python 测试套件，不宣称满足合并 main 的全仓门禁。独立审查此前的 1,758 项 Python 测试及 262 项前端测试结果见 [审查报告](2026-10-03-independent-architecture-audit.md)；不将其冒充本次全仓测试。

## 外部核验

公开和推送完成后补录 GitHub 的实际可见性、默认分支及许可证识别结果。
