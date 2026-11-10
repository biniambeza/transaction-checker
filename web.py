"""Dashboard and management server for the Telegram Mini App (authenticated with Telegram initData)."""
import hashlib
import hmac
import html
import json
import logging
import re
import time
from urllib.parse import parse_qsl

from aiohttp import web
from telegram import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

import banks
import config
import db
import pdf_report
import reports
import verify

log = logging.getLogger(__name__)
WEBAPP_DIR = config.BASE_DIR / "webapp"
BOT_KEY = web.AppKey("bot", object)
DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
MAX_AUTH_AGE = 24 * 3600


def check_init_data(init_data: str) -> dict | None:
    """Validates Telegram Mini App initData (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)."""
    if not init_data:
        return None
    data = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = data.pop("hash", None)
    if not received_hash:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None
    if time.time() - int(data.get("auth_date", "0")) > MAX_AUTH_AGE:
        return None
    try:
        return json.loads(data.get("user", "{}"))
    except ValueError:
        return None


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path.startswith("/api/"):
        init_data = request.headers.get("X-Telegram-Init-Data") or request.query.get("auth", "")
        user = check_init_data(init_data)
        if not user:
            raise web.HTTPUnauthorized(text="Open this dashboard from the Telegram bot (invalid authentication).")
        uid = user.get("id")
        is_admin = db.is_superadmin(uid)
        owned_biz = db.get_user_owned_businesses(uid)
        waiter_biz = db.get_waiter_businesses(uid)
        if is_admin:
            role = "superadmin"
        elif owned_biz:
            role = "owner"
        elif waiter_biz:
            role = "waiter"
        else:
            role = "guest"

        request["user"] = user
        request["role"] = role
    return await handler(request)


def get_req_business(request: web.Request, required_owner: bool = False) -> dict:
    uid = request["user"]["id"]
    is_admin = db.is_superadmin(uid)

    bid_str = request.query.get("business_id") or request.match_info.get("id")
    if bid_str and str(bid_str).isdigit():
        bid = int(bid_str)
        biz = db.get_business(bid)
        if biz:
            if is_admin or db.is_business_owner(bid, uid):
                return biz
            if not required_owner and any(wb["id"] == bid for wb in db.get_waiter_businesses(uid)):
                return biz

    owned = db.get_user_owned_businesses(uid)
    if owned:
        return owned[0]
    if is_admin:
        all_b = db.list_businesses()
        if all_b:
            return all_b[0]
    if not required_owner:
        waiter_b = db.get_waiter_businesses(uid)
        if waiter_b:
            return waiter_b[0]
    raise web.HTTPForbidden(text="No accessible store found for your account.")


def _day(request: web.Request) -> str:
    day = request.match_info["day"]
    if not DAY_RE.fullmatch(day):
        raise web.HTTPBadRequest(text="Bad date")
    return day


def _split_accounts(accounts_str: str) -> dict:
    accs = [a.strip() for a in (accounts_str or "").split(",") if a.strip()]
    cbe = ""
    telebirr = ""
    for a in accs:
        digits = re.sub(r"\D", "", a)
        if len(digits) == 13 and digits.startswith("1") and not cbe:
            cbe = digits
        elif not telebirr:
            telebirr = digits or a
    return {"cbe": cbe, "telebirr": telebirr, "raw": accounts_str or ""}


