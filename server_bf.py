"""
server_bf.py — Serveur Web Interactif Mobile-First (API Backend + Interface Mobile Responsive) pour ProspectBF v2.6.
Optimisé spécifiquement pour les écrans de smartphones (Android / iOS 360px-430px) et PC.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote_plus, urlparse

from prospect_bf import (
    CITIES,
    SECTOR_CATEGORIES,
    BusinessLead,
    audit_single_business,
    classify_business_sector,
    export_csv,
    fetch_osm_businesses,
    is_likely_mobile_bf,
    normalize_bf_phone,
)

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "leads_state.json"
DEFAULT_INVITE_CODE = os.environ.get("INVITE_CODE", "BF-PROSPECT-2026")
DEFAULT_ADMIN_USER = {
    "id": 1,
    "username": "admin",
    "display_name": "Direction Agence",
    "role": "admin",
}

PIPELINE_ACTIVE_STATUSES = {"CONTACTE", "RDV", "CLIENT", "LIVRE", "PAYE", "ECHEC", "IGNORE"}
VALID_CRM_STATUSES = {"NOUVEAU", "A_CONTACTER", "CONTACTE", "RDV", "CLIENT", "LIVRE", "PAYE", "ECHEC", "IGNORE"}


def _safe_int_amount(val: object) -> int:
    try:
        return max(0, int(float(val or 0)))
    except (ValueError, TypeError):
        return 0


def lead_to_dict(b: BusinessLead) -> dict:
    from urllib.parse import quote_plus
    phone_display = b.phone_clean or b.phone_raw or "Non renseigné"
    is_mobile = is_likely_mobile_bf(b.phone_clean)
    has_coords = bool(b.lat and b.lon and b.city != "Audit Direct")
    if has_coords:
        maps_url = f"https://www.google.com/maps/search/?api=1&query={b.lat},{b.lon}"
    else:
        loc = b.city if b.city and b.city != "Audit Direct" else "Burkina Faso"
        q = quote_plus(f"{b.name} {loc}".strip())
        maps_url = f"https://www.google.com/maps/search/?api=1&query={q}"
    crm_st = getattr(b, "crm_status", "A_CONTACTER") or "A_CONTACTER"
    in_pipe = bool(getattr(b, "in_pipeline", False) or (crm_st in PIPELINE_ACTIVE_STATUSES))
    return {
        "name": b.name,
        "city": b.city,
        "neighborhood": b.neighborhood,
        "sector": b.sector,
        "sector_key": b.sector_key,
        "phone": phone_display,
        "phone_type": "MOBILE_WHATSAPP" if is_mobile else ("FIXE_ONATEL" if b.phone_clean else "AUCUN"),
        "tel_url": f"tel:{b.phone_clean}" if b.phone_clean else "",
        "email": getattr(b, "email", "") or "",
        "website": b.website,
        "facebook_url": b.facebook_url,
        "status": b.audit_status,
        "response_ms": b.response_ms,
        "issues": b.issues,
        "score": b.lead_score,
        "pitch": b.pitch_angle,
        "whatsapp_url": b.whatsapp_url,
        "lat": b.lat,
        "lon": b.lon,
        "maps_url": maps_url,
        "crm_status": crm_st,
        "in_pipeline": in_pipe,
        "rdv_done": bool(getattr(b, "rdv_done", False)),
        "deal_amount": _safe_int_amount(getattr(b, "deal_amount", 0)),
        "notes": str(getattr(b, "notes", "") or ""),
        "updated_at": str(getattr(b, "updated_at", "") or ""),
        "has_fb_pixel": bool(getattr(b, "has_fb_pixel", False)),
        "has_google_analytics": bool(getattr(b, "has_google_analytics", False)),
    }


STATE_LOCK = threading.RLock()


def get_db_path() -> Path:
    if STATE_FILE == BASE_DIR / "leads_state.json":
        return BASE_DIR / "prospectbf.db"
    return STATE_FILE.with_suffix(".db")


def get_db_connection(db_path: Path | None = None) -> sqlite3.Connection:
    target = db_path or get_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=15.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    if not salt:
        salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000).hex()
    return hashed, salt


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    hashed, _ = hash_password(password, salt)
    return secrets.compare_digest(hashed, expected_hash)


def init_db(db_path: Path | None = None) -> None:
    with STATE_LOCK:
        conn = get_db_connection(db_path)
        try:
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT UNIQUE NOT NULL COLLATE NOCASE,
                        display_name TEXT NOT NULL,
                        password_hash TEXT NOT NULL,
                        password_salt TEXT NOT NULL,
                        role TEXT NOT NULL DEFAULT 'commercial',
                        created_at TEXT NOT NULL
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS sessions (
                        token TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS shared_leads (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        city TEXT NOT NULL,
                        name_norm TEXT NOT NULL,
                        city_norm TEXT NOT NULL,
                        neighborhood TEXT DEFAULT '',
                        sector TEXT DEFAULT '',
                        sector_key TEXT DEFAULT '',
                        phone TEXT DEFAULT '',
                        phone_type TEXT DEFAULT '',
                        tel_url TEXT DEFAULT '',
                        email TEXT DEFAULT '',
                        website TEXT DEFAULT '',
                        facebook_url TEXT DEFAULT '',
                        status TEXT DEFAULT '',
                        response_ms INTEGER DEFAULT 0,
                        issues_json TEXT DEFAULT '[]',
                        score INTEGER DEFAULT 0,
                        pitch TEXT DEFAULT '',
                        whatsapp_url TEXT DEFAULT '',
                        lat REAL DEFAULT 0.0,
                        lon REAL DEFAULT 0.0,
                        maps_url TEXT DEFAULT '',
                        has_fb_pixel INTEGER DEFAULT 0,
                        has_google_analytics INTEGER DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(name_norm, city_norm)
                    );
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_shared_leads_lookup ON shared_leads(name_norm, city_norm);")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_pipelines (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        lead_id INTEGER NOT NULL REFERENCES shared_leads(id) ON DELETE CASCADE,
                        crm_status TEXT NOT NULL DEFAULT 'A_CONTACTER',
                        in_pipeline INTEGER NOT NULL DEFAULT 0,
                        rdv_done INTEGER NOT NULL DEFAULT 0,
                        deal_amount INTEGER NOT NULL DEFAULT 0,
                        notes TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL,
                        UNIQUE(user_id, lead_id)
                    );
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_user_pipelines_uid ON user_pipelines(user_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_user_pipelines_lid ON user_pipelines(lead_id);")

                cur = conn.execute("SELECT COUNT(*) AS c FROM users;")
                if cur.fetchone()["c"] == 0:
                    admin_pass = os.environ.get("ADMIN_PASSWORD", "admin2026")
                    h, s = hash_password(admin_pass)
                    now_str = datetime.now(timezone.utc).isoformat()
                    conn.execute(
                        "INSERT INTO users (id, username, display_name, password_hash, password_salt, role, created_at) VALUES (1, 'admin', 'Direction Agence', ?, ?, 'admin', ?);",
                        (h, s, now_str),
                    )

                cur_leads = conn.execute("SELECT COUNT(*) AS c FROM shared_leads;")
                if cur_leads.fetchone()["c"] == 0 and STATE_FILE.exists():
                    try:
                        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                        if isinstance(raw, list) and len(raw) > 0:
                            _import_leads_into_db(conn, raw, user_id=1)
                    except Exception:
                        pass
        finally:
            conn.close()


def _import_leads_into_db(conn: sqlite3.Connection, leads_list: list[dict], user_id: int = 1) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    for item in leads_list:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        city = str(item.get("city", "")).strip()
        if not name:
            continue
        name_norm = name.lower()
        city_norm = city.lower()

        skey = str(item.get("sector_key") or "").strip().lower()
        sector = str(item.get("sector") or "")
        if not skey or skey not in SECTOR_CATEGORIES or skey == "all":
            skey, slbl = classify_business_sector({"name": name, "description": sector})
            if not sector:
                sector = slbl

        maps_url = str(item.get("maps_url") or "")
        if not maps_url:
            lat = item.get("lat")
            lon = item.get("lon")
            if lat and lon and city != "Audit Direct":
                maps_url = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
            else:
                loc = city if city and city != "Audit Direct" else "Burkina Faso"
                q = quote_plus(f"{name} {loc}".strip())
                maps_url = f"https://www.google.com/maps/search/?api=1&query={q}"

        issues = item.get("issues", [])
        issues_json = json.dumps(issues if isinstance(issues, list) else [])
        up_at = str(item.get("updated_at") or now_iso)

        conn.execute("""
            INSERT INTO shared_leads (
                name, city, name_norm, city_norm, neighborhood, sector, sector_key,
                phone, phone_type, tel_url, email, website, facebook_url, status,
                response_ms, issues_json, score, pitch, whatsapp_url, lat, lon,
                maps_url, has_fb_pixel, has_google_analytics, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name_norm, city_norm) DO UPDATE SET
                neighborhood = excluded.neighborhood,
                sector = excluded.sector,
                sector_key = excluded.sector_key,
                phone = excluded.phone,
                phone_type = excluded.phone_type,
                tel_url = excluded.tel_url,
                email = excluded.email,
                website = excluded.website,
                facebook_url = excluded.facebook_url,
                status = excluded.status,
                response_ms = excluded.response_ms,
                issues_json = excluded.issues_json,
                score = excluded.score,
                pitch = excluded.pitch,
                whatsapp_url = excluded.whatsapp_url,
                lat = excluded.lat,
                lon = excluded.lon,
                maps_url = excluded.maps_url,
                has_fb_pixel = excluded.has_fb_pixel,
                has_google_analytics = excluded.has_google_analytics,
                updated_at = excluded.updated_at;
        """, (
            name, city, name_norm, city_norm,
            str(item.get("neighborhood") or ""),
            sector,
            skey,
            str(item.get("phone") or "Non renseigné"),
            str(item.get("phone_type") or "AUCUN"),
            str(item.get("tel_url") or ""),
            str(item.get("email") or ""),
            str(item.get("website") or ""),
            str(item.get("facebook_url") or ""),
            str(item.get("status") or "SANS_SITE"),
            int(item.get("response_ms") or 0),
            issues_json,
            int(item.get("score") or 0),
            str(item.get("pitch") or ""),
            str(item.get("whatsapp_url") or ""),
            float(item.get("lat") or 0.0),
            float(item.get("lon") or 0.0),
            maps_url,
            1 if item.get("has_fb_pixel") else 0,
            1 if item.get("has_google_analytics") else 0,
            str(item.get("created_at") or now_iso),
            up_at
        ))

        lead_row = conn.execute("SELECT id FROM shared_leads WHERE name_norm = ? AND city_norm = ?;", (name_norm, city_norm)).fetchone()
        if lead_row:
            lead_id = lead_row["id"]
            crm_st = str(item.get("crm_status") or "A_CONTACTER")
            in_pipe = 1 if (item.get("in_pipeline") or crm_st in PIPELINE_ACTIVE_STATUSES) else 0
            rdv = 1 if item.get("rdv_done") else 0
            deal = _safe_int_amount(item.get("deal_amount", 0))
            notes = str(item.get("notes") or "")
            conn.execute("""
                INSERT INTO user_pipelines (
                    user_id, lead_id, crm_status, in_pipeline, rdv_done, deal_amount, notes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, lead_id) DO UPDATE SET
                    crm_status = excluded.crm_status,
                    in_pipeline = excluded.in_pipeline,
                    rdv_done = excluded.rdv_done,
                    deal_amount = excluded.deal_amount,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at;
            """, (user_id, lead_id, crm_st, in_pipe, rdv, deal, notes, up_at))


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=7)
    with STATE_LOCK:
        conn = get_db_connection()
        try:
            with conn:
                conn.execute("DELETE FROM sessions WHERE expires_at <= ?;", (now.isoformat(),))
                conn.execute(
                    "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?);",
                    (token, user_id, now.isoformat(), expires.isoformat()),
                )
        finally:
            conn.close()
    return token


def get_user_by_session(token: str) -> dict | None:
    if not token:
        return None
    init_db()
    with STATE_LOCK:
        conn = get_db_connection()
        try:
            now_str = datetime.now(timezone.utc).isoformat()
            cur = conn.execute("""
                SELECT u.id, u.username, u.display_name, u.role
                FROM sessions s
                JOIN users u ON s.user_id = u.id
                WHERE s.token = ? AND s.expires_at > ?;
            """, (token, now_str))
            row = cur.fetchone()
            if row:
                return dict(row)
            with conn:
                conn.execute("DELETE FROM sessions WHERE token = ? AND expires_at <= ?;", (token, now_str))
            return None
        finally:
            conn.close()


def delete_session(token: str) -> None:
    if not token:
        return
    with STATE_LOCK:
        conn = get_db_connection()
        try:
            with conn:
                conn.execute("DELETE FROM sessions WHERE token = ?;", (token,))
        finally:
            conn.close()


def register_user(username: str, password: str, display_name: str, invite_code: str) -> tuple[bool, str, dict | None]:
    init_db()
    invite_code = invite_code.strip()
    if invite_code.upper() != DEFAULT_INVITE_CODE.strip().upper():
        return False, "Code d'invitation agence invalide", None

    username = username.strip()
    if len(username) < 3 or not re.match(r"^[a-zA-Z0-9_.@+-]{3,100}$", username):
        return False, "Nom d'utilisateur invalide (3 à 100 caractères alphanumériques ou email)", None

    password = password.strip()
    if len(password) < 6:
        return False, "Le mot de passe doit comporter au moins 6 caractères", None

    display_name = display_name.strip() or username

    with STATE_LOCK:
        conn = get_db_connection()
        try:
            with conn:
                cur = conn.execute("SELECT id FROM users WHERE username = ?;", (username,))
                if cur.fetchone():
                    return False, "Ce nom d'utilisateur est déjà utilisé", None

                cur_users = conn.execute("SELECT COUNT(*) AS c FROM users;")
                count = cur_users.fetchone()["c"]
                role = "admin" if (count == 0 or username.lower() == "admin") else "commercial"

                hashed, salt = hash_password(password)
                now_str = datetime.now(timezone.utc).isoformat()
                try:
                    cur_ins = conn.execute(
                        "INSERT INTO users (username, display_name, password_hash, password_salt, role, created_at) VALUES (?, ?, ?, ?, ?, ?);",
                        (username, display_name, hashed, salt, role, now_str),
                    )
                    user_id = cur_ins.lastrowid
                except sqlite3.IntegrityError:
                    return False, "Ce nom d'utilisateur est déjà utilisé", None

                user_obj = {
                    "id": user_id,
                    "username": username,
                    "display_name": display_name,
                    "role": role,
                }
                return True, "Utilisateur enregistré avec succès", user_obj
        finally:
            conn.close()


def authenticate_user(username: str, password: str) -> tuple[bool, str, dict | None]:
    init_db()
    username = username.strip()
    password = password.strip()
    with STATE_LOCK:
        conn = get_db_connection()
        try:
            cur = conn.execute(
                "SELECT id, username, display_name, password_hash, password_salt, role FROM users WHERE username = ?;",
                (username,),
            )
            row = cur.fetchone()
            if not row:
                return False, "Nom d'utilisateur ou mot de passe incorrect", None

            if not verify_password(password, row["password_salt"], row["password_hash"]):
                return False, "Nom d'utilisateur ou mot de passe incorrect", None

            user_obj = {
                "id": row["id"],
                "username": row["username"],
                "display_name": row["display_name"],
                "role": row["role"],
            }
            return True, "Authentification réussie", user_obj
        finally:
            conn.close()


def get_token_from_request(handler: BaseHTTPRequestHandler) -> str | None:
    cookie_header = handler.headers.get("Cookie", "")
    if cookie_header:
        try:
            c = cookies.SimpleCookie()
            c.load(cookie_header)
            if "session_token" in c:
                return c["session_token"].value
        except Exception:
            pass
    auth_header = handler.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    return None


def get_request_user(handler: BaseHTTPRequestHandler) -> dict | None:
    token = get_token_from_request(handler)
    if token:
        return get_user_by_session(token)
    return None


def get_admin_overview() -> dict:
    init_db()
    with STATE_LOCK:
        conn = get_db_connection()
        try:
            cur_leads = conn.execute("SELECT COUNT(*) AS c FROM shared_leads;")
            total_leads = cur_leads.fetchone()["c"]

            cur_reps = conn.execute("""
                SELECT
                    u.id AS user_id,
                    u.username,
                    u.display_name,
                    u.role,
                    u.created_at,
                    COUNT(CASE WHEN up.in_pipeline = 1 OR up.crm_status IN ('CONTACTE', 'RDV', 'CLIENT', 'LIVRE', 'PAYE', 'ECHEC', 'IGNORE') THEN 1 END) AS pipeline_count,
                    COUNT(CASE WHEN up.crm_status IN ('CLIENT', 'LIVRE', 'PAYE') THEN 1 END) AS won_count,
                    COALESCE(SUM(CASE WHEN up.crm_status = 'PAYE' THEN up.deal_amount ELSE 0 END), 0) AS paid_revenue_fcfa,
                    COALESCE(SUM(CASE WHEN up.crm_status IN ('CONTACTE', 'RDV', 'CLIENT', 'LIVRE', 'PAYE') THEN up.deal_amount ELSE 0 END), 0) AS pipeline_fcfa,
                    COALESCE(MAX(up.updated_at), u.created_at) AS last_active
                FROM users u
                LEFT JOIN user_pipelines up ON u.id = up.user_id
                GROUP BY u.id
                ORDER BY paid_revenue_fcfa DESC, pipeline_fcfa DESC, u.id ASC;
            """)
            reps = [dict(r) for r in cur_reps.fetchall()]

            total_paid = sum(r["paid_revenue_fcfa"] for r in reps)
            total_pipeline = sum(r["pipeline_fcfa"] for r in reps)
            total_won = sum(r["won_count"] for r in reps)
            total_active = sum(r["pipeline_count"] for r in reps)

            return {
                "total_shared_leads": total_leads,
                "total_reps": len(reps),
                "agency_revenue_paid_fcfa": total_paid,
                "agency_pipeline_fcfa": total_pipeline,
                "agency_deals_won": total_won,
                "agency_deals_active": total_active,
                "reps": reps,
            }
        finally:
            conn.close()


def load_saved_leads(user_id: int | None = None) -> list[dict]:
    init_db()
    uid = user_id if user_id is not None else 1
    with STATE_LOCK:
        conn = get_db_connection()
        try:
            claims_cur = conn.execute("""
                SELECT up.lead_id, up.user_id, u.display_name, u.username, up.crm_status, up.in_pipeline
                FROM user_pipelines up
                JOIN users u ON up.user_id = u.id
                WHERE up.in_pipeline = 1 OR up.crm_status IN ('CONTACTE', 'RDV', 'CLIENT', 'LIVRE', 'PAYE', 'ECHEC', 'IGNORE');
            """)
            claims_map: dict[int, list[dict]] = {}
            for r in claims_cur.fetchall():
                claims_map.setdefault(r["lead_id"], []).append(dict(r))

            sql = """
                SELECT
                    sl.*,
                    up.crm_status AS user_crm_status,
                    up.in_pipeline AS user_in_pipeline,
                    up.rdv_done AS user_rdv_done,
                    up.deal_amount AS user_deal_amount,
                    up.notes AS user_notes,
                    up.updated_at AS user_updated_at
                FROM shared_leads sl
                LEFT JOIN user_pipelines up ON up.lead_id = sl.id AND up.user_id = ?
                ORDER BY sl.score DESC, (sl.phone_type = 'MOBILE_WHATSAPP') DESC;
            """
            cur = conn.execute(sql, (uid,))
            rows = cur.fetchall()
            result = []
            for r in rows:
                lead_id = r["id"]
                crm_st = r["user_crm_status"] or "A_CONTACTER"
                in_pipe = bool(r["user_in_pipeline"] or (crm_st in PIPELINE_ACTIVE_STATUSES))
                rdv_done = bool(r["user_rdv_done"])
                deal_amount = _safe_int_amount(r["user_deal_amount"])
                notes = str(r["user_notes"] or "")
                updated_at = str(r["user_updated_at"] or r["updated_at"] or "")

                lead_claims = claims_map.get(lead_id, [])
                other_claim = next((c for c in lead_claims if c["user_id"] != uid), None)
                my_claim = any(c["user_id"] == uid for c in lead_claims)

                try:
                    issues = json.loads(r["issues_json"]) if r["issues_json"] else []
                except Exception:
                    issues = []

                skey = str(r["sector_key"] or "").strip().lower()
                sector = str(r["sector"] or "")
                if not skey or skey not in SECTOR_CATEGORIES or skey == "all":
                    skey, slbl = classify_business_sector({"name": r["name"], "description": sector})
                    if not sector:
                        sector = slbl

                maps_url = r["maps_url"] or ""
                if not maps_url:
                    lat = r["lat"]
                    lon = r["lon"]
                    city = r["city"] or ""
                    name = r["name"] or ""
                    if lat and lon and city != "Audit Direct":
                        maps_url = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
                    else:
                        loc = city if city and city != "Audit Direct" else "Burkina Faso"
                        q = quote_plus(f"{name} {loc}".strip())
                        maps_url = f"https://www.google.com/maps/search/?api=1&query={q}"

                lead = {
                    "name": r["name"],
                    "city": r["city"],
                    "neighborhood": r["neighborhood"] or "",
                    "sector": sector,
                    "sector_key": skey,
                    "phone": r["phone"] or "Non renseigné",
                    "phone_type": r["phone_type"] or "AUCUN",
                    "tel_url": r["tel_url"] or "",
                    "email": r["email"] or "",
                    "website": r["website"] or "",
                    "facebook_url": r["facebook_url"] or "",
                    "status": r["status"] or "SANS_SITE",
                    "response_ms": r["response_ms"] or 0,
                    "issues": issues,
                    "score": r["score"] or 0,
                    "pitch": r["pitch"] or "",
                    "whatsapp_url": r["whatsapp_url"] or "",
                    "lat": r["lat"] or 0.0,
                    "lon": r["lon"] or 0.0,
                    "maps_url": maps_url,
                    "crm_status": crm_st,
                    "in_pipeline": in_pipe,
                    "rdv_done": rdv_done,
                    "deal_amount": deal_amount,
                    "notes": notes,
                    "updated_at": updated_at,
                    "has_fb_pixel": bool(r["has_fb_pixel"]),
                    "has_google_analytics": bool(r["has_google_analytics"]),
                    "claimed_by": other_claim["display_name"] if other_claim else "",
                    "claimed_by_name": other_claim["display_name"] if other_claim else "",
                    "claimed_by_id": other_claim["user_id"] if other_claim else None,
                    "is_claimed_by_me": bool(my_claim or in_pipe),
                }
                result.append(lead)
            return result
        finally:
            conn.close()


def save_leads(leads_dicts: list[dict], user_id: int | None = None) -> None:
    init_db()
    uid = user_id if user_id is not None else 1
    with STATE_LOCK:
        conn = get_db_connection()
        try:
            with conn:
                if not leads_dicts:
                    conn.execute("DELETE FROM user_pipelines;")
                    conn.execute("DELETE FROM shared_leads;")
                else:
                    _import_leads_into_db(conn, leads_dicts, user_id=uid)
        finally:
            conn.close()

        # Atomic synchronization with STATE_FILE for legacy compatibility
        try:
            snapshot = load_saved_leads(user_id=1)
            payload = json.dumps(snapshot, ensure_ascii=False, indent=2)
            unique_suffix = f"{os.getpid()}_{threading.get_ident()}_{time.time_ns()}"
            temp_file = STATE_FILE.with_name(f"{STATE_FILE.name}.{unique_suffix}.tmp")
            temp_file.write_text(payload, encoding="utf-8")
            os.replace(temp_file, STATE_FILE)
        except Exception:
            try:
                STATE_FILE.write_text(payload, encoding="utf-8")
            except Exception:
                pass
        finally:
            if 'temp_file' in locals() and temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass




def merge_leads(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Fusionne les nouveaux leads avec les existants sans écraser les statuts CRM, montants, RDV ou notes."""
    from urllib.parse import quote_plus
    merged_map: dict[str, dict] = {}
    for item in existing:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        city = str(item.get("city", "")).strip()
        key = f"{name.lower()}|{city.lower()}"
        if not name and not city:
            continue
        entry = dict(item)
        if not entry.get("crm_status"):
            entry["crm_status"] = "A_CONTACTER"
        entry["in_pipeline"] = bool(entry.get("in_pipeline", False) or (entry["crm_status"] in PIPELINE_ACTIVE_STATUSES))
        entry["rdv_done"] = bool(entry.get("rdv_done", False))
        entry["deal_amount"] = _safe_int_amount(entry.get("deal_amount", 0))
        entry["notes"] = str(entry.get("notes") or "")
        entry["updated_at"] = str(entry.get("updated_at") or "")
        merged_map[key] = entry

    for item in incoming:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        city = str(item.get("city", "")).strip()
        key = f"{name.lower()}|{city.lower()}"
        if not name and not city:
            continue
        entry = dict(item)
        if key in merged_map:
            existing_lead = merged_map[key]
            existing_status = existing_lead.get("crm_status")
            existing_in_pipe = bool(existing_lead.get("in_pipeline", False) or (existing_status in PIPELINE_ACTIVE_STATUSES))
            existing_rdv_done = bool(existing_lead.get("rdv_done", False))
            existing_amount = _safe_int_amount(existing_lead.get("deal_amount", 0))
            existing_notes = str(existing_lead.get("notes") or "")
            existing_updated = str(existing_lead.get("updated_at") or "").strip()
            entry_updated = str(entry.get("updated_at") or "").strip()

            combined = dict(existing_lead)
            combined.update(entry)

            if entry_updated and (not existing_updated or entry_updated >= existing_updated):
                combined["crm_status"] = entry.get("crm_status") or existing_status or "A_CONTACTER"
                combined["in_pipeline"] = bool(
                    entry.get("in_pipeline", existing_in_pipe)
                    or (combined["crm_status"] in PIPELINE_ACTIVE_STATUSES)
                )
                combined["rdv_done"] = bool(entry.get("rdv_done", False))
                combined["deal_amount"] = _safe_int_amount(entry.get("deal_amount", 0))
                combined["notes"] = str(entry.get("notes") or "")
                combined["updated_at"] = entry_updated
            else:
                if existing_status and (existing_status != "A_CONTACTER" or not entry.get("crm_status") or entry.get("crm_status") == "A_CONTACTER"):
                    combined["crm_status"] = existing_status
                else:
                    combined["crm_status"] = entry.get("crm_status") or "A_CONTACTER"

                combined["in_pipeline"] = bool(
                    existing_in_pipe
                    or entry.get("in_pipeline", False)
                    or (combined["crm_status"] in PIPELINE_ACTIVE_STATUSES)
                )
                combined["rdv_done"] = bool(existing_rdv_done or entry.get("rdv_done", False))
                combined["deal_amount"] = _safe_int_amount(entry.get("deal_amount")) if _safe_int_amount(entry.get("deal_amount")) > 0 else existing_amount
                combined["notes"] = str(entry.get("notes") or "") if str(entry.get("notes") or "").strip() else existing_notes
                combined["updated_at"] = existing_updated or entry_updated
            merged_map[key] = combined
        else:
            if not entry.get("crm_status"):
                entry["crm_status"] = "A_CONTACTER"
            entry["in_pipeline"] = bool(entry.get("in_pipeline", False) or (entry["crm_status"] in PIPELINE_ACTIVE_STATUSES))
            entry["rdv_done"] = bool(entry.get("rdv_done", False))
            entry["deal_amount"] = _safe_int_amount(entry.get("deal_amount", 0))
            entry["notes"] = str(entry.get("notes") or "")
            entry["updated_at"] = str(entry.get("updated_at") or "")
            merged_map[key] = entry

    for lead in merged_map.values():
        lead.setdefault("has_fb_pixel", False)
        lead.setdefault("has_google_analytics", False)
        lead.setdefault("in_pipeline", bool(lead.get("crm_status") in PIPELINE_ACTIVE_STATUSES))
        lead.setdefault("rdv_done", False)
        lead.setdefault("deal_amount", 0)
        lead.setdefault("notes", "")
        lead.setdefault("updated_at", "")
        if not lead.get("maps_url"):
            lat = lead.get("lat")
            lon = lead.get("lon")
            c = lead.get("city", "")
            n = lead.get("name", "")
            if lat and lon and c != "Audit Direct":
                lead["maps_url"] = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
            else:
                loc = c if c and c != "Audit Direct" else "Burkina Faso"
                q = quote_plus(f"{n} {loc}".strip())
                lead["maps_url"] = f"https://www.google.com/maps/search/?api=1&query={q}"

    result = list(merged_map.values())
    result.sort(
        key=lambda b: (
            int(b.get("score", 0)),
            b.get("phone_type") == "MOBILE_WHATSAPP",
            b.get("phone_type") != "AUCUN",
        ),
        reverse=True,
    )
    return result


