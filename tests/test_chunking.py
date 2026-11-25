"""Chunker tests (Day 11, task 2).

Most use a small fake tokenizer, so they run on any machine and the numbers can be worked out
by hand: every word is cut into pieces of up to 4 characters, one token each. The last tests use
the real BGE-M3 tokenizer and are skipped where its file is not present.
"""

import random
import re
from collections.abc import Callable

import pytest

from ai_service.core.config import Settings
from ai_service.services.chunking import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    TextChunk,
    add_contextual_headers,
    contextual_header,
    fixed_size_chunks,
    markdown_blocks,
    markdown_chunks,
    recursive_chunks,
)
from ai_service.services.tokenizer import TokenCounter, find_tokenizer_file, load_token_counter


class FakeCounter:
    def offsets(self, text: str) -> list[tuple[int, int]]:
        found = []
        for word in re.finditer(r"\S+", text):
            for start in range(word.start(), word.end(), 4):
                found.append((start, min(start + 4, word.end())))
        return found

    def count(self, text: str) -> int:
        return len(self.offsets(text))


class DriftingCounter(FakeCounter):
    """Like the real tokenizer, a piece counted on its own can be one token longer."""

    def count(self, text: str) -> int:
        return super().count(text) + (1 if len(text.split()) > 1 else 0)


def words(n: int) -> str:
    return " ".join(f"w{i:03d}" for i in range(n))  # 4 characters: one fake token each


def assert_valid(text: str, chunks: list[TextChunk], counter: TokenCounter, limit: int) -> None:
    covered = bytearray(len(text))
    for number, chunk in enumerate(chunks):
        assert chunk.index == number
        assert chunk.text == text[chunk.start : chunk.end]
        assert chunk.text == chunk.text.strip() and chunk.text
        assert chunk.token_count == counter.count(chunk.text) <= limit
        covered[chunk.start : chunk.end] = b"\x01" * (chunk.end - chunk.start)
    lost = [ch for i, ch in enumerate(text) if not covered[i] and not ch.isspace()]
    assert lost == []
    # every chunk starts and ends after the one before it, so the loop always moved forward
    for before, after in zip(chunks, chunks[1:], strict=False):
        assert after.start > before.start and after.end > before.end


@pytest.mark.parametrize("text", ["", "   \n\n  "])
def test_no_text_gives_no_chunks(text: str) -> None:
    assert fixed_size_chunks(text, FakeCounter()) == []


def test_a_short_text_is_one_chunk() -> None:
    chunks = fixed_size_chunks("  one two three \n", FakeCounter(), max_tokens=10, overlap_tokens=2)
    assert [(c.text, c.token_count) for c in chunks] == [("one two three", 4)]


def test_limit_and_overlap_with_numbers_worked_out_by_hand() -> None:
    chunks = fixed_size_chunks(words(10), FakeCounter(), max_tokens=4, overlap_tokens=1)
    assert [c.text for c in chunks] == [
        "w000 w001 w002 w003",
        "w003 w004 w005 w006",
        "w006 w007 w008 w009",
    ]


def test_zero_overlap_repeats_nothing() -> None:
    chunks = fixed_size_chunks(words(10), FakeCounter(), max_tokens=4, overlap_tokens=0)
    assert " ".join(c.text for c in chunks) == words(10)


def test_words_are_not_cut_when_they_fit() -> None:
    text = "alpha bravo charlie delta echo foxtrot golf hotel " * 5
    for chunk in fixed_size_chunks(text, FakeCounter(), max_tokens=6, overlap_tokens=2):
        assert set(chunk.text.split()) <= set(text.split())


def test_a_word_longer_than_the_limit_is_cut_and_nothing_is_lost() -> None:
    text = "start " + "x" * 100 + " end"
    chunks = fixed_size_chunks(text, FakeCounter(), max_tokens=5, overlap_tokens=1)
    assert_valid(text, chunks, FakeCounter(), 5)
    assert sum(chunk.text.count("x") for chunk in chunks) >= 100


