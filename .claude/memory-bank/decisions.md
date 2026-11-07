# 결정 기록

## 2026-11-07 — 박제는 가계부 advisory lock + 활성 부분 unique, 매매는 종목 FOR UPDATE 를 첫 쓰기보다 먼저
- 결정: 수동 박제(시세 upsert 뒤)·월 잡(가계부 루프 맨 위)이 pg_advisory_xact_lock(1001, hashtext(household)) 대기. account_snapshots·PVH 활성 (owner, 월) 부분 unique(중복 정리 리비전 분리).
- 매매: buy·sell·종목 수정/삭제·거래 수정/삭제가 종목 SELECT FOR UPDATE 를 먼저, 거래 수정/삭제는 lock 뒤 tx refresh(삭제됐으면 404). 데모 purge 는 종목→매매거래 순, 시세 upsert 는 정렬 — lock 순서 통일.
- 왜: 더블클릭·잡 겹침에 통장마다 2벌 박제(yearly 2배), 동시 매도가 둘 다 보유 검사 통과(초과 매도), 거래 수정↔과거 매수가 데드락(테스트로 재현).
- 버린 옵션: unique 만(동시 요청 500) / try-lock(두 번째 실패 응답) / SERIALIZABLE(전 요청 재시도 필요).
- 외부 검토 미실시: Codex 모델 설정 400 → Claude 새 컨텍스트 반박(Blocker 2 반영)·리뷰 2회·최종 리뷰(테스트 barrier 2건 수정, 변이로 RED 확인).
- 마이그레이션: d3f5b7c9e1a2(dedupe)·e4a6c8d0f2b3(unique) 테스트 DB 왕복만. 운영 적용 전 중복 건수 확인·월 1일 잡 시간대 피하기(CREATE INDEX SHARE lock).
- 상세: .claude/plans/done/w6-snapshot-unique-trade-lock.md · tests/domain/test_concurrency_w6.py

## 2026-11-07 — 거래 수정은 바뀐 값만 검증, 타입 전환 규칙을 생성과 맞춤 · 카테고리 kind·통장 타입 그룹 변경 차단
- 결정: 수정 PUT 은 새로 고른 FK 만 소속·ACTIVE 검증(유형이 바뀌면 출발 통장 포함), 남은 값은 유형 규칙·kind 만. 명시된 null 은 해제. 전환 시 방향·카테고리·고정지출 정리, 필수값은 관련 칸이 바뀔 때만.
- 차단: 활성 수입·지출 거래나 고정지출이 쓰는 카테고리의 kind 변경(CT001) · 거래·종목·매매내역 있는 통장의 투자/수동자산/일반 그룹 변경(AC002). 통장 삭제 cascade 는 to_account 없는 단독 거래 전부.
- 왜: 삭제된 고정지출·카테고리가 걸린 거래는 메모도 못 고쳤고, 전환 뒤 남은 값이 월 합계·카테고리 차트·잔액을 어긋나게 했다.
- 버린 옵션: 수정 API PATCH 재설계(프론트 계약) / 삭제된 FK 자동 해제(모르게 데이터 변경) / 통장 타입 변경 전면 금지.
- 외부 검토 미실시: Codex 모델 설정 400 → Claude 새 컨텍스트 리뷰(9건 중 5 수정 · 1 W7 이관 · 2 주차장 · 1 리팩터) + 최종 리뷰(1건 수정).
- 상세: .claude/plans/done/w5-tx-account-integrity.md · tests/domain/test_integrity_w5.py

