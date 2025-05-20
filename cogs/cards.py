import discord
import logging
import aiosqlite
import os
import base64

from discord.ext import commands
from discord import app_commands
from discord.ui import View, Button
from openai import AsyncOpenAI


from core.utils import log_command_usage, check_permissions, get_embed_colour
from core.pagination import InventoryPaginationView
from core.autocomplete import rarity_autocomplete, set_name_autocomplete, card_name_autocomplete, non_preset_card_name_autocomplete
from config import OPENAI_MODERATION_KEY

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
# Cards Cog Class
# ---------------------------------------------------------------------------------------------------------------------
class CardCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

        if not OPENAI_MODERATION_KEY:
            raise RuntimeError("OPENAI_MODERATION_KEY must be set for image moderation")
        self.mod_client = AsyncOpenAI(api_key=OPENAI_MODERATION_KEY)

# ---------------------------------------------------------------------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------------------------------------------------------------------
    async def get_support_server_channel_id(self) -> int:
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute('SELECT card_channel_id FROM config')
            row = await cursor.fetchone()
            return row[0] if row else None

    async def get_card_channel_id(self, guild_id: int) -> int:
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute('SELECT card_channel_id FROM config WHERE guild_id = ?', (guild_id,))
            row = await cursor.fetchone()
            return row[0] if row else None

    async def add_card_to_set(self, card_name, set_id, guild_id):
        async with aiosqlite.connect(db_path) as conn:
            # Fetch the card_id using the card_name
            cursor = await conn.execute("SELECT card_id FROM cards WHERE name = ? AND guild_id = ?",
                                        (card_name, guild_id))
            card_row = await cursor.fetchone()

            if not card_row:
                return False  # Card not found in the database

            card_id = card_row[0]

            # Check if the card is already in the set
            cursor = await conn.execute("SELECT * FROM set_cards WHERE set_id = ? AND card_id = ? AND guild_id = ?",
                                        (set_id, card_id, guild_id))
            existing_card = await cursor.fetchone()

            if existing_card:
                return False  # Card already exists in the set

            await conn.execute("INSERT INTO set_cards (set_id, card_id, guild_id) VALUES (?, ?, ?)",
                               (set_id, card_id, guild_id))

            await conn.commit()
            return True

    async def is_card_part_of_preset(self, card_id: str, guild_id: int) -> bool:
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute('''
                SELECT 1 
                FROM set_cards 
                JOIN card_sets ON set_cards.set_id = card_sets.set_id 
                WHERE set_cards.card_id = ? AND set_cards.guild_id = ? AND card_sets.is_preset = 1
            ''', (card_id, guild_id))
            result = await cursor.fetchone()
            return result is not None


