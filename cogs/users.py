import discord
import logging
import aiosqlite
import os

from discord.ext import commands
from discord import app_commands
from discord.ui import View, Button, Select

from core.utils import log_command_usage, check_permissions, get_embed_colour
from core.pagination import InventoryPaginationView

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
# Autocomplete Function
# ---------------------------------------------------------------------------------------------------------------------
async def inventory_autocomplete(interaction: discord.Interaction, current: str):
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute('''
            SELECT c.name
            FROM user_inventory ui
            JOIN cards c ON ui.card_id = c.card_id AND ui.guild_id = c.guild_id
            WHERE ui.user_id = ? AND ui.guild_id = ? AND ui.quantity > 0 AND c.name LIKE ?
            LIMIT 25
        ''', (interaction.user.id, interaction.guild.id, f'%{current}%'))
        items = await cursor.fetchall()

    return [app_commands.Choice(name=item[0], value=item[0]) for item in items]

# ---------------------------------------------------------------------------------------------------------------------
# Select Views
# ---------------------------------------------------------------------------------------------------------------------
class GiftSelectView(View):
    def __init__(self, items, bot, giver, receiver):
        super().__init__(timeout=180)
        self.bot = bot
        self.giver = giver
        self.receiver = receiver
        self.items = items

        options = [discord.SelectOption(label=f"{item[1]} (Quantity: {item[2]})", description=f"Rarity: {item[3]}",
                                        value=item[0])
                   for item in items]
        self.select = Select(options=options, placeholder="Choose an item to gift...", min_values=1, max_values=1)
        self.select.callback = self.confirm_gift
        self.add_item(self.select)

    async def confirm_gift(self, interaction: discord.Interaction):
        selected_card_id = self.select.values[0]
        for item in self.items:
            if item[0] == selected_card_id:
                selected_card = item
                break

        confirm_button = Button(label="Confirm Gift", style=discord.ButtonStyle.success)
        cancel_button = Button(label="Cancel", style=discord.ButtonStyle.danger)
        confirm_button.callback = lambda inter: self.perform_gift(inter, selected_card_id)
        cancel_button.callback = lambda inter: inter.response.edit_message(content="Gift cancelled.", view=None)

        self.clear_items()
        self.add_item(confirm_button)
        self.add_item(cancel_button)

        await interaction.response.edit_message(
            content=f"Confirm gifting `{selected_card[1]}` to '{self.receiver.display_name}'?", view=self)

    async def perform_gift(self, interaction, card_id):
        async with aiosqlite.connect(db_path) as conn:
            transaction_started = False
            try:
                await conn.execute('BEGIN')
                transaction_started = True

                # Reduce the quantity of the item in the giver's inventory
                await conn.execute('''
                    UPDATE user_inventory SET quantity = quantity - 1
                    WHERE user_id = ? AND card_id = ? AND guild_id = ?
                ''', (self.giver.id, card_id, interaction.guild.id))

                # Check if the quantity is now zero and remove the entry if it is
                await conn.execute('''
                    DELETE FROM user_inventory
                    WHERE user_id = ? AND card_id = ? AND guild_id = ? AND quantity = 0
                ''', (self.giver.id, card_id, interaction.guild.id))

                # Check if the receiver already has the item in their inventory
                result = await conn.execute('''
                    SELECT quantity FROM user_inventory
                    WHERE user_id = ? AND card_id = ? AND guild_id = ?
                ''', (self.receiver.id, card_id, interaction.guild.id))
                row = await result.fetchone()

                if row:
                    # If the receiver already has the item, increase their quantity
                    await conn.execute('''
                        UPDATE user_inventory SET quantity = quantity + 1
                        WHERE user_id = ? AND card_id = ? AND guild_id = ?
                    ''', (self.receiver.id, card_id, interaction.guild.id))
                else:
                    # If the receiver doesn't have the item, insert a new entry
                    await conn.execute('''
                        INSERT INTO user_inventory (guild_id, user_id, card_id, quantity)
                        VALUES (?, ?, ?, 1)
                    ''', (interaction.guild.id, self.receiver.id, card_id))

                await conn.commit()
                transaction_started = False

                if not interaction.response.is_done():
                    await interaction.response.edit_message(
                        content=f"Gifted successfully to {self.receiver.display_name}.", view=None)
                else:
                    await interaction.followup.send(content=f"Gifted successfully to {self.receiver.display_name}.",
                                                    ephemeral=True)

            except Exception as e:
                if transaction_started:
                    await conn.rollback()
                logger.error(f"Failed to transfer gift: {e}")
                if not interaction.response.is_done():
                    await interaction.response.send_message("Failed to transfer the gift due to an internal error.",
                                                            ephemeral=True)
                else:
                    await interaction.followup.send("Failed to transfer the gift due to an internal error.",
                                                    ephemeral=True)


