from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from src.retail_providers import DnsRetailProvider, OzonRetailProvider, WildberriesRetailProvider


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="One-attempt retail provider diagnostic")
    parser.add_argument("provider", choices=("dns", "ozon", "wildberries"))
    parser.add_argument("--query")
    parser.add_argument("--key", help="Expected comparable key (defaults to normalized query)")
    parser.add_argument("--url", help="Explicit mapped product URL")
    parser.add_argument("--product-id", help="Wildberries nmId for one mapped-product request")
    parser.add_argument("--region")
    parser.add_argument("--output-dir", type=Path, default=Path("debug/retail"))
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.query and not (args.provider == "wildberries" and (args.url or args.product_id)):
        raise SystemExit("--query is required unless a Wildberries --url or --product-id is supplied")
    from src.analytics import normalize_product
    query = args.query or args.key or ""
    key = args.key or normalize_product(query).comparable_key
    if not key:
        raise SystemExit("query cannot be normalized; provide --key")
    classes = {"dns": DnsRetailProvider, "ozon": OzonRetailProvider,
               "wildberries": WildberriesRetailProvider}
    provider = classes[args.provider]()
    try:
        mapped_url = args.url
        if args.product_id:
            if args.provider != "wildberries" or not args.product_id.isdigit():
                raise SystemExit("--product-id is a numeric Wildberries nmId")
            mapped_url = args.product_id
        result = provider.search(key, query, region=args.region, mapped_url=mapped_url)
    finally:
        provider.close()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = args.output_dir / f"{args.provider}-{stamp}"
    target.mkdir(parents=True, exist_ok=True)
    suffix = ".json" if args.provider == "wildberries" else ".html"
    if result.raw_body is not None:
        (target / f"response{suffix}").write_text(_sanitize(result.raw_body), encoding="utf-8")
    metadata = {"timestamp": stamp, "provider": result.retailer,
                "status": result.status_code, "transport": result.transport,
                "health": result.health, "final_url": _safe_url(result.final_url),
                "candidates_found": result.candidates_found,
                "accepted_matches": len(result.observations),
                "prices": [item.price for item in result.observations],
                "representative_price": _representative_price(result),
                "product_ids": sorted({item.product_id for item in result.observations if item.product_id}),
                "availability": sorted({item.availability for item in result.observations}),
                "region": args.region, "region_context": result.region_context,
                "retrieval_method": result.retrieval_method,
                "block_classification": result.block_classification,
                "retry_after_seconds": result.retry_after_seconds,
                "response_content_type": result.response_content_type,
                "response_size": result.response_size,
                "error": result.error}
    (target / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps(metadata, indent=2))
    return 0 if result.health == "healthy" else 2


def _safe_url(value: str | None) -> str | None:
    if not value: return value
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _sanitize(value: str) -> str:
    patterns = (
        r'(?i)(authorization|access[_-]?token|refresh[_-]?token|api[_-]?key|cookie)(["\s:=]+)([^"\s,;<]+)',
        r'(?i)(https?://)([^/@\s:]+):([^/@\s]+)@',
    )
    result = value
    for pattern in patterns:
        result = re.sub(pattern, lambda m: m.group(1) + "[REDACTED]" if len(m.groups()) == 3 and m.group(1).startswith("http") else m.group(1) + m.group(2) + "[REDACTED]", result)
    return result


def _representative_price(result) -> int | None:
    prices = sorted(item.price for item in result.observations
                    if item.availability == "available")
    if not prices:
        return None
    middle = len(prices) // 2
    return prices[middle] if len(prices) % 2 else int(round((prices[middle - 1] + prices[middle]) / 2))


if __name__ == "__main__":
    raise SystemExit(run())
