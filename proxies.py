import asyncio, time
import aiohttp
from store import set_proxy_result

TEST_URL = "https://api.ipify.org?format=json"

async def _check_one(session, row, timeout=10):
    proxy_url = row["url"]
    started = time.monotonic()
    try:
        async with session.get(
            TEST_URL, proxy=proxy_url,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            if r.status == 200:
                await r.read()
                ms = int((time.monotonic() - started) * 1000)
                set_proxy_result(row["id"], True, ms)
                return True
    except Exception:
        pass
    set_proxy_result(row["id"], False, None)
    return False

async def check_pool(rows: list[dict], concurrency: int = 30):
    """
    Checks all proxies in parallel. Returns (live_count, dead_count).
    """
    if not rows:
        return 0, 0
    sem = asyncio.Semaphore(concurrency)
    async with aiohttp.ClientSession() as session:
        async def worker(row):
            async with sem:
                return await _check_one(session, row)
        results = await asyncio.gather(*(worker(r) for r in rows))
    live = sum(1 for r in results if r)
    return live, len(results) - live
