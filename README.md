# moeum-back

부부 공유 가계부 백엔드. `ai-router-api` 스택 그대로 (FastAPI / SQLAlchemy 2.x async / asyncpg / JWT).

## 빌드/실행

```bash
# 의존성 설치
uv sync

# 환경변수 세팅 (예시 복사 후 수정)
cp .env.example .env

# 개발 서버
uv run uvicorn app.main:app --reload

# Docker 개발 환경 (Postgres + app)
docker compose -f docker-compose.yml up
```

서버 기동 후: `GET /v1/health` 로 핑 확인.

## 테스트

테스트는 **실 PostgreSQL**(운영과 동일 엔진)에서 돈다 — 멱등성 `ON CONFLICT` / advisory lock semantics 를 SQLite 가 재현 못 하기 때문. `docker-compose.test.yml` 이 tmpfs 휘발성 PG(포트 55432)를 띄운다.

```bash
# 1. 테스트 PG 기동 (healthy 될 때까지 몇 초 — 한 번 띄우면 계속 떠 있음)
docker compose -f docker-compose.test.yml up -d

# 2. 실행 (컨테이너 떠 있는 동안 몇 번이든)
uv run pytest                       # 전체
uv run pytest tests/idempotency -v  # 멱등성 시나리오만 (케이스별 표시)
uv run pytest tests/smoke -v        # 환경 스모크만

# 커버리지
uv run pytest --cov=app --cov-branch --cov-report=term-missing

# 3. 정리 (tmpfs 라 데이터 자동 소멸)
docker compose -f docker-compose.test.yml down
```

- `ConnectionRefused 55432` → 테스트 PG 가 안 떠 있는 것. 1번 다시 실행하고 `docker ps` 에 `(healthy)` 뜨는지 확인 후 pytest.
- 스키마는 SQLAlchemy 모델(`Base.metadata.create_all`)로 생성하고 매 테스트 후 `TRUNCATE` 로 격리한다.
- 시나리오 체크리스트: `tests/SCENARIOS.md` (A 멱등성 / B advisory lock / C fault-injection / D 도메인).

## 디렉토리 구조

```
app/
├── main.py                 # FastAPI 앱 + 라우터 등록 + 예외 핸들러 + CORS
├── core/                   # 공통 인프라
│   ├── api_response.py     # ApiResponse[T] 응답 래퍼
│   ├── config.py           # pydantic-settings 설정
│   ├── database.py         # async engine/session, DbDepends(get_db, scope=function) 의존성
│   ├── model.py            # Base, BaseEntity (UUID + 감사 + data_stat_cd)
│   ├── logging.py          # 콘솔 + 일별 파일 로테이션
│   ├── auth/               # JWT, 비밀번호 해싱, 인증 의존성 (deps.py 는 user 도메인 추가 시)
│   ├── enums/              # DataStatus 등 공통 enum
│   └── exceptions/         # CustomException + ErrorCode + 4종 핸들러
└── domain/
    └── health/             # /health 엔드포인트
```

## 도메인 추가 예정 (다음 단계)

| 도메인 | 테이블 |
|---|---|
| auth | refresh_tokens |
| user | users |
| household | households + household_members |
| account | accounts + account_snapshots |
| category | categories |
| transaction | transactions |
| fixed_expense | fixed_expenses |
| portfolio | portfolio_items + portfolio_value_history |

각 도메인 = 한 폴더 = 6파일 (`model.py`, `schema.py`, `repository.py`, `service.py`, `router.py`, `enum.py`).

## 핵심 규약

| 항목 | 값 |
|---|---|
| Python | 3.14 |
| ASGI | uvicorn |
| ORM | SQLAlchemy 2.x async + asyncpg |
| 인증 | JWT (access Bearer + refresh HttpOnly cookie) |
| ID | UUID (`uuid.uuid4()`) |
| 감사 필드 | `frst_reg_dt`, `last_mdfcn_dt` (onupdate), `data_stat_cd` |
| 응답 래퍼 | `ApiResponse[T]` — `.ok(data)` / `.fail(status, code, message)` |
| 예외 | `CustomException(ErrorCode)` — 핸들러가 `ApiResponse.fail` 변환 |
| Soft Delete | `data_stat_cd = "99"` (ACTIVE="50") |
| API prefix | `/v1` (FastAPI `root_path`) |
| 트랜잭션 | `DbDepends`(= `Depends(get_db, scope="function")`)가 응답 **전에** commit/rollback. 서비스는 `db.flush()` 만. 맨 `Depends(get_db)` 금지 — 응답 뒤 commit 으로 돌아감(테스트 W4-3 이 막음) |
| 서비스 | 모듈 함수 (`service.create_user(db, req)`) — 클래스 X |
| 레포지토리 | 클래스 (`__init__(self, db)`) |
