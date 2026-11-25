"""Reading an original file into text, page by page (Day 11, task 1: "parse")."""

import io
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

from pypdf import PdfReader

from ai_service.core.exceptions import InvalidDocumentError, UnsupportedFileTypeError

# Part of the pipeline version: text parsed by another parser or version may differ.
PDF_PARSER = f"pypdf-{version('pypdf')}"


@dataclass(frozen=True)
class ParsedDocument:
    """What the parser found, before any cleaning."""

    kind: str  # "pdf", "markdown" or "text"
    pages: tuple[str, ...]  # one string per PDF page; a text file is a single "page"
    parser: str


_KINDS = {".pdf": "pdf", ".md": "markdown", ".txt": "text"}


def kind_of(filename: str) -> str:
    """ "pdf", "markdown" or "text", from the file name."""
    try:
        return _KINDS[Path(filename).suffix.lower()]
    except KeyError:
        raise UnsupportedFileTypeError() from None


def parse_file(filename: str, data: bytes) -> ParsedDocument:
    """Blocking and CPU-bound for a PDF, so async callers run it in a thread.

    Every error raised here is permanent: trying the same file again cannot help.
    """
    kind = kind_of(filename)
    if kind == "pdf":
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise InvalidDocumentError("Encrypted PDFs are not supported.")
            pages = tuple(page.extract_text() or "" for page in reader.pages)
        except InvalidDocumentError:
            raise
        except Exception as exc:  # pypdf raises many different errors for broken files
            raise InvalidDocumentError("The PDF could not be read.") from exc
        return ParsedDocument("pdf", pages, PDF_PARSER)
    try:
        text = data.decode("utf-8-sig")  # also removes a byte-order mark
    except UnicodeDecodeError as exc:
        raise InvalidDocumentError("The file is not valid UTF-8 text.") from exc
    return ParsedDocument(kind, (text,), "utf-8")
