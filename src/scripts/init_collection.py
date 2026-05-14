#!/usr/bin/env python3
"""
Initialize the cowork-memories Qdrant collection for a Mem-Fusion install.

  QDRANT_URL    default: http://127.0.0.1:6333

Safe to re-run — skips creation if the collection already exists; creates
payload indexes idempotently.
"""
import os
import sys
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PayloadSchemaType

QDRANT_URL  = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
COLLECTION  = "cowork_memories"
VECTOR_SIZE = 768  # nomic-embed-text dimensions

print(f"→ qdrant: {QDRANT_URL}")
print(f"→ collection: {COLLECTION}")

client = QdrantClient(url=QDRANT_URL, timeout=10)

existing = [c.name for c in client.get_collections().collections]
if COLLECTION in existing:
    print(f"Collection '{COLLECTION}' already exists — skipping creation.")
else:
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
    )
    print(f"Created collection '{COLLECTION}'.")

indexes = {
    "type":         PayloadSchemaType.KEYWORD,
    "project":      PayloadSchemaType.KEYWORD,
    "groups":       PayloadSchemaType.KEYWORD,  # v0.4 list-valued routing key
    "source":       PayloadSchemaType.KEYWORD,  # legacy v0.3, kept for migration
    "group_name":   PayloadSchemaType.KEYWORD,  # legacy v0.3, kept for migration
    "session_id":   PayloadSchemaType.KEYWORD,
    "content_hash": PayloadSchemaType.KEYWORD,
    "origin_node":  PayloadSchemaType.KEYWORD,
    "tags":         PayloadSchemaType.KEYWORD,
    "importance":   PayloadSchemaType.INTEGER,
    "timestamp":    PayloadSchemaType.DATETIME,
    "submitted_at": PayloadSchemaType.DATETIME,
}
for field, schema in indexes.items():
    try:
        client.create_payload_index(COLLECTION, field, schema)
        print(f"  Index: {field} ({schema.value})")
    except Exception as e:
        if "already exists" in str(e).lower():
            print(f"  Index: {field} (already exists)")
        else:
            print(f"  Index: {field} FAILED — {e}", file=sys.stderr)

info = client.get_collection(COLLECTION)
print(f"\nCollection ready. Vectors: {info.vectors_count or 0}")
