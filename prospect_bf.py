"""
ProspectBF v2.5 — Automated B2B Lead Hunter & Digital Auditor for Burkina Faso (Ouagadougou & Bobo-Dioulasso)
Built with 100% Python Standard Library.

Features:
- 11 High-Value B2B Sectors adapted to Burkina Faso's economy.
- Detects Facebook-only businesses (`contact:facebook`, `facebook`, `instagram`, `website` pointing to social pages).
- Applies ARCEP Burkina phone rules (converts legacy +226 50 fixed lines to +226 25, separates Mobile WhatsApp from Fixed ONATEL lines).
- Excludes embassies, ministries, police, and public administration.
- Audits live websites in parallel (HTTP status, SSL certificate, mobile viewport, load time in ms, WhatsApp integration, live +226 phone scraping).
"""

from __future__ import annotations

import concurrent.futures
import csv
import datetime
import html
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# 1. CONFIGURATION, CITIES & EXPANDED BURKINA FASO B2B SECTORS
# ---------------------------------------------------------------------------

CITIES = {
    "ouaga": {
        "name": "Ouagadougou",
        "bbox": (12.28, -1.65, 12.46, -1.40),
        "center": (12.3714, -1.5197),
    },
    "bobo": {
        "name": "Bobo-Dioulasso",
        "bbox": (11.12, -4.38, 11.25, -4.22),
        "center": (11.1772, -4.2979),
    },
    "koudougou": {
        "name": "Koudougou",
        "bbox": (12.18, -2.43, 12.31, -2.30),
        "center": (12.2500, -2.3667),
    },
    "banfora": {
        "name": "Banfora",
        "bbox": (10.58, -4.82, 10.69, -4.71),
        "center": (10.6333, -4.7667),
    },
    "ouahigouya": {
        "name": "Ouahigouya",
        "bbox": (13.52, -2.47, 13.64, -2.37),
        "center": (13.5828, -2.4216),
    },
    "tenkodogo": {
        "name": "Tenkodogo",
        "bbox": (11.73, -0.41, 11.83, -0.32),
        "center": (11.7833, -0.3667),
    },
    "kaya": {
        "name": "Kaya",
        "bbox": (13.03, -1.13, 13.14, -1.03),
        "center": (13.0833, -1.0833),
    },
}

# B2B sector categories tailored to Burkinabè cities
SECTOR_CATEGORIES = {
    "all": "🏢 Tous les secteurs B2B privés",
    "hotel": "🏨 Hôtels, Auberges & Résidences",
    "sante": "🏥 Cliniques, Cabinets, Opticiens & Labos",
    "pharmacie": "💊 Pharmacies & Dépôts",
    "resto": "🍽️ Restaurants, Cafés & Maquis VIP",
    "btp": "🏗️ BTP, Quincailleries, Énergie & Artisans",
    "auto": "🚗 Auto, Location Véhicules, Garages & Transport",
    "commerce": "🛒 Supermarchés, Boutiques, Électronique & Mode",
    "finance": "🏦 Assurances, Microfinance, Banques & Cabinets",
    "education": "🎓 Universités, Grandes Écoles & Formation",
    "voyage": "✈️ Agences de Voyage, Tourisme & Immobilier",
    "agri": "🌾 Agriculture, Élevage & Agroalimentaire",
    "beaute": "💄 Beauté, Coiffure & Cosmétique",
    "industrie": "🏭 Industrie, Usines & Manufactures",
    "event": "🎉 Événementiel, Loisirs & Nuit",
    "securite": "🛡️ Sécurité Privée & Gardiennage",
    "entreprise": "💼 Bureaux, Sociétés IT, Conseil & ONG",
}


@dataclass
class BusinessLead:
    name: str
    city: str
    neighborhood: str
    sector: str
    sector_key: str
    phone_raw: str
    phone_clean: str
    website: str
    facebook_url: str
    email: str
    lat: float
    lon: float
    # Audit results
    audit_status: str = "NON_TESTE"  # SANS_SITE, SITE_MORT, SITE_FAIBLE, SITE_OK, FACEBOOK_SEUL
    http_code: int = 0
    response_ms: int = 0
    has_https: bool = False
    has_viewport: bool = False
    has_whatsapp_link: bool = False
    has_fb_pixel: bool = False
    has_google_analytics: bool = False
    crm_status: str = "A_CONTACTER"
    in_pipeline: bool = False
    rdv_done: bool = False
    deal_amount: int = 0
    notes: str = ""
    updated_at: str = ""
    issues: list[str] = field(default_factory=list)
    lead_score: int = 0
    pitch_angle: str = ""
    whatsapp_url: str = ""


# ---------------------------------------------------------------------------
# 2. BURKINA FASO PHONE NORMALIZATION (+226 & ARCEP RULES)
# ---------------------------------------------------------------------------

def normalize_bf_phone(raw_phone: str) -> str:
    """
    Cleans raw phone strings from Burkina Faso into standard +226XXXXXXXX format.
    If multiple numbers exist in the string, prefers a mobile number over a fixed line!
    Applies ARCEP migration: old Ouaga fixed lines (50 XX XX XX) -> 25 XX XX XX.
    """
    if not raw_phone:
        return ""

    parts = re.split(r"[;/,|\n]", raw_phone)
    cleaned_candidates: list[str] = []

    for part in parts:
        digits = re.sub(r"[^\d+]", "", part)
        if not digits:
            continue
        if digits.startswith("00226"):
            digits = "+" + digits[2:]
        elif digits.startswith("226") and len(digits) == 11:
            digits = "+" + digits
        elif len(digits) == 8 and digits.isdigit():
            digits = "+226" + digits

        # ARCEP migration for old Ouaga fixed lines
        if digits.startswith("+22650") and len(digits) == 12:
            digits = "+22625" + digits[6:]

        if digits.startswith("+226") and len(digits) == 12:
            cleaned_candidates.append(digits)

    if not cleaned_candidates:
        # Fallback if unconventional length
        d = re.sub(r"[^\d+]", "", parts[0])
        return d

    # Prefer a mobile number (for WhatsApp) if the business listed both a landline and a mobile
    for cand in cleaned_candidates:
        if is_likely_mobile_bf(cand):
            return cand
    return cleaned_candidates[0]


