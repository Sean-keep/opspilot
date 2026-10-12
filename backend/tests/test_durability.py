"""A 2xx means the write is already durable.

``TestClient`` cannot see this class of bug. It drives the ASGI application to
completion — including the dependency exit stack — before it hands the response
back to the test, so a commit tucked into a ``yield`` dependency's teardown is
always "in time" as far as a test is concerned, and never in time for a real
client.

Over a socket the ordering is the opposite. ``fastapi.routing.request_response``
sends the response and only then unwinds the stack that owns ``get_db``, so a
``201 Created`` can reach the browser before the row is committed. The next
request reads a database that does not have the row yet and answers ``404``.

**Isolation level is load-bearing here.** The shared ``db`` fixture pins the
application at ``READ COMMITTED`` so assertions can see rows mid-transaction.
Production does not: MySQL's default is ``REPEATABLE READ``, and under that
default a transaction that opened before the commit keeps its old snapshot — so
the late commit is invisible to the request that lost the race, and only
*subsequent* requests see the row. That is exactly the shape of the original
bug: create-then-read 404s, a moment later the row is there. These tests
therefore run against a **production-shaped engine** (no isolation override),
and that is why they catch what the other 648 could not.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from backend.tests.conftest import BOOTSTRAP_ADMIN, BOOTSTRAP_PASSWORD

# Enough attempts that a regression shows up as a failure, not a flake. With the
# bug present this lost 8 of 12 immediate reads; hiding it again would take an
# unusually lucky streak.
_ATTEMPTS = 12


@contextmanager
def live_base_url(app: Any) -> Iterator[str]:
    """Serve *app* on a random loopback port and yield its base URL.

    uvicorn is a runtime dependency — it is how the API runs under systemd — so
    this is not reaching for a test-only extra.
    """
    import uvicorn

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        lifespan="off",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="durability-uvicorn")
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        if not thread.is_alive():
            raise RuntimeError("uvicorn exited before it started")
        time.sleep(0.01)
    assert server.started, "uvicorn did not start within 15s"
    port = int(server.servers[0].sockets[0].getsockname()[1])
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def _call(
    base: str, method: str, path: str, body: dict[str, Any] | None, token: str
) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read() or b"null"
            code = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b"null"
        code = exc.code
    return code, json.loads(raw)


@pytest.fixture()
def prod_engine(settings: Any, engine: Engine, db: Any, seeded: Any) -> Iterator[Engine]:
    """An engine shaped like the one the real API process uses.

    No ``isolation_level`` override — MySQL's default ``REPEATABLE READ``. The
    tables are already there (``engine`` built them) and ``seeded`` has already
    committed the bootstrap admin onto that schema.
    """
    from backend.app.db import session as db_session

    shaped = create_engine(
        settings.test_database_url,
        pool_pre_ping=True,
        future=True,
        pool_size=5,
        max_overflow=10,
    )
    previous_engine = db_session._engine
    previous_factory = db_session._session_factory
    db_session._engine = shaped
    db_session._session_factory = sessionmaker(
        bind=shaped, expire_on_commit=False, future=True
    )
    try:
        yield shaped
    finally:
        db_session._engine = previous_engine
        db_session._session_factory = previous_factory
        shaped.dispose()


@pytest.fixture()
def live(prod_engine: Engine) -> Iterator[str]:
    """A real uvicorn serving the app against the production-shaped engine."""
    from backend.app.main import create_app

    with live_base_url(create_app()) as base:
        yield base


@pytest.fixture()
def live_token(live: str) -> str:
    code, payload = _call(
        live,
        "POST",
        "/api/v1/auth/login",
        {"username": BOOTSTRAP_ADMIN, "password": BOOTSTRAP_PASSWORD},
        token="",
    )
    assert code == 200, payload
    return str(payload["access_token"])


class TestCreateIsVisibleToTheNextRequest:
    def test_a_created_server_can_be_read_back_immediately(
        self, live: str, live_token: str
    ) -> None:
        """The console's create-then-refresh, which is every create in the UI."""
        misses: list[str] = []
        for i in range(_ATTEMPTS):
            code, created = _call(
                live,
                "POST",
                "/api/v1/servers",
                {"name": f"race-{i}", "hostname": "127.0.0.1", "port": 22},
                token=live_token,
            )
            assert code == 201, created
            server_id = created["id"]

            code, _readback = _call(
                live, "GET", f"/api/v1/servers/{server_id}", None, token=live_token
            )
            if code != 200:
                misses.append(f"GET /servers/{server_id} -> {code}")

        assert not misses, (
            "a 201 reached the client before the row was durable; "
            "the immediate read could not see it:\n  " + "\n  ".join(misses)
        )

    def test_a_created_credential_can_be_read_back_immediately(
        self, live: str, live_token: str
    ) -> None:
        """Same rule on a route that writes through a different service."""
        for i in range(_ATTEMPTS):
            code, created = _call(
                live,
                "POST",
                "/api/v1/credentials",
                {"name": f"race-{i}", "kind": "ssh_key", "secret": "k" * 16},
                token=live_token,
            )
            assert code == 201, created
            code, _readback = _call(
                live,
                "GET",
                f"/api/v1/credentials/{created['id']}",
                None,
                token=live_token,
            )
            assert code == 200, (
                f"GET /credentials/{created['id']} -> {code} right after a 201"
            )

    def test_a_list_sees_what_was_just_created(self, live: str, live_token: str) -> None:
        """A list refresh is the same race as a read-by-id."""
        code, created = _call(
            live,
            "POST",
            "/api/v1/servers",
            {"name": "list-race", "hostname": "127.0.0.1", "port": 22},
            token=live_token,
        )
        assert code == 201, created
        code, listed = _call(live, "GET", "/api/v1/servers", None, token=live_token)
        assert code == 200, listed
        assert any(row["id"] == created["id"] for row in listed), (
            f"the row created a moment ago is missing from the list: {listed}"
        )
