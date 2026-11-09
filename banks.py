"""Ethiopian banks & wallets: recognise receipts, read their text, and check them on the bank's own receipt site.

Online checks (the receipt is fetched from the bank itself, so a fake screenshot can't pass):
  * CBE       https://apps.cbe.com.et:100/?id=<FT reference><last 8 digits of an account>  (PDF)
  * telebirr  https://transactioninfo.ethiotelecom.et/receipt/<transaction number>     (HTML)
Other banks (Dashen, Abyssinia, Awash, Zemen, M-Pesa, ...) are recognised from their QR links, and the
owner gets the official receipt link to open from the dashboard.
"""
from __future__ import annotations

import asyncio
import html as htmlmod
import io
import logging
import re
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlparse

import aiohttp

import config

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    PdfReader = None

log = logging.getLogger(__name__)

NUM = r"(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
FT_RE = re.compile(r"^(FT[A-Z0-9]{10})(\d{5,8})?$")

HOSTS = {
    "apps.cbe.com.et": "CBE",
    "mbreciept.cbe.com.et": "CBE",   # new CBE mobile banking receipt QR (spelling is CBE's)
    "mbreceipt.cbe.com.et": "CBE",
    "cbe.com.et": "CBE",
    "transactioninfo.ethiotelecom.et": "telebirr",
    "telebirr.ethiotelecom.et": "telebirr",
    "receipt.dashensuperapp.com": "Dashen",
    "dashensuperapp.com": "Dashen",
    "dashenbanksc.com": "Dashen",
    "cs.bankofabyssinia.com": "Abyssinia",
    "bankofabyssinia.com": "Abyssinia",
    "awashpay.awashbank.com": "Awash",
    "awashbank.com": "Awash",
    "share.zemenbank.com": "Zemen",
    "zemenbank.com": "Zemen",
    "m-pesabusiness.safaricom.et": "M-Pesa",
    "safaricom.et": "M-Pesa",
    "coopbankoromia.com.et": "Coop",
    "ebirr.com": "Coop",
    "hibretbank.com.et": "Hibret",
    "nibbanksc.com": "Nib",
    "wegagen.com": "Wegagen",
    "berhanbank.com": "Berhan",
    "siinqeebank.com.et": "Siinqee",
    "amharabank.com.et": "Amhara",
    "oromiabank.com": "Oromia",
    "bunnabanksc.com": "Bunna",
}
KEYWORDS = [  # most specific first
    ("CBE Birr", r"cbe\s?birr"),
    ("telebirr", r"tele\s?birr|ethio\s?telecom"),
    ("CBE", r"commercial bank of ethiopia|\bcbe\b"),
    ("Abyssinia", r"abyssinia|\bboa\b"),
    ("Awash", r"\bawash\b"),
    ("Dashen", r"\bdashen\b"),
    ("Zemen", r"\bzemen\b"),
    ("M-Pesa", r"m-?pesa|safaricom"),
    ("Amhara", r"amhara bank"),
    ("Coop", r"cooperative bank|\bcoop\s?bank|\bcoopay"),
    ("Oromia", r"oromia (?:international )?bank"),
    ("Wegagen", r"\bwegagen\b"),
    ("Hibret", r"\bhibret\b"),
    ("Nib", r"\bnib (?:international )?bank"),
    ("Bunna", r"\bbunna\b"),
    ("Berhan", r"\bberhan bank"),
    ("Enat", r"\benat bank"),
    ("Siinqee", r"\bsiinqee\b"),
    ("Ahadu", r"\bahadu\b"),
    ("Global", r"global bank"),
]

