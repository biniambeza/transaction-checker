"""Telegram bot: waiters submit receipts, owners get alerts, summaries and the dashboard."""
import asyncio
import hmac
import html
import logging
import re
import uuid
from datetime import date, time as dtime, timedelta

from telegram import (BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup,
                      MenuButtonWebApp, Update, WebAppInfo)
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

import banks
import config
import db
import parsing
import reports
import verify

log = logging.getLogger(__name__)
H = html.escape

WAITER_HELP = (
    "👋 <b>Cashier Guide — How to submit transactions</b>\n\n"
    "📸 <b>Take a photo</b> of the customer's payment screen / receipt (CBE, telebirr, BoA, any bank) — ensure the QR code and amount are clear.\n"
    "   You can add the amount as a caption, e.g. <code>450</code>\n\n"
    "⌨️ <b>Or type</b> the transaction code and amount:\n"
    "   <code>FT263113HT19 450</code>\n"
    "   (Forwarding the bank SMS confirmation also works).\n\n"
    "👤 <b>Set your counter name:</b>\n"
    "   <code>/name Counter 1</code> or <code>/name Counter 2 - Meron</code>\n\n"
    "/today – view your sales and transactions today\n"
    "/undo – cancel/delete your last entry"
)
OWNER_HELP = (
    "👑 <b>Supermarket Owner Commands</b>\n\n"
    "/report – open live dashboard\n"
    "/summary [today | yesterday | YYYY-MM-DD] – sales totals by cashier & bank\n"
    "/export [YYYY-MM-DD] – download CSV spreadsheet for Excel\n"
    "/cashiers – list all cashiers and counters\n"
    "/remove &lt;id&gt; – deactivate a cashier\n\n"
    f"Cashiers join by sending <code>/join {H(config.JOIN_CODE or '<code>')} [Counter Name]</code> to this bot.\n"
    "Example: <code>/join " + (config.JOIN_CODE or "CODE") + " Counter 1</code>"
    + ("" if config.JOIN_CODE else "\n⚠️ JOIN_CODE is not set in .env, so nobody can join yet.")
    + ("" if config.MY_ACCOUNTS else "\n⚠️ MY_ACCOUNTS is not set in .env, so I can't check that money "
                                     "went to <b>your</b> account, and CBE bank checks are off.")
)
CHECK_ICONS = {"ok": "✅", "mismatch": "🔴", "not_found": "🔴", "error": "⚠️", "unsupported": "ℹ️"}


# ------------------------------------------------------------------ helpers
def is_owner(uid: int) -> bool:
    return uid in config.OWNER_IDS


def can_submit(uid: int) -> bool:
    if is_owner(uid):
        return True
    user = db.get_user(uid)
    return bool(user and user["active"])


def dashboard_markup() -> InlineKeyboardMarkup | None:
    if not config.WEBAPP_URL:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📊 Open dashboard", web_app=WebAppInfo(url=config.WEBAPP_URL + "/"))]]
    )


async def notify_owners(bot, text: str, markup=None) -> None:
    for oid in config.OWNER_IDS:
        try:
            await bot.send_message(oid, text, parse_mode="HTML", reply_markup=markup)
        except TelegramError as e:
            log.warning("Could not message owner %s: %s (has the owner pressed /start?)", oid, e)


def parse_day_arg(args: list[str]) -> str | None:
    if not args:
        return config.business_day()
    a = args[0].lower()
    if a == "yesterday":
        return (date.fromisoformat(config.business_day()) - timedelta(days=1)).isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", a):
        return a
    return None


