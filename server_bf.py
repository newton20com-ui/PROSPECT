"""
server_bf.py — Serveur Web Interactif Mobile-First (API Backend + Interface Mobile Responsive) pour ProspectBF v2.6.
Optimisé spécifiquement pour les écrans de smartphones (Android / iOS 360px-430px) et PC.
"""

from __future__ import annotations

import concurrent.futures
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from prospect_bf import (
    SECTOR_CATEGORIES,
    BusinessLead,
    audit_single_business,
    export_csv,
    fetch_osm_businesses,
    is_likely_mobile_bf,
    normalize_bf_phone,
)

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "leads_state.json"


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
    return {
        "name": b.name,
        "city": b.city,
        "neighborhood": b.neighborhood,
        "sector": b.sector,
        "sector_key": b.sector_key,
        "phone": phone_display,
        "phone_type": "MOBILE_WHATSAPP" if is_mobile else ("FIXE_ONATEL" if b.phone_clean else "AUCUN"),
        "tel_url": f"tel:{b.phone_clean}" if b.phone_clean else "",
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
        "crm_status": getattr(b, "crm_status", "A_CONTACTER") or "A_CONTACTER",
        "has_fb_pixel": bool(getattr(b, "has_fb_pixel", False)),
        "has_google_analytics": bool(getattr(b, "has_google_analytics", False)),
    }


STATE_LOCK = threading.RLock()


def load_saved_leads() -> list[dict]:
    from urllib.parse import quote_plus
    with STATE_LOCK:
        if STATE_FILE.exists():
            try:
                raw_leads = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(raw_leads, list):
                    normalized = []
                    for item in raw_leads:
                        if not isinstance(item, dict):
                            continue
                        lead = dict(item)
                        if not lead.get("crm_status"):
                            lead["crm_status"] = "A_CONTACTER"
                        lead["has_fb_pixel"] = bool(lead.get("has_fb_pixel", False))
                        lead["has_google_analytics"] = bool(lead.get("has_google_analytics", False))
                        if not lead.get("maps_url"):
                            lat = lead.get("lat")
                            lon = lead.get("lon")
                            city = lead.get("city", "")
                            name = lead.get("name", "")
                            if lat and lon and city != "Audit Direct":
                                lead["maps_url"] = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
                            else:
                                loc = city if city and city != "Audit Direct" else "Burkina Faso"
                                q = quote_plus(f"{name} {loc}".strip())
                                lead["maps_url"] = f"https://www.google.com/maps/search/?api=1&query={q}"
                        normalized.append(lead)
                    return normalized
            except Exception:
                pass
    return []


def save_leads(leads_dicts: list[dict]) -> None:
    with STATE_LOCK:
        STATE_FILE.write_text(json.dumps(leads_dicts, ensure_ascii=False, indent=2), encoding="utf-8")


