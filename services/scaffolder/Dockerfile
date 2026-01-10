FROM python:3.12-slim

# Install git for cloning repos
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install --no-cache-dir \
    copier==9.4.1 \
    redis[hiredis]==5.0.1 \
    httpx==0.27.0 \
    structlog==24.1.0 \
    pyyaml==6.0.1 \
    pyjwt[crypto]==2.8.0

WORKDIR /app

COPY src/ /app/

CMD ["python", "main.py"]