async def check_duplicate(bot, pid: int, ref: str | None, who: str, url: str | None = None) -> str:
    """Flags the entry if the same transaction code (or receipt QR link) was already submitted."""
    dup = db.find_duplicate(ref, pid) if ref else None
    what = f"code <code>{H(ref)}</code>" if dup else ""
    if not dup and url:
        dup = db.find_duplicate_url(url, pid)
        what = "this receipt (same QR code)"
    db.update_payment(pid, duplicate_of=dup["id"] if dup else None)
    if not dup:
        return ""
    warning = (f"⚠️ <b>DUPLICATE</b>: {what} was already sent as #{dup['id']} "
               f"by {H(dup['waiter_name'])} ({config.local_datetime(dup['created_at'])}).")
    await notify_owners(bot, f"🚨 {warning}\nNew entry #{pid} from {H(who)}.")
    return warning


async def alert_flags(bot, pid: int, flags: list[str], who: str) -> None:
    if flags:
        await notify_owners(bot, f"🚩 <b>Check entry #{pid}</b> from {H(who)}:\n" +
                            "\n".join(f"• {H(f)}" for f in flags))


def _receiver(p: dict) -> str | None:
    if not (p.get("receiver") or p.get("receiver_account")):
        return None
    return banks.receiver_label(banks.Receipt(receiver=p.get("receiver"), receiver_account=p.get("receiver_account")))


def entry_text(p: dict, extra: list[str], checking: bool, title: str = "Saved") -> str:
    lines = [f"🧾 <b>{title} #{p['id']}</b>" + (f" · {H(p['bank'])}" if p.get("bank") else ""),
             f"Code: <code>{H(p['tx_ref'])}</code>" if p["tx_ref"] else "Code: ❓ not found",
             f"Amount: <b>{config.money(p['amount'])}</b>" if p["amount"] is not None else "Amount: ❓ not found"]
    if _receiver(p):
        lines.append(f"To: {H(_receiver(p))}")
    lines += [f"🚩 {H(f)}" for f in (p.get("flags") or "").split("; ") if f]
    lines += [x for x in extra if x]
    if p.get("bank_check_msg"):
        lines.append(f"{CHECK_ICONS.get(p['bank_check'], 'ℹ️')} {H(p['bank_check_msg'])}")
    elif checking:
        lines.append("🏦 <i>Checking with the bank…</i>")
    if p["status"] == "verified":
        lines.append("✅ <b>Verified</b>")
    if (p["tx_ref"] is None or p["amount"] is None) and not checking and p.get("bank_check") != "ok":
        lines.append("\n↩️ <i>Reply to this message with the missing amount and/or code.</i>")
    return "\n".join(lines)


def batch_text(pids: list[int], extras: dict, checking: bool, footer: list[str]) -> str:
    if len(pids) == 1 and not footer:
        return entry_text(db.get_payment(pids[0]), extras.get(pids[0], []), checking)
    lines = []
    for pid in pids:
        p = db.get_payment(pid)
        line = f"🧾 #{pid} <code>{H(p['tx_ref'])}</code> · " + (
            config.money(p["amount"]) if p["amount"] is not None else "❓ amount")
        if p.get("bank_check_msg"):
            line += f"\n   {CHECK_ICONS.get(p['bank_check'], 'ℹ️')} {H(p['bank_check_msg'])}"
        elif checking:
            line += "  🏦…"
        line += "".join(f"\n   🚩 {H(f)}" for f in (p.get("flags") or "").split("; ") if f)
        line += "".join(f"\n   {x}" for x in extras.get(pid, []) if x)
        lines.append(line)
    return "\n".join(lines + footer)


