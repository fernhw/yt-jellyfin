#!/usr/bin/env python3
"""diskPriceCheck.py — Monitor diskprices.com for good HDD/SSD storage deals.

Runs from cron twice daily. Sends OneSignal push notifications when drives drop
below the deal thresholds. Tracks already-seen deals in disk_deals_seen.json to
avoid repeat alerts.

Budget context: 4-bay DAS, RAID5, ~$1 000 budget, targeting ≥ 30 TB usable.
  Ideal: 10 TB @ $250 → 4 drives = $1 000 → 30 TB usable (RAID5).
  Better: any drive with a lower $/TB than those thresholds.

Thresholds:
  NEW drives:         ≤ $25.00/TB
  RECERTIFIED drives: ≤ $20.00/TB
  USED drives:        skipped

Usage:
  python3 diskPriceCheck.py           # normal cron run
  python3 diskPriceCheck.py --force   # re-notify ALL current deals (testing)
"""

import json
import logging
import os
import re
import sys
import time
import urllib.request
import urllib.error
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

# ── Paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = os.path.dirname(os.path.realpath(__file__))
SECRETS_FILE = os.path.join(SCRIPT_DIR, "secrets.md")
SEEN_FILE    = os.path.join(SCRIPT_DIR, "disk_deals_seen.json")
LOG_FILE     = os.path.join(SCRIPT_DIR, "diskPriceCheck.log")

# ── OneSignal ──────────────────────────────────────────────────────────────────
ONESIGNAL_APP_ID  = "c88ae5a3-36df-4301-945f-9da65e63d87c"
ONESIGNAL_API_URL = "https://onesignal.com/api/v1/notifications"
DISKPRICES_URL    = "https://diskprices.com/"

# ── DAS / budget config ────────────────────────────────────────────────────────
DAS_BAYS   = 4       # drive bays in the DAS
DAS_BUDGET = 1000.0  # total USD budget for all drives
# RAID5 with 4 drives: 1 parity drive → 3 usable
USABLE_BAYS = DAS_BAYS - 1  # 3

# ── Deal thresholds ($/TB) ─────────────────────────────────────────────────────
# Ideal: 10 TB @ $250.00 = $25.00/TB → 4 × $250 = $1 000 → 30 TB usable
# "Better" means *below* these numbers → even louder flag in notification.
THRESHOLD_NEW    = 26.00   # $/TB cap for NEW drives
THRESHOLD_RECERT = 26.00   # $/TB cap for RECERTIFIED drives
MIN_CAPACITY_TB  = 10.0    # ignore drives smaller than this (must be ≥ 10 TB)

# Per-drive absolute price caps (independent of $/TB threshold)
# ≥ 12 TB drives get a higher cap since they're naturally more expensive.
PRICE_CAP_STANDARD = 270.0   # max price/drive for drives < 12 TB
PRICE_CAP_LARGE    = 319.0   # max price/drive for drives ≥ 12 TB
PRICE_CAP_CUTOFF   = 12.0    # TB boundary between the two caps

# Conditions to check and their thresholds; used drives intentionally excluded
CONDITIONS: Dict[str, float] = {
    "new":         THRESHOLD_NEW,
    "recertified": THRESHOLD_RECERT,
}

# diskprices.com serves condition-filtered SSR pages. We fetch once per
# condition and validate the condition column in Python as a sanity check.
BASE_URL = (
    "https://diskprices.com/?locale=us&capacity=4-40"
    "&disk_types=internal_hdd,m2_ssd,u2"
)

# Condition aliases — normalise page values to our keys
COND_ALIASES: Dict[str, str] = {
    "new":                  "new",
    "certified refurbished": "recertified",
    "refurbished":          "recertified",
    "recertified":          "recertified",
}

# Send at most 1 notification per run (the single best deal across all conditions).
MAX_NOTIFY_PER_RUN = 1

# ── NAS drive keywords — flagged specially in notifications ────────────────────
NAS_KEYWORDS = (
    "ironwolf", "red plus", "red pro", "wd red", "exos", "ultrastar",
    "gold", "n300", "toshiba n300", "enterprise",
)

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

