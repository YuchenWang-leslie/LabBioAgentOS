# Scientific scRNA execution image

This image is generic execution infrastructure. It provides Python 3.11 and
AnnData-compatible numerical libraries, but contains no analysis script,
workflow, threshold, dataset identifier, or runtime fallback.

Build it during trusted image preparation, then record the resulting local
image ID (`docker image inspect --format '{{.Id}}' ...`). LabBio runtime
configuration uses that immutable `sha256:...` ID directly and keeps runtime
network access disabled.

Scanpy is intentionally absent: the C7 bounded QC task needs AnnData, NumPy,
SciPy, pandas, and h5py, while method and code selection remain with the runtime
ExecutionAgent.
