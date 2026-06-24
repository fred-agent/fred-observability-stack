#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║          Langfuse Prompt Classification Script v2.0                         ║
║          Classifies traces with category + type tags                         ║
║                                                                              ║
║  Tags applied per trace (Approach A — same trace, multiple tags):            ║
║    category:*          → WHAT topic (code, code-review, debugging, chat.)    ║
║    type:user-prompt    → WHO sent it (human typed this)                      ║
║    type:agent-internal → WHO sent it (agent/system injected this)            ║
║    type:llm-response   → WHAT came back (LLM actually responded)             ║
║                                                                              ║
║  Usage:                                                                      ║
║    python3 langfuse_prompt_classify.py                    # classify last 1h        ║
║    python3 langfuse_prompt_classify.py --hours 24         # classify last 24h       ║
║    python3 langfuse_prompt_classify.py --dry-run          # preview, no updates     ║
║    python3 langfuse_prompt_classify.py --report           # summary report only     ║
║    python3 langfuse_prompt_classify.py --hours 72 --dry-run                         ║
║                                                                              ║
║  Cron (every hour):                                                          ║
║    0 * * * * cd /path && python3 langfuse_prompt_classify.py >> classify.log 2>&1   ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import argparse
import base64
import json
import os
import re
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

# Batch size for ingestion API (max 50 per Langfuse docs)
BATCH_SIZE = 50

# ── User-prompt detection thresholds ─────────────────────────────────────────
# A trace is considered a real user prompt if ALL of these pass:
USER_PROMPT_MAX_CHARS    = 1000   # cleaned text must be under this length
USER_PROMPT_MAX_TOKENS   = 300    # promptTokens must be under this (0 = skip check)
USER_PROMPT_MAX_MESSAGES = 3      # input.messages list must have <= this many items

# ── Agent-injected content patterns ──────────────────────────────────────────
# If ANY of these patterns appear in the raw input text, it is agent-internal.
AGENT_PATTERNS: List[str] = [
    r"<system-reminder>",
    r"<command-name>",
    r"<command-message>",
    r"<command-args>",
    r"<local-command-stdout>",
    r"<local-command-stderr>",
    r"<local-command-exitcode>",
    r"^#\s+(System|Environment|Doing tasks|Using your tools|"
    r"Tone and style|Text output|Context management|"
    r"Executing actions|Session-specific guidance|auto memory)",
    r"^You are an interactive agent",
    r"^You are a helpful assistant",
    r"The following skills are available",
    r"IMPORTANT:.*context may or may not be relevant",
    r"<SYSTEM>",
    r"\[INST\]",
]
_AGENT_PATTERN_RE = re.compile(
    "|".join(AGENT_PATTERNS),
    re.IGNORECASE | re.MULTILINE
)

# ── XML / markup stripping (for text extraction only) ─────────────────────────
_STRIP_XML_RE = re.compile(
    r"<(system-reminder|command-name|command-message|command-args|"
    r"local-command-stdout|local-command-stderr|local-command-exitcode|SYSTEM)"
    r"[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE
)
_STRIP_TAGS_RE = re.compile(r"<[^>]+>")

# ── LLM response detection ────────────────────────────────────────────────────
# A trace has a real LLM response if ANY of these are true:
#   - trace.output is non-null and non-empty
#   - observation completionTokens > 0
#   - observation output is non-null

