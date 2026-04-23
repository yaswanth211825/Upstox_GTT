"""
Fetch today's raw messages from MS-OPTIONS-PREMIUM-📈📉 via existing Telethon session.
Dumps each message with timestamp so we can see the real signal formats.

Usage:
    cd /Users/yash_2111825/Projects/UpstoxGTT
    python3 fetch_today_signals.py
"""
import asyncio
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(".env")

import sys
sys.path.insert(0, str(Path(__file__).parent))
from telethon import TelegramClient
from entity_finder import resolve_entity

TELEGRAM_API_ID   = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_PHONE    = os.getenv("TELEGRAM_PHONE", "")
SESSION_PATH      = str(Path("data/session_name"))
GROUP_NAME        = os.getenv("GROUP_ENTITY_NAME", "MS-OPTIONS-PREMIUM")

IST = timezone(timedelta(hours=5, minutes=30))


async def main():
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        print("ERROR: Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env")
        return

    client = TelegramClient(SESSION_PATH, TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await client.start(phone=TELEGRAM_PHONE)

    entity = await resolve_entity(client, GROUP_NAME)
    if not entity:
        print(f"ERROR: Could not resolve group '{GROUP_NAME}'")
        await client.disconnect()
        return

    group_title = getattr(entity, "title", GROUP_NAME)
    print(f"Fetching today's messages from: {group_title}  (id={entity.id})\n{'='*60}")

    today_ist = datetime.now(IST).date()
    since_date = today_ist - timedelta(days=3)   # last 3 days if today is empty
    messages = []

    async for msg in client.iter_messages(entity, limit=500):
        msg_date = msg.date.astimezone(IST).date()
        if msg_date < since_date:
            break
        if msg.text:
            messages.append(msg)

    # Print oldest-first
    messages.reverse()
    today_msgs = [m for m in messages if m.date.astimezone(IST).date() == today_ist]
    print(f"Found {len(today_msgs)} messages today, {len(messages)} in last 3 days ({since_date} → {today_ist})\n")

    for i, msg in enumerate(messages, 1):
        ts = msg.date.astimezone(IST).strftime("%H:%M:%S")
        sender = getattr(msg.sender, "first_name", None) or getattr(msg.sender, "title", "?")
        print(f"[{i:03d}] {ts}  {sender}")
        print(f"      {msg.text!r}")
        print()

    await client.disconnect()
    print(f"{'='*60}\nTotal: {len(messages)} messages")


if __name__ == "__main__":
    asyncio.run(main())
