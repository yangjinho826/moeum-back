from fastapi import APIRouter
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_response import ApiResponse
from app.core.database import DbDepends
from app.domain.household.deps import CurrentHousehold
from app.domain.settings import service
from app.domain.settings.schema import SettingsOverviewResponse

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/overview")
async def get_overview(
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[SettingsOverviewResponse]:
    """설정 페이지 1호출 — 각 도메인 count"""
    response = await service.get_settings_overview(db, household)
    return ApiResponse.ok(data=response)
