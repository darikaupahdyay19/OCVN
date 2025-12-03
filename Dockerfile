FROM python:3.10-slim

# Install system dependencies
# ffmpeg: for muxing
# curl, unzip: for downloading/extracting tools
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# We will handle the N_m3u8DL-RE download in the python script, 
# but we need to ensure the script knows to download the LINUX version.

CMD ["python", "bot.py"]
