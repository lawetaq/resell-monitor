from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping


def collision_safe_export_path(output_dir: Path, extension: str,
                               rows: Iterable[Mapping[str, Any]], *,
                               export_kind: str = "listings",
                               timestamp: datetime | None = None) -> Path:
    values = [dict(row) for row in rows]
    categories = {str(row.get("product_category")) for row in values
                  if row.get("product_category")}
    scope = next(iter(categories)) if len(categories) == 1 else "mixed" if categories else "all"
    stamp = (timestamp or datetime.now().astimezone()).strftime("%Y-%m-%d_%H%M%S")
    stem = _safe_name(f"{export_kind}_{scope}_{stamp}")
    candidate = output_dir / f"{stem}.{extension}"
    suffix = 2
    while candidate.exists():
        candidate = output_dir / f"{stem}_{suffix}.{extension}"
        suffix += 1
    return candidate


def export_json(rows: Iterable[Mapping[str, Any]], path: Path) -> None:
    values = [dict(row) for row in rows]
    values = [_public_row(row) for row in values if _normal_row(row)]
    path.write_text(json.dumps(values, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def export_txt(rows: Iterable[Mapping[str, Any]], path: Path, *, include_history: bool = False) -> None:
    values = [dict(row) for row in rows]
    values = [_public_row(row) for row in values if _normal_row(row)]
    active = [row for row in values if row.get("availability") == "active"]
    historical = [row for row in values if row not in active]
    groups = (("Priority 1", "1"), ("Priority 2", "2"), ("Priority 3", "3"))
    sections = ["CURRENT OPPORTUNITIES"]
    for label, priority in groups:
        selected = [row for row in active if row.get("priority") == priority]
        sections.append(f"\n{label}\n" + ("\n\n".join(_txt_row(row) for row in selected) or "None"))
    review = [row for row in active if row.get("priority") == "4 / needs_review"]
    sections.append("\nACTIVE NEEDS REVIEW\n" + ("\n\n".join(_txt_row(row) for row in review) or "None"))
    remaining = [row for row in active if row.get("priority") not in
                 {value for _, value in groups} | {"4 / needs_review"}]
    if remaining:
        sections.append("\nOTHER ACTIVE LISTINGS / PASS\n" + "\n\n".join(_txt_row(row) for row in remaining))
    if include_history and historical:
        sections.append("\nHISTORICAL / UNAVAILABLE\n" +
                        "\n\n".join(_txt_row(row) for row in historical))
    path.write_text("\n".join(sections), encoding="utf-8")


def export_html(rows: Iterable[Mapping[str, Any]], path: Path, *, include_history: bool = False) -> None:
    cards = []
    for source in rows:
        source = dict(source)
        if not _normal_row(source):
            continue
        row = _public_row(source)
        if row.get("availability") != "active" and not include_history:
            continue
        reasons = _combined_reasons(row)
        cards.append(
            f"<article><h2><a href='{html.escape(str(row['url']), quote=True)}'>"
            f"{html.escape(str(row['title']))}</a></h2><p>{html.escape(str(row['price_display']))} · "
            f"{html.escape(str(row['source']))} · {html.escape(str(row.get('availability', 'unknown')))} · "
            f"Priority {html.escape(str(row.get('priority', '—')))}</p>"
            f"<p>Overall {row.get('overall_score', '—')} · Deal {row.get('deal_score', '—')} · "
            f"Confidence {row.get('confidence_score', '—')} · Liquidity {row.get('liquidity_score', '—')} · "
            f"Risk {row.get('risk_score', '—')}</p><ul>"
            + "".join(f"<li>{html.escape(str(reason))}</li>" for reason in reasons) + "</ul></article>"
        )
    path.write_text("<!doctype html><meta charset='utf-8'><title>Resell Monitor</title><h1>Listings</h1>"
                    + "".join(cards), encoding="utf-8")


def _normal_row(row: Mapping[str, Any]) -> bool:
    return bool(row.get("candidate_valid", 1))


def _public_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return dict(row)


def _txt_row(row: Mapping[str, Any]) -> str:
    reasons = _combined_reasons(row)
    metrics = (f"overall={row.get('overall_score', '—')} deal={row.get('deal_score', '—')} "
               f"confidence={row.get('confidence_score', '—')} liquidity={row.get('liquidity_score', '—')} "
               f"risk={row.get('risk_score', '—')}")
    return (f"[{row['source']}] {row['title']}\n{row['price_display']} | "
            f"{row.get('availability', 'unknown')} | {row.get('condition_class', 'OK')}\n"
            f"{metrics}\n{row.get('verdict', row.get('recommendation', '—'))}\n"
            + "\n".join(f"- {reason}" for reason in reasons) + f"\n{row['url']}")


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(" .-")
    return cleaned[:120] or "export"


def _combined_reasons(row: Mapping[str, Any]) -> list[Any]:
    values = [*row.get("score_reasons", []), *row.get("risk_reasons", []),
              *row.get("review_reasons", [])]
    return list(dict.fromkeys(values))
