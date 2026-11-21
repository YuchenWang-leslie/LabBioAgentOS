"""The explicit user installer never overwrites or invokes unchecked downloads."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


SCRIPT = (Path(__file__).resolve().parents[1]
          / "src/labbioagentos/resources/install-user-apptainer.sh")


class _InstallEnvironment(dict):
    def __repr__(self):
        return "<controlled installer environment>"


def _run(script, prefix, *, env=None):
    return subprocess.run(["/bin/bash", str(script), str(prefix)], env=env,
                          text=True, capture_output=True, timeout=15)


def test_existing_prefix_is_not_overwritten(tmp_path):
    prefix = tmp_path / "existing"
    prefix.mkdir()
    sentinel = prefix / "keep"
    sentinel.write_text("unchanged")
    outcome = _run(SCRIPT, prefix)
    assert outcome.returncode == 2
    assert "PREFIX_EXISTS" in outcome.stderr
    assert sentinel.read_text() == "unchanged"


@pytest.mark.parametrize("prefix", ["", "/", "relative-prefix", "/tmp/prefix with spaces", "/tmp/../alias"])
def test_unsafe_prefix_is_refused_before_creating_anything(prefix):
    outcome = _run(SCRIPT, prefix)
    assert outcome.returncode == 2
    assert "PREFIX_INVALID" in outcome.stderr


def test_prefix_parent_symlink_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    outcome = _run(SCRIPT, alias / "new")
    assert outcome.returncode == 2
    assert "PREFIX_SYMLINK" in outcome.stderr
    assert not (real / "new").exists()


@pytest.mark.parametrize("arguments", [[], ["/tmp/unused-prefix", "extra"]])
def test_argument_count_is_checked(arguments):
    outcome = subprocess.run(["/bin/bash", str(SCRIPT), *arguments], text=True,
                             capture_output=True, timeout=5)
    assert outcome.returncode == 2
    assert "PREFIX_INVALID" in outcome.stderr


@pytest.fixture
def installation(tmp_path):
    """Replace fixed download bytes, not the upstream CLI or integrity checks."""
    if os.geteuid() == 0:
        pytest.skip("The installer intentionally refuses root; use an ordinary-user test job")
    fixtures = tmp_path / "downloads"
    fixtures.mkdir()
    upstream = fixtures / "installer"
    upstream.write_text("""#!/bin/bash
set -eu
printf '%s\\n' "$@" > "$TEST_UPSTREAM_ARGS"
test "$#" = 5 && test "$1" = -d && test "$2" = el8 && test "$3" = -v
test -f "$4"
mkdir -p "$5/bin"
cp "$TEST_APPTAINER" "$5/bin/apptainer"
chmod 700 "$5/bin/apptainer"
if test "${TEST_SETUID:-0}" = 1; then chmod 4700 "$5/bin/apptainer"; fi
""")
    payloads = {"installer": upstream.read_bytes(), "runtime": b"fixed rpm fixture"}
    for name in ("rpm2cpio", "librpm9", "librpmio9"):
        payloads[name] = (name + " fixed deb fixture").encode()
    for name, data in payloads.items():
        (fixtures / name).write_bytes(data)

    fake = tmp_path / "apptainer"
    fake.write_text("""#!/bin/bash
case "$1" in
version) printf '%s\\n' "${TEST_VERSION:-1.5.5-1}";;
buildcfg) printf '%s\\n' "APPTAINER_SUID_INSTALL=${TEST_SUID_CONFIG:-0}";;
*) exit 2;;
esac
""")
    fake.chmod(0o700)
    commands = tmp_path / "bin"
    commands.mkdir()
    for name in ("bash", "python3", "mkdir", "mktemp", "realpath", "sha256sum",
                 "find", "grep", "cp", "env", "tail", "chmod", "tee", "dirname"):
        commands.joinpath(name).symlink_to(shutil.which(name))
    curl = commands / "curl"
    curl.write_text(f"""#!{sys.executable}
import os, pathlib, sys
a = sys.argv[1:]
assert '--proto' in a and a[a.index('--proto') + 1] == '=https'
assert '--fail' in a and '--max-time' in a and a[a.index('--max-time') + 1] == '600'
url = a[-1]
name = ('installer' if url.endswith('install-unprivileged.sh') else
        'runtime' if url.endswith('.rpm') else url.rsplit('/', 1)[-1].split('_')[0])
