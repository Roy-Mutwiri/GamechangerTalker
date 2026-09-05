"""One chat completion against any OpenAI-compatible endpoint.

Every hosted brain worth using speaks the same shape -- POST /chat/completions,
messages in, choices out -- so this module talks that shape and nothing else.
Changing provider is changing `base_url` and a token env var, which is the
whole reason it was written generically rather than against one vendor.

That generality is not academic. This was originally specified against GitHub
Models, which was retired on 2026-07-30: playground, catalog, inference API and
BYOK, for every customer including existing ones. A client written to one
vendor's URL would have died with it. See PRESETS below for the survivors.

WHAT THIS MODULE WILL NOT DO
----------------------------
**Retry more than once.** Retrying is the conversation's decision, not the
transport's. A backend that quietly retries five times with a backoff is how a
twenty-second hole appears in a live stream, and from the outside it is
indistinguishable from a slow model.

**Log a prompt or a token.** The DEBUG line carries the model, the latency and
the token counts. The config file is the thing most likely to be on screen; the
log is the thing most likely to be pasted into an issue.

**Guess.** A wrong model id, a missing permission and an exhausted quota are
three different problems with three different fixes, so they are three
different exceptions carrying the API's own words.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: Bodies longer than this are almost certainly an HTML error page from a proxy
#: rather than an API error. Truncated before it reaches a log or a status bar.
MAX_ERROR_CHARS = 400


# ---------------------------------------------------------------------------
# Failure vocabulary
#
# The conversation classifies on type, not on substring matching against a
# message -- which is what hosts.py had to do for the Anthropic SDK, and why
# TERMINAL_ERRORS exists at all. Typed exceptions let it stop guessing.
# ---------------------------------------------------------------------------


class LLMError(RuntimeError):
    """Base for everything this client raises."""


class AuthError(LLMError):
    """401/403. Terminal: a revoked token does not heal in eight seconds."""


class RateLimited(LLMError):
    """429. Never terminal, and never counted as a failure.

    A rate limit is the service working as designed. Treating it as a failure
    is how a free tier trips FAILURE_LIMIT and disables the brain for a whole
    stream over something that would have cleared in forty seconds.
    """

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class BadRequest(LLMError):
    """400/404/422. Terminal only when it is about the request's shape.

    A bad model id will be bad on every subsequent turn; an over-long prompt
    for one unusual turn will not. `terminal` carries that distinction so the
    conversation does not have to re-derive it from the message.
    """

    def __init__(self, message: str, *, terminal: bool = False) -> None:
        super().__init__(message)
        self.terminal = terminal


class Upstream(LLMError):
    """5xx. The provider's problem. Worth exactly one retry."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class EmptyCompletion(LLMError):
    """200, with nothing in it. One line lost; the library covers."""


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Preset:
    """A provider, reduced to the four things that actually differ."""

    key: str
    base_url: str
    token_env: str
    #: Whether model ids are "publisher/model". Decides the validation message.
    publisher_ids: bool
    example_model: str
    #: Set when the service no longer exists. `ready()` says so instead of
    #: letting the operator debug DNS.
    retired: str = ""
    notes: str = ""


PRESETS: dict[str, Preset] = {
    "github": Preset(
        key="github",
        base_url="https://models.github.ai/inference",
        token_env="GITHUB_TOKEN",
        publisher_ids=True,
        example_model="openai/gpt-4o-mini",
        # Kept, rather than deleted, so an operator following an older guide
        # gets told what happened instead of a DNS failure or a 404.
        retired=(
            "GitHub Models was retired on 2026-07-30 — the inference API, the "
            'catalog and BYOK are all gone. Switch backend to "openrouter" '
            "(same publisher/model ids, so nothing else changes), or "
            '"ollama" to run locally for free'
        ),
    ),
    "openrouter": Preset(
        key="openrouter",
        base_url="https://openrouter.ai/api/v1",
        token_env="OPENROUTER_API_KEY",
        # Same publisher/model form GitHub Models used, which is why it is the
        # closest replacement: model ids, validation and error messages carry
        # over without changing.
        publisher_ids=True,
        example_model="openai/gpt-4o-mini",
        notes="many publishers behind one key; has a free tier",
    ),
    "openai": Preset(
        key="openai",
        base_url="https://api.openai.com/v1",
        token_env="OPENAI_API_KEY",
        publisher_ids=False,
        example_model="gpt-4o-mini",
    ),
    "groq": Preset(
        key="groq",
        base_url="https://api.groq.com/openai/v1",
        token_env="GROQ_API_KEY",
        publisher_ids=False,
        example_model="llama-3.3-70b-versatile",
        notes="fast enough that timeout_seconds can come down a long way",
    ),
    "together": Preset(
        key="together",
        base_url="https://api.together.xyz/v1",
        token_env="TOGETHER_API_KEY",
        publisher_ids=True,
        example_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
    ),
    "azure": Preset(
        key="azure",
        # Per-resource; the operator supplies the whole thing.
        base_url="",
        token_env="AZURE_AI_API_KEY",
        publisher_ids=False,
        example_model="gpt-4o-mini",
        notes="set base_url to your Foundry endpoint, including the version",
    ),
    "local": Preset(
        key="local",
        # LM Studio's default. vLLM's is the same shape on 8000.
        base_url="http://127.0.0.1:1234/v1",
        token_env="LOCAL_LLM_API_KEY",
        publisher_ids=False,
        example_model="local-model",
        notes="LM Studio, vLLM, llama.cpp — anything serving /chat/completions",
    ),
}


