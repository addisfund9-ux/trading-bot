"""Dagi's trading bot.

Send your Binance Futures screenshots (Position History and/or the PNL calendar) after each day.
The bot logs every trade, replies with a review, and sends a morning brief with the scanner's picks.
"""
import asyncio
import logging
import os
import uuid
from datetime import date, datetime, time, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

import anthropic
from dotenv import load_dotenv
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, MenuButtonWebApp, Update,
                      WebAppInfo)
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import coach
import extract
import importer
import review
import scanner
import store
import tilt
import tradechart
import json
import shutil

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO,
                    handlers=[logging.StreamHandler(),
                              logging.FileHandler(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log"),
                                                  encoding="utf-8")])
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("matplotlib").setLevel(logging.WARNING)
log = logging.getLogger("bot")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().strip('"').strip("'")
if not TOKEN or ":" not in TOKEN:
    log_ = logging.getLogger("bot")
    log_.error("TELEGRAM_BOT_TOKEN is missing or not in the right format (it looks like 7123456789:AAH...). "
               "Open .env in Notepad, paste the token from @BotFather right after TELEGRAM_BOT_TOKEN= and save.")
    raise SystemExit(1)
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
TZ = ZoneInfo(os.getenv("TIMEZONE", "Africa/Addis_Ababa"))
MORNING = os.getenv("MORNING_TIME", "08:00")
EVENING = os.getenv("EVENING_REMINDER", "21:00")
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
MAX_HOLD = float(os.getenv("MAX_HOLD_MIN", "10"))
MAX_LOSSES = int(os.getenv("MAX_LOSSES_PER_COIN", "2"))
COOLDOWN = float(os.getenv("COOLDOWN_MIN", "15"))            # wait this long after any loss
MAX_TRADES = int(os.getenv("MAX_TRADES_PER_DAY", "8"))
LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "5"))       # stop after giving back this much from the day's high
INBOX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inbox")
SCAN_TOP = int(os.getenv("SCAN_TOP", "30"))
SCAN_MIN_VOL = float(os.getenv("SCAN_MIN_VOL_M", "20"))
ALBUM_WAIT = 6  # seconds to wait for the rest of a batch of screenshots

con = store.connect()
# The Claude key is optional: with it, screenshots are read and summaries are written by Claude;
# without it, trades come from Binance export files and summaries are rule-based.
CLAUDE_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
claude = anthropic.Anthropic(api_key=CLAUDE_KEY) if CLAUDE_KEY else None


def today():
    return datetime.now(TZ).date()


def owner_id():
    v = os.getenv("OWNER_CHAT_ID", "").strip() or store.get_setting(con, "owner")
    return int(v) if v else None


def owner_only(fn):
    @wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        oid = owner_id()
        if oid is None or update.effective_chat.id != oid:
            if update.message:
                await update.message.reply_text("This is a private bot.")
            return
        return await fn(update, ctx)
    return wrapper


def parse_hhmm(s):
    h, m = s.split(":")
    return time(int(h), int(m), tzinfo=TZ)


def schedule(app: Application, chat_id: int):
    jq = app.job_queue
    for j in jq.jobs():
        j.schedule_removal()
    jq.run_daily(morning_job, parse_hhmm(MORNING), chat_id=chat_id, name="morning")
    jq.run_repeating(inbox_job, interval=60, first=10, chat_id=chat_id, name="inbox")
    if EVENING:
        jq.run_daily(evening_job, parse_hhmm(EVENING), chat_id=chat_id, name="evening")
    log.info("Scheduled morning brief at %s and reminder at %s (%s)", MORNING, EVENING or "off", TZ)


def webapp_markup():
    if not WEBAPP_URL:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("📈 Open scanner", web_app=WebAppInfo(WEBAPP_URL))]])


async def send(ctx, chat_id, text, **kw):
    for i in range(0, len(text), 3900):  # Telegram's limit is 4096 characters
        await ctx.bot.send_message(chat_id, text[i:i + 3900], parse_mode=ParseMode.HTML,
                                   link_preview_options=LinkPreviewOptions(is_disabled=True), **kw)


