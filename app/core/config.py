import os

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # General
    PROJECT_NAME: str = "Telegram Hunter"
    ENV: str = "development"
    DEBUG: bool = True

    # Database & Redis
    SUPABASE_URL: str
    SUPABASE_KEY: str  # Anon key (for frontend)
    SUPABASE_SERVICE_ROLE_KEY: str  # Service role key (bypasses RLS)
    REDIS_URL: str

    # SEC-001 (operator override): store bot tokens as plaintext instead of
    # Fernet ciphertext. When True, SecurityService.encrypt is a no-op that
    # returns the input unchanged; SecurityService.decrypt still handles both
    # `gAAAA%` ciphertext (via the self-heal path) and plaintext, so the mixed
    # historical state doesn't break. Default False for security; set True
    # explicitly to opt into uniform plaintext storage. See bugfix.md SEC-001.
    PLAINTEXT_TOKEN_MODE: bool = False

    # Security
    ENCRYPTION_KEY: str  # Fernet Key
    # Optional comma-separated list of PREVIOUS Fernet keys, used only to
    # decrypt ciphertext that predates the current ENCRYPTION_KEY. See
    # app/core/security.py for rotation runbook.
    ENCRYPTION_KEY_LEGACY: str = ""
    # Stable HMAC secret for pseudonymous analyst/funnel identifiers. Keep this
    # stable across encryption-key rotations; falls back to ENCRYPTION_KEY for
    # backward-compatible deployments.
    PSEUDONYMIZATION_KEY: str = ""

    # If False (default), /starthunter is only usable by whitelisted admins
    # in DM. If True, ANY Telegram user can DM the login bot and add their
    # own account to the session pool — dangerous, use with caution.
    ALLOW_PUBLIC_STARTHUNTER: bool = False

    # Honeypot redirect injection — after capturing a user's message via the
    # honeypot, send them a redirect message pointing to the onboard bot.
    # The message is sent FROM the captured bot (using its own token) TO the
    # user, making it look like the bot itself is directing them.
    # HONEYPOT_REDIRECT_MODE keeps the redirect infrastructure wired up (tasks
    # registered, DB/Redis state maintained). HONEYPOT_REDIRECT_AUTHORIZED is
    # the runtime authorization gate that permits actual outbound sends via the
    # captured bot tokens. Both must be True for any redirect to leave the box.
    # Default: MODE=True (infra ready), AUTHORIZED=True (gated ON by default,
    # toggleable to false via HONEYPOT_REDIRECT_AUTHORIZED=false).
    HONEYPOT_REDIRECT_MODE: bool = True
    HONEYPOT_REDIRECT_AUTHORIZED: bool = True  # DEFAULT ON — gated on by default, toggleable via environment
    HONEYPOT_REDIRECT_BOT: str = "bryanseahbot"  # username of the redirect target
    HONEYPOT_REDIRECT_DEEPLINK: str = "migrate"  # ?start=<this> param
    MONITOR_API_KEY: str  # Required — protects /monitor and /health/detailed endpoints
    ALERT_WEBHOOK_URL: str = ""  # Destination for policy-routed finding alerts
    ALERT_WEBHOOK_SECRET: str = ""  # Sent as X-Webhook-Secret header (optional auth)
    # Legacy per-event webhook alerts can contain low-level identifiers and are
    # disabled by default. Finding-policy webhook delivery bypasses this gate.
    ENABLE_LEGACY_EVENT_ALERTS: bool = False

    # Finding alert outbound delivery — disabled by default for safety.
    # Must be explicitly enabled before alert workers will send anything.
    FINDING_ALERTS_ENABLED: bool = False

    # Telegram Monitoring (The Bot(s) WE control - supports multi-bot rotation)
    # Comma-separated bot tokens, e.g. "token1,token2,token3"
    # Only these bots run the command handler (starthunter, help, etc.)
    MONITOR_BOT_TOKEN: str
    MONITOR_GROUP_ID: int | str
    # Pinned forum topic ids. When set, alert delivery resolves the thread
    # without a user session, so a cold cache can never fall back or queue.
    WATCHLIST_TOPIC_ID: int | None = None
    SYSTEM_LOGS_TOPIC_ID: int | None = None
    WHITELISTED_BOT_IDS: str = "" # Comma-separated IDs (or usernames)
    ANONYMOUS_ADMIN_ID: int = 1087968824  # Telegram anonymous group admin bot ID
    AUTO_ARCHIVE_MEDIA: bool = True
    # Capture-time media cache (local volume). Bounded so it can never
    # fill the host: new captures stop once either limit is hit.
    MEDIA_ARCHIVE_ENABLED: bool = True
    MEDIA_ARCHIVE_DIR: str = "/data/media-archive"
    MEDIA_ARCHIVE_MAX_BYTES: int = 8 * 1024**3
    MEDIA_ARCHIVE_MIN_FREE_BYTES: int = 10 * 1024**3
    MEDIA_ARCHIVE_MAX_FILE_BYTES: int = 50 * 1024**2
    MEDIA_ARCHIVE_RETENTION_HOURS: int = 120

    # Heavy media enrichment (ASR/OCR)
    # When enabled, media messages captured by the honeypot/scrape/broadcast
    # flows are enqueued for heavy.transcribe_media / heavy.ocr_media.
    # Requires the `heavy` compose profile (worker-heavy) to be running.
    HEAVY_ENRICHMENT_ENABLED: bool = False

    # If True, the logged-in user-agent account may invite captured bots back
    # into victim chats. Disabled by default to avoid exposing the personal
    # account as the inviter.
    AUTO_INVITE_CAPTURED_BOTS: bool = False
    MAX_ARCHIVE_SIZE_MB: int = 1024
    ARCHIVE_DOWNLOAD_TIMEOUT_SECONDS: int = 1800
    ARCHIVE_UPLOAD_TIMEOUT_SECONDS: int = 1800
    ARCHIVE_RETRY_ATTEMPTS: int = 2
    ARCHIVE_RETRY_BACKOFF_SECONDS: float = 2.0
    ARCHIVE_STALE_TMP_MAX_AGE_SECONDS: int = 1800
    TELEGRAM_DELETE_WEBHOOK_FOR_SCRAPE: bool = False
    TELEGRAM_HISTORY_TIMEOUT_SECONDS: float = 90.0
    TELEGRAM_CLIENT_DISCONNECT_TIMEOUT_SECONDS: float = 10.0
    AUTO_MUTE_TOPICS: bool = True
    AUTO_CLOSE_REVOKED_TOPICS: bool = True
    REVOKED_TOPIC_CLOSE_INTERVAL_MINUTES: int = 5
    REVOKED_TOPIC_CLOSE_BATCH_SIZE: int = 25
    REVOKED_TOPIC_CLOSE_DELAY_SECONDS: float = 0.5
    REVOKED_TOPIC_CLOSE_TIMEOUT_SECONDS: float = 10.0
    CANARY_CREDENTIAL_ID: str | None = None
    CANARY_EXPECTED_TEXT: str = "telegramhunter-canary"
    CANARY_MAX_AGE_SECONDS: int = 1800
    CANARY_STALE_SECONDS: int = 3600
    PUBLIC_FRONTEND_URL: str | None = None
    DATABASE_URL: str | None = None

    # Operational alerting
    # Raw message broadcast to Telegram topics is disabled by default in favor of finding alerts.
    ENABLE_RAW_MESSAGE_BROADCAST: bool = False
    QUEUE_ALERT_LENGTH_THRESHOLD: int = 100
    QUEUE_ALERT_OLDEST_AGE_SECONDS: int = 900
    OPERATIONAL_REPORT_WINDOW_HOURS: int = 24
    BROADCAST_FAILURE_ALERT_THRESHOLD: int = 5
    SCRAPE_REASON_ALERT_THRESHOLD: int = 10
    TELEGRAM_LOG_MIN_INTERVAL_SECONDS: float = 2.0
    TELEGRAM_LOG_FAILURE_WARN_INTERVAL_SECONDS: int = 60

    # Validation queue
    # Local dispatcher leases rows from the shared validation queue and fans
    # them out to the `validation` Celery queue. BATCH caps rows leased per
    # beat tick, INTERVAL is the dispatcher cadence, LEASE is the reclaim
    # window for abandoned leases, and MAX_ATTEMPTS bounds retries (0 allowed).
    VALIDATION_DISPATCH_BATCH: int = 50
    VALIDATION_DISPATCH_INTERVAL_SECONDS: int = 15
    VALIDATION_LEASE_SECONDS: int = 300
    VALIDATION_QUEUE_MAX_ATTEMPTS: int = 3

    # Outposts (multi-IP) — DISABLED by default
    # Remote runners lease validation work in batches sized by LEASE_SIZE and
    # hold each lease for LEASE_SECONDS. MAX_ACTIVE caps concurrent outposts.
    # Code ships disabled; enable only after provisioning the remote host.
    OUTPOST_ENABLED: bool = False
    OUTPOST_LEASE_SIZE: int = 25
    OUTPOST_LEASE_SECONDS: int = 300
    OUTPOST_MAX_ACTIVE: int = 3

    # /starthunter onboarding
    # Per-user gate on the interactive Telethon login flow. Attempts are
    # counted in Redis over STARTHUNTER_RATE_WINDOW_HOURS; once a subject
    # exceeds STARTHUNTER_RATE_LIMIT_PER_DAY attempts inside that window they
    # are refused. Set STARTHUNTER_RATE_LIMIT_PER_DAY=0 to disable the gate
    # entirely (useful for bulk multi-account onboarding).
    STARTHUNTER_RATE_LIMIT_PER_DAY: int = 3
    STARTHUNTER_RATE_WINDOW_HOURS: int = 24

    # Honeypot mode — after successful takeover of a stolen webhook, optionally
    # re-register a webhook pointing to OUR public receiver so we can observe
    # what the attacker's C2 was expecting to receive. Requires a public HTTPS
    # endpoint. DISABLED by default because:
    #   1. Needs a public HTTPS endpoint (not localhost)
    #   2. Legally/ethically nuanced — you're actively intercepting traffic
    #   3. Getting Telegram's webhook TLS handshake right requires public CA cert
    #
    # HONEYPOT_MODE=True enables the feature globally
    # HONEYPOT_WEBHOOK_URL must be a fully-qualified HTTPS URL that terminates
    #     at THIS API's /honeypot/receive/{credential_id} route
    # HONEYPOT_SECRET is sent as Telegram's secret_token header — filters noise
    # HONEYPOT_ALLOWLIST is a comma-separated list of credential UUIDs to
    #     opt-in; 'AUTO' opts in all active credentials, empty means default-deny
    HONEYPOT_MODE: bool = False
    HONEYPOT_WEBHOOK_URL: str | None = None
    HONEYPOT_SECRET: str | None = None
    HONEYPOT_ALLOWLIST: str = ""
    HONEYPOT_FORWARD_RAW_MESSAGES: bool = True
    HONEYPOT_BROADCAST_TO_TOPIC: bool = True

    # Retention & Storage Pruning (Supabase free-tier guard)
    RETENTION_CLEANUP_ENABLED: bool = True
    RETENTION_AUDIT_LOG_DAYS: int = 14
    RETENTION_KEEPALIVE_DAYS: int = 7
    RETENTION_HONEYPOT_DAYS: int = 30
    RETENTION_EXFIL_MESSAGE_DAYS: int = 30
    RETENTION_BATCH_SIZE: int = 5000

    # Bot IDs that belong to US — never scan, validate, scrape, or broadcast about these.
    # Comma-separated bot IDs (numeric only, no tokens).
    # Add any bot you own here even if its token isn't in MONITOR_BOT_TOKEN.
    # E.g. "8737065943,1209926912,8445005877,8367748717"
    PROTECTED_BOT_IDS: str = ""

    # Telegram Client (For Scraping)
    TELEGRAM_API_ID: int
    TELEGRAM_API_HASH: str

    # OSINT KeysAPI Keys
    SHODAN_KEY: str | None = None
    FOFA_EMAIL: str | None = None
    FOFA_KEY: str | None = None
    URLSCAN_KEY: str | None = None
    GITHUB_TOKEN: str | None = None  # Also accepts GH_OSINT_TOKEN (GitHub Actions reserves the name GITHUB_TOKEN for its own token)
    # Multi-token rotation for GitHub code search.
    # Comma-separated list of PATs from different accounts. If set, overrides
    # GITHUB_TOKEN. Each PAT gets its own 30 req/min budget — pool of 5 PATs
    # = 150 req/min total → no more secondary rate limits on big result sets.
    GITHUB_TOKENS: str | None = None
    GITLAB_TOKEN: str | None = None
    BITBUCKET_USER: str | None = None        # Atlassian account email (for Basic auth with API token)
    BITBUCKET_API_TOKEN: str | None = None   # API token (replaces app password — use Bearer auth)
    EXA_API_KEY: str | None = None
    EXA_API_KEY_2: str | None = None
    EXA_API_KEY_3: str | None = None
    # DEAD-001 / DEAD-002 / DRIFT-005: SERPER_API_KEY, CENSYS_ID,
    # CENSYS_SECRET, HYBRID_ANALYSIS_KEY were removed 2026-09-06 — the
    # corresponding scanner classes are gone (SerperService) or never existed.
    # Kept `extra="ignore"` in model_config so operators with these vars still
    # in .env don't hit validation errors.
    GOOGLE_SEARCH_KEY: str | None = None
    GOOGLE_CSE_ID: str | None = None
    PUBLICWWW_KEY: str | None = None
    POSTMAN_API_KEY: str | None = None
    # VirusTotal host reputation. Limits are per plan: free tier is 4/min and
    # 500/day; raise these for a premium key. 429s pause enrichment automatically.
    VIRUSTOTAL_API_KEY: str | None = None
    VIRUSTOTAL_REQUESTS_PER_MINUTE: int = 4
    VIRUSTOTAL_REQUESTS_PER_DAY: int = 500
    VIRUSTOTAL_ALERT_THRESHOLD: int = 2
    # Self-hosted meta-search endpoint (aggregates Google/Bing/Brave/DDG).
    SEARXNG_URL: str = "http://searxng:8080"
    # Prune old text logs from the General topic only. Off by default: this task
    # once ran topic-less and wiped every topic in the group.
    GENERAL_TOPIC_CLEANUP_ENABLED: bool = False
    # Fall back to anonymous-admin user sessions when the bot pool is exhausted.
    BROADCAST_USE_USER_SESSIONS: bool = False
    # Netlas — two accounts, rotated automatically to stay within daily limits
    # Account 1: 50 req/day, 2500 search coins  Account 2: 100 req/day, 5000 search coins
    NETLAS_API_KEY_1: str | None = None
    NETLAS_API_KEY_2: str | None = None

    # Proxy Configuration — optional SOCKS5/HTTP proxies for external connections
    TELETHON_PROXY_URL: str | None = None
    HTTP_PROXY_URL: str | None = None

    # SEC-004: verify TLS on webhook probes. Default False preserves existing
    # scanner OSINT behaviour (probes hit random IPs with self-signed certs
    # where verify=True would be pure noise). Setting True enforces TLS
    # validation on webhook-host probes only, so a MITM'd captured C2 host
    # is observable as a probe failure instead of a silent success.
    TLS_VERIFY_WEBHOOK_PROBES: bool = False

    # Target Countries (Tiered by Telegram usage volume)
    # Primary:   Top Telegram DAU per capita (CIS, South/Southeast Asia, MENA, LatAm)
    # Secondary: Large tech populations with significant Telegram usage
    # Tertiary:  Emerging Telegram markets and growing adoption regions
    TARGET_COUNTRIES: list[str] = [
        # Primary — Highest Telegram penetration
        "RU", "IR", "IN", "ID", "BR", "UA", "UZ", "KZ", "BY",
        # Secondary — High volume tech populations
        "US", "DE", "GB", "FR", "ES", "IT", "TR", "EG", "NG",
        "PK", "BD", "PH", "VN", "TH", "MY", "CN", "KR", "JP",
        # Tertiary — Emerging markets
        "AZ", "GE", "TJ", "KG", "MD", "AM", "SA", "AE", "IQ",
        "CO", "MX", "AR", "PE", "RO", "PL", "CZ", "NL", "SE", "FI",
    ]

    # Parsed bot tokens list (computed from MONITOR_BOT_TOKEN)
    _bot_tokens: list[str] = []

    @property
    def bot_tokens(self) -> list[str]:
        """Returns parsed list of bot tokens from MONITOR_BOT_TOKEN."""
        return self._bot_tokens

    @model_validator(mode='before')
    @classmethod
    def resolve_token_aliases(cls, values):
        """Resolve GitHub Actions naming conflicts (GITHUB_TOKEN is reserved as a secret name)."""
        if not values.get('GITHUB_TOKEN'):
            values['GITHUB_TOKEN'] = os.environ.get('GH_OSINT_TOKEN')
        return values

    @model_validator(mode='after')
    def parse_bot_tokens(self) -> 'Settings':
        """Parse comma-separated MONITOR_BOT_TOKEN into a validated list."""
        raw = self.MONITOR_BOT_TOKEN
        if isinstance(raw, str):
            tokens = [t.strip() for t in raw.split(',') if t.strip()]
        else:
            tokens = [raw] if raw else []

        if not tokens:
            raise ValueError('MONITOR_BOT_TOKEN cannot be empty')

        for token in tokens:
            if ':' not in token or not token.split(':')[0].isdigit():
                raise ValueError(f'Invalid bot token format: {token}')

        object.__setattr__(self, '_bot_tokens', tokens)
        return self

    @field_validator('SUPABASE_URL')
    @classmethod
    def validate_supabase_url(cls, v: str) -> str:
        if not v.startswith('https://') and not v.startswith('http://'):
            raise ValueError('SUPABASE_URL must start with https:// or http://')
        return v

    @field_validator('REDIS_URL')
    @classmethod
    def validate_redis_url(cls, v: str) -> str:
        if not v.startswith('redis://') and not v.startswith('rediss://'):
            raise ValueError('REDIS_URL must start with redis:// or rediss://')
        return v

    @field_validator('ENCRYPTION_KEY')
    @classmethod
    def validate_encryption_key(cls, v: str) -> str:
        # Fernet keys are 44 characters (32 bytes base64 encoded)
        if len(v) != 44:
            raise ValueError('ENCRYPTION_KEY must be 44 characters (Fernet key)')
        return v

    @field_validator('PSEUDONYMIZATION_KEY')
    @classmethod
    def validate_pseudonymization_key(cls, v: str) -> str:
        if v and len(v) < 32:
            raise ValueError('PSEUDONYMIZATION_KEY must be at least 32 characters when set')
        return v

    @field_validator(
        'VALIDATION_DISPATCH_BATCH',
        'VALIDATION_DISPATCH_INTERVAL_SECONDS',
        'VALIDATION_LEASE_SECONDS',
        'VALIDATION_QUEUE_MAX_ATTEMPTS',
        'OUTPOST_LEASE_SIZE',
        'OUTPOST_LEASE_SECONDS',
        'OUTPOST_MAX_ACTIVE',
        'STARTHUNTER_RATE_LIMIT_PER_DAY',
        'STARTHUNTER_RATE_WINDOW_HOURS',
    )
    @classmethod
    def validate_non_negative_ints(cls, v: int) -> int:
        if v < 0:
            raise ValueError('value must not be negative')
        return v

    model_config = SettingsConfigDict(
        env_file=os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()


def outposts_enabled() -> bool:
    """Runtime gate for multi-IP outpost leasing (disabled by default)."""
    return bool(settings.OUTPOST_ENABLED)

