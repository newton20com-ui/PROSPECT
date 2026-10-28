"""
test_multiuser_sqlite.py — Automated Test Suite for Multi-User Architecture & SQLite WAL Persistence.

Tests:
1. SQLite WAL Mode & Schema Verification.
2. Auto-migration from legacy leads_state.json.
3. User Registration with Invitation Code (Valid vs Invalid code rejection).
4. PBKDF2-HMAC password hashing & verification.
5. User Login, Session Token issuance, Cookie / Bearer header auth, and Logout.
6. Shared Radar & Private Commercial Pipelines (complete data isolation between reps).
7. Anti-collision lead attribution badge (claimed_by, is_claimed_by_me).
8. Admin Consolidated Team Dashboard (/api/admin/overview) access control and financial metrics.
9. Backward compatibility for unauthenticated legacy clients.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from http.server import ThreadingHTTPServer
import threading

import server_bf


class EphemeralServer:
    def __init__(self, handler_cls):
        self.handler_cls = handler_cls
        self.server = None
        self.thread = None
        self.port = None

    def start(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler_cls)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"


def http_req(url: str, method: str = "GET", data: dict | None = None, headers: dict | None = None, cookie: str | None = None) -> tuple[int, dict, dict]:
    req_headers = {"User-Agent": "ProspectBF-Test/2.7"}
    if headers:
        req_headers.update(headers)
    if cookie:
        req_headers["Cookie"] = cookie

    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        req_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            raw = resp.read().decode("utf-8")
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = {"raw": raw}
            return status, parsed, resp_headers
    except urllib.error.HTTPError as err:
        resp_headers = {k.lower(): v for k, v in err.headers.items()}
        raw = err.read().decode("utf-8")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"raw": raw}
        return err.code, parsed, resp_headers


class MultiUserSQLiteTests(unittest.TestCase):
    def setUp(self):
        self.orig_state_file = server_bf.STATE_FILE
        self.temp_dir = tempfile.mkdtemp()
        self.test_state_file = Path(self.temp_dir) / "leads_state.json"
        self.test_state_file.write_text("[]", encoding="utf-8")
        server_bf.STATE_FILE = self.test_state_file
        server_bf.init_db()
        self.server = EphemeralServer(server_bf.ProspectHandler).start()

    def tearDown(self):
        self.server.stop()
        server_bf.STATE_FILE = self.orig_state_file
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_sqlite_wal_mode_and_tables(self):
        """SQLite database initializes with WAL mode and all 4 core tables exist."""
        server_bf.init_db()
        db_path = server_bf.get_db_path()
        self.assertTrue(db_path.exists(), "Database file must be created")
        
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.execute("PRAGMA journal_mode;")
        row = cur.fetchone()
        self.assertEqual(row[0].lower(), "wal", "SQLite must operate in WAL journal mode")

        # Verify tables
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {r["name"] for r in cur.fetchall()}
        self.assertIn("users", tables)
        self.assertIn("sessions", tables)
        self.assertIn("shared_leads", tables)
        self.assertIn("user_pipelines", tables)
        conn.close()

    def test_02_auto_migration_from_json(self):
        """Auto-migrates existing leads from leads_state.json into SQLite on startup."""
        sample_leads = [
            {
                "name": "Clinique Sandof",
                "city": "Ouagadougou",
                "sector": "Cliniques & Santé",
                "sector_key": "sante",
                "phone": "+226 70 11 22 33",
                "phone_type": "MOBILE_WHATSAPP",
                "crm_status": "RDV",
                "in_pipeline": True,
                "deal_amount": 250000,
                "notes": "Rencontre Dr. Ouedraogo prévue",
            }
        ]
        self.test_state_file.write_text(json.dumps(sample_leads), encoding="utf-8")
        
        # When init_db runs on an empty DB, it auto-migrates from STATE_FILE
        server_bf.init_db()
        loaded = server_bf.load_saved_leads()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["name"], "Clinique Sandof")
        self.assertEqual(loaded[0]["crm_status"], "RDV")
        self.assertEqual(loaded[0]["deal_amount"], 250000)
        self.assertEqual(loaded[0]["notes"], "Rencontre Dr. Ouedraogo prévue")

    def test_03_registration_requires_invitation_code(self):
        """Registration with invalid or missing invite code is rejected with HTTP 403."""
        # 1. Reject invalid code
        st, resp, _ = http_req(
            f"{self.server.base_url}/api/auth/register",
            method="POST",
            data={
                "username": "hacker",
                "password": "password123",
                "display_name": "Inconnu",
                "invite_code": "WRONG_CODE_999",
            },
        )
        self.assertEqual(st, 403)
        self.assertIn("invitation", resp.get("error", "").lower())

        # 2. Accept valid code
        st, resp, hdrs = http_req(
            f"{self.server.base_url}/api/auth/register",
            method="POST",
            data={
                "username": "moussa",
                "password": "password123",
                "display_name": "Moussa Diarra",
                "invite_code": server_bf.DEFAULT_INVITE_CODE,
            },
        )
        self.assertEqual(st, 200)
        self.assertTrue(resp.get("ok"))
        self.assertEqual(resp.get("user", {}).get("username"), "moussa")
        self.assertIn("session_token=", hdrs.get("set-cookie", ""))

        # 3. Accept lowercase invite code (case-insensitivity for mobile UX)
        st, resp_lower, _ = http_req(
            f"{self.server.base_url}/api/auth/register",
            method="POST",
            data={
                "username": "rep+ouaga@agency.bf",
                "password": "password123",
                "display_name": "Rep Email Plus",
                "invite_code": server_bf.DEFAULT_INVITE_CODE.lower(),
            },
        )
        self.assertEqual(st, 200)
        self.assertTrue(resp_lower.get("ok"))
        self.assertEqual(resp_lower.get("user", {}).get("username"), "rep+ouaga@agency.bf")

    def test_04_pbkdf2_hmac_password_hashing(self):
        """Passwords are never stored in plaintext and use PBKDF2 HMAC SHA-256 with random salt."""
        h1, s1 = server_bf.hash_password("mon_mot_de_passe")
        h2, s2 = server_bf.hash_password("mon_mot_de_passe")
        self.assertNotEqual(s1, s2, "Salts must be randomly generated per hash")
        self.assertNotEqual(h1, h2, "Hashes of same password with different salts must differ")
        self.assertTrue(server_bf.verify_password("mon_mot_de_passe", s1, h1))
        self.assertFalse(server_bf.verify_password("mauvais_mdp", s1, h1))

        # Check DB directly to confirm no plaintext
        conn = server_bf.get_db_connection()
        try:
            cur = conn.execute("SELECT password_hash, password_salt FROM users WHERE username = 'admin';")
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertNotIn("admin2026", row["password_hash"])
            self.assertEqual(len(row["password_hash"]), 64)  # SHA-256 hex length
        finally:
            conn.close()

    def test_05_login_sessions_and_auth_me(self):
        """User login validates credentials, returns session token, and supports /api/auth/me."""
        # Register a commercial
        st, reg_resp, _ = http_req(
            f"{self.server.base_url}/api/auth/register",
            method="POST",
            data={
                "username": "fatou",
                "password": "secretpassword",
                "display_name": "Fatou Sawadogo",
                "invite_code": server_bf.DEFAULT_INVITE_CODE,
            },
        )
        self.assertEqual(st, 200)

        # Bad login
        st, bad_resp, _ = http_req(
            f"{self.server.base_url}/api/auth/login",
            method="POST",
            data={"username": "fatou", "password": "wrongpassword"},
        )
        self.assertEqual(st, 401)

        # Successful login
        st, login_resp, hdrs = http_req(
            f"{self.server.base_url}/api/auth/login",
            method="POST",
            data={"username": "fatou", "password": "secretpassword"},
        )
        self.assertEqual(st, 200)
        token = login_resp.get("token")
        self.assertIsNotNone(token)

        # Verify /api/auth/me with Bearer token
        st, me_resp, _ = http_req(
            f"{self.server.base_url}/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(st, 200)
        self.assertTrue(me_resp.get("authenticated"))
        self.assertEqual(me_resp.get("user", {}).get("username"), "fatou")

        # Verify logout
        st, logout_resp, logout_hdrs = http_req(
            f"{self.server.base_url}/api/auth/logout",
            method="POST",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(st, 200)

        # After logout, me returns authenticated: False
        st, me_after, _ = http_req(
            f"{self.server.base_url}/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertFalse(me_after.get("authenticated"))

        # Test session expiration & cleanup
        db_path = server_bf.get_db_path()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES ('expired_tok_xyz', 1, '2020-01-01T00:00:00Z', '2020-01-02T00:00:00Z');"
        )
        conn.commit()
        conn.close()

        st_exp, me_exp, _ = http_req(
            f"{self.server.base_url}/api/auth/me",
            headers={"Authorization": "Bearer expired_tok_xyz"},
        )
        self.assertEqual(st_exp, 200)
        self.assertFalse(me_exp.get("authenticated"))

        # Verify expired token was opportunistically purged from DB
        conn = sqlite3.connect(str(db_path))
        cur = conn.execute("SELECT COUNT(*) FROM sessions WHERE token = 'expired_tok_xyz';")
        self.assertEqual(cur.fetchone()[0], 0, "Expired session token must be pruned from SQLite")
        conn.close()

    def test_06_shared_radar_and_private_pipeline_isolation(self):
        """
        Shared Radar & Private Commercial Pipelines:
        - Lead is visible in Shared Radar to both reps.
        - Rep A claims lead to CONTACTE with private notes and deal amount.
        - Rep B sees clean private pipeline (status A_CONTACTER, 0 FCFA, empty notes).
        - Rep B sees anti-collision badge: claimed_by == Rep A.
        """
        # Register Rep A and Rep B
        _, rA, _ = http_req(
            f"{self.server.base_url}/api/auth/register",
            method="POST",
            data={"username": "rep_a", "password": "password123", "display_name": "Commercial Alpha", "invite_code": server_bf.DEFAULT_INVITE_CODE},
        )
        token_a = rA["token"]

        _, rB, _ = http_req(
            f"{self.server.base_url}/api/auth/register",
            method="POST",
            data={"username": "rep_b", "password": "password123", "display_name": "Commercial Bravo", "invite_code": server_bf.DEFAULT_INVITE_CODE},
        )
        token_b = rB["token"]

        # Seed shared lead in Koudougou
        server_bf.save_leads([
            {
                "name": "Boulangerie du Centre",
                "city": "Koudougou",
                "sector": "Restaurants & Cafés",
                "sector_key": "resto",
                "phone": "+226 70 99 88 77",
                "phone_type": "MOBILE_WHATSAPP",
                "score": 85,
            }
        ])

        # Rep A updates the lead to CONTACTE, 450,000 FCFA deal, and private notes
        st, r_stat, _ = http_req(
            f"{self.server.base_url}/api/leads/status",
            method="POST",
            data={
                "name": "Boulangerie du Centre",
                "city": "Koudougou",
                "crm_status": "CONTACTE",
                "deal_amount": 450000,
                "notes": "Contacté le gérant M. Traore, intéressé par un menu digital.",
            },
            headers={"Authorization": f"Bearer {token_a}"},
        )
        self.assertEqual(st, 200)

        # Rep A checks /api/leads -> sees their own pipeline data
        _, leads_a, _ = http_req(
            f"{self.server.base_url}/api/leads",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        lead_a = next(l for l in leads_a["leads"] if l["name"] == "Boulangerie du Centre")
        self.assertEqual(lead_a["crm_status"], "CONTACTE")
        self.assertEqual(lead_a["deal_amount"], 450000)
        self.assertEqual(lead_a["notes"], "Contacté le gérant M. Traore, intéressé par un menu digital.")
        self.assertTrue(lead_a["is_claimed_by_me"])
        self.assertEqual(lead_a["claimed_by"], "")

        # Rep B checks /api/leads -> sees DATA ISOLATION & ATTRIBUTION BADGE
        _, leads_b, _ = http_req(
            f"{self.server.base_url}/api/leads",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        lead_b = next(l for l in leads_b["leads"] if l["name"] == "Boulangerie du Centre")
        self.assertEqual(lead_b["crm_status"], "A_CONTACTER", "Rep B's pipeline must be isolated from Rep A")
        self.assertEqual(lead_b["deal_amount"], 0, "Rep B must not see Rep A's deal amount")
        self.assertEqual(lead_b["notes"], "", "Rep B must not see Rep A's private notes")
        self.assertFalse(lead_b["is_claimed_by_me"])
        self.assertEqual(lead_b["claimed_by_name"], "Commercial Alpha", "Rep B must see attribution badge pointing to Rep A")

    def test_07_admin_overview_role_protection_and_kpis(self):
        """Admin overview (/api/admin/overview) is protected by role and calculates consolidated agency revenue."""
        # 0. Unauthenticated access to /api/admin/overview -> HTTP 403 (Security check)
        st_unauth, _, _ = http_req(f"{self.server.base_url}/api/admin/overview")
        self.assertEqual(st_unauth, 403, "Unauthenticated visitors must not access agency overview")

        # 0b. Unauthenticated access to /api/clear -> HTTP 403
        st_clear_unauth, _, _ = http_req(f"{self.server.base_url}/api/clear", method="POST")
        self.assertEqual(st_clear_unauth, 403, "Unauthenticated callers must not clear agency database")

        # Register a commercial
        _, r_comm, _ = http_req(
            f"{self.server.base_url}/api/auth/register",
            method="POST",
            data={"username": "rep_comm", "password": "password123", "display_name": "Commercial Simple", "invite_code": server_bf.DEFAULT_INVITE_CODE},
        )
        token_comm = r_comm["token"]

        # Commercial cannot access /api/admin/overview -> HTTP 403
        st, forbidden_resp, _ = http_req(
            f"{self.server.base_url}/api/admin/overview",
            headers={"Authorization": f"Bearer {token_comm}"},
        )
        self.assertEqual(st, 403)
        self.assertIn("réservé", forbidden_resp.get("error", "").lower())

        # Commercial cannot call /api/clear -> HTTP 403
        st_clear_comm, _, _ = http_req(
            f"{self.server.base_url}/api/clear",
            method="POST",
            headers={"Authorization": f"Bearer {token_comm}"},
        )
        self.assertEqual(st_clear_comm, 403)

        # Admin logs in
        st, admin_login, _ = http_req(
            f"{self.server.base_url}/api/auth/login",
            method="POST",
            data={"username": "admin", "password": "admin2026"},
        )
        self.assertEqual(st, 200)
        token_admin = admin_login["token"]

        # Commercial signs a deal for 600,000 FCFA marked PAYE
        http_req(
            f"{self.server.base_url}/api/leads/status",
            method="POST",
            data={
                "name": "Entreprise Kaboré IT",
                "city": "Ouagadougou",
                "crm_status": "PAYE",
                "deal_amount": 600000,
                "notes": "Paiement Orange Money reçu",
            },
            headers={"Authorization": f"Bearer {token_comm}"},
        )

        # Admin accesses /api/admin/overview -> HTTP 200
        st, ov_resp, _ = http_req(
            f"{self.server.base_url}/api/admin/overview",
            headers={"Authorization": f"Bearer {token_admin}"},
        )
        self.assertEqual(st, 200)
        ov = ov_resp.get("overview", {})
        self.assertGreaterEqual(ov.get("agency_revenue_paid_fcfa", 0), 600000)
        reps = ov.get("reps", [])
        rep_entry = next((r for r in reps if r["username"] == "rep_comm"), None)
        self.assertIsNotNone(rep_entry)
        self.assertEqual(rep_entry["paid_revenue_fcfa"], 600000)
        self.assertEqual(rep_entry["won_count"], 1)

    def test_08_backward_compatibility_unauthenticated_requests(self):
        """Legacy endpoints without session tokens default to admin workspace and work seamlessly."""
        # GET /api/leads without auth
        st, resp, _ = http_req(f"{self.server.base_url}/api/leads")
        self.assertEqual(st, 200)
        self.assertIn("leads", resp)

        # POST /api/leads/status without auth
        st, resp, _ = http_req(
            f"{self.server.base_url}/api/leads/status",
            method="POST",
            data={"name": "Legacy Auto Tech", "city": "Bobo-Dioulasso", "crm_status": "RDV", "deal_amount": 150000},
        )
        self.assertEqual(st, 200)
        self.assertEqual(resp.get("crm_status"), "RDV")

        # Verify state is persisted and accessible via load_saved_leads()
        saved = server_bf.load_saved_leads()
        lead = next((l for l in saved if l["name"] == "Legacy Auto Tech"), None)
        self.assertIsNotNone(lead)
        self.assertEqual(lead["crm_status"], "RDV")
        self.assertEqual(lead["deal_amount"], 150000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
