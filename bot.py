import discord
from discord.ext import commands, tasks
import requests
import re
import json
import asyncio
import datetime
import aiohttp
import os
from typing import Dict, List
import logging
from logging.handlers import RotatingFileHandler

# Setup logging with rotation
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler('bot.log', maxBytes=5_000_000, backupCount=3),
        logging.StreamHandler()
    ]
)

# Bot configuration from environment variables
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")  # FIXED: Use environment variable
if not BOT_TOKEN:
    logging.critical("DISCORD_BOT_TOKEN environment variable not set!")
    exit(1)

ADMIN_ROLE_ID = 1398784596509855862
PLAYER_ROLE_ID = 1427774080605753424
POINT_TRACKER_CHANNEL_ID = 1427778616435015934
LEADERBOARD_CHANNEL_ID = 1427778686832349338
ORDERS_CHANNEL_ID = 1427778727676350576

# Production intervals (seconds)
POINTS_UPDATE_INTERVAL = 3600  # 1 hour
LEADERBOARD_UPDATE_INTERVAL = 3600  # 1 hour
DAILY_ORDERS_INTERVAL = 86400  # 24 hours

# Database to store player data
player_data = {}  # Format: {player_tag: {"discord_id": id, "points": int, "name": str}}
user_accounts = {}  # Format: {discord_id: [player_tag1, player_tag2, ...]}

intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix='/', intents=intents)

class RoyaleAPIScraper:
    last_scrape_time = 0
    SCRAPE_COOLDOWN = 30  # seconds between scrapes
    
    @staticmethod
    async def get_linked_players_placements():
        """
        Scrape RoyaleAPI leaderboard using JSON extraction and find placements of linked players in top 8
        Returns list of linked players found in top 8 with their ranks (1-8)
        """
        # Rate limiting
        current_time = asyncio.get_event_loop().time()
        if current_time - RoyaleAPIScraper.last_scrape_time < RoyaleAPIScraper.SCRAPE_COOLDOWN:
            await asyncio.sleep(RoyaleAPIScraper.SCRAPE_COOLDOWN)
        
        RoyaleAPIScraper.last_scrape_time = current_time
        
        try:
            url = "https://royaleapi.com/players/leaderboard?lang=en"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=15) as response:
                    response.raise_for_status()
                    html_content = await response.text()
                    
                    # Parse the JSON data from the page
                    players = await RoyaleAPIScraper.extract_player_data_from_json(html_content)
                    
                    if players:
                        logging.info(f"Successfully extracted {len(players)} players from RoyaleAPI")
                        linked_players = await RoyaleAPIScraper.find_linked_players_in_top_8(players)
                        return linked_players
                    else:
                        logging.error("No player data found in RoyaleAPI")
                        return []
                        
        except Exception as e:
            logging.error(f"Error scraping RoyaleAPI: {e}")
            return []
    
    @staticmethod
    async def extract_player_data_from_json(html_content: str):
        """
        Extract player data from JSON embedded in the page
        """
        pattern = r'(\[\s*{.*?"tag".*?}\s*(?:,\s*{.*?"tag".*?}\s*)*\])'
        matches = re.findall(pattern, html_content, re.DOTALL)
        
        if not matches:
            logging.error("Could not find any embedded player data.")
            return []

        for i, match in enumerate(matches):
            try:
                json_str = match
                json_str = re.sub(r',\s*]', ']', json_str)
                json_str = re.sub(r',\s*}', '}', json_str)
                players = json.loads(json_str)

                if isinstance(players, list) and len(players) > 0 and 'tag' in players[0]:
                    logging.info(f"Successfully parsed JSON array with {len(players)} players")
                    return players
                    
            except json.JSONDecodeError as e:
                logging.error(f"Could not parse JSON array: {e}")
                continue

        logging.error("No valid player JSON found in the page.")
        return []
    
    @staticmethod
    async def find_linked_players_in_top_8(players: list):
        """
        Find linked players in the top 8 positions
        """
        top_8_players = players[:8]
        linked_players_found = []
        
        for player in top_8_players:
            player_tag = player.get('tag', '').replace('#', '').upper()
            player_name = player.get('name', 'Unknown')
            rank = player.get('rank', player.get('position', 0))
            
            if not player_tag:
                continue
                
            if player_tag in player_data:
                logging.info(f"Found linked player at rank {rank}: {player_name} (#{player_tag})")
                
                linked_players_found.append({
                    'tag': player_tag,
                    'name': player_name,
                    'rank': rank
                })
        
        return linked_players_found

