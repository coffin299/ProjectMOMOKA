"""英単語を kanalizer でカタカナ化し、日本語 TTS へ渡す。

Style-Bert-VITS2 の日本語合成はラテン文字を文字名（エー、ビー）で読むため、
英単語は VOICEVOX/kanalizer で仮名読みへ置換してから合成する。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)

# OpenJTalk 系のアルファベット1文字読み（VOICEVOX/open_jtalk と同じ対応）
_OJT_LETTER = {
    "A": "エー",
    "B": "ビー",
    "C": "シー",
    "D": "ディー",
    "E": "イー",
    "F": "エフ",
    "G": "ジー",
    "H": "エイチ",
    "I": "アイ",
    "J": "ジェー",
    "K": "ケー",
    "L": "エル",
    "M": "エム",
    "N": "エヌ",
    "O": "オー",
    "P": "ピー",
    "Q": "キュー",
    "R": "アール",
    "S": "エス",
    "T": "ティー",
    "U": "ユー",
    "V": "ブイ",
    "W": "ダブリュー",
    "X": "エックス",
    "Y": "ワイ",
    "Z": "ズィー",
}

# 英単語（内部のアポストロフィ・ハイフンは単語の一部）
_EN_WORD = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")
# キャメルケース分割（VoiceVox → Voice, Vox）
_CAMEL_PARTS = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")

# kanalizer の利用可否（None=未判定）
_kanalizer_ok: Optional[bool] = None


def _fold_fullwidth_latin(text: str) -> str:
    """全角英字だけ半角にする。記号や仮名は変えない。"""
    # 変換結果を貯める
    folded: list[str] = []
    # 1 文字ずつ見る
    for ch in text:
        # 符号点
        code = ord(ch)
        # 全角 A-Z / a-z だけ NFKC で半角にする
        if 0xFF21 <= code <= 0xFF3A or 0xFF41 <= code <= 0xFF5A:
            # 半角へ
            folded.append(unicodedata.normalize("NFKC", ch))
        else:
            # その他はそのまま
            folded.append(ch)
    # 結合して返す
    return "".join(folded)


def _letters_only(word: str) -> str:
    """アポストロフィ等を除いた A-Za-z だけを返す。"""
    # 英字以外を落とす
    return re.sub(r"[^A-Za-z]", "", word)


def _char_wise_katakana(letters: str) -> str:
    """アルファベットを1文字ずつカタカナ読みにする。"""
    # 大文字キーで対応表を引く
    return "".join(_OJT_LETTER[ch] for ch in letters.upper() if ch in _OJT_LETTER)


def _should_use_kanalizer(letters: str) -> bool:
    """kanalizer で単語読みにすべきか（1文字・全部大文字は文字読み）。"""
    # 空は変換しない
    if not letters:
        # 変換対象外
        return False
    # 1 文字は文字読み
    if len(letters) == 1:
        # 文字読み
        return False
    # 全部大文字（略語）は文字読み
    if letters == letters.upper():
        # 文字読み
        return False
    # それ以外は kanalizer
    return True


def _kanalizer_available() -> bool:
    """kanalizer を import できるか一度だけ調べる。"""
    global _kanalizer_ok
    # 判定済みなら再利用
    if _kanalizer_ok is not None:
        # キャッシュ
        return _kanalizer_ok
    try:
        # 実体の存在確認
        import kanalizer  # noqa: F401

        # 使える
        _kanalizer_ok = True
    except Exception as exc:  # pragma: no cover - 実行環境依存
        # 以後スキップする
        _kanalizer_ok = False
        # 起動時に一度だけ分かるよう警告する
        logger.warning(
            "kanalizer を import できないため英単語のカタカナ変換をスキップします: %s",
            exc,
        )
    # 判定結果
    return bool(_kanalizer_ok)


@lru_cache(maxsize=4096)
def _convert_letters(letters: str) -> str:
    """英字列（記号なし）をカタカナへ変換する。"""
    # 空ならそのまま
    if not letters:
        # 空文字
        return ""
    # 文字読み対象
    if not _should_use_kanalizer(letters):
        # 1 文字ずつ
        return _char_wise_katakana(letters)
    # キャメルケースを分割する
    parts = _CAMEL_PARTS.findall(letters)
    # 分割できなければ全体を1語にする
    if not parts:
        # フォールバック
        parts = [letters]
    # 各部分の読み
    out: list[str] = []
    # 部分ごとに変換
    for part in parts:
        # 部分が文字読み対象なら対応表
        if not _should_use_kanalizer(part):
            # 文字読み
            out.append(_char_wise_katakana(part))
            # 次へ
            continue
        try:
            # kanalizer は小文字英単語を想定する
            import kanalizer

            # 読みを生成する
            out.append(kanalizer.convert(part.lower()))
        except Exception as exc:
            # 失敗時は文字読みへ落とす
            logger.debug("kanalizer 変換失敗 (%s): %s", part, exc)
            # 文字読み
            out.append(_char_wise_katakana(part))
    # 連結して返す
    return "".join(out)


def apply_english_reading(text: str) -> str:
    """文中の英単語をカタカナ読みへ置換する。kanalizer 不可なら原文のまま。"""
    # 空文は何もしない
    if not text:
        # 空
        return text
    # パッケージが無ければ原文
    if not _kanalizer_available():
        # 変換なし
        return text
    # 全角英字を半角にしてから置換する
    folded = _fold_fullwidth_latin(text)

    def _replace(match: re.Match[str]) -> str:
        # マッチした英単語
        word = match.group(0)
        # 記号を除いた英字列
        letters = _letters_only(word)
        # 英字が無ければ原文
        if not letters:
            # そのまま
            return word
        # カタカナへ
        return _convert_letters(letters)

    # 英単語スパンだけ置換する
    return _EN_WORD.sub(_replace, folded)
