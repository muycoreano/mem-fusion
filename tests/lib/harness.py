"""
Shared test harness — Qdrant + Constellation lifecycle helpers.

Each helper is a self-contained primitive. Tests compose them as needed:

  start_qdrant(port, storage_dir, log_path)
      → spawn Qdrant subprocess, wait for healthz, return Popen

  init_cowork_memories(port)
      → create the `cowork_memories` collection + payload indexes on the
        Qdrant at `port`, idempotent

  write_constellation_config(path, ...)
      → write a Constellation config.json

  start_constellation(config_path, log_path)
      → spawn Constellation daemon subprocess, return Popen

  wait_health(port, timeout_s)
      → poll http://127.0.0.1:<port>/health until 200 or timeout

  check(label, condition, detail)
      → print a ✓ / ✗ line, return the bool (for the
        `passed.append(check(...))` pattern)

Importing tests:

    REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(REPO_ROOT / "src"))
    sys.path.insert(0, str(REPO_ROOT / "tests" / "lib"))
    import core, harness
"""
import json
import os
import pathlib
import subprocess
import time

import httpx
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, PayloadSchemaType, VectorParams,
)


REPO_ROOT     = pathlib.Path(__file__).resolve().parents[2]
QDRANT_BIN    = pathlib.Path.home() / ".local/share/mem-fusion/bin/qdrant"
VENV_PYTHON   = pathlib.Path.home() / ".local/share/mem-fusion/venv/bin/python"
CONSTELLATION = REPO_ROOT / "src/constellation.py"
MEM_FUSION    = REPO_ROOT / "src/mem_fusion.py"
COLLECTION    = "cowork_memories"
VECTOR_SIZE   = 768


def start_qdrant(http_port: int, storage_dir: pathlib.Path,
                 log_path: pathlib.Path, *, grpc_port: int | None = None,
                 timeout_s: float = 15.0) -> subprocess.Popen:
    """Start a Qdrant subprocess on `http_port` with storage under `storage_dir`.
    gRPC port defaults to `http_port + 1`. Polls /healthz until ready.
    """
    cfg_path = storage_dir / "qdrant.yaml"
    cfg_path.write_text(
        f"storage:\n"
        f"  storage_path: {storage_dir}/data\n"
        f"service:\n"
        f"  host: 127.0.0.1\n"
        f"  http_port: {http_port}\n"
        f"  grpc_port: {grpc_port or http_port + 1}\n"
        f"  enable_cors: false\n"
        f"log_level: WARN\n"
    )
    proc = subprocess.Popen(
        [str(QDRANT_BIN), "--config-path", str(cfg_path)],
        stdout=open(log_path, "w"), stderr=subprocess.STDOUT,
    )
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{http_port}/healthz", timeout=1.0)
            if r.status_code == 200:
                return proc
        except Exception:
            pass
        time.sleep(0.3)
    proc.terminate()
    raise RuntimeError(f"Qdrant on :{http_port} failed to start; see {log_path}")


def init_cowork_memories(qdrant_port: int) -> None:
    """Create the cowork_memories collection + payload indexes idempotently."""
    client = QdrantClient(url=f"http://127.0.0.1:{qdrant_port}", timeout=10)
    try:
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
    except Exception:
        pass
    indexes = {
        "type":         PayloadSchemaType.KEYWORD,
        "project":      PayloadSchemaType.KEYWORD,
        "groups":       PayloadSchemaType.KEYWORD,
        "source":       PayloadSchemaType.KEYWORD,  # legacy v0.3
        "session_id":   PayloadSchemaType.KEYWORD,
        "content_hash": PayloadSchemaType.KEYWORD,
        "tags":         PayloadSchemaType.KEYWORD,
        "group_name":   PayloadSchemaType.KEYWORD,  # legacy v0.3
        "origin_node":  PayloadSchemaType.KEYWORD,
        "importance":   PayloadSchemaType.INTEGER,
        "timestamp":    PayloadSchemaType.DATETIME,
        "submitted_at": PayloadSchemaType.DATETIME,
        "received_at":  PayloadSchemaType.DATETIME,
    }
    for field, schema in indexes.items():
        try:
            client.create_payload_index(COLLECTION, field, schema)
        except Exception:
            pass


