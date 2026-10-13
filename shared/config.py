"""Runtime configuration.

Lives in ``shared/`` so the worker process can read settings without importing
``backend.app``. ``backend.app.core.settings`` re-exports these names.

Every external value comes from the environment or a permission-restricted
file — never from a committed constant. See ``.env.example`` for the full set.

All settings are read with the ``OPSPILOT_`` prefix (``OPSPILOT_DB_HOST`` …).

Two conventions matter for the security model:

* ``*_FILE`` variants let a secret be injected from a file the process can read
  but that does not live next to the data it protects. The master encryption key
  MUST be supplied this way in production.
* Nothing here ever logs a resolved secret; use ``safe_summary()``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _normalise_write_prefix(value: str) -> str:
    """Coerce one ``OPSPILOT_ALLOWED_WRITE_PREFIXES`` entry into a safe prefix.

    A write prefix is a security boundary, so this refuses the shapes that
    would silently widen it past what the operator wrote: a relative path, the
    root itself (which would allow everything), any ``..`` component, and any
    control character. Everything is then normalised to end in ``/`` so that
    ``startswith`` cannot be satisfied by a sibling directory that merely
    shares a name prefix (``/opt/app`` matching ``/opt/app-evil``).
    """
    prefix = value.strip()
    if not prefix:
        raise ValueError(f"write prefix {value!r} is empty")
    # Checked on the *raw* value: strip() would otherwise eat a trailing
    # newline and let "/opt/app\n" through as the perfectly clean "/opt/app".
    if not value.isprintable():
        raise ValueError(f"write prefix {value!r} contains control characters")
    if not prefix.startswith("/"):
        raise ValueError(f"write prefix {value!r} must be an absolute path")
    parts = [p for p in prefix.split("/") if p]
    if not parts:
        raise ValueError("write prefix '/' would allow writing anywhere")
    if any(p == ".." for p in parts):
        raise ValueError(f"write prefix {value!r} must not contain '..'")
    return "/" + "/".join(parts) + "/"


class Settings(BaseSettings):
    """Typed view over the ``OPSPILOT_*`` environment."""

    model_config = SettingsConfigDict(
        env_prefix="OPSPILOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # -- runtime ---------------------------------------------------------
    env: Literal["dev", "staging", "production"] = "dev"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8080, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # -- MySQL -----------------------------------------------------------
    db_host: str = "127.0.0.1"
    db_port: int = Field(default=3306, ge=1, le=65535)
    db_name: str = "opspilot"
    db_user: str = "opspilot"
    db_password: SecretStr = SecretStr("")
    test_db_name: str = "opspilot_test"

    # -- auth ------------------------------------------------------------
    secret_key: SecretStr = SecretStr("")
    access_token_ttl: int = Field(default=3600, ge=60, le=86400)
    login_max_failures: int = Field(default=10, ge=1, le=1000)
    login_window_seconds: int = Field(default=300, ge=10, le=86400)

    # -- credential encryption ------------------------------------------
    master_key: SecretStr = SecretStr("")
    master_key_file: Path | None = None

    # -- worker ----------------------------------------------------------
    worker_id: str = ""
    lease_seconds: int = Field(default=30, ge=5, le=3600)
    heartbeat_seconds: int = Field(default=5, ge=1, le=600)
    worker_concurrency: int = Field(default=2, ge=1, le=64)

    # -- SSH -------------------------------------------------------------
    ssh_user: str = ""
    ssh_connect_timeout: int = Field(default=10, ge=1, le=300)
    ssh_command_timeout: int = Field(default=600, ge=1, le=86400)
    healthcheck_timeout: int = Field(default=30, ge=1, le=600)

    # -- allowlists ------------------------------------------------------
    artifact_allowed_hosts: str = ""
    healthcheck_allowed_cidrs: str = "127.0.0.0/8"

    #: Remote path prefixes a template is allowed to **write** to. Empty means
    #: "use the defaults in ``shared.actions.render``"; a non-empty value
    #: *replaces* them. Widen this only when an existing deployment genuinely
    #: lives outside the defaults — the API and every worker must be given the
    #: same value, or a plan the API accepted will be refused at execution.
    allowed_write_prefixes: str = ""

    # -- retention -------------------------------------------------------
    log_retention_days: int = Field(default=30, ge=1, le=3650)
    log_max_bytes_per_run: int = Field(default=10 * 1024 * 1024, ge=1024)

    # -- notifications ---------------------------------------------------
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""

    # ------------------------------------------------------------------
    @field_validator("db_host", "api_host", "ssh_user", "worker_id", mode="before")
    @classmethod
    def _strip_str(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v

    @field_validator("allowed_write_prefixes")
    @classmethod
    def _check_write_prefixes(cls, v: str) -> str:
        """Reject a boundary-widening entry at startup, not at the first write.

        An empty setting is legal and means "use the product defaults"; every
        non-empty entry must survive normalisation, so ``/`` or a ``..`` climb
        stops the process from starting at all.
        """
        for part in v.split(","):
            if part.strip():
                _normalise_write_prefix(part)
        return v

    @model_validator(mode="after")
    def _check_heartbeat_under_lease(self) -> Settings:
        if self.heartbeat_seconds >= self.lease_seconds:
            raise ValueError(
                "heartbeat_seconds must be strictly less than lease_seconds, "
                f"got {self.heartbeat_seconds} >= {self.lease_seconds}"
            )
        return self

    # ------------------------------------------------------------------
    @property
    def database_url(self) -> str:
        """SQLAlchemy URL for the control-plane metadata database."""
        return self._build_url(self.db_name)

    @property
    def test_database_url(self) -> str:
        """SQLAlchemy URL for the destructive test database."""
        return self._build_url(self.test_db_name)

    def _build_url(self, database: str) -> str:
        password = self.db_password.get_secret_value()
        # PyMySQL driver. The init_command pins the session to UTC so every
        # DATETIME(3) column is written and read consistently regardless of the
        # server's own time_zone.
        auth = f"{self.db_user}:{password}@" if password else f"{self.db_user}@"
        return (
            f"mysql+pymysql://{auth}{self.db_host}:{self.db_port}/{database}"
            "?charset=utf8mb4"
            "&init_command=SET%20time_zone%3D%27%2B00%3A00%27"
        )

    # ------------------------------------------------------------------
    def resolve_master_key(self) -> str:
        """Return the Fernet key used to encrypt stored credential payloads.

        Prefers ``master_key_file`` so the key is never stored alongside the
        ciphertext it protects. Falls back to the inline value for local dev.
        """
        if self.master_key_file is not None:
            path = Path(self.master_key_file)
            if not path.is_file():
                raise RuntimeError(f"master key file not found: {path}")
            # Refuse group/world readable key files — this is a hard requirement.
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                raise RuntimeError(
                    f"master key file {path} is group/world accessible "
                    f"(mode {oct(mode)}); chmod 600 it"
                )
            return path.read_text(encoding="utf-8").strip()
        value = self.master_key.get_secret_value()
        if not value:
            raise RuntimeError(
                "no master key configured: set OPSPILOT_MASTER_KEY_FILE "
                "(preferred) or OPSPILOT_MASTER_KEY"
            )
        return value

    # ------------------------------------------------------------------
    def artifact_hosts(self) -> frozenset[str]:
        return frozenset(
            h.strip().lower() for h in self.artifact_allowed_hosts.split(",") if h.strip()
        )

    def healthcheck_cidrs(self) -> tuple[str, ...]:
        return tuple(c.strip() for c in self.healthcheck_allowed_cidrs.split(",") if c.strip())

    def write_prefixes(self) -> tuple[str, ...]:
        """The write allowlist to enforce.

        An empty setting means "the defaults" and is resolved by the caller
        (``shared.actions.render.allowed_path_prefixes``), which is where the
        default tuple lives. A non-empty setting replaces them wholesale so an
        operator cannot accidentally get a union wider than they meant.
        """
        raw = [p.strip() for p in self.allowed_write_prefixes.split(",")]
        return tuple(_normalise_write_prefix(p) for p in raw if p.strip())

    def safe_summary(self) -> dict[str, object]:
        """A loggable snapshot: every secret reduced to a presence flag.

        Keys are chosen to avoid the redaction markers (``password``,
        ``secret``, ``token`` …) so the presence flags themselves survive
        scrubbing. The values are booleans and source labels, never material.
        """
        return {
            "env": self.env,
            "api": f"{self.api_host}:{self.api_port}",
            "db": f"{self.db_user}@{self.db_host}:{self.db_port}/{self.db_name}",
            "db_login_present": bool(self.db_password.get_secret_value()),
            "signing_ready": bool(self.secret_key.get_secret_value()),
            "vault_source": (
                "file"
                if self.master_key_file
                else ("inline" if self.master_key.get_secret_value() else "unset")
            ),
            "worker_id": self.worker_id,
            "lease_seconds": self.lease_seconds,
            "heartbeat_seconds": self.heartbeat_seconds,
            "ssh_user": self.ssh_user,
            "log_level": self.log_level,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Tests call ``clear_settings_cache()`` after mutating the environment.
    """
    return Settings()


def clear_settings_cache() -> None:
    """Drop the cached settings so the next read reflects new env vars."""
    get_settings.cache_clear()
