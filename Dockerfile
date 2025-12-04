FROM python:3.10-slim

# Make Python output unbuffered (better logs on PaaS) and avoid pip cache
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Install system dependencies
# ffmpeg     : for muxing
# curl,wget  : for downloading tools
# tar,xz,unzip: for extracting archives
# ca-certificates: for HTTPS (very important on hosts like Railway)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    wget \
    tar \
    xz-utils \
    unzip \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# ---- Install N_m3u8DL-RE (x86_64 / amd64 build) ----
# NOTE:
#  - This URL is for linux-x64. It will work on most common cloud hosts (which are amd64).
#  - On ARM hosts (e.g., some Raspberry Pi or ARM VPS), you’ll need to switch to an ARM build.
RUN wget https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v0.5.1-beta/N_m3u8DL-RE_v0.5.1-beta_linux-x64_20251029.tar.gz \
    && tar -xzf N_m3u8DL-RE_v0.5.1-beta_linux-x64_20251029.tar.gz \
    && mv N_m3u8DL-RE_v0.5.1-beta_linux-x64/N_m3u8DL-RE /usr/local/bin/N_m3u8DL-RE \
    && chmod +x /usr/local/bin/N_m3u8DL-RE \
    && rm -rf N_m3u8DL-RE_v0.5.1-beta_linux-x64_20251029.tar.gz N_m3u8DL-RE_v0.5.1-beta_linux-x64

# Create a non-root user (good for security & some platforms)
RUN useradd -m appuser

WORKDIR /app

# Install Python dependencies first (better Docker layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the app
COPY . .

# Change to non-root user
USER appuser

# If your app exposes a web server on Railway/Render/etc,
# make sure it listens on 0.0.0.0 and uses the PORT env var.
# For example in Python:
#   port = int(os.environ.get("PORT", 8000))
#   app.run(host="0.0.0.0", port=port)
#
# For a bot with no HTTP server, this is fine:
CMD ["python", "bot.py"]
