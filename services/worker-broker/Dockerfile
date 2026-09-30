# syntax=docker/dockerfile:1.4
FROM python:3.12.13-slim
WORKDIR /app

# Install the locked service dependencies before any source: a shared/ or src/ edit
# reuses this layer. The service's own code runs from PYTHONPATH=/app.
COPY services/worker-broker/requirements.lock /tmp/requirements.lock
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /tmp/requirements.lock

COPY shared ./shared
COPY services/worker-broker/src ./src
ENV PYTHONPATH=/app
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8001"]

ARG SOURCE_HASH=unknown
LABEL org.codegen.worker_source_hash=$SOURCE_HASH
