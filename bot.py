import asyncio, io, logging, os, re, time
from functools import wraps

from telegram import Update, InputFile
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters,
)

import store, checker, scraper, proxies as px_mod, exporter
from config import BOT_TOKEN, ADMIN_ID, CONCURRENCY, MAX_URLS, PORT, DEFAULT_MAX_PRICE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("bot")

# per-chat runtime state (stop flags, live browser, etc)
RUNTIME = {}
# conversation state per chat
CONV = {}

# ── auth decorator ────────────────────────────────────────────────

def require_auth(fn):
    @wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *a, **kw):
        chat_id = update.effective_chat.id
        store.upsert_user(chat_id, update.effective_user.username or "")
        if store.is_authed(chat_id):
            return await fn(update, ctx, *a, **kw)
        CONV.setdefault(chat_id, {})["awaiting_pin"] = True
        await update.effective_message.reply_text(
            "🔒 PIN required.\n\nSend /login <PIN> or just type your PIN."
        )
    return wrapper

def is_admin(update: Update) -> bool:
    return update.effective_user.id == ADMIN_ID

def admin_only(fn):
    @wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *a, **kw):
        if not is_admin(update):
            await update.effective_message.reply_text("admin only.")
            return
        return await fn(update, ctx, *a, **kw)
    return wrapper

# ── extract text/file helper ──────────────────────────────────────

async def _extract_text(update: Update) -> str:
    m = update.message
    if not m:
        return ""
    target = m.reply_to_message or m
    if target.document:
        doc = target.document
        if doc.file_name and doc.file_name.lower().endswith((".txt", ".csv")):
            f = await doc.get_file()
            bio = io.BytesIO()
            await f.download_to_memory(bio)
            return bio.getvalue().decode("utf-8", "ignore")
    if target.text:
        return target.text
    return ""

async def _send_txt(ctx, chat_id, name, data: bytes, caption=None):
    if not data:
        return
    await ctx.bot.send_document(chat_id, InputFile(data, filename=name), caption=caption)

# ── commands ──────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    store.upsert_user(update.effective_chat.id, update.effective_user.username or "")
    if not store.is_authed(update.effective_chat.id):
        CONV.setdefault(update.effective_chat.id, {})["awaiting_pin"] = True
        await update.message.reply_text(
            "OGGY SITE CHKR 🔒\n\nSend your PIN.\n(/login <PIN> works too.)"
        )
        return
    await _help(update, ctx)

async def cmd_login(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    store.upsert_user(chat_id, update.effective_user.username or "")
    args = ctx.args
    if not args:
        CONV.setdefault(chat_id, {})["awaiting_pin"] = True
        await update.message.reply_text("send PIN.")
        return
    await _try_pin(update, " ".join(args))

async def _try_pin(update: Update, pin: str):
    chat_id = update.effective_chat.id
    if pin.strip() == store.get_pin():
        store.mark_authed(chat_id)
        CONV.get(chat_id, {}).pop("awaiting_pin", None)
        await update.effective_message.reply_text("✅ authed. /help for commands.")
    else:
        await update.effective_message.reply_text("❌ wrong PIN.")

async def _help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    admin = is_admin(update)
    txt = (
        "*OGGY SITE CHKR* — commands\n\n"
        "*Auth*\n"
        "`/login <PIN>` — authenticate\n\n"
        "*Utils*\n"
        "`/cleantxt` — reply to txt/message, extract URLs\n"
        "`/status` — recent jobs\n"
        "`/export <job_id>` — get all txt files for a job\n"
        "`/find <job_id> <lo>-<hi>` — price bucket from a completed job\n\n"
        "*Check (shopify cart/checkout/captcha/price)*\n"
        "`/check` — reply to txt/message with URLs\n"
        "`/stop` — stop current job (partial files still sent)\n\n"
        "*Scrape (google → shopify)*\n"
        "`/scrape <keyword> <max_price> <count>` — one-liner\n"
        "`/scrape` — interactive prompts\n\n"
        "*Proxies*\n"
        "`/px` — reply to txt of proxies (one per line)\n"
        "`/pxcheck` — test all proxies, mark live/dead\n"
        "`/pxlist` — pool summary\n"
        "`/pxexport` — download live proxies as txt\n"
        "`/pxclear` — wipe pool\n"
    )
    if admin:
        txt += (
            "\n*Admin*\n"
            "`/setpin <new>` — change bot PIN\n"
            "`/users` — user list\n"
            "`/revoke <chat_id>` — deauth a user\n"
            "`/stats` — global stats\n"
            "`/broadcast <msg>` — message all authed users\n"
        )
    await update.effective_message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)

