FROM python:3.12-slim

WORKDIR /app

# Install test dependencies
COPY docker/test/integration/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared and packages
COPY shared ./shared
COPY packages ./packages
COPY services ./services

# Copy tests (usually mounted, but good for caching deps)
COPY tests/integration /app/tests/integration

ENV PYTHONPATH=/app

CMD ["pytest"]
