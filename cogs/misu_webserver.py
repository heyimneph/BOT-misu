import os
import io
import json
import random
import logging
import sqlite3
import requests
import socket
from threading import Thread
from datetime import datetime
from functools import lru_cache
from discord.ext import commands
from flask import Flask, request, render_template, url_for, redirect, session, jsonify, Response

from waitress import serve
from werkzeug.middleware.proxy_fix import ProxyFix
from concurrent.futures import ThreadPoolExecutor
from config import DISCORD_TOKEN, DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, DISCORD_REDIRECT_URL

RUN_IN_IDE = False

db_path = './data/databases/tcg.db'
PRESET_DIR = './data/presets'

# ---------------------------------------------------------------------------------------------------------------------
# Setup Webserver Directories
# ---------------------------------------------------------------------------------------------------------------------

if RUN_IN_IDE:
    template_dir = '../webserver/templates'
    static_dir = '../webserver/static'
else:
    template_dir = '/app/webserver/templates'
    static_dir = '/app/webserver/static'

# ---------------------------------------------------------------------------------------------------------------------
# Initialise Flask
# ---------------------------------------------------------------------------------------------------------------------

app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)
app.secret_key = 'asyubgifdhFDFDUFKCV5742FHSF8'

# Discord OAuth2 Configuration
DISCORD_AUTH_URL = f'https://discord.com/api/oauth2/authorize?client_id={DISCORD_CLIENT_ID}&redirect_uri={DISCORD_REDIRECT_URL}&response_type=code&scope=identify%20guilds'

# ---------------------------------------------------------------------------------------------------------------------
# Setup Logging
# ---------------------------------------------------------------------------------------------------------------------

logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    handlers=[logging.StreamHandler(), logging.FileHandler('app.log', 'a', 'utf-8')])

logger = logging.getLogger(__name__)

HEADERS = {
    "Authorization": f"Bot {DISCORD_TOKEN}",
    "Content-Type": "application/json"
}

# ---------------------------------------------------------------------------------------------------------------------
# Caching Functions
# ---------------------------------------------------------------------------------------------------------------------

@lru_cache(maxsize=100)  # Cache up to 100 results
def fetch_user_info(access_token):
    """Fetch user information from Discord API with caching."""
    headers = {
        'Authorization': f'Bearer {access_token}'
    }
    response = requests.get('https://discord.com/api/users/@me', headers=headers)
    if response.status_code == 200:
        return response.json()
    return None

@lru_cache(maxsize=100)  # Cache up to 100 results
def fetch_user_guilds(access_token):
    """Fetch user's guilds from Discord API with caching."""
    headers = {
        'Authorization': f'Bearer {access_token}'
    }
    response = requests.get('https://discord.com/api/users/@me/guilds', headers=headers)
    if response.status_code == 200:
        return response.json()
    return None

@lru_cache(maxsize=100)  # Cache up to 100 results
def fetch_guild_info(guild_id):
    """Fetch guild information from Discord API with caching."""
    response = requests.get(f'https://discord.com/api/guilds/{guild_id}', headers=HEADERS)
    if response.status_code == 200:
        return response.json()
    return None

# ---------------------------------------------------------------------------------------------------------------------
# Flask Routes
# ---------------------------------------------------------------------------------------------------------------------

@app.route('/')
def home():
    return render_template("index.html")

@app.route('/login')
def login():
    return redirect(DISCORD_AUTH_URL)

@app.route('/logout')
def logout():
    session.clear()
    fetch_user_info.cache_clear()
    fetch_user_guilds.cache_clear()
    fetch_guild_info.cache_clear()
    return redirect(url_for('home'))

@app.route("/privacy-policy")
def privacy_policy():
    return render_template("privacy-policy.html")

@app.route('/tos')
def tos():
    return render_template("tos.html")


@app.route('/get_card_names')
def get_card_names():
    guild_id = request.args.get('guild_id')
    if not guild_id:
        return jsonify([])

    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute('''
            SELECT name FROM cards WHERE guild_id = ?
        ''', (guild_id,))
        card_names = [row[0] for row in cursor.fetchall()]

    return jsonify(card_names)

@app.route('/auth/callback')
def auth_callback():
    code = request.args.get('code')
    if not code:
        return "Error: No code provided", 400

    # Exchange the code for an access token
    data = {
        'client_id': DISCORD_CLIENT_ID,
        'client_secret': DISCORD_CLIENT_SECRET,
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': DISCORD_REDIRECT_URL,
        'scope': 'identify guilds'
    }
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    response = requests.post('https://discord.com/api/oauth2/token', data=data, headers=headers)
    if response.status_code != 200:
        logger.error(f"Failed to authenticate with Discord. Status code: {response.status_code}")
        return "Error: Failed to authenticate with Discord", 400

    # Store the access token in the session
    session['access_token'] = response.json()['access_token']
    session['expires_at'] = datetime.now().timestamp() + response.json()['expires_in']
    return redirect(url_for('dashboard'))

# ---------------------------------------------------------------------------------------------------------------------
# ---------------------------------------------------------------------------------------------------------------------

@app.route('/dashboard')
def dashboard():
    if 'access_token' not in session or 'expires_at' not in session or session['expires_at'] < datetime.now().timestamp():
        logger.warning("Access token expired or missing. Redirecting to login.")
        return redirect(url_for('login'))

    # Fetch user information using the cached function
    user = fetch_user_info(session['access_token'])
    if not user:
        logger.error("Failed to fetch user information.")
        return "Error: Failed to fetch user information", 400

    logger.debug(f"Fetched user information: {user}")

    # Fetch user's guilds using the cached function
    guilds = fetch_user_guilds(session['access_token'])
    if not guilds:
        logger.error("Failed to fetch guilds.")
        return "Error: Failed to fetch guilds", 400

    logger.debug(f"Fetched guilds: {guilds}")

    # Fetch guilds where Misu is present
    bot_guilds_response = requests.get('https://discord.com/api/users/@me/guilds', headers=HEADERS)
    if bot_guilds_response.status_code != 200:
        logger.error(f"Failed to fetch bot guilds. Status code: {bot_guilds_response.status_code}")
        return "Error: Failed to fetch bot guilds", 400

    bot_guilds = bot_guilds_response.json()
    bot_guild_ids = {guild['id'] for guild in bot_guilds}

    # Filter guilds where the user has admin permissions, manage server permissions, or is authorized
    authorized_guilds = []
    for guild in guilds:
        permissions = int(guild['permissions'])
        # Check if the user has admin or manage server permissions
        if (permissions & 0x8) == 0x8 or (permissions & 0x20) == 0x20:  # 0x8 = ADMINISTRATOR, 0x20 = MANAGE_GUILD
            authorized_guilds.append(guild)
        else:
            # Check if the user is authorized for the bot in this guild
            with sqlite3.connect(db_path) as conn:
                cursor = conn.execute('''
                    SELECT 1 FROM permissions WHERE guild_id = ? AND user_id = ? AND can_use_commands = 1
                ''', (guild['id'], user['id']))
                if cursor.fetchone():
                    authorized_guilds.append(guild)

    # Categorize guilds
    guilds_with_misu = [guild for guild in authorized_guilds if guild['id'] in bot_guild_ids]
    guilds_without_misu = [guild for guild in authorized_guilds if guild['id'] not in bot_guild_ids]

    return render_template('dashboard.html', user=user, guilds_with_misu=guilds_with_misu, guilds_without_misu=guilds_without_misu)


