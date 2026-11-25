import asyncio
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic
import httpx2 as httpx
import pytest

from calorie_bot.db import Store
from calorie_bot.nutrition import EstimateError, ApiEstimator

TZ = ZoneInfo("America/Los_Angeles")


def item(name, kcal):
    return {"item": name, "calories": kcal, "protein_g": 1, "carbs_g": 2, "fat_g": 3}


def test_store_daily_totals_goal_undo_delete(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    now = datetime.now(TZ)
    s.add_items(1, "breakfast", [item("eggs", 180), item("toast", 80)], "m1", now)
    s.add_items(1, "lunch", [item("sandwich", 450)], "m2", now)
    s.add_items(2, "other user", [item("pizza", 900)], "m3", now)
    s.add_items(1, "yesterday", [item("pasta", 700)], "m4", now - timedelta(days=1))

    today = s.entries_for_day(1, s.today())
    assert sum(e.calories for e in today) == 710
    assert [t for _, t in s.totals_by_day(1, 2)] == [700, 710]

    s.set_goal(1, 2000)
    assert s.get_goal(1) == 2000
    s.set_goal(1, None)
    assert s.get_goal(1) is None

    removed = s.undo_last_meal(1)
    assert [e.item for e in removed] == ["pasta"]  # last *inserted* meal
    eggs = next(e for e in today if e.item == "eggs")
    assert s.delete_entry(2, eggs.id) is None       # can't delete another user's entry
    assert s.delete_entry(1, eggs.id).item == "eggs"
    assert sum(e.calories for e in s.entries_for_day(1, s.today())) == 530


def test_day_boundary_uses_local_timezone(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    day = s.today()
    late = datetime.combine(day, datetime.min.time(), TZ) - timedelta(minutes=1)  # 23:59 yesterday
    s.add_items(1, "midnight snack", [item("chips", 150)], "m1", late)
    assert s.entries_for_day(1, day) == []
    assert len(s.entries_for_day(1, day - timedelta(days=1))) == 1


def fake_client(handler):
    return anthropic.AsyncAnthropic(api_key="test", max_retries=0,
                                    http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def message_response(payload, stop_reason="end_turn"):
    return httpx.Response(200, json={
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10}})


def test_estimator_request_shape_and_parse():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        seen["beta"] = req.headers.get("anthropic-beta")
        return message_response({"items": [item("2 eggs", 156)], "note": ""})

    est = ApiEstimator(fake_client(handler))
    out = asyncio.run(est.estimate(text="2 eggs", image=b"\xff\xd8fake"))
    assert out["items"][0]["calories"] == 156
    body = seen["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen["beta"]
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert [c["type"] for c in body["messages"][0]["content"]] == ["image", "text"]


def test_estimator_refusal_and_auth_errors():
    est = ApiEstimator(fake_client(lambda r: message_response({}, "refusal")))
    with pytest.raises(EstimateError, match="declined"):
        asyncio.run(est.estimate(text="x"))

    est = ApiEstimator(fake_client(lambda r: httpx.Response(
        401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad"}})))
    with pytest.raises(EstimateError, match="API key"):
        asyncio.run(est.estimate(text="x"))


def test_build_app_registers_handlers(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("ALLOWED_USER_IDS", "42, 43")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "c.db"))
    from calorie_bot.bot import build_app
    app = build_app()
    assert len(app.handlers[0]) == 2  # one router for everything, plus the private-bot reply


# ---- Claude Code backend, against a fake `claude` executable ---------------------------

from calorie_bot.nutrition import ClaudeCodeEstimator  # noqa: E402

FAKE_CLAUDE = r"""#!/bin/sh
# Records what it was called with, then prints a canned `claude -p --output-format json` result.
{
  echo "ARGS: $*"
  echo "STDIN: $(cat)"
  echo "APIKEY: ${ANTHROPIC_API_KEY:-unset}"
  echo "FILES: $(ls)"
} > "$FAKE_LOG"
printf '%s' "$FAKE_OUTPUT"
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    bin_path = tmp_path / "claude"
    bin_path.write_text(FAKE_CLAUDE)
    bin_path.chmod(0o755)
    log_path = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_LOG", str(log_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")

    def run(output: dict, **kwargs):
        monkeypatch.setenv("FAKE_OUTPUT", json.dumps(output))
        est = ClaudeCodeEstimator(claude_bin=str(bin_path), model="")
        result = asyncio.run(est.estimate(**kwargs))
        return result, log_path.read_text()
    return run


def test_claude_code_text_meal(fake_claude):
    parsed = {"items": [item("2 eggs", 156)], "note": ""}
    result, calls = fake_claude({"is_error": False, "structured_output": parsed},
                                text="-2 eggs")
    assert result == parsed
    assert "STDIN: -2 eggs" in calls            # prompt via stdin, never as a CLI arg
    assert "APIKEY: unset" in calls             # subscription billing, not the API key
    assert "--json-schema" in calls and "--tools  " in calls  # no tools for text


def test_claude_code_photo_meal(fake_claude):
    parsed = {"items": [item("banana", 105)], "note": ""}
    _, calls = fake_claude({"is_error": False, "structured_output": parsed},
                           text=None, image=b"\xff\xd8jpeg")
    assert "FILES: meal.jpg" in calls
    assert "--tools Read" in calls and "./meal.jpg" in calls


def test_claude_code_errors(fake_claude):
    with pytest.raises(EstimateError, match="logged in"):
        fake_claude({"is_error": True, "result": "Not logged in · Please run /login"}, text="x")
    with pytest.raises(EstimateError, match="couldn't estimate"):
        fake_claude({"is_error": False, "result": "no schema output"}, text="x")


def test_claude_code_missing_binary():
    est = ClaudeCodeEstimator(claude_bin="/nonexistent/claude")
    with pytest.raises(EstimateError, match="isn't installed"):
        asyncio.run(est.estimate(text="x"))


# ---- recipes -----------------------------------------------------------------------

from calorie_bot.bot import apply_recipes, recipe_card  # noqa: E402

VINAIGRETTE = [  # 120 g, 92 kcal
    {"ingredient": "Dijon mustard", "grams": 20, "calories": 15, "protein_g": 1, "carbs_g": 1, "fat_g": 1},
    {"ingredient": "Cider vinegar", "grams": 30, "calories": 6, "protein_g": 0, "carbs_g": 0, "fat_g": 0},
    {"ingredient": "Water", "grams": 55, "calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0},
    {"ingredient": "Olive oil", "grams": 5, "calories": 44, "protein_g": 0, "carbs_g": 0, "fat_g": 5},
    {"ingredient": "Honey", "grams": 7, "calories": 21, "protein_g": 0, "carbs_g": 6, "fat_g": 0},
    {"ingredient": "Garlic, salt, pepper", "grams": 3, "calories": 6, "protein_g": 0, "carbs_g": 1, "fat_g": 0},
]


def test_recipe_save_lookup_portion_and_replace(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    r, replaced = s.save_recipe(1, "Dijon vinaigrette", VINAIGRETTE, 0, 0, "src")
    assert not replaced and r.total_weight_g == 120 and r.calories == 92
    assert s.get_recipe(1, "  dijon   VINAIGRETTE ").id == r.id       # case/space-insensitive
    assert s.get_recipe(2, "dijon vinaigrette") is None               # per user

    p = r.portion(grams=37)
    assert round(p["calories"], 1) == round(92 * 37 / 120, 1)          # ~28.4 kcal
    assert p["item"] == "Dijon vinaigrette, 37 g"
    assert r.portion(servings=1) is None                               # no servings known

    r2, replaced = s.save_recipe(1, "dijon vinaigrette", VINAIGRETTE, 0, 4, "src2")
    assert replaced and r2.id == r.id and r2.portion(servings=1)["calories"] == 23
    assert [x.name for x in s.list_recipes(1)] == ["dijon vinaigrette"]
    assert s.delete_recipe(1, "Dijon Vinaigrette") and s.list_recipes(1) == []


def test_recipe_final_weight_overrides_ingredient_sum(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    r, _ = s.save_recipe(1, "Reduced sauce", VINAIGRETTE, 60, 0, "src")
    assert r.total_weight_g == 60 and r.portion(grams=30)["calories"] == 46


def test_apply_recipes_swaps_in_exact_numbers(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    s.save_recipe(1, "Dijon vinaigrette", VINAIGRETTE, 0, 0, "src")
    items = [
        {**item("dijon dressing", 50), "recipe": "Dijon vinaigrette", "recipe_grams": 37, "recipe_servings": 0},
        {**item("salad greens", 15), "recipe": "", "recipe_grams": 0, "recipe_servings": 0},
        {**item("mystery sauce", 80), "recipe": "Not saved", "recipe_grams": 10, "recipe_servings": 0},
    ]
    out, used, warnings = apply_recipes(s, 1, items)
    assert warnings == []
    assert used == [True, False, False]
    assert round(out[0]["calories"], 1) == 28.4 and out[0]["item"] == "Dijon vinaigrette, 37 g"
    assert out[1]["calories"] == 15 and out[2]["calories"] == 80     # unknown recipe: keep estimate
    s.add_items(1, "lunch", out, "m1")                                 # stores cleanly
    assert sum(e.calories for e in s.entries_for_day(1, s.today())) == 28 + 15 + 80


def test_recipe_card_renders(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    r, _ = s.save_recipe(1, "Dijon <b>vinaigrette", VINAIGRETTE, 0, 4, "src")
    card = recipe_card(r)
    assert "&lt;b&gt;" in card and "Per 100 g:</b> 77 kcal (0.77 kcal/g)" in card
    assert "makes 4): 23 kcal" in card


def test_meal_prompt_lists_saved_recipes(fake_claude):
    parsed = {"items": [], "note": "hi"}
    _, calls = fake_claude({"is_error": False, "structured_output": parsed}, text="37g dijon with lunch",
                           recipe_names=["Dijon vinaigrette", "Chili"])
    assert "My saved recipes: Dijon vinaigrette; Chili" in calls


# ---- saving recipes from plain messages, made-up titles, rename ----------------------

from calorie_bot.bot import free_name, save_recipe_and_reply  # noqa: E402


class FakeMsg:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


def recipe_result(name="", name_given=False, note=""):
    return {"name": name, "name_given": name_given, "ingredients": VINAIGRETTE,
            "final_weight_g": 0, "servings": 0, "note": note}


def test_made_up_title_never_overwrites(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    msg = FakeMsg()
    asyncio.run(save_recipe_and_reply(msg, s, 1, recipe_result("Honey dijon vinaigrette"), "src"))
    asyncio.run(save_recipe_and_reply(msg, s, 1, recipe_result("Honey dijon vinaigrette"), "src"))
    assert [r.name for r in s.list_recipes(1)] == ["Honey dijon vinaigrette", "Honey dijon vinaigrette 2"]
    assert "I called it “Honey dijon vinaigrette 2”" in msg.replies[1]
    assert "rename it to" in msg.replies[1] and not re.search(r"(^|\s)/\w", msg.replies[1])
    assert free_name(s, 1, "Chili") == "Chili"


def test_named_save_updates_and_says_what_it_was(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    msg = FakeMsg()
    asyncio.run(save_recipe_and_reply(msg, s, 1, recipe_result("Vinaigrette", True), "src"))
    smaller = [dict(i, calories=i["calories"] / 2) for i in VINAIGRETTE]
    asyncio.run(save_recipe_and_reply(msg, s, 1, {**recipe_result("vinaigrette", True),
                                                  "ingredients": smaller}, "src2"))
    assert len(s.list_recipes(1)) == 1
    assert "Updated your recipe (it was 92 kcal for 120 g)" in msg.replies[1]
    assert "I called it" not in msg.replies[1]


def test_explicit_name_wins_and_empty_ingredients(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    msg = FakeMsg()
    asyncio.run(save_recipe_and_reply(msg, s, 1, recipe_result("Claude's idea", True), "src",
                                      explicit_name="House dressing"))
    assert [r.name for r in s.list_recipes(1)] == ["House dressing"]
    asyncio.run(save_recipe_and_reply(msg, s, 1, {**recipe_result(), "ingredients": [],
                                                  "note": "No ingredients there."}, "src"))
    assert msg.replies[-1] == "No ingredients there."


def test_rename_recipe(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    s.save_recipe(1, "Honey dijon vinaigrette", VINAIGRETTE, 0, 0, "src")
    s.save_recipe(1, "Chili", VINAIGRETTE, 0, 0, "src")
    assert s.rename_recipe(1, "nope", "x") == "missing"
    assert s.rename_recipe(1, "chili", "Honey Dijon Vinaigrette") == "taken"
    assert s.rename_recipe(1, "honey dijon vinaigrette", "Dijon vinaigrette") == "ok"
    assert s.rename_recipe(1, "dijon vinaigrette", "Dijon Vinaigrette") == "ok"   # case-only rename
    assert sorted(r.name for r in s.list_recipes(1)) == ["Chili", "Dijon Vinaigrette"]


# ---- servings and batch fractions ---------------------------------------------------

from calorie_bot.bot import update_recipe_and_reply  # noqa: E402


def recipe_item(**amounts):
    base = {"recipe": "Dijon vinaigrette", "recipe_grams": 0, "recipe_servings": 0, "recipe_fraction": 0}
    return {**item("dressing", 50), **base, **amounts}


def test_fraction_and_servings_portions(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    r, _ = s.save_recipe(1, "Dijon vinaigrette", VINAIGRETTE, 0, 0, "src")   # 92 kcal, 120 g
    assert r.portion(fraction=0.5) == {**r.portion(grams=60), "item": "Dijon vinaigrette, half the batch"}
    assert r.portion(fraction=1)["item"] == "Dijon vinaigrette, the whole batch"
    assert r.portion(fraction=0.333)["item"] == "Dijon vinaigrette, a third of the batch"
    assert r.portion(fraction=0.4)["item"] == "Dijon vinaigrette, 40% of the batch"


def test_servings_missing_warns_then_update_fixes_it(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    s.save_recipe(1, "Dijon vinaigrette", VINAIGRETTE, 0, 0, "src")
    out, used, warnings = apply_recipes(s, 1, [recipe_item(recipe_servings=1)])
    assert used == [False] and out[0]["calories"] == 50              # kept Claude's estimate
    assert "doesn't have a number of servings yet" in warnings[0]

    msg = FakeMsg()
    asyncio.run(update_recipe_and_reply(msg, s, 1, {"name": "Dijon vinaigrette", "servings": 4,
                                                    "final_weight_g": 0, "ingredients": []}))
    assert "makes 4 servings" in msg.replies[0]
    out, used, warnings = apply_recipes(s, 1, [recipe_item(recipe_servings=1)])
    assert used == [True] and out[0]["calories"] == 23 and warnings == []
    assert s.get_recipe(1, "dijon vinaigrette").total_weight_g == 120   # weight untouched


def test_update_recipe_weight_and_unknown_name(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    s.save_recipe(1, "Chili", VINAIGRETTE, 0, 0, "src")
    msg = FakeMsg()
    asyncio.run(update_recipe_and_reply(msg, s, 1, {"name": "Chili", "servings": 0,
                                                    "final_weight_g": 60, "ingredients": []}))
    assert s.get_recipe(1, "chili").portion(grams=30)["calories"] == 46
    asyncio.run(update_recipe_and_reply(msg, s, 1, {"name": "Soup", "servings": 2,
                                                    "final_weight_g": 0, "ingredients": []}))
    assert "Your recipes: Chili" in msg.replies[-1]


def test_recipe_label(tmp_path):
    from calorie_bot.bot import recipe_label
    s = Store(tmp_path / "t.db", TZ)
    r, _ = s.save_recipe(1, "Vinaigrette", VINAIGRETTE, 0, 0, "src")
    assert recipe_label(r) == "Vinaigrette (whole batch: 120 g, servings not set)"
    r = s.update_recipe_size(1, "Vinaigrette", servings=4)
    assert recipe_label(r) == "Vinaigrette (whole batch: 120 g, 4 servings)"


# ---- context, plain-language recipe requests, servings backup ------------------------

from calorie_bot.bot import ChatMemory, RecordingMessage, servings_in  # noqa: E402
from calorie_bot.nutrition import meal_prompt  # noqa: E402


@pytest.mark.parametrize("text,expected", [
    ("Salt 1 g\n\nTotaling 107 servings", 107), ("serves 4", 4), ("Makes 6 portions", 6),
    ("makes 12", 12), ("yields 2.5 servings", 2.5), ("makes 1.2 kg", 0), ("makes 2 cups", 0),
    ("Dijon mustard 20 g", 0),
])
def test_servings_in(text, expected):
    assert servings_in(text) == expected


def test_servings_backup_when_claude_misses_it(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    asyncio.run(save_recipe_and_reply(FakeMsg(), s, 1, recipe_result("Vinaigrette", True),
                                      "Dijon mustard 20 g\n...\nTotaling 107 servings"))
    r = s.get_recipe(1, "vinaigrette")
    assert r.servings == 107
    assert round(r.portion(servings=33)["calories"], 1) == round(92 * 33 / 107, 1)


def test_chat_memory_and_recording_message():
    mem = ChatMemory(max_lines=3, max_chars=20)
    msg = RecordingMessage(FakeMsg(), mem, 1)
    mem.add(1, "User", "save my vinaigrette")
    asyncio.run(msg.reply_text("✅ Saved <b>Vinaigrette</b> &amp; more text that is long"))
    mem.add(1, "User", "rename it")
    mem.add(1, "User", "4th line")
    assert mem.lines(1) == ["Bot: ✅ Saved Vinaigrette …", "User: rename it", "User: 4th line"]
    assert mem.lines(2) == []
    assert msg.replies  # forwarded to the real message (via __getattr__)


def test_meal_prompt_includes_history():
    p = meal_prompt("Rename to Dijon vinegrette", ["Vinaigrette (whole batch: 104 g, 107 servings)"],
                    ["User: Make vinaigrette recipe for ...", "Bot: ✅ Saved recipe Vinaigrette"])
    assert p.startswith("Recent messages, oldest first")
    assert "My latest message:\nRename to Dijon vinegrette" in p
    assert p.endswith("My saved recipes: Vinaigrette (whole batch: 104 g, 107 servings)")


# ---- the plain-language router ------------------------------------------------------

from types import SimpleNamespace as NS  # noqa: E402

from calorie_bot.bot import handle_message, normalize, quick_intent  # noqa: E402

EMPTY_RECIPE = {"name": "", "name_given": False, "ingredients": [], "final_weight_g": 0, "servings": 0}


def claude_says(intent, **fields):
    return {"intent": intent, "items": [], "entry_ids": [], "goal_kcal": 0,
            "recipe": dict(EMPTY_RECIPE), "rename_to": "", "note": "", **fields}


class ScriptedEstimator:
    """Stands in for Claude: returns queued results and records what it was asked."""

    def __init__(self):
        self.queue, self.calls = [], []

    async def estimate(self, **kwargs):
        self.calls.append(kwargs)
        return self.queue.pop(0)


class Chat:
    """Drives handle_message the way Telegram would, one user, one chat."""

    def __init__(self, tmp_path):
        self.store = Store(tmp_path / "t.db", TZ)
        self.claude = ScriptedEstimator()
        self.ctx = NS(bot_data={"store": self.store, "estimator": self.claude}, bot=self)
        self.replies, self.photos = [], []

    async def send_chat_action(self, *a):
        pass

    def say(self, text, then=None):
        if then is not None:
            self.claude.queue.append(then)
        chat = self

        class Msg:
            photo, caption, chat_id = None, None, 1

            async def reply_text(self, t, **k):
                chat.replies.append(t)

            async def reply_photo(self, photo, caption="", **k):
                chat.photos.append(photo)
                chat.replies.append(caption)

        m = Msg()
        m.text = text
        asyncio.run(handle_message(NS(effective_message=m, effective_user=NS(id=1)), self.ctx))
        return self.replies[-1]


@pytest.mark.parametrize("text,intent", [
    ("today", "today"), ("/today", "today"), ("What did I eat today?", "today"),
    ("/week@my_calorie_bot", "week"), ("Undo", "undo"), ("/start", "help"),
    ("my recipes", "recipes"), ("2 eggs", None), ("today I had 2 eggs", None),
])
def test_quick_intents(text, intent):
    assert quick_intent(text) == intent


def test_normalize():
    assert normalize("/Today@my_bot!") == "today"


def test_quick_replies_skip_claude(tmp_path):
    chat = Chat(tmp_path)
    assert "Nothing logged today" in chat.say("today")
    assert "No complete days" in chat.say("/week")
    assert "Just talk to me" in chat.say("/start")
    assert "/" not in chat.say("help").replace("</", "")   # help mentions no commands
    assert chat.claude.calls == []


def test_log_then_correct_then_remove(tmp_path):
    chat = Chat(tmp_path)
    reply = chat.say("3 eggs", claude_says("log_meal", items=[{**item("3 large eggs", 215), "recipe": "",
                     "recipe_grams": 0, "recipe_servings": 0, "recipe_fraction": 0}]))
    assert "Logged" in reply and "215" in reply
    eggs = chat.store.entries_for_day(1, chat.store.today())[0]
    assert chat.claude.calls[-1]["today_log"] == []

    chat.say("actually it was 2 eggs", claude_says("correct_log", entry_ids=[eggs.id], items=[{
        **item("2 large eggs", 143), "recipe": "", "recipe_grams": 0, "recipe_servings": 0,
        "recipe_fraction": 0}]))
    assert f"#{eggs.id}" in chat.claude.calls[-1]["today_log"][0]          # Claude saw the log
    assert "Fixed" in chat.replies[-1] and "<s>3 large eggs" in chat.replies[-1]
    [fixed] = chat.store.entries_for_day(1, chat.store.today())
    assert fixed.item == "2 large eggs" and fixed.eaten_at == eggs.eaten_at  # kept the time

    reply = chat.say("remove the eggs", claude_says("delete_entries", entry_ids=[fixed.id]))
    assert "Removed" in reply and chat.store.entries_for_day(1, chat.store.today()) == []
    assert "couldn't tell which entry" in chat.say("remove the toast",
                                                   claude_says("delete_entries", entry_ids=[999]))


def test_goal_in_plain_words(tmp_path):
    chat = Chat(tmp_path)
    assert "don't have a daily goal" in chat.say("what's my goal", claude_says("set_goal"))
    assert "2,000" in chat.say("set my goal to 2000", claude_says("set_goal", goal_kcal=2000))
    assert chat.store.get_goal(1) == 2000
    assert "looks off" in chat.say("goal 20", claude_says("set_goal", goal_kcal=20))
    assert "cleared" in chat.say("no more goal", claude_says("set_goal", goal_kcal=-1))
    assert chat.store.get_goal(1) is None


def test_recipe_rename_show_list_in_plain_words(tmp_path):
    chat = Chat(tmp_path)
    chat.store.save_recipe(1, "Vinaigrette", VINAIGRETTE, 0, 107, "src")
    named = lambda n: {**EMPTY_RECIPE, "name": n}
    assert chat.say("rename to dijon vinegrette", claude_says(
        "rename_recipe", recipe=named("Vinaigrette"), rename_to="Dijon vinaigrette")) == \
        "✅ Renamed “Vinaigrette” to “Dijon vinaigrette”."
    assert "📖 <b>Dijon vinaigrette</b>" in chat.say("show it", claude_says("show_recipe",
                                                                          recipe=named("Dijon vinaigrette")))
    assert "Dijon vinaigrette" in chat.say("recipes")
    assert "couldn't tell which recipe" in chat.say("show soup", claude_says("show_recipe",
                                                                           recipe=named("Soup")))
    # The rename reply was remembered, so Claude gets it as context next time.
    assert any("Renamed" in line for line in chat.claude.calls[-1]["history"])


def test_recipe_delete_needs_yes(tmp_path):
    chat = Chat(tmp_path)
    chat.store.save_recipe(1, "Chili", VINAIGRETTE, 0, 0, "src")
    ask = claude_says("delete_recipe", recipe={**EMPTY_RECIPE, "name": "Chili"})
    assert "Reply yes to delete" in chat.say("delete the chili recipe", ask)
    assert "kept “Chili”" in chat.say("no")
    assert chat.store.get_recipe(1, "chili") is not None

    chat.say("delete the chili recipe", dict(ask))
    assert "Deleted the recipe “Chili”" in chat.say("yes")
    assert chat.store.get_recipe(1, "chili") is None
    assert len(chat.claude.calls) == 2                       # "yes" and "no" never went to Claude


def test_unrelated_message_cancels_pending_delete(tmp_path):
    chat = Chat(tmp_path)
    chat.store.save_recipe(1, "Chili", VINAIGRETTE, 0, 0, "src")
    chat.say("delete chili", claude_says("delete_recipe", recipe={**EMPTY_RECIPE, "name": "Chili"}))
    chat.say("today")                                         # something else in between
    chat.say("yes", claude_says("other", note="Yes to what?"))
    assert chat.store.get_recipe(1, "chili") is not None      # "yes" no longer deletes


# ---- meals ------------------------------------------------------------------------

import sqlite3  # noqa: E402

from calorie_bot.db import meal_for_time, meal_in_text  # noqa: E402


def food(name, kcal):
    return {**item(name, kcal), "recipe": "", "recipe_grams": 0, "recipe_servings": 0,
            "recipe_fraction": 0}


@pytest.mark.parametrize("hour,meal", [(3, "snack"), (7, "breakfast"), (12, "lunch"),
                                       (18, "dinner"), (23, "snack")])
def test_meal_for_time(hour, meal):
    assert meal_for_time(datetime(2026, 11, 24, hour, 0, tzinfo=TZ)) == meal


def test_meal_in_text():
    assert meal_in_text("3 eggs for breakfast") == "breakfast"
    assert meal_in_text("Add 33 servings to lunch along with 59g arugula") == "lunch"
    assert meal_in_text("breakfast and lunch") is None and meal_in_text("2 eggs") is None


def test_old_database_gets_meals_filled_in(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)  # the schema before meals existed
    old.executescript("""CREATE TABLE entries (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER
        NOT NULL, eaten_at TEXT NOT NULL, description TEXT NOT NULL, item TEXT NOT NULL, calories
        INTEGER NOT NULL, protein_g REAL NOT NULL DEFAULT 0, carbs_g REAL NOT NULL DEFAULT 0,
        fat_g REAL NOT NULL DEFAULT 0, meal_id TEXT NOT NULL);""")
    old.executemany("INSERT INTO entries (user_id, eaten_at, description, item, calories, meal_id)"
                    " VALUES (1, ?, ?, ?, ?, 'm')",
                    [("2026-11-24T19:42:39+00:00", "3 eggs for breakfast", "eggs", 215),   # 11:42, but text says breakfast
                     ("2026-11-24T20:10:04+00:00", "33 servings to lunch", "dressing", 29),
                     ("2026-11-25T02:30:00+00:00", "steak", "steak", 600)])             # 18:30 in Los Angeles
    old.commit(); old.close()
    s = Store(path, TZ)
    rows = s._fetch("WHERE user_id=1", ())
    assert [e.meal for e in rows] == ["breakfast", "lunch", "dinner"]
    Store(path, TZ)  # opening again is harmless


def test_day_view_grouped_by_meal_and_meal_questions(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    s.add_items(1, "x", [item("eggs", 143)], "a", meal="breakfast")
    s.add_items(1, "x", [item("salad", 15), item("dressing", 29)], "b", meal="lunch")
    s.add_items(1, "x", [item("pasta", 700)], "c", datetime.now(TZ) - timedelta(days=1), meal="dinner")
    from calorie_bot.bot import day_text
    today = day_text(s, 1)
    assert today.index("Breakfast") < today.index("Lunch") and "Dinner" not in today
    assert "<b>Lunch</b> — 44 kcal" in today and "Today:</b> 187 kcal" in today
    lunch = day_text(s, 1, 0, "lunch")
    assert "salad" in lunch and "eggs" not in lunch
    assert day_text(s, 1, 0, "dinner") == "Nothing logged for dinner today."
    assert "pasta" in day_text(s, 1, -1, "dinner")
    assert "Total:</b> 700 kcal" in day_text(s, 1, -1)


def test_logging_uses_meal_and_corrections_keep_it(tmp_path):
    chat = Chat(tmp_path)
    assert "Logged for lunch" in chat.say("salad for lunch", claude_says(
        "log_meal", meal="lunch", items=[food("salad", 120)]))
    assert chat.claude.calls[-1]["now"]                         # Claude was told the time
    salad = chat.store.entries_for_day(1, chat.store.today())[0]
    assert salad.meal == "lunch"
    chat.say("actually a big salad", claude_says("correct_log", entry_ids=[salad.id],
                                                 items=[food("big salad", 200)]))
    [fixed] = chat.store.entries_for_day(1, chat.store.today())
    assert fixed.meal == "lunch"                                  # correction kept the meal
    assert "Moved big salad to dinner" in chat.say("that was dinner", claude_says(
        "move_entries", entry_ids=[fixed.id], meal="dinner"))
    assert chat.store.entries_for_day(1, chat.store.today())[0].meal == "dinner"
    assert "big salad" in chat.say("what did i have for dinner", claude_says(
        "show_day", meal="dinner"))
    assert "Nothing logged for lunch today" in chat.say("and lunch?", claude_says(
        "show_day", meal="lunch"))


# ---- cooked recipes ---------------------------------------------------------------

def ing(name, grams, cooked_grams, kcal, protein=0, fat=0):
    return {"ingredient": name, "grams": grams, "cooked_grams": cooked_grams, "calories": kcal,
            "protein_g": protein, "carbs_g": 0, "fat_g": fat}


DIJON_CHICKEN = [  # 706 g raw, 876 kcal; chicken keeps ~75% of its weight
    ing("Chicken breast, raw", 644, 483, 773, 145, 17),
    ing("Dijon mustard", 18, 18, 12), ing("Soy sauce", 12, 12, 6), ing("Ginger-garlic paste", 15, 15, 12),
    ing("Olive oil", 5, 5, 44, 0, 5), ing("Spices", 10, 10, 29), ing("Salt", 2, 2, 0),
]


def test_cooked_recipe_uses_estimated_cooked_weight(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    r, _ = s.save_recipe(1, "Dijon chicken", DIJON_CHICKEN, 0, 0, "src", cooked=True)
    assert (r.raw_weight_g, r.total_weight_g, r.weight_basis) == (706, 545, "estimated") and r.cooked
    p = r.portion(grams=100)
    assert round(p["calories"]) == round(876 * 100 / 545) == 161      # not 124 (raw-based)
    assert p["item"] == "Dijon chicken, 100 g cooked"
    raw = r.portion(grams=100, raw=True)
    assert round(raw["calories"]) == 124 and raw["item"] == "Dijon chicken, 100 g raw"
    assert r.portion(fraction=0.5)["calories"] == 438                  # fractions don't care


def test_weighing_replaces_the_estimate(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    s.save_recipe(1, "Dijon chicken", DIJON_CHICKEN, 0, 0, "src", cooked=True)
    r = s.update_recipe_size(1, "Dijon chicken", final_weight_g=520)
    assert (r.total_weight_g, r.weight_basis, r.raw_weight_g) == (520, "weighed", 706)
    assert round(r.portion(grams=100)["calories"]) == 168
    r2, _ = s.save_recipe(1, "Stew", DIJON_CHICKEN, 600, 0, "weighed 600 g", cooked=True)
    assert (r2.total_weight_g, r2.weight_basis) == (600, "weighed")


def test_uncooked_recipe_stays_raw(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    r, _ = s.save_recipe(1, "Vinaigrette", VINAIGRETTE, 0, 0, "src", cooked=False)
    assert (r.total_weight_g, r.weight_basis, r.cooked) == (120, "raw", False)
    assert r.portion(grams=37)["item"] == "Dijon vinaigrette, 37 g".replace("Dijon v", "V")
    assert r.portion(grams=37, raw=True) == r.portion(grams=37)          # raw = finished here


def test_cooked_recipe_label_and_card(tmp_path):
    from calorie_bot.bot import recipe_label
    s = Store(tmp_path / "t.db", TZ)
    r, _ = s.save_recipe(1, "Dijon chicken", DIJON_CHICKEN, 0, 0, "src", cooked=True)
    assert recipe_label(r) == "Dijon chicken (whole batch: 545 g cooked, 706 g raw, servings not set)"
    card = recipe_card(r)
    assert "706 g raw → about 545 g cooked (estimated)" in card
    assert "Per 100 g cooked:</b> 161 kcal" in card
    msg = FakeMsg()
    asyncio.run(save_recipe_and_reply(msg, s, 1, {"name": "Dijon chicken", "name_given": True,
        "ingredients": DIJON_CHICKEN, "cooked": True, "final_weight_g": 0, "servings": 0}, "src"))
    assert "weigh it after cooking" in msg.replies[-1]


def test_apply_recipes_raw_flag(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    s.save_recipe(1, "Dijon chicken", DIJON_CHICKEN, 0, 0, "src", cooked=True)
    base = {**item("chicken", 999), "recipe": "Dijon chicken", "recipe_grams": 100,
            "recipe_servings": 0, "recipe_fraction": 0}
    out, used, _ = apply_recipes(s, 1, [{**base, "recipe_grams_raw": False},
                                        {**base, "recipe_grams_raw": True}])
    assert used == [True, True] and [round(o["calories"]) for o in out] == [161, 124]


def test_old_recipes_get_raw_weight(tmp_path):
    path = tmp_path / "old.db"
    s = Store(path, TZ)
    s.save_recipe(1, "Vinaigrette", VINAIGRETTE, 0, 0, "src")
    s.save_recipe(1, "Chili", VINAIGRETTE, 60, 0, "weighed")
    # Turn it back into the old schema: no raw_weight_g / weight_basis columns.
    s.conn.executescript("""CREATE TABLE r2 AS SELECT id, user_id, name, name_key, ingredients,
        total_weight_g, servings, calories, protein_g, carbs_g, fat_g, source_text, updated_at
        FROM recipes; DROP TABLE recipes; ALTER TABLE r2 RENAME TO recipes;""")
    s.conn.close()
    s = Store(path, TZ)
    v, c = s.get_recipe(1, "vinaigrette"), s.get_recipe(1, "chili")
    assert (v.raw_weight_g, v.weight_basis) == (120, "raw")
    assert (c.raw_weight_g, c.total_weight_g, c.weight_basis) == (120, 60, "weighed")


# ---- Apple Health sync ------------------------------------------------------------

import urllib.request  # noqa: E402

from calorie_bot import health  # noqa: E402


def log_on(s, days_ago, meal, name, kcal, hour):
    when = datetime.combine(s.today() - timedelta(days=days_ago), datetime.min.time(), TZ) \
        + timedelta(hours=hour)
    s.add_items(1, "x", [item(name, kcal)], uuid4hex(), when, meal=meal)


def uuid4hex():
    import uuid
    return uuid.uuid4().hex


def test_health_export_finished_days_by_meal(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    log_on(s, 2, "breakfast", "eggs", 143, 8)
    log_on(s, 2, "breakfast", "toast", 80, 9)
    log_on(s, 2, "dinner", "pasta", 700, 19)
    log_on(s, 1, "lunch", "salad", 300, 12)
    log_on(s, 0, "lunch", "today's lunch", 500, 0)      # today: never sent
    log_on(s, 40, "lunch", "too old", 500, 12)          # beyond 30 days
    out = health.export(s, 1, "http://pi/health/tok")
    assert [(x["meal"], x["kcal"]) for x in out["samples"]] == [("breakfast", 223), ("dinner", 700),
                                                                ("lunch", 300)]
    first = out["samples"][0]
    assert first["protein_g"] == 2 and first["iso"].endswith(("-08:00", "-07:00"))
    assert first["date"].endswith("at 8:00 AM")                     # earliest item in the meal
    d2, d1 = (s.today() - timedelta(days=2)).isoformat(), (s.today() - timedelta(days=1)).isoformat()
    assert out["days"] == [d2, d1] and out["ack_url"] == f"http://pi/health/tok/ack?days={d2},{d1}"

    assert health.acknowledge(s, 1, f"{d2},{s.today().isoformat()},garbage") == [d2]  # today refused
    assert health.export(s, 1, "l")["days"] == [d1]
    health.acknowledge(s, 1, d1)
    assert health.export(s, 1, "l") == {"samples": [], "days": [], "ack_url": "l/ack"}


def test_health_tokens_are_per_user(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    t1, t2 = s.health_token(1), s.health_token(2)
    assert t1 != t2 and s.health_token(1) == t1 and len(t1) >= 20
    assert s.user_for_health_token(t2) == 2 and s.user_for_health_token("nope") is None
    s.set_goal(1, 2000)
    assert s.health_token(1) == t1 and s.get_goal(1) == 2000          # goal and token coexist


def test_health_http_server(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    s = Store(path, TZ)
    log_on(s, 1, "dinner", "steak", 600, 19)
    token = s.health_token(1)
    server = health.ThreadingHTTPServer(("127.0.0.1", 0), health.make_handler(str(path), TZ, 0))
    port = server.server_address[1]
    monkeypatch.setenv("HEALTH_BASE_URL", f"http://127.0.0.1:{port}")
    health.threading.Thread(target=server.serve_forever, daemon=True).start()
    get = lambda p: json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}{p}").read())
    try:
        out = get(f"/health/{token}")
        assert [x["kcal"] for x in out["samples"]] == [600]
        assert get(urlparse_path(out["ack_url"]))["synced"] == out["days"]
        assert get(f"/health/{token}")["samples"] == []
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health/wrong-token-1234567890")
        assert e.value.code == 404
    finally:
        server.shutdown()


def urlparse_path(url):
    from urllib.parse import urlparse
    u = urlparse(url)
    return f"{u.path}?{u.query}"


def test_health_setup_reply(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTH_BASE_URL", "http://raspberrypi.local:8787")
    chat = Chat(tmp_path)
    reply = chat.say("set up apple health")                      # instant, no Claude
    token = chat.store.health_token(1)
    assert f"http://raspberrypi.local:8787/health/{token}" in reply and "Log Health Sample" in reply
    assert chat.claude.calls == []
    assert "Apple Health sync" in chat.say("can you sync to my iphone health app?",
                                           claude_says("apple_health"))


def test_configured_timezone(monkeypatch):
    from calorie_bot.bot import configured_timezone
    monkeypatch.setenv("TIMEZONE", "Europe/Berlin")
    assert str(configured_timezone()) == "Europe/Berlin"
    monkeypatch.setenv("TIMEZONE", "Not/AZone")
    assert str(configured_timezone()) == "UTC"
    monkeypatch.setenv("TIMEZONE", "")
    assert configured_timezone() is not None        # the machine's own zone, or UTC


# ---- guardrails ---------------------------------------------------------------------

from calorie_bot.bot import free_reply  # noqa: E402


@pytest.mark.parametrize("text,reply", [
    ("hi", "Hi!"), ("Good morning!", "Hi!"), ("thanks!!", "👍"), ("ok", "👍"),
    ("👍🔥", "👍"), ("...", "👍"), ("", ""),
    ("2 eggs", None), ("hi, I had 2 eggs", None), ("thanks, also a banana", None),
])
def test_free_reply(text, reply):
    out = free_reply(text, has_photo=False)
    assert (out is None) if reply is None else out.startswith(reply)


def test_photo_always_goes_to_claude():
    assert free_reply("", has_photo=True) is None


def test_long_message_refused(monkeypatch):
    monkeypatch.setenv("MAX_MESSAGE_CHARS", "50")
    assert "too long" in free_reply("x" * 51, has_photo=False)
    assert free_reply("x" * 50, has_photo=False) is None


def test_free_replies_and_cap_skip_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_DAILY_LIMIT", "2")
    chat = Chat(tmp_path)
    assert chat.say("hi").startswith("Hi!") and chat.say("thanks") == "👍"
    chat.say("an apple", claude_says("other", note="ok"))
    chat.say("a pear", claude_says("other", note="ok"))
    assert "today's limit of 2" in chat.say("a plum")
    assert len(chat.claude.calls) == 2                    # greetings, thanks and the 3rd call: free
    assert "Nothing logged" in chat.say("today")           # instant replies still work


def test_claude_usage_counts_per_user_per_day(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    assert [s.count_claude_call(1) for _ in range(3)] == [1, 2, 3]
    assert s.count_claude_call(2) == 1


def test_claude_code_runs_at_low_effort(fake_claude):
    _, calls = fake_claude({"is_error": False, "structured_output": {"items": [], "note": ""}}, text="x")
    assert "--effort low" in calls


# ---- meal reminders -----------------------------------------------------------------

from calorie_bot import reminders as rem  # noqa: E402


def at(s, hh, mm=0):
    return datetime.combine(s.today(), datetime.min.time(), TZ) + timedelta(hours=hh, minutes=mm)


class FakeApp:
    def __init__(self, store):
        self.bot_data = {"store": store, "memory": __import__("calorie_bot.bot", fromlist=["ChatMemory"]).ChatMemory()}
        self.sent = []
        self.bot = self

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


def test_reminder_due_only_in_window_and_only_once(tmp_path, monkeypatch):
    monkeypatch.delenv("REMINDERS", raising=False)
    s = Store(tmp_path / "t.db", TZ)
    assert rem.due_reminders(s, 1, at(s, 10, 29)) == []
    assert rem.due_reminders(s, 1, at(s, 10, 30)) == ["breakfast"]
    assert rem.due_reminders(s, 1, at(s, 12, 31)) == []                    # past the 2 h window
    app = FakeApp(s)
    asyncio.run(rem.send_due(app, s, [1], at(s, 10, 45)))
    assert app.sent == [(1, rem.MESSAGES["breakfast"])]
    asyncio.run(rem.send_due(app, s, [1], at(s, 11, 0)))
    assert len(app.sent) == 1                                               # only once
    assert "Haven't logged breakfast" in app.bot_data["memory"].lines(1)[-1]  # Claude sees it


def test_no_reminder_when_meal_logged_or_skipped(tmp_path, monkeypatch):
    monkeypatch.delenv("REMINDERS", raising=False)
    s = Store(tmp_path / "t.db", TZ)
    s.add_items(1, "x", [item("salad", 300)], "a", at(s, 12), meal="lunch")
    assert rem.due_reminders(s, 1, at(s, 14, 40)) == []
    s.mark_reminder(1, s.today(), "dinner", "skipped")
    assert rem.due_reminders(s, 1, at(s, 20, 40)) == []
    assert rem.due_reminders(s, 2, at(s, 20, 40)) == ["dinner"]          # per user


def test_reminder_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("REMINDERS", raising=False)
    s = Store(tmp_path / "t.db", TZ)
    assert rem.apply_change(s, 1, "lunch", "13:00")["lunch"] == "13:00"
    assert rem.apply_change(s, 1, "breakfast", "off")["breakfast"] is None
    assert rem.due_reminders(s, 1, at(s, 13, 5)) == ["lunch"]
    everything_off = rem.apply_change(s, 1, "", "off")
    assert set(everything_off.values()) == {None}
    assert rem.apply_change(s, 1, "", "on") == {"breakfast": "10:30", "lunch": "14:30", "dinner": "20:30"}
    assert rem.fmt_time("14:30") == "2:30 PM" and rem.normalize_time("9:05") == "09:05"


@pytest.mark.parametrize("env,expected", [
    ("", {"breakfast": "10:30", "lunch": "14:30", "dinner": "20:30"}),
    ("off", {"breakfast": None, "lunch": None, "dinner": None}),
    ("lunch=12:15, dinner=19:00", {"breakfast": None, "lunch": "12:15", "dinner": "19:00"}),
])
def test_default_reminders_from_env(monkeypatch, env, expected):
    monkeypatch.setenv("REMINDERS", env)
    assert rem.default_reminders() == expected


def test_reminders_in_plain_words(tmp_path, monkeypatch):
    monkeypatch.delenv("REMINDERS", raising=False)
    chat = Chat(tmp_path)
    assert "Lunch: 2:30 PM" in chat.say("reminders")                       # instant
    assert "Lunch: 1:00 PM" in chat.say("remind me about lunch at 1pm", claude_says(
        "set_reminders", meal="lunch", reminder_time="13:00"))
    assert "Breakfast: off" in chat.say("no breakfast reminders", claude_says(
        "set_reminders", meal="breakfast", reminder_time="off"))
    assert "didn't catch the time" in chat.say("remind me at teatime", claude_says(
        "set_reminders", meal="dinner", reminder_time="teatime"))
    assert "won't remind you" in chat.say("I skipped dinner", claude_says("skip_meal", meal="dinner"))
    assert chat.store.reminder_done(1, chat.store.today(), "dinner") == "skipped"


# ---- 7-day summary and chart --------------------------------------------------------

from calorie_bot import summary as summ  # noqa: E402


def full_day(s, days_ago, kcal_each, uid=1):
    for meal, hour in (("breakfast", 8), ("lunch", 12), ("dinner", 19)):
        when = datetime.combine(s.today() - timedelta(days=days_ago), datetime.min.time(), TZ) \
            + timedelta(hours=hour)
        s.add_items(uid, "x", [item(meal, kcal_each)], uuid4hex(), when, meal=meal)


def test_summary_only_charts_complete_days(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    full_day(s, 1, 600)                                   # 1,800: complete
    full_day(s, 2, 700)                                   # 2,100: complete
    log_on(s, 3, "breakfast", "eggs", 300, 8)             # only breakfast: left out
    log_on(s, 4, "breakfast", "oats", 300, 8)             # breakfast + lunch, dinner skipped
    log_on(s, 4, "lunch", "salad", 400, 12)
    s.mark_reminder(1, s.today() - timedelta(days=4), "dinner", "skipped")
    s.set_goal(1, 2000)
    out = summ.build(s, 1, s.today())
    assert sorted(d.kcal for d in out.complete) == [700, 1800, 2100]   # the skipped day counts
    assert [(d, m) for d, m in out.left_out] == [(s.today() - timedelta(days=3), ["lunch", "dinner"])]
    assert round(out.average) == 1533
    cap = summ.caption(out)
    assert "1,533 kcal</b>/day over 3 complete days" in cap
    assert "no lunch or dinner" in cap and "2,000 goal on 2 of 3 days" in cap
    png = summ.chart_png(out)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 10_000


def test_summary_with_nothing_complete(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    log_on(s, 1, "lunch", "salad", 400, 12)
    out = summ.build(s, 1, s.today())
    assert out.complete == [] and "No complete days" in summ.empty_text(out)
    assert "no breakfast or dinner" in summ.empty_text(out)


def test_summary_request_sends_a_photo(tmp_path):
    chat = Chat(tmp_path)
    full_day(chat.store, 1, 500)
    assert "Last 7 days" in chat.say("give me a summary")          # instant, no Claude
    assert chat.photos and chat.photos[0][:4] == b"\x89PNG" and chat.claude.calls == []
    chat.say("how did I do this week?", claude_says("show_week"))
    assert len(chat.photos) == 2


class PhotoApp(FakeApp):
    async def send_photo(self, chat_id, photo, caption, parse_mode=None):
        self.sent.append((chat_id, "photo:" + caption))


def test_weekly_summary_on_sunday_once(tmp_path, monkeypatch):
    monkeypatch.delenv("WEEKLY_SUMMARY", raising=False)
    monkeypatch.setenv("REMINDERS", "off")
    s = Store(tmp_path / "t.db", TZ)
    full_day(s, 1, 600)
    app = PhotoApp(s)
    today = s.today()
    sunday = today + timedelta(days=(6 - today.weekday()) % 7)
    monday = sunday + timedelta(days=1)
    at_time = lambda d, h: datetime.combine(d, datetime.min.time(), TZ) + timedelta(hours=h)
    assert not rem.weekly_due(s, 1, at_time(monday, 10))
    assert not rem.weekly_due(s, 1, at_time(sunday, 8))
    assert rem.weekly_due(s, 1, at_time(sunday, 9))
    assert not rem.weekly_due(s, 1, at_time(sunday, 22))            # past the 12-hour window
    # Sending on a Sunday: data must be in the week before it, so log on the Saturday.
    full_day(s, (today - (sunday - timedelta(days=1))).days, 650)
    asyncio.run(rem.send_due(app, s, [1], at_time(sunday, 9.5)))
    asyncio.run(rem.send_due(app, s, [1], at_time(sunday, 10)))
    weekly = [t for _, t in app.sent if t.startswith("photo:")]
    assert len(weekly) == 1 and "Your week" in weekly[0]


def test_weekly_summary_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("WEEKLY_SUMMARY", raising=False)
    chat = Chat(tmp_path)
    chat.store.set_reminders(1, {"lunch": "13:00"})
    assert "on, Sundays at 9:00 AM" in chat.say("weekly summary settings")
    assert "off" in chat.say("no weekly summary", claude_says("set_weekly_summary", reminder_time="off"))
    assert "6:00 PM" in chat.say("send it at 6pm", claude_says("set_weekly_summary", reminder_time="18:00"))
    assert chat.store.get_reminders(1, rem.default_reminders())["lunch"] == "13:00"  # kept
    monkeypatch.setenv("WEEKLY_SUMMARY", "off")
    assert rem.default_weekly() is None


# ---- weight goals, targets and coaching --------------------------------------------

from calorie_bot import targets as tg  # noqa: E402

ME = {"age": 34, "sex": "male", "height_cm": 180, "weight_kg": 85, "activity": "moderate",
      "goal": "lose"}


def test_targets_match_hand_calculation():
    t = tg.calculate(ME)
    assert t.bmr == 10 * 85 + 6.25 * 180 - 5 * 34 + 5 == 1810          # Mifflin-St Jeor
    assert round(t.tdee, 1) == round(1810 * 1.55, 1)
    assert t.calories == 2260                                          # 2805.5 - 550, to 10s
    assert (t.protein_g, t.fat_g, t.carbs_g) == (153, 68, 259)
    assert round(t.rate_kg_week, 2) == -0.5 and t.notes == []
    assert tg.calculate({**ME, "goal": "maintain"}).calories == 2810
    assert tg.calculate({**ME, "goal": "gain"}).calories == 3080          # +275 (0.25 kg/wk)


def test_targets_safety_limits():
    fast = tg.calculate({**ME, "rate_kg_week": 2})
    assert fast.rate_kg_week >= -0.85 and any("capped" in n for n in fast.notes)
    small = tg.calculate({"age": 60, "sex": "female", "height_cm": 150, "weight_kg": 55,
                          "activity": "sedentary", "goal": "lose", "rate_kg_week": 1})
    assert small.calories == 1200 and any("minimum" in n for n in small.notes)
    thin = tg.calculate({**ME, "weight_kg": 55})                      # BMI 17
    assert thin.rate_kg_week == 0 and any("won't plan weight loss" in n for n in thin.notes)
    teen = tg.calculate({**ME, "age": 16})
    assert teen.rate_kg_week == 0 and "under 18" in teen.notes[0]
    assert tg.calculate({**ME, "goal": "gain", "rate_kg_week": 3}).calories == 2810 + 500 - 5 + 5
    no_sex = tg.calculate({**ME, "sex": ""})
    assert no_sex.bmr == 1810 - 5 - 78


def test_profile_conversation(tmp_path):
    chat = Chat(tmp_path)
    p = lambda **kw: {"sex": "", "age": 0, "height_cm": 0, "weight_kg": 0, "activity": "",
                      "goal": "", "rate_kg_week": 0, "target_weight_kg": 0, "coach_tone": "", **kw}
    reply = chat.say("I want to lose weight", claude_says("update_profile", profile=p(goal="lose")))
    assert "still need your age, your height, your current weight and how active" in reply
    reply = chat.say("34 male 180cm 85kg", claude_says("update_profile", profile=p(
        age=34, sex="male", height_cm=180, weight_kg=85)))
    assert "still need how active you are" in reply
    reply = chat.say("gym 4x a week", claude_says("update_profile", profile=p(activity="moderate")))
    assert "2,260 kcal</b> a day" in reply and "Mifflin-St Jeor" in reply
    assert chat.store.get_goal(1) == 2260
    assert chat.store.get_macro_targets(1) == {"protein_g": 153, "carbs_g": 259, "fat_g": 68}
    assert "2,260 kcal" in chat.say("my targets")                         # instant
    reply = chat.say("I weigh 83 kg today", claude_says("update_profile", profile=p(weight_kg=83)))
    assert "Logged 83 kg" in reply and chat.store.get_goal(1) < 2260
    assert chat.store.weights_between(1, chat.store.today(), chat.store.today())[0][1] == 83
    assert "Tough love" in chat.say("be harsh", claude_says("update_profile", profile=p(coach_tone="tough")))


def test_coaching_and_day_verdict(tmp_path):
    chat = Chat(tmp_path)
    chat.store.update_profile(1, {**ME, "coach_tone": "tough"})
    apply = __import__("calorie_bot.bot", fromlist=["apply_targets"]).apply_targets
    apply(chat.store, 1, chat.store.get_profile(1))                       # 2,260 kcal
    for meal, kcal in (("breakfast", 500), ("lunch", 700)):
        chat.say(meal, claude_says("log_meal", meal=meal, items=[food(meal, kcal)],
                                   coach="Fine." if meal == "breakfast" else ""))
    ctx_line = chat.claude.calls[-1]["coaching"]
    assert "tone=tough" in ctx_line and "eaten today before this message: 500 kcal" in ctx_line
    assert "💬 Fine." in chat.replies[0] and "Day complete" not in chat.replies[-1]
    reply = chat.say("pizza", claude_says("log_meal", meal="dinner", items=[food("pizza", 1300)],
                                          coach="That pizza blew your day. Plan dinner next time."))
    assert "💬 That pizza blew your day" in reply
    assert "Day complete:</b> 2,500 / 2,260 kcal, ❌ over target by 240" in reply
    assert "Protein" in reply


def test_no_coaching_without_profile_or_when_off(tmp_path):
    chat = Chat(tmp_path)
    chat.say("eggs", claude_says("log_meal", meal="breakfast", items=[food("eggs", 200)], coach="Nice!"))
    assert chat.claude.calls[-1]["coaching"] is None and "💬" not in chat.replies[-1]
    chat.store.update_profile(1, {**ME, "coach_tone": "off"})
    chat.store.set_goal(1, 2000)
    chat.say("toast", claude_says("log_meal", meal="breakfast", items=[food("toast", 80)], coach="Hm."))
    assert "💬" not in chat.replies[-1]


def test_skipping_last_meal_gives_verdict(tmp_path):
    chat = Chat(tmp_path)
    chat.store.set_goal(1, 2000)
    for meal in ("breakfast", "lunch"):
        chat.say(meal, claude_says("log_meal", meal=meal, items=[food(meal, 600)]))
    reply = chat.say("skipping dinner", claude_says("skip_meal", meal="dinner"))
    assert "Day complete:</b> 1,200 / 2,000 kcal, ⚠️ 800 under target. That's a lot under" in reply


def test_verdict_slightly_under_is_on_target(tmp_path):
    from calorie_bot.bot import day_verdict
    s = Store(tmp_path / "t.db", TZ)
    s.set_goal(1, 2000)
    s.add_items(1, "x", [item("food", 1850)], "a", meal="lunch")
    assert "✅ on target, 150 under" in day_verdict(s, 1)


# ---- setup prompts -------------------------------------------------------------------

def blank_profile(**kw):
    return {"sex": "", "age": 0, "height_cm": 0, "weight_kg": 0, "activity": "", "goal": "",
            "rate_kg_week": 0, "target_weight_kg": 0, "coach_tone": "", **kw}


def test_setup_nudge_only_once(tmp_path):
    chat = Chat(tmp_path)
    first = chat.say("eggs", claude_says("log_meal", meal="breakfast", items=[food("eggs", 200)]))
    assert "Say “set up my goals”" in first
    second = chat.say("toast", claude_says("log_meal", meal="breakfast", items=[food("toast", 80)]))
    assert "set up my goals" not in second
    assert tg.missing_fields(chat.store.get_profile(1))       # the flag doesn't count as set up


def test_no_nudge_once_set_up(tmp_path):
    chat = Chat(tmp_path)
    chat.store.update_profile(1, {**ME, "coach_tone": "encouraging"})
    reply = chat.say("eggs", claude_says("log_meal", meal="breakfast", items=[food("eggs", 200)]))
    assert "set up my goals" not in reply


def test_set_up_my_goals_is_instant(tmp_path):
    chat = Chat(tmp_path)
    reply = chat.say("set up my goals")
    assert "Let's set up your goals" in reply and "tough love" in reply and chat.claude.calls == []


def test_tone_question_until_chosen(tmp_path):
    chat = Chat(tmp_path)
    reply = chat.say("all of it", claude_says("update_profile", profile=blank_profile(**ME)))
    assert "Your targets" in reply and "how should I comment on your meals" in reply
    reply = chat.say("tough love", claude_says("update_profile", profile=blank_profile(coach_tone="tough")))
    assert "Tough love it is" in reply
    assert "how should I comment" not in chat.say("my targets")       # asked only while unset


# ---- help ----------------------------------------------------------------------------

from calorie_bot import help as helpdocs  # noqa: E402


def test_help_pages_fit_in_one_telegram_message():
    for name, text in [("overview", helpdocs.OVERVIEW), *helpdocs.TOPICS.items()]:
        visible = re.sub(r"<[^>]+>", "", text)
        assert len(visible) < 4096, name
        assert not re.search(r"(^|\s)/\w", visible), f"{name} mentions a slash command"


def test_overview_lists_every_topic():
    for topic in helpdocs.TOPICS:
        assert f"help {topic}" in helpdocs.OVERVIEW


@pytest.mark.parametrize("text,starts", [
    ("help", "👋"), ("/help", "👋"), ("Help!", "👋"), ("/start", None),
    ("help recipes", "📖"), ("help with recipes please", "📖"), ("/help goals", "🎯"),
    ("help coaching", "🎯"), ("help setup", "🎯"), ("help reminders", "⏰"),
    ("help weekly summary", "⏰"), ("help apple health", "❤️"), ("help logging", "🍽"),
    ("help today", "📋"), ("help summary", "📋"), ("help privacy", "🔒"), ("help xyz", "👋"),
    ("how does 100g raw chicken compare to cooked?", None),    # a food question, not help
    ("helpful tip: 2 eggs", None),
])
def test_quick_help_routing(text, starts):
    page = helpdocs.quick_help(text)
    assert (page is None) if starts is None else page.startswith(starts)


def test_help_in_chat(tmp_path):
    chat = Chat(tmp_path)
    assert chat.say("help recipes").startswith("📖") and chat.claude.calls == []
    assert chat.say("/start").startswith("👋")
    assert chat.say("how do reminders work?", claude_says("help", help_topic="reminders")).startswith("⏰")
    assert chat.say("what can you do?", claude_says("help")).startswith("👋")
