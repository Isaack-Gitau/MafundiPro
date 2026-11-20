import re
import unicodedata
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.location import Location
from app.services.location_lock import lock_location_mutations


class LocationError(Exception):
    """Base exception for location business-rule violations."""


class LocationNotFoundError(LocationError):
    """Raised when a requested location does not exist."""


class LocationValidationError(LocationError):
    """Raised when location input violates business rules."""


class LocationConflictError(LocationError):
    """Raised when a location operation conflicts with existing data."""


class _UnsetType:
    """Represent an omitted optional update field."""


_UNSET = _UnsetType()


def normalize_location_name(name: str) -> tuple[str, str]:
    """Normalize a location name into display and comparison forms."""
    if not isinstance(name, str):
        raise LocationValidationError("Location name must be a string.")

    display_name = " ".join(unicodedata.normalize("NFKC", name).split())

    if not display_name:
        raise LocationValidationError("Location name cannot be empty.")

    if len(display_name) > 120:
        raise LocationValidationError("Location name cannot exceed 120 characters.")

    return display_name, display_name.casefold()


def normalize_location_type(location_type: str) -> str:
    """Normalize standard and custom location types to snake_case."""
    if not isinstance(location_type, str):
        raise LocationValidationError("Location type must be a string.")

    normalized = unicodedata.normalize("NFKC", location_type).strip().casefold()
    normalized = re.sub(r"[\s-]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized)

    if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", normalized):
        raise LocationValidationError(
            "Location type must be a valid snake_case identifier of at most 40 characters."
        )

    return normalized


def normalize_country_code(country_code: str | None) -> str | None:
    """Normalize and validate an optional ISO-style two-letter country code."""
    if country_code is None:
        return None

    if not isinstance(country_code, str):
        raise LocationValidationError("Country code must be a string.")

    normalized = country_code.strip().upper()

    if not re.fullmatch(r"[A-Z]{2}", normalized):
        raise LocationValidationError("Country code must contain exactly two letters.")

    return normalized


def normalize_description(description: str | None) -> str | None:
    """Normalize optional descriptions and convert blank values to None."""
    if description is None:
        return None

    if not isinstance(description, str):
        raise LocationValidationError("Description must be a string.")

    normalized = description.strip()
    return normalized or None


def validate_display_order(display_order: int) -> None:
    """Ensure display ordering uses a non-negative integer."""
    if isinstance(display_order, bool) or not isinstance(display_order, int):
        raise LocationValidationError("Display order must be an integer.")

    if display_order < 0:
        raise LocationValidationError("Display order cannot be negative.")


def normalize_coordinates(
    latitude: Decimal | int | float | None,
    longitude: Decimal | int | float | None,
) -> tuple[Decimal | None, Decimal | None]:
    """Validate optional coordinates and normalize them to Decimal values."""
    if latitude is None and longitude is None:
        return None, None

    if latitude is None or longitude is None:
        raise LocationValidationError("Latitude and longitude must be provided together.")

    try:
        normalized_latitude = Decimal(str(latitude))
        normalized_longitude = Decimal(str(longitude))
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise LocationValidationError("Coordinates must be valid numbers.") from exc

    if not normalized_latitude.is_finite() or not normalized_longitude.is_finite():
        raise LocationValidationError("Coordinates must be finite numbers.")

    if not Decimal("-90") <= normalized_latitude <= Decimal("90"):
        raise LocationValidationError("Latitude must be between -90 and 90.")

    if not Decimal("-180") <= normalized_longitude <= Decimal("180"):
        raise LocationValidationError("Longitude must be between -180 and 180.")

    return normalized_latitude, normalized_longitude


def get_location_or_raise(db: Session, location_id: UUID) -> Location:
    """Retrieve a location or raise a domain-level not-found error."""
    location = db.get(Location, location_id)

    if location is None:
        raise LocationNotFoundError("Location was not found.")

    return location


def ensure_location_name_available(
    db: Session,
    name_key: str,
    parent_id: UUID | None,
    exclude_id: UUID | None = None,
) -> None:
    """Prevent duplicate location names among siblings."""
    statement = select(Location.id).where(Location.name_key == name_key)

    if parent_id is None:
        statement = statement.where(Location.parent_id.is_(None))
    else:
        statement = statement.where(Location.parent_id == parent_id)

    if exclude_id is not None:
        statement = statement.where(Location.id != exclude_id)

    if db.scalar(statement) is not None:
        raise LocationConflictError("A location with this name already exists at this level.")


def ensure_country_code_available(
    db: Session,
    country_code: str,
    exclude_id: UUID | None = None,
) -> None:
    """Prevent a country code from being assigned to multiple countries."""
    statement = select(Location.id).where(Location.country_code == country_code)

    if exclude_id is not None:
        statement = statement.where(Location.id != exclude_id)

    if db.scalar(statement) is not None:
        raise LocationConflictError("A location with this country code already exists.")


def validate_location_parent(
    db: Session,
    parent_id: UUID | None,
    location_id: UUID | None = None,
) -> Location | None:
    """Validate the ancestor chain and prevent circular or inactive ancestry."""
    if parent_id is None:
        return None

    if location_id is not None and parent_id == location_id:
        raise LocationValidationError("A location cannot be its own parent.")

    parent = get_location_or_raise(db, parent_id)
    visited: set[UUID] = set()
    current: Location | None = parent

    while current is not None:
        if current.id in visited:
            raise LocationConflictError("An existing circular location hierarchy was detected.")

        visited.add(current.id)

        if not current.is_active:
            raise LocationValidationError(
                "A location cannot be placed beneath an inactive location."
            )

        if location_id is not None and current.id == location_id:
            raise LocationValidationError("Moving this location would create a circular hierarchy.")

        if current.parent_id is None:
            break

        current = get_location_or_raise(db, current.parent_id)

    return parent


def validate_location_structure(
    location_type: str,
    parent_id: UUID | None,
    country_code: str | None,
) -> None:
    """Enforce country-root and non-country-parent structural rules."""
    if location_type == "country":
        if parent_id is not None:
            raise LocationValidationError("A country must be a root location.")

        if country_code is None:
            raise LocationValidationError("A country requires a two-letter country code.")

        return

    if parent_id is None:
        raise LocationValidationError("Every non-country location must have a parent.")

    if country_code is not None:
        raise LocationValidationError("Only country locations may have a country code.")


def validate_active_location_chain(db: Session, location_id: UUID) -> Location:
    """Ensure a location and every ancestor are active."""
    location = get_location_or_raise(db, location_id)
    validate_location_parent(db, location.parent_id, location_id=location.id)
    if not location.is_active:
        raise LocationValidationError("This operation requires an active location.")

    return location


def create_location(
    db: Session,
    location_type: str,
    name: str,
    *,
    parent_id: UUID | None = None,
    country_code: str | None = None,
    description: str | None = None,
    latitude: Decimal | int | float | None = None,
    longitude: Decimal | int | float | None = None,
    display_order: int = 0,
) -> Location:
    """Create a country root or a nested location."""
    lock_location_mutations(db)

    normalized_type = normalize_location_type(location_type)
    display_name, name_key = normalize_location_name(name)
    normalized_country_code = normalize_country_code(country_code)
    normalized_description = normalize_description(description)
    normalized_latitude, normalized_longitude = normalize_coordinates(latitude, longitude)
    validate_display_order(display_order)
    validate_location_structure(normalized_type, parent_id, normalized_country_code)

    if parent_id is not None:
        validate_location_parent(db, parent_id)

    ensure_location_name_available(db, name_key, parent_id)

    if normalized_country_code is not None:
        ensure_country_code_available(db, normalized_country_code)

    location = Location(
        location_type=normalized_type,
        name=display_name,
        name_key=name_key,
        parent_id=parent_id,
        country_code=normalized_country_code,
        description=normalized_description,
        latitude=normalized_latitude,
        longitude=normalized_longitude,
        display_order=display_order,
        is_active=True,
    )

    db.add(location)
    db.flush()

    return location


def update_location(
    db: Session,
    location_id: UUID,
    *,
    name: str | None = None,
    description: str | None | _UnsetType = _UNSET,
    country_code: str | None | _UnsetType = _UNSET,
    latitude: Decimal | int | float | None | _UnsetType = _UNSET,
    longitude: Decimal | int | float | None | _UnsetType = _UNSET,
    display_order: int | None = None,
) -> Location:
    """Update location details while supporting explicit optional-field clearing."""
    lock_location_mutations(db)
    location = get_location_or_raise(db, location_id)

    if name is not None:
        display_name, name_key = normalize_location_name(name)
        ensure_location_name_available(
            db,
            name_key,
            location.parent_id,
            exclude_id=location.id,
        )
        location.name = display_name
        location.name_key = name_key

    if not isinstance(description, _UnsetType):
        location.description = normalize_description(description)

    if not isinstance(country_code, _UnsetType):
        normalized_code = normalize_country_code(country_code)

        if location.location_type == "country":
            if normalized_code is None:
                raise LocationValidationError("A country requires a country code.")
            ensure_country_code_available(db, normalized_code, exclude_id=location.id)
        elif normalized_code is not None:
            raise LocationValidationError("Only country locations may have a country code.")

        location.country_code = normalized_code

    if not isinstance(latitude, _UnsetType) or not isinstance(longitude, _UnsetType):
        new_latitude = location.latitude if isinstance(latitude, _UnsetType) else latitude
        new_longitude = location.longitude if isinstance(longitude, _UnsetType) else longitude
        normalized_latitude, normalized_longitude = normalize_coordinates(
            new_latitude,
            new_longitude,
        )
        location.latitude = normalized_latitude
        location.longitude = normalized_longitude

    if display_order is not None:
        validate_display_order(display_order)
        location.display_order = display_order

    db.flush()
    return location


def move_location(
    db: Session,
    location_id: UUID,
    new_parent_id: UUID | None,
) -> Location:
    """Move a location while preserving country and hierarchy integrity."""
    lock_location_mutations(db)
    location = get_location_or_raise(db, location_id)

    if location.location_type == "country":
        raise LocationValidationError("A country must remain a root location.")

    if new_parent_id is None:
        raise LocationValidationError("A non-country location must have a parent.")

    if location.parent_id == new_parent_id:
        return location

    validate_location_parent(db, new_parent_id, location_id=location.id)
    ensure_location_name_available(
        db,
        location.name_key,
        new_parent_id,
        exclude_id=location.id,
    )

    location.parent_id = new_parent_id
    db.flush()

    return location


def set_location_active(
    db: Session,
    location_id: UUID,
    is_active: bool,
) -> Location:
    """Activate or deactivate a location without deleting its history."""
    lock_location_mutations(db)

    if not isinstance(is_active, bool):
        raise LocationValidationError("Location activation state must be a boolean.")

    location = get_location_or_raise(db, location_id)

    if is_active and location.parent_id is not None:
        validate_location_parent(db, location.parent_id, location_id=location.id)

    location.is_active = is_active
    db.flush()

    return location


def delete_location(db: Session, location_id: UUID) -> None:
    """Delete a location only when it has no children or database references."""
    lock_location_mutations(db)
    location = get_location_or_raise(db, location_id)

    child_exists = db.scalar(select(Location.id).where(Location.parent_id == location.id).limit(1))

    if child_exists is not None:
        raise LocationConflictError("A location with child locations cannot be deleted.")

    db.delete(location)
    db.flush()
