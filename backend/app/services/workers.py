from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.identity import User
from app.models.taxonomy import Category, Skill, category_skills
from app.models.worker import WorkerProfession, WorkerProfile, WorkerSkill
from app.services.taxonomy import (
    TaxonomyValidationError,
    get_category_or_raise,
    get_skill_or_raise,
    validate_category_active_chain,
)


class WorkerProfileError(Exception):
    """Base exception for worker profile operations."""


class WorkerProfileNotFoundError(WorkerProfileError):
    """Raised when the authenticated user has no worker profile."""


class WorkerProfileConflictError(WorkerProfileError):
    """Raised when a worker profile already exists or cannot be created."""


def create_worker_profile(db: Session, user_id: UUID) -> WorkerProfile:
    """Create one worker profile for an eligible authenticated account."""
    user = db.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    if user is None or user.account_status != "active" or user.phone_verified_at is None:
        raise WorkerProfileNotFoundError("The eligible account was not found.")

    existing_profile = db.scalar(select(WorkerProfile.id).where(WorkerProfile.user_id == user_id))

    if existing_profile is not None:
        raise WorkerProfileConflictError("A worker profile already exists.")

    profile = WorkerProfile(
        user_id=user.id,
        display_name=user.full_name,
        availability="unavailable",
    )

    try:
        with db.begin_nested():
            db.add(profile)
            db.flush()
    except IntegrityError as exc:
        diagnostic = getattr(exc.orig, "diag", None)
        constraint_name = getattr(diagnostic, "constraint_name", None)

        if constraint_name == "uq_worker_profiles_user_id":
            raise WorkerProfileConflictError("A worker profile already exists.") from exc

        raise

    return profile


def get_worker_profile(db: Session, user_id: UUID) -> WorkerProfile:
    """Retrieve the worker profile belonging to the authenticated account."""
    profile = db.scalar(select(WorkerProfile).where(WorkerProfile.user_id == user_id))

    if profile is None:
        raise WorkerProfileNotFoundError("Worker profile was not found.")

    return profile


