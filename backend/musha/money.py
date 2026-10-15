"""Money is integer cents inside Musha and a two-decimal string at the PayPal edge."""
from decimal import ROUND_HALF_UP, Decimal


def to_cents(value) -> int:
    if isinstance(value, int):
        raise TypeError("pass a decimal string or Decimal, cents are ints already")
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_value(cents: int) -> str:
    return f"{Decimal(cents) / 100:.2f}"


def amount(cents: int, currency: str) -> dict:
    return {"currency_code": currency, "value": to_value(cents)}