def _serialize(p: dict) -> dict:
    return {
        "id": p["id"],
        "business_id": p.get("business_id"),
        "business_name": p.get("business_name"),
        "day": p["day"],
        "time": config.local_time(p["created_at"]),
        "waiter": p.get("waiter_name") or f"Cashier {p['waiter_id']}",
        "waiter_id": p["waiter_id"],
        "kind": p["kind"],
        "has_photo": bool(p["photo_path"]),
        "qr_data": p["qr_data"],
        "raw_text": p["raw_text"],
        "ocr_text": (p["ocr_text"] or "")[:1500] or None,
        "tx_ref": p["tx_ref"],
        "amount": p["amount"],
        "status": p["status"],
        "duplicate_of": p["duplicate_of"],
        "note": p["note"],
        "bank": p.get("bank"),
        "receipt_url": p.get("receipt_url"),
        "payer": p.get("payer"),
        "receiver": p.get("receiver"),
        "receiver_account": p.get("receiver_account"),
        "receipt_time": config.local_datetime(p["receipt_time"]) if p.get("receipt_time") else None,
        "flags": [f for f in (p.get("flags") or "").split("; ") if f],
        "bank_check": p.get("bank_check"),
        "bank_check_msg": p.get("bank_check_msg"),
        "problem": db.has_problem(p),
    }


# ------------------------------------------------------------------ views & endpoints
async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEBAPP_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def api_me(request: web.Request) -> web.Response:
    uid = request["user"]["id"]
    is_admin = db.is_superadmin(uid)
    user_row = db.get_user(uid)
    owned = db.list_all_businesses() if is_admin else db.get_user_owned_businesses(uid)
    waiter_b = db.get_waiter_businesses(uid)

    return web.json_response({
        "user_id": uid,
        "name": user_row["name"] if user_row else request["user"].get("first_name", "User"),
        "role": request["role"],
        "is_superadmin": is_admin,
        "active_business_id": user_row.get("active_business_id") if user_row else None,
        "owned_businesses": owned,
        "waiter_businesses": waiter_b,
        "pending_business_request": db.get_user_pending_business_request(uid),
    })


async def api_switch_active_business(request: web.Request) -> web.Response:
    uid = request["user"]["id"]
    body = await request.json()
    bid = int(body.get("business_id", 0))
    biz = db.get_business(bid)
    if not biz:
        raise web.HTTPNotFound(text="Business not found")
    db.set_user_active_business(uid, bid)
    return web.json_response({"ok": True, "active_business_id": bid, "business_name": biz["name"]})


async def api_join_business(request: web.Request) -> web.Response:
    uid = request["user"]["id"]
    u = db.get_user(uid) or {}
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid request payload"}, status=400)
    code = (data.get("code") or "").strip().lower()
    custom_name = (data.get("name") or "").strip()
    if not code:
        return web.json_response({"error": "Please enter a business join code"}, status=400)
    biz = db.get_business_by_code(code)
    if not biz or not biz["active"]:
        return web.json_response({"error": "Business code not found. Please ask your store owner for the correct code."}, status=404)
    cashier_name = f"{custom_name} ({u.get('name') or uid})" if custom_name else (u.get("name") or str(uid))
    if db.is_waiter_active_in_business(biz["id"], uid):
        return web.json_response({"status": "already_active", "message": f"You are already an active cashier for {biz['name']}."})
    db.upsert_user(uid, cashier_name, u.get("username"), "waiter")
    db.add_waiter_to_business(biz["id"], uid, cashier_name, active=0)

    # Notify store owners with approval card
    bot = request.app.get(BOT_KEY)
    if bot:
        owners = db.get_business_owners(biz["id"])
        owner_ids = {o["tg_id"] for o in owners}
        if not owner_ids:
            owner_ids = set(config.OWNER_IDS)
        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve Cashier", callback_data=f"waiter_appr:{biz['id']}:{uid}"),
                InlineKeyboardButton("❌ Decline", callback_data=f"waiter_decl:{biz['id']}:{uid}")
            ]
        ])
        for oid in owner_ids:
            try:
                await bot.send_message(
                    oid,
                    f"👤 <b>New Cashier Join Request!</b>\n\n"
                    f"• Store: <b>{html.escape(biz['name'])}</b>\n"
                    f"• Cashier: <b>{html.escape(cashier_name)}</b>\n"
                    f"• Telegram ID: <code>{uid}</code>\n\n"
                    f"Tap below or approve in your WebApp Cashiers tab:",
                    reply_markup=markup,
                    parse_mode="HTML"
                )
            except Exception:
                pass
    return web.json_response({"ok": True, "message": f"Join request sent to the owner of {biz['name']}! They will approve you shortly."})


