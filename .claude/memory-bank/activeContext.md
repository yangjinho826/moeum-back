# 활성 컨텍스트

## Goal

**이직 포폴 "운영 강점" 카드화 + SRE 로드맵 실행** (2026-07-21 시작). household-back 을 이력서/포폴에 추가 — 포지션은 "저비용 단일 서버를 운영 가능한 상태로 만드는 운영 감각" 보조 카드 (트래픽 카드 아님, codex 2회 교차검증). 포폴 본문은 `carrer/portfolio/household-back.md`, 이력서 블록은 `carrer/resume/household-이력서-블록.md`, 구현 로드맵은 이 레포 `docs/portfolio-sre-roadmap.md`. (직전 트랙: codewalk 시리즈 9개 문서 완결 — 2026-07-03 종료.)

## Status

**🚀 거래 탭 통장 필터 배포 완료 (2026-09-20). back `v0.13.0` → front `v0.15.0` 순서.**
거래 탭 좌우(목록 vs 요약·달력·지출Top5) 숫자가 어긋나던 문제. 집계 3개(`sum_by_type_for_month`·
`sum_by_category_for_month`·`daily_sums_for_month`)와 `/stats/monthly`·`/transaction/calendar/{y}/{m}/full`
에 `account_id` 옵션 추가. 조건은 `_account_involved` = `or_(account_id, to_account_id)`.
기본값 None 이라 홈(`home/service.py:46`)은 무영향 — 회귀 테스트 D6_4 로 고정.
백엔드를 먼저 올린 이유 = 프론트가 `accountId` 를 보내는데 구 백엔드는 모르는 쿼리 파라미터를
무시할 뿐이라 에러는 안 나지만 필터가 안 먹는다(반대 순서면 그 틈이 생김).
**곁다리로 선결 버그 1건 수정**: `update_transaction` 에 `_normalize_category` 가 없어 EXPENSE→TRANSFER
전환 시 category_id 가 살아남았고, 새 `or_` 조건 탓에 두 통장 양쪽에 이중계상됐다(재현 테스트 D6_6).
테스트 `tests/domain/test_stats_account_filter.py` 6개 추가 → 전체 99 passed.

**🚀 Stage 1~3 운영 배포 완료 (2026-08-17). USD(Stage 4)는 보류 — 사용자가 "계좌 외화"를 원한 것으로 드러남.**

