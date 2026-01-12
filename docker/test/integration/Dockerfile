FROM python:3.12-slim

WORKDIR /app

# Install test dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy tests (usually mounted, but good for caching deps)
COPY . .

CMD ["pytest"]
