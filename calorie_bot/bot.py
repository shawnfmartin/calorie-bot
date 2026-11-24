"""Telegram calorie tracker: message it what you ate, it keeps the daily tally."""

from __future__ import annotations

import asyncio
import contextlib
import html
import re
import logging
import os
import uuid
from collections import deque
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (Application, CommandHandler, ContextTypes, MessageHandler,
                          filters)

from .db import Entry, Recipe, Store
from .nutrition import EstimateError, make_estimator

log = logging.getLogger("calorie_bot")

HELP = """<b>Calorie tracker</b>
Just tell me what you ate, e.g. <i>2 eggs, toast with butter and a latte</i>.
You can also send a photo of your meal (add a caption for better accuracy).

/today – what you've eaten today
/week – daily totals for the last 7 days
/undo – remove the last meal you logged
/delete &lt;id&gt; – remove one item (ids are shown in /today)
/goal &lt;kcal&gt; – set your daily goal (/goal off to clear)

<b>Recipes</b>
/recipe &lt;name&gt; + ingredients on the next lines – save a recipe
/recipe &lt;name&gt; – show a saved recipe
/recipes – list your recipes
/renamerecipe &lt;old&gt; = &lt;new&gt; – rename one
/deleterecipe &lt;name&gt; – delete one
You can also just say “save a recipe for …” with the ingredients; I'll make up a name if you don't give one.
Then log portions like <i>37g dijon vinaigrette</i> or <i>1 serving of chili</i>.

/help – this message"""


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


# ---- handlers ---------------------------------------------------------------

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.HTML)


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


def remembering(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user_text: str):
    """Record the user's message; return (context before it, message that records replies)."""
    memory: ChatMemory = ctx.bot_data.setdefault("memory", ChatMemory())
    user_id = update.effective_user.id
    history = memory.lines(user_id)
    memory.add(user_id, "User", user_text)
    return history, RecordingMessage(update.effective_message, memory, user_id)


