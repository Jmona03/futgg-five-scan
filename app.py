"""Read rendered FUT.GG Console card prices; never buy or list cards."""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hmac
import logging
import os
import re
from statistics import median
import threading
from urllib.parse import urlsplit, urlunsplit

from flask import Flask, jsonify, request
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from werkzeug.exceptions import HTTPException

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024
logging.basicConfig(level=logging.INFO)
SCAN_LOCK = threading.Lock()
TARGET_PROFIT = 25_000
PAGE_TIMEOUT_MS = 25_000


def now():
    return datetime.now(timezone.utc).isoformat()


def validate_url(value):
    if not isinstance(value, str) or len(value) > 1000:
        raise ValueError('Each URL must be a FUT.GG card URL string.')
    u = urlsplit(value.strip())
    # Require a specific card, not the player career hub, momentum, or filters.
    if (u.scheme != 'https' or u.netloc.lower() not in ('fut.gg', 'www.fut.gg')
            or not re.fullmatch(r'/players/[A-Za-z0-9-]+/\d{2}-\d+/?', u.path)
            or u.query):
        raise ValueError('Use an HTTPS FUT.GG card URL: /players/<player>/<year>-<item-id>/ (no query).')
    return urlunsplit(('https', 'www.fut.gg', u.path.rstrip('/') + '/', '', ''))


def coins(value):
    """Strict parser: missing/unparseable data stays null, never zero."""
    if value is None:
        return None
    m = re.fullmatch(r'\s*([\d,]+(?:\.\d+)?)\s*([kKmM]?)\s*', str(value))
    if not m:
        return None
    try:
        n = Decimal(m[1].replace(',', '')) * {'': 1, 'k': 1000, 'm': 1000000}[m[2].lower()]
        return int(n) if 0 < n <= 15_000_000 else None
    except (InvalidOperation, ValueError):
        return None


def tick_floor(value):
    n = max(0, int(value))
    step = 50 if n < 1000 else 100 if n < 10000 else 250 if n < 50000 else 500 if n < 100000 else 1000
    return n // step * step


def age_minutes(text):
    if re.search(r'just now|seconds? ago', text, re.I):
        return 0
    m = re.search(r'(\d+)\s*(minute|hour|day)s? ago', text, re.I)
    return int(m[1]) * {'minute': 1, 'hour': 60, 'day': 1440}[m[2].lower()] if m else None


