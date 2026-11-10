"""Telegram bot: waiters submit receipts, owners get alerts, summaries, PDF reports and the dashboard."""
import asyncio
import hmac
import html
import logging
import re
import uuid
from datetime import date, time as dtime, timedelta

from telegram import (BotCommand, BotCommandScopeChat, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                      MenuButtonWebApp, Update, WebAppInfo)
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

import banks
import config
import db
import parsing
import pdf_report
import reports
import verify

log = logging.getLogger(__name__)
H = html.escape

WAITER_HELP = (
    "👋 <b>Cashier / Waiter Guide</b>\n\n"
    "📸 <b>Submit Transactions:</b>\n"
    "• Take a photo of the customer's payment screen (CBE, telebirr, BoA, any bank) and send it directly to this chat.\n"
    "• Or forward the bank SMS confirmation, or type the transaction code and amount (e.g. <code>FT263113HT19 450</code>).\n\n"
    "🔒 <b>Anti-Fake Protection:</b>\n"
    "The bot automatically verifies if the receipt is genuine and confirms the money went to your store owner's CBE / telebirr account.\n\n"
    "📱 <b>Your Dashboard:</b>\n"
    "Tap the <b>📱 Open Dashboard</b> button below at any time to view your sales and transactions today."
)

OWNER_HELP = (
    "🏢 <b>Store Owner Portal</b>\n\n"
    "Welcome! You can manage everything directly inside your <b>interactive WebApp dashboard</b> — no slash commands needed!\n\n"
    "📱 <b>Tap '📱 Open Dashboard & Store Settings' below to:</b>\n"
    "• 📊 <b>Sales & Reports:</b> View daily, monthly, and yearly sales totals, top customers, cashier performance, and download PDF / Excel reports.\n"
    "• 🏦 <b>Accounts Setup:</b> Enter your CBE 13-digit account, telebirr phone/till number, and legal store names using simple input fields.\n"
    "• 👥 <b>Cashiers:</b> Add cashiers by their Telegram ID or username, approve pending join requests, or deactivate cashiers with 1 tap.\n\n"
    "🔒 <i>All customer payments must go to your registered accounts; payments to other accounts are automatically rejected as invalid.</i>"
)

SUPERADMIN_HELP = (
    "👑 <b>Platform Super Admin Portal</b>\n\n"
    "Welcome! You are the platform owner and control all businesses and clients.\n\n"
    "📱 <b>Everything is managed through interactive forms in your WebApp:</b>\n"
    "• ⚡ <b>Onboard Businesses:</b> Open the dashboard → Go to <b>⚡ Super Admin</b> tab → Enter the client's store name, owner Telegram ID, CBE, and telebirr.\n"
    "• 🏢 <b>Assign Owners:</b> Give cafe and supermarket owners full management access.\n"
    "• 🏦 <b>Account & Fraud Rules:</b> Set receiving accounts so fraud is blocked automatically.\n"
    "• 📊 <b>Platform-Wide Reports:</b> Monitor all transactions, daily/monthly revenue, and export PDF statements.\n\n"
    "<i>Tap the button below to open your dashboard:</i>"
)

CHECK_ICONS = {"ok": "✅", "mismatch": "🔴", "not_found": "🔴", "error": "⚠️", "unsupported": "ℹ️"}


# ------------------------------------------------------------------ permission helpers
def is_superadmin(uid: int) -> bool:
    return db.is_superadmin(uid)


def is_any_owner(uid: int) -> bool:
    if is_superadmin(uid):
        return True
    return len(db.get_user_owned_businesses(uid)) > 0


def get_active_business(uid: int) -> dict | None:
    u = db.get_user(uid)
    if u and u.get("active_business_id"):
        biz = db.get_business(u["active_business_id"])
        if biz and biz["active"]:
            return biz
    owned = db.get_user_owned_businesses(uid)
    if owned:
        db.set_user_active_business(uid, owned[0]["id"])
        return owned[0]
    waiter_biz = db.get_waiter_businesses(uid)
    if waiter_biz:
        db.set_user_active_business(uid, waiter_biz[0]["id"])
        return waiter_biz[0]
    default_biz = db.get_business(1)
    if default_biz:
        return default_biz
    all_b = db.list_businesses()
    return all_b[0] if all_b else None


def can_submit(uid: int) -> bool:
    if is_superadmin(uid) or is_any_owner(uid):
        return True
    active_jobs = db.get_waiter_businesses(uid)
    return len(active_jobs) > 0


def dashboard_markup() -> InlineKeyboardMarkup | None:
    if not config.WEBAPP_URL:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📱 Open Dashboard & Store Settings", web_app=WebAppInfo(url=config.WEBAPP_URL + "/"))]]
    )


async def notify_business_owners(bot, business_id: int, text: str, markup=None, photo_path=None) -> None:
    """Delivers alerts specifically to the owners of the business."""
    owners = db.get_business_owners(business_id)
    owner_ids = {o["tg_id"] for o in owners}
    if not owner_ids:
        owner_ids = set(config.OWNER_IDS)

    photo_file = (config.PHOTOS_DIR / photo_path) if photo_path else None

    for oid in owner_ids:
        try:
            if photo_file and photo_file.is_file():
                if len(text) <= 1024:
                    with open(photo_file, "rb") as pf:
                        await bot.send_photo(oid, photo=pf, caption=text, parse_mode="HTML", reply_markup=markup)
                else:
                    with open(photo_file, "rb") as pf:
                        await bot.send_photo(oid, photo=pf, caption=text[:1000] + "…", parse_mode="HTML")
                    await bot.send_message(oid, text, parse_mode="HTML", reply_markup=markup)
            else:
                await bot.send_message(oid, text, parse_mode="HTML", reply_markup=markup)
        except TelegramError as e:
            log.warning("Could not message owner %s for business %s: %s", oid, business_id, e)


def parse_period_arg(args: list[str]) -> tuple[str, str] | tuple[None, None]:
    """Parses [today | yesterday | YYYY-MM-DD | YYYY-MM | YYYY]. Returns (period_type, value)."""
    if not args:
        return "day", config.business_day()
    a = args[0].strip().lower()
    if a in ("today", "day"):
        return "day", config.business_day()
    if a == "yesterday":
        val = (date.fromisoformat(config.business_day()) - timedelta(days=1)).isoformat()
        return "day", val
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", a):
        return "day", a
    if re.fullmatch(r"\d{4}-\d{2}", a):
        return "month", a
    if re.fullmatch(r"\d{4}", a):
        return "year", a
    return None, None


async def check_duplicate(bot, pid: int, ref: str | None, who: str, business_id: int, url: str | None = None) -> str:
    """Flags entry if tx_ref or QR link was submitted before. Detects cross-store duplicate attempts."""
    dup, same_biz = (None, True)
    what = ""
    if ref:
        dup, same_biz = db.find_duplicate(ref, pid, business_id)
        what = f"code <code>{H(ref)}</code>"
    if not dup and url:
        dup, same_biz = db.find_duplicate_url(url, pid, business_id)
        what = "this receipt (same QR code)"

    if not dup:
        return ""

    db.update_payment(pid, duplicate_of=dup["id"])

    if same_biz:
        warning = (f"⚠️ <b>DUPLICATE</b>: {what} was already sent as #{dup['id']} "
                   f"by {H(dup['waiter_name'])} ({config.local_datetime(dup['created_at'])}).")
    else:
        warning = (f"🚨 <b>CROSS-STORE FRAUD ALERT</b>: {what} was already redeemed at "
                   f"<b>{H(dup.get('business_name') or 'another business')}</b> (#{dup['id']} on {dup['day']})!")

    await notify_business_owners(bot, business_id, f"🚨 {warning}\nNew entry #{pid} from {H(who)}.")
    return warning


async def alert_flags(bot, pid: int, flags: list[str], who: str, business_id: int) -> None:
    if flags:
        await notify_business_owners(bot, business_id, f"🚩 <b>Check entry #{pid}</b> from {H(who)}:\n" +
                                     "\n".join(f"• {H(f)}" for f in flags))


def _receiver(p: dict) -> str | None:
    if not (p.get("receiver") or p.get("receiver_account")):
        return None
    return banks.receiver_label(banks.Receipt(receiver=p.get("receiver"), receiver_account=p.get("receiver_account")))


