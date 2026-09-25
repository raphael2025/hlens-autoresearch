"""HTTP entry point (apps/api README; ADR-0048): orchestration and validation only."""

from apps.api.app import create_app

__all__ = ["create_app"]