def merge_leads(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Fusionne les nouveaux leads avec les existants sans écraser les statuts CRM."""
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
            combined = dict(existing_lead)
            combined.update(entry)
            if existing_status:
                combined["crm_status"] = existing_status
            else:
                combined["crm_status"] = entry.get("crm_status") or "A_CONTACTER"
            merged_map[key] = combined
        else:
            if not entry.get("crm_status"):
                entry["crm_status"] = "A_CONTACTER"
            merged_map[key] = entry

    for lead in merged_map.values():
        lead.setdefault("has_fb_pixel", False)
        lead.setdefault("has_google_analytics", False)
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
  <title>ProspectBF — Audit B2B Burkina Faso</title>
  <style>
    :root {
      --bg: #0b0f17;
      --card: #151c28;
      --card-2: #1c2536;
      --border: #28354a;
      --text: #f1f5f9;
      --muted: #94a3b8;
      --red: #ef4444;
      --amber: #f59e0b;
      --green: #10b981;
      --blue: #3b82f6;
      --purple: #a855f7;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      -webkit-tap-highlight-color: transparent;
    }

    html, body {
      background: var(--bg);
      color: var(--text);
      width: 100%;
      max-width: 100vw;
      overflow-x: hidden;
      line-height: 1.45;
    }

    body {
      padding: 12px 10px 48px;
    }

    @media (min-width: 768px) {
      body { padding: 20px 24px 60px; }
    }

    .container {
      width: 100%;
      max-width: 1180px;
      margin: 0 auto;
    }

    /* Compact Mobile Header */
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 12px;
      padding-bottom: 10px;
      border-bottom: 1px solid var(--border);
    }

    .brand-row {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }

    h1 {
      font-size: 1.22rem;
      font-weight: 800;
      letter-spacing: -0.02em;
    }

    @media (min-width: 768px) {
      h1 { font-size: 1.5rem; }
    }

    .badge-bf {
      background: linear-gradient(90deg, #ef4444 50%, #10b981 50%);
      color: #fff;
      padding: 2px 7px;
      border-radius: 4px;
      font-size: 0.68rem;
      font-weight: 700;
      text-transform: uppercase;
    }

    /* Mode Switcher Tabs (Saves vertical space on mobile!) */
    .tool-tabs {
      display: grid;
      grid-template-columns: 1fr 1fr auto;
      gap: 6px;
      margin-bottom: 12px;
      background: #0f1520;
      padding: 5px;
      border-radius: 10px;
      border: 1px solid var(--border);
    }

    .tab-btn {
      background: transparent;
      color: var(--muted);
      border: none;
      padding: 9px 8px;
      border-radius: 7px;
      font-size: 0.82rem;
      font-weight: 700;
      cursor: pointer;
      text-align: center;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      min-height: 42px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }

    .tab-btn.active {
      background: var(--card-2);
      color: #fff;
      border: 1px solid var(--border);
    }

    .panel {
      display: none;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 12px;
      margin-bottom: 12px;
    }

    .panel.open {
      display: block;
    }

    .panel h2 {
      font-size: 0.94rem;
      margin-bottom: 4px;
      color: #f8fafc;
    }

    .panel p {
      font-size: 0.78rem;
      color: var(--muted);
      margin-bottom: 10px;
    }

    .form-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 8px;
      margin-bottom: 10px;
    }

    @media (min-width: 640px) {
      .form-grid { grid-template-columns: 1fr 1fr; }
    }

    /* 16px font-size prevents iOS/Android auto-zoom on focus! */
    input, select {
      width: 100%;
      max-width: 100%;
      background: #0f1520;
      color: var(--text);
      border: 1px solid var(--border);
      padding: 10px 11px;
      border-radius: 8px;
      font-size: 16px;
      min-height: 42px;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    @media (min-width: 768px) {
      input, select { font-size: 0.88rem; }
    }

    .btn {
      text-decoration: none;
      text-align: center;
      padding: 10px 12px;
      border-radius: 8px;
      font-weight: 700;
      font-size: 0.86rem;
      cursor: pointer;
      border: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      min-height: 42px;
      width: 100%;
      touch-action: manipulation;
    }

    .btn-primary { background: var(--blue); color: #fff; }
    .btn-amber { background: var(--amber); color: #0b0f17; }
    .btn-reset { background: #1e293b; color: #cbd5e1; border: 1px solid var(--border); }
    .btn:disabled { opacity: 0.55; cursor: wait; }

    /* Horizontal Swipeable / Responsive KPI Bar */
    .kpi-strip {
      display: flex;
      gap: 8px;
      overflow-x: auto;
      padding-bottom: 8px;
      margin-bottom: 12px;
      scrollbar-width: none;
      -webkit-overflow-scrolling: touch;
      scroll-snap-type: x mandatory;
    }
    .kpi-strip::-webkit-scrollbar { display: none; }

    .kpi {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 9px;
      padding: 9px 10px;
      cursor: pointer;
      user-select: none;
      text-align: left;
      flex: 0 0 auto;
      min-width: 110px;
      scroll-snap-align: start;
    }

    .kpi.active-kpi {
      border-color: var(--blue);
      background: var(--card-2);
      box-shadow: 0 0 0 1px var(--blue);
    }

    .kpi-val {
      font-size: 1.25rem;
      font-weight: 800;
      line-height: 1.15;
    }

    .kpi-label {
      color: var(--muted);
      font-size: 0.72rem;
      margin-top: 2px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    /* Responsive Filter Panel */
    .filters-box {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px;
      margin-bottom: 10px;
    }

    .search-row {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      margin-bottom: 8px;
    }

    .search-row .btn-reset {
      width: auto;
      padding: 0 12px;
      white-space: nowrap;
      font-size: 0.8rem;
    }

    .selects-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 8px;
    }

    @media (min-width: 600px) {
      .selects-grid {
        grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      }
    }

    .filter-summary {
      font-size: 0.78rem;
      color: var(--muted);
      margin-bottom: 10px;
      padding: 0 2px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    /* Mobile-First Lead Cards */
    .lead-list {
      display: grid;
      grid-template-columns: 1fr;
      gap: 10px;
    }

    .lead-card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 12px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      width: 100%;
      overflow: hidden;
    }

    @media (min-width: 860px) {
      .lead-card {
        display: grid;
        grid-template-columns: 1fr 200px;
        align-items: center;
        padding: 16px;
        gap: 16px;
      }
    }

    .card-top {
      display: flex;
      align-items: flex-start;
      gap: 10px;
    }

    .score-badge {
      flex: 0 0 46px;
      height: 46px;
      background: #0f1520;
      border: 1px solid var(--border);
      border-radius: 8px;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
    }

    .score-num {
      font-size: 1.05rem;
      font-weight: 800;
      line-height: 1;
    }

    .score-lbl {
      font-size: 0.56rem;
      color: var(--muted);
      text-transform: uppercase;
      margin-top: 2px;
    }

    .title-wrap {
      flex: 1;
      min-width: 0;
    }

    .biz-name {
      font-size: 1rem;
      font-weight: 800;
      color: #fff;
      word-break: break-word;
      display: block;
      margin-bottom: 4px;
    }

    .badges-row {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      align-items: center;
    }

    .status-pill {
      display: inline-block;
      padding: 2px 7px;
      border-radius: 999px;
      font-size: 0.68rem;
      font-weight: 700;
    }

    .st-SITE_MORT { background: rgba(239,68,68,0.2); color: #fca5a5; border: 1px solid #ef4444; }
    .st-SITE_FAIBLE { background: rgba(245,158,11,0.2); color: #fcd34d; border: 1px solid #f59e0b; }
    .st-FACEBOOK_SEUL { background: rgba(168,85,247,0.2); color: #d8b4fe; border: 1px solid #a855f7; }
    .st-SANS_SITE { background: rgba(59,130,246,0.2); color: #93c5fd; border: 1px solid #3b82f6; }
    .st-SITE_OK { background: rgba(16,185,129,0.2); color: #6ee7b7; border: 1px solid #10b981; }

    .phone-badge {
      font-size: 0.68rem;
      padding: 2px 6px;
      border-radius: 4px;
      font-weight: 600;
    }
    .ph-MOBILE_WHATSAPP { background: rgba(16,185,129,0.18); color: #6ee7b7; }
    .ph-FIXE_ONATEL { background: rgba(148,163,184,0.2); color: #cbd5e1; }

    /* Clean Wrapping Meta Info Grid */
    .meta-chips {
      display: flex;
      flex-wrap: wrap;
      gap: 6px 12px;
      font-size: 0.79rem;
      color: var(--muted);
      margin-top: 8px;
    }

    .meta-item {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      max-width: 100%;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .meta-item a {
      color: #60a5fa;
      text-decoration: none;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .issues {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      margin-top: 8px;
    }

    .issue-tag {
      background: #1e293b;
      color: #e2e8f0;
      font-size: 0.72rem;
      padding: 3px 7px;
      border-radius: 5px;
      line-height: 1.3;
    }

    .pitch-box {
      background: #0f1520;
      border-left: 3px solid var(--amber);
      padding: 8px 10px;
      font-size: 0.78rem;
      color: #cbd5e1;
      border-radius: 0 6px 6px 0;
      margin-top: 8px;
      overflow-wrap: anywhere;
    }

    /* Action Buttons: Side-by-side on mobile, stacked column on desktop */
    .actions {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(125px, 1fr));
      gap: 7px;
      width: 100%;
    }

    @media (min-width: 860px) {
      .actions {
        grid-template-columns: 1fr;
      }
    }

    .btn-wa { background: #10b981; color: #052e16; }
    .btn-fb { background: #1877f2; color: #ffffff; }
    .btn-call { background: #3b82f6; color: #ffffff; }
    .btn-maps { background: #0f766e; color: #ffffff; border: 1px solid #14b8a6; }
    .btn-copy { background: #1e293b; color: var(--text); border: 1px solid var(--border); }

    .empty-box {
      text-align: center;
      padding: 28px 14px;
      background: var(--card);
      border: 1px dashed var(--border);
      border-radius: 10px;
      color: var(--muted);
    }

    #statusBanner {
      display: none;
      margin-bottom: 12px;
      padding: 10px 12px;
      border-radius: 8px;
      background: #1e293b;
      border: 1px solid var(--blue);
      color: #93c5fd;
      font-size: 0.82rem;
      font-weight: 600;
    }
    /* Offer Selector Card (R3) */
    .offer-selector-card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px 12px;
      margin-bottom: 12px;
    }

    .offer-header {
      display: flex;
      flex-direction: column;
      gap: 2px;
      margin-bottom: 8px;
    }

    @media (min-width: 600px) {
      .offer-header {
        flex-direction: row;
        align-items: center;
        gap: 10px;
      }
    }

    .offer-badge-title {
      font-size: 0.72rem;
      font-weight: 800;
      color: var(--amber);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }

    .offer-subtitle {
      font-size: 0.74rem;
      color: var(--muted);
    }

    .offer-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 6px;
    }

    @media (min-width: 680px) {
      .offer-grid {
        grid-template-columns: repeat(3, 1fr);
      }
    }

    .offer-card-btn {
      background: #0f1520;
      border: 1px solid var(--border);
      color: var(--text);
      padding: 8px 10px;
      border-radius: 8px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 8px;
      text-align: left;
      transition: all 0.15s ease;
      touch-action: manipulation;
      min-height: 42px;
    }

    .offer-card-btn:hover {
      border-color: #3b82f6;
    }

    .offer-card-btn.active {
      background: rgba(59, 130, 246, 0.16);
      border-color: var(--blue);
      box-shadow: 0 0 0 1px var(--blue);
    }

    .offer-icon {
      font-size: 1.25rem;
      flex: 0 0 auto;
    }

    .offer-btn-text {
      display: flex;
      flex-direction: column;
      min-width: 0;
    }

    .offer-name {
      font-size: 0.82rem;
      font-weight: 700;
      color: #fff;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .offer-desc {
      font-size: 0.68rem;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    /* 4-Pill Segmented Mini-CRM Control (R1) */
    .crm-bar {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 4px;
      background: #0f1520;
      padding: 3px;
      border-radius: 8px;
      border: 1px solid var(--border);
      margin-top: 8px;
    }

    .crm-pill {
      background: transparent;
      border: 1px solid transparent;
      color: var(--muted);
      font-size: 0.70rem;
      font-weight: 700;
      padding: 6px 3px;
      border-radius: 6px;
      cursor: pointer;
      text-align: center;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      transition: all 0.15s ease-in-out;
      touch-action: manipulation;
      min-height: 42px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }

    .crm-pill:hover {
      background: rgba(255, 255, 255, 0.05);
    }

    .crm-pill.crm-a-contacter.active {
      background: #1e293b;
      border-color: #64748b;
      color: #f8fafc;
      box-shadow: 0 1px 3px rgba(0,0,0,0.3);
    }

    .crm-pill.crm-contacte.active {
      background: rgba(245, 158, 11, 0.22);
      border-color: var(--amber);
      color: #fde68a;
      box-shadow: 0 0 6px rgba(245, 158, 11, 0.25);
    }

    .crm-pill.crm-client.active {
      background: rgba(16, 185, 129, 0.25);
      border-color: var(--green);
      color: #6ee7b7;
      box-shadow: 0 0 8px rgba(16, 185, 129, 0.3);
    }

    .crm-pill.crm-ignore.active {
      background: rgba(239, 68, 68, 0.2);
      border-color: var(--red);
      color: #fca5a5;
    }

    .lead-card.crm-state-IGNORE {
      opacity: 0.55;
      border-color: #334155;
    }

    /* Tracking Badges (R4) */
    .tag-pixel { background: rgba(59, 130, 246, 0.2); color: #93c5fd; border: 1px solid #3b82f6; }
    .tag-analytics { background: rgba(245, 158, 11, 0.2); color: #fde68a; border: 1px solid #f59e0b; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="brand-row">
        <h1>ProspectBF</h1>
        <span class="badge-bf">BURKINA B2B</span>
      </div>
      <span style="font-size:0.76rem; color:var(--muted);">11 Secteurs • Temps Réel</span>
    </header>

    <div id="statusBanner"></div>

    <!-- COMPACT TABS FOR TOOLS (Mobile Friendly!) -->
    <div class="tool-tabs">
      <button id="tabScan" class="tab-btn active" onclick="switchTab('scan')">🔍 Scanner Ville/Secteur</button>
      <button id="tabSingle" class="tab-btn" onclick="switchTab('single')">⚡ Auditer un Site/Nom</button>
      <button id="tabHide" class="tab-btn" onclick="switchTab('none')" title="Réduire">🔼</button>
    </div>

    <!-- PANEL SCAN VILLE/SECTEUR -->
    <div id="panelScan" class="panel open">
      <h2>🔍 Scanner une Ville & un Secteur B2B</h2>
      <p>Choisis un secteur précis ou toute la ville pour auditer les entreprises en parallèle :</p>
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
      <h2>⚡ Auditer une Entreprise, un Site ou un Facebook</h2>
      <p>Teste n'importe quelle boîte trouvée sur Google Maps, Facebook ou dans la rue :</p>
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

    <!-- SWIPEABLE / TAP KPI STRIP -->
    <div class="kpi-strip">
      <div id="kpiCard_ALL" class="kpi active-kpi" onclick="quickFilterStatus('ALL')">
        <div class="kpi-val" id="kpiTotal">0</div>
        <div class="kpi-label">📊 Tout voir</div>
      </div>
      <div id="kpiCard_SITE_MORT" class="kpi" onclick="quickFilterStatus('SITE_MORT')">
        <div class="kpi-val" id="kpiDead" style="color: var(--red);">0</div>
        <div class="kpi-label">🔥 Sites Cassés</div>
      </div>
      <div id="kpiCard_FACEBOOK_SEUL" class="kpi" onclick="quickFilterStatus('FACEBOOK_SEUL')">
        <div class="kpi-val" id="kpiFb" style="color: var(--purple);">0</div>
        <div class="kpi-label">📱 Facebook Seul</div>
      </div>
      <div id="kpiCard_SITE_FAIBLE" class="kpi" onclick="quickFilterStatus('SITE_FAIBLE')">
        <div class="kpi-val" id="kpiWeak" style="color: var(--amber);">0</div>
        <div class="kpi-label">⚠️ Sites Lents</div>
      </div>
      <div id="kpiCard_SANS_SITE" class="kpi" onclick="quickFilterStatus('SANS_SITE')">
        <div class="kpi-val" id="kpiNoSite" style="color: var(--blue);">0</div>
        <div class="kpi-label">🚀 Sans Site</div>
      </div>
    </div>

    <!-- SERVICE A VENDRE / SALES OFFER SELECTOR -->
    <div class="offer-selector-card">
      <div class="offer-header">
        <span class="offer-badge-title">🎯 SERVICE À VENDRE</span>
        <span class="offer-subtitle">Adapte instantanément le pitch et le message WhatsApp sur chaque fiche :</span>
      </div>
      <div class="offer-grid">
        <button type="button" id="offerBtn_WEB" class="offer-card-btn active" onclick="setSalesOffer('WEB')">
          <span class="offer-icon">🌐</span>
          <div class="offer-btn-text">
            <span class="offer-name">Site Web & Refonte</span>
            <span class="offer-desc">Vitrine, Vitesse, SSL, Mobile</span>
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
    </div>

    <!-- RESPONSIVE FILTERS -->
    <div class="filters-box">
      <div class="search-row">
        <input type="search" id="searchInput" placeholder="🔎 Chercher nom, quartier, défaut..." />
        <button class="btn btn-reset" onclick="resetFilters()">🔄 Tout</button>
      </div>
      <div class="selects-grid">
        <select id="cityFilter"></select>
        <select id="crmFilter"></select>
        <select id="statusFilter"></select>
        <select id="sectorFilter"></select>
        <select id="phoneFilter"></select>
      </div>
    </div>

    <div class="filter-summary">
      <span id="filterCountText">Chargement...</span>
      <div style="display:flex; gap:8px;">
        <button class="btn btn-reset" style="padding:6px 12px; font-size:0.76rem;" onclick="exportLeadsCsv()">📥 Export CSV</button>
        <button class="btn btn-reset" style="padding:6px 12px; font-size:0.76rem; color:#fca5a5;" onclick="clearAllLeads()">🗑️ Vider</button>
      </div>
    </div>

    <div id="leadContainer" class="lead-list"></div>
  </div>

  <script>
    let LEADS = [];
    const STORAGE_KEY = 'prospectbf_leads_v26';
    let CURRENT_OFFER = 'WEB';
    try {
      CURRENT_OFFER = localStorage.getItem('prospectbf_offer') || localStorage.getItem('prospectbf_sales_offer') || 'WEB';
    } catch (e) {}

    const safeAttrEnc = (str) => encodeURIComponent(str || '').replace(/'/g, '%27');

    function escapeHtml(str) {
      if (!str) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
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
      });
      render();
    }

    function buildWhatsAppUrl(phone, pitch) {
      if (!phone || phone === 'Non renseigné') return '';
      const digits = phone.replace(/[^0-9]/g, '');
      if (!digits) return '';
      return 'https://wa.me/' + digits + '?text=' + encodeURIComponent(pitch);
    }

    function copyPitchText(btn, text) {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement('textarea');
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
      btn.textContent = '✅ Copié !';
      setTimeout(() => { btn.textContent = '📋 Copier Pitch'; }, 1500);
    }

    async function setLeadCrmStatus(name, newStatus, city) {
      const match = LEADS.find(l => {
        const sameName = (l.name || '').trim().toLowerCase() === (name || '').trim().toLowerCase();
        if (city && city !== 'Audit Direct') {
          return sameName && (l.city || '').trim().toLowerCase() === city.trim().toLowerCase();
        }
        return sameName;
      });
      if (match) {
        match.crm_status = newStatus;
        saveLocalBackup();
        populateFilterDropdowns();
        render();
        try {
          await fetch('/api/leads/status', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: match.name, city: match.city, crm_status: newStatus })
          });
        } catch (e) {
          console.warn('Backend CRM sync failed, cached locally', e);
        }
      }
    }

    function generatePitch(lead, offer) {
      const off = offer || CURRENT_OFFER || 'WEB';
      const name = lead.name || "l'entreprise";
      const city = lead.city && lead.city !== 'Audit Direct' ? lead.city : 'Burkina Faso';
      const sector = SECTOR_LABELS[lead.sector_key] || lead.sector || "votre secteur";
      const neighborhood = lead.neighborhood && lead.neighborhood !== 'Quartier non précisé' ? `vers ${lead.neighborhood}` : `à ${city}`;
      const status = lead.status || 'SANS_SITE';
      const issues = lead.issues || [];

      if (off === 'WEB') {
        if (status === 'SITE_MORT') {
          return `Bonjour ${name}, je voulais consulter votre site web (${lead.website || 'actuel'}) à ${city} mais il affiche une page d'erreur technique (en panne ou hors-ligne). Vos clients potentiels tombent sur une page cassée : je peux vous le remettre en ligne rapidement avec une refonte moderne et un bouton WhatsApp direct. Dispo pour en parler ?`;
        }
        if (status === 'SITE_FAIBLE') {
          const flaws = issues.filter(iss => !iss.includes('Pixel') && !iss.includes('Analytics')).slice(0, 2).join(', ').toLowerCase() || "la lenteur sur mobile et l'absence de cadenas SSL sécurisé";
          return `Bonjour ${name}, j'ai visité votre site web (${lead.website}) depuis mon téléphone à ${city}. J'ai relevé des défauts techniques qui freinent vos clients (${flaws}). Je peux moderniser votre vitrine pour un chargement en 1s sur smartphone avec contact WhatsApp direct. Auriez-vous 5 minutes pour échanger ?`;
        }
        if (status === 'FACEBOOK_SEUL') {
          return `Bonjour ${name}, votre page Facebook à ${city} est active, mais vous n'avez pas de site web officiel. Sur Google, les clients qui cherchent des services de ${sector} commandent chez des concurrents avec vitrine officielle. Je vous propose un site vitrine rapide relié à votre WhatsApp en 72h. Quand seriez-vous disponible ?`;
        }
        if (status === 'SANS_SITE') {
          return `Bonjour ${name}, en recherchant vos services (${sector}) à ${city}, j'ai remarqué que vous n'avez pas encore de vitrine digitale. Vos futurs clients vous cherchent sur Google : je peux vous créer un site vitrine professionnel avec catalogue et bouton WhatsApp direct en 72h. Pouvons-nous échanger quelques minutes ?`;
        }
        if (status === 'SITE_OK') {
          return `Bonjour ${name}, votre site web fonctionne bien. Nous accompagnons les entreprises de ${sector} à ${city} pour y intégrer des fonctionnalités de prise de rendez-vous automatique et un tunnel WhatsApp pour doubler vos demandes de devis. Seriez-vous ouvert à une démonstration ?`;
        }
        return `Bonjour ${name}, nous vous proposons un site web moderne, rapide et optimisé mobile avec bouton WhatsApp direct pour booster votre activité à ${city}. Échangeons rapidement !`;
      }

      if (off === 'ADS') {
        const hasPixel = lead.has_fb_pixel;
        const hasGA = lead.has_google_analytics;
        if (!hasPixel || !hasGA || issues.some(iss => iss.includes('Pixel') || iss.includes('Analytics'))) {
          return `Bonjour ${name}, nous avons analysé votre présence digitale à ${city} : vous recevez des visiteurs mais sans Pixel Facebook ni outil de suivi, vous perdez 95% de vos prospects sans pouvoir les recibler. Nous gérons vos publicités sponsorisées Facebook & Instagram ciblées et configurons le pixel de retargeting pour convertir les personnes intéressées directement sur WhatsApp. Avez-vous 5 minutes pour en discuter ?`;
        }
        if (lead.facebook_url || status === 'FACEBOOK_SEUL') {
          return `Bonjour ${name}, j'ai vu votre page Facebook à ${city}. Les publications gratuites ne touchent plus qu'une infime partie de votre audience. Nous pilotons des campagnes de publicités sponsorisées Facebook & Meta Ads ultra-ciblées sur les clients recherchant ${sector} à ${city}, avec recibler des visiteurs pour générer des contacts qualifiés sur WhatsApp. Seriez-vous disponible cette semaine ?`;
        }
        return `Bonjour ${name}, à ${city}, vos clients potentiels passent des heures chaque jour sur Facebook et Instagram. Nous créons et gérons des campagnes de publicité sponsorisée Meta Ads pour ${sector} afin de générer des messages de clients qualifiés directement sur votre WhatsApp grâce au ciblage et au retargeting pixel. Quand pouvons-nous en parler ?`;
      }

      if (off === 'MAPS') {
        if (lead.neighborhood && lead.neighborhood !== 'Quartier non précisé') {
          return `Bonjour ${name}, lorsqu'un client recherche "${sector}" ${neighborhood} ou à ${city} sur Google Maps, est-ce que votre entreprise ressort dans les 3 premiers résultats ? Nous optimisons votre visibilité Google Maps (SEO local, Top 3 maps, avis clients et photos) pour capter tous les appels et visites de votre zone. Souhaitez-vous un audit gratuit de votre fiche Maps ?`;
        }
        return `Bonjour ${name}, à ${city}, la majorité des clients recherchent "${sector}" directement sur Google Maps depuis leur téléphone portable. Nous positionnons votre fiche Google Maps dans le Top 3 local grâce au SEO local et aux avis clients pour multiplier vos appels et visites. Dispo pour un test rapide de votre positionnement ?`;
      }

      return `Bonjour ${name}, je vous contacte pour développer la visibilité de votre entreprise à ${city}. Pouvons-nous échanger ?`;
    }

    function dedupeAndSortLeads(list) {
      const map = new Map();
      list.forEach(item => {
        if (!item || !item.name) return;
        const key = `${item.name.trim().toLowerCase()}|${(item.city || '').trim().toLowerCase()}`;
        if (map.has(key)) {
          const prev = map.get(key);
          const combined = Object.assign({}, prev, item);
          if (prev.crm_status && prev.crm_status !== 'A_CONTACTER' && (!item.crm_status || item.crm_status === 'A_CONTACTER')) {
            combined.crm_status = prev.crm_status;
          }
          map.set(key, combined);
        } else {
          if (!item.crm_status) item.crm_status = 'A_CONTACTER';
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

    function saveLocalBackup() {
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(LEADS));
      } catch (e) {}
    }

    function loadLocalBackup() {
      try {
        const raw = localStorage.getItem(STORAGE_KEY);
        const parsed = raw ? JSON.parse(raw) : [];
        return Array.isArray(parsed) ? parsed : [];
      } catch (e) {
        return [];
      }
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
      } catch (e) {
        return url.slice(0, 28);
      }
    }

    function showBanner(msg, isError = false) {
      const b = document.getElementById('statusBanner');
      b.style.display = 'block';
      b.style.borderColor = isError ? '#ef4444' : '#3b82f6';
      b.style.color = isError ? '#fca5a5' : '#93c5fd';
      b.textContent = msg;
    }

    function highlightActiveKpi(st) {
      ['ALL', 'SITE_MORT', 'FACEBOOK_SEUL', 'SITE_FAIBLE', 'SANS_SITE'].forEach(k => {
        const el = document.getElementById('kpiCard_' + k);
        if (el) el.classList.toggle('active-kpi', k === st);
      });
    }

    function quickFilterStatus(st) {
      document.getElementById('statusFilter').value = st;
      highlightActiveKpi(st);
      render();
    }

    function resetFilters() {
      document.getElementById('searchInput').value = '';
      document.getElementById('cityFilter').value = 'ALL';
      document.getElementById('crmFilter').value = 'ALL';
      document.getElementById('statusFilter').value = 'ALL';
      document.getElementById('sectorFilter').value = 'ALL';
      document.getElementById('phoneFilter').value = 'ALL';
      highlightActiveKpi('ALL');
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
      let cityHtml = `<option value="ALL">📍 Toutes villes (${LEADS.length})</option>`;
      cities.forEach(c => {
        const cnt = LEADS.filter(b => b.city === c).length;
        cityHtml += `<option value="${c}">📍 ${c} (${cnt})</option>`;
      });
      cityEl.innerHTML = cityHtml;
      cityEl.value = cities.includes(curCity) ? curCity : 'ALL';

      const countAContacter = LEADS.filter(b => (b.crm_status || 'A_CONTACTER') === 'A_CONTACTER').length;
      const countContacte   = LEADS.filter(b => b.crm_status === 'CONTACTE').length;
      const countClient     = LEADS.filter(b => b.crm_status === 'CLIENT').length;
      const countIgnore     = LEADS.filter(b => b.crm_status === 'IGNORE').length;

      crmEl.innerHTML = `
        <option value="ALL">📋 Tout le CRM (${LEADS.length})</option>
        <option value="A_CONTACTER">⚪ À contacter (${countAContacter})</option>
        <option value="CONTACTE">🟡 Contacté (${countContacte})</option>
        <option value="CLIENT">🟢 RDV / Client (${countClient})</option>
        <option value="IGNORE">🔴 Ignoré (${countIgnore})</option>
      `;
      crmEl.value = ['ALL', 'A_CONTACTER', 'CONTACTE', 'CLIENT', 'IGNORE'].includes(curCrm) ? curCrm : 'ALL';

      const countStatus = (st) => LEADS.filter(b => b.status === st).length;
      const countHasFb = LEADS.filter(b => Boolean(b.facebook_url)).length;

      statusEl.innerHTML = `
        <option value="ALL">📊 Tous (${LEADS.length})</option>
        <option value="SITE_MORT">🔥 Cassés (${countStatus('SITE_MORT')})</option>
        <option value="FACEBOOK_SEUL">📱 FB Seul (${countStatus('FACEBOOK_SEUL')})</option>
        <option value="HAS_FACEBOOK">📘 Avec FB (${countHasFb})</option>
        <option value="SITE_FAIBLE">⚠️ Lents/Sans SSL (${countStatus('SITE_FAIBLE')})</option>
        <option value="SANS_SITE">🚀 Sans site (${countStatus('SANS_SITE')})</option>
        <option value="SITE_OK">✅ OK (${countStatus('SITE_OK')})</option>
      `;
      statusEl.value = curStatus;

      let sectorHtml = `<option value="ALL">🏢 Tous secteurs (${LEADS.length})</option>`;
      Object.keys(SECTOR_LABELS).forEach(k => {
        if (k === 'all') return;
        const c = LEADS.filter(b => b.sector_key === k).length;
        sectorHtml += `<option value="${k}">${SECTOR_LABELS[k]} (${c})</option>`;
      });
      sectorEl.innerHTML = sectorHtml;
      sectorEl.value = curSector;

      const cMob = LEADS.filter(b => b.phone_type === 'MOBILE_WHATSAPP').length;
      const cFix = LEADS.filter(b => b.phone_type === 'FIXE_ONATEL').length;
      const cNone = LEADS.filter(b => b.phone_type === 'AUCUN').length;

      phoneEl.innerHTML = `
        <option value="ALL">📞 Tous contacts (${LEADS.length})</option>
        <option value="MOBILE_WHATSAPP">💬 Mobile WhatsApp (${cMob})</option>
        <option value="FIXE_ONATEL">☎️ Fixe ONATEL (${cFix})</option>
        <option value="AUCUN">🌐 Web sans numéro (${cNone})</option>
      `;
      phoneEl.value = curPhone;
    }

    async function loadInitialLeads() {
      const localLeads = loadLocalBackup();
      try {
        const res = await fetch('/api/leads');
        const data = await res.json();
        const serverLeads = data.leads || [];
        LEADS = dedupeAndSortLeads([...serverLeads, ...localLeads]);
        saveLocalBackup();
      } catch (e) {
        LEADS = localLeads;
      }
      populateFilterDropdowns();
      setSalesOffer(CURRENT_OFFER);
      render();
    }

    async function clearAllLeads() {
      if (!confirm('Voulez-vous vraiment vider toute la liste actuelle ?')) return;
      LEADS = [];
      try {
        localStorage.removeItem(STORAGE_KEY);
      } catch (e) {}
      try {
        await fetch('/api/clear', { method: 'POST' });
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
      const headers = ['Nom', 'Ville', 'Quartier', 'Secteur', 'Telephone', 'Type_Tel', 'Statut_Digital', 'Statut_CRM', 'Score', 'Site_Web', 'Facebook', 'Google_Maps', 'Problemes'];
      const rows = LEADS.map(b => [
        b.name || '',
        b.city || '',
        b.neighborhood || '',
        b.sector || '',
        b.phone || '',
        b.phone_type || '',
        b.status || '',
        b.crm_status || 'A_CONTACTER',
        b.score || 0,
        b.website || '',
        b.facebook_url || '',
        b.maps_url || '',
        (b.issues || []).join(' | ')
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
      } finally {
        btn.disabled = false;
      }
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
          populateFilterDropdowns();
          render();
          const added = Math.max(0, LEADS.length - prevCount);
          showBanner(`✅ Scan terminé : ${data.leads.length} auditées (${added} nouvelles ajoutées, total ${LEADS.length}) !`);
        }
      } catch (err) {
        showBanner(`❌ Erreur : ${err}`, true);
      } finally {
        btn.disabled = false;
      }
    }

    function updateKPIs() {
      document.getElementById('kpiTotal').textContent = LEADS.length;
      document.getElementById('kpiDead').textContent = LEADS.filter(b => b.status === 'SITE_MORT').length;
      document.getElementById('kpiFb').textContent = LEADS.filter(b => b.status === 'FACEBOOK_SEUL').length;
      document.getElementById('kpiWeak').textContent = LEADS.filter(b => b.status === 'SITE_FAIBLE').length;
      document.getElementById('kpiNoSite').textContent = LEADS.filter(b => b.status === 'SANS_SITE').length;
    }

    function render() {
      updateKPIs();
      const q = document.getElementById('searchInput').value.toLowerCase().trim();
      const cf = document.getElementById('cityFilter').value || 'ALL';
      const crmf = document.getElementById('crmFilter').value || 'ALL';
      const st = document.getElementById('statusFilter').value || 'ALL';
      const sec = document.getElementById('sectorFilter').value || 'ALL';
      const pf = document.getElementById('phoneFilter').value || 'ALL';
      const container = document.getElementById('leadContainer');

      highlightActiveKpi(st);

      const filtered = LEADS.filter(item => {
        if (cf !== 'ALL' && item.city !== cf) {
          return false;
        }
        if (crmf !== 'ALL' && (item.crm_status || 'A_CONTACTER') !== crmf) {
          return false;
        }
        if (st === 'HAS_FACEBOOK') {
          if (!item.facebook_url) return false;
        } else if (st !== 'ALL' && item.status !== st) {
          return false;
        }
        if (sec !== 'ALL' && item.sector_key !== sec) {
          return false;
        }
        if (pf !== 'ALL' && item.phone_type !== pf) {
          return false;
        }
        if (q) {
          const hay = (
            item.name + ' ' +
            item.neighborhood + ' ' +
            item.sector + ' ' +
            item.phone + ' ' +
            (item.crm_status || 'A_CONTACTER') + ' ' +
            (item.website || '') + ' ' +
            (item.facebook_url || '') + ' ' +
            (item.issues || []).join(' ')
          ).toLowerCase();
          if (!hay.includes(q)) return false;
        }
        return true;
      });

      document.getElementById('filterCountText').textContent =
        `${filtered.length} sur ${LEADS.length} entreprises affichées`;

      if (filtered.length === 0) {
        container.innerHTML = `
          <div class="empty-box">
            <p style="font-size:0.95rem; font-weight:700; margin-bottom:6px;">Aucun résultat pour ce filtre.</p>
            <p style="font-size:0.8rem; margin-bottom:12px;">Réinitialise les filtres ou lance un scan sur ce secteur en haut.</p>
            <button class="btn btn-reset" style="max-width:220px;" onclick="resetFilters()">🔄 Réinitialiser</button>
          </div>
        `;
        return;
      }

      container.innerHTML = filtered.map((b) => {
        const scoreColor = b.score >= 85 ? '#ef4444' : (b.score >= 70 ? '#f59e0b' : '#10b981');
        const fullUrl = b.website ? (b.website.startsWith('http') ? b.website : 'http://' + b.website) : '';
        const domainText = b.website ? shortDomain(b.website) : 'Aucun site';
        const siteHtml = b.website
          ? `<a href="${fullUrl}" target="_blank">🌐 ${escapeHtml(domainText)}</a>`
          : '<span>🌐 Aucun site</span>';

        const speedTag = b.response_ms ? ` • ${b.response_ms}ms` : '';
        const phoneLabel = b.phone_type === 'MOBILE_WHATSAPP'
          ? '<span class="phone-badge ph-MOBILE_WHATSAPP">📱 Mobile</span>'
          : (b.phone_type === 'FIXE_ONATEL' ? '<span class="phone-badge ph-FIXE_ONATEL">☎️ Fixe</span>' : '');

        const pixelBadge = b.has_fb_pixel
          ? '<span class="status-pill tag-pixel">🎯 Pixel Meta</span>'
          : '';
        const gaBadge = b.has_google_analytics
          ? '<span class="status-pill tag-analytics">📊 Google Analytics</span>'
          : '';

        const currentPitch = generatePitch(b, CURRENT_OFFER);
        b.pitch = currentPitch;

        let dynamicWaUrl = '';
        if (b.phone_type === 'MOBILE_WHATSAPP' && b.phone && b.phone !== 'Non renseigné') {
          const digits = b.phone.replace(/[^0-9]/g, '');
          if (digits) {
            dynamicWaUrl = 'https://wa.me/' + digits + '?text=' + encodeURIComponent(currentPitch);
            b.whatsapp_url = dynamicWaUrl;
          }
        }

        const contactBtn = dynamicWaUrl
          ? `<a class="btn btn-wa" href="${dynamicWaUrl}" target="_blank">💬 WhatsApp</a>`
          : (b.tel_url ? `<a class="btn btn-call" href="${b.tel_url}">📞 Appeler</a>` : '');

        const fbBtn = b.facebook_url
          ? `<a class="btn btn-fb" href="${b.facebook_url}" target="_blank">📘 Facebook</a>`
          : '';

        const copyBtn = `<button class="btn btn-copy" onclick="copyPitchText(this, decodeURIComponent('${safeAttrEnc(currentPitch)}'))">📋 Copier Pitch</button>`;

        const mapsUrl = b.maps_url || `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(b.name + ' ' + (b.city && b.city !== 'Audit Direct' ? b.city : 'Burkina Faso'))}`;
        const mapsBtn = `<a class="btn btn-maps" href="${mapsUrl}" target="_blank">📍 Google Maps</a>`;

        const shortSector = SECTOR_LABELS[b.sector_key] || b.sector;

        const crmStatus = b.crm_status || 'A_CONTACTER';
        const cardClass = crmStatus === 'IGNORE' ? 'lead-card crm-state-IGNORE' : 'lead-card';
        const encName = safeAttrEnc(b.name || '');
        const encCity = safeAttrEnc(b.city || '');

        const crmBarHtml = `
          <div class="crm-bar" role="group" aria-label="Statut CRM">
            <button type="button" class="crm-pill crm-a-contacter ${crmStatus === 'A_CONTACTER' ? 'active' : ''}" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'A_CONTACTER', decodeURIComponent('${encCity}'))">⚪ À contacter</button>
            <button type="button" class="crm-pill crm-contacte ${crmStatus === 'CONTACTE' ? 'active' : ''}" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'CONTACTE', decodeURIComponent('${encCity}'))">🟡 Contacté</button>
            <button type="button" class="crm-pill crm-client ${crmStatus === 'CLIENT' ? 'active' : ''}" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'CLIENT', decodeURIComponent('${encCity}'))">🟢 RDV / Client</button>
            <button type="button" class="crm-pill crm-ignore ${crmStatus === 'IGNORE' ? 'active' : ''}" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'IGNORE', decodeURIComponent('${encCity}'))">🔴 Ignorer</button>
          </div>
        `;

        return `
          <div class="${cardClass}">
            <div>
              <div class="card-top">
                <div class="score-badge">
                  <span class="score-num" style="color:${scoreColor}">${b.score}</span>
                  <span class="score-lbl">Score</span>
                </div>
                <div class="title-wrap">
                  <span class="biz-name">${escapeHtml(b.name)}</span>
                  <div class="badges-row">
                    <span class="status-pill st-${b.status}">${b.status.replace('_', ' ')}${speedTag}</span>
                    ${phoneLabel}
                    ${pixelBadge}
                    ${gaBadge}
                  </div>
                </div>
              </div>

              ${crmBarHtml}

              <div class="meta-chips">
                <span class="meta-item"><a href="${mapsUrl}" target="_blank" title="Voir sur Google Maps">📍 ${escapeHtml(b.city)} (${escapeHtml(b.neighborhood)})</a></span>
                <span class="meta-item">${escapeHtml(shortSector)}</span>
                <span class="meta-item">📞 <strong>${escapeHtml(b.phone)}</strong></span>
                <span class="meta-item">${siteHtml}</span>
              </div>

              <div class="issues">
                ${(b.issues || []).map(iss => `<span class="issue-tag">⚠️ ${escapeHtml(iss)}</span>`).join('')}
              </div>

              <div class="pitch-box">
                <strong>Pitch (${escapeHtml(CURRENT_OFFER)}) :</strong> ${escapeHtml(currentPitch)}
              </div>
            </div>

            <div class="actions">
              ${contactBtn}
              ${mapsBtn}
              ${fbBtn}
              ${copyBtn}
            </div>
          </div>
        `;
      }).join('');
    }

    document.getElementById('searchInput').addEventListener('input', render);
    document.getElementById('cityFilter').addEventListener('change', render);
    document.getElementById('crmFilter').addEventListener('change', render);
    document.getElementById('statusFilter').addEventListener('change', render);
    document.getElementById('sectorFilter').addEventListener('change', render);
    document.getElementById('phoneFilter').addEventListener('change', render);
    loadInitialLeads();
  </script>
</body>
</html>
"""


class ProspectHandler(BaseHTTPRequestHandler):
    def _send_json(self, data: dict, status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, html_str: str) -> None:
        raw = html_str.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/leads":
            leads = load_saved_leads()
            self._send_json({"leads": leads})
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

        if parsed.path in ("/api/leads/status", "/api/leads/crm"):
            VALID_CRM_STATUSES = {"A_CONTACTER", "CONTACTE", "CLIENT", "IGNORE"}
            name = str(body.get("name", "") or "").strip()
            city = str(body.get("city", "") or "").strip()
            new_status = body.get("crm_status")

            if not name or not city:
                self._send_json({"error": "Nom et ville requis"}, status=400)
                return

            if not isinstance(new_status, str) or new_status not in VALID_CRM_STATUSES:
                self._send_json({"error": f"Statut CRM invalide: {new_status}"}, status=400)
                return

            with STATE_LOCK:
                current = load_saved_leads()
                norm_name = name.lower()
                norm_city = city.lower()
                matched = False
                for lead in current:
                    l_name = str(lead.get("name", "")).strip().lower()
                    l_city = str(lead.get("city", "")).strip().lower()
                    if l_name == norm_name and (l_city == norm_city or not l_city or not norm_city):
                        lead["crm_status"] = new_status
                        matched = True
                        break
                if not matched:
                    from urllib.parse import quote_plus
                    loc = city if city != "Audit Direct" else "Burkina Faso"
                    q = quote_plus(f"{name} {loc}".strip())
                    new_lead = {
                        "name": name,
                        "city": city,
                        "crm_status": new_status,
                        "has_fb_pixel": False,
                        "has_google_analytics": False,
                        "maps_url": f"https://www.google.com/maps/search/?api=1&query={q}",
                    }
                    current.append(new_lead)
                save_leads(current)

            self._send_json({"ok": True, "name": name, "city": city, "crm_status": new_status})
            return

        if parsed.path == "/api/clear":
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

            with STATE_LOCK:
                current = load_saved_leads()
                merged = merge_leads(current, [lead_dict])
                save_leads(merged)
            self._send_json({"lead": lead_dict})
            return

        if parsed.path == "/api/scan":
            city = body.get("city", "ouaga")
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
            with STATE_LOCK:
                merged_dicts = merge_leads(load_saved_leads(), incoming_dicts)
                save_leads(merged_dicts)
            self._send_json({"leads": merged_dicts})
            return

        self._send_json({"error": "Route inconnue"}, status=404)


def _seed_initial_leads_async() -> None:
    if not STATE_FILE.exists():
        try:
            raw_leads = fetch_osm_businesses("ouaga", "all", limit=75)
            with concurrent.futures.ThreadPoolExecutor(max_workers=14) as pool:
                audited = list(pool.map(audit_single_business, raw_leads))
            audited.sort(
                key=lambda b: (b.lead_score, is_likely_mobile_bf(b.phone_clean), bool(b.phone_clean)),
                reverse=True,
            )
            save_leads([lead_to_dict(b) for b in audited])
        except Exception as exc:
            print(f"[!] Erreur init arrière-plan: {exc}", flush=True)


def run_server(port: int | None = None) -> None:
    import os
    actual_port = int(port or os.environ.get("PORT", 8080))
    threading.Thread(target=_seed_initial_leads_async, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", actual_port), ProspectHandler)
    print(f"[OK] Serveur ProspectBF v2.6 Mobile-Responsive actif sur port {actual_port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    run_server()

