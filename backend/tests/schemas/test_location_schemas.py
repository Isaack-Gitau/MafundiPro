from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.locations import (
    LocationActivation,
    LocationCreate,
    LocationMove,
    LocationTreeRead,
    LocationUpdate,
)


def test_location_create_accepts_valid_country() -> None:
    schema = LocationCreate(
        location_type="country",
        name="Kenya",
        country_code="KE",
    )

    assert schema.location_type == "country"
    assert schema.name == "Kenya"
    assert schema.country_code == "KE"
    assert schema.parent_id is None


def test_location_create_accepts_valid_coordinates() -> None:
    schema = LocationCreate(
        location_type="town",
        name="Nairobi",
        parent_id=uuid4(),
        latitude=Decimal("-1.286389"),
        longitude=Decimal("36.817223"),
    )

    assert schema.latitude == Decimal("-1.286389")
    assert schema.longitude == Decimal("36.817223")


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [
        (Decimal("-1.2"), None),
        (None, Decimal("36.8")),
    ],
)
def test_location_create_rejects_incomplete_coordinates(
    latitude: Decimal | None,
    longitude: Decimal | None,
) -> None:
    with pytest.raises(ValidationError):
        LocationCreate(
            location_type="town",
            name="Nairobi",
            parent_id=uuid4(),
            latitude=latitude,
            longitude=longitude,
        )


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [
        (Decimal("90.1"), Decimal("36.8")),
        (Decimal("-90.1"), Decimal("36.8")),
        (Decimal("-1.2"), Decimal("180.1")),
        (Decimal("-1.2"), Decimal("-180.1")),
    ],
)
def test_location_create_rejects_out_of_range_coordinates(
    latitude: Decimal,
    longitude: Decimal,
) -> None:
    with pytest.raises(ValidationError):
        LocationCreate(
            location_type="town",
            name="Nairobi",
            parent_id=uuid4(),
            latitude=latitude,
            longitude=longitude,
        )


def test_location_update_preserves_omitted_fields() -> None:
    schema = LocationUpdate()

    assert schema.model_fields_set == set()


def test_location_update_allows_explicit_description_clearing() -> None:
    schema = LocationUpdate(description=None)

    assert "description" in schema.model_fields_set
    assert schema.description is None


def test_location_update_allows_clearing_both_coordinates() -> None:
    schema = LocationUpdate(latitude=None, longitude=None)

    assert "latitude" in schema.model_fields_set
    assert "longitude" in schema.model_fields_set
    assert schema.latitude is None
    assert schema.longitude is None


@pytest.mark.parametrize(
    "values",
    [
        {"latitude": Decimal("-1.2")},
        {"longitude": Decimal("36.8")},
    ],
)
def test_location_update_rejects_partial_coordinate_updates(
    values: dict[str, Decimal],
) -> None:
    with pytest.raises(ValidationError):
        LocationUpdate(**values)


def test_location_update_rejects_null_name() -> None:
    with pytest.raises(ValidationError):
        LocationUpdate(name=None)


def test_location_update_rejects_null_display_order() -> None:
    with pytest.raises(ValidationError):
        LocationUpdate(display_order=None)


def test_location_update_rejects_null_country_code() -> None:
    with pytest.raises(ValidationError):
        LocationUpdate(country_code=None)


def test_location_move_accepts_root_location() -> None:
    schema = LocationMove(parent_id=None)

    assert schema.parent_id is None


@pytest.mark.parametrize("value", [True, False])
def test_location_activation_accepts_booleans(value: bool) -> None:
    schema = LocationActivation(is_active=value)

    assert schema.is_active is value


@pytest.mark.parametrize("value", ["true", "false", 1, 0, None])
def test_location_activation_rejects_non_booleans(value: object) -> None:
    with pytest.raises(ValidationError):
        LocationActivation.model_validate({"is_active": value})


def test_location_create_rejects_negative_display_order() -> None:
    with pytest.raises(ValidationError):
        LocationCreate(
            location_type="country",
            name="Kenya",
            country_code="KE",
            display_order=-1,
        )


def test_location_create_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        LocationCreate(
            location_type="country",
            name="Kenya",
            country_code="KE",
            unexpected_field="value",
        )


def test_location_tree_read_supports_nested_children() -> None:
    now = datetime.now(UTC)
    country_id = uuid4()
    county_id = uuid4()

    country = LocationTreeRead(
        id=country_id,
        parent_id=None,
        location_type="country",
        name="Kenya",
        country_code="KE",
        description=None,
        latitude=None,
        longitude=None,
        display_order=0,
        is_active=True,
        created_at=now,
        updated_at=now,
        children=[
            {
                "id": county_id,
                "parent_id": country_id,
                "location_type": "county",
                "name": "Nairobi",
                "country_code": None,
                "description": None,
                "latitude": None,
                "longitude": None,
                "display_order": 0,
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )

    assert country.children[0].id == county_id
    assert country.children[0].name == "Nairobi"
