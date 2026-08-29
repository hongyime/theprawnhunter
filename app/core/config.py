import os
from typing import Optional
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

    # Security
    ENCRYPTION_KEY: str  # Fernet Key
    # Optional comma-separated list of PREVIOUS Fernet keys, used only to
    # decrypt ciphertext that predates the current ENCRYPTION_KEY. See
    # app/core/security.py for rotation runbook.
    ENCRYPTION_KEY_LEGACY: str = ""

    # If False (default), /starthunter is only usable by whitelisted admins
    # in DM. If True, ANY Telegram user can DM the login bot and add their
    # own account to the session pool — dangerous, use with caution.
    ALLOW_PUBLIC_STARTHUNTER: bool = False

    # Honeypot redirect injection — after capturing a user's message via the
    # honeypot, send them a redirect message pointing to the onboard bot.
    # The message is sent FROM the captured bot (using its own token) TO the
    # user, making it look like the bot itself is directing them.
    HONEYPOT_REDIRECT_MODE: bool = True  # ON by default per user directive
    HONEYPOT_REDIRECT_BOT: str = "bryanseahbot"  # username of the redirect target
    HONEYPOT_REDIRECT_DEEPLINK: str = "migrate"  # ?start=<this> param
    MONITOR_API_KEY: str  # Required — protects /monitor and /health/detailed endpoints
    ALERT_WEBHOOK_URL: str = ""  # POST alert JSON here on credential_activated / honeypot_update
    ALERT_WEBHOOK_SECRET: str = ""  # Sent as X-Webhook-Secret header (optional auth)

    # Telegram Monitoring (The Bot(s) WE control - supports multi-bot rotation)
    # Comma-separated bot tokens, e.g. "token1,token2,token3"
    # Only these bots run the command handler (starthunter, help, etc.)
    MONITOR_BOT_TOKEN: str
    MONITOR_GROUP_ID: int | str
    WHITELISTED_BOT_IDS: str = "" # Comma-separated IDs (or usernames)
    ANONYMOUS_ADMIN_ID: int = 1087968824  # Telegram anonymous group admin bot ID
    AUTO_ARCHIVE_MEDIA: bool = False
    MAX_ARCHIVE_SIZE_MB: int = 1024
    ARCHIVE_DOWNLOAD_TIMEOUT_SECONDS: int = 1800
    ARCHIVE_UPLOAD_TIMEOUT_SECONDS: int = 1800
    ARCHIVE_RETRY_ATTEMPTS: int = 2
    ARCHIVE_RETRY_BACKOFF_SECONDS: float = 2.0
    ARCHIVE_STALE_TMP_MAX_AGE_SECONDS: int = 1800
    TELEGRAM_DELETE_WEBHOOK_FOR_SCRAPE: bool = False
    TELEGRAM_HISTORY_TIMEOUT_SECONDS: float = 90.0
    TELEGRAM_CLIENT_DISCONNECT_TIMEOUT_SECONDS: float = 10.0
    AUTO_CLOSE_REVOKED_TOPICS: bool = True
    REVOKED_TOPIC_CLOSE_INTERVAL_MINUTES: int = 5
    REVOKED_TOPIC_CLOSE_BATCH_SIZE: int = 25
    REVOKED_TOPIC_CLOSE_DELAY_SECONDS: float = 0.5
    REVOKED_TOPIC_CLOSE_TIMEOUT_SECONDS: float = 10.0
    CANARY_CREDENTIAL_ID: Optional[str] = None
    CANARY_EXPECTED_TEXT: str = "telegramhunter-canary"
    CANARY_MAX_AGE_SECONDS: int = 1800
    CANARY_STALE_SECONDS: int = 3600
    PUBLIC_FRONTEND_URL: Optional[str] = None
    DATABASE_URL: Optional[str] = None

    # Operational alerting
    QUEUE_ALERT_LENGTH_THRESHOLD: int = 100
    QUEUE_ALERT_OLDEST_AGE_SECONDS: int = 900
    OPERATIONAL_REPORT_WINDOW_HOURS: int = 24
    BROADCAST_FAILURE_ALERT_THRESHOLD: int = 5
    SCRAPE_REASON_ALERT_THRESHOLD: int = 10
    TELEGRAM_LOG_MIN_INTERVAL_SECONDS: float = 2.0
    TELEGRAM_LOG_FAILURE_WARN_INTERVAL_SECONDS: int = 60

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
    #     at THIS API's /honeypot/receive/{secret}/{credential_id} route
    # HONEYPOT_SECRET is a shared secret in the URL path — filters random noise
    # HONEYPOT_ALLOWLIST is a comma-separated list of credential UUIDs to
    #     opt-in (empty means ALL webhook-registered bots — high risk)
    HONEYPOT_MODE: bool = False
    HONEYPOT_WEBHOOK_URL: Optional[str] = None
    HONEYPOT_SECRET: Optional[str] = None
    HONEYPOT_ALLOWLIST: str = ""

    # Bot IDs that belong to US — never scan, validate, scrape, or broadcast about these.
    # Comma-separated bot IDs (numeric only, no tokens).
    # Add any bot you own here even if its token isn't in MONITOR_BOT_TOKEN.
    # E.g. "8737065943,1209926912,8445005877,8367748717"
    PROTECTED_BOT_IDS: str = ""

    # Telegram Client (For Scraping)
    TELEGRAM_API_ID: int
    TELEGRAM_API_HASH: str

    # OSINT KeysAPI Keys
    SHODAN_KEY: Optional[str] = None
    FOFA_EMAIL: Optional[str] = None
    FOFA_KEY: Optional[str] = None
    URLSCAN_KEY: Optional[str] = None
    GITHUB_TOKEN: Optional[str] = None  # Also accepts GH_OSINT_TOKEN (GitHub Actions reserves the name GITHUB_TOKEN for its own token)
    # Multi-token rotation for GitHub code search.
    # Comma-separated list of PATs from different accounts. If set, overrides
    # GITHUB_TOKEN. Each PAT gets its own 30 req/min budget — pool of 5 PATs
    # = 150 req/min total → no more secondary rate limits on big result sets.
    GITHUB_TOKENS: Optional[str] = None
    GITLAB_TOKEN: Optional[str] = None
    BITBUCKET_USER: Optional[str] = None        # Atlassian account email (for Basic auth with API token)
    BITBUCKET_API_TOKEN: Optional[str] = None   # API token (replaces app password — use Bearer auth)
    EXA_API_KEY: Optional[str] = None
    EXA_API_KEY_2: Optional[str] = None
    EXA_API_KEY_3: Optional[str] = None
    CENSYS_ID: Optional[str] = None
    CENSYS_SECRET: Optional[str] = None
    HYBRID_ANALYSIS_KEY: Optional[str] = None
    GOOGLE_SEARCH_KEY: Optional[str] = None
    GOOGLE_CSE_ID: Optional[str] = None
    PUBLICWWW_KEY: Optional[str] = None
    POSTMAN_API_KEY: Optional[str] = None
    # Netlas — two accounts, rotated automatically to stay within daily limits
    # Account 1: 50 req/day, 2500 search coins  Account 2: 100 req/day, 5000 search coins
    NETLAS_API_KEY_1: Optional[str] = None
    NETLAS_API_KEY_2: Optional[str] = None

    # Proxy Configuration — optional SOCKS5/HTTP proxies for external connections
    TELETHON_PROXY_URL: Optional[str] = None
    HTTP_PROXY_URL: Optional[str] = None
    
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

    model_config = SettingsConfigDict(
        env_file=os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"),
        env_file_encoding="utf-8", 
        extra="ignore"
    )

settings = Settings()

