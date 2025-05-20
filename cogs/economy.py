import discord
import aiosqlite
import os
import logging

from datetime import datetime, timedelta
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, View

from core.utils import log_command_usage, check_permissions

# Ensure the database directory exists
os.makedirs('./data/databases', exist_ok=True)

# Path to the SQLite database
db_path = './data/databases/tcg.db'

DEFAULT_VOICE_POINTS_PER_MINUTE = 2
DEFAULT_MESSAGE_COUNT_THRESHOLD = 100
DEFAULT_MESSAGE_REWARD_POINTS = 10
MIN_CHAR_LIMIT = 10
# ---------------------------------------------------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------------------------------------------------

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

#  ---------------------------------------------------------------------------------------------------------------------
#  Economy Class
#  ---------------------------------------------------------------------------------------------------------------------
class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.voice_tracking = {}

    async def ensure_economy_config(self):
        async with aiosqlite.connect(db_path) as db:
            for guild in self.bot.guilds:
                logger.debug(f"Ensuring economy config for guild ID: {guild.id}")
                await db.execute('''
                    INSERT OR IGNORE INTO economy_config (guild_id, voice_points_per_minute, message_count_threshold, message_reward_points)
                    VALUES (?, ?, ?, ?)
                ''', (guild.id, DEFAULT_VOICE_POINTS_PER_MINUTE, DEFAULT_MESSAGE_COUNT_THRESHOLD,
                      DEFAULT_MESSAGE_REWARD_POINTS))
            await db.commit()

    @commands.Cog.listener()
    async def on_ready(self):
        await self.ensure_economy_config()

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        async with aiosqlite.connect(db_path) as db:
            logger.debug(f"Ensuring economy config for new guild ID: {guild.id}")
            await db.execute('''
                INSERT OR IGNORE INTO economy_config (guild_id, voice_points_per_minute, message_count_threshold, message_reward_points)
                VALUES (?, ?, ?, ?)
            ''', (guild.id, DEFAULT_VOICE_POINTS_PER_MINUTE, DEFAULT_MESSAGE_COUNT_THRESHOLD,
                  DEFAULT_MESSAGE_REWARD_POINTS))
            await db.commit()


    def format_time_remaining(self, time_remaining: timedelta) -> str:
        days = time_remaining.days
        hours, remainder = divmod(time_remaining.seconds, 3600)
        minutes = remainder // 60

        day_str = "day" if days == 1 else "days"
        hour_str = "hour" if hours == 1 else "hours"
        minute_str = "minute" if minutes == 1 else "minutes"

        if days > 0:
            return f"{days} {day_str}, {hours} {hour_str}, and {minutes} {minute_str}"
        elif hours > 0:
            return f"{hours} {hour_str} and {minutes} {minute_str}"
        else:
            return f"{minutes} {minute_str}"