_con = logging.StreamHandler(sys.stdout)
_con.setLevel(logging.INFO)
_con.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
log.addHandler(_con)

# ── OneSignal helpers ──────────────────────────────────────────────────────────

def _read_onesignal_key() -> Optional[str]:
    """Reconstruct the obfuscated OneSignal REST key from secrets.md."""
    chars: Dict[int, str] = {}
    try:
        with open(SECRETS_FILE) as fh:
            for line in fh:
                m = re.match(r'^K(\d+)="(.)"', line.rstrip())
                if m:
                    chars[int(m.group(1))] = m.group(2)
    except FileNotFoundError:
        return None
    return "".join(v for k, v in sorted(chars.items())) if chars else None


def onesignal_push(heading: str, body: str, url: str = DISKPRICES_URL) -> None:
    key = _read_onesignal_key()
    if not key:
        log.warning("OneSignal key missing — push skipped")
        return
    payload = json.dumps({
        "app_id":            ONESIGNAL_APP_ID,
        "included_segments": ["All"],
        "headings":          {"en": heading},
        "contents":          {"en": body},
        "url":               url,
    }).encode()
    req = urllib.request.Request(
        ONESIGNAL_API_URL,
        data=payload,
        headers={
            "Authorization": f"Basic {key}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            log.info(f"  Push sent: {heading}")
    except Exception as exc:
        log.warning(f"  Push failed: {exc}")

# ── Seen-deals persistence ─────────────────────────────────────────────────────

def load_seen() -> set:
    try:
        with open(SEEN_FILE) as fh:
            return set(json.load(fh).get("seen", []))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen: set) -> None:
    with open(SEEN_FILE, "w") as fh:
        json.dump({"seen": sorted(seen)}, fh, indent=2)

# ── HTML table parser ──────────────────────────────────────────────────────────

class _TableParser(HTMLParser):
    """Collect <th>/<td> text AND the first <a href> from every cell in every <tr>."""

    def __init__(self) -> None:
        super().__init__()
        self.rows:  List[List[str]] = []
        self.links: List[List[str]] = []   # parallel to rows; href per cell
        self._row:       List[str] = []
        self._row_links: List[str] = []
        self._cell:      List[str] = []
        self._cell_link: str = ""
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._row = []
            self._row_links = []
        elif tag in ("td", "th"):
            self._in_cell = True
            self._cell = []
            self._cell_link = ""
        elif tag == "a" and self._in_cell and not self._cell_link:
            attrs_dict = dict(attrs)
            href = attrs_dict.get("href", "")
            if href.startswith("http"):
                self._cell_link = href

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th"):
            self._row.append(" ".join("".join(self._cell).split()))
            self._row_links.append(self._cell_link)
            self._in_cell = False
        elif tag == "tr":
            if any(c for c in self._row):
                self.rows.append(self._row[:])
                self.links.append(self._row_links[:])

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell.append(data)

# ── Parsing helpers ────────────────────────────────────────────────────────────

def _parse_capacity(raw: str) -> Optional[Tuple[float, int]]:
    """Return (capacity_tb, pack_size).  e.g. '28 TB x4' → (28.0, 4)."""
    pack = 1
    pm = re.search(r'x\s*(\d+)', raw, re.IGNORECASE)
    if pm:
        pack = int(pm.group(1))
    m = re.search(r'([\d.]+)\s*(TB|GB|T|G)\b', raw, re.IGNORECASE)
    if not m:
        return None
    val, unit = float(m.group(1)), m.group(2).upper()
    tb = val if unit in ("T", "TB") else val / 1000
    return (tb, pack)


def _parse_price(raw: str) -> Optional[float]:
    cleaned = raw.replace(",", "").strip()
    m = re.search(r'\$?\s*([\d]+\.?\d*)', cleaned)
    return float(m.group(1)) if m else None

# ── Fetch & parse diskprices.com ───────────────────────────────────────────────

def fetch_drives(condition: str) -> List[dict]:
    """Fetch drives for one condition. Validates the condition column in HTML
    and skips any rows that don’t match (catches server/client filter quirks)."""
    url = f"{BASE_URL}&condition={condition}"
    log.info(f"Fetching [{condition}]: {url}")

    try:
        req = urllib.request.Request(url, headers=HTTP_HEADERS)
        with urllib.request.urlopen(req, timeout=25) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        log.warning(f"  Fetch failed: {exc}")
        return []

    parser = _TableParser()
    parser.feed(html)
    rows  = parser.rows
    links = parser.links

    if not rows:
        log.warning("  No table rows found in HTML")
        return []

    # ── Identify header row ─────────────────────────────────────────────────
    # diskprices.com columns (in order):
    #   Price per TB | Price | Capacity | Warranty | Form Factor | Technology | Condition | Affiliate Link
    header_idx: Optional[int] = None
    col_map: Dict[str, int] = {}

    for i, row in enumerate(rows):
        row_lc = [c.lower().strip() for c in row]
        if any("price" in c for c in row_lc) and any("capacity" in c for c in row_lc):
            header_idx = i
            col_map = {text: idx for idx, text in enumerate(row_lc)}
            break

    if header_idx is None:
        log.warning("  Header row not found")
        return []

    # Map column names to indices
    # "price per tb" and "price" are distinct keys in col_map so no collision
    i_pptb       = col_map.get("price per tb", col_map.get("$/tb"))
    i_price      = col_map.get("price")
    i_capacity   = col_map.get("capacity")
    i_technology = col_map.get("technology")
    i_condition  = col_map.get("condition")
    i_name       = col_map.get("affiliate link", col_map.get("link", col_map.get("name")))

    # Fallback to positional defaults if header parsing is ambiguous
    # diskprices.com columns (0-indexed): Price/GB | Price/TB | Price | Capacity |
    #   Warranty | Form Factor | Technology | Condition | Affiliate Link
    if i_pptb is None:       i_pptb       = 1
    if i_price is None:      i_price      = 2
    if i_capacity is None:   i_capacity   = 3
    if i_technology is None: i_technology = 6
    if i_condition is None:  i_condition  = 7
    if i_name is None:       i_name       = 8

    def _get(row: List[str], idx: int) -> str:
        return row[idx].strip() if idx < len(row) else ""

    drives: List[dict] = []
    for row_idx, row in enumerate(rows[header_idx + 1:]):
        if len(row) < 3:
            continue

        # Only keep SATA-compatible drives — skip SAS, NVMe, tape, etc.
        # diskprices.com labels SATA HDDs as 'HDD', SATA SSDs as 'SATA', SAS as 'SAS'.
        tech_raw = _get(row, i_technology).lower()
        if tech_raw and tech_raw not in ("hdd", "sata", "hybrid"):
            continue

        # Validate condition column against what we requested.
        # If the page leaks other conditions, skip them.
        cond_raw = _get(row, i_condition).lower()
        cond_norm = COND_ALIASES.get(cond_raw, "")
        # Accept rows whose normalised condition matches the requested one,
        # OR rows with an empty condition cell (some pages omit it).
        if cond_norm and cond_norm != condition:
            continue

        name_raw  = _get(row, i_name)
        price_raw = _get(row, i_price)
        cap_raw   = _get(row, i_capacity)
        # Grab the Amazon/affiliate href from the name column
        link_row  = links[header_idx + 1 + row_idx] if (header_idx + 1 + row_idx) < len(links) else []
        drive_link = link_row[i_name] if i_name < len(link_row) else ""

        price = _parse_price(price_raw)
        if price is None or price <= 0:
            continue

        cap_result = _parse_capacity(cap_raw)
        if cap_result is None:
            # Try to pull capacity from the drive name itself
            m = re.search(r'(\d+(?:\.\d+)?)\s*TB', name_raw, re.IGNORECASE)
            if m:
                cap_result = (float(m.group(1)), 1)
            else:
                continue

        capacity_tb, pack_size = cap_result
        if capacity_tb < MIN_CAPACITY_TB:
            continue

        # Multi-packs: price column is total pack price; normalize to per-drive
        per_drive_price = price / pack_size
        pptb = per_drive_price / capacity_tb

        drives.append({
            "name":        name_raw[:80],
            "link":        drive_link or DISKPRICES_URL,
            "price":       per_drive_price,
            "pack_size":   pack_size,
            "capacity_tb": capacity_tb,
            "condition":   condition,  # normalised: "new" or "recertified"
            "pptb":        pptb,
        })

    log.info(f"  → {len(drives)} drives parsed")
    return drives

# ── Deal helpers ───────────────────────────────────────────────────────────────

def is_nas(name: str) -> bool:
    lower = name.lower()
    return any(kw in lower for kw in NAS_KEYWORDS)


def deal_key(d: dict) -> str:
    """Stable dedup key: condition + name snippet + price bucket ($5 steps)."""
    bucket = int(d["price"] // 5) * 5
    return f"{d['condition']}|{d['name'][:60]}|{bucket}"


def format_notification(d: dict) -> Tuple[str, str]:
    cond_label = "NEW" if d["condition"] == "new" else "RECERT"
    nas_tag    = " [NAS]" if is_nas(d["name"]) else ""

    # Flag if this beats the ideal ($25/TB for new, $20/TB for recert)
    if d["pptb"] < (THRESHOLD_NEW * 0.8):      # ≥ 20% better than threshold
        deal_tier = "🔥 GREAT DEAL"
    else:
        deal_tier = "✅ DEAL"

    heading = (
        f"{deal_tier} — {cond_label}{nas_tag}: "
        f"{d['capacity_tb']:.0f} TB @ ${d['pptb']:.2f}/TB"
    )

    # RAID5 usable capacity across all DAS bays
    usable_tb  = d["capacity_tb"] * USABLE_BAYS
    total_cost = d["price"] * DAS_BAYS
    budget_ok  = "fits $1k budget ✅" if total_cost <= DAS_BUDGET else f"total ${total_cost:.0f} ⚠️"

    name_short = d["name"][:52] + ("…" if len(d["name"]) > 52 else "")
    body = (
        f"{name_short}  "
        f"${d['price']:.0f}/drive · ${d['pptb']:.2f}/TB  "
        f"4× → {usable_tb:.0f} TB usable (RAID5) · {budget_ok}"
    )
    return heading, body

# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    force = "--force" in sys.argv
    log.info("=== diskPriceCheck run ===")

    seen      = load_seen()
    new_seen: set = set()
    sent      = 0

    # Collect all qualifying deals across every condition
    all_deals: List[dict] = []
    for condition, threshold in CONDITIONS.items():
        drives = fetch_drives(condition)
        if not drives:
            log.warning(f"  No drives returned for condition={condition}")
            continue

        def _price_ok(d: dict) -> bool:
            cap = PRICE_CAP_LARGE if d["capacity_tb"] >= PRICE_CAP_CUTOFF else PRICE_CAP_STANDARD
            return d["price"] < cap

        deals = sorted(
            [d for d in drives if d["pptb"] <= threshold and _price_ok(d)],
            key=lambda x: x["pptb"],
        )
        log.info(f"  [{condition}] {len(drives)} drives, {len(deals)} deal(s) at ≤${threshold:.2f}/TB + price cap")

        # Track all deal keys as seen regardless of whether we notify
        for d in deals:
            new_seen.add(deal_key(d))

        all_deals.extend(deals)

    # Sort globally by $/TB and pick the single best unseen deal
    all_deals.sort(key=lambda x: x["pptb"])
    to_notify = [d for d in all_deals if deal_key(d) not in seen or force]
    to_notify = to_notify[:MAX_NOTIFY_PER_RUN]  # just 1

    for d in to_notify:
        heading, body = format_notification(d)
        log.info(f"  DEAL: {heading}")
        onesignal_push(heading, body, url=d.get("link", DISKPRICES_URL))
        sent += 1

    # Persist seen keys; cap at 1 000 entries
    merged = seen | new_seen
    if len(merged) > 1000:
        merged = new_seen
    save_seen(merged)

    log.info(f"=== Done — {sent} new notification(s) sent ===")


if __name__ == "__main__":
    main()