if os.environ.get('TEST_CURL_FAIL') == name:
    print('synthetic download failure', file=sys.stderr)
    sys.exit(int(os.environ.get('TEST_CURL_EXIT', '22')))
data = pathlib.Path(os.environ['TEST_DOWNLOADS'], name).read_bytes()
if os.environ.get('TEST_TAMPER') == name: data += b'tampered'
pathlib.Path(a[a.index('--output') + 1]).write_bytes(data)
""")
    curl.chmod(0o700)
    commands.joinpath("uname").write_text("#!/bin/bash\ncase \"$1\" in -s) echo Linux;; -m) echo x86_64;; esac\n")
    commands.joinpath("uname").chmod(0o700)
    for name in ("rpm2cpio", "cpio"):
        commands.joinpath(name).write_text("#!/bin/bash\nexit 0\n")
        commands.joinpath(name).chmod(0o700)

    script = tmp_path / "install.sh"
    source = SCRIPT.read_text() if SCRIPT.exists() else "exit 127\n"
    hashes = {
        "a097956eafa6ab3dd843ee429d0b774ed029753bfadf013b8046556228fac6f2": "installer",
        "e821c129c8ee49b3994816db3b763d1c6cfc69f3ed043af187e07d10dfe6d2c0": "runtime",
        "3221e62595718384418b22ad6566205c3023611b78b03168dc774920818a024c": "rpm2cpio",
        "93fefb127f13cc8fb0e1864786a41c2cc1a57b331063b5a82a9b8aea446031de": "librpm9",
        "88a8f3f992098f7e88bf85c033c25f1a5d33518e6cc5133ee7561f424f9ee7b7": "librpmio9",
    }
    for fixed, name in hashes.items():
        source = source.replace(fixed, hashlib.sha256(payloads[name]).hexdigest())
    release = tmp_path / "os-release"
    release.write_text('ID=ubuntu\nVERSION_ID="22.04"\n')
    source = source.replace("OS_RELEASE=/etc/os-release", f"OS_RELEASE={release}")
    script.write_text(source)
    env = _InstallEnvironment(PATH=str(commands), TEST_DOWNLOADS=str(fixtures),
                              TEST_APPTAINER=str(fake),
                              TEST_UPSTREAM_ARGS=str(tmp_path / "upstream-args"))
    return script, tmp_path / "user-install", env, commands


def _receipt(prefix):
    work = next(prefix.parent.glob(".labbio-apptainer-install.*"))
    return json.loads((work / "installation-receipt.json").read_text())


def test_verified_install_uses_official_cli_and_retains_provenance(installation):
    script, prefix, env, _ = installation
    outcome = _run(script, prefix, env=env)
    assert outcome.returncode == 0, outcome.stderr
    args = Path(env["TEST_UPSTREAM_ARGS"]).read_text().splitlines()
    assert args[:3] == ["-d", "el8", "-v"]
    assert args[3].endswith(".rpm") and Path(args[3]).is_absolute()
    assert args[4] == str(prefix)
    record = _receipt(prefix)
    assert record["status"] == "INSTALLED"
    assert record["version"] == "1.5.5-1"
    assert record["dependency_resolution"] == "official-installer-live-repositories"
    assert record["setuid"] is False
    assert (prefix / "installation-receipt.json").read_text() == (
        Path(record["work_dir"]) / "installation-receipt.json").read_text()


def test_first_install_creates_private_parents_without_changing_existing_parent(installation):
    script, prefix, env, _ = installation
    existing_mode = prefix.parent.stat().st_mode
    prefix = prefix.parent / "new-user-data" / "apptainer" / "1.5.5"
    outcome = _run(script, prefix, env=env)
    assert outcome.returncode == 0, outcome.stderr
    assert prefix.parent.stat().st_mode & 0o777 == 0o700
    assert prefix.parent.parent.stat().st_mode & 0o777 == 0o700
    assert prefix.parent.parent.parent.stat().st_mode == existing_mode
    assert _receipt(prefix)["status"] == "INSTALLED"


def test_unwritable_parent_preserves_existing_contents_and_permissions(installation):
    script, prefix, env, _ = installation
    parent = prefix.parent / "unwritable"
    parent.mkdir(mode=0o500)
    prefix = parent / "new-parent" / "apptainer"
    try:
        outcome = _run(script, prefix, env=env)
        assert outcome.returncode == 2
        assert "PREFIX_PARENT_UNAVAILABLE" in outcome.stderr
        assert parent.stat().st_mode & 0o777 == 0o500
        assert list(parent.iterdir()) == []
        assert not Path(env["TEST_UPSTREAM_ARGS"]).exists()
    finally:
        parent.chmod(0o700)


@pytest.mark.parametrize("variable", ["TEST_CURL_FAIL", "TEST_TAMPER"])
@pytest.mark.parametrize("artifact", ["installer", "runtime"])
def test_failed_or_unverified_download_never_executes_installer(installation, variable, artifact):
    script, prefix, env, _ = installation
    outcome = _run(script, prefix, env={**env, variable: artifact})
    assert outcome.returncode != 0
    assert not Path(env["TEST_UPSTREAM_ARGS"]).exists()
    assert _receipt(prefix)["status"] == "FAILED"
    assert _receipt(prefix)["version"] is None
    assert _receipt(prefix)["requested_version"] == "1.5.5"
    assert "Preserved installation diagnostics:" in outcome.stderr


def test_download_timeout_is_preserved_without_running_installer(installation):
    script, prefix, env, _ = installation
    outcome = _run(script, prefix, env={**env, "TEST_CURL_FAIL": "runtime", "TEST_CURL_EXIT": "28"})
    assert outcome.returncode == 28
    assert not Path(env["TEST_UPSTREAM_ARGS"]).exists()
    assert _receipt(prefix)["status"] == "FAILED"


@pytest.mark.parametrize("variable,value", [("TEST_VERSION", "1.4.0"),
                                            ("TEST_VERSION", "1.5.5"),
                                            ("TEST_VERSION", "1.5.5-1-extra"),
                                            ("TEST_SUID_CONFIG", "1"),
                                            ("TEST_SETUID", "1")])
def test_install_verification_refuses_wrong_version_or_privilege(installation, variable, value):
    script, prefix, env, _ = installation
    outcome = _run(script, prefix, env={**env, variable: value})
    assert outcome.returncode != 0
    assert _receipt(prefix)["status"] == "FAILED"


def test_ubuntu22_bootstrap_extracts_only_private_tools(installation):
    script, prefix, env, commands = installation
    commands.joinpath("rpm2cpio").unlink()
    extractor = commands / "dpkg-deb"
    extractor.write_text("""#!/bin/bash
