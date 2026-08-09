from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Iterable, Mapping, Any


def export_json(rows: Iterable[Mapping[str, Any]], path: Path) -> None:
    path.write_text(json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2), encoding="utf-8")


def export_txt(rows: Iterable[Mapping[str, Any]], path: Path) -> None:
    path.write_text("\n\n".join(f"[{row['source']}] {row['title']}\n{row['price_display']} | {row['status']}\n{row['url']}" for row in rows), encoding="utf-8")


def export_html(rows: Iterable[Mapping[str, Any]], path: Path) -> None:
    cards = []
    for row in rows:
        cards.append(f"<article><h2><a href='{html.escape(row['url'], quote=True)}'>{html.escape(row['title'])}</a></h2><p>{html.escape(row['price_display'])} · {html.escape(row['source'])} · {html.escape(row['status'])}</p></article>")
    path.write_text("<!doctype html><meta charset='utf-8'><title>Resell Monitor</title><h1>Listings</h1>" + "".join(cards), encoding="utf-8")
