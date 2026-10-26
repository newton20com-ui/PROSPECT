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
    phone_display = b.phone_clean or b.phone_raw or "Non renseigné"
    is_mobile = is_likely_mobile_bf(b.phone_clean)
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
    }


def load_saved_leads() -> list[dict]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_leads(leads_dicts: list[dict]) -> None:
    STATE_FILE.write_text(json.dumps(leads_dicts, ensure_ascii=False, indent=2), encoding="utf-8")


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

    <!-- RESPONSIVE FILTERS -->
    <div class="filters-box">
      <div class="search-row">
        <input type="search" id="searchInput" placeholder="🔎 Chercher nom, quartier, défaut..." />
        <button class="btn btn-reset" onclick="resetFilters()">🔄 Tout</button>
      </div>
      <div class="selects-grid">
        <select id="statusFilter"></select>
        <select id="sectorFilter"></select>
        <select id="phoneFilter"></select>
      </div>
    </div>

    <div class="filter-summary">
      <span id="filterCountText">Chargement...</span>
    </div>

    <div id="leadContainer" class="lead-list"></div>
  </div>

  <script>
    let LEADS = [];

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
      document.getElementById('statusFilter').value = 'ALL';
      document.getElementById('sectorFilter').value = 'ALL';
      document.getElementById('phoneFilter').value = 'ALL';
      highlightActiveKpi('ALL');
      render();
    }

    function populateFilterDropdowns() {
      const statusEl = document.getElementById('statusFilter');
      const sectorEl = document.getElementById('sectorFilter');
      const phoneEl = document.getElementById('phoneFilter');

      const curStatus = statusEl.value || 'ALL';
      const curSector = sectorEl.value || 'ALL';
      const curPhone = phoneEl.value || 'ALL';

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

      let sectorHtml = `<option value="ALL">🏢 Tous les secteurs (${LEADS.length})</option>`;
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
        <option value="ALL">📞 Tous les contacts (${LEADS.length})</option>
        <option value="MOBILE_WHATSAPP">💬 Mobile WhatsApp (${cMob})</option>
        <option value="FIXE_ONATEL">☎️ Fixe ONATEL (${cFix})</option>
        <option value="AUCUN">🌐 Web sans numéro (${cNone})</option>
      `;
      phoneEl.value = curPhone;
    }

    async function loadInitialLeads() {
      try {
        const res = await fetch('/api/leads');
        const data = await res.json();
        LEADS = data.leads || [];
        populateFilterDropdowns();
        render();
      } catch (e) {
        console.error(e);
      }
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
          LEADS.unshift(data.lead);
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
          LEADS = data.leads;
          resetFilters();
          populateFilterDropdowns();
          render();
          showBanner(`✅ ${LEADS.length} entreprises auditées en direct !`);
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
      const st = document.getElementById('statusFilter').value || 'ALL';
      const sec = document.getElementById('sectorFilter').value || 'ALL';
      const pf = document.getElementById('phoneFilter').value || 'ALL';
      const container = document.getElementById('leadContainer');

      highlightActiveKpi(st);

      const filtered = LEADS.filter(item => {
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
          ? `<a href="${fullUrl}" target="_blank">🌐 ${domainText}</a>`
          : '<span>🌐 Aucun site</span>';

        const speedTag = b.response_ms ? ` • ${b.response_ms}ms` : '';
        const phoneLabel = b.phone_type === 'MOBILE_WHATSAPP'
          ? '<span class="phone-badge ph-MOBILE_WHATSAPP">📱 Mobile</span>'
          : (b.phone_type === 'FIXE_ONATEL' ? '<span class="phone-badge ph-FIXE_ONATEL">☎️ Fixe</span>' : '');

        const contactBtn = b.whatsapp_url
          ? `<a class="btn btn-wa" href="${b.whatsapp_url}" target="_blank">💬 WhatsApp</a>`
          : (b.tel_url ? `<a class="btn btn-call" href="${b.tel_url}">📞 Appeler</a>` : '');

        const fbBtn = b.facebook_url
          ? `<a class="btn btn-fb" href="${b.facebook_url}" target="_blank">📘 Facebook</a>`
          : '';

        const shortSector = SECTOR_LABELS[b.sector_key] || b.sector;

        return `
          <div class="lead-card">
            <div>
              <div class="card-top">
                <div class="score-badge">
                  <span class="score-num" style="color:${scoreColor}">${b.score}</span>
                  <span class="score-lbl">Score</span>
                </div>
                <div class="title-wrap">
                  <span class="biz-name">${b.name}</span>
                  <div class="badges-row">
                    <span class="status-pill st-${b.status}">${b.status.replace('_', ' ')}${speedTag}</span>
                    ${phoneLabel}
                  </div>
                </div>
              </div>

              <div class="meta-chips">
                <span class="meta-item">📍 ${b.city} (${b.neighborhood})</span>
                <span class="meta-item">${shortSector}</span>
                <span class="meta-item">📞 <strong>${b.phone}</strong></span>
                <span class="meta-item">${siteHtml}</span>
              </div>

              <div class="issues">
                ${(b.issues || []).map(iss => `<span class="issue-tag">⚠️ ${iss}</span>`).join('')}
              </div>

              <div class="pitch-box">
                <strong>Pitch :</strong> ${b.pitch}
              </div>
            </div>

            <div class="actions">
              ${contactBtn}
              ${fbBtn}
              <button class="btn btn-copy" onclick="navigator.clipboard.writeText(${JSON.stringify(b.pitch).replace(/"/g, '&quot;')}); this.textContent='✅ Copié !'; setTimeout(()=>this.textContent='📋 Copier Pitch', 1500);">📋 Copier Pitch</button>
            </div>
          </div>
        `;
      }).join('');
    }

    document.getElementById('searchInput').addEventListener('input', render);
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
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}

        if parsed.path == "/api/audit-single":
            name = body.get("name", "Entreprise").strip() or "Entreprise"
            sector_key = body.get("sector_key", "entreprise").strip() or "entreprise"
            sector_label = SECTOR_CATEGORIES.get(sector_key, SECTOR_CATEGORIES["entreprise"])
            website = body.get("website", "").strip()
            phone_raw = body.get("phone", "").strip()

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

            current = load_saved_leads()
            current.insert(0, lead_dict)
            save_leads(current)
            self._send_json({"lead": lead_dict})
            return

        if parsed.path == "/api/scan":
            city = body.get("city", "ouaga")
            sector = body.get("sector", "all")
            limit = min(int(body.get("limit", 75)), 130)

            raw_leads = fetch_osm_businesses(city_key=city, sector_filter=sector, limit=limit)
            with concurrent.futures.ThreadPoolExecutor(max_workers=14) as pool:
                audited_leads = list(pool.map(audit_single_business, raw_leads))

            audited_leads.sort(
                key=lambda b: (b.lead_score, is_likely_mobile_bf(b.phone_clean), bool(b.phone_clean)),
                reverse=True,
            )
            export_csv(audited_leads, BASE_DIR / f"leads_{city}.csv")

            leads_dicts = [lead_to_dict(b) for b in audited_leads]
            save_leads(leads_dicts)
            self._send_json({"leads": leads_dicts})
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