# Wording of common receipts/SMS (whitespace is collapsed before matching).
# If your bank's wording differs, add a pattern here.
TEXT_PATTERNS = [
    ("CBE", re.compile(  # CBE app receipt: "ETB 100.00 has been debited from A ETB-0302 for B ETB-6076 on ..."
        r"ETB\s?(?P<amount>" + NUM + r") has been debited from (?P<payer>.+?)\s+(?:ETB-?\d+|[\d*]{4,})\s+"
        r"for (?P<receiver>.+?)\s+(?P<racct>ETB-?\d{3,}|[\d*]{4,})\s+on (?P<time>.+?)\s+"
        r"with transaction ID:?\s*(?P<ref>FT[A-Z0-9]{10})", re.I)),
    ("CBE", re.compile(  # CBE SMS to the receiver: "Account 1****6076 has been Credited with ETB 100.00 from ..."
        r"Account (?P<racct>[\d*]{4,}) has been Credited with ETB\s?(?P<amount>" + NUM + r")\s+from (?P<payer>.+?),?\s+"
        r"on (?P<time>\d{1,2}/\d{1,2}/\d{4}(?: at)? \d{1,2}:\d{2}(?::\d{2})?).*?Ref No\.?:?\s*(?P<ref>FT[A-Z0-9]{10})",
        re.I)),
    ("telebirr", re.compile(  # telebirr SMS to the sender
        r"You have transferred ETB\s?(?P<amount>" + NUM + r")\s+to (?P<receiver>.+?)\s?\((?P<racct>[\d*]+)\)\s+"
        r"on (?P<time>\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}(?::\d{2})?).*?transaction number is (?P<ref>[A-Z0-9]{8,12})",
        re.I)),
    ("telebirr", re.compile(  # telebirr SMS to the receiver
        r"You have received ETB\s?(?P<amount>" + NUM + r")\s+.*?from (?P<payer>.+?)\s?\([\d*]+\)\s+"
        r"on (?P<time>\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}(?::\d{2})?).*?transaction number is (?P<ref>[A-Z0-9]{8,12})",
        re.I)),
    ("Abyssinia", re.compile(  # Bank of Abyssinia SMS / receipt
        r"(?:transferred|credited|received)\s+(?:with\s+)?ETB\s?(?P<amount>" + NUM + r").*?"
        r"(?:to|from)\s+(?P<receiver>[^,\n]+?)\s*(?:\((?P<racct>[\d*]+)\))?.*?"
        r"(?:Txn|Trx|Ref|ID|Code)[\s.:#]*(?P<ref>FT[A-Z0-9]{10}|[A-Z0-9]{10,16})", re.I)),
    ("Dashen", re.compile(  # Dashen / Amole
        r"(?:transferred|received|credited)\s+(?:with\s+)?ETB\s?(?P<amount>" + NUM + r").*?"
        r"(?:to|from)\s+(?P<receiver>[^,\n]+?).*?"
        r"(?:Txn|Trx|Ref|ID|Code)[\s.:#]*(?P<ref>\d{3}[A-Z0-9]{13}|[A-Z0-9]{10,18})", re.I)),
    ("Awash", re.compile(  # Awash Bank / AwashBirr
        r"(?:transferred|received|credited)\s+(?:with\s+)?ETB\s?(?P<amount>" + NUM + r").*?"
        r"(?:to|from)\s+(?P<receiver>[^,\n]+?).*?"
        r"(?:Txn|Trx|Ref|ID|Code)[\s.:#]*(?P<ref>[A-Z0-9]{8,18})", re.I)),
    ("M-Pesa", re.compile(  # Safaricom M-Pesa Ethiopia
        r"(?:confirmed|sent|received)\.?\s*ETB\s?(?P<amount>" + NUM + r").*?"
        r"(?:to|from)\s+(?P<receiver>[^,\n]+?).*?"
        r"(?:Transaction ID|Txn|Ref)[\s.:#]*(?P<ref>[A-Z0-9]{10,16})", re.I)),
]

TIME_FORMATS = ["%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
                "%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y, %I:%M:%S %p", "%d/%m/%Y"]


@dataclass
class Receipt:
    bank: str | None = None
    ref: str | None = None
    amount: float | None = None
    payer: str | None = None
    receiver: str | None = None
    receiver_account: str | None = None
    time: datetime | None = None
    url: str | None = None
    suffix: str | None = None       # account digits that came with the reference in a QR link
    status: str | None = None       # transaction status reported by the bank

    def merge(self, other: "Receipt | None") -> "Receipt":
        """Fill empty fields from another receipt."""
        if other:
            for f in fields(self):
                if getattr(self, f.name) in (None, "") and getattr(other, f.name) not in (None, ""):
                    setattr(self, f.name, getattr(other, f.name))
        return self


@dataclass
class BankCheck:
    status: str        # ok | mismatch | not_found | error | unsupported
    message: str
    receipt: Receipt | None = None


# ------------------------------------------------------------------ recognising receipts
def _to_amount(s: str | None) -> float | None:
    m = re.search(NUM, s or "")
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return round(v, 2) if v > 0 else None


def parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    s = re.sub(r"\s+", " ", str(s).replace(" at ", " ")).strip(" .,")
    try:  # ISO format from JSON, e.g. 2026-11-07T13:31:00
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=config.TZ)
    except ValueError:
        pass
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=config.TZ)
        except ValueError:
            continue
    return None


def detect_bank(text: str) -> str | None:
    for bank, rx in KEYWORDS:
        if re.search(rx, text or "", re.I):
            return bank
    return None


def guess_bank_from_ref(ref: str | None) -> str | None:
    ref = (ref or "").upper()
    if re.fullmatch(r"FT[A-Z0-9]{10}", ref):
        return "CBE"            # Abyssinia also uses FT…; the caption/QR decides when present
    if re.fullmatch(r"\d{3}[A-Z0-9]{13}", ref):
        return "Dashen"
    if re.fullmatch(r"[A-Z]{2,3}[A-Z0-9]{7,8}", ref) and any(c.isdigit() for c in ref):
        return "telebirr"
    return None


def from_url(url: str) -> Receipt | None:
    """Receipt info from an official bank receipt link (usually what the receipt's QR code contains)."""
    u = urlparse(url.strip())
    host = (u.hostname or "").lower()
    bank = next((b for h, b in HOSTS.items() if host == h or host.endswith("." + h)), None)
    if not bank:
        return None
    query = {k.lower(): v for k, v in parse_qsl(u.query)}
    segs = [s for s in u.path.split("/") if s]
    r = Receipt(bank=bank, url=url.strip())
    if bank in ("CBE", "Abyssinia"):
        m = FT_RE.match((query.get("id") or query.get("trx") or "").upper())
        if m:
            r.ref, r.suffix = m.group(1), m.group(2)
    elif bank == "Zemen":
        r.ref = segs[-2] if len(segs) >= 2 and segs[-1].lower() == "pdf" else (segs[-1] if segs else None)
    elif segs:
        r.ref = segs[-1].lstrip("-")
    if r.ref:
        r.ref = r.ref.upper()
    return r


def parse_text(text: str) -> Receipt:
    """Bank/wallet receipt text (OCR of a screenshot or a forwarded SMS) -> Receipt."""
    flat = re.sub(r"\s+", " ", text or "").strip()
    r = Receipt(bank=detect_bank(flat))
    for bank, rx in TEXT_PATTERNS:
        m = rx.search(flat)
        if not m:
            continue
        g = m.groupdict()
        r.bank = bank
        r.ref = g["ref"].upper()
        r.amount = _to_amount(g.get("amount"))
        r.payer = (g.get("payer") or "").strip() or None
        r.receiver = (g.get("receiver") or "").strip() or None
        r.receiver_account = g.get("racct")
        r.time = parse_time(g.get("time"))
        break
    return r


# ------------------------------------------------------------------ is it really the owner's money?
def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def account_matches(shown: str | None, accounts: list[str] | None = None) -> bool | None:
    """Compares a (usually masked) account like 'ETB-6076', '1*********6076' or '2519****3344'."""
    acc_list = accounts if accounts is not None else config.MY_ACCOUNTS
    if not acc_list or not shown:
        return None
    groups = re.findall(r"\d+", shown)
    tail = groups[-1] if groups else ""
    if len(tail) < 3:
        return None
    for acc in acc_list:
        clean_acc = re.sub(r"\D", "", acc)
        n = min(len(tail), len(clean_acc), 9)
        if clean_acc and tail[-n:] == clean_acc[-n:]:
            return True
    return False


def name_matches(name: str | None, names: list[str] | None = None) -> bool | None:
    name_list = names if names is not None else config.MY_NAMES
    if not name_list or not name:
        return None
    words = set(re.findall(r"[a-z]+", name.lower()))
    for mine in name_list:
        my_words = set(re.findall(r"[a-z]+", mine.lower()))
        if my_words and len(words & my_words) >= min(2, len(my_words)):
            return True
    return False


def receiver_ok(r: Receipt, accounts: list[str] | None = None, names: list[str] | None = None) -> bool | None:
    acc = account_matches(r.receiver_account, accounts)
    nm = name_matches(r.receiver, names)
    if acc is False:
        return False
    if nm is False and acc is None:
        return False
    if acc is True:
        if nm is False:
            return False
        return True
    if nm is True:
        return True
    return None


