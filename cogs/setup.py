import discord
import aiosqlite
import os
import logging

from discord import app_commands
from discord.ext import commands

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
# Setup Class
# ---------------------------------------------------------------------------------------------------------------------
class SetupCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db_path = './data/databases/tcg.db'

    async def owner_check(self, interaction: discord.Interaction):
        owner_id = 111941993629806592
        return interaction.user.id == owner_id

    # ---------------------------------------------------------------------------------------------------------------------
    # Setup Commands
    # ---------------------------------------------------------------------------------------------------------------------
    @app_commands.command(description="Owner: Run the setup for Misu")
    async def setup(self, interaction: discord.Interaction):
        if not await self.owner_check(interaction):
            await interaction.response.send_message("You do not have permission to use this command. "
                                                    "This command is for the Bot Owner only.",
                                                    ephemeral=True)
            return
        try:
            guild = interaction.guild

            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }

            # Grant access to admins
            for role in guild.roles:
                if role.permissions.administrator:
                    overwrites[role] = discord.PermissionOverwrite(read_messages=True)

            # Check for existing channels or create new ones
            log_channel = discord.utils.get(guild.text_channels, name='misu_logs')
            card_channel = discord.utils.get(guild.text_channels, name='misu_images')

            if not log_channel:
                log_channel = await guild.create_text_channel('misu_logs', overwrites=overwrites)
                await log_channel.send("Welcome to the Misu Logs Channel! This channel will be used for logging various events and actions.")
            if not card_channel:
                card_channel = await guild.create_text_channel('misu_images', overwrites=overwrites)
                await card_channel.send("Welcome to the Misu Images Channel! This channel will be used to store and view card images.")

            # Insert or update the configuration in the database
            async with aiosqlite.connect(self.db_path) as conn:
                await conn.execute('''
                    INSERT INTO config (guild_id, log_channel_id, card_channel_id)
                    VALUES (?, ?, ?)
                    ON CONFLICT(guild_id) DO UPDATE SET
                        log_channel_id = excluded.log_channel_id,
                        card_channel_id = excluded.card_channel_id
                ''', (guild.id, log_channel.id, card_channel.id))
                await conn.commit()

            await interaction.response.send_message('Setup completed! Channels created and configurations saved.', ephemeral=True)
        except Exception as e:
            logger.error(f"Error with setup command: {e}")
            await interaction.response.send_message(f"An unexpected error occurred: {e}", ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)

# ---------------------------------------------------------------------------------------------------------------------
# Setup Function
# ---------------------------------------------------------------------------------------------------------------------
async def setup(bot):
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS config (
                guild_id INTEGER PRIMARY KEY,
                log_channel_id TEXT,
                card_channel_id TEXT
            )
        ''')
        await conn.commit()
    await bot.add_cog(SetupCog(bot))
