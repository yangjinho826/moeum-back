"""D6. 월간 집계 · 달력의 통장 필터 정합.

거래 탭은 좌(목록)·우(요약·달력·지출Top5)가 **같은 통장 필터**를 봐야 숫자가 맞는다.
필터를 안 주면 가구 전체 — 홈 화면 등 기존 호출부가 그대로 동작해야 한다.

| # | 불변식 |
|---|---|
| INV-A | `account_id` 를 주면 그 통장이 낀 거래만 집계된다 |
| INV-B | 통장별 집계의 합 = 가구 전체 집계 (단독거래만 있을 때) |
| INV-C | 이체는 출금·입금 **양쪽 통장 조회 모두**에 잡힌다 (`or_` 조건) |
| INV-D | 카테고리별 집계도 같은 통장 필터를 탄다 |
| INV-E | 달력 일별 합계와 월간 요약이 같은 필터에서 서로 일치한다 |
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest_asyncio

from app.domain.stats import service as stats_service
from app.domain.transaction import service
from app.domain.transaction.enum import TxType
from app.domain.transaction.schema import (
    TransactionCreateRequest,
    TransactionUpdateRequest,
)
from tests.fixtures.factory import LedgerContext, seed_ledger_context

YEAR = 2026
MONTH = 3
DAY = date(YEAR, MONTH, 11)


@pytest_asyncio.fixture
async def ctx(db) -> LedgerContext:
    return await seed_ledger_context(db)


async def _create(db, ctx, *, tx_type, amount, account_id, **kwargs):
    payload: dict = {
        "txType": tx_type,
        "amount": Decimal(amount),
        "txDate": DAY,
        "accountId": account_id,
        **kwargs,
    }
    return await service.create_transaction(
        db, ctx.household, TransactionCreateRequest(**payload), ctx.user,
    )


async def _stats(db, ctx, account_id=None):
    return await stats_service.get_monthly_stats(
        db, ctx.household, YEAR, MONTH, account_id,
    )


async def test_D6_1_통장을_주면_그_통장_거래만_집계된다(db, ctx):
    """INV-A + INV-B — 통장별 합이 전체와 맞아야 필터가 '나누기'로 동작한 것."""
    # given: A 통장에 지출 10,000 / B 통장에 지출 3,000
    await _create(
        db, ctx, tx_type=TxType.EXPENSE, amount="10000",
        account_id=ctx.account.id, categoryId=ctx.expense_category.id,
    )
    await _create(
        db, ctx, tx_type=TxType.EXPENSE, amount="3000",
        account_id=ctx.other_account.id, categoryId=ctx.expense_category.id,
    )

    # when
    total = await _stats(db, ctx)
    only_a = await _stats(db, ctx, ctx.account.id)
    only_b = await _stats(db, ctx, ctx.other_account.id)

    # then
    assert total.monthly_expense == Decimal("13000")
    assert only_a.monthly_expense == Decimal("10000")
    assert only_b.monthly_expense == Decimal("3000")
    assert only_a.monthly_expense + only_b.monthly_expense == total.monthly_expense


async def test_D6_2_이체는_출금_입금_양쪽_통장에서_모두_보인다(db, ctx):
    """INV-C — `or_(account_id, to_account_id)` 조건이 빠지면 입금 통장에서 사라진다.

    좌측 목록(ledger)은 이미 양쪽에서 보여주므로, 요약이 한쪽에서만 잡히면
    같은 통장인데 목록엔 있고 요약엔 없는 어긋남이 생긴다.
    """
    # given: A → B 이체 50,000 한 건
    await _create(
        db, ctx, tx_type=TxType.TRANSFER, amount="50000",
        account_id=ctx.account.id, toAccountId=ctx.other_account.id,
    )

    # when
    from_side = await _stats(db, ctx, ctx.account.id)
    to_side = await _stats(db, ctx, ctx.other_account.id)

    # then: 양쪽 다 이체 합계에 잡힌다
    assert from_side.monthly_transfer == Decimal("50000")
    assert to_side.monthly_transfer == Decimal("50000")
    # 이체는 수입·지출이 아니다
    assert from_side.monthly_expense == Decimal("0")
    assert to_side.monthly_income == Decimal("0")


async def test_D6_3_카테고리별_집계도_통장_필터를_탄다(db, ctx):
    """INV-D — 지출 Top5 가 목록과 다른 모수를 쓰면 안 된다."""
    # given: 같은 카테고리로 A 7,000 / B 2,000
    await _create(
        db, ctx, tx_type=TxType.EXPENSE, amount="7000",
        account_id=ctx.account.id, categoryId=ctx.expense_category.id,
    )
    await _create(
        db, ctx, tx_type=TxType.EXPENSE, amount="2000",
        account_id=ctx.other_account.id, categoryId=ctx.expense_category.id,
    )

    # when
    only_a = await _stats(db, ctx, ctx.account.id)

    # then
    assert len(only_a.by_category) == 1
    assert only_a.by_category[0].category_id == ctx.expense_category.id
    assert only_a.by_category[0].amount == Decimal("7000")


async def test_D6_4_통장_없이_부르면_가구_전체_그대로다(db, ctx):
    """기존 호출부(홈 화면 등) 회귀 방어 — 기본값이 필터를 걸면 안 된다."""
    # given
    await _create(
        db, ctx, tx_type=TxType.INCOME, amount="1000",
        account_id=ctx.account.id, categoryId=ctx.income_category.id,
    )
    await _create(
        db, ctx, tx_type=TxType.INCOME, amount="2000",
        account_id=ctx.other_account.id, categoryId=ctx.income_category.id,
    )

    # when
    stats = await _stats(db, ctx)

    # then
    assert stats.monthly_income == Decimal("3000")


async def test_D6_5_달력과_요약이_같은_통장에서_일치한다(db, ctx):
    """INV-E — 좌우 불일치의 본질. calendar_full 한 응답 안에서 먼저 닫혀야 한다."""
    # given: A 에 수입 5,000 · 지출 1,500, B 에 지출 9,999(섞이면 안 되는 잡음)
    await _create(
        db, ctx, tx_type=TxType.INCOME, amount="5000",
        account_id=ctx.account.id, categoryId=ctx.income_category.id,
    )
    await _create(
        db, ctx, tx_type=TxType.EXPENSE, amount="1500",
        account_id=ctx.account.id, categoryId=ctx.expense_category.id,
    )
    await _create(
        db, ctx, tx_type=TxType.EXPENSE, amount="9999",
        account_id=ctx.other_account.id, categoryId=ctx.expense_category.id,
    )

    # when
    full = await service.get_calendar_full(
        db, ctx.household, YEAR, MONTH, ctx.account.id,
    )
    summary = await _stats(db, ctx, ctx.account.id)

    # then: 달력 월합계 == 요약 == 목록 거래들의 합
    assert full.monthly_income == summary.monthly_income == Decimal("5000")
    assert full.monthly_expense == summary.monthly_expense == Decimal("1500")

    # 목록(transactions)도 같은 필터 — B 통장 9,999 는 없어야 한다
    assert len(full.transactions) == 2
    assert all(t.account_id == ctx.account.id for t in full.transactions)

    # 일별 합계도 같은 모수
    assert len(full.days) == 1
    assert full.days[0].income == Decimal("5000")
    assert full.days[0].expense == Decimal("1500")


async def test_D6_6_이체로_바뀐_거래의_카테고리는_양쪽에_이중계상되지_않는다(db, ctx):
    """INV-B 방어 — 통장별 합이 전체를 넘으면 안 된다.

    생성은 TRANSFER+카테고리를 막지만 수정 경로엔 막이 없었다. 그렇게 남은
    category_id 는 `_account_involved`(출금 OR 입금) 때문에 두 통장 모두에서
    같은 카테고리에 더해져 "통장별 합 > 전체" 가 된다.
    """
    # given: 지출로 만든 뒤 이체로 전환 — 카테고리가 딸려 오는 경로
    created = await _create(
        db, ctx, tx_type=TxType.EXPENSE, amount="10000",
        account_id=ctx.account.id, categoryId=ctx.expense_category.id,
    )
    await service.update_transaction(
        db, ctx.household, created.id,
        TransactionUpdateRequest(
            txType=TxType.TRANSFER,
            amount=Decimal("10000"),
            txDate=DAY,
            accountId=ctx.account.id,
            toAccountId=ctx.other_account.id,
        ),
    )

    # when
    total = await _stats(db, ctx)
    only_a = await _stats(db, ctx, ctx.account.id)
    only_b = await _stats(db, ctx, ctx.other_account.id)

    def cat_sum(stats) -> Decimal:
        return sum((c.amount for c in stats.by_category), Decimal("0"))

    # then: 이체는 카테고리 집계에서 빠진다 → 어디서도 안 잡힌다
    assert cat_sum(total) == Decimal("0")
    assert cat_sum(only_a) + cat_sum(only_b) == cat_sum(total)
