"""Summary text and CSV export shared by the bot and the dashboard."""
import csv
import html
import io

import config
import db


def format_summary(s: dict) -> str:
    m = config.money
    lines = [
        f"📊 <b>Supermarket Daily Sales — {s['day']}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🧾 Total Transactions: <b>{s['count']}</b>",
        f"💰 <b>Total Sales Claimed:</b> <b>{m(s['claimed']['total'])}</b> ({s['claimed']['count']} sales)",
        f"✅ <b>Total Verified:</b> {m(s['verified']['total'])} ({s['verified']['count']})",
        f"⏳ Pending Review: {m(s['pending']['total'])} ({s['pending']['count']})",
    ]
    if s["rejected"]["count"]:
        lines.append(f"❌ Rejected / Invalid: {m(s['rejected']['total'])} ({s['rejected']['count']})")
    if s["bank_confirmed"]["count"]:
        lines.append(f"🏦 Bank Confirmed: {m(s['bank_confirmed']['total'])} ({s['bank_confirmed']['count']})")
    if s["problems"]["count"]:
        lines.append(f"🚩 <b>Problems Flagged:</b> {s['problems']['count']} · {m(s['problems']['total'])}")
    if s["duplicates"]["count"]:
        lines.append(f"🚨 <b>Duplicate Attempts:</b> {s['duplicates']['count']} · {m(s['duplicates']['total'])}")
    if s["missing_amount"]:
        lines.append(f"❓ Missing amount: {s['missing_amount']}")
    if s["missing_ref"]:
        lines.append(f"❓ Missing transaction code: {s['missing_ref']}")
    if s["bank_total"] is not None:
        diff = s["difference"]
        icon = "✅" if abs(diff) < 0.01 else "🔴"
        lines.append(f"🏦 Bank Statement Total: {m(s['bank_total'])} → Difference {icon} <b>{m(diff)}</b>")

    if s.get("by_bank"):
        lines.append("\n💳 <b>By Payment System / Bank:</b>")
        for b in s["by_bank"]:
            lines.append(f"• <b>{html.escape(b['bank'])}:</b> {m(b['total'])} ({b['count']} sales)")

    if s.get("by_waiter"):
        lines.append("\n🛒 <b>By Cashier / Counter:</b>")
        for w in s["by_waiter"]:
            verif_str = f" (✅ {m(w['verified'])} verified)" if w["verified"] else ""
            lines.append(f"• <b>{html.escape(w['name'])}:</b> {m(w['total'])} ({w['count']} sales{verif_str})")

    return "\n".join(lines)


def csv_bytes(day: str) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "day", "time", "waiter", "bank", "transaction_ref", "amount", "status",
                     "bank_check", "bank_check_details", "problems", "payer", "receiver", "receiver_account",
                     "receipt_time", "duplicate_of", "type", "receipt_link", "qr_data", "text", "note"])
    for p in db.payments_for_day(day):
        writer.writerow([p["id"], p["day"], config.local_datetime(p["created_at"]), p["waiter_name"],
                         p["bank"] or "", p["tx_ref"] or "", "" if p["amount"] is None else f"{p['amount']:.2f}",
                         p["status"], p["bank_check"] or "", p["bank_check_msg"] or "", p["flags"] or "",
                         p["payer"] or "", p["receiver"] or "", p["receiver_account"] or "",
                         config.local_datetime(p["receipt_time"]) if p["receipt_time"] else "",
                         p["duplicate_of"] or "", p["kind"], p["receipt_url"] or "", p["qr_data"] or "",
                         p["raw_text"] or "", p["note"] or ""])
    return ("﻿" + buf.getvalue()).encode("utf-8")  # BOM so Excel opens it correctly