def is_likely_mobile_bf(clean_phone: str) -> bool:
    """
    In Burkina Faso:
    - Fixed lines (ONATEL) start with 25 (or legacy 20/50). Call only.
    - Mobile numbers (Orange, Moov, Telecel) start with 01, 02, 05, 06, 07, 51-58, 60-79.
    """
    if not clean_phone.startswith("+226") or len(clean_phone) != 12:
        return False
    prefix = clean_phone[4:6]
    return prefix not in ("25", "20", "50")


def normalize_facebook_url(raw_fb: str) -> str:
    """Turns a raw OSM facebook tag (e.g. 'HotelSanaOuaga' or 'facebook.com/...') into a clean URL."""
    val = raw_fb.strip()
    if not val:
        return ""
    if val.startswith(("http://", "https://")):
        return val
    if val.startswith(("www.facebook.", "facebook.com", "fb.com", "m.facebook.")):
        return "https://" + val
    return f"https://www.facebook.com/{val.lstrip('@/')}"


def is_social_media_url(url: str) -> bool:
    u = url.lower()
    return any(
        s in u
        for s in (
            "facebook.com",
            "fb.com",
            "fb.me",
            "instagram.com",
            "linkedin.com",
            "tiktok.com",
            "twitter.com",
            "x.com",
        )
    )


# ---------------------------------------------------------------------------
# 3. OPENSTREETMAP MULTI-SECTOR QUERY & CLASSIFICATION
# ---------------------------------------------------------------------------

EXCLUDED_OFFICE_TYPES = {
    "government",
    "diplomatic",
    "ministry",
    "administrative",
    "political_party",
    "religion",
    "Courthouse",
}

EXCLUDED_NAME_KEYWORDS = (
    "ambassade",
    "ministère",
    "ministere",
    "mairie",
    "consulat",
    "gendarmerie",
    "police",
    "direction générale",
    "direction generale",
    "institut géographique",
    "haut-commissariat",
    "préfecture",
    "prefecture",
    "tribunal",
    "assemblée nationale",
    "ceni",
    "sonabel",
    "onea",
)


