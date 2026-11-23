FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Kolkata

# tzdata is required: your code calls ZoneInfo("Asia/Kolkata"),
# and slim images often ship without the zoneinfo database
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# Run as a non-root user
RUN useradd --create-home --uid 1000 app
WORKDIR /app

# Install deps first so this layer is cached until requirements.txt changes
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN mkdir -p /app/output && chown -R app:app /app
USER app

CMD ["python", "main.py"]