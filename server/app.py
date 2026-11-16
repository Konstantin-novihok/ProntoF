from __future__ import annotations

import datetime as dt
import json
import os
import re
import hmac
import secrets
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pwdlib import PasswordHash
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

DATA_DIR = Path(os.getenv("DATA_DIR", "/data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
VIDEO_DIR = DATA_DIR / "videos"
ORDER_DATA_DIR = Path(os.getenv("ORDER_DATA_DIR", str(DATA_DIR))).resolve()
JOB_DIR = DATA_DIR / "jobs"
for folder in (DATA_DIR, UPLOAD_DIR, VIDEO_DIR, JOB_DIR, ORDER_DATA_DIR):
    folder.mkdir(parents=True, exist_ok=True)

DB_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'prontof.db'}")
SECRET_KEY = os.getenv("SECRET_KEY", "")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "")
OWNER_PASSWORD = os.getenv("OWNER_PASSWORD", "")
ALLOW_REGISTRATION = os.getenv("ALLOW_REGISTRATION", "0") == "1"
INTERNAL_ONLY = os.getenv("INTERNAL_ONLY", "0") == "1"
MAX_UPLOAD_GB = min(500, max(1, int(os.getenv("MAX_UPLOAD_GB", "500"))))
MAX_UPLOAD_BYTES = MAX_UPLOAD_GB * 1024**3
UPLOAD_ACCOUNTING_LOCK = threading.Lock()
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
CPU_THREADS = max(1, min(4, int(os.getenv("CPU_THREADS", "2"))))
TOKEN_DAYS = max(1, int(os.getenv("TOKEN_DAYS", "7")))
LANGUAGES = {
    "uk":"Українська", "ru":"Русский", "de":"Deutsch", "fr":"Français",
    "it":"Italiano", "en":"English", "ar":"العربية", "pt":"Português",
    "es":"Español", "bg":"Български", "pl":"Polski", "cs":"Čeština",
    "sk":"Slovenčina", "lt":"Lietuvių", "lv":"Latviešu", "no":"Norsk"
}
if len(SECRET_KEY) < 32:
    raise RuntimeError("SECRET_KEY не задан или короче 32 символов. Настройте .env.")
if len(OWNER_PASSWORD) < 8:
    raise RuntimeError("OWNER_PASSWORD должен содержать не менее 8 символов.")

engine = create_engine(DB_URL, connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {}, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
password_hash = PasswordHash.recommended()
app = FastAPI(title="ProntoF internal video worker", version="0.2.0", description="Внутренний сервис обработки видео для Home Master", docs_url=None if INTERNAL_ONLY else "/docs", redoc_url=None if INTERNAL_ONLY else "/redoc", openapi_url=None if INTERNAL_ONLY else "/openapi.json")

@app.middleware("http")
async def internal_only_guard(request: Request, call_next):
    # Worker is never a standalone user application. Only Home Master may call /internal/*.
    if INTERNAL_ONLY and request.url.path.startswith("/api/") and request.url.path != "/api/health":
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    return await call_next(request)

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: dt.datetime.now(dt.timezone.utc).replace(tzinfo=None))

class Video(Base):
    __tablename__ = "videos"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    stored_name: Mapped[str] = mapped_column(String(100), unique=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    visibility: Mapped[str] = mapped_column(String(20), default="private")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: dt.datetime.now(dt.timezone.utc).replace(tzinfo=None))

class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(String(500), default="")
    source_language: Mapped[str] = mapped_column(String(12), default="")
    target_language: Mapped[str] = mapped_column(String(12), default="")
    transcript: Mapped[str] = mapped_column(Text, default="")
    translated_text: Mapped[str] = mapped_column(Text, default="")
    segments_json: Mapped[str] = mapped_column(Text, default="[]")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: dt.datetime.now(dt.timezone.utc).replace(tzinfo=None))
    order_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)

class InternalUploadReceipt(Base):
    __tablename__ = "internal_upload_receipts"
    upload_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    job_id: Mapped[str] = mapped_column(String(36), unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: dt.datetime.now(dt.timezone.utc).replace(tzinfo=None))

Base.metadata.create_all(engine)
# Backward-compatible migration for the existing test database.
with engine.begin() as conn:
    job_columns = {c["name"] for c in __import__("sqlalchemy").inspect(engine).get_columns("jobs")}
    if "order_id" not in job_columns:
        conn.exec_driver_sql("ALTER TABLE jobs ADD COLUMN order_id INTEGER")

def db_session():
    with SessionLocal() as session:
        yield session

def create_token(user: User) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    return jwt.encode({"sub": str(user.id), "role": user.role, "iat": now, "exp": now + dt.timedelta(days=TOKEN_DAYS)}, SECRET_KEY, algorithm="HS256")

