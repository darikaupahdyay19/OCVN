FROM python:3.10-slim

# Better logs and no pip cache
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    wget \
    tar \
    xz-utils \
    unzip \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Install N_m3u8DL-RE (linux x64 build)
RUN wget https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v0.5.1-beta/N_m3u8DL-RE_v0.5.1-beta_linux-x64_20251029.tar.gz \
    && tar -xzf N_m3u8DL-RE_v0.5.1-beta_linux-x64_20251029.tar.gz \
    && mv N_m3u8DL-RE /usr/local/bin/N_m3u8DL-RE \
    && chmod +x /usr/local/bin/N_m3u8DL-RE \
    && rm -rf N_m3u8DL-RE_v0.5.1-beta_linux-x64_20251029.tar.gz

# Non-root user
RUN useradd -m -u 1000 appuser

WORKDIR /app

# Install Python deps (as root, so site-packages stays root-owned)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code with correct ownership so appuser can write
# session files, downloads, and temp_work in this directory.
COPY --chown=appuser:appuser . .

# Pre-create writable dirs the bot uses at runtime
RUN mkdir -p /app/downloads /app/temp_work /app/sessions \
    && chown -R appuser:appuser /app

# Drop privileges
USER appuser

# Tell the bot where to keep the Pyrogram session file
ENV SESSION_DIR=/app/sessions

# Start your bot
CMD ["python", "bot.py"]