@app.route('/settings/<guild_id>')
def settings(guild_id):
    active_tab = request.args.get('active_tab', 'customisation')

    if 'access_token' not in session or 'expires_at' not in session or session['expires_at'] < datetime.now().timestamp():
        logger.warning("Access token expired or missing. Redirecting to login.")
        return redirect(url_for('login'))

    # Fetch user information using the cached function
    user = fetch_user_info(session['access_token'])
    if not user:
        logger.error("Failed to fetch user information.")
        return "Error: Failed to fetch user information", 400

    logger.debug(f"Fetched user information: {user}")

    # Fetch guild information using the cached function
    guild = fetch_guild_info(guild_id)
    if not guild:
        logger.error("Failed to fetch guild information.")
        return "Error: Failed to fetch guild information", 400

    logger.debug(f"Fetched guild information: {guild}")

    # Fetch user's guilds using the cached function
    guilds = fetch_user_guilds(session['access_token'])
    if not guilds:
        logger.error("Failed to fetch guilds.")
        return "Error: Failed to fetch guilds", 400

    logger.debug(f"Fetched guilds: {guilds}")

    # Check if the user has permissions to manage this guild
    user_guild = next((g for g in guilds if g['id'] == guild_id), None)
    if not user_guild:
        logger.warning(f"User does not have access to guild {guild_id}")
        return "Error: You do not have access to this guild", 403

    permissions = int(user_guild['permissions'])
    if not ((permissions & 0x8) == 0x8 or (permissions & 0x20) == 0x20):
        logger.warning(f"User does not have permission to manage guild {guild_id}")
        return "Error: You do not have permission to manage this guild", 403

    with sqlite3.connect(db_path) as conn:
        # Fetch customisation settings
        cursor = conn.execute('SELECT type, value FROM customisation WHERE guild_id = ?', (guild_id,))
        settings = {row[0]: row[1] for row in cursor.fetchall()}

        # Fetch economy settings
        cursor = conn.execute('''
            SELECT voice_points_per_minute, message_count_threshold, message_reward_points
            FROM economy_config WHERE guild_id = ?
        ''', (guild_id,))
        economy_settings = cursor.fetchone()
        if economy_settings:
            settings['voice_points_per_minute'] = economy_settings[0]
            settings['message_count_threshold'] = economy_settings[1]
            settings['message_reward_points'] = economy_settings[2]

        # Fetch lottery settings
        cursor = conn.execute('SELECT ticket_price FROM lottery_events WHERE guild_id = ?', (guild_id,))
        lottery_settings = cursor.fetchone()
        if lottery_settings:
            settings['ticket_price'] = lottery_settings[0]

        # Fetch active lotteries for the dropdown
        cursor = conn.execute('''
            SELECT id, name, ticket_price FROM lottery_events WHERE guild_id = ? AND active = 1
        ''', (guild_id,))
        lotteries = [{'id': row[0], 'name': row[1], 'ticket_price': row[2]} for row in cursor.fetchall()]

        # Fetch burn settings (rarity and burn value only)
        cursor = conn.execute('SELECT rarity, burn_value FROM rarity_weights WHERE guild_id = ?', (guild_id,))
        burn_settings = {row[0]: row[1] for row in cursor.fetchall()}

        # Fetch full rarity settings (weight and burn value)
        cursor = conn.execute('SELECT rarity, weight, burn_value FROM rarity_weights WHERE guild_id = ?', (guild_id,))
        rarities = {}
        for row in cursor.fetchall():
            rarities[row[0]] = {"weight": row[1], "burn_value": row[2]}

        # Fetch events for the Events tab
        cursor = conn.execute('''
            SELECT event_name, point_reward, event_cooldown, set_reward
            FROM events
            WHERE guild_id = ?
        ''', (guild_id,))
        events = [
            {
                "event_name": row[0],
                "point_reward": row[1],
                "event_cooldown": row[2],
                "set_reward": row[3]
            }
            for row in cursor.fetchall()
        ]

        # Fetch available card sets for event rewards
        cursor = conn.execute('SELECT set_id, name, description FROM card_sets WHERE guild_id = ?', (guild_id,))
        sets = [{'set_id': row[0], 'name': row[1], 'description': row[2]} for row in cursor.fetchall()]

        # Fetch all cards
        cursor = conn.execute('SELECT card_id, name FROM cards WHERE guild_id = ?', (guild_id,))
        cards = [{'card_id': row[0], 'name': row[1]} for row in cursor.fetchall()]

    # Ensure the color code is properly formatted
    if 'embed_color' in settings and not settings['embed_color'].startswith('#'):
        settings['embed_color'] = f"#{settings['embed_color']}"

    return render_template('settings.html', user=user, guild=guild, settings=settings,
                           lotteries=lotteries, burn_settings=burn_settings, rarities=rarities,
                           events=events, sets=sets, cards=cards, active_tab=active_tab)


# ---------------------------------------------------------------------------------------------------------------------
# ---------------------------------------------------------------------------------------------------------------------

@app.route('/update_customisation_settings/<guild_id>', methods=['POST'])
def update_customisation_settings(guild_id):
    if 'access_token' not in session or 'expires_at' not in session or session['expires_at'] < datetime.now().timestamp():
        logger.warning("Access token expired or missing. Redirecting to login.")
        return redirect(url_for('login'))

    # Fetch user information
    headers = {'Authorization': f'Bearer {session["access_token"]}'}
    user_response = requests.get('https://discord.com/api/users/@me', headers=headers)
    if user_response.status_code != 200:
        logger.error(f"Failed to fetch user information. Status code: {user_response.status_code}")
        return "Error: Failed to fetch user information", 400

    user = user_response.json()
    logger.debug(f"Fetched user information: {user}")

    # Fetch guild information
    guild_response = requests.get(f'https://discord.com/api/guilds/{guild_id}', headers=HEADERS)
    if guild_response.status_code != 200:
        logger.error(f"Failed to fetch guild information. Status code: {guild_response.status_code}")
        return "Error: Failed to fetch guild information", 400

    guild = guild_response.json()
    logger.debug(f"Fetched guild information: {guild}")

    # Fetch user's guilds
    guilds_response = requests.get('https://discord.com/api/users/@me/guilds', headers=headers)
    if guilds_response.status_code != 200:
        logger.error(f"Failed to fetch guilds. Status code: {guilds_response.status_code}")
        return "Error: Failed to fetch guilds", 400

    guilds = guilds_response.json()
    logger.debug(f"Fetched guilds: {guilds}")

    # Check if the user has permissions to manage this guild
    user_guild = next((g for g in guilds if g['id'] == guild_id), None)
    if not user_guild:
        logger.warning(f"User does not have access to guild {guild_id}")
        return "Error: You do not have access to this guild", 403

    permissions = int(user_guild['permissions'])
    if not ((permissions & 0x8) == 0x8 or (permissions & 0x20) == 0x20):
        logger.warning(f"User does not have permission to manage guild {guild_id}")
        return "Error: You do not have permission to manage this guild", 403

    # Get the new embed color from the form
    embed_color = request.form.get('embed_color')
    if not embed_color:
        return "Error: No embed color provided", 400

    # Remove the '#' from the color if present
    if embed_color.startswith('#'):
        embed_color = embed_color[1:]

    # Update the embed color in the database
    with sqlite3.connect(db_path) as conn:
        conn.execute('''
            INSERT OR REPLACE INTO customisation (guild_id, type, value)
            VALUES (?, 'embed_color', ?)
        ''', (guild_id, embed_color))
        conn.commit()

    return redirect(url_for('settings', guild_id=guild_id, active_tab=request.form.get('active_tab')))

# ---------------------------------------------------------------------------------------------------------------------
# ---------------------------------------------------------------------------------------------------------------------

@app.route('/update_lottery_settings/<guild_id>', methods=['POST'])
def update_lottery_settings(guild_id):
    if 'access_token' not in session or 'expires_at' not in session or session['expires_at'] < datetime.now().timestamp():
        logger.warning("Access token expired or missing. Redirecting to login.")
        return redirect(url_for('login'))

    # Fetch user information
    headers = {
        'Authorization': f'Bearer {session["access_token"]}'
    }
    user_response = requests.get('https://discord.com/api/users/@me', headers=headers)
    if user_response.status_code != 200:
        logger.error(f"Failed to fetch user information. Status code: {user_response.status_code}")
        return "Error: Failed to fetch user information", 400

    user = user_response.json()
    logger.debug(f"Fetched user information: {user}")

    # Fetch guild information
    guild_response = requests.get(f'https://discord.com/api/guilds/{guild_id}', headers=HEADERS)
    if guild_response.status_code != 200:
        logger.error(f"Failed to fetch guild information. Status code: {guild_response.status_code}")
        return "Error: Failed to fetch guild information", 400

    guild = guild_response.json()
    logger.debug(f"Fetched guild information: {guild}")

    # Fetch user's guilds
    guilds_response = requests.get('https://discord.com/api/users/@me/guilds', headers=headers)
    if guilds_response.status_code != 200:
        logger.error(f"Failed to fetch guilds. Status code: {guilds_response.status_code}")
        return "Error: Failed to fetch guilds", 400

    guilds = guilds_response.json()
    logger.debug(f"Fetched guilds: {guilds}")

    # Check if the user has permissions to manage this guild
    user_guild = next((g for g in guilds if g['id'] == guild_id), None)
    if not user_guild:
        logger.warning(f"User does not have access to guild {guild_id}")
        return "Error: You do not have access to this guild", 403

    permissions = int(user_guild['permissions'])
    if not ((permissions & 0x8) == 0x8 or (permissions & 0x20) == 0x20):  # 0x8 = ADMINISTRATOR, 0x20 = MANAGE_GUILD
        logger.warning(f"User does not have permission to manage guild {guild_id}")
        return "Error: You do not have permission to manage this guild", 403

    # Get the lottery ID and new ticket price from the form
    lottery_id = request.form.get('lottery_to_edit')
    new_ticket_price = request.form.get('ticket_price')

    if not lottery_id or not new_ticket_price:
        logger.error("Lottery ID or ticket price not provided.")
        return "Error: Lottery ID or ticket price not provided", 400

    # Update the lottery ticket price in the database
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute('''
                UPDATE lottery_events SET ticket_price = ? WHERE id = ? AND guild_id = ?
            ''', (new_ticket_price, lottery_id, guild_id))
            conn.commit()
        logger.info(f"Successfully updated ticket price for lottery {lottery_id} in guild {guild_id}.")
    except sqlite3.Error as e:
        logger.error(f"Failed to update ticket price for lottery {lottery_id} in guild {guild_id}. Error: {e}")
        return f"Error: Failed to update ticket price. Please try again. Error: {e}", 500

    # Redirect back to the settings page with the active tab set to 'lottery'
    active_tab = request.form.get('active_tab', 'customisation')
    return redirect(url_for('settings', guild_id=guild_id, active_tab=active_tab))

