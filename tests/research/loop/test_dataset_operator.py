"""ADR-0105 §5: the Dataset-sourced operator (``research.loop.dataset_operator`` and
``dataset_operator_config``), the parallel of the ADR-0074 synthetic operator.

No Validation Profile is frozen for production, so no configuration can run (ADR-0074 §2.8,
unchanged): these tests cover what holds before any state is opened — the command line, the
refusal of every configuration that is not fully real, the order of the checks (the trusted
catalog factory is never called before the configuration passed), the ``[source]`` table, the
semantic identity and the provider seal — and pin the synthetic operator's identity so the shared
helpers extracted for §5 provably did not change it. The durable path over a real dataset (v5
state with an ``operator_identity``) is in
``tests/infrastructure/e2e/test_research_loop_dataset_operator.py``. Every value is TEST ONLY.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from research.loop import dataset_operator, dataset_operator_config, operator_config
from research.loop.dataset_operator_config import (
    DATASET_PROVIDER_ROLES,
    DatasetInjectedProviders,
    DatasetOperatorConfig,
    DatasetSourceValues,
    build_dataset_providers,
)
from research.loop.dataset_source import DatasetRound
from research.loop.operator import EXIT_REFUSED
from research.loop.operator_config import (
    PROVIDER_ROLES,
    OperatorConfigError,
    ProviderIdentity,
)
from research.loop.operator_providers import OperatorProviderError
from tests.research.loop import loop_fixtures as fx

H1, H2, H3 = "1" * 64, "2" * 64, "3" * 64
#: The calls the recording catalog factory saw (``_recording_factory``).
FACTORY_CALLS: list[str] = []


def _recording_factory() -> object:
    """A TEST ONLY ``--catalog`` factory: records that it was called (it must not be)."""
    FACTORY_CALLS.append("called")
    raise AssertionError("the catalog factory ran before the configuration was verified")


def _providers(roles: tuple[str, ...]) -> tuple[ProviderIdentity, ...]:
    return tuple(
        ProviderIdentity(role=role, id=f"p_{role}", version="1.0.0", descriptor_hash="a" * 64)
        for role in roles
    )


def _wiring_values() -> Any:
    """Duck-typed ``WiringValues`` over the TEST ONLY loop fixtures (identity inputs only)."""
    w = fx.wiring(evolution=False)
    return SimpleNamespace(
        feature_spec=w.feature_spec,
        feature_chunk_bars=w.feature_chunk_bars,
        state_spec=w.state_spec,
        decision_step=w.decision_step,
        decision_warmup=w.decision_warmup,
        strategies=[
            SimpleNamespace(spec=c.spec, hypothesis_family_id=c.hypothesis_family_id)
            for c in w.strategies
        ],
        cost_model=w.cost_model,
        initial_equity=w.initial_equity,
        label_spec=w.label_spec,
        outcome_spec=w.label_spec,
        robustness=w.robustness,
        profile_selection=w.profile_selection,
        profile_selection_rule=w.profile_selection,
        declared_research_class="intraday",
        code_commit=w.code_commit,
        environment_lock=w.environment_lock,
    )


def _loop_values(*, synthetic: bool) -> Any:
    c = fx.config()
    values: dict[str, Any] = {
        "loop_id": c.loop_id,
        "seed": c.seed,
        "epoch": c.epoch,
        "cadence": c.cadence,
        "budget": c.budget,
        "family_id": c.family_id,
        "knowledge": c.knowledge,
        "max_new_hypotheses_per_round": 1,
        "max_reevaluations_per_round": 1,
        "hypothesis_compute_seconds": Decimal("0.5"),
        "compute_seconds_per_trial": Decimal(1),
        "validation_compute_seconds": Decimal(5),
        "state_compute_seconds": Decimal(1),
        "profile": c.profile,
        "constitution_version": "1.0.0",
        "llm_cost_units_per_call": Decimal(0),
    }
    if synthetic:
        values.update(
            market=c.market,
            minutes_per_round=c.minutes_per_round,
            compute_seconds_per_bar=c.compute_seconds_per_bar,
        )
    return SimpleNamespace(**values)


def _source(**changes: Any) -> DatasetSourceValues:
    values: dict[str, Any] = {
        "symbol": "BTC-USDT",
        "rounds": (DatasetRound(H1, H2), DatasetRound(H1, H3, sealed_manifest_hash=H2)),
        "ingest_compute_seconds": Decimal("0.5"),
    }
    values.update(changes)
    return DatasetSourceValues(**values)


# ---- the synthetic operator's identity is unchanged ---------------------------------------


def test_the_synthetic_operator_identity_is_unchanged() -> None:
    """Golden: the ADR-0074 identity of fixed TEST ONLY inputs, computed before ADR-0105 §5
    extracted ``wiring_identity`` (and recomputed with the pre-§5 module, 2026-10-02)."""
    identity = operator_config._operator_identity(
        _providers(PROVIDER_ROLES), _loop_values(synthetic=True), _wiring_values()
    )
    assert identity == "81f4173b520f17e47c6e01df803804ecbe99bbc3e1482e69e5843c0be73e8d50"


# ---- the dataset identity -----------------------------------------------------------------


def _dataset_identity(source: DatasetSourceValues | None = None, **loop: Any) -> str:
    values = _loop_values(synthetic=False)
    for key, value in loop.items():
        setattr(values, key, value)
    return dataset_operator_config._operator_identity(
        _providers(DATASET_PROVIDER_ROLES), source or _source(), values, _wiring_values()
    )


def test_the_dataset_identity_binds_the_source_and_every_loop_value() -> None:
    base = _dataset_identity()
    assert base == _dataset_identity()  # deterministic
    assert len(base) == 64
    changed = {
        _dataset_identity(_source(symbol="ETH-USDT")),
        _dataset_identity(_source(rounds=(DatasetRound(H1, H2),))),
        _dataset_identity(_source(rounds=(DatasetRound(H1, H2), DatasetRound(H1, H3)))),
        _dataset_identity(_source(ingest_compute_seconds=Decimal("0.75"))),
        _dataset_identity(seed=12),
        _dataset_identity(cadence=timedelta(days=2)),
        _dataset_identity(family_id="other_family"),
    }
    assert base not in changed and len(changed) == 7


def test_the_dataset_roles_are_the_synthetic_roles_without_the_market() -> None:
    assert DATASET_PROVIDER_ROLES == tuple(
        role for role in PROVIDER_ROLES if role != "synthetic_market_provider"
    )


# ---- the [source] table -------------------------------------------------------------------


def _source_table(**changes: Any) -> dict[str, Any]:
    table: dict[str, Any] = {
        "kind": "research_dataset",
        "symbol": "BTC-USDT",
        "ingest_compute_seconds": "0.5",
        "rounds": [
            {"feature_manifest_hash": H1, "price_manifest_hash": H2, "sealed_manifest_hash": False},
            {"feature_manifest_hash": H1, "price_manifest_hash": H3, "sealed_manifest_hash": H2},
        ],
    }
    table.update(changes)
    return table


def test_the_source_table_declares_the_rounds() -> None:
    source = dataset_operator_config._source(_source_table())
    assert source == _source()
    assert source.payload()["kind"] == "research_dataset"
    assert [item["sealed_manifest_hash"] for item in source.payload()["rounds"]] == [None, H2]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"kind": "synthetic"}, "kind must be"),
        ({"symbol": "SYN-USDT"}, "not a Canonical symbol"),
        ({"symbol": "btc-usdt"}, "not a Canonical symbol"),
        ({"rounds": []}, "non-empty array"),
        ({"rounds": "r"}, "non-empty array"),
        ({"ingest_compute_seconds": 0.5}, "decimal"),
        ({"ingest_compute_seconds": "-1"}, "ingest_compute_seconds"),
        (
            {
                "rounds": [
                    {
                        "feature_manifest_hash": "abc",
                        "price_manifest_hash": H2,
                        "sealed_manifest_hash": False,
                    }
                ]
            },
            "feature_manifest_hash",
        ),
        (
            {
                "rounds": [
                    {
                        "feature_manifest_hash": H1,
                        "price_manifest_hash": H2,
                        "sealed_manifest_hash": True,
                    }
                ]
            },
            "sealed_manifest_hash",
        ),
        (
            {"rounds": [{"feature_manifest_hash": H1, "price_manifest_hash": H2}]},
            "sealed_manifest_hash",
        ),
        (
            {
                "rounds": [
                    {
                        "feature_manifest_hash": H1,
                        "price_manifest_hash": H2,
                        "sealed_manifest_hash": False,
                        "sealed_feature_manifest_hash": H3,
                    }
                ]
            },
            "sealed_feature_manifest_hash",
        ),
    ],
)
def test_an_invalid_source_table_is_refused(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(OperatorConfigError, match=message):
        dataset_operator_config._source(_source_table(**changes))


# ---- providers and the allowlist seal -----------------------------------------------------


def test_providers_must_come_from_the_static_allowlist() -> None:
    with pytest.raises(OperatorProviderError, match="DatasetOperatorConfig"):
        build_dataset_providers(cast(Any, object()))
    config = cast(Any, SimpleNamespace)  # only the providers' role order is read before building
    bad = DatasetOperatorConfig(
        schema_version="1.0.0",
        paths=config(),
        providers=_providers(tuple(reversed(DATASET_PROVIDER_ROLES))),
        source=_source(),
        loop=config(),
        wiring=_wiring_values(),
        freeze=config(),
        operator_identity="0" * 64,
    )
    with pytest.raises(OperatorProviderError, match="DATASET_PROVIDER_ROLES order"):
        build_dataset_providers(bad)
    good = replace(bad, providers=_providers(DATASET_PROVIDER_ROLES))
    unsealed = DatasetInjectedProviders(
        identities=good.providers,
        feature_provider=None,
        state_provider=None,
        strategy_provider=None,
        backtester=None,
        outcome_provider=None,
    )
    with pytest.raises(OperatorConfigError, match="static allowlist"):
        good.build(unsealed)
    with pytest.raises(OperatorConfigError, match="other identities"):
        good.build(replace(unsealed, identities=bad.providers))
    with pytest.raises(OperatorConfigError, match="DatasetInjectedProviders"):
        good.build(cast(Any, object()))


# ---- the command line and the order of the checks -----------------------------------------

#: The dataset schema with every value a placeholder (README template): always refused.
TEMPLATE = """\
schema_version = "1.0.0"

