"""CLI waiting/answer persistence uses the same conversation and writer boundary."""

import json

import pytest

from labbioagentos import LabBioApplication, NextActionProposal, WorkflowStage, cli
from labbioagentos.local_conversations import read_run_record
from labbioagentos.runtime import CapabilityEvidenceBundle
from labbioagentos.runtime.pantheon import PantheonCapabilityStageInvoker, PantheonTypedStageInvoker, PantheonRuntimeFactory
from test_local_cli import local_cli, _REAL_APPLICATION_RUN
from test_c10_durable_control_plane import _next_result
from test_clarification_continuation import _question


@pytest.fixture
def waiting_cli(local_cli, monkeypatch, capsys):
    settings, source, directory, _ = local_cli
    observed = {"provider": 0, "capability": 0, "finalize": 0}

    async def capability(self, stage_input):
        observed["capability"] += 1
        assert stage_input.stage_id is WorkflowStage.INTAKE
        await self.evidence_sources[0].artifact_list()
        return CapabilityEvidenceBundle(run_id=stage_input.run_id, stage_id=stage_input.stage_id,
            invocation_id=stage_input.invocation_id, items=self.evidence_sources[0].evidence_items())

    async def finalize(self, stage_input, *, capability_evidence=None):
        observed["finalize"] += 1
        assert not stage_input.workflow_control.request_user_input_available
        assert stage_input.workflow_control.clarification_available
        result = _next_result(stage_input.stage_id)
        if not stage_input.clarifications:
            return result.model_copy(update={"next_action": _question()})
        assert stage_input.clarifications[0].answer_text == "面向初次阅读的人。"
        # A terminal fixture decision avoids pretending this test did analysis.
        return result.model_copy(update={"next_action": NextActionProposal(action="fail", reason="CLI fixture endpoint")})

    def load_provider(_):
        observed["provider"] += 1

    monkeypatch.setattr(LabBioApplication, "run", _REAL_APPLICATION_RUN)
    monkeypatch.setattr(PantheonCapabilityStageInvoker, "invoke", capability)
    monkeypatch.setattr(PantheonTypedStageInvoker, "invoke", finalize)
    monkeypatch.setattr(PantheonRuntimeFactory, "_configure_transport", staticmethod(lambda model: model.model_identifier))
    monkeypatch.setattr("labbioagentos.local_config._load_provider", load_provider)
    assert cli.main(["run", "--conversation", "dialogue", "--data", str(source),
                     "--task", "Synthetic software task", "--output", str(directory)]) == 2
    waiting = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert waiting["status"] == "WAITING_FOR_USER"
    base = ["--conversation", "dialogue", "--run-id", waiting["run_id"]]
    answer = [*base, "--question-id", waiting["pending_clarification"]["question_id"],
              "--text", "面向初次阅读的人。"]
    return directory, base, answer, observed


def test_question_wait_continue_and_save_only_do_not_load_model(waiting_cli, capsys):
    directory, base, answer, seen = waiting_cli
    before = dict(seen)
    assert cli.main(["question", *base]) == 0
    assert json.loads(capsys.readouterr().out)["pending_clarification"]["question"]["issue_key"] == "audience"
    assert cli.main(["continue", *base]) == 2
    assert "pending_clarification" in capsys.readouterr().out
    assert seen == before
    assert cli.main(["answer", *answer, "--save-only"]) == 0
    assert "answer_saved" in capsys.readouterr().out
    assert seen == before
    saved = read_run_record(directory)
    assert saved.workflow_run.clarifications[0].status == "ANSWERED"
    assert cli.main(["answer", *answer]) == 0
    assert "answer_already_saved" in capsys.readouterr().out
    assert read_run_record(directory).record_version == saved.record_version
    assert seen == before
    assert cli.main(["continue", *base]) == 2
    capsys.readouterr()
    assert seen["provider"] == 1
    assert seen["capability"] == 1 and seen["finalize"] == 2


def test_answer_default_resumes_once_without_replaying_tools(waiting_cli, capsys):
    directory, _, answer, seen = waiting_cli
    assert cli.main(["answer", *answer]) == 2
    output = capsys.readouterr().out
    assert "answer_saved" in output and '"event": "continued"' in output
    assert seen == {"provider": 1, "capability": 1, "finalize": 2}
    before = read_run_record(directory).model_dump_json()
    assert cli.main(["answer", *answer]) == 0
    capsys.readouterr()
    assert read_run_record(directory).model_dump_json() == before
    assert seen["provider"] == 1


def test_active_writer_prevents_answer_submission(waiting_cli, capsys):
    from labbioagentos.local_continuation import run_writer
    directory, _, answer, seen = waiting_cli
    before = read_run_record(directory).model_dump_json()
    with run_writer(directory):
        assert cli.main(["answer", *answer]) == 2
    assert "RUN_WRITER_ACTIVE" in capsys.readouterr().out
    assert read_run_record(directory).model_dump_json() == before
    assert seen["provider"] == 0