INDEX_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0" />
  <title>ProspectBF — Plateforme SaaS B2B & CRM Burkina Faso</title>
  <style>
    :root {
      --bg: #f8fafc;
      --bg-mesh: radial-gradient(circle at 10% 0%, rgba(37, 99, 235, 0.05), transparent 45%),
                 radial-gradient(circle at 90% 12%, rgba(16, 185, 129, 0.04), transparent 45%);
      --surface-0: #ffffff;
      --card: #ffffff;
      --card-2: #f1f5f9;
      --card-hover: #ffffff;
      --border: #e2e8f0;
      --border-light: #cbd5e1;
      --text: #0f172a;
      --text-secondary: #334155;
      --muted: #64748b;
      --red: #ef4444;
      --amber: #f59e0b;
      --green: #10b981;
      --blue: #2563eb;
      --indigo: #4f46e5;
      --purple: #7c3aed;
      --teal: #0d9488;
      --shadow-sm: 0 1px 3px 0 rgba(15, 23, 42, 0.06), 0 1px 2px -1px rgba(15, 23, 42, 0.04);
      --shadow-md: 0 4px 14px -2px rgba(15, 23, 42, 0.08), 0 2px 6px -2px rgba(15, 23, 42, 0.04);
      --shadow-lg: 0 12px 28px -4px rgba(15, 23, 42, 0.12), 0 4px 10px -2px rgba(15, 23, 42, 0.06);
      --radius-sm: 8px;
      --radius-md: 12px;
      --radius-lg: 16px;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      font-family: 'Inter', system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      -webkit-tap-highlight-color: transparent;
    }

    html, body {
      background-color: var(--bg);
      background-image: var(--bg-mesh);
      background-attachment: fixed;
      color: var(--text);
      width: 100%;
      max-width: 100vw;
      overflow-x: hidden;
      line-height: 1.5;
      min-height: 100vh;
    }

    /* Sticky Top SaaS Navbar */
    .app-navbar {
      position: sticky;
      top: 0;
      z-index: 100;
      background: rgba(255, 255, 255, 0.94);
      backdrop-filter: blur(14px);
      -webkit-backdrop-filter: blur(14px);
      border-bottom: 1px solid var(--border);
      padding: 10px 14px;
      box-shadow: 0 1px 8px rgba(15, 23, 42, 0.05);
    }

    @media (min-width: 768px) {
      .app-navbar { padding: 12px 28px; }
    }

    .navbar-inner {
      max-width: 1280px;
      margin: 0 auto;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 12px;
    }

    .brand-row {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }

    .brand-logo {
      width: 36px;
      height: 36px;
      border-radius: 10px;
      background: linear-gradient(135deg, #2563eb 0%, #10b981 100%);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 1.15rem;
      font-weight: 900;
      color: #fff;
      box-shadow: 0 2px 8px rgba(37, 99, 235, 0.3);
      flex-shrink: 0;
    }

    .brand-text-wrap {
      display: flex;
      flex-direction: column;
    }

    .brand-title-line {
      display: flex;
      align-items: center;
      gap: 8px;
    }

    h1 {
      font-size: 1.18rem;
      font-weight: 800;
      letter-spacing: -0.025em;
      color: #0f172a;
      line-height: 1.1;
    }

    @media (min-width: 768px) {
      h1 { font-size: 1.35rem; }
    }

    .badge-bf {
      background: linear-gradient(90deg, #ef4444 50%, #10b981 50%);
      color: #fff;
      padding: 2px 8px;
      border-radius: 5px;
      font-size: 0.65rem;
      font-weight: 800;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      box-shadow: 0 1px 3px rgba(0,0,0,0.15);
    }

    .brand-sub {
      font-size: 0.72rem;
      color: var(--muted);
      margin-top: 2px;
    }

    /* Main App View Navigation Pills ("Rentrer dans d'autres endroits") */
    .app-nav-pills {
      display: flex;
      align-items: center;
      gap: 6px;
      background: #f1f5f9;
      padding: 4px;
      border-radius: 10px;
      border: 1px solid var(--border);
      overflow-x: auto;
      max-width: 100%;
      scrollbar-width: none;
    }
    .app-nav-pills::-webkit-scrollbar { display: none; }

    .nav-pill {
      background: transparent;
      color: var(--muted);
      border: 1px solid transparent;
      padding: 7px 12px;
      border-radius: 7px;
      font-size: 0.78rem;
      font-weight: 700;
      cursor: pointer;
      white-space: nowrap;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.18s ease;
    }

    .nav-pill:hover {
      color: var(--text);
      background: #e2e8f0;
    }

    .nav-pill.active {
      background: #ffffff;
      color: #2563eb;
      border-color: #cbd5e1;
      box-shadow: 0 1px 4px rgba(15, 23, 42, 0.08);
    }

    .nav-pill-count {
      background: #eff6ff;
      color: #2563eb;
      padding: 1px 6px;
      border-radius: 999px;
      font-size: 0.68rem;
      font-weight: 800;
    }

    .container {
      width: 100%;
      max-width: 1280px;
      margin: 0 auto;
      padding: 16px 12px 64px;
    }

    @media (min-width: 768px) {
      .container { padding: 22px 28px 80px; }
    }

    /* Section Header Component for clear visual separation */
    .section-block {
      margin-bottom: 20px;
    }

    .section-title-bar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 10px;
      padding: 0 2px;
    }

    .section-heading {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 0.85rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text);
    }

    .section-step-badge {
      background: #eff6ff;
      border: 1px solid #bfdbfe;
      color: #2563eb;
      font-size: 0.7rem;
      font-weight: 800;
      padding: 2px 7px;
      border-radius: 6px;
    }

    .section-hint {
      font-size: 0.76rem;
      color: var(--muted);
    }

    /* Mode Switcher Tabs (Acquisition Tools) */
    .tool-tabs {
      display: grid;
      grid-template-columns: 1fr 1fr auto;
      gap: 8px;
      margin-bottom: 10px;
      background: #ffffff;
      padding: 6px;
      border-radius: var(--radius-md);
      border: 1px solid var(--border);
      box-shadow: var(--shadow-sm);
    }

    .tab-btn {
      background: transparent;
      color: var(--muted);
      border: 1px solid transparent;
      padding: 10px 12px;
      border-radius: 9px;
      font-size: 0.84rem;
      font-weight: 700;
      cursor: pointer;
      text-align: center;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      min-height: 44px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      transition: all 0.18s ease;
    }

    .tab-btn:hover {
      color: var(--text);
      background: #f8fafc;
    }

    .tab-btn.active {
      background: #f1f5f9;
      color: #0f172a;
      border: 1px solid #cbd5e1;
      box-shadow: var(--shadow-sm);
    }

    .panel {
      display: none;
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      padding: 16px;
      margin-bottom: 16px;
      box-shadow: var(--shadow-sm);
      position: relative;
      overflow: hidden;
    }

    .panel::before {
      content: "";
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 3px;
      background: linear-gradient(90deg, var(--blue), var(--green));
    }

    .panel.open {
      display: block;
    }

    .panel h2 {
      font-size: 1rem;
      font-weight: 800;
      margin-bottom: 4px;
      color: #0f172a;
    }

    .panel p {
      font-size: 0.8rem;
      color: var(--muted);
      margin-bottom: 14px;
    }

    .form-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 10px;
      margin-bottom: 12px;
    }

    @media (min-width: 640px) {
      .form-grid { grid-template-columns: repeat(2, 1fr); }
    }

    @media (min-width: 960px) {
      #panelScan .form-grid { grid-template-columns: 1.2fr 1.5fr 1.1fr 1fr; margin-bottom: 0; }
    }

    .field-wrap {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }

    .field-label {
      font-size: 0.7rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--muted);
      padding-left: 2px;
    }

    /* 16px font-size prevents iOS/Android auto-zoom on focus */
    input, select, textarea {
      width: 100%;
      max-width: 100%;
      background: #ffffff;
      color: var(--text);
      border: 1px solid var(--border);
      padding: 10px 12px;
      border-radius: 9px;
      font-size: 16px;
      min-height: 44px;
      overflow: hidden;
      text-overflow: ellipsis;
      transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }

    input:focus, select:focus, textarea:focus {
      outline: none;
      border-color: var(--blue);
      box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.15);
    }

    @media (min-width: 768px) {
      input, select, textarea { font-size: 0.86rem; }
    }

    .btn {
      text-decoration: none;
      text-align: center;
      padding: 10px 14px;
      border-radius: 9px;
      font-weight: 700;
      font-size: 0.84rem;
      cursor: pointer;
      border: 1px solid transparent;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 7px;
      min-height: 44px;
      width: 100%;
      touch-action: manipulation;
      transition: all 0.16s ease;
    }

    .btn:hover:not(:disabled) {
      transform: translateY(-1px);
      filter: brightness(1.05);
    }

    .btn:active:not(:disabled) {
      transform: translateY(0);
    }

    .btn-primary {
      background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%);
      color: #fff;
      box-shadow: 0 2px 8px rgba(37, 99, 235, 0.25);
    }
    .btn-amber {
      background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%);
      color: #fff;
      box-shadow: 0 2px 8px rgba(245, 158, 11, 0.25);
    }
    .btn-reset {
      background: #f8fafc;
      color: #334155;
      border: 1px solid var(--border);
    }
    .btn-reset:hover {
      background: #f1f5f9;
      color: #0f172a;
      border-color: #cbd5e1;
    }
    .btn:disabled { opacity: 0.55; cursor: wait; }

    /* Interactive KPI Dashboard Cards */
    .kpi-strip {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 10px;
      margin-bottom: 18px;
    }

    @media (min-width: 640px) {
      .kpi-strip {
        grid-template-columns: repeat(3, 1fr);
      }
    }

    @media (min-width: 960px) {
      .kpi-strip {
        grid-template-columns: repeat(5, 1fr);
      }
    }

    .kpi {
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      padding: 12px 14px;
      cursor: pointer;
      user-select: none;
      text-align: left;
      position: relative;
      overflow: hidden;
      transition: all 0.18s ease;
      box-shadow: var(--shadow-sm);
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      min-height: 78px;
    }

    .kpi:hover {
      transform: translateY(-2px);
      border-color: #cbd5e1;
      box-shadow: var(--shadow-md);
    }

    .kpi.active-kpi {
      border-color: var(--blue);
      background: #eff6ff;
      box-shadow: 0 0 0 1px var(--blue), var(--shadow-sm);
    }

    .kpi-top-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 6px;
    }

    .kpi-val {
      font-size: 1.55rem;
      font-weight: 900;
      line-height: 1.1;
      letter-spacing: -0.02em;
      color: #0f172a;
    }

    .kpi-tag {
      font-size: 0.65rem;
      font-weight: 700;
      padding: 2px 6px;
      border-radius: 999px;
      background: #f1f5f9;
      color: var(--muted);
    }

    .kpi-label {
      color: #475569;
      font-size: 0.78rem;
      font-weight: 600;
      margin-top: 6px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    /* Responsive Filter Panel */
    .filters-box {
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      padding: 14px;
      margin-bottom: 14px;
      box-shadow: var(--shadow-sm);
    }

    .search-row {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 10px;
      margin-bottom: 10px;
    }

    .search-row .btn-reset {
      width: auto;
      padding: 0 16px;
      white-space: nowrap;
      font-size: 0.82rem;
    }

    .selects-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 8px;
    }

    @media (min-width: 600px) {
      .selects-grid {
        grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      }
    }

    /* Unified Filter Chips Bar (SaaS Clean Single-Panel Design) */
    .unified-chips-bar {
      margin-top: 10px;
      padding-top: 10px;
      border-top: 1px dashed var(--border);
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
    }

    .chips-subgroup {
      display: inline-flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 6px;
    }

    .chips-mini-label {
      font-size: 0.70rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: #94a3b8;
      margin-right: 2px;
      user-select: none;
    }

    .filter-chip {
      appearance: none;
      -webkit-appearance: none;
      background: #ffffff;
      border: 1px solid #cbd5e1;
      border-radius: 9999px;
      padding: 4px 10px;
      font-size: 0.74rem;
      font-weight: 600;
      color: #334155;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 5px;
      transition: all 0.15s ease-in-out;
      user-select: none;
      white-space: nowrap;
      line-height: 1.2;
    }

    .filter-chip:hover {
      border-color: #94a3b8;
      background: #f8fafc;
      transform: translateY(-1px);
    }

    .filter-chip .chip-cnt {
      font-size: 0.68rem;
      font-weight: 700;
      padding: 1px 5px;
      border-radius: 9999px;
      background: #f1f5f9;
      color: #64748b;
    }

    .filter-chip.active {
      font-weight: 700;
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
    }

    .filter-chip.active .chip-cnt {
      background: rgba(0, 0, 0, 0.08);
    }

    .filter-chip.chip-web.active {
      background: #eff6ff;
      border-color: #3b82f6;
      color: #1d4ed8;
    }
    .filter-chip.chip-web.active .chip-cnt {
      background: #dbeafe;
      color: #1e40af;
    }

    .filter-chip.chip-dead.active {
      background: #fef2f2;
      border-color: #ef4444;
      color: #b91c1c;
    }
    .filter-chip.chip-dead.active .chip-cnt {
      background: #fee2e2;
      color: #991b1b;
    }

    .filter-chip.chip-weak.active {
      background: #fffbeb;
      border-color: #f59e0b;
      color: #b45309;
    }
    .filter-chip.chip-weak.active .chip-cnt {
      background: #fef3c7;
      color: #92400e;
    }

    .filter-chip.chip-ok.active {
      background: #ecfdf5;
      border-color: #10b981;
      color: #047857;
    }
    .filter-chip.chip-ok.active .chip-cnt {
      background: #d1fae5;
      color: #065f46;
    }

    .filter-chip.chip-fb.active {
      background: #faf5ff;
      border-color: #8b5cf6;
      color: #6d28d9;
    }
    .filter-chip.chip-fb.active .chip-cnt {
      background: #ede9fe;
      color: #5b21b6;
    }

    .filter-chip.chip-wa.active {
      background: #ecfdf5;
      border-color: #10b981;
      color: #047857;
    }
    .filter-chip.chip-wa.active .chip-cnt {
      background: #d1fae5;
      color: #065f46;
    }

    .filter-chip.chip-fixe.active {
      background: #f1f5f9;
      border-color: #64748b;
      color: #1e293b;
    }
    .filter-chip.chip-fixe.active .chip-cnt {
      background: #e2e8f0;
      color: #0f172a;
    }

    .filter-chip.chip-track.active {
      background: #fff1f2;
      border-color: #f43f5e;
      color: #be123c;
    }
    .filter-chip.chip-track.active .chip-cnt {
      background: #ffe4e6;
      color: #9f1239;
    }

    .active-filters-banner {
      display: none;
      align-items: center;
      justify-content: space-between;
      background: #f0fdf4;
      border: 1px solid #bbf7d0;
      border-radius: 8px;
      padding: 6px 12px;
      font-size: 0.76rem;
      color: #15803d;
      font-weight: 600;
      margin-top: 8px;
      flex-wrap: wrap;
      gap: 6px;
    }

    .filter-summary {
      font-size: 0.82rem;
      color: #475569;
      margin-bottom: 14px;
      padding: 10px 14px;
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: 10px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
    }

    /* Redesigned SaaS Lead Cards with Clear Visual Zones */
    .lead-list {
      display: grid;
      grid-template-columns: 1fr;
      gap: 14px;
    }

    .lead-card {
      background: #ffffff;
      border: 1px solid var(--border);
      border-left: 4px solid var(--blue);
      border-radius: var(--radius-md);
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 12px;
      width: 100%;
      overflow: hidden;
      box-shadow: var(--shadow-sm);
      transition: border-color 0.18s ease, box-shadow 0.18s ease, transform 0.18s ease;
    }

    .lead-card:hover {
      border-color: #cbd5e1;
      box-shadow: var(--shadow-md);
      transform: translateY(-1px);
    }

    .lead-card.accent-SITE_MORT { border-left-color: var(--red); }
    .lead-card.accent-SITE_FAIBLE { border-left-color: var(--amber); }
    .lead-card.accent-FACEBOOK_SEUL { border-left-color: var(--purple); }
    .lead-card.accent-SANS_SITE { border-left-color: var(--blue); }
    .lead-card.accent-SITE_OK { border-left-color: var(--green); }

    @media (min-width: 920px) {
      .lead-card {
        display: grid;
        grid-template-columns: 1fr 220px;
        align-items: stretch;
        padding: 18px 20px;
        gap: 20px;
      }
    }

    .card-top {
      display: flex;
      align-items: flex-start;
      gap: 12px;
    }

    .score-badge {
      flex: 0 0 54px;
      height: 54px;
      background: #f8fafc;
      border: 2px solid var(--border);
      border-radius: 12px;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      transition: transform 0.15s ease, border-color 0.15s ease;
    }

    .score-badge:hover {
      transform: scale(1.05);
      border-color: var(--blue);
    }

    .score-num {
      font-size: 1.15rem;
      font-weight: 900;
      line-height: 1;
    }

    .score-lbl {
      font-size: 0.56rem;
      font-weight: 700;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
      margin-top: 3px;
    }

    .title-wrap {
      flex: 1;
      min-width: 0;
    }

    .biz-name-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 6px;
    }

    .biz-name {
      font-size: 1.08rem;
      font-weight: 800;
      color: #0f172a;
      word-break: break-word;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: color 0.15s ease;
    }

    .biz-name:hover {
      color: var(--blue);
      text-decoration: underline;
    }

    .btn-open-detail {
      background: #eff6ff;
      border: 1px solid #bfdbfe;
      color: #1d4ed8;
      font-size: 0.72rem;
      font-weight: 700;
      padding: 4px 10px;
      border-radius: 7px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      transition: all 0.15s ease;
      white-space: nowrap;
    }

    .btn-open-detail:hover {
      background: var(--blue);
      color: #fff;
      border-color: var(--blue);
    }

    .badges-row {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
    }

    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 3px 9px;
      border-radius: 999px;
      font-size: 0.7rem;
      font-weight: 800;
      letter-spacing: 0.02em;
    }

    .st-SITE_MORT { background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; }
    .st-SITE_FAIBLE { background: #fffbeb; color: #b45309; border: 1px solid #fde68a; }
    .st-FACEBOOK_SEUL { background: #faf5ff; color: #6d28d9; border: 1px solid #e9d5ff; }
    .st-SANS_SITE { background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; }
    .st-SITE_OK { background: #ecfdf5; color: #047857; border: 1px solid #a7f3d0; }

    .phone-badge {
      font-size: 0.7rem;
      padding: 3px 8px;
      border-radius: 6px;
      font-weight: 700;
    }
    .ph-MOBILE_WHATSAPP { background: #ecfdf5; color: #047857; border: 1px solid #a7f3d0; }
    .ph-FIXE_ONATEL { background: #f1f5f9; color: #475569; border: 1px solid #cbd5e1; }

    /* Distinct Info Chips Bar inside Lead Card */
    .meta-chips {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(175px, 1fr));
      gap: 8px;
      font-size: 0.8rem;
      color: #334155;
      margin-top: 10px;
      background: #f8fafc;
      border: 1px solid var(--border);
      padding: 8px 12px;
      border-radius: 8px;
    }

    .meta-item {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      max-width: 100%;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .meta-item a {
      color: #2563eb;
      font-weight: 600;
      text-decoration: none;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .meta-item a:hover {
      text-decoration: underline;
    }

    .issues {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 8px;
    }

    .issue-tag {
      background: #fff1f2;
      border: 1px solid #fecdd3;
      color: #be123c;
      font-size: 0.73rem;
      font-weight: 600;
      padding: 3px 8px;
      border-radius: 6px;
      line-height: 1.3;
    }

    .pitch-box {
      background: #fffbeb;
      border: 1px solid #fde68a;
      border-left: 3px solid #f59e0b;
      padding: 10px 12px;
      font-size: 0.8rem;
      color: #78350f;
      border-radius: 0 8px 8px 0;
      margin-top: 10px;
      overflow-wrap: anywhere;
      line-height: 1.45;
    }

    /* Action Buttons Column */
    .actions {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
      gap: 8px;
      width: 100%;
      align-content: start;
    }

    @media (min-width: 920px) {
      .actions {
        grid-template-columns: 1fr;
        border-left: 1px solid var(--border);
        padding-left: 18px;
        justify-content: center;
      }
    }

    .btn-wa {
      background: linear-gradient(135deg, #10b981 0%, #059669 100%);
      color: #fff;
      box-shadow: 0 2px 8px rgba(16, 185, 129, 0.25);
    }
    .btn-fb {
      background: #1877f2;
      color: #ffffff;
    }
    .btn-call {
      background: #2563eb;
      color: #ffffff;
    }
    .btn-maps {
      background: #f0fdfa;
      color: #0f766e;
      border: 1px solid #99f6e4;
    }
    .btn-maps:hover {
      background: #ccfbf1;
      color: #115e59;
    }
    .btn-copy {
      background: #f8fafc;
      color: #334155;
      border: 1px solid var(--border);
    }
    .btn-copy:hover {
      background: #f1f5f9;
      color: #0f172a;
    }

    .empty-box {
      text-align: center;
      padding: 36px 18px;
      background: #ffffff;
      border: 1px dashed #cbd5e1;
      border-radius: var(--radius-md);
      color: var(--muted);
    }

    #statusBanner {
      display: none;
      margin-bottom: 14px;
      padding: 12px 16px;
      border-radius: 10px;
      background: #f0fdf4;
      border: 1px solid #bbf7d0;
      color: #166534;
      font-size: 0.84rem;
      font-weight: 700;
      box-shadow: var(--shadow-sm);
    }

    /* Offer Selector Card (R3) */
    .offer-selector-card {
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      padding: 14px 16px;
      margin-bottom: 16px;
      box-shadow: var(--shadow-sm);
    }

    .offer-header {
      display: flex;
      flex-direction: column;
      gap: 4px;
      margin-bottom: 12px;
    }

    @media (min-width: 600px) {
      .offer-header {
        flex-direction: row;
        align-items: center;
        justify-content: space-between;
        gap: 10px;
      }
    }

    .offer-badge-title {
      font-size: 0.76rem;
      font-weight: 800;
      color: #d97706;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }

    .offer-subtitle {
      font-size: 0.78rem;
      color: var(--muted);
    }

    .offer-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 10px;
    }

    @media (min-width: 680px) {
      .offer-grid {
        grid-template-columns: repeat(3, 1fr);
      }
    }

    .offer-card-btn {
      background: #ffffff;
      border: 1px solid var(--border);
      color: var(--text);
      padding: 12px 14px;
      border-radius: 10px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 12px;
      text-align: left;
      transition: all 0.18s ease;
      touch-action: manipulation;
      min-height: 56px;
      position: relative;
    }

    .offer-card-btn:hover {
      border-color: #cbd5e1;
      background: #f8fafc;
      transform: translateY(-1px);
    }

    .offer-card-btn.active {
      background: #eff6ff;
      border-color: var(--blue);
      box-shadow: 0 0 0 1px var(--blue), var(--shadow-sm);
    }

    .offer-icon {
      font-size: 1.35rem;
      width: 38px;
      height: 38px;
      border-radius: 9px;
      background: #f1f5f9;
      display: flex;
      align-items: center;
      justify-content: center;
      flex: 0 0 auto;
    }

    .offer-btn-text {
      display: flex;
      flex-direction: column;
      min-width: 0;
      flex: 1;
    }

    .offer-name {
      font-size: 0.86rem;
      font-weight: 800;
      color: #0f172a;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .offer-desc {
      font-size: 0.72rem;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      margin-top: 2px;
    }

    /* 6-Stage Segmented Mini-CRM Control & Progressive Pipeline Controls */
    .crm-bar {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 6px;
      background: #f8fafc;
      padding: 6px;
      border-radius: 10px;
      border: 1px solid var(--border);
      margin-top: 10px;
    }

    @media (min-width: 640px) {
      .crm-bar {
        grid-template-columns: repeat(3, 1fr);
      }
    }

    @media (min-width: 960px) {
      .crm-bar {
        grid-template-columns: repeat(6, 1fr);
      }
    }

    .crm-pill {
      background: transparent;
      border: 1px solid transparent;
      color: var(--muted);
      font-size: 0.73rem;
      font-weight: 700;
      padding: 8px 6px;
      border-radius: 7px;
      cursor: pointer;
      text-align: center;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      transition: all 0.15s ease-in-out;
      touch-action: manipulation;
      min-height: 40px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 4px;
    }

    .crm-pill:hover {
      background: #e2e8f0;
      color: #0f172a;
    }

    .crm-pill.crm-a-contacter.active {
      background: #eff6ff;
      border-color: var(--blue);
      color: #1d4ed8;
      box-shadow: 0 0 6px rgba(37, 99, 235, 0.2);
    }

    .crm-pill.crm-contacte.active {
      background: #fffbeb;
      border-color: var(--amber);
      color: #b45309;
      box-shadow: 0 0 6px rgba(245, 158, 11, 0.2);
    }

    .crm-pill.crm-client.active {
      background: #faf5ff;
      border-color: var(--purple);
      color: #7c3aed;
      box-shadow: 0 0 6px rgba(124, 58, 237, 0.2);
    }

    .crm-pill.crm-livre.active {
      background: #f0fdfa;
      border-color: var(--teal);
      color: #0f766e;
      box-shadow: 0 0 6px rgba(13, 148, 136, 0.2);
    }

    .crm-pill.crm-paye.active {
      background: #ecfdf5;
      border-color: var(--green);
      color: #047857;
      box-shadow: 0 0 6px rgba(16, 185, 129, 0.2);
    }

    .crm-pill.crm-ignore.active {
      background: #fef2f2;
      border-color: var(--red);
      color: #b91c1c;
    }

    .lead-card.crm-state-IGNORE,
    .lead-card.crm-state-ECHEC {
      opacity: 0.6;
      border-color: #cbd5e1;
    }

    /* Single Primary Button on Untriaged Scan Cards ("Mettre dans À contacter") */
    .scan-triage-bar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-top: 10px;
      padding: 10px 12px;
      border-radius: 10px;
      background: #f8fafc;
      border: 1px dashed #cbd5e1;
    }

    .btn-add-pipeline {
      background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%);
      color: #ffffff;
      border: 1px solid #2563eb;
      padding: 10px 16px;
      border-radius: 9px;
      font-size: 0.85rem;
      font-weight: 800;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      min-height: 44px;
      box-shadow: 0 2px 8px rgba(37, 99, 235, 0.25);
      transition: all 0.16s ease;
      flex: 1 1 220px;
    }

    .btn-add-pipeline:hover {
      transform: translateY(-1px);
      filter: brightness(1.08);
      box-shadow: 0 4px 12px rgba(37, 99, 235, 0.35);
    }

    /* Visual Memory Banner on Scan Cards when lead is ALREADY in the user's Pipeline */
    .pipeline-memory-banner {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 10px 14px;
      border-radius: 10px;
      font-size: 0.8rem;
      font-weight: 800;
      margin-top: 8px;
      border: 1px solid var(--border);
    }

    .mem-A_CONTACTER {
      background: #eff6ff;
      border-color: #bfdbfe;
      color: #1e40af;
    }
    .mem-CONTACTE {
      background: #fffbeb;
      border-color: #fde68a;
      color: #92400e;
    }
    .mem-RDV, .mem-CLIENT {
      background: #faf5ff;
      border-color: #e9d5ff;
      color: #6b21a8;
    }
    .mem-LIVRE {
      background: #f0fdfa;
      border-color: #99f6e4;
      color: #115e59;
    }
    .mem-PAYE {
      background: #ecfdf5;
      border-color: #a7f3d0;
      color: #065f46;
    }
    .mem-ECHEC, .mem-IGNORE {
      background: #fef2f2;
      border-color: #fecaca;
      color: #991b1b;
    }

    /* Progressive Pipeline Stage Decision Box inside "Mon Pipeline Commercial" */
    .pipeline-stage-box {
      margin-top: 10px;
      padding: 12px 14px;
      border-radius: 10px;
      background: #f8fafc;
      border: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      gap: 10px;
    }

    .pipeline-stage-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
      font-size: 0.78rem;
      font-weight: 800;
      color: #334155;
    }

    .pipeline-next-btns {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .btn-stage-action {
      padding: 9px 14px;
      border-radius: 8px;
      font-size: 0.8rem;
      font-weight: 800;
      cursor: pointer;
      border: 1px solid transparent;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      min-height: 42px;
      transition: all 0.15s ease;
      flex: 1 1 auto;
    }

    .btn-stage-action:hover {
      transform: translateY(-1px);
      filter: brightness(1.05);
    }

    .btn-stage-contacte {
      background: #fffbeb;
      border-color: #fde68a;
      color: #b45309;
    }
    .btn-stage-rdv {
      background: linear-gradient(135deg, #7c3aed 0%, #6d28d9 100%);
      border-color: #7c3aed;
      color: #fff;
    }
    .btn-stage-rdv-check {
      background: #faf5ff;
      border: 2px solid #c084fc;
      color: #7c3aed;
    }
    .btn-stage-rdv-check.done {
      background: #ecfdf5;
      border-color: #10b981;
      color: #047857;
    }
    .btn-stage-livre {
      background: linear-gradient(135deg, #0d9488 0%, #0f766e 100%);
      border-color: #0d9488;
      color: #fff;
    }
    .btn-stage-paye {
      background: linear-gradient(135deg, #10b981 0%, #059669 100%);
      border-color: #10b981;
      color: #fff;
      box-shadow: 0 2px 8px rgba(16, 185, 129, 0.25);
    }
    .btn-stage-echec {
      background: #fef2f2;
      border-color: #fecaca;
      color: #b91c1c;
    }

    /* Inline Deal Amount (FCFA) & Notes Bar on Pipeline Cards */
    .pipeline-meta-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 8px;
      margin-top: 8px;
      padding-top: 8px;
      border-top: 1px solid var(--border);
    }

    @media (min-width: 640px) {
      .pipeline-meta-grid {
        grid-template-columns: 190px 1fr;
      }
    }

    .meta-input-group {
      display: flex;
      flex-direction: column;
      gap: 3px;
    }

    .meta-input-group label {
      font-size: 0.68rem;
      font-weight: 700;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.03em;
    }

    .meta-input-group input {
      min-height: 38px;
      padding: 6px 10px;
      font-size: 0.82rem;
      background: #ffffff;
      border: 1px solid var(--border);
      color: #0f172a;
    }

    /* Financial Summary Banner in Mon Pipeline Commercial */
    .pipeline-finance-banner {
      display: none;
      grid-template-columns: 1fr;
      gap: 10px;
      margin-bottom: 16px;
    }

    @media (min-width: 680px) {
      .pipeline-finance-banner {
        grid-template-columns: repeat(3, 1fr);
      }
    }

    .fin-card {
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      padding: 14px 16px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      box-shadow: var(--shadow-sm);
    }

    .fin-card.fin-paid { border-left: 4px solid var(--green); }
    .fin-card.fin-pending { border-left: 4px solid var(--teal); }
    .fin-card.fin-active { border-left: 4px solid var(--blue); }

    .fin-title {
      font-size: 0.74rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--muted);
    }

    .fin-amount {
      font-size: 1.4rem;
      font-weight: 900;
      margin-top: 4px;
      color: #0f172a;
    }

    .fin-sub {
      font-size: 0.74rem;
      color: #64748b;
      margin-top: 4px;
    }

    /* Interactive 6-Stage CRM Pipeline Board */
    .crm-pipeline-strip {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 8px;
      margin-bottom: 16px;
    }

    @media (min-width: 640px) {
      .crm-pipeline-strip { grid-template-columns: repeat(4, 1fr); }
    }

    @media (min-width: 1024px) {
      .crm-pipeline-strip { grid-template-columns: repeat(7, 1fr); }
    }

    .crm-stage-card {
      background: #ffffff;
      border: 1px solid var(--border);
      border-top: 3px solid #cbd5e1;
      border-radius: var(--radius-md);
      padding: 10px 12px;
      cursor: pointer;
      user-select: none;
      transition: all 0.16s ease;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      min-height: 68px;
      box-shadow: var(--shadow-sm);
    }

    .crm-stage-card:hover {
      transform: translateY(-2px);
      border-color: #94a3b8;
      box-shadow: var(--shadow-md);
    }

    .crm-stage-card.active-crm-stage {
      border-color: var(--blue);
      background: #eff6ff;
      box-shadow: 0 0 0 1px var(--blue), var(--shadow-sm);
    }

    /* One-Tap Hide Known Leads Toggle Bar */
    .radar-memory-toggle-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 10px;
      padding-top: 10px;
      border-top: 1px solid var(--border);
    }

    .toggle-known-label {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 0.8rem;
      font-weight: 700;
      color: #334155;
      cursor: pointer;
      user-select: none;
      padding: 6px 12px;
      border-radius: 8px;
      background: #eff6ff;
      border: 1px solid #bfdbfe;
      transition: all 0.15s ease;
    }

    .toggle-known-label:hover {
      background: #dbeafe;
    }

    .toggle-known-label input[type="checkbox"] {
      width: 18px;
      height: 18px;
      min-height: 18px;
      margin: 0;
      accent-color: var(--blue);
      cursor: pointer;
    }

    .workspace-context-bar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
      background: #eff6ff;
      border: 1px solid #bfdbfe;
      border-left: 4px solid var(--blue);
      border-radius: 10px;
      padding: 10px 14px;
      margin-bottom: 16px;
      font-size: 0.8rem;
      color: #1e40af;
    }

    /* Tracking Badges (R4) */
    .tag-pixel { background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; }
    .tag-analytics { background: #fffbeb; color: #b45309; border: 1px solid #fde68a; }

    /* Interactive Slide-Over Lead Detail Modal ("Rentrer dans la fiche d'une entreprise") */
    .modal-backdrop {
      position: fixed;
      inset: 0;
      z-index: 250;
      background: rgba(15, 23, 42, 0.45);
      backdrop-filter: blur(4px);
      -webkit-backdrop-filter: blur(4px);
      display: none;
      justify-content: flex-end;
      align-items: stretch;
    }

    .modal-backdrop.open {
      display: flex;
    }

    .detail-drawer {
      width: 100%;
      max-width: 620px;
      background: #ffffff;
      border-left: 1px solid var(--border);
      box-shadow: var(--shadow-lg);
      display: flex;
      flex-direction: column;
      height: 100%;
      overflow-y: auto;
      padding: 20px 18px 40px;
      animation: slideInRight 0.2s ease-out;
    }

    @keyframes slideInRight {
      from { transform: translateX(40px); opacity: 0; }
      to { transform: translateX(0); opacity: 1; }
    }

    .drawer-header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      padding-bottom: 14px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 16px;
    }

    .drawer-close-btn {
      background: #f1f5f9;
      border: 1px solid var(--border);
      color: #334155;
      width: 42px;
      height: 42px;
      border-radius: 9px;
      font-size: 1rem;
      font-weight: 800;
      cursor: pointer;
      flex-shrink: 0;
    }
    .drawer-close-btn:hover {
      background: #e2e8f0;
      color: #0f172a;
    }

    .drawer-section {
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 14px;
      margin-bottom: 14px;
    }

    .drawer-section-title {
      font-size: 0.75rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: #475569;
      margin-bottom: 10px;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }

    .diag-grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 8px;
    }

    .diag-item {
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 9px 10px;
    }

    .diag-lbl {
      font-size: 0.68rem;
      color: var(--muted);
      display: block;
    }

    .diag-val {
      font-size: 0.84rem;
      font-weight: 700;
      color: #0f172a;
      margin-top: 2px;
      display: block;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    /* Pitch Studio Controls & Psychological Pills */
    .angle-pills-grid {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 6px;
      margin-bottom: 12px;
    }

    .angle-pill-btn {
      background: #ffffff;
      border: 1px solid var(--border);
      color: #475569;
      border-radius: 6px;
      padding: 6px 4px;
      font-size: 0.72rem;
      font-weight: 700;
      cursor: pointer;
      text-align: center;
      transition: all 0.15s ease;
      user-select: none;
    }

    .angle-pill-btn:hover {
      background: #f1f5f9;
      color: #0f172a;
      border-color: #cbd5e1;
    }

    .angle-pill-btn.active-angle {
      background: #eff6ff;
      border-color: var(--blue);
      color: #1d4ed8;
      box-shadow: 0 1px 4px rgba(37, 99, 235, 0.15);
    }

    .stage-tabs-row {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 6px;
      margin-bottom: 12px;
    }

    .stage-tab-btn {
      background: #ffffff;
      border: 1px solid var(--border);
      color: #475569;
      border-radius: 6px;
      padding: 6px 4px;
      font-size: 0.70rem;
      font-weight: 800;
      cursor: pointer;
      text-align: center;
      transition: all 0.15s ease;
      user-select: none;
    }

    .stage-tab-btn:hover {
      background: #f1f5f9;
      color: #0f172a;
      border-color: #cbd5e1;
    }

    .stage-tab-btn.active-stage {
      background: #ecfdf5;
      border-color: var(--green);
      color: #047857;
    }

    .copy-strip-label {
      font-size: 0.68rem;
      font-weight: 700;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
      margin-bottom: 5px;
    }

    .pitch-stats-badge {
      font-size: 0.68rem;
      color: #1d4ed8;
      background: #eff6ff;
      padding: 2px 8px;
      border-radius: 4px;
      font-weight: 600;
    }

    /* Modern Floating Toast Notification System (Steve Jobs & Jony Ive) */
    .toast-container {
      position: fixed;
      bottom: 24px;
      right: 20px;
      z-index: 9999;
      display: flex;
      flex-direction: column;
      gap: 10px;
      pointer-events: none;
      max-width: calc(100vw - 40px);
    }

    .toast-item {
      background: #0f172a;
      color: #ffffff;
      padding: 12px 18px;
      border-radius: 10px;
      font-size: 0.82rem;
      font-weight: 600;
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.25), 0 8px 10px -6px rgba(0, 0, 0, 0.2);
      display: flex;
      align-items: center;
      gap: 10px;
      pointer-events: auto;
      animation: toastSlideUp 0.25s cubic-bezier(0.16, 1, 0.3, 1) forwards;
      max-width: 420px;
      border: 1px solid rgba(255, 255, 255, 0.1);
    }
    .toast-item.toast-success { border-left: 4px solid var(--green); }
    .toast-item.toast-error { border-left: 4px solid var(--red); }
    .toast-item.toast-info { border-left: 4px solid var(--blue); }

    @keyframes toastSlideUp {
      from { transform: translateY(20px); opacity: 0; }
      to { transform: translateY(0); opacity: 1; }
    }

    /* Executive Engineering Board Footer */
    .app-footer {
      margin-top: 40px;
      padding-top: 24px;
      border-top: 1px solid var(--border);
      color: var(--muted);
      font-size: 0.78rem;
    }

    .footer-inner {
      display: flex;
      flex-direction: column;
      gap: 14px;
      align-items: center;
      text-align: center;
    }

    @media (min-width: 768px) {
      .footer-inner {
        flex-direction: row;
        justify-content: space-between;
        text-align: left;
      }
    }

    .footer-board {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }

    .board-badge {
      font-size: 0.70rem;
      font-weight: 800;
      color: #2563eb;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    .board-names {
      font-size: 0.74rem;
      color: #64748b;
      line-height: 1.4;
    }

    .footer-shortcuts {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
      justify-content: center;
    }

    .shortcut-tag {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-size: 0.72rem;
      color: #475569;
      background: #f1f5f9;
      padding: 3px 8px;
      border-radius: 6px;
      border: 1px solid var(--border);
    }

    kbd {
      background: #ffffff;
      border: 1px solid #cbd5e1;
      border-radius: 4px;
      box-shadow: 0 1px 1px rgba(0,0,0,0.1);
      color: #0f172a;
      font-family: inherit;
      font-size: 0.68rem;
      font-weight: 700;
      padding: 1px 5px;
    }

    .quick-amount-chips {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-top: 6px;
    }

    .chip-btn-amount {
      background: #f1f5f9;
      border: 1px solid #cbd5e1;
      color: #334155;
      font-size: 0.70rem;
      font-weight: 700;
      padding: 2px 7px;
      border-radius: 6px;
      cursor: pointer;
      transition: all 0.15s ease;
    }

    .chip-btn-amount:hover {
      background: #eff6ff;
      border-color: #3b82f6;
      color: #1d4ed8;
      transform: translateY(-1px);
    }

    .chip-btn-amount.active {
      background: #2563eb;
      border-color: #1d4ed8;
      color: #ffffff;
      box-shadow: 0 1px 4px rgba(37, 99, 235, 0.3);
    }

    .center-dialog-box {
      width: 100%;
      max-width: 480px;
      margin: auto;
      background: #ffffff;
      border-radius: 12px;
      box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.2), 0 10px 10px -5px rgba(0, 0, 0, 0.1);
      padding: 22px 20px;
      border: 1px solid var(--border);
      max-height: 90vh;
      overflow-y: auto;
      animation: modalScaleIn 0.2s cubic-bezier(0.16, 1, 0.3, 1);
    }
    @keyframes modalScaleIn {
      from { transform: scale(0.95); opacity: 0; }
      to { transform: scale(1); opacity: 1; }
    }
    .badge-claimed {
      display: inline-flex;
      align-items: center;
      gap: 3px;
      font-size: 0.72rem;
      font-weight: 700;
      background: #fef3c7;
      color: #92400e;
      border: 1px solid #fcd34d;
      padding: 2px 8px;
      border-radius: 9999px;
      white-space: nowrap;
    }
    .user-auth-strip {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-left: auto;
    }
    @media (max-width: 640px) {
      .user-auth-strip {
        width: 100%;
        justify-content: flex-end;
        margin-top: 6px;
      }
    }
  </style>
</head>
<body>
  <!-- STICKY SAAS NAVBAR WITH WORKSPACE NAVIGATION -->
  <header class="app-navbar">
    <div class="navbar-inner">
      <div class="brand-row">
        <div class="brand-logo">BF</div>
        <div class="brand-text-wrap">
          <div class="brand-title-line">
            <h1>ProspectBF</h1>
            <span class="badge-bf">BURKINA B2B</span>
          </div>
          <span class="brand-sub">Radar d'Audit Digital & Pipeline Commercial B2B • 7 Villes • 16 Secteurs</span>
        </div>
      </div>

      <!-- Navigation Principale : Séparation claire entre Radar de Scan et Mon Pipeline Commercial -->
      <nav class="app-nav-pills" aria-label="Navigation principale">
        <button type="button" id="navView_scanner" class="nav-pill active" onclick="switchAppView('scanner')">
          🔍 Radar de Scan <span class="nav-pill-count" id="navCountAll">0</span>
        </button>
        <button type="button" id="navView_crm" class="nav-pill" onclick="switchAppView('crm')">
          📋 Mon Pipeline Commercial <span class="nav-pill-count" id="navCountCrm" style="background:#ecfdf5; color:#047857;">0</span>
        </button>
        <button type="button" id="navView_offers" class="nav-pill" onclick="switchAppView('offers')">
          🎯 Offres & Pitchs
        </button>
        <button type="button" id="navView_all" class="nav-pill" onclick="switchAppView('all')">
          🏠 Vue Globale
        </button>
      </nav>

      <!-- ESPACE UTILISATEUR & GESTION EQUIPE AGÈNCE -->
      <div class="user-auth-strip" id="userAuthStrip">
        <div id="userLoggedInBlock" style="display:none; align-items:center; gap:8px;">
          <span class="user-badge" id="navUserBadge" style="font-size:0.75rem; font-weight:700; color:#1e40af; background:#eff6ff; border:1px solid #bfdbfe; padding:4px 10px; border-radius:9999px;">👤 Direction</span>
          <button type="button" id="btnAdminDashboard" class="btn btn-reset" style="display:none; width:auto; min-height:30px; padding:3px 10px; font-size:0.74rem; font-weight:700; background:#fefce8; color:#a16207; border-color:#fde047;" onclick="openAdminModal()">📊 Vue Agence</button>
          <button type="button" class="btn btn-reset" style="width:auto; min-height:30px; padding:3px 8px; font-size:0.72rem; color:#64748b; border-color:#cbd5e1; background:#ffffff;" onclick="logoutUser()" title="Déconnexion">🚪 Sortir</button>
        </div>
        <div id="userLoggedOutBlock" style="display:flex; align-items:center; gap:6px;">
          <button type="button" class="btn btn-reset" style="width:auto; min-height:30px; padding:3px 10px; font-size:0.74rem; font-weight:700; color:#2563eb; border-color:#bfdbfe; background:#ffffff;" onclick="openAuthModal('login')">🔑 Connexion</button>
          <button type="button" class="btn btn-primary" style="width:auto; min-height:30px; padding:3px 10px; font-size:0.74rem; font-weight:700;" onclick="openAuthModal('register')">✨ Inscription</button>
        </div>
      </div>
    </div>
  </header>

  <div class="container">
    <div id="workspaceContextBar" class="workspace-context-bar">
      <span id="workspaceContextTitle"><strong>🔍 Espace Actif : Radar de Scan & Triage</strong> — Scannez une ville, repérez les opportunités et cliquez sur <strong>« ➕ Mettre dans À contacter »</strong> pour les envoyer dans votre Pipeline Commercial</span>
      <button type="button" id="btnQuickGoPipeline" class="btn btn-reset" style="width:auto; min-height:34px; padding:5px 12px; font-size:0.76rem; border-color:#bfdbfe; color:#2563eb; background:#ffffff;" onclick="switchAppView('crm')">📂 Ouvrir Mon Pipeline Commercial →</button>
    </div>

    <div id="statusBanner"></div>

    <!-- SECTION 1 : ACQUISITION & AUDIT EN TEMPS RÉEL -->
    <section id="sectionAcquisition" class="section-block">
      <div class="section-title-bar">
        <div class="section-heading">
          <span class="section-step-badge">ÉTAPE 1</span>
          <span>Radar de Scan & Audit Technique B2B</span>
        </div>
        <span class="section-hint">Scannez une ville entière ou testez une entreprise précise</span>
      </div>

      <div class="tool-tabs">
        <button id="tabScan" class="tab-btn active" onclick="switchTab('scan')">🔍 Scanner Ville / Secteur</button>
        <button id="tabSingle" class="tab-btn" onclick="switchTab('single')">⚡ Auditer un Site / Nom</button>
        <button id="tabHide" class="tab-btn" onclick="switchTab('none')" title="Réduire le panneau">🔼 Réduire</button>
      </div>

      <!-- PANEL SCAN VILLE/SECTEUR -->
      <div id="panelScan" class="panel open">
        <h2>🔍 Scanner une Ville & un Secteur B2B au Burkina Faso</h2>
        <p>Sélectionnez une localité et une catégorie d'activité pour auditer les entreprises en parallèle :</p>
        <div class="form-grid">
          <select id="scanCity">
            <option value="ouaga">📍 Ouagadougou</option>
            <option value="bobo">📍 Bobo-Dioulasso</option>
            <option value="koudougou">📍 Koudougou</option>
            <option value="banfora">📍 Banfora</option>
            <option value="ouahigouya">📍 Ouahigouya</option>
            <option value="tenkodogo">📍 Tenkodogo</option>
            <option value="kaya">📍 Kaya</option>
          </select>
          <select id="scanSector">
            <option value="all">🏢 Tous les 16 secteurs B2B</option>
            <option value="hotel">🏨 Hôtels & Résidences</option>
            <option value="sante">🏥 Cliniques, Cabinets & Labos</option>
            <option value="pharmacie">💊 Pharmacies & Dépôts</option>
            <option value="resto">🍽️ Restaurants & Cafés</option>
            <option value="btp">🏗️ BTP, Quincailleries & Solaire</option>
            <option value="auto">🚗 Auto, Location & Transport</option>
            <option value="commerce">🛒 Boutiques, Supermarchés & Mode</option>
            <option value="finance">🏦 Assurances, Finance & Cabinets</option>
            <option value="education">🎓 Universités & Formation</option>
            <option value="voyage">✈️ Agences Voyage & Immobilier</option>
            <option value="agri">🌾 Agriculture & Élevage</option>
            <option value="beaute">💄 Beauté & Cosmétique</option>
            <option value="industrie">🏭 Industrie & Usines</option>
            <option value="event">🎉 Événementiel & Loisirs</option>
            <option value="securite">🛡️ Sécurité & Gardiennage</option>
            <option value="entreprise">💼 Bureaux, Sociétés IT & ONG</option>
          </select>
          <select id="scanLimit">
            <option value="40">40 entreprises (~8 sec)</option>
            <option value="75" selected>75 entreprises (~14 sec)</option>
            <option value="110">110 entreprises (~20 sec)</option>
          </select>
          <button id="btnScanCity" class="btn btn-primary" onclick="runCityScan()">🔍 Lancer le scan</button>
        </div>
      </div>

      <!-- PANEL AUDIT DIRECT -->
      <div id="panelSingle" class="panel">
        <h2>⚡ Auditer une Entreprise, un Site Web ou une Page Facebook</h2>
        <p>Testez instantanément n'importe quel prospect repéré sur Google Maps, Facebook ou sur le terrain :</p>
        <div class="form-grid">
          <input type="text" id="customName" placeholder="Nom (ex: Clinique Suka...)" />
          <select id="customSector">
            <option value="hotel">🏨 Hôtels & Résidences</option>
            <option value="sante">🏥 Cliniques & Santé</option>
            <option value="pharmacie">💊 Pharmacies</option>
            <option value="resto">🍽️ Restaurants & Maquis</option>
            <option value="btp">🏗️ BTP & Quincailleries</option>
            <option value="auto">🚗 Auto & Transport</option>
            <option value="commerce">🛒 Commerce & Boutiques</option>
            <option value="finance">🏦 Assurances & Cabinets</option>
            <option value="education">🎓 Universités & Écoles</option>
            <option value="voyage">✈️ Voyage & Immobilier</option>
            <option value="agri">🌾 Agriculture & Élevage</option>
            <option value="beaute">💄 Beauté & Cosmétique</option>
            <option value="industrie">🏭 Industrie & Usines</option>
            <option value="event">🎉 Événementiel & Loisirs</option>
            <option value="securite">🛡️ Sécurité & Gardiennage</option>
            <option value="entreprise" selected>💼 Bureaux, IT & Conseil</option>
          </select>
          <input type="text" id="customUrl" placeholder="Site web ou lien Facebook (optionnel)" />
          <input type="tel" id="customPhone" placeholder="Téléphone (ex: 70 12 34 56)" />
        </div>
        <button id="btnAuditSingle" class="btn btn-amber" onclick="runSingleAudit()">⚡ Auditer en direct maintenant</button>
      </div>
    </section>

    <!-- SECTION 2 : STRATÉGIE COMMERCIALE & OFFRE À VENDRE -->
    <section id="sectionOffers" class="section-block">
      <div class="offer-selector-card">
        <div class="offer-header">
          <span class="offer-badge-title">🎯 SERVICE À VENDRE (PITCH WHATSAPP DYNAMIQUE)</span>
          <span class="offer-subtitle">Cliquez sur une offre pour adapter les arguments et messages WhatsApp :</span>
        </div>
        <div class="offer-grid">
          <button type="button" id="offerBtn_WEB" class="offer-card-btn active" onclick="setSalesOffer('WEB')">
            <span class="offer-icon">🌐</span>
            <div class="offer-btn-text">
              <span class="offer-name">Site Web & Refonte</span>
              <span class="offer-desc">Vitrine pro, Vitesse, SSL & Mobile</span>
            </div>
          </button>
          <button type="button" id="offerBtn_ADS" class="offer-card-btn" onclick="setSalesOffer('ADS')">
            <span class="offer-icon">📱</span>
            <div class="offer-btn-text">
              <span class="offer-name">Facebook & Ads Meta</span>
              <span class="offer-desc">Publicités ciblées, Pixel & Retargeting</span>
            </div>
          </button>
          <button type="button" id="offerBtn_MAPS" class="offer-card-btn" onclick="setSalesOffer('MAPS')">
            <span class="offer-icon">📍</span>
            <div class="offer-btn-text">
              <span class="offer-name">Google Maps & SEO Local</span>
              <span class="offer-desc">Top 3 local, Fiche Maps & Avis clients</span>
            </div>
          </button>
        </div>

        <!-- Quick Global Controls for Copywriting Angle & Cadence -->
        <div style="margin-top:14px; padding-top:12px; border-top:1px solid var(--border); display:grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap:12px;">
          <div>
            <div class="copy-strip-label">🧠 Angle Psychologique Global (Chris Voss • Cialdini • Klaff)</div>
            <div class="angle-pills-grid" style="margin-bottom:0;">
              <button type="button" id="topAngle_voss" class="angle-pill-btn active-angle" onclick="setPitchAngle('voss')">🧠 Voss (No-oriented)</button>
              <button type="button" id="topAngle_diagnostic" class="angle-pill-btn" onclick="setPitchAngle('diagnostic')">🔬 Diagnostic</button>
              <button type="button" id="topAngle_reciprocity" class="angle-pill-btn" onclick="setPitchAngle('reciprocity')">🎁 Réciprocité</button>
              <button type="button" id="topAngle_social_proof" class="angle-pill-btn" onclick="setPitchAngle('social_proof')">👥 Preuve Sociale</button>
            </div>
          </div>
          <div>
            <div class="copy-strip-label">📅 Étape de Relance Globale (Cadence Temporelle)</div>
            <div class="stage-tabs-row" style="margin-bottom:0;">
              <button type="button" id="topStage_j0" class="stage-tab-btn active-stage" onclick="setPitchStage('j0')">⚡ J0 Premier Contact</button>
              <button type="button" id="topStage_j2" class="stage-tab-btn" onclick="setPitchStage('j2')">⏳ J+2 Rappel</button>
              <button type="button" id="topStage_j5" class="stage-tab-btn" onclick="setPitchStage('j5')">📈 J+5 Valeur FCFA</button>
              <button type="button" id="topStage_j8" class="stage-tab-btn" onclick="setPitchStage('j8')">🚪 J+8 Clôture Polie</button>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- SECTION 3 : TABLEAU DE BORD KPI, MON PIPELINE COMMERCIAL & LISTE PROSPECTS -->
    <section id="sectionProspects" class="section-block">
      <div class="section-title-bar">
        <div class="section-heading">
          <span class="section-step-badge" id="sectionProspectsBadge">RADAR & PIPELINE</span>
          <span id="sectionProspectsHeadingText">Résultats du Scan & Suivi Commercial</span>
        </div>
        <span class="section-hint" id="sectionProspectsHint">Séparez vos prospects à contacter et suivez vos affaires jusqu'au paiement</span>
      </div>

      <!-- BANNIÈRE FINANCIÈRE DU PIPELINE COMMERCIAL (Affichée dans Mon Pipeline Commercial) -->
      <div id="pipelineFinanceBanner" class="pipeline-finance-banner">
        <div class="fin-card fin-paid">
          <span class="fin-title">💰 Total Encaissé (Payé)</span>
          <div class="fin-amount" id="finPaidTotal" style="color: var(--green);">0 FCFA</div>
          <span class="fin-sub" id="finPaidCount">0 affaire conclue et payée</span>
        </div>
        <div class="fin-card fin-pending">
          <span class="fin-title">⏳ En attente (Livré + RDV)</span>
          <div class="fin-amount" id="finPendingTotal" style="color: #0d9488;">0 FCFA</div>
          <span class="fin-sub" id="finPendingCount">0 projet en cours / livré</span>
        </div>
        <div class="fin-card fin-active">
          <span class="fin-title">📥 Prospects dans Mon Pipeline</span>
          <div class="fin-amount" id="finActiveCount" style="color: #2563eb;">0</div>
          <span class="fin-sub" id="finTodoCount">Dont 0 dans « À contacter »</span>
        </div>
        <div class="fin-card fin-active" style="border-left-color: #7c3aed;">
          <span class="fin-title">📊 Conversion & Pipeline Total</span>
          <div class="fin-amount" id="finConversionRate" style="color: #7c3aed;">0%</div>
          <span class="fin-sub" id="finPotentialTotal">Pipeline global : 0 FCFA</span>
        </div>
      </div>

      <!-- KPI STRIP (DIAGNOSTIC TECHNIQUE - Affiché dans Radar de Scan) -->
      <div id="kpiDiagnosticStrip" class="kpi-strip">
        <div id="kpiCard_ALL" class="kpi active-kpi" onclick="quickFilterStatus('ALL')">
          <div class="kpi-top-row">
            <div class="kpi-val" id="kpiTotal">0</div>
            <span class="kpi-tag">100%</span>
          </div>
          <div class="kpi-label">📊 Tout le Radar</div>
        </div>
        <div id="kpiCard_SITE_MORT" class="kpi" onclick="quickFilterStatus('SITE_MORT')">
          <div class="kpi-top-row">
            <div class="kpi-val" id="kpiDead" style="color: var(--red);">0</div>
            <span class="kpi-tag">Urgent</span>
          </div>
          <div class="kpi-label">🔥 Sites Cassés</div>
        </div>
        <div id="kpiCard_FACEBOOK_SEUL" class="kpi" onclick="quickFilterStatus('FACEBOOK_SEUL')">
          <div class="kpi-top-row">
            <div class="kpi-val" id="kpiFb" style="color: var(--purple);">0</div>
            <span class="kpi-tag">Social</span>
          </div>
          <div class="kpi-label">📱 Facebook Seul</div>
        </div>
        <div id="kpiCard_SITE_FAIBLE" class="kpi" onclick="quickFilterStatus('SITE_FAIBLE')">
          <div class="kpi-top-row">
            <div class="kpi-val" id="kpiWeak" style="color: var(--amber);">0</div>
            <span class="kpi-tag">Refonte</span>
          </div>
          <div class="kpi-label">⚠️ Sites Lents / Sans SSL</div>
        </div>
        <div id="kpiCard_SANS_SITE" class="kpi" onclick="quickFilterStatus('SANS_SITE')">
          <div class="kpi-top-row">
            <div class="kpi-val" id="kpiNoSite" style="color: var(--blue);">0</div>
            <span class="kpi-tag">Création</span>
          </div>
          <div class="kpi-label">🚀 Sans Site Web</div>
        </div>
      </div>

      <!-- INTERACTIVE 6-STAGE CRM PIPELINE STRIP -->
      <div id="crmPipelineStrip" class="crm-pipeline-strip">
        <div id="crmStage_ALL" class="crm-stage-card active-crm-stage" style="border-top-color: var(--blue);" onclick="quickFilterCrm('ALL')">
          <div class="kpi-top-row">
            <div class="kpi-val" id="crmCount_ALL" style="font-size:1.25rem;">0</div>
            <span class="kpi-tag">Total</span>
          </div>
          <div class="kpi-label">📋 Tout le Pipeline</div>
        </div>
        <div id="crmStage_A_CONTACTER" class="crm-stage-card" style="border-top-color: #3b82f6;" onclick="quickFilterCrm('A_CONTACTER')">
          <div class="kpi-top-row">
            <div class="kpi-val" id="crmCount_A_CONTACTER" style="font-size:1.25rem; color:var(--blue);">0</div>
            <span class="kpi-tag">Étape 1</span>
          </div>
          <div class="kpi-label">📥 À contacter</div>
        </div>
        <div id="crmStage_CONTACTE" class="crm-stage-card" style="border-top-color: var(--amber);" onclick="quickFilterCrm('CONTACTE')">
          <div class="kpi-top-row">
            <div class="kpi-val" id="crmCount_CONTACTE" style="font-size:1.25rem; color:var(--amber);">0</div>
            <span class="kpi-tag">Étape 2</span>
          </div>
          <div class="kpi-label">⏳ Déjà contacté</div>
        </div>
        <div id="crmStage_CLIENT" class="crm-stage-card" style="border-top-color: var(--purple);" onclick="quickFilterCrm('CLIENT')">
          <div class="kpi-top-row">
            <div class="kpi-val" id="crmCount_CLIENT" style="font-size:1.25rem; color:var(--purple);">0</div>
            <span class="kpi-tag">Étape 3</span>
          </div>
          <div class="kpi-label">📅 Rendez-vous</div>
        </div>
        <div id="crmStage_LIVRE" class="crm-stage-card" style="border-top-color: var(--teal);" onclick="quickFilterCrm('LIVRE')">
          <div class="kpi-top-row">
            <div class="kpi-val" id="crmCount_LIVRE" style="font-size:1.25rem; color:var(--teal);">0</div>
            <span class="kpi-tag">Étape 4</span>
          </div>
          <div class="kpi-label">🛠️ Livré</div>
        </div>
        <div id="crmStage_PAYE" class="crm-stage-card" style="border-top-color: var(--green);" onclick="quickFilterCrm('PAYE')">
          <div class="kpi-top-row">
            <div class="kpi-val" id="crmCount_PAYE" style="font-size:1.25rem; color:var(--green);">0</div>
            <span class="kpi-tag">Étape 5</span>
          </div>
          <div class="kpi-label">💰 Payé</div>
        </div>
        <div id="crmStage_IGNORE" class="crm-stage-card" style="border-top-color: var(--red);" onclick="quickFilterCrm('IGNORE')">
          <div class="kpi-top-row">
            <div class="kpi-val" id="crmCount_IGNORE" style="font-size:1.25rem; color:var(--red);">0</div>
            <span class="kpi-tag">Étape 6</span>
          </div>
          <div class="kpi-label">❌ Échecs / Sans suite</div>
        </div>
      </div>

      <!-- CONSOLE DE FILTRAGE UNIFIÉE & COMPACTE -->
      <div class="filters-box">
        <div class="search-row">
          <input type="search" id="searchInput" placeholder="🔎 Rechercher un nom d'entreprise, quartier, ville, note, téléphone..." />
          <button type="button" class="btn btn-reset" onclick="resetFilters()">🔄 Tout réinitialiser</button>
        </div>

        <div class="selects-grid">
          <select id="cityFilter" title="Filtrer par Ville"></select>
          <select id="sectorFilter" title="Filtrer par Secteur"></select>
          <select id="crmFilter" title="Filtrer par Statut Pipeline"></select>
          <!-- Éléments masqués maintenus pour compatibilité des tests et synchronisation JS -->
          <select id="statusFilter" style="display:none;"></select>
          <select id="phoneFilter" style="display:none;"></select>
        </div>

        <!-- Badges interactifs consolidés (Sans doublons) -->
        <div class="unified-chips-bar">
          <div class="chips-subgroup">
            <span class="chips-mini-label">Diagnostic Web :</span>
            <button type="button" id="chip_web_SANS_SITE" class="filter-chip chip-web" onclick="toggleWebStatusFilter('SANS_SITE')" title="Entreprises sans site web propre"><span class="chip-icon" id="icon_web_SANS_SITE">🚀</span> <span>Sans site</span> <span class="chip-cnt" id="cnt_web_SANS_SITE">0</span></button>
            <button type="button" id="chip_web_SITE_MORT" class="filter-chip chip-dead" onclick="toggleWebStatusFilter('SITE_MORT')" title="Site web hors-ligne, erreur serveur HTTP ou SSL expiré"><span class="chip-icon" id="icon_web_SITE_MORT">🔥</span> <span>Site mort</span> <span class="chip-cnt" id="cnt_web_SITE_MORT">0</span></button>
            <button type="button" id="chip_web_SITE_FAIBLE" class="filter-chip chip-weak" onclick="toggleWebStatusFilter('SITE_FAIBLE')" title="Site web lent ou sans chiffrement SSL HTTPS"><span class="chip-icon" id="icon_web_SITE_FAIBLE">⚠️</span> <span>Site lent</span> <span class="chip-cnt" id="cnt_web_SITE_FAIBLE">0</span></button>
            <button type="button" id="chip_web_SITE_OK" class="filter-chip chip-ok" onclick="toggleWebStatusFilter('SITE_OK')" title="Site web opérationnel"><span class="chip-icon" id="icon_web_SITE_OK">✅</span> <span>Site OK</span> <span class="chip-cnt" id="cnt_web_SITE_OK">0</span></button>
          </div>

          <div class="chips-subgroup">
            <span class="chips-mini-label">Réseaux :</span>
            <button type="button" id="chip_fb_YES" class="filter-chip chip-fb" onclick="toggleFacebookFilter('WITH_FB')" title="A une page Facebook"><span class="chip-icon" id="icon_fb_YES">📘</span> <span>Avec Facebook</span> <span class="chip-cnt" id="cnt_fb_YES">0</span></button>
            <button type="button" id="chip_fb_NO" class="filter-chip chip-fb" onclick="toggleFacebookFilter('WITHOUT_FB')" title="Sans page Facebook"><span class="chip-icon" id="icon_fb_NO">🚫</span> <span>Sans Facebook</span> <span class="chip-cnt" id="cnt_fb_NO">0</span></button>
          </div>

          <div class="chips-subgroup">
            <span class="chips-mini-label">Contact :</span>
            <button type="button" id="chip_phone_WA" class="filter-chip chip-wa" onclick="togglePhoneTypeFilter('MOBILE_WHATSAPP')" title="Numéro mobile compatible WhatsApp direct"><span class="chip-icon" id="icon_phone_WA">💬</span> <span>WhatsApp Direct</span> <span class="chip-cnt" id="cnt_phone_WA">0</span></button>
            <button type="button" id="chip_phone_FIXE" style="display:none;"><span class="chip-icon" id="icon_phone_FIXE"></span><span class="chip-cnt" id="cnt_phone_FIXE"></span></button>
          </div>

          <div class="chips-subgroup">
            <span class="chips-mini-label">Opportunités :</span>
            <button type="button" id="chip_track_NO_PIXEL" class="filter-chip chip-track" onclick="toggleTrackingFilter('NO_PIXEL')" title="Pas de Pixel Facebook (cible vente Meta Ads)"><span class="chip-icon" id="icon_track_NO_PIXEL">🎯</span> <span>Sans Pixel Meta</span> <span class="chip-cnt" id="cnt_track_NO_PIXEL">0</span></button>
            <button type="button" id="chip_track_NO_GA" class="filter-chip chip-track" onclick="toggleTrackingFilter('NO_GA')" title="Pas de Google Analytics détecté"><span class="chip-icon" id="icon_track_NO_GA">📊</span> <span>Sans Analytics</span> <span class="chip-cnt" id="cnt_track_NO_GA">0</span></button>
          </div>
        </div>

        <div id="radarMemoryToggleBar" class="radar-memory-toggle-bar">
          <label class="toggle-known-label" for="hideKnownLeadsToggle">
            <input type="checkbox" id="hideKnownLeadsToggle" onchange="render()" />
            <span>🙈 Masquer ceux déjà dans mon Pipeline / Échecs (Ne voir que les nouveaux)</span>
          </label>
          <span id="radarKnownSummaryText" style="font-size:0.76rem; color:var(--muted);"></span>
        </div>

        <div id="activeFiltersSummary" class="active-filters-banner"></div>
      </div>

      <div class="filter-summary">
        <span id="filterCountText" style="font-weight:700;">Chargement...</span>
        <div style="display:flex; gap:8px; flex-wrap:wrap;">
          <button class="btn btn-reset" style="width:auto; min-height:36px; padding:6px 14px; font-size:0.78rem;" onclick="exportLeadsCsv()">📥 Exporter CSV</button>
          <button class="btn btn-reset" style="width:auto; min-height:36px; padding:6px 14px; font-size:0.78rem; color:#b91c1c; border-color:#fecaca; background:#fff1f2;" onclick="clearAllLeads()">🗑️ Vider la liste</button>
        </div>
      </div>

      <div id="leadContainer" class="lead-list"></div>
    </section>

    <!-- EXECUTIVE ENGINEERING BOARD & SHORTCUTS FOOTER -->
    <footer class="app-footer">
      <div class="footer-inner">
        <div class="footer-board">
          <span class="board-badge">🚀 Titan Executive Board — Conçu & Optimisé par l'Élite</span>
          <span class="board-names">Elon Musk (CEO • Simplicité & Vitesse) • Steve Jobs (Design & Tactile) • Jensen Huang (Compute & Temps Réel) • Mark Zuckerberg (WhatsApp & Social) • Guido van Rossum (Architecture Python) • Sam Altman (Conversion SaaS) • Dario Amodei (Sécurité & Évaluation) • Sir Jonathan Ive (Pixel-Perfection) • Linus Torvalds (Robustesse Noyau)</span>
        </div>
        <div class="footer-shortcuts">
          <span class="shortcut-tag"><kbd>/</kbd> Rechercher</span>
          <span class="shortcut-tag"><kbd>Esc</kbd> Fermer / Reset</span>
          <span class="shortcut-tag"><kbd>1</kbd> Radar</span>
          <span class="shortcut-tag"><kbd>2</kbd> Pipeline</span>
          <span class="shortcut-tag"><kbd>3</kbd> Offres</span>
          <span class="shortcut-tag"><kbd>4</kbd> Global</span>
        </div>
      </div>
    </footer>
  </div>

  <!-- SLIDE-OVER LEAD DETAIL MODAL ("Rentrer dans la fiche complète d'une entreprise") -->
  <div id="leadDetailModal" class="modal-backdrop" onclick="if(event.target===this) closeLeadDetail()">
    <div class="detail-drawer" role="dialog" aria-modal="true" aria-labelledby="drawerBizName">
      <div class="drawer-header">
        <div>
          <span id="drawerSectorBadge" style="font-size:0.72rem; color:var(--blue); font-weight:800; text-transform:uppercase;">SECTEUR</span>
          <h2 id="drawerBizName" style="font-size:1.25rem; font-weight:900; color:var(--text); margin-top:2px;">Entreprise</h2>
          <p id="drawerLocation" style="font-size:0.8rem; color:var(--muted); margin-top:2px;">📍 Localisation</p>
        </div>
        <button type="button" class="drawer-close-btn" onclick="closeLeadDetail()" title="Fermer">✕</button>
      </div>
      <div id="drawerBodyContent"></div>
    </div>
  </div>

  <!-- MODAL AUTHENTIFICATION & INSCRIPTION (COMMERCIAUX / ÉQUIPE) -->
  <div id="authModal" class="modal-backdrop" onclick="if(event.target===this) closeAuthModal()">
    <div class="center-dialog-box" role="dialog" aria-modal="true" aria-labelledby="authDialogTitle">
      <div class="drawer-header" style="margin-bottom:12px;">
        <div>
          <span style="font-size:0.72rem; color:var(--blue); font-weight:800; text-transform:uppercase;">ESPACE COMMERCIAL AGÈNCE</span>
          <h2 id="authDialogTitle" style="font-size:1.15rem; font-weight:900; color:var(--text); margin-top:2px;">Connexion Commerciale</h2>
        </div>
        <button type="button" class="drawer-close-btn" onclick="closeAuthModal()" title="Fermer">✕</button>
      </div>

      <div class="auth-tabs" style="display:flex; border-bottom:1px solid #e2e8f0; margin-bottom:14px;">
        <button type="button" id="tabAuthLogin" class="tab-btn active" style="flex:1; padding:8px; font-size:0.8rem; font-weight:700;" onclick="switchAuthTab('login')">🔑 Se connecter</button>
        <button type="button" id="tabAuthRegister" class="tab-btn" style="flex:1; padding:8px; font-size:0.8rem; font-weight:700;" onclick="switchAuthTab('register')">✨ Créer un compte</button>
      </div>

      <div id="authErrorMsg" style="color:#b91c1c; font-size:0.78rem; font-weight:700; margin-bottom:10px; min-height:18px;"></div>

      <!-- FORMULAIRE CONNEXION -->
      <form id="formAuthLogin" onsubmit="event.preventDefault(); submitLogin();" style="display:block;">
        <div style="margin-bottom:12px;">
          <label style="display:block; font-size:0.76rem; font-weight:700; color:#334155; margin-bottom:4px;">Nom d'utilisateur</label>
          <input type="text" id="loginUsername" class="search-input" style="width:100%; border:1px solid #cbd5e1; border-radius:6px; padding:8px 10px; font-size:0.85rem;" placeholder="Ex: moussa" autocomplete="username" required>
        </div>
        <div style="margin-bottom:16px;">
          <label style="display:block; font-size:0.76rem; font-weight:700; color:#334155; margin-bottom:4px;">Mot de passe</label>
          <input type="password" id="loginPassword" class="search-input" style="width:100%; border:1px solid #cbd5e1; border-radius:6px; padding:8px 10px; font-size:0.85rem;" placeholder="••••••••" autocomplete="current-password" required>
        </div>
        <button type="submit" class="btn btn-primary" style="width:100%; padding:10px; font-size:0.88rem; font-weight:800;">Se connecter à mon pipeline</button>
      </form>

      <!-- FORMULAIRE INSCRIPTION AVEC CODE D'INVITATION -->
      <form id="formAuthRegister" onsubmit="event.preventDefault(); submitRegister();" style="display:none;">
        <div style="margin-bottom:10px;">
          <label style="display:block; font-size:0.76rem; font-weight:700; color:#334155; margin-bottom:4px;">Nom d'utilisateur <span style="color:#ef4444;">*</span></label>
          <input type="text" id="regUsername" class="search-input" style="width:100%; border:1px solid #cbd5e1; border-radius:6px; padding:8px 10px; font-size:0.85rem;" placeholder="Ex: moussa_ouaga" autocomplete="username" required>
        </div>
        <div style="margin-bottom:10px;">
          <label style="display:block; font-size:0.76rem; font-weight:700; color:#334155; margin-bottom:4px;">Nom complet / Prénom</label>
          <input type="text" id="regDisplayName" class="search-input" style="width:100%; border:1px solid #cbd5e1; border-radius:6px; padding:8px 10px; font-size:0.85rem;" placeholder="Ex: Moussa Ouédraogo">
        </div>
        <div style="margin-bottom:10px;">
          <label style="display:block; font-size:0.76rem; font-weight:700; color:#334155; margin-bottom:4px;">Mot de passe (min 6 car.) <span style="color:#ef4444;">*</span></label>
          <input type="password" id="regPassword" class="search-input" style="width:100%; border:1px solid #cbd5e1; border-radius:6px; padding:8px 10px; font-size:0.85rem;" placeholder="••••••••" autocomplete="new-password" required>
        </div>
        <div style="margin-bottom:16px;">
          <label style="display:block; font-size:0.76rem; font-weight:700; color:#1e40af; margin-bottom:4px;">🔐 Code d'invitation Agence <span style="color:#ef4444;">*</span></label>
          <input type="text" id="regInviteCode" class="search-input" style="width:100%; border:2px solid #93c5fd; background:#eff6ff; border-radius:6px; padding:8px 10px; font-size:0.85rem; font-weight:700;" placeholder="Ex: BF-PROSPECT-2026" required>
          <p style="font-size:0.68rem; color:#64748b; margin-top:3px;">Requis pour protéger vos données et empêcher les inscriptions non autorisées.</p>
        </div>
        <button type="submit" class="btn btn-primary" style="width:100%; padding:10px; font-size:0.88rem; font-weight:800;">Créer mon compte commercial</button>
      </form>
    </div>
  </div>

  <!-- MODAL TABLEAU DE BORD DIRECTION AGÈNCE (ADMIN) -->
  <div id="adminTeamModal" class="modal-backdrop" onclick="if(event.target===this) closeAdminModal()">
    <div class="center-dialog-box" style="max-width:760px;" role="dialog" aria-modal="true" aria-labelledby="adminDialogTitle">
      <div class="drawer-header" style="margin-bottom:12px;">
        <div>
          <span style="font-size:0.72rem; color:#b45309; font-weight:800; text-transform:uppercase;">DIRECTION AGÈNCE & PILOTAGE</span>
          <h2 id="adminDialogTitle" style="font-size:1.25rem; font-weight:900; color:var(--text); margin-top:2px;">📊 Performance Globale & Pipelines Équipe</h2>
        </div>
        <button type="button" class="drawer-close-btn" onclick="closeAdminModal()" title="Fermer">✕</button>
      </div>
      <div id="adminDashboardBody" style="min-height:220px;"></div>
    </div>
  </div>

  <!-- FLOATING TOAST NOTIFICATION CONTAINER -->
  <div id="toastContainer" class="toast-container" aria-live="polite"></div>

  <script>
  let CURRENT_USER = null;
  let LEADS = [];
  const STORAGE_KEY = 'prospectbf_leads_v26';
  let CURRENT_OFFER = 'WEB';
  try {
    CURRENT_OFFER = localStorage.getItem('prospectbf_offer') || localStorage.getItem('prospectbf_sales_offer') || 'WEB';
  } catch (e) {}

  const safeAttrEnc = (str) => encodeURIComponent(str || '').replace(/'/g, '%27');

  function escapeHtml(str) {
    if (!str) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  async function checkAuthStatus() {
    try {
      const res = await fetch('/api/auth/me');
      const data = await res.json();
      if (data && data.authenticated && data.user) {
        CURRENT_USER = data.user;
      } else {
        CURRENT_USER = null;
      }
    } catch (e) {
      CURRENT_USER = null;
    }
    updateUserNavUI();
  }

  function updateUserNavUI() {
    const loggedInBlock = document.getElementById('userLoggedInBlock');
    const loggedOutBlock = document.getElementById('userLoggedOutBlock');
    const userBadge = document.getElementById('navUserBadge');
    const adminBtn = document.getElementById('btnAdminDashboard');

    if (!loggedInBlock || !loggedOutBlock) return;

    if (CURRENT_USER) {
      loggedInBlock.style.display = 'flex';
      loggedOutBlock.style.display = 'none';
      if (userBadge) {
        const roleLabel = CURRENT_USER.role === 'admin' ? 'Direction' : 'Commercial';
        userBadge.textContent = `👤 ${CURRENT_USER.display_name || CURRENT_USER.username} (${roleLabel})`;
      }
      if (adminBtn) {
        adminBtn.style.display = CURRENT_USER.role === 'admin' ? 'inline-flex' : 'none';
      }
    } else {
      loggedInBlock.style.display = 'none';
      loggedOutBlock.style.display = 'flex';
      if (adminBtn) adminBtn.style.display = 'none';
    }
  }

  function openAuthModal(mode) {
    const modal = document.getElementById('authModal');
    if (!modal) return;
    switchAuthTab(mode || 'login');
    modal.classList.add('open');
  }

  function closeAuthModal() {
    const modal = document.getElementById('authModal');
    if (modal) modal.classList.remove('open');
  }

  function switchAuthTab(mode) {
    const tabLogin = document.getElementById('tabAuthLogin');
    const tabReg = document.getElementById('tabAuthRegister');
    const formLogin = document.getElementById('formAuthLogin');
    const formReg = document.getElementById('formAuthRegister');
    const authErr = document.getElementById('authErrorMsg');
    if (authErr) authErr.textContent = '';

    if (mode === 'login') {
      if (tabLogin) tabLogin.classList.add('active');
      if (tabReg) tabReg.classList.remove('active');
      if (formLogin) formLogin.style.display = 'block';
      if (formReg) formReg.style.display = 'none';
    } else {
      if (tabLogin) tabLogin.classList.remove('active');
      if (tabReg) tabReg.classList.add('active');
      if (formLogin) formLogin.style.display = 'none';
      if (formReg) formReg.style.display = 'block';
    }
  }

  async function submitLogin() {
    const uEl = document.getElementById('loginUsername');
    const pEl = document.getElementById('loginPassword');
    const errEl = document.getElementById('authErrorMsg');
    if (!uEl || !pEl) return;
    const username = uEl.value.trim();
    const password = pEl.value.trim();
    if (!username || !password) {
      if (errEl) errEl.textContent = 'Identifiant et mot de passe requis.';
      return;
    }
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password })
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        if (errEl) errEl.textContent = data.error || 'Identifiants invalides';
        return;
      }
      CURRENT_USER = data.user;
      updateUserNavUI();
      closeAuthModal();
      showBanner(`👋 Bienvenue ${CURRENT_USER.display_name || CURRENT_USER.username} ! Espace commercial connecté.`);
      await loadInitialLeads();
    } catch (e) {
      if (errEl) errEl.textContent = 'Erreur réseau: ' + e;
    }
  }

  async function submitRegister() {
    const uEl = document.getElementById('regUsername');
    const dEl = document.getElementById('regDisplayName');
    const pEl = document.getElementById('regPassword');
    const iEl = document.getElementById('regInviteCode');
    const errEl = document.getElementById('authErrorMsg');
    if (!uEl || !pEl || !iEl) return;
    const username = uEl.value.trim();
    const display_name = (dEl ? dEl.value.trim() : '') || username;
    const password = pEl.value.trim();
    const invite_code = iEl.value.trim();

    if (!username || !password || !invite_code) {
      if (errEl) errEl.textContent = 'Tous les champs marqués * sont requis.';
      return;
    }
    try {
      const res = await fetch('/api/auth/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, display_name, password, invite_code })
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        if (errEl) errEl.textContent = data.error || 'Erreur lors de la création du compte';
        return;
      }
      CURRENT_USER = data.user;
      updateUserNavUI();
      closeAuthModal();
      showBanner(`🎉 Compte créé avec succès ! Bienvenue ${CURRENT_USER.display_name}.`);
      await loadInitialLeads();
    } catch (e) {
      if (errEl) errEl.textContent = 'Erreur réseau: ' + e;
    }
  }

  async function logoutUser() {
    try {
      await fetch('/api/auth/logout', { method: 'POST' });
    } catch (e) {}
    CURRENT_USER = null;
    updateUserNavUI();
    showBanner('Déconnexion effectuée.');
    await loadInitialLeads();
  }

  async function openAdminModal() {
    const modal = document.getElementById('adminTeamModal');
    if (!modal) return;
    modal.classList.add('open');
    const body = document.getElementById('adminDashboardBody');
    if (body) body.innerHTML = '<div style="text-align:center; padding:30px; color:#64748b;">⏳ Chargement des données agence...</div>';
    try {
      const res = await fetch('/api/admin/overview');
      const data = await res.json();
      if (!res.ok || !data.ok) {
        if (body) body.innerHTML = `<div style="color:#b91c1c; padding:20px; font-weight:700;">❌ ${data.error || 'Accès non autorisé au tableau de bord agence.'}</div>`;
        return;
      }
      renderAdminDashboard(data.overview);
    } catch (e) {
      if (body) body.innerHTML = `<div style="color:#b91c1c; padding:20px;">❌ Erreur: ${e}</div>`;
    }
  }

  function closeAdminModal() {
    const modal = document.getElementById('adminTeamModal');
    if (modal) modal.classList.remove('open');
  }

  function renderAdminDashboard(ov) {
    const body = document.getElementById('adminDashboardBody');
    if (!body || !ov) return;
    const fmt = (n) => (n || 0).toLocaleString('fr-FR') + ' FCFA';
    let repsRows = '';
    (ov.reps || []).forEach(r => {
      const roleBadge = r.role === 'admin'
        ? '<span style="background:#dbeafe; color:#1e40af; font-size:0.7rem; font-weight:700; padding:2px 6px; border-radius:4px;">ADMIN</span>'
        : '<span style="background:#f1f5f9; color:#475569; font-size:0.7rem; font-weight:700; padding:2px 6px; border-radius:4px;">COMMERCIAL</span>';
      repsRows += `
        <tr style="border-bottom:1px solid #e2e8f0; font-size:0.82rem;">
          <td style="padding:10px 8px; font-weight:700;">${escapeHtml(r.display_name || r.username)} <div style="font-size:0.7rem; color:#64748b; font-weight:normal;">@${escapeHtml(r.username)}</div></td>
          <td style="padding:10px 8px;">${roleBadge}</td>
          <td style="padding:10px 8px; text-align:center; font-weight:700; color:#2563eb;">${r.pipeline_count || 0}</td>
          <td style="padding:10px 8px; text-align:center; font-weight:700; color:#059669;">${r.won_count || 0}</td>
          <td style="padding:10px 8px; text-align:right; font-weight:800; color:#047857;">${fmt(r.paid_revenue_fcfa)}</td>
          <td style="padding:10px 8px; text-align:right; font-weight:700; color:#475569;">${fmt(r.pipeline_fcfa)}</td>
        </tr>
      `;
    });

    body.innerHTML = `
      <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(130px, 1fr)); gap:10px; margin-bottom:18px;">
        <div style="background:#eff6ff; border:1px solid #bfdbfe; border-radius:8px; padding:12px;">
          <div style="font-size:0.72rem; color:#1e40af; font-weight:700;">TOTAL RADAR</div>
          <div style="font-size:1.3rem; font-weight:900; color:#1d4ed8;">${ov.total_shared_leads || 0}</div>
          <div style="font-size:0.68rem; color:#60a5fa;">Entreprises scannées</div>
        </div>
        <div style="background:#ecfdf5; border:1px solid #a7f3d0; border-radius:8px; padding:12px;">
          <div style="font-size:0.72rem; color:#065f46; font-weight:700;">CA ENCAISSÉ</div>
          <div style="font-size:1.15rem; font-weight:900; color:#047857;">${fmt(ov.agency_revenue_paid_fcfa)}</div>
          <div style="font-size:0.68rem; color:#34d399;">Statut PAYÉ</div>
        </div>
        <div style="background:#fefce8; border:1px solid #fef08a; border-radius:8px; padding:12px;">
          <div style="font-size:0.72rem; color:#854d0e; font-weight:700;">PIPELINE ACTIF</div>
          <div style="font-size:1.15rem; font-weight:900; color:#a16207;">${fmt(ov.agency_pipeline_fcfa)}</div>
          <div style="font-size:0.68rem; color:#ca8a04;">En cours</div>
        </div>
        <div style="background:#faf5ff; border:1px solid #e9d5ff; border-radius:8px; padding:12px;">
          <div style="font-size:0.72rem; color:#6b21a8; font-weight:700;">ÉQUIPE AGÈNCE</div>
          <div style="font-size:1.3rem; font-weight:900; color:#7e22ce;">${(ov.reps || []).length}</div>
          <div style="font-size:0.68rem; color:#c084fc;">Membres actifs</div>
        </div>
      </div>
      <h3 style="font-size:0.92rem; font-weight:800; color:#0f172a; margin-bottom:10px;">👥 Performance par Commercial</h3>
      <div style="overflow-x:auto;">
        <table style="width:100%; border-collapse:collapse; text-align:left;">
          <thead>
            <tr style="background:#f8fafc; border-bottom:2px solid #e2e8f0; font-size:0.72rem; color:#64748b; text-transform:uppercase;">
              <th style="padding:8px;">Commercial</th>
              <th style="padding:8px;">Rôle</th>
              <th style="padding:8px; text-align:center;">Prospects</th>
              <th style="padding:8px; text-align:center;">Gagnés</th>
              <th style="padding:8px; text-align:right;">CA Payé</th>
              <th style="padding:8px; text-align:right;">Pipeline</th>
            </tr>
          </thead>
          <tbody>
            ${repsRows || '<tr><td colspan="6" style="padding:15px; text-align:center; color:#94a3b8;">Aucun commercial enregistré</td></tr>'}
          </tbody>
        </table>
      </div>
    `;
  }

  const SECTOR_LABELS = {
    all: "🏢 Tous les secteurs",
    hotel: "🏨 Hôtels & Résidences",
    sante: "🏥 Cliniques & Santé",
    pharmacie: "💊 Pharmacies",
    resto: "🍽️ Restaurants & Cafés",
    btp: "🏗️ BTP & Quincailleries",
    auto: "🚗 Auto & Logistique",
    commerce: "🛒 Boutiques & Commerce",
    finance: "🏦 Assurances & Finance",
    education: "🎓 Universités & Formation",
    voyage: "✈️ Voyage & Immobilier",
    agri: "🌾 Agriculture",
    beaute: "💄 Beauté",
    industrie: "🏭 Industrie",
    event: "🎉 Événementiel",
    securite: "🛡️ Sécurité",
    entreprise: "💼 Bureaux, IT & Conseil"
  };

  function getLeadSectorKey(item) {
    if (!item) return 'entreprise';
    const sk = String(item.sector_key || '').trim().toLowerCase();
    if (sk && sk in SECTOR_LABELS && sk !== 'all') return sk;
    const s = String(item.sector || '').toLowerCase();
    for (const [k, lbl] of Object.entries(SECTOR_LABELS)) {
      if (k === 'all') continue;
      if (s === k || s.includes(k) || lbl.toLowerCase().includes(s)) {
        return k;
      }
    }
    return 'entreprise';
  }

  const SECTOR_METRICS_JS = {
    hotel: {
      label: "Hôtels, Auberges & Résidences",
      salutation: "Monsieur le Gérant",
      client_term: "voyageurs d'affaires et touristes",
      loss_desc: "nuitées réservées directement sans commissions",
      loss_amount: "80 000 à 250 000 FCFA par semaine",
      value_prop: "un système de réservation de chambres instantané relié directement à votre WhatsApp",
      competitor_example: "les résidences et hôtels de votre zone"
    },
    sante: {
      label: "Cliniques, Cabinets & Labos",
      salutation: "Docteur",
      client_term: "patients recherchant une prise en charge rapide",
      loss_desc: "consultations spécialisées orientées vers d'autres centres",
      loss_amount: "150 000 à 500 000 FCFA par mois de soins",
      value_prop: "une orientation médicale claire et une prise de rendez-vous fluide sur WhatsApp",
      competitor_example: "les cabinets médicaux et polycliniques concurrents"
    },
    pharmacie: {
      label: "Pharmacies & Dépôts",
      salutation: "Docteur",
      client_term: "clients cherchant une officine de garde ou un médicament",
      loss_desc: "ventes d'ordonnances captées par d'autres officines",
      loss_amount: "60 000 à 200 000 FCFA par semaine",
      value_prop: "une fiche Google Maps Top 3 avec bouton d'appel direct et disponibilité WhatsApp",
      competitor_example: "les officines bien positionnées sur les cartes"
    },
    resto: {
      label: "Restaurants, Cafés & Maquis VIP",
      salutation: "Monsieur le Promoteur",
      client_term: "amateurs de bonne table et déjeuners d'affaires",
      loss_desc: "tables non réservées et commandes traiteurs parties chez vos voisins",
      loss_amount: "120 000 à 450 000 FCFA par week-end",
      value_prop: "un menu digital consultable en 1 clic avec commande WhatsApp directe",
      competitor_example: "les espaces et restaurants prisés du quartier"
    },
    btp: {
      label: "BTP, Quincailleries & Solaire",
      salutation: "Monsieur le Directeur",
      client_term: "entrepreneurs, particuliers et maîtres d'ouvrage",
      loss_desc: "demandes de devis matériaux, forages ou chantiers solaires",
      loss_amount: "500 000 à plus de 2 500 000 FCFA par marché manqué",
      value_prop: "un catalogue technique clair et une demande de devis instantanée sur WhatsApp",
      competitor_example: "les quincailleries modernes et sociétés BTP bien référencées"
    },
    auto: {
      label: "Auto, Location Véhicules & Garages",
      salutation: "Monsieur le Responsable",
      client_term: "automobilistes en panne ou clients cherchant une location",
      loss_desc: "locations de véhicules et réparations mécaniques urgentes",
      loss_amount: "100 000 à 350 000 FCFA par semaine de location",
      value_prop: "votre parc automobile repéré en 1 clic sur Google Maps et WhatsApp",
      competitor_example: "les loueurs et garages les mieux notés de la ville"
    },
    commerce: {
      label: "Supermarchés, Boutiques & Mode",
      salutation: "Cher Promoteur",
      client_term: "acheteurs à la recherche d'articles ou équipement",
      loss_desc: "visiteurs repartis sans acheter faute de catalogue lisible ou de suivi",
      loss_amount: "150 000 à 600 000 FCFA de chiffre d'affaires mensuel",
      value_prop: "une vitrine e-commerce épurée couplée au retargeting Meta Ads et WhatsApp",
      competitor_example: "les boutiques tendance qui captent l'attention sur Facebook et Google"
    },
    finance: {
      label: "Assurances, Microfinance & Cabinets",
      salutation: "Monsieur le Directeur",
      client_term: "professionnels et particuliers en quête de solutions financières",
      loss_desc: "dossiers de crédit, souscriptions d'assurance ou contrats comptables",
      loss_amount: "300 000 à 1 500 000 FCFA de commissions",
      value_prop: "un simulateur de devis et un tunnel de contact sécurisé pour vos conseillers",
      competitor_example: "les cabinets d'assurance et institutions financières leaders"
    },
    education: {
      label: "Universités, Écoles & Formation",
      salutation: "Monsieur le Fondateur",
      client_term: "étudiants et parents d'élèves pour les inscriptions",
      loss_desc: "scolarités de rentrée académique ou formations professionnelles",
      loss_amount: "400 000 à 1 800 000 FCFA par promotion d'étudiants",
      value_prop: "la plaquette des filières téléchargeable en ligne et pré-inscription sur WhatsApp",
      competitor_example: "les instituts et universités privées en tête sur le web"
    },
    voyage: {
      label: "Agences de Voyage & Immobilier",
      salutation: "Cher Directeur",
      client_term: "voyageurs pour billets et locataires en quête de logements",
      loss_desc: "commissions sur billets d'avion et mandats de location ou vente",
      loss_amount: "150 000 à 750 000 FCFA par transaction manquée",
      value_prop: "vos offres de vols et annonces immobilières reliées à un canal WhatsApp réactif",
      competitor_example: "les agences de voyages et cabinets immobiliers majeurs"
    },
    agri: {
      label: "Agriculture, Élevage & Agroalimentaire",
      salutation: "Cher Promoteur",
      client_term: "grossistes, restaurateurs et acheteurs d'intrants",
      loss_desc: "commandes en gros de volailles, céréales, bétail ou semences",
      loss_amount: "250 000 à 1 200 000 FCFA par commande gros",
      value_prop: "un bon de commande clair et un canal d'expédition direct sur WhatsApp",
      competitor_example: "les fermes et distributeurs agroalimentaires connectés"
    },
    beaute: {
      label: "Beauté, Coiffure & Cosmétique",
      salutation: "Madame la Directrice",
      client_term: "clientes et clients souhaitant des soins ou produits de beauté",
      loss_desc: "créneaux de soins non pourvus en semaine et ventes de cosmétiques",
      loss_amount: "50 000 à 200 000 FCFA par semaine de rendez-vous",
      value_prop: "la grille des tarifs et la réservation de créneaux en 2 clics sur mobile",
      competitor_example: "les salons VIP et instituts les plus visibles sur les réseaux"
    },
    industrie: {
      label: "Industrie, Usines & Manufactures",
      salutation: "Monsieur le Directeur",
      client_term: "distributeurs, acheteurs industriels et entreprises",
      loss_desc: "contrats de fourniture industrielle ou partenariats grossistes",
      loss_amount: "1 000 000 à 5 000 000 FCFA par contrat B2B",
      value_prop: "une vitrine professionnelle avec fiche technique et contact direct",
      competitor_example: "les usines et manufactures de référence dans le pays"
    },
    event: {
      label: "Événementiel, Loisirs & Nuit",
      salutation: "Cher Promoteur",
      client_term: "organisateurs d'événements, mariages, conférences et soirées",
      loss_desc: "dates de week-end sans location de salle ou réservation VIP",
      loss_amount: "300 000 à 1 200 000 FCFA par événement manqué",
      value_prop: "la visite virtuelle de votre espace et la réservation de date sur WhatsApp",
      competitor_example: "les complexes et salles de fêtes les plus recherchées"
    },
    securite: {
      label: "Sécurité Privée & Gardiennage",
      salutation: "Monsieur le Directeur Général",
      client_term: "sociétés, ONG, résidences et chantiers recherchant un gardiennage",
      loss_desc: "contrats de surveillance et installations d'alarmes",
      loss_amount: "400 000 à 2 000 000 FCFA par contrat annuel",
      value_prop: "une plaquette de sécurité certifiée et un chiffrage rapide envoyé par WhatsApp",
      competitor_example: "les sociétés de gardiennage recommandées sur le secteur"
    },
    entreprise: {
      label: "Bureaux, Sociétés IT, Conseil & ONG",
      salutation: "Monsieur le Directeur",
      client_term: "dirigeants, institutions et partenaires d'affaires",
      loss_desc: "missions d'expertise, audits ou prestations B2B",
      loss_amount: "500 000 à 3 000 000 FCFA par mission",
      value_prop: "un portfolio valorisant votre savoir-faire avec prise de contact rapide",
      competitor_example: "les cabinets conseils et sociétés réputées à Ouagadougou"
    }
  };

  let CURRENT_PITCH_ANGLE = 'voss';
  let CURRENT_PITCH_STAGE = 'j0';

  try {
    const savedAngle = localStorage.getItem('prospectbf_pitch_angle');
    if (savedAngle && ['voss', 'diagnostic', 'reciprocity', 'social_proof'].includes(savedAngle)) {
      CURRENT_PITCH_ANGLE = savedAngle;
    }
    const savedStage = localStorage.getItem('prospectbf_pitch_stage');
    if (savedStage && ['j0', 'j2', 'j5', 'j8'].includes(savedStage)) {
      CURRENT_PITCH_STAGE = savedStage;
    }
  } catch (e) {}

  let ACTIVE_DETAIL_LEAD = null;

  function setSalesOffer(offerKey) {
    if (!['WEB', 'ADS', 'MAPS'].includes(offerKey)) return;
    CURRENT_OFFER = offerKey;
    try {
      localStorage.setItem('prospectbf_offer', offerKey);
      localStorage.setItem('prospectbf_sales_offer', offerKey);
    } catch (e) {}
    ['WEB', 'ADS', 'MAPS'].forEach(k => {
      const btn = document.getElementById('offerBtn_' + k);
      if (btn) btn.classList.toggle('active', k === offerKey);
      const drawerBtn = document.getElementById('drawerOfferBtn_' + k);
      if (drawerBtn) drawerBtn.classList.toggle('active-angle', k === offerKey);
    });
    render();
    if (ACTIVE_DETAIL_LEAD) refreshDrawerPitch();
  }

  function setPitchAngle(angleKey) {
    if (!['voss', 'diagnostic', 'reciprocity', 'social_proof'].includes(angleKey)) return;
    CURRENT_PITCH_ANGLE = angleKey;
    try {
      localStorage.setItem('prospectbf_pitch_angle', angleKey);
    } catch (e) {}
    ['voss', 'diagnostic', 'reciprocity', 'social_proof'].forEach(a => {
      const topEl = document.getElementById('topAngle_' + a);
      if (topEl) topEl.classList.toggle('active-angle', a === angleKey);
      const btnEl = document.getElementById('btnAngle_' + a);
      if (btnEl) btnEl.classList.toggle('active-angle', a === angleKey);
    });
    refreshDrawerPitch();
    render();
  }

  function setPitchStage(stageKey) {
    if (!['j0', 'j2', 'j5', 'j8'].includes(stageKey)) return;
    CURRENT_PITCH_STAGE = stageKey;
    try {
      localStorage.setItem('prospectbf_pitch_stage', stageKey);
    } catch (e) {}
    ['j0', 'j2', 'j5', 'j8'].forEach(s => {
      const topEl = document.getElementById('topStage_' + s);
      if (topEl) topEl.classList.toggle('active-stage', s === stageKey);
      const btnEl = document.getElementById('btnStage_' + s);
      if (btnEl) btnEl.classList.toggle('active-stage', s === stageKey);
    });
    refreshDrawerPitch();
    render();
  }

  function refreshDrawerPitch() {
    if (!ACTIVE_DETAIL_LEAD) return;
    const lead = ACTIVE_DETAIL_LEAD;
    const pitchText = generatePitch(lead, CURRENT_OFFER, CURRENT_PITCH_ANGLE, CURRENT_PITCH_STAGE);
    const ta = document.getElementById('drawerCustomPitch');
    if (ta) ta.value = pitchText;

    const words = pitchText.trim().split(/\\s+/).filter(Boolean).length;
    const readSec = Math.max(5, Math.round((words / 180) * 60));
    const statsEl = document.getElementById('drawerPitchStats');
    if (statsEl) {
      statsEl.textContent = `${words} mots • ~${readSec}s de lecture • Réponse en 1 clic`;
    }

    const waBtn = document.getElementById('drawerWaBtn');
    if (waBtn && lead.phone_type === 'MOBILE_WHATSAPP' && lead.phone && lead.phone !== 'Non renseigné') {
      waBtn.href = buildWhatsAppUrl(lead.phone, pitchText);
    }

    ['WEB', 'ADS', 'MAPS'].forEach(k => {
      const el = document.getElementById('drawerOfferBtn_' + k);
      if (el) el.classList.toggle('active-angle', k === CURRENT_OFFER);
    });
    ['voss', 'diagnostic', 'reciprocity', 'social_proof'].forEach(a => {
      const el = document.getElementById('btnAngle_' + a);
      if (el) el.classList.toggle('active-angle', a === CURRENT_PITCH_ANGLE);
    });
    ['j0', 'j2', 'j5', 'j8'].forEach(s => {
      const el = document.getElementById('btnStage_' + s);
      if (el) el.classList.toggle('active-stage', s === CURRENT_PITCH_STAGE);
    });
  }

  function buildWhatsAppUrl(phone, pitch) {
    if (!phone || phone === 'Non renseigné') return '';
    let digits = String(phone).replace(/[^0-9]/g, '');
    if (!digits) return '';
    if (digits.length === 8) {
      digits = '226' + digits;
    } else if (digits.startsWith('00226')) {
      digits = digits.slice(2);
    }
    return 'https://wa.me/' + digits + '?text=' + encodeURIComponent(pitch);
  }

  function showToast(msg, type = 'info') {
    if (typeof document === 'undefined') return;
    const container = document.getElementById('toastContainer');
    if (!container || typeof container.appendChild !== 'function') return;
    try {
      const toast = document.createElement('div');
      toast.className = `toast-item toast-${type}`;
      const icon = type === 'success' ? '✅' : (type === 'error' ? '❌' : (msg.includes('⏳') ? '⏳' : 'ℹ️'));
      toast.innerHTML = `<span>${icon}</span><span>${escapeHtml(msg)}</span>`;
      container.appendChild(toast);
      setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateY(10px)';
        toast.style.transition = 'all 0.25s ease';
        setTimeout(() => { if (toast.parentNode) toast.parentNode.removeChild(toast); }, 250);
      }, 3000);
    } catch (e) {}
  }

  function sharePitch(name, phone, pitch) {
    const waUrl = buildWhatsAppUrl(phone, pitch);
    if (typeof navigator !== 'undefined' && navigator.share) {
      navigator.share({
        title: `Proposition B2B — ${name}`,
        text: pitch,
        url: waUrl || undefined
      }).catch((err) => {
        if (err && err.name !== 'AbortError') {
          copyPitchText(null, pitch);
        }
      });
    } else {
      copyPitchText(null, pitch);
    }
  }

  function copyPitchText(btn, text) {
    const orig = (btn && btn.textContent && btn.textContent !== '✅ Copié !') ? btn.textContent : '📋 Copier Pitch';
    const fallbackExecCopy = () => {
      try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        const ok = document.execCommand('copy');
        document.body.removeChild(ta);
        return ok;
      } catch (e) {
        return false;
      }
    };

    const triggerSuccess = () => {
      if (btn) {
        btn.textContent = '✅ Copié !';
        setTimeout(() => { btn.textContent = orig; }, 1500);
      }
      showToast('📋 Pitch copié dans le presse-papier !', 'success');
    };

    if (typeof navigator !== 'undefined' && navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      navigator.clipboard.writeText(text).then(triggerSuccess).catch(() => {
        if (fallbackExecCopy()) {
          triggerSuccess();
        } else {
          showToast('⚠️ Impossible de copier automatiquement', 'error');
        }
      });
    } else {
      if (fallbackExecCopy()) {
        triggerSuccess();
      } else {
        showToast('⚠️ Presse-papier non supporté', 'error');
      }
    }
  }

  let CURRENT_VIEW = 'scanner';
  const PIPELINE_STAGE_KEYS = ['CONTACTE', 'RDV', 'CLIENT', 'LIVRE', 'PAYE', 'ECHEC', 'IGNORE'];

  function isLeadInPipeline(b) {
    if (!b) return false;
    const st = b.crm_status || 'A_CONTACTER';
    if (PIPELINE_STAGE_KEYS.includes(st)) return true;
    return Boolean(b.in_pipeline);
  }

  function normalizeStageGroup(crmStatus) {
    const st = crmStatus || 'A_CONTACTER';
    if (st === 'RDV' || st === 'CLIENT') return 'CLIENT';
    if (st === 'ECHEC' || st === 'IGNORE') return 'IGNORE';
    if (st === 'LIVRE') return 'LIVRE';
    if (st === 'PAYE') return 'PAYE';
    if (st === 'CONTACTE') return 'CONTACTE';
    return 'A_CONTACTER';
  }

  function formatFcfa(amount) {
    const n = Math.max(0, parseInt(amount || 0, 10) || 0);
    return n.toString().replace(/\\B(?=(\\d{3})+(?!\\d))/g, ' ') + ' FCFA';
  }

  function formatRelativeTime(isoStr) {
    if (!isoStr) return "Aujourd'hui";
    try {
      const dt = new Date(isoStr);
      if (isNaN(dt.getTime())) return "Aujourd'hui";
      const diffMs = Date.now() - dt.getTime();
      const diffMin = Math.floor(diffMs / 60000);
      if (diffMin < 2) return "À l'instant";
      if (diffMin < 60) return `Il y a ${diffMin} min`;
      const diffHours = Math.floor(diffMin / 60);
      if (diffHours < 24) return `Aujourd'hui (il y a ${diffHours}h)`;
      const diffDays = Math.floor(diffHours / 24);
      return `Il y a ${diffDays} j`;
    } catch (e) {
      return "Aujourd'hui";
    }
  }

  function findLeadByNameCity(name, city) {
    return LEADS.find(l => {
      const sameName = (l.name || '').trim().toLowerCase() === (name || '').trim().toLowerCase();
      if (city && city !== 'Audit Direct') {
        return sameName && (l.city || '').trim().toLowerCase() === city.trim().toLowerCase();
      }
      return sameName;
    });
  }

  async function syncLeadToServer(lead, extraPayload = {}) {
    if (!lead) return;
    try {
      await fetch('/api/leads/status', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(Object.assign({
          name: lead.name,
          city: lead.city,
          crm_status: lead.crm_status || 'A_CONTACTER',
          in_pipeline: Boolean(lead.in_pipeline),
          rdv_done: Boolean(lead.rdv_done),
          deal_amount: parseInt(lead.deal_amount || 0, 10) || 0,
          notes: lead.notes || '',
          updated_at: lead.updated_at || new Date().toISOString()
        }, extraPayload))
      });
    } catch (e) {
      console.warn('Backend CRM sync failed, cached locally', e);
    }
  }

  async function addLeadToPipeline(name, city) {
    if (!CURRENT_USER) {
      openAuthModal('login');
      showBanner('🔒 Connexion requise : Veuillez vous connecter avec votre compte agence pour ajouter un prospect.');
      return;
    }
    const match = findLeadByNameCity(name, city);
    if (!match) return;
    if (match.claimed_by && !match.is_claimed_by_me && typeof confirm === 'function') {
      if (!confirm(`⚠️ Attention : Ce prospect est déjà dans le pipeline de ${match.claimed_by_name || match.claimed_by}. Souhaitez-vous l'ajouter à votre pipeline personnel ?`)) return;
    }
    match.crm_status = 'A_CONTACTER';
    match.in_pipeline = true;
    match.updated_at = new Date().toISOString();
    saveLocalBackup();
    populateFilterDropdowns();
    render();
    if (ACTIVE_DETAIL_LEAD === match) renderLeadDetailDrawer();
    showBanner(`📌 "${match.name}" a été envoyé dans « Mon Pipeline Commercial → À contacter » !`);
    await syncLeadToServer(match, { crm_status: 'A_CONTACTER', in_pipeline: true });
  }

  async function setLeadCrmStatus(name, newStatus, city) {
    if (!CURRENT_USER) {
      openAuthModal('login');
      showBanner('🔒 Connexion requise : Veuillez vous connecter pour modifier le statut commercial.');
      return;
    }
    const match = findLeadByNameCity(name, city);
    if (match) {
      match.crm_status = newStatus;
      match.in_pipeline = newStatus !== 'NOUVEAU';
      match.updated_at = new Date().toISOString();
      saveLocalBackup();
      populateFilterDropdowns();
      render();
      if (ACTIVE_DETAIL_LEAD === match) renderLeadDetailDrawer();
      await syncLeadToServer(match, { crm_status: newStatus, in_pipeline: match.in_pipeline });
    }
  }

  async function toggleLeadRdvDone(name, city) {
    if (!CURRENT_USER) {
      openAuthModal('login');
      showBanner('🔒 Connexion requise : Veuillez vous connecter pour valider un rendez-vous.');
      return;
    }
    const match = findLeadByNameCity(name, city);
    if (!match) return;
    match.rdv_done = !Boolean(match.rdv_done);
    match.in_pipeline = true;
    if (match.crm_status !== 'CLIENT' && match.crm_status !== 'RDV') {
      match.crm_status = 'CLIENT';
    }
    match.updated_at = new Date().toISOString();
    saveLocalBackup();
    populateFilterDropdowns();
    render();
    if (ACTIVE_DETAIL_LEAD === match) renderLeadDetailDrawer();
    await syncLeadToServer(match, { rdv_done: match.rdv_done });
  }

  async function updateLeadMeta(name, city, fields) {
    if (!CURRENT_USER) {
      openAuthModal('login');
      showBanner('🔒 Connexion requise : Veuillez vous connecter pour modifier les montants et notes de suivi.');
      return;
    }
    const match = findLeadByNameCity(name, city);
    if (!match) return;
    if (fields.deal_amount !== undefined) {
      match.deal_amount = Math.max(0, parseInt(fields.deal_amount || 0, 10) || 0);
    }
    if (fields.notes !== undefined) {
      match.notes = String(fields.notes || '').trim();
    }
    match.updated_at = new Date().toISOString();
    saveLocalBackup();
    render();
    if (ACTIVE_DETAIL_LEAD === match) renderLeadDetailDrawer();
    await syncLeadToServer(match, {
      deal_amount: match.deal_amount,
      notes: match.notes,
      updated_at: match.updated_at
    });
  }

  function openLeadInPipeline(name, city) {
    const match = findLeadByNameCity(name, city);
    const stageGroup = match ? normalizeStageGroup(match.crm_status) : 'ALL';
    switchAppView('crm');
    quickFilterCrm(stageGroup);
  }

  function generatePitch(lead, offerOverride, angleOverride, stageOverride) {
    const off = offerOverride || CURRENT_OFFER || 'WEB';
    const ang = angleOverride || CURRENT_PITCH_ANGLE || 'voss';
    const stg = stageOverride || CURRENT_PITCH_STAGE || 'j0';

    const name = (lead && lead.name) ? lead.name.trim() : "l'entreprise";
    const city = (lead && lead.city && lead.city !== 'Audit Direct') ? lead.city : 'Burkina Faso';
    const rawNeigh = lead && lead.neighborhood ? lead.neighborhood.trim() : '';
    const neighborhood = (rawNeigh && rawNeigh !== 'Quartier non précisé' && rawNeigh !== 'Saisie manuelle')
      ? `vers ${rawNeigh}`
      : `à ${city}`;

    const skey = getLeadSectorKey(lead);
    const met = SECTOR_METRICS_JS[skey] || SECTOR_METRICS_JS.entreprise;
    const salutationTitle = met.salutation || 'Monsieur le Responsable';
    const status = (lead && (lead.status || lead.audit_status)) || 'SANS_SITE';
    const issues = (lead && lead.issues) || [];
    const respMs = (lead && lead.response_ms) || 0;

    // RELANCES CADENCÉES (J+2, J+5, J+8)
    if (stg === 'j2') {
      return `Bonjour ${salutationTitle} (${name}),\n\nJe me permets de faire remonter mon message précédent concernant le renforcement de votre visibilité ${off} à ${city}.\n\nAvez-vous eu l'opportunité d'en prendre connaissance, ou préférez-vous que nous refassions un point la semaine prochaine ?\n\n👉 Un simple « Oui » ou « Plus tard » me suffit pour adapter mon suivi !`;
    }
    if (stg === 'j5') {
      return `Bonjour ${salutationTitle} (${name}),\n\nPour information, notre analyse du secteur « ${met.label} » à ${city} montre que les entreprises dotées d'une présence ${off} optimisée avec tunnel WhatsApp récupèrent en moyenne ${met.loss_amount} de commandes supplémentaires par mois.\n\n👉 Seriez-vous curieux de voir la simulation chiffrée pour votre établissement ?`;
    }
    if (stg === 'j8') {
      return `Bonjour ${name},\n\nJe suppose que l'optimisation de votre présence ${off} à ${city} n'est pas votre priorité actuellement, et je le respecte tout à fait.\n\nJe classe votre dossier pour ne plus encombrer votre messagerie. Si le sujet redevient d'actualité, vous avez mes coordonnées ici même.\n\nJe vous souhaite plein succès dans le développement de vos activités !`;
    }

    // J0 PREMIER CONTACT EN 5 MODULES D'ÉLITE
    let hook = `Bonjour ${salutationTitle} (${name} • ${city}),`;

    let observation = '';
    let flawFiltered = issues.filter(i => !i.includes('Pixel') && !i.includes('Analytics'));
    let flawText = flawFiltered.length > 0 ? flawFiltered[0].toLowerCase() : 'la lenteur de chargement sur smartphone';

    if (off === 'WEB') {
      if (status === 'SITE_MORT') {
        observation = `en voulant consulter votre site web (${lead.website || 'actuel'}) ${neighborhood}, j'ai constaté qu'il est en panne et affiche une page d'erreur technique (serveur hors-ligne ou certificat expiré).`;
      } else if (status === 'SITE_FAIBLE') {
        const msInfo = respMs > 0 ? ` (${respMs} ms mesurés sur smartphone)` : '';
        observation = `en visitant votre site (${lead.website}) depuis mon smartphone ${neighborhood}, j'ai relevé des défauts techniques${msInfo} liés à ${flawText}.`;
      } else if (status === 'FACEBOOK_SEUL') {
        observation = `votre page Facebook ${neighborhood} est active, mais vous ne disposez pas encore d'un site web officiel professionnel à votre nom.`;
      } else if (status === 'SANS_SITE') {
        observation = `en recherchant vos services ${neighborhood}, j'ai remarqué que vous n'avez pas encore de vitrine digitale officielle reliée à WhatsApp.`;
      } else {
        observation = `votre site web (${lead.website}) fonctionne bien, mais il lui manque un tunnel de conversion direct vers WhatsApp.`;
      }
    } else if (off === 'ADS') {
      if (!lead.has_fb_pixel) {
        observation = `en analysant votre dispositif digital ${neighborhood}, j'ai noté qu'aucun Pixel Meta / Pixel Facebook n'est configuré : vous perdez vos visiteurs sans pouvoir faire de retargeting publicitaire ou recibler vos prospects.`;
      } else {
        observation = `à ${neighborhood}, des centaines de prospects consultent vos publications sans pouvoir les recibler ou les convertir directement en clients.`;
      }
    } else {
      observation = `lorsqu'un client recherche des services de ${met.label} ${neighborhood} ou à ${city} directement sur Google Maps depuis son téléphone,`;
    }

    let impact = '';
    if (ang === 'diagnostic') {
      impact = `Chaque semaine, plus de 60% des visiteurs mobiles quittent la page sans appeler, ce qui représente un manque à gagner estimé à environ ${met.loss_amount} en ${met.loss_desc}.`;
    } else if (ang === 'social_proof') {
      impact = `Pendant ce temps, ${met.competitor_example} captent l'essentiel de ces demandes qualifiées, vous privant d'environ ${met.loss_amount}.`;
    } else if (ang === 'reciprocity') {
      impact = `Beaucoup d'entreprises perdent environ ${met.loss_amount} simplement à cause de frictions évitables lors de la première interaction sur mobile.`;
    } else {
      impact = `Vos futurs ${met.client_term} hésitent et partent avant de vous contacter, ce qui peut représenter une opportunité manquée de ${met.loss_amount}.`;
    }

    let value = '';
    if (off === 'WEB') {
      if (status === 'SITE_MORT' || status === 'SITE_FAIBLE') {
        value = `Je vous propose de corriger cette panne et d'effectuer une refonte moderne de votre vitrine en 72h avec un affichage ultra-rapide en 1 seconde et ${met.value_prop}`;
      } else {
        value = `Nous vous mettons en place une vitrine officielle moderne, optimisée pour smartphone et reliée directement à ${met.value_prop}`;
      }
    } else if (off === 'ADS') {
      value = `Nous pilotons vos campagnes de publicité sponsorisée Meta Ads et configurons le pixel de retargeting pour recibler ces visiteurs et les transformer en messages qualifiés sur votre WhatsApp`;
    } else {
      value = `Nous positionnons votre établissement dans le Top 3 Google Maps avec référencement SEO local et gestion des avis clients pour capter en priorité tous les appels et visites de votre zone`;
    }

    let cta = '';
    if (ang === 'voss') {
      cta = `Seriez-vous contre l'idée que je vous envoie un aperçu vidéo de 60 secondes sans aucun engagement ?`;
    } else if (ang === 'diagnostic') {
      cta = `Avez-vous 2 minutes pour que je vous transmette les détails du test technique directement sur WhatsApp ?`;
    } else if (ang === 'reciprocity') {
      cta = `J'ai déjà préparé un modèle adapté à votre activité : souhaitez-vous que je vous le transmette gratuitement ici ?`;
    } else {
      cta = `Serait-ce déraisonnable de faire un point de 5 minutes pour comparer votre visibilité avec vos concurrents locaux ?`;
    }

    return `${hook}\n\nEn observant votre activité, ${observation} ${impact}\n\n${value}.\n\n👉 ${cta}`;
  }

  function dedupeAndSortLeads(list) {
    const map = new Map();
    list.forEach(item => {
      if (!item || !item.name) return;
      const key = `${item.name.trim().toLowerCase()}|${(item.city || '').trim().toLowerCase()}`;
      if (map.has(key)) {
        const prev = map.get(key);
        const combined = Object.assign({}, prev, item);
        const prevUp = String(prev.updated_at || '').trim();
        const itemUp = String(item.updated_at || '').trim();

        if (prevUp && itemUp) {
          const crmWinner = itemUp >= prevUp ? item : prev;
          combined.crm_status = crmWinner.crm_status || 'A_CONTACTER';
          combined.in_pipeline = Boolean(
            crmWinner.in_pipeline || PIPELINE_STAGE_KEYS.includes(combined.crm_status)
          );
          combined.rdv_done = Boolean(crmWinner.rdv_done);
          combined.deal_amount = Math.max(0, parseInt(crmWinner.deal_amount || 0, 10) || 0);
          combined.notes = String(crmWinner.notes || '');
          combined.updated_at = crmWinner.updated_at || '';
        } else if (itemUp && !prevUp) {
          combined.crm_status = item.crm_status || prev.crm_status || 'A_CONTACTER';
          combined.in_pipeline = Boolean(
            item.in_pipeline || PIPELINE_STAGE_KEYS.includes(combined.crm_status)
          );
          combined.rdv_done = Boolean(item.rdv_done);
          combined.deal_amount = Math.max(0, parseInt(item.deal_amount || 0, 10) || 0);
          combined.notes = String(item.notes || '');
          combined.updated_at = itemUp;
        } else if (prevUp && !itemUp) {
          combined.crm_status = prev.crm_status || item.crm_status || 'A_CONTACTER';
          combined.in_pipeline = Boolean(
            prev.in_pipeline || PIPELINE_STAGE_KEYS.includes(combined.crm_status)
          );
          combined.rdv_done = Boolean(prev.rdv_done);
          combined.deal_amount = Math.max(0, parseInt(prev.deal_amount || 0, 10) || 0);
          combined.notes = String(prev.notes || '');
          combined.updated_at = prevUp;
        } else {
          if (prev.crm_status && prev.crm_status !== 'A_CONTACTER' && (!item.crm_status || item.crm_status === 'A_CONTACTER')) {
            combined.crm_status = prev.crm_status;
          }
          combined.in_pipeline = Boolean(
            prev.in_pipeline || item.in_pipeline || PIPELINE_STAGE_KEYS.includes(combined.crm_status)
          );
          combined.rdv_done = Boolean(prev.rdv_done || item.rdv_done);
          combined.deal_amount = parseInt(item.deal_amount || prev.deal_amount || 0, 10) || 0;
          combined.notes = (item.notes && String(item.notes).trim()) ? item.notes : (prev.notes || '');
          combined.updated_at = itemUp || prevUp || '';
        }
        map.set(key, combined);
      } else {
        if (!item.crm_status) item.crm_status = 'A_CONTACTER';
        item.in_pipeline = Boolean(item.in_pipeline || PIPELINE_STAGE_KEYS.includes(item.crm_status));
        item.rdv_done = Boolean(item.rdv_done);
        item.deal_amount = parseInt(item.deal_amount || 0, 10) || 0;
        item.notes = item.notes || '';
        item.updated_at = item.updated_at || '';
        map.set(key, item);
      }
    });
    const arr = Array.from(map.values());
    arr.sort((a, b) => {
      if ((b.score || 0) !== (a.score || 0)) return (b.score || 0) - (a.score || 0);
      const aMob = a.phone_type === 'MOBILE_WHATSAPP' ? 1 : 0;
      const bMob = b.phone_type === 'MOBILE_WHATSAPP' ? 1 : 0;
      return bMob - aMob;
    });
    return arr;
  }

  function getStorageKey() {
    const uid = CURRENT_USER && CURRENT_USER.id ? CURRENT_USER.id : 'guest';
    return `prospectbf_leads_v26_u${uid}`;
  }

  function saveLocalBackup() {
    try { localStorage.setItem(getStorageKey(), JSON.stringify(LEADS)); } catch (e) {}
  }

  function loadLocalBackup() {
    try {
      const raw = localStorage.getItem(getStorageKey());
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed : [];
    } catch (e) { return []; }
  }

  function switchTab(mode) {
    document.getElementById('panelScan').classList.toggle('open', mode === 'scan');
    document.getElementById('panelSingle').classList.toggle('open', mode === 'single');
    document.getElementById('tabScan').classList.toggle('active', mode === 'scan');
    document.getElementById('tabSingle').classList.toggle('active', mode === 'single');
    document.getElementById('tabHide').classList.toggle('active', mode === 'none');
  }

  function shortDomain(url) {
    if (!url) return '';
    try {
      const u = new URL(url.startsWith('http') ? url : 'http://' + url);
      return u.hostname.replace(/^www\\./, '');
    } catch (e) { return url.slice(0, 28); }
  }

  function showBanner(msg, isError = false) {
    const b = document.getElementById('statusBanner');
    if (b) {
      b.style.display = 'block';
      b.style.borderColor = isError ? '#fecaca' : '#bfdbfe';
      b.style.background = isError ? '#fef2f2' : '#eff6ff';
      b.style.color = isError ? '#b91c1c' : '#1d4ed8';
      b.textContent = msg;
    }
    const toastType = isError ? 'error' : (msg.includes('⏳') ? 'info' : 'success');
    showToast(msg, toastType);
  }

  function highlightActiveKpi(st) {
    let effective = st;
    if (!effective) {
      if (MULTI_FILTERS.webStatuses.size === 0 && MULTI_FILTERS.facebook === 'ALL') {
        effective = 'ALL';
      } else if (MULTI_FILTERS.webStatuses.size === 1 && MULTI_FILTERS.webStatuses.has('SANS_SITE') && MULTI_FILTERS.facebook === 'WITH_FB') {
        effective = 'FACEBOOK_SEUL';
      } else if (MULTI_FILTERS.webStatuses.size === 1 && MULTI_FILTERS.facebook === 'ALL') {
        effective = Array.from(MULTI_FILTERS.webStatuses)[0];
      } else {
        effective = '';
      }
    }
    ['ALL', 'SITE_MORT', 'FACEBOOK_SEUL', 'SITE_FAIBLE', 'SANS_SITE'].forEach(k => {
      const el = document.getElementById('kpiCard_' + k);
      if (el) el.classList.toggle('active-kpi', k === effective);
    });
  }

  function highlightActiveCrmStage(crmSt) {
    const norm = crmSt === 'RDV' ? 'CLIENT' : (crmSt === 'ECHEC' ? 'IGNORE' : crmSt);
    ['ALL', 'A_CONTACTER', 'CONTACTE', 'CLIENT', 'LIVRE', 'PAYE', 'IGNORE'].forEach(k => {
      const el = document.getElementById('crmStage_' + k);
      if (el) el.classList.toggle('active-crm-stage', k === norm);
    });
  }

  const MULTI_FILTERS = {
    webStatuses: new Set(),
    facebook: 'ALL', // 'ALL' | 'WITH_FB' | 'WITHOUT_FB'
    phoneTypes: new Set(),
    tracking: new Set(), // 'NO_PIXEL' | 'NO_GA'
  };

  function leadHasDedicatedWebsite(b) {
    if (!b) return false;
    if (b.status === 'SANS_SITE' || b.status === 'FACEBOOK_SEUL') return false;
    if (!b.website) return false;
    const low = b.website.toLowerCase();
    if (low.includes('facebook.com') || low.includes('fb.com')) return false;
    return true;
  }

  function leadHasFacebook(b) {
    if (!b) return false;
    if (b.facebook_url && b.facebook_url.trim()) return true;
    if (b.status === 'FACEBOOK_SEUL') return true;
    if (b.website && (b.website.toLowerCase().includes('facebook.com') || b.website.toLowerCase().includes('fb.com'))) return true;
    return false;
  }

  function toggleWebStatusFilter(st) {
    if (MULTI_FILTERS.webStatuses.has(st)) {
      MULTI_FILTERS.webStatuses.delete(st);
    } else {
      MULTI_FILTERS.webStatuses.add(st);
    }
    syncDropdownsFromMulti();
    render();
  }

  function toggleFacebookFilter(fbType) {
    if (MULTI_FILTERS.facebook === fbType) {
      MULTI_FILTERS.facebook = 'ALL';
    } else {
      MULTI_FILTERS.facebook = fbType;
    }
    syncDropdownsFromMulti();
    render();
  }

  function togglePhoneTypeFilter(pt) {
    if (MULTI_FILTERS.phoneTypes.has(pt)) {
      MULTI_FILTERS.phoneTypes.delete(pt);
    } else {
      MULTI_FILTERS.phoneTypes.add(pt);
    }
    syncDropdownsFromMulti();
    render();
  }

  function toggleTrackingFilter(tr) {
    if (MULTI_FILTERS.tracking.has(tr)) {
      MULTI_FILTERS.tracking.delete(tr);
    } else {
      MULTI_FILTERS.tracking.add(tr);
    }
    syncDropdownsFromMulti();
    render();
  }

  function clearMultiFilters() {
    MULTI_FILTERS.webStatuses.clear();
    MULTI_FILTERS.facebook = 'ALL';
    MULTI_FILTERS.phoneTypes.clear();
    MULTI_FILTERS.tracking.clear();
    const statusEl = document.getElementById('statusFilter');
    if (statusEl) statusEl.value = 'ALL';
    const phoneEl = document.getElementById('phoneFilter');
    if (phoneEl) phoneEl.value = 'ALL';
  }

  function syncDropdownsFromMulti() {
    const statusEl = document.getElementById('statusFilter');
    if (statusEl) {
      if (MULTI_FILTERS.webStatuses.size === 0 && MULTI_FILTERS.facebook === 'ALL') {
        statusEl.value = 'ALL';
      } else if (MULTI_FILTERS.webStatuses.size === 1 && MULTI_FILTERS.webStatuses.has('SANS_SITE') && MULTI_FILTERS.facebook === 'WITH_FB') {
        statusEl.value = 'FACEBOOK_SEUL';
      } else if (MULTI_FILTERS.webStatuses.size === 0 && MULTI_FILTERS.facebook === 'WITH_FB') {
        statusEl.value = 'HAS_FACEBOOK';
      } else if (MULTI_FILTERS.webStatuses.size === 1 && MULTI_FILTERS.facebook === 'ALL') {
        const only = Array.from(MULTI_FILTERS.webStatuses)[0];
        statusEl.value = only;
      } else {
        statusEl.value = 'CUSTOM';
      }
    }

    const phoneEl = document.getElementById('phoneFilter');
    if (phoneEl) {
      if (MULTI_FILTERS.phoneTypes.size === 1) {
        phoneEl.value = Array.from(MULTI_FILTERS.phoneTypes)[0];
      } else {
        phoneEl.value = 'ALL';
      }
    }
  }

  function updateFilterChipsUI() {
    ['SANS_SITE', 'SITE_MORT', 'SITE_FAIBLE', 'SITE_OK'].forEach(st => {
      const el = document.getElementById('chip_web_' + st);
      if (!el) return;
      const isActive = MULTI_FILTERS.webStatuses.has(st);
      el.classList.toggle('active', isActive);
      const icon = document.getElementById('icon_web_' + st);
      if (icon) {
        icon.textContent = isActive ? '✓' : (st === 'SANS_SITE' ? '🚀' : (st === 'SITE_MORT' ? '🔥' : (st === 'SITE_FAIBLE' ? '⚠️' : '✅')));
      }
    });

    const fbYesEl = document.getElementById('chip_fb_YES');
    if (fbYesEl) {
      const isYes = MULTI_FILTERS.facebook === 'WITH_FB';
      fbYesEl.classList.toggle('active', isYes);
      const icon = document.getElementById('icon_fb_YES');
      if (icon) icon.textContent = isYes ? '✓' : '📘';
    }
    const fbNoEl = document.getElementById('chip_fb_NO');
    if (fbNoEl) {
      const isNo = MULTI_FILTERS.facebook === 'WITHOUT_FB';
      fbNoEl.classList.toggle('active', isNo);
      const icon = document.getElementById('icon_fb_NO');
      if (icon) icon.textContent = isNo ? '✓' : '🚫';
    }

    const phWaEl = document.getElementById('chip_phone_WA');
    if (phWaEl) {
      const isWa = MULTI_FILTERS.phoneTypes.has('MOBILE_WHATSAPP');
      phWaEl.classList.toggle('active', isWa);
      const icon = document.getElementById('icon_phone_WA');
      if (icon) icon.textContent = isWa ? '✓' : '💬';
    }
    const phFixEl = document.getElementById('chip_phone_FIXE');
    if (phFixEl) {
      const isFix = MULTI_FILTERS.phoneTypes.has('FIXE_ONATEL');
      phFixEl.classList.toggle('active', isFix);
      const icon = document.getElementById('icon_phone_FIXE');
      if (icon) icon.textContent = isFix ? '✓' : '☎️';
    }

    const trPixEl = document.getElementById('chip_track_NO_PIXEL');
    if (trPixEl) {
      const isNoPix = MULTI_FILTERS.tracking.has('NO_PIXEL');
      trPixEl.classList.toggle('active', isNoPix);
      const icon = document.getElementById('icon_track_NO_PIXEL');
      if (icon) icon.textContent = isNoPix ? '✓' : '🎯';
    }
    const trGaEl = document.getElementById('chip_track_NO_GA');
    if (trGaEl) {
      const isNoGa = MULTI_FILTERS.tracking.has('NO_GA');
      trGaEl.classList.toggle('active', isNoGa);
      const icon = document.getElementById('icon_track_NO_GA');
      if (icon) icon.textContent = isNoGa ? '✓' : '📊';
    }

    const setCnt = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.textContent = val;
    };
    setCnt('cnt_web_SANS_SITE', LEADS.filter(b => !leadHasDedicatedWebsite(b)).length);
    setCnt('cnt_web_SITE_MORT', LEADS.filter(b => b.status === 'SITE_MORT').length);
    setCnt('cnt_web_SITE_FAIBLE', LEADS.filter(b => b.status === 'SITE_FAIBLE').length);
    setCnt('cnt_web_SITE_OK', LEADS.filter(b => b.status === 'SITE_OK').length);
    setCnt('cnt_fb_YES', LEADS.filter(leadHasFacebook).length);
    setCnt('cnt_fb_NO', LEADS.filter(b => !leadHasFacebook(b)).length);
    setCnt('cnt_phone_WA', LEADS.filter(b => b.phone_type === 'MOBILE_WHATSAPP').length);
    setCnt('cnt_phone_FIXE', LEADS.filter(b => b.phone_type === 'FIXE_ONATEL').length);
    setCnt('cnt_track_NO_PIXEL', LEADS.filter(b => !b.has_fb_pixel).length);
    setCnt('cnt_track_NO_GA', LEADS.filter(b => !b.has_google_analytics).length);

    const activeSummaryEl = document.getElementById('activeFiltersSummary');
    if (activeSummaryEl) {
      const badges = [];
      if (MULTI_FILTERS.webStatuses.has('SANS_SITE')) badges.push('🚀 Sans site web');
      if (MULTI_FILTERS.webStatuses.has('SITE_MORT')) badges.push('🔥 Site mort');
      if (MULTI_FILTERS.webStatuses.has('SITE_FAIBLE')) badges.push('⚠️ Site faible');
      if (MULTI_FILTERS.webStatuses.has('SITE_OK')) badges.push('✅ Site OK');
      if (MULTI_FILTERS.facebook === 'WITH_FB') badges.push('📘 Avec Page Facebook');
      if (MULTI_FILTERS.facebook === 'WITHOUT_FB') badges.push('🚫 Sans Facebook');
      if (MULTI_FILTERS.phoneTypes.has('MOBILE_WHATSAPP')) badges.push('💬 WhatsApp direct');
      if (MULTI_FILTERS.phoneTypes.has('FIXE_ONATEL')) badges.push('☎️ Fixe ONATEL');
      if (MULTI_FILTERS.tracking.has('NO_PIXEL')) badges.push('🎯 Sans Pixel Meta');
      if (MULTI_FILTERS.tracking.has('NO_GA')) badges.push('📊 Sans Google Analytics');

      if (badges.length > 0) {
        activeSummaryEl.style.display = 'flex';
        activeSummaryEl.innerHTML = `<div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">` +
          `<span>⚡ <strong>${badges.length} critère(s) combiné(s) actif(s) :</strong></span>` +
          badges.map(b => `<span class="status-pill" style="background:#e0f2fe; color:#0369a1; border:1px solid #bae6fd; font-size:0.72rem; padding:2px 8px;">${b}</span>`).join(' ') +
          `</div>` +
          `<button type="button" class="btn btn-reset" style="width:auto; padding:3px 10px; font-size:0.72rem; min-height:26px; border-color:#86efac; color:#166534;" onclick="clearMultiFilters(); render();">✕ Effacer les critères</button>`;
      } else {
        activeSummaryEl.style.display = 'none';
      }
    }
  }

  function quickFilterStatus(st) {
    if (st === 'ALL') {
      clearMultiFilters();
    } else if (st === 'FACEBOOK_SEUL') {
      MULTI_FILTERS.webStatuses.clear();
      MULTI_FILTERS.webStatuses.add('SANS_SITE');
      MULTI_FILTERS.facebook = 'WITH_FB';
    } else if (st === 'HAS_FACEBOOK') {
      MULTI_FILTERS.webStatuses.clear();
      MULTI_FILTERS.facebook = 'WITH_FB';
    } else if (st === 'SANS_SITE') {
      MULTI_FILTERS.webStatuses.clear();
      MULTI_FILTERS.webStatuses.add('SANS_SITE');
      MULTI_FILTERS.facebook = 'ALL';
    } else {
      MULTI_FILTERS.webStatuses.clear();
      MULTI_FILTERS.webStatuses.add(st);
      MULTI_FILTERS.facebook = 'ALL';
    }
    const statusEl = document.getElementById('statusFilter');
    if (statusEl) statusEl.value = st;
    highlightActiveKpi(st);
    render();
  }

  function quickFilterCrm(crmSt) {
    const crmEl = document.getElementById('crmFilter');
    if (crmEl) crmEl.value = crmSt;
    highlightActiveCrmStage(crmSt);
    render();
  }

  function resetFilters() {
    document.getElementById('searchInput').value = '';
    document.getElementById('cityFilter').value = 'ALL';
    document.getElementById('crmFilter').value = 'ALL';
    document.getElementById('sectorFilter').value = 'ALL';
    const hideToggle = document.getElementById('hideKnownLeadsToggle');
    if (hideToggle) hideToggle.checked = false;
    clearMultiFilters();
    highlightActiveKpi('ALL');
    highlightActiveCrmStage('ALL');
    render();
  }

  function populateFilterDropdowns() {
    const cityEl = document.getElementById('cityFilter');
    const crmEl = document.getElementById('crmFilter');
    const statusEl = document.getElementById('statusFilter');
    const sectorEl = document.getElementById('sectorFilter');
    const phoneEl = document.getElementById('phoneFilter');

    const curCity = cityEl.value || 'ALL';
    const curCrm = crmEl.value || 'ALL';
    const curStatus = statusEl.value || 'ALL';
    const curSector = sectorEl.value || 'ALL';
    const curPhone = phoneEl.value || 'ALL';

    const cities = Array.from(new Set(LEADS.map(b => b.city).filter(Boolean)));
    let cityHtml = `<option value="ALL">📍 Ville : Toutes (${LEADS.length})</option>`;
    cities.forEach(c => {
      const cnt = LEADS.filter(b => b.city === c).length;
      cityHtml += `<option value="${c}">📍 ${c} (${cnt})</option>`;
    });
    cityEl.innerHTML = cityHtml;
    cityEl.value = cities.includes(curCity) ? curCity : 'ALL';

    const pipeLeads = LEADS.filter(isLeadInPipeline);
    const untriagedCount = LEADS.filter(b => !isLeadInPipeline(b)).length;
    const countAContacter = pipeLeads.filter(b => normalizeStageGroup(b.crm_status) === 'A_CONTACTER').length;
    const countContacte = pipeLeads.filter(b => normalizeStageGroup(b.crm_status) === 'CONTACTE').length;
    const countClient = pipeLeads.filter(b => normalizeStageGroup(b.crm_status) === 'CLIENT').length;
    const countLivre = pipeLeads.filter(b => normalizeStageGroup(b.crm_status) === 'LIVRE').length;
    const countPaye = pipeLeads.filter(b => normalizeStageGroup(b.crm_status) === 'PAYE').length;
    const countIgnore = pipeLeads.filter(b => normalizeStageGroup(b.crm_status) === 'IGNORE').length;

    const untriagedOptHtml = CURRENT_VIEW !== 'crm'
      ? `<option value="NOUVEAU">🔍 Nouveaux non classés (${untriagedCount})</option>`
      : '';

    crmEl.innerHTML = `<option value="ALL">📋 Pipeline : Tout afficher (${CURRENT_VIEW === 'crm' ? pipeLeads.length : LEADS.length})</option>` +
      untriagedOptHtml +
      `<option value="A_CONTACTER">📥 1. À contacter (${countAContacter})</option>` +
      `<option value="CONTACTE">⏳ 2. Déjà contacté (${countContacte})</option>` +
      `<option value="CLIENT">📅 3. Rendez-vous (${countClient})</option>` +
      `<option value="LIVRE">🛠️ 4. Livré (${countLivre})</option>` +
      `<option value="PAYE">💰 5. Payé (${countPaye})</option>` +
      `<option value="IGNORE">❌ 6. Échec / Sans suite (${countIgnore})</option>`;
    crmEl.value = ['ALL', 'NOUVEAU', 'A_CONTACTER', 'CONTACTE', 'CLIENT', 'RDV', 'LIVRE', 'PAYE', 'IGNORE', 'ECHEC'].includes(curCrm) ? curCrm : 'ALL';

    const countStatus = (st) => LEADS.filter(b => b.status === st).length;
    const countHasFb = LEADS.filter(b => Boolean(b.facebook_url)).length;

    statusEl.innerHTML = `<option value="ALL">📊 Tous (${LEADS.length})</option><option value="CUSTOM">⚡ Filtres combinés</option><option value="SITE_MORT">🔥 Cassés (${countStatus('SITE_MORT')})</option><option value="FACEBOOK_SEUL">📱 FB Seul (${countStatus('FACEBOOK_SEUL')})</option><option value="HAS_FACEBOOK">📘 Avec FB (${countHasFb})</option><option value="SITE_FAIBLE">⚠️ Lents/Sans SSL (${countStatus('SITE_FAIBLE')})</option><option value="SANS_SITE">🚀 Sans site (${countStatus('SANS_SITE')})</option><option value="SITE_OK">✅ OK (${countStatus('SITE_OK')})</option>`;
    statusEl.value = curStatus;

    let sectorHtml = `<option value="ALL">🏢 Secteur : Tous (${LEADS.length})</option>`;
    Object.keys(SECTOR_LABELS).forEach(k => {
      if (k === 'all') return;
      const c = LEADS.filter(b => getLeadSectorKey(b) === k).length;
      sectorHtml += `<option value="${k}">${SECTOR_LABELS[k]} (${c})</option>`;
    });
    sectorEl.innerHTML = sectorHtml;
    sectorEl.value = (curSector in SECTOR_LABELS || curSector === 'ALL') ? curSector : 'ALL';

    const cMob = LEADS.filter(b => b.phone_type === 'MOBILE_WHATSAPP').length;
    const cFix = LEADS.filter(b => b.phone_type === 'FIXE_ONATEL').length;
    const cNone = LEADS.filter(b => b.phone_type === 'AUCUN').length;

    phoneEl.innerHTML = `<option value="ALL">📞 Tous contacts (${LEADS.length})</option><option value="MOBILE_WHATSAPP">💬 Mobile WhatsApp (${cMob})</option><option value="FIXE_ONATEL">☎️ Fixe ONATEL (${cFix})</option><option value="AUCUN">🌐 Web sans numéro (${cNone})</option>`;
    phoneEl.value = curPhone;
  }

  async function loadInitialLeads() {
    await checkAuthStatus();
    try {
      const res = await fetch('/api/leads');
      if (res.ok) {
        const data = await res.json();
        LEADS = Array.isArray(data.leads) ? data.leads : [];
        saveLocalBackup();
      } else {
        LEADS = loadLocalBackup();
      }
    } catch (e) {
      LEADS = loadLocalBackup();
    }
    populateFilterDropdowns();
    setSalesOffer(CURRENT_OFFER);
    switchAppView('scanner');
  }

  async function clearAllLeads() {
    if (!CURRENT_USER || CURRENT_USER.role !== 'admin') {
      showBanner("🔒 Action réservée à la direction de l'agence (Admin connecté).");
      return;
    }
    if (!confirm('Voulez-vous vraiment vider toute la liste actuelle ?')) return;
    LEADS = [];
    try { localStorage.removeItem(getStorageKey()); } catch (e) {}
    try {
      const res = await fetch('/api/clear', { method: 'POST' });
      if (!res.ok) {
        showBanner('Erreur lors de la suppression sur le serveur.');
        return;
      }
    } catch (e) {}
    populateFilterDropdowns();
    render();
    showBanner('🗑️ Liste vidée. Lance un nouveau scan quand tu veux !');
  }

  function exportLeadsCsv() {
    if (!LEADS.length) {
      showBanner('⚠️ Aucune entreprise à exporter.', true);
      return;
    }
    const headers = ['Nom', 'Ville', 'Quartier', 'Secteur', 'Telephone', 'Email', 'Type_Tel', 'Statut_Digital', 'Statut_CRM', 'Dans_Pipeline', 'RDV_Fait', 'Montant_Deal_FCFA', 'Notes_CRM', 'Score', 'Site_Web', 'Facebook', 'Google_Maps', 'Problemes'];
    const rows = LEADS.map(b => [
      b.name || '', b.city || '', b.neighborhood || '', b.sector || '', b.phone || '', b.email || '',
      b.phone_type || '', b.status || '', b.crm_status || 'A_CONTACTER',
      isLeadInPipeline(b) ? 'OUI' : 'NON', b.rdv_done ? 'OUI' : 'NON', b.deal_amount || 0, b.notes || '',
      b.score || 0, b.website || '', b.facebook_url || '', b.maps_url || '', (b.issues || []).join(' | ')
    ].map(v => `"${String(v).replace(/"/g, '""')}"`).join(','));
    const csvContent = '\\uFEFF' + headers.join(',') + '\\n' + rows.join('\\n');
    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `prospectbf_${new Date().toISOString().slice(0,10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function runSingleAudit() {
    const name = document.getElementById('customName').value.trim() || 'Entreprise Auditée';
    const sector_key = document.getElementById('customSector').value;
    const website = document.getElementById('customUrl').value.trim();
    const phone = document.getElementById('customPhone').value.trim();

    const btn = document.getElementById('btnAuditSingle');
    btn.disabled = true;
    showBanner(`⏳ Audit en direct de "${name}"...`);

    try {
      const res = await fetch('/api/audit-single', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, sector_key, website, phone })
      });
      const data = await res.json();
      if (data.lead) {
        LEADS = dedupeAndSortLeads([data.lead, ...LEADS]);
        saveLocalBackup();
        resetFilters();
        populateFilterDropdowns();
        render();
        showBanner(`✅ "${data.lead.name}" audité -> Score ${data.lead.score}/100 (${data.lead.status})`);
      }
    } catch (err) {
      showBanner(`❌ Erreur : ${err}`, true);
    } finally { btn.disabled = false; }
  }

  async function runCityScan() {
    const city = document.getElementById('scanCity').value;
    const sector = document.getElementById('scanSector').value;
    const limit = parseInt(document.getElementById('scanLimit').value, 10);

    const btn = document.getElementById('btnScanCity');
    btn.disabled = true;
    showBanner(`⏳ Audit de ${limit} entreprises (${city.toUpperCase()}) en cours...`);

    try {
      const res = await fetch('/api/scan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ city, sector, limit })
      });
      const data = await res.json();
      if (data.leads) {
        const prevCount = LEADS.length;
        LEADS = dedupeAndSortLeads([...LEADS, ...data.leads]);
        saveLocalBackup();
        if (sector && sector !== 'all') {
          const secEl = document.getElementById('sectorFilter');
          if (secEl) secEl.value = sector;
        }
        populateFilterDropdowns();
        render();
        const added = Math.max(0, LEADS.length - prevCount);
        const scannedCount = typeof data.scan_count === 'number' ? data.scan_count : data.leads.length;
        const alreadyKnown = typeof data.known_in_scan === 'number' ? data.known_in_scan : LEADS.filter(isLeadInPipeline).length;
        showBanner(`✅ Scan terminé : ${scannedCount} auditées (${added} nouvelles, ${alreadyKnown} déjà connues dans ton Pipeline) !`);
      }
    } catch (err) {
      showBanner(`❌ Erreur : ${err}`, true);
    } finally { btn.disabled = false; }
  }

  function switchAppView(view) {
    CURRENT_VIEW = view;
    ['all', 'scanner', 'crm', 'offers'].forEach(v => {
      const btn = document.getElementById('navView_' + v);
      if (btn) btn.classList.toggle('active', v === view);
    });
    const secAcq = document.getElementById('sectionAcquisition');
    const secOff = document.getElementById('sectionOffers');
    const secPros = document.getElementById('sectionProspects');
    const kpiStrip = document.getElementById('kpiDiagnosticStrip');
    const crmStrip = document.getElementById('crmPipelineStrip');
    const finBanner = document.getElementById('pipelineFinanceBanner');
    const toggleBar = document.getElementById('radarMemoryToggleBar');
    const ctxTitle = document.getElementById('workspaceContextTitle');
    const quickGoBtn = document.getElementById('btnQuickGoPipeline');
    const badgeEl = document.getElementById('sectionProspectsBadge');
    const headEl = document.getElementById('sectionProspectsHeadingText');

    if (view === 'scanner') {
      if (secAcq) secAcq.style.display = 'block';
      if (secOff) secOff.style.display = 'none';
      if (secPros) secPros.style.display = 'block';
      if (kpiStrip) kpiStrip.style.display = 'grid';
      if (crmStrip) crmStrip.style.display = 'none';
      if (finBanner) finBanner.style.display = 'none';
      if (toggleBar) toggleBar.style.display = 'flex';
      if (badgeEl) badgeEl.textContent = 'RADAR DE SCAN';
      if (headEl) headEl.textContent = 'Résultats du Scan — Séparez qui vous voulez contacter';
      if (ctxTitle) ctxTitle.innerHTML = "<strong>🔍 Espace Actif : Radar de Scan & Triage</strong> — Appuyez sur <strong>« ➕ Mettre dans À contacter »</strong> sur les entreprises qui vous intéressent pour les envoyer dans votre Pipeline Commercial.";
      if (quickGoBtn) {
        quickGoBtn.textContent = '📂 Aller dans Mon Pipeline Commercial →';
        quickGoBtn.setAttribute('onclick', "switchAppView('crm')");
      }
      switchTab('scan');
    } else if (view === 'offers') {
      if (secAcq) secAcq.style.display = 'none';
      if (secOff) secOff.style.display = 'block';
      if (secPros) secPros.style.display = 'block';
      if (kpiStrip) kpiStrip.style.display = 'none';
      if (crmStrip) crmStrip.style.display = 'none';
      if (finBanner) finBanner.style.display = 'none';
      if (toggleBar) toggleBar.style.display = 'flex';
      if (ctxTitle) ctxTitle.innerHTML = "<strong>🎯 Espace Actif : Stratégie d'Offres & Pitchs</strong> — Adaptez vos messages WhatsApp (Site Web, Facebook Ads, Google Maps).";
    } else if (view === 'crm') {
      if (secAcq) secAcq.style.display = 'none';
      if (secOff) secOff.style.display = 'none';
      if (secPros) secPros.style.display = 'block';
      if (kpiStrip) kpiStrip.style.display = 'none';
      if (crmStrip) crmStrip.style.display = 'grid';
      if (finBanner) finBanner.style.display = 'grid';
      if (toggleBar) toggleBar.style.display = 'none';
      if (badgeEl) badgeEl.textContent = 'MON PIPELINE COMMERCIAL';
      if (headEl) headEl.textContent = 'Suivi de vos Prospects : À contacter → Contacté → RDV (Fait) → Livré → Payé';
      if (ctxTitle) ctxTitle.innerHTML = "<strong>📋 Espace Actif : Mon Pipeline Commercial</strong> — Gérez vos prospects mis de côté : contactez-les, notez les RDV (RDV Fait), suivez les livraisons, les paiements (FCFA) et les échecs.";
      if (quickGoBtn) {
        quickGoBtn.textContent = '🔍 Retour au Radar de Scan';
        quickGoBtn.setAttribute('onclick', "switchAppView('scanner')");
      }
    } else {
      if (secAcq) secAcq.style.display = 'block';
      if (secOff) secOff.style.display = 'block';
      if (secPros) secPros.style.display = 'block';
      if (kpiStrip) kpiStrip.style.display = 'grid';
      if (crmStrip) crmStrip.style.display = 'grid';
      if (finBanner) finBanner.style.display = 'grid';
      if (toggleBar) toggleBar.style.display = 'flex';
      if (badgeEl) badgeEl.textContent = 'VUE GLOBALE';
      if (headEl) headEl.textContent = 'Radar de Scan & Pipeline Commercial Complet';
      if (ctxTitle) ctxTitle.innerHTML = "<strong>🏠 Espace Actif : Vue Globale SaaS</strong> — Acquisition, Stratégie d'Offre, Diagnostic & Pipeline CRM complet";
    }
    populateFilterDropdowns();
    render();
    if (typeof window.scrollTo === 'function') window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function openLeadDetail(name, city) {
    const lead = LEADS.find(l => {
      const sameName = (l.name || '').trim().toLowerCase() === (name || '').trim().toLowerCase();
      if (city && city !== 'Audit Direct') {
        return sameName && (l.city || '').trim().toLowerCase() === city.trim().toLowerCase();
      }
      return sameName;
    });
    if (!lead) return;
    ACTIVE_DETAIL_LEAD = lead;
    renderLeadDetailDrawer();
    const modal = document.getElementById('leadDetailModal');
    if (modal) modal.classList.add('open');
  }

  function closeLeadDetail() {
    ACTIVE_DETAIL_LEAD = null;
    const modal = document.getElementById('leadDetailModal');
    if (modal) modal.classList.remove('open');
  }

  if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
    window.addEventListener('keydown', (ev) => {
      if (ev && ev.key === 'Escape' && ACTIVE_DETAIL_LEAD) closeLeadDetail();
    });
  }

  function renderLeadDetailDrawer() {
    const b = ACTIVE_DETAIL_LEAD;
    if (!b) return;
    const shortSector = SECTOR_LABELS[b.sector_key] || b.sector || 'Entreprise B2B';
    document.getElementById('drawerSectorBadge').textContent = shortSector;
    document.getElementById('drawerBizName').textContent = b.name || 'Entreprise';
    document.getElementById('drawerLocation').textContent = `📍 ${b.city || 'Burkina Faso'} • ${b.neighborhood || 'Quartier non précisé'}`;

    const scoreColor = b.score >= 85 ? '#dc2626' : (b.score >= 70 ? '#d97706' : '#16a34a');
    const fullUrl = b.website ? (b.website.startsWith('http') ? b.website : 'http://' + b.website) : '';
    const mapsUrl = b.maps_url || `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent((b.name || '') + ' ' + (b.city || 'Burkina Faso'))}`;
    const currentPitch = generatePitch(b, CURRENT_OFFER, CURRENT_PITCH_ANGLE, CURRENT_PITCH_STAGE);
    const words = currentPitch.trim().split(/\\s+/).filter(Boolean).length;
    const readSec = Math.max(5, Math.round((words / 180) * 60));
    const pitchStatsText = `${words} mots • ~${readSec}s de lecture • Réponse en 1 clic`;
    const encName = safeAttrEnc(b.name || '');
    const encCity = safeAttrEnc(b.city || '');
    const encPhone = safeAttrEnc(b.phone || '');
    const stageGroup = normalizeStageGroup(b.crm_status);
    const inPipe = isLeadInPipeline(b);

    let waUrl = '';
    if (b.phone_type === 'MOBILE_WHATSAPP' && b.phone && b.phone !== 'Non renseigné') {
      waUrl = buildWhatsAppUrl(b.phone, currentPitch);
    }

    const contactActionHtml = waUrl
      ? `<a id="drawerWaBtn" class="btn btn-wa" href="${waUrl}" target="_blank" onclick="const customTxt=document.getElementById('drawerCustomPitch').value; this.href=buildWhatsAppUrl(decodeURIComponent('${encPhone}'), customTxt); setLeadCrmStatus(decodeURIComponent('${encName}'), 'CONTACTE', decodeURIComponent('${encCity}'));">💬 Envoyer WhatsApp (& Marquer Contacté)</a>`
      : (b.tel_url ? `<a class="btn btn-call" href="${b.tel_url}" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'CONTACTE', decodeURIComponent('${encCity}'));">📞 Appeler</a>` : `<span class="btn btn-reset" style="opacity:0.55; cursor:not-allowed;">📵 Aucun numéro</span>`);

    const rdvToggleDrawerHtml = stageGroup === 'CLIENT'
      ? `<div style="margin-top:10px;"><button type="button" class="btn-stage-action btn-stage-rdv-check ${b.rdv_done ? 'done' : ''}" style="width:100%;" onclick="toggleLeadRdvDone(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'))">${b.rdv_done ? '☑️ Rendez-vous Fait ! (Validé)' : '⬜ Cliquer ici quand le Rendez-vous est Fait'}</button></div>`
      : '';

    const visitorAlertHtml = !CURRENT_USER
      ? `<div style="background:#eff6ff; border:1px solid #bfdbfe; color:#1e40af; padding:10px 14px; border-radius:8px; font-size:0.8rem; font-weight:700; margin-bottom:14px;">🔒 Vous consultez ce prospect en mode visiteur. <button type="button" onclick="openAuthModal('login')" style="background:none; border:none; color:#2563eb; text-decoration:underline; font-weight:800; cursor:pointer; padding:0;">Connectez-vous</button> pour gérer votre pipeline commercial.</div>`
      : '';

    const bodyEl = document.getElementById('drawerBodyContent');
    bodyEl.innerHTML = `${visitorAlertHtml}${claimAlertHtml}<div class="drawer-section"><div class="drawer-section-title"><span>📊 Diagnostic Technique Complet</span><span class="status-pill st-${b.status}">${(b.status || '').replace('_', ' ')}</span></div>` +
      `<div class="diag-grid">` +
      `<div class="diag-item"><span class="diag-lbl">Score d'Opportunité</span><span class="diag-val" style="color:${scoreColor}; font-size:1.1rem;">${b.score || 0} / 100</span></div>` +
      `<div class="diag-item"><span class="diag-lbl">Temps de Réponse HTTP</span><span class="diag-val">${b.response_ms ? b.response_ms + ' ms' : 'Non mesuré / Sans site'}</span></div>` +
      `<div class="diag-item"><span class="diag-lbl">Pixel Meta (Facebook Ads)</span><span class="diag-val" style="color:${b.has_fb_pixel ? '#059669' : '#dc2626'}">${b.has_fb_pixel ? '✅ Détecté' : '❌ Absent'}</span></div>` +
      `<div class="diag-item"><span class="diag-lbl">Google Analytics / Tag</span><span class="diag-val" style="color:${b.has_google_analytics ? '#059669' : '#dc2626'}">${b.has_google_analytics ? '✅ Détecté' : '❌ Absent'}</span></div>` +
      `<div class="diag-item"><span class="diag-lbl">Téléphone & Email</span><span class="diag-val">📞 ${escapeHtml(b.phone || 'Non renseigné')}${b.email ? ' • ✉️ ' + escapeHtml(b.email) : ''}</span></div>` +
      `<div class="diag-item"><span class="diag-lbl">Site Web Officiel</span><span class="diag-val">${fullUrl ? `<a href="${fullUrl}" target="_blank" style="color:#2563eb;">🌐 ${escapeHtml(shortDomain(b.website))}</a>` : '❌ Aucun site web'}</span></div>` +
      `</div><div class="issues" style="margin-top:12px;">${(b.issues || []).map(iss => `<span class="issue-tag">⚠️ ${escapeHtml(iss)}</span>`).join('') || '<span style="font-size:0.78rem; color:var(--muted);">Aucun problème critique signalé.</span>'}</div></div>` +
      `<div class="drawer-section"><div class="drawer-section-title"><span>📋 Étape Pipeline Commercial (6 Étapes)</span><span style="font-size:0.7rem; color:#2563eb; font-weight:700;">${inPipe ? '📌 Dans Mon Pipeline (' + formatRelativeTime(b.updated_at) + ')' : '🔍 Non classé'}</span></div>` +
      `${!inPipe ? `<button type="button" class="btn-add-pipeline" style="width:100%; margin-bottom:10px;" onclick="addLeadToPipeline(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'))">➕ Mettre dans « À contacter » (Mon Pipeline)</button>` : ''}` +
      `<div class="crm-bar" style="margin-top:0;">` +
      `<button type="button" class="crm-pill crm-a-contacter ${inPipe && stageGroup === 'A_CONTACTER' ? 'active' : ''}" onclick="addLeadToPipeline(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'))">📥 1. À contacter</button>` +
      `<button type="button" class="crm-pill crm-contacte ${stageGroup === 'CONTACTE' ? 'active' : ''}" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'CONTACTE', decodeURIComponent('${encCity}'))">⏳ 2. Contacté</button>` +
      `<button type="button" class="crm-pill crm-client ${stageGroup === 'CLIENT' ? 'active' : ''}" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'CLIENT', decodeURIComponent('${encCity}'))">📅 3. RDV</button>` +
      `<button type="button" class="crm-pill crm-livre ${stageGroup === 'LIVRE' ? 'active' : ''}" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'LIVRE', decodeURIComponent('${encCity}'))">🛠️ 4. Livré</button>` +
      `<button type="button" class="crm-pill crm-paye ${stageGroup === 'PAYE' ? 'active' : ''}" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'PAYE', decodeURIComponent('${encCity}'))">💰 5. Payé</button>` +
      `<button type="button" class="crm-pill crm-ignore ${stageGroup === 'IGNORE' ? 'active' : ''}" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'IGNORE', decodeURIComponent('${encCity}'))">❌ 6. Échec</button>` +
      `</div>${rdvToggleDrawerHtml}` +
      `<div class="pipeline-meta-grid" style="margin-top:12px;">` +
      `<div class="meta-input-group"><label>💵 Montant du Deal (FCFA)</label><input type="number" min="0" step="5000" value="${b.deal_amount || ''}" placeholder="Ex: 75000" onchange="updateLeadMeta(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'), {deal_amount: this.value})" />` +
      `<div class="quick-amount-chips"><button type="button" class="chip-btn-amount ${parseInt(b.deal_amount || 0, 10) === 50000 ? 'active' : ''}" onclick="updateLeadMeta(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'), {deal_amount: 50000})" title="Pack 50 000 FCFA">50k F</button><button type="button" class="chip-btn-amount ${parseInt(b.deal_amount || 0, 10) === 100000 ? 'active' : ''}" onclick="updateLeadMeta(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'), {deal_amount: 100000})" title="Pack 100 000 FCFA">100k F</button><button type="button" class="chip-btn-amount ${parseInt(b.deal_amount || 0, 10) === 150000 ? 'active' : ''}" onclick="updateLeadMeta(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'), {deal_amount: 150000})" title="Pack 150 000 FCFA">150k F</button><button type="button" class="chip-btn-amount ${parseInt(b.deal_amount || 0, 10) === 250000 ? 'active' : ''}" onclick="updateLeadMeta(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'), {deal_amount: 250000})" title="Pack 250 000 FCFA">250k F</button></div></div>` +
      `<div class="meta-input-group"><label>📝 Notes & Suivi client</label><input type="text" value="${escapeHtml(b.notes || '')}" placeholder="Ex: Rappeler jeudi à 15h, intéressé par pack Web..." onchange="updateLeadMeta(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'), {notes: this.value})" /></div>` +
      `</div></div>` +
      `<div class="drawer-section"><div class="drawer-section-title"><span>💬 Studio Copywriting & Psychologie de Vente</span><span id="drawerPitchStats" class="pitch-stats-badge">${pitchStatsText}</span></div>` +
      `<div class="copy-strip-label">🎯 1. Service commercial à proposer</div>` +
      `<div style="display:grid; grid-template-columns:repeat(3, 1fr); gap:6px; margin-bottom:12px;">` +
      `<button type="button" id="drawerOfferBtn_WEB" class="angle-pill-btn ${CURRENT_OFFER==='WEB'?'active-angle':''}" onclick="setSalesOffer('WEB')">🌐 Site Web</button>` +
      `<button type="button" id="drawerOfferBtn_ADS" class="angle-pill-btn ${CURRENT_OFFER==='ADS'?'active-angle':''}" onclick="setSalesOffer('ADS')">📱 Meta Ads</button>` +
      `<button type="button" id="drawerOfferBtn_MAPS" class="angle-pill-btn ${CURRENT_OFFER==='MAPS'?'active-angle':''}" onclick="setSalesOffer('MAPS')">📍 Google Maps</button>` +
      `</div>` +
      `<div class="copy-strip-label">🧠 2. Angle Psychologique (Chris Voss, Cialdini, Klaff)</div>` +
      `<div class="angle-pills-grid">` +
      `<button type="button" id="btnAngle_voss" class="angle-pill-btn ${CURRENT_PITCH_ANGLE==='voss'?'active-angle':''}" onclick="setPitchAngle('voss')" title="Empathie tactique & question orientée Non">🧠 Voss (No-oriented)</button>` +
      `<button type="button" id="btnAngle_diagnostic" class="angle-pill-btn ${CURRENT_PITCH_ANGLE==='diagnostic'?'active-angle':''}" onclick="setPitchAngle('diagnostic')" title="Diagnostic technique & choc chiffré">🔬 Diagnostic Choc</button>` +
      `<button type="button" id="btnAngle_reciprocity" class="angle-pill-btn ${CURRENT_PITCH_ANGLE==='reciprocity'?'active-angle':''}" onclick="setPitchAngle('reciprocity')" title="Maquette ou audit vidéo offert">🎁 Réciprocité</button>` +
      `<button type="button" id="btnAngle_social_proof" class="angle-pill-btn ${CURRENT_PITCH_ANGLE==='social_proof'?'active-angle':''}" onclick="setPitchAngle('social_proof')" title="Pression concurrentielle locale & FCFA">👥 Preuve Sociale</button>` +
      `</div>` +
      `<div class="copy-strip-label">📅 3. Étape de la Séquence Commerciale</div>` +
      `<div class="stage-tabs-row">` +
      `<button type="button" id="btnStage_j0" class="stage-tab-btn ${CURRENT_PITCH_STAGE==='j0'?'active-stage':''}" onclick="setPitchStage('j0')">⚡ J0 Premier Contact</button>` +
      `<button type="button" id="btnStage_j2" class="stage-tab-btn ${CURRENT_PITCH_STAGE==='j2'?'active-stage':''}" onclick="setPitchStage('j2')">⏳ J+2 Rappel Doux</button>` +
      `<button type="button" id="btnStage_j5" class="stage-tab-btn ${CURRENT_PITCH_STAGE==='j5'?'active-stage':''}" onclick="setPitchStage('j5')">📈 J+5 Impact FCFA</button>` +
      `<button type="button" id="btnStage_j8" class="stage-tab-btn ${CURRENT_PITCH_STAGE==='j8'?'active-stage':''}" onclick="setPitchStage('j8')">🚪 J+8 Clôture Polie</button>` +
      `</div>` +
      `<textarea id="drawerCustomPitch" rows="6" style="width:100%; font-size:0.84rem; line-height:1.45; margin-bottom:10px; border-radius:8px;" oninput="const waEl=document.getElementById('drawerWaBtn'); if(waEl) waEl.href=buildWhatsAppUrl(decodeURIComponent('${encPhone}'), this.value);">${escapeHtml(currentPitch)}</textarea>` +
      `<div style="display:grid; grid-template-columns: 1fr 1fr; gap:8px;">${contactActionHtml}<button type="button" class="btn btn-copy" onclick="copyPitchText(this, document.getElementById('drawerCustomPitch').value)">📋 Copier le message</button><button type="button" class="btn btn-reset" style="border-color:#10b981; color:#047857;" onclick="sharePitch(decodeURIComponent('${encName}'), decodeURIComponent('${encPhone}'), document.getElementById('drawerCustomPitch').value)">📲 Partager</button><a class="btn btn-maps" href="${mapsUrl}" target="_blank">📍 Google Maps</a>${b.facebook_url ? `<a class="btn btn-fb" href="${b.facebook_url}" target="_blank">📘 Page Facebook</a>` : ''}</div></div>`;
  }

  function updateKPIs() {
    document.getElementById('kpiTotal').textContent = LEADS.length;
    document.getElementById('kpiDead').textContent = LEADS.filter(b => b.status === 'SITE_MORT').length;
    document.getElementById('kpiFb').textContent = LEADS.filter(b => b.status === 'FACEBOOK_SEUL').length;
    document.getElementById('kpiWeak').textContent = LEADS.filter(b => b.status === 'SITE_FAIBLE').length;
    document.getElementById('kpiNoSite').textContent = LEADS.filter(b => b.status === 'SANS_SITE').length;

    const pipeLeads = LEADS.filter(isLeadInPipeline);
    const todoLeads = pipeLeads.filter(b => normalizeStageGroup(b.crm_status) === 'A_CONTACTER');
    const contacteLeads = pipeLeads.filter(b => normalizeStageGroup(b.crm_status) === 'CONTACTE');
    const rdvLeads = pipeLeads.filter(b => normalizeStageGroup(b.crm_status) === 'CLIENT');
    const livreLeads = pipeLeads.filter(b => normalizeStageGroup(b.crm_status) === 'LIVRE');
    const payeLeads = pipeLeads.filter(b => normalizeStageGroup(b.crm_status) === 'PAYE');
    const ignoreLeads = pipeLeads.filter(b => normalizeStageGroup(b.crm_status) === 'IGNORE');

    const cAll = document.getElementById('crmCount_ALL');
    if (cAll) cAll.textContent = pipeLeads.length;
    const cAC = document.getElementById('crmCount_A_CONTACTER');
    if (cAC) cAC.textContent = todoLeads.length;
    const cCo = document.getElementById('crmCount_CONTACTE');
    if (cCo) cCo.textContent = contacteLeads.length;
    const cCl = document.getElementById('crmCount_CLIENT');
    if (cCl) cCl.textContent = rdvLeads.length;
    const cLi = document.getElementById('crmCount_LIVRE');
    if (cLi) cLi.textContent = livreLeads.length;
    const cPa = document.getElementById('crmCount_PAYE');
    if (cPa) cPa.textContent = payeLeads.length;
    const cIg = document.getElementById('crmCount_IGNORE');
    if (cIg) cIg.textContent = ignoreLeads.length;

    const paidTotalFcfa = payeLeads.reduce((acc, b) => acc + (parseInt(b.deal_amount || 0, 10) || 0), 0);
    const pendingTotalFcfa = [...rdvLeads, ...livreLeads].reduce((acc, b) => acc + (parseInt(b.deal_amount || 0, 10) || 0), 0);

    const finPaidEl = document.getElementById('finPaidTotal');
    if (finPaidEl) finPaidEl.textContent = formatFcfa(paidTotalFcfa);
    const finPaidCntEl = document.getElementById('finPaidCount');
    if (finPaidCntEl) finPaidCntEl.textContent = `${payeLeads.length} affaire(s) conclue(s) et payée(s)`;

    const finPendEl = document.getElementById('finPendingTotal');
    if (finPendEl) finPendEl.textContent = formatFcfa(pendingTotalFcfa);
    const finPendCntEl = document.getElementById('finPendingCount');
    if (finPendCntEl) finPendCntEl.textContent = `${livreLeads.length} livré(s) • ${rdvLeads.length} en RDV`;

    const finActEl = document.getElementById('finActiveCount');
    if (finActEl) finActEl.textContent = pipeLeads.length;
    const finTodoEl = document.getElementById('finTodoCount');
    if (finTodoEl) finTodoEl.textContent = `📥 ${todoLeads.length} à contacter • ⏳ ${contacteLeads.length} contacté(s)`;

    const navAll = document.getElementById('navCountAll');
    if (navAll) navAll.textContent = LEADS.length;
    const navCrm = document.getElementById('navCountCrm');
    if (navCrm) navCrm.textContent = pipeLeads.length;

    const totalPipelineFcfa = pipeLeads.reduce((acc, b) => acc + (parseInt(b.deal_amount || 0, 10) || 0), 0);
    const closedDeals = payeLeads.length + ignoreLeads.length;
    const winRate = closedDeals > 0
      ? Math.round((payeLeads.length / closedDeals) * 100)
      : (pipeLeads.length > 0 ? Math.round((payeLeads.length / pipeLeads.length) * 100) : 0);
    const finConvEl = document.getElementById('finConversionRate');
    if (finConvEl) finConvEl.textContent = `${winRate}% closing`;
    const finPotEl = document.getElementById('finPotentialTotal');
    if (finPotEl) finPotEl.textContent = `Pipeline : ${formatFcfa(totalPipelineFcfa)} (${payeLeads.length} payé / ${pipeLeads.length} total)`;

    const knownSummaryEl = document.getElementById('radarKnownSummaryText');
    if (knownSummaryEl) {
      knownSummaryEl.textContent = pipeLeads.length > 0
        ? `📌 ${pipeLeads.length} entreprise(s) déjà enregistrée(s) dans ton Pipeline / Échecs`
        : `Aucune entreprise dans ton Pipeline pour l'instant`;
    }
  }

  function buildRadarMemoryBannerHtml(b, encName, encCity) {
    const stageGroup = normalizeStageGroup(b.crm_status);
    const relTime = formatRelativeTime(b.updated_at);
    const amtTxt = b.deal_amount ? ` • 💵 ${formatFcfa(b.deal_amount)}` : '';
    const noteTxt = b.notes ? ` • 📝 « ${escapeHtml(b.notes)} »` : '';
    let label = '';
    let cssClass = 'mem-A_CONTACTER';

    if (stageGroup === 'A_CONTACTER') {
      label = `📌 Déjà dans ta liste : À CONTACTER (${relTime})${amtTxt}${noteTxt}`;
      cssClass = 'mem-A_CONTACTER';
    } else if (stageGroup === 'CONTACTE') {
      label = `⏳ Déjà contacté — En attente de réponse (${relTime})${amtTxt}${noteTxt}`;
      cssClass = 'mem-CONTACTE';
    } else if (stageGroup === 'CLIENT') {
      label = `📅 En Rendez-vous ${b.rdv_done ? '• ☑️ RDV FAIT' : '• ⏳ RDV à faire'} (${relTime})${amtTxt}${noteTxt}`;
      cssClass = 'mem-CLIENT';
    } else if (stageGroup === 'LIVRE') {
      label = `🛠️ Projet Livré — En attente de paiement (${relTime})${amtTxt}${noteTxt}`;
      cssClass = 'mem-LIVRE';
    } else if (stageGroup === 'PAYE') {
      label = `💰 Affaire déjà conclue & Payée ! (${relTime})${amtTxt}${noteTxt}`;
      cssClass = 'mem-PAYE';
    } else if (stageGroup === 'IGNORE') {
      label = `❌ Déjà tenté : Échec / Sans suite (${relTime})${noteTxt}`;
      cssClass = 'mem-IGNORE';
    }

    const actionRight = stageGroup === 'IGNORE'
      ? `<button type="button" class="btn btn-reset" style="width:auto; min-height:34px; padding:5px 12px; font-size:0.75rem;" onclick="addLeadToPipeline(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'))">🔄 Remettre dans À contacter</button>`
      : `<button type="button" class="btn btn-reset" style="width:auto; min-height:34px; padding:5px 12px; font-size:0.75rem; border-color:currentColor;" onclick="openLeadInPipeline(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'))">📂 Gérer dans Mon Pipeline →</button>`;

    return `<div class="pipeline-memory-banner ${cssClass}"><span>${label}</span>${actionRight}</div>`;
  }

  function buildPipelineStageBoxHtml(b, encName, encCity, dynamicWaUrl) {
    const stageGroup = normalizeStageGroup(b.crm_status);
    const relTime = formatRelativeTime(b.updated_at);
    let stageHeader = '';
    let stageBtns = '';

    if (stageGroup === 'A_CONTACTER') {
      stageHeader = `<span>📥 Étape 1 : À contacter — Ajouté ${escapeHtml(relTime)}</span>`;
      const waStepBtn = dynamicWaUrl
        ? `<a class="btn-stage-action btn-wa" href="${dynamicWaUrl}" target="_blank" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'CONTACTE', decodeURIComponent('${encCity}'))">💬 Contacter sur WhatsApp (& Passer en Contacté)</a>`
        : '';
      stageBtns = `${waStepBtn}` +
        `<button type="button" class="btn-stage-action btn-stage-contacte" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'CONTACTE', decodeURIComponent('${encCity}'))">📨 Marquer comme « Déjà contacté » →</button>` +
        `<button type="button" class="btn-stage-action btn-stage-echec" style="flex:0 0 auto;" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'IGNORE', decodeURIComponent('${encCity}'))">❌ Échec</button>`;
    } else if (stageGroup === 'CONTACTE') {
      stageHeader = `<span>⏳ Étape 2 : Déjà contacté (${escapeHtml(relTime)}) — Quel est le résultat ?</span>`;
      stageBtns = `<button type="button" class="btn-stage-action btn-stage-rdv" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'CLIENT', decodeURIComponent('${encCity}'))">📅 Rendez-vous décroché !</button>` +
        `<button type="button" class="btn-stage-action btn-stage-echec" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'IGNORE', decodeURIComponent('${encCity}'))">❌ Échec / Sans suite</button>`;
    } else if (stageGroup === 'CLIENT') {
      stageHeader = `<span>📅 Étape 3 : Rendez-vous (${escapeHtml(relTime)}) — Validez le RDV puis livrez :</span>`;
      stageBtns = `<button type="button" class="btn-stage-action btn-stage-rdv-check ${b.rdv_done ? 'done' : ''}" onclick="toggleLeadRdvDone(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'))">${b.rdv_done ? '☑️ RDV Fait ! (Validé)' : '⬜ Marquer « RDV Fait »'}</button>` +
        `<button type="button" class="btn-stage-action btn-stage-livre" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'LIVRE', decodeURIComponent('${encCity}'))">🛠️ Passer en « Livré » →</button>` +
        `<button type="button" class="btn-stage-action btn-stage-echec" style="flex:0 0 auto;" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'IGNORE', decodeURIComponent('${encCity}'))">❌ Échec</button>`;
    } else if (stageGroup === 'LIVRE') {
      stageHeader = `<span>🛠️ Étape 4 : Projet Livré (${escapeHtml(relTime)}) — En attente du paiement :</span>`;
      stageBtns = `<button type="button" class="btn-stage-action btn-stage-paye" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'PAYE', decodeURIComponent('${encCity}'))">💰 Marquer comme « Payé & Encaissé ! »</button>` +
        `<button type="button" class="btn-stage-action btn-stage-echec" style="flex:0 0 auto;" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'CLIENT', decodeURIComponent('${encCity}'))">↩️ Retour RDV</button>`;
    } else if (stageGroup === 'PAYE') {
      stageHeader = `<span style="color:#047857; font-weight:700;">🎉 Étape 5 : Affaire conclue, livrée & Payée (${escapeHtml(relTime)}) !</span>`;
      stageBtns = `<span class="status-pill st-SITE_OK" style="padding:8px 14px; font-size:0.82rem;">💰 Encaissé : ${formatFcfa(b.deal_amount || 0)}</span>`;
    } else {
      stageHeader = `<span style="color:#b91c1c; font-weight:700;">❌ Étape 6 : Échec / Sans suite (${escapeHtml(relTime)})</span>`;
      stageBtns = `<button type="button" class="btn-stage-action btn-stage-contacte" onclick="addLeadToPipeline(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'))">🔄 Repêcher → Remettre dans « À contacter »</button>`;
    }

    const sixPillsHtml = `<div class="crm-bar" role="group" aria-label="Statut CRM">` +
      `<button type="button" class="crm-pill crm-a-contacter ${stageGroup === 'A_CONTACTER' ? 'active' : ''}" onclick="addLeadToPipeline(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'))">📥 À contacter</button>` +
      `<button type="button" class="crm-pill crm-contacte ${stageGroup === 'CONTACTE' ? 'active' : ''}" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'CONTACTE', decodeURIComponent('${encCity}'))">⏳ Contacté</button>` +
      `<button type="button" class="crm-pill crm-client ${stageGroup === 'CLIENT' ? 'active' : ''}" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'CLIENT', decodeURIComponent('${encCity}'))">📅 RDV ${b.rdv_done ? '☑️' : ''}</button>` +
      `<button type="button" class="crm-pill crm-livre ${stageGroup === 'LIVRE' ? 'active' : ''}" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'LIVRE', decodeURIComponent('${encCity}'))">🛠️ Livré</button>` +
      `<button type="button" class="crm-pill crm-paye ${stageGroup === 'PAYE' ? 'active' : ''}" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'PAYE', decodeURIComponent('${encCity}'))">💰 Payé</button>` +
      `<button type="button" class="crm-pill crm-ignore ${stageGroup === 'IGNORE' ? 'active' : ''}" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'IGNORE', decodeURIComponent('${encCity}'))">❌ Échec</button>` +
      `</div>`;

    const metaInputsHtml = `<div class="pipeline-meta-grid">` +
      `<div class="meta-input-group"><label>💵 Montant du Deal (FCFA)</label><input type="number" min="0" step="5000" value="${b.deal_amount || ''}" placeholder="Ex: 75000 FCFA" onchange="updateLeadMeta(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'), {deal_amount: this.value})" />` +
      `<div class="quick-amount-chips"><button type="button" class="chip-btn-amount ${parseInt(b.deal_amount || 0, 10) === 50000 ? 'active' : ''}" onclick="updateLeadMeta(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'), {deal_amount: 50000})" title="Pack 50 000 FCFA">50k F</button><button type="button" class="chip-btn-amount ${parseInt(b.deal_amount || 0, 10) === 100000 ? 'active' : ''}" onclick="updateLeadMeta(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'), {deal_amount: 100000})" title="Pack 100 000 FCFA">100k F</button><button type="button" class="chip-btn-amount ${parseInt(b.deal_amount || 0, 10) === 150000 ? 'active' : ''}" onclick="updateLeadMeta(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'), {deal_amount: 150000})" title="Pack 150 000 FCFA">150k F</button><button type="button" class="chip-btn-amount ${parseInt(b.deal_amount || 0, 10) === 250000 ? 'active' : ''}" onclick="updateLeadMeta(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'), {deal_amount: 250000})" title="Pack 250 000 FCFA">250k F</button></div></div>` +
      `<div class="meta-input-group"><label>📝 Note / Suivi / Relance</label><input type="text" value="${escapeHtml(b.notes || '')}" placeholder="Ex: Rappeler jeudi à 15h, intéressé par site vitrine..." onchange="updateLeadMeta(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'), {notes: this.value})" /></div>` +
      `</div>`;

    return `<div class="pipeline-stage-box"><div class="pipeline-stage-header">${stageHeader}</div><div class="pipeline-next-btns">${stageBtns}</div>${sixPillsHtml}${metaInputsHtml}</div>`;
  }

  function render() {
    updateKPIs();
    updateFilterChipsUI();
    const q = document.getElementById('searchInput').value.toLowerCase().trim();
    const cf = document.getElementById('cityFilter').value || 'ALL';
    const crmf = document.getElementById('crmFilter').value || 'ALL';
    const sec = document.getElementById('sectorFilter').value || 'ALL';
    const hideKnownEl = document.getElementById('hideKnownLeadsToggle');
    const hideKnown = Boolean(hideKnownEl && hideKnownEl.checked && CURRENT_VIEW !== 'crm');
    const container = document.getElementById('leadContainer');

    highlightActiveKpi();
    highlightActiveCrmStage(crmf);

    const filtered = LEADS.filter(item => {
      const inPipe = isLeadInPipeline(item);
      const stageGroup = normalizeStageGroup(item.crm_status);

      // In Mon Pipeline Commercial view, only show leads that have entered the pipeline
      if (CURRENT_VIEW === 'crm' && !inPipe) return false;

      // In Radar de Scan view, if "Masquer ceux déjà dans mon Pipeline" is checked, hide known leads
      if (hideKnown && inPipe) return false;

      if (cf !== 'ALL' && item.city !== cf) return false;
      if (crmf !== 'ALL') {
        if (crmf === 'NOUVEAU') {
          if (inPipe) return false;
        } else {
          const targetGroup = normalizeStageGroup(crmf);
          if (!inPipe || stageGroup !== targetGroup) return false;
        }
      }
      if (sec !== 'ALL' && getLeadSectorKey(item) !== sec) return false;

      // Multi-criteria: Web presence / Audit status facet
      if (MULTI_FILTERS.webStatuses.size > 0) {
        let matchWeb = false;
        if (MULTI_FILTERS.webStatuses.has('SANS_SITE') && !leadHasDedicatedWebsite(item)) {
          matchWeb = true;
        }
        if (MULTI_FILTERS.webStatuses.has('SITE_MORT') && item.status === 'SITE_MORT') {
          matchWeb = true;
        }
        if (MULTI_FILTERS.webStatuses.has('SITE_FAIBLE') && item.status === 'SITE_FAIBLE') {
          matchWeb = true;
        }
        if (MULTI_FILTERS.webStatuses.has('SITE_OK') && item.status === 'SITE_OK') {
          matchWeb = true;
        }
        if (!matchWeb) return false;
      }

      // Multi-criteria: Facebook presence facet
      if (MULTI_FILTERS.facebook === 'WITH_FB') {
        if (!leadHasFacebook(item)) return false;
      } else if (MULTI_FILTERS.facebook === 'WITHOUT_FB') {
        if (leadHasFacebook(item)) return false;
      }

      // Multi-criteria: Phone channel facet
      if (MULTI_FILTERS.phoneTypes.size > 0) {
        if (!MULTI_FILTERS.phoneTypes.has(item.phone_type)) return false;
      }

      // Multi-criteria: Tracking & Pixels facet
      if (MULTI_FILTERS.tracking.has('NO_PIXEL') && item.has_fb_pixel) return false;
      if (MULTI_FILTERS.tracking.has('NO_GA') && item.has_google_analytics) return false;

      if (q) {
        const hay = (
          item.name + ' ' + item.neighborhood + ' ' + item.sector + ' ' + item.phone + ' ' +
          (item.email || '') + ' ' + (item.notes || '') + ' ' +
          (item.crm_status || 'A_CONTACTER') + ' ' + (item.website || '') + ' ' +
          (item.facebook_url || '') + ' ' + (item.issues || []).join(' ')
        ).toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });

    const totalScope = CURRENT_VIEW === 'crm' ? LEADS.filter(isLeadInPipeline).length : LEADS.length;
    document.getElementById('filterCountText').textContent = CURRENT_VIEW === 'crm'
      ? `${filtered.length} sur ${totalScope} prospect(s) de Mon Pipeline affichés`
      : `${filtered.length} sur ${LEADS.length} entreprises affichées dans le Radar`;

    if (filtered.length === 0) {
      if (CURRENT_VIEW === 'crm' && totalScope === 0) {
        container.innerHTML = `<div class="empty-box"><p style="font-size:1rem; font-weight:800; color:#0f172a; margin-bottom:6px;">📭 Ton Pipeline Commercial est vide pour le moment.</p><p style="font-size:0.84rem; margin-bottom:14px;">Va dans le <strong>Radar de Scan</strong> et appuie sur le bouton <strong>« ➕ Mettre dans À contacter »</strong> sur les entreprises qui t'intéressent pour les retrouver ici !</p><button class="btn btn-primary" style="max-width:280px;" onclick="switchAppView('scanner')">🔍 Aller au Radar de Scan</button></div>`;
      } else {
        container.innerHTML = `<div class="empty-box"><p style="font-size:0.95rem; font-weight:700; margin-bottom:6px;">Aucun résultat pour ce filtre.</p><p style="font-size:0.8rem; margin-bottom:12px;">Réinitialise les filtres ou change d'onglet CRM en haut.</p><button class="btn btn-reset" style="max-width:220px;" onclick="resetFilters()">🔄 Réinitialiser les filtres</button></div>`;
      }
      return;
    }

    container.innerHTML = filtered.map((b) => {
      const scoreColor = b.score >= 85 ? '#ef4444' : (b.score >= 70 ? '#f59e0b' : '#10b981');
      const fullUrl = b.website ? (b.website.startsWith('http') ? b.website : 'http://' + b.website) : '';
      const domainText = b.website ? shortDomain(b.website) : 'Aucun site';
      const siteHtml = b.website ? `<a href="${fullUrl}" target="_blank">🌐 ${escapeHtml(domainText)}</a>` : '<span>🌐 Aucun site</span>';
      const speedTag = b.response_ms ? ` • ${b.response_ms}ms` : '';
      const phoneLabel = b.phone_type === 'MOBILE_WHATSAPP'
        ? '<span class="phone-badge ph-MOBILE_WHATSAPP">📱 Mobile</span>'
        : (b.phone_type === 'FIXE_ONATEL' ? '<span class="phone-badge ph-FIXE_ONATEL">☎️ Fixe</span>' : '');
      const pixelBadge = b.has_fb_pixel ? '<span class="status-pill tag-pixel">🎯 Pixel Meta</span>' : '';
      const gaBadge = b.has_google_analytics ? '<span class="status-pill tag-analytics">📊 Google Analytics</span>' : '';

      const currentPitch = generatePitch(b, CURRENT_OFFER, CURRENT_PITCH_ANGLE, CURRENT_PITCH_STAGE);
      b.pitch = currentPitch;

      const encName = safeAttrEnc(b.name || '');
      const encCity = safeAttrEnc(b.city || '');

      let dynamicWaUrl = '';
      if (b.phone_type === 'MOBILE_WHATSAPP' && b.phone && b.phone !== 'Non renseigné') {
        dynamicWaUrl = buildWhatsAppUrl(b.phone, currentPitch);
        if (dynamicWaUrl) b.whatsapp_url = dynamicWaUrl;
      }

      const contactBtn = dynamicWaUrl
        ? `<a class="btn btn-wa" href="${dynamicWaUrl}" target="_blank" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'CONTACTE', decodeURIComponent('${encCity}'))">💬 WhatsApp Direct</a>`
        : (b.tel_url ? `<a class="btn btn-call" href="${b.tel_url}" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'CONTACTE', decodeURIComponent('${encCity}'))">📞 Appeler</a>` : '');
      const fbBtn = b.facebook_url ? `<a class="btn btn-fb" href="${b.facebook_url}" target="_blank">📘 Facebook</a>` : '';
      const copyBtn = CURRENT_VIEW === 'crm'
        ? `<button class="btn btn-copy" onclick="copyPitchText(this, decodeURIComponent('${safeAttrEnc(currentPitch)}'))">📋 Copier Pitch</button>`
        : '';
      const pitchBoxHtml = CURRENT_VIEW === 'crm'
        ? `<div class="pitch-box"><strong>Pitch (${escapeHtml(CURRENT_OFFER)} • ${escapeHtml(CURRENT_PITCH_ANGLE.toUpperCase())} • ${escapeHtml(CURRENT_PITCH_STAGE.toUpperCase())}) :</strong> ${escapeHtml(currentPitch)}</div>`
        : '';
      const mapsUrl = b.maps_url || `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(b.name + ' ' + (b.city && b.city !== 'Audit Direct' ? b.city : 'Burkina Faso'))}`;
      const mapsBtn = `<a class="btn btn-maps" href="${mapsUrl}" target="_blank">📍 Google Maps</a>`;

      const shortSector = SECTOR_LABELS[b.sector_key] || b.sector;
      const stageGroup = normalizeStageGroup(b.crm_status);
      const inPipe = isLeadInPipeline(b);
      const cardClass = `lead-card accent-${b.status || 'SANS_SITE'}${stageGroup === 'IGNORE' ? ' crm-state-IGNORE' : ''}`;
      const emailChip = b.email ? `<span class="meta-item">✉️ <strong>${escapeHtml(b.email)}</strong></span>` : '';

      let workflowZoneHtml = '';
      if (CURRENT_VIEW === 'crm') {
        workflowZoneHtml = buildPipelineStageBoxHtml(b, encName, encCity, dynamicWaUrl);
      } else {
        if (inPipe) {
          workflowZoneHtml = buildRadarMemoryBannerHtml(b, encName, encCity);
        } else {
          const claimNotice = (b.claimed_by && !b.is_claimed_by_me)
            ? ` (📌 Pris par ${escapeHtml(b.claimed_by_name || b.claimed_by)})`
            : '';
          workflowZoneHtml = `<div class="scan-triage-bar">` +
            `<button type="button" class="btn-add-pipeline" onclick="addLeadToPipeline(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'))">➕ Mettre dans « À contacter »${claimNotice}</button>` +
            `<button type="button" class="btn btn-reset" style="width:auto; min-height:42px; padding:8px 12px; font-size:0.75rem; color:#b91c1c; border-color:#fecaca; background:#fff1f2;" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'IGNORE', decodeURIComponent('${encCity}'))" title="Ignorer ce résultat">🚫 Ignorer</button>` +
            `</div>`;
        }
      }

      const claimBadge = (b.claimed_by && !b.is_claimed_by_me)
        ? `<span class="badge-claimed">📌 Pris par ${escapeHtml(b.claimed_by_name || b.claimed_by)}</span>`
        : '';

      return `<div class="${cardClass}"><div><div class="card-top">` +
        `<div class="score-badge" onclick="openLeadDetail(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'))" title="Ouvrir la fiche détaillée"><span class="score-num" style="color:${scoreColor}">${b.score}</span><span class="score-lbl">Score</span></div>` +
        `<div class="title-wrap"><div class="biz-name-row"><span class="biz-name" onclick="openLeadDetail(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'))" title="Cliquer pour entrer dans la fiche complète">${escapeHtml(b.name)}</span>` +
        `<button type="button" class="btn-open-detail" onclick="openLeadDetail(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'))">🔎 Fiche & Suivi</button></div>` +
        `<div class="badges-row"><span class="status-pill st-${b.status}">${b.status.replace('_', ' ')}${speedTag}</span>${phoneLabel}${pixelBadge}${gaBadge}${claimBadge}</div></div></div>` +
        `${workflowZoneHtml}` +
        `<div class="meta-chips"><span class="meta-item"><a href="${mapsUrl}" target="_blank" title="Voir sur Google Maps">📍 ${escapeHtml(b.city)} (${escapeHtml(b.neighborhood)})</a></span><span class="meta-item">${escapeHtml(shortSector)}</span><span class="meta-item">📞 <strong>${escapeHtml(b.phone)}</strong></span>${emailChip}<span class="meta-item">${siteHtml}</span></div>` +
        `<div class="issues">${(b.issues || []).map(iss => `<span class="issue-tag">⚠️ ${escapeHtml(iss)}</span>`).join('')}</div>` +
        `${pitchBoxHtml}</div>` +
        `<div class="actions">${contactBtn}${mapsBtn}${fbBtn}${copyBtn}</div></div>`;
    }).join('');
  }

  function debounce(fn, wait = 100) {
    let t;
    return function(...args) {
      clearTimeout(t);
      t = setTimeout(() => fn.apply(this, args), wait);
    };
  }

  // Keyboard Navigation Shortcuts (Elon Musk & Steve Jobs)
  if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
    window.addEventListener('keydown', (ev) => {
      if (!ev) return;
      if (ev.key === 'Escape') {
        if (ACTIVE_DETAIL_LEAD) {
          closeLeadDetail();
        } else {
          const s = document.getElementById('searchInput');
          if (s && document.activeElement === s) {
            s.value = '';
            s.blur();
            render();
          }
        }
        return;
      }
      if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
      const tag = (document.activeElement && document.activeElement.tagName) || '';
      const isInput = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || Boolean(document.activeElement && document.activeElement.isContentEditable);
      if (!isInput) {
        if (ev.key === '/') {
          ev.preventDefault();
          const s = document.getElementById('searchInput');
          if (s) { s.focus(); s.select(); }
        } else if (ev.key === '1') {
          switchAppView('scanner');
        } else if (ev.key === '2') {
          switchAppView('crm');
        } else if (ev.key === '3') {
          switchAppView('offers');
        } else if (ev.key === '4') {
          switchAppView('all');
        }
      }
    });
  }

  const searchInputEl = document.getElementById('searchInput');
  if (searchInputEl) {
    const handleSearchInput = (typeof setTimeout !== 'undefined') ? debounce(render, 100) : render;
    searchInputEl.addEventListener('input', handleSearchInput);
    searchInputEl.addEventListener('search', render);
  }
  document.getElementById('cityFilter').addEventListener('change', render);
  document.getElementById('crmFilter').addEventListener('change', render);
  document.getElementById('statusFilter').addEventListener('change', () => {
    quickFilterStatus(document.getElementById('statusFilter').value);
  });
  document.getElementById('sectorFilter').addEventListener('change', render);
  document.getElementById('phoneFilter').addEventListener('change', () => {
    const v = document.getElementById('phoneFilter').value;
    MULTI_FILTERS.phoneTypes.clear();
    if (v !== 'ALL') MULTI_FILTERS.phoneTypes.add(v);
    render();
  });

  ['voss', 'diagnostic', 'reciprocity', 'social_proof'].forEach(a => {
    const el = document.getElementById('topAngle_' + a);
    if (el) el.classList.toggle('active-angle', a === CURRENT_PITCH_ANGLE);
  });
  ['j0', 'j2', 'j5', 'j8'].forEach(s => {
    const el = document.getElementById('topStage_' + s);
    if (el) el.classList.toggle('active-stage', s === CURRENT_PITCH_STAGE);
  });

  loadInitialLeads();
  </script>