@app.route('/create_lottery/<guild_id>', methods=['POST'])
def create_lottery(guild_id):
    if 'access_token' not in session or 'expires_at' not in session or session['expires_at'] < datetime.now().timestamp():
        logger.warning("Access token expired or missing. Redirecting to login.")
        return redirect(url_for('login'))

    # Fetch user information
    headers = {
        'Authorization': f'Bearer {session["access_token"]}'
    }
    user_response = requests.get('https://discord.com/api/users/@me', headers=headers)
    if user_response.status_code != 200:
        logger.error(f"Failed to fetch user information. Status code: {user_response.status_code}")
        return "Error: Failed to fetch user information", 400

    user = user_response.json()
    logger.debug(f"Fetched user information: {user}")

    # Fetch guild information
    guild_response = requests.get(f'https://discord.com/api/guilds/{guild_id}', headers=HEADERS)
    if guild_response.status_code != 200:
        logger.error(f"Failed to fetch guild information. Status code: {guild_response.status_code}")
        return "Error: Failed to fetch guild information", 400

    guild = guild_response.json()
    logger.debug(f"Fetched guild information: {guild}")

    # Fetch user's guilds
    guilds_response = requests.get('https://discord.com/api/users/@me/guilds', headers=headers)
    if guilds_response.status_code != 200:
        logger.error(f"Failed to fetch guilds. Status code: {guilds_response.status_code}")
        return "Error: Failed to fetch guilds", 400

    guilds = guilds_response.json()
    logger.debug(f"Fetched guilds: {guilds}")

    # Check if the user has permissions to manage this guild
    user_guild = next((g for g in guilds if g['id'] == guild_id), None)
    if not user_guild:
        logger.warning(f"User does not have access to guild {guild_id}")
        return "Error: You do not have access to this guild", 403

    permissions = int(user_guild['permissions'])
    if not ((permissions & 0x8) == 0x8 or (permissions & 0x20) == 0x20):  # 0x8 = ADMINISTRATOR, 0x20 = MANAGE_GUILD
        logger.warning(f"User does not have permission to manage guild {guild_id}")
        return "Error: You do not have permission to manage this guild", 403

    # Get the lottery details from the form
    lottery_name = request.form.get('lottery_name')
    prize_type = request.form.get('prize_type')
    card_prize = request.form.get('card_prize')
    ticket_price = request.form.get('ticket_price_create')

    if not lottery_name or not prize_type or not ticket_price:
        return "Error: Missing required fields", 400

    # Check if the card exists if the prize type is 'card'
    if prize_type == "card":
        if not card_prize:
            return "Error: You must select a card for the card prize.", 400

        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute('''
                SELECT card_id FROM cards WHERE name = ? AND guild_id = ?
            ''', (card_prize, guild_id))
            card = cursor.fetchone()

            if not card:
                return "Error: Card not found in the database. Please check the name.", 400

            card_prize = card[0]  # Update card_prize to be the card_id

    elif prize_type == "points":
        card_prize = None  # Ensure card_prize is nullified for points prize

    else:
        return "Error: Invalid prize type. Please select either 'points' or 'card'.", 400

    # Get the active tab from the form
    active_tab = request.form.get('active_tab', 'lottery')

    # Insert the new lottery into the database
    with sqlite3.connect(db_path) as conn:
        conn.execute('''
            INSERT INTO lottery_events (guild_id, name, prize_type, card_prize, ticket_price, active, lottery_number)
            VALUES (?, ?, ?, ?, ?, 1, ?)
        ''', (guild_id, lottery_name, prize_type, card_prize, ticket_price, random.randint(1, 5000)))
        conn.commit()

    return redirect(url_for('settings', guild_id=guild_id, active_tab=active_tab))

@app.route('/delete_lottery/<guild_id>', methods=['POST'])
def delete_lottery(guild_id):
    if 'access_token' not in session or 'expires_at' not in session or session['expires_at'] < datetime.now().timestamp():
        logger.warning("Access token expired or missing. Redirecting to login.")
        return redirect(url_for('login'))

    # Fetch user information
    headers = {
        'Authorization': f'Bearer {session["access_token"]}'
    }
    user_response = requests.get('https://discord.com/api/users/@me', headers=headers)
    if user_response.status_code != 200:
        logger.error(f"Failed to fetch user information. Status code: {user_response.status_code}")
        return "Error: Failed to fetch user information", 400

    user = user_response.json()
    logger.debug(f"Fetched user information: {user}")

    # Fetch guild information
    guild_response = requests.get(f'https://discord.com/api/guilds/{guild_id}', headers=HEADERS)
    if guild_response.status_code != 200:
        logger.error(f"Failed to fetch guild information. Status code: {guild_response.status_code}")
        return "Error: Failed to fetch guild information", 400

    guild = guild_response.json()
    logger.debug(f"Fetched guild information: {guild}")

    # Fetch user's guilds
    guilds_response = requests.get('https://discord.com/api/users/@me/guilds', headers=headers)
    if guilds_response.status_code != 200:
        logger.error(f"Failed to fetch guilds. Status code: {guilds_response.status_code}")
        return "Error: Failed to fetch guilds", 400

    guilds = guilds_response.json()
    logger.debug(f"Fetched guilds: {guilds}")

    # Check if the user has permissions to manage this guild
    user_guild = next((g for g in guilds if g['id'] == guild_id), None)
    if not user_guild:
        logger.warning(f"User does not have access to guild {guild_id}")
        return "Error: You do not have access to this guild", 403

    permissions = int(user_guild['permissions'])
    if not ((permissions & 0x8) == 0x8 or (permissions & 0x20) == 0x20):  # 0x8 = ADMINISTRATOR, 0x20 = MANAGE_GUILD
        logger.warning(f"User does not have permission to manage guild {guild_id}")
        return "Error: You do not have permission to manage this guild", 403

    # Get the lottery ID to delete from the form
    lottery_id = request.form.get('lottery_name_delete')
    if not lottery_id:
        logger.error("No lottery selected for deletion.")
        return "Error: No lottery selected", 400

    # Get the active tab from the form
    active_tab = request.form.get('active_tab', 'lottery')

    # Delete the lottery from the database
    try:
        with sqlite3.connect(db_path) as conn:
            # Delete lottery tickets first (to avoid foreign key constraint issues)
            conn.execute('DELETE FROM lottery_tickets WHERE event_id = ?', (lottery_id,))
            # Delete the lottery event
            conn.execute('DELETE FROM lottery_events WHERE id = ?', (lottery_id,))
            conn.commit()
        logger.info(f"Successfully deleted lottery {lottery_id} from guild {guild_id}.")
    except sqlite3.Error as e:
        logger.error(f"Failed to delete lottery {lottery_id} from guild {guild_id}. Error: {e}")
        return f"Error: Failed to delete lottery. Please try again. Error: {e}", 500

    return redirect(url_for('settings', guild_id=guild_id, active_tab=active_tab))

# ---------------------------------------------------------------------------------------------------------------------
# ---------------------------------------------------------------------------------------------------------------------

