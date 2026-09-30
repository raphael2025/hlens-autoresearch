"""Data Plane Adapter 的 Protocol 与 DTO 契约（Phase 1 B3，roadmap 验收 #6）。

依据 ADR-0017 / 0021 / 0022 / 0023。覆盖：三个 Protocol 的精确成员与签名、不以 runtime-checkable
冒充行为证据；15 个 DTO 的 JSON / Schema 往返、未知字段、frozen、`model_copy(update=...)` 重新
校验；对象 key 的路径逃逸反例与合法嵌套 key；SHA-256 / size / URI / 表名 / 覆盖区间 / 集合规范化；
Collector 的凭据形状拒绝；`core/` 的依赖边界与不硬编码 venue；注册表只追加、B3 之前的 current
Schema 与全部 v1 / 向量文件逐字节不变。

行为（staging / 原子发布 / 幂等 / 冲突 / 重启 / 重放）由 `tests/contract_suites/` 的公开 suite
检查，其有效性见 `tests/test_adapter_contract_suites.py`。
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import re
import types
import typing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.compat.v1 import V1_MODEL_NAMES
from core.contracts import _uri, catalog, collector, revision, storage
from core.contracts.catalog import (
    CatalogAdapter,
    CommitOutcome,
    CommitRequest,
    CommitResult,
    SnapshotInfo,
    TableDefinition,
    TableInfo,
    TableNameViolation,
    validate_table_name,
)
from core.contracts.collector import (
    CollectedObject,
    CollectionRequest,
    CollectionResult,
    CollectorAdapter,
    CollectorDescriptor,
    CoverageGap,
    GapReason,
    SourceBinding,
    validate_source_uri,
)
from core.contracts.registry import CONTRACT_MODELS, export_json_schemas
from core.contracts.storage import (
    ObjectKeyViolation,
    ObjectRef,
    PublishOutcome,
    PublishResult,
    StagedObject,
    StageRequest,
    StorageAdapter,
    validate_object_key,
    validate_object_uri,
)
from core.domain.base import CONTRACT_SCHEMA_VERSION, Contract, FrozenMapping
from tests.contract_suites.catalog import INVALID_TABLE_NAMES
from tests.contract_suites.storage import INVALID_OBJECT_KEYS
from tests.contract_version_support import as_published_at, as_published_at_2_0_0

REPO = Path(__file__).resolve().parents[1]
CURRENT_SCHEMA_DIR = REPO / "schemas"
LEGACY_SCHEMA_DIR = CURRENT_SCHEMA_DIR / "v1"
B3_MODULES = (storage, catalog, collector)

B3_MODELS: tuple[type[Contract], ...] = (
    StageRequest,
    StagedObject,
    ObjectRef,
    PublishResult,
    TableDefinition,
    SnapshotInfo,
    TableInfo,
    CommitRequest,
    CommitResult,
    SourceBinding,
    CollectorDescriptor,
    CollectionRequest,
    CollectedObject,
    CoverageGap,
    CollectionResult,
)

#: B3 之前 `CONTRACT_MODELS` 的 59 个模型（起点 b9e584d），顺序即导出顺序。
PRE_B3_MODEL_NAMES = (
    "Ref",
    "GitCodeRevision",
    "ContentBlobRef",
    "Instrument",
    "DatasetRef",
    "RepresentationSpec",
    "FeatureSpec",
    "StateSpec",
    "EventSpec",
    "OutcomeSpec",
    "StrategySpec",
    "RiskPolicy",
    "KnowledgeItem",
    "Hypothesis",
    "LlmCall",
    "ReproducibilityTuple",
    "ExperimentSpec",
    "ExperimentRun",
    "GateResult",
    "ValidationReport",
    "FailureRecord",
    "RetirementRecord",
    "ValidationProfile",
    "ProfileSelectionKey",
    "ProfileSelection",
    "SelectionEntry",
    "ProfileSelectionRule",
    "OosUnsealing",
    "ExperimentMetadata",
    "LifecycleTransition",
    "LifecycleHistory",
    "RiskGateRecord",
    "AuthorizationRecord",
    "ExecutionModeChange",
    "GoldenOutputs",
    "StrategyArtifact",
    "EquivalenceCheck",
    "DeploymentRecord",
    "PolicyBinding",
    "ObservationTimes",
    "AvailabilityDecision",
    "RevisionRecord",
    "PrecedenceEvidence",
    "RevisionGraph",
    "PointInTimeSpec",
    "PointInTimeSelection",
    "TradableInterval",
    "StableEpisodeKey",
    "DegradedEpisodeKey",
    "ListingRevision",
    "ListingHistory",
    "UniverseFilter",
    "UniverseSelectionSpec",
    "UniverseSpecBinding",
    "UniverseMember",
    "UniverseExclusion",
    "SelectedRevisionLineage",
    "AvailabilityEvidenceGap",
    "ResearchDatasetManifest",
)

#: 起点 b9e584d 的 59 份 current Schema 的 SHA-256：B3 只新增，不得改动任何一份。
#: ADR-0052 (2.1.0) added optional exact / Profile fields to these four schemas: pinned anew.
#: Current schemas changed by ADR-0055's fields (contract 2.2.0; the knowledge tag / asset fields).
ADR_0055_SCHEMA_SHA256 = {
    "KnowledgeItem": "e14ed123adaf2a6b8d536d037c2dd5d543b7a930187607466016fd4c27663d8c",
}
ADR_0052_SCHEMA_SHA256 = {
    "ExperimentMetadata": "0c80994796dbf6d13ea6b0fc4dd3e304923107e51ce9800cff1c2c81eb5ecc03",
    "GateResult": "9872878eeb55b9c075553dfcbce6fa8efd6c5b6621894e31b7d19d1eb2876b72",
    "ValidationProfile": "d2eb9781c9e84ca6dbb178d04a61e6870bb520a59b1abd4f28977983d2dde733",
    "ValidationReport": "72b741d92ed994b9959757d3b4940f922f82eca2b56a2c44526b290a9f11d851",
}
PRE_B3_SCHEMA_SHA256 = {
    "AuthorizationRecord": "c3b234ad9bbc55408bfe4e4d3c52936425d41f9ef37fd93ca6a5cae707421b0d",
    "AvailabilityDecision": "c2c7b023702bb22eba14187274a9dc489a7d29de1b9e5362e1bcad15fad83017",
    "AvailabilityEvidenceGap": "ccade7a868f3445041c2116d057cea6c056394fc48d108ea1492a2078d75880f",
    "ContentBlobRef": "f5054b1b689861bf6f6db9d6cc91afb4b1ea82b045736e8aa9c872cc63f6cd2f",
    "DatasetRef": "6f9d28fb365b8acfe5d15b93049bdc0f2e683b58794c07383e4776f9bed6d742",
    "DegradedEpisodeKey": "04f0e6e89fd83b5624b6c8b42b402ee0b557fec5a0430567422634f14357b2e0",
    "DeploymentRecord": "b5fb3016776d1b5aaad9dcf4b259a0c222a8e608feba60f04f1101279eef6c1d",
    "EquivalenceCheck": "6284f56c40bc2fe3b54680986cf0405a0d5ba3d0efc8d1f157a79a18bc444009",
    "EventSpec": "45aba997f73d4bf77ee7693546dd7ca218811bb000c51c7d28efbe92fed3354b",
    "ExecutionModeChange": "a1c7d7369e105dd43d28ab89deb20b998bf8c6796b179b63db11e7b1b357f56f",
    "ExperimentMetadata": "bc1b7e6edada1fa1197a33cbf93b4325ab83400a320a8380bc5ca6f3b65ea94f",
    "ExperimentRun": "1f81f864b56c4e60be4a705225ee3d2bef1cead42b9db93eed6454a1c2a8aeff",
    "ExperimentSpec": "f15ee8fb6761e86c7bda38831a52e02d8c1cd1537f73122dbdf74ea86ff3fcb2",
    "FailureRecord": "feb8aaefe86bcaf13d2541271ca9d8ac1f09c60fa0a7ef499dedf3196f874983",
    "FeatureSpec": "241372e342e14635fdd4f505d58b74e318da5af5952462d162edc59aaa8da471",
    "GateResult": "632ac23e608d767d573e68a411df66819fdb9e1859af60432249bc64640d08c6",
    "GitCodeRevision": "aa976d4fcdb09f973062c5eb9e816d03937f81a0c3c363307b15e5f61eebf1a0",
    "GoldenOutputs": "f58e08e2783421e3c07c2227fefd65c47d972b407e6880bad731ff1eec566c76",
    "Hypothesis": "a6b3a0caa6522389170249303ff082b5eb30201d62a6fd37d85056f377dea33e",
    "Instrument": "ebb85f8b2d8cc7124d82978f284f91f0832f860ad828e2ffdc4dc2e51fc69df6",
    "KnowledgeItem": "d82dbe9445c251c8ecb34d89d33cad57ef59a8a035c6dd81ee8360d470ea5edf",
    "LifecycleHistory": "0ba8ee2bb010948f6d96fc6261b92acf9d7252c09c29e1f81e29019ab9f143cd",
    "LifecycleTransition": "889c67ed274f9dbab787edc01ba3139e09cf9674fb99a1729c17abf0a3f66e23",
    "ListingHistory": "0c1aaa74e3e42c5de6ef2d6f2d583f5858a1a894c3c14d4c19b676b6a13a6aa4",
    "ListingRevision": "4b17f2743be4ef1bd880f44dded74ab37df0586f0ad67e471aaaff6b48d95bc0",
    "LlmCall": "58674b5ef44820b385170f54f2bbe04e651eda89820ce3cd93ac6473b4d1d49a",
    "ObservationTimes": "65cdf496c3096a90a3b3edf3482fef11ea27d3a6d2a0ff8f2af7c9942b5d8bb3",
    "OosUnsealing": "736fc8793882646edff095e47b98b9540428ad9e43f7c42a9303928dbc74a4b8",
    "OutcomeSpec": "135ff531441cf1eb0bd2c7f8082018bde465d1c328f6cfd2a9338a913ac3f45f",
    "PointInTimeSelection": "0e194da3e875f0b154bdb24d42b08240f6a343c8f112332fbbacfc2de63791b8",
    "PointInTimeSpec": "962e3d0c5e771498928477d81ee351fc7ef168f039be89d71f2b97e2a74dedbc",
    "PolicyBinding": "0c2ae9395ce52099d0ff443f4ce88e01221ae299fa636af0b1a2ccb0f932cd00",
    "PrecedenceEvidence": "9b50164d4c69c7cd326d75c6f94b4ea06962c76e82437dc5d3ba8b7421c753c6",
    "ProfileSelection": "dfab68cdd87647a55ab38f9554da1aa4c5465a1ed2687bdcbab63ee897ae1573",
    "ProfileSelectionKey": "9f011297ee625428e09fe4674c82fb766890862ce85ee95eaa58dad6fe2369cf",
    "ProfileSelectionRule": "2b133a3c7f048e34b27ffa15929590916d1f0c458a9cf97452ea2090e4ff2f5d",
    "Ref": "75710a927d1c2f84dfb52a0d6979fa1e82b7ee30e683375dfec227fa3b8527d5",
    "RepresentationSpec": "f2ec1de7dc0c48a13b328ce3fb936c062cb7336e6b4a9349d1a095680dbd5917",
    "ReproducibilityTuple": "9183c12b72bd7d1ac20166bd34c923904264ca30273fda799e74b6cf9506ae16",
    "ResearchDatasetManifest": "72fac11f0af97aabd9faa1bd9ed4402072dec30d0bae9a1df2f31f24a52b08fa",
    "RetirementRecord": "6ff5d228e5b88d25d2399238880358133da66b96f65ac2c3c12a110e5c564513",
    "RevisionGraph": "9b47ddf752423fc4477c7e0d6ec4bd13ef6281d3c32f4fa8dfc2ed850b22356c",
    "RevisionRecord": "57f676ad427a96f6b7d8d4bdbfa5a17876065b19db28fa0a68f1680d5bbc8ad1",
    "RiskGateRecord": "22318c2c00e8c7a3fecd83a5ed82b2ed80f2e12c8a23ffd80b80dce06bbdc781",
    "RiskPolicy": "965d9734098cedaff856330372b6b984f8f2b577899417ee4c1b21ff74ac442a",
    "SelectedRevisionLineage": "30063af63eb74041a8a31bd4459155eb76d7513768c8f0bb65d53ed8a647f956",
    "SelectionEntry": "44e5935e433789600b0c5e7d77339c8cb9b7b3f54ad6bed3c937716d7c50d6d6",
    "StableEpisodeKey": "b6882391cf3f8c6f64b9d7414275e13abe50cbd1cc3dadbced61c6f39adcf6bb",
    "StateSpec": "33f77731eec82fb60ab66d97bc44563d1fc90ea395278e2050ab38e5725cbaaf",
    "StrategyArtifact": "39008854fe3ed72608c7e97c001b668ec09f007cff1bc4797021a6fae7e20a12",
    "StrategySpec": "fa6a2bc4dbf9ef5814f38ab73ea42a263f47882b659373006ebb73bb6b2ee9d1",
    "TradableInterval": "f2ef0d26937179ba7ecdc16e7141d0688215c83fb6285b53b2e964f92bed733f",
    "UniverseExclusion": "54212eaf41d15c11be91d3b9b49dcb3281d6a61e2bae11b82d9c4e91cef634f7",
    "UniverseFilter": "a4f7eb29cba6474b804ee7806043ab934a7fc11d65d2fcd128249dba1508ea5b",
    "UniverseMember": "0b50a5cb86bdfd06ccc2aae328950b7eed3c42dba2734aa18871874418a2894b",
    "UniverseSelectionSpec": "5c1a118d61209511f960078c36312370b7beecb49ad4fd0eac22e049cafc5d7f",
    "UniverseSpecBinding": "bd33a54872e67747b6b5b8a6a5e9beca3d59d1ebbd53d8499ce9dceca2daa607",
    "ValidationProfile": "93d8ca00544fb7d19b54c64f3dca01a0ccb564134f6e634296020f1490da2857",
    "ValidationReport": "f119e1d3c9ceeb3130ff91781bac4f069b06ee3d7064dd6f2c99351347b3424e",
}

#: 起点 b9e584d 的 `schemas/v1/` 全部文件与 `tests/vectors/` 全部文件的 SHA-256。
FROZEN_FILE_SHA256 = {
    "schemas/v1/AuthorizationRecord.schema.json": (
        "f4a4385f6b43854d043cded57123a4ab9921415c431174636e20cd94b641ec4b"
    ),
    "schemas/v1/DatasetRef.schema.json": (
        "54b9be25175ddc68e2428935a87edea7d04df6d26161f867072f7e2d44ea776b"
    ),
    "schemas/v1/DeploymentRecord.schema.json": (
        "9733c1642fca9ec678e0ef666733a28863f28a707beee1774bfc83169ce07c0a"
    ),
    "schemas/v1/EquivalenceCheck.schema.json": (
        "578438017b1f1e2e11766e6e48119d6da2326ed6092b3bc375fd5f441a5713d3"
    ),
    "schemas/v1/EventSpec.schema.json": (
        "b7d88e642cefd2fa976a58abe7d50803b73c0df317f5f42a08acdec4ee8f6e6a"
    ),
    "schemas/v1/ExecutionModeChange.schema.json": (
        "1ff8b5a48a8ed7d00088d29a6775250151d9e03fd292a507acfb951d5bf4260a"
    ),
    "schemas/v1/ExperimentMetadata.schema.json": (
        "7602efe086c564c428d0f8d1b73b7003542f9137f4610375135912cf3b2fa4a9"
    ),
    "schemas/v1/ExperimentRun.schema.json": (
        "37d1c04bd01677c5d785acd85304e55265dfe6024c6d4d1e138469644d72e088"
    ),
    "schemas/v1/ExperimentSpec.schema.json": (
        "2c4b6ff2d89f1f33731fb55e932ad54eb80f3dc1fb5ca93d10ffe7a143655ba3"
    ),
    "schemas/v1/FailureRecord.schema.json": (
        "d367842265f2ea39d78ae226a5e21259ffe6316359e41ad423dbe0c142d94636"
    ),
    "schemas/v1/FeatureSpec.schema.json": (
        "1078354a5e9fd6dbf6df6904db19043cf8e457c2b4285fdac3ef8db5f4f9e733"
    ),
    "schemas/v1/GateResult.schema.json": (
        "e199728b77611034c7b1b77a5a173accc98946a74906c53f3842bfdb3987f8f2"
    ),
    "schemas/v1/GoldenOutputs.schema.json": (
        "73e0bf9248a1eedc3a7e4b937a2ce66ec640ee1056adf532003812274abe93ff"
    ),
    "schemas/v1/Hypothesis.schema.json": (
        "81b567fde5643d5733537aa0b36aa9ba015221d69e85e08008394b9390ac0169"
    ),
    "schemas/v1/Instrument.schema.json": (
        "bfb21c0b53fda00503456c66bf8163e5dcf95c64176eda3de24f7fe639b67880"
    ),
    "schemas/v1/KnowledgeItem.schema.json": (
        "032108b642be97ab30260999e63c778ce15d76b4b9b2dded64d11ae8d861740c"
    ),
    "schemas/v1/LifecycleHistory.schema.json": (
        "f574754c0b641f36fe41e275f153f97585caabbf27b1774673096a9d62b04cfd"
    ),
    "schemas/v1/LifecycleTransition.schema.json": (
        "8bc405172363e64a3b778333275b01b1f6189f718b20032371d3ca848400d33b"
    ),
    "schemas/v1/LlmCall.schema.json": (
        "27829b71324106ab6617271c25001f9bfa0868c7f9a046da94e77e783d0d4d05"
    ),
    "schemas/v1/OosUnsealing.schema.json": (
        "352b09cfe2fc42cb183e01aca9f5b9488d3514ab9007c2be1100e0c29f6a7b13"
    ),
    "schemas/v1/OutcomeSpec.schema.json": (
        "b2a62750a02b1d7c4204ac7e442b7f86b633665dd458da93bb0cac58fd8f5985"
    ),
    "schemas/v1/ProfileSelectionKey.schema.json": (
        "07098d303a25b23bb37771e6ff16bf412a1f349955aed30cd41efb57175bbcd2"
    ),
    "schemas/v1/ProfileSelectionRule.schema.json": (
        "83810b3854bd9bedf87a531ade9bc199765f6c63fe70ea4e5ab134ca2ba4eb7a"
    ),
    "schemas/v1/README.md": "f0958d3e599efae9af20531a2b0eaa7a951b1ba0f0ae096bc50688bbba51208f",
    "schemas/v1/Ref.schema.json": (
        "4ac3ee99bd0552af3d05662ae0e21a6a55de174f6a6f63d6fbd08ff9a1d0b3fd"
    ),
    "schemas/v1/RepresentationSpec.schema.json": (
        "b28b2de8af0fed3e3e20f79b4016a6228404f9c771332c9105de5f26f325633b"
    ),
    "schemas/v1/ReproducibilityTuple.schema.json": (
        "3e5d7d9d6a2c2753694ea177522afa8575c43ec19481d1fa149338111f8c675c"
    ),
    "schemas/v1/RetirementRecord.schema.json": (
        "9acbd9b3879eced1ba51d31182766901d0df46a4fc8d01e9e88dff60a7712ba1"
    ),
    "schemas/v1/RiskGateRecord.schema.json": (
        "03aef49d17492e210a3a020f8f00767a0273bb786a9d1376b6e17df7f249dd2d"
    ),
    "schemas/v1/RiskPolicy.schema.json": (
        "0003a8e96691e59e344f7b2bb7cf9cc7494d6d7334c24703e995c64819bab49a"
    ),
    "schemas/v1/SelectionEntry.schema.json": (
        "b2454d8de28019d2edf530e0d959a478424552ac25a9f8c3a11a75096981bdca"
    ),
    "schemas/v1/StateSpec.schema.json": (
        "a167fe9479daebc4de926e7337e8dae4fbcbd3103e39459f7d8a6e2a734bc25b"
    ),
    "schemas/v1/StrategyArtifact.schema.json": (
        "fbb881af5d2496b24d39adcaacc7c3e0ed5676075ab0520228bb0a33b0ef4efe"
    ),
    "schemas/v1/StrategySpec.schema.json": (
        "273f117d647ab118baf287e683aefddeeda91630d7b857fb29665e29a8f6ce8e"
    ),
    "schemas/v1/ValidationProfile.schema.json": (
        "955a01c319f28d98e001a60970b80f1ef577379060952d3a42b9f0b3cb6048d5"
    ),
    "schemas/v1/ValidationReport.schema.json": (
        "9508a4f3ae86bfa7ee3fff585d20b24937c26ed76b86489b4dcd003a995d3c76"
    ),
    "tests/vectors/v1/README.md": (
        "d866577ee83e995f14ac551b5f3ba089d1f508dcea36de8bbbc28cd141a68bd9"
    ),
    "tests/vectors/v1/experiment_spec.json": (
        "2ed4e04af21075e6ffff33595803328e64f0f5a13f6d0bd2df3fd8259e90eb0a"
    ),
    "tests/vectors/v1/reproducibility_tuple.json": (
        "2bcf93afd4da6665848ee171e2301559c14d98dcc123ae07be770e8aaf639b5f"
    ),
    "tests/vectors/v1/strategy_spec.json": (
        "6b31774b6afa72561b6b20cf791e99f1e84f6d0adb73a1f14cd6315cc739bae9"
    ),
    "tests/vectors/v1/validation_profile_draft.json": (
        "8bb41ca13712f0bea7ba081963e0c321533818885ad39bf853ee501ce3b0332f"
    ),
    "tests/vectors/v1/validation_profile_frozen.json": (
        "1d287953df6bf649bc07c8a9401945a73ca5accb192f05b19a7f38e04bfa4a29"
    ),
    "tests/vectors/v1_coverage/README.md": (
        "21cd858b839d3385d035b2ad2d1b4e984df02f3f7787afbceaf14e808b94c5f0"
    ),
    "tests/vectors/v1_coverage/llm_call.json": (
        "d45172b471690b94587a2e6901f818032c8d3c7d1bcccc163a0b0f091e4963e3"
    ),
}

T0 = datetime(2024, 1, 1, tzinfo=UTC)
T1 = datetime(2024, 1, 2, tzinfo=UTC)
T2 = datetime(2024, 1, 3, tzinfo=UTC)
SHA_A = hashlib.sha256(b"a").hexdigest()
SHA_B = hashlib.sha256(b"b").hexdigest()
KEY = "raw/archives/AAAUSD/2024-01-01/day=2024-01-01/archive.zip"


# ======================================================================================
# 合法实例构造器（数字与哈希都是测试数据）
# ======================================================================================


def object_ref(**update: Any) -> ObjectRef:
    fields: dict[str, Any] = {
        "key": KEY,
        "uri": f"file:///srv/warehouse/{KEY}",
        "sha256": SHA_A,
        "size": 1,
    }
    return ObjectRef.model_validate({**fields, **update})


def definition(**update: Any) -> TableDefinition:
    fields: dict[str, Any] = {
        "table": "canonical.trades",
        "definition_id": "hlens.canonical.trades",
        "version": "1.0.0",
        "definition_hash": SHA_A,
    }
    return TableDefinition.model_validate({**fields, **update})


def snapshot(**update: Any) -> SnapshotInfo:
    fields: dict[str, Any] = {
        "table": "canonical.trades",
        "snapshot_id": "2",
        "parent_snapshot_id": "1",
        "committed_at": T0,
        "batch_id": "batch-2",
        "batch_fingerprint": SHA_B,
        "added_rows": 3,
        "total_rows": 10,
    }
    return SnapshotInfo.model_validate({**fields, **update})


def commit_request(**update: Any) -> CommitRequest:
    fields: dict[str, Any] = {
        "table": "canonical.trades",
        "batch_id": "batch-2",
        "batch_fingerprint": SHA_B,
        "row_count": 3,
        "expected_parent_snapshot_id": "1",
    }
    return CommitRequest.model_validate({**fields, **update})


SOURCE = SourceBinding(source_id="example.public.archive", version="1.0.0")


def request(**update: Any) -> CollectionRequest:
    fields: dict[str, Any] = {
        "request_id": "req-1",
        "source": SOURCE,
        "data_type": "agg_trades",
        "symbols": ("BBBUSD", "AAAUSD"),
        "coverage_start": T0,
        "coverage_end": T2,
    }
    return CollectionRequest.model_validate({**fields, **update})


def collected(symbol: str, start: datetime, end: datetime, **update: Any) -> CollectedObject:
    key = f"raw/{symbol}/{start.date().isoformat()}.zip"
    fields: dict[str, Any] = {
        "ref": object_ref(key=key, uri=f"file:///srv/warehouse/{key}"),
        "symbol": symbol,
        "coverage_start": start,
        "coverage_end": end,
        "source_uri": f"https://archive.example.test/{symbol}/{start.date().isoformat()}.zip",
        "retrieved_at": T2 + timedelta(hours=1),
        "source_sha256": SHA_A,
        "source_metadata": {"etag": "abc", "last-modified": "Wed, 03 Jan 2024 00:00:00 GMT"},
    }
    return CollectedObject.model_validate({**fields, **update})


def gap(symbol: str, start: datetime, end: datetime) -> CoverageGap:
    return CoverageGap(
        symbol=symbol,
        coverage_start=start,
        coverage_end=end,
        reason=GapReason.SOURCE_ABSENT,
        detail="HTTP 404",
    )


def result(**update: Any) -> CollectionResult:
    fields: dict[str, Any] = {
        "request": request(),
        "collector_id": "example.daily-archive",
        "collector_version": "1.0.0",
        "objects": (
            collected("AAAUSD", T0, T1),
            collected("AAAUSD", T1, T2),
            collected("BBBUSD", T0, T1),
        ),
        "gaps": (gap("BBBUSD", T1, T2),),
    }
    return CollectionResult.model_validate({**fields, **update})


def descriptor(**update: Any) -> CollectorDescriptor:
    fields: dict[str, Any] = {
        "collector_id": "example.daily-archive",
        "version": "1.0.0",
        "sources": (SOURCE,),
        "network_origins": ("https://archive.example.test",),
    }
    return CollectorDescriptor.model_validate({**fields, **update})


VALID: dict[type[Contract], Contract] = {
    StageRequest: StageRequest(key=KEY, expected_sha256=SHA_A, expected_size=1),
    StagedObject: StagedObject(key=KEY, sha256=SHA_A, size=1, staging_id="stg-1"),
    ObjectRef: object_ref(),
    PublishResult: PublishResult(ref=object_ref(), outcome=PublishOutcome.CREATED),
    TableDefinition: definition(),
    SnapshotInfo: snapshot(),
    TableInfo: TableInfo(definition=definition(), current_snapshot=snapshot()),
    CommitRequest: commit_request(),
    CommitResult: CommitResult(
        request=commit_request(), snapshot=snapshot(), outcome=CommitOutcome.COMMITTED
    ),
    SourceBinding: SOURCE,
    CollectorDescriptor: descriptor(),
    CollectionRequest: request(),
    CollectedObject: collected("AAAUSD", T0, T1),
    CoverageGap: gap("AAAUSD", T0, T1),
    CollectionResult: result(),
}

#: 每个模型一处会被拒绝的 `model_copy(update=...)`：公开的复制更新路径与构造等价（ADR-0010 §D-13）。
INVALID_UPDATES: dict[type[Contract], dict[str, Any]] = {
    StageRequest: {"key": "../escape.zip"},
    StagedObject: {"size": -1},
    ObjectRef: {"uri": "file:///srv/../etc/passwd"},
    PublishResult: {"outcome": "overwritten"},
    TableDefinition: {"table": "trades"},
    SnapshotInfo: {"parent_snapshot_id": "2"},
    TableInfo: {"definition": definition(table="canonical.bars_1m")},
    CommitRequest: {"row_count": 0},
    CommitResult: {
        "outcome": CommitOutcome.COMMITTED,
        "snapshot": snapshot(parent_snapshot_id="9"),
    },
    SourceBinding: {"version": "1.0"},
    CollectorDescriptor: {"network_origins": ("http://archive.example.test",)},
    CollectionRequest: {"coverage_end": T0},
    CollectedObject: {"source_sha256": SHA_B},
    CoverageGap: {"detail": "   "},
    CollectionResult: {"gaps": ()},
}


def test_every_b3_model_has_a_valid_instance_and_an_invalid_update() -> None:
    assert set(VALID) == set(B3_MODELS) == set(INVALID_UPDATES)


# ======================================================================================
# 通用 DTO 性质
# ======================================================================================


@pytest.mark.parametrize("model", B3_MODELS, ids=lambda m: m.__name__)
def test_json_round_trip_is_lossless(model: type[Contract]) -> None:
    instance = VALID[model]
    again = model.model_validate_json(instance.model_dump_json())
    assert again == instance
    assert again.content_hash() == instance.content_hash()
    assert instance.schema_version == CONTRACT_SCHEMA_VERSION


@pytest.mark.parametrize("model", B3_MODELS, ids=lambda m: m.__name__)
def test_unknown_fields_are_rejected(model: type[Contract]) -> None:
    payload = VALID[model].model_dump(mode="json")
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "unexpected": 1})
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "api_key": "secret"})


@pytest.mark.parametrize("model", B3_MODELS, ids=lambda m: m.__name__)
def test_instances_are_frozen(model: type[Contract]) -> None:
    instance = VALID[model]
    field = next(name for name in model.model_fields if name != "schema_version")
    with pytest.raises(ValidationError):
        setattr(instance, field, getattr(instance, field))


@pytest.mark.parametrize("model", B3_MODELS, ids=lambda m: m.__name__)
def test_model_copy_update_revalidates(model: type[Contract]) -> None:
    instance = VALID[model]
    with pytest.raises(ValidationError):
        instance.model_copy(update=INVALID_UPDATES[model])
    with pytest.raises(ValidationError):
        instance.model_copy(update={"schema_version": "3.0.0"})


@pytest.mark.parametrize("model", B3_MODELS, ids=lambda m: m.__name__)
def test_foreign_major_is_rejected(model: type[Contract]) -> None:
    payload = VALID[model].model_dump(mode="json")
    for version in ("1.0.0", "3.0.0", "2.0", "v2.0.0"):
        with pytest.raises(ValidationError):
            model.model_validate({**payload, "schema_version": version})


@pytest.mark.parametrize("model", B3_MODELS, ids=lambda m: m.__name__)
def test_committed_schema_forbids_unknown_fields(model: type[Contract]) -> None:
    schema = (CURRENT_SCHEMA_DIR / f"{model.__name__}.schema.json").read_text("utf-8")
    exported = model.model_json_schema(mode="serialization")
    assert exported["additionalProperties"] is False
    assert f'"title": "{model.__name__}"' in schema


def test_naive_datetimes_are_rejected() -> None:
    naive = datetime(2024, 1, 1)
    with pytest.raises(ValidationError):
        snapshot(committed_at=naive)
    with pytest.raises(ValidationError):
        request(coverage_start=naive)
    with pytest.raises(ValidationError):
        collected("AAAUSD", T0, T1, retrieved_at=naive)


def test_offset_datetimes_are_normalized_to_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    shifted = request(coverage_start=T0.astimezone(plus_two), coverage_end=T2.astimezone(plus_two))
    assert shifted == request()
    assert shifted.coverage_start.tzinfo is UTC


# ======================================================================================
# StorageAdapter DTO：key / URI / hash / size
# ======================================================================================


@pytest.mark.parametrize("bad", INVALID_OBJECT_KEYS, ids=repr)
def test_invalid_object_keys_are_rejected_everywhere(bad: str) -> None:
    with pytest.raises(ObjectKeyViolation):
        validate_object_key(bad)
    with pytest.raises(ValidationError):
        StageRequest(key=bad, expected_sha256=SHA_A)
    with pytest.raises(ValidationError):
        StagedObject(key=bad, sha256=SHA_A, size=1, staging_id="stg-1")
    with pytest.raises(ValidationError):
        object_ref(key=bad)
    with pytest.raises(ValidationError):
        StageRequest.model_validate_json(json.dumps({"key": bad, "expected_sha256": SHA_A}))


def test_object_key_violation_is_a_value_error_and_not_a_normalizer() -> None:
    assert issubclass(ObjectKeyViolation, ValueError)
    with pytest.raises(ObjectKeyViolation):
        validate_object_key(3)
    for padded in (f" {KEY}", f"{KEY} ", f"{KEY}\n"):
        with pytest.raises(ValidationError):
            StageRequest(key=padded, expected_sha256=SHA_A)


@pytest.mark.parametrize(
    "key",
    [
        "a",
        "A0",
        "raw/archives/AAAUSD/2024-01-01/archive.zip",
        "raw/day=2024-01-01/part-0.parquet",
        "a/b/c/d/e/f/g/h",
        "x..y/z",
        "a" * 255,
        "/".join(["b" * 255] * 4),
    ],
)
def test_legal_object_keys_are_accepted_unchanged(key: str) -> None:
    assert validate_object_key(key) == key
    assert StageRequest(key=key, expected_sha256=SHA_A).key == key
    assert len(key) <= storage.OBJECT_KEY_MAX_LENGTH


@pytest.mark.parametrize(
    "uri",
    [
        "file:///srv/warehouse/raw/a.zip",
        "s3://bucket/raw/a.zip",
        "memory://store/a",
        "cas://h:9000/a",
        "cas://h:65535/a/b=1/c.parquet",
        "s3://my-bucket.example/raw/x..y",
    ],
)
def test_legal_object_uris(uri: str) -> None:
    assert validate_object_uri(uri) == uri
    assert object_ref(uri=uri).uri == uri


@pytest.mark.parametrize(
    "uri",
    [
        "",
        "raw/a.zip",
        "/srv/warehouse/a.zip",
        "FILE:///srv/a.zip",
        "file:/srv/a.zip",
        "file:///srv/../etc/passwd",
        "file:///srv/./a.zip",
        "file://user:pw@host/a.zip",
        "file://../a.zip",
        "file:///srv/a.zip?x=1",
        "file:///srv/a.zip#frag",
        "file:///srv/a b.zip",
        "file:///srv\\a.zip",
        "file:///srv/café.zip",
        # R2：`file` 必须是无远程 authority 的绝对路径
        "file://relative",
        "file://relative/a.zip",
        "file://host/srv/a.zip",
        "file://localhost/srv/a.zip",
        "file:///",
        "file:///srv/",
        "file:///srv//a.zip",
        "file:///srv/%2e%2e/etc/passwd",
        "file:///srv/%2E/a.zip",
        # R2：其它 scheme 必须有非空 authority 与非空对象路径
        "s3:///bucket/a.zip",
        "memory:///a",
        "s3://bucket",
        "s3://bucket/",
        "s3://bucket/a//b",
        "s3://user@bucket/a.zip",
        "s3://Bucket/a.zip",
        "s3://-bucket/a.zip",
        "s3://bucket../a.zip",
        "cas://h:0/a",
        "cas://h:0443/a",
        "cas://h:65536/a",
        "cas://h:/a",
        "cas://h:port/a",
        "s3://bucket/a.zip?versionId=1",
        "s3:bucket/a.zip",
    ],
)
def test_illegal_object_uris(uri: str) -> None:
    with pytest.raises(ValueError):
        validate_object_uri(uri)
    with pytest.raises(ValidationError):
        object_ref(uri=uri)


@pytest.mark.parametrize("digest", ["", SHA_A.upper(), SHA_A[:-1], SHA_A + "0", "g" * 64])
def test_illegal_sha256_is_rejected(digest: str) -> None:
    with pytest.raises(ValidationError):
        object_ref(sha256=digest)
    with pytest.raises(ValidationError):
        StageRequest(key=KEY, expected_sha256=digest)


@pytest.mark.parametrize("size", [-1, True, "1", 1.0])
def test_illegal_sizes_are_rejected(size: object) -> None:
    with pytest.raises(ValidationError):
        object_ref(size=size)
    with pytest.raises(ValidationError):
        StageRequest.model_validate({"key": KEY, "expected_sha256": SHA_A, "expected_size": size})


def test_expected_size_is_optional_and_zero_is_legal() -> None:
    assert StageRequest(key=KEY, expected_sha256=SHA_A).expected_size is None
    assert object_ref(size=0).size == 0


@pytest.mark.parametrize("staging_id", ["", " ", "../stg", "stg 1", "-stg"])
def test_illegal_staging_ids_are_rejected(staging_id: str) -> None:
    with pytest.raises(ValidationError):
        StagedObject(key=KEY, sha256=SHA_A, size=1, staging_id=staging_id)


def test_publish_outcomes_are_exactly_created_and_already_present() -> None:
    assert {outcome.value for outcome in PublishOutcome} == {"created", "already_present"}


# ======================================================================================
# CatalogAdapter DTO：表 / snapshot / batch 身份与提交绑定
# ======================================================================================


@pytest.mark.parametrize("bad", INVALID_TABLE_NAMES, ids=repr)
def test_invalid_table_names_are_rejected_everywhere(bad: str) -> None:
    with pytest.raises(TableNameViolation):
        validate_table_name(bad)
    for build in (
        lambda: definition(table=bad),
        lambda: snapshot(table=bad),
        lambda: commit_request(table=bad),
    ):
        with pytest.raises(ValidationError):
            build()


def test_table_identity_reuses_the_snapshot_binding_key_format() -> None:
    assert catalog.TABLE_NAME_PATTERN == revision.SNAPSHOT_TABLE_PATTERN
    for table in ("raw.binance_spot_archives", "canonical.trades", "quality.data_quality_reports"):
        assert validate_table_name(table) == table


@pytest.mark.parametrize("snapshot_id", ["", " ", "a b", "../1", "-1"])
def test_illegal_snapshot_and_batch_ids_are_rejected(snapshot_id: str) -> None:
    with pytest.raises(ValidationError):
        snapshot(snapshot_id=snapshot_id)
    with pytest.raises(ValidationError):
        commit_request(batch_id=snapshot_id)


def test_snapshot_shape_invariants() -> None:
    assert snapshot(parent_snapshot_id=None).parent_snapshot_id is None
    assert snapshot(batch_id=None, batch_fingerprint=None).batch_id is None
    for update in (
        {"parent_snapshot_id": "2"},
        {"batch_id": None},
        {"batch_fingerprint": None},
        {"added_rows": 11},
        {"total_rows": -1},
        {"added_rows": True},
    ):
        with pytest.raises(ValidationError):
            snapshot(**update)


def test_nullable_identities_must_be_explicit() -> None:
    """`parent_snapshot_id`、`expected_parent_snapshot_id`、`current_snapshot` 没有默认值。"""
    payload = snapshot().model_dump(mode="json")
    del payload["parent_snapshot_id"]
    with pytest.raises(ValidationError):
        SnapshotInfo.model_validate(payload)
    payload = commit_request().model_dump(mode="json")
    del payload["expected_parent_snapshot_id"]
    with pytest.raises(ValidationError):
        CommitRequest.model_validate(payload)
    with pytest.raises(ValidationError):
        TableInfo.model_validate({"definition": definition().model_dump(mode="json")})


def test_table_info_snapshot_must_belong_to_the_table() -> None:
    assert TableInfo(definition=definition(), current_snapshot=None).current_snapshot is None
    with pytest.raises(ValidationError):
        TableInfo(definition=definition(), current_snapshot=snapshot(table="canonical.bars_1m"))


@pytest.mark.parametrize("rows", [0, -1, True, "3"])
def test_commit_request_rejects_empty_or_non_integer_batches(rows: object) -> None:
    with pytest.raises(ValidationError):
        commit_request(row_count=rows)


@pytest.mark.parametrize(
    "snapshot_update",
    [
        {"table": "canonical.bars_1m"},
        {"batch_id": "batch-3"},
        {"batch_fingerprint": SHA_A},
        {"added_rows": 4},
        {"batch_id": None, "batch_fingerprint": None},
    ],
)
def test_commit_result_binds_snapshot_to_request(snapshot_update: dict[str, Any]) -> None:
    for outcome in CommitOutcome:
        with pytest.raises(ValidationError):
            CommitResult(
                request=commit_request(), snapshot=snapshot(**snapshot_update), outcome=outcome
            )


def test_committed_parent_must_match_but_replay_may_differ() -> None:
    stale = commit_request(expected_parent_snapshot_id=None)
    with pytest.raises(ValidationError):
        CommitResult(request=stale, snapshot=snapshot(), outcome=CommitOutcome.COMMITTED)
    replay = CommitResult(
        request=stale, snapshot=snapshot(), outcome=CommitOutcome.ALREADY_COMMITTED
    )
    assert replay.snapshot.parent_snapshot_id == "1"


def test_table_definition_is_a_binding_not_a_schema() -> None:
    """B3 不发明列级 Schema 或 partition spec：定义只是 `id + SemVer + hash` 绑定。"""
    assert list(TableDefinition.model_fields) == [
        "schema_version",
        "table",
        "definition_id",
        "version",
        "definition_hash",
    ]
    for bad in ({"definition_id": "Hlens"}, {"version": "1"}, {"definition_hash": "x"}):
        with pytest.raises(ValidationError):
            definition(**bad)


# ======================================================================================
# CollectorAdapter DTO：请求、结果覆盖关系、来源证据
# ======================================================================================


def test_request_symbols_are_a_canonical_set() -> None:
    assert request().symbols == ("AAAUSD", "BBBUSD")
    assert request(symbols=("AAAUSD", "BBBUSD")).content_hash() == request().content_hash()
    for bad in ((), ("AAAUSD", "AAAUSD"), ("AAA USD",), ("",), ("../x",), ("A" * 65,)):
        with pytest.raises(ValidationError):
            request(symbols=bad)


def test_request_symbols_are_case_sensitive_and_unnormalized() -> None:
    assert request(symbols=("aaausd", "AAAUSD")).symbols == ("AAAUSD", "aaausd")


@pytest.mark.parametrize(("start", "end"), [(T1, T0), (T0, T0)], ids=["reversed", "empty"])
def test_request_coverage_must_be_a_non_empty_half_open_interval(
    start: datetime, end: datetime
) -> None:
    with pytest.raises(ValidationError):
        request(coverage_start=start, coverage_end=end)
    with pytest.raises(ValidationError):
        gap("AAAUSD", start, end)
    with pytest.raises(ValidationError):
        collected("AAAUSD", start, end)


def test_request_requires_identity_source_and_data_type() -> None:
    for bad in (
        {"request_id": ""},
        {"request_id": "a b"},
        {"data_type": "AggTrades"},
        {"data_type": ""},
        {"source": {"source_id": "Example", "version": "1.0.0"}},
        {"source": {"source_id": "example.public", "version": "01.0.0"}},
    ):
        with pytest.raises(ValidationError):
            request(**bad)


def test_descriptor_sets_are_canonical() -> None:
    older = SourceBinding(source_id="example.public.archive", version="0.9.0")
    other = SourceBinding(source_id="another.source", version="1.0.0")
    forward = descriptor(
        sources=(SOURCE, older, other),
        network_origins=("https://b.example.test", "https://a.example.test"),
    )
    backward = descriptor(
        sources=(other, SOURCE, older),
        network_origins=("https://a.example.test", "https://b.example.test"),
    )
    assert forward == backward
    assert forward.content_hash() == backward.content_hash()
    assert forward.sources == (other, older, SOURCE)
    with pytest.raises(ValidationError):
        descriptor(sources=())
    with pytest.raises(ValidationError):
        descriptor(sources=(SOURCE, SOURCE))
    with pytest.raises(ValidationError):
        descriptor(network_origins=("https://a.example.test", "https://a.example.test"))
    assert descriptor(network_origins=()).network_origins == ()


@pytest.mark.parametrize(
    "origin",
    [
        "http://archive.example.test",
        "https://archive.example.test/",
        "https://archive.example.test/path",
        "https://user:pw@archive.example.test",
        "https://user@archive.example.test",
        "https://Archive.example.test",
        "https://archive.example.test?x=1",
        "wss://stream.example.test",
        "ftp://archive.example.test",
        "https://",
        "https://-bad.example.test",
        # R2：端口范围与无前导零只在运行时表达（Schema pattern 允许 1 ~ 5 位数字）
        "https://archive.example.test:0",
        "https://archive.example.test:00080",
        "https://archive.example.test:080",
        "https://archive.example.test:65536",
        "https://archive.example.test:99999",
        "https://archive.example.test:",
        "https://archive.example.test#f",
        "https://archive..example.test",
    ],
)
def test_illegal_network_origins_are_rejected(origin: str) -> None:
    with pytest.raises(ValidationError):
        descriptor(network_origins=(origin,))


def test_legal_network_origins() -> None:
    for origin in (
        "https://archive.example.test",
        "https://localhost:8443",
        "https://a-b.c1.test",
        "https://archive.example.test:1",
        "https://archive.example.test:443",
        "https://archive.example.test:65535",
    ):
        assert descriptor(network_origins=(origin,)).network_origins == (origin,)


def test_origin_schema_pattern_is_weaker_than_runtime() -> None:
    """Schema 端口 pattern 表达不了数值范围与无前导零（02-domain.md §3.7）；权威校验是运行时。"""
    pattern = re.compile(collector.NETWORK_ORIGIN_PATTERN)
    for origin in ("https://archive.example.test:00080", "https://archive.example.test:99999"):
        assert pattern.fullmatch(origin) is not None
        with pytest.raises(ValidationError):
            descriptor(network_origins=(origin,))


@pytest.mark.parametrize(
    "uri",
    [
        "https://archive.example.test/a.zip",
        "https://market.example.test/v3/klines?symbol=AAAUSD&interval=1m&limit=1000",
        "file:///imports/history/AAAUSD.zip",
        "https://archive.example.test",
        "https://archive.example.test/",
        "https://archive.example.test:8443/data/spot/daily/a.zip",
        "https://archive.example.test/a..b/c.zip",
    ],
)
def test_legal_source_uris(uri: str) -> None:
    assert validate_source_uri(uri) == uri
    assert collected("AAAUSD", T0, T1, source_uri=uri).source_uri == uri


@pytest.mark.parametrize(
    "uri",
    [
        "",
        "archive.example.test/a.zip",
        "https://user:pw@archive.example.test/a.zip",
        "https://token@archive.example.test/a.zip",
        "https://archive.example.test/a.zip#part",
        "https://archive.example.test/a zip",
        "https://market.example.test/v3/order?symbol=A&signature=abc",
        "https://market.example.test/v3/account?apiKey=abc",
        "https://market.example.test/v3/x?api_key=abc",
        "https://market.example.test/v3/userDataStream?listenKey=abc",
        "https://market.example.test/v3/x?access_token=abc",
        "https://market.example.test/v3/x?secret=abc",
        "https://market.example.test/v3/x?password=abc",
        "https://market.example.test/v3/x?Authorization=abc",
        # R2：只允许 https 网络来源与 file 只读导入，且必须真的绝对
        "https:///path",
        "https:///",
        "https://",
        "https:archive.example.test/a.zip",
        "http://archive.example.test/a.zip",
        "ftp://archive.example.test/a.zip",
        "s3://bucket/a.zip",
        "HTTPS://archive.example.test/a.zip",
        "https://Archive.example.test/a.zip",
        "https://archive.example.test:0/a.zip",
        "https://archive.example.test:65536/a.zip",
        "https://archive.example.test:08443/a.zip",
        "https://archive.example.test:/a.zip",
        "https://archive.example.test:https/a.zip",
        "https://archive..example.test/a.zip",
        "https://archive.example.test/../secret",
        "https://archive.example.test/a/%2e%2e/b",
        "https://archive.example.test/./a.zip",
        "file://relative",
        "file://relative/a.zip",
        "file://host/imports/a.zip",
        "file:relative/a.zip",
        "file:/imports/a.zip",
        "file:///",
        "file:///imports/",
        "file:///imports//a.zip",
        "file:///imports/../etc/passwd",
        "file:///imports/a.zip?x=1",
        "file:///imports/a.zip#f",
    ],
)
def test_illegal_or_credential_shaped_source_uris(uri: str) -> None:
    with pytest.raises(ValueError):
        validate_source_uri(uri)
    with pytest.raises(ValidationError):
        collected("AAAUSD", T0, T1, source_uri=uri)


#: R2 对抗反例：一次 percent-decode 后会产生路径分隔符、反斜杠或控制字符；以及畸形 escape。
ENCODED_OBJECT_URI_ATTACKS = (
    "file:///warehouse/a%2f..%2fsecret",
    "file:///warehouse/a%2F..%2Fsecret",
    "file:///warehouse/a%5c..%5csecret",
    "file:///warehouse/a%5C..%5Csecret",
    "file:///warehouse/a%00b",
    "file:///warehouse/a%1fb",
    "file:///warehouse/a%0Ab",
    "file:///warehouse/a%7fb",
    "file:///warehouse/a%7Fb",
    "file:///warehouse/%2e%2E/secret",
    "s3://bucket/a%2f..%2fsecret",
    "s3://bucket/a%5C..%5Csecret",
    "s3://bucket/a%00",
    "file:///warehouse/a%",
    "file:///warehouse/a%2",
    "file:///warehouse/a%GG",
    "file:///warehouse/a%g0",
    "s3://bucket/%%41",
)
ENCODED_SOURCE_URI_ATTACKS = (
    "https://host/a%2f..%2fsecret",
    "https://host/a%2F..%2Fsecret",
    "https://host/a%5c..%5csecret",
    "https://host/a%5C..%5Csecret",
    "https://host/a%00b",
    "https://host/a%0db",
    "https://host/a%7Fb",
    "https://host/%2e%2e/secret",
    "https://host/a%",
    "https://host/a%2",
    "https://host/%zz",
    "file:///imports/a%2fb.zip",
    "file:///imports/a%5cb.zip",
    "file:///imports/a%00.zip",
    "file:///imports/a%.zip",
    # https 路径不得有空段或尾随空段（根 `/` 除外），避免规范化歧义
    "https://host//a",
    "https://host/a//b",
    "https://host/a/",
    "https://host//",
)


@pytest.mark.parametrize("uri", ENCODED_OBJECT_URI_ATTACKS)
def test_encoded_object_uri_attacks_are_rejected(uri: str) -> None:
    with pytest.raises(ValueError):
        validate_object_uri(uri)
    with pytest.raises(ValidationError):
        object_ref(uri=uri)


@pytest.mark.parametrize("uri", ENCODED_SOURCE_URI_ATTACKS)
def test_encoded_source_uri_attacks_are_rejected(uri: str) -> None:
    with pytest.raises(ValueError):
        validate_source_uri(uri)
    with pytest.raises(ValidationError):
        collected("AAAUSD", T0, T1, source_uri=uri)


def test_ordinary_percent_encoding_is_preserved() -> None:
    """合法 escape（`%20`、UTF-8 百分号字节、`%2e%2e%2e`）保留原样；字面空白仍被拒绝。"""
    for uri in (
        "file:///warehouse/a%20b.zip",
        "file:///warehouse/caf%C3%A9.zip",
        "s3://bucket/a%2Db/%2e%2e%2e",
    ):
        assert validate_object_uri(uri) == uri
        assert object_ref(uri=uri).uri == uri
    for uri in (
        "https://archive.example.test/a%20b.zip",
        "https://archive.example.test/caf%C3%A9.zip",
        "file:///imports/a%20b.zip",
    ):
        assert validate_source_uri(uri) == uri
        assert collected("AAAUSD", T0, T1, source_uri=uri).source_uri == uri
    with pytest.raises(ValueError):
        validate_object_uri("file:///warehouse/a b.zip")
    with pytest.raises(ValueError):
        validate_source_uri("https://archive.example.test/a b.zip")


def test_uri_parsing_helpers_are_not_public_contract() -> None:
    """URI 拆分与主机 / 路径助手是私有实现：不进入任何公共 `__all__`、注册表或 Schema。"""
    internal = {
        "UriParts",
        "split",
        "split_uri",
        "is_host_port",
        "is_object_path",
        "is_https_path",
        "has_dot_segment",
        "is_scheme",
        "is_visible_ascii",
    }
    for module in B3_MODULES:
        assert not internal & set(module.__all__), module.__name__
        assert not {name for name in module.__all__ if name.startswith("_")}, module.__name__
    assert _uri.__all__ == []
    registered = {model.__name__ for model in CONTRACT_MODELS}
    assert not internal & registered
    assert not any((CURRENT_SCHEMA_DIR / f"{name}.schema.json").exists() for name in internal)


def test_source_uri_schemes_are_exactly_https_and_file() -> None:
    assert collector.SOURCE_URI_SCHEMES == {"https", "file"}


@pytest.mark.parametrize(
    "header",
    [
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-mbx-apikey",
        "x-api-key",
        "x-auth-token",
        "x-signature",
        "ETag",
        "",
        "etag value",
    ],
)
def test_credential_shaped_or_malformed_metadata_keys_are_rejected(header: str) -> None:
    with pytest.raises(ValidationError):
        collected("AAAUSD", T0, T1, source_metadata={header: "x"})


def test_source_metadata_is_read_only_and_order_independent() -> None:
    forward = collected("AAAUSD", T0, T1, source_metadata={"etag": "1", "content-length": "9"})
    backward = collected("AAAUSD", T0, T1, source_metadata={"content-length": "9", "etag": "1"})
    assert forward.content_hash() == backward.content_hash()
    assert isinstance(forward.source_metadata, FrozenMapping)
    with pytest.raises(TypeError):
        forward.source_metadata["etag"] = "2"  # type: ignore[index]
    assert collected("AAAUSD", T0, T1, source_metadata={}).source_metadata == {}


def test_source_checksum_must_match_the_published_object() -> None:
    assert collected("AAAUSD", T0, T1, source_sha256=None).source_sha256 is None
    with pytest.raises(ValidationError):
        collected("AAAUSD", T0, T1, source_sha256=SHA_B)


def test_gap_requires_reason_and_evidence() -> None:
    with pytest.raises(ValidationError):
        CoverageGap.model_validate(
            {
                "symbol": "AAAUSD",
                "coverage_start": T0,
                "coverage_end": T1,
                "reason": "guessed",
                "detail": "x",
            }
        )
    with pytest.raises(ValidationError):
        CoverageGap(
            symbol="AAAUSD",
            coverage_start=T0,
            coverage_end=T1,
            reason=GapReason.SOURCE_ABSENT,
            detail="",
        )


def test_result_objects_and_gaps_are_canonical_and_order_independent() -> None:
    base = result()
    shuffled = result(objects=tuple(reversed(base.objects)), gaps=base.gaps)
    assert shuffled == base
    assert shuffled.content_hash() == base.content_hash()
    assert [item.ref.key for item in base.objects] == sorted(item.ref.key for item in base.objects)


@pytest.mark.parametrize(
    ("objects", "gaps"),
    [
        pytest.param(
            (collected("AAAUSD", T0, T1), collected("BBBUSD", T0, T1)),
            (gap("BBBUSD", T1, T2),),
            id="hole-without-gap",
        ),
        pytest.param(
            (collected("AAAUSD", T0, T2), collected("BBBUSD", T0, T1)),
            (gap("BBBUSD", T0, T2),),
            id="gap-overlaps-object",
        ),
        pytest.param(
            (collected("AAAUSD", T0, T2),),
            (gap("BBBUSD", T0, T2), gap("BBBUSD", T1, T2)),
            id="overlapping-gaps",
        ),
        pytest.param(
            (collected("AAAUSD", T0, T2), collected("CCCUSD", T0, T2)),
            (gap("BBBUSD", T0, T2),),
            id="symbol-not-requested",
        ),
        pytest.param(
            (collected("AAAUSD", T0, T2 + timedelta(days=1)),),
            (gap("BBBUSD", T0, T2),),
            id="object-beyond-request",
        ),
        pytest.param(
            (collected("AAAUSD", T0 - timedelta(days=1), T2),),
            (gap("BBBUSD", T0, T2),),
            id="object-before-request",
        ),
        pytest.param((), (), id="nothing-accounted"),
    ],
)
def test_result_must_account_exactly_for_the_request(
    objects: tuple[CollectedObject, ...], gaps: tuple[CoverageGap, ...]
) -> None:
    with pytest.raises(ValidationError):
        result(objects=objects, gaps=gaps)


def test_result_allows_overlapping_objects_and_all_gap_results() -> None:
    checksum = collected("AAAUSD", T0, T2).model_copy(
        update={"ref": object_ref(key="raw/AAAUSD/archive.zip.CHECKSUM")}
    )
    overlapping = result(
        objects=(collected("AAAUSD", T0, T2), checksum), gaps=(gap("BBBUSD", T0, T2),)
    )
    assert len(overlapping.objects) == 2
    all_gaps = result(objects=(), gaps=(gap("AAAUSD", T0, T2), gap("BBBUSD", T0, T2)))
    assert all_gaps.objects == ()


def test_result_rejects_duplicate_object_keys_and_requires_explicit_lists() -> None:
    item = collected("AAAUSD", T0, T2)
    with pytest.raises(ValidationError):
        result(objects=(item, item), gaps=(gap("BBBUSD", T0, T2),))
    payload = result().model_dump(mode="json")
    for field in ("objects", "gaps"):
        partial = {key: value for key, value in payload.items() if key != field}
        with pytest.raises(ValidationError):
            CollectionResult.model_validate(partial)


def test_result_does_not_carry_content_bytes() -> None:
    for model in (CollectedObject, CollectionResult, ObjectRef, PublishResult, StagedObject):
        for name, info in model.model_fields.items():
            assert info.annotation not in (bytes, bytearray), f"{model.__name__}.{name}"


# ======================================================================================
# Protocol：精确成员、签名与替换性
# ======================================================================================

EXPECTED_SIGNATURES: dict[str, dict[str, str]] = {
    "StorageAdapter": {
        "stage": "(self, request: 'StageRequest', content: 'Iterable[bytes]') -> 'StagedObject'",
        "publish": "(self, staged: 'StagedObject') -> 'PublishResult'",
        "lookup": "(self, key: 'str') -> 'ObjectRef | None'",
        "open_read": "(self, ref: 'ObjectRef') -> 'BinaryIO'",
    },
    "CatalogAdapter": {
        "load_table": "(self, table: 'str') -> 'TableInfo | None'",
        "create_table": "(self, definition: 'TableDefinition') -> 'TableInfo'",
        "get_snapshot": "(self, table: 'str', snapshot_id: 'str') -> 'SnapshotInfo'",
        "commit_batch": "(self, request: 'CommitRequest', batch: 'BatchT') -> 'CommitResult'",
    },
    "CollectorAdapter": {
        "collect": "(self, request: 'CollectionRequest') -> 'CollectionResult'",
    },
}
PROTOCOLS: dict[str, type] = {
    "StorageAdapter": StorageAdapter,
    "CatalogAdapter": CatalogAdapter,
    "CollectorAdapter": CollectorAdapter,
}


@pytest.mark.parametrize("name", sorted(PROTOCOLS))
def test_protocol_members_are_the_minimal_set(name: str) -> None:
    protocol = PROTOCOLS[name]
    assert typing.is_protocol(protocol)
    expected = set(EXPECTED_SIGNATURES[name])
    if name == "CollectorAdapter":
        expected.add("descriptor")
    assert typing.get_protocol_members(protocol) == expected


@pytest.mark.parametrize("name", sorted(PROTOCOLS))
def test_protocol_signatures_are_exact(name: str) -> None:
    protocol = PROTOCOLS[name]
    for method, signature in EXPECTED_SIGNATURES[name].items():
        assert str(inspect.signature(getattr(protocol, method))) == signature, method


def test_collector_descriptor_is_a_read_only_property() -> None:
    member = inspect.getattr_static(CollectorAdapter, "descriptor")
    assert isinstance(member, property)
    assert member.fset is None
    assert member.fget is not None
    assert inspect.signature(member.fget).return_annotation == "CollectorDescriptor"


def test_catalog_adapter_is_generic_in_the_batch_type_only() -> None:
    params = CatalogAdapter.__type_params__
    assert [param.__name__ for param in params] == ["BatchT"]


@pytest.mark.parametrize("name", sorted(PROTOCOLS))
def test_protocols_are_not_runtime_checkable(name: str) -> None:
    """契约正确性来自 contract suite 与 mypy，不来自 `isinstance`。"""
    with pytest.raises(TypeError):
        isinstance(object(), PROTOCOLS[name])


# ======================================================================================
# 静态边界：依赖、venue 硬编码、凭据形状、arrival_seq、网络能力
# ======================================================================================

_STDLIB_ALLOWED = {"__future__", "collections", "datetime", "enum", "itertools", "re", "typing"}
#: `core.contracts` 只以 `from core.contracts import _uri` 的形式出现（私有 URI 助手模块）。
_CORE_ALLOWED = {
    "core.contracts",
    "core.contracts.revision",
    "core.contracts.storage",
    "core.domain.base",
}
#: 静态边界检查也覆盖私有 URI 助手模块。
_STATIC_MODULES = (*B3_MODULES, _uri)


def _tree(module: object) -> ast.Module:
    path = Path(inspect.getfile(module))  # type: ignore[arg-type]
    return ast.parse(path.read_text("utf-8"), filename=str(path))


def _imports(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


@pytest.mark.parametrize("module", _STATIC_MODULES, ids=lambda m: m.__name__)
def test_b3_modules_import_only_stdlib_pydantic_and_core(module: object) -> None:
    for imported in _imports(_tree(module)):
        root = imported.split(".")[0]
        assert (
            root in _STDLIB_ALLOWED
            or root == "pydantic"
            or imported in _CORE_ALLOWED
            or imported.startswith("collections.")
        ), f"{imported} 不在 B3 契约模块允许的依赖内"
        assert root not in {
            "infrastructure",
            "plugins",
            "pyiceberg",
            "pyarrow",
            "httpx",
            "sqlalchemy",
            "psycopg",
            "psycopg2",
            "urllib",
            "socket",
            "os",
            "io",
            "pathlib",
        }


#: 形如 `scheme://host` 的字面端点（`://` 后紧跟主机字符）；正则模式与报错说明里的
#: `https://[…]`、`file:///…` 不是端点。
_LITERAL_ENDPOINT_RE = re.compile(r"[a-z][a-z0-9+.-]*://[A-Za-z0-9]")


def _code_strings(tree: ast.Module) -> list[str]:
    """模块中的字符串常量，排除文档字符串（文档可以提及后续批次的具体技术）。"""
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


@pytest.mark.parametrize("module", _STATIC_MODULES, ids=lambda m: m.__name__)
def test_b3_modules_hardcode_no_venue_symbol_or_endpoint(module: object) -> None:
    tree = _tree(module)
    for text in _code_strings(tree):
        lowered = text.lower()
        for token in ("binance", "btcusdt", "ethusdt", "data-api", "vision"):
            assert token not in lowered, f"{module!r} 硬编码了 {token!r}：{text!r}"
        literal = _LITERAL_ENDPOINT_RE.search(text)
        assert literal is None, f"{module!r} 硬编码了端点 {literal.group(0)!r}"
    identifiers = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert not {name for name in identifiers if "binance" in name.lower()}


def _all_fields(model: type[Contract]) -> set[str]:
    return set(model.model_fields)


_FORBIDDEN_FIELD_PARTS = {
    "account",
    "apikey",
    "balance",
    "credential",
    "leverage",
    "order",
    "orders",
    "password",
    "position",
    "private",
    "secret",
    "signature",
    "token",
    "trade",
    "trades",
    "wallet",
    "withdraw",
}


def test_no_trading_account_or_secret_shaped_fields() -> None:
    for model in B3_MODELS:
        for field in _all_fields(model):
            parts = field.lower().split("_")
            assert not _FORBIDDEN_FIELD_PARTS & set(parts), f"{model.__name__}.{field}"
            assert not ("api" in parts and "key" in parts), f"{model.__name__}.{field}"


def test_catalog_never_uses_arrival_seq() -> None:
    for model in B3_MODELS:
        assert "arrival_seq" not in _all_fields(model)
    tree = _tree(catalog)
    names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    names |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "arrival_seq" not in names


def test_only_the_collector_declares_network_capability() -> None:
    network_parts = {"origin", "origins", "url", "endpoint", "host", "network"}
    declaring = {
        model.__name__
        for model in B3_MODELS
        if any(network_parts & set(field.split("_")) for field in _all_fields(model))
    }
    assert declaring == {"CollectorDescriptor"}


@pytest.mark.parametrize("module", _STATIC_MODULES, ids=lambda m: m.__name__)
def test_b3_modules_read_no_wall_clock(module: object) -> None:
    tree = _tree(module)
    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not attrs & {"now", "utcnow", "today", "time_ns", "monotonic"}


@pytest.mark.parametrize("module", B3_MODULES, ids=lambda m: m.__name__)
def test_module_exports_its_models_and_protocol(module: types.ModuleType) -> None:
    exported = set(module.__all__)
    own = {model.__name__ for model in B3_MODELS if model.__module__ == module.__name__}
    assert own <= exported
    assert any(name.endswith("Adapter") for name in exported)


# ======================================================================================
# 注册表与冻结内容：只追加，不改动
# ======================================================================================


def test_b3_only_appends_to_the_registry() -> None:
    names = tuple(model.__name__ for model in CONTRACT_MODELS)
    assert names[: len(PRE_B3_MODEL_NAMES)] == PRE_B3_MODEL_NAMES
    b3_end = len(PRE_B3_MODEL_NAMES) + len(B3_MODELS)
    assert names[len(PRE_B3_MODEL_NAMES) : b3_end] == tuple(model.__name__ for model in B3_MODELS)
    assert b3_end == 74
    assert len(CONTRACT_MODELS) == 148


def test_every_b3_model_is_exported_byte_identically(tmp_path: Path) -> None:
    written = export_json_schemas(tmp_path)
    for model in B3_MODELS:
        committed = (CURRENT_SCHEMA_DIR / f"{model.__name__}.schema.json").read_bytes()
        assert committed == written[model.__name__].read_bytes(), model.__name__


@pytest.mark.parametrize("name", sorted(PRE_B3_SCHEMA_SHA256))
def test_pre_b3_current_schemas_are_byte_identical(name: str, tmp_path: Path) -> None:
    committed = (CURRENT_SCHEMA_DIR / f"{name}.schema.json").read_bytes()
    regenerated = export_json_schemas(tmp_path)[name].read_bytes()
    if name in ADR_0055_SCHEMA_SHA256:  # changed by ADR-0055's fields: the 2.2.0 pin
        # ADR-0077: the 2.3.0 bump may change only the envelope default of these schemas.
        for schema in (committed, regenerated):
            digest = hashlib.sha256(as_published_at(schema, "2.2.0")).hexdigest()
            assert digest == ADR_0055_SCHEMA_SHA256[name]
        return
    if name in ADR_0052_SCHEMA_SHA256:  # changed by ADR-0052's fields: the 2.1.0 pin
        # ADR-0055 / ADR-0077: the 2.2.0 and 2.3.0 bumps may change only the envelope default.
        for schema in (committed, regenerated):
            digest = hashlib.sha256(as_published_at(schema, "2.1.0")).hexdigest()
            assert digest == ADR_0052_SCHEMA_SHA256[name]
        return
    # ADR-0052 §4 / ADR-0055 / ADR-0077: the 2.1.0, 2.2.0 and 2.3.0 bumps may change only the
    # envelope default.
    assert (
        hashlib.sha256(as_published_at_2_0_0(committed)).hexdigest() == (PRE_B3_SCHEMA_SHA256[name])
    )
    assert (
        hashlib.sha256(as_published_at_2_0_0(regenerated)).hexdigest()
        == (PRE_B3_SCHEMA_SHA256[name])
    )


def test_v1_snapshots_and_vectors_are_byte_identical() -> None:
    present = {
        str(path.relative_to(REPO))
        for root in (LEGACY_SCHEMA_DIR, REPO / "tests" / "vectors")
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    assert present == set(FROZEN_FILE_SHA256)
    for relative, digest in FROZEN_FILE_SHA256.items():
        assert hashlib.sha256((REPO / relative).read_bytes()).hexdigest() == digest, relative


def test_contract_version_and_reused_patterns_are_unchanged() -> None:
    # ADR-0052 §4 raised the minor to 2.1.0, ADR-0055 to 2.2.0, ADR-0077 to 2.3.0, ADR-0088
    # to 2.4.0, and ADR-0094 to 2.5.0.
    assert CONTRACT_SCHEMA_VERSION == "2.5.0"
    assert revision.SNAPSHOT_TABLE_PATTERN == r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$"
    assert revision.BINDING_ID_PATTERN == r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)*$"


def test_b3_models_are_not_legacy_v1_models() -> None:
    for model in B3_MODELS:
        assert model.__name__ not in V1_MODEL_NAMES
        assert not (LEGACY_SCHEMA_DIR / f"{model.__name__}.schema.json").exists()
