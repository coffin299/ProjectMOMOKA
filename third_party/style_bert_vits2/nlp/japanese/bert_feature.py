from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Optional, Union

import numpy as np
import onnxruntime
from numpy.typing import NDArray

from style_bert_vits2.constants import Languages
from style_bert_vits2.nlp import bert_models, onnx_bert_models
from style_bert_vits2.nlp.japanese.g2p import text_to_sep_kata
from style_bert_vits2.utils import get_onnx_device_options


if TYPE_CHECKING:
    import torch

# 日本語 BERT 特徴量のログ
_logger = logging.getLogger(__name__)


def extract_bert_feature(
    text: str,
    word2ph: list[int],
    device: str,
    assist_text: Optional[str] = None,
    assist_text_weight: float = 0.7,
) -> torch.Tensor:
    """
    日本語のテキストから BERT の特徴量を抽出する (PyTorch 推論)

    Args:
        text (str): 日本語のテキスト
        word2ph (list[int]): 元のテキストの各文字に音素が何個割り当てられるかを表すリスト
        device (str): 推論に利用するデバイス
        assist_text (Optional[str], optional): 補助テキスト (デフォルト: None)
        assist_text_weight (float, optional): 補助テキストの重み (デフォルト: 0.7)

    Returns:
        torch.Tensor: BERT の特徴量
    """

    import torch

    def _hidden_to_fp32(states: tuple) -> torch.Tensor:
        # 特徴量テンソルを文長×1024 で取り出す
        feature = torch.cat(states[-3:-2], -1)[0].cpu()
        # fp16 の出力だけ fp32 に戻す。fp32 重みの出力はそのまま使う
        if feature.dtype != torch.float32:
            return feature.float()
        return feature

    def _feature_is_bad(feature: torch.Tensor) -> bool:
        # NaN / Inf / 全ゼロは無音合成の原因になる
        if not torch.isfinite(feature).all():
            return True
        # 振幅が無ければ壊れている
        return bool(feature.abs().max().item() == 0.0)

    # 各単語が何文字かを作る `word2ph` を使う必要があるので、読めない文字は必ず無視する
    # でないと `word2ph` の結果とテキストの文字数結果が整合性が取れない
    text = "".join(text_to_sep_kata(text, raise_yomi_error=False)[0])
    if assist_text:
        assist_text = "".join(text_to_sep_kata(assist_text, raise_yomi_error=False)[0])

    if device == "cuda" and not torch.cuda.is_available():
        device = "cpu"

    # ホスト設定が fp32 なら CUDA でも fp32。未設定の CUDA は fp16
    model = bert_models.load_model(
        Languages.JP,
        device_map=device,
        torch_dtype=bert_models.jp_load_dtype(device),
    )
    bert_models.transfer_model(Languages.JP, device)

    style_res_mean = None
    with torch.no_grad():
        tokenizer = bert_models.load_tokenizer(Languages.JP)
        inputs = tokenizer(text, return_tensors="pt")
        for i in inputs:
            inputs[i] = inputs[i].to(device)  # type: ignore
        res = model(**inputs, output_hidden_states=True)
        res = _hidden_to_fp32(res["hidden_states"])
        # fp16 が壊れたら一度だけ fp32 に上げて同じ文を作り直す
        if _feature_is_bad(res) and device.startswith("cuda"):
            _logger.warning(
                "Japanese BERT feature looks broken under current dtype; retrying in fp32"
            )
            # 以降の遅延ロードも fp32 にする
            bert_models.set_jp_bert_dtype_name("fp32")
            # 重みを全精度へ揃える
            model.to(dtype=torch.float32)
            # 同じ入力で再推論する
            res = model(**inputs, output_hidden_states=True)
            res = _hidden_to_fp32(res["hidden_states"])
        if assist_text:
            style_inputs = tokenizer(assist_text, return_tensors="pt")
            for i in style_inputs:
                style_inputs[i] = style_inputs[i].to(device)  # type: ignore
            style_res = model(**style_inputs, output_hidden_states=True)
            style_res = _hidden_to_fp32(style_res["hidden_states"])
            style_res_mean = style_res.mean(0)

    assert len(word2ph) == len(text) + 2, text
    word2phone = word2ph
    phone_level_feature = []
    for i in range(len(word2phone)):
        if assist_text:
            assert style_res_mean is not None
            repeat_feature = (
                res[i].repeat(word2phone[i], 1) * (1 - assist_text_weight)
                + style_res_mean.repeat(word2phone[i], 1) * assist_text_weight
            )
        else:
            repeat_feature = res[i].repeat(word2phone[i], 1)
        phone_level_feature.append(repeat_feature)

    phone_level_feature = torch.cat(phone_level_feature, dim=0)

    return phone_level_feature.T


