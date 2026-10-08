#!/usr/bin/env python3
"""Build a local transcript catalog for a YouTube channel.

Examples:
  python pipeline.py --channel "https://www.youtube.com/@SomeCreator" --data data
  python pipeline.py --video "https://www.youtube.com/watch?v=VIDEO_ID" --mode whisper
  python pipeline.py --data data --reindex
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sqlite3
import sys
import time
import traceback
from pathlib import Path

import yt_dlp

ID_RE = re.compile(r"^[\w-]{11}$")
VIDEO_RE = re.compile(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})")
CUE_RE = re.compile(
    r"(?:(\d+):)?(\d+):(\d+)[.,](\d+)\s+-->\s+(?:(\d+):)?(\d+):(\d+)[.,](\d+)"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    title TEXT,
    channel TEXT,
    upload_date TEXT,
    view_count INTEGER,
    like_count INTEGER,
    duration INTEGER,
    url TEXT,
    description TEXT,
    source TEXT
);
CREATE TABLE IF NOT EXISTS sentences (
    id INTEGER PRIMARY KEY,
    video_id TEXT NOT NULL,
    start REAL NOT NULL,
    end REAL NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sentences_video_start
    ON sentences(video_id, start);
CREATE VIRTUAL TABLE IF NOT EXISTS sentences_fts USING fts5(
    text,
    content='sentences',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS sentences_ai AFTER INSERT ON sentences BEGIN
    INSERT INTO sentences_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS sentences_ad AFTER DELETE ON sentences BEGIN
    INSERT INTO sentences_fts(sentences_fts, rowid, text)
    VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS sentences_au AFTER UPDATE ON sentences BEGIN
    INSERT INTO sentences_fts(sentences_fts, rowid, text)
    VALUES ('delete', old.id, old.text);
    INSERT INTO sentences_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


def safe_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def reset_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TRIGGER IF EXISTS sentences_ai;
        DROP TRIGGER IF EXISTS sentences_ad;
        DROP TRIGGER IF EXISTS sentences_au;
        DROP TABLE IF EXISTS sentences_fts;
        DROP TABLE IF EXISTS sentences;
        DROP TABLE IF EXISTS videos;
        """
    )
    init_db(conn)


def clean_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\u00a0", " ").replace("\x00", "")
    return re.sub(r"\s+", " ", text).strip()


def format_date(value) -> str:
    raw = str(value or "")
    if re.fullmatch(r"\d{8}", raw):
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw or "unknown"


def format_duration(seconds) -> str:
    if seconds is None:
        return "unknown"
    total = max(0, int(round(float(seconds))))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_ts(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_count(value) -> str:
    if value is None:
        return "n/a"
    return f"{int(value):,}"


import random

def ydl_common(cookies: str | None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "retries": 5,
        "fragment_retries": 5,
        "sleep_requests": 2,
        "sleep_interval_requests": 1,
    }
    if cookies:
        opts["cookiesfrombrowser"] = (cookies,)
    return opts


def retry_yt_dlp(func, max_retries=3):
    """Simple retry with exponential backoff for 429 errors."""
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            if "429" in str(e) or "Too Many Requests" in str(e):
                wait = (2 ** attempt) + random.uniform(1, 3)
                print(f"  Rate limited (429). Waiting {wait:.1f}s before retry...")
                time.sleep(wait)
            else:
                raise
    raise


def expand_sources(url: str) -> list[str]:
    if re.search(r"watch\?v=|youtu\.be/|list=", url):
        return [url]
    base = url.rstrip("/")
    if base.endswith(("/videos", "/shorts", "/streams")):
        return [base]
    return [f"{base}/videos", f"{base}/shorts", f"{base}/streams"]


def list_ids(url: str) -> list[str]:
    opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        return []
    found: list[tuple[str, int]] = []

    def take(entry: dict | None) -> None:
        if not entry:
            return
        nested = entry.get("entries")
        if nested:
            for child in nested:
                take(child)
            return
        video_id = entry.get("id")
        if video_id and ID_RE.fullmatch(str(video_id)):
            views = entry.get("view_count") or 0
            found.append((str(video_id), int(views)))

    if info.get("entries"):
        for entry in info["entries"]:
            take(entry)
    else:
        take(info)

    # sort by view count desc, then keep unique
    found.sort(key=lambda x: x[1], reverse=True)
    seen: set[str] = set()
    unique: list[tuple[str, int]] = []
    for video_id, views in found:
        if video_id not in seen:
            seen.add(video_id)
            unique.append((video_id, views))
    return unique


def parse_video_arg(value: str) -> str:
    match = VIDEO_RE.search(value)
    if match:
        return match.group(1)
    if ID_RE.fullmatch(value):
        return value
    raise argparse.ArgumentTypeError(f"Not a video URL or 11-character ID: {value}")


def meta_from_info(info: dict, video_id: str, source: str) -> dict:
    duration = info.get("duration")
    return {
        "video_id": video_id,
        "title": clean_text(info.get("title") or video_id),
        "channel": clean_text(info.get("channel") or info.get("uploader") or ""),
        "upload_date": format_date(info.get("upload_date")),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "duration": None if duration is None else int(round(float(duration))),
        "url": info.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}",
        "description": (info.get("description") or "").replace("\x00", "").strip(),
        "source": source,
        "sentences": [],
    }