async def api_request_business(request: web.Request) -> web.Response:
    """User requests to register as a business owner."""
    uid = request["user"]["id"]
    u = db.get_user(uid) or {}
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid request payload"}, status=400)

    name = (body.get("business_name") or "").strip()
    if not name:
        return web.json_response({"error": "Business name is required"}, status=400)

    category = (body.get("category") or "").strip()
    phone = (body.get("phone") or "").strip()
    code = (body.get("code") or "").strip().lower()
    cbe = (body.get("cbe") or "").strip()
    telebirr = (body.get("telebirr") or "").strip()
    names = (body.get("names") or "").strip()

    req_id = db.create_business_request(
        tg_id=uid,
        user_name=u.get("name") or request["user"].get("first_name", ""),
        username=u.get("username") or request["user"].get("username"),
        business_name=name,
        category=category,
        phone=phone,
        code=code,
        cbe=cbe,
        telebirr=telebirr,
        names=names
    )

    # Notify Super Admin (Platform Owner)
    bot = request.app.get(BOT_KEY)
    if bot:
        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve Business", callback_data=f"biz_req_appr:{req_id}"),
                InlineKeyboardButton("❌ Decline", callback_data=f"biz_req_decl:{req_id}")
            ]
        ])
        for admin_id in config.OWNER_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"🏢 <b>New Business Owner Request!</b>\n\n"
                    f"• <b>Business:</b> {html.escape(name)}\n"
                    f"• <b>Category:</b> {html.escape(category or 'General')}\n"
                    f"• <b>Applicant:</b> {html.escape(u.get('name') or str(uid))}" + (f" (@{u.get('username')})" if u.get('username') else "") + f"\n"
                    f"• <b>Telegram ID:</b> <code>{uid}</code>\n"
                    f"• <b>Phone:</b> {html.escape(phone or 'Not provided')}\n"
                    f"• <b>Desired Code:</b> <code>{html.escape(code or 'auto')}</code>\n"
                    f"• <b>CBE Account:</b> {html.escape(cbe or 'None')}\n"
                    f"• <b>telebirr:</b> {html.escape(telebirr or 'None')}\n"
                    f"• <b>Legal Name:</b> {html.escape(names or 'None')}\n\n"
                    f"Tap below to approve or decline:",
                    reply_markup=markup,
                    parse_mode="HTML"
                )
            except Exception:
                pass

    return web.json_response({
        "ok": True,
        "message": "Your request has been submitted to the platform admin! You will be notified in Telegram as soon as it is approved."
    })


async def api_list_business_requests(request: web.Request) -> web.Response:
    uid = request["user"]["id"]
    if not db.is_superadmin(uid):
        raise web.HTTPForbidden(text="Only platform admin can view business requests")
    return web.json_response({
        "requests": db.list_all_business_requests(),
        "pending": db.list_pending_business_requests()
    })


async def api_approve_business_request(request: web.Request) -> web.Response:
    uid = request["user"]["id"]
    if not db.is_superadmin(uid):
        raise web.HTTPForbidden(text="Only platform admin can approve business requests")
    req_id = int(request.match_info["id"])
    approved = db.approve_business_request(req_id, uid)
    if not approved:
        raise web.HTTPNotFound(text="Request not found or already processed")

    # Notify applicant
    bot = request.app.get(BOT_KEY)
    biz = approved.get("business") or {}
    if bot:
        try:
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("📱 Open Store Dashboard", web_app=WebAppInfo(url=config.WEBAPP_URL + "/"))]
            ]) if config.WEBAPP_URL else None
            await bot.send_message(
                approved["tg_id"],
                f"🎉 <b>Congratulations! Your Business Owner Request was APPROVED!</b>\n\n"
                f"• Store: <b>{html.escape(approved['business_name'])}</b>\n"
                f"• Cashier Join Code: <code>{html.escape(biz.get('code', ''))}</code>\n\n"
                f"You now have full access as the <b>Store Owner</b>!\n"
                f"Tap the button below to open your dashboard, set payment accounts, and approve cashiers:",
                reply_markup=markup,
                parse_mode="HTML"
            )
        except Exception:
            pass

    return web.json_response({"ok": True, "request": approved})


