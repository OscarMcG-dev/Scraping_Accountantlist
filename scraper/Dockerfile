# Scraper + Playwright/Chromium for Railway (use when Railpack doesn't install browsers).
# In Railway: Service → Settings → Build → Builder = Dockerfile.

FROM python:3.12-slim

WORKDIR /app

# System deps for Playwright/Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium --with-deps

COPY . .

ENV PORT=8000
EXPOSE 8000
# Shell form so $PORT is expanded at runtime (Railway sets PORT).
CMD uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}
