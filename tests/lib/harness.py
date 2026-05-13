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
import pathlib
import subprocess
import time

import httpx
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, PayloadSchemaType, VectorParams,
)


REPO_ROOT     = pathlib.Path(__file__).resolve().parents[2]
QDRANT_BIN    = pathlib.Path.home() / ".local/share/cowork-memory/bin/qdrant"
VENV_PYTHON   = pathlib.Path.home() / ".local/share/cowork-memory/venv/bin/python"
CONSTELLATION = REPO_ROOT / "src/constellation.py"
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
        "source":       PayloadSchemaType.KEYWORD,
        "session_id":   PayloadSchemaType.KEYWORD,
        "content_hash": PayloadSchemaType.KEYWORD,
        "tags":         PayloadSchemaType.KEYWORD,
        "group_name":   PayloadSchemaType.KEYWORD,
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
                               group_name: str,
                               peers: list[dict] | None = None) -> None:
    """Write a constellation daemon config.json. `peers` is a list of
    {node_name, endpoint} dicts; defaults to empty list."""
    cfg = {
        "node_name":              node_name,
        "peer_listen_address":    f"127.0.0.1:{peer_port}",
        "gateway_listen_address": f"127.0.0.1:{gateway_port}",
        "qdrant_url":             f"http://127.0.0.1:{qdrant_port}",
        "state_dir":              str(state_dir),
        "memberships": [{
            "group_name": group_name,
            "peers":      peers or [],
        }],
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
