"""Explicit reuse of registered intermediates, never inferred code or data repair."""
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from native_test_provider import LocalProvider

from labbioagentos import LabBioRuntimeToolSet, WorkflowStage
from test_execution_inspection import inspection, _submit, _inspect  # noqa: F401
from test_runtime_milestone_b import boundary, _toolset  # noqa: F401
from test_skill_retrieval_grounding import boundary as gold_boundary, _pending_proposal  # noqa: F401


@pytest.mark.parametrize('mode', ['base', 'optional', 'data_bearing'])
@pytest.mark.parametrize('capability', ['execution_submit', 'execution_revise'])
def test_generated_native_input_guidance_publishes_both_uuid_sources(mode, capability):
    from labbioagentos import ExecutionRuntime, RequestedResources, RuntimeExecutionCapabilityView
    from labbioagentos.execution.models import ExecutionReceipt
    from labbioagentos.runtime.native_tools import build_labbio_tool_registry

    # Check actual published ToolSpecs, including the data-bearing field override.
    # The reference names must correspond to real public contracts, not a prose alias.
    checkpoint_field = 'registered_outputs'
    initial_field = 'mountable_input_artifact_ids'
    assert checkpoint_field in ExecutionReceipt.model_fields
    assert initial_field in RuntimeExecutionCapabilityView.model_fields
    view = None if mode == 'base' else RuntimeExecutionCapabilityView(
        runtime=ExecutionRuntime.PYTHON, image_key='fixture-image',
        resources=RequestedResources(), network_required=False,
        minimum_queryable_output_count=int(mode == 'data_bearing'),
        mountable_input_artifact_ids=(uuid4(),))
    spec = next(item for item in build_labbio_tool_registry(object(), view).specs
                if item.name == capability)
    for guidance in (spec.description, spec.parameters['properties']['input_artifact_ids']['description']):
        assert checkpoint_field in guidance
        assert initial_field in guidance
        assert 'invocation' in guidance.lower()
        assert 'scratch' in guidance.lower()


def test_mountable_input_view_distinguishes_initial_inputs_from_receipt_checkpoints():
    from labbioagentos import RuntimeExecutionCapabilityView
    guidance = RuntimeExecutionCapabilityView.model_json_schema()['properties'][
        'mountable_input_artifact_ids']['description']
    assert 'initial' in guidance.lower()
    assert 'registered_outputs' in guidance


def test_native_revision_guidance_identifies_original_scope_and_fresh_rerun():
    from labbioagentos.runtime.native_tools import build_labbio_tool_registry
    registry = build_labbio_tool_registry(object())
    spec = next(item for item in registry.specs if item.name == 'execution_revise')
    properties = spec.parameters['properties']
    for field in ('execution_id', 'expected_script_hash', 'edits'):
        assert field in properties
        assert field in spec.description
    assert 'execution_inspect' in registry.names
    assert 'execution_inspect' in spec.description
    assert 'same-run' in spec.description
    assert 'prior-run' in spec.description
    assert '.py' in spec.description
    assert 'fresh' in spec.description


@pytest.mark.asyncio
async def test_revision_explicitly_replaces_inputs_and_parameters(inspection, boundary):
    service, executor, run_id = inspection
    first = await _submit(inspection, boundary, 'value = 1\nprint(value)\n')
    checkpoint_id = next(iter(first.registered_outputs))
    base = _toolset(boundary, WorkflowStage.EXECUTE,
        ('execution_revise', 'execution_inspect'), execution_submission=service)
    tools = LabBioRuntimeToolSet(replace(base.binding, run_id=run_id,
        mountable_input_artifact_ids=()), base.services)
    provider = LocalProvider(tools)
    await provider.initialize()
    schemas = {t.name: t.inputSchema['parameters'] for t in await provider.list_tools()}
    assert 'input_artifact_ids' in schemas['execution_revise']['properties']
    assert 'parameters' in schemas['execution_revise']['properties']
    assert 'review_rationale' in schemas['execution_revise']['properties']
    result = await provider.call_tool('execution_revise', {
        'execution_id': str(first.execution_id), 'expected_script_hash': first.script_hash,
        'input_artifact_ids': [str(checkpoint_id)], 'parameters': {'mode': 'reuse'},
        'edits': [{'old': 'value = 1', 'new': 'value = 2'}]})
    assert result['success']
    plan = executor.plans[-1]
    assert plan.input_artifact_ids == (checkpoint_id,)
    assert plan.parameters == {'mode': 'reuse'}
    assert _inspect(inspection, boundary, first.execution_id)['submitted_contract']['input_artifact_ids'] == []
    assert _inspect(inspection, boundary, first.execution_id)['submitted_program']['source'] == 'value = 1\nprint(value)\n'
    assert len(executor.plans) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("second_old", "match_count"), [("SECRET_MISSING_OLD", 0), ("print(value)", 2)])