[operator]
llm_enabled = false

[paths]
state_dir = "<state directory>"
state_anchor = "<external state anchor>"
bus_anchor = "<external bus anchor>"
reports_root = "<reports directory>"
freeze_registry_dir = "<existing ADR-0062 registry directory>"
freeze_registry_anchor = "<existing external freeze anchor>"

[source]
kind = "research_dataset"
symbol = "<Canonical symbol>"
ingest_compute_seconds = "<seconds>"
rounds = [{ feature_manifest_hash = "<sha256>", price_manifest_hash = "<sha256>", \
sealed_manifest_hash = false }]
"""


def _cli(tmp_path: Path, *extra: str, text: str = TEMPLATE) -> list[str]:
    config = tmp_path / "operator.toml"
    config.write_text(text, encoding="utf-8")
    return ["run", "--config", str(config), "--rounds", "1", *extra]


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["run", "--rounds", "1", "--catalog", "m:f"],
        ["run", "--config", "c.toml", "--rounds", "0", "--catalog", "m:f"],
        ["run", "--config", "c.toml", "--rounds", "01", "--catalog", "m:f"],
        ["run", "--config", "c.toml", "--rounds", "1", "--catalog", "m:f", "--catalog", "m:g"],
        ["run", "--config", "c.toml", "--config", "d.toml", "--rounds", "1"],
        ["serve", "--config", "c.toml", "--rounds", "1"],
    ],
)
def test_usage_errors_exit_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        dataset_operator.main(argv)
    assert exited.value.code == 2


def test_without_a_catalog_nothing_is_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "never-read.toml"
    code = dataset_operator.main(["run", "--config", str(missing), "--rounds", "1"])
    err = capsys.readouterr().err
    assert code == EXIT_REFUSED == 2
    assert "--catalog" in err and "not configuration" in err
    assert not missing.exists() and list(tmp_path.iterdir()) == []


def test_a_catalog_from_both_sides_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exited:
        dataset_operator.main(
            _cli(tmp_path, "--catalog", "m:f"), catalog=cast(Any, SimpleNamespace())
        )
    assert exited.value.code == 2


def test_an_embedded_catalog_must_be_a_dataset_catalog(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = dataset_operator.main(_cli(tmp_path), catalog=cast(Any, SimpleNamespace()))
    assert code == 2
    assert "must be a DatasetCatalog" in capsys.readouterr().err


def test_a_placeholder_configuration_is_refused_before_the_catalog_factory_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    FACTORY_CALLS.clear()
    before = sorted(path.name for path in tmp_path.iterdir())
    code = dataset_operator.main(_cli(tmp_path, "--catalog", f"{__name__}:_recording_factory"))
    err = capsys.readouterr().err
    assert code == 2, err
    assert "placeholder" in err
    assert FACTORY_CALLS == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["operator.toml", *before]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("not toml [", "TOML"),
        ('schema_version = "2.0.0"\n', ""),
        ('schema_version = "1.0.0"\nnote = "a test-only value"\n', "TEST ONLY"),
    ],
)
def test_malformed_configurations_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str, message: str
) -> None:
    FACTORY_CALLS.clear()
    code = dataset_operator.main(
        _cli(tmp_path, "--catalog", f"{__name__}:_recording_factory", text=text)
    )
    err = capsys.readouterr().err
    assert code == 2, err
    assert message in err
    assert FACTORY_CALLS == []


def test_the_synthetic_schema_is_not_a_dataset_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A configuration naming a synthetic market (or lacking ``[source]``) is refused: the two
    operators never read each other's schema."""
    synthetic = TEMPLATE.split("[source]")[0] + '[loop]\nmarket = "m"\n'
    code = dataset_operator.main(
        _cli(tmp_path, "--catalog", f"{__name__}:_recording_factory", text=synthetic)
    )
    assert code == 2
    assert "refused" in capsys.readouterr().err