async def api_reject_business_request(request: web.Request) -> web.Response:
    uid = request["user"]["id"]
    if not db.is_superadmin(uid):
        raise web.HTTPForbidden(text="Only platform admin can reject business requests")
    req_id = int(request.match_info["id"])
    rejected = db.reject_business_request(req_id, uid)
    if not rejected:
        raise web.HTTPNotFound(text="Request not found or already processed")

    # Notify applicant
    bot = request.app.get(BOT_KEY)
    if bot:
        try:
            await bot.send_message(
                rejected["tg_id"],
                f"ℹ️ Your request to register <b>{html.escape(rejected['business_name'])}</b> as a store owner was not approved at this time.",
                parse_mode="HTML"
            )
        except Exception:
            pass

    return web.json_response({"ok": True, "request": rejected})


async def api_businesses(request: web.Request) -> web.Response:
    uid = request["user"]["id"]
    is_admin = db.is_superadmin(uid)
    businesses = db.list_all_businesses_with_details() if is_admin else db.get_user_owned_businesses(uid)
    return web.json_response({
        "businesses": businesses,
        "is_superadmin": is_admin
    })


async def api_platform_stats(request: web.Request) -> web.Response:
    uid = request["user"]["id"]
    if not db.is_superadmin(uid):
        raise web.HTTPForbidden(text="Only bot owner can view platform stats")
    return web.json_response(db.get_platform_stats())


