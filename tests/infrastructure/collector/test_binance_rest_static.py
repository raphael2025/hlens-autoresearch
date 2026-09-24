"""Acceptance #18: a static proof that the REST collector cannot reach anything but market data.

These checks read the module's source and AST. They do not execute it, so they hold even for
code paths no behavioural test happens to exercise.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from infrastructure.collector import binance_rest
from infrastructure.revision.rest_identity import DATA_TYPES

SOURCE_PATH = Path(binance_rest.__file__)
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE, filename=str(SOURCE_PATH))
STRINGS = [
    node.value
    for node in ast.walk(TREE)
    if isinstance(node, ast.Constant) and isinstance(node.value, str)
]


def test_the_only_request_paths_are_the_two_frozen_market_data_endpoints() -> None:
    paths = {text for text in STRINGS if re.match(r"^/(?:api|sapi|wapi|fapi|dapi)(/|$)", text)}
    assert paths == set()  # the paths come from DATA_TYPES, never from a literal here
    assert set(DATA_TYPES.values()) == {"/api/v3/aggTrades", "/api/v3/klines"}


@pytest.mark.parametrize(
    "forbidden",
    [
        "/api/v3/order",
        "/api/v3/account",
        "/api/v3/myTrades",
        "/api/v3/userDataStream",
        "/sapi/",
        "X-MBX-APIKEY",
        "apiKey",
        "recvWindow",
        "timestamp=",
        "listenKey",
    ],
)
def test_no_account_order_or_signed_endpoint_appears_anywhere(forbidden: str) -> None:
    assert forbidden not in SOURCE


def test_nothing_signs_a_request() -> None:
    imported: set[str] = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "hmac" not in imported
    assert "hashlib" in imported  # content addressing only
    assert "new(" not in SOURCE  # no hashlib.new(<algo>) indirection


def test_no_environment_or_credential_lookup_happens_here() -> None:
    for node in ast.walk(TREE):
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"environ", "getenv", "getenvb"}
        if isinstance(node, ast.Name):
            assert node.id not in {"getenv", "environ", "os"}
    assert "os.environ" not in SOURCE
    assert "dotenv" not in SOURCE


def test_no_host_or_absolute_url_is_hard_coded() -> None:
    """The origin always comes from settings; nothing in here names a reachable host."""
    host_shaped = re.compile(r"[a-z][a-z0-9+.-]*://[a-z0-9-]+\.[a-z]")
    assert [text for text in STRINGS if host_shaped.search(text)] == []
    assert host_shaped.search(SOURCE) is None


def test_redirects_are_never_followed() -> None:
    values: list[bool] = []
    for node in ast.walk(TREE):
        if isinstance(node, ast.keyword) and node.arg == "follow_redirects":
            assert isinstance(node.value, ast.Constant)
            assert node.value.value is False
            values.append(node.value.value)
    assert values, "the collector must state follow_redirects explicitly"
    assert "allow_redirects" not in SOURCE


def test_only_get_is_ever_sent() -> None:
    methods = {
        text
        for text in STRINGS
        if text in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
    }
    assert methods == {"GET"}


def test_the_credential_denylist_is_the_only_place_secret_words_appear() -> None:
    """Every credential word in the module belongs to the refusal list or its documentation."""
    lowered = SOURCE.lower()
    for word in ("password", "secret", "signature", "token", "credential"):
        for line_number, line in enumerate(lowered.splitlines(), start=1):
            if word not in line:
                continue
            assert '"' in line or "#" in line or "credential" in line, (
                f"{SOURCE_PATH.name}:{line_number} uses {word!r} outside the refusal list"
            )
