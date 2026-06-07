import os
from datetime import datetime
from sqlalchemy import create_engine, Column, String, Integer, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./tiktok_bot.db")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Profile(Base):
    __tablename__ = "profiles"

    username = Column(String, primary_key=True)
    display_name = Column(String, nullable=True)
    avatar_url = Column(String, nullable=True)
    bio = Column(String, nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)
    last_checked_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)
    video_count = Column(Integer, default=0)

    videos = relationship("Video", back_populates="profile", cascade="all, delete-orphan")


class Video(Base):
    __tablename__ = "videos"

    video_id = Column(String, primary_key=True)
    username = Column(String, ForeignKey("profiles.username"), nullable=False)
    title = Column(String, nullable=True)
    thumbnail_url = Column(String, nullable=True)
    local_thumbnail = Column(String, nullable=True)
    duration = Column(Integer, nullable=True)
    view_count = Column(Integer, nullable=True)
    like_count = Column(Integer, nullable=True)
    tiktok_url = Column(String, nullable=True)
    sent_at = Column(DateTime, default=datetime.utcnow)
    downloaded_at = Column(DateTime, default=datetime.utcnow)

    profile = relationship("Profile", back_populates="videos")


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
