"""ADR-0100 item 4: the default ``AuthorityEnvironment`` factory
(``research.operations.authority_environment``).

Everything the factory pins before it connects anything — settings, artifact files, the recorded
provider identities, the decision pipeline's identity — is checked here; a refusal is always
``authority_environment_unavailable`` naming settings, never their values. The identities the
default environment builds are verified with the resolver's own check (``_check_execution``), so
the two cannot drift. The plugin registry is fed the project's **declared** entry points
(``pyproject.toml``; they become ``importlib.metadata`` entry points only after a ``uv sync`` of
the project), the way ``infrastructure.plugins.discovery``'s own tests inject a source.

The data plane itself (PostgreSQL catalog, storage, v3 verifier) is not opened here. Every number
and artifact is TEST ONLY.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any

import pytest

from core.contracts.outcome import OutcomeLabelSpec, OutcomeMethod
from core.domain.base import Kind, Ref, canonical_json
from core.domain.research import ExperimentRun
from core.domain.specs import OutcomeSpec, StrategySpec
from infrastructure.plugins import discovery
from plugins.backtest import BarBacktester
from plugins.outcomes import ForwardReturnOutcome
from research.experiments.run_inputs import RUN_INPUTS_KEY, with_run_inputs
from research.operations import authority_environment as ae
from research.operations.authority import WindowExecution, _check_execution
from research.operations.authority_environment import (
    EVIDENCE_VERIFIER_ARGUMENT,
    AuthorityEnvironmentSettings,
    AuthorityEnvironmentUnavailable,
    default_environment,
)
from research.operations.degradation import ObservationWindow
from research.operations.degradation_cli import AUTHORITY_ENVIRONMENT_UNAVAILABLE
from research.strategies.time_series_momentum import TimeSeriesMomentumProvider, tsmom_spec
from tests import factories
from tests.research.operations import authority_fixtures as af

ROOT = Path(__file__).resolve().parents[3]
INSTRUMENTS = ("BTC-USDT",)
SPEC = tsmom_spec()
LABEL = OutcomeLabelSpec.bind(
    OutcomeSpec(
        name="authority_env_forward_1m",
        version="1.0.0",
        created_at=datetime(2023, 11, 1, tzinfo=UTC),
        horizon=af.MINUTE,
        label_definition="one-minute forward return (environment test)",
    ),
    OutcomeMethod.FORWARD_RETURN,
)
REQUIRED = [
    "HLENS_AUTHORITY_BASELINE_RUN",
    "HLENS_AUTHORITY_BASELINE_MANIFEST_HASH",
    "HLENS_AUTHORITY_STRATEGY_SPEC",
    "HLENS_AUTHORITY_COST_MODEL",
    "HLENS_AUTHORITY_LABEL_SPEC",
    "HLENS_AUTHORITY_INITIAL_EQUITY",
    "HLENS_AUTHORITY_DECISION_STEP",
    "HLENS_AUTHORITY_DECISION_WARMUP",
]


def _declared_source(group: str) -> Iterable[EntryPoint]:
    """The project's declared ``hlens.plugins.<kind>`` entry points (``pyproject.toml``)."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = project["entry-points"].get(group, {})
    return [EntryPoint(name=name, value=value, group=group) for name, value in declared.items()]


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No ``HLENS_*`` variable and no ``.env`` from the developer's machine; the declared
    plugin entry points."""
    for name in list(os.environ):
        if name.startswith("HLENS_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    real = discovery.discover
    monkeypatch.setattr(ae, "discover", lambda kind: real(kind, source=_declared_source))


def _plugin(descriptor: Any) -> tuple[str, str]:
    return f"{descriptor.name}@{descriptor.version}", descriptor.content_hash()


def _run(**overrides: Any) -> ExperimentRun:
    """A baseline run of the tsmom spec as the research loop records it (TEST ONLY values)."""
    strategy = TimeSeriesMomentumProvider((SPEC,))
    outcome = ForwardReturnOutcome((LABEL,))
    plugins = dict(
        [
            _plugin(BarBacktester().descriptor),
            _plugin(strategy.descriptor),
            _plugin(outcome.descriptor),
        ]
    )
    deps = {
        str(factories.hypothesis_ref()): factories.HASH_A,
        str(SPEC.ref): SPEC.content_hash(),
        str(LABEL.outcome): LABEL.outcome_spec_hash,
        str(af.COST_MODEL.ref): af.COST_MODEL.content_hash(),
    }
    values: dict[str, Any] = {
        "strategy_ref": SPEC.ref,
        "risk_policy_ref": None,
        "outcome_ref": LABEL.outcome,
        "cost_model_ref": af.COST_MODEL.ref,
        "dependency_hashes": deps,
        "plugin_versions": plugins,
        "params": with_run_inputs(dict(SPEC.params), af.run_inputs()),
    }
    values.update(overrides)
    return factories.experiment_run(repro=factories.repro_tuple(**values))


def _write(tmp_path: Path, name: str, payload: Any) -> Path:
    path = tmp_path / f"{name}.json"
    path.write_text(canonical_json(payload), encoding="utf-8")
    return path


def _settings(tmp_path: Path, run: ExperimentRun | None = None, **changes: Any) -> Any:
    values: dict[str, Any] = {
        "baseline_run": _write(tmp_path, "run", (run or _run()).model_dump(mode="json")),
        "baseline_manifest_hash": "a" * 64,
        "strategy_spec": _write(tmp_path, "spec", SPEC.model_dump(mode="json")),
        "cost_model": _write(tmp_path, "cost", af.COST_MODEL.model_dump(mode="json")),
        "label_spec": _write(tmp_path, "label", LABEL.model_dump(mode="json")),
        "initial_equity": af.INITIAL_EQUITY,
        "decision_step": af.MINUTE,
        "decision_warmup": timedelta(0),
    }
    values.update(changes)
    return AuthorityEnvironmentSettings(_env_file=None, **values)  # type: ignore[call-arg]


def _unavailable(call: Any, *names: str) -> AuthorityEnvironmentUnavailable:
    with pytest.raises(AuthorityEnvironmentUnavailable) as refused:
        call()
    assert refused.value.code == AUTHORITY_ENVIRONMENT_UNAVAILABLE
    for name in names:
        assert name in refused.value.missing_settings
    return refused.value


# ---- the factory's arguments and settings -------------------------------------------------


@pytest.mark.parametrize("verifier", [None, ""])
def test_the_evidence_verifier_must_be_given_explicitly(verifier: str | None) -> None:
    refused = _unavailable(lambda: default_environment(evidence_verifier=verifier))
    assert refused.missing_settings == (EVIDENCE_VERIFIER_ARGUMENT,)


def test_entering_without_settings_names_every_missing_one_without_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HLENS_AUTHORITY_INITIAL_EQUITY", "not-a-number")
    factory = "tests.research.operations.authority_fixtures:environment_factory"
    with pytest.raises(AuthorityEnvironmentUnavailable) as refused:
        with default_environment(evidence_verifier=factory):
            pytest.fail("nothing may be built without the settings")
    names = refused.value.missing_settings
    assert all(name.startswith("HLENS_") for name in names)
    assert "HLENS_CATALOG_URI" in names and "HLENS_AUTHORITY_INITIAL_EQUITY" in names
    assert "not-a-number" not in str(refused.value)


def test_every_required_authority_setting_is_named_at_once() -> None:
    empty = AuthorityEnvironmentSettings(_env_file=None)  # type: ignore[call-arg]
    refused = _unavailable(lambda: ae._pinned(empty))
    assert list(refused.missing_settings) == REQUIRED
    with_validation = AuthorityEnvironmentSettings(
        _env_file=None,  # type: ignore[call-arg]
        validation_metadata=Path("metadata.json"),
    )
    refused = _unavailable(lambda: ae._pinned(with_validation))
    assert list(refused.missing_settings) == [
        *REQUIRED,
        "HLENS_AUTHORITY_VALIDATION_SEED",
        "HLENS_AUTHORITY_ROBUSTNESS_PARAMS",
    ]


# ---- the pinned artifacts and providers ---------------------------------------------------


def test_the_default_identities_are_exactly_the_baseline_runs(tmp_path: Path) -> None:
    run = _run()
    pinned = ae._pinned(_settings(tmp_path, run))
    assert pinned.backtester.descriptor == BarBacktester().descriptor
    assert pinned.validation is None  # no validation metadata: no binding
    source = ae._target_source(pinned, INSTRUMENTS)
    assert RUN_INPUTS_KEY not in source.identity.params  # the strategy's params only
    assert dict(source.request_params) == dict(SPEC.params)
    execution = WindowExecution(
        backtester=pinned.backtester,
        cost_model=pinned.cost_model,
        initial_equity=pinned.initial_equity,
        targets=source,
    )
    descriptor, identity = _check_execution(execution, run)  # the resolver's own check
    assert descriptor == pinned.backtester.descriptor and identity == source.identity


def test_the_pipeline_targets_stay_inside_the_window(tmp_path: Path) -> None:
    pinned = ae._pinned(_settings(tmp_path))
    source = ae._target_source(pinned, INSTRUMENTS)
    bars = af.bars(INSTRUMENTS, af.START, 12)
    window = ObservationWindow(start=af.START, end=af.START + 12 * af.MINUTE, label="w")
    targets = source.targets(bars, window)
    assert all(window.contains(t.decision_time) for t in targets)
    assert all(t.instrument in INSTRUMENTS for t in targets)
    with pytest.raises(ValueError, match="no bar"):
        source.inputs((), window)


def test_a_label_spec_that_is_not_the_runs_outcome_is_refused(tmp_path: Path) -> None:
    other = OutcomeLabelSpec.bind(
        OutcomeSpec(
            name="authority_env_forward_5m",
            version="1.0.0",
            created_at=datetime(2023, 11, 1, tzinfo=UTC),
            horizon=5 * af.MINUTE,
            label_definition="five-minute forward return (environment test)",
        ),
        OutcomeMethod.FORWARD_RETURN,
    )
    settings = _settings(
        tmp_path, label_spec=_write(tmp_path, "other", other.model_dump(mode="json"))
    )
    _unavailable(lambda: ae._pinned(settings), "HLENS_AUTHORITY_LABEL_SPEC")


def test_unreadable_or_invalid_artifact_files_name_their_setting(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    _unavailable(
        lambda: ae._pinned(_settings(tmp_path, baseline_run=broken)),
        "HLENS_AUTHORITY_BASELINE_RUN",
    )
    _unavailable(
        lambda: ae._pinned(_settings(tmp_path, strategy_spec=tmp_path / "missing.json")),
        "HLENS_AUTHORITY_STRATEGY_SPEC",
    )
    not_a_spec = _write(tmp_path, "cost_as_spec", af.COST_MODEL.model_dump(mode="json"))
    _unavailable(
        lambda: ae._pinned(_settings(tmp_path, strategy_spec=not_a_spec)),
        "HLENS_AUTHORITY_STRATEGY_SPEC",
    )


def test_a_provider_constructed_with_other_inputs_is_refused(tmp_path: Path) -> None:
    plugins = dict(_run().repro.plugin_versions)
    strategy_key = _plugin(TimeSeriesMomentumProvider((SPEC,)).descriptor)[0]
    plugins[strategy_key] = "0" * 64  # the loop built it with other specs: another hash
    refused = _unavailable(lambda: ae._pinned(_settings(tmp_path, _run(plugin_versions=plugins))))
    assert "does not have the identity the baseline run recorded" in str(refused)


@pytest.mark.parametrize("change", ["wrong_hash", "none"])
def test_the_backtest_provider_must_be_exactly_the_recorded_one(
    tmp_path: Path, change: str
) -> None:
    plugins = dict(_run().repro.plugin_versions)
    key = BarBacktester().descriptor.plugin_key
    if change == "wrong_hash":
        plugins[key] = "0" * 64
    else:
        del plugins[key]
    refused = _unavailable(lambda: ae._pinned(_settings(tmp_path, _run(plugin_versions=plugins))))
    assert "backtest" in str(refused)


def test_a_strategy_reading_an_unconstructible_signal_is_refused() -> None:
    unknown = Ref(kind=Kind.FEATURE, name="bar_unknown_signal", version="1.0.0")
    spec = StrategySpec.model_validate(
        {**SPEC.model_dump(mode="json"), "signals": [unknown.model_dump(mode="json")]}
    )
    with pytest.raises(AuthorityEnvironmentUnavailable):
        ae._check_signal_refs(spec)
    ae._check_signal_refs(SPEC)  # the allowlisted bar log return is constructible


def test_a_float_parameter_cannot_be_requested() -> None:
    assert ae._request_params(SPEC, {"lookback": 60, "not_searched": 1}) == {"lookback": 60}
    with pytest.raises(AuthorityEnvironmentUnavailable):
        ae._request_params(SPEC, {"lookback": 60.0})


# ---- the validation binding's robustness parameters ---------------------------------------


def test_robustness_params_need_exactly_the_six_fields(tmp_path: Path) -> None:
    fields = {
        "cscv_partitions": 8,
        "max_participation_rate": None,
        "min_capacity": None,
        "impact_coefficient": 0.1,
        "cross_asset_min_positive_fraction": None,
        "max_undersampled_pnl_share": None,
    }
    params = ae._robustness(_write(tmp_path, "robust", fields))
    assert params.cscv_partitions == 8 and params.impact_coefficient == 0.1
    for broken in (
        {k: v for k, v in fields.items() if k != "min_capacity"},
        {**fields, "extra": 1},
        {**fields, "cscv_partitions": 8.0},
        {**fields, "cscv_partitions": True},
        {**fields, "min_capacity": "10"},
    ):
        _unavailable(
            lambda b=broken: ae._robustness(_write(tmp_path, "broken", b)),
            "HLENS_AUTHORITY_ROBUSTNESS_PARAMS",
        )


# ---- the evidence verifier factory --------------------------------------------------------


def test_an_evidence_verifier_factory_must_return_the_catalogs_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = "tests.research.operations.authority_fixtures:verifier_factory"
    monkeypatch.setattr(af, "FACTORY_ENVIRONMENT", ["not a verifier"])
    refused = _unavailable(
        lambda: ae._evidence_verifier(factory, object(), object()), EVIDENCE_VERIFIER_ARGUMENT
    )
    assert "did not return a StreamingEvidenceVerifier" in str(refused)
    failing = "tests.research.operations.authority_fixtures:failing_factory"
    refused = _unavailable(
        lambda: ae._evidence_verifier(failing, object(), object()), EVIDENCE_VERIFIER_ARGUMENT
    )
    assert "secret" not in str(refused)
