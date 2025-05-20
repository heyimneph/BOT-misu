import discord
import os
import logging
import aiosqlite

from discord import app_commands



# ---------------------------------------------------------------------------------------------------------------------
# Database Configuration
# ---------------------------------------------------------------------------------------------------------------------
os.makedirs('./data/databases', exist_ok=True)
db_path = './data/databases/tcg.db'

# ---------------------------------------------------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------------------------------------------------
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------------------------------------------------
# Autocompletes
# ---------------------------------------------------------------------------------------------------------------------

async def rarity_autocomplete(interaction: discord.Interaction, current: str):
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute("SELECT DISTINCT rarity FROM rarity_weights WHERE guild_id = ?",
                                    (interaction.guild.id,))
        rarities = [row[0] for row in await cursor.fetchall()]

    # Filter based on user input (if they started typing)
    return [
        app_commands.Choice(name=rarity, value=rarity)
        for rarity in rarities if current.lower() in rarity.lower()]

async def card_name_autocomplete(interaction: discord.Interaction, current: str):
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT name FROM cards WHERE name LIKE ? AND guild_id = ? LIMIT 25",
            (f'%{current}%', interaction.guild.id))
        cards = await cursor.fetchall()

    return [app_commands.Choice(name=card[0], value=card[0]) for card in cards]

async def set_name_autocomplete(interaction: discord.Interaction, current: str):
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT name FROM card_sets WHERE guild_id = ? AND name LIKE ? LIMIT 25",
            (interaction.guild.id, f"%{current}%"))
        sets = await cursor.fetchall()

    return [app_commands.Choice(name=set_name[0], value=set_name[0]) for set_name in sets]




async def non_preset_card_name_autocomplete(interaction: discord.Interaction, current: str):
    async with aiosqlite.connect(db_path) as conn:
        # Fetch cards that are NOT part of any preset set
        cursor = await conn.execute('''
            SELECT c.name 
            FROM cards AS c
            LEFT JOIN set_cards AS sc ON c.card_id = sc.card_id AND c.guild_id = sc.guild_id
            LEFT JOIN card_sets AS cs ON sc.set_id = cs.set_id AND cs.is_preset = 1
            WHERE c.guild_id = ? AND c.name LIKE ? AND cs.is_preset IS NULL
            GROUP BY c.name
            LIMIT 25
        ''', (interaction.guild.id, f'%{current}%'))
        cards = await cursor.fetchall()

    return [app_commands.Choice(name=card[0], value=card[0]) for card in cards]

