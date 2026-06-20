# Reproducible runner image. The Playwright base image ships Chromium and all the
# system libraries it needs, which is the fiddly part of running browsers in Docker.
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml ./
COPY qascan ./qascan
COPY vendor ./vendor
COPY migrations ./migrations
COPY alembic.ini ./
RUN pip install --no-cache-dir -e .

# Browsers are already installed in the base image; ensure Chromium is present.
RUN python -m playwright install chromium

ENTRYPOINT []
CMD ["qascan", "--help"]
