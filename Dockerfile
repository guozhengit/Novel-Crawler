FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY . .

# Playwright installs the Chromium revision matching the Python package and
# the required system libraries in one build layer.
RUN pip install --no-cache-dir . \
    && python -m playwright install --with-deps chromium \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin novel \
    && install -d -o novel -g novel /app/data

USER novel

VOLUME ["/app/data"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD ["novel-crawler", "--data-dir", "/app/data", "env"]

ENTRYPOINT ["novel-crawler", "--data-dir", "/app/data"]