def _get_locked_worker_profile(db: Session, user_id: UUID) -> WorkerProfile:
    """Retrieve and lock the authenticated worker's profile for selection changes."""
    profile = db.scalar(
        select(WorkerProfile)
        .where(WorkerProfile.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    if profile is None:
        raise WorkerProfileNotFoundError("Worker profile was not found.")

    return profile


def _get_root_category(db: Session, category: Category) -> Category:
    """Resolve a category's root while validating its complete active hierarchy."""
    current = validate_category_active_chain(db, category.id)

    while current.parent_id is not None:
        current = get_category_or_raise(db, current.parent_id)

    return current


def _validate_main_profession(db: Session, category_id: UUID) -> Category:
    """Require an active root category as a main profession."""
    category = validate_category_active_chain(db, category_id)

    if category.parent_id is not None:
        raise TaxonomyValidationError(
            "Only active root categories can be selected as main professions."
        )

    return category


def _validate_skill_selection(
    db: Session,
    main_category_id: UUID,
    category_id: UUID,
    skill_id: UUID,
) -> tuple[Category, Skill]:
    """Validate a skill, its category, and its association with a profession."""
    main_category = _validate_main_profession(db, main_category_id)
    category = validate_category_active_chain(db, category_id)
    root_category = _get_root_category(db, category)

    if root_category.id != main_category.id:
        raise TaxonomyValidationError(
            "The skill category must belong to the selected main profession."
        )

    skill = get_skill_or_raise(db, skill_id)

    if not skill.is_active:
        raise TaxonomyValidationError("Only active skills can be selected.")

    association_exists = db.scalar(
        select(category_skills.c.category_id).where(
            category_skills.c.category_id == category_id,
            category_skills.c.skill_id == skill_id,
        )
    )

    if association_exists is None:
        raise TaxonomyValidationError(
            "The selected skill is not associated with the specified category."
        )

    return category, skill


def get_worker_profession_selections(db: Session, user_id: UUID) -> dict[str, list]:
    """Retrieve the worker's selected professions and their selected skills."""
    profile = get_worker_profile(db, user_id)

    professions = db.execute(
        select(WorkerProfession, Category)
        .join(Category, Category.id == WorkerProfession.category_id)
        .where(WorkerProfession.worker_profile_id == profile.id)
        .order_by(Category.display_order, Category.name_key, Category.id)
    ).all()

    result = []

    for profession, category in professions:
        selected_skills = db.execute(
            select(WorkerSkill, Category, Skill)
            .join(Category, Category.id == WorkerSkill.category_id)
            .join(Skill, Skill.id == WorkerSkill.skill_id)
            .where(
                WorkerSkill.worker_profile_id == profile.id,
                WorkerSkill.main_category_id == profession.category_id,
            )
            .order_by(Category.display_order, Category.name_key, Skill.name_key, Skill.id)
        ).all()

        result.append(
            {
                "category_id": profession.category_id,
                "category_name": category.name,
                "skills": [
                    {
                        "category_id": selected_category.id,
                        "category_name": selected_category.name,
                        "skill_id": skill.id,
                        "skill_name": skill.name,
                    }
                    for _, selected_category, skill in selected_skills
                ],
            }
        )

    return {"professions": result}


def replace_worker_professions(
    db: Session,
    user_id: UUID,
    category_ids: list[UUID],
) -> dict[str, list]:
    """Atomically replace main professions while retaining skills for retained professions."""
    if len(category_ids) > 3:
        raise TaxonomyValidationError("A worker can select no more than three main professions.")

    if len(category_ids) != len(set(category_ids)):
        raise TaxonomyValidationError("A profession cannot be selected more than once.")

    profile = _get_locked_worker_profile(db, user_id)

    validated_categories = [
        _validate_main_profession(db, category_id) for category_id in category_ids
    ]
    requested_ids = {category.id for category in validated_categories}

    existing_professions = db.scalars(
        select(WorkerProfession)
        .where(WorkerProfession.worker_profile_id == profile.id)
        .with_for_update()
    ).all()

    existing_by_category = {
        profession.category_id: profession for profession in existing_professions
    }

    for category_id, profession in existing_by_category.items():
        if category_id not in requested_ids:
            db.delete(profession)

    for category in validated_categories:
        if category.id not in existing_by_category:
            db.add(
                WorkerProfession(
                    worker_profile_id=profile.id,
                    category_id=category.id,
                )
            )

    db.flush()

    return get_worker_profession_selections(db, user_id)


def replace_worker_skills(
    db: Session,
    user_id: UUID,
    main_category_id: UUID,
    selections: list[dict[str, UUID]],
) -> dict[str, list]:
    """Atomically replace selected skills for one existing main profession."""
    profile = _get_locked_worker_profile(db, user_id)

    profession = db.scalar(
        select(WorkerProfession)
        .where(
            WorkerProfession.worker_profile_id == profile.id,
            WorkerProfession.category_id == main_category_id,
        )
        .with_for_update()
    )

    if profession is None:
        raise TaxonomyValidationError(
            "The specified category is not one of your selected main professions."
        )

    skill_ids = [selection["skill_id"] for selection in selections]

    if len(skill_ids) != len(set(skill_ids)):
        raise TaxonomyValidationError(
            "A skill cannot be selected more than once under the same profession."
        )

    validated_selections = [
        _validate_skill_selection(
            db,
            main_category_id,
            selection["category_id"],
            selection["skill_id"],
        )
        for selection in selections
    ]

    existing_skills = db.scalars(
        select(WorkerSkill)
        .where(
            WorkerSkill.worker_profile_id == profile.id,
            WorkerSkill.main_category_id == main_category_id,
        )
        .with_for_update()
    ).all()

    for selected_skill in existing_skills:
        db.delete(selected_skill)

    # Delete old rows before inserting replacements to avoid transient
    # uniqueness conflicts when a worker retains an existing skill.
    db.flush()

    for selection, _validated in zip(selections, validated_selections, strict=True):
        db.add(
            WorkerSkill(
                worker_profile_id=profile.id,
                main_category_id=main_category_id,
                category_id=selection["category_id"],
                skill_id=selection["skill_id"],
            )
        )

    db.flush()

    return get_worker_profession_selections(db, user_id)


def update_worker_profile(
    db: Session,
    user_id: UUID,
    updates: dict[str, object],
) -> WorkerProfile:
    """Update only the supplied fields on the authenticated user's profile."""
    profile = get_worker_profile(db, user_id)

    for field, value in updates.items():
        setattr(profile, field, value)

    db.flush()

    return profile
