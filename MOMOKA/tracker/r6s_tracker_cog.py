# MOMOKA/tracker/r6s_tracker_cog.py
import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
from typing import Optional, Literal, List, Dict, Any
import urllib.parse
from datetime import datetime, timedelta
import asyncio
from functools import wraps
from MOMOKA.tracker.error.errors import (
    R6APIError,
    PlayerNotFoundError,
    InvalidPlatformError,
    RateLimitError,
    DataParseError,
    NetworkError,
    TimeoutError,
    OperatorNotFoundError,
    StatsNotAvailableError,
    ServerStatusError
)

# Constants
PLATFORMS = Literal["uplay", "psn", "xbl"]
API_BASE_URL = "https://r6-api.vercel.app/api"
REQUEST_TIMEOUT = 15
CACHE_DURATION = 300  # 5 minutes


class APICache:
    """Simple cache for API responses"""

    def __init__(self, duration: int = CACHE_DURATION):
        self.cache: Dict[str, tuple[Any, datetime]] = {}
        self.duration = duration

    def get(self, key: str) -> Optional[Any]:
        """Get cached value if still valid"""
        if key in self.cache:
            value, timestamp = self.cache[key]
            if datetime.now() - timestamp < timedelta(seconds=self.duration):
                return value
            else:
                del self.cache[key]
        return None

    def set(self, key: str, value: Any) -> None:
        """Set cache value"""
        self.cache[key] = (value, datetime.now())

    def clear(self) -> None:
        """Clear all cache"""
        self.cache.clear()


