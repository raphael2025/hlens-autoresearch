"""Unit tests for the C2 PyIceberg catalog adapter (injected SQLite catalog; no PostgreSQL).

These cover implementation details beyond the provider-agnostic contract suite. They are not
PostgreSQL integration evidence (ADR-0021 D-01 §6).
"""

from __future__ import annotations

import ast
import socket
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from pydantic import SecretStr
from pyiceberg.catalog.sql import SqlCatalog

from core.contracts.catalog import (
    BatchRejected,
    CommitOutcome,
    CommitRequest,
    UnknownTableDefinition,
)
from infrastructure.catalog import (
    CatalogIntegrityError,
    CatalogUnavailable,
    PyIcebergCatalogAdapter,
    RegisteredTableDefinition,
    TableDefinitionRegistry,
    open_postgres_catalog_adapter,
)
from infrastructure.settings import Settings
from tests.infrastructure.catalog.catalog_support import (
    ALPHA,
    ALPHA_V110,
    BETA,
    REGISTRY,
    RULE,
    SqliteCatalogHarness,
    make_batch,
)

REPO = Path(__file__).resolve().parents[3]
_FAKE_PASSWORD = "not-a-real-secret-5f0c1d"


def _request(
    batch: pa.Table, batch_id: str = "batch-a", parent: str | None = None
) -> CommitRequest:
    return CommitRequest(
        table=ALPHA.table,
        batch_id=batch_id,
        batch_fingerprint=RULE.fingerprint(batch),
        row_count=batch.num_rows,
        expected_parent_snapshot_id=parent,
    )


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def _unreachable_settings(tmp_path: Path) -> Settings:
    uri = f"postgresql+psycopg2://nobody:{_FAKE_PASSWORD}@127.0.0.1:{_closed_port()}/nothing_test"
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        catalog_uri=SecretStr(uri),
        catalog_name="c2t_unavailable",
        warehouse_uri=(tmp_path / "warehouse").as_uri(),
        staging_uri=(tmp_path / "staging").as_uri(),
    )


def _real_driver_error(tmp_path: Path) -> Exception:
    """A genuine database-driver exception from a refused PostgreSQL connection."""
    settings = _unreachable_settings(tmp_path)
    try:
        SqlCatalog("probe", uri=settings.catalog_uri.get_secret_value())
    except Exception as exc:
        return exc
    raise AssertionError("connecting to a closed port unexpectedly succeeded")


# --------------------------------------------------------------------------- definitions


def test_definition_hash_is_derived_from_content() -> None:
    again = RegisteredTableDefinition(
        table=ALPHA.table,
        definition_id=ALPHA.definition_id,
        version=ALPHA.version,
        schema=ALPHA.schema,
        fingerprint_rule=RULE,
        properties=dict(ALPHA.properties),
    )
    assert again.definition_hash == ALPHA.definition_hash
    assert len({ALPHA.definition_hash, BETA.definition_hash, ALPHA_V110.definition_hash}) == 3
    changed_property = RegisteredTableDefinition(
        table=ALPHA.table,
        definition_id=ALPHA.definition_id,
        version=ALPHA.version,
        schema=ALPHA.schema,
        fingerprint_rule=RULE,
    )
    assert changed_property.definition_hash != ALPHA.definition_hash


@pytest.mark.parametrize(
    "key", ["hlens.definition.hash", "commit.retry.num-retries", "format-version"]
)
def test_definition_rejects_adapter_owned_properties(key: str) -> None:
    with pytest.raises(ValueError, match="reserved"):
        RegisteredTableDefinition(
            table=ALPHA.table,
            definition_id="c2test.reserved",
            version="1.0.0",
            schema=ALPHA.schema,
            fingerprint_rule=RULE,
            properties={key: "x"},
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("table", "Bad.Name"), ("definition_id", "Bad Id"), ("version", "1.0")],
)
def test_definition_rejects_malformed_identity(field: str, value: str) -> None:
    kwargs: dict[str, Any] = {
        "table": ALPHA.table,
        "definition_id": "c2test.ok",
        "version": "1.0.0",
        "schema": ALPHA.schema,
        "fingerprint_rule": RULE,
        field: value,
    }
    with pytest.raises(ValueError):
        RegisteredTableDefinition(**kwargs)


def test_registry_rejects_duplicates_and_resolves_only_exact_bindings() -> None:
    with pytest.raises(ValueError, match="twice"):
        TableDefinitionRegistry((ALPHA, ALPHA))
    assert REGISTRY.resolve(ALPHA.binding) is ALPHA
    other_table = ALPHA.binding.model_copy(update={"table": BETA.table})
    with pytest.raises(UnknownTableDefinition):
        REGISTRY.resolve(other_table)


# --------------------------------------------------------------------------- adapter


def test_created_table_pins_binding_and_disables_pyiceberg_commit_retries(
    sqlite_harness: SqliteCatalogHarness,
) -> None:
    sqlite_harness.open_adapter().create_table(ALPHA.binding)
    properties = sqlite_harness.sql_catalog().load_table(("c2test", "alpha")).properties
    assert properties["commit.retry.num-retries"] == "0"
    assert properties["hlens.definition.hash"] == ALPHA.definition_hash
    assert properties["hlens.batch.fingerprint-rule"] == RULE.rule_id
    assert properties["write.parquet.compression-codec"] == "zstd"


