from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.routers.rag_ingest import upsert_document
from src.schemas.rag import RAGDocsIngest, RAGDocumentPayload


class _Encoding:
    def encode(self, text: str) -> list[int]:
        return list(range(len(text)))


@pytest.mark.asyncio
async def test_embedding_failure_does_not_stage_document_or_chunks():
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute.return_value = result
    payload = RAGDocsIngest(documents=[])
    document = RAGDocumentPayload(
        source_type="task",
        source_id="task-1",
        scope="public",
        content="A document that needs embedding.",
    )

    with patch(
        "src.routers.rag_ingest.generate_chunk_embeddings",
        side_effect=RuntimeError("embedding backend unavailable"),
    ):
        with pytest.raises(RuntimeError, match="embedding backend unavailable"):
            await upsert_document(db, payload, document, _Encoding())

    db.add.assert_not_called()
    db.add_all.assert_not_called()
    db.flush.assert_not_called()
