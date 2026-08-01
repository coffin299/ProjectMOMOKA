# MOMOKA/utilities/latex_command_cog.py
"""LaTeX 風数式を matplotlib mathtext で PNG 化し Discord に送信する Cog。"""

from __future__ import annotations

import asyncio
import io
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

# 入力文字数の上限（乱用・巨大描画の抑制）
MAX_EXPRESSION_LENGTH = 500

# PNG 出力の解像度
RENDER_DPI = 200

# 数式フォントサイズ
MATH_FONT_SIZE = 28

# matplotlib の遅延インポート状態
_MATPLOTLIB_AVAILABLE = False
_plt = None


def _ensure_matplotlib() -> bool:
    """matplotlib を Agg バックエンドで読み込み、利用可否を返す。"""
    global _MATPLOTLIB_AVAILABLE, _plt
    # 既に読み込み済みなら再インポートしない
    if _MATPLOTLIB_AVAILABLE and _plt is not None:
        return True
    try:
        import matplotlib
        # GUI 不要の非対話バックエンドを強制する
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        _plt = plt
        _MATPLOTLIB_AVAILABLE = True
        logger.info("matplotlib (Agg) を LaTeX 描画用に読み込みました。")
        return True
    except Exception as exc:
        # 依存欠落時はコマンド側で案内する
        logger.error("matplotlib の読み込みに失敗しました: %s", exc)
        _MATPLOTLIB_AVAILABLE = False
        _plt = None
        return False


def _normalize_expression(expression: str) -> str:
    """入力を mathtext 向けに整え、必要なら $...$ で包む。"""
    # 前後空白を除去する
    text = expression.strip()
    # ユーザーが貼りがちなコードフェンスを剥がす
    if text.startswith("```") and text.endswith("```"):
        # 先頭の ```lang 行を落とす
        text = re.sub(r"^```(?:latex|tex|math)?\s*", "", text, flags=re.IGNORECASE)
        # 末尾の ``` を落とす
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
    # すでに数式区切りがあればそのまま使う
    if "$" in text or text.startswith(r"\(") or text.startswith(r"\["):
        return text
    # 断片入力はインライン数式として包む
    return f"${text}$"


def _render_latex_to_png(expression: str) -> io.BytesIO:
    """数式文字列を PNG の BytesIO に変換する（同期・スレッド用）。"""
    # バックエンド未準備ならここで例外にする
    if not _ensure_matplotlib() or _plt is None:
        raise RuntimeError("matplotlib is not available")
    # mathtext 用に正規化する
    math_text = _normalize_expression(expression)
    # 図を作成する（サイズは bbox_inches で最終調整）
    fig = _plt.figure(figsize=(0.01, 0.01))
    try:
        # 透明ではなく白背景で Discord 上の視認性を確保する
        fig.patch.set_facecolor("white")
        # 軸なしで中央に数式だけ描く
        fig.text(
            0.5,
            0.5,
            math_text,
            fontsize=MATH_FONT_SIZE,
            ha="center",
            va="center",
            color="black",
        )
        # メモリ上の PNG バッファへ書き出す
        buffer = io.BytesIO()
        fig.savefig(
            buffer,
            format="png",
            dpi=RENDER_DPI,
            bbox_inches="tight",
            pad_inches=0.3,
            facecolor="white",
            edgecolor="none",
        )
        # 読み出し位置を先頭へ戻す
        buffer.seek(0)
        return buffer
    finally:
        # リーク防止のため必ず figure を閉じる
        _plt.close(fig)


class LatexCommandCog(commands.Cog, name="LaTeX"):
    """`/latex` で数式 PNG を返すユーティリティ Cog。"""

    def __init__(self, bot: commands.Bot):
        # Bot 参照を保持する
        self.bot = bot
        # 起動時に一度だけ可用性を確認する
        _ensure_matplotlib()

    @app_commands.command(
        name="latex",
        description="LaTeX風の数式をPNG画像に変換します / Render LaTeX-like math as a PNG image",
    )
    @app_commands.describe(
        expression="数式 (例: E=mc^2 や \\frac{a}{b}) / Math expression (e.g. E=mc^2 or \\frac{a}{b})",
    )
    async def latex(self, interaction: discord.Interaction, expression: str) -> None:
        """ユーザー入力の数式を描画して PNG で返信する。"""
        # 描画は重いので先に defer する
        await interaction.response.defer(thinking=True)

        # 空入力を弾く
        if not expression or not expression.strip():
            embed = discord.Embed(
                title="⚠️ Input Error / 入力エラー",
                description=(
                    "数式が空です。\n"
                    "The expression is empty."
                ),
                color=discord.Color.orange(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # 長さ制限を適用する
        if len(expression) > MAX_EXPRESSION_LENGTH:
            embed = discord.Embed(
                title="⚠️ Input Too Long / 入力が長すぎます",
                description=(
                    f"数式は {MAX_EXPRESSION_LENGTH} 文字以内にしてください。\n"
                    f"Please keep the expression within {MAX_EXPRESSION_LENGTH} characters."
                ),
                color=discord.Color.orange(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # matplotlib が無い場合は案内する
        if not _ensure_matplotlib():
            embed = discord.Embed(
                title="❌ Dependency Missing / 依存関係不足",
                description=(
                    "`matplotlib` が利用できません。要件を確認してください。\n"
                    "`matplotlib` is unavailable. Please check requirements."
                ),
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        try:
            # イベントループを塞がないようスレッドで描画する
            buffer = await asyncio.to_thread(_render_latex_to_png, expression)
        except Exception as exc:
            # mathtext の構文エラー等をユーザー向けに返す
            logger.warning("/latex 描画失敗 (User: %s): %s", interaction.user.id, exc)
            # 長すぎる例外文は切り詰める
            detail = str(exc)
            if len(detail) > 300:
                detail = detail[:300] + "..."
            embed = discord.Embed(
                title="❌ Render Failed / 描画失敗",
                description=(
                    "数式を画像化できませんでした（matplotlib mathtext の範囲外の可能性）。\n"
                    "Could not render the expression (it may be outside matplotlib mathtext).\n\n"
                    f"```\n{detail}\n```"
                ),
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Discord 添付ファイルとして包む
        file = discord.File(fp=buffer, filename="latex.png")
        # プレビュー用 Embed を組み立てる
        preview = expression.strip()
        if len(preview) > 200:
            preview = preview[:200] + "..."
        embed = discord.Embed(
            title="📐 LaTeX Preview",
            description=f"```latex\n{preview}\n```",
            color=discord.Color.blurple(),
        )
        # 添付画像を Embed 内に表示する
        embed.set_image(url="attachment://latex.png")
        embed.set_footer(text="Rendered with matplotlib mathtext")
        # 画像付きで返信する
        await interaction.followup.send(embed=embed, file=file)
        logger.info("/latex が実行されました。 (User: %s)", interaction.user.id)


async def setup(bot: commands.Bot) -> None:
    """Cog を Bot に登録する。"""
    # 標準どおり add_cog する
    await bot.add_cog(LatexCommandCog(bot))
    logger.info("LatexCommandCog が正常にロードされました。")