def calculate(data):
    """Use recent sale evidence; momentum extremes alone do not justify a buy."""
    reasons = []
    sales = [s['price'] for s in data['recent_sales']
             if s['price'] and s['age_minutes'] is not None and s['age_minutes'] <= 120]
    bins = sorted(a['bin'] for a in data['active_listings'] if a['bin'])
    current = data['current_price']
    sell = max_buy = profit = None
    falling = len(sales) >= 6 and median(sales[:3]) < median(sales[3:6]) * .97
    if len(sales) < 5:
        reasons.append('Fewer than five completed sales in the last two hours.')
    else:
        # Lower quartile of newest ten sales, with another 2% margin.
        sample = sorted(sales[:10])
        basis = sample[(len(sample) - 1) // 4]
        caps = [basis]
        if data['average']:
            caps.append(data['average'])
        if len(bins) >= 2:
            caps.append(bins[1])  # ignore one isolated undercut for resale cap
        sell = tick_floor(Decimal(min(caps)) * Decimal('.98'))
        net = sell * 95 // 100
        max_buy = tick_floor(net - TARGET_PROFIT)
        profit = net - current if current else None
    if falling:
        reasons.append('Newest completed sales are falling by more than 3%.')
    if not current:
        reasons.append('Current Lowest BIN is unavailable.')
    if not bins:
        reasons.append('No active BIN listing is available to confirm an undercut.')
    age = age_minutes(data.get('price_updated_text') or '')
    if age is None or age > 15:
        reasons.append('Current Lowest BIN freshness is unconfirmed or older than 15 minutes.')
    if current and bins and bins[0] > current * 1.02:
        reasons.append('Displayed Lowest BIN is not confirmed by active listings.')
    if not data['lowest_bin_history']:
        reasons.append('No readable graph history points; history is unavailable.')
    status = 'WATCH'
    # History is informational; sale evidence/freshness/listings are mandatory.
    blockers = [r for r in reasons if not r.startswith('No readable graph')]
    if not blockers and max_buy and current:
        if current <= max_buy:
            status = 'CHECK LISTING'
            reasons.append('Estimated net profit meets target; verify the listing on your console.')
        elif current - max_buy <= max(5000, max_buy * .02):
            status = 'NEAR TARGET'
            reasons.append('Price is within 5,000 coins or 2% of the maximum buy price.')
        else:
            reasons.append('Current price is above the target buy price.')
    return dict(conservative_sell_price=sell, max_buy_price=max_buy,
                current_estimated_profit=profit, status=status, reasons=reasons,
                recent_sales_used=len(sales[:10]), falling_recent_sales=falling)


# Only visible, labelled price sections and table columns are read. Attribute
# stats, price-range limits and graph axis ticks are not treated as market prices.
EXTRACT_JS = r"""() => {
  const visible = e => !!e && e.getClientRects().length > 0;
  const headings = [...document.querySelectorAll('h3')];
  const heading = name => headings.find(e => e.textContent.trim() === name);
  const bin = heading('Lowest BIN')?.parentElement.parentElement;
  const momentum = heading('Price Momentum')?.parentElement.parentElement;
  const text = e => e?.innerText?.trim() || '';
  const labelled = label => {
    if (!momentum) return null;
    const nodes = [...momentum.querySelectorAll('div,span')];
    const e = nodes.find(e => e.firstChild?.nodeType === 3 &&
      e.firstChild.textContent.trim() === label);
    return e ? text(e.querySelector('.tabular-nums') ||
      e.parentElement.querySelector('.tabular-nums')) : null;
  };
  const tables = [...document.querySelectorAll('#prices table')].filter(visible);
  const table = label => tables.find(t => [...t.querySelectorAll('th')]
    .some(th => text(th) === label));
  const rows = t => t ? [...t.querySelectorAll('tbody tr')].map(r =>
    [...r.querySelectorAll('td')].map(text)) : [];
  const group = document.querySelector('[aria-label="Item version"]');
  const selected = group?.querySelector('[aria-pressed="true"], [aria-checked="true"], input:checked');
  const change = momentum?.querySelector('h3')?.parentElement;
  return {
    name: text(document.querySelector('h1')),
    item_version: text(selected) || selected?.getAttribute('aria-label') || null,
    current_price: text(bin?.querySelector('.tabular-nums')),
    price_updated_text: text(heading('Lowest BIN')?.nextElementSibling),
    average: labelled('Average'), low: labelled('Lowest'), high: labelled('Highest'),
    momentum_text: text(change),
    momentum_down: !!change?.querySelector('.lucide-chevron-down'),
    sales: rows(table('Time Sold')), auctions: rows(table('Start Bid')),
    graph_present: !!document.querySelector('#prices .recharts-surface')
  };
}"""


async def history_points(page):
    """Sample hover prices from the visible chart header (or tooltip)."""
    points, seen = [], set()
    graph = page.locator('#prices .recharts-surface').first
    if not await graph.count():
        return points
    await graph.scroll_into_view_if_needed()
    box = await graph.bounding_box()
    if not box:
        return points
    for i in range(25):
        await page.mouse.move(box['x'] + 56 + (box['width'] - 58) * i / 24,
                              box['y'] + box['height'] / 2)
        await page.wait_for_timeout(80)
        header = await page.evaluate("""() => {
          const img = document.querySelector('#prices img[alt="FC Coin"]');
          const parent = img?.parentElement;
          return {price: parent?.querySelector('.tabular-nums')?.textContent,
                  label: parent?.parentElement.querySelector('p')?.innerText};
        }""")
        price = coins(header.get('price'))
        label = header.get('label') or ''
        if price and 'vs current' in label:
            label = label.split('·')[0].strip()
            if label not in seen:
                seen.add(label)
                points.append({'label': label, 'price': price})
            continue
        tooltip = page.locator('#prices .recharts-tooltip-wrapper').first
        if not await tooltip.count() or not await tooltip.is_visible():
            continue
        raw = await tooltip.inner_text()
        # Coin-marked values distinguish the price from date/time text.
        values = await tooltip.locator('img[alt="FC Coin"]').evaluate_all(
            '(imgs) => imgs.map(e => e.parentElement.innerText.trim())')
        parsed = [coins(v) for v in values]
        parsed = [v for v in parsed if v]
        if len(parsed) == 1 and raw not in seen:
            seen.add(raw)
            points.append({'label': raw.splitlines()[0], 'price': parsed[0], 'tooltip_text': raw})
    return points


async def scan_page(browser, url):
    result = dict(url=url, fetched_at=now(), market='Console', name=None,
                  item_version=None, current_price=None, average=None, low=None, high=None,
                  price_updated_text=None, lowest_bin_history=[],
                  history_sampling='up to 25 visible tooltip samples; not a full time series',
                  price_momentum=None, recent_sales=[], active_listings=[], error=None)
    context = await browser.new_context(viewport={'width': 1280, 'height': 1000}, locale='en-GB')
    page = await context.new_page()
    page.set_default_timeout(5000)
    # Block navigation to non-FUT.GG hosts, including redirects (SSRF defence).
    async def guard(route):
        req = route.request
        u = urlsplit(req.url)
        if req.is_navigation_request() and (u.scheme != 'https' or u.hostname not in ('www.fut.gg', 'fut.gg')):
            await route.abort()
        elif req.resource_type in ('media', 'font'):
            await route.abort()
        else:
            await route.continue_()
    await context.route('**/*', guard)
    try:
        response = await page.goto(url, wait_until='domcontentloaded', timeout=PAGE_TIMEOUT_MS)
        body = await page.locator('body').inner_text()
        if re.search(r'verify you are human|checking your browser|just a moment|access denied', body, re.I):
            raise ValueError('FUT.GG returned a verification/access block; no bypass attempted.')
        if response and response.status >= 400:
            raise ValueError(f'FUT.GG returned HTTP {response.status}.')
        await page.get_by_role('heading', name='Lowest BIN', exact=True).wait_for(timeout=15000)
        # Wait for hydration before selecting Console, even if SSR already has prices.
        switch = page.get_by_role('button', name='Switch platform', exact=True)
        await switch.click(timeout=10000)
        await page.get_by_role('menu').get_by_text('Console', exact=True).click()
        await page.wait_for_function(r"""() => {
          const h = [...document.querySelectorAll('h3')].find(e=>e.textContent.trim()==='Lowest BIN');
          return /\d/.test(h?.parentElement.parentElement.querySelector('.tabular-nums')?.textContent || '')
            || document.body.innerText.includes('No price history available.');
        }""", timeout=15000)
        await page.wait_for_timeout(1000)
        # Allow table requests to finish, without networkidle (ads can run forever).
        try:
            await page.wait_for_function("""() => document.querySelector('#prices tbody tr') ||
              document.querySelector('#prices')?.innerText.includes('No sales data available.')""", timeout=8000)
        except PlaywrightTimeout:
            pass
        raw = await page.evaluate(EXTRACT_JS)
        result.update(name=raw['name'], item_version=raw['item_version'],
                      price_updated_text=raw['price_updated_text'])
        for key in ('current_price', 'average', 'low', 'high'):
            result[key] = coins(raw[key])
        percent = re.search(r'([+-]?\d+(?:\.\d+)?)\s*%', raw['momentum_text'])
        change = re.search(r'\(([+-]?[\d,]+)\)', raw['momentum_text'])
        pct = float(percent[1]) if percent else None
        if pct is not None and raw['momentum_down']:
            pct = -abs(pct)
        result['price_momentum'] = {'percent': pct, 'coin_change': int(change[1].replace(',', '')) if change else None,
                                    'source_text': raw['momentum_text'], 'period': 'FUT.GG displayed period'}
        result['recent_sales'] = [{'time': row[0], 'age_minutes': age_minutes(row[0]), 'price': coins(row[1])}
                                  for row in raw['sales'] if len(row) == 2]
        result['active_listings'] = [{'ending': row[0], 'start_bid': coins(row[1]), 'bin': coins(row[2])}
                                     for row in raw['auctions'] if len(row) == 3]
        try:
            result['lowest_bin_history'] = await history_points(page)
        except PlaywrightTimeout:
            pass
        result['resolved_url'] = page.url
    except (ValueError, PlaywrightTimeout) as exc:
        result['error'] = str(exc).splitlines()[0][:300]
    except Exception:
        app.logger.exception('Player scan failed')
        result['error'] = 'Browser or extraction failure; see service logs.'
    finally:
        await context.close()
    result.update(calculate(result))
    if result['error']:
        result['status'] = 'WATCH'
        result['reasons'].insert(0, result['error'])
    return result


async def scan_urls(urls):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
        try:
            # At most two tabs to keep RAM bounded; fresh context per card.
            limit = asyncio.Semaphore(2)
            async def one(url):
                async with limit:
                    return await scan_page(browser, url)
            return await asyncio.wait_for(asyncio.gather(*(one(u) for u in urls)), timeout=210)
        finally:
            await browser.close()


@app.get('/health')
def health():
    return jsonify(status='ok')


@app.post('/api/futgg-scan')
def futgg_scan():
    token = os.environ.get('SCAN_API_KEY')
    if token and not hmac.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + token):
        return jsonify(error='Unauthorized'), 401
    if not request.is_json:
        return jsonify(error='Send Content-Type: application/json.'), 415
    body = request.get_json(silent=True)
    urls = body.get('urls') if isinstance(body, dict) else None
    if not isinstance(urls, list) or not 1 <= len(urls) <= 5:
        return jsonify(error='urls must be an array of 1 to 5 FUT.GG card URLs.'), 400
    try:
        urls = [validate_url(u) for u in urls]
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if not SCAN_LOCK.acquire(blocking=False):
        return jsonify(error='A scan is running. Retry shortly.'), 429
    try:
        results = asyncio.run(scan_urls(urls))
        return jsonify(scanned_at=now(), market='Console', tax_rate=.05,
                       target_net_profit=TARGET_PROFIT, results=results)
    except asyncio.TimeoutError:
        return jsonify(error='Scan exceeded 210 seconds. Try fewer URLs.'), 504
    except Exception:
        app.logger.exception('Scan failed')
        return jsonify(error='Chromium could not complete the scan. See service logs.'), 503
    finally:
        SCAN_LOCK.release()


@app.errorhandler(HTTPException)
def http_error(exc):
    return jsonify(error=exc.description), exc.code


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '10000')))
