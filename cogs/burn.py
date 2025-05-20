import discord
import logging
import aiosqlite
import os

from discord.ext import commands
from discord import app_commands
from discord.ui import View, Select, Button

from core.utils import log_command_usage, check_permissions
from core.autocomplete import rarity_autocomplete

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
# Select Views with Pagination
# ---------------------------------------------------------------------------------------------------------------------
class PaginatedSelect(Select):
    def __init__(self, placeholder, options, bot, callback, page=0):
        self.bot = bot
        self.options_data = options
        self.page = page
        self.callback_func = callback
        paginated_options = self.options_data[page * 25:(page + 1) * 25]

        # Create options with unique values using index
        select_options = []
        seen_values = set()
        for i, opt in enumerate(paginated_options):
            unique_value = f"{opt['card_id']}_{i}"  # Add index to ensure uniqueness
            if unique_value in seen_values:
                continue  # Safety check, should never happen now
            seen_values.add(unique_value)

            select_options.append(discord.SelectOption(
                label=opt['name'],
                description=f"Quantity: {opt['quantity']}",
                value=unique_value
            ))

        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=select_options
        )

    async def callback(self, interaction: discord.Interaction):
        await self.callback_func(self, interaction)


class BurnCardSelect(PaginatedSelect):
    def __init__(self, cards, bot, page=0):
        self.bot = bot
        super().__init__(
            placeholder="Select a card to burn...",
            options=cards,
            bot=bot,
            callback=self.burn_select_callback,
            page=page
        )

    async def burn_select_callback(self, select, interaction: discord.Interaction):
        try:
            # Extract the card_id from the value (split by underscore)
            card_id = select.values[0].split("_")[0]
            burn_cog = self.bot.get_cog("BurnCog")
            if burn_cog:
                await burn_cog.burn_card(interaction, card_id)
            else:
                await interaction.response.send_message(
                    "Burn functionality not available.",
                    ephemeral=True
                )
        except Exception as e:
            error_msg = f"Error during card burn: {str(e)}"
            if len(error_msg) > 1900:
                error_msg = error_msg[:1900] + "..."
            if interaction.response.is_done():
                await interaction.followup.send(error_msg, ephemeral=True)
            else:
                await interaction.response.send_message(error_msg, ephemeral=True)

# ---------------------------------------------------------------------------------------------------------------------
# Buttons and Views
# ---------------------------------------------------------------------------------------------------------------------
class NextButton(Button):
    def __init__(self, select_menu):
        super().__init__(style=discord.ButtonStyle.primary, label="Next", row=1)
        self.select_menu = select_menu

    async def callback(self, interaction: discord.Interaction):
        new_page = self.select_menu.page + 1
        if new_page * 25 >= len(self.select_menu.options_data):
            new_page = 0  # Loop back to the first page

        new_select = type(self.select_menu)(self.select_menu.options_data, self.select_menu.bot, new_page)
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

        new_select = type(self.select_menu)(self.select_menu.options_data, self.select_menu.bot, new_page)
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
        await interaction.response.edit_message(content="Burning Complete", view=None)

# ---------------------------------------------------------------------------------------------------------------------
# Burn Cog Class
# ---------------------------------------------------------------------------------------------------------------------

class BurnCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def card_name_autocomplete(self, interaction: discord.Interaction, current: str):
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute(
                "SELECT c.name FROM cards c "
                "JOIN user_inventory ui ON c.card_id = ui.card_id "
                "WHERE ui.user_id = ? AND ui.guild_id = ? AND c.guild_id = ? AND c.name LIKE ?",
                (interaction.user.id, interaction.guild.id, interaction.guild.id, f'%{current}%')
            )
            card_names = await cursor.fetchall()
            await cursor.close()

            # Slice to 25 results max
            return [
                app_commands.Choice(name=card[0], value=card[0])
                for card in card_names[:25]
            ]

    async def burn_card(self, interaction, card_id):
        async with aiosqlite.connect(db_path) as conn:
            # Get quantity and rarity of the card
            cursor = await conn.execute(
                "SELECT quantity, rarity FROM user_inventory "
                "JOIN cards ON user_inventory.card_id = cards.card_id "
                "WHERE user_inventory.user_id = ? AND user_inventory.card_id = ? AND user_inventory.guild_id = ?",
                (interaction.user.id, card_id, interaction.guild.id)
            )
            card = await cursor.fetchone()
            await cursor.close()

            if card and card[0] > 0:
                raw_rarity = card[1]
                normalized_rarity = raw_rarity.strip().lower()

                logger.debug(
                    f"User {interaction.user.id} burning card {card_id} with rarity '{raw_rarity}' (normalized: '{normalized_rarity}') in guild {interaction.guild.id}")

                # Fetch the burn value using normalized rarity
                cursor = await conn.execute(
                    "SELECT burn_value FROM rarity_weights WHERE guild_id = ? AND LOWER(rarity) = ?",
                    (interaction.guild.id, normalized_rarity)
                )
                burn_value = await cursor.fetchone()
                await cursor.close()

                if not burn_value:
                    await interaction.response.send_message(
                        f"Error: Burn value not configured for rarity `{raw_rarity}`.",
                        ephemeral=True
                    )
                    return

                points_to_add = burn_value[0]
                new_quantity = card[0] - 1

                if new_quantity > 0:
                    await conn.execute(
                        "UPDATE user_inventory SET quantity = ? WHERE user_id = ? AND card_id = ? AND guild_id = ?",
                        (new_quantity, interaction.user.id, card_id, interaction.guild.id)
                    )
                else:
                    await conn.execute(
                        "DELETE FROM user_inventory WHERE user_id = ? AND card_id = ? AND guild_id = ?",
                        (interaction.user.id, card_id, interaction.guild.id)
                    )

                # Update the user's balance
                cursor = await conn.execute(
                    "SELECT balance FROM economy WHERE user_id = ? AND guild_id = ?",
                    (interaction.user.id, interaction.guild.id)
                )
                user_balance = await cursor.fetchone()
                await cursor.close()

                if user_balance is None:
                    await conn.execute(
                        "INSERT INTO economy (user_id, guild_id, balance) VALUES (?, ?, ?)",
                        (interaction.user.id, interaction.guild.id, points_to_add)
                    )
                else:
                    await conn.execute(
                        "UPDATE economy SET balance = balance + ? WHERE user_id = ? AND guild_id = ?",
                        (points_to_add, interaction.user.id, interaction.guild.id)
                    )

                await conn.commit()

                message = f"Card burned successfully. You've earned `{points_to_add}` points!"
                if not interaction.response.is_done():
                    await interaction.response.send_message(message, ephemeral=True)
                else:
                    await interaction.followup.send(message, ephemeral=True)
            else:
                error_msg = "You do not own this card or have insufficient quantity."
                if not interaction.response.is_done():
                    await interaction.response.send_message(error_msg, ephemeral=True)
                else:
                    await interaction.followup.send(error_msg, ephemeral=True)

    # ---------------------------------------------------------------------------------------------------------------------
    # Burn Commands
    # ---------------------------------------------------------------------------------------------------------------------
    @app_commands.command(name="burn", description="Burn a card from your inventory for points")
    @app_commands.autocomplete(card_name=card_name_autocomplete)
    async def burn(self, interaction: discord.Interaction, card_name: str = None):
        try:
            if card_name:
                async with aiosqlite.connect(db_path) as conn:
                    cursor = await conn.execute(
                        "SELECT card_id FROM cards WHERE name = ? AND guild_id = ?",
                        (card_name, interaction.guild.id))
                    card = await cursor.fetchone()
                    await cursor.close()

                    if card:
                        await self.burn_card(interaction, card[0])
                    else:
                        await interaction.response.send_message("Card not found.", ephemeral=True)
            else:
                async with aiosqlite.connect(db_path) as conn:
                    cursor = await conn.execute(
                        "SELECT cards.card_id, cards.name, user_inventory.quantity, cards.rarity "
                        "FROM user_inventory JOIN cards ON user_inventory.card_id = cards.card_id "
                        "WHERE user_inventory.user_id = ? AND user_inventory.guild_id = ?",
                        (interaction.user.id, interaction.guild.id))
                    cards = [{'card_id': row[0], 'name': row[1], 'quantity': row[2], 'rarity': row[3]} for row in await cursor.fetchall()]
                    await cursor.close()

                if cards:
                    view = View()
                    select_menu = BurnCardSelect(cards, self.bot)
                    view.add_item(select_menu)
                    view.add_item(PreviousButton(select_menu))
                    view.add_item(FinishButton())
                    view.add_item(NextButton(select_menu))
                    await interaction.response.send_message("Select a card to burn:", view=view, ephemeral=True)
                else:
                    await interaction.response.send_message("You have no cards to burn.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error Burning Card - {e}")
            await interaction.response.send_message(f"Error with Burn Command: {e}", ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)

    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(name="burn_set_values",
                          description="Admin: Set the point values for burning cards of different rarities")
    @app_commands.describe(
        rarity="The rarity type",
        burn_value="Points for burning a card of this rarity"
    )
    @app_commands.autocomplete(rarity=rarity_autocomplete)
    async def burn_set_values(self, interaction: discord.Interaction, rarity: str, burn_value: int):
        if not await check_permissions(interaction):
            await interaction.response.send_message(
                "You do not have permission to use this command. An Admin needs to `/authorise` you!",
                ephemeral=True
            )
            return
        try:
            async with aiosqlite.connect(db_path) as conn:
                await conn.execute(
                    "UPDATE rarity_weights SET burn_value = ? WHERE guild_id = ? AND rarity = ?",
                    (burn_value, interaction.guild.id, rarity)
                )
                await conn.commit()
                await interaction.response.send_message(
                    f"Burn value for `{rarity}` set to `{burn_value}`.", ephemeral=True
                )
        except Exception as e:
            logger.error(f"Error setting burn values - {e}")
            await interaction.response.send_message(f"Error updating burn values: {e}", ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)


# ---------------------------------------------------------------------------------------------------------------------
# Setup Function
# ---------------------------------------------------------------------------------------------------------------------
async def setup(bot):
    await bot.add_cog(BurnCog(bot))
