# ProspectBF E2E Test Infrastructure & Methodology (`TEST_INFRA.md`)

## 1. Overview & Architecture

This document defines the end-to-end (E2E) testing methodology, architectural framework, and complete test inventory for the **ProspectBF** commercial upgrade.

The E2E test suite resides in `test_suite_e2e.py` at the project root and is executed via:
```bash
python test_suite_e2e.py
```

### Key Architectural Characteristics
- **Zero External Dependencies**: Built 100% on the Python Standard Library (`unittest`, `urllib.request`, `http.server`, `json`, `csv`, `re`, `subprocess`, `threading`, `tempfile`, `pathlib`).
- **Opaque-Box Testing**: Validates system contracts, HTTP APIs, data persistence, client-side rendering contracts, and file configurations against authoritative specifications without relying on internal private state.
- **Hermetic & Isolated**: Spins up ephemeral local HTTP servers and sandboxes `leads_state.json` within temporary storage to guarantee tests run offline, deterministically, and without side effects.
- **Multi-Tiered Coverage**: Organized into 4 distinct functional and stress tiers with structured reporting per tier.

---

## 2. Test Design Methodologies

The test suite is designed following four rigorous testing disciplines:

### 2.1 Category-Partition Methodology
Each requirement (R1 through R5) is partitioned into functional categories, operational parameters, and equivalence classes:
- **CRM State Partitioning (R1)**:
  - Default initialization: newly created/discovered leads.
  - State transitions: `A_CONTACTER` → `CONTACTE` → `CLIENT` → `IGNORE`.
  - Persistence partition: client `localStorage` sync vs backend `/api/leads/status` & `leads_state.json`.
  - Non-destructive merging: existing custom CRM states vs incoming scan defaults.
  - Export partition: CSV export headers and row-level serialization.
- **Geographic Partitioning (R2)**:
  - 7 Cities: `ouaga`, `bobo`, `koudougou`, `banfora`, `ouahigouya`, `tenkodogo`, `kaya`.
  - Coordinates & Bounding Boxes: `(south, west, north, east)` format, center coordinates, boundary validation within Burkina Faso national territory.
  - Fallback partition: offline simulation for each city.
- **Offer & Pitch Partitioning (R3)**:
  - Commercial offers: `WEB`, `ADS`, `MAPS`.
  - Business technical health partitions: `SITE_MORT`, `SITE_FAIBLE`, `FACEBOOK_SEUL`, `SANS_SITE`, `SITE_OK`.
  - Pitch template parameters: business name, city, sector, detected weaknesses.
  - Action link partition: `wa.me/{phone}?text={encoded_pitch}` and `tel:{phone}`.
- **Auditing & Tracking Detection Partitioning (R4)**:
  - Meta/Facebook Pixel signatures: `fbq(`, `connect.facebook.net`, `fbevents.js`, `facebook.com/tr`.
  - Google Analytics / GTM signatures: `gtag(`, `googletagmanager.com`, `google-analytics.com`, `ga(`.
  - Technical vs Marketing issue partitioning: ensuring missing tracking flags `"Aucun Pixel FB ni Google Analytics détecté"` without breaking `SITE_OK`.
- **Deployment Partitioning (R5)**:
  - Process specification: `Procfile` command format.
  - Render configuration: `render.yaml` services, runtime, and build/start commands.
  - Python environment: `requirements.txt` cleanliness and standard library compliance.
  - Dynamic port binding: host `0.0.0.0` and `PORT` environment variable resolution.

### 2.2 Boundary Value Analysis (BVA)
- **Geographic Boundaries**: Burkina Faso territorial extremities:
  - Latitude: [9.40° N, 15.08° N]
  - Longitude: [-5.52° W, +2.40° E]
  - City bounding box edges tested for `south < north` and `west < east`.
- **Payload Boundaries**:
  - Empty, null, and whitespace lead names and city names in CRM status updates.
  - Invalid CRM statuses rejected with HTTP 400.
  - Zero-length and truncated HTML content in website audits.
  - Extreme phone formats: legacy +226 50 fixed lines, mobile 8-digit numbers, missing country codes, non-numeric characters.
