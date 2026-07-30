# scripts/migrate_json_to_sqlite.py
# data/*.json を data/momoka.db へ取り込む一回限りの移行ツール。
# 実行後も JSON は削除しない。不要になったら本スクリプトのみ削除してよい。
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# リポジトリルートを import パスへ入れる
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    # プロジェクトルートを先頭に追加する
    sys.path.insert(0, str(_ROOT))

from MOMOKA.storage.settings_db import (  # noqa: E402
    DEFAULT_DB_PATH,
    NS_CHANNEL_IMAGE_MODELS,
    NS_CHANNEL_LLM_MODELS,
    NS_EARTHQUAKE_CONFIG,
    NS_GDRIVE_DELETION_SCHEDULE,
    NS_LINK_FIX_SETTINGS,
    NS_LOG_VIEWER_CONFIG,
    NS_LOGGING_CHANNELS,
    NS_RESPONSE_TIMES,
    NS_SPEECH_DICTIONARY,
    NS_SPEECH_SETTINGS,
    NS_TTS_SETTINGS,
    NS_TWITCH_SETTINGS,
    SettingsDB,
)

# namespace → 相対 JSON パス
MIGRATE_MAP = {
    NS_CHANNEL_LLM_MODELS: "data/channel_llm_models.json",
    NS_CHANNEL_IMAGE_MODELS: "data/channel_image_models.json",
    NS_LINK_FIX_SETTINGS: "data/link_fix_settings.json",
    NS_TTS_SETTINGS: "data/tts_settings.json",
    NS_SPEECH_SETTINGS: "data/speech_settings.json",
    NS_SPEECH_DICTIONARY: "data/speech_dictionary.json",
    NS_TWITCH_SETTINGS: "data/twitch_settings.json",
    NS_EARTHQUAKE_CONFIG: "data/earthquake_tsunami_notification_config.json",
    NS_LOGGING_CHANNELS: "data/logging_channels.json",
    NS_RESPONSE_TIMES: "data/response_times.json",
    NS_LOG_VIEWER_CONFIG: "data/log_viewer_config.json",
    NS_GDRIVE_DELETION_SCHEDULE: "data/gdrive_deletion_schedule.json",
}


def _parse_args() -> argparse.Namespace:
    """CLI 引数を解釈する。"""
    # パーサを作る
    parser = argparse.ArgumentParser(
        description="Migrate data/*.json runtime settings into SQLite (data/momoka.db).",
    )
    # dry-run で書込を抑止する
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be imported without writing to the database.",
    )
    # DB パス上書き
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"SQLite path (default: {DEFAULT_DB_PATH})",
    )
    # 解釈結果を返す
    return parser.parse_args()


def main() -> int:
    """移行を実行し、終了コードを返す。"""
    # 引数を取る
    args = _parse_args()
    # 作業ディレクトリはリポジトリルート想定
    root = _ROOT
    # カウンタ
    imported = 0
    skipped = 0
    failed = 0
    # dry-run でなければ DB を開く
    db: SettingsDB | None = None
    if not args.dry_run:
        # SettingsDB でスキーマも用意する
        db = SettingsDB(args.db)
    else:
        # dry-run でもパスを表示する
        print(f"[dry-run] would use DB: {root / args.db}")

    # 各 JSON を走査する
    for namespace, rel_path in MIGRATE_MAP.items():
        # 絶対パスに解決する
        json_path = root / rel_path
        # 無ければスキップ
        if not json_path.exists():
            print(f"[skip] {namespace}: missing {rel_path}")
            skipped += 1
            continue
        try:
            # JSON を読む
            with json_path.open("r", encoding="utf-8") as fp:
                # 文書全体をオブジェクトとして取り込む
                data = json.load(fp)
        except (OSError, json.JSONDecodeError) as exc:
            # 読込失敗を数える
            print(f"[fail] {namespace}: cannot read {rel_path}: {exc}")
            failed += 1
            continue
        # dry-run なら書かずに報告する
        if args.dry_run:
            print(f"[dry-run] {namespace}: would import from {rel_path}")
            imported += 1
            continue
        try:
            # DB へ保存する（assert で型チェッカ向け）
            assert db is not None
            db.save(namespace, data)
            print(f"[ok] {namespace}: imported from {rel_path}")
            imported += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[fail] {namespace}: write failed: {exc}")
            failed += 1

    # サマリを出す
    print("---")
    print(f"imported={imported} skipped={skipped} failed={failed} at={time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("JSON files were NOT deleted (kept as backup).")
    # 失敗があれば非ゼロ
    return 1 if failed else 0


if __name__ == "__main__":
    # プロセス終了コードを返す
    raise SystemExit(main())
