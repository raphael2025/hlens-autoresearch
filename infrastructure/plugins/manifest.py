"""Plugin manifest: data class and strict parsing (ADR-0087; docs/architecture/05-plugin.md §4).

A `PluginManifest` is the machine-readable identity + capability declaration every plugin
(built-in or third-party) carries: `name`, `kind`, `version`, the `contract_version` of the
Provider interface it implements, whether it is `deterministic`, its `params_schema` (a JSON
Schema *subset*, validated structurally — this module is not a general JSON Schema validator),
the `inputs` / `outputs` it declares, and — only for `feature` / `state` / `event` plugins — an
`available_lag`.

Two invariants this module enforces beyond format (05-plugin.md §3, §4):

- `deterministic` must be `True` for every kind except `knowledge` and `llm` (the only two the
  architecture allows to be non-deterministic, and only when they record every call).
- `contract_version`'s major must match `core.domain.base.CONTRACT_SCHEMA_MAJOR` — "the current
  contract major" ADR-0087 refers to. This module deliberately reuses that single existing
  constant rather than inventing a second, parallel "provider protocol version": a Provider's
  Protocol and DTOs are defined by, and evolve with, the same contract major as everything else
  in `core/contracts` (ADR-0030 / ADR-0035 / ADR-0036 / ADR-0038 were all published within major
  2). Infrastructure is allowed to depend on Domain (`docs/architecture/01-system.md` §4), so this
  import is not a layering violation.

`available_lag`: 05-plugin.md §4's sample YAML shows a single fixed `available_lag: PT0S` per
plugin. For every built-in `feature` / `state` / `event` provider in this codebase, `available_lag`
is instead a per-`FeatureSpec` / `StateSpec` / `EventSpec` parameter with **no default**
(H rule: no implicit default windows/lags) — a provider serves whatever `available_lag` its spec
declares, and the same provider class serves many specs with different lags. A single fixed value
on the class-level manifest would therefore misstate the plugin's actual behaviour. This module
keeps the field (third-party plugins may legitimately have one fixed lag) but makes it optional;
the built-in manifests in `infrastructure/plugins/builtin/` leave it unset and say so.

`inputs` / `outputs` are lightweight `Ref`-string / `name:type`-token declarations, not a full
Arrow schema — no such per-provider Arrow schema exists anywhere else in this codebase yet (Arrow
is used at the catalog/storage boundary, not at the Provider Protocol boundary), so inventing one
here would be unfounded. `outputs` tokens are documented as a minimal, honest label, not a
type system.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, cast

from core.domain.base import (
    CONTRACT_SCHEMA_MAJOR,
    NAME_PATTERN,
    REF_KEY_PATTERN,
    FrozenMapping,
    parse_semver,
)

__all__ = [
    "PluginKind",
    "PluginManifest",
    "PluginManifestError",
    "SUPPORTED_CONTRACT_MAJOR",
    "validate_params_schema",
]

_NAME_RE: Final = re.compile(NAME_PATTERN)
_REF_KEY_RE: Final = re.compile(REF_KEY_PATTERN)
#: ISO 8601 *time*-duration subset actually used by this codebase's lags (`PT0S`, `PT1H30M`, ...);
#: at least one numeric component is required (bare `PT` is not a duration).
_AVAILABLE_LAG_RE: Final = re.compile(
    r"^PT(?=.*[0-9])(?:[0-9]+H)?(?:[0-9]+M)?(?:[0-9]+(?:\.[0-9]+)?S)?$"
)
#: `outputs` token: `<name>:<type>` or `<name>:<type>(<detail>)`, e.g. `value:decimal`.
_OUTPUT_TOKEN_RE: Final = re.compile(r"^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*(?:\([a-z0-9_, ]*\))?$")

#: 05-plugin.md §4: only these three kinds may declare `available_lag`.
_LAG_ALLOWED_KINDS: Final = frozenset({"feature", "state", "event"})

_SCHEMA_TYPES: Final = frozenset(
    {"object", "string", "integer", "number", "boolean", "array", "null"}
)
#: `params_schema` object-level keywords this subset understands.
_OBJECT_KEYS: Final = frozenset(
    {"type", "properties", "required", "additionalProperties", "description", "title"}
)
#: `params_schema` scalar/array-level keywords this subset understands.
_SCALAR_KEYS: Final = frozenset(
    {
        "type",
        "description",
        "title",
        "enum",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "items",
    }
)


class PluginKind(StrEnum):
    """05-plugin.md §4 `kind`:
    `feature|state|event|outcome|strategy|risk|backtest|knowledge|llm|synthetic`."""

    FEATURE = "feature"
    STATE = "state"
    EVENT = "event"
    OUTCOME = "outcome"
    STRATEGY = "strategy"
    RISK = "risk"
    BACKTEST = "backtest"
    KNOWLEDGE = "knowledge"
    LLM = "llm"
    SYNTHETIC = "synthetic"


#: Kinds 05-plugin.md §3 allows to be non-deterministic (and only when every call is recorded).
_NONDETERMINISTIC_ALLOWED: Final = frozenset({PluginKind.KNOWLEDGE, PluginKind.LLM})

#: "The current contract major" ADR-0087 checks `contract_version` against — see module docstring.
SUPPORTED_CONTRACT_MAJOR: Final[int] = CONTRACT_SCHEMA_MAJOR


class PluginManifestError(ValueError):
    """A manifest failed strict parsing or one of its invariants; fail closed (ADR-0087)."""


def validate_params_schema(schema: object, *, path: str = "params_schema") -> None:
    """`schema` is a legal JSON Schema *subset*: reject unknown keywords, illegal `type`, illegal
    nesting. Not a general JSON Schema validator (no draft, no `$ref`, no `oneOf`/`anyOf`/`allOf`,
    no format keywords) — only the shape a plugin's own parameters realistically need."""
    if not isinstance(schema, Mapping):
        raise PluginManifestError(f"{path} must be an object")
    schema_type = schema.get("type")
    if isinstance(schema_type, str):
        types = {schema_type}
    elif isinstance(schema_type, Sequence) and not isinstance(schema_type, str) and schema_type:
        types = set(schema_type)
    else:
        raise PluginManifestError(f"{path}.type must be a string or non-empty array of strings")
    if not all(isinstance(item, str) for item in types):
        raise PluginManifestError(f"{path}.type entries must be strings")
    unknown_types = types - _SCHEMA_TYPES
    if unknown_types:
        raise PluginManifestError(f"{path}.type has unknown type(s): {sorted(unknown_types)}")
    if "object" in types:
        _validate_object_schema(schema, path)
    else:
        _validate_scalar_schema(schema, types, path)