</body>
</html>
"""


class ProspectHandler(BaseHTTPRequestHandler):
    def _send_json(self, data: dict, status: int = 200, set_cookie: str | None = None) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, html_str: str) -> None:
        raw = html_str.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/leads":
            user = get_request_user(self)
            uid = user["id"] if user else 1
            leads = load_saved_leads(user_id=uid)
            self._send_json({"leads": leads})
            return
        if parsed.path == "/api/auth/me":
            user = get_request_user(self)
            if user:
                self._send_json({"authenticated": True, "user": user})
            else:
                self._send_json({"authenticated": False, "user": None})
            return
        if parsed.path == "/api/admin/overview":
            user = get_request_user(self)
            if not user or user.get("role") != "admin":
                self._send_json({"error": "Accès réservé aux administrateurs"}, status=403)
                return
            overview = get_admin_overview()
            self._send_json({"ok": True, "overview": overview})
            return
        if parsed.path == "/api/health":
            self._send_json({"status": "ok", "version": "2.7", "engine": "ProspectBF-Titan", "db": "sqlite-wal"})
            return
        self._send_html(INDEX_HTML)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        if parsed.path == "/api/auth/register":
            username = str(body.get("username", "") or "").strip()
            password = str(body.get("password", "") or "").strip()
            display_name = str(body.get("display_name", "") or "").strip()
            invite_code = str(body.get("invite_code", "") or "").strip()

            ok, msg, user_obj = register_user(username, password, display_name, invite_code)
            if not ok:
                status_code = 403 if "invitation" in msg.lower() else 400
                self._send_json({"error": msg}, status=status_code)
                return

            token = create_session(user_obj["id"])
            cookie_header = f"session_token={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800"
            self._send_json({
                "ok": True,
                "token": token,
                "user": user_obj
            }, set_cookie=cookie_header)
            return

        if parsed.path == "/api/auth/login":
            username = str(body.get("username", "") or "").strip()
            password = str(body.get("password", "") or "").strip()

            ok, msg, user_obj = authenticate_user(username, password)
            if not ok:
                self._send_json({"error": msg}, status=401)
                return

            token = create_session(user_obj["id"])
            cookie_header = f"session_token={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800"
            self._send_json({
                "ok": True,
                "token": token,
                "user": user_obj
            }, set_cookie=cookie_header)
            return

        if parsed.path == "/api/auth/logout":
            token = get_token_from_request(self)
            if token:
                delete_session(token)
            cookie_header = "session_token=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
            self._send_json({"ok": True}, set_cookie=cookie_header)
            return

        if parsed.path in ("/api/leads/status", "/api/leads/crm"):
            name = str(body.get("name", "") or "").strip()
            city = str(body.get("city", "") or "").strip()
            has_crm_key = "crm_status" in body
            new_status = body.get("crm_status")
            has_meta_keys = any(k in body for k in ("in_pipeline", "rdv_done", "deal_amount", "notes"))

            if not name or not city:
                self._send_json({"error": "Nom et ville requis"}, status=400)
                return

            if has_crm_key or not has_meta_keys:
                if not isinstance(new_status, str) or new_status not in VALID_CRM_STATUSES:
                    self._send_json({"error": f"Statut CRM invalide: {new_status}"}, status=400)
                    return

            user = get_request_user(self)
            uid = user["id"] if user else 1
            now_iso = str(body.get("updated_at") or datetime.now(timezone.utc).isoformat())

            with STATE_LOCK:
                init_db()
                conn = get_db_connection()
                try:
                    with conn:
                        name_norm = name.lower()
                        city_norm = city.lower()
                        lead_row = conn.execute(
                            "SELECT id FROM shared_leads WHERE name_norm = ? AND (city_norm = ? OR city_norm = '' OR ? = '');",
                            (name_norm, city_norm, city_norm),
                        ).fetchone()

                        if not lead_row:
                            loc = city if city != "Audit Direct" else "Burkina Faso"
                            q = quote_plus(f"{name} {loc}".strip())
                            conn.execute("""
                                INSERT INTO shared_leads (
                                    name, city, name_norm, city_norm, neighborhood, sector, sector_key,
                                    phone, phone_type, tel_url, email, website, facebook_url, status,
                                    response_ms, issues_json, score, pitch, whatsapp_url, lat, lon,
                                    maps_url, has_fb_pixel, has_google_analytics, created_at, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                            """, (
                                name, city, name_norm, city_norm,
                                "Saisie directe",
                                SECTOR_CATEGORIES.get("entreprise", "🏢 Bureaux, Sociétés IT, Conseil & ONG"),
                                "entreprise",
                                "Non renseigné", "AUCUN", "", "", "", "", "SANS_SITE",
                                0, "[]", 0, "", "", 12.37, -1.52,
                                f"https://www.google.com/maps/search/?api=1&query={q}",
                                0, 0, now_iso, now_iso
                            ))
                            lead_row = conn.execute(
                                "SELECT id FROM shared_leads WHERE name_norm = ? AND city_norm = ?;",
                                (name_norm, city_norm),
                            ).fetchone()

                        lead_id = lead_row["id"]

                        pipe_row = conn.execute(
                            "SELECT * FROM user_pipelines WHERE user_id = ? AND lead_id = ?;",
                            (uid, lead_id),
                        ).fetchone()

                        cur_status = pipe_row["crm_status"] if pipe_row else "A_CONTACTER"
                        cur_in_pipe = bool(pipe_row["in_pipeline"]) if pipe_row else False
                        cur_rdv = bool(pipe_row["rdv_done"]) if pipe_row else False
                        cur_deal = _safe_int_amount(pipe_row["deal_amount"]) if pipe_row else 0
                        cur_notes = str(pipe_row["notes"] or "") if pipe_row else ""

                        target_status = new_status if isinstance(new_status, str) and new_status in VALID_CRM_STATUSES else cur_status

                        if "in_pipeline" in body:
                            target_in_pipe = 1 if body.get("in_pipeline") else 0
                        elif isinstance(new_status, str):
                            target_in_pipe = 1 if target_status != "NOUVEAU" else 0
                        else:
                            target_in_pipe = 1 if cur_in_pipe else 0

                        target_rdv = 1 if body.get("rdv_done") else (0 if "rdv_done" in body else (1 if cur_rdv else 0))
                        target_deal = _safe_int_amount(body.get("deal_amount")) if "deal_amount" in body else cur_deal
                        target_notes = str(body.get("notes") or "").strip()[:5000] if "notes" in body else cur_notes

                        conn.execute("""
                            INSERT INTO user_pipelines (
                                user_id, lead_id, crm_status, in_pipeline, rdv_done, deal_amount, notes, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(user_id, lead_id) DO UPDATE SET
                                crm_status = excluded.crm_status,
                                in_pipeline = excluded.in_pipeline,
                                rdv_done = excluded.rdv_done,
                                deal_amount = excluded.deal_amount,
                                notes = excluded.notes,
                                updated_at = excluded.updated_at;
                        """, (uid, lead_id, target_status, target_in_pipe, target_rdv, target_deal, target_notes, now_iso))
                finally:
                    conn.close()

                if uid == 1:
                    try:
                        snapshot = load_saved_leads(user_id=1)
                        payload = json.dumps(snapshot, ensure_ascii=False, indent=2)
                        unique_suffix = f"{os.getpid()}_{threading.get_ident()}_{time.time_ns()}"
                        temp_file = STATE_FILE.with_name(f"{STATE_FILE.name}.{unique_suffix}.tmp")
                        temp_file.write_text(payload, encoding="utf-8")
                        os.replace(temp_file, STATE_FILE)
                    except Exception:
                        try:
                            STATE_FILE.write_text(payload, encoding="utf-8")
                        except Exception:
                            pass
                    finally:
                        if 'temp_file' in locals() and temp_file.exists():
                            try:
                                temp_file.unlink()
                            except Exception:
                                pass

            self._send_json({
                "ok": True,
                "name": name,
                "city": city,
                "crm_status": target_status,
                "in_pipeline": bool(target_in_pipe),
                "rdv_done": bool(target_rdv),
                "deal_amount": target_deal,
                "notes": target_notes,
                "updated_at": now_iso,
            })
            return

        if parsed.path == "/api/clear":
            user = get_request_user(self)
            if not user or user.get("role") != "admin":
                self._send_json({"error": "Action réservée à la direction agence (admin)"}, status=403)
                return
            save_leads([])
            self._send_json({"ok": True})
            return

        if parsed.path == "/api/audit-single":
            name = str(body.get("name") or "Entreprise").strip() or "Entreprise"
            sector_key = str(body.get("sector_key") or "entreprise").strip() or "entreprise"
            sector_label = SECTOR_CATEGORIES.get(sector_key, SECTOR_CATEGORIES["entreprise"])
            website = str(body.get("website") or "").strip()
            phone_raw = str(body.get("phone") or "").strip()

            lead = BusinessLead(
                name=name,
                city="Audit Direct",
                neighborhood="Saisie manuelle",
                sector=sector_label,
                sector_key=sector_key,
                phone_raw=phone_raw,
                phone_clean=normalize_bf_phone(phone_raw),
                website=website,
                facebook_url="",
                email="",
                lat=12.37,
                lon=-1.52,
            )
            audited = audit_single_business(lead)
            lead_dict = lead_to_dict(audited)

            user = get_request_user(self)
            uid = user["id"] if user else 1

            with STATE_LOCK:
                current = load_saved_leads(user_id=uid)
                merged = merge_leads(current, [lead_dict])
                save_leads(merged, user_id=uid)
                norm_key = f"{name.lower()}|audit direct"
                merged_lead = next(
                    (
                        item
                        for item in merged
                        if f"{str(item.get('name', '')).strip().lower()}|{str(item.get('city', '')).strip().lower()}"
                        == norm_key
                    ),
                    lead_dict,
                )
            self._send_json({"lead": merged_lead})
            return

        if parsed.path == "/api/scan":
            city_raw = str(body.get("city", "ouaga") or "ouaga").strip().lower()
            city = re.sub(r"[^a-z0-9_-]", "", city_raw)
            if city not in CITIES:
                city = "ouaga"
            sector = body.get("sector", "all")
            try:
                limit = min(max(1, int(body.get("limit") or 75)), 130)
            except (ValueError, TypeError):
                limit = 75

            raw_leads = fetch_osm_businesses(city_key=city, sector_filter=sector, limit=limit)
            with concurrent.futures.ThreadPoolExecutor(max_workers=14) as pool:
                audited_leads = list(pool.map(audit_single_business, raw_leads))

            audited_leads.sort(
                key=lambda b: (b.lead_score, is_likely_mobile_bf(b.phone_clean), bool(b.phone_clean)),
                reverse=True,
            )
            export_csv(audited_leads, BASE_DIR / f"leads_{city}.csv")

            incoming_dicts = [lead_to_dict(b) for b in audited_leads]
            incoming_keys = {
                f"{str(d.get('name', '')).strip().lower()}|{str(d.get('city', '')).strip().lower()}"
                for d in incoming_dicts
            }

            user = get_request_user(self)
            uid = user["id"] if user else 1

            with STATE_LOCK:
                merged_dicts = merge_leads(load_saved_leads(user_id=uid), incoming_dicts)
                save_leads(merged_dicts, user_id=uid)
            known_in_scan = sum(
                1
                for item in merged_dicts
                if f"{str(item.get('name', '')).strip().lower()}|{str(item.get('city', '')).strip().lower()}"
                in incoming_keys
                and bool(item.get("in_pipeline") or (item.get("crm_status") in PIPELINE_ACTIVE_STATUSES))
            )
            self._send_json({
                "leads": merged_dicts,
                "scan_count": len(incoming_dicts),
                "known_in_scan": known_in_scan,
            })
            return

        self._send_json({"error": "Route inconnue"}, status=404)


def _seed_initial_leads_async() -> None:
    should_seed = False
    init_db()
    with STATE_LOCK:
        existing = load_saved_leads(user_id=1)
        covered_sectors = {d.get("sector_key") for d in existing if d.get("sector_key")}
        if len(existing) < 40 or len(covered_sectors) < 10:
            should_seed = True

    if should_seed:
        try:
            raw_leads = fetch_osm_businesses("ouaga", "all", limit=80)
            with concurrent.futures.ThreadPoolExecutor(max_workers=14) as pool:
                audited = list(pool.map(audit_single_business, raw_leads))
            audited.sort(
                key=lambda b: (b.lead_score, is_likely_mobile_bf(b.phone_clean), bool(b.phone_clean)),
                reverse=True,
            )
            incoming = [lead_to_dict(b) for b in audited]
            with STATE_LOCK:
                existing = load_saved_leads(user_id=1)
                merged = merge_leads(existing, incoming)
                save_leads(merged, user_id=1)
            print(f"[+] Base de leads initialisée avec succès ({len(merged)} prospects multi-secteurs).", flush=True)
        except Exception as exc:
            print(f"[!] Erreur init arrière-plan: {exc}", flush=True)



def run_server(port: int | None = None) -> None:
    import os
    actual_port = int(port or os.environ.get("PORT", 8080))
    init_db()
    threading.Thread(target=_seed_initial_leads_async, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", actual_port), ProspectHandler)
    print(f"[OK] Serveur ProspectBF v2.7 Mobile-Responsive & Multi-User actif sur port {actual_port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    run_server()