- **HTTP & Response Time Boundaries**:
  - Response time threshold: `< 2500ms` (normal) vs `> 2500ms` (slow mobile penalty).
  - HTTP 500 error responses and connection timeouts.

### 2.3 Pairwise / Combinatorial Testing
- Cross-product testing across multiple feature axes:
  - City (7 cities) × Commercial Offer (`WEB`, `ADS`, `MAPS`) × Site Status (`SITE_OK`, `SITE_FAIBLE`, `FACEBOOK_SEUL`, `SANS_SITE`).
  - CRM Status (`A_CONTACTER`, `CONTACTE`, `CLIENT`, `IGNORE`) × Multi-scan merging over time.
  - City Filter × CRM Status Filter combinations in the SPA UI.

### 2.4 Real-World Workload Scenarios
- End-to-end user journeys mirroring actual tele-prospecting workflows:
  - **Workload Flow 1**: Scan city → Review lead cards → Update CRM statuses (`CONTACTE`, `CLIENT`, `IGNORE`) → Switch offer to `ADS` → Verify customized WhatsApp pitch → Re-scan city (confirming no data loss) → Export CSV with CRM status.
  - **Workload Flow 2**: Manual single lead audit → Detection of tracking tags → Automatic Google Maps URL generation → Offer switch to `MAPS` → Direct CRM update.

---

## 3. Test Inventory by Tier

