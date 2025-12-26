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
- project scope: only data from a single project (project_id required).
- user scope: all data owned by a user (tenant_id required).
- public scope: shared docs about the orchestrator/product (no tenant_id).

## Data model (index layer)
The RAG service stores indexed copies of content in a dedicated layer.

- rag_documents
  - id, tenant_id, project_id, scope, source_type, source_id
  - title, body, tsv, created_at, updated_at
- rag_chunks
  - id, document_id, chunk_index, chunk_text, embedding (vector(512)), token_count, tsv
  - created_at
- rag_conversation_summaries
  - id, tenant_id, project_id, thread_id
  - summary_text, message_ids, created_at
- rag_query_logs (optional in v1)
  - id, tenant_id, project_id, scope, query_text, latency_ms
  - result_count, token_usage, created_at

## Iteration plan

### Iteration 0 - Alignment and API contract
Scope: agree on identifiers and service responsibilities.

Tasks:
- Define tenant identity (telegram user vs org/account).
- Define project ownership rules (who can query what).
- Define "public" corpus boundaries.
- Draft API contract:
  - POST /rag/query (scope, tenant_id, project_id, query, top_k, mode)
  - POST /rag/enrich (scope, tenant_id, project_id, message, top_k)
  - POST /rag/ingest (source_type, source_id, content, metadata)
- Define v1 sources (spec, README, decisions, incidents, summaries).

Done when:
- API contract and identifiers are approved.
- v1 sources are explicitly listed.

### Iteration 1 - Database schema and access control
Scope: make Postgres ready for hybrid search.

Tasks:
- Enable pgvector extension.
- Add tables: rag_documents, rag_chunks, rag_conversation_summaries.
- Add FTS indexes (tsvector) and vector indexes.
- Decide and implement access control enforcement:
  - Option A: RLS in Postgres
  - Option B: strict filtering in RAG service queries

Done when:
- Tables and indexes are created.
- Queries can filter by tenant_id and scope before retrieval.

### Iteration 2 - Ingestion pipelines (v1 sources)
Scope: populate the index layer from existing sources.

Tasks:
- Index project specs (.project-spec.yaml) and README.
- Index decisions/ADRs and incident summaries.
- Add conversation summarizer (creates rag_conversation_summaries).
- Implement chunking (see Chunking Strategy below).
- Add embedding generation via OpenAI API.
- Add reindex hooks on updates (github_sync or scheduler tasks).

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
  - Hard filter by tenant_id, project_id, scope
  - Full-text search to gather candidates
  - Vector search for semantic matches
  - Merge and rerank (simple RRF or score merge)
- Add "no results" handling with clear signals.
- Return sources and metadata for transparency.

Done when:
- Query results are limited to allowed scope.
- API returns structured hits with sources and scores.

### Iteration 4 - Integration with LangGraph
Scope: use RAG in PO and other nodes.

Tasks:
- Add RAG tool client in LangGraph services.
- Add "enrichment" mode that returns context blocks and citations.
- Keep input and enrichment separate to avoid query rewriting.
- Define when to call RAG (routing heuristics).

Done when:
- PO can call search/enrich and append context to prompts.
- Enrichment is optional and traceable.

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

1) Tenant identity
   - Decision: use user_id from the main database (not Telegram-specific).
   - Note: project_id is used for scoping convenience and noise reduction.
   - Risk: migrating from user_id to org/account later can be painful.

2) Public corpus
   - Option A: separate table with scope=public
   - Option B: same tables with tenant_id NULL
   - Risk: accidental leaks if filters are inconsistent.

3) Conversation indexing
   - Option A: embed all messages
   - Option B: embed summaries only
   - Tradeoff: accuracy vs noise/cost.

4) Code indexing
   - Option A: no code in v1
   - Option B: only docs and specs
   - Risk: code embeddings are expensive and noisy.

5) Access control enforcement
   - Option A: Postgres RLS
   - Option B: service-level filtering only
   - Risk: service bug may leak data without RLS.

6) Embedding provider
   - Decision: OpenAI text-embedding-3-small, 512 dimensions.
   - Rationale: already have API key, cheap ($0.02/1M tokens), sufficient for small corpus.
   - Fallback: switch to Voyage multilingual if Russian retrieval quality is poor.
   - Note: 512 dims via API parameter (not post-hoc truncation).

7) Enrichment policy
   - Option A: always enrich
   - Option B: conditional enrich (classification)
   - Risk: irrelevant context can degrade responses.

8) Reindex triggers
   - Option A: webhook-driven
   - Option B: scheduled sync
   - Risk: stale context vs operational complexity.

9) Retention and deletion
   - Option A: keep full history
   - Option B: TTL for messages, keep summaries
   - Risk: privacy vs recall quality.
