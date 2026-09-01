"""
אתחול חבילת הטסטים.

חייב לרוץ לפני שנטען app.config — ולכן הוא כאן ולא ב-base.py. בלי זה קובץ
.env אמיתי שיושב על מחשב המפתחת נטען לתוך הטסטים, והם נכשלים או עוברים
מסיבות שאינן קשורות לקוד.
"""
from __future__ import annotations

import os

# נתיב שאינו קובץ — טעינת .env תחזיר מיד dict ריק.
os.environ["ENV_FILE"] = os.devnull
