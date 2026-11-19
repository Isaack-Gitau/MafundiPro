from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.api.dependencies.authorization import AdminUser
from app.api.dependencies.database import DatabaseSession
from app.core.errors import AppError
from app.models.taxonomy import Category, Skill
from app.schemas.taxonomy import (
    CategoryActivation,
    CategoryCreate,
    CategoryMove,
    CategoryRead,
    CategoryTreeRead,
    CategoryUpdate,
    SkillActivation,
    SkillAssociationCreate,
    SkillCreate,
    SkillRead,
    SkillUpdate,
)
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
    set_category_active,
    set_skill_active,
    update_category,
    update_skill,
)

router = APIRouter()


def _run_taxonomy_write[T](
    db: DatabaseSession,
    operation: Callable[[], T],
) -> T:
    """Execute a taxonomy operation and translate domain/database errors."""
    try:
        result = operation()
        db.commit()
        return result
    except TaxonomyNotFoundError as exc:
        db.rollback()
        raise AppError(
            status_code=404,
            code="TAXONOMY_RESOURCE_NOT_FOUND",
            message=str(exc),
        ) from exc
    except TaxonomyValidationError as exc:
        db.rollback()
        raise AppError(
            status_code=422,
            code="INVALID_TAXONOMY_INPUT",
            message=str(exc),
        ) from exc
    except TaxonomyConflictError as exc:
        db.rollback()
        raise AppError(
            status_code=409,
            code="TAXONOMY_CONFLICT",
            message=str(exc),
        ) from exc
    except IntegrityError as exc:
        db.rollback()
        raise AppError(
            status_code=409,
            code="TAXONOMY_CONFLICT",
            message="The taxonomy operation conflicts with existing data.",
        ) from exc


@router.get(
    "/categories",
    response_model=list[CategoryTreeRead],
    summary="List all categories for administration",
)
def list_admin_categories(
    _: AdminUser,
    db: DatabaseSession,
) -> list[CategoryTreeRead]:
    """Return the complete category tree, including inactive records."""
    statement = (
        select(Category)
        .options(selectinload(Category.skills))
        .order_by(Category.display_order, Category.name)
    )
    categories = db.scalars(statement).all()

    nodes = {category.id: CategoryTreeRead.model_validate(category) for category in categories}

    roots: list[CategoryTreeRead] = []

    for category in categories:
        node = nodes[category.id]

        if category.parent_id is None:
            roots.append(node)
        else:
            parent = nodes.get(category.parent_id)
            if parent is not None:
                parent.children.append(node)

    return roots


@router.post(
    "/categories",
    response_model=CategoryRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a category",
)
def admin_create_category(
    payload: CategoryCreate,
    _: AdminUser,
    db: DatabaseSession,
) -> Category:
    return _run_taxonomy_write(
        db,
        lambda: create_category(
            db,
            payload.name,
            parent_id=payload.parent_id,
            description=payload.description,
            display_order=payload.display_order,
        ),
    )


@router.patch(
    "/categories/{category_id}",
    response_model=CategoryRead,
    summary="Update a category",
)
def admin_update_category(
    category_id: UUID,
    payload: CategoryUpdate,
    _: AdminUser,
    db: DatabaseSession,
) -> Category:
    updates = payload.model_dump(exclude_unset=True)

    return _run_taxonomy_write(
        db,
        lambda: update_category(db, category_id, **updates),
    )


@router.put(
    "/categories/{category_id}/parent",
    response_model=CategoryRead,
    summary="Move a category within the hierarchy",
)
def admin_move_category(
    category_id: UUID,
    payload: CategoryMove,
    _: AdminUser,
    db: DatabaseSession,
) -> Category:
    return _run_taxonomy_write(
        db,
        lambda: move_category(db, category_id, payload.parent_id),
    )


@router.patch(
    "/categories/{category_id}/activation",
    response_model=CategoryRead,
    summary="Activate or deactivate a category",
)
def admin_set_category_activation(
    category_id: UUID,
    payload: CategoryActivation,
    _: AdminUser,
    db: DatabaseSession,
) -> Category:
    return _run_taxonomy_write(
        db,
        lambda: set_category_active(db, category_id, payload.is_active),
    )


@router.delete(
    "/categories/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an unused category",
)
def admin_delete_category(
    category_id: UUID,
    _: AdminUser,
    db: DatabaseSession,
) -> Response:
    _run_taxonomy_write(db, lambda: delete_category(db, category_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/skills",
    response_model=list[SkillRead],
    summary="List all skills for administration",
)
def list_admin_skills(
    _: AdminUser,
    db: DatabaseSession,
) -> list[Skill]:
    statement = select(Skill).order_by(Skill.name)
    return list(db.scalars(statement).all())


@router.post(
    "/skills",
    response_model=SkillRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a skill",
)
def admin_create_skill(
    payload: SkillCreate,
    _: AdminUser,
    db: DatabaseSession,
) -> Skill:
    return _run_taxonomy_write(
        db,
        lambda: create_skill(
            db,
            payload.name,
            description=payload.description,
        ),
    )


@router.patch(
    "/skills/{skill_id}",
    response_model=SkillRead,
    summary="Update a skill",
)
def admin_update_skill(
    skill_id: UUID,
    payload: SkillUpdate,
    _: AdminUser,
    db: DatabaseSession,
) -> Skill:
    updates = payload.model_dump(exclude_unset=True)

    return _run_taxonomy_write(
        db,
        lambda: update_skill(db, skill_id, **updates),
    )


@router.patch(
    "/skills/{skill_id}/activation",
    response_model=SkillRead,
    summary="Activate or deactivate a skill",
)
def admin_set_skill_activation(
    skill_id: UUID,
    payload: SkillActivation,
    _: AdminUser,
    db: DatabaseSession,
) -> Skill:
    return _run_taxonomy_write(
        db,
        lambda: set_skill_active(db, skill_id, payload.is_active),
    )


@router.delete(
    "/skills/{skill_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an unused skill",
)
def admin_delete_skill(
    skill_id: UUID,
    _: AdminUser,
    db: DatabaseSession,
) -> Response:
    _run_taxonomy_write(db, lambda: delete_skill(db, skill_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/categories/{category_id}/skills",
    response_model=CategoryRead,
    summary="Associate a skill with a category",
)
def admin_associate_skill(
    category_id: UUID,
    payload: SkillAssociationCreate,
    _: AdminUser,
    db: DatabaseSession,
) -> Category:
    return _run_taxonomy_write(
        db,
        lambda: associate_skill(db, category_id, payload.skill_id),
    )


@router.delete(
    "/categories/{category_id}/skills/{skill_id}",
    response_model=CategoryRead,
    summary="Remove a skill association from a category",
)
def admin_disassociate_skill(
    category_id: UUID,
    skill_id: UUID,
    _: AdminUser,
    db: DatabaseSession,
) -> Category:
    return _run_taxonomy_write(
        db,
        lambda: disassociate_skill(db, category_id, skill_id),
    )
