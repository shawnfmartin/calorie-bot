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
from . import reminders, summary
from . import targets as tgt

log = logging.getLogger("calorie_bot")

HELP = """<b>Calorie tracker</b>
Just talk to me. Some things you can say:

🍽 <i>2 eggs and toast for breakfast</i>, or send a photo of your meal
   (no meal named? I go by the time of day)
📋 <i>what did I eat today?</i> · <i>what did I have for lunch?</i> · <i>dinner yesterday?</i>
   · <i>how did I do this week?</i>
✏️ <i>actually it was 2 eggs, not 3</i> · <i>remove the toast</i> · <i>the arugula was dinner</i>
   · <i>undo</i>
🎯 <i>I want to lose weight</i>, then tell me your age, height, weight and activity, and I'll
   work out your calories and macros · <i>my targets</i> · <i>set my goal to 2000</i>
💬 <i>be tough on me</i> · <i>be encouraging</i> · <i>no coaching</i> (comments on your meals)
⚖️ <i>I weigh 82 kg today</i>
❤️ <i>set up Apple Health</i> (sends each finished day to the Health app)
📊 <i>summary</i> (a chart of your last 7 days; one also comes every Sunday)
⏰ <i>remind me about lunch at 1pm</i> · <i>no breakfast reminders</i> · <i>I skipped lunch</i>
   (I remind you once if a meal isn't logged by its time)

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
        macros = store.get_macro_targets(user_id)
        if macros:
            p = sum(e.protein_g for e in entries)
            c = sum(e.carbs_g for e in entries)
            f = sum(e.fat_g for e in entries)
            lines.append(f"P {p:.0f}/{macros['protein_g']}g · C {c:.0f}/{macros['carbs_g']}g · "
                         f"F {f:.0f}/{macros['fat_g']}g")
            return "\n".join(lines)
    else:
        lines.append(f"<b>Total:</b> {fmt_kcal(sum(e.calories for e in entries))} kcal")
    lines.append(macro_line(entries))
    return "\n".join(lines)


def today_text(store: Store, user_id: int) -> str:
    return day_text(store, user_id, 0)


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

    async def reply_photo(self, photo, caption: str = "", **kwargs):
        self._memory.add(self._user_id, "Bot", "[chart] " + caption)
        return await self._msg.reply_photo(photo, caption=caption, **kwargs)

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
    "week": {"week", "this week", "my week", "last 7 days", "weekly", "summary", "my summary",
             "give summary", "give me a summary", "give me summary", "weekly summary",
             "week summary", "show summary", "graph", "chart", "show my graph"},
    "weekly_settings": {"weekly summary settings"},
    "profile": {"targets", "my targets", "profile", "my profile", "my plan", "my macros"},
    "undo": {"undo", "undo that", "undo last", "scratch that"},
    "help": {"help", "start", "what can you do", "how does this work", "commands", "menu"},
    "recipes": {"recipes", "my recipes", "list recipes", "show recipes", "show my recipes"},
    "reminders": {"reminders", "my reminders", "show reminders", "show my reminders",
                  "what are my reminders", "reminder settings"},
    "health": {"apple health", "health", "health sync", "sync health", "set up apple health",
               "setup apple health", "connect apple health", "sync apple health"},
}
YES_RE = re.compile(r"^(yes|y|yep|yeah|yup|sure|confirm|do it|delete it|ok|okay)\b", re.I)
NO_RE = re.compile(r"^(no|n|nope|cancel|keep it|don't|dont|stop)\b", re.I)
CONFIRM_SECONDS = 300

# ---- guardrails: things that never need Claude ---------------------------------------
GREETINGS = {"hi", "hello", "hey", "hiya", "yo", "sup", "good morning", "morning",
             "good afternoon", "good evening", "hi there", "hello there", "hey there"}
ACKS = {"thanks", "thank you", "thanks a lot", "thank you so much", "thx", "ty", "tysm",
        "cool", "nice", "great", "awesome", "perfect", "ok", "okay", "k", "kk", "got it",
        "sounds good", "lol", "haha", "good", "alright", "sweet", "noted", "will do"}


def max_message_chars() -> int:
    return int(os.environ.get("MAX_MESSAGE_CHARS", "3000"))


def daily_claude_limit() -> int:
    """Claude calls allowed per person per day; 0 means no limit."""
    return int(os.environ.get("CLAUDE_DAILY_LIMIT", "100"))


def free_reply(text: str, has_photo: bool) -> str | None:
    """A canned reply for messages that don't need Claude, or None to ask Claude."""
    if has_photo:
        return None
    t = normalize(text)
    if not text.strip():
        return ""                                   # nothing to answer (e.g. a sticker)
    if not re.search(r"[^\W_]", text):              # emoji or punctuation only
        return "👍"
    if t in GREETINGS:
        return "Hi! Tell me what you ate, or say “help” to see what I can do."
    if t in ACKS:
        return "👍"
    if len(text) > max_message_chars():
        return (f"That message is too long for me ({len(text):,} characters; the limit is "
                f"{max_message_chars():,}). Send just the food, or the recipe's ingredients.")
    return None


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

    canned = free_reply(text, bool(raw.photo))
    if canned is not None:
        if canned:
            await msg.reply_text(canned)
        return

    limit = daily_claude_limit()
    if limit and store.count_claude_call(user_id) > limit:
        log.warning("user %s hit the daily limit of %s Claude calls", user_id, limit)
        await msg.reply_text(f"You've reached today's limit of {limit} messages that need "
                             "Claude. It resets at midnight. “today”, “undo” and “help” "
                             "still work.")
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
            now=f"{datetime.now(store.tz):%A %H:%M}", coaching=coaching_context(store, user_id))
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
    elif intent == "set_weekly_summary":
        await weekly_settings_reply(msg, store, user_id,
                                    (result.get("reminder_time") or "").strip().lower())
    elif intent == "set_reminders":
        await set_reminders_reply(msg, store, user_id, meal,
                                  (result.get("reminder_time") or "").strip().lower())
    elif intent == "skip_meal":
        await skip_meal_reply(msg, store, user_id, meal)
    elif intent == "update_profile":
        await update_profile_reply(msg, store, user_id, result.get("profile") or {})
    elif intent == "show_profile":
        await show_profile_reply(msg, store, user_id)
    elif intent in ("log_meal", "correct_log") and result.get("items"):
        await log_items_reply(msg, store, user_id, result["items"], note, text or "photo",
                              replace_ids=entry_ids if intent == "correct_log" else [], meal=meal,
                              coach=(result.get("coach") or "").strip())
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
                          meal: str = "", coach: str = "") -> None:
    items, from_recipe, warnings = apply_recipes(store, user_id, items)
    was_complete = day_complete(store, user_id)

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
    if coach and coaching_on(store, user_id):
        lines += ["", f"💬 {html.escape(coach)}"]
    lines += ["", day_summary(store, user_id)]
    if not was_complete and day_complete(store, user_id):
        lines += ["", day_verdict(store, user_id)]
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


