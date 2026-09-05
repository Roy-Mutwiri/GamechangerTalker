"""The OpenAI-compatible client: what it sends, and how it names what failed.

No network. A fake client stands in for httpx, in the style of the fake used in
test_hosts.py, because the whole point of these tests is the shape of the
request and the classification of the response -- neither of which needs a
socket.
"""

from __future__ import annotations

import logging

import pytest

from narrator.llm.openai_compat import (
    PRESETS,
    AuthError,
    BadRequest,
    EmptyCompletion,
    OpenAICompatClient,
    RateLimited,
    RateLimitInfo,
    Upstream,
    resolve_preset,
    validate_model_id,
)


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeClient:
    """Records the one request it was given, and returns what it was told to."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.closed = False

    async def post(self, url, json=None):
        self.requests.append({"url": url, "json": json})
        if not self.responses:
            raise AssertionError("the client posted more times than expected")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def aclose(self):
        self.closed = True


def ok(text="hello", **kwargs):
    payload = {
        "model": "openai/gpt-4o-mini",
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3},
    }
    return FakeResponse(200, payload, **kwargs)


def client(*responses, token="tok"):
    fake = FakeClient(*responses)
    return OpenAICompatClient(base_url="https://x/v1", token=token, client=fake), fake


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_request_has_the_shape_the_api_expects():
    api, fake = client(ok())
    await api.chat("m", "SYSTEM", "USER", max_tokens=120, temperature=0.9)

    body = fake.requests[0]["json"]
    assert fake.requests[0]["url"] == "/chat/completions"
    assert body["model"] == "m"
    assert body["messages"] == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "USER"},
    ]
    assert body["max_tokens"] == 120
    assert body["temperature"] == 0.9
    assert body["stream"] is False


@pytest.mark.asyncio
async def test_the_penalties_replace_ollamas_repeat_penalty():
    """This API has no repeat_penalty. Without these two the hosts echo each
    other, which is what the Ollama path uses repeat_penalty to stop."""
    api, fake = client(ok())
    await api.chat(
        "m",
        "s",
        "u",
        max_tokens=10,
        temperature=1.0,
        presence_penalty=0.6,
        frequency_penalty=0.4,
        top_p=0.92,
    )
    body = fake.requests[0]["json"]
    assert body["presence_penalty"] == 0.6
    assert body["frequency_penalty"] == 0.4
    assert body["top_p"] == 0.92


@pytest.mark.asyncio
async def test_stop_is_only_sent_when_it_is_set():
    api, fake = client(ok(), ok())
    await api.chat("m", "s", "u", max_tokens=10, temperature=1.0)
    assert "stop" not in fake.requests[0]["json"]
    await api.chat("m", "s", "u", max_tokens=10, temperature=1.0, stop=["\n"])
    assert fake.requests[1]["json"]["stop"] == ["\n"]


def test_the_bearer_header_is_present_and_the_token_is_not_the_url():
    api = OpenAICompatClient(base_url="https://x/v1", token="secret-token")
    headers = api._headers()
    assert headers["Authorization"] == "Bearer secret-token"
    assert "secret-token" not in api.base_url


def test_no_authorization_header_without_a_token():
    """A local LM Studio needs no key, and sending "Bearer " upsets some."""
    assert (
        "Authorization"
        not in OpenAICompatClient(base_url="http://x", token="")._headers()
    )


@pytest.mark.asyncio
async def test_the_token_never_reaches_the_log(caplog):
    api, _ = client(ok(), token="super-secret-value")
    with caplog.at_level(logging.DEBUG, logger="narrator.llm.openai_compat"):
        await api.chat("m", "s", "u", max_tokens=10, temperature=1.0)
    assert caplog.records, "the debug line should have been emitted"
    assert "super-secret-value" not in caplog.text


@pytest.mark.asyncio
async def test_the_prompt_never_reaches_the_log(caplog):
    api, _ = client(ok())
    with caplog.at_level(logging.DEBUG, logger="narrator.llm.openai_compat"):
        await api.chat(
            "m", "SENSITIVE-SYSTEM", "SENSITIVE-USER", max_tokens=10, temperature=1.0
        )
    assert "SENSITIVE-SYSTEM" not in caplog.text
    assert "SENSITIVE-USER" not in caplog.text


# ---------------------------------------------------------------------------
# The response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_good_response_carries_its_cost():
    api, _ = client(ok("the turn"))
    completion = await api.chat("m", "s", "u", max_tokens=10, temperature=1.0)
    assert completion.text == "the turn"
    assert completion.finish_reason == "stop"
    assert completion.prompt_tokens == 11
    assert completion.completion_tokens == 3
    assert completion.total_tokens == 14
    assert completion.latency_s >= 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_a_rejected_token_is_an_auth_error(status):
    api, _ = client(FakeResponse(status, {"error": {"message": "Bad credentials"}}))
    with pytest.raises(AuthError, match="Bad credentials"):
        await api.chat("m", "s", "u", max_tokens=10, temperature=1.0)


@pytest.mark.asyncio
async def test_a_429_carries_retry_after():
    api, _ = client(
        FakeResponse(
            429, {"error": {"message": "slow down"}}, headers={"retry-after": "42"}
        )
    )
    with pytest.raises(RateLimited) as caught:
        await api.chat("m", "s", "u", max_tokens=10, temperature=1.0)
    assert caught.value.retry_after_s == 42.0


@pytest.mark.asyncio
async def test_a_429_without_retry_after_still_classifies():
    api, _ = client(FakeResponse(429, {"error": {"message": "slow down"}}))
    with pytest.raises(RateLimited) as caught:
        await api.chat("m", "s", "u", max_tokens=10, temperature=1.0)
    assert caught.value.retry_after_s is None


@pytest.mark.asyncio
async def test_an_unknown_model_is_a_terminal_bad_request():
    """It will be wrong on the next turn too, so the layer should stop."""
    api, _ = client(FakeResponse(404, {"error": {"message": "model_not_found: nope"}}))
    with pytest.raises(BadRequest) as caught:
        await api.chat("m", "s", "u", max_tokens=10, temperature=1.0)
    assert caught.value.terminal


@pytest.mark.asyncio
async def test_an_over_long_prompt_is_not_terminal():
    """One unusual turn was too big. The next one probably is not."""
    api, _ = client(FakeResponse(400, {"error": {"message": "maximum context length"}}))
    with pytest.raises(BadRequest) as caught:
        await api.chat("m", "s", "u", max_tokens=10, temperature=1.0)
    assert not caught.value.terminal


@pytest.mark.asyncio
async def test_a_5xx_is_upstream_and_carries_its_status():
    api, _ = client(FakeResponse(503, {"error": {"message": "overloaded"}}))
    with pytest.raises(Upstream) as caught:
        await api.chat("m", "s", "u", max_tokens=10, temperature=1.0)
    assert caught.value.status == 503


@pytest.mark.asyncio
async def test_the_client_itself_never_retries():
    """Retrying is the conversation's decision. A transport that retried on
    its own would turn one slow turn into silent seconds that look identical
    to a hung model from the outside."""
    api, fake = client(FakeResponse(503, {"error": {"message": "nope"}}))
    with pytest.raises(Upstream):
        await api.chat("m", "s", "u", max_tokens=10, temperature=1.0)
    assert len(fake.requests) == 1


@pytest.mark.asyncio
async def test_no_choices_is_an_empty_completion():
    api, _ = client(FakeResponse(200, {"choices": []}))
    with pytest.raises(EmptyCompletion):
        await api.chat("m", "s", "u", max_tokens=10, temperature=1.0)


@pytest.mark.asyncio
async def test_an_empty_string_is_an_empty_completion():
    api, _ = client(ok(""))
    with pytest.raises(EmptyCompletion):
        await api.chat("m", "s", "u", max_tokens=10, temperature=1.0)


@pytest.mark.asyncio
async def test_a_non_json_body_is_upstream_not_a_crash():
    api, _ = client(FakeResponse(200, None, text="<html>502 Bad Gateway</html>"))
    with pytest.raises(Upstream):
        await api.chat("m", "s", "u", max_tokens=10, temperature=1.0)


@pytest.mark.asyncio
async def test_a_network_failure_is_upstream():
    api, _ = client(OSError("connection refused"))
    with pytest.raises(Upstream, match="connection refused"):
        await api.chat("m", "s", "u", max_tokens=10, temperature=1.0)


@pytest.mark.asyncio
async def test_an_html_error_page_is_truncated_not_dumped():
    api, _ = client(FakeResponse(500, None, text="x" * 5000))
    with pytest.raises(Upstream) as caught:
        await api.chat("m", "s", "u", max_tokens=10, temperature=1.0)
    assert len(str(caught.value)) <= 401


# ---------------------------------------------------------------------------
# Rate-limit headers
# ---------------------------------------------------------------------------


def test_rate_limit_headers_are_all_optional():
    assert RateLimitInfo.from_headers({}).empty


@pytest.mark.parametrize(
    "value, expected",
    [
        ("20", 20.0),
        ("20.5", 20.5),
        ("1m30s", 90.0),
        ("2h", 7200.0),
        ("", None),
        ("x", None),
    ],
)
def test_retry_after_accepts_the_formats_providers_actually_send(value, expected):
    info = RateLimitInfo.from_headers({"retry-after": value})
    assert info.retry_after_s == expected


def test_the_common_ratelimit_headers_are_read():
    info = RateLimitInfo.from_headers(
        {
            "x-ratelimit-limit-requests": "150",
            "x-ratelimit-remaining-requests": "61",
            "x-ratelimit-remaining-tokens": "8000",
        }
    )
    assert info.limit_requests == 150
    assert info.remaining_requests == 61
    assert info.remaining_tokens == 8000
    assert not info.empty


# ---------------------------------------------------------------------------
# Providers and model ids
# ---------------------------------------------------------------------------


def test_github_models_is_kept_and_marked_retired():
    """Deleting it would leave an operator following an older guide debugging
    DNS. Keeping it means they are told what happened."""
    preset = resolve_preset("github")
    assert preset is not None
    assert "2026-07-30" in preset.retired
    assert "openrouter" in preset.retired


def test_every_live_preset_names_the_variable_that_holds_its_key():
    for preset in PRESETS.values():
        if preset.retired:
            continue
        assert preset.token_env, f"{preset.key} has no token env var"
        assert preset.example_model, f"{preset.key} has no example model"


def test_a_publisherless_id_is_refused_rather_than_repaired():
    """Silently prepending a publisher would send the turn to a model the
    operator did not choose, and bill them for it."""
    message = validate_model_id("gpt-4o-mini", PRESETS["openrouter"])
    assert "publisher/model" in message
    assert "openai/gpt-4o-mini" in message


def test_a_publisher_id_is_accepted():
    assert validate_model_id("openai/gpt-4o-mini", PRESETS["openrouter"]) == ""


def test_providers_with_bare_ids_do_not_demand_a_publisher():
    assert validate_model_id("gpt-4o-mini", PRESETS["openai"]) == ""


def test_an_empty_model_id_says_where_to_put_one():
    assert "[hosts] model" in validate_model_id("", PRESETS["openai"])


@pytest.mark.asyncio
async def test_aclose_closes_the_underlying_client():
    api, fake = client(ok())
    await api.chat("m", "s", "u", max_tokens=10, temperature=1.0)
    await api.aclose()
    assert fake.closed
