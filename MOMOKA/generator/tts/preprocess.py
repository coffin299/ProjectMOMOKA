import re
from typing import Optional

from .english_reading import apply_english_reading

try:
    import pyopenjtalk
except Exception:  # pragma: no cover - optional dependency at runtime
    pyopenjtalk = None

# 全角記号から半角記号への変換マッピング
FULLWIDTH_TO_HALFWIDTH = {
    '！': '!',
    '？': '?',
    '。': '.',
    '、': ',',
    '：': ':',
    '；': ';',
    '（': '(',
    '）': ')',
    '［': '[',
    '］': ']',
    '｛': '{',
    '｝': '}',
    '＜': '<',
    '＞': '>',
    '＆': '&',
    '＊': '*',
    '＠': '@',
    '＃': '#',
    '＄': '$',
    '％': '%',
    '＾': '^',
    '＿': '_',
    '＝': '=',
    '＋': '+',
    '－': '-',
    '／': '/',
    '＼': '\\',
    '｜': '|',
    '～': '~',
    '｀': '`',
    '＂': '"',
    '＇': "'",
}


def normalize_text(text: str, dictionary_dir: Optional[str] = None) -> str:
    """Normalize Japanese text for TTS.

    - Strips URLs
    - Converts fullwidth punctuation to halfwidth
    - Rewrites English words as katakana via kanalizer
    - Removes characters that may cause issues with TTS processing
    - Collapses whitespace
    - Optionally uses pyopenjtalk for reading normalization if available
    - Keeps it conservative to avoid overprocessing
    """
    if not text:
        return ''

    # Remove URLs
    text = re.sub(r'https?://\S+', ' ', text)
    
    # 全角記号を半角に変換
    for fullwidth, halfwidth in FULLWIDTH_TO_HALFWIDTH.items():
        text = text.replace(fullwidth, halfwidth)

    # 記号を消す前に英単語を kanalizer のカタカナ読みへ置換する
    text = apply_english_reading(text)

    # TTSで問題を起こす可能性のある記号を除去（スペースに置換）
    # カタカナ変換で問題になる記号をフィルタリング
    text = re.sub(r'[!?.,;:()[\]{}<>&*@#$%^_=+\-/\\|~`"\']', ' ', text)
    
    text = re.sub(r'\s+', ' ', text).strip()

    if not text:
        return ''

    # Optional pyopenjtalk normalization
    if pyopenjtalk is not None:
        try:
            if dictionary_dir:
                # If user provided dictionary dir (e.g., pyopenjtalk-dict), try loaddic
                try:
                    pyopenjtalk.set_user_dict(dictionary_dir)  # type: ignore[attr-defined]
                except Exception:
                    pass
            # Convert to phoneme-like reading for better robustness
            # Note: pyopenjtalk.g2p returns phoneme sequence; we keep original text here
            # but g2p can be useful for downstream models expecting phonemes.
            # We return original text to leave flexibility to synthesizer side.
            _ = pyopenjtalk.g2p(text)  # ensure dictionary works; ignore output for now
        except Exception:
            # If pyopenjtalk fails, continue with original text
            pass

    return text