def test_sqlite_catalog_and_warehouse_survive_adapter_reopen(
    sqlite_harness: SqliteCatalogHarness,
) -> None:
    writer = sqlite_harness.open_adapter()
    created = writer.create_table(ALPHA.binding)
    batch = make_batch(3, "restart")
    request = _request(batch)
    first = writer.commit_batch(request, batch)
    writer.close()

    reopened = sqlite_harness.open_adapter()
    info = reopened.load_table(ALPHA.table)
    assert info is not None and info.definition == created.definition
    assert info.current_snapshot == first.snapshot
    replay = reopened.commit_batch(request, batch)
    assert replay.outcome is CommitOutcome.ALREADY_COMMITTED
    assert replay.snapshot == first.snapshot
    rows = sqlite_harness.sql_catalog().load_table(("c2test", "alpha")).scan().to_arrow()
    assert rows.to_pylist() == batch.to_pylist()


def test_table_without_persisted_binding_fails_closed(sqlite_harness: SqliteCatalogHarness) -> None:
    catalog = sqlite_harness.sql_catalog()
    catalog.create_namespace("c2test")
    catalog.create_table(("c2test", "alpha"), schema=ALPHA.schema)
    adapter = sqlite_harness.open_adapter()
    with pytest.raises(CatalogIntegrityError):
        adapter.load_table(ALPHA.table)
    with pytest.raises(CatalogIntegrityError):
        adapter.create_table(ALPHA.binding)


@pytest.mark.parametrize(
    "batch",
    [
        pytest.param(make_batch(2, "a").to_pylist(), id="not-a-table"),
        pytest.param(
            make_batch(2, "a").cast(
                pa.schema(
                    [
                        pa.field("seq", pa.int32(), nullable=False),
                        pa.field("tag", pa.large_string(), nullable=False),
                        pa.field("price", pa.float64()),
                    ]
                )
            ),
            id="wrong-column-type",
        ),
        pytest.param(make_batch(2, "a").select(["seq", "tag"]), id="missing-column"),
    ],
)
def test_batch_that_is_not_the_registered_arrow_shape_is_rejected(
    sqlite_harness: SqliteCatalogHarness, batch: Any
) -> None:
    adapter = sqlite_harness.open_adapter()
    adapter.create_table(ALPHA.binding)
    request = _request(make_batch(2, "a"))
    with pytest.raises(BatchRejected):
        adapter.commit_batch(request, batch)
    info = adapter.load_table(ALPHA.table)
    assert info is not None and info.current_snapshot is None


def test_fingerprint_rule_output_is_checked(sqlite_harness: SqliteCatalogHarness) -> None:
    class Broken:
        rule_id = "c2test.broken@1.0.0"

        def fingerprint(self, batch: pa.Table) -> str:
            return "NOT-HEX"

    broken = RegisteredTableDefinition(
        table=ALPHA.table,
        definition_id=ALPHA.definition_id,
        version=ALPHA.version,
        schema=ALPHA.schema,
        fingerprint_rule=Broken(),
    )
    adapter = sqlite_harness.open_adapter(TableDefinitionRegistry((broken,)))
    adapter.create_table(broken.binding)
    batch = make_batch(1, "a")
    with pytest.raises(CatalogIntegrityError):
        adapter.commit_batch(_request(batch), batch)


def test_backend_failure_mid_commit_fails_closed(
    sqlite_harness: SqliteCatalogHarness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    driver_error = _real_driver_error(tmp_path / "probe")
    catalog = sqlite_harness.sql_catalog()
    adapter = sqlite_harness.open_adapter()
    adapter.create_table(ALPHA.binding)
    failing = PyIcebergCatalogAdapter(catalog, REGISTRY)

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise driver_error

    monkeypatch.setattr(catalog, "commit_table", refuse)
    batch = make_batch(2, "a")
    with pytest.raises(CatalogUnavailable) as raised:
        failing.commit_batch(_request(batch), batch)
    assert raised.value.__cause__ is None and _FAKE_PASSWORD not in str(raised.value)
    monkeypatch.setattr(catalog, "load_table", refuse)
    with pytest.raises(CatalogUnavailable):
        failing.load_table(ALPHA.table)
    monkeypatch.undo()
    info = adapter.load_table(ALPHA.table)
    assert info is not None and info.current_snapshot is None


def test_unreachable_postgres_fails_closed_without_local_state(tmp_path: Path) -> None:
    settings = _unreachable_settings(tmp_path)
    with pytest.raises(CatalogUnavailable) as raised:
        open_postgres_catalog_adapter(settings, REGISTRY)
    text = f"{raised.value!s} {raised.value!r}"
    assert _FAKE_PASSWORD not in text and "postgresql" not in text
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    assert list(tmp_path.iterdir()) == [], "no local substitute state may be created"
    assert not list(REPO.glob("*.sqlite")) and not list(REPO.glob("*.db"))


def test_adapter_repr_hides_connection_details(sqlite_harness: SqliteCatalogHarness) -> None:
    adapter = sqlite_harness.open_adapter()
    assert repr(adapter) == f"PyIcebergCatalogAdapter(catalog={sqlite_harness.catalog_name!r})"


# --------------------------------------------------------------------------- boundaries


def _code_strings(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_runtime_catalog_code_has_no_sqlite_or_pytest_branch() -> None:
    for module in (REPO / "infrastructure" / "catalog").glob("*.py"):
        source = module.read_text(encoding="utf-8")
        assert "pytest" not in source, module
        assert not [s for s in _code_strings(module) if "sqlite" in s.lower()], module


def test_sql_catalog_class_stays_out_of_core_and_application_layers() -> None:
    for layer in ("core", "application", "apps", "plugins", "research"):
        for path in (REPO / layer).rglob("*.py") if (REPO / layer).exists() else ():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            } | {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            }
            assert not {name for name in imported if name.split(".")[0] == "pyiceberg"}, path
            assert "SqlCatalog" not in _code_strings(path), path
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            assert "SqlCatalog" not in names, path
