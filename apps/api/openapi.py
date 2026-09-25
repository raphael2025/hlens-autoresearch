"""Export the OpenAPI document for the web client (``python -m apps.api.openapi``)."""

from __future__ import annotations

import json
from pathlib import Path

from apps.api.app import create_app

TARGET = Path(__file__).resolve().parent / "openapi.json"


def export(target: Path = TARGET) -> Path:
    target.write_text(
        json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


if __name__ == "__main__":  # pragma: no cover - manual export
    print(export())
