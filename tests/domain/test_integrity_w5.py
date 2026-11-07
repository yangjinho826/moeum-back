"""W5. 거래·계좌 무결성 — 상태가 어긋나 숫자가 틀어지는 경로를 막는다.

**공개계약**
- 통장 삭제 = 단독 거래 cascade soft-delete — `account/service.py` delete_account docstring
- 거래 수정 — 프론트는 상세값 전체를 PUT(`moeum-front/.../transaction/hooks/use-sub/use-form.tsx`)
- 평가조정(VALUATION): 방향 필수 + category 금지 — `transaction/schema.py` TransactionCreateRequest
- 지출계열엔 EXPENSE 카테고리, 수입엔 INCOME 카테고리 — `_category_kind_matches` docstring
  ("kind 불일치 시 월합계(타입 기준)와 카테고리차트(kind 기준)가 어긋난다")
- 전량 매도된 종목도 매도 이력·손익은 남는다 — `get_realized_pnl_by_account` docstring
- INVESTMENT 잔액 = 현금 + 종목 평가, 그 외 = 현금흐름 — `_calc_balance`
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio

from app.core.exceptions import CustomException, ErrorCode
from app.domain.account import service as account_service
from app.domain.account.enum import AccountType
from app.domain.account.schema import AccountUpdateRequest
from app.domain.category import service as category_service
from app.domain.category.schema import CategoryUpdateRequest
from app.domain.fixed import service as fixed_service
from app.domain.portfolio import service as portfolio_service
from app.domain.portfolio.enum import Market
from app.domain.portfolio.schema import (
    PortfolioBuyRequest,
    PortfolioCreateRequest,
    PortfolioSellRequest,
)
from app.domain.transaction import service as tx_service
from app.domain.transaction.enum import TxType
from app.domain.transaction.model import Transaction
from app.domain.transaction.repository import TransactionFilter
from app.domain.transaction.schema import TransactionCreateRequest, TransactionUpdateRequest
from tests.fixtures.factory import (
    LedgerContext,
    account_factory,
    fixed_expense_factory,
    seed_ledger_context,
    seed_portfolio_context,
)


@pytest_asyncio.fixture
async def ctx(db) -> LedgerContext:
    return await seed_ledger_context(db)


async def _create(db, ctx: LedgerContext, **body):
    req = TransactionCreateRequest.model_validate(
        {"amount": "10000", "txDate": "2026-11-05", **body}
    )
    return await tx_service.create_transaction(db, ctx.household, req, ctx.user)


def _full_body(tx, **changes) -> dict:
    """프론트 수정 폼처럼 상세값 전체 + 바꾼 칸 — 지운 칸은 None 으로 간다."""
    body = {
        "txType": tx.tx_type,
        "amount": str(tx.amount),
        "txDate": tx.tx_date.isoformat(),
        "accountId": str(tx.account_id),
        "toAccountId": str(tx.to_account_id) if tx.to_account_id else None,
        "categoryId": str(tx.category_id) if tx.category_id else None,
        "paidByUserId": str(tx.paid_by_user_id) if tx.paid_by_user_id else None,
        "fixedExpenseId": str(tx.fixed_expense_id) if tx.fixed_expense_id else None,
        "memo": tx.memo,
        "valuationDirection": tx.valuation_direction,
    }
    return {**body, **changes}


async def _update(db, ctx: LedgerContext, tx, **changes):
    req = TransactionUpdateRequest.model_validate(_full_body(tx, **changes))
    return await tx_service.update_transaction(db, ctx.household, tx.id, req)


# ── B4 ──
async def test_W5_1_수동자산_통장을_지우면_평가조정_거래도_목록에서_사라진다(db, ctx):
    await _create(
        db, ctx, txType="VALUATION", accountId=str(ctx.manual_asset.id),
        amount="50000000", valuationDirection="INCREASE",
    )

    await account_service.delete_account(db, ctx.household, ctx.manual_asset.id)

    page = await tx_service.list_transactions(db, ctx.household, TransactionFilter(), cursor=None, limit=50)
    assert [t for t in page.items if t.tx_type == TxType.VALUATION] == []


# ── B9 ──
async def test_W5_2_삭제된_고정지출이_걸린_거래도_메모는_고칠_수_있다(db, ctx):
    fixed = await fixed_expense_factory(db, household=ctx.household)
    tx = await _create(
        db, ctx, txType="FIXED_EXPENSE", accountId=str(ctx.account.id),
        categoryId=str(ctx.expense_category.id), fixedExpenseId=str(fixed.id),
    )
    await fixed_service.delete_fixed_expense(db, ctx.household, fixed.id)

    updated = await _update(db, ctx, tx, memo="11월 월세")

    assert updated.memo == "11월 월세"
    assert updated.fixed_expense_id == fixed.id


async def test_W5_3_수정에서_카테고리를_지우면_해제된다(db, ctx):
    tx = await _create(
        db, ctx, txType="EXPENSE", accountId=str(ctx.account.id),
        categoryId=str(ctx.expense_category.id),
    )

    updated = await _update(db, ctx, tx, categoryId=None)

    assert updated.category_id is None


# ── B11 ──
async def test_W5_4_평가조정으로_바꾸면서_방향이_없으면_거부한다(db, ctx):
    tx = await _create(
        db, ctx, txType="EXPENSE", accountId=str(ctx.account.id),
        categoryId=str(ctx.expense_category.id),
    )

    with pytest.raises(CustomException) as exc:
        await _update(
            db, ctx, tx, txType="VALUATION", accountId=str(ctx.manual_asset.id),
            valuationDirection=None,
        )
    assert exc.value.error_code == ErrorCode.BAD_REQUEST


async def test_W5_5_평가조정으로_바꾸면_남은_카테고리는_정리된다(db, ctx):
    tx = await _create(
        db, ctx, txType="EXPENSE", accountId=str(ctx.account.id),
        categoryId=str(ctx.expense_category.id),
    )

    updated = await _update(
        db, ctx, tx, txType="VALUATION", accountId=str(ctx.manual_asset.id),
        valuationDirection="INCREASE",
    )

    assert updated.tx_type == TxType.VALUATION
    assert updated.category_id is None


# ── B8 ──
async def test_W5_6_전량_매도한_종목도_매매손익과_거래내역을_볼_수_있다(db):
    pctx = await seed_portfolio_context(db)
    item = await portfolio_service.create_portfolio(
        db, pctx.household,
        PortfolioCreateRequest(
            name="삼성전자", code="005930", market=Market.KRX_KOSPI,
            currentPrice=Decimal("60000"), accountId=pctx.account.id,
        ),
    )
    await portfolio_service.buy(
        db, pctx.household, item.id,
        PortfolioBuyRequest(quantity=Decimal("10"), price=Decimal("50000"), txDate=date(2026, 9, 1)),
    )
    await portfolio_service.sell(
        db, pctx.household, item.id,
        PortfolioSellRequest(quantity=Decimal("10"), sellPrice=Decimal("60000"), txDate=date(2026, 10, 1)),
    )

    pnl = await portfolio_service.get_realized_pnl_by_item(db, pctx.household, item.id)
    txs = await portfolio_service.list_item_transactions_cursor(db, pctx.household, item.id, None, 20)

    assert len(pnl.rows) == 1
    assert pnl.summary.total_realized == Decimal("100000.00")
    assert len(txs.items) == 2


# ── B10 ──
async def test_W5_7_거래가_붙은_카테고리는_수입_지출_구분을_못_바꾼다(db, ctx):
    await _create(
        db, ctx, txType="EXPENSE", accountId=str(ctx.account.id),
        categoryId=str(ctx.expense_category.id),
    )

    with pytest.raises(CustomException) as exc:
        await category_service.update_category(
            db, ctx.household, ctx.expense_category.id, CategoryUpdateRequest(kind="INCOME"),
        )
    assert exc.value.error_code == ErrorCode.CATEGORY_IN_USE


# ── B2 ──
async def test_W5_8_종목이_있는_투자통장은_일반통장으로_못_바꾼다(db):
    pctx = await seed_portfolio_context(db)
    await portfolio_service.create_portfolio(
        db, pctx.household,
        PortfolioCreateRequest(
            name="삼성전자", code="005930", market=Market.KRX_KOSPI,
            currentPrice=Decimal("60000"), accountId=pctx.account.id,
        ),
    )

    with pytest.raises(CustomException) as exc:
        await account_service.update_account(
            db, pctx.household, pctx.account.id, AccountUpdateRequest(accountType=AccountType.LIVING),
        )
    assert exc.value.error_code == ErrorCode.ACCOUNT_TYPE_LOCKED


async def test_W5_8b_지출이_있는_일반통장은_수동자산으로_못_바꾼다(db, ctx):
    await _create(db, ctx, txType="EXPENSE", accountId=str(ctx.account.id))

    with pytest.raises(CustomException) as exc:
        await account_service.update_account(
            db, ctx.household, ctx.account.id, AccountUpdateRequest(accountType=AccountType.REAL_ESTATE),
        )
    assert exc.value.error_code == ErrorCode.ACCOUNT_TYPE_LOCKED


async def test_W5_9_거래가_없거나_같은_묶음_안이면_타입을_바꿀_수_있다(db, ctx):
    empty = await account_factory(db, household=ctx.household, name="새 통장")
    await _create(
        db, ctx, txType="VALUATION", accountId=str(ctx.manual_asset.id),
        amount="1000000", valuationDirection="INCREASE",
    )

    a = await account_service.update_account(
        db, ctx.household, empty.id, AccountUpdateRequest(accountType=AccountType.INVESTMENT),
    )
    b = await account_service.update_account(
        db, ctx.household, ctx.manual_asset.id, AccountUpdateRequest(accountType=AccountType.PENSION),
    )

    assert a.account_type == AccountType.INVESTMENT
    assert b.account_type == AccountType.PENSION


# ── 리뷰 반영 ──
async def test_W5_10_고정지출_거래를_지출로_바꾸면_고정지출_연결이_풀린다(db, ctx):
    fixed = await fixed_expense_factory(db, household=ctx.household)
    tx = await _create(
        db, ctx, txType="FIXED_EXPENSE", accountId=str(ctx.account.id),
        categoryId=str(ctx.expense_category.id), fixedExpenseId=str(fixed.id),
    )

    updated = await _update(db, ctx, tx, txType="EXPENSE")

    assert updated.fixed_expense_id is None


async def test_W5_11_상대_통장이_삭제된_이체도_출발_통장을_바꿀_수_있다(db, ctx):
    third = await account_factory(db, household=ctx.household, name="새 통장")
    tx = await _create(
        db, ctx, txType="TRANSFER", accountId=str(ctx.account.id),
        toAccountId=str(ctx.other_account.id),
    )
    await account_service.delete_account(db, ctx.household, ctx.other_account.id)

    updated = await _update(db, ctx, tx, accountId=str(third.id))

    assert updated.account_id == third.id
    assert updated.to_account_id == ctx.other_account.id


async def test_W5_12_삭제된_카테고리가_걸린_지출도_고정지출로_바꿀_수_있다(db, ctx):
    fixed = await fixed_expense_factory(db, household=ctx.household)
    tx = await _create(
        db, ctx, txType="EXPENSE", accountId=str(ctx.account.id),
        categoryId=str(ctx.expense_category.id),
    )
    await category_service.delete_category(db, ctx.household, ctx.expense_category.id)

    updated = await _update(db, ctx, tx, txType="FIXED_EXPENSE", fixedExpenseId=str(fixed.id))

    assert updated.tx_type == TxType.FIXED_EXPENSE
    assert updated.category_id == ctx.expense_category.id


async def test_W5_13_고정지출이_쓰는_카테고리는_수입_지출_구분을_못_바꾼다(db, ctx):
    fixed = await fixed_expense_factory(db, household=ctx.household)
    fixed.category_id = ctx.expense_category.id
    await db.flush()

    with pytest.raises(CustomException) as exc:
        await category_service.update_category(
            db, ctx.household, ctx.expense_category.id, CategoryUpdateRequest(kind="INCOME"),
        )
    assert exc.value.error_code == ErrorCode.CATEGORY_IN_USE


async def test_W5_14_예전_규칙으로_저장된_고정지출_거래도_메모는_고칠_수_있다(db, ctx):
    tx = await _create(db, ctx, txType="EXPENSE", accountId=str(ctx.account.id))
    row = await db.get(Transaction, tx.id)
    row.tx_type = TxType.FIXED_EXPENSE  # 옛 수정 경로가 허용하던 '고정지출 없는 고정지출'
    await db.flush()

    updated = await _update(db, ctx, tx, txType="FIXED_EXPENSE", memo="정리")

    assert updated.memo == "정리"


async def test_W5_15_카테고리가_남은_옛_이체는_수입_지출_구분_변경을_막지_않는다(db, ctx):
    tx = await _create(
        db, ctx, txType="TRANSFER", accountId=str(ctx.account.id),
        toAccountId=str(ctx.other_account.id),
    )
    row = await db.get(Transaction, tx.id)
    row.category_id = ctx.expense_category.id  # 정규화 이전에 저장된 행
    await db.flush()

    res = await category_service.update_category(
        db, ctx.household, ctx.expense_category.id, CategoryUpdateRequest(kind="INCOME"),
    )

    assert res.kind == "INCOME"


async def test_W5_16_출발_통장이_삭제된_이체를_지출로_바꾸면_404(db, ctx):
    tx = await _create(
        db, ctx, txType="TRANSFER", accountId=str(ctx.account.id),
        toAccountId=str(ctx.other_account.id),
    )
    await account_service.delete_account(db, ctx.household, ctx.account.id)

    with pytest.raises(CustomException) as exc:
        await _update(db, ctx, tx, txType="EXPENSE")
    assert exc.value.error_code == ErrorCode.NOT_FOUND
