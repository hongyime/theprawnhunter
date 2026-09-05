"""Minimal Celery app for Flower monitoring only — reads only REDIS_URL and
task-name introspection from the broker. Does NOT import app/core/config.py,
so it never sees ENCRYPTION_KEY, SUPABASE keys, bot tokens, or scanner keys.

Enables the secret-split roadmap where flower's env_file is just .env.public
(and this file — nothing else). See round 2 audit item #1.
"""
import os

from celery import Celery

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

app = Celery(
    "flower_monitor",
    broker=REDIS_URL,
    backend=REDIS_URL,
)

# Match main worker task naming so Flower's task-graph makes sense.
# We don't need to actually import the task modules — Flower shows names
# from the broker events.
app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
)
