"""Bot listener runtime handlers."""

from .common import *

from .commands import *
from .login import *
from .updates import *
from .login import _conversation_timeout

def _bot_id_from_token(token: str) -> str:
    """Extracts Telegram bot ID prefix from token safely."""
    try:
        return token.strip().split(":", 1)[0]
    except Exception:
        return "unknown"

def _poll_lock_key(token: str) -> str:
    """Redis key used to enforce single active poller per bot ID."""
    return f"bot_listener:poll_lock:{_bot_id_from_token(token)}"

async def _acquire_poll_lock(token: str) -> str | None:
    """Acquire distributed lock for a bot poller. Returns lock key when acquired."""
    if not redis_client:
        return None

    key = _poll_lock_key(token)
    acquired = await redis_client.set(key, INSTANCE_ID, ex=LOCK_TTL_SECONDS, nx=True)
    if acquired:
        return key

    owner = await redis_client.get(key)
    logger.warning(
        f"⚠️ Poll lock already held for bot_id={_bot_id_from_token(token)} by {owner}. "
        "Skipping this poller instance to avoid getUpdates conflicts."
    )
    return None

async def _release_poll_lock(lock_key: str | None):
    """Release lock only if still owned by this process."""
    if not lock_key or not redis_client:
        return
    try:
        owner = await redis_client.get(lock_key)
        if owner == INSTANCE_ID:
            await redis_client.delete(lock_key)
    except Exception as e:
        logger.warning(f"Failed to release poll lock {lock_key}: {e}")

async def _renew_poll_lock(lock_key: str | None):
    """Periodically refresh lock TTL while polling is active."""
    if not lock_key or not redis_client:
        return

    try:
        while not stop_event.is_set():
            await asyncio.sleep(max(15, LOCK_TTL_SECONDS // 3))
            owner = await redis_client.get(lock_key)
            if owner != INSTANCE_ID:
                logger.warning(f"Lost ownership of poll lock {lock_key}.")
                return
            await redis_client.expire(lock_key, LOCK_TTL_SECONDS)
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.warning(f"Poll lock renew failed for {lock_key}: {e}")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    # 1. Check Redis
    redis_status = "✅ Online"
    if redis_client:
        try:
            await redis_client.ping()
        except Exception:
            redis_status = "Unreachable"
    else:
        redis_status = "Not Initialized"

    # 2. Check DB / Pending Queue
    queue_count = "?"
    try:
        res = await asyncio.to_thread(
            lambda: db.table("exfiltrated_messages").select("id", count="exact").eq("is_broadcasted", False).execute()
        )
        queue_count = res.count
    except Exception as e:
        queue_count = f"Error: {str(e)[:20]}"

    # 3. Check System Pause State
    is_paused = False
    if redis_client:
        with contextlib.suppress(Exception):
            is_paused = await redis_client.get(PAUSE_KEY)

    system_status = "PAUSED" if is_paused else "RUNNING"

    # 4. Bot pool info
    bot_count = len(_bot_usernames)
    locked_count = len(_locked_bots)

    msg = (
        f"📊 **System Status**\n\n"
        f"**State**: {system_status}\n"
        f"**Redis**: {redis_status}\n"
        f"**Pending Broadcasts**: `{queue_count}`\n"
        f"**Bot Pool**: `{bot_count} bots ({locked_count} locked)`\n"
        f"**Monitor Group**: `{settings.MONITOR_GROUP_ID}`\n"
        f"**Environment**: `{settings.ENV}`"
    )
    await _reply_and_delete(update, msg, parse_mode=ParseMode.MARKDOWN)

async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    if redis_client:
        await redis_client.set(PAUSE_KEY, "true")
        await _reply_and_delete(update, "Paused.")
    else:
         await _reply_and_delete(update, "Redis unavailable.")

async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    if redis_client:
        await redis_client.delete(PAUSE_KEY)
        await _reply_and_delete(update, "Resumed.")
    else:
         await _reply_and_delete(update, "Redis unavailable.")

async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    await _reply_and_delete(update, "Restarting...")
    # Signal main loop to stop gracefully
    stop_event.set()

async def watchdog_loop(bot):
    """
    Monitors System Health every 60 seconds.
    - Checks Redis connectivity.
    - Checks Worker Last Seen timestamp.
    """
    logger.info("🐶 Watchdog System Started.")

    # Initial State
    state = {
        "redis": True,
        "worker": True
    }

    while not stop_event.is_set():
        try:
            # Check Redis
            if redis_client:
                try:
                    await redis_client.ping()
                    if not state["redis"]:
                        state["redis"] = True
                        await _send_alert(bot, "✅ **RECOVERY**: Redis connection restored.")
                except Exception as e:
                    if state["redis"]:
                        state["redis"] = False
                        await _send_alert(bot, f"❌ **CRITICAL**: Redis connection LOST! ({str(e)[:20]})")

                    # If Redis is down, we can't check worker stats from Redis
                    await asyncio.sleep(60)
                    continue

                # Check Worker Heartbeat
                try:
                    last_seen = await redis_client.get("system:heartbeat:last_seen")
                    if last_seen:
                        import time
                        age = int(time.time()) - int(last_seen)

                        if age > (45 * 60): # 45 minutes
                            if state["worker"]:
                                state["worker"] = False
                                await _send_alert(bot, f"⚠️ **WARNING**: Worker silent for {int(age/60)} minutes!\n(It might be stuck or crashed)")
                        else:
                            if not state["worker"]:
                                state["worker"] = True
                                await _send_alert(bot, "✅ **RECOVERY**: Worker heartbeat detected.")
                except Exception as _swallowed:
                    logger.debug(f"[suppressed] {_swallowed}")

            await asyncio.sleep(60)

        except asyncio.CancelledError:
            break
        except Exception as e:
             logger.error(f"Watchdog error: {e}")
             await asyncio.sleep(60)

async def _send_alert(bot, msg):
    try:
        await _send_and_delete(bot, chat_id=settings.MONITOR_GROUP_ID, text=f"[Watchdog] {msg}")
    except Exception as e:
        logger.error(f"Failed to send watchdog alert: {e}")

def _build_application(token: str) -> Application:
    """Builds a python-telegram-bot Application for a single bot token."""
    request = HTTPXRequest(
        read_timeout=30.0,
        connect_timeout=30.0,
        pool_timeout=60.0
    )
    application = ApplicationBuilder().token(token).request(request).build()
    application.bot_data['_bot_token'] = token

    # Owned-bot voluntary funnel tracking. This handler stores only an HMAC
    # pseudonym and event metadata; private message content is never persisted.
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            track_private_inbound,
        ),
        group=-2,
    )

    # Group -1 runs before other handlers
    application.add_handler(MessageHandler(filters.ALL, log_update), group=-1)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler(["stop", "optout", "unsubscribe"], opt_out_command))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("pause", pause))
    application.add_handler(CommandHandler("resume", resume))
    application.add_handler(CommandHandler("restart", restart))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("commands", help_command))
    application.add_handler(CommandHandler("bots", bots_command))
    application.add_handler(CommandHandler("telemetry", telemetry_command))
    application.add_handler(CommandHandler("indicators", telemetry_command))
    application.add_handler(CommandHandler("getfile", getfile_command))
    application.add_handler(CommandHandler("archive", getfile_command))
    application.add_handler(CommandHandler("backfill", backfill_command))

    # Auto-promote logged-in session accounts when they join monitor group
    application.add_handler(
        ChatMemberHandler(on_chat_member_update, ChatMemberHandler.CHAT_MEMBER)
    )

    login_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('starthunter', starthunter)],
        states={
            WAIT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phone)],
            WAIT_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_code)],
            WAIT_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password)],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, _conversation_timeout)],
        },
        fallbacks=[CommandHandler('cancel', cancel_login)],
        conversation_timeout=180,  # 3-minute cap — orphan temp session files get wiped
    )
    application.add_handler(login_conv_handler)
    return application

