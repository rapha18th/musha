"""The owner abroad, the manager at home, the shop, and every dollar between them."""
from datetime import datetime, timezone

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


class Person(Base):
    __tablename__ = "people"
    id: Mapped[int] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(String(16))  # owner | manager
    name: Mapped[str] = mapped_column(String(80))
    email: Mapped[str] = mapped_column(String(160), default="")  # PayPal sandbox email
    phone: Mapped[str] = mapped_column(String(32), default="")
    language: Mapped[str] = mapped_column(String(8), default="en")  # en | sn | nd | zu
    city: Mapped[str] = mapped_column(String(80), default="")


class Shop(Base):
    __tablename__ = "shops"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    place: Mapped[str] = mapped_column(String(120))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    owner_id: Mapped[int] = mapped_column(ForeignKey("people.id"))
    manager_id: Mapped[int] = mapped_column(ForeignKey("people.id"))
    owner: Mapped[Person] = relationship(foreign_keys=[owner_id])
    manager: Mapped[Person] = relationship(foreign_keys=[manager_id])


class StockRequest(Base):
    """A manager's ask for stock money. Its status walks the money loop:
    requested -> approving -> held -> captured | part_captured | voided."""
    __tablename__ = "stock_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), index=True)
    item: Mapped[str] = mapped_column(String(80))
    quantity: Mapped[int]
    unit: Mapped[str] = mapped_column(String(24), default="units")
    amount_cents: Mapped[int]
    supplier: Mapped[str] = mapped_column(String(120), default="")
    words: Mapped[str] = mapped_column(Text, default="")  # the manager's own words
    status: Mapped[str] = mapped_column(String(20), default="requested")
    order_id: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(default=now)
    shop: Mapped[Shop] = relationship()
    hold: Mapped["Hold | None"] = relationship(back_populates="request", uselist=False)


class Hold(Base):
    """A PayPal authorization: the owner's money, held until proof arrives."""
    __tablename__ = "holds"
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("stock_requests.id"), unique=True)
    authorization_id: Mapped[str] = mapped_column(String(40), index=True)
    first_authorization_id: Mapped[str] = mapped_column(String(40))
    amount_cents: Mapped[int]
    captured_cents: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(20), default="held")  # held | captured | part_captured | voided
    authorized_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    reauthorized_at: Mapped[datetime | None] = mapped_column(default=None)
    request: Mapped[StockRequest] = relationship(back_populates="hold")


class LedgerEntry(Base):
    """One line in the shop's books. `ref` makes every posting idempotent, so the
    API path and the webhook path can both post and the books still agree."""
    __tablename__ = "ledger"
    __table_args__ = (UniqueConstraint("ref"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), index=True)
    at: Mapped[datetime] = mapped_column(default=now)
    kind: Mapped[str] = mapped_column(String(24))
    amount_cents: Mapped[int] = mapped_column(default=0)
    item: Mapped[str] = mapped_column(String(80), default="")
    quantity: Mapped[int] = mapped_column(default=0)
    memo: Mapped[str] = mapped_column(String(240), default="")
    ref: Mapped[str] = mapped_column(String(96))
    source: Mapped[str] = mapped_column(String(16), default="api")  # api | webhook | seed


class PricePoint(Base):
    """A past purchase, for price checks: what this shop paid this supplier per unit."""
    __tablename__ = "price_points"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), index=True)
    item: Mapped[str] = mapped_column(String(80))
    supplier: Mapped[str] = mapped_column(String(120))
    unit_cents: Mapped[int]
    bought_at: Mapped[datetime]


class Message(Base):
    """The manager's WhatsApp thread, simulated in the browser."""
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), index=True)
    sender: Mapped[str] = mapped_column(String(16))  # manager | musha | owner
    text: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(8), default="en")
    request_id: Mapped[int | None] = mapped_column(ForeignKey("stock_requests.id"), default=None)
    at: Mapped[datetime] = mapped_column(default=now)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # PayPal event id
    type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(default=now)


class Payout(Base):
    __tablename__ = "payouts"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), index=True)
    batch_id: Mapped[str] = mapped_column(String(64), unique=True)
    paypal_batch_id: Mapped[str] = mapped_column(String(64), default="")
    receiver: Mapped[str] = mapped_column(String(160))
    amount_cents: Mapped[int]
    purpose: Mapped[str] = mapped_column(String(24), default="wages")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(default=now)
