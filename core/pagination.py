import discord
import os
import logging
import aiosqlite

from discord.ui import View, Button
from core.utils import get_embed_colour

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
# Inventory Pagination View
# ---------------------------------------------------------------------------------------------------------------------

class InventoryPaginationView(View):
    def __init__(self, embeds, bot, user_id, show_descriptions=True, is_ephemeral=True):
        super().__init__(timeout=180)
        self.bot = bot
        self.embeds = embeds
        self.current_page = 0
        self.user_id = user_id
        self.show_descriptions = show_descriptions
        self.is_ephemeral = is_ephemeral
        self.public_message = None

        # Pagination buttons
        self.previous_button = Button(style=discord.ButtonStyle.secondary, label="Prev", disabled=True)
        self.home_button = Button(style=discord.ButtonStyle.primary, label="Home")
        self.next_button = Button(style=discord.ButtonStyle.secondary, label="Next", disabled=(len(embeds) <= 1))

        # New buttons
        self.show_hide_button = Button(
            style=discord.ButtonStyle.success,
            label="Hide" if not self.is_ephemeral else "Show",
            row=1
        )
        self.toggle_descriptions_button = Button(
            style=discord.ButtonStyle.secondary,
            label="Hide Descriptions" if self.show_descriptions else "Show Descriptions",
            row=1
        )

        # Add buttons to the view
        self.add_item(self.previous_button)
        self.add_item(self.home_button)
        self.add_item(self.next_button)
        self.add_item(self.show_hide_button)
        self.add_item(self.toggle_descriptions_button)

        # Assign callbacks to buttons
        self.previous_button.callback = self.previous_page
        self.home_button.callback = self.go_home
        self.next_button.callback = self.next_page
        self.show_hide_button.callback = self.toggle_visibility
        self.toggle_descriptions_button.callback = self.toggle_descriptions

    async def previous_page(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("You cannot interact with this inventory.", ephemeral=True)
            return

        self.current_page -= 1
        if self.current_page == 0:
            self.previous_button.disabled = True
        self.next_button.disabled = False
        await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

    async def go_home(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("You cannot interact with this inventory.", ephemeral=True)
            return

        self.current_page = 0
        self.previous_button.disabled = True
        self.next_button.disabled = (len(self.embeds) <= 1)
        await interaction.response.edit_message(embed=self.embeds[0], view=self)

    async def next_page(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("You cannot interact with this inventory.", ephemeral=True)
            return

        self.current_page += 1
        if self.current_page == len(self.embeds) - 1:
            self.next_button.disabled = True
        self.previous_button.disabled = False
        await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

    async def toggle_visibility(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("You cannot interact with this inventory.", ephemeral=True)
            return

        # Acknowledge the interaction early to prevent timeout
        await interaction.response.defer()

        # Toggle ephemeral state
        self.is_ephemeral = not self.is_ephemeral
        self.show_hide_button.label = "Hide" if not self.is_ephemeral else "Show"

        # Update the embed title
        for embed in self.embeds:
            if not self.is_ephemeral:
                embed.title = f"{interaction.user.display_name}'s Inventory"
            else:
                embed.title = "Your Inventory"

        # If making the inventory public
        if not self.is_ephemeral:
            # Delete the ephemeral message (if any)
            try:
                await interaction.delete_original_response()
            except discord.NotFound:
                # If the original response is already deleted, ignore the error
                pass

            # Send the inventory as a public message
            self.public_message = await interaction.channel.send(embed=self.embeds[self.current_page], view=self)
        else:
            # If hiding the inventory, delete the public message and resend as ephemeral
            if self.public_message:
                await self.public_message.delete()
                self.public_message = None

            # Resend the inventory as an ephemeral message
            await interaction.followup.send(embed=self.embeds[self.current_page], view=self, ephemeral=True)

    async def toggle_descriptions(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("You cannot interact with this inventory.", ephemeral=True)
            return

        # Toggle descriptions visibility
        self.show_descriptions = not self.show_descriptions
        self.toggle_descriptions_button.label = "Hide Descriptions" if self.show_descriptions else "Show Descriptions"

        # Rebuild the embeds with or without card descriptions
        new_embeds = []
        async with aiosqlite.connect(db_path) as conn:
            # Fetch balance
            cursor = await conn.execute('''
                SELECT balance FROM economy WHERE user_id = ? AND guild_id = ?
            ''', (interaction.user.id, interaction.guild.id))
            balance_row = await cursor.fetchone()
            balance = balance_row[0] if balance_row else 0

            # Fetch inventory items
            cursor = await conn.execute('''
                SELECT ui.guild_id, ui.card_id, ui.quantity, c.name, c.description, c.rarity, c.img_url
                FROM user_inventory ui
                JOIN cards c ON ui.card_id = c.card_id AND ui.guild_id = c.guild_id
                WHERE ui.user_id = ? AND ui.guild_id = ? AND ui.quantity > 0
            ''', (interaction.user.id, interaction.guild.id))
            inventory_items = await cursor.fetchall()

            if inventory_items:
                colour = await get_embed_colour(interaction.guild.id)
                items_per_page = 5
                page_count = (len(inventory_items) + items_per_page - 1) // items_per_page

                for page in range(page_count):
                    start_index = page * items_per_page
                    end_index = start_index + items_per_page
                    page_items = inventory_items[start_index:end_index]

                    embed = discord.Embed(color=colour)
                    embed.set_thumbnail(url=interaction.user.display_avatar.url)

                    # Update the title based on whether the message is public or not
                    if not self.is_ephemeral:
                        embed.title = f"{interaction.user.display_name}'s Inventory"
                    else:
                        embed.title = "Your Inventory"

                    embed_description = ""
                    for index, item in enumerate(page_items, start=1):
                        # Get the description if available, otherwise provide a fallback
                        description = item[4] if item[4] else None

                        # If descriptions are to be shown, add the real description
                        if self.show_descriptions and description:
                            embed_description += f"{index + start_index}. **{item[3]}** (x{item[2]})\n*{description}*\n\n"
                        elif not self.show_descriptions:
                            # If descriptions are hidden, leave it blank
                            embed_description += f"{index + start_index}. **{item[3]}** (x{item[2]})\n\n"

                    # Set the final description of the embed
                    embed.description = embed_description.strip()
                    embed.set_footer(text=f"Points Balance: {balance}")
                    embed.timestamp = discord.utils.utcnow()
                    new_embeds.append(embed)

                # Update the embeds with the new description visibility
                self.embeds = new_embeds

                # Edit the existing ephemeral message instead of sending a new one
                await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)
            else:
                await interaction.response.send_message("Your inventory is empty.", ephemeral=True)
