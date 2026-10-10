# The agent's workroom: what the three repos need to run their gates, and nothing of
# this host. omp is bind-mounted read-only at run time (it is a host binary, updated
# with the host), and model access comes through harness/gateway_relay.py.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git poppler-utils ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Union of the three repos' requirements.txt plus the lint gate. The optional search
# stacks (torch, Milvus) are left out, as in CI: their tests skip without them.
RUN pip install --no-cache-dir "requests>=2.31" "defusedxml>=0.7" "yfinance>=1.5" \
    "pandas>=2.0" "ruff==0.17.0"

ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