def entry_text(p: dict, extra: list[str], checking: bool, title: str = "Saved") -> str:
    biz_name = p.get("business_name") or ""
    biz_header = f" [{H(biz_name)}]" if biz_name else ""
    curr = p.get("business_currency") or config.CURRENCY

    lines = [
        f"🧾 <b>{title} #{p['id']}</b>{biz_header}" + (f" · {H(p['bank'])}" if p.get("bank") else ""),
        f"Code: <code>{H(p['tx_ref'])}</code>" if p["tx_ref"] else "Code: ❓ not found",
        f"Amount: <b>{config.money(p['amount'], curr)}</b>" if p["amount"] is not None else "Amount: ❓ not found"
    ]
    if p.get("payer"):
        lines.append(f"From: <b>{H(p['payer'])}</b>")
    if _receiver(p):
        lines.append(f"To: {H(_receiver(p))}")
    lines += [f"🚩 {H(f)}" for f in (p.get("flags") or "").split("; ") if f]
    lines += [x for x in extra if x]
    if p.get("bank_check_msg"):
        lines.append(f"{CHECK_ICONS.get(p['bank_check'], 'ℹ️')} {H(p['bank_check_msg'])}")
    elif checking:
        lines.append("🏦 <i>Checking with the bank…</i>")
    if p["status"] == "verified":
        lines.append("✅ <b>Verified Real</b>")
    elif p["status"] == "rejected":
        lines.append("❌ <b>REJECTED / INVALID</b>")
    if (p["tx_ref"] is None or p["amount"] is None) and not checking and p.get("bank_check") != "ok":
        lines.append("\n↩️ <i>Reply to this message with the missing amount and/or code.</i>")
    return "\n".join(lines)


def batch_text(pids: list[int], extras: dict, checking: bool, footer: list[str]) -> str:
    if len(pids) == 1 and not footer:
        return entry_text(db.get_payment(pids[0]), extras.get(pids[0], []), checking)
    lines = []
    for pid in pids:
        p = db.get_payment(pid)
        curr = p.get("business_currency") or config.CURRENCY
        line = f"🧾 #{pid} <code>{H(p['tx_ref'])}</code> · " + (
            config.money(p["amount"], curr) if p["amount"] is not None else "❓ amount")
        if p.get("bank_check_msg"):
            line += f"\n   {CHECK_ICONS.get(p['bank_check'], 'ℹ️')} {H(p['bank_check_msg'])}"
        elif checking:
            line += "  🏦…"
        line += "".join(f"\n   🚩 {H(f)}" for f in (p.get("flags") or "").split("; ") if f)
        line += "".join(f"\n   {x}" for x in extras.get(pid, []) if x)
        lines.append(line)
    return "\n".join(lines + footer)


def owner_payment_text(p: dict) -> str:
    m = lambda val: config.money(val, p.get("business_currency") or config.CURRENCY)
    biz_name = p.get("business_name") or "Store"
    who = p.get("waiter_name") or f"Cashier {p['waiter_id']}"
    bank = p.get("bank") or "Unknown Bank"
    amount_str = m(p["amount"]) if p["amount"] is not None else "❓ Amount not detected"
    ref_str = p["tx_ref"] or "❓ Not detected"
    receiver = _receiver(p) or "Not specified"
    payer = p.get("payer") or "Customer"

    check = p.get("bank_check")
    if check == "ok":
        verif = f"✅ <b>Verified Real by Bank</b>\n   ↳ <i>{H(p.get('bank_check_msg', ''))}</i>"
    elif check in ("mismatch", "not_found"):
        verif = f"🔴 <b>FRAUD ALERT / MISMATCH!</b>\n   ↳ <i>{H(p.get('bank_check_msg', ''))}</i>"
    elif p["status"] == "verified":
        verif = "✅ <b>Verified</b>"
    elif p["status"] == "rejected":
        verif = "❌ <b>REJECTED / INVALID</b> (Money went to another account!)"
    elif p.get("duplicate_of"):
        dup = db.get_payment(p["duplicate_of"])
        dup_by = dup["waiter_name"] if dup else "earlier"
        verif = f"🚨 <b>DUPLICATE RECEIPT!</b> (Sent as #{p['duplicate_of']} by {H(dup_by)})"
    else:
        verif = f"⏳ <b>Pending manual check</b> (QR: {'✓' if p.get('qr_data') else 'none'})"

    lines = [
        f"🛒 <b>{H(biz_name)} · Cashier Sale #{p['id']}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"👤 <b>Cashier / Waiter:</b> {H(who)}",
        f"🧑‍💼 <b>Customer:</b> {H(payer)}",
        f"💰 <b>Amount:</b> <b>{amount_str}</b>",
        f"💳 <b>Bank / Method:</b> {H(bank)}",
        f"🔢 <b>Reference:</b> <code>{H(ref_str)}</code>",
        f"📥 <b>Paid To:</b> {H(receiver)}",
        f"🛡️ <b>Status:</b> {verif}",
    ]
    flags = [f for f in (p.get("flags") or "").split("; ") if f]
    if flags:
        lines.append("🚩 <b>Alerts:</b>")
        for f in flags:
            lines.append(f"  • {H(f)}")

    day_summary = db.summary(business_id=p.get("business_id"), period_type="day", period_value=p["day"])
    cashier_stat = next((w for w in day_summary["by_waiter"] if w["waiter_id"] == p["waiter_id"]), None)
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("📊 <b>Today's Running Sales:</b>")
    if cashier_stat:
        lines.append(f"• <b>{H(who)}:</b> {m(cashier_stat['total'])} ({cashier_stat['count']} tx)")
    lines.append(f"• <b>Store Total:</b> <b>{m(day_summary['claimed']['total'])}</b> ({day_summary['count']} tx)")
    if day_summary["verified"]["count"]:
        lines.append(f"• <b>Total Verified:</b> {m(day_summary['verified']['total'])}")

    return "\n".join(lines)


async def finish_checks(bot, pids: list[int], chat_id: int, msg_id: int, extras: dict, who: str,
                        business_id: int, footer: list[str] | None = None) -> None:
    """Background task: check transactions with bank, update message, and alert business owners."""
    footer = footer or []
    dup_before = {pid: db.get_payment(pid)["duplicate_of"] for pid in pids}
    results = await asyncio.gather(*(verify.check_payment(pid) for pid in pids), return_exceptions=True)
    for pid, res in zip(pids, results):
        p = db.get_payment(pid)
        if p["duplicate_of"] and not dup_before[pid]:
            dup = db.get_payment(p["duplicate_of"])
            warning = (f"⚠️ <b>DUPLICATE</b>: code <code>{H(p['tx_ref'])}</code> was already sent as "
                       f"#{dup['id']} by {H(dup['waiter_name'])} ({config.local_datetime(dup['created_at'])}).")
            extras.setdefault(pid, []).append(warning)
            await notify_business_owners(bot, business_id, f"🚨 {warning}\nNew entry #{pid} from {H(who)}.")
        if isinstance(res, Exception):
            log.exception("Bank check crashed for #%s", pid, exc_info=res)
            db.update_payment(pid, bank_check="error", bank_check_msg="Bank check failed (internal error)")
        elif res and res.status in ("mismatch", "not_found"):
            await notify_business_owners(bot, business_id, f"🔴 <b>Bank check failed</b> for #{pid} from {H(who)}:\n{H(res.message)}")

        # Notify business owners with photo & structured receipt card
        photo_path = p.get("photo_path")
        await notify_business_owners(bot, business_id, owner_payment_text(p), photo_path=photo_path)

    try:
        await bot.edit_message_text(batch_text(pids, extras, False, footer), chat_id=chat_id, message_id=msg_id,
                                    parse_mode="HTML")
    except TelegramError as e:
        log.debug("Could not update message: %s", e)


def start_checks(ctx: ContextTypes.DEFAULT_TYPE, pids: list[int], sent, extras: dict, who: str,
                 business_id: int, footer: list[str] | None = None) -> None:
    if config.BANK_CHECK and pids:
        ctx.application.create_task(finish_checks(ctx.bot, pids, sent.chat_id, sent.message_id, extras, who,
                                                  business_id, footer))