def owner_payment_text(p: dict) -> str:
    m = config.money
    who = p.get("waiter_name") or f"Cashier {p['waiter_id']}"
    bank = p.get("bank") or "Unknown Bank"
    amount_str = m(p["amount"]) if p["amount"] is not None else "❓ Amount not detected"
    ref_str = p["tx_ref"] or "❓ Not detected"
    receiver = _receiver(p) or "Not specified"

    check = p.get("bank_check")
    if check == "ok":
        verif = f"✅ <b>Verified Real by Bank</b>\n   ↳ <i>{H(p.get('bank_check_msg', ''))}</i>"
    elif check in ("mismatch", "not_found"):
        verif = f"🔴 <b>FRAUD ALERT / MISMATCH!</b>\n   ↳ <i>{H(p.get('bank_check_msg', ''))}</i>"
    elif p["status"] == "verified":
        verif = "✅ <b>Verified</b>"
    elif p.get("duplicate_of"):
        dup = db.get_payment(p["duplicate_of"])
        dup_by = dup["waiter_name"] if dup else "earlier"
        verif = f"🚨 <b>DUPLICATE RECEIPT!</b> (Sent as #{p['duplicate_of']} by {H(dup_by)})"
    else:
        verif = f"⏳ <b>Pending manual check</b> (QR: {'✓' if p.get('qr_data') else 'none'})"

    lines = [
        f"🛒 <b>Cashier Sale #{p['id']}</b> · {H(bank)}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"👤 <b>Cashier:</b> {H(who)}",
        f"💰 <b>Amount:</b> <b>{amount_str}</b>",
        f"🔢 <b>Reference:</b> <code>{H(ref_str)}</code>",
        f"📥 <b>Paid To:</b> {H(receiver)}",
        f"🛡️ <b>Status:</b> {verif}",
    ]
    flags = [f for f in (p.get("flags") or "").split("; ") if f]
    if flags:
        lines.append("🚩 <b>Alerts:</b>")
        for f in flags:
            lines.append(f"  • {H(f)}")

    day_summary = db.summary(p["day"])
    cashier_stat = next((w for w in day_summary["by_waiter"] if w["waiter_id"] == p["waiter_id"]), None)
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("📊 <b>Today's Running Sales:</b>")
    if cashier_stat:
        lines.append(f"• <b>{H(who)}:</b> {m(cashier_stat['total'])} ({cashier_stat['count']} tx)")
    lines.append(f"• <b>Store Total:</b> <b>{m(day_summary['claimed']['total'])}</b> ({day_summary['count']} tx)")
    if day_summary["verified"]["count"]:
        lines.append(f"• <b>Total Verified:</b> {m(day_summary['verified']['total'])}")

    return "\n".join(lines)


async def notify_owner_payment(bot, pid: int) -> None:
    p = db.get_payment(pid)
    if not p:
        return
    text = owner_payment_text(p)
    photo_file = (config.PHOTOS_DIR / p["photo_path"]) if p.get("photo_path") else None

    for oid in config.OWNER_IDS:
        if p["waiter_id"] == oid:
            continue
        try:
            if photo_file and photo_file.is_file():
                if len(text) <= 1024:
                    with open(photo_file, "rb") as pf:
                        await bot.send_photo(oid, photo=pf, caption=text, parse_mode="HTML")
                else:
                    with open(photo_file, "rb") as pf:
                        await bot.send_photo(oid, photo=pf, caption=f"🛒 Cashier Sale #{p['id']} · {H(p.get('bank') or '')}", parse_mode="HTML")
                    await bot.send_message(oid, text, parse_mode="HTML")
            else:
                await bot.send_message(oid, text, parse_mode="HTML")
        except TelegramError as e:
            log.warning("Could not send cashier transaction to owner %s: %s", oid, e)


async def finish_checks(bot, pids: list[int], chat_id: int, msg_id: int, extras: dict, who: str,
                        footer: list[str] | None = None) -> None:
    """Background task: check every entry with the bank, then update the waiter's message and alert owners."""
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
            await notify_owners(bot, f"🚨 {warning}\nNew entry #{pid} from {H(who)}.")
        if isinstance(res, Exception):
            log.exception("Bank check crashed for #%s", pid, exc_info=res)
            db.update_payment(pid, bank_check="error", bank_check_msg="Bank check failed (internal error)")
        elif res and res.status in ("mismatch", "not_found"):
            await notify_owners(bot, f"🔴 <b>Bank check failed</b> for #{pid} from {H(who)}:\n{H(res.message)}")
        # Notify owners with full structured card and photo
        await notify_owner_payment(bot, pid)
    try:
        await bot.edit_message_text(batch_text(pids, extras, False, footer), chat_id=chat_id, message_id=msg_id,
                                    parse_mode="HTML")
    except TelegramError as e:
        log.debug("Could not update message: %s", e)


