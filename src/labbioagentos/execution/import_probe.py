"""One bounded probe contract shared by the two image builders: imports, inventories, permissions."""

import hashlib
import re


PROGRAM = """import contextlib, glob, hashlib, importlib, importlib.metadata, json, os, stat, sys, sysconfig
modules = json.loads(sys.argv[1])
def unshared():
    # Every lab member runs the image under their own uid: each file must be readable and each
    # directory searchable by others. Links are not followed.
    roots = sorted({'/opt/labbio-env', sysconfig.get_paths()['purelib'], sysconfig.get_paths()['platlib']})
    count, paths = 0, []
    for root in roots:
        if not os.path.isdir(root) or os.path.islink(root):
            continue
        for base, dirs, files in os.walk(root):
            for path in [base] * (base == root) + [os.path.join(base, name) for name in dirs + files]:
                try:
                    mode = os.lstat(path).st_mode
                except OSError:
                    continue
                need = 0o005 if stat.S_ISDIR(mode) else 0o004
                if mode & need != need:  # A link's own mode always allows reading.
                    count += 1
                    paths.append(path) if len(paths) < 8 else None
    return count, paths
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
    conda = {}
    for path in sorted(glob.glob('/opt/labbio-env/conda-meta/*.json'))[:4096]:
        try:
            with open(path) as stream:
                record = json.load(stream)
            conda[record['name']] = record['version']
        except Exception:
            pass
conda_on_path = '/opt/labbio-env/bin' in os.environ.get('PATH', '').split(os.pathsep)
unshared_count, unshared_paths = unshared()
print(json.dumps({'installed_packages': packages, 'failed_imports': failed, 'import_errors': errors,
                  'conda_packages': conda, 'conda_on_path': conda_on_path,
                  'unshared_count': unshared_count, 'unshared_paths': unshared_paths}, sort_keys=True))
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
    # The conda inventory and the permission check (C15) are validated where they are used:
    # builder.probed_conda_packages and builder.probed_unshared.
    extra = {key: value[key] for key in ('conda_packages', 'conda_on_path', 'unshared_count', 'unshared_paths')
             if key in value}
    return {'installed_packages': packages, 'failed_imports': failed, 'import_errors': errors, **extra}


def import_diagnostics(inventory: dict) -> tuple[dict, ...]:
    return tuple({'code': 'IMPORT_CHECK_FAILED', 'module': module,
        **inventory['import_errors'].get(module, {})} for module in inventory['failed_imports'][:8])