async def test_same_run_revision_reports_the_failing_edit_and_match_count(
    inspection, boundary, second_old, match_count,
):
    service, executor, run_id = inspection
    original = 'value = 1\nprint(value)\nprint(value)\n'
    first = await _submit(inspection, boundary, original)
    base = _toolset(boundary, WorkflowStage.EXECUTE, ('execution_revise',), execution_submission=service)
    tools = LabBioRuntimeToolSet(replace(base.binding, run_id=run_id), base.services)

    result = await tools.execution_revise(str(first.execution_id), first.script_hash, edits=[
        {'old': 'value = 1', 'new': 'value = 2'},
        {'old': second_old, 'new': 'SECRET_REPLACEMENT'},
    ])

    assert not result['success'] and result['error']['error_code'] == 'REVISION_MATCH_NOT_UNIQUE'
    assert result['error']['revision_validation']['issues'] == [
        {'field': 'edits.old', 'item_index': 1, 'reason': 'invalid_value'},
    ]
    assert f'{match_count} exact matches' in result['error']['safe_message']
    assert 'SECRET_' not in str(result)
    assert len(executor.plans) == 1
    assert _inspect(inspection, boundary, first.execution_id)['submitted_program']['source'] == original


@pytest.mark.asyncio
async def test_unknown_checkpoint_does_not_mount_or_execute(inspection, boundary):
    service, executor, run_id = inspection
    first = await _submit(inspection, boundary)
    base = _toolset(boundary, WorkflowStage.EXECUTE, ('execution_revise',), execution_submission=service)
    tools = LabBioRuntimeToolSet(replace(base.binding, run_id=run_id,
        mountable_input_artifact_ids=()), base.services)
    result = await tools.execution_revise(str(first.execution_id), first.script_hash,
        input_artifact_ids=[str(uuid4())])
    assert not result['success']
    assert result['error']['error_code'] == 'INVALID_EXECUTION_INPUT'
    assert len(executor.plans) == 1


@pytest.mark.asyncio
async def test_execution_review_requires_real_feedback_and_remains_model_context(inspection, boundary):
    service, executor, run_id = inspection
    first = await _submit(inspection, boundary, 'value = 1\n')
    base = _toolset(boundary, WorkflowStage.EXECUTE, ('execution_revise',), execution_submission=service)
    tools = LabBioRuntimeToolSet(replace(base.binding, run_id=run_id), base.services)
    failed = await tools.execution_revise(str(first.execution_id), '0' * 64)
    assert failed['error']['error_code'] == 'REVISION_HASH_MISMATCH'
    assert 'Explicit input_artifact_ids and parameters replace' in failed['error']['safe_message']
    assert 'requires execution_submit' not in failed['error']['safe_message']
    actual_feedback = UUID(failed['capability_invocation_id'])
    invented = await tools.execution_revise(str(first.execution_id), first.script_hash,
        review={'rationale': 'Use the verified base.', 'feedback_invocation_ids': [str(uuid4())]})
    assert invented['success']
    assert invented['retained_metadata']['execution_review_status'] == 'IGNORED_INVALID'
    assert tools.evidence_items()[-1].execution_review is None
    assert len(executor.plans) == 2
    tools.mark_feedback_delivered((failed,))
    completed = await tools.execution_revise(str(first.execution_id), first.script_hash,
        review={'rationale': 'Use the actual source hash returned for this base.',
                'feedback_invocation_ids': [str(actual_feedback)]})
    assert completed['success']
    audit = tools.evidence_items()[-1].execution_review
    assert audit.authority == 'MODEL_CONTEXT'
    assert audit.feedback_invocation_ids == (actual_feedback,)
    assert len(executor.plans) == 3
    assert type(tools.evidence_items()[-1]).model_validate_json(
        tools.evidence_items()[-1].model_dump_json()).execution_review == audit


