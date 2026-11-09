"""Dashboard server for the Telegram Mini App (authenticated with Telegram initData)."""
import hashlib
import hmac
import json
import logging
import re
import time
from urllib.parse import parse_qsl

from aiohttp import web

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
        if not (db.is_superadmin(uid) or len(db.get_user_owned_businesses(uid)) > 0):
            raise web.HTTPUnauthorized(text="Access restricted: only registered business owners can access this dashboard.")
        request["user"] = user
    return await handler(request)


def get_req_business(request: web.Request) -> dict:
    uid = request["user"]["id"]
    bid_str = request.query.get("business_id")
    if bid_str and bid_str.isdigit():
        bid = int(bid_str)
        biz = db.get_business(bid)
        if biz and (db.is_superadmin(uid) or db.is_business_owner(bid, uid)):
            return biz
    owned = db.get_user_owned_businesses(uid)
    if owned:
        return owned[0]
    if db.is_superadmin(uid):
        all_b = db.list_businesses()
        if all_b:
            return all_b[0]
    raise web.HTTPForbidden(text="No accessible business found for your account.")


def _day(request: web.Request) -> str:
    day = request.match_info["day"]
    if not DAY_RE.fullmatch(day):
        raise web.HTTPBadRequest(text="Bad date")
    return day


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


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEBAPP_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def api_businesses(request: web.Request) -> web.Response:
    uid = request["user"]["id"]
    is_admin = db.is_superadmin(uid)
    businesses = db.list_businesses() if is_admin else db.get_user_owned_businesses(uid)
    return web.json_response({
        "businesses": businesses,
        "is_superadmin": is_admin
    })


async def api_report_data(request: web.Request) -> web.Response:
    biz = get_req_business(request)
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
    summary = db.summary(business_id=biz["id"], period_type=period_type, period_value=period_val, rows=rows)

    return web.json_response({
        "business": biz,
        "currency": biz.get("currency") or config.CURRENCY,
        "period_type": period_type,
        "period_value": period_val,
        "summary": summary,
        "payments": [_serialize(p) for p in rows],
        "recent_periods": {
            "days": db.recent_days(biz["id"]),
            "months": db.recent_months(biz["id"]),
            "years": db.recent_years(biz["id"]),
        }
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
        web.get("/api/businesses", api_businesses),
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
