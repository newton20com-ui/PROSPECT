"""
test_pitch_engine.py — Comprehensive Test Suite for Dynamic Copywriting Engine (DCE-BF)
Validates the psychological copywriting matrix, Burkinabè cultural etiquette, 16 sectors,
4 psychological angles (Voss, Diagnostic, Reciprocity, Social Proof), 4 cadence stages (J0, J2, J5, J8),
and 3 sales offers (WEB, ADS, MAPS) across Python and JavaScript client engine.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
import urllib.parse
from pathlib import Path

import prospect_bf
import server_bf
from pitch_engine import DynamicPitchEngine, SECTOR_METRICS


class TestPitchEnginePython(unittest.TestCase):
    def test_all_16_sectors_configured(self):
        expected_sectors = {
            "hotel", "sante", "pharmacie", "resto", "btp", "auto",
            "commerce", "finance", "education", "voyage", "agri",
            "beaute", "industrie", "event", "securite", "entreprise"
        }
        self.assertEqual(set(SECTOR_METRICS.keys()), expected_sectors)
        for sk, met in SECTOR_METRICS.items():
            self.assertIn("label", met, f"{sk} missing label")
            self.assertIn("salutation", met, f"{sk} missing salutation")
            self.assertIn("client_term", met, f"{sk} missing client_term")
            self.assertIn("loss_desc", met, f"{sk} missing loss_desc")
            self.assertIn("loss_amount", met, f"{sk} missing loss_amount")
            self.assertIn("value_prop", met, f"{sk} missing value_prop")
            self.assertIn("competitor_example", met, f"{sk} missing competitor_example")
            self.assertIn("FCFA", met["loss_amount"], f"{sk} loss_amount must be in FCFA")

    def test_pitch_matrix_combinations(self):
        """Tests all combinations of offers x angles x stages for a sample lead."""
        lead = {
            "name": "Clinique Saint-Camille",
            "city": "Ouagadougou",
            "neighborhood": "Ouaga 2000",
            "sector_key": "sante",
            "status": "SITE_FAIBLE",
            "issues": ["Temps de réponse très lent (> 4s)", "Pas de certificat HTTPS"],
            "website": "http://saint-camille.bf",
            "response_ms": 4200,
            "has_fb_pixel": False,
        }
        for offer in ["WEB", "ADS", "MAPS"]:
            for angle in ["voss", "diagnostic", "reciprocity", "social_proof"]:
                for stage in ["j0", "j2", "j5", "j8"]:
                    pitch = DynamicPitchEngine.generate(lead, offer=offer, angle=angle, stage=stage)
                    self.assertIsInstance(pitch, str)
                    self.assertTrue(len(pitch) > 30, f"Pitch too short for {offer}/{angle}/{stage}")
                    if stage == "j0":
                        self.assertIn("Docteur", pitch)  # Sector salutation
                        self.assertIn("Clinique Saint-Camille", pitch)
                        self.assertIn("Ouaga 2000", pitch)
                    elif stage == "j2":
                        self.assertIn("faire remonter mon message précédent", pitch)
                    elif stage == "j5":
                        self.assertIn("FCFA", pitch)
                    elif stage == "j8":
                        self.assertIn("classe votre dossier", pitch)

    def test_psychological_angle_ctas(self):
        lead = {"name": "Hôtel Palm Beach", "city": "Bobo-Dioulasso", "sector_key": "hotel"}
        # Voss: No-oriented question
        p_voss = DynamicPitchEngine.generate(lead, angle="voss", stage="j0")
        self.assertIn("Seriez-vous contre l'idée", p_voss)

        # Diagnostic: Technical proof
        p_diag = DynamicPitchEngine.generate(lead, angle="diagnostic", stage="j0")
        self.assertIn("Avez-vous 2 minutes", p_diag)

        # Reciprocity: Free sample/model
        p_reci = DynamicPitchEngine.generate(lead, angle="reciprocity", stage="j0")
        self.assertIn("gratuitement", p_reci)

        # Social Proof: Local benchmark
        p_sp = DynamicPitchEngine.generate(lead, angle="social_proof", stage="j0")
        self.assertIn("concurrents", p_sp)

    def test_whatsapp_url_generation(self):
        lead_phone = "+226 70 25 30 11"
        pitch = "Bonjour Docteur,\n\nTest de message WhatsApp."
        wa_url = DynamicPitchEngine.build_whatsapp_url(lead_phone, pitch)
        self.assertTrue(wa_url.startswith("https://wa.me/22670253011?text="))
        self.assertIn(urllib.parse.quote(pitch), wa_url)


class TestPitchEngineJavaScriptClient(unittest.TestCase):
    def test_js_engine_execution_in_node(self):
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node runtime not found")

        html_src = server_bf.INDEX_HTML
        script_start = html_src.find("<script>")
        script_end = html_src.rfind("</script>")
        self.assertTrue(script_start > 0 and script_end > script_start)
        full_script = html_src[script_start + 8 : script_end]

        test_harness = f"""
        const _els = {{}};
        const document = {{
            getElementById: (id) => {{
                if (!_els[id]) _els[id] = {{ id, addEventListener: () => {{}}, setAttribute: () => {{}}, classList: {{ toggle: () => {{}}, add: () => {{}}, remove: () => {{}} }}, style: {{}}, value: '', checked: false, innerHTML: '', textContent: '' }};
                return _els[id];
            }}
        }};
        const localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }};
        const window = {{ addEventListener: () => {{}}, scrollTo: () => {{}} }};
        const fetch = async () => ({{ json: async () => ({{ leads: [] }}) }});

        {full_script}

        // Test 1: Check SECTOR_METRICS_JS contains all 16 sectors
        const expectedSectors = ['hotel', 'sante', 'pharmacie', 'resto', 'btp', 'auto', 'commerce', 'finance', 'education', 'voyage', 'agri', 'beaute', 'industrie', 'event', 'securite', 'entreprise'];
        for (const s of expectedSectors) {{
            if (!SECTOR_METRICS_JS[s]) {{
                console.error('Missing sector in JS: ' + s);
                process.exit(1);
            }}
            if (!SECTOR_METRICS_JS[s].loss_amount.includes('FCFA')) {{
                console.error('Missing FCFA in sector JS: ' + s);
                process.exit(2);
            }}
        }}

        // Test 2: generatePitch across offers, angles and stages
        const testLead = {{
            name: "Pharmacie de la Paix",
            city: "Ouagadougou",
            neighborhood: "Koulouba",
            sector_key: "pharmacie",
            status: "SANS_SITE",
            issues: ["Aucun site web"],
            phone: "+226 78 12 34 56",
            phone_type: "MOBILE_WHATSAPP"
        }};

        const pWeb = generatePitch(testLead, 'WEB', 'voss', 'j0');
        if (!pWeb.includes('Pharmacie de la Paix') || !pWeb.includes('Koulouba') || !pWeb.includes('vitrine') || !pWeb.includes("Seriez-vous contre l'idée")) {{
            console.error('WEB Voss pitch failed: ' + pWeb);
            process.exit(3);
        }}

        const pAds = generatePitch(testLead, 'ADS', 'diagnostic', 'j0');
        if (!pAds.includes('Pixel') || !pAds.includes('retargeting') || !pAds.includes('Avez-vous 2 minutes')) {{
            console.error('ADS Diagnostic pitch failed: ' + pAds);
            process.exit(4);
        }}

        const pMaps = generatePitch(testLead, 'MAPS', 'social_proof', 'j0');
        if (!pMaps.includes('Google Maps') || !pMaps.includes('Top 3') || !pMaps.includes('concurrents')) {{
            console.error('MAPS Social Proof pitch failed: ' + pMaps);
            process.exit(5);
        }}

        const pJ2 = generatePitch(testLead, 'WEB', 'voss', 'j2');
        if (!pJ2.includes('remonter mon message précédent')) {{
            console.error('J2 relance failed: ' + pJ2);
            process.exit(6);
        }}

        const pJ5 = generatePitch(testLead, 'WEB', 'voss', 'j5');
        if (!pJ5.includes('FCFA')) {{
            console.error('J5 relance failed: ' + pJ5);
            process.exit(7);
        }}

        const pJ8 = generatePitch(testLead, 'WEB', 'voss', 'j8');
        if (!pJ8.includes('classe votre dossier')) {{
            console.error('J8 break-up failed: ' + pJ8);
            process.exit(8);
        }}

        console.log('ALL JS DCE-BF TESTS PASSED');
        process.exit(0);
        """

        harness_path = Path("temp_harness_pitch.js")
        try:
            harness_path.write_text(test_harness, encoding="utf-8")
            res = subprocess.run([node_bin, str(harness_path)], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"JS Pitch Engine test failed ({res.returncode}):\n{res.stderr}\n{res.stdout}")
        finally:
            if harness_path.exists():
                harness_path.unlink()


if __name__ == "__main__":
    unittest.main()
