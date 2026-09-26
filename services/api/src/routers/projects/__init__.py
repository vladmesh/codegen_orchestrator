"""Aggregate the project route domains under their stable public prefix."""

from fastapi import APIRouter

from . import access, lifecycle, qa_probes, secrets, teardown, telegram, verification_gaps

router = APIRouter(prefix="/projects", tags=["projects"])
router.include_router(lifecycle.router)
router.include_router(access.router)
router.include_router(secrets.router)
router.include_router(telegram.router)
router.include_router(qa_probes.router)
router.include_router(verification_gaps.router)
router.include_router(secrets.delete_router)
router.include_router(teardown.router)
