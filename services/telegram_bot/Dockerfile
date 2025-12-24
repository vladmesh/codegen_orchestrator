FROM python:3.12-slim

WORKDIR /app

# Copy and install shared module first
COPY shared ./shared
RUN pip install --no-cache-dir ./shared

# Copy and install service dependencies
COPY services/telegram_bot/pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy source
COPY services/telegram_bot/src ./src

# Run
CMD ["python", "-m", "src.main"]
