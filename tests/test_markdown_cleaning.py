"""The Markdown cleaning rules (Day 11, task 6)."""

from collections import Counter

import pytest

from ai_service.services.cleaning import CLEANER_VERSION, clean_document
from ai_service.services.markdown_cleaning import clean_markdown
from ai_service.services.parsing import parse_file


def clean(text: str) -> str:
    return clean_markdown(text, Counter())


def unchanged(text: str) -> None:
    done: Counter[str] = Counter()
    assert clean_markdown(text, done) == text
    assert not done


# ── what is removed or rewritten ────────────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        "{* ../../docs_src/first_steps/tutorial001.py *}",
        "{* ../../docs_src/dependencies/tutorial001.py hl[3] *}",
        "    {* ../../docs_src/a.py ln[1:5] *}",
        "{!../../docs_src/templates/item.html!}",
        "{!> ../../docs_src/generate_clients/tutorial004.js!}",
        '--8<-- "docs/snippets/a.py"',
        "::: pydantic.aliases",
        "    ::: pydantic.fields.Field",
    ],
)
def test_include_and_tool_lines_are_removed(line: str) -> None:
    assert clean(f"before\n{line}\nafter") == "before\nafter"


def test_a_slash_box_keeps_its_text_and_gets_a_label() -> None:
    text = "/// tip\n\nUse it.\n\n///\n\n/// note | Technical Details\n\nMore.\n\n///"
    assert clean(text) == "Tip:\n\nUse it.\n\n\nNote: Technical Details\n\nMore.\n"


def test_slash_boxes_inside_each_other() -> None:
    assert clean("/// note\nouter\n/// tip\ninner\n///\n///") == "Note:\nouter\nTip:\ninner"


def test_a_bang_box_gets_a_label_and_moves_left() -> None:
    text = (
        '!!! warning "Careful"\n    First line.\n\n    ```python\n    if x:\n        y()\n'
        "    ```\n\nOutside.\n    still indented"
    )
    assert clean(text) == (
        "Warning: Careful\nFirst line.\n\n```python\nif x:\n    y()\n```\n\nOutside.\n"
        "    still indented"
    )
    assert clean("!!! note\n    text") == "Note:\ntext"


@pytest.mark.parametrize(
    ("line", "cleaned"),
    [
        ("# First Steps { #first-steps }", "# First Steps"),
        ("### Use `CORSMiddleware` { #use-corsmiddleware }", "### Use `CORSMiddleware`"),
        ("###### Deep {#a_b}", "###### Deep"),
    ],
)
def test_heading_ids_are_removed(line: str, cleaned: str) -> None:
    assert clean(line) == cleaned


def test_front_matter_is_removed() -> None:
    assert clean("---\ntitle: A\ntags:\n  - one\n- two\n\n# c\n---\n# Body") == "# Body"


@pytest.mark.parametrize(
    ("text", "cleaned"),
    [
        ("a <!-- x --> b", "a  b"),
        ("<!-- one --><!-- two -->kept", "kept"),
        ("a\n<!--\nhidden\n-->\nb", "a\n\nb"),
        ("a <!-- starts\nhidden\nends --> b <!-- again --> c", "a  b  c"),
        ("`<!-- kept -->` and <!-- gone -->", "`<!-- kept -->` and "),
    ],
)
def test_comments_are_removed(text: str, cleaned: str) -> None:
    assert clean(text) == cleaned


# ── what must NOT change ────────────────────────────────────────────────


@pytest.mark.parametrize("fence", ["```", "~~~", "````"])
def test_nothing_inside_a_code_block_changes(fence: str) -> None:
    code = [
        "{* ../../docs_src/a.py *}",
        "{!a.html!}",
        '--8<-- "a"',
        "::: pydantic.aliases",
        "/// tip",
        "///",
        '!!! note "x"',
        "    body",
        "## Title { #title }",
        "<!-- comment -->",
    ]
    unchanged("\n".join([f"{fence}text", *code, fence]))


def test_a_shorter_fence_does_not_close_a_longer_one() -> None:
    unchanged("````md\n```\n{* a.py *}\n```\n````")


def test_a_code_block_that_is_never_closed_is_left_alone() -> None:
    unchanged("```\n{* a.py *}\n<!-- c -->")


def test_rules_work_again_after_a_code_block() -> None:
    assert clean("```\n{* a.py *}\n```\n{* b.py *}\nend") == "```\n{* a.py *}\n```\nend"


@pytest.mark.parametrize(
    "text",
    [
        "See {* this *} in a sentence.",
        "The marker `{* a.py *}` includes a file.",
        ":::note\ntext\n:::",  # another tool's box: its text matters, and so may its title
        "::: warning Read this first\ntext\n:::",
        "::: note",
        "/// This is a documentation comment",
        "/// tip\nno closing line",
        "text\n///\nmore",
        "//// tab | Python\ntext\n////",
        "??? note\n    folded",
        "Really!!! Yes",
        "A line with { #not-a-heading }",
        "## Sets { 1, 2 }",
        "## Braces {#a} in the middle",
    ],
)
def test_lines_that_only_look_similar_stay(text: str) -> None:
    unchanged(text)


@pytest.mark.parametrize(
    "text",
    [
        "---\n\nA story between two dividers: it begins here.\n\n---\n\nMore.",
        "---\nJust a sentence without a key\n---\nbody",
        "---\n---\nbody",
        "---\ntitle: never closed\n# Body",
        "# Title\n\n---\nkey: value\n---",
    ],
)
def test_only_real_front_matter_is_removed(text: str) -> None:
    unchanged(text)


@pytest.mark.parametrize(
    "text",
    [
        "a <!-- never closed\n\n# Chapter two\n\nimportant",
        # the "-->" belongs to a diagram, not to the stray "<!--" above it
        "Type <!-- to start.\n\n## Flow\n\n```mermaid\nA --> B\n```\n\nend",
        "`<!--` opens a comment.\n\nKeep this.\n\n`-->` closes it.",
    ],
)
def test_a_stray_comment_start_does_not_swallow_the_file(text: str) -> None:
    unchanged(text)


# ── in the cleaning stage ───────────────────────────────────────────────

SOURCE = (
    b"---\r\ntitle: x\r\n---\r\n# Guide { #guide }\r\n\r\n{* ../../docs_src/a.py *}\r\n\r\n"
    b"/// tip\r\n\r\nHello.\r\n\r\n///\r\n"
)


def test_a_markdown_file_is_cleaned_and_the_work_is_counted() -> None:
    result = clean_document(parse_file("guide.md", SOURCE))
    assert result.text == "# Guide\n\nTip:\n\nHello."
    assert result.removed == {
        "front matter removed": 1,
        "heading ids removed": 1,
        "code include lines removed": 1,
        "note boxes rewritten": 1,
    }


def test_a_plain_text_file_is_not_treated_as_markdown() -> None:
    result = clean_document(parse_file("notes.txt", SOURCE))
    assert "{* ../../docs_src/a.py *}" in result.text and "{ #guide }" in result.text


def test_cleaning_twice_changes_nothing_more() -> None:
    once = clean(SOURCE.decode().replace("\r\n", "\n"))
    assert clean(once) == once


def test_the_version_changed_with_the_rules() -> None:
    """The version is part of the pipeline version: saved documents are processed again."""
    assert CLEANER_VERSION == "2"
