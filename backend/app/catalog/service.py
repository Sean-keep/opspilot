"""Template catalog operations.

Two rules get enforced here and nowhere else, because both are about *state*
rather than about parsing:

1. **A version body is written once.** ``spec_json`` / ``inputs_schema_json`` /
   ``checksum`` may only change while the version is ``DRAFT``. The moment it
   leaves ``DRAFT`` the body is frozen — even before it is published — because
   ``VALIDATED`` and ``TESTED`` are claims about a specific body.
2. **Status moves along the lifecycle, one edge at a time.** ``PUBLISHED`` can
   only become ``DEPRECATED``. There is no "unpublish", and there is no path
   back to ``DRAFT``.

Neither rule is a UI convention. ``TemplateService`` refuses both, and the
tests assert the refusal rather than the happy path.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from shared.templates import (
    STATUS_TRANSITIONS,
    LoadedTemplate,
    TemplateSpec,
    dry_run_params,
    load_template,
    validate_template,
)
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from backend.app.catalog.models import (
    FROZEN_STATUSES,
    TEMPLATE_DEPRECATED,
    TEMPLATE_DRAFT,
    TEMPLATE_PUBLISHED,
    TEMPLATE_VALIDATED,
    Template,
    TemplateVersion,
)
from backend.app.core.errors import NotFound
from backend.app.db.base import utcnow


class TemplateConflict(ValueError):
    """A version exists and is frozen, but something tried to rewrite it."""


class InvalidTransition(ValueError):
    """A status move that the lifecycle does not allow."""


class CatalogService:
    """Template catalog reads and lifecycle writes."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def list_all(self) -> list[Template]:
        stmt = (
            select(Template)
            .options(selectinload(Template.versions))
            .order_by(Template.category, Template.name)
            .execution_options(populate_existing=True)
        )
        return list(self.db.scalars(stmt))

    def get_by_name(self, name: str) -> Template | None:
        # populate_existing: a template created earlier in this session carries
        # a stale `versions` collection (the ORM does not refresh a relationship
        # it believes is already loaded), and re-importing into that stale view
        # would INSERT a version row that is already there.
        stmt = (
            select(Template)
            .options(selectinload(Template.versions))
            .where(Template.name == name)
            .execution_options(populate_existing=True)
        )
        return self.db.scalars(stmt).one_or_none()

    def require_by_id(self, template_id: int) -> Template:
        """Load one template by primary key, or raise ``NotFound``.

        The HTTP layer addresses templates by ``id`` because that is what a
        list response hands back; the service still resolves versions by name
        internally so every write path goes through one lookup.
        """
        stmt = (
            select(Template)
            .options(selectinload(Template.versions))
            .where(Template.id == template_id)
            .execution_options(populate_existing=True)
        )
        row = self.db.scalars(stmt).one_or_none()
        if row is None:
            raise NotFound(f"no template with id {template_id}")
        return row

    def get_version(self, template_name: str, version: str) -> TemplateVersion | None:
        """One version row, queried directly rather than via the collection.

        Walking ``template.versions`` would read whatever the session last
        loaded for that relationship, which is not necessarily what the
        transaction currently holds.
        """
        stmt = (
            select(TemplateVersion)
            .join(Template, TemplateVersion.template_id == Template.id)
            .where(Template.name == template_name, TemplateVersion.version == version)
            .execution_options(populate_existing=True)
        )
        return self.db.scalars(stmt).one_or_none()

    # ------------------------------------------------------------------
    # Import from disk
    # ------------------------------------------------------------------
    def import_tree(self, root: Path | str, *, category: str) -> list[Template]:
        """Sync every template directory under *root* into the catalog.

        A version that already exists and is frozen is left alone even when the
        file on disk has changed — the file is not the source of truth for a
        published body, and silently rewiring one is exactly the failure mode
        the checksum exists to catch.
        """
        base = Path(root)
        imported: list[Template] = []
        if not base.is_dir():
            return imported

        for directory in sorted(p for p in base.iterdir() if p.is_dir()):
            if not (directory / "template.yaml").is_file():
                continue
            loaded = load_template(directory)
            imported.append(self.upsert_loaded(loaded, category=category))
        self.db.flush()
        return imported

    def upsert_loaded(self, loaded: LoadedTemplate, *, category: str) -> Template:
        """Create or update one template from a loaded, already-validated body."""
        spec = loaded.spec
        checksum = loaded.checksum
        inputs_schema = loaded.inputs_schema

        template = self.get_by_name(spec.name)
        if template is None:
            template = Template(
                name=spec.name,
                category=category,
                description=spec.description[:512],
                latest_version=spec.version,
            )
            self.db.add(template)
            self.db.flush()
        else:
            template.description = spec.description[:512]
            template.category = category or template.category

        existing = self.get_version(spec.name, spec.version)
        if existing is None:
            self.db.add(
                TemplateVersion(
                    template_id=template.id,
                    version=spec.version,
                    status=TEMPLATE_DRAFT,
                    checksum=checksum,
                    spec_json=spec.model_dump(),
                    inputs_schema_json=dict(inputs_schema),
                    notes="",
                )
            )
            # Flush so the row is visible to the next lookup in this session.
            self.db.flush()
        else:
            self._rewrite_body(existing, checksum=checksum, spec=spec, schema=inputs_schema)

        # latest_version tracks the highest version number we know about, by
        # insertion order — not "whatever was imported last".
        if not template.latest_version:
            template.latest_version = spec.version
        template.updated_at = utcnow()
        return template

    def _rewrite_body(
        self,
        row: TemplateVersion,
        *,
        checksum: str,
        spec: TemplateSpec,
        schema: Mapping[str, Any],
    ) -> None:
        if row.status in FROZEN_STATUSES:
            if row.checksum == checksum:
                return  # unchanged on disk; nothing to do
            raise TemplateConflict(
                f"{row.template.name} {row.version} is {row.status} and its body is "
                "frozen; publish a new version instead of editing this one"
            )
        row.checksum = checksum
        row.spec_json = spec.model_dump()
        row.inputs_schema_json = dict(schema)
        row.updated_at = utcnow()

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def validate_version(self, template_name: str, version: str) -> dict[str, object]:
        """Re-run the full template checks on a stored version.

        Returns a result dict rather than raising: an operator wants the list of
        problems, and a version that fails validation simply does not move.
        """
        row = self.get_version(template_name, version)
        if row is None:
            raise NotFound(f"no template {template_name!r} version {version!r}")

        spec = TemplateSpec.model_validate(row.spec_json)
        schema = dict(row.inputs_schema_json or {})

        errors = validate_template(spec, schema)
        problems = dry_run_params(spec, schema)
        warnings: list[str] = []
        for step_key, messages in sorted(problems.items()):
            if step_key == "_probe":
                warnings.extend(messages)
                continue
            errors.extend(f"step {step_key!r}: {m}" for m in messages)

        ok = not errors
        result: dict[str, object] = {
            "ok": ok,
            "template": template_name,
            "version": version,
            "checksum": row.checksum,
            "errors": errors,
            "warnings": warnings,
            "status": row.status,
        }

        if ok and row.status == TEMPLATE_DRAFT:
            self.transition(template_name, version, TEMPLATE_VALIDATED)
            result["status"] = TEMPLATE_VALIDATED
        return result

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def transition(
        self,
        template_name: str,
        version: str,
        to_status: str,
        *,
        notes: str = "",
    ) -> TemplateVersion:
        row = self.get_version(template_name, version)
        if row is None:
            raise NotFound(f"no template {template_name!r} version {version!r}")

        allowed = STATUS_TRANSITIONS.get(row.status, frozenset())
        if to_status not in allowed:
            raise InvalidTransition(
                f"{template_name} {version} cannot move {row.status} -> {to_status}; "
                f"allowed next: {', '.join(sorted(allowed)) or '<none>'}"
            )

        row.status = to_status
        if notes:
            row.notes = notes
        now = utcnow()
        if to_status == TEMPLATE_PUBLISHED:
            row.published_at = now
        elif to_status == TEMPLATE_DEPRECATED:
            row.deprecated_at = now
        row.updated_at = now
        self.db.flush()
        return row

    def publish(self, template_name: str, version: str, *, notes: str = "") -> TemplateVersion:
        """Move a version to PUBLISHED. The body must already be VALIDATED."""
        row = self.get_version(template_name, version)
        if row is None:
            raise NotFound(f"no template {template_name!r} version {version!r}")
        if not row.checksum:
            raise TemplateConflict(
                f"{template_name} {version} has no checksum; it cannot be published"
            )
        return self.transition(template_name, version, TEMPLATE_PUBLISHED, notes=notes)

    def assert_body_mutable(self, template_name: str, version: str) -> None:
        """Raise unless this version's body may still be rewritten."""
        row = self.get_version(template_name, version)
        if row is not None and row.status in FROZEN_STATUSES:
            raise TemplateConflict(
                f"{template_name} {version} is {row.status} and its body is frozen"
            )
