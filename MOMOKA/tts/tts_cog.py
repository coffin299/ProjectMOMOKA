# PLANA/tts/tts_cog.py
import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
import io
import asyncio
import json
from pathlib import Path
import re
from typing import Dict, Optional, List, Any
import time
import logging
import gc  # ガベージコレクションを追加

try:
    from MOMOKA.music.music_cog import MusicCog
    from MOMOKA.music.plugins.audio_mixer import TTSAudioSource, MusicAudioSource
except ImportError:
    MusicCog = None
    TTSAudioSource = None
    MusicAudioSource = None

# MusicCog 未 import 時も get_cog 名を解決できるようフォールバックする
_MUSIC_COG_NAME = MusicCog.COG_NAME if MusicCog is not None else "music_cog"

try:
    from MOMOKA.tts.error.errors import TTSCogExceptionHandler
except ImportError as e:
    logging.getLogger("MOMOKA.tts").critical(
        "TTSCog: 必須コンポーネントのインポートに失敗しました。エラー: %s", e
    )
    TTSCogExceptionHandler = None

from MOMOKA.storage import (
    NS_SPEECH_DICTIONARY,
    NS_SPEECH_SETTINGS,
    NS_TTS_SETTINGS,
    resolve_settings_db,
)
from MOMOKA.bots.registry import registry