@app.route('/update_economy_settings/<guild_id>', methods=['POST'])
def update_economy_settings(guild_id):
    if 'access_token' not in session or 'expires_at' not in session or session[
        'expires_at'] < datetime.now().timestamp():
        logger.warning("Access token expired or missing. Redirecting to login.")
        return redirect(url_for('login'))

    # Fetch user information
    headers = {'Authorization': f'Bearer {session["access_token"]}'}
    user_response = requests.get('https://discord.com/api/users/@me', headers=headers)
    if user_response.status_code != 200:
        logger.error(f"Failed to fetch user information. Status code: {user_response.status_code}")
        return "Error: Failed to fetch user information", 400

    user = user_response.json()
    logger.debug(f"Fetched user information: {user}")

    # Fetch guild information
    guild_response = requests.get(f'https://discord.com/api/guilds/{guild_id}', headers=HEADERS)
    if guild_response.status_code != 200:
        logger.error(f"Failed to fetch guild information. Status code: {guild_response.status_code}")
        return "Error: Failed to fetch guild information", 400

    guild = guild_response.json()
    logger.debug(f"Fetched guild information: {guild}")

    # Fetch user's guilds
    guilds_response = requests.get('https://discord.com/api/users/@me/guilds', headers=headers)
    if guilds_response.status_code != 200:
        logger.error(f"Failed to fetch guilds. Status code: {guilds_response.status_code}")
        return "Error: Failed to fetch guilds", 400

    guilds = guilds_response.json()
    logger.debug(f"Fetched guilds: {guilds}")

    # Check if the user has permissions to manage this guild
    user_guild = next((g for g in guilds if g['id'] == guild_id), None)
    if not user_guild:
        logger.warning(f"User does not have access to guild {guild_id}")
        return "Error: You do not have access to this guild", 403

    permissions = int(user_guild['permissions'])
    if not ((permissions & 0x8) == 0x8 or (permissions & 0x20) == 0x20):  # ADMINISTRATOR or MANAGE_GUILD
        logger.warning(f"User does not have permission to manage guild {guild_id}")
        return "Error: You do not have permission to manage this guild", 403

    # Get the new economy settings from the form (may be partial)
    voice_points = request.form.get('voice_points_per_minute')
    msg_count_threshold = request.form.get('message_count_threshold')
    msg_reward_points = request.form.get('message_reward_points')

    # Open database connection and retrieve current configuration
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute('''
            SELECT voice_points_per_minute, message_count_threshold, message_reward_points
            FROM economy_config WHERE guild_id = ?
        ''', (guild_id,))
        current_config = cursor.fetchone()
        if current_config is None:
            # If no config exists, use defaults (or handle as needed)
            current_config = (
            DEFAULT_VOICE_POINTS_PER_MINUTE, DEFAULT_MESSAGE_COUNT_THRESHOLD, DEFAULT_MESSAGE_REWARD_POINTS)

        # Use the submitted values if provided, otherwise keep current ones
        new_voice_points = int(voice_points) if voice_points else current_config[0]
        new_msg_threshold = int(msg_count_threshold) if msg_count_threshold else current_config[1]
        new_msg_reward = int(msg_reward_points) if msg_reward_points else current_config[2]

        conn.execute('''
            INSERT OR REPLACE INTO economy_config 
            (guild_id, voice_points_per_minute, message_count_threshold, message_reward_points)
            VALUES (?, ?, ?, ?)
        ''', (guild_id, new_voice_points, new_msg_threshold, new_msg_reward))
        conn.commit()

    return redirect(url_for('settings', guild_id=guild_id, active_tab=request.form.get('active_tab')))


# ---------------------------------------------------------------------------------------------------------------------
# ---------------------------------------------------------------------------------------------------------------------


@app.route('/update_burn_settings/<guild_id>', methods=['POST'])
def update_burn_settings(guild_id):
    if 'access_token' not in session or 'expires_at' not in session or session['expires_at'] < datetime.now().timestamp():
        logger.warning("Access token expired or missing. Redirecting to login.")
        return redirect(url_for('login'))

    # Fetch user info and guilds
    headers = {'Authorization': f'Bearer {session["access_token"]}'}
    user_response = requests.get('https://discord.com/api/users/@me', headers=headers)
    if user_response.status_code != 200:
        logger.error(f"Failed to fetch user information. Status code: {user_response.status_code}")
        return "Error: Failed to fetch user information", 400

    user = user_response.json()

    guilds_response = requests.get('https://discord.com/api/users/@me/guilds', headers=headers)
    if guilds_response.status_code != 200:
        logger.error(f"Failed to fetch guilds. Status code: {guilds_response.status_code}")
        return "Error: Failed to fetch guilds", 400

    guilds = guilds_response.json()
    user_guild = next((g for g in guilds if g['id'] == guild_id), None)
    if not user_guild:
        logger.warning(f"User does not have access to guild {guild_id}")
        return "Error: You do not have access to this guild", 403

    permissions = int(user_guild['permissions'])
    if not ((permissions & 0x8) == 0x8 or (permissions & 0x20) == 0x20):  # ADMINISTRATOR or MANAGE_GUILD
        logger.warning(f"User does not have permission to manage guild {guild_id}")
        return "Error: You do not have permission to manage this guild", 403

    # Get the new burn settings from the form
    rarity = request.form.get('rarity')
    burn_value = request.form.get('burn_value')
    if not rarity or not burn_value:
        return "Error: Missing rarity or burn value", 400

    try:
        burn_value = int(burn_value)
    except ValueError:
        return "Error: Burn value must be an integer", 400

    # Update the burn value in the database
    with sqlite3.connect(db_path) as conn:
        conn.execute('''
            UPDATE rarity_weights
            SET burn_value = ?
            WHERE guild_id = ? AND rarity = ?
        ''', (burn_value, guild_id, rarity))
        conn.commit()

    return redirect(url_for('settings', guild_id=guild_id, active_tab='burn'))


# ---------------------------------------------------------------------------------------------------------------------
# ---------------------------------------------------------------------------------------------------------------------

@app.route('/update_rarity_settings/<guild_id>', methods=['POST'])
def update_rarity_settings(guild_id):
    if 'access_token' not in session or 'expires_at' not in session or session['expires_at'] < datetime.now().timestamp():
        logger.warning("Access token expired or missing. Redirecting to login.")
        return redirect(url_for('login'))

    # Fetch user information
    headers = {'Authorization': f'Bearer {session["access_token"]}'}
    user_response = requests.get('https://discord.com/api/users/@me', headers=headers)
    if user_response.status_code != 200:
        logger.error("Failed to fetch user information. Status code: %s", user_response.status_code)
        return "Error: Failed to fetch user information", 400
    user = user_response.json()

    # Verify guild access and permissions
    guilds_response = requests.get('https://discord.com/api/users/@me/guilds', headers=headers)
    if guilds_response.status_code != 200:
        logger.error("Failed to fetch guilds. Status code: %s", guilds_response.status_code)
        return "Error: Failed to fetch guilds", 400
    guilds = guilds_response.json()
    user_guild = next((g for g in guilds if g['id'] == guild_id), None)
    if not user_guild:
        logger.warning("User does not have access to guild %s", guild_id)
        return "Error: You do not have access to this guild", 403

    permissions = int(user_guild['permissions'])
    if not ((permissions & 0x8) == 0x8 or (permissions & 0x20) == 0x20):
        logger.warning("User does not have permission to manage guild %s", guild_id)
        return "Error: You do not have permission to manage this guild", 403

    # Retrieve the active tab from the form
    active_tab = request.form.get('active_tab', 'rarities')

    # Get form values
    rarity = request.form.get('rarity')
    weight = request.form.get('weight')
    burn_value = request.form.get('burn_value')
    if not rarity or not weight or not burn_value:
        return "Error: Missing one or more rarity settings", 400

    try:
        weight = float(weight)
        burn_value = int(burn_value)
    except ValueError:
        return "Error: Weight must be a number and burn value must be an integer", 400

    # Update the rarity in the database
    with sqlite3.connect(db_path) as conn:
        conn.execute('''
            INSERT INTO rarity_weights (guild_id, rarity, weight, burn_value)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, rarity) DO UPDATE SET 
                weight = excluded.weight, 
                burn_value = excluded.burn_value
        ''', (guild_id, rarity, weight, burn_value))
        conn.commit()

    # Redirect back to the settings page using the same active_tab
    return redirect(url_for('settings', guild_id=guild_id, active_tab=active_tab))

@app.route('/create_rarity/<guild_id>', methods=['POST'])
def create_rarity(guild_id):
    if 'access_token' not in session or 'expires_at' not in session or session['expires_at'] < datetime.now().timestamp():
        logger.warning("Access token expired or missing. Redirecting to login.")
        return redirect(url_for('login'))

    headers = {'Authorization': f'Bearer {session["access_token"]}'}
    user_response = requests.get('https://discord.com/api/users/@me', headers=headers)
    if user_response.status_code != 200:
        logger.error("Failed to fetch user information.")
        return "Error: Failed to fetch user information", 400
    user = user_response.json()

    guilds_response = requests.get('https://discord.com/api/users/@me/guilds', headers=headers)
    if guilds_response.status_code != 200:
        return "Error: Failed to fetch guilds", 400
    guilds = guilds_response.json()
    user_guild = next((g for g in guilds if g['id'] == guild_id), None)
    if not user_guild:
        return "Error: You do not have access to this guild", 403
    permissions = int(user_guild['permissions'])
    if not ((permissions & 0x8) == 0x8 or (permissions & 0x20) == 0x20):
        return "Error: You do not have permission to manage this guild", 403

    rarity = request.form.get('rarity')
    weight = request.form.get('weight')
    burn_value = request.form.get('burn_value')
    if not rarity or not weight or not burn_value:
        return "Error: Missing one or more fields", 400
    try:
        weight = float(weight)
        burn_value = int(burn_value)
    except ValueError:
        return "Error: Weight must be a number and burn value must be an integer", 400

    with sqlite3.connect(db_path) as conn:
        conn.execute('''
            INSERT INTO rarity_weights (guild_id, rarity, weight, burn_value)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, rarity) DO NOTHING
        ''', (guild_id, rarity, weight, burn_value))
        conn.commit()

    return redirect(url_for('settings', guild_id=guild_id, active_tab='rarities'))

