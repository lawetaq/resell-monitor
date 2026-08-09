from __future__ import annotations

from src.models import Listing, SearchConfig


def matches(listing: Listing, config: SearchConfig) -> bool:
    text = f"{listing.title} {listing.description or ''}".casefold()
    if config.min_price is not None and (listing.price is None or listing.price < config.min_price):
        return False
    if config.max_price is not None and (listing.price is None or listing.price > config.max_price):
        return False
    if config.include_terms and not any(term.casefold() in text for term in config.include_terms):
        return False
    if config.brands and not any(brand.casefold() in text for brand in config.brands):
        return False
    return not any(term.casefold() in text for term in config.exclude_terms)
