import discord
import logging
import aiosqlite
import os
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import has_permissions

from core.utils import log_command_usage, check_permissions

# Ensure the database directory exists
os.makedirs('./data/databases', exist_ok=True)

# Path to the SQLite database
db_path = './data/databases/tcg.db'

# ---------------------------------------------------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------------------------------------------------

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------------------------------------------------
# Customisation Functions
# ---------------------------------------------------------------------------------------------------------------------

async def get_bio_settings():
    try:
        async with aiosqlite.connect(db_path) as conn:
            async with conn.execute('SELECT value FROM customisation WHERE type = ?', ("activity_type",)) as cursor:
                activity_type_doc = await cursor.fetchone()
            async with conn.execute('SELECT value FROM customisation WHERE type = ?', ("bio",)) as cursor:
                bio_doc = await cursor.fetchone()
            if activity_type_doc and bio_doc:
                return activity_type_doc[0], bio_doc[0]
            return None, None
    except Exception as e:
        logger.error(f"Failed to retrieve bio settings: {e}")
        return None, None

# ---------------------------------------------------------------------------------------------------------------------
# Customisation Cog
# ---------------------------------------------------------------------------------------------------------------------
class CustomisationCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

    async def owner_check(self, interaction: discord.Interaction):
        owner_id = 111941993629806592
        return interaction.user.id == owner_id

    # ---------------------------------------------------------------------------------------------------------------------
    @app_commands.command(description="Owner: Change the bot's avatar")
    async def change_avatar(self, interaction: discord.Interaction, url: str):
        if not await self.owner_check(interaction):
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return

        try:
            async with self.bot.http._HTTPClient__session.get(url) as response:
                data = await response.read()
                await self.bot.user.edit(avatar=data)

            await interaction.response.send_message("`Success: Avatar Changed!`")

        except Exception as e:
            await interaction.followup.send("`Error: Something Unexpected Happened`")
            logger.error(f"Failed to change avatar: {e}")
        finally:
            await log_command_usage(self.bot, interaction)

    # ---------------------------------------------------------------------------------------------------------------------
    @app_commands.command(description="Admin: Set Embed Colour. Use the format #C4A7EC")
    async def set_embed_colour(self, interaction: discord.Interaction, colour: str):
        if not await check_permissions(interaction):
            await interaction.response.send_message("You do not have permission to use this command. "
                                                    "An Admin needs to `/authorise` you!",
                                                    ephemeral=True)
            return

        async with aiosqlite.connect(db_path) as conn:
            try:
                if colour.startswith("#"):
                    color = colour[1:]
                else:
                    color = colour

                color_obj = discord.Color(int(color, 16))

                async with conn.execute(
                    'SELECT value FROM customisation WHERE type = ? AND guild_id = ?',
                    ("embed_color", interaction.guild_id)) as cursor:
                    embed_color_doc = await cursor.fetchone()

                if not embed_color_doc:
                    await conn.execute(
                        'INSERT INTO customisation (guild_id, type, value) VALUES (?, ?, ?)',
                        (interaction.guild_id, "embed_color", color))
                else:
                    await conn.execute(
                        'UPDATE customisation SET value = ? WHERE type = ? AND guild_id = ?',
                        (color, "embed_color", interaction.guild_id))

                await conn.commit()

                await interaction.response.send_message(f"`Success: Embed color has been set to #{color}!`",
                                                        ephemeral=True)

            except ValueError:
                await interaction.response.send_message(
                    "`Error: Invalid color format! Please provide a valid hexadecimal color value.`", ephemeral=True)
            except Exception as e:
                logger.error(f"An error occurred: {str(e)}")
                await interaction.followup.send(f"`Error: {e}`", ephemeral=True)
            finally:
                await log_command_usage(self.bot, interaction)

    # ---------------------------------------------------------------------------------------------------------------------
    @app_commands.command(description="Owner: Change Bot's Bio")
    async def set_bio(self, interaction: discord.Interaction, activity_type: str, bio: str):
        if not await self.owner_check(interaction):
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return

        async with aiosqlite.connect(db_path) as conn:
            try:
                if activity_type.lower() == "playing":
                    activity = discord.Game(name=bio)
                elif activity_type.lower() == "listening":
                    activity = discord.Activity(type=discord.ActivityType.listening, name=bio)
                elif activity_type.lower() == "watching":
                    activity = discord.Activity(type=discord.ActivityType.watching, name=bio)
                else:
                    await interaction.response.send_message(
                        "`Error: Invalid activity type! Choose from playing, listening, or watching.`", ephemeral=True)
                    return

                await self.bot.change_presence(activity=activity)

                # Store the bio settings in the database
                await conn.execute('INSERT INTO customisation (guild_id, type, value) VALUES (?, ?, ?) '
                                   'ON CONFLICT(guild_id, type) DO UPDATE SET value=excluded.value',
                                   (interaction.guild_id, "activity_type", activity_type))
                await conn.execute('INSERT INTO customisation (guild_id, type, value) VALUES (?, ?, ?) '
                                   'ON CONFLICT(guild_id, type) DO UPDATE SET value=excluded.value',
                                   (interaction.guild_id, "bio", bio))
                await conn.commit()

                # Send a confirmation message
                await interaction.response.send_message(f"`Success: Bot's activity has been set to {activity_type} '{bio}'`", ephemeral=True)

            except Exception as e:
                logger.error(f"An error occurred: {str(e)}")
                await interaction.followup.send(f"`Error: {e}`", ephemeral=True)
            finally:
                await log_command_usage(self.bot, interaction)

    @set_bio.autocomplete("activity_type")
    async def activity_type_autocomplete(self, interaction: discord.Interaction, current: str):
        activity_types = ["playing", "listening", "watching"]
        return [app_commands.Choice(name=atype, value=atype) for atype in activity_types if current.lower() in atype.lower()]

# ---------------------------------------------------------------------------------------------------------------------
# Setup Function
# ---------------------------------------------------------------------------------------------------------------------

async def setup(bot):
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute('''
        CREATE TABLE IF NOT EXISTS customisation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            value TEXT NOT NULL,
            UNIQUE(guild_id, type)
        )
        ''')
        await conn.commit()
    await bot.add_cog(CustomisationCog(bot))