def day_complete(store: Store, user_id: int) -> bool:
    """Breakfast, lunch and dinner each logged or skipped today."""
    return not summary.missing_meals(store, user_id, store.today())


def day_verdict(store: Store, user_id: int) -> str:
    """The end-of-day line: did today land on target?"""
    entries = store.entries_for_day(user_id, store.today())
    total = sum(e.calories for e in entries)
    goal = store.get_goal(user_id)
    if not goal:
        return f"🏁 <b>Day complete:</b> {fmt_kcal(total)} kcal."
    diff = total - goal
    if total < 0.75 * goal:
        verdict = (f"⚠️ {fmt_kcal(-diff)} under target. That's a lot under: eating too little "
                   "slows progress and costs muscle, so aim closer to your target")
    elif diff <= 0:
        verdict = f"✅ on target, {fmt_kcal(-diff)} under" if diff < 0 else "✅ exactly on target"
    else:
        verdict = f"❌ over target by {fmt_kcal(diff)}"
    line = f"🏁 <b>Day complete:</b> {fmt_kcal(total)} / {fmt_kcal(goal)} kcal, {verdict}."
    macros = store.get_macro_targets(user_id)
    if macros.get("protein_g"):
        protein = sum(e.protein_g for e in entries)
        mark = "✅" if protein >= 0.9 * macros["protein_g"] else "⚠️"
        line += f"\n{mark} Protein {protein:.0f} / {macros['protein_g']} g"
    return line


def coaching_on(store: Store, user_id: int) -> bool:
    profile = store.get_profile(user_id)
    return bool(store.get_goal(user_id)) and (profile.get("coach_tone") or "encouraging") != "off" \
        and bool(profile)


def coaching_context(store: Store, user_id: int) -> str | None:
    """What Claude needs to comment on a meal: tone, goal, target, eaten so far."""
    if not coaching_on(store, user_id):
        return None
    profile = store.get_profile(user_id)
    goal = store.get_goal(user_id)
    macros = store.get_macro_targets(user_id)
    entries = store.entries_for_day(user_id, store.today())
    eaten = sum(e.calories for e in entries)
    protein = sum(e.protein_g for e in entries)
    plan = {"lose": "lose weight", "gain": "gain weight", "maintain": "maintain weight"}.get(
        profile.get("goal", ""), "track calories")
    parts = [f"tone={profile.get('coach_tone') or 'encouraging'}", f"goal: {plan}",
             f"daily target {goal} kcal"]
    if macros:
        parts.append(f"protein target {macros.get('protein_g')} g")
    parts.append(f"eaten today before this message: {eaten} kcal, protein {protein:.0f} g")
    return "; ".join(parts)