def receiver_label(r: Receipt) -> str:
    return " ".join(x for x in (r.receiver, f"({r.receiver_account})" if r.receiver_account else None) if x) or "?"


def offline_flags(r: Receipt, accounts: list[str] | None = None, names: list[str] | None = None) -> list[str]:
    """Problems visible on the receipt itself."""
    flags = []
    if receiver_ok(r, accounts, names) is False:
        flags.append(f"Paid to {receiver_label(r)} — not the business account!")
    if r.time:
        now = config.now_utc()
        if now - r.time > timedelta(hours=config.MAX_RECEIPT_AGE_HOURS):
            flags.append(f"Old receipt: dated {r.time.astimezone(config.TZ):%Y-%m-%d %H:%M}")
        elif r.time - now > timedelta(minutes=15):
            flags.append(f"Receipt time is in the future ({r.time:%Y-%m-%d %H:%M})")
    if r.url and r.url.lower().startswith(("http://", "https://")):
        u = urlparse(r.url)
        host = (u.hostname or "").lower()
        is_known = any(host == h or host.endswith("." + h) for h in HOSTS)
        if not is_known:
            flags.append(f"Suspicious QR link ({host}) — not from an authorized Ethiopian bank!")
    return flags


# ------------------------------------------------------------------ online checks
async def _get(url: str, insecure_fallback: bool = False) -> tuple[int, bytes]:
    timeout = aiohttp.ClientTimeout(total=config.BANK_TIMEOUT)
    headers = {"User-Agent": "Mozilla/5.0 (receipt-checker)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
        try:
            async with s.get(url) as resp:
                return resp.status, await resp.read()
        except aiohttp.ClientSSLError:
            if not insecure_fallback:
                raise
            # Some bank receipt sites have broken certificate chains; it's a read-only public receipt.
            async with s.get(url, ssl=False) as resp:
                return resp.status, await resp.read()


def _pdf_text(data: bytes) -> str:
    if PdfReader is None:
        return ""
    try:
        return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
    except Exception:  # noqa: BLE001
        return ""


def parse_cbe_pdf(text: str, ref: str) -> Receipt:
    flat = re.sub(r"[ \t]+", " ", text)

    def grab(label: str) -> str | None:
        m = re.search(label + r"\s*:?\s*([^\n]+)", flat, re.I)
        return m.group(1).strip() if m else None

    r = Receipt(bank="CBE", ref=ref)
    r.payer = grab(r"\bPayer\b")
    r.receiver = grab(r"\bReceiver\b")
    accounts = re.findall(r"\bAccount\b\s*:?\s*([\d*][\d* ]{3,})", flat, re.I)
    if len(accounts) >= 2:
        r.receiver_account = accounts[1].strip()
    r.amount = _to_amount(grab(r"Transferred Amount"))
    r.time = parse_time(grab(r"Payment Date\s*&\s*Time"))
    return r


async def _check_cbe(r: Receipt, cbe_accounts: list[str] | None = None) -> Receipt | None:
    cbe_accs = cbe_accounts if cbe_accounts is not None else config.CBE_ACCOUNTS
    suffixes = [r.suffix] if r.suffix else []
    suffixes += [acc[-8:] for acc in cbe_accs if len(acc) >= 8]
    for suffix in dict.fromkeys(suffixes):
        url = f"https://apps.cbe.com.et:100/?id={r.ref}{suffix}"
        status, body = await _get(url, insecure_fallback=True)
        if status == 200 and body.startswith(b"%PDF"):
            found = parse_cbe_pdf(_pdf_text(body), r.ref)
            found.url = url
            return found
    return None


TELEBIRR_LABELS = ["payer name", "credited party name", "credited party account no", "transaction status",
                   "settled amount", "payment date", "invoice no", "receipt no", "total paid"]


def parse_telebirr_html(page: str, ref: str) -> Receipt | None:
    def clean(cell: str) -> str:
        return re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", cell))).strip()

    rows = [[clean(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)]
            for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S | re.I)]

    def is_label(s: str) -> bool:
        return any(lbl in s.lower() for lbl in TELEBIRR_LABELS)

    def find(label: str) -> str | None:
        for ri, row in enumerate(rows):
            for ci, cell in enumerate(row):
                if label in cell.lower():
                    if ci + 1 < len(row) and row[ci + 1] and not is_label(row[ci + 1]):
                        return row[ci + 1]          # value to the right
                    if ri + 1 < len(rows) and ci < len(rows[ri + 1]) and not is_label(rows[ri + 1][ci]):
                        return rows[ri + 1][ci]     # value in the row below
        return None

    amount = _to_amount(find("settled amount"))
    if amount is None and ref.lower() not in page.lower():
        return None
    return Receipt(bank="telebirr", ref=ref, amount=amount, payer=find("payer name"),
                   receiver=find("credited party name"), receiver_account=find("credited party account no"),
                   time=parse_time(find("payment date")), status=find("transaction status"))


