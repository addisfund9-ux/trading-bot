# Trading Bot: setup on your Windows PC

Your bot does three things:
1. **Logs your trades.** You send it your Binance Position History screenshots, and it reads them and saves every trade.
2. **Reviews your day.** It replies with your P&L, win rate, best and worst trades, and checks your two rules.
3. **Sends a morning brief at 08:00.** It starts with Claude's written summary of yesterday, then your trades drawn on the price chart, your 7-day total and the scanner's clean setups.

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

## 3. Get a Claude API key
1. Go to https://console.anthropic.com, sign in and add a few dollars of credit under **Billing**.
2. Open **API Keys** → **Create Key**, then copy it (it starts with `sk-ant-`).

Reading screenshots costs a small amount per image, roughly a few cents for a day's screenshots.

## 4. Install and start
1. Unzip this folder somewhere permanent, for example `C:\TradingBot`.
2. Double-click **install.bat** and wait for it to finish.
3. Open the new **.env** file with Notepad. Paste your Telegram token after `TELEGRAM_BOT_TOKEN=` and your Claude key after `ANTHROPIC_API_KEY=`, then save.
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
- **After trading:** in Binance, go to Futures → Orders → **Position History**. Take screenshots while you scroll through the day's trades and send them all to the bot at once. Overlapping screenshots are fine; duplicates are skipped. You can also add the **PNL Analysis** calendar screenshot so the bot can cross-check its totals against Binance's numbers.
  - Send them as **files** (📎 → File) rather than photos, so the small numbers stay sharp.
- **Morning:** read the brief at 08:00 and open the scanner.

## Commands
| Command | What it does |
|---|---|
| `/report`, `/report 2026-10-24` | Claude's summary + your trades on the chart + the numbers (yesterday by default) |
| `/chart`, `/chart 2026-10-24` | Your trades drawn on the price chart (today by default) |
| `/today`, `/yesterday`, `/day 2026-10-24` | Review of one day |
| `/week` | Last 7 days with a chart |
| `/scan` | Run the scanner now |
| `/morning` | Send the morning brief now (good for testing) |
| `/undo` | Remove the last batch of screenshots you sent |
| `/export` | Download every trade as a CSV |
| `/rules` | Show your rules |

## Settings (in `.env`)
- `MORNING_TIME`, `EVENING_REMINDER`: message times (Addis Ababa time).
- `MAX_HOLD_MIN`, `MAX_LOSSES_PER_COIN`: your two rules.
- `SCAN_TOP`, `SCAN_MIN_VOL_M`: how many movers to scan, and the minimum 24h volume in $M.

After you change `.env`, restart the bot by closing the black window and opening `start.bat` again.

## Your data
- All trades are stored in **trades.db** in this folder. Back it up now and then, or use `/export`.
- **Never share your `.env` file.** It contains your keys. The bot only answers you, and never needs your Binance login or Binance API keys.

## If something goes wrong
- **The black window shows an error mentioning `TELEGRAM_BOT_TOKEN`:** the token in `.env` is missing or mistyped.
- **"screenshot(s) couldn't be read":** check the Claude key and the credit balance at console.anthropic.com.
- **The scanner says it can't reach Binance:** check your internet connection, since the scanner runs from your PC.
- **A number was read wrong:** send `/undo`, then send the screenshots again as files.
- **A trade isn't drawn on the chart:** its open or close time was cut off in the screenshot. It still counts in the P&L. Send a screenshot that shows the full card and it fills in.
