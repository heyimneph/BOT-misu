import discord
import logging
import aiosqlite
import os
from datetime import datetime
import random

from discord.ext import commands
from discord import app_commands
from discord.ui import View, Select, Button

from core.utils import log_command_usage, check_permissions

# ---------------------------------------------------------------------------------------------------------------------
# Database Configuration
# ---------------------------------------------------------------------------------------------------------------------
os.makedirs('./data/databases', exist_ok=True)
os.makedirs('./data/card_images', exist_ok=True)

db_path = './data/databases/tcg.db'

# ---------------------------------------------------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------------------------------------------------
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------------------------------------------------
# Rarity Cog Class
# ---------------------------------------------------------------------------------------------------------------------
class RarityCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        async with aiosqlite.connect(db_path) as conn:
            default_rarities = [
                ("Common", 1.0, 10),
                ("Uncommon", 0.5, 20),
                ("Rare", 0.2, 50),
                ("Legendary", 0.01, 100)
            ]

            guild_ids = [guild.id for guild in self.bot.guilds]

            for guild_id in guild_ids:
                for rarity, weight, burn_value in default_rarities:
                    await conn.execute(
                        """
                        INSERT OR IGNORE INTO rarity_weights (guild_id, rarity, weight, burn_value)
                        VALUES (?, ?, ?, ?)
                        """,
                        (guild_id, rarity, weight, burn_value)
                    )
            await conn.commit()

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        async with aiosqlite.connect(db_path) as conn:
            default_rarities = [
                ("Common", 1.0, 10),  # Added burn_value
                ("Uncommon", 0.5, 20),
                ("Rare", 0.2, 50),
                ("Legendary", 0.01, 100)
            ]
            for rarity, weight, burn_value in default_rarities:
                await conn.execute(
                    """
                    INSERT OR IGNORE INTO rarity_weights (guild_id, rarity, weight, burn_value)
                    VALUES (?, ?, ?, ?)
                    """,
                    (guild.id, rarity, weight, burn_value)
                )
            await conn.commit()

    # ---------------------------------------------------------------------------------------------------------------------
    # Rarity Autocomplete
    # ---------------------------------------------------------------------------------------------------------------------
    async def rarity_autocomplete(self, interaction: discord.Interaction, current: str):
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute("SELECT DISTINCT rarity FROM rarity_weights WHERE guild_id = ?",
                                        (interaction.guild.id,))
            rarities = [row[0] for row in await cursor.fetchall()]

        # Filter based on user input (if they started typing)
        return [
            app_commands.Choice(name=rarity, value=rarity)
            for rarity in rarities if current.lower() in rarity.lower()]

    # ---------------------------------------------------------------------------------------------------------------------
    # Rarity Management Commands
    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="Admin: Set the weight and burn value for a rarity type")
    @app_commands.describe(
        rarity="The rarity type",
        weight="The weight for the rarity",
        burn_value="The burn value for the rarity"
    )
    @app_commands.autocomplete(rarity=rarity_autocomplete)
    async def rarity_set_weight(self, interaction: discord.Interaction, rarity: str, weight: float, burn_value: int):
        if not await check_permissions(interaction):
            await interaction.response.send_message(
                "You do not have permission to use this command. An Admin needs to `/authorise` you!",
                ephemeral=True
            )
            return

        if weight > 1.0 or weight < 0:
            await interaction.response.send_message(
                "Invalid weight! The rarity weight must be between **0.0** and **1.0**.",
                ephemeral=True
            )
            return

        try:
            async with aiosqlite.connect(db_path) as conn:
                await conn.execute(
                    "INSERT INTO rarity_weights (guild_id, rarity, weight, burn_value) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(guild_id, rarity) DO UPDATE SET weight = excluded.weight, burn_value = excluded.burn_value",
                    (interaction.guild.id, rarity, weight, burn_value)
                )
                await conn.commit()

            await interaction.response.send_message(
                f"Rarity weight for `{rarity}` set to `{weight}` and burn value set to `{burn_value}`.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message("An error occurred while setting rarity weight and burn value.", ephemeral=True)
            logger.error(f"Error in rarity_set_weight: {e}")
        finally:
            await log_command_usage(self.bot, interaction)

    @app_commands.command(description="Admin: Create a new rarity type")
    @app_commands.describe(
        rarity="The name of the new rarity type",
        weight="The weight for the new rarity",
        burn_value="The burn value for the new rarity"
    )
    async def rarity_create(self, interaction: discord.Interaction, rarity: str, weight: float, burn_value: int):
        if not await check_permissions(interaction):
            await interaction.response.send_message(
                "You do not have permission to use this command. An Admin needs to `/authorise` you!",
                ephemeral=True
            )
            return

        if weight > 1.0 or weight < 0.0:
            await interaction.response.send_message(
                "Invalid weight! The rarity weight must be between **0.0** and **1.0**.",
                ephemeral=True
            )
            return

        try:
            async with aiosqlite.connect(db_path) as conn:
                await conn.execute(
                    "INSERT INTO rarity_weights (guild_id, rarity, weight, burn_value) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(guild_id, rarity) DO UPDATE SET weight = excluded.weight, burn_value = excluded.burn_value",
                    (interaction.guild.id, rarity, weight, burn_value)
                )
                await conn.commit()

            await interaction.response.send_message(
                f"New rarity type `{rarity}` created with weight `{weight}` and burn value `{burn_value}`.",
                ephemeral=True)
        except Exception as e:
            await interaction.response.send_message("An error occurred while creating the rarity.", ephemeral=True)
            logger.error(f"Error in rarity_create: {e}")
        finally:
            await log_command_usage(self.bot, interaction)

    @app_commands.command(description="Admin: Remove a rarity type")
    @app_commands.describe(rarity="The rarity type to remove")
    @app_commands.autocomplete(rarity=rarity_autocomplete)
    async def rarity_remove(self, interaction: discord.Interaction, rarity: str):
        if not await check_permissions(interaction):
            await interaction.response.send_message(
                "You do not have permission to use this command. An Admin needs to `/authorise` you!",
                ephemeral=True
            )
            return

        try:
            async with aiosqlite.connect(db_path) as conn:
                cursor = await conn.execute("SELECT 1 FROM rarity_weights WHERE guild_id = ? AND rarity = ?",
                                            (interaction.guild.id, rarity))
                exists = await cursor.fetchone()

                if not exists:
                    await interaction.response.send_message(f"Error: Rarity `{rarity}` does not exist.", ephemeral=True)
                    return

                await conn.execute("DELETE FROM rarity_weights WHERE guild_id = ? AND rarity = ?",
                                   (interaction.guild.id, rarity))
                await conn.commit()

            await interaction.response.send_message(f"Rarity `{rarity}` has been removed.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message("An error occurred while removing the rarity.", ephemeral=True)
            logger.error(f"Error in rarity_remove: {e}")
        finally:
            await log_command_usage(self.bot, interaction)

    @app_commands.command(description="Admin: List all available rarities")
    async def rarity_list(self, interaction: discord.Interaction):
        try:
            async with aiosqlite.connect(db_path) as conn:
                cursor = await conn.execute("SELECT rarity, weight, burn_value FROM rarity_weights WHERE guild_id = ?",
                                            (interaction.guild.id,))
                rarities = await cursor.fetchall()

            if not rarities:
                await interaction.response.send_message("No rarities have been set up for this server yet.",
                                                        ephemeral=True)
                return

            rarity_list = "\n".join(f"- `{rarity}` (Weight: {weight}, Burn Value: {burn_value})" for rarity, weight, burn_value in rarities)
            await interaction.response.send_message(f"**Available Rarities:**\n{rarity_list}", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message("An error occurred while retrieving the rarities.", ephemeral=True)
            logger.error(f"Error in rarity_list: {e}")
        finally:
            await log_command_usage(self.bot, interaction)

    @app_commands.command(description="Admin: Reset all rarities to default settings")
    async def rarity_reset(self, interaction: discord.Interaction):
        if not await check_permissions(interaction):
            await interaction.response.send_message(
                "You do not have permission to use this command. An Admin needs to `/authorise` you!",
                ephemeral=True
            )
            return

        default_rarities = [
            ("Common", 1.0, 10),
            ("Uncommon", 0.5, 20),
            ("Rare", 0.2, 50),
            ("Legendary", 0.01, 100)
        ]

        try:
            async with aiosqlite.connect(db_path) as conn:
                await conn.execute("DELETE FROM rarity_weights WHERE guild_id = ?", (interaction.guild.id,))
                for rarity, weight, burn_value in default_rarities:
                    await conn.execute(
                        "INSERT INTO rarity_weights (guild_id, rarity, weight, burn_value) VALUES (?, ?, ?, ?)",
                        (interaction.guild.id, rarity, weight, burn_value)
                    )
                await conn.commit()

            await interaction.response.send_message("All rarities have been reset to their default settings.",
                                                    ephemeral=True)
        except Exception as e:
            await interaction.response.send_message("An error occurred while resetting rarities.", ephemeral=True)
            logger.error(f"Error in rarity_reset: {e}")
        finally:
            await log_command_usage(self.bot, interaction)


# ---------------------------------------------------------------------------------------------------------------------
# Setup Function
# ---------------------------------------------------------------------------------------------------------------------
async def setup(bot):
    async with aiosqlite.connect(db_path) as conn:
        # Create the table if it doesn't exist
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS rarity_weights (
                guild_id INTEGER,
                rarity TEXT,
                weight REAL,
                burn_value INTEGER,
                PRIMARY KEY (guild_id, rarity)
            )
        ''')

        # Check if the burn_value column exists
        cursor = await conn.execute("PRAGMA table_info(rarity_weights)")
        columns = await cursor.fetchall()
        column_names = [column[1] for column in columns]  # Column names are in the second position

        if 'burn_value' not in column_names:
            # Add the burn_value column
            await conn.execute('ALTER TABLE rarity_weights ADD COLUMN burn_value INTEGER')
            logger.info("Added 'burn_value' column to 'rarity_weights' table.")

        # Define default rarities with their correct burn values
        default_rarities = [
            ("Common", 1.0, 10),
            ("Uncommon", 0.5, 20),
            ("Rare", 0.2, 50),
            ("Legendary", 0.01, 100)
        ]

        # Ensure default rarities have the correct burn values
        for rarity, weight, burn_value in default_rarities:
            await conn.execute('''
                UPDATE rarity_weights
                SET burn_value = ?
                WHERE rarity = ? AND (burn_value IS NULL OR burn_value = '')
            ''', (burn_value, rarity))

        # Ensure custom rarities do not have NULL burn_value (set to 1 if missing)
        await conn.execute('''
            UPDATE rarity_weights
            SET burn_value = 1
            WHERE burn_value IS NULL OR burn_value = ''
        ''')

        # Ensure each guild has default rarities if they are missing
        guild_ids = [guild.id for guild in bot.guilds]

        for guild_id in guild_ids:
            for rarity, weight, burn_value in default_rarities:
                await conn.execute('''
                    INSERT INTO rarity_weights (guild_id, rarity, weight, burn_value)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(guild_id, rarity) DO NOTHING
                ''', (guild_id, rarity, weight, burn_value))

        await conn.commit()

    await bot.add_cog(RarityCog(bot))
