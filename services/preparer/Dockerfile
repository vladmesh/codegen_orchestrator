# Preparer Container
# Lightweight container for preparing project structure via copier.
# Does NOT include Factory.ai - just git, copier, and prepare script.

FROM python:3.12-slim

# Install git (required for copier and git operations)
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# Install copier
RUN pip install --no-cache-dir copier==9.4.1

# Copy prepare script
COPY prepare.py /prepare.py

# Set git config (required for commits)
RUN git config --global user.email "orchestrator@codegen.local" && \
    git config --global user.name "Codegen Orchestrator"

ENTRYPOINT ["python", "/prepare.py"]
