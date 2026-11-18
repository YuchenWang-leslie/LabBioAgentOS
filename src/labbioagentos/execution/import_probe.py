"""One bounded import-failure contract shared by the two image builders."""

import hashlib
import re


PROGRAM = """import contextlib, hashlib, importlib, importlib.metadata, json, sys
modules = json.loads(sys.argv[1])
failed, errors = [], {}
with open('/tmp/import-check.log', 'w') as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
    for module in modules:
        try:
            importlib.import_module(module)
        except BaseException as error:
            failed.append(module)
            message = str(error)
            detail = {'exception_type': type(error).__name__, 'message': message[:2000],
                'message_sha256': hashlib.sha256(message.encode('utf-8', errors='replace')).hexdigest(),
                'truncated': len(message) > 2000}
            if isinstance(error, ModuleNotFoundError) and isinstance(error.name, str):
                detail['missing_module'] = error.name
            errors[module] = detail
    packages = {item.metadata['Name']: item.version for item in importlib.metadata.distributions()}
print(json.dumps({'installed_packages': packages, 'failed_imports': failed, 'import_errors': errors}, sort_keys=True))
"""

_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")


def parse_import_probe(value: dict, modules: tuple[str, ...]) -> dict:
    """Validate identities and sizes before either adapter trusts probe output.

    Historical probes without import_errors remain readable, but do not acquire
    inferred exception details. No module output, traceback locals or paths are
    parsed to invent a repair.
    """
    if not isinstance(value, dict):
        raise ValueError("Invalid import probe")
    failed, packages, errors = value.get('failed_imports'), value.get('installed_packages'), value.get('import_errors', {})
    if (not isinstance(failed, list) or any(not isinstance(item, str) or item not in modules for item in failed)
            or len(set(failed)) != len(failed) or not isinstance(packages, dict) or not isinstance(errors, dict)
            or ('import_errors' in value and set(errors) != set(failed))):
        raise ValueError("Invalid import probe")
    for detail in errors.values():
        if (not isinstance(detail, dict) or set(detail) - {
                'exception_type', 'missing_module', 'message', 'message_sha256', 'truncated'}
                or not isinstance(detail.get('exception_type'), str)
                or re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', detail['exception_type']) is None
                or not isinstance(detail.get('message'), str) or len(detail['message']) > 2000
                or type(detail.get('truncated')) is not bool
                or not isinstance(detail.get('message_sha256'), str)
                or re.fullmatch(r'[0-9a-f]{64}', detail['message_sha256']) is None):
            raise ValueError("Invalid import probe")
        if 'missing_module' in detail and (
                detail['exception_type'] != 'ModuleNotFoundError'
                or not isinstance(detail['missing_module'], str) or len(detail['missing_module']) > 128
                or _MODULE.fullmatch(detail['missing_module']) is None):
            raise ValueError("Invalid import probe")
        if not detail['truncated'] and hashlib.sha256(
                detail['message'].encode('utf-8', errors='replace')).hexdigest() != detail['message_sha256']:
            raise ValueError("Invalid import probe")
    return {'installed_packages': packages, 'failed_imports': failed, 'import_errors': errors}


def import_diagnostics(inventory: dict) -> tuple[dict, ...]:
    return tuple({'code': 'IMPORT_CHECK_FAILED', 'module': module,
        **inventory['import_errors'].get(module, {})} for module in inventory['failed_imports'][:8])
