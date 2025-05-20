import discord
import logging
import random
import aiosqlite
import os

from discord.ext import commands
from discord import app_commands
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
# LotteryCog Class
# ---------------------------------------------------------------------------------------------------------------------
class LotteryCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.house_user_id = 111941993629806592

    async def lottery_event_names_autocomplete(self, interaction: discord.Interaction, current: str):
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute(
                "SELECT id, name, ticket_price FROM lottery_events WHERE guild_id = ? AND name LIKE ? AND active = 1",
                (interaction.guild.id, f'%{current}%')
            )
            event_names = await cursor.fetchall()
            return [app_commands.Choice(name=f"{event[1]} (ID: {event[0]}) - {event[2]} points", value=str(event[0])) for event in event_names]

    async def card_prize_autocomplete(self, interaction: discord.Interaction, current: str):
        event_id = interaction.namespace.event_name
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute(
                "SELECT prize_type FROM lottery_events WHERE id = ? AND guild_id = ?",
                (event_id, interaction.guild.id)
            )
            prize_type = await cursor.fetchone()
            if prize_type and prize_type[0] == 'points':
                return [app_commands.Choice(name="The prize selected is: points", value="points")]
            else:
                cursor = await conn.execute(
                    "SELECT name FROM cards WHERE name LIKE ? AND guild_id = ? LIMIT 25",
                    (f'%{current}%', interaction.guild.id)
                )
                card_names = await cursor.fetchall()
                return [app_commands.Choice(name=card[0], value=card[0]) for card in card_names]

    async def prize_type_autocomplete(self, interaction: discord.Interaction, current: str):
        types = ["points", "card"]
        return [
            app_commands.Choice(name=type_, value=type_)
            for type_ in types if current.lower() in type_.lower()
        ]

    async def ticket_number_autocomplete(self, interaction: discord.Interaction, current: str):
        event_id = interaction.namespace.event_name
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute(
                "SELECT ticket_number FROM lottery_tickets WHERE event_id = ?",
                (event_id,)
            )
            taken_numbers = [str(row[0]) for row in await cursor.fetchall()]
            available_numbers = [str(num) for num in range(1, 5001) if
                                 str(num) not in taken_numbers and str(num).startswith(current)]
            return [app_commands.Choice(name=num, value=num) for num in available_numbers[:25]]

    # ---------------------------------------------------------------------------------------------------------------------
    # Admin Commands
    # ---------------------------------------------------------------------------------------------------------------------
    @app_commands.command(description="Admin: Create a lottery event")
    @app_commands.describe(name="Name of the lottery event", prize_type="Type of prize (points or card)",
                           card_prize="Optional card prize (if applicable)",
                           ticket_price="Price of a lottery ticket")
    @app_commands.autocomplete(prize_type=prize_type_autocomplete, card_prize=card_prize_autocomplete)
    async def lottery_create(self, interaction: discord.Interaction, name: str, prize_type: str, ticket_price: int,
                             card_prize: str = None):
        if not await check_permissions(interaction):
            await interaction.response.send_message(
                "You do not have permission to use this command. An Admin needs to `/authorise` you!", ephemeral=True)
            return

        message_to_send = None

        async with aiosqlite.connect(db_path) as conn:
            if prize_type == "card":
                if not card_prize:
                    await interaction.response.send_message("You must select a card for the card prize.",
                                                            ephemeral=True)
                    return

                # Fetch card_id instead of using the card name directly
                cursor = await conn.execute(
                    "SELECT card_id FROM cards WHERE name = ? AND guild_id = ?", (card_prize, interaction.guild.id)
                )
                card = await cursor.fetchone()
                if not card:
                    await interaction.response.send_message("Card not found in the database. Please check the name.",
                                                            ephemeral=True)
                    return
                card_prize = card[0]  # Update card_prize to be the card_id

            elif prize_type == "points":
                if card_prize:
                    message_to_send = f"Lottery event created with a points prize. The card prize `{card_prize}` was not set because the points prize was chosen."
                card_prize = None  # Ensure card_prize is nullified

            else:
                await interaction.response.send_message("Invalid prize type. Please select either 'points' or 'card'.",
                                                        ephemeral=True)
                return

            # Generate a random number for the lottery event
            lottery_number = random.randint(1, 5000)

            await conn.execute('''
                INSERT INTO lottery_events (guild_id, name, prize_type, card_prize, ticket_price, active, lottery_number)
                VALUES (?, ?, ?, ?, ?, 1, ?)
            ''', (interaction.guild.id, name, prize_type, card_prize, ticket_price, lottery_number))
            await conn.commit()

        if message_to_send:
            await interaction.response.send_message(f"Lottery event `{name}` created successfully!\n"
                                                    f"{message_to_send}",
                                                    ephemeral=True)
        else:
            await interaction.response.send_message(f"Lottery event `{name}` created successfully!", ephemeral=True)

        await log_command_usage(self.bot, interaction)

    @app_commands.command(description="Admin: End a lottery event")
    @app_commands.describe(event_name="The ID of the lottery event")
    @app_commands.autocomplete(event_name=lottery_event_names_autocomplete)
    async def lottery_end(self, interaction: discord.Interaction, event_name: str):
        if not await check_permissions(interaction):
            await interaction.response.send_message(
                "You do not have permission to use this command. An Admin needs to `/authorise` you!",
                ephemeral=True)
            return

        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute('''
                SELECT id, name FROM lottery_events WHERE id = ? AND guild_id = ? AND active = 1
            ''', (event_name, interaction.guild.id))
            event = await cursor.fetchone()

            if not event:
                await interaction.response.send_message(
                    "Lottery event not found or already ended.",
                    ephemeral=True)
                return

            event_id, event_name = event

            # Remove the lottery event and its tickets from the database
            await conn.execute('DELETE FROM lottery_tickets WHERE event_id = ?', (event_id,))
            await conn.execute('DELETE FROM lottery_events WHERE id = ?', (event_id,))
            await conn.commit()

        await interaction.response.send_message(
            f"The lottery event `{event_name}` has been ended and removed from the database.",
            ephemeral=True)
        await log_command_usage(self.bot, interaction)

    # ---------------------------------------------------------------------------------------------------------------------
    @app_commands.command(description="Admin: Get information about a lottery event")
    @app_commands.describe(event_name="The ID of the lottery event")
    @app_commands.autocomplete(event_name=lottery_event_names_autocomplete)
    async def lottery_info(self, interaction: discord.Interaction, event_name: str):
        colour = await get_embed_colour(interaction.guild.id)
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute('''
                SELECT name, prize_type, card_prize, ticket_price, lottery_number
                FROM lottery_events WHERE id = ? AND guild_id = ? AND active = 1
            ''', (event_name, interaction.guild.id))
            event = await cursor.fetchone()

            if not event:
                await interaction.response.send_message("Lottery event not found or not active.", ephemeral=True)
                return

            name, prize_type, card_prize, ticket_price, lottery_number = event

            cursor = await conn.execute('''
                SELECT COUNT(*) FROM lottery_tickets WHERE event_id = ?
            ''', (event_name,))
            entry_count_row = await cursor.fetchone()
            entry_count = entry_count_row[0]

            embed = discord.Embed(
                title=f"🎟️ {name}",
                color=colour
            )
            embed.add_field(name="Prize Type", value=prize_type.capitalize(), inline=False)

            if prize_type == "card":
                cursor = await conn.execute('''
                    SELECT img_url, name FROM cards WHERE card_id = ? AND guild_id = ?
                ''', (card_prize, interaction.guild.id))
                card = await cursor.fetchone()
                if card:
                    card_img_url, card_name = card
                    embed.add_field(name="Current Prize", value=f"Card: {card_name}", inline=False)
                    embed.set_image(url=card_img_url)
            elif prize_type == "points":
                current_prize = int(ticket_price * entry_count * 0.95)  # 95% of total ticket sales
                embed.add_field(name="Current Prize", value=f"{current_prize} points", inline=False)

            embed.add_field(name="Ticket Price", value=f"{ticket_price} points", inline=False)
            embed.add_field(name="Number of Entries", value=str(entry_count), inline=False)
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)

            embed.set_footer(text=f"{name} Information")
            embed.timestamp = discord.utils.utcnow()

            await interaction.response.send_message(embed=embed)

        await log_command_usage(self.bot, interaction)

    # ---------------------------------------------------------------------------------------------------------------------
    # User Commands
    # ---------------------------------------------------------------------------------------------------------------------
    @app_commands.command(description="User: Buy a ticket for an active lottery event")
    @app_commands.describe(event_name="The ID of the lottery event",
                           ticket_number="Number between 1 and 5,000 for your ticket")
    @app_commands.autocomplete(event_name=lottery_event_names_autocomplete, ticket_number=ticket_number_autocomplete)
    async def lottery_enter(self, interaction: discord.Interaction, event_name: str, ticket_number: int):
        if ticket_number < 1 or ticket_number > 5000:
            await interaction.response.send_message("Ticket number must be between 1 and 10,000.", ephemeral=True)
            return

        async with aiosqlite.connect(db_path) as conn:
            # Fetch the event details, including the name
            cursor = await conn.execute('''
                SELECT id, name, ticket_price, lottery_number, prize_type, card_prize 
                FROM lottery_events WHERE id = ? AND guild_id = ? AND active = 1
            ''', (event_name, interaction.guild.id))
            event = await cursor.fetchone()

            if not event:
                await interaction.response.send_message("Lottery event not found or not active.", ephemeral=True)
                return

            event_id, event_name, ticket_price, lottery_number, prize_type, card_prize = event

            cursor = await conn.execute('''
                SELECT balance FROM economy WHERE user_id = ? AND guild_id = ?
            ''', (interaction.user.id, interaction.guild.id))
            economy = await cursor.fetchone()

            if not economy or economy[0] < ticket_price:
                await interaction.response.send_message("You do not have enough points to buy a ticket.",
                                                        ephemeral=True)
                return

            # Check if the ticket number has already been taken in this specific event (using event_id)
            cursor = await conn.execute('''
                SELECT 1 FROM lottery_tickets WHERE event_id = ? AND ticket_number = ?
            ''', (event_id, ticket_number))
            existing_ticket = await cursor.fetchone()

            if existing_ticket:
                await interaction.response.send_message(
                    "This ticket number is already taken for this event. Please choose another number.", ephemeral=True)
                return

            # Deduct the ticket price from the user's balance
            await conn.execute('''
                UPDATE economy SET balance = balance - ? WHERE user_id = ? AND guild_id = ?
            ''', (ticket_price, interaction.user.id, interaction.guild.id))

            # Add ticket entry
            await conn.execute('''
                INSERT INTO lottery_tickets (event_id, user_id, ticket_number)
                VALUES (?, ?, ?)
            ''', (event_id, interaction.user.id, ticket_number))

            await conn.commit()

            # Check if the purchased ticket is the winning number
            if ticket_number == lottery_number:
                # Award the prize immediately
                if prize_type == "points":
                    cursor = await conn.execute('''
                        SELECT COUNT(*) FROM lottery_tickets WHERE event_id = ?
                    ''', (event_id,))
                    ticket_count_row = await cursor.fetchone()
                    ticket_count = ticket_count_row[0]

                    total_points = ticket_count * ticket_price
                    winner_prize = int(total_points * 0.95)
                    house_cut = total_points - winner_prize
                    await conn.execute(
                        "UPDATE economy SET balance = balance + ? WHERE user_id = ? AND guild_id = ?",
                        (winner_prize, interaction.user.id, interaction.guild.id))
                    await conn.execute(
                        "UPDATE economy SET balance = balance + ? WHERE user_id = ? AND guild_id = ?",
                        (house_cut, self.house_user_id, interaction.guild.id))
                elif prize_type == "card" and card_prize:
                    cursor = await conn.execute(
                        "SELECT card_id, img_url FROM cards WHERE card_id = ? AND guild_id = ?", (card_prize, interaction.guild.id))
                    card = await cursor.fetchone()
                    if card:
                        card_id, img_url = card[0], card[1]
                        await conn.execute(
                            "INSERT INTO user_inventory (guild_id, user_id, card_id, quantity) VALUES (?, ?, ?, 1) "
                            "ON CONFLICT(guild_id, user_id, card_id) DO UPDATE SET quantity = quantity + 1",
                            (interaction.guild.id, interaction.user.id, card_id))

                # End the lottery event
                await conn.execute('UPDATE lottery_events SET active = 0 WHERE id = ?', (event_id,))
                await conn.commit()

                colour = await get_embed_colour(interaction.guild.id)
                embed = discord.Embed(
                    title="🎉 Congratulations! 🎉",
                    description=f"You've won the `{event_name}` lottery!",
                    color=colour
                )
                embed.add_field(name="Winning Ticket Number", value=f"{ticket_number}", inline=False)

                if prize_type == "points":
                    embed.add_field(name="Prize", value=f"{winner_prize} points", inline=False)
                elif prize_type == "card":
                    embed.add_field(name="Prize", value=f"Card: {card_prize}", inline=False)
                    embed.set_thumbnail(url=img_url)

                embed.add_field(name="Thank you for participating!", value="\u200b", inline=False)
                embed.set_thumbnail(url=interaction.user.display_avatar.url)
                embed.timestamp = discord.utils.utcnow()

                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(
                    f"You have successfully purchased ticket number `{ticket_number}` for "
                    f"`{event_name}`. Unfortunately, this is not the winning number.",
                    ephemeral=True)

        await log_command_usage(self.bot, interaction)

# ---------------------------------------------------------------------------------------------------------------------
# Setup Function
# ---------------------------------------------------------------------------------------------------------------------
async def setup(bot):
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS lottery_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                prize_type TEXT NOT NULL,
                card_prize TEXT,
                ticket_price INTEGER NOT NULL,
                active INTEGER DEFAULT 1,
                lottery_number INTEGER NOT NULL
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS lottery_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                ticket_number INTEGER NOT NULL,
                FOREIGN KEY (event_id) REFERENCES lottery_events(id),
                UNIQUE(event_id, ticket_number)             
            )
        ''')

        await conn.commit()
    await bot.add_cog(LotteryCog(bot))

