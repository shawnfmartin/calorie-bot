"""SQLite storage for food entries and per-user settings."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    eaten_at    TEXT    NOT NULL,          -- ISO-8601 UTC timestamp
    description TEXT    NOT NULL,          -- what the user typed (or "photo")
    item        TEXT    NOT NULL,          -- one parsed food item
    calories    INTEGER NOT NULL,
    protein_g   REAL    NOT NULL DEFAULT 0,
    carbs_g     REAL    NOT NULL DEFAULT 0,
    fat_g       REAL    NOT NULL DEFAULT 0,
    meal_id     TEXT    NOT NULL,          -- groups items from one message
    meal        TEXT                       -- breakfast, lunch, dinner or snack
);
CREATE INDEX IF NOT EXISTS idx_entries_user_time ON entries (user_id, eaten_at);

CREATE TABLE IF NOT EXISTS recipes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    name           TEXT    NOT NULL,
    name_key       TEXT    NOT NULL,       -- lower-cased name, for lookups
    ingredients    TEXT    NOT NULL,       -- JSON list from the estimator
    total_weight_g REAL    NOT NULL,       -- finished weight the totals are spread over
    servings       REAL    NOT NULL DEFAULT 0,
    calories       REAL    NOT NULL,       -- whole-recipe totals
    protein_g      REAL    NOT NULL,
    carbs_g        REAL    NOT NULL,
    fat_g          REAL    NOT NULL,
    source_text    TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL,
    UNIQUE (user_id, name_key)
);

CREATE TABLE IF NOT EXISTS settings (
    user_id     INTEGER PRIMARY KEY,
    daily_goal  INTEGER
);
"""


@dataclass
class Entry:
    id: int
    eaten_at: datetime
    item: str
    calories: int
    protein_g: float
    carbs_g: float
    fat_g: float
    meal_id: str
    meal: str = "snack"


MEALS = ("breakfast", "lunch", "dinner", "snack")


def meal_for_time(when: datetime) -> str:
    """The meal a time of day most likely belongs to, when the user doesn't say."""
    h = when.hour
    if 4 <= h < 11:
        return "breakfast"
    if 11 <= h < 16:
        return "lunch"
    if 16 <= h < 22:
        return "dinner"
    return "snack"


def meal_in_text(text: str) -> str | None:
    """'...for breakfast' -> 'breakfast'. Used to fill in entries saved before meals existed."""
    lowered = (text or "").lower()
    found = [m for m in MEALS if m in lowered]
    return found[0] if len(found) == 1 else None


MACROS = ("calories", "protein_g", "carbs_g", "fat_g")


def recipe_key(name: str) -> str:
    return " ".join(name.lower().split())


@dataclass
class Recipe:
    id: int
    name: str
    ingredients: list[dict]
    total_weight_g: float
    servings: float
    calories: float
    protein_g: float
    carbs_g: float
    fat_g: float

    def portion(self, grams: float = 0, servings: float = 0, fraction: float = 0) -> dict | None:
        """Calories and macros for a portion, or None if the amount can't be worked out."""
        if fraction > 0:
            share = fraction
            named = {0.25: "a quarter of the batch", 0.33: "a third of the batch",
                     0.5: "half the batch", 1.0: "the whole batch"}
            label = named.get(round(fraction, 2), f"{fraction:.0%} of the batch")
        elif grams > 0 and self.total_weight_g > 0:
            share, label = grams / self.total_weight_g, f"{grams:g} g"
        elif servings > 0 and self.servings > 0:
            share = servings / self.servings
            label = f"{servings:g} serving" + ("" if servings == 1 else "s")
        else:
            return None
        out = {k: getattr(self, k) * share for k in MACROS}
        out["item"] = f"{self.name}, {label}"
        return out


