"""
seed_leads.py — Seeds leads_state.json with a rich, balanced dataset across all 16 B2B sectors.
"""
from pathlib import Path
import json
import concurrent.futures

import prospect_bf
import server_bf

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "leads_state.json"

def seed():
    print("[*] Fetching leads across all 16 sectors...")
    raw_leads = prospect_bf.fetch_osm_businesses("ouaga", "all", limit=80)
    print(f"[+] Fetched {len(raw_leads)} leads from OSM and fallbacks.")

    print("[*] Auditing leads...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=14) as pool:
        audited = list(pool.map(prospect_bf.audit_single_business, raw_leads))

    audited.sort(
        key=lambda b: (b.lead_score, prospect_bf.is_likely_mobile_bf(b.phone_clean), bool(b.phone_clean)),
        reverse=True,
    )

    incoming_dicts = [server_bf.lead_to_dict(b) for b in audited]
    
    # Also load any fallback leads for sectors with fewer than 3 leads
    from collections import Counter
    counts = Counter(d.get("sector_key") for d in incoming_dicts)
    for skey in prospect_bf.SECTOR_CATEGORIES:
        if skey == "all":
            continue
        if counts[skey] < 3:
            more = prospect_bf.get_offline_fallback_leads("Ouagadougou", skey)
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
                audited_more = list(pool.map(prospect_bf.audit_single_business, more))
            for b in audited_more:
                d = server_bf.lead_to_dict(b)
                if not any(x["name"].lower() == d["name"].lower() for x in incoming_dicts):
                    incoming_dicts.append(d)

    print(f"[*] Total leads to save: {len(incoming_dicts)}")
    final_counts = Counter(d.get("sector_key") for d in incoming_dicts)
    print("Sectors breakdown:", dict(final_counts))
    
    server_bf.save_leads(incoming_dicts)
    print(f"[SUCCESS] Saved {len(incoming_dicts)} leads to {STATE_FILE.name}!")

if __name__ == "__main__":
    seed()
