"""Piper TTS + FFmpeg rendering for translated order videos.

Generated MP4 files are temporary worker artifacts. Originals remain untouched.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import wave
from pathlib import Path

import numpy as np

_RENDER_LOCK = threading.Lock()

VOICE_BY_LANGUAGE = {
    "ar": "ar_JO-kareem-medium",
    "bg": "bg_BG-dimitar-medium",
    "cs": "cs_CZ-jirka-medium",
    "de": "de_DE-thorsten-medium",
    "en": "en_US-lessac-medium",
    "es": "es_ES-davefx-medium",
    "fr": "fr_FR-siwis-medium",
    "it": "it_IT-paola-medium",
    "no": "no_NO-talesyntese-medium",
    "pl": "pl_PL-darkman-medium",
    "pt": "pt_BR-faber-medium",
    "ru": "ru_RU-irina-medium",
    "sk": "sk_SK-lili-medium",
    "uk": "uk_UA-ukrainian_tts-medium",
}

def voice_for_language(language: str) -> str | None:
    return VOICE_BY_LANGUAGE.get((language or "").lower())


def _run(command: list[str], timeout: int = 72 * 60 * 60) -> str:
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=timeout, check=False)
    if result.returncode:
        message = (result.stderr or result.stdout or "External media tool failed")[-3000:]
        raise RuntimeError(message)
    return result.stdout.strip()


def _probe(path: Path) -> tuple[float, bool, str]:
    raw = _run(["ffprobe", "-v", "error", "-show_entries",
                "format=duration:stream=codec_type,codec_name",
                "-of", "json", str(path)], timeout=300)
    data = json.loads(raw)
    duration = float(data.get("format", {}).get("duration") or 0)
    if duration <= 0 or duration > 12 * 60 * 60:
        raise RuntimeError("Длительность исходного видео не определена или превышает 12 часов")
    streams = data.get("streams", [])
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    video_codec = next((s.get("codec_name", "") for s in streams if s.get("codec_type") == "video"), "")
    if not any(s.get("codec_type") == "video" for s in streams):
        raise RuntimeError("В исходном файле не найдена видеодорожка")
    return duration, has_audio, video_codec


def _download_voice(voice_id: str, voice_dir: Path) -> Path:
    voice_dir.mkdir(parents=True, exist_ok=True)
    model = voice_dir / f"{voice_id}.onnx"
    config = voice_dir / f"{voice_id}.onnx.json"
    if not model.is_file() or model.stat().st_size < 1024 or not config.is_file():
        _run([sys.executable, "-m", "piper.download_voices", voice_id,
              "--download-dir", str(voice_dir)], timeout=1800)
    if not model.is_file() or not config.is_file():
        raise RuntimeError(f"Piper не загрузил модель голоса {voice_id}")
    return model


def _write_voice_track(segments: list[dict], track_path: Path, duration: float, voice):
    sample_rate = int(voice.config.sample_rate)
    total_frames = int(duration * sample_rate)
    if total_frames <= 0 or total_frames * 2 >= 4_000_000_000:
        raise RuntimeError("Слишком длинная аудиодорожка для временного WAV")
    # Create a disk-backed silent mono PCM track; RAM use stays bounded.
    with wave.open(str(track_path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        silence = b"\x00" * (1024 * 1024)
        remaining = total_frames * 2
        while remaining:
            block = silence[:min(len(silence), remaining)]
            out.writeframesraw(block)
            remaining -= len(block)

    with open(track_path, "r+b") as raw_track:
        for index, segment in enumerate(segments):
            text = str(segment.get("translated", "")).strip()
            if not text:
                continue
            start = max(0.0, float(segment.get("start", 0) or 0))
            end = min(duration, float(segment.get("end", start) or start))
            if end <= start:
                continue
            from piper import SynthesisConfig
            def synthesize(length_scale=None):
                buffer = io.BytesIO()
                with wave.open(buffer, "wb") as wav_out:
                    if length_scale is None:
                        voice.synthesize_wav(text[:5000], wav_out)
                    else:
                        voice.synthesize_wav(text[:5000], wav_out,
                                             syn_config=SynthesisConfig(length_scale=length_scale))
                buffer.seek(0)
                with wave.open(buffer, "rb") as wav_in:
                    channels = wav_in.getnchannels()
                    width = wav_in.getsampwidth()
                    rate = wav_in.getframerate()
                    if width != 2 or channels not in (1, 2):
                        raise RuntimeError("Неожиданный формат WAV от Piper")
                    samples = np.frombuffer(wav_in.readframes(wav_in.getnframes()), dtype=np.int16)
                if channels == 2:
                    samples = samples.reshape(-1, 2).astype(np.int32).mean(axis=1).astype(np.int16)
                if rate != sample_rate and len(samples):
                    target_len = max(1, round(len(samples) * sample_rate / rate))
                    samples = np.interp(np.linspace(0, len(samples) - 1, target_len),
                                        np.arange(len(samples)), samples).astype(np.int16)
                return samples
            pcm = synthesize()
            slot_frames = max(1, int((end - start) * sample_rate))
            if len(pcm) > slot_frames:
                length_scale = max(0.55, min(1.0, slot_frames / max(1, len(pcm))))
                pcm = synthesize(length_scale)
            if len(pcm) > slot_frames:
                # Last-resort fit for exceptionally long translated phrases.
                pcm = np.interp(np.linspace(0, len(pcm) - 1, slot_frames),
                                np.arange(len(pcm)), pcm).astype(np.int16)
            offset = min(total_frames, int(start * sample_rate))
            count = min(len(pcm), total_frames - offset)
            if count <= 0:
                continue
            raw_track.seek(44 + offset * 2)
            existing_bytes = raw_track.read(count * 2)
            existing = np.frombuffer(existing_bytes, dtype=np.int16).astype(np.int32)
            mixed = np.clip(existing + pcm[:count].astype(np.int32), -32768, 32767).astype(np.int16)
            raw_track.seek(44 + offset * 2)
            raw_track.write(mixed.tobytes())


def render_translated_video(job_id: str):
    with _RENDER_LOCK:
        _render_translated_video_locked(job_id)


def _render_translated_video_locked(job_id: str):
    import app as worker

    work_dir = worker.JOB_DIR / f"render_{job_id}"
    result_dir = worker.JOB_DIR / "rendered"
    voice_dir = Path(os.getenv("PIPER_VOICE_DIR", str(worker.DATA_DIR / "piper-voices")))
    result_dir.mkdir(parents=True, exist_ok=True)
    output_path = result_dir / f"{job_id}.mp4"
    output_path.unlink(missing_ok=True)
    try:
        with worker.SessionLocal() as db:
            job = db.get(worker.Job, job_id)
            if not job:
                raise RuntimeError("Задача обработки не найдена")
            video = db.get(worker.Video, job.video_id)
            if not video:
                raise RuntimeError("Исходное видео не найдено")
            row = db.get(worker.RenderedOutput, job_id)
            if not row:
                row = worker.RenderedOutput(job_id=job_id, status="running")
                db.add(row)
            row.status = "running"
            row.error = ""
            db.commit()
            source_path = worker.resolve_video_path(video)
            segments = json.loads(job.segments_json or "[]")
            if not segments or any(not str(x.get("translated", "")).strip() for x in segments if str(x.get("text", "")).strip()):
                raise RuntimeError("Не все сегменты имеют перевод. Исправьте перевод и повторите сборку.")
            target_language = job.target_language
            original_name = video.filename

        voice_id = voice_for_language(target_language)
        if not voice_id:
            raise RuntimeError(f"Для языка {target_language} нет доступной модели Piper")
        duration, has_audio, video_codec = _probe(source_path)
        free_bytes = shutil.disk_usage(result_dir).free
        required_bytes = max(512 * 1024 * 1024, int(source_path.stat().st_size * 1.5))
        if free_bytes < required_bytes:
            raise RuntimeError("Недостаточно свободного места для временного MP4. Освободите место и повторите.")
        model_path = _download_voice(voice_id, voice_dir)
        from piper import PiperVoice
        voice = PiperVoice.load(model_path, download_dir=voice_dir)

        if work_dir.exists():
            shutil.rmtree(work_dir)
        work_dir.mkdir(parents=True)
        voice_track = work_dir / "translated.wav"
        _write_voice_track(segments, voice_track, duration, voice)

        safe_stem = Path(original_name).stem[:120] or "video"
        file_name = f"{safe_stem}_translated_{target_language}.mp4"
        if has_audio:
            audio_filter = "[0:a:0]volume=0.30[original];[1:a:0]volume=1.0[voice];[original][voice]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0[aout]"
            audio_args = ["-filter_complex", audio_filter, "-map", "[aout]"]
        else:
            audio_args = ["-map", "1:a:0"]
        video_args = ["-c:v", "copy"] if video_codec in ("h264", "hevc", "av1") else [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"
        ]
        command = ["ffmpeg", "-y", "-i", str(source_path), "-i", str(voice_track),
                   "-map", "0:v:0", *audio_args, *video_args,
                   "-c:a", "aac", "-b:a", "160k", "-map_metadata", "0",
                   "-movflags", "+faststart", "-shortest", str(output_path)]
        _run(command)

        if not output_path.is_file() or output_path.stat().st_size < 1024:
            raise RuntimeError("FFmpeg не создал корректный MP4")
        with worker.SessionLocal() as db:
            row = db.get(worker.RenderedOutput, job_id)
            row.status = "ready"
            row.file_path = str(output_path)
            row.file_name = file_name
            row.size_bytes = output_path.stat().st_size
            row.error = ""
            row.expires_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=24)
            db.commit()
    except Exception as exc:
        output_path.unlink(missing_ok=True)
        with worker.SessionLocal() as db:
            row = db.get(worker.RenderedOutput, job_id)
            if row:
                row.status = "failed"
                row.file_path = ""
                row.size_bytes = 0
                row.error = str(exc)[:3000]
                row.expires_at = None
                db.commit()
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def recover_interrupted_renders():
    """Mark jobs interrupted by a worker restart as retryable failures."""
    import app as worker
    with worker.SessionLocal() as db:
        rows = db.query(worker.RenderedOutput).filter(
            worker.RenderedOutput.status.in_(("queued", "running"))).all()
        for row in rows:
            row.status = "failed"
            row.error = "Worker перезапустился во время сборки. Нажмите «Создать видео» повторно."
            row.file_path = ""
            row.size_bytes = 0
        db.commit()


def cleanup_expired_rendered_outputs():
    """Remove temporary MP4 files after 24h if they were not downloaded."""
    import app as worker
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    with worker.SessionLocal() as db:
        rows = db.query(worker.RenderedOutput).filter(
            worker.RenderedOutput.status == "ready",
            worker.RenderedOutput.expires_at.is_not(None),
            worker.RenderedOutput.expires_at <= now).all()
        for row in rows:
            if row.file_path:
                Path(row.file_path).unlink(missing_ok=True)
            row.status = "expired"
            row.file_path = ""
            row.size_bytes = 0
        db.commit()
