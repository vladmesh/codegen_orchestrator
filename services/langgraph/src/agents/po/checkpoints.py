"""PO's encrypted PostgreSQL boundary, using the released saver and serializer.

The native serializer covers blobs/writes, but not JSONB checkpoint state or
metadata. Keep only SQL routing fields outside authenticated envelopes.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from typing import Any

from langgraph.checkpoint.base import get_serializable_checkpoint_metadata
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from psycopg.types.json import Jsonb

from shared.crypto import SecretsCipher

ENVELOPE = "po_encrypted_v1"
CIPHER = "fernet"


class CheckpointProtectionError(RuntimeError):
    """Safe diagnostic: never contains a payload, key or chained exception."""


class _ProjectCipher:
    def __init__(self) -> None:
        try:
            self.cipher = SecretsCipher()
        except Exception:
            raise CheckpointProtectionError(
                "PO checkpoints require a valid SECRETS_ENCRYPTION_KEY"
            ) from None

    def encrypt(self, plaintext: bytes) -> tuple[str, bytes]:
        # SecretsCipher's public API takes text; base64 is transport INSIDE Fernet.
        return CIPHER, self.cipher.encrypt(base64.b64encode(plaintext).decode()).encode()

    def decrypt(self, ciphername: str, ciphertext: bytes) -> bytes:
        if ciphername != CIPHER:
            raise CheckpointProtectionError("Unsupported PO checkpoint cipher")
        if not ciphertext:
            raise CheckpointProtectionError("Empty PO checkpoint ciphertext")
        return base64.b64decode(self.cipher.decrypt(ciphertext.decode()), validate=True)


class ProtectedSerializer(EncryptedSerializer):
    def __init__(self) -> None:
        super().__init__(_ProjectCipher())

    def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
        try:
            return super().dumps_typed(obj)
        except Exception:
            raise CheckpointProtectionError("PO checkpoint encryption failed") from None

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        if not data[0].endswith(f"+{CIPHER}"):
            raise CheckpointProtectionError("Plaintext PO checkpoint requires quiesced upgrade")
        try:
            return super().loads_typed(data)
        except Exception:
            raise CheckpointProtectionError("PO checkpoint decryption failed") from None

    def seal(self, value: dict) -> dict:
        typ, blob = self.dumps_typed(value)
        return {ENVELOPE: {"type": typ, "ciphertext": blob.decode()}}

    def open(self, value: dict) -> dict:
        try:
            if set(value) != {ENVELOPE}:
                raise CheckpointProtectionError("Plaintext PO checkpoint requires quiesced upgrade")
            envelope = value[ENVELOPE]
            result = self.loads_typed((envelope["type"], envelope["ciphertext"].encode()))
            if not isinstance(result, dict):
                raise TypeError
            return result
        except CheckpointProtectionError:
            raise
        except Exception:
            raise CheckpointProtectionError("Invalid PO checkpoint envelope") from None

    def seal_checkpoint(self, value: dict) -> dict:
        return {
            "v": value["v"],
            "channel_versions": value["channel_versions"],
            **self.seal({k: v for k, v in value.items() if k not in {"v", "channel_versions"}}),
        }

    def open_checkpoint(self, value: dict) -> dict:
        return {
            **self.open({k: v for k, v in value.items() if k not in {"v", "channel_versions"}}),
            "v": value["v"],
            "channel_versions": value["channel_versions"],
        }


def _contains(value: Any, query: Any) -> bool:
    """JSONB containment for metadata filtering after authenticated decryption."""
    if isinstance(query, dict):
        return isinstance(value, dict) and all(
            k in value and _contains(value[k], v) for k, v in query.items()
        )
    if isinstance(query, list):
        return isinstance(value, list) and all(
            any(isinstance(v, list) == isinstance(q, list) and _contains(v, q) for v in value)
            for q in query
        )
    if type(value) in {int, float} and type(query) in {int, float}:
        return value == query
    return type(value) is type(query) and value == query


class ProtectedPostgresSaver(AsyncPostgresSaver):
    def __init__(self, conn, pipe=None, *, serde: ProtectedSerializer) -> None:
        super().__init__(conn=conn, pipe=pipe, serde=serde)

    async def aput(self, config, checkpoint, metadata, new_versions):
        # Same split and SQL as checkpoint-postgres 3.0.4. Encryption of ALL
        # parameters completes before opening the write cursor, including blobs.
        thread_id = config["configurable"]["thread_id"]
        namespace = config["configurable"]["checkpoint_ns"]
        parent = config["configurable"].get("checkpoint_id")
        copy = {**checkpoint, "channel_values": checkpoint["channel_values"].copy()}
        blobs = {
            k: copy["channel_values"].pop(k)
            for k, value in checkpoint["channel_values"].items()
            if value is not None and not isinstance(value, str | int | float | bool)
        }
        blob_params = await asyncio.to_thread(
            self._dump_blobs,
            thread_id,
            namespace,
            blobs,
            {k: v for k, v in new_versions.items() if k in blobs},
        )
        checkpoint_json = self.serde.seal_checkpoint(copy)
        metadata_json = self.serde.seal(get_serializable_checkpoint_metadata(config, metadata))
        async with self._cursor(pipeline=True) as cur:
            if blob_params:
                await cur.executemany(self.UPSERT_CHECKPOINT_BLOBS_SQL, blob_params)
            await cur.execute(
                self.UPSERT_CHECKPOINTS_SQL,
                (
                    thread_id,
                    namespace,
                    checkpoint["id"],
                    parent,
                    Jsonb(checkpoint_json),
                    Jsonb(metadata_json),
                ),
            )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": namespace,
                "checkpoint_id": checkpoint["id"],
            }
        }

    async def _load_checkpoint_tuple(self, value):
        return await super()._load_checkpoint_tuple(
            {
                **value,
                "checkpoint": self.serde.open_checkpoint(value["checkpoint"]),
                "metadata": self.serde.open(value["metadata"]),
            }
        )

    async def alist(self, config, *, filter=None, before=None, limit=None) -> AsyncIterator:
        count = 0
        async for saved in super().alist(config, before=before):
            if filter and not _contains(saved.metadata, filter):
                continue
            if limit is not None and count >= limit:
                break
            yield saved
            count += 1

    async def require_encrypted_rows(self) -> None:
        async with self._cursor() as cur:
            await cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM checkpoints
                    WHERE NOT checkpoint ? %s OR NOT metadata ? %s
                    UNION ALL SELECT 1 FROM checkpoint_blobs
                    WHERE type <> 'empty' AND type NOT LIKE %s
                    UNION ALL SELECT 1 FROM checkpoint_writes
                    WHERE type IS NULL OR type NOT LIKE %s
                ) AS plaintext
            """,
                (ENVELOPE, ENVELOPE, f"%+{CIPHER}", f"%+{CIPHER}"),
            )
            if (await cur.fetchone())["plaintext"]:
                raise CheckpointProtectionError("Plaintext PO checkpoint requires quiesced upgrade")
