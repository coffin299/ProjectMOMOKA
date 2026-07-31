# MOMOKA/cogs/images/image_commands_cog.py

import logging
import aiohttp
import discord
import random
from discord import app_commands
from discord.ext import commands

from MOMOKA.images.error import errors

logger = logging.getLogger(__name__)

# --- images_config の image_commands 配下で使う設定キー ---
THECATAPI_API_KEY = "thecatapi_api_key"
# User-Agent設定
BOT_USER_AGENT = "PlanaDiscordBot/1.0"


class ImageCommandsCog(commands.Cog, name="image_commands"):
    # get_cog 用の正式名
    COG_NAME = "image_commands"

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self.bot_user_agent = self.bot.config.get("bot_user_agent", BOT_USER_AGENT)
        headers = {"User-Agent": self.bot_user_agent}
        self.http_session = aiohttp.ClientSession(headers=headers)

        # images_config のネスト済み設定から TheCatAPI キーを取得する
        image_config = self.bot.config.get("image_commands", {})
        self.thecatapi_key = image_config.get(THECATAPI_API_KEY)
        if not self.thecatapi_key:
            logger.warning("TheCatAPIのAPIキーが設定されていません。レート制限が厳しくなる可能性があります。")

    async def cog_unload(self):
        await self.http_session.close()
        logger.info("ImageCommandsCog: aiohttpセッションを閉じました。")

    @app_commands.command(name="meow", description="Displays a cute cat picture.")
    async def meow_command(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        try:
            cat_api_headers = {}
            if self.thecatapi_key:
                cat_api_headers["x-api-key"] = self.thecatapi_key

            request_headers = {**self.http_session.headers, **cat_api_headers}

            async with self.http_session.get("https://api.thecatapi.com/v1/images/search",
                                             headers=request_headers) as response:
                if response.status == 200:
                    data = await response.json()
                    # 正常なデータかチェック
                    if data and isinstance(data, list) and len(data) > 0 and data[0].get("url"):
                        # --- 成功時の処理 ---
                        cat_url = data[0]["url"]
                        embed = discord.Embed(title="にゃーん！ / Meow!", color=discord.Color.random())
                        embed.set_image(url=cat_url)
                        embed.set_footer(text="Powered by thecatapi.com")
                        await interaction.followup.send(embed=embed, ephemeral=False)
                        logger.info(f"/meow: 猫画像送信成功 - {cat_url} (User: {interaction.user.id})")
                    else:
                        await errors.handle_meow_invalid_data(interaction)
                else:
                    await errors.handle_meow_api_error(interaction, response)

        # ▼▼▼ 変更点: 各エラーハンドリングを委譲 ▼▼▼
        except aiohttp.ClientError as e:  # aiohttpの接続関連エラーを広く捕捉
            await errors.handle_meow_connection_error(interaction, e)
        except Exception as e:
            await errors.handle_meow_unexpected_error(interaction, e)

    @app_commands.command(name="yandere-safe", description="Search safe images from Yandere (rating:safe).")
    @app_commands.describe(query="Search tags (space separated).")
    async def yandere_safe_command(self, interaction: discord.Interaction, query: str = ""):
        await interaction.response.defer(ephemeral=False)
        try:
            # ユーザー指定タグに安全評価タグを追加し、HTTP パラメータとしてエンコードする
            tags = " ".join(filter(None, (query.strip(), "rating:safe")))

            # Yandere APIエンドポイント
            url = "https://yande.re/post.json"

            async with self.http_session.get(
                url,
                params={"limit": 100, "tags": tags},
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if data and isinstance(data, list) and len(data) > 0:
                        # ランダムに1つ選択
                        post = random.choice(data)
                        image_url = post.get("file_url") or post.get("sample_url")

                        if image_url:
                            embed = discord.Embed(
                                title="Yandere Image (Safe)",
                                color=discord.Color.pink(),
                                url=f"https://yande.re/post/show/{post.get('id', '')}"
                            )
                            embed.set_image(url=image_url)

                            # タグ情報を追加
                            tags_str = post.get("tags", "")[:200]  # 200文字まで
                            if tags_str:
                                embed.add_field(name="Tags", value=tags_str, inline=False)

                            embed.set_footer(text=f"Rating: {post.get('rating', 'unknown')} | Yande.re")

                            await interaction.followup.send(embed=embed, ephemeral=False)
                            logger.info(f"/yandere-safe: 画像送信成功 - Query: {query} (User: {interaction.user.id})")
                        else:
                            await interaction.followup.send("画像URLが取得できませんでした。", ephemeral=False)
                    else:
                        await interaction.followup.send(
                            f"検索結果が見つかりませんでした。\nQuery: `{query}`",
                            ephemeral=False
                        )
                else:
                    await interaction.followup.send(
                        f"Yandere APIエラー: ステータスコード {response.status}",
                        ephemeral=False
                    )
                    logger.error(f"/yandere-safe: API error - Status: {response.status}")

        except aiohttp.ClientError as e:
            await interaction.followup.send("接続エラーが発生しました。", ephemeral=False)
            logger.error(f"/yandere-safe: Connection error - {e}")
        except Exception as e:
            await interaction.followup.send("予期しないエラーが発生しました。", ephemeral=False)
            logger.error(f"/yandere-safe: Unexpected error - {e}")

    @app_commands.command(name="danbooru-safe", description="Search safe images from Danbooru (rating:general).")
    @app_commands.describe(query="Search tags (space separated).")
    async def danbooru_safe_command(self, interaction: discord.Interaction, query: str = ""):
        await interaction.response.defer(ephemeral=False)
        try:
            # ユーザー指定タグに一般向け評価タグを追加し、HTTP パラメータとしてエンコードする
            tags = " ".join(filter(None, (query.strip(), "rating:general")))

            # Danbooru APIエンドポイント
            url = "https://danbooru.donmai.us/posts.json"

            async with self.http_session.get(
                url,
                params={"limit": 100, "tags": tags},
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if data and isinstance(data, list) and len(data) > 0:
                        # ランダムに1つ選択
                        post = random.choice(data)
                        image_url = post.get("file_url") or post.get("large_file_url")

                        if image_url:
                            embed = discord.Embed(
                                title="Danbooru Image (Safe)",
                                color=discord.Color.blue(),
                                url=f"https://danbooru.donmai.us/posts/{post.get('id', '')}"
                            )
                            embed.set_image(url=image_url)

                            # タグ情報を追加
                            tag_string = post.get("tag_string", "")[:200]  # 200文字まで
                            if tag_string:
                                embed.add_field(name="Tags", value=tag_string, inline=False)

                            embed.set_footer(text=f"Rating: {post.get('rating', 'unknown')} | Danbooru")

                            await interaction.followup.send(embed=embed, ephemeral=False)
                            logger.info(f"/danbooru-safe: 画像送信成功 - Query: {query} (User: {interaction.user.id})")
                        else:
                            await interaction.followup.send("画像URLが取得できませんでした。", ephemeral=False)
                    else:
                        await interaction.followup.send(
                            f"検索結果が見つかりませんでした。\nQuery: `{query}`",
                            ephemeral=False
                        )
                else:
                    await interaction.followup.send(
                        f"Danbooru APIエラー: ステータスコード {response.status}",
                        ephemeral=False
                    )
                    logger.error(f"/danbooru-safe: API error - Status: {response.status}")

        except aiohttp.ClientError as e:
            await interaction.followup.send("接続エラーが発生しました。", ephemeral=False)
            logger.error(f"/danbooru-safe: Connection error - {e}")
        except Exception as e:
            await interaction.followup.send("予期しないエラーが発生しました。", ephemeral=False)
            logger.error(f"/danbooru-safe: Unexpected error - {e}")


async def setup(bot: commands.Bot):
    if not hasattr(bot, 'config') or not bot.config:
        logger.error("ImageCommandsCog: Botインスタンスに 'config' 属性が見つからないか空です。Cogをロードできません。")
        raise commands.ExtensionFailed("ImageCommandsCog", "Botのconfigがロードされていません。")

    await bot.add_cog(ImageCommandsCog(bot))
    logger.info("ImageCommandsCogが正常にロードされました。")