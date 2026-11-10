"""SQLite storage for businesses, waiters, submitted payments and multi-period reconciliation."""
import re
import sqlite3
import threading

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    code        TEXT UNIQUE NOT NULL,
    accounts    TEXT,
    names       TEXT,
    currency    TEXT NOT NULL DEFAULT 'ETB',
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS business_owners (
    business_id INTEGER NOT NULL,
    tg_id       INTEGER NOT NULL,
    role        TEXT NOT NULL DEFAULT 'owner',
    joined_at   TEXT NOT NULL,
    PRIMARY KEY (business_id, tg_id)
);
CREATE TABLE IF NOT EXISTS business_waiters (
    business_id  INTEGER NOT NULL,
    tg_id        INTEGER NOT NULL,
    counter_name TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    joined_at    TEXT NOT NULL,
    PRIMARY KEY (business_id, tg_id)
);
CREATE TABLE IF NOT EXISTS users (
    tg_id               INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    username            TEXT,
    role                TEXT NOT NULL DEFAULT 'waiter',
    active              INTEGER NOT NULL DEFAULT 1,
    active_business_id  INTEGER,
    is_superadmin       INTEGER NOT NULL DEFAULT 0,
    joined_at           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id   INTEGER,
    waiter_id     INTEGER NOT NULL,
    day           TEXT NOT NULL,
    month         TEXT,
    year          TEXT,
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
CREATE TABLE IF NOT EXISTS days (
    business_id INTEGER NOT NULL DEFAULT 1,
    day         TEXT NOT NULL,
    bank_total  REAL,
    updated_at  TEXT,
    PRIMARY KEY (business_id, day)
);
"""
INDEXES = """
CREATE INDEX IF NOT EXISTS ix_payments_biz_day ON payments(business_id, day);
CREATE INDEX IF NOT EXISTS ix_payments_biz_month ON payments(business_id, month);
CREATE INDEX IF NOT EXISTS ix_payments_biz_year ON payments(business_id, year);
CREATE INDEX IF NOT EXISTS ix_payments_biz_ref ON payments(business_id, tx_ref);
CREATE INDEX IF NOT EXISTS ix_payments_ref ON payments(tx_ref);
CREATE INDEX IF NOT EXISTS ix_biz_code ON businesses(code);
CREATE INDEX IF NOT EXISTS ix_biz_waiters_user ON business_waiters(tg_id);
CREATE INDEX IF NOT EXISTS ix_biz_owners_user ON business_owners(tg_id);
"""


NEW_PAYMENT_COLUMNS = {
    "bank": "TEXT", "receipt_url": "TEXT", "payer": "TEXT", "receiver": "TEXT", "receiver_account": "TEXT",
    "receipt_time": "TEXT", "flags": "TEXT", "bank_check": "TEXT", "bank_check_msg": "TEXT", "checked_at": "TEXT",
    "business_id": "INTEGER", "month": "TEXT", "year": "TEXT",
}
NEW_USER_COLUMNS = {
    "active_business_id": "INTEGER", "is_superadmin": "INTEGER NOT NULL DEFAULT 0",
}
PAYMENT_COLUMNS = {
    "waiter_id", "day", "month", "year", "business_id", "created_at", "kind", "photo_path", "qr_data", "ocr_text", "raw_text",
    "tx_ref", "amount", "status", "duplicate_of", "note", "chat_id", "reply_msg_id", "reviewed_at",
} | set(NEW_PAYMENT_COLUMNS)

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def init() -> None:
    global _conn
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.executescript(SCHEMA)

    # Migrate columns if existing database
    existing_p = {r[1] for r in _conn.execute("PRAGMA table_info(payments)")}
    for col, typ in NEW_PAYMENT_COLUMNS.items():
        if col not in existing_p:
            _conn.execute(f"ALTER TABLE payments ADD COLUMN {col} {typ}")

    existing_u = {r[1] for r in _conn.execute("PRAGMA table_info(users)")}
    for col, typ in NEW_USER_COLUMNS.items():
        if col not in existing_u:
            _conn.execute(f"ALTER TABLE users ADD COLUMN {col} {typ}")

    existing_d = {r[1] for r in _conn.execute("PRAGMA table_info(days)")}
    if existing_d and "business_id" not in existing_d:
        _conn.executescript("""
            CREATE TABLE IF NOT EXISTS days_new (
                business_id INTEGER NOT NULL DEFAULT 1,
                day         TEXT NOT NULL,
                bank_total  REAL,
                updated_at  TEXT,
                PRIMARY KEY (business_id, day)
            );
            INSERT OR IGNORE INTO days_new (business_id, day, bank_total, updated_at)
                SELECT 1, day, bank_total, updated_at FROM days;
            DROP TABLE days;
            ALTER TABLE days_new RENAME TO days;
        """)

    _conn.commit()
    _conn.executescript(INDEXES)
    _conn.commit()
    ensure_default_business()


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


# ---------------------------------------------------------------- superadmin & businesses
def is_superadmin(tg_id: int) -> bool:
    if tg_id in config.OWNER_IDS:
        return True
    u = get_user(tg_id)
    return bool(u and u.get("is_superadmin"))


def create_business(name: str, code: str | None = None, accounts: str = "", names: str = "",
                    currency: str = "ETB") -> int:
    name = name.strip()
    if not code:
        code = re.sub(r"[^a-z0-9]", "", name.lower())[:16] or "biz"
    code = re.sub(r"[^a-zA-Z0-9_-]", "", code).lower()
    # Ensure code is unique
    base_code = code
    idx = 1
    while _one("SELECT 1 FROM businesses WHERE lower(code)=?", (code,)):
        code = f"{base_code}{idx}"
        idx += 1
    return _exec(
        "INSERT INTO businesses (name, code, accounts, names, currency, active, created_at) VALUES (?,?,?,?,?,1,?)",
        (name, code, accounts.strip(), names.strip(), currency.strip(), config.now_utc().isoformat()),
    )


def get_business(business_id: int | None) -> dict | None:
    if business_id is None:
        return None
    return _one("SELECT * FROM businesses WHERE id=?", (business_id,))


def get_business_by_code(code: str) -> dict | None:
    return _one("SELECT * FROM businesses WHERE lower(code)=?", (code.strip().lower(),))


def list_businesses() -> list[dict]:
    return _query("SELECT * FROM businesses WHERE active=1 ORDER BY id")


def list_all_businesses() -> list[dict]:
    return _query("SELECT * FROM businesses ORDER BY id")


def update_business(business_id: int, **fields) -> None:
    allowed = {"name", "code", "accounts", "names", "currency", "active"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    _exec(f"UPDATE businesses SET {sets} WHERE id=?", (*fields.values(), business_id))


def add_business_owner(business_id: int, tg_id: int, role: str = "owner") -> None:
    _exec("INSERT OR REPLACE INTO business_owners (business_id, tg_id, role, joined_at) VALUES (?,?,?,?)",
          (business_id, tg_id, role, config.now_utc().isoformat()))
    # Set as active business if not set
    u = get_user(tg_id)
    if u and not u.get("active_business_id"):
        set_user_active_business(tg_id, business_id)


def remove_business_owner(business_id: int, tg_id: int) -> None:
    _exec("DELETE FROM business_owners WHERE business_id=? AND tg_id=?", (business_id, tg_id))


def get_business_owners(business_id: int) -> list[dict]:
    return _query("""SELECT bo.*, COALESCE(u.name, CAST(bo.tg_id AS TEXT)) as name, u.username
                     FROM business_owners bo LEFT JOIN users u ON u.tg_id = bo.tg_id
                     WHERE bo.business_id=?""", (business_id,))


def is_business_owner_strict(business_id: int, tg_id: int) -> bool:
    """Strictly checks if user is an assigned owner of this business."""
    return bool(_one("SELECT 1 FROM business_owners WHERE business_id=? AND tg_id=?", (business_id, tg_id)))


def is_business_owner(business_id: int, tg_id: int) -> bool:
    if is_superadmin(tg_id):
        return True
    return is_business_owner_strict(business_id, tg_id)


def set_business_owner(business_id: int, tg_id: int) -> None:
    """Replaces current owners with the specified tg_id."""
    _exec("DELETE FROM business_owners WHERE business_id=?", (business_id,))
    add_business_owner(business_id, tg_id)


def delete_business(business_id: int) -> None:
    """Deactivates a business."""
    _exec("UPDATE businesses SET active=0 WHERE id=?", (business_id,))


def list_all_businesses_with_details() -> list[dict]:
    """Returns all businesses enriched with owners, cashiers count, and transaction stats."""
    businesses = _query("SELECT * FROM businesses ORDER BY id DESC")
    for b in businesses:
        b["owners"] = get_business_owners(b["id"])
        w_cnt = _one("SELECT count(*) as c FROM business_waiters WHERE business_id=? AND active=1", (b["id"],))
        b["waiters_count"] = w_cnt["c"] if w_cnt else 0
        tx_stat = _one("SELECT count(*) as cnt, coalesce(sum(amount), 0) as tot FROM payments WHERE business_id=?", (b["id"],))
        b["payments_count"] = tx_stat["cnt"] if tx_stat else 0
        b["payments_total"] = tx_stat["tot"] if tx_stat else 0.0
    return businesses


def get_platform_stats() -> dict:
    """Aggregates platform-wide stats for the bot owner."""
    total_biz = _one("SELECT count(*) as c FROM businesses")["c"]
    active_biz = _one("SELECT count(*) as c FROM businesses WHERE active=1")["c"]
    total_waiters = _one("SELECT count(DISTINCT tg_id) as c FROM business_waiters WHERE active=1")["c"]
    total_tx = _one("SELECT count(*) as c, coalesce(sum(amount), 0) as s FROM payments WHERE status='verified'")
    return {
        "total_businesses": total_biz,
        "active_businesses": active_biz,
        "total_cashiers": total_waiters,
        "total_verified_tx": total_tx["c"] if total_tx else 0,
        "total_verified_amount": total_tx["s"] if total_tx else 0.0
    }


def get_user_owned_businesses(tg_id: int) -> list[dict]:
    if is_superadmin(tg_id):
        return list_businesses()
    return _query("""SELECT b.* FROM businesses b
                     JOIN business_owners bo ON bo.business_id = b.id
                     WHERE bo.tg_id=? AND b.active=1 ORDER BY b.name""", (tg_id,))


# ---------------------------------------------------------------- waiters per business
def add_waiter_to_business(business_id: int, tg_id: int, counter_name: str | None = None, active: int = 1) -> None:
    _exec("""INSERT INTO business_waiters (business_id, tg_id, counter_name, active, joined_at)
             VALUES (?,?,?,?,?)
             ON CONFLICT(business_id, tg_id) DO UPDATE SET active=excluded.active,
             counter_name=COALESCE(excluded.counter_name, business_waiters.counter_name)""",
          (business_id, tg_id, counter_name, active, config.now_utc().isoformat()))
    if active:
        set_user_active_business(tg_id, business_id)


def is_waiter_active_in_business(business_id: int, tg_id: int) -> bool:
    row = _one("SELECT active FROM business_waiters WHERE business_id=? AND tg_id=?", (business_id, tg_id))
    return bool(row and row["active"] == 1)


def get_pending_waiters_for_business(business_id: int) -> list[dict]:
    return _query("""SELECT bw.*, COALESCE(u.name, CAST(bw.tg_id AS TEXT)) AS user_name, u.username
                     FROM business_waiters bw
                     LEFT JOIN users u ON u.tg_id = bw.tg_id
                     WHERE bw.business_id=? AND bw.active=0 ORDER BY bw.joined_at DESC""", (business_id,))


def set_user_active_business(tg_id: int, business_id: int) -> None:
    _exec("UPDATE users SET active_business_id=? WHERE tg_id=?", (business_id, tg_id))


def get_waiter_businesses(tg_id: int) -> list[dict]:
    return _query("""SELECT b.*, bw.counter_name, bw.active as waiter_active
                     FROM business_waiters bw
                     JOIN businesses b ON b.id = bw.business_id
                     WHERE bw.tg_id=? AND bw.active=1 AND b.active=1
                     ORDER BY b.name""", (tg_id,))


def get_waiters_for_business(business_id: int) -> list[dict]:
    return _query("""SELECT bw.*, COALESCE(u.name, CAST(bw.tg_id AS TEXT)) AS user_name, u.username
                     FROM business_waiters bw
                     LEFT JOIN users u ON u.tg_id = bw.tg_id
                     WHERE bw.business_id=? ORDER BY bw.active DESC, bw.counter_name, u.name""", (business_id,))


def set_business_waiter_active(business_id: int, tg_id: int, active: bool) -> bool:
    _exec("UPDATE business_waiters SET active=? WHERE business_id=? AND tg_id=?", (1 if active else 0, business_id, tg_id))
    return True


def remove_waiter_from_business(business_id: int, tg_id: int) -> bool:
    _exec("DELETE FROM business_waiters WHERE business_id=? AND tg_id=?", (business_id, tg_id))
    return True


# ---------------------------------------------------------------- default business migration
def ensure_default_business() -> None:
    row = _one("SELECT id FROM businesses LIMIT 1")
    if not row:
        accounts = ",".join(config.MY_ACCOUNTS)
        names = ",".join(config.MY_NAMES)
        bid = _exec(
            "INSERT INTO businesses (name, code, accounts, names, currency, active, created_at) VALUES (?,?,?,?,?,1,?)",
            ("Main Store", config.JOIN_CODE or "shop2026", accounts, names, config.CURRENCY, config.now_utc().isoformat()),
        )
        for oid in config.OWNER_IDS:
            _exec("INSERT OR IGNORE INTO business_owners (business_id, tg_id, role, joined_at) VALUES (?,?,?,?)",
                  (bid, oid, "owner", config.now_utc().isoformat()))
            _exec("UPDATE users SET is_superadmin=1, active_business_id=? WHERE tg_id=?", (bid, oid))

        _exec("UPDATE payments SET business_id=? WHERE business_id IS NULL", (bid,))
        for p in _query("SELECT id, day FROM payments WHERE month IS NULL OR year IS NULL"):
            day = p["day"]
            _exec("UPDATE payments SET month=?, year=? WHERE id=?", (day[:7], day[:4], p["id"]))

        for w in _query("SELECT tg_id, name FROM users WHERE role='waiter'"):
            _exec("INSERT OR IGNORE INTO business_waiters (business_id, tg_id, counter_name, active, joined_at) VALUES (?,?,?,1,?)",
                  (bid, w["tg_id"], w["name"], config.now_utc().isoformat()))
            _exec("UPDATE users SET active_business_id=? WHERE tg_id=? AND active_business_id IS NULL", (bid, w["tg_id"]))


# ---------------------------------------------------------------- users
def upsert_user(tg_id: int, name: str, username: str | None, role: str) -> None:
    is_admin = 1 if tg_id in config.OWNER_IDS else 0
    _exec(
        """INSERT INTO users (tg_id, name, username, role, active, is_superadmin, joined_at) VALUES (?,?,?,?,1,?,?)
           ON CONFLICT(tg_id) DO UPDATE SET name=excluded.name, username=excluded.username,
                                            role=excluded.role, active=1""",
        (tg_id, name, username, role, is_admin, config.now_utc().isoformat()),
    )


def get_user(tg_id: int) -> dict | None:
    return _one("SELECT * FROM users WHERE tg_id=?", (tg_id,))


def get_user_by_username(username: str) -> dict | None:
    uname = username.lstrip("@").strip().lower()
    return _one("SELECT * FROM users WHERE lower(username)=?", (uname,))


def set_user_active(tg_id: int, active: bool) -> bool:
    _exec("UPDATE users SET active=? WHERE tg_id=?", (1 if active else 0, tg_id))
    return get_user(tg_id) is not None


# ---------------------------------------------------------------- payments
_PAYMENT_SELECT = """SELECT p.*, COALESCE(bw.counter_name, u.name, CAST(p.waiter_id AS TEXT)) AS waiter_name,
                            b.name AS business_name, b.currency AS business_currency
                     FROM payments p
                     LEFT JOIN users u ON u.tg_id = p.waiter_id
                     LEFT JOIN businesses b ON b.id = p.business_id
                     LEFT JOIN business_waiters bw ON bw.business_id = p.business_id AND bw.tg_id = p.waiter_id"""


def add_payment(**fields) -> int:
    fields = {k: v for k, v in fields.items() if k in PAYMENT_COLUMNS}
    fields.setdefault("created_at", config.now_utc().isoformat())
    day = fields.setdefault("day", config.business_day())
    fields.setdefault("month", day[:7])
    fields.setdefault("year", day[:4])
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


def last_payment(waiter_id: int, business_id: int | None = None) -> dict | None:
    if business_id is not None:
        return _one(f"{_PAYMENT_SELECT} WHERE p.waiter_id=? AND p.business_id=? ORDER BY p.id DESC LIMIT 1",
                    (waiter_id, business_id))
    return _one(f"{_PAYMENT_SELECT} WHERE p.waiter_id=? ORDER BY p.id DESC LIMIT 1", (waiter_id,))


def delete_payment(pid: int) -> None:
    _exec("DELETE FROM payments WHERE id=?", (pid,))
    _exec("UPDATE payments SET duplicate_of=NULL WHERE duplicate_of=?", (pid,))


def find_duplicate(tx_ref: str, exclude_id: int, business_id: int | None = None) -> tuple[dict | None, bool]:
    """Finds existing payment with tx_ref. Returns (dup_payment, is_same_business)."""
    if not tx_ref:
        return None, False
    row = _one(
        f"{_PAYMENT_SELECT} WHERE p.tx_ref=? AND p.id<>? AND p.status<>'rejected' ORDER BY p.id LIMIT 1",
        (tx_ref, exclude_id),
    )
    if not row:
        return None, False
    same_biz = (business_id is None or row.get("business_id") == business_id)
    return row, same_biz


def find_duplicate_url(url: str, exclude_id: int, business_id: int | None = None) -> tuple[dict | None, bool]:
    """Same official receipt link submitted before."""
    if not url:
        return None, False
    row = _one(
        f"{_PAYMENT_SELECT} WHERE p.receipt_url=? AND p.id<>? AND p.status<>'rejected' ORDER BY p.id LIMIT 1",
        (url, exclude_id),
    )
    if not row:
        return None, False
    same_biz = (business_id is None or row.get("business_id") == business_id)
    return row, same_biz


def payments_for_period(business_id: int | None, period_type: str = "day", period_value: str = "",
                        waiter_id: int | None = None) -> list[dict]:
    where = []
    params = []
    if business_id is not None:
        where.append("p.business_id = ?")
        params.append(business_id)
    if period_type == "day":
        where.append("p.day = ?")
        params.append(period_value or config.business_day())
    elif period_type == "month":
        where.append("p.month = ?")
        params.append(period_value)
    elif period_type == "year":
        where.append("p.year = ?")
        params.append(period_value)
    if waiter_id is not None:
        where.append("p.waiter_id = ?")
        params.append(waiter_id)

    w_str = ("WHERE " + " AND ".join(where)) if where else ""
    return _query(f"{_PAYMENT_SELECT} {w_str} ORDER BY p.id", tuple(params))


def payments_for_day(day: str, waiter_id: int | None = None, business_id: int | None = None) -> list[dict]:
    return payments_for_period(business_id, "day", day, waiter_id)


def verify_all_pending(day: str, business_id: int | None = None) -> int:
    with _lock:
        where = ["day=?", "status='pending'", "duplicate_of IS NULL", "amount IS NOT NULL",
                 "flags IS NULL", "COALESCE(bank_check, '') NOT IN ('mismatch', 'not_found')"]
        params = [config.now_utc().isoformat(), day]
        if business_id is not None:
            where.append("business_id=?")
            params.append(business_id)
        w_str = " AND ".join(where)
        cur = _conn.execute(f"UPDATE payments SET status='verified', reviewed_at=? WHERE {w_str}", tuple(params))
        _conn.commit()
        return cur.rowcount


def recent_days(business_id: int | None = None, limit: int = 90) -> list[dict]:
    if business_id is not None:
        return _query("SELECT day, COUNT(*) AS count FROM payments WHERE business_id=? GROUP BY day ORDER BY day DESC LIMIT ?",
                      (business_id, limit))
    return _query("SELECT day, COUNT(*) AS count FROM payments GROUP BY day ORDER BY day DESC LIMIT ?", (limit,))


def recent_months(business_id: int | None = None, limit: int = 24) -> list[dict]:
    if business_id is not None:
        return _query("SELECT month, COUNT(*) AS count FROM payments WHERE business_id=? GROUP BY month ORDER BY month DESC LIMIT ?",
                      (business_id, limit))
    return _query("SELECT month, COUNT(*) AS count FROM payments GROUP BY month ORDER BY month DESC LIMIT ?", (limit,))


def recent_years(business_id: int | None = None, limit: int = 10) -> list[dict]:
    if business_id is not None:
        return _query("SELECT year, COUNT(*) AS count FROM payments WHERE business_id=? GROUP BY year ORDER BY year DESC LIMIT ?",
                      (business_id, limit))
    return _query("SELECT year, COUNT(*) AS count FROM payments GROUP BY year ORDER BY year DESC LIMIT ?", (limit,))


# ---------------------------------------------------------------- reconciliation
def set_bank_total(business_id: int, day: str, total: float | None) -> None:
    _exec(
        """INSERT INTO days (business_id, day, bank_total, updated_at) VALUES (?,?,?,?)
           ON CONFLICT(business_id, day) DO UPDATE SET bank_total=excluded.bank_total, updated_at=excluded.updated_at""",
        (business_id, day, total, config.now_utc().isoformat()),
    )


def get_bank_total(business_id: int, day: str) -> float | None:
    row = _one("SELECT bank_total FROM days WHERE business_id=? AND day=?", (business_id, day))
    return row["bank_total"] if row else None


def has_problem(p: dict) -> bool:
    return p["status"] == "pending" and (bool(p.get("flags")) or p.get("bank_check") in ("mismatch", "not_found"))


def is_excluded(p: dict) -> bool:
    """Rejected entries and unresolved duplicates don't count toward claimed sales."""
    return p["status"] == "rejected" or (p["duplicate_of"] is not None and p["status"] == "pending")


def summary(day: str | None = None, rows: list[dict] | None = None, business_id: int | None = None,
            period_type: str = "day", period_value: str = "") -> dict:
    if rows is None:
        val = period_value or day or config.business_day()
        rows = payments_for_period(business_id, period_type, val)
    else:
        val = period_value or day or config.business_day()

    biz = get_business(business_id) if business_id else None
    biz_name = biz["name"] if biz else "All Stores"
    currency = biz["currency"] if biz else config.CURRENCY

    def agg(pred):
        sel = [r for r in rows if pred(r)]
        return {"count": len(sel), "total": round(sum(r["amount"] or 0 for r in sel), 2)}

    counted = [r for r in rows if not is_excluded(r)]
    by_waiter: dict[int, dict] = {}
    by_bank: dict[str, dict] = {}
    by_payer: dict[str, dict] = {}

    for r in counted:
        w = by_waiter.setdefault(r["waiter_id"], {"waiter_id": r["waiter_id"], "name": r["waiter_name"],
                                                  "count": 0, "total": 0.0, "verified": 0.0})
        w["count"] += 1
        w["total"] = round(w["total"] + (r["amount"] or 0), 2)
        if r["status"] == "verified":
            w["verified"] = round(w["verified"] + (r["amount"] or 0), 2)

        b = r.get("bank") or "Other"
        bk = by_bank.setdefault(b, {"bank": b, "count": 0, "total": 0.0, "verified": 0.0})
        bk["count"] += 1
        bk["total"] = round(bk["total"] + (r["amount"] or 0), 2)
        if r["status"] == "verified":
            bk["verified"] = round(bk["verified"] + (r["amount"] or 0), 2)

        p_name = (r.get("payer") or "").strip()
        if p_name:
            py = by_payer.setdefault(p_name, {"name": p_name, "count": 0, "total": 0.0})
            py["count"] += 1
            py["total"] = round(py["total"] + (r["amount"] or 0), 2)

    claimed = agg(lambda r: not is_excluded(r))
    bank_total = get_bank_total(business_id, val) if (business_id and period_type == "day") else None

    return {
        "business_id": business_id,
        "business_name": biz_name,
        "currency": currency,
        "period_type": period_type,
        "period_value": val,
        "day": val,  # backwards compatibility
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
        "by_customer": sorted(by_payer.values(), key=lambda p: -p["total"])[:20],
    }
