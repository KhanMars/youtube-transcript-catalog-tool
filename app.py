#!/usr/bin/env python3
"""Search timestamped transcript sentences in a local catalog."""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

from flask import Flask, request
from markupsafe import Markup, escape

PAGE_SIZE = 40
TOKEN_RE = re.compile(r"[\w']+", re.UNICODE)

PAGE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Transcript search</title>
  <style>
    :root { color-scheme: light; }
    * { box-sizing: border-box; }
    body {
      margin: 0; background: #f4f1ea; color: #1d1b16;
      font: 16px/1.45 "Segoe UI", system-ui, sans-serif;
    }
    header, main { width: min(920px, calc(100% - 32px)); margin: 0 auto; }
    header { padding: 28px 0 8px; }
    h1 { font-size: 28px; line-height: 1.15; margin: 0 0 6px; letter-spacing: -0.03em; }
    .sub { color: #5e584e; margin: 0 0 18px; }
    form { display: flex; gap: 8px; flex-wrap: wrap; }
    input[type=search], select {
      border: 1px solid #cfc6b8; background: #fff; color: inherit;
      border-radius: 8px; padding: 10px 12px; font: inherit;
    }
    input[type=search] { flex: 1 1 280px; }
    button, .pager a {
      border: 0; border-radius: 8px; padding: 10px 14px; font: inherit;
      background: #1d1b16; color: #fff; text-decoration: none; cursor: pointer;
    }
    .stats { color: #5e584e; margin: 14px 0; }
    article {
      background: #fff; border: 1px solid #e4ddd2; border-radius: 12px;
      padding: 14px 16px; margin: 0 0 10px;
    }
    .sentence { font-size: 18px; margin: 0 0 8px; }
    .context { color: #6d675d; margin: 0 0 8px; font-size: 14px; }
    .meta { color: #5e584e; font-size: 14px; display: flex; gap: 10px; flex-wrap: wrap; }
    .meta a { color: #9a3412; }
    mark { background: #f6d36b; color: inherit; border-radius: 3px; padding: 0 2px; }
    .pager { display: flex; gap: 8px; margin: 16px 0 40px; }
    .empty { padding: 24px 0 48px; color: #5e584e; }
    pre.desc {
      white-space: pre-wrap; font: inherit; background: #fff;
      border: 1px solid #e4ddd2; border-radius: 12px; padding: 14px 16px;
    }
    .line { margin: 0 0 8px; }
    .line a { color: #9a3412; text-decoration: none; margin-right: 8px; }
  </style>
</head>
<body>
  <header>
    <h1>{{ heading }}</h1>
    <p class="sub">{{ subtitle }}</p>
    <form method="get" action="/">
      <input type="search" name="q" value="{{ q }}" placeholder="Keyword or &quot;exact phrase&quot;" autofocus>
      {% if channels %}
      <select name="channel">
        <option value="">All channels</option>
        {% for name in channels %}
        <option value="{{ name }}" {% if name == channel %}selected{% endif %}>{{ name }}</option>
        {% endfor %}
      </select>
      {% endif %}
      <select name="sort">
        <option value="relevance" {% if sort == "relevance" %}selected{% endif %}>Relevance</option>
        <option value="views" {% if sort == "views" %}selected{% endif %}>Most viewed first</option>
      </select>
      <button type="submit">Search</button>
    </form>
    <p class="stats">{{ stats }}</p>
  </header>
  <main>
    {% if detail %}
      <pre class="desc">{{ detail.description or "No description." }}</pre>
      {% for line in detail.lines %}
      <p class="line"><a href="{{ line.url }}" target="_blank" rel="noopener noreferrer">{{ line.stamp }}</a> {{ line.text }}</p>
      {% endfor %}
      <p class="pager"><a href="/">Back to search</a></p>
    {% elif q and not results %}
      <p class="empty">No sentences matched. Try a shorter or less exact query.</p>
    {% endif %}
    {% for row in results %}
    <article>
      <p class="sentence">{{ row.text_html | safe }}</p>
      {% if row.prev or row.next %}
      <p class="context">{{ row.prev }} {% if row.prev and row.next %}·{% endif %} {{ row.next }}</p>
      {% endif %}
      <div class="meta">
        <a href="{{ row.jump }}" target="_blank" rel="noopener noreferrer">Open at {{ row.stamp }}</a>
        <a href="{{ row.video_page }}">{{ row.title }}</a>
        <span>{{ row.channel }}</span>
        <span>{{ row.published }}</span>
        <span>{{ row.views }} views</span>
        <span>{{ row.likes }} likes</span>
        <span>{{ row.duration }}</span>
      </div>
    </article>
    {% endfor %}
    {% if prev_url or next_url %}
    <div class="pager">
      {% if prev_url %}<a href="{{ prev_url }}">Previous</a>{% endif %}
      {% if next_url %}<a href="{{ next_url }}">Next</a>{% endif %}
    </div>
    {% endif %}
  </main>
</body>
</html>
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def to_fts(query: str) -> str | None:
    stripped = query.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] == '"':
        phrase = stripped[1:-1].strip()
        return f'"{phrase.replace(chr(34), " ")}"' if phrase else None
    tokens = [token for token in TOKEN_RE.findall(stripped) if len(token) > 1]
    if not tokens:
        return None
    return " AND ".join('"' + token.replace('"', "") + '"' for token in tokens)


def highlight(text: str, tokens: list[str]) -> Markup:
    if not tokens:
        return Markup(escape(text or ""))
    pattern = re.compile(
        "(" + "|".join(re.escape(token) for token in sorted(set(tokens), key=len, reverse=True)) + ")",
        re.IGNORECASE,
    )
    parts = []
    last = 0
    for match in pattern.finditer(text or ""):
        parts.append(escape(text[last:match.start()]))
        parts.append(Markup("<mark>") + escape(match.group(0)) + Markup("</mark>"))
        last = match.end()
    parts.append(escape((text or "")[last:]))
    return Markup("").join(parts)


def stamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def duration_label(seconds) -> str:
    if seconds is None:
        return "unknown"
    return stamp(float(seconds))


def count_label(value) -> str:
    return "n/a" if value is None else f"{int(value):,}"


def jump_url(url: str, video_id: str, start: float) -> str:
    base = url or f"https://www.youtube.com/watch?v={video_id}"
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}t={max(0, int(start))}s"


def neighbor(conn: sqlite3.Connection, video_id: str, start: float, before: bool) -> str:
    if before:
        sql = "SELECT text FROM sentences WHERE video_id = ? AND start < ? ORDER BY start DESC LIMIT 1"
    else:
        sql = "SELECT text FROM sentences WHERE video_id = ? AND start > ? ORDER BY start ASC LIMIT 1"
    row = conn.execute(sql, (video_id, start)).fetchone()
    return row["text"] if row else ""


def search(conn, fts_query: str, channel: str, limit: int, offset: int, sort: str = "relevance"):
    order = "bm25(sentences_fts)"
    if sort == "views":
        order = "v.view_count DESC, bm25(sentences_fts)"
    sql = f"""
        SELECT s.video_id, s.start, s.end, s.text,
               v.title, v.channel, v.upload_date, v.view_count, v.like_count,
               v.duration, v.url
        FROM sentences_fts
        JOIN sentences s ON s.id = sentences_fts.rowid
        JOIN videos v ON v.video_id = s.video_id
        WHERE sentences_fts MATCH ?
          AND (? = '' OR v.channel = ?)
        ORDER BY {order}
        LIMIT ? OFFSET ?
    """
    return conn.execute(sql, (fts_query, channel, channel, limit, offset)).fetchall()


def count_matches(conn, fts_query: str, channel: str) -> int:
    sql = """
        SELECT count(*)
        FROM sentences_fts
        JOIN sentences s ON s.id = sentences_fts.rowid
        JOIN videos v ON v.video_id = s.video_id
        WHERE sentences_fts MATCH ?
          AND (? = '' OR v.channel = ?)
    """
    return int(conn.execute(sql, (fts_query, channel, channel)).fetchone()[0])


def make_app(db_path: Path) -> Flask:
    app = Flask(__name__)

    def db():
        return connect(db_path)

    @app.get("/")
    def index():
        from flask import render_template_string

        query = (request.args.get("q") or "").strip()
        channel = (request.args.get("channel") or "").strip()
        sort = (request.args.get("sort") or "relevance").strip()
        page = max(1, int(request.args.get("page") or 1))
        conn = db()
        channels = [
            row["channel"]
            for row in conn.execute(
                "SELECT DISTINCT channel FROM videos WHERE channel != '' ORDER BY channel"
            )
        ]
        video_count = conn.execute("SELECT count(*) FROM videos").fetchone()[0]
        sentence_count = conn.execute("SELECT count(*) FROM sentences").fetchone()[0]
        results = []
        total = 0
        fts_query = to_fts(query) if query else None
        tokens = TOKEN_RE.findall(query)
        error = ""
        if query and fts_query:
            try:
                total = count_matches(conn, fts_query, channel)
                rows = search(conn, fts_query, channel, PAGE_SIZE, (page - 1) * PAGE_SIZE, sort)
            except sqlite3.OperationalError as exc:
                rows = []
                error = str(exc)
            for row in rows:
                results.append(
                    {
                        "text_html": highlight(row["text"], tokens),
                        "prev": neighbor(conn, row["video_id"], row["start"], True),
                        "next": neighbor(conn, row["video_id"], row["start"], False),
                        "title": row["title"],
                        "channel": row["channel"] or "unknown channel",
                        "published": row["upload_date"] or "unknown date",
                        "views": count_label(row["view_count"]),
                        "likes": count_label(row["like_count"]),
                        "duration": duration_label(row["duration"]),
                        "stamp": stamp(row["start"]),
                        "jump": jump_url(row["url"], row["video_id"], row["start"]),
                        "video_page": f"/video/{row['video_id']}",
                    }
                )
        elif query:
            error = "Enter at least one word longer than one character."
        stats = f"{video_count:,} videos · {sentence_count:,} sentences"
        if query:
            stats = f"{total:,} matches · " + stats
        if error:
            stats = error
        def page_url(number: int) -> str:
            return f"/?q={query}&channel={channel}&sort={sort}&page={number}"
        return render_template_string(
            PAGE,
            heading="Transcript search",
            subtitle="Matching sentences across the whole catalog.",
            q=query,
            channel=channel,
            channels=channels,
            stats=stats,
            results=results,
            detail=None,
            prev_url=page_url(page - 1) if page > 1 else "",
            next_url=page_url(page + 1) if page * PAGE_SIZE < total else "",
            sort=sort,
        )

    @app.get("/video/<video_id>")
    def video(video_id: str):
        from flask import render_template_string

        if not re.fullmatch(r"[\w-]{11}", video_id):
            return ("Not found", 404)
        conn = db()
        video_row = conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,)).fetchone()
        if not video_row:
            return ("Not found", 404)
        lines = []
        for row in conn.execute(
            "SELECT start, text FROM sentences WHERE video_id = ? ORDER BY start",
            (video_id,),
        ):
            lines.append(
                {
                    "stamp": stamp(row["start"]),
                    "text": row["text"],
                    "url": jump_url(video_row["url"], video_id, row["start"]),
                }
            )
        return render_template_string(
            PAGE,
            heading=video_row["title"],
            subtitle=(
                f"{video_row['channel'] or 'unknown'} · {video_row['upload_date'] or 'unknown date'} · "
                f"{count_label(video_row['view_count'])} views · {count_label(video_row['like_count'])} likes · "
                f"{duration_label(video_row['duration'])}"
            ),
            q="",
            channel="",
            channels=[],
            stats=video_row["url"],
            results=[],
            detail={"description": video_row["description"], "lines": lines},
            prev_url="",
            next_url="",
        )

    return app


def print_cli(conn, query: str, channel: str) -> None:
    fts_query = to_fts(query)
    if not fts_query:
        sys.exit("Query needs at least one word.")
    rows = search(conn, fts_query, channel, 50, 0)
    if not rows:
        print("No matches.")
        return
    for row in rows:
        print(f"[{stamp(row['start'])}] {row['title']}")
        print(f"  {row['text']}")
        print(f"  {jump_url(row['url'], row['video_id'], row['start'])}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Search a local transcript catalog.")
    parser.add_argument("--data", default="data")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--query", help="Print matches and exit instead of serving the UI")
    parser.add_argument("--channel", default="")
    args = parser.parse_args()
    db_path = Path(args.data) / "catalog.db"
    if not db_path.exists():
        sys.exit(f"No database at {db_path}. Run pipeline.py first.")
    conn = connect(db_path)
    if args.query:
        print_cli(conn, args.query, args.channel)
        return
    print(f"Serving on http://{args.host}:{args.port}")
    make_app(db_path).run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