#  ---------------------------------------------------------------------------------------------------------------------
# Economy Commands
#  ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="User: Check your points balance")
    async def balance(self, interaction: discord.Interaction):
        try:
            async with aiosqlite.connect(db_path) as db:
                cursor = await db.execute(
                    "SELECT balance FROM economy WHERE user_id = ? AND guild_id = ?",
                    (interaction.user.id, interaction.guild_id),
                )
                row = await cursor.fetchone()

                if row is None:
                    message = "`You haven't claimed any points yet.`"
                else:
                    message = f"`Your balance: {row[0]} points`"

            await interaction.response.send_message(message, ephemeral=True)
        except Exception as e:
            logger.error(f"Error with Balance in Economy - {e}")
            await interaction.response.send_message(f"Error with Balance in Economy - {e}",
                                                    ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)

    #  ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="Admin: Add points to a user")
    async def points_add(self, interaction: discord.Interaction, user: discord.Member, points: int):
        if not await check_permissions(interaction):
            await interaction.response.send_message("You do not have permission to use this command. "
                                                    "An Admin needs to `/authorise` you!",
                                                    ephemeral=True)
            return

        try:
            if points <= 0:
                await interaction.response.send_message("`Points must be a positive number.`")
                return

            async with aiosqlite.connect(db_path) as db:
                cursor = await db.execute(
                    "SELECT balance FROM economy WHERE user_id = ? AND guild_id = ?",
                    (user.id, interaction.guild_id),
                )
                row = await cursor.fetchone()

                if row is None:
                    await db.execute(
                        "INSERT INTO economy (user_id, guild_id, balance) VALUES (?, ?, ?)",
                        (user.id, interaction.guild_id, points),
                    )
                else:
                    await db.execute(
                        "UPDATE economy SET balance = balance + ? WHERE user_id = ? AND guild_id = ?",
                        (points, user.id, interaction.guild_id),
                    )
                await db.commit()
            await interaction.response.send_message(f"Added `{points}` points to `{user.name}'s` balance.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error with Add in Economy - {e}")
            await interaction.response.send_message(f"Error with Add in Economy - {e}", ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)
    #  ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="Admin: Remove points from a user")
    async def points_remove(self, interaction: discord.Interaction, user: discord.Member, points: int):
        if not await check_permissions(interaction):
            await interaction.response.send_message("You do not have permission to use this command. "
                                                    "An Admin needs to `/authorise` you!",
                                                    ephemeral=True)
            return

        try:
            if points <= 0:
                await interaction.followup.send("`Points must be a positive number.`")
                return

            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "UPDATE economy SET balance = balance - ? WHERE user_id = ? AND guild_id = ?",
                    (points, user.id, interaction.guild_id),
                )
                await db.commit()
            await interaction.response.send_message(
                f"Removed `{points}` points from `{user.name}'s` balance.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error with Remove in Economy - {e}")
            await interaction.response.send_message(f"Error with Remove in Economy - {e}",
                                                    ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)

    #  ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="User: Give some of your points to a user")
    async def points_give(self, interaction: discord.Interaction, user: discord.Member, points: int):
        try:
            if points <= 0:
                await interaction.response.send_message("`Points must be a positive number.`")
                return

            async with aiosqlite.connect(db_path) as db:
                cursor = await db.execute(
                    "SELECT balance FROM economy WHERE user_id = ? AND guild_id = ?",
                    (interaction.user.id, interaction.guild_id),
                )
                giver_row = await cursor.fetchone()

                if giver_row is None or giver_row[0] < points:
                    await interaction.response.send_message("`You don't have enough points.`")
                else:
                    await db.execute(
                        "UPDATE economy SET balance = balance - ? WHERE user_id = ? AND guild_id = ?",
                        (points, interaction.user.id, interaction.guild_id),
                    )

                    cursor = await db.execute(
                        "SELECT balance FROM economy WHERE user_id = ? AND guild_id = ?",
                        (user.id, interaction.guild_id),
                    )
                    receiver_row = await cursor.fetchone()

                    if receiver_row is None:
                        await db.execute(
                            "INSERT INTO economy (user_id, guild_id, balance) VALUES (?, ?, ?)",
                            (user.id, interaction.guild_id, points),
                        )
                    else:
                        await db.execute(
                            "UPDATE economy SET balance = balance + ? WHERE user_id = ? AND guild_id = ?",
                            (points, user.id, interaction.guild_id),
                        )

                    await db.commit()
                    await interaction.response.send_message(
                        f"You gave `{points}` points to {user.name}", ephemeral=True
                    )

        except Exception as e:
            logger.error(f"Error with Give in Economy - {e}")
            await interaction.response.send_message(
                f"Error with Give in Economy - {e}", ephemeral=True
            )
        finally:
            await log_command_usage(self.bot, interaction)

