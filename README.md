# Transaction Checker Bot

Waiters/cashiers send every customer payment (receipt screenshot, QR photo or typed
transaction code) to a Telegram bot during the day. The owner sees everything —
structured, totalled and checked for duplicates — in a dashboard that opens inside Telegram.

## What it does

**Waiters**
- Send a photo/screenshot → the bot reads the QR code (and the text, if OCR is enabled),
  extracts the **transaction code** and **amount**, saves the photo.
- Or type `FT25123ABC12 450` (one payment per line), or forward the bank SMS.
- If something is missing, they **reply** to the bot's message with the amount/code.
- `/today` shows their entries, `/undo` deletes their last one.

**Owners**
- 🚨 Instant alert when a transaction code is sent **twice** (same screenshot reused, any day).
- `/report` → dashboard (Telegram Mini App):
  - claimed total, verified / pending / rejected, duplicates, entries missing info
  - type the **amount received in the bank** → shows the difference (missing money)
  - totals per waiter
  - every entry with its photo, QR content, ✓ Verify / ✗ Reject / ✎ Edit, "Verify all"
  - send a CSV of the day to your chat
- `/summary [date|yesterday]`, `/export [date]`, `/waiters`, `/remove <id>`
- Automatic end-of-day summary at `DAILY_REPORT_TIME`.

## Ethiopian banks & wallets

| | Reads receipt / QR | Checks on the bank's own website |
|---|---|---|
| **CBE** (app receipt, QR link, SMS) | ✅ | ✅ `apps.cbe.com.et` – needs your CBE account in `MY_ACCOUNTS` |
| **telebirr** (SMS, receipt link, transaction number) | ✅ | ✅ `transactioninfo.ethiotelecom.et` – may only work from an Ethiopian internet connection |
| Dashen, Abyssinia, Awash, Zemen, M-Pesa | ✅ recognised from the QR link | ❌ owner opens the official receipt link from the dashboard |
| Other banks | code + amount only | ❌ |

For every receipt the bot checks:
1. **Already used?** The same transaction code was sent before, on any day.
2. **Right account?** The receiver on the receipt or bank site must match `MY_ACCOUNTS` / `MY_NAMES`.
3. **Fresh?** Receipts older than `MAX_RECEIPT_AGE_HOURS` are flagged (re-used screenshots).
4. **Real?** CBE and telebirr: the transaction is looked up on the bank's site, so the amount, receiver
   and date come from the bank, not from an image someone could edit. With `AUTO_VERIFY=1` the entry is marked
   ✅ verified when everything matches. Problems are sent to the owner immediately.

The amount used is the **transferred amount** (e.g. ETB 100.00), not "Total Amount Debited" (100.61, includes fees).

> The bank receipt sites are not official APIs. If a bank changes its site, that check stops working
> (the entry is shown as "could not check"). Everything else keeps working, and you can still verify manually.

## Setup

1. **Create the bot**: in Telegram, message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
2. **Get your user id** from [@userinfobot](https://t.me/userinfobot).
3. Install (Python 3.10+):
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\activate
   pip install -r requirements.txt
   copy .env.example .env      # then edit .env
   ```
4. **Dashboard URL**: Telegram only opens Mini Apps over HTTPS.
   - For testing: install [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
     and run `cloudflared tunnel --url http://localhost:8080`, then put the `https://….trycloudflare.com`
     URL in `WEBAPP_URL`. (This URL changes on every restart.)
   - For production: run on a VPS with a domain + HTTPS (Caddy/nginx), proxying to port 8080.
5. Run:
   ```powershell
   python main.py
   ```
6. Open your bot and press **Start** (as owner). Give waiters the join code;
   they send `/join <code>`.

### Optional: read text from screenshots (OCR)
Many receipt screenshots have no QR code. To read the code/amount from the text, install
[Tesseract](https://github.com/UB-Mannheim/tesseract/wiki) and `pip install pytesseract`.
The bot detects it automatically.

## Files
| File | Purpose |
|---|---|
| `main.py` | starts the bot and the dashboard server |
| `bot.py` | Telegram commands and message handling |
| `parsing.py` | QR decoding, OCR, extraction of codes and amounts |
| `banks.py` | Ethiopian bank receipts: recognising, reading, checking on CBE / telebirr sites |
| `verify.py` | runs the bank check for an entry and saves the result |
| `db.py` | SQLite storage and daily summaries (`data/payments.db`) |
| `web.py` | dashboard API, secured with Telegram's signed `initData` (owners only) |
| `webapp/index.html` | the dashboard page |
| `reports.py` | summary text and CSV export |

Photos are stored in `data/photos/<day>/`. Back up the `data/` folder.
