"""Telegram calorie tracker: talk to it in plain language and it keeps the daily tally.

There are no commands. Every message goes through `handle_message`: a few short phrases
("today", "undo", "yes") are answered directly, everything else is read by Claude, which
works out what the user wants (log a meal, fix the log, set a goal, save a recipe, ...).
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
import re
import time
import uuid
from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from .db import MEALS, Entry, Recipe, Store, meal_for_time
from .health import pending_days, start_server, sync_link
from .nutrition import EstimateError, make_estimator

log = logging.getLogger("calorie_bot")

HELP = """<b>Calorie tracker</b>
Just talk to me. Some things you can say:

🍽 <i>2 eggs and toast for breakfast</i>, or send a photo of your meal
   (no meal named? I go by the time of day)
📋 <i>what did I eat today?</i> · <i>what did I have for lunch?</i> · <i>dinner yesterday?</i>
   · <i>how did I do this week?</i>
✏️ <i>actually it was 2 eggs, not 3</i> · <i>remove the toast</i> · <i>the arugula was dinner</i>
   · <i>undo</i>
🎯 <i>set my goal to 2000</i> · <i>what's my goal?</i>
❤️ <i>set up Apple Health</i> (sends each finished day to the Health app)

📖 <b>Recipes</b>
<i>save this as my vinaigrette:</i> and the ingredients (I'll make up a name if you don't give one)
<i>it makes 4 servings</i> · <i>it weighed 1.6 kg cooked</i>
<i>37g of the vinaigrette</i> · <i>1 serving of chili</i> · <i>half the batch</i>
<i>show my recipes</i> · <i>rename it to Dijon vinaigrette</i> · <i>delete the chili recipe</i>"""


# ---- formatting -------------------------------------------------------------

def fmt_kcal(n: float) -> str:
    return f"{int(round(n)):,}"


def fmt_small_kcal(n: float) -> str:
    """Like fmt_kcal, but keeps a decimal under 10 kcal (a serving of dressing can be 0.8)."""
    return f"{n:.1f}" if 0 < n < 10 else fmt_kcal(n)


def day_summary(store: Store, user_id: int) -> str:
    total = sum(e.calories for e in store.entries_for_day(user_id, store.today()))
    goal = store.get_goal(user_id)
    if goal:
        left = goal - total
        tail = f"{fmt_kcal(left)} left" if left >= 0 else f"{fmt_kcal(-left)} over"
        return f"<b>Today:</b> {fmt_kcal(total)} / {fmt_kcal(goal)} kcal ({tail})"
    return f"<b>Today:</b> {fmt_kcal(total)} kcal"


def macro_line(entries: list[Entry]) -> str:
    p = sum(e.protein_g for e in entries)
    c = sum(e.carbs_g for e in entries)
    f = sum(e.fat_g for e in entries)
    return f"P {p:.0f}g · C {c:.0f}g · F {f:.0f}g"


MEAL_ICONS = {"breakfast": "🍳", "lunch": "🥗", "dinner": "🍝", "snack": "🍎"}


def day_name(store: Store, offset: int) -> str:
    if offset == 0:
        return "today"
    if offset == -1:
        return "yesterday"
    return f"{store.today() + timedelta(days=offset):%A %b %-d}"


def meal_block(meal: str, entries: list[Entry]) -> list[str]:
    total = sum(e.calories for e in entries)
    lines = [f"{MEAL_ICONS[meal]} <b>{meal.capitalize()}</b> — {fmt_kcal(total)} kcal"]
    lines += [f"   • {html.escape(e.item)} — {fmt_kcal(e.calories)}" for e in entries]
    return lines


def day_text(store: Store, user_id: int, offset: int = 0, meal: str = "") -> str:
    """One day's log grouped by meal, or just one meal of it."""
    offset = max(-30, min(0, offset))
    day = store.today() + timedelta(days=offset)
    entries = store.entries_for_day(user_id, day)
    when = day_name(store, offset)

    if meal in MEALS:
        picked = [e for e in entries if e.meal == meal]
        if not picked:
            return f"Nothing logged for {meal} {when}."
        lines = meal_block(meal, picked) + ["", macro_line(picked)]
        return "\n".join(lines)

    if not entries:
        return f"Nothing logged {when} yet. Tell me what you ate." if offset == 0 else \
               f"Nothing logged {when}."
    lines = [f"<b>{day:%A %b %-d}</b>"]
    for m in MEALS:
        picked = [e for e in entries if e.meal == m]
        if picked:
            lines += [""] + meal_block(m, picked)
    lines.append("")
    if offset == 0:
        lines.append(day_summary(store, user_id))
    else:
        lines.append(f"<b>Total:</b> {fmt_kcal(sum(e.calories for e in entries))} kcal")
    lines.append(macro_line(entries))
    return "\n".join(lines)