def start_checks(ctx: ContextTypes.DEFAULT_TYPE, pids: list[int], sent, extras: dict, who: str,
                 footer: list[str] | None = None) -> None:
    if config.BANK_CHECK and pids:
        ctx.application.create_task(finish_checks(ctx.bot, pids, sent.chat_id, sent.message_id, extras, who, footer))


# ------------------------------------------------------------------ general commands
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u, msg = update.effective_user, update.effective_message
    if is_owner(u.id):
        db.upsert_user(u.id, u.full_name, u.username, "owner")
        await setup_owner_chat(ctx.bot, u.id)
        text = OWNER_HELP
        if not config.WEBAPP_URL:
            text += "\n\n⚠️ WEBAPP_URL is not set in .env, so the dashboard button is disabled."
        await msg.reply_html(text, reply_markup=dashboard_markup())
        await msg.reply_html("You can also send receipts yourself:\n\n" + WAITER_HELP)
    elif can_submit(u.id):
        await msg.reply_html(WAITER_HELP)
    else:
        await msg.reply_html("👋 Hi! Ask the owner for the join code, then send:\n<code>/join CODE</code>")


async def cmd_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u, msg = update.effective_user, update.effective_message
    if not config.JOIN_CODE:
        await msg.reply_text("Joining is disabled. Ask the owner.")
        return
    code = ctx.args[0] if ctx.args else ""
    if not hmac.compare_digest(code.encode(), config.JOIN_CODE.encode()):
        await msg.reply_text("❌ Wrong code.")
        return
    db.upsert_user(u.id, u.full_name, u.username, "owner" if is_owner(u.id) else "waiter")
    await msg.reply_html("✅ You're registered!\n\n" + WAITER_HELP)
    await notify_owners(ctx.bot, f"👤 New waiter joined: <b>{H(u.full_name)}</b> (id <code>{u.id}</code>)")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if is_owner(uid):
        await update.effective_message.reply_html(OWNER_HELP, reply_markup=dashboard_markup())
    else:
        await update.effective_message.reply_html(WAITER_HELP)


# ------------------------------------------------------------------ waiter: submissions
async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u, msg = update.effective_user, update.effective_message
    if not can_submit(u.id):
        await msg.reply_html("You're not registered. Send <code>/join CODE</code> first.")
        return

    tg_file = await (msg.photo[-1].get_file() if msg.photo else msg.document.get_file())
    day = config.business_day()
    folder = config.PHOTOS_DIR / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex}.jpg"
    await tg_file.download_to_drive(path)

    caption = msg.caption or ""
    info = await asyncio.to_thread(parsing.analyze_image, str(path), caption)
    r: banks.Receipt = info["receipt"]
    flags = banks.offline_flags(r)
    flags += info.get("tamper_flags", [])
    pid = db.add_payment(
        waiter_id=u.id, day=day, kind="photo",
        photo_path=str(path.relative_to(config.PHOTOS_DIR)),
        qr_data="\n".join(info["qr_data"]) or None,
        ocr_text=info["ocr_text"] or None,
        raw_text=caption or None,
        tx_ref=r.ref, amount=r.amount,
        note=" ".join(info["notes"]) or None,
    )
    verify.save_receipt_fields(pid, r, flags)
    extra = ["📷 QR code read ✓" if info["qr_data"] else "📷 No QR code found in the photo"]
    extra += [f"ℹ️ {H(n)}" for n in info["notes"]]
    extra.append(await check_duplicate(ctx.bot, pid, r.ref, u.full_name, r.url))
    await alert_flags(ctx.bot, pid, flags, u.full_name)

    checking = config.BANK_CHECK and bool(r.ref or r.url)
    sent = await msg.reply_html(entry_text(db.get_payment(pid), extra, checking))
    db.update_payment(pid, chat_id=sent.chat_id, reply_msg_id=sent.message_id)
    if r.amount is None:
        ctx.user_data["awaiting_amount"] = pid
    if checking:
        start_checks(ctx, [pid], sent, {pid: extra}, u.full_name)
    else:
        await notify_owner_payment(ctx.bot, pid)
    else:
        await notify_owner_payment(ctx.bot, pid)


