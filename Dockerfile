FROM python:3.12-slim

# Timezone — so that dates in the UI are shown on Israel time.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ_NAME=Asia/Jerusalem \
    DB_PATH=/data/warehouse.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY config/ ./config/
COPY data/Inventory_Report.csv ./data/Inventory_Report.csv

# Relevant only in SQLite mode (no DATABASE_URL): there the database is a file,
# and it needs a persistent disk or the data is wiped on every deploy.
# When DATABASE_URL is set the data lives in Postgres, the container is
# stateless, and this volume simply goes unused.
VOLUME ["/data"]
EXPOSE 8000

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /data && chown -R appuser:appuser /data /app
USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",8000)}/healthz')"

CMD ["python", "-m", "app.server"]
