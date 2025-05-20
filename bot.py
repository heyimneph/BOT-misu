import discord
import aiosqlite
import asyncio
import os
from datetime import datetime, timezone
from config import client, DISCORD_TOKEN, perform_sync

# Ensure the database directory exists
os.makedirs('./data/databases', exist_ok=True)

# Path to SQLite database
db_path = './data/databases/tcg.db'

# ---------------------------------------------------------------------------------------------------------------------
# Customisation Functions
# ---------------------------------------------------------------------------------------------------------------------

async def get_embed_colour():
    async with aiosqlite.connect(db_path) as conn:
        async with conn.execute('SELECT value FROM customisation WHERE type = ?', ("embed_color",)) as cursor:
            row = await cursor.fetchone()
            if row:
                return int(row[0], 16)
            return 0x3498db

async def get_bio_settings():
    async with aiosqlite.connect(db_path) as conn:
        async with conn.execute('SELECT value FROM customisation WHERE type = ?', ("activity_type",)) as cursor:
            activity_type_doc = await cursor.fetchone()
        async with conn.execute('SELECT value FROM customisation WHERE type = ?', ("bio",)) as cursor:
            bio_doc = await cursor.fetchone()
        if activity_type_doc and bio_doc:
            return activity_type_doc[0], bio_doc[0]
        return None, None

# ---------------------------------------------------------------------------------------------------------------------
# Event Handlers
# ---------------------------------------------------------------------------------------------------------------------

@client.event
async def on_ready():
    print(f'Bot is logged in as {client.user.name} ({client.user.id})')
    synced_count = await perform_sync()
    print(f"{synced_count} commands synced")

# ---------------------------------------------------------------------------------------------------------------------
# Main Function
# ---------------------------------------------------------------------------------------------------------------------

async def main():
    await client.load_extension("core.initialisation")

    for filename in os.listdir('cogs'):
        if filename.endswith('.py'):
            await client.load_extension(f'cogs.{filename[:-3]}')
            print(f"Loading {filename[:-3]}...")

    print("Starting Bot...")

    await client.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
