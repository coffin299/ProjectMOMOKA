# 分類結果に基づくモード実行ヘルパー。
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from MOMOKA.llm.router.classifier import RouteResult, classify_request
from MOMOKA.llm.router.command_executor import run_command_mode
from MOMOKA.utilities.locale import pick_str

if TYPE_CHECKING:
    from MOMOKA.llm.llm_cog import LLMCog

logger = logging.getLogger(__name__)

# 待機 UI 用カスタム絵文字
EMOJI_ROUTER = "<a:loading:1531129757234757744>"
EMOJI_CODING = "<a:coding:1531206254574178346>"
EMOJI_STREAM = "<:stream:1313474295372058758>"


def waiting_phase_title(phase: str, lang: str) -> str:
    """フェーズ見出し文言。"""
    ja = lang.startswith("ja")
    if phase == "router":
        return (
            f"{EMOJI_ROUTER} リクエストを振り分け中…"
            if ja
            else f"{EMOJI_ROUTER} Routing your request…"
        )
    if phase == "coding":
        return (
            f"{EMOJI_CODING} コードを生成中…"
            if ja
            else f"{EMOJI_CODING} Generating code…"
        )
    if phase == "command":
        return (
            f"{EMOJI_STREAM} コマンドを処理中…"
            if ja
            else f"{EMOJI_STREAM} Processing command…"
        )
    # conversation / default
    return (
        f"{EMOJI_STREAM} 考え中…"
        if ja
        else f"{EMOJI_STREAM} Thinking…"
    )


def mode_model_chain(llm_config: Dict[str, Any], mode: str) -> List[str]:
    """modes.<mode> の [model]+fallback_models。無ければトップレベルへフォールバック。"""
    modes = llm_config.get("modes") or {}
    entry = modes.get(mode) if isinstance(modes, dict) else None
    chain: List[str] = []
    if isinstance(entry, dict):
        primary = entry.get("model")
        if primary:
            chain.append(str(primary))
        for m in entry.get("fallback_models") or []:
            if m and str(m) not in chain:
                chain.append(str(m))
    # conversation で空ならトップレベル
    if not chain:
        top = llm_config.get("model")
        if top:
            chain.append(str(top))
        for m in llm_config.get("fallback_models") or []:
            if m and str(m) not in chain:
                chain.append(str(m))
    return chain


def coding_attach_settings(llm_config: Dict[str, Any]) -> Dict[str, Any]:
    """coding モードの添付関連設定。"""
    modes = llm_config.get("modes") or {}
    coding = modes.get("coding") if isinstance(modes, dict) else {}
    if not isinstance(coding, dict):
        coding = {}
    return {
        "attach_as_file_threshold": int(coding.get("attach_as_file_threshold", 1800)),
        "ask_attach_if_over": bool(coding.get("ask_attach_if_over", True)),
        "always_attach_if_code_fence": bool(
            coding.get("always_attach_if_code_fence", True)
        ),
        "skip_stream_preview": bool(coding.get("skip_stream_preview", True)),
        "plain_text_max_chars": int(coding.get("plain_text_max_chars", 500)),
        "max_attached_text_chars": int(coding.get("max_attached_text_chars", 50000)),
        "article_filename": str(coding.get("article_filename", "article.md")),
    }


ARTICLE_FILENAME_DEFAULT = "article.md"

_CODEFENCE_RE = re.compile(r"```([^\n`]*)\n?(.*?)```", re.DOTALL)

# 冒頭の挨拶・導入っぽい文を plain text 側へ寄せる
_INTRO_HINT_PATTERNS = (
    r"了解",
    r"承知",
    r"以下",
    r"こちら",
    r"ご確認",
    r"お待ち",
    r"here('s| is)",
    r"\bsure\b",
    r"\bokay\b",
    r"of course",
    r"below is",
    r"the code",
    r"コード",
    r"実装しました",
    r"作成しました",
    r"generated",
    r"attached",
    r"please find",
)

