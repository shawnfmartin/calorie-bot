"""Meal reminders: one nudge per meal per day, only if nothing's logged for it yet.

Each person has a time per meal (breakfast, lunch, dinner; no snack reminders). Once a
minute the bot checks everyone: if it's past a meal's reminder time, nothing is logged for
that meal today, and the meal hasn't been reminded about or skipped, it sends one message.
Reminders only go out within REMINDER_WINDOW of their time, so a bot that was down all
afternoon doesn't send a stale lunch reminder at night.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta

from .db import REMINDER_MEALS, Store

log = logging.getLogger(__name__)

REMINDER_WINDOW = timedelta(hours=2)
BUILT_IN_DEFAULTS = {"breakfast": "10:30", "lunch": "14:30", "dinner": "20:30"}
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

MESSAGES = {
    "breakfast": "🍳 Haven't logged breakfast yet. What did you have? "
                 "(Or tell me you skipped it.)",
    "lunch": "🥗 Haven't logged lunch yet. What did you have? (Or tell me you skipped it.)",
    "dinner": "🍝 Haven't logged dinner yet. What did you have? (Or tell me you skipped it.)",
}


def default_reminders() -> dict[str, str | None]:
    """From REMINDERS in .env: "breakfast=10:30,lunch=14:30,dinner=20:30", or "off"."""
    raw = os.environ.get("REMINDERS", "").strip()
    if not raw:
        return dict(BUILT_IN_DEFAULTS)
    if raw.lower() in ("off", "none", "0", "false"):
        return {m: None for m in REMINDER_MEALS}
    out: dict[str, str | None] = {m: None for m in REMINDER_MEALS}
    for part in raw.split(","):
        meal, _, when = part.partition("=")
        meal, when = meal.strip().lower(), when.strip()
        if meal in out and TIME_RE.match(when):
            out[meal] = normalize_time(when)
    return out


def normalize_time(value: str) -> str:
    h, m = TIME_RE.match(value.strip()).groups()
    return f"{int(h):02d}:{m}"


def fmt_time(hhmm: str) -> str:
    """'14:30' -> '2:30 PM'."""
    t = datetime.strptime(hhmm, "%H:%M")
    return f"{t:%-I:%M %p}"


def due_reminders(store: Store, user_id: int, now: datetime) -> list[str]:
    """Meals to remind this person about right now."""
    today = now.date()
    entries = store.entries_for_day(user_id, today)
    logged = {e.meal for e in entries}
    due = []
    for meal, when in store.get_reminders(user_id, default_reminders()).items():
        if not when or meal in logged or store.reminder_done(user_id, today, meal):
            continue
        at = datetime.combine(today, datetime.strptime(when, "%H:%M").time(), now.tzinfo)
        if at <= now < at + REMINDER_WINDOW:
            due.append(meal)
    return due


def apply_change(store: Store, user_id: int, meal: str, value: str) -> dict[str, str | None]:
    """Change one meal's reminder (or all, when meal is ""). value: HH:MM, "off" or "on"."""
    current = store.get_reminders(user_id, default_reminders())
    meals = [meal] if meal in REMINDER_MEALS else list(REMINDER_MEALS)
    for m in meals:
        if value == "off":
            current[m] = None
        elif value == "on":
            current[m] = current[m] or default_reminders().get(m) or BUILT_IN_DEFAULTS[m]
        else:
            current[m] = normalize_time(value)
    store.set_reminders(user_id, current)
    return current


async def reminder_loop(app, user_ids: list[int], every: float = 60) -> None:
    """Runs for the bot's lifetime, checking every minute."""
    store: Store = app.bot_data["store"]
    while True:
        try:
            await send_due(app, store, user_ids, datetime.now(store.tz))
        except Exception:  # never let one bad check stop reminders for good
            log.exception("Reminder check failed")
        await asyncio.sleep(every)


async def send_due(app, store: Store, user_ids: list[int], now: datetime) -> list[tuple[int, str]]:
    sent = []
    for user_id in user_ids:
        for meal in due_reminders(store, user_id, now):
            # Mark first: even if sending fails (e.g. they never opened the bot), only once.
            store.mark_reminder(user_id, now.date(), meal, "sent")
            try:
                await app.bot.send_message(chat_id=user_id, text=MESSAGES[meal])
            except Exception as e:
                log.warning("Couldn't send %s reminder to %s: %s", meal, user_id, e)
                continue
            memory = app.bot_data.get("memory")
            if memory is not None:
                memory.add(user_id, "Bot", MESSAGES[meal])
            log.info("Sent %s reminder to %s", meal, user_id)
            sent.append((user_id, meal))
    return sent
