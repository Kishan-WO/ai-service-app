# Multi-stage Dockerfile using Astral uv
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

WORKDIR /app

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Install dependencies first (for optimal Docker layer caching)
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-install-project --no-dev

# Copy application source code
COPY . .
RUN uv sync --frozen --no-dev


# Runner stage
FROM python:3.14-slim-bookworm AS runner

WORKDIR /app

# Create a non-root user for security best practices
RUN groupadd -r appuser && useradd -r -g appuser appuser

# The tokenizer that the chunkers count with (Day 11): BAAI/bge-m3 (MIT), one file, 17.1 MB.
# The URL names one revision, so the file cannot change under us, and the build fails if the
# bytes do not match the checksum. It stays above the application code, so that a code change
# does not download it again.
RUN mkdir -p /opt/bge-m3
ADD --checksum=sha256:21106b6d7dab2952c1d496fb21d5dc9db75c28ed361a05f5020bbba27810dd08 \
    --chmod=644 \
    https://huggingface.co/BAAI/bge-m3/resolve/5617a9f61b028005a4858fdac845db406aefb181/tokenizer.json \
    /opt/bge-m3/tokenizer.json
ENV TOKENIZER_PATH=/opt/bge-m3/tokenizer.json

# Copy virtual environment from builder stage
COPY --from=builder --chown=appuser:appuser /app /app

# Place virtual environment on PATH
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

USER appuser

EXPOSE 8000

# Start FastAPI server using uvicorn
CMD ["uvicorn", "ai_service.main:app", "--host", "0.0.0.0", "--port", "8000"]

