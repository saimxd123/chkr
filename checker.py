import asyncio, re, logging
from urllib.parse import urlparse, urljoin
import aiohttp
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from config import (
    SHOPIFY_SIGS, CAPTCHA_SIGS, CART_PATHS,
    NAV_TIMEOUT_MS, CONCURRENCY, HTTP_TIMEOUT,
)

log = logging.getLogger("checker")
PRICE_RE = re.compile(
    r'(?:[$\u20ac\u00a3\u20b9]|USD|EUR|GBP|INR)\s*([0-9]+(?:[.,][0-9]{1,2})?)',
    re.I,
)

async def _alive(session, url, proxy=None):
    try:
        async with session.get(
            url, proxy=proxy, allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
        ) as r:
            return r.status, str(r.url)
    except Exception as e:
        return None, str(e)

async def check_one(session, browser, url, proxies):
    out = {"live": 0, "shopify": 0, "has_cart": 0, "has_checkout": 0,
           "has_captcha": 0, "lowest_price": None, "lowest_product": None, "note": ""}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    proxy = None
    if proxies:
        import random
        proxy = random.choice(proxies)

    status, final = await _alive(session, url, proxy=proxy)
    if status is None:
        out["note"] = f"unreachable: {final[:120]}"
        return out
    out["live"] = 1 if status < 400 else 0
    if not out["live"]:
        out["note"] = f"http {status}"
        return out
    out["note"] = f"http {status}"

    p = urlparse(final)
    origin = f"{p.scheme}://{p.netloc}"

    try:
        async with session.get(f"{origin}/cart.js", proxy=proxy,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                txt = await r.text()
                if '"items"' in txt or '"token"' in txt:
                    out["shopify"] = 1
                    out["has_cart"] = 1
    except Exception:
        pass

    page = await browser.new_page(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        viewport={"width": 1366, "height": 900},
    )
    try:
        await page.goto(final, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
        html = await page.content()
        if not out["shopify"]:
            low = html.lower()
            out["shopify"] = 1 if any(s.lower() in low for s in SHOPIFY_SIGS) else 0

        if not out["has_cart"]:
            for path in CART_PATHS:
                try:
                    resp = await page.goto(urljoin(origin, path),
                                           timeout=NAV_TIMEOUT_MS,
                                           wait_until="domcontentloaded")
                    if resp and resp.status == 200:
                        out["has_cart"] = 1
                        break
                except Exception:
                    continue
            try:
                await page.goto(final, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            except Exception:
                pass

        try:
            resp = await page.goto(urljoin(origin, "/checkout"),
                                   timeout=NAV_TIMEOUT_MS,
                                   wait_until="domcontentloaded")
            if resp and resp.status == 200:
                out["has_checkout"] = 1
                chk = (await page.content()).lower()
                out["has_captcha"] = 1 if any(s.lower() in chk for s in CAPTCHA_SIGS) else 0
        except Exception:
            pass

        try:
            await page.goto(urljoin(origin, "/collections/all?sort_by=price-ascending"),
                            timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            soup = BeautifulSoup(await page.content(), "html.parser")
            best = None
            for el in soup.select("[data-price], .price, .product-price, [class*='price']"):
                text = el.get_text(" ", strip=True)
                m = PRICE_RE.search(text)
                if not m:
                    continue
                try:
                    p = float(m.group(1).replace(",", "."))
                except ValueError:
                    continue
                if p <= 0:
                    continue
                parent = el.find_parent(class_=re.compile("product|card|item", re.I))
                title = (parent.get_text(" ", strip=True) if parent else text)[:100]
                if best is None or p < best[0]:
                    best = (p, title)
            if best:
                out["lowest_price"] = best[0]
                out["lowest_product"] = best[1]
        except Exception as e:
            out["note"] += f" | price_err:{str(e)[:40]}"
    except PWTimeout:
        out["note"] += " | browser_timeout"
    except Exception as e:
        out["note"] += f" | err:{str(e)[:80]}"
    finally:
        await page.close()

    return out

async def run_check(browser, session, rows, on_result, stop_flag, proxies):
    sem = asyncio.Semaphore(CONCURRENCY)
    async def worker(row):
        async with sem:
            if stop_flag.get("stop"):
                return
            try:
                data = await check_one(session, browser, row["url"], proxies)
            except Exception as e:
                data = {"live": 0, "shopify": 0, "has_cart": 0, "has_checkout": 0,
                        "has_captcha": 0, "lowest_price": None, "lowest_product": None,
                        "note": f"fatal:{str(e)[:80]}"}
            await on_result(row["url"], data)
    await asyncio.gather(*(worker(r) for r in rows))

async def make_browser():
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    return pw, browser
