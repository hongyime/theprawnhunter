import asyncio
import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.config import settings
from telethon import TelegramClient

async def get_names():
    # Paths from DB
    paths = [
        '/app/sessions/account_00000000_1779153362',
        '/app/sessions/account_6584731565_1779170779'
    ]
    
    for path in paths:
        # Note: path in DB doesn't have .session, but file on disk might
        full_path = path if os.path.exists(path + ".session") else path
        print(f"Checking {full_path}...")
        client = TelegramClient(full_path, settings.TELEGRAM_API_ID, settings.TELEGRAM_API_HASH)
        try:
            await client.connect()
            if await client.is_user_authorized():
                me = await client.get_me()
                print(f"Account {full_path}: {me.first_name} (@{me.username or 'No Username'})")
            else:
                print(f"Account {full_path}: NOT AUTHORIZED")
        except Exception as e:
            print(f"Account {full_path}: Error {e}")
        finally:
            await client.disconnect()

if __name__ == "__main__":
    asyncio.run(get_names())
