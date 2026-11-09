"""SQLite storage for waiters, submitted payments and daily reconciliation."""
import sqlite3
import threading

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id      INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    username   TEXT,
    role       TEXT NOT NULL DEFAULT 'waiter',
    active     INTEGER NOT NULL DEFAULT 1,
    joined_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    waiter_id     INTEGER NOT NULL,
    day           TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    kind          TEXT NOT NULL,              -- photo | text
    photo_path    TEXT,
    qr_data       TEXT,
    ocr_text      TEXT,
    raw_text      TEXT,
    tx_ref        TEXT,
    amount        REAL,
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending | verified | rejected
    duplicate_of  INTEGER,
    note          TEXT,
    chat_id       INTEGER,
    reply_msg_id  INTEGER,
    reviewed_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_payments_day ON payments(day);
CREATE INDEX IF NOT EXISTS ix_payments_ref ON payments(tx_ref);
CREATE INDEX IF NOT EXISTS ix_payments_reply ON payments(chat_id, reply_msg_id);
CREATE TABLE IF NOT EXISTS days (
    day         TEXT PRIMARY KEY,
    bank_total  REAL,
    updated_at  TEXT
);
"""

NEW_COLUMNS = {  # added later; created automatically on existing databases
    "bank": "TEXT", "receipt_url": "TEXT", "payer": "TEXT", "receiver": "TEXT", "receiver_account": "TEXT",
    "receipt_time": "TEXT", "flags": "TEXT", "bank_check": "TEXT", "bank_check_msg": "TEXT", "checked_at": "TEXT",
}
PAYMENT_COLUMNS = {
    "waiter_id", "day", "created_at", "kind", "photo_path", "qr_data", "ocr_text", "raw_text",
    "tx_ref", "amount", "status", "duplicate_of", "note", "chat_id", "reply_msg_id", "reviewed_at",
} | set(NEW_COLUMNS)

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def init() -> None:
    global _conn
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.executescript(SCHEMA)
    existing = {r[1] for r in _conn.execute("PRAGMA table_info(payments)")}
    for col, typ in NEW_COLUMNS.items():
        if col not in existing:
            _conn.execute(f"ALTER TABLE payments ADD COLUMN {col} {typ}")
    _conn.commit()


def _query(sql: str, params=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, params).fetchall()]


def _one(sql: str, params=()) -> dict | None:
    rows = _query(sql, params)
    return rows[0] if rows else None


def _exec(sql: str, params=()) -> int:
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur.lastrowid


# ---------------------------------------------------------------- users
def upsert_user(tg_id: int, name: str, username: str | None, role: str) -> None:
    _exec(
        """INSERT INTO users (tg_id, name, username, role, active, joined_at) VALUES (?,?,?,?,1,?)
           ON CONFLICT(tg_id) DO UPDATE SET name=excluded.name, username=excluded.username,
                                            role=excluded.role, active=1""",
        (tg_id, name, username, role, config.now_utc().isoformat()),
    )


def get_user(tg_id: int) -> dict | None:
    return _one("SELECT * FROM users WHERE tg_id=?", (tg_id,))


def set_user_active(tg_id: int, active: bool) -> bool:
    _exec("UPDATE users SET active=? WHERE tg_id=?", (1 if active else 0, tg_id))
    return get_user(tg_id) is not None


def list_waiters() -> list[dict]:
    return _query("SELECT * FROM users WHERE role='waiter' ORDER BY active DESC, name")


# ---------------------------------------------------------------- payments
_PAYMENT_SELECT = """SELECT p.*, COALESCE(u.name, CAST(p.waiter_id AS TEXT)) AS waiter_name
                     FROM payments p LEFT JOIN users u ON u.tg_id = p.waiter_id"""


def add_payment(**fields) -> int:
    fields = {k: v for k, v in fields.items() if k in PAYMENT_COLUMNS}
    fields.setdefault("created_at", config.now_utc().isoformat())
    fields.setdefault("day", config.business_day())
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    return _exec(f"INSERT INTO payments ({cols}) VALUES ({marks})", tuple(fields.values()))


def update_payment(pid: int, **fields) -> None:
    fields = {k: v for k, v in fields.items() if k in PAYMENT_COLUMNS}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    _exec(f"UPDATE payments SET {sets} WHERE id=?", (*fields.values(), pid))


def get_payment(pid: int) -> dict | None:
    return _one(f"{_PAYMENT_SELECT} WHERE p.id=?", (pid,))


def payment_by_reply(chat_id: int, msg_id: int) -> dict | None:
    return _one(f"{_PAYMENT_SELECT} WHERE p.chat_id=? AND p.reply_msg_id=?", (chat_id, msg_id))


def last_payment(waiter_id: int) -> dict | None:
    return _one(f"{_PAYMENT_SELECT} WHERE p.waiter_id=? ORDER BY p.id DESC LIMIT 1", (waiter_id,))


def delete_payment(pid: int) -> None:
    _exec("DELETE FROM payments WHERE id=?", (pid,))
    _exec("UPDATE payments SET duplicate_of=NULL WHERE duplicate_of=?", (pid,))


def find_duplicate(tx_ref: str, exclude_id: int) -> dict | None:
    """Earliest other non-rejected submission with the same transaction reference (any day)."""
    return _one(
        f"{_PAYMENT_SELECT} WHERE p.tx_ref=? AND p.id<>? AND p.status<>'rejected' ORDER BY p.id LIMIT 1",
        (tx_ref, exclude_id),
    )


def find_duplicate_url(url: str, exclude_id: int) -> dict | None:
    """Same official receipt link (from the QR code) submitted before."""
    return _one(
        f"{_PAYMENT_SELECT} WHERE p.receipt_url=? AND p.id<>? AND p.status<>'rejected' ORDER BY p.id LIMIT 1",
        (url, exclude_id),
    )


def payments_for_day(day: str, waiter_id: int | None = None) -> list[dict]:
    if waiter_id is None:
        return _query(f"{_PAYMENT_SELECT} WHERE p.day=? ORDER BY p.id", (day,))
    return _query(f"{_PAYMENT_SELECT} WHERE p.day=? AND p.waiter_id=? ORDER BY p.id", (day, waiter_id))


def verify_all_pending(day: str) -> int:
    with _lock:
        cur = _conn.execute(
            "UPDATE payments SET status='verified', reviewed_at=? "
            "WHERE day=? AND status='pending' AND duplicate_of IS NULL AND amount IS NOT NULL "
            "AND flags IS NULL AND COALESCE(bank_check, '') NOT IN ('mismatch', 'not_found')",
            (config.now_utc().isoformat(), day),
        )
        _conn.commit()
        return cur.rowcount


def recent_days(limit: int = 90) -> list[dict]:
    return _query(
        "SELECT day, COUNT(*) AS count FROM payments GROUP BY day ORDER BY day DESC LIMIT ?", (limit,)
    )


# ---------------------------------------------------------------- reconciliation
def set_bank_total(day: str, total: float | None) -> None:
    _exec(
        """INSERT INTO days (day, bank_total, updated_at) VALUES (?,?,?)
           ON CONFLICT(day) DO UPDATE SET bank_total=excluded.bank_total, updated_at=excluded.updated_at""",
        (day, total, config.now_utc().isoformat()),
    )


def get_bank_total(day: str) -> float | None:
    row = _one("SELECT bank_total FROM days WHERE day=?", (day,))
    return row["bank_total"] if row else None


def has_problem(p: dict) -> bool:
    return p["status"] == "pending" and (bool(p.get("flags")) or p.get("bank_check") in ("mismatch", "not_found"))


def is_excluded(p: dict) -> bool:
    """Rejected entries and unresolved duplicates don't count toward the claimed total."""
    return p["status"] == "rejected" or (p["duplicate_of"] is not None and p["status"] == "pending")


