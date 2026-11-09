"""Read QR codes / screenshot text and pull out transaction references and amounts."""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, urlparse

import cv2
import numpy as np

import banks

try:  # optional: OCR of receipt screenshots (needs the Tesseract program installed)
    import os
    import shutil

    import pytesseract

    if not shutil.which("tesseract"):  # Windows installer doesn't add itself to PATH
        for candidate in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                          os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe")):
            if os.path.exists(candidate):
                pytesseract.pytesseract.tesseract_cmd = candidate
                break
    pytesseract.get_tesseract_version()
    OCR_AVAILABLE = True
except Exception:  # noqa: BLE001
    OCR_AVAILABLE = False

CURRENCIES = r"ETB|BIRR|BR|USD|KES|KSH|NGN|GHS|UGX|TZS|RWF|ZAR|INR|PKR|EUR|GBP|AED|SAR"
SYMBOLS = r"[$€£₦₹]"
NUM = r"(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"

AMOUNT_LABEL_RE = re.compile(rf"\b(?:amount|total|paid|debited|credited)\b[^\d\n]{{0,15}}?{NUM}", re.I)
CURRENCY_AMOUNT_RE = re.compile(
    rf"(?<![A-Za-z])(?:{CURRENCIES})\.?:?\s?{NUM}|{SYMBOLS}\s?{NUM}|{NUM}\s?(?:{CURRENCIES})(?![A-Za-z])", re.I
)
PLAIN_AMOUNT_RE = re.compile(
    rf"\s*(?:(?:{CURRENCIES})\.?\s?|{SYMBOLS}\s?)?{NUM}\s*(?:{CURRENCIES})?\.?\s*", re.I
)
REF_LABEL_RE = re.compile(
    r"\b(?:transaction|trans|txn|trx|tx|reference|ref|receipt|confirmation|invoice|tid)\b\.?\s*"
    r"(?:id|no|number|code|#)?\.?\s*[:#=\-]?\s*([A-Z0-9][A-Z0-9\-]{3,39})",
    re.I,
)
TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z0-9][A-Z0-9\-]{5,39}(?![A-Za-z0-9])", re.I)
DATE_RE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}-\d{1,2}-\d{4}")

REF_KEYS = {"id", "ref", "reference", "tx", "txn", "trx", "txid", "tid", "trxid", "transaction",
            "transactionid", "transaction_id", "receipt", "receiptno", "refno", "reference_no"}
AMOUNT_KEYS = {"amount", "amt", "total", "value"}


# ------------------------------------------------------------------ helpers
def _first_group(m: re.Match) -> str | None:
    return next((g for g in m.groups() if g), None)


def to_amount(s: str | None) -> float | None:
    if not s:
        return None
    try:
        v = float(str(s).replace(",", ""))
    except ValueError:
        return None
    return round(v, 2) if 0 < v < 1e10 else None


def looks_like_ref(token: str, strict: bool = False) -> bool:
    t = token.strip("-")
    if not 6 <= len(t) <= 40 or not any(c.isdigit() for c in t) or DATE_RE.fullmatch(t):
        return False
    if not any(c.isalpha() for c in t):
        # digits-only references must be long when we're guessing without a label
        return len(t.replace("-", "")) >= (10 if strict else 6)
    return True


def normalize_ref(ref: str) -> str:
    return ref.strip("-").upper()


def find_ref(text: str, fallback: bool = True) -> str | None:
    for m in REF_LABEL_RE.finditer(text or ""):
        if looks_like_ref(m.group(1)):
            return normalize_ref(m.group(1))
    if fallback:
        for m in TOKEN_RE.finditer(text or ""):
            if looks_like_ref(m.group(0), strict=True):
                return normalize_ref(m.group(0))
    return None


NOT_THE_PAYMENT_RE = re.compile(
    r"balance|charge|fee|vat|commission|disaster|levy|total amount debited|total paid|debited:", re.I)


