# Modal 上で動かす画像生成アプリ。
# Bot プロセスからは import しない。デプロイは `modal deploy MOMOKA/generator/image/modal_app.py`。
# 重みの取得は CPU 関数。GPU は T4 で、生成メソッドが戻ると scaledown_window=2 で落ちる。

from __future__ import annotations

import os
import queue
import threading
from io import BytesIO
from typing import Any, Iterator

import modal

# 公開の Illustrious-XL v2.0。Starter 枠ではこの1本だけを載せる。
REPO_ID = "OnomaAIResearch/Illustrious-XL-v2.0"
# Volume 上のファイル名。Bot 側 YAML の weights_filename と揃える。
WEIGHTS_FILENAME = "Illustrious-XL-v2.0.safetensors"
# マウント先。CPU 関数と GPU クラスで同じ Volume を見る。
VOLUME_PATH = "/models"
# T4 で失敗させない上限。Bot 側モーダルでも同じ値で拒否する。
MAX_SIDE = 1024
MAX_STEPS = 32
# Illustrious の CLIP skip。
CLIP_SKIP = 2
# 6.9GB 未満は未完了とみなす。
MIN_WEIGHTS_BYTES = 6_000_000_000

APP_NAME = "momoka-image"
VOLUME_NAME = "momoka-image-weights"

app = modal.App(APP_NAME)
# 重みの置き場。月 1TiB の Volume 枠に収まる。
weights_volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)

# ダウンロード専用。torch は入れない。
cpu_image = modal.Image.debian_slim(python_version="3.11").pip_install(
    "huggingface_hub"
)
# GPU 推論用。Bot の torch 2.1 / diffusers 0.23 とは別物。
gpu_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "torch",
        extra_index_url="https://download.pytorch.org/whl/cu124",
    )
    .pip_install(
        "diffusers>=0.32.2",
        "transformers",
        "accelerate",
        "safetensors",
        "huggingface_hub",
        "pillow",
    )
)


def _weights_file() -> str:
    # Volume 内のチェックポイント絶対パス。
    return os.path.join(VOLUME_PATH, WEIGHTS_FILENAME)


def _weights_ready() -> bool:
    # サイズが足りるファイルだけ完成扱いにする。
    path = _weights_file()
    # 未作成なら未完了。
    if not os.path.isfile(path):
        return False
    # 途中のファイルは取り直す。
    return os.path.getsize(path) >= MIN_WEIGHTS_BYTES


@app.function(
    image=cpu_image,
    volumes={VOLUME_PATH: weights_volume},
    cpu=1.0,
    memory=2048,
    timeout=1800,
)
def download_weights(repo_id: str, filename: str, hf_token: str = "") -> str:
    """GPU を付けずにチェックポイントを Volume へ置く。"""
    # 既にあれば GPU 時間を使わず戻る。
    if _weights_ready():
        return "ready"
    # 空文字トークンは未指定として扱う。
    token = hf_token.strip() or None
    # 公開リポジトリから単一ファイルを落とす。
    from huggingface_hub import hf_hub_download

    # Volume 直下へ保存する。
    saved = hf_hub_download(
        repo_id=repo_id or REPO_ID,
        filename=filename or WEIGHTS_FILENAME,
        local_dir=VOLUME_PATH,
        token=token,
    )
    # 期待ファイル名と違う場所に落ちたら揃える。
    target = _weights_file()
    # 同一パスなら移動しない。
    if os.path.abspath(saved) != os.path.abspath(target):
        os.replace(saved, target)
    # 次の GPU コンテナから見えるように確定する。
    weights_volume.commit()
    # 呼び出し元が成否を見る。
    if not _weights_ready():
        raise RuntimeError("checkpoint download finished but the file is still missing")
    return "downloaded"


def _apply_clip_skip(pipe: Any, clip_skip: int) -> None:
    # 1 以下は最終層のまま。
    if clip_skip <= 1:
        return
    # CLIP-L の末尾層を外す（A1111 の clip skip と同じ考え方）。
    encoder = pipe.text_encoder.text_model.encoder
    # 残す層数。
    keep = len(encoder.layers) - (clip_skip - 1)
    # 全部消す指定は無視する。
    if keep < 1:
        return
    # 推論で使う層だけ残す。
    encoder.layers = encoder.layers[:keep]


def _apply_sampler(pipe: Any, sampler_name: str) -> None:
    # 未知の名前は Illustrious 向けの Euler a に倒す。
    from diffusers import (
        DPMSolverMultistepScheduler,
        EulerAncestralDiscreteScheduler,
        EulerDiscreteScheduler,
    )

    # 空白と + を除いて照合する。
    key = (sampler_name or "").replace(" ", "").replace("+", "").lower()
    # 既定は Euler ancestral。
    scheduler_cls = EulerAncestralDiscreteScheduler
    # 追加のスケジューラ引数。
    extra: dict[str, Any] = {}
    # Euler（ancestral ではない）。
    if key == "euler":
        scheduler_cls = EulerDiscreteScheduler
    # DPM++ 2M。Karras は名前に含まれるときだけ。
    elif "dpm" in key and "2m" in key:
        scheduler_cls = DPMSolverMultistepScheduler
        extra = {
            "use_karras_sigmas": "karras" in key,
            "algorithm_type": "dpmsolver++",
        }
    # 現在の設定を引き継いで差し替える。
    pipe.scheduler = scheduler_cls.from_config(pipe.scheduler.config, **extra)