def write_constellation_config(path: pathlib.Path, *,
                               node_name: str,
                               peer_port: int,
                               gateway_port: int,
                               qdrant_port: int,
                               state_dir: pathlib.Path,
                               group_name: str | None = None,
                               peers: list[dict] | None = None,
                               memberships: list[dict] | None = None) -> None:
    """Write a constellation daemon config.json.

    Either pass `memberships` (a list of {group_name, peers} dicts — v0.4
    multi-group form) or the singular `group_name` + `peers` shorthand for
    one-membership tests. `peers` defaults to empty list.
    """
    if memberships is None:
        if group_name is None:
            raise ValueError("write_constellation_config: provide either "
                             "memberships=[...] or group_name=...")
        memberships = [{"group_name": group_name, "peers": peers or []}]
    cfg = {
        "node_name":              node_name,
        "peer_listen_address":    f"127.0.0.1:{peer_port}",
        "gateway_listen_address": f"127.0.0.1:{gateway_port}",
        "qdrant_url":             f"http://127.0.0.1:{qdrant_port}",
        "state_dir":              str(state_dir),
        "memberships":            memberships,
    }
    path.write_text(json.dumps(cfg, indent=2))


def start_constellation(config_path: pathlib.Path,
                        log_path: pathlib.Path) -> subprocess.Popen:
    """Spawn a constellation daemon subprocess."""
    return subprocess.Popen(
        [str(VENV_PYTHON), str(CONSTELLATION),
         "--config", str(config_path), "--foreground"],
        stdout=open(log_path, "w"), stderr=subprocess.STDOUT,
    )


