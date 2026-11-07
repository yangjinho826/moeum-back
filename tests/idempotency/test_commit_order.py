"""W4. 비즈니스 commit 은 응답 전에 끝난다.

**공개계약**
- `get_db`(`core/database.py`) — yield 뒤에 commit, 실패 시 rollback 후 re-raise
- 멱등 미들웨어 — 응답 5xx·예외면 PENDING 해제(재시도 허용), 그 밖은 COMPLETED 로 캐시
- FastAPI: yield 의존성 기본 scope="request" 는 yield 뒤 코드를 **응답을 보낸 뒤** 실행,
  scope="function" 은 경로 함수 직후·응답 전에 실행(Context7: dependencies-with-yield)

commit 이 응답 뒤라면 commit 이 실패해도 클라이언트는 200 을 받고, 미들웨어는 그 200 을
COMPLETED 로 굳혀 같은 키 재시도가 캐시만 받는다 → 데이터가 영영 안 들어간다.
"""
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core import database
from app.core.database import async_session, get_db
from app.core.idempotency.model import IdempotencyRecord
from app.domain.transaction.model import Transaction
from app.main import app
from tests.fixtures.factory import seed_transaction_context


async def _count(model) -> int:
    async with async_session() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


def _commit_fails_factory():
    """진짜 get_db 는 그대로 두고, 그 세션의 commit 만 실패시킨다(커넥션 끊김·제약 위반 흉내).

    get_db 는 모듈 전역 async_session 을 부를 때 찾고, 멱등 미들웨어는 import 시점 참조를
    들고 있어서 database.async_session 만 바꾸면 비즈니스 세션만 영향을 받는다.
    """
    def factory():
        session = async_session()

        async def failing_commit():
            raise RuntimeError("commit failed")

        session.commit = failing_commit  # type: ignore[method-assign]
        return session

    return factory


async def test_W4_1_commit_이_실패하면_클라이언트는_성공을_받지_않고_재시도가_열려_있다(db, monkeypatch):
    ctx = await seed_transaction_context(db)
    key = "key-w4-1"
    with monkeypatch.context() as m:
        m.setattr(database, "async_session", _commit_fails_factory())
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/transaction/create",
                json=ctx.create_body(),
                headers={**ctx.auth_headers, "Idempotency-Key": key},
            )

    # then: 저장 안 됐는데 200 을 주면 안 된다 + 같은 키가 "성공" 캐시로 굳으면 안 된다
    assert await _count(Transaction) == 0
    assert resp.status_code >= 500
    assert await _count(IdempotencyRecord) == 0

    # 같은 키 재시도는 라우터를 다시 타서 저장된다
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        retried = await c.post(
            "/transaction/create",
            json=ctx.create_body(),
            headers={**ctx.auth_headers, "Idempotency-Key": key},
        )
    assert retried.status_code == 200
    assert await _count(Transaction) == 1


def _get_db_scopes(dependant, found: list[str]) -> list[str]:
    for child in dependant.dependencies:
        if child.call is get_db:
            found.append(child.computed_scope)
        _get_db_scopes(child, found)
    return found


def test_W4_3_모든_라우트가_get_db_를_function_scope_로_쓴다():
    """누가 Depends(get_db) 를 새로 쓰면 응답 뒤 commit 으로 돌아가고 세션도 둘로 갈린다 — 막는다."""
    offenders = [
        r.path
        for r in app.routes
        if isinstance(r, APIRoute)
        and any(scope != "function" for scope in _get_db_scopes(r.dependant, []))
    ]
    assert offenders == []