def current_user(authorization: Optional[str] = Header(default=None), db: Session = Depends(db_session)) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Требуется авторизация")
    try:
        payload = jwt.decode(authorization[7:], SECRET_KEY, algorithms=["HS256"])
        user_id = int(payload["sub"])
    except Exception:
        raise HTTPException(401, "Недействительный или просроченный токен")
    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(401, "Пользователь не найден или отключён")
    return user

def accessible_video(db: Session, video_id: str, user: User) -> Video:
    video = db.get(Video, video_id)
    if not video or (video.owner_id != user.id and video.visibility != "shared" and user.role != "owner"):
        raise HTTPException(404, "Видео не найдено")
    return video

def resolve_video_path(video: Video) -> Path:
    for root in (ORDER_DATA_DIR, VIDEO_DIR):
        base = root.resolve()
        candidate = (root / video.stored_name).resolve()
        if os.path.commonpath([str(base), str(candidate)]) == str(base) and candidate.is_file():
            return candidate
    raise FileNotFoundError(video.stored_name)


def clean_filename(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r"[^\w.() -]+", "_", name, flags=re.UNICODE).strip(" .")
    return (name or "video")[:240]

def save_upload_chunk(folder: Path, index: int, body: bytes, free_space_message: str):
    """Persist one chunk while keeping O(1) upload-size accounting for very large files."""
    folder.mkdir(parents=True, exist_ok=True)
    with UPLOAD_ACCOUNTING_LOCK:
        part = folder / f"{index}.part"
        temp = folder / f"{index}.tmp"
        size_file = folder / ".upload_size"
        previous_size = part.stat().st_size if part.is_file() else 0
        try:
            staged_size = int(size_file.read_text(encoding="ascii"))
            if staged_size < previous_size:
                raise ValueError("stale upload accounting")
        except (OSError, ValueError):
            staged_size = sum(p.stat().st_size for p in folder.glob("*.part") if p.is_file())
        next_size = staged_size - previous_size + len(body)
        if next_size > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Не удалось принять файл. Проверьте доступность хранилища и повторите попытку.")
        if shutil.disk_usage(folder).free < len(body) + 64 * 1024 * 1024:
            raise HTTPException(507, free_space_message)
        temp.write_bytes(body)
        temp.replace(part)
        size_tmp = folder / ".upload_size.tmp"
        try:
            size_tmp.write_text(str(next_size), encoding="ascii")
            size_tmp.replace(size_file)
        except OSError:
            size_file.unlink(missing_ok=True)
            size_tmp.unlink(missing_ok=True)
            raise HTTPException(507, "Не удалось обновить состояние загрузки на диске")
    return {"index": index, "bytes": len(body)}


def format_srt_time(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{ms:03}"

def make_srt(segments: list[dict], field: str) -> str:
    rows = []
    for i, segment in enumerate(segments, 1):
        text = str(segment.get(field, "")).strip()
        if not text:
            continue
        rows.append(f"{i}\n{format_srt_time(float(segment['start']))} --> {format_srt_time(float(segment['end']))}\n{text}")
    return "\n\n".join(rows) + ("\n" if rows else "")

def user_can_access_job(db: Session, job_id: str, user: User) -> Job:
    job = db.get(Job, job_id)
    if not job or (job.owner_id != user.id and user.role != "owner"):
        raise HTTPException(404, "Задача не найдена")
    return job

@app.on_event("startup")
def startup():
    with SessionLocal() as db:
        if db.scalar(select(User).limit(1)) is None:
            db.add(User(username="owner", full_name="Владелец", password_hash=password_hash.hash(OWNER_PASSWORD), role="owner"))
            db.commit()

def require_internal(request: Request):
    if not INTERNAL_API_KEY:
        raise HTTPException(503, "Внутренняя интеграция не настроена")
    supplied = request.headers.get("x-prontof-internal-key", "")
    if not hmac.compare_digest(supplied, INTERNAL_API_KEY):
        raise HTTPException(403, "Недостаточно прав для внутреннего API")


@app.get("/api/health")
def health():
    return {"status":"ok", "app":"ProntoF", "version":"0.1.0", "whisper_model":WHISPER_MODEL, "cpu_threads":CPU_THREADS, "languages":LANGUAGES}

class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=256)
    full_name: str = Field(default="", max_length=120)

class LoginRequest(BaseModel):
    username: str
    password: str

@app.post("/api/auth/register")
def register(data: RegisterRequest, db: Session = Depends(db_session)):
    if not ALLOW_REGISTRATION:
        raise HTTPException(403, "Регистрация временно закрыта")
    if db.scalar(select(User).where(User.username == data.username)):
        raise HTTPException(409, "Такой логин уже занят")
    user = User(username=data.username, full_name=data.full_name, password_hash=password_hash.hash(data.password), role="user")
    db.add(user)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(409, "Не удалось создать пользователя; возможно, логин уже занят")
    db.refresh(user)
    return {"access_token":create_token(user), "token_type":"bearer", "user":{"id":user.id,"username":user.username,"role":user.role}}

