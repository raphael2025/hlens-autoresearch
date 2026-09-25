"""LLMProvider implementations (ADR-0040). Only an offline scripted provider exists."""

from plugins.llm.scripted import ScriptedLLMProvider

__all__ = ["ScriptedLLMProvider"]
