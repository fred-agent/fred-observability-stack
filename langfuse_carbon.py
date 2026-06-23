#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║          langfuse_carbon.py — Carbon Footprint Tracker          ║
║          Calculates CO₂ from LLM token usage stored in Langfuse             ║
║                                                                              ║
║  Modes:                                                                      ║
║    --report              Terminal report (all users, all models)             ║
║    --report --user NAME  Report for a specific user only                     ║
║    --push-scores         Write carbon_kg scores to Langfuse per trace        ║
║    --hours N             Look-back window (default: 720 = 30 days)           ║
║    --dry-run             Preview score pushes without writing                ║
║                                                                              ║
║  Examples:                                                                   ║
║    python3 langfuse_carbon.py --report                                       ║
║    python3 langfuse_carbon.py --report --user vishvendra.singh               ║
║    python3 langfuse_carbon.py --push-scores --hours 24                       ║
║    python3 langfuse_carbon.py --push-scores --dry-run                        ║
║                                                                              ║
║  Cron (daily report):                                                        ║
║    0 8 * * * cd /path && python3 langfuse_carbon.py --report >> carbon.log   ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import argparse
import base64
import json
import os
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
import urllib.request
import urllib.error

# ── Configuration ─────────────────────────────────────────────────────────────
LANGFUSE_BASE_URL   = os.environ.get("LANGFUSE_BASE_URL",   "http://localhost:3001")
LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-1e33f02a-a91b-4b0b-9ef7-4661c6dea09d")
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-10a4262e-2cc8-471c-ac2a-57f054d5e5f8")

# ── CO₂ emission factors per 1M tokens (kg CO₂) ──────────────────────────────
# Source: estimated from Anthropic infrastructure + grid intensity data.
# US East Coast (AWS us-east-1) grid intensity ≈ 0.233 kg CO₂/kWh
# Energy per 1M tokens estimated from published LLM benchmark data.
# These are directional estimates — not auditable for formal ESG reporting.
CO2_PER_1M_TOKENS: Dict[str, float] = {
    # Claude 3 family
    "claude-3-haiku":               0.28,
    "claude-3-sonnet":              0.55,
    "claude-3-opus":                1.10,
    # Claude 3.5 family
    "claude-3-5-haiku":             0.30,
    "claude-3-5-sonnet":            0.58,
    # Claude 3.7 family
    "claude-3-7-sonnet":            0.60,
    # Claude 4 / Haiku 4.5 family (your stack)
    "claude-haiku-4":               0.25,
    "claude-haiku-4-5":             0.27,
    "claude-haiku-4-5-20251001":    0.27,
    "claude-sonnet-4":              0.58,
    "claude-sonnet-4-6":            0.60,
    "claude-sonnet-4-6-20260626":   0.60,
    "claude-opus-4":                1.15,
    "claude-opus-4-5":              1.15,
    # Fallback for unknown models
    "default":                      0.58,
}

# Equivalence factors for report context
KG_CO2_PER_KM_CAR    = 0.12    # average petrol car kg CO₂ per km
KG_CO2_PER_FLIGHT_KM = 0.255   # economy seat kg CO₂ per km (short haul)
KWH_PER_1M_TOKENS    = 2.5     # estimated kWh per 1M tokens (mid-range)

# Score name written to Langfuse
CARBON_SCORE_NAME = "carbon_kg"

# Batch size for score ingestion
BATCH_SIZE = 50

# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} [{level:5s}] {msg}", flush=True)

def info(msg: str)  -> None: log(msg, "INFO")
def warn(msg: str)  -> None: log(msg, "WARN")
def error(msg: str) -> None: log(msg, "ERROR")

# ── API helpers ───────────────────────────────────────────────────────────────
def _auth() -> str:
    raw = f"{LANGFUSE_PUBLIC_KEY}:{LANGFUSE_SECRET_KEY}"
    return "Basic " + base64.b64encode(raw.encode()).decode()

def api_get(path: str, retries: int = 3) -> Dict:
    url = f"{LANGFUSE_BASE_URL}{path}"
    req = urllib.request.Request(url, headers={"Authorization": _auth()})
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            error(f"GET {path} → HTTP {e.code} (attempt {attempt})")
            if e.code in (401, 403, 404):
                return {}
            time.sleep(attempt * 1.5)
        except Exception as e:
            error(f"GET {path} → {e} (attempt {attempt})")
            time.sleep(attempt * 1.5)
    return {}

