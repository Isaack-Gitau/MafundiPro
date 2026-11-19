import unicodedata
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.taxonomy import Category, Skill
from app.models.worker import WorkerProfession, WorkerSkill


class TaxonomyError(Exception):
    """Base exception for taxonomy business-rule violations."""


class TaxonomyNotFoundError(TaxonomyError):
    """Raised when a requested category or skill does not exist."""


class TaxonomyValidationError(TaxonomyError):
    """Raised when taxonomy input violates business rules."""


class TaxonomyConflictError(TaxonomyError):
    """Raised when a taxonomy operation conflicts with existing data."""


class _UnsetType:
    """Represent an omitted optional update field."""


_UNSET = _UnsetType()


def normalize_name(name: str) -> tuple[str, str]:
    """Normalize a taxonomy name into its display and comparison forms."""
    if not isinstance(name, str):
        raise TaxonomyValidationError("Name must be a string.")

    display_name = " ".join(unicodedata.normalize("NFKC", name).split())

    if not display_name:
        raise TaxonomyValidationError("Name cannot be empty.")

    if len(display_name) > 120:
        raise TaxonomyValidationError("Name cannot exceed 120 characters.")

    return display_name, display_name.casefold()


def normalize_description(description: str | None) -> str | None:
    """Normalize optional descriptions and convert blank values to None."""
    if description is None:
        return None

    if not isinstance(description, str):
        raise TaxonomyValidationError("Description must be a string.")

    normalized = description.strip()
    return normalized or None


def validate_display_order(display_order: int) -> None:
    """Ensure display ordering uses a non-negative integer."""
    if isinstance(display_order, bool) or not isinstance(display_order, int):
        raise TaxonomyValidationError("Display order must be an integer.")

    if display_order < 0:
        raise TaxonomyValidationError("Display order cannot be negative.")


def get_category_or_raise(db: Session, category_id: UUID) -> Category:
    """Retrieve a category or raise a domain-level not-found error."""
    category = db.get(Category, category_id)

    if category is None:
        raise TaxonomyNotFoundError("Category was not found.")

    return category


def get_skill_or_raise(db: Session, skill_id: UUID) -> Skill:
    """Retrieve a skill or raise a domain-level not-found error."""
    skill = db.get(Skill, skill_id)

    if skill is None:
        raise TaxonomyNotFoundError("Skill was not found.")

    return skill


def ensure_category_name_available(
    db: Session,
    name_key: str,
    parent_id: UUID | None,
    exclude_id: UUID | None = None,
) -> None:
    """Prevent duplicate names among categories with the same parent."""
    statement = select(Category.id).where(Category.name_key == name_key)

    if parent_id is None:
        statement = statement.where(Category.parent_id.is_(None))
    else:
        statement = statement.where(Category.parent_id == parent_id)

    if exclude_id is not None:
        statement = statement.where(Category.id != exclude_id)

    if db.scalar(statement) is not None:
        raise TaxonomyConflictError("A category with this name already exists at this level.")


def ensure_skill_name_available(
    db: Session,
    name_key: str,
    exclude_id: UUID | None = None,
) -> None:
    """Prevent duplicate skill names across the taxonomy."""
    statement = select(Skill.id).where(Skill.name_key == name_key)

    if exclude_id is not None:
        statement = statement.where(Skill.id != exclude_id)

    if db.scalar(statement) is not None:
        raise TaxonomyConflictError("A skill with this name already exists.")


def validate_parent(
    db: Session,
    parent_id: UUID | None,
    category_id: UUID | None = None,
) -> Category | None:
    """Validate the complete ancestor chain and prevent circular hierarchies."""
    if parent_id is None:
        return None

    if category_id is not None and parent_id == category_id:
        raise TaxonomyValidationError("A category cannot be its own parent.")

    parent = get_category_or_raise(db, parent_id)
    visited: set[UUID] = set()
    current: Category | None = parent

    while current is not None:
        if current.id in visited:
            raise TaxonomyConflictError("An existing circular category hierarchy was detected.")

        visited.add(current.id)

        if not current.is_active:
            raise TaxonomyValidationError(
                "A category cannot be placed beneath an inactive category."
            )

        if category_id is not None and current.id == category_id:
            raise TaxonomyValidationError("Moving this category would create a circular hierarchy.")

        if current.parent_id is None:
            break

        current = get_category_or_raise(db, current.parent_id)

    return parent


