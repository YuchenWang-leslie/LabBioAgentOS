"""Construct the one adapter that speaks a binding's transport."""

from __future__ import annotations

from .anthropic_messages import AnthropicMessagesProvider
from .contracts import ModelProvider, ProviderBinding
from .gemini_content import GeminiGenerateContentProvider
from .gemini_interactions import GeminiInteractionsProvider
from .http_stream import DEFAULT_MAX_RETRIES, HttpStreamTransport, RetryPolicy
from .openai_chat import HttpxChatCompletionTransport, OpenAICompatibleChatProvider
from .openai_responses import OpenAIResponsesProvider

_STREAM_ADAPTERS = {
    "OPENAI_RESPONSES": OpenAIResponsesProvider,
    "ANTHROPIC_MESSAGES": AnthropicMessagesProvider,
    "GEMINI_GENERATE_CONTENT": GeminiGenerateContentProvider,
    "GEMINI_INTERACTIONS": GeminiInteractionsProvider,
}


def create_provider(
    binding: ProviderBinding,
    *,
    api_key: str,
    trust_env: bool = True,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> ModelProvider:
    """Return the adapter for the binding's transport; there is no fallback."""

    if binding.transport == "OPENAI_COMPATIBLE_CHAT":
        return OpenAICompatibleChatProvider(
            binding=binding,
            api_key=api_key,
            transport=HttpxChatCompletionTransport(
                trust_env=trust_env, max_retries=max_retries,
            ),
        )
    return _STREAM_ADAPTERS[binding.transport](
        binding=binding,
        api_key=api_key,
        transport=HttpStreamTransport(
            trust_env=trust_env, retry=RetryPolicy(max_retries=max_retries),
        ),
    )