@app.route('/delete_rarity/<guild_id>', methods=['POST'])
def delete_rarity(guild_id):
    if 'access_token' not in session or 'expires_at' not in session or session['expires_at'] < datetime.now().timestamp():
        return redirect(url_for('login'))

    headers = {'Authorization': f'Bearer {session["access_token"]}'}
    user_response = requests.get('https://discord.com/api/users/@me', headers=headers)
    if user_response.status_code != 200:
        return "Error: Failed to fetch user information", 400
    user = user_response.json()

    guilds_response = requests.get('https://discord.com/api/users/@me/guilds', headers=headers)
    if guilds_response.status_code != 200:
        return "Error: Failed to fetch guilds", 400
    guilds = guilds_response.json()
    user_guild = next((g for g in guilds if g['id'] == guild_id), None)
    if not user_guild:
        return "Error: You do not have access to this guild", 403
    permissions = int(user_guild['permissions'])
    if not ((permissions & 0x8) == 0x8 or (permissions & 0x20) == 0x20):
        return "Error: You do not have permission to manage this guild", 403

    rarity = request.form.get('rarity')
    if not rarity:
        return "Error: No rarity selected", 400

    with sqlite3.connect(db_path) as conn:
        conn.execute('DELETE FROM rarity_weights WHERE guild_id = ? AND rarity = ?', (guild_id, rarity))
        conn.commit()

    return redirect(url_for('settings', guild_id=guild_id, active_tab='rarities'))

@app.route('/reset_rarities/<guild_id>', methods=['POST'])
def reset_rarities(guild_id):
    if 'access_token' not in session or 'expires_at' not in session or session['expires_at'] < datetime.now().timestamp():
        return redirect(url_for('login'))

    headers = {'Authorization': f'Bearer {session["access_token"]}'}
    user_response = requests.get('https://discord.com/api/users/@me', headers=headers)
    if user_response.status_code != 200:
        return "Error: Failed to fetch user information", 400
    user = user_response.json()

    guilds_response = requests.get('https://discord.com/api/users/@me/guilds', headers=headers)
    if guilds_response.status_code != 200:
        return "Error: Failed to fetch guilds", 400
    guilds = guilds_response.json()
    user_guild = next((g for g in guilds if g['id'] == guild_id), None)
    if not user_guild:
        return "Error: You do not have access to this guild", 403
    permissions = int(user_guild['permissions'])
    if not ((permissions & 0x8) == 0x8 or (permissions & 0x20) == 0x20):
        return "Error: You do not have permission to manage this guild", 403

    default_rarities = [
        ("Common", 1.0, 10),
        ("Uncommon", 0.5, 20),
        ("Rare", 0.2, 50),
        ("Legendary", 0.01, 100)
    ]

    with sqlite3.connect(db_path) as conn:
        conn.execute('DELETE FROM rarity_weights WHERE guild_id = ?', (guild_id,))
        for rarity, weight, burn_value in default_rarities:
            conn.execute('''
                INSERT INTO rarity_weights (guild_id, rarity, weight, burn_value)
                VALUES (?, ?, ?, ?)
            ''', (guild_id, rarity, weight, burn_value))
        conn.commit()

    return redirect(url_for('settings', guild_id=guild_id, active_tab='rarities'))

# ---------------------------------------------------------------------------------------------------------------------
# ---------------------------------------------------------------------------------------------------------------------

@app.route('/create_event/<guild_id>', methods=['POST'])
def create_event(guild_id):
    # Validate session and permissions (similar to other endpoints)
    if 'access_token' not in session or 'expires_at' not in session or session[
        'expires_at'] < datetime.now().timestamp():
        logger.warning("Access token expired or missing. Redirecting to login.")
        return redirect(url_for('login'))

    headers = {'Authorization': f'Bearer {session["access_token"]}'}
    user_response = requests.get('https://discord.com/api/users/@me', headers=headers)
    if user_response.status_code != 200:
        logger.error("Failed to fetch user information.")
        return "Error: Failed to fetch user information", 400
    user = user_response.json()

    # Verify guild access and permissions
    guilds_response = requests.get('https://discord.com/api/users/@me/guilds', headers=headers)
    if guilds_response.status_code != 200:
        return "Error: Failed to fetch guilds", 400
    guilds = guilds_response.json()
    user_guild = next((g for g in guilds if g['id'] == guild_id), None)
    if not user_guild:
        return "Error: You do not have access to this guild", 403
    permissions = int(user_guild['permissions'])
    if not ((permissions & 0x8) == 0x8 or (permissions & 0x20) == 0x20):
        return "Error: You do not have permission to manage this guild", 403

    # Get form values
    event_name = request.form.get('event_name')
    points = request.form.get('points')
    cooldown = request.form.get('cooldown')
    unit = request.form.get('unit', 'hours').lower()
    set_ids = request.form.getlist('set_names')
    active_tab = request.form.get('active_tab', 'events')

    if not event_name or not points or not cooldown:
        return "Error: Missing required fields", 400
    try:
        points = int(points)
        cooldown = int(cooldown)
    except ValueError:
        return "Error: Points and cooldown must be numbers", 400

    if unit == "days":
        cooldown_hours = cooldown * 24
    elif unit == "months":
        cooldown_hours = cooldown * 24 * 30
    else:
        cooldown_hours = cooldown

    set_ids_str = ",".join(set_ids) if set_ids else ""

    with sqlite3.connect(db_path) as conn:
        # Check if event already exists
        cursor = conn.execute("SELECT 1 FROM events WHERE guild_id = ? AND event_name = ?", (guild_id, event_name))
        if cursor.fetchone():
            return "Error: An event with that name already exists.", 400

        conn.execute(
            """
            INSERT INTO events (guild_id, event_name, point_reward, set_reward, event_cooldown)
            VALUES (?, ?, ?, ?, ?)
            """,
            (guild_id, event_name, points, set_ids_str, cooldown_hours)
        )
        conn.commit()

    return redirect(url_for('settings', guild_id=guild_id, active_tab=active_tab))


@app.route('/update_event/<guild_id>', methods=['POST'])
def update_event(guild_id):
    # Validate session and permissions
    if 'access_token' not in session or 'expires_at' not in session or session[
        'expires_at'] < datetime.now().timestamp():
        logger.warning("Access token expired or missing. Redirecting to login.")
        return redirect(url_for('login'))

    headers = {'Authorization': f'Bearer {session["access_token"]}'}
    user_response = requests.get('https://discord.com/api/users/@me', headers=headers)
    if user_response.status_code != 200:
        logger.error(f"Failed to fetch user information. Status: {user_response.status_code}")
        return "Error: Failed to fetch user information", 400
    user = user_response.json()

    guilds_response = requests.get('https://discord.com/api/users/@me/guilds', headers=headers)
    if guilds_response.status_code != 200:
        logger.error(f"Failed to fetch user guilds. Status: {guilds_response.status_code}")
        return "Error: Failed to fetch guilds", 400
    guilds = guilds_response.json()

    user_guild = next((g for g in guilds if g['id'] == guild_id), None)
    if not user_guild:
        logger.warning(f"User {user['id']} does not have access to guild {guild_id}")
        return "Error: You do not have access to this guild", 403

    permissions = int(user_guild['permissions'])
    if not ((permissions & 0x8) == 0x8 or (permissions & 0x20) == 0x20):
        logger.warning(f"User {user['id']} does not have permission to manage guild {guild_id}")
        return "Error: You do not have permission to manage this guild", 403

    # Get form values
    event_name = request.form.get('event_name')
    new_event_name = request.form.get('new_event_name')  # Optional
    points = request.form.get('points')
    cooldown = request.form.get('cooldown')
    unit = request.form.get('unit')
    set_names = request.form.getlist('set_names')  # Multi-select field
    active_tab = request.form.get('active_tab', 'events')

    if not event_name:
        return "Error: No event specified", 400

    logger.debug(f"Updating event: {event_name} in guild {guild_id}")

    updates = []
    params = []

    if new_event_name:
        updates.append("event_name = ?")
        params.append(new_event_name)
    if points:
        try:
            points = int(points)
            updates.append("point_reward = ?")
            params.append(points)
        except ValueError:
            return "Error: Points must be a number", 400
    if cooldown and unit:
        try:
            cooldown = int(cooldown)
        except ValueError:
            return "Error: Cooldown must be a number", 400

        unit = unit.lower()
        if unit == "days":
            cooldown_hours = cooldown * 24
        elif unit == "months":
            cooldown_hours = cooldown * 24 * 30
        else:
            cooldown_hours = cooldown

        updates.append("event_cooldown = ?")
        params.append(cooldown_hours)

    if set_names:
        set_ids = []
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            for set_name in set_names:
                cursor.execute("SELECT set_id FROM card_sets WHERE name = ? AND guild_id = ?", (set_name, guild_id))
                set_row = cursor.fetchone()
                if set_row:
                    set_ids.append(set_row[0])
                else:
                    return f"Error: No card set found with the name '{set_name}'", 400

        set_ids_str = ",".join(map(str, set_ids))
        updates.append("set_reward = ?")
        params.append(set_ids_str)

    if not updates:
        return "Error: No updates provided", 400

    params.extend([guild_id, event_name])
    query = f"UPDATE events SET {', '.join(updates)} WHERE guild_id = ? AND event_name = ?"

    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(query, params)
            conn.commit()
        logger.info(f"Updated event '{event_name}' in guild {guild_id}")
    except sqlite3.Error as e:
        logger.error(f"Database error updating event {event_name}: {e}")
        return f"Error: Failed to update event. {e}", 500

    return redirect(url_for('settings', guild_id=guild_id, active_tab=active_tab))


