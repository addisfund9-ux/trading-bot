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

import backtest
import coach
import guard
import hours
import lessons
import pump
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
logging.getLogger("apscheduler").setLevel(logging.WARNING)
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
# 27 Oct: the scanner page moved from Netlify (out of free build credits) to GitHub Pages, which has no limit
if "datradingbot.netlify.app" in WEBAPP_URL:
    WEBAPP_URL = "https://addisfund9-ux.github.io/trading-bot/"
MAX_HOLD = float(os.getenv("MAX_HOLD_MIN", "10"))
MAX_LOSSES = int(os.getenv("MAX_LOSSES_PER_COIN", "2"))
COOLDOWN = float(os.getenv("COOLDOWN_MIN", "15"))            # wait this long after any loss
# your plan from 27 Oct: at most 3 trades a day, mornings only (PLAN_MAX_TRADES / LAST_ENTRY_TIME override)
MAX_TRADES = min(int(os.getenv("MAX_TRADES_PER_DAY", "3")), int(os.getenv("PLAN_MAX_TRADES", "3")))
LAST_ENTRY = os.getenv("LAST_ENTRY_TIME", "12:00").strip() or None
# your own words, 27 Oct: trading is practice while you look for work
FOCUS = os.getenv("FOCUS_NOTE", "🎯 <b>Job search comes first.</b> Trading is practice for now: tiny size, "
                  "at most 3 trades, mornings only, a stop on every one. You trade best when you're "
                  "focused on finding work.").strip()
LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "5"))       # stop after giving back this much from the day's high
IDEAS_URL = os.getenv("IDEAS_URL", (WEBAPP_URL.rstrip("/") + "/ideas.json") if WEBAPP_URL else "").strip()
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


_WARNED = set()


async def stranger(update: Update, ctx):
    """Someone who isn't you: say nothing to them, and tell you once per person."""
    u = update.effective_user
    uid = u.id if u else update.effective_chat.id
    log.warning("Ignored a message from a stranger (id %s, @%s)", uid, getattr(u, "username", None))
    oid = owner_id()
    if oid and uid != oid and uid not in _WARNED:
        _WARNED.add(uid)
        who = (f"@{u.username}" if u and u.username else (u.full_name if u else "someone")) + f" (id {uid})"
        try:
            await ctx.bot.send_message(oid, f"🔒 {who} tried to use your bot. They were ignored and saw nothing.")
        except Exception:
            pass


def owner_only(fn):
    @wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        oid = owner_id()
        user = update.effective_user
        # must be your own private chat with the bot, and you as the sender (blocks groups and forwarded access)
        if oid is None or update.effective_chat.id != oid or (user and user.id != oid):
            await stranger(update, ctx)
            return
        return await fn(update, ctx)
    return wrapper


def parse_hhmm(s):
    h, m = s.split(":")
    return time(int(h), int(m), tzinfo=TZ)


