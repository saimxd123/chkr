import re, json

def clean_urls(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.search(r'(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s]*)?', line, re.I)
        if m:
            u = m.group(0)
            if not u.startswith(("http://", "https://")):
                u = "https://" + u
            out.append(u)
    seen, dedup = set(), []
    for u in out:
        k = u.lower().rstrip("/")
        if k not in seen:
            seen.add(k)
            dedup.append(u)
    return dedup

def lines_of(rows) -> bytes:
    return ("\n".join(r["url"] for r in rows)).encode() if rows else b""

def bucket_by_price(rows, lo: float, hi: float):
    return [r for r in rows
            if r.get("lowest_price") is not None and lo <= r["lowest_price"] <= hi]

def categorize(rows):
    checked = [r for r in rows if r["status"] == "checked"]
    pending = [r for r in rows if r["status"] == "pending"]
    clean   = [r for r in checked
               if r["live"] and r["shopify"] and r["has_cart"] and not r["has_captcha"]]
    captcha = [r for r in checked if r["has_captcha"]]
    priced  = [r for r in checked if r["lowest_price"] is not None]
    return {
        "checked":   checked,
        "pending":   pending,
        "clean":     clean,
        "captcha":   captcha,
        "priced":    priced,
    }

def results_json_bytes(rows) -> bytes:
    return json.dumps(rows, indent=2, default=str).encode()