def test_each_chunk_is_counted_again_on_its_own() -> None:
    counter = DriftingCounter()
    chunks = fixed_size_chunks(words(30), counter, max_tokens=5, overlap_tokens=1)
    assert_valid(words(30), chunks, counter, 5)  # the plain sum would have given 6


@pytest.mark.parametrize("counter", [FakeCounter(), DriftingCounter()])
def test_random_texts_and_settings_keep_every_rule(counter: TokenCounter) -> None:
    rng = random.Random(11)
    for _ in range(400):
        text = "".join(
            "y" * rng.choice([1, 3, 5, 9, 40]) + rng.choice([" ", " ", "\n", "\n\n", "  "])
            for _ in range(rng.randint(1, 60))
        )
        limit = rng.randint(2, 20)
        overlap = rng.randint(0, limit - 1)
        chunks = fixed_size_chunks(text, counter, max_tokens=limit, overlap_tokens=overlap)
        assert_valid(text, chunks, counter, limit)


def test_the_same_input_gives_the_same_chunks() -> None:
    text = words(200)
    assert fixed_size_chunks(text, FakeCounter()) == fixed_size_chunks(text, FakeCounter())


@pytest.mark.parametrize(("limit", "overlap"), [(0, 0), (10, 10), (10, 11), (10, -1)])
def test_bad_settings_are_refused(limit: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        fixed_size_chunks("text", FakeCounter(), max_tokens=limit, overlap_tokens=overlap)


def test_a_configured_tokenizer_path_must_exist() -> None:
    with pytest.raises(FileNotFoundError, match="TOKENIZER_PATH"):
        find_tokenizer_file("no/such/tokenizer.json")


# ── with the real tokenizer ──────────────────────────────────────────────

REAL_SAMPLES = {
    "english": "A path parameter is declared with braces. FastAPI reads its type.\n\n" * 30,
    "hindi": "प्रकाश सीधी रेखा में चलता है। आपतन कोण परावर्तन कोण के बराबर होता है।\n" * 30,
    "table": "| कोर्स | फ़ीस |\n|---|---|\n" + "| Mathematics course | ₹500 |\n" * 60,
    "long word": "see https://example.com/" + "a1b2c3d4" * 150 + " now",
}


@pytest.fixture(scope="module")
def real_counter() -> TokenCounter:
    try:
        return load_token_counter(find_tokenizer_file(None))
    except FileNotFoundError:
        pytest.skip("the BGE-M3 tokenizer file is not on this machine")


@pytest.mark.parametrize("name", REAL_SAMPLES)
@pytest.mark.parametrize(("limit", "overlap"), [(400, 60), (50, 10), (8, 3)])
def test_real_tokenizer_keeps_every_rule(
    real_counter: TokenCounter, name: str, limit: int, overlap: int
) -> None:
    text = REAL_SAMPLES[name]
    chunks = fixed_size_chunks(text, real_counter, max_tokens=limit, overlap_tokens=overlap)
    assert_valid(text, chunks, real_counter, limit)


def test_settings_defaults_match_the_chunker_defaults() -> None:
    fields = Settings.model_fields
    assert fields["CHUNK_MAX_TOKENS"].default == DEFAULT_MAX_TOKENS
    assert fields["CHUNK_OVERLAP_TOKENS"].default == DEFAULT_OVERLAP_TOKENS
    assert fields["TOKENIZER_PATH"].default is None


# ── recursive splitting ─────────────────────────────────────────────────


def split(text: str, limit: int, overlap: int = 0) -> list[str]:
    chunks = recursive_chunks(text, FakeCounter(), max_tokens=limit, overlap_tokens=overlap)
    assert_valid(text, chunks, FakeCounter(), limit)
    return [c.text for c in chunks]


@pytest.mark.parametrize("text", ["", "   \n\n  "])
def test_recursive_no_text_gives_no_chunks(text: str) -> None:
    assert recursive_chunks(text, FakeCounter()) == []


def test_a_paragraph_that_fits_is_not_cut() -> None:
    text = "aa bb cc\n\ndd ee ff\n\ngg hh ii"
    assert split(text, 5) == ["aa bb cc", "dd ee ff", "gg hh ii"]
    # fixed size would fill the chunk with the first words of the next paragraph
    fixed = fixed_size_chunks(text, FakeCounter(), max_tokens=5, overlap_tokens=0)
    assert fixed[0].text == "aa bb cc\n\ndd ee"


def test_small_neighbours_are_packed_together() -> None:
    assert split("aa bb\n\ncc dd\n\nee ff", 4) == ["aa bb\n\ncc dd", "ee ff"]


def test_a_long_paragraph_is_cut_at_sentences_then_words() -> None:
    assert split("aa bb cc. dd ee ff. gg", 4) == ["aa bb cc.", "dd ee ff. gg"]
    assert split("aa bb cc dd ee ff", 4) == ["aa bb cc dd", "ee ff"]


def test_a_line_break_inside_a_sentence_is_not_a_boundary_of_its_own() -> None:
    # as in text taken from a PDF: the sentence goes on after the line break
    assert split("aa bb. cc\ndd ee.", 4) == ["aa bb.", "cc\ndd ee."]
    # with no sentence end, the words are packed, across the line break
    assert split("aa bb cc\ndd ee ff", 4) == ["aa bb cc\ndd", "ee ff"]


@pytest.mark.parametrize("end_mark", ["।", "॥", "?", "!", '."', ".)"])
def test_sentence_ends_include_the_hindi_danda_and_closing_marks(end_mark: str) -> None:
    first = f"aa bb cc{end_mark}"
    assert split(f"{first} dd ee ff", 5) == [first, "dd ee ff"]


def test_recursive_overlap_repeats_whole_pieces_only() -> None:
    text = "aa bb cc. dd. ee ff gg. hh ii jj."
    # "dd." is one token, so it can be repeated
    assert split(text, 5, overlap=1) == ["aa bb cc. dd.", "dd. ee ff gg.", "hh ii jj."]
    # every piece here is larger than the overlap, so nothing is repeated
    assert split("aa bb cc\n\ndd ee ff", 4, overlap=2) == ["aa bb cc", "dd ee ff"]


def test_recursive_cuts_a_word_longer_than_the_limit() -> None:
    chunks = split("start " + "x" * 100 + " end", 5)
    assert sum(chunk.count("x") for chunk in chunks) == 100


@pytest.mark.parametrize("counter", [FakeCounter(), DriftingCounter()])
def test_recursive_random_texts_and_settings_keep_every_rule(counter: TokenCounter) -> None:
    rng = random.Random(12)
    gaps = [" ", " ", " ", ". ", "। ", "\n", "\n\n", "  "]
    for _ in range(400):
        text = "".join(
            "y" * rng.choice([1, 3, 5, 9, 40]) + rng.choice(gaps) for _ in range(rng.randint(1, 60))
        )
        limit = rng.randint(2, 20)
        overlap = rng.randint(0, limit - 1)
        chunks = recursive_chunks(text, counter, max_tokens=limit, overlap_tokens=overlap)
        assert_valid(text, chunks, counter, limit)


@pytest.mark.parametrize("name", REAL_SAMPLES)
@pytest.mark.parametrize(("limit", "overlap"), [(400, 60), (50, 10), (8, 3)])
def test_recursive_with_the_real_tokenizer_keeps_every_rule(
    real_counter: TokenCounter, name: str, limit: int, overlap: int
) -> None:
    text = REAL_SAMPLES[name]
    chunks = recursive_chunks(text, real_counter, max_tokens=limit, overlap_tokens=overlap)
    assert_valid(text, chunks, real_counter, limit)


def test_recursive_bad_settings_are_refused() -> None:
    with pytest.raises(ValueError):
        recursive_chunks("text", FakeCounter(), max_tokens=10, overlap_tokens=10)


# ── structure-aware: Markdown ───────────────────────────────────────────


def md(text: str, limit: int = 100, overlap: int = 0) -> list[TextChunk]:
    return markdown_chunks(text, FakeCounter(), max_tokens=limit, overlap_tokens=overlap)


def test_each_section_is_its_own_chunk_with_its_heading_path() -> None:
    text = "intro\n\n# A\n\naa\n\n## B\n\nbb\n\n### C\n\ncc\n\n## D\n\ndd\n\n# E\n\nee"
    assert [(c.headings, c.text) for c in md(text)] == [
        ((), "intro"),
        (("A",), "# A\n\naa"),
        (("A", "B"), "## B\n\nbb"),
        (("A", "B", "C"), "### C\n\ncc"),
        (("A", "D"), "## D\n\ndd"),
        (("E",), "# E\n\nee"),
    ]


def test_a_heading_without_text_stays_with_the_next_section() -> None:
    chunks = md("# A\n\n## B\n\nbb")
    assert [(c.headings, c.text) for c in chunks] == [(("A", "B"), "# A\n\n## B\n\nbb")]


def test_a_heading_anchor_is_not_part_of_the_title() -> None:
    assert md("## Use `x` { #use-x }\n\ntext")[0].headings == ("Use `x`",)


@pytest.mark.parametrize("fence", ["```", "~~~", "````", "    ```"])
def test_a_hash_line_inside_code_is_not_a_heading(fence: str) -> None:
    text = f"# A\n\n{fence}python\n# not a heading\n\nx = 1\n{fence}\n\nafter"
    chunks = md(text)
    assert [c.headings for c in chunks] == [("A",)]
    assert [b.kind for b in markdown_blocks(text)] == ["heading", "code", "text"]


def test_an_unclosed_code_block_ends_at_its_last_line_with_text() -> None:
    chunks = md("```\naa\n\nbb\n\n\n")
    assert [c.text for c in chunks] == ["```\naa\n\nbb"]


def test_a_shorter_fence_does_not_close_a_longer_one() -> None:
    text = "````\n```\n# still code\n```\n````\n# Real"
    assert [b.kind for b in markdown_blocks(text)] == ["code", "heading"]


def test_a_code_block_that_fits_is_never_cut() -> None:
    code = "```\naa bb\n\ncc dd\n\nee ff\n```"  # 8 fake tokens, with blank lines inside
    text = f"# A\n\none two\n\n{code}\n\nsix seven"
    chunks = md(text, limit=10)
    assert_valid(text, chunks, FakeCounter(), 10)
    assert any(code in c.text for c in chunks)
    # the recursive chunker sees blank lines, not a code block, and cuts it
    recursive = recursive_chunks(text, FakeCounter(), max_tokens=10, overlap_tokens=0)
    assert not any(code in c.text for c in recursive)


def test_a_table_that_is_too_long_is_cut_only_between_rows() -> None:
    rows = [f"| r{i} | v{i} |" for i in range(12)]  # 5 fake tokens each
    text = "# T\n\n| k | v |\n" + "\n".join(rows)
    chunks = md(text, limit=17)
    assert_valid(text, chunks, FakeCounter(), 17)
    assert len(chunks) > 1
    for row in rows:
        assert any(row in c.text for c in chunks)


def test_a_heading_stays_with_the_text_below_it() -> None:
    text = "# Title\n\naa bb cc. dd ee ff. gg hh."
    chunks = md(text, limit=6)
    assert chunks[0].text == "# Title\n\naa bb cc."


def test_overlap_never_crosses_into_another_section() -> None:
    text = "# A\n\naa. bb. cc. dd. ee. ff.\n\n# B\n\ngg."
    chunks = md(text, limit=5, overlap=2)
    assert_valid(text, chunks, FakeCounter(), 5)
    assert [c.headings for c in chunks if "gg." in c.text] == [("B",)]
    assert all(not ("ff." in c.text and "# B" in c.text) for c in chunks)


def test_text_without_headings_is_one_section() -> None:
    assert [c.headings for c in md("aa bb. " * 40, limit=10)] == [()] * 8


@pytest.mark.parametrize("counter", [FakeCounter(), DriftingCounter()])
def test_markdown_random_texts_keep_every_rule(counter: TokenCounter) -> None:
    rng = random.Random(13)
    pieces = ["y", "yyyyy", "y" * 40, ". ", " ", "\n", "\n\n", "\n# H\n", "\n## H2 y\n"]
    pieces += ["\n```\n", "\n| a | b |\n", "\n~~~\n", "    ", "। "]
    for _ in range(600):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 80)))
        limit = rng.randint(2, 20)
        overlap = rng.randint(0, limit - 1)
        chunks = markdown_chunks(text, counter, max_tokens=limit, overlap_tokens=overlap)
        assert_valid(text, chunks, counter, limit)


