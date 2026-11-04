"""The owner abroad, the manager at home, the shop, and every dollar between them."""
import json
from datetime import date, datetime, timezone

from sqlalchemy import Date, ForeignKey, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


class JsonText:
    """Helpers for the few columns that hold JSON as text, so SQLite and Postgres agree."""

    @staticmethod
    def dump(value) -> str:
        return json.dumps(value, separators=(",", ":"), default=str)

    @staticmethod
    def load(text: str | None, default=None):
        if not text:
            return default
        try:
            return json.loads(text)
        except ValueError:
            return default


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
    created_at: Mapped[datetime] = mapped_column(default=now)
    # Chitoro lite: when the counter phone last checked in, and when the owner last asked to look in
    camera_seen_at: Mapped[datetime | None] = mapped_column(default=None)
    look_in_requested_at: Mapped[datetime | None] = mapped_column(default=None)
    # a shop the seed made, for a visitor or the server's first: staged camera scenes count here alone
    demo: Mapped[bool] = mapped_column(default=False)
    # A visitor's shop opens with its keys, one per role (routes/core.py mints and checks them).
    # Empty on a shop made without keys, such as the server's own first one: that shop is open.
    owner_key: Mapped[str] = mapped_column(String(64), default="")
    manager_key: Mapped[str] = mapped_column(String(64), default="")
    counter_key: Mapped[str] = mapped_column(String(64), default="")
    # The manager's agreement to the counter camera, given on the counter page; the first one stands
    camera_consent_at: Mapped[datetime | None] = mapped_column(default=None)
    camera_consent_by: Mapped[str] = mapped_column(String(80), default="")
    owner: Mapped[Person] = relationship(foreign_keys=[owner_id])
    manager: Mapped[Person] = relationship(foreign_keys=[manager_id])


class Product(Base):
    """What the shop sells: the unit it counts in, its shelf price, and who usually supplies it."""
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))  # "eggs"
    unit: Mapped[str] = mapped_column(String(24))  # "trays"
    sell_cents: Mapped[int]  # shelf price per unit
    usual_payee: Mapped[str] = mapped_column(String(120), default="")
    pack: Mapped[int] = mapped_column(default=1)  # reorder in multiples of this


class SpendRequest(Base):
    """Any outflow the manager asks the owner to fund. Its status walks the money loop:

    requested -> approving -> (reviewing) -> held -> (asking) -> (capturing) -> captured | part_captured | voided
    The owner's agent goes requested -> agent_pending -> held when PayPal answers late.
    `reviewing` waits on PayPal's review of the payment, `capturing` on a pending capture.
    A hold the proof engine keeps back becomes `flagged` until the owner decides.
    """
    __tablename__ = "spend_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), index=True)
    kind: Mapped[str] = mapped_column(String(12), default="stock")  # stock | expense | asset
    item: Mapped[str] = mapped_column(String(80))  # "eggs", "fridge door seal", "October rent"
    quantity: Mapped[int] = mapped_column(default=1)
    unit: Mapped[str] = mapped_column(String(24), default="units")
    amount_cents: Mapped[int]
    payee: Mapped[str] = mapped_column(String(120), default="")  # supplier, landlord, mechanic
    words: Mapped[str] = mapped_column(Text, default="")  # the manager's own words
    words_en: Mapped[str] = mapped_column(Text, default="")  # the same, in the owner's language
    language: Mapped[str] = mapped_column(String(8), default="en")
    origin: Mapped[str] = mapped_column(String(12), default="manager")  # manager | alert | owner
    status: Mapped[str] = mapped_column(String(20), default="requested")
    approved_by: Mapped[str] = mapped_column(String(12), default="")  # owner | agent
    order_id: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(default=now)
    approved_at: Mapped[datetime | None] = mapped_column(default=None)
    inferred: Mapped[str] = mapped_column(String(32), default="")  # fields Musha filled in: payee,amount
    shop: Mapped[Shop] = relationship()
    hold: Mapped["Hold | None"] = relationship(back_populates="request", uselist=False)
    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="request", order_by="Attachment.id")
    proofs: Mapped[list["Proof"]] = relationship(back_populates="request", order_by="Proof.id")