def classify_business_sector(tags: dict) -> tuple[str, str]:
    """Returns (sector_key, sector_label) based on OSM tags and business naming."""
    # Direct match if sector_key is already provided
    direct_key = tags.get("sector_key", "").lower().strip()
    if direct_key in SECTOR_CATEGORIES and direct_key != "all":
        return direct_key, SECTOR_CATEGORIES[direct_key]

    name = tags.get("name", "").lower().strip()
    desc = tags.get("description", "").lower().strip()
    hay = f"{name} {desc}"

    tourism = tags.get("tourism", "").lower().strip()
    amenity = tags.get("amenity", "").lower().strip()
    shop = tags.get("shop", "").lower().strip()
    office = tags.get("office", "").lower().strip()
    craft = tags.get("craft", "").lower().strip()
    healthcare = tags.get("healthcare", "").lower().strip()
    leisure = tags.get("leisure", "").lower().strip()
    man_made = tags.get("man_made", "").lower().strip()

    # 1. Hôtels & Résidences
    if tourism in ("hotel", "guest_house", "motel", "apartment", "hostel", "chalet") or any(
        k in hay for k in ("hôtel", "hotel", "auberge", "résidence hôtelière", "residence hoteliere", "motel", "guest house", "hébergement", "hebergement", "résidence", "residence")
    ):
        return "hotel", SECTOR_CATEGORIES["hotel"]

    # 2. Pharmacies
    if amenity == "pharmacy" or healthcare == "pharmacy" or any(
        k in hay for k in ("pharmacie", "officine", "dépôt pharmaceutique", "depot pharmaceutique")
    ):
        return "pharmacie", SECTOR_CATEGORIES["pharmacie"]

    # 3. Santé & Cliniques
    if (
        amenity in ("clinic", "hospital", "doctors", "dentist", "veterinary")
        or healthcare
        or shop == "optician"
        or any(k in hay for k in ("clinique", "hôpital", "hopital", "polyclinique", "médical", "medical", "dentaire", "opticien", "optique", "laboratoire", "radiologie", "maternité", "cma", "centre médical"))
    ):
        return "sante", SECTOR_CATEGORIES["sante"]

    # 4. Restaurants & Cafés
    if amenity in ("restaurant", "cafe", "fast_food", "bar", "ice_cream", "pub") or any(
        k in hay for k in ("restaurant", "resto", "maquis", "café", "cafe", "snack", "pâtisserie", "patisserie", "boulangerie", "glacier", "lounge", "fast food", "traiteur", "grillade")
    ):
        return "resto", SECTOR_CATEGORIES["resto"]

    # 5. BTP, Quincailleries & Solaire
    if (
        shop in ("hardware", "doityourself", "trade", "paint", "electrical", "energy", "tiles")
        or craft in ("electrician", "plumber", "carpenter", "builder", "hvac")
        or office in ("construction_company", "architect", "engineer", "energy_supplier", "water_utility")
        or any(k in hay for k in ("quincaillerie", "btp", "construction", "bâtiment", "batiment", "menuiserie", "plomberie", "électricité", "electricite", "peinture", "carrelage", "énergie solaire", "energie solaire", "forage", "génie civil", "genie civil", "architecte", "architecture"))
    ):
        return "btp", SECTOR_CATEGORIES["btp"]

    # 6. Auto, Garage & Transport
    if (
        shop in ("car", "car_repair", "car_parts", "tyres", "motorcycle")
        or amenity in ("car_rental", "fuel", "bus_station")
        or office in ("logistics", "transport", "moving_company", "courier")
        or any(k in hay for k in ("garage", "automobile", "auto", "mécanique", "mecanique", "pièces détachées", "pieces detachees", "pneus", "lavage auto", "location de voiture", "location véhicule", "transport", "logistique", "fret", "transit"))
    ):
        return "auto", SECTOR_CATEGORIES["auto"]

    # 7. Finance, Banques & Assurances
    if (
        amenity in ("bank", "bureau_de_change", "microfinance")
        or office in ("insurance", "financial", "accountant", "lawyer", "notary", "tax_advisor")
        or any(k in hay for k in ("banque", "microfinance", "assurance", "assurances", "mutuelle", "expert-comptable", "comptabilité", "avocat", "notaire", "fiduciaire", "change", "crédit", "finances"))
    ):
        return "finance", SECTOR_CATEGORIES["finance"]

    # 8. Éducation & Formation
    if amenity in ("university", "college", "training", "language_school", "driving_school", "music_school") or any(
        k in hay for k in ("université", "universite", "institut", "école", "ecole", "collège", "college", "lycée", "lycee", "formation", "auto-école", "auto-ecole", "académie", "academie", "centre de formation", "campus")
    ):
        return "education", SECTOR_CATEGORIES["education"]

    # 9. Voyages & Immobilier
    if (
        shop == "travel_agency"
        or office in ("travel_agent", "estate_agent", "property_management")
        or tourism in ("information", "travel_agency")
        or any(k in hay for k in ("agence de voyage", "voyages", "voyage", "tourisme", "billetterie", "compagnie aérienne", "aerienne", "immobilier", "immobilière", "agence immobilière"))
    ):
        return "voyage", SECTOR_CATEGORIES["voyage"]

    # 10. Agriculture & Élevage
    if shop in ("agrarian", "farm") or office in ("agriculture", "forestry") or any(
        k in hay for k in ("agriculture", "agricole", "agrobusiness", "agroalimentaire", "ferme", "élevage", "elevage", "aviculture", "intrants", "semences", "maraîchage", "coton", "sofitex")
    ):
        return "agri", SECTOR_CATEGORIES["agri"]

    # 11. Beauté & Cosmétique
    if shop in ("beauty", "hairdresser", "cosmetics", "massage", "perfumery", "tattoo") or leisure == "spa" or any(
        k in hay for k in ("coiffure", "salon de coiffure", "beauté", "beaute", "esthétique", "esthetique", "cosmétique", "cosmetique", "barber", "parfumerie", "spa", "soins de beauté")
    ):
        return "beaute", SECTOR_CATEGORIES["beaute"]

    # 12. Industrie & Usines
    if man_made == "works" or "industrial" in tags.get("building", "") or any(
        k in hay for k in ("usine", "manufacture", "industriel", "industrielle", "industrie", "fabrique", "cimenterie", "brasserie", "brakina", "filature", "métallurgie", "emballage")
    ):
        return "industrie", SECTOR_CATEGORIES["industrie"]

    # 13. Événementiel & Loisirs
    if amenity in ("nightclub", "events_venue", "cinema", "theatre", "casino", "community_centre") or leisure in ("dance", "stadium") or any(
        k in hay for k in ("night club", "discothèque", "discotheque", "espace événementiel", "evenementiel", "salle des fêtes", "salle de fête", "spectacle", "cinéma", "cinema", "théâtre", "theatre", "espace culturel", "club vip")
    ):
        return "event", SECTOR_CATEGORIES["event"]

    # 14. Sécurité & Gardiennage
    if office == "security" or any(
        k in hay for k in ("sécurité", "securite", "gardiennage", "surveillance", "vigile", "sécurité privée", "securite privee", "alarme", "vidéosurveillance")
    ):
        return "securite", SECTOR_CATEGORIES["securite"]

    # 15. Bureaux, Sociétés IT, Conseil & ONG
    if office or any(
        k in hay for k in ("cabinet conseil", "bureau d'études", "bureau d'etude", "consulting", "ingénierie", "ingenierie", "solutions it", "software", "agence digitale", "services informatiques")
    ):
        return "entreprise", SECTOR_CATEGORIES["entreprise"]

    # 16. Commerce & Boutiques
    if shop or any(
        k in hay for k in ("boutique", "supermarché", "supermarche", "alimentation", "magasin", "prêt-à-porter", "mode", "chaussures", "vêtements", "vetements", "électronique", "electronique", "téléphonie", "meubles", "décoration", "bijouterie")
    ):
        return "commerce", SECTOR_CATEGORIES["commerce"]

    # Entreprise & Services (par défaut)
    return "entreprise", SECTOR_CATEGORIES["entreprise"]


def build_overpass_clauses(bbox_str: str, sector_filter: str) -> str:
    """Builds targeted Overpass QL clauses for the chosen sector."""
    clauses_map = {
        "hotel": f"""
            node["tourism"~"^(hotel|guest_house|motel|apartment)$"]({bbox_str});
            way["tourism"~"^(hotel|guest_house|motel|apartment)$"]({bbox_str});
        """,
        "sante": f"""
            node["amenity"~"^(clinic|hospital|doctors|dentist)$"]({bbox_str});
            way["amenity"~"^(clinic|hospital|doctors|dentist)$"]({bbox_str});
            node["healthcare"]({bbox_str});
            node["shop"="optician"]({bbox_str});
        """,
        "pharmacie": f"""
            node["amenity"="pharmacy"]({bbox_str});
            way["amenity"="pharmacy"]({bbox_str});
        """,
        "resto": f"""
            node["amenity"~"^(restaurant|cafe|fast_food|bar)$"]({bbox_str});
            way["amenity"~"^(restaurant|cafe)$"]({bbox_str});
        """,
        "btp": f"""
            node["shop"~"^(hardware|doityourself|trade|paint|electrical|energy)$"]({bbox_str});
            node["craft"]({bbox_str});
            node["office"~"^(construction_company|architect|engineer|energy_supplier)$"]({bbox_str});
        """,
        "auto": f"""
            node["shop"~"^(car|car_repair|car_parts|tyres|motorcycle)$"]({bbox_str});
            node["amenity"="car_rental"]({bbox_str});
            node["office"~"^(logistics|transport|moving_company|courier)$"]({bbox_str});
        """,
        "commerce": f"""
            node["shop"~"^(supermarket|convenience|electronics|computer|mobile_phone|clothes|furniture|jewelry|shoes|boutique|department_store)$"]({bbox_str});
        """,
        "finance": f"""
            node["amenity"~"^(bank|microfinance|bureau_de_change)$"]({bbox_str});
            node["office"~"^(insurance|financial|accountant|lawyer|notary)$"]({bbox_str});
        """,
        "education": f"""
            node["amenity"~"^(university|college|training|language_school|driving_school)$"]({bbox_str});
            way["amenity"~"^(university|college)$"]({bbox_str});
        """,
        "voyage": f"""
            node["shop"="travel_agency"]({bbox_str});
            node["office"~"^(travel_agent|estate_agent)$"]({bbox_str});
        """,
        "agri": f"""
            node["shop"~"^(agrarian|farm)$"]({bbox_str});
            node["office"~"^(agriculture|forestry)$"]({bbox_str});
        """,
        "beaute": f"""
            node["shop"~"^(beauty|hairdresser|cosmetics|massage|perfumery|tattoo)$"]({bbox_str});
            node["leisure"="spa"]({bbox_str});
        """,
        "industrie": f"""
            node["man_made"="works"]({bbox_str});
            way["man_made"="works"]({bbox_str});
            node["building"="industrial"]({bbox_str});
        """,
        "event": f"""
            node["amenity"~"^(nightclub|events_venue|cinema|theatre|casino|community_centre)$"]({bbox_str});
            node["leisure"~"^(dance|stadium)$"]({bbox_str});
        """,
        "securite": f"""
            node["office"="security"]({bbox_str});
        """,
        "entreprise": f"""
            node["office"]({bbox_str});
            way["office"]({bbox_str});
        """,
    }

    if sector_filter in clauses_map:
        return clauses_map[sector_filter]

    # "all": fetch across all major B2B categories + explicit contact:facebook / website nodes
    return f"""
        node["tourism"="hotel"]({bbox_str});
        node["tourism"="guest_house"]({bbox_str});
        node["amenity"~"^(clinic|hospital|dentist|pharmacy|restaurant|cafe|university|college|bank|car_rental|nightclub|events_venue)$"]({bbox_str});
        node["man_made"="works"]({bbox_str});
        node["office"]({bbox_str});
        node["shop"]({bbox_str});
        node["craft"]({bbox_str});
    """