def resolve_preset(name: str) -> Preset | None:
    return PRESETS.get((name or "").strip().lower())


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class RateLimitInfo:
    """Whatever the response chose to tell us. Every field optional.

    Providers disagree about these headers -- names, units, and whether they
    appear at all -- so nothing here is required and nothing is computed from a
    missing value. The budget treats this as a hint that refines its own
    estimate, never as the estimate itself.
    """

    limit_requests: int | None = None
    remaining_requests: int | None = None
    reset_requests_s: float | None = None
    limit_tokens: int | None = None
    remaining_tokens: int | None = None
    retry_after_s: float | None = None

    @classmethod
    def from_headers(cls, headers: Any) -> RateLimitInfo:
        def get(*names: str) -> str | None:
            for name in names:
                value = headers.get(name)
                if value not in (None, ""):
                    return str(value)
            return None

        return cls(
            limit_requests=_as_int(
                get("x-ratelimit-limit-requests", "x-ratelimit-limit")
            ),
            remaining_requests=_as_int(
                get("x-ratelimit-remaining-requests", "x-ratelimit-remaining")
            ),
            reset_requests_s=_as_seconds(
                get("x-ratelimit-reset-requests", "x-ratelimit-reset")
            ),
            limit_tokens=_as_int(get("x-ratelimit-limit-tokens")),
            remaining_tokens=_as_int(get("x-ratelimit-remaining-tokens")),
            retry_after_s=_as_seconds(get("retry-after", "x-ratelimit-reset-requests")),
        )

    @property
    def empty(self) -> bool:
        """True when the provider told us nothing. Then it is not attached."""
        return all(value is None for value in vars(self).values())


@dataclass
class Completion:
    """One turn, and what it cost."""

    text: str
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    rate_limit: RateLimitInfo | None = None
    model: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