def _validate_object_schema(schema: Mapping[str, object], path: str) -> None:
    unknown_keys = set(schema) - _OBJECT_KEYS
    if unknown_keys:
        raise PluginManifestError(f"{path} has unknown keyword(s): {sorted(unknown_keys)}")
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise PluginManifestError(f"{path}.properties must be an object")
    for key, sub_schema in properties.items():
        if not isinstance(key, str) or not key:
            raise PluginManifestError(f"{path}.properties keys must be non-empty strings")
        validate_params_schema(sub_schema, path=f"{path}.properties.{key}")
    required = schema.get("required", [])
    if (
        not isinstance(required, Sequence)
        or isinstance(required, str)
        or not all(isinstance(item, str) for item in required)
    ):
        raise PluginManifestError(f"{path}.required must be an array of strings")
    missing = set(required) - set(properties)
    if missing:
        raise PluginManifestError(f"{path}.required names undeclared properties: {sorted(missing)}")
    additional = schema.get("additionalProperties", False)
    if not isinstance(additional, bool):
        raise PluginManifestError(f"{path}.additionalProperties must be a bool")


def _validate_scalar_schema(schema: Mapping[str, object], types: set[str], path: str) -> None:
    unknown_keys = set(schema) - _SCALAR_KEYS
    if unknown_keys:
        raise PluginManifestError(f"{path} has unknown keyword(s): {sorted(unknown_keys)}")
    if "array" in types:
        items = schema.get("items")
        if items is None:
            raise PluginManifestError(f"{path}.items is required for an array type")
        validate_params_schema(items, path=f"{path}.items")
    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, Sequence) or isinstance(enum, str) or not enum):
        raise PluginManifestError(f"{path}.enum must be a non-empty array")


