"""Summary text and CSV export shared by the bot and the dashboard."""
import csv
import html
import io

import config
import db


def format_summary(s: dict) -> str:
    biz_name = s.get("business_name") or "Supermarket"
    curr = s.get("currency") or config.CURRENCY or "ETB"
    period_type = s.get("period_type", "day")
    val = s.get("period_value") or s.get("day", "")

    if period_type == "year":
        period_lbl = f"Yearly Report — {val}"
    elif period_type == "month":
        period_lbl = f"Monthly Report — {val}"
    else:
        period_lbl = f"Daily Report — {val}"

    m = lambda val: f"{val:,.2f} {curr}" if val is not None else f"0.00 {curr}"

    lines = [
        f"📊 <b>{html.escape(biz_name)} — {period_lbl}</b>",
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
    if s.get("bank_total") is not None:
        diff = s["difference"]
        icon = "✅" if abs(diff) < 0.01 else "🔴"
        lines.append(f"🏦 Bank Statement Total: {m(s['bank_total'])} → Difference {icon} <b>{m(diff)}</b>")

    if s.get("by_bank"):
        lines.append("\n💳 <b>By Payment System / Bank:</b>")
        for b in s["by_bank"]:
            lines.append(f"• <b>{html.escape(b['bank'])}:</b> {m(b['total'])} ({b['count']} sales)")

    if s.get("by_waiter"):
        lines.append("\n🛒 <b>By Cashier / Waiter:</b>")
        for w in s["by_waiter"]:
            verif_str = f" (✅ {m(w['verified'])} verified)" if w["verified"] else ""
            lines.append(f"• <b>{html.escape(w['name'])}:</b> {m(w['total'])} ({w['count']} sales{verif_str})")

    if s.get("by_customer"):
        lines.append("\n👤 <b>Top Customers:</b>")
        for c in s["by_customer"][:5]:
            lines.append(f"• <b>{html.escape(c['name'])}:</b> {m(c['total'])} ({c['count']} payments)")

    return "\n".join(lines)


def csv_bytes(period_value: str, business_id: int | None = None, period_type: str = "day") -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "business_id", "business_name", "day", "time", "waiter", "customer_payer",
                     "bank", "transaction_ref", "amount", "currency", "status", "bank_check",
                     "bank_check_details", "problems", "receiver", "receiver_account",
                     "receipt_time", "duplicate_of", "type", "receipt_link", "qr_data", "text", "note"])
    payments = db.payments_for_period(business_id, period_type, period_value)
    for p in payments:
        writer.writerow([
            p["id"],
            p.get("business_id") or "",
            p.get("business_name") or "",
            p["day"],
            config.local_datetime(p["created_at"]),
            p.get("waiter_name") or "",
            p.get("payer") or "",
            p.get("bank") or "",
            p.get("tx_ref") or "",
            "" if p.get("amount") is None else f"{p['amount']:.2f}",
            p.get("business_currency") or config.CURRENCY,
            p["status"],
            p.get("bank_check") or "",
            p.get("bank_check_msg") or "",
            p.get("flags") or "",
            p.get("receiver") or "",
            p.get("receiver_account") or "",
            config.local_datetime(p["receipt_time"]) if p.get("receipt_time") else "",
            p.get("duplicate_of") or "",
            p["kind"],
            p.get("receipt_url") or "",
            p.get("qr_data") or "",
            p.get("raw_text") or "",
            p.get("note") or "",
        ])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM so Excel opens UTF-8 correctly
