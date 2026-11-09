"""Test a receipt without Telegram: shows what the bot reads from an image or text, and the bank check.

    python test_receipt.py path\\to\\screenshot.png ["optional caption"]
    python test_receipt.py --text "FT263113HT19 100"
    python test_receipt.py --no-bank ...     (skip the online bank check)
"""
import asyncio
import os
import sys

# Lets the tool run before .env is filled in.
os.environ.setdefault("BOT_TOKEN", "0:test")
os.environ.setdefault("OWNER_IDS", "1")

import banks  # noqa: E402
import config  # noqa: E402
import parsing  # noqa: E402


def show(r: banks.Receipt, flags: list[str]) -> None:
    print("\n=== What the bot reads ===")
    for label, value in [("Bank", r.bank), ("Transaction code", r.ref), ("Amount", config.money(r.amount)),
                         ("Payer", r.payer), ("Receiver", r.receiver), ("Receiver account", r.receiver_account),
                         ("Receipt time", r.time.strftime("%Y-%m-%d %H:%M") if r.time else None),
                         ("Official receipt link", r.url)]:
        print(f"  {label:22} {value if value not in (None, '') else '-'}")
    print("\n=== Checks on the receipt ===")
    ok = banks.receiver_ok(r)
    print("  Sent to your account: ", {True: "YES", False: "NO !!", None: "unknown (set MY_ACCOUNTS / MY_NAMES)"}[ok])
    for f in flags:
        print("  FLAG:", f)
    if not flags:
        print("  No problems found")


async def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--no-bank"]
    bank_check = "--no-bank" not in sys.argv
    if not args:
        print(__doc__)
        return
    if args[0] == "--text":
        (r, _), *rest = parsing.split_entries(" ".join(args[1:]))
        if rest:
            print(f"(found {len(rest) + 1} entries; showing the first)")
    else:
        path, caption = args[0], " ".join(args[1:])
        if not os.path.exists(path):
            sys.exit(f"File not found: {path}")
        info = parsing.analyze_image(path, caption)
        r = info["receipt"]
        print("=== QR codes found ===")
        for q in info["qr_data"] or ["(none)"]:
            print("  ", q)
        print("OCR (text reading):", "on" if parsing.OCR_AVAILABLE else "off — install Tesseract + pytesseract")
        for n in info["notes"]:
            print("  NOTE:", n)
    if not r.bank:
        r.bank = banks.guess_bank_from_ref(r.ref)
    show(r, banks.offline_flags(r))

    if bank_check:
        print("\n=== Bank website check ===")
        result = await banks.check_online(r, r.amount)
        print(f"  {result.status.upper()}: {result.message}")
        if result.receipt and result.receipt.url:
            print("  Receipt:", result.receipt.url)


if __name__ == "__main__":
    asyncio.run(main())
