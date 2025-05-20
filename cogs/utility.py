import discord
import logging
import aiosqlite
import os
import psutil
import inspect

from discord.ext import commands
from discord import app_commands
from discord.ui import View, Button
from datetime import datetime

from core.utils import log_command_usage, check_permissions, get_embed_colour

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
# Help Modals
# ---------------------------------------------------------------------------------------------------------------------
class SuggestionModal(discord.ui.Modal):
    def __init__(self, bot):
        super().__init__(title="Submit a Suggestion")
        self.bot = bot

    ticket_name = discord.ui.TextInput(label="Ticket Name", style=discord.TextStyle.short, required=True)
    suggestion = discord.ui.TextInput(label="Describe your suggestion", style=discord.TextStyle.long, required=True)
    additional_info = discord.ui.TextInput(label="Additional information", style=discord.TextStyle.long, required=False)

    async def on_submit(self, interaction: discord.Interaction):
        user = interaction.user
        current_time = discord.utils.utcnow()
        formatted_time = current_time.strftime("%d/%m/%Y")

        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute("SELECT 1 FROM blacklist WHERE user_id = ?", (interaction.user.id,))
            if await cursor.fetchone():
                support_url = "https://discord.gg/SXmXmteyZ3"  # Your support server link
                response_message = ("You are blacklisted from making suggestions. "
                                    f"If you believe this is a mistake, please contact us: [Support Server]({support_url}).")
                await interaction.response.send_message(response_message, ephemeral=True)
                return
            colour = await get_embed_colour(interaction.guild.id)

        channel = self.bot.get_channel(1268168019297697914)
        if channel:
            embed = discord.Embed(title=f"Suggestion: {self.ticket_name.value}",
                                  description=f"```{self.suggestion.value}```",
                                  color=colour)
            embed.add_field(name="Additional Information",
                            value=f"```{self.additional_info.value or 'None provided'}```",
                            inline=False)
            embed.set_footer(text=f"Submitted by {user.name} on {formatted_time}")

            view = View()
            view.add_item(BlacklistButton(interaction.user.id))

            await channel.send(embed=embed, view=view)
            await interaction.response.send_message("Your suggestion has been submitted successfully!", ephemeral=True)
        else:
            await interaction.response.send_message("Failed to send suggestion. Support channel not found.", ephemeral=True)

# ---------------------------------------------------------------------------------------------------------------------
# Buttons and Views
# ---------------------------------------------------------------------------------------------------------------------
class BlacklistButton(discord.ui.Button):
    def __init__(self, user_id):
        super().__init__(style=discord.ButtonStyle.danger, label="Blacklist User")
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute("INSERT OR IGNORE INTO blacklist (user_id) VALUES (?)", (self.user_id,))
            await conn.commit()
        await interaction.response.send_message("User has been blacklisted from making suggestions.", ephemeral=True)

# ---------------------------------------------------------------------------------------------------------------------
# Pagination for Help Command
# ---------------------------------------------------------------------------------------------------------------------
class HelpPaginator(View):
    def __init__(self, bot, pages, updates_page):
        super().__init__(timeout=180)
        self.bot = bot
        self.pages = pages
        self.current_page = 0
        self.updates_page = updates_page

        self.prev_button = Button(label="Prev", style=discord.ButtonStyle.primary)
        self.prev_button.callback = self.prev_page
        self.add_item(self.prev_button)

        self.home_button = Button(label="Home", style=discord.ButtonStyle.green)
        self.home_button.callback = self.go_home
        self.add_item(self.home_button)

        self.next_button = Button(label="Next", style=discord.ButtonStyle.primary)
        self.next_button.callback = self.next_page
        self.add_item(self.next_button)

        self.updates_button = Button(label="Updates", style=discord.ButtonStyle.secondary)
        self.updates_button.callback = self.go_to_updates
        self.add_item(self.updates_button)

    async def next_page(self, interaction: discord.Interaction):
        self.current_page += 1
        if self.current_page >= len(self.pages):
            self.current_page = 0
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)

    async def prev_page(self, interaction: discord.Interaction):
        self.current_page -= 1
        if self.current_page < 0:
            self.current_page = len(self.pages) - 1
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)

    async def go_home(self, interaction: discord.Interaction):
        self.current_page = 0
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)

    async def go_to_updates(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=self.updates_page, view=self)

    async def start(self, interaction: discord.Interaction):
        for page in self.pages:
            page.set_thumbnail(url=self.bot.user.display_avatar.url)
            page.set_footer(text="Created by heyimneph")
            page.timestamp = discord.utils.utcnow()
        await interaction.response.send_message(embed=self.pages[self.current_page], view=self, ephemeral=True)

