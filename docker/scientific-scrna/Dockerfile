ARG BASE_IMAGE=python:3.11-slim@sha256:1042b61448fef4ba92d16a8c7eb4996d027568ce64792a7877fd88511e0af7c6
FROM ${BASE_IMAGE}

ENV MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    NUMBA_CACHE_DIR=/tmp/numba

COPY requirements.lock /tmp/scientific-scrna-requirements.lock
RUN python -m pip install --no-cache-dir --only-binary=:all: \
      --requirement /tmp/scientific-scrna-requirements.lock \
    && rm /tmp/scientific-scrna-requirements.lock \
    && python -m pip check \
    && python -c "import anndata, h5py, numpy, pandas, scipy, scanpy, matplotlib, seaborn, sklearn, statsmodels, igraph, leidenalg, numba"
