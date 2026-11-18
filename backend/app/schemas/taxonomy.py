from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaxonomySchema(BaseModel):
    """Base configuration for taxonomy request and response schemas."""

    model_config = ConfigDict(
        extra="forbid",
        from_attributes=True,
    )


class CategoryCreate(TaxonomySchema):
    """Validate category creation."""

    name: str = Field(min_length=1, max_length=120)
    parent_id: UUID | None = None
    description: str | None = Field(default=None, max_length=2000)
    display_order: int = Field(default=0, ge=0, strict=True)


class CategoryUpdate(TaxonomySchema):
    """Validate partial category updates."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    display_order: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_explicit_nulls(self) -> CategoryUpdate:
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("Category name cannot be null.")

        if "display_order" in self.model_fields_set and self.display_order is None:
            raise ValueError("Display order cannot be null.")

        return self


class CategoryMove(TaxonomySchema):
    """Validate a category hierarchy move."""

    parent_id: UUID | None


class CategoryActivation(TaxonomySchema):
    """Validate category activation changes."""

    is_active: bool = Field(strict=True)


class SkillCreate(TaxonomySchema):
    """Validate skill creation."""

    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


class SkillUpdate(TaxonomySchema):
    """Validate partial skill updates."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_explicit_null_name(self) -> SkillUpdate:
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("Skill name cannot be null.")

        return self


class SkillActivation(TaxonomySchema):
    """Validate skill activation changes."""

    is_active: bool = Field(strict=True)


class SkillAssociationCreate(TaxonomySchema):
    """Validate a category-to-skill association."""

    skill_id: UUID


class PublicSkillRead(TaxonomySchema):
    """Represent a skill visible to public clients."""

    id: UUID
    name: str
    description: str | None


class PublicCategoryTreeRead(TaxonomySchema):
    """Represent a public category with nested subcategories and skills."""

    id: UUID
    parent_id: UUID | None
    name: str
    description: str | None
    display_order: int
    skills: list[PublicSkillRead] = Field(default_factory=list)
    children: list[PublicCategoryTreeRead] = Field(default_factory=list)


class SkillRead(TaxonomySchema):
    """Represent a skill in administrative API responses."""

    id: UUID
    name: str
    description: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class CategoryRead(TaxonomySchema):
    """Represent a category in administrative API responses."""

    id: UUID
    parent_id: UUID | None
    name: str
    description: str | None
    display_order: int
    is_active: bool
    created_at: datetime
    updated_at: datetime
    skills: list[SkillRead] = Field(default_factory=list)


class CategoryTreeRead(CategoryRead):
    """Represent an administrative category with nested child categories."""

    children: list[CategoryTreeRead] = Field(default_factory=list)