@app.post("/api/auth/login")
def login(data: LoginRequest, db: Session = Depends(db_session)):
    user = db.scalar(select(User).where(User.username == data.username))
    if not user or not password_hash.verify(data.password, user.password_hash):
        raise HTTPException(401, "Неверный логин или пароль")
    if not user.is_active:
        raise HTTPException(403, "Учётная запись отключена")
    return {"access_token":create_token(user), "token_type":"bearer"}

@app.get("/api/auth/me")
def me(user: User = Depends(current_user)):
    return {"id":user.id,"username":user.username,"full_name":user.full_name,"role":user.role}

@app.get("/api/videos")
def list_videos(db: Session = Depends(db_session), user: User = Depends(current_user)):
    stmt = select(Video).order_by(Video.created_at.desc())
    if user.role != "owner":
        stmt = stmt.where((Video.owner_id == user.id) | (Video.visibility == "shared"))
    rows = db.scalars(stmt).all()
    return [{"id":v.id,"filename":v.filename,"size_bytes":v.size_bytes,"visibility":v.visibility,"created_at":v.created_at.isoformat()+"Z"} for v in rows]

@app.get("/api/videos/{video_id}")
def video_detail(video_id: str, db: Session = Depends(db_session), user: User = Depends(current_user)):
    v = accessible_video(db, video_id, user)
    return {"id":v.id,"filename":v.filename,"size_bytes":v.size_bytes,"visibility":v.visibility,"created_at":v.created_at.isoformat()+"Z"}

class VisibilityRequest(BaseModel):
    visibility: str = Field(pattern=r"^(private|shared)$")

@app.patch("/api/videos/{video_id}/visibility")
def set_video_visibility(video_id: str, data: VisibilityRequest, db: Session = Depends(db_session), user: User = Depends(current_user)):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(404, "Видео не найдено")
    if video.owner_id != user.id and user.role != "owner":
        raise HTTPException(403, "Изменять доступ может владелец видео или администратор")
    video.visibility = data.visibility
    db.commit()
    return {"id":video.id,"visibility":video.visibility}

@app.get("/api/videos/{video_id}/playback-token")
def create_playback_token(video_id: str, db: Session = Depends(db_session), user: User = Depends(current_user)):
    video = accessible_video(db, video_id, user)
    now = dt.datetime.now(dt.timezone.utc)
    token = jwt.encode({"scope":"video_playback","video_id":video.id,"user_id":user.id,"iat":now,"exp":now+dt.timedelta(minutes=5)}, SECRET_KEY, algorithm="HS256")
    return {"token":token,"expires_in":300}