def _require_semver(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise PluginManifestError(f"{field_name} must be a string")
    try:
        parse_semver(value)
    except ValueError as exc:
        raise PluginManifestError(f"{field_name} is not a legal SemVer: {value!r}") from exc
    return value


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """A plugin's identity and capability declaration (05-plugin.md §4).

    Construct directly (every field already typed, e.g. the built-in manifests) or via
    `from_mapping` (untrusted input, e.g. an entry point's loaded object) — both paths run the
    same validation in `__post_init__`; there is exactly one place a manifest can be legal.
    """

    name: str
    kind: PluginKind
    version: str
    contract_version: str
    deterministic: bool
    params_schema: Mapping[str, object]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    available_lag: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PluginKind):
            raise PluginManifestError(f"kind must be a PluginKind, got {self.kind!r}")
        if not isinstance(self.name, str) or not _NAME_RE.fullmatch(self.name):
            raise PluginManifestError(
                f"name is not legal: {self.name!r} (must match {NAME_PATTERN})"
            )
        _require_semver(self.version, "version")
        _require_semver(self.contract_version, "contract_version")
        contract_major = int(parse_semver(self.contract_version).group("major"))
        if contract_major != SUPPORTED_CONTRACT_MAJOR:
            raise PluginManifestError(
                f"contract_version {self.contract_version!r} has major={contract_major}, "
                f"incompatible with the current contract major={SUPPORTED_CONTRACT_MAJOR}"
            )
        if not isinstance(self.deterministic, bool):
            raise PluginManifestError("deterministic must be a bool")
        if self.kind not in _NONDETERMINISTIC_ALLOWED and not self.deterministic:
            raise PluginManifestError(
                f"{self.kind.value} plugins must be deterministic=True (05-plugin.md §3)"
            )
        validate_params_schema(self.params_schema)
        if not isinstance(self.inputs, tuple) or not all(
            isinstance(item, str) for item in self.inputs
        ):
            raise PluginManifestError("inputs must be a tuple of strings")
        for ref in self.inputs:
            if not _REF_KEY_RE.fullmatch(ref):
                raise PluginManifestError(f"inputs entry is not a legal Ref: {ref!r}")
        if not isinstance(self.outputs, tuple) or not self.outputs:
            raise PluginManifestError("outputs must be a non-empty tuple of strings")
        for token in self.outputs:
            if not isinstance(token, str) or not _OUTPUT_TOKEN_RE.fullmatch(token):
                raise PluginManifestError(
                    f"outputs entry is not a legal name:type token: {token!r}"
                )
        if self.available_lag is not None:
            if self.kind.value not in _LAG_ALLOWED_KINDS:
                raise PluginManifestError(
                    f"{self.kind.value} plugins must not declare available_lag "
                    "(only feature/state/event may)"
                )
            if not _AVAILABLE_LAG_RE.fullmatch(self.available_lag):
                raise PluginManifestError(
                    f"available_lag is not a legal ISO-8601 duration: {self.available_lag!r}"
                )

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> PluginManifest:
        """Strict parse of an untrusted mapping: missing / extra fields are rejected outright."""
        if not isinstance(data, Mapping):
            raise PluginManifestError("a manifest must be a mapping")
        required = {
            "name",
            "kind",
            "version",
            "contract_version",
            "deterministic",
            "params_schema",
            "inputs",
            "outputs",
        }
        optional = {"available_lag"}
        present = set(data)
        missing = required - present
        if missing:
            raise PluginManifestError(f"manifest is missing field(s): {sorted(missing)}")
        extra = present - required - optional
        if extra:
            raise PluginManifestError(f"manifest has unexpected field(s): {sorted(extra)}")
        kind_value = data["kind"]
        if not isinstance(kind_value, str):
            raise PluginManifestError("kind must be a string")
        try:
            kind = PluginKind(kind_value)
        except ValueError:
            raise PluginManifestError(f"unknown plugin kind: {kind_value!r}") from None
        inputs = _string_sequence(data["inputs"], "inputs")
        outputs = _string_sequence(data["outputs"], "outputs")
        params_schema = data["params_schema"]
        if not isinstance(params_schema, Mapping):
            raise PluginManifestError("params_schema must be a mapping")
        available_lag = data.get("available_lag")
        if available_lag is not None and not isinstance(available_lag, str):
            raise PluginManifestError("available_lag must be a string or absent")
        deterministic = data["deterministic"]
        if not isinstance(deterministic, bool):
            raise PluginManifestError("deterministic must be a bool")
        return cls(
            name=cast(str, data["name"]),
            kind=kind,
            version=cast(str, data["version"]),
            contract_version=cast(str, data["contract_version"]),
            deterministic=deterministic,
            params_schema=FrozenMapping(params_schema),
            inputs=tuple(inputs),
            outputs=tuple(outputs),
            available_lag=available_lag,
        )

    @property
    def plugin_key(self) -> str:
        """`name@version` (same syntax as `core.domain.base.PluginKey` /
        a reproducibility tuple key)."""
        return f"{self.name}@{self.version}"


def _string_sequence(value: object, field_name: str) -> tuple[str, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, str)
        or not all(isinstance(item, str) for item in value)
    ):
        raise PluginManifestError(f"{field_name} must be an array of strings")
    return tuple(value)
