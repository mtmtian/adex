"""Shared brand matching for transcript and OCR output."""

from __future__ import annotations

import re
from typing import NamedTuple

try:
    from .config import BRANDS
except ImportError:  # direct script compatibility
    from config import BRANDS  # type: ignore[no-redef]


class BrandHit(NamedTuple):
    brand: str
    matched_text: str


_CANONICAL = {brand.casefold(): brand for brand in BRANDS}
_PATTERN = re.compile("|".join(re.escape(brand) for brand in BRANDS), re.IGNORECASE)


def find_brand_hits(text: str) -> list[BrandHit]:
    if not text:
        return []
    return [
        BrandHit(_CANONICAL.get(match.group(0).casefold(), match.group(0)), match.group(0))
        for match in _PATTERN.finditer(text)
    ]


def has_brand_hit(text: str) -> bool:
    return bool(text and _PATTERN.search(text))
