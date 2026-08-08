# MOMOKA/utilities/updates_view.py
# /updates 用 Components V2 LayoutView（全件取得済みコミットの 5 件ページング）。
from __future__ import annotations

import datetime
from typing import Any, Dict, List

import discord

from MOMOKA.utilities.locale import pick_str

# 1 ページあたりのコミット表示件数（音楽キューと同型）
UPDATES_PAGE_SIZE = 5

# コミットメッセージの表示上限（文字）
_MESSAGE_MAX_LEN = 80


class UpdatesLayoutView(discord.ui.LayoutView):
    """GitHub コミット履歴を Components V2 でページ表示する。"""

    def __init__(
        self,
        *,
        commits: List[Dict[str, Any]],
        repo_name: str,
        repo_url: str,
        lang: str = "en",
        page: int = 0,
    ) -> None:
        # 放置された操作用 UI を約10分後に無効化する
        super().__init__(timeout=600)
        # 取得済みコミット一覧を保持する
        self.commits = commits
        # リポジトリ名（表示用）
        self.repo_name = repo_name
        # リポジトリ URL（リンクボタン用）
        self.repo_url = repo_url
        # UI 言語を正規化する
        self.lang = "ja" if lang == "ja" else "en"
        # 初期ページを範囲内に収める
        self.page = max(0, min(page, self._total_pages() - 1))
        # UI を組み立てる
        self._rebuild()

    def _total_pages(self) -> int:
        """総ページ数を返す（空リストでも最低 1）。"""
        # コミットが無い場合もページ表示用に 1 とする
        if not self.commits:
            return 1
        # 切り上げ除算で総ページを求める
        return (len(self.commits) + UPDATES_PAGE_SIZE - 1) // UPDATES_PAGE_SIZE

    def _rebuild(self) -> None:
        """タイトル・本文・ナビ・リポジトリリンクを組み立てる。"""
        # 既存子要素を捨てて作り直す
        self.clear_items()
        # 総ページを最新件数から再計算する
        total_pages = self._total_pages()
        # ページ番号を範囲内にクランプする
        self.page = max(0, min(self.page, total_pages - 1))
        # ルートコンテナを用意する
        container = discord.ui.Container()
        # タイトル行を組み立てる
        title = pick_str(
            self.lang,
            ja="📜 アップデート履歴",
            en="📜 Update History",
        )
        # 説明文（件数・ページサイズ・リポジトリ）
        if self.commits:
            desc = pick_str(
                self.lang,
                ja=(
                    f"[{self.repo_name}]({self.repo_url}) のコミット全 {len(self.commits)} 件を"
                    f" {UPDATES_PAGE_SIZE} 件ずつ表示しています。"
                ),
                en=(
                    f"Showing all {len(self.commits)} commits from "
                    f"[{self.repo_name}]({self.repo_url}), {UPDATES_PAGE_SIZE} per page."
                ),
            )
        else:
            # 空のときは空状態メッセージにする
            desc = pick_str(
                self.lang,
                ja=f"[{self.repo_name}]({self.repo_url}) に表示できるコミットがありません。",
                en=f"No commits to show for [{self.repo_name}]({self.repo_url}).",
            )
        # タイトル＋説明を TextDisplay に載せる
        container.add_item(discord.ui.TextDisplay(f"## {title}\n{desc}"))
        # 区切り線で本文とヘッダを分ける
        container.add_item(discord.ui.Separator())
        # 現ページのコミット本文を載せる
        container.add_item(discord.ui.TextDisplay(self._page_body()))
        # 複数ページのときだけナビ行を付ける
        if total_pages > 1:
            # ナビ用 ActionRow を作る
            nav_row = discord.ui.ActionRow()
            # 先頭ページボタン
            first_btn = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label="⏪",
                custom_id="updates_first",
                disabled=(self.page <= 0),
            )
            # コールバックを結ぶ
            first_btn.callback = self._first_callback
            # 行に追加する
            nav_row.add_item(first_btn)
            # 前ページボタン
            prev_btn = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label="◀️",
                custom_id="updates_prev",
                disabled=(self.page <= 0),
            )
            # コールバックを結ぶ
            prev_btn.callback = self._prev_callback
            # 行に追加する
            nav_row.add_item(prev_btn)
            # 現在ページ表示（押下不可）
            page_btn = discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label=f"{self.page + 1}/{total_pages}",
                custom_id="updates_page_indicator",
                disabled=True,
            )
            # 行に追加する
            nav_row.add_item(page_btn)
            # 次ページボタン
            next_btn = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label="▶️",
                custom_id="updates_next",
                disabled=(self.page >= total_pages - 1),
            )
            # コールバックを結ぶ
            next_btn.callback = self._next_callback
            # 行に追加する
            nav_row.add_item(next_btn)
            # 末尾ページボタン
            last_btn = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label="⏩",
                custom_id="updates_last",
                disabled=(self.page >= total_pages - 1),
            )
            # コールバックを結ぶ
            last_btn.callback = self._last_callback
            # 行に追加する
            nav_row.add_item(last_btn)
            # コンテナにナビ行を付ける
            container.add_item(nav_row)
        # リポジトリ URL があればリンク行を付ける
        if self.repo_url:
            # リンク用 ActionRow
            link_row = discord.ui.ActionRow()
            # GitHub へのリンクボタン
            link_label = pick_str(
                self.lang,
                ja="GitHub で見る",
                en="View on GitHub",
            )
            # Link スタイルは url 必須
            link_btn = discord.ui.Button(
                style=discord.ButtonStyle.link,
                label=link_label,
                url=self.repo_url,
            )
            # 行に追加する
            link_row.add_item(link_btn)
            # コンテナに付ける
            container.add_item(link_row)
        # ルートにコンテナを載せる
        self.add_item(container)

    def _page_body(self) -> str:
        """現在ページのコミット一覧テキストを返す。"""
        # 空なら空状態のみ
        if not self.commits:
            return pick_str(
                self.lang,
                ja="（コミットなし）",
                en="(No commits)",
            )
        # スライス開始位置を計算する
        start = self.page * UPDATES_PAGE_SIZE
        # 終了位置を計算する
        end = start + UPDATES_PAGE_SIZE
        # 現ページ分だけ取り出す
        slice_commits = self.commits[start:end]
        # 行バッファ
        lines: List[str] = []
        # 各コミットを Markdown 行に整形する
        for commit_data in slice_commits:
            # 1 件分の行を追加する
            lines.append(self._format_commit(commit_data))
        # 空行区切りで結合する
        return "\n\n".join(lines)

    def _format_commit(self, commit_data: Dict[str, Any]) -> str:
        """単一コミットを表示用 Markdown にする。"""
        # SHA 先頭 7 桁
        sha = str(commit_data.get("sha") or "")[:7]
        # commit オブジェクトを取る
        commit_obj = commit_data.get("commit") or {}
        # メッセージ先頭行のみ使う
        raw_message = str((commit_obj.get("message") or "")).split("\n")[0]
        # 長すぎるメッセージは切り詰める
        if len(raw_message) > _MESSAGE_MAX_LEN:
            # 末尾に省略記号を付ける
            message = raw_message[: _MESSAGE_MAX_LEN - 3] + "..."
        else:
            # そのまま使う
            message = raw_message or "(no message)"
        # 作者名
        author_obj = commit_obj.get("author") or {}
        # 作者表示（無い場合は Unknown）
        author = str(author_obj.get("name") or "Unknown")
        # コミット HTML URL
        html_url = str(commit_data.get("html_url") or self.repo_url or "")
        # 日付文字列
        date_str = str(author_obj.get("date") or "")
        # 相対時刻表示用のタイムスタンプ
        timestamp = ""
        # 日付が取れたときだけ相対時刻を付ける
        if date_str:
            try:
                # ISO8601（Z 付き）を datetime にする
                commit_date = datetime.datetime.fromisoformat(
                    date_str.replace("Z", "+00:00")
                )
                # Discord 相対時刻フォーマット
                timestamp = discord.utils.format_dt(commit_date, style="R")
            except (TypeError, ValueError):
                # パース失敗時は時刻なし
                timestamp = ""
        # ヘッダ行（sha / author / 相対時刻）
        if timestamp:
            header = f"📝 `{sha}` by {author} ({timestamp})"
        else:
            header = f"📝 `{sha}` by {author}"
        # メッセージをリンク化する（URL が無いときはプレーン）
        if html_url:
            body = f"[{message}]({html_url})"
        else:
            body = message
        # ヘッダ＋本文
        return f"**{header}**\n{body}"

    async def _goto_page(self, interaction: discord.Interaction, target_page: int) -> None:
        """指定ページへ移動してメッセージを編集する。"""
        # 総ページを求める
        total_pages = self._total_pages()
        # 範囲内にクランプする
        new_page = max(0, min(target_page, total_pages - 1))
        # 変化が無ければ応答だけ返す
        if new_page == self.page:
            await interaction.response.defer()
            return
        # ページを更新する
        self.page = new_page
        # UI を組み直す
        self._rebuild()
        # V2 は embed 併用不可のため view のみ更新する
        await interaction.response.edit_message(view=self)

    async def _first_callback(self, interaction: discord.Interaction) -> None:
        """先頭ページへ移動する。"""
        # ページ 0 へ
        await self._goto_page(interaction, 0)

    async def _prev_callback(self, interaction: discord.Interaction) -> None:
        """前ページへ移動する。"""
        # 1 つ戻る
        await self._goto_page(interaction, self.page - 1)

    async def _next_callback(self, interaction: discord.Interaction) -> None:
        """次ページへ移動する。"""
        # 1 つ進む
        await self._goto_page(interaction, self.page + 1)

    async def _last_callback(self, interaction: discord.Interaction) -> None:
        """末尾ページへ移動する。"""
        # 最終ページへ
        await self._goto_page(interaction, self._total_pages() - 1)
