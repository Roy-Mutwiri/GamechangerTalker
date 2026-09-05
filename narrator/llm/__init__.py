"""The hosted brain: an OpenAI-compatible client, and a budget to spend it with.

Two modules, deliberately separate:

  openai_compat.py  one HTTP call, and a precise vocabulary of failure
  budget.py         how many calls a night is allowed, and how to spread them

Keeping them apart is what lets the budget wrap *any* backend -- Ollama gets an
unlimited one and nothing about the local path changes.
"""

from __future__ import annotations

from narrator.llm.base import Backend
from narrator.llm.budget import (
    BudgetedBackend,
    BudgetExhausted,
    BudgetStatus,
    BudgetWait,
    RateBudget,
)
from narrator.llm.openai_compat import (
    PRESETS,
    AuthError,
    BadRequest,
    Completion,
    EmptyCompletion,
    LLMError,
    OpenAICompatClient,
    Preset,
    RateLimited,
    RateLimitInfo,
    Upstream,
    resolve_preset,
)

__all__ = [
    "PRESETS",
    "AuthError",
    "Backend",
    "BadRequest",
    "BudgetExhausted",
    "BudgetStatus",
    "BudgetWait",
    "BudgetedBackend",
    "Completion",
    "EmptyCompletion",
    "LLMError",
    "OpenAICompatClient",
    "Preset",
    "RateBudget",
    "RateLimitInfo",
    "RateLimited",
    "Upstream",
    "resolve_preset",
]
