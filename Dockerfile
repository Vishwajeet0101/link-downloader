FROM python:3.13-slim

# ffmpeg joins HD video + audio and makes MP3s; Deno lets yt-dlp unlock YouTube formats
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
RUN pip install --no-cache-dir -U "yt-dlp[default]" qrcode

# run as a normal user, not root
RUN useradd -m -u 1000 user
USER user
WORKDIR /home/user/app
COPY --chown=user engine.py server.py cloud.py ./

ENV CLOUD=1 PORT=10000 OUT_DIR=/tmp/downloads PYTHONUNBUFFERED=1
EXPOSE 10000
CMD ["python", "cloud.py"]
