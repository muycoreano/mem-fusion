#!/usr/bin/env python3
"""
Preload test memories into dev peers' local Qdrant collections.

Used by WP3 verification (test-wp3a-promotion.py) to populate peer-b and
peer-c with realistic memory records before exercising the federation flow.

Idempotent: skips memories that already exist (by content_hash dedup).

Usage:
  ~/.local/share/cowork-memory/venv/bin/python dev/preload-test-memories.py
"""
import hashlib
import sys
import uuid
from datetime import datetime, timezone

import httpx
from qdrant_client import QdrantClient
from qdrant_client.models import (
    FieldCondition, Filter, MatchValue, PointStruct,
)

OLLAMA_URL = "http://127.0.0.1:11434"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()[:16]


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def embed(text: str) -> list[float]:
    r = httpx.post(f"{OLLAMA_URL}/api/embeddings",
                   json={"model": "nomic-embed-text", "prompt": text},
                   timeout=30.0)
    r.raise_for_status()
    return r.json()["embedding"]


PEERS = {
    "mem-fusion-peer-b": {
        "qdrant_url": "http://127.0.0.1:6533",
        "collection": "mem_fusion_peer_b_memories",
        "memories": [
            {
                "content":    "Constellation v0.3.0 ships with auto-promote as the default; a human-review apprenticeship loop is deferred for a later version.",
                "type":       "decision",
                "tags":       ["constellation", "v0.3.0-scope"],
                "project":    "mem-fusion",
                "importance": 4,
            },
            {
                "content":    "The orchestrator is whichever node currently holds orchestration rights for a group; it's a per-group role, not a per-node type.",
                "type":       "context",
                "tags":       ["constellation", "architecture"],
                "project":    "mem-fusion",
                "importance": 3,
            },
            {
                "content":    "Verbatim content plus vector preservation is the integrity invariant for memory federation between Mem-Fusion nodes.",
                "type":       "preference",
                "tags":       ["constellation", "integrity"],
                "project":    "mem-fusion",
                "importance": 4,
            },
        ],
    },
    "mem-fusion-peer-c": {
        "qdrant_url": "http://127.0.0.1:6633",
        "collection": "mem_fusion_peer_c_memories",
        "memories": [
            {
                "content":    "Each Qdrant collection in the constellation is sovereign; canonicals across different groups do not converge to a shared state.",
                "type":       "decision",
                "tags":       ["constellation", "qdrant"],
                "project":    "mem-fusion",
                "importance": 4,
            },
            {
                "content":    "The content_hash function across Mem-Fusion and Constellation is SHA256(content.strip().lower()).hexdigest()[:16] — must be byte-identical at every node.",
                "type":       "fact",
                "tags":       ["constellation", "protocol"],
                "project":    "mem-fusion",
                "importance": 5,
            },
            {
                "content":    "Mem-Fusion stays at v0.1.0 stdio MCP unchanged; Constellation v0.3.0 runs as a separate persistent HTTP MCP daemon on the same machine.",
                "type":       "context",
                "tags":       ["constellation", "architecture"],
                "project":    "mem-fusion",
                "importance": 3,
            },
        ],
    },
}


def find_existing(client: QdrantClient, collection: str, chash: str) -> str | None:
    results, _ = client.scroll(
        collection_name=collection,
        scroll_filter=Filter(must=[
            FieldCondition(key="content_hash", match=MatchValue(value=chash))
        ]),
        limit=1, with_payload=False,
    )
    return str(results[0].id) if results else None


def preload_peer(peer_name: str, cfg: dict) -> dict:
    """Preload one peer. Returns dict mapping content → memory_id."""
    print(f"\n→ {peer_name}  (Qdrant: {cfg['qdrant_url']})")
    client = QdrantClient(url=cfg["qdrant_url"], timeout=10)

    results = {"stored": [], "skipped": []}
    for mem in cfg["memories"]:
        chash = content_hash(mem["content"])
        existing_id = find_existing(client, cfg["collection"], chash)
        snippet = mem["content"][:60].replace("\n", " ") + "..."

        if existing_id:
            print(f"  [skip ] {existing_id[:8]}…  {snippet}")
            results["skipped"].append({"id": existing_id, "content": mem["content"]})
            continue

        vec = embed(mem["content"])
        mem_id = str(uuid.uuid4())
        client.upsert(
            collection_name=cfg["collection"],
            points=[PointStruct(
                id=mem_id, vector=vec,
                payload={
                    "content":      mem["content"],
                    "type":         mem["type"],
                    "tags":         mem["tags"],
                    "project":      mem["project"],
                    "importance":   mem["importance"],
                    "session_id":   "",
                    "content_hash": chash,
                    "timestamp":    iso_now(),
                    "source":       "preload",
                },
            )],
        )
        print(f"  [store] {mem_id[:8]}…  {snippet}")
        results["stored"].append({"id": mem_id, "content": mem["content"]})

    return results


def main():
    print("=" * 70)
    print("Preloading test memories into dev peers")
    print("=" * 70)

    # Verify Ollama is reachable
    try:
        httpx.get(f"{OLLAMA_URL}/api/tags", timeout=2.0).raise_for_status()
    except Exception as e:
        print(f"✗ Ollama unreachable at {OLLAMA_URL}: {e}", file=sys.stderr)
        sys.exit(1)

    summary = {}
    for peer_name, cfg in PEERS.items():
        try:
            httpx.get(f"{cfg['qdrant_url']}/healthz", timeout=2.0).raise_for_status()
        except Exception as e:
            print(f"✗ {peer_name} Qdrant unreachable at {cfg['qdrant_url']}: {e}", file=sys.stderr)
            continue
        summary[peer_name] = preload_peer(peer_name, cfg)

    print("\n" + "=" * 70)
    print("Summary")
    print("=" * 70)
    for peer_name, r in summary.items():
        print(f"  {peer_name}:  {len(r['stored'])} stored, {len(r['skipped'])} skipped")

    # Emit a manifest the test script can read
    import json
    manifest_path = "/tmp/wp3a-preload-manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\n  Manifest written to {manifest_path}")


if __name__ == "__main__":
    main()