def wait_health(port: int, timeout_s: float = 15.0) -> bool:
    """Poll http://127.0.0.1:<port>/health until 200 or timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def check(label: str, condition: bool, detail: str = "") -> bool:
    """Print a test assertion result. Returns the bool for use with
    `passed.append(check(...))`."""
    mark = "✓" if condition else "✗"
    extra = f" — {detail}" if (detail and not condition) else ""
    print(f"  {mark} {label}{extra}")
    return condition


def stop_proc(proc: subprocess.Popen | None, timeout_s: float = 3.0) -> None:
    """Best-effort graceful shutdown of a subprocess."""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()


# ── Fake-HOME staging for hook-script tests ───────────────────────────────
def stage_mem_fusion_home(parent: pathlib.Path) -> pathlib.Path:
    """Build a $HOME-shaped layout under `parent/fake_home` so hook scripts
    (which hardcode $HOME/.local/share/mem-fusion/...) can run in isolation.

    Returns the staged HOME path. Inside it:
      $HOME/.local/share/mem-fusion/core.py        ← copy from src/
      $HOME/.local/share/mem-fusion/mem_fusion.py  ← copy from src/
      $HOME/.local/share/mem-fusion/scripts/*      ← copy from src/scripts/
      $HOME/.local/share/mem-fusion/venv           ← symlink to shared test venv
      $HOME/.local/share/mem-fusion/{queue,logs}   ← mkdir
      $HOME/.claude/sessions/                       ← mkdir (used by Stop hook)
    """
    home = parent / "fake_home"
    mf   = home / ".local/share/mem-fusion"
    mf.mkdir(parents=True)
    (mf / "scripts").mkdir()
    (mf / "queue").mkdir()
    (mf / "logs").mkdir()
    (home / ".claude/sessions").mkdir(parents=True)

    for fname in ("core.py", "mem_fusion.py"):
        (mf / fname).write_bytes((REPO_ROOT / "src" / fname).read_bytes())
    for src in (REPO_ROOT / "src/scripts").glob("*"):
        if not src.is_file():
            continue
        dst = mf / "scripts" / src.name
        dst.write_bytes(src.read_bytes())
        dst.chmod(0o755)

    venv_src = pathlib.Path.home() / ".local/share/mem-fusion/venv"
    (mf / "venv").symlink_to(venv_src)
    return home


def run_hook(script_path: pathlib.Path, *,
             fake_home: pathlib.Path, qdrant_port: int,
             stdin: str = "", env_extra: dict | None = None,
             timeout_s: float = 30.0) -> tuple[int, str, str]:
    """Run a hook script in a fake-HOME subprocess. Returns (rc, stdout, stderr)."""
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["QDRANT_URL"] = f"http://127.0.0.1:{qdrant_port}"
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [str(script_path)], input=stdin, env=env,
        capture_output=True, text=True, timeout=timeout_s,
    )
    return proc.returncode, proc.stdout, proc.stderr


# ── Mem-Fusion MCP subprocess + JSON-RPC client ───────────────────────────
def start_mem_fusion(qdrant_port: int, stderr_path: pathlib.Path,
                     *, gateway_url: str | None = None,
                     node_name: str | None = None) -> subprocess.Popen:
    """Spawn `mem_fusion.py` as an MCP stdio subprocess.

    `qdrant_port` becomes the QDRANT_URL the subprocess will use.
    `gateway_url`, if given, becomes MEMFUSION_CONSTELLATION_GATEWAY — used
    by mem-fusion's group_pull/group_push tools to find the local
    Constellation daemon. Omit to test graceful-degradation behavior.
    `node_name`, if given, becomes MEMFUSION_NODE_NAME — the local peer's
    identity, written into `origin_node` on locally-originated entries.
    Multi-peer tests must set this per peer so each subprocess doesn't fall
    back to the shared hostname.
    """
    env = os.environ.copy()
    env["QDRANT_URL"] = f"http://127.0.0.1:{qdrant_port}"
    if gateway_url is not None:
        env["MEMFUSION_CONSTELLATION_GATEWAY"] = gateway_url
    if node_name is not None:
        env["MEMFUSION_NODE_NAME"] = node_name
    return subprocess.Popen(
        [str(VENV_PYTHON), str(MEM_FUSION)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=open(stderr_path, "w"),
        env=env, bufsize=0,
    )


class MCPClient:
    """Minimal newline-delimited JSON-RPC client for an MCP stdio server."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self._next_id = 0

    def _send(self, msg: dict) -> None:
        line = (json.dumps(msg) + "\n").encode()
        self.proc.stdin.write(line)
        self.proc.stdin.flush()

    def _read_response(self, expected_id: int) -> dict:
        # Skip notifications/unrelated messages until we get a matching id.
        while True:
            raw = self.proc.stdout.readline()
            if not raw:
                raise RuntimeError("mem_fusion stdout closed unexpectedly")
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

    def notify(self, method: str, params: dict | None = None) -> None:
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        self._send(body)

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Send tools/call and unwrap the double-JSON-encoded tool result.

        MCP wraps tool output as TextContent(text=json.dumps(result)); the
        actual tool dict is at response.result.content[0].text, JSON-encoded.
        """
        r = self.request("tools/call", {"name": name, "arguments": arguments})
        if "error" in r:
            raise RuntimeError(f"tool {name} JSON-RPC error: {r['error']}")
        text = r["result"]["content"][0]["text"]
        return json.loads(text)


def mcp_initialize(client: MCPClient, client_name: str = "test-harness") -> dict:
    """Perform the standard MCP handshake: initialize + notifications/initialized.
    Returns the initialize response. Raises if it errors."""
    r = client.request("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": client_name, "version": "1.0"},
    })
    if "error" in r:
        raise RuntimeError(f"MCP initialize failed: {r['error']}")
    client.notify("notifications/initialized")
    return r
