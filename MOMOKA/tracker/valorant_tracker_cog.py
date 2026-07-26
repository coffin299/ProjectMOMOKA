# MOMOKA/tracker/valorant_tracker_cog.py
"""
Valorant Stats Tracker Cog using HenrikDev Unofficial Valorant API
"""

import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import asyncio
from typing import Optional, Literal
from datetime import datetime
import urllib.parse

# Import custom errors (adjust path as needed)
from MOMOKA.tracker.error.errors import (
    ValorantAPIError,
    ValorantPlayerNotFoundError,
    InvalidRegionError,
    ValorantRateLimitError,
    ValorantDataParseError,
    ValorantNetworkError,
    ValorantStatsNotAvailableError
)


class ValorantAPI:
    """HenrikDev Unofficial Valorant API wrapper"""

    BASE_URL = "https://api.henrikdev.xyz/valorant"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session"""
        if self.session is None or self.session.closed:
            headers = {"Authorization": self.api_key}
            self.session = aiohttp.ClientSession(headers=headers)
        return self.session

    async def close(self):
        """Close the aiohttp session"""
        if self.session and not self.session.closed:
            await self.session.close()

    async def _request(self, endpoint: str) -> dict:
        """Make API request"""
        url = f"{self.BASE_URL}{endpoint}"
        session = await self._get_session()

        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 429:
                    retry_after = int(response.headers.get('Retry-After', 60))
                    raise ValorantRateLimitError(retry_after)

                if response.status == 404:
                    raise ValorantPlayerNotFoundError("Unknown", "Unknown")

                if response.status != 200:
                    raise ValorantAPIError(response.status)

                data = await response.json()

                if data.get("status") != 200:
                    error_msg = data.get("errors", [{}])[0].get("message", "Unknown error")
                    raise ValorantAPIError(error_msg)

                return data.get("data", {})

        except asyncio.TimeoutError:
            raise ValorantNetworkError("Request timed out after 10 seconds")
        except aiohttp.ClientError as e:
            raise ValorantNetworkError(str(e))
        except Exception as e:
            if isinstance(e, (ValorantAPIError, ValorantPlayerNotFoundError, ValorantRateLimitError,
                              ValorantNetworkError)):
                raise
            raise ValorantDataParseError(str(e))

    async def get_account(self, name: str, tag: str) -> dict:
        """Get account information"""
        # パス区切り文字として解釈されないよう Riot ID を完全にエンコードする
        endpoint = (
            f"/v2/account/{urllib.parse.quote(name, safe='')}/"
            f"{urllib.parse.quote(tag, safe='')}"
        )
        try:
            return await self._request(endpoint)
        except ValorantPlayerNotFoundError:
            raise ValorantPlayerNotFoundError(name, tag)

    async def get_mmr(self, region: str, name: str, tag: str) -> dict:
        """Get MMR/Rank information"""
        # パス区切り文字として解釈されないよう Riot ID を完全にエンコードする
        endpoint = (
            f"/v3/mmr/{region}/pc/{urllib.parse.quote(name, safe='')}/"
            f"{urllib.parse.quote(tag, safe='')}"
        )
        try:
            return await self._request(endpoint)
        except ValorantPlayerNotFoundError:
            raise ValorantPlayerNotFoundError(name, tag, region)

    async def get_mmr_history(self, region: str, name: str, tag: str) -> list:
        """Get MMR history"""
        # First get account to get PUUID
        account = await self.get_account(name, tag)
        puuid = account.get("puuid")
        if not puuid:
            raise ValorantDataParseError("Failed to get PUUID from account data")

        endpoint = f"/v1/by-puuid/mmr-history/{region}/{puuid}"
        data = await self._request(endpoint)

        # The data should be a list of match history
        if isinstance(data, dict):
            # If it's wrapped in another structure, try to extract the list
            data = data.get("data", data)

        if not isinstance(data, list):
            raise ValorantDataParseError(f"Expected list for MMR history, got {type(data).__name__}")

        return data


class ValorantCog(commands.Cog):
    """Valorant Stats Tracker"""

    VALID_REGIONS = ["eu", "na", "ap", "kr", "latam", "br"]

    RANK_COLORS = {
        "Unrated": 0x9E9E9E,
        "Iron": 0x4D4D4D,
        "Bronze": 0xCD7F32,
        "Silver": 0xC0C0C0,
        "Gold": 0xFFD700,
        "Platinum": 0x00CED1,
        "Diamond": 0xB57EDC,
        "Ascendant": 0x00FF66,
        "Immortal": 0xFF5050,
        "Radiant": 0xFFFF88,
    }

    def __init__(self, bot: commands.Bot, api_key: str):
        self.bot = bot
        self.api = ValorantAPI(api_key)

    async def cog_unload(self):
        """Cleanup when cog is unloaded"""
        await self.api.close()

    def _parse_riot_id(self, riot_id: str) -> tuple[str, str]:
        """Parse Riot ID (Name#Tag) into name and tag"""
        if "#" not in riot_id:
            raise ValueError("Invalid Riot ID format. Use Name#Tag")

        parts = riot_id.split("#", 1)
        return parts[0].strip(), parts[1].strip()

    def _get_rank_color(self, rank_name: str) -> int:
        """Get color for rank"""
        for key in self.RANK_COLORS:
            if key.lower() in rank_name.lower():
                return self.RANK_COLORS[key]
        return discord.Color.blurple().value

    @app_commands.command(name="valorant-stats", description="Get Valorant player statistics.")
    @app_commands.describe(
        riot_id="Player's Riot ID (e.g., PlayerName#TAG).",
        region="Region (eu, na, ap, kr, latam, br)."
    )
    async def valorant_stats(
            self,
            interaction: discord.Interaction,
            riot_id: str,
            region: Literal["eu", "na", "ap", "kr", "latam", "br"] = "ap"
    ):
        """Get Valorant player rank and stats"""
        await interaction.response.defer()

        try:
            # Parse Riot ID
            name, tag = self._parse_riot_id(riot_id)

            # Get account info
            account = await self.api.get_account(name, tag)

            # Get MMR data
            mmr_data = await self.api.get_mmr(region, name, tag)

            # Create embed
            embed = discord.Embed(
                title=f"📊 {account['name']}#{account['tag']}",
                color=self._get_rank_color(mmr_data.get("current", {}).get("tier", {}).get("name", "Unrated")),
                timestamp=datetime.utcnow()
            )

            # Account level
            if "account_level" in account:
                embed.add_field(
                    name="Level",
                    value=f"`{account['account_level']}`",
                    inline=True
                )

            # Current Rank
            current = mmr_data.get("current", {})
            tier = current.get("tier", {})
            rank_name = tier.get("name", "Unrated")
            rr = current.get("rr", 0)

            embed.add_field(
                name="Current Rank",
                value=f"**{rank_name}**\n`{rr} RR`",
                inline=True
            )

            # Games needed for rating (placement matches)
            games_needed = current.get("games_needed_for_rating", 0)
            if games_needed > 0:
                embed.add_field(
                    name="Placement",
                    value=f"`{games_needed} games left`",
                    inline=True
                )

            # Peak Rank
            peak = mmr_data.get("peak", {})
            if peak:
                peak_tier = peak.get("tier", {})
                peak_name = peak_tier.get("name", "N/A")
                peak_rr = peak.get("rr", 0)
                peak_season = peak.get("season", {}).get("short", "N/A")
                embed.add_field(
                    name="Peak Rank",
                    value=f"**{peak_name}** (`{peak_rr} RR`)\n`{peak_season}`",
                    inline=True
                )

            # Elo
            elo = current.get("elo", 0)
            if elo > 0:
                embed.add_field(
                    name="Elo",
                    value=f"`{elo}`",
                    inline=True
                )

            # Last RR Change
            last_change = current.get("last_change", 0)
            if last_change != 0:
                change_str = f"+{last_change}" if last_change > 0 else str(last_change)
                embed.add_field(
                    name="Last Match RR Change",
                    value=f"`{change_str}`",
                    inline=True
                )

            # Rank protection shields
            shields = current.get("rank_protection_shields", 0)
            if shields > 0:
                embed.add_field(
                    name="RR Protection",
                    value=f"`{shields} shield(s)`",
                    inline=True
                )

            # Leaderboard placement
            leaderboard = current.get("leaderboard_placement")
            if leaderboard:
                embed.add_field(
                    name="Leaderboard Rank",
                    value=f"`#{leaderboard}`",
                    inline=True
                )

            # Current season stats
            seasonal = mmr_data.get("seasonal", [])
            if seasonal and len(seasonal) > 0:
                current_season = seasonal[-1]  # Last season is current
                wins = current_season.get("wins", 0)
                games = current_season.get("games", 0)
                if games > 0:
                    winrate = (wins / games) * 100
                    embed.add_field(
                        name="Current Season",
                        value=f"`{wins}W - {games - wins}L ({winrate:.1f}%)`",
                        inline=True
                    )

            # Region
            embed.add_field(
                name="Region",
                value=f"`{region.upper()}`",
                inline=True
            )

            # Card image (if available)
            card_id = account.get("card")
            if card_id:
                # Valorant card URL format
                card_url = f"https://media.valorant-api.com/playercards/{card_id}/wideart.png"
                embed.set_thumbnail(url=card_url)

            embed.set_footer(text=f"PUUID: {account.get('puuid', 'N/A')[:8]}...")

            await interaction.followup.send(embed=embed)

        except ValueError as e:
            await interaction.followup.send(
                f"❌ {str(e)}\nPlease use the format: `Name#TAG`",
                ephemeral=True
            )
        except ValorantPlayerNotFoundError as e:
            await interaction.followup.send(
                f"❌ {e.message}",
                ephemeral=True
            )
        except ValorantRateLimitError as e:
            await interaction.followup.send(
                f"⏱️ {e.message}",
                ephemeral=True
            )
        except ValorantStatsNotAvailableError as e:
            await interaction.followup.send(
                f"📊 {e.message}",
                ephemeral=True
            )
        except (ValorantAPIError, ValorantNetworkError, ValorantDataParseError) as e:
            await interaction.followup.send(
                f"❌ Error: {e.message}",
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(
                f"❌ An unexpected error occurred: {str(e)}",
                ephemeral=True
            )

    @app_commands.command(name="valorant-recent", description="Get recent competitive matches.")
    @app_commands.describe(
        riot_id="Player's Riot ID (e.g., PlayerName#TAG).",
        region="Region (eu, na, ap, kr, latam, br)."
    )
    async def valorant_recent(
            self,
            interaction: discord.Interaction,
            riot_id: str,
            region: Literal["eu", "na", "ap", "kr", "latam", "br"] = "ap"
    ):
        """Get recent competitive match history with RR changes"""
        await interaction.response.defer()

        try:
            # Parse Riot ID
            name, tag = self._parse_riot_id(riot_id)

            # Get MMR history
            history = await self.api.get_mmr_history(region, name, tag)

            if not history or len(history) == 0:
                await interaction.followup.send(
                    f"📊 No competitive match history found for **{name}#{tag}**",
                    ephemeral=True
                )
                return

            # Create embed
            embed = discord.Embed(
                title=f"📈 Recent Matches - {name}#{tag}",
                color=discord.Color.blue(),
                timestamp=datetime.utcnow()
            )

            # Get up to 10 most recent matches
            matches = history[:10]

            for idx, match in enumerate(matches, 1):
                # Skip if match data is not a dictionary
                if not isinstance(match, dict):
                    continue

                # RR change (mmr_change_to_last_game in this API version)
                rr_change = match.get("mmr_change_to_last_game", 0)
                change_emoji = "📈" if rr_change > 0 else "📉" if rr_change < 0 else "➖"
                change_str = f"+{rr_change}" if rr_change > 0 else str(rr_change)

                # Rank after match (currenttierpatched in this API version)
                rank_name = match.get("currenttierpatched", "Unknown")

                # Ranking in tier (RR)
                rr = match.get("ranking_in_tier", 0)

                # Map
                map_data = match.get("map", {})
                if isinstance(map_data, dict):
                    map_name = map_data.get("name", "Unknown")
                else:
                    map_name = "Unknown"

                # Match date
                date_str = match.get("date", "Unknown")

                embed.add_field(
                    name=f"{change_emoji} Match {idx}",
                    value=f"**{rank_name}** (`{rr} RR`)\n`{change_str} RR`\n{map_name}",
                    inline=True
                )

            embed.set_footer(text=f"Region: {region.upper()}")

            await interaction.followup.send(embed=embed)

        except ValueError as e:
            await interaction.followup.send(
                f"❌ {str(e)}\nPlease use the format: `Name#TAG`",
                ephemeral=True
            )
        except ValorantPlayerNotFoundError as e:
            await interaction.followup.send(
                f"❌ {e.message}",
                ephemeral=True
            )
        except ValorantRateLimitError as e:
            await interaction.followup.send(
                f"⏱️ {e.message}",
                ephemeral=True
            )
        except (ValorantAPIError, ValorantNetworkError, ValorantDataParseError) as e:
            await interaction.followup.send(
                f"❌ Error: {e.message}",
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(
                f"❌ An unexpected error occurred: {str(e)}",
                ephemeral=True
            )


async def setup(bot: commands.Bot):
    """Setup function for the cog"""
    import logging

    # ログ用ロガー
    logger = logging.getLogger(__name__)

    # プレースホルダ判定用
    _PLACEHOLDERS = {
        "",
        "YOUR_API_KEY",
        "YOUR_API_KEY_HERE",
        "YOUR_VALORANT_API_KEY",
    }

    # 手動設定（互換）を先に見る
    api_key = getattr(bot, "valorant_api_key", None)

    # bot.config（configs/ マージ済み）から読む
    if not api_key and hasattr(bot, "config") and isinstance(bot.config, dict):
        # valorant セクションから api_key を取る
        api_key = (bot.config.get("valorant") or {}).get("api_key")

    # 文字列化して前後空白を除去する
    api_key = str(api_key or "").strip()

    # 未設定・プレースホルダなら Cog をスキップ（起動は継続）
    if not api_key or api_key in _PLACEHOLDERS or api_key.startswith("YOUR_"):
        logger.warning(
            "Valorant tracker skipped: api_key is unset. "
            "Set valorant.api_key in configs/tracker_config.yaml to enable. "
            "Key guide: https://discord.com/invite/X3GaVkX2YN"
        )
        # ロード失敗にせず終わる
        return

    # キーがあるときだけ Cog を登録する
    await bot.add_cog(ValorantCog(bot, api_key))
