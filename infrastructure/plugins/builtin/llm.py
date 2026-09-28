"""Static `PluginManifest` for the built-in `LLMProvider` (`plugins/llm/scripted.py`).

`ScriptedLLMProvider` is offline and deterministic (`network=False`) — it replays a scripted
output list, never a real model call. `name` / `version` / `deterministic` are checked against a
constructed instance's own `descriptor` in
`tests/infrastructure/plugins/test_builtin_manifests.py`.
"""

from __future__ import annotations

from infrastructure.plugins.manifest import PluginKind, PluginManifest

__all__ = ["HLENS_LLM_SCRIPTED"]

HLENS_LLM_SCRIPTED = PluginManifest(
    name="hlens_llm_scripted",
    kind=PluginKind.LLM,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema={
        "type": "object",
        "properties": {
            "outputs": {
                "type": "array",
                "items": {"type": "object"},
                "description": "the scripted output payloads to replay, in order",
            }
        },
        "required": ["outputs"],
        "additionalProperties": False,
    },
    inputs=(),
    outputs=("output:object",),
)