class StatsPageView(discord.ui.View):
    """View for paginating through stats embeds"""

    def __init__(self, embeds: List[discord.Embed], timeout: int = 180):
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.current_page = 0
        self.message: Optional[discord.Message] = None
        self.update_buttons()

    def update_buttons(self) -> None:
        """Update button states based on current page"""
        self.children[0].disabled = self.current_page == 0
        self.children[2].disabled = self.current_page == len(self.embeds) - 1
        self.children[1].label = f"Page {self.current_page + 1}/{len(self.embeds)}"

    async def update_message(self, interaction: discord.Interaction) -> None:
        """Update the message with current page"""
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

    @discord.ui.button(label="◀️ Previous", style=discord.ButtonStyle.primary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Go to previous page"""
        self.current_page = max(0, self.current_page - 1)
        await self.update_message(interaction)

    @discord.ui.button(label="Page 1/3", style=discord.ButtonStyle.secondary, disabled=True)
    async def page_indicator(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Page indicator (disabled button)"""
        pass

    @discord.ui.button(label="Next ▶️", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Go to next page"""
        self.current_page = min(len(self.embeds) - 1, self.current_page + 1)
        await self.update_message(interaction)

    @discord.ui.button(label="🗑️ Close", style=discord.ButtonStyle.danger)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Close the stats view"""
        await interaction.response.edit_message(view=None)
        self.stop()

    async def on_timeout(self) -> None:
        """Called when view times out"""
        if self.message:
            try:
                await self.message.edit(view=None)
            except Exception:
                pass


def handle_api_errors(func):
    """Decorator for consistent error handling across commands"""

    @wraps(func)
    async def wrapper(self, interaction: discord.Interaction, *args, **kwargs):
        try:
            return await func(self, interaction, *args, **kwargs)
        except PlayerNotFoundError as e:
            await interaction.followup.send(f"❌ {str(e)}", ephemeral=True)
        except StatsNotAvailableError as e:
            await interaction.followup.send(f"❌ {str(e)}", ephemeral=True)
        except OperatorNotFoundError as e:
            await interaction.followup.send(f"❌ {str(e)}", ephemeral=True)
        except RateLimitError as e:
            await interaction.followup.send(f"⏱️ {str(e)}", ephemeral=True)
        except NetworkError as e:
            await interaction.followup.send(f"🌐 {str(e)}", ephemeral=True)
        except TimeoutError as e:
            await interaction.followup.send(f"⏰ {str(e)}", ephemeral=True)
        except ServerStatusError as e:
            await interaction.followup.send(f"❌ {str(e)}", ephemeral=True)
        except R6APIError as e:
            await interaction.followup.send(f"❌ API Error: {str(e)}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(
                f"❌ An unexpected error occurred: {str(e)}",
                ephemeral=True
            )
            # Log the error for debugging
            print(f"Unexpected error in {func.__name__}: {e}")
            import traceback
            traceback.print_exc()

    return wrapper


class R6SiegeTrackerExtended(commands.Cog):
    """Extended Rainbow Six Siege stat tracker with comprehensive features"""

    # Rank configuration
    RANK_NAMES = {
        0: "Unranked",
        1: "Copper V", 2: "Copper IV", 3: "Copper III", 4: "Copper II", 5: "Copper I",
        6: "Bronze V", 7: "Bronze IV", 8: "Bronze III", 9: "Bronze II", 10: "Bronze I",
        11: "Silver V", 12: "Silver IV", 13: "Silver III", 14: "Silver II", 15: "Silver I",
        16: "Gold V", 17: "Gold IV", 18: "Gold III", 19: "Gold II", 20: "Gold I",
        21: "Platinum V", 22: "Platinum IV", 23: "Platinum III", 24: "Platinum II", 25: "Platinum I",
        26: "Emerald V", 27: "Emerald IV", 28: "Emerald III", 29: "Emerald II", 30: "Emerald I",
        31: "Diamond V", 32: "Diamond IV", 33: "Diamond III", 34: "Diamond II", 35: "Diamond I",
        36: "Champion"
    }

    PLATFORM_FAMILY_MAP = {
        "uplay": "pc",
        "psn": "console",
        "xbl": "console"
    }

    GAME_MODE_NAMES = {
        "standard": "🏆 Ranked",
        "living_game_mode": "⚔️ Quick Match",
        "casual": "🎮 Casual"
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.base_url = API_BASE_URL
        self.session: Optional[aiohttp.ClientSession] = None
        self.cache = APICache()

    async def cog_load(self) -> None:
        """Called when cog is loaded"""
        self.session = aiohttp.ClientSession()

    async def cog_unload(self) -> None:
        """Called when cog is unloaded"""
        if self.session:
            await self.session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        """非同期コンテキストで HTTP セッションを取得または作成する。"""
        # Cog 読込後に閉じられたセッションだけを非同期処理内で作り直す
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def _make_request(self, url: str, timeout: int = REQUEST_TIMEOUT) -> Dict[str, Any]:
        """Make HTTP request with error handling"""
        cache_key = url
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        # ClientSession は実行中の非同期コンテキストから取得する
        session = await self._get_session()

        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                if response.status == 404:
                    raise PlayerNotFoundError("Player", "unknown")
                elif response.status == 429:
                    raise RateLimitError()
                elif response.status != 200:
                    response_text = await response.text()
                    raise R6APIError(f"Status {response.status}: {response_text}")

                data = await response.json()
                self.cache.set(cache_key, data)
                return data

        except aiohttp.ClientConnectionError as e:
            raise NetworkError(str(e))
        except aiohttp.ServerTimeoutError:
            raise TimeoutError(timeout)
        except aiohttp.ClientError as e:
            raise NetworkError(str(e))

    async def fetch_account_info(self, username: str, platform: str) -> Dict[str, Any]:
        """Fetch player account information"""
        encoded_username = urllib.parse.quote(username)
        url = f"{self.base_url}/stats?type=accountInfo&nameOnPlatform={encoded_username}&platformType={platform}"

        data = await self._make_request(url)

        if not data or "profiles" not in data:
            raise StatsNotAvailableError(username, "No profile data returned from API")

        return data

    async def fetch_player_stats(self, username: str, platform: str, platform_family: str = "pc") -> Dict[str, Any]:
        """Fetch player statistics"""
        encoded_username = urllib.parse.quote(username)
        url = f"{self.base_url}/stats?type=stats&nameOnPlatform={encoded_username}&platformType={platform}&platform_families={platform_family}"

        data = await self._make_request(url)

        if not data:
            raise StatsNotAvailableError(username, "No statistics data returned")

        return data

    @staticmethod
    def get_platform_family(platform: str) -> str:
        """Get platform family from platform"""
        return R6SiegeTrackerExtended.PLATFORM_FAMILY_MAP.get(platform, "pc")

    @staticmethod
    def format_number(value: Any) -> str:
        """Format numbers to readable format"""
        try:
            num = float(value)
            if num >= 1_000_000:
                return f"{num / 1_000_000:.1f}M"
            elif num >= 1_000:
                return f"{num / 1_000:.1f}K"
            else:
                return f"{int(num):,}"
        except (ValueError, TypeError):
            return str(value)

    @staticmethod
    def format_playtime(seconds: Any) -> str:
        """Format playtime to readable format"""
        try:
            sec = int(seconds)
            hours = sec // 3600
            minutes = (sec % 3600) // 60
            return f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
        except (ValueError, TypeError):
            return "N/A"

    @classmethod
    def get_rank_name(cls, rank_id: int) -> str:
        """Get rank name from rank ID"""
        return cls.RANK_NAMES.get(rank_id, f"Rank {rank_id}")

    @staticmethod
    def get_rank_color(rank_id: int) -> discord.Color:
        """Get color based on rank"""
        if rank_id >= 36:
            return discord.Color.from_rgb(255, 215, 0)  # Champion
        elif rank_id >= 31:
            return discord.Color.from_rgb(180, 180, 255)  # Diamond
        elif rank_id >= 26:
            return discord.Color.from_rgb(80, 200, 120)  # Emerald
        elif rank_id >= 21:
            return discord.Color.from_rgb(0, 180, 240)  # Platinum
        elif rank_id >= 16:
            return discord.Color.from_rgb(255, 215, 0)  # Gold
        elif rank_id >= 11:
            return discord.Color.from_rgb(192, 192, 192)  # Silver
        elif rank_id >= 6:
            return discord.Color.from_rgb(205, 127, 50)  # Bronze
        elif rank_id >= 1:
            return discord.Color.from_rgb(184, 115, 51)  # Copper
        return discord.Color.dark_grey()  # Unranked

    def extract_season_history(self, stats_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract seasonal ranking history from stats data"""
        history = []

        try:
            platform_families = stats_data.get("platform_families_full_profiles", [])

            for pf in platform_families:
                if not isinstance(pf, dict):
                    continue

                board_ids = pf.get("board_ids_full_profiles", [])

                for board in board_ids:
                    if not isinstance(board, dict):
                        continue

                    # Only process ranked stats
                    if board.get("board_id") != "standard":
                        continue

                    full_profiles = board.get("full_profiles", [])

                    for fp in full_profiles:
                        if not isinstance(fp, dict):
                            continue

                        profile_data = fp.get("profile", {})
                        season_stats = fp.get("season_statistics", {})

                        if not season_stats:
                            continue

                        season_id = profile_data.get("season_id", "Unknown")
                        rank = profile_data.get("rank", 0)
                        rank_points = profile_data.get("rank_points", 0)
                        max_rank = profile_data.get("max_rank", 0)
                        max_rank_points = profile_data.get("max_rank_points", 0)

                        kills = season_stats.get("kills", 0)
                        deaths = season_stats.get("deaths", 0)
                        match_outcomes = season_stats.get("match_outcomes", {})

                        wins = match_outcomes.get("wins", 0)
                        losses = match_outcomes.get("losses", 0)

                        kd = (kills / deaths) if deaths > 0 else float(kills)
                        win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0.0

                        history.append({
                            "season": season_id,
                            "rank": rank,
                            "rank_name": self.get_rank_name(rank),
                            "mmr": rank_points,
                            "max_rank": max_rank,
                            "max_rank_name": self.get_rank_name(max_rank),
                            "max_mmr": max_rank_points,
                            "kills": kills,
                            "deaths": deaths,
                            "kd": kd,
                            "wins": wins,
                            "losses": losses,
                            "win_rate": win_rate,
                            "total_matches": wins + losses
                        })

        except Exception as e:
            print(f"Error extracting season history: {e}")
            import traceback
            traceback.print_exc()

        history.sort(key=lambda x: x["season"], reverse=True)
        return history

    def create_comprehensive_stats_embed(
            self,
            account_data: Dict[str, Any],
            stats_data: Dict[str, Any],
            username: str,
            platform: str
    ) -> List[discord.Embed]:
        """Create comprehensive statistics embeds with all data including MMR history"""
        embeds = []

        history = self.extract_season_history(stats_data)
        highest_rank = max((s["rank"] for s in history), default=0) if history else 0

        profile = account_data.get("profiles", [{}])[0]
        player_name = profile.get("nameOnPlatform", username)
        level = account_data.get("level", "N/A")

        # === EMBED 1: Player Overview & Current Season ===
        embed1 = discord.Embed(
            title=f"🎮 {player_name} - Rainbow Six Siege Stats",
            description=f"**Platform:** {platform.upper()} | **Level:** {level}",
            color=self.get_rank_color(highest_rank)
        )

        if "profilePicture" in account_data:
            embed1.set_thumbnail(url=account_data["profilePicture"])

        if history:
            current = history[0]
            rank_emoji = "👑" if current["rank"] >= 36 else "🏆"

            current_stats = (
                f"{rank_emoji} **{current['rank_name']}** - {current['mmr']:,} MMR\n"
                f"🔝 **Peak:** {current['max_rank_name']} ({current['max_mmr']:,} MMR)\n"
                f"📊 **Season:** {current['season']}\n\n"
                f"**Performance:**\n"
                f"├ K/D Ratio: **{current['kd']:.2f}**\n"
                f"├ Win Rate: **{current['win_rate']:.1f}%**\n"
                f"├ Matches: **{current['total_matches']:,}**\n"
                f"├ Wins: **{current['wins']:,}**\n"
                f"└ Losses: **{current['losses']:,}**"
            )
            embed1.add_field(name="🏆 Current Season (Ranked)", value=current_stats, inline=False)

        embed1.set_footer(text="📄 Page 1 of 3 • Use buttons below to navigate")
        embeds.append(embed1)

        # === EMBED 2: MMR History & Season Progress ===
        embed2 = discord.Embed(
            title=f"📊 MMR History & Seasonal Rankings",
            description=f"Player: **{player_name}**",
            color=self.get_rank_color(highest_rank)
        )

        if history:
            for season_data in history[:5]:
                rank_emoji = "👑" if season_data["rank"] >= 36 else "🏆"

                season_text = (
                    f"{rank_emoji} **{season_data['rank_name']}** ({season_data['mmr']:,} MMR)\n"
                    f"🔝 Peak: {season_data['max_rank_name']} ({season_data['max_mmr']:,})\n"
                    f"📈 K/D: {season_data['kd']:.2f} | WR: {season_data['win_rate']:.1f}%\n"
                    f"🎮 {season_data['total_matches']} matches ({season_data['wins']}W-{season_data['losses']}L)"
                )

                embed2.add_field(
                    name=f"Season {season_data['season']}",
                    value=season_text,
                    inline=False
                )

            avg_kd = sum(s["kd"] for s in history) / len(history)
            avg_wr = sum(s["win_rate"] for s in history) / len(history)
            total_matches = sum(s["total_matches"] for s in history)
            total_wins = sum(s["wins"] for s in history)
            highest_mmr = max(s["max_mmr"] for s in history)
            highest_rank_name = max(history, key=lambda s: s["max_rank"])["max_rank_name"]

            overall = (
                f"**Career Highs:**\n"
                f"├ Highest Rank: **{highest_rank_name}**\n"
                f"├ Highest MMR: **{highest_mmr:,}**\n"
                f"└ Total Wins: **{total_wins:,}**\n\n"
                f"**Averages:**\n"
                f"├ Avg K/D: **{avg_kd:.2f}**\n"
                f"├ Avg Win Rate: **{avg_wr:.1f}%**\n"
                f"└ Total Matches: **{total_matches:,}**"
            )
            embed2.add_field(name="📈 Career Statistics", value=overall, inline=False)
        else:
            embed2.add_field(
                name="⚠️ No Ranked History",
                value="No ranked match history found.",
                inline=False
            )

        embed2.set_footer(text="📄 Page 2 of 3 • Use buttons below to navigate")
        embeds.append(embed2)

        # === EMBED 3: Detailed Game Mode Statistics ===
        embed3 = discord.Embed(
            title=f"📊 Detailed Statistics by Game Mode",
            description=f"Player: **{player_name}**",
            color=self.get_rank_color(highest_rank)
        )

        self._add_game_mode_stats(embed3, stats_data)

        embed3.set_footer(text="📄 Page 3 of 3 • Use buttons below to navigate")
        embeds.append(embed3)

        return embeds

    def _add_game_mode_stats(self, embed: discord.Embed, stats_data: Dict[str, Any]) -> None:
        """Add game mode statistics to embed"""
        try:
            stats_found = False
            platform_families = stats_data.get("platform_families_full_profiles", [])

            for pf in platform_families:
                if not isinstance(pf, dict):
                    continue

                board_ids = pf.get("board_ids_full_profiles", [])

                for board in board_ids:
                    if not isinstance(board, dict):
                        continue

                    board_id = board.get("board_id", "")
                    full_profiles = board.get("full_profiles", [])

                    if not full_profiles:
                        continue

                    for fp in full_profiles:
                        if not isinstance(fp, dict):
                            continue

                        season_stats = fp.get("season_statistics", {})
                        if not season_stats:
                            continue

                        stats_text = self._format_mode_stats(season_stats)
                        mode_name = self.GAME_MODE_NAMES.get(board_id, f"📊 {board_id.title()}")

                        embed.add_field(name=mode_name, value=stats_text, inline=False)
                        stats_found = True

            if not stats_found:
                embed.add_field(
                    name="⚠️ No Statistics Available",
                    value="No statistics data found for this season.",
                    inline=False
                )

        except Exception as e:
            embed.add_field(
                name="⚠️ Statistics Parsing Error",
                value=f"Error: {str(e)}",
                inline=False
            )
            print(f"Stats parsing error: {e}")
            import traceback
            traceback.print_exc()

    @staticmethod
    def _format_mode_stats(season_stats: Dict[str, Any]) -> str:
        """Format game mode statistics"""
        kills = season_stats.get("kills", 0)
        deaths = season_stats.get("deaths", 0)
        assists = season_stats.get("assists", 0)
        headshots = season_stats.get("headshots", 0)
        melee_kills = season_stats.get("melee_kills", 0)
        revives = season_stats.get("revives", 0)

        match_outcomes = season_stats.get("match_outcomes", {})
        wins = match_outcomes.get("wins", 0)
        losses = match_outcomes.get("losses", 0)

        kd = (kills / deaths) if deaths > 0 else float(kills)
        headshot_rate = (headshots / kills * 100) if kills > 0 else 0.0
        total_matches = wins + losses
        win_rate = (wins / total_matches * 100) if total_matches > 0 else 0.0

        return (
            f"**Combat Stats:**\n"
            f"├ K/D: **{kd:.2f}** ({kills:,} kills / {deaths:,} deaths)\n"
            f"├ Headshot %: **{headshot_rate:.1f}%** ({headshots:,} headshots)\n"
            f"├ Assists: **{assists:,}**\n"
            f"├ Melee Kills: **{melee_kills:,}**\n"
            f"└ Revives: **{revives:,}**\n\n"
            f"**Match Record:**\n"
            f"├ Win Rate: **{win_rate:.1f}%**\n"
            f"├ Matches: **{total_matches:,}**\n"
            f"├ Wins: **{wins:,}**\n"
            f"└ Losses: **{losses:,}**"
        )

    @app_commands.command(
        name="r6s-stats",
        description="Display comprehensive R6 Siege player statistics."
    )
    @app_commands.describe(
        username="Player username (Ubisoft Connect name).",
        platform="Platform."
    )
    @handle_api_errors
    async def r6s_stats(
            self,
            interaction: discord.Interaction,
            username: str,
            platform: PLATFORMS = "uplay"
    ) -> None:
        """Get comprehensive R6 Siege player statistics"""
        await interaction.response.defer()

        account_data = await self.fetch_account_info(username, platform)
        platform_family = self.get_platform_family(platform)
        stats_data = await self.fetch_player_stats(username, platform, platform_family)

        embeds = self.create_comprehensive_stats_embed(account_data, stats_data, username, platform)

        view = StatsPageView(embeds)
        message = await interaction.followup.send(embed=embeds[0], view=view)
        view.message = message

    @app_commands.command(name="r6s-compare", description="Compare two players' statistics.")
    @app_commands.describe(
        player1="First player username.",
        player2="Second player username.",
        platform="Platform (both players must be on same platform)."
    )
    @handle_api_errors
    async def r6s_compare(
            self,
            interaction: discord.Interaction,
            player1: str,
            player2: str,
            platform: PLATFORMS = "uplay"
    ) -> None:
        """Compare two players"""
        await interaction.response.defer()

        platform_family = self.get_platform_family(platform)

        # Fetch both players' data concurrently
        acc1, stats1, acc2, stats2 = await asyncio.gather(
            self.fetch_account_info(player1, platform),
            self.fetch_player_stats(player1, platform, platform_family),
            self.fetch_account_info(player2, platform),
            self.fetch_player_stats(player2, platform, platform_family)
        )

        history1 = self.extract_season_history(stats1)
        history2 = self.extract_season_history(stats2)

        embed = discord.Embed(
            title="⚔️ Player Comparison",
            description=f"**{player1}** vs **{player2}** ({platform.upper()})",
            color=discord.Color.purple()
        )

        if history1 and history2:
            s1, s2 = history1[0], history2[0]

            comparisons = [
                ("🏆 Current Rank",
                 f"**{player1}**: {s1['rank_name']} ({s1['mmr']:,} MMR)\n**{player2}**: {s2['rank_name']} ({s2['mmr']:,} MMR)",
                 False),
                ("📈 K/D Ratio", f"**{player1}**: {s1['kd']:.2f}\n**{player2}**: {s2['kd']:.2f}", True),
                ("🎯 Win Rate", f"**{player1}**: {s1['win_rate']:.1f}%\n**{player2}**: {s2['win_rate']:.1f}%", True),
                ("🎮 Matches Played", f"**{player1}**: {s1['total_matches']:,}\n**{player2}**: {s2['total_matches']:,}",
                 True),
            ]

            for name, value, inline in comparisons:
                embed.add_field(name=name, value=value, inline=inline)

        level1 = acc1.get("level", 0)
        level2 = acc2.get("level", 0)
        embed.add_field(
            name="⭐ Player Level",
            value=f"**{player1}**: Level {level1}\n**{player2}**: Level {level2}",
            inline=False
        )

        embed.set_footer(text="Powered by R6Data API • Current Season Stats")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="r6s-operator", description="Search Rainbow Six Siege operator information.")
    @app_commands.describe(
        name="Operator name (e.g., Ash, Thermite).",
        role="Filter by role."
    )
    @handle_api_errors
    async def r6s_operator(
            self,
            interaction: discord.Interaction,
            name: Optional[str] = None,
            role: Optional[Literal["attacker", "defender"]] = None
    ) -> None:
        """Get operator information"""
        await interaction.response.defer()

        params = []
        if name:
            params.append(f"name={urllib.parse.quote(name)}")
        if role:
            params.append(f"roles={role}")

        query_string = "&".join(params)
        url = f"{self.base_url}/operators?{query_string}" if query_string else f"{self.base_url}/operators"

        data = await self._make_request(url, timeout=10)

        if not data:
            raise OperatorNotFoundError(name if name else "Unknown")

        operator = data[0] if isinstance(data, list) else data

        embed = discord.Embed(
            title=f"🎭 {operator.get('name', 'Unknown')}",
            description=f"**Real Name:** {operator.get('realname', 'N/A')}",
            color=discord.Color.orange()
        )

        fields = [
            ("📍 Birthplace", operator.get('birthplace', 'N/A'), True),
            ("🎂 Age", operator.get('age', 'N/A'), True),
            ("🎖️ Unit", operator.get('unit', 'N/A'), True),
            ("❤️ Health", operator.get('health', 'N/A'), True),
            ("⚡ Speed", operator.get('speed', 'N/A'), True),
            ("🎯 Role", operator.get('roles', 'N/A'), True),
            ("📅 Season Introduced", operator.get('season_introduced', 'N/A'), False)
        ]

        for field_name, field_value, inline in fields:
            embed.add_field(name=field_name, value=str(field_value), inline=inline)

        if operator.get('icon_url'):
            embed.set_thumbnail(url=operator['icon_url'])

        embed.set_footer(text="Powered by R6Data API")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="r6s-server-status", description="Check Rainbow Six Siege server status.")
    @handle_api_errors
    async def r6s_server_status(self, interaction: discord.Interaction) -> None:
        """Get R6 Siege server status"""
        await interaction.response.defer()

        url = "https://r6data.eu/api/service-status"

        try:
            data = await self._make_request(url, timeout=30)
        except Exception:
            raise ServerStatusError("Failed to fetch server status")

        embed = discord.Embed(
            title="🌐 Rainbow Six Siege Server Status",
            color=discord.Color.green()
        )

        for platform in data:
            status = platform.get('status', 'Unknown')
            services = platform.get('services', [])

            status_lower = str(status).strip().lower()
            status_emoji = "🟢" if "no issues" in status_lower or "operational" in status_lower else "🔴"

            services_text = "\n".join(services) if services else "No information"

            embed.add_field(
                name=f"{status_emoji} {platform.get('name', 'Unknown')}",
                value=f"**Status:** {status}\n{services_text}",
                inline=False
            )

        embed.set_footer(text="Powered by R6Data.eu API")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="r6s-search", description="Search for operators, weapons, maps, and more.")
    @app_commands.describe(
        query="Search query (e.g., 'Ash', 'R4-C', 'Oregon')."
    )
    @handle_api_errors
    async def r6s_search(
            self,
            interaction: discord.Interaction,
            query: str
    ) -> None:
        """Global search across all R6 data"""
        await interaction.response.defer()

        encoded_query = urllib.parse.quote(query)
        url = f"{self.base_url}/searchAll?q={encoded_query}"

        data = await self._make_request(url, timeout=10)

        if not data or not data.get('results'):
            await interaction.followup.send(
                f"❌ No results found for '{query}'",
                ephemeral=True
            )
            return

        summary = data.get('summary', {})
        results = data.get('results', {})

        embed = discord.Embed(
            title=f"🔍 Search Results: '{query}'",
            description=f"Found {summary.get('total', 0)} total results",
            color=discord.Color.blue()
        )

        search_categories = [
            ("operators", "🎭 Operators", lambda x: f"{x.get('name', 'Unknown')} ({x.get('roles', 'N/A')})"),
            ("weapons", "🔫 Weapons", lambda x: f"{x.get('name', 'Unknown')} ({x.get('type', 'N/A')})"),
            ("maps", "🗺️ Maps", lambda x: f"{x.get('name', 'Unknown')} ({x.get('location', 'N/A')})"),
            ("seasons", "📅 Seasons", lambda x: f"{x.get('name', 'Unknown')}")
        ]

        for key, title, formatter in search_categories:
            items = results.get(key, [])
            if items:
                item_text = "\n".join([f"• {formatter(item)}" for item in items[:3]])
                if len(items) > 3:
                    item_text += f"\n*...and {len(items) - 3} more*"
                embed.add_field(name=f"{title} ({len(items)})", value=item_text, inline=False)

        embed.set_footer(text="Powered by R6Data API • Use specific commands for detailed info")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="r6s-map", description="Get information about Rainbow Six Siege maps.")
    @app_commands.describe(
        name="Map name (e.g., 'Oregon', 'Clubhouse')."
    )
    @handle_api_errors
    async def r6s_map(
            self,
            interaction: discord.Interaction,
            name: Optional[str] = None
    ) -> None:
        """Get map information"""
        await interaction.response.defer()

        url = f"{self.base_url}/maps"
        if name:
            url += f"?name={urllib.parse.quote(name)}"

        data = await self._make_request(url, timeout=10)

        if not data:
            await interaction.followup.send(
                f"❌ No maps found" + (f" for '{name}'" if name else ""),
                ephemeral=True
            )
            return

        map_data = data[0] if isinstance(data, list) else data

        embed = discord.Embed(
            title=f"🗺️ {map_data.get('name', 'Unknown Map')}",
            color=discord.Color.green()
        )

        fields = [
            ("📍 Location", map_data.get('location', 'N/A'), True),
            ("📅 Release Date", map_data.get('releaseDate', 'N/A'), True),
            ("🎮 Available In", map_data.get('playlists', 'N/A'), False)
        ]

        for field_name, field_value, inline in fields:
            embed.add_field(name=field_name, value=field_value, inline=inline)

        rework = map_data.get('mapReworked')
        if rework:
            embed.add_field(name="🔧 Reworked", value=rework, inline=True)

        embed.set_footer(text="Powered by R6Data API")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="r6s-weapon", description="Get information about weapons.")
    @app_commands.describe(
        name="Weapon name (e.g., 'R4-C', 'AK-12')."
    )
    @handle_api_errors
    async def r6s_weapon(
            self,
            interaction: discord.Interaction,
            name: str
    ) -> None:
        """Get weapon information"""
        await interaction.response.defer()

        encoded_name = urllib.parse.quote(name)
        url = f"{self.base_url}/weapons?name={encoded_name}"

        data = await self._make_request(url, timeout=10)

        if not data:
            await interaction.followup.send(
                f"❌ Weapon '{name}' not found",
                ephemeral=True
            )
            return

        weapon = data[0] if isinstance(data, list) else data

        embed = discord.Embed(
            title=f"🔫 {weapon.get('name', 'Unknown Weapon')}",
            color=discord.Color.red()
        )

        fields = [
            ("Type", weapon.get('type', 'N/A'), True),
            ("Damage", str(weapon.get('damage', 'N/A')), True),
            ("Fire Rate", f"{weapon.get('fireRate', 'N/A')} RPM", True),
            ("Magazine Size", str(weapon.get('magazineSize', 'N/A')), True)
        ]

        for field_name, field_value, inline in fields:
            embed.add_field(name=field_name, value=field_value, inline=inline)

        operators = weapon.get('operators', [])
        if operators:
            op_list = ", ".join(operators[:5])
            if len(operators) > 5:
                op_list += f", +{len(operators) - 5} more"
            embed.add_field(name="Used By", value=op_list, inline=False)

        embed.set_footer(text="Powered by R6Data API")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="r6s-clear-cache", description="Clear API response cache (Admin only).")
    @app_commands.default_permissions(administrator=True)
    async def r6s_clear_cache(self, interaction: discord.Interaction) -> None:
        """Clear the API cache"""
        self.cache.clear()
        await interaction.response.send_message(
            "✅ API cache cleared successfully!",
            ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    """Cog setup function"""
    await bot.add_cog(R6SiegeTrackerExtended(bot))