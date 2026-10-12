"""Settings, logging, and the worker skeleton.

Small but load-bearing: ``safe_summary`` must stay loggable (its presence flags
have to survive redaction), and the worker must be startable and stoppable
without touching anything.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from shared.redaction import REDACTED, redact

from backend.app.core.settings import Settings


def test_database_url_pins_utc_session() -> None:
    s = Settings(db_password="pw")
    assert "init_command=SET%20time_zone%3D%27%2B00%3A00%27" in s.database_url
    assert "mysql+pymysql://" in s.database_url


def test_database_url_omits_password_when_unset() -> None:
    s = Settings(db_password="")
    assert "://" in s.database_url
    assert ":@" not in s.database_url


def test_heartbeat_must_be_under_lease() -> None:
    with pytest.raises(ValueError, match="heartbeat_seconds"):
        Settings(lease_seconds=10, heartbeat_seconds=10)


def test_safe_summary_presence_flags_survive_redaction() -> None:
    """The summary is only useful if redaction leaves the flags readable."""
    s = Settings(
        db_password="super-secret-db-password",
        secret_key="super-secret-signing-key",
        master_key="super-secret-master-key",
    )
    summary = s.safe_summary()

    scrubbed = redact(summary)
    assert isinstance(scrubbed, dict)

    # Presence flags must still be booleans, not [REDACTED].
    assert scrubbed["db_login_present"] is True
    assert scrubbed["signing_ready"] is True
    assert scrubbed["vault_source"] == "inline"

    # And nothing that looks like a secret value may appear anywhere.
    blob = str(scrubbed)
    assert "super-secret-db-password" not in blob
    assert "super-secret-signing-key" not in blob
    assert "super-secret-master-key" not in blob


def test_safe_summary_never_contains_secret_material() -> None:
    s = Settings(db_password="hunter2-hunter2", secret_key="abc123abc123")
    blob = str(s.safe_summary())
    assert "hunter2" not in blob
    assert "abc123" not in blob


def test_master_key_file_must_not_be_group_or_world_readable(tmp_path: Path) -> None:
    key_file = tmp_path / "master.key"
    key_file.write_text("dGVzdA==\n", encoding="utf-8")
    key_file.chmod(0o644)  # deliberately too open

    s = Settings(master_key_file=key_file)
    with pytest.raises(RuntimeError, match="group/world"):
        s.resolve_master_key()

    key_file.chmod(0o600)
    assert s.resolve_master_key() == "dGVzdA=="


def test_master_key_file_missing_is_an_error(tmp_path: Path) -> None:
    s = Settings(master_key_file=tmp_path / "nope.key")
    with pytest.raises(RuntimeError, match="not found"):
        s.resolve_master_key()


def test_no_master_key_configured_is_an_error() -> None:
    s = Settings(master_key="", master_key_file=None)
    with pytest.raises(RuntimeError, match="no master key"):
        s.resolve_master_key()


def test_worker_scheduler_is_idle_but_startable() -> None:
    """S0 ships the process skeleton; the queue arrives in S2."""
    from worker.scheduler import Scheduler

    from backend.app.core.settings import Settings

    sched = Scheduler(worker_id="test-worker", settings=Settings())
    assert sched.tick() is False  # nothing to do in S0
    sched.shutdown()
    assert sched.tick() is False  # and nothing after shutdown either
    sched.shutdown()  # idempotent


def test_worker_id_resolution_prefers_configured_value() -> None:
    from worker.main import resolve_worker_id

    assert resolve_worker_id(Settings(worker_id="fixed-1")) == "fixed-1"
    auto = resolve_worker_id(Settings(worker_id=""))
    assert auto and ":" in auto  # hostname:pid:suffix


def test_utcnow_is_tz_aware_utc() -> None:
    from backend.app.db.base import utcnow

    now = utcnow()
    assert now.tzinfo is dt.UTC
    assert now.utcoffset() == dt.timedelta(0)


def test_repeated_redaction_is_idempotent() -> None:
    once = redact({"password": "x", "keep": 1})
    twice = redact(once)
    assert once == twice
    assert once["password"] == REDACTED
