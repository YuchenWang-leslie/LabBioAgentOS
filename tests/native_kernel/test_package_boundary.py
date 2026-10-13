"""Static P1 boundary checks for the production Native Kernel package."""

from pathlib import Path

from labbioagentos.runtime.registry import StageRuntimeInvoker


KERNEL_ROOT = Path(__file__).parents[2] / "src" / "labbioagentos" / "kernel"


def test_native_kernel_has_no_domain_coupling():
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(KERNEL_ROOT.glob("*.py"))
    ).lower()

    for forbidden in (
        "single-cell",
        "scrna",
        "seurat",
        "cellchat",
        "gsea",
        "fastq",
        "bam",
        "h5ad",
    ):
        assert forbidden not in source


def test_stage_runtime_invoker_remains_the_outer_port():
    assert "invoke" in StageRuntimeInvoker.__dict__