def fetch_info(video_id: str, cookies: str | None) -> dict:
    opts = ydl_common(cookies)
    opts["skip_download"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = retry_yt_dlp(lambda: ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False))
    if not info:
        raise RuntimeError("no metadata returned")
    return info


def timestamp_to_seconds(hours, minutes, seconds, millis) -> float:
    ms = (millis or "0")[:3].ljust(3, "0")
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(ms) / 1000.0


def parse_vtt(raw: str) -> list[dict]:
    cues: list[dict] = []
    lines = raw.splitlines()
    index = 0
    while index < len(lines):
        match = CUE_RE.search(lines[index])
        if not match:
            index += 1
            continue
        start = timestamp_to_seconds(*match.group(1, 2, 3, 4))
        end = timestamp_to_seconds(*match.group(5, 6, 7, 8))
        index += 1
        parts: list[str] = []
        while index < len(lines) and lines[index].strip() and not CUE_RE.search(lines[index]):
            parts.append(lines[index].strip())
            index += 1
        text = clean_text(" ".join(parts))
        if text:
            cues.append({"start": start, "end": end, "text": text})
    return dedupe_cues(cues)


def dedupe_cues(cues: list[dict]) -> list[dict]:
    cleaned: list[dict] = []
    for cue in cues:
        text = clean_text(cue["text"])
        if not text:
            continue
        item = {"start": float(cue["start"]), "end": float(cue["end"]), "text": text}
        if not cleaned:
            cleaned.append(item)
            continue
        prev = cleaned[-1]
        if text == prev["text"]:
            prev["end"] = max(prev["end"], item["end"])
            continue
        if text.startswith(prev["text"]) and item["start"] <= prev["end"] + 1.5:
            cleaned[-1] = item
            continue
        if prev["text"].endswith(text) and len(text) < len(prev["text"]):
            continue
        cleaned.append(item)
    return cleaned


def cues_to_sentences(cues: list[dict]) -> list[dict]:
    sentences: list[dict] = []
    buf: list[str] = []
    start = None
    end = None

    def flush() -> None:
        nonlocal buf, start, end
        if not buf:
            return
        text = clean_text(" ".join(buf))
        if text and start is not None and end is not None:
            sentences.append({"start": start, "end": end, "text": text})
        buf = []
        start = None
        end = None

    for cue in cues:
        piece = clean_text(cue["text"])
        if not piece:
            continue
        if start is None:
            start = float(cue["start"])
        if buf and piece.lower() == buf[-1].lower():
            end = float(cue["end"])
            continue
        buf.append(piece)
        end = float(cue["end"])
        if re.search(r"[.!?]$", piece) or sum(len(part) for part in buf) > 280:
            flush()
    flush()
    return sentences


def captions_are_usable(sentences: list[dict]) -> bool:
    text = " ".join(item["text"] for item in sentences)
    return len(sentences) >= 3 and len(text) >= 80


