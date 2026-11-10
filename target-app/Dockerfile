FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

RUN pip install --no-cache-dir "fastapi>=0.115.0" "uvicorn>=0.30.0" "pydantic>=2.8.0" \
    && useradd --create-home --uid 10001 appuser

COPY app.py /app/app.py

USER 10001
EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "warning"]