def missing_profile_text(missing: list[str]) -> str:
    asks = [tgt.FIELD_PROMPTS[f] for f in missing]
    listed = asks[0] if len(asks) == 1 else ", ".join(asks[:-1]) + " and " + asks[-1]
    return (f"To work out your calories and macros I still need {listed}. You can send it all "
            "in one message, e.g. “34, male, 180 cm, 85 kg, desk job, gym 3 times a week”. "
            "Sex is optional but makes the estimate more accurate.")


def apply_targets(store: Store, user_id: int, profile: dict) -> tgt.Targets:
    t = tgt.calculate(profile)
    store.set_goal(user_id, t.calories)
    store.set_macro_targets(user_id, {"protein_g": t.protein_g, "carbs_g": t.carbs_g,
                                      "fat_g": t.fat_g})
    return t


async def update_profile_reply(msg, store: Store, user_id: int, changes: dict) -> None:
    changes = {k: v for k, v in changes.items() if v not in (None, "", 0, 0.0)}
    if not changes:
        await show_profile_reply(msg, store, user_id)
        return
    if changes.get("weight_kg"):
        store.log_weight(user_id, float(changes["weight_kg"]))
    profile = store.update_profile(user_id, changes)
    missing = tgt.missing_fields(profile)

    if set(changes) == {"coach_tone"}:
        tone = changes["coach_tone"]
        text = {"tough": "💬 Tough love it is. I'll be blunt about your meals.",
                "encouraging": "💬 Got it, I'll keep it encouraging.",
                "off": "💬 Okay, no more comments on your meals."}[tone]
        if tone != "off" and missing:
            text += "\n\n" + missing_profile_text(missing)
        await msg.reply_text(text)
        return
    if missing:
        await msg.reply_text("Got it. " + missing_profile_text(missing))
        return
    t = apply_targets(store, user_id, profile)
    head = ""
    if changes.get("weight_kg") and len(changes) == 1:
        head = f"⚖️ Logged {float(changes['weight_kg']):g} kg and updated your targets.\n\n"
    await msg.reply_text(head + tgt.profile_card(profile, t), parse_mode=ParseMode.HTML)


async def show_profile_reply(msg, store: Store, user_id: int) -> None:
    profile = store.get_profile(user_id)
    missing = tgt.missing_fields(profile)
    if missing:
        intro = ("I can work out your daily calories and macros. Tell me your goal (lose, "
                 "maintain or gain weight) and a bit about yourself.\n\n") if not profile else ""
        await msg.reply_text(intro + missing_profile_text(missing))
        return
    await msg.reply_text(tgt.profile_card(profile, tgt.calculate(profile)),
                         parse_mode=ParseMode.HTML)


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
    """The last 7 days (today included) as a line chart of complete days."""
    s = summary.build(store, user_id, store.today())
    if not s.complete:
        await msg.reply_text(summary.empty_text(s))
        return
    png = await asyncio.to_thread(summary.chart_png, s)  # drawing takes a moment on a Pi
    await msg.reply_photo(png, caption=summary.caption(s), parse_mode=ParseMode.HTML)


def weekly_text(store: Store, user_id: int) -> str:
    when = store.get_reminders(user_id, {"weekly": reminders.default_weekly()})["weekly"]
    state = f"on, Sundays at {reminders.fmt_time(when)}" if when else "off"
    return (f"📊 Weekly summary: {state}. It charts your last 7 finished days.\n"
            "Change it: “send my weekly summary at 6pm”, “turn off the weekly summary”.")


async def weekly_settings_reply(msg, store: Store, user_id: int, value: str = "") -> None:
    if value == "off":
        store.set_reminders(user_id, {"weekly": None})
    elif value == "on":
        store.set_reminders(user_id, {"weekly": reminders.default_weekly() or "09:00"})
    elif value:
        if not reminders.TIME_RE.match(value):
            await msg.reply_text("I didn't catch the time. Try e.g. “send my weekly summary at 6pm”.")
            return
        store.set_reminders(user_id, {"weekly": reminders.normalize_time(value)})
    await msg.reply_text(weekly_text(store, user_id))


async def help_reply(msg, store: Store, user_id: int) -> None:
    await msg.reply_text(HELP, parse_mode=ParseMode.HTML)