async def keep_typing(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Show "typing..." until cancelled (Telegram clears it after ~5s, estimates take longer)."""
    with contextlib.suppress(Exception):
        while True:
            await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(4)


async def log_meal(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = ctx.bot_data["store"]
    estimator = ctx.bot_data["estimator"]
    user_id = update.effective_user.id
    raw = update.effective_message
    text = raw.text or raw.caption
    history, msg = remembering(update, ctx, (text or "") + (" [photo]" if raw.photo else ""))

    image = None
    if msg.photo:
        tg_file = await msg.photo[-1].get_file()
        image = bytes(await tg_file.download_as_bytearray())

    typing = asyncio.create_task(keep_typing(ctx, msg.chat_id))
    try:
        recipes = store.list_recipes(user_id)
        result = await estimator.estimate(text=text, image=image,
                                          recipe_names=[recipe_label(r) for r in recipes],
                                          history=history)
    except EstimateError as e:
        await msg.reply_text(f"⚠️ {e}")
        return
    finally:
        typing.cancel()

    items = result.get("items") or []
    note = (result.get("note") or "").strip()
    recipe_out = result.get("recipe") or {}
    if result.get("intent") == "save_recipe" and recipe_out.get("ingredients"):
        await save_recipe_and_reply(msg, store, user_id, {**recipe_out, "note": note},
                                    source_text=text or "photo")
        return
    intent = result.get("intent")
    if intent == "update_recipe":
        await update_recipe_and_reply(msg, store, user_id, recipe_out)
        return
    if intent in ("rename_recipe", "show_recipe", "list_recipes", "delete_recipe"):
        await recipe_request_reply(msg, store, user_id, intent,
                                   (recipe_out.get("name") or "").strip(),
                                   (result.get("rename_to") or "").strip())
        return
    if not items:
        await msg.reply_text(note or "I couldn't find any food in that. Try e.g. "
                             "\"a turkey sandwich and an apple\".")
        return

    items, from_recipe, warnings = apply_recipes(store, user_id, items)
    store.add_items(user_id, text or "photo", items, meal_id=uuid.uuid4().hex)

    lines = ["🍽 <b>Logged</b>"]
    lines += [f"• {'📖 ' if used else ''}{html.escape(i['item'])} — {fmt_kcal(i['calories'])} kcal"
              for i, used in zip(items, from_recipe)]
    meal_total = sum(i["calories"] for i in items)
    if len(items) > 1:
        lines.append(f"<b>Meal:</b> {fmt_kcal(meal_total)} kcal")
    if note:
        lines.append(f"<i>{html.escape(note)}</i>")
    lines += [f"⚠️ {html.escape(w)}" for w in warnings]
    lines.append("")
    lines.append(day_summary(store, user_id))
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


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
            portion = recipe.portion(grams, servings, i.get("recipe_fraction") or 0)
            if portion is None and servings and not recipe.servings:
                warnings.append(f"“{recipe.name}” doesn't have a number of servings yet, so I "
                                f"estimated that one. Tell me e.g. “{recipe.name} makes 4 servings” "
                                "and I'll use it from now on, or give the amount in grams.")
        out.append(portion or i)
        used.append(portion is not None)
    return out, used, warnings


# ---- recipes ----------------------------------------------------------------

def recipe_label(r: Recipe) -> str:
    """How a saved recipe is described to Claude when it reads a meal."""
    servings = f"{r.servings:g} servings" if r.servings else "servings not set"
    return f"{r.name} (whole batch: {r.total_weight_g:g} g, {servings})"


def per_100g(r: Recipe) -> float:
    return r.calories / r.total_weight_g * 100 if r.total_weight_g else 0


def recipe_card(r: Recipe) -> str:
    lines = [f"📖 <b>{html.escape(r.name)}</b>"]
    for i in r.ingredients:
        lines.append(f"• {html.escape(i['ingredient'])} ({i['grams']:g} g) — {fmt_kcal(i['calories'])} kcal")
    lines.append("")
    lines.append(f"<b>Whole recipe:</b> {fmt_kcal(r.calories)} kcal, {r.total_weight_g:g} g")
    if r.total_weight_g:
        w = r.total_weight_g
        lines.append(f"<b>Per 100 g:</b> {fmt_kcal(per_100g(r))} kcal "
                     f"({r.calories / w:.2f} kcal/g) · P {r.protein_g / w * 100:.1f}g · "
                     f"C {r.carbs_g / w * 100:.1f}g · F {r.fat_g / w * 100:.1f}g")
    if r.servings:
        lines.append(f"<b>Per serving</b> (makes {r.servings:g}): {fmt_small_kcal(r.calories / r.servings)} kcal")
    return "\n".join(lines)


def split_recipe_message(text: str) -> tuple[str, str]:
    """'/recipe Name\n<ingredients>' or '/recipe Name: <ingredients>' -> (name, ingredients)."""
    first, _, rest = text.partition("\n")
    first = first.split(maxsplit=1)[1] if len(first.split(maxsplit=1)) > 1 else ""  # drop /recipe
    if not rest.strip() and ":" in first:
        first, _, rest = first.partition(":")
    name, body = first.strip().strip(":").strip(), rest.strip()
    if AMOUNT_RE.search(name):
        name, body = "", f"{name}\n{body}".strip()
    return name, body


SERVINGS_RE = re.compile(
    r"\b(?:serves|yields?|makes)\s+(\d+(?:\.\d+)?)(?![\d.])"
    r"(?!\s*(?:g|kg|grams?|ml|l|liters?|oz|lbs?|pounds?|cups?|quarts?)\b)"
    r"|\b(\d+(?:\.\d+)?)\s*(?:servings?|portions?)\b", re.I)


def servings_in(text: str) -> float:
    """'Totaling 107 servings', 'serves 4', 'makes 6 portions' -> the number; 0 if none."""
    m = SERVINGS_RE.search(text or "")
    return float(m.group(1) or m.group(2)) if m else 0


AMOUNT_RE = re.compile(r"\d\s*(g|kg|mg|ml|l|oz|lbs?|cups?|tbsp|tsp|grams?)\b", re.I)


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
                                         result.get("final_weight_g") or 0, servings, source_text)
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
                     f"To change it: /renamerecipe {html.escape(recipe.name)} = New name")
    example = max(5, round(recipe.total_weight_g / 4 / 5) * 5)  # about a quarter of the batch
    lines.append(f"\nLog a portion like: <i>{example:g}g of {html.escape(recipe.name.lower())}</i>")
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_recipe(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = ctx.bot_data["store"]
    estimator = ctx.bot_data["estimator"]
    user_id = update.effective_user.id
    _, msg = remembering(update, ctx, update.effective_message.text or "")
    name, body = split_recipe_message(msg.text or "")

    if not name and not body:
        await msg.reply_text(
            "To save a recipe, put its name after /recipe and the ingredients on the next lines:\n\n"
            "/recipe Dijon vinaigrette\nDijon mustard 20 g\nOlive oil 5 g\n...\n\n"
            "Add the finished weight or the number of servings if you know them.")
        return
    if name and not body:
        recipe = store.get_recipe(user_id, name)
        if recipe:
            await msg.reply_text(recipe_card(recipe), parse_mode=ParseMode.HTML)
        else:
            await msg.reply_text(f"No saved recipe called “{name}”. /recipes lists them; "
                                 "to save it, put the ingredients on the lines after the name.")
        return

    typing = asyncio.create_task(keep_typing(ctx, msg.chat_id))
    try:
        result = await estimator.estimate_recipe(
            body, name=name, recipe_names=[r.name for r in store.list_recipes(user_id)])
    except EstimateError as e:
        await msg.reply_text(f"⚠️ {e}")
        return
    finally:
        typing.cancel()
    await save_recipe_and_reply(msg, store, user_id, result, source_text=body, explicit_name=name)


async def cmd_recipes(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = ctx.bot_data["store"]
    _, msg = remembering(update, ctx, update.effective_message.text or "/recipes")
    await msg.reply_text(recipes_list_text(store, update.effective_user.id),
                         parse_mode=ParseMode.HTML)


async def update_recipe_and_reply(msg, store: Store, user_id: int, recipe_out: dict) -> None:
    name = (recipe_out.get("name") or "").strip()
    servings = recipe_out.get("servings") or 0
    weight = recipe_out.get("final_weight_g") or 0
    recipe = store.get_recipe(user_id, name) if name else None
    if recipe is None:
        names = ", ".join(r.name for r in store.list_recipes(user_id)) or "none yet"
        await msg.reply_text(f"I couldn't tell which recipe you meant. Your recipes: {names}.")
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
        return ("No saved recipes yet. Send the ingredients and say “save this recipe”, "
                "or use /recipe.")
    lines = ["📖 <b>Your recipes</b>"]
    for r in recipes:
        serving = f", {fmt_small_kcal(r.calories / r.servings)} per serving" if r.servings else ""
        lines.append(f"• {html.escape(r.name)} — {fmt_kcal(per_100g(r))} kcal/100 g{serving}")
    lines.append("\nAsk to see one, e.g. “show the vinaigrette”.")
    return "\n".join(lines)


async def recipe_request_reply(msg, store: Store, user_id: int, intent: str,
                               name: str, new_name: str) -> None:
    if intent == "list_recipes":
        await msg.reply_text(recipes_list_text(store, user_id), parse_mode=ParseMode.HTML)
        return
    recipe = store.get_recipe(user_id, name) if name else None
    if recipe is None:
        names = ", ".join(r.name for r in store.list_recipes(user_id)) or "none yet"
        await msg.reply_text(f"I couldn't tell which recipe you meant. Your recipes: {names}.")
        return
    if intent == "show_recipe":
        await msg.reply_text(recipe_card(recipe), parse_mode=ParseMode.HTML)
    elif intent == "delete_recipe":
        # Deleting is permanent, so a plain message only offers it; the command does it.
        await msg.reply_text(f"To delete “{recipe.name}”, send:\n/deleterecipe {recipe.name}")
    elif intent == "rename_recipe":
        if not new_name:
            await msg.reply_text(f"What should I rename “{recipe.name}” to?")
            return
        result = store.rename_recipe(user_id, recipe.name, new_name)
        replies = {"ok": f"✅ Renamed “{recipe.name}” to “{new_name}”.",
                   "taken": f"You already have a recipe called “{new_name}”. "
                            "Delete or rename that one first."}
        await msg.reply_text(replies.get(result, f"No saved recipe called “{recipe.name}”."))


async def cmd_renamerecipe(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = ctx.bot_data["store"]
    old, sep, new = " ".join(ctx.args).partition("=")
    old, new = old.strip(), new.strip()
    if not (sep and old and new):
        await update.effective_message.reply_text("Usage: /renamerecipe Old name = New name")
        return
    result = store.rename_recipe(update.effective_user.id, old, new)
    replies = {"ok": f"Renamed “{old}” to “{new}”.",
               "missing": f"No saved recipe called “{old}”.",
               "taken": f"You already have a recipe called “{new}”. Delete or rename that one first."}
    await update.effective_message.reply_text(replies[result])


async def cmd_deleterecipe(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = ctx.bot_data["store"]
    name = " ".join(ctx.args)
    if not name:
        await update.effective_message.reply_text("Usage: /deleterecipe <name>")
    elif store.delete_recipe(update.effective_user.id, name):
        await update.effective_message.reply_text(f"🗑 Deleted the recipe “{name}”. "
                                                  "Meals you already logged with it stay as they are.")
    else:
        await update.effective_message.reply_text(f"No saved recipe called “{name}”.")


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = ctx.bot_data["store"]
    user_id = update.effective_user.id
    entries = store.entries_for_day(user_id, store.today())
    if not entries:
        await update.effective_message.reply_text("Nothing logged today yet.")
        return
    lines = [f"<b>{store.today():%A %b %-d}</b>"]
    for e in entries:
        lines.append(f"<code>{e.id:>4}</code> {e.eaten_at:%H:%M} {html.escape(e.item)}"
                     f" — {fmt_kcal(e.calories)}")
    lines += ["", day_summary(store, user_id), macro_line(entries)]
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = ctx.bot_data["store"]
    user_id = update.effective_user.id
    days = store.totals_by_day(user_id, 7)
    goal = store.get_goal(user_id)
    scale = max([goal or 0] + [t for _, t in days]) or 1
    lines = ["<b>Last 7 days</b>", "<pre>"]
    for d, total in days:
        bar = "█" * round(12 * total / scale)
        lines.append(f"{d:%a %d} {total:>6,} {bar}")
    lines.append("</pre>")
    logged = [t for _, t in days if t]
    if logged:
        lines.append(f"Average (days logged): {fmt_kcal(sum(logged) / len(logged))} kcal")
    if goal:
        lines.append(f"Goal: {fmt_kcal(goal)} kcal")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = ctx.bot_data["store"]
    removed = store.undo_last_meal(update.effective_user.id)
    if not removed:
        await update.effective_message.reply_text("Nothing to undo.")
        return
    names = ", ".join(html.escape(e.item) for e in removed)
    kcal = sum(e.calories for e in removed)
    await update.effective_message.reply_text(
        f"↩️ Removed {names} ({fmt_kcal(kcal)} kcal)\n\n"
        f"{day_summary(store, update.effective_user.id)}", parse_mode=ParseMode.HTML)


async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = ctx.bot_data["store"]
    if not ctx.args or not ctx.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /delete <id> (ids are in /today)")
        return
    entry = store.delete_entry(update.effective_user.id, int(ctx.args[0]))
    if entry is None:
        await update.effective_message.reply_text("No entry with that id.")
        return
    await update.effective_message.reply_text(
        f"🗑 Removed {html.escape(entry.item)} ({fmt_kcal(entry.calories)} kcal)\n\n"
        f"{day_summary(store, update.effective_user.id)}", parse_mode=ParseMode.HTML)


async def cmd_goal(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = ctx.bot_data["store"]
    user_id = update.effective_user.id
    arg = ctx.args[0].replace(",", "") if ctx.args else ""
    if arg.lower() in ("off", "none", "clear"):
        store.set_goal(user_id, None)
        await update.effective_message.reply_text("Daily goal cleared.")
    elif arg.isdigit() and 500 <= int(arg) <= 10000:
        store.set_goal(user_id, int(arg))
        await update.effective_message.reply_text(
            f"Daily goal set to {fmt_kcal(int(arg))} kcal.\n\n{day_summary(store, user_id)}",
            parse_mode=ParseMode.HTML)
    else:
        goal = store.get_goal(user_id)
        current = f"Current goal: {fmt_kcal(goal)} kcal. " if goal else ""
        await update.effective_message.reply_text(f"{current}Usage: /goal 2000 (or /goal off)")


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

    app.add_handler(CommandHandler(["start", "help"], cmd_help, filters=me))
    app.add_handler(CommandHandler("today", cmd_today, filters=me))
    app.add_handler(CommandHandler("week", cmd_week, filters=me))
    app.add_handler(CommandHandler("undo", cmd_undo, filters=me))
    app.add_handler(CommandHandler("delete", cmd_delete, filters=me))
    app.add_handler(CommandHandler("goal", cmd_goal, filters=me))
    app.add_handler(CommandHandler("recipe", cmd_recipe, filters=me))
    app.add_handler(CommandHandler("recipes", cmd_recipes, filters=me))
    app.add_handler(CommandHandler("deleterecipe", cmd_deleterecipe, filters=me))
    app.add_handler(CommandHandler("renamerecipe", cmd_renamerecipe, filters=me))
    app.add_handler(MessageHandler(me & (filters.TEXT | filters.PHOTO) & ~filters.COMMAND, log_meal))
    app.add_handler(MessageHandler(~me, unauthorized))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log the bot token in URLs
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
