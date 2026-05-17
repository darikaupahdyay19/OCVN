FROM python:3.10-slim

# Better logs and no pip cache
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# System dependencies.
# libicu + libssl + libstdc++/zlib are required by the .NET runtime that
# ships inside N_m3u8DL-RE; without them the binary aborts (SIGABRT) before
# it even parses arguments, with a stack trace pointing at
# System.Text.EncodingHelper.GetCharset / Console.get_OutputEncoding.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    wget \
    tar \
    xz-utils \
    unzip \
    ca-certificates \
    libicu-dev \
    libssl-dev \
    libstdc++6 \
    zlib1g \
    locales \
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

# Belt-and-suspenders: tell .NET (used by N_m3u8DL-RE) to skip ICU entirely
# in case the runtime still struggles to detect the console encoding inside
# the slim image. Combined with libicu-dev above this should be airtight.
ENV DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# Start your bot
CMD ["python", "bot.py"]
