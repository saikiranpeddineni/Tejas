FROM python:3.11.9-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir -r requirements.lock
COPY . .
RUN python -m compileall -q .

# The default is the offline-safe paper trading loop.  Run webui.py explicitly
# with TEJAS_UI_HOST/TEJAS_UI_TOKEN when exposing the dashboard.
CMD ["python", "tejas.py"]