class Hold(Base):
    """A PayPal authorization: the owner's money, held until proof arrives."""
    __tablename__ = "holds"
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("spend_requests.id"), unique=True)
    authorization_id: Mapped[str] = mapped_column(String(40), index=True)
    first_authorization_id: Mapped[str] = mapped_column(String(40))
    amount_cents: Mapped[int]
    captured_cents: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(20), default="held")  # pending | held | captured | part_captured | voided
    authorized_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    reauthorized_at: Mapped[datetime | None] = mapped_column(default=None)
    capture_id: Mapped[str] = mapped_column(String(40), default="")  # a capture PayPal reports as pending
    capture_attempts: Mapped[int] = mapped_column(default=0)  # each attempt sends its own request id
    request: Mapped[SpendRequest] = relationship(back_populates="hold")


class Attachment(Base):
    """Evidence the manager sends: a receipt, a photo of the goods, a quote, a voice note.
    `reading` keeps what the model read from it, as JSON."""
    __tablename__ = "attachments"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), index=True)
    request_id: Mapped[int | None] = mapped_column(ForeignKey("spend_requests.id"), default=None, index=True)
    kind: Mapped[str] = mapped_column(String(12))  # receipt | goods | quote | voice | photo
    content_type: Mapped[str] = mapped_column(String(64))
    data: Mapped[bytes] = mapped_column(LargeBinary)
    size: Mapped[int] = mapped_column(default=0)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    phash: Mapped[str] = mapped_column(String(16), default="")  # 64-bit perceptual hash, hex
    reading: Mapped[str] = mapped_column(Text, default="")
    uploaded_at: Mapped[datetime] = mapped_column(default=now)
    request: Mapped[SpendRequest | None] = relationship(back_populates="attachments")


class Proof(Base):
    """One run of the proof engine over a request's evidence: the checks, the score, the decision."""
    __tablename__ = "proofs"
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("spend_requests.id"), index=True)
    receipt_id: Mapped[int | None] = mapped_column(ForeignKey("attachments.id"), default=None)
    round: Mapped[int] = mapped_column(default=1)  # 2 after the manager answers a question
    score: Mapped[int] = mapped_column(default=0)
    decision: Mapped[str] = mapped_column(String(12))  # release | ask | hold
    spent_cents: Mapped[int] = mapped_column(default=0)  # what the evidence says was spent
    checks: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of check results
    question: Mapped[str] = mapped_column(Text, default="")
    answer: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")  # one line for the owner
    elapsed_ms: Mapped[int] = mapped_column(default=0)  # evidence in to decision out
    created_at: Mapped[datetime] = mapped_column(default=now)
    request: Mapped[SpendRequest] = relationship(back_populates="proofs")


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
    source: Mapped[str] = mapped_column(String(16), default="api")  # api | webhook | seed | job | manager
    request_id: Mapped[int | None] = mapped_column(ForeignKey("spend_requests.id"), default=None)


class PricePoint(Base):
    """A past purchase, for price checks: what this shop paid this payee per unit."""
    __tablename__ = "price_points"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), index=True)
    item: Mapped[str] = mapped_column(String(80))
    supplier: Mapped[str] = mapped_column(String(120))
    unit_cents: Mapped[int]
    bought_at: Mapped[datetime]