@app.get("/api/videos/{video_id}/source")
def video_source(video_id: str, token: str = Query(min_length=20), db: Session = Depends(db_session)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        if payload.get("scope") != "video_playback" or payload.get("video_id") != video_id:
            raise ValueError("wrong playback scope")
        user_id = int(payload["user_id"])
    except Exception:
        raise HTTPException(401, "Ссылка на просмотр недействительна или истекла")
    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(401, "Пользователь отключён")
    v = accessible_video(db, video_id, user)
    path = (VIDEO_DIR / v.stored_name).resolve()
    if os.path.commonpath([VIDEO_DIR.resolve(), path]) != str(VIDEO_DIR.resolve()) or not path.is_file():
        raise HTTPException(404, "Файл видео отсутствует")
    media_type = "video/mp4" if path.suffix.lower() == ".mp4" else "application/octet-stream"
    return FileResponse(path, filename=v.filename, media_type=media_type, content_disposition_type="inline", headers={"Cache-Control":"private, no-store"})

@app.put("/api/uploads/{upload_id}/chunks/{index}")
async def upload_chunk(upload_id: str, index: int, request: Request, user: User = Depends(current_user)):
    if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", upload_id) or index < 0 or index > 100000:
        raise HTTPException(400, "Некорректный идентификатор загрузки или номер части")
    body = await request.body()
    if not body or len(body) > 32 * 1024 * 1024:
        raise HTTPException(413, "Размер части должен быть от 1 байта до 32 MiB")
    folder = UPLOAD_DIR / f"{user.id}_{upload_id}"
    saved = save_upload_chunk(folder, index, body, "На диске недостаточно места для следующей части загрузки")
    return {"upload_id": upload_id, **saved}

class UploadComplete(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    total_chunks: int = Field(ge=1, le=100001)

@app.post("/api/uploads/{upload_id}/complete")
def complete_upload(upload_id: str, data: UploadComplete, db: Session = Depends(db_session), user: User = Depends(current_user)):
    if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", upload_id):
        raise HTTPException(400, "Некорректный идентификатор загрузки")
    folder = UPLOAD_DIR / f"{user.id}_{upload_id}"
    if not folder.is_dir():
        raise HTTPException(404, "Части загрузки не найдены")
    (folder / ".upload_size").unlink(missing_ok=True)
    filename = clean_filename(data.filename)
    video_id = str(uuid.uuid4())
    stored_name = f"orders/{order_id}/customer/original_video/{video_id}{Path(filename).suffix.lower()[:12]}"
    destination = ORDER_DATA_DIR / stored_name
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with destination.open("wb") as output:
            for index in range(data.total_chunks):
                part = folder / f"{index}.part"
                if not part.is_file():
                    raise HTTPException(400, f"Не найдена часть загрузки №{index}")
                part_size = part.stat().st_size
                total += part_size
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "Не удалось принять файл. Проверьте доступность хранилища и повторите попытку.")
                with part.open("rb") as source:
                    shutil.copyfileobj(source, output, 4 * 1024 * 1024)
        if total == 0:
            raise HTTPException(400, "Пустой файл")
        video = Video(id=video_id, owner_id=user.id, filename=filename, stored_name=stored_name, size_bytes=total)
        db.add(video)
        db.commit()
        shutil.rmtree(folder, ignore_errors=True)
        return {"id":video_id,"filename":filename,"size_bytes":total,"status":"uploaded"}
    except Exception:
        destination.unlink(missing_ok=True)
        raise

class ProcessRequest(BaseModel):
    source_language: Optional[str] = None
    target_language: str = Field(min_length=2, max_length=8)

@app.post("/api/videos/{video_id}/process")
def create_processing_job(video_id: str, data: ProcessRequest, db: Session = Depends(db_session), user: User = Depends(current_user)):
    video = accessible_video(db, video_id, user)
    if data.source_language and data.source_language not in LANGUAGES:
        raise HTTPException(400, "Неподдерживаемый исходный язык")
    if data.target_language not in LANGUAGES:
        raise HTTPException(400, "Неподдерживаемый язык перевода")
    existing = db.scalar(select(Job).where(Job.video_id == video_id, Job.status.in_([ "queued", "processing" ])))
    if existing:
        raise HTTPException(409, "Для этого видео уже выполняется обработка")
    job = Job(id=str(uuid.uuid4()), video_id=video.id, owner_id=user.id, source_language=data.source_language or "", target_language=data.target_language, status="queued", progress=0, message="Ожидает свободного CPU worker")
    db.add(job)
    db.commit()
    return {"id":job.id,"status":job.status}

class InternalUploadComplete(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    total_chunks: int = Field(ge=1, le=100001)
    source_language: Optional[str] = None
    target_language: str = Field(min_length=2, max_length=8)


@app.post("/internal/orders/{order_id}/uploads/{upload_id}")
async def internal_upload_single(order_id: int, upload_id: str, request: Request,
                                 filename: str = Query(..., min_length=1, max_length=255),
                                 target_language: str = Query(..., min_length=2, max_length=8),
                                 source_language: Optional[str] = Query(default=None),
                                 db: Session = Depends(db_session)):
    """Receive one complete video request and stream it straight to disk (constant RAM use)."""
    require_internal(request)
    if order_id < 1 or not re.fullmatch(r"[A-Za-z0-9-]{8,64}", upload_id):
        raise HTTPException(400, "Некорректный ID заказа или загрузки")
    receipt = db.get(InternalUploadReceipt, upload_id)
    if receipt:
        if receipt.order_id != order_id:
            raise HTTPException(409, "ID загрузки уже привязан к другому заказу")
        job = db.get(Job, receipt.job_id)
        if not job:
            raise HTTPException(409, "Запись загрузки найдена, но задача отсутствует")
        return {"job_id": job.id, "video_id": job.video_id, "order_id": order_id, "status": job.status,
                "filename": db.get(Video, job.video_id).filename if db.get(Video, job.video_id) else filename}
    if source_language and source_language not in LANGUAGES:
        raise HTTPException(400, "Неподдерживаемый исходный язык")
    if target_language not in LANGUAGES:
        raise HTTPException(400, "Неподдерживаемый язык перевода")
    safe_filename = clean_filename(filename)
    suffix = Path(safe_filename).suffix.lower()
    if suffix not in {".mp4", ".mov", ".webm", ".3gp", ".mkv", ".avi", ".m4v", ".mpeg", ".mpg"}:
        raise HTTPException(400, "Поддерживаются видеофайлы MP4, MOV, WEBM, 3GP, MKV, AVI, M4V, MPEG")
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "Размер видео превышает установленный лимит загрузки")
        except ValueError:
            raise HTTPException(400, "Некорректный Content-Length")
    video_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    stored_name = f"orders/{order_id}/customer/original_video/{video_id}{suffix[:12]}"
    destination = VIDEO_DIR / stored_name
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = UPLOAD_DIR / f"single_{order_id}_{upload_id}_{uuid.uuid4().hex}.tmp"
    total = 0
    committed = False
    try:
        with temp_path.open("wb") as output:
            async for chunk in request.stream():
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "Размер видео превышает установленный лимит загрузки")
                if shutil.disk_usage(ORDER_DATA_DIR).free < len(chunk) + 64 * 1024 * 1024:
                    raise HTTPException(507, "На диске недостаточно свободного места для загрузки видео")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if total <= 0:
            raise HTTPException(400, "Пустой видеофайл")
        owner = db.scalar(select(User).where(User.role == "owner").limit(1))
        if not owner:
            raise HTTPException(503, "Владелец сервиса не создан")
        os.replace(temp_path, destination)
        video = Video(id=video_id, owner_id=owner.id, filename=safe_filename, stored_name=stored_name,
                      size_bytes=total, visibility="private")
        job = Job(id=job_id, video_id=video_id, owner_id=owner.id, order_id=order_id,
                  status="queued", progress=0, message="Видео заказа принято в очередь",
                  source_language=source_language or "", target_language=target_language)
        db.add(video)
        db.add(job)
        db.add(InternalUploadReceipt(upload_id=upload_id, order_id=order_id, job_id=job.id))
        db.commit()
        committed = True
        return {"job_id": job.id, "video_id": video_id, "order_id": order_id, "status": job.status,
                "filename": safe_filename, "size_bytes": total}
    except BaseException:
        if not committed:
            db.rollback()
            temp_path.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
        raise