def download_captions(video_id: str, work_dir: Path, cookies: str | None) -> tuple[dict, list[dict]]:
    work_dir.mkdir(parents=True, exist_ok=True)
    for old in work_dir.glob(f"{video_id}*.vtt"):
        old.unlink()
    opts = ydl_common(cookies)
    opts.update(
        {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en", "en-US", "en-GB", "en.*"],
            "subtitlesformat": "vtt",
            "outtmpl": str(work_dir / "%(id)s"),
        }
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = retry_yt_dlp(lambda: ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True))
    if not info:
        raise RuntimeError("no metadata returned")
    paths = sorted(work_dir.glob(f"{video_id}*.vtt"))
    doc = meta_from_info(info, video_id, "captions")
    if not paths:
        return doc, []
    best = min(paths, key=lambda path: ("orig" in path.name.lower(), len(path.name)))
    cues = parse_vtt(best.read_text(encoding="utf-8", errors="replace"))
    return doc, cues_to_sentences(cues)


def download_audio(video_id: str, audio_dir: Path, cookies: str | None) -> tuple[Path, dict]:
    audio_dir.mkdir(parents=True, exist_ok=True)
    opts = ydl_common(cookies)
    opts.update(
        {
            "format": "bestaudio/best",
            "outtmpl": str(audio_dir / f"{video_id}.%(ext)s"),
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "64",
                }
            ],
        }
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = retry_yt_dlp(lambda: ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True))
    if not info:
        raise RuntimeError("audio download returned no metadata")
    mp3 = audio_dir / f"{video_id}.mp3"
    if mp3.exists():
        return mp3, info
    matches = [path for path in audio_dir.glob(f"{video_id}.*") if path.is_file()]
    if not matches:
        raise FileNotFoundError(f"audio file missing for {video_id}")
    return matches[0], info


def load_whisper(model_name: str, device: str):
    from faster_whisper import WhisperModel

    if device == "cuda":
        attempts = [("cuda", "float16"), ("cuda", "int8")]
    elif device == "cpu":
        attempts = [("cpu", "int8")]
    else:
        attempts = [("cuda", "float16"), ("cpu", "int8")]
    last_error: Exception | None = None
    for dev, compute in attempts:
        try:
            print(f"Loading Whisper '{model_name}' on {dev} ({compute})...")
            return WhisperModel(model_name, device=dev, compute_type=compute)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Could not load Whisper model: {last_error}")


def words_to_sentences(segments) -> list[dict]:
    sentences: list[dict] = []
    buf = []
    sent_start = None
    last_end = None

    def flush() -> None:
        nonlocal buf, sent_start, last_end
        if not buf:
            return
        text = clean_text("".join(word.word for word in buf))
        if text:
            sentences.append(
                {
                    "start": float(sent_start if sent_start is not None else buf[0].start),
                    "end": float(last_end if last_end is not None else buf[-1].end),
                    "text": text,
                }
            )
        buf = []
        sent_start = None

    for segment in segments:
        words = getattr(segment, "words", None) or []
        if not words:
            text = clean_text(getattr(segment, "text", "") or "")
            if text:
                sentences.append(
                    {
                        "start": float(segment.start),
                        "end": float(segment.end),
                        "text": text,
                    }
                )
            continue
        for word in words:
            if sent_start is None:
                sent_start = word.start
            buf.append(word)
            last_end = word.end
            token = (word.word or "").strip()
            if re.search(r"[.!?][\"')\]]*$", token):
                flush()
        if buf and len("".join(word.word for word in buf)) > 400:
            flush()
    flush()
    return sentences


def transcribe_audio(model, audio_path: Path, language: str | None) -> list[dict]:
    def run(vad: bool, word_timestamps: bool = True) -> list[dict]:
        segments, _info = model.transcribe(
            str(audio_path),
            language=language,
            vad_filter=vad,
            word_timestamps=word_timestamps,
        )
        return words_to_sentences(segments)

    try:
        return run(True)
    except TypeError as e:
        if "metadata_errors" in str(e):
            print("  Whisper version issue detected, retrying without word timestamps...")
            return run(True, word_timestamps=False)
        print(f"  retrying without VAD ({e})")
        return run(False)
    except Exception as exc:
        print(f"  retrying without VAD ({exc})")
        return run(False)