def schedule(app: Application, chat_id: int):
    jq = app.job_queue
    for j in jq.jobs():
        if j.name != "reload":                       # keep the self-update check running
            j.schedule_removal()
    jq.run_daily(morning_job, parse_hhmm(MORNING), chat_id=chat_id, name="morning")
    jq.run_repeating(inbox_job, interval=60, first=10, chat_id=chat_id, name="inbox")
    jq.run_repeating(busy_job, interval=3600, first=max(5, (45 - datetime.now(TZ).minute) % 60 * 60), chat_id=chat_id, name="busy")
    if LIVE:
        jq.run_repeating(guard_job, interval=20, first=15, chat_id=chat_id, name="guard")
    if LAST_ENTRY:
        jq.run_daily(window_closed_job, parse_hhmm(LAST_ENTRY), chat_id=chat_id, name="window")
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
    "/ideas — today's research: new ideas for the bot and your trading\n"
    "/hours — when the market is busy vs when you make money, as a chart\n"
    "/lessons — your mistakes ranked by what they cost, a rule for today, your clean-day streak\n"
    "/rings — today's trading rings (calm · wins · rules), or /rings 2026-10-24\n"
    "/tilt — revenge-trading and overtrading check (today, /tilt 2026-10-24, or /tilt week)\n"
    "/today · /yesterday · /day 2026-10-24 — reviews\n"
    "/week — last 7 days with a chart\n"
    "/scan — run the scanner now\n"
    "/pumptest — test the pump trigger (30m/1h/4h direction + 1m/5m/15m EMA 9/21) on 14 days of 1-minute data\n"
    "/backtest — would the scanner's Clean signals have made money? (last 28 days, ~10 min; /backtest 14)\n"
    "/morning — send the morning brief now\n"
    "/guard — live tilt guard status (warns you while you trade; needs a read-only Binance key)\n"
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
    if chat != oid or (update.effective_user and update.effective_user.id != oid):
        await stranger(update, ctx)
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
        (FOCUS + "\n\n" if FOCUS else "") +
        f"<b>Your rules</b>\n1. Close any losing trade still open after {MAX_HOLD:.0f} minutes.\n"
        f"2. Stop trading a coin after {MAX_LOSSES} losses in a row on it.\n"
        f"3. After any loss, wait {COOLDOWN:g} minutes before the next trade.\n"
        f"4. No more than {MAX_TRADES} trades a day" + (f", and no new trades after {LAST_ENTRY}" if LAST_ENTRY else "") + ".\n"
        f"5. Stop for the day after giving back {LOSS_LIMIT:g} USDT from the day's high.\n"
        "6. Never raise leverage after a loss.\n"
        "7. The first broken rule ends the day.\n\n"
        "Change them in the .env file (MAX_HOLD_MIN, MAX_LOSSES_PER_COIN, COOLDOWN_MIN, PLAN_MAX_TRADES, "
        "LAST_ENTRY_TIME, DAILY_LOSS_LIMIT).", parse_mode=ParseMode.HTML)


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


BT_RUNNING = {"on": False}
BINANCE_KEY, BINANCE_SECRET = os.getenv("BINANCE_API_KEY", "").strip(), os.getenv("BINANCE_API_SECRET", "").strip()
LIVE = guard.Live(BINANCE_KEY, BINANCE_SECRET, cooldown_min=COOLDOWN, max_trades=MAX_TRADES,
                  loss_limit=LOSS_LIMIT, max_hold_min=MAX_HOLD, last_entry=LAST_ENTRY) if BINANCE_KEY and BINANCE_SECRET else None
GUARD_STATE = {"told_bad_key": False, "errors": 0}