@app.put("/internal/orders/{order_id}/uploads/{upload_id}/chunks/{index}")
async def internal_upload_chunk(order_id: int, upload_id: str, index: int, request: Request):
    require_internal(request)
    if order_id < 1 or not re.fullmatch(r"[A-Za-z0-9-]{8,64}", upload_id) or index < 0 or index > 100000:
        raise HTTPException(400, "Некорректный ID заказа, загрузки или номер части")
    body = await request.body()
    if not body or len(body) > 32 * 1024 * 1024:
        raise HTTPException(413, "Размер части должен быть от 1 байта до 32 MiB")
    folder = UPLOAD_DIR / f"hm_{order_id}_{upload_id}"
    saved = save_upload_chunk(folder, index, body, "На диске недостаточно места для следующей части")
    return {"order_id": order_id, "upload_id": upload_id, **saved}


@app.post("/internal/orders/{order_id}/uploads/{upload_id}/complete")
def internal_complete_upload(order_id: int, upload_id: str, data: InternalUploadComplete,
                             request: Request, db: Session = Depends(db_session)):
    require_internal(request)
    if order_id < 1 or not re.fullmatch(r"[A-Za-z0-9-]{8,64}", upload_id):
        raise HTTPException(400, "Некорректный ID заказа или загрузки")
    receipt = db.get(InternalUploadReceipt, upload_id)
    if receipt:
        if receipt.order_id != order_id:
            raise HTTPException(409, "ID загрузки уже привязан к другому заказу")
        job = db.get(Job, receipt.job_id)
        if not job:
            raise HTTPException(409, "Запись загрузки найдена, но задача отсутствует")
        return {"job_id": job.id, "video_id": job.video_id, "order_id": order_id, "status": job.status}
    if data.source_language and data.source_language not in LANGUAGES:
        raise HTTPException(400, "Неподдерживаемый исходный язык")
    if data.target_language not in LANGUAGES:
        raise HTTPException(400, "Неподдерживаемый язык перевода")
    folder = UPLOAD_DIR / f"hm_{order_id}_{upload_id}"
    if not folder.is_dir():
        raise HTTPException(404, "Части видео не найдены")
    (folder / ".upload_size").unlink(missing_ok=True)
    filename = clean_filename(data.filename)
    suffix = Path(filename).suffix.lower()
    if suffix not in {".mp4", ".mov", ".webm", ".3gp", ".mkv", ".avi", ".m4v", ".mpeg", ".mpg"}:
        raise HTTPException(400, "Поддерживаются видеофайлы MP4, MOV, WEBM, 3GP, MKV, AVI, M4V, MPEG")
    video_id = str(uuid.uuid4())
    stored_name = f"orders/{order_id}/customer/original_video/{video_id}{suffix[:12]}"
    destination = VIDEO_DIR / stored_name
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with destination.open("wb") as output:
            for index in range(data.total_chunks):
                part = folder / f"{index}.part"
                if not part.is_file():
                    raise HTTPException(400, f"Не найдена часть загрузки №{index}")
                part_size = part.stat().st_size
                if total + part_size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "Не удалось принять файл. Проверьте доступность хранилища и повторите попытку.")
                with part.open("rb") as source:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                total += part_size
                # Release each staged chunk immediately: peak extra space stays near one chunk,
                # rather than requiring a second full copy of a very large video.
                part.unlink()
        if total <= 0:
            raise HTTPException(400, "Пустой видеофайл")
        owner = db.scalar(select(User).where(User.role == "owner").limit(1))
        if not owner:
            raise HTTPException(503, "Владелец сервиса не создан")
        video = Video(id=video_id, owner_id=owner.id, filename=filename, stored_name=stored_name,
                      size_bytes=total, visibility="private")
        job = Job(id=str(uuid.uuid4()), video_id=video_id, owner_id=owner.id, order_id=order_id,
                  status="queued", progress=0, message="Видео заказа принято в очередь",
                  source_language=data.source_language or "", target_language=data.target_language)
        db.add(video)
        db.add(job)
        db.add(InternalUploadReceipt(upload_id=upload_id, order_id=order_id, job_id=job.id))
        db.commit()
        shutil.rmtree(folder, ignore_errors=True)
        return {"job_id": job.id, "video_id": video_id, "order_id": order_id, "status": job.status,
                "filename": filename, "size_bytes": total}
    except Exception:
        db.rollback()
        destination.unlink(missing_ok=True)
        raise