async def _run_bot_once(token: str, is_primary: bool = False):
    """Runs a single bot's polling loop. Primary bot also runs the Watchdog."""
    lock_key = await _acquire_poll_lock(token)
    if redis_client and not lock_key:
        logger.info(
            f"Poll lock held by previous instance for bot_id={_bot_id_from_token(token)}. "
            f"Waiting {LOCK_TTL_SECONDS + 5}s for expiry before retrying..."
        )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=LOCK_TTL_SECONDS + 5)
            return
        except TimeoutError:
            pass
        lock_key = await _acquire_poll_lock(token)
        if not lock_key:
            logger.error(f"Poll lock still held after wait. Giving up for bot_id={_bot_id_from_token(token)}.")
            return

    application = _build_application(token)
    lock_renew_task = None
    watchdog_task = None

    try:
        await application.initialize()
        bot_info = await application.bot.get_me()
        bot_username = bot_info.username or f"bot_{bot_info.id}"
        _bot_usernames[token] = bot_username
        logger.info(f"🤖 Bot @{bot_username} starting polling...")

        if lock_key:
            lock_renew_task = asyncio.create_task(_renew_poll_lock(lock_key))

        # Retry start_polling up to 3 times on Conflict — another instance
        # may still be releasing its long-poll connection.
        for attempt in range(3):
            try:
                await application.updater.start_polling(
                    drop_pending_updates=False,
                    allowed_updates=Update.ALL_TYPES,
                )
                break
            except Conflict as e:
                if attempt < 2:
                    logger.warning(f"⚠️ Polling conflict for @{bot_username} (attempt {attempt+1}/3), retrying in 10s: {e}")
                    await asyncio.sleep(10)
                else:
                    logger.error(f"⚠️ Polling conflict for @{bot_username} after 3 attempts, giving up: {e}")
                    return

        # MUST call application.start() so the update queue processor runs.
        # async with application: only calls initialize/shutdown — handlers
        # are never dispatched without start().
        await application.start()

        if is_primary:
            watchdog_task = asyncio.create_task(watchdog_loop(application.bot))
            logger.info(f"🐶 Watchdog attached to primary bot @{bot_username}")

        logger.info(f"🚀 Bot @{bot_username} Started and Polling...")

        heartbeat_count = 0
        _ALIVE_FILE = "/tmp/bot_alive"  # noqa: N806 — function-scoped constant
        while not stop_event.is_set():
            await asyncio.sleep(10)
            heartbeat_count += 1
            # Touch the liveness file every iteration so the Docker healthcheck
            # can assert the file was modified within the last 60 seconds.
            try:
                import pathlib
                pathlib.Path(_ALIVE_FILE).touch()
            except Exception as _swallowed:
                logger.debug(f"[suppressed] {_swallowed}")
            if heartbeat_count % 30 == 0:
                logger.info(f"💓 Bot @{bot_username} polling heartbeat (Event loop active)")

        logger.info(f"Stopping bot @{bot_username}...")

    finally:
        if watchdog_task:
            watchdog_task.cancel()
        if lock_renew_task:
            lock_renew_task.cancel()
        try:
            await application.updater.stop()
            await application.stop()
        except Exception as _swallowed:
            logger.debug(f"[suppressed] {_swallowed}")
        await application.shutdown()
        await _release_poll_lock(lock_key)