class Store:
    def __init__(self, path: str | Path, tz: ZoneInfo):
        self.tz = tz
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Bring databases made by older versions up to date."""
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(entries)")}
        if "meal" not in columns:
            self.conn.execute("ALTER TABLE entries ADD COLUMN meal TEXT")
        # Entries from before meals existed: what the user wrote, else the time of day.
        rows = self.conn.execute(
            "SELECT id, eaten_at, description FROM entries WHERE meal IS NULL").fetchall()
        for r in rows:
            local = datetime.fromisoformat(r["eaten_at"]).astimezone(self.tz)
            meal = meal_in_text(r["description"]) or meal_for_time(local)
            self.conn.execute("UPDATE entries SET meal=? WHERE id=?", (meal, r["id"]))

    # ---- writes -----------------------------------------------------------

    def add_items(self, user_id: int, description: str, items: list[dict],
                  meal_id: str, eaten_at: datetime | None = None, meal: str | None = None) -> None:
        when = eaten_at or datetime.now(self.tz)
        meal = meal if meal in MEALS else meal_for_time(when.astimezone(self.tz))
        ts = when.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")
        self.conn.executemany(
            "INSERT INTO entries (user_id, eaten_at, description, item, calories,"
            " protein_g, carbs_g, fat_g, meal_id, meal) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(user_id, ts, description, i["item"], int(round(i["calories"])),
              i.get("protein_g", 0), i.get("carbs_g", 0), i.get("fat_g", 0), meal_id, meal)
             for i in items],
        )
        self.conn.commit()

    def set_meal(self, user_id: int, entry_ids: list[int], meal: str) -> list[Entry]:
        """Move entries to another meal. Returns the entries that moved."""
        if meal not in MEALS or not entry_ids:
            return []
        marks = ",".join("?" * len(entry_ids))
        self.conn.execute(f"UPDATE entries SET meal=? WHERE user_id=? AND id IN ({marks})",
                          (meal, user_id, *entry_ids))
        self.conn.commit()
        return self._fetch(f"WHERE user_id=? AND id IN ({marks})", (user_id, *entry_ids))

    def undo_last_meal(self, user_id: int) -> list[Entry]:
        row = self.conn.execute(
            "SELECT meal_id FROM entries WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,)).fetchone()
        if not row:
            return []
        removed = self._fetch("WHERE user_id=? AND meal_id=?", (user_id, row["meal_id"]))
        self.conn.execute("DELETE FROM entries WHERE user_id=? AND meal_id=?",
                          (user_id, row["meal_id"]))
        self.conn.commit()
        return removed

    def delete_entry(self, user_id: int, entry_id: int) -> Entry | None:
        found = self._fetch("WHERE user_id=? AND id=?", (user_id, entry_id))
        if not found:
            return None
        self.conn.execute("DELETE FROM entries WHERE user_id=? AND id=?", (user_id, entry_id))
        self.conn.commit()
        return found[0]

    def set_goal(self, user_id: int, goal: int | None) -> None:
        self.conn.execute(
            "INSERT INTO settings (user_id, daily_goal) VALUES (?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET daily_goal=excluded.daily_goal",
            (user_id, goal))
        self.conn.commit()

    def save_recipe(self, user_id: int, name: str, ingredients: list[dict],
                    final_weight_g: float, servings: float, source_text: str) -> tuple[Recipe, bool]:
        """Create or replace a recipe. Returns (recipe, replaced_existing)."""
        totals = {k: sum(float(i.get(k, 0)) for i in ingredients) for k in MACROS}
        weight = final_weight_g if final_weight_g > 0 else sum(float(i.get("grams", 0)) for i in ingredients)
        replaced = self.get_recipe(user_id, name) is not None
        self.conn.execute(
            "INSERT INTO recipes (user_id, name, name_key, ingredients, total_weight_g, servings,"
            " calories, protein_g, carbs_g, fat_g, source_text, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(user_id, name_key) DO UPDATE SET name=excluded.name,"
            " ingredients=excluded.ingredients, total_weight_g=excluded.total_weight_g,"
            " servings=excluded.servings, calories=excluded.calories,"
            " protein_g=excluded.protein_g, carbs_g=excluded.carbs_g, fat_g=excluded.fat_g,"
            " source_text=excluded.source_text, updated_at=excluded.updated_at",
            (user_id, name.strip(), recipe_key(name), json.dumps(ingredients), weight, servings,
             totals["calories"], totals["protein_g"], totals["carbs_g"], totals["fat_g"],
             source_text, datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds")))
        self.conn.commit()
        return self.get_recipe(user_id, name), replaced

    def update_recipe_size(self, user_id: int, name: str, servings: float = 0,
                           final_weight_g: float = 0) -> Recipe | None:
        """Set how many servings a recipe makes and/or its finished weight (0 = leave as is)."""
        recipe = self.get_recipe(user_id, name)
        if recipe is None:
            return None
        if servings > 0:
            self.conn.execute("UPDATE recipes SET servings=? WHERE id=?", (servings, recipe.id))
        if final_weight_g > 0:
            self.conn.execute("UPDATE recipes SET total_weight_g=? WHERE id=?",
                              (final_weight_g, recipe.id))
        self.conn.commit()
        return self.get_recipe(user_id, name)

    def rename_recipe(self, user_id: int, old: str, new: str) -> str:
        """Returns "ok", "missing" (no recipe `old`) or "taken" (another recipe is called `new`)."""
        recipe = self.get_recipe(user_id, old)
        if recipe is None:
            return "missing"
        other = self.get_recipe(user_id, new)
        if other is not None and other.id != recipe.id:
            return "taken"
        self.conn.execute("UPDATE recipes SET name=?, name_key=? WHERE id=?",
                          (new.strip(), recipe_key(new), recipe.id))
        self.conn.commit()
        return "ok"

    def delete_recipe(self, user_id: int, name: str) -> bool:
        cur = self.conn.execute("DELETE FROM recipes WHERE user_id=? AND name_key=?",
                                (user_id, recipe_key(name)))
        self.conn.commit()
        return cur.rowcount > 0

    # ---- reads ------------------------------------------------------------

    def get_recipe(self, user_id: int, name: str) -> Recipe | None:
        row = self.conn.execute("SELECT * FROM recipes WHERE user_id=? AND name_key=?",
                                (user_id, recipe_key(name))).fetchone()
        return self._recipe(row) if row else None

    def list_recipes(self, user_id: int) -> list[Recipe]:
        rows = self.conn.execute("SELECT * FROM recipes WHERE user_id=? ORDER BY name_key",
                                 (user_id,)).fetchall()
        return [self._recipe(r) for r in rows]

    @staticmethod
    def _recipe(r: sqlite3.Row) -> Recipe:
        return Recipe(r["id"], r["name"], json.loads(r["ingredients"]), r["total_weight_g"],
                      r["servings"], r["calories"], r["protein_g"], r["carbs_g"], r["fat_g"])

    def get_goal(self, user_id: int) -> int | None:
        row = self.conn.execute("SELECT daily_goal FROM settings WHERE user_id=?",
                                (user_id,)).fetchone()
        return row["daily_goal"] if row else None

    def entries_for_day(self, user_id: int, day: date) -> list[Entry]:
        start, end = self._day_bounds(day)
        return self._fetch("WHERE user_id=? AND eaten_at>=? AND eaten_at<?",
                           (user_id, start, end))

    def totals_by_day(self, user_id: int, days: int) -> list[tuple[date, int]]:
        today = datetime.now(self.tz).date()
        out = []
        for offset in range(days - 1, -1, -1):
            d = today - timedelta(days=offset)
            out.append((d, sum(e.calories for e in self.entries_for_day(user_id, d))))
        return out

    def today(self) -> date:
        return datetime.now(self.tz).date()

    # ---- helpers ----------------------------------------------------------

    def _day_bounds(self, day: date) -> tuple[str, str]:
        utc = ZoneInfo("UTC")
        start = datetime.combine(day, time.min, self.tz).astimezone(utc)
        end = datetime.combine(day + timedelta(days=1), time.min, self.tz).astimezone(utc)
        return start.isoformat(), end.isoformat()

    def _fetch(self, where: str, params: tuple) -> list[Entry]:
        rows = self.conn.execute(
            "SELECT id, eaten_at, item, calories, protein_g, carbs_g, fat_g, meal_id, meal"
            f" FROM entries {where} ORDER BY eaten_at, id", params).fetchall()
        return [Entry(r["id"], datetime.fromisoformat(r["eaten_at"]).astimezone(self.tz),
                      r["item"], r["calories"], r["protein_g"], r["carbs_g"], r["fat_g"],
                      r["meal_id"], r["meal"] or "snack") for r in rows]
