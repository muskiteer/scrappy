FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libxshmfence1 \
    --no-install-recommends && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create user first
RUN useradd -ms /bin/bash scraper

# Install Chromium AS the scraper user so it goes to /home/scraper/.cache
RUN su scraper -c "playwright install chromium"

COPY scraper.py main.py db.py session.json ./
RUN chown -R scraper /app

USER scraper

CMD ["python", "main.py"]