import discord
import aiosqlite
import os
from discord import app_commands
from discord.ext import commands
from config import client, perform_sync
from core.utils import log_command_usage, check_permissions, get_embed_colour

# ---------------------------------------------------------------------------------------------------------------------
# Database Configuration
# ---------------------------------------------------------------------------------------------------------------------
os.makedirs('./data/databases', exist_ok=True)
db_path = './data/databases/tcg.db'


# ---------------------------------------------------------------------------------------------------------------------
# Admin Class
# ---------------------------------------------------------------------------------------------------------------------
class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def owner_check(self, interaction: discord.Interaction):
        owner_id = 111941993629806592
        return interaction.user.id == owner_id

    # ---------------------------------------------------------------------------------------------------------------------
    # Admin Commands
    # ---------------------------------------------------------------------------------------------------------------------
    @app_commands.command(description="Owner: Reset a specific table in the database")
    async def reset_table(self, interaction: discord.Interaction, table_name: str):
        if not await self.owner_check(interaction):
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return

        await interaction.response.defer()

        try:
            # Connect to the database
            async with aiosqlite.connect(db_path) as conn:
                # Fetch the schema for the specified table
                cursor = await conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name = ?",
                    (table_name,)
                )
                schema = await cursor.fetchone()
                await cursor.close()

                if not schema:
                    await interaction.followup.send(f'`Error: No table found with name {table_name}`')
                    return

                # Drop the specified table
                await conn.execute(f'DROP TABLE IF EXISTS {table_name}')
                # Recreate the table using the fetched schema
                await conn.execute(schema[0])
                await conn.commit()

            await interaction.followup.send(f'`Success: {table_name} table has been reset`')
        except Exception as e:
            await interaction.followup.send(f'`Error: Failed to reset {table_name} table. {str(e)}`')

    @app_commands.command(description="Owner: Delete a specific table from the database")
    async def delete_table(self, interaction: discord.Interaction, table_name: str):
        if not await self.owner_check(interaction):
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return

        await interaction.response.defer()

        try:
            # Connect to the database
            async with aiosqlite.connect(db_path) as conn:
                # Check if the table exists before attempting to delete
                cursor = await conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
                    (table_name,)
                )
                exists = await cursor.fetchone()
                await cursor.close()

                if not exists:
                    await interaction.followup.send(f'`Error: No table found with name {table_name}`')
                    return

                # Delete the specified table
                await conn.execute(f'DROP TABLE IF EXISTS {table_name}')
                await conn.commit()

            await interaction.followup.send(f'`Success: {table_name} table has been deleted`')
        except Exception as e:
            await interaction.followup.send(f'`Error: Failed to delete {table_name} table. {str(e)}`')

    # ---------------------------------------------------------------------------------------------------------------------
    # Owner Commands
    # ---------------------------------------------------------------------------------------------------------------------
    @app_commands.command(description="Owner: Load a Cog")
    async def load(self, interaction: discord.Interaction, extension: str):
        if not await self.owner_check(interaction):
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return

        try:
            await interaction.response.defer()
            await client.load_extension(f'cogs.{extension}')
            await interaction.followup.send(f'`Success: Loaded {extension}`')
            await perform_sync()
        except Exception as e:
            await interaction.followup.send(f'`Error: Failed to load {extension}`')

    @app_commands.command(description="Owner: Unload a Cog")
    async def unload(self, interaction: discord.Interaction, extension: str):
        if not await self.owner_check(interaction):
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return

        try:
            await interaction.response.defer()
            await client.unload_extension(f'cogs.{extension}')
            await interaction.followup.send(f'`Success: Unloaded {extension}`')
        except Exception as e:
            await interaction.followup.send(f'`Error: Failed to unload {extension}`')

    @app_commands.command(description="Owner: Reload a Cog")
    async def reload(self, interaction: discord.Interaction, extension: str):
        if not await self.owner_check(interaction):
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return

        try:
            await interaction.response.defer()
            await client.unload_extension(f'cogs.{extension}')
            await client.load_extension(f'cogs.{extension}')
            await interaction.followup.send(f'Reloaded {extension}.')
            await perform_sync()
        except Exception as e:
            await interaction.followup.send(f'`Error: Failed to reload {extension}`')


# ---------------------------------------------------------------------------------------------------------------------
# Setup Function
# ---------------------------------------------------------------------------------------------------------------------
async def setup(bot):
    await bot.add_cog(AdminCog(bot))
