FROM python:3.12-slim

WORKDIR /app

# Install dependencies
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastapi uvicorn

# Copy mock server code
COPY tests/e2e/mock_anthropic/ /app/mock_anthropic/

# Make it a proper package
RUN touch /app/__init__.py

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "mock_anthropic.server:app", "--host", "0.0.0.0", "--port", "8000"]
