"""纯标准库实现的 BERT WordPiece 分词器。

为什么不直接用 transformers / tokenizers：
- 那些都是第三方依赖（含 Rust 扩展），离线机器上不好装
- 我们只需要 bge-small-zh 这种标准 BERT 中文词表的一小部分功能

实现遵循 HuggingFace BertTokenizer 的规范：
- BasicTokenizer：清洗文本 / 中文单字切分 / 标点切分 / 可选小写
- WordPiece：贪心最长匹配 + "##" 续接，未命中整词则退 [UNK]
"""

from __future__ import annotations

import unicodedata
from typing import Dict, List, Sequence

# 中文字符区间（含扩展区、兼容汉字）
_CHINESE_RANGES = (
    (0x4E00, 0x9FFF),
    (0x3400, 0x4DBF),
    (0x20000, 0x2A6DF),
    (0x2A700, 0x2B73F),
    (0x2B740, 0x2B81F),
    (0x2B820, 0x2CEAF),
    (0xF900, 0xFAFF),
    (0x2F800, 0x2FA1F),
)


def is_chinese_char(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in _CHINESE_RANGES)


def is_punctuation(ch: str) -> bool:
    cp = ord(ch)
    if (33 <= cp <= 47) or (58 <= cp <= 64) or (91 <= cp <= 96) or (123 <= cp <= 126):
        return True
    return unicodedata.category(ch).startswith("P")


def _is_control(ch: str) -> bool:
    if ch in ("\t", "\n", "\r"):
        return False
    return unicodedata.category(ch).startswith("C")


def _is_whitespace(ch: str) -> bool:
    if ch in (" ", "\t", "\n", "\r"):
        return True
    return unicodedata.category(ch) == "Zs"


def _is_never_split(ch: str) -> bool:
    return ch in ("\t", "\n", "\r") or is_chinese_char(ord(ch))


class BertTokenizer:
    """BERT WordPiece 分词器（纯 Python）。"""

    def __init__(
        self,
        vocab: Dict[str, int] | Sequence[str],
        do_lower_case: bool = False,
        tokenize_chinese_chars: bool = True,
        strip_accents: bool | None = None,
        unk_token: str = "[UNK]",
        cls_token: str = "[CLS]",
        sep_token: str = "[SEP]",
        pad_token: str = "[PAD]",
        max_input_chars_per_word: int = 100,
    ) -> None:
        if not isinstance(vocab, dict):
            # 允许直接传词表列表，按出现顺序编号
            vocab = {tok: i for i, tok in enumerate(vocab)}
        self.vocab = vocab
        self.do_lower_case = do_lower_case
        self.tokenize_chinese_chars = tokenize_chinese_chars
        self.strip_accents = do_lower_case if strip_accents is None else strip_accents
        self.unk_token = unk_token
        self.cls_token = cls_token
        self.sep_token = sep_token
        self.pad_token = pad_token
        self.max_input_chars_per_word = max_input_chars_per_word

    # ---------------- 加载 ----------------

    @classmethod
    def from_vocab_file(cls, path: str, do_lower_case: bool = False, **kw) -> "BertTokenizer":
        vocab: Dict[str, int] = {}
        with open(path, "r", encoding="utf-8") as fh:
            for idx, line in enumerate(fh):
                token = line.rstrip("\n")
                if not token:
                    continue
                vocab[token] = idx
        return cls(vocab, do_lower_case=do_lower_case, **kw)

    # ---------------- BasicTokenizer ----------------

    def _clean_text(self, text: str) -> str:
        out = []
        for ch in text:
            cp = ord(ch)
            if cp == 0 or cp == 0xFFFD or _is_control(ch):
                continue
            out.append(" " if _is_whitespace(ch) else ch)
        return "".join(out)

    def _tokenize_chinese_chars(self, text: str) -> str:
        out = []
        for ch in text:
            if is_chinese_char(ord(ch)):
                out.extend((" ", ch, " "))
            else:
                out.append(ch)
        return "".join(out)

    def _run_strip_accents(self, text: str) -> str:
        text = unicodedata.normalize("NFD", text)
        return "".join(c for c in text if unicodedata.category(c) != "Mn")

    def _run_split_on_punc(self, text: str) -> List[str]:
        chars = list(text)
        out: List[str] = []
        start_new_word = True
        for ch in chars:
            if is_punctuation(ch):
                out.append([ch])
                start_new_word = True
            else:
                if start_new_word:
                    out.append([])
                start_new_word = False
                out[-1].append(ch)
        return ["".join(x) for x in out]

    def _tokenize(self, text: str) -> List[str]:
        text = self._clean_text(text)
        if self.tokenize_chinese_chars:
            text = self._tokenize_chinese_chars(text)
        text = text.lower() if self.do_lower_case else text
        if self.strip_accents:
            text = self._run_strip_accents(text)
        # 中文单字本身不会被标点切分影响，这里只按空白 + 标点切
        pieces: List[str] = []
        for chunk in text.split():
            pieces.extend(self._run_split_on_punc(chunk))
        return [p for p in pieces if p]

    # ---------------- WordPiece ----------------

    def _wordpiece(self, token: str) -> List[str]:
        if len(token) > self.max_input_chars_per_word:
            return [self.unk_token]
        sub_tokens: List[str] = []
        start = 0
        while start < len(token):
            end = len(token)
            cur = None
            while start < end:
                substr = token[start:end]
                if start > 0:
                    substr = "##" + substr
                if substr in self.vocab:
                    cur = substr
                    break
                end -= 1
            if cur is None:
                return [self.unk_token]
            sub_tokens.append(cur)
            start = end
        return sub_tokens

    def tokenize(self, text: str) -> List[str]:
        out: List[str] = []
        for tok in self._tokenize(text or ""):
            out.extend(self._wordpiece(tok))
        return out

    # ---------------- 编码 ----------------

    def convert_tokens_to_ids(self, tokens: Sequence[str]) -> List[int]:
        unk = self.vocab.get(self.unk_token)
        return [self.vocab.get(t, unk if unk is not None else 0) for t in tokens]

    def encode(self, text: str, max_len: int = 256) -> Dict[str, List[int]]:
        """返回 input_ids / attention_mask / token_type_ids（已 padding 到 max_len）。"""
        tokens: List[str] = []
        for tok in self._tokenize(text or ""):
            tokens.extend(self._wordpiece(tok))
        # 预留 [CLS] / [SEP]
        tokens = tokens[: max(0, max_len - 2)]
        tokens = [self.cls_token] + tokens + [self.sep_token]

        ids = self.convert_tokens_to_ids(tokens)
        pad_id = self.vocab.get(self.pad_token, 0)
        n_pad = max_len - len(ids)
        if n_pad > 0:
            ids = ids + [pad_id] * n_pad
        mask = [1] * len(tokens) + [0] * max(0, n_pad)
        types = [0] * len(ids)
        return {
            "input_ids": ids,
            "attention_mask": mask,
            "token_type_ids": types,
        }

    @property
    def vocab_size(self) -> int:
        return len(self.vocab)
