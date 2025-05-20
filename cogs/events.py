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
# Select Menus with Pagination
# ---------------------------------------------------------------------------------------------------------------------
class PaginatedSelect(Select):
    def __init__(self, placeholder, options, user_id, bot, callback, page=0):
        self.bot = bot
        self.user_id = user_id
        self.options_data = options
        self.page = page
        self.callback_func = callback
        paginated_options = self.options_data[page * 25:(page + 1) * 25]

        super().__init__(placeholder=placeholder, min_values=1, max_values=1,
                         options=[discord.SelectOption(label=opt['event_name'], description="Claim your reward.")
                                  for opt in paginated_options])

    async def callback(self, interaction: discord.Interaction):
        await self.callback_func(self, interaction)

class ClaimSelect(PaginatedSelect):
    def __init__(self, events, user_id, bot, page=0):
        self.bot = bot
        super().__init__(placeholder="Choose an event to claim...", options=events, user_id=user_id, bot=bot,
                         callback=self.claim_select_callback, page=page)

    async def claim_select_callback(self, select, interaction: discord.Interaction):
        try:
            event_name = select.values[0]
            async with aiosqlite.connect(db_path) as conn:
                cursor = await conn.execute(
                    """
                    SELECT point_reward, event_cooldown, set_reward, 
                           (SELECT last_claim FROM user_events WHERE user_id = ? AND event_name = ?)
                    FROM events 
                    WHERE event_name = ? AND guild_id = ?
                    """,
                    (self.user_id, event_name, event_name, interaction.guild.id)
                )
                event_data = await cursor.fetchone()

                if event_data:
                    point_reward, cooldown, set_reward, last_claim = event_data
                    reward_details = []

                    # Check cooldown
                    if last_claim:
                        last_claim_time = datetime.fromisoformat(last_claim)
                        time_diff = (datetime.utcnow() - last_claim_time).total_seconds()
                        remaining_time = cooldown * 3600 - time_diff

                        if remaining_time > 0:
                            await interaction.response.send_message(
                                f"You must wait {self.format_cooldown(remaining_time)} to claim this event again.",
                                ephemeral=True
                            )
                            return

                    # Handle point rewards
                    if point_reward > 0:
                        await conn.execute(
                            """
                            INSERT INTO economy (guild_id, user_id, balance)
                            VALUES (?, ?, ?)
                            ON CONFLICT(guild_id, user_id) DO UPDATE SET balance = economy.balance + excluded.balance
                            """,
                            (interaction.guild.id, self.user_id, point_reward)
                        )
                        reward_details.append(f"{point_reward} points")

                    # Handle set rewards (cards)
                    card_img_url = None
                    card_name = None
                    card_description = None
                    card_rarity = None
                    if set_reward:
                        set_ids = set_reward.split(',')
                        chosen_set_id = random.choice(set_ids)  # Randomly select one set
                        events_cog = self.bot.get_cog('EventCog')
                        if events_cog:
                            # Call the handle_set_reward method
                            card_id, card_img_url, card_name, card_description, card_rarity = await events_cog.handle_set_reward(
                                interaction.guild.id, chosen_set_id
                            )

                            # Check if a valid card ID was returned
                            if card_id:
                                reward_details.append(f"Card: {card_name} (ID: {card_id})")
                                # Update inventory
                                await conn.execute(
                                    """
                                    INSERT INTO user_inventory (guild_id, user_id, card_id, quantity)
                                    VALUES (?, ?, ?, 1)
                                    ON CONFLICT(guild_id, user_id, card_id) DO UPDATE SET quantity = quantity + 1
                                    """,
                                    (interaction.guild.id, self.user_id, card_id)
                                )
                            else:
                                # Log and add fallback message if no valid card is found
                                logger.warning(f"No valid card found for set ID {chosen_set_id}")
                                reward_details.append("No card reward available.")

                    # Update the last claim time
                    await conn.execute(
                        """
                        INSERT INTO user_events (guild_id, user_id, event_name, last_claim)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(user_id, event_name, guild_id) DO UPDATE SET last_claim = excluded.last_claim
                        """,
                        (interaction.guild.id, self.user_id, event_name, datetime.utcnow().isoformat())
                    )

                    await conn.commit()

                    # Prepare and send the embed
                    embed = discord.Embed(
                        title="Rewards Claimed",
                        description="You've successfully claimed the following rewards:",
                        color=discord.Color.green()
                    )
                    embed.add_field(name="Rewards", value="\n".join(reward_details))

                    # Add card information if available
                    if card_img_url:
                        embed.set_image(url=card_img_url)

                        # Truncate card description if it exceeds the limit
                        if card_description and len(card_description) > 800:
                            card_description = card_description[:797] + "..."  # Truncate and add ellipsis

                        # Add card information as a field
                        embed.add_field(
                            name="Card Information",
                            value=f"**Name:** {card_name}\n**Rarity:** {str(card_rarity).capitalize()}\n**Description:** {card_description}",
                            inline=False
                        )

                    embed.set_footer(text=f"Reward Claimed for '{event_name}' by {interaction.user.display_name}")
                    embed.timestamp = discord.utils.utcnow()

                    await interaction.response.send_message(embed=embed)
                    logger.info(
                        f"User {interaction.user.id} claimed rewards for event '{event_name}': {reward_details}")
                else:
                    await interaction.response.send_message("Event not found.", ephemeral=True)

        except Exception as e:
            logger.error(f"Error handling claim command: {e}")
            await interaction.response.send_message("An error occurred while processing your request.", ephemeral=True)

        finally:
            await log_command_usage(self.bot, interaction)

    def format_cooldown(self, seconds):
        parts = []
        years, seconds = divmod(seconds, 31536000)
        months, seconds = divmod(seconds, 2592000)
        days, seconds = divmod(seconds, 86400)
        hours, seconds = divmod(seconds, 3600)
        minutes = seconds // 60

        if years > 0:
            parts.append(f"{int(years)} year{'s' if years != 1 else ''}")
        if months > 0:
            parts.append(f"{int(months)} month{'s' if months != 1 else ''}")
        if days > 0:
            parts.append(f"{int(days)} day{'s' if days != 1 else ''}")
        if hours > 0:
            parts.append(f"{int(hours)} hour{'s' if hours != 1 else ''}")
        if years == 0 and months == 0 and days == 0 and minutes > 0:
            parts.append(f"{int(minutes)} minute{'s' if minutes != 1 else ''}")

        if len(parts) > 1:
            return ", ".join(parts[:-1]) + " and " + parts[-1]
        else:
            return parts[0] if parts else "0 minutes"

