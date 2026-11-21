"""Build diagnostics must survive probe, adapter, service and model feedback."""

import hashlib
import json
import subprocess
import sys

import pytest

from labbioagentos.execution.apptainer_build import BuildExit, classify_build
from labbioagentos.execution.apptainer_environment_builder import _probe_inventory
from labbioagentos.execution.environment_builder import _PROBE, pip_log_diagnostics
from labbioagentos.execution.environments import EnvironmentBuildResult
from labbioagentos.execution.import_probe import parse_import_probe
from test_environment_builder import backend, build  # noqa: F401
from test_environment_tooling import _tools
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_execution_environments import environment  # noqa: F401


def test_actual_import_probe_preserves_nested_failure_without_module_output(tmp_path):
    program = """import importlib
def fail(name):
    if name == 'healthy':
        print('IMPORT_STDOUT_PRIVATE')
        return None
    raise ModuleNotFoundError("No module named 'nested_dep'", name='nested_dep')
importlib.import_module = fail
""" + _PROBE.replace("'/tmp/import-check.log'", repr(str(tmp_path / 'imports.log')))
    result = subprocess.run(
        [sys.executable, '-I', '-c', program, '["healthy", "outer_pkg"]'],
        capture_output=True, text=True, check=True,
    )
    record = json.loads(result.stdout)
    assert record['failed_imports'] == ['outer_pkg']
    detail = record['import_errors']['outer_pkg']
    assert detail['exception_type'] == 'ModuleNotFoundError'
    assert detail['missing_module'] == 'nested_dep'
    assert detail['message'] == "No module named 'nested_dep'"
    assert detail['message_sha256'] == hashlib.sha256(detail['message'].encode()).hexdigest()
    assert 'IMPORT_STDOUT_PRIVATE' not in result.stdout + result.stderr


def test_apptainer_decoder_retains_probe_details(tmp_path):
    details = {'outer_pkg': {'exception_type': 'ImportError', 'message': 'ABI mismatch',
        'message_sha256': hashlib.sha256(b'ABI mismatch').hexdigest(), 'truncated': False}}
    probe = tmp_path / 'probe.json'
    probe.write_text(json.dumps({'installed_packages': {'outer-pkg': '1.0'},
        'failed_imports': ['outer_pkg'], 'import_errors': details, 'unshared_count': 0}))  # C15 probe field.
    assert _probe_inventory(probe, ('outer_pkg',))['import_errors'] == details


@pytest.mark.asyncio
async def test_structural_import_cause_reaches_receipt_and_persisted_observation(environment):
    environment.builder.result = EnvironmentBuildResult(failure_code='IMPORT_CHECK_FAILED',
        diagnostics=({'code': 'IMPORT_CHECK_FAILED', 'module': 'outer_pkg',
            'exception_type': 'ModuleNotFoundError', 'missing_module': 'nested_dep'},))
    result = await environment.service.build_environment('base', (), ('outer_pkg',))
    detail = result['diagnostics'][0]
    assert detail['exception_type'] == 'ModuleNotFoundError'
    assert detail['missing_module'] == 'nested_dep'
    saved = json.loads(next(environment.root.glob('attempts/*/receipt.json')).read_text())
    assert saved['diagnostics'] == result['diagnostics']
    assert [image.key for image in environment.registry.list()] == ['base']


@pytest.mark.parametrize('line, expected', (
    ('pyramid 1.5.2 requires WebOb, which is not installed.',
        {'package': 'pyramid', 'version': '1.5.2', 'dependency': 'webob'}),
    ('pyramid 1.5.2 has requirement WebOb>=1.3.1, but you have WebOb 0.8.',
        {'package': 'pyramid', 'version': '1.5.2', 'dependency': 'webob>=1.3.1',
         'installed_version': '0.8'}),
))
def test_pip_check_returns_actual_dependency_relation(line, expected):
    diagnostics = pip_log_diagnostics(line, 'PIP_CHECK_FAILED')
    assert diagnostics == ({'code': 'PIP_CHECK_FAILED', **expected},)