async def guard_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Live tilt guard: every 20 seconds, look at the Binance account (read-only) and warn on rule breaks."""
    if not LIVE:
        return
    try:
        alerts = await asyncio.to_thread(LIVE.poll)
        GUARD_STATE["errors"] = 0
    except Exception as e:
        GUARD_STATE["errors"] += 1
        if GUARD_STATE["errors"] in (1, 30):
            log.warning("guard: %s", e)
        return
    if LIVE.ok is False and not GUARD_STATE["told_bad_key"]:
        GUARD_STATE["told_bad_key"] = True
        await ctx.bot.send_message(ctx.job.chat_id, "🛡 Tilt guard is OFF: " + LIVE.reason)
    for a in alerts:
        await ctx.bot.send_message(ctx.job.chat_id, "🛡 " + a, parse_mode=ParseMode.HTML)


@owner_only
async def guard_cmd(update: Update, ctx):
    if not LIVE:
        await update.message.reply_text(
            "🛡 The live tilt guard isn't set up yet. It needs a READ-ONLY Binance API key:\n"
            "1. Binance → Profile → API Management → Create API → System generated.\n"
            "2. Leave ONLY “Enable Reading” ticked (no trading, no withdrawals). Restrict access to your IP if you can.\n"
            "3. Paste the key and secret into .env after BINANCE_API_KEY= and BINANCE_API_SECRET=, then restart start.bat.\n"
            "Never send the key to anyone, including in this chat.")
        return
    ok, reason = await asyncio.to_thread(LIVE.verify)
    g = LIVE.guard
    await update.message.reply_text(
        ("🛡 Tilt guard is ON — checking every 20 seconds.\n" if ok else f"🛡 Tilt guard is OFF: {reason}\n")
        + (f"Open positions: {', '.join(k[0].replace('USDT', '') + ' ' + k[1].lower() for k in g.open) or 'none'}\n"
           f"Positions opened today: {g.opens_today}" if ok else ""))


async def run_backtest(ctx, chat_id, days=28):
    if BT_RUNNING["on"]:
        await ctx.bot.send_message(chat_id, "A backtest is already running — the result will arrive here.")
        return
    BT_RUNNING["on"] = True
    try:
        await ctx.bot.send_message(chat_id, f"🧪 Backtesting the scanner over the last {days} days. This pulls a lot of "
                                            "Binance data, so it takes about 10 minutes. Keep the PC awake; the result arrives here.")
        res = await asyncio.to_thread(backtest.run, days, lambda m: log.info("backtest: %s", m))
        try:
            png = await asyncio.to_thread(backtest.chart, res)
            await ctx.bot.send_photo(chat_id, open(png, "rb"))
        except Exception:
            log.exception("backtest chart failed")
        await send(ctx, chat_id, backtest.text(res))
        log.info("backtest done: %s", {k: res[k] for k in ("new", "old")})
    except Exception as e:
        log.exception("backtest failed")
        await ctx.bot.send_message(chat_id, f"Backtest failed: {e}")
    finally:
        BT_RUNNING["on"] = False


async def run_pump_test(ctx, chat_id, days=14):
    if BT_RUNNING["on"]:
        await ctx.bot.send_message(chat_id, "A test is already running — the result will arrive here.")
        return
    BT_RUNNING["on"] = True
    try:
        await ctx.bot.send_message(chat_id, f"🚀 Testing your pump trigger on the last {days} days of 1-minute candles. "
                                            "It downloads a lot, so give it 20–40 minutes. Keep the PC awake.")
        res = await asyncio.to_thread(pump.run, days, 100, lambda m: log.info("pump: %s", m))
        await send(ctx, chat_id, pump.text(res))
        log.info("pump test done: %s triggers", res.get("triggers"))
    except Exception as e:
        log.exception("pump test failed")
        await ctx.bot.send_message(chat_id, f"Pump test failed: {e}")
    finally:
        BT_RUNNING["on"] = False


@owner_only
async def pumptest_cmd(update: Update, ctx):
    days = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 14
    ctx.application.create_task(run_pump_test(ctx, update.effective_chat.id, max(2, min(25, days))))


@owner_only
async def backtest_cmd(update: Update, ctx):
    days = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 28
    ctx.application.create_task(run_backtest(ctx, update.effective_chat.id, max(3, min(29, days))))


HOURS = {"day": None, "mkt": None, "warned": None}


async def hours_profile():
    """Market volume by hour, refreshed once a day (it shifts when US clocks change)."""
    if HOURS["day"] != today().isoformat() or not HOURS["mkt"]:
        HOURS["mkt"] = await asyncio.to_thread(hours.market_profile)
        HOURS["day"] = today().isoformat()
    return HOURS["mkt"]


@owner_only
async def hours_cmd(update: Update, ctx):
    msg = await update.message.reply_text("Reading 14 days of hourly volume on the 40 most-traded coins…")
    try:
        mkt = await hours_profile()
        mine = hours.my_profile(store.positions_for(con, "2000-01-01", "2100-01-01"))
        png = await asyncio.to_thread(hours.chart, mkt, mine)
        await msg.delete()
        await ctx.bot.send_photo(update.effective_chat.id, png)
        await send(ctx, update.effective_chat.id, hours.text(mkt, mine))
    except Exception as e:
        log.exception("hours failed")
        await msg.edit_text(f"Couldn't build the time-of-day chart: {e}")


async def busy_job(ctx: ContextTypes.DEFAULT_TYPE):
    """15 minutes before the market's busiest hour, remind you what that window has done to your account."""
    try:
        mkt = await hours_profile()
    except Exception:
        return
    peak = max(mkt, key=lambda h: mkt[h]["vol"])
    now = datetime.now(TZ)
    if (now.hour + 1) % 24 != peak or HOURS["warned"] == today().isoformat():
        return
    HOURS["warned"] = today().isoformat()
    mine = hours.my_profile(store.positions_for(con, "2000-01-01", "2100-01-01"))
    w = hours.window(mine, peak, min(24, peak + 3))
    rec = (f" Your record in this window: {w['n']} trades, {w['pnl']:+.2f}, {w['wins'] / w['n'] * 100:.0f}% won."
           if w["n"] else "")
    await ctx.bot.send_message(ctx.job.chat_id,
        f"⏰ <b>In 15 minutes the market's busiest hour starts</b> ({peak:02d}:00, {mkt[peak]['vol']:.1f}× normal volume)."
        + rec + " Bigger, faster swings. Your plan: no new trades in this window — if something is open, its stop is set.",
        parse_mode=ParseMode.HTML)


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

