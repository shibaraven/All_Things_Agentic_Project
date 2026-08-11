FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt requirements-agent.txt ./
RUN pip install --no-cache-dir -r requirements-agent.txt

COPY shiftzero_core ./shiftzero_core
COPY shiftzero_agents ./shiftzero_agents
COPY shiftzero_cloud ./shiftzero_cloud
COPY services ./services

CMD ["sh", "-c", "exec uvicorn services.api.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
