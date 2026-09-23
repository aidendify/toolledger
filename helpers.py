"""ToolLedger helpers: DB, custody, QR, CSV, optional SMTP/Twilio notify."""
from __future__ import annotations

import base64
import csv
import io
import os
import secrets
import smtplib
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any

from flask import g

APP_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = str(APP_ROOT / "data" / "toolledger.db")

STATUSES = ("available", "out", "missing")
STATUS_LABELS = {
    "available": "In shop / available",
    "out": "Checked out",
    "missing": "Missing",
}
EVENT_KINDS = (
    "created",
    "checked_out",
    "returned",
    "force_transfer",
    "marked_missing",
    "cleared_missing",
    "updated",
    "label_printed",
)
OPEN_ENDPOINTS = {
    "health",
    "login",
    "static",
    "tool_public",
    "tool_checkout",
    "tool_return",
    "tool_transfer",
}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def database_path() -> str:
    return _env("DATABASE_PATH") or DEFAULT_DB


def owner_password() -> str:
    return _env("OWNER_PASSWORD")


def business_name() -> str:
    return _env("BUSINESS_NAME") or "ToolLedger"


def public_base_url() -> str:
    return _env("PUBLIC_BASE_URL").rstrip("/")


def marketing_url() -> str:
    return _env("MARKETING_URL")


def smtp_configured() -> bool:
    return bool(_env("SMTP_HOST"))


def twilio_configured() -> bool:
    return bool(
        _env("TWILIO_ACCOUNT_SID")
        and _env("TWILIO_AUTH_TOKEN")
        and _env("TWILIO_FROM_NUMBER")
    )