def review_for(day: str) -> str:
    pos = store.positions_for(con, day)
    cal = store.daily_for(con, day, day).get(day)
    return review.day_review(day, pos, cal, MAX_HOLD, MAX_LOSSES)


def week_data(n=7):
    end = today()
    days = [(end - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]
    pos = store.positions_for(con, days[0], days[-1])
    by_day = {}
    for p in pos:
        by_day.setdefault(p["day"], []).append(p)
    return review.week_summary(days, store.daily_for(con, days[0], days[-1]), by_day)


# ---------------- commands ----------------

EXPORT_HOWTO = (
    "In Binance: <b>Orders → Futures → Trade History</b> (or Position History) → tap the "
    "<b>export/download</b> icon → pick the date range → export as CSV or Excel. Then send the file here with 📎 → File (a .zip from Binance is fine)."
)

HELP = (
    "<b>How to use me</b>\n"
    + ("• After trading, send screenshots of Binance → Futures → <b>Position History</b> (scroll and send all of today's), "
       "plus the <b>PNL Analysis</b> calendar if you like. Send several at once; overlaps are fine. "
       "Tip: send them as <i>files</i> so the small numbers stay sharp.\n"
       "• Or send Binance's export file (CSV/Excel) — exact and free.\n"
       if CLAUDE_KEY else
       "• After trading, send your Binance <b>export file</b>. " + EXPORT_HOWTO + "\n"
       "• Overlapping files are fine; trades already logged are skipped.\n")
    + f"• Every morning at {MORNING} you get a summary of yesterday, your trades drawn on the chart, "
    "your week and the scanner's picks.\n\n"
    "/report — summary + your trades on the chart (yesterday, or /report 2026-10-24)\n"
    "/chart — today's trades drawn on the price chart\n"
    "/tilt — revenge-trading and overtrading check (today, /tilt 2026-10-24, or /tilt week)\n"
    "/today · /yesterday · /day 2026-10-24 — reviews\n"
    "/week — last 7 days with a chart\n"
    "/scan — run the scanner now\n"
    "/morning — send the morning brief now\n"
    "/undo — remove the last batch you sent\n"
    "/export — all trades as CSV\n"
    "/rules — your trading rules"
)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat.id
    oid = owner_id()
    if oid is None:
        store.set_setting(con, "owner", chat)
        oid = chat
        schedule(ctx.application, chat)
    if chat != oid:
        await update.message.reply_text("This is a private bot.")
        return
    if WEBAPP_URL:
        await ctx.bot.set_chat_menu_button(chat, MenuButtonWebApp("Scanner", WebAppInfo(WEBAPP_URL)))
    await update.message.reply_text("👋 Ready. I'm your trading log and morning brief.\n\n" + HELP,
                                    parse_mode=ParseMode.HTML, reply_markup=webapp_markup())


@owner_only
async def help_cmd(update: Update, ctx):
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)


@owner_only
async def rules_cmd(update: Update, ctx):
    await update.message.reply_text(
        f"<b>Your rules</b>\n1. Close any losing trade still open after {MAX_HOLD:.0f} minutes.\n"
        f"2. Stop trading a coin after {MAX_LOSSES} losses in a row on it.\n"
        f"3. After any loss, wait {COOLDOWN:g} minutes before the next trade.\n"
        f"4. No more than {MAX_TRADES} trades a day.\n"
        f"5. Stop for the day after giving back {LOSS_LIMIT:g} USDT from the day's high.\n"
        "6. Never raise leverage after a loss.\n\n"
        "Change them in the .env file (MAX_HOLD_MIN, MAX_LOSSES_PER_COIN, COOLDOWN_MIN, MAX_TRADES_PER_DAY, "
        "DAILY_LOSS_LIMIT).", parse_mode=ParseMode.HTML)


@owner_only
async def today_cmd(update: Update, ctx):
    await send(ctx, update.effective_chat.id, review_for(today().isoformat()))


