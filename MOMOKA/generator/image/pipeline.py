from __future__ import annotations

import asyncio
import gc
import logging
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING, Any, Optional, Callable

import torch
from PIL import Image

from .model_registry import ImageModelInfo

if TYPE_CHECKING:
    from diffusers import StableDiffusionPipeline, StableDiffusionXLPipeline

logger = logging.getLogger(__name__)

# 最後の生成からこの秒数使われなければパイプラインを解放する（0 以下で常駐）
DEFAULT_IDLE_UNLOAD_SECONDS = 300.0


def _diffusers_pipeline_classes():
    """diffusers（transformers を連鎖 import する重いモジュール）を初回生成時まで読み込まない。"""
    from diffusers import StableDiffusionPipeline, StableDiffusionXLPipeline

    return StableDiffusionPipeline, StableDiffusionXLPipeline


def _scheduler_map() -> dict:
    try:  # diffusers>=0.27 removed SCHEDULER_MAP
        from diffusers.schedulers import SCHEDULER_MAP
    except ImportError:  # pragma: no cover - fallback when map is unavailable
        return {}
    return SCHEDULER_MAP


@dataclass
class GenerationParams:
    prompt: str
    negative_prompt: str
    width: int
    height: int
    steps: int
    cfg_scale: float
    seed: int
    sampler_name: Optional[str] = None


