"""python -m yt_dlp の起動時に、Deno / Node の回収パッチを入れる。"""

try:
    # 子プロセス側の Popen を、親と同じ回収処理で包む。
    from MOMOKA.music.plugins.process_tree import install_ytdlp_popen_guard

    # チャレンジ解決プロセスが残らないようにする。
    install_ytdlp_popen_guard()
except Exception:
    # パッチ失敗でも yt-dlp 本体の起動は続ける。
    pass
