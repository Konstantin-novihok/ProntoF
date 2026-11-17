"""Внутренний мост Home Master -> ProntoF. Пользователи работают только в Home Master."""
import os
import json
import re
import uuid
import asyncio
import datetime as dt
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import ForeignKey, Integer, String, DateTime, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from server import (ADMIN, CLIENT, EXECUTOR, HEAD, OWNER, STAFF, Base, Media, MEDIA_DIR, ORDER_DATA_DIR, User, current_user, media_path,
                    get_db, get_order, log, need, now)

router = APIRouter()
PRONTOF_API_URL = os.getenv("PRONTOF_API_URL", "").rstrip("/")
PRONTOF_INTERNAL_KEY = os.getenv("PRONTOF_INTERNAL_KEY", "")
CHUNK_LIMIT = 32 * 1024 * 1024


class OrderVideoJob(Base):
    __tablename__ = "order_video_jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    upload_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    remote_job_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    filename: Mapped[str] = mapped_column(String(255))
    target_language: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class VideoUploadComplete(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    total_chunks: int = Field(ge=1, le=100001)
    source_language: Optional[str] = None
    target_language: str = Field(min_length=2, max_length=8)


def _ensure_config():
    if not PRONTOF_API_URL or not PRONTOF_INTERNAL_KEY:
        raise HTTPException(503, "Видеообработка не подключена: проверьте PRONTOF_API_URL и PRONTOF_INTERNAL_KEY на сервере Home Master")


def _headers():
    return {"X-ProntoF-Internal-Key": PRONTOF_INTERNAL_KEY}


async def _remote(method: str, path: str, **kwargs):
    _ensure_config()
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            response = await client.request(method, PRONTOF_API_URL + path, headers=_headers(), **kwargs)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Сервис обработки видео недоступен: {exc.__class__.__name__}")
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text[:500])
        except Exception:
            detail = response.text[:500]
        raise HTTPException(response.status_code, detail)
    return response



async def _remote_stream(path: str, request: Request, params: dict):
    """Proxy the incoming request body to Worker without buffering the video in RAM."""
    _ensure_config()

    async def body_stream():
        async for chunk in request.stream():
            if chunk:
                yield chunk

    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST", PRONTOF_API_URL + path, headers={**_headers(), "Content-Type": "application/octet-stream"},
                params=params, content=body_stream()
            ) as response:
                await response.aread()
                if response.status_code >= 400:
                    try:
                        detail = response.json().get("detail", response.text[:500])
                    except Exception:
                        detail = response.text[:500]
                    raise HTTPException(response.status_code, detail)
                return response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Сервис обработки видео недоступен: {exc.__class__.__name__}")


async def _remote_file_stream(path: str, filepath: str, params: dict):
    """Потоковая передача уже сохранённого оригинала Worker без чтения всего файла в RAM."""
    _ensure_config()

    async def body_stream():
        with open(filepath, "rb") as source:
            while True:
                chunk = await asyncio.to_thread(source.read, 1024 * 1024)
                if not chunk:
                    break
                yield chunk

    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST", PRONTOF_API_URL + path,
                headers={**_headers(), "Content-Type": "application/octet-stream"},
                params=params, content=body_stream()
            ) as response:
                await response.aread()
                if response.status_code >= 400:
                    try:
                        detail = response.json().get("detail", response.text[:500])
                    except Exception:
                        detail = response.text[:500]
                    raise HTTPException(response.status_code, detail)
                return response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Сервис обработки видео недоступен: {exc.__class__.__name__}")


def _require_upload_participant(oid:int,user:User,db:Session):
    if user.role==EXECUTOR:
        from collaboration import participant
        if not participant(db,oid,user.id):
            raise HTTPException(403,"Загружать новое видео могут только исполнители заказа")


def _check_order(oid: int, user: User, db: Session):
    need(user, OWNER, ADMIN, HEAD, EXECUTOR, CLIENT, STAFF)
    return get_order(db, user, oid)


