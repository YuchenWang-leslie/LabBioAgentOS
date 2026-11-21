"""The optional workload runner never assumes a workstation data directory."""

from pathlib import Path
import subprocess
import sys


RUNNER = Path(__file__).parents[1] / "scripts" / "runtime_stress.py"


def test_workload_help_declares_explicit_data_root():
    result = subprocess.run([sys.executable, str(RUNNER), "--help"],
        capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert "--data-root" in result.stdout


def test_missing_workload_data_root_is_rejected_before_creating_outputs(tmp_path):
    result = subprocess.run([sys.executable, str(RUNNER),
        "--source", str(tmp_path), "--root", str(tmp_path / "outputs"),
        "--config", str(tmp_path / "absent.toml"),
        "--arm", "baseline", "--case", "autonomous"],
        capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "--data-root" in result.stderr
    assert not (tmp_path / "outputs").exists()
