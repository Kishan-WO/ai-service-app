"""Token counting for chunking (Day 11): the BGE-M3 tokenizer, read from a local file.

Its counts match what Jina billed for my chunks by a fixed rule (notes/day-11, Step 2), so
"400 tokens" here is close to what the embedding model sees. The 17 MB file is not in git.
"""

from functools import lru_cache
from pathlib import Path
from typing import Protocol

from tokenizers import Tokenizer

_HUB_PATTERN = "models--BAAI--bge-m3/snapshots/*/tokenizer.json"


class TokenCounter(Protocol):
    """What a chunker needs from a tokenizer. Counts are CONTENT tokens (no <s> </s>)."""

    def offsets(self, text: str) -> list[tuple[int, int]]:
        """(start, end) character positions of every token, in order."""
        ...

    def count(self, text: str) -> int: ...


class BgeM3TokenCounter:
    def __init__(self, tokenizer_file: Path) -> None:
        self._tokenizer = Tokenizer.from_file(str(tokenizer_file))  # reads the file, no network
        self._tokenizer.no_truncation()
        self._tokenizer.no_padding()

    def offsets(self, text: str) -> list[tuple[int, int]]:
        return list(self._tokenizer.encode(text, add_special_tokens=False).offsets)

    def count(self, text: str) -> int:
        return len(self._tokenizer.encode(text, add_special_tokens=False).ids)


def find_tokenizer_file(configured: str | None) -> Path:
    """TOKENIZER_PATH if set, else the copy in the Hugging Face cache of this machine."""
    if configured:
        path = Path(configured)
        if not path.is_file():
            raise FileNotFoundError(f"TOKENIZER_PATH is not a file: {path}")
        return path
    hub = Path.home() / ".cache" / "huggingface" / "hub"
    found = sorted(hub.glob(_HUB_PATTERN))
    if not found:
        raise FileNotFoundError(
            f"BGE-M3 tokenizer.json not found under {hub}. Set TOKENIZER_PATH to the file."
        )
    return found[-1]


@lru_cache(maxsize=4)
def load_token_counter(tokenizer_file: Path) -> BgeM3TokenCounter:
    """One loaded tokenizer per file: loading 17 MB for every document would be wasteful."""
    return BgeM3TokenCounter(tokenizer_file)
