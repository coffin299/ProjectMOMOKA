# ログビューア本体（tkinter）

import asyncio
import json
import os
import queue
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from MOMOKA.GUI.bot_bridge import get_bot_ref
from MOMOKA.GUI.theme import (
    apply_windows_dark_mode_to_foreground,
    get_theme_colors,
)
from MOMOKA.GUI.version import COPYRIGHT, LOG_VIEWER_NAME, VERSION


class LogViewerApp:
    """MOMOKA ログビューアのメインウィンドウ。"""

    def __init__(self, root: tk.Tk, log_queue: queue.Queue):
        # Tk ルートウィンドウ
        self.root = root
        # ログキュー（logging_bridge と共有）
        self.log_queue = log_queue
        # ウィンドウタイトルを設定する
        self.root.title(LOG_VIEWER_NAME)
        # 初期化中は非表示にしてちらつきを防ぐ
        self.root.withdraw()
        # Windows ダークモードを先に適用する
        self.apply_windows_dark_mode()
        # 初期サイズを設定する
        self.root.geometry("1200x800")
        # テーマカラー辞書を取得する
        self.theme = get_theme_colors()
        # メインウィンドウの背景色を設定する
        self.root.configure(bg=self.theme["bg"])
        # 設定ファイルパス
        self.config_file = "data/log_viewer_config.json"
        # 設定を読み込む
        self.load_config()
        # ttk スタイルを保持する
        self.style = ttk.Style()
        # スタイルを初期化する
        self.setup_styles()
        # メニューバーを作る
        self.create_menu()
        # ウィジェットを配置する
        self.setup_gui()
        # キューを定期的にチェックする
        self.poll_log_queue()
        # VC / LLM 稼働数を定期更新する
        self.poll_status()
        # ウィンドウクローズ時の処理を登録する
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        # 準備完了後に再表示する
        self.root.deiconify()

    def apply_windows_dark_mode(self) -> None:
        """Windows のダークモード設定を適用する。"""
        # テーマモジュールの共通処理に委譲する
        apply_windows_dark_mode_to_foreground()

    def create_menu(self) -> None:
        """メニューバーの作成。"""
        # ルートメニューを作る
        self.menubar = tk.Menu(
            self.root,
            bg=self.theme["bg"],
            fg=self.theme["fg"],
            activebackground=self.theme["select_bg"],
            activeforeground=self.theme["select_fg"],
            relief="flat",
            bd=0,
        )
        # ファイルメニュー
        file_menu = tk.Menu(
            self.menubar,
            tearoff=0,
            bg=self.theme["bg"],
            fg=self.theme["fg"],
            activebackground=self.theme["select_bg"],
            activeforeground=self.theme["select_fg"],
            bd=1,
            relief="solid",
        )
        # 終了コマンドを追加する
        file_menu.add_command(
            label="終了",
            command=self.root.quit,
            activebackground=self.theme["select_bg"],
            activeforeground=self.theme["select_fg"],
        )
        # ファイルカスケードを載せる
        self.menubar.add_cascade(label="ファイル", menu=file_menu)
        # 表示メニュー
        view_menu = tk.Menu(
            self.menubar,
            tearoff=0,
            bg=self.theme["bg"],
            fg=self.theme["fg"],
            activebackground=self.theme["select_bg"],
            activeforeground=self.theme["select_fg"],
            bd=1,
            relief="solid",
        )
        # 自動スクロールの状態変数を初期化する
        self.auto_scroll_var = tk.BooleanVar(value=self.config.get("auto_scroll", True))
        # チェックメニューを追加する
        view_menu.add_checkbutton(
            label="自動スクロール",
            variable=self.auto_scroll_var,
            command=self.toggle_auto_scroll,
            activebackground=self.theme["select_bg"],
            activeforeground=self.theme["select_fg"],
        )
        # 表示カスケードを載せる
        self.menubar.add_cascade(label="表示", menu=view_menu)
        # ヘルプメニュー
        help_menu = tk.Menu(
            self.menubar,
            tearoff=0,
            bg=self.theme["bg"],
            fg=self.theme["fg"],
            activebackground=self.theme["select_bg"],
            activeforeground=self.theme["select_fg"],
            bd=1,
            relief="solid",
        )
        # バージョン情報ダイアログを開く
        help_menu.add_command(
            label="バージョン情報",
            command=self.show_about,
            activebackground=self.theme["select_bg"],
            activeforeground=self.theme["select_fg"],
        )
        # ヘルプカスケードを載せる
        self.menubar.add_cascade(label="ヘルプ", menu=help_menu)
        # メニューバーをウィンドウへ設定する
        self.root.config(menu=self.menubar)
        # メニューのスタイルオプションを全体に適用する
        self.root.option_add("*Menu*background", self.theme["bg"])
        self.root.option_add("*Menu*foreground", self.theme["fg"])
        self.root.option_add("*Menu*activeBackground", self.theme["select_bg"])
        self.root.option_add("*Menu*activeForeground", self.theme["select_fg"])

    def setup_styles(self) -> None:
        """ttk スタイルの初期化のみを行う。"""
        # clam テーマを使う
        self.style.theme_use("clam")
        # フレーム
        self.style.configure(
            "TFrame",
            background=self.theme["bg"],
            borderwidth=0,
        )
        # ラベル
        self.style.configure(
            "TLabel",
            background=self.theme["bg"],
            foreground=self.theme["fg"],
            font=("Meiryo UI", 9),
            padding=2,
        )
        # ボタン
        self.style.configure(
            "TButton",
            background=self.theme["button_bg"],
            foreground=self.theme["button_fg"],
            borderwidth=1,
            relief="raised",
            padding=5,
        )
        # ボタンのホバー / 押下
        self.style.map(
            "TButton",
            background=[
                ("active", self.theme["button_active_bg"]),
                ("pressed", self.theme["select_bg"]),
            ],
            foreground=[
                ("active", self.theme["button_active_fg"]),
                ("pressed", self.theme["select_fg"]),
            ],
            relief=[("pressed", "sunken"), ("!pressed", "raised")],
        )
        # エントリー
        self.style.configure(
            "TEntry",
            fieldbackground=self.theme["entry_bg"],
            foreground=self.theme["entry_fg"],
            insertcolor=self.theme["insert_fg"],
            borderwidth=1,
            relief="solid",
        )
        # コンボボックス
        self.style.configure(
            "TCombobox",
            fieldbackground=self.theme["entry_bg"],
            background=self.theme["entry_bg"],
            foreground=self.theme["entry_fg"],
            selectbackground=self.theme["select_bg"],
            selectforeground=self.theme["select_fg"],
            arrowcolor=self.theme["fg"],
            borderwidth=1,
            relief="solid",
        )
        # readonly 時の色
        self.style.map(
            "TCombobox",
            fieldbackground=[("readonly", self.theme["entry_bg"])],
            selectbackground=[("readonly", self.theme["select_bg"])],
            selectforeground=[("readonly", self.theme["select_fg"])],
        )
        # 縦スクロールバー
        self.style.configure(
            "Vertical.TScrollbar",
            background=self.theme["scrollbar_bg"],
            troughcolor=self.theme["scrollbar_trough"],
            arrowcolor=self.theme["fg"],
            bordercolor=self.theme["bg"],
            darkcolor=self.theme["bg"],
            lightcolor=self.theme["bg"],
            gripcount=0,
            arrowsize=12,
        )
        # 横スクロールバー
        self.style.configure(
            "Horizontal.TScrollbar",
            background=self.theme["scrollbar_bg"],
            troughcolor=self.theme["scrollbar_trough"],
            arrowcolor=self.theme["fg"],
            bordercolor=self.theme["bg"],
            darkcolor=self.theme["bg"],
            lightcolor=self.theme["bg"],
            gripcount=0,
            arrowsize=12,
        )
        # アクティブ時のスクロールバー色
        self.style.map(
            "Vertical.TScrollbar",
            background=[("active", self.theme["scrollbar_bg"])],
        )
        # LabelFrame
        self.style.configure(
            "TLabelframe",
            background=self.theme["bg"],
            foreground=self.theme["fg"],
            relief="groove",
            borderwidth=2,
        )
        self.style.configure(
            "TLabelframe.Label",
            background=self.theme["bg"],
            foreground=self.theme["fg"],
        )
        # チェックボタン
        self.style.configure(
            "TCheckbutton",
            background=self.theme["bg"],
            foreground=self.theme["fg"],
            indicatorbackground=self.theme["bg"],
            indicatorcolor=self.theme["fg"],
            selectcolor=self.theme["bg"],
        )
        self.style.map(
            "TCheckbutton",
            background=[("active", self.theme["bg"])],
            foreground=[("active", self.theme["fg"])],
        )
        # ラジオボタン
        self.style.configure(
            "TRadiobutton",
            background=self.theme["bg"],
            foreground=self.theme["fg"],
            indicatorbackground=self.theme["bg"],
            indicatorcolor=self.theme["fg"],
            selectcolor=self.theme["bg"],
        )
        self.style.map(
            "TRadiobutton",
            background=[("active", self.theme["bg"])],
            foreground=[("active", self.theme["fg"])],
        )
        # メニューボタン
        self.style.configure("TMenubutton", borderwidth=2)

    def load_config(self) -> None:
        """設定ファイルの読み込み。"""
        # デフォルト設定
        self.config = {
            "font": ("Meiryo UI", 9),
            "max_lines": 1000,
            "auto_scroll": True,
            "log_levels": {
                "general": "INFO",
                "llm": "INFO",
                "tts": "INFO",
                "error": "WARNING",
            },
        }
        try:
            # ファイルが存在すればマージする
            if os.path.exists(self.config_file):
                with open(self.config_file, "r", encoding="utf-8") as f:
                    # JSON を読む
                    saved_config = json.load(f)
                    # デフォルトへ上書きマージする
                    self.config.update(saved_config)
        except Exception as e:
            # 破損時はデフォルトのまま続ける
            print(f"設定ファイルの読み込み中にエラーが発生しました: {e}")

    def save_config(self) -> None:
        """設定ファイルの保存。"""
        try:
            # 親ディレクトリを作る
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
            # UTF-8 JSON で書き出す
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            # 保存失敗はステータス以外に出す
            print(f"設定ファイルの保存中にエラーが発生しました: {e}")

    def setup_gui(self) -> None:
        """GUI ウィジェットの作成。"""
        # メインフレーム
        main_frame = ttk.Frame(self.root, padding="5", style="TFrame")
        main_frame.pack(fill=tk.BOTH, expand=True)
        # コントロールフレーム
        control_frame = ttk.Frame(main_frame, padding="5", style="TFrame")
        control_frame.pack(fill=tk.X, padx=5, pady=5)
        # ログレベル選択枠
        log_level_frame = ttk.LabelFrame(control_frame, text="ログレベル", padding=5)
        log_level_frame.pack(side=tk.LEFT, padx=5, pady=5)
        # ログレベル用の変数を初期化する
        self.general_level_var = tk.StringVar(
            value=self.config["log_levels"].get("general", "INFO")
        )
        self.llm_level_var = tk.StringVar(
            value=self.config["log_levels"].get("llm", "INFO")
        )
        self.tts_level_var = tk.StringVar(
            value=self.config["log_levels"].get("tts", "INFO")
        )
        self.error_level_var = tk.StringVar(
            value=self.config["log_levels"].get("error", "WARNING")
        )
        # 一般ログレベル
        ttk.Label(log_level_frame, text="一般:").grid(
            row=0, column=0, padx=2, pady=2, sticky=tk.W
        )
        general_level = ttk.Combobox(
            log_level_frame,
            textvariable=self.general_level_var,
            values=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            state="readonly",
            width=10,
        )
        general_level.grid(row=0, column=1, padx=2, pady=2)
        general_level.bind(
            "<<ComboboxSelected>>",
            lambda e: self.update_log_level("general", self.general_level_var.get()),
        )
        # LLM ログレベル
        ttk.Label(log_level_frame, text="LLM:").grid(
            row=0, column=2, padx=2, pady=2, sticky=tk.W
        )
        llm_level = ttk.Combobox(
            log_level_frame,
            textvariable=self.llm_level_var,
            values=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            state="readonly",
            width=10,
        )
        llm_level.grid(row=0, column=3, padx=2, pady=2)
        llm_level.bind(
            "<<ComboboxSelected>>",
            lambda e: self.update_log_level("llm", self.llm_level_var.get()),
        )
        # TTS+Music ログレベル
        ttk.Label(log_level_frame, text="TTS+Music:").grid(
            row=0, column=4, padx=2, pady=2, sticky=tk.W
        )
        tts_level = ttk.Combobox(
            log_level_frame,
            textvariable=self.tts_level_var,
            values=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            state="readonly",
            width=10,
        )
        tts_level.grid(row=0, column=5, padx=2, pady=2)
        tts_level.bind(
            "<<ComboboxSelected>>",
            lambda e: self.update_log_level("tts", self.tts_level_var.get()),
        )
        # エラーログレベル
        ttk.Label(log_level_frame, text="エラー:").grid(
            row=0, column=6, padx=2, pady=2, sticky=tk.W
        )
        error_level = ttk.Combobox(
            log_level_frame,
            textvariable=self.error_level_var,
            values=["WARNING", "ERROR", "CRITICAL"],
            state="readonly",
            width=10,
        )
        error_level.grid(row=0, column=7, padx=2, pady=2)
        error_level.bind(
            "<<ComboboxSelected>>",
            lambda e: self.update_log_level("error", self.error_level_var.get()),
        )
        # ボタンフレーム
        button_frame = ttk.Frame(control_frame, style="TFrame")
        button_frame.pack(side=tk.RIGHT, padx=5, pady=5)
        # VC / LLM 稼働数表示（起動前はプレースホルダ）
        self.vc_status_var = tk.StringVar(value="VC: -")
        self.llm_status_var = tk.StringVar(value="LLM: -")
        # VC 接続中ギルド数ラベル
        ttk.Label(
            button_frame,
            textvariable=self.vc_status_var,
            style="TLabel",
        ).pack(side=tk.LEFT, padx=6)
        # LLM 生成中ギルド数ラベル
        ttk.Label(
            button_frame,
            textvariable=self.llm_status_var,
            style="TLabel",
        ).pack(side=tk.LEFT, padx=6)
        # クリアボタン
        clear_button = ttk.Button(
            button_frame,
            text="ログをクリア",
            command=self.clear_all_logs,
        )
        clear_button.pack(side=tk.LEFT, padx=2)
        # 自動スクロールチェックボタン
        auto_scroll = ttk.Checkbutton(
            button_frame,
            text="自動スクロール",
            variable=self.auto_scroll_var,
            command=self.toggle_auto_scroll,
        )
        auto_scroll.pack(side=tk.LEFT, padx=2)
        # シャットダウンボタン（二重押し防止のため参照を保持）
        self.shutdown_button = ttk.Button(
            button_frame,
            text="シャットダウン",
            command=self.request_shutdown,
        )
        # 危険操作なので右端に置く
        self.shutdown_button.pack(side=tk.LEFT, padx=6)
        # シャットダウン要求中フラグ
        self._shutdown_requested = False
        # ログ表示エリアのフレーム
        log_frame = ttk.Frame(main_frame, style="TFrame")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        # グリッドの設定
        log_frame.columnconfigure(0, weight=1)
        log_frame.columnconfigure(1, weight=1)
        log_frame.rowconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        # 左上: 一般ログ
        general_frame = ttk.LabelFrame(
            log_frame, text="一般ログ", padding="2", style="TLabelframe"
        )
        general_frame.grid(row=0, column=0, padx=2, pady=2, sticky="nsew")
        self.general_log = scrolledtext.ScrolledText(
            general_frame,
            wrap=tk.WORD,
            width=60,
            height=15,
            font=self.config["font"],
            bg=self.theme["text_bg"],
            fg=self.theme["text_fg"],
            insertbackground=self.theme["fg"],
            selectbackground=self.theme["select_bg"],
            selectforeground=self.theme["select_fg"],
            relief="flat",
        )
        self.general_log.pack(fill=tk.BOTH, expand=True)
        # 右上: LLM ログ
        llm_frame = ttk.LabelFrame(
            log_frame, text="LLMログ", padding="2", style="TLabelframe"
        )
        llm_frame.grid(row=0, column=1, padx=2, pady=2, sticky="nsew")
        self.llm_log = scrolledtext.ScrolledText(
            llm_frame,
            wrap=tk.WORD,
            width=60,
            height=15,
            font=self.config["font"],
            bg=self.theme["text_bg"],
            fg=self.theme["text_fg"],
            insertbackground=self.theme["fg"],
            selectbackground=self.theme["select_bg"],
            selectforeground=self.theme["select_fg"],
            relief="flat",
        )
        self.llm_log.pack(fill=tk.BOTH, expand=True)
        # 左下: TTS+Music ログ
        tts_frame = ttk.LabelFrame(
            log_frame, text="TTS+Musicログ", padding="2", style="TLabelframe"
        )
        tts_frame.grid(row=1, column=0, padx=2, pady=2, sticky="nsew")
        self.tts_log = scrolledtext.ScrolledText(
            tts_frame,
            wrap=tk.WORD,
            width=60,
            height=15,
            font=self.config["font"],
            bg=self.theme["text_bg"],
            fg=self.theme["text_fg"],
            insertbackground=self.theme["fg"],
            selectbackground=self.theme["select_bg"],
            selectforeground=self.theme["select_fg"],
            relief="flat",
        )
        self.tts_log.pack(fill=tk.BOTH, expand=True)
        # 右下: エラーログ
        error_frame = ttk.LabelFrame(
            log_frame, text="エラーログ", padding="2", style="TLabelframe"
        )
        error_frame.grid(row=1, column=1, padx=2, pady=2, sticky="nsew")
        self.error_log = scrolledtext.ScrolledText(
            error_frame,
            wrap=tk.WORD,
            width=60,
            height=15,
            font=self.config["font"],
            bg=self.theme["text_bg"],
            fg=self.theme["error"],
            insertbackground=self.theme["fg"],
            selectbackground=self.theme["select_bg"],
            selectforeground=self.theme["select_fg"],
            relief="flat",
        )
        self.error_log.pack(fill=tk.BOTH, expand=True)
        # ステータスバー
        self.status_var = tk.StringVar()
        self.status_var.set("準備完了")
        status_bar = ttk.Label(
            self.root,
            textvariable=self.status_var,
            relief=tk.SUNKEN,
            anchor=tk.W,
            style="TLabel",
        )
        status_bar.pack(fill=tk.X, side=tk.BOTTOM, ipady=2)
        # コンテキストメニューの設定
        self.setup_context_menu(self.general_log)
        self.setup_context_menu(self.llm_log)
        self.setup_context_menu(self.tts_log)
        self.setup_context_menu(self.error_log)
        # キーバインドの設定
        self.root.bind_all(
            "<Control-c>", lambda e: self.copy_text(self.root.focus_get())
        )
        self.root.bind_all(
            "<Control-a>", lambda e: self.select_all(self.root.focus_get())
        )
        # 初期フォーカスを設定する
        self.general_log.focus_set()

    def setup_context_menu(self, widget) -> None:
        """コンテキストメニューの設定。"""

        def show_menu(event):
            # 右クリックメニューを組み立てる
            menu = tk.Menu(
                self.root,
                tearoff=0,
                bg=self.theme["bg"],
                fg=self.theme["fg"],
                activebackground=self.theme["select_bg"],
                activeforeground=self.theme["select_fg"],
            )
            # コピー
            menu.add_command(label="コピー", command=lambda: self.copy_text(widget))
            # 区切り
            menu.add_separator()
            # 全選択
            menu.add_command(
                label="すべて選択", command=lambda: self.select_all(widget)
            )
            # クリア
            menu.add_command(label="クリア", command=lambda: self.clear_log(widget))
            try:
                # カーソル位置にポップアップする
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                # グラブを解放する
                menu.grab_release()

        # 右クリックにバインドする
        widget.bind("<Button-3>", show_menu)

    def copy_text(self, widget) -> None:
        """選択されたテキストをコピーする。"""
        try:
            # 選択範囲を取得する
            selected_text = widget.get("sel.first", "sel.last")
            # クリップボードをクリアする
            self.root.clipboard_clear()
            # 選択文字列を載せる
            self.root.clipboard_append(selected_text)
            # ステータスを更新する
            self.status_var.set("選択されたテキストをクリップボードにコピーしました")
        except tk.TclError:
            # 未選択時
            self.status_var.set("コピーするテキストが選択されていません")

    def select_all(self, widget):
        """すべてのテキストを選択する。"""
        # 全文を選択範囲にする
        widget.tag_add(tk.SEL, "1.0", tk.END)
        # カーソルを先頭へ
        widget.mark_set(tk.INSERT, "1.0")
        # 先頭が見えるようにする
        widget.see(tk.INSERT)
        # デフォルトの Ctrl+A を抑止する
        return "break"

    def clear_log(self, widget) -> None:
        """指定ウィジェットのログをクリアする。"""
        # 編集可能にする
        widget.config(state="normal")
        # 全文削除
        widget.delete(1.0, tk.END)
        # 再び読み取り専用にする
        widget.config(state="disabled")
        # ステータス更新
        self.status_var.set("ログをクリアしました")

    def clear_all_logs(self) -> None:
        """すべてのログをクリアする。"""
        # 4 パネルすべて消す
        for widget in [self.general_log, self.llm_log, self.tts_log, self.error_log]:
            self.clear_log(widget)
        # まとめて完了を出す
        self.status_var.set("すべてのログをクリアしました")

    def toggle_auto_scroll(self) -> None:
        """自動スクロールの切り替え。"""
        # 設定へ反映する
        self.config["auto_scroll"] = self.auto_scroll_var.get()
        # 永続化する
        self.save_config()
        # 日本語ステータス文言
        status = "有効" if self.auto_scroll_var.get() else "無効"
        # ステータスバーへ出す
        self.status_var.set(f"自動スクロールを{status}にしました")

    def update_log_level(self, log_type: str, level: str) -> None:
        """ログレベルの更新。"""
        # 設定辞書を更新する
        self.config["log_levels"][log_type] = level
        # ディスクへ保存する
        self.save_config()
        # 変更内容をステータスに出す
        self.status_var.set(f"{log_type}のログレベルを{level}に設定しました")

    def poll_log_queue(self) -> None:
        """ログキューを定期的にチェックする。"""
        try:
            # 溜まっている分をまとめて処理する
            while True:
                # 非ブロッキングで 1 件取る
                name, level, log_entry = self.log_queue.get_nowait()
                # パネルへ振り分ける
                self.process_log_entry(name, level, log_entry)
        except queue.Empty:
            # キューが空なら何もしない
            pass
        finally:
            # 100ms 後に再スケジュールする
            self.root.after(100, self.poll_log_queue)

    def process_log_entry(self, name: str, level: str, log_entry: str) -> None:
        """ログエントリを処理し、適切なパネルへ表示する。"""
        # 数値比較用のレベル表
        log_levels = {
            "DEBUG": 10,
            "INFO": 20,
            "WARNING": 30,
            "ERROR": 40,
            "CRITICAL": 50,
        }
        # エラー / CRITICAL はエラーパネルへ
        if level in ["ERROR", "CRITICAL"]:
            widget = self.error_log
            min_level = log_levels.get(self.error_level_var.get(), 30)
            # 閾値以上のみ表示する
            if log_levels.get(level, 0) >= min_level:
                self.append_to_log(widget, log_entry, level)
            return
        # 標準出力は一般ログへ
        if name == "stdout":
            widget = self.general_log
            min_level = log_levels.get(self.general_level_var.get(), 20)
            # stdout は INFO 相当として扱う
            if log_levels.get("INFO", 20) >= min_level:
                self.append_to_log(widget, log_entry, "INFO")
            return
        # LLM モジュール
        if "MOMOKA.llm" in name:
            widget = self.llm_log
            min_level = log_levels.get(self.llm_level_var.get(), 20)
        elif "MOMOKA.tts" in name or "MOMOKA.music" in name:
            # TTS と Music は同一パネルへ
            widget = self.tts_log
            min_level = log_levels.get(self.tts_level_var.get(), 20)
        else:
            # その他は一般ログ
            widget = self.general_log
            min_level = log_levels.get(self.general_level_var.get(), 20)
        # 閾値以上のみ追記する
        if log_levels.get(level, 0) >= min_level:
            self.append_to_log(widget, log_entry, level)

    def append_to_log(self, text_widget, message: str, level=None) -> None:
        """ログをテキストウィジェットに追加する。"""
        # 追記のため一時的に編集可能にする
        text_widget.config(state="normal")
        # 現在行数を取得する
        lines = int(text_widget.index("end-1c").split(".")[0])
        # 上限超過なら古い行を落とす
        if lines > self.config["max_lines"]:
            text_widget.delete(1.0, f"{lines - self.config['max_lines']}.0")
        # ボット識別タグの色付け（変更しない）
        text_widget.tag_config(
            "plana_tag", foreground="#b388ff", font=("Meiryo UI", 9, "bold")
        )
        text_widget.tag_config(
            "arona_tag", foreground="#ff6b9d", font=("Meiryo UI", 9, "bold")
        )
        # USER_INPUT 行は黄緑で日時付き行全体を塗る
        text_widget.tag_config("user_input_tag", foreground="#A9E34B")
        # LLM_RESPONSE 行はシアンで日時付き行全体を塗る
        text_widget.tag_config("llm_output_tag", foreground="#3BC9DB")
        # レベル色タグを用意する（専用マーカーが無い行用）
        text_widget.tag_config("error", foreground=self.theme["error"])
        text_widget.tag_config("warning", foreground=self.theme["warning"])
        text_widget.tag_config("info", foreground=self.theme["info"])
        text_widget.tag_config("debug", foreground=self.theme["debug"])
        # USER_INPUT / LLM_RESPONSE を level 色より優先する
        if "[USER_INPUT]" in message:
            # ユーザー入力行は黄緑
            tag = "user_input_tag"
        elif "[LLM_RESPONSE]" in message:
            # LLM 出力行はシアン
            tag = "llm_output_tag"
        elif level == "ERROR" or level == "CRITICAL":
            # エラーレベル
            tag = "error"
        elif level == "WARNING":
            # 警告レベル
            tag = "warning"
        elif level == "INFO":
            # 情報レベル
            tag = "info"
        elif level == "DEBUG":
            # デバッグレベル
            tag = "debug"
        else:
            # 不明レベルは無色
            tag = None
        # [PLANA] を色分け挿入する（本文は専用色または level 色）
        if "[PLANA]" in message or "[LLM_RESPONSE][PLANA]" in message:
            parts = message.split("[PLANA]")
            for i, part in enumerate(parts):
                if i > 0:
                    # タグ文字列だけ PLANA 色・太字
                    text_widget.insert(tk.END, "[PLANA]", "plana_tag")
                if tag:
                    text_widget.insert(tk.END, part, tag)
                else:
                    text_widget.insert(tk.END, part)
            text_widget.insert(tk.END, "\n")
        elif "[ARONA]" in message or "[LLM_RESPONSE][ARONA]" in message:
            # [ARONA] を色分け挿入する
            parts = message.split("[ARONA]")
            for i, part in enumerate(parts):
                if i > 0:
                    # タグ文字列だけ ARONA 色・太字
                    text_widget.insert(tk.END, "[ARONA]", "arona_tag")
                if tag:
                    text_widget.insert(tk.END, part, tag)
                else:
                    text_widget.insert(tk.END, part)
            text_widget.insert(tk.END, "\n")
        else:
            # ボットタグ無しは行全体を一括挿入する
            if tag:
                text_widget.insert(tk.END, message + "\n", tag)
            else:
                text_widget.insert(tk.END, message + "\n")
        # 自動スクロールが有効なら末尾へ
        if self.config["auto_scroll"]:
            text_widget.see(tk.END)
        # 読み取り専用に戻す
        text_widget.config(state="disabled")

    def show_about(self) -> None:
        """バージョン情報を表示する。"""
        # 子ウィンドウを作る
        about_window = tk.Toplevel(self.root)
        # タイトル
        about_window.title("バージョン情報")
        # 親の上に重ねる
        about_window.transient(self.root)
        # リサイズ不可
        about_window.resizable(False, False)
        # 背景色
        about_window.configure(bg=self.theme["bg"])
        # 中央配置用サイズ
        window_width = 300
        window_height = 150
        # 画面サイズを取得する
        screen_width = about_window.winfo_screenwidth()
        screen_height = about_window.winfo_screenheight()
        # 中央座標を計算する
        x = (screen_width // 2) - (window_width // 2)
        y = (screen_height // 2) - (window_height // 2)
        # ジオメトリを適用する
        about_window.geometry(f"{window_width}x{window_height}+{x}+{y}")
        # version.py の定数から表示文言を組み立てる
        about_text = (
            f"{LOG_VIEWER_NAME}\nバージョン {VERSION}\n\n{COPYRIGHT}"
        )
        # ラベル
        version_label = ttk.Label(
            about_window,
            text=about_text,
            justify=tk.CENTER,
            style="TLabel",
        )
        version_label.pack(expand=True, padx=20, pady=20)
        # OK ボタン
        ok_button = ttk.Button(
            about_window,
            text="OK",
            command=about_window.destroy,
            style="TButton",
        )
        ok_button.pack(pady=(0, 20))
        # モーダルにする
        about_window.grab_set()
        about_window.focus_set()
        about_window.wait_window()

    def poll_status(self) -> None:
        """VC / LLM 稼働ギルド数を約 1 秒ごとに更新する。"""
        try:
            # Bot 参照を橋渡しモジュールから取る
            bot = get_bot_ref()
            # 未生成ならプレースホルダを維持する
            if bot is None:
                self.vc_status_var.set("VC: -")
                self.llm_status_var.set("LLM: -")
            else:
                # MusicCog から接続中ギルド数を取得する
                music_cog = bot.get_cog("music_cog")
                if music_cog is not None and hasattr(
                    music_cog, "get_active_vc_guild_count"
                ):
                    self.vc_status_var.set(
                        f"VC: {music_cog.get_active_vc_guild_count()}"
                    )
                else:
                    self.vc_status_var.set("VC: -")
                # LLMCog から生成中ギルド数を取得する
                llm_cog = bot.get_cog("LLM")
                if llm_cog is not None and hasattr(
                    llm_cog, "get_active_llm_guild_count"
                ):
                    self.llm_status_var.set(
                        f"LLM: {llm_cog.get_active_llm_guild_count()}"
                    )
                else:
                    self.llm_status_var.set("LLM: -")
        except Exception:
            # GUI スレッドを落とさないため表示はそのまま維持する
            pass
        finally:
            # 1 秒後に再スケジュールする
            self.root.after(1000, self.poll_status)

    def request_shutdown(self) -> None:
        """確認後に Bot を /shutdown と同じ経路で閉じる。"""
        # 二重押しを無視する
        if self._shutdown_requested:
            return
        # 誤操作防止の確認ダイアログ
        confirmed = messagebox.askyesno(
            "シャットダウン確認",
            "ボットをシャットダウンしますか？\n利用中ユーザーへ再起動通知が送られます。",
            parent=self.root,
        )
        # キャンセルなら何もしない
        if not confirmed:
            return
        # Bot 未生成なら実行できない
        bot = get_bot_ref()
        if bot is None:
            self.status_var.set("ボット未起動のためシャットダウンできません")
            return
        try:
            # discord.py のイベントループを取得する
            loop = bot.loop
            # ループ未稼働なら安全に中断する
            if loop is None or not loop.is_running():
                self.status_var.set(
                    "イベントループが利用できないためシャットダウンできません"
                )
                return
            # 二重実行を防ぐ
            self._shutdown_requested = True
            # ボタンを無効化する
            self.shutdown_button.config(state=tk.DISABLED)
            # 進行状況をステータスバーへ出す
            self.status_var.set("シャットダウン中...")
            # 両 Bot をレジストリ経由で閉じる（GUI シャットダウン）
            from MOMOKA.bots.registry import registry

            asyncio.run_coroutine_threadsafe(registry.close_all(), loop)
        except Exception as e:
            # 失敗したら再試行できるように戻す
            self._shutdown_requested = False
            # ボタンを再び有効化する
            self.shutdown_button.config(state=tk.NORMAL)
            # 失敗理由をステータスバーへ出す
            self.status_var.set(f"シャットダウン失敗: {e}")

    def on_closing(self) -> None:
        """ウィンドウを閉じる時の処理。"""
        # 設定を保存する
        self.save_config()
        # ウィンドウを閉じる（ボットは継続実行）
        self.root.destroy()
