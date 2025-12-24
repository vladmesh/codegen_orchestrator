FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Copy and install service dependencies
COPY services/api/pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy source
COPY services/api/src ./src
COPY shared ./shared
COPY alembic ./alembic
COPY alembic.ini ./

# Expose port
EXPOSE 8000

# Run
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
