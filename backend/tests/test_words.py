import re
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from musha import insights, proof
from musha.models import LedgerEntry
from musha.words import number, qty, singular


@pytest.mark.parametrize("unit, one", [
    ("trays", "tray"),
    ("loaves", "loaf"),
    ("2 kg packs", "2 kg pack"),
    ("2 L bottles", "2 L bottle"),
    ("boxes", "box"),
    ("batteries", "battery"),
    ("bunches", "bunch"),
    ("dishes", "dish"),
    ("glasses", "glass"),
    ("units", "unit"),
    ("dozens", "dozen"),
    ("glass", "glass"),
    ("kg", "kg"),
    ("Trays", "Tray"),
    ("", ""),
])
def test_the_last_word_turns_singular(unit, one):
    assert singular(unit) == one


@pytest.mark.parametrize("n, unit, said", [
    (40, "trays", "40 trays"),
    (1, "trays", "1 tray"),
    (1, "loaves", "1 loaf"),
    (20, "loaves", "20 loaves"),
    (24, "2 L bottles", "24 × 2 L bottles"),
    (1, "2 kg packs", "1 × 2 kg pack"),
    (10, "2 kg packs", "10 × 2 kg packs"),
    (-1, "trays", "-1 tray"),
    (2, "trays", "2 trays"),
    (0, "trays", "0 trays"),
    (1.0, "trays", "1 tray"),
    (5.5, "trays", "5.5 trays"),
    (2.5, "2 L bottles", "2.5 × 2 L bottles"),
    (3, "", "3"),
    (3, "  trays ", "3 trays"),
])
def test_a_count_reads_in_the_shops_unit(n, unit, said):
    assert qty(n, unit) == said


def test_a_whole_float_reads_as_a_whole_number():
    assert (number(40.0), number(39.5), number(7)) == ("40", "39.5", "7")


# The rule, where the backend prints a count

SUGAR = SimpleNamespace(kind="stock", item="sugar", quantity=10, unit="2 kg packs", payee="Mbare wholesale",
                        hold=SimpleNamespace(amount_cents=1900),
                        shop=SimpleNamespace(currency="USD", owner=SimpleNamespace(name="Chipo"),
                                             manager=SimpleNamespace(name="Farai")))
TWO_NUMBERS = re.compile(r"\b\d+(?:\.\d+)? \d")  # "24 2 L", the shape the rule replaces


def test_the_quantity_reason_and_question_read_by_the_rule():
    one_pack = proof.Reading(source="model", payee="Mbare wholesale", receipt_no="MW-1", day=None,
                             lines=[proof.Line("sugar (2 kg packs)", 1, 190, 190)], total_cents=190,
                             currency="USD", legible=True)
    c = proof.match_check(SUGAR, one_pack)
    assert "quantity" in c.data["failed"]
    assert "The receipt shows 1 × 2 kg pack; the request was for 10 × 2 kg packs." in c.reason
    c.data["failed"] = ["quantity"]
    assert proof.template_question(SUGAR, c, one_pack) == (
        "How many 2 kg packs of sugar did you buy? The request was for 10 × 2 kg packs.")


def test_a_shelf_count_question_reads_by_the_rule():
    oil = SimpleNamespace(name="cooking oil", unit="2 L bottles")
    asked = insights.stock_question(date(2026, 10, 31), oil, 3, 1)
    assert asked == ("On Saturday 31 October the shelf count for cooking oil was 1 × 2 L bottle. "
                     "The books expected 3. What happened to the other 2 × 2 L bottles?")
    assert insights.stock_question(date(2026, 10, 31), oil, 3, 4).endswith(
        "Where did the extra 1 × 2 L bottle come from?")


def test_the_seeded_books_print_counts_by_the_rule(app):
    with app.state.sessions() as s:
        memos = [e.memo for e in s.scalars(select(LedgerEntry).where(
            LedgerEntry.item.in_(("sugar", "cooking oil")), LedgerEntry.quantity > 0))]
    assert memos and not [m for m in memos if TWO_NUMBERS.search(m)]
    assert any(re.fullmatch(r"\d+ × 2 kg packs sold", m) for m in memos)
    assert any(re.fullmatch(r"\d+ × 2 L bottles from Mbare wholesale", m) for m in memos)
    assert any(m.startswith("Opening stock, ") and "× 2 kg pack" in m for m in memos)