def sms_configured() -> bool:
    return twilio_configured()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def connect_db(path: str | None = None) -> sqlite3.Connection:
    db_path = path or database_path()
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect_db()
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS people (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          role TEXT,
          phone TEXT,
          email TEXT,
          home_location TEXT,
          notes TEXT,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tools (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          asset_tag TEXT,
          category TEXT,
          home_location TEXT,
          token TEXT NOT NULL UNIQUE,
          status TEXT NOT NULL,
          holder_person_id INTEGER REFERENCES people(id),
          checked_out_at TEXT,
          job_ref TEXT,
          notes TEXT,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY,
          tool_id INTEGER NOT NULL REFERENCES tools(id),
          kind TEXT NOT NULL,
          person_id INTEGER REFERENCES people(id),
          actor TEXT NOT NULL,
          job_ref TEXT,
          note TEXT,
          at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_tools_token ON tools(token);
        CREATE INDEX IF NOT EXISTS idx_tools_status ON tools(status);
        CREATE INDEX IF NOT EXISTS idx_events_tool ON events(tool_id);
        """
    )
    db.commit()


def get_setting(db: sqlite3.Connection, key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row and row["value"] is not None:
        return str(row["value"]).strip()
    return default


def set_setting(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute(
        """
        INSERT INTO settings(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    db.commit()


def overdue_hours(db: sqlite3.Connection | None = None) -> float:
    if db is not None:
        stored = get_setting(db, "overdue_hours", "")
        if stored:
            try:
                return float(stored)
            except ValueError:
                pass
    raw = _env("OVERDUE_HOURS") or "24"
    try:
        return float(raw)
    except ValueError:
        return 24.0


def new_token() -> str:
    return secrets.token_urlsafe(16)


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def add_event(
    db: sqlite3.Connection,
    *,
    tool_id: int,
    kind: str,
    actor: str,
    person_id: int | None = None,
    job_ref: str | None = None,
    note: str | None = None,
    at: str | None = None,
) -> None:
    db.execute(
        """
        INSERT INTO events(tool_id, kind, person_id, actor, job_ref, note, at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tool_id,
            kind,
            person_id,
            actor,
            job_ref or None,
            note or None,
            at or utc_now_iso(),
        ),
    )


def get_person(db: sqlite3.Connection, person_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()


def list_people(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM people ORDER BY name COLLATE NOCASE, id").fetchall()


def get_tool(db: sqlite3.Connection, tool_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM tools WHERE id = ?", (tool_id,)).fetchone()


def get_tool_by_token(db: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM tools WHERE token = ?", (token,)).fetchone()


def list_tools(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT t.*, p.name AS holder_name
        FROM tools t
        LEFT JOIN people p ON p.id = t.holder_person_id
        ORDER BY t.name COLLATE NOCASE, t.id
        """
    ).fetchall()


def tool_public_url(token: str) -> str:
    base = public_base_url()
    if not base:
        return f"/t/{token}"
    return f"{base}/t/{token}"


def create_tool(
    db: sqlite3.Connection,
    *,
    name: str,
    asset_tag: str = "",
    category: str = "",
    home_location: str = "",
    notes: str = "",
    actor: str = "owner",
) -> int:
    token = new_token()
    now = utc_now_iso()
    cur = db.execute(
        """
        INSERT INTO tools(
          name, asset_tag, category, home_location, token, status,
          holder_person_id, checked_out_at, job_ref, notes, created_at
        ) VALUES (?, ?, ?, ?, ?, 'available', NULL, NULL, NULL, ?, ?)
        """,
        (
            name.strip(),
            asset_tag.strip() or None,
            category.strip() or None,
            home_location.strip() or None,
            token,
            notes.strip() or None,
            now,
        ),
    )
    tool_id = int(cur.lastrowid)
    add_event(db, tool_id=tool_id, kind="created", actor=actor)
    db.commit()
    return tool_id


def update_tool(
    db: sqlite3.Connection,
    tool_id: int,
    *,
    name: str,
    asset_tag: str = "",
    category: str = "",
    home_location: str = "",
    notes: str = "",
) -> None:
    db.execute(
        """
        UPDATE tools
        SET name = ?, asset_tag = ?, category = ?, home_location = ?, notes = ?
        WHERE id = ?
        """,
        (
            name.strip(),
            asset_tag.strip() or None,
            category.strip() or None,
            home_location.strip() or None,
            notes.strip() or None,
            tool_id,
        ),
    )
    add_event(db, tool_id=tool_id, kind="updated", actor="owner")
    db.commit()


def create_person(
    db: sqlite3.Connection,
    *,
    name: str,
    role: str = "",
    phone: str = "",
    email: str = "",
    home_location: str = "",
    notes: str = "",
) -> int:
    cur = db.execute(
        """
        INSERT INTO people(name, role, phone, email, home_location, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name.strip(),
            role.strip() or None,
            phone.strip() or None,
            email.strip() or None,
            home_location.strip() or None,
            notes.strip() or None,
            utc_now_iso(),
        ),
    )
    db.commit()
    return int(cur.lastrowid)


def update_person(
    db: sqlite3.Connection,
    person_id: int,
    *,
    name: str,
    role: str = "",
    phone: str = "",
    email: str = "",
    home_location: str = "",
    notes: str = "",
) -> None:
    db.execute(
        """
        UPDATE people
        SET name = ?, role = ?, phone = ?, email = ?, home_location = ?, notes = ?
        WHERE id = ?
        """,
        (
            name.strip(),
            role.strip() or None,
            phone.strip() or None,
            email.strip() or None,
            home_location.strip() or None,
            notes.strip() or None,
            person_id,
        ),
    )
    db.commit()


def _normalize_header(name: str) -> str:
    return "".join(ch for ch in name.strip().lower() if ch.isalnum() or ch == "_")


CSV_ALIASES = {
    "name": {"name", "tool", "tool_name", "toolname"},
    "asset_tag": {"asset_tag", "assettag", "asset", "serial", "serial_number", "tag"},
    "category": {"category", "cat", "type"},
    "home_location": {"home_location", "homelocation", "location", "home", "van"},
    "notes": {"notes", "note", "comment", "comments"},
}


def map_csv_headers(fieldnames: list[str] | None) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not fieldnames:
        return mapping
    for raw in fieldnames:
        norm = _normalize_header(raw)
        for canonical, aliases in CSV_ALIASES.items():
            if norm in aliases and canonical not in mapping:
                mapping[canonical] = raw
    return mapping


def import_tools_csv(db: sqlite3.Connection, text: str, actor: str = "owner") -> int:
    reader = csv.DictReader(io.StringIO(text))
    mapping = map_csv_headers(reader.fieldnames)
    if "name" not in mapping:
        raise ValueError("CSV must include a name column.")
    count = 0
    for row in reader:
        name = (row.get(mapping["name"]) or "").strip()
        if not name:
            continue
        create_tool(
            db,
            name=name,
            asset_tag=(row.get(mapping.get("asset_tag", ""), "") or "").strip(),
            category=(row.get(mapping.get("category", ""), "") or "").strip(),
            home_location=(row.get(mapping.get("home_location", ""), "") or "").strip(),
            notes=(row.get(mapping.get("notes", ""), "") or "").strip(),
            actor=actor,
        )
        count += 1
    return count


def is_overdue(tool: sqlite3.Row | dict, hours: float) -> bool:
    status = tool["status"] if not isinstance(tool, dict) else tool.get("status")
    if status != "out":
        return False
    checked = tool["checked_out_at"] if not isinstance(tool, dict) else tool.get("checked_out_at")
    dt = parse_iso(checked)
    if not dt:
        return False
    return utc_now() - dt >= timedelta(hours=hours)


def missing_or_overdue(db: sqlite3.Connection) -> list[dict[str, Any]]:
    hours = overdue_hours(db)
    rows = db.execute(
        """
        SELECT t.*, p.name AS holder_name
        FROM tools t
        LEFT JOIN people p ON p.id = t.holder_person_id
        WHERE t.status = 'missing' OR t.status = 'out'
        ORDER BY CASE WHEN t.checked_out_at IS NULL THEN 1 ELSE 0 END, t.checked_out_at ASC, t.id
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        overdue = is_overdue(row, hours)
        if d["status"] == "missing" or overdue:
            d["is_overdue"] = overdue
            d["overdue_hours_threshold"] = hours
            out.append(d)
    return out


def checkout_tool(
    db: sqlite3.Connection,
    tool: sqlite3.Row,
    *,
    person_id: int,
    job_ref: str = "",
    note: str = "",
    actor: str,
    force: bool = False,
) -> tuple[str, str]:
    """Return (result_key, message). result_key: ok|noop|need_confirm|error."""
    person = get_person(db, person_id)
    if not person:
        return "error", "Choose a valid person."

    status = tool["status"]
    holder = tool["holder_person_id"]

    if status == "out" and holder == person_id:
        return "noop", f"Already checked out to {person['name']}."

    if status == "out" and holder and holder != person_id and not force:
        other = get_person(db, int(holder))
        other_name = other["name"] if other else "someone else"
        return "need_confirm", f"Currently out to {other_name}. Confirm force transfer."

    kind = "force_transfer" if (status == "out" and holder and holder != person_id) else "checked_out"
    now = utc_now_iso()
    db.execute(
        """
        UPDATE tools
        SET status = 'out', holder_person_id = ?, checked_out_at = ?, job_ref = ?
        WHERE id = ?
        """,
        (person_id, now, job_ref.strip() or None, tool["id"]),
    )
    add_event(
        db,
        tool_id=int(tool["id"]),
        kind=kind,
        actor=actor,
        person_id=person_id,
        job_ref=job_ref.strip() or None,
        note=note.strip() or None,
        at=now,
    )
    db.commit()
    if kind == "force_transfer":
        return "ok", f"Force transferred to {person['name']}."
    return "ok", f"Checked out to {person['name']}."


def return_tool(
    db: sqlite3.Connection,
    tool: sqlite3.Row,
    *,
    note: str = "",
    actor: str,
) -> tuple[str, str]:
    if tool["status"] == "available":
        return "noop", "Already available."

    now = utc_now_iso()
    prev_person = tool["holder_person_id"]
    db.execute(
        """
        UPDATE tools
        SET status = 'available', holder_person_id = NULL, checked_out_at = NULL, job_ref = NULL
        WHERE id = ?
        """,
        (tool["id"],),
    )
    add_event(
        db,
        tool_id=int(tool["id"]),
        kind="returned",
        actor=actor,
        person_id=int(prev_person) if prev_person else None,
        note=note.strip() or None,
        at=now,
    )
    db.commit()
    return "ok", "Returned to shop."


def mark_missing(db: sqlite3.Connection, tool: sqlite3.Row, *, missing: bool, actor: str = "owner") -> None:
    now = utc_now_iso()
    if missing:
        db.execute("UPDATE tools SET status = 'missing' WHERE id = ?", (tool["id"],))
        add_event(
            db,
            tool_id=int(tool["id"]),
            kind="marked_missing",
            actor=actor,
            person_id=int(tool["holder_person_id"]) if tool["holder_person_id"] else None,
            at=now,
        )
    else:
        # Clear missing: restore out if holder present, else available
        if tool["holder_person_id"]:
            db.execute("UPDATE tools SET status = 'out' WHERE id = ?", (tool["id"],))
        else:
            db.execute(
                """
                UPDATE tools
                SET status = 'available', holder_person_id = NULL, checked_out_at = NULL, job_ref = NULL
                WHERE id = ?
                """,
                (tool["id"],),
            )
        add_event(
            db,
            tool_id=int(tool["id"]),
            kind="cleared_missing",
            actor=actor,
            person_id=int(tool["holder_person_id"]) if tool["holder_person_id"] else None,
            at=now,
        )
    db.commit()


def recent_events(db: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT e.*, t.name AS tool_name, p.name AS person_name
        FROM events e
        JOIN tools t ON t.id = e.tool_id
        LEFT JOIN people p ON p.id = e.person_id
        ORDER BY e.at DESC, e.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def tool_events(db: sqlite3.Connection, tool_id: int, limit: int = 50) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT e.*, p.name AS person_name
        FROM events e
        LEFT JOIN people p ON p.id = e.person_id
        WHERE e.tool_id = ?
        ORDER BY e.at DESC, e.id DESC
        LIMIT ?
        """,
        (tool_id, limit),
    ).fetchall()


def custody_csv(db: sqlite3.Connection) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "tool_id",
            "name",
            "asset_tag",
            "category",
            "home_location",
            "status",
            "holder",
            "checked_out_at",
            "job_ref",
            "token",
            "public_url",
        ]
    )
    for row in list_tools(db):
        writer.writerow(
            [
                row["id"],
                row["name"],
                row["asset_tag"] or "",
                row["category"] or "",
                row["home_location"] or "",
                row["status"],
                row["holder_name"] or "",
                row["checked_out_at"] or "",
                row["job_ref"] or "",
                row["token"],
                tool_public_url(row["token"]),
            ]
        )
    return buf.getvalue()


def events_csv(db: sqlite3.Connection) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["id", "at", "tool_id", "tool_name", "kind", "person", "actor", "job_ref", "note"]
    )
    rows = db.execute(
        """
        SELECT e.*, t.name AS tool_name, p.name AS person_name
        FROM events e
        JOIN tools t ON t.id = e.tool_id
        LEFT JOIN people p ON p.id = e.person_id
        ORDER BY e.at ASC, e.id ASC
        """
    ).fetchall()
    for row in rows:
        writer.writerow(
            [
                row["id"],
                row["at"],
                row["tool_id"],
                row["tool_name"],
                row["kind"],
                row["person_name"] or "",
                row["actor"],
                row["job_ref"] or "",
                row["note"] or "",
            ]
        )
    return buf.getvalue()


def qr_data_uri(url: str, box_size: int = 4) -> str:
    import qrcode

    qr = qrcode.QRCode(version=None, box_size=box_size, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def send_smtp(to_email: str, subject: str, body: str) -> None:
    host = _env("SMTP_HOST")
    if not host:
        raise RuntimeError("SMTP is not configured.")
    from_email = _env("FROM_EMAIL") or _env("OWNER_EMAIL")
    if not from_email:
        raise RuntimeError("FROM_EMAIL or OWNER_EMAIL is required to send mail.")
    port = int(_env("SMTP_PORT") or "587")
    user = _env("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD", "")
    tls_raw = _env("SMTP_TLS") or "true"
    use_tls = tls_raw.lower() in {"1", "true", "yes", "on"}
    from_name = _env("FROM_NAME") or business_name()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, from_email)) if from_name else from_email
    msg["To"] = to_email
    msg.set_content(body)
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if use_tls:
            smtp.starttls()
        if user:
            smtp.login(user, password)
        smtp.send_message(msg)


def send_twilio_sms(to_phone: str, body: str) -> None:
    sid = _env("TWILIO_ACCOUNT_SID")
    token = _env("TWILIO_AUTH_TOKEN")
    from_number = _env("TWILIO_FROM_NUMBER")
    if not (sid and token and from_number):
        raise RuntimeError("Twilio is not configured.")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urllib.parse.urlencode(
        {"To": to_phone, "From": from_number, "Body": body}
    ).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    credentials = base64.b64encode(f"{sid}:{token}".encode("utf-8")).decode("ascii")
    req.add_header("Authorization", f"Basic {credentials}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"Twilio HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Twilio HTTP {exc.code}") from exc


def notify_missing(db: sqlite3.Connection) -> dict[str, Any]:
    items = missing_or_overdue(db)
    meta: dict[str, Any] = {"email": False, "sms": False, "count": len(items)}
    if not items:
        meta["message"] = "No missing or overdue tools."
        return meta

    lines = []
    for item in items:
        holder = item.get("holder_name") or "unknown"
        since = item.get("checked_out_at") or "?"
        flag = "MISSING" if item["status"] == "missing" else "OVERDUE"
        lines.append(f"- [{flag}] {item['name']} — last with {holder} since {since}")
    body = (
        f"Missing / overdue tools for {business_name()}:\n\n"
        + "\n".join(lines)
        + "\n"
    )
    subject = f"[{business_name()}] {len(items)} missing/overdue tool(s)"

    owner_email = _env("OWNER_EMAIL")
    owner_phone = _env("OWNER_PHONE")

    if not smtp_configured() and not twilio_configured():
        meta["message"] = "SMTP/Twilio not configured."
        return meta
    if not owner_email and not owner_phone:
        meta["message"] = "Set OWNER_EMAIL and/or OWNER_PHONE to notify."
        return meta

    if smtp_configured() and owner_email:
        try:
            send_smtp(owner_email, subject, body)
            meta["email"] = True
        except Exception as exc:  # noqa: BLE001
            meta["email_error"] = str(exc)

    if twilio_configured() and owner_phone:
        try:
            sms = f"{business_name()}: {len(items)} missing/overdue tool(s). Check dashboard."
            send_twilio_sms(owner_phone, sms[:1500])
            meta["sms"] = True
        except Exception as exc:  # noqa: BLE001
            meta["sms_error"] = str(exc)

    if meta.get("email") or meta.get("sms"):
        meta["message"] = "Notification sent."
    elif not meta.get("message"):
        meta["message"] = "Notify failed."
    return meta


def out_count(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT COUNT(*) AS c FROM tools WHERE status = 'out'").fetchone()
    return int(row["c"]) if row else 0
