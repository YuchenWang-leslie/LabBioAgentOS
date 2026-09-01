"""Prepare the public C7 PBMC3k RAW AnnData fixture outside Git.

This is development/test data acquisition, not production runtime analysis.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import anndata as ad
import pandas as pd
from anndata.utils import make_index_unique
from scipy.io import mmread


SOURCE_SHA256 = "847d6ebd9a1ec9a768f2be7e40ca42cbfe75ebeb6d76a4c24167041699dc28b5"
SOURCE_URL = (
    "https://cf.10xgenomics.com/samples/cell-exp/1.1.0/pbmc3k/"
    "pbmc3k_filtered_gene_bc_matrices.tar.gz"
)
DOCUMENTATION_URL = (
    "https://scanpy.readthedocs.io/en/stable/api/scanpy.datasets.pbmc3k.html"
)
MATRIX_MEMBER = "filtered_gene_bc_matrices/hg19/matrix.mtx"
GENES_MEMBER = "filtered_gene_bc_matrices/hg19/genes.tsv"
BARCODES_MEMBER = "filtered_gene_bc_matrices/hg19/barcodes.tsv"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _member_bytes(archive: tarfile.TarFile, name: str) -> bytes:
    member = archive.getmember(name)
    if not member.isfile():
        raise ValueError(f"Expected regular archive member: {name}")
    handle = archive.extractfile(member)
    if handle is None:
        raise ValueError(f"Could not read archive member: {name}")
    return handle.read()


def prepare(source: Path, output: Path, provenance: Path) -> None:
    source = source.resolve(strict=True)
    if _sha256(source) != SOURCE_SHA256:
        raise ValueError("PBMC3k source checksum does not match the authoritative value")

    with tarfile.open(source, "r:gz") as archive:
        matrix = mmread(io.BytesIO(_member_bytes(archive, MATRIX_MEMBER))).tocsr().T
        genes = tuple(
            csv.reader(
                io.StringIO(_member_bytes(archive, GENES_MEMBER).decode("utf-8")),
                delimiter="\t",
            )
        )
        barcodes = tuple(
            row[0]
            for row in csv.reader(
                io.StringIO(_member_bytes(archive, BARCODES_MEMBER).decode("utf-8")),
                delimiter="\t",
            )
        )

    if len(genes) != matrix.shape[1] or len(barcodes) != matrix.shape[0]:
        raise ValueError("PBMC3k archive axes do not match its matrix")
    data = ad.AnnData(
        X=matrix,
        obs=pd.DataFrame(index=pd.Index(barcodes, name=None)),
        var=pd.DataFrame(
            {"gene_ids": tuple(row[0] for row in genes)},
            index=make_index_unique(
                pd.Index((row[1] for row in genes), name=None)
            ),
        ),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    data.write_h5ad(output, compression="gzip")

    record = {
        "dataset_name": "PBMC3k",
        "source_organization": "10x Genomics",
        "source_url": SOURCE_URL,
        "documentation_url": DOCUMENTATION_URL,
        "acquisition_mechanism": "Direct canonical 10x archive download documented by Scanpy",
        "original_download_sha256": SOURCE_SHA256,
        "generated_h5ad_sha256": _sha256(output),
        "acquired_at_utc": datetime.now(timezone.utc).isoformat(),
        "expected_dimensions": [2700, 32738],
        "actual_dimensions": [int(data.n_obs), int(data.n_vars)],
        "transformation": (
            "Read 10x Matrix Market, genes.tsv, and barcodes.tsv; transpose to "
            "observations by variables; preserve sparse RAW count values; use gene "
            "symbols as unique var index and gene IDs as var metadata; write gzip H5AD"
        ),
    }
    provenance.parent.mkdir(parents=True, exist_ok=True)
    provenance.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("provenance", type=Path)
    args = parser.parse_args()
    prepare(args.source, args.output, args.provenance)


if __name__ == "__main__":
    main()
