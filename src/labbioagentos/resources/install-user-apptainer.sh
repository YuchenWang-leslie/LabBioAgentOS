#!/bin/bash
# Explicit user installation, never called implicitly by an Agent or launcher.
set -euo pipefail
umask 077

if [[ $# != 1 || "$1" != /* || "$1" == / || "$1" =~ [[:space:][:cntrl:]] || "/$1/" == */../* ]]; then
    printf 'PREFIX_INVALID: provide one new absolute directory without whitespace, roots or aliases.\n' >&2
    exit 2
fi
prefix=$(realpath -ms -- "$1")
probe=$prefix
while [[ "$probe" != / ]]; do
    if [[ -L "$probe" ]]; then
        printf 'PREFIX_SYMLINK: installation paths must not traverse symbolic links.\n' >&2
        exit 2
    fi
    probe=${probe%/*}
    [[ -n "$probe" ]] || probe=/
done
if [[ -e "$prefix" ]]; then
    printf 'PREFIX_EXISTS: existing directories and files are never overwritten.\n' >&2
    exit 2
fi
if [[ $EUID == 0 || $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
    printf 'UNSUPPORTED_HOST: run as an ordinary user on Linux x86_64.\n' >&2
    exit 2
fi
for tool in curl cpio python3 sha256sum mktemp find grep cp env; do
    if ! command -v "$tool" >/dev/null; then
        printf 'REQUIRED_TOOL_MISSING: %s\n' "$tool" >&2
        exit 2
    fi
done
parent=${prefix%/*}
[[ -n "$parent" ]] || parent=/
if ! mkdir -p -- "$parent"; then
    printf 'PREFIX_PARENT_UNAVAILABLE: could not create the private installation parent.\n' >&2
    exit 2
fi
work=$(mktemp -d "$parent/.labbio-apptainer-install.XXXXXXXX")
# Reserve atomically; the upstream installer accepts an existing empty prefix.
mkdir -- "$prefix"
status=FAILED
bootstrap=none
installer_sha=a097956eafa6ab3dd843ee429d0b774ed029753bfadf013b8046556228fac6f2
runtime_sha=e821c129c8ee49b3994816db3b763d1c6cfc69f3ed043af187e07d10dfe6d2c0
finish() {
    exit_code=$?
    python3 - "$work" "$prefix" "$status" "$bootstrap" "$installer_sha" "$runtime_sha" <<'PY'
import json
from pathlib import Path
import sys
work, prefix, status, bootstrap, installer, runtime = sys.argv[1:]
record = dict(status=status, requested_version="1.5.5",
              version="1.5.5-1" if status == "INSTALLED" else None, prefix=prefix, work_dir=work,
              installer_sha256=installer, runtime_rpm_sha256=runtime, bootstrap=bootstrap,
              dependency_resolution="official-installer-live-repositories",
              setuid=False if status == "INSTALLED" else None)
with (Path(work) / "installation-receipt.json").open("x") as stream:
    json.dump(record, stream, indent=2, sort_keys=True)
    stream.write("\n")
PY
    if [[ "$status" == INSTALLED ]]; then
        cp -- "$work/installation-receipt.json" "$prefix/installation-receipt.json"
        printf 'Installed: %s/bin/apptainer\nReceipt: %s/installation-receipt.json\n' "$prefix" "$prefix"
    else
        printf 'Preserved installation diagnostics: %s\nPartial installation: %s\n' "$work" "$prefix" >&2
    fi
    exit "$exit_code"
}
trap finish EXIT

download() {
    local url=$1 expected=$2 target=$3
    curl --proto '=https' --tlsv1.2 --fail --location --retry 2 \
        --connect-timeout 20 --max-time 600 --output "$target" "$url" >>"$work/install.log" 2>&1
    if ! printf '%s  %s\n' "$expected" "$target" | sha256sum --check --status; then
        printf 'DOWNLOAD_CHECKSUM_MISMATCH\n' >&2
        return 1
    fi
    sha256sum -- "$target" >>"$work/verified-downloads.sha256"
}

download https://raw.githubusercontent.com/apptainer/apptainer/a9325172651120164949df072e37b3238a7c00de/tools/install-unprivileged.sh \
    "$installer_sha" "$work/install-unprivileged.sh"
download https://github.com/apptainer/apptainer/releases/download/v1.5.5/apptainer-1.5.5-1.x86_64.rpm \
    "$runtime_sha" "$work/apptainer-1.5.5-1.x86_64.rpm"

tool_path=$PATH
tool_lib=${LD_LIBRARY_PATH:-}
if ! command -v rpm2cpio >/dev/null; then
    OS_RELEASE=/etc/os-release
    if ! grep -qx 'ID=ubuntu' "$OS_RELEASE" || ! grep -Eq '^VERSION_ID="?22\.04"?$' "$OS_RELEASE" \
            || ! command -v dpkg-deb >/dev/null; then
        printf 'RPM2CPIO_UNAVAILABLE: private bootstrap supports Ubuntu 22.04 with dpkg-deb only.\n' >&2
        exit 2
    fi
    # These packages are extracted privately, not installed into the host package database.
    base=https://archive.ubuntu.com/ubuntu/pool/universe/r/rpm
    for item in \
        rpm2cpio:3221e62595718384418b22ad6566205c3023611b78b03168dc774920818a024c \
        librpm9:93fefb127f13cc8fb0e1864786a41c2cc1a57b331063b5a82a9b8aea446031de \
        librpmio9:88a8f3f992098f7e88bf85c033c25f1a5d33518e6cc5133ee7561f424f9ee7b7; do
        package=${item%%:*}_4.17.0+dfsg1-4build1_amd64.deb
        download "$base/$package" "${item#*:}" "$work/$package"
        dpkg-deb -x "$work/$package" "$work/tools" >>"$work/install.log" 2>&1
    done
    tool_path="$work/tools/usr/bin:$PATH"
    tool_lib="$work/tools/usr/lib/x86_64-linux-gnu${tool_lib:+:$tool_lib}"
    env LD_LIBRARY_PATH="$tool_lib" "$work/tools/usr/bin/rpm2cpio" \
        "$work/apptainer-1.5.5-1.x86_64.rpm" >/dev/null 2>>"$work/install.log"
    bootstrap=ubuntu-22.04-private-rpm2cpio
fi
printf 'Installing into: %s\nDiagnostics: %s\n' "$prefix" "$work"
env PATH="$tool_path" LD_LIBRARY_PATH="$tool_lib" /bin/bash "$work/install-unprivileged.sh" \
    -d el8 -v "$work/apptainer-1.5.5-1.x86_64.rpm" "$prefix" >>"$work/install.log" 2>&1

binary=$(realpath -- "$prefix/bin/apptainer")
if [[ "$binary" != "$prefix/"* || ! -x "$binary" \
        || $("$prefix/bin/apptainer" version) != 1.5.5-1 ]]; then
    printf 'INSTALLED_VERSION_INVALID\n' >&2
    exit 1
fi
"$prefix/bin/apptainer" buildcfg >"$work/buildcfg.txt"
if ! grep -qx 'APPTAINER_SUID_INSTALL=0' "$work/buildcfg.txt" \
        || [[ -n $(find "$prefix" -type f -perm /6000 -print -quit) ]]; then
    printf 'UNPRIVILEGED_INSTALL_VERIFICATION_FAILED\n' >&2
    exit 1
fi
status=INSTALLED
