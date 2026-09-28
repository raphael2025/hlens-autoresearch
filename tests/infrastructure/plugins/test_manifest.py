"""`infrastructure.plugins.manifest.PluginManifest`: strict parsing and invariants (ADR-0087).

Every branch of `__post_init__` / `from_mapping` gets one failing case (missing field, extra
field, illegal SemVer, unknown kind, illegal `params_schema`, a non-deterministic kind that must
be deterministic, an `available_lag` on a kind that may not declare one, an illegal duration, an
illegal `inputs` / `outputs` entry) plus the legal case each of them is a minimal mutation of, so a
future change that quietly drops a check shows up as a missing failure, not just a missing
success.
"""

from __future__ import annotations

import pytest

from infrastructure.plugins.manifest import (
    PluginKind,
    PluginManifest,
    PluginManifestError,
    SUPPORTED_CONTRACT_MAJOR,
    validate_params_schema,
)

_VALID_KWARGS: dict[str, object] = {
    "name": "example_feature",
    "kind": PluginKind.FEATURE,
    "version": "1.0.0",
    "contract_version": "2.0.0",
    "deterministic": True,
    "params_schema": {
        "type": "object",
        "properties": {"window": {"type": "integer", "minimum": 1}},
        "required": ["window"],
        "additionalProperties": False,
    },
    "inputs": ("representation:canonical_bar_1m@1.0.0",),
    "outputs": ("value:decimal",),
}


def _manifest(**overrides: object) -> PluginManifest:
    return PluginManifest(**{**_VALID_KWARGS, **overrides})  # type: ignore[arg-type]


def test_a_legal_manifest_constructs_and_exposes_plugin_key() -> None:
    manifest = _manifest()
    assert manifest.plugin_key == "example_feature@1.0.0"
    assert manifest.available_lag is None


# ---------------------------------------------------------------- name / version / kind


def test_illegal_name_is_rejected() -> None:
    with pytest.raises(PluginManifestError, match="name"):
        _manifest(name="Bad-Name")


def test_illegal_version_is_rejected() -> None:
    with pytest.raises(PluginManifestError, match="version"):
        _manifest(version="1.0")


def test_wrong_kind_type_is_rejected() -> None:
    with pytest.raises(PluginManifestError, match="kind"):
        _manifest(kind="feature")  # a raw str, not PluginKind


# ---------------------------------------------------------------- contract_version major


def test_contract_version_major_must_match_the_current_contract_major() -> None:
    with pytest.raises(PluginManifestError, match="major"):
        _manifest(contract_version=f"{SUPPORTED_CONTRACT_MAJOR + 1}.0.0")


def test_contract_version_illegal_semver_is_rejected() -> None:
    with pytest.raises(PluginManifestError, match="contract_version"):
        _manifest(contract_version="not-a-version")


# ---------------------------------------------------------------- deterministic


def test_a_feature_plugin_must_be_deterministic() -> None:
    with pytest.raises(PluginManifestError, match="deterministic"):
        _manifest(deterministic=False)


def test_knowledge_and_llm_may_be_non_deterministic() -> None:
    for kind in (PluginKind.KNOWLEDGE, PluginKind.LLM):
        manifest = _manifest(kind=kind, deterministic=False, inputs=(), outputs=("output:object",))
        assert manifest.deterministic is False


# ---------------------------------------------------------------- params_schema


def test_params_schema_must_be_a_mapping() -> None:
    with pytest.raises(PluginManifestError):
        _manifest(params_schema="not-a-schema")


def test_params_schema_rejects_unknown_top_level_keyword() -> None:
    with pytest.raises(PluginManifestError, match="unknown keyword"):
        _manifest(params_schema={"type": "object", "unexpected": True})


def test_params_schema_rejects_required_naming_an_undeclared_property() -> None:
    with pytest.raises(PluginManifestError, match="required"):
        _manifest(
            params_schema={
                "type": "object",
                "properties": {"window": {"type": "integer"}},
                "required": ["window", "missing"],
            }
        )