async def _run_bot(token: str, is_primary: bool = False):
    """Supervisor: keep a bot's poller alive across transient failures.

    _run_bot_once returns/raises on transient issues (Redis blip, network
    error, getUpdates Conflict, lock give-up). Previously that ended the task,
    main()'s gather returned, the process logged "Bye!" and exited — so
    Docker's restart policy churned the container in a tight loop with no
    logged cause. Now we retry in-process with capped backoff until stop_event
    is set, logging every failure, so brief outages self-heal instead of
    triggering a full container restart.
    """
    backoff = 5
    while not stop_event.is_set():
        try:
            await _run_bot_once(token, is_primary)
        except Exception as exc:
            logger.error(f"Bot {_bot_id_from_token(token)} poller crashed: {exc!r}")
        if stop_event.is_set():
            break
        logger.warning(
            f"Bot {_bot_id_from_token(token)} poller stopped; retrying in {backoff}s."
        )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=backoff)
        backoff = min(backoff * 2, 60)

async def main():
    global redis_client

    # Wait for internet on startup (handles machine boot / container restart)
    from app.core.connectivity import wait_for_internet_async
    logger.info("Checking internet connectivity before starting...")
    if not await wait_for_internet_async(max_wait=300, check_interval=10):
        logger.error("No internet after 300s — starting anyway (will retry on each operation).")

    # Sweep orphan temp login session files left from crashed / abandoned flows.
    # Each is a live Telethon auth key — must not persist after the conversation dies.
    try:
        import glob as _glob
        import tempfile as _tempfile
        _temp_dir = _tempfile.gettempdir()
        _orphans = _glob.glob(os.path.join(_temp_dir, "temp_login_*.session*"))
        for _f in _orphans:
            try:
                os.remove(_f)
                logger.info(f"[Startup] swept orphan temp session file: {os.path.basename(_f)}")
            except Exception as _swallowed:
                logger.debug(f"[suppressed] {_swallowed}")
    except Exception as _sweep_exc:
        logger.debug(f"[Startup] orphan sweep failed: {_sweep_exc}")

    raw_tokens = settings.bot_tokens
    seen_ids = set()
    tokens = []
    for token in raw_tokens:
        token = token.strip()
        if not token:
            continue
        bot_id = _bot_id_from_token(token)
        if bot_id in seen_ids:
            continue
        seen_ids.add(bot_id)
        tokens.append(token)

    if not tokens:
        logger.error("MONITOR_BOT_TOKEN not set!")
        return

    logger.info(f"🚀 Starting Multi-Bot Listener with {len(tokens)} bot(s)...")
    redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)

    if os.name != 'nt':
        loop = asyncio.get_running_loop()
        def _handle_signal(): stop_event.set()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _handle_signal)

    tasks = []
    for i, token in enumerate(tokens):
        tasks.append(asyncio.create_task(_run_bot(token, is_primary=(i == 0))))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for tok, result in zip(tokens, results, strict=True):
        if isinstance(result, Exception):
            logger.error(f"Bot {_bot_id_from_token(tok)} supervisor crashed: {result!r}")
    await redis_client.aclose()
    if stop_event.is_set():
        logger.info("Bye! (graceful shutdown)")
    else:
        logger.error("All bot supervisors exited without a stop signal — see errors above.")
