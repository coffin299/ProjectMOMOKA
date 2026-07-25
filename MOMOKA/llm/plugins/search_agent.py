# MOMOKA/llm/plugins/search_agent.py
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, List, Dict

# カスタム例外をインポート
from MOMOKA.llm.error.errors import (
    SearchExecutionError,
    SearchAgentError,
)

if TYPE_CHECKING:
    from discord.ext import commands

logger = logging.getLogger(__name__)

# ddgs 未インストール時の案内メッセージ
_DDGS_MISSING_MSG = (
    "ddgs package is not installed. "
    'Run: pip install "ddgs>=9.0.0"'
)


class SearchAgent:
    name = "search"
    # LLMが呼び出すツール定義（OpenAI互換Function Calling形式）
    tool_spec = {
        "type": "function",
        "function": {
            "name": name,
            "description": (
                "Run a web search and return results. "
                "Prefer 1-2 focused queries; avoid near-duplicate re-searches."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }

    def __init__(self, bot: "commands.Bot", config: dict = None) -> None:
        self.bot = bot
        # 設定セクションの取得（configはllmセクション全体）
        gcfg = None
        if config:
            logger.info(f"SearchAgent init with config. Keys: {list(config.keys())}")
            # configはllmセクション全体 → "agent"キーで検索設定を取得
            gcfg = config.get("agent")
        elif hasattr(self.bot, "cfg") and self.bot.cfg:
            logger.info("SearchAgent init with bot.cfg.")
            gcfg = self.bot.cfg.get("llm", {}).get("agent")

        # デフォルト設定（ddgsはAPIキー不要・autoで複数バックエンドを試行）
        self.max_results = 10
        self.timeout = 30.0
        self.backend = "auto"

        # configがあれば設定を上書き
        if gcfg:
            self.max_results = gcfg.get("max_results", 10)
            self.timeout = gcfg.get("timeout", 30.0)
            self.backend = gcfg.get("backend", "auto")

        logger.info(
            f"SearchAgent initialized "
            f"(ddgs backend={self.backend}, max_results={self.max_results})."
        )

    @staticmethod
    def _map_ddgs_results(raw_results: list) -> List[Dict[str, str]]:
        """ddgs の戻り値を既存形式 {title, url, snippet} に変換する。"""
        # 結果リストを初期化する
        results: List[Dict[str, str]] = []
        # ddgs が返す各エントリを走査する
        for item in raw_results or []:
            # dict 以外はスキップする
            if not isinstance(item, dict):
                continue
            # タイトルを取り出す
            title = (item.get("title") or "").strip()
            # href を URL として使う（互換キー url も許容）
            url = (item.get("href") or item.get("url") or "").strip()
            # body をスニペットとして使う
            snippet = (item.get("body") or item.get("snippet") or "").strip()
            # タイトルまたは URL が空なら無効な結果とする
            if not title or not url:
                continue
            # 既存フォーマットへ追加する
            results.append({
                "title": title,
                "url": url,
                "snippet": snippet,
            })
        # 変換済みリストを返す
        return results

    def _search_sync(self, query: str) -> List[Dict[str, str]]:
        """同期 API の ddgs.DDGS().text() を呼び、結果をマッピングして返す。"""
        try:
            # ddgs を遅延インポートする（未インストール時は明確なエラーにする）
            from ddgs import DDGS
        except ImportError as e:
            # 依存不足を実行エラーとして返す
            raise SearchExecutionError(_DDGS_MISSING_MSG) from e

        # タイムアウト秒数を int に丸める（ddgs は秒単位の int を期待）
        timeout_sec = max(1, int(self.timeout))
        try:
            # DDGS クライアントを生成してテキスト検索する
            with DDGS(timeout=timeout_sec) as ddgs:
                # auto なら複数バックエンドを自動試行する
                raw = ddgs.text(
                    query,
                    max_results=self.max_results,
                    backend=self.backend,
                )
        except SearchExecutionError:
            # そのまま再送出する
            raise
        except Exception as e:
            # 検索ライブラリ側の例外を実行エラーに包む
            raise SearchExecutionError(
                f"ddgs search failed: {e}"
            ) from e

        # 既存形式へ変換して返す
        return self._map_ddgs_results(raw)

    async def _search_duckduckgo(self, query: str) -> List[Dict[str, str]]:
        """ddgs メタ検索を非同期で実行し、結果リストを返す。

        DDGS は同期 API のため asyncio.to_thread でイベントループを塞がない。
        backend=auto 時は bing / brave / wikipedia 等へ自動フォールバックする。
        """
        # 同期検索を別スレッドで実行する
        results = await asyncio.to_thread(self._search_sync, query)
        # 結果が空なら実行エラーにする
        if not results:
            raise SearchExecutionError(
                f"Web search for '{query}' returned no results "
                f"(backend={self.backend})."
            )
        # 成功ログを出す
        logger.info(
            f"ddgs search for '{query}' returned {len(results)} results "
            f"(backend={self.backend})."
        )
        # 結果を返す
        return results

    @staticmethod
    def _format_results_as_text(query: str, results: List[Dict[str, str]]) -> str:
        """検索結果をLLMが理解しやすいテキスト形式にフォーマットする"""
        if not results:
            return f"Web search for '{query}' returned no results."

        # ヘッダー
        lines = [f"## Web Search Results for: {query}", ""]

        # 各結果をフォーマット
        for i, result in enumerate(results, 1):
            title = result.get("title", "No Title")
            url = result.get("url", "")
            snippet = result.get("snippet", "")

            lines.append(f"### {i}. {title}")
            lines.append(f"**URL:** {url}")
            if snippet:
                lines.append(f"**Summary:** {snippet}")
            lines.append("")  # 空行で区切り

        # LLMへの指示
        lines.append("---")
        lines.append(
            "Use the above search results to provide a comprehensive, "
            "accurate answer to the user's question. "
            "Cite sources where appropriate."
        )

        return "\n".join(lines)

    async def run(self, *, arguments: dict, bot: "commands.Bot", channel_id: int) -> str:
        """検索を実行するメインメソッド。フォーマット済みテキスト結果を返す。"""
        query = arguments.get("query", "")
        if not query:
            raise SearchExecutionError("Query cannot be empty.")

        try:
            # ddgs で検索を実行
            results = await self._search_duckduckgo(query)
            # 結果をLLM向けテキストにフォーマットして返す
            return self._format_results_as_text(query, results)

        except SearchAgentError:
            # SearchAgentError系はそのまま再raise
            raise
        except asyncio.TimeoutError:
            raise SearchExecutionError(
                f"Web search timed out after {self.timeout}s."
            )
        except Exception as e:
            logger.error(f"Search Agent unexpected error: {e}", exc_info=True)
            raise SearchExecutionError(
                f"An unexpected error occurred during search: {str(e)}",
                original_exception=e,
            )