@router.post("/orders/{oid}/video-processing/uploads/{upload_id}")
async def video_upload_single(oid: int, upload_id: str, request: Request,
                              filename: str = Query(..., min_length=1, max_length=255),
                              target_language: str = Query(..., min_length=2, max_length=8),
                              source_language: Optional[str] = Query(default=None),
                              user: User = Depends(current_user), db: Session = Depends(get_db)):
    _check_order(oid, user, db)
    _require_upload_participant(oid,user,db)
    if not re.fullmatch(r"hm-[A-Za-z0-9-]{8,60}", upload_id):
        raise HTTPException(400, "Некорректный ID загрузки")
    existing = db.scalar(select(OrderVideoJob).where(OrderVideoJob.upload_id == upload_id))
    if existing:
        if existing.order_id != oid or existing.user_id != user.id:
            raise HTTPException(409, "ID загрузки уже использован")
        return {"job_id": existing.remote_job_id, "order_id": oid, "status": "queued", "filename": existing.filename}
    params = {"filename": filename, "target_language": target_language}
    if source_language:
        params["source_language"] = source_language
    result = await _remote_stream(f"/internal/orders/{oid}/uploads/{upload_id}", request, params)
    row = OrderVideoJob(order_id=oid, user_id=user.id, upload_id=upload_id,
                        remote_job_id=result["job_id"], filename=filename[:255],
                        target_language=target_language)
    db.add(row)
    log(db, user, "video_processing_start", "order", oid, filename[:200])
    try:
        db.commit()
    except Exception:
        db.rollback()
        existing = db.scalar(select(OrderVideoJob).where(OrderVideoJob.upload_id == upload_id))
        if existing and existing.order_id == oid and existing.user_id == user.id:
            return {"job_id": existing.remote_job_id, "order_id": oid, "status": "queued", "filename": existing.filename}
        raise HTTPException(409, "Не удалось сохранить связь видео с заказом; повторите отправку с тем же ID")
    return {"job_id": result["job_id"], "order_id": oid, "status": result.get("status", "queued"),
            "filename": filename, "size_bytes": result.get("size_bytes")}


@router.put("/orders/{oid}/video-processing/uploads/{upload_id}/chunks/{index}")
async def video_upload_chunk(oid: int, upload_id: str, index: int, request: Request,
                             user: User = Depends(current_user), db: Session = Depends(get_db)):
    _check_order(oid, user, db)
    _require_upload_participant(oid,user,db)
    if not re.fullmatch(r"hm-[A-Za-z0-9-]{8,60}", upload_id) or index < 0 or index > 100000:
        raise HTTPException(400, "Некорректный ID загрузки или номер части")
    body = await request.body()
    if not body or len(body) > CHUNK_LIMIT:
        raise HTTPException(413, "Размер части должен быть от 1 байта до 32 MiB")
    response = await _remote("PUT", f"/internal/orders/{oid}/uploads/{upload_id}/chunks/{index}", content=body)
    return response.json()


@router.post("/orders/{oid}/video-processing/uploads/{upload_id}/complete")
async def video_upload_complete(oid: int, upload_id: str, data: VideoUploadComplete,
                                user: User = Depends(current_user), db: Session = Depends(get_db)):
    _check_order(oid, user, db)
    _require_upload_participant(oid,user,db)
    if not re.fullmatch(r"hm-[A-Za-z0-9-]{8,60}", upload_id):
        raise HTTPException(400, "Некорректный ID загрузки")
    existing = db.scalar(select(OrderVideoJob).where(OrderVideoJob.upload_id == upload_id))
    if existing:
        if existing.order_id != oid or existing.user_id != user.id:
            raise HTTPException(409, "ID загрузки уже использован")
        return {"job_id": existing.remote_job_id, "order_id": oid, "status": "queued", "filename": existing.filename}
    response = await _remote("POST", f"/internal/orders/{oid}/uploads/{upload_id}/complete",
                             json=data.model_dump())
    result = response.json()
    row = OrderVideoJob(order_id=oid, user_id=user.id, upload_id=upload_id,
                        remote_job_id=result["job_id"], filename=data.filename[:255],
                        target_language=data.target_language)
    db.add(row)
    log(db, user, "video_processing_start", "order", oid, data.filename[:200])
    try:
        db.commit()
    except Exception:
        db.rollback()
        existing = db.scalar(select(OrderVideoJob).where(OrderVideoJob.upload_id == upload_id))
        if existing and existing.order_id == oid and existing.user_id == user.id:
            return {"job_id": existing.remote_job_id, "order_id": oid, "status": "queued", "filename": existing.filename}
        raise HTTPException(409, "Не удалось сохранить связь видео с заказом; повторите отправку с тем же ID")
    return {"job_id": result["job_id"], "order_id": oid, "status": result.get("status", "queued"),
            "filename": data.filename, "size_bytes": result.get("size_bytes")}


