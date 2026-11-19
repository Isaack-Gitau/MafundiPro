from collections.abc import Generator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.database.session import engine
from app.models.identity import User
from app.models.taxonomy import Category
from app.models.worker import WorkerProfession, WorkerProfile, WorkerSkill
from app.services.taxonomy import (
    TaxonomyConflictError,
    TaxonomyNotFoundError,
    TaxonomyValidationError,
    associate_skill,
    create_category,
    create_skill,
    delete_category,
    delete_skill,
    disassociate_skill,
    move_category,
    normalize_name,
    set_category_active,
    update_category,
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


def create_worker_profession(
    db: Session,
    main_category: Category,
) -> WorkerProfile:
    """Create a test worker with one selected main profession."""
    username = f"taxonomy_{uuid4().hex[:12]}"

    user = User(
        full_name="Taxonomy Test Worker",
        username=username,
        username_key=username.casefold(),
        phone_number=f"+2547{uuid4().int % 100_000_000:08d}",
        password_hash="test-password-hash",
        account_status="active",
        phone_verified_at=datetime.now(UTC),
    )
    db.add(user)
    db.flush()

    profile = WorkerProfile(
        user_id=user.id,
        display_name=user.full_name,
    )
    db.add(profile)
    db.flush()

    profession = WorkerProfession(
        worker_profile_id=profile.id,
        category_id=main_category.id,
    )
    db.add(profession)
    db.flush()

    return profile


def add_worker_skill(
    db: Session,
    profile: WorkerProfile,
    main_category: Category,
    skill_category: Category,
    skill_id: UUID,
) -> WorkerSkill:
    """Add a selected skill beneath one of the worker's main professions."""
    worker_skill = WorkerSkill(
        worker_profile_id=profile.id,
        main_category_id=main_category.id,
        category_id=skill_category.id,
        skill_id=skill_id,
    )
    db.add(worker_skill)
    db.flush()

    return worker_skill


def test_normalize_name() -> None:
    display_name, name_key = normalize_name("  ＲＯＯＦＩＮＧ   ")

    assert display_name == "ROOFING"
    assert name_key == "roofing"


@pytest.mark.parametrize(
    "invalid_name",
    [
        "",
        "   ",
        "\t\n",
        "x" * 121,
    ],
)
def test_create_category_rejects_invalid_names(
    db_session: Session,
    invalid_name: str,
) -> None:
    with pytest.raises(TaxonomyValidationError):
        create_category(db_session, invalid_name)


def test_create_nested_categories(
    db_session: Session,
) -> None:
    construction = create_category(db_session, "Construction")
    roofing = create_category(
        db_session,
        "Roofing",
        parent_id=construction.id,
    )
    metal_roofing = create_category(
        db_session,
        "Metal Roofing",
        parent_id=roofing.id,
    )
    standing_seam = create_category(
        db_session,
        "Standing Seam",
        parent_id=metal_roofing.id,
    )

    assert construction.parent_id is None
    assert roofing.parent_id == construction.id
    assert metal_roofing.parent_id == roofing.id
    assert standing_seam.parent_id == metal_roofing.id


def test_duplicate_category_names_are_rejected(
    db_session: Session,
) -> None:
    create_category(db_session, "Construction")

    with pytest.raises(TaxonomyConflictError):
        create_category(db_session, " construction ")


def test_same_child_name_is_allowed_under_different_parents(
    db_session: Session,
) -> None:
    construction = create_category(db_session, "Construction")
    maintenance = create_category(db_session, "Maintenance")

    first = create_category(
        db_session,
        "General",
        parent_id=construction.id,
    )
    second = create_category(
        db_session,
        "General",
        parent_id=maintenance.id,
    )

    assert first.id != second.id
    assert first.parent_id != second.parent_id


def test_category_can_be_moved_to_another_valid_parent(
    db_session: Session,
) -> None:
    construction = create_category(db_session, "Construction")
    maintenance = create_category(db_session, "Maintenance")

    roofing = create_category(
        db_session,
        "Roofing",
        parent_id=construction.id,
    )
    metal_roofing = create_category(
        db_session,
        "Metal Roofing",
        parent_id=roofing.id,
    )

    moved = move_category(
        db_session,
        roofing.id,
        maintenance.id,
    )

    assert moved.parent_id == maintenance.id
    assert metal_roofing.parent_id == roofing.id


def test_category_can_be_moved_back_to_root(
    db_session: Session,
) -> None:
    construction = create_category(db_session, "Construction")
    roofing = create_category(
        db_session,
        "Roofing",
        parent_id=construction.id,
    )

    moved = move_category(
        db_session,
        roofing.id,
        None,
    )

    assert moved.parent_id is None


def test_category_cannot_be_moved_under_itself(
    db_session: Session,
) -> None:
    construction = create_category(db_session, "Construction")

    with pytest.raises(TaxonomyValidationError):
        move_category(
            db_session,
            construction.id,
            construction.id,
        )


def test_category_cannot_be_moved_under_its_descendant(
    db_session: Session,
) -> None:
    construction = create_category(db_session, "Construction")
    roofing = create_category(
        db_session,
        "Roofing",
        parent_id=construction.id,
    )
    metal_roofing = create_category(
        db_session,
        "Metal Roofing",
        parent_id=roofing.id,
    )

    with pytest.raises(TaxonomyValidationError):
        move_category(
            db_session,
            construction.id,
            metal_roofing.id,
        )


def test_category_cannot_be_moved_under_inactive_parent(
    db_session: Session,
) -> None:
    construction = create_category(db_session, "Construction")
    maintenance = create_category(db_session, "Maintenance")
    roofing = create_category(
        db_session,
        "Roofing",
        parent_id=construction.id,
    )

    set_category_active(db_session, maintenance.id, False)

    with pytest.raises(TaxonomyValidationError):
        move_category(
            db_session,
            roofing.id,
            maintenance.id,
        )


def test_category_cannot_be_moved_under_parent_with_inactive_ancestor(
    db_session: Session,
) -> None:
    construction = create_category(db_session, "Construction")
    maintenance = create_category(
        db_session,
        "Maintenance",
        parent_id=construction.id,
    )
    roofing = create_category(
        db_session,
        "Roofing",
        parent_id=maintenance.id,
    )
    plumbing = create_category(
        db_session,
        "Plumbing",
        parent_id=maintenance.id,
    )

    set_category_active(db_session, construction.id, False)

    with pytest.raises(TaxonomyValidationError):
        move_category(
            db_session,
            roofing.id,
            plumbing.id,
        )


def test_existing_circular_hierarchy_is_detected(
    db_session: Session,
) -> None:
    construction = create_category(db_session, "Construction")
    roofing = create_category(
        db_session,
        "Roofing",
        parent_id=construction.id,
    )

    # Simulate corrupted data introduced outside the taxonomy service.
    db_session.execute(
        update(Category).where(Category.id == construction.id).values(parent_id=roofing.id),
        execution_options={"synchronize_session": False},
    )
    db_session.expire_all()

    with pytest.raises(TaxonomyConflictError):
        create_category(
            db_session,
            "Metal Roofing",
            parent_id=construction.id,
        )


def test_inactive_ancestor_blocks_new_categories_and_reactivation(
    db_session: Session,
) -> None:
    construction = create_category(db_session, "Construction")
    roofing = create_category(
        db_session,
        "Roofing",
        parent_id=construction.id,
    )

    set_category_active(db_session, construction.id, False)

    with pytest.raises(TaxonomyValidationError):
        create_category(
            db_session,
            "Metal Roofing",
            parent_id=roofing.id,
        )

    with pytest.raises(TaxonomyValidationError):
        set_category_active(db_session, roofing.id, True)


def test_category_with_children_cannot_be_deleted(
    db_session: Session,
) -> None:
    construction = create_category(db_session, "Construction")
    create_category(
        db_session,
        "Roofing",
        parent_id=construction.id,
    )

    with pytest.raises(TaxonomyConflictError):
        delete_category(db_session, construction.id)


def test_category_without_children_can_be_deleted(
    db_session: Session,
) -> None:
    category = create_category(db_session, "Temporary Category")

    delete_category(db_session, category.id)


def test_shared_skill_can_be_associated_with_multiple_categories(
    db_session: Session,
) -> None:
    construction = create_category(db_session, "Construction")
    maintenance = create_category(db_session, "Maintenance")
    welding = create_skill(db_session, "Welding")

    associate_skill(db_session, construction.id, welding.id)
    associate_skill(db_session, maintenance.id, welding.id)

    assert welding in construction.skills
    assert welding in maintenance.skills
    assert len(welding.categories) == 2


def test_associated_skill_cannot_be_deleted(
    db_session: Session,
) -> None:
    construction = create_category(db_session, "Construction")
    welding = create_skill(db_session, "Welding")

    associate_skill(db_session, construction.id, welding.id)

    with pytest.raises(TaxonomyConflictError):
        delete_skill(db_session, welding.id)


def test_skill_can_be_deleted_after_all_associations_are_removed(
    db_session: Session,
) -> None:
    construction = create_category(db_session, "Construction")
    maintenance = create_category(db_session, "Maintenance")
    welding = create_skill(db_session, "Welding")

    associate_skill(db_session, construction.id, welding.id)
    associate_skill(db_session, maintenance.id, welding.id)

    disassociate_skill(db_session, construction.id, welding.id)
    disassociate_skill(db_session, maintenance.id, welding.id)

    delete_skill(db_session, welding.id)

    with pytest.raises(TaxonomyNotFoundError):
        delete_skill(db_session, welding.id)


def test_category_can_be_renamed_without_changing_its_identity(
    db_session: Session,
) -> None:
    category = create_category(db_session, "Electrical")

    updated = update_category(
        db_session,
        category.id,
        name="Electrical Installation",
    )

    assert updated.id == category.id
    assert updated.name == "Electrical Installation"
    assert updated.name_key == "electrical installation"


def test_selected_main_profession_cannot_be_moved(
    db_session: Session,
) -> None:
    construction = create_category(db_session, "Construction")
    maintenance = create_category(db_session, "Maintenance")
    create_worker_profession(db_session, construction)

    with pytest.raises(TaxonomyConflictError, match="main profession"):
        move_category(db_session, construction.id, maintenance.id)


def test_category_subtree_with_worker_selected_skill_cannot_be_moved(
    db_session: Session,
) -> None:
    construction = create_category(db_session, "Construction")
    maintenance = create_category(db_session, "Maintenance")
    roofing = create_category(db_session, "Roofing", parent_id=construction.id)
    metal_roofing = create_category(db_session, "Metal Roofing", parent_id=roofing.id)
    skill = create_skill(db_session, "Standing Seam Installation")
    associate_skill(db_session, metal_roofing.id, skill.id)

    profile = create_worker_profession(db_session, construction)
    add_worker_skill(db_session, profile, construction, metal_roofing, skill.id)

    with pytest.raises(TaxonomyConflictError, match="worker-selected skills"):
        move_category(db_session, roofing.id, maintenance.id)


def test_category_selected_as_main_profession_cannot_be_deleted(
    db_session: Session,
) -> None:
    construction = create_category(db_session, "Construction")
    create_worker_profession(db_session, construction)

    with pytest.raises(TaxonomyConflictError, match="main profession"):
        delete_category(db_session, construction.id)


def test_category_with_worker_selected_skill_cannot_be_deleted(
    db_session: Session,
) -> None:
    construction = create_category(db_session, "Construction")
    masonry = create_category(db_session, "Masonry", parent_id=construction.id)
    skill = create_skill(db_session, "Block Laying")
    associate_skill(db_session, masonry.id, skill.id)

    profile = create_worker_profession(db_session, construction)
    add_worker_skill(db_session, profile, construction, masonry, skill.id)

    with pytest.raises(TaxonomyConflictError, match="worker-selected skills"):
        delete_category(db_session, masonry.id)


def test_worker_selected_category_skill_association_cannot_be_removed(
    db_session: Session,
) -> None:
    construction = create_category(db_session, "Construction")
    skill = create_skill(db_session, "Masonry")
    associate_skill(db_session, construction.id, skill.id)

    profile = create_worker_profession(db_session, construction)
    add_worker_skill(db_session, profile, construction, construction, skill.id)

    with pytest.raises(TaxonomyConflictError, match="selected by workers"):
        disassociate_skill(db_session, construction.id, skill.id)


def test_worker_selected_skill_cannot_be_deleted(
    db_session: Session,
) -> None:
    construction = create_category(db_session, "Construction")
    skill = create_skill(db_session, "Masonry")
    associate_skill(db_session, construction.id, skill.id)

    profile = create_worker_profession(db_session, construction)
    add_worker_skill(db_session, profile, construction, construction, skill.id)

    with pytest.raises(TaxonomyConflictError, match="selected by workers"):
        delete_skill(db_session, skill.id)