@pytest.mark.asyncio
async def test_malformed_or_unread_gold_review_is_ignored(inspection, boundary):
    service, executor, run_id = inspection
    base = _toolset(boundary, WorkflowStage.EXECUTE, ('execution_submit',), execution_submission=service)
    tools = LabBioRuntimeToolSet(replace(base.binding, run_id=run_id), base.services)
    for review in ({'rationale': '   '}, {'rationale': 'x', 'SECRET_FIELD': 'SECRET_VALUE'},
                   {'rationale': '/private/SECRET_PATH'},
                   {'rationale': '-----BEGIN PRIVATE KEY-----\nSECRET_KEY\n-----END PRIVATE KEY-----'},
                   {'rationale': 'Use guidance.', 'gold_authorization_ids': [str(uuid4())]}):
        result = await tools.execution_submit('approved', 'pass\n', review=review)
        assert result['success']
        assert result['retained_metadata']['execution_review_status'] == 'IGNORED_INVALID'
        assert tools.evidence_items()[-1].execution_review is None
        assert 'SECRET_' not in str(result)
    assert len(executor.plans) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("review_arguments", [
    {"review_feedback_invocation_ids": [str(uuid4())]},
    {"review_gold_authorization_ids": [str(uuid4())]},
    {"review": '{"rationale":"PRIVATE_CANARY"}'},
    {"review_feedback_invocation_ids": "not-an-array"},
])
async def test_native_invalid_review_keeps_execution_and_drops_annotation(
    inspection, boundary, review_arguments,
):
    service, executor, run_id = inspection
    base = _toolset(boundary, WorkflowStage.EXECUTE, ('execution_submit',),
                    execution_submission=service)
    tools = LabBioRuntimeToolSet(replace(base.binding, run_id=run_id), base.services)
    response = await LocalProvider(tools).call_tool("execution_submit", {
        "image_key": "approved",
        "script_content": "pass\n",
        **({"review_rationale": "Considered the supplied reference."}
           if "review" not in review_arguments else {}),
        **review_arguments,
    })
    assert response["success"]
    assert response["retained_metadata"]["execution_review_status"] == "IGNORED_INVALID"
    assert tools.evidence_items()[-1].execution_review is None
    assert "PRIVATE_CANARY" not in str(response)
    assert len(executor.plans) == 1


@pytest.mark.asyncio
async def test_partial_failed_execution_checkpoint_is_the_only_revision_mount(boundary, tmp_path):
    import json
    from pathlib import Path
    from labbioagentos import ArtifactExposureClass, ArtifactRepresentation
    from test_execution_output_error_feedback import _execution_tools
    document = b'{"schema_id":"synthetic.records.v1","records":[{"record_type":"fixture","value":1}]}'
    base, runner, contract = _execution_tools(boundary, tmp_path, document)
    source = tmp_path / 'synthetic-input.txt'
    source.write_text('synthetic input only', encoding='utf-8')
    workspace, store = boundary[4], boundary[5]
    raw = store.register_file(source, artifact_type='fixture-input',
        exposure_class=ArtifactExposureClass.RAW, representation=ArtifactRepresentation(),
        owner_user_id=workspace.user_id, project_id=workspace.project_id, lab_id=workspace.lab_id)
    tools = LabBioRuntimeToolSet(replace(base.binding,
        capability_allowlist=('execution_submit', 'execution_revise', 'execution_inspect'),
        mountable_input_artifact_ids=(raw.artifact_id,)), base.services)
    runner.exit_code = 1
    first = await tools.execution_submit('python-analysis', '# initial fixture\npass\n',
        input_artifact_ids=[str(raw.artifact_id)], requested_outputs=[{
            'relative_path': 'result.json', 'artifact_type': 'fixture-checkpoint',
            'requested_exposure': 'DERIVED', 'output_contract_id': contract.contract_id}])
    receipt = first['data']
    assert receipt['status'] == 'FAILED'
    checkpoint = next(iter(receipt['registered_outputs']))
    original_ref = store.get_ref(UUID(checkpoint))
    original_bytes = Path(original_ref.storage_locator).read_bytes()
    runner.exit_code = 0
    tools.mark_feedback_delivered((first,))
    revised = await tools.execution_revise(receipt['execution_id'], receipt['script_hash'],
        edits=[{'old': '# initial fixture', 'new': '# explicit reuse fixture'}],
        input_artifact_ids=[checkpoint], parameters={'mode': 'reuse'},
        review={'rationale': 'Reuse the registered fixture output; the prior process failed later.',
                'feedback_invocation_ids': [first['capability_invocation_id']]})
    assert revised['success'] and revised['data']['status'] == 'SUCCEEDED'
    assert len(runner.calls) == 2
    execution = tmp_path / 'executions' / revised['data']['execution_id']
    manifest = json.loads((execution / 'input-manifest.json').read_text())
    assert set(manifest) == {checkpoint}
    assert str(raw.artifact_id) not in manifest
    argv = runner.calls[-1][0]
    input_mounts = [argv[i+1] for i, item in enumerate(argv[:-1])
                    if item == '--mount' and 'target=' + manifest[checkpoint] in argv[i+1]]
    assert len(input_mounts) == 1 and 'readonly' in input_mounts[0]
    assert original_ref.storage_locator in input_mounts[0]
    assert Path(original_ref.storage_locator).read_bytes() == original_bytes
    assert raw.storage_locator not in str(argv)
    latest = await tools.execution_inspect(revised['data']['execution_id'])
    assert latest['data']['submitted_contract']['input_artifact_ids'] == [checkpoint]
    assert latest['data']['submitted_contract']['parameters'] == {'mode': 'reuse'}


