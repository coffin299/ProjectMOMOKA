# Modal 上で動かす画像生成アプリ。
# Bot プロセスからは import しない。デプロイは `modal deploy MOMOKA/generator/image/modal_app.py`。
# 重みの取得は CPU 関数。GPU は T4 (16 GiB)。生成メソッドが戻ると短時間でスケールダウンする。

from __future__ import annotations

import os
import queue
import threading
import urllib.error
import urllib.request
from io import BytesIO
from typing import Any, Iterator
from urllib.parse import urlencode

import modal

# 公開の Illustrious-XL v2.0。Starter 枠ではこの1本だけを載せる。
REPO_ID = "OnomaAIResearch/Illustrious-XL-v2.0"
# Volume 上のファイル名。Bot 側 YAML の weights_filename と揃える。
WEIGHTS_FILENAME = "Illustrious-XL-v2.0.safetensors"
# Civitai Shiitake-Mix v2.0（Illustrious 系チェックポイント）。
SHIITAKE_VERSION_ID = "1570140"
SHIITAKE_FILENAME = "ShiitakeMix_v2.safetensors"
# マウント先。CPU 関数と GPU クラスで同じ Volume を見る。
VOLUME_PATH = "/models"
# T4 で許可する最大辺長。Bot 側モーダルでも同じ値で拒否する。
MAX_SIDE = 2048
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


def _safe_filename(filename: str) -> str:
    name = os.path.basename((filename or "").replace("\\", "/")).strip()
    if not name.endswith(".safetensors") or name.startswith("."):
        raise RuntimeError(f"invalid checkpoint filename: {filename}")
    return name


def _weights_file(filename: str = WEIGHTS_FILENAME) -> str:
    # Volume 内のチェックポイント絶対パス。
    return os.path.join(VOLUME_PATH, _safe_filename(filename))


def _weights_ready(filename: str = WEIGHTS_FILENAME) -> bool:
    # サイズが足りるファイルだけ完成扱いにする。
    path = _weights_file(filename)
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
def download_weights(
    filename: str,
    source: str = "huggingface",
    repo_id: str = "",
    version_id: str = "",
    hf_token: str = "",
    civitai_token: str = "",
) -> str:
    """GPU を付けずにチェックポイントを Volume へ置く。"""
    # 既にあれば GPU 時間を使わず戻る。
    safe = _safe_filename(filename)
    if _weights_ready(safe):
        return "ready"
    if (source or "huggingface").strip().lower() == "civitai":
        if not str(version_id).strip():
            raise RuntimeError("civitai version_id is empty")
        url = f"https://civitai.com/api/download/models/{str(version_id).strip()}"
        headers = {"User-Agent": "momoka-image/1.0"}
        if civitai_token.strip():
            # Civitai redirects downloads to object storage; urllib strips Authorization
            # on cross-host redirects. Its download endpoint supports token= for this flow.
            url = f"{url}?{urlencode({'token': civitai_token.strip()})}"
        else:
            raise RuntimeError(
                "Civitai requires an API token for model downloads. Set CIVITAI_TOKEN "
                "in the shell before running modal run MOMOKA/generator/image/modal_app.py."
            )
        target = _weights_file(safe)
        partial = target + ".partial"
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=600) as response, open(partial, "wb") as handle:
                if "text/html" in response.headers.get("Content-Type", "").lower():
                    raise RuntimeError("civitai download returned html instead of a checkpoint")
                while True:
                    chunk = response.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise RuntimeError(
                    "Civitai rejected the API token (HTTP 401). Check CIVITAI_TOKEN "
                    "and rerun modal run MOMOKA/generator/image/modal_app.py."
                ) from exc
            raise RuntimeError(f"civitai download failed: HTTP {exc.code}") from exc
        if os.path.getsize(partial) < MIN_WEIGHTS_BYTES:
            os.remove(partial)
            raise RuntimeError("civitai download was too small; check Civitai access/token")
        os.replace(partial, target)
    else:
        # 空文字トークンは未指定として扱う。
        token = hf_token.strip() or None
        from huggingface_hub import hf_hub_download

        saved = hf_hub_download(
            repo_id=repo_id or REPO_ID,
            filename=safe,
            local_dir=VOLUME_PATH,
            token=token,
        )
        target = _weights_file(safe)
        if os.path.abspath(saved) != os.path.abspath(target):
            os.replace(saved, target)
    # 次の GPU コンテナから見えるように確定する。
    weights_volume.commit()
    # 呼び出し元が成否を見る。
    if not _weights_ready(safe):
        raise RuntimeError(f"checkpoint download finished but the file is still missing: {safe}")
    return "downloaded"


