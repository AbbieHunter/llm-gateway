import json
import os

from dotenv import load_dotenv

load_dotenv()

# Provider credentials are referenced via env only (never stored in DB).
# Adding a new provider => add its key here AND restart the gateway (ARCHITECTURE §4.2).
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

# M0 informational default; request `model` is still required (validated in the router).
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gpt-4o-mini")

# M0: static known model subset returned by GET /v1/models (base list; aliases added in M1).
KNOWN_MODELS = [
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-3.5-turbo",
    "deepseek-chat",
    "deepseek-reasoner",
]

# --- M1 configuration (see M1_DEV_PLAN §2.9 / §2.10) ---
# SQLite (default) or Postgres. Examples:
#   sqlite+aiosqlite:///./data/gateway.db
#   postgresql+asyncpg://gateway:gateway@postgres:5432/gateway
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/gateway.db")

BOOTSTRAP_ADMIN_USERNAME = os.getenv("BOOTSTRAP_ADMIN_USERNAME", "admin")
# Required at first boot; missing => startup fails (fail-loud, R5).
BOOTSTRAP_ADMIN_PASSWORD = os.getenv("BOOTSTRAP_ADMIN_PASSWORD")

# JWT signing secret for DB-backed sessions (R1). Required in non-test runs.
JWT_SECRET = os.getenv("JWT_SECRET", "dev-insecure-change-me")

# Redis for provider health status; optional in M1 (health returns 'healthy' if unset).
REDIS_URL = os.getenv("REDIS_URL")

# Session lifetime (minutes); only affects sessions.expires_at (R1).
SESSION_EXPIRE_MIN = int(os.getenv("SESSION_EXPIRE_MIN", "60"))

# Test-only mock adapter switch (R4). "1" => model "mock/echo" returns a fixed response
# without calling LiteLLM, enabling keyless end-to-end routing tests.
MOCK_PROVIDER = os.getenv("MOCK_PROVIDER", "0") == "1"

# Test-only in-memory Redis (M2). "1" => use fakeredis instead of a real Redis server,
# so the quota/usage code paths can be exercised locally without redis-server.
# Production MUST use a real Redis (set REDIS_URL + leave this "0").
REDIS_FAKE = os.getenv("REDIS_FAKE", "0") == "1"

# --- M3 resilience / probe / cache configuration (all have safe defaults) ---

# Circuit breaker: trip when failure rate over the window exceeds this (R2).
CB_FAILURE_RATE = float(os.getenv("CB_FAILURE_RATE", "0.5"))
# Circuit breaker window + cooldown (seconds).
CB_WINDOW_SEC = int(os.getenv("CB_WINDOW_SEC", "30"))
CB_MIN_SAMPLES = int(os.getenv("CB_MIN_SAMPLES", "5"))
CB_COOLDOWN_SEC = int(os.getenv("CB_COOLDOWN_SEC", "30"))

# Retry: max attempts for a retryable error within a single request (exp backoff + jitter).
RETRY_MAX = int(os.getenv("RETRY_MAX", "3"))

# Per-VK in-flight concurrency cap for /v1/chat/completions (no queue; excess -> 429).
# Default 8 suits small-team parallel chats. 0 (or negative) = unlimited.
VK_MAX_INFLIGHT = int(os.getenv("VK_MAX_INFLIGHT", "8"))

# Uvicorn worker processes. >1 requires a non-SQLite DATABASE_URL (Postgres).
# entrypoint.sh enforces this so multi-worker never shares one SQLite file.
UVICORN_WORKERS = int(os.getenv("UVICORN_WORKERS", "1"))


# Probe: interval for quota_exhausted auto-recovery sweep (R3).
PROBE_INTERVAL_SEC = int(os.getenv("PROBE_INTERVAL_SEC", "600"))
# Probe failure backoff: doubles each failure, capped here (R3).
PROBE_COOLDOWN_CAP_SEC = int(os.getenv("PROBE_COOLDOWN_CAP_SEC", "3600"))

# Exact cache: TTL for cached non-stream responses (seconds, R7).
CACHE_TTL_SEC = int(os.getenv("CACHE_TTL_SEC", "3600"))

# --- Runtime cache switches (Tier-1 exact + Tier-2 semantic) ---
# These are runtime-mutable and persisted next to the DB so an admin can toggle
# caching from the web console without a restart or code change. Startup
# precedence: env default -> persisted data/cache_config.json override.
if DATABASE_URL.startswith("sqlite"):
    _DB_PATH = DATABASE_URL.replace("sqlite+aiosqlite:///", "").replace("sqlite:///", "")
    DATA_DIR = os.path.dirname(_DB_PATH) or "."