def summary(day: str, rows: list[dict] | None = None) -> dict:
    rows = payments_for_day(day) if rows is None else rows

    def agg(pred):
        sel = [r for r in rows if pred(r)]
        return {"count": len(sel), "total": round(sum(r["amount"] or 0 for r in sel), 2)}

    counted = [r for r in rows if not is_excluded(r)]
    by_waiter: dict[int, dict] = {}
    by_bank: dict[str, dict] = {}
    for r in counted:
        w = by_waiter.setdefault(r["waiter_id"], {"waiter_id": r["waiter_id"], "name": r["waiter_name"],
                                                  "count": 0, "total": 0.0, "verified": 0.0})
        w["count"] += 1
        w["total"] = round(w["total"] + (r["amount"] or 0), 2)
        if r["status"] == "verified":
            w["verified"] = round(w["verified"] + (r["amount"] or 0), 2)

        b = r.get("bank") or "Unknown / Other"
        bk = by_bank.setdefault(b, {"bank": b, "count": 0, "total": 0.0, "verified": 0.0})
        bk["count"] += 1
        bk["total"] = round(bk["total"] + (r["amount"] or 0), 2)
        if r["status"] == "verified":
            bk["verified"] = round(bk["verified"] + (r["amount"] or 0), 2)

    claimed = agg(lambda r: not is_excluded(r))
    bank_total = get_bank_total(day)
    return {
        "day": day,
        "count": len(rows),
        "claimed": claimed,
        "verified": agg(lambda r: r["status"] == "verified"),
        "pending": agg(lambda r: r["status"] == "pending" and r["duplicate_of"] is None),
        "rejected": agg(lambda r: r["status"] == "rejected"),
        "duplicates": agg(lambda r: r["duplicate_of"] is not None and r["status"] == "pending"),
        "missing_amount": sum(1 for r in counted if r["amount"] is None),
        "missing_ref": sum(1 for r in counted if not r["tx_ref"]),
        "problems": agg(has_problem),
        "bank_confirmed": agg(lambda r: r.get("bank_check") == "ok" and r["status"] != "rejected"),
        "bank_total": bank_total,
        "difference": None if bank_total is None else round(bank_total - claimed["total"], 2),
        "by_waiter": sorted(by_waiter.values(), key=lambda w: -w["total"]),
        "by_bank": sorted(by_bank.values(), key=lambda b: -b["total"]),
    }
