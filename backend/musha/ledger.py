"""The shop's books. Every posting carries a unique ref, so posting twice is harmless."""
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import LedgerEntry

# Money the owner sent, by stage. Stock, sales and wages sit beside them.
FUNDS_HELD = "funds_held"
FUNDS_CAPTURED = "funds_captured"
FUNDS_RELEASED = "funds_released"
STOCK_IN = "stock_in"
WAGES = "wages"
SALE = "sale"
EXPENSE = "expense"
ASSET = "asset"
BANKED = "banked"

# What a released request becomes in the books, by request kind
SPEND_KIND = {"stock": STOCK_IN, "expense": EXPENSE, "asset": ASSET}


def post(session: Session, *, shop_id: int, kind: str, ref: str, amount_cents: int = 0,
         item: str = "", quantity: int = 0, memo: str = "", source: str = "api",
         at=None, request_id: int | None = None) -> tuple[LedgerEntry, bool]:
    """Post once per ref. Returns the entry and whether this call created it."""
    existing = session.scalar(select(LedgerEntry).where(LedgerEntry.ref == ref))
    if existing:
        return existing, False
    entry = LedgerEntry(shop_id=shop_id, kind=kind, ref=ref, amount_cents=amount_cents,
                        item=item, quantity=quantity, memo=memo, source=source,
                        request_id=request_id)
    if at is not None:
        entry.at = at
    session.add(entry)
    session.flush()
    return entry, True


def entries(session: Session, shop_id: int) -> list[LedgerEntry]:
    return list(session.scalars(
        select(LedgerEntry).where(LedgerEntry.shop_id == shop_id).order_by(LedgerEntry.at, LedgerEntry.id)
    ))


def summary(session: Session, shop_id: int) -> dict:
    totals: dict[str, int] = defaultdict(int)
    stock: dict[str, int] = defaultdict(int)
    for e in entries(session, shop_id):
        totals[e.kind] += e.amount_cents
        if e.kind == STOCK_IN:
            stock[e.item] += e.quantity
        elif e.kind == SALE and e.item:
            stock[e.item] -= e.quantity
    return {
        "held_cents": totals[FUNDS_HELD] - totals[FUNDS_CAPTURED] - totals[FUNDS_RELEASED],
        "funded_cents": totals[FUNDS_CAPTURED],
        "returned_cents": totals[FUNDS_RELEASED],
        "wages_cents": totals[WAGES],
        "sales_cents": totals[SALE],
        "expenses_cents": totals[EXPENSE],
        "assets_cents": totals[ASSET],
        "banked_cents": totals[BANKED],
        "stock": dict(stock),
    }
