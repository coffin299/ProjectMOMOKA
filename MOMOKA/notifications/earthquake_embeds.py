"""地震・津波通知 embed の組み立てと配信。"""

from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Any, Dict, Optional

import discord

from MOMOKA.notifications.earthquake_constants import (
    InfoType,
    NOT_FOUND_DELETE_THRESHOLD,
    NOTIFY_SCALE_LABELS,
    notification_embed_footer,
    scale_to_japanese,
)
from MOMOKA.notifications.earthquake_map import CARTOPY_AVAILABLE
from MOMOKA.notifications.error.earthquake_errors import NotificationError

logger = logging.getLogger("EarthquakeTsunamiCog")


class EarthquakeEmbedsMixin:
    """地震・津波用の embed 作成と通知配信を提供する。"""

    def scale_to_japanese(self, scale_code: int | None) -> str:
        """震度コードを配信用の日本語へ変換する。"""
        # 共通ヘルパーに委譲して表示を統一する
        return scale_to_japanese(scale_code)

    def get_embed_color(
        self,
        scale_code: int | None,
        info_type: str = "quake",
    ) -> discord.Color:
        """情報種別と震度に対応する embed 色を返す。"""
        # 津波は常に既存の紫色を使う
        if info_type == InfoType.TSUNAMI.value:
            return discord.Color.purple()
        # 不明震度は灰色で表示する
        if scale_code is None or scale_code == -1:
            return discord.Color.light_grey()
        # 高い震度ほど警戒色を使う
        if scale_code >= 55:
            return discord.Color.dark_red()
        if scale_code >= 50:
            return discord.Color.red()
        if scale_code >= 40:
            return discord.Color.orange()
        if scale_code >= 30:
            return discord.Color.gold()
        return discord.Color.blue()

    def parse_earthquake_time(
        self,
        time_str: str | None,
        announced_time: str | None = None,
    ) -> datetime:
        """P2P API の時刻を JST の datetime へ変換する。"""
        # 発生時刻、発表時刻の順で既存フォーマットを解釈する
        for value in (time_str, announced_time):
            if isinstance(value, str) and value.strip():
                try:
                    return datetime.strptime(
                        value,
                        "%Y/%m/%d %H:%M:%S",
                    ).replace(tzinfo=self.jst)
                except ValueError:
                    continue
        # 欠損や不正値では現在時刻を使う
        return datetime.now(self.jst)

    @staticmethod
    def format_magnitude(magnitude: Any) -> str:
        """マグニチュードを既存の表示形式へ整形する。"""
        # 未確定値は不明として表示する
        if magnitude is None or magnitude == -1 or magnitude == "-1":
            return "不明"
        try:
            value = float(magnitude)
        except (TypeError, ValueError):
            return "不明"
        return "不明" if value == -1 else f"M{value:.1f}"

    @staticmethod
    def format_depth(depth: Any) -> str:
        """震源の深さを既存の表示形式へ整形する。"""
        # 未確定値は不明として表示する
        if depth is None or depth == -1 or depth == "-1":
            return "不明"
        try:
            if isinstance(depth, str):
                if not depth.replace("km", "").replace("m", "").strip().isdigit():
                    return depth
                value = int(depth.replace("km", "").strip())
            else:
                value = int(depth)
        except (TypeError, ValueError):
            return "不明"
        return "不明" if value == -1 else "ごく浅い" if value == 0 else f"{value}km"

    def get_tsunami_info(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """地震または津波 API 応答から津波情報を抽出する。"""
        # 常に同じキーを持つ空情報を初期化する
        info = {
            "has_tsunami": False,
            "warning_level": None,
            "areas": [],
            "description": "",
        }
        try:
            if data.get("code") == 552:
                tsunami = data.get("tsunami")
                if not isinstance(tsunami, dict):
                    return info
                # 津波情報があることを明示する
                info["has_tsunami"] = True
                grades = {
                    "MajorWarning": "大津波警報",
                    "Warning": "津波警報",
                    "Watch": "津波注意報",
                }
                priorities = {"MajorWarning": 3, "Warning": 2, "Watch": 1}
                best_grade = None
                for area in tsunami.get("areas", []):
                    if not isinstance(area, dict):
                        continue
                    grade = area.get("grade")
                    if priorities.get(grade, 0) > priorities.get(best_grade, 0):
                        best_grade = grade
                    if area.get("name"):
                        info["areas"].append(
                            {
                                "name": area["name"],
                                "grade": grades.get(grade, "情報"),
                            },
                        )
                info["warning_level"] = grades.get(best_grade, "津波予報")
                return info
            # 通常地震の domesticTsunami も既存どおり扱う
            earthquake = data.get("earthquake", {})
            domestic = earthquake.get("domesticTsunami", "None")
            if domestic not in ("None", "", None):
                info["has_tsunami"] = True
                info["warning_level"] = {
                    "Checking": "津波の有無調査中",
                    "NonEffective": "津波の心配なし",
                    "Watch": "津波注意報",
                    "Warning": "津波警報",
                    "Unknown": "不明",
                }.get(domestic, domestic)
        except Exception as error:  # noqa: BLE001
            logger.warning("津波情報取得エラー: %s", error, exc_info=True)
        return info

    def get_eew_max_scale(self, data: Dict[str, Any]) -> int:
        """EEW の地域予測震度から最大値を取得する。"""
        # 有効な地域配列以外は不明震度にする
        areas = data.get("areas", [])
        if not isinstance(areas, list):
            return -1
        # 各地域の上限値を正規化して最大を返す
        scales = [
            self.normalize_max_scale(area.get("scaleTo", area.get("scaleFrom")))
            for area in areas
            if isinstance(area, dict)
            and area.get("scaleTo", area.get("scaleFrom")) is not None
        ]
        return max(scales, default=-1)

    def format_eew_areas(self, data: Dict[str, Any]) -> str:
        """EEW の予測地域を Discord embed 用文字列へ整形する。"""
        # 配列でない地域情報は表示しない
        areas = data.get("areas", [])
        if not isinstance(areas, list):
            return ""
        formatted = []
        for area in areas:
            if not isinstance(area, dict):
                continue
            # 上下限を正規化して表示文字列を作る
            scale_to = self.normalize_max_scale(
                area.get("scaleTo", area.get("scaleFrom")),
            )
            scale_from = self.normalize_max_scale(
                area.get("scaleFrom", area.get("scaleTo")),
            )
            emoji = (
                "🔴" if scale_to >= 55 else "🟠" if scale_to >= 50
                else "🟡" if scale_to >= 40 else "🟢" if scale_to >= 30
                else "🔵"
            )
            scale_text = (
                f"{self.scale_to_japanese(scale_from)}〜"
                f"{self.scale_to_japanese(scale_to)}"
                if scale_from != scale_to
                else self.scale_to_japanese(scale_to)
            )
            arrival = area.get("arrivalTime")
            suffix = f"（到達予測: {arrival}）" if arrival else ""
            formatted.append(
                (
                    scale_to,
                    f"{emoji} **{scale_text}** - "
                    f"{area.get('name') or area.get('pref') or '地域不明'}{suffix}",
                ),
            )
        # 高い予測震度から最大8地域を表示する
        return "\n".join(
            text
            for _, text in sorted(
                formatted,
                key=lambda item: item[0],
                reverse=True,
            )[:8]
        )

    async def send_quake_notification(self, data: Dict[str, Any]) -> None:
        """通常地震情報を共通の地震通知処理へ渡す。"""
        # 既存のタイトルと種別を維持する
        await self.send_notification(data, InfoType.QUAKE.value, "📊 地震情報")

    async def send_notification(
        self,
        data: Dict[str, Any],
        info_type: str,
        title_prefix: str,
    ) -> None:
        """通常地震情報の embed を組み立てて配信する。"""
        try:
            # 震源情報がない更新通知は配信しない
            earthquake = data.get("earthquake", {})
            if not earthquake:
                return
            hypocenter = earthquake.get("hypocenter", {})
            issue = data.get("issue", {})
            max_scale = earthquake.get("maxScale", -1)
            timestamp = self.parse_earthquake_time(
                earthquake.get("time", ""),
                issue.get("time", ""),
            )
            # 既存の地震情報本文を作る
            description = (
                f"**最大震度 {self.scale_to_japanese(max_scale)}** の地震が発生しました。"
            )
            embed = discord.Embed(
                title=f"{title_prefix} ({issue.get('type', '情報')})",
                description=description,
                color=self.get_embed_color(max_scale, info_type),
                timestamp=timestamp,
            )
            # 震源の主要項目を埋め込む
            embed.add_field(
                name="🌏 震源地",
                value=f"```{hypocenter.get('name') or '調査中'}```",
                inline=True,
            )
            embed.add_field(
                name="📊 マグニチュード",
                value=f"```{self.format_magnitude(hypocenter.get('magnitude', -1))}```",
                inline=True,
            )
            embed.add_field(
                name="📏 深さ",
                value=f"```{self.format_depth(hypocenter.get('depth', -1))}```",
                inline=True,
            )
            # 地域別震度は高い順で最大8件を表示する
            points = data.get("points", [])
            if isinstance(points, list) and points:
                lines = []
                for point in sorted(
                    points,
                    key=lambda point: point.get("scale", 0),
                    reverse=True,
                )[:8]:
                    scale = point.get("scale", -1)
                    emoji = (
                        "🔴" if scale >= 55 else "🟠" if scale >= 50
                        else "🟡" if scale >= 40 else "🟢" if scale >= 30
                        else "🔵"
                    )
                    lines.append(
                        f"{emoji} **{self.scale_to_japanese(scale)}** - "
                        f"{point.get('addr', '不明')}",
                    )
                embed.add_field(
                    name="📍 各地の震度",
                    value="\n".join(lines)[:1024],
                    inline=False,
                )
            # 通常地震に津波情報があれば表示する
            tsunami_info = self.get_tsunami_info(data)
            if tsunami_info["has_tsunami"]:
                embed.add_field(
                    name="🌊 津波情報",
                    value=(
                        f"🌊 **{tsunami_info.get('warning_level', '津波予報')}** "
                        "が発表されています"
                    ),
                    inline=False,
                )
            embed.set_footer(text=notification_embed_footer())
            embed.set_thumbnail(
                url="https://www.p2pquake.net/images/QuakeLogo_100x100.png",
            )
            # 地図生成は Cartopy が利用できる場合だけ試みる
            map_file = None
            if CARTOPY_AVAILABLE:
                lat = hypocenter.get("latitude")
                lon = hypocenter.get("longitude")
                if lat is not None and lon is not None:
                    try:
                        buffer = await self.generate_single_earthquake_map(
                            {
                                "lat": lat,
                                "lon": lon,
                                "magnitude": hypocenter.get("magnitude", -1),
                                "depth": hypocenter.get("depth", -1),
                                "max_scale": max_scale,
                                "name": hypocenter.get("name", "不明"),
                                "time": timestamp,
                            },
                            info_type,
                        )
                        map_file = discord.File(
                            fp=buffer,
                            filename="earthquake_location.png",
                        )
                        embed.set_image(url="attachment://earthquake_location.png")
                    except Exception as error:  # noqa: BLE001
                        logger.warning("地図生成に失敗: %s", error)
            await self.send_embed_to_channels(
                embed,
                info_type,
                map_file,
                max_scale=max_scale,
            )
        except Exception as error:  # noqa: BLE001
            raise NotificationError(f"{info_type}通知処理エラー: {error}") from error

    async def send_embed_to_channels(
        self,
        embed: discord.Embed,
        info_type: str,
        map_file: Optional[discord.File] = None,
        max_scale: Optional[int] = None,
        apply_scale_filter: bool = True,
    ) -> None:
        """設定済みチャンネルへ embed を配信する。"""
        # 設定済みギルドごとに通知先を確認する
        config_modified = False
        for guild_id, guild_config in self.config.copy().items():
            if not isinstance(guild_config, dict):
                continue
            channel_id = guild_config.get(info_type)
            if not channel_id:
                continue
            # 津波設定と震度フィルタを既存と同じ条件で適用する
            if info_type == InfoType.TSUNAMI.value and not self.get_notify_tsunami(guild_id):
                continue
            if (
                apply_scale_filter
                and info_type in (InfoType.EEW.value, InfoType.QUAKE.value)
                and not self.should_notify_by_scale(guild_id, info_type, max_scale)
            ):
                continue
            try:
                guild = self.bot.get_guild(int(guild_id))
                if guild is None:
                    continue
                channel = guild.get_channel(int(channel_id))
                if channel is None:
                    channel = await self.bot.fetch_channel(int(channel_id))
                # 他ギルドのチャンネルや権限不足は設定を残して送らない
                if getattr(channel, "guild", None) != guild:
                    continue
                permissions = channel.permissions_for(guild.me)
                if not permissions.send_messages or not permissions.embed_links:
                    continue
                if map_file:
                    map_file.fp.seek(0)
                    file_copy = discord.File(
                        fp=io.BytesIO(map_file.fp.read()),
                        filename=map_file.filename,
                    )
                    await channel.send(embed=embed, file=file_copy)
                else:
                    await channel.send(embed=embed)
                self.not_found_counts.pop((guild_id, info_type), None)
            except discord.NotFound:
                # 実際の NotFound だけを削除判定に使う
                key = (guild_id, info_type)
                count = self.not_found_counts.get(key, 0) + 1
                self.not_found_counts[key] = count
                if count >= NOT_FOUND_DELETE_THRESHOLD:
                    settings = self.config.get(guild_id)
                    if isinstance(settings, dict) and info_type in settings:
                        del settings[info_type]
                        self.not_found_counts.pop(key, None)
                        config_modified = True
            except discord.HTTPException as error:
                logger.warning("通知送信失敗 (%s): %s", info_type, error)
            except Exception as error:  # noqa: BLE001
                logger.error("予期せぬ送信失敗 (%s): %s", info_type, error)
        # NotFound による設定削除だけを保存する
        if config_modified:
            await self.save_config()
