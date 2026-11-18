from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.dependencies.database import DatabaseSession
from app.models.taxonomy import Category, Skill
from app.schemas.taxonomy import PublicCategoryTreeRead, PublicSkillRead

router = APIRouter()


@router.get(
    "/categories",
    response_model=list[PublicCategoryTreeRead],
    summary="Browse public profession categories",
)
def list_public_categories(
    db: DatabaseSession,
) -> list[PublicCategoryTreeRead]:
    """Return active categories, nested subcategories, and active skills."""

    statement = (
        select(Category)
        .where(Category.is_active.is_(True))
        .options(selectinload(Category.skills.and_(Skill.is_active.is_(True))))
        .order_by(Category.display_order, Category.name)
    )

    categories = db.scalars(statement).all()

    nodes: dict[UUID, PublicCategoryTreeRead] = {}

    for category in categories:
        nodes[category.id] = PublicCategoryTreeRead(
            id=category.id,
            parent_id=category.parent_id,
            name=category.name,
            description=category.description,
            display_order=category.display_order,
            skills=[PublicSkillRead.model_validate(skill) for skill in category.skills],
            children=[],
        )

    roots: list[PublicCategoryTreeRead] = []

    for category in categories:
        node = nodes[category.id]

        if category.parent_id is None:
            roots.append(node)
            continue

        parent = nodes.get(category.parent_id)

        if parent is not None:
            parent.children.append(node)

    return roots