# 言語タグ → 拡張子
_LANG_EXTENSION_MAP: Dict[str, str] = {
    "python": "py",
    "py": "py",
    "javascript": "js",
    "js": "js",
    "typescript": "ts",
    "ts": "ts",
    "tsx": "tsx",
    "jsx": "jsx",
    "java": "java",
    "kotlin": "kt",
    "kt": "kt",
    "swift": "swift",
    "go": "go",
    "golang": "go",
    "rust": "rs",
    "rs": "rs",
    "ruby": "rb",
    "rb": "rb",
    "php": "php",
    "c": "c",
    "cpp": "cpp",
    "c++": "cpp",
    "csharp": "cs",
    "cs": "cs",
    "html": "html",
    "css": "css",
    "scss": "scss",
    "sql": "sql",
    "bash": "sh",
    "sh": "sh",
    "shell": "sh",
    "zsh": "sh",
    "powershell": "ps1",
    "ps1": "ps1",
    "yaml": "yaml",
    "yml": "yaml",
    "json": "json",
    "toml": "toml",
    "xml": "xml",
    "lua": "lua",
    "r": "r",
    "dart": "dart",
    "vue": "vue",
    "svelte": "svelte",
    "markdown": "md",
    "md": "md",
    "txt": "txt",
    "text": "txt",
    "plaintext": "txt",
}


@dataclass
class CodingOutputSplit:
    """coding 応答を Discord 本文 / article.md / コードファイルに分割した結果。"""

    plain_text: str = ""
    article_md: str = ""
    code_files: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def attachment_names(self) -> List[str]:
        """添付予定ファイル名一覧。"""
        names: List[str] = []
        if self.article_md.strip():
            names.append(ARTICLE_FILENAME_DEFAULT)
        names.extend(name for name, _ in self.code_files)
        return names


def coding_response_has_code_fence(text: str) -> bool:
    """Markdown コードフェンスを含むか。"""
    return "```" in (text or "")


def should_attach_coding_output(text: str, cfg: Dict[str, Any]) -> bool:
    """coding 応答をファイル添付すべきか。"""
    if not cfg.get("ask_attach_if_over", True):
        return False
    if cfg.get("always_attach_if_code_fence", True) and coding_response_has_code_fence(text):
        return True
    threshold = int(cfg.get("attach_as_file_threshold", 1800))
    return len(text) >= threshold or text.count("```") >= 4


def _language_to_extension(lang_tag: str) -> str:
    """フェンス言語タグから拡張子を返す。"""
    key = (lang_tag or "").strip().lower()
    return _LANG_EXTENSION_MAP.get(key, "txt")


def _looks_like_intro_paragraph(para: str) -> bool:
    """冒頭段落が挨拶・導入文か。"""
    stripped = para.strip()
    if not stripped:
        return False
    if len(stripped) > 400:
        return False
    if re.match(r"^#{1,6}\s", stripped):
        return False
    if stripped.count("\n- ") > 2 or stripped.count("\n* ") > 2:
        return False
    if any(re.search(pattern, stripped, re.IGNORECASE) for pattern in _INTRO_HINT_PATTERNS):
        return True
    return len(stripped) <= 160


def _split_single_paragraph_intro(para: str) -> Tuple[str, str]:
    """1段落内に挨拶+説明が続く場合、先頭文だけ plain へ。"""
    match = re.match(r"^(.{0,220}?[。！!？?～~]+)\s*(.+)$", para, re.DOTALL)
    if match:
        intro = match.group(1).strip()
        rest = match.group(2).strip()
        if intro and rest and _looks_like_intro_paragraph(intro):
            return intro, rest
    return para, ""


def _split_prose(prose: str) -> Tuple[str, str]:
    """説明テキストを plain 導入文と article 本文に分ける。"""
    cleaned = re.sub(r"\n{3,}", "\n\n", (prose or "").strip())
    if not cleaned:
        return "", ""
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", cleaned) if part.strip()]
    if not paragraphs:
        return "", ""
    if len(paragraphs) == 1 and _looks_like_intro_paragraph(paragraphs[0]):
        intro, article = _split_single_paragraph_intro(paragraphs[0])
        if article:
            return intro, article
        return paragraphs[0], ""
    if _looks_like_intro_paragraph(paragraphs[0]) and len(paragraphs) > 1:
        return paragraphs[0], "\n\n".join(paragraphs[1:])
    if _looks_like_intro_paragraph(paragraphs[0]) and len(paragraphs) == 1:
        return paragraphs[0], ""
    return "", "\n\n".join(paragraphs)


def _extract_code_files(text: str) -> List[Tuple[str, str, str]]:
    """コードフェンスを (lang, content, ext) のリストで返す。"""
    blocks: List[Tuple[str, str, str]] = []
    for match in _CODEFENCE_RE.finditer(text or ""):
        lang_tag = (match.group(1) or "").strip()
        content = (match.group(2) or "").strip("\n")
        if not content:
            continue
        ext = _language_to_extension(lang_tag)
        blocks.append((lang_tag, content, ext))
    return blocks