# ---------------------------------------------------------------------------------------------------------------------
# Card Commands
# ---------------------------------------------------------------------------------------------------------------------
    @app_commands.command(description="Admin: Add a custom card")
    @app_commands.describe(
        name="The name of the card",
        description="A brief description of the card (optional, max 180 characters)",
        file="The image file to be uploaded",
        rarity="The rarity of the card",
        set="Optional: Add the card to an existing set"
    )
    @app_commands.autocomplete(rarity=rarity_autocomplete, set=set_name_autocomplete)
    async def card_create(self, interaction: discord.Interaction, name: str, file: discord.Attachment, rarity: str,
                          description: str = None, set: str = None):
        if not await check_permissions(interaction):
            await interaction.response.send_message(
                "You do not have permission to use this command. An Admin needs to `/authorise` you!", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            if description and len(description) > 180:
                await interaction.followup.send(
                    f"Error: The description must be 180 characters or fewer. Your description has {len(description)} characters.",
                    ephemeral=True
                )
                return

            async with aiosqlite.connect(db_path) as conn:
                cursor = await conn.execute("SELECT DISTINCT rarity FROM rarity_weights WHERE guild_id = ?",
                                            (interaction.guild.id,))
                valid_rarities = {row[0].lower() for row in await cursor.fetchall()}

            if rarity.lower() not in valid_rarities:
                await interaction.followup.send(
                    f"Error: Invalid rarity. Choose from: {', '.join(valid_rarities)}.", ephemeral=True
                )
                return

            # Get card channel
            card_channel_id = await self.get_support_server_channel_id()
            if not card_channel_id:
                await interaction.followup.send("Error: Card image channel not configured. Run setup first.",
                                                ephemeral=True)
                return

            channel = self.bot.get_channel(int(card_channel_id))
            if not channel:
                await interaction.followup.send("Error: Cannot access the configured image channel.", ephemeral=True)
                return

            guild_dir = f'./data/card_images/{interaction.guild.id}'
            os.makedirs(guild_dir, exist_ok=True)
            file_path = f'{guild_dir}/{file.filename}'
            await file.save(file_path)

            try:
                print("DEBUG: Starting image moderation")  # or logger.info(...)
                with open(file_path, "rb") as f:
                    raw = f.read()
                b64 = base64.b64encode(raw).decode("utf-8")
                ext = file.filename.rsplit(".", 1)[-1].lower()
                data_url = f"data:image/{ext};base64,{b64}"
                print(f"DEBUG: data_url length = {len(data_url)}")

                # 3) Call the omni moderation endpoint
                resp = await self.mod_client.moderations.create(
                    model="omni-moderation-latest",
                    input=[{
                        "type": "image_url",
                        "image_url": {"url": data_url}
                    }]
                )
                result = resp.results[0]
                print(f"DEBUG: moderation categories = {result.categories}")

                # 4) Reject if sexual flagged
                if result.categories.sexual_minors:
                    print("DEBUG: content flagged as sexual → rejecting")
                    os.remove(file_path)
                    await interaction.followup.send(
                        "❌ Upload rejected: image flagged as sexual content.",
                        ephemeral=True
                    )
                    return
                else:
                    print("DEBUG: content passed moderation")

            except Exception as e:
                print(f"DEBUG: Moderation threw exception: {e}")
                logger.error(f"Image moderation failed: {e}")
                if os.path.exists(file_path):
                    os.remove(file_path)
                await interaction.followup.send(
                    "⚠️ Could not verify the image for policy compliance. Please try again later.",
                    ephemeral=True
                )
                return
            # Upload to Discord
            discord_file = discord.File(file_path)
            message = await channel.send(file=discord_file)
            image_url = message.attachments[0].url

            async with aiosqlite.connect(db_path) as conn:
                cursor = await conn.execute('''
                    INSERT INTO cards (guild_id, name, description, rarity, img_url, local_img_url)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (interaction.guild.id, name, description, rarity, image_url, file_path))
                await conn.commit()
                new_card_id = cursor.lastrowid

            display_id = f"{new_card_id:08d}"

            if set:
                set_cog = self.bot.get_cog("SetCog")
                if not set_cog:
                    await interaction.followup.send("Set functionality unavailable. Please check setup.",
                                                    ephemeral=True)
                    return

                result = await set_cog.add_card_to_set(name, set, interaction.guild.id)
                if result:
                    await interaction.followup.send(
                        f"Card `{name}` added successfully with ID `{display_id}` and linked to set `{set}`!",
                        ephemeral=True)
                else:
                    await interaction.followup.send(
                        f"Card `{name}` added with ID `{display_id}`, but couldn't be linked to set `{set}` (possibly already added).",
                        ephemeral=True)
            else:
                await interaction.followup.send(f"Card `{name}` added successfully with ID `{display_id}`!",
                                                ephemeral=True)

        except Exception as e:
            logger.error(f"Failed to add card: {e}")
            await interaction.followup.send("Error: Something unexpected happened during card creation.",
                                            ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)

    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="Admin: Delete a custom card")
    @app_commands.autocomplete(card_name=card_name_autocomplete)
    @app_commands.describe(card_name="The name of the card to delete")
    async def card_delete(self, interaction: discord.Interaction, card_name: str):
        if not await check_permissions(interaction):
            await interaction.response.send_message("You do not have permission to use this command. "
                                                    "An Admin needs to `/authorise` you!",
                                                    ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            async with aiosqlite.connect(db_path) as conn:
                cursor = await conn.execute('SELECT card_id, name FROM cards WHERE name = ? AND guild_id = ?',
                                            (card_name, interaction.guild.id))
                card = await cursor.fetchone()
                if card:
                    await conn.execute('DELETE FROM cards WHERE card_id = ? AND guild_id = ?',
                                       (card[0], interaction.guild.id))
                    await conn.commit()
                    await interaction.followup.send(f"Card `{card_name}` has been successfully removed.",
                                                    ephemeral=True)
                else:
                    await interaction.followup.send("Card not found. Please check the name and try again.",
                                                    ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to delete card: {e}")
            await interaction.followup.send(f"Failed to process your request due to an internal error: {e}",
                                            ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)

    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="Admin: Give a card to a user")
    @app_commands.describe(member="The member to receive the card", card_name="The name of the card to give")
    @app_commands.autocomplete(card_name=card_name_autocomplete)
    async def card_give(self, interaction: discord.Interaction, member: discord.Member, card_name: str):
        if not await check_permissions(interaction):
            await interaction.response.send_message(
                "You do not have permission to use this command. An Admin needs to `/authorise` you!",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            async with aiosqlite.connect(db_path) as conn:
                # Check if the card exists in the cards table
                cursor = await conn.execute(
                    'SELECT card_id, name FROM cards WHERE name = ? AND guild_id = ?',
                    (card_name, interaction.guild.id)
                )
                card = await cursor.fetchone()

                if card:
                    # Check if the user already has this card in their inventory
                    cursor = await conn.execute(
                        'SELECT quantity FROM user_inventory WHERE guild_id = ? AND user_id = ? AND card_id = ?',
                        (interaction.guild.id, member.id, card[0])
                    )
                    existing_card = await cursor.fetchone()

                    if existing_card:
                        # Update the quantity if the card already exists
                        await conn.execute(
                            'UPDATE user_inventory SET quantity = quantity + 1 WHERE guild_id = ? AND user_id = ? AND card_id = ?',
                            (interaction.guild.id, member.id, card[0])
                        )
                    else:
                        # Insert a new row for the card if it doesn't exist
                        await conn.execute(
                            'INSERT INTO user_inventory (guild_id, user_id, card_id, quantity) VALUES (?, ?, ?, 1)',
                            (interaction.guild.id, member.id, card[0])
                        )

                    await conn.commit()
                    await interaction.followup.send(
                        f"Card `{card_name}` has been successfully given to {member.display_name}.",
                        ephemeral=True
                    )
                else:
                    # If the card does not exist, notify the user
                    await interaction.followup.send("Card not found. Please check the name and try again.",
                                                    ephemeral=True)

        except Exception as e:
            logger.error(f"Failed to give card: {e}")
            await interaction.followup.send(
                f"Failed to process your request due to an internal error: {e}",
                ephemeral=True
            )
        finally:
            await log_command_usage(self.bot, interaction)

    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="Admin: Remove a card from a user's inventory")
    @app_commands.describe(member="The member from whom to remove the card", card_name="The name of the card to remove")
    @app_commands.autocomplete(card_name=card_name_autocomplete)
    async def card_remove(self, interaction: discord.Interaction, member: discord.Member, card_name: str):
        if not await check_permissions(interaction):
            await interaction.response.send_message("You do not have permission to use this command. "
                                                    "An Admin needs to `/authorise` you!",
                                                    ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            async with aiosqlite.connect(db_path) as conn:
                cursor = await conn.execute('SELECT card_id, name FROM cards WHERE name = ? AND guild_id = ?',
                                            (card_name, interaction.guild.id))
                card = await cursor.fetchone()
                if card:
                    cursor = await conn.execute(
                        'SELECT quantity FROM user_inventory WHERE user_id = ? AND card_id = ? AND guild_id = ?',
                        (member.id, card[0], interaction.guild.id))
                    result = await cursor.fetchone()
                    if result and result[0] > 1:
                        new_quantity = result[0] - 1
                        await conn.execute(
                            'UPDATE user_inventory SET quantity = ? WHERE user_id = ? AND card_id = ? AND guild_id = ?',
                            (new_quantity, member.id, card[0], interaction.guild.id))
                    else:
                        await conn.execute('DELETE FROM user_inventory WHERE user_id = ? AND card_id = ? AND guild_id = ?',
                                           (member.id, card[0], interaction.guild.id))
                    await conn.commit()
                    await interaction.followup.send(
                        f"Removed 1x `{card_name}` from {member.display_name}'s inventory.", ephemeral=True)
                else:
                    await interaction.followup.send("Card not found. Please check the name and try again.",
                                                    ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to remove card: {e}")
            await interaction.followup.send(f"Failed to process your request due to an internal error: {e}",
                                            ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)

    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="Admin: Edit an existing card")
    @app_commands.describe(
        card_name="The name of the card to edit",
        new_name="New name for the card (optional)",
        new_description="New description for the card (optional)",
        new_rarity="New rarity for the card (optional)",
        new_file="New image file for the card (optional)"
    )
    @app_commands.autocomplete(card_name=non_preset_card_name_autocomplete, new_rarity=rarity_autocomplete)
    async def card_edit(self, interaction: discord.Interaction, card_name: str, new_name: str = None,
                        new_description: str = None,
                        new_rarity: str = None, new_file: discord.Attachment = None):
        if not await check_permissions(interaction):
            await interaction.response.send_message(
                "You do not have permission to use this command. An Admin needs to `/authorise` you!",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            # Validate description length
            if new_description and len(new_description) > 180:
                await interaction.followup.send(
                    f"Error: The description must be 180 characters or fewer. Your description has {len(new_description)} characters.",
                    ephemeral=True
                )
                return

            async with aiosqlite.connect(db_path) as conn:
                # Fetch the card from the database
                cursor = await conn.execute(
                    "SELECT card_id, name, description, rarity, img_url, local_img_url FROM cards WHERE name = ? AND guild_id = ?",
                    (card_name, interaction.guild.id)
                )
                card = await cursor.fetchone()

                if not card:
                    await interaction.followup.send("Error: Card not found.", ephemeral=True)
                    return

                card_id, old_name, old_description, old_rarity, old_img_url, old_local_img_url = card

                # Keep old values if new ones are not provided
                new_name = new_name or old_name
                new_description = new_description or old_description
                # Normalize rarity
                new_rarity = new_rarity.strip().lower() if new_rarity else old_rarity

                # Validate rarity dynamically
                cursor = await conn.execute(
                    "SELECT rarity FROM rarity_weights WHERE guild_id = ?",
                    (interaction.guild.id,)
                )
                valid_rarities = [row[0].lower() for row in await cursor.fetchall()]
                await cursor.close()

                if new_rarity.lower() not in valid_rarities:
                    await interaction.followup.send(
                        f"Error: Invalid rarity. Available options are: `{', '.join(valid_rarities)}`.",
                        ephemeral=True
                    )
                    return

                # Handle file upload if a new file is provided
                new_img_url = old_img_url
                new_local_img_url = old_local_img_url

                if new_file:
                    guild_dir = f'./data/card_images/{interaction.guild.id}'
                    os.makedirs(guild_dir, exist_ok=True)

                    file_path = f'{guild_dir}/{new_file.filename}'
                    await new_file.save(file_path)

                    card_channel_id = await self.get_support_server_channel_id()
                    if not card_channel_id:
                        await interaction.followup.send(
                            "Error: Card image channel is not configured properly in the support server. Please run the setup command.",
                            ephemeral=True
                        )
                        return

                    channel = self.bot.get_channel(int(card_channel_id))
                    if not channel:
                        await interaction.followup.send(
                            "Error: The configured channel ID is invalid or the bot does not have access to it.",
                            ephemeral=True
                        )
                        return

                    discord_file = discord.File(file_path)
                    message = await channel.send(file=discord_file)
                    new_img_url = message.attachments[0].url
                    new_local_img_url = file_path

                # Update the database entry
                await conn.execute(
                    "UPDATE cards SET name = ?, description = ?, rarity = ?, img_url = ?, local_img_url = ? WHERE card_id = ? AND guild_id = ?",
                    (new_name, new_description, new_rarity, new_img_url, new_local_img_url, card_id,
                     interaction.guild.id)
                )
                await conn.commit()

                await interaction.followup.send(
                    f"Card `{old_name}` has been updated successfully!",
                    ephemeral=True
                )

        except Exception as e:
            logger.error(f"Failed to edit card: {e}")
            await interaction.followup.send("Error: Something unexpected happened while updating the card.",
                                            ephemeral=True)

        finally:
            await log_command_usage(self.bot, interaction)

    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="User: Get information about a specific card")
    @app_commands.describe(card_name="The name of the card to get information about")
    @app_commands.autocomplete(card_name=card_name_autocomplete)
    async def info(self, interaction: discord.Interaction, card_name: str):
        async with aiosqlite.connect(db_path) as conn:
            try:
                cursor = await conn.execute(
                    "SELECT name, description, rarity, img_url, local_img_url FROM cards WHERE name LIKE ? AND guild_id = ?",
                    ('%' + card_name + '%', interaction.guild.id,)
                )
                card = await cursor.fetchone()

                if card:
                    guild_id = interaction.guild.id
                    colour = await get_embed_colour(guild_id)
                    embed = discord.Embed(title=f"{card[0]} ({card[2].capitalize()})", description=f"*{card[1]}*",
                                          color=colour)

                    if card[3]:
                        embed.set_image(url=card[3])
                        embed.set_footer(text=f"Card Information for '{card[0]}'")
                        embed.timestamp = discord.utils.utcnow()
                        await interaction.response.send_message(embed=embed)
                    else:
                        card_channel_id = await self.get_card_channel_id(guild_id)
                        if not card_channel_id:
                            await interaction.followup.send(
                                "Error: Card image channel is not configured properly. Please run the setup command.",
                                ephemeral=True)
                            return

                        channel = self.bot.get_channel(int(card_channel_id))
                        if not channel:
                            await interaction.followup.send(
                                "Error: The configured channel ID is invalid or the bot does not have access to it.",
                                ephemeral=True)
                            return

                        try:
                            with open(card[4], 'rb') as image_file:
                                discord_file = discord.File(image_file)
                                message = await channel.send(file=discord_file)
                                new_image_url = message.attachments[0].url

                                await conn.execute(
                                    "UPDATE cards SET img_url = ? WHERE card_id = ? AND guild_id = ?",
                                    (new_image_url, card[4], interaction.guild.id)
                                )
                                await conn.commit()

                                embed.set_image(url=new_image_url)
                                embed.set_footer(text=f"Card Information for '{card[0]}'")
                                embed.timestamp = discord.utils.utcnow()
                                await interaction.response.send_message(embed=embed)

                        except Exception as e:
                            logger.error(f"Failed to re-upload card image: {e}")
                            await interaction.response.send_message(
                                "Error: Failed to re-upload the card image. Please contact the admin.",
                                ephemeral=True
                            )

                else:
                    await interaction.response.send_message("Card not found. Please check the name and try again.",
                                                            ephemeral=True)

            except Exception as e:
                logger.error(f"Failed to fetch card information: {e}")
                await interaction.response.send_message(f"Failed to process your request due to an internal error: {e}",
                                                        ephemeral=True)
            finally:
                await log_command_usage(self.bot, interaction)

    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="Admin: Inspect a user's inventory")
    @app_commands.describe(member="The member whose inventory you want to inspect")
    async def inspect_inventory(self, interaction: discord.Interaction, member: discord.Member):
        if not await check_permissions(interaction):
            await interaction.response.send_message(
                "You do not have permission to use this command. An Admin needs to `/authorise` you!",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            async with aiosqlite.connect(db_path) as conn:
                # Fetch the user's inventory
                cursor = await conn.execute('''
                    SELECT cards.name, user_inventory.quantity 
                    FROM user_inventory
                    JOIN cards ON user_inventory.card_id = cards.card_id
                    WHERE user_inventory.guild_id = ? AND user_inventory.user_id = ?
                ''', (interaction.guild.id, member.id))
                inventory = await cursor.fetchall()

                if not inventory:
                    await interaction.followup.send(f"{member.display_name} has no cards in their inventory.",
                                                    ephemeral=True)
                    return

                # Create embeds for pagination
                guild_id = interaction.guild.id
                embed_colour = await get_embed_colour(guild_id)
                embeds = []

                # Split inventory into chunks of 10 items per page
                for i in range(0, len(inventory), 10):
                    page_items = inventory[i:i + 10]
                    embed = discord.Embed(title=f"{member.display_name}'s Inventory", color=embed_colour)
                    for card_name, quantity in page_items:
                        embed.add_field(name=card_name, value=f"Quantity: {quantity}", inline=False)
                    embed.set_footer(
                        text=f"Page {len(embeds) + 1} of {(len(inventory) - 1) // 10 + 1} | Inventory for {member.display_name}")
                    embed.timestamp = discord.utils.utcnow()
                    embeds.append(embed)

                # Create the pagination view
                view = InventoryPaginationView(embeds, self.bot, interaction.user.id, is_ephemeral=True)

                # Remove the "Show/Hide" and "Toggle Descriptions" buttons
                view.remove_item(view.show_hide_button)
                view.remove_item(view.toggle_descriptions_button)

                # Send the initial embed
                await interaction.followup.send(embed=embeds[0], view=view, ephemeral=True)

        except Exception as e:
            logger.error(f"Failed to inspect inventory: {e}")
            await interaction.followup.send(
                f"Failed to process your request due to an internal error: {e}",
                ephemeral=True
            )
        finally:
            await log_command_usage(self.bot, interaction)


# ---------------------------------------------------------------------------------------------------------------------
# Setup Function
# ---------------------------------------------------------------------------------------------------------------------
async def setup(bot):
    async with aiosqlite.connect(db_path) as conn:
        # Create the cards table if it doesn't exist
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS cards (
                guild_id INTEGER,
                card_id TEXT,
                name TEXT NOT NULL,
                description TEXT,
                rarity TEXT,
                img_url TEXT,
                local_img_url TEXT,
                PRIMARY KEY (card_id, guild_id)
            )
        ''')

        # Recreate the user_inventory table to ensure the correct primary key is set
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_inventory(
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                card_id TEXT NOT NULL,
                quantity INTEGER,
                FOREIGN KEY (card_id) REFERENCES cards(card_id),
                PRIMARY KEY (guild_id, user_id, card_id)
            )
        ''')

    await bot.add_cog(CardCog(bot))
