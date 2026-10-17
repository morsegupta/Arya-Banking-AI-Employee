"""Money is stored as integer paise everywhere. Never floats."""

from __future__ import annotations


def rupees_to_paise(rupees: int | float | str) -> int:
    # Go through str so 1234.5 -> "1234.50" -> 123450 without float error.
    text = f"{float(rupees):.2f}" if not isinstance(rupees, str) else rupees.strip()
    whole, _, frac = text.replace(",", "").partition(".")
    frac = (frac + "00")[:2]
    sign = -1 if whole.startswith("-") else 1
    return sign * (abs(int(whole or "0")) * 100 + int(frac))


def paise_to_str(paise: int) -> str:
    """Plain decimal string, e.g. 34000000 -> '340000.00'. Used in CSV/REST."""
    sign = "-" if paise < 0 else ""
    paise = abs(paise)
    return f"{sign}{paise // 100}.{paise % 100:02d}"


def format_inr(paise: int, symbol: str = "₹") -> str:
    """Indian digit grouping, e.g. 34000000 -> '₹3,40,000.00'."""
    sign = "-" if paise < 0 else ""
    paise = abs(paise)
    whole, frac = divmod(paise, 100)
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups) + "," + tail
    return f"{sign}{symbol}{digits}.{frac:02d}"