class InternalVideoRename(BaseModel):
    filename: str = Field(min_length=1, max_length=255)

@app.get("/internal/library")
def internal_library(request: Request, db: Session = Depends(db_session)):
    require_internal(request)
    videos = db.scalars(select(Video).order_by(Video.created_at.desc())).all()
    jobs = db.scalars(select(Job).order_by(Job.created_at.desc())).all()
    latest = {}
    for job in jobs:
        latest.setdefault(job.video_id, job)
    return [{"id": v.id, "filename": v.filename, "size_bytes": v.size_bytes,
             "created_at": v.created_at.isoformat() + "Z",
             "order_id": latest[v.id].order_id if v.id in latest else None,
             "status": latest[v.id].status if v.id in latest else "uploaded"} for v in videos]

@app.get("/internal/videos/{video_id}/source")
def internal_video_source(video_id: str, request: Request, db: Session = Depends(db_session)):
    require_internal(request)
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(404, "Видео не найдено")
    try:
        path = resolve_video_path(video)
    except FileNotFoundError:
        raise HTTPException(404, "Файл видео отсутствует")
    import mimetypes
    mime = mimetypes.guess_type(video.filename)[0] or "application/octet-stream"
    return FileResponse(path, filename=video.filename, media_type=mime, content_disposition_type="attachment")

@app.patch("/internal/videos/{video_id}")
def internal_video_rename(video_id: str, data: InternalVideoRename, request: Request,
                          db: Session = Depends(db_session)):
    require_internal(request)
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(404, "Видео не найдено")
    filename = clean_filename(data.filename)
    if not Path(filename).suffix or Path(filename).suffix.lower() != Path(video.filename).suffix.lower():
        raise HTTPException(400, "Нельзя менять расширение исходного видео")
    video.filename = filename
    db.commit()
    return {"ok": True, "id": video.id, "filename": video.filename}

@app.delete("/internal/videos/{video_id}")
def internal_video_delete(video_id: str, request: Request, db: Session = Depends(db_session)):
    require_internal(request)
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(404, "Видео не найдено")
    jobs = db.scalars(select(Job).where(Job.video_id == video_id)).all()
    if any(job.status == "processing" for job in jobs):
        raise HTTPException(409, "Нельзя удалить видео во время обработки")
    job_ids = [job.id for job in jobs]
    for job in jobs:
        db.delete(job)
        shutil.rmtree(JOB_DIR / job.id, ignore_errors=True)
    if job_ids:
        for receipt in db.scalars(select(InternalUploadReceipt).where(InternalUploadReceipt.job_id.in_(job_ids))).all():
            db.delete(receipt)
    try:
        resolve_video_path(video).unlink(missing_ok=True)
    except FileNotFoundError:
        pass
    db.delete(video)
    db.commit()
    return {"ok": True, "job_ids": job_ids}

