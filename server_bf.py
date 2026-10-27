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
    classify_business_sector,
    export_csv,
    fetch_osm_businesses,
    is_likely_mobile_bf,
    normalize_bf_phone,
)

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "leads_state.json"


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
                        crm_st = lead["crm_status"]
                        lead["in_pipeline"] = bool(lead.get("in_pipeline", False) or (crm_st in PIPELINE_ACTIVE_STATUSES))
                        lead["rdv_done"] = bool(lead.get("rdv_done", False))
                        lead["deal_amount"] = _safe_int_amount(lead.get("deal_amount", 0))
                        lead["notes"] = str(lead.get("notes") or "")
                        lead["updated_at"] = str(lead.get("updated_at") or "")
                        lead["email"] = str(lead.get("email") or "")
                        lead["has_fb_pixel"] = bool(lead.get("has_fb_pixel", False))
                        lead["has_google_analytics"] = bool(lead.get("has_google_analytics", False))
                        skey = str(lead.get("sector_key") or "").strip().lower()
                        if not skey or skey not in SECTOR_CATEGORIES or skey == "all":
                            skey, slbl = classify_business_sector({"name": lead.get("name", ""), "description": lead.get("sector", "")})
                            lead["sector_key"] = skey
                            if not lead.get("sector"):
                                lead["sector"] = slbl
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
      --bg: #070b12;
      --bg-mesh: radial-gradient(circle at 12% 0%, rgba(59, 130, 246, 0.12), transparent 38%),
                 radial-gradient(circle at 88% 8%, rgba(16, 185, 129, 0.09), transparent 36%);
      --surface-0: #0c121e;
      --card: #111927;
      --card-2: #172235;
      --card-hover: #1b283e;
      --border: #243249;
      --border-light: #334766;
      --text: #f8fafc;
      --text-secondary: #cbd5e1;
      --muted: #94a3b8;
      --red: #ef4444;
      --amber: #f59e0b;
      --green: #10b981;
      --blue: #3b82f6;
      --indigo: #6366f1;
      --purple: #a855f7;
      --teal: #14b8a6;
      --shadow-sm: 0 2px 8px rgba(0, 0, 0, 0.28);
      --shadow-md: 0 8px 24px rgba(0, 0, 0, 0.42);
      --shadow-lg: 0 16px 40px rgba(0, 0, 0, 0.58);
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
      background: rgba(12, 18, 30, 0.88);
      backdrop-filter: blur(14px);
      -webkit-backdrop-filter: blur(14px);
      border-bottom: 1px solid var(--border);
      padding: 10px 14px;
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.35);
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
      background: linear-gradient(135deg, #3b82f6 0%, #10b981 100%);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 1.15rem;
      font-weight: 900;
      color: #fff;
      box-shadow: 0 2px 10px rgba(59, 130, 246, 0.4);
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
      color: #fff;
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
      box-shadow: 0 1px 4px rgba(0,0,0,0.3);
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
      background: #080d16;
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
      color: #fff;
      background: rgba(255, 255, 255, 0.05);
    }

    .nav-pill.active {
      background: var(--card-2);
      color: #fff;
      border-color: var(--blue);
      box-shadow: 0 2px 8px rgba(59, 130, 246, 0.22);
    }

    .nav-pill-count {
      background: rgba(59, 130, 246, 0.22);
      color: #93c5fd;
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
      color: var(--text-secondary);
    }

    .section-step-badge {
      background: rgba(59, 130, 246, 0.18);
      border: 1px solid rgba(59, 130, 246, 0.45);
      color: #93c5fd;
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
      background: var(--surface-0);
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
      background: rgba(255, 255, 255, 0.04);
    }

    .tab-btn.active {
      background: linear-gradient(180deg, #1e2b42 0%, #162033 100%);
      color: #fff;
      border: 1px solid var(--border-light);
      box-shadow: 0 2px 10px rgba(0, 0, 0, 0.35);
    }

    .panel {
      display: none;
      background: linear-gradient(180deg, var(--card) 0%, var(--surface-0) 100%);
      border: 1px solid var(--border-light);
      border-radius: var(--radius-md);
      padding: 16px;
      margin-bottom: 16px;
      box-shadow: var(--shadow-md);
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
      color: #f8fafc;
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
      background: #080d16;
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
      box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.2);
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
      filter: brightness(1.08);
    }

    .btn:active:not(:disabled) {
      transform: translateY(0);
    }

    .btn-primary {
      background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%);
      color: #fff;
      box-shadow: 0 4px 12px rgba(37, 99, 235, 0.35);
    }
    .btn-amber {
      background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%);
      color: #070b12;
      box-shadow: 0 4px 12px rgba(245, 158, 11, 0.3);
    }
    .btn-reset {
      background: #162032;
      color: #e2e8f0;
      border: 1px solid var(--border-light);
    }
    .btn-reset:hover {
      background: #1e2c44;
      border-color: #475569;
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
      background: linear-gradient(180deg, var(--card) 0%, var(--surface-0) 100%);
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
      border-color: var(--border-light);
      background: var(--card-2);
    }

    .kpi.active-kpi {
      border-color: var(--blue);
      background: linear-gradient(180deg, rgba(59, 130, 246, 0.16) 0%, var(--card-2) 100%);
      box-shadow: 0 0 0 1px var(--blue), 0 6px 20px rgba(59, 130, 246, 0.22);
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
    }

    .kpi-tag {
      font-size: 0.65rem;
      font-weight: 700;
      padding: 2px 6px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.06);
      color: var(--muted);
    }

    .kpi-label {
      color: var(--text-secondary);
      font-size: 0.78rem;
      font-weight: 600;
      margin-top: 6px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    /* Responsive Filter Panel */
    .filters-box {
      background: var(--card);
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
        grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      }
    }

    .filter-summary {
      font-size: 0.82rem;
      color: var(--text-secondary);
      margin-bottom: 14px;
      padding: 10px 14px;
      background: var(--surface-0);
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
      background: linear-gradient(180deg, var(--card) 0%, #0d1422 100%);
      border: 1px solid var(--border);
      border-left: 4px solid var(--blue);
      border-radius: var(--radius-md);
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 14px;
      width: 100%;
      overflow: hidden;
      box-shadow: var(--shadow-sm);
      transition: border-color 0.18s ease, box-shadow 0.18s ease, transform 0.18s ease;
    }

    .lead-card:hover {
      border-color: var(--border-light);
      box-shadow: var(--shadow-md);
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
      background: #080d16;
      border: 2px solid var(--border-light);
      border-radius: 12px;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      transition: transform 0.15s ease, border-color 0.15s ease;
      box-shadow: inset 0 2px 6px rgba(0,0,0,0.5);
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
      color: #fff;
      word-break: break-word;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: color 0.15s ease;
    }

    .biz-name:hover {
      color: #60a5fa;
      text-decoration: underline;
    }

    .btn-open-detail {
      background: rgba(59, 130, 246, 0.14);
      border: 1px solid rgba(59, 130, 246, 0.4);
      color: #93c5fd;
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

    .st-SITE_MORT { background: rgba(239,68,68,0.18); color: #fca5a5; border: 1px solid rgba(239,68,68,0.6); }
    .st-SITE_FAIBLE { background: rgba(245,158,11,0.18); color: #fcd34d; border: 1px solid rgba(245,158,11,0.6); }
    .st-FACEBOOK_SEUL { background: rgba(168,85,247,0.18); color: #d8b4fe; border: 1px solid rgba(168,85,247,0.6); }
    .st-SANS_SITE { background: rgba(59,130,246,0.18); color: #93c5fd; border: 1px solid rgba(59,130,246,0.6); }
    .st-SITE_OK { background: rgba(16,185,129,0.18); color: #6ee7b7; border: 1px solid rgba(16,185,129,0.6); }

    .phone-badge {
      font-size: 0.7rem;
      padding: 3px 8px;
      border-radius: 6px;
      font-weight: 700;
    }
    .ph-MOBILE_WHATSAPP { background: rgba(16,185,129,0.18); color: #6ee7b7; border: 1px solid rgba(16,185,129,0.35); }
    .ph-FIXE_ONATEL { background: rgba(148,163,184,0.16); color: #cbd5e1; border: 1px solid rgba(148,163,184,0.3); }

    /* Distinct Info Chips Bar inside Lead Card */
    .meta-chips {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(175px, 1fr));
      gap: 8px;
      font-size: 0.8rem;
      color: var(--text-secondary);
      margin-top: 12px;
      background: rgba(8, 13, 22, 0.65);
      border: 1px solid var(--border);
      padding: 10px 12px;
      border-radius: 10px;
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
      color: #60a5fa;
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
      margin-top: 10px;
    }

    .issue-tag {
      background: rgba(239, 68, 68, 0.1);
      border: 1px solid rgba(239, 68, 68, 0.28);
      color: #fecaca;
      font-size: 0.73rem;
      font-weight: 600;
      padding: 4px 9px;
      border-radius: 6px;
      line-height: 1.3;
    }

    .pitch-box {
      background: #080d16;
      border: 1px solid var(--border);
      border-left: 3px solid var(--amber);
      padding: 10px 12px;
      font-size: 0.8rem;
      color: #e2e8f0;
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
      box-shadow: 0 3px 10px rgba(16, 185, 129, 0.25);
    }
    .btn-fb {
      background: linear-gradient(135deg, #1877f2 0%, #1d4ed8 100%);
      color: #ffffff;
    }
    .btn-call {
      background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%);
      color: #ffffff;
    }
    .btn-maps {
      background: rgba(20, 184, 166, 0.16);
      color: #5eead4;
      border: 1px solid rgba(20, 184, 166, 0.5);
    }
    .btn-maps:hover {
      background: #0d9488;
      color: #fff;
    }
    .btn-copy {
      background: #162032;
      color: var(--text);
      border: 1px solid var(--border-light);
    }

    .empty-box {
      text-align: center;
      padding: 36px 18px;
      background: var(--card);
      border: 1px dashed var(--border-light);
      border-radius: var(--radius-md);
      color: var(--muted);
    }

    #statusBanner {
      display: none;
      margin-bottom: 14px;
      padding: 12px 16px;
      border-radius: 10px;
      background: rgba(30, 41, 59, 0.9);
      border: 1px solid var(--blue);
      color: #93c5fd;
      font-size: 0.84rem;
      font-weight: 700;
      box-shadow: var(--shadow-sm);
    }

    /* Offer Selector Card (R3) */
    .offer-selector-card {
      background: linear-gradient(180deg, var(--card) 0%, var(--surface-0) 100%);
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
      color: var(--amber);
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
      background: #080d16;
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
      border-color: var(--border-light);
      background: var(--card-2);
      transform: translateY(-1px);
    }

    .offer-card-btn.active {
      background: linear-gradient(135deg, rgba(59, 130, 246, 0.2) 0%, rgba(16, 185, 129, 0.08) 100%);
      border-color: var(--blue);
      box-shadow: 0 0 0 1px var(--blue), 0 6px 16px rgba(59, 130, 246, 0.2);
    }

    .offer-icon {
      font-size: 1.35rem;
      width: 38px;
      height: 38px;
      border-radius: 9px;
      background: rgba(255, 255, 255, 0.05);
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
      color: #fff;
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
      background: #080d16;
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
      background: rgba(255, 255, 255, 0.06);
      color: #fff;
    }

    .crm-pill.crm-a-contacter.active {
      background: rgba(59, 130, 246, 0.22);
      border-color: var(--blue);
      color: #93c5fd;
      box-shadow: 0 0 8px rgba(59, 130, 246, 0.25);
    }

    .crm-pill.crm-contacte.active {
      background: rgba(245, 158, 11, 0.22);
      border-color: var(--amber);
      color: #fde68a;
      box-shadow: 0 0 8px rgba(245, 158, 11, 0.25);
    }

    .crm-pill.crm-client.active {
      background: rgba(168, 85, 247, 0.24);
      border-color: var(--purple);
      color: #e9d5ff;
      box-shadow: 0 0 10px rgba(168, 85, 247, 0.3);
    }

    .crm-pill.crm-livre.active {
      background: rgba(20, 184, 166, 0.24);
      border-color: var(--teal);
      color: #99f6e4;
      box-shadow: 0 0 10px rgba(20, 184, 166, 0.3);
    }

    .crm-pill.crm-paye.active {
      background: rgba(16, 185, 129, 0.28);
      border-color: var(--green);
      color: #6ee7b7;
      box-shadow: 0 0 12px rgba(16, 185, 129, 0.35);
    }

    .crm-pill.crm-ignore.active {
      background: rgba(239, 68, 68, 0.2);
      border-color: var(--red);
      color: #fca5a5;
    }

    .lead-card.crm-state-IGNORE,
    .lead-card.crm-state-ECHEC {
      opacity: 0.6;
      border-color: #334155;
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
      background: rgba(15, 23, 42, 0.75);
      border: 1px dashed rgba(59, 130, 246, 0.45);
    }

    .btn-add-pipeline {
      background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%);
      color: #ffffff;
      border: 1px solid #60a5fa;
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
      box-shadow: 0 4px 14px rgba(37, 99, 235, 0.4);
      transition: all 0.16s ease;
      flex: 1 1 220px;
    }

    .btn-add-pipeline:hover {
      transform: translateY(-1px);
      filter: brightness(1.1);
      box-shadow: 0 6px 18px rgba(37, 99, 235, 0.55);
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
      border: 1px solid var(--border-light);
    }

    .mem-A_CONTACTER {
      background: rgba(59, 130, 246, 0.16);
      border-color: #3b82f6;
      color: #bfdbfe;
    }
    .mem-CONTACTE {
      background: rgba(245, 158, 11, 0.16);
      border-color: #f59e0b;
      color: #fde68a;
    }
    .mem-RDV, .mem-CLIENT {
      background: rgba(168, 85, 247, 0.18);
      border-color: #a855f7;
      color: #e9d5ff;
    }
    .mem-LIVRE {
      background: rgba(20, 184, 166, 0.18);
      border-color: #14b8a6;
      color: #99f6e4;
    }
    .mem-PAYE {
      background: rgba(16, 185, 129, 0.22);
      border-color: #10b981;
      color: #6ee7b7;
    }
    .mem-ECHEC, .mem-IGNORE {
      background: rgba(239, 68, 68, 0.14);
      border-color: rgba(239, 68, 68, 0.55);
      color: #fca5a5;
    }

    /* Progressive Pipeline Stage Decision Box inside "Mon Pipeline Commercial" */
    .pipeline-stage-box {
      margin-top: 12px;
      padding: 12px 14px;
      border-radius: 10px;
      background: rgba(8, 13, 22, 0.88);
      border: 1px solid var(--border-light);
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
      color: var(--text-secondary);
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
      filter: brightness(1.08);
    }

    .btn-stage-contacte {
      background: rgba(245, 158, 11, 0.2);
      border-color: var(--amber);
      color: #fde68a;
    }
    .btn-stage-rdv {
      background: linear-gradient(135deg, #9333ea 0%, #7e22ce 100%);
      border-color: #c084fc;
      color: #fff;
    }
    .btn-stage-rdv-check {
      background: rgba(168, 85, 247, 0.16);
      border: 2px solid #a855f7;
      color: #e9d5ff;
    }
    .btn-stage-rdv-check.done {
      background: rgba(16, 185, 129, 0.24);
      border-color: #10b981;
      color: #6ee7b7;
    }
    .btn-stage-livre {
      background: linear-gradient(135deg, #0d9488 0%, #0f766e 100%);
      border-color: #5eead4;
      color: #fff;
    }
    .btn-stage-paye {
      background: linear-gradient(135deg, #10b981 0%, #059669 100%);
      border-color: #6ee7b7;
      color: #fff;
      box-shadow: 0 4px 12px rgba(16, 185, 129, 0.35);
    }
    .btn-stage-echec {
      background: rgba(239, 68, 68, 0.15);
      border-color: rgba(239, 68, 68, 0.5);
      color: #fca5a5;
    }

    /* Inline Deal Amount (FCFA) & Notes Bar on Pipeline Cards */
    .pipeline-meta-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 8px;
      margin-top: 8px;
      padding-top: 8px;
      border-top: 1px solid rgba(51, 71, 102, 0.45);
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
      background: #050911;
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
      background: linear-gradient(135deg, #111927 0%, #0b111d 100%);
      border: 1px solid var(--border-light);
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
      color: #fff;
    }

    .fin-sub {
      font-size: 0.74rem;
      color: var(--text-secondary);
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
      background: linear-gradient(180deg, #101827 0%, #0b101b 100%);
      border: 1px solid var(--border);
      border-top: 3px solid #64748b;
      border-radius: var(--radius-md);
      padding: 10px 12px;
      cursor: pointer;
      user-select: none;
      transition: all 0.16s ease;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      min-height: 68px;
    }

    .crm-stage-card:hover {
      transform: translateY(-2px);
      border-color: var(--border-light);
      background: var(--card-2);
    }

    .crm-stage-card.active-crm-stage {
      border-color: var(--blue);
      background: linear-gradient(180deg, rgba(59, 130, 246, 0.16) 0%, var(--card-2) 100%);
      box-shadow: 0 0 0 1px var(--blue), 0 6px 18px rgba(59, 130, 246, 0.2);
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
      color: #cbd5e1;
      cursor: pointer;
      user-select: none;
      padding: 6px 12px;
      border-radius: 8px;
      background: rgba(59, 130, 246, 0.1);
      border: 1px solid rgba(59, 130, 246, 0.35);
      transition: all 0.15s ease;
    }

    .toggle-known-label:hover {
      background: rgba(59, 130, 246, 0.18);
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
      background: rgba(17, 25, 39, 0.85);
      border: 1px solid var(--border);
      border-left: 4px solid var(--blue);
      border-radius: 10px;
      padding: 10px 14px;
      margin-bottom: 16px;
      font-size: 0.8rem;
      color: var(--text-secondary);
    }

    /* Tracking Badges (R4) */
    .tag-pixel { background: rgba(59, 130, 246, 0.2); color: #93c5fd; border: 1px solid #3b82f6; }
    .tag-analytics { background: rgba(245, 158, 11, 0.2); color: #fde68a; border: 1px solid #f59e0b; }

    /* Interactive Slide-Over Lead Detail Modal ("Rentrer dans la fiche d'une entreprise") */
    .modal-backdrop {
      position: fixed;
      inset: 0;
      z-index: 250;
      background: rgba(3, 7, 15, 0.78);
      backdrop-filter: blur(6px);
      -webkit-backdrop-filter: blur(6px);
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
      background: var(--surface-0);
      border-left: 1px solid var(--border-light);
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
      background: #1e293b;
      border: 1px solid var(--border-light);
      color: #fff;
      width: 42px;
      height: 42px;
      border-radius: 9px;
      font-size: 1rem;
      font-weight: 800;
      cursor: pointer;
      flex-shrink: 0;
    }

    .drawer-section {
      background: var(--card);
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
      color: var(--muted);
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
      background: #080d16;
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
      color: #fff;
      margin-top: 2px;
      display: block;
      overflow: hidden;
      text-overflow: ellipsis;
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
          📋 Mon Pipeline Commercial <span class="nav-pill-count" id="navCountCrm" style="background:rgba(16,185,129,0.25); color:#6ee7b7;">0</span>
        </button>
        <button type="button" id="navView_offers" class="nav-pill" onclick="switchAppView('offers')">
          🎯 Offres & Pitchs
        </button>
        <button type="button" id="navView_all" class="nav-pill" onclick="switchAppView('all')">
          🏠 Vue Globale
        </button>
      </nav>
    </div>
  </header>

  <div class="container">
    <div id="workspaceContextBar" class="workspace-context-bar">
      <span id="workspaceContextTitle"><strong>🔍 Espace Actif : Radar de Scan & Triage</strong> — Scannez une ville, repérez les opportunités et cliquez sur <strong>« ➕ Mettre dans À contacter »</strong> pour les envoyer dans votre Pipeline Commercial</span>
      <button type="button" id="btnQuickGoPipeline" class="btn btn-reset" style="width:auto; min-height:34px; padding:5px 12px; font-size:0.76rem; border-color:var(--blue); color:#93c5fd;" onclick="switchAppView('crm')">📂 Ouvrir Mon Pipeline Commercial →</button>
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
          <div class="fin-amount" id="finPendingTotal" style="color: #5eead4;">0 FCFA</div>
          <span class="fin-sub" id="finPendingCount">0 projet en cours / livré</span>
        </div>
        <div class="fin-card fin-active">
          <span class="fin-title">📥 Prospects dans Mon Pipeline</span>
          <div class="fin-amount" id="finActiveCount" style="color: #93c5fd;">0</div>
          <span class="fin-sub" id="finTodoCount">Dont 0 dans « À contacter »</span>
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
            <div class="kpi-val" id="crmCount_A_CONTACTER" style="font-size:1.25rem; color:#93c5fd;">0</div>
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
            <div class="kpi-val" id="crmCount_CLIENT" style="font-size:1.25rem; color:#d8b4fe;">0</div>
            <span class="kpi-tag">Étape 3</span>
          </div>
          <div class="kpi-label">📅 Rendez-vous</div>
        </div>
        <div id="crmStage_LIVRE" class="crm-stage-card" style="border-top-color: var(--teal);" onclick="quickFilterCrm('LIVRE')">
          <div class="kpi-top-row">
            <div class="kpi-val" id="crmCount_LIVRE" style="font-size:1.25rem; color:#5eead4;">0</div>
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

      <!-- RESPONSIVE FILTERS -->
      <div class="filters-box">
        <div class="search-row">
          <input type="search" id="searchInput" placeholder="🔎 Rechercher par nom d'entreprise, quartier, ville, note, problème technique..." />
          <button class="btn btn-reset" onclick="resetFilters()">🔄 Réinitialiser</button>
        </div>
        <div class="selects-grid">
          <select id="cityFilter"></select>
          <select id="crmFilter"></select>
          <select id="statusFilter"></select>
          <select id="sectorFilter"></select>
          <select id="phoneFilter"></select>
        </div>
        <div id="radarMemoryToggleBar" class="radar-memory-toggle-bar">
          <label class="toggle-known-label" for="hideKnownLeadsToggle">
            <input type="checkbox" id="hideKnownLeadsToggle" onchange="render()" />
            <span>🙈 Masquer ceux déjà dans mon Pipeline / Échecs (Ne voir que les nouveaux)</span>
          </label>
          <span id="radarKnownSummaryText" style="font-size:0.76rem; color:var(--muted);"></span>
        </div>
      </div>

      <div class="filter-summary">
        <span id="filterCountText" style="font-weight:700;">Chargement...</span>
        <div style="display:flex; gap:8px; flex-wrap:wrap;">
          <button class="btn btn-reset" style="width:auto; min-height:36px; padding:6px 14px; font-size:0.78rem;" onclick="exportLeadsCsv()">📥 Exporter CSV</button>
          <button class="btn btn-reset" style="width:auto; min-height:36px; padding:6px 14px; font-size:0.78rem; color:#fca5a5; border-color:rgba(239,68,68,0.4);" onclick="clearAllLeads()">🗑️ Vider la liste</button>
        </div>
      </div>

      <div id="leadContainer" class="lead-list"></div>
    </section>
  </div>

  <!-- SLIDE-OVER LEAD DETAIL MODAL ("Rentrer dans la fiche complète d'une entreprise") -->
  <div id="leadDetailModal" class="modal-backdrop" onclick="if(event.target===this) closeLeadDetail()">
    <div class="detail-drawer" role="dialog" aria-modal="true" aria-labelledby="drawerBizName">
      <div class="drawer-header">
        <div>
          <span id="drawerSectorBadge" style="font-size:0.72rem; color:var(--blue); font-weight:800; text-transform:uppercase;">SECTEUR</span>
          <h2 id="drawerBizName" style="font-size:1.25rem; font-weight:900; color:#fff; margin-top:2px;">Entreprise</h2>
          <p id="drawerLocation" style="font-size:0.8rem; color:var(--muted); margin-top:2px;">📍 Localisation</p>
        </div>
        <button type="button" class="drawer-close-btn" onclick="closeLeadDetail()" title="Fermer">✕</button>
      </div>
      <div id="drawerBodyContent"></div>
    </div>
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
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
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
    });
    render();
    if (ACTIVE_DETAIL_LEAD) renderLeadDetailDrawer();
  }

  function buildWhatsAppUrl(phone, pitch) {
    if (!phone || phone === 'Non renseigné') return '';
    const digits = String(phone).replace(/[^0-9]/g, '');
    if (!digits) return '';
    return 'https://wa.me/' + digits + '?text=' + encodeURIComponent(pitch);
  }

  function copyPitchText(btn, text) {
    const orig = (btn && btn.textContent && btn.textContent !== '✅ Copié !') ? btn.textContent : '📋 Copier Pitch';
    if (typeof navigator !== 'undefined' && navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
    if (btn) {
      btn.textContent = '✅ Copié !';
      setTimeout(() => { btn.textContent = orig; }, 1500);
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
    const match = findLeadByNameCity(name, city);
    if (!match) return;
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

  function saveLocalBackup() {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(LEADS)); } catch (e) {}
  }

  function loadLocalBackup() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
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

  function highlightActiveCrmStage(crmSt) {
    const norm = crmSt === 'RDV' ? 'CLIENT' : (crmSt === 'ECHEC' ? 'IGNORE' : crmSt);
    ['ALL', 'A_CONTACTER', 'CONTACTE', 'CLIENT', 'LIVRE', 'PAYE', 'IGNORE'].forEach(k => {
      const el = document.getElementById('crmStage_' + k);
      if (el) el.classList.toggle('active-crm-stage', k === norm);
    });
  }

  function quickFilterStatus(st) {
    document.getElementById('statusFilter').value = st;
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
    document.getElementById('statusFilter').value = 'ALL';
    document.getElementById('sectorFilter').value = 'ALL';
    document.getElementById('phoneFilter').value = 'ALL';
    const hideToggle = document.getElementById('hideKnownLeadsToggle');
    if (hideToggle) hideToggle.checked = false;
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
    let cityHtml = `<option value="ALL">📍 Toutes villes (${LEADS.length})</option>`;
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

    crmEl.innerHTML = `<option value="ALL">📋 Tous statuts CRM (${CURRENT_VIEW === 'crm' ? pipeLeads.length : LEADS.length})</option>` +
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

    statusEl.innerHTML = `<option value="ALL">📊 Tous (${LEADS.length})</option><option value="SITE_MORT">🔥 Cassés (${countStatus('SITE_MORT')})</option><option value="FACEBOOK_SEUL">📱 FB Seul (${countStatus('FACEBOOK_SEUL')})</option><option value="HAS_FACEBOOK">📘 Avec FB (${countHasFb})</option><option value="SITE_FAIBLE">⚠️ Lents/Sans SSL (${countStatus('SITE_FAIBLE')})</option><option value="SANS_SITE">🚀 Sans site (${countStatus('SANS_SITE')})</option><option value="SITE_OK">✅ OK (${countStatus('SITE_OK')})</option>`;
    statusEl.value = curStatus;

    let sectorHtml = `<option value="ALL">🏢 Tous secteurs (${LEADS.length})</option>`;
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
    const localLeads = loadLocalBackup();
    try {
      const res = await fetch('/api/leads');
      const data = await res.json();
      const serverLeads = data.leads || [];
      LEADS = dedupeAndSortLeads([...serverLeads, ...localLeads]);
      saveLocalBackup();
    } catch (e) { LEADS = localLeads; }
    populateFilterDropdowns();
    setSalesOffer(CURRENT_OFFER);
    switchAppView('scanner');
  }

  async function clearAllLeads() {
    if (!confirm('Voulez-vous vraiment vider toute la liste actuelle ?')) return;
    LEADS = [];
    try { localStorage.removeItem(STORAGE_KEY); } catch (e) {}
    try { await fetch('/api/clear', { method: 'POST' }); } catch (e) {}
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
      if (secOff) secOff.style.display = 'block';
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
      if (ctxTitle) ctxTitle.innerHTML = "<strong>🎯 Espace Actif : Stratégie d'Offres & Pitchs</strong> — Adaptez automatiquement vos messages WhatsApp (Site Web, Facebook Ads, Google Maps).";
    } else if (view === 'crm') {
      if (secAcq) secAcq.style.display = 'none';
      if (secOff) secOff.style.display = 'block';
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

    const scoreColor = b.score >= 85 ? '#ef4444' : (b.score >= 70 ? '#f59e0b' : '#10b981');
    const fullUrl = b.website ? (b.website.startsWith('http') ? b.website : 'http://' + b.website) : '';
    const mapsUrl = b.maps_url || `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent((b.name || '') + ' ' + (b.city || 'Burkina Faso'))}`;
    const currentPitch = generatePitch(b, CURRENT_OFFER);
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

    const bodyEl = document.getElementById('drawerBodyContent');
    bodyEl.innerHTML = `<div class="drawer-section"><div class="drawer-section-title"><span>📊 Diagnostic Technique Complet</span><span class="status-pill st-${b.status}">${(b.status || '').replace('_', ' ')}</span></div>` +
      `<div class="diag-grid">` +
      `<div class="diag-item"><span class="diag-lbl">Score d'Opportunité</span><span class="diag-val" style="color:${scoreColor}; font-size:1.1rem;">${b.score || 0} / 100</span></div>` +
      `<div class="diag-item"><span class="diag-lbl">Temps de Réponse HTTP</span><span class="diag-val">${b.response_ms ? b.response_ms + ' ms' : 'Non mesuré / Sans site'}</span></div>` +
      `<div class="diag-item"><span class="diag-lbl">Pixel Meta (Facebook Ads)</span><span class="diag-val" style="color:${b.has_fb_pixel ? '#6ee7b7' : '#fca5a5'}">${b.has_fb_pixel ? '✅ Détecté' : '❌ Absent'}</span></div>` +
      `<div class="diag-item"><span class="diag-lbl">Google Analytics / Tag</span><span class="diag-val" style="color:${b.has_google_analytics ? '#6ee7b7' : '#fca5a5'}">${b.has_google_analytics ? '✅ Détecté' : '❌ Absent'}</span></div>` +
      `<div class="diag-item"><span class="diag-lbl">Téléphone & Email</span><span class="diag-val">📞 ${escapeHtml(b.phone || 'Non renseigné')}${b.email ? ' • ✉️ ' + escapeHtml(b.email) : ''}</span></div>` +
      `<div class="diag-item"><span class="diag-lbl">Site Web Officiel</span><span class="diag-val">${fullUrl ? `<a href="${fullUrl}" target="_blank" style="color:#60a5fa;">🌐 ${escapeHtml(shortDomain(b.website))}</a>` : '❌ Aucun site web'}</span></div>` +
      `</div><div class="issues" style="margin-top:12px;">${(b.issues || []).map(iss => `<span class="issue-tag">⚠️ ${escapeHtml(iss)}</span>`).join('') || '<span style="font-size:0.78rem; color:var(--muted);">Aucun problème critique signalé.</span>'}</div></div>` +
      `<div class="drawer-section"><div class="drawer-section-title"><span>📋 Étape Pipeline Commercial (6 Étapes)</span><span style="font-size:0.7rem; color:#93c5fd;">${inPipe ? '📌 Dans Mon Pipeline (' + formatRelativeTime(b.updated_at) + ')' : '🔍 Non classé'}</span></div>` +
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
      `<div class="meta-input-group"><label>💵 Montant du Deal (FCFA)</label><input type="number" min="0" step="5000" value="${b.deal_amount || ''}" placeholder="Ex: 75000" onchange="updateLeadMeta(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'), {deal_amount: this.value})" /></div>` +
      `<div class="meta-input-group"><label>📝 Notes & Suivi client</label><input type="text" value="${escapeHtml(b.notes || '')}" placeholder="Ex: Rappeler jeudi à 15h, intéressé par pack Web..." onchange="updateLeadMeta(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'), {notes: this.value})" /></div>` +
      `</div></div>` +
      `<div class="drawer-section"><div class="drawer-section-title"><span>💬 Studio Pitch Commercial (${escapeHtml(CURRENT_OFFER)})</span></div>` +
      `<div style="display:flex; gap:6px; margin-bottom:10px;">` +
      `<button type="button" class="btn btn-reset" style="font-size:0.74rem; padding:6px 8px; min-height:36px; ${CURRENT_OFFER==='WEB'?'border-color:var(--blue); color:#fff;':''}" onclick="setSalesOffer('WEB')">🌐 Offre Web</button>` +
      `<button type="button" class="btn btn-reset" style="font-size:0.74rem; padding:6px 8px; min-height:36px; ${CURRENT_OFFER==='ADS'?'border-color:var(--blue); color:#fff;':''}" onclick="setSalesOffer('ADS')">📱 Offre Ads</button>` +
      `<button type="button" class="btn btn-reset" style="font-size:0.74rem; padding:6px 8px; min-height:36px; ${CURRENT_OFFER==='MAPS'?'border-color:var(--blue); color:#fff;':''}" onclick="setSalesOffer('MAPS')">📍 Offre Maps</button>` +
      `</div>` +
      `<textarea id="drawerCustomPitch" rows="5" style="width:100%; font-size:0.84rem; line-height:1.45; margin-bottom:10px;" oninput="const waEl=document.getElementById('drawerWaBtn'); if(waEl) waEl.href=buildWhatsAppUrl(decodeURIComponent('${encPhone}'), this.value);">${escapeHtml(currentPitch)}</textarea>` +
      `<div style="display:grid; grid-template-columns: 1fr 1fr; gap:8px;">${contactActionHtml}<button type="button" class="btn btn-copy" onclick="copyPitchText(this, document.getElementById('drawerCustomPitch').value)">📋 Copier le message</button><a class="btn btn-maps" href="${mapsUrl}" target="_blank">📍 Ouvrir Google Maps</a>${b.facebook_url ? `<a class="btn btn-fb" href="${b.facebook_url}" target="_blank">📘 Page Facebook</a>` : ''}</div></div>`;
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
      stageHeader = `<span style="color:#6ee7b7;">🎉 Étape 5 : Affaire conclue, livrée & Payée (${escapeHtml(relTime)}) !</span>`;
      stageBtns = `<span class="status-pill st-SITE_OK" style="padding:8px 14px; font-size:0.82rem;">💰 Encaissé : ${formatFcfa(b.deal_amount || 0)}</span>`;
    } else {
      stageHeader = `<span style="color:#fca5a5;">❌ Étape 6 : Échec / Sans suite (${escapeHtml(relTime)})</span>`;
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
      `<div class="meta-input-group"><label>💵 Montant du Deal (FCFA)</label><input type="number" min="0" step="5000" value="${b.deal_amount || ''}" placeholder="Ex: 75000 FCFA" onchange="updateLeadMeta(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'), {deal_amount: this.value})" /></div>` +
      `<div class="meta-input-group"><label>📝 Note / Suivi / Relance</label><input type="text" value="${escapeHtml(b.notes || '')}" placeholder="Ex: Rappeler jeudi à 15h, intéressé par site vitrine..." onchange="updateLeadMeta(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'), {notes: this.value})" /></div>` +
      `</div>`;

    return `<div class="pipeline-stage-box"><div class="pipeline-stage-header">${stageHeader}</div><div class="pipeline-next-btns">${stageBtns}</div>${sixPillsHtml}${metaInputsHtml}</div>`;
  }

  function render() {
    updateKPIs();
    const q = document.getElementById('searchInput').value.toLowerCase().trim();
    const cf = document.getElementById('cityFilter').value || 'ALL';
    const crmf = document.getElementById('crmFilter').value || 'ALL';
    const st = document.getElementById('statusFilter').value || 'ALL';
    const sec = document.getElementById('sectorFilter').value || 'ALL';
    const pf = document.getElementById('phoneFilter').value || 'ALL';
    const hideKnownEl = document.getElementById('hideKnownLeadsToggle');
    const hideKnown = Boolean(hideKnownEl && hideKnownEl.checked && CURRENT_VIEW !== 'crm');
    const container = document.getElementById('leadContainer');

    highlightActiveKpi(st);
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
      if (st === 'HAS_FACEBOOK') {
        if (!item.facebook_url) return false;
      } else if (st !== 'ALL' && item.status !== st) return false;
      if (sec !== 'ALL' && getLeadSectorKey(item) !== sec) return false;
      if (pf !== 'ALL' && item.phone_type !== pf) return false;
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
        container.innerHTML = `<div class="empty-box"><p style="font-size:1rem; font-weight:800; color:#fff; margin-bottom:6px;">📭 Ton Pipeline Commercial est vide pour le moment.</p><p style="font-size:0.84rem; margin-bottom:14px;">Va dans le <strong>Radar de Scan</strong> et appuie sur le bouton <strong>« ➕ Mettre dans À contacter »</strong> sur les entreprises qui t'intéressent pour les retrouver ici !</p><button class="btn btn-primary" style="max-width:280px;" onclick="switchAppView('scanner')">🔍 Aller au Radar de Scan</button></div>`;
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

      const currentPitch = generatePitch(b, CURRENT_OFFER);
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
        ? `<div class="pitch-box"><strong>Pitch (${escapeHtml(CURRENT_OFFER)}) :</strong> ${escapeHtml(currentPitch)}</div>`
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
          workflowZoneHtml = `<div class="scan-triage-bar">` +
            `<button type="button" class="btn-add-pipeline" onclick="addLeadToPipeline(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'))">➕ Mettre dans « À contacter »</button>` +
            `<button type="button" class="btn btn-reset" style="width:auto; min-height:42px; padding:8px 12px; font-size:0.75rem; color:#fca5a5; border-color:rgba(239,68,68,0.35);" onclick="setLeadCrmStatus(decodeURIComponent('${encName}'), 'IGNORE', decodeURIComponent('${encCity}'))" title="Ignorer ce résultat">🚫 Ignorer</button>` +
            `</div>`;
        }
      }

      return `<div class="${cardClass}"><div><div class="card-top">` +
        `<div class="score-badge" onclick="openLeadDetail(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'))" title="Ouvrir la fiche détaillée"><span class="score-num" style="color:${scoreColor}">${b.score}</span><span class="score-lbl">Score</span></div>` +
        `<div class="title-wrap"><div class="biz-name-row"><span class="biz-name" onclick="openLeadDetail(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'))" title="Cliquer pour entrer dans la fiche complète">${escapeHtml(b.name)}</span>` +
        `<button type="button" class="btn-open-detail" onclick="openLeadDetail(decodeURIComponent('${encName}'), decodeURIComponent('${encCity}'))">🔎 Fiche & Suivi</button></div>` +
        `<div class="badges-row"><span class="status-pill st-${b.status}">${b.status.replace('_', ' ')}${speedTag}</span>${phoneLabel}${pixelBadge}${gaBadge}</div></div></div>` +
        `${workflowZoneHtml}` +
        `<div class="meta-chips"><span class="meta-item"><a href="${mapsUrl}" target="_blank" title="Voir sur Google Maps">📍 ${escapeHtml(b.city)} (${escapeHtml(b.neighborhood)})</a></span><span class="meta-item">${escapeHtml(shortSector)}</span><span class="meta-item">📞 <strong>${escapeHtml(b.phone)}</strong></span>${emailChip}<span class="meta-item">${siteHtml}</span></div>` +
        `<div class="issues">${(b.issues || []).map(iss => `<span class="issue-tag">⚠️ ${escapeHtml(iss)}</span>`).join('')}</div>` +
        `${pitchBoxHtml}</div>` +
        `<div class="actions">${contactBtn}${mapsBtn}${fbBtn}${copyBtn}</div></div>`;
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
            import datetime
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

            now_iso = str(body.get("updated_at") or datetime.datetime.now(datetime.timezone.utc).isoformat())

            with STATE_LOCK:
                current = load_saved_leads()
                norm_name = name.lower()
                norm_city = city.lower()
                target_lead = None
                for lead in current:
                    l_name = str(lead.get("name", "")).strip().lower()
                    l_city = str(lead.get("city", "")).strip().lower()
                    if l_name == norm_name and (l_city == norm_city or not l_city or not norm_city):
                        target_lead = lead
                        break

                if target_lead is None:
                    from urllib.parse import quote_plus
                    loc = city if city != "Audit Direct" else "Burkina Faso"
                    q = quote_plus(f"{name} {loc}".strip())
                    target_lead = {
                        "name": name,
                        "city": city,
                        "crm_status": new_status if isinstance(new_status, str) else "A_CONTACTER",
                        "in_pipeline": True,
                        "rdv_done": False,
                        "deal_amount": 0,
                        "notes": "",
                        "updated_at": now_iso,
                        "has_fb_pixel": False,
                        "has_google_analytics": False,
                        "maps_url": f"https://www.google.com/maps/search/?api=1&query={q}",
                    }
                    current.append(target_lead)

                if isinstance(new_status, str) and new_status in VALID_CRM_STATUSES:
                    target_lead["crm_status"] = new_status
                    if "in_pipeline" not in body:
                        target_lead["in_pipeline"] = new_status != "NOUVEAU"

                if "in_pipeline" in body:
                    target_lead["in_pipeline"] = bool(body.get("in_pipeline"))
                if "rdv_done" in body:
                    target_lead["rdv_done"] = bool(body.get("rdv_done"))
                if "deal_amount" in body:
                    target_lead["deal_amount"] = _safe_int_amount(body.get("deal_amount", 0))
                if "notes" in body:
                    target_lead["notes"] = str(body.get("notes") or "").strip()
                target_lead["updated_at"] = now_iso

                save_leads(current)

            self._send_json({
                "ok": True,
                "name": name,
                "city": city,
                "crm_status": target_lead.get("crm_status", "A_CONTACTER"),
                "in_pipeline": bool(target_lead.get("in_pipeline", True)),
                "rdv_done": bool(target_lead.get("rdv_done", False)),
                "deal_amount": _safe_int_amount(target_lead.get("deal_amount", 0)),
                "notes": str(target_lead.get("notes") or ""),
                "updated_at": target_lead.get("updated_at", now_iso),
            })
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
            incoming_keys = {
                f"{str(d.get('name', '')).strip().lower()}|{str(d.get('city', '')).strip().lower()}"
                for d in incoming_dicts
            }
            with STATE_LOCK:
                merged_dicts = merge_leads(load_saved_leads(), incoming_dicts)
                save_leads(merged_dicts)
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
    if not STATE_FILE.exists():
        should_seed = True
    else:
        try:
            existing = load_saved_leads()
            covered_sectors = {d.get("sector_key") for d in existing if d.get("sector_key")}
            if len(existing) < 40 or len(covered_sectors) < 10:
                should_seed = True
        except Exception:
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
                existing = load_saved_leads() if STATE_FILE.exists() else []
                merged = merge_leads(existing, incoming)
                save_leads(merged)
            print(f"[+] Base de leads initialisée avec succès ({len(merged)} prospects multi-secteurs).", flush=True)
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

