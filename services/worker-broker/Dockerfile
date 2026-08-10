FROM python:3.12.13-slim
WORKDIR /app
COPY shared ./shared
COPY services/worker-broker/pyproject.toml .
RUN pip install .
COPY services/worker-broker/src ./src
ENV PYTHONPATH=/app
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8001"]

ARG SOURCE_HASH=unknown
LABEL org.codegen.worker_source_hash=$SOURCE_HASH
