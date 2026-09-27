"""Provider-agnostic contract suites for Data Plane Adapters（ADR-0017；Phase 1 B3）。

任何声称实现 `StorageAdapter`、`CatalogAdapter` 或 `CollectorAdapter` 的类都必须通过同一套检查。
复用入口（未来 C1 / C2 / D0 的测试）：

```python
from tests.contract_suites.storage import StorageAdapterContract, StorageSubject

class TestFileStorage(StorageAdapterContract):
    @pytest.fixture
    def storage_subject(self, tmp_path: Path) -> StorageSubject:
        return StorageSubject(open=lambda: FileStorage(root=tmp_path / "warehouse"))
```

`CatalogAdapterContract` / `CatalogSubject`（`catalog.py`）与 `CollectorAdapterContract` /
`CollectorSubject`（`collector.py`）用法相同。每个检查也是可单独调用的普通函数（`STORAGE_CHECKS`、
`CATALOG_CHECKS`、`COLLECTOR_CHECKS`），失败时一律抛 `ContractSuiteFailure`。

这些 suite 只检查可观察行为，不检查 `isinstance` 或字段是否存在；suite 自身不是任何 Adapter 的实现，
B3 用来证明 suite 有效的内存假实现（`tests/fake_adapters.py`）也不是。
"""