# ---------------------------------------------------------------------------------------------------------------------
# Utility Cog Class
# ---------------------------------------------------------------------------------------------------------------------
class UtilityCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot_start_time = datetime.utcnow()

    async def has_required_permissions(self, interaction, command):
        if interaction.user.guild_permissions.administrator:
            return True

        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute('''
                SELECT can_use_commands FROM permissions WHERE guild_id = ? AND user_id = ?
            ''', (interaction.guild.id, interaction.user.id))
            permission = await cursor.fetchone()
            if permission and permission[0]:
                return True

        if "Admin" in command.description or "Owner" in command.description:
            return False

        for check in command.checks:
            try:
                if inspect.iscoroutinefunction(check):
                    result = await check(interaction)
                else:
                    result = check(interaction)
                if not result:
                    return False
            except Exception as e:
                logger.error(f"Permission check failed: {e}")
                return False

        return True



    async def owner_check(self, interaction: discord.Interaction):
        owner_id = 111941993629806592
        return interaction.user.id == owner_id


    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(name="help", description="User: Display help information for all commands")
    async def help(self, interaction: discord.Interaction):
        try:
            pages = []
            colour = await get_embed_colour(interaction.guild.id)

            # Main help intro page
            help_intro = discord.Embed(
                title="About Misu",
                description="*Hello, I am Misu! I am a Discord APP capable of creating and "
                            "managing your custom card needs! I have many commands for "
                            "configuring and customising your game. \n\n"
                            "You may navigate the pages of Commands simply by pressing the "
                            "buttons below.* \n\n",
                color=colour
            )
            help_intro.add_field(name="What do you need to do?",
                                 value="1. Run `/card_create` \n"
                                       "*create your custom card(s)* \n\n"
                                       "2. Run `/set_create` \n"
                                       "*creates a collection for cards* \n\n"
                                       "3. Run `/set_add_card` \n"
                                       "*add card(s) to a collection(s)\n\n"
                                       "4. Run `/event_create` \n"
                                       "*allow users to claim cards and points* \n\n"
                                       "If you would like command logging for *Misu*, create a channel called "
                                       "`misu_logs` to enable this.")
            help_intro.add_field(name="Need Support?",
                                 value="*Sometimes, things don't work as expected. If you need assistance or "
                                       "would like to report an issue you can join our "
                                       "[support server](https://discord.gg/SXmXmteyZ3) and create a ticket. We'd be "
                                       "happy to help!*",
                                 inline=False)
            pages.append(help_intro)

            # Generating command pages
            for cog_name, cog in self.bot.cogs.items():
                if cog_name == "Core" or cog_name == "TheMachineBotCore" or cog_name == "AdminCog":
                    continue
                embed = discord.Embed(title=f"{cog_name.replace('Cog', '')} Commands", description="", color=colour)

                for cmd in cog.get_app_commands():
                    if "Owner" in cmd.description and not await self.owner_check(interaction):
                        continue
                    if not await self.has_required_permissions(interaction, cmd):
                        continue
                    embed.add_field(name=f"/{cmd.name}", value=f"```{cmd.description}```", inline=False)

                if len(embed.fields) > 0:
                    pages.append(embed)

            # Updates page
            updates_page = discord.Embed(
                title="Latest Updates",
                description= "13/05/2025 \n"
                             "- Added `/set_edit` command \n\n"
                             "20/04/2025 \n"
                             "- Major Database Rework. Please report any bugs or unexpected interactions \n\n"
                             "10/03/2025 \n"
                             "- First iteration of the Dashboard. Found at: \n"
                             "https://misu.nephbox.net \n\n"
                             "26/02/2025 \n"
                             "- Fixed issue where new rarities did not have burn values \n"
                             "- Updated `rarity_create` and `rarity_edit` \n\n"
                             "20/02/2025 \n"
                             "- Fixed Bug where embeds wouldn't display because it exceeds 1024 character limit \n"
                             "- Implemented character limit on card descriptions to prevent further issues \n\n"
                             "18/02/2025 \n"
                             "- Added `inspect_inventory` for easier Admin Management of user inventories \n"
                             "- Added various 'rarity' management features \n\n"
                             "17/02/2025\n"
                             "- Added a `card_edit` command to update cards\n"
                             "- Updated `/inventory`. You can now hide descriptions as well as show your inventory publicly \n"
                             "- Added an `event_edit` command to modify existing events.\n"
                             "- Bug fixes and performance improvements \n\n"
                             "Please leave a review/rating here: https://top.gg/bot/1268589797149118670",
                color=colour
            )
            updates_page.set_footer(text="Created by heyimneph")
            updates_page.timestamp = discord.utils.utcnow()


            # Create paginator and pass the updates page
            paginator = HelpPaginator(self.bot, pages=pages, updates_page=updates_page)
            await paginator.start(interaction)

        except Exception as e:
            logger.error(f"Error with Help command: {e}")
            await interaction.response.send_message("Failed to fetch help information.", ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)


    # ---------------------------------------------------------------------------------------------------------------------
    # Utility Commands
    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(name="invite", description="User: Get an invite link for Misu")
    async def invite(self, interaction: discord.Interaction):
        try:
            async with aiosqlite.connect(db_path) as conn:
                permissions = discord.Permissions(
                    read_messages=True,
                    send_messages=True,
                    manage_messages=True,
                    embed_links=True,
                    read_message_history=True,
                )
                invite_url = discord.utils.oauth_url(client_id=self.bot.user.id, permissions=permissions)
                colour = await get_embed_colour(interaction.guild.id)

                embed = discord.Embed(
                    title="Invite Me",
                    description=f"[Click here to invite me to your server!]({invite_url})",
                    color=colour
                )
                embed.set_thumbnail(url=self.bot.user.display_avatar.url)
                embed.set_footer(text=f"Invite Generated by {interaction.user.name}")
                embed.timestamp = discord.utils.utcnow()

                await interaction.response.send_message(embed=embed)
        except Exception as e:
            logger.error(f"Error with Invite - {e}")
            await interaction.response.send_message(f"Error with Invite - {e}", ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)

    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(name="stats", description="User: Display statistics for Misu")
    async def stats(self, interaction: discord.Interaction):
        colour = await get_embed_colour(interaction.guild.id)

        try:
            async with aiosqlite.connect(db_path) as conn:
                cursor = await conn.execute("SELECT COUNT(*) FROM cards")
                total_unique_cards = (await cursor.fetchone())[0]

                cursor = await conn.execute("SELECT SUM(quantity) FROM user_inventory")
                total_cards_inventory = (await cursor.fetchone())[0] or 0

                cursor = await conn.execute("SELECT COUNT(*) FROM card_sets")
                total_card_sets = (await cursor.fetchone())[0]

            total_servers = len(self.bot.guilds)
            total_users = sum(len(guild.members) for guild in self.bot.guilds)

            bot_ping = round(self.bot.latency * 1000)
            bot_uptime = datetime.utcnow() - self.bot_start_time
            days, remainder = divmod(bot_uptime.total_seconds(), 86400)
            hours, remainder = divmod(remainder, 3600)
            minutes, _ = divmod(remainder, 60)

            uptime_display = []
            if days > 0:
                uptime_display.append(f"{int(days)} day(s)")
            if hours > 0:
                uptime_display.append(f"{int(hours)} hour(s)")
            if minutes > 0:
                uptime_display.append(f"{int(minutes)} minute(s)")

            if len(uptime_display) > 1:
                uptime_display = ', '.join(uptime_display[:-1]) + ' and ' + uptime_display[-1]
            elif uptime_display:
                uptime_display = uptime_display[0]
            else:
                uptime_display = "0 minute(s)"

            cpu = psutil.cpu_percent()
            memory = psutil.virtual_memory().percent

            embed = discord.Embed(title="", description="",
                                  color=colour)

            embed.add_field(name="🎴 Player Cards", value=f"┕ `{total_cards_inventory}`", inline=True)
            embed.add_field(name="🎴 Unique Cards", value=f"┕ `{total_unique_cards}`", inline=True)
            embed.add_field(name="📦 Collections", value=f"┕ `{total_card_sets}`", inline=True)
            embed.add_field(name="🏡 Servers", value=f"┕ `{total_servers}`", inline=True)
            embed.add_field(name="🏓 Ping", value=f"┕ `{bot_ping} ms`", inline=True)
            embed.add_field(name="🌐 Language", value=f"┕ `Python`", inline=True)
            embed.add_field(name="‍💻 CPU", value=f"┕ `{cpu}%`", inline=True)
            embed.add_field(name="💾 Memory", value=f"┕ `{memory}%`", inline=True)
            embed.add_field(name="⏳ Uptime", value=f"┕ `{uptime_display}`", inline=True)

            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error fetching stats: {e}")
            await interaction.response.send_message("Failed to fetch statistics.")
        finally:
            await log_command_usage(self.bot, interaction)

    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(name="suggest", description="User: Make a suggestion for Misu")
    async def suggest(self, interaction: discord.Interaction):
        try:
            modal = SuggestionModal(self.bot)
            await interaction.response.send_modal(modal)
            await log_command_usage(self.bot, interaction)
        except Exception as e:
            logger.error(f"Failed to launch suggestion modal: {e}")
            await interaction.response.send_message("Failed to launch the suggestion modal.", ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)

    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(name="unblacklist", description="Owner: Remove a user from the blacklist")
    @app_commands.describe(user_id="The ID of the user to unblacklist as a string")
    async def unblacklist(self, interaction: discord.Interaction, user_id: str):
        if not await self.owner_check(interaction):
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return

        try:
            user_id = int(user_id)
        except ValueError:
            await interaction.response.send_message("Please enter a valid user ID.", ephemeral=True)
            return

        try:
            async with aiosqlite.connect(db_path) as conn:
                cursor = await conn.execute("SELECT 1 FROM blacklist WHERE user_id = ?", (user_id,))
                exists = await cursor.fetchone()
                if not exists:
                    await interaction.response.send_message("This user is not blacklisted.", ephemeral=True)
                    return

                await conn.execute("DELETE FROM blacklist WHERE user_id = ?", (user_id,))
                await conn.commit()

            await interaction.response.send_message(f"User {user_id} has been removed from the blacklist.",
                                                    ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to remove user from blacklist: {e}")
            await interaction.response.send_message(f"Failed to remove user from blacklist due to an error: {e}",
                                                    ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)

    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="Admin: Authorize a user to use Misu Admin commands")
    @app_commands.describe(user="The user to authorize")
    @app_commands.checks.has_permissions(administrator=True)
    async def authorise(self, interaction: discord.Interaction, user: discord.User):
        try:
            async with aiosqlite.connect(db_path) as conn:
                await conn.execute('''
                    INSERT INTO permissions (guild_id, user_id, can_use_commands) VALUES (?, ?, 1)
                    ON CONFLICT(guild_id, user_id) DO UPDATE SET can_use_commands = 1
                ''', (interaction.guild.id, user.id))
                await conn.commit()
            await interaction.response.send_message(f"{user.display_name} has been authorized.", ephemeral=True)

        except Exception as e:
            logger.error(f"Failed to authorise user: {e}")
            await interaction.response.send_message(f"Failed to authorise user: {e}",
                                                    ephemeral=True)


        finally:
            await log_command_usage(self.bot, interaction)

    @app_commands.command(description="Admin: Revoke a user's authorization to use Misu's Admin commands")
    @app_commands.describe(user="The user to unauthorize")
    @app_commands.checks.has_permissions(administrator=True)
    async def unauthorise(self, interaction: discord.Interaction, user: discord.User):
        try:
            async with aiosqlite.connect(db_path) as conn:
                await conn.execute('''
                    UPDATE permissions SET can_use_commands = 0 WHERE guild_id = ? AND user_id = ?
                ''', (interaction.guild.id, user.id))
                await conn.commit()
            await interaction.response.send_message(f"{user.display_name} has been unauthorized.", ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to unauthorise user: {e}")
            await interaction.response.send_message(f"Failed to unauthorise user: {e}",
                                                    ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)


# ---------------------------------------------------------------------------------------------------------------------
# Setup Function
# ---------------------------------------------------------------------------------------------------------------------
async def setup(bot):
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS blacklist (
                user_id INTEGER PRIMARY KEY
            )
        ''')
        await conn.execute('''
                CREATE TABLE IF NOT EXISTS permissions (
                    guild_id INTEGER,
                    user_id INTEGER,
                    can_use_commands BOOLEAN DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                )
            ''')

        await conn.commit()
    await bot.add_cog(UtilityCog(bot))



