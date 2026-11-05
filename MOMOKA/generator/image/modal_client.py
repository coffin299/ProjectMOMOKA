# Bot からデプロイ済み Modal アプリを呼ぶクライアント。
# modal パッケージは生成時だけ import する。

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# 進捗コールバック。step, total, 表示用ステータス。
ProgressCallback = Callable[[int, int, str], Awaitable[None]]


class ModalImageError(RuntimeError):
    """利用者が読める Modal 画像生成の失敗。"""


class ModalImageClient:
    """YAML のトークンで T4 クラスを呼び、進捗と PNG を受け取る。"""

    def __init__(self, modal_config: dict[str, Any], repo_id: str) -> None:
        # 実行用 YAML の modal 節。
        self.config = modal_config or {}
        # 表示用・ダウンロード用のモデル ID。
        self.repo_id = repo_id
        # デプロイ名。
        self.app_name = str(self.config.get("app_name") or "momoka-image")
        # GPU クラス名。
        self.class_name = str(self.config.get("class_name") or "IllustriousGenerator")
        # CPU ダウンロード関数名。
        self.download_name = str(self.config.get("download_function") or "download_weights")
        # Volume 上のファイル名。
        self.filename = str(
            self.config.get("weights_filename") or "Illustrious-XL-v2.0.safetensors"
        )
        # 空なら公開モデルとしてトークンなし。
        self.hf_token = str(self.config.get("hf_token") or "").strip()
        # 1 ジョブの待ち秒。
        self.timeout = float(self.config.get("timeout") or 600)
        # トークンはプロパティ経由で都度読む。
        self._client: Any = None
        # 一度 Volume に載ったあとは CPU 関数を呼ばない。
        self._weights_ready = False

    def _token_pair(self) -> tuple[str, str]:
        # YAML の ID。
        token_id = str(self.config.get("token_id") or "").strip()
        # YAML のシークレット。
        token_secret = str(self.config.get("token_secret") or "").strip()
        # プレースホルダは未設定。
        if token_id in {"", "YOUR_MODAL_TOKEN_ID"} or token_secret in {
            "",
            "YOUR_MODAL_TOKEN_SECRET",
        }:
            raise ModalImageError(
                "Modal token is missing in configs/image_generator_config.yaml "
                "(modal.token_id / modal.token_secret).\n"
                "configs/image_generator_config.yaml の modal.token_id と "
                "modal.token_secret を設定してください。"
            )
        return token_id, token_secret

    def _sdk(self) -> Any:
        # ローカル生成だけの起動では modal を読まない。
        try:
            import modal
        except ImportError as exc:
            raise ModalImageError(
                "Install the modal package in the existing environment: pip install modal\n"
                "既存環境で pip install modal してください。venv は作らないでください。"
            ) from exc
        return modal

    def _modal_client(self) -> Any:
        # 同じトークンのクライアントを使い回す。
        if self._client is not None:
            return self._client
        # YAML から資格情報を作る。
        token_id, token_secret = self._token_pair()
        # プロセスの modal setup には依存しない。
        self._client = self._sdk().Client.from_credentials(token_id, token_secret)
        return self._client

    def _explain(self, exc: Exception) -> ModalImageError:
        # 既に翻訳済みならそのまま。
        if isinstance(exc, ModalImageError):
            return exc
        # サーバ側の文言。
        text = str(exc)
        # 小文字で分類する。
        lowered = text.lower()
        # アプリ未デプロイ。
        if "not found" in lowered or "lookup" in lowered:
            return ModalImageError(
                "Modal app is not deployed. From the repo root run: "
                "modal deploy MOMOKA/generator/image/modal_app.py\n"
                "Modal アプリが未デプロイです。リポジトリルートで "
                "modal deploy MOMOKA/generator/image/modal_app.py を実行してください。"
            )
        # トークン拒否。
        if "token" in lowered or "unauth" in lowered or "401" in lowered:
            return ModalImageError(
                "Modal rejected token_id / token_secret from "
                "configs/image_generator_config.yaml.\n"
                "configs/image_generator_config.yaml の Modal トークンが拒否されました。"
            )
        # Volume に重みが無い。
        if "checkpoint" in lowered:
            return ModalImageError(
                "Checkpoint is not on the Modal volume. Run: "
                "modal run MOMOKA/generator/image/modal_app.py\n"
                "Modal Volume に重みがありません。"
                "modal run MOMOKA/generator/image/modal_app.py を実行してください。"
            )
        # それ以外は原文を残す。
        return ModalImageError(
            f"Modal image generation failed: {text}\nModal での画像生成に失敗しました: {text}"
        )

    async def ensure_weights(self, *, force: bool = False) -> None:
        """CPU 関数で重みの有無を確認し、無ければダウンロードする。"""
        # 成功済みなら GPU の前に CPU コンテナを起動しない。
        if self._weights_ready and not force:
            return
        # SDK を取る。
        modal = self._sdk()
        # デプロイ済みの CPU 関数。
        function = modal.Function.from_name(
            self.app_name,
            self.download_name,
            client=self._modal_client(),
        )
        try:
            # GPU を付けずに取得する。
            status = await function.remote.aio(
                self.repo_id,
                self.filename,
                self.hf_token,
            )
        except Exception as exc:  # noqa: BLE001
            # 未デプロイやトークンエラーに翻訳する。
            raise self._explain(exc) from exc
        # 想定外の戻りは失敗。
        if status not in {"ready", "downloaded"}:
            raise ModalImageError(
                f"Unexpected weight download status: {status}\n"
                f"重み取得の戻り値が想定外です: {status}"
            )
        # 次回以降はスキップする。
        self._weights_ready = True
        # 初回だけログを残す。
        logger.info("Modal weights status: %s", status)

    async def generate(
        self,
        *,
        prompt: str,
        negative_prompt: str,
        width: int,
        height: int,
        steps: int,
        cfg_scale: float,
        seed: int,
        sampler_name: str,
        on_progress: Optional[ProgressCallback] = None,
    ) -> bytes:
        """重み確認のあと T4 で生成し、PNG バイトを返す。"""
        # 進捗が無いときは空呼び出し。
        async def report(step: int, total: int, status: str) -> None:
            # コールバック未設定なら何もしない。
            if on_progress is None:
                return
            # Discord 更新へ渡す。
            await on_progress(step, total, status)

        # ダウンロード中であることが分かるようにする。
        await report(0, steps, "Generating... / 重みを確認中")
        # CPU で Volume を用意する。
        await self.ensure_weights()
        # ここから先は GPU のコールドスタート。
        await report(0, steps, "Generating... / GPU 起動中")
        try:
            # 1 回目。
            png = await self._generate_once(
                prompt=prompt,
                negative_prompt=negative_prompt,
                width=width,
                height=height,
                steps=steps,
                cfg_scale=cfg_scale,
                seed=seed,
                sampler_name=sampler_name,
                report=report,
            )
        except ModalImageError as exc:
            # 重み欠落のときだけ CPU 取得をやり直す。
            if "Checkpoint" not in str(exc):
                raise
            # キャッシュを捨てる。
            self._weights_ready = False
            # もう一度 Volume を確認する。
            await self.ensure_weights(force=True)
            # GPU を起動し直す。
            await report(0, steps, "Generating... / GPU 起動中")
            png = await self._generate_once(
                prompt=prompt,
                negative_prompt=negative_prompt,
                width=width,
                height=height,
                steps=steps,
                cfg_scale=cfg_scale,
                seed=seed,
                sampler_name=sampler_name,
                report=report,
            )
        return png

    async def _generate_once(
        self,
        *,
        prompt: str,
        negative_prompt: str,
        width: int,
        height: int,
        steps: int,
        cfg_scale: float,
        seed: int,
        sampler_name: str,
        report: ProgressCallback,
    ) -> bytes:
        # SDK。
        modal = self._sdk()
        # デプロイ済みクラス。
        generator_cls = modal.Cls.from_name(
            self.app_name,
            self.class_name,
            client=self._modal_client(),
        )
        # 呼び出しインスタンス。
        generator = generator_cls()
        # 最後に届いた PNG。
        png: Optional[bytes] = None
        try:
            # ステップと画像を順に受け取る。
            stream = generator.generate.remote_gen.aio(
                prompt,
                negative_prompt,
                int(width),
                int(height),
                int(steps),
                float(cfg_scale),
                int(seed),
                sampler_name or "",
            )
            # 各イベントを進捗か画像に振り分ける。
            async for event in stream:
                # dict 以外は無視する。
                if not isinstance(event, dict):
                    continue
                # ステップ。
                if event.get("type") == "progress":
                    await report(
                        int(event.get("step") or 0),
                        int(event.get("total") or steps),
                        "Generating... / 生成中...",
                    )
                    continue
                # 完成画像。
                if event.get("type") == "image":
                    raw = event.get("png")
                    # bytes へ揃える。
                    png = bytes(raw) if raw is not None else None
        except Exception as exc:  # noqa: BLE001
            # 利用者向けの文へ翻訳する。
            raise self._explain(exc) from exc
        # 画像が無ければ失敗。
        if not png:
            raise ModalImageError(
                "Modal returned no image.\nModal から画像が返りませんでした。"
            )
        return png
