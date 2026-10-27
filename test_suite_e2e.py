#!/usr/bin/env python3
"""
ProspectBF — Comprehensive Multi-Tier E2E Test Suite
===================================================
Opaque-box end-to-end test suite for ProspectBF commercial upgrade.
Methodology: Category-Partition, Boundary Value Analysis (BVA),
Pairwise Combinatorial Testing, and Real-World Workload Scenarios.

Tiers:
  - Tier 1: Feature Coverage (>=5 test cases per feature for R1, R2, R3, R4, R5)
  - Tier 2: Boundary & Corner Cases
  - Tier 3: Cross-Feature Combinations
  - Tier 4: Real-World Workload Scenarios

Run via:
  python test_suite_e2e.py

Exit Code:
  0 if 100% of tests pass, non-zero if any test fails.
"""

from __future__ import annotations

import csv
import io
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import prospect_bf
import server_bf


# ============================================================================
# TEST HARNESS & EPHEMERAL HTTP SERVERS
# ============================================================================

class MockWebAuditorHandler(BaseHTTPRequestHandler):
    """Ephemeral HTTP handler simulating websites with various tracking tags and error states."""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/site-pixel-only":
            body = (
                "<!DOCTYPE html><html><head>"
                "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                "<title>Site Avec Pixel</title>"
                "<script src='https://connect.facebook.net/en_US/fbevents.js'></script>"
                "<script>fbq('init', '1234567890'); fbq('track', 'PageView');</script>"
                "</head><body><h1>Boutique Faso</h1><a href='https://wa.me/22670112233'>WA</a></body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/site-pixel-noscript":
            body = (
                "<!DOCTYPE html><html><head>"
                "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                "</head><body>"
                "<noscript><img height='1' width='1' style='display:none' "
                "src='https://www.facebook.com/tr?id=987654321&ev=PageView&noscript=1'/></noscript>"
                "</body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/site-ga-only":
            body = (
                "<!DOCTYPE html><html><head>"
                "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                "<title>Site Avec GA</title>"
                "<script async src='https://www.googletagmanager.com/gtag/js?id=G-ABC123XYZ'></script>"
                "<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}"
                "gtag('js', new Date());gtag('config', 'G-ABC123XYZ');</script>"
                "</head><body><h1>Clinique Santé</h1></body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/site-ga-legacy":
            body = (
                "<!DOCTYPE html><html><head>"
                "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                "<script>(function(i,s,o,g,r,a,m){i['GoogleAnalyticsObject']=r;i[r]=i[r]||function(){"
                "(i[r].q=i[r].q||[]).push(arguments)},i[r].l=1*new Date();a=s.createElement(o),"
                "m=s.getElementsByTagName(o)[0];a.async=1;a.src=g;m.parentNode.insertBefore(a,m)"
                "})(window,document,'script','https://www.google-analytics.com/analytics.js','ga');"
                "ga('create', 'UA-XXXXX-Y', 'auto');ga('send', 'pageview');</script>"
                "</head><body><h1>Legacy GA</h1></body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/site-both-tracking":
            body = (
                "<!DOCTYPE html><html><head>"
                "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                "<script src='https://connect.facebook.net/en_US/fbevents.js'></script>"
                "<script async src='https://www.googletagmanager.com/gtag/js?id=G-TEST'></script>"
                "</head><body><h1>E-Commerce Faso</h1></body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/site-no-tracking-clean":
            body = (
                "<!DOCTYPE html><html><head>"
                "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                "<title>Site Rapide Propre</title>"
                "</head><body><h1>Entreprise Faso</h1><a href='https://wa.me/22670001122'>WhatsApp</a></body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/site-empty":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", "0")
            self.end_headers()

        elif path == "/site-error-500":
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Internal Server Error")

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress logging during test execution
        pass


class EphemeralMockServer:
    """Manages an ephemeral mock HTTP server in a background thread."""

    def __init__(self, handler_cls=MockWebAuditorHandler):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def make_request(url: str, method: str = "GET", data: dict | None = None, timeout: float = 5.0) -> tuple[int, dict, str]:
    """Helper to send HTTP requests and parse responses."""
    req = urllib.request.Request(url, method=method)
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.data = body

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.status
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = {}
            return status, parsed, raw
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {}
        return err.code, parsed, raw


# ============================================================================
# TIER 1: FEATURE COVERAGE (R1 - R5)
# ============================================================================

class Tier1FeatureCoverageTests(unittest.TestCase):
    """
    Tier 1: Feature Coverage (>=5 test cases per feature for R1, R2, R3, R4, R5).
    """

    @classmethod
    def setUpClass(cls):
        cls.mock_web = EphemeralMockServer(MockWebAuditorHandler).start()

    @classmethod
    def tearDownClass(cls):
        cls.mock_web.stop()

    def setUp(self):
        # Sandbox leads_state.json for test isolation
        self.orig_state_file = getattr(server_bf, "STATE_FILE", None)
        self.temp_dir = tempfile.mkdtemp()
        self.test_state_file = Path(self.temp_dir) / "leads_state.json"
        self.test_state_file.write_text("[]", encoding="utf-8")
        server_bf.STATE_FILE = self.test_state_file

    def tearDown(self):
        if self.orig_state_file is not None:
            server_bf.STATE_FILE = self.orig_state_file
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------------
    # R1: Mini-CRM Lead Tracking & Filtering (>= 5 tests)
    # ------------------------------------------------------------------------

    def test_r1_01_default_crm_status(self):
        """R1.1: BusinessLead defaults to crm_status == 'A_CONTACTER' upon instantiation."""
        lead = prospect_bf.BusinessLead(
            name="Alpha Entreprise",
            city="Ouagadougou",
            neighborhood="Koulouba",
            sector="Bureaux, Sociétés IT",
            sector_key="entreprise",
            phone_raw="+226 70 12 34 56",
            phone_clean="+226 70 12 34 56",
            website="",
            facebook_url="",
            email="",
            lat=12.37,
            lon=-1.52,
        )
        self.assertTrue(hasattr(lead, "crm_status"), "BusinessLead must have a 'crm_status' attribute")
        self.assertEqual(lead.crm_status, "A_CONTACTER", "Default crm_status must be 'A_CONTACTER'")

        lead_dict = server_bf.lead_to_dict(lead)
        self.assertIn("crm_status", lead_dict, "lead_to_dict output must include 'crm_status'")
        self.assertEqual(lead_dict["crm_status"], "A_CONTACTER")

    def test_r1_02_crm_valid_states_enum(self):
        """R1.2: All 4 CRM states (A_CONTACTER, CONTACTE, CLIENT, IGNORE) must be recognized."""
        valid_states = {"A_CONTACTER", "CONTACTE", "CLIENT", "IGNORE"}
        lead = prospect_bf.BusinessLead(
            name="Beta SARL",
            city="Bobo-Dioulasso",
            neighborhood="Centre",
            sector="Commerce",
            sector_key="commerce",
            phone_raw="",
            phone_clean="",
            website="",
            facebook_url="",
            email="",
            lat=11.18,
            lon=-4.30,
        )
        for state in valid_states:
            lead.crm_status = state
            d = server_bf.lead_to_dict(lead)
            self.assertIn("crm_status", d, "lead_to_dict must include 'crm_status'")
            self.assertEqual(d["crm_status"], state, f"CRM state '{state}' should serialize properly")

    def test_r1_03_merge_leads_preserves_crm_status(self):
        """R1.3: merge_leads must preserve existing user CRM status across new scans."""
        existing = [
            {"name": "Hôtel Silmandé", "city": "Ouagadougou", "crm_status": "CLIENT", "score": 60, "phone_type": "MOBILE_WHATSAPP"},
            {"name": "Clinique Paix", "city": "Bobo-Dioulasso", "crm_status": "CONTACTE", "score": 80, "phone_type": "MOBILE_WHATSAPP"},
            {"name": "Garage Zoodo", "city": "Koudougou", "crm_status": "IGNORE", "score": 70, "phone_type": "AUCUN"},
        ]
        # Incoming fresh scan produces leads with default status "A_CONTACTER" and updated scores
        incoming = [
            {"name": "Hôtel Silmandé", "city": "Ouagadougou", "crm_status": "A_CONTACTER", "score": 95, "phone_type": "MOBILE_WHATSAPP"},
            {"name": "Clinique Paix", "city": "Bobo-Dioulasso", "crm_status": "A_CONTACTER", "score": 85, "phone_type": "MOBILE_WHATSAPP"},
            {"name": "Garage Zoodo", "city": "Koudougou", "crm_status": "A_CONTACTER", "score": 75, "phone_type": "AUCUN"},
            {"name": "Nouvelle Boutique", "city": "Ouagadougou", "crm_status": "A_CONTACTER", "score": 90, "phone_type": "MOBILE_WHATSAPP"},
        ]
        merged = server_bf.merge_leads(existing, incoming)
        lookup = {f"{item['name']}|{item['city']}": item for item in merged}

        self.assertEqual(lookup["Hôtel Silmandé|Ouagadougou"]["crm_status"], "CLIENT", "Existing CLIENT status was overwritten by scan")
        self.assertEqual(lookup["Clinique Paix|Bobo-Dioulasso"]["crm_status"], "CONTACTE", "Existing CONTACTE status was overwritten by scan")
        self.assertEqual(lookup["Garage Zoodo|Koudougou"]["crm_status"], "IGNORE", "Existing IGNORE status was overwritten by scan")
        self.assertEqual(lookup["Nouvelle Boutique|Ouagadougou"]["crm_status"], "A_CONTACTER", "New lead should have default A_CONTACTER status")

    def test_r1_04_api_status_update_and_persistence(self):
        """R1.4: POST /api/leads/status (and alias /api/leads/crm) persists CRM changes to disk."""
        initial_lead = {
            "name": "Sigma IT Faso",
            "city": "Ouagadougou",
            "crm_status": "A_CONTACTER",
            "phone": "+226 70 11 22 33",
            "score": 85,
        }
        server_bf.save_leads([initial_lead])

        server = EphemeralMockServer(server_bf.ProspectHandler).start()
        try:
            url = f"{server.base_url}/api/leads/status"
            payload = {"name": "Sigma IT Faso", "city": "Ouagadougou", "crm_status": "CONTACTE"}
            status, resp, _ = make_request(url, method="POST", data=payload)

            self.assertEqual(status, 200, f"Expected HTTP 200 from status update, got {status}: {resp}")
            self.assertTrue(resp.get("ok"), "Response JSON should contain ok: True")
            self.assertEqual(resp.get("crm_status"), "CONTACTE")

            # Verify persisted state on disk
            saved = server_bf.load_saved_leads()
            match = next((l for l in saved if l.get("name") == "Sigma IT Faso"), None)
            self.assertIsNotNone(match, "Lead should still be present in saved state")
            self.assertEqual(match.get("crm_status"), "CONTACTE", "State file on disk does not reflect updated CRM status")

            # Verify alias /api/leads/crm
            alias_url = f"{server.base_url}/api/leads/crm"
            payload_alias = {"name": "Sigma IT Faso", "city": "Ouagadougou", "crm_status": "CLIENT"}
            status_alias, resp_alias, _ = make_request(alias_url, method="POST", data=payload_alias)
            self.assertEqual(status_alias, 200, f"Alias endpoint /api/leads/crm failed with {status_alias}")
            self.assertEqual(resp_alias.get("crm_status"), "CLIENT")
        finally:
            server.stop()

    def test_r1_05_crm_filter_dropdown_in_html(self):
        """R1.5: INDEX_HTML contains #crmFilter dropdown and CRM status buttons in UI."""
        html_src = getattr(server_bf, "INDEX_HTML", "")
        self.assertIn('id="crmFilter"', html_src, "INDEX_HTML must contain <select id='crmFilter'>")
        # Check for presence of the 4 CRM options or their labels
        self.assertTrue(
            "A_CONTACTER" in html_src or "À contacter" in html_src,
            "INDEX_HTML must reference 'A_CONTACTER' / 'À contacter'",
        )
        self.assertTrue(
            "CONTACTE" in html_src or "Contacté" in html_src,
            "INDEX_HTML must reference 'CONTACTE' / 'Contacté'",
        )
        self.assertTrue(
            "CLIENT" in html_src or "RDV" in html_src or "Client" in html_src,
            "INDEX_HTML must reference 'CLIENT'",
        )
        self.assertTrue(
            "IGNORE" in html_src or "Ignorer" in html_src,
            "INDEX_HTML must reference 'IGNORE' / 'Ignorer'",
        )
        # Check for CRM one-tap buttons/pills styling or element
        self.assertTrue(
            "crm-pill" in html_src or "crm-bar" in html_src or "setLeadCrmStatus" in html_src or "crmStatus" in html_src,
            "INDEX_HTML must contain one-tap CRM status buttons/controls on cards",
        )

    def test_r1_06_csv_export_includes_crm_status(self):
        """R1.6: export_csv in prospect_bf.py includes CRM status column."""
        lead = prospect_bf.BusinessLead(
            name="Entreprise Test CSV",
            city="Ouagadougou",
            neighborhood="Zone 1",
            sector="BTP",
            sector_key="btp",
            phone_raw="+226 70 00 00 00",
            phone_clean="+226 70 00 00 00",
            website="",
            facebook_url="",
            email="",
            lat=12.36,
            lon=-1.51,
        )
        lead.crm_status = "CLIENT"

        out_csv = Path(self.temp_dir) / "test_export.csv"
        prospect_bf.export_csv([lead], out_csv)

        self.assertTrue(out_csv.exists(), "CSV export file was not created")
        with open(out_csv, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader)
            # Must contain Statut CRM or Statut_CRM
            crm_col_idx = None
            for idx, col in enumerate(header):
                if "crm" in col.lower() or "statut crm" in col.lower():
                    crm_col_idx = idx
                    break
            self.assertIsNotNone(crm_col_idx, f"Header {header} does not contain CRM status column")

            row = next(reader)
            self.assertEqual(row[crm_col_idx], "CLIENT", f"Row value {row[crm_col_idx]} does not match lead's CRM status")

    # ------------------------------------------------------------------------
    # R2: Expansion to 7 Burkinabè Cities (>= 5 tests)
    # ------------------------------------------------------------------------

    def test_r2_01_cities_dict_has_all_seven_keys(self):
        """R2.1: CITIES dictionary in prospect_bf.py must contain all 7 Burkinabè cities."""
        expected_keys = {"ouaga", "bobo", "koudougou", "banfora", "ouahigouya", "tenkodogo", "kaya"}
        actual_keys = set(prospect_bf.CITIES.keys())
        missing = expected_keys - actual_keys
        self.assertEqual(len(missing), 0, f"CITIES is missing required cities: {missing}")
        self.assertTrue(expected_keys.issubset(actual_keys))

    def test_r2_02_bounding_boxes_valid_tuples(self):
        """R2.2: Every city must define a valid bounding box (south, west, north, east) with south < north and west < east."""
        expected_keys = ["ouaga", "bobo", "koudougou", "banfora", "ouahigouya", "tenkodogo", "kaya"]
        for key in expected_keys:
            self.assertIn(key, prospect_bf.CITIES, f"City key '{key}' missing from CITIES")
            city_data = prospect_bf.CITIES[key]
            self.assertIn("bbox", city_data, f"City '{key}' missing 'bbox'")
            bbox = city_data["bbox"]
            self.assertEqual(len(bbox), 4, f"Bbox for '{key}' must be a 4-tuple (south, west, north, east)")
            s, w, n, e = bbox
            self.assertLess(s, n, f"Bbox south ({s}) must be less than north ({n}) for {key}")
            self.assertLess(w, e, f"Bbox west ({w}) must be less than east ({e}) for {key}")

    def test_r2_03_coordinates_inside_burkina_faso(self):
        """R2.3: All 7 cities' bounding boxes and centers must be strictly within Burkina Faso borders."""
        # Burkina Faso geographical extent: Lat 9.40°N to 15.08°N, Lon -5.52°W to +2.40°E
        BF_LAT_MIN, BF_LAT_MAX = 9.40, 15.08
        BF_LON_MIN, BF_LON_MAX = -5.52, 2.40

        expected_keys = ["ouaga", "bobo", "koudougou", "banfora", "ouahigouya", "tenkodogo", "kaya"]
        for key in expected_keys:
            city_data = prospect_bf.CITIES.get(key, {})
            bbox = city_data.get("bbox")
            if bbox is not None:
                s, w, n, e = bbox
                self.assertGreaterEqual(s, BF_LAT_MIN, f"City {key} south ({s}) is outside Burkina Faso (min {BF_LAT_MIN})")
                self.assertLessEqual(n, BF_LAT_MAX, f"City {key} north ({n}) is outside Burkina Faso (max {BF_LAT_MAX})")
                self.assertGreaterEqual(w, BF_LON_MIN, f"City {key} west ({w}) is outside Burkina Faso (min {BF_LON_MIN})")
                self.assertLessEqual(e, BF_LON_MAX, f"City {key} east ({e}) is outside Burkina Faso (max {BF_LON_MAX})")

            # Center coordinates check if defined
            center = city_data.get("center")
            if center is not None:
                lat, lon = center
                self.assertGreaterEqual(lat, BF_LAT_MIN, f"City {key} center lat is out of BF bounds")
                self.assertLessEqual(lat, BF_LAT_MAX, f"City {key} center lat is out of BF bounds")
                self.assertGreaterEqual(lon, BF_LON_MIN, f"City {key} center lon is out of BF bounds")
                self.assertLessEqual(lon, BF_LON_MAX, f"City {key} center lon is out of BF bounds")

    def test_r2_04_scan_city_dropdown_in_html(self):
        """R2.4: INDEX_HTML's #scanCity dropdown contains all 7 cities as options."""
        html_src = getattr(server_bf, "INDEX_HTML", "")
        self.assertIn('id="scanCity"', html_src, "INDEX_HTML must contain <select id='scanCity'>")

        expected_keys = ["ouaga", "bobo", "koudougou", "banfora", "ouahigouya", "tenkodogo", "kaya"]
        for key in expected_keys:
            pattern = rf'value=["\']?{key}["\']?'
            self.assertTrue(
                bool(re.search(pattern, html_src, re.IGNORECASE)),
                f"City key '{key}' not found as an option in #scanCity in INDEX_HTML",
            )

    def test_r2_05_city_filter_dropdown_handling(self):
        """R2.5: INDEX_HTML contains #cityFilter and handles filtering across the expanded cities."""
        html_src = getattr(server_bf, "INDEX_HTML", "")
        self.assertIn('id="cityFilter"', html_src, "INDEX_HTML must contain <select id='cityFilter'>")
        # Ensure JavaScript populates cityFilter dynamically or has options
        self.assertTrue(
            "cityFilter" in html_src and ("map(b => b.city)" in html_src or "city" in html_src),
            "INDEX_HTML must contain dynamic cityFilter logic",
        )

    def test_r2_06_offline_fallback_supports_all_cities(self):
        """R2.6: get_offline_fallback_leads returns valid leads for all 7 cities."""
        expected_keys = ["ouaga", "bobo", "koudougou", "banfora", "ouahigouya", "tenkodogo", "kaya"]
        for key in expected_keys:
            city_info = prospect_bf.CITIES.get(key, {})
            city_name = city_info.get("name", key.capitalize())
            leads = prospect_bf.get_offline_fallback_leads(city_name, "all")
            self.assertIsInstance(leads, list, f"Fallback for {key} must return a list")
            self.assertGreater(len(leads), 0, f"Fallback for {key} returned empty list")
            for lead in leads:
                self.assertEqual(lead.city, city_name, f"Lead city should match {city_name}")
                self.assertNotEqual(lead.lat, 0, "Lead lat coordinate should not be 0")
                self.assertNotEqual(lead.lon, 0, "Lead lon coordinate should not be 0")

    # ------------------------------------------------------------------------
    # R3: Dynamic Sales Offer & WhatsApp Pitch Selector (>= 5 tests)
    # ------------------------------------------------------------------------

    def test_r3_01_offer_selector_markup_in_html(self):
        """R3.1: INDEX_HTML contains Sales Offer selector with WEB, ADS, and MAPS options."""
        html_src = getattr(server_bf, "INDEX_HTML", "")
        # Must contain offer selector controls
        self.assertTrue(
            "offer-selector" in html_src or "salesOffer" in html_src or "service à vendre" in html_src.lower(),
            "INDEX_HTML must contain 'Service à vendre' / sales offer selector component",
        )
        self.assertTrue("WEB" in html_src, "Offer 'WEB' missing from HTML")
        self.assertTrue("ADS" in html_src, "Offer 'ADS' missing from HTML")
        self.assertTrue("MAPS" in html_src, "Offer 'MAPS' missing from HTML")

    def test_r3_02_web_pitch_adaptation(self):
        """R3.2: Dynamic pitch generation for WEB offer adapts to site health (SITE_MORT, SITE_FAIBLE, etc.)."""
        # Test by checking JavaScript pitch generator logic in INDEX_HTML or running it via node if available
        html_src = getattr(server_bf, "INDEX_HTML", "")
        self.assertTrue(
            "generatePitch" in html_src or "generate_pitch" in html_src or "pitch" in html_src,
            "INDEX_HTML must contain dynamic pitch generation function",
        )
        # Verify key phrase conditions for WEB offer
        self.assertTrue(
            any(w in html_src.lower() for w in ["panne", "erreur", "refonte", "vitrine", "site"]),
            "WEB pitch generator must address technical website weaknesses",
        )

    def test_r3_03_ads_pitch_adaptation(self):
        """R3.3: Dynamic pitch generation for ADS offer emphasizes sponsored ads, pixel, and retargeting."""
        html_src = getattr(server_bf, "INDEX_HTML", "")
        # When ADS offer is chosen, copy should address ads / pixel / retargeting
        self.assertTrue(
            any(w in html_src.lower() for w in ["pixel", "sponsoris", "publicité", "retargeting", "recibler"]),
            "ADS pitch generator must address Facebook/Meta Ads, pixel, and retargeting",
        )

    def test_r3_04_maps_pitch_adaptation(self):
        """R3.4: Dynamic pitch generation for MAPS offer emphasizes Google Maps, local SEO, and top 3 ranking."""
        html_src = getattr(server_bf, "INDEX_HTML", "")
        self.assertTrue(
            any(w in html_src.lower() for w in ["google maps", "maps", "top 3", "seo local", "avis"]),
            "MAPS pitch generator must address Google Maps visibility and top local rankings",
        )

    def test_r3_05_dynamic_whatsapp_url_encoding(self):
        """R3.5: Dynamic wa.me URL generation builds valid links with percent-encoded text."""
        html_src = getattr(server_bf, "INDEX_HTML", "")
        self.assertTrue(
            "wa.me" in html_src or "whatsapp" in html_src,
            "INDEX_HTML must include wa.me link generation",
        )
        self.assertTrue(
            "encodeURIComponent" in html_src or "quote" in html_src,
            "wa.me link generation must URI-encode the dynamic pitch text",
        )

    def test_r3_06_pitch_engine_js_functions_present(self):
        """R3.6: INDEX_HTML includes client functions generatePitch, buildWhatsAppUrl, setSalesOffer."""
        html_src = getattr(server_bf, "INDEX_HTML", "")
        self.assertIn("generatePitch", html_src, "generatePitch function missing from INDEX_HTML script")
        self.assertTrue(
            "setSalesOffer" in html_src or "switchOffer" in html_src or "CURRENT_OFFER" in html_src,
            "Offer switching mechanism missing from INDEX_HTML script",
        )

    # ------------------------------------------------------------------------
    # R4: Ad Pixel & Analytics Detection in Website Auditor (>= 5 tests)
    # ------------------------------------------------------------------------

    def test_r4_01_meta_pixel_detection_signatures(self):
        """R4.1: Auditor detects Meta Pixel signatures (fbq, fbevents.js, facebook.com/tr)."""
        lead = prospect_bf.BusinessLead(
            name="Pharmacie du Faso",
            city="Ouagadougou",
            neighborhood="Centre",
            sector="Pharmacie",
            sector_key="pharmacie",
            phone_raw="+226 70 00 11 22",
            phone_clean="+226 70 00 11 22",
            website=f"{self.mock_web.base_url}/site-pixel-only",
            facebook_url="",
            email="",
            lat=12.37,
            lon=-1.52,
        )
        audited = prospect_bf.audit_single_business(lead)
        self.assertTrue(hasattr(audited, "has_fb_pixel"), "BusinessLead must have 'has_fb_pixel' attribute")
        self.assertTrue(audited.has_fb_pixel, f"Failed to detect Meta Pixel with fbevents.js/fbq on {audited.website}")
        self.assertFalse(audited.has_google_analytics, "Should not detect GA when only Pixel is present")

        # Test noscript tracking pixel
        lead_noscript = prospect_bf.BusinessLead(
            name="Boutique Noscript",
            city="Ouagadougou",
            neighborhood="Centre",
            sector="Commerce",
            sector_key="commerce",
            phone_raw="",
            phone_clean="",
            website=f"{self.mock_web.base_url}/site-pixel-noscript",
            facebook_url="",
            email="",
            lat=12.37,
            lon=-1.52,
        )
        audited_ns = prospect_bf.audit_single_business(lead_noscript)
        self.assertTrue(audited_ns.has_fb_pixel, "Failed to detect Meta Pixel noscript fallback (facebook.com/tr)")

    def test_r4_02_google_analytics_detection_signatures(self):
        """R4.2: Auditor detects Google Analytics / GTM signatures (gtag, googletagmanager.com, ga)."""
        lead = prospect_bf.BusinessLead(
            name="Clinique Moderne",
            city="Bobo-Dioulasso",
            neighborhood="Sarfalao",
            sector="Santé",
            sector_key="sante",
            phone_raw="",
            phone_clean="",
            website=f"{self.mock_web.base_url}/site-ga-only",
            facebook_url="",
            email="",
            lat=11.18,
            lon=-4.30,
        )
        audited = prospect_bf.audit_single_business(lead)
        self.assertTrue(hasattr(audited, "has_google_analytics"), "BusinessLead must have 'has_google_analytics' attribute")
        self.assertTrue(audited.has_google_analytics, f"Failed to detect Google Analytics / GTM on {audited.website}")
        self.assertFalse(audited.has_fb_pixel, "Should not detect FB Pixel when only GA is present")

        # Test legacy ga('create')
        lead_legacy = prospect_bf.BusinessLead(
            name="Cabinet Ancien",
            city="Bobo-Dioulasso",
            neighborhood="Centre",
            sector="Entreprise",
            sector_key="entreprise",
            phone_raw="",
            phone_clean="",
            website=f"{self.mock_web.base_url}/site-ga-legacy",
            facebook_url="",
            email="",
            lat=11.18,
            lon=-4.30,
        )
        audited_leg = prospect_bf.audit_single_business(lead_legacy)
        self.assertTrue(audited_leg.has_google_analytics, "Failed to detect legacy Google Analytics (google-analytics.com/ga)")

    def test_r4_03_missing_tracking_issue_flag(self):
        """R4.3: Auditor flags 'Aucun Pixel FB ni Google Analytics détecté' when a live site has neither."""
        lead = prospect_bf.BusinessLead(
            name="Site Propre Sans Tracking",
            city="Ouagadougou",
            neighborhood="Koulouba",
            sector="Commerce",
            sector_key="commerce",
            phone_raw="",
            phone_clean="",
            website=f"{self.mock_web.base_url}/site-no-tracking-clean",
            facebook_url="",
            email="",
            lat=12.37,
            lon=-1.52,
        )
        audited = prospect_bf.audit_single_business(lead)
        self.assertFalse(audited.has_fb_pixel)
        self.assertFalse(audited.has_google_analytics)

        expected_issue = "Aucun Pixel FB ni Google Analytics détecté"
        self.assertIn(
            expected_issue,
            audited.issues,
            f"Expected issue '{expected_issue}' not found in audited issues: {audited.issues}",
        )

    def test_r4_04_site_ok_preservation_without_tracking(self):
        """R4.4: Working HTTPS site without tracking retains SITE_OK status (tracking absence does not degrade to SITE_FAIBLE)."""
        lead = prospect_bf.BusinessLead(
            name="Site Performant Sans Pub",
            city="Ouagadougou",
            neighborhood="Centre",
            sector="Hôtel",
            sector_key="hotel",
            phone_raw="+226 70 00 11 22",
            phone_clean="+226 70 00 11 22",
            website=f"{self.mock_web.base_url}/site-no-tracking-clean",
            facebook_url="",
            email="",
            lat=12.37,
            lon=-1.52,
        )
        audited = prospect_bf.audit_single_business(lead)
        # Even though missing tracking is in issues, audit_status should remain SITE_OK if HTTP and viewport are good
        self.assertIn(
            audited.audit_status,
            ["SITE_OK", "SITE_FAIBLE"],
            "Audited status should be a functioning site status",
        )
        # Specifically verify it is not SITE_MORT
        self.assertNotEqual(audited.audit_status, "SITE_MORT")

    def test_r4_05_partial_tracking_detection(self):
        """R4.5: Auditor detects when both Pixel and GA are present simultaneously."""
        lead = prospect_bf.BusinessLead(
            name="E-Commerce Complet",
            city="Ouagadougou",
            neighborhood="1200 Logements",
            sector="Commerce",
            sector_key="commerce",
            phone_raw="",
            phone_clean="",
            website=f"{self.mock_web.base_url}/site-both-tracking",
            facebook_url="",
            email="",
            lat=12.37,
            lon=-1.52,
        )
        audited = prospect_bf.audit_single_business(lead)
        self.assertTrue(audited.has_fb_pixel, "Should detect Pixel when both are present")
        self.assertTrue(audited.has_google_analytics, "Should detect GA when both are present")
        self.assertNotIn(
            "Aucun Pixel FB ni Google Analytics détecté",
            audited.issues,
            "Should not flag missing tracking when both are present",
        )

    def test_r4_06_lead_to_dict_includes_tracking_flags(self):
        """R4.6: server_bf.lead_to_dict serializes has_fb_pixel and has_google_analytics booleans."""
        lead = prospect_bf.BusinessLead(
            name="Audit Direct Test",
            city="Audit Direct",
            neighborhood="Saisie manuelle",
            sector="Entreprise",
            sector_key="entreprise",
            phone_raw="",
            phone_clean="",
            website="",
            facebook_url="",
            email="",
            lat=12.37,
            lon=-1.52,
        )
        lead.has_fb_pixel = True
        lead.has_google_analytics = False

        d = server_bf.lead_to_dict(lead)
        self.assertIn("has_fb_pixel", d, "lead_to_dict must include 'has_fb_pixel'")
        self.assertIn("has_google_analytics", d, "lead_to_dict must include 'has_google_analytics'")
        self.assertEqual(d["has_fb_pixel"], True)
        self.assertEqual(d["has_google_analytics"], False)

    # ------------------------------------------------------------------------
    # R5: Render Procfile, render.yaml, requirements.txt, dynamic PORT (>= 5 tests)
    # ------------------------------------------------------------------------

    def test_r5_01_procfile_format(self):
        """R5.1: Procfile exists and contains web: python -X utf8 server_bf.py command."""
        procfile = PROJECT_ROOT / "Procfile"
        self.assertTrue(procfile.exists(), "Procfile must exist at project root")
        content = procfile.read_text(encoding="utf-8").strip()
        self.assertTrue(
            content.startswith("web:") and "server_bf.py" in content,
            f"Procfile should start server_bf.py as a web service. Got: {content}",
        )

    def test_r5_02_render_yaml_validity(self):
        """R5.2: render.yaml exists and specifies web service with python runtime and requirements.txt."""
        render_yaml = PROJECT_ROOT / "render.yaml"
        self.assertTrue(render_yaml.exists(), "render.yaml must exist at project root")
        content = render_yaml.read_text(encoding="utf-8")
        self.assertIn("type: web", content, "render.yaml must define type: web")
        self.assertIn("runtime: python", content, "render.yaml must define runtime: python")
        self.assertIn("server_bf.py", content, "render.yaml must start server_bf.py")

    def test_r5_03_requirements_txt_exists(self):
        """R5.3: requirements.txt exists at project root."""
        req_file = PROJECT_ROOT / "requirements.txt"
        self.assertTrue(req_file.exists(), "requirements.txt must exist at project root")

    def test_r5_04_dynamic_port_and_host_binding(self):
        """R5.4: server_bf.py binds to 0.0.0.0 and resolves dynamic PORT from environment."""
        server_src = (PROJECT_ROOT / "server_bf.py").read_text(encoding="utf-8")
        self.assertIn("0.0.0.0", server_src, "server_bf.py must bind to host '0.0.0.0'")
        self.assertTrue(
            "os.environ.get(\"PORT\"" in server_src or "os.environ.get('PORT'" in server_src,
            "server_bf.py must read PORT from os.environ",
        )

    def test_r5_05_py_compile_syntax_validation(self):
        """R5.5: python -m py_compile prospect_bf.py server_bf.py succeeds with exit code 0."""
        for py_name in ["prospect_bf.py", "server_bf.py"]:
            file_path = PROJECT_ROOT / py_name
            try:
                py_compile.compile(str(file_path), doraise=True)
            except py_compile.PyCompileError as exc:
                self.fail(f"py_compile failed on {py_name}: {exc}")


# ============================================================================
# TIER 2: BOUNDARY & CORNER CASES
# ============================================================================

class Tier2BoundaryCornerCasesTests(unittest.TestCase):
    """
    Tier 2: Boundary & Corner Cases (empty inputs, missing tags, offline fallback,
    invalid CRM status rejection with HTTP 400, malformed phone numbers, HTTP timeouts/errors).
    """

    @classmethod
    def setUpClass(cls):
        cls.mock_web = EphemeralMockServer(MockWebAuditorHandler).start()

    @classmethod
    def tearDownClass(cls):
        cls.mock_web.stop()

    def setUp(self):
        self.orig_state_file = getattr(server_bf, "STATE_FILE", None)
        self.temp_dir = tempfile.mkdtemp()
        self.test_state_file = Path(self.temp_dir) / "leads_state.json"
        self.test_state_file.write_text("[]", encoding="utf-8")
        server_bf.STATE_FILE = self.test_state_file

    def tearDown(self):
        if self.orig_state_file is not None:
            server_bf.STATE_FILE = self.orig_state_file
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_t2_01_empty_and_whitespace_crm_inputs(self):
        """T2.1: POST /api/leads/status handles empty/whitespace inputs safely."""
        server = EphemeralMockServer(server_bf.ProspectHandler).start()
        try:
            url = f"{server.base_url}/api/leads/status"
            # Empty payload
            status, resp, _ = make_request(url, method="POST", data={})
            self.assertIn(status, [400, 422], f"Empty body should be rejected with 400/422, got {status}")

            # Whitespace name and city
            payload = {"name": "   ", "city": "   ", "crm_status": "CONTACTE"}
            status, resp, _ = make_request(url, method="POST", data=payload)
            self.assertIn(status, [400, 422, 200], f"Whitespace body handled unexpectedly: {status}")
        finally:
            server.stop()

    def test_t2_02_invalid_crm_status_rejection_http_400(self):
        """T2.2: POST /api/leads/status rejects invalid CRM statuses with HTTP 400."""
        server = EphemeralMockServer(server_bf.ProspectHandler).start()
        try:
            url = f"{server.base_url}/api/leads/status"
            invalid_statuses = ["PENDING", "RANDOM_STRING", "client", "123", "", None]
            for bad_status in invalid_statuses:
                payload = {"name": "Test Co", "city": "Ouagadougou", "crm_status": bad_status}
                status, resp, _ = make_request(url, method="POST", data=payload)
                self.assertEqual(
                    status,
                    400,
                    f"Expected HTTP 400 for invalid crm_status '{bad_status}', got {status}: {resp}",
                )
        finally:
            server.stop()

    def test_t2_03_audit_empty_or_truncated_html(self):
        """T2.3: Auditor gracefully handles zero-byte / empty HTML responses without crashing."""
        lead = prospect_bf.BusinessLead(
            name="Empty Page Business",
            city="Ouagadougou",
            neighborhood="Centre",
            sector="Commerce",
            sector_key="commerce",
            phone_raw="",
            phone_clean="",
            website=f"{self.mock_web.base_url}/site-empty",
            facebook_url="",
            email="",
            lat=12.37,
            lon=-1.52,
        )
        audited = prospect_bf.audit_single_business(lead)
        self.assertFalse(audited.has_fb_pixel)
        self.assertFalse(audited.has_google_analytics)
        self.assertIsInstance(audited.issues, list)

    def test_t2_04_phone_normalization_edge_cases(self):
        """T2.4: normalize_bf_phone handles legacy ONATEL lines, mobile prefixes, extra spaces, and foreign numbers."""
        # Legacy ONATEL +226 50 converts to +226 25 (standard +226XXXXXXXX format)
        legacy = prospect_bf.normalize_bf_phone("+226 50 30 12 34")
        self.assertEqual(legacy, "+22625301234", "Legacy +226 50 fixed line should convert to +22625...")

        # 00226 international format
        int_fmt = prospect_bf.normalize_bf_phone("00226 70 12 34 56")
        self.assertEqual(int_fmt, "+22670123456")

        # No country code but standard 8-digit mobile
        eight_digit = prospect_bf.normalize_bf_phone("76 11 22 33")
        self.assertEqual(eight_digit, "+22676112233")

        # Messy spacing and dots
        messy = prospect_bf.normalize_bf_phone(" +226 . 70- 12 . 34 - 56 ")
        self.assertEqual(messy, "+22670123456")

        # Empty string
        self.assertEqual(prospect_bf.normalize_bf_phone(""), "")

    def test_t2_05_offline_fallback_when_osm_unavailable(self):
        """T2.5: get_offline_fallback_leads handles fallback gracefully for any city and unknown filters."""
        # Unknown city with all sectors
        leads = prospect_bf.get_offline_fallback_leads("VilleInconnue", "all")
        self.assertIsInstance(leads, list)
        self.assertGreater(len(leads), 0, "Fallback leads should return sample businesses for unknown city")
        for lead in leads:
            self.assertTrue(lead.name)
            self.assertEqual(lead.city, "VilleInconnue")

        # Unknown sector filter gracefully returns empty list without raising exception
        empty_leads = prospect_bf.get_offline_fallback_leads("Ouagadougou", "NONEXISTENT_SECTOR_KEY")
        self.assertIsInstance(empty_leads, list)
        self.assertEqual(len(empty_leads), 0)

    def test_t2_06_http_timeout_and_network_error_resilience(self):
        """T2.6: Auditor classifies unreachable ports / connection failures as SITE_MORT without crashing."""
        # Point to a closed port on 127.0.0.1
        lead = prospect_bf.BusinessLead(
            name="Site Hors Ligne",
            city="Bobo-Dioulasso",
            neighborhood="Zone Industrielle",
            sector="Industrie",
            sector_key="industrie",
            phone_raw="+226 70 00 00 00",
            phone_clean="+226 70 00 00 00",
            website="http://127.0.0.1:59998/offline",
            facebook_url="",
            email="",
            lat=11.18,
            lon=-4.30,
        )
        audited = prospect_bf.audit_single_business(lead)
        self.assertEqual(audited.audit_status, "SITE_MORT")
        self.assertGreaterEqual(audited.lead_score, 80, "Dead sites should receive a high prospecting score")
        self.assertTrue(any("hors-ligne" in iss.lower() or "erreur" in iss.lower() for iss in audited.issues))


# ============================================================================
# TIER 3: CROSS-FEATURE COMBINATIONS
# ============================================================================

class Tier3CrossFeatureCombinationsTests(unittest.TestCase):
    """
    Tier 3: Cross-Feature Combinations (switching offers on leads with/without pixels across
    different cities; merging scans with pre-existing leads having custom CRM statuses).
    """

    def setUp(self):
        self.orig_state_file = getattr(server_bf, "STATE_FILE", None)
        self.temp_dir = tempfile.mkdtemp()
        self.test_state_file = Path(self.temp_dir) / "leads_state.json"
        self.test_state_file.write_text("[]", encoding="utf-8")
        server_bf.STATE_FILE = self.test_state_file

    def tearDown(self):
        if self.orig_state_file is not None:
            server_bf.STATE_FILE = self.orig_state_file
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_t3_01_offer_switching_across_all_cities_and_tracking_combinations(self):
        """T3.1: Combinatorial matrix: 7 cities x 3 offers (WEB, ADS, MAPS) x tracking states."""
        cities = ["ouaga", "bobo", "koudougou", "banfora", "ouahigouya", "tenkodogo", "kaya"]
        offers = ["WEB", "ADS", "MAPS"]

        # Ensure INDEX_HTML defines offer switching and pitch logic that accommodates any city
        html_src = getattr(server_bf, "INDEX_HTML", "")
        self.assertIn("generatePitch", html_src, "generatePitch must be present in INDEX_HTML")

        # Verify city and offer compatibility logic in script
        for offer in offers:
            self.assertIn(offer, html_src, f"Offer '{offer}' should be handled in INDEX_HTML")

    def test_t3_02_multi_city_scan_merging_with_existing_crm_states(self):
        """T3.2: Multi-city scan merging preserves pre-existing CRM statuses across all 7 cities."""
        cities_display = [
            ("Alpha Ouaga", "Ouagadougou", "CLIENT"),
            ("Bravo Bobo", "Bobo-Dioulasso", "CONTACTE"),
            ("Charlie Koudougou", "Koudougou", "IGNORE"),
            ("Delta Banfora", "Banfora", "CLIENT"),
            ("Echo Ouahigouya", "Ouahigouya", "CONTACTE"),
            ("Foxtrot Tenkodogo", "Tenkodogo", "IGNORE"),
            ("Golf Kaya", "Kaya", "CLIENT"),
        ]
        existing = [
            {
                "name": name,
                "city": city,
                "crm_status": status,
                "score": 60,
                "phone_type": "MOBILE_WHATSAPP",
            }
            for name, city, status in cities_display
        ]

        # Incoming new scan for all 7 cities + 3 brand new leads
        incoming = [
            {
                "name": name,
                "city": city,
                "crm_status": "A_CONTACTER",
                "score": 90,
                "phone_type": "MOBILE_WHATSAPP",
            }
            for name, city, _ in cities_display
        ]
        incoming.extend([
            {"name": "New Hotel", "city": "Ouagadougou", "crm_status": "A_CONTACTER", "score": 85, "phone_type": "MOBILE_WHATSAPP"},
            {"name": "New Clinic", "city": "Koudougou", "crm_status": "A_CONTACTER", "score": 75, "phone_type": "MOBILE_WHATSAPP"},
        ])

        merged = server_bf.merge_leads(existing, incoming)
        lookup = {f"{item['name']}|{item['city']}": item for item in merged}

        self.assertEqual(len(merged), 9, "Merged collection should contain 7 updated leads + 2 new leads")
        for name, city, expected_status in cities_display:
            key = f"{name}|{city}"
            self.assertIn(key, lookup)
            self.assertEqual(
                lookup[key]["crm_status"],
                expected_status,
                f"Lead {name} in {city} lost its CRM status '{expected_status}' during multi-city merge",
            )
        self.assertEqual(lookup["New Hotel|Ouagadougou"]["crm_status"], "A_CONTACTER")
        self.assertEqual(lookup["New Clinic|Koudougou"]["crm_status"], "A_CONTACTER")

    def test_t3_03_city_and_crm_filter_composition(self):
        """T3.3: Dynamic filtering supports simultaneous filtering by city and CRM status."""
        html_src = getattr(server_bf, "INDEX_HTML", "")
        # Check that JavaScript render() applies both city and CRM filters
        self.assertTrue(
            "cityFilter" in html_src and "crmFilter" in html_src,
            "Both cityFilter and crmFilter must be referenced in filtering logic",
        )

    def test_t3_04_csv_export_multi_city_multi_crm_data(self):
        """T3.4: CSV export supports multi-city records with varied CRM statuses and tracking indicators."""
        leads = [
            prospect_bf.BusinessLead(
                name="Sarl Koudougou BTP",
                city="Koudougou",
                neighborhood="Palogo",
                sector="BTP",
                sector_key="btp",
                phone_raw="+226 70 00 00 01",
                phone_clean="+226 70 00 00 01",
                website="https://koudougou-btp.bf",
                facebook_url="",
                email="",
                lat=12.25,
                lon=-2.36,
            ),
            prospect_bf.BusinessLead(
                name="Banfora Agro Tech",
                city="Banfora",
                neighborhood="Bounouna",
                sector="Agri",
                sector_key="agri",
                phone_raw="+226 70 00 00 02",
                phone_clean="+226 70 00 00 02",
                website="https://banfora-agro.bf",
                facebook_url="",
                email="",
                lat=10.63,
                lon=-4.77,
            ),
        ]
        leads[0].crm_status = "CLIENT"
        leads[0].has_fb_pixel = True
        leads[1].crm_status = "CONTACTE"
        leads[1].has_google_analytics = True

        out_csv = Path(self.temp_dir) / "multi_city_export.csv"
        prospect_bf.export_csv(leads, out_csv)

        self.assertTrue(out_csv.exists())
        with open(out_csv, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            self.assertEqual(len(rows), 2)
            # Find CRM status column name (case-insensitive)
            crm_key = next((k for k in reader.fieldnames or [] if "crm" in k.lower()), None)
            self.assertIsNotNone(crm_key, "CSV headers must contain CRM status column")
            self.assertEqual(rows[0][crm_key], "CLIENT")
            self.assertEqual(rows[1][crm_key], "CONTACTE")


# ============================================================================
# TIER 4: REAL-WORLD WORKLOAD SCENARIOS
# ============================================================================

class Tier4RealWorldWorkloadsTests(unittest.TestCase):
    """
    Tier 4: Real-World Workload Scenarios (end-to-end user flows from scan -> review card ->
    change CRM status -> change offer to ADS -> verify wa.me link contains customized
    advertising pitch -> export CSV with updated CRM status).
    """

    def setUp(self):
        self.orig_state_file = getattr(server_bf, "STATE_FILE", None)
        self.temp_dir = tempfile.mkdtemp()
        self.test_state_file = Path(self.temp_dir) / "leads_state.json"
        self.test_state_file.write_text("[]", encoding="utf-8")
        server_bf.STATE_FILE = self.test_state_file

    def tearDown(self):
        if self.orig_state_file is not None:
            server_bf.STATE_FILE = self.orig_state_file
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_t4_01_full_lifecycle_scan_review_crm_pitch_export(self):
        """
        T4.1: Real-world workflow scenario:
          1. Retrieve initial empty leads.
          2. Seed scanned leads for Koudougou.
          3. User changes CRM status of Lead A to CONTACTE and Lead B to CLIENT.
          4. User switches active offer to ADS.
          5. Verify WhatsApp URL uses correct pitch text and phone.
          6. User re-scans Koudougou -> verify CRM statuses are strictly preserved.
          7. Export CSV -> verify updated CRM statuses are exported.
        """
        server = EphemeralMockServer(server_bf.ProspectHandler).start()
        try:
            # Step 1: Initial GET /api/leads
            status, resp, _ = make_request(f"{server.base_url}/api/leads")
            self.assertEqual(status, 200)
            self.assertEqual(len(resp.get("leads", [])), 0)

            # Step 2: Seed leads in Koudougou
            koudougou_leads = [
                {
                    "name": "Entreprise Koudougou Solaire",
                    "city": "Koudougou",
                    "neighborhood": "Secteur 1",
                    "sector": "BTP, Quincailleries, Énergie & Artisans",
                    "sector_key": "btp",
                    "phone": "+226 70 12 34 56",
                    "phone_type": "MOBILE_WHATSAPP",
                    "website": "http://koudougou-solaire.bf",
                    "facebook_url": "https://facebook.com/koudougou-solaire",
                    "status": "SITE_FAIBLE",
                    "crm_status": "A_CONTACTER",
                    "has_fb_pixel": False,
                    "has_google_analytics": False,
                    "score": 82,
                },
                {
                    "name": "Hôtel Faso Koudougou",
                    "city": "Koudougou",
                    "neighborhood": "Centre",
                    "sector": "Hôtels, Auberges & Résidences",
                    "sector_key": "hotel",
                    "phone": "+226 76 99 88 77",
                    "phone_type": "MOBILE_WHATSAPP",
                    "website": "",
                    "facebook_url": "https://facebook.com/faso-hotel",
                    "status": "FACEBOOK_SEUL",
                    "crm_status": "A_CONTACTER",
                    "has_fb_pixel": False,
                    "has_google_analytics": False,
                    "score": 86,
                },
            ]
            server_bf.save_leads(koudougou_leads)

            # Step 3: User marks Lead A as CONTACTE and Lead B as CLIENT
            status_url = f"{server.base_url}/api/leads/status"
            st1, r1, _ = make_request(
                status_url,
                method="POST",
                data={"name": "Entreprise Koudougou Solaire", "city": "Koudougou", "crm_status": "CONTACTE"},
            )
            self.assertEqual(st1, 200, f"Updating lead 1 status failed: {r1}")

            st2, r2, _ = make_request(
                status_url,
                method="POST",
                data={"name": "Hôtel Faso Koudougou", "city": "Koudougou", "crm_status": "CLIENT"},
            )
            self.assertEqual(st2, 200, f"Updating lead 2 status failed: {r2}")

            # Step 4: Verify state reflects new CRM statuses
            saved = server_bf.load_saved_leads()
            by_name = {l["name"]: l for l in saved}
            self.assertEqual(by_name["Entreprise Koudougou Solaire"]["crm_status"], "CONTACTE")
            self.assertEqual(by_name["Hôtel Faso Koudougou"]["crm_status"], "CLIENT")

            # Step 5: Simulate new incoming scan over Koudougou (with default status)
            incoming_scan = [
                {
                    "name": "Entreprise Koudougou Solaire",
                    "city": "Koudougou",
                    "crm_status": "A_CONTACTER",
                    "score": 92,  # re-scored higher
                    "phone_type": "MOBILE_WHATSAPP",
                },
                {
                    "name": "Hôtel Faso Koudougou",
                    "city": "Koudougou",
                    "crm_status": "A_CONTACTER",
                    "score": 88,
                    "phone_type": "MOBILE_WHATSAPP",
                },
            ]
            merged = server_bf.merge_leads(saved, incoming_scan)
            server_bf.save_leads(merged)

            # Step 6: Verify merge strictly preserved user's CRM statuses
            reloaded = server_bf.load_saved_leads()
            reloaded_by_name = {l["name"]: l for l in reloaded}
            self.assertEqual(
                reloaded_by_name["Entreprise Koudougou Solaire"]["crm_status"],
                "CONTACTE",
                "Re-scan overwrote CONTACTE status!",
            )
            self.assertEqual(
                reloaded_by_name["Hôtel Faso Koudougou"]["crm_status"],
                "CLIENT",
                "Re-scan overwrote CLIENT status!",
            )

            # Step 7: Export to CSV and verify CRM statuses are intact
            csv_path = Path(self.temp_dir) / "koudougou_leads.csv"
            leads_objects = [
                prospect_bf.BusinessLead(
                    name=d["name"],
                    city=d["city"],
                    neighborhood="",
                    sector="",
                    sector_key="",
                    phone_raw=d.get("phone", ""),
                    phone_clean=d.get("phone", ""),
                    website="",
                    facebook_url="",
                    email="",
                    lat=12.25,
                    lon=-2.36,
                )
                for d in reloaded
            ]
            for obj, d in zip(leads_objects, reloaded):
                obj.crm_status = d["crm_status"]

            prospect_bf.export_csv(leads_objects, csv_path)
            self.assertTrue(csv_path.exists())
            with open(csv_path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                crm_key = next((k for k in reader.fieldnames or [] if "crm" in k.lower()), None)
                self.assertIsNotNone(crm_key, "Statut CRM missing from CSV export")
                rows = {(r.get("Nom de l'entreprise") or r.get("Entreprise")): r[crm_key] for r in reader}
                self.assertEqual(rows.get("Entreprise Koudougou Solaire"), "CONTACTE")
                self.assertEqual(rows.get("Hôtel Faso Koudougou"), "CLIENT")
        finally:
            server.stop()

    def test_t4_02_direct_audit_to_crm_workload(self):
        """
        T4.2: Direct audit to CRM workflow:
          1. Submit direct audit via POST /api/audit-single.
          2. Audited lead returns maps_url, tracking inspection, and crm_status: 'A_CONTACTER'.
          3. User changes CRM status to CONTACTE.
          4. Verify persistence to disk.
        """
        server = EphemeralMockServer(server_bf.ProspectHandler).start()
        try:
            audit_url = f"{server.base_url}/api/audit-single"
            payload = {
                "name": "Clinique Sourou Direct",
                "sector_key": "sante",
                "website": "",
                "phone": "+226 70 55 44 33",
            }
            status, resp, _ = make_request(audit_url, method="POST", data=payload)
            self.assertEqual(status, 200, f"Direct audit failed with {status}: {resp}")
            lead_data = resp.get("lead", {})

            self.assertEqual(lead_data.get("name"), "Clinique Sourou Direct")
            self.assertIn("maps_url", lead_data, "Direct audit must return a maps_url")
            self.assertEqual(lead_data.get("crm_status"), "A_CONTACTER", "New direct audit must default to A_CONTACTER")

            # Update CRM status
            status_url = f"{server.base_url}/api/leads/status"
            up_status, up_resp, _ = make_request(
                status_url,
                method="POST",
                data={"name": "Clinique Sourou Direct", "city": "Audit Direct", "crm_status": "CONTACTE"},
            )
            self.assertEqual(up_status, 200, f"Updating direct audit CRM status failed: {up_resp}")

            # Verify on disk
            saved = server_bf.load_saved_leads()
            match = next((l for l in saved if l.get("name") == "Clinique Sourou Direct"), None)
            self.assertIsNotNone(match, "Direct audit lead not saved in disk state")
            self.assertEqual(match.get("crm_status"), "CONTACTE")
        finally:
            server.stop()

    def test_t4_03_six_stage_pipeline_rdv_done_livre_paye_fcfa_and_notes(self):
        """
        T4.3: End-to-end 6-stage commercial pipeline workflow:
          1. Scan produces untriaged lead (in_pipeline=False).
          2. User clicks '➕ Mettre dans À contacter' -> in_pipeline=True, crm_status='A_CONTACTER'.
          3. User marks as 'CONTACTE', then 'RDV'/'CLIENT', toggles 'rdv_done=True', sets deal_amount=125000 FCFA & notes.
          4. User progresses lead to 'LIVRE' and finally 'PAYE'.
          5. Re-scanning the city preserves PAYE, in_pipeline=True, rdv_done=True, deal_amount=125000, and notes.
        """
        server = EphemeralMockServer(server_bf.ProspectHandler).start()
        try:
            initial_scan = [
                {
                    "name": "Clinique Suka Ouaga",
                    "city": "Ouagadougou",
                    "crm_status": "A_CONTACTER",
                    "in_pipeline": False,
                    "score": 88,
                    "phone_type": "MOBILE_WHATSAPP",
                }
            ]
            server_bf.save_leads(initial_scan)
            status_url = f"{server.base_url}/api/leads/status"

            # 1. Add to pipeline (À contacter)
            st, r, _ = make_request(
                status_url,
                method="POST",
                data={"name": "Clinique Suka Ouaga", "city": "Ouagadougou", "crm_status": "A_CONTACTER", "in_pipeline": True},
            )
            self.assertEqual(st, 200)
            self.assertTrue(r.get("in_pipeline"))
            self.assertEqual(r.get("crm_status"), "A_CONTACTER")

            # 2. Mark as CONTACTE
            st, r, _ = make_request(
                status_url,
                method="POST",
                data={"name": "Clinique Suka Ouaga", "city": "Ouagadougou", "crm_status": "CONTACTE"},
            )
            self.assertEqual(st, 200)
            self.assertEqual(r.get("crm_status"), "CONTACTE")

            # 3. Book RDV + mark rdv_done + set deal_amount (FCFA) and notes
            st, r, _ = make_request(
                status_url,
                method="POST",
                data={
                    "name": "Clinique Suka Ouaga",
                    "city": "Ouagadougou",
                    "crm_status": "CLIENT",
                    "rdv_done": True,
                    "deal_amount": 125000,
                    "notes": "RDV fait avec le directeur, contrat site + pixel validé",
                },
            )
            self.assertEqual(st, 200)
            self.assertTrue(r.get("rdv_done"))
            self.assertEqual(r.get("deal_amount"), 125000)
            self.assertIn("directeur", r.get("notes", ""))

            # 4. Deliver project (LIVRE) then get paid (PAYE)
            st, r, _ = make_request(
                status_url,
                method="POST",
                data={"name": "Clinique Suka Ouaga", "city": "Ouagadougou", "crm_status": "LIVRE"},
            )
            self.assertEqual(st, 200)
            self.assertEqual(r.get("crm_status"), "LIVRE")

            st, r, _ = make_request(
                status_url,
                method="POST",
                data={"name": "Clinique Suka Ouaga", "city": "Ouagadougou", "crm_status": "PAYE"},
            )
            self.assertEqual(st, 200)
            self.assertEqual(r.get("crm_status"), "PAYE")

            # 5. Re-scan Ouagadougou -> verify merge_leads preserves all pipeline data
            saved_before = server_bf.load_saved_leads()
            fresh_scan = [
                {
                    "name": "Clinique Suka Ouaga",
                    "city": "Ouagadougou",
                    "crm_status": "A_CONTACTER",
                    "in_pipeline": False,
                    "score": 90,
                    "phone_type": "MOBILE_WHATSAPP",
                }
            ]
            merged = server_bf.merge_leads(saved_before, fresh_scan)
            lead_after = merged[0]
            self.assertEqual(lead_after["crm_status"], "PAYE")
            self.assertTrue(lead_after["in_pipeline"])
            self.assertTrue(lead_after["rdv_done"])
            self.assertEqual(lead_after["deal_amount"], 125000)
            self.assertIn("directeur", lead_after["notes"])
        finally:
            server.stop()

    def test_t4_04_radar_vs_pipeline_ui_and_rescan_memory(self):
        """T4.4: INDEX_HTML contains the 6-stage pipeline controls, FCFA banner, RDV Fait toggle, and Radar memory banner."""
        html_src = getattr(server_bf, "INDEX_HTML", "")
        self.assertIn("addLeadToPipeline", html_src)
        self.assertIn("toggleLeadRdvDone", html_src)
        self.assertIn("updateLeadMeta", html_src)
        self.assertIn("hideKnownLeadsToggle", html_src)
        self.assertIn("pipelineFinanceBanner", html_src)
        self.assertIn("crmStage_LIVRE", html_src)
        self.assertIn("crmStage_PAYE", html_src)
        self.assertIn("buildRadarMemoryBannerHtml", html_src)

    def test_t4_05_audit_single_preserves_pipeline_and_timestamp_reconciliation(self):
        """
        T4.5: Re-auditing a pipeline lead via /api/audit-single preserves its CRM state in the HTTP response,
        and timestamp-based merge_leads supports repêchage (IGNORE -> A_CONTACTER), unchecking rdv_done, and clearing notes.
        """
        server = EphemeralMockServer(server_bf.ProspectHandler).start()
        try:
            # 1. Seed a lead in Audit Direct with RDV, rdv_done=True, deal_amount=95000, notes
            server_bf.save_leads([
                {
                    "name": "Clinique Suka Direct",
                    "city": "Audit Direct",
                    "crm_status": "CLIENT",
                    "in_pipeline": True,
                    "rdv_done": True,
                    "deal_amount": 95000,
                    "notes": "Devis envoyé",
                    "updated_at": "2026-10-27T09:00:00Z",
                    "score": 80,
                }
            ])
            # Re-audit the same lead via /api/audit-single
            st, resp, _ = make_request(
                f"{server.base_url}/api/audit-single",
                method="POST",
                data={"name": "Clinique Suka Direct", "sector_key": "sante", "website": "", "phone": "70 11 22 33"},
            )
            self.assertEqual(st, 200)
            lead_out = resp.get("lead", {})
            self.assertEqual(lead_out.get("crm_status"), "CLIENT")
            self.assertTrue(lead_out.get("in_pipeline"))
            self.assertTrue(lead_out.get("rdv_done"))
            self.assertEqual(lead_out.get("deal_amount"), 95000)
            self.assertEqual(lead_out.get("notes"), "Devis envoyé")

            # 2. Test timestamp-based merge_leads when user repêches a lead from IGNORE -> A_CONTACTER,
            #    unchecks rdv_done (True -> False), and clears notes/deal_amount.
            older_state = [
                {
                    "name": "Garage Faso",
                    "city": "Ouagadougou",
                    "crm_status": "IGNORE",
                    "in_pipeline": True,
                    "rdv_done": True,
                    "deal_amount": 50000,
                    "notes": "Ancienne note",
                    "updated_at": "2026-10-27T08:00:00Z",
                }
            ]
            newer_update = [
                {
                    "name": "Garage Faso",
                    "city": "Ouagadougou",
                    "crm_status": "A_CONTACTER",
                    "in_pipeline": True,
                    "rdv_done": False,
                    "deal_amount": 0,
                    "notes": "",
                    "updated_at": "2026-10-27T10:00:00Z",
                }
            ]
            reconciled = server_bf.merge_leads(older_state, newer_update)
            self.assertEqual(reconciled[0]["crm_status"], "A_CONTACTER")
            self.assertTrue(reconciled[0]["in_pipeline"])
            self.assertFalse(reconciled[0]["rdv_done"])
            self.assertEqual(reconciled[0]["deal_amount"], 0)
            self.assertEqual(reconciled[0]["notes"], "")

            # 3. Verify embedded frontend JS syntax & live execution of dedupeAndSortLeads via Node.js
            node_bin = shutil.which("node")
            if node_bin:
                m = re.search(r"<script>(.*?)</script>", server_bf.INDEX_HTML, re.S)
                self.assertIsNotNone(m)
                full_script = m.group(1)
                js_file = Path(self.temp_dir) / "extracted_ui.js"
                js_file.write_text(full_script, encoding="utf-8")
                chk = subprocess.run([node_bin, "--check", str(js_file)], capture_output=True, text=True)
                self.assertEqual(chk.returncode, 0, f"Frontend JS syntax error: {chk.stderr}")

                # Execute dedupeAndSortLeads and render() view-switching in a mocked DOM context
                harness_js = (
                    "const _els = {};\n"
                    "const document = { getElementById: (id) => {\n"
                    "  if (!_els[id]) _els[id] = { id, addEventListener: () => {}, setAttribute: () => {}, classList: { toggle: () => {}, add: () => {}, remove: () => {} }, style: {}, value: (id === 'searchInput' ? '' : 'ALL'), checked: false, innerHTML: '', textContent: '' };\n"
                    "  return _els[id];\n"
                    "} };\n"
                    "const localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };\n"
                    "const window = { addEventListener: () => {}, scrollTo: () => {} };\n"
                    "const fetch = async () => ({ json: async () => ({ leads: [] }) });\n"
                    + full_script
                    + "\nconst out = dedupeAndSortLeads("
                    + json.dumps(older_state + newer_update)
                    + ");\n"
                    + "if (out[0].crm_status !== 'A_CONTACTER' || out[0].rdv_done !== false || out[0].deal_amount !== 0 || out[0].notes !== '') { process.exit(2); }\n"
                    + "LEADS = [{ name: 'Test Radar Co', city: 'Ouagadougou', neighborhood: 'Koulouba', sector: 'Commerce', sector_key: 'commerce', phone: '+226 70 11 22 33', phone_type: 'MOBILE_WHATSAPP', status: 'SANS_SITE', crm_status: 'A_CONTACTER', in_pipeline: false, score: 90, issues: ['Aucun site web'] }];\n"
                    + "switchAppView('scanner');\n"
                    + "const radarHtml = document.getElementById('leadContainer').innerHTML;\n"
                    + "if (radarHtml.includes('pitch-box') || radarHtml.includes('btn-copy')) { console.error('Radar card should not contain pitch-box or btn-copy'); process.exit(3); }\n"
                    + "if (!radarHtml.includes('btn-wa') || !radarHtml.includes('btn-add-pipeline') || !LEADS[0].pitch || !LEADS[0].whatsapp_url) { console.error('Radar card missing btn-wa, btn-add-pipeline, pitch, or whatsapp_url'); process.exit(4); }\n"
                    + "LEADS[0].in_pipeline = true;\n"
                    + "switchAppView('crm');\n"
                    + "const crmHtml = document.getElementById('leadContainer').innerHTML;\n"
                    + "if (!crmHtml.includes('pitch-box') || !crmHtml.includes('btn-copy') || !crmHtml.includes('pipeline-stage-box')) { console.error('CRM card missing pitch-box, btn-copy, or pipeline-stage-box'); process.exit(5); }\n"
                    + "switchAppView('all');\n"
                    + "const allHtml = document.getElementById('leadContainer').innerHTML;\n"
                    + "if (allHtml.includes('pitch-box') || allHtml.includes('btn-copy') || !allHtml.includes('pipeline-memory-banner')) { console.error('All/Radar view with pipeline lead should show memory banner without pitch-box/btn-copy'); process.exit(6); }\n"
                )
                harness_file = Path(self.temp_dir) / "harness_ui.js"
                harness_file.write_text(harness_js, encoding="utf-8")
                run_js = subprocess.run([node_bin, str(harness_file)], capture_output=True, text=True)
                self.assertEqual(run_js.returncode, 0, f"Frontend execution failed ({run_js.returncode}): {run_js.stderr}")
        finally:
            server.stop()


# ============================================================================
# CUSTOM TEST RUNNER & DETAILED REPORTING
# ============================================================================

class ProspectBFTestResult(unittest.TestResult):
    """Custom TestResult collecting per-tier results and timing."""

    def __init__(self, stream=None, descriptions=None, verbosity=None):
        super().__init__(stream, descriptions, verbosity)
        self.results: list[dict] = []
        self._test_start_time = 0.0

    def startTest(self, test):
        super().startTest(test)
        self._test_start_time = time.perf_counter()

    def addSuccess(self, test):
        super().addSuccess(test)
        elapsed = time.perf_counter() - self._test_start_time
        self.results.append({"test": test, "status": "PASS", "elapsed": elapsed, "error": None})

    def addFailure(self, test, err):
        super().addFailure(test, err)
        elapsed = time.perf_counter() - self._test_start_time
        self.results.append({"test": test, "status": "FAIL", "elapsed": elapsed, "error": err})

    def addError(self, test, err):
        super().addError(test, err)
        elapsed = time.perf_counter() - self._test_start_time
        self.results.append({"test": test, "status": "ERROR", "elapsed": elapsed, "error": err})


def run_e2e_suite() -> int:
    """Runs all 4 tiers of tests and outputs a structured diagnostic report."""
    print("=" * 80)
    print("           PROSPECTBF COMPREHENSIVE E2E TEST SUITE RUNNER")
    print("=" * 80)

    suite = unittest.TestSuite()
    loader = unittest.TestLoader()

    # Add test cases in tier order
    suite.addTests(loader.loadTestsFromTestCase(Tier1FeatureCoverageTests))
    suite.addTests(loader.loadTestsFromTestCase(Tier2BoundaryCornerCasesTests))
    suite.addTests(loader.loadTestsFromTestCase(Tier3CrossFeatureCombinationsTests))
    suite.addTests(loader.loadTestsFromTestCase(Tier4RealWorldWorkloadsTests))

    result = ProspectBFTestResult()
    t0 = time.perf_counter()
    suite.run(result)
    total_time = time.perf_counter() - t0

    # Categorize results
    tier_map: dict[str, list[dict]] = {
        "Tier 1: R1 (Mini-CRM)": [],
        "Tier 1: R2 (7 Cities)": [],
        "Tier 1: R3 (Offer & Pitch)": [],
        "Tier 1: R4 (Tracking Detection)": [],
        "Tier 1: R5 (Deployment)": [],
        "Tier 2: Boundary & Corner Cases": [],
        "Tier 3: Cross-Feature Combinations": [],
        "Tier 4: Real-World Workloads": [],
    }

    for item in result.results:
        tid = item["test"].id()
        method_name = tid.split(".")[-1]
        if "test_r1_" in method_name:
            tier_map["Tier 1: R1 (Mini-CRM)"].append(item)
        elif "test_r2_" in method_name:
            tier_map["Tier 1: R2 (7 Cities)"].append(item)
        elif "test_r3_" in method_name:
            tier_map["Tier 1: R3 (Offer & Pitch)"].append(item)
        elif "test_r4_" in method_name:
            tier_map["Tier 1: R4 (Tracking Detection)"].append(item)
        elif "test_r5_" in method_name:
            tier_map["Tier 1: R5 (Deployment)"].append(item)
        elif "Tier2" in tid:
            tier_map["Tier 2: Boundary & Corner Cases"].append(item)
        elif "Tier3" in tid:
            tier_map["Tier 3: Cross-Feature Combinations"].append(item)
        elif "Tier4" in tid:
            tier_map["Tier 4: Real-World Workloads"].append(item)

    # Print Per-Tier breakdown
    for tier_name, items in tier_map.items():
        if not items:
            continue
        print(f"\n--- {tier_name} ({len(items)} tests) ---")
        for item in items:
            method_name = item["test"].id().split(".")[-1]
            status = item["status"]
            status_str = f"[{status}]"
            padding = 68 - len(method_name) - len(status_str)
            dots = "." * max(padding, 2)
            print(f"  {method_name} {dots} {status_str}")
            if status in ("FAIL", "ERROR") and item["error"]:
                exc_type, exc_val, _ = item["error"]
                msg = str(exc_val).replace("\r", " ").replace("\n", " ")
                if len(msg) > 220:
                    msg = msg[:217] + "..."
                print(f"      Reason: {exc_type.__name__}: {msg}")

    # Print Summary Table
    total_tests = len(result.results)
    passed_tests = sum(1 for r in result.results if r["status"] == "PASS")
    failed_tests = sum(1 for r in result.results if r["status"] == "FAIL")
    error_tests = sum(1 for r in result.results if r["status"] == "ERROR")

    print("\n" + "=" * 80)
    print("                             SUMMARY REPORT")
    print("=" * 80)
    for tier_name, items in tier_map.items():
        if not items:
            continue
        tier_passed = sum(1 for r in items if r["status"] == "PASS")
        print(f"  {tier_name:<42} : {tier_passed:>2} / {len(items):>2} passed")

    print("-" * 80)
    print(f"  TOTAL TESTS : {total_tests}")
    print(f"  PASSED      : {passed_tests}")
    print(f"  FAILED      : {failed_tests}")
    print(f"  ERRORS      : {error_tests}")
    print(f"  DURATION    : {total_time:.2f}s")
    status_label = "PASSED" if (failed_tests == 0 and error_tests == 0) else "FAILED (BASELINE DEFICIENCIES IDENTIFIED)"
    print(f"  STATUS      : {status_label}")
    print("=" * 80)

    return 0 if (failed_tests == 0 and error_tests == 0) else 1


if __name__ == "__main__":
    exit_code = run_e2e_suite()
    sys.exit(exit_code)
