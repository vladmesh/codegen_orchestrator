"""The schema created by the migration chain must agree with the ORM."""

from pprint import pformat

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import pytest

from shared.models import Base


@pytest.mark.asyncio
async def test_migrated_schema_matches_model_metadata(db_session) -> None:
    # The service harness starts the API with `alembic upgrade head` against a
    # fresh PostgreSQL database. Comparing that schema catches changes omitted
    # from either the migration chain or the model, without create_all masking
    # the disagreement.
    def compare(session):
        context = MigrationContext.configure(session.connection(), opts={"compare_type": True})
        return compare_metadata(context, Base.metadata)

    differences = await db_session.run_sync(compare)
    assert not differences, f"Migration/model schema drift:\n{pformat(differences)}"