# ── /cleantxt ─────────────────────────────────────────────────────

@require_auth
async def cmd_cleantxt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = await _extract_text(update)
    if not raw:
        await update.message.reply_text("reply to a txt or paste URLs.")
        return
    urls = exporter.clean_urls(raw)
    if not urls:
        await update.message.reply_text("no urls found.")
        return
    await _send_txt(ctx, update.effective_chat.id, "cleaned.txt",
                    "\n".join(urls).encode(),
                    caption=f"{len(urls)} unique URLs.")

# ── /check ────────────────────────────────────────────────────────

async def _do_check(chat_id, ctx, urls, label_prefix="job"):
    if not urls:
        await ctx.bot.send_message(chat_id, "no urls.")
        return None
    if len(urls) > MAX_URLS:
        await ctx.bot.send_message(chat_id, f"too many ({len(urls)}), max {MAX_URLS}.")
        return None
    proxies = store.pick_live_proxies(chat_id)
    jid = store.create_job(chat_id, "check",
                           {"proxies": len(proxies), "urls": len(urls)},
                           total=len(urls))
    store.add_urls(jid, urls)
    RUNTIME[jid] = {"stop": False, "chat_id": chat_id}
    asyncio.create_task(_run_check(jid, chat_id, ctx))
    proxy_note = f", {len(proxies)} proxies" if proxies else ", no proxies"
    await ctx.bot.send_message(
        chat_id,
        f"job #{jid} started — {len(urls)} urls{proxy_note}.\n"
        f"/stop to halt."
    )
    return jid

async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    raw = await _extract_text(update)
    if not raw:
        await update.message.reply_text("reply to a txt or paste URLs.")
        return
    urls = exporter.clean_urls(raw)
    await _do_check(chat_id, ctx, urls)