@app.route('/delete_event/<guild_id>', methods=['POST'])
def delete_event(guild_id):
    # Validate session and permissions
    if 'access_token' not in session or 'expires_at' not in session or session[
        'expires_at'] < datetime.now().timestamp():
        return redirect(url_for('login'))

    headers = {'Authorization': f'Bearer {session["access_token"]}'}
    user_response = requests.get('https://discord.com/api/users/@me', headers=headers)
    if user_response.status_code != 200:
        logger.error(f"Failed to fetch user information. Status: {user_response.status_code}")
        return "Error: Failed to fetch user information", 400
    user = user_response.json()

    guilds_response = requests.get('https://discord.com/api/users/@me/guilds', headers=headers)
    if guilds_response.status_code != 200:
        logger.error(f"Failed to fetch user guilds. Status: {guilds_response.status_code}")
        return "Error: Failed to fetch guilds", 400
    guilds = guilds_response.json()

    user_guild = next((g for g in guilds if g['id'] == guild_id), None)
    if not user_guild:
        return "Error: You do not have access to this guild", 403
    permissions = int(user_guild['permissions'])
    if not ((permissions & 0x8) == 0x8 or (permissions & 0x20) == 0x20):
        return "Error: You do not have permission to manage this guild", 403

    # Get event name
    event_name = request.form.get('event_name')
    active_tab = request.form.get('active_tab', 'events')

    if not event_name:
        return "Error: No event specified", 400

    logger.debug(f"Deleting event: {event_name} in guild {guild_id}")

    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("DELETE FROM events WHERE guild_id = ? AND event_name = ?", (guild_id, event_name))
            conn.execute("DELETE FROM user_events WHERE guild_id = ? AND event_name = ?", (guild_id, event_name))
            conn.commit()
        logger.info(f"Deleted event '{event_name}' in guild {guild_id}")
    except sqlite3.Error as e:
        logger.error(f"Database error deleting event {event_name}: {e}")
        return f"Error: Failed to delete event. {e}", 500

    return redirect(url_for('settings', guild_id=guild_id, active_tab=active_tab))

# ---------------------------------------------------------------------------------------------------------------------
# ---------------------------------------------------------------------------------------------------------------------
@app.route('/get_user_cards/<guild_id>', methods=['GET'])
def get_user_cards(guild_id):
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"error": "User ID is required"}), 400

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute("""
                SELECT c.card_id, c.name, uc.quantity
                FROM user_inventory uc
                JOIN cards c ON uc.card_id = c.card_id
                WHERE uc.guild_id = ? AND uc.user_id = ?
            """, (guild_id, user_id))

            user_cards = [
                {"card_id": row[0], "name": row[1], "quantity": row[2]}
                for row in cursor.fetchall()
            ]

        logger.debug(f"User {user_id} in Guild {guild_id} has Cards: {user_cards}")
        return jsonify(user_cards)

    except Exception as e:
        logger.error(f"Error fetching user cards: {e}")
        return jsonify({"error": "Failed to fetch user cards"}), 500


@app.route('/get_users/<guild_id>', methods=['GET'])
def get_users(guild_id):
    bot = app.bot
    guild = bot.get_guild(int(guild_id))

    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute("""
                SELECT DISTINCT user_id FROM user_inventory WHERE guild_id = ?
            """, (guild_id,))
            user_ids = [str(row[0]) for row in cursor.fetchall()]  # ✅ Ensure IDs remain strings

        if not user_ids:
            return jsonify([])

        users = []
        for user_id in user_ids:
            member = guild.get_member(int(user_id))  # Fetch from bot's cache
            if member:
                username = member.nick or member.name  # Prefer nickname
            else:
                username = f"Unknown User {user_id}"  # Fallback

            users.append({"user_id": str(user_id), "username": username})  # ✅ Ensure `user_id` is always a string

        return jsonify(users)

    except Exception as e:
        logger.error(f"Error fetching users: {e}")
        return jsonify({"error": "Failed to fetch users"}), 500


# Fetch all cards in the guild
@app.route('/get_all_cards/<guild_id>', methods=['GET'])
def get_all_cards(guild_id):
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute("""
                SELECT card_id, name FROM cards WHERE guild_id = ?
            """, (guild_id,))
            cards = [{"card_id": row[0], "name": row[1]} for row in cursor.fetchall()]
        return jsonify(cards)
    except Exception as e:
        logger.error(f"Error fetching all cards: {e}")
        return jsonify({"error": "Failed to fetch cards"}), 500


@app.route('/create_card/<guild_id>', methods=['POST'])
def create_card(guild_id):
    if 'access_token' not in session or 'expires_at' not in session or session[
        'expires_at'] < datetime.now().timestamp():
        return redirect(url_for('login'))

    headers = {'Authorization': f'Bearer {session["access_token"]}'}
    user_response = requests.get('https://discord.com/api/users/@me', headers=headers)
    if user_response.status_code != 200:
        return "Error: Failed to fetch user information", 400
    user = user_response.json()

    # Ensure the user has permissions
    guilds = fetch_user_guilds(session["access_token"])
    user_guild = next((g for g in guilds if g['id'] == guild_id), None)
    if not user_guild or not ((int(user_guild['permissions']) & 0x8) == 0x8):
        return "Error: You do not have permission to manage this guild", 403

    # Retrieve form data
    card_name = request.form.get('card_name')
    description = request.form.get('description')
    rarity = request.form.get('rarity')
    set_id = request.form.get('set_id')
    img_url = request.form.get('img_url')

    if not card_name or not rarity:
        return "Error: Card name and rarity are required", 400

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()

        # Generate a new card ID
        cursor.execute("SELECT MAX(CAST(card_id AS INTEGER)) FROM cards WHERE guild_id = ?", (guild_id,))
        max_card_id = cursor.fetchone()[0]
        new_card_id = max_card_id + 1 if max_card_id else 1

        cursor.execute('''
            INSERT INTO cards (guild_id, card_id, name, description, rarity, img_url) 
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (guild_id, str(new_card_id), card_name, description, rarity, img_url))
        conn.commit()

    return redirect(url_for('settings', guild_id=guild_id, active_tab='cards'))


@app.route('/edit_card/<guild_id>', methods=['POST'])
def edit_card(guild_id):
    if 'access_token' not in session or 'expires_at' not in session or session[
        'expires_at'] < datetime.now().timestamp():
        return redirect(url_for('login'))

    user_guilds = fetch_user_guilds(session["access_token"])
    user_guild = next((g for g in user_guilds if g['id'] == guild_id), None)
    if not user_guild or not ((int(user_guild['permissions']) & 0x8) == 0x8):
        return "Error: You do not have permission to manage this guild", 403

    card_id = request.form.get('card_id')
    new_name = request.form.get('new_card_name')
    new_description = request.form.get('new_description')
    new_rarity = request.form.get('new_rarity')
    new_img_url = request.form.get('new_img_url')

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE cards 
            SET name = ?, description = ?, rarity = ?, img_url = ? 
            WHERE guild_id = ? AND card_id = ?
        ''', (new_name, new_description, new_rarity, new_img_url, guild_id, card_id))
        conn.commit()

    return redirect(url_for('settings', guild_id=guild_id, active_tab='cards'))


@app.route('/delete_card/<guild_id>', methods=['POST'])
def delete_card(guild_id):
    if 'access_token' not in session or 'expires_at' not in session or session[
        'expires_at'] < datetime.now().timestamp():
        return redirect(url_for('login'))

    card_id = request.form.get('card_id')

    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM cards WHERE guild_id = ? AND card_id = ?", (guild_id, card_id))
        conn.commit()

    return redirect(url_for('settings', guild_id=guild_id, active_tab='cards'))