async def _update_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE, target: dict,
                        ref: str | None, amount: float | None) -> None:
    """Waiter filled in a missing amount/code by replying."""
    u, msg = update.effective_user, update.effective_message
    pid = target["id"]
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
    extra = [await check_duplicate(ctx.bot, pid, p["tx_ref"], u.full_name)] if ref else []
    checking = config.BANK_CHECK and bool(p["tx_ref"] or p.get("receipt_url")) and p["status"] == "pending"
    sent = await msg.reply_html(entry_text(p, extra, checking, title="Updated"))
    db.update_payment(pid, chat_id=sent.chat_id, reply_msg_id=sent.message_id)
    if checking:
        start_checks(ctx, [pid], sent, {pid: extra}, u.full_name)
    else:
        await notify_owner_payment(ctx.bot, pid)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u, msg = update.effective_user, update.effective_message
    if not can_submit(u.id):
        await msg.reply_html("You're not registered. Send <code>/join CODE</code> first.")
        return
    text = msg.text.strip()

    # 1) Reply to one of the bot's receipt messages -> fill in the missing amount / code
    target = None
    if msg.reply_to_message:
        target = db.payment_by_reply(msg.chat_id, msg.reply_to_message.message_id)
        if target and target["waiter_id"] != u.id and not is_owner(u.id):
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
        flags = banks.offline_flags(r)
        pid = db.add_payment(waiter_id=u.id, kind="text", raw_text=raw, tx_ref=r.ref, amount=r.amount)
        verify.save_receipt_fields(pid, r, flags)
        extras[pid] = [await check_duplicate(ctx.bot, pid, r.ref, u.full_name)]
        await alert_flags(ctx.bot, pid, flags, u.full_name)
        pids.append(pid)

    checking = config.BANK_CHECK
    sent = await msg.reply_html(batch_text(pids, extras, checking, footer))
    if len(pids) == 1:
        db.update_payment(pids[0], chat_id=sent.chat_id, reply_msg_id=sent.message_id)
        if db.get_payment(pids[0])["amount"] is None:
            ctx.user_data["awaiting_amount"] = pids[0]
    start_checks(ctx, pids, sent, extras, u.full_name, footer)


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not can_submit(u.id):
        return
    day = config.business_day()
    rows = db.payments_for_day(day, waiter_id=u.id)
    if not rows:
        await update.effective_message.reply_text("No entries from you today yet.")
        return
    counted = [r for r in rows if not db.is_excluded(r)]
    total = sum(r["amount"] or 0 for r in counted)
    lines = [f"📋 <b>Your entries — {day}</b>"]
    for r in rows[-40:]:
        flag = {"verified": "✅", "rejected": "❌"}.get(r["status"], "⏳")
        if r["duplicate_of"] or db.has_problem(r):
            flag = "⚠️"
        lines.append(f"{flag} #{r['id']} {config.local_time(r['created_at'])} "
                     f"<code>{H(r['tx_ref'] or '—')}</code> {config.money(r['amount'])}")
    lines.append(f"\nTotal: <b>{config.money(total)}</b> ({len(counted)} entries)")
    await update.effective_message.reply_html("\n".join(lines))


async def cmd_undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u, msg = update.effective_user, update.effective_message
    p = db.last_payment(u.id)
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
    await msg.reply_html(f"🗑 Deleted #{p['id']} (<code>{H(p['tx_ref'] or '—')}</code> {config.money(p['amount'])})")


