FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY pyproject.toml README.md README.zh-CN.md LICENSE ./
COPY orchestrator orchestrator
COPY voice voice
COPY providers providers
COPY meet meet
COPY scripts scripts
COPY web web
COPY config config
COPY main.py ./

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 meet \
    && mkdir -p /app/runs /app/links \
    && chown -R meet:meet /app

USER meet
EXPOSE 8787
CMD ["meet", "worker"]
