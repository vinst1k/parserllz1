FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates fonts-liberation libnss3 libxss1 libasound2 \
    libatk-bridge2.0-0 libatk1.0-0 libcups2 libdrm2 libxcomposite1 \
    libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libnspr4 \
    libx11-xcb1 xdg-utils && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    playwright install chromium && \
    playwright install-deps chromium

COPY anketa_bot.py .

CMD ["python", "anketa_bot.py"]
