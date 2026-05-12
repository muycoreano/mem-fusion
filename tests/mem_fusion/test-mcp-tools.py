#!/usr/bin/env python3
"""
mem-fusion MCP integration test.

Stands up an isolated Qdrant (on a dedicated port, in a temp dir), launches
mem_fusion.py as its MCP subprocess (stdio JSON-RPC transport), and walks
through all 9 MCP tools end-to-end. No interaction with peer Qdrants, no
constellation involvement — this exercises mem_fusion.py's actual deployment
shape as Claude would invoke it.

Lifecycle:
  1. Spin up Qdrant on :6733 with a tempdir storage path
  2. Init the cowork_memories collection on it
  3. Spawn mem_fusion.py with QDRANT_URL pointed at the test Qdrant
  4. Perform MCP initialize handshake over stdio
  5. Exercise: store_memory, search_memory, search_recent, find_or_create,
                upsert_memory, get_related, export_record, memory_stats,
                delete_memory
  6. Tear down (kill mem_fusion, kill Qdrant, drop tempdir)

Usage:
  ~/.local/share/cowork-memory/venv/bin/python tests/mem_fusion/test-mcp-tools.py
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

import httpx
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PayloadSchemaType, VectorParams

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
import core  # noqa: E402


# ── Config ─────────────────────────────────────────────────────────────────
QDRANT_PORT     = 6733
QDRANT_GRPC     = 6734
QDRANT_URL      = f"http://127.0.0.1:{QDRANT_PORT}"
QDRANT_BINARY   = pathlib.Path.home() / ".local/share/cowork-memory/bin/qdrant"
MEM_FUSION_PY   = REPO_ROOT / "src/mem_fusion.py"
VENV_PYTHON     = pathlib.Path.home() / ".local/share/cowork-memory/venv/bin/python"

# Tool list expected from mem_fusion.py — kept in sync with the 9 tools
# registered in src/mem_fusion.py.
EXPECTED_TOOLS = {
    "store_memory", "search_memory", "search_recent", "upsert_memory",
    "find_or_create", "delete_memory", "get_related", "memory_stats",
    "export_record",
}


# ── MCP stdio JSON-RPC client ─────────────────────────────────────────────
class MCPClient:
    """Minimal newline-delimited JSON-RPC client for an MCP stdio server."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self._next_id = 0

    def _send(self, msg: dict):
        line = (json.dumps(msg) + "\n").encode()
        self.proc.stdin.write(line)
        self.proc.stdin.flush()

    def _read_response(self, expected_id: int) -> dict:
        # Skip notifications/unrelated messages until we get a matching id.
        # The MCP server may emit notifications during initialize; we ignore
        # them here since this test is request/response shaped.
        while True:
            raw = self.proc.stdout.readline()
            if not raw:
                err = self.proc.stderr.read().decode() if self.proc.stderr else ""
                raise RuntimeError(f"mem_fusion stdout closed; stderr: {err}")
            msg = json.loads(raw)
            if msg.get("id") == expected_id:
                return msg

    def request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        rid = self._next_id
        body = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            body["params"] = params
        self._send(body)
        return self._read_response(rid)

    def notify(self, method: str, params: dict | None = None):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        self._send(body)

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Send tools/call and unwrap the double-JSON-encoded tool result.

        MCP wraps tool output as TextContent(text=json.dumps(result)), so the
        actual tool dict is at response.result.content[0].text, JSON-encoded.
        """
        r = self.request("tools/call", {"name": name, "arguments": arguments})
        if "error" in r:
            raise RuntimeError(f"tool {name} JSON-RPC error: {r['error']}")
        text = r["result"]["content"][0]["text"]
        return json.loads(text)


# ── Lifecycle helpers ─────────────────────────────────────────────────────
def start_qdrant(storage_dir: pathlib.Path, log_path: pathlib.Path) -> subprocess.Popen:
    config_path = storage_dir / "qdrant-config.yaml"
    config_path.write_text(
        f"storage:\n"
        f"  storage_path: {storage_dir}/data\n"
        f"service:\n"
        f"  host: 127.0.0.1\n"
        f"  http_port: {QDRANT_PORT}\n"
        f"  grpc_port: {QDRANT_GRPC}\n"
        f"  enable_cors: false\n"
        f"log_level: WARN\n"
    )
    log_file = open(log_path, "w")
    proc = subprocess.Popen(
        [str(QDRANT_BINARY), "--config-path", str(config_path)],
        stdout=log_file, stderr=log_file,
    )
    for _ in range(20):
        try:
            if httpx.get(f"{QDRANT_URL}/healthz", timeout=1.0).status_code == 200:
                return proc
        except Exception:
            pass
        time.sleep(0.5)
    proc.terminate()
    raise RuntimeError(f"Qdrant failed to start; see {log_path}")


def init_collection():
    """Create cowork_memories on the test Qdrant with the indexes core expects."""
    client = QdrantClient(url=QDRANT_URL, timeout=10)
    existing = [c.name for c in client.get_collections().collections]
    if core.COLLECTION not in existing:
        client.create_collection(
            collection_name=core.COLLECTION,
            vectors_config=VectorParams(size=core.VECTOR_SIZE, distance=Distance.COSINE),
        )
    indexes = {
        "type":         PayloadSchemaType.KEYWORD,
        "project":      PayloadSchemaType.KEYWORD,
        "source":       PayloadSchemaType.KEYWORD,
        "session_id":   PayloadSchemaType.KEYWORD,
        "content_hash": PayloadSchemaType.KEYWORD,
        "tags":         PayloadSchemaType.KEYWORD,
        "importance":   PayloadSchemaType.INTEGER,
        "timestamp":    PayloadSchemaType.DATETIME,
    }
    for field, schema in indexes.items():
        try:
            client.create_payload_index(core.COLLECTION, field, schema)
        except Exception as e:
            if "already exists" not in str(e).lower():
                raise


def start_mem_fusion(stderr_path: pathlib.Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["QDRANT_URL"] = QDRANT_URL
    stderr_file = open(stderr_path, "w")
    return subprocess.Popen(
        [str(VENV_PYTHON), str(MEM_FUSION_PY)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr_file,
        env=env, bufsize=0,
    )


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "✓" if condition else "✗"
    extra = f" — {detail}" if (detail and not condition) else ""
    print(f"  {mark} {label}{extra}")
    return condition


# ── Test body ─────────────────────────────────────────────────────────────
def run_tests(mcp: MCPClient) -> int:
    passed: list[bool] = []

    print("\nSTEP 3: MCP initialize handshake")
    r = mcp.request("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "mem-fusion-test", "version": "1.0"},
    })
    passed.append(check("initialize returned a result", "result" in r))
    passed.append(check("server identifies as mem-fusion",
                        r["result"]["serverInfo"]["name"] == "mem-fusion",
                        f"got {r['result'].get('serverInfo')}"))
    mcp.notify("notifications/initialized")

    print("\nSTEP 4: tools/list returns all 9 tools")
    r = mcp.request("tools/list", {})
    tool_names = {t["name"] for t in r["result"]["tools"]}
    passed.append(check(f"all 9 expected tools present ({len(tool_names)} total)",
                        EXPECTED_TOOLS.issubset(tool_names),
                        f"missing: {EXPECTED_TOOLS - tool_names}"))

    print("\nSTEP 5: memory_stats on empty collection")
    stats = mcp.call_tool("memory_stats", {})
    passed.append(check("stats: total_memories == 0", stats["total_memories"] == 0,
                        f"got {stats.get('total_memories')}"))
    passed.append(check("stats: collection == cowork_memories",
                        stats["collection"] == "cowork_memories"))

    print("\nSTEP 6: store_memory — store first memory")
    paris = "The capital of France is Paris, the largest city in the country."
    r1 = mcp.call_tool("store_memory", {
        "content": paris, "type": "fact",
        "tags": ["geography"], "project": "mcp-test", "importance": 3,
    })
    passed.append(check("store: status == 'stored'", r1.get("status") == "stored",
                        f"got {r1}"))
    paris_id = r1.get("id")
    passed.append(check("store: returns an id", bool(paris_id)))

    print("\nSTEP 7: store_memory — store second, different memory")
    qm = "Quantum mechanics describes wave-particle duality at subatomic scales."
    r2 = mcp.call_tool("store_memory", {
        "content": qm, "type": "fact",
        "tags": ["physics"], "project": "mcp-test", "importance": 3,
    })
    qm_id = r2.get("id")
    passed.append(check("store: second memory stored", r2.get("status") == "stored"))

    print("\nSTEP 8: store_memory — duplicate content returns 'duplicate'")
    r3 = mcp.call_tool("store_memory", {"content": paris, "type": "fact"})
    passed.append(check("duplicate detected", r3.get("status") == "duplicate"))
    passed.append(check("duplicate returns existing_id matching original",
                        r3.get("existing_id") == paris_id))

    print("\nSTEP 9: memory_stats reflects 2 stored")
    stats = mcp.call_tool("memory_stats", {})
    passed.append(check("stats: total_memories == 2",
                        stats["total_memories"] == 2,
                        f"got {stats['total_memories']}"))
    passed.append(check("stats: by_type['fact'] == 2",
                        stats["by_type"].get("fact") == 2))

    print("\nSTEP 10: search_memory — semantic query for 'France'")
    r = mcp.call_tool("search_memory", {"query": "What is the capital of France?", "top_k": 5})
    passed.append(check("search: at least 1 result", r["count"] >= 1))
    top = r["results"][0]
    passed.append(check(f"search: top result is paris (score={top['score']})",
                        top["id"] == paris_id))
    passed.append(check("search: top score > 0.75", top["score"] > 0.75,
                        f"score={top['score']}"))

    print("\nSTEP 11: search_memory with project filter")
    r = mcp.call_tool("search_memory", {
        "query": "physics", "top_k": 5, "project": "mcp-test",
    })
    passed.append(check("filtered search returns results", r["count"] >= 1))

    print("\nSTEP 12: export_record — full record with 768-dim vector")
    rec = mcp.call_tool("export_record", {"id": paris_id})
    passed.append(check("export: id matches", rec["id"] == paris_id))
    passed.append(check("export: content matches", rec["content"] == paris))
    passed.append(check("export: vector len == 768",
                        isinstance(rec["vector"], list) and len(rec["vector"]) == 768))
    passed.append(check("export: source == 'local'", rec["source"] == "local"))

    print("\nSTEP 13: find_or_create — same content returns 'found'")
    r = mcp.call_tool("find_or_create", {
        "content": paris, "type": "fact", "project": "mcp-test",
    })
    passed.append(check("find_or_create: status == 'found'",
                        r.get("status") == "found", f"got {r}"))

    print("\nSTEP 14: find_or_create — new content creates")
    novel = "The mitochondrion is the powerhouse of the cell."
    r = mcp.call_tool("find_or_create", {
        "content": novel, "type": "fact", "project": "mcp-test",
    })
    passed.append(check("find_or_create: status == 'created'",
                        r.get("status") == "created", f"got {r}"))
    mito_id = r.get("id")

    print("\nSTEP 15: get_related — find memories similar to paris")
    r = mcp.call_tool("get_related", {"memory_id": paris_id, "top_k": 5})
    passed.append(check("get_related: returns related list",
                        "results" in r and r["reference_id"] == paris_id))

    print("\nSTEP 16: upsert_memory — update content of paris")
    new_content = "Paris is the capital city of France and home to the Eiffel Tower."
    r = mcp.call_tool("upsert_memory", {
        "id": paris_id, "content": new_content, "importance": 4,
    })
    passed.append(check("upsert: status == 'updated'", r.get("status") == "updated"))
    # Verify via export
    rec = mcp.call_tool("export_record", {"id": paris_id})
    passed.append(check("upsert: content was rewritten",
                        rec["content"] == new_content,
                        f"got {rec['content'][:40]!r}"))
    passed.append(check("upsert: content_hash changed",
                        rec["content_hash"] == core.content_hash(new_content)))

    print("\nSTEP 17: search_recent — recent memories")
    r = mcp.call_tool("search_recent", {"hours": 1, "top_k": 10})
    passed.append(check(f"search_recent: 3 entries (got {r['count']})",
                        r["count"] == 3))

    print("\nSTEP 18: delete_memory — remove quantum-mechanics entry")
    r = mcp.call_tool("delete_memory", {"id": qm_id})
    passed.append(check("delete: status == 'deleted'", r.get("status") == "deleted"))

    print("\nSTEP 19: memory_stats reflects deletion")
    stats = mcp.call_tool("memory_stats", {})
    passed.append(check(f"stats: total_memories == 2 after delete (got {stats['total_memories']})",
                        stats["total_memories"] == 2))

    print("\nSTEP 20: delete_memory — remove mito entry")
    r = mcp.call_tool("delete_memory", {"id": mito_id})
    passed.append(check("delete: status == 'deleted'", r.get("status") == "deleted"))

    return sum(passed), len(passed)


def main():
    print("=" * 70)
    print("mem-fusion MCP integration — 9 tools via stdio JSON-RPC")
    print("=" * 70)

    if not QDRANT_BINARY.exists():
        print(f"✗ Qdrant binary not found at {QDRANT_BINARY}", file=sys.stderr)
        sys.exit(1)
    if not MEM_FUSION_PY.exists():
        print(f"✗ mem_fusion.py not found at {MEM_FUSION_PY}", file=sys.stderr)
        sys.exit(1)

    qdrant_proc = None
    mcp_proc = None

    with tempfile.TemporaryDirectory(prefix="mem-fusion-mcp-test-") as tdir:
        tmp = pathlib.Path(tdir)
        try:
            print("\nSTEP 1: spin up Qdrant on :%d" % QDRANT_PORT)
            qdrant_log = tmp / "qdrant.log"
            qdrant_proc = start_qdrant(tmp, qdrant_log)
            print(f"  ✓ Qdrant ready (pid {qdrant_proc.pid}, log {qdrant_log})")

            core.QDRANT_URL = QDRANT_URL
            core.qdrant = QdrantClient(url=QDRANT_URL, timeout=10)
            init_collection()
            print(f"  ✓ collection '{core.COLLECTION}' initialized")

            print("\nSTEP 2: spawn mem_fusion.py")
            mcp_stderr = tmp / "mem-fusion.stderr"
            mcp_proc = start_mem_fusion(mcp_stderr)
            print(f"  ✓ mem-fusion subprocess started (pid {mcp_proc.pid}, stderr {mcp_stderr})")

            mcp = MCPClient(mcp_proc)
            ok, total = run_tests(mcp)
            print(f"\n  {ok}/{total} invariants passed")

            if ok != total:
                print("\n✗ MCP integration FAILED", file=sys.stderr)
                if mcp_stderr.exists():
                    print(f"--- mem-fusion stderr ({mcp_stderr}) ---", file=sys.stderr)
                    print(mcp_stderr.read_text(), file=sys.stderr)
                sys.exit(1)

            print("\n" + "=" * 70)
            print("✓ MCP integration PASSED")
            print("=" * 70)

        finally:
            if mcp_proc is not None and mcp_proc.poll() is None:
                print("\n[teardown] stopping mem-fusion subprocess")
                mcp_proc.terminate()
                try:
                    mcp_proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    mcp_proc.kill()
            if qdrant_proc is not None and qdrant_proc.poll() is None:
                print("[teardown] stopping Qdrant")
                qdrant_proc.terminate()
                try:
                    qdrant_proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    qdrant_proc.kill()


if __name__ == "__main__":
    main()
