"""Settings loaded from .env (see .env.example)."""
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing setting {name} in .env (copy .env.example to .env and fill it in).")
    return value


BOT_TOKEN = _required("BOT_TOKEN")
if BOT_TOKEN.endswith("AA...") or not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", BOT_TOKEN):
    if BOT_TOKEN != "0:test":  # 0:test = test_receipt.py
        raise SystemExit("BOT_TOKEN in .env is still the example or is wrong. "
                         "Get your token from @BotFather in Telegram and paste it after BOT_TOKEN=")
try:
    OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x}
except ValueError:
    raise SystemExit("OWNER_IDS in .env must be numbers, e.g. OWNER_IDS=123456789")
if not OWNER_IDS or OWNER_IDS == {111111111}:
    raise SystemExit("Set OWNER_IDS in .env to your Telegram user id (a number). Get it from @userinfobot.")

JOIN_CODE = os.getenv("JOIN_CODE", "").strip()           # waiters send /join <code>
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")  # public https URL of the dashboard
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", os.getenv("HTTPS_PROXY", "")).strip()

_tz_name = os.getenv("TIMEZONE", "UTC").strip() or "UTC"
try:
    TZ = ZoneInfo(_tz_name)
except ZoneInfoNotFoundError:
    if _tz_name.upper() != "UTC":
        raise SystemExit(f"Unknown TIMEZONE '{_tz_name}'. On Windows run: pip install tzdata")
    TZ = timezone.utc
DAY_START_HOUR = int(os.getenv("DAY_START_HOUR", "0"))   # e.g. 4 = sales until 04:00 count for the previous day
DAILY_REPORT_TIME = os.getenv("DAILY_REPORT_TIME", "23:00").strip()
CURRENCY = os.getenv("CURRENCY", "ETB").strip()


def _list(name: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


# Where customers should send money. Used to catch payments that went to someone else.
MY_ACCOUNTS = [re.sub(r"\D", "", a) for a in _list("MY_ACCOUNTS") if re.sub(r"\D", "", a)]  # bank accounts / telebirr phones
MY_NAMES = _list("MY_NAMES")                                            # account holder names as the bank shows them
CBE_ACCOUNTS = [a for a in MY_ACCOUNTS if len(a) == 13 and a.startswith("1")]  # CBE accounts are 13 digits

BANK_CHECK = os.getenv("BANK_CHECK", "1") == "1"          # look receipts up on CBE / telebirr websites
AUTO_VERIFY = os.getenv("AUTO_VERIFY", "1") == "1"        # mark entries verified when the bank confirms them
BANK_TIMEOUT = int(os.getenv("BANK_TIMEOUT", "20"))
MAX_RECEIPT_AGE_HOURS = int(os.getenv("MAX_RECEIPT_AGE_HOURS", "24"))

DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
PHOTOS_DIR = DATA_DIR / "photos"
DB_PATH = DATA_DIR / "payments.db"
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def business_day(dt: datetime | None = None) -> str:
    """The shop's working day (YYYY-MM-DD) for a moment in time."""
    local = (dt or now_utc()).astimezone(TZ)
    return (local - timedelta(hours=DAY_START_HOUR)).date().isoformat()


def local_time(iso_utc: str) -> str:
    return datetime.fromisoformat(iso_utc).astimezone(TZ).strftime("%H:%M")


def local_datetime(iso_utc: str) -> str:
    return datetime.fromisoformat(iso_utc).astimezone(TZ).strftime("%Y-%m-%d %H:%M")


def money(value) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}" + (f" {CURRENCY}" if CURRENCY else "")
