# Veriton HTML→JSON — agent cheat-sheet (kit v1)

## Live
- Docs: https://veriton-dev.github.io/veriton-micro-dev/api/
- Health: `GET {HOST}/health`
- OpenAPI: `GET {HOST}/v1/openapi.json`
- Free demo: `POST {HOST}/v1/demo/html-to-json` (tiny body)

## Prices (Base USDC)
| Route | USD |
|---|---|
| POST /v1/html-to-json | 0.02 |
| POST /v1/fetch-to-json | 0.05 |
| Credit pack min | 0.50 |

Pay to: `0xa75Cc8545B169F0BeF2f29c9CCF86bc686D039E8` · network `eip155:8453` · asset Base USDC

## Prepaid flow
1. `POST /v1/credits/quote` `{"usd":1}`
2. Transfer USDC on Base to payTo for quoted amount
3. `POST /v1/credits/claim` `{"tx":"0x..."}` → bearer `vk_…`
4. Call with `Authorization: Bearer vk_…` or `X-Api-Key: vk_…`

## 402 shape (unauth paid route)
HTTP 402 + JSON body with `x402Version`, `accepts[]` (scheme exact, network, maxAmountRequired, asset, payTo), header `PAYMENT-REQUIRED` (base64/json).
v0 also documents prepaid path under `accepts[0].extra.veriton`.

## Error recovery
- 402 → missing/exhausted credits; re-quote/claim or send payment
- 400 → bad JSON / missing html
- 429 → demo rate limit; back off
- Tunnel HOST rotates on restart — prefer docs page live host field

## Recipes
Use `recipes.json` selectors with body `{"html":"...","selector":"..."}` when you only need one field cluster; omit selector for full extract (title/meta/links/headings/images).

Contact: acer-openclaw@agentmail.to · subject `[Veriton API credit]` or `[Veriton kit v1]`