def fetch_osm_businesses(city_key: str = "ouaga", sector_filter: str = "all", limit: int = 75) -> list[BusinessLead]:
    """
    Queries OpenStreetMap Overpass API for private B2B businesses across 11 sectors
    in Ouagadougou or Bobo-Dioulasso, with local disk caching (`osm_cache_{city}.json`)
    so repeated sector scans from the phone never trigger Overpass 504 rate limits.
    """
    city_info = CITIES.get(city_key, CITIES["ouaga"])
    s, w, n, e = city_info["bbox"]
    bbox_str = f"{s},{w},{n},{e}"

    cache_file = Path(__file__).resolve().parent / f"osm_cache_{city_key}.json"
    raw_elements = []

    # 1. Check local cache first if we are scanning "all" or if cache already has full city data
    if cache_file.exists():
        try:
            cached_data = json.loads(cache_file.read_text(encoding="utf-8"))
            if len(cached_data) > 50:
                raw_elements = cached_data
                print(f"[+] Cache local OpenStreetMap chargé pour {city_info['name']} ({len(raw_elements)} éléments).")
        except Exception:
            pass

    # 2. If no cache yet, query Overpass API (using fast 'out tags;' on nodes)
    if not raw_elements:
        clauses = build_overpass_clauses(bbox_str, "all")
        overpass_query = f"""
        [out:json][timeout:25];
        (
          {clauses}
        );
        out tags;
        """

        endpoints = [
            "https://overpass-api.de/api/interpreter",
            "https://lz4.overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter",
        ]

        for url in endpoints:
            try:
                print(f"[*] Téléchargement OpenStreetMap ({url}) pour {city_info['name']}...")
                data = urllib.parse.urlencode({"data": overpass_query}).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers={"User-Agent": "ProspectBF/2.5 (Burkina B2B Auditor)"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                    raw_elements = payload.get("elements", [])
                    if raw_elements:
                        print(f"[+] Réponse reçue ({len(raw_elements)} éléments bruts) -> Sauvegarde dans {cache_file.name}")
                        cache_file.write_text(json.dumps(raw_elements, ensure_ascii=False), encoding="utf-8")
                        break
            except Exception as exc:
                print(f"[!] Avertissement sur {url}: {exc}")

    if not raw_elements:
        print("[!] Serveurs Overpass hors-ligne : chargement de la base B2B locale de secours.")
        return get_offline_fallback_leads(city_info["name"], sector_filter)

    leads: list[BusinessLead] = []
    seen_names: set[str] = set()

    for el in raw_elements:
        tags = el.get("tags", {})
        name = tags.get("name", "").strip()
        if not name or name.lower() in seen_names:
            continue

        # Exclude embassies, ministries, state utilities, police
        office_type = tags.get("office", "").lower()
        if office_type in EXCLUDED_OFFICE_TYPES:
            continue
        if any(kw in name.lower() for kw in EXCLUDED_NAME_KEYWORDS):
            continue

        phone_raw = (
            tags.get("phone")
            or tags.get("contact:phone")
            or tags.get("contact:mobile")
            or tags.get("contact:whatsapp")
            or tags.get("telephone")
            or ""
        ).strip()

        website = (
            tags.get("website")
            or tags.get("contact:website")
            or tags.get("url")
            or ""
        ).strip()

        fb_raw = (
            tags.get("contact:facebook")
            or tags.get("facebook")
            or tags.get("contact:instagram")
            or tags.get("instagram")
            or ""
        ).strip()
        facebook_url = normalize_facebook_url(fb_raw)

        # If the mapper put a Facebook/Instagram link inside 'website' or 'description'/'note'
        if website and is_social_media_url(website):
            if not facebook_url:
                facebook_url = website
            website = ""

        # Check if any other tag value contains a facebook.com link
        if not facebook_url:
            for k, v in tags.items():
                if isinstance(v, str) and ("facebook.com/" in v.lower() or "fb.com/" in v.lower()):
                    facebook_url = normalize_facebook_url(v)
                    break

        email = (tags.get("email") or tags.get("contact:email") or "").strip()

        # Must have at least one contact/digital channel
        if not phone_raw and not website and not facebook_url and not email:
            continue

        sector_key, sector_label = classify_business_sector(tags)
        if sector_filter != "all" and sector_key != sector_filter:
            continue

        seen_names.add(name.lower())

        neighborhood = (
            tags.get("addr:suburb")
            or tags.get("addr:neighbourhood")
            or tags.get("addr:district")
            or tags.get("addr:street")
            or tags.get("addr:place")
            or "Quartier non précisé"
        )

        el_center = el.get("center") or {}
        lat = el.get("lat") or el_center.get("lat", city_info.get("center", (12.3714, -1.5197))[0])
        lon = el.get("lon") or el_center.get("lon", city_info.get("center", (12.3714, -1.5197))[1])

        leads.append(
            BusinessLead(
                name=name,
                city=city_info["name"],
                neighborhood=neighborhood,
                sector=sector_label,
                sector_key=sector_key,
                phone_raw=phone_raw,
                phone_clean=normalize_bf_phone(phone_raw),
                website=website,
                facebook_url=facebook_url,
                email=email,
                lat=float(lat),
                lon=float(lon),
            )
        )

    # Sort leads by contact richness first
    leads.sort(
        key=lambda b: (
            bool(b.facebook_url) and not bool(b.website),
            bool(b.website),
            is_likely_mobile_bf(b.phone_clean),
            bool(b.phone_clean),
        ),
        reverse=True,
    )

    if sector_filter != "all":
        if not leads:
            return get_offline_fallback_leads(city_info["name"], sector_filter)[:limit]
        return leads[:limit]

    # When scanning "all", guarantee representation of:
    # 1) All Facebook-only leads
    # 2) At least 2-4 leads per sector across all 16 sectors
    # 3) Website-having leads to maximize dead/weak site discoveries
    selected: list[BusinessLead] = []
    selected_names: set[str] = set()

    def _add(item: BusinessLead) -> None:
        key = item.name.lower()
        if key not in selected_names and len(selected) < limit:
            selected_names.add(key)
            selected.append(item)

    # 1. Include Facebook-only leads first so the Facebook filter is always rich
    for b in leads:
        if b.facebook_url and not b.website:
            _add(b)

    # 2. Include up to 4 leads from each of the 16 sectors so every sector filter has results
    for skey in SECTOR_CATEGORIES:
        if skey == "all":
            continue
        sector_items = [b for b in leads if b.sector_key == skey]
        for b in sector_items[:4]:
            _add(b)
        # Supplement missing sectors from fallback dataset
        if len(sector_items) < 2:
            for b in get_offline_fallback_leads(city_info["name"], skey)[:2]:
                _add(b)

    # 3. Fill remaining slots with website-having leads and mobile-phone leads
    for b in leads:
        if b.website:
            _add(b)
    for b in leads:
        _add(b)

    return selected[:limit]


def get_offline_fallback_leads(city_name: str, sector_filter: str = "all") -> list[BusinessLead]:
    """Rich multi-sector fallback dataset for offline testing and 100% resilient sector scans."""
    city_center = (12.3714, -1.5197)
    city_key_found = "ouaga"
    for c_key, c_info in CITIES.items():
        if c_key == city_name.lower() or c_info.get("name", "").lower() == city_name.lower():
            city_center = c_info.get("center", city_center)
            city_key_found = c_key
            break

    city_neighborhoods = {
        "ouaga": ["Ouaga 2000", "Zone du Bois", "Gounghin", "Koulouba", "Karpala", "Zogona", "Kossodo", "Pissy", "Wemtenga", "Centre-Ville"],
        "bobo": ["Accart-Ville", "Bindougousso", "Koko", "Sarfalao", "Bolomakoté", "Belle-Ville", "Lafiabougou", "Colma"],
        "koudougou": ["Bourkina", "Dapoya", "Palogo", "Nayalgué", "Secteur 1", "Centre-Ville"],
        "banfora": ["Tatana", "Korona", "Flantama", "Bounouna", "Centre-Ville"],
        "ouahigouya": ["Tougou", "Yaadega", "Souli", "Secteur 2", "Centre-Ville"],
        "tenkodogo": ["Cissin", "Gambaga", "Zamsé", "Secteur 3", "Centre-Ville"],
        "kaya": ["Dem", "Basnéré", "Damesma", "Secteur 1", "Centre-Ville"],
    }
    neighs = city_neighborhoods.get(city_key_found, city_neighborhoods["ouaga"])

    samples = [
        # hotel (Hôtels, Auberges & Résidences)
        ("Hôtel Ricardo", "hotel", "Kossodo", "+226 25 35 61 00", "http://www.hotelricardo-bf.com", ""),
        ("Résidence Hôtelière La Paix", "hotel", "Ouaga 2000", "+226 65 12 34 56", "", "https://www.facebook.com/residencepaix"),
        ("Auberge & Suites Le Sahel", "hotel", "Centre-Ville", "+226 70 12 45 78", "", ""),

        # sante (Cliniques, Cabinets, Opticiens & Labos)
        ("Polyclinique Internationale de Ouagadougou", "sante", "Zone du Bois", "+226 70 20 11 22", "", ""),
        ("Clinique Dentaire du Faso", "sante", "Koulouba", "+226 76 45 89 10", "http://cliniquedentairedumanager.bf", ""),
        ("Centre Médical Sandof", "sante", "Gounghin", "+226 78 50 12 34", "", "https://www.facebook.com/cmsandof"),

        # pharmacie (Pharmacies & Dépôts)
        ("Pharmacie de la Gare", "pharmacie", "Centre-Ville", "+226 25 30 61 62", "", ""),
        ("Pharmacie Kadiogo", "pharmacie", "Pissy", "+226 70 41 85 96", "", "https://www.facebook.com/pharmaciekadiogo"),
        ("Grande Pharmacie de la Paix", "pharmacie", "Wemtenga", "+226 76 33 22 11", "", ""),

        # resto (Restaurants, Cafés & Maquis VIP)
        ("Restaurant Le Verdoyant", "resto", "Zone du Bois", "+226 70 21 33 44", "", "https://www.facebook.com/leverdoyantouaga"),
        ("Maquis Le Jardin Gourmand", "resto", "Ouaga 2000", "+226 75 14 25 36", "", ""),
        ("Café Pâtisserie Cappuccino Faso", "resto", "Kwame Nkrumah", "+226 66 77 88 99", "http://cappuccinofaso.com", ""),

        # btp (BTP, Quincailleries, Énergie & Artisans)
        ("SOPAM BTP & Quincaillerie", "btp", "Gounghin", "+226 70 88 99 00", "", ""),
        ("Faso Énergie Solaire & Travaux", "btp", "Dassasgho", "+226 74 12 89 63", "", "https://www.facebook.com/fasoenergiesolaire"),
        ("Burkina Matériaux & Construction", "btp", "Kossodo", "+226 78 96 54 12", "http://burkinamateriaux.bf", ""),

        # auto (Auto, Location Véhicules, Garages & Transport)
        ("Faso Auto Location & Logistique", "auto", "Karpala", "+226 78 11 22 33", "", "https://www.facebook.com/fasoautolocation"),
        ("Garage Central & Mécanique Moderne", "auto", "Larlé", "+226 71 23 45 67", "", ""),
        ("Sahel Fret & Transit International", "auto", "Aéroport", "+226 64 55 66 77", "http://sahelfret-bf.com", ""),

        # commerce (Supermarchés, Boutiques, Électronique & Mode)
        ("Marina Market Faso", "commerce", "Kwame Nkrumah", "+226 25 31 12 34", "http://marinamarket-bf.com", ""),
        ("Électronique & High-Tech Express", "commerce", "Rood-Woko", "+226 70 99 88 77", "", "https://www.facebook.com/electroniquehightech"),
        ("Prestige Mode & Prêt-à-Porter", "commerce", "Ouaga 2000", "+226 76 11 33 55", "", "https://www.facebook.com/prestigemodebf"),

        # finance (Assurances, Microfinance, Banques & Cabinets)
        ("Cabinet d'Assurance & Conseil Sahel", "finance", "Koulouba", "+226 76 90 12 34", "", ""),
        ("Faso Microfinance & Crédit Populaire", "finance", "Gounghin", "+226 70 33 66 99", "http://fasomicrofinance.bf", ""),
        ("Cabinet d'Expertise Comptable Savane", "finance", "Ouaga 2000", "+226 78 44 11 22", "", ""),

        # education (Universités, Grandes Écoles & Formation)
        ("Institut Supérieur de Management & Technologies", "education", "Zone du Bois", "+226 70 15 30 45", "http://ismt-burkina.edu.bf", ""),
        ("Université Privée de Ouagadougou UPO", "education", "Zogona", "+226 76 88 99 00", "", "https://www.facebook.com/upoburkina"),
        ("Académie Digitale & Formation Pro", "education", "Wemtenga", "+226 65 44 33 22", "", "https://www.facebook.com/academiedigitalefaso"),

        # voyage (Agences de Voyage, Tourisme & Immobilier)
        ("Agence Faso Voyages & Tourisme", "voyage", "Zogona", "+226 70 44 55 66", "", "https://www.facebook.com/fasovoyages"),
        ("Sahel Immobilier & Gestion Locative", "voyage", "Ouaga 2000", "+226 78 22 44 66", "http://sahelimmo-bf.com", ""),
        ("Air Sahel Tourisme & Billetterie", "voyage", "Centre-Ville", "+226 71 88 77 66", "", ""),

        # agri (Agriculture, Élevage & Agroalimentaire)
        ("Agro-Business Faso & Céréales", "agri", "Baskuy", "+226 70 55 88 11", "", "https://www.facebook.com/agrobusinessfaso"),
        ("Ferme Avicole & Maraîchage du Sahel", "agri", "Tanghin", "+226 76 12 34 99", "http://fermesahel-bf.com", ""),
        ("Faso Intrants Agricoles & Semences", "agri", "Kossodo", "+226 78 45 67 89", "", ""),

        # beaute (Beauté, Coiffure & Cosmétique)
        ("Salon Prestige Coiffure & Esthétique", "beaute", "Ouaga 2000", "+226 70 77 88 11", "", "https://www.facebook.com/prestigecoiffurebf"),
        ("Merveille Beauté Spa & Cosmétique", "beaute", "Zone du Bois", "+226 76 54 32 10", "", "https://www.facebook.com/merveillebeautespa"),
        ("Barber Shop VIP & Soins Homme", "beaute", "Koulouba", "+226 67 89 01 23", "", ""),

        # industrie (Industrie, Usines & Manufactures)
        ("Faso Plastique & Emballage Industriel", "industrie", "Zone Industrielle", "+226 25 35 10 20", "http://fasoplastique.bf", ""),
        ("Manufacture Métallique du Burkina", "industrie", "Zone Industrielle", "+226 70 24 68 10", "", ""),
        ("Savonnerie & Huilerie Industrielle du Sahel", "industrie", "Zone Industrielle", "+226 78 90 12 34", "", "https://www.facebook.com/huileriesahel"),

        # event (Événementiel, Loisirs & Nuit)
        ("Palais des Événements & Fêtes Faso", "event", "Ouaga 2000", "+226 70 11 44 77", "", "https://www.facebook.com/palaisfetesfaso"),
        ("Espace Culturel & Salle Polyvalente Sahel", "event", "Gounghin", "+226 76 33 55 77", "http://espacecultuelsahel.com", ""),
        ("L'Acoustic VIP Lounge & Événements", "event", "Zone du Bois", "+226 65 22 88 99", "", "https://www.facebook.com/acousticviplounge"),

        # securite (Sécurité Privée & Gardiennage)
        ("Société Burkinabè de Sécurité Privée (SBSP)", "securite", "Koulouba", "+226 25 33 44 55", "http://sbsp-securite.bf", ""),
        ("Faso Gardiennage & Vidéosurveillance", "securite", "Wemtenga", "+226 70 89 12 34", "", "https://www.facebook.com/fasogardiennage"),
        ("Vigiles du Sahel Protection", "securite", "Pissy", "+226 78 66 55 44", "", ""),

        # entreprise (Bureaux, Sociétés IT, Conseil & ONG)
        ("Sahel Tech Solutions & Informatique", "entreprise", "Centre-Ville", "+226 70 99 00 11", "http://saheltech-bf.com", "https://www.facebook.com/saheltech"),
        ("Cabinet Conseil B2B & Stratégie", "entreprise", "Ouaga 2000", "+226 76 22 33 44", "", ""),
        ("Faso Digital Agency & Communication", "entreprise", "Zone du Bois", "+226 64 11 22 33", "http://fasodigital.bf", "https://www.facebook.com/fasodigital"),
    ]
    out = []
    idx = 0
    for name, skey, neigh, ph, web, fb in samples:
        if sector_filter != "all" and skey != sector_filter:
            continue
        # Use city-adapted neighborhood
        chosen_neigh = neigh if city_key_found == "ouaga" else neighs[idx % len(neighs)]
        idx += 1
        out.append(
            BusinessLead(
                name=name,
                city=city_name,
                neighborhood=chosen_neigh,
                sector=SECTOR_CATEGORIES.get(skey, SECTOR_CATEGORIES["entreprise"]),
                sector_key=skey,
                phone_raw=ph,
                phone_clean=normalize_bf_phone(ph),
                website=web,
                facebook_url=fb,
                email="",
                lat=city_center[0],
                lon=city_center[1],
            )
        )
    return out


# ---------------------------------------------------------------------------
# 4. TECHNICAL AUDITOR & LEAD SCORING ENGINE
# ---------------------------------------------------------------------------

def audit_single_business(lead: BusinessLead) -> BusinessLead:
    """
    Inspects a single business's digital footprint (website + Facebook),
    detects technical flaws, extracts live contacts, and writes a custom sales pitch.
    """
    url = lead.website.strip()
    fb = lead.facebook_url.strip()

    # If the user entered a Facebook/Instagram link in the website field
    if url and is_social_media_url(url):
        if not fb:
            fb = url
            lead.facebook_url = url
        url = ""

    # CASE 1: Has a Facebook/Social page ONLY (no dedicated website domain)
    if not url and fb:
        lead.website = fb
        lead.audit_status = "FACEBOOK_SEUL"
        lead.issues.append("Utilise uniquement une page Facebook/Réseaux Sociaux (aucun site web propre)")
        lead.issues.append("Invisible sur les recherches Google classiques hors Facebook")
        lead.lead_score = 86 if is_likely_mobile_bf(lead.phone_clean) else 76
        lead.pitch_angle = (
            f"Bonjour {lead.name}, j'ai vu votre page Facebook ({fb}), mais vous n'avez pas encore "
            f"de site web professionnel à votre nom. À {lead.city}, beaucoup de clients et entreprises "
            f"cherchent sur Google avant de commander : je peux transformer votre page Facebook en vrai "
            f"site rapide avec catalogue/réservation relié directement à votre WhatsApp."
        )
        _attach_whatsapp_link(lead)
        return lead

    # CASE 2: Neither Website nor Facebook page referenced
    if not url and not fb:
        lead.audit_status = "SANS_SITE"
        lead.issues.append("Aucun site web ni page digitale référencée")
        lead.lead_score = 80 if is_likely_mobile_bf(lead.phone_clean) else (68 if lead.phone_clean else 50)
        lead.pitch_angle = (
            f"Bonjour {lead.name}, en cherchant vos services ({lead.sector}) à {lead.city}, "
            f"j'ai remarqué que vous n'avez pas encore de site web/vitrine digitale reliée à WhatsApp. "
            f"Vos futurs clients vous cherchent sur Google : je peux vous créer une vitrine professionnelle "
            f"avec bouton WhatsApp direct en 72h."
        )
        _attach_whatsapp_link(lead)
        return lead

    # CASE 3: Has a dedicated website URL -> Test it live!
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    lead.has_https = url.lower().startswith("https://")
    t0 = time.perf_counter()

    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36"
            },
        )
        with urllib.request.urlopen(req, timeout=7, context=ctx) as resp:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            lead.response_ms = elapsed_ms
            lead.http_code = resp.getcode() or 200
            final_url = resp.geturl() or url

            # Check if the domain redirected straight to Facebook/Instagram!
            if is_social_media_url(final_url):
                lead.facebook_url = final_url
                lead.audit_status = "FACEBOOK_SEUL"
                lead.issues.append("Le domaine renvoie uniquement vers une page réseau social")
                lead.lead_score = 84
                lead.pitch_angle = (
                    f"Bonjour {lead.name}, votre lien web redirige uniquement vers les réseaux sociaux "
                    f"au lieu d'afficher un vrai site officiel. Je peux vous mettre en place un vrai site "
                    f"rapide avec prise de contact WhatsApp intégrée."
                )
                _attach_whatsapp_link(lead)
                return lead

            lead.has_https = final_url.lower().startswith("https://")
            raw_bytes = resp.read(140_000)
            body_lower = raw_bytes.decode("utf-8", errors="ignore").lower()

            # Extract Facebook link from HTML if not already known
            if not lead.facebook_url:
                # Amélioration Regex pour capter profile.php?id=... et autres paramètres
                fb_matches = re.findall(r"https?://(?:www\.|m\.)?(?:facebook\.com|fb\.com|fb\.me)/[a-zA-Z0-9._\-?&/=]+", body_lower)
                if fb_matches:
                    lead.facebook_url = fb_matches[0].rstrip("-").rstrip("/")

            # Extract mobile +226 phone from HTML if missing or fixed-line
            if not is_likely_mobile_bf(lead.phone_clean):
                found_phones = re.findall(r"(?:\+226|00226)\s*[0567]\d(?:[\s.-]*\d{2}){3}", body_lower)
                if found_phones:
                    extracted = normalize_bf_phone(found_phones[0])
                    if is_likely_mobile_bf(extracted):
                        lead.phone_clean = extracted

            lead.has_viewport = 'name="viewport"' in body_lower or "name='viewport'" in body_lower
            lead.has_whatsapp_link = any(
                w in body_lower for w in ("wa.me/", "api.whatsapp.com", "whatsapp://")
            )

            # Detect Meta / Facebook Pixel
            lead.has_fb_pixel = bool(
                "connect.facebook.net" in body_lower
                or "fbevents.js" in body_lower
                or "facebook.com/tr/" in body_lower
                or "facebook.com/tr?" in body_lower
                or re.search(r"\bfbq\s*\(", body_lower)
            )

            # Detect Google Analytics / Google Tag Manager
            lead.has_google_analytics = bool(
                "googletagmanager.com" in body_lower
                or "google-analytics.com" in body_lower
                or re.search(r"\bgtag\s*\(", body_lower)
                or re.search(r"\bga\s*\(", body_lower)
            )

            # Technical flaws determine core website status
            technical_flaws: list[str] = []
            if not lead.has_https:
                technical_flaws.append("Site non sécurisé (HTTP sans cadenas SSL)")
            if elapsed_ms > 2500:
                technical_flaws.append(f"Site lent sur réseau mobile ({elapsed_ms} ms)")
            if not lead.has_viewport:
                technical_flaws.append("Non adapté aux téléphones (pas de viewport mobile)")
            if not lead.has_whatsapp_link:
                technical_flaws.append("Aucun bouton de contact rapide WhatsApp intégré")

            lead.issues.extend(technical_flaws)

            # Marketing tracking flaws (decoupled from technical status)
            if not lead.has_fb_pixel and not lead.has_google_analytics:
                lead.issues.append("Aucun Pixel FB ni Google Analytics détecté")
            elif not lead.has_fb_pixel:
                lead.issues.append("Pixel Facebook manquant (pas de retargeting)")
            elif not lead.has_google_analytics:
                lead.issues.append("Google Analytics manquant")

            if len(technical_flaws) >= 2 or not lead.has_https:
                lead.audit_status = "SITE_FAIBLE"
                lead.lead_score = 74 + min(18, len(technical_flaws) * 5)
                flaws_str = ", ".join(technical_flaws[:2]).lower()
                lead.pitch_angle = (
                    f"Bonjour {lead.name}, j'ai visité votre site ({lead.website}) depuis mon téléphone "
                    f"et j'ai relevé un souci technique qui freine vos clients : {flaws_str}. "
                    f"Je peux corriger cela rapidement et ajouter un bouton WhatsApp direct."
                )
            else:
                lead.audit_status = "SITE_OK"
                lead.lead_score = 35 if (lead.has_fb_pixel or lead.has_google_analytics) else 45
                lead.pitch_angle = (
                    f"Bonjour {lead.name}, votre site web fonctionne bien. Si vous souhaitez automatiser "
                    f"vos réponses clients sur WhatsApp ou vos réservations, faisons un point rapide."
                )

    except urllib.error.HTTPError as http_err:
        lead.response_ms = int((time.perf_counter() - t0) * 1000)
        lead.http_code = http_err.code
        lead.audit_status = "SITE_MORT"
        lead.issues.append(f"Erreur serveur HTTP {http_err.code} (site inaccessible)")
        lead.lead_score = 94
        lead.pitch_angle = (
            f"Bonjour {lead.name}, je voulais consulter votre site web ({lead.website}) mais il affiche "
            f"une erreur technique (HTTP {http_err.code}). Vos clients tombent sur une page cassée : "
            f"je peux vous le remettre en ligne proprement cette semaine."
        )
    except Exception as exc:
        lead.response_ms = int((time.perf_counter() - t0) * 1000)
        lead.audit_status = "SITE_MORT"
        reason = "Certificat SSL expiré" if "SSL" in str(exc) else "Domaine expiré ou serveur hors-ligne"
        lead.issues.append(f"Site hors-ligne ({reason})")
        lead.lead_score = 96
        lead.pitch_angle = (
            f"Bonjour {lead.name}, saviez-vous que votre site web ({lead.website}) est actuellement "
            f"inaccessible ({reason}) ? Vous avez déjà investi dedans mais vos clients ne peuvent plus "
            f"vous voir. Je peux le réparer et le moderniser rapidement."
        )

    _attach_whatsapp_link(lead)
    return lead


