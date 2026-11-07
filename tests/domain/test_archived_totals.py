"""W3. 보관 통장은 총자산·배분·투자 요약에서 뺀다 — 월 박제와 같은 모집단.

**공개계약**
- 월 박제는 보관 통장 제외 — `account_snapshot/service.py` `_build_and_save_snapshot`(`not a.is_archived`),
  `portfolio/snapshot_service.py`(INVESTMENT 중 비보관만)
- 홈 hero 증감 = 현재 total − 마지막 박제 total (프론트 `total-asset-hero.tsx`)
→ 현재 total 이 보관 통장을 더하면 증감이 그 잔액만큼 튄다. 정책(2026-11-07): 보관 = 집계 밖.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.domain.account.enum import AccountType
from app.domain.home import service as home_service
from app.domain.portfolio import service as portfolio_service
from app.domain.portfolio.enum import AssetClass, Market
from app.domain.portfolio.schema import PortfolioBuyRequest, PortfolioCreateRequest
from app.domain.wealth import service as wealth_service
from tests.fixtures.factory import (
    account_factory,
    household_factory,
    member_factory,
    user_factory,
)


async def _household(db):
    user = await user_factory(db)
    household = await household_factory(db, owner=user)
    await member_factory(db, household=household, user=user)
    return household


async def _archive(db, account) -> None:
    account.is_archived = True
    await db.flush()


async def _archived_investment_with_stock(db, household):
    """보관된 증권 계좌 — 시작 1,000,000 으로 10주×50,000 매수(현금 500,000 + 평가 500,000)."""
    inv = await account_factory(
        db, household=household, name="옛 증권", account_type=AccountType.INVESTMENT,
        start_balance=Decimal("1000000"),
    )
    item = await portfolio_service.create_portfolio(
        db, household,
        PortfolioCreateRequest(
            name="삼성전자", code="005930", market=Market.KRX_KOSPI,
            currentPrice=Decimal("50000"), accountId=inv.id,
        ),
    )
    await portfolio_service.buy(
        db, household, item.id,
        PortfolioBuyRequest(quantity=Decimal("10"), price=Decimal("50000"), txDate=date(2026, 9, 1)),
    )
    await _archive(db, inv)
    return inv


async def test_W3_1_보관_통장_잔액은_홈_총자산에_안_들어간다(db):
    household = await _household(db)
    await account_factory(db, household=household, start_balance=Decimal("1000000"))
    old = await account_factory(db, household=household, name="옛 통장", start_balance=Decimal("2000000"))
    await _archive(db, old)

    res = await home_service.get_home_overview(db, household, 2026, 11)

    assert res.total_balance == Decimal("1000000")


async def test_W3_2_보관_투자통장은_자산_총액과_배분에서_빠진다(db):
    household = await _household(db)
    await account_factory(db, household=household, start_balance=Decimal("1000000"))
    await _archived_investment_with_stock(db, household)

    res = await wealth_service.get_wealth_overview(db, household, None, None)

    assert res.total_balance == Decimal("1000000")
    slices = {s.asset_class: s.valuation for s in res.allocation.current_allocation}
    assert slices == {AssetClass.CASH: Decimal("1000000")}


async def test_W3_3_보관_투자통장은_투자_요약_합계에서_빠진다(db):
    household = await _household(db)
    await _archived_investment_with_stock(db, household)

    res = await portfolio_service.get_portfolio_overview(db, household)

    assert res.summary.total_valuation == Decimal("0")
    assert res.summary.total_balance == Decimal("0")


async def test_W3_4_보관_통장이_없으면_합계는_그대로(db):
    household = await _household(db)
    await account_factory(db, household=household, start_balance=Decimal("1000000"))
    await account_factory(db, household=household, name="비상금", start_balance=Decimal("300000"))

    res = await home_service.get_home_overview(db, household, 2026, 11)

    assert res.total_balance == Decimal("1300000")


async def test_W3_5_보관_통장은_합계에서만_빠지고_목록에는_남는다(db):
    """목록까지 거르면 API 로 보관된 통장·수동자산을 앱에서 다시 열 길이 없어진다."""
    household = await _household(db)
    await account_factory(db, household=household, start_balance=Decimal("1000000"))
    old = await account_factory(db, household=household, name="옛 통장", start_balance=Decimal("2000000"))
    await _archive(db, old)
    inv = await _archived_investment_with_stock(db, household)

    home = await home_service.get_home_overview(db, household, 2026, 11)
    wealth = await wealth_service.get_wealth_overview(db, household, None, None)
    portfolio = await portfolio_service.get_portfolio_overview(db, household)

    assert old.id in {a.id for a in home.accounts}
    assert {old.id, inv.id} <= {a.id for a in wealth.accounts}
    assert inv.id in {g.account.id for g in portfolio.investment_accounts}


async def test_W3_6_활성_투자통장은_현금과_평가액이_배분·요약에_그대로_들어간다(db):
    """정상 경로 — 배분 INVESTMENT 슬라이스 출처(portfolio_valuation)와 요약 필터를 고정."""
    household = await _household(db)
    active = await account_factory(
        db, household=household, name="증권", account_type=AccountType.INVESTMENT,
        start_balance=Decimal("1000000"),
    )
    item = await portfolio_service.create_portfolio(
        db, household,
        PortfolioCreateRequest(
            name="삼성전자", code="005930", market=Market.KRX_KOSPI,
            currentPrice=Decimal("50000"), accountId=active.id,
        ),
    )
    await portfolio_service.buy(
        db, household, item.id,
        PortfolioBuyRequest(quantity=Decimal("10"), price=Decimal("50000"), txDate=date(2026, 9, 1)),
    )
    await _archived_investment_with_stock(db, household)

    wealth = await wealth_service.get_wealth_overview(db, household, None, None)
    portfolio = await portfolio_service.get_portfolio_overview(db, household)

    slices = {s.asset_class: s.valuation for s in wealth.allocation.current_allocation}
    assert slices == {AssetClass.CASH: Decimal("500000"), AssetClass.INVESTMENT: Decimal("500000")}
    assert portfolio.summary.total_valuation == Decimal("500000")
    assert portfolio.summary.total_balance == Decimal("1000000")