# ------------------------------------------------------------------ general commands
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u, msg = update.effective_user, update.effective_message
    is_admin = is_superadmin(u.id)
    owned = db.get_user_owned_businesses(u.id)
    waiter_biz = db.get_waiter_businesses(u.id)

    db.upsert_user(u.id, u.full_name, u.username, "owner" if (is_admin or owned) else "waiter")

    if is_admin:
        await setup_owner_chat(ctx.bot, u.id)
        await msg.reply_html(SUPERADMIN_HELP, reply_markup=dashboard_markup())
    elif owned:
        await setup_owner_chat(ctx.bot, u.id)
        active = get_active_business(u.id)
        name = active["name"] if active else "Your Store"
        await msg.reply_html(f"👋 Welcome back, owner of <b>{H(name)}</b>!\n\n" + OWNER_HELP,
                             reply_markup=dashboard_markup())
    elif waiter_biz:
        active = get_active_business(u.id)
        name = active["name"] if active else "your store"
        await msg.reply_html(f"👋 Welcome back! Active store: <b>{H(name)}</b>\n\n" + WAITER_HELP,
                             reply_markup=dashboard_markup())
    else:
        pending_biz = db.get_user_pending_business_request(u.id)
        if pending_biz:
            await msg.reply_html(
                f"⏳ <b>Your Business Request is Pending Review</b>\n\n"
                f"• Store Name: <b>{H(pending_biz['business_name'])}</b>\n"
                f"• Category: <b>{H(pending_biz.get('category') or 'General')}</b>\n"
                f"• Desired Code: <code>{H(pending_biz.get('code') or 'auto')}</code>\n\n"
                f"Your request has been submitted to the platform admin. "
                f"You will receive a notification as soon as it is approved!",
                reply_markup=dashboard_markup()
            )
        else:
            await msg.reply_html(
                f"👋 <b>Welcome to Kifiya Transaction Checker!</b>\n\n"
                f"Your Telegram User ID is: <code>{u.id}</code>\n\n"
                f"📱 <b>Get Started:</b>\n"
                f"Tap the button below to open the portal and submit a request:\n"
                f"• 🏢 <b>Request to Register as Business Owner</b> (Cafe, Supermarket, Restaurant)\n"
                f"• 🛒 <b>Or Join as a Cashier</b> using your store's code.",
                reply_markup=dashboard_markup()
            )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if is_superadmin(uid):
        await update.effective_message.reply_html(SUPERADMIN_HELP, reply_markup=dashboard_markup())
    elif is_any_owner(uid):
        await update.effective_message.reply_html(OWNER_HELP, reply_markup=dashboard_markup())
    else:
        await update.effective_message.reply_html(WAITER_HELP)


# ------------------------------------------------------------------ waiter: registration & multi-business switching
async def cmd_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Cashier requests to join a business. Requires store owner approval."""
    u, msg = update.effective_user, update.effective_message
    if not ctx.args:
        await msg.reply_html("Usage: <code>/join &lt;CODE&gt; [Counter Name]</code>\nExample: <code>/join bolecafe Counter 1</code>")
        return

    code = ctx.args[0].strip().lower()
    biz = db.get_business_by_code(code)
    if not biz or not biz["active"]:
        await msg.reply_text("❌ Business code not found. Please ask your store owner for the correct code.")
        return

    custom_name = " ".join(ctx.args[1:]).strip() if len(ctx.args) > 1 else ""
    cashier_name = f"{custom_name} ({u.full_name})" if custom_name else u.full_name

    # Check if already active
    if db.is_waiter_active_in_business(biz["id"], u.id):
        await msg.reply_html(f"ℹ️ You are already registered as an active cashier for <b>{H(biz['name'])}</b>.")
        return

    # Save as pending (active=0)
    db.upsert_user(u.id, cashier_name, u.username, "waiter")
    db.add_waiter_to_business(biz["id"], u.id, cashier_name, active=0)

    await msg.reply_html(
        f"⏳ <b>Registration request sent to the owner of {H(biz['name'])}!</b>\n\n"
        f"• Store: <b>{H(biz['name'])}</b>\n"
        f"• Cashier: <b>{H(cashier_name)}</b>\n"
        f"• Your Telegram ID: <code>{u.id}</code>\n\n"
        f"🔒 The business owner must approve your registration before you can submit receipts. "
        f"You will receive a notification as soon as you are approved."
    )

    # Send approval card with inline buttons to all store owners
    owners = db.get_business_owners(biz["id"])
    owner_ids = {o["tg_id"] for o in owners}
    owner_ids.update(config.OWNER_IDS)

    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve Cashier", callback_data=f"waiter_appr:{biz['id']}:{u.id}"),
            InlineKeyboardButton("❌ Decline", callback_data=f"waiter_decl:{biz['id']}:{u.id}")
        ]
    ])

    alert_text = (
        f"👤 <b>New Cashier Registration Request</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• Store: <b>{H(biz['name'])}</b>\n"
        f"• Cashier: <b>{H(cashier_name)}</b> (@{H(u.username or 'none')})\n"
        f"• Telegram ID: <code>{u.id}</code>\n"
        f"• Counter: <b>{H(custom_name or 'Not specified')}</b>\n\n"
        f"Do you want to register this cashier for your store?"
    )

    for oid in owner_ids:
        try:
            await ctx.bot.send_message(oid, alert_text, parse_mode="HTML", reply_markup=markup)
        except TelegramError as e:
            log.warning("Could not message owner %s for waiter approval: %s", oid, e)


async def cmd_switch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch active business when working for multiple stores."""
    u, msg = update.effective_user, update.effective_message
    # Check owned and waiter businesses
    owned = db.get_user_owned_businesses(u.id)
    waiter_biz = db.get_waiter_businesses(u.id)

    # Combine unique businesses
    biz_map = {b["id"]: b for b in (owned + waiter_biz)}
    if not biz_map:
        if is_superadmin(u.id):
            biz_map = {b["id"]: b for b in db.list_businesses()}
        else:
            await msg.reply_html("You haven't joined any businesses yet. Give your ID to your store owner so they can register you.")
            return

    active = get_active_business(u.id)
    active_id = active["id"] if active else None

    if len(biz_map) == 1:
        single = next(iter(biz_map.values()))
        await msg.reply_html(f"You are currently working for <b>{H(single['name'])}</b> (ID: <code>{single['id']}</code>).")
        return

    buttons = []
    for bid, b in biz_map.items():
        is_cur = (bid == active_id)
        label = f"{'✅ ' if is_cur else '🏢 '}{b['name']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"switch_biz:{bid}")])

    markup = InlineKeyboardMarkup(buttons)
    await msg.reply_html("🔄 <b>Select your active business:</b>\nTransactions you submit will belong to the selected store.", reply_markup=markup)


