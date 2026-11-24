import asyncio
import json
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
    assert len(app.handlers[0]) == 12


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

from calorie_bot.bot import apply_recipes, recipe_card, split_recipe_message  # noqa: E402

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


@pytest.mark.parametrize("text,name,body", [
    ("/recipe Dijon vinaigrette\nDijon mustard\t20 g\nWater\t55 g",
     "Dijon vinaigrette", "Dijon mustard\t20 g\nWater\t55 g"),
    ("/recipe@shawn_calorie_tracker_bot Chili: beans 400g, beef 500g", "Chili", "beans 400g, beef 500g"),
    ("/recipe Dijon vinaigrette", "Dijon vinaigrette", ""),
    ("/recipe", "", ""),
])
def test_split_recipe_message(text, name, body):
    assert split_recipe_message(text) == (name, body)


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


def test_split_treats_ingredient_first_line_as_body():
    assert split_recipe_message("/recipe Dijon mustard 20 g\nWater 55 g") == \
        ("", "Dijon mustard 20 g\nWater 55 g")
    assert split_recipe_message("/recipe\nDijon mustard 20 g") == ("", "Dijon mustard 20 g")


def test_made_up_title_never_overwrites(tmp_path):
    s = Store(tmp_path / "t.db", TZ)
    msg = FakeMsg()
    asyncio.run(save_recipe_and_reply(msg, s, 1, recipe_result("Honey dijon vinaigrette"), "src"))
    asyncio.run(save_recipe_and_reply(msg, s, 1, recipe_result("Honey dijon vinaigrette"), "src"))
    assert [r.name for r in s.list_recipes(1)] == ["Honey dijon vinaigrette", "Honey dijon vinaigrette 2"]
    assert "I called it “Honey dijon vinaigrette 2”" in msg.replies[1]
    assert "/renamerecipe Honey dijon vinaigrette 2 = New name" in msg.replies[1]
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
