#!/usr/bin/env python3
"""
0.5.0-019 chunked embedding — store / search / delete / upsert / find_or_create
+ envelope receive against real Qdrant + Ollama.

Resolves Marketing-flagged bug 581c761d (2026-05-18): 10K-char dense markdown
returned embed_failed despite 0.5.0-007's truncation guard. Chunked embedding
fixes by embedding each ~1500-char slice independently and deduplicating
search results by canonical_id.

Production helpers throughout — no hand-composed envelopes; cross-peer
arrival simulated via origin_node swap.

Usage:
  ~/.local/share/mem-fusion/venv/bin/python tests/mem_fusion/test-chunked-embedding.py
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile
import uuid

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import core                            # noqa: E402
from connectors import get_connector   # noqa: E402

_SLACK = get_connector("slack")


def dense_markdown(n_chars: int) -> str:
    """Marketing-style content: heading + code fence + bullets + table + blockquote.

    Mirrors the token-density profile of the 581c761d bug repro.
    """
    block = (
        "## Heading with **bold** and `inline code` and [link](https://example.com)\n"
        "```python\n"
        "def helper(arg, *, kw=None):\n"
        "    return {'a': 1, 'b': [2, 3, 4]}.get(arg, kw)\n"
        "```\n"
        "- bullet with `code` and **emph**\n"
        "- bullet with [link](https://github.com/example/x/pulls/42) reference\n"
        "\n"
        "| col1 | col2 | col3       |\n"
        "|------|------|------------|\n"
        "| v1   | v2   | description |\n"
        "\n"
        "> blockquote with multiple sentences. Adds dense text that "
        "tokenizes more aggressively than prose.\n\n"
    )
    out = ""
    while len(out) < n_chars:
        out += block
    return out[:n_chars]


def main():
    failures: list[str] = []
    cleanup_ids: list[str] = []
    cleanup_paths: list[str] = []

    def check(name: str, cond: bool, detail: str = ""):
        prefix = "  ✓" if cond else "  ✗"
        print(f"{prefix} {name}{(' — ' + detail) if detail else ''}")
        if not cond:
            failures.append(name)

    project_tag = f"chunked-embed-test-{uuid.uuid4().hex[:8]}"

    # ───────────────────────────────────────────────────────────────────
    # Case 1 — chunk_text helper produces correct slicing
    # ───────────────────────────────────────────────────────────────────
    print("=" * 60)
    print("Case 1: chunk_text helper")
    print("=" * 60)
    short = "short input"
    check("short input → single chunk", core.chunk_text(short) == [short])
    long = "x" * 5000
    chunks = core.chunk_text(long, chunk_chars=1500, overlap=200)
    check("5000-char input → multiple chunks",
          len(chunks) > 1, f"got {len(chunks)} chunks")
    check("each chunk ≤ chunk_chars",
          all(len(c) <= 1500 for c in chunks),
          f"sizes: {[len(c) for c in chunks]}")
    check("first chunk starts at index 0", chunks[0] == long[:1500])
    if len(chunks) > 1:
        # Step = chunk_chars - overlap. Chunk 1 starts at offset 1300.
        check("consecutive chunks overlap by overlap chars",
              chunks[1] == long[1500 - 200:1500 - 200 + 1500])

    # ───────────────────────────────────────────────────────────────────
    # Case 2 — store_memory short content stays single-point
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 2: store_memory short content → single point")
    print("=" * 60)

    async def case2():
        content = f"short memory {uuid.uuid4()}"
        r = await core.store_memory({
            "content": content, "type": "context", "project": project_tag,
        })
        check("short content → status=stored",
              r.get("status") == "stored", f"got: {r}")
        check("short content → no chunk_count in response",
              "chunk_count" not in r,
              f"got: {r.get('chunk_count')}")
        cleanup_ids.append(r["id"])
    asyncio.run(case2())

    # ───────────────────────────────────────────────────────────────────
    # Case 3 — store_memory long dense content → chunked
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 3: store_memory 10K dense markdown → chunked")
    print("=" * 60)

    async def case3():
        content = dense_markdown(10_000)
        r = await core.store_memory({
            "content": content, "type": "context", "project": project_tag,
            "tags": ["case3-chunked"],
        })
        check("10K dense → status=stored",
              r.get("status") == "stored", f"got: {r}")
        check("10K dense → chunk_count > 1",
              r.get("chunk_count", 0) > 1,
              f"got chunk_count={r.get('chunk_count')}")
        cleanup_ids.append(r["id"])

        # Verify canonical chunk has full content + chunked metadata.
        record = await core.export_record({"id": r["id"]})
        check("canonical chunk has full content",
              record.get("content") == content)

        # Verify dependent chunks exist via Qdrant scroll.
        from qdrant_client.http.models import Filter, FieldCondition, MatchValue
        deps, _ = core.qdrant.scroll(
            collection_name=core.COLLECTION,
            scroll_filter=Filter(must=[
                FieldCondition(key="canonical_id", match=MatchValue(value=r["id"])),
            ]),
            limit=64, with_payload=True, with_vectors=False,
        )
        non_canonical = [p for p in deps if str(p.id) != r["id"]]
        check("dependent chunks count = chunk_count - 1",
              len(non_canonical) == r["chunk_count"] - 1,
              f"got {len(non_canonical)}, expected {r['chunk_count'] - 1}")
        check("dependent chunks have chunk_index > 0",
              all((p.payload or {}).get("chunk_index", 0) > 0 for p in non_canonical))
    asyncio.run(case3())

    # ───────────────────────────────────────────────────────────────────
    # Case 4 — search dedupes by canonical_id; returns full content
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 4: search dedupes chunks → one result per memory, full content")
    print("=" * 60)

    async def case4():
        sr = await core.search_memory({
            "query": "helper function returns dictionary",
            "project": project_tag, "top_k": 5,
        })
        check("search returns at least one hit", sr["count"] >= 1)
        # The one chunked memory in this project should dedupe to a single result.
        ids = [r["id"] for r in sr["results"]]
        check("no duplicate ids in results",
              len(ids) == len(set(ids)),
              f"ids: {ids}")
        if sr["results"]:
            top = sr["results"][0]
            check("top result content starts with the source heading",
                  top["content"].startswith("## Heading"),
                  f"got: {top['content'][:40]!r}")
    asyncio.run(case4())

    # ───────────────────────────────────────────────────────────────────
    # Case 5 — find_or_create on long content → created (chunked)
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 5: find_or_create 12K → status=created, chunked")
    print("=" * 60)

    async def case5():
        pj = project_tag + "-foc"
        # Use completely-different prose so find_or_create's 0.82 semantic
        # threshold can't match Case 3's dense_markdown content. The whole
        # content drives the embedding; a few unique seed chars on a sea of
        # similar markdown still produces high similarity.
        unique_topic = (
            f"Quantum entanglement experiment log {uuid.uuid4()}. "
            "Today we calibrated the photon detector array and measured "
            "Bell inequality violations at three angles. Detector A "
            "registered 1247 coincidences in the 22.5-degree configuration. "
            "Detector B saw 943 in the 67.5-degree configuration. "
        )
        content = ""
        while len(content) < 12_000:
            content += unique_topic
        content = content[:12_000]
        r = await core.find_or_create({
            "content": content, "type": "context", "project": pj,
        })
        check("find_or_create on novel long content → created",
              r.get("status") == "created", f"got: {r}")
        check("created memory has chunk_count > 1",
              r.get("chunk_count", 0) > 1)
        if r.get("id"):
            cleanup_ids.append(r["id"])
    asyncio.run(case5())

    # ───────────────────────────────────────────────────────────────────
    # Case 6 — cross-peer arrival of long content (envelope receive)
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 6: store_memory_from_envelope chunks long incoming content")
    print("=" * 60)

    async def case6():
        # Stand up a temp connector.json
        fd, cfg_path = tempfile.mkstemp(suffix=".json")
        cleanup_paths.append(cfg_path)
        with os.fdopen(fd, "w") as f:
            json.dump({"connectors": [
                {"id": "test", "type": "slack", "channel": "C0B3VGB3RF1"},
            ]}, f)
        core.CONNECTORS_CONFIG_PATH = pathlib.Path(cfg_path)

        # Prose content (no triple-backticks) — the SlackConnector wire-format
        # parser uses string-find on ``` so content containing markdown code
        # fences would collapse the envelope parse. Existing limitation,
        # separate from chunking; tracked for follow-up.
        prose_block = (
            "This is a long prose section about chunked embedding. "
            "The receive flow on this peer should chunk the content "
            "into multiple Qdrant points sharing a canonical_id. "
            "Each chunk gets its own vector via nomic-embed-text. "
            "Search later dedupes by canonical_id and returns the "
            "canonical chunk's payload, which carries the full content. "
        )
        content = ""
        while len(content) < 11_000:
            content += prose_block
        content = content[:11_000]
        sr = await core.store_memory({
            "content": content, "type": "context",
            "project": project_tag + "-rcv", "connector_ids": ["test"],
        })
        record = await core.export_record({"id": sr["id"]})
        # Simulate cross-peer arrival
        record["origin_node"] = f"fake-marketing-peer-{uuid.uuid4().hex[:8]}"
        env = _SLACK.build_envelope_from_record(record, "test")
        # Drop the vector so receiver re-chunks
        env.pop("vector", None)
        body = _SLACK.format_envelope(env)
        parsed = _SLACK.parse_envelope(body)
        # Delete local source so receive truly stores
        await core.delete_memory({"id": sr["id"]})

        r = await core.store_memory_from_envelope(parsed, "slack")
        check("envelope receive on long content → stored",
              r.get("status") == "stored", f"got: {r}")
        check("envelope receive chunked → chunk_count > 1",
              r.get("chunk_count", 0) > 1)
        if r.get("id"):
            cleanup_ids.append(r["id"])
    asyncio.run(case6())

    # ───────────────────────────────────────────────────────────────────
    # Case 7 — upsert on long content re-chunks cleanly
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 7: upsert_memory on chunked memory re-chunks")
    print("=" * 60)

    async def case7():
        # Create a long memory, then upsert with different long content
        pj = project_tag + "-upsert"
        c1 = dense_markdown(8_000)
        r1 = await core.store_memory({"content": c1, "type": "context", "project": pj})
        check("upsert: initial 8K → chunked", r1.get("chunk_count", 0) > 1)
        original_id = r1["id"]

        c2 = dense_markdown(11_000) + "  REVISED"
        r2 = await core.upsert_memory({"id": original_id, "content": c2})
        # Upsert with re-chunk replaces the canonical id (documented in code).
        check("upsert with re-chunk returns new id (canonical_id change)",
              r2.get("status") == "stored"
              and r2.get("id") != original_id
              and r2.get("chunk_count", 0) > 1,
              f"got: {r2}")
        if r2.get("id"):
            cleanup_ids.append(r2["id"])

        # Verify old chunks are gone
        from qdrant_client.http.models import Filter, FieldCondition, MatchValue
        old_chunks, _ = core.qdrant.scroll(
            collection_name=core.COLLECTION,
            scroll_filter=Filter(must=[
                FieldCondition(key="canonical_id", match=MatchValue(value=original_id)),
            ]),
            limit=64, with_payload=False, with_vectors=False,
        )
        check("upsert removed old chunks",
              len(old_chunks) == 0,
              f"got {len(old_chunks)} stale chunks")
    asyncio.run(case7())

    # ───────────────────────────────────────────────────────────────────
    # Case 8 — delete_memory removes all chunks
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 8: delete_memory removes all chunks of a chunked memory")
    print("=" * 60)

    async def case8():
        c = dense_markdown(9_000)
        r = await core.store_memory({
            "content": c, "type": "context", "project": project_tag + "-del",
        })
        chunk_count = r.get("chunk_count", 1)
        check("setup: stored as chunked", chunk_count > 1)
        d = await core.delete_memory({"id": r["id"]})
        check("delete reports correct chunks_deleted",
              d.get("chunks_deleted") == chunk_count,
              f"got {d.get('chunks_deleted')}, expected {chunk_count}")
    asyncio.run(case8())

    # ───────────────────────────────────────────────────────────────────
    # Cleanup
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Cleanup")
    print("=" * 60)

    async def cleanup_async():
        # Delete each canonical id (cascades to chunks via the production path)
        for mid in cleanup_ids:
            try:
                await core.delete_memory({"id": mid})
            except Exception:
                pass
    asyncio.run(cleanup_async())
    print(f"  ✓ cleaned up {len(cleanup_ids)} canonical ids")
    for p in cleanup_paths:
        if os.path.exists(p):
            os.unlink(p)
    print(f"  ✓ removed {len(cleanup_paths)} temp config files")

    print()
    print("=" * 60)
    if failures:
        print(f"FAILED ({len(failures)}): {', '.join(failures)}")
        sys.exit(1)
    print("ALL CHUNKED-EMBEDDING TESTS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
