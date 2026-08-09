# Retail provider research — updated 2026-08-09

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
- The canonical consumer host remains `www.ozon.ru`; search uses `/search/` and
  product cards use `/product/<slug>-<numeric-id>/`. The numeric article in the
  canonical product path is retained as mapping identity.
- Consumer HTML can expose embedded JSON widget state. Search and mapped cards
  are parsed from that state without undocumented API calls or JavaScript execution.
  The schema remains undocumented and therefore fragile.
- A bounded route diagnostic found that the first 307 stays on the same search
  path and adds only the `__rr` routing parameter. With the ordinary anonymous
  session retained, the next response was HTTP 403. The old automatic Requests
  redirect handling hid this sequence; a bare 307 was not itself proof of blocking.
- Ozon follows at most three redirects, stops before revisiting a URL, and records
  only redirect host/path, changed query names, and Set-Cookie names. HTTP 401/403
  and 429 are blocked; redirect cycles and 5xx are degraded; network errors fail;
  HTTP 200 with no recognized product state is a schema change.
- Current public cards visibly distinguish a lower Ozon Card/bank price from the
  higher ordinary-payment price. The ordinary price is the retail reference;
  card/bank price is conditional and stored separately. Original/list price is
  separate and delivery is not folded into product price.
- Seller, offer, and product IDs are retained when widget state exposes them.
  Multiple compatible seller offers stay distinct and their representative is a
  median. Strict normalized titles still reject incompatible variants.
- Search is explicit discovery. A reviewed canonical product mapping is preferred
  for scheduled monitoring; invalid or identity-mismatched mappings fail without
  broad-search fallback.
- No supported anonymous city/destination mechanism was established. The configured
  region is not sent to Ozon; observations are labeled
  `ozon_scope=default-unresolved`, not presented as Khabarovsk-specific.
- Fixed ordinary browser headers and a persistent Requests session are used. No
  curl/browser fallback was attempted after the explicit 403. There is no retry,
  fingerprint rotation, account-cookie import, or challenge handling.
- Current status: **BLOCKED ON TEST ROUTE / experimental**. Whether this execution
  route is VPN or direct could not be determined.

## Wildberries

- Official documented APIs are seller-side, authenticated APIs and are not used
  for anonymous retail comparison. Consumer catalog responses remain undocumented.
- The production candidate search path is one GET to
  `search.wb.ru/exactmatch/ru/common/v18/search`. An existing manual mapping is
  preferred and becomes one GET to `card.wb.ru/cards/v4/detail` using the stable
  numeric catalog `nmId`; the product HTML page is not fetched first.
- Search and mapped-card retrieval use fixed normal JSON request headers and a
  persistent Requests session. There is no retry or automatic transport fallback.
- A configured region name is not sent as `dest`. WB destination IDs are opaque
  numeric values. An explicit value such as `Region name; wb_dest=-123` is used
  when supplied; otherwise the observation records `wb_dest=unresolved` and the
  request has no `dest`, so its geographic scope is not overstated.
- Consumer size prices are separated: `price.product` is treated as the normal
  discounted buyer price, `price.basic` as the original/list price, and a lower
  `price.total` as conditional wallet/club/payment-method evidence. Conditional
  price is stored separately and never drives the representative retail price.
- Stock is aggregated from explicit size quantities. Zero is out of stock;
  missing stock evidence remains unknown. Multiple sizes remain distinct offers.
- Product titles still require exact normalized PC-component identity. A mapped
  response is additionally filtered to the requested `nmId`.
- On the Codex test route, both Russian and global storefront documents returned
  WB HTTP 498 challenge pages before catalog JavaScript ran. Fixed normal Chromium
  was challenged too. VPN/direct route could not be determined, and no challenge
  solving was attempted.
- The earlier 429 is now classified as rate limiting, captures a sanitized
  `Retry-After`, and schedules the later of the normal refresh interval or that
  delay. A block is never parsed as catalog JSON or retried automatically.
- Current status: **BLOCKED ON TEST ROUTE / experimental** until a one-request
  direct non-VPN diagnostic verifies search or mapped-card access.
- The final one-request Requests acceptance attempt could not resolve
  `search.wb.ru` in the execution environment and therefore produced no HTTP
  response. This is classified as a route/network DNS failure, not as evidence
  that Wildberries universally blocks anonymous clients.

## Reliability decision

Search is deliberately conservative and remains experimental for all three
providers. Manual per-product mappings are preferred for exact retail refresh.
No provider is
allowed to affect another provider's refresh or the existing used-market ranking
when retail data is absent, stale, ambiguous, or low-confidence.
