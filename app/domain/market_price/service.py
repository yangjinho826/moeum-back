"""portfolio_items.current_price 자동 갱신 서비스.

스케줄 잡(KR 16:10 / US 09:10) 이 같은 refresh() 호출 — markets 만 다름.
USD 시장(NASDAQ/NYSE) 은 시세 갱신 시점에 currency_rates(USD/KRW) 환율 적용해 KRW 박제 —
sum_valuation_by_account 의 qty * current_price SUM 이 단일 통화에서만 의미 있어서.

야후 심볼은 Market enum 의 yahoo_suffix 로 1:1 매핑 — fallback 없음, 항상 1번 호출.
"""
import asyncio
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exchange_rate.enum import CurrencyCode
from app.domain.exchange_rate.repository import CurrencyRateRepository
from app.domain.market_price.repository import MarketPriceHistoryRepository
from app.domain.market_price.yahoo_client import fetch_chart_quote, fetch_monthly_closes
from app.domain.portfolio.enum import Market
from app.domain.portfolio.repository import PortfolioItemRepository
from app.domain.portfolio.yahoo import build_yahoo_symbol

logger = logging.getLogger(__name__)

# USD 시장 — KRW 환산 분기용
_USD_MARKETS = frozenset({Market.NASDAQ, Market.NYSE})

# 청크 병렬화 — Yahoo rate-limit 회피 + 종목 N개 확장성
_CHUNK_SIZE = 10
_CHUNK_SLEEP_SEC = 0.2


@dataclass
class RefreshResult:
    fetched: int        # Yahoo 응답 받은 종목 수
    skipped: int        # fetch 실패 종목 수
    updated_rows: int   # DB row 업데이트 수


async def refresh(
    session: AsyncSession,
    markets: list[Market],
    household_id: UUID | None = None,
) -> RefreshResult:
    """주어진 시장들의 활성 portfolio_items.current_price 갱신.

    household_id 가 없으면 전 가계부(스케줄 잡), 있으면 그 가계부 보유 종목만 fetch(수동 갱신).
    가격은 시장 공통이라 bulk update 는 매칭되는 전 가계부 row 에 적용.

    1. USD 시장이 포함되면 최신 환율 fetch — 없으면 USD 시장 제외
    2. (code, market) DISTINCT 추출
    3. 청크 병렬 (_CHUNK_SIZE 개씩 asyncio.gather), 청크 사이 sleep
    4. 가격 캐시 모은 후 bulk update
    5. fetch 실패는 per-item skip + 로그 (yahoo_client 내부 retry 1회)
    """
    if not markets:
        return RefreshResult(0, 0, 0)

    needs_fx = any(m in _USD_MARKETS for m in markets)
    fx_rate: Decimal | None = None
    if needs_fx:
        latest = await CurrencyRateRepository(session).find_by_pair(
            CurrencyCode.USD, CurrencyCode.KRW,
        )
        if latest is None:
            logger.error(
                "환율 없음 — USD 시장 종목 갱신 skip. 환율 잡 먼저 실행 필요"
            )
            markets = [m for m in markets if m not in _USD_MARKETS]
            if not markets:
                return RefreshResult(0, 0, 0)
        else:
            fx_rate = latest.rate
            logger.info("USD 환산 환율 적용 (%s)", fx_rate)

    repo = PortfolioItemRepository(session)
    if household_id is None:
        pairs = await repo.find_active_distinct_code_market_by_markets(markets)
    else:
        pairs = await repo.find_active_distinct_code_market_by_household_and_markets(
            household_id, markets,
        )

    prices_to_apply: dict[tuple[str, Market], Decimal] = {}
    fetched = 0
    skipped = 0
    chunk_list = list(_chunks(pairs, _CHUNK_SIZE))
    last_idx = len(chunk_list) - 1

    for idx, chunk in enumerate(chunk_list):
        results = await asyncio.gather(
            *[_fetch_one(code, market, fx_rate) for code, market in chunk],
            return_exceptions=True,
        )
        for (code, market), price_or_exc in zip(chunk, results, strict=True):
            if isinstance(price_or_exc, Exception):
                logger.warning(
                    "fetch 예외 (code=%s, market=%s): %s",
                    code, market, price_or_exc,
                )
                skipped += 1
                continue
            if price_or_exc is None:
                skipped += 1
                continue
            prices_to_apply[(code, market)] = price_or_exc
            fetched += 1

        if idx < last_idx:
            await asyncio.sleep(_CHUNK_SLEEP_SEC)

    updated_rows = await repo.bulk_update_current_price_by_code_market(prices_to_apply)

    logger.info(
        "시세 갱신 (markets=%s, fetched=%d, skipped=%d, rows=%d)",
        [m.value for m in markets], fetched, skipped, updated_rows,
    )
    return RefreshResult(fetched=fetched, skipped=skipped, updated_rows=updated_rows)


