FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY bot ./bot

RUN useradd --system --create-home --shell /usr/sbin/nologin botuser \
    && mkdir -p /app/data \
    && chown -R botuser:botuser /app

USER botuser

# Каталог состояния монтируйте как volume — база переживёт пересоздание контейнера.
VOLUME ["/app/data"]
ENV DB_PATH=/app/data/state.db

CMD ["python", "-m", "bot"]