# ── Categories — ordered by priority (first match wins) ──────────────────────
CATEGORIES: List[Tuple[str, List[str]]] = [
    ("category:security", [
        "vulnerability", "vulnerabilities", "cve", "exploit", "injection",
        "xss", "sql injection", "auth", "authentication", "authorization",
        "oauth", "jwt", "token", "permission", "rbac", "acl", "firewall",
        "pentest", "penetration", "security scan", "sast", "dast", "owasp",
        "encrypt", "decrypt", "hash", "ssl", "tls", "certificate", "secret",
        "credential", "password", "api key", "access key",
    ]),
    ("category:infrastructure", [
        "terraform", "terragrunt", "pulumi", "cdk", "cloudformation",
        "kubernetes", "k8s", "kubectl", "helm", "kustomize", "argocd",
        "gcp", "gke", "aws", "azure", "eks", "aks", "vpc", "subnet",
        "load balancer", "ingress", "egress", "networking", "dns",
        "docker", "dockerfile", "container", "pod", "deployment", "service",
        "configmap", "namespace", "node pool", "cluster",
        "prometheus", "grafana", "loki", "otel", "opentelemetry", "monitoring",
        "vm", "virtual machine", "compute", "storage", "bucket", "blob",
    ]),
    ("category:cicd-devops", [
        "ci/cd", "cicd", "pipeline", "github action", "gitlab ci",
        "jenkins", "travis", "circle ci", "drone", "tekton", "argo workflow",
        "deploy", "deployment", "release", "rollout", "rollback",
        "artifact", "registry", "docker hub", "publish",
        "webhook", "trigger", "workflow", "runner", "agent build",
        "npm run", "make build", "gradle", "maven",
    ]),
    ("category:debugging", [
        "fix", "bug", "error", "debug", "traceback", "exception",
        "stacktrace", "stack trace", "crash", "fail", "failing", "failed",
        "broken", "not working", "issue", "problem", "why is", "why does",
        "wrong output", "incorrect", "unexpected", "investigate",
        "root cause", "rca", "diagnose", "troubleshoot", "not running",
        "throws", "raises", "segfault",
    ]),
    ("category:testing", [
        "unit test", "unittest", "pytest", "jest", "mocha", "cypress",
        "test case", "test suite", "mock", "stub", "fixture", "assert",
        "coverage", "tdd", "bdd", "integration test", "end to end", "e2e",
        "load test", "performance test", "regression", "test data",
        "write test", "add test", "spec file", "vitest", "jasmine",
    ]),
    ("category:code-review", [
        "review", "code review", "refactor", "refactoring", "clean up",
        "cleanup", "improve", "optimise", "optimize", "best practice",
        "suggestions", "feedback", "lint", "linting", "smell", "anti-pattern",
        "simplify", "readable", "maintainable", "check this", "look at this",
        "what do you think", "is this correct", "is this good", "better way",
    ]),
    ("category:requirements-analysis", [
        "requirement", "requirements", "user story", "use case", "acceptance",
        "functional", "non-functional", "stakeholder", "specification",
        "design", "architecture", "propose", "suggest approach",
        "how should i", "what approach", "which pattern", "which design",
        "analyse", "analyze", "breakdown", "scope", "feasibility",
    ]),
    ("category:documentation", [
        "document", "documentation", "readme", "wiki", "confluence",
        "comment", "docstring", "jsdoc", "javadoc", "explain",
        "summarize", "summary", "describe", "what does", "how does",
        "write docs", "add comments", "annotate", "changelog", "release note",
    ]),
    ("category:data-sql", [
        "sql", "query", "select", "insert", "update", "delete", "join",
        "database", "postgres", "mysql", "sqlite", "mongodb", "redis",
        "elasticsearch", "bigquery", "snowflake", "dbt", "dataflow",
        "migration", "schema", "index", "table", "view", "stored procedure",
        "data pipeline", "etl", "elt", "spark", "pandas", "dataframe",
    ]),
    ("category:code-generation", [
        "write", "create", "generate", "implement", "build", "develop",
        "scaffold", "boilerplate", "template", "class", "function", "method",
        "module", "component", "api", "endpoint", "controller", "service",
        "repository", "model", "interface", "add feature",
        "new feature", "make a", "create a", "write a", "build a",
    ]),
    ("category:general-chat", [
        "hi", "hello", "hey", "thanks", "thank you", "help me",
        "what is", "how to", "can you", "please", "good morning",
        "good afternoon", "okay", "ok", "yes", "no", "sure", "prompt",
        "tell me", "show me", "give me",
    ]),
]

FALLBACK_CATEGORY = "category:uncategorised"
AGENT_CATEGORY    = "category:agentic-calls"

# ── Type tags ─────────────────────────────────────────────────────────────────
TYPE_USER_PROMPT    = "type:user-prompt"
TYPE_AGENT_INTERNAL = "type:agent-internal"
TYPE_LLM_RESPONSE   = "type:llm-response"

# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} [{level:5s}] {msg}", flush=True)

def info(msg: str)  -> None: log(msg, "INFO")
def warn(msg: str)  -> None: log(msg, "WARN")
def error(msg: str) -> None: log(msg, "ERROR")
def debug(msg: str) -> None: log(msg, "DEBUG")