@app.cls(
    image=gpu_image,
    gpu="T4",
    cpu=2.0,
    memory=20480,
    volumes={VOLUME_PATH: weights_volume},
    scaledown_window=2,
    timeout=600,
    min_containers=0,
    max_containers=1,
)
@modal.concurrent(max_inputs=1)
class IllustriousGenerator:
    """T4 で Illustrious-XL を1枚だけ生成し、すぐスケールダウンする。"""

    @modal.enter()
    def load(self) -> None:
        # 重みが無い起動は推論に入らず落とす。
        if not _weights_ready():
            raise RuntimeError(
                "checkpoint missing on volume; run modal run "
                "MOMOKA/generator/image/modal_app.py"
            )
        # GPU イメージの中だけで diffusers を読む。
        import torch
        from diffusers import StableDiffusionXLPipeline

        # fp16 で T4 16GB に載せる。
        pipe = StableDiffusionXLPipeline.from_single_file(
            _weights_file(),
            torch_dtype=torch.float16,
        )
        # VAE を分割してピークを下げる。
        pipe.enable_vae_slicing()
        # Attention を分割する。CPU オフロードは GPU 秒が増えるので使わない。
        pipe.enable_attention_slicing()
        # 重みを GPU へ載せる。
        pipe.to("cuda")
        # CLIP skip 2。
        _apply_clip_skip(pipe, CLIP_SKIP)
        # 既定サンプラー。
        _apply_sampler(pipe, "Euler a")
        # 以後の呼び出しで使う。
        self.pipe = pipe

    @modal.method()
    def generate(
        self,
        prompt: str,
        negative_prompt: str,
        width: int,
        height: int,
        steps: int,
        cfg_scale: float,
        seed: int,
        sampler_name: str,
    ) -> Iterator[dict[str, Any]]:
        """ステップを yield し、最後に PNG バイトを yield する。"""
        # 上限を超えたリクエストは推論しない。
        if width > MAX_SIDE or height > MAX_SIDE or steps > MAX_STEPS:
            raise ValueError(
                f"T4 limit is {MAX_SIDE}px and {MAX_STEPS} steps"
            )
        # 8 の倍数でないサイズはここで切る。
        if width % 8 or height % 8 or width < 256 or height < 256:
            raise ValueError("size must be a multiple of 8 and at least 256")
        # 推論スレッドから進捗を受け取る。
        events: queue.Queue = queue.Queue()

        def on_step(_pipe: Any, step_index: int, _timestep: int, callback_kwargs: dict) -> dict:
            # 1 始まりのステップ番号を積む。
            events.put({"type": "progress", "step": int(step_index) + 1, "total": int(steps)})
            # diffusers が kwargs の返却を要求する。
            return callback_kwargs

        # スレッド間の結果。
        result: dict[str, Any] = {}

        def run_pipeline() -> None:
            # このスレッドの中だけで torch を使う。
            import torch

            try:
                # 呼び出しごとのサンプラー。
                _apply_sampler(self.pipe, sampler_name)
                # シード生成器。
                generator = torch.Generator(device="cuda")
                # 負のシードはランダム。
                if int(seed) >= 0:
                    generator.manual_seed(int(seed))
                else:
                    generator.seed()
                # 1枚だけ生成する。
                image = self.pipe(
                    prompt=prompt,
                    negative_prompt=negative_prompt or None,
                    width=int(width),
                    height=int(height),
                    num_inference_steps=int(steps),
                    guidance_scale=float(cfg_scale),
                    generator=generator,
                    callback_on_step_end=on_step,
                ).images[0]
                # PNG にして返す。
                buffer = BytesIO()
                image.save(buffer, format="PNG")
                result["png"] = buffer.getvalue()
            except Exception as exc:  # noqa: BLE001
                # 本体の yield 側で上げ直す。
                result["error"] = exc
            finally:
                # 進捗ループを終わらせる番兵。
                events.put(None)

        # パイプラインがブロックしてもステップを先に返せるようにする。
        worker = threading.Thread(target=run_pipeline, daemon=True)
        # 推論を開始する。
        worker.start()
        # 番兵まで進捗を流す。
        while True:
            item = events.get()
            # 終了。
            if item is None:
                break
            # ステップをクライアントへ流す。
            yield item
        # スレッドの後始末。
        worker.join()
        # 推論例外はここで出す。
        if "error" in result:
            raise result["error"]
        # PNG が無ければ失敗。
        if "png" not in result:
            raise RuntimeError("pipeline finished without an image")
        # 最後の要素が画像。
        yield {"type": "image", "png": result["png"]}


@app.local_entrypoint()
def main() -> None:
    # CLI トークンで CPU ダウンロードだけ行う。
    status = download_weights.remote(REPO_ID, WEIGHTS_FILENAME, "")
    # 結果を表示する。
    print(status)
