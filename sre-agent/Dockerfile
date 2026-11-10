FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false

WORKDIR /app

# Cloud Run instances 上で隔離サンドボックス (sandbox do) 内からも python3 や統計計算を実行できるよう配置
COPY pyproject.toml README.md LICENSE /app/
COPY sre-agent/ /app/sre-agent/

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir \
       "google-adk[db,gcp,otel-gcp]>=2.13.0,<3.0.0" \
       "mcp>=1.10.0" \
       "google-genai>=2.30.1" \
       "google-cloud-bigquery>=3.25.0" \
       "google-cloud-pubsub>=2.23.0" \
       "google-cloud-monitoring>=2.22.0" \
       "google-cloud-logging>=3.11.0" \
       "google-cloud-container>=2.50.0" \
       "google-cloud-firestore>=2.16.0" \
       "kubernetes>=30.1.0" \
       "slack-bolt>=1.20.0" \
       "aiohttp>=3.10.0" \
       "aiosqlite>=0.20.0" \
       "sqlalchemy[asyncio]>=2.0.0" \
       "greenlet>=3.0.0" \
       "pydantic>=2.8.0" \
       "python-dotenv>=1.0.1" \
    && useradd --create-home --uid 10001 sreagent

ENV PYTHONPATH="/app/sre-agent"

EXPOSE 8080

USER 10001

CMD ["python3", "-m", "agent_app.main"]
