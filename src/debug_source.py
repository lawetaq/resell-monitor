from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from src.source_diagnostics import DiagnosticConfig, run_diagnostic


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Perform one marketplace retrieval and save sanitized diagnostics."
    )
    parser.add_argument("source", choices=("avito", "farpost"))
    parser.add_argument("--url", required=True)
    parser.add_argument(
        "--transport",
        choices=("curl_cffi", "requests", "playwright"),
        default="curl_cffi",
        help="Avito transport (FarPost always uses requests)",
    )
    parser.add_argument(
        "--impersonation",
        default="chrome",
        help="Fixed curl_cffi impersonation target for Avito",
    )
    parser.add_argument(
        "--session-mode",
        choices=("persistent", "fresh"),
        default="persistent",
        help="Avito curl_cffi session lifecycle",
    )
    route = parser.add_mutually_exclusive_group()
    route.add_argument(
        "--direct",
        action="store_true",
        help="Use the direct network route (the default)",
    )
    route.add_argument(
        "--proxy",
        help="Use one user-supplied HTTP(S) or SOCKS proxy URL",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output-dir", type=Path, default=Path("output/source-debug"))
    parser.add_argument(
        "--playwright-profile-dir",
        type=Path,
        default=Path("data/playwright/diagnostic-profile"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.source == "farpost" and args.transport != "curl_cffi":
        parser.error("--transport applies only to Avito")
    config = DiagnosticConfig(
        source=args.source,
        url=args.url,
        output_dir=args.output_dir,
        avito_transport=args.transport,
        impersonation=args.impersonation,
        session_mode=args.session_mode,
        proxy=args.proxy,
        timeout_seconds=args.timeout,
        playwright_profile_dir=args.playwright_profile_dir,
    )
    try:
        result = run_diagnostic(config)
    except (ValueError, RuntimeError) as error:
        parser.exit(1, f"diagnostic failed: {error}\n")

    fields = (
        ("source", result.source),
        ("transport", result.transport),
        ("route", result.route),
        ("impersonation", result.impersonation),
        ("session mode", result.session_mode),
        ("response cookie names", ", ".join(result.response_cookie_names) or "none"),
        ("HTTP status", result.http_status),
        ("final URL", result.final_url),
        ("page title", result.page_title),
        ("content type", result.content_type),
        (
            "extraction candidates found",
            json.dumps(result.extraction_candidates, ensure_ascii=False, sort_keys=True),
        ),
        ("challenge/block classification", result.challenge_classification),
        ("listing count", result.listing_count),
        ("parse error", result.parse_error),
        ("retrieval error", result.retrieval_error),
        ("raw HTML artifact", result.html_path),
        ("metadata artifact", result.metadata_path),
    )
    for label, value in fields:
        print(f"{label}: {value if value is not None else 'unavailable'}")
    return 1 if result.retrieval_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
