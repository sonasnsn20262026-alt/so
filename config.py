import os

class Config:
    # التوكن من Railway Environment Variables
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
    
    # Telegram API من Railway أو ملف .env
    API_ID = int(os.environ.get("API_ID", 123456))
    API_HASH = os.environ.get("API_HASH", "")
    
    # قناة المسلسلات
    CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "@your_channel")
    
    # المشرفون (ضع ID الخاص بك)
    ADMIN_IDS = list(map(int, os.environ.get("ADMIN_IDS", "123456789").split(",")))
    
    # إعدادات قاعدة البيانات
    DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///series.db")