@pytest.mark.asyncio
async def test_review_gold_requires_scoped_approval_and_real_context_access(gold_boundary):
    from test_c9_gold_skill_lifecycle import _decide_use
    tools, _, _ = gold_boundary
    proposed = await _pending_proposal(gold_boundary)
    service = tools.services.skill_service
    proposal = service.store.get_use_proposal(UUID(proposed['proposal_id']))
    authorization = _decide_use(service, tools.binding.principal, proposal, approved=True)
    review = {'rationale': 'Adapt the accessed fixture guidance.',
              'gold_authorization_ids': [str(authorization.authorization_id)]}
    with pytest.raises(ValueError):
        tools._review_execution(review)  # Authorization alone is not a read.
    tools._skill_view(authorization.authorization_id)
    assert tools._review_execution(review).gold_authorization_ids == (authorization.authorization_id,)
    other_run = LabBioRuntimeToolSet(replace(tools.binding, run_id=uuid4()), tools.services)
    with pytest.raises(ValueError):
        other_run._review_execution(review)


@pytest.mark.asyncio
async def test_revision_none_inherits_but_empty_overrides_clear(inspection, boundary):
    service, executor, run_id = inspection
    first = await _submit(inspection, boundary)
    checkpoint = next(iter(first.registered_outputs))
    base = _toolset(boundary, WorkflowStage.EXECUTE, ('execution_revise',), execution_submission=service)
    tools = LabBioRuntimeToolSet(replace(base.binding, run_id=run_id), base.services)
    selected = (await tools.execution_revise(str(first.execution_id), first.script_hash,
        input_artifact_ids=[str(checkpoint)], parameters={'mode': 'reuse'}))['data']
    inherited = await tools.execution_revise(selected['execution_id'], selected['script_hash'])
    assert inherited['success']
    assert inherited['data']['execution_id'] != selected['execution_id']
    assert executor.plans[-1].script_content == executor.plans[-2].script_content
    assert executor.plans[-1].input_artifact_ids == (checkpoint,)
    assert executor.plans[-1].parameters == {'mode': 'reuse'}
    cleared = await tools.execution_revise(selected['execution_id'], selected['script_hash'],
        input_artifact_ids=[], parameters={})
    assert cleared['success']
    assert executor.plans[-1].input_artifact_ids == () and executor.plans[-1].parameters == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['missing', 'symlink', 'invalid_contract'])
async def test_failed_process_keeps_collection_checks_and_primary_failure(boundary, tmp_path, failure):
    from test_execution_output_error_feedback import _execution_tools
    tools, runner, contract = _execution_tools(boundary, tmp_path, b'INVALID_DOCUMENT')
    runner.exit_code = 1
    if failure == 'missing':
        runner.output_writer = lambda root: None
    elif failure == 'symlink':
        def symlink(root):
            (root / 'undeclared.txt').write_text('PRIVATE_CONTENT')
            (root / 'result.json').symlink_to(root / 'undeclared.txt')
        runner.output_writer = symlink
    result = await tools.execution_submit('python-analysis', 'pass\n', requested_outputs=[{
        'relative_path': 'result.json', 'artifact_type': 'fixture-checkpoint',
        'requested_exposure': 'DERIVED', 'output_contract_id': contract.contract_id}])
    receipt = result['data']
    assert receipt['status'] == 'FAILED' and receipt['exit_code'] == 1
    assert 'NON_ZERO_EXIT' in receipt['issue_codes']
    assert 'Execution failed: NON_ZERO_EXIT.' in receipt['issue_messages']
    assert receipt['output_artifact_ids'] == []
    assert len(receipt['registered_outputs']) == int(failure == 'invalid_contract')
    assert receipt['output_issues']
    assert 'PRIVATE_CONTENT' not in str(result)
    assert len(runner.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('input_artifact_ids', ['NOT_UUID']),
    ('input_artifact_ids', [str(uuid4())] * 129), ('parameters', {'x': 'x' * 70000})])
async def test_new_revision_fields_fail_safely_before_execution(inspection, boundary, field, value):
    service, executor, run_id = inspection
    first = await _submit(inspection, boundary)
    base = _toolset(boundary, WorkflowStage.EXECUTE, ('execution_revise',), execution_submission=service)
    tools = LabBioRuntimeToolSet(replace(base.binding, run_id=run_id), base.services)
    failed = await tools.execution_revise(str(first.execution_id), first.script_hash, **{field: value})
    assert failed['error']['error_code'] == 'INVALID_EXECUTION_REVISION'
    assert field in {issue['field'] for issue in failed['error']['revision_validation']['issues']}
    assert len(executor.plans) == 1
