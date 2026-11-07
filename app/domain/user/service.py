from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.security import hash_password, verify_password
from app.core.demo.guard import is_demo_user
from app.core.enums.data_status import DataStatus
from app.core.exceptions import CustomException, ErrorCode
from app.core.rate_limit import SlidingWindowLimiter
from app.domain.auth.repository import RefreshTokenRepository
from app.domain.household.repository import HouseholdRepository
from app.domain.user.model import User
from app.domain.user.repository import UserRepository
from app.domain.user.schema import UserCreateRequest, UserResponse, UserUpdateRequest


async def create_user(db: AsyncSession, req: UserCreateRequest) -> User:
    repo = UserRepository(db)

    email = req.email.strip().lower()
    name = req.name.strip()

    if await repo.find_by_email(email):
        raise CustomException(ErrorCode.USER_DUPLICATE_EMAIL)

    user = User(
        email=email,
        name=name,
        password_hash=await hash_password(req.password),
        language=req.language,
        data_stat_cd=DataStatus.ACTIVE,
    )
    await repo.save(user)
    return user


async def detail_user(db: AsyncSession, user_id: UUID) -> UserResponse:
    repo = UserRepository(db)
    user = await repo.find_by_id(user_id)
    if not user:
        raise CustomException(ErrorCode.NOT_FOUND)
    return UserResponse.model_validate(user)


async def search_by_email(
    db: AsyncSession, email: str, current_user: User, limiter: SlidingWindowLimiter,
) -> UserResponse:
    """이메일 정확 매칭 검색 — 가계부 멤버 초대용. 미가입 시 NOT_FOUND.

    실제 통제는 사용자별 분당 횟수 제한이다. owner 검사는 초대 화면 밖 호출을 걸러 주는 정도
    (누구나 가계부를 만들면 owner). 헤더 가계부 대신 '소유 가계부 존재' 로 보는 이유: 멤버 화면이
    URL 의 가계부로도 열려 헤더(현재 가계부)와 다를 수 있다.
    """
    limiter.hit(str(current_user.id))  # 먼저 센다 — 막히는 요청(403)도 조회 비용이 든다
    if not await HouseholdRepository(db).has_active_owned_by(current_user.id):
        raise CustomException(ErrorCode.HOUSEHOLD_NOT_OWNER)
    repo = UserRepository(db)
    normalized = email.strip().lower()
    user = await repo.find_by_email(normalized)
    if not user:
        raise CustomException(ErrorCode.NOT_FOUND)
    return UserResponse.model_validate(user)


async def update_user(
    db: AsyncSession,
    user_id: UUID,
    req: UserUpdateRequest,
    current_user: User,
    password_limiter: SlidingWindowLimiter,
    keep_refresh: str | None = None,
) -> UserResponse:
    if user_id != current_user.id:
        raise CustomException(ErrorCode.FORBIDDEN)

    repo = UserRepository(db)
    user = await repo.find_by_id(user_id)
    if not user:
        raise CustomException(ErrorCode.NOT_FOUND)

    name = req.name.strip() if req.name is not None else None
    password_hash = None
    if req.password is not None:
        if is_demo_user(user):  # 공개 계정 비번이 바뀌면 다른 체험자가 못 들어온다
            raise CustomException(ErrorCode.DEMO_READ_ONLY)
        password_limiter.hit(str(user.id))  # 현재 비번 추측·bcrypt 부하 상한(10분 5회)
        # access 토큰만 훔친 사람이 비번을 바꿔 계정을 가져가지 못하게 현재 비번을 확인한다
        # 비번은 64자까지만 저장된다(_check_password) — 그보다 길면 해시 비교 없이 틀린 것(passlib 4096자 초과 예외 방지)
        current = req.current_password
        if current is None or not (1 <= len(current) <= 64) or not await verify_password(
            current, user.password_hash,
        ):
            raise CustomException(ErrorCode.INVALID_PASSWORD)
        password_hash = await hash_password(req.password)

    user.update(name=name, password_hash=password_hash, language=req.language)
    if password_hash is not None:
        # 다른 기기의 세션(탈취됐을 수 있는)을 끊는다 — 요청한 기기는 유지
        await RefreshTokenRepository(db).revoke_all_for_user(user.id, except_token=keep_refresh)
    await db.flush()
    return UserResponse.model_validate(user)
