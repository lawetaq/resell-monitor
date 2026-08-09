from __future__ import annotations


def score_badge(score: int | None) -> dict[str, str]:
    if score is None:
        return {"label": "INSUFFICIENT DATA", "tone": "insufficient"}
    if score >= 80:
        return {"label": "BUY", "tone": "strong-green"}
    if score >= 65:
        return {"label": "GOOD DEAL", "tone": "green"}
    if score >= 45:
        return {"label": "NEGOTIATE", "tone": "amber"}
    return {"label": "PASS", "tone": "red"}


def format_market_summary(summary: dict[str, object]) -> str:
    return (
        f"Median: {summary['median']}; Q1–Q3: {summary['q1']}–{summary['q3']}; "
        f"sample: {summary['sample_count']}; 7d: {_percent(summary.get('trend_7d_percent'))}; "
        f"30d: {_percent(summary.get('trend_30d_percent'))}; "
        f"disappeared/turnover signal: {summary['disappeared_count']}"
    )


def _percent(value: object) -> str:
    return "—" if value is None else f"{value}%"