else:
    # Postgres (and other non-file DBs): keep cache_config.json under ./data.
    DATA_DIR = os.getenv("DATA_DIR", "./data")
CACHE_CONFIG_PATH = os.path.join(DATA_DIR, "cache_config.json")

_cache_cfg = {
    "exact": os.getenv("CACHE_ENABLE", "1") == "1",
    "semantic": os.getenv("SEMANTIC_CACHE_ENABLE", "1") == "1",
}


def load_cache_config() -> None:
    """Override in-memory defaults with whatever was persisted (if any)."""
    global _cache_cfg
    try:
        with open(CACHE_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return
    if isinstance(data, dict):
        if "exact" in data:
            _cache_cfg["exact"] = bool(data["exact"])
        if "semantic" in data:
            _cache_cfg["semantic"] = bool(data["semantic"])


def save_cache_config() -> None:
    """Persist the current in-memory cache config next to the DB file."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CACHE_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(_cache_cfg, f)


def get_cache_config() -> dict:
    return dict(_cache_cfg)


def set_cache_config(exact: bool, semantic: bool) -> dict:
    _cache_cfg["exact"] = bool(exact)
    _cache_cfg["semantic"] = bool(semantic)
    save_cache_config()
    return dict(_cache_cfg)


def is_exact_cache_enabled() -> bool:
    return _cache_cfg["exact"]


def is_semantic_cache_enabled() -> bool:
    return _cache_cfg["semantic"]


# Apply persisted overrides at import time, before the gateway serves traffic.
load_cache_config()

# --- M4 observability (T-04, R7) ---
# Master switch for the /metrics endpoint. When False, /metrics returns 404 so
# the gateway exposes no Prometheus surface at all (defence-in-depth).
METRICS_ENABLED = os.getenv("METRICS_ENABLED", "1") == "1"

# --- M4 semantic cache (T-01, R1~R3) ---
# Tier2 cache: only consulted on an exact-cache miss, non-stream, and when no
# `seed` is present (deterministic requests must not be soft-reused). Scope is
# the precise `provider/model` string so answers never cross models. The on/off
# switch itself is now runtime-mutable (see _cache_cfg / is_semantic_cache_enabled).
SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.92"))
# Default embedding model for the semantic (Tier-2) cache. bge-small-zh-v1.5 is a
# local/open-source Chinese-optimized model; serve it via an OpenAI-compatible
# embedding endpoint (vLLM / TEI / FlagEmbedding / Ollama) and point
# SEMANTIC_EMBEDDING_API_BASE at it. Set to "fake" to force the offline bag-of-words
# embedding (tests / no embedding server). The MOCK_PROVIDER switch also forces fake.
SEMANTIC_EMBEDDING_MODEL = os.getenv("SEMANTIC_EMBEDDING_MODEL", "quentinz/bge-small-zh-v1.5")
# When set, the semantic cache routes to a LOCAL OpenAI-compatible embedding server
# (forces the `openai/` provider route and passes api_base + api_key to LiteLLM).
# Leave empty to call a hosted model purely by name (e.g. text-embedding-3-small).
SEMANTIC_EMBEDDING_API_BASE = os.getenv("SEMANTIC_EMBEDDING_API_BASE", "")
SEMANTIC_EMBEDDING_API_KEY = os.getenv("SEMANTIC_EMBEDDING_API_KEY", "")
SEMANTIC_CACHE_TTL_SEC = int(os.getenv("SEMANTIC_CACHE_TTL_SEC", "86400"))

# --- M4 PII / audit guardrails (T-05, R8) ---
# Default OFF (personal/small-team self-use). Inbound can redact PII before it
# leaves for the upstream provider; outbound masking is a SEPARATE, stricter
# switch (default OFF) so we never destroy a model's legitimate output.
GUARDRAILS_ENABLED = os.getenv("GUARDRAILS_ENABLED", "0") == "1"
GUARDRAILS_INBOUND_MODE = os.getenv("GUARDRAILS_INBOUND_MODE", "redact")  # redact | detect
GUARDRAILS_OUTBOUND_MASK = os.getenv("GUARDRAILS_OUTBOUND_MASK", "0") == "1"