@pytest.mark.parametrize("name", REAL_SAMPLES)
@pytest.mark.parametrize(("limit", "overlap"), [(400, 60), (50, 10), (8, 3)])
def test_markdown_with_the_real_tokenizer_keeps_every_rule(
    real_counter: TokenCounter, name: str, limit: int, overlap: int
) -> None:
    text = f"# शीर्षक\n\n{REAL_SAMPLES[name]}\n\n## Code\n\n```python\n# a comment\nx = 1\n```"
    chunks = markdown_chunks(text, real_counter, max_tokens=limit, overlap_tokens=overlap)
    assert_valid(text, chunks, real_counter, limit)


# ── contextual headers ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("title", "headings", "expected"),
    [
        ("Fees", ("Refunds", "Online courses"), "Fees > Refunds > Online courses"),
        ("CORS", ("CORS", "Steps"), "CORS > Steps"),  # the first heading is the title
        ("Use `x`", ("use x",), "Use `x`"),  # the same apart from case and code marks
        ("A paper", (), "A paper"),
        ("", ("Only", "headings"), "Only > headings"),
        ("Two\n  lines", ("a",), "Two lines > a"),
    ],
)
def test_the_header_is_the_title_and_the_section_path(
    title: str, headings: tuple[str, ...], expected: str
) -> None:
    assert contextual_header(title, headings) == expected