@app.route('/give_card/<guild_id>', methods=['POST'])
def give_card(guild_id):
    if 'access_token' not in session or 'expires_at' not in session or session['expires_at'] < datetime.now().timestamp():
        return redirect(url_for('login'))

    user_id = request.form.get('user_id')  # 🔥 Ensure this gets the correct ID from the form
    card_id = request.form.get('card_id')

    logger.info(f"Attempting to give card. Guild: {guild_id}, User: {user_id}, Card: {card_id}")

    if not user_id or not card_id:
        logger.error("Missing user_id or card_id in form submission.")
        return "Error: Missing required fields", 400

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()

        # Check if the user already has the card
        cursor.execute("SELECT quantity FROM user_inventory WHERE user_id = ? AND card_id = ? AND guild_id = ?",
                       (user_id, card_id, guild_id))
        existing = cursor.fetchone()

        if existing:
            logger.info(f"User {user_id} already has card {card_id}. Increasing quantity.")
            cursor.execute(
                "UPDATE user_inventory SET quantity = quantity + 1 WHERE user_id = ? AND card_id = ? AND guild_id = ?",
                (user_id, card_id, guild_id))
        else:
            logger.info(f"User {user_id} does not have card {card_id}. Adding new entry.")
            cursor.execute("INSERT INTO user_inventory (guild_id, user_id, card_id, quantity) VALUES (?, ?, ?, 1)",
                           (guild_id, user_id, card_id))

        conn.commit()

    return redirect(url_for('settings', guild_id=guild_id, active_tab='cards'))


@app.route('/remove_card/<guild_id>', methods=['POST'])
def remove_card(guild_id):
    if 'access_token' not in session or 'expires_at' not in session or session[
        'expires_at'] < datetime.now().timestamp():
        return redirect(url_for('login'))

    user_id = request.form.get('user_id')
    card_id = request.form.get('card_id')

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT quantity FROM user_inventory WHERE user_id = ? AND card_id = ? AND guild_id = ?",
                       (user_id, card_id, guild_id))
        existing = cursor.fetchone()

        if existing and existing[0] > 1:
            cursor.execute(
                "UPDATE user_inventory SET quantity = quantity - 1 WHERE user_id = ? AND card_id = ? AND guild_id = ?",
                (user_id, card_id, guild_id))
        else:
            cursor.execute("DELETE FROM user_inventory WHERE user_id = ? AND card_id = ? AND guild_id = ?",
                           (user_id, card_id, guild_id))

        conn.commit()

    return redirect(url_for('settings', guild_id=guild_id, active_tab='cards'))

# ---------------------------------------------------------------------------------------------------------------------
# ---------------------------------------------------------------------------------------------------------------------

@app.route('/get_sets/<guild_id>', methods=['GET'])
def get_sets(guild_id):
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT set_id, name, description FROM card_sets WHERE guild_id = ?", (guild_id,))
        sets = [{"id": row[0], "name": row[1], "description": row[2]} for row in cursor.fetchall()]
    return jsonify(sets)

@app.route('/get_cards_in_set/<guild_id>/<set_id>', methods=['GET'])
def get_cards_in_set(guild_id, set_id):
    with sqlite3.connect(db_path) as conn:
        # Print all set_cards data
        cursor = conn.execute("SELECT * FROM set_cards WHERE guild_id = ?", (guild_id,))

        # Fetch cards linked to the set
        cursor = conn.execute('''
            SELECT cards.card_id, cards.name
            FROM set_cards
            JOIN cards ON set_cards.card_id = cards.card_id
            WHERE set_cards.set_id = ? AND set_cards.guild_id = ?
        ''', (set_id, guild_id))
        cards = [{"card_id": row[0], "name": row[1]} for row in cursor.fetchall()]

    return jsonify(cards)


@app.route('/get_presets', methods=['GET'])
def get_presets():
    try:
        # Ensure the directory exists
        if not os.path.exists(PRESET_DIR):
            print(f"Preset directory '{PRESET_DIR}' does not exist.")
            return jsonify([])

        # Print available files for debugging
        available_files = os.listdir(PRESET_DIR)
        print(f"Files in preset directory: {available_files}")

        # Get all JSON files in the preset directory
        presets = [f.replace('.json', '') for f in available_files if f.endswith('.json')]

        print(f"Available presets: {presets}")  # Debugging log
        return jsonify(presets)
    except Exception as e:
        print(f"Error fetching presets: {e}")
        return jsonify({"error": "Failed to fetch presets"}), 500



@app.route('/create_set/<guild_id>', methods=['POST'])
def create_set(guild_id):
    set_name = request.form.get('set_name')
    set_description = request.form.get('set_description', '')

    if not set_name:
        return "Error: Set name is required", 400

    with sqlite3.connect(db_path) as conn:
        # Check if a set with the same name already exists
        cursor = conn.execute("SELECT 1 FROM card_sets WHERE name = ? AND guild_id = ?", (set_name, guild_id))
        if cursor.fetchone():
            return "Error: Set with this name already exists", 400

        # Create the new set
        conn.execute("INSERT INTO card_sets (guild_id, name, description) VALUES (?, ?, ?)", (guild_id, set_name, set_description))
        conn.commit()

    return redirect(url_for('settings', guild_id=guild_id, active_tab='sets'))

@app.route('/add_card_to_set/<guild_id>', methods=['POST'])
def add_card_to_set(guild_id):
    set_id = request.form.get('set_id')  # Fix: Now correctly fetching `set_id`
    card_id = request.form.get('card_id')  # Fix: Fetching `card_id` instead of `card_name`

    if not set_id or not card_id:
        return "Error: Set ID and Card ID are required", 400

    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT 1 FROM set_cards WHERE set_id = ? AND card_id = ? AND guild_id = ?",
                              (set_id, card_id, guild_id))
        if cursor.fetchone():
            return "Error: Card already in set", 400

        # Insert into set_cards
        conn.execute("INSERT INTO set_cards (set_id, card_id, guild_id) VALUES (?, ?, ?)",
                     (set_id, card_id, guild_id))
        conn.commit()

    return redirect(url_for('settings', guild_id=guild_id, active_tab='sets'))


@app.route('/remove_card_from_set/<guild_id>', methods=['POST'])
def remove_card_from_set(guild_id):
    set_id = request.form.get('set_id')
    card_id = request.form.get('card_id')

    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("DELETE FROM set_cards WHERE set_id = ? AND card_id = ? AND guild_id = ?", (set_id, card_id, guild_id))
        conn.commit()

    return redirect(url_for('settings', guild_id=guild_id, active_tab='sets'))


@app.route('/delete_set/<guild_id>', methods=['POST'])
def delete_set(guild_id):
    set_id = request.form.get('set_id')

    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM set_cards WHERE set_id = ? AND guild_id = ?", (set_id, guild_id))
        conn.execute("DELETE FROM card_sets WHERE set_id = ? AND guild_id = ?", (set_id, guild_id))
        conn.commit()

    return redirect(url_for('settings', guild_id=guild_id, active_tab='sets'))


@app.route('/load_preset/<guild_id>', methods=['POST'])
def load_preset(guild_id):
    try:
        file = request.files.get('preset_file')
        preset_name = request.form.get('preset_name')

        if file:
            # If a file is uploaded, parse the JSON data
            if not file.filename.endswith('.json'):
                return "Error: Invalid file type. Please upload a JSON file.", 400

            try:
                preset_data = json.load(file)
            except json.JSONDecodeError:
                return "Error: Failed to decode JSON file.", 400
            is_preset = 0  # Since this is a manual import
        elif preset_name:
            # If a preset is selected from the dropdown, load it from the preset directory
            preset_path = os.path.join(PRESET_DIR, f"{preset_name}.json")

            if not os.path.exists(preset_path):
                return f"Error: Preset `{preset_name}` not found.", 400

            with open(preset_path, 'r') as f:
                preset_data = json.load(f)
            is_preset = 1  # Mark it as a preset
        else:
            return "Error: No preset name or file provided.", 400

        # Debugging output
        print(f"Loading preset: {preset_data['set']['name']} into guild {guild_id}")

        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()

            # Ensure the set does not already exist
            cursor.execute(
                "SELECT 1 FROM card_sets WHERE name = ? AND guild_id = ?",
                (preset_data['set']['name'], guild_id)
            )
            if cursor.fetchone():
                return "Error: A set with this name already exists.", 400

            # Insert set data
            cursor.execute(
                "INSERT INTO card_sets (guild_id, name, description, is_preset) VALUES (?, ?, ?, ?)",
                (guild_id, preset_data['set']['name'], preset_data['set']['description'], is_preset)
            )
            set_id = cursor.lastrowid

            # Find the maximum existing card_id in the guild
            cursor.execute("SELECT MAX(card_id) FROM cards WHERE guild_id = ?", (guild_id,))
            max_card_id_row = cursor.fetchone()
            max_card_id = int(max_card_id_row[0]) if max_card_id_row and max_card_id_row[0] else 0

            # Insert cards from the preset
            for card in preset_data['cards']:
                max_card_id += 1
                new_card_id = f"{max_card_id:08}"  # Format ID as an 8-digit string

                cursor.execute(
                    "INSERT INTO cards (guild_id, card_id, name, description, rarity, img_url, local_img_url) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (guild_id, new_card_id, card['name'], card['description'], card.get('rarity', 'Common'),
                     card.get('img_url', ''), card.get('local_img_url', ''))
                )

                # Insert into set_cards table
                cursor.execute(
                    "INSERT INTO set_cards (set_id, card_id, guild_id) VALUES (?, ?, ?)",
                    (set_id, new_card_id, guild_id)
                )

            conn.commit()

        return redirect(url_for('settings', guild_id=guild_id, active_tab='sets'))

    except Exception as e:
        print(f"Error loading preset: {e}")
        return f"Error: {e}", 500


