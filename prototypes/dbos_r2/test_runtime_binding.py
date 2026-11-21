"""Changes in actual contracts/configuration cannot reuse a declared version."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

import runtime_binding
from runtime_binding import probe_settings, runtime_identity


def test_binding_is_deterministic_and_does_not_load_credentials(tmp_path):
    settings = probe_settings(tmp_path)
    first, second = runtime_identity(settings), runtime_identity(settings)
    assert first == second
    assert not settings.provider.env_file.exists()
    assert first["execution_environment"]["kind"] == "FAKE_PROVIDER_AND_TOOL_NO_CONTAINER"
    assert set(first["native_manifest"]["source"]) >= {"labbio_sha256", "native_kernel_sha256"}


@pytest.mark.parametrize("field", ["provider", "model", "budget", "permission", "schema", "source", "prototype", "dependency"])
def test_full_binding_changes_for_each_contract_dimension(tmp_path, monkeypatch, field):
    settings = probe_settings(tmp_path)
    first = runtime_identity(settings)
    raw = settings.model_dump(mode="json", by_alias=True)
    if field in {"provider", "model", "budget"}:
        key, value = {"provider": ("endpoint", "https://different.invalid/v1"),
                      "model": ("model_identifier", "different"),
                      "budget": ("max_output_tokens", 8192)}[field]
        raw["provider"][key] = value
    elif field == "permission":
        raw["execution"]["allow_generated_file_read"] = True
    elif field == "schema":
        original = runtime_binding.ModelTurn.model_json_schema
        monkeypatch.setattr(runtime_binding.ModelTurn, "model_json_schema", lambda **kw: {
            **original(**kw), "description": "changed-contract",
        })
    elif field == "source":
        original = runtime_binding.runtime_manifest

        def changed(selected):
            manifest = copy.deepcopy(original(selected))
            manifest["source"]["labbio_sha256"] = "changed"
            return manifest

        monkeypatch.setattr(runtime_binding, "runtime_manifest", changed)
    elif field == "prototype":
        monkeypatch.setattr(runtime_binding, "_source_digest", lambda _: "changed")
    elif field == "dependency":
        original = runtime_binding.importlib.metadata.version
        monkeypatch.setattr(runtime_binding.importlib.metadata, "version",
                            lambda name: "changed" if name == "dbos" else original(name))
    changed_settings = type(settings).model_validate(raw)
    assert runtime_identity(changed_settings) != first


def test_environment_hashes_are_derived_from_files_not_claimed_by_caller(tmp_path):
    sif = tmp_path / "synthetic-sif-bytes"
    prefix = tmp_path / "installation"
    executable = prefix / "bin/apptainer"
    executable.parent.mkdir(parents=True)
    (prefix / "installation-receipt.json").write_text(json.dumps({
        "prefix": str(prefix), "status": "INSTALLED", "setuid": False,
    }))
    sif.write_bytes(b"identity-fixture-not-an-executable-SIF")
    executable.write_bytes(b"identity-fixture-not-an-executable")
    settings = probe_settings(tmp_path)
    raw = settings.model_dump(mode="json", by_alias=True)
    raw["execution"]["image_reference"] = "sha256:" + hashlib.sha256(sif.read_bytes()).hexdigest()
    settings = type(settings).model_validate(raw)
    first = runtime_identity(settings, sif_path=sif, executable_path=executable)
    assert first["execution_environment"]["kind"] == "HASH_BOUND_NOT_PREFLIGHT"
    (prefix / "actual-runtime-behind-wrapper").write_bytes(b"changed-helper")
    assert runtime_identity(settings, sif_path=sif, executable_path=executable) != first
    executable.write_bytes(b"changed-executable")
    assert runtime_identity(settings, sif_path=sif, executable_path=executable) != first
    sif.write_bytes(b"changed-sif")
    with pytest.raises(ValueError, match="SIF identity differs"):
        runtime_identity(settings, sif_path=sif, executable_path=executable)


def test_profile_bytes_change_binding_at_same_path(tmp_path):
    settings = probe_settings(tmp_path)
    profile = tmp_path / "profile.json"
    from labbioagentos import local_config
    profile.write_bytes((Path(local_config.__file__).parent /
                         "resources/local-default.json").read_bytes())
    raw = settings.model_dump(mode="json", by_alias=True)
    raw["profile"] = str(profile)
    selected = type(settings).model_validate(raw)
    first = runtime_identity(selected)
    value = json.loads(profile.read_text())
    value["capability_common"] += "\nChanged external guidance."
    profile.write_text(json.dumps(value))
    assert runtime_identity(selected) != first
