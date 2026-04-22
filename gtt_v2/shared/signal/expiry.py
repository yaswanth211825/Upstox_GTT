"""Expiry date parser — ported from gtt_strategy.parse_expiry_date."""
import re
from datetime import datetime, date
from typing import Optional


def parse_expiry_date(expiry_str: str) -> Optional[date]:
    """
    Parse expiry string to a date object.
    Accepts: "6th NOVEMBER", "06 NOV 2025", "06NOV25", "18th DECEMBER",
             "26 DEC 25", "2025-12-26", "26DEC2025"
    Returns None if unparseable.
    """
    if not expiry_str:
        return None

    s = str(expiry_str).strip().upper()

    # ISO format first (2025-12-26)
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        pass

    # Remove ordinal suffixes
    cleaned = re.sub(r"(ST|ND|RD|TH)\b", "", s).strip()

    # Fix 2-digit year at end: "26 DEC 25" or "26DEC25" → "26 DEC 2025"
    m = re.search(r"(\d{2})\s*$", cleaned)
    if m and not re.search(r"\d{4}", cleaned):
        yy = int(m.group(1))
        yyyy = 2000 + yy if yy <= 50 else 1900 + yy
        cleaned = cleaned[: m.start()].strip() + f" {yyyy}"

    # Add current year if no 4-digit year found
    if not re.search(r"\d{4}", cleaned):
        cleaned = f"{cleaned} {datetime.now().year}"

    formats = [
        "%d %B %Y",
        "%d %b %Y",
        "%d%B%Y",
        "%d%b%Y",
        "%d%b %Y",   # "26DEC 2025" — compact month + space + year
        "%d %b%Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(cleaned.replace("  ", " "), fmt).date()
        except ValueError:
            continue

    return None
