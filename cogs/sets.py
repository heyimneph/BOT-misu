import discord
import logging
import aiosqlite
import os
import io
import json

from discord.ext import commands
from discord import app_commands
from discord.ui import View, Select, Button

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


def truncate_field_value(value, max_length=1024):
    if len(value) > max_length:
        return value[:max_length - 3] + "..."
    return value

# ---------------------------------------------------------------------------------------------------------------------
# Select Menus with Pagination
# ---------------------------------------------------------------------------------------------------------------------
class PaginatedSelect(Select):
    def __init__(self, placeholder, options, bot, callback, page=0):
        self.bot = bot
        self.options_data = options
        self.page = page
        self.callback_func = callback
        paginated_options = self.options_data[page * 25:(page + 1) * 25]

        super().__init__(placeholder=placeholder, min_values=1, max_values=1,
                         options=[discord.SelectOption(label=opt['name'], description=opt.get('description', '')[:100],
                                                       value=str(opt['name']))
                                  for opt in paginated_options])

    async def callback(self, interaction: discord.Interaction):
        await self.callback_func(self, interaction)


class SetSelect(PaginatedSelect):
    def __init__(self, sets, bot, page=0):
        self.bot = bot
        super().__init__(placeholder="Select a set...", options=sets, bot=bot, callback=self.set_select_callback,
                         page=page)

    async def set_select_callback(self, select, interaction: discord.Interaction):
        set_id = select.values[0]
        all_cards = await select.bot.get_cards(interaction.guild.id)
        cards_in_set = await select.bot.get_cards_in_set(set_id, interaction.guild.id)
        cards_not_in_set = [card for card in all_cards if card['name'] not in {c['name'] for c in cards_in_set}]

        card_select_view = View()

        card_select = CardAddSelect(cards_not_in_set, select.bot, set_id)
        card_select_view.add_item(card_select)
        card_select_view.add_item(PreviousButton(card_select))
        card_select_view.add_item(FinishButton())
        card_select_view.add_item(NextButton(card_select))

        await interaction.response.edit_message(content="Select cards to add to the set:", view=card_select_view)


class CardAddSelect(PaginatedSelect):
    def __init__(self, cards, bot, set_id, page=0):
        self.bot = bot
        self.set_id = set_id
        super().__init__(placeholder="Choose cards to add...", options=cards, bot=bot, callback=self.card_add_callback,
                         page=page)

    async def card_add_callback(self, select, interaction: discord.Interaction):
        card_name = select.values[0]
        result = await select.bot.add_card_to_set(card_name, select.set_id, interaction.guild.id)

        message = f"Added card `{card_name}` to set successfully." if result else f"Card '{card_name}' is already in the set."

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class SetRemoveCardSelect(PaginatedSelect):
    def __init__(self, sets, bot, page=0):
        self.bot = bot
        super().__init__(placeholder="Select a set to remove cards from...", options=sets, bot=bot,
                         callback=self.set_remove_select_callback, page=page)

    async def set_remove_select_callback(self, select, interaction: discord.Interaction):
        set_id = select.values[0]
        cards_in_set = await select.bot.get_cards_in_set(set_id, interaction.guild.id)
        card_select_view = View()

        card_select = CardRemoveFromSetSelect(cards_in_set, select.bot, set_id)
        card_select_view.add_item(card_select)
        card_select_view.add_item(PreviousButton(card_select))
        card_select_view.add_item(FinishButton())
        card_select_view.add_item(NextButton(card_select))

        await interaction.response.edit_message(content="Select cards to remove from the set:", view=card_select_view)


class CardRemoveFromSetSelect(PaginatedSelect):
    def __init__(self, cards, bot, set_id, page=0):
        self.bot = bot
        self.set_id = set_id
        super().__init__(placeholder="Choose a card to remove from the set...", options=cards, bot=bot,
                         callback=self.card_remove_callback, page=page)

    async def card_remove_callback(self, select, interaction: discord.Interaction):
        card_name = select.values[0]
        await select.bot.remove_card_from_set(card_name, select.set_id, interaction.guild.id)
        await interaction.response.send_message(f"Card `{card_name}` removed from the set successfully.",
                                                ephemeral=True)