# ---------------------------------------------------------------------------------------------------------------------
# Pagination Buttons
# ---------------------------------------------------------------------------------------------------------------------
class NextButton(Button):
    def __init__(self, select_menu):
        super().__init__(style=discord.ButtonStyle.primary, label="Next", row=1)
        self.select_menu = select_menu

    async def callback(self, interaction: discord.Interaction):
        new_page = self.select_menu.page + 1
        if new_page * 25 >= len(self.select_menu.options_data):
            new_page = 0  # Loop back to the first page

        new_select = type(self.select_menu)(self.select_menu.options_data, self.select_menu.user_id, self.select_menu.bot,
                                            new_page)
        new_view = View()
        new_view.add_item(new_select)
        new_view.add_item(PreviousButton(new_select))
        new_view.add_item(FinishButton())
        new_view.add_item(NextButton(new_select))

        await interaction.response.edit_message(view=new_view)

class PreviousButton(Button):
    def __init__(self, select_menu):
        super().__init__(style=discord.ButtonStyle.primary, label="Previous", row=1)
        self.select_menu = select_menu

    async def callback(self, interaction: discord.Interaction):
        new_page = self.select_menu.page - 1
        if new_page < 0:
            new_page = (len(self.select_menu.options_data) - 1) // 25  # Go to the last page

        new_select = type(self.select_menu)(self.select_menu.options_data, self.select_menu.user_id, self.select_menu.bot,
                                            new_page)
        new_view = View()
        new_view.add_item(new_select)
        new_view.add_item(PreviousButton(new_select))
        new_view.add_item(FinishButton())
        new_view.add_item(NextButton(new_select))

        await interaction.response.edit_message(view=new_view)

class FinishButton(Button):
    def __init__(self):
        super().__init__(style=discord.ButtonStyle.secondary, label="Finish", row=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="Action Complete.", view=None)


# ---------------------------------------------------------------------------------------------------------------------
# Event Cog Class
# ---------------------------------------------------------------------------------------------------------------------
class EventCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

