import os
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
from config import Config

# إنشاء محرك قاعدة البيانات
if Config.DATABASE_URL.startswith("postgres"):
    engine = create_engine(Config.DATABASE_URL)
else:
    engine = create_engine(Config.DATABASE_URL)

Base = declarative_base()
Session = sessionmaker(bind=engine)

# تعريف النماذج
# تعريف Series المصحّح في database.py
class Series(Base):
    __tablename__ = 'series'
    
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False, unique=True)  # هذا العمود موجود
    created_at = Column(DateTime, default=datetime.utcnow)   # هذا موجود
    # احذف الأعمدة التالية إذا كانت موجودة في كودك:
    # description = Column(Text)
    # category = Column(String(100))
    # cover_image = Column(String(500))
    # is_active = Column(Integer, default=1)

class Episode(Base):
    __tablename__ = 'episodes'
    
    id = Column(Integer, primary_key=True)
    series_id = Column(Integer, nullable=False)
    season = Column(Integer, default=1)
    episode_number = Column(Integer, nullable=False)
    title = Column(String(255))
    telegram_message_id = Column(Integer, nullable=False)
    telegram_channel_id = Column(String(255), nullable=False)
    quality = Column(String(50))
    duration = Column(String(50))
    added_at = Column(DateTime, default=datetime.utcnow)

class UserFavorite(Base):
    __tablename__ = 'user_favorites'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    series_id = Column(Integer, nullable=False)
    added_at = Column(DateTime, default=datetime.utcnow)

# إنشاء الجداول
def init_db():
    Base.metadata.create_all(engine)

# فئات المساعدة
class DatabaseManager:
    def __init__(self):
        self.session = Session()
    
    def add_series(self, name, description="", category="عام", cover_image=""):
        series = Series(
            name=name,
            description=description,
            category=category,
            cover_image=cover_image
        )
        self.session.add(series)
        self.session.commit()
        return series.id
    
    def get_all_series(self):
        return self.session.query(Series).filter_by(is_active=1).all()
    
    def close(self):
        self.session.close()

# تهيئة قاعدة البيانات عند الاستيراد
init_db()