**배포 순서와 태그** — back `v0.12.0` → front `v0.12.0` → front `v0.12.1`(장애 복구) → front `v0.12.2`(필터).
백엔드를 먼저 올린 이유 = 프론트가 `fixedExpenseName`/`fee`/`settlementAmount` 를 읽는데
백엔드는 additive 라 구 프론트와 호환되기 때문. PR 은 단계별 3개(back #23·#26·#25 / front #17·#20·#19).

**🔴 배포 사고 1건 (내 실수, v0.12.1 로 복구).** `fix/logout-household-reset` 브랜치를
**`feat/portfolio-usd` 위에서 분기**해 USD 커밋 2개가 PR 에 딸려 들어갔고 그대로 머지·배포됐다.
백엔드 USD 는 미머지라 프론트만 없는 필드를 읽어 **계좌 상세가 렌더 단계에서 죽었다**(API 는 전부 200).
원인 코드: `const dual = row.currency !== "KRW" && row.realizedPnlCcy !== null` —
**`undefined` 가 두 비교를 모두 통과**해 달러 경로로 진입 → `undefined.toLocaleString()`.
**교훈 3개**: ⓐ `git checkout -b` 전에 현재 브랜치 확인 ⓑ PR 생성 후 **커밋 목록 확인**(`gh pr view --json commits`)
ⓒ 타입 정의에 필드가 있으면 tsc·빌드가 다 통과한다 — **타입은 백엔드 응답의 검증이 아니다**.
`--delete-branch` 로 base 를 지우면 그 브랜치를 base 로 한 PR 이 **자동 CLOSED** 되고 reopen 도 안 된다(실측).

**🔴 계정 전환 버그 수정 (운영 재현 → v0.12.1 에 포함).** 로그아웃이 auth 만 비우고
`currentHouseholdId`(localStorage)를 남겨, **다음에 로그인한 다른 계정이 이전 가계부 id 로
`X-Household-Id` 를 보냈다**(HH001). `clearSession` 이 그 값도 비우게 하고(호출부 3곳 일괄 커버),
`OnboardingGuard` 가 render 단계에서도 차단하게 했다. 로그아웃은 `window.location.replace`(풀 리로드)인데
로그인은 `router.push`(SPA)라 **캐시는 날아가고 localStorage 만 남는 비대칭**이 원인.
풀 로드 경로는 수정 전에도 안 깨져 대조 실험으로 효과를 실측하진 못함(SPA 전환은 실제 계정 전환 필요).

**매매손익 기간 필터 단순화 (v0.12.2)** — 프리셋 4개(전체/올해/작년/직접) 제거, 날짜 2개만 상시 노출,
**기본 당일**(증권사 방식). 누적 성과는 레일이 이미 보여주므로 '전체'는 중복이었다.
부작용: 시트를 열면 대부분 0건 — 레일의 누적 표시와 괴리. 거슬리면 시작일 기본값만 조정하면 됨.

**🔜 다음 트랙: 외화 — 통합 설계부터 다시 (2026-08-17 사용자 결정).**
종목 통화(Stage 4)와 계좌 통화(Stage 5)를 **따로 설계했다가 방향이 반대로 나왔다** —
Stage 4 는 "KRW 진실 원천", Stage 5 초안은 "외화 진실 원천". codex #4 지적으로 Stage 5 를
뒤집었지만 **설계를 둘로 쪼갠 것 자체가 원인**이었다. 그래서 종목·계좌·환전을 묶어 처음부터
다시 그린다. **`feat/portfolio-usd` 는 그대로 머지하지 않고 참고용으로 보존**(양쪽 레포, rebase 완료, back 113 passed).
단 그 브랜치의 **안건 A(스냅샷 평단 replay 통일)** 와 **거래통화 필드 직렬화 수정**은 외화 설계와
독립이라 먼저 떼어내도 된다. 재설계가 정면으로 답할 것 = 외화 투자계좌 / 환차손익의 자리 /
환전 수수료·손익 / 환율 이력.

**(참고) 외화 계좌(Stage 5) 설계 문서 — 재설계의 입력.**
사용자 원 요구 "해외 투자했을경우 달러입력"이 사실 **계좌 단위 외화**였다(토스뱅크 외화통장).
Stage 4 가 한 건 종목 단위라 "그 돈이 어느 통화 지갑에서 나갔는지"는 여전히 원화로만 안다.
문서 `docs/foreign-currency-account-design.md` (codex 결함 10건 반영한 v2).
**초안에서 뒤집은 것**: "계좌는 외화가 진실 원천" → **KRW 를 합산 기준으로 유지**.
투자계좌가 `balance = cash + portfolio_valuation` 인데 현금 USD + 종목 KRW 면 **단위가 다른 값을 더하게** 되고,
Stage 4(종목 KRW 진실)와 같은 계좌 안에서 충돌하기 때문. 대신 `native_balance`(계좌통화) / `balance`(KRW 환산) 분리.
**1차 범위는 비투자 계좌만** — 외화 투자계좌는 매매 정산 통화까지 재정의해야 해 Stage 4 를 다시 건드린다.
확정 제약 2개: **계좌 통화는 생성 후 불변**(바꾸면 과거 거래 금액의 통화 의미가 통째로 바뀜),
**투자계좌는 외화 거부**.

---

**(이전) MVP 기능 추가개발 4단계 (2026-08-17 시작 — 1단계 코드 완료, 미커밋).** 지금까지의 SRE/포폴 트랙과 성격이 다름 = **가계부를 실사용 가능하게 만드는 제품 작업**. 사용자 요청 6항목 + 세션 중 추가 1항목을 **4단계로 쪼개 하나씩 진행**하기로 확정(한 번에 X, 단계마다 대화). 계획 파일 `~/.claude/plans/wobbly-prancing-pebble.md` (1단계만 상세, 2~4단계는 로드맵 한 줄).
**진행 방식 결정: 백엔드는 TDD(RED→GREEN), 프론트는 제외**(육안 검증).
**단계 순서와 근거**: ① 고정지출 라벨 + 거래 복사(마이그레이션 0, 독립적, 체감 빠름) → ② 수수료(`portfolio_transactions.fee` — 매수는 평단 가산 / 매도는 실현손익 차감 / 매매현금 반영) → ③ 매매손익 카드 재설계 + 매도내역 수정(키움 거래내역 스타일, **전량매도로 사라진 종목의 매도도 카드에서 바로 수정** — 지금은 종목 화면 진입이 막혀 수정 불가. BE 는 `find_by_id_any_status`(portfolio/service.py:269) 로 이미 지원됨) + 기간 직접 지정 → ④ USD 거래. ②→③ 은 순서 의존(카드가 fee 를 표시).
**④ USD 설계 확정 (사용자: "계산은 USD 기준, 표시는 토스증권처럼")**: **KRW 컬럼을 진실 원천으로 유지하고 거래통화 컬럼을 병행 추가**. 통화 전환을 안 하는 이유 = `account/service.py:305-306`·`wealth/service.py:165`·`portfolio/repository.py:136` 이 `quantity * current_price` 를 통화 구분 없이 SQL/파이썬으로 합산해 **통화가 섞이면 순자산이 조용히 틀린다**. 거래시점 환율은 tx 에 박제(`currency_rates` 는 pair 당 1행·최신값뿐이라 **과거 거래 소급 입력 시 최신 환율로 박제되는 한계** 존재 — 환율 이력 수집은 스코프 밖).
**1단계 실적 (2026-08-17)**: 시나리오 10개 도출 → RED 10 실패 확인 → 구현 → **GREEN 10, 전체 75 passed**(기존 65 유지). 산출물 = `tests/domain/test_transaction_fixed_label.py`, `fixed_expense_factory`(fixtures/factory.py), `FixedRepository.find_by_ids`, `TransactionResponse.fixed_expense_name`, `_build_response(+fixed_map)` 와 호출부 3곳(list/ledger/single), 프론트 `tx-row`·`ledger-row` 배지 + 거래 복사(store `openCopy` / use-form `copyFromId` / form-actions `onCopy`).
**설계 판단 2개 (되풀이 금지)**: ⓐ **`FixedRepository.find_by_ids` 는 `data_stat_cd` 필터를 걸지 않는다** — 고정지출 삭제는 soft delete(`fixed/service.py:168`)라 FK `ondelete=SET NULL` 이 안 터지고 거래는 `fixed_expense_id` 를 계속 들고 있다. ACTIVE 로 거르면 보관/삭제된 고정지출에 물린 **과거 거래가 `고정지출 · —` 로 깨진다**(테스트 #3·#4 가 이걸 고정). ⓑ 프론트가 `formOptions.fixedExpenses` 로 이름을 매핑하는 대안을 **기각** — 그 목록은 `is_archived=False` 만 내려줌(`transaction/service.py:255-259`). ⓒ 거래 복사는 **평가조정(VALUATION) 에서 버튼 숨김** — 생성 경로가 "새 평가액 절대값 → 차액 계산"이라 값 복사가 무의미.
**1단계 QA (2026-08-17, 로컬 브라우저 실측) — 통과 + 버그 2건 잡음**: API 4경로(list/detail/ledger/calendar) 전부 `fixedExpenseName` 정상, 콘솔 에러 0. 화면에서 잡은 것 ⓐ **거래 복사 시 날짜가 원본 그대로** — `quick-add-sheet` 의 `key={editId ?? copyFromId}` 가 수정→복사 전환 시 **같은 거래 id 라 key 가 안 바뀌어** 재마운트가 안 됐고, `useEffect` 의존성 `[sourceId]` 도 값이 동일해 재실행 X. 수정 = key 에 `edit-`/`copy-` 모드 접두사 + 의존성에 `transactionId` 추가. **(교훈: 모드가 바뀌어도 식별자가 같으면 key 는 안 바뀐다)** ⓑ 제목과 서브라인에 고정지출명이 **두 줄 중복** — 메모를 안 쓰면 제목이 고정지출명이 되므로 항상 중복. 수정 = 서브라인은 `제목과 다를 때만` 이름, 같으면 카테고리. **로컬 dev DB 사전 정비 2건(내 변경과 무관한 기존 드리프트)**: `market_price_history` 테이블이 없어(alembic 은 `f1b3d5a7c9e2` 로 stamp 돼 있는데 실제 테이블 누락) `create_all` 로 보충 → `python -m scripts.seed_demo` 로 로컬 시딩(거래 547). 또 이 venv 는 콘솔 스크립트가 깨져 있어 `uv run pytest`/`uvicorn` 이 안 뜸 → **`uv run python -m pytest` / `python -m uvicorn` 으로 실행**.
**다음**: 2단계(수수료) 착수 전 사용자와 대화. 커밋/PR 은 아직 안 함(사용자 요청 시).

**(이전) 이력서 공개용 데모 계정 + 12개월 더미데이터 (2026-08-11 — PR #22 머지 + `v0.11.0` 배포 트리거까지 완료, 운영 육안 확인 미완).** 동기 = 이력서/포폴이 **글로만 설명**돼 채용담당자가 화면을 못 봄 → 도메인 + 계정을 공개해 직접 로그인하게 만든다. 체험 URL `https://household.jinho826.com`, 계정 `moeum@gmail.com` / `moeum2026` (`김모음` / "모음 데모 가계부"). **shell/cron 안 씀 — 기존 앱 인프라에 얹었다**: ① 매일 05:00 KST `register_jobs()` 6번째 잡(`reset_demo`) ② 기동 시 `lifespan` 에서 **"데모 가계부가 없을 때만"** 부트스트랩(`asyncio.create_task` 백그라운드 — 야후 호출+12개월 박제로 수십 초라 헬스체크를 막으면 안 됨. 재배포엔 아무 일도 안 해 체험 중 화면이 리셋되지 않음) ③ 수동 `python -m scripts.seed_demo`(게이트 무관). 셋 다 `run_locked_job` 을 타 **advisory lock 으로 인스턴스 2개여도 1개만 시딩** + 실패 시 기존 Discord 알림 경로에 자동 편승 — 새 알림 배선 0. **설계 핵심 2개**: ⓐ 리셋은 "추가분만 삭제"가 아니라 **데모 유저가 owner 인 household 전부 hard delete 후 재생성** (시드 데이터의 수정·삭제까지 원복해야 함. 체험자가 `POST /household` 로 새 가계부를 만들 수 있어 "1개"가 아니라 "owner 인 전부"가 기준) ⓑ 스냅샷·평가액을 **직접 INSERT 하지 않고 `_build_and_save_snapshot` 12회 호출** — 손으로 계산하면 잔액 공식(`account/service.py:291`)과 갈라져 화면과 어긋난다. `market_price_history` 는 시장 공통이라 삭제 제외, 시세는 `backfill_yahoo_monthly` 로 **실제 야후에서** 수집(rows=50 실측). **`users` 는 append-only** — 로그인 정보가 이력서에 인쇄돼 고정이어야 하고 `restore-drill.sh:144` 의 한글 UTF8 검증이 `users.name` 에 한글 1행을 전제하기 때문. **최대 리스크는 `DEMO_SEED_ENABLED` 기본 False** — `tests/multi_instance/` 가 진짜 uvicorn 을 띄워 lifespan 을 태우므로 켜져 있으면 게이트가 오염된다(회귀 **65 passed 유지** 확인). 검증 5종 전부 PASS: 멱등(2회 실행 539건 동일) / 회귀 65 / 부트스트랩 조건부(빈 DB 시딩 → 재호출 skip) / 격리(데모 토큰 + 남의 household_id → `HH001` 차단, 자기 것은 통과) / 수동 CLI. **금액 밸런스 1차 교정**: 초안이 생활비 통장에 1,621만이 고이고 비상금 통장 거래 0건이라 비현실적 → 이체 계획을 잉여(약 163만)에 맞춰 재배분(증권 60만·연금 30만·청약 20만·비상금 45만). 증권 이체는 월 매수액(약 55만)보다 커야 매수 현금이 음수로 안 빠짐 — 실측 현금 +1,076,325. 결과 순자산 1,400만 → **3,771만 우상향**. 데이터 규모: 거래 539 / 계좌 5 / 카테고리 13 / 고정지출 8 / 종목 5 / 스냅샷 60 / 평가이력 60. **남은 일**: 운영 `.env` 는 사용자가 직접 추가 완료 → `v0.11.0` 태그 푸시로 배포 트리거(결과 미확인). **다음 세션 첫 확인 3개** = ① 브라우저 로그인 육안 ② 컨테이너 로그에 `데모 시딩 완료` 찍혔는지(안 됐으면 `docker compose exec -T household-back python -m scripts.seed_demo`) ③ 다음날 05:00 `reset_demo` 로그 + 복구 리허설이 데모 데이터 늘어난 상태에서도 통과하는지. Figma 이력서 반영과 `GET /user/search` 이름 노출 차단은 **의도적으로 스코프 밖**(전자는 레이아웃 재배치 동반, 후자는 막으면 멤버 초대가 죽음).

**(이전) 로드맵 전 항목 종료 (2026-08-03) — 다중 인스턴스 멱등성 실증 완료.** PR #21 머지(CI green). 검증 명제 = **"상태를 DB 에 둔 것만 인스턴스 안전하다"**. 기존 A11(ASGITransport)·B6(한 프로세스 세션 2개)은 전부 한 프로세스 안 경합이라 `scheduler.py` 의 "다중 인스턴스/워커"와 `alert.py` 의 "인스턴스별로 따로 센다"가 증거 없는 서술이었다. **2층 구성** — ① 회귀 게이트: 호스트 uvicorn 프로세스 2개(`tests/multi_instance/`, E0~E4 6개, **59→65 passed**, test.yml reusable 이라 PR·배포 게이트 자동 편입·워크플로우 수정 0) ② 운영 구성 재현: `docker-compose.multi.yml`(운영 이미지·entrypoint→alembic→uvicorn, `proof_run.py` 3회 반복 전부 PASS). **LB 안 씀** — 라운드로빈이면 두 요청이 같은 인스턴스로 갈 수 있어 경합 미보장 → 주소 지정 = 인스턴스 지정. **결과**: 멱등성·advisory lock ✅ 프로세스 경계 넘어 성립 / 알림 쿨다운 ❌ 인스턴스별(E4 는 RED 아니라 자백 — C 섹션 성격). 라우팅 증거 = 같은 user·같은 초에 8101 은 `POST /transaction/create 200`, 8102 는 `idempotency cache hit`. E3 증명력은 **배선 검증**까지(락 판정 단위가 커넥션이라 프로세스 나눠도 의미론 동일). **발견 3건 — 전부 기록만, 소스 미수정**: ① `ddl/init.sql` 이 alembic baseline(`be504a39cec0`)에서 드리프트 → 빈 볼륨에서 `init.sql → upgrade head` 가 `fixed_expenses.amount` 에서 끊김(리비전 `a9668f7687a9` 가 없는 컬럼 drop 시도). 운영은 stamp head 라 무관, 재해복구는 덤프 복원이라 무관 — **새 환경 스키마 구축 경로만** 영향. `init_proof_schema.py`(create_all+stamp)로 우회, 근본 수정은 선택지가 갈려(초기 파일을 기준점으로 되돌리기 vs 기준점 재작성) 별건 ② 멱등 캐시 재생 요청은 access log 미기록 — `IdempotencyMiddleware` 가 `AccessLogMiddleware` 보다 바깥이라 재시도 트래픽이 관측 사각 ③ **HTTP 200 수신이 DB 커밋 가시성을 보장 안 함** — 보호 없는 경로에서 즉시 집계 0/1/2 흔들림(최종 항상 2), 멱등 경로는 즉시값도 안정(미들웨어가 body 다 읽고 커밋 후 반환). 메커니즘은 가설, 흔들림은 실측 → 단언을 `_settled_count`(기대값 폴링 + settle 재확인) 최종 상태 기준으로 교체. 리포트 `docs/multi-instance-proof.md`, `tests/SCENARIOS.md` E 섹션, 수치 재검증 **22/24/17/65**. carrer 반영(포폴 카드1·이력서·대본 Q11 신설·Figma) + 노션 하위 페이지까지 완료. **→ 남은 로드맵 없음.**

**(이전) 로드맵 4번 완료 (2026-07-31) — migration/rollback playbook.** `docs/migration-playbook.md` 신규 — 롤백 가능성을 "직전 이미지가 새 스키마에서 돌 수 있는가" 한 줄로 정의하고, 실제 24개 마이그레이션을 위험도 분류(additive/widen/backfill ✅ · drop 3건/rename in-place 2건 ❌ — 단일 배포로 한 이력 정직 기록), expand-contract 3단계(파괴적 DDL만 강제, additive 자유로 비용 조절), downgrade ≠ 운영 롤백(drop 복원은 default 0 — 운영 복원은 pre-deploy 스냅샷, 복원 덤프 alembic_version=구 head라 구 이미지 자동 upgrade no-op 자기일관), 사고 runbook 4케이스(A 리비전없음→rollback.yml만 / B additive 동반→**구 이미지 기동 불가 함정**: entrypoint alembic이 DB의 새 리비전을 해석 못 해 `Can't locate revision` 즉사 — roll-forward 기본, 급하면 alembic_version 수동 왕복 / C 파괴적→스냅샷 복원+유실 창 정산 / D 마이그레이션 자체 실패→transactional DDL이라 반적용 없음). 코드 변경 0 — 문서만. 로드맵 표·섹션 4 갱신, carrer 대본 Q5 격상(예고→실물 + 예상 반격 4개: "실제로 해봤나"→과거 이력 정직 인정 / downgrade 왜 안 쓰나 / 도중 죽으면 / 유실 창) 완료. **남은 로드맵 = 다중 인스턴스 실증뿐.**

**(이전) 로드맵 3번 구현 완료 (2026-07-31) — 장애 알림 "감지+자가복구 세트", PR #20 (CI green, 머지 대기).** 설계 원칙 = 서버 발신 알림은 서버가 죽으면 못 나감 → 감시 주체를 실패 유형별 분리: 앱(잡 실패·5xx)→Discord 직접(`app/core/alert.py` — 미설정 no-op / 발송 실패 격리 / 300s 쿨다운을 task 생성 앞 동기 판정), 백업·리허설→Healthchecks.io dead man's switch(성공 ping 끊김 감지 + /fail 즉시 ping. **trap ERR 대신 EXIT 백스톱** — `${VAR:?}` 확장·`|| fail` 없는 라인·함수 미상속 3구멍 때문), cron 유실→감지(HC)+예방(`register-cron.sh` 매 배포 재등록, 사용자 확정 "감지+자가복구 세트"), 배포/롤백 실패→Actions failure(), 서버 다운→UptimeRobot `/health` 5분. 5xx 는 글로벌 핸들러(미핸들, ServerErrorMiddleware 레벨)·access log(핸들된 응답) 두 지점이 중복 없이 상보. 테스트 **58 passed**(신규 9, scheduler 8 무수정=계약 유지 증명). codex 플랜 검증은 사용량 한도(Aug 5 리셋)로 자체 적대 검증 대체(1번 전례) — trap ERR 결함 발견이 성과. **운영 검증까지 완전 종료 (07-31)**: PR #20 머지 → v0.10.1 배포(register-cron 자가복구 첫 실행, notify job 정상 skip) → 외부 설정 전부 완료(webhook 발송 테스트 수신 / GH secret / HC 체크 2개+Discord 연동 / 서버 .env 3변수 / UptimeRobot). 실측: HC ping 수신("15 seconds ago"), 고의 `/fail` → Discord DOWN 알림 수신(**up→down 전이 시에만 알림** — down 상태서 통합 연결하면 조용한 함정도 확인). **계획 대비 변경 2건**: UptimeRobot free 가 Discord·메서드 잠금 → 서버 다운만 E-mail(채널 단일 장애점 회피로 정리) / UptimeRobot HEAD 고정인데 FastAPI GET 라우트가 HEAD 405(운영 실측) → `/health` 에 HEAD 명시 v0.10.2 배포로 해결. **다음 = carrer 쪽: 포폴 "알림·관측" 카드 부활 + 대본 Q6 "로그에만 남는다"·Q7 반격 2번 격상 갱신.** 이후 로드맵 4번 migration playbook.

**(이전) 로드맵 2번 완결 (2026-07-30) — 자동 복구 리허설.** `infra/backup/restore-drill.sh`(PR #18 머지) 매일 04:00 cron, 최신 R2 백업 → 임시 DB 복구 → 6항목 검증 → drop. 운영 호스트 **RTO=3s** 실측(거래 203건, 덤프 1MB 미만 — 속도 자랑 아니라 기준선 확보/추세 추적 프레임). 주 1회→매일 변경, 체크섬 미채택. **UTF8 인코딩 손상 검사가 핵심** (한글 `?` 치환인데 복구는 "성공"으로 보이는 실패를 로컬 드릴서 겪음) + **신선도 26h assert** (백업 멈춰도 옛 덤프는 복구되니 매일 OK 찍히는 구멍). 포폴 카드 4 + 이력서 블록 + 로드맵 반영 완료. 남은 흠 = 실패 알림 없음(로그만) → 로드맵 3번에서 닫힘.

**(이전) 로드맵 1번 전체 완결 (2026-07-28) — 테스트 49 passed(멱등성 14+3 / 스케줄러 8 / smoke 3 / 도메인 21, 멱등성 97% · 락 로직 100%) + CI 게이트(test.yml reusable 을 ci.yml·deploy.yml 이 공유, 백업 게이트와 2중). 2026-07-30 포폴 카드 4장 확정: 멱등성 / 테스트+CI / 배포 안전망 / 복구되는 백업 (+보조 2: advisory lock·AI 추적). 백필 카드는 도메인 설명 비용으로 제거 — 면접 재료로만 보존. 다음 = 로드맵 2번(주간 복구 리허설 + RTO 실측).**

**D 종합 (도메인 21 케이스)**: D1 원장 running balance 5(GREEN) / D1-6 깨진 커서 3(⚪ 의도된 한계) / **D2 실현손익 4(RED 2 → 소스 결함 1건 수정)** / D3 가계부 격리 5(GREEN) / D4 수량·상태 전이 4(GREEN). **D2 만 갈린 이유** — D1·D2 는 똑같이 "값을 두 곳에서 계산"하는 구조인데 D1 은 두 경로의 순서·규칙이 일치했고 D2 는 입력 순서 vs `tx_date asc` 로 갈렸다. 구조가 아니라 **두 경로가 같은 순서·같은 규칙을 보는가**가 결함을 가른다(면접 카드). **D4-2 가 D2 수정의 개선을 박제** — 수정 전엔 전량매도 시 `quantity` 가 마지막 보유량으로 남았고(운영 DB 점검서 25·101·115주 화석 확인), 이제 replay 가 0 까지 맞춘다. **D1-6c 면접 미끼**: 커서에 carry 를 실으면 클라이언트가 잔액 기준점을 통제 — 숫자로 파싱만 되면 서버가 검증 없이 쓴다. read-only 라 피해는 자기 화면뿐이고, 고치려면 매 페이지 재계산(성능) 또는 커서 HMAC 이라 이 규모엔 과함(C 와 같은 "수정 비용이 방어 논리를 가른다").

**D1 계좌 원장 running balance (`tests/domain/test_account_ledger.py`, 5 케이스 전부 GREEN, 소스 결함 0)**: 불변식 5개를 docstring 에서 도출 — A 닫힘(끝까지 순회 시 마지막 행 `balance_after − signed_amount == start_balance`) / B 페이지 불변(`limit=100` 1페이지 == `limit=2` 3페이지) / C 월 기준점(다음 달 거래 추가해도 당월 잔액 불변) / D 이체 부호(출금 −, 입금 +) / E 평가조정 부호(INCREASE +, DECREASE −). **착수 전 코드 독해로 GREEN 을 예측했고 그대로 나왔다** — D2 와 같은 "값을 두 곳에서 계산" 구조지만 `_signed_amount`(service.py:422) 와 `sum_for_account`(repository.py:185) 의 부호 규칙이 일치하고, 조회/합산 필터도 `or_(account_id, to_account_id)` 로 대칭이라 안 갈렸다. **결함을 가른 건 구조가 아니라 두 경로가 같은 순서·같은 규칙을 보는가** — D2 는 입력순서 vs `tx_date asc` 로 갈렸다(면접 대비 대비 카드). D1-2 에 **페이지 수 단언**(`paged_pages == 3`)을 넣어 커서가 한 페이지만 돌고도 통과하는 구멍을 막음. 계획: `~/.claude/plans/d1-snug-newt.md`. 미착수 엣지: D1-6 깨진 커서 fallback(`service.py:447,450-451`).

**D2 포트폴리오 실현손익 (`tests/domain/test_portfolio_pnl.py`, 4 케이스)**: 도출 불변식 = "활성 거래 집합이 같으면 realized_pnl·quantity·avg_price 는 재계산 트리거 시점과 무관하게 같다". D2-1(매도 후 재매수 평단)·D2-2(과거 BUY 수정 → SELL 재박제) GREEN, **D2-3·D2-4 RED**. 결함 = **진실 원천 2개** — `buy()`/`sell()` 은 incremental(입력 순서, 그 순간 `item.avg_price`)인데 `_recompute_realized_pnl()` 은 replay(`tx_date asc`, `repository.py:236`). 백데이팅 매수(매도보다 앞선 날짜를 뒤늦게 입력) 시 갈리고, **금액과 무관한 memo 수정 한 번**이 저장값을 뒤집었다(실현손익 25,000 → 0 / 평단 1,666.67 → 1,500). **수정**: buy/sell incremental 제거 → `_recalc_item_from_transactions`(replay) 통일, sell 사전 `realized_pnl` 박제 제거, 전량매도 판정을 replay 결과(`item.quantity == 0`) 기준으로. C 와 달리 **자백 아닌 수정** — 고치는 비용이 한 줄이라 "알고도 안 고쳤다"가 성립 안 함. **D3(IDOR) 강등**: 착수 전 `app/domain/*/service.py` public 함수 전수 스캔 → `find_by_id` 후 소속 검증 누락 0건(user 도메인만 예외 = 멤버 초대용 설계 선택, 라우터에 "인증 가드용" 주석 명시). C 핵심 실측: 라우터 성공 후 미들웨어 예외는 **거래 1건이 남는다**(A9 은 0건) → `call_next` 반환 시점에 `get_db` 커밋이 이미 끝났다는 직접 증거 = crash window 실재. C3 는 TTL 만료 후 재시도 시 **거래 2건** → exactly-once 아님 확정. codex 반영: 용어를 fault injection(C1)/state-based simulation(C2·C3)으로 분리, 다른 crash 지점 4개는 최종 상태가 C1·C2 로 수렴함을 논증해 기각, 면접 반격 4개는 `SCENARIOS.md` 표로 보관. B 는 codex 교차검증 3건 중 2건 채택 — `pg_backend_pid()` 상이 단언(독립 커넥션 자립 증명) + **B6 동시 `run_locked_job` 경합**(계약 "다중 워커 동시 진입 시 1개만" 직접 검증, `asyncio.Event` 로 순서 고정해 flaky 제거). nc 용어 정정: A11-nc(보호 제거 → N건)만 역증명, B 의 nc 2개는 상수 오작동 배제 **대조군**. 테스트 0개 → **명세 기반 사후검증**(소스 이미 있음, TDD 신규작성 아님). 계획: `~/.claude/plans/drifting-knitting-corbato.md`. 확정 결정: 실 PG(docker-compose.test) / 스키마 소스 **`Base.metadata.create_all`**(계획의 `alembic upgrade head` 는 이 레포 baseline 이 빈 마이그레이션이라 스키마 0개 생성 → 정정. decisions.md 참조) / 매 테스트 **TRUNCATE**(동시성 때문에 tx-rollback 불가) / **negative control 역증명**(결과 1건만으론 순차와 구분불가 → 락 우회 버전이 N건 만드는 걸 동일 하니스로 대조) / CI **ci.yml+deploy 게이트 둘 다** / 도메인 통합 D는 **🔴 4~5개만**. codex 순서 교차검증 시도 → 이 환경서 긴 프롬프트 hang("OK" 11초는 되나 4질문 분석은 5분+ timeout, 3회) → **자체 ultrathink 로 4질문 대체 검증**: 순서 타당하나 ⓪smoke·⑤CI 명시 추가, ASGITransport 동시성은 진짜 경합 성립(진짜 PG+독립세션+gather)이나 negative control 없으면 극장.

**핵심 제약**: 멱등성 미들웨어가 `async_session()` 직접 사용(DI 아님) → dependency_overrides 로 못 바꿈 → conftest 최상단서 `os.environ` 테스트 PG 주입 **후** 앱 import (config/database 가 import 시점 engine 생성). 멱등성 실증 엔드포인트 = `POST /transaction/create`(user→household→membership→account→category factory 필요).

**(이전 마일스톤) 로드맵·포폴 초안 (2026-07-21) + 노션 재료 (2026-07-22).** 포폴 6카드, 검증 숫자: 도메인 17 / 마이그레이션 22 / 태그 배포 18회 / 잡 5개 / AI 로그 183건. realized_pnl 백필 회고 반영(codex 3차 통과). "운영 성과" ❌ → "운영 준비도·체계" ✅ 톤. nginx 502 건 제외(미적용 상태).

## Context

- **로드맵 순서**: 1 테스트+CI 게이트(실 PostgreSQL 필수, 멱등성 asyncio.gather 동시 N발 + advisory lock 경쟁 + 선택 fault-injection) → 2 주간 자동 복구 리허설(RTO 실측) → 3 장애 알림(잡/백업/배포/헬스체크 webhook) → 4 migration playbook(expand-contract) → 실증: compose 앱 2개 다중 인스턴스 멱등성. 옵션: 용량 한계 실측·오버헤드·무중단 배포. 상세·확정 판단 표는 `docs/portfolio-sre-roadmap.md`.
- **핵심 제약 (재논의 X)**: k6 절대 처리량 폐기 / 멀티스레드 클라이언트 무의미(단일 이벤트 루프) / 멱등성 주장은 "동시 in-flight 중복 방지"까지 — crash window(비즈니스 커밋 후 COMPLETED 전 죽음)는 포폴에 안 쓰고 면접 미끼 / "exactly-once·무장애" 표현 금지.
- 브랜치: `docs/portfolio-sre-roadmap`.

## Next Step

- 진행중: 단계 4/8 · .claude/plans/w5-tx-account-integrity.md (마스터 ../.claude/plans/cosmic-napping-locket.md) · W5 · 시작 ea53726 · finding 0
0. **[현재 트랙] MVP 추가개발** — 1단계 완료(미커밋). **2단계 = 수수료** 착수 전 사용자 확인. 3단계는 2단계 fee 를 카드에 쓰므로 순서 고정. 계획 `~/.claude/plans/wobbly-prancing-pebble.md`.
1. ~~**로드맵 1번 전체**~~ **완료** — 테스트 49(⓪ smoke / A 멱등성 14 / B advisory lock 8 / C fault-injection 3 / D 도메인 21) + CI 게이트(ci.yml·deploy.yml → test.yml reusable 공유) + 포폴 실측값 반영. 상세 이력은 Status 참조.
2. ~~**포폴 카드 확정**~~ **완료** (2026-07-30) — 4장 + 보조 2장, 백필 제거. 정본 `carrer/portfolio/household-back.md`, 이력서 블록 `carrer/resume/household-이력서-블록.md`.
3. ~~**로드맵 2번 — 자동 복구 리허설**~~ **완료** (2026-07-30) — 매일 04:00, RTO=3s. 상세는 Status 참조.
4. ~~**로드맵 3번 — 장애 알림**~~ **구현 완료** (2026-07-31, PR #20) — 남은 건 머지 + 외부 설정 5스텝 + 운영 검증 (상세 Status). 완료되면 "리허설 실패 알림 없음" 미끼가 구조적으로 닫힘.
5. ~~로드맵 3번 운영 검증 + 포폴/대본 반영~~ **완료** (2026-07-31) / ~~로드맵 4번 migration playbook~~ **완료** (2026-07-31, `docs/migration-playbook.md` + 대본 Q5 격상). **다음: 다중 인스턴스 멱등성 실증** — 로컬 compose로 확정, 설계 탐색 완료 (2026-07-31, 구현 미착수):
   - 구성: `docker-compose.multi.yml` 신규 — postgres-multi(55433, tmpfs) + app 2개(`build: .`, 호스트 18001/18002). **entrypoint 의 `alembic upgrade head` 는 건너뜀** (빈 baseline 이라 스키마 못 만듦 — conftest 와 같은 이유) → entrypoint override 로 uvicorn 직행, 스키마는 호스트 스크립트가 `Base.metadata.create_all`.
   - 앱 env: DATABASE_URL(postgres-multi 향), **JWT_SECRET 은 .env.test 값과 동일하게** (스크립트가 `token_for` 로 직접 발급한 토큰을 앱이 검증해야 함), DISCORD_WEBHOOK_URL 빈 값.
   - 스크립트(scripts/ 예정): conftest 방식 env 선주입 후 app import, `tests/fixtures/factory.py` 의 `seed_transaction_context` 재활용. 동일 키 10개 × 4발(인스턴스당 2발) gather → 거래 10건·레코드 10건 COMPLETED + 키별 200 최소 1개 + **200 바디 동일(교차 인스턴스 캐시 재생 증명)**. negative control 은 **Idempotency-Key 헤더 생략** (미들웨어 게이트가 헤더 없으면 패스 — A2/A11-nc 와 동일 원리) → 4발이 4건.
   - 확인된 사실: 성공 응답은 200(201 아님, 라우터 status_code 미지정) / 동시 나머지는 409 또는 200 캐시 / 두 인스턴스 lifespan 에서 scheduler 둘 다 시작 — advisory lock 실전이지만 실험 창 짧아 무해.
   - 완료 시: 포폴 멱등성 카드 한 줄 추가 + 대본 Q4 "단일 프로세스 한계" 인정 문구 격상.
6. carrer 쪽 남은 것: JD 생기면 AI 셀프리뷰 3프롬프트, 대본 소리 내 연습. "복구 리허설 1회" 미끼는 2번으로, "백업 실패 감지" 역공은 3번으로 닫힘(운영 검증 후).

**노션 누적 정리 규칙**: 트랙별 하위 페이지에 `## NN.` 섹션 이어붙임 (다시 지시 없어도). 규칙 상세는 harness 메모리 `phase1-notion-worklog`.
- 트랙 1 → "Phase 1 · 테스트 + CI 게이트 — 진행 기록" (https://app.notion.com/p/3a7a6161032981bd8f8bf4f4196584ac)
- 트랙 2 → "Phase 2 · 자동 복구 리허설 — 진행 기록" (2026-07-30 개설)
- 트랙 3 → "Phase 3 · 장애 알림 — 진행 기록" (https://app.notion.com/p/3aea6161032981a692bfec11b45dfa68, 2026-07-31 개설·완결 기록)
- 부모 "운영 준비도 로드맵" (https://app.notion.com/p/3a7a6161032980729907d63a113b83cb) 의 `## 1. 작업 목록` 상태·`## 4. 진행 기록` 표도 같이 갱신할 것 — 트랙 1 이 완료됐는데도 ⬜ 로 방치돼 있던 전례 있음.