async def _check_telebirr(r: Receipt) -> Receipt | None:
    url = f"https://transactioninfo.ethiotelecom.et/receipt/{r.ref}"
    status, body = await _get(url)
    if status != 200:
        return None
    found = parse_telebirr_html(body.decode("utf-8", "ignore"), r.ref)
    if found:
        found.url = url
    return found


# ------------------------------------------------------------------ receipt links opened in a browser
BROWSER_HOSTS = {"mbreciept.cbe.com.et", "mbreceipt.cbe.com.et"}
JSON_KEYS = {
    "ref": {"transactionid", "txnid", "reference", "referenceno", "referencenumber", "refno", "ftnumber",
            "transactionreference", "transactionref", "trxref", "trxid", "ftreference"},
    "amount": {"amount", "transferredamount", "transactionamount", "txnamount", "debitamount", "creditamount",
               "transferamount"},
    "receiver": {"receivername", "creditaccountname", "beneficiaryname", "creditedpartyname", "receiver",
                 "beneficiary", "creditorname", "toaccountname", "toname"},
    "receiver_account": {"receiveraccount", "creditaccount", "creditaccountnumber", "beneficiaryaccount",
                         "toaccount", "toaccountnumber", "creditedaccount", "receiveraccountnumber"},
    "payer": {"payername", "debitaccountname", "sendername", "payer", "debtorname", "fromaccountname", "fromname"},
    "time": {"transactiondate", "paymentdate", "transactiontime", "datetime", "valuedate", "date", "createdat"},
}


def is_browser_link(url: str | None) -> bool:
    return bool(url) and (urlparse(url).hostname or "").lower() in BROWSER_HOSTS


def parse_json_payloads(payloads: list) -> Receipt:
    """Receipt fields from JSON the receipt page downloaded (field names vary, so match loosely)."""
    values: dict[str, object] = {}

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                key = re.sub(r"[^a-z]", "", str(k).lower())
                if isinstance(v, (dict, list)):
                    walk(v)
                elif v not in (None, ""):
                    for name, keys in JSON_KEYS.items():
                        if key in keys and name not in values:
                            values[name] = v
        elif isinstance(obj, list):
            for x in obj:
                walk(x)

    for p in payloads:
        walk(p.get("data"))
    ref = str(values.get("ref") or "").strip().upper() or None
    return Receipt(ref=ref, amount=_to_amount(str(values.get("amount") or "")),
                   receiver=str(values["receiver"]).strip() if values.get("receiver") else None,
                   receiver_account=str(values["receiver_account"]) if values.get("receiver_account") else None,
                   payer=str(values["payer"]).strip() if values.get("payer") else None,
                   time=parse_time(values.get("time")))


async def _check_receipt_page(r: Receipt) -> tuple[Receipt | None, str]:
    """Opens the receipt link like a phone would and reads it. Returns (receipt or None, page text)."""
    import parsing  # local import: parsing imports this module
    import receipt_browser

    page = await receipt_browser.open_page(r.url)
    text = page.all_text()
    found = parse_text(page.text)                       # "ETB 100.00 has been debited from … for …"
    found.merge(parse_json_payloads(page.json_payloads))
    found.merge(parse_cbe_pdf(page.text, None))         # "Receiver … Transferred Amount …" style labels
    for pdf in page.pdfs:
        pdf_text = _pdf_text(pdf)
        found.merge(parse_text(pdf_text)).merge(parse_cbe_pdf(pdf_text, None))
        text += "\n" + pdf_text
    if not found.ref:
        m = re.search(r"\bFT[A-Z0-9]{10}\b", text)
        found.ref = m.group(0) if m else None
    if found.amount is None:
        found.amount = parsing.find_amount(page.text)
    if not found.ref and found.amount is None:
        return None, text
    found.bank, found.url = "CBE", r.url
    return found, text


