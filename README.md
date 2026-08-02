# NSE + BSE Result Notifier → Telegram

Polls NSE and BSE every 15 minutes for newly filed financial results,
de-duplicates announcements that show up on **both** exchanges (many
companies are dual-listed), and pushes an alert to your Telegram.

## 1. Create your Telegram bot (2 minutes, one-time)

1. Open Telegram, search for **BotFather**, send `/newbot`.
2. Give it a name — you'll get back a **bot token** that looks like
   `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`.
3. Search for **userinfobot**, send `/start` — it replies with your
   numeric **chat ID**.

## 2. Install dependencies

```bash
pip install -r requirements.txt
```

## 3. Configure

Easiest: set environment variables before running —

```bash
export TELEGRAM_BOT_TOKEN="123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
export TELEGRAM_CHAT_ID="987654321"
```

Or just edit the two variables directly at the top of
`result_notifier.py` under `CONFIG`.

## 4. Run

```bash
python result_notifier.py
```

Leave it running (see "keep it running 24/7" below). It logs to
`notifier.log` and prints to console. Every new result filing on NSE
or BSE sends you a Telegram message like:

```
📢 Reliance Industries Ltd (NSE)
Financial Results for the quarter ended June 30, 2026
🕐 2026-08-01 09:32:00
🔗 <link to filing PDF>
```

## Config knobs (top of result_notifier.py)

| Variable | Purpose |
|---|---|
| `POLL_INTERVAL_MINUTES` | How often to check (default 15) |
| `POLL_ONLY_MARKET_HOURS` | Only poll Mon–Fri 9am–4pm IST (default True) |
| `RESULT_KEYWORDS` | Subject-line keywords that count as a "result" filing. Set to `[]` to alert on **every** corporate announcement, not just results |
| `WATCHLIST` | Leave `[]` to track all companies, or add tickers e.g. `["RELIANCE", "TCS"]` to restrict |

## Keep it running 24/7

Options, roughly in order of ease:

- **Your own PC**: just leave the terminal open, or use `nohup python result_notifier.py &` on Mac/Linux, or Task Scheduler on Windows.
- **Free cloud (recommended for reliability)**: PythonAnywhere free tier can run this as an "always-on task" (paid tier) or you can trigger it via a scheduled task every 15 min on the free tier.
- **GitHub Actions**: instead of a long-running loop, you can strip the `while True` loop and run `poll_once()` a single time, triggered by a cron schedule (`*/15 9-16 * * 1-5` in IST-adjusted UTC). This avoids needing a server at all — GitHub runs it for free on a schedule. Ask me if you want this version.

## Known limitations

- NSE actively rate-limits/blocks non-browser traffic. The script mimics a browser session, but if NSE changes its anti-bot measures, NSE-side alerts may silently stop — check `notifier.log` for `NSE fetch failed` errors. BSE polling is unaffected.
- The dedup logic matches on normalised company name + subject keywords + date. Very rare edge case: two *different* announcements from the same company on the same day with very similar wording could be treated as one. Widen `RESULT_KEYWORDS` matching or check `notifier.log` if you suspect a miss.
- This is unofficial use of NSE/BSE's public web APIs, not a documented/supported data feed — endpoints can change without notice.
