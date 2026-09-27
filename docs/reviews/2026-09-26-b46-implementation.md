# B46 修复：`file:` URI scheme 大小写不敏感脱敏（2026-09-26）

> 状态：`CODE_COMPLETE / DEBUG_PENDING`（非验收）。来源：[Codex 全量代码复核](2026-09-26-codex-full-code-review.md) B46 节。
> 基线：`origin/wip/all-code-completion` `1cd3284`；分支 `fix/b46-file-uri-redaction`。未合并到任何集成 / phase / main 分支。

## 缺陷

`apps/api/app.py::public_detail` 的 `file:` 分支只匹配小写 scheme；URI scheme 按 RFC 3986 §3.1 不区分大小写，
因此 `FILE://host/home/…/report.json`、`FiLe://…` 原样返回服务器路径（`FILE:/p`、`FILE:///p` 则残留 `FILE:` 前缀）。

## 修复

只把 `_ABSOLUTE_PATH` 中的 `\bfile:` 改为 `\b(?i:file):`（作用域内联标志，只影响 scheme 五个字符）。
authority（`//host`）、路径分段、其余三个分支（冒号后路径、`~` 路径、裸绝对路径）与 `\b` 词边界均不变，
故 `profile://…`、`https://host/path`、`1/2`、普通文字照旧不被改写。模块 docstring / 注释与 `apps/api/README.md` 同步。

## 测试（`tests/apps/test_api.py`，均断言完整输出）

- `file` / `FILE` / `File` / `FiLe` / `fILE` × `file:/p`、`file:///p`、`file://host/p`、引号包裹、目录尾 `/`、
  与 HTTPS / 比例 / `~` 路径混排的一句：30 例。
- 不变：`https://host/path`、`HTTPS://Host/Path`、`ratio 1/2`、含 `FILE` 的普通文字、`profile://` / `PROFILE://`。
- API 端到端：provider 抛出含 `FiLe://host/…` 的 `KnowledgeProviderError`，502 body 恰为
  `{"detail": "knowledge provider could not answer: unreadable: report.json"}`。
- 修复前运行：25 failed（4 种非小写 scheme × 6 形态 + API 用例），小写用例通过。

## 已运行

| 命令 | 结果 |
|---|---|
| `uv run pytest -q -p no:cacheprovider tests/apps/test_api.py` | 62 passed, 1 warning |
| `uv run pytest -q -p no:cacheprovider tests/apps` | 353 passed, 1 skipped, 1 warning |
| `uv run ruff check apps/api tests/apps` | All checks passed |
| `uv run ruff format --check apps/api tests/apps` | 25 files already formatted |
| `uv run mypy apps/api tests/apps/test_api.py` | Success: no issues found in 5 source files |
| `uv run pytest -q -p no:cacheprovider tests/test_docs_consistency.py` | 7 passed |

未运行全量 pytest；最终门禁须在集成后的最终 SHA 上运行。B45、B49 不在本分支范围内。
