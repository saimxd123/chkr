import asyncio, random, re, logging
from urllib.parse import quote_plus, unquote, urlparse
import aiohttp
from bs4 import BeautifulSoup

log = logging.getLogger("scraper")

UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

QUERY_TPL = [
    'site:myshopify.com "{kw}"',
    'inurl:myshopify.com "{kw}"',
    '"{kw}" "myshopify.com"',
    '"powered by shopify" "{kw}"',
    '"{kw}" shopify store',
]

PRICE_RE = re.compile(
    r'(?:[$\u20ac\u00a3\u20b9]|USD|EUR|GBP|INR)\s*([0-9]+(?:[.,][0-9]{1,2})?)',
    re.I,
)

def _headers():
    return {
        "User-Agent": random.choice(UA),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

def _parse_google(html: str):
    urls = []
    for m in re.finditer(r'/url\?q=([^&"]+)', html):
        u = unquote(m.group(1))
        if u.startswith("http"):
            urls.append(u)
    try:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if href.startswith("/url?q="):
                u = unquote(href[7:].split("&")[0])
                if u.startswith("http"):
                    urls.append(u)
            elif href.startswith("http") and "google." not in href and "gstatic" not in href:
                urls.append(href)
    except Exception:
        pass
    return urls

def _root(url: str):
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return None
    if host.endswith(".myshopify.com"):
        parts = host[:-len(".myshopify.com")].split(".")
        if parts:
            return f"https://{parts[-1]}.myshopify.com"
    return None

async def _fetch(session, url, proxy=None):
    try:
        async with session.get(
            url, proxy=proxy, headers=_headers(),
            timeout=aiohttp.ClientTimeout(total=20),
            allow_redirects=True,
        ) as r:
            if r.status == 200:
                return await r.text()
    except Exception as e:
        log.debug("fetch %s: %s", url, e)
    return None

async def _shopify_ok(session, base, proxy=None) -> bool:
    try:
        async with session.get(
            base + "/cart.js", proxy=proxy, headers=_headers(),
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status == 200:
                txt = await r.text()
                return '"items"' in txt or '"token"' in txt
    except Exception:
        pass
    return False

async def _lowest_price(session, base, proxy=None):
    try:
        async with session.get(
            base + "/collections/all?sort_by=price-ascending",
            proxy=proxy, headers=_headers(),
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status != 200:
                return None, None
            html = await r.text()
    except Exception:
        return None, None

    soup = BeautifulSoup(html, "html.parser")
    best = None
    for el in soup.select("[data-price], .price, .product-price, [class*='price']"):
        text = el.get_text(" ", strip=True)
        m = PRICE_RE.search(text)
        if not m:
            continue
        try:
            price = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        if price <= 0:
            continue
        parent = el.find_parent(class_=re.compile("product|card|item", re.I))
        title = (parent.get_text(" ", strip=True) if parent else text)[:100]
        if best is None or price < best[0]:
            best = (price, title)
    return best if best else (None, None)

async def scrape_shopify(
    keyword: str, want: int, max_price: float,
    proxies: list[str], on_hit, stop_flag,
):
    seen = set()
    accepted = []
    def _pick():
        return random.choice(proxies) if proxies else None
    def _stop():
        return bool(stop_flag and stop_flag.get("stop"))
    cap = max(want * 5, 60)

    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        candidates = []
        for tmpl in QUERY_TPL:
            if _stop() or len(candidates) >= cap:
                break
            q = tmpl.format(kw=keyword)
            for start in (0, 10, 20):
                if _stop() or len(candidates) >= cap:
                    break
                url = (
                    f"https://www.google.com/search?q={quote_plus(q)}"
                    f"&start={start}&num=10&hl=en"
                )
                html = await _fetch(session, url, proxy=_pick())
                if not html:
                    continue
                for raw in _parse_google(html):
                    root = _root(raw)
                    if root and root not in seen:
                        seen.add(root)
                        candidates.append(root)
                await asyncio.sleep(random.uniform(1.2, 2.8))

        log.info("[scrape] %s: %d candidates", keyword, len(candidates))

        for host in candidates:
            if _stop() or len(accepted) >= want:
                break
            if not await _shopify_ok(session, host, proxy=_pick()):
                continue
            price, title = await _lowest_price(session, host, proxy=_pick())
            if price is None or price > max_price:
                continue
            row = {"site": host, "price": price, "title": title or ""}
            accepted.append(row)
            await on_hit(row)
            await asyncio.sleep(random.uniform(0.5, 1.2))

    return accepted