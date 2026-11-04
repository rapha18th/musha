"""Counts in the shop's own units, by the same rule as the web app's words.ts.

    qty(40, "trays") -> "40 trays"
    qty(1, "trays") -> "1 tray"
    qty(24, "2 L bottles") -> "24 × 2 L bottles"
    qty(1, "2 kg packs") -> "1 × 2 kg pack"

A count of one makes the unit's last word singular. A unit that starts with its own number
takes a multiplication sign, so the two numbers read apart.
"""
import re

_LAST = re.compile(r"(\w+)$", re.ASCII)


def _one(word: str) -> str:
    if re.search(r"ves$", word, re.I):
        return word[:-3] + "f"
    if re.search(r"ies$", word, re.I):
        return word[:-3] + "y"
    if re.search(r"(ch|sh|x|ss)es$", word, re.I):
        return word[:-2]
    if re.search(r"[^s]s$", word, re.I):
        return word[:-1]
    return word


def singular(unit: str) -> str:
    """'tray' from 'trays', 'loaf' from 'loaves', '2 kg pack' from '2 kg packs': the last word made singular."""
    return _LAST.sub(lambda m: _one(m.group(1)), unit)


def number(n) -> str:
    """A count as people write it: 40, 5.5, and 1 for 1.0."""
    if isinstance(n, float) and n.is_integer():
        return str(int(n))
    return str(n)


def qty(n, unit: str) -> str:
    """A count in the shop's unit: '40 trays', '1 tray', '24 × 2 L bottles'."""
    u = unit.strip()
    if abs(n) == 1:
        u = singular(u)
    if not u:
        return number(n)
    return f"{number(n)} × {u}" if "0" <= u[0] <= "9" else f"{number(n)} {u}"
