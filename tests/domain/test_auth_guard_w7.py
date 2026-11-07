"""W7. 인증·권한 — 세션 폐기·데모 계정 보호·검색 남용 제한·결제자 멤버 검증.

**공개계약**
- 로그인 = access(JSON) + refresh(HttpOnly 쿠키 `refresh_token`) — `auth/router.py` login
- refresh 는 DB 의 활성 토큰만 통과, 실패 시 401 + 쿠키 삭제 — `auth/router.py` refresh
- 사용자 수정은 본인만 — `user/service.py` update_user
- 가계부 삭제는 owner 만 — `household/service.py` delete_household
- 이메일 검색 = 가계부 멤버 초대용 — `user/router.py` search

HTTP 요청은 각자 새 client(기기 하나 = client 하나). 쿠키는 응답에서 꺼내 헤더로 직접 싣는다
(COOKIE_SECURE 설정과 무관하게 같은 값을 보내려고).
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.auth.jwt import create_refresh_token
from app.core.config import settings
from app.core.database import async_session
from app.core.enums.data_status import DataStatus
from app.core.exceptions import CustomException, ErrorCode
from app.domain.auth.model import RefreshToken
from app.domain.household.model import Household
from app.domain.transaction import service as tx_service
from app.domain.transaction.model import Transaction
from app.domain.transaction.schema import TransactionCreateRequest, TransactionUpdateRequest
from app.domain.user import service as user_service
from app.domain.user.schema import UserCreateRequest
from app.main import app
from tests.fixtures.factory import (
    household_factory,
    member_factory,
    seed_ledger_context,
    user_factory,
)

PASSWORD = "oldpass12"


@pytest.fixture(autouse=True)
def _fresh_limits():
    """횟수 제한은 프로세스 메모리 — 테스트끼리 카운트가 섞이지 않게 매번 비운다."""
    from app.core.rate_limit import get_password_limiter, get_search_limiter

    for limiter in (get_search_limiter(), get_password_limiter()):
        limiter.reset()
    yield
    for limiter in (get_search_limiter(), get_password_limiter()):
        limiter.reset()


@pytest.fixture
def demo_on(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_SEED_ENABLED", True)


async def _call(method: str, path: str, *, token: str | None = None,
                refresh: str | None = None, body: dict | None = None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if refresh:
        headers["Cookie"] = f"refresh_token={refresh}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.request(method, path, json=body, headers=headers)


async def _signup(db, email: str = "w7@test.com"):
    user = await user_service.create_user(
        db, UserCreateRequest(email=email, name="테스터", password=PASSWORD),
    )
    await db.commit()
    return user


async def _login(email: str = "w7@test.com", password: str = PASSWORD) -> tuple[str, str]:
    resp = await _call("POST", "/auth/login", body={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["accessToken"], resp.cookies["refresh_token"]


def test_W7_10_같은_순간에_만든_refresh_토큰도_서로_다르다():
    """exp 는 초 단위 — 같은 초 두 로그인이 같은 문자열이면 refresh 조회가 2행을 만나 500 이 난다.

    로그인은 bcrypt 검증 시간 때문에 초가 넘어가 우연히 통과할 수 있어 발급 함수로 직접 본다."""
    data = {"sub": "00000000-0000-0000-0000-000000000001", "language": "ko"}

    assert create_refresh_token(data) != create_refresh_token(data)


async def test_W7_1_비번을_바꾸면_다른_기기_세션은_끊기고_바꾼_기기는_남는다(db):
    user = await _signup(db)
    access_a, r1 = await _login()
    _, r2 = await _login()

    resp = await _call(
        "PUT", f"/user/{user.id}", token=access_a, refresh=r1,
        body={"password": "newpass34", "currentPassword": PASSWORD},
    )

    assert resp.status_code == 200, resp.text
    assert (await _call("POST", "/auth/refresh", refresh=r2)).status_code == 401
    assert (await _call("POST", "/auth/refresh", refresh=r1)).status_code == 200
    await _login(password="newpass34")


async def test_W7_2_현재_비번이_없거나_틀리면_비번을_못_바꾸고_세션도_그대로다(db):
    user = await _signup(db)
    access, r1 = await _login()

    missing = await _call("PUT", f"/user/{user.id}", token=access, body={"password": "newpass34"})
    wrong = await _call(
        "PUT", f"/user/{user.id}", token=access,
        body={"password": "newpass34", "currentPassword": "wrongpw99"},
    )

    assert (missing.json()["code"], wrong.json()["code"]) == ("AU003", "AU003")
    assert (await _call("POST", "/auth/refresh", refresh=r1)).status_code == 200
    await _login(password=PASSWORD)  # 옛 비번 그대로


async def test_W7_3_데모_계정은_비번을_못_바꾼다(db, demo_on):
    demo = await _signup(db, email=settings.DEMO_EMAIL)
    access, _ = await _login(email=settings.DEMO_EMAIL)

    resp = await _call(
        "PUT", f"/user/{demo.id}", token=access,
        body={"password": "newpass34", "currentPassword": PASSWORD},
    )

    assert resp.status_code == 403
    assert resp.json()["code"] == "US006"
    await _login(email=settings.DEMO_EMAIL)  # 옛 비번 그대로


async def test_W7_4_데모_계정은_가계부를_못_지운다(db, demo_on):
    demo = await _signup(db, email=settings.DEMO_EMAIL)
    household = await household_factory(db, owner=demo)
    await member_factory(db, household=household, user=demo)
    await db.commit()
    access, _ = await _login(email=settings.DEMO_EMAIL)

    resp = await _call("DELETE", f"/household/delete/{household.id}", token=access)

    assert resp.status_code == 403
    assert resp.json()["code"] == "US006"
    async with async_session() as s:
        assert (await s.get(Household, household.id)).data_stat_cd == DataStatus.ACTIVE


async def test_W7_8a_데모_계정도_이름은_바꿀_수_있다(db, demo_on):
    demo = await _signup(db, email=settings.DEMO_EMAIL)
    access, _ = await _login(email=settings.DEMO_EMAIL)

    resp = await _call("PUT", f"/user/{demo.id}", token=access, body={"name": "체험"})

    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "체험"


async def _owner(db, email: str):
    user = await _signup(db, email=email)
    household = await household_factory(db, owner=user)
    await member_factory(db, household=household, user=user)
    await db.commit()
    return user, household


async def test_W7_5_소유한_가계부가_없으면_이메일_검색을_못_한다(db):
    owner, household = await _owner(db, "owner@test.com")
    member = await _signup(db, email="member@test.com")
    await member_factory(db, household=household, user=member)
    await db.commit()
    access, _ = await _login(email="member@test.com")

    resp = await _call("GET", "/user/search?email=owner@test.com", token=access)

    assert resp.status_code == 403
    assert resp.json()["code"] == "HH003"


async def test_W7_7_검색은_1분에_10번까지다(db):
    await _owner(db, "owner@test.com")
    access, _ = await _login(email="owner@test.com")

    codes = [
        (await _call("GET", f"/user/search?email=nobody{i}@test.com", token=access)).status_code
        for i in range(11)
    ]

    assert set(codes[:10]) <= {200, 400}  # 없는 이메일은 NOT_FOUND(400)
    assert codes[10] == 429


async def test_W7_8b_다른_가계부를_소유한_사람은_검색할_수_있다(db):
    await _owner(db, "owner@test.com")
    target = await _signup(db, email="friend@test.com")
    access, _ = await _login(email="owner@test.com")

    resp = await _call("GET", "/user/search?email=friend@test.com", token=access)

    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == str(target.id)


def _expense(ctx, **extra) -> TransactionCreateRequest:
    return TransactionCreateRequest.model_validate({
        "txType": "EXPENSE", "amount": "10000", "txDate": "2026-11-05",
        "accountId": str(ctx.account.id), **extra,
    })


async def test_W7_6_다른_가계부_사람을_결제자로_넣을_수_없다(db):
    ctx = await seed_ledger_context(db)
    outsider = await user_factory(db, email="outsider@test.com")

    with pytest.raises(CustomException) as exc:
        await tx_service.create_transaction(
            db, ctx.household, _expense(ctx, paidByUserId=str(outsider.id)), ctx.user,
        )
    assert exc.value.error_code == ErrorCode.NOT_FOUND


async def test_W7_9_결제자가_밖의_사람으로_박힌_옛_거래도_메모는_고칠_수_있다(db):
    ctx = await seed_ledger_context(db)
    outsider = await user_factory(db, email="outsider@test.com")
    tx = await tx_service.create_transaction(db, ctx.household, _expense(ctx), ctx.user)
    row = await db.get(Transaction, tx.id)
    row.paid_by_user_id = outsider.id  # 검증이 없던 시절 저장된 값
    await db.flush()

    req = TransactionUpdateRequest.model_validate({"paidByUserId": str(outsider.id), "memo": "정리"})
    updated = await tx_service.update_transaction(db, ctx.household, tx.id, req)

    assert updated.memo == "정리"


async def test_W7_5b_소유_가계부가_없는_사람의_검색도_횟수에_들어간다(db):
    await _signup(db, email="member@test.com")
    access, _ = await _login(email="member@test.com")

    codes = [
        (await _call("GET", "/user/search?email=x@test.com", token=access)).status_code
        for _ in range(11)
    ]

    assert codes[:10] == [403] * 10
    assert codes[10] == 429


async def test_W7_11_현재_비번_시도는_10분에_5번까지다(db):
    user = await _signup(db)
    access, _ = await _login()
    body = {"password": "newpass34", "currentPassword": "wrongpw99"}

    codes = [
        (await _call("PUT", f"/user/{user.id}", token=access, body=body)).status_code
        for _ in range(6)
    ]

    assert codes[:5] == [400] * 5
    assert codes[5] == 429


async def test_W7_12_데모가_꺼진_배포에선_그_이메일도_일반_사용자다(db):
    user = await _signup(db, email=settings.DEMO_EMAIL)  # DEMO_SEED_ENABLED=False(테스트 기본)
    access, _ = await _login(email=settings.DEMO_EMAIL)

    resp = await _call(
        "PUT", f"/user/{user.id}", token=access,
        body={"password": "newpass34", "currentPassword": PASSWORD},
    )

    assert resp.status_code == 200


async def test_W7_6b_수정에서_결제자를_밖의_사람으로_바꿀_수_없다(db):
    ctx = await seed_ledger_context(db)
    outsider = await user_factory(db, email="outsider@test.com")
    tx = await tx_service.create_transaction(db, ctx.household, _expense(ctx), ctx.user)

    req = TransactionUpdateRequest.model_validate({"paidByUserId": str(outsider.id)})
    with pytest.raises(CustomException) as exc:
        await tx_service.update_transaction(db, ctx.household, tx.id, req)
    assert exc.value.error_code == ErrorCode.NOT_FOUND


async def test_W7_13_jti_이전에_같은_문자열로_저장된_refresh_가_둘이어도_500_이_아니다(db):
    user = await _signup(db)
    _, r1 = await _login()
    async with async_session() as s:  # 같은 초 두 로그인이 같은 문자열이던 시절의 행
        original = (await s.execute(select(RefreshToken).where(RefreshToken.token == r1))).scalar_one()
        s.add(RefreshToken(
            user_id=user.id, token=r1, expires_at=original.expires_at, data_stat_cd=DataStatus.ACTIVE,
        ))
        await s.commit()

    assert (await _call("POST", "/auth/refresh", refresh=r1)).status_code == 200
    assert (await _call("POST", "/auth/logout", refresh=r1)).status_code == 200


async def test_W7_14_아주_긴_현재_비번은_500_이_아니라_AU003_이다(db):
    user = await _signup(db)
    access, _ = await _login()

    resp = await _call(
        "PUT", f"/user/{user.id}", token=access,
        body={"password": "newpass34", "currentPassword": "a" * 5000},
    )

    assert resp.status_code == 400
    assert resp.json()["code"] == "AU003"