def reminders_text(store: Store, user_id: int) -> str:
    current = store.get_reminders(user_id, reminders.default_reminders())
    lines = ["⏰ <b>Meal reminders</b> (once a day, only if the meal isn't logged yet)"]
    for meal, when in current.items():
        state = reminders.fmt_time(when) if when else "off"
        lines.append(f"{MEAL_ICONS[meal]} {meal.capitalize()}: {state}")
    lines.append("\nChange one: “remind me about lunch at 1pm”, “no breakfast reminders”, "
                 "“turn off reminders”.")
    return "\n".join(lines)


async def reminders_reply(msg, store: Store, user_id: int) -> None:
    await msg.reply_text(reminders_text(store, user_id), parse_mode=ParseMode.HTML)


async def set_reminders_reply(msg, store: Store, user_id: int, meal: str, value: str) -> None:
    if value and value not in ("on", "off") and not reminders.TIME_RE.match(value):
        await msg.reply_text("I didn't catch the time. Try e.g. “remind me about lunch at 1pm”.")
        return
    if value:
        reminders.apply_change(store, user_id, meal, value)
    await reminders_reply(msg, store, user_id)


async def skip_meal_reply(msg, store: Store, user_id: int, meal: str) -> None:
    if meal not in MEALS:
        await msg.reply_text("Which meal did you skip?")
        return
    was_complete = day_complete(store, user_id)
    store.mark_reminder(user_id, store.today(), meal, "skipped")
    where = "snacks" if meal == "snack" else meal
    text = f"Okay, no {where} today. I won't remind you about it."
    if not was_complete and day_complete(store, user_id):
        text += "\n\n" + day_verdict(store, user_id)
    await msg.reply_text(text, parse_mode=ParseMode.HTML)


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
        "A Shortcut on your iPhone copies each finished day into Health: calories, protein, "
        "carbs and fat for each meal. It runs when you open Telegram, because iOS won't let "
        "anything write to Health while the phone is locked. Today's meals go in tomorrow, "
        "so anything you fix or undo today doesn't leave stray entries in Health.",
        "",
        "<b>Your personal link</b> (keep it private):",
        f"<code>{html.escape(link)}</code>",
        "It only works on your home Wi-Fi.",
        "",
        "<b>Make the Shortcut</b> (Shortcuts app → +), adding these actions in order:",
        "0. <b>Get Network Details</b>: Wi-Fi Network Name, then <b>If</b> Network Details "
        "is your home Wi-Fi name. Put everything below inside the If, and leave Otherwise "
        "empty, so away from home it quietly does nothing.",
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
        "<b>Run it automatically:</b> Shortcuts → Automation → + → App → Telegram → "
        "<i>Is Opened</i> → <i>Run Immediately</i> → pick this Shortcut. Running it often is "
        "fine: once a day is synced it's never sent again, and days missed while you're away "
        "catch up next time you open Telegram at home.",
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
                 "help": help_reply, "recipes": recipes_reply, "health": health_reply,
                 "reminders": reminders_reply, "weekly_settings": weekly_settings_reply,
                 "profile": show_profile_reply}


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

def configured_timezone() -> ZoneInfo:
    """TIMEZONE from .env, else the machine's own zone, else UTC."""
    name = os.environ.get("TIMEZONE", "").strip()
    if not name:
        try:  # /etc/localtime -> /usr/share/zoneinfo/Europe/Berlin
            link = os.path.realpath("/etc/localtime")
            name = link.split("zoneinfo/", 1)[1] if "zoneinfo/" in link else ""
        except OSError:
            name = ""
    try:
        return ZoneInfo(name or "UTC")
    except Exception:  # an unknown name: say so, and don't crash the bot
        log.warning("Unknown TIMEZONE %r; using UTC", name)
        return ZoneInfo("UTC")


def build_app() -> Application:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    tz = configured_timezone()
    db_path = os.environ.get("DB_PATH", "data/calories.db")
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    allowed = [int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x]

    store = Store(db_path, tz)

    async def start_reminders(application: Application) -> None:
        if allowed:
            application.bot_data["reminder_task"] = asyncio.create_task(
                reminders.reminder_loop(application, allowed))
            log.info("Meal reminders on for %d user(s)", len(allowed))

    async def stop_reminders(application: Application) -> None:
        task = application.bot_data.pop("reminder_task", None)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = (Application.builder().token(token).concurrent_updates(True)
           .post_init(start_reminders).post_shutdown(stop_reminders).build())
    app.bot_data["store"] = store
    app.bot_data["estimator"] = make_estimator()
    app.bot_data["memory"] = ChatMemory()

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