async def _run_check(jid: int, chat_id: int, ctx):
    stop_flag = RUNTIME[jid]
    pending = store.pending_urls(jid)

    pw, browser = await checker.make_browser()
    import aiohttp
    async with aiohttp.ClientSession(
        headers={"User-Agent": "Mozilla/5.0 OggyChkr/1.0"},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as session:
        last = {"t": 0.0}
        async def on_result(url, data):
            store.save_url_result(jid, url, data)
            now = time.time()
            if now - last["t"] > 8:
                last["t"] = now
                j = store.get_job(jid)
                if j:
                    try:
                        await ctx.bot.send_message(
                            chat_id, f"job #{jid}: {j['done']}/{j['total']} done"
                        )
                    except Exception:
                        pass
        try:
            await checker.run_check(
                browser, session, pending, on_result, stop_flag,
                store.pick_live_proxies(chat_id),
            )
        except Exception as e:
            log.exception("check %s failed", jid)
            store.set_job_status(jid, "failed")
            await ctx.bot.send_message(chat_id, f"job #{jid} failed: {str(e)[:120]}")
        finally:
            try:
                await browser.close(); await pw.stop()
            except Exception:
                pass

    j = store.get_job(jid)
    if not j:
        return
    if j["status"] == "running":
        store.set_job_status(jid, "stopped" if stop_flag.get("stop") else "done")
    rows = store.job_urls(jid)
    cat = exporter.categorize(rows)

    await ctx.bot.send_message(
        chat_id,
        f"job #{jid} done.\n"
        f"checked: {len(cat['checked'])} | pending: {len(cat['pending'])} | "
        f"clean: {len(cat['clean'])} | captcha: {len(cat['captcha'])} | priced: {len(cat['priced'])}"
    )
    await _send_txt(ctx, chat_id, f"job_{jid}_checked.txt",
                    exporter.lines_of(cat["checked"]),
                    caption=f"checked ({len(cat['checked'])})")
    await _send_txt(ctx, chat_id, f"job_{jid}_clean.txt",
                    exporter.lines_of(cat["clean"]),
                    caption=f"clean — live+shopify+cart, no captcha ({len(cat['clean'])})")
    await _send_txt(ctx, chat_id, f"job_{jid}_captcha.txt",
                    exporter.lines_of(cat["captcha"]),
                    caption=f"captcha-blocked ({len(cat['captcha'])})")
    await _send_txt(ctx, chat_id, f"job_{jid}_remaining.txt",
                    exporter.lines_of(cat["pending"]),
                    caption=f"remaining / unchecked ({len(cat['pending'])})")
    await _send_txt(ctx, chat_id, f"job_{jid}_results.json",
                    exporter.results_json_bytes(rows),
                    caption="full results.json")

    # if a cat range was pending, auto-export it too
    c = CONV.get(chat_id, {})
    if c.get("cat_range") and c.get("cat_job") == jid:
        lo, hi = c["cat_range"]
        bucket = exporter.bucket_by_price(cat["clean"], lo, hi)
        await _send_txt(ctx, chat_id, f"job_{jid}_clean_{lo}-{hi}.txt",
                        exporter.lines_of(bucket),
                        caption=f"clean sites in ${lo}-${hi} ({len(bucket)})")
        c.pop("cat_range", None); c.pop("cat_job", None)

# ── /stop ─────────────────────────────────────────────────────────

@require_auth
async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    active = [jid for jid, rt in RUNTIME.items()
              if rt.get("chat_id") == chat_id and not rt.get("stop")]
    if not active:
        await update.message.reply_text("no active job.")
        return
    for jid in active:
        RUNTIME[jid]["stop"] = True
    await update.message.reply_text(f"stopping {len(active)} job(s). partial txt will follow.")

# ── /status, /export, /find ───────────────────────────────────────

@require_auth
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    jobs = store.list_jobs(update.effective_chat.id)
    if not jobs:
        await update.message.reply_text("no jobs.")
        return
    lines = []
    for j in jobs:
        lines.append(f"#{j['id']} {j['kind']} {j['status']}  {j['done']}/{j['total']}  hits={j['hits']}")
    await update.message.reply_text("```\n" + "\n".join(lines) + "\n```",
                                    parse_mode=ParseMode.MARKDOWN)

@require_auth
async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("usage: /export <job_id>")
        return
    jid = int(args[0])
    j = store.get_job(jid)
    if not j or j["chat_id"] != update.effective_chat.id:
        await update.message.reply_text("job not found.")
        return
    rows = store.job_urls(jid)
    cat = exporter.categorize(rows)
    chat_id = update.effective_chat.id
    await _send_txt(ctx, chat_id, f"job_{jid}_checked.txt",
                    exporter.lines_of(cat["checked"]), caption="checked")
    await _send_txt(ctx, chat_id, f"job_{jid}_clean.txt",
                    exporter.lines_of(cat["clean"]), caption="clean")
    await _send_txt(ctx, chat_id, f"job_{jid}_captcha.txt",
                    exporter.lines_of(cat["captcha"]), caption="captcha")
    await _send_txt(ctx, chat_id, f"job_{jid}_remaining.txt",
                    exporter.lines_of(cat["pending"]), caption="remaining")
    await _send_txt(ctx, chat_id, f"job_{jid}_results.json",
                    exporter.results_json_bytes(rows), caption="results.json")

@require_auth
async def cmd_find(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text(
            "usage: /find <job_id> <lo>-<hi>  (e.g. /find 12 0-5)\n"
            "or: /find <lo>-<hi>  (uses latest job)"
        )
        return
    if len(args) == 2 and re.fullmatch(r"[0-9.]+-[0-9.]+", args[0]):
        jobs = store.list_jobs(update.effective_chat.id, 1)
        if not jobs:
            await update.message.reply_text("no jobs.")
            return
        jid = jobs[0]["id"]
        rng = args[0]
    else:
        if not args[0].isdigit():
            await update.message.reply_text("job_id must be numeric.")
            return
        jid = int(args[0])
        rng = args[1]

    m = re.fullmatch(r"\$?([0-9.]+)\s*-\s*\$?([0-9.]+)", rng.replace(" to ", "-"))
    if not m:
        await update.message.reply_text("range must be lo-hi, e.g. 0-5")
        return
    lo, hi = float(m.group(1)), float(m.group(2))

    j = store.get_job(jid)
    if not j or j["chat_id"] != update.effective_chat.id:
        await update.message.reply_text("job not found.")
        return

    rows = store.job_urls(jid)
    cat = exporter.categorize(rows)
    bucket_all = exporter.bucket_by_price(cat["priced"], lo, hi)
    bucket_clean = exporter.bucket_by_price(cat["clean"], lo, hi)

    chat_id = update.effective_chat.id
    await _send_txt(ctx, chat_id, f"job_{jid}_${lo}-${hi}_all.txt",
                    exporter.lines_of(bucket_all),
                    caption=f"all priced in ${lo}-${hi} ({len(bucket_all)})")
    await _send_txt(ctx, chat_id, f"job_{jid}_${lo}-${hi}_clean.txt",
                    exporter.lines_of(bucket_clean),
                    caption=f"clean + priced in ${lo}-${hi} ({len(bucket_clean)})")

# ── /scrape ───────────────────────────────────────────────────────

@require_auth
async def cmd_scrape(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = ctx.args
    if len(args) >= 3:
        try:
            kw = args[0]
            max_price = float(args[1].lstrip("$"))
            count = int(args[2])
        except ValueError:
            await update.message.reply_text("usage: /scrape <keyword> <max_price> <count>")
            return
        await _start_scrape(chat_id, ctx, kw, max_price, count)
        return
    CONV.setdefault(chat_id, {})["scrape"] = {"stage": "kw"}
    await update.message.reply_text("keyword? (product category — socks, sticker, pin…)")

async def _start_scrape(chat_id, ctx, kw, max_price, count):
    if count < 1 or count > 500:
        await ctx.bot.send_message(chat_id, "count must be 1-500.")
        return
    proxies = store.pick_live_proxies(chat_id)
    jid = store.create_job(chat_id, "scrape",
                           {"kw": kw, "max_price": max_price, "want": count,
                            "proxies": len(proxies)},
                           total=count)
    RUNTIME[jid] = {"stop": False, "chat_id": chat_id}
    asyncio.create_task(_run_scrape(jid, chat_id, ctx, kw, max_price, count))
    note = f", {len(proxies)} proxies" if proxies else ", no proxies (google will rate-limit)"
    await ctx.bot.send_message(
        chat_id,
        f"scrape #{jid} started — '{kw}', ≤${max_price}, {count} sites{note}.\n/stop to halt."
    )

async def _run_scrape(jid, chat_id, ctx, kw, max_price, count):
    stop_flag = RUNTIME[jid]
    store.set_job_status(jid, "running")

    async def on_hit(row):
        store.save_scrape_hit(jid, row["site"], row["price"], row["title"])
        try:
            await ctx.bot.send_message(
                chat_id, f"+ {row['site']}  ${row['price']:.2f}"
            )
        except Exception:
            pass

    try:
        accepted = await scraper.scrape_shopify(
            keyword=kw, want=count, max_price=max_price,
            proxies=store.pick_live_proxies(chat_id),
            on_hit=on_hit, stop_flag=stop_flag,
        )
        store.set_job_status(jid, "stopped" if stop_flag.get("stop") else "done")
        rows = store.job_urls(jid)
        body = "\n".join(f"{r['url']}\t${r['lowest_price']:.2f}\t{r['lowest_product'] or ''}".strip()
                         for r in rows).encode()
        await _send_txt(ctx, chat_id, f"scrape_{kw.replace(' ','_')}.txt",
                        body, caption=f"{len(rows)} sites under ${max_price}")
        urls_only = "\n".join(r["url"] for r in rows).encode()
        await _send_txt(ctx, chat_id, f"scrape_{kw.replace(' ','_')}_urls.txt",
                        urls_only, caption=f"{len(rows)} URLs only")
    except Exception as e:
        log.exception("scrape %s failed", jid)
        store.set_job_status(jid, "failed")
        await ctx.bot.send_message(chat_id, f"scrape #{jid} failed: {str(e)[:120]}")

# ── proxies ───────────────────────────────────────────────────────

@require_auth
async def cmd_px(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    raw = await _extract_text(update)
    if not raw:
        await update.message.reply_text("reply to a txt of proxies (one per line).")
        return
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    added = store.add_proxies(chat_id, lines)
    await update.message.reply_text(f"added {added} proxies. run /pxcheck to test them.")

@require_auth
async def cmd_pxcheck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = store.list_proxies(chat_id)
    if not rows:
        await update.message.reply_text("pool is empty. /px first.")
        return
    msg = await update.message.reply_text(f"checking {len(rows)} proxies…")
    live, dead = await px_mod.check_pool(rows, concurrency=30)
    await msg.edit_text(
        f"proxy check done.\n✅ live: {live}\n❌ dead: {dead}\n\n"
        f"live proxies will be used for /check and /scrape."
    )

@require_auth
async def cmd_pxlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    all_p = store.list_proxies(chat_id)
    live_p = store.list_proxies(chat_id, live_only=True)
    await update.message.reply_text(
        f"proxies: {len(all_p)} total, {len(live_p)} live."
    )

@require_auth
async def cmd_pxexport(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    live_p = store.list_proxies(chat_id, live_only=True)
    if not live_p:
        await update.message.reply_text("no live proxies.")
        return
    body = "\n".join(p["url"] for p in live_p).encode()
    await _send_txt(ctx, chat_id, "live_proxies.txt", body,
                    caption=f"{len(live_p)} live proxies")

@require_auth
async def cmd_pxclear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    store.clear_proxies(update.effective_chat.id)
    await update.message.reply_text("pool cleared.")

# ── admin ─────────────────────────────────────────────────────────

@admin_only
async def cmd_setpin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("usage: /setpin <new>")
        return
    new = " ".join(ctx.args).strip()
    if len(new) < 4:
        await update.message.reply_text("pin must be 4+ chars.")
        return
    store.set_pin(new)
    await update.message.reply_text("pin updated.")

@admin_only
async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    users = store.list_users()
    if not users:
        await update.message.reply_text("no users.")
        return
    lines = []
    for u in users[:100]:
        flag = "ADMIN" if u["is_admin"] else ("authed" if u["authed_at"] else "locked")
        lines.append(f"{u['chat_id']}  @{u['username'] or '-'}  {flag}")
    await update.message.reply_text("```\n" + "\n".join(lines) + "\n```",
                                    parse_mode=ParseMode.MARKDOWN)

@admin_only
async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or not ctx.args[0].lstrip("-").isdigit():
        await update.message.reply_text("usage: /revoke <chat_id>")
        return
    cid = int(ctx.args[0])
    ok = store.revoke_user(cid)
    await update.message.reply_text("revoked." if ok else "not found.")

@admin_only
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = store.admin_stats()
    await update.message.reply_text(
        f"users: {s['users']} (authed: {s['authed']})\n"
        f"jobs: {s['jobs']}\nurls: {s['urls']}\nlive proxies: {s['live_proxies']}"
    )

@admin_only
async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("usage: /broadcast <message>")
        return
    body = "📢 " + " ".join(ctx.args)
    sent, failed = 0, 0
    for u in store.list_users():
        if not u["authed_at"] and not u["is_admin"]:
            continue
        try:
            await ctx.bot.send_message(u["chat_id"], body)
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"sent: {sent}, failed: {failed}")

# ── free-text handler (PIN / scrape conversation / cat range) ─────

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    store.upsert_user(chat_id, update.effective_user.username or "")
    c = CONV.setdefault(chat_id, {})

    if c.get("awaiting_pin"):
        await _try_pin(update, text)
        return

    if not store.is_authed(chat_id):
        c["awaiting_pin"] = True
        await update.message.reply_text("PIN required. send it.")
        return

    sc = c.get("scrape")
    if sc:
        stage = sc.get("stage")
        if stage == "kw":
            sc["kw"] = text
            sc["stage"] = "max_price"
            await update.message.reply_text("max price ($)?")
            return
        if stage == "max_price":
            try:
                sc["max_price"] = float(text.lstrip("$"))
            except ValueError:
                await update.message.reply_text("number only, e.g. 5")
                return
            sc["stage"] = "count"
            await update.message.reply_text("how many sites?")
            return
        if stage == "count":
            try:
                sc["count"] = int(text)
            except ValueError:
                await update.message.reply_text("integer only.")
                return
            kw = sc["kw"]; mp = sc["max_price"]; n = sc["count"]
            c.pop("scrape", None)
            await _start_scrape(chat_id, ctx, kw, mp, n)
            return

    # /cat interactive range (after job completes we auto-filter)
    # nothing else uses free text — reply help
    await update.message.reply_text("unknown input. /help")

# ── keepalive http server (keeps Render happy) ────────────────────

async def _health_server():
    from aiohttp import web
    async def h(_r):
        return web.Response(text="ok")
    app = web.Application()
    app.router.add_get("/", h)
    app.router.add_get("/healthz", h)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("health server on :%s", PORT)

# ── main ──────────────────────────────────────────────────────────

async def _post_init(app):
    await _health_server()

def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", _help))
    app.add_handler(CommandHandler("login", cmd_login))
    app.add_handler(CommandHandler("cleantxt", cmd_cleantxt))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("scrape", cmd_scrape))
    app.add_handler(CommandHandler("px", cmd_px))
    app.add_handler(CommandHandler("pxcheck", cmd_pxcheck))
    app.add_handler(CommandHandler("pxlist", cmd_pxlist))
    app.add_handler(CommandHandler("pxexport", cmd_pxexport))
    app.add_handler(CommandHandler("pxclear", cmd_pxclear))
    app.add_handler(CommandHandler("setpin", cmd_setpin))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("OGGY SITE CHKR up.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
