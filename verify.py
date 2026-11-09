"""Runs the checks for a saved payment and stores the results (used by the bot and the dashboard)."""
from datetime import datetime

import banks
import config
import db


def receipt_from_payment(p: dict) -> banks.Receipt:
    r = banks.from_url(p["receipt_url"]) if p.get("receipt_url") else None
    base = banks.Receipt(
        bank=p.get("bank"), ref=p.get("tx_ref"), amount=p.get("amount"), payer=p.get("payer"),
        receiver=p.get("receiver"), receiver_account=p.get("receiver_account"),
        time=datetime.fromisoformat(p["receipt_time"]) if p.get("receipt_time") else None,
        url=p.get("receipt_url"),
    )
    return base.merge(r)


def save_receipt_fields(pid: int, r: banks.Receipt, flags: list[str]) -> None:
    db.update_payment(
        pid, bank=r.bank, receipt_url=r.url, payer=r.payer, receiver=r.receiver,
        receiver_account=r.receiver_account, receipt_time=r.time.isoformat() if r.time else None,
        flags="; ".join(flags) or None,
    )


async def check_payment(pid: int) -> banks.BankCheck | None:
    """Looks the payment up at the bank, stores the result and auto-verifies when everything matches."""
    p = db.get_payment(pid)
    if not p:
        return None
    r = receipt_from_payment(p)

    biz = db.get_business(p.get("business_id")) if p.get("business_id") else None
    accounts = [a.strip() for a in (biz["accounts"] or "").split(",") if a.strip()] if biz else None
    names = [n.strip() for n in (biz["names"] or "").split(",") if n.strip()] if biz else None

    result = await banks.check_online(r, p["amount"], accounts=accounts, names=names)
    changes = {"bank_check": result.status, "bank_check_msg": result.message,
               "checked_at": config.now_utc().isoformat()}
    found = result.receipt
    if found:
        # The bank's own data is better than what we read from the screenshot.
        r_bank = banks.Receipt(bank=found.bank, ref=found.ref, payer=found.payer, receiver=found.receiver,
                               receiver_account=found.receiver_account, time=found.time, url=found.url)
        merged = r_bank.merge(r)
        changes.update(bank=merged.bank, payer=merged.payer, receiver=merged.receiver,
                       receiver_account=merged.receiver_account, receipt_url=merged.url,
                       receipt_time=merged.time.isoformat() if merged.time else None)
        if p["amount"] is None and found.amount is not None:
            changes["amount"] = found.amount
        if not p["tx_ref"] and found.ref:
            changes["tx_ref"] = found.ref
            dup, same_biz = db.find_duplicate(found.ref, pid, p.get("business_id"))
            if dup:
                changes["duplicate_of"] = dup["id"]
                p["duplicate_of"] = dup["id"]
                if not same_biz:
                    existing_f = (p.get("flags") or "")
                    changes["flags"] = (existing_f + "; " if existing_f else "") + f"Reused receipt: already redeemed at another business (#{dup['id']})"
    if (result.status == "ok" and config.AUTO_VERIFY and p["status"] == "pending"
            and not p["duplicate_of"] and not p.get("flags")):
        changes["status"] = "verified"
        changes["reviewed_at"] = config.now_utc().isoformat()
    db.update_payment(pid, **changes)
    return result
