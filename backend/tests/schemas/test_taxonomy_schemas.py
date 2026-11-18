import pytest
from pydantic import ValidationError

from app.schemas.taxonomy import (
    CategoryCreate,
    CategoryMove,
    CategoryUpdate,
    SkillActivation,
    SkillCreate,
    SkillUpdate,
)


def test_category_create_accepts_valid_data() -> None:
    schema = CategoryCreate(
        name="Construction",
        description="Construction-related professions",
        display_order=1,
    )

    assert schema.name == "Construction"
    assert schema.parent_id is None
    assert schema.display_order == 1


def test_category_update_preserves_omitted_fields() -> None:
    schema = CategoryUpdate()

    assert schema.model_fields_set == set()


def test_category_update_allows_explicit_description_clearing() -> None:
    schema = CategoryUpdate(description=None)

    assert "description" in schema.model_fields_set
    assert schema.description is None


def test_category_update_rejects_null_name() -> None:
    with pytest.raises(ValidationError):
        CategoryUpdate(name=None)


def test_category_update_rejects_null_display_order() -> None:
    with pytest.raises(ValidationError):
        CategoryUpdate(display_order=None)


def test_category_move_accepts_root_category() -> None:
    schema = CategoryMove(parent_id=None)

    assert schema.parent_id is None


def test_skill_create_accepts_valid_data() -> None:
    schema = SkillCreate(name="Welding")

    assert schema.name == "Welding"


def test_skill_update_preserves_omitted_fields() -> None:
    schema = SkillUpdate()

    assert schema.model_fields_set == set()


def test_skill_update_allows_explicit_description_clearing() -> None:
    schema = SkillUpdate(description=None)

    assert "description" in schema.model_fields_set
    assert schema.description is None


def test_skill_update_rejects_null_name() -> None:
    with pytest.raises(ValidationError):
        SkillUpdate(name=None)


@pytest.mark.parametrize("value", [True, False])
def test_skill_activation_accepts_booleans(value: bool) -> None:
    schema = SkillActivation(is_active=value)

    assert schema.is_active is value


@pytest.mark.parametrize("value", ["true", "false", 1, 0, None])
def test_skill_activation_rejects_non_booleans(value: object) -> None:
    with pytest.raises(ValidationError):
        SkillActivation.model_validate({"is_active": value})


def test_category_create_rejects_negative_display_order() -> None:
    with pytest.raises(ValidationError):
        CategoryCreate(name="Construction", display_order=-1)


def test_category_create_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CategoryCreate(name="Construction", unexpected_field="value")