def find_amount(text: str) -> float | None:
    """The transferred amount — skips fees, VAT, balances and 'total debited' (which includes fees)."""
    text = text or ""
    for rx in (AMOUNT_LABEL_RE, CURRENCY_AMOUNT_RE):
        for m in rx.finditer(text):
            number_start = next(m.start(i) for i in range(1, len(m.groups()) + 1) if m.group(i))
            if NOT_THE_PAYMENT_RE.search(text[max(0, m.start() - 30):number_start]):
                continue
            value = to_amount(_first_group(m))
            if value:
                return value
    return None


def plain_amount(text: str) -> float | None:
    """'450', '1,250.50', 'ETB 300' -> number. Long digit strings are treated as codes, not amounts."""
    m = PLAIN_AMOUNT_RE.fullmatch(text or "")
    if not m:
        return None
    digits = m.group(1).replace(",", "").split(".")[0]
    return to_amount(m.group(1)) if len(digits) <= 8 else None


# ------------------------------------------------------------------ typed messages
def parse_text_entry(text: str) -> tuple[str | None, float | None]:
    """'FT25123ABC 450' / '450 FT25123ABC' / a forwarded bank SMS -> (ref, amount)."""
    text = (text or "").strip()
    ref, amount = find_ref(text, fallback=False), find_amount(text)
    if ref or amount:
        return ref or find_ref(text, fallback=True), amount

    tokens = [t.strip(".;:") for t in text.split()]
    ref_tok = next((t for t in tokens if looks_like_ref(t)), None)
    for t in tokens:
        if t is ref_tok:
            continue
        if re.fullmatch(NUM, t):
            amount = to_amount(t)
            break
    return (normalize_ref(ref_tok) if ref_tok else None), amount


def text_receipt(text: str) -> banks.Receipt:
    """Receipt info from typed/forwarded text: bank SMS wording first, then 'CODE AMOUNT'."""
    r = banks.parse_text(text)
    if not r.ref or r.amount is None:
        ref, amount = parse_text_entry(text)
        r.merge(banks.Receipt(ref=ref, amount=amount))
    return r


def split_entries(text: str) -> list[tuple[banks.Receipt, str]]:
    """One bank SMS / labelled message = one entry; otherwise every line is an entry."""
    text = (text or "").strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) <= 1 or banks.parse_text(text).ref or find_ref(text, fallback=False):
        return [(text_receipt(text), text)]
    return [(text_receipt(ln), ln) for ln in lines]


# ------------------------------------------------------------------ QR codes
def _read_image(path: str):
    data = np.fromfile(path, dtype=np.uint8)  # works with non-ASCII Windows paths
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def decode_qr(path: str) -> list[str]:
    img = _read_image(path)
    if img is None:
        return []
    det = cv2.QRCodeDetector()
    found: list[str] = []
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    big = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    for candidate in (img, gray, big):
        try:
            ok, decoded, _, _ = det.detectAndDecodeMulti(candidate)
            if ok:
                found += [d for d in decoded if d]
        except cv2.error:
            pass
        if not found:
            try:
                d, _, _ = det.detectAndDecode(candidate)
                if d:
                    found.append(d)
            except cv2.error:
                pass
        if found:
            break
    return list(dict.fromkeys(found))


def _parse_tlv(s: str) -> dict[str, str]:
    out, i = {}, 0
    while i + 4 <= len(s):
        tag, length = s[i:i + 2], s[i + 2:i + 4]
        if not length.isdigit():
            break
        n = int(length)
        value = s[i + 4:i + 4 + n]
        if len(value) < n:
            break
        out[tag] = value
        i += 4 + n
    return out