# ── Langfuse API helpers ──────────────────────────────────────────────────────
def _auth_header() -> str:
    raw = f"{LANGFUSE_PUBLIC_KEY}:{LANGFUSE_SECRET_KEY}"
    return "Basic " + base64.b64encode(raw.encode()).decode()

def api_get(path: str, retries: int = 3) -> Dict:
    url = f"{LANGFUSE_BASE_URL}{path}"
    req = urllib.request.Request(url, headers={"Authorization": _auth_header()})
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            error(f"GET {path} → HTTP {e.code}: {e.reason} (attempt {attempt})")
            if e.code in (401, 403, 404):
                return {}   # no point retrying auth/not-found errors
            time.sleep(attempt * 1.0)
        except Exception as e:
            error(f"GET {path} → {e} (attempt {attempt})")
            time.sleep(attempt * 1.0)
    return {}

def api_post(path: str, payload: Dict, retries: int = 3) -> Dict:
    url  = f"{LANGFUSE_BASE_URL}{path}"
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        url, data=data,
        headers={
            "Authorization": _auth_header(),
            "Content-Type":  "application/json",
        },
        method="POST"
    )
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            error(f"POST {path} → HTTP {e.code}: {body[:300]} (attempt {attempt})")
            if e.code in (401, 403):
                return {}
            time.sleep(attempt * 1.0)
        except Exception as e:
            error(f"POST {path} → {e} (attempt {attempt})")
            time.sleep(attempt * 1.0)
    return {}

# ── Text extraction ───────────────────────────────────────────────────────────
def extract_raw_text(input_data: Any) -> str:
    """
    Extract raw user message text WITHOUT stripping agent patterns.
    Used for agent detection (we need to see the patterns).
    """
    if not input_data:
        return ""
    if isinstance(input_data, str):
        return input_data

    if isinstance(input_data, dict):
        # Check for "messages" key (list of message dicts)
        messages = input_data.get("messages", [])
        if messages:
            parts = []
            for msg in messages:
                role = msg.get("role", "")
                if role not in ("user", "human"):
                    continue
                content = msg.get("content", "")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(block.get("text", ""))
            if parts:
                return "\n".join(p for p in parts if p)

        # Check for single message: {role: "user", content: "..."}
        role = input_data.get("role", "")
        if role in ("user", "human", "assistant"):
            content = input_data.get("content", "")
            if isinstance(content, str):
                return content
            elif isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                if parts:
                    return "\n".join(p for p in parts if p)

        # Check for "prompt" key
        if "prompt" in input_data:
            return str(input_data["prompt"])

    return str(input_data)[:2000]

def extract_clean_text(raw_text: str) -> str:
    """
    Strip all agent-injected XML, system tags, and whitespace.
    Used for classification after type detection.
    """
    if not raw_text:
        return ""
    # Remove XML blocks entirely
    text = _STRIP_XML_RE.sub("", raw_text)
    # Remove remaining stray tags
    text = _STRIP_TAGS_RE.sub("", text)
    return text.strip()

def count_messages(input_data: Any) -> int:
    """Count how many messages are in the input."""
    if not isinstance(input_data, dict):
        return 0
    messages = input_data.get("messages", [])
    if not isinstance(messages, list):
        return 0
    return len(messages)

# ── Type detection ────────────────────────────────────────────────────────────
def detect_type(
    trace: Dict,
    raw_text: str,
    prompt_tokens: int,
) -> str:
    """
    Determine whether this trace is a real user prompt or agent-internal.

    Returns: TYPE_USER_PROMPT or TYPE_AGENT_INTERNAL
    """
    if not raw_text:
        return TYPE_AGENT_INTERNAL

    # Rule 1 — contains known agent-injected patterns → agent-internal
    if _AGENT_PATTERN_RE.search(raw_text):
        return TYPE_AGENT_INTERNAL

    # Rule 2 — too many messages in input → likely conversation replay → agent-internal
    msg_count = count_messages(trace.get("input"))
    if msg_count > USER_PROMPT_MAX_MESSAGES:
        return TYPE_AGENT_INTERNAL

    # Rule 3 — raw text too long → system prompt or context dump → agent-internal
    if len(raw_text) > USER_PROMPT_MAX_CHARS:
        return TYPE_AGENT_INTERNAL

    # Rule 4 — too many tokens → definitely injected context → agent-internal
    if USER_PROMPT_MAX_TOKENS > 0 and prompt_tokens > USER_PROMPT_MAX_TOKENS:
        return TYPE_AGENT_INTERNAL

    # Rule 5 — starts with markdown section header (# Word) → system prompt → agent-internal
    first_line = raw_text.strip().split("\n")[0].strip()
    if re.match(r"^#+\s+\w", first_line):
        return TYPE_AGENT_INTERNAL

    # Passed all checks → real user prompt
    return TYPE_USER_PROMPT

