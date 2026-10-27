# ProspectBF Test Suite Readiness Report (`TEST_READY.md`)

## 1. Test Runner Command

The test suite is fully implemented, verified, and executable via standard Python:

```bash
python test_suite_e2e.py
```

### Execution Properties
- **Location**: `C:\Users\User\Desktop\PROJETSAUHASARD\prospectbf\test_suite_e2e.py`
- **Dependencies**: 100% Python Standard Library (`unittest`, `urllib`, `http.server`, `json`, `csv`, `re`, `subprocess`, `threading`, `tempfile`, `pathlib`).
- **Isolation**: Ephemeral mock HTTP servers (`127.0.0.1`) and sandboxed state files prevent network flake and state pollution.
- **Exit Code**: Exits with code `0` when all tests pass, non-zero when tests fail.

---

## 2. Test Coverage & Statistics Across All 4 Tiers

| Tier / Feature Group | Tests Planned | Current Baseline Passes | Current Baseline Failures | Target When Complete | Status |
|---|---|---|---|---|---|
| **Tier 1: Feature Coverage** | **29** | **19** | **10** | **29** | **READY** |
| • R1: Mini-CRM Lead Tracking & Sync | 6 | 1 | 5 | 6 | Awaiting M2 & M3 |
| • R2: Expansion to 7 Burkinabè Cities | 6 | 5 | 1 | 6 | Awaiting M3 `#scanCity` |
| • R3: Sales Offer & Dynamic WhatsApp Pitch | 6 | 3 | 3 | 6 | Awaiting M3 UI & JS |
| • R4: Ad Pixel & Analytics Detection | 6 | 5 | 1 | 6 | Awaiting M2 `lead_to_dict` |
| • R5: Render Compatibility & Deployment | 5 | 5 | 0 | 5 | **100% PASS** |
| **Tier 2: Boundary & Corner Cases** | **6** | **4** | **2** | **6** | **READY** |
| • Invalid CRM status rejection (HTTP 400) | 1 | 0 | 1 | 1 | Awaiting M2 API |
| • Empty / whitespace CRM inputs | 1 | 0 | 1 | 1 | Awaiting M2 API |
| • Phone normalization edge cases (ARCEP) | 1 | 1 | 0 | 1 | **100% PASS** |
| • Empty & truncated HTML audit resilience | 1 | 1 | 0 | 1 | **100% PASS** |
| • Offline fallback on network timeout | 1 | 1 | 0 | 1 | **100% PASS** |
| • Connection failure resilience (SITE_MORT) | 1 | 1 | 0 | 1 | **100% PASS** |
| **Tier 3: Cross-Feature Combinations** | **4** | **1** | **3** | **4** | **READY** |
| • 7 Cities × 3 Offers × Tracking Combinations | 1 | 0 | 1 | 1 | Awaiting M3 JS |
| • Multi-city scan merging preserving CRM status | 1 | 0 | 1 | 1 | Awaiting M2 merge |
| • City & CRM filter composition in UI | 1 | 0 | 1 | 1 | Awaiting M3 UI |
| • Multi-city CSV export with CRM status | 1 | 1 | 0 | 1 | **100% PASS** |
| **Tier 4: Real-World Workload Scenarios** | **2** | **0** | **2** | **2** | **READY** |
| • Scan → Review → Update CRM → Pitch → Re-scan → Export | 1 | 0 | 1 | 1 | Awaiting M2 & M3 |
| • Single direct audit → Tracking check → CRM update | 1 | 0 | 1 | 1 | Awaiting M2 & M3 |
| **TOTAL** | **41** | **24** | **17** | **41** | **SUITE OPERATIONAL** |

---

## 3. Baseline Failure Breakdown & Worker Handoff Contract

The 17 baseline failures strictly delineate the pending implementation milestones:

1. **Milestone M2 (Server API & CRM Persistence)**:
   - Needs: Expose `"crm_status"`, `"has_fb_pixel"`, `"has_google_analytics"` in `lead_to_dict`.
   - Needs: Update `merge_leads` to preserve existing `crm_status` across scans.
   - Needs: Implement `POST /api/leads/status` and alias `POST /api/leads/crm` validating valid CRM states (`A_CONTACTER`, `CONTACTE`, `CLIENT`, `IGNORE`) and rejecting invalid inputs with HTTP 400.
2. **Milestone M3 (Frontend UI, CRM Controls & Pitch Selector)**:
   - Needs: Add 5 cities (`koudougou`, `banfora`, `ouahigouya`, `tenkodogo`, `kaya`) into `#scanCity`.
   - Needs: Add `#crmFilter` dropdown and one-tap CRM buttons (`⚪ À contacter`, `🟡 Contacté`, `🟢 RDV / Client`, `🔴 Ignorer`) on cards.
   - Needs: Add `.offer-selector-card` with `WEB`, `ADS`, `MAPS` buttons.
   - Needs: Implement `generatePitch(lead, offer)`, `buildWhatsAppUrl(phone, pitch)`, and `setSalesOffer(offer)` in JavaScript.

As soon as M2 and M3 are committed, running `python test_suite_e2e.py` will automatically verify all 41 test cases without any modification to the test harness.