def analyze_qr(data: str) -> tuple[str | None, float | None, str | None]:
    """Returns (ref, amount, note) for one decoded QR string."""
    s = data.strip()

    if s.startswith("000201"):  # EMVCo payment QR (merchant / bank QR standard)
        tlv = _parse_tlv(s)
        extra = _parse_tlv(tlv.get("62", ""))
        ref = extra.get("05") or extra.get("01") or extra.get("07")
        amount = to_amount(tlv.get("54"))
        note = None if (ref or amount) else "This is a merchant payment QR, not a payment receipt."
        return (normalize_ref(ref) if ref and looks_like_ref(ref) else None), amount, note

    if s.lower().startswith(("http://", "https://")):
        official = banks.from_url(s)
        if official:
            return official.ref, official.amount, None
        url = urlparse(s)
        params = parse_qsl(url.query)
        ref = next((v for k, v in params if k.lower() in REF_KEYS and looks_like_ref(v)), None)
        amount = next((to_amount(v) for k, v in params if k.lower() in AMOUNT_KEYS and to_amount(v)), None)
        if not ref:
            ref = next((seg for seg in reversed(url.path.split("/")) if seg and looks_like_ref(seg)), None)
        return (normalize_ref(ref) if ref else None), amount, None

    try:
        obj = json.loads(s)
    except ValueError:
        obj = None
    if isinstance(obj, dict):
        flat = {str(k).lower(): str(v) for k, v in obj.items() if not isinstance(v, (dict, list))}
        ref = next((v for k, v in flat.items() if k in REF_KEYS and looks_like_ref(v)), None)
        amount = next((to_amount(v) for k, v in flat.items() if k in AMOUNT_KEYS and to_amount(v)), None)
        return (normalize_ref(ref) if ref else None), amount, None

    return find_ref(s), find_amount(s), None


def ocr_text(path: str) -> str:
    if not OCR_AVAILABLE:
        return ""
    img = _read_image(path)
    if img is None:
        return ""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    try:
        return pytesseract.image_to_string(gray)
    except Exception:  # noqa: BLE001
        return ""


def analyze_image(path: str, caption: str = "") -> dict:
    """Everything we can learn from one receipt photo + the waiter's caption.

    Returns {"receipt": banks.Receipt, "qr_data": [...], "ocr_text": str, "notes": [...]}
    """
    result = {"qr_data": [], "ocr_text": "", "notes": [], "tamper_flags": []}
    from_image = banks.Receipt()
    for q in decode_qr(path):
        result["qr_data"].append(q)
        official = banks.from_url(q) if q.lower().startswith(("http://", "https://")) else None
        if official:
            from_image.merge(official)
            continue
        ref, amount, note = analyze_qr(q)
        from_image.merge(banks.Receipt(ref=ref, amount=amount))
        if note:
            result["notes"].append(note)

    # Screenshot text: amount, receiver and date are usually only written there, not in the QR.
    if OCR_AVAILABLE:
        result["ocr_text"] = ocr_text(path)
        ocr = banks.parse_text(result["ocr_text"])
        if not ocr.ref:
            ocr.ref = find_ref(result["ocr_text"], fallback=False)
        if ocr.amount is None:
            ocr.amount = find_amount(result["ocr_text"])
        if ocr.ref and from_image.ref and ocr.ref != from_image.ref:
            alert = f"Tamper Alert: Code on screen ({ocr.ref}) differs from the QR code ({from_image.ref})!"
            result["notes"].append(alert)
            result["tamper_flags"].append(alert)
        from_image.merge(ocr)

    caption_r = text_receipt(caption) if caption else banks.Receipt()
    if caption_r.amount and from_image.amount and abs(caption_r.amount - from_image.amount) > 0.009:
        result["notes"].append(
            f"Waiter wrote {caption_r.amount:,.2f} but the receipt shows {from_image.amount:,.2f}.")
    receipt = banks.Receipt(amount=caption_r.amount).merge(from_image).merge(caption_r)
    if not receipt.bank:
        receipt.bank = banks.guess_bank_from_ref(receipt.ref)
    result["receipt"] = receipt
    return result