def detect_llm_response(trace: Dict, observation: Optional[Dict]) -> bool:
    """
    Returns True if this trace has a real LLM response.
    Checks both trace-level output and observation-level output/tokens.
    """
    # Check trace-level output
    trace_output = trace.get("output")
    if trace_output is not None and str(trace_output).strip():
        return True

    # Check observation-level (GENERATION)
    if observation:
        obs_output = observation.get("output")
        if obs_output is not None and str(obs_output).strip():
            return True
        completion_tokens = (
            observation.get("completionTokens") or
            (observation.get("usage") or {}).get("output", 0) or
            (observation.get("usageDetails") or {}).get("output", 0) or
            0
        )
        if completion_tokens > 0:
            return True

    return False

# ── Classifier ────────────────────────────────────────────────────────────────
def classify_category(clean_text: str) -> str:
    """
    Keyword-based category classifier.
    Only call this on clean_text (agent patterns already stripped).
    """
    if not clean_text or not clean_text.strip():
        return FALLBACK_CATEGORY

    lowered = clean_text.lower()

    for category_tag, keywords in CATEGORIES:
        for kw in keywords:
            if len(kw) <= 4:
                pattern = r'\b' + re.escape(kw) + r'\b'
                if re.search(pattern, lowered):
                    return category_tag
            else:
                if kw in lowered:
                    return category_tag

    return FALLBACK_CATEGORY

def classify_agent_internal(
    trace: Dict,
    observation: Optional[Dict],
    raw_text: str,
    trace_id: str = "",
) -> str:
    """
    Sub-classify agent-internal traces using alternative fields when available.
    Priority order: metadata.tags → tool_calls → messages → raw_text → span_name → fallback
    """
    classify_source = ""

    # 1. Check metadata.tags for "agentic" hint
    metadata = trace.get("metadata")
    if isinstance(metadata, dict):
        tags = metadata.get("tags", [])
        if isinstance(tags, list):
            for tag in tags:
                if isinstance(tag, str) and "agentic" in tag.lower():
                    classify_source = "metadata.tags:agentic"
                    return AGENT_CATEGORY

    # 2. Check observation for tool_calls (indicates agentic behavior)
    if observation:
        obs_input = observation.get("input")
        if isinstance(obs_input, dict):
            tool_calls = obs_input.get("tool_calls") or obs_input.get("toolCalls")
            if isinstance(tool_calls, list) and len(tool_calls) > 0:
                classify_source = "observation.tool_calls"
                return AGENT_CATEGORY

    # 3. Check for structured messages (conversation pattern)
    trace_input = trace.get("input")
    if isinstance(trace_input, dict) and "messages" in trace_input:
        messages = trace_input.get("messages", [])
        if isinstance(messages, list) and len(messages) > 0:
            # Extract message text and try to classify
            msg_text_parts = []
            for msg in messages:
                if isinstance(msg, dict):
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        msg_text_parts.append(content)
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                msg_text_parts.append(block.get("text", ""))
            if msg_text_parts:
                combined_text = "\n".join(msg_text_parts)
                if combined_text.strip():
                    category = classify_category(extract_clean_text(combined_text))
                    if category != FALLBACK_CATEGORY:
                        classify_source = "messages.content"
                        debug(f"  {trace_id[:14]}...  classify_source={classify_source:30s}  "
                              f"category={category.replace('category:', ''):20s}")
                        return category

    # 4. Try raw_text (primary input field)
    if raw_text and len(raw_text) > 2:
        category = classify_category(extract_clean_text(raw_text))
        if category != FALLBACK_CATEGORY:
            classify_source = "input.text"
            debug(f"  {trace_id[:14]}...  classify_source={classify_source:30s}  "
                  f"category={category.replace('category:', ''):20s}")
            return category

    # 5. Try span name (trace.name field)
    span_name = trace.get("name", "")
    if span_name and isinstance(span_name, str) and len(span_name) > 2:
        category = classify_category(span_name)
        if category != FALLBACK_CATEGORY:
            classify_source = "span.name"
            debug(f"  {trace_id[:14]}...  classify_source={classify_source:30s}  "
                  f"category={category.replace('category:', ''):20s}")
            return category

    # 6. Fallback: preserve default agentic-calls behavior
    classify_source = "fallback"
    available_fields = []
    if trace.get("input"):
        available_fields.append("input")
    if trace.get("output"):
        available_fields.append("output")
    if trace.get("name"):
        available_fields.append("name")
    if observation:
        available_fields.append("observation")
    debug(f"  {trace_id[:14]}...  classify_source={classify_source:30s}  "
          f"category={'agentic-calls':20s}  available_fields={available_fields}")
    return AGENT_CATEGORY