# ---------------------------------------------------------------------------------------------------------------------
# User Cog Class
# ---------------------------------------------------------------------------------------------------------------------
class UserCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.show_descriptions = True

# ---------------------------------------------------------------------------------------------------------------------
# User Commands
# ---------------------------------------------------------------------------------------------------------------------
    @app_commands.command(description="User: Check your inventory")
    async def inventory(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            async with aiosqlite.connect(db_path) as conn:
                cursor = await conn.execute('''
                    SELECT balance FROM economy WHERE user_id = ? AND guild_id = ?
                ''', (interaction.user.id, interaction.guild.id))
                balance_row = await cursor.fetchone()
                balance = balance_row[0] if balance_row else 0

                # Fetch inventory items based on user_inventory table
                cursor = await conn.execute('''
                    SELECT ui.guild_id, ui.card_id, ui.quantity, c.name, c.description, c.rarity, c.img_url
                    FROM user_inventory ui
                    JOIN cards c ON ui.card_id = c.card_id AND ui.guild_id = c.guild_id
                    WHERE ui.user_id = ? AND ui.guild_id = ? AND ui.quantity > 0
                ''', (interaction.user.id, interaction.guild.id))
                inventory_items = await cursor.fetchall()

                if inventory_items:
                    colour = await get_embed_colour(interaction.guild.id)
                    embeds = []
                    items_per_page = 5
                    page_count = (len(inventory_items) + items_per_page - 1) // items_per_page

                    for page in range(page_count):
                        start_index = page * items_per_page
                        end_index = start_index + items_per_page
                        page_items = inventory_items[start_index:end_index]

                        embed = discord.Embed(title="Your Inventory", color=colour)
                        embed.set_thumbnail(url=interaction.user.display_avatar.url)

                        embed_description = ""
                        for index, item in enumerate(page_items, start=1):
                            # Get the description if available, otherwise provide a fallback
                            description = item[4] if item[4] else "No description available"

                            # If descriptions are to be shown, add the real description
                            if self.show_descriptions:
                                embed_description += f"{index + start_index}. **{item[3]}** (x{item[2]})\n*{description}*\n\n"
                            else:
                                # If descriptions are hidden, just show a placeholder message or leave it blank
                                embed_description += f"{index + start_index}. **{item[3]}** (x{item[2]})\nNo description available\n\n"

                        # Set the final description of the embed
                        embed.description = embed_description.strip()

                        embed.set_footer(text=f"Points Balance: {balance}")
                        embed.timestamp = discord.utils.utcnow()
                        embeds.append(embed)

                    # Create pagination view and send the first embed
                    view = InventoryPaginationView(embeds, self.bot, interaction.user.id, show_descriptions=self.show_descriptions)
                    await interaction.followup.send(embed=embeds[0], view=view, ephemeral=True)

                else:
                    await interaction.followup.send("Your inventory is empty.", ephemeral=True)

        except Exception as e:
            logger.error(f"Failed to fetch inventory or process command: {e}")
            await interaction.followup.send("Failed to process your request due to an internal error.", ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)

    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="User: Gift an item to another user")
    @app_commands.describe(member="The member to gift an item to", item_name="The item name from your inventory")
    @app_commands.autocomplete(item_name=inventory_autocomplete)
    async def gift(self, interaction: discord.Interaction, member: discord.Member, item_name: str):
        #await interaction.response.defer(ephemeral=True)
        await interaction.response.send_message("This command is temporarily disabled", ephemeral=True)
        return
        try:
            async with aiosqlite.connect(db_path) as conn:
                cursor = await conn.execute('''
                    SELECT cards.card_id, cards.name, user_inventory.quantity, cards.rarity
                    FROM user_inventory
                    JOIN cards ON user_inventory.card_id = cards.card_id
                    WHERE user_inventory.user_id = ? AND user_inventory.guild_id = ? AND cards.name = ?
                ''', (interaction.user.id, interaction.guild.id, item_name))
                inventory_item = await cursor.fetchone()

            if inventory_item:
                # Directly process the gift
                view = GiftSelectView([inventory_item], self.bot, interaction.user, member)
                await view.perform_gift(interaction, inventory_item[0])
            else:
                await interaction.followup.send("The selected item is not available in your inventory.", ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to initiate gifting process: {e}")
            await interaction.followup.send("Failed to initiate the gifting process due to an internal error.",
                                            ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)

    # ---------------------------------------------------------------------------------------------------------------------
    # Profile Commands
    # ---------------------------------------------------------------------------------------------------------------------
    @app_commands.command(description="User: View a user's profile")
    @app_commands.describe(member="The member whose profile you want to view")
    async def profile(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        colour = await get_embed_colour(interaction.guild.id)
        try:
            async with aiosqlite.connect(db_path) as conn:
                cursor = await conn.execute('''
                    SELECT bio, favourite_card, searching_for FROM user_profiles
                    WHERE user_id = ? AND guild_id = ?
                ''', (member.id, interaction.guild.id))
                profile = await cursor.fetchone()

                cursor = await conn.execute('''
                    SELECT COUNT(*), SUM(quantity) FROM user_inventory
                    WHERE user_id = ? AND guild_id = ?
                ''', (member.id, interaction.guild.id))
                result = await cursor.fetchone()
                card_count, total_cards = result if result else (0, 0)

                fav_card, fav_card_img = None, None
                if profile and profile[1]:
                    cursor = await conn.execute('''
                        SELECT name, img_url FROM cards WHERE name = ? AND guild_id = ?
                    ''', (profile[1], interaction.guild.id))
                    fav_result = await cursor.fetchone()
                    if fav_result:
                        fav_card, fav_card_img = fav_result

                search_card = profile[2] if profile and profile[2] else "n/a"

                embed = discord.Embed(title=f"Profile for '{member.display_name}'",
                                      description=f"*{profile[0] if profile else 'No bio available.'}* \n",
                                      color= colour)
                embed.set_thumbnail(url=member.display_avatar.url)

                embed.add_field(name="Extra Information:",
                                value=f"Total Cards: {total_cards} \n"
                                      f"Favourite Card: *{fav_card if fav_card else 'n/a'}* \n"
                                      f"Looking For: *{search_card}* \n"
                                      f"Joined: *{member.joined_at.strftime('%d/%m/%Y')}*",
                                inline=False)

                if fav_card_img:
                    embed.set_image(url=fav_card_img)

                embed.set_footer(text="Profile Information")
                embed.timestamp = discord.utils.utcnow()
                await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Failed to fetch profile: {e}")
            await interaction.followup.send("Failed to retrieve profile.", ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)

    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="User: Update your profile information")
    @app_commands.describe(bio="A short description about you", favourite_card="Your favourite card",
                           searching_for="What you are currently searching for in the game")
    async def profile_update(self, interaction: discord.Interaction, bio: str = None, favourite_card: str = None,
                             searching_for: str = None):
        await interaction.response.defer(ephemeral=True)
        try:
            async with aiosqlite.connect(db_path) as conn:
                cursor = await conn.execute('''
                    SELECT bio, favourite_card, searching_for FROM user_profiles
                    WHERE user_id = ? AND guild_id = ?
                ''', (interaction.user.id, interaction.guild.id))
                existing_profile = await cursor.fetchone()

                new_bio = bio if bio is not None else (existing_profile[0] if existing_profile else "")
                new_favourite_card = favourite_card if favourite_card is not None else (
                    existing_profile[1] if existing_profile else "")
                new_searching_for = searching_for if searching_for is not None else (
                    existing_profile[2] if existing_profile else "")

                await conn.execute('''
                    INSERT INTO user_profiles (user_id, guild_id, bio, favourite_card, searching_for)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, guild_id) DO UPDATE SET
                    bio = excluded.bio,
                    favourite_card = excluded.favourite_card,
                    searching_for = excluded.searching_for
                ''', (interaction.user.id, interaction.guild.id, new_bio, new_favourite_card, new_searching_for))
                await conn.commit()
            await interaction.followup.send("Profile updated successfully!", ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to update profile: {e}")
            await interaction.followup.send("Failed to update your profile.", ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)


# ---------------------------------------------------------------------------------------------------------------------
# Setup Function
# ---------------------------------------------------------------------------------------------------------------------
async def setup(bot):
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                bio TEXT,
                favourite_card TEXT,
                searching_for TEXT,
                PRIMARY KEY (user_id, guild_id)
            )
        ''')
        await conn.commit()
    await bot.add_cog(UserCog(bot))
