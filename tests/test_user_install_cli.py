"""Runtime installation precedes application configuration and never loads a model."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from labbioagentos import cli


@pytest.mark.parametrize("returncode", [0, 2, 28])
def test_explicit_runtime_install_needs_no_existing_config_or_provider(tmp_path, monkeypatch, returncode):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PROVIDER_SECRET", "must-not-be-inherited")
    monkeypatch.setenv("BASH_ENV", "must-not-be-sourced")
    monkeypatch.setattr(cli, "load_settings", lambda *_: pytest.fail("Runtime install loaded application settings"))
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[0] == "/bin/bash"
        assert Path(argv[1]).is_file()
        assert argv[2] == str(tmp_path / ".local/share/labbioagent/apptainer/1.5.5")
        assert kwargs["check"] is False
        assert "PROVIDER_SECRET" not in kwargs["env"]
        assert "BASH_ENV" not in kwargs["env"]
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli.main(["apptainer-install"]) == returncode
    assert len(calls) == 1
    assert not (tmp_path / ".local").exists()
