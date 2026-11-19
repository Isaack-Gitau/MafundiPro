from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.public_taxonomy import router as public_taxonomy_router
from app.api.v1.users import router as users_router

router = APIRouter()


@router.get("/", tags=["API"])
def api_v1_root() -> dict[str, str]:
    """Provide information about the versioned API."""
    return {
        "name": "MafundiPro API",
        "version": "v1",
        "status": "available",
    }


router.include_router(
    auth_router,
    prefix="/auth",
    tags=["Authentication"],
)

router.include_router(
    public_taxonomy_router,
    prefix="/public",
    tags=["Public Taxonomy"],
)

router.include_router(
    users_router,
    prefix="/users",
    tags=["Users"],
)
