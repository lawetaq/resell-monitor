from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from src.models import Listing
from src.sources.avito import AvitoError, AvitoSource
from src.sources.base import MarketplaceSource

OUTPUT_DIR = Path("output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Загрузка поисковой страницы Авито.",
    )
    parser.add_argument(
        "--url",
        required=True,
        help="Полная ссылка на поисковую выдачу Авито.",
    )
    parser.add_argument(
        "--debug-save",
        action="store_true",
        help="Сохранить timestamped HTML/JSON-диагностику в output/.",
    )
    parser.add_argument(
        "--transport",
        choices=("auto", "curl_cffi", "requests", "playwright"),
        default="auto",
        help="Транспорт загрузки (по умолчанию: auto).",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Показать Chromium при использовании Playwright.",
    )
    parser.add_argument(
        "--diagnostic-pause",
        type=float,
        default=15.0,
        metavar="SECONDS",
        help="Пауза перед закрытием headed-браузера (по умолчанию: 15).",
    )
    return parser


def display_listing(number: int, listing: Listing) -> None:
    location = listing.location or "Не указано"
    print()
    print(f"{number}. {listing.title}")
    print(f"   Цена: {listing.price_display}")
    print(f"   Местоположение: {location}")
    print(f"   Ссылка: {listing.url}")


def run(
    argv: Sequence[str] | None = None,
    *,
    source: MarketplaceSource | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.diagnostic_pause < 0:
        print("Ошибка: --diagnostic-pause не может быть отрицательной.")
        return 2

    marketplace_source = source or AvitoSource(
        transport_mode=args.transport,
        headed=args.headed,
        diagnostic_pause_seconds=args.diagnostic_pause,
    )
    debug_dir = OUTPUT_DIR if args.debug_save else None

    try:
        result = marketplace_source.search(args.url, debug_dir=debug_dir)
    except AvitoError as error:
        print(f"Ошибка: {error}")
        return 1

    status = result.status_code if result.status_code is not None else "неизвестен"
    print(f"HTTP-статус: {status}")
    transport = result.transport
    if result.fallback_reason:
        transport += f" (fallback: {result.fallback_reason})"
    print(f"Transport: {transport}")
    print(f"Extraction: {result.extraction}")
    print(f"Найдено объявлений: {len(result.listings)}")

    for number, listing in enumerate(result.listings[:5], start=1):
        display_listing(number, listing)

    if debug_dir is not None:
        print(f"\nОтладочные данные сохранены в: {debug_dir}")

    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