def test_without_the_option_the_text_is_embedded_as_it_is() -> None:
    chunk = md("# A\n\naa")[0]
    assert chunk.header == "" and chunk.embedding_text == chunk.text


@pytest.mark.parametrize("chunker", [fixed_size_chunks, recursive_chunks, markdown_chunks])
def test_headers_change_only_what_is_embedded(chunker: Callable[..., list[TextChunk]]) -> None:
    text = "# Guide\n\n## Fees\n\naa bb cc. dd ee ff. gg hh.\n\n## Dates\n\nii jj kk. ll mm."
    plain = chunker(text, FakeCounter(), max_tokens=7, overlap_tokens=2)
    with_headers = add_contextual_headers(text, plain, "Guide")
    assert [(c.text, c.start, c.end, c.token_count) for c in with_headers] == [
        (c.text, c.start, c.end, c.token_count) for c in plain
    ]
    for chunk in with_headers:
        assert chunk.header.startswith("Guide")
        assert chunk.embedding_text == f"{chunk.header}\n\n{chunk.text}"
    assert with_headers[-1].header == "Guide > Dates"


def test_the_header_names_the_section_the_chunk_starts_in() -> None:
    text = "intro words here\n\n# A\n\naa bb\n\n## B\n\ncc dd ee ff gg hh"
    chunks = add_contextual_headers(
        text, fixed_size_chunks(text, FakeCounter(), max_tokens=6, overlap_tokens=0), "Doc"
    )
    got = [(c.text.split()[0], c.header) for c in chunks]
    assert got[0] == ("intro", "Doc")  # before the first heading
    assert got[-1][1] == "Doc > A > B"


def test_a_hash_comment_in_code_never_reaches_the_header() -> None:
    text = "# Real\n\n```\n# returns a dictionary:\n```\n\nafter the code"
    for chunk in add_contextual_headers(
        text, fixed_size_chunks(text, FakeCounter(), max_tokens=5, overlap_tokens=0), "Doc"
    ):
        assert "dictionary" not in chunk.header


def test_headers_are_off_by_default() -> None:
    assert Settings.model_fields["CHUNK_CONTEXT_HEADERS"].default is False


def test_an_empty_html_anchor_is_not_part_of_the_title() -> None:
    assert md('## Using it <a name="x_y"></a>\n\ntext')[0].headings == ("Using it",)
    # other angle brackets in a heading are kept
    assert md("## The `list<int>` type\n\ntext")[0].headings == ("The `list<int>` type",)
