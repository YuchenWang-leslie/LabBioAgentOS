"""Trusted execution input authority survives root and delegated tool assembly."""

from dataclasses import replace
from hashlib import sha256
from types import MethodType
from uuid import uuid4

import pytest
from pantheon.agent import AgentResponse

from labbioagentos import (
    PantheonRuntimeFactory,
    PerInvocationPantheonStageInvoker,
    RuntimeCapabilityServices,
    RuntimeInvocationMode,
    RuntimeProfileCatalog,
    RuntimeProfileConfigurationError,
)
from labbioagentos.execution.models import (
    ExecutionFailureClass,
    ExecutionReceipt,
    ExecutionStatus,
)
from test_c12_execution_capability_contract import (
    _execution_schema,
    _trusted_view,
    _valid_wire_draft,
)
from test_c8_scientific_specialists import (
    ROOT_KEY,
    SPECIALIST_KEY,
    _agent_run_context,
    _assembly,
    _catalog,
    _result,
    _stage_input,
    governed_boundary,
)


class _Submission:
    def __init__(self):
        self.calls = []
        self.receipts = []

    async def submit(self, draft, **kwargs):
        self.calls.append((draft, kwargs))
        receipt = ExecutionReceipt(
            execution_id=uuid4(),
            status=ExecutionStatus.FAILED,
            image_key=draft.image_key,
            script_hash=sha256(draft.script_content.encode("utf-8")).hexdigest(),
            exit_code=1,
            issue_codes=(ExecutionFailureClass.NON_ZERO_EXIT,),
        )
        self.receipts.append(receipt)
        return receipt


class _BindingFactory(PantheonRuntimeFactory):
    """Use real team assembly/delegation with deterministic synthetic agents."""

    def __init__(self, selected_ids):
        source = _catalog()
        specialist_capability = source.agents[SPECIALIST_KEY].capability_profile_key
        catalog = RuntimeProfileCatalog(
            agents=tuple(source.agents.values()),
            prompts=tuple(source.prompts.values()),
            models=tuple(source.models.values()),
            schemas=tuple(source.schemas.values()),
            capabilities=tuple(
                profile.model_copy(update={"capability_allowlist": ("execution_submit",)})
                if key == specialist_capability else profile
                for key, profile in source.capabilities.items()
            ),
        )
        super().__init__(catalog)
        self.selected_ids = selected_ids
        self.toolsets = {}
        self.created_team_count = 0

    async def create_team(self, profile_keys, **kwargs):
        team, prompts = await super().create_team(profile_keys, **kwargs)
        self.created_team_count += 1
        if kwargs.get("invocation_mode") is RuntimeInvocationMode.CAPABILITY:
            self.toolsets = dict(kwargs["toolsets"])
            agents = {agent.name: agent for agent in team.team_agents}

            async def submit(profile_key):
                wire = _valid_wire_draft()
                wire["input_artifact_ids"] = [str(value) for value in self.selected_ids]
                result = await self.toolsets[profile_key].execution_submit(**wire)
                assert result["success"]

            async def child_run(agent_self, _message, **_run_kwargs):
                await submit(SPECIALIST_KEY)
                return AgentResponse(agent_name=agent_self.name, content="synthetic child done", details=None)

            async def root_run(agent_self, _message, **run_kwargs):
                await submit(ROOT_KEY)
                with _agent_run_context(agent_self, run_kwargs):
                    await agent_self.functions["call_agent"](
                        agent_name="SingleCellAnalysisSpecialist",
                        instruction="Exercise the independently bound tool.",
                        context_variables={
                            "tool_call_id": "binding-delegation",
                            "_metadata": {"chain_path": ["executionagent"]},
                        },
                    )
                return AgentResponse(agent_name=agent_self.name, content="synthetic root done", details=None)

            agents["ExecutionAgent"].run = MethodType(root_run, agents["ExecutionAgent"])
            agents["SingleCellAnalysisSpecialist"].run = MethodType(
                child_run, agents["SingleCellAnalysisSpecialist"]
            )
        else:
            async def finalize(_team_self, _message, **_kwargs):
                return AgentResponse(agent_name="ExecutionAgent", content=_result(_message).model_dump(mode="json"), details=None)

            team.run = MethodType(finalize, team)
        return team, prompts


def _invoker(boundary, trusted_view, selected_ids):
    principal, workspace, store, exposure, _, _ = boundary
    factory, submission = _BindingFactory(selected_ids), _Submission()
    assembly = _assembly()
    assembly = replace(assembly, capability_peer_specs=tuple(
        replace(peer, capability_allowlist=("execution_submit",))
        if peer.profile_key == SPECIALIST_KEY else peer
        for peer in assembly.capability_peer_specs
    ))
    invoker = PerInvocationPantheonStageInvoker(
        assembly=assembly, factory=factory, principal=principal, workspace=workspace,
        services=RuntimeCapabilityServices(
            artifact_store=store, artifact_exposure=exposure,
            execution_submission=submission,
        ),
        execution_capability=trusted_view,
    )
    return invoker, factory, submission


@pytest.mark.parametrize("authority", [None, (), (uuid4(), uuid4())])
async def test_constructor_input_authority_reaches_root_and_delegated_submission(
    governed_boundary, authority,
):
    trusted = (
        None if authority is None
        else _trusted_view().model_copy(update={"mountable_input_artifact_ids": authority})
    )
    selected = (authority[0],) if authority else ()
    invoker, factory, submission = _invoker(governed_boundary, trusted, selected)
    presented = (
        None if trusted is None
        else trusted.model_copy(update={"mountable_input_artifact_ids": tuple(list(authority))})
    )
    stage_input = _stage_input().model_copy(update={"execution_capability": presented})

    result = await invoker.invoke(stage_input)

    assert len(submission.calls) == 2
    assert result.body.execution_status == "FAILED"
    assert result.body.execution_reference.reference_id == str(submission.receipts[0].execution_id)
    assert result.body.output_artifact_references == ()
    for key in (ROOT_KEY, SPECIALIST_KEY):
        toolset = factory.toolsets[key]
        assert toolset.binding.mountable_input_artifact_ids == authority
        parameters = (await _execution_schema(toolset))["parameters"]
        assert "mountable_input_artifact_ids" not in parameters["properties"]
        assert "input_artifact_ids" in parameters["properties"]
    for draft, kwargs in submission.calls:
        assert kwargs["mountable_input_artifact_ids"] == authority
        assert kwargs["run_id"] == stage_input.run_id
        assert kwargs["invocation_id"] == stage_input.invocation_id
        # Binding authority is neither derived from nor substituted for model selection.
        assert draft.input_artifact_ids == selected
        if authority is not None:
            assert kwargs["mountable_input_artifact_ids"] is trusted.mountable_input_artifact_ids


@pytest.mark.parametrize("authority", [None, (), (uuid4(),)])
async def test_forged_stage_execution_view_cannot_widen_trusted_input_authority(
    governed_boundary, authority,
):
    trusted = (
        None if authority is None
        else _trusted_view().model_copy(update={"mountable_input_artifact_ids": authority})
    )
    forged = _trusted_view().model_copy(update={
        "mountable_input_artifact_ids": (*(authority or ()), uuid4()),
    })
    invoker, factory, submission = _invoker(governed_boundary, trusted, ())
    stage_input = _stage_input().model_copy(update={"execution_capability": forged})

    with pytest.raises(RuntimeProfileConfigurationError, match="trusted configuration"):
        await invoker.invoke(stage_input)

    assert factory.created_team_count == 0
    assert submission.calls == []
