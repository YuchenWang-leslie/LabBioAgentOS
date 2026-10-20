"""Authorized engineering workload runner; no analysis program or method presets.

Each invocation creates one fresh run using the ordinary CLI submission path.
The six tasks are evaluation inputs, never runtime routing or Agent instructions
about how to perform an analysis. Run at most two processes concurrently.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time


DATA_ROOT = Path("/media/desk16/iy1982/WYC/projects/test/TEST1/projects")
H5AD = DATA_ROOT / "ADVANCED_H5AD/data/pbmc68k_reduced_annotated.h5ad"
GOLD = DATA_ROOT / "GOLDDEMO/data"
BASE_IMAGE = "sha256:89f2385fb9a86c72bbe8f28ec4643becf8d356ad61b9eb94bdc1c3f4ab7845cb"
COLD_IMAGE = "sha256:fe316ce25958c9a5fd10d55a42d2597a2736a1c84f92690cf79cd8a0ada67506"
TASKS = {
    "autonomous": "请基于这份已注释的单细胞数据，自主提出一个适合该数据的科学问题，自行选择方法并完成实际分析。检查结果可信度，交付中文报告及可复查产物。",
    "annotation": "请重点检查这份单细胞数据已有注释的可信度和不确定性，自主选择合适的方法完成实际分析，并说明哪些结论不能成立。交付中文报告和可复查产物。",
    "cold": "请在提供的精简 Python 环境中，基于这份已注释单细胞数据自主提出合适问题，选择并配置需要的科学库，完成实际分析、结果检查和中文报告。依赖和程序需要调整时请依据实际错误自行修正。",
    "multifile": "请检查随附多文件表达数据及事实说明，自主提出该数据能够支持的科学问题并完成实际分析，交付中文报告及可复查产物。不要把细胞当成独立患者；表达尺度尚未独立确认。",
    "long_results": "请基于随附表达数据与事实说明自主完成有价值且有证据边界的分析。保留完整可复查结果文件，在中文报告中总结实际支持的发现、结果范围和限制。",
    "recovery": "请基于这份已注释单细胞数据自主提出合适问题，选择方法完成实际分析，检查结果并交付中文报告和可复查产物。",
}
PREFERENCE = (
    "科学问题、方法、参数、库和程序由你自主选择。仅使用提供的数据；"
    "不需要先询问我选择哪种方法。保留未知与数据限制，不虚构结果或证据。"
)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write_new(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--arm", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--case", choices=tuple(TASKS), required=True)
    parser.add_argument("--replicate", type=int, default=1)
    args = parser.parse_args()
    source, root = args.source.resolve(strict=True), args.root.resolve()
    sys.path.insert(0, str(source / "src"))
    from labbioagentos import cli
    from labbioagentos.local_config import LocalSettings, load_settings

    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = f"{args.arm}-{args.case}-{args.replicate}"
    receipt = root / "receipts" / name
    receipt.mkdir(parents=True, mode=0o700)  # Refuse an existing attempt.
    settings = load_settings(args.config)
    config = settings.model_dump(mode="json")
    config.update(
        result_root=str(root / "runs"),
        input_roots=[str(H5AD.parent), str(GOLD)],
        default_format="raw",
        managed_root=None,
        gold_root=None,
        profile=None,
        identity={"user_id": "TEST1", "project_id": "NATIVE_STRESS", "lab_id": "local-lab"},
    )
    config["provider"].update(
        model_identifier="mimo-v2.6-pro", trust_env=False,
        max_output_tokens=16384, max_model_phase_seconds=900,
    )
    large = args.case in {"multifile", "long_results"}
    config["execution"].update(
        backend="docker", apptainer=None, image_key="scientific-python",
        image_reference=COLD_IMAGE if args.case == "cold" else BASE_IMAGE,
        available_python_modules=[] if args.case == "cold" else [
            "anndata", "h5py", "numpy", "pandas", "scipy",
        ],
        installed_packages={},
        include_error_context=True, allow_generated_file_read=True,
        resources=dict(cpus=4.0, memory_mb=16384 if large else 4096,
                       pids_limit=128, timeout_seconds=1800.0),
    )
    config["environment"].update(root=str(root / "environments" / name))
    settings = LocalSettings.model_validate(config)
    paths = [*sorted(GOLD.glob("GSM*.csv")), GOLD / "DATA_CONTEXT.md",
             GOLD / "SAMPLE_METADATA.tsv"] if large else [H5AD]
    task = TASKS[args.case]
    if large:
        task += ("\n已知输入事实：四个文件来自两个患者的肾癌肿瘤/配对癌旁组织，"
                 "不是骨转移数据。文件的 Gene_ID、Symbol 两列为基因元数据；"
                 "表达值为已发布的 count-like 矩阵，不能仅凭整数断言是原始 UMI。")
    write_new(receipt / "CONFIG.json", settings.model_dump(mode="json"))
    write_new(receipt / "MANIFEST.json", {
        "source_commit": subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip(),
        "source": str(source), "runner_sha256": digest(Path(__file__)),
        "arm": args.arm, "case": args.case, "replicate": args.replicate,
        "inputs": [{"path": str(p), "sha256": digest(p)} for p in paths],
        "task": task, "preference": PREFERENCE,
    })
    if args.case == "recovery":
        build = cli.build_application

        def interrupted_build(*positional, **keywords):
            application = build(*positional, **keywords)
            original = application.configuration.boundary_observer

            def observe(kind, value):
                if original is not None:
                    original(kind, value)
                if kind == "capability_evidence" and value.stage_id.value == "EXECUTE":
                    write_new(receipt / "INJECTED_STOP.json", {
                        "run_id": str(value.run_id), "stage": value.stage_id.value,
                        "evidence_id": str(value.evidence_id),
                        "boundary": "AFTER_DURABLE_CAPABILITY_EVIDENCE",
                    })
                    raise SystemExit(75)

            application.configuration = replace(
                application.configuration, boundary_observer=observe)
            return application

        cli.build_application = interrupted_build
    request = argparse.Namespace(
        task=task, preference=[PREFERENCE], data=paths,
        format="raw" if large else "h5ad", output=root / "runs" / name,
        conversation=name,
    )
    started = time.monotonic()
    try:
        code = asyncio.run(cli._run(request, settings))
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:
        # Do not persist arbitrary provider/credential-bearing exception text.
        code = 1
        write_new(receipt / "ERROR.json", {"exception_type": type(exc).__name__})
    write_new(receipt / "PROCESS_RESULT.json", {
        "exit_code": code, "elapsed_seconds": round(time.monotonic() - started, 3),
    })
    return code


if __name__ == "__main__":
    raise SystemExit(main())