async def on_switch_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data.startswith("switch_biz:"):
        bid = int(data.split(":")[1])
        biz = db.get_business(bid)
        if not biz:
            await query.edit_message_text("Business not found.")
            return
        db.set_user_active_business(query.from_user.id, bid)
        await query.edit_message_text(
            f"✅ Active business switched to <b>{H(biz['name'])}</b>!\n"
            f"Receipts you submit will now be credited to <b>{H(biz['name'])}</b>.",
            parse_mode="HTML"
        )
    elif data.startswith("waiter_appr:"):
        parts = data.split(":")
        biz_id, waiter_id = int(parts[1]), int(parts[2])
        if not (is_superadmin(query.from_user.id) or db.is_business_owner(biz_id, query.from_user.id)):
            await query.answer("Only the store owner can approve cashiers.", show_alert=True)
            return
        biz = db.get_business(biz_id)
        w_user = db.get_user(waiter_id)
        w_name = w_user["name"] if w_user else f"Cashier {waiter_id}"
        db.add_waiter_to_business(biz_id, waiter_id, active=1)
        await query.edit_message_text(
            f"✅ <b>Approved by {H(query.from_user.full_name)}!</b>\n"
            f"Cashier <b>{H(w_name)}</b> (ID: <code>{waiter_id}</code>) is now registered for <b>{H(biz['name'])}</b>.",
            parse_mode="HTML"
        )
        try:
            await ctx.bot.send_message(
                waiter_id,
                f"🎉 <b>Your registration for {H(biz['name'])} was APPROVED by the owner!</b>\n\n"
                f"You can now submit customer receipts for {H(biz['name'])}.\n\n" + WAITER_HELP,
                parse_mode="HTML"
            )
        except TelegramError:
            pass
    elif data.startswith("waiter_decl:"):
        parts = data.split(":")
        biz_id, waiter_id = int(parts[1]), int(parts[2])
        if not (is_superadmin(query.from_user.id) or db.is_business_owner(biz_id, query.from_user.id)):
            await query.answer("Only the store owner can decline cashiers.", show_alert=True)
            return
        db.remove_waiter_from_business(biz_id, waiter_id)
        await query.edit_message_text("❌ <b>Registration declined.</b>", parse_mode="HTML")
        try:
            await ctx.bot.send_message(
                waiter_id,
                "❌ <b>Your registration request was declined by the store owner.</b>",
                parse_mode="HTML"
            )
        except TelegramError:
            pass
    elif data.startswith("biz_req_appr:"):
        req_id = int(data.split(":")[1])
        if not is_superadmin(query.from_user.id):
            await query.answer("Only platform admin can approve business owners.", show_alert=True)
            return
        approved = db.approve_business_request(req_id, query.from_user.id)
        if approved:
            biz = approved.get("business") or {}
            await query.edit_message_text(
                f"✅ <b>Approved by Platform Admin!</b>\n\n"
                f"• Store: <b>{H(approved['business_name'])}</b> (#<code>{biz.get('id')}</code>)\n"
                f"• Owner: <b>{H(approved['user_name'])}</b> (ID: <code>{approved['tg_id']}</code>)\n"
                f"• Cashier Join Code: <code>{H(biz.get('code', ''))}</code>\n\n"
                f"The business owner has been notified and granted full access.",
                parse_mode="HTML"
            )
            try:
                await ctx.bot.send_message(
                    approved["tg_id"],
                    f"🎉 <b>Congratulations! Your Business Owner Request was APPROVED!</b>\n\n"
                    f"• Store: <b>{H(approved['business_name'])}</b>\n"
                    f"• Cashier Join Code: <code>{H(biz.get('code', ''))}</code>\n\n"
                    f"You now have full access as the <b>Store Owner</b>!\n"
                    f"Tap the button below to open your dashboard, set payment accounts, and approve cashiers:",
                    reply_markup=dashboard_markup(),
                    parse_mode="HTML"
                )
            except Exception:
                pass
        else:
            await query.answer("Request not found or already processed.", show_alert=True)
    elif data.startswith("biz_req_decl:"):
        req_id = int(data.split(":")[1])
        if not is_superadmin(query.from_user.id):
            await query.answer("Only platform admin can decline business owners.", show_alert=True)
            return
        rejected = db.reject_business_request(req_id, query.from_user.id)
        if rejected:
            await query.edit_message_text(
                f"❌ <b>Declined:</b> Request for <b>{H(rejected['business_name'])}</b> by {H(rejected['user_name'])} was declined.",
                parse_mode="HTML"
            )
            try:
                await ctx.bot.send_message(
                    rejected["tg_id"],
                    f"ℹ️ Your request to register <b>{H(rejected['business_name'])}</b> as a store owner was not approved at this time.",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        else:
            await query.answer("Request not found or already processed.", show_alert=True)


async def cmd_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u, msg = update.effective_user, update.effective_message
    if not can_submit(u.id):
        await msg.reply_html(
            "You are not registered. Tap below to join your store using their Join Code or register your business:",
            reply_markup=dashboard_markup()
        )
        return
    if not ctx.args:
        await msg.reply_html("Usage: <code>/name Counter 1</code> or <code>/name Counter 2 - Meron</code>")
        return
    new_name = " ".join(ctx.args).strip()
    biz = get_active_business(u.id)
    if biz:
        db.add_waiter_to_business(biz["id"], u.id, new_name, active=1)
    user = db.get_user(u.id)
    role = user["role"] if user else "waiter"
    db.upsert_user(u.id, new_name, u.username, role)
    store_info = f" for <b>{H(biz['name'])}</b>" if biz else ""
    await msg.reply_html(f"✅ Your cashier / counter name is now set to: <b>{H(new_name)}</b>{store_info}")
    if biz:
        await notify_business_owners(ctx.bot, biz["id"], f"👤 Cashier updated name: <b>{H(new_name)}</b> (ID: <code>{u.id}</code>)")


# ------------------------------------------------------------------ waiter: transaction submissions
async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u, msg = update.effective_user, update.effective_message
    if not can_submit(u.id):
        await msg.reply_html(
            f"🔒 <b>You are not an authorized cashier.</b>\n\n"
            f"Your Telegram ID is: <code>{u.id}</code>\n"
            f"Tap below to open the portal and join your store using their Join Code, or request to register as a Business Owner:",
            reply_markup=dashboard_markup()
        )
        return

    biz = get_active_business(u.id)
    if not biz:
        await msg.reply_text("⚠️ No active business selected. Use /switch or ask your store owner to register you.")
        return

    biz_id = biz["id"]
    accounts = [a.strip() for a in (biz["accounts"] or "").split(",") if a.strip()]
    names = [n.strip() for n in (biz["names"] or "").split(",") if n.strip()]

    # If owner hasn't set accounts yet, warn owner
    if not accounts and not names:
        await notify_business_owners(
            ctx.bot, biz_id,
            f"⚠️ <b>WARNING for {H(biz['name'])}:</b> Cashier {H(u.full_name)} submitted a payment, "
            f"but you have NOT set your CBE or telebirr payment accounts yet!\n"
            f"Please open your WebApp Dashboard -> ⚙️ Store Settings to set your accounts so payments can be verified!",
            markup=dashboard_markup()
        )

    tg_file = await (msg.photo[-1].get_file() if msg.photo else msg.document.get_file())
    day = config.business_day()
    folder = config.PHOTOS_DIR / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex}.jpg"
    await tg_file.download_to_drive(path)

    caption = msg.caption or ""
    info = await asyncio.to_thread(parsing.analyze_image, str(path), caption)
    r: banks.Receipt = info["receipt"]

    # STRICT RULE: Transactions must go to the owner's accounts!
    receiver_valid = banks.receiver_ok(r, accounts=accounts, names=names)
    is_fraudulent_receiver = (receiver_valid is False)

    flags = banks.offline_flags(r, accounts=accounts, names=names)
    flags += info.get("tamper_flags", [])

    if is_fraudulent_receiver:
        flags.append(f"🔴 FRAUD: Money was paid to {banks.receiver_label(r)}, NOT {biz['name']}'s registered account!")

    status = "rejected" if is_fraudulent_receiver else "pending"

    pid = db.add_payment(
        business_id=biz_id,
        waiter_id=u.id, day=day, kind="photo",
        status=status,
        reviewed_at=config.now_utc().isoformat() if status == "rejected" else None,
        photo_path=str(path.relative_to(config.PHOTOS_DIR)),
        qr_data="\n".join(info["qr_data"]) or None,
        ocr_text=info["ocr_text"] or None,
        raw_text=caption or None,
        tx_ref=r.ref, amount=r.amount,
        note=f"REJECTED: Paid to {banks.receiver_label(r)}" if is_fraudulent_receiver else (" ".join(info["notes"]) or None),
    )
    verify.save_receipt_fields(pid, r, flags)

    if is_fraudulent_receiver:
        # Transaction is INVALID!
        await msg.reply_html(
            f"❌ <b>REJECTED / INVALID TRANSACTION #{pid}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Paid To: <b>{H(banks.receiver_label(r))}</b>\n"
            f"Amount: <b>{config.money(r.amount, biz.get('currency'))}</b>\n\n"
            f"⚠️ <b>This payment went to a DIFFERENT account, NOT {H(biz['name'])}'s account!</b>\n"
            f"Only payments to the store owner's registered CBE/telebirr account are valid.\n"
            f"This transaction is <b>REJECTED</b> and is <b>NOT counted</b> in sales."
        )
        await notify_business_owners(
            ctx.bot, biz_id,
            f"🚨 <b>FRAUD ALERT: INVALID PAYMENT #{pid}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 Cashier: <b>{H(u.full_name)}</b>\n"
            f"💰 Amount: <b>{config.money(r.amount, biz.get('currency'))}</b>\n"
            f"📥 Receiver: <b>{H(banks.receiver_label(r))}</b>\n"
            f"❌ <b>Status: REJECTED (Did NOT go to your store account!)</b>",
            photo_path=str(path.relative_to(config.PHOTOS_DIR))
        )
        return

    extra = ["📷 QR code read ✓" if info["qr_data"] else "📷 No QR code found in the photo"]
    extra += [f"ℹ️ {H(n)}" for n in info["notes"]]
    extra.append(await check_duplicate(ctx.bot, pid, r.ref, u.full_name, biz_id, r.url))
    await alert_flags(ctx.bot, pid, flags, u.full_name, biz_id)

    checking = config.BANK_CHECK and bool(r.ref or r.url)
    sent = await msg.reply_html(entry_text(db.get_payment(pid), extra, checking))
    db.update_payment(pid, chat_id=sent.chat_id, reply_msg_id=sent.message_id)
    if r.amount is None:
        ctx.user_data["awaiting_amount"] = pid
    if checking:
        start_checks(ctx, [pid], sent, {pid: extra}, u.full_name, biz_id)
    else:
        photo_rel = db.get_payment(pid).get("photo_path")
        await notify_business_owners(ctx.bot, biz_id, owner_payment_text(db.get_payment(pid)), photo_path=photo_rel)