@app.route('/export_set/<guild_id>', methods=['GET'])
def export_set(guild_id):
    set_id = request.args.get('set_id')

    if not set_id:
        return "Error: Set ID is required", 400

    print(f"Exporting set: {set_id} for guild {guild_id}")  # Debugging log

    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "SELECT name, description, is_preset FROM card_sets WHERE set_id = ? AND guild_id = ?",
            (set_id, guild_id)
        )
        set_row = cursor.fetchone()

        if not set_row:
            return "Error: Set not found", 400

        set_name, set_description, is_preset = set_row

        # Prevent exporting preset sets
        if is_preset:
            return "Error: Preset sets cannot be exported", 400

        # Fetch all cards in the set
        cursor = conn.execute(
            """SELECT cards.name, cards.description, cards.rarity, cards.img_url, cards.local_img_url
               FROM set_cards
               JOIN cards ON set_cards.card_id = cards.card_id AND set_cards.guild_id = cards.guild_id
               WHERE set_cards.set_id = ? AND set_cards.guild_id = ?""",
            (set_id, guild_id)
        )
        cards = cursor.fetchall()

        # Construct JSON structure
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

        # Convert the JSON to a file-like object
        json_data = json.dumps(preset_data, indent=4)
        json_file = io.BytesIO(json_data.encode('utf-8'))  # Convert to BytesIO for Flask response

    # Return JSON file for download
    return Response(
        json_file.getvalue(),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment;filename={set_name}.json"}
    )


@app.route('/import_set/<guild_id>', methods=['POST'])
def import_set(guild_id):
    # Ensure a file is uploaded
    if 'import_file' not in request.files:
        return "Error: No file uploaded", 400

    file = request.files['import_file']

    if file.filename == '':
        return "Error: No file selected", 400

    # Ensure the file is a JSON file
    if not file.filename.endswith('.json'):
        return "Error: Invalid file type. Please upload a JSON file.", 400

    try:
        preset_data = json.load(file)
    except json.JSONDecodeError:
        return "Error: Failed to decode JSON file.", 400

    print(f"Importing set: {preset_data['set']['name']} into guild {guild_id}")  # Debugging log

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()

        # Check if the set already exists
        cursor.execute(
            "SELECT 1 FROM card_sets WHERE name = ? AND guild_id = ?",
            (preset_data['set']['name'], guild_id)
        )
        if cursor.fetchone():
            return "Error: A set with this name already exists", 400

        # Insert the new set
        cursor.execute(
            "INSERT INTO card_sets (guild_id, name, description) VALUES (?, ?, ?)",
            (guild_id, preset_data['set']['name'], preset_data['set']['description'])
        )
        set_id = cursor.lastrowid

        # Find the highest current card_id for this guild
        cursor.execute(
            "SELECT MAX(card_id) FROM cards WHERE guild_id = ?", (guild_id,)
        )
        max_card_id_row = cursor.fetchone()
        max_card_id = int(max_card_id_row[0]) if max_card_id_row and max_card_id_row[0] else 0

        # Insert cards
        for card in preset_data['cards']:
            max_card_id += 1
            new_card_id = f"{max_card_id:08}"  # Ensure it's zero-padded

            cursor.execute(
                "INSERT INTO cards (guild_id, card_id, name, description, rarity, img_url, local_img_url) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (guild_id, new_card_id, card['name'], card['description'], card.get('rarity', 'Common'), card.get('img_url', ''), card.get('local_img_url', ''))
            )

            # Insert into set_cards table
            cursor.execute(
                "INSERT INTO set_cards (set_id, card_id, guild_id) VALUES (?, ?, ?)",
                (set_id, new_card_id, guild_id)
            )

        conn.commit()

    return redirect(url_for('settings', guild_id=guild_id, active_tab='sets'))


@app.route('/edit_set/<guild_id>', methods=['POST'])
def edit_set(guild_id):
    if 'access_token' not in session or 'expires_at' not in session or session[
        'expires_at'] < datetime.now().timestamp():
        return redirect(url_for('login'))

    # Verify user permissions
    headers = {'Authorization': f'Bearer {session["access_token"]}'}
    user_response = requests.get('https://discord.com/api/users/@me', headers=headers)
    if user_response.status_code != 200:
        return "Error: Failed to fetch user information", 400
    user = user_response.json()

    guilds_response = requests.get('https://discord.com/api/users/@me/guilds', headers=headers)
    if guilds_response.status_code != 200:
        return "Error: Failed to fetch guilds", 400
    guilds = guilds_response.json()

    user_guild = next((g for g in guilds if g['id'] == guild_id), None)
    if not user_guild:
        return "Error: You do not have access to this guild", 403

    permissions = int(user_guild['permissions'])
    if not ((permissions & 0x8) == 0x8 or (permissions & 0x20) == 0x20):
        return "Error: You do not have permission to manage this guild", 403

    # Get form data
    set_id = request.form.get('set_id')
    new_name = request.form.get('new_name')
    new_description = request.form.get('new_description')

    if not set_id:
        return "Error: Set ID is required", 400

    if not new_name and not new_description:
        return "Error: At least one of name or description must be provided", 400

    try:
        with sqlite3.connect(db_path) as conn:
            # Check if set exists
            cursor = conn.execute("SELECT 1 FROM card_sets WHERE set_id = ? AND guild_id = ?", (set_id, guild_id))
            if not cursor.fetchone():
                return "Error: Set not found", 404

            # Check if new name already exists (if changing name)
            if new_name:
                cursor = conn.execute(
                    "SELECT 1 FROM card_sets WHERE name = ? AND guild_id = ? AND set_id != ?",
                    (new_name, guild_id, set_id)
                )
                if cursor.fetchone():
                    return "Error: A set with this name already exists", 400

            # Build update query
            updates = []
            params = []

            if new_name:
                updates.append("name = ?")
                params.append(new_name)

            if new_description is not None:  # Allow empty description
                updates.append("description = ?")
                params.append(new_description)

            if updates:
                params.extend([set_id, guild_id])
                query = f"UPDATE card_sets SET {', '.join(updates)} WHERE set_id = ? AND guild_id = ?"
                conn.execute(query, params)
                conn.commit()

        return redirect(url_for('settings', guild_id=guild_id, active_tab='sets'))

    except Exception as e:
        logger.error(f"Error editing set: {e}")
        return f"Error: Failed to edit set - {str(e)}", 500
# ---------------------------------------------------------------------------------------------------------------------
# Check if Port is Available
# ---------------------------------------------------------------------------------------------------------------------

def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('localhost', port)) == 0

# ---------------------------------------------------------------------------------------------------------------------
# Webserver Class
# ---------------------------------------------------------------------------------------------------------------------
class WebServerCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        app.bot = bot
        self.executor = ThreadPoolExecutor()
        self.server_thread = None

    def start_flask_server(self):
        if is_port_in_use(5007):
            logger.warning("Port 5007 is already in use. Flask server will not be started.")
            return

        logger.info("Starting Flask server on port 5000")
        serve(app, host='0.0.0.0', port=5007)
# ---------------------------------------------------------------------------------------------------------------------
# Setup Function
# ---------------------------------------------------------------------------------------------------------------------
async def setup(bot):
    try:
        web_server_cog = WebServerCog(bot)
        await bot.add_cog(web_server_cog)
        if RUN_IN_IDE:
            # Start Flask server in a separate thread
            web_server_cog.server_thread = Thread(target=web_server_cog.start_flask_server, daemon=True)
            web_server_cog.server_thread.start()
        else:
            # Start Flask server in a separate thread (for production)
            web_server_cog.server_thread = Thread(target=web_server_cog.start_flask_server, daemon=True)
            web_server_cog.server_thread.start()
    except Exception as e:
        logger.error(f"Error setting up WebServerCog: {e}")

# ---------------------------------------------------------------------------------------------------------------------
# Webserver Configuration
# ---------------------------------------------------------------------------------------------------------------------
if __name__ == "__main__":
    from waitress import serve
    serve(app, host='0.0.0.0', port=5007)
else:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_host=1)