def validate_category_active_chain(
    db: Session,
    category_id: UUID,
) -> Category:
    """Ensure a category and every ancestor are active."""
    category = get_category_or_raise(db, category_id)
    visited: set[UUID] = set()
    current: Category | None = category

    while current is not None:
        if current.id in visited:
            raise TaxonomyConflictError("An existing circular category hierarchy was detected.")

        visited.add(current.id)

        if not current.is_active:
            raise TaxonomyValidationError(
                "This operation requires an active category and active ancestors."
            )

        if current.parent_id is None:
            break

        current = get_category_or_raise(db, current.parent_id)

    return category


def create_category(
    db: Session,
    name: str,
    *,
    parent_id: UUID | None = None,
    description: str | None = None,
    display_order: int = 0,
) -> Category:
    """Create a root category or a nested category."""
    display_name, name_key = normalize_name(name)
    normalized_description = normalize_description(description)
    validate_display_order(display_order)
    validate_parent(db, parent_id)

    ensure_category_name_available(db, name_key, parent_id)

    category = Category(
        name=display_name,
        name_key=name_key,
        parent_id=parent_id,
        description=normalized_description,
        display_order=display_order,
        is_active=True,
    )

    db.add(category)
    db.flush()

    return category


def update_category(
    db: Session,
    category_id: UUID,
    *,
    name: str | None = None,
    description: str | None | _UnsetType = _UNSET,
    display_order: int | None = None,
) -> Category:
    """Update a category while distinguishing omitted and cleared descriptions."""
    category = get_category_or_raise(db, category_id)

    if name is not None:
        display_name, name_key = normalize_name(name)
        ensure_category_name_available(
            db,
            name_key,
            category.parent_id,
            exclude_id=category.id,
        )
        category.name = display_name
        category.name_key = name_key

    if not isinstance(description, _UnsetType):
        category.description = normalize_description(description)

    if display_order is not None:
        validate_display_order(display_order)
        category.display_order = display_order

    db.flush()
    return category


def _category_subtree_ids(db: Session, category_id: UUID):
    """Return a recursive SQL query containing a category and all descendants."""
    subtree = (
        select(Category.id.label("id"))
        .where(Category.id == category_id)
        .cte(name="category_subtree", recursive=True)
    )

    return subtree.union(select(Category.id.label("id")).where(Category.parent_id == subtree.c.id))


def _ensure_category_subtree_has_no_worker_selections(
    db: Session,
    category_id: UUID,
) -> None:
    """Prevent moving a category subtree containing selected worker professions or skills."""
    subtree_ids = _category_subtree_ids(db, category_id)

    selected_profession = db.scalar(
        select(WorkerProfession.id).where(WorkerProfession.category_id == category_id).limit(1)
    )

    if selected_profession is not None:
        raise TaxonomyConflictError(
            "A category selected as a worker's main profession cannot be moved."
        )

    selected_skill = db.scalar(
        select(WorkerSkill.id).where(WorkerSkill.category_id.in_(select(subtree_ids.c.id))).limit(1)
    )

    if selected_skill is not None:
        raise TaxonomyConflictError("A category containing worker-selected skills cannot be moved.")


def _ensure_category_has_no_worker_references(
    db: Session,
    category_id: UUID,
) -> None:
    """Prevent deleting a category referenced by worker selections."""
    profession_exists = db.scalar(
        select(WorkerProfession.id).where(WorkerProfession.category_id == category_id).limit(1)
    )

    if profession_exists is not None:
        raise TaxonomyConflictError(
            "A category selected as a worker's main profession cannot be deleted."
        )

    skill_exists = db.scalar(
        select(WorkerSkill.id).where(WorkerSkill.category_id == category_id).limit(1)
    )

    if skill_exists is not None:
        raise TaxonomyConflictError(
            "A category containing worker-selected skills cannot be deleted."
        )


def move_category(
    db: Session,
    category_id: UUID,
    new_parent_id: UUID | None,
) -> Category:
    """Move a category while preserving hierarchy integrity."""
    category = get_category_or_raise(db, category_id)

    if category.parent_id == new_parent_id:
        return category

    _ensure_category_subtree_has_no_worker_selections(db, category.id)

    validate_parent(db, new_parent_id, category_id=category.id)
    ensure_category_name_available(
        db,
        category.name_key,
        new_parent_id,
        exclude_id=category.id,
    )

    category.parent_id = new_parent_id
    db.flush()

    return category


