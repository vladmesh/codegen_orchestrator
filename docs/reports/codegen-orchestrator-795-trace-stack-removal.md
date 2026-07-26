# Trace stack removal measurement

Measured on the production stand on 2026-07-26 before removing the unused trace receiver:

| Component | RSS | Image size |
|---|---:|---:|
| ClickHouse | 569 MB | 1.22 GB |
| Langfuse worker | 341 MB | 1.67 GB |
| MinIO | 75 MB | 241 MB |
| Langfuse web | 10 MB | 1.5 GB |
| Total | about 995 MB | about 4.6 GB |

The trace data tables were empty apart from 72 schema-migration rows. The operator should remove the obsolete containers, images, and volumes after this change is deployed; that operation is intentionally outside this repository change.

Loki remains in place. Its retention is configured in `infra/loki.yml` for 168 hours, with the compactor retention deletion enabled.
