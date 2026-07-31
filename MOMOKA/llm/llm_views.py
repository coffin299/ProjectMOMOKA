"""LLM 機能が使用する Discord UI View。"""

from __future__ import annotations

import logging
from typing import Any

import discord

logger = logging.getLogger(__name__)


class ThreadCreationView(discord.ui.View):
    """会話履歴を引き継ぐスレッド作成ボタンを提供する View。"""

    def __init__(self, llm_cog: Any, original_message: discord.Message) -> None:
        # 操作可能時間を 5 分に設定して親 View を初期化する。
        super().__init__(timeout=300)
        # LLM の既存サービス群へ委譲するため Cog を保持する。
        self.llm_cog = llm_cog
        # スレッド作成元となるメッセージを保持する。
        self.original_message = original_message

    @discord.ui.button(
        label="スレッドを作成する / Create Thread",
        style=discord.ButtonStyle.primary,
        emoji="🧵",
    )
    async def create_thread(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        """スレッドを作成し、元メッセージの会話履歴を引き継ぐ。"""
        # Discord の操作期限切れを防ぐため、先に応答を遅延する。
        await interaction.response.defer(ephemeral=True)

        try:
            # ユーザー名を含むスレッドを元メッセージから作成する。
            thread = await self.original_message.create_thread(
                name=f"AI Chat - {interaction.user.display_name}",
                auto_archive_duration=60,
                reason="AI conversation thread created by user",
            )

            # スレッド作成前の返信チェーンから履歴を初期化する。
            messages = []
            try:
                # 返信チェーンの走査起点と循環検出用の状態を初期化する。
                current_msg = self.original_message
                visited_ids = set()
                message_count = 0

                # 最大 40 件まで親メッセージを遡って履歴を集める。
                while current_msg and message_count < 40:
                    # 循環参照を検出した場合は安全に走査を終了する。
                    if current_msg.id in visited_ids:
                        break
                    # 現在メッセージを訪問済みとして記録する。
                    visited_ids.add(current_msg.id)

                    # Bot 以外の発言だけをユーザー履歴として取り込む。
                    if current_msg.author != self.llm_cog.bot.user:
                        # 履歴用には現在メッセージ単体の画像だけを収集する。
                        image_contents, text_content = (
                            await self.llm_cog._prepare_multimodal_content(
                                current_msg,
                                include_reply_chain=False,
                            )
                        )
                        # Bot メンションを API 送信本文から除去する。
                        text_content = text_content.replace(
                            f"<@!{self.llm_cog.bot.user.id}>",
                            "",
                        ).replace(
                            f"<@{self.llm_cog.bot.user.id}>",
                            "",
                        ).strip()

                        # テキストまたは画像を含む発言だけを履歴に追加する。
                        if text_content or image_contents:
                            # API 互換のマルチモーダル配列を初期化する。
                            user_content_parts = []
                            # テキストがある場合だけテキストパーツを追加する。
                            if text_content:
                                # 履歴ターンには言語追従リマインダを付けない。
                                user_content_parts.append(
                                    {
                                        "type": "text",
                                        "text": (
                                            self.llm_cog._format_user_text_for_api(
                                                current_msg.created_at.astimezone(
                                                    self.llm_cog.jst,
                                                ).strftime("[%H:%M]"),
                                                text_content,
                                                mirror_language=False,
                                            )
                                        ),
                                    },
                                )
                            # 収集した画像パーツをテキストパーツの後ろへ追加する。
                            user_content_parts.extend(image_contents)
                            # ユーザーロールの履歴として登録する。
                            messages.append(
                                {
                                    "role": "user",
                                    "content": user_content_parts,
                                },
                            )
                            # 取り込んだユーザー発言数を加算する。
                            message_count += 1

                    # 返信先があれば次の親メッセージへ進む。
                    if current_msg.reference and current_msg.reference.message_id:
                        try:
                            # キャッシュ済みの親を使い、無ければ Discord から取得する。
                            current_msg = (
                                current_msg.reference.resolved
                                or await current_msg.channel.fetch_message(
                                    current_msg.reference.message_id,
                                )
                            )
                        except (discord.NotFound, discord.HTTPException):
                            # 親取得に失敗した場合は取得できた履歴だけを使う。
                            break
                    else:
                        # 返信先がなければチェーン終端として終了する。
                        break

                # 古い発言から新しい発言の順へ並べ直す。
                messages.reverse()
            except Exception as error:
                # 履歴取得失敗はスレッド作成自体を妨げないよう空履歴へ戻す。
                logger.error(
                    "Failed to collect conversation history for thread: %s",
                    error,
                    exc_info=True,
                )
                messages = []

            # 履歴がある場合は最初の LLM 応答を生成する。
            if messages:
                # スレッド用の LLM クライアントを解決する。
                llm_client = await self.llm_cog._get_llm_client_for_channel(thread.id)
                # クライアント未設定ならスレッドへ理由を通知して終了する。
                if not llm_client:
                    await thread.send(
                        "❌ LLM client is not available for this thread.\n"
                        "このスレッドではLLMクライアントが利用できません。",
                    )
                    return

                # スレッドの参加者に対応したシステムプロンプトを構築する。
                system_prompt = await self.llm_cog._prepare_system_prompt(
                    thread.id,
                    interaction.user.id,
                    interaction.user.display_name,
                )
                # システムプロンプトを先頭にした API メッセージ列を作る。
                messages_for_api = [{"role": "system", "content": system_prompt}]
                # 収集済みの会話履歴を API メッセージ列へ追加する。
                messages_for_api.extend(messages)

                # 初回生成中であることをスレッドに通知する。
                temp_message = await thread.send(
                    "⏳ Processing conversation history... / 会話履歴を処理中...",
                )
                # スレッド内での会話方法を案内する。
                await thread.send(
                    "💡 **スレッド内での会話方法 / How to chat in this thread:**\n"
                    "• Botのメッセージにリプライして会話を続けられます / "
                    "Reply to bot messages to continue chatting\n"
                    "• 画像も送信可能です / Images are also supported\n"
                    "• 会話履歴は自動的に保持されます / "
                    "Conversation history is automatically maintained",
                )
                # Cog の既存ストリーミング処理へ応答生成を委譲する。
                sent_messages, full_response_text, _used_key_index = (
                    await self.llm_cog._process_streaming_and_send_response(
                        sent_message=temp_message,
                        channel=thread,
                        user=interaction.user,
                        messages_for_api=messages_for_api,
                        llm_client=llm_client,
                    )
                )

                # 応答が完了した場合は実際に使用されたモデルを記録する。
                if sent_messages and full_response_text:
                    used_model = self.llm_cog._effective_model_label(
                        llm_client,
                        thread.id,
                    )
                    logger.info(
                        "✅ Thread conversation completed | model='%s' | "
                        "response_length=%s chars",
                        used_model,
                        len(full_response_text),
                    )

                # 重複作成を防ぐため作成済みのボタンを無効化する。
                button.disabled = True
                button.label = "✅ Thread Created / スレッド作成済み"
                # 無効化した状態を元のメッセージへ反映する。
                await interaction.edit_original_response(view=self)
            else:
                # 履歴がないスレッドでも、そのまま会話を始められることを案内する。
                await thread.send(
                    "ℹ️ No conversation history found, but you can start chatting!\n"
                    "会話履歴は見つかりませんでしたが、ここから会話を始めることができます！\n\n"
                    "💡 **スレッド内での会話方法 / How to chat in this thread:**\n"
                    "• Botのメッセージにリプライして会話を続けられます / "
                    "Reply to bot messages to continue chatting\n"
                    "• 画像も送信可能です / Images are also supported\n"
                    "• 会話履歴は自動的に保持されます / "
                    "Conversation history is automatically maintained",
                )
        except Exception as error:
            # 作成失敗を記録し、操作したユーザーだけへ失敗を通知する。
            logger.error("Failed to create thread: %s", error, exc_info=True)
            await interaction.followup.send(
                "❌ Failed to create thread.\nスレッドの作成に失敗しました。",
                ephemeral=True,
            )