def today_text(store: Store, user_id: int) -> str:
    return day_text(store, user_id, 0)


def week_text(store: Store, user_id: int) -> str:
    days = store.totals_by_day(user_id, 7)
    goal = store.get_goal(user_id)
    scale = max([goal or 0] + [t for _, t in days]) or 1
    lines = ["<b>Last 7 days</b>", "<pre>"]
    for d, total in days:
        lines.append(f"{d:%a %d} {total:>6,} {'█' * round(12 * total / scale)}")
    lines.append("</pre>")
    logged = [t for _, t in days if t]
    if logged:
        lines.append(f"Average (days logged): {fmt_kcal(sum(logged) / len(logged))} kcal")
    if goal:
        lines.append(f"Goal: {fmt_kcal(goal)} kcal")
    return "\n".join(lines)


def today_log_for_claude(store: Store, user_id: int) -> list[str]:
    return [f"#{e.id} {e.eaten_at:%H:%M} {e.meal}: {e.item} ({fmt_kcal(e.calories)} kcal)"
            for e in store.entries_for_day(user_id, store.today())]


# ---- conversation memory ----------------------------------------------------

class ChatMemory:
    """The last few messages each way, per user, so "rename it" knows what "it" is.

    Kept in memory only: a restart forgets it, which is fine for short-range context."""

    def __init__(self, max_lines: int = 8, max_chars: int = 400):
        self.max_lines, self.max_chars = max_lines, max_chars
        self._lines: dict[int, deque] = {}

    def add(self, user_id: int, who: str, text: str) -> None:
        plain = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
        if len(plain) > self.max_chars:
            plain = plain[: self.max_chars] + "…"
        self._lines.setdefault(user_id, deque(maxlen=self.max_lines)).append(f"{who}: {plain}")

    def lines(self, user_id: int) -> list[str]:
        return list(self._lines.get(user_id, ()))


class RecordingMessage:
    """Wraps a Telegram message so every reply_text is also remembered."""

    def __init__(self, msg, memory: ChatMemory, user_id: int):
        self._msg, self._memory, self._user_id = msg, memory, user_id

    async def reply_text(self, text, **kwargs):
        self._memory.add(self._user_id, "Bot", text)
        return await self._msg.reply_text(text, **kwargs)

    def __getattr__(self, name):
        return getattr(self._msg, name)


