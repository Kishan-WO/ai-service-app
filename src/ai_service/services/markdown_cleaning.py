"""Cleaning rules for Markdown (Day 11, task 6): what a documentation website would turn into
something else, or not show at all. The rules follow what Day 9 did to my corpus, but narrower,
because they run on every uploaded Markdown file:

- front matter at the very start, only if it looks like "key: value" lines;
- HTML comments, only if they are closed before the next code fence;
- lines that only include a code file: {* path *}, {! path !}, --8<-- "path";
- documentation-tool lines naming a Python object: "::: package.name";
- "/// tip | Title" ... "///" boxes (only when both ends are there): "Tip: Title";
- '!!! note "Title"' boxes: "Note: Title", and their text is moved back to the left;
- heading ids: "## Title { #title }" becomes "## Title".

Nothing inside a fenced code block or inline code is changed. The one exception: the whole body
of a "!!!" box moves left, its code blocks included.
"""

import re
from collections import Counter

_FENCE = re.compile(r"[ \t]*(`{3,}|~{3,})")
_FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?)^---[ \t]*(?:\n|\Z)", re.S | re.M)
# a line that can be part of front matter: blank, a comment, indented, a list item, or "key:"
_FRONT_MATTER_LINE = re.compile(r"$|#|[ \t]|-[ \t]|[\w.\-]+:(?:[ \t]|$)")
# inline code (which is kept), or the start of a comment
_CODE_OR_COMMENT = re.compile(r"(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)|<!--")
_INCLUDE = re.compile(r"[ \t]*(?:\{\*.*\*\}|\{!.*!\}|--8<--.*)[ \t]*")
_DIRECTIVE = re.compile(r"[ \t]*:::[ \t]+[A-Za-z_]\w*(?:\.\w+)+[ \t]*")  # ::: pydantic.fields
_SLASH_OPEN = re.compile(r"([ \t]*)///(?!/)[ \t]*([A-Za-z][\w-]*)[ \t]*(?:\|[ \t]*(.*?))?[ \t]*")
_SLASH_CLOSE = re.compile(r"[ \t]*///[ \t]*")
_BANG = re.compile(r'([ \t]*)!!![ \t]+(\w+)(?:[ \t]+"(.*)")?[ \t]*')
_HEADING_ID = re.compile(r"([ \t]{0,3}#{1,6}[ \t].*?)[ \t]*\{[ \t]*#[\w\-]+[ \t]*\}[ \t]*")


def _fence_after(line: str, fence: str) -> str:
    """The mark of the code block that is open after this line ("" outside code)."""
    if fence:
        stripped = line.strip()
        closes = stripped.startswith(fence) and set(stripped) == {fence[0]}
        return "" if closes else fence
    opening = _FENCE.match(line)
    return opening.group(1) if opening else ""


def _remove_front_matter(text: str, done: Counter[str]) -> str:
    found = _FRONT_MATTER.match(text)
    if not found:
        return text
    lines = found.group(1).splitlines()
    if not any(":" in line for line in lines):
        return text  # two "---" dividers with prose between them are not front matter
    if not all(_FRONT_MATTER_LINE.match(line) for line in lines):
        return text
    done["front matter removed"] += 1
    return text[found.end() :]


def _comment_end(lines: list[str], index: int, after: int) -> tuple[int, int] | None:
    """Where the comment that starts on lines[index] ends: (line, position after "-->").
    None if it is not closed before the next code fence."""
    end = lines[index].find("-->", after)
    if end != -1:
        return index, end + 3
    for later in range(index + 1, len(lines)):
        if _FENCE.match(lines[later]):
            return None
        end = lines[later].find("-->")
        if end != -1:
            return later, end + 3
    return None


def _remove_comments(lines: list[str], done: Counter[str]) -> list[str]:
    lines = list(lines)
    out: list[str] = []
    fence = ""
    index = 0
    while index < len(lines):
        line = lines[index]
        if not fence and not _FENCE.match(line):
            position = 0
            while found := _CODE_OR_COMMENT.search(line, position):
                if found.group() != "<!--":
                    position = found.end()  # inline code: step over it
                    continue
                lines[index] = line
                end = _comment_end(lines, index, found.end())
                if end is None:
                    break
                last, after = end
                position = found.start()
                line = line[:position] + lines[last][after:]
                index = last  # the lines in between were the comment
                done["HTML comments removed"] += 1
        out.append(line)
        fence = _fence_after(line, fence)
        index += 1
    return out


def _paired_slash_lines(lines: list[str]) -> tuple[set[int], set[int]]:
    """The openings and closings of "///" boxes that have both."""
    openings: set[int] = set()
    closings: set[int] = set()
    waiting: list[int] = []
    fence = ""
    for index, line in enumerate(lines):
        if not fence:
            if _SLASH_OPEN.fullmatch(line):
                waiting.append(index)
            elif _SLASH_CLOSE.fullmatch(line) and waiting:
                openings.add(waiting.pop())
                closings.add(index)
        fence = _fence_after(line, fence)
    return openings, closings


def _label(indent: str, kind: str, title: str | None) -> str:
    return f"{indent}{kind.capitalize()}: {title}" if title else f"{indent}{kind.capitalize()}:"


def clean_markdown(text: str, done: Counter[str]) -> str:
    """`text` uses "\\n" line ends. `done` counts what each rule did."""
    lines = _remove_comments(_remove_front_matter(text, done).split("\n"), done)
    openings, closings = _paired_slash_lines(lines)

    out: list[str] = []
    fence = ""
    body_indent: str | None = None  # inside a "!!!" box: what its text is indented by
    for index, line in enumerate(lines):
        if body_indent is not None and line.strip():
            if line.startswith(body_indent + "    "):
                line = body_indent + line[len(body_indent) + 4 :]
            else:
                body_indent = None
        if not fence:
            if _INCLUDE.fullmatch(line):
                done["code include lines removed"] += 1
                continue
            if _DIRECTIVE.fullmatch(line):
                done["documentation tool lines removed"] += 1
                continue
            if index in closings:
                continue
            if index in openings and (box := _SLASH_OPEN.fullmatch(lines[index])):
                done["note boxes rewritten"] += 1
                line = _label(box.group(1), box.group(2), box.group(3))
            elif box := _BANG.fullmatch(line):
                done["note boxes rewritten"] += 1
                body_indent = box.group(1)
                line = _label(box.group(1), box.group(2), box.group(3))
            elif heading := _HEADING_ID.fullmatch(line):
                done["heading ids removed"] += 1
                line = heading.group(1)
        out.append(line)
        fence = _fence_after(line, fence)
    return "\n".join(out)
