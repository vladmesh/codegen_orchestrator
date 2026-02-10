"""Unit tests for env_analyzer module."""

from src.subgraphs.devops.env_analyzer import _classify_by_pattern


class TestClassifyByPattern:
    """Tests for _classify_by_pattern function."""

    def test_classify_image_as_computed(self):
        """Docker image variables should be classified as computed."""
        assert _classify_by_pattern("BACKEND_IMAGE") == "computed"
        assert _classify_by_pattern("TG_BOT_IMAGE") == "computed"
        assert _classify_by_pattern("FRONTEND_IMAGE") == "computed"
        assert _classify_by_pattern("backend_image") == "computed"

    def test_classify_infra_exact(self):
        """Exact infra matches should be classified as infra."""
        assert _classify_by_pattern("DATABASE_URL") == "infra"
        assert _classify_by_pattern("REDIS_URL") == "infra"
        assert _classify_by_pattern("POSTGRES_USER") == "infra"
        assert _classify_by_pattern("POSTGRES_PASSWORD") == "infra"

    def test_classify_infra_patterns(self):
        """Infra pattern matches should be classified as infra."""
        assert _classify_by_pattern("APP_SECRET_KEY") == "infra"
        assert _classify_by_pattern("JWT_SECRET") == "infra"
        assert _classify_by_pattern("SESSION_SECRET") == "infra"

    def test_classify_computed_exact(self):
        """Exact computed matches should be classified as computed."""
        assert _classify_by_pattern("APP_NAME") == "computed"
        assert _classify_by_pattern("APP_ENV") == "computed"
        assert _classify_by_pattern("DEBUG") == "computed"
        assert _classify_by_pattern("BACKEND_URL") == "computed"

    def test_classify_user_patterns(self):
        """User pattern matches should be classified as user."""
        assert _classify_by_pattern("TELEGRAM_BOT_TOKEN") == "user"
        assert _classify_by_pattern("OPENAI_API_KEY") == "user"
        assert _classify_by_pattern("STRIPE_API_KEY") == "user"
        # Note: STRIPE_SECRET_KEY is classified as infra because SECRET_KEY is an infra pattern

    def test_unknown_returns_none(self):
        """Unknown variables should return None."""
        assert _classify_by_pattern("RANDOM_VAR") is None
        assert _classify_by_pattern("UNKNOWN_SETTING") is None
