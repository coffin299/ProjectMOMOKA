# 応答言語の推定・一致判定（ルーター共有）。
from __future__ import annotations

import re

# ルーターと同一の許可言語コード
VALID_LANGS = frozenset({"ja", "en", "ko", "vi", "zh-CN", "zh-TW"})

# 判定用テキストがこれ未満ならリトライしない（短文・絵文字のみ等）
_MIN_DETECT_CHARS = 12

# コードフェンスを除去する
_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)
# インラインコードを除去する
_INLINE_CODE_RE = re.compile(r"`[^`]+`")
# URL を除去する
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def heuristic_lang(text: str) -> str:
    """簡易言語推定（ルーター失敗時・応答チェック共用）。"""
    # 空なら英語
    if not text or not text.strip():
        return "en"
    # ハングル
    if re.search(r"[\uac00-\ud7af]", text):
        return "ko"
    # ひらがな・カタカナがあれば日本語
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    # 漢字のみ寄りは簡体寄りとする（厳密判定はしない）
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh-CN"
    # ベトナム語っぽい声調記号
    if re.search(r"[ăâêôơưđĂÂÊÔƠƯĐ]", text):
        return "vi"
    # それ以外は英語
    return "en"


def strip_for_lang_detect(text: str) -> str:
    """言語判定用にコード・URL を除いた本文を返す。"""
    # 空はそのまま
    if not text:
        return ""
    # コードフェンスを落とす
    cleaned = _CODE_FENCE_RE.sub(" ", text)
    # インラインコードを落とす
    cleaned = _INLINE_CODE_RE.sub(" ", cleaned)
    # URL を落とす
    cleaned = _URL_RE.sub(" ", cleaned)
    # 空白を整える
    return re.sub(r"\s+", " ", cleaned).strip()


def _count_scripts(text: str) -> dict[str, int]:
    """主要な文字種の出現数を数える。"""
    # 集計表を初期化する
    counts = {"ja_kana": 0, "han": 0, "ko": 0, "latin": 0, "vi": 0}
    # 1 文字ずつ分類する
    for ch in text:
        # ひらがな・カタカナ
        if "\u3040" <= ch <= "\u30ff":
            counts["ja_kana"] += 1
        # CJK 漢字
        elif "\u4e00" <= ch <= "\u9fff":
            counts["han"] += 1
        # ハングル
        elif "\uac00" <= ch <= "\ud7af":
            counts["ko"] += 1
        # ベトナム特有字
        elif ch in "ăâêôơưđĂÂÊÔƠƯĐ":
            counts["vi"] += 1
        # ラテン文字
        elif ch.isascii() and ch.isalpha():
            counts["latin"] += 1
    # 集計結果を返す
    return counts


def response_matches_lang(text: str, expected: str) -> bool:
    """応答本文が期待言語とおおむね一致するか。

    極端に短い／コード主体の場合は一致扱い（誤リトライ抑制）。
    """
    # 期待言語を正規化する
    expected_norm = (expected or "en").strip()
    # 未知コードは緩く一致扱い
    if expected_norm not in VALID_LANGS:
        return True
    # 判定用本文を作る
    cleaned = strip_for_lang_detect(text or "")
    # 短すぎる場合はリトライしない
    if len(cleaned) < _MIN_DETECT_CHARS:
        return True
    # 文字種別を数える
    counts = _count_scripts(cleaned)
    # 日本語期待: かな／漢字が必要
    if expected_norm == "ja":
        return (counts["ja_kana"] + counts["han"]) > 0
    # 韓国語期待: ハングルが必要
    if expected_norm == "ko":
        return counts["ko"] > 0
    # 中国語期待: 漢字があり、かながほぼ無い
    if expected_norm in ("zh-CN", "zh-TW"):
        return counts["han"] > 0 and counts["ja_kana"] == 0
    # ベトナム語期待: 声調字またはラテン主体でかなが無い
    if expected_norm == "vi":
        if counts["ja_kana"] > 0 or counts["ko"] > 0:
            return False
        return counts["vi"] > 0 or counts["latin"] > 0
    # 英語等: かな／ハングルが支配的なら不一致
    if expected_norm == "en":
        # かなが一定以上あれば日本語混入とみなす
        if counts["ja_kana"] >= 3:
            return False
        # ハングルが一定以上あれば韓国語混入とみなす
        if counts["ko"] >= 3:
            return False
        # それ以外は一致扱い（漢字混じりの英説明は許容）
        return True
    # その他は一致扱い
    return True