@owner_only
async def yesterday_cmd(update: Update, ctx):
    await send(ctx, update.effective_chat.id, review_for((today() - timedelta(days=1)).isoformat()))


@owner_only
async def day_cmd(update: Update, ctx):
    try:
        d = date.fromisoformat(ctx.args[0]).isoformat()
    except (IndexError, ValueError):
        await update.message.reply_text("Use it like: /day 2026-10-24")
        return
    await send(ctx, update.effective_chat.id, review_for(d))


@owner_only
async def week_cmd(update: Update, ctx):
    rows, total, s = week_data()
    png = await asyncio.to_thread(review.week_chart, rows)
    await ctx.bot.send_photo(update.effective_chat.id, png, caption=review.week_text(rows, total, s)[:1024],
                             parse_mode=ParseMode.HTML)


def tilt_for(pos):
    return tilt.analyse(pos, COOLDOWN, MAX_TRADES, LOSS_LIMIT)


async def send_tilt(ctx, chat_id, day, pos, with_chart=True):
    a = tilt_for(pos)
    if with_chart:
        try:
            png = await asyncio.to_thread(tilt.chart, day, a)
            if png:
                await ctx.bot.send_photo(chat_id, png, caption=f"Your day on one line — {day}")
        except Exception:
            log.exception("tilt chart failed")
    await send(ctx, chat_id, tilt.report(day, a))


