from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import select

from app.api.dependencies.database import DatabaseSession
from app.models.location import Location
from app.schemas.locations import PublicLocationTreeRead

router = APIRouter()


@router.get(
    "/locations",
    response_model=list[PublicLocationTreeRead],
    summary="Browse public locations",
)
def list_public_locations(
    db: DatabaseSession,
) -> list[PublicLocationTreeRead]:
    """Return active locations with active ancestor chains, without coordinates."""
    locations = db.scalars(
        select(Location)
        .where(Location.is_active.is_(True))
        .order_by(Location.display_order, Location.name)
    ).all()

    nodes: dict[UUID, PublicLocationTreeRead] = {
        location.id: PublicLocationTreeRead(
            id=location.id,
            parent_id=location.parent_id,
            location_type=location.location_type,
            name=location.name,
            country_code=location.country_code,
            description=location.description,
            display_order=location.display_order,
            children=[],
        )
        for location in locations
    }

    roots: list[PublicLocationTreeRead] = []

    for location in locations:
        node = nodes[location.id]

        if location.parent_id is None:
            roots.append(node)
            continue

        parent = nodes.get(location.parent_id)

        if parent is not None:
            parent.children.append(node)

    return roots