# ------------------------------------------------------------------------------------------------------------------
# Point Configs
# ------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="Admin: Set points per minute of voice chat")
    @app_commands.describe(points="Points to be gained by time spent in Voice (per minute).")
    async def set_voice_points(self, interaction: discord.Interaction, points: int):
        if not await check_permissions(interaction):
            await interaction.response.send_message("You do not have permission to use this command. "
                                                    "An Admin needs to `/authorise` you!",
                                                    ephemeral=True)
            return

        try:
            if points <= 0:
                await interaction.response.send_message("Points per minute must be a positive number.", ephemeral=True)
                return

            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO economy_config (guild_id, voice_points_per_minute) VALUES (?, ?)",
                    (interaction.guild_id, points)
                )
                await db.commit()
            await interaction.response.send_message(
                f"Set voice chat points to `{points}` per minute.", ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error with Voice Points in Economy - {e}")
            await interaction.response.send_message(
                f"Error with Voice Points  in Economy - {e}", ephemeral=True
            )
        finally:
            await log_command_usage(self.bot, interaction)

    @app_commands.command(description="Admin: Set message count threshold and reward")
    @app_commands.describe(points="Points to be claimed by sending messages!")
    async def set_message_reward(
            self, interaction: discord.Interaction, message_count: int, points: int
    ):
        if not await check_permissions(interaction):
            await interaction.response.send_message("You do not have permission to use this command. "
                                                    "An Admin needs to `/authorise` you!",
                                                    ephemeral=True)
            return

        try:

            if message_count <= 0 or points <= 0:
                await interaction.response.send_message(
                    "Message count and points must be positive numbers.", ephemeral=True
                )
                return

            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO economy_config (guild_id, message_count_threshold, message_reward_points) VALUES (?, ?, ?)",
                    (interaction.guild_id, message_count, points)
                )
                await db.commit()
            await interaction.response.send_message(
                f"Set message reward to `{points}` points for every `{message_count}` messages.",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Error with Message Reward in Economy - {e}")
            await interaction.response.send_message(
                f"Error with Message Reward in Economy - {e}", ephemeral=True
            )
        finally:
            await log_command_usage(self.bot, interaction)

# ------------------------------------------------------------------------------------------------------------------
# Event Listeners
# ------------------------------------------------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot:
            return
        try:
            if before.channel is None and after.channel is not None:
                self.voice_tracking[member.id] = datetime.now()
            elif before.channel is not None and after.channel is None:
                if member.id in self.voice_tracking:
                    start_time = self.voice_tracking.pop(member.id)
                    time_spent = datetime.now() - start_time
                    minutes_spent = time_spent.total_seconds() // 60

                    async with aiosqlite.connect(db_path) as db:
                        cursor = await db.execute(
                            "SELECT voice_points_per_minute FROM economy_config WHERE guild_id = ?",
                            (member.guild.id,),
                        )
                        config = await cursor.fetchone()
                        if config:
                            points_earned = minutes_spent * config[0]

                            # Check if the user exists in the economy table
                            cursor = await db.execute(
                                "SELECT balance FROM economy WHERE user_id = ? AND guild_id = ?",
                                (member.id, member.guild.id),
                            )
                            user = await cursor.fetchone()
                            if user:
                                await db.execute(
                                    "UPDATE economy SET balance = balance + ? WHERE user_id = ? AND guild_id = ?",
                                    (points_earned, member.id, member.guild.id),
                                )
                            else:
                                await db.execute(
                                    "INSERT INTO economy (user_id, guild_id, balance, message_count) VALUES (?, ?, ?, 0)",
                                    (member.id, member.guild.id, points_earned),
                                )
                            await db.commit()
        except Exception as e:
            logger.error(f"Error in on_voice_state_update: {e}")


    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return
        try:

            async with aiosqlite.connect(db_path) as db:
                cursor = await db.execute(
                    "SELECT message_count FROM economy WHERE user_id = ? AND guild_id = ?",
                    (message.author.id, message.guild.id),
                )
                user = await cursor.fetchone()
                if user:
                    await db.execute(
                        "UPDATE economy SET message_count = message_count + 1 WHERE user_id = ? AND guild_id = ?",
                        (message.author.id, message.guild.id),
                    )
                else:
                    await db.execute(
                        "INSERT INTO economy (user_id, guild_id, balance, message_count) VALUES (?, ?, 0, 1)",
                        (message.author.id, message.guild.id),
                    )

                cursor = await db.execute(
                    "SELECT message_count, message_count_threshold, message_reward_points FROM economy "
                    "JOIN economy_config ON economy.guild_id = economy_config.guild_id "
                    "WHERE user_id = ? AND economy.guild_id = ?",
                    (message.author.id, message.guild.id),
                )
                row = await cursor.fetchone()
                if row and row[0] >= row[1]:
                    await db.execute(
                        "UPDATE economy SET balance = balance + ?, message_count = 0 WHERE user_id = ? AND guild_id = ?",
                        (row[2], message.author.id, message.guild.id),
                    )
                await db.commit()
        except Exception as e:
                logger.error(f"Error in on_message: {e}")

#  ---------------------------------------------------------------------------------------------------------------------
#  Setup Function
#  ---------------------------------------------------------------------------------------------------------------------
async def setup(bot):
    async with aiosqlite.connect(db_path) as conn:
        # Create the economy table if it doesn't exist
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS economy (
                guild_id INTEGER,
                user_id INTEGER,
                balance INTEGER DEFAULT 0,
                message_count INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
        ''')

        # Create the economy_config table if it doesn't exist
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS economy_config (
                guild_id INTEGER PRIMARY KEY,
                voice_points_per_minute INTEGER DEFAULT 2,
                message_count_threshold INTEGER DEFAULT 100,
                message_reward_points INTEGER DEFAULT 10
            )
        ''')

        await conn.commit()
    await bot.add_cog(Economy(bot))