def test_apptainer_pip_check_stage_does_not_discard_diagnostics(tmp_path):
    (tmp_path / 'check.log').write_text('pyramid 1.5.2 requires WebOb, which is not installed.\n')
    code, details = classify_build(BuildExit.CHECK_FAILED, timed_out=False, log_dir=tmp_path)
    assert code == 'PIP_CHECK_FAILED'
    assert details[0]['dependency'] == 'webob'


def test_legacy_and_successful_probes_remain_legal():
    for failed in ([], ['outer_pkg']):
        result = parse_import_probe({'installed_packages': {}, 'failed_imports': failed}, ('outer_pkg',))
        assert result['failed_imports'] == failed
        assert result['import_errors'] == {}


@pytest.mark.parametrize('change', (
    {'exception_type': 'Error\nPRIVATE'}, {'message': 'x' * 2001},
    {'message_sha256': 'a' * 64}, {'truncated': 1},
    {'missing_module': '/private/lib'}, {'missing_module': 'x; install something'},
    {'stdout_body': 'PRIVATE'},
))
def test_malformed_probe_details_are_not_released(change):
    detail = {'exception_type': 'ImportError', 'message': 'ABI mismatch',
        'message_sha256': hashlib.sha256(b'ABI mismatch').hexdigest(), 'truncated': False, **change}
    with pytest.raises(ValueError):
        parse_import_probe({'installed_packages': {}, 'failed_imports': ['outer_pkg'],
            'import_errors': {'outer_pkg': detail}}, ('outer_pkg',))


def test_probe_errors_cannot_attach_to_other_or_successful_modules():
    for failed, errors in ((['other'], {}), ([], {'outer_pkg': {}}), (['outer_pkg'], {})):
        with pytest.raises(ValueError):
            parse_import_probe({'installed_packages': {}, 'failed_imports': failed,
                'import_errors': errors}, ('outer_pkg',))


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', (False, True))
async def test_import_message_release_is_explicit_redacted_and_source_bound(environment, enabled):
    message = 'binary ABI mismatch; token=PRIVATE_TOKEN /private/path https://secret:password@host/path'
    environment.service.include_error_context = enabled
    environment.builder.result = EnvironmentBuildResult(failure_code='IMPORT_CHECK_FAILED', diagnostics=({
        'code': 'IMPORT_CHECK_FAILED', 'module': 'outer_pkg', 'exception_type': 'ValueError',
        'message': message, 'message_sha256': hashlib.sha256(message.encode()).hexdigest(), 'truncated': False,
    },))
    receipt = await environment.service.build_environment('base', (), ('outer_pkg',))
    detail = receipt['diagnostics'][0]
    assert detail['exception_type'] == 'ValueError'
    assert ('error_context' in detail) is enabled
    assert 'PRIVATE_TOKEN' not in json.dumps(receipt)
    assert '/private/path' not in json.dumps(receipt)
    assert 'https://' not in json.dumps(receipt)
    if enabled:
        assert 'binary ABI mismatch' in detail['error_context']['text']
        assert detail['error_context']['redacted']
        assert detail['error_context']['message_sha256'] == hashlib.sha256(message.encode()).hexdigest()


def test_wrong_package_in_pip_check_is_not_misattributed():
    line = 'pyramid 1.5.2 has requirement WebOb>=1.3.1, but you have otherpkg 0.8.'
    assert pip_log_diagnostics(line, 'PIP_CHECK_FAILED') == ({'code': 'PIP_CHECK_FAILED'},)


