"""Quiesced, atomic upgrade of released plaintext PO checkpoint payloads.

Run with the deployed key and CHECKPOINT_DATABASE_URL. Default is validation
and counts only; --apply converts in one transaction under exclusive locks.
"""

from __future__ import annotations

import argparse
import os

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import structlog

from shared.log_config import setup_logging

from .checkpoints import CIPHER, ENVELOPE, CheckpointProtectionError, ProtectedSerializer

JSON_PAYLOADS = ("checkpoint", "metadata")
BLOB_KEYS = {
    "checkpoint_blobs": ("thread_id", "checkpoint_ns", "channel", "version"),
    "checkpoint_writes": ("thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx"),
}


def _counts(conn) -> dict:
    counts = {}
    for column in JSON_PAYLOADS:
        row = conn.execute(
            sql.SQL("""
            SELECT count(*) FILTER (WHERE {column} ? %s) AS encrypted,
                   count(*) FILTER (WHERE NOT {column} ? %s) AS plaintext
            FROM checkpoints
        """).format(column=sql.Identifier(column)),
            (ENVELOPE, ENVELOPE),
        ).fetchone()
        counts[f"checkpoints.{column}"] = row
    for table in BLOB_KEYS:
        counts[table] = conn.execute(
            sql.SQL("""
            SELECT count(*) FILTER (WHERE type LIKE %s) AS encrypted,
                   count(*) FILTER (WHERE type IS NULL OR
                       (type <> 'empty' AND type NOT LIKE %s)) AS plaintext,
                   count(*) FILTER (WHERE type = 'empty') AS empty
            FROM {table}
        """).format(table=sql.Identifier(table)),
            (f"%+{CIPHER}", f"%+{CIPHER}"),
        ).fetchone()
    return counts


def _upgrade_json(conn, serde, *, apply) -> dict:
    converted = {f"checkpoints.{column}": 0 for column in JSON_PAYLOADS}
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM checkpoints")
        for row in cur:
            values = {}
            for column in JSON_PAYLOADS:
                value = row[column]
                if ENVELOPE in value:
                    if column == "checkpoint":
                        serde.open_checkpoint(value)
                    else:
                        serde.open(value)
                    continue
                if column == "checkpoint":
                    # Released checkpoint-postgres 3.0.4 / LangGraph 1.0.5 format.
                    if value["v"] not in {1, 2, 3, 4} or not isinstance(
                        value["channel_values"], dict
                    ):
                        raise CheckpointProtectionError("Unsupported released checkpoint format")
                    values[column] = serde.seal_checkpoint(value)
                else:
                    values[column] = serde.seal(value)
                converted[f"checkpoints.{column}"] += 1
            if apply and values:
                assignments = sql.SQL(", ").join(
                    sql.SQL("{} = %s").format(sql.Identifier(c)) for c in values
                )
                conn.execute(
                    sql.SQL("""
                    UPDATE checkpoints SET {assignments}
                    WHERE thread_id = %s AND checkpoint_ns = %s AND checkpoint_id = %s
                """).format(assignments=assignments),
                    (
                        *(Jsonb(v) for v in values.values()),
                        row["thread_id"],
                        row["checkpoint_ns"],
                        row["checkpoint_id"],
                    ),
                )
    return converted


def _upgrade_blobs(conn, serde, *, apply) -> dict:
    converted = {}
    legacy = JsonPlusSerializer()
    for table, keys in BLOB_KEYS.items():
        converted[table] = 0
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table)))
            for row in cur:
                typ, blob = row["type"], row["blob"]
                if typ == "empty":
                    if table != "checkpoint_blobs" or blob is not None:
                        raise CheckpointProtectionError("Invalid empty checkpoint blob")
                    continue
                if typ and typ.endswith(f"+{CIPHER}"):
                    serde.loads_typed((typ, blob))
                    continue
                if typ not in {"json", "msgpack", "bytes", "bytearray", "null"} or blob is None:
                    raise CheckpointProtectionError("Unsupported released checkpoint blob")
                # Validate first, then encrypt the exact released bytes. No object
                # round-trip changes message IDs, tool calls or pending-work order.
                legacy.loads_typed((typ, blob))
                ciphername, encrypted = serde.cipher.encrypt(blob)
                converted[table] += 1
                if apply:
                    where = sql.SQL(" AND ").join(
                        sql.SQL("{} = %s").format(sql.Identifier(k)) for k in keys
                    )
                    conn.execute(
                        sql.SQL("UPDATE {} SET type = %s, blob = %s WHERE {}").format(
                            sql.Identifier(table), where
                        ),
                        (f"{typ}+{ciphername}", encrypted, *(row[k] for k in keys)),
                    )
    return converted


def upgrade(database_url: str, *, writers_quiesced: bool, apply: bool = False) -> dict:
    if not writers_quiesced:
        raise CheckpointProtectionError("PO checkpoint upgrade requires quiesced writers")
    serde = ProtectedSerializer()
    try:
        with psycopg.connect(database_url, row_factory=dict_row) as conn:
            with conn.transaction():
                if conn.execute("SELECT current_schema() AS schema").fetchone()["schema"] != (
                    "langgraph"
                ):
                    raise CheckpointProtectionError(
                        "CHECKPOINT_DATABASE_URL must select the langgraph schema"
                    )
                # Stops all table writers for the whole conversion, fails promptly
                # if an in-flight transaction remains. An old writer must never resume.
                conn.execute("""
                    LOCK TABLE checkpoints, checkpoint_blobs, checkpoint_writes
                    IN ACCESS EXCLUSIVE MODE NOWAIT
                """)
                before = _counts(conn)
                converted = {
                    **_upgrade_json(conn, serde, apply=apply),
                    **_upgrade_blobs(conn, serde, apply=apply),
                }
                after = _counts(conn)
                if apply and any(c["plaintext"] for c in after.values()):
                    raise CheckpointProtectionError("PO checkpoint upgrade left plaintext payloads")
                return {
                    "mode": "apply" if apply else "dry-run",
                    "before": before,
                    "converted" if apply else "would_convert": converted,
                    "after": after,
                }
    except CheckpointProtectionError:
        raise
    except Exception:
        raise CheckpointProtectionError(
            "PO checkpoint upgrade failed; transaction rolled back"
        ) from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--writers-quiesced", action="store_true", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    setup_logging(service_name="po-checkpoint-upgrade", log_format="json")
    logger = structlog.get_logger(__name__)
    url = os.environ.get("CHECKPOINT_DATABASE_URL")
    if not url:
        logger.error("po_checkpoint_upgrade_failed", reason="CHECKPOINT_DATABASE_URL is required")
        return 1
    try:
        report = upgrade(url, writers_quiesced=args.writers_quiesced, apply=args.apply)
    except CheckpointProtectionError as exc:
        logger.error("po_checkpoint_upgrade_failed", reason=str(exc))
        return 1
    logger.info("po_checkpoint_upgrade_counts", **report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