async def _update_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE, target: dict,
                        ref: str | None, amount: float | None) -> None:
    """Waiter filled in a missing amount/code by replying."""
    u, msg = update.effective_user, update.effective_message
    pid = target["id"]
    biz_id = target.get("business_id") or 1
    changes = {}
    if amount is not None:
        changes["amount"] = amount
    if ref:
        changes["tx_ref"] = ref
        if not target.get("bank"):
            changes["bank"] = banks.guess_bank_from_ref(ref)
    changes.update(bank_check=None, bank_check_msg=None)
    db.update_payment(pid, **changes)
    ctx.user_data.pop("awaiting_amount", None)
    p = db.get_payment(pid)
    extra = [await check_duplicate(ctx.bot, pid, p["tx_ref"], u.full_name, biz_id)] if ref else []
    checking = config.BANK_CHECK and bool(p["tx_ref"] or p.get("receipt_url")) and p["status"] == "pending"
    sent = await msg.reply_html(entry_text(p, extra, checking, title="Updated"))
    db.update_payment(pid, chat_id=sent.chat_id, reply_msg_id=sent.message_id)
    if checking:
        start_checks(ctx, [pid], sent, {pid: extra}, u.full_name, biz_id)
    else:
        photo_rel = p.get("photo_path")
        await notify_business_owners(ctx.bot, biz_id, owner_payment_text(p), photo_path=photo_rel)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u, msg = update.effective_user, update.effective_message
    if not can_submit(u.id):
        await msg.reply_html(
            f"🔒 <b>You are not an authorized cashier.</b>\n\n"
            f"Your Telegram ID is: <code>{u.id}</code>\n"
            f"Tap below to open the portal and join your store using their Join Code, or request to register as a Business Owner:",
            reply_markup=dashboard_markup()
        )
        return

    biz = get_active_business(u.id)
    biz_id = biz["id"] if biz else 1
    accounts = [a.strip() for a in (biz["accounts"] or "").split(",") if a.strip()] if biz else []
    names = [n.strip() for n in (biz["names"] or "").split(",") if n.strip()] if biz else []

    text = msg.text.strip()

    # 1) Reply to one of the bot's receipt messages -> fill in the missing amount / code
    target = None
    if msg.reply_to_message:
        target = db.payment_by_reply(msg.chat_id, msg.reply_to_message.message_id)
        if target and target["waiter_id"] != u.id and not is_any_owner(u.id):
            target = None
    amount_only = parsing.plain_amount(text)
    if target is None and amount_only is not None and ctx.user_data.get("awaiting_amount"):
        target = db.get_payment(ctx.user_data["awaiting_amount"])

    if target is not None:
        ref, amount = (None, amount_only) if amount_only is not None else parsing.parse_text_entry(text)
        if ref is None and amount is None:
            await msg.reply_html("I couldn't find an amount or code in that. Example: <code>450</code>")
            return
        await _update_entry(update, ctx, target, ref, amount)
        return

    if amount_only is not None:
        await msg.reply_html("Which payment is this amount for? <b>Reply</b> to the bot's receipt message, "
                             "or send the code with the amount: <code>FT263113HT19 450</code>")
        return

    # 2) New entries typed (or a bank SMS forwarded) by the waiter
    entries = parsing.split_entries(text)
    if not any(r.ref for r, _ in entries):
        await msg.reply_html("❓ I couldn't find a transaction code.\n\n" + WAITER_HELP)
        return

    pids, extras, footer = [], {}, []
    for r, raw in entries:
        if not r.ref:
            footer.append(f"⏭ Skipped (no code): <code>{H(raw[:40])}</code>")
            continue
        if not r.bank:
            r.bank = banks.guess_bank_from_ref(r.ref)

        receiver_valid = banks.receiver_ok(r, accounts=accounts, names=names)
        is_fraud = (receiver_valid is False)

        flags = banks.offline_flags(r, accounts=accounts, names=names)
        if is_fraud:
            flags.append(f"🔴 FRAUD: Money paid to {banks.receiver_label(r)}, NOT store account!")

        status = "rejected" if is_fraud else "pending"

        pid = db.add_payment(
            business_id=biz_id, waiter_id=u.id, kind="text", raw_text=raw,
            tx_ref=r.ref, amount=r.amount, status=status,
            reviewed_at=config.now_utc().isoformat() if status == "rejected" else None
        )
        verify.save_receipt_fields(pid, r, flags)

        if is_fraud:
            footer.append(f"❌ #{pid} <b>REJECTED</b>: Paid to {H(banks.receiver_label(r))} (not store account)!")
            await notify_business_owners(
                ctx.bot, biz_id,
                f"🚨 <b>FRAUD ALERT: INVALID PAYMENT #{pid}</b>\n"
                f"👤 Cashier: {H(u.full_name)} · 💰 Amount: {config.money(r.amount, biz.get('currency'))}\n"
                f"📥 Receiver: {H(banks.receiver_label(r))} (NOT your account!)"
            )
            continue

        extras[pid] = [await check_duplicate(ctx.bot, pid, r.ref, u.full_name, biz_id)]
        await alert_flags(ctx.bot, pid, flags, u.full_name, biz_id)
        pids.append(pid)

    if not pids and footer:
        await msg.reply_html("\n".join(footer))
        return

    checking = config.BANK_CHECK
    sent = await msg.reply_html(batch_text(pids, extras, checking, footer))
    if len(pids) == 1:
        db.update_payment(pids[0], chat_id=sent.chat_id, reply_msg_id=sent.message_id)
        if db.get_payment(pids[0])["amount"] is None:
            ctx.user_data["awaiting_amount"] = pids[0]
    start_checks(ctx, pids, sent, extras, u.full_name, biz_id, footer)


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not can_submit(u.id):
        return
    biz = get_active_business(u.id)
    biz_id = biz["id"] if biz else 1
    curr = biz.get("currency") or config.CURRENCY

    day = config.business_day()
    rows = db.payments_for_day(day, waiter_id=u.id, business_id=biz_id)
    if not rows:
        await update.effective_message.reply_text(f"No entries from you today yet for {biz['name'] if biz else 'this store'}.")
        return
    counted = [r for r in rows if not db.is_excluded(r)]
    total = sum(r["amount"] or 0 for r in counted)
    lines = [f"📋 <b>Your entries — {H(biz['name'] if biz else '')} ({day})</b>"]
    for r in rows[-40:]:
        flag = {"verified": "✅", "rejected": "❌"}.get(r["status"], "⏳")
        if r["duplicate_of"] or db.has_problem(r):
            flag = "⚠️"
        lines.append(f"{flag} #{r['id']} {config.local_time(r['created_at'])} "
                     f"<code>{H(r['tx_ref'] or '—')}</code> {config.money(r['amount'], curr)}")
    lines.append(f"\nTotal: <b>{config.money(total, curr)}</b> ({len(counted)} entries)")
    await update.effective_message.reply_html("\n".join(lines))


async def cmd_undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u, msg = update.effective_user, update.effective_message
    biz = get_active_business(u.id)
    biz_id = biz["id"] if biz else None
    curr = biz.get("currency") or config.CURRENCY if biz else config.CURRENCY

    p = db.last_payment(u.id, business_id=biz_id)
    if not p:
        await msg.reply_text("Nothing to undo.")
        return
    if p["status"] != "pending":
        await msg.reply_text(f"#{p['id']} was already {p['status']} and can't be deleted. Ask the owner.")
        return
    db.delete_payment(p["id"])
    if p["photo_path"]:
        (config.PHOTOS_DIR / p["photo_path"]).unlink(missing_ok=True)
    ctx.user_data.pop("awaiting_amount", None)
    await msg.reply_html(f"🗑 Deleted #{p['id']} (<code>{H(p['tx_ref'] or '—')}</code> {config.money(p['amount'], curr)})")


# ------------------------------------------------------------------ business owner commands
def owner_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not is_any_owner(uid):
            await update.effective_message.reply_text("Owner only.")
            return
        return await func(update, ctx)
    return wrapper


def superadmin_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not is_superadmin(uid):
            await update.effective_message.reply_text("Super Admin only.")
            return
        return await func(update, ctx)
    return wrapper


@owner_only
async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    markup = dashboard_markup()
    if not markup:
        await update.effective_message.reply_text("Set WEBAPP_URL in .env to enable the dashboard. Use /summary meanwhile.")
        return
    await update.effective_message.reply_text("Tap below to open your live dashboard:", reply_markup=markup)


@owner_only
async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    biz = get_active_business(u.id)
    if not biz:
        await update.effective_message.reply_text("No active business found.")
        return

    ptype, pval = parse_period_arg(ctx.args)
    if not ptype:
        await update.effective_message.reply_text("Usage: /summary [today | yesterday | YYYY-MM-DD | YYYY-MM | YYYY]")
        return

    s = db.summary(business_id=biz["id"], period_type=ptype, period_value=pval)
    await update.effective_message.reply_html(reports.format_summary(s), reply_markup=dashboard_markup())