def _attach_whatsapp_link(lead: BusinessLead) -> None:
    if is_likely_mobile_bf(lead.phone_clean):
        wa_num = lead.phone_clean.lstrip("+")
        encoded_msg = urllib.parse.quote(lead.pitch_angle)
        lead.whatsapp_url = f"https://wa.me/{wa_num}?text={encoded_msg}"


# ---------------------------------------------------------------------------
# 5. CSV EXPORT
# ---------------------------------------------------------------------------

def export_csv(leads: list[BusinessLead], output_path: Path) -> None:
    with output_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Score (/100)",
            "Statut Digital",
            "Statut CRM",
            "Dans Pipeline",
            "RDV Fait",
            "Montant Deal (FCFA)",
            "Notes CRM",
            "Entreprise",
            "Secteur",
            "Ville",
            "Quartier",
            "Téléphone (+226)",
            "Email",
            "Site Web",
            "Page Facebook",
            "Temps (ms)",
            "Problèmes Détectés",
            "Message de Prospection Suggéré",
            "Lien WhatsApp Direct",
        ])
        for b in leads:
            writer.writerow([
                b.lead_score,
                b.audit_status,
                getattr(b, "crm_status", "A_CONTACTER"),
                "OUI" if getattr(b, "in_pipeline", False) else "NON",
                "OUI" if getattr(b, "rdv_done", False) else "NON",
                getattr(b, "deal_amount", 0) or 0,
                getattr(b, "notes", "") or "",
                b.name,
                b.sector,
                b.city,
                b.neighborhood,
                b.phone_clean or b.phone_raw,
                getattr(b, "email", "") or "",
                b.website,
                b.facebook_url,
                b.response_ms or "",
                " | ".join(b.issues),
                b.pitch_angle,
                b.whatsapp_url,
            ])


if __name__ == "__main__":
    city_arg = sys.argv[1].lower() if len(sys.argv) > 1 else "ouaga"
    sector_arg = sys.argv[2].lower() if len(sys.argv) > 2 else "all"
    limit_arg = int(sys.argv[3]) if len(sys.argv) > 3 else 60
    raw = fetch_osm_businesses(city_arg, sector_arg, limit=limit_arg)
    with concurrent.futures.ThreadPoolExecutor(max_workers=14) as pool:
        audited = list(pool.map(audit_single_business, raw))
    from collections import Counter
    print("Répartition par statut :", Counter(b.audit_status for b in audited))
    print("Répartition par secteur :", Counter(b.sector_key for b in audited))
