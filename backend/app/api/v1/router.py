from fastapi import APIRouter

from app.api.v1.admin_job_advertisements import (
    router as admin_job_advertisements_router,
)
from app.api.v1.admin_locations import router as admin_locations_router
from app.api.v1.admin_taxonomy import router as admin_taxonomy_router
from app.api.v1.auth import router as auth_router
from app.api.v1.employer_job_advertisements import (
    router as employer_job_advertisements_router,
)
from app.api.v1.employer_payments import router as employer_payments_router
from app.api.v1.employers import router as employers_router
from app.api.v1.public_job_advertisements import (
    router as public_job_advertisements_router,
)
from app.api.v1.public_locations import router as public_locations_router
from app.api.v1.public_taxonomy import router as public_taxonomy_router
from app.api.v1.users import router as users_router
from app.api.v1.workers import router as workers_router

router = APIRouter()

router.include_router(
    admin_job_advertisements_router,
    prefix="/admin/job-advertisements",
    tags=["Admin Job Advertisements"],
)


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
    admin_taxonomy_router,
    prefix="/admin/taxonomy",
    tags=["Admin Taxonomy"],
)

router.include_router(
    users_router,
    prefix="/users",
    tags=["Users"],
)

router.include_router(
    workers_router,
    prefix="/workers",
    tags=["Workers"],
)

router.include_router(
    employers_router,
    prefix="/employers",
    tags=["Employers"],
)

router.include_router(
    employer_job_advertisements_router,
    prefix="/employers/advertisements",
    tags=["Employer Job Advertisements"],
)

router.include_router(
    admin_locations_router,
    prefix="/admin/locations",
    tags=["Admin Locations"],
)

router.include_router(
    public_locations_router,
    prefix="/public",
    tags=["Public Locations"],
)

router.include_router(
    public_job_advertisements_router,
    prefix="/public",
    tags=["Public Job Advertisements"],
)

router.include_router(
    employer_payments_router,
    prefix="/employers/payments",
    tags=["Employer Payments"],
)
