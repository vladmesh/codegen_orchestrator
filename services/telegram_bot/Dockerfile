FROM python:3.12-slim

WORKDIR /app

# Copy requirements
COPY pyproject.toml ./

# Install dependencies
RUN pip install --no-cache-dir .

# Copy source
COPY services/telegram_bot/src ./src

# Run
CMD ["python", "-m", "src.main"]
