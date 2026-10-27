# Trading Bot: setup on your Windows PC

Your bot does three things:
1. **Logs your trades.** You send it your Binance export file (CSV or Excel), or screenshots if you've added a Claude key, and it saves every trade.
2. **Reviews your day.** It replies with your P&L, win rate, best and worst trades, and checks your two rules.
3. **Sends a morning brief at 08:00.** It starts with a written summary of yesterday (by Claude if you have a key, otherwise by built-in rules), then your trades drawn on the price chart, your 7-day total and the scanner's clean setups.

The bot runs on your PC, so it's only online while the PC is on and awake. The scanner page is hosted on Netlify from this GitHub repo.

**Getting the code:** download it from GitHub (green **Code** button → **Download ZIP**), or, if you have Git installed, run `git clone` and later `git pull` to get updates. Your `.env` keys and `trades.db` are never uploaded to GitHub.

---

## 1. Install Python (one time)
Download Python 3.12 or newer from https://www.python.org/downloads/.
On the first install screen, **tick "Add python.exe to PATH"**, then click Install.

## 2. Create your bot in Telegram (2 minutes)
1. In Telegram, search for **@BotFather** and open it.
2. Send `/newbot`, choose a name (for example "Dagi Trading") and a username ending in `bot`.
3. BotFather replies with a **token** like `7123456789:AAH...`. Copy it.

## 3. Optional: a Claude API key
The bot works without one. Without a key, you log trades by sending Binance's **export file** (CSV or Excel), and the morning summary is written by built-in rules.

With a key, the bot can also read **screenshots**, and Claude writes the morning summary. To get one, go to https://console.anthropic.com → **Billing** (add a few dollars) → **API Keys** → **Create Key**, and paste it after `ANTHROPIC_API_KEY=` in `.env`. Reading screenshots costs roughly a few cents a day.

## 4. Install and start
1. Unzip this folder somewhere permanent, for example `C:\TradingBot`.
2. Double-click **install.bat** and wait for it to finish.
3. Open the new **.env** file with Notepad. Paste your Telegram token after `TELEGRAM_BOT_TOKEN=` (and your Claude key after `ANTHROPIC_API_KEY=` if you have one), then save.
   - **Slow connection?** If `install.bat` keeps timing out, run **install_offline.bat** instead. It installs from the `wheels` folder without downloading anything, if that folder is present.
4. Double-click **start.bat**. A black window opens and says "Bot running". Keep it open.
5. In Telegram, open your new bot and send **/start**. You are now its only owner.

**Start it automatically:** press `Win + R`, type `shell:startup`, press Enter, and drag a **shortcut** of `start.bat` into that folder.
Also set Windows **Sleep** to "Never" while plugged in (Settings → System → Power), or the 08:00 message can't be sent.

## 5. The scanner inside Telegram (Mini App) via Netlify
Telegram only opens Mini Apps from an `https://` address. Netlify hosts the scanner page for free straight from this repo, and updates it whenever the repo changes:
1. Go to https://app.netlify.com and sign up with **GitHub**.
2. Click **Add new project → Import an existing project → GitHub**, and allow Netlify to see the **trading-bot** repo.
3. Pick **trading-bot**. The settings fill in automatically from `netlify.toml` (publish folder `webapp`). Click **Deploy**.
4. When it finishes, copy your link (like `https://something.netlify.app`). You can rename it under **Site configuration → Change site name**.
5. Paste it after `WEBAPP_URL=` in `.env`, save, restart `start.bat`, and send `/start` once more.

You'll get a **Scanner** button next to the message box, and an "Open scanner" button under each morning brief.

---