def set_category_active(
    db: Session,
    category_id: UUID,
    is_active: bool,
) -> Category:
    """Activate or deactivate a category without deleting its history."""
    if not isinstance(is_active, bool):
        raise TaxonomyValidationError("Category activation state must be a boolean.")

    category = get_category_or_raise(db, category_id)

    if is_active and category.parent_id is not None:
        validate_parent(db, category.parent_id)

    category.is_active = is_active
    db.flush()

    return category


def delete_category(db: Session, category_id: UUID) -> None:
    """Delete a category only when it has no children or worker references."""
    category = get_category_or_raise(db, category_id)

    _ensure_category_has_no_worker_references(db, category.id)

    child_exists = db.scalar(select(Category.id).where(Category.parent_id == category.id).limit(1))

    if child_exists is not None:
        raise TaxonomyConflictError("A category with child categories cannot be deleted.")

    if category.skills:
        raise TaxonomyConflictError(
            "A category with associated skills cannot be deleted. "
            "Remove its skill associations first."
        )

    db.delete(category)
    db.flush()


def create_skill(
    db: Session,
    name: str,
    *,
    description: str | None = None,
) -> Skill:
    """Create a reusable skill."""
    display_name, name_key = normalize_name(name)
    normalized_description = normalize_description(description)
    ensure_skill_name_available(db, name_key)

    skill = Skill(
        name=display_name,
        name_key=name_key,
        description=normalized_description,
        is_active=True,
    )

    db.add(skill)
    db.flush()

    return skill


def update_skill(
    db: Session,
    skill_id: UUID,
    *,
    name: str | None = None,
    description: str | None | _UnsetType = _UNSET,
) -> Skill:
    """Update a reusable skill while supporting explicit description clearing."""
    skill = get_skill_or_raise(db, skill_id)

    if name is not None:
        display_name, name_key = normalize_name(name)
        ensure_skill_name_available(
            db,
            name_key,
            exclude_id=skill.id,
        )
        skill.name = display_name
        skill.name_key = name_key

    if not isinstance(description, _UnsetType):
        skill.description = normalize_description(description)

    db.flush()
    return skill


def set_skill_active(
    db: Session,
    skill_id: UUID,
    is_active: bool,
) -> Skill:
    """Activate or deactivate a skill without deleting its history."""
    if not isinstance(is_active, bool):
        raise TaxonomyValidationError("Skill activation state must be a boolean.")

    skill = get_skill_or_raise(db, skill_id)
    skill.is_active = is_active
    db.flush()

    return skill


def associate_skill(
    db: Session,
    category_id: UUID,
    skill_id: UUID,
) -> Category:
    """Associate an active reusable skill with an active category."""
    category = validate_category_active_chain(db, category_id)
    skill = get_skill_or_raise(db, skill_id)

    if not skill.is_active:
        raise TaxonomyValidationError("An inactive skill cannot be associated with a category.")

    if skill in category.skills:
        return category

    category.skills.append(skill)
    db.flush()

    return category


def disassociate_skill(
    db: Session,
    category_id: UUID,
    skill_id: UUID,
) -> Category:
    """Remove a skill association without deleting the shared skill."""
    category = get_category_or_raise(db, category_id)
    skill = get_skill_or_raise(db, skill_id)

    if skill in category.skills:
        selected_skill = db.scalar(
            select(WorkerSkill.id)
            .where(
                WorkerSkill.category_id == category.id,
                WorkerSkill.skill_id == skill.id,
            )
            .limit(1)
        )

        if selected_skill is not None:
            raise TaxonomyConflictError(
                "A category-skill association selected by workers cannot be removed."
            )

        category.skills.remove(skill)
        db.flush()

    return category


def delete_skill(db: Session, skill_id: UUID) -> None:
    """Delete a skill only when it has no worker or category references."""
    skill = get_skill_or_raise(db, skill_id)

    selected_skill = db.scalar(
        select(WorkerSkill.id).where(WorkerSkill.skill_id == skill.id).limit(1)
    )

    if selected_skill is not None:
        raise TaxonomyConflictError("A skill selected by workers cannot be deleted.")

    if skill.categories:
        raise TaxonomyConflictError(
            "A skill associated with categories cannot be deleted. "
            "Remove its category associations first."
        )

    db.delete(skill)
    db.flush()
