FROM python:3.10-slim

# Install system dependencies
# ffmpeg: for muxing
# curl, unzip: for downloading/extracting tools
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    wget \
    tar \
    xz-utils \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install N_m3u8DL-RE
RUN wget https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v0.5.1-beta/N_m3u8DL-RE_v0.5.1-beta_linux-x64_20251029.tar.gz \
    && tar -xzf N_m3u8DL-RE_v0.2.0_linux-x64.tar.gz \
    && mv N_m3u8DL-RE_v0.2.0_linux-x64/N_m3u8DL-RE /usr/local/bin/N_m3u8DL-RE \
    && chmod +x /usr/local/bin/N_m3u8DL-RE \
    && rm -rf N_m3u8DL-RE_v0.2.0_linux-x64.tar.gz N_m3u8DL-RE_v0.2.0_linux-x64

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
