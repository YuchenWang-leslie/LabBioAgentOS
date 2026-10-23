"""C14 step 9: the full LabBio workflow through each provider API format; run only on request.

Set LABBIO_PROVIDER_WORKFLOW_LIVE=1 and LABBIO_PROVIDER_WORKFLOW_DIR to a
directory that holds one complete runtime TOML per variant, named as in
test_provider_formats_live.py (openai-vendor.toml, anthropic.toml, gemini.toml,
...). Each configuration must list <dir>/data among its input_roots and pass
`labbio backend-check` first. A missing file skips that variant.

Each variant runs the same synthetic task on the same small CSV through the
real CLI, in this process, so the run can be observed: provider turns, retries,
usage and response sizes. A clarification question gets one fixed neutral
answer, at most three times. The run directory is then checked, and a record
without content, keys or private reasoning is written under
<dir>/results/<UTC time>/. The checks are safe to repeat; each run gets a new
directory. A completed workflow is not a scientific acceptance.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

import pytest

from labbioagentos import cli, local_config
from labbioagentos.local_config import ProviderCredentials, load_settings
from labbioagentos.trace import TraceEvent, TraceEventType
from provider_live_support import Observed, PrivateValues, Wire, _turn_record

pytestmark = pytest.mark.skipif(
    os.environ.get("LABBIO_PROVIDER_WORKFLOW_LIVE") != "1",
    reason="set LABBIO_PROVIDER_WORKFLOW_LIVE=1 and LABBIO_PROVIDER_WORKFLOW_DIR",
)

WORKFLOW_DIR = Path(os.environ.get("LABBIO_PROVIDER_WORKFLOW_DIR", ".")).expanduser()
STAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
RESULTS = WORKFLOW_DIR / "results" / STAMP
VARIANTS = ("openai-vendor", "openai-official", "openai-responses", "anthropic", "gemini",
            "gemini-interactions")
MAIN_STAGES = ("INTAKE", "UNDERSTAND", "PLAN", "PREFLIGHT", "EXECUTE", "VALIDATE",
               "INTERPRET", "REPORT", "LEARN")
TASK = (
    "The supplied CSV file has one row per sample: a sample_id column, a group column "
    "(control or treated) and expression values for eight genes. For each gene, compute the "
    "mean expression in each group and the log2 fold change of treated over control. Report "
    "the three genes with the largest absolute log2 fold change in a short report that "
    "includes a results table."
)
NEUTRAL_ANSWER = "Please continue with the task as stated; there are no further preferences."
GENES = ("GENE_A", "GENE_B", "GENE_C", "GENE_D", "GENE_E", "GENE_F", "GENE_G", "GENE_H")
BASE = (12.0, 40.0, 8.0, 150.0, 60.0, 22.0, 5.0, 90.0)
FOLD = (1.0, 2.0, 8.0, 1.1, 0.25, 1.0, 4.5, 0.5)
# Largest absolute log2 fold change: GENE_C (3.0), GENE_G (2.17), GENE_E (2.0).
EXPECTED_TOP_GENES = ("GENE_C", "GENE_G", "GENE_E")


def _dataset() -> str:
    """Deterministic table; small within-group variation, clear group effects."""
    rows = ["sample_id,group," + ",".join(GENES)]
    for group, prefix in (("control", "C"), ("treated", "T")):
        for sample in range(1, 7):
            values = []
            for index, base in enumerate(BASE):
                wobble = 1 + 0.04 * (((sample * 7 + index * 3) % 5) - 2)
                value = base * wobble * (FOLD[index] if group == "treated" else 1.0)
                values.append(f"{value:.2f}")
            rows.append(f"{prefix}{sample},{group}," + ",".join(values))
    return "\n".join(rows) + "\n"


def _events(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()
            if line.startswith("{")]


def _trace(directory: Path) -> list[TraceEvent]:
    path = directory / "run-trace.jsonl"
    if not path.is_file():
        return []
    return [TraceEvent.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _usage(turns) -> dict:
    totals = {"input": 0, "output": 0, "total": 0}
    unknown = Counter()
    for turn in turns:
        for name, value in zip(totals, (turn.usage.input_tokens, turn.usage.output_tokens,
                                        turn.usage.total_tokens)):
            if value is None:
                unknown[name] += 1
            else:
                totals[name] += value
    return {"known_totals": totals, "turns_with_unknown": dict(unknown)}


@pytest.mark.parametrize("variant", VARIANTS)
def test_full_workflow_through_the_provider_format(variant, monkeypatch, capsys):
    config = WORKFLOW_DIR / f"{variant}.toml"
    if not config.is_file():
        pytest.skip(f"{config.name} is absent")
    settings = load_settings(config)
    credentials = ProviderCredentials(settings.provider)
    credentials.load()
    data = WORKFLOW_DIR / "data" / "c14-synthetic-expression.csv"
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_text(_dataset(), encoding="utf-8")

    observed: list[Observed] = []
    real_build = local_config.LocalProviderBuilder.__call__

    def build(builder, model):
        provider = Observed(real_build(builder, model))
        observed.append(provider)
        return provider

    monkeypatch.setattr(local_config.LocalProviderBuilder, "__call__", build)
    wire = Wire(monkeypatch)
    private = PrivateValues(monkeypatch)
    conversation = f"c14-{variant}-{STAMP}"
    directory = settings.result_root / f"c14-{variant}-{STAMP}"
    started = monotonic()

    status = cli.main(["run", "--config", str(config), "--data", str(data), "--task", TASK,
                       "--output", str(directory), "--conversation", conversation])
    events = _events(capsys)
    run_id = next((item["run_id"] for item in events if item.get("event") == "started"), None)
    final = events[-1] if events else {}
    answered = 0
    while (status == 2 and run_id and answered < 3
           and (final.get("pending_clarification") or {}).get("question_id")):
        answered += 1
        status = cli.main(["answer", "--config", str(config), "--conversation", conversation,
                           "--run-id", run_id,
                           "--question-id", final["pending_clarification"]["question_id"],
                           "--text", NEUTRAL_ANSWER])
        events = _events(capsys)
        final = events[-1] if events else {}

    turns = [turn for provider in observed for turn in provider.turns]
    trace = _trace(directory)
    kinds = Counter(event.event_type for event in trace)
    observed_stages = sorted({event.stage_id.value for event in trace
                              if event.event_type is TraceEventType.PROVIDER_TURN_OBSERVED
                              and event.stage_id is not None})
    runtime = (json.loads((directory / "RUNTIME.json").read_text(encoding="utf-8"))
               if (directory / "RUNTIME.json").is_file() else {})
    key = credentials.api_key().encode("utf-8")
    leaks = {"key_files": [], "private_files": []}
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        content = path.read_bytes()
        if key in content:
            leaks["key_files"].append(str(path.relative_to(directory)))
        if private.found_in(content):
            leaks["private_files"].append(str(path.relative_to(directory)))
    report_text = ""
    delivery = final.get("delivery") or {}
    delivery_dir = directory / "delivery"
    if delivery_dir.is_dir():
        report_text = "".join(path.read_text(encoding="utf-8", errors="replace")
                              for path in delivery_dir.rglob("*")
                              if path.is_file() and path.suffix in {".md", ".txt", ".html"})
    sizes = wire.response_bytes
    per_token = [round(size / turn.usage.output_tokens, 1)
                 for size, turn in zip(sizes, turns) if turn.usage.output_tokens
                 ] if len(sizes) == len(turns) else []
    record = {
        "variant": variant,
        "api_format": settings.provider.api_format,
        "model": settings.provider.model_identifier,
        "endpoint_origin": runtime.get("provider_endpoint", {}).get("origin"),
        "execution_backend": settings.execution.backend,
        "exit_status": status,
        "final_status": final.get("status"),
        "final_event": final.get("event") or final.get("error"),
        "clarifications_answered": answered,
        "elapsed_seconds": round(monotonic() - started, 1),
        "run_directory": directory.name,
        "provider_turns": len(turns),
        "finish_reasons": dict(Counter(turn.finish_reason.value for turn in turns)),
        "tool_calls": sum(len(turn.tool_calls) for turn in turns),
        "turns_with_private_reasoning": sum(1 for turn in turns
                                            if turn.private_reasoning_present),
        "retries": dict(Counter(item.category for turn in turns
                                for item in turn.transport_retries)),
        "usage": _usage(turns),
        "max_response_bytes": max(sizes) if sizes else None,
        "max_bytes_per_output_token": max(per_token) if per_token else None,
        "stages_with_provider_turns": observed_stages,
        "trace_counts": {kind.value: kinds[kind] for kind in (
            TraceEventType.EXECUTION_STARTED, TraceEventType.EXECUTION_COMPLETED,
            TraceEventType.EXECUTION_FAILED, TraceEventType.PROVIDER_RETRY_OBSERVED,
            TraceEventType.STAGE_COMPLETED, TraceEventType.RUN_COMPLETED,
        )},
        "delivery_exported": bool(delivery) and delivery_dir.is_dir(),
        "report_names_expected_genes": [gene for gene in EXPECTED_TOP_GENES
                                        if gene in report_text],
        "runtime_identity": {
            "api_format": runtime.get("configuration", {}).get("provider", {}).get("api_format"),
            "raw_endpoint_absent": "endpoint" not in runtime.get("configuration", {})
            .get("provider", {}),
        },
        "private_values_seen": len(private.values),
        "leaks": leaks,
        "last_turn": _turn_record(turns[-1]) if turns else None,
    }
    RESULTS.mkdir(parents=True, exist_ok=True, mode=0o700)
    (RESULTS / f"{variant}__workflow.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )

    assert record["final_status"] == "COMPLETED", (record["final_event"], record["final_status"])
    assert record["runtime_identity"] == {"api_format": settings.provider.api_format,
                                          "raw_endpoint_absent": True}
    assert record["endpoint_origin"] == local_config._provider_endpoint(
        settings.provider.endpoint)["origin"]
    assert set(MAIN_STAGES) <= set(observed_stages)
    assert record["trace_counts"]["EXECUTION_COMPLETED"] >= 1
    assert record["delivery_exported"]
    assert leaks == {"key_files": [], "private_files": []}