# ------------------------------------------------------------------ owner commands
def owner_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update.effective_user.id):
            await update.effective_message.reply_text("Owner only.")
            return
        return await func(update, ctx)
    return wrapper


@owner_only
async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    markup = dashboard_markup()
    if not markup:
        await update.effective_message.reply_text("Set WEBAPP_URL in .env to enable the dashboard. Use /summary meanwhile.")
        return
    await update.effective_message.reply_text("Tap to open the dashboard:", reply_markup=markup)


@owner_only
async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    day = parse_day_arg(ctx.args)
    if not day:
        await update.effective_message.reply_text("Usage: /summary [YYYY-MM-DD | yesterday]")
        return
    await update.effective_message.reply_html(reports.format_summary(db.summary(day)),
                                              reply_markup=dashboard_markup())


@owner_only
async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    day = parse_day_arg(ctx.args)
    if not day:
        await update.effective_message.reply_text("Usage: /export [YYYY-MM-DD | yesterday]")
        return
    await update.effective_message.reply_document(document=reports.csv_bytes(day),
                                                  filename=f"payments_{day}.csv")


@owner_only
async def cmd_waiters(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    waiters = db.list_waiters()
    if not waiters:
        await update.effective_message.reply_html(
            f"No waiters yet. They join with <code>/join {H(config.JOIN_CODE or 'CODE')}</code>")
        return
    lines = ["👥 <b>Waiters</b>"]
    for w in waiters:
        lines.append(f"{'🟢' if w['active'] else '⚪️'} {H(w['name'])} — <code>{w['tg_id']}</code>")
    lines.append("\nRemove: /remove &lt;id&gt;")
    await update.effective_message.reply_html("\n".join(lines))


@owner_only
async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args or not ctx.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /remove <waiter id> (see /waiters)")
        return
    ok = db.set_user_active(int(ctx.args[0]), False)
    await update.effective_message.reply_text("✅ Removed." if ok else "No waiter with that id.")


# ------------------------------------------------------------------ scheduled + setup
async def daily_report(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = db.summary(config.business_day())
    await notify_owners(ctx.bot, "🌙 <b>End of day</b>\n\n" + reports.format_summary(s), dashboard_markup())


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


WAITER_COMMANDS = [BotCommand("today", "My entries today"), BotCommand("undo", "Delete my last entry"),
                   BotCommand("help", "How to send payments")]
OWNER_COMMANDS = [BotCommand("report", "Open dashboard"), BotCommand("summary", "Today's totals"),
                  BotCommand("export", "CSV export"), BotCommand("waiters", "List waiters"),
                  BotCommand("help", "Help")] + WAITER_COMMANDS[:2]


async def setup_bot(app: Application) -> None:
    await app.bot.set_my_commands(WAITER_COMMANDS)
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
    app.add_handler(CommandHandler("start", cmd_start, private))
    app.add_handler(CommandHandler("join", cmd_join, private))
    app.add_handler(CommandHandler("help", cmd_help, private))
    app.add_handler(CommandHandler("today", cmd_today, private))
    app.add_handler(CommandHandler("undo", cmd_undo, private))
    app.add_handler(CommandHandler("report", cmd_report, private))
    app.add_handler(CommandHandler("summary", cmd_summary, private))
    app.add_handler(CommandHandler("export", cmd_export, private))
    app.add_handler(CommandHandler("waiters", cmd_waiters, private))
    app.add_handler(CommandHandler("remove", cmd_remove, private))
    app.add_handler(MessageHandler(private & (filters.PHOTO | filters.Document.IMAGE), on_photo))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    hour, minute = (int(x) for x in config.DAILY_REPORT_TIME.split(":"))
    if app.job_queue is None:
        log.warning('Nightly report disabled: run  pip install "python-telegram-bot[job-queue]"')
    else:
        app.job_queue.run_daily(daily_report, time=dtime(hour, minute, tzinfo=config.TZ), name="daily_report")
    return app