class PointManager:
    POINT_SYSTEM = {
        1: 20,  # Top 1
        2: 14,  # Top 2
        3: 12,  # Top 3
        4: 10,  # Top 4
        5: 8,   # Top 5
        6: 6,   # Top 6
        7: 4,   # Top 7
        8: 2    # Top 8
    }
    
    @staticmethod
    def calculate_points(rank: int) -> int:
        """Calculate points based on rank"""
        return PointManager.POINT_SYSTEM.get(rank, 0)
    
    @staticmethod
    def calculate_order_percentages(player_points: Dict[str, int]) -> Dict[str, float]:
        """Calculate order percentages for players"""
        total_points = sum(player_points.values())
        if total_points == 0:
            return {}
        
        percentages = {}
        for player_tag, points in player_points.items():
            percentage = (points / total_points) * 100
            percentages[player_tag] = round(percentage, 1)
        
        return percentages

class Database:
    @staticmethod
    def get_data_path():
        """Get the data file path"""
        data_dir = os.path.join(os.getcwd(), 'data')
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, 'player_data.json')
    
    @staticmethod
    def get_backup_path():
        """Get the backup file path"""
        data_dir = os.path.join(os.getcwd(), 'data')
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, 'player_data_backup.json')
    
    @staticmethod
    def save_player_data():
        """Save player data to file with backup"""
        data = {
            'player_data': player_data,
            'user_accounts': user_accounts
        }
        try:
            data_path = Database.get_data_path()
            backup_path = Database.get_backup_path()
            
            # Create backup first
            if os.path.exists(data_path):
                import shutil
                shutil.copy2(data_path, backup_path)
            
            with open(data_path, 'w') as f:
                json.dump(data, f)
            
            logging.info("Player data saved successfully")
        except Exception as e:
            logging.error(f"Failed to save player data: {e}")
    
    @staticmethod
    def load_player_data():
        """Load player data from file with backup fallback"""
        global player_data, user_accounts
        
        try:
            data_path = Database.get_data_path()
            backup_path = Database.get_backup_path()
            
            # Try main file first
            if os.path.exists(data_path):
                with open(data_path, 'r') as f:
                    data = json.load(f)
                    player_data = data.get('player_data', {})
                    user_accounts = data.get('user_accounts', {})
                logging.info("Player data loaded successfully")
                return
            
            # Try backup file
            if os.path.exists(backup_path):
                with open(backup_path, 'r') as f:
                    data = json.load(f)
                    player_data = data.get('player_data', {})
                    user_accounts = data.get('user_accounts', {})
                logging.warning("Loaded player data from backup")
                return
                
            # No data files found
            player_data = {}
            user_accounts = {}
            logging.info("No existing player data found, starting fresh")
            
        except Exception as e:
            logging.error(f"Failed to load player data: {e}")
            player_data = {}
            user_accounts = {}

def update_user_accounts():
    """Update user_accounts from player_data"""
    global user_accounts
    user_accounts = {}
    
    for player_tag, data in player_data.items():
        discord_id = data["discord_id"]
        if discord_id not in user_accounts:
            user_accounts[discord_id] = []
        if player_tag not in user_accounts[discord_id]:
            user_accounts[discord_id].append(player_tag)

def validate_player_tag(tag: str) -> bool:
    """Validate Clash Royale player tag format"""
    import re
    pattern = r'^[0289PYLQGRJCUV]{3,}$'
    return bool(re.match(pattern, tag.upper()))

