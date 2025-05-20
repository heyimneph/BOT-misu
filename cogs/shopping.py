import discord
import logging
import aiosqlite
import os

from discord.ext import commands
from discord import app_commands
from datetime import datetime

from core.utils import log_command_usage, check_permissions, get_embed_colour

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
# TradeRequestView Class
# ---------------------------------------------------------------------------------------------------------------------
class TradeRequestView(discord.ui.View):
    def __init__(self, bot, user1, user1_item_id, user1_item_name, user2, user2_item_id, user2_item_name, guild_id):
        super().__init__(timeout=43200)
        self.bot = bot
        self.user1 = user1
        self.user1_item_id = user1_item_id
        self.user1_item_name = user1_item_name
        self.user2 = user2
        self.user2_item_id = user2_item_id
        self.user2_item_name = user2_item_name
        self.guild_id = guild_id

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with aiosqlite.connect(db_path) as conn:
            try:
                await conn.execute('BEGIN')

                await conn.execute(
                    "UPDATE user_inventory SET quantity = quantity - 1 WHERE user_id = ? AND card_id = ? AND guild_id = ?",
                    (self.user1.id, self.user1_item_id, self.guild_id))
                await conn.execute(
                    "UPDATE user_inventory SET quantity = quantity - 1 WHERE user_id = ? AND card_id = ? AND guild_id = ?",
                    (self.user2.id, self.user2_item_id, self.guild_id))

                cursor = await conn.execute(
                    "SELECT quantity FROM user_inventory WHERE user_id = ? AND card_id = ? AND guild_id = ?",
                    (self.user2.id, self.user1_item_id, self.guild_id))
                user2_item_exists = await cursor.fetchone()

                if user2_item_exists:
                    await conn.execute(
                        "UPDATE user_inventory SET quantity = quantity + 1 WHERE user_id = ? AND card_id = ? AND guild_id = ?",
                        (self.user2.id, self.user1_item_id, self.guild_id))
                else:
                    await conn.execute(
                        "INSERT INTO user_inventory (guild_id, user_id, card_id, quantity) VALUES (?, ?, ?, 1)",
                        (self.guild_id, self.user2.id, self.user1_item_id))

                cursor = await conn.execute(
                    "SELECT quantity FROM user_inventory WHERE user_id = ? AND card_id = ? AND guild_id = ?",
                    (self.user1.id, self.user2_item_id, self.guild_id))
                user1_item_exists = await cursor.fetchone()

                if user1_item_exists:
                    await conn.execute(
                        "UPDATE user_inventory SET quantity = quantity + 1 WHERE user_id = ? AND card_id = ? AND guild_id = ?",
                        (self.user1.id, self.user2_item_id, self.guild_id))
                else:
                    await conn.execute(
                        "INSERT INTO user_inventory (guild_id, user_id, card_id, quantity) VALUES (?, ?, ?, 1)",
                        (self.guild_id, self.user1.id, self.user2_item_id))

                await conn.execute(
                    "DELETE FROM user_inventory WHERE quantity <= 0")

                await conn.execute(
                    "INSERT INTO trade_history (guild_id, user1_id, user2_id, user1_item, user2_item) VALUES (?, ?, ?, ?, ?)",
                    (self.guild_id, self.user1.id, self.user2.id, self.user1_item_name, self.user2_item_name))

                await conn.commit()

                await interaction.response.send_message("Trade accepted successfully.", ephemeral=True)
                await self.user1.send(f"`{self.user2.display_name}` accepted your trade offer.")

                # Edit the original trade request message to reflect the success
                embed = self.message.embeds[0]  # Get the original embed
                embed.title = "Trade Successful"
                embed.color = discord.Color.green()
                embed.description = (f"`{self.user2.display_name}` accepted the trade offer from "
                                     f"{self.user1.display_name}.\n\n"
                                     f"**{self.user1.display_name}** traded `{self.user1_item_name}`.\n"
                                     f"**{self.user2.display_name}** traded `{self.user2_item_name}`.")
                await self.message.edit(embed=embed, view=None)  # Remove the view after trade is accepted

                self.stop()

            except Exception as e:
                logger.error(f"Error during trade: {e}")
                await conn.execute('ROLLBACK')
                await interaction.response.send_message("An error occurred during the trade.", ephemeral=True)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("You have denied the trade.", ephemeral=True)
        await self.user1.send(f"`{self.user2.display_name}` denied your trade offer.")


        embed = self.message.embeds[0]
        embed.title = "Trade Denied"
        embed.color = discord.Color.red()
        embed.description = (f"`{self.user2.display_name}` denied the trade offer from "
                             f"`{self.user1.display_name}`.")
        await self.message.edit(embed=embed, view=None)

        self.stop()

    async def on_timeout(self):
        await self.user1.send(f"Your trade offer to `{self.user2.display_name}` has expired.")
        embed = self.message.embeds[0]
        embed.title = "Trade Expired"
        embed.color = discord.Color.orange()
        embed.description = "This trade offer has expired due to inactivity."
        await self.message.edit(embed=embed, view=None)
        self.stop()