| Tier | Focus | Test ID | Description |
|---|---|---|---|
| **Tier 1** | **Feature: R1 (Mini-CRM)** | `test_r1_01_default_crm_status` | Verifies default `crm_status` is `A_CONTACTER` on new leads |
| | | `test_r1_02_crm_valid_states_enum` | Validates all 4 CRM states (`A_CONTACTER`, `CONTACTE`, `CLIENT`, `IGNORE`) |
| | | `test_r1_03_merge_leads_preserves_crm_status` | Verifies `merge_leads` preserves custom CRM status across scans |
| | | `test_r1_04_api_status_update_and_persistence` | Tests `POST /api/leads/status` endpoint and disk persistence |
| | | `test_r1_05_crm_filter_dropdown_in_html` | Verifies `#crmFilter` dropdown and CRM action buttons in HTML |
| | | `test_r1_06_csv_export_includes_crm_status` | Confirms CRM status column in backend & frontend CSV exports |
| **Tier 1** | **Feature: R2 (7 Cities)** | `test_r2_01_cities_dict_has_all_seven_keys` | Verifies `CITIES` contains all 7 Burkinabè cities |
| | | `test_r2_02_bounding_boxes_valid_tuples` | Validates `(south, west, north, east)` tuples with `south < north`, `west < east` |
| | | `test_r2_03_coordinates_inside_burkina_faso` | Ensures bounding boxes & centers are strictly within Burkina Faso borders |
| | | `test_r2_04_scan_city_dropdown_in_html` | Confirms all 7 cities are present in `#scanCity` select element |
| | | `test_r2_05_city_filter_dropdown_handling` | Validates `#cityFilter` dropdown and dynamic city extraction |
| | | `test_r2_06_offline_fallback_supports_all_cities` | Verifies offline fallback handles all 7 cities with valid coordinates |
| **Tier 1** | **Feature: R3 (Offer & Pitch)** | `test_r3_01_offer_selector_markup_in_html` | Verifies UI markup contains offer selector with `WEB`, `ADS`, `MAPS` |
| | | `test_r3_02_web_pitch_adaptation` | Validates dynamic pitch generation for `WEB` offer by site condition |
| | | `test_r3_03_ads_pitch_adaptation` | Validates dynamic pitch generation for `ADS` offer by tracking/presence |
| | | `test_r3_04_maps_pitch_adaptation` | Validates dynamic pitch generation for `MAPS` offer with local SEO context |
| | | `test_r3_05_dynamic_whatsapp_url_encoding` | Confirms dynamic `wa.me` links with proper URL percent-encoding |
| | | `test_r3_06_pitch_engine_js_functions_present` | Verifies client-side functions `generatePitch`, `buildWhatsAppUrl`, `setSalesOffer` |
| **Tier 1** | **Feature: R4 (Tracking Audit)** | `test_r4_01_meta_pixel_detection_signatures` | Tests detection of Meta Pixel signatures (`fbq`, `fbevents.js`, `facebook.com/tr`) |
| | | `test_r4_02_google_analytics_detection_signatures` | Tests detection of GA/GTM signatures (`gtag`, `googletagmanager.com`, `ga`) |
| | | `test_r4_03_missing_tracking_issue_flag` | Confirms `"Aucun Pixel FB ni Google Analytics détecté"` is flagged in issues |
| | | `test_r4_04_site_ok_preservation_without_tracking` | Verifies healthy HTTPS sites retain `SITE_OK` even without tracking pixels |
| | | `test_r4_05_partial_tracking_detection` | Tests isolation when only Pixel or only GA is present |
| | | `test_r4_06_lead_to_dict_includes_tracking_flags` | Confirms serialization includes `has_fb_pixel` and `has_google_analytics` |
| **Tier 1** | **Feature: R5 (Deployment)** | `test_r5_01_procfile_format` | Verifies `Procfile` contains valid `web: python -X utf8 server_bf.py` |
| | | `test_r5_02_render_yaml_validity` | Verifies `render.yaml` specification for free Python web service |
| | | `test_r5_03_requirements_txt_exists` | Confirms `requirements.txt` is present and valid |
| | | `test_r5_04_dynamic_port_and_host_binding` | Checks `PORT` env var resolution and `0.0.0.0` binding in `server_bf.py` |
| | | `test_r5_05_py_compile_syntax_validation` | Runs `py_compile` on `prospect_bf.py` and `server_bf.py` (exit code 0) |
| **Tier 2** | **Boundary & Corner Cases** | `test_t2_01_empty_and_whitespace_crm_inputs` | Tests handling of blank, whitespace, or missing fields in CRM status API |
| | | `test_t2_02_invalid_crm_status_rejection_http_400` | Verifies invalid CRM status strings are rejected with HTTP 400 |
| | | `test_t2_03_audit_empty_or_truncated_html` | Verifies auditor resilience against empty (`""`) or truncated HTML |
| | | `test_t2_04_phone_normalization_edge_cases` | Tests `normalize_bf_phone` on legacy lines, mobile, spaces, missing prefixes |
| | | `test_t2_05_offline_fallback_when_osm_unavailable` | Verifies graceful fallback when Overpass is unreachable or inputs are edge-cases |
| | | `test_t2_06_http_timeout_and_network_error_resilience` | Confirms auditor assigns `SITE_MORT` on connection failure without crashing |
| **Tier 3** | **Cross-Feature Combinations**| `test_t3_01_offer_switching_across_all_cities_and_tracking_combinations` | Tests pitch generation across 7 cities × 3 offers × 4 tracking states |
| | | `test_t3_02_multi_city_scan_merging_with_existing_crm_states` | Tests merging scans across multiple cities preserving pre-existing CRM statuses |
| | | `test_t3_03_city_and_crm_filter_composition` | Tests combinatorial filtering by both City and CRM status in the SPA |
| | | `test_t3_04_csv_export_multi_city_multi_crm_data` | Tests CSV generation with mixed cities, CRM statuses, and tracking flags |
| **Tier 4** | **Real-World Workloads** | `test_t4_01_full_lifecycle_scan_review_crm_pitch_export` | Complete workflow: Scan → Review → Update CRM → Change Offer → Check wa.me → Re-scan → Export |
| | | `test_t4_02_direct_audit_to_crm_workload` | Workflow: Single business direct audit → Tracking check → Offer switch → CRM persist |

Total Planned Tests: **40 test cases** across all 4 tiers.

---

## 4. Execution & Validation Command

To execute the complete E2E test suite:
```bash
python test_suite_e2e.py
```

The runner provides colored/structured per-tier terminal reports, highlights failing assertions with clear diagnostic diffs, and exits with code `0` on 100% success or non-zero on failure.
