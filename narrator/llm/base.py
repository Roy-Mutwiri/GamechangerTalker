"""The two-method interface every brain implements.

It lived in script/hosts.py, which is where it belongs conceptually -- the
conversation is the only thing that calls it. It moved here for one mechanical
reason: `BudgetedBackend` wraps a backend and must *be* one, and it cannot
import from hosts.py because hosts.py imports the budget.

`narrator.script.hosts.Backend` still names this class, so nothing that
imported it from there has to change.
"""

from __future__ import annotations


class Backend:
    """Turns a system prompt and a user block into one spoken turn."""

    name = "none"

    async def complete(
        self, system: str, user: str, *, max_tokens: int, temperature: float
    ) -> str:
        raise NotImplementedError

    def ready(self) -> str:
        """Empty if usable, otherwise why not -- in words worth showing a human."""
        return ""