def api_post(path: str, payload: Dict, retries: int = 3) -> Dict:
    url  = f"{LANGFUSE_BASE_URL}{path}"
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        url, data=data,
        headers={"Authorization": _auth(), "Content-Type": "application/json"},
        method="POST"
    )
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            error(f"POST {path} → HTTP {e.code}: {body[:200]} (attempt {attempt})")
            if e.code in (401, 403):
                return {}
            time.sleep(attempt * 1.5)
        except Exception as e:
            error(f"POST {path} → {e} (attempt {attempt})")
            time.sleep(attempt * 1.5)
    return {}

# ── CO₂ calculation ───────────────────────────────────────────────────────────
def get_co2_factor(model: str) -> float:
    """Return kg CO₂ per 1M tokens for a given model name."""
    if not model:
        return CO2_PER_1M_TOKENS["default"]
    model_lower = model.lower().strip()
    # Exact match first
    if model_lower in CO2_PER_1M_TOKENS:
        return CO2_PER_1M_TOKENS[model_lower]
    # Prefix match
    for key, factor in CO2_PER_1M_TOKENS.items():
        if key != "default" and model_lower.startswith(key):
            return factor
    # Substring match
    for key, factor in CO2_PER_1M_TOKENS.items():
        if key != "default" and key in model_lower:
            return factor
    return CO2_PER_1M_TOKENS["default"]

def tokens_to_co2(total_tokens: int, model: str) -> float:
    """Convert token count to kg CO₂."""
    if total_tokens <= 0:
        return 0.0
    factor = get_co2_factor(model)
    return (total_tokens / 1_000_000) * factor

def tokens_to_kwh(total_tokens: int) -> float:
    """Estimate kWh from token count."""
    return (total_tokens / 1_000_000) * KWH_PER_1M_TOKENS

# ── Data structures ───────────────────────────────────────────────────────────
class ObservationRecord:
    """Holds all carbon-relevant data for one GENERATION observation."""
    def __init__(self, obs: Dict, trace: Optional[Dict] = None):
        self.obs_id       = obs.get("id", "")
        self.trace_id     = obs.get("traceId", "")
        self.model        = obs.get("model") or obs.get("name") or ""
        self.timestamp    = obs.get("startTime") or obs.get("createdAt") or ""
        self.user_id      = ""
        self.session_id   = ""

        # Token counts — try multiple field paths for robustness
        usage         = obs.get("usage") or {}
        usage_details = obs.get("usageDetails") or {}
        self.input_tokens  = (
            obs.get("promptTokens") or
            usage.get("input") or
            usage_details.get("input") or 0
        )
        self.output_tokens = (
            obs.get("completionTokens") or
            usage.get("output") or
            usage_details.get("output") or 0
        )
        self.total_tokens = (
            obs.get("totalTokens") or
            usage.get("total") or
            usage_details.get("total") or
            self.input_tokens + self.output_tokens
        )

        # Cost from Langfuse (may be underestimated if output=null)
        cost_details = obs.get("costDetails") or {}
        self.langfuse_cost = (
            obs.get("calculatedTotalCost") or
            cost_details.get("total") or 0.0
        )
        try:
            self.langfuse_cost = float(self.langfuse_cost)
        except (TypeError, ValueError):
            self.langfuse_cost = 0.0

        # CO₂ calculation
        self.co2_kg = tokens_to_co2(self.total_tokens, self.model)
        self.kwh    = tokens_to_kwh(self.total_tokens)

        # Enrich from trace if available
        if trace:
            self.user_id    = trace.get("userId") or ""
            self.session_id = trace.get("sessionId") or ""

    def month_key(self) -> str:
        """Return YYYY-MM for grouping."""
        if self.timestamp and len(self.timestamp) >= 7:
            return self.timestamp[:7]
        return "unknown"

    def date_key(self) -> str:
        """Return YYYY-MM-DD for grouping."""
        if self.timestamp and len(self.timestamp) >= 10:
            return self.timestamp[:10]
        return "unknown"