def fetch_ideas():
    """Daily research notes published to the scanner site by the morning research task."""
    if not IDEAS_URL:
        return None
    import requests
    r = requests.get(IDEAS_URL, params={"t": int(datetime.now().timestamp())}, timeout=15)
    r.raise_for_status()
    return r.json()


def ideas_text(data, today_iso):
    import html as _h
    if not data or not data.get("ideas"):
        return None
    stale = data.get("date") != today_iso
    L = [f"<b>💡 Ideas for the bot and your trading</b>" + (f" <i>(from {data.get('date')})</i>" if stale else "")]
    if data.get("headline"):
        L.append(_h.escape(data["headline"], quote=False))
    for i, it in enumerate(data["ideas"][:3], 1):
        L.append(f"{i}. <b>{_h.escape(it.get('title', ''), quote=False)}</b> · {_h.escape(it.get('effort', ''), quote=False)}\n   {_h.escape(it.get('why', ''), quote=False)}")
    for w in (data.get("watch") or [])[:2]:
        L.append(f"⚠️ {_h.escape(w, quote=False)}")
    L.append("<i>Want one built? Tell Claude in the Claude app, e.g. “build idea 1”.</i>")
    return "\n".join(L)


@owner_only
async def ideas_cmd(update: Update, ctx):
    try:
        data = await asyncio.to_thread(fetch_ideas)
    except Exception as e:
        await update.message.reply_text(f"Couldn't load today's ideas ({e}).")
        return
    txt = ideas_text(data, today().isoformat())
    await send(ctx, update.effective_chat.id, txt or "No ideas published yet — the research runs each morning before 08:00.")


def lessons_data(last_day: date):
    days = [(last_day - timedelta(days=i)).isoformat() for i in range(7)]
    by_day = {d: store.positions_for(con, d) for d in days}
    return lessons.build(by_day, last_day, {"cooldown": COOLDOWN, "max_trades": MAX_TRADES, "loss_limit": LOSS_LIMIT,
                                            "max_hold": MAX_HOLD, "max_losses": MAX_LOSSES})


async def send_lessons(ctx, chat_id, last_day: date, full=True):
    data = lessons_data(last_day)          # sqlite connection belongs to this thread
    try:
        png = await asyncio.to_thread(lessons.chart, data)
        rule = lessons.rule_for_today(data)
        await ctx.bot.send_photo(chat_id, png, caption=(f"Rule for today: {rule}" if rule and not full else None))
    except Exception:
        log.exception("rings chart failed")
    if full:
        await send(ctx, chat_id, lessons.text(data))
    return data


def _day_arg(args, default):
    if args:
        try:
            return date.fromisoformat(args[0])
        except ValueError:
            pass
    return default


@owner_only
async def lessons_cmd(update: Update, ctx):
    """Your mistakes ranked by cost, a rule for today, and the rings for the last day you traded."""
    last_day = _day_arg(ctx.args, None)
    if last_day is None:
        row = con.execute("SELECT MAX(day) d FROM positions").fetchone()
        last_day = date.fromisoformat(row["d"]) if row and row["d"] else today()
    await send_lessons(ctx, update.effective_chat.id, last_day, full=True)


@owner_only
async def rings_cmd(update: Update, ctx):
    await send_lessons(ctx, update.effective_chat.id, _day_arg(ctx.args, today()), full=False)


async def morning_brief(ctx, chat_id):
    y = (today() - timedelta(days=1)).isoformat()
    rows, total, _ = week_data()
    await send_report(ctx, chat_id, y, header=f"☀️ <b>Good morning, Dagi</b> — {datetime.now(TZ):%A %d %B}\n\n"
                                              + (FOCUS + "\n\n" if FOCUS else ""))
    try:
        await send_lessons(ctx, chat_id, today() - timedelta(days=1), full=False)
    except Exception:
        log.exception("morning rings failed")
    parts = [f"<b>Last 7 days:</b> {review.money(total)} USDT · /lessons for what your mistakes cost", ""]
    try:
        parts.append(await run_scan_text())
    except Exception as e:
        log.exception("scan failed")
        parts.append(f"Scanner couldn't reach Binance ({e}). Try /scan later.")
    await send(ctx, chat_id, "\n".join(parts), reply_markup=webapp_markup())
    try:
        txt = ideas_text(await asyncio.to_thread(fetch_ideas), today().isoformat())
        if txt:
            await send(ctx, chat_id, txt)
    except Exception:
        log.exception("ideas fetch failed")