def _build_code_filenames(blocks: List[Tuple[str, str, str]]) -> List[Tuple[str, str]]:
    """拡張子ごとに連番付きファイル名を作る。"""
    ext_counts: Dict[str, int] = {}
    files: List[Tuple[str, str]] = []
    for _lang_tag, content, ext in blocks:
        ext_counts[ext] = ext_counts.get(ext, 0) + 1
        count = ext_counts[ext]
        if len(blocks) == 1:
            filename = f"code.{ext}"
        else:
            filename = f"code_{count}.{ext}"
        files.append((filename, content))
    return files


def split_coding_response(
    text: str,
    *,
    lang: str = "en",
    plain_text_max_chars: int = 500,
) -> CodingOutputSplit:
    """coding 応答を plain / article.md / 言語別コードファイルへ分割する。"""
    raw = text or ""
    code_blocks = _extract_code_files(raw)
    prose = _CODEFENCE_RE.sub("", raw)
    prose = re.sub(r"\n{3,}", "\n\n", prose).strip()
    plain_part, article_part = _split_prose(prose)
    code_files = _build_code_filenames(code_blocks)

    if plain_part and len(plain_part) > plain_text_max_chars:
        overflow = plain_part[plain_text_max_chars:].strip()
        plain_part = plain_part[:plain_text_max_chars].rstrip() + "..."
        if overflow:
            article_part = f"{overflow}\n\n{article_part}".strip() if article_part else overflow

    if not plain_part:
        if article_part or code_files:
            plain_part = pick_str(
                lang,
                ja="説明とコードは添付ファイルを参照してください。",
                en="See the attached files for the explanation and code.",
            )
        else:
            plain_part = pick_str(
                lang,
                ja="応答を生成しました。",
                en="Response generated.",
            )

    return CodingOutputSplit(
        plain_text=plain_part.strip(),
        article_md=article_part.strip(),
        code_files=code_files,
    )


def format_coding_attachment_note(
    split: CodingOutputSplit,
    *,
    lang: str,
    article_filename: str = ARTICLE_FILENAME_DEFAULT,
) -> str:
    """Discord 本文末尾に付ける添付一覧。"""
    names: List[str] = []
    if split.article_md.strip():
        names.append(article_filename)
    names.extend(name for name, _ in split.code_files)
    if not names:
        return ""
    joined = ", ".join(f"`{name}`" for name in names)
    return pick_str(
        lang,
        ja=f"\n\n📎 {joined}",
        en=f"\n\n📎 Attachments: {joined}",
    )


def coding_output_prompt(llm_config: Dict[str, Any]) -> str:
    """coding モード用の出力形式指示（config から）。"""
    modes = llm_config.get("modes") or {}
    coding = modes.get("coding") if isinstance(modes, dict) else {}
    if not isinstance(coding, dict):
        return ""
    return str(coding.get("output_prompt") or "").strip()


def unsupported_message(lang: str, reason: str = "") -> str:
    """能力外の正直な拒否文。"""
    if lang.startswith("ja"):
        base = (
            "すみません、その操作はこのボットでは実行できません。"
            "できること・できないことを偽って完了したようには言いません。"
        )
    else:
        base = (
            "Sorry — I cannot perform that action with this bot's capabilities. "
            "I will not pretend it was completed."
        )
    if reason:
        return f"{base}\n({reason})"
    return base


def image_gen_disabled_message(lang: str) -> str:
    """画像生成無効時の案内。"""
    if lang.startswith("ja"):
        return (
            "画像生成機能は現在無効です（設定でオフになっています）。"
            "代わりに画像検索などのスラッシュコマンドをご利用ください。"
            "PLANA の `/help` または `/invite` からコマンド一覧を確認できます。"
        )
    return (
        "Image generation is currently disabled in config. "
        "Please use related slash commands (e.g. image search) instead. "
        "Check PLANA `/help` or `/invite` for available commands."
    )


async def run_routed_response(
    cog: "LLMCog",
    *,
    user_text: str,
    channel_id: int,
) -> RouteResult:
    """分類のみ実行して RouteResult を返す（実行本体は llm_cog 側）。"""
    # 分類を呼ぶ
    return await classify_request(cog, user_text=user_text, channel_id=channel_id)


async def execute_command_baton(
    cog: "LLMCog",
    *,
    user_text: str,
    lang: str,
    channel: Any,
    author: Any,
) -> str:
    """command モードのバトンタッチ実行。"""
    return await run_command_mode(
        cog,
        user_text=user_text,
        lang=lang,
        channel=channel,
        author=author,
    )
