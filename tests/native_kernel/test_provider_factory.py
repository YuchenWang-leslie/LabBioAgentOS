"""C14 step 8: one adapter per transport, built from an exact binding."""

from __future__ import annotations

import pytest

from labbioagentos.kernel import (
    AnthropicMessagesDialect,
    AnthropicMessagesProvider,
    GeminiGenerateContentDialect,
    GeminiGenerateContentProvider,
    GeminiInteractionsDialect,
    GeminiInteractionsProvider,
    OpenAICompatibleChatProvider,
    OpenAIResponsesProvider,
    ProviderBinding,
    create_provider,
)

KEY = "PRIVATE_KEY_CANARY"
GEMINI_LEVELS = {"thinking_level_enabled": "high", "thinking_level_disabled": "low"}
CASES = [
    ("OPENAI_COMPATIBLE_CHAT", None, OpenAICompatibleChatProvider),
    ("OPENAI_RESPONSES", None, OpenAIResponsesProvider),
    ("ANTHROPIC_MESSAGES", AnthropicMessagesDialect(thinking_off="DISABLED"),
     AnthropicMessagesProvider),
    ("GEMINI_GENERATE_CONTENT", GeminiGenerateContentDialect(**GEMINI_LEVELS),
     GeminiGenerateContentProvider),
    ("GEMINI_INTERACTIONS", GeminiInteractionsDialect(**GEMINI_LEVELS),
     GeminiInteractionsProvider),
]


def _binding(transport, dialect):
    values = {"provider": "factory", "model": "factory-model",
              "endpoint": "https://factory.invalid/v1", "transport": transport,
              "thinking_mode": "ENABLED"}
    if dialect is not None:
        values["dialect"] = dialect
    return ProviderBinding(**values)


def _http(provider):
    transport = provider._transport
    # The Chat adapter keeps its own transport around the shared one.
    return transport._http if isinstance(provider, OpenAICompatibleChatProvider) else transport


@pytest.mark.parametrize("transport,dialect,adapter", CASES, ids=[case[0] for case in CASES])
@pytest.mark.parametrize("trust_env,max_retries", [(True, 2), (False, 0), (False, 10)])
def test_each_transport_gets_its_adapter_with_the_requested_policy(
    transport, dialect, adapter, trust_env, max_retries,
):
    binding = _binding(transport, dialect)

    provider = create_provider(binding, api_key=KEY, trust_env=trust_env,
                               max_retries=max_retries)

    assert type(provider) is adapter
    assert provider.binding == binding
    assert _http(provider).trust_env is trust_env
    assert _http(provider).retry.max_retries == max_retries
    assert KEY not in repr(provider)


@pytest.mark.parametrize("transport,dialect,adapter", CASES, ids=[case[0] for case in CASES])
def test_defaults_trust_the_environment_and_retry_twice(transport, dialect, adapter):
    provider = create_provider(_binding(transport, dialect), api_key=KEY)

    assert _http(provider).trust_env is True
    assert _http(provider).retry.max_retries == 2


@pytest.mark.parametrize("values", [{"api_key": ""}, {"api_key": KEY, "max_retries": 11}],
                         ids=["empty-key", "retries-over-limit"])
def test_invalid_construction_is_refused(values):
    with pytest.raises(ValueError):
        create_provider(_binding("OPENAI_RESPONSES", None), **values)
