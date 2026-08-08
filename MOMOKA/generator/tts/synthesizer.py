from __future__ import annotations

import os
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Optional

from .preprocess import normalize_text
from .wav import encode_wav_from_floats

try:
    import torch  # type: ignore
except Exception:  # pragma: no cover - optional runtime dep
    torch = None

try:
    import numpy as np  # type: ignore
except Exception:  # pragma: no cover - optional runtime dep
    np = None
import importlib
import logging


@dataclass
class SynthesizerConfig:
    model_root: str = "models/tts-models"
    model_name: Optional[str] = None  # e.g. "my-voice"
    dictionary_dir: Optional[str] = None  # path to pyopenjtalk-dict
    sample_rate: int = 48000  # Discord standard: 48kHz
    noise_scale: float = 0.667
    noise_w: float = 0.8
    length_scale: float = 1.0
    sbvits2_module_path: Optional[str] = None  # optional: e.g. 'style_bert_vits2'


class StyleBertVITS2Synthesizer:
    """Wrapper for Style-Bert-VITS2 TTS engine.

    Notes:
        - Requires Style-Bert-VITS2 model files to be present.
        - Expected model directory structure:
            models/tts-models/<model_name>/<model_name>.safetensors or G_*.pth
            and an accompanying config.json file in the same directory.
        - Optional: style_vectors.npy for style-based synthesis.
    """

    def __init__(self, config: SynthesizerConfig):
        self.config = config
        self._model_ready = False
        self._device = 'cpu'
        self._sample_rate = int(config.sample_rate)
        self._model_dir: Optional[Path] = None
        self._ckpt_path: Optional[Path] = None
        self._json_path: Optional[Path] = None
        self._style_vectors_path: Optional[Path] = None
        self._config_data: Optional[dict] = None
        self._style_vectors: Optional[object] = None
        self._engine = None  # Style-Bert-VITS2 TTSModel instance

        self._discover_model_paths()
        # モデルは遅延ロード: load_model() を明示的に呼ぶまでロードしない
        # これにより起動時のVRAM消費を回避する

    def _discover_model_paths(self) -> None:
        # Get project root directory (where main.py is located)
        # synthesizer.py is at MOMOKA/generator/tts/synthesizer.py, so go up 3 levels
        module_path = Path(__file__).resolve()
        project_root = module_path.parents[3]
        
        # Use project root as base, then resolve model_root relative to it
        if Path(self.config.model_root).is_absolute():
            root = Path(self.config.model_root)
        else:
            root = project_root / self.config.model_root
        
        if not root.exists():
            logging.getLogger(__name__).warning(
                f"Model root directory not found: {root}. Expected: {project_root / 'models' / 'tts-models'}"
            )
            return
        
        logging.getLogger(__name__).debug(f"Searching for models in: {root}")
        
        target_dir: Optional[Path] = None
        
        if self.config.model_name:
            # Specific model name provided
            candidate = root / self.config.model_name
            logger = logging.getLogger(__name__)
            logger.debug(f"Looking for specific model: {candidate}")
            if candidate.exists() and candidate.is_dir():
                # Verify it contains model files
                has_checkpoint = False
                has_config = False
                for p in candidate.iterdir():
                    if p.is_file():
                        if p.suffix in ('.safetensors', '.pth'):
                            has_checkpoint = True
                            logger.debug(f"  Found checkpoint: {p.name}")
                        elif p.name == 'config.json':
                            has_config = True
                            logger.debug(f"  Found config.json: {p.name}")
                
                if has_checkpoint and has_config:
                    target_dir = candidate
                    logger.info(f"Found specified model directory: {target_dir}")
                else:
                    logger.warning(
                        f"Specified model directory found but incomplete (checkpoint={has_checkpoint}, config={has_config}): {candidate}"
                    )
            else:
                logger.warning(f"Specified model directory not found: {candidate}")
        else:
            # Search all directories directly under models/tts-models/ for model files
            # Check all subdirectories (Custom_EN_V1, Custom_JP_V1, foo, bar, etc.) in models/tts-models/
            logger = logging.getLogger(__name__)
            logger.debug(f"Scanning directories in {root}")
            for d in root.iterdir():
                if not d.is_dir():
                    continue
                logger.debug(f"Checking directory: {d.name}")
                # Check if this directory contains model files
                has_checkpoint = False
                has_config = False
                checkpoint_files = []
                config_file = None
                for p in d.iterdir():
                    if p.is_file():
                        if p.suffix in ('.safetensors', '.pth'):
                            # Accept any checkpoint file - Style-Bert-VITS2 supports various naming conventions
                            checkpoint_files.append(p)
                            has_checkpoint = True
                            logger.debug(f"  Found checkpoint: {p.name}")
                        elif p.name == 'config.json':
                            has_config = True
                            config_file = p
                            logger.debug(f"  Found config.json: {p.name}")
                
                # If both checkpoint and config found, this is a valid model directory
                if has_checkpoint and has_config:
                    target_dir = d
                    logger.info(
                        f"Found Style-Bert-VITS2 model at: {target_dir} (checkpoints: {[f.name for f in checkpoint_files]})"
                    )
                    break
                elif has_checkpoint or has_config:
                    logger.debug(f"  Directory {d.name} has checkpoint={has_checkpoint}, config={has_config} (incomplete)")
        
        if not target_dir:
            logger = logging.getLogger(__name__)
            # List all directories found for debugging
            found_dirs = [d.name for d in root.iterdir() if d.is_dir()]
            logger.warning(
                f"No valid model found in {root}. "
                f"Found directories: {found_dirs if found_dirs else 'none'}. "
                f"Each directory must contain both a checkpoint file (.safetensors/.pth) and config.json"
            )
            return
        
        # Extract model files from the target directory
        # Expected structure:
        #   tts-models/モデル名/モデル名.safetensors (or .pth)
        #   tts-models/モデル名/config.json
        #   tts-models/モデル名/style_vectors.npy (optional)
        ckpt = None
        jsonf = None
        stylef = None
        checkpoint_candidates = []
        for p in sorted(target_dir.iterdir()):
            if not p.is_file():
                continue
            if p.suffix in ('.safetensors', '.pth'):
                # Accept any checkpoint file - typically named as <model_name>.safetensors or G_*.pth
                checkpoint_candidates.append(p)
            if p.name == 'config.json':
                jsonf = p
            if p.name == 'style_vectors.npy':
                stylef = p
        
        # Select best checkpoint: prefer safetensors, then prefer <model_name>.* or G_*.pth
        if checkpoint_candidates:
            # Sort: safetensors first, then prefer files matching model name, then G_*.pth
            checkpoint_candidates.sort(key=lambda x: (
                x.suffix != '.safetensors',  # .safetensors first
                x.stem != target_dir.name,  # <model_name>.* preferred (e.g., Custom_EN_V1.safetensors)
                not x.stem.startswith('G_'),  # G_*.pth as fallback
                x.name
            ))
            ckpt = checkpoint_candidates[0]
            logging.getLogger(__name__).debug(
                f"Selected checkpoint: {ckpt.name} from candidates: {[f.name for f in checkpoint_candidates]}"
            )
        
        self._model_dir = target_dir
        self._ckpt_path = ckpt
        self._json_path = jsonf
        self._style_vectors_path = stylef

    def load_model(self) -> None:
        """Style-Bert-VITS2モデルをオンデマンドでロードする（遅延ロード対応）。
        
        既にロード済みの場合は何もしない。
        """
        # 既にロード済みならスキップ
        if self._model_ready and self._engine is not None:
            return
        logger = logging.getLogger(__name__)
        if torch is None:
            self._model_ready = False
            logger.warning("PyTorch not available. Style-Bert-VITS2 requires PyTorch. TTS will be disabled.")
            return
        if not (self._ckpt_path and self._json_path):
            self._model_ready = False
            # Get project root for error message
            module_path = Path(__file__).resolve()
            project_root = module_path.parents[3]
            expected_path = project_root / "models" / "tts-models" / "<model_name>"
            logger.warning(
                f"Model files not found. Please place Style-Bert-VITS2 model files in: {expected_path}. "
                f"TTS will be disabled until models are available."
            )
            return

        # Load config.json
        try:
            with open(self._json_path, 'r', encoding='utf-8') as f:
                self._config_data = json.load(f)
        except Exception as e:
            self._config_data = None
            logging.getLogger(__name__).warning(f"Failed to load config.json: {e}")

        # Load style_vectors.npy (optional)
        if self._style_vectors_path and np is not None:
            try:
                self._style_vectors = np.load(str(self._style_vectors_path), allow_pickle=False)
            except Exception as e:
                self._style_vectors = None
                logging.getLogger(__name__).debug(f"Style vectors not loaded: {e}")
        else:
            self._style_vectors = None

        # Try to load Style-Bert-VITS2 TTSModel
        self._engine = None
        self._device = 'cuda' if torch.cuda.is_available() else 'cpu'
        
        # First, try direct import of style_bert_vits2 (third_party 同梱パッケージ)
        logger = logging.getLogger(__name__)
        try:
            logger.debug(f"Attempting to import TTSModel from third_party package")
            # style_bert_vits2 は絶対 import のため、パッケージ親を sys.path に追加する
            import sys
            # synthesizer.py → プロジェクトルート（parents[3]）
            project_root = Path(__file__).resolve().parents[3]
            # 同梱先: third_party/style_bert_vits2/（パッケージ本体）
            third_party_dir = project_root / "third_party"
            # 未登録なら先頭に挿入して import を優先する
            if str(third_party_dir) not in sys.path:
                sys.path.insert(0, str(third_party_dir))
                logger.debug(
                    f"Added {third_party_dir} to sys.path for style_bert_vits2 imports"
                )

            # 絶対 import で TTSModel を読み込む
            from style_bert_vits2.tts_model import TTSModel
            logger.debug(f"TTSModel imported successfully. Initializing with: model_path={self._ckpt_path}, config_path={self._json_path}")
            # TTSModel expects Path objects, not strings
            style_vec = None
            if self._style_vectors_path:
                style_vec = self._style_vectors_path
            self._engine = TTSModel(
                model_path=self._ckpt_path,
                config_path=self._json_path,
                style_vec_path=style_vec,
                device=self._device,
            )
            logger.debug(f"TTSModel instance created. Loading model...")
            self._engine.load()  # Load the model
            
            # Pre-load Japanese BERT model and tokenizer from Hugging Face
            # This is required because the default local path doesn't exist
            # The models will be automatically downloaded from Hugging Face
            try:
                from style_bert_vits2.nlp import bert_models
                from style_bert_vits2.constants import Languages
                logger.debug("Pre-loading Japanese BERT model and tokenizer from Hugging Face...")
                # Use Hugging Face model ID instead of local path
                # Load BERT model first
                bert_models.load_model(
                    Languages.JP,
                    pretrained_model_name_or_path="ku-nlp/deberta-v2-large-japanese-char-wwm",
                    device_map=self._device if self._device != 'cpu' else None,
                    cache_dir=None,  # Use default Hugging Face cache
                )
                logger.debug("Japanese BERT model loaded successfully")
                # Load BERT tokenizer
                bert_models.load_tokenizer(
                    Languages.JP,
                    pretrained_model_name_or_path="ku-nlp/deberta-v2-large-japanese-char-wwm",
                    cache_dir=None,  # Use default Hugging Face cache
                )
                logger.info("Japanese BERT model and tokenizer loaded successfully")
            except Exception as e:
                logger.warning(f"Failed to pre-load BERT model/tokenizer: {e}. It will be loaded on first inference.")
            
            self._model_ready = True
            logger.info(
                f"Loaded Style-Bert-VITS2 model from {self._ckpt_path} on {self._device}"
            )
            return
        except ImportError as e:
            # style_bert_vits2 が読めない場合は後続のカスタム module path へフォールバック
            logger.error(
                f"Failed to import TTSModel from third_party/style_bert_vits2: {e}. "
                f"This may indicate missing dependencies or import path issues."
            )
            import traceback
            logger.debug(f"ImportError traceback: {traceback.format_exc()}")
        except Exception as e:
            logger.error(
                f"Failed to load Style-Bert-VITS2 model: {e}. "
                f"Model path: {self._ckpt_path}, Config path: {self._json_path}"
            )
            import traceback
            logger.debug(f"Exception traceback: {traceback.format_exc()}")

        # Fallback: try custom module path if provided
        module_path = self.config.sbvits2_module_path
        if module_path:
            try:
                mod = importlib.import_module(module_path)
                # Try TTSModel class
                if hasattr(mod, 'TTSModel'):
                    TTSModel = getattr(mod, 'TTSModel')
                    self._engine = TTSModel(
                        model_path=str(self._ckpt_path),
                        config_path=str(self._json_path),
                        style_vec_path=str(self._style_vectors_path) if self._style_vectors_path else None,
                        device=self._device,
                    )
                    if hasattr(self._engine, 'load'):
                        self._engine.load()
                    self._model_ready = True
                    logging.getLogger(__name__).info(
                        f"Loaded SBVITS2 engine from {module_path}"
                    )
                    return
                # Try other common factory names
                candidates = [
                    getattr(mod, 'load_synthesizer', None),
                    getattr(mod, 'create_synthesizer', None),
                    getattr(mod, 'Synthesizer', None),
                    getattr(mod, 'SynthesizerTrn', None),
                ]
                factory = None
                for c in candidates:
                    if callable(c):
                        factory = c
                        break
                if factory is not None:
                    try:
                        self._engine = factory(
                            config_path=str(self._json_path),
                            checkpoint_path=str(self._ckpt_path),
                            device=self._device,
                        )
                        if self._engine is not None:
                            self._model_ready = True
                            logging.getLogger(__name__).info(
                                f"Loaded SBVITS2 engine from {module_path}"
                            )
                            return
                    except Exception as e:
                        logging.getLogger(__name__).debug(f"Factory method failed: {e}")
            except Exception as e:
                logging.getLogger(__name__).warning(
                    f"Failed to import SBVITS2 module '{module_path}': {e}"
                )

                # No model loaded
                self._model_ready = False
                logger = logging.getLogger(__name__)
                logger.error(
                    f"Style-Bert-VITS2 model could not be loaded. "
                    f"Checkpoint: {self._ckpt_path}, Config: {self._json_path}, "
                    f"Style vectors: {self._style_vectors_path}. "
                    f"Please ensure all dependencies are installed and model files are valid."
                )

    def unload_model(self) -> None:
        """Style-Bert-VITS2モデルをアンロードしてVRAMを解放する。"""
        logger = logging.getLogger(__name__)
        if self._engine is None and not self._model_ready:
            # 既にアンロード済み
            return

        logger.info("🧹 [TTS] Unloading Style-Bert-VITS2 model to free VRAM...")

        # エンジン（TTSModel）を削除
        if self._engine is not None:
            try:
                # モデル内部のテンソルをCPUに移動してからdelする
                if hasattr(self._engine, 'model') and self._engine.model is not None:
                    try:
                        self._engine.model.cpu()
                    except Exception:  # noqa: BLE001
                        pass
                del self._engine
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error while deleting TTS engine: %s", exc)
            self._engine = None

        # モデル状態をリセット（次回 load_model() で再ロード可能）
        self._model_ready = False

        # CUDAキャッシュをクリア
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()

        # ガベージコレクション
        import gc
        gc.collect()

        logger.info("🧹 [TTS] Style-Bert-VITS2 model unloaded successfully")

    def synthesize_to_wav(self, text: str, style: Optional[str] = None,
                           style_weight: float = 5.0, speed: float = 1.0,
                           noise_scale: Optional[float] = None,
                           noise_w: Optional[float] = None,
                           length_scale: Optional[float] = None) -> bytes:
        """テキストを音声に変換します。モデル未ロード時は自動ロードします。"""
        # モデル未ロード時はオンデマンドでロード
        if not self._model_ready or self._engine is None:
            self.load_model()
        # ロード後もエンジンが無い場合はエラー
        if not self._model_ready or self._engine is None:
            error_msg = "Style-Bert-VITS2 model not loaded. Cannot synthesize audio."
            logging.getLogger(__name__).error(error_msg)
            raise RuntimeError(error_msg)
        
        processed = normalize_text(text, self.config.dictionary_dir)
        if not processed:
            return encode_wav_from_floats([], self._sample_rate)

        # Parameters with defaults
        ns = self.config.noise_scale if noise_scale is None else float(noise_scale)
        nw = self.config.noise_w if noise_w is None else float(noise_w)
        ls = self.config.length_scale if length_scale is None else float(length_scale)

        # Use Style-Bert-VITS2
        try:
            # Style-Bert-VITS2 TTSModel.infer() signature:
            # infer(text, language, speaker_id, reference_audio_path, sdp_ratio, noise, noise_w, length, ...)
            # For our use case, we'll use default speaker_id=0 and language=JP
            # Import using absolute import (package is in sys.path)
            from style_bert_vits2.constants import Languages
            
            # Determine style ID
            style_id = 0
            if style and hasattr(self._engine, 'style2id'):
                style_id = self._engine.style2id.get(style, 0)
            
            # Perform inference
            sr, audio = self._engine.infer(
                text=processed,
                language=Languages.JP,
                speaker_id=0,  # Default speaker
                reference_audio_path=None,
                sdp_ratio=0.2,  # Default SDP ratio
                noise=ns,
                noise_w=nw,
                length=ls / speed if speed != 1.0 else ls,
                line_split=False,
                split_interval=0.0,
                assist_text=None,
                assist_text_weight=0.0,
                use_assist_text=False,
                style=style or 'Neutral',
                style_weight=style_weight,
            )
            
            # Convert numpy array to WAV bytes
            if np is not None and isinstance(audio, np.ndarray):
                # Ensure audio is in the right format
                audio = audio.astype(np.float32)
                # Normalize to [-1, 1] range
                if audio.max() > 1.0 or audio.min() < -1.0:
                    audio = audio / max(abs(audio.max()), abs(audio.min()))
                return encode_wav_from_floats(audio, sample_rate=int(sr))
            else:
                # Fallback: try to encode as-is
                return encode_wav_from_floats(audio, sample_rate=int(sr) if hasattr(sr, '__int__') else self._sample_rate)
        except ImportError as e:
            error_msg = f"Style-Bert-VITS2 package not available: {e}"
            logging.getLogger(__name__).error(error_msg)
            raise RuntimeError(error_msg)
        except Exception as e:
            error_msg = f"Style-Bert-VITS2 inference failed: {e}"
            logging.getLogger(__name__).error(error_msg)
            raise RuntimeError(error_msg)


