from fastapi import APIRouter

from app.api.dependencies.authentication import CurrentUser
from app.schemas.users import CurrentUserRead

router = APIRouter()


@router.get(
    "/me",
    response_model=CurrentUserRead,
    summary="Get the authenticated user's account",
)
def get_me(current_user: CurrentUser) -> CurrentUserRead:
    """Return safe account information for the authenticated user."""
    return CurrentUserRead(
        id=current_user.id,
        full_name=current_user.full_name,
        username=current_user.username,
        phone_number=current_user.phone_number,
        account_status=current_user.account_status,
        phone_verified_at=current_user.phone_verified_at,
        created_at=current_user.created_at,
        updated_at=current_user.updated_at,
        platform_roles=sorted(role.role for role in current_user.roles),
    )
