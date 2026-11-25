"""Apple Health sync: a tiny web endpoint an iPhone Shortcut reads every morning.

Apple Health has no server API, so the phone pulls instead: a Shortcut fetches
GET /health/<token>, logs each sample into Health ("Log Health Sample"), then calls the
`ack_url` from the response so those days aren't sent again.

Only finished days are sent (yesterday and earlier, up to 30 days back). A Shortcut can't
delete what it logged, so waiting until a day is over means fixes and undos never leave
stray entries in Health. Each sample is one meal of one day: its calories and macros,
timed at the first thing eaten in that meal.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from .db import MEALS, Store

log = logging.getLogger(__name__)

MAX_DAYS_BACK = 30


def base_url(port: int) -> str:
    configured = os.environ.get("HEALTH_BASE_URL", "").rstrip("/")
    return configured or f"http://{socket.gethostname()}.local:{port}"


def sync_link(store: Store, user_id: int, port: int) -> str:
    return f"{base_url(port)}/health/{store.health_token(user_id)}"


def pending_days(store: Store, user_id: int) -> list[date]:
    """Finished days with food logged that haven't gone to Apple Health yet, oldest first."""
    today = store.today()
    synced = store.synced_days(user_id)
    days = [today - timedelta(days=k) for k in range(MAX_DAYS_BACK, 0, -1)]
    return [d for d in days if d not in synced and store.entries_for_day(user_id, d)]


def export(store: Store, user_id: int, link: str) -> dict:
    samples, days = [], pending_days(store, user_id)
    for d in days:
        entries = store.entries_for_day(user_id, d)
        for meal in MEALS:
            picked = [e for e in entries if e.meal == meal]
            if not picked:
                continue
            when = min(e.eaten_at for e in picked)
            samples.append({
                "day": d.isoformat(),
                "meal": meal,
                # Shortcuts reads this format as a date without any extra step.
                "date": f"{when:%b} {when.day}, {when:%Y} at {when:%-I:%M %p}",
                "iso": when.isoformat(timespec="seconds"),
                "kcal": round(sum(e.calories for e in picked)),
                "protein_g": round(sum(e.protein_g for e in picked), 1),
                "carbs_g": round(sum(e.carbs_g for e in picked), 1),
                "fat_g": round(sum(e.fat_g for e in picked), 1),
            })
    ack = f"{link}/ack?days={','.join(d.isoformat() for d in days)}" if days else f"{link}/ack"
    return {"samples": samples, "days": [d.isoformat() for d in days], "ack_url": ack}


def acknowledge(store: Store, user_id: int, day_list: str) -> list[str]:
    """Mark the given days synced. Only days that could have been sent are accepted."""
    allowed = set(pending_days(store, user_id))
    picked = []
    for part in (day_list or "").split(","):
        try:
            d = date.fromisoformat(part.strip())
        except ValueError:
            continue
        if d in allowed:
            picked.append(d)
    store.mark_synced(user_id, picked)
    return [d.isoformat() for d in picked]


PATH_RE = re.compile(r"^/health/([A-Za-z0-9_-]{16,64})(/ack)?/?$")


def make_handler(db_path: str, tz: ZoneInfo, port: int):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server's naming)
            url = urlparse(self.path)
            m = PATH_RE.match(url.path)
            if not m:
                return self._send(404, {"error": "not found"})
            store = Store(db_path, tz)  # one connection per request: sqlite and threads
            try:
                user_id = store.user_for_health_token(m.group(1))
                if user_id is None:
                    return self._send(404, {"error": "not found"})
                link = f"{base_url(port)}/health/{m.group(1)}"
                if m.group(2):
                    days = parse_qs(url.query).get("days", [""])[0]
                    synced = acknowledge(store, user_id, days)
                    log.info("Apple Health: user %s synced %s", user_id, synced or "nothing new")
                    return self._send(200, {"ok": True, "synced": synced})
                return self._send(200, export(store, user_id, link))
            finally:
                store.conn.close()

        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):  # keep tokens out of the logs
            log.debug("health http: " + fmt, *[PATH_RE.sub("/health/…", str(a)) for a in args])

    return Handler


def start_server(db_path: str, tz: ZoneInfo, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(db_path, tz, port))
    threading.Thread(target=server.serve_forever, name="health-http", daemon=True).start()
    log.info("Apple Health sync listening on port %s", port)
    return server