async def keep_typing(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Show "typing..." until cancelled (Telegram clears it after ~5s, estimates take longer)."""
    with contextlib.suppress(Exception):
        while True:
            await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(4)


# ---- the router -------------------------------------------------------------

def normalize(text: str) -> str:
    """'/Today@my_bot!' -> 'today'. Used only for the instant replies below."""
    text = re.sub(r"^/(\w+)(@\w+)?", r"\1", text.strip().lower())
    return re.sub(r"[^\w' ]+", "", text).strip()


# Short messages answered without asking Claude, so they're instant.
QUICK = {
    "today": {"today", "log", "my log", "today's log", "todays log", "what did i eat today",
              "what have i eaten today", "how many calories left", "calories left", "summary"},
    "week": {"week", "this week", "my week", "last 7 days", "weekly"},
    "undo": {"undo", "undo that", "undo last", "scratch that"},
    "help": {"help", "start", "what can you do", "how does this work", "commands", "menu"},
    "recipes": {"recipes", "my recipes", "list recipes", "show recipes", "show my recipes"},
    "health": {"apple health", "health", "health sync", "sync health", "set up apple health",
               "setup apple health", "connect apple health", "sync apple health"},
}
YES_RE = re.compile(r"^(yes|y|yep|yeah|yup|sure|confirm|do it|delete it|ok|okay)\b", re.I)
NO_RE = re.compile(r"^(no|n|nope|cancel|keep it|don't|dont|stop)\b", re.I)
CONFIRM_SECONDS = 300


def quick_intent(text: str) -> str | None:
    t = normalize(text)
    return next((name for name, phrases in QUICK.items() if t in phrases), None)


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = ctx.bot_data["store"]
    memory: ChatMemory = ctx.bot_data.setdefault("memory", ChatMemory())
    pending: dict = ctx.bot_data.setdefault("pending_delete", {})
    user_id = update.effective_user.id
    raw = update.effective_message
    text = (raw.text or raw.caption or "").strip()
    history = memory.lines(user_id)
    memory.add(user_id, "User", text + (" [photo]" if raw.photo else ""))
    msg = RecordingMessage(raw, memory, user_id)

    # A recipe deletion waiting for "yes"?
    waiting = pending.pop(user_id, None)
    if waiting and not raw.photo and time.monotonic() - waiting[1] < CONFIRM_SECONDS:
        if YES_RE.match(text):
            await delete_recipe_reply(msg, store, user_id, waiting[0])
            return
        if NO_RE.match(text):
            await msg.reply_text(f"Okay, I kept “{waiting[0]}”.")
            return
        # Anything else: drop the pending delete and treat the message normally.

    quick = None if raw.photo else quick_intent(text)
    if quick:
        await QUICK_REPLIES[quick](msg, store, user_id)
        return

    await claude_reply(update, ctx, msg, store, user_id, text, history)


async def claude_reply(update, ctx, msg, store: Store, user_id: int, text: str,
                       history: list[str]) -> None:
    estimator = ctx.bot_data["estimator"]
    image = None
    if msg.photo:
        tg_file = await msg.photo[-1].get_file()
        image = bytes(await tg_file.download_as_bytearray())

    typing = asyncio.create_task(keep_typing(ctx, msg.chat_id))
    try:
        result = await estimator.estimate(
            text=text or None, image=image,
            recipe_names=[recipe_label(r) for r in store.list_recipes(user_id)],
            history=history, today_log=today_log_for_claude(store, user_id),
            now=f"{datetime.now(store.tz):%A %H:%M}")
    except EstimateError as e:
        await msg.reply_text(f"⚠️ {e}")
        return
    finally:
        typing.cancel()

    intent = result.get("intent")
    note = (result.get("note") or "").strip()
    recipe_out = result.get("recipe") or {}
    recipe_name = (recipe_out.get("name") or "").strip()
    entry_ids = [int(i) for i in result.get("entry_ids") or []]
    meal = result.get("meal") or ""
    log.info("intent=%s", intent)

    if intent == "save_recipe" and recipe_out.get("ingredients"):
        await save_recipe_and_reply(msg, store, user_id, {**recipe_out, "note": note},
                                    source_text=text or "photo")
    elif intent == "update_recipe":
        await update_recipe_and_reply(msg, store, user_id, recipe_out)
    elif intent == "rename_recipe":
        await rename_recipe_reply(msg, store, user_id, recipe_name,
                                  (result.get("rename_to") or "").strip())
    elif intent == "show_recipe":
        await show_recipe_reply(msg, store, user_id, recipe_name)
    elif intent == "list_recipes":
        await recipes_reply(msg, store, user_id)
    elif intent == "delete_recipe":
        await ask_delete_recipe(msg, store, user_id, recipe_name, ctx.bot_data["pending_delete"])
    elif intent in ("show_day", "show_today"):
        await msg.reply_text(day_text(store, user_id, int(result.get("day_offset") or 0), meal),
                             parse_mode=ParseMode.HTML)
    elif intent == "move_entries":
        await move_entries_reply(msg, store, user_id, entry_ids, meal)
    elif intent == "show_week":
        await week_reply(msg, store, user_id)
    elif intent == "undo_last":
        await undo_reply(msg, store, user_id)
    elif intent == "set_goal":
        await goal_reply(msg, store, user_id, result.get("goal_kcal") or 0)
    elif intent == "delete_entries":
        await delete_entries_reply(msg, store, user_id, entry_ids)
    elif intent == "help":
        await help_reply(msg, store, user_id)
    elif intent == "apple_health":
        await health_reply(msg, store, user_id)
    elif intent in ("log_meal", "correct_log") and result.get("items"):
        await log_items_reply(msg, store, user_id, result["items"], note, text or "photo",
                              replace_ids=entry_ids if intent == "correct_log" else [], meal=meal)
    else:
        await msg.reply_text(note or "I couldn't find any food in that. Try e.g. "
                             "“a turkey sandwich and an apple”, or ask me for help.")


# ---- logging food -----------------------------------------------------------

def apply_recipes(store: Store, user_id: int,
                  items: list[dict]) -> tuple[list[dict], list[bool], list[str]]:
    """Swap Claude's guesses for exact numbers where an item is a portion of a saved recipe.

    Returns (items, which ones came from a recipe, warnings for the user)."""
    out, used, warnings = [], [], []
    for i in items:
        name = (i.get("recipe") or "").strip()
        recipe = store.get_recipe(user_id, name) if name else None
        portion = None
        if recipe:
            grams, servings = i.get("recipe_grams") or 0, i.get("recipe_servings") or 0
            portion = recipe.portion(grams, servings, i.get("recipe_fraction") or 0,
                                     raw=bool(i.get("recipe_grams_raw")))
            if portion is None and servings and not recipe.servings:
                warnings.append(f"“{recipe.name}” doesn't have a number of servings yet, so I "
                                f"estimated that one. Tell me e.g. “{recipe.name} makes 4 servings” "
                                "and I'll use it from now on, or give the amount in grams.")
        out.append(portion or i)
        used.append(portion is not None)
    return out, used, warnings


async def log_items_reply(msg, store: Store, user_id: int, items: list[dict], note: str,
                          description: str, replace_ids: list[int] | None = None,
                          meal: str = "") -> None:
    items, from_recipe, warnings = apply_recipes(store, user_id, items)

    replaced: list[Entry] = []
    for entry_id in replace_ids or []:
        entry = store.delete_entry(user_id, entry_id)
        if entry:
            replaced.append(entry)
    # A correction keeps the original time and meal, so it stays where it was.
    eaten_at = min(e.eaten_at for e in replaced) if replaced else None
    if replaced and meal not in MEALS:
        meal = replaced[0].meal
    if meal not in MEALS:
        meal = meal_for_time(datetime.now(store.tz))
    store.add_items(user_id, description, items, meal_id=uuid.uuid4().hex, eaten_at=eaten_at,
                    meal=meal)

    lines = []
    if replaced:
        lines.append("✏️ <b>Fixed</b>")
        lines += [f"• <s>{html.escape(e.item)} — {fmt_kcal(e.calories)} kcal</s>" for e in replaced]
    else:
        label = "as a snack" if meal == "snack" else f"for {meal}"
        lines.append(f"{MEAL_ICONS[meal]} <b>Logged {label}</b>")
    lines += [f"• {'📖 ' if used else ''}{html.escape(i['item'])} — {fmt_kcal(i['calories'])} kcal"
              for i, used in zip(items, from_recipe)]
    if len(items) > 1:
        lines.append(f"<b>Meal:</b> {fmt_kcal(sum(i['calories'] for i in items))} kcal")
    if note:
        lines.append(f"<i>{html.escape(note)}</i>")
    lines += [f"⚠️ {html.escape(w)}" for w in warnings]
    lines += ["", day_summary(store, user_id)]
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def move_entries_reply(msg, store: Store, user_id: int, entry_ids: list[int],
                             meal: str) -> None:
    moved = store.set_meal(user_id, entry_ids, meal)
    if not moved:
        await msg.reply_text("I couldn't tell which entry to move, or to which meal. "
                             "Here's today so far:\n\n" + today_text(store, user_id),
                             parse_mode=ParseMode.HTML)
        return
    names = ", ".join(html.escape(e.item) for e in moved)
    where = "snacks" if meal == "snack" else meal
    await msg.reply_text(f"{MEAL_ICONS[meal]} Moved {names} to {where}.", parse_mode=ParseMode.HTML)


async def delete_entries_reply(msg, store: Store, user_id: int, entry_ids: list[int]) -> None:
    removed = [e for e in (store.delete_entry(user_id, i) for i in entry_ids) if e]
    if not removed:
        await msg.reply_text("I couldn't tell which entry you meant. Here's today so far:\n\n"
                             + today_text(store, user_id), parse_mode=ParseMode.HTML)
        return
    lines = ["🗑 <b>Removed</b>"]
    lines += [f"• {html.escape(e.item)} — {fmt_kcal(e.calories)} kcal" for e in removed]
    lines += ["", day_summary(store, user_id)]
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def undo_reply(msg, store: Store, user_id: int) -> None:
    removed = store.undo_last_meal(user_id)
    if not removed:
        await msg.reply_text("Nothing to undo.")
        return
    names = ", ".join(html.escape(e.item) for e in removed)
    await msg.reply_text(f"↩️ Removed {names} ({fmt_kcal(sum(e.calories for e in removed))} kcal)"
                         f"\n\n{day_summary(store, user_id)}", parse_mode=ParseMode.HTML)


async def today_reply(msg, store: Store, user_id: int) -> None:
    await msg.reply_text(today_text(store, user_id), parse_mode=ParseMode.HTML)


async def week_reply(msg, store: Store, user_id: int) -> None:
    await msg.reply_text(week_text(store, user_id), parse_mode=ParseMode.HTML)


async def help_reply(msg, store: Store, user_id: int) -> None:
    await msg.reply_text(HELP, parse_mode=ParseMode.HTML)


def health_port() -> int:
    return int(os.environ.get("HEALTH_PORT", "8787"))


async def health_reply(msg, store: Store, user_id: int) -> None:
    if not health_port():
        await msg.reply_text("Apple Health sync is turned off on this bot (HEALTH_PORT=0).")
        return
    link = sync_link(store, user_id, health_port())
    waiting = len(pending_days(store, user_id))
    lines = [
        "❤️ <b>Apple Health sync</b>",
        "A Shortcut on your iPhone copies each finished day into Health every morning: "
        "calories, protein, carbs and fat for each meal. Today's meals go in tomorrow, so "
        "anything you fix or undo today doesn't leave stray entries in Health.",
        "",
        "<b>Your personal link</b> (keep it private):",
        f"<code>{html.escape(link)}</code>",
        "It only works on your home Wi-Fi.",
        "",
        "<b>Make the Shortcut</b> (Shortcuts app → +), adding these actions in order:",
        "1. <b>Get Contents of URL</b>: paste your link",
        "2. <b>Get Dictionary Value</b>: Value for <code>samples</code> in Contents of URL",
        "3. <b>Repeat with Each</b> item in Dictionary Value. Inside the repeat:",
        "   a. <b>Get Dictionary Value</b>: <code>date</code> in Repeat Item, then "
        "<b>Get Dates from Input</b>",
        "   b. <b>Get Dictionary Value</b>: <code>kcal</code> in Repeat Item, then "
        "<b>Log Health Sample</b>: Dietary Energy, value = Dictionary Value, date = Dates",
        "   c. Same pair for <code>protein_g</code> → Protein, <code>carbs_g</code> → "
        "Carbohydrates, <code>fat_g</code> → Total Fat",
        "4. After End Repeat: <b>Get Dictionary Value</b>: <code>ack_url</code> in Contents "
        "of URL, then <b>Get Contents of URL</b> with that value",
        "",
        "Run it once by hand so iOS asks to let Shortcuts write to Health; allow it.",
        "",
        "<b>Run it every morning:</b> Shortcuts → Automation → + → Time of Day, e.g. 6:00 AM, "
        "Daily, <i>Run Immediately</i>. If your phone isn't home one morning, the next run "
        "catches up.",
        "",
        f"Days waiting to sync: {waiting}." if waiting else "Nothing waiting to sync yet.",
    ]
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def goal_reply(msg, store: Store, user_id: int, goal: float) -> None:
    if goal < 0:
        store.set_goal(user_id, None)
        await msg.reply_text("Daily goal cleared.")
    elif goal == 0:
        current = store.get_goal(user_id)
        await msg.reply_text(f"Your daily goal is {fmt_kcal(current)} kcal." if current else
                             "You don't have a daily goal yet. Say e.g. “set my goal to 2000”.")
    elif 500 <= goal <= 10000:
        store.set_goal(user_id, int(round(goal)))
        await msg.reply_text(f"🎯 Daily goal set to {fmt_kcal(goal)} kcal.\n\n"
                             f"{day_summary(store, user_id)}", parse_mode=ParseMode.HTML)
    else:
        await msg.reply_text("That goal looks off. Pick something between 500 and 10,000 kcal.")


# ---- recipes ----------------------------------------------------------------

def recipe_label(r: Recipe) -> str:
    """How a saved recipe is described to Claude when it reads a message."""
    servings = f"{r.servings:g} servings" if r.servings else "servings not set"
    if r.cooked:
        return (f"{r.name} (whole batch: {r.total_weight_g:.0f} g cooked, "
                f"{r.raw_weight_g:.0f} g raw, {servings})")
    return f"{r.name} (whole batch: {r.total_weight_g:g} g, {servings})"


def batch_weight_text(r: Recipe) -> str:
    if r.weight_basis == "estimated" and r.cooked:
        return f"{r.raw_weight_g:.0f} g raw → about {r.total_weight_g:.0f} g cooked (estimated)"
    if r.weight_basis == "weighed" and r.cooked:
        return f"{r.raw_weight_g:.0f} g raw → {r.total_weight_g:.0f} g cooked (weighed)"
    return f"{r.total_weight_g:g} g"


def per_100g(r: Recipe) -> float:
    return r.calories / r.total_weight_g * 100 if r.total_weight_g else 0


def recipe_card(r: Recipe) -> str:
    lines = [f"📖 <b>{html.escape(r.name)}</b>"]
    for i in r.ingredients:
        lines.append(f"• {html.escape(i['ingredient'])} ({i['grams']:g} g) — {fmt_kcal(i['calories'])} kcal")
    lines.append("")
    lines.append(f"<b>Whole recipe:</b> {fmt_kcal(r.calories)} kcal, {batch_weight_text(r)}")
    if r.total_weight_g:
        w = r.total_weight_g
        state = " cooked" if r.cooked else ""
        lines.append(f"<b>Per 100 g{state}:</b> {fmt_kcal(per_100g(r))} kcal "
                     f"({r.calories / w:.2f} kcal/g) · P {r.protein_g / w * 100:.1f}g · "
                     f"C {r.carbs_g / w * 100:.1f}g · F {r.fat_g / w * 100:.1f}g")
    if r.servings:
        lines.append(f"<b>Per serving</b> (makes {r.servings:g}): {fmt_small_kcal(r.calories / r.servings)} kcal")
    return "\n".join(lines)


SERVINGS_RE = re.compile(
    r"\b(?:serves|yields?|makes)\s+(\d+(?:\.\d+)?)(?![\d.])"
    r"(?!\s*(?:g|kg|grams?|ml|l|liters?|oz|lbs?|pounds?|cups?|quarts?)\b)"
    r"|\b(\d+(?:\.\d+)?)\s*(?:servings?|portions?)\b", re.I)


def servings_in(text: str) -> float:
    """'Totaling 107 servings', 'serves 4', 'makes 6 portions' -> the number; 0 if none."""
    m = SERVINGS_RE.search(text or "")
    return float(m.group(1) or m.group(2)) if m else 0


def free_name(store: Store, user_id: int, name: str) -> str:
    """`name`, or `name 2`, `name 3`... so a made-up title never overwrites a saved recipe."""
    candidate, n = name, 2
    while store.get_recipe(user_id, candidate):
        candidate, n = f"{name} {n}", n + 1
    return candidate


async def save_recipe_and_reply(msg, store: Store, user_id: int, result: dict,
                                source_text: str, explicit_name: str = "") -> None:
    ingredients = result.get("ingredients") or []
    note = (result.get("note") or "").strip()
    if not ingredients:
        await msg.reply_text(note or "I couldn't find any ingredients in that.")
        return

    suggested = (result.get("name") or "").strip()[:60]
    if explicit_name:
        name, made_up = explicit_name, False
    elif suggested and result.get("name_given"):
        name, made_up = suggested, False
    else:
        name, made_up = free_name(store, user_id, suggested or "Untitled recipe"), True

    before = store.get_recipe(user_id, name)
    servings = result.get("servings") or servings_in(source_text)  # backup if Claude missed it
    recipe, replaced = store.save_recipe(user_id, name, ingredients,
                                         result.get("final_weight_g") or 0, servings, source_text,
                                         cooked=bool(result.get("cooked")))
    if replaced and before:
        head = (f"✅ Updated your recipe (it was {fmt_kcal(before.calories)} kcal for "
                f"{before.total_weight_g:g} g)")
    else:
        head = "✅ Saved recipe"
    lines = [head, "", recipe_card(recipe)]
    if note:
        lines.append(f"<i>{html.escape(note)}</i>")
    if made_up:
        lines.append(f"\nYou didn't give it a name, so I called it “{html.escape(recipe.name)}”. "
                     "To change it, say e.g. “rename it to …”.")
    if recipe.weight_basis == "estimated":
        lines.append(f"\nThe cooked weight is an estimate. For exact numbers, weigh it after "
                     f"cooking and tell me, e.g. “the {html.escape(recipe.name.lower())} weighed "
                     f"{recipe.total_weight_g:.0f} g cooked”.")
    example = max(5, round(recipe.total_weight_g / 4 / 5) * 5)  # about a quarter of the batch
    cooked = " cooked" if recipe.cooked else ""
    lines.append(f"\nLog a portion like: <i>{example:g}g of {html.escape(recipe.name.lower())}</i>"
                 f" (grams{cooked}, as you'd weigh it on your plate)")
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def update_recipe_and_reply(msg, store: Store, user_id: int, recipe_out: dict) -> None:
    name = (recipe_out.get("name") or "").strip()
    servings = recipe_out.get("servings") or 0
    weight = recipe_out.get("final_weight_g") or 0
    recipe = store.get_recipe(user_id, name) if name else None
    if recipe is None:
        await not_found_reply(msg, store, user_id)
        return
    if not (servings > 0 or weight > 0):
        await msg.reply_text(f"What should I change about “{recipe.name}”? You can tell me how "
                             "many servings it makes or its finished weight.")
        return
    recipe = store.update_recipe_size(user_id, recipe.name, servings, weight)
    changed = []
    if servings > 0:
        changed.append(f"makes {servings:g} servings")
    if weight > 0:
        changed.append(f"weighs {weight:g} g when finished")
    await msg.reply_text(f"✅ Got it: “{html.escape(recipe.name)}” {' and '.join(changed)}.\n\n"
                         + recipe_card(recipe), parse_mode=ParseMode.HTML)


def recipes_list_text(store: Store, user_id: int) -> str:
    recipes = store.list_recipes(user_id)
    if not recipes:
        return ("No saved recipes yet. Send the ingredients and say “save this recipe”.")
    lines = ["📖 <b>Your recipes</b>"]
    for r in recipes:
        serving = f", {fmt_small_kcal(r.calories / r.servings)} per serving" if r.servings else ""
        lines.append(f"• {html.escape(r.name)} — {fmt_kcal(per_100g(r))} kcal/100 g{serving}")
    lines.append("\nAsk to see one, e.g. “show the vinaigrette”.")
    return "\n".join(lines)


async def recipes_reply(msg, store: Store, user_id: int) -> None:
    await msg.reply_text(recipes_list_text(store, user_id), parse_mode=ParseMode.HTML)


async def not_found_reply(msg, store: Store, user_id: int) -> None:
    names = ", ".join(r.name for r in store.list_recipes(user_id)) or "none yet"
    await msg.reply_text(f"I couldn't tell which recipe you meant. Your recipes: {names}.")


async def show_recipe_reply(msg, store: Store, user_id: int, name: str) -> None:
    recipe = store.get_recipe(user_id, name) if name else None
    if recipe is None:
        await not_found_reply(msg, store, user_id)
        return
    await msg.reply_text(recipe_card(recipe), parse_mode=ParseMode.HTML)


async def rename_recipe_reply(msg, store: Store, user_id: int, name: str, new_name: str) -> None:
    recipe = store.get_recipe(user_id, name) if name else None
    if recipe is None:
        await not_found_reply(msg, store, user_id)
        return
    if not new_name:
        await msg.reply_text(f"What should I rename “{recipe.name}” to?")
        return
    result = store.rename_recipe(user_id, recipe.name, new_name)
    replies = {"ok": f"✅ Renamed “{recipe.name}” to “{new_name}”.",
               "taken": f"You already have a recipe called “{new_name}”. "
                        "Delete or rename that one first."}
    await msg.reply_text(replies.get(result, f"No saved recipe called “{recipe.name}”."))


async def ask_delete_recipe(msg, store: Store, user_id: int, name: str, pending: dict) -> None:
    recipe = store.get_recipe(user_id, name) if name else None
    if recipe is None:
        await not_found_reply(msg, store, user_id)
        return
    pending[user_id] = (recipe.name, time.monotonic())
    await msg.reply_text(f"Delete the recipe “{recipe.name}”? Reply yes to delete it or no to keep "
                         "it. Meals you already logged with it won't change.")


async def delete_recipe_reply(msg, store: Store, user_id: int, name: str) -> None:
    if store.delete_recipe(user_id, name):
        await msg.reply_text(f"🗑 Deleted the recipe “{name}”.")
    else:
        await msg.reply_text(f"“{name}” was already gone.")


QUICK_REPLIES = {"today": today_reply, "week": week_reply, "undo": undo_reply,
                 "help": help_reply, "recipes": recipes_reply, "health": health_reply}


# ---- access and errors ------------------------------------------------------

async def unauthorized(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else "?"
    log.warning("Ignoring message from unauthorized user %s", uid)
    if update.effective_message is None:
        return
    await update.effective_message.reply_text(
        f"This is a private bot. Your Telegram user id is {uid}; "
        "add it to ALLOWED_USER_IDS if this is your bot.")


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("⚠️ Something went wrong. Check the logs.")


# ---- wiring -----------------------------------------------------------------

def build_app() -> Application:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    tz = ZoneInfo(os.environ.get("TIMEZONE", "America/Los_Angeles"))
    db_path = os.environ.get("DB_PATH", "data/calories.db")
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    allowed = [int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x]

    store = Store(db_path, tz)
    app = Application.builder().token(token).concurrent_updates(True).build()
    app.bot_data["store"] = store
    app.bot_data["estimator"] = make_estimator()

    if allowed:
        me = filters.User(user_id=allowed)
    else:
        log.warning("ALLOWED_USER_IDS is empty: nobody can use the bot until you set it.")
        me = filters.User(user_id=[])

    # One handler for everything, slash-prefixed text included: there are no commands.
    app.add_handler(MessageHandler(me & (filters.TEXT | filters.PHOTO), handle_message))
    app.add_handler(MessageHandler(~me, unauthorized))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log the bot token in URLs
    app = build_app()
    if health_port():
        store: Store = app.bot_data["store"]
        start_server(os.environ.get("DB_PATH", "data/calories.db"), store.tz, health_port())
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