# ── Already-tagged checks ─────────────────────────────────────────────────────
def has_category_tag(tags: List[str]) -> bool:
    return any(t.startswith("category:") for t in (tags or []))

def has_type_tag(tags: List[str]) -> bool:
    return any(t.startswith("type:") for t in (tags or []))

def is_fully_classified(tags: List[str]) -> bool:
    """Trace is fully classified if it has BOTH a category: and type: tag."""
    return has_category_tag(tags) and has_type_tag(tags)

# ── Observation fetcher ───────────────────────────────────────────────────────
def fetch_first_generation(trace_id: str) -> Optional[Dict]:
    """
    Fetch the first GENERATION observation for a trace.
    Used to get token counts and output for type detection.
    Returns None if not found or on error.
    """
    data = api_get(
        f"/api/public/observations?traceId={trace_id}"
        f"&type=GENERATION&limit=1"
    )
    observations = data.get("data", [])
    return observations[0] if observations else None

# ── Trace fetching ────────────────────────────────────────────────────────────
def fetch_traces(hours: int) -> List[Dict]:
    """Fetch all traces in the time window, handling pagination."""
    since = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    all_traces: List[Dict] = []
    page = 1

    info(f"Fetching traces since {since} ...")

    while True:
        data = api_get(
            f"/api/public/traces?limit=50&page={page}&fromTimestamp={since}"
        )
        if not data:
            warn("Empty response from traces API — stopping pagination.")
            break

        batch = data.get("data", [])
        if not batch:
            break

        all_traces.extend(batch)

        meta        = data.get("meta", {})
        total_pages = meta.get("totalPages", 1)
        total_items = meta.get("totalItems", len(all_traces))
        info(f"  Page {page}/{total_pages} — fetched {len(batch)} traces "
             f"(total so far: {len(all_traces)} / {total_items})")

        if page >= total_pages:
            break
        page += 1
        time.sleep(0.15)   # gentle rate limiting

    return all_traces

# ── Ingestion ─────────────────────────────────────────────────────────────────
def push_tag_updates(
    updates: List[Dict],
    dry_run: bool = False,
) -> Tuple[int, int]:
    """
    Send tag updates to Langfuse via batch ingestion API.
    Each update dict: {trace_id, existing_tags, new_tags_to_add}
    Returns (success_count, error_count).
    """
    if not updates:
        return 0, 0

    success_count = 0
    error_count   = 0
    ts_now        = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for i in range(0, len(updates), BATCH_SIZE):
        chunk = updates[i : i + BATCH_SIZE]
        batch_items = []

        for u in chunk:
            # Merge: existing tags + new tags, remove duplicates, preserve order
            merged = list(dict.fromkeys(u["existing_tags"] + u["new_tags_to_add"]))
            batch_items.append({
                "type":      "trace-create",
                "id":        str(uuid.uuid4()),
                "timestamp": ts_now,
                "body": {
                    "id":   u["trace_id"],
                    "tags": merged,
                }
            })

        if dry_run:
            for item in batch_items:
                info(f"  [DRY-RUN] {item['body']['id'][:16]}... "
                     f"→ {item['body']['tags']}")
            success_count += len(chunk)
            continue

        resp      = api_post("/api/public/ingestion", {"batch": batch_items})
        successes = resp.get("successes", [])
        errors    = resp.get("errors",    [])
        success_count += len(successes)
        error_count   += len(errors)

        for e in errors:
            error(f"  Ingestion error: {e}")

        time.sleep(0.2)

    return success_count, error_count