def test_params_schema_rejects_unknown_type() -> None:
    with pytest.raises(PluginManifestError, match="type"):
        validate_params_schema({"type": "tensor"})


def test_params_schema_array_without_items_is_rejected() -> None:
    with pytest.raises(PluginManifestError, match="items"):
        validate_params_schema({"type": "array"})


def test_params_schema_nested_property_is_validated_too() -> None:
    with pytest.raises(PluginManifestError, match="unknown keyword"):
        _manifest(
            params_schema={
                "type": "object",
                "properties": {"window": {"type": "integer", "bogus": 1}},
                "required": ["window"],
            }
        )


# ---------------------------------------------------------------- inputs / outputs


def test_inputs_entry_must_be_a_legal_ref() -> None:
    with pytest.raises(PluginManifestError, match="Ref"):
        _manifest(inputs=("not-a-ref",))


def test_outputs_must_be_non_empty() -> None:
    with pytest.raises(PluginManifestError, match="outputs"):
        _manifest(outputs=())


def test_outputs_entry_must_match_the_name_type_token_shape() -> None:
    with pytest.raises(PluginManifestError, match="outputs"):
        _manifest(outputs=("Not Valid!",))


# ---------------------------------------------------------------- available_lag


def test_available_lag_is_accepted_for_a_feature_plugin() -> None:
    manifest = _manifest(available_lag="PT0S")
    assert manifest.available_lag == "PT0S"


def test_available_lag_is_rejected_for_a_kind_that_may_not_declare_one() -> None:
    with pytest.raises(PluginManifestError, match="available_lag"):
        _manifest(
            kind=PluginKind.BACKTEST,
            inputs=(),
            outputs=("equity_close:decimal",),
            available_lag="PT0S",
        )


def test_available_lag_must_be_a_legal_duration() -> None:
    with pytest.raises(PluginManifestError, match="duration"):
        _manifest(available_lag="1 hour")


def test_bare_pt_is_not_a_legal_duration() -> None:
    with pytest.raises(PluginManifestError, match="duration"):
        _manifest(available_lag="PT")


# ---------------------------------------------------------------- from_mapping: strict parsing


def _mapping(**overrides: object) -> dict[str, object]:
    data = {
        "name": "example_feature",
        "kind": "feature",
        "version": "1.0.0",
        "contract_version": "2.0.0",
        "deterministic": True,
        "params_schema": {"type": "object", "properties": {}, "required": []},
        "inputs": [],
        "outputs": ["value:decimal"],
    }
    data.update(overrides)
    return data


def test_from_mapping_accepts_a_legal_mapping() -> None:
    manifest = PluginManifest.from_mapping(_mapping())
    assert manifest.name == "example_feature"
    assert manifest.kind is PluginKind.FEATURE
    assert manifest.inputs == ()
    assert manifest.outputs == ("value:decimal",)


def test_from_mapping_accepts_an_optional_available_lag() -> None:
    manifest = PluginManifest.from_mapping(_mapping(available_lag="PT1H"))
    assert manifest.available_lag == "PT1H"


def test_from_mapping_rejects_a_missing_field() -> None:
    data = _mapping()
    del data["version"]
    with pytest.raises(PluginManifestError, match="missing"):
        PluginManifest.from_mapping(data)


def test_from_mapping_rejects_an_unexpected_field() -> None:
    with pytest.raises(PluginManifestError, match="unexpected"):
        PluginManifest.from_mapping(_mapping(extra_field="nope"))


def test_from_mapping_rejects_an_unknown_kind() -> None:
    with pytest.raises(PluginManifestError, match="unknown plugin kind"):
        PluginManifest.from_mapping(_mapping(kind="not_a_kind"))


def test_from_mapping_rejects_a_non_mapping() -> None:
    with pytest.raises(PluginManifestError):
        PluginManifest.from_mapping(["not", "a", "mapping"])  # type: ignore[arg-type]


def test_from_mapping_rejects_non_string_inputs_entries() -> None:
    with pytest.raises(PluginManifestError, match="inputs"):
        PluginManifest.from_mapping(_mapping(inputs=[123]))