class TTSCog(commands.Cog, name="tts_cog"):
    """
    TTS Cog - Style-Bert-VITS2対応
    
    Note: KoboldCPPはLLM推論サーバーであり、TTSとは直接関係ありません。
    このCogはStyle-Bert-VITS2を使用してテキストを音声に変換します。
    KoboldCPPを使用している場合でも、LLM Cogが応答を生成すると、
    このCogが自動的にその応答を読み上げます（設定されている場合）。
    """
    # get_cog 用の正式名
    COG_NAME = "tts_cog"

    def __init__(self, bot: commands.Bot):
        if TTSCogExceptionHandler is None:
            raise commands.ExtensionFailed(self.qualified_name,
                                           "必須コンポーネントTTSCogExceptionHandlerのインポート失敗")
        if TTSAudioSource is None:
            raise commands.ExtensionFailed(self.qualified_name,
                                           "必須コンポーネントTTSAudioSourceのインポート失敗")
        self.bot = bot
        self.config = bot.config.get('tts', {})

        # Internal TTS synthesizer configuration
        from MOMOKA.generator.tts import StyleBertVITS2Synthesizer, SynthesizerConfig
        tts_cfg = SynthesizerConfig(
            model_root=self.config.get('model_root', 'models/tts-models'),
            model_name=self.config.get('default_model_dir'),
            dictionary_dir=self.config.get('pyopenjtalk_dict_dir'),
            sample_rate=int(self.config.get('sample_rate', 48000)),  # Discord standard: 48kHz
            noise_scale=float(self.config.get('noise_scale', 0.667)),
            noise_w=float(self.config.get('noise_w', 0.8)),
            length_scale=float(self.config.get('length_scale', 1.0)),
        )
        self.synthesizer = StyleBertVITS2Synthesizer(tts_cfg)
        self.api_url = self.config.get('api_server_url')  # optional legacy
        self.api_key = self.config.get('api_key')

        self.default_model_id = self.config.get('default_model_id', 0)
        self.default_style = self.config.get('default_style', 'Neutral')
        self.default_style_weight = self.config.get('default_style_weight', 5.0)
        self.default_speed = self.config.get('default_speed', 1.0)
        self.default_volume = self.config.get('default_volume', 1.0)

        headers = {}
        if self.api_key:
            headers["X-API-KEY"] = self.api_key

        self.session = aiohttp.ClientSession(headers=headers)
        self.exception_handler = TTSCogExceptionHandler()

        self.tts_locks: Dict[int, asyncio.Lock] = {}

        self.available_models: List[Dict] = []
        self.models_loaded: bool = False

        # SettingsDB（TTS / 読み上げ設定）
        self.settings_db = resolve_settings_db(bot)
        self.channel_settings: Dict[int, Dict] = {}
        # ロード成功時のみ True。失敗時の空 dict 保存で DB を消さない
        self._channel_settings_loaded_ok = False
        self._load_settings()

        self.speech_settings: Dict[str, Dict[str, Any]] = {}
        # 読み上げ設定のロード成否（unload 全置換のガード）
        self._speech_settings_loaded_ok = False
        self._load_speech_settings()

        self.speech_dictionary: Dict[str, Dict[str, str]] = {}
        # 辞書ロード成否（失敗時の空保存を禁止）
        self._dictionary_loaded_ok = False
        self._load_dictionary()

        logging.getLogger(__name__).info(
            "TTSCog loaded (Internal Style-Bert-VITS2 wrapper, AudioMixer enabled)"
        )

    def _bot_id_key(self) -> str:
        """自 Bot の bot_id を返す。"""
        # Momoka.bot_id があればそれを使う
        return str(getattr(self.bot, "bot_id", None) or "plana")

    def _partner_voice_block_message(
        self,
        guild_id: int,
        channel_id: int,
    ) -> Optional[str]:
        """相方が同 VC にいる場合の拒否メッセージ。いなければ None。"""
        # 相方が同 channel に接続中か判定する
        if not registry.partner_in_voice_channel(self._bot_id_key(), guild_id, channel_id):
            # ブロック不要
            return None
        # 相方表示名を解決する
        partner_name = registry.display_name(registry.partner_id(self._bot_id_key()))
        # MusicCog の config 文言を再利用する
        music_cog = self.bot.get_cog(_MUSIC_COG_NAME)
        # MusicCog があればメッセージキーから組み立てる
        if music_cog and hasattr(music_cog, "exception_handler"):
            # music_config の partner_already_in_voice を使う
            return music_cog.exception_handler.get_message(
                "partner_already_in_voice",
                partner_name=partner_name,
            )
        # MusicCog 未ロード時の英語フォールバック
        return (
            f"❌ **{partner_name}** is already connected to this voice channel. "
            "PLANA and ARONA cannot join the same VC at once."
        )

    async def cog_load(self):
        logging.getLogger(__name__).info("TTSCog loaded. Preparing internal synthesizer...")
        await self.fetch_available_models()  # still useful for UI and IDs

    async def cog_unload(self):
        """Cogのアンロード時にリソースをクリーンアップ"""
        # ロード成功時のみ保存し、失敗時の空データで DB を消さない
        self._save_settings()
        # 読み上げも同様にガード付き保存
        self._save_speech_settings()
        # 辞書も同様にガード付き保存
        self._save_dictionary()
        
        # TTSモデルをアンロードしてVRAMを解放
        try:
            self.synthesizer.unload_model()
        except Exception as e:  # noqa: BLE001
            logging.getLogger(__name__).warning("[TTSCog] モデルアンロードエラー: %s", e)
        
        # aiohttpセッションのクローズ
        if self.session and not self.session.closed:
            await self.session.close()
        
        # ロック辞書のクリア
        self.tts_locks.clear()
        
        # ガベージコレクションを強制実行
        gc.collect()
        
        logging.getLogger(__name__).info("TTSCog unloaded and session closed.")

    def _load_settings(self):
        # 毎回ロード前に未成功扱いへ戻す
        self._channel_settings_loaded_ok = False
        try:
            # SettingsDB からチャンネル別 TTS 設定を読む
            data = self.settings_db.load(NS_TTS_SETTINGS)
            # 未設定（空テーブル）は正当な空データとして受理する
            if data is None:
                # メモリは空でよい
                self.channel_settings = {}
                # 空でもロード自体は成功扱い
                self._channel_settings_loaded_ok = True
            elif isinstance(data, dict):
                self.channel_settings = {int(k): v for k, v in data.items()}
                # 形状正常なので保存を許可する
                self._channel_settings_loaded_ok = True
                logging.getLogger(__name__).info(
                    "[TTSCog] モデル設定を読み込みました: %dチャンネル", len(self.channel_settings)
                )
            else:
                # 不正形状はメモリを空にしつつ保存禁止のまま
                self.channel_settings = {}
                logging.getLogger(__name__).warning(
                    "[TTSCog] モデル設定の形状が不正なため保存を無効化します"
                )
        except Exception as e:
            logging.getLogger(__name__).error("[TTSCog] モデル設定読み込みエラー: %s", e)
            # 失敗時は空メモリのまま loaded_ok=False を維持する
            self.channel_settings = {}

    def _save_settings(self):
        # ロード失敗時は全置換で DB を消さない
        if not self._channel_settings_loaded_ok:
            # スキップ理由を残す
            logging.getLogger(__name__).warning(
                "[TTSCog] モデル設定が未ロードのため保存をスキップします"
            )
            # 書き込まない
            return
        try:
            # キーを文字列にして保存する
            data = {str(k): v for k, v in self.channel_settings.items()}
            self.settings_db.save(NS_TTS_SETTINGS, data)
        except Exception as e:
            logging.getLogger(__name__).error("[TTSCog] モデル設定保存エラー: %s", e)

    def _load_speech_settings(self):
        # 毎回ロード前に未成功扱いへ戻す
        self._speech_settings_loaded_ok = False
        try:
            # SettingsDB からギルド別読み上げ設定を読む
            data = self.settings_db.load(NS_SPEECH_SETTINGS)
            # 未設定は空 dict として成功扱い
            if data is None:
                # メモリを空にする
                self.speech_settings = {}
                # ロード成功として保存を許可する
                self._speech_settings_loaded_ok = True
            elif isinstance(data, dict):
                self.speech_settings = data
                # 正常読み込み完了
                self._speech_settings_loaded_ok = True
                logging.getLogger(__name__).info(
                    "[TTSCog] 読み上げ設定を読み込みました: %dギルド", len(self.speech_settings)
                )
            else:
                # 不正形状は保存禁止のまま空にする
                self.speech_settings = {}
                logging.getLogger(__name__).warning(
                    "[TTSCog] 読み上げ設定の形状が不正なため保存を無効化します"
                )
        except Exception as e:
            logging.getLogger(__name__).error("[TTSCog] 読み上げ設定読み込みエラー: %s", e)
            # 失敗時は空メモリのまま loaded_ok=False
            self.speech_settings = {}

    def _save_speech_settings(self, guild_id: Optional[int] = None):
        # ロード失敗時は DB を触らない
        if not self._speech_settings_loaded_ok:
            # スキップを明示する
            logging.getLogger(__name__).warning(
                "[TTSCog] 読み上げ設定が未ロードのため保存をスキップします"
            )
            # 終了
            return
        try:
            # 単一ギルド更新は他ギルドを巻き込む全置換を避ける
            if guild_id is not None:
                # 永続化キーは文字列ギルド ID
                guild_key = str(guild_id)
                # 対象ギルドの現在メモリ内容だけを渡す
                guild_data = self.speech_settings.get(guild_key, {})
                # save_guild で当該ギルドのみ upsert する
                self.settings_db.save_guild(
                    NS_SPEECH_SETTINGS, guild_id, guild_data
                )
            else:
                # unload 等の全体保存パス
                self.settings_db.save(NS_SPEECH_SETTINGS, self.speech_settings)
        except Exception as e:
            logging.getLogger(__name__).error("[TTSCog] 読み上げ設定保存エラー: %s", e)

    def _get_guild_speech_settings(self, guild_id: int) -> Dict[str, Any]:
        guild_id_str = str(guild_id)
        if guild_id_str not in self.speech_settings:
            self.speech_settings[guild_id_str] = {
                "speech_channel_id": None,
                "auto_join_users": [],
                "enable_notifications": True,
                "volume": self.default_volume
            }
        self.speech_settings[guild_id_str].setdefault("enable_notifications", True)
        self.speech_settings[guild_id_str].setdefault("volume", self.default_volume)
        return self.speech_settings[guild_id_str]

    def _get_channel_settings(self, channel_id: int) -> Dict:
        if channel_id not in self.channel_settings:
            return {
                "model_id": self.default_model_id,
                "style": self.default_style,
                "style_weight": self.default_style_weight,
                "speed": self.default_speed
            }
        return self.channel_settings[channel_id]

    def _set_channel_settings(self, channel_id: int, settings: Dict):
        self.channel_settings[channel_id] = settings
        self._save_settings()

    async def fetch_available_models(self) -> bool:
        # レガシー API URL が未設定なら無効な HTTP リクエストを行わない
        if not self.api_url:
            return False
        try:
            async with self.session.get(f"{self.api_url}/models/info") as response:
                if response.status == 200:
                    data = await response.json()
                    self.available_models = data.get('models', []) if isinstance(data, dict) else data
                    self.models_loaded = True
                    print(f"✓ [TTSCog] {len(self.available_models)}個のモデルを検出")
                    return True
                return False
        except aiohttp.ClientConnectorError:
            return False
        except Exception:
            return False

    def get_model_name(self, model_id: int) -> str:
        for model in self.available_models:
            if isinstance(model, dict) and model.get('id') == model_id:
                return model.get('name', f"Model {model_id}")
        return f"Model {model_id}"

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or message.embeds:
            return

        guild_settings = self._get_guild_speech_settings(message.guild.id)
        if message.channel.id != guild_settings.get("speech_channel_id"):
            return

        voice_client = message.guild.voice_client
        if not voice_client or not voice_client.is_connected() or not message.clean_content:
            return

        lock = self._get_tts_lock(message.guild.id)
        if lock.locked():
            return

        async with lock:
            channel_settings = self._get_channel_settings(voice_client.channel.id)
            await self._handle_say_logic(
                message.guild, message.clean_content,
                channel_settings["model_id"], channel_settings["style"],
                channel_settings["style_weight"], channel_settings["speed"],
                guild_settings.get("volume", self.default_volume)
            )




    def _music_occupies_voice(self, guild_id: int) -> bool:
        """MusicCog が再生中・キューあり・ミキサーに music を持つ場合は True。"""
        # MusicCog を取得する
        music_cog: Optional[MusicCog] = self.bot.get_cog(_MUSIC_COG_NAME)
        # 未ロードなら音楽占有なし
        if music_cog is None:
            # 占有していない
            return False
        # 既存ギルド状態だけを見る（新規作成しない）
        music_state = None
        # get_existing_guild_state があればそれを使う
        if hasattr(music_cog, "get_existing_guild_state"):
            # 削除済みを復活させない API
            music_state = music_cog.get_existing_guild_state(guild_id)
        # フォールバック
        elif hasattr(music_cog, "_get_guild_state"):
            # 旧 API
            music_state = music_cog._get_guild_state(guild_id)
        # 状態が無ければ占有なし
        if music_state is None:
            # 音楽なし
            return False
        # 再生中なら占有
        if getattr(music_state, "is_playing", False):
            # 再生中
            return True
        # キューに曲が残っていれば占有
        queue = getattr(music_state, "queue", None)
        if queue is not None and not queue.empty():
            # キューあり
            return True
        # ミキサーに music ソースがあれば占有
        mixer = getattr(music_state, "mixer", None)
        if mixer is not None and hasattr(mixer, "get_source") and mixer.get_source("music") is not None:
            # music ソース残存
            return True
        # いずれも無ければ占有なし
        return False

    async def _remove_tts_sources_only(self, guild_id: int) -> None:
        """ミキサー上の TTS ソースだけを除去する（music は残す）。"""
        # MusicCog を取得する
        music_cog: Optional[MusicCog] = self.bot.get_cog(_MUSIC_COG_NAME)
        # 未ロードなら何もしない
        if music_cog is None:
            # 早期リターン
            return
        # ギルド状態を取得する
        music_state = None
        # 既存状態 API を優先する
        if hasattr(music_cog, "get_existing_guild_state"):
            # 既存のみ
            music_state = music_cog.get_existing_guild_state(guild_id)
        elif hasattr(music_cog, "_get_guild_state"):
            # フォールバック
            music_state = music_cog._get_guild_state(guild_id)
        # ミキサーが無ければ終了
        if not music_state or not getattr(music_state, "mixer", None):
            # 除去対象なし
            return
        # TTS ソース名を列挙する（ミキサーのロック付き API を使う）
        tts_sources = [
            name
            for name in music_state.mixer.get_source_names()
            if name.startswith("tts_")
        ]
        # 各 TTS ソースを除去する
        for name in tts_sources:
            # ソースを削除する
            await music_state.mixer.remove_source(name)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.id == self.bot.user.id:
            return

        guild = member.guild
        guild_settings = self._get_guild_speech_settings(guild.id)
        voice_client = guild.voice_client

        # 自動参加: 登録ユーザーがVCに入室したらBotも自動接続
        if member.id in guild_settings.get("auto_join_users", []) and not before.channel and after.channel:
            if not voice_client or not voice_client.is_connected():
                # 相方が同一 VC にいる場合は自動参加しない
                block_msg = self._partner_voice_block_message(guild.id, after.channel.id)
                # ブロック時はログだけ残して接続しない
                if block_msg:
                    # 同居拒否を記録する
                    logging.getLogger(__name__).info(
                        "[TTSCog] autojoin skipped guild=%s channel=%s: partner already in VC",
                        guild.id,
                        after.channel.id,
                    )
                else:
                    try:
                        # 接続時は自己deafせず、直後にサーバー側スピーカーミュートへ
                        await after.channel.connect(self_deaf=False)
                        # MusicCog があればサーバー側 deafen 処理を再利用する
                        music_cog = self.bot.get_cog(_MUSIC_COG_NAME)
                        # MusicCog のヘルパーが使える場合は緑アイコン deafen を適用
                        if music_cog and hasattr(music_cog, "_apply_server_deafen"):
                            # サーバー側スピーカーミュートを適用
                            await music_cog._apply_server_deafen(guild)
                    except Exception as e:
                        logging.getLogger(__name__).error("[TTSCog] 自動参加エラー: %s", e)

        if not voice_client:
            return

        # 自動退出: BotのいるVCに人間がいなくなったら切断（Bot同士残留を防ぐ）
        if before.channel == voice_client.channel and not any(
            m for m in voice_client.channel.members if not m.bot
        ):
            # 音楽が占有中なら VC は維持し、読み上げ設定と TTS だけ消す
            if self._music_occupies_voice(guild.id):
                # TTS ソースのみ除去する
                await self._remove_tts_sources_only(guild.id)
                # 読み上げ対象チャンネル設定をクリアする
                guild_settings["speech_channel_id"] = None
                # 当該ギルドだけを永続化する
                self._save_speech_settings(guild.id)
                # 切断せず終了する
                return
            # VCから切断する
            await voice_client.disconnect()
            # 読み上げ対象チャンネル設定をクリアする
            guild_settings["speech_channel_id"] = None
            # 当該ギルドだけを永続化する
            self._save_speech_settings(guild.id)
            return

    tts_group = app_commands.Group(name="tts", description="TTS-related commands.")

    @tts_group.command(name="volume", description="Set TTS volume (0-200%).")
    @app_commands.describe(volume="Volume (integer from 0 to 200).")
    async def tts_volume(self, interaction: discord.Interaction, volume: app_commands.Range[int, 0, 200]):
        guild_settings = self._get_guild_speech_settings(interaction.guild.id)
        float_volume = volume / 100.0
        guild_settings['volume'] = float_volume
        # 当該ギルドの音量だけを保存する
        self._save_speech_settings(interaction.guild.id)

        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.is_playing():
            music_cog: Optional[MusicCog] = self.bot.get_cog(_MUSIC_COG_NAME)
            music_state = music_cog._get_guild_state(interaction.guild.id) if music_cog else None
            if music_state and music_state.mixer:
                tts_sources = [
                    name
                    for name in music_state.mixer.get_source_names()
                    if name.startswith("tts_")
                ]
                for name in tts_sources:
                    await music_state.mixer.set_volume(name, float_volume)
            elif isinstance(voice_client.source, discord.PCMVolumeTransformer):
                voice_client.source.volume = float_volume

        await interaction.response.send_message(f"🔊 TTSの音量を **{volume}%** に設定しました。")

    speech_group = app_commands.Group(name="speech", description="Text channel read-aloud commands.")

    @speech_group.command(name="enable", description="Enable message read-aloud for this channel.")
    async def enable_speech(self, interaction: discord.Interaction):
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.response.send_message("❌ ボイスチャンネルに接続してから実行してください。", ephemeral=True)
        
        vc = interaction.user.voice.channel
        # 相方が同一 VC にいる場合は接続・移動を拒否する
        block_msg = self._partner_voice_block_message(interaction.guild.id, vc.id)
        # 拒否メッセージがあれば ephemeral で返して終了する
        if block_msg:
            # ユーザーへ同居不可を伝える
            return await interaction.response.send_message(block_msg, ephemeral=True)
        try:
            if interaction.guild.voice_client:
                # 既存接続をユーザーのVCへ移動する
                await interaction.guild.voice_client.move_to(vc)
            else:
                # 新規接続（自己deafはせずサーバー側 mute を後で適用）
                await vc.connect(self_deaf=False)
            # MusicCog のサーバー側スピーカーミュート処理を再利用する
            music_cog = self.bot.get_cog(_MUSIC_COG_NAME)
            # ヘルパーが存在する場合のみ適用する
            if music_cog and hasattr(music_cog, "_apply_server_deafen"):
                # 緑アイコンのサーバー側スピーカーミュートを適用
                await music_cog._apply_server_deafen(interaction.guild)
        except Exception as e:
            return await interaction.response.send_message(f"❌ 接続失敗: `{e}`", ephemeral=True)

        guild_settings = self._get_guild_speech_settings(interaction.guild.id)
        guild_settings["speech_channel_id"] = interaction.channel.id
        # 当該ギルドの読み上げチャンネルだけを保存する
        self._save_speech_settings(interaction.guild.id)
        embed = discord.Embed(title="🔊 VC読み上げ開始", description=f"対象: {interaction.channel.mention}, {vc.mention}", color=discord.Color.green())
        await interaction.response.send_message(embed=embed)

    @speech_group.command(name="disable", description="Disable message read-aloud.")
    async def disable_speech(self, interaction: discord.Interaction):
        guild_settings = self._get_guild_speech_settings(interaction.guild.id)
        if guild_settings.get("speech_channel_id") is None:
            return await interaction.response.send_message("ℹ️ 読み上げは無効です。", ephemeral=True)
        
        guild_settings["speech_channel_id"] = None
        # 当該ギルドの無効化だけを保存する
        self._save_speech_settings(interaction.guild.id)
        # 音楽が占有中なら VC 切断せず TTS ソースだけ除去する
        if self._music_occupies_voice(interaction.guild.id):
            # ミキサー上の TTS のみ消す
            await self._remove_tts_sources_only(interaction.guild.id)
        elif interaction.guild.voice_client:
            # 音楽が無ければ従来どおり切断する
            await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("✅ 読み上げを無効にしました。")

    @speech_group.command(name="skip", description="Skip the current read-aloud.")
    async def skip_speech(self, interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        if not voice_client:
            return await interaction.response.send_message("❌ BotがVCにいません。", ephemeral=True)

        skipped = False
        music_cog: Optional[MusicCog] = self.bot.get_cog(_MUSIC_COG_NAME)
        music_state = music_cog._get_guild_state(interaction.guild.id) if music_cog else None
        if music_state and music_state.mixer:
            tts_sources = [
                name
                for name in music_state.mixer.get_source_names()
                if name.startswith("tts_")
            ]
            if tts_sources:
                for name in tts_sources: await music_state.mixer.remove_source(name)
                skipped = True

        if not skipped and voice_client.is_playing() and isinstance(voice_client.source, (TTSAudioSource, discord.PCMVolumeTransformer)):
            voice_client.stop()
            skipped = True

        await interaction.response.send_message("✅ スキップしました。" if skipped else "❌ スキップ対象がありません。", ephemeral=not skipped)

    autojoin_group = app_commands.Group(name="autojoin", description="Auto-join voice channel commands.")

    @autojoin_group.command(name="enable", description="Auto-join VC when you join.")
    async def enable_auto_join(self, interaction: discord.Interaction):
        guild_settings = self._get_guild_speech_settings(interaction.guild.id)
        auto_join_users = guild_settings.setdefault("auto_join_users", [])
        if interaction.user.id in auto_join_users:
            return await interaction.response.send_message("ℹ️ 自動参加は既に有効です。", ephemeral=True)
        auto_join_users.append(interaction.user.id)
        # 当該ギルドの自動参加一覧だけを保存する
        self._save_speech_settings(interaction.guild.id)
        await interaction.response.send_message("✅ 自動参加を有効にしました。")

    @autojoin_group.command(name="disable", description="Disable bot auto-join.")
    async def disable_auto_join(self, interaction: discord.Interaction):
        guild_settings = self._get_guild_speech_settings(interaction.guild.id)
        auto_join_users = guild_settings.get("auto_join_users", [])
        if interaction.user.id not in auto_join_users:
            return await interaction.response.send_message("ℹ️ 自動参加は設定されていません。", ephemeral=True)
        auto_join_users.remove(interaction.user.id)
        # 当該ギルドの自動参加解除だけを保存する
        self._save_speech_settings(interaction.guild.id)
        await interaction.response.send_message("✅ 自動参加を解除しました。")

    @app_commands.command(name="say", description="Read text aloud with TTS.")
    @app_commands.describe(text="Text to read aloud.", model_id="Model ID.", style="Style name.", style_weight="Style weight.", speed="Speech speed.")
    async def say(self, interaction: discord.Interaction, text: str, model_id: Optional[int] = None, style: Optional[str] = None, style_weight: Optional[float] = None, speed: Optional[float] = None):
        if not self.config.get('enable_say_command', True):
            return await interaction.response.send_message("読み上げコマンドは無効です。", ephemeral=True)
        if not interaction.guild.voice_client:
            return await self.exception_handler.send_message(interaction, "bot_not_in_voice", ephemeral=True)
        
        lock = self._get_tts_lock(interaction.guild.id)
        if lock.locked():
            return await self.exception_handler.send_message(interaction, "tts_in_progress", ephemeral=True)

        channel_settings = self._get_channel_settings(interaction.guild.voice_client.channel.id)
        guild_settings = self._get_guild_speech_settings(interaction.guild.id)

        final_model_id = model_id if model_id is not None else channel_settings["model_id"]
        final_style = style if style is not None else channel_settings["style"]
        final_style_weight = style_weight if style_weight is not None else channel_settings["style_weight"]
        final_speed = speed if speed is not None else channel_settings["speed"]
        final_volume = guild_settings.get("volume", self.default_volume)

        await interaction.response.defer()
        async with lock:
            success = await self._handle_say_logic(interaction.guild, text, final_model_id, final_style, final_style_weight, final_speed, final_volume, interaction)
            if success:
                await interaction.followup.send(f"🔊 読み上げ中: `{text}`", ephemeral=True)

    def _get_tts_lock(self, guild_id: int) -> asyncio.Lock:
        return self.tts_locks.setdefault(guild_id, asyncio.Lock())

    def _load_dictionary(self):
        # 毎回ロード前に未成功扱いへ戻す
        self._dictionary_loaded_ok = False
        try:
            # SettingsDB から読み上げ辞書を取る
            dictionary_data = self.settings_db.load(NS_SPEECH_DICTIONARY)
            # 未設定は空として成功扱い
            if dictionary_data is None:
                # メモリを空にする
                self.speech_dictionary = {}
                # 空でもロード成功とする
                self._dictionary_loaded_ok = True
                # 早期リターン
                return
            # ギルド ID ごとの辞書だけを受け入れ、旧来のフラット形式は破棄する
            if self._is_guild_scoped_dictionary(dictionary_data):
                self.speech_dictionary = dictionary_data
                # 正常読み込み完了
                self._dictionary_loaded_ok = True
                logging.getLogger(__name__).info(
                    "[TTSCog] 読み上げ辞書を読み込みました: %dギルド",
                    len(self.speech_dictionary),
                )
            else:
                # 不正形式はメモリ破棄しつつ DB 上書きは禁止
                self.speech_dictionary = {}
                logging.getLogger(__name__).warning(
                    "[TTSCog] 旧形式または不正形式の読み上げ辞書を破棄しました（保存無効）。"
                )
        except Exception as e:
            logging.getLogger(__name__).error("[TTSCog] 辞書読み込みエラー: %s", e)
            # 失敗時は空メモリのまま loaded_ok=False
            self.speech_dictionary = {}

    def _save_dictionary(self, guild_id: Optional[int] = None):
        # ロード失敗時は DB を触らない
        if not self._dictionary_loaded_ok:
            # スキップを明示する
            logging.getLogger(__name__).warning(
                "[TTSCog] 読み上げ辞書が未ロードのため保存をスキップします"
            )
            # 終了
            return
        try:
            # 単一ギルド更新は他ギルドを巻き込む全置換を避ける
            if guild_id is not None:
                # 永続化キーは文字列ギルド ID
                guild_key = str(guild_id)
                # 対象ギルドの辞書だけを渡す
                guild_data = self.speech_dictionary.get(guild_key, {})
                # save_guild で当該ギルドのみ upsert する
                self.settings_db.save_guild(
                    NS_SPEECH_DICTIONARY, guild_id, guild_data
                )
            else:
                # unload 等の全体保存パス
                self.settings_db.save(NS_SPEECH_DICTIONARY, self.speech_dictionary)
        except Exception as e:
            logging.getLogger(__name__).error("[TTSCog] 辞書保存エラー: %s", e)

    @staticmethod
    def _is_guild_scoped_dictionary(dictionary_data: Any) -> bool:
        """辞書データがギルド単位の JSON 形式か判定する。"""
        # 最上位はギルド ID をキーにした辞書でなければならない
        if not isinstance(dictionary_data, dict):
            return False
        # 全ギルドの単語・読み方が文字列ペアか確認する
        return all(
            str(guild_id).isdigit()
            and isinstance(entries, dict)
            and all(
                isinstance(word, str) and isinstance(reading, str)
                for word, reading in entries.items()
            )
            for guild_id, entries in dictionary_data.items()
        )

    def _get_guild_dictionary(self, guild_id: int) -> Dict[str, str]:
        """指定ギルド専用の読み上げ辞書を返す。"""
        # JSON のキー形式に合わせてギルド ID を文字列化する
        guild_id_key = str(guild_id)
        # 未登録ギルドには空の辞書を作成して返す
        return self.speech_dictionary.setdefault(guild_id_key, {})

    async def _get_interaction_dictionary(
        self,
        interaction: discord.Interaction,
    ) -> Optional[Dict[str, str]]:
        """辞書コマンドの実行ギルドを検証して辞書を返す。"""
        # DM にはギルド固有の辞書がないため実行を拒否する
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ 読み上げ辞書コマンドはサーバー内でのみ使用できます。",
                ephemeral=True,
            )
            return None
        # 実行元ギルドだけの辞書を返して他ギルドの辞書を分離する
        return self._get_guild_dictionary(interaction.guild.id)

    def _apply_dictionary(self, guild_id: int, text: str) -> str:
        """指定ギルドの辞書を読み上げテキストへ適用する。"""
        # 現在のギルドに登録された単語だけを置換対象にする
        guild_dictionary = self._get_guild_dictionary(guild_id)
        if not guild_dictionary:
            return text
        # 長い単語から置換して短い単語による部分一致を防ぐ
        sorted_words = sorted(guild_dictionary.keys(), key=len, reverse=True)
        for word in sorted_words:
            # 登録済みの読み方へ単語を置換する
            text = text.replace(word, guild_dictionary[word])
        return text

    dictionary_group = app_commands.Group(name="dictionary", description="Manage the read-aloud dictionary.")

    @dictionary_group.command(name="add", description="Add a word to the read-aloud dictionary.")
    @app_commands.describe(word="Word to register.", reading="How to read it.")
    async def add_dictionary(self, interaction: discord.Interaction, word: str, reading: str):
        dictionary = await self._get_interaction_dictionary(interaction)
        if dictionary is None:
            return
        is_update = word in dictionary
        old_reading = dictionary.get(word)
        dictionary[word] = reading
        # 当該ギルドの辞書だけを保存する
        self._save_dictionary(interaction.guild.id)
        
        embed = discord.Embed(title=f"📖 辞書を{'更新' if is_update else '追加'}しました", color=discord.Color.blue() if is_update else discord.Color.green())
        embed.add_field(name="単語", value=f"`{word}`", inline=False)
        if is_update: embed.add_field(name="変更前", value=f"`{old_reading}`", inline=True)
        embed.add_field(name="読み方", value=f"`{reading}`", inline=True)
        await interaction.response.send_message(embed=embed)

    @dictionary_group.command(name="remove", description="Remove a word from the read-aloud dictionary.")
    @app_commands.describe(word="Word to remove.")
    async def remove_dictionary(self, interaction: discord.Interaction, word: str):
        dictionary = await self._get_interaction_dictionary(interaction)
        if dictionary is None:
            return
        if word not in dictionary:
            return await interaction.response.send_message(f"❌ `{word}` は辞書にありません。", ephemeral=True)
        
        reading = dictionary.pop(word)
        # 当該ギルドの辞書削除だけを保存する
        self._save_dictionary(interaction.guild.id)
        embed = discord.Embed(title="📖 辞書から削除しました", color=discord.Color.orange())
        embed.add_field(name="単語", value=f"`{word}`", inline=True).add_field(name="読み方", value=f"`{reading}`", inline=True)
        await interaction.response.send_message(embed=embed)

    @dictionary_group.command(name="list", description="List registered dictionary entries.")
    async def list_dictionary(self, interaction: discord.Interaction):
        dictionary = await self._get_interaction_dictionary(interaction)
        if dictionary is None:
            return
        if not dictionary:
            return await interaction.response.send_message("📖 辞書は空です。", ephemeral=True)
        
        # Simple list for now, pagination can be re-added if needed
        description = "\n".join(
            f"`{word}` → `{reading}`"
            for word, reading in sorted(dictionary.items())
        )
        embed = discord.Embed(title="📖 読み上げ辞書", description=description, color=discord.Color.blue())
        await interaction.response.send_message(embed=embed)

    @dictionary_group.command(name="search", description="Search the dictionary for a word.")
    @app_commands.describe(query="Word to search (partial match).")
    async def search_dictionary(self, interaction: discord.Interaction, query: str):
        dictionary = await self._get_interaction_dictionary(interaction)
        if dictionary is None:
            return
        results = {
            word: reading
            for word, reading in dictionary.items()
            if query.lower() in word.lower()
        }
        if not results:
            return await interaction.response.send_message(f"❌ `{query}` に一致する単語は見つかりませんでした。", ephemeral=True)

        description = "\n".join(f"`{word}` → `{reading}`" for word, reading in sorted(results.items())[:25])
        embed = discord.Embed(title=f"🔍 検索結果: {query}", description=description, color=discord.Color.blue())
        await interaction.response.send_message(embed=embed)

    async def _handle_say_logic(self, guild: discord.Guild, text: str, model_id: int, style: str, style_weight: float, speed: float, volume: float, interaction: Optional[discord.Interaction] = None) -> bool:
        """TTS音声の再生を制御するメインロジック。音楽再生中はミキサーでオーバーレイする。"""
        voice_client = guild.voice_client
        # ボイス接続がなければ再生不可
        if not voice_client:
            return False

        # テキストの前処理: URLを省略し、辞書変換を適用
        processed_text = re.sub(r'https?://[\S]+', ' URL省略 ', text)
        converted_text = self._apply_dictionary(guild.id, processed_text)
        # 200文字以上は切り詰め
        if len(converted_text) > 200:
            converted_text = converted_text[:200] + " 以下省略"

        # MusicCogの状態を確認
        music_cog: Optional[MusicCog] = self.bot.get_cog(_MUSIC_COG_NAME)
        music_state = music_cog._get_guild_state(guild.id) if music_cog else None

        # 音楽が再生中（ミキサーあり & is_playing）ならミキサーでオーバーレイ
        if music_state and music_state.mixer and music_state.is_playing:
            return await self._overlay_tts_with_mixer(guild, converted_text, model_id, style, style_weight, speed, volume, interaction)

        # ミキサーが存在するがソースが残っている場合（TTS等）もミキサーを使う
        if music_state and music_state.mixer and music_state.mixer.has_sources():
            return await self._overlay_tts_with_mixer(guild, converted_text, model_id, style, style_weight, speed, volume, interaction)

        # それ以外は直接再生（voice_clientから直接play）
        return await self._play_tts_directly(guild, converted_text, model_id, style, style_weight, speed, volume, interaction)

    async def _api_call_to_audio_data(self, text: str, model_id: int, style: str, style_weight: float, speed: float) -> Optional[bytes]:
        # 内製シンセサイザーを優先。失敗時はレガシーHTTP APIにフォールバック
        try:
            # synthesize_to_wav 内で未ロード時は自動ロードされる
            wav = self.synthesizer.synthesize_to_wav(
                text=text,
                style=style,
                style_weight=style_weight,
                speed=speed,
                noise_scale=self.config.get('noise_scale', 0.667),
                noise_w=self.config.get('noise_w', 0.8),
                length_scale=self.config.get('length_scale', 1.0),
            )
            # 合成完了後、モデルをアンロードしてVRAMを解放
            self.synthesizer.unload_model()
            return wav
        except Exception as e:
            logging.getLogger(__name__).error("[TTSCog] 内製TTS処理エラー: %s", e)
            # エラー時もVRAM解放を試みる
            try:
                self.synthesizer.unload_model()
            except Exception:  # noqa: BLE001
                pass

        if not self.api_url:
            return None

        endpoint = f"{self.api_url}/voice"
        params = {"text": text, "model_id": model_id, "style": style, "style_weight": style_weight, "speed": speed, "encoding": "wav"}
        try:
            # タイムアウトを設定してメモリリークを防止
            timeout = aiohttp.ClientTimeout(total=30)
            async with self.session.post(endpoint, params=params, timeout=timeout) as response:
                if response.status == 200:
                    audio_data = await response.read()
                    return audio_data
                logging.getLogger(__name__).error(
                    "[TTSCog] 音声生成APIエラー: %s %s", response.status, await response.text()
                )
                return None
        except asyncio.TimeoutError:
            logging.getLogger(__name__).error("[TTSCog] 音声生成APIタイムアウト")
            return None
        except Exception as e:
            logging.getLogger(__name__).error("[TTSCog] 音声生成APIリクエストエラー: %s", e)
            return None

    async def _overlay_tts_with_mixer(self, guild: discord.Guild, text: str, model_id: int, style: str, style_weight: float, speed: float, volume: float, interaction: Optional[discord.Interaction] = None) -> bool:
        music_cog: Optional[MusicCog] = self.bot.get_cog(_MUSIC_COG_NAME)
        music_state = music_cog._get_guild_state(guild.id)
        
        wav_data = await self._api_call_to_audio_data(text, model_id, style, style_weight, speed)
        if not wav_data:
            if interaction: await interaction.followup.send("❌ 音声生成に失敗しました。", ephemeral=True)
            return False

        try:
            # BytesIOをwith文で管理してメモリリークを防止
            audio_buffer = io.BytesIO(wav_data)
            tts_source = TTSAudioSource(audio_buffer, text=text, guild_id=guild.id, pipe=True)
            source_name = f"tts_{int(time.time() * 1000)}"
            await music_state.mixer.add_source(source_name, tts_source, volume=volume)
            # wav_dataの参照を明示的に削除
            del wav_data
            return True
        except Exception as e:
            logging.getLogger(__name__).error(f"[TTSCog] ミキサーへのTTS追加エラー: {e}")
            if interaction: await interaction.followup.send("❌ 音声の再生に失敗しました。", ephemeral=True)
            return False

    async def _play_tts_directly(self, guild: discord.Guild, text: str, model_id: int, style: str, style_weight: float, speed: float, volume: float, interaction: Optional[discord.Interaction] = None) -> bool:
        """TTS音声をvoice_clientから直接再生する（音楽非再生時）"""
        voice_client = guild.voice_client
        # ボイス接続状態を確認
        if not voice_client or not voice_client.is_connected():
            return False
        # 既に別の音声が再生中の場合はスキップ
        # ※ _handle_say_logicでミキサー再生中はオーバーレイに回されるため、
        #   ここに来る場合はミキサー不在 = voice_client.is_playing()で正しく判定できる
        if voice_client.is_playing():
            return False

        wav_data = await self._api_call_to_audio_data(text, model_id, style, style_weight, speed)
        if not wav_data:
            if interaction: await interaction.followup.send("❌ 音声生成に失敗しました。", ephemeral=True)
            return False

        # 再度接続状態を確認（音声生成中に切断された可能性）
        if not voice_client.is_connected():
            return False

        try:
            # BytesIOをメモリ効率的に管理
            audio_buffer = io.BytesIO(wav_data)
            source = TTSAudioSource(audio_buffer, text=text, guild_id=guild.id, pipe=True)
            volume_source = discord.PCMVolumeTransformer(source, volume=volume)
            
            # 再生完了後のクリーンアップコールバック
            def after_playback(error):
                if error:
                    logging.getLogger(__name__).warning(f"[TTSCog] 再生エラー: {error}")
                # メモリ解放を促進
                gc.collect()
            
            voice_client.play(volume_source, after=after_playback)
            # wav_dataの参照を明示的に削除
            del wav_data
            return True
        except discord.errors.ClientException as e:
            logging.getLogger(__name__).warning(f"[TTSCog] 再生エラー: {e}")
            return False
        except Exception as e:
            logging.getLogger(__name__).error(f"[TTSCog] TTS再生中の予期しないエラー: {e}")
            return False


async def setup(bot: commands.Bot):
    # TTSセクションが存在しない場合はロードをスキップ
    if 'tts' not in bot.config:
        logging.getLogger("MOMOKA.tts").warning(
            "'tts' section not found in configs/tts_config.yaml (merged config). "
            "TTSCog will not be loaded."
        )
        return
    # enabled フラグが false の場合はCog全体をロードしない（VRAM節約）
    tts_config = bot.config.get('tts', {})
    if not tts_config.get('enabled', True):
        logging.getLogger("MOMOKA.tts").info(
            "TTSCog is disabled in configs/tts_config.yaml (tts.enabled=false). "
            "Skipping TTS model loading to conserve VRAM."
        )
        return
    if not bot.get_cog(_MUSIC_COG_NAME):
        logging.getLogger("MOMOKA.tts").warning("MusicCog is not loaded. TTSCog may not function correctly with music.")
    
    await bot.add_cog(TTSCog(bot))