## 2026-11-07 — DB commit 을 응답 전에(DbDepends = scope="function")
- 결정: get_db 의존성을 전부 Depends(get_db, scope="function") 하나(DbDepends)로. 모든 라우트가 그 scope 인지 테스트(W4-3)가 막는다.
- 왜: 기본 request scope 는 yield 뒤 commit 을 응답을 보낸 뒤 실행 → commit 실패해도 200, 멱등 미들웨어가 그 200 을 COMPLETED 로 캐시해 재시도까지 막혔다(테스트로 재현).
- 버린 옵션: 핸들러마다 명시 commit(66곳·누락 위험) / 미들웨어가 비즈 세션 commit(소유권 꼬임) / Annotated 별칭 통일(기본값 순서 때문에 매개변수 재배치 필요).
- 상세: .claude/plans/done/w4-commit-before-response.md · tests/idempotency/test_commit_order.py

## 2026-11-07 — 보관 통장은 목록엔 두고 합계에서만 뺀다
- 결정: 홈·자산·투자 overview 의 total_balance·배분·투자 요약은 counts_in_totals(비보관)만. 계좌 목록은 전부 그대로. 월 박제도 같은 함수.
- 왜: 박제는 보관 통장을 빼는데 현재 합계는 더해서 hero "지난 기록 대비" 증감이 그 잔액만큼 튀었다. 목록까지 거르면 API 로 보관된 통장을 앱에서 다시 열 길이 없다.
- 결과: 마지막 박제 뒤에 보관하면 그달 증감이 잔액만큼 − 로 보인다(자산이 집계 밖으로 나간 것 — 의도).
- 버린 옵션: 박제에 보관 포함(사용자 A 선택) / 목록도 거르기(리뷰 finding) / 프론트에서 거르기(합계가 백엔드).
- 상세: .claude/plans/done/w3-archived-out-of-totals.md

## 2026-11-07 — OTHER 시세는 종목 id 키로, 과거 값은 PVH 에서 backfill
- 결정: market_price_history 의 OTHER 행 code = str(item_id), 컬럼 20→50. 옛 (code, OTHER) 행은 안 읽고, 각 종목이 그 달 박제에 쓴 PVH current_price 를 새 키로 옮긴다(리비전 분리).
- 왜: code "" 를 여러 가계부가 공유해 월 잡 upsert 가 통째로 롤백되고(재현됨) 남의 수동가로 평가됐다. 옛 키 fallback 은 같은 공유 문제라 버림.
- 버린 옵션: household_id/item_id 컬럼 추가(조회 경로 둘) / 옛 키 fallback / PVH 직접 조회(재박제 순서 의존).
- 검증: pytest W2-1~7 + backfill 은 테스트 DB 수동 왕복만(자동 테스트 없음). 외부 검토: Codex 401 → Claude 새 컨텍스트 반박·최종 리뷰로 대체.
- 상세: .claude/plans/done/w2-other-price-key.md · 리비전 b7d1e3f5a9c2, c2e4a6b8d0f1

## 2026-11-07 — 전체 점검 후속을 W1~W8 로 나눠 순서대로 (풀오토)
- 결정: 계산식·도메인 버그·입력 편의 점검 결과를 W1(팝업 지출) → W2(OTHER 시세 키) → W3(보관 통장 총자산 제외) → W4(응답 전 commit) → W5(거래·계좌 무결성) → W6(박제 unique·매도 lock) → W7(인증·데모 보호) → W8(입력 편의) 로.
- 왜: 한 루프로 가면 리뷰·커밋이 섞이고 위험 작업(W6·W7)은 반박·Codex 가 따로 필요. 잡 전체 롤백(W2)·사용자가 본 숫자(W1)가 먼저.
- 버린 옵션: 한 루프 / B1 에 household_id 컬럼(마이그레이션 비용) / 백엔드 total_expense 에 고정지출 합산(계좌 리포트 의미 꼬임) / 새 필터값 SPEND.
- 정책: 보관 통장은 총자산·배분에서 제외(박제와 같은 기준). push 안 함 · W6 마이그레이션은 테스트 DB 만 · 데모는 비번·이메일·가계부 삭제만 차단.
- 상세: 마스터 plan `../.claude/plans/cosmic-napping-locket.md`