class DayClose(Base):
    """The manager's end of day: cash counted, cash banked, cash spent, and a shelf count."""
    __tablename__ = "day_closes"
    __table_args__ = (UniqueConstraint("shop_id", "day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), index=True)
    day: Mapped[date] = mapped_column(Date)
    counted_cash_cents: Mapped[int]
    banked_cents: Mapped[int] = mapped_column(default=0)
    cash_expenses_cents: Mapped[int] = mapped_column(default=0)
    stock_counts: Mapped[str] = mapped_column(Text, default="{}")  # JSON {item: units}
    note: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(default=now)


class Alert(Base):
    """Something the owner hears first: low stock, a cash gap, a held proof, an agent asking."""
    __tablename__ = "alerts"
    __table_args__ = (UniqueConstraint("key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), index=True)
    key: Mapped[str] = mapped_column(String(120))  # dedupe, e.g. "low_stock:1:eggs:2026-10-27"
    kind: Mapped[str] = mapped_column(String(16))  # low_stock | cash_gap | stock_gap | proof | mandate
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[str] = mapped_column(Text, default="{}")  # JSON, e.g. a suggested request
    request_id: Mapped[int | None] = mapped_column(ForeignKey("spend_requests.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(default=now)
    dismissed: Mapped[bool] = mapped_column(default=False)


class Mandate(Base):
    """The owner's standing instruction to Musha's agent, and the saved PayPal it may use."""
    __tablename__ = "mandates"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), unique=True)
    active: Mapped[bool] = mapped_column(default=False)
    limit_cents: Mapped[int] = mapped_column(default=20000)  # per request
    weekly_cap_cents: Mapped[int] = mapped_column(default=50000)
    kinds: Mapped[str] = mapped_column(String(64), default="stock")  # comma separated
    known_payees_only: Mapped[bool] = mapped_column(default=True)
    price_tolerance_pct: Mapped[int] = mapped_column(default=10)
    vault_id: Mapped[str] = mapped_column(String(64), default="")  # PayPal payment token
    vault_customer_id: Mapped[str] = mapped_column(String(64), default="")
    vault_setup_token: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(default=now)


class CameraFrame(Base):
    """A still from the counter phone, kept 30 days. `reading` is the model's view of it."""
    __tablename__ = "camera_frames"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), index=True)
    at: Mapped[datetime] = mapped_column(default=now, index=True)
    kind: Mapped[str] = mapped_column(String(12), default="sample")  # sample | look_in | staged
    content_type: Mapped[str] = mapped_column(String(32), default="image/jpeg")
    change: Mapped[float] = mapped_column(default=0.0)  # pixel gate: share of the picture that moved
    reading: Mapped[str] = mapped_column(Text, default="")
    line: Mapped[str] = mapped_column(String(200), default="")  # "Open, busy, egg shelf low"
    # last, so a scan of the columns above skips the picture's overflow pages in SQLite
    data: Mapped[bytes] = mapped_column(LargeBinary)


class Message(Base):
    """A line in one of two threads: the manager's WhatsApp, or the owner's chat with Musha.
    `text` is what the reader of that thread sees; `text_en` keeps the English for the owner."""
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), index=True)
    thread: Mapped[str] = mapped_column(String(12), default="manager")  # manager | owner
    sender: Mapped[str] = mapped_column(String(16))  # manager | musha | owner
    text: Mapped[str] = mapped_column(Text)
    text_en: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(String(8), default="en")
    request_id: Mapped[int | None] = mapped_column(ForeignKey("spend_requests.id"), default=None)
    attachment_id: Mapped[int | None] = mapped_column(ForeignKey("attachments.id"), default=None)
    at: Mapped[datetime] = mapped_column(default=now)


class Heard(Base):
    """A message from the manager's phone, by the phone's own id or by its words, so a retry
    or a second tap is heard once. `until` is the last line it produced; None while Musha reads.
    `line_id` is the manager's own line, once kept. `request_ids` are the requests it made or
    answered. The shop id carries no foreign key: a row older than its shop comes from an
    earlier database, and its message reads as new."""
    __tablename__ = "heard"
    __table_args__ = (UniqueConstraint("shop_id", "key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(index=True)
    key: Mapped[str] = mapped_column(String(80))  # id:<client id> | text:<sha256> | voice:<sha256>
    since: Mapped[int] = mapped_column(default=0)  # the shop's last message id before this one
    until: Mapped[int | None] = mapped_column(default=None)
    line_id: Mapped[int | None] = mapped_column(default=None)
    request_ids: Mapped[str] = mapped_column(Text, default="[]")
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
    purpose: Mapped[str] = mapped_column(String(24), default="wages")  # wages | release
    request_id: Mapped[int | None] = mapped_column(ForeignKey("spend_requests.id"), default=None)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(default=now)