# ── Data fetching ─────────────────────────────────────────────────────────────
def fetch_observations(hours: int, user_filter: str = "") -> List[ObservationRecord]:
    """
    Fetch all GENERATION observations in the time window.
    Enriches each with trace-level user/session data.
    """
    since = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    info(f"Fetching GENERATION observations since {since} ...")

    # Build trace cache to avoid repeated API calls
    trace_cache: Dict[str, Dict] = {}
    all_records: List[ObservationRecord] = []
    page = 1

    while True:
        path = (
            f"/api/public/observations"
            f"?type=GENERATION&limit=50&page={page}"
            f"&fromStartTime={since}"
        )
        data = api_get(path)
        if not data:
            warn("Empty response from observations API — stopping.")
            break

        batch = data.get("data", [])
        if not batch:
            break

        meta        = data.get("meta", {})
        total_pages = meta.get("totalPages", 1)
        total_items = meta.get("totalItems", "?")
        info(f"  Page {page}/{total_pages} — {len(batch)} observations "
             f"(total so far: {len(all_records)+len(batch)} / {total_items})")

        for obs in batch:
            trace_id = obs.get("traceId", "")
            # Fetch trace once and cache
            if trace_id and trace_id not in trace_cache:
                trace_data = api_get(f"/api/public/traces/{trace_id}")
                trace_cache[trace_id] = trace_data if trace_data else {}
                time.sleep(0.05)

            trace = trace_cache.get(trace_id)
            record = ObservationRecord(obs, trace)

            # Apply user filter if specified
            if user_filter and record.user_id.lower() != user_filter.lower():
                continue

            # Only include observations with actual token usage
            if record.total_tokens > 0:
                all_records.append(record)

        if page >= total_pages:
            break
        page += 1
        time.sleep(0.15)

    info(f"Found {len(all_records)} observations with token usage.")
    return all_records

def check_existing_carbon_scores(obs_ids: List[str]) -> set:
    """
    Return set of observation IDs that already have a carbon_kg score.
    Prevents duplicate scoring.
    """
    existing: set = set()
    page = 1
    while True:
        data = api_get(
            f"/api/public/scores?name={CARBON_SCORE_NAME}&limit=50&page={page}"
        )
        if not data:
            break
        batch = data.get("data", [])
        if not batch:
            break
        for score in batch:
            oid = score.get("observationId") or score.get("traceId") or ""
            if oid:
                existing.add(oid)
        meta = data.get("meta", {})
        if page >= meta.get("totalPages", 1):
            break
        page += 1
        time.sleep(0.1)
    return existing

# ── Score pushing ─────────────────────────────────────────────────────────────
def push_carbon_scores(
    records: List[ObservationRecord],
    dry_run: bool = False,
) -> Tuple[int, int]:
    """
    Write carbon_kg as a numeric score on each observation in Langfuse.
    Skips observations that already have a carbon score.
    Returns (success_count, error_count).
    """
    if not records:
        return 0, 0

    info(f"Checking for existing {CARBON_SCORE_NAME} scores ...")
    obs_ids  = [r.obs_id for r in records if r.obs_id]
    existing = check_existing_carbon_scores(obs_ids)
    info(f"  Already scored: {len(existing)}  To score: {len(records) - len(existing)}")

    to_score = [r for r in records if r.obs_id not in existing]
    if not to_score:
        info("All observations already have carbon scores — nothing to push.")
        return 0, 0

    success_count = 0
    error_count   = 0
    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for i in range(0, len(to_score), BATCH_SIZE):
        chunk = to_score[i : i + BATCH_SIZE]
        batch_items = []

        for r in chunk:
            if not r.trace_id:
                continue
            score_item = {
                "id":            str(uuid.uuid4()),
                "traceId":       r.trace_id,
                "observationId": r.obs_id or None,
                "name":          CARBON_SCORE_NAME,
                "value":         round(r.co2_kg, 8),
                "dataType":      "NUMERIC",
                "comment":       (
                    f"model={r.model} "
                    f"tokens={r.total_tokens} "
                    f"factor={get_co2_factor(r.model):.3f}kg/1Mtok"
                ),
            }
            batch_items.append(score_item)

            if dry_run:
                info(f"  [DRY-RUN] obs={r.obs_id[:14]}...  "
                     f"co2={r.co2_kg:.6f}kg  "
                     f"tokens={r.total_tokens}  "
                     f"model={r.model}")

        if dry_run:
            success_count += len(batch_items)
            continue

        # Langfuse scores API accepts one score at a time — loop
        for score_item in batch_items:
            resp = api_post("/api/public/scores", score_item)
            if resp and resp.get("id"):
                success_count += 1
            else:
                error_count += 1
            time.sleep(0.05)

    return success_count, error_count