# ---------------------------------------------------------------------------------------------------------------------
# ---------------------------------------------------------------------------------------------------------------------

    async def handle_set_reward(self, guild_id, set_id):
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute("""
                SELECT cards.card_id, cards.rarity, cards.img_url, cards.name, cards.description 
                FROM set_cards
                JOIN cards ON set_cards.card_id = cards.card_id
                WHERE set_cards.set_id = ? AND set_cards.guild_id = ? AND cards.guild_id = ?
            """, (set_id, guild_id, guild_id))
            cards = await cursor.fetchall()

        if not cards:
            logger.warning(f"No cards found for set ID {set_id} in guild ID {guild_id}")
            return None, None, None, None, None

        # Handle card selection logic
        if len(cards) == 1:
            chosen_card = cards[0]
        else:
            rarity_weights = {"Common": 1, "Uncommon": 0.5, "Rare": 0.2, "Legendary": 0.01}
            card_pool = [(card[0], rarity_weights.get(card[1], 1), card[2], card[3], card[4]) for card in cards]
            probabilities = [weight for _, weight, _, _, _ in card_pool]
            total_prob = sum(probabilities)
            normalized_probs = [prob / total_prob for prob in probabilities]
            chosen_index = random.choices(range(len(card_pool)), weights=normalized_probs, k=1)[0]
            chosen_card = cards[chosen_index]

        chosen_card_id, chosen_rarity, chosen_img_url, chosen_name, chosen_description = chosen_card
        return chosen_card_id, chosen_img_url, chosen_name, chosen_description, chosen_rarity

    async def event_names_autocomplete(self, interaction: discord.Interaction, current: str):
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute(
                "SELECT event_name FROM events WHERE guild_id = ? AND event_name LIKE ?",
                (interaction.guild.id, f'%{current}%')
            )
            event_names = await cursor.fetchall()
            return [app_commands.Choice(name=event[0], value=event[0]) for event in event_names]

    async def set_name_autocomplete(self, interaction: discord.Interaction, current: str):
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute(
                "SELECT name FROM card_sets WHERE guild_id = ? AND name LIKE ? LIMIT 25",
                (interaction.guild.id, f'%{current}%')
            )
            set_names = await cursor.fetchall()
            return [app_commands.Choice(name=set_name[0], value=set_name[0]) for set_name in set_names]

    async def unit_autocomplete(self, interaction: discord.Interaction, current: str):
        units = ["hours", "days", "months"]
        return [
            app_commands.Choice(name=unit, value=unit)
            for unit in units if current.lower() in unit.lower()]


    # ---------------------------------------------------------------------------------------------------------------------
    # Event Commands
    # ---------------------------------------------------------------------------------------------------------------------
    @app_commands.command(name="claim", description="User: Claim event rewards")
    async def claim(self, interaction: discord.Interaction):
        try:
            async with aiosqlite.connect(db_path) as conn:
                cursor = await conn.execute("SELECT event_name FROM events WHERE guild_id = ?", (interaction.guild.id,))
                events = [{'event_name': row[0]} for row in await cursor.fetchall()]

            if events:
                view = View()
                select_menu = ClaimSelect(events, interaction.user.id, self.bot)
                view.add_item(select_menu)
                view.add_item(PreviousButton(select_menu))
                view.add_item(FinishButton())
                view.add_item(NextButton(select_menu))

                await interaction.response.send_message("Select the type of claim:", view=view, ephemeral=True)
            else:
                await interaction.response.send_message("No events available to claim.", ephemeral=True)

        except Exception as e:
            logger.error(f"Error handling claim command: {e}")
            await interaction.response.send_message("An error occurred while processing your request.",
                                                ephemeral=True)

        finally:
            await log_command_usage(self.bot, interaction)


    # ---------------------------------------------------------------------------------------------------------------------
    # Admin Commands to Manage Events
    # ---------------------------------------------------------------------------------------------------------------------
    @app_commands.command(description="Admin: Add an event with optional card sets as rewards")
    @app_commands.describe(event_name="Name of the event", points="Points to be rewarded", cooldown="Cooldown duration",
                           unit="Unit of cooldown (hours, days, months)",
                           set_names="Comma-separated names of card sets to use as rewards (if you want multiple)")
    @app_commands.autocomplete(set_names=set_name_autocomplete, unit=unit_autocomplete)
    async def event_create(self, interaction: discord.Interaction, event_name: str, points: int, cooldown: int,
                           unit: str,
                           set_names: str = None):
        if not await check_permissions(interaction):
            await interaction.response.send_message("You do not have permission to use this command. "
                                                    "An Admin needs to `/authorise` you!",
                                                    ephemeral=True)
            return

        if points < 0 or cooldown <= 0:
            await interaction.response.send_message("Points and cooldown must be positive numbers.", ephemeral=True)
            return

        unit = unit.lower()
        if unit == "days":
            cooldown_hours = cooldown * 24
        elif unit == "months":
            cooldown_hours = cooldown * 24 * 30
        elif unit == "hours":
            cooldown_hours = cooldown
        else:
            await interaction.response.send_message(
                "Invalid unit for cooldown. Please use 'hours', 'days', or 'months'.", ephemeral=True)
            return

        async with aiosqlite.connect(db_path) as db:
            set_ids = []
            if set_names:
                set_names_list = [name.strip() for name in set_names.split(',')]
                async with aiosqlite.connect(db_path) as db:
                    for set_name in set_names_list:
                        cursor = await db.execute("SELECT set_id FROM card_sets WHERE name = ? AND guild_id = ?",
                                                  (set_name, interaction.guild.id))
                        set_row = await cursor.fetchone()
                        if set_row:
                            set_ids.append(set_row[0])
                        else:
                            await interaction.response.send_message(f"No card set found with the name '{set_name}'.",
                                                                    ephemeral=True)
                            return

            set_ids_str = ",".join(map(str, set_ids))
            # Save the event
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "INSERT INTO events (guild_id, event_name, point_reward, set_reward, event_cooldown) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(guild_id, event_name) DO UPDATE SET point_reward = excluded.point_reward, "
                    "event_cooldown = excluded.event_cooldown, set_reward = excluded.set_reward",
                    (interaction.guild.id, event_name, points, set_ids_str, cooldown_hours)
                )
                await db.commit()

            reward_info = f" and card sets `{set_names}`" if set_names else ""
            await interaction.response.send_message(
                f"Event `{event_name}` added with `{points}` points and a cooldown of `{cooldown} {unit}`{reward_info}.",
                ephemeral=True
            )
        await log_command_usage(self.bot, interaction)

    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="Admin: Remove an event")
    @app_commands.autocomplete(event_name=event_names_autocomplete)
    @app_commands.describe(event_name="Name of the event to remove")
    async def event_delete(self, interaction: discord.Interaction, event_name: str):
        if not await check_permissions(interaction):
            await interaction.response.send_message("You do not have permission to use this command. "
                                                    "An Admin needs to `/authorise` you!",
                                                    ephemeral=True)
            return

        try:
            async with aiosqlite.connect(db_path) as db:
                # Delete the event from events table
                await db.execute(
                    "DELETE FROM events WHERE guild_id = ? AND event_name = ?",
                    (interaction.guild.id, event_name)
                )
                # Delete user-related data for this event
                await db.execute(
                    "DELETE FROM user_events WHERE guild_id = ? AND event_name = ?",
                    (interaction.guild.id, event_name)
                )
                await db.commit()

            await interaction.response.send_message(
                f"Event `{event_name}` has been removed.", ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error with event_remove command: {e}")
            await interaction.response.send_message(f"An unexpected error occured: {e}", ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)

# ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="Admin: Edit an existing event")
    @app_commands.describe(
        event_name="Name of the event to edit",
        new_event_name="New name for the event (optional)",
        points="New points to be rewarded (optional)",
        cooldown="New cooldown duration (optional)",
        unit="Unit of cooldown (hours, days, months) (optional)",
        set_names="Comma-separated names of card sets to use as rewards (optional)"
    )
    @app_commands.autocomplete(event_name=event_names_autocomplete, set_names=set_name_autocomplete,
                               unit=unit_autocomplete)
    async def event_edit(
            self,
            interaction: discord.Interaction,
            event_name: str,
            new_event_name: str = None,
            points: int = None,
            cooldown: int = None,
            unit: str = None,
            set_names: str = None
    ):
        try:
            if not await check_permissions(interaction):
                await interaction.response.send_message(
                    "You do not have permission to use this command. An Admin needs to `/authorise` you!",
                    ephemeral=True
                )
                return

            # Validate inputs
            if points is not None and points < 0:
                await interaction.response.send_message("Points must be a positive number.", ephemeral=True)
                return
            if cooldown is not None and cooldown <= 0:
                await interaction.response.send_message("Cooldown must be a positive number.", ephemeral=True)
                return
            if unit and unit.lower() not in ["hours", "days", "months"]:
                await interaction.response.send_message(
                    "Invalid unit for cooldown. Please use 'hours', 'days', or 'months'.", ephemeral=True
                )
                return

            # Convert cooldown to hours if provided
            cooldown_hours = None
            if cooldown is not None and unit:
                unit = unit.lower()
                if unit == "days":
                    cooldown_hours = cooldown * 24
                elif unit == "months":
                    cooldown_hours = cooldown * 24 * 30
                elif unit == "hours":
                    cooldown_hours = cooldown

            # Process set names if provided
            set_ids_str = None
            if set_names:
                set_names_list = [name.strip() for name in set_names.split(',')]
                set_ids = []
                async with aiosqlite.connect(db_path) as db:
                    for set_name in set_names_list:
                        cursor = await db.execute(
                            "SELECT set_id FROM card_sets WHERE name = ? AND guild_id = ?",
                            (set_name, interaction.guild.id)
                        )
                        set_row = await cursor.fetchone()
                        if set_row:
                            set_ids.append(set_row[0])
                        else:
                            await interaction.response.send_message(
                                f"No card set found with the name '{set_name}'.", ephemeral=True
                            )
                            return
                set_ids_str = ",".join(map(str, set_ids))

            # Update the event in the database
            async with aiosqlite.connect(db_path) as db:
                # Fetch the existing event data
                cursor = await db.execute(
                    "SELECT point_reward, event_cooldown, set_reward FROM events WHERE guild_id = ? AND event_name = ?",
                    (interaction.guild.id, event_name)
                )
                event_data = await cursor.fetchone()

                if not event_data:
                    await interaction.response.send_message(f"Event `{event_name}` not found.", ephemeral=True)
                    return

                # Prepare the updated values
                updated_event_name = new_event_name if new_event_name else event_name
                updated_points = points if points is not None else event_data[0]
                updated_cooldown = cooldown_hours if cooldown_hours is not None else event_data[1]
                updated_set_reward = set_ids_str if set_ids_str is not None else event_data[2]

                # Update the event
                await db.execute(
                    """
                    UPDATE events
                    SET event_name = ?, point_reward = ?, event_cooldown = ?, set_reward = ?
                    WHERE guild_id = ? AND event_name = ?
                    """,
                    (updated_event_name, updated_points, updated_cooldown, updated_set_reward, interaction.guild.id,
                     event_name)
                )

                # Reset the cooldown for all users by deleting their `last_claim` entries
                await db.execute(
                    """
                    DELETE FROM user_events
                    WHERE guild_id = ? AND event_name = ?
                    """,
                    (interaction.guild.id, event_name)
                )

                await db.commit()

            # Prepare the response message
            response_message = f"Event `{event_name}` has been updated with the following changes:\n"
            if new_event_name:
                response_message += f"- New name: `{new_event_name}`\n"
            if points is not None:
                response_message += f"- Points: `{points}`\n"
            if cooldown is not None:
                response_message += f"- Cooldown: `{cooldown} {unit}`\n"
            if set_names:
                response_message += f"- Card sets: `{set_names}`\n"
            response_message += "- Cooldown has been reset for all users."

            await interaction.response.send_message(response_message, ephemeral=True)

        except Exception as e:
            logger.error(f"Error in event_edit command: {e}")
            await interaction.response.send_message(
                "An error occurred while processing your request.", ephemeral=True
            )

        finally:
            await log_command_usage(self.bot, interaction)


# ---------------------------------------------------------------------------------------------------------------------
# Setup Function
# ---------------------------------------------------------------------------------------------------------------------
async def setup(bot):
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS events (
                guild_id INTEGER,
                event_name TEXT,
                point_reward INTEGER,
                set_reward TEXT,
                event_cooldown INTEGER,
                PRIMARY KEY (guild_id, event_name)
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_events (
                guild_id INTEGER,
                user_id INTEGER,
                event_name TEXT,
                last_claim TIMESTAMP,
                PRIMARY KEY (user_id, event_name, guild_id)
            )
        ''')

        await conn.commit()
    await bot.add_cog(EventCog(bot))