def _compare(bank: str, found: Receipt, claimed: float | None, expected_ref: str | None = None,
             accounts: list[str] | None = None, names: list[str] | None = None) -> BankCheck:
    m = config.money
    problems = []
    if found.status and found.status.lower() not in ("completed", "success", "successful"):
        problems.append(f"status is '{found.status}'")
    if expected_ref and found.ref and expected_ref != found.ref:
        problems.append(f"bank code {found.ref} ≠ code on the receipt {expected_ref}")
    if found.amount is not None and claimed is not None and abs(found.amount - claimed) > 0.009:
        problems.append(f"bank amount {m(found.amount)} ≠ claimed {m(claimed)}")
    if receiver_ok(found, accounts, names) is False:
        problems.append(f"paid to {receiver_label(found)}, not the business account")
    if found.time and config.now_utc() - found.time > timedelta(hours=config.MAX_RECEIPT_AGE_HOURS):
        problems.append(f"payment date {found.time:%Y-%m-%d %H:%M} is old")
    if problems:
        return BankCheck("mismatch", f"{bank}: " + "; ".join(problems), found)
    msg = f"{bank} confirmed"
    msg += f": {m(found.amount)}" if found.amount is not None else " (amount not readable)"
    if found.receiver:
        msg += f" to {found.receiver}"
    return BankCheck("ok", msg, found)


async def check_online(r: Receipt, claimed: float | None, accounts: list[str] | None = None,
                       names: list[str] | None = None) -> BankCheck:
    """Looks the transaction up on the bank's own site and compares amount, receiver and date."""
    bank = r.bank or guess_bank_from_ref(r.ref)
    if not config.BANK_CHECK:
        return BankCheck("unsupported", "Automatic bank check is turned off")

    if is_browser_link(r.url):
        import receipt_browser
        if not receipt_browser.AVAILABLE:
            return BankCheck("unsupported", f"{bank}: receipt browser not installed — run start.bat again")
        try:
            found, text = await _check_receipt_page(r)
        except Exception as e:  # noqa: BLE001 - browser/network problems
            log.warning("Opening receipt link %s failed: %r", r.url, e)
            return BankCheck("error", f"{bank}: could not open the receipt link ({e.__class__.__name__}) — try again")
        if found is None:
            if re.search(r"not found|invalid|expired|does not exist", text, re.I):
                return BankCheck("not_found", f"{bank}: the bank says this receipt does not exist")
            return BankCheck("error", f"{bank}: receipt page opened but couldn't be read (saved for review)")
        return _compare(bank, found, claimed, r.ref, accounts=accounts, names=names)

    if not r.ref:
        if r.url:
            return BankCheck("unsupported", f"{bank or 'Bank'}: transaction code not read — open the bank receipt link "
                                            "to check, or have the waiter type the code")
        return BankCheck("unsupported", "No transaction code to check")

    cbe_accs = [re.sub(r"\D", "", a) for a in (accounts or config.CBE_ACCOUNTS) if len(re.sub(r"\D", "", a)) == 13 and re.sub(r"\D", "", a).startswith("1")]
    if bank == "CBE" and not (r.suffix or cbe_accs):
        return BankCheck("unsupported", "CBE: add your 13-digit CBE account to enable automatic bank checks")
    
    if bank == "CBE":
        checker_func = lambda rec: _check_cbe(rec, cbe_accounts=cbe_accs)
    elif bank == "telebirr":
        checker_func = _check_telebirr
    else:
        checker_func = None

    if checker_func is None:
        return BankCheck("unsupported", f"{bank or 'This bank'}: no automatic check — owner checks manually")
    try:
        found = await checker_func(r)
    except asyncio.TimeoutError:
        return BankCheck("error", f"{bank}: bank site did not answer (try again later)")
    except aiohttp.ClientError as e:
        log.warning("%s check failed for %s: %r", bank, r.ref, e)
        return BankCheck("error", f"{bank}: could not reach the bank site ({e.__class__.__name__})")
    if found is None:
        return BankCheck("not_found", f"{bank}: transaction {r.ref} NOT FOUND at the bank")
    return _compare(bank, found, claimed, accounts=accounts, names=names)
