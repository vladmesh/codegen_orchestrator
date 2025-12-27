# RAG Service Plan (Iterative)

Plan for a dedicated RAG microservice that uses the shared Postgres database.
This document is aligned with docs/GRAPH_REFACTORING_PLAN.md (Iteration 4).

## Goals and principles
- Separate RAG microservice, shared Postgres (no separate DB).
- Strict access control by tenant and scope; LLM cannot bypass it.
- Hybrid search: full-text + vector retrieval.
- Keep v1 sources minimal and high signal.
- Provide "enrichment" as optional context, not a rewrite of user input.

## Scope model
- project scope: only data from a single project (user_id + project_id required).
- user scope: all data owned by a user (user_id required).
- public scope: shared docs about the orchestrator/product (scope = public, user_id NULL).

## Data model (index layer)
The RAG service stores indexed copies of content in a dedicated layer.

- rag_documents
  - id, user_id, project_id, scope, source_type, source_id, source_uri
  - source_hash, source_updated_at, language
  - title, body, tsv, created_at, updated_at
- rag_chunks
  - id, document_id, user_id, project_id, scope
  - chunk_index, chunk_text, chunk_hash, token_count
  - embedding (vector(512)), embedding_model, tsv
  - created_at

Note: retrieval should run on rag_chunks. If you do not denormalize user_id/project_id/scope into rag_chunks, expose a SECURITY BARRIER view that joins rag_chunks to rag_documents and apply filters there.
- rag_conversation_summaries
  - id, user_id, project_id, thread_id
  - summary_text, message_ids, created_at
- rag_query_logs (optional in v1)
  - id, user_id, project_id, scope, query_text, latency_ms
  - result_count, token_usage, created_at

## Infrastructure additions
- Use a Postgres image with pgvector enabled (pgvector/pgvector or custom).
- Add init SQL (docker-entrypoint-initdb.d) or Alembic migration to enable:
  - CREATE EXTENSION IF NOT EXISTS vector;
  - CREATE EXTENSION IF NOT EXISTS pg_trgm; (optional, for FTS quality)
  - CREATE EXTENSION IF NOT EXISTS unaccent; (optional)
- Phase 2 (RLS): create DB roles:
  - app/rag_service role without BYPASSRLS (used by services).
  - admin/migration role for Alembic and backfills.

## RLS implementation notes (Phase 2)
- RLS is enforced by Postgres, not by the service itself.
- The service must set session variables per transaction:
  - SET LOCAL app.user_id = '<user_id>';
  - SET LOCAL app.project_id = '<project_id>'; (required for project scope)
- Use a non-superuser role without BYPASSRLS for app connections.
- If using pgbouncer with transaction pooling, set variables in every transaction.

Example policy:
```sql
ALTER TABLE rag_documents ENABLE ROW LEVEL SECURITY;

CREATE POLICY rag_documents_select ON rag_documents
FOR SELECT
USING (
  scope = 'public'
  OR (scope = 'user' AND user_id = current_setting('app.user_id')::int)
  OR (scope = 'project' AND user_id = current_setting('app.user_id')::int
      AND project_id = current_setting('app.project_id'))
);

CREATE POLICY rag_documents_insert ON rag_documents
FOR INSERT
WITH CHECK (
  user_id = current_setting('app.user_id')::int
  AND (scope != 'project' OR project_id = current_setting('app.project_id'))
);
```

Example transaction:
```sql
BEGIN;
SET LOCAL app.user_id = '...';
SET LOCAL app.project_id = '...';
-- queries here
COMMIT;
```

## Iteration plan

### Iteration 0 - Alignment and API contract
Scope: agree on identifiers and service responsibilities.

Tasks:
- Define user identity (user_id vs telegram_id mapping).
- Define project ownership rules (who can query what).
- Define "public" corpus boundaries.
- Draft API contract:
  - POST /rag/query (scope, user_id, project_id, query, top_k, mode)
  - POST /rag/enrich (scope, user_id, project_id, message, top_k)
  - POST /rag/ingest (source_type, source_id, content, metadata)
- Define v1 sources (spec, README, decisions, incidents, summaries).

Done when:
- API contract and identifiers are approved.
- v1 sources are explicitly listed.

### Iteration 1 - Database schema and access control
Scope: make Postgres ready for hybrid search.

Tasks:
- Enable pgvector extension (via Alembic or init SQL).
- Add tables: rag_documents, rag_chunks, rag_conversation_summaries (include source metadata).
- Add FTS indexes on rag_chunks.tsv and vector indexes on rag_chunks.embedding.
- Access control for v1:
  - No RLS in Iteration 1 (deferred to Phase 2).
  - Service-level filtering by user_id/scope will be added with the RAG service.

Done when:
- Tables and indexes are created.
- Schema is ready for hybrid search; RLS will be added in Phase 2.

### Iteration 2 - Ingestion pipelines (v1 sources)
Scope: populate the index layer from existing sources.

Tasks:
- Index project specs (.project-spec.yaml) and README.
- Index decisions/ADRs and incident summaries.
- Add conversation summarizer (creates rag_conversation_summaries) after the
  total unsummarized message volume per tenant exceeds a threshold (tokens or
  characters). Note: no thread_id in Telegram; revisit when per-thread context
  is available.
- Implement chunking (see Chunking Strategy below).
- Add embedding generation via OpenAI API.
- Store source_uri, source_hash, source_updated_at, language, chunk_hash, embedding_model.
- Add reindex hooks on updates via GitHub Actions webhook (push to main with
  path filters). Make ingestion idempotent by source_hash or commit_sha and
  verify webhook auth (HMAC or signed JWT). Optional: scheduled reconcile.

