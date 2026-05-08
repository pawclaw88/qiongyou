# ─── Dockerfile for 窮遊 (qiongyou) ────────────────────────────────────────────
#
# Production image — single stage, no dev dependencies.
# Build:
#   docker build -t qiongyou:latest .
#
# Run:
#   docker run -p 8000:8000 \
#     -e QIONGYOU_API_KEYS="your-key-here,another-key" \
#     -e ORS_API_KEY="your-ors-key" \
#     -e QIONGYOU_ALLOWED_ORIGINS="https://your-app.com" \
#     qiongyou:latest

FROM python:3.12-slim

# Install curl for healthchecks (alpine would need it too).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies (no virtualenv — single-service image).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source.
COPY core/           core/
COPY app/            app/
COPY tests/          tests/

EXPOSE 8000

# Health check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run.
ENV QIONGYOU_HOST=0.0.0.0
ENV QIONGYOU_PORT=8000
ENV PYTHONUNBUFFERED=1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
