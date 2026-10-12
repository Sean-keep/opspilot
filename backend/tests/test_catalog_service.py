"""Template catalog service: a version body is written once, and status moves
one edge at a time.

These two rules are the reason the catalog is not just a file loader. A
``PUBLISHED`` version is what a task will execute, so anything that would
silently change it — a re-import from a edited directory, a status nudge back
to ``DRAFT`` — has to be refused rather than absorbed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from shared.actions.base import register_all
from shared.templates import LoadedTemplate, TemplateSpec, compute_checksum, validate_template
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.catalog.models import (
    TEMPLATE_DEPRECATED,
    TEMPLATE_DRAFT,
    TEMPLATE_PUBLISHED,
    TEMPLATE_TESTED,
    TEMPLATE_VALIDATED,
    Template,
    TemplateVersion,
)
from backend.app.catalog.service import CatalogService, InvalidTransition, TemplateConflict
from backend.app.core.errors import NotFound


@pytest.fixture(autouse=True)
def _registry() -> None:
    register_all()


def _spec(**overrides: Any) -> TemplateSpec:
    body: dict[str, Any] = {
        "api_version": "v1",
        "name": "demo",
        "version": "1.0.0",
        "description": "catalog fixture",
        "steps": [
            {"key": "check", "action": "system.check_os", "params": {"expect": ["ubuntu-24.04"]}}
        ],
    }
    body.update(overrides)
    return TemplateSpec.model_validate(body)


EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": False, "properties": {}}


def _loaded(**overrides: Any) -> LoadedTemplate:
    spec = _spec(**overrides)
    files = {
        "template.yaml": spec.model_dump_json(),
        "schemas/inputs.schema.json": "{}",
    }
    return LoadedTemplate(
        spec=spec,
        root=Path("/nonexistent") / spec.name,
        inputs_schema=dict(EMPTY_SCHEMA),
        checksum=compute_checksum(files),
        files=files,
    )


@pytest.fixture()
def svc(db: Session) -> CatalogService:
    return CatalogService(db)


def _import(svc: CatalogService, **overrides: Any) -> Template:
    row = svc.upsert_loaded(_loaded(**overrides), category="runtime")
    svc.db.commit()
    return row


# ==========================================================================
# Immutability
# ==========================================================================
def test_a_published_body_is_byte_identical_after_a_rejected_edit(svc: CatalogService) -> None:
    template = _import(svc)
    svc.validate_version(template.name, "1.0.0")
    svc.transition(template.name, "1.0.0", TEMPLATE_TESTED)
    svc.publish(template.name, "1.0.0")
    svc.db.commit()

    before = svc.get_version(template.name, "1.0.0")
    assert before is not None
    spec_before = str(before.spec_json)
    checksum_before = before.checksum

    # Same version number, different body — exactly what an operator would do
    # by editing the file and re-importing.
    changed = _loaded(
        description="edited after publish",
        steps=[
            {"key": "check", "action": "system.check_os", "params": {"expect": ["debian-12"]}}
        ],
    )
    assert changed.checksum != checksum_before

    with pytest.raises(TemplateConflict, match="frozen"):
        svc.upsert_loaded(changed, category="runtime")

    svc.db.rollback()
    after = svc.get_version(template.name, "1.0.0")
    assert after is not None
    assert after.checksum == checksum_before
    assert str(after.spec_json) == spec_before


def test_an_unchanged_reimport_of_a_published_version_is_a_no_op(svc: CatalogService) -> None:
    template = _import(svc)
    svc.validate_version(template.name, "1.0.0")
    svc.transition(template.name, "1.0.0", TEMPLATE_TESTED)
    svc.publish(template.name, "1.0.0")
    svc.db.commit()

    checksum = svc.get_version(template.name, "1.0.0").checksum  # type: ignore[union-attr]
    svc.upsert_loaded(_loaded(), category="runtime")
    svc.db.commit()
    assert svc.get_version(template.name, "1.0.0").checksum == checksum  # type: ignore[union-attr]


def test_a_draft_body_may_be_rewritten(svc: CatalogService) -> None:
    template = _import(svc)
    before = svc.get_version(template.name, "1.0.0")
    assert before is not None
    assert before.status == TEMPLATE_DRAFT
    # Snapshot as a plain str: `before` and the post-edit row are the same ORM
    # object under the identity map, so reading the attribute later would show
    # the mutated value and the comparison would be vacuous.
    checksum_before = before.checksum

    svc.upsert_loaded(_loaded(description="edited while still a draft"), category="runtime")
    svc.db.commit()
    after = svc.get_version(template.name, "1.0.0")
    assert after is not None
    assert after.checksum != checksum_before


def test_assert_body_mutable_refuses_anything_past_draft(svc: CatalogService) -> None:
    template = _import(svc)
    svc.validate_version(template.name, "1.0.0")  # DRAFT -> VALIDATED
    svc.db.commit()
    with pytest.raises(TemplateConflict, match="frozen"):
        svc.assert_body_mutable(template.name, "1.0.0")


# ==========================================================================
# Lifecycle
# ==========================================================================
def test_an_illegal_transition_is_refused(svc: CatalogService) -> None:
    template = _import(svc)
    with pytest.raises(InvalidTransition):
        svc.transition(template.name, "1.0.0", TEMPLATE_PUBLISHED)  # straight from DRAFT
    with pytest.raises(InvalidTransition):
        svc.transition(template.name, "1.0.0", TEMPLATE_DEPRECATED)  # DRAFT -> DEPRECATED


def test_a_published_version_can_only_be_deprecated(svc: CatalogService) -> None:
    template = _import(svc)
    svc.validate_version(template.name, "1.0.0")
    svc.transition(template.name, "1.0.0", TEMPLATE_TESTED)
    svc.publish(template.name, "1.0.0")
    svc.db.commit()

    with pytest.raises(InvalidTransition):
        svc.transition(template.name, "1.0.0", TEMPLATE_DRAFT)
    with pytest.raises(InvalidTransition):
        svc.transition(template.name, "1.0.0", TEMPLATE_VALIDATED)

    row = svc.transition(template.name, "1.0.0", TEMPLATE_DEPRECATED)
    assert row.status == TEMPLATE_DEPRECATED
    assert row.deprecated_at is not None
    # And deprecation is terminal.
    with pytest.raises(InvalidTransition):
        svc.transition(template.name, "1.0.0", TEMPLATE_PUBLISHED)


def test_publishing_a_version_without_a_checksum_is_refused(svc: CatalogService) -> None:
    template = _import(svc)
    row = svc.get_version(template.name, "1.0.0")
    assert row is not None
    row.status = TEMPLATE_TESTED
    row.checksum = ""
    svc.db.flush()
    with pytest.raises(TemplateConflict, match="no checksum"):
        svc.publish(template.name, "1.0.0")


def test_validate_promotes_a_passing_draft_and_leaves_a_failing_one_alone(
    svc: CatalogService,
) -> None:
    template = _import(svc)
    result = svc.validate_version(template.name, "1.0.0")
    assert result["ok"] is True
    assert result["status"] == TEMPLATE_VALIDATED

    # Second call: nothing to promote, still reports the same verdict.
    again = svc.validate_version(template.name, "1.0.0")
    assert again["ok"] is True
    assert again["status"] == TEMPLATE_VALIDATED


def test_a_version_that_fails_validation_does_not_move(svc: CatalogService) -> None:
    loaded = _loaded(steps=[{"key": "x", "action": "nope.not_registered", "params": {}}])
    assert validate_template(loaded.spec, loaded.inputs_schema)  # it really is bad

    template = svc.upsert_loaded(loaded, category="runtime")
    svc.db.commit()
    result = svc.validate_version(template.name, "1.0.0")
    assert result["ok"] is False
    assert result["errors"]
    assert result["status"] == TEMPLATE_DRAFT


def test_validating_a_missing_version_raises(svc: CatalogService) -> None:
    template = _import(svc)
    with pytest.raises(NotFound):
        svc.validate_version(template.name, "9.9.9")


# ==========================================================================
# Uniqueness
# ==========================================================================
def test_a_second_row_for_the_same_template_and_version_is_refused(
    svc: CatalogService, db: Session
) -> None:
    template = _import(svc)
    with pytest.raises(IntegrityError):
        db.add(
            TemplateVersion(
                template_id=template.id,
                version="1.0.0",
                status=TEMPLATE_DRAFT,
                checksum="0" * 64,
                spec_json={},
                inputs_schema_json={},
                notes="",
            )
        )
        db.flush()
    db.rollback()


def test_two_templates_may_share_a_version_number(svc: CatalogService) -> None:
    a = _import(svc)
    again = _import(svc)  # same name -> upserts onto `a`
    assert again.id == a.id

    other = svc.upsert_loaded(_loaded(name="other"), category="application")
    svc.db.commit()
    assert other.id != a.id
    assert other.latest_version == "1.0.0"
    assert a.latest_version == "1.0.0"


def test_a_template_name_is_unique(svc: CatalogService, db: Session) -> None:
    _import(svc)
    with pytest.raises(IntegrityError):
        db.add(Template(name="demo", category="runtime", description="", latest_version=""))
        db.flush()
    db.rollback()