@bot.event
async def on_ready():
    logging.info(f'{bot.user} has connected to Discord!')
    Database.load_player_data()
    update_user_accounts()
    
    try:
        synced = await bot.tree.sync()
        logging.info(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        logging.error(f"Error syncing slash commands: {e}")
    
    update_points.start()
    update_leaderboard.start()
    daily_orders_calculation.start()
    health_check.start()
    
    logging.info("Bot is now running in production mode")

# Admin Commands
@bot.tree.command(name="link", description="Link a Clash Royale account to a Discord user (Admin only)")
@discord.app_commands.checks.has_role(ADMIN_ROLE_ID)
async def link_slash(interaction: discord.Interaction, player_tag: str, discord_user: discord.Member):
    player_tag = player_tag.replace('#', '').upper()
    
    if not player_tag or not validate_player_tag(player_tag):
        await interaction.response.send_message("❌ Please provide a valid player tag.", ephemeral=True)
        return
    
    if player_tag in player_data:
        current_user = bot.get_user(player_data[player_tag]["discord_id"])
        await interaction.response.send_message(
            f"❌ Player tag `#{player_tag}` is already linked to {current_user.mention if current_user else 'another user'}.", 
            ephemeral=True
        )
        return
    
    player_data[player_tag] = {
        "discord_id": discord_user.id,
        "points": 0,
        "name": discord_user.display_name
    }
    
    if discord_user.id not in user_accounts:
        user_accounts[discord_user.id] = []
    user_accounts[discord_user.id].append(player_tag)
    
    Database.save_player_data()
    
    embed = discord.Embed(
        title="✅ Account Linked Successfully",
        color=discord.Color.green(),
        timestamp=datetime.datetime.utcnow()
    )
    embed.add_field(name="Player Tag", value=f"#{player_tag}", inline=True)
    embed.add_field(name="Discord User", value=discord_user.mention, inline=True)
    embed.add_field(name="Current Points", value="0", inline=True)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="unlink", description="Unlink a Clash Royale account (Admin only)")
@discord.app_commands.checks.has_role(ADMIN_ROLE_ID)
async def unlink_slash(interaction: discord.Interaction, player_tag: str):
    player_tag = player_tag.replace('#', '').upper()
    
    if player_tag not in player_data:
        await interaction.response.send_message("❌ This player tag is not linked.", ephemeral=True)
        return
    
    discord_id = player_data[player_tag]["discord_id"]
    del player_data[player_tag]
    
    if discord_id in user_accounts and player_tag in user_accounts[discord_id]:
        user_accounts[discord_id].remove(player_tag)
        if not user_accounts[discord_id]:
            del user_accounts[discord_id]
    
    Database.save_player_data()
    await interaction.response.send_message(f"✅ Successfully unlinked player tag `#{player_tag}`.")

@bot.tree.command(name="linked_accounts", description="View all linked accounts for a user (Admin only)")
@discord.app_commands.checks.has_role(ADMIN_ROLE_ID)
async def linked_accounts_slash(interaction: discord.Interaction, discord_user: discord.Member):
    user_id = discord_user.id
    
    if user_id not in user_accounts or not user_accounts[user_id]:
        await interaction.response.send_message(
            f"❌ {discord_user.mention} doesn't have any linked accounts.", 
            ephemeral=True
        )
        return
    
    user_tags = user_accounts[user_id]
    accounts_info = []
    total_points = 0
    
    for tag in user_tags:
        points = player_data[tag]["points"]
        total_points += points
        accounts_info.append(f"`#{tag}`: {points} points")
    
    embed = discord.Embed(
        title=f"🔗 Linked Accounts - {discord_user.display_name}",
        color=discord.Color.purple(),
        timestamp=datetime.datetime.utcnow()
    )
    embed.add_field(name="Total Points", value=str(total_points), inline=True)
    embed.add_field(name="Account Count", value=str(len(user_tags)), inline=True)
    embed.add_field(name="Discord User", value=discord_user.mention, inline=True)
    
    embed.add_field(
        name="Linked Accounts", 
        value="\n".join(accounts_info) if accounts_info else "No accounts linked", 
        inline=False
    )
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="all_linked_accounts", description="View all linked accounts in the system (Admin only)")
@discord.app_commands.checks.has_role(ADMIN_ROLE_ID)
async def all_linked_accounts_slash(interaction: discord.Interaction):
    if not user_accounts:
        await interaction.response.send_message("❌ No accounts are currently linked.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="🔗 All Linked Accounts",
        color=discord.Color.purple(),
        timestamp=datetime.datetime.utcnow()
    )
    
    for user_id, tags in user_accounts.items():
        user = bot.get_user(user_id)
        if user:
            total_points = sum(player_data[tag]["points"] for tag in tags)
            account_count = len(tags)
            embed.add_field(
                name=user.display_name,
                value=f"Accounts: {account_count} | Total Points: {total_points}",
                inline=False
            )
    
    await interaction.response.send_message(embed=embed)

