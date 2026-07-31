"""LLM 応答メッセージの整形ヘルパー。"""

from __future__ import annotations


def split_message_smartly(text: str, max_length: int) -> list[str]:
    """Discord の文字数上限に収まるよう本文を自然な位置で分割する。"""
    # 上限内の本文は分割せず、そのまま 1 要素で返す。
    if len(text) <= max_length:
        return [text]

    # 未処理の本文と分割済みチャンクを初期化する。
    chunks: list[str] = []
    remaining = text

    # 本文が残る限り、上限内のチャンクへ分割する。
    while remaining:
        # 残りが上限内になった場合は最後のチャンクとして追加する。
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        # 上限までの範囲で最適な分割位置を探索する。
        candidate = remaining[:max_length]
        split_point = find_best_split_point(candidate)
        # 自然な区切りがなければ、無限ループを避ける余白付きの位置で分割する。
        if split_point == -1:
            split_point = max_length - 20

        # 末尾の余分な空白を除いたチャンクを作る。
        chunk_text = remaining[:split_point].rstrip()
        # 空でないチャンクだけを結果に追加する。
        if chunk_text:
            chunks.append(chunk_text)
        # 先頭の余分な空白を除いて残り本文を更新する。
        remaining = remaining[split_point:].lstrip()

    # 順序を保った分割結果を返す。
    return chunks


def find_best_split_point(chunk: str) -> int:
    """本文後半にある自然な区切り位置を優先順で返す。"""
    # コードブロックの終端を最優先にして Markdown 構造を保つ。
    code_block_end = chunk.rfind("```\n")
    if code_block_end > len(chunk) * 0.5:
        return code_block_end + 4

    # 段落境界が後半にある場合は段落単位で区切る。
    paragraph_break = chunk.rfind("\n\n")
    if paragraph_break > len(chunk) * 0.5:
        return paragraph_break + 2

    # 改行が後半にある場合は行末で区切る。
    newline = chunk.rfind("\n")
    if newline > len(chunk) * 0.6:
        return newline + 1

    # 日本語の文末記号が後半にある場合は文末で区切る。
    japanese_period = max(chunk.rfind("。"), chunk.rfind("！"), chunk.rfind("？"))
    if japanese_period > len(chunk) * 0.7:
        return japanese_period + 1

    # 英語の文末記号が後半にある場合は文末直後で区切る。
    english_period = max(chunk.rfind(". "), chunk.rfind("! "), chunk.rfind("? "))
    if english_period > len(chunk) * 0.7:
        return english_period + 2

    # 読点またはカンマが後半にある場合はその直後で区切る。
    comma = max(chunk.rfind("、"), chunk.rfind(", "))
    if comma > len(chunk) * 0.7:
        return comma + 1

    # 空白が後半にある場合は単語境界で区切る。
    space = chunk.rfind(" ")
    if space > len(chunk) * 0.7:
        return space + 1

    # 適切な区切りがないことを呼び出し元へ伝える。
    return -1