@router.post("/orders/{oid}/video-processing/from-media/{mid}")
async def video_translate_existing_media(oid:int,mid:int,target_language:Optional[str]=Query(default=None,min_length=2,max_length=8),
                                         source_language:Optional[str]=Query(default=None),
                                         user:User=Depends(current_user),db:Session=Depends(get_db)):
    _check_order(oid,user,db)
    media=db.get(Media,mid)
    if not media or media.order_id!=oid or not (media.mime or "").startswith("video/"):
        raise HTTPException(404,"Исходное видео не найдено в этом заказе")
    filepath=media_path(media)
    if not os.path.isfile(filepath):
        raise HTTPException(404,"Файл оригинала отсутствует на сервере")
    upload_id="hm-"+uuid.uuid4().hex
    if media.filename.startswith(f"orders/{oid}/"):
        payload={"filename":media.original,"stored_name":media.filename,"target_language":target_language}
        if source_language:payload["source_language"]=source_language
        response=await _remote("POST",f"/internal/orders/{oid}/videos/import",json=payload)
        result=response.json()
    else:
        if not target_language:
            raise HTTPException(422,"Для распознавания старого файла без перевода требуется сначала перенести его в общее хранилище заказа")
        params={"filename":media.original,"target_language":target_language}
        if source_language:params["source_language"]=source_language
        result=await _remote_file_stream(f"/internal/orders/{oid}/uploads/{upload_id}",filepath,params)
    row=OrderVideoJob(order_id=oid,user_id=user.id,upload_id=upload_id,remote_job_id=result["job_id"],
                      filename=media.original[:255],target_language=target_language)
    db.add(row)
    log(db,user,"video_processing_start","order",oid,f"source_media={mid}; target={target_language}")
    db.commit()
    return {"job_id":result["job_id"],"order_id":oid,"status":result.get("status","queued"),
            "filename":media.original,"size_bytes":result.get("size_bytes")}


@router.post("/orders/{oid}/video-processing/from-job/{job_id}")
async def video_retranslate_existing_job(oid:int,job_id:str,target_language:str=Query(...,min_length=2,max_length=8),
                                         source_language:Optional[str]=Query(default=None),
                                         user:User=Depends(current_user),db:Session=Depends(get_db)):
    _check_order(oid,user,db)
    original=db.scalar(select(OrderVideoJob).where(OrderVideoJob.remote_job_id==job_id,OrderVideoJob.order_id==oid))
    if not original:
        raise HTTPException(404,"Исходное видео этой заявки не найдено")
    payload={"target_language":target_language}
    if source_language:payload["source_language"]=source_language
    response=await _remote("POST",f"/internal/jobs/{job_id}/retranslate",json=payload)
    result=response.json()
    upload_id="hm-"+uuid.uuid4().hex
    row=OrderVideoJob(order_id=oid,user_id=user.id,upload_id=upload_id,remote_job_id=result["job_id"],
                      filename=original.filename[:255],target_language=target_language)
    db.add(row)
    log(db,user,"video_retranslation_start","order",oid,f"source_job={job_id}; target={target_language}")
    db.commit()
    return {"job_id":result["job_id"],"order_id":oid,"status":result.get("status","queued"),
            "filename":original.filename}


