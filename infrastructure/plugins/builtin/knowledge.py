"""Static `PluginManifest` for the built-in `KnowledgeProvider` (`plugins/knowledge/local.py`).

`LocalKnowledgeProvider` is offline and deterministic (reviewed JSON files under
`docs/research/knowledge/`). `name` / `version` / `deterministic` are checked against a
constructed instance's own `descriptor` in
`tests/infrastructure/plugins/test_builtin_manifests.py`. Version `1.1.0` per
`plugins.knowledge.local` (ADR-0055, contract 2.2.0 tag filters); still `contract_version` major 2.
"""

from __future__ import annotations

from infrastructure.plugins.manifest import PluginKind, PluginManifest

__all__ = ["HLENS_KNOWLEDGE_LOCAL"]

HLENS_KNOWLEDGE_LOCAL = PluginManifest(
    name="hlens_knowledge_local",
    kind=PluginKind.KNOWLEDGE,
    version="1.1.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema={
        "type": "object",
        "properties": {
            "items_dir": {"type": "string", "description": "path to the item JSON files"}
        },
        "required": [],
        "additionalProperties": False,
    },
    inputs=(),
    outputs=("items:object",),
)
