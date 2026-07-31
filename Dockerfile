FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/
COPY pyproject.toml /app/pyproject.toml

RUN mkdir -p /app/cache /app/logs \
    && useradd -m appuser \
    && chown -R appuser:appuser /app

USER appuser

ENV PYTHONUNBUFFERED=1
ENV BRIDGE_CONFIG=/app/config.yaml

CMD ["python", "-m", "src.bot"]