@router.get("/orders/{oid}/video-processing/{job_id}/source")
async def video_original_source(oid:int,job_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    _check_order(oid,user,db)
    row=db.scalar(select(OrderVideoJob).where(OrderVideoJob.order_id==oid,OrderVideoJob.remote_job_id==job_id))
    if not row:raise HTTPException(404,"Исходное видео не найдено")
    response=await _remote("GET",f"/internal/jobs/{job_id}")
    item=response.json()
    if item.get("order_id")!=oid:raise HTTPException(404,"Видео не относится к этому заказу")
    stored_name=item.get("stored_name") or ""
    if stored_name.startswith(f"orders/{oid}/"):
        path=os.path.abspath(os.path.join(ORDER_DATA_DIR,stored_name))
        root=os.path.abspath(os.path.join(ORDER_DATA_DIR,"orders",str(oid)))
        if os.path.commonpath([root,path])==root and os.path.isfile(path):
            import mimetypes
            mime=mimetypes.guess_type(row.filename)[0] or "application/octet-stream"
            safe_name=re.sub(r'[\r\n"]+', "_", os.path.basename(row.filename))
            return FileResponse(path,media_type=mime,headers={"Content-Disposition":f'inline; filename="{safe_name}"'})
    video_id=item.get("video_id")
    if not video_id:raise HTTPException(404,"Оригинал видео недоступен")
    async def stream():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET",PRONTOF_API_URL+f"/internal/videos/{video_id}/source",headers=_headers()) as remote:
                if remote.status_code>=400:
                    raise HTTPException(remote.status_code,"Не удалось получить оригинал видео")
                async for chunk in remote.aiter_bytes():
                    yield chunk
    import mimetypes
    mime=mimetypes.guess_type(row.filename)[0] or "application/octet-stream"
    safe_name=re.sub(r'[\r\n"]+', "_", os.path.basename(row.filename))
    return StreamingResponse(stream(),media_type=mime,headers={"Content-Disposition":f'inline; filename="{safe_name}"'})



def _video_job_row(oid: int, job_id: str, user: User, db: Session):
    _check_order(oid, user, db)
    row = db.scalar(select(OrderVideoJob).where(
        OrderVideoJob.order_id == oid, OrderVideoJob.remote_job_id == job_id))
    if not row:
        raise HTTPException(404, "Видео этой заявки не найдено")
    return row


@router.post("/orders/{oid}/video-processing/{job_id}/render", status_code=202)
async def render_translated_video(oid: int, job_id: str,
                                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    _video_job_row(oid, job_id, user, db)
    response = await _remote("POST", f"/internal/jobs/{job_id}/render")
    log(db, user, "video_render_start", "order", oid, f"job={job_id}")
    db.commit()
    return response.json()


@router.get("/orders/{oid}/video-processing/{job_id}/render")
async def translated_video_status(oid: int, job_id: str,
                                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    _video_job_row(oid, job_id, user, db)
    return (await _remote("GET", f"/internal/jobs/{job_id}/render")).json()


async def _proxy_render_file(oid: int, job_id: str, user: User, db: Session,
                             request: Request, mode: str):
    _video_job_row(oid, job_id, user, db)
    _ensure_config()
    remote_path = f"/internal/jobs/{job_id}/render/{mode}"
    client = httpx.AsyncClient(timeout=None)
    headers = _headers()
    if mode == "preview" and request.headers.get("range"):
        headers["Range"] = request.headers["range"]
    try:
        remote = await client.send(client.build_request("GET", PRONTOF_API_URL + remote_path, headers=headers), stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(502, f"Не удалось получить готовое видео: {exc.__class__.__name__}")
    if remote.status_code >= 400:
        body = (await remote.aread()).decode("utf-8", "replace")
        await remote.aclose()
        await client.aclose()
        try:
            detail = json.loads(body).get("detail", body[:500])
        except Exception:
            detail = body[:500]
        raise HTTPException(remote.status_code, detail)
    forward_headers = {"Cache-Control": "private, no-store", "Accept-Ranges": "bytes"}
    for key in ("content-length", "content-range", "content-disposition", "etag", "last-modified"):
        if remote.headers.get(key):
            forward_headers[key.title()] = remote.headers[key]
    async def stream():
        completed = False
        try:
            async for chunk in remote.aiter_bytes():
                if chunk:
                    yield chunk
            completed = True
        finally:
            await remote.aclose()
            await client.aclose()
            # Acknowledge only after the entire response has been sent to the client.
            # A disconnected/partial transfer leaves the temporary MP4 available for retry.
            if mode == "download" and completed and request.query_params.get("client") != "android":
                try:
                    await _remote("POST", f"/internal/jobs/{job_id}/render/ack")
                except Exception:
                    pass
    media_type = remote.headers.get("content-type", "video/mp4").split(";")[0]
    return StreamingResponse(stream(), status_code=remote.status_code, media_type=media_type,
                             headers=forward_headers)



@router.post("/orders/{oid}/video-processing/{job_id}/render/ack")
async def translated_video_ack(oid: int, job_id: str,
                               user: User = Depends(current_user), db: Session = Depends(get_db)):
    _video_job_row(oid, job_id, user, db)
    result = await _remote("POST", f"/internal/jobs/{job_id}/render/ack")
    log(db, user, "video_render_downloaded", "order", oid, f"job={job_id}")
    db.commit()
    return result.json()


@router.get("/orders/{oid}/video-processing/{job_id}/render/preview")
async def translated_video_preview(oid: int, job_id: str, request: Request,
                                   user: User = Depends(current_user), db: Session = Depends(get_db)):
    return await _proxy_render_file(oid, job_id, user, db, request, "preview")


@router.get("/orders/{oid}/video-processing/{job_id}/render/download")
async def translated_video_download(oid: int, job_id: str, request: Request,
                                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    return await _proxy_render_file(oid, job_id, user, db, request, "download")


@router.get("/orders/{oid}/video-processing")
async def order_video_processing(oid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _check_order(oid, user, db)
    rows = db.scalars(select(OrderVideoJob).where(OrderVideoJob.order_id == oid).order_by(OrderVideoJob.id.desc())).all()
    out = []
    for row in rows:
        try:
            response = await _remote("GET", f"/internal/jobs/{row.remote_job_id}")
            item = response.json()
            if item.get("order_id") != oid:
                raise HTTPException(409, "Задача не связана с этим заказом")
            out.append({"job_id": row.remote_job_id, "filename": row.filename,
                        "target_language": row.target_language, **item})
        except HTTPException as exc:
            out.append({"job_id": row.remote_job_id, "filename": row.filename,
                        "target_language": row.target_language, "status": "unavailable",
                        "message": "Не удалось получить состояние сервиса обработки", "error": str(exc.detail)})
    return out
