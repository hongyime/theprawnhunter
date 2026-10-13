"""Application-wide constants — replaces magic numbers scattered across modules."""

import os

# Bot listener distributed lock TTL (seconds)
LOCK_TTL_SECONDS = 120

# Broadcast claim timeout — claims older than this are considered stale (minutes).
# A single broadcast batch processes up to 500 messages at 1.5s/msg = 750s worst case.
# The timeout must exceed this so a slow-but-alive worker's early claims are not
# reclaimed by a concurrent broadcaster while still in progress.
# 15 minutes (900s) gives safe headroom over the 750s worst case.
CLAIM_TIMEOUT_MINUTES = 15

# Worker heartbeat timeout — alert if worker silent longer than this (seconds)
WORKER_HEARTBEAT_TIMEOUT_SECONDS = 45 * 60

# Rate limit sleep between broadcast sends (seconds).
# The default keeps a 200-message batch under the 15-minute claim window while
# giving Telegram more room than the previous 1.5s pause.
BROADCAST_RATE_LIMIT_SLEEP = float(os.getenv("BROADCAST_RATE_LIMIT_SLEEP_S", "2.5"))

# Maximum entries kept in scanner error buffers
MAX_ERRORS_BUFFER = 100

# Session file permissions: owner read/write only
SESSION_FILE_PERMISSIONS = 0o600

# Telegram system account IDs (fixed by Telegram, not secrets)
TELEGRAM_SERVICE_NOTIFICATIONS_ID = 777000  # Official Telegram service notifications sender