#### Chunking Strategy (v1)
- Chunk size: 512 tokens
- Overlap: 10% (~50 tokens)
- Split method: by `\n\n` (paragraphs), fallback to token boundary

| Source type | Strategy |
|-------------|----------|
| `.project-spec.yaml` | Whole file (usually <1k tokens) |
| README.md | Split by `##` headers, then by paragraphs |
| ADR/Decisions | Whole file (usually <1k tokens) |
| Conversations | Summaries only, not raw messages |

Upgrade path (v2): semantic chunking by headings for markdown if quality insufficient.

Done when:
- Updated sources are reindexed reliably.
- Chunks and embeddings are stored for v1 sources.

### Iteration 3 - Retrieval pipeline (hybrid search)
Scope: return context in a predictable, testable way.

Tasks:
- Implement search flow:
  - Hard filter by user_id, project_id, scope
  - Full-text search on rag_chunks.tsv to gather candidates
  - Vector search on rag_chunks.embedding for semantic matches
  - Merge and rerank (simple RRF or score merge)
- Apply token budget limits (see below).
- Add "no results" handling with clear signals.
- Return sources and metadata for transparency.

#### Token Budget (v1)
| Parameter | Value | Rationale |
|-----------|-------|-----------|
| `top_k` | 5 | More chunks = more noise |
| `max_tokens` | 2000 | ~10-20% of typical prompt |
| `min_similarity` | 0.7 | Skip irrelevant results |

Logic:
1. Retrieve top-5 chunks by combined score
2. Sum token counts, truncate if > 2000 tokens
3. If best chunk score < 0.7 → return empty (no relevant context found)

Done when:
- Query results are limited to allowed scope.
- API returns structured hits with sources and scores.

### Iteration 4 - Integration with LangGraph
Scope: use RAG as explicit tool in agent nodes.

Tasks:
- Add RAG tool `search_project_context` in LangGraph services.
- Tool returns context blocks with sources and citations.
- Add tool to relevant agents (PO, Analyst, Developer).
- Update agent prompts to guide when to use the tool.

#### RAG Usage (v1)
- **Mode:** explicit tool call only, no automatic enrichment.
- **Rationale:** avoids overhead on simple queries ("привет", status checks).
- **Agent decides** when context is needed based on prompt guidance.
- **v2 option:** add automatic enrichment if agents consistently miss useful context.

Example prompt addition:
```
When answering questions about existing projects, use search_project_context
tool to retrieve relevant specs, decisions, and history.
```

Done when:
- Agents can call RAG tool and receive structured context.
- No automatic pre-call enrichment (deferred to v2 if needed).

### Iteration 5 - Metrics and evaluation
Scope: visibility into quality and cost.

Tasks:
- Log latency, hit rate, top_k coverage, token usage.
- Track empty result rates and stale documents.
- Add minimal offline evaluation set (few sample queries).

Done when:
- Basic quality and performance metrics are available.

### Iteration 6 - Hardening and cleanup
Scope: long-term safety and maintainability.

Tasks:
- Add retention and delete-by-tenant logic.
- Add backfill and reindex jobs.
- Document the RAG service and its API.

Done when:
- Retention and deletion are supported.
- Docs cover scope boundaries and access rules.

## Open questions and options
These are unresolved and should be decided during Iteration 0/1.

1) User identity
   - Decision: use user_id (users.id) as the canonical identifier.
   - Note: project_id is used for scoping convenience and noise reduction.
   - Risk: moving from single-user to org/account later can be painful.

2) Public corpus
   - Option A: separate table with scope=public
   - Option B: same tables with scope='public' and user_id NULL
   - Recommendation: Option B + SECURITY BARRIER view for all reads to keep a single retrieval path.
   - When to revisit: if public corpus grows significantly or needs a different retention/audit policy.
   - Risk: accidental mixing if filters/views are inconsistent.

3) Conversation indexing
   - Decision: summaries only, created when the per-tenant unsummarized
     message volume exceeds a threshold (tokens or characters).
   - Note: no thread_id in Telegram; revisit when per-thread context is available.
   - Tradeoff: accuracy vs noise/cost.

4) Code indexing
   - Option A: no code in v1
   - Option B: only docs and specs
   - Risk: code embeddings are expensive and noisy.

5) Access control enforcement
   - Decision: v1 uses service-level filtering only; add RLS in Phase 2.
   - Rationale: reduce early DB changes; RLS adds a hard safety barrier later.
   - Risk: more setup and testing complexity when RLS is added.

6) Embedding provider
   - Decision: OpenAI text-embedding-3-small, 512 dimensions.
   - Rationale: already have API key, cheap ($0.02/1M tokens), sufficient for small corpus.
   - Fallback: switch to Voyage multilingual if Russian retrieval quality is poor.
   - Note: 512 dims via API parameter (not post-hoc truncation).

7) Enrichment policy
   - Decision: explicit tool call only in v1, no automatic enrichment.
   - Rationale: avoids overhead on simple queries, agent decides when context is needed.
   - v2 option: add automatic enrichment if agents consistently miss useful context.

8) Reindex triggers
   - Decision: GitHub Actions webhook on push to main (with path filters).
   - Option: scheduled reconcile as a fallback.
   - Risk: stale context vs operational complexity.

9) Retention and deletion
   - Option A: keep full history
   - Option B: TTL for messages, keep summaries
   - Risk: privacy vs recall quality.
