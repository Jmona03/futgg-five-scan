# FUT.GG Five Scan

Flask + Playwright Chromium backend for a Render **Docker Web Service**.
Scans 1–5 specific FUT.GG card pages on the **Console** market. It returns
prices and trade estimates in the response, rather than just links.

## Deploy on Render

1. Create a Web Service connected to `Jmona03/futgg-five-scan`, branch `main`.
2. Choose **Docker**, Dockerfile `./Dockerfile`, context `.`. Leave the custom
   start command empty; the Dockerfile runs Gunicorn on `0.0.0.0:$PORT`.
3. Set the health check path to `/health`.
4. Optionally set `SCAN_API_KEY` to a secret of your choice, then send it as
   `Authorization: Bearer <secret>`. Without this variable the endpoint is public.
5. Deploy. No Node build command or separate browser installation is needed.

Chromium and its system dependencies are installed in the image. The browser
version matches the pinned Playwright package. One worker allows one scan at a
time; two tabs run concurrently. Use enough RAM for Chromium (1 GB is a sensible
starting point). Cold starts and five-page scans can take time; allow up to 240
seconds in the caller. A running scan produces HTTP 429 for another request.

## Request

`POST /api/futgg-scan` with `Content-Type: application/json`:

```json
{
  "urls": [
    "https://www.fut.gg/players/27-joe-cole/27-27/",
    "https://www.fut.gg/players/27-joe-cole/27-67108891/"
  ]
}
```

These are separate Standard and Holographic card URLs. Copy the URL for the
actual card version you want; player career hubs and the momentum overview
are rejected because they do not identify a single card. URLs must use HTTPS,
the `fut.gg` or `www.fut.gg` host, and `/players/<player>/<year>-<item-id>/`.
Fragments are removed; query strings, credentials, custom ports, and other hosts
are rejected. Top-level browser redirects to other hosts are blocked.

```bash
curl --request POST https://YOUR-SERVICE.onrender.com/api/futgg-scan \
  --header 'Content-Type: application/json' \
  --header 'Authorization: Bearer YOUR-SECRET' \
  --data '{"urls":["https://www.fut.gg/players/27-joe-cole/27-27/"]}'
```

In an iPhone Shortcut, use **Get Contents of URL**, method **POST**, request
body **JSON**, with `urls` as an array of up to five text URLs. Read the
`results` array in the JSON response to display each report. Omit the
Authorization header if no `SCAN_API_KEY` was set.

## Response

The response contains `scanned_at` (UTC ISO timestamp), `market`, `tax_rate`,
`target_net_profit`, and a `results` array in the same order as the input.
Each result includes:

| Field | Meaning |
| --- | --- |
| `url`, `resolved_url`, `name`, `item_version`, `fetched_at` | Source, card identity and scan time |
| `current_price`, `price_updated_text` | Displayed Lowest BIN and its update age |
| `average`, `low`, `high` | Labelled Price Momentum average, lowest and highest; not EA price-range limits |
| `price_momentum` | Signed percent, coin change, displayed text and period description |
| `lowest_bin_history` | Graph hover samples with displayed date labels and prices |
| `recent_sales` | Completed-sale price, displayed time, parsed age in minutes |
| `active_listings` | Auction ending time, start bid, BIN |
| `conservative_sell_price` | Conservative, tick-rounded resale estimate |
| `max_buy_price` | Maximum tick-rounded purchase price for 25,000 net profit |
| `current_estimated_profit` | Net resale proceeds minus displayed current price; may be negative |
| `status`, `reasons`, `error` | Decision, evidence limitations and per-card failure |

Missing numbers are `null`, not zero. Missing lists are empty. Graph history is
**up to 25 hover samples of the displayed graph window**, deduplicated by label;
it is not a complete time series or completed sales. The default graph window
is whatever FUT.GG displays (currently Week). No axis labels are used as prices.
The momentum period is not assumed to mean 6h, 12h or 24h unless FUT.GG labels it.

## Calculation and statuses

At least five readable completed sales within two hours are needed for a resale
estimate. Take the lower quartile of the newest ten qualifying sales, cap it
at the displayed momentum average and second-cheapest active BIN when available,
then reduce it by 2% and round down to an EA listing-price increment.

```text
net proceeds = floor(conservative sell price × 95 / 100)
max buy price = round down to price increment(net proceeds − 25,000)
current estimated profit = net proceeds − current Lowest BIN
```

For example, a 500,000 sell estimate yields 475,000 after tax and a maximum buy
of 450,000 for 25,000 net profit. A current price of 440,000 estimates 35,000.

- **CHECK LISTING**: Current price meets the maximum buy threshold, at least
  five recent sales exist, the price was updated within 15 minutes, active BINs
  confirm the displayed price (within 2%), and recent sales are not falling.
  Verify the actual listing on your console; this service does not buy anything.
- **NEAR TARGET**: Same evidence checks pass, and current price is above maximum
  buy by at most 5,000 coins or 2% of maximum buy, whichever is larger.
- **WATCH**: Insufficient/stale/conflicting data, falling recent sales, a price
  above the near-target range, or a page failure. Falling means the median of
  the newest three sales is over 3% below the next three qualifying sales.

Missing graph samples are reported in `reasons` but do not override otherwise
adequate recent-sales/listing evidence. Estimates cannot guarantee recovery or
a sale. The service reads public rendered DOM, not private APIs or EA accounts.

## Failures and limits

- HTTP 400: invalid JSON shape, URL or count; 415: non-JSON request.
- HTTP 401: wrong/missing API key when configured; 413: body over 16 KB.
- HTTP 429: scan already running; 504: total scan exceeded 210 seconds.
- HTTP 503: browser startup or whole-scan failure.
- Individual page failures return HTTP 200 with that result set to `WATCH`,
  `error` populated and unavailable fields `null`; other cards still finish.

FUT.GG may return missing data, verification blocks or change its markup. The
scanner does not bypass verification, spoof fingerprints, or use proxies.
Inspect Render logs and update the extraction selectors if FUT.GG changes.
There is no persistent cache, database or scheduled scan. Browser contexts are
closed after every card; the browser closes after every request.

## Local development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
python app.py
```

Or build and run the deployment image:

```bash
docker build -t futgg-five-scan .
docker run --rm -p 10000:10000 --init --shm-size=1g futgg-five-scan
```

## Validation at implementation

The Console selector, Standard/Holographic URLs, labelled Lowest BIN and
Momentum panels, completed sales and auction columns were inspected on FUT.GG.
Local checks cover Flask input/auth/error handling, rounding and status
calculations, and Chromium extraction against representative DOM fixtures.
The development runtime's outbound connection to FUT.GG returned
`ERR_EMPTY_RESPONSE`, so a full live scan from this backend and Render deployment
are **not yet verified**. Deploy and test a known card URL before relying on it.
