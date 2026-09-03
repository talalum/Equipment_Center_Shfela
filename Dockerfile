FROM python:3.12-slim

# אזור זמן — כדי שתאריכים בממשק יוצגו בשעון ישראל.
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

# רלוונטי רק במצב SQLite (בלי DATABASE_URL): שם המסד הוא קובץ, והוא
# חייב דיסק קבוע אחרת הנתונים נמחקים בכל דיפלוי.
# כשמוגדר DATABASE_URL הנתונים יושבים ב-Postgres, הקונטיינר חסר-מצב,
# והנפח הזה פשוט אינו בשימוש.
VOLUME ["/data"]
EXPOSE 8000

# הרצה כמשתמש לא-root.
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /data && chown -R appuser:appuser /data /app
USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",8000)}/healthz')"

CMD ["python", "-m", "app.server"]
