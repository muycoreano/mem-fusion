#!/usr/bin/env python3
"""
Preload test memories into dev peers' Qdrant collections via core.store_memory.

Goes through the same code path mem-fusion uses in production (core.py), so
preload is itself a meaningful exercise of the rewritten stack. Each peer's
Qdrant is on a different port; we rebind core's module-level client per peer.

Idempotent: core.store_memory dedupes by content_hash internally.

Usage:
  ~/.local/share/cowork-memory/venv/bin/python tests/constellation/preload-memories.py
"""
import asyncio
import json
import pathlib
import sys

import httpx
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PayloadSchemaType, VectorParams

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
import core  # noqa: E402


COLLECTION_INDEXES = {
    "type":         PayloadSchemaType.KEYWORD,
    "project":      PayloadSchemaType.KEYWORD,
    "source":       PayloadSchemaType.KEYWORD,
    "session_id":   PayloadSchemaType.KEYWORD,
    "content_hash": PayloadSchemaType.KEYWORD,
    "tags":         PayloadSchemaType.KEYWORD,
    "importance":   PayloadSchemaType.INTEGER,
    "timestamp":    PayloadSchemaType.DATETIME,
}


def ensure_collection():
    """Create `cowork_memories` on core.qdrant if it does not yet exist."""
    existing = [c.name for c in core.qdrant.get_collections().collections]
    if core.COLLECTION not in existing:
        core.qdrant.create_collection(
            collection_name=core.COLLECTION,
            vectors_config=VectorParams(size=core.VECTOR_SIZE, distance=Distance.COSINE),
        )
        print(f"  (created collection '{core.COLLECTION}')")
    for field, schema in COLLECTION_INDEXES.items():
        try:
            core.qdrant.create_payload_index(core.COLLECTION, field, schema)
        except Exception as e:
            if "already exists" not in str(e).lower():
                print(f"  index {field} FAILED: {e}", file=sys.stderr)


PEERS = {
    "mem-fusion-peer-b": {
        "qdrant_url": "http://127.0.0.1:6533",
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


def use_qdrant(url: str):
    """Rebind core's module-level Qdrant client to a target URL.

    Tests run in a single Python process but talk to several Qdrants; this is
    the same rebinding pattern constellation.py uses at boot.
    """
    core.QDRANT_URL = url
    core.qdrant = QdrantClient(url=url, timeout=10)


async def preload_peer(peer_name: str, cfg: dict) -> dict:
    print(f"\n→ {peer_name}  (Qdrant: {cfg['qdrant_url']})")
    use_qdrant(cfg["qdrant_url"])
    ensure_collection()

    results = {"stored": [], "skipped": []}
    for mem in cfg["memories"]:
        snippet = mem["content"][:60].replace("\n", " ") + "..."
        r = await core.store_memory({
            "content":    mem["content"],
            "type":       mem["type"],
            "tags":       mem["tags"],
            "project":    mem["project"],
            "importance": mem["importance"],
        })
        if r.get("status") == "stored":
            print(f"  [store] {r['id'][:8]}…  {snippet}")
            results["stored"].append({"id": r["id"], "content": mem["content"]})
        elif r.get("status") == "duplicate":
            print(f"  [skip ] {r['existing_id'][:8]}…  {snippet}")
            results["skipped"].append({"id": r["existing_id"], "content": mem["content"]})
        else:
            print(f"  [ERR  ] {r}", file=sys.stderr)
            sys.exit(1)
    return results


async def main():
    print("=" * 70)
    print("Preloading test memories into dev peers (via core.store_memory)")
    print("=" * 70)

    try:
        httpx.get(f"{core.OLLAMA_URL}/api/tags", timeout=2.0).raise_for_status()
    except Exception as e:
        print(f"✗ Ollama unreachable at {core.OLLAMA_URL}: {e}", file=sys.stderr)
        sys.exit(1)

    summary = {}
    for peer_name, cfg in PEERS.items():
        try:
            httpx.get(f"{cfg['qdrant_url']}/healthz", timeout=2.0).raise_for_status()
        except Exception as e:
            print(f"✗ {peer_name} Qdrant unreachable at {cfg['qdrant_url']}: {e}", file=sys.stderr)
            continue
        summary[peer_name] = await preload_peer(peer_name, cfg)

    print("\n" + "=" * 70)
    print("Summary")
    print("=" * 70)
    for peer_name, r in summary.items():
        print(f"  {peer_name}:  {len(r['stored'])} stored, {len(r['skipped'])} skipped")

    manifest_path = "/tmp/wp3a-preload-manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\n  Manifest written to {manifest_path}")


if __name__ == "__main__":
    asyncio.run(main())
