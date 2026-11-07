from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_response import ApiResponse
from app.core.database import DbDepends
from app.core.pagination import CursorPage
from app.domain.category import service
from app.domain.category.schema import (
    CategoryCreateRequest,
    CategoryListQuery,
    CategoryResponse,
    CategoryUpdateRequest,
)
from app.domain.household.deps import CurrentHousehold

router = APIRouter(prefix="/category", tags=["category"])


@router.get("/list")
async def list_categories(
    household: CurrentHousehold,
    q: Annotated[CategoryListQuery, Query()],
    db: AsyncSession = DbDepends,
) -> ApiResponse[CursorPage[CategoryResponse]]:
    """카테고리 목록 (cursor 무한 스크롤) — searchTerm/kind/isArchived 필터"""
    response = await service.list_categories_cursor(
        db, household,
        search_term=q.search_term,
        kind=q.kind.value if q.kind else None,
        is_archived=q.is_archived,
        cursor=q.cursor,
        limit=q.limit,
    )
    return ApiResponse.ok(data=response)


@router.post("/create")
async def create_category(
    req: CategoryCreateRequest,
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[CategoryResponse]:
    """카테고리 생성"""
    response = await service.create_category(db, household, req)
    return ApiResponse.ok(data=response)


@router.get("/detail/{category_id}")
async def get_category_detail(
    category_id: UUID,
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[CategoryResponse]:
    """카테고리 단건 조회"""
    response = await service.get_category_detail(db, household, category_id)
    return ApiResponse.ok(data=response)


@router.put("/update/{category_id}")
async def update_category(
    category_id: UUID,
    req: CategoryUpdateRequest,
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[CategoryResponse]:
    """카테고리 수정"""
    response = await service.update_category(db, household, category_id, req)
    return ApiResponse.ok(data=response)


@router.delete("/delete/{category_id}")
async def delete_category(
    category_id: UUID,
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[None]:
    """카테고리 삭제 (soft)"""
    await service.delete_category(db, household, category_id)
    return ApiResponse.ok()