test "$1" = -x
mkdir -p "$3/usr/bin"
printf '#!/bin/bash\\ntest "$#" = 1 && test -f "$1" && [[ "$1" == *.rpm ]]\\n' > "$3/usr/bin/rpm2cpio"
chmod 700 "$3/usr/bin/rpm2cpio"
""")
    extractor.chmod(0o700)
    outcome = _run(script, prefix, env=env)
    assert outcome.returncode == 0, outcome.stderr
    record = _receipt(prefix)
    assert record["bootstrap"] == "ubuntu-22.04-private-rpm2cpio"
    assert (Path(record["work_dir"]) / "tools/usr/bin/rpm2cpio").is_file()


def test_missing_cpio_refuses_without_system_installation(installation):
    script, prefix, env, commands = installation
    commands.joinpath("cpio").unlink()
    outcome = _run(script, prefix, env=env)
    assert outcome.returncode != 0
    assert "REQUIRED_TOOL_MISSING: cpio" in outcome.stderr
    assert not Path(env["TEST_UPSTREAM_ARGS"]).exists()


def test_other_linux_without_rpm2cpio_has_explicit_support_boundary(installation):
    script, prefix, env, commands = installation
    commands.joinpath("rpm2cpio").unlink()
    script.with_name("os-release").write_text('ID=fedora\nVERSION_ID="43"\n')
    outcome = _run(script, prefix, env=env)
    assert outcome.returncode == 2
    assert "RPM2CPIO_UNAVAILABLE" in outcome.stderr
    assert not Path(env["TEST_UPSTREAM_ARGS"]).exists()
    assert _receipt(prefix)["status"] == "FAILED"


def test_unsupported_architecture_does_not_create_prefix(installation):
    script, prefix, env, commands = installation
    commands.joinpath("uname").write_text('#!/bin/bash\ncase "$1" in -s) echo Linux;; -m) echo aarch64;; esac\n')
    outcome = _run(script, prefix, env=env)
    assert outcome.returncode == 2
    assert "UNSUPPORTED_HOST" in outcome.stderr
    assert not prefix.exists()
