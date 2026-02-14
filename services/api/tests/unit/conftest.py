import os
import sys

# Ensure /app is in path so 'src' can be imported inside Docker
sys.path.append("/app")

# Provide required env vars for Settings validation in unit tests
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