@owner_only
async def cmd_pdf(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    biz = get_active_business(u.id)
    if not biz:
        await update.effective_message.reply_text("No active business found.")
        return

    ptype, pval = parse_period_arg(ctx.args)
    if not ptype:
        await update.effective_message.reply_text("Usage: /pdf [today | yesterday | YYYY-MM-DD | YYYY-MM | YYYY]")
        return

    wait_msg = await update.effective_message.reply_text(f"⏳ Generating {ptype} PDF report for {biz['name']} ({pval})…")
    try:
        s = db.summary(business_id=biz["id"], period_type=ptype, period_value=pval)
        payments = db.payments_for_period(biz["id"], ptype, pval)
        title = f"{ptype.capitalize()} Report — {pval}"
        pdf_bytes = await pdf_report.generate_pdf(biz, s, payments, title)
        filename = f"report_{biz['code']}_{pval}.pdf"
        caption = (f"📄 <b>{H(biz['name'])} — {title}</b>\n"
                   f"💰 Claimed: <b>{config.money(s['claimed']['total'], biz.get('currency'))}</b> ({s['claimed']['count']} sales)\n"
                   f"✅ Verified: <b>{config.money(s['verified']['total'], biz.get('currency'))}</b>")
        await update.effective_message.reply_document(document=pdf_bytes, filename=filename,
                                                      caption=caption, parse_mode="HTML")
        await wait_msg.delete()
    except Exception as e:
        log.exception("Error generating PDF: %s", e)
        await wait_msg.edit_text(f"⚠️ Failed to generate PDF: {e}")


@owner_only
async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    biz = get_active_business(u.id)
    if not biz:
        await update.effective_message.reply_text("No active business found.")
        return

    ptype, pval = parse_period_arg(ctx.args)
    if not ptype:
        await update.effective_message.reply_text("Usage: /export [today | yesterday | YYYY-MM-DD | YYYY-MM | YYYY]")
        return

    csv_data = reports.csv_bytes(pval, business_id=biz["id"], period_type=ptype)
    filename = f"payments_{biz['code']}_{pval}.csv"
    await update.effective_message.reply_document(document=csv_data, filename=filename,
                                                  caption=f"Spreadsheet: {H(biz['name'])} ({pval})",
                                                  parse_mode="HTML")


@owner_only
async def cmd_addwaiter(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """The business owner registers a waiter directly."""
    u, msg = update.effective_user, update.effective_message
    biz = get_active_business(u.id)
    if not biz:
        await msg.reply_text("No active business configured.")
        return

    target_id = None
    counter_name = ""

    # Check if replied to a message from the waiter
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target_user = msg.reply_to_message.from_user
        target_id = target_user.id
        counter_name = " ".join(ctx.args).strip() if ctx.args else target_user.full_name
    elif ctx.args:
        first_arg = ctx.args[0].strip()
        if first_arg.isdigit():
            target_id = int(first_arg)
            counter_name = " ".join(ctx.args[1:]).strip() if len(ctx.args) > 1 else ""
        elif first_arg.startswith("@"):
            found_u = db.get_user_by_username(first_arg)
            if found_u:
                target_id = found_u["tg_id"]
                counter_name = " ".join(ctx.args[1:]).strip() if len(ctx.args) > 1 else found_u["name"]
            else:
                await msg.reply_html(f"❌ User <code>{H(first_arg)}</code> hasn't messaged the bot yet. Ask them to press /start to get their Telegram ID.")
                return

    if not target_id:
        await msg.reply_html(
            "<b>How to register a cashier for your store:</b>\n\n"
            "Usage: <code>/addwaiter &lt;telegram_id&gt; [Counter Name]</code>\n"
            "Example: <code>/addwaiter 987654321 Counter 1 - Meron</code>\n\n"
            "💡 <i>Tip: The cashier can find their Telegram ID by sending /start to this bot. You can also forward any message from them and reply with <code>/addwaiter Counter 1</code>!</i>"
        )
        return

    target_user = db.get_user(target_id)
    name_display = counter_name or (target_user["name"] if target_user else f"Cashier {target_id}")

    db.upsert_user(target_id, name_display, target_user["username"] if target_user else None, "waiter")
    db.add_waiter_to_business(biz["id"], target_id, name_display, active=1)

    await msg.reply_html(
        f"✅ <b>Cashier Registered!</b>\n\n"
        f"• Store: <b>{H(biz['name'])}</b>\n"
        f"• Cashier: <b>{H(name_display)}</b>\n"
        f"• Telegram ID: <code>{target_id}</code>\n\n"
        f"They can now submit customer payment receipts for your store."
    )

    # Notify cashier
    try:
        await ctx.bot.send_message(
            target_id,
            f"🎉 <b>You have been registered as a cashier for {H(biz['name'])}!</b>\n\n"
            f"Counter / Name: <b>{H(name_display)}</b>\n\n"
            f"You can now take photos of customer receipts and send them here.\n\n" + WAITER_HELP,
            parse_mode="HTML"
        )
    except TelegramError:
        pass


@owner_only
async def cmd_mybusiness(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    biz = get_active_business(u.id)
    if not biz:
        await update.effective_message.reply_text("No business configured.")
        return

    waiters = db.get_waiters_for_business(biz["id"])
    active_waiters = [w for w in waiters if w["active"]]
    pending_waiters = [w for w in waiters if not w["active"]]
    owners = db.get_business_owners(biz["id"])
    owner_names = ", ".join(H(o["name"]) for o in owners) or "None"

    accounts_str = H(biz["accounts"] or "⚠️ None set! Run /setcbe & /settelebirr")
    names_str = H(biz["names"] or "⚠️ None set! Run /setnames")

    text = (
        f"🏢 <b>Business Profile: {H(biz['name'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Business ID: <code>{biz['id']}</code>\n"
        f"🔑 Join Code: <code>{H(biz['code'])}</code>\n"
        f"💱 Currency: <b>{H(biz['currency'])}</b>\n"
        f"👥 Owners: {owner_names}\n"
        f"🛒 Active Cashiers: <b>{len(active_waiters)}</b>"
        + (f" (⏳ {len(pending_waiters)} pending)" if pending_waiters else "") + "\n\n"
        f"🏦 <b>Registered Payment Accounts:</b>\n<code>{accounts_str}</code>\n\n"
        f"👤 <b>Account Holder Legal Names:</b>\n<code>{names_str}</code>\n\n"
        "🔒 <b>FRAUD PROTECTION ACTIVE:</b>\n"
        "All transactions MUST be paid to the accounts above. Any payment to another account is automatically rejected!\n\n"
        "<b>Management Commands:</b>\n"
        "• <code>/addwaiter &lt;id&gt; [Counter]</code> – register a cashier\n"
        "• <code>/setcbe 1000...</code> – set your CBE account\n"
        "• <code>/settelebirr 0911...</code> – set your telebirr account\n"
        "• <code>/setnames Store Name PLC</code> – set legal account name\n"
        "• <code>/cashiers</code> – view registered cashiers\n"
        "• <code>/code &lt;new_code&gt;</code> – change join code"
    )
    await update.effective_message.reply_html(text)


@owner_only
async def cmd_setcbe(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    biz = get_active_business(u.id)
    if not biz:
        await update.effective_message.reply_text("No business configured.")
        return
    if not ctx.args:
        await update.effective_message.reply_html(
            "<b>Set your Commercial Bank of Ethiopia (CBE) Account:</b>\n\n"
            "Usage: <code>/setcbe 1000123456076</code>\n"
            "(Must be your 13-digit CBE account number)."
        )
        return
    acc = re.sub(r"\D", "", ctx.args[0])
    if len(acc) != 13 or not acc.startswith("1"):
        await update.effective_message.reply_text("⚠️ CBE account numbers are 13 digits starting with 1 (e.g. 1000123456076).")
        return

    current = [a.strip() for a in (biz["accounts"] or "").split(",") if a.strip()]
    # Replace old CBE or add
    kept = [a for a in current if not (len(re.sub(r"\D", "", a)) == 13 and re.sub(r"\D", "", a).startswith("1"))]
    kept.insert(0, acc)
    new_accounts_str = ",".join(kept)
    db.update_business(biz["id"], accounts=new_accounts_str)

    await update.effective_message.reply_html(
        f"✅ <b>CBE Account Set!</b>\n\n"
        f"• Store: <b>{H(biz['name'])}</b>\n"
        f"• CBE Account: <code>{acc}</code>\n\n"
        f"Automatic CBE bank confirmation is now active! Any receipt claiming to be CBE that was paid to another account will be rejected as fraud."
    )


@owner_only
async def cmd_settelebirr(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    biz = get_active_business(u.id)
    if not biz:
        await update.effective_message.reply_text("No business configured.")
        return
    if not ctx.args:
        await update.effective_message.reply_html(
            "<b>Set your telebirr Account or Merchant Till:</b>\n\n"
            "Usage: <code>/settelebirr 0911223344</code> or <code>/settelebirr 582910</code>\n"
            "(Enter your phone number or merchant till code)."
        )
        return
    acc = re.sub(r"\D", "", ctx.args[0])
    current = [a.strip() for a in (biz["accounts"] or "").split(",") if a.strip()]
    # Keep CBE accounts
    kept = [a for a in current if len(re.sub(r"\D", "", a)) == 13 and re.sub(r"\D", "", a).startswith("1")]
    kept.append(acc)
    new_accounts_str = ",".join(kept)
    db.update_business(biz["id"], accounts=new_accounts_str)

    await update.effective_message.reply_html(
        f"✅ <b>telebirr Account Set!</b>\n\n"
        f"• Store: <b>{H(biz['name'])}</b>\n"
        f"• telebirr: <code>{acc}</code>\n\n"
        f"telebirr payments to other numbers will now be automatically rejected as fraud!"
    )


@owner_only
async def cmd_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    biz = get_active_business(u.id)
    if not biz:
        await update.effective_message.reply_text("No business configured.")
        return
    if not ctx.args:
        await update.effective_message.reply_html(f"Current join code is: <code>{H(biz['code'])}</code>\n\n"
                                                  f"To change it: <code>/code newcode123</code>")
        return
    new_code = ctx.args[0].strip().lower()
    existing = db.get_business_by_code(new_code)
    if existing and existing["id"] != biz["id"]:
        await update.effective_message.reply_text("❌ That code is already in use by another business. Choose a different one.")
        return
    db.update_business(biz["id"], code=new_code)
    await update.effective_message.reply_html(f"✅ Cashier join code updated to: <code>{H(new_code)}</code>\n"
                                             f"Cashiers can request to join with: <code>/join {H(new_code)} [Counter]</code>")


@owner_only
async def cmd_setaccounts(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    biz = get_active_business(u.id)
    if not biz:
        await update.effective_message.reply_text("No business configured.")
        return
    if not ctx.args:
        await update.effective_message.reply_html(
            f"Current accounts: <code>{H(biz['accounts'] or 'None')}</code>\n\n"
            "Usage: <code>/setaccounts 1000123456076, 0911223344</code>\n"
            "(Enter your 13-digit CBE account and telebirr merchant/phone number separated by commas)."
        )
        return
    new_accs = " ".join(ctx.args).replace(" ", "")
    db.update_business(biz["id"], accounts=new_accs)
    await update.effective_message.reply_html(
        f"✅ Bank accounts updated for <b>{H(biz['name'])}</b>:\n<code>{H(new_accs)}</code>\n\n"
        "🔒 <b>Anti-Fake Rule:</b> Only payments sent to these accounts are valid! All others will be rejected automatically."
    )


@owner_only
async def cmd_setnames(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    biz = get_active_business(u.id)
    if not biz:
        await update.effective_message.reply_text("No business configured.")
        return
    if not ctx.args:
        await update.effective_message.reply_html(
            f"Current receiver names: <code>{H(biz['names'] or 'None')}</code>\n\n"
            "Usage: <code>/setnames Bole Cafe PLC, Abebe Balcha</code>\n"
            "(Enter business account holder names as they appear on bank receipts)."
        )
        return
    new_names = " ".join(ctx.args).strip()
    db.update_business(biz["id"], names=new_names)
    await update.effective_message.reply_html(
        f"✅ Account holder names updated for <b>{H(biz['name'])}</b>:\n<code>{H(new_names)}</code>\n\n"
        "Receipts with different recipient names will be rejected automatically as fraud attempts!"
    )


@owner_only
async def cmd_waiters(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    biz = get_active_business(u.id)
    if not biz:
        await update.effective_message.reply_text("No business configured.")
        return

    waiters = db.get_waiters_for_business(biz["id"])
    if not waiters:
        await update.effective_message.reply_html(
            f"No cashiers registered yet for <b>{H(biz['name'])}</b>.\n\n"
            f"Register a cashier now: <code>/addwaiter &lt;telegram_id&gt; [Counter Name]</code>")
        return
    lines = [f"👥 <b>Registered Cashiers & Counters ({H(biz['name'])})</b>\n━━━━━━━━━━━━━━━━━━━━"]
    for w in waiters:
        status_icon = "🟢 Active" if w["active"] else "⏳ Pending Approval"
        c_name = w["counter_name"] or w["user_name"]
        lines.append(f"• {status_icon}: <b>{H(c_name)}</b> (ID: <code>{w['tg_id']}</code>)")
    lines.append("\n• Register cashier: <code>/addwaiter &lt;id&gt; [Counter]</code>")
    lines.append("• Deactivate cashier: <code>/remove &lt;id&gt;</code>")
    await update.effective_message.reply_html("\n".join(lines))


@owner_only
async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    biz = get_active_business(u.id)
    if not biz:
        await update.effective_message.reply_text("No business configured.")
        return

    if not ctx.args or not ctx.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /remove <cashier id> (see /cashiers)")
        return
    wid = int(ctx.args[0])
    ok = db.set_business_waiter_active(biz["id"], wid, False)
    await update.effective_message.reply_text(f"✅ Cashier {wid} deactivated from {biz['name']}." if ok else "Not found.")


# ------------------------------------------------------------------ platform super admin commands
@superadmin_only
async def cmd_registerowner(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Shortcut for platform owner: /registerowner <owner_tg_id> <Business Name>"""
    msg = update.effective_message
    if not ctx.args and not (msg.reply_to_message and msg.reply_to_message.from_user):
        await msg.reply_html(
            "<b>How to register a Business Owner:</b>\n\n"
            "Usage: <code>/registerowner &lt;owner_tg_id&gt; &lt;Business Name&gt;</code>\n"
            "Example: <code>/registerowner 987654321 Friendship Supermarket</code>\n\n"
            "💡 <i>Tip: You can also forward any message from the client to this chat and reply with <code>/registerowner Friendship Supermarket</code>!</i>"
        )
        return

    owner_id = None
    name = ""

    if msg.reply_to_message and msg.reply_to_message.from_user:
        owner_id = msg.reply_to_message.from_user.id
        name = " ".join(ctx.args).strip()
    elif ctx.args:
        first = ctx.args[0].strip()
        if first.isdigit():
            owner_id = int(first)
            name = " ".join(ctx.args[1:]).strip()
        elif first.startswith("@"):
            u = db.get_user_by_username(first)
            if u:
                owner_id = u["tg_id"]
                name = " ".join(ctx.args[1:]).strip()
            else:
                await msg.reply_html(f"❌ User <code>{H(first)}</code> hasn't messaged the bot yet. Ask them to press /start to get their Telegram ID.")
                return

    if not owner_id or not name:
        await msg.reply_html("Usage: <code>/registerowner &lt;owner_tg_id&gt; &lt;Business Name&gt;</code>\nExample: <code>/registerowner 987654321 Friendship Supermarket</code>")
        return

    bid = db.create_business(name=name)
    biz = db.get_business(bid)
    db.add_business_owner(bid, owner_id)

    await msg.reply_html(
        f"✅ <b>Business Owner Successfully Registered!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• Business: <b>{H(name)}</b> (ID: <code>{bid}</code>)\n"
        f"• Owner Telegram ID: <code>{owner_id}</code>\n"
        f"• Cashier Join Code: <code>{biz['code']}</code>\n\n"
        f"The client now has full business owner access to manage <b>{H(name)}</b>:\n"
        f"• They can set their CBE & telebirr accounts with /setcbe & /settelebirr\n"
        f"• They can register their cashiers with /addwaiter\n"
        f"• They will get instant transaction alerts and reports for {H(name)}."
    )

    try:
        await ctx.bot.send_message(
            owner_id,
            f"🎉 <b>Welcome! You have been registered as the Owner of {H(name)}!</b>\n\n"
            f"• Business ID: <code>{bid}</code>\n"
            f"• Cashier Join Code: <code>{biz['code']}</code>\n\n"
            "Here is how to get started:\n"
            "1️⃣ <b>Set your payment accounts</b> (only payments to your accounts will be accepted):\n"
            "   • <code>/setcbe 1000...</code> (your 13-digit CBE account)\n"
            "   • <code>/settelebirr 0911...</code> (your telebirr phone or merchant till)\n"
            "   • <code>/setnames Store Legal Name PLC</code> (account name on receipts)\n\n"
            "2️⃣ <b>Register your cashiers / waiters:</b>\n"
            "   • <code>/addwaiter &lt;cashier_telegram_id&gt; [Counter Name]</code>\n\n"
            "3️⃣ <b>Live Dashboard & Reports:</b>\n"
            "   Whenever a cashier sends a receipt photo (QR or code), you get an instant verified payment notification.\n"
            "   Use /report to open your WebApp dashboard and /pdf to download financial reports.\n\n" + OWNER_HELP,
            parse_mode="HTML"
        )
    except TelegramError:
        pass


@superadmin_only
async def cmd_newbusiness(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Registers a new business on the platform. Usage: /newbusiness "Name" [code] [owner_id]"""
    if not ctx.args:
        await update.effective_message.reply_html(
            "Usage: <code>/newbusiness &lt;Name&gt; [code] [owner_tg_id]</code>\n"
            "Example: <code>/newbusiness \"Bole Cafe\" bolecafe 7212240185</code>"
        )
        return

    raw = " ".join(ctx.args)
    match = re.match(r'^["\']([^"\']+)["\']\s*(.*)$', raw)
    if match:
        name = match.group(1).strip()
        rem = match.group(2).strip().split()
    else:
        parts = raw.split()
        name = parts[0]
        rem = parts[1:]

    code = rem[0] if len(rem) > 0 and not rem[0].isdigit() else None
    owner_id = int(rem[1] if (code and len(rem) > 1 and rem[1].isdigit()) else (rem[0] if (len(rem) > 0 and rem[0].isdigit()) else 0))

    bid = db.create_business(name=name, code=code)
    biz = db.get_business(bid)

    owner_str = "None"
    if owner_id:
        db.add_business_owner(bid, owner_id)
        owner_str = f"<code>{owner_id}</code>"
        try:
            await ctx.bot.send_message(
                owner_id,
                f"🎉 <b>You have been registered as owner of {H(name)}!</b>\n\n"
                f"• Business ID: <code>{bid}</code>\n"
                f"• Cashier Join Code: <code>{biz['code']}</code>\n\n"
                "Use /setcbe and /settelebirr to set your payment accounts, and /addwaiter to register cashiers.",
                parse_mode="HTML"
            )
        except TelegramError:
            pass

    text = (
        f"🏢 <b>New Business Created!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• Name: <b>{H(name)}</b>\n"
        f"• ID: <code>{bid}</code>\n"
        f"• Join Code: <code>{H(biz['code'])}</code>\n"
        f"• Assigned Owner: {owner_str}\n\n"
        f"Owner can set accounts with /setcbe, /settelebirr, and register cashiers with /addwaiter."
    )
    await update.effective_message.reply_html(text)


@superadmin_only
async def cmd_setowner(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if len(ctx.args) < 2 or not ctx.args[1].isdigit():
        await update.effective_message.reply_html("Usage: <code>/setowner &lt;biz_id_or_code&gt; &lt;user_id&gt;</code>")
        return

    arg = ctx.args[0].strip()
    target_uid = int(ctx.args[1])
    biz = db.get_business(int(arg)) if arg.isdigit() else db.get_business_by_code(arg)
    if not biz:
        await update.effective_message.reply_text("❌ Business not found.")
        return

    db.add_business_owner(biz["id"], target_uid)
    await update.effective_message.reply_html(
        f"✅ User <code>{target_uid}</code> is now an owner of <b>{H(biz['name'])}</b> (ID: <code>{biz['id']}</code>)!"
    )


@superadmin_only
async def cmd_allbusinesses(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    businesses = db.list_all_businesses()
    if not businesses:
        await update.effective_message.reply_text("No businesses registered.")
        return

    lines = ["🌐 <b>All Registered Businesses</b>\n━━━━━━━━━━━━━━━━━━━━"]
    for b in businesses:
        owners = db.get_business_owners(b["id"])
        waiters = db.get_waiters_for_business(b["id"])
        status = "🟢" if b["active"] else "⚪️"
        lines.append(
            f"{status} <b>#{b['id']} {H(b['name'])}</b>\n"
            f"   Code: <code>{H(b['code'])}</code> · Owners: {len(owners)} · Cashiers: {len(waiters)}"
        )
    lines.append("\nCreate new business: <code>/newbusiness &lt;Name&gt; [code] [owner_id]</code>")
    await update.effective_message.reply_html("\n".join(lines))


# ------------------------------------------------------------------ scheduled + setup
async def daily_report(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends nightly summary to owners of each active business."""
    for biz in db.list_businesses():
        s = db.summary(business_id=biz["id"], period_type="day", period_value=config.business_day())
        if s["count"] > 0:
            text = f"🌙 <b>End of Day Report — {H(biz['name'])}</b>\n\n" + reports.format_summary(s)
            await notify_business_owners(ctx.bot, biz["id"], text, dashboard_markup())


async def setup_owner_chat(bot, owner_id: int) -> None:
    try:
        await bot.set_my_commands(OWNER_COMMANDS, scope=BotCommandScopeChat(owner_id))
        if config.WEBAPP_URL:
            await bot.set_chat_menu_button(
                chat_id=owner_id,
                menu_button=MenuButtonWebApp("Dashboard", WebAppInfo(url=config.WEBAPP_URL + "/")),
            )
    except TelegramError as e:
        log.info("Owner %s chat setup skipped: %s", owner_id, e)


WAITER_COMMANDS = [
    BotCommand("today", "My sales today"),
    BotCommand("switch", "Switch active business"),
    BotCommand("name", "Set counter / cashier name"),
    BotCommand("undo", "Delete last entry"),
    BotCommand("help", "How to submit payments"),
]
OWNER_COMMANDS = [
    BotCommand("report", "Open live dashboard"),
    BotCommand("summary", "Daily / monthly / yearly sales summary"),
    BotCommand("pdf", "Download PDF financial report"),
    BotCommand("export", "Download CSV spreadsheet"),
    BotCommand("mybusiness", "Business profile, accounts & stats"),
    BotCommand("addwaiter", "Register a cashier"),
    BotCommand("cashiers", "List cashiers & counters"),
    BotCommand("setcbe", "Set CBE account"),
    BotCommand("settelebirr", "Set telebirr account"),
    BotCommand("switch", "Switch active business"),
    BotCommand("help", "Help & instructions"),
] + WAITER_COMMANDS[:4]


async def setup_bot(app: Application) -> None:
    await app.bot.set_my_commands(WAITER_COMMANDS)
    if config.WEBAPP_URL:
        try:
            await app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp("📱 Dashboard", WebAppInfo(url=config.WEBAPP_URL + "/"))
            )
        except Exception as e:
            log.info("Global menu button setup skipped: %s", e)
    for oid in config.OWNER_IDS:
        await setup_owner_chat(app.bot, oid)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Error while handling update", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Something went wrong, please try again.")
        except TelegramError:
            pass


def build_application() -> Application:
    request_kwargs = {
        "connect_timeout": 20.0,
        "read_timeout": 20.0,
        "write_timeout": 20.0,
        "pool_timeout": 5.0,
    }
    if config.TELEGRAM_PROXY:
        request_kwargs["proxy"] = config.TELEGRAM_PROXY

    request = HTTPXRequest(**request_kwargs)
    get_updates_request = HTTPXRequest(**request_kwargs)

    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .request(request)
        .get_updates_request(get_updates_request)
        .build()
    )
    private = filters.ChatType.PRIVATE

    # General & Cashier commands
    app.add_handler(CommandHandler("start", cmd_start, private))
    app.add_handler(CommandHandler("help", cmd_help, private))
    app.add_handler(CommandHandler("join", cmd_join, private))
    app.add_handler(CommandHandler(["switch", "businesses"], cmd_switch, private))
    app.add_handler(CommandHandler("name", cmd_name, private))
    app.add_handler(CommandHandler("today", cmd_today, private))
    app.add_handler(CommandHandler("undo", cmd_undo, private))

    # Business Owner commands
    app.add_handler(CommandHandler("report", cmd_report, private))
    app.add_handler(CommandHandler("summary", cmd_summary, private))
    app.add_handler(CommandHandler("pdf", cmd_pdf, private))
    app.add_handler(CommandHandler("export", cmd_export, private))
    app.add_handler(CommandHandler("mybusiness", cmd_mybusiness, private))
    app.add_handler(CommandHandler(["addwaiter", "addcashier"], cmd_addwaiter, private))
    app.add_handler(CommandHandler("setcbe", cmd_setcbe, private))
    app.add_handler(CommandHandler("settelebirr", cmd_settelebirr, private))
    app.add_handler(CommandHandler("setaccounts", cmd_setaccounts, private))
    app.add_handler(CommandHandler("setnames", cmd_setnames, private))
    app.add_handler(CommandHandler("code", cmd_code, private))
    app.add_handler(CommandHandler(["cashiers", "waiters"], cmd_waiters, private))
    app.add_handler(CommandHandler("remove", cmd_remove, private))

    # Super Admin commands
    app.add_handler(CommandHandler(["registerowner", "registerbusiness", "onboard"], cmd_registerowner, private))
    app.add_handler(CommandHandler("newbusiness", cmd_newbusiness, private))
    app.add_handler(CommandHandler("setowner", cmd_setowner, private))
    app.add_handler(CommandHandler("allbusinesses", cmd_allbusinesses, private))

    # Callback Query handler (for /switch and waiter approval buttons)
    app.add_handler(CallbackQueryHandler(on_switch_callback))

    # Message handlers
    app.add_handler(MessageHandler(private & (filters.PHOTO | filters.Document.IMAGE), on_photo))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    hour, minute = (int(x) for x in config.DAILY_REPORT_TIME.split(":"))
    if app.job_queue is None:
        log.warning('Nightly report disabled: run  pip install "python-telegram-bot[job-queue]"')
    else:
        app.job_queue.run_daily(daily_report, time=dtime(hour, minute, tzinfo=config.TZ), name="daily_report")
    return app