# ---------------------------------------------------------------------------------------------------------------------
# Pagination View
# ---------------------------------------------------------------------------------------------------------------------
class PaginationView(View):
    def __init__(self, embeds, bot):
        super().__init__(timeout=180)
        self.bot = bot
        self.embeds = embeds
        self.current_page = 0

        # Pagination buttons
        self.previous_button = Button(style=discord.ButtonStyle.secondary, label="Prev", disabled=True)
        self.home_button = Button(style=discord.ButtonStyle.primary, label="Home")
        self.next_button = Button(style=discord.ButtonStyle.secondary, label="Next", disabled=(len(embeds) <= 1))

        # Add buttons to the view
        self.add_item(self.previous_button)
        self.add_item(self.home_button)
        self.add_item(self.next_button)

        # Assign callbacks to buttons
        self.previous_button.callback = self.previous_page
        self.home_button.callback = self.go_home
        self.next_button.callback = self.next_page

    async def previous_page(self, interaction: discord.Interaction):
        if self.current_page > 0:
            self.current_page -= 1
            self.next_button.disabled = False
            if self.current_page == 0:
                self.previous_button.disabled = True
            await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)
        else:
            self.previous_button.disabled = True
            await interaction.response.edit_message(view=self)

    async def go_home(self, interaction: discord.Interaction):
        self.current_page = 0
        self.previous_button.disabled = True
        self.next_button.disabled = (len(self.embeds) <= 1)
        await interaction.response.edit_message(embed=self.embeds[0], view=self)

    async def next_page(self, interaction: discord.Interaction):
        if self.current_page < len(self.embeds) - 1:
            self.current_page += 1
            self.previous_button.disabled = False
            if self.current_page == len(self.embeds) - 1:
                self.next_button.disabled = True
            await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)
        else:
            self.next_button.disabled = True
            await interaction.response.edit_message(view=self)


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

        new_select = type(self.select_menu)(self.select_menu.options_data, self.select_menu.bot,
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

        new_select = type(self.select_menu)(self.select_menu.options_data, self.select_menu.bot,
                                            new_page)
        new_view = View()
        new_view.add_item(new_select)
        new_view.add_item(PreviousButton(new_select))
        new_view.add_item(FinishButton())
        new_view.add_item(NextButton(new_select))

        await interaction.response.edit_message(view=new_view)


# ---------------------------------------------------------------------------------------------------------------------
# Finish Button
# ---------------------------------------------------------------------------------------------------------------------
class FinishButton(Button):
    def __init__(self):
        super().__init__(style=discord.ButtonStyle.secondary, label="Finish", row=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="Set Complete.", view=None)


# ---------------------------------------------------------------------------------------------------------------------
# Set Cog Class
# ---------------------------------------------------------------------------------------------------------------------
class SetCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def get_sets(self, guild_id):
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute("SELECT set_id, name, description FROM card_sets WHERE guild_id = ?",
                                        (guild_id,))
            rows = await cursor.fetchall()
            sets = [{'id': row[0], 'name': row[1], 'description': row[2]} for row in rows]
            return sets

    async def get_cards(self, guild_id):
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute("SELECT name, description FROM cards WHERE guild_id = ?", (guild_id,))
            rows = await cursor.fetchall()
            return [{'name': row[0], 'description': row[1]} for row in rows]

    async def get_cards_in_set(self, set_id, guild_id):
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute('''
                SELECT cards.name, cards.img_url 
                FROM set_cards 
                JOIN cards ON set_cards.card_id = cards.card_id 
                WHERE set_cards.set_id = ? AND set_cards.guild_id = ? AND cards.guild_id = ?
            ''', (set_id, guild_id, guild_id))  # Ensure guild consistency
            rows = await cursor.fetchall()
            return [{'name': row[0], 'img_url': row[1]} for row in rows]


    async def get_user_cards_in_set(self, user_id, set_id, guild_id):
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute('''
                SELECT cards.name 
                FROM user_inventory 
                JOIN set_cards ON user_inventory.card_id = set_cards.card_id AND user_inventory.guild_id = set_cards.guild_id
                JOIN cards ON cards.card_id = user_inventory.card_id AND cards.guild_id = user_inventory.guild_id
                WHERE user_inventory.user_id = ? AND set_cards.set_id = ? AND user_inventory.guild_id = ?
            ''', (user_id, set_id, guild_id))

            rows = await cursor.fetchall()
            return [{'name': row[0]} for row in rows]

    async def add_card_to_set(self, card_name: str, set_name: str, guild_id: int) -> bool:
        async with aiosqlite.connect(db_path) as conn:
            # Resolve the set_id from the set name
            cursor = await conn.execute(
                "SELECT set_id FROM card_sets WHERE name = ? AND guild_id = ?",
                (set_name, guild_id)
            )
            set_row = await cursor.fetchone()

            if not set_row:
                logger.error(f"Set `{set_name}` not found in guild `{guild_id}`.")
                return False  # Set not found

            set_id = set_row[0]

            # Resolve the card_id from the card name
            cursor = await conn.execute(
                "SELECT card_id FROM cards WHERE name = ? AND guild_id = ?",
                (card_name, guild_id)
            )
            card_row = await cursor.fetchone()

            if not card_row:
                logger.error(f"Card `{card_name}` not found in guild `{guild_id}`.")
                return False  # Card not found

            card_id = card_row[0]

            # Check if the card is already in the set
            cursor = await conn.execute(
                "SELECT 1 FROM set_cards WHERE set_id = ? AND card_id = ? AND guild_id = ?",
                (set_id, card_id, guild_id)
            )
            if await cursor.fetchone():
                logger.warning(f"Card `{card_name}` is already in set `{set_name}`.")
                return False  # Already in set

            # Add the card to the set
            await conn.execute(
                "INSERT INTO set_cards (set_id, card_id, guild_id) VALUES (?, ?, ?)",
                (set_id, card_id, guild_id)
            )
            await conn.commit()
            logger.info(f"Card `{card_name}` successfully added to set `{set_name}`.")
            return True

    async def remove_card_from_set(self, card_name: str, set_id: int, guild_id: int) -> bool:
        async with aiosqlite.connect(db_path) as conn:
            # Resolve the card_id from the card name
            cursor = await conn.execute(
                "SELECT card_id FROM cards WHERE name = ? AND guild_id = ?",
                (card_name, guild_id)
            )
            card_row = await cursor.fetchone()

            if not card_row:
                logger.error(f"Card `{card_name}` not found in guild `{guild_id}`.")
                return False  # Card not found

            card_id = card_row[0]

            # Remove the card from the set
            cursor = await conn.execute(
                "DELETE FROM set_cards WHERE set_id = ? AND card_id = ? AND guild_id = ?",
                (set_id, card_id, guild_id)
            )
            await conn.commit()

            # Check if the card was removed
            if cursor.rowcount > 0:
                logger.info(f"Card `{card_name}` successfully removed from set ID `{set_id}`.")
                return True
            else:
                logger.warning(f"Card `{card_name}` was not found in set ID `{set_id}`.")
                return False

    async def delete_set(self, set_id, guild_id):
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute("DELETE FROM set_cards WHERE set_id = ? AND guild_id = ?", (set_id, guild_id))
            await conn.execute("DELETE FROM card_sets WHERE set_id = ? AND guild_id = ?", (set_id, guild_id))
            await conn.commit()



    async def load_preset(self, preset_file, guild_id):
        with open(preset_file, 'r') as f:
            preset = json.load(f)

        set_name = preset['set']['name']
        set_description = preset['set']['description']
        cards = preset['cards']

        async with aiosqlite.connect(db_path) as conn:
            # Insert the set into the database
            cursor = await conn.execute('''
                INSERT INTO card_sets (guild_id, name, description)
                VALUES (?, ?, ?)
            ''', (int(guild_id), set_name, set_description))
            set_id = cursor.lastrowid

            # Insert the cards into the set
            for card in cards:
                card_name = card['name']
                card_description = card['description']

                # Insert the card into the cards table
                await conn.execute('''
                    INSERT INTO cards (guild_id, name, description)
                    VALUES (?, ?, ?)
                    ON CONFLICT(name, guild_id) DO NOTHING
                ''', (int(guild_id), card_name, card_description))

                # Insert the card into the set
                await conn.execute('''
                    INSERT INTO set_cards (set_id, name, guild_id)
                    VALUES (?, ?, ?)
                ''', (int(set_id), card_name, int(guild_id)))

            await conn.commit()

    async def preset_name_autocomplete(self, interaction: discord.Interaction, current: str):
        """Autocomplete function to suggest preset JSON files in the ./data/presets directory."""
        preset_dir = './data/presets'
        presets = [f[:-5] for f in os.listdir(preset_dir) if f.endswith('.json')]  # Remove '.json' from filename
        return [
            app_commands.Choice(name=preset, value=preset)
            for preset in presets if current.lower() in preset.lower()
        ]

    async def set_name_autocomplete(self, interaction: discord.Interaction, current: str):
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute(
                "SELECT name FROM card_sets WHERE guild_id = ? AND name LIKE ? LIMIT 25",
                (interaction.guild.id, f"%{current}%"))
            sets = await cursor.fetchall()

        return [
            app_commands.Choice(name=set_name[0], value=set_name[0])
            for set_name in sets
        ]

    async def card_name_autocomplete(self, interaction: discord.Interaction, current: str):
        try:
            async with aiosqlite.connect(db_path) as conn:
                cursor = await conn.execute(
                    "SELECT name FROM cards WHERE guild_id = ? AND name LIKE ? LIMIT 25",
                    (interaction.guild.id, f"%{current}%"))
                cards = await cursor.fetchall()

            return [
                app_commands.Choice(name=card_name[0], value=card_name[0])
                for card_name in cards
            ]
        except Exception as e:
            logger.error(f"Error in card_name_autocomplete: {e}")
            return []

    async def card_name_in_set_autocomplete(self, interaction: discord.Interaction, current: str):
        # Extract the set name from the interaction options
        set_name = interaction.namespace.set_name

        async with aiosqlite.connect(db_path) as conn:
            # Get the set ID based on the set name
            cursor = await conn.execute("SELECT set_id FROM card_sets WHERE name = ? AND guild_id = ?",
                                        (set_name, interaction.guild.id))
            set_row = await cursor.fetchone()

            if not set_row:
                return [app_commands.Choice(name="No cards found", value="no_cards_available")]

            set_id = set_row[0]

            # Fetch cards that are in the selected set
            cursor = await conn.execute('''
                SELECT cards.name 
                FROM set_cards 
                JOIN cards ON set_cards.card_id = cards.card_id AND set_cards.guild_id = cards.guild_id
                WHERE set_cards.set_id = ? AND set_cards.guild_id = ? AND cards.name LIKE ?
                LIMIT 25
            ''', (set_id, interaction.guild.id, f"%{current}%"))

            cards_in_set = await cursor.fetchall()

        if not cards_in_set:
            return [app_commands.Choice(name="No cards found", value="no_cards_available")]

        return [
            app_commands.Choice(name=card_name[0], value=card_name[0])
            for card_name in cards_in_set
        ]

    # ----------------------------------------------------------------------------------------------------------------------
    # Set-Related Commands
    # ---------------------------------------------------------------------------------------------------------------------
    @app_commands.command(description="Admin: Create a new card set")
    @app_commands.describe(name="The name of the set", description="A brief description of the set (optional)")
    async def set_create(self, interaction: discord.Interaction, name: str, description: str = ""):
        if not await check_permissions(interaction):
            await interaction.response.send_message("You do not have permission to use this command. "
                                                    "An Admin needs to `/authorise` you!",
                                                    ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            async with aiosqlite.connect(db_path) as conn:
                # Check if a set with this name already exists
                cursor = await conn.execute(
                    "SELECT 1 FROM card_sets WHERE guild_id = ? AND name = ?",
                    (interaction.guild.id, name)
                )
                if await cursor.fetchone():
                    await interaction.followup.send(
                        f"A set with name `{name}` already exists in this guild. Please choose a different name.",
                        ephemeral=True
                    )
                    return

                # Create the new set
                await conn.execute('''
                    INSERT INTO card_sets (guild_id, name, description)
                    VALUES (?, ?, ?)
                ''', (interaction.guild.id, name, description))
                await conn.commit()
                await interaction.followup.send(f"Set `{name}` created successfully!", ephemeral=True)

        except Exception as e:
            logger.error(f"Failed to create card set: {e}")
            await interaction.followup.send(f"Failed to create the set due to an internal error: {e}", ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)
    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="Admin: Add a card to a set")
    @app_commands.autocomplete(set_name=set_name_autocomplete, card_name=card_name_autocomplete)
    @app_commands.describe(set_name="The name of the set", card_name="The name of the card")
    async def set_add_card(self, interaction: discord.Interaction, set_name: str = None, card_name: str = None):
        if not await check_permissions(interaction):
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return

        try:
            if set_name and card_name:
                async with aiosqlite.connect(db_path) as conn:
                    # Get the set ID from the set name
                    cursor = await conn.execute(
                        "SELECT set_id FROM card_sets WHERE name = ? AND guild_id = ?",
                        (set_name, interaction.guild.id)
                    )
                    set_row = await cursor.fetchone()

                    if not set_row:
                        await interaction.response.send_message(
                            f"Set `{set_name}` not found in this guild.", ephemeral=True
                        )
                        return

                    set_id = set_row[0]

                    # Resolve card_id
                    cursor = await conn.execute(
                        "SELECT card_id FROM cards WHERE name = ? AND guild_id = ?",
                        (card_name, interaction.guild.id)
                    )
                    card_row = await cursor.fetchone()

                    if not card_row:
                        await interaction.response.send_message(
                            f"Card `{card_name}` not found in this guild.", ephemeral=True
                        )
                        return

                    card_id = card_row[0]

                    # Add the card to the set
                    cursor = await conn.execute(
                        "SELECT 1 FROM set_cards WHERE set_id = ? AND card_id = ? AND guild_id = ?",
                        (set_id, card_id, interaction.guild.id)
                    )
                    if await cursor.fetchone():
                        await interaction.response.send_message(
                            f"Card `{card_name}` is already in the set `{set_name}`.", ephemeral=True
                        )
                        return

                    await conn.execute(
                        "INSERT INTO set_cards (set_id, card_id, guild_id) VALUES (?, ?, ?)",
                        (set_id, card_id, interaction.guild.id)
                    )
                    await conn.commit()

                    await interaction.response.send_message(
                        f"Card `{card_name}` has been successfully added to the set `{set_name}`.",
                        ephemeral=True
                    )

            else:
                await interaction.response.send_message(
                    "Please specify both the set name and the card name.", ephemeral=True
                )

        except Exception as e:
            logger.error(f"Failed to add card to set: {e}")
            await interaction.followup.send(f"Failed to process your request due to an error: {e}", ephemeral=True)
    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="Admin: Edit an existing card set's information")
    @app_commands.autocomplete(set_name=set_name_autocomplete)
    @app_commands.describe(
        set_name="The name of the set to edit",
        new_name="The new name for the set (optional)",
        new_description="The new description for the set (optional)"
    )
    async def set_edit(
            self,
            interaction: discord.Interaction,
            set_name: str,
            new_name: str = None,
            new_description: str = None
    ):
        """Edit an existing card set's name and/or description."""
        if not await check_permissions(interaction):
            await interaction.response.send_message(
                "You do not have permission to use this command. An Admin needs to `/authorise` you!",
                ephemeral=True
            )
            return

        if not new_name and not new_description:
            await interaction.response.send_message(
                "You must provide at least one of: new_name or new_description",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            async with aiosqlite.connect(db_path) as conn:
                # Check if the set exists
                cursor = await conn.execute(
                    "SELECT set_id FROM card_sets WHERE name = ? AND guild_id = ?",
                    (set_name, interaction.guild.id)
                )
                set_row = await cursor.fetchone()

                if not set_row:
                    await interaction.followup.send(
                        f"Set `{set_name}` not found in this guild.",
                        ephemeral=True
                    )
                    return

                set_id = set_row[0]

                # Check if new_name is provided and different from current name
                if new_name:
                    # Check if new name already exists
                    cursor = await conn.execute(
                        "SELECT 1 FROM card_sets WHERE name = ? AND guild_id = ? AND set_id != ?",
                        (new_name, interaction.guild.id, set_id)
                    )
                    if await cursor.fetchone():
                        await interaction.followup.send(
                            f"A set with name `{new_name}` already exists in this guild.",
                            ephemeral=True
                        )
                        return

                # Build the update query based on provided parameters
                updates = []
                params = []

                if new_name:
                    updates.append("name = ?")
                    params.append(new_name)

                if new_description is not None:  # Allow empty description
                    updates.append("description = ?")
                    params.append(new_description)

                if updates:
                    params.append(set_id)
                    params.append(interaction.guild.id)

                    update_query = f"""
                        UPDATE card_sets 
                        SET {', '.join(updates)} 
                        WHERE set_id = ? AND guild_id = ?
                    """

                    await conn.execute(update_query, params)
                    await conn.commit()

                # Prepare response message
                response_parts = []
                if new_name:
                    response_parts.append(f"name to `{new_name}`")
                if new_description is not None:
                    response_parts.append(f"description to `{new_description}`")

                await interaction.followup.send(
                    f"Successfully updated set `{set_name}`: {', '.join(response_parts)}",
                    ephemeral=True
                )

        except Exception as e:
            logger.error(f"Failed to edit set: {e}")
            await interaction.followup.send(
                f"Failed to edit the set due to an internal error: {e}",
                ephemeral=True
            )
        finally:
            await log_command_usage(self.bot, interaction)
    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="Admin: Remove a card from a set")
    @app_commands.autocomplete(set_name=set_name_autocomplete, card_name=card_name_in_set_autocomplete)
    @app_commands.describe(set_name="The name of the set", card_name="The name of the card")
    async def set_remove_card(self, interaction: discord.Interaction, set_name: str = None, card_name: str = None):
        if not await check_permissions(interaction):
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return

        try:
            if set_name and card_name:
                async with aiosqlite.connect(db_path) as conn:
                    # Get the set ID from the set name
                    cursor = await conn.execute(
                        "SELECT set_id FROM card_sets WHERE name = ? AND guild_id = ?",
                        (set_name, interaction.guild.id)
                    )
                    set_row = await cursor.fetchone()

                    if not set_row:
                        await interaction.response.send_message(f"Set `{set_name}` not found.", ephemeral=True)
                        return

                    set_id = set_row[0]

                    # Call the helper to remove the card from the set
                    success = await self.remove_card_from_set(card_name, set_id, interaction.guild.id)

                    if success:
                        await interaction.response.send_message(
                            f"Card `{card_name}` has been successfully removed from the set `{set_name}`.",
                            ephemeral=True
                        )
                    else:
                        await interaction.response.send_message(
                            f"Failed to remove card `{card_name}` from the set `{set_name}`. "
                            f"Ensure the card exists in the set.",
                            ephemeral=True
                        )
            else:
                await interaction.response.send_message("Please specify both the set name and the card name.",
                                                        ephemeral=True)

        except Exception as e:
            logger.error(f"Failed to remove card from set: {e}")
            await interaction.followup.send(f"Failed to process your request due to an error: {e}", ephemeral=True)

    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="Admin: Delete a card set")
    @app_commands.autocomplete(set_name=set_name_autocomplete)
    @app_commands.describe(set_name="The name of the set")
    async def set_delete(self, interaction: discord.Interaction, set_name: str = None):
        if not await check_permissions(interaction):
            await interaction.response.send_message(
                "You do not have permission to use this command. An Admin needs to `/authorise` you!",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            async with aiosqlite.connect(db_path) as conn:
                # Get all sets with this name (should only be one after unique constraint is added)
                cursor = await conn.execute(
                    "SELECT set_id FROM card_sets WHERE name = ? AND guild_id = ?",
                    (set_name, interaction.guild.id),
                )
                sets = await cursor.fetchall()

                if not sets:
                    await interaction.followup.send(f"Set `{set_name}` not found in this guild.", ephemeral=True)
                    return

                # For each set with this name (handles legacy duplicates)
                for set_info in sets:
                    set_id = set_info[0]
                    # Delete the set and associated cards
                    await conn.execute("DELETE FROM set_cards WHERE set_id = ? AND guild_id = ?",
                                       (set_id, interaction.guild.id))
                    await conn.execute("DELETE FROM card_sets WHERE set_id = ? AND guild_id = ?",
                                       (set_id, interaction.guild.id))

                await conn.commit()

                await interaction.followup.send(
                    f"Deleted {len(sets)} set(s) with name `{set_name}`.",
                    ephemeral=True
                )

        except Exception as e:
            logger.error(f"Failed to delete set: {e}")
            await interaction.followup.send(
                f"Failed to delete the set due to an internal error: {e}",
                ephemeral=True
            )
        finally:
            await log_command_usage(self.bot, interaction)
    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="User: Get information about a specific set")
    @app_commands.describe(set_name="The name of the set to get information about")
    @app_commands.autocomplete(set_name=set_name_autocomplete)
    async def info_set(self, interaction: discord.Interaction, set_name: str):
        await interaction.response.defer(ephemeral=True)
        try:
            colour = await get_embed_colour(interaction.guild.id)

            if set_name == "no_sets_available":
                await interaction.followup.send("No sets available in this guild.", ephemeral=True)
                return

            async with aiosqlite.connect(db_path) as conn:
                # Get basic set information
                cursor = await conn.execute(
                    '''
                    SELECT cs.name, cs.description, COUNT(sc.card_id)
                    FROM card_sets AS cs
                    LEFT JOIN set_cards AS sc ON cs.set_id = sc.set_id
                    WHERE cs.guild_id = ? AND cs.name = ?
                    GROUP BY cs.set_id
                    ''',
                    (interaction.guild.id, set_name)
                )
                set_info = await cursor.fetchone()

                if not set_info:
                    await interaction.followup.send(
                        "Set not found or is empty. Please check the name and try again.",
                        ephemeral=True
                    )
                    return

                set_name, set_description, total_count = set_info

                # Get cards in the set
                cursor = await conn.execute(
                    '''
                    SELECT c.name, c.description, c.rarity, c.img_url
                    FROM set_cards AS sc
                    JOIN cards AS c ON sc.card_id = c.card_id AND sc.guild_id = c.guild_id
                    WHERE sc.set_id = (SELECT set_id FROM card_sets WHERE guild_id = ? AND name = ?)
                    ''',
                    (interaction.guild.id, set_name)
                )
                cards = await cursor.fetchall()

                # Get user's collected cards in the set
                cursor = await conn.execute(
                    '''
                    SELECT COUNT(*)
                    FROM set_cards AS sc
                    JOIN user_inventory AS ui ON sc.card_id = ui.card_id AND sc.guild_id = ui.guild_id
                    WHERE sc.set_id = (SELECT set_id FROM card_sets WHERE guild_id = ? AND name = ?)
                    AND ui.user_id = ?
                    ''',
                    (interaction.guild.id, set_name, interaction.user.id)
                )
                collected_count = await cursor.fetchone()
                collected_count = collected_count[0] if collected_count else 0

                # Get the first card image URL if available
                first_card_image = cards[0][3] if cards and cards[0][3] else None

                # Prepare embeds for pagination
                embeds = []

                # First page: set information overview
                overview_embed = discord.Embed(
                    title=f"{set_name} Information",
                    description=truncate_field_value(f"*{set_description}*\n\n"
                                                     f"You have collected `{collected_count}/{total_count}` cards from this set."),
                    color=colour
                )
                if first_card_image:
                    overview_embed.set_image(url=first_card_image)
                overview_embed.set_footer(text=f"Set Information for '{set_name}'")
                overview_embed.timestamp = discord.utils.utcnow()
                embeds.append(overview_embed)

                # Subsequent pages: list of cards
                items_per_page = 5
                page_count = (len(cards) + items_per_page - 1) // items_per_page

                for page in range(page_count):
                    start_index = page * items_per_page
                    end_index = start_index + items_per_page
                    page_cards = cards[start_index:end_index]

                    card_embed = discord.Embed(
                        title=f"Set Cards for '{set_name}'",
                        color=colour
                    )
                    if first_card_image:
                        card_embed.set_thumbnail(url=first_card_image)

                    for idx, card in enumerate(page_cards, start=1 + start_index):
                        card_embed.add_field(
                            name=f"{idx}. {card[0]}",
                            value=truncate_field_value(f"Rarity: *{card[2].capitalize() if card[2] else 'Unknown'}*\n"
                                                       f"Description: *{card[1] if card[1] else 'No description available'}*"),
                            inline=False
                        )

                    card_embed.set_footer(text=f"Page {page + 1}/{page_count}")
                    card_embed.timestamp = discord.utils.utcnow()
                    embeds.append(card_embed)

                # Display the paginated embeds
                if embeds:
                    view = PaginationView(embeds, self.bot)
                    await interaction.followup.send(embed=embeds[0], view=view, ephemeral=True)
                else:
                    await interaction.followup.send(
                        f"The set `{set_name}` has no cards associated with it.",
                        ephemeral=True
                    )

        except Exception as e:
            logger.error(f"Failed to fetch set information: {e}")
            await interaction.followup.send(
                f"Failed to process your request due to an internal error: {e}",
                ephemeral=True
            )
        finally:
            await log_command_usage(self.bot, interaction)

    # ---------------------------------------------------------------------------------------------------------------------

    from collections import defaultdict

    @app_commands.command(description="Admin: Import a preloaded Set or a JSON file")
    @app_commands.describe(set="The Set to be loaded (name of preset file without extension)",
                           file="Upload a JSON file to load the set")
    @app_commands.autocomplete(set=preset_name_autocomplete)
    async def set_load(self, interaction: discord.Interaction, set: str = None, file: discord.Attachment = None):
        if not await check_permissions(interaction):
            await interaction.response.send_message("You do not have permission to use this command. "
                                                    "An Admin needs to `/authorise` you!", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            # Determine the source of the set data
            if file:
                if not file.filename.endswith('.json'):
                    await interaction.followup.send("Please upload a valid JSON file.", ephemeral=True)
                    return

                file_content = await file.read()
                try:
                    preset_data = json.loads(file_content)
                    is_preset = 0
                except json.JSONDecodeError as e:
                    await interaction.followup.send(f"Failed to decode JSON file: {e}", ephemeral=True)
                    return

            elif set:
                preset_dir = './data/presets'
                file_path = os.path.join(preset_dir, f"{set}.json")

                if not os.path.exists(file_path):
                    await interaction.followup.send(f"Preset file `{set}.json` not found.", ephemeral=True)
                    return

                with open(file_path, 'r') as f:
                    preset_data = json.load(f)
                is_preset = 1
            else:
                await interaction.followup.send("You must provide either a set name or upload a JSON file.",
                                                ephemeral=True)
                return

            async with aiosqlite.connect(db_path) as conn:
                # Check if the set already exists
                cursor = await conn.execute(
                    "SELECT 1 FROM card_sets WHERE name = ? AND guild_id = ?",
                    (preset_data['set']['name'], interaction.guild.id)
                )
                existing_set = await cursor.fetchone()
                if existing_set:
                    await interaction.followup.send(
                        f"The set `{preset_data['set']['name']}` is already loaded in this guild.", ephemeral=True)
                    return

                # Insert set
                cursor = await conn.execute('''
                    INSERT INTO card_sets (guild_id, name, description, is_preset)
                    VALUES (?, ?, ?, ?)
                ''', (
                    interaction.guild.id, preset_data['set']['name'], preset_data['set']['description'], is_preset))
                set_id = cursor.lastrowid

                # Get current max card ID
                cursor = await conn.execute("SELECT MAX(card_id) FROM cards WHERE guild_id = ?",
                                            (interaction.guild.id,))
                max_card_id_row = await cursor.fetchone()
                max_card_id = int(max_card_id_row[0]) if max_card_id_row and max_card_id_row[0] else 0

                cursor = await conn.execute(
                    "SELECT LOWER(rarity) FROM rarity_weights WHERE guild_id = ?", (interaction.guild.id,))
                existing_rarities = {row[0] for row in await cursor.fetchall()}

                # Insert cards and rarities
                for card in preset_data['cards']:
                    rarity = card['rarity'].strip()
                    rarity_normalized = rarity.lower()

                    # Insert missing rarity into rarity_weights with default values
                    if rarity_normalized not in existing_rarities:
                        await conn.execute('''
                            INSERT INTO rarity_weights (guild_id, rarity, weight, burn_value)
                            VALUES (?, ?, ?, ?)
                        ''', (interaction.guild.id, rarity, 1.0, 10))
                        existing_rarities.add(rarity_normalized)

                    max_card_id += 1
                    new_card_id = f"{max_card_id:08}"

                    await conn.execute('''
                        INSERT INTO cards (guild_id, card_id, name, description, rarity, img_url, local_img_url)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        interaction.guild.id, new_card_id, card['name'], card['description'], card['rarity'],
                        card['img_url'], card['local_img_url']))

                    await conn.execute('''
                        INSERT INTO set_cards (set_id, card_id, guild_id)
                        VALUES (?, ?, ?)
                    ''', (set_id, new_card_id, interaction.guild.id))

                await conn.commit()

            source_info = "uploaded JSON file" if file else f"`{set}.json`"
            await interaction.followup.send(
                f"Set `{preset_data['set']['name']}` imported successfully from {source_info}!", ephemeral=True)

        except Exception as e:
            logger.error(f"Failed to import set: {e}")
            await interaction.followup.send(f"Failed to import the set due to an internal error: {e}", ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)

    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="Admin: Unload a loaded set")
    @app_commands.describe(set_name="The name of the set to be unloaded. You may only unload Presets")
    @app_commands.autocomplete(set_name=set_name_autocomplete)
    async def set_unload(self, interaction: discord.Interaction, set_name: str):
        if not await check_permissions(interaction):
            await interaction.response.send_message("You do not have permission to use this command. "
                                                    "An Admin needs to `/authorise` you!",
                                                    ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            async with aiosqlite.connect(db_path) as conn:
                # Check if the set exists and is a preset
                cursor = await conn.execute(
                    "SELECT set_id FROM card_sets WHERE name = ? AND guild_id = ? AND is_preset = 1",
                    (set_name, interaction.guild.id)
                )
                set_info = await cursor.fetchone()

                if not set_info:
                    await interaction.followup.send(
                        f"The set `{set_name}` does not exist or is not a preset in this guild.", ephemeral=True)
                    return

                set_id = set_info[0]

                # Remove the association between the cards and the set
                await conn.execute(
                    "DELETE FROM set_cards WHERE set_id = ? AND guild_id = ?",
                    (set_id, interaction.guild.id)
                )

                # Remove the set itself
                await conn.execute(
                    "DELETE FROM card_sets WHERE set_id = ? AND guild_id = ?",
                    (set_id, interaction.guild.id)
                )

                # No need to delete the cards from the 'cards' table, just disassociate from the set

                await conn.commit()

            await interaction.followup.send(f"Set `{set_name}` unloaded successfully!", ephemeral=True)

        except Exception as e:
            logger.error(f"Failed to unload set: {e}")
            await interaction.followup.send(f"Failed to unload the set due to an internal error: {e}", ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)

    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="Admin: Export a set to a JSON file")
    @app_commands.describe(set_name="The name of the set to export")
    @app_commands.autocomplete(set_name=set_name_autocomplete)
    async def set_export(self, interaction: discord.Interaction, set_name: str):
        if not await check_permissions(interaction):
            await interaction.response.send_message("You do not have permission to use this command. "
                                                    "An Admin needs to `/authorise` you!",
                                                    ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            async with aiosqlite.connect(db_path) as conn:
                # Get the set details, including the is_preset flag
                cursor = await conn.execute(
                    "SELECT set_id, name, description, is_preset FROM card_sets WHERE name = ? AND guild_id = ?",
                    (set_name, interaction.guild.id)
                )
                set_row = await cursor.fetchone()

                if not set_row:
                    await interaction.followup.send(f"Set `{set_name}` not found in this guild.", ephemeral=True)
                    return

                set_id, set_name, set_description, is_preset = set_row

                # Check if the set is a preset
                if is_preset:
                    await interaction.followup.send(f"Preset sets cannot be exported.", ephemeral=True)
                    return

                # Get the cards associated with the set
                cursor = await conn.execute(
                    "SELECT cards.name, cards.description, cards.rarity, cards.img_url, cards.local_img_url "
                    "FROM set_cards "
                    "JOIN cards ON set_cards.card_id = cards.card_id AND set_cards.guild_id = cards.guild_id "
                    "WHERE set_cards.set_id = ? AND set_cards.guild_id = ?",
                    (set_id, interaction.guild.id)
                )
                cards = await cursor.fetchall()

                # Structure the data in the same format as the presets
                preset_data = {
                    "set": {
                        "name": set_name,
                        "description": set_description
                    },
                    "cards": [
                        {
                            "name": card[0],
                            "description": card[1],
                            "rarity": card[2],
                            "img_url": card[3],
                            "local_img_url": card[4]
                        } for card in cards
                    ]
                }

                # Convert the preset data to a JSON string
                json_data = json.dumps(preset_data, indent=4)

                # Use io.StringIO to create a file-like object from the JSON string
                json_file = io.StringIO(json_data)
                file = discord.File(fp=json_file, filename=f"{set_name}.json")

                # Send the file in the response
                await interaction.followup.send(content=f"Set `{set_name}` exported successfully.", file=file,
                                                ephemeral=True)

        except Exception as e:
            logger.error(f"Failed to export set: {e}")
            await interaction.followup.send(f"Failed to export the set due to an internal error: {e}", ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)


# ---------------------------------------------------------------------------------------------------------------------
# Setup Function with Migration
# ---------------------------------------------------------------------------------------------------------------------
async def setup(bot):
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS card_sets (
                set_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                name TEXT NOT NULL,
                description TEXT,
                is_preset INTEGER DEFAULT 0
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS set_cards (
                set_id INTEGER,
                card_id TEXT,
                guild_id INTEGER,
                FOREIGN KEY (set_id) REFERENCES card_sets(set_id),
                FOREIGN KEY (card_id, guild_id) REFERENCES cards(card_id, guild_id),
                PRIMARY KEY (set_id, card_id, guild_id)
            )
        ''')
        await conn.commit()
    await bot.add_cog(SetCog(bot))