def write_outputs(doc: dict, transcript_dir: Path) -> None:
    transcript_dir.mkdir(parents=True, exist_ok=True)
    video_id = doc["video_id"]
    json_path = transcript_dir / f"{video_id}.json"
    txt_path = transcript_dir / f"{video_id}.txt"
    json_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        f"Title: {doc['title']}",
        f"Channel: {doc.get('channel') or 'unknown'}",
        f"Published: {doc.get('upload_date') or 'unknown'}",
        f"Views: {format_count(doc.get('view_count'))}",
        f"Likes: {format_count(doc.get('like_count'))}",
        f"Duration: {format_duration(doc.get('duration'))}",
        f"Link: {doc.get('url')}",
        f"Source: {doc.get('source') or 'unknown'}",
        "",
        "Description:",
        doc.get("description") or "",
        "",
        "Transcript:",
    ]
    if not doc["sentences"]:
        lines.append("(no transcript)")
    for sentence in doc["sentences"]:
        stamp = f"{format_ts(sentence['start'])} - {format_ts(sentence['end'])}"
        lines.append(f"[{stamp}] {sentence['text']}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def upsert(conn: sqlite3.Connection, doc: dict) -> None:
    conn.execute("DELETE FROM sentences WHERE video_id = ?", (doc["video_id"],))
    conn.execute(
        """
        INSERT INTO videos (
            video_id, title, channel, upload_date, view_count, like_count,
            duration, url, description, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(video_id) DO UPDATE SET
            title=excluded.title,
            channel=excluded.channel,
            upload_date=excluded.upload_date,
            view_count=excluded.view_count,
            like_count=excluded.like_count,
            duration=excluded.duration,
            url=excluded.url,
            description=excluded.description,
            source=excluded.source
        """,
        (
            doc["video_id"],
            doc.get("title"),
            doc.get("channel"),
            doc.get("upload_date"),
            doc.get("view_count"),
            doc.get("like_count"),
            doc.get("duration"),
            doc.get("url"),
            doc.get("description"),
            doc.get("source"),
        ),
    )
    conn.executemany(
        "INSERT INTO sentences (video_id, start, end, text) VALUES (?, ?, ?, ?)",
        [
            (doc["video_id"], float(item["start"]), float(item["end"]), item["text"])
            for item in doc.get("sentences") or []
            if item.get("text")
        ],
    )
    conn.commit()


def ensure_indexed(conn: sqlite3.Connection, json_path: Path, video_id: str) -> str:
    row = conn.execute("SELECT 1 FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    if row:
        return "skip"
    upsert(conn, json.loads(json_path.read_text(encoding="utf-8")))
    return "indexed"


def log_error(log_path: Path, video_id: str, exc: BaseException) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n=== {video_id} ===\n")
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=handle)


def reindex(data_dir: Path) -> None:
    transcript_dir = data_dir / "transcripts"
    conn = connect(data_dir / "catalog.db")
    reset_db(conn)
    files = sorted(transcript_dir.glob("*.json"))
    for path in files:
        upsert(conn, json.loads(path.read_text(encoding="utf-8")))
    conn.execute("INSERT INTO sentences_fts(sentences_fts) VALUES ('optimize')")
    conn.commit()
    print(f"Reindexed {len(files)} transcript files.")


def save_doc(conn: sqlite3.Connection, doc: dict, transcript_dir: Path) -> None:
    write_outputs(doc, transcript_dir)
    upsert(conn, doc)


def process_video(video_id: str, args, conn, dirs: dict, model_box: dict) -> str:
    json_path = dirs["transcripts"] / f"{video_id}.json"
    if json_path.exists() and not args.force:
        return ensure_indexed(conn, json_path, video_id)

    captions_error = None
    if args.mode in {"auto", "captions"}:
        try:
            doc, sentences = download_captions(video_id, dirs["subs"], args.cookies_from_browser)
            doc["sentences"] = sentences
            usable = captions_are_usable(sentences)
            if args.mode == "captions" or usable:
                if not sentences:
                    raise RuntimeError("no captions found")
                save_doc(conn, doc, dirs["transcripts"])
                return "captions"
            captions_error = "captions missing or too short"
        except Exception as exc:
            captions_error = str(exc)
            if args.mode == "captions":
                raise
        print(f"  captions unusable ({captions_error}); falling back to Whisper")

    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is not on PATH, required for Whisper mode")
    if model_box["model"] is None:
        model_box["model"] = load_whisper(args.model, args.device)

    audio_path = dirs["audio"] / f"{video_id}.mp3"
    if audio_path.exists() and audio_path.stat().st_size > 0 and not args.force:
        info = fetch_info(video_id, args.cookies_from_browser)
    else:
        audio_path, info = download_audio(video_id, dirs["audio"], args.cookies_from_browser)

    doc = meta_from_info(info, video_id, "whisper")
    doc["sentences"] = transcribe_audio(model_box["model"], audio_path, args.language)
    if not doc["sentences"]:
        raise RuntimeError("Whisper returned an empty transcript")
    save_doc(conn, doc, dirs["transcripts"])
    if not args.keep_audio:
        audio_path.unlink(missing_ok=True)
        for leftover in dirs["audio"].glob(f"{video_id}.*"):
            leftover.unlink(missing_ok=True)
    return "whisper"


def collect_ids(args) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for video_id in args.video or []:
        if video_id not in seen:
            seen.add(video_id)
            ids.append(video_id)
    if args.channel:
        for source in expand_sources(args.channel):
            try:
                found = list_ids(source)
            except Exception as exc:
                print(f"Could not list {source}: {exc}")
                continue
            print(f"{source}: {len(found)} videos")
            for video_id, _ in found:
                if video_id not in seen:
                    seen.add(video_id)
                    ids.append(video_id)
    if args.limit:
        ids = ids[: args.limit]
    return ids


def main() -> None:
    safe_stdio()
    parser = argparse.ArgumentParser(description="Download metadata and build timestamped transcripts.")
    parser.add_argument("--channel", help="Channel or tab URL, preferably .../videos")
    parser.add_argument("--video", action="append", type=parse_video_arg, help="Single video URL or ID")
    parser.add_argument("--data", default="data", help="Output directory")
    parser.add_argument("--mode", choices=("auto", "whisper", "captions"), default="auto")
    parser.add_argument("--model", default="small", help="faster-whisper model: tiny, base, small, medium, large-v3")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--language", default=None, help="Whisper language code, e.g. en. Default: detect")
    parser.add_argument("--limit", type=int, default=0, help="Process only the first N videos")
    parser.add_argument("--sleep", type=float, default=3.0, help="Seconds to wait between videos")
    parser.add_argument("--force", action="store_true", help="Redo videos that already have transcripts")
    parser.add_argument("--keep-audio", action="store_true", help="Keep mp3 files after Whisper")
    parser.add_argument("--cookies-from-browser", default=None, help="e.g. chrome, for videos you can already watch")
    parser.add_argument("--reindex", action="store_true", help="Rebuild the search database from JSON files")
    args = parser.parse_args()

    data_dir = Path(args.data)
    if args.reindex:
        reindex(data_dir)
        return
    if not args.channel and not args.video:
        parser.error("--channel or --video is required")

    dirs = {
        "transcripts": data_dir / "transcripts",
        "audio": data_dir / "audio",
        "subs": data_dir / "subs",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "errors.log"
    conn = connect(data_dir / "catalog.db")
    init_db(conn)

    ids = collect_ids(args)
    if not ids:
        sys.exit("No videos found. Pass a /videos channel URL, or check that yt-dlp can see the channel.")
    print(f"Processing {len(ids)} videos in mode={args.mode}")

    counts = {"skip": 0, "indexed": 0, "captions": 0, "whisper": 0, "failed": 0}
    model_box = {"model": None}
    for number, video_id in enumerate(ids, start=1):
        print(f"[{number}/{len(ids)}] {video_id}")
        try:
            status = process_video(video_id, args, conn, dirs, model_box)
            counts[status] = counts.get(status, 0) + 1
            print(f"  {status}")
        except Exception as exc:
            counts["failed"] += 1
            log_error(log_path, video_id, exc)
            print(f"  failed: {exc}")
        if args.sleep and number < len(ids):
            time.sleep(args.sleep)

    conn.execute("INSERT INTO sentences_fts(sentences_fts) VALUES ('optimize')")
    conn.commit()
    print(
        "Done. "
        + ", ".join(f"{name}={value}" for name, value in counts.items())
        + f". Search with: python app.py --data {data_dir}"
    )


if __name__ == "__main__":
    main()