## Daily use
- **After trading, without a Claude key:** in Binance, go to Orders → Futures → **Trade History** (or Position History), tap the **export/download** icon, choose the date range, and export as CSV or Excel. Send the file to the bot with 📎 → File. A **.zip** from Binance works as-is, so there's no need to unzip it. Overlapping date ranges are fine; trades already logged are skipped, even ones that came from screenshots.
- **After trading, with a Claude key:** you can send screenshots instead. In Binance, go to Futures → Orders → **Position History**. Take screenshots while you scroll through the day's trades and send them all to the bot at once. Overlapping screenshots are fine; duplicates are skipped. You can also add the **PNL Analysis** calendar screenshot so the bot can cross-check its totals against Binance's numbers.
  - Send them as **files** (📎 → File) rather than photos, so the small numbers stay sharp.
- **Morning:** read the brief at 08:00 and open the scanner.

## Commands
| Command | What it does |
|---|---|
| `/report`, `/report 2026-10-24` | Claude's summary + your trades on the chart + the numbers (yesterday by default) |
| `/chart`, `/chart 2026-10-24` | Your trades drawn on the price chart (today by default) |
| `/ideas` | Today's research: new ideas for the bot and your trading (also in the 08:00 brief) |
| `/tilt`, `/tilt 2026-10-24`, `/tilt week` | Revenge-trading and overtrading check, with your day drawn on one line |
| `/today`, `/yesterday`, `/day 2026-10-24` | Review of one day |
| `/week` | Last 7 days with a chart |
| `/scan` | Run the scanner now |
| `/backtest`, `/backtest 14` | Replay the scanner over the last 28 (or 14) days and trade every Clean signal: win rate, average R, total. Takes ~10 min |
| `/guard` | Live tilt guard status |
| `/morning` | Send the morning brief now (good for testing) |
| `/undo` | Remove the last batch of screenshots you sent |
| `/export` | Download every trade as a CSV |
| `/rules` | Show your rules |

## Settings (in `.env`)
- `MORNING_TIME`, `EVENING_REMINDER`: message times (Addis Ababa time).
- `MAX_HOLD_MIN`, `MAX_LOSSES_PER_COIN`: your two exit rules.
- `COOLDOWN_MIN`, `MAX_TRADES_PER_DAY`, `DAILY_LOSS_LIMIT`: your anti-tilt rules (wait after a loss, cap trades per day, stop after giving back this much from the day's high).
- `SCAN_TOP`, `SCAN_MIN_VOL_M`: how many movers to scan, and the minimum 24h volume in $M.

After you change `.env`, restart the bot by closing the black window and opening `start.bat` again.

## Live tilt guard (optional)
While you trade, the bot checks your Binance account every 20 seconds and warns you in Telegram when you: open a trade within your cooldown after a loss, flip sides right after a loss, raise leverage or size after a loss, go past your trade count or daily loss limit, have a position with no stop-loss, or hold a losing trade past `MAX_HOLD_MIN`.

It needs a **read-only** Binance API key:
1. Binance → Profile → **API Management** → **Create API** → System generated.
2. Leave **only "Enable Reading"** ticked. No trading, no withdrawals, no transfers. Restricting it to your IP is even better.
3. Open `.env` in Notepad and add two lines: `BINANCE_API_KEY=...` and `BINANCE_API_SECRET=...`. Save, then restart `start.bat`.
4. Send `/guard` to check it says ON.

The bot asks Binance what the key can do and **refuses any key that can trade or withdraw**.

## Your data
- All trades are stored in **trades.db** in this folder. Back it up now and then, or use `/export`.
- **Never share your `.env` file.** It contains your keys. The bot only answers you, and never needs your Binance login or Binance API keys.

## If something goes wrong
- **The black window shows an error mentioning `TELEGRAM_BOT_TOKEN`:** the token in `.env` is missing or mistyped.
- **"screenshot(s) couldn't be read":** check the Claude key and the credit balance at console.anthropic.com.
- **"I don't recognise this file's columns":** send that message to Claude so the importer can be adjusted to Binance's exact export format.
- **The scanner says it can't reach Binance:** check your internet connection, since the scanner runs from your PC.
- **A number was read wrong:** send `/undo`, then send the screenshots again as files.
- **A trade isn't drawn on the chart:** its open or close time was cut off in the screenshot. It still counts in the P&L. Send a screenshot that shows the full card and it fills in.
