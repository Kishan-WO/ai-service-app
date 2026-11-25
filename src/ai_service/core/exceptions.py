"""Application errors. Each one carries its HTTP status and a stable machine-readable code."""

from typing import Any


class AppError(Exception):
    """Base class for errors that become a JSON error envelope."""

    status_code = 500
    code = "INTERNAL_ERROR"
    message = "Something went wrong."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        self.details = details
        self.headers = headers
        super().__init__(self.message)


class NotAuthenticatedError(AppError):
    status_code = 401
    code = "NOT_AUTHENTICATED"
    message = "Not authenticated."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message, headers={"WWW-Authenticate": "Bearer"})


class InvalidCredentialsError(AppError):
    status_code = 401
    code = "INVALID_CREDENTIALS"
    message = "Incorrect email or password."
    # Same message for "unknown email" and "wrong password", so emails cannot be probed.

    def __init__(self) -> None:
        super().__init__(headers={"WWW-Authenticate": "Bearer"})


class EmailAlreadyRegisteredError(AppError):
    status_code = 409
    code = "EMAIL_ALREADY_REGISTERED"
    message = "An account with this email already exists."


class DocumentNotFoundError(AppError):
    """Also used when the document belongs to another tenant: never reveal that it exists."""

    status_code = 404
    code = "DOCUMENT_NOT_FOUND"
    message = "Document not found."


class DuplicateDocumentError(AppError):
    status_code = 409
    code = "DUPLICATE_DOCUMENT"

    def __init__(self, document_id: object) -> None:
        super().__init__(
            f"This document was already uploaded (id {document_id}).",
            details={"document_id": str(document_id)},
        )


class ConversationNotFoundError(AppError):
    """Also used for another tenant's or user's conversation: never reveal that it exists."""

    status_code = 404
    code = "CONVERSATION_NOT_FOUND"
    message = "Conversation not found."


class LLMNotConfiguredError(AppError):
    status_code = 503
    code = "LLM_NOT_CONFIGURED"
    message = "The language model is not configured on this server."


class LLMUnavailableError(AppError):
    """The provider failed (down, rate limited, bad key, timeout). Details stay in the logs."""

    status_code = 502
    code = "LLM_UNAVAILABLE"
    message = "The language model could not be reached. Please try again."


class SummariseFailedError(AppError):
    """Too many documents could not be summarised, so no combined overview was built."""

    status_code = 502
    code = "SUMMARISE_FAILED"
    message = "Too many documents could not be summarised. Please try again."


class DocumentTooLongError(AppError):
    status_code = 413
    code = "DOCUMENT_TOO_LONG"
    message = "A document is too long to summarise in one call."


class ExtractionFailedError(AppError):
    status_code = 422
    code = "EXTRACTION_FAILED"
    message = "The model did not return data in the expected shape."


class ExtractionRefusedError(AppError):
    """The model, or a safety filter in front of it, declined to read the text."""

    status_code = 422
    code = "EXTRACTION_REFUSED"
    message = "The model declined to process this text."


class UnsupportedFileTypeError(AppError):
    status_code = 415
    code = "UNSUPPORTED_FILE_TYPE"
    message = "Only .txt, .md and .pdf files are supported."


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "FILE_TOO_LARGE"

    def __init__(self, max_bytes: int) -> None:
        super().__init__(f"The file is larger than the {max_bytes // (1024 * 1024)} MB limit.")


class InvalidDocumentError(AppError):
    status_code = 422
    code = "INVALID_DOCUMENT"
    message = "The file could not be read as a document."


class VectorStoreError(AppError):
    status_code = 503
    code = "VECTOR_STORE_UNAVAILABLE"
    message = "The vector store is not available. Please try again."