@owner_only
async def tilt_cmd(update: Update, ctx):
    chat = update.effective_chat.id
    if ctx.args and ctx.args[0].lower().startswith("week"):
        end = today()
        days = [(end - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
        data = [(d, tilt_for(store.positions_for(con, d))) for d in days]
        await send(ctx, chat, tilt.week_report(data))
        return
    try:
        d = date.fromisoformat(ctx.args[0]).isoformat() if ctx.args else today().isoformat()
    except ValueError:
        await update.message.reply_text("Use it like: /tilt, /tilt 2026-10-24 or /tilt week")
        return
    pos = store.positions_for(con, d)
    if not pos and not ctx.args:
        d = (today() - timedelta(days=1)).isoformat(); pos = store.positions_for(con, d)
    if not pos:
        await update.message.reply_text(f"No trades logged for {d}.")
        return
    await send_tilt(ctx, chat, d, pos)


async def send_report(ctx, chat_id, day: str, header: str = ""):
    """Claude's written summary, the trade charts, then the numbers. Used by /report and the morning brief."""
    pos = store.positions_for(con, day)
    cal = store.daily_for(con, day, day).get(day)
    text = None
    if pos:
        try:
            text = await asyncio.to_thread(coach.summary, claude, MODEL, day, pos, cal, MAX_HOLD, MAX_LOSSES)
        except Exception:
            log.exception("summary failed")
    opener = header + (f"<b>📝 {day}</b>\n{text}" if text else "")
    if opener.strip():
        await send(ctx, chat_id, opener)
    if pos:
        try:
            charts = await asyncio.to_thread(tradechart.charts_for_day, pos, TZ)
        except Exception:
            log.exception("chart failed")
            charts = []
        for sym, png in charts:
            await ctx.bot.send_photo(chat_id, png, caption=f"{sym.replace('USDT', '')} — your trades on {day}")
    await send(ctx, chat_id, review_for(day))
    if pos:
        await send_tilt(ctx, chat_id, day, pos, with_chart=True)


@owner_only
async def report_cmd(update: Update, ctx):
    if ctx.args:
        try:
            d = date.fromisoformat(ctx.args[0]).isoformat()
        except ValueError:
            await update.message.reply_text("Use it like: /report or /report 2026-10-24")
            return
    else:
        d = (today() - timedelta(days=1)).isoformat()
        if not store.positions_for(con, d) and store.positions_for(con, today().isoformat()):
            d = today().isoformat()   # nothing yesterday but trades today: show today
    await send_report(ctx, update.effective_chat.id, d)


@owner_only
async def chart_cmd(update: Update, ctx):
    try:
        d = date.fromisoformat(ctx.args[0]).isoformat() if ctx.args else today().isoformat()
    except ValueError:
        await update.message.reply_text("Use it like: /chart or /chart 2026-10-24")
        return
    pos = store.positions_for(con, d)
    if not pos:
        await update.message.reply_text(f"No trades logged for {d}.")
        return
    charts = await asyncio.to_thread(tradechart.charts_for_day, pos, TZ)
    if not charts:
        await update.message.reply_text("Couldn't draw the chart (no trade times visible, or Binance unreachable).")
    for sym, png in charts:
        await ctx.bot.send_photo(update.effective_chat.id, png, caption=f"{sym.replace('USDT', '')} — {d}")


async def run_scan_text():
    res = await asyncio.to_thread(scanner.scan, SCAN_TOP, SCAN_MIN_VOL, True)
    return scanner.scan_text(res)


@owner_only
async def scan_cmd(update: Update, ctx):
    msg = await update.message.reply_text("Scanning Binance futures… (about a minute)")
    try:
        text = await run_scan_text()
    except Exception as e:
        log.exception("scan failed")
        await msg.edit_text(f"Scan failed: {e}")
        return
    await msg.delete()
    await send(ctx, update.effective_chat.id, text, reply_markup=webapp_markup())


@owner_only
async def morning_cmd(update: Update, ctx):
    await morning_brief(ctx, update.effective_chat.id)


@owner_only
async def undo_cmd(update: Update, ctx):
    batch = store.get_setting(con, "last_batch")
    if not batch:
        await update.message.reply_text("Nothing to undo.")
        return
    n = store.undo_batch(con, batch)
    store.set_setting(con, "last_batch", "")
    await update.message.reply_text(f"Removed {n} entries from the last batch you sent.")


@owner_only
async def export_cmd(update: Update, ctx):
    pos = store.positions_for(con, "0000-01-01", "9999-12-31")
    if not pos:
        await update.message.reply_text("No trades logged yet.")
        return
    await ctx.bot.send_document(update.effective_chat.id, review.to_csv(pos),
                                filename=f"trades-{today().isoformat()}.csv", caption=f"{len(pos)} trades")


# ---------------- export files ----------------

@owner_only
async def on_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.message
    name = m.document.file_name or "export.csv"
    f = await m.document.get_file()
    data = bytes(await f.download_as_bytearray())
    log.info("Received file %s (%d bytes)", name, len(data))
    try:
        kinds, positions, notes, files = await asyncio.to_thread(importer.parse_any, data, name, TZ)
    except Exception as e:
        log.exception("import failed")
        await m.reply_text(f"I couldn't open that file ({e}). Send the CSV, Excel or zip export from Binance.")
        return
    note = "\n".join(notes)
    if not files:
        await m.reply_text("I couldn't find a CSV or Excel file in that. Send the zip or CSV that Binance gave you.")
        return
    if not kinds:
        log.warning("Unrecognised export %s: %s", files, note)
        await m.reply_text("I don't recognise this file's columns, so nothing was logged.\n" + note +
                           "\n\nSend this message to Claude and the importer can be adjusted.")
        return
    kind = "positions" if kinds == {"positions"} else "trades" if kinds == {"trades"} else "mixed"
    batch = uuid.uuid4().hex[:8]
    counts = {"new": 0, "updated": 0, "dup": 0}
    days = set()
    for p in positions:
        counts[store.upsert_position(con, p, batch)] += 1
        days.add(p["day"])
    con.commit()
    store.set_setting(con, "last_batch", batch)
    label = {"positions": "Position History", "trades": "Trade History"}.get(kind, "Binance")
    log.info("Imported %s: %s", files, counts)
    head = (f"✅ {label} file: logged <b>{counts['new']}</b> new trade(s)"
            + (f", updated {counts['updated']}" if counts["updated"] else "")
            + (f", skipped {counts['dup']} already logged" if counts["dup"] else "") + "."
            + (f"\n{note}" if note else "")
            + (f"\nDays covered: {min(days)} → {max(days)}" if days else ""))
    await m.reply_text(head, parse_mode=ParseMode.HTML)
    for d in sorted(days)[-2:]:
        await send(ctx, m.chat_id, review_for(d))
    if days:
        last_day = max(days)
        await send_tilt(ctx, m.chat_id, last_day, store.positions_for(con, last_day), with_chart=False)


# ---------------- screenshots ----------------

@owner_only
async def on_image(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.message
    if claude is None:
        await m.reply_text("Reading screenshots needs a Claude API key, which isn't set up. "
                           "Send Binance's export file instead.\n\n" + EXPORT_HOWTO.replace("<b>", "").replace("</b>", ""))
        return
    f = await (m.photo[-1].get_file() if m.photo else m.document.get_file())
    data = bytes(await f.download_as_bytearray())
    pending = ctx.chat_data.setdefault("pending", [])
    pending.append(data)
    for j in ctx.job_queue.get_jobs_by_name(f"flush-{m.chat_id}"):
        j.schedule_removal()
    ctx.job_queue.run_once(flush_job, ALBUM_WAIT, chat_id=m.chat_id, name=f"flush-{m.chat_id}")
    if len(pending) == 1:
        await m.reply_text("📥 Got it — send the rest, I'll read them all together.")


async def flush_job(ctx: ContextTypes.DEFAULT_TYPE):
    chat = ctx.job.chat_id
    images = ctx.application.chat_data[chat].pop("pending", [])
    if not images:
        return
    status = await ctx.bot.send_message(chat, f"🔍 Reading {len(images)} screenshot(s)…")
    t = today()
    batch = uuid.uuid4().hex[:8]
    counts = {"new": 0, "updated": 0, "dup": 0}
    days_touched, daily_rows, failed = set(), 0, 0
    for img in images:
        try:
            raw = await asyncio.to_thread(extract.read_screenshot, claude, MODEL, img, t)
        except Exception as e:
            log.exception("extraction failed")
            failed += 1
            last_err = str(e)
            continue
        for p in extract.clean_positions(raw["positions"], t):
            counts[store.upsert_position(con, p, batch)] += 1
            days_touched.add(p["day"])
        for d, v in extract.clean_daily(raw["daily"]):
            store.upsert_daily(con, d, v, batch)
            daily_rows += 1
    con.commit()
    store.set_setting(con, "last_batch", batch)
    head = (f"✅ Logged <b>{counts['new']}</b> new trade(s)"
            + (f", updated {counts['updated']}" if counts["updated"] else "")
            + (f", skipped {counts['dup']} already logged" if counts["dup"] else "")
            + (f", {daily_rows} calendar day(s)" if daily_rows else "") + ".")
    if failed:
        head += f"\n⚠️ {failed} screenshot(s) couldn't be read ({last_err[:120]})."
    if not counts["new"] and not counts["updated"] and not daily_rows and not failed:
        head = (f"All {counts['dup']} trade(s) in those were already logged." if counts["dup"] else
                "I couldn't find any positions in those. Send Binance → Futures → Position History "
                "or the PNL Analysis calendar.")
    await status.edit_text(head, parse_mode=ParseMode.HTML)
    for d in sorted(days_touched)[-3:]:
        await send(ctx, chat, review_for(d))
    if days_touched:
        last_day = max(days_touched)
        await send_tilt(ctx, chat, last_day, store.positions_for(con, last_day), with_chart=False)


# ---------------- scheduled ----------------

async def morning_brief(ctx, chat_id):
    y = (today() - timedelta(days=1)).isoformat()
    rows, total, _ = week_data()
    await send_report(ctx, chat_id, y, header=f"☀️ <b>Good morning, Dagi</b> — {datetime.now(TZ):%A %d %B}\n\n")
    parts = [f"<b>Last 7 days:</b> {review.money(total)} USDT",
             f"<b>Today's rules:</b> close losers after {MAX_HOLD:g} min · stop a coin after {MAX_LOSSES} losses in a row", ""]
    try:
        parts.append(await run_scan_text())
    except Exception as e:
        log.exception("scan failed")
        parts.append(f"Scanner couldn't reach Binance ({e}). Try /scan later.")
    await send(ctx, chat_id, "\n".join(parts), reply_markup=webapp_markup())


async def morning_job(ctx: ContextTypes.DEFAULT_TYPE):
    await morning_brief(ctx, ctx.job.chat_id)


async def evening_job(ctx: ContextTypes.DEFAULT_TYPE):
    d = today().isoformat()
    if store.positions_for(con, d) or store.daily_for(con, d, d):
        return
    what = "Position History screenshots" if claude else "Binance Trade History export file"
    await ctx.bot.send_message(ctx.job.chat_id, f"🌙 Done trading for today? Send your {what} "
                                                "and I'll have your review ready for the morning.")


async def inbox_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Trades Claude read from your screenshots arrive as JSON files in the inbox folder. Import them, then
    move each file to inbox/done so it is never imported twice."""
    os.makedirs(os.path.join(INBOX, "done"), exist_ok=True)
    files = sorted(f for f in os.listdir(INBOX) if f.lower().endswith(".json"))
    for name in files:
        path = os.path.join(INBOX, name)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            items = data.get("positions", []) if isinstance(data, dict) else data
            daily = data.get("daily", []) if isinstance(data, dict) else []
            batch = "inbox-" + uuid.uuid4().hex[:6]
            counts, days = {"new": 0, "updated": 0, "dup": 0}, set()
            for p in items:
                p.setdefault("sort_ts", p.get("opened_at") or p.get("closed_at"))
                p.setdefault("day", (p.get("sort_ts") or "")[:10])
                if not p["day"]:
                    continue
                counts[store.upsert_position(con, p, batch)] += 1
                days.add(p["day"])
            for d in daily:
                store.upsert_daily(con, d["date"], float(d["pnl"]), batch)
            for d in (data.get("clear_daily", []) if isinstance(data, dict) else []):
                con.execute("DELETE FROM daily WHERE day=?", (d,))   # an outdated calendar total for that day
            con.commit()
            store.set_setting(con, "last_batch", batch)
            shutil.move(path, os.path.join(INBOX, "done", name))
            log.info("Inbox %s: %s", name, counts)
            await ctx.bot.send_message(ctx.job.chat_id,
                f"📥 Claude logged <b>{counts['new']}</b> trade(s) from your screenshots"
                + (f", skipped {counts['dup']} already logged" if counts["dup"] else "") + ".",
                parse_mode=ParseMode.HTML)
            for d in sorted(days)[-2:]:
                await send(ctx, ctx.job.chat_id, review_for(d))
            if days:
                await send_tilt(ctx, ctx.job.chat_id, max(days), store.positions_for(con, max(days)))
        except Exception:
            log.exception("inbox import failed for %s", name)
            try:
                shutil.move(path, os.path.join(INBOX, "done", name + ".failed"))
            except Exception:
                pass


async def post_init(app: Application):
    oid = owner_id()
    if oid:
        schedule(app, oid)
    else:
        log.info("No owner yet — open the bot in Telegram and send /start")


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Error while handling an update: %s", ctx.error, exc_info=ctx.error)


def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", start))
    for name, fn in [("help", help_cmd), ("rules", rules_cmd), ("today", today_cmd), ("yesterday", yesterday_cmd),
                     ("day", day_cmd), ("week", week_cmd), ("scan", scan_cmd), ("morning", morning_cmd),
                     ("report", report_cmd), ("chart", chart_cmd), ("tilt", tilt_cmd),
                     ("undo", undo_cmd), ("export", export_cmd)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_image))
    # every other file (csv, xlsx, zip, txt, or a file with an odd/missing extension) goes to the importer
    app.add_handler(MessageHandler(filters.Document.ALL, on_export))
    log.info("Bot running%s. Press Ctrl+C to stop.",
             "" if claude else " without a Claude key (export files + rule-based summaries)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
