from collections.abc import Generator

import pytest
from sqlalchemy.orm import Session

from app.database.session import engine
from app.services.taxonomy import (
    TaxonomyConflictError,
    TaxonomyValidationError,
    associate_skill,
    create_category,
    create_skill,
    delete_category,
    disassociate_skill,
    set_category_active,
    set_skill_active,
    update_category,
    update_skill,
)


@pytest.fixture
def db_session() -> Generator[Session]:
    """Provide an isolated PostgreSQL transaction for each test."""
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(
            bind=connection,
            autoflush=False,
            expire_on_commit=False,
        )

        try:
            yield session
        finally:
            session.close()
            transaction.rollback()


def test_category_description_is_preserved_when_omitted(
    db_session: Session,
) -> None:
    category = create_category(
        db_session,
        "Construction",
        description="Construction services",
    )

    updated = update_category(db_session, category.id, name="Building")

    assert updated.description == "Construction services"


def test_category_description_can_be_explicitly_cleared(
    db_session: Session,
) -> None:
    category = create_category(
        db_session,
        "Construction",
        description="Construction services",
    )

    updated = update_category(
        db_session,
        category.id,
        description=None,
    )

    assert updated.description is None


def test_skill_description_is_preserved_when_omitted(
    db_session: Session,
) -> None:
    skill = create_skill(
        db_session,
        "Welding",
        description="Metal joining",
    )

    updated = update_skill(db_session, skill.id, name="Arc Welding")

    assert updated.description == "Metal joining"


def test_skill_description_can_be_explicitly_cleared(
    db_session: Session,
) -> None:
    skill = create_skill(
        db_session,
        "Welding",
        description="Metal joining",
    )

    updated = update_skill(
        db_session,
        skill.id,
        description=None,
    )

    assert updated.description is None


def test_inactive_skill_cannot_be_associated(
    db_session: Session,
) -> None:
    category = create_category(db_session, "Construction")
    skill = create_skill(db_session, "Welding")

    set_skill_active(db_session, skill.id, False)

    with pytest.raises(TaxonomyValidationError):
        associate_skill(db_session, category.id, skill.id)


def test_category_under_inactive_ancestor_cannot_receive_skill(
    db_session: Session,
) -> None:
    construction = create_category(db_session, "Construction")
    roofing = create_category(
        db_session,
        "Roofing",
        parent_id=construction.id,
    )
    skill = create_skill(db_session, "Roof Installation")

    set_category_active(db_session, construction.id, False)

    with pytest.raises(TaxonomyValidationError):
        associate_skill(db_session, roofing.id, skill.id)


def test_category_with_associated_skills_cannot_be_deleted(
    db_session: Session,
) -> None:
    category = create_category(db_session, "Construction")
    skill = create_skill(db_session, "Welding")

    associate_skill(db_session, category.id, skill.id)

    with pytest.raises(TaxonomyConflictError):
        delete_category(db_session, category.id)


def test_category_can_be_deleted_after_skill_disassociation(
    db_session: Session,
) -> None:
    category = create_category(db_session, "Construction")
    skill = create_skill(db_session, "Welding")

    associate_skill(db_session, category.id, skill.id)
    disassociate_skill(db_session, category.id, skill.id)

    delete_category(db_session, category.id)


@pytest.mark.parametrize("invalid_value", ["true", 1, None])
def test_category_activation_requires_boolean(
    db_session: Session,
    invalid_value: object,
) -> None:
    category = create_category(db_session, "Construction")

    with pytest.raises(TaxonomyValidationError):
        set_category_active(db_session, category.id, invalid_value)


@pytest.mark.parametrize("invalid_value", ["false", 0, None])
def test_skill_activation_requires_boolean(
    db_session: Session,
    invalid_value: object,
) -> None:
    skill = create_skill(db_session, "Welding")

    with pytest.raises(TaxonomyValidationError):
        set_skill_active(db_session, skill.id, invalid_value)