async def api_update_business(request: web.Request) -> web.Response:
    """Bot Owner updates a business and assigned owner."""
    uid = request["user"]["id"]
    if not db.is_superadmin(uid):
        raise web.HTTPForbidden(text="Only bot owner can update businesses")

    bid = int(request.match_info["id"])
    biz = db.get_business(bid)
    if not biz:
        raise web.HTTPNotFound(text="Business not found")

    body = await request.json()
    updates = {}
    if "name" in body and body["name"].strip():
        updates["name"] = body["name"].strip()
    if "code" in body and body["code"].strip():
        updates["code"] = re.sub(r"[^a-zA-Z0-9_-]", "", body["code"].strip()).lower()
    if "active" in body:
        updates["active"] = 1 if body["active"] else 0
    if "accounts" in body:
        updates["accounts"] = body["accounts"].strip()
    if "names" in body:
        updates["names"] = body["names"].strip()

    if updates:
        db.update_business(bid, **updates)

    # Re-assign owner if provided
    owner_str = str(body.get("owner_id") or "").strip()
    if owner_str:
        owner_id = None
        if owner_str.isdigit():
            owner_id = int(owner_str)
        elif owner_str.startswith("@"):
            u = db.get_user_by_username(owner_str)
            if u:
                owner_id = u["tg_id"]
        if owner_id:
            db.set_business_owner(bid, owner_id)
            bot = request.app.get(BOT_KEY)
            if bot:
                try:
                    await bot.send_message(
                        owner_id,
                        f"🎉 <b>You are now the assigned Owner of {biz['name']}!</b>\n\n"
                        f"Open your live WebApp dashboard to manage your cashiers and view sales.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

    return web.json_response({"ok": True, "business": db.get_business(bid)})


async def api_delete_business(request: web.Request) -> web.Response:
    """Bot Owner deactivates or removes a business."""
    uid = request["user"]["id"]
    if not db.is_superadmin(uid):
        raise web.HTTPForbidden(text="Only bot owner can remove businesses")
    bid = int(request.match_info["id"])
    db.delete_business(bid)
    return web.json_response({"ok": True, "message": "Business deactivated"})


async def api_create_business(request: web.Request) -> web.Response:
    """Platform owner registers a new business via WebApp input form."""
    uid = request["user"]["id"]
    if not db.is_superadmin(uid):
        raise web.HTTPForbidden(text="Only platform owner can register businesses")

    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise web.HTTPBadRequest(text="Business name is required")

    code = (body.get("code") or "").strip().lower() or None
    owner_str = str(body.get("owner_id") or "").strip()
    cbe = (body.get("cbe") or "").strip()
    telebirr = (body.get("telebirr") or "").strip()
    names = (body.get("names") or "").strip()

    accounts = ",".join(x for x in (re.sub(r"\D", "", cbe), re.sub(r"\D", "", telebirr)) if x)

    bid = db.create_business(name=name, code=code, accounts=accounts, names=names)
    biz = db.get_business(bid)

    owner_id = None
    if owner_str:
        if owner_str.isdigit():
            owner_id = int(owner_str)
        elif owner_str.startswith("@"):
            u = db.get_user_by_username(owner_str)
            if u:
                owner_id = u["tg_id"]

    if owner_id:
        db.add_business_owner(bid, owner_id)
        bot = request.app[BOT_KEY]
        try:
            await bot.send_message(
                owner_id,
                f"🎉 <b>Welcome! You have been registered as the Owner of {name}!</b>\n\n"
                f"• Business ID: <code>{bid}</code>\n"
                f"• Cashier Join Code: <code>{biz['code']}</code>\n\n"
                f"Open your live WebApp dashboard to set your payment accounts and manage your cashiers.",
                parse_mode="HTML"
            )
        except Exception:
            pass

    return web.json_response({"ok": True, "business": db.get_business(bid)})


async def api_business_settings(request: web.Request) -> web.Response:
    """Store owner saves their CBE, telebirr, legal names, and join code via WebApp form."""
    biz = get_req_business(request, required_owner=True)
    body = await request.json()

    cbe = (body.get("cbe") or "").strip()
    telebirr = (body.get("telebirr") or "").strip()
    names = (body.get("names") or "").strip()
    code = (body.get("code") or "").strip().lower()

    updates = {}
    if names:
        updates["names"] = names

    # Clean accounts
    clean_cbe = re.sub(r"\D", "", cbe) if cbe else None
    clean_tb = re.sub(r"\D", "", telebirr) if telebirr else None

    # Current accounts
    current = [a.strip() for a in (biz["accounts"] or "").split(",") if a.strip()]
    other = [a for a in current if not (len(re.sub(r"\D", "", a)) == 13 and re.sub(r"\D", "", a).startswith("1"))
             and not (len(re.sub(r"\D", "", a)) in (10, 12) and (re.sub(r"\D", "", a).startswith("09") or re.sub(r"\D", "", a).startswith("2519")))]

    new_accs = []
    if clean_cbe:
        new_accs.append(clean_cbe)
    if clean_tb:
        new_accs.append(clean_tb)
    new_accs.extend(other)

    if new_accs:
        updates["accounts"] = ",".join(new_accs)

    if code and code != biz["code"]:
        existing = db.get_business_by_code(code)
        if existing and existing["id"] != biz["id"]:
            raise web.HTTPBadRequest(text="That cashier code is already taken. Please choose another.")
        updates["code"] = code

    if updates:
        db.update_business(biz["id"], **updates)

    updated_biz = db.get_business(biz["id"])
    return web.json_response({
        "ok": True,
        "business": updated_biz,
        "accounts_split": _split_accounts(updated_biz["accounts"])
    })


async def api_business_waiters(request: web.Request) -> web.Response:
    """Lists registered and pending cashiers for the business."""
    biz = get_req_business(request, required_owner=True)
    waiters = db.get_waiters_for_business(biz["id"])
    return web.json_response({
        "waiters": waiters,
        "business": biz
    })


async def api_add_waiter(request: web.Request) -> web.Response:
    """Store owner registers a cashier via WebApp form."""
    biz = get_req_business(request, required_owner=True)
    body = await request.json()

    target_str = str(body.get("tg_id") or "").strip()
    counter_name = (body.get("counter_name") or "").strip()

    target_id = None
    if target_str.isdigit():
        target_id = int(target_str)
    elif target_str.startswith("@"):
        u = db.get_user_by_username(target_str)
        if u:
            target_id = u["tg_id"]
            if not counter_name:
                counter_name = u["name"]

    if not target_id:
        raise web.HTTPBadRequest(text="Please enter a valid numeric Telegram ID or existing username.")

    name_display = counter_name or f"Cashier {target_id}"
    db.upsert_user(target_id, name_display, None, "waiter")
    db.add_waiter_to_business(biz["id"], target_id, name_display, active=1)

    # Send welcome to waiter
    bot = request.app[BOT_KEY]
    try:
        await bot.send_message(
            target_id,
            f"🎉 <b>You have been registered as a cashier for {biz['name']}!</b>\n\n"
            f"Counter Name: <b>{name_display}</b>\n"
            f"You can now take photos of customer receipts and send them here.",
            parse_mode="HTML"
        )
    except Exception:
        pass

    return web.json_response({
        "ok": True,
        "waiters": db.get_waiters_for_business(biz["id"])
    })


async def api_update_waiter_status(request: web.Request) -> web.Response:
    """Approve or deactivate a cashier."""
    biz = get_req_business(request, required_owner=True)
    wid = int(request.match_info["wid"])
    body = await request.json()
    active = bool(body.get("active", True))

    db.set_business_waiter_active(biz["id"], wid, active)

    bot = request.app[BOT_KEY]
    if active:
        try:
            await bot.send_message(
                wid,
                f"🎉 <b>Your registration for {biz['name']} was APPROVED by the owner!</b>\n"
                f"You can now submit customer receipts for {biz['name']}.",
                parse_mode="HTML"
            )
        except Exception:
            pass

    return web.json_response({
        "ok": True,
        "waiters": db.get_waiters_for_business(biz["id"])
    })


async def api_delete_waiter(request: web.Request) -> web.Response:
    biz = get_req_business(request, required_owner=True)
    wid = int(request.match_info["wid"])
    db.remove_waiter_from_business(biz["id"], wid)
    return web.json_response({
        "ok": True,
        "waiters": db.get_waiters_for_business(biz["id"])
    })


async def api_report_data(request: web.Request) -> web.Response:
    biz = get_req_business(request)
    uid = request["user"]["id"]
    role = request["role"]
    period_type = request.query.get("period_type", "day").lower()
    if period_type not in ("day", "month", "year"):
        period_type = "day"

    period_val = request.query.get("period_value", "").strip()
    if not period_val:
        today = config.business_day()
        if period_type == "day":
            period_val = today
        elif period_type == "month":
            period_val = today[:7]
        else:
            period_val = today[:4]

    rows = db.payments_for_period(biz["id"], period_type, period_val)

    # Cashier role: strictly show ONLY this cashier's sent money amounts
    if role == "waiter":
        rows = [r for r in rows if r.get("waiter_id") == uid]
        summary = db.summary(business_id=biz["id"], period_type=period_type, period_value=period_val, rows=rows)
        summary["by_waiter"] = {}
        summary["by_bank"] = {}
        summary["by_payer"] = {}
        summary["bank_total"] = None
    else:
        summary = db.summary(business_id=biz["id"], period_type=period_type, period_value=period_val, rows=rows)

    return web.json_response({
        "business": biz,
        "accounts_split": _split_accounts(biz["accounts"]) if role != "waiter" else {},
        "currency": biz.get("currency") or config.CURRENCY,
        "period_type": period_type,
        "period_value": period_val,
        "summary": summary,
        "payments": [_serialize(p) for p in rows],
        "recent_periods": {
            "days": db.recent_days(biz["id"]),
            "months": db.recent_months(biz["id"]),
            "years": db.recent_years(biz["id"]),
        } if role != "waiter" else {"days": [], "months": [], "years": []}
    })


async def api_pdf(request: web.Request) -> web.Response:
    biz = get_req_business(request)
    period_type = request.query.get("period_type", "day").lower()
    if period_type not in ("day", "month", "year"):
        period_type = "day"

    period_val = request.query.get("period_value", "").strip()
    if not period_val:
        today = config.business_day()
        period_val = today if period_type == "day" else (today[:7] if period_type == "month" else today[:4])

    rows = db.payments_for_period(biz["id"], period_type, period_val)
    summary = db.summary(business_id=biz["id"], period_type=period_type, period_value=period_val, rows=rows)
    title = f"{period_type.capitalize()} Report — {period_val}"

    pdf_bytes = await pdf_report.generate_pdf(biz, summary, rows, title)
    filename = f"report_{biz['code']}_{period_val}.pdf"
    return web.Response(
        body=pdf_bytes,
        content_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


async def api_days(request: web.Request) -> web.Response:
    biz = get_req_business(request)
    return web.json_response({
        "today": config.business_day(),
        "days": db.recent_days(biz["id"]),
        "currency": biz.get("currency") or config.CURRENCY
    })


async def api_day(request: web.Request) -> web.Response:
    biz = get_req_business(request)
    day = _day(request)
    rows = db.payments_for_period(biz["id"], "day", day)
    return web.json_response({
        "summary": db.summary(business_id=biz["id"], period_type="day", period_value=day, rows=rows),
        "payments": [_serialize(p) for p in rows]
    })


async def api_update_payment(request: web.Request) -> web.Response:
    pid = int(request.match_info["pid"])
    p = db.get_payment(pid)
    if not p:
        raise web.HTTPNotFound(text="No such entry")

    uid = request["user"]["id"]
    if not (db.is_superadmin(uid) or db.is_business_owner(p.get("business_id", 1), uid)):
        raise web.HTTPForbidden(text="Not authorized to edit this payment")

    body = await request.json()
    changes = {}
    if "status" in body:
        if body["status"] not in ("pending", "verified", "rejected"):
            raise web.HTTPBadRequest(text="Bad status")
        changes["status"] = body["status"]
        changes["reviewed_at"] = config.now_utc().isoformat()
    if "amount" in body:
        amount = body["amount"]
        if amount in (None, ""):
            changes["amount"] = None
        else:
            try:
                changes["amount"] = round(float(amount), 2)
            except (TypeError, ValueError):
                raise web.HTTPBadRequest(text="Bad amount")
    if "tx_ref" in body:
        ref = str(body["tx_ref"] or "").strip().upper() or None
        if ref != p["tx_ref"]:
            changes.update(bank_check=None, bank_check_msg=None)
            if not p.get("bank"):
                changes["bank"] = banks.guess_bank_from_ref(ref)
        changes["tx_ref"] = ref
        dup, _ = db.find_duplicate(ref, pid, p.get("business_id")) if ref else (None, True)
        changes["duplicate_of"] = dup["id"] if dup else None
    if body.get("clear_duplicate"):
        changes["duplicate_of"] = None
    db.update_payment(pid, **changes)
    return web.json_response(_serialize(db.get_payment(pid)))


async def api_bank_check(request: web.Request) -> web.Response:
    pid = int(request.match_info["pid"])
    p = db.get_payment(pid)
    if not p:
        raise web.HTTPNotFound(text="No such entry")
    uid = request["user"]["id"]
    if not (db.is_superadmin(uid) or db.is_business_owner(p.get("business_id", 1), uid)):
        raise web.HTTPForbidden(text="Not authorized")
    await verify.check_payment(pid)
    return web.json_response(_serialize(db.get_payment(pid)))


async def api_verify_all(request: web.Request) -> web.Response:
    biz = get_req_business(request)
    day = _day(request)
    return web.json_response({"updated": db.verify_all_pending(day, business_id=biz["id"])})


async def api_bank_total(request: web.Request) -> web.Response:
    biz = get_req_business(request)
    day = _day(request)
    body = await request.json()
    value = body.get("bank_total")
    try:
        total = None if value in (None, "") else round(float(value), 2)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Bad amount")
    db.set_bank_total(biz["id"], day, total)
    return web.json_response(db.summary(business_id=biz["id"], period_type="day", period_value=day))


async def api_export(request: web.Request) -> web.Response:
    biz = get_req_business(request)
    day = _day(request)
    bot = request.app[BOT_KEY]
    csv_bytes = reports.csv_bytes(day, business_id=biz["id"], period_type="day")
    await bot.send_document(
        request["user"]["id"],
        document=csv_bytes,
        filename=f"payments_{biz['code']}_{day}.csv",
        caption=f"Payments {biz['name']} ({day})"
    )
    return web.json_response({"ok": True})


async def api_photo(request: web.Request) -> web.FileResponse:
    p = db.get_payment(int(request.match_info["pid"]))
    if not p or not p["photo_path"]:
        raise web.HTTPNotFound()
    path = (config.PHOTOS_DIR / p["photo_path"]).resolve()
    if config.PHOTOS_DIR.resolve() not in path.parents or not path.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


async def start_web(bot) -> web.AppRunner:
    app = web.Application(middlewares=[auth_middleware])
    app[BOT_KEY] = bot
    app.add_routes([
        web.get("/", index),
        web.get("/api/me", api_me),
        web.post("/api/switch-business", api_switch_active_business),
        web.post("/api/join", api_join_business),
        web.post("/api/request-business", api_request_business),
        web.get("/api/business-requests", api_list_business_requests),
        web.post("/api/business-requests/{id:\\d+}/approve", api_approve_business_request),
        web.post("/api/business-requests/{id:\\d+}/reject", api_reject_business_request),
        web.get("/api/businesses", api_businesses),
        web.post("/api/businesses", api_create_business),
        web.put("/api/business/{id:\\d+}", api_update_business),
        web.delete("/api/business/{id:\\d+}", api_delete_business),
        web.get("/api/platform/stats", api_platform_stats),
        web.put("/api/business/{id}/settings", api_business_settings),
        web.get("/api/business/{id}/waiters", api_business_waiters),
        web.post("/api/business/{id}/waiters", api_add_waiter),
        web.patch("/api/business/{id}/waiters/{wid:\\d+}", api_update_waiter_status),
        web.delete("/api/business/{id}/waiters/{wid:\\d+}", api_delete_waiter),
        web.get("/api/report-data", api_report_data),
        web.get("/api/pdf", api_pdf),
        web.get("/api/days", api_days),
        web.get("/api/day/{day}", api_day),
        web.post("/api/day/{day}/verify-all", api_verify_all),
        web.put("/api/day/{day}/bank", api_bank_total),
        web.post("/api/day/{day}/export", api_export),
        web.patch("/api/payments/{pid:\\d+}", api_update_payment),
        web.post("/api/payments/{pid:\\d+}/check", api_bank_check),
        web.get("/api/photo/{pid:\\d+}", api_photo),
    ])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, config.WEB_HOST, config.WEB_PORT).start()
    log.info("Dashboard listening on http://%s:%s (public URL: %s)", config.WEB_HOST, config.WEB_PORT,
             config.WEBAPP_URL or "not set")
    return runner
