FROM python:3.10-bookworm
# ffprobe: camera checks; ffmpeg: demo placeholder images
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
RUN useradd -r -u 10001 app && mkdir -p /data/media && chown app /data/media
USER app
EXPOSE 8000
CMD ["sh", "-c", "python -m alembic upgrade head && exec uvicorn src.main:app --host 0.0.0.0 --port 8000 --proxy-headers"]
