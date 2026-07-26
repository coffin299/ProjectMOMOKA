# MOMOKA/timer/timer_cog.py
import discord
from discord.ext import commands
from discord import app_commands
import datetime
import asyncio

# 作成したカスタムエラーをインポート
from MOMOKA.timer.error.errors import TimerAlreadyStartedError, TimerNotStartedError

# --- 定数 ---
# メッセージの更新間隔（秒）。レートリミットを避けるため5秒以上に設定。
UPDATE_INTERVAL = 5
# タイムアウト時間（秒）。24時間 = 86400秒
TIMEOUT_SECONDS = 24 * 60 * 60


class TimerCog(commands.Cog):
    """タイマー機能を提供するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # { (guild_id, user_id): {"start_time": datetime, "message": discord.Message} }
        self.timers = {}

    # (エラーハンドリング部分は変更なし)
    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, (TimerAlreadyStartedError, TimerNotStartedError)):
            embed = discord.Embed(
                title="タイマーエラー",
                description=str(error),
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            print(f"TimerCogで予期せぬエラーが発生しました: {error}")
            await interaction.response.send_message("予期せぬエラーが発生しました。", ephemeral=True)

    timer_group = app_commands.Group(name="timer", description="Timer-related commands.")

    async def update_timer_display(self, user_key: tuple, message: discord.Message, start_time: datetime.datetime,
                                   user: discord.User):
        """バックグラウンドでタイマー表示を更新し続けるタスク（タイムアウト機能付き）"""
        while user_key in self.timers:
            try:
                # 更新間隔を待つ
                await asyncio.sleep(UPDATE_INTERVAL)

                # ループの間にタイマーが停止された場合、再度チェックして終了
                if user_key not in self.timers:
                    break

                now = datetime.datetime.now(datetime.timezone.utc)
                elapsed = now - start_time

                # --- タイムアウトチェック ---
                if elapsed.total_seconds() >= TIMEOUT_SECONDS:
                    print(f"タイマーがタイムアウトしました: {user_key}")
                    # self.timersから削除。キーが存在しない場合もエラーにならないようにする
                    self.timers.pop(user_key, None)

                    embed = discord.Embed(
                        title="⌛ タイマー自動停止",
                        description=f"{user.mention} のタイマーが24時間経過したため、自動的に停止しました。",
                        color=discord.Color.orange()
                    )
                    start_unix_timestamp = int(start_time.timestamp())
                    end_time = start_time + datetime.timedelta(seconds=TIMEOUT_SECONDS)
                    end_unix_timestamp = int(end_time.timestamp())

                    embed.add_field(name="開始時刻", value=f"<t:{start_unix_timestamp}:F>", inline=False)
                    embed.add_field(name="自動停止時刻", value=f"<t:{end_unix_timestamp}:F>", inline=False)
                    embed.add_field(name="**最終経過時間**", value="**24:00:00**", inline=False)

                    await message.edit(embed=embed)
                    break  # ループを終了

                # --- 通常の更新処理 ---
                total_seconds = int(elapsed.total_seconds())
                minutes, seconds = divmod(total_seconds, 60)

                embed = discord.Embed(
                    title="⏱️ タイマー実行中...",
                    description=f"経過時間: **{minutes:02}:{seconds:02}**",
                    color=discord.Color.green()
                )
                start_unix_timestamp = int(start_time.timestamp())
                embed.add_field(name="開始時刻", value=f"<t:{start_unix_timestamp}:T>")
                embed.set_footer(text=f"{UPDATE_INTERVAL}秒ごとに更新")

                await message.edit(embed=embed)

            except discord.NotFound:
                print(f"タイマーメッセージが見つからなかったため、更新タスクを停止します: {user_key}")
                self.timers.pop(user_key, None)
                break
            except Exception as e:
                print(f"タイマー更新中にエラーが発生しました: {e}")
                self.timers.pop(user_key, None)
                break

    @timer_group.command(name="start", description="Start a timer.")
    async def start_timer(self, interaction: discord.Interaction):
        """ユーザーのタイマーを開始する"""
        user_key = (interaction.guild_id, interaction.user.id)

        if user_key in self.timers:
            raise TimerAlreadyStartedError()

        start_time = datetime.datetime.now(datetime.timezone.utc)

        embed = discord.Embed(
            title="⏱️ タイマー開始",
            description="計測を開始しました...",
            color=discord.Color.green()
        )
        start_unix_timestamp = int(start_time.timestamp())
        embed.add_field(name="開始時刻", value=f"<t:{start_unix_timestamp}:T>")

        await interaction.response.send_message(embed=embed)
        message = await interaction.original_response()

        self.timers[user_key] = {
            "start_time": start_time,
            "message": message
        }

        # バックグラウンドで更新タスクを開始。interaction.userを渡す
        self.bot.loop.create_task(self.update_timer_display(user_key, message, start_time, interaction.user))

    @timer_group.command(name="stop", description="Stop the timer and show elapsed time.")
    async def stop_timer(self, interaction: discord.Interaction):
        """ユーザーのタイマーを停止し、結果を表示する"""
        user_key = (interaction.guild_id, interaction.user.id)

        if user_key not in self.timers:
            raise TimerNotStartedError()

        timer_data = self.timers.pop(user_key)
        start_time = timer_data["start_time"]
        original_message = timer_data["message"]

        end_time = datetime.datetime.now(datetime.timezone.utc)
        elapsed_time = end_time - start_time

        total_seconds = int(elapsed_time.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        formatted_elapsed_time = f"{hours:02}:{minutes:02}:{seconds:02}"

        embed = discord.Embed(
            title="✅ タイマー停止",
            description=f"{interaction.user.mention} がタイマーを停止しました。",
            color=discord.Color.blue()
        )
        start_unix_timestamp = int(start_time.timestamp())
        end_unix_timestamp = int(end_time.timestamp())

        embed.add_field(name="開始時刻", value=f"<t:{start_unix_timestamp}:F>", inline=False)
        embed.add_field(name="終了時刻", value=f"<t:{end_unix_timestamp}:F>", inline=False)
        embed.add_field(name="**最終経過時間**", value=f"**{formatted_elapsed_time}**", inline=False)

        try:
            await original_message.edit(embed=embed)
            await interaction.response.send_message("タイマーを停止しました。", ephemeral=True)
        except discord.NotFound:
            await interaction.response.send_message(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message(
                "メッセージの編集に失敗しました。結果を新しいメッセージで送信します。", embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(TimerCog(bot))