def _apply_clip_skip(pipe: Any, clip_skip: int) -> None:
    # 1 以下は最終層のまま。
    if clip_skip <= 1:
        return
    # Diffusers のラッパー型は text_encoder.text_model.encoder、
    # safetensors 読み込み後の型によっては text_encoder.encoder。
    text_encoder = getattr(pipe, "text_encoder", None)
    transformer = getattr(text_encoder, "text_model", text_encoder)
    encoder = getattr(transformer, "encoder", None)
    layers = getattr(encoder, "layers", None)
    if layers is None:
        # CLIP 構造が異なる重みでは Skip を適用せず標準出力を使う。
        return
    # 残す層数。
    keep = len(layers) - (clip_skip - 1)
    # 全部消す指定は無視する。
    if keep < 1:
        return
    # 推論で使う層だけ残す。
    encoder.layers = layers[:keep]


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
    timeout=1200,
    min_containers=0,
    max_containers=1,
)
@modal.concurrent(max_inputs=1)
class IllustriousGenerator:
    """T4 で Illustrious-XL を1枚だけ生成し、すぐスケールダウンする。"""

    @modal.enter()
    def setup(self) -> None:
        self.pipe = None
        self.loaded_filename = ""
        self.loaded_with_cpu_offload = False

    def _ensure_loaded(self, filename: str, *, use_cpu_offload: bool) -> None:
        safe = _safe_filename(filename)
        if (
            self.pipe is not None
            and self.loaded_filename == safe
            and self.loaded_with_cpu_offload == use_cpu_offload
        ):
            return
        if not _weights_ready(safe):
            raise RuntimeError(
                f"checkpoint missing on volume: {safe}; run modal run "
                "MOMOKA/generator/image/modal_app.py"
            )
        # GPU イメージの中だけで diffusers を読む。
        import torch
        from diffusers import StableDiffusionXLPipeline

        # 前モデルの参照を破棄してGPUメモリを空ける。
        if self.pipe is not None:
            del self.pipe
            self.pipe = None
            torch.cuda.empty_cache()
        pipe = StableDiffusionXLPipeline.from_single_file(
            _weights_file(safe),
            torch_dtype=torch.float16,
        )
        # diffusers のバージョンやパイプラインによって未実装の最適化 API があるため、
        # 存在するものだけ適用する。VAE slicing は単画像生成では必須ではない。
        enable_vae_slicing = getattr(pipe, "enable_vae_slicing", None)
        if callable(enable_vae_slicing):
            enable_vae_slicing()
        enable_attention_slicing = getattr(pipe, "enable_attention_slicing", None)
        if callable(enable_attention_slicing):
            enable_attention_slicing()
        # 16 GiB の T4 にモデル全体を載せて GPU のみで推論する。
        if use_cpu_offload:
            # 将来、明示的に CPU offload を選ぶ場合のフォールバック経路。
            pipe.enable_model_cpu_offload()
        else:
            pipe.to("cuda")
        # CLIP skip 2。
        _apply_clip_skip(pipe, CLIP_SKIP)
        # 既定サンプラー。
        _apply_sampler(pipe, "Euler a")
        # 以後の呼び出しで使う。
        self.pipe = pipe
        self.loaded_filename = safe
        self.loaded_with_cpu_offload = use_cpu_offload

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
        weights_filename: str = WEIGHTS_FILENAME,
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
        # 16 GiB の T4 ではモデル全体を GPU に載せて推論する。
        use_cpu_offload = False
        self._ensure_loaded(weights_filename, use_cpu_offload=use_cpu_offload)
        # 高解像度ではVAE decodeだけタイル化し、GPU上のピーク使用量を抑える。
        if width > 1024 or height > 1024:
            enable_vae_tiling = getattr(self.pipe, "enable_vae_tiling", None)
            if callable(enable_vae_tiling):
                enable_vae_tiling()
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
    civitai_token = os.environ.get("CIVITAI_TOKEN", "")
    jobs = (
        (WEIGHTS_FILENAME, "huggingface", REPO_ID, "", "", ""),
        (SHIITAKE_FILENAME, "civitai", "", SHIITAKE_VERSION_ID, "", civitai_token),
    )
    for args in jobs:
        print(args[0], download_weights.remote(*args))
