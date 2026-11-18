"""Replay the real empty-public LENGTH boundary; no model network or science."""

import pytest
from pathlib import Path

from labbioagentos.kernel import FinishReason, ModelTurn, ProviderProtocolError, PublicMessage, TokenUsage, ToolCall
from test_agent_loop import _tool_loop
from test_protocol_finish_diagnostics import _provider_and_request, _response
from labbioagentos.local_config import LocalProviderSettings


def recorded():
    return ModelTurn(tool_calls=(ToolCall(call_id='first-effect', name='record_value',
        arguments_json='{"value":"kept"}'),), finish_reason=FinishReason.TOOL_CALLS,
        usage=TokenUsage(total_tokens=4))


def empty(*, known=True, finish=FinishReason.LENGTH):
    return ModelTurn(content=None, finish_reason=finish,
        private_reasoning_present=True,
        usage=TokenUsage(total_tokens=7) if known else TokenUsage())


def test_default_total_completion_budget_leaves_room_for_reasoning_and_answer():
    settings = LocalProviderSettings(endpoint='https://provider.invalid/v1',
        env_file=Path('/not-opened/provider.env'), api_key_env='FIXTURE_KEY', model_identifier='fixture')
    assert settings.max_output_tokens == 32768
    assert settings.model_copy(update={'max_output_tokens':16384}).max_output_tokens == 16384


@pytest.mark.asyncio
async def test_supported_32k_completion_does_not_hit_a_smaller_private_text_guard():
    response = _response('tool_calls', None)
    response['choices'][0]['message']['tool_calls'] = [{'id':'call-1','type':'function',
        'function':{'name':'record_value','arguments':'{"value":"kept"}'}}]
    private = 'PRIVATE_CONTINUATION_CANARY_' * 4000
    response['choices'][0]['message']['reasoning_content'] = private
    response['usage'] = {'prompt_tokens':11, 'completion_tokens':32768, 'total_tokens':32779}
    provider, request, _transport = _provider_and_request(response)
    result = await provider.generate(request.model_copy(update={'max_output_tokens':32768}))
    assert result.private_reasoning_present
    assert list(provider._private_reasoning._entries.values()) == [private]
    assert 'PRIVATE_CONTINUATION_CANARY' not in result.model_dump_json() + repr(provider._private_reasoning)


@pytest.mark.asyncio
@pytest.mark.parametrize('length', (256_000,256_001))
async def test_private_character_limit_still_has_an_explicit_upper_boundary(length):
    response = _response('stop','Valid public output.')
    response['choices'][0]['message']['reasoning_content'] = 'x' * length
    provider, request, _transport = _provider_and_request(response)
    if length==256_000:
        result = await provider.generate(request)
        assert result.content=='Valid public output.' and not provider._private_reasoning._entries
    else:
        with pytest.raises(ProviderProtocolError) as caught:
            await provider.generate(request)
        assert caught.value.validation_fields==('message.reasoning_content',)
        assert not provider._private_reasoning._entries


@pytest.mark.asyncio
async def test_actual_chat_empty_length_is_incomplete_not_a_malformed_protocol():
    provider, request, transport = _provider_and_request(_response('length', ''))
    turn = await provider.generate(request)
    assert turn.content is None and turn.finish_reason is FinishReason.LENGTH
    assert turn.usage.total_tokens == 8203 and len(transport.requests) == 1
    assert turn.private_reasoning_present and not provider._private_reasoning._entries
    assert 'PRIVATE_REASONING_CANARY' not in turn.model_dump_json()


@pytest.mark.asyncio
async def test_empty_output_gets_one_explicit_feedback_without_replaying_tools():
    loop, provider, effects = _tool_loop((recorded(), empty(),
        ModelTurn(content='A concise public decision.', finish_reason=FinishReason.STOP,
                  usage=TokenUsage(total_tokens=3))))
    result = await loop.run(messages=(PublicMessage(role='USER', content='Continue within scope.'),),
        deadline_monotonic=10_000_000_000.0)
    assert result.termination == 'MODEL_COMPLETED'
    assert result.budget_usage.tokens == 14 and len(provider.requests) == 3
    assert effects == [{'value':'kept', 'nested':None}]
    feedback = provider.requests[-1].messages[-1].content
    assert 'NO_PUBLIC_OUTPUT_AT_LIMIT' in feedback and 'No tools' in feedback
    assert 'private' not in feedback.lower()
    assert len([e for e in result.events if e.kind.value=='MODEL_OUTPUT_REJECTED']) == 1


@pytest.mark.asyncio
async def test_repeated_empty_output_stops_without_unbounded_regeneration():
    loop, provider, effects = _tool_loop((recorded(), empty(), empty(),
        ModelTurn(content='Must not be reached.', finish_reason=FinishReason.STOP)))
    result = await loop.run(messages=(PublicMessage(role='USER', content='Keep the completed effect.'),),
        deadline_monotonic=10_000_000_000.0)
    assert result.termination == 'INCOMPLETE_PROVIDER_STOP'
    assert len(provider.requests) == 3 and result.budget_usage.tokens == 18
    assert len(effects) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ('unknown_usage','filter','partial','no_turns'))
async def test_adjacent_unrecoverable_boundaries_remain_closed(case):
    turn = (empty(known=False) if case=='unknown_usage' else
            empty(finish=FinishReason.CONTENT_FILTER) if case=='filter' else
            ModelTurn(content='Partial public text.', finish_reason=FinishReason.LENGTH)
            if case=='partial' else empty())
    loop, provider, effects = _tool_loop((recorded(), turn,
        ModelTurn(content='Not a permitted automatic retry.', finish_reason=FinishReason.STOP)),
        provider_turns=2 if case=='no_turns' else 8)
    result = await loop.run(messages=(PublicMessage(role='USER', content='Preserve facts.'),),
        deadline_monotonic=10_000_000_000.0)
    assert result.termination == 'INCOMPLETE_PROVIDER_STOP'
    assert len(provider.requests) == 2 and len(effects) == 1
    if case=='unknown_usage':
        assert result.budget_usage.tokens is None
