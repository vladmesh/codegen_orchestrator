# Trace stack removal and validation

## Resource measurement

The following values were measured on the production stand on 2026-07-26, before removing the unused trace receiver:

| Component | RSS | Image size |
|---|---:|---:|
| ClickHouse | 569 MB | 1.22 GB |
| Langfuse worker | 341 MB | 1.67 GB |
| MinIO | 75 MB | 241 MB |
| Langfuse web | 10 MB | 1.5 GB |
| Total | about 995 MB | about 4.6 GB |

The trace data tables were empty apart from 72 schema-migration rows. These are a measurement of reclaimable resources, not a post-removal measurement: removing running containers, images, and volumes on the production stand is an operator action outside this repository change. A post-removal disk or memory measurement is therefore not available from this work.

## Post-removal validation

- The Compose configuration was rendered successfully without Langfuse, ClickHouse, or MinIO services and without their volumes.
- The affected LangGraph consumers (PO, architect, engineering, and deploy) start and invoke their graphs without Langfuse callbacks or exporter configuration. Their logs contain no span-export errors because this application had no independent OpenTelemetry instrumentation; the OpenTelemetry packages were transitive Langfuse SDK dependencies and were removed with that SDK. This records the mismatch with the original task wording.
- The hourly scheduler analytics path remains unchanged and continues to query Loki to write `analytics_hourly`, `analytics_daily`, and `analytics_known_users`; the LK endpoints continue to read those tables. This was checked after the removal by the existing unit suite and Compose configuration validation.

Loki remains in place. Its retention is configured in `infra/loki.yml` for 168 hours, with the compactor retention deletion enabled.