# Player Commands
@bot.tree.command(name="points", description="Check your current points")
@discord.app_commands.checks.has_role(PLAYER_ROLE_ID)
async def points_slash(interaction: discord.Interaction):
    user_id = interaction.user.id
    
    if user_id not in user_accounts or not user_accounts[user_id]:
        await interaction.response.send_message(
            "❌ You don't have any linked accounts. Please ask an admin to link your account using `/link`.", 
            ephemeral=True
        )
        return
    
    user_tags = user_accounts[user_id]
    total_points = 0
    accounts_info = []
    
    for tag in user_tags:
        points = player_data[tag]["points"]
        total_points += points
        accounts_info.append(f"`#{tag}`: {points} points")
    
    embed = discord.Embed(
        title="📊 Your Points Summary",
        color=discord.Color.blue(),
        timestamp=datetime.datetime.utcnow()
    )
    embed.add_field(name="Total Points", value=str(total_points), inline=True)
    embed.add_field(name="Linked Accounts", value=str(len(user_tags)), inline=True)
    embed.add_field(name="Discord User", value=interaction.user.mention, inline=True)
    
    if accounts_info:
        embed.add_field(
            name="Account Details", 
            value="\n".join(accounts_info), 
            inline=False
        )
    
    await interaction.response.send_message(embed=embed)

async def update_points_for_leaderboard():
    """Update points based on current leaderboard positions"""
    linked_players = await RoyaleAPIScraper.get_linked_players_placements()
    
    if not linked_players:
        logging.info("No linked players found in top 8")
        return []
    
    updates = []
    
    for player_info in linked_players:
        player_tag = player_info['tag']
        rank = player_info['rank']
        
        # Convert rank to integer
        try:
            rank = int(rank)
        except (ValueError, TypeError):
            logging.warning(f"Could not convert rank '{rank}' to integer for player {player_tag}")
            continue
        
        points_to_add = PointManager.calculate_points(rank)
        if points_to_add > 0:
            old_points = player_data[player_tag]["points"]
            player_data[player_tag]["points"] += points_to_add
            new_points = player_data[player_tag]["points"]
            
            logging.info(f"Added {points_to_add} points to {player_info['name']} (#{player_tag}) for rank {rank}")
            
            updates.append({
                'tag': player_tag,
                'name': player_info['name'],
                'rank': rank,
                'points_added': points_to_add,
                'total_points': new_points
            })
    
    if updates:
        Database.save_player_data()
    
    return updates

@tasks.loop(seconds=POINTS_UPDATE_INTERVAL)
async def update_points():
    """Update points every hour"""
    channel = bot.get_channel(POINT_TRACKER_CHANNEL_ID)
    if not channel:
        return
    
    updates = await update_points_for_leaderboard()
    
    if updates:
        embed = discord.Embed(
            title="🔄 Points Updated",
            description="Points have been updated based on current leaderboard positions",
            color=discord.Color.gold(),
            timestamp=datetime.datetime.utcnow()
        )
        
        for update in updates:
            embed.add_field(
                name=f"#{update['rank']} - {update['name']}",
                value=f"Added: {update['points_added']} points | Total: {update['total_points']}",
                inline=False
            )
        
        await channel.send(embed=embed)

async def create_leaderboard_embed():
    """Create leaderboard embed"""
    user_totals = {}
    for user_id, tags in user_accounts.items():
        total_points = sum(player_data[tag]["points"] for tag in tags)
        user_totals[user_id] = total_points
    
    sorted_users = sorted(user_totals.items(), key=lambda x: x[1], reverse=True)
    
    embed = discord.Embed(
        title="🏆 CRL 2025 Points Leaderboard",
        description="Current standings for CRL 20 Win Challenge Orders",
        color=discord.Color.gold(),
        timestamp=datetime.datetime.utcnow()
    )
    
    if not sorted_users:
        embed.add_field(
            name="No Players",
            value="No players have been linked yet. Use `/link` to add players.",
            inline=False
        )
        return embed
    
    leaderboard_text = ""
    for i, (user_id, total_points) in enumerate(sorted_users[:10], 1):
        user = bot.get_user(user_id)
        if user:
            account_count = len(user_accounts[user_id])
            leaderboard_text += f"**{i}. {user.mention}**\n"
            leaderboard_text += f"   Total Points: {total_points} | Accounts: {account_count}\n"
            
            if account_count <= 3:
                account_details = []
                for tag in user_accounts[user_id]:
                    points = player_data[tag]["points"]
                    account_details.append(f"`#{tag}`: {points}")
                leaderboard_text += f"   Accounts: {', '.join(account_details)}\n"
            
            leaderboard_text += "\n"
    
    embed.add_field(name="Top Players", value=leaderboard_text, inline=False)
    
    point_system_text = "\n".join([f"Top {rank}: {points} pts/hr" for rank, points in PointManager.POINT_SYSTEM.items()])
    embed.add_field(name="Point System", value=point_system_text, inline=True)
    
    return embed

