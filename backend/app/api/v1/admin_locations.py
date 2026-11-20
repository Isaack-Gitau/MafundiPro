from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.dependencies.authorization import AdminUser
from app.api.dependencies.database import DatabaseSession
from app.core.errors import AppError
from app.models.location import Location
from app.schemas.locations import (
    LocationActivation,
    LocationCreate,
    LocationMove,
    LocationRead,
    LocationTreeRead,
    LocationUpdate,
)
from app.services.locations import (
    LocationConflictError,
    LocationNotFoundError,
    LocationValidationError,
    create_location,
    delete_location,
    move_location,
    set_location_active,
    update_location,
)

router = APIRouter()


def _run_location_write[T](
    db: DatabaseSession,
    operation: Callable[[], T],
) -> T:
    """Execute a location operation and translate domain/database errors."""
    try:
        result = operation()
        db.commit()
        return result
    except LocationNotFoundError as exc:
        db.rollback()
        raise AppError(
            status_code=404,
            code="LOCATION_RESOURCE_NOT_FOUND",
            message=str(exc),
        ) from exc
    except LocationValidationError as exc:
        db.rollback()
        raise AppError(
            status_code=422,
            code="INVALID_LOCATION_INPUT",
            message=str(exc),
        ) from exc
    except LocationConflictError as exc:
        db.rollback()
        raise AppError(
            status_code=409,
            code="LOCATION_CONFLICT",
            message=str(exc),
        ) from exc
    except IntegrityError as exc:
        db.rollback()
        raise AppError(
            status_code=409,
            code="LOCATION_CONFLICT",
            message="The location operation conflicts with existing data.",
        ) from exc


@router.get(
    "",
    response_model=list[LocationTreeRead],
    summary="List all locations for administration",
)
def list_admin_locations(
    _: AdminUser,
    db: DatabaseSession,
) -> list[LocationTreeRead]:
    """Return the complete location hierarchy, including inactive records."""
    locations = db.scalars(select(Location).order_by(Location.display_order, Location.name)).all()

    nodes = {
        location.id: LocationTreeRead.model_validate(
            {
                **LocationRead.model_validate(location).model_dump(),
                "children": [],
            }
        )
        for location in locations
    }

    roots: list[LocationTreeRead] = []

    for location in locations:
        node = nodes[location.id]

        if location.parent_id is None:
            roots.append(node)
        else:
            parent = nodes.get(location.parent_id)
            if parent is not None:
                parent.children.append(node)

    return roots


@router.post(
    "",
    response_model=LocationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a location",
)
def admin_create_location(
    payload: LocationCreate,
    _: AdminUser,
    db: DatabaseSession,
) -> Location:
    return _run_location_write(
        db,
        lambda: create_location(
            db,
            payload.location_type,
            payload.name,
            parent_id=payload.parent_id,
            country_code=payload.country_code,
            description=payload.description,
            latitude=payload.latitude,
            longitude=payload.longitude,
            display_order=payload.display_order,
        ),
    )


@router.patch(
    "/{location_id}",
    response_model=LocationRead,
    summary="Update a location",
)
def admin_update_location(
    location_id: UUID,
    payload: LocationUpdate,
    _: AdminUser,
    db: DatabaseSession,
) -> Location:
    updates = payload.model_dump(exclude_unset=True)

    return _run_location_write(
        db,
        lambda: update_location(db, location_id, **updates),
    )


@router.put(
    "/{location_id}/parent",
    response_model=LocationRead,
    summary="Move a location within the hierarchy",
)
def admin_move_location(
    location_id: UUID,
    payload: LocationMove,
    _: AdminUser,
    db: DatabaseSession,
) -> Location:
    return _run_location_write(
        db,
        lambda: move_location(db, location_id, payload.parent_id),
    )


@router.patch(
    "/{location_id}/activation",
    response_model=LocationRead,
    summary="Activate or deactivate a location",
)
def admin_set_location_activation(
    location_id: UUID,
    payload: LocationActivation,
    _: AdminUser,
    db: DatabaseSession,
) -> Location:
    return _run_location_write(
        db,
        lambda: set_location_active(db, location_id, payload.is_active),
    )


@router.delete(
    "/{location_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an unused location",
)
def admin_delete_location(
    location_id: UUID,
    _: AdminUser,
    db: DatabaseSession,
) -> Response:
    _run_location_write(db, lambda: delete_location(db, location_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
