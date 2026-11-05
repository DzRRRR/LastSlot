"""Fixtures communes.

Les tests marqués `requires_db` exigent TEST_DATABASE_URL, une base PostgreSQL + PostGIS
JETABLE : le seed y vide toutes les tables (TRUNCATE). Les autres tests tournent sans base.
"""
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.dbutil import TEST_DATABASE_URL

BACKEND_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Seed complet via la CLI, une fois par session."""
    tokens = tmp_path_factory.mktemp("seed") / "demo-tokens.csv"
    proc = subprocess.run(
        [sys.executable, "-m", "app.seed", "--reset", "--tokens-out", str(tokens),
         "--base-url", "http://demo.test"],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return {"tokens": tokens, "stdout": proc.stdout}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def server(seeded: dict[str, Any], tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Vrai uvicorn en sous-processus : les clients de test ASGI tamponnent la réponse
    entière, inutilisable pour un flux SSE infini."""
    port = _free_port()
    log_path = tmp_path_factory.mktemp("server") / "uvicorn.log"
    env = {
        **os.environ,
        "DATABASE_URL": TEST_DATABASE_URL,
        "CSP_ENABLED": "true",
        "EXPIRY_INTERVAL_SECONDS": "0.2",
        "SSE_HEARTBEAT_SECONDS": "0.5",
    }
    with log_path.open("w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(port), "--workers", "1", "--timeout-graceful-shutdown", "1"],
            cwd=BACKEND_DIR, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    while True:
        try:
            if httpx.get(f"{base_url}/api/config", timeout=1, trust_env=False).status_code == 200:
                break
        except httpx.TransportError:
            pass
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.kill()
            pytest.fail(f"uvicorn n'a pas démarré :\n{log_path.read_text()}")
        time.sleep(0.1)

    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("uvicorn ne s'est pas arrêté en 10 s")
    log_text = log_path.read_text()
    assert "Traceback" not in log_text, log_text


@pytest.fixture
def client(server: str) -> Iterator[httpx.Client]:
    # trust_env=False : ignore d'éventuelles variables HTTP(S)_PROXY de l'environnement.
    with httpx.Client(base_url=server, timeout=5, trust_env=False) as http:
        yield http
