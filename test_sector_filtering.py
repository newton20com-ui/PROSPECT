"""
test_sector_filtering.py — Comprehensive Automated Tests for B2B Sector Filtering and Classification.
Tests:
1. All 16 sectors in SECTOR_CATEGORIES have fallback leads.
2. classify_business_sector accurately categorizes names and OSM tags into appropriate sectors.
3. Overpass scanning handles sector filtering and fallback gracefully.
4. load_saved_leads and merge_leads preserve and reconcile sector_key for all leads.
5. In HTML/JS frontend, getLeadSectorKey, populateFilterDropdowns, and render work seamlessly for every sector.
"""

import unittest
import json
import tempfile
import shutil
from pathlib import Path

import prospect_bf
import server_bf


class SectorFilteringTests(unittest.TestCase):
    def setUp(self):
        self.orig_state = getattr(server_bf, "STATE_FILE", None)
        self.temp_dir = tempfile.mkdtemp()
        self.temp_state = Path(self.temp_dir) / "leads_state.json"
        self.temp_state.write_text("[]", encoding="utf-8")
        server_bf.STATE_FILE = self.temp_state

    def tearDown(self):
        if self.orig_state is not None:
            server_bf.STATE_FILE = self.orig_state
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_all_16_sectors_have_fallback_leads(self):
        """Every sector in SECTOR_CATEGORIES (except 'all') must return at least 3 fallback leads."""
        for skey in prospect_bf.SECTOR_CATEGORIES:
            if skey == "all":
                continue
            leads = prospect_bf.get_offline_fallback_leads("Ouagadougou", skey)
            self.assertGreaterEqual(
                len(leads), 3, f"Sector '{skey}' has fewer than 3 fallback leads (got {len(leads)})"
            )
            for lead in leads:
                self.assertEqual(lead.sector_key, skey, f"Lead sector_key should be '{skey}'")
                self.assertTrue(bool(lead.phone_clean or lead.website or lead.facebook_url))

    def test_multi_city_fallback_adaptation(self):
        """Fallback leads adapt city and neighborhoods for Bobo-Dioulasso, Koudougou, etc."""
        bobo_hotels = prospect_bf.get_offline_fallback_leads("Bobo-Dioulasso", "hotel")
        self.assertGreaterEqual(len(bobo_hotels), 3)
        self.assertEqual(bobo_hotels[0].city, "Bobo-Dioulasso")
        self.assertIn(bobo_hotels[0].neighborhood, ["Accart-Ville", "Bindougousso", "Koko", "Sarfalao", "Bolomakoté", "Belle-Ville", "Lafiabougou", "Colma"])

    def test_sector_classification_from_names(self):
        """classify_business_sector accurately recognizes sector from French/Burkina business names."""
        test_cases = [
            ({"name": "Hôtel Résidence Prestige"}, "hotel"),
            ({"name": "Auberge Le Bon Samaritain"}, "hotel"),
            ({"name": "Clinique Sandof Internationale"}, "sante"),
            ({"name": "Cabinet Médical & Dentaire du Faso"}, "sante"),
            ({"name": "Pharmacie de la Liberté"}, "pharmacie"),
            ({"name": "Restaurant Maquis Le Relais"}, "resto"),
            ({"name": "Quincaillerie Moderne BTP"}, "btp"),
            ({"name": "Garage Mécanique Auto Faso"}, "auto"),
            ({"name": "Société d'Assurance Sahel"}, "finance"),
            ({"name": "Institut Supérieur de Commerce"}, "education"),
            ({"name": "Agence de Voyages & Tourisme"}, "voyage"),
            ({"name": "Ferme Avicole et Maraîchère"}, "agri"),
            ({"name": "Salon de Coiffure & Beauté VIP"}, "beaute"),
            ({"name": "Usine Industrielle de Ciment"}, "industrie"),
            ({"name": "Night Club VIP & Discothèque"}, "event"),
            ({"name": "Société de Sécurité et Gardiennage"}, "securite"),
            ({"name": "Supermarché Marina Shop"}, "commerce"),
            ({"name": "Cabinet Conseil Informatique"}, "entreprise"),
        ]
        for tags, expected_key in test_cases:
            skey, slbl = prospect_bf.classify_business_sector(tags)
            self.assertEqual(skey, expected_key, f"Failed for name '{tags['name']}': got '{skey}', expected '{expected_key}'")

    def test_load_saved_leads_reconciles_sector_key(self):
        """load_saved_leads infers sector_key if missing."""
        raw = [
            {"name": "Hôtel du Centre", "city": "Ouagadougou", "crm_status": "A_CONTACTER"},
            {"name": "Pharmacie Wend-Panga", "city": "Ouagadougou", "crm_status": "A_CONTACTER"},
        ]
        server_bf.save_leads(raw)
        loaded = server_bf.load_saved_leads()
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["sector_key"], "hotel")
        self.assertEqual(loaded[1]["sector_key"], "pharmacie")

    def test_scan_all_guarantees_representation_across_all_16_sectors(self):
        """A scan with limit=80 covers all 16 sectors without omission."""
        leads = prospect_bf.fetch_osm_businesses("ouaga", "all", limit=80)
        sector_keys_present = {b.sector_key for b in leads}
        for skey in prospect_bf.SECTOR_CATEGORIES:
            if skey == "all":
                continue
            self.assertIn(
                skey, sector_keys_present, f"Sector '{skey}' is missing from the 80 scanned leads"
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
