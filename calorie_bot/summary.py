"""The 7-day summary: a line chart of daily calories, plus a short caption.

Only complete days are charted. A day is complete when breakfast, lunch and dinner are each
either logged or marked as skipped ("I skipped lunch"); snacks are optional. A day with a
meal missing would make the chart look like you ate less than you did, so it's left out
entirely, and the caption says which days were left out and why.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date, timedelta

import matplotlib

matplotlib.use("Agg")  # no screen on a Pi: draw straight to PNG
import matplotlib.pyplot as plt  # noqa: E402

from .db import REMINDER_MEALS, Store  # noqa: E402

DAYS = 7


@dataclass
class DayTotals:
    day: date
    kcal: int
    protein_g: float
    carbs_g: float
    fat_g: float


@dataclass
class Summary:
    start: date
    end: date
    complete: list[DayTotals] = field(default_factory=list)
    left_out: list[tuple[date, list[str]]] = field(default_factory=list)  # (day, missing meals)
    goal: int | None = None

    @property
    def average(self) -> float:
        return sum(d.kcal for d in self.complete) / len(self.complete) if self.complete else 0


def missing_meals(store: Store, user_id: int, day: date) -> list[str]:
    """Main meals with nothing logged and not marked skipped, in order."""
    logged = {e.meal for e in store.entries_for_day(user_id, day)}
    return [m for m in REMINDER_MEALS
            if m not in logged and store.reminder_done(user_id, day, m) != "skipped"]


def build(store: Store, user_id: int, end: date, days: int = DAYS) -> Summary:
    start = end - timedelta(days=days - 1)
    out = Summary(start=start, end=end, goal=store.get_goal(user_id))
    for k in range(days):
        d = start + timedelta(days=k)
        entries = store.entries_for_day(user_id, d)
        if not entries:
            continue  # nothing at all: not worth mentioning
        missing = missing_meals(store, user_id, d)
        if missing:
            out.left_out.append((d, missing))
            continue
        out.complete.append(DayTotals(
            d, sum(e.calories for e in entries), sum(e.protein_g for e in entries),
            sum(e.carbs_g for e in entries), sum(e.fat_g for e in entries)))
    return out


def chart_png(s: Summary) -> bytes:
    """Line chart of the complete days, on a 7-day axis so gaps are visible."""
    all_days = [s.start + timedelta(days=k) for k in range((s.end - s.start).days + 1)]
    by_day = {d.day: d.kcal for d in s.complete}
    # NaN for days not charted, so the line breaks there instead of bridging the gap.
    ys = [by_day.get(d, float("nan")) for d in all_days]
    xs = list(range(len(all_days)))
    values = [y for y in ys if y == y]

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=150)
    ax.plot(xs, ys, marker="o", linewidth=2.5, markersize=8, color="#2f7ed8", zorder=3,
            label="Calories (complete days)")
    for x, y in zip(xs, ys):
        if y == y:
            ax.annotate(f"{int(y):,}", (x, y), textcoords="offset points", xytext=(0, 11),
                        ha="center", fontsize=10, color="#333333", zorder=4,
                        bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.9))
    if s.goal:
        ax.axhline(s.goal, linestyle="--", linewidth=1.5, color="#e4572e", zorder=2,
                   label=f"Goal ({s.goal:,})")
    if s.complete:
        ax.axhline(s.average, linestyle=":", linewidth=1.4, color="#888888", zorder=1,
                   label=f"Average ({s.average:,.0f})")

    ax.set_xticks(range(len(all_days)))
    left_out = {d for d, _ in s.left_out}
    labels = [f"{d:%a}\n{d.day}" + ("\n(left out)" if d in left_out else "") for d in all_days]
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_xlim(-0.5, len(all_days) - 0.5)
    top = max(values + [s.goal or 0]) if values else (s.goal or 2000)
    ax.set_ylim(0, top * 1.25)
    ax.legend(loc="lower right", fontsize=9, frameon=True, framealpha=0.95)
    ax.set_ylabel("kcal")
    ax.set_title(f"Calories, {s.start:%b} {s.start.day} – {s.end:%b} {s.end.day}", fontsize=13)
    ax.grid(axis="y", alpha=0.3)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def caption(s: Summary, weekly: bool = False) -> str:
    head = "📊 <b>Your week</b>" if weekly else "📊 <b>Last 7 days</b>"
    n = len(s.complete)
    lines = [head]
    if n:
        lines.append(f"Average: <b>{s.average:,.0f} kcal</b>/day over {n} complete day"
                     + ("" if n == 1 else "s"))
        p = sum(d.protein_g for d in s.complete) / n
        c = sum(d.carbs_g for d in s.complete) / n
        f = sum(d.fat_g for d in s.complete) / n
        lines.append(f"Per day: P {p:.0f}g · C {c:.0f}g · F {f:.0f}g")
        hi = max(s.complete, key=lambda d: d.kcal)
        lo = min(s.complete, key=lambda d: d.kcal)
        if n > 1:
            lines.append(f"Highest {hi.day:%a} ({hi.kcal:,}) · lowest {lo.day:%a} ({lo.kcal:,})")
        if s.goal:
            under = sum(1 for d in s.complete if d.kcal <= s.goal)
            lines.append(f"At or under your {s.goal:,} goal on {under} of {n} days")
    if s.left_out:
        parts = [f"{d:%a} (no {' or '.join(m)})" for d, m in s.left_out]
        lines.append("Left out, meals missing: " + ", ".join(parts))
        lines.append("<i>Tell me you skipped a meal and that day will count.</i>")
    return "\n".join(lines)


def empty_text(s: Summary) -> str:
    text = ("📊 No complete days in the last 7 days yet. A day counts once breakfast, lunch "
            "and dinner are each logged, or you tell me you skipped one.")
    if s.left_out:
        parts = [f"{d:%a} (no {' or '.join(m)})" for d, m in s.left_out]
        text += "\nMissing: " + ", ".join(parts)
    return text
