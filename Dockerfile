FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates curl unzip git \
    && rm -rf /var/lib/apt/lists/*

# Deno is used by yt-dlp's JS challenge support and the local BgUtils PO-token provider.
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | sh \
    && deno --version

# Install the BgUtils provider runtime matching the Python plugin version.
RUN git clone --depth 1 --branch 1.3.1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil-ytdlp-pot-provider \
    && cd /opt/bgutil-ytdlp-pot-provider/server \
    && deno install --allow-scripts=npm:canvas --frozen

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN chmod +x /app/start.sh
ENV PORT=10000
ENV YOUTUBE_POT_ENABLED=true
ENV YOUTUBE_POT_BASE_URL=http://127.0.0.1:4416
ENV YOUTUBE_PLAYER_CLIENT=mweb
CMD ["/app/start.sh"]
