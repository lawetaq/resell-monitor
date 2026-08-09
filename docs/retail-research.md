# Retail provider research — 2026-08-08

The providers use only normal consumer access paths. There is no CAPTCHA solver,
proxy rotation, paid bypass, automatic IP switching, retry loop, or browser
fingerprint randomization.

## DNS

- Consumer search and product pages are HTML and may include JSON-LD product data.
- A single controlled search request from this network returned HTTP 401 with a
  Qrator challenge page. No retry was made.
- DNS has no documented public buyer/catalog API suitable for this application.
- Price and stock are region-sensitive. The configured locality is stored with
  every observation; it is not presented as a universal price.
- Chosen path: persistent ordinary HTTP; JSON-LD/catalog-card parsing when normal
  HTML is returned. Explicit mapped product URLs are preferred over search.
- Current status: **BLOCKED / experimental on this network**.

## Ozon

- Official public APIs are seller/business APIs, not an anonymous buyer catalog API.
- Consumer HTML can expose structured widget state, but endpoint and schema are
  undocumented and therefore fragile.
- A single controlled consumer-search request entered a repeated HTTP 307 redirect
  sequence and was stopped. No retry was made.
- Multiple third-party offers and seller identity matter; representative pricing
  uses a median of exact credible offers.
- Chosen path: persistent ordinary HTTP and embedded widget JSON parsing. Explicit
  mapped product URLs are preferred.
- Current status: **DEGRADED / experimental on this network**.

## Wildberries

- Official documented APIs are seller-side APIs. Consumer catalog search uses an
  undocumented structured JSON response.
- Multiple sellers/offers and the destination/region parameter matter.
- A single controlled request to the consumer search JSON endpoint returned HTTP
  429. No retry was made.
- Chosen path: one normal request to the structured consumer search endpoint, with
  explicit destination context and strict local comparable-key matching.
- Current status: **BLOCKED / experimental on this network**.

## Reliability decision

Search is deliberately conservative and remains experimental for all three
providers. Manual per-product URL mappings are supported in storage so future GUI
mapping can use known product pages without redesigning providers. No provider is
allowed to affect another provider's refresh or the existing used-market ranking
when retail data is absent, stale, ambiguous, or low-confidence.