async def morning_job(ctx: ContextTypes.DEFAULT_TYPE):
    await send_brief_once(ctx, ctx.job.chat_id)


async def send_brief_once(ctx, chat_id):
    """Send today's brief once. Also called every minute after the brief time, so a brief missed while the PC
    was asleep or offline goes out as soon as it is back (until 18:00; after that, tomorrow's)."""
    t = today().isoformat()
    if store.get_setting(con, "last_brief") == t:
        return
    store.set_setting(con, "last_brief", t)
    try:
        await morning_brief(ctx, chat_id)
    except Exception:
        store.set_setting(con, "last_brief", "")       # try again next minute
        raise


async def window_closed_job(ctx: ContextTypes.DEFAULT_TYPE):
    n = len(store.positions_for(con, today().isoformat()))
    await ctx.bot.send_message(ctx.job.chat_id,
        f"🕛 <b>{LAST_ENTRY} — trading window closed.</b> " + (f"{n} trade(s) logged so far today. " if n else "")
        + "Practice is done for today. Close Binance and give the afternoon to the job search: "
        "applications, portfolio, reaching out to clients.", parse_mode=ParseMode.HTML)


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
            if isinstance(data, dict) and data.get("command") in ("backtest", "pump_test"):   # Claude asked for a test run
                shutil.move(path, os.path.join(INBOX, "done", name))
                fn = run_backtest if data["command"] == "backtest" else run_pump_test
                ctx.application.create_task(fn(ctx, ctx.job.chat_id, int(data.get("days", 28 if fn is run_backtest else 14))))
                continue
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


HERE = os.path.dirname(os.path.abspath(__file__))
def _code_stamp():
    return {f: os.path.getmtime(os.path.join(HERE, f)) for f in os.listdir(HERE) if f.endswith(".py")}
_STAMP = _code_stamp()


async def reload_job(ctx: ContextTypes.DEFAULT_TYPE):
    """When Claude updates the bot's code, restart so the new version runs (start.bat starts it again).
    Also catches up a morning brief that was missed while the PC slept."""
    try:
        now = datetime.now(TZ)
        oid = owner_id()
        if oid and parse_hhmm(MORNING).replace(tzinfo=None) <= now.time().replace(tzinfo=None) < time(18, 0):
            await send_brief_once(ctx, oid)
    except Exception:
        log.exception("brief catch-up failed (will retry)")
    try:
        if _code_stamp() != _STAMP and not BT_RUNNING["on"]:   # never restart in the middle of a backtest
            log.info("Code updated — restarting to load the new version.")
            os._exit(0)
    except Exception:
        log.exception("reload check failed")


async def post_init(app: Application):
    app.job_queue.run_repeating(reload_job, interval=60, first=60, name="reload")
    oid = owner_id()
    if oid:
        schedule(app, oid)
        if WEBAPP_URL:   # keep the Scanner button pointing at the current address
            try:
                await app.bot.set_chat_menu_button(oid, MenuButtonWebApp("Scanner", WebAppInfo(WEBAPP_URL)))
            except Exception as e:
                log.warning("menu button: %s", e)
    else:
        log.info("No owner yet — open the bot in Telegram and send /start")


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    from telegram.error import NetworkError, TimedOut
    if isinstance(ctx.error, (NetworkError, TimedOut)):
        log.warning("No connection to Telegram (%s) — retrying automatically.", ctx.error)
        return
    log.error("Error while handling an update: %s", ctx.error, exc_info=ctx.error)


def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", start))
    for name, fn in [("help", help_cmd), ("rules", rules_cmd), ("today", today_cmd), ("yesterday", yesterday_cmd),
                     ("day", day_cmd), ("week", week_cmd), ("scan", scan_cmd), ("backtest", backtest_cmd), ("pumptest", pumptest_cmd), ("guard", guard_cmd), ("hours", hours_cmd), ("lessons", lessons_cmd), ("rings", rings_cmd), ("morning", morning_cmd),
                     ("report", report_cmd), ("chart", chart_cmd), ("tilt", tilt_cmd), ("ideas", ideas_cmd),
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