@tasks.loop(seconds=LEADERBOARD_UPDATE_INTERVAL)
async def update_leaderboard():
    """Update leaderboard every hour"""
    channel = bot.get_channel(LEADERBOARD_CHANNEL_ID)
    if not channel:
        return
    
    update_user_accounts()
    
    try:
        await channel.purge(limit=10)
    except:
        pass
    
    embed = await create_leaderboard_embed()
    await channel.send(embed=embed)

async def calculate_daily_orders():
    """Calculate and send daily order percentages"""
    user_totals = {}
    for user_id, tags in user_accounts.items():
        total_points = sum(player_data[tag]["points"] for tag in tags)
        user_totals[user_id] = total_points
    
    if not user_totals:
        return None
    
    total_points = sum(user_totals.values())
    user_percentages = {}
    
    for user_id, points in user_totals.items():
        percentage = (points / total_points) * 100
        user_percentages[user_id] = round(percentage, 1)
    
    sorted_percentages = sorted(user_percentages.items(), key=lambda x: x[1], reverse=True)
    
    embed = discord.Embed(
        title="📦 Daily CRL 20 Win Challenge Orders Breakdown",
        description="Order distribution for today based on accumulated points\n*70% of total orders dedicated to pushers*",
        color=discord.Color.purple(),
        timestamp=datetime.datetime.utcnow()
    )
    
    breakdown_text = ""
    total_orders_percentage = 0
    
    for i, (user_id, percentage) in enumerate(sorted_percentages, 1):
        user = bot.get_user(user_id)
        if not user:
            continue
            
        actual_percentage = round(percentage * 0.7, 1)
        total_orders_percentage += actual_percentage
        
        account_count = len(user_accounts[user_id])
        total_user_points = user_totals[user_id]
        
        breakdown_text += f"**{i}. {user.mention}**\n"
        breakdown_text += f"   Total Points: {total_user_points}\n"
        breakdown_text += f"   Linked Accounts: {account_count}\n"
        breakdown_text += f"   Orders: {actual_percentage}%\n\n"
    
    embed.add_field(name="Order Distribution", value=breakdown_text, inline=False)
    
    summary_text = f"**Total Orders Allocated: {total_orders_percentage}%**\n"
    summary_text += f"*Remaining {round(100 - total_orders_percentage, 1)}% allocated to other categories*"
    
    embed.add_field(name="Summary", value=summary_text, inline=False)
    
    return embed

@tasks.loop(time=datetime.time(hour=16, minute=0))  # 4PM UTC = 9AM PST
async def daily_orders_calculation():
    """Calculate and send daily orders at 9AM PST - clears old embeds first"""
    channel = bot.get_channel(ORDERS_CHANNEL_ID)
    if not channel:
        return
    
    try:
        # Clear previous daily order messages from the bot
        async for message in channel.history(limit=50):
            if message.author == bot.user and "Daily CRL 20 Win Challenge Orders Breakdown" in message.embeds[0].title if message.embeds else False:
                await message.delete()
                await asyncio.sleep(1)  # Rate limit protection
    except Exception as e:
        logging.error(f"Error clearing old order messages: {e}")
    
    embed = await calculate_daily_orders()
    if embed:
        await channel.send(embed=embed)
        logging.info("Daily orders calculated and sent successfully - old embeds cleared")
    else:
        logging.info("No data available for daily orders calculation")

@tasks.loop(minutes=5)
async def health_check():
    """Monitor bot health"""
    try:
        if not update_points.is_running():
            logging.error("Points update task stopped! Restarting...")
            update_points.start()
        
        if not update_leaderboard.is_running():
            logging.error("Leaderboard update task stopped! Restarting...")
            update_leaderboard.start()
            
        if not daily_orders_calculation.is_running():
            logging.error("Daily orders task stopped! Restarting...")
            daily_orders_calculation.start()
            
    except Exception as e:
        logging.error(f"Health check failed: {e}")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRole):
        await ctx.send("❌ You don't have permission to use this command.")
    else:
        await ctx.send("❌ An error occurred while executing the command.")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, discord.app_commands.MissingRole):
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ An error occurred while executing the command.", ephemeral=True)

if __name__ == "__main__":
    Database.load_player_data()
    update_user_accounts()
    
    try:
        bot.run(BOT_TOKEN)
    except Exception as e:
        logging.critical(f"Bot crashed: {e}")