@dataclass
class OpenAICompatClient:
    """POST /chat/completions, and a precise account of what came back."""

    base_url: str
    token: str = ""
    timeout: float = 30.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    #: Injected by the tests. Anything with an async `post` and `aclose`.
    client: Any = None

    def _http(self) -> Any:
        if self.client is None:
            import httpx

            self.client = httpx.AsyncClient(
                base_url=self.base_url.rstrip("/"),
                timeout=self.timeout,
                headers=self._headers(),
            )
        return self.client

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        headers.update(self.extra_headers)
        return headers

    async def aclose(self) -> None:
        if self.client is not None:
            close = getattr(self.client, "aclose", None)
            if close is not None:
                await close()
            self.client = None

    async def chat(
        self,
        model: str,
        system: str,
        user: str,
        *,
        max_tokens: int,
        temperature: float,
        top_p: float = 0.92,
        presence_penalty: float = 0.6,
        frequency_penalty: float = 0.4,
        stop: list[str] | None = None,
        content: list[dict[str, Any]] | None = None,
    ) -> Completion:
        """One completion.

        `content` replaces the plain user string with OpenAI content parts,
        which is how an image reaches a vision model. The chart eyes use it;
        the hosts never do.
        """
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content if content is not None else user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            # There is no repeat_penalty in this API. The echoing the Ollama
            # path suppresses with repeat_penalty/repeat_last_n -- both hosts
            # opening "Exactly," and restating each other -- is suppressed here
            # by these two instead. Presence discourages returning to a subject
            # at all; frequency discourages saying the same word again.
            "presence_penalty": presence_penalty,
            "frequency_penalty": frequency_penalty,
            "stream": False,
        }
        if stop:
            body["stop"] = stop

        start = time.perf_counter()
        try:
            response = await self._http().post("/chat/completions", json=body)
        except Exception as exc:  # network down, DNS, TLS, connect timeout
            raise Upstream(f"{exc.__class__.__name__}: {exc}", 0) from exc
        latency = time.perf_counter() - start

        rate_limit = RateLimitInfo.from_headers(response.headers)
        self._raise_for_status(response, rate_limit)

        try:
            payload = response.json()
        except Exception as exc:
            raise Upstream(f"response was not JSON: {exc}", response.status_code) from exc

        choices = payload.get("choices") or []
        if not choices:
            raise EmptyCompletion(_detail(payload) or "no choices in the response")

        message = choices[0].get("message") or {}
        text = str(message.get("content") or "").strip()
        usage = payload.get("usage") or {}

        completion = Completion(
            text=text,
            finish_reason=str(choices[0].get("finish_reason") or ""),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            latency_s=latency,
            rate_limit=None if rate_limit.empty else rate_limit,
            model=str(payload.get("model") or model),
        )

        # Never the prompt, never the token. Model, timing and cost only.
        log.debug(
            "llm %s: %.2fs, %d+%d tokens, finish=%s%s",
            completion.model,
            completion.latency_s,
            completion.prompt_tokens,
            completion.completion_tokens,
            completion.finish_reason or "?",
            f", {rate_limit.remaining_requests} left"
            if rate_limit.remaining_requests is not None
            else "",
        )

        if not text:
            raise EmptyCompletion(
                f"the model returned an empty turn (finish_reason="
                f"{completion.finish_reason or 'unknown'})"
            )
        return completion

    def _raise_for_status(self, response: Any, rate_limit: RateLimitInfo) -> None:
        status = int(response.status_code)
        if status < 400:
            return

        detail = _detail(_safe_json(response)) or _truncate(_safe_text(response))

        if status in (401, 403):
            raise AuthError(detail or f"HTTP {status}: the token was rejected")
        if status == 429:
            raise RateLimited(
                detail or "rate limited", retry_after_s=rate_limit.retry_after_s
            )
        if status in (400, 404, 422):
            raise BadRequest(
                detail or f"HTTP {status}", terminal=_is_terminal_request_error(detail)
            )
        raise Upstream(detail or f"HTTP {status}", status)


# ---------------------------------------------------------------------------
# Message shaping
# ---------------------------------------------------------------------------

#: A 400 about the request's *shape* will be a 400 on the next turn too. One
#: about this particular prompt's length will not, so it costs one line.
_TERMINAL_REQUEST_HINTS = (
    "model_not_found",
    "does not exist",
    "unknown model",
    "invalid model",
    "unsupported model",
    "no deployment",
    "deployment not found",
    "unknown_model",
)


def _is_terminal_request_error(detail: str) -> bool:
    lowered = (detail or "").lower()
    return any(hint in lowered for hint in _TERMINAL_REQUEST_HINTS)


def _safe_json(response: Any) -> Any:
    try:
        return response.json()
    except Exception:
        return None


def _safe_text(response: Any) -> str:
    try:
        return str(response.text or "")
    except Exception:
        return ""


def _detail(payload: Any) -> str:
    """The provider's own sentence, dug out of whichever shape it used."""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("message", "detail", "code"):
            value = error.get(key)
            if isinstance(value, str) and value.strip():
                return _truncate(value)
    if isinstance(error, str) and error.strip():
        return _truncate(error)
    for key in ("message", "detail"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _truncate(value)
    return ""


def _truncate(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 1] + "…"


def _as_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


_DURATION = re.compile(
    r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?$"
)


def _as_seconds(value: str | None) -> float | None:
    """Seconds from whichever format the provider chose.

    Seen in the wild: "20", "20.5", "1m30s", "2h". An HTTP-date Retry-After is
    legal too and is deliberately not parsed -- it needs a wall clock this
    module does not have, and every provider that matters sends a delta.
    """
    if value is None:
        return None
    text = value.strip().lower()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    match = _DURATION.match(text)
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = (float(g) if g else 0.0 for g in match.groups())
    return hours * 3600.0 + minutes * 60.0 + seconds


def validate_model_id(model: str, preset: Preset | None) -> str:
    """Empty if the id looks right for this provider, otherwise why not.

    Checked rather than repaired. Silently prepending a publisher would send a
    turn to a model the operator did not choose and bill them for it, and the
    first they would know is a voice that does not sound like the one they
    picked.
    """
    model = (model or "").strip()
    if not model:
        return "no model id set — put one under [hosts] model"
    if preset is None or not preset.publisher_ids:
        return ""
    if "/" in model:
        return ""
    return (
        f"{model!r} is missing its publisher. This provider wants "
        f"'publisher/model' — try {preset.example_model!r}"
    )
