FROM denoland/deno:bin AS deno
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=10000 REEL_OUTPUT_DIR=/tmp/reel
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deno /deno /usr/local/bin/deno
WORKDIR /app
RUN pip install --no-cache-dir "yt-dlp[default]"
COPY companion/server.py /app/companion/server.py
COPY cloud/service.py /app/cloud/service.py
RUN useradd --create-home reel && mkdir -p /tmp/reel && chown reel:reel /tmp/reel
USER reel
EXPOSE 10000
CMD ["python", "cloud/service.py"]