> `/decide` 호출 시 결정 결과가 여기에 자동 append.
> 형식: `YYYY-MM-DD: <주제> — <선택 옵션> (<근거>)`

---

2026-06-03: 통장 삭제 시 이체 처리 — D안(이체 행 유지) (상대 통장이 살아있으면 이체 행을 보존해 상대 잔액·통계 무결성 유지. A안(이체→단순거래 변환)은 통계왜곡+비가역이라 탈락. 거래조회 양방향 `or_(account_id, to_account_id)` + transfer_in/out 분리집계라 코드상 자연스럽게 동작)
2026-06-03: 통장 삭제 정책 — 보유종목만 차단 + 나머지 cascade soft-delete (단독거래/수동자산/종목이력/스냅샷은 함께 삭제. 투자 데이터(보유종목)만 보호 위해 차단 유지)
2026-06-03: 카테고리 삭제 — 차단 제거하고 soft-delete만 (category_id 유지. `find_by_ids`가 data_stat 필터 없어 기존 거래는 이름 조회 O, `find_active_by_household_id`로 새 입력 선택목록엔 제외. 코드 변경 거의 없이 충족)
2026-06-04: 수동자산 모델 — 통장(account) 일원화 + 평가조정(VALUATION) 거래 (ManualAsset+전용계좌 이원화의 '평가액+이체' 단순합산이 평가액 재입력 시 이체분 이중계상. 모든 자산을 account로 통일, 가치변동은 VALUATION 거래로. 잔액=start_balance+거래합. 대안 '성격별 분리'·'이체를 평가액에 흡수'는 복잡도/모호성으로 탈락)
2026-06-04: 평가조정 부호 표현 — valuation_direction 컬럼(INCREASE/DECREASE) (amount는 양수 유지. signed amount는 amount>0 불변량 깨고, UP/DOWN 2타입은 '평가조정' 개념 분할. 방향 플래그가 이체 패턴과 일관)
2026-06-04: 평가액 입력 UX — 현재 총액 절대값 입력 → 차액 자동 VALUATION (사용자는 '지금 얼마'만 입력, 시스템이 (새값-현재잔액) 차액을 평가조정 거래로 생성. 이체는 기존 그대로 별도)
2026-07-02: 자산 추이 원가박제 해결 — market_price_history 시세이력 + 종목별 출처 분기 (A안 정공법. 박제 시 원가 대신 그 달 시가로 평가 → 손실이 자산추이에 반영. 야후 종목(KRX/US)은 월봉 `interval=1mo`로 미래수집+과거backfill, 금 등 OTHER는 박제시점 current_price를 그 달로 저장('현재부터', 과거 소급X). 시가 없으면 원가 fallback이라 회귀 없음. B안(current_price 직박제)은 catch-up 왜곡으로 탈락)
2026-07-02: 박제 시가조회 키 — tx기반(asof_holdings) 아니라 item 현재 (code,market) (실데이터에서 종목 market 변경 이력 발견: tx=KRX_KOSPI인데 item=OTHER로 바뀐 금 종목. asof_holdings는 tx.code/market이라 시세이력(item 기준 저장)과 어긋나 원가 fallback로 샘. value_holdings_at_month가 item_id로 현재 code/market 교정해 정합)
2026-07-21: 포폴 포지션 — "저비용 단일 서버 운영 감각" 보조 카드 (codex 2회 교차검증. 메인4=배포안전망·멱등성·백업복구·알림관측 + 보조2=advisory lock·AI추적. 실사용 미미라 "운영 성과" 아닌 "운영 준비도" 톤. exactly-once/무장애 표현 금지, crash window 는 면접 미끼로 비움)
2026-07-21: 포폴 실측 로드맵 — 1 테스트+CI(실 PG) → 2 주간 복구 리허설(RTO) → 3 장애 알림 → 4 migration playbook (k6 절대처리량 폐기 — 1vCPU 숫자 무의미. 동시 중복 검증은 k6/멀티스레드 아닌 pytest asyncio.gather + compose 인스턴스 2개 실증. 상세 docs/portfolio-sre-roadmap.md)
2026-07-24: 테스트 스키마 생성 — alembic upgrade head (session 1회) (metadata.create_all 은 모델기준 → 마이그레이션 22개와 drift 시 테스트통과·운영깨짐. alembic 경로가 CI서 검증되는 부가효과. 이후 TRUNCATE 격리)
2026-07-24: 테스트 격리 — 매 테스트 TRUNCATE CASCADE (tx-rollback 아님. 멱등성 동시성 테스트는 요청마다 독립 커넥션 필요 → 단일커넥션 롤백 fixture 는 경합 무효화. alembic_version 제외)
2026-07-24: 동시성 테스트 신뢰성 — negative control 역증명 필수 ("결과 1건"만으론 순차 실행과 구분불가 = 올바른 멱등성은 순차도 1건. 락 우회 버전이 동일 하니스로 N건 만드는 걸 보여야 경합이 진짜임 증명. 포폴·면접 카드로도 강력)
2026-07-24: CI 게이트 위치 — ci.yml(push/PR) + deploy.yml(needs:test) 둘 다 (deploy 는 tag push 트리거뿐 → 그것만 걸면 평소 "깨진 채 머지". 백업 게이트와 2중)
2026-07-24: 도메인 통합테스트(D) 범위 — 🔴 핵심 4~5개만 (transaction 잔액/portfolio realized_pnl/household IDOR/auth. 전수 17개는 ROI 낮고 포폴 초점 흐림. 돈·권한 경로만 두껍게)
2026-07-27: 포트폴리오 실현손익 진실 원천 — replay 단일화 ✅ (buy/sell 의 incremental 계산 제거 → `_recalc_item_from_transactions` 로 통일. 대안 "백데이팅 차단"은 과거 거래를 뒤늦게 입력하는 정상 사용을 막는 제품 제약이라 탈락, "자백으로 남김"은 수정 비용이 재계산 호출 한 줄이라 C(crash window, outbox급 필요)와 달리 "알고도 안 고쳤다"가 성립 안 해 탈락. `_recompute_realized_pnl` docstring 이 이미 replay 를 정본으로 선언 = 계약 준수 방향)
2026-07-27: D 도메인 테스트 대상 재확정 — D3(IDOR) 강등, D2 최우선 (착수 전 `app/domain/*/service.py` public 함수 전수 스캔 결과 `find_by_id` 후 소속 검증 누락 0건 → D3 는 GREEN 이 뻔해 회귀 안전망 가치만. user 도메인 예외는 멤버 초대용 설계 선택. 대신 D2 는 코드 독해 단계에서 진실 원천 2개를 발견 → 실측으로 결함 확정)
2026-07-24: 테스트 스키마 생성 정정 — alembic upgrade head ❌ → Base.metadata.create_all ✅ (⓪ 착수해보니 이 레포 alembic baseline 이 빈 마이그레이션 = 운영은 init.sql + `stamp head`. upgrade head 로는 스키마 0개 생성됨. init.sql 도 코드 불일치(idempotency_records 누락)라 소스로 못 씀 → SQLAlchemy 모델이 유일 완전 진실. drift 위험은 별도 인지하되 스키마가 아예 안 생기는 것보다 우선. 앞 결정(2026-07-24 alembic upgrade head) 뒤집음)
2026-07-30: 포폴 카드 최종 확정 — 멱등성 / 테스트+CI / 배포 안전망 / 복구되는 백업 4장 + 보조 2장 (백필 카드는 투자 도메인 설명 비용으로 제거·면접 재료로만 보존. 2026-07-21 "메인4=배포·멱등성·백업·알림관측" 구성을 대체 — 알림관측은 로드맵 3번 구현 후 재추가. 정본: carrer/portfolio/household-back.md)
2026-07-31: 장애 알림 채널 — Discord webhook (기존 운영 채널이라 설정 비용 0, curl 한 줄, 외부 모니터링 서비스 Discord 연동 전부 free. Telegram은 기존 이력 없어 이득 없음, ntfy.sh는 무료 topic 보안 약점으로 탈락)
2026-07-31: 헬스체크·cron 감시 — Healthchecks.io + UptimeRobot free 조합 (잡는 실패 유형이 상호배타: dead man's switch=cron 생존, /health 폴링=서버 생존. Uptime Kuma는 동일 서버 자기모순, GH Actions schedule은 지연 5~60분+60일 비활성 자동꺼짐으로 탈락. 서버 발신 알림은 서버가 죽으면 못 나가므로 감시 주체를 서버 밖에 두는 게 원칙)
2026-07-31: cron 자가복구 — 등록부를 register-cron.sh로 분리해 deploy.yml SSH step에서 매 배포 재등록 (멱등·sudo 불필요. HC 감지와 보완: 예방=배포 시 자가복구, 감지=배포 없는 기간 유실을 HC가 잡음. install.sh 통째 실행은 sudo 필요 스텝 때문에 배포에 안 섞음)
2026-08-17: MVP 추가개발 진행 방식 — 6+1 항목을 4단계로 쪼개 하나씩 (사용자 "한번에 다 하지말고 나랑 얘기하면서". 순서 = ①고정지출라벨+거래복사(마이그레이션 0) → ②수수료 → ③매매손익 카드+매도수정 → ④USD. ②→③ 은 카드가 fee 를 표시해 순서 의존. 백엔드만 TDD(RED→GREEN), 프론트는 육안 검증)
2026-08-17: 거래 응답의 고정지출명 — `FixedRepository.find_by_ids` 를 상태 필터 없이 (고정지출 삭제는 soft delete 라 FK SET NULL 이 안 터지고 거래는 fixed_expense_id 를 계속 보유 → ACTIVE 로 거르면 보관/삭제된 항목의 과거 거래가 `고정지출 · —` 로 깨짐. 2026-06-03 카테고리 결정의 선례를 그대로 계승. 프론트가 formOptions.fixedExpenses 로 매핑하는 대안은 그 목록이 is_archived=False 만 내려줘 탈락)
2026-08-17: 매매 수수료 반영 범위 — 평단 가산 + 실현손익 차감 + 매매현금 반영 (증권사 계산과 동일. "실현손익만 차감"·"표시만"은 화면 숫자가 증권사 앱과 갈려 탈락. 기존 거래는 fee=0 이라 replay 결과 불변 = 회귀 없음)
2026-08-17: USD 거래 저장 — KRW 컬럼을 진실 원천으로 두고 거래통화 컬럼 병행 추가 (거래통화로 전환하는 대안은 account/service.py:305-306·wealth/service.py:165·portfolio/repository.py:136 이 quantity*current_price 를 통화 구분 없이 합산해 순자산이 조용히 틀림. 표시·손익률만 거래통화 기준(토스증권식 $ 주 + ₩ 보조). 거래시점 환율은 tx 에 박제하되 currency_rates 가 최신값 1행뿐이라 과거 소급 입력은 최신 환율로 박제되는 한계 인지)
2026-08-17: 외화 계좌의 진실 원천 — "계좌는 외화가 진실" ❌ → **KRW 를 합산 기준으로 유지 + native_balance 병기** ✅ (초안을 codex 검증으로 뒤집음. 투자계좌 공식이 `balance = cash + portfolio_valuation` 인데 현금을 USD 원본으로 두면 `$1,000 + ₩3,000,000` 처럼 **단위가 다른 값을 더하게** 된다. 게다가 Stage 4 에서 종목을 KRW 진실로 확정해 같은 계좌 안에서 두 원칙이 충돌. 대신 `native_balance`(계좌통화, 원장의 진실) / `balance`(KRW, 조회 시 현재 환율로 계산)로 분리 → 순자산·자산구성 합산 경로를 **손대지 않아도 된다**. 초안이 걱정한 "환율이 올라도 자산이 안 는다"는 balance 를 저장값이 아니라 계산값으로 두어 해결)
2026-08-17: 외화 계좌 1차 범위 — 비투자 계좌만, 투자계좌는 외화 거부 (외화 투자계좌는 한 계좌 안에 일반 입출금 amount=USD / 매매 정산액=KRW / 종목 평가액=KRW 세 통화가 섞여 `cash - pt_sums["buy"]` 가 USD 에서 KRW 를 빼는 계산이 된다. 풀려면 매매 정산 통화까지 계좌 통화로 재정의해야 해 Stage 4 를 다시 건드림. 실사용 대상이 예금(토스뱅크 외화통장)이라 1차는 충분)
2026-08-17: 계좌 통화는 생성 후 불변 (거래 amount 의 통화를 계좌에서 도출하므로 currency 를 바꾸면 과거 모든 거래 금액의 의미가 통째로 바뀐다 — 원화 계좌를 USD 로 바꾸는 순간 과거 100000 이 ₩100,000 에서 $100,000 으로 재해석. 수정 API 에서 변경 거부하고, 통화를 바꾸려면 계좌를 새로 만든다)
2026-08-17: 매매손익 기간 필터 — 프리셋 4개 ❌ → 날짜 2개 + 기본 당일 ✅ (증권사 매매손익 방식. 누적 성과는 바로 위 레일이 이미 보여주므로 '전체' 프리셋이 중복이었고, '직접'을 눌러야 날짜가 나오는 2단 구조도 불필요. 부작용으로 시트 진입 시 대부분 0건이라 레일 누적 표시와 괴리가 생기는 건 감수)
2026-09-20: 월간 집계 통장 필터 — `account_id` 옵션 파라미터 + 이체는 양쪽 통장에 집계 (거래 탭에서 좌측 목록만 통장 필터가 걸리고 우측 요약·달력·지출Top5 는 가구 전체라 숫자가 어긋나던 문제. `sum_by_type_for_month`·`sum_by_category_for_month`·`daily_sums_for_month` 와 `/stats/monthly`·`/transaction/calendar/{y}/{m}/full` 에 `account_id: UUID | None = None` 추가. 조건은 `_account_involved` = `or_(account_id, to_account_id)` — 좌측 원장이 이미 이체를 출금·입금 양쪽에서 보여주므로 집계도 양쪽에 잡혀야 목록↔요약이 맞는다. '출금 통장만 집계' 대안은 목록엔 있는데 요약엔 없는 새 불일치를 만들어 탈락. 기본값 None 이라 홈(`home/service.py:46`) 등 기존 호출부 무영향 — 회귀 방어 테스트 D6_4. 라우터는 `Query(None, alias="accountId")` 로 portfolio 의 fromDate 패턴을 따름)
2026-09-20: TRANSFER 는 수정 경로에서도 카테고리를 떼어낸다 — `_normalize_category` 추가 (생성은 `schema.py` 의 `_validate` 가 TRANSFER+category_id 를 막지만 `update_transaction` 엔 막이 없고 `_category_kind_matches` 는 TRANSFER 에 True 를 돌려줘, `PUT /update` 로 EXPENSE→TRANSFER 전환 시 category_id 가 살아남았다. 위 `_account_involved`(출금 OR 입금) 때문에 그런 행이 두 통장 양쪽에서 같은 카테고리에 더해져 "통장별 합 > 가계부 전체" 가 된다. `_normalize_to_account` 바로 옆에 짝을 둬서 정규화 규칙을 한자리에. `_category_kind_matches` 를 False 로 바꿔 400 을 던지는 대안은 이미 그 상태로 저장된 기존 행의 수정까지 막아 탈락. 재현 테스트 D6_6 으로 RED 확인 후 수정 — 코드리뷰가 찾아준 선결 버그)