def _chunks(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


async def _fetch_one(
    code: str, market: Market, fx_rate: Decimal | None,
) -> Decimal | None:
    """시장별 1:1 야후 심볼로 1번 호출. USD 시장이면 fx_rate 곱해 KRW 환산."""
    symbol = build_yahoo_symbol(market, code)
    quote = await fetch_chart_quote(symbol)
    if quote is None or quote.price <= 0:
        return None
    price = quote.price
    if market in _USD_MARKETS and fx_rate is not None:
        price = price * fx_rate
    return price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# =========================================================
# 월별 시세 이력(market_price_history) 수집 — 자산 스냅샷 시가 박제용
# =========================================================

# backfill 대상 야후 시장 — OTHER(야후 미지원) 제외.
_YAHOO_MARKETS = [
    Market.KRX_KOSPI,
    Market.KRX_KOSDAQ,
    Market.NASDAQ,
    Market.NYSE,
]


async def _resolve_usd_krw(session: AsyncSession) -> Decimal | None:
    latest = await CurrencyRateRepository(session).find_by_pair(
        CurrencyCode.USD, CurrencyCode.KRW,
    )
    return latest.rate if latest else None


async def backfill_yahoo_monthly(
    session: AsyncSession,
    household_id: UUID | None = None,
    range_: str = "2y",
) -> int:
    """야후 종목 월봉 종가를 market_price_history 에 채운다(미래 수집 + 과거 backfill 공용).

    종목당 1회 호출로 range_ 기간 월별 종가를 upsert. USD 시장은 '현재' 환율로 KRW 환산
    (과거 환율 이력이 없어 근사 — 원가박제보다는 정확). 환율 없으면 USD 시장 제외.
    household_id 없으면 전 가계부(시세는 시장 공통). 반환: upsert 시도한 (종목×월) row 수.
    """
    repo = PortfolioItemRepository(session)
    markets = list(_YAHOO_MARKETS)

    fx_rate = await _resolve_usd_krw(session)
    if fx_rate is None:
        markets = [m for m in markets if m not in _USD_MARKETS]
    if not markets:
        return 0

    if household_id is None:
        pairs = await repo.find_active_distinct_code_market_by_markets(markets)
    else:
        pairs = await repo.find_active_distinct_code_market_by_household_and_markets(
            household_id, markets,
        )
    if not pairs:
        return 0

    rows: list[dict] = []
    for code, market in pairs:
        symbol = build_yahoo_symbol(market, code)
        closes = await fetch_monthly_closes(symbol, range_)
        if not closes:
            continue
        use_fx = market in _USD_MARKETS and fx_rate is not None
        for month, close in closes.items():
            price = close * fx_rate if use_fx else close
            rows.append({
                "code": code,
                "market": market.value,
                "price_date": month,
                "price": price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            })

    upserted = await MarketPriceHistoryRepository(session).upsert_prices(rows)
    logger.info(
        "월봉 시세 backfill (household=%s, pairs=%d, rows=%d)",
        household_id, len(pairs), len(rows),
    )
    return upserted


def other_price_code(item_id: UUID) -> str:
    """OTHER 종목 시세 이력의 code 값 — 종목 id. 저장(snapshot)·조회(value) 가 같은 규칙을 쓰게 한 곳에."""
    return str(item_id)


async def snapshot_other_prices(
    session: AsyncSession, month: date, household_id: UUID | None = None,
) -> int:
    """OTHER(금 등) 종목의 현재 current_price 를 그 달 시세로 박는다.

    야후 미지원이라 과거 소급 불가 — 박제 시점 수동가를 '그 달' 값으로 저장('현재부터').
    반환: upsert 시도 row 수.
    """
    others = await PortfolioItemRepository(session).find_active_other_prices(household_id)
    if not others:
        return 0
    rows = [
        {
            # 종목 단위 키 — code("" 허용)로 묶으면 한 INSERT 안 키 중복으로 잡 전체가 롤백된다
            "code": other_price_code(o["item_id"]),
            "market": Market.OTHER.value,
            "price_date": month,
            "price": o["current_price"],
        }
        for o in others
    ]
    return await MarketPriceHistoryRepository(session).upsert_prices(rows)


async def value_holdings_at_month(
    session: AsyncSession, holdings: list[dict], month: date,
) -> dict:
    """asof holdings 를 그 달 시가(market_price_history)로 평가 — 스냅샷 박제 공용.

    종목 식별을 tx 기반(holdings의 code/market)이 아니라 item 현재 (code,market)로 한다:
    매수 후 종목 market 을 바꾼 경우(예: KRX→OTHER) tx 와 시세 이력이 어긋나므로,
    시세를 '저장한 기준'(item 현재값)과 맞춰야 정합한다. 시가 없으면 원가 fallback.
    반환: {item_id: {"current_price", "valuation"}}.
    """
    if not holdings:
        return {}

    item_ids = [h["item_id"] for h in holdings]
    items = await PortfolioItemRepository(session).find_by_ids_including_deleted(item_ids)
    item_map = {i.id: i for i in items}

    def _key(h: dict) -> tuple[str, str]:
        it = item_map.get(h["item_id"])
        code, market = (it.code, it.market) if it else (h["code"], h["market"])
        # OTHER 는 종목 단위 키만 — 옛 (code, OTHER) 행은 가계부끼리 공유돼 남의 수동가일 수 있다.
        # 과거 달 기록값은 backfill 리비전(c2e4a6b8d0f1)이 이 키로 옮겨 두었다.
        if market == Market.OTHER.value:
            return (other_price_code(h["item_id"]), market)
        return (code, market)

    keys = {h["item_id"]: _key(h) for h in holdings}
    prices = await MarketPriceHistoryRepository(session).find_prices_for_month(
        list(set(keys.values())), month,
    )

    valued: dict = {}
    for h in holdings:
        px = prices.get(keys[h["item_id"]])
        if px is not None:
            valued[h["item_id"]] = {
                "current_price": px, "valuation": h["quantity"] * px,
            }
        else:
            valued[h["item_id"]] = {
                "current_price": h["avg_cost"], "valuation": h["cost"],
            }
    return valued