class LocalTxt2ImgPipeline:
    """Wrapper around diffusers StableDiffusionPipeline with async execution."""

    def __init__(
        self,
        device: Optional[str] = None,
        max_cache_size: int = 1,
        enable_cpu_offload: bool = True,
        enable_vae_slicing: bool = True,
        enable_vae_tiling: bool = False,
        attention_slicing: Optional[str] = "max",
        idle_unload_seconds: float = DEFAULT_IDLE_UNLOAD_SECONDS,
    ) -> None:
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        # CPU オフロード時は重みが RAM に常駐するため、未使用が続いたら解放する
        self.idle_unload_seconds = float(idle_unload_seconds)
        self._idle_unload_task: Optional[asyncio.Task] = None
        self._active_generations = 0
        # LRUキャッシュ: OrderedDictで実装（最近使用したものが最後に移動）
        self._pipeline_cache: OrderedDict[str, StableDiffusionPipeline | StableDiffusionXLPipeline] = OrderedDict()
        self._lock = asyncio.Lock()
        self.max_cache_size = max_cache_size  # 最大キャッシュ数（デフォルト: 1モデルのみ保持）
        
        # メモリ最適化設定（8GB VRAM対応）
        self.enable_cpu_offload = enable_cpu_offload  # CPUオフロード（デフォルト: 有効）
        self.enable_vae_slicing = enable_vae_slicing  # VAEスライシング（デフォルト: 有効）
        self.enable_vae_tiling = enable_vae_tiling  # VAEタイル分割（大きな画像用、デフォルト: 無効）
        self.attention_slicing = attention_slicing  # Attentionスライシング（"max", "auto", None）
    
    def _is_sdxl_model(self, model: ImageModelInfo) -> bool:
        """Check if the model is SDXL based on name, metadata, or file size."""
        # Check metadata first
        if model.metadata.get("is_sdxl") is True:
            return True
        if model.metadata.get("is_sdxl") is False:
            return False
        
        # Check model name for "xl" indicator
        model_name_lower = model.name.lower()
        weights_name_lower = model.weights.name.lower()
        if "xl" in model_name_lower or "xl" in weights_name_lower:
            return True
        
        # Check file size (SDXL models are typically > 6GB)
        try:
            file_size_gb = model.weights.stat().st_size / (1024 ** 3)
            if file_size_gb > 5.5:  # Threshold around 6GB
                return True
        except (OSError, AttributeError):
            pass
        
        return False

    def _apply_memory_optimizations(
        self,
        pipeline: StableDiffusionPipeline | StableDiffusionXLPipeline,
        model_name: str,
    ) -> None:
        """メモリ最適化設定を適用（8GB VRAM対応）"""
        logger.info("Applying memory optimizations for model '%s'", model_name)
        
        # CPUオフロード: モデルコンポーネントを自動的にCPU/GPU間で移動
        # 最も効果的なメモリ節約手法（速度は若干低下）
        if self.enable_cpu_offload and self.device == "cuda":
            try:
                # enable_model_cpu_offload は推論時に自動的にコンポーネントを移動
                # enable_sequential_cpu_offload はより積極的だが、enable_model_cpu_offloadで十分
                pipeline.enable_model_cpu_offload()
                logger.info("Enabled CPU offloading for model '%s' (significant VRAM savings)", model_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to enable CPU offloading: %s. Falling back to standard loading.", exc)
                # CPUオフロードが失敗した場合、通常の読み込みにフォールバック
                if not hasattr(pipeline, '_offload_state'):
                    pipeline.to(self.device)
        else:
            # CPUオフロードが無効な場合、通常通りデバイスに読み込み
            pipeline.to(self.device)
        
        # VAEスライシング: VAE処理をバッチに分割してメモリ使用量を削減
        # デコード時にメモリ使用量を大幅に削減（品質への影響なし）
        if self.enable_vae_slicing:
            try:
                pipeline.enable_vae_slicing()
                logger.info("Enabled VAE slicing for model '%s'", model_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to enable VAE slicing: %s", exc)
        
        # VAEタイル分割: 大きな画像をタイルに分割して処理
        # 1024x1024以上の大きな画像で有効（品質への影響は最小限）
        if self.enable_vae_tiling:
            try:
                pipeline.enable_vae_tiling()
                logger.info("Enabled VAE tiling for model '%s' (for large images)", model_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to enable VAE tiling: %s", exc)
        
        # Attentionスライシング: Attention計算を分割してメモリ使用量を削減
        # 既に実装されているが、より細かく制御可能に
        if self.attention_slicing:
            try:
                if self.attention_slicing == "max":
                    pipeline.enable_attention_slicing("max")
                elif self.attention_slicing == "auto":
                    pipeline.enable_attention_slicing()
                else:
                    pipeline.enable_attention_slicing(self.attention_slicing)
                logger.info("Enabled attention slicing (%s) for model '%s'", self.attention_slicing, model_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to enable attention slicing: %s", exc)
        
        # xFormers: メモリ効率の良いAttention実装（利用可能な場合）
        # CPUオフロードとは独立して動作
        try:
            pipeline.enable_xformers_memory_efficient_attention()
            logger.info("Enabled xFormers memory efficient attention for model '%s'", model_name)
        except (ImportError, AttributeError, RuntimeError, ValueError) as exc:
            logger.info("xFormers memory efficient attention unavailable: %s", exc)

    def _unload_pipeline(self, model_name: str) -> None:
        """指定されたモデルをVRAMから解放する"""
        if model_name not in self._pipeline_cache:
            return
        
        pipeline = self._pipeline_cache.pop(model_name)
        logger.info("Unloading model '%s' from VRAM", model_name)
        
        # パイプラインの各コンポーネントをCPUに移動してから削除
        try:
            # 各コンポーネントをCPUに移動（メモリを解放）
            if hasattr(pipeline, 'unet') and pipeline.unet is not None:
                pipeline.unet.to('cpu')
                del pipeline.unet
            if hasattr(pipeline, 'vae') and pipeline.vae is not None:
                pipeline.vae.to('cpu')
                del pipeline.vae
            if hasattr(pipeline, 'text_encoder') and pipeline.text_encoder is not None:
                pipeline.text_encoder.to('cpu')
                del pipeline.text_encoder
            if hasattr(pipeline, 'text_encoder_2') and pipeline.text_encoder_2 is not None:
                pipeline.text_encoder_2.to('cpu')
                del pipeline.text_encoder_2
            
            # tokenizerとschedulerはメモリをほとんど使わないが、念のため削除
            if hasattr(pipeline, 'tokenizer'):
                del pipeline.tokenizer
            if hasattr(pipeline, 'tokenizer_2'):
                del pipeline.tokenizer_2
            if hasattr(pipeline, 'scheduler'):
                del pipeline.scheduler
            
            del pipeline
            
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error while unloading model '%s': %s", model_name, exc)
        
        # CUDAキャッシュをクリア
        if self.device == "cuda" and torch.cuda.is_available():
            torch.cuda.empty_cache()
        
        # ガベージコレクション
        gc.collect()
        logger.info("Model '%s' unloaded successfully", model_name)

    def _load_pipeline(self, model: ImageModelInfo) -> StableDiffusionPipeline | StableDiffusionXLPipeline:
        # キャッシュに存在する場合、LRUの最後に移動（最近使用されたことを記録）
        if model.name in self._pipeline_cache:
            self._pipeline_cache.move_to_end(model.name)
            return self._pipeline_cache[model.name]
        
        # キャッシュサイズ制限を超える場合、古いモデルを解放
        while len(self._pipeline_cache) >= self.max_cache_size:
            # 最も古い（最初の）モデルを取得して解放
            oldest_model = next(iter(self._pipeline_cache))
            self._unload_pipeline(oldest_model)

        StableDiffusionPipeline, StableDiffusionXLPipeline = _diffusers_pipeline_classes()
        is_sdxl = self._is_sdxl_model(model)
        pipeline_class = StableDiffusionXLPipeline if is_sdxl else StableDiffusionPipeline
        
        logger.info("Loading diffusers pipeline for model '%s' (SDXL: %s)", model.name, is_sdxl)
        
        # SDXLモデルの場合とSD 1.x/2.xモデルの場合で読み込み方法を分ける
        try:
            pipeline = pipeline_class.from_single_file(
                str(model.weights),
                torch_dtype=torch.float16 if self.device == "cuda" else torch.float32,
                safety_checker=None,
            )
        except TypeError as exc:
            # text_encoder_2 エラーが発生した場合、SDXLパイプラインを試す
            if "text_encoder_2" in str(exc) and not is_sdxl:
                logger.warning(
                    "Failed to load model '%s' as SD 1.x/2.x. Retrying as SDXL...",
                    model.name
                )
                pipeline = StableDiffusionXLPipeline.from_single_file(
                    str(model.weights),
                    torch_dtype=torch.float16 if self.device == "cuda" else torch.float32,
                    safety_checker=None,
                )
                is_sdxl = True
            else:
                raise

        # メモリ最適化設定を適用（8GB VRAM対応）
        # 注意: CPUオフロードを使用する場合、pipeline.to()は呼ばない
        # enable_model_cpu_offload()がパイプラインの各コンポーネントを自動的に管理する
        self._apply_memory_optimizations(pipeline, model.name)
        
        # VAEとLoRAの読み込み（メモリ最適化後に実行）
        if model.vae:
            try:
                logger.info("Loading VAE weights for model '%s'", model.name)
                pipeline.vae.from_pretrained(model.vae.parent, subfolder=model.vae.stem)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to load VAE for %s: %s", model.name, exc)

        if model.loras:
            for lora in model.loras:
                try:
                    logger.info("Loading LoRA '%s'", lora.name)
                    pipeline.load_lora_weights(str(lora))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to load LoRA %s: %s", lora, exc)
        
        # キャッシュに保存（新しいモデルは最後に追加）
        self._pipeline_cache[model.name] = pipeline
        return pipeline
    
    def clear_cache(self) -> None:
        """すべてのキャッシュされたモデルを解放する"""
        logger.info("Clearing all cached models from VRAM")
        model_names = list(self._pipeline_cache.keys())
        for model_name in model_names:
            self._unload_pipeline(model_name)
        logger.info("All cached models cleared")
    
    def _cancel_idle_unload(self) -> None:
        task = self._idle_unload_task
        self._idle_unload_task = None
        if task and not task.done():
            task.cancel()

    def _schedule_idle_unload(self) -> None:
        if self.idle_unload_seconds <= 0 or self._active_generations > 0 or not self._pipeline_cache:
            return
        self._cancel_idle_unload()
        self._idle_unload_task = asyncio.create_task(self._idle_unload_after_delay())

    async def _idle_unload_after_delay(self) -> None:
        await asyncio.sleep(self.idle_unload_seconds)
        async with self._lock:
            if self._active_generations > 0 or not self._pipeline_cache:
                return
            logger.info(
                "No image generation for %.0fs; releasing cached pipelines",
                self.idle_unload_seconds,
            )
            await asyncio.to_thread(self.clear_cache)

    def get_cache_info(self) -> dict[str, Any]:
        """キャッシュ情報を返す"""
        return {
            "cached_models": len(self._pipeline_cache),
            "max_cache_size": self.max_cache_size,
            "models": list(self._pipeline_cache.keys()),
        }

    def _apply_sampler(
        self, 
        pipeline: StableDiffusionPipeline | StableDiffusionXLPipeline, 
        sampler_name: Optional[str]
    ) -> None:
        if not sampler_name:
            return

        normalized = sampler_name.replace(" ", "").lower()
        for key, scheduler_cls in _scheduler_map().items():
            if normalized == key.replace(" ", "").lower():
                logger.info("Switching scheduler to '%s'", key)
                pipeline.scheduler = scheduler_cls.from_config(pipeline.scheduler.config)
                return
        logger.warning("Requested sampler '%s' not found. Using default scheduler.", sampler_name)

    async def generate(
        self,
        model: ImageModelInfo,
        params: GenerationParams,
        progress_callback: Optional[Callable[[int, int, object], None]] = None,
    ) -> bytes:
        async with self._lock:
            self._cancel_idle_unload()
            self._active_generations += 1
        try:
            return await self._generate_locked_load(model, params, progress_callback)
        finally:
            self._active_generations -= 1
            self._schedule_idle_unload()

    async def _generate_locked_load(
        self,
        model: ImageModelInfo,
        params: GenerationParams,
        progress_callback: Optional[Callable[[int, int, object], None]],
    ) -> bytes:
        async with self._lock:
            pipeline = self._load_pipeline(model)
            self._apply_sampler(pipeline, params.sampler_name)

        generator = torch.Generator(device=self.device)
        if params.seed >= 0:
            generator.manual_seed(params.seed)
        else:
            generator.seed()

        logger.info(
            "Running local txt2img | model=%s size=%dx%d steps=%d cfg=%.2f seed=%d sampler=%s",
            model.name,
            params.width,
            params.height,
            params.steps,
            params.cfg_scale,
            params.seed,
            params.sampler_name or "default",
        )

        # 実行中のイベントループを取得する（3.11 では get_event_loop は非推奨）
        loop = asyncio.get_running_loop()

        def _run_pipeline() -> Image.Image:
            invocation_kwargs = dict(
                prompt=params.prompt,
                negative_prompt=params.negative_prompt or None,
                width=params.width,
                height=params.height,
                num_inference_steps=params.steps,
                guidance_scale=params.cfg_scale,
                generator=generator,
            )
            if progress_callback:
                invocation_kwargs["callback"] = progress_callback
                invocation_kwargs["callback_steps"] = 1
            return pipeline(**invocation_kwargs).images[0]

        image: Image.Image = await loop.run_in_executor(None, _run_pipeline)

        buffer = BytesIO()
        await loop.run_in_executor(None, lambda: image.save(buffer, format="PNG"))
        return buffer.getvalue()