def extract_bert_feature_onnx(
    text: str,
    word2ph: list[int],
    onnx_providers: Sequence[Union[str, tuple[str, dict[str, Any]]]],
    assist_text: Optional[str] = None,
    assist_text_weight: float = 0.7,
) -> NDArray[Any]:
    """
    日本語のテキストから BERT の特徴量を抽出する (ONNX 推論)

    Args:
        text (str): 日本語のテキスト
        word2ph (list[int]): 元のテキストの各文字に音素が何個割り当てられるかを表すリスト
        onnx_providers (list[str]): ONNX 推論で利用する ExecutionProvider (CPUExecutionProvider, CUDAExecutionProvider など)
        assist_text (Optional[str], optional): 補助テキスト (デフォルト: None)
        assist_text_weight (float, optional): 補助テキストの重み (デフォルト: 0.7)

    Returns:
        NDArray[Any]: BERT の特徴量
    """

    # 各単語が何文字かを作る `word2ph` を使う必要があるので、読めない文字は必ず無視する
    # でないと `word2ph` の結果とテキストの文字数結果が整合性が取れない
    text = "".join(text_to_sep_kata(text, raise_yomi_error=False)[0])
    if assist_text:
        assist_text = "".join(text_to_sep_kata(assist_text, raise_yomi_error=False)[0])

    # トークナイザーとモデルの読み込み
    tokenizer = onnx_bert_models.load_tokenizer(Languages.JP)
    session = onnx_bert_models.load_model(
        language=Languages.JP,
        onnx_providers=onnx_providers,
    )
    input_names = [input.name for input in session.get_inputs()]
    output_name = session.get_outputs()[0].name

    # 入力テンソルの転送に使用するデバイス種別, デバイス ID, 実行オプションを取得
    device_type, device_id, run_options = get_onnx_device_options(session, onnx_providers)  # fmt: skip

    # 入力をテンソルに変換
    inputs = tokenizer(text, return_tensors="np")
    input_tensor = [
        inputs["input_ids"].astype(np.int64),  # type: ignore
        inputs["attention_mask"].astype(np.int64),  # type: ignore
    ]
    # 推論デバイスに入力テンソルを割り当て
    ## GPU 推論の場合、device_type + device_id に対応する GPU デバイスに入力テンソルが割り当てられる
    io_binding = session.io_binding()
    for name, value in zip(input_names, input_tensor):
        gpu_tensor = onnxruntime.OrtValue.ortvalue_from_numpy(
            value, device_type, device_id
        )
        io_binding.bind_ortvalue_input(name, gpu_tensor)
    # text から BERT 特徴量を抽出
    io_binding.bind_output(output_name, device_type)
    session.run_with_iobinding(io_binding, run_options=run_options)
    res = io_binding.get_outputs()[0].numpy()

    style_res_mean = None
    if assist_text:
        # 入力をテンソルに変換
        style_inputs = tokenizer(assist_text, return_tensors="np")
        style_input_tensor = [
            style_inputs["input_ids"].astype(np.int64),  # type: ignore
            style_inputs["attention_mask"].astype(np.int64),  # type: ignore
        ]
        # 推論デバイスに入力テンソルを割り当て
        ## GPU 推論の場合、device_type + device_id に対応する GPU デバイスに入力テンソルが割り当てられる
        io_binding = session.io_binding()  # IOBinding は作り直す必要がある
        for name, value in zip(input_names, style_input_tensor):
            gpu_tensor = onnxruntime.OrtValue.ortvalue_from_numpy(
                value, device_type, device_id
            )
            io_binding.bind_ortvalue_input(name, gpu_tensor)
        # assist_text から BERT 特徴量を抽出
        io_binding.bind_output(output_name, device_type)
        session.run_with_iobinding(io_binding, run_options=run_options)
        style_res = io_binding.get_outputs()[0].numpy()
        style_res_mean = np.mean(style_res, axis=0)

    assert len(word2ph) == len(text) + 2, text
    word2phone = word2ph
    phone_level_feature = []
    for i in range(len(word2phone)):
        if assist_text:
            assert style_res_mean is not None
            repeat_feature = (
                np.tile(res[i], (word2phone[i], 1)) * (1 - assist_text_weight)
                + np.tile(style_res_mean, (word2phone[i], 1)) * assist_text_weight
            )
        else:
            repeat_feature = np.tile(res[i], (word2phone[i], 1))
        phone_level_feature.append(repeat_feature)

    phone_level_feature = np.concatenate(phone_level_feature, axis=0)

    return phone_level_feature.T
