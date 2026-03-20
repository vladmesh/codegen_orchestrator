import os
import sys

# Ensure /app is in path so 'src' can be imported inside Docker
sys.path.append("/app")

# Provide required env vars for Settings validation in unit tests
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault(
    "SECRETS_ENCRYPTION_KEY", "wHhIQWmPfLt60oHdxzbQhY1ZKnUon12e5_SuZ33xDxc="
)  # Valid Fernet key for tests only
os.environ.setdefault("LK_JWT_SECRET", "unit-test-lk-jwt-secret")