# ── Report ────────────────────────────────────────────────────────────────────
def print_report(
    cat_stats: Dict[str, int],
    type_stats: Dict[str, int],
    hours: int,
) -> None:
    print()
    print("=" * 65)
    print(f"  PROMPT CLASSIFICATION REPORT — Last {hours}h")
    print("=" * 65)

    # Type breakdown
    total_typed = sum(type_stats.values())
    if total_typed:
        print(f"\n  {'── Type Breakdown':-<52}")
        for t, count in sorted(type_stats.items(), key=lambda x: -x[1]):
            pct  = count / total_typed * 100
            bar  = "█" * min(count, 25)
            name = t.replace("type:", "")
            print(f"  {name:<30} {count:>5}  {pct:>5.1f}%  {bar}")

    # Category breakdown
    total_cats = sum(cat_stats.values())
    if total_cats:
        print(f"\n  {'── Category Breakdown':-<52}")
        for cat, count in sorted(cat_stats.items(), key=lambda x: -x[1]):
            pct  = count / total_cats * 100
            bar  = "█" * min(count, 25)
            name = cat.replace("category:", "")
            print(f"  {name:<30} {count:>5}  {pct:>5.1f}%  {bar}")
        print(f"\n  {'TOTAL':<30} {total_cats:>5}")

    print("=" * 65)
    print()

# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Classify Langfuse traces with category + type tags"
    )
    parser.add_argument("--hours",   type=int,  default=1,
                        help="Look-back window in hours (default: 1)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview classifications without updating Langfuse")
    parser.add_argument("--report",  action="store_true",
                        help="Show category/type breakdown of existing tags only")
    args = parser.parse_args()

    print()
    info("=" * 65)
    info("Langfuse Prompt Classifier v2.0")
    info(f"Base URL    : {LANGFUSE_BASE_URL}")
    info(f"Window      : Last {args.hours} hour(s)")
    info(f"Dry run     : {args.dry_run}")
    info(f"Categories  : {len(CATEGORIES)} defined")
    info(f"Type tags   : {TYPE_USER_PROMPT} | {TYPE_AGENT_INTERNAL} | {TYPE_LLM_RESPONSE}")
    info(f"Agent cat   : {AGENT_CATEGORY} (dedicated widget for agentic traces)")
    info("=" * 65)

    traces = fetch_traces(args.hours)
    if not traces:
        info("No traces found in window — nothing to classify.")
        return
    info(f"Found {len(traces)} traces total.")

    # ── Report mode ──────────────────────────────────────────────────────────
    if args.report:
        cat_stats:  Dict[str, int] = defaultdict(int)
        type_stats: Dict[str, int] = defaultdict(int)
        for t in traces:
            for tag in (t.get("tags") or []):
                if tag.startswith("category:"):
                    cat_stats[tag] += 1
                elif tag.startswith("type:"):
                    type_stats[tag] += 1
        print_report(dict(cat_stats), dict(type_stats), args.hours)
        return

    # ── Classification pass ───────────────────────────────────────────────────
    skipped     = 0
    to_update:  List[Dict] = []
    cat_stats:  Dict[str, int] = defaultdict(int)
    type_stats: Dict[str, int] = defaultdict(int)

    for trace in traces:
        trace_id      = trace["id"]
        existing_tags = list(trace.get("tags") or [])

        # Skip only if BOTH category and type tags already present
        if is_fully_classified(existing_tags):
            skipped += 1
            for tag in existing_tags:
                if tag.startswith("category:"): cat_stats[tag]  += 1
                elif tag.startswith("type:"):   type_stats[tag] += 1
            debug(f"  SKIP {trace_id[:14]}... (already fully classified)")
            continue

        # ── Step 1: Extract raw text for agent detection ──────────────────
        raw_text = extract_raw_text(trace.get("input"))

        # Fallback: if trace has no direct input, fetch first GENERATION obs
        observation: Optional[Dict] = None
        if not raw_text or len(raw_text) < 3:
            observation = fetch_first_generation(trace_id)
            if observation:
                raw_text = extract_raw_text(observation.get("input"))
            time.sleep(0.05)

        # ── Step 2: Get token counts (for type detection) ─────────────────
        prompt_tokens = (
            trace.get("promptTokens") or
            trace.get("usage", {}).get("input", 0) or
            0
        )
        # If not on trace, try to get from observation
        if not prompt_tokens and observation:
            prompt_tokens = (
                observation.get("promptTokens") or
                (observation.get("usage") or {}).get("input", 0) or
                (observation.get("usageDetails") or {}).get("input", 0) or
                0
            )

        # ── Step 3: Detect type tag ───────────────────────────────────────
        # Only add type tag if not already present
        new_tags: List[str] = []

        if not has_type_tag(existing_tags):
            type_tag = detect_type(trace, raw_text, prompt_tokens)
            new_tags.append(type_tag)
            type_stats[type_tag] += 1
        else:
            # Already has type tag — count it for report
            for tag in existing_tags:
                if tag.startswith("type:"):
                    type_stats[tag] += 1

        # ── Step 4: Detect LLM response tag ──────────────────────────────
        if TYPE_LLM_RESPONSE not in existing_tags:
            # Only fetch observation if we haven't already
            if observation is None:
                observation = fetch_first_generation(trace_id)
                time.sleep(0.05)
            if detect_llm_response(trace, observation):
                new_tags.append(TYPE_LLM_RESPONSE)
                type_stats[TYPE_LLM_RESPONSE] += 1

        # ── Step 5: Classify category (only for user prompts) ────────────
        if not has_category_tag(existing_tags):
            # Determine the type for category decision
            current_type = next(
                (t for t in existing_tags + new_tags if t.startswith("type:")),
                TYPE_AGENT_INTERNAL
            )

            if current_type == TYPE_USER_PROMPT:
                # Classify based on CLEAN text (strip agent markup)
                clean_text = extract_clean_text(raw_text)
                category   = classify_category(clean_text)
            else:
                # Agent-internal — try to sub-classify via alternative fields
                category = classify_agent_internal(trace, observation, raw_text, trace_id)

            new_tags.append(category)
            cat_stats[category] += 1
        else:
            for tag in existing_tags:
                if tag.startswith("category:"):
                    cat_stats[tag] += 1

        # ── Step 6: Queue for update if we have anything new ──────────────
        if new_tags:
            # Determine primary type for logging
            primary_type = next(
                (t for t in new_tags if t.startswith("type:")), "")
            primary_cat  = next(
                (t for t in new_tags if t.startswith("category:")), "")

            debug(f"  {trace_id[:14]}...  "
                  f"type={primary_type.replace('type:',''):20s}  "
                  f"cat={primary_cat.replace('category:',''):20s}  "
                  f"prompt='{(extract_clean_text(raw_text) or raw_text)[:40]}'")

            to_update.append({
                "trace_id":      trace_id,
                "existing_tags": existing_tags,
                "new_tags_to_add": new_tags,
            })

    info(f"Skipped (already fully classified): {skipped}")
    info(f"To update: {len(to_update)}")

    if not to_update:
        info("All traces already classified — nothing to update.")
        print_report(dict(cat_stats), dict(type_stats), args.hours)
        return

    # ── Preview table ─────────────────────────────────────────────────────────
    print()
    print("  " + "-" * 80)
    print(f"  {'Trace ID':16s}  {'Type':22s}  {'Category':25s}  New Tags")
    print("  " + "-" * 80)
    for u in to_update:
        tid   = u["trace_id"][:14] + "..."
        types = [t for t in u["new_tags_to_add"] if t.startswith("type:")]
        cats  = [t for t in u["new_tags_to_add"] if t.startswith("category:")]
        tp    = (types[0] if types else "").replace("type:", "")
        cat   = (cats[0]  if cats  else "").replace("category:", "")
        tags  = ", ".join(u["new_tags_to_add"])
        print(f"  {tid:16s}  {tp:22s}  {cat:25s}  {tags}")
    print("  " + "-" * 80)
    print()

    # ── Push updates ──────────────────────────────────────────────────────────
    info(f"Sending {len(to_update)} tag updates to Langfuse "
         f"{'[DRY RUN]' if args.dry_run else ''}...")
    success, errors = push_tag_updates(to_update, dry_run=args.dry_run)
    info(f"Done — successes: {success}  errors: {errors}")

    # ── Final report ──────────────────────────────────────────────────────────
    print_report(dict(cat_stats), dict(type_stats), args.hours)

if __name__ == "__main__":
    main()