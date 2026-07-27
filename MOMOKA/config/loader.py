# カテゴリ別 configs/ を読み込み、実行時用のマージ済み dict を返す。
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# リポジトリルートからの相対パス
CONFIGS_DIR_NAME = "configs"

# 読み込むカテゴリ（順序はログ用。キー衝突時は後勝ち）
CATEGORIES: List[str] = [
    "core",
    "bots",
    "llm",
    # LLM 画像生成（旧 llm.image_generator）
    "image_generator",
    "music",
    "tts",
    "images",
    "notifications",
    "tracker",
    "utilities",
    "link_fix",
    "count",
    # スラッシュコマンド description の多言語カタログ
    "commands_i18n",
]

# *_config.yaml を生成せず、*_config.default.yaml を直接読むカテゴリ
# （ユーザー編集不要のカタログ系。default 更新が即反映される）
DEFAULT_ONLY_CATEGORIES = frozenset({"commands_i18n"})

# プレースホルダ判定用
_TOKEN_PLACEHOLDERS = {
    "YOUR_PLANA_BOT_TOKEN",
    "YOUR_ARONA_BOT_TOKEN",
    "YOUR_BOT_TOKEN_HERE",
    "",
}


def _project_root() -> Path:
    # MOMOKA/config/loader.py → リポジトリルート
    return Path(__file__).resolve().parents[2]


def configs_dir(root: Optional[Path] = None) -> Path:
    # configs ディレクトリの絶対パスを返す
    return (root or _project_root()) / CONFIGS_DIR_NAME


def ensure_default_configs(root: Optional[Path] = None) -> None:
    """default があり yaml が無いカテゴリだけコピーする。"""
    # ルートと configs ディレクトリを確定する
    base = configs_dir(root)
    # ディレクトリが無ければ作成する
    base.mkdir(parents=True, exist_ok=True)
    # 各カテゴリを走査する
    for category in CATEGORIES:
        # default 直読みカテゴリはコピーしない
        if category in DEFAULT_ONLY_CATEGORIES:
            continue
        # default / 実ファイルのパスを組み立てる
        default_path = base / f"{category}_config.default.yaml"
        runtime_path = base / f"{category}_config.yaml"
        # default が無ければスキップ（実装漏れの可能性）
        if not default_path.exists():
            # 警告を出して次へ進む
            logger.warning("Missing default config: %s", default_path)
            continue
        # 実行用 yaml が既にあれば触らない
        if runtime_path.exists():
            continue
        # default からコピーする
        shutil.copyfile(default_path, runtime_path)
        # 生成を知らせる
        logger.info("Generated %s from %s", runtime_path.name, default_path.name)


def _load_yaml(path: Path) -> Dict[str, Any]:
    """YAML ファイルを dict として読む。空なら {}。"""
    # ファイルを開いてパースする
    with path.open("r", encoding="utf-8") as f:
        # YAML を読み込む
        data = yaml.safe_load(f)
    # None / 非 dict は空にする
    if not isinstance(data, dict):
        return {}
    # 読み込んだ dict を返す
    return data


def load_merged_config(root: Optional[Path] = None) -> Dict[str, Any]:
    """全カテゴリ yaml をマージして返す。先に ensure_default_configs を呼ぶ。"""
    # 不足分をコピーする
    ensure_default_configs(root)
    # マージ先を用意する
    merged: Dict[str, Any] = {}
    # configs ディレクトリ
    base = configs_dir(root)
    # カテゴリ順に読み込む
    for category in CATEGORIES:
        # default 直読みカテゴリは常に *.default.yaml
        if category in DEFAULT_ONLY_CATEGORIES:
            runtime_path = base / f"{category}_config.default.yaml"
        else:
            # 実行用パス
            runtime_path = base / f"{category}_config.yaml"
            # 無ければ default を試す
            if not runtime_path.exists():
                runtime_path = base / f"{category}_config.default.yaml"
        # どちらも無ければスキップ
        if not runtime_path.exists():
            logger.error("Config not found for category '%s'", category)
            continue
        # 読み込む
        data = _load_yaml(runtime_path)
        # 浅いマージ（トップレベルキー後勝ち）
        merged.update(data)
        # 読込ログ
        logger.info("Loaded config category '%s' from %s", category, runtime_path.name)
    # マージ結果を返す
    return merged


def validate_bot_tokens(config: Dict[str, Any]) -> None:
    """bots.*.token がプレースホルダでないことを確認する。失敗時は ValueError。"""
    # bots セクションを取得する
    bots = config.get("bots") or {}
    # dict でなければエラー
    if not isinstance(bots, dict) or not bots:
        raise ValueError("bots_config: 'bots' section is missing or empty.")
    # 各 Bot を検査する
    for bot_id, bot_cfg in bots.items():
        # エントリが dict か確認する
        if not isinstance(bot_cfg, dict):
            raise ValueError(f"bots.{bot_id} must be a mapping.")
        # token を取り出す
        token = str(bot_cfg.get("token") or "").strip()
        # プレースホルダなら拒否する
        if token in _TOKEN_PLACEHOLDERS or token.startswith("YOUR_"):
            raise ValueError(
                f"bots.{bot_id}.token is unset or still a placeholder. "
                f"Edit configs/bots_config.yaml."
            )


def get_bot_entry(config: Dict[str, Any], bot_id: str) -> Dict[str, Any]:
    """bots.<bot_id> の設定 dict を返す。"""
    # bots から取り出す
    bots = config.get("bots") or {}
    # エントリを取得する
    entry = bots.get(bot_id)
    # 無ければエラー
    if not isinstance(entry, dict):
        raise KeyError(f"bots.{bot_id} not found in config.")
    # 返す
    return entry
