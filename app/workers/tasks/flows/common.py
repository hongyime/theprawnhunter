import asyncio

import contextlib

import logging

import os

import random

import time

from collections import defaultdict

from datetime import UTC

from typing import Any

import httpx

import redis

from celery.exceptions import SoftTimeLimitExceeded

from app.core.audit import AuditEvent, AuditLogger

from app.core.config import settings

from app.core.database import db

from app.core.security import security

from app.infrastructure.database import execute_async as _execute_async_shared

from app.services.scraper_srv import scraper_service

from app.workers.celery_app import app

logger = logging.getLogger("flow.tasks")

async def async_execute(query_builder):
    """Executes a Supabase query builder synchronously in a background thread."""
    return await _execute_async_shared(query_builder)

redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)

_broadcaster = None

def get_broadcaster():
    """
    Returns the module-level BroadcasterService singleton.
    Lazy-initialized to avoid import-time issues.
    Bot rotation state is preserved across all task calls in this worker.
    """
    global _broadcaster
    if _broadcaster is None:
        from app.services.broadcaster_srv import BroadcasterService
        _broadcaster = BroadcasterService()
    return _broadcaster

async def _mark_credential_revoked(cred_id: str, reason: str) -> None:
    await async_execute(
        db.table("discovered_credentials")
        .update({"status": "revoked"})
        .eq("id", cred_id)
    )
    from .broadcast import _queue_revoked_topic_close

    _queue_revoked_topic_close(cred_id, reason)

__all__ = ['Any', 'AuditEvent', 'AuditLogger', 'SoftTimeLimitExceeded', 'UTC', '_broadcaster', '_execute_async_shared', '_mark_credential_revoked', 'app', 'async_execute', 'asyncio', 'contextlib', 'db', 'defaultdict', 'get_broadcaster', 'httpx', 'logger', 'logging', 'os', 'random', 'redis', 'redis_client', 'scraper_service', 'security', 'settings', 'time']