class InternalImportVideoIn(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    stored_name: str = Field(min_length=1, max_length=255)
    target_language: str = Field(min_length=2, max_length=8)
    source_language: Optional[str] = None

@app.post("/internal/orders/{order_id}/videos/import")
def internal_import_order_video(order_id:int,data:InternalImportVideoIn,request:Request,
                                db:Session=Depends(db_session)):
    require_internal(request)
    if order_id<1 or data.target_language not in LANGUAGES:
        raise HTTPException(400,"Некорректный заказ или язык перевода")
    if data.source_language and data.source_language not in LANGUAGES:
        raise HTTPException(400,"Некорректный исходный язык")
    relative=Path(data.stored_name)
    if relative.is_absolute() or ".." in relative.parts or relative.parts[:2] != ("orders",str(order_id)):
        raise HTTPException(400,"Исходный файл должен находиться в папке этого заказа")
    path=(ORDER_DATA_DIR/relative).resolve()
    if os.path.commonpath([str(ORDER_DATA_DIR.resolve()),str(path)])!=str(ORDER_DATA_DIR.resolve()) or not path.is_file():
        raise HTTPException(404,"Общий файл заказа не найден")
    size=path.stat().st_size
    if size<=0 or size>MAX_UPLOAD_BYTES:
        raise HTTPException(413,"Размер исходного видео недопустим")
    suffix=Path(clean_filename(data.filename)).suffix.lower()
    if suffix not in {".mp4",".mov",".webm",".3gp",".mkv",".avi",".m4v",".mpeg",".mpg"}:
        raise HTTPException(400,"Неподдерживаемый формат видео")
    owner=db.scalar(select(User).where(User.role=="owner").limit(1))
    if not owner:raise HTTPException(503,"Владелец worker не создан")
    video_id=str(uuid.uuid4());job_id=str(uuid.uuid4())
    video=Video(id=video_id,owner_id=owner.id,filename=clean_filename(data.filename),
                stored_name=relative.as_posix(),size_bytes=size,visibility="private")
    job=Job(id=job_id,video_id=video_id,owner_id=owner.id,order_id=order_id,status="queued",progress=0,
            message="Видео заказа поставлено в очередь без копирования оригинала",
            source_language=data.source_language or "",target_language=data.target_language)
    db.add(video);db.add(job);db.commit()
    return {"job_id":job.id,"video_id":video_id,"order_id":order_id,"filename":video.filename,
            "size_bytes":size,"status":job.status}


class InternalRetranslateIn(BaseModel):
    target_language: str = Field(min_length=2, max_length=8)
    source_language: Optional[str] = None

@app.post("/internal/jobs/{job_id}/retranslate")
def internal_retranslate(job_id: str, data: InternalRetranslateIn, request: Request,
                         db: Session = Depends(db_session)):
    require_internal(request)
    original = db.get(Job, job_id)
    if not original or original.order_id is None:
        raise HTTPException(404, "Исходная задача заказа не найдена")
    video = db.get(Video, original.video_id)
    if not video:
        raise HTTPException(404, "Оригинальный видеофайл не найден")
    if data.target_language not in LANGUAGES:
        raise HTTPException(400, "Неподдерживаемый язык перевода")
    if data.source_language and data.source_language not in LANGUAGES:
        raise HTTPException(400, "Неподдерживаемый исходный язык")
    job = Job(id=str(uuid.uuid4()), video_id=video.id, owner_id=original.owner_id,
              order_id=original.order_id, status="queued", progress=0,
              message="Повторный перевод оригинала по запросу исполнителя",
              source_language=data.source_language or "", target_language=data.target_language)
    db.add(job)
    db.commit()
    return {"job_id":job.id,"video_id":video.id,"order_id":original.order_id,
            "filename":video.filename,"status":job.status}


@app.get("/internal/jobs/{job_id}")
def internal_job_detail(job_id: str, request: Request, db: Session = Depends(db_session)):
    require_internal(request)
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена")
    video = db.get(Video, job.video_id)
    return {"id": job.id, "order_id": job.order_id, "video_id": job.video_id,
            "filename": video.filename if video else "", "stored_name": video.stored_name if video else "", "status": job.status,
            "progress": job.progress, "message": job.message, "error": job.error,
            "source_language": job.source_language, "target_language": job.target_language,
            "transcript": job.transcript, "translated_text": job.translated_text}


@app.get("/api/jobs")
def list_jobs(db: Session = Depends(db_session), user: User = Depends(current_user)):
    stmt = select(Job).order_by(Job.created_at.desc()).limit(100)
    if user.role != "owner":
        stmt = stmt.where(Job.owner_id == user.id)
    return [{"id":j.id,"video_id":j.video_id,"status":j.status,"progress":j.progress,"message":j.message,"created_at":j.created_at.isoformat()+"Z"} for j in db.scalars(stmt).all()]

@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str, db: Session = Depends(db_session), user: User = Depends(current_user)):
    j = user_can_access_job(db, job_id, user)
    return {"id":j.id,"video_id":j.video_id,"status":j.status,"progress":j.progress,"message":j.message,"error":j.error,"source_language":j.source_language,"target_language":j.target_language,"transcript":j.transcript,"translated_text":j.translated_text}

class EditRequest(BaseModel):
    transcript: str = Field(max_length=20_000_000)
    translated_text: str = Field(max_length=20_000_000)

@app.put("/api/jobs/{job_id}/edit")
def edit_job(job_id: str, data: EditRequest, db: Session = Depends(db_session), user: User = Depends(current_user)):
    job = user_can_access_job(db, job_id, user)
    if job.status != "done":
        raise HTTPException(409, "Редактировать можно после завершения обработки")
    job.transcript = data.transcript
    job.translated_text = data.translated_text
    # Preserve timing while applying user-edited lines to subtitle segments.
    segments = json.loads(job.segments_json or "[]")
    original_lines = [line.strip() for line in data.transcript.splitlines() if line.strip()]
    translated_lines = [line.strip() for line in data.translated_text.splitlines() if line.strip()]
    for index, segment in enumerate(segments):
        if index < len(original_lines):
            segment["text"] = original_lines[index]
        if index < len(translated_lines):
            segment["translated"] = translated_lines[index]
    job.segments_json = json.dumps(segments, ensure_ascii=False)
    db.commit()
    return {"status":"saved","subtitle_segments":len(segments)}