# ── Aggregation ───────────────────────────────────────────────────────────────
def aggregate(records: List[ObservationRecord]) -> Dict:
    """Aggregate all carbon metrics from observation records."""
    agg = {
        "total_co2_kg":    0.0,
        "total_kwh":       0.0,
        "total_tokens":    0,
        "total_input":     0,
        "total_output":    0,
        "total_cost_usd":  0.0,
        "obs_count":       len(records),
        "by_model":        defaultdict(lambda: {"co2_kg": 0.0, "tokens": 0, "count": 0}),
        "by_user":         defaultdict(lambda: {"co2_kg": 0.0, "tokens": 0, "count": 0}),
        "by_month":        defaultdict(lambda: {"co2_kg": 0.0, "tokens": 0, "count": 0}),
        "by_day":          defaultdict(lambda: {"co2_kg": 0.0, "tokens": 0}),
    }

    for r in records:
        agg["total_co2_kg"]   += r.co2_kg
        agg["total_kwh"]      += r.kwh
        agg["total_tokens"]   += r.total_tokens
        agg["total_input"]    += r.input_tokens
        agg["total_output"]   += r.output_tokens
        agg["total_cost_usd"] += r.langfuse_cost

        model = r.model or "unknown"
        agg["by_model"][model]["co2_kg"]  += r.co2_kg
        agg["by_model"][model]["tokens"]  += r.total_tokens
        agg["by_model"][model]["count"]   += 1

        user = r.user_id or "unknown"
        agg["by_user"][user]["co2_kg"]    += r.co2_kg
        agg["by_user"][user]["tokens"]    += r.total_tokens
        agg["by_user"][user]["count"]     += 1

        month = r.month_key()
        agg["by_month"][month]["co2_kg"]  += r.co2_kg
        agg["by_month"][month]["tokens"]  += r.total_tokens
        agg["by_month"][month]["count"]   += 1

        day = r.date_key()
        agg["by_day"][day]["co2_kg"]      += r.co2_kg
        agg["by_day"][day]["tokens"]      += r.total_tokens

    return agg

# ── Report ────────────────────────────────────────────────────────────────────
def format_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

def bar(value: float, max_value: float, width: int = 30) -> str:
    if max_value <= 0:
        return ""
    filled = int((value / max_value) * width)
    return "█" * filled + "░" * (width - filled)

def print_report(agg: Dict, hours: int, user_filter: str = "") -> None:
    total_co2   = agg["total_co2_kg"]
    total_tok   = agg["total_tokens"]
    total_kwh   = agg["total_kwh"]
    total_cost  = agg["total_cost_usd"]
    obs_count   = agg["obs_count"]

    # Equivalences
    car_km      = total_co2 / KG_CO2_PER_KM_CAR if KG_CO2_PER_KM_CAR else 0
    flight_km   = total_co2 / KG_CO2_PER_FLIGHT_KM if KG_CO2_PER_FLIGHT_KM else 0

    # Window label
    if hours >= 8760:
        window = f"Last {hours//8760} year(s)"
    elif hours >= 720:
        window = f"Last {hours//720} month(s)"
    elif hours >= 24:
        window = f"Last {hours//24} day(s)"
    else:
        window = f"Last {hours} hour(s)"

    user_label = f" — User: {user_filter}" if user_filter else " — All Users"

    print()
    print("╔" + "═" * 65 + "╗")
    print(f"║  🌱 Carbon Footprint Report{user_label:<25}║")
    print(f"║  {window:<63}║")
    print("╠" + "═" * 65 + "╣")
    print(f"║                                                                 ║")
    co2_str = f"{total_co2:.2f} kg CO₂"
    print(f"║   {co2_str:<62}║")
    print(f"║   {format_tokens(total_tok)} tokens consumed across {obs_count} LLM calls{' '*15}║")
    print(f"║                                                                 ║")
    print("╠" + "═" * 65 + "╣")

    # Equivalences
    print(f"║  Equivalences:                                                  ║")
    print(f"║   🚗  {car_km:>8.1f} km driven (petrol car)                          ║")
    print(f"║   ✈️   {flight_km:>8.1f} km flown (economy)                           ║")
    print(f"║   ⚡  {total_kwh:>8.2f} kWh estimated energy                         ║")
    print(f"║   💰  ${total_cost:>8.4f} API cost (Langfuse estimate)               ║")
    print("╠" + "═" * 65 + "╣")

    # Monthly breakdown
    by_month = agg["by_month"]
    if by_month:
        print(f"║  Monthly Breakdown:                                             ║")
        max_co2 = max(v["co2_kg"] for v in by_month.values()) if by_month else 1
        for month in sorted(by_month.keys()):
            v      = by_month[month]
            b      = bar(v["co2_kg"], max_co2, 28)
            kg_str = f"{v['co2_kg']:.2f} kg"
            print(f"║   {month}  {b}  {kg_str:>10}  ║")
        print("║                                                                 ║")

    # Model breakdown
    by_model = agg["by_model"]
    if by_model:
        print("╠" + "═" * 65 + "╣")
        print(f"║  By Model:                                                      ║")
        max_co2 = max(v["co2_kg"] for v in by_model.values()) if by_model else 1
        for model, v in sorted(by_model.items(), key=lambda x: -x[1]["co2_kg"]):
            short = model[:22]
            b     = bar(v["co2_kg"], max_co2, 18)
            kg_s  = f"{v['co2_kg']:.3f}kg"
            tok_s = format_tokens(v["tokens"])
            print(f"║   {short:<22}  {b}  {kg_s:>9}  {tok_s:>7} tok  ║")

    # User breakdown (only if multi-user)
    by_user = agg["by_user"]
    if by_user and len(by_user) > 1:
        print("╠" + "═" * 65 + "╣")
        print(f"║  By User:                                                       ║")
        max_co2 = max(v["co2_kg"] for v in by_user.values()) if by_user else 1
        for user, v in sorted(by_user.items(), key=lambda x: -x[1]["co2_kg"]):
            short = user[:22]
            b     = bar(v["co2_kg"], max_co2, 18)
            kg_s  = f"{v['co2_kg']:.3f}kg"
            tok_s = format_tokens(v["tokens"])
            print(f"║   {short:<22}  {b}  {kg_s:>9}  {tok_s:>7} tok  ║")

    print("╠" + "═" * 65 + "╣")
    print(f"║  CO₂ factors used: Haiku≈0.27  Sonnet≈0.60  Opus≈1.15         ║")
    print(f"║  kg CO₂ per 1M tokens (estimated, not audited for ESG)         ║")
    print("╚" + "═" * 65 + "╝")
    print()

# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Carbon Footprint Tracker via Langfuse"
    )
    parser.add_argument(
        "--hours", type=int, default=720,
        help="Look-back window in hours (default: 720 = 30 days)"
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Print carbon footprint report to terminal"
    )
    parser.add_argument(
        "--push-scores", action="store_true",
        help="Write carbon_kg numeric scores to Langfuse for dashboard use"
    )
    parser.add_argument(
        "--user", type=str, default="",
        help="Filter by specific userId (e.g. vishvendra.singh)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview score pushes without writing to Langfuse"
    )
    args = parser.parse_args()

    # Default to report if no mode specified
    if not args.report and not args.push_scores:
        args.report = True

    print()
    info("=" * 65)
    info("langfuse_carbon.py — Carbon Footprint Tracker")
    info(f"Base URL  : {LANGFUSE_BASE_URL}")
    info(f"Window    : Last {args.hours}h")
    info(f"User      : {args.user or 'all'}")
    info(f"Mode      : {'report' if args.report else ''}"
         f"{'  push-scores' if args.push_scores else ''}"
         f"{'  [DRY RUN]' if args.dry_run else ''}")
    info("=" * 65)

    # Fetch observations
    records = fetch_observations(args.hours, user_filter=args.user)

    if not records:
        info("No token-bearing observations found in window.")
        info("Tips:")
        info("  - Widen the window: --hours 720")
        info("  - Check LANGFUSE_BASE_URL / credentials")
        info("  - Run langfuse_classify.py first to confirm traces exist")
        return

    # Aggregate
    agg = aggregate(records)

    # Report mode
    if args.report:
        print_report(agg, args.hours, user_filter=args.user)

    # Push scores mode
    if args.push_scores:
        info(f"Pushing {CARBON_SCORE_NAME} scores to Langfuse ...")
        ok, err = push_carbon_scores(records, dry_run=args.dry_run)
        info(f"Scores pushed — successes: {ok}  errors: {err}")
        if not args.dry_run and ok > 0:
            info(f"View in Langfuse: {LANGFUSE_BASE_URL}/project/claude-code-project/scores")
            info(f"Add dashboard widget: View=Scores  Metric=Average  Name={CARBON_SCORE_NAME}")

if __name__ == "__main__":
    main()