# ---------------------------------------------------------------------------------------------------------------------
# Shopping Class
# ---------------------------------------------------------------------------------------------------------------------
class ShoppingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def buy_autocomplete(self, interaction: discord.Interaction, current: str):
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute(
                """
                SELECT sl.guild_id, sl.user_id, sl.card_id, c.name, c.rarity, sl.value
                FROM sale_listings sl
                JOIN cards c ON sl.card_id = c.card_id AND sl.guild_id = c.guild_id
                WHERE sl.user_id != ?
                  AND c.name LIKE ?
                  AND sl.guild_id = ?
                LIMIT 25
                """,
                (interaction.user.id, f"%{current}%", interaction.guild_id)
            )
            cards = await cursor.fetchall()

        if not cards:
            return [discord.app_commands.Choice(name="No listings available", value="0")]

        return [
            discord.app_commands.Choice(
                name=f"{card[3]} ({card[4].capitalize()}) - {card[5]} pts",
                value=f"{card[0]}|{card[1]}|{card[2]}"
            )
            for card in cards
        ]

    async def remove_sale_autocomplete(self, interaction: discord.Interaction, current: str):
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute(
                """
                SELECT sl.guild_id, sl.user_id, sl.card_id, c.name, c.rarity, sl.value
                FROM sale_listings sl
                JOIN cards c ON sl.card_id = c.card_id AND sl.guild_id = c.guild_id
                WHERE sl.user_id = ?
                  AND c.name LIKE ?
                  AND sl.guild_id = ?
                LIMIT 25
                """,
                (interaction.user.id, f"%{current}%", interaction.guild_id)
            )
            cards = await cursor.fetchall()

        if not cards:
            return [discord.app_commands.Choice(name="No sales to remove", value="0")]

        return [
            discord.app_commands.Choice(
                name=f"{card[3]} ({card[4].capitalize()}) - {card[5]} pts",
                value=f"{card[0]}|{card[1]}|{card[2]}"  # guild_id|user_id|card_id
            )
            for card in cards
        ]

    async def card_autocomplete(self, interaction: discord.Interaction, current: str):
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute(
                """
                SELECT DISTINCT c.card_id, c.name, c.rarity
                FROM cards AS c
                JOIN user_inventory AS ui ON c.card_id = ui.card_id AND c.guild_id = ui.guild_id
                WHERE ui.user_id = ?
                  AND ui.guild_id = ?
                  AND c.guild_id = ?
                  AND c.name LIKE ?
                ORDER BY c.name
                LIMIT 25
                """,
                (interaction.user.id, interaction.guild.id, interaction.guild.id, f"%{current}%")
            )
            cards = await cursor.fetchall()

        if not cards:
            return [discord.app_commands.Choice(name="No cards available", value="0")]

        return [
            discord.app_commands.Choice(
                name=f"{card[1]} ({card[2].capitalize()})",
                value=f"{card[0]}|{card[1]} ({card[2].capitalize()})"
            )
            for card in cards
        ]

    async def other_player_card_autocomplete(self, interaction: discord.Interaction, current: str):
        other_player = interaction.namespace.other_player
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute(
                """
                SELECT DISTINCT c.card_id, c.name, c.rarity
                FROM cards AS c
                JOIN user_inventory AS ui ON c.card_id = ui.card_id AND c.guild_id = ui.guild_id
                WHERE ui.user_id = ?
                  AND ui.guild_id = ?
                  AND c.guild_id = ?
                  AND c.name LIKE ?
                ORDER BY c.name
                LIMIT 25
                """,
                (other_player.id, interaction.guild.id, interaction.guild.id, f"%{current}%")
            )
            cards = await cursor.fetchall()

        if not cards:
            return [discord.app_commands.Choice(name="No cards available", value="0")]

        return [
            discord.app_commands.Choice(
                name=f"{card[1]} ({card[2].capitalize()})",
                value=f"{card[0]}|{card[1]} ({card[2].capitalize()})"
            )
            for card in cards
        ]


    # ---------------------------------------------------------------------------------------------------------------------
    # Shopping Commands
    # ---------------------------------------------------------------------------------------------------------------------
    @app_commands.command(description="User: Buy a card from available listings")
    @app_commands.describe(card="The card you want to buy")
    @app_commands.autocomplete(card=buy_autocomplete)
    async def buy(self, interaction: discord.Interaction, card: str):
        try:
            guild_id, seller_id, card_id = card.split('|')

            async with aiosqlite.connect(db_path) as conn:
                cursor = await conn.execute(
                    """
                    SELECT value
                    FROM sale_listings
                    WHERE guild_id = ? AND user_id = ? AND card_id = ?
                    """,
                    (guild_id, seller_id, card_id)
                )
                sale_info = await cursor.fetchone()

                if not sale_info:
                    await interaction.response.send_message("This card is no longer available.", ephemeral=True)
                    return

                price = sale_info[0]
                if int(seller_id) == interaction.user.id:
                    await interaction.response.send_message("You cannot buy your own listing.", ephemeral=True)
                    return

                cursor = await conn.execute(
                    """
                    SELECT balance
                    FROM economy
                    WHERE user_id = ? AND guild_id = ?
                    """,
                    (interaction.user.id, interaction.guild_id)
                )
                buyer_info = await cursor.fetchone()

                if buyer_info and buyer_info[0] >= price:
                    await conn.execute(
                        "UPDATE economy SET balance = balance - ? WHERE user_id = ? AND guild_id = ?",
                        (price, interaction.user.id, interaction.guild_id)
                    )
                    await conn.execute(
                        "UPDATE economy SET balance = balance + ? WHERE user_id = ? AND guild_id = ?",
                        (price, seller_id, interaction.guild_id)
                    )

                    cursor = await conn.execute(
                        """
                        SELECT quantity
                        FROM user_inventory
                        WHERE user_id = ? AND card_id = ? AND guild_id = ?
                        """,
                        (interaction.user.id, card_id, interaction.guild_id)
                    )
                    inventory_info = await cursor.fetchone()

                    if inventory_info:
                        await conn.execute(
                            """
                            UPDATE user_inventory
                            SET quantity = quantity + 1
                            WHERE user_id = ? AND card_id = ? AND guild_id = ?
                            """,
                            (interaction.user.id, card_id, interaction.guild_id)
                        )
                    else:
                        await conn.execute(
                            """
                            INSERT INTO user_inventory (guild_id, user_id, card_id, quantity)
                            VALUES (?, ?, ?, 1)
                            """,
                            (interaction.guild_id, interaction.user.id, card_id)
                        )

                    await conn.execute(
                        """
                        DELETE FROM sale_listings
                        WHERE guild_id = ? AND user_id = ? AND card_id = ?
                        """,
                        (guild_id, seller_id, card_id)
                    )
                    await conn.commit()

                    await interaction.response.send_message(
                        f"You have successfully purchased the card for {price} points.",
                        ephemeral=True
                    )
                else:
                    await interaction.response.send_message(
                        "You do not have enough points to buy this card.",
                        ephemeral=True
                    )
        except Exception as e:
            logger.error(f"Error handling buy command: {e}")
            await interaction.response.send_message("An error occurred while processing your request.", ephemeral=True)
    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="User: Sell a card from your inventory")
    @app_commands.describe(card="The card you want to sell", price="Price in points for the card")
    @app_commands.autocomplete(card=card_autocomplete)
    async def sell(self, interaction: discord.Interaction, card: str, price: int):
        try:
            async with aiosqlite.connect(db_path) as conn:
                # Log the input data
                logging.info(
                    f"User ID: {interaction.user.id}, Guild ID: {interaction.guild_id}, Card: {card}, Price: {price}")

                # Split the card input to get the card_id
                card_id, card_name = card.split('|')
                logging.info(f"Card ID: {card_id}, Card Name: {card_name}")

                # Fetch the card from the user's inventory
                cursor = await conn.execute(
                    "SELECT quantity FROM user_inventory WHERE user_id = ? AND card_id = ? AND guild_id = ?",
                    (interaction.user.id, card_id, interaction.guild_id))
                result = await cursor.fetchone()

                logging.info(f"Result from DB: {result}")

                if result and result[0] > 0:
                    new_quantity = result[0] - 1
                    await conn.execute(
                        "INSERT INTO sale_listings (guild_id, user_id, card_id, value) VALUES (?, ?, ?, ?)",
                        (interaction.guild_id, interaction.user.id, card_id, price))

                    if new_quantity > 0:
                        await conn.execute(
                            "UPDATE user_inventory SET quantity = ? WHERE user_id = ? AND card_id = ? AND guild_id = ?",
                            (new_quantity, interaction.user.id, card_id, interaction.guild_id))
                    else:
                        await conn.execute(
                            "DELETE FROM user_inventory WHERE user_id = ? AND card_id = ? AND guild_id = ?",
                            (interaction.user.id, card_id, interaction.guild_id))

                    await conn.commit()
                    await interaction.response.send_message(f"Card `{card_name}` listed for sale at `{price}` points.",
                                                            ephemeral=True)
                else:
                    await interaction.response.send_message("You do not own this card or have insufficient quantity.",
                                                            ephemeral=True)

        except Exception as e:
            logger.error(f"Error handling sell command: {e}")
            await interaction.response.send_message("An error occurred while processing your request.", ephemeral=True)

        finally:
            await log_command_usage(self.bot, interaction)

    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="User: Remove a card from your sale listings")
    @app_commands.describe(card="The card you want to remove from sale")
    @app_commands.autocomplete(card=remove_sale_autocomplete)
    async def remove_sale(self, interaction: discord.Interaction, card: str):
        try:
            # Parse the composite key
            guild_id, user_id, card_id = card.split('|')

            async with aiosqlite.connect(db_path) as conn:
                # Ensure the sale listing exists
                cursor = await conn.execute(
                    """
                    SELECT card_id
                    FROM sale_listings
                    WHERE guild_id = ? AND user_id = ? AND card_id = ?
                    """,
                    (guild_id, user_id, card_id)
                )
                result = await cursor.fetchone()

                if result:
                    # Remove the sale listing
                    await conn.execute(
                        """
                        DELETE FROM sale_listings
                        WHERE guild_id = ? AND user_id = ? AND card_id = ?
                        """,
                        (guild_id, user_id, card_id)
                    )

                    # Update the user's inventory
                    update_cursor = await conn.execute(
                        """
                        UPDATE user_inventory
                        SET quantity = quantity + 1
                        WHERE user_id = ? AND card_id = ? AND guild_id = ?
                        """,
                        (interaction.user.id, card_id, guild_id)
                    )

                    # If no row was updated, insert the item into the inventory
                    if update_cursor.rowcount == 0:
                        await conn.execute(
                            """
                            INSERT INTO user_inventory (guild_id, user_id, card_id, quantity)
                            VALUES (?, ?, ?, 1)
                            """,
                            (guild_id, interaction.user.id, card_id)
                        )

                    await conn.commit()
                    await interaction.response.send_message(
                        "The sale listing has been removed and the card has been returned to your inventory.",
                        ephemeral=True
                    )
                else:
                    await interaction.response.send_message(
                        "No such sale listing found for your account.",
                        ephemeral=True
                    )

        except Exception as e:
            logger.error(f"Error handling remove_sale command: {e}")
            await interaction.response.send_message(
                "An error occurred while processing your request.",
                ephemeral=True
            )
        finally:
            await log_command_usage(self.bot, interaction)

    # ---------------------------------------------------------------------------------------------------------------------

    @app_commands.command(description="User: Trade items with another player")
    @app_commands.describe(other_player="The player you want to trade with", your_item="The item you're offering",
                           their_item="The item you want")
    @app_commands.autocomplete(your_item=card_autocomplete, their_item=other_player_card_autocomplete)
    async def trade(self, interaction: discord.Interaction, other_player: discord.Member, your_item: str,
                    their_item: str):
        await interaction.response.defer(ephemeral=True)
        try:
            if other_player.id == interaction.user.id:
                await interaction.followup.send("You cannot trade with yourself.", ephemeral=True)
                return

            your_item_id, your_item_name = your_item.split('|')
            their_item_id, their_item_name = their_item.split('|')

            async with aiosqlite.connect(db_path) as conn:
                # Check if the user owns the item they want to trade
                cursor = await conn.execute(
                    "SELECT quantity FROM user_inventory WHERE user_id = ? AND card_id = ? AND guild_id = ?",
                    (interaction.user.id, your_item_id, interaction.guild_id))
                user_item = await cursor.fetchone()

                if not user_item or user_item[0] < 1:
                    await interaction.followup.send("You do not own the item you're trying to trade.", ephemeral=True)
                    return

                # Check if the other player owns the item they are supposed to trade
                cursor = await conn.execute(
                    "SELECT quantity FROM user_inventory WHERE user_id = ? AND card_id = ? AND guild_id = ?",
                    (other_player.id, their_item_id, interaction.guild_id))
                other_item = await cursor.fetchone()

                if not other_item or other_item[0] < 1:
                    await interaction.followup.send(f"{other_player.display_name} does not own the item you want.",
                                                    ephemeral=True)
                    return

                # Send trade request to the other player
                guild_name = interaction.guild.name
                trade_request_embed = discord.Embed(
                    title="Trade Request",
                    description=f"{interaction.user.display_name} from **{guild_name}** wants to trade their "
                                f"`{your_item_name}` for your `{their_item_name}`.",
                    color=await get_embed_colour(interaction.guild.id)
                )

                # Create the view and send it to the other player
                view = TradeRequestView(self.bot, interaction.user, your_item_id, your_item_name, other_player,
                                        their_item_id, their_item_name, interaction.guild.id)
                message = await other_player.send(embed=trade_request_embed, view=view)

                # Save the message object to the view for later use in the timeout handler
                view.message = message

                await interaction.followup.send(f"Trade request sent to {other_player.display_name}.", ephemeral=True)

        except Exception as e:
            logger.error(f"Error handling trade command: {e}")
            await interaction.followup.send("An error occurred while processing your trade request.", ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)

    @staticmethod
    def format_timestamp(timestamp_str):
        timestamp = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
        return timestamp.strftime("%d/%m/%Y at %H:%M")

    @app_commands.command(description="User: View your trade history")
    async def trade_history(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            async with aiosqlite.connect(db_path) as conn:
                cursor = await conn.execute(
                    "SELECT user1_id, user2_id, user1_item, user2_item, timestamp FROM trade_history WHERE (user1_id = ? OR user2_id = ?) AND guild_id = ? ORDER BY timestamp DESC LIMIT 10",
                    (interaction.user.id, interaction.user.id, interaction.guild_id))
                trades = await cursor.fetchall()

                if not trades:
                    await interaction.followup.send("You have no trade history.", ephemeral=True)
                    return

                trade_list = []
                for trade in trades:
                    user1_id, user2_id, user1_item, user2_item, timestamp_str = trade
                    formatted_timestamp = self.format_timestamp(timestamp_str)

                    if interaction.user.id == user1_id:
                        trade_list.append(
                            f"**User:** <@{user2_id}>\n"
                            f"**You:** *{user1_item}*\n"
                            f"**Them:** *{user2_item}*\n"
                            f"**When:** *{formatted_timestamp}*\n"
                        )
                    else:
                        trade_list.append(
                            f"**User:** <@{user1_id}>\n"
                            f"**You:** *{user2_item}*\n"
                            f"**Them:** *{user1_item}*\n"
                            f"**When:** *{formatted_timestamp}*\n"
                        )

                trade_history_embed = discord.Embed(
                    title="Trade History",
                    description="\n\n".join(trade_list),
                    color=await get_embed_colour(interaction.guild_id)
                )
                trade_history_embed.set_thumbnail(url=self.bot.user.display_avatar.url)
                trade_history_embed.set_footer(text=f"Trade History for {interaction.user.name}")
                trade_history_embed.timestamp = discord.utils.utcnow()

                await interaction.followup.send(embed=trade_history_embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error fetching trade history: {e}")
            await interaction.followup.send("An error occurred while fetching your trade history.", ephemeral=True)
        finally:
            await log_command_usage(self.bot, interaction)


# ---------------------------------------------------------------------------------------------------------------------
# Setup Function
# ---------------------------------------------------------------------------------------------------------------------
async def setup(bot):
    async with aiosqlite.connect(db_path) as conn:
        # Check if the `sale_listings` table exists
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sale_listings';"
        )
        table_exists = await cursor.fetchone()

        # If the table exists, check for `listing_id` and migrate if necessary
        if table_exists:
            cursor = await conn.execute("PRAGMA table_info(sale_listings);")
            columns = await cursor.fetchall()

            # Check if `listing_id` exists in the table schema
            if any(column[1] == "listing_id" for column in columns):
                # Migrate the table to remove `listing_id`
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS sale_listings_new (
                        guild_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        card_id TEXT NOT NULL,
                        value INTEGER NOT NULL,
                        FOREIGN KEY (guild_id, user_id, card_id) REFERENCES user_inventory(guild_id, user_id, card_id)
                    );
                """)
                await conn.execute("""
                    INSERT INTO sale_listings_new (guild_id, user_id, card_id, value)
                    SELECT guild_id, user_id, card_id, value FROM sale_listings;
                """)
                await conn.execute("DROP TABLE sale_listings;")
                await conn.execute("ALTER TABLE sale_listings_new RENAME TO sale_listings;")
                await conn.commit()
                print("Migrated sale_listings table: removed listing_id column.")

        # Create the `sale_listings` table if it doesn't exist
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sale_listings (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                card_id TEXT NOT NULL,
                value INTEGER NOT NULL,
                FOREIGN KEY (guild_id, user_id, card_id) REFERENCES user_inventory(guild_id, user_id, card_id)
            );
        """)

        # Create the `trade_history` table if it doesn't exist
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user1_id INTEGER NOT NULL,
                user2_id INTEGER NOT NULL,
                user1_item TEXT NOT NULL,
                user2_item TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)

        await conn.commit()
    await bot.add_cog(ShoppingCog(bot))