@app.get("/api/jobs/{job_id}/subtitles/{kind}")
def download_subtitles(job_id: str, kind: str, db: Session = Depends(db_session), user: User = Depends(current_user)):
    job = user_can_access_job(db, job_id, user)
    if kind not in ("original", "translated"):
        raise HTTPException(400, "Тип субтитров: original или translated")
    segments = json.loads(job.segments_json or "[]")
    if not segments:
        raise HTTPException(404, "Субтитры пока не созданы")
    field = "text" if kind == "original" else "translated"
    content = make_srt(segments, field)
    path = JOB_DIR / f"{job.id}_{kind}.srt"
    path.write_text(content, encoding="utf-8")
    return FileResponse(path, filename=f"{kind}.srt", media_type="application/x-subrip; charset=utf-8")

def update_job(job_id: str, **values):
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job:
            for key, value in values.items():
                setattr(job, key, value)
            db.commit()

def translate_segment(text: str, source: str, target: str) -> str:
    if source == target or not text.strip():
        return text
    try:
        import argostranslate.translate as argos
        # Argos can compose a route through an intermediate language (usually English).
        # This allows a compact set of bidirectional English models to cover many pairs.
        translation = argos.get_translation_from_codes(source, target)
        return translation.translate(text) if translation is not None else ""
    except Exception:
        return ""

def process_job(job_id: str):
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if not job:
            return
        video = db.get(Video, job.video_id)
        if not video:
            update_job(job_id, status="error", error="Видео не найдено", message="Ошибка")
            return
        try:
            source_path = resolve_video_path(video)
        except FileNotFoundError:
            update_job(job_id, status="error", error="Файл оригинала отсутствует", message="Ошибка")
            return
        target_language = job.target_language
        source_language = job.source_language or None
        update_job(job_id, status="processing", progress=2, message="Загрузка модели распознавания")
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=CPU_THREADS, num_workers=1)
        update_job(job_id, progress=10, message="Распознавание речи на CPU")
        segments_iter, info = model.transcribe(str(source_path), language=source_language, beam_size=5, vad_filter=True)
        rows = []
        for seg in segments_iter:
            txt = seg.text.strip()
            if txt:
                rows.append({"start":float(seg.start),"end":float(seg.end),"text":txt,"translated":""})
                if len(rows) % 25 == 0:
                    update_job(job_id, progress=min(65, 10 + len(rows)//25), message=f"Распознано сегментов: {len(rows)}")
        if not rows:
            raise RuntimeError("Речь не распознана. Проверьте звуковую дорожку.")
        detected = source_language or info.language or "unknown"
        update_job(job_id, progress=68, message="Перевод текста")
        translation_available = target_language == detected
        if not translation_available:
            try:
                import argostranslate.translate as argos
                route = argos.get_translation_from_codes(detected, target_language)
                translation_available = route is not None
            except Exception:
                translation_available = False
        for index, row in enumerate(rows):
            row["translated"] = translate_segment(row["text"], detected, target_language) if translation_available else ""
            if index % 20 == 0:
                update_job(job_id, progress=min(92, 68 + int(index / max(1,len(rows)) * 24)), message=f"Перевод сегмента {index+1}/{len(rows)}")
        transcript = "\n".join(x["text"] for x in rows)
        translated = "\n".join(x["translated"] for x in rows if x["translated"])
        final_message = "Обработка завершена" if translation_available else f"Распознавание завершено. Не найдена языковая пара Argos {detected} → {target_language}; перевод не выполнен."
        update_job(job_id, status="done", progress=100, message=final_message, source_language=detected, transcript=transcript, translated_text=translated, segments_json=json.dumps(rows, ensure_ascii=False), error="")
    except Exception as exc:
        update_job(job_id, status="error", progress=0, message="Ошибка обработки", error=str(exc))

def worker_loop():
    while True:
        job_id = None
        try:
            with SessionLocal() as db:
                job = db.scalar(select(Job).where(Job.status == "queued").order_by(Job.created_at.asc()).limit(1))
                if job:
                    job_id = job.id
                    job.status = "processing"
                    job.progress = 1
                    job.message = "Запускается worker"
                    db.commit()
            if job_id:
                process_job(job_id)
            else:
                time.sleep(2)
        except Exception:
            time.sleep(5)

@app.on_event("startup")
def start_worker():
    threading.Thread(target=worker_loop, daemon=True, name="prontof-worker").start()

# Пользовательский интерфейс приложения обслуживается Home Master; этот контейнер — только внутренний worker/API.