@pytest.mark.asyncio
async def test_docker_compatibility_uses_the_same_probe_contract(backend):
    detail = {'exception_type': 'ImportError', 'message': 'ABI mismatch',
        'message_sha256': hashlib.sha256(b'ABI mismatch').hexdigest(), 'truncated': False}
    backend.responses['verification.log'] = (0, None, json.dumps({
        'installed_packages': {'examplepkg': '2.0'}, 'failed_imports': ['examplepkg'],
        'import_errors': {'examplepkg': detail},
    }))
    result = await build(backend)
    assert result.failure_code == 'IMPORT_CHECK_FAILED'
    assert result.diagnostics == ({'code': 'IMPORT_CHECK_FAILED', 'module': 'examplepkg', **detail},)


@pytest.mark.asyncio
async def test_original_failure_reaches_native_tool_feedback_and_observation(artifact_query_boundary, environment):
    tools, _ = _tools(artifact_query_boundary, environment)
    environment.builder.result = EnvironmentBuildResult(failure_code='IMPORT_CHECK_FAILED', diagnostics=({
        'code': 'IMPORT_CHECK_FAILED', 'module': 'outer_pkg',
        'exception_type': 'ModuleNotFoundError', 'missing_module': 'nested_dep',
    },))
    response = await tools.environment_build('base', [], ['outer_pkg'])
    assert response['success'] and response['data']['status'] == 'FAILED'
    detail = response['data']['diagnostics'][0]
    assert detail['missing_module'] == 'nested_dep'
    assert tools.evidence_items()[-1].safe_result['diagnostics'] == response['data']['diagnostics']
    assert tools.stop_reason is None  # A confirmed failed build is not an unknown side effect.


@pytest.mark.asyncio
async def test_dependency_relation_survives_service_projection(environment):
    diagnostics = pip_log_diagnostics(
        'pyramid 1.5.2 has requirement WebOb>=1.3.1, but you have WebOb 0.8.', 'PIP_CHECK_FAILED')
    environment.builder.result = EnvironmentBuildResult(failure_code='PIP_CHECK_FAILED', diagnostics=diagnostics)
    receipt = await environment.service.build_environment('base', ())
    assert receipt['diagnostics'][0]['dependency'] == 'webob>=1.3.1'
    assert receipt['diagnostics'][0]['installed_version'] == '0.8'


@pytest.mark.parametrize('enabled', (False, True))
def test_local_apptainer_wires_explicit_build_text_policy(tmp_path, enabled):
    from contextlib import closing
    from labbioagentos import ApprovedImageRegistry
    from labbioagentos.execution.job_ledger import JobLedger
    from labbioagentos.local_config import _environment_wiring, load_settings
    from test_apptainer_configuration import _write

    path = _write(tmp_path, launcher='', enforcement='systemd-affinity', extra=(
        '[environment]\nroot = "environments"\ninclude_error_context = '
        + str(enabled).lower() + '\n[environment.local_apptainer]\nmachine = "x86_64"\n'))
    settings = load_settings(path)
    with closing(JobLedger(tmp_path / 'run/jobs.sqlite')) as ledger:
        service = _environment_wiring(settings, tmp_path / 'run', ledger, None)[
            'environment_service_factory'](ApprovedImageRegistry(()))
        assert service.include_error_context is enabled
        assert service.builder.build_launcher.resource_enforcement == 'systemd-affinity'


@pytest.mark.asyncio
async def test_long_unicode_error_keeps_truthful_bounds_and_original_hash(environment):
    message = '错' * 2000
    environment.service.include_error_context = True
    environment.builder.result = EnvironmentBuildResult(failure_code='IMPORT_CHECK_FAILED', diagnostics=({
        'code': 'IMPORT_CHECK_FAILED', 'module': 'outer_pkg', 'exception_type': 'ValueError',
        'message': message, 'message_sha256': hashlib.sha256(message.encode()).hexdigest(), 'truncated': False,
    },))
    receipt = await environment.service.build_environment('base', (), ('outer_pkg',))
    context = receipt['diagnostics'][0]['error_context']
    assert len(context['text']) == 1000
    assert context['truncated'] is True and context['redacted'] is False
    assert context['message_sha256'] == hashlib.sha256(message.encode()).hexdigest()
