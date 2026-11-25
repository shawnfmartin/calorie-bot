"""Daily calorie and macro targets from a person's profile.

The maths is done here, in code, not by Claude, so it's exact and the same every time:

1. BMR with the Mifflin-St Jeor equation (what most online TDEE calculators use):
       10 x weight_kg + 6.25 x height_cm - 5 x age + 5     (male)
       10 x weight_kg + 6.25 x height_cm - 5 x age - 161   (female)
   For anyone who doesn't say male or female, the midpoint (-78) is used.
2. TDEE = BMR x activity factor (1.2 sedentary ... 1.9 very active).
3. Goal: about 7,700 kcal per kg of body weight, so losing 0.5 kg a week is a deficit of
   about 550 kcal a day. Gaining adds a surplus the same way.
4. Macros: protein by body weight (1.6-1.8 g/kg), fat 27% of calories, carbs the rest.

Safety limits, applied before anything is shown:
- weight loss is capped at 1 kg or 1% of body weight a week, and the daily deficit at
  1,000 kcal or 25% of TDEE, whichever is smaller;
- calories never go below 1,500 (male) / 1,200 (female) / 1,350 (unspecified);
- no weight-loss plan for anyone under 18 or with a BMI under 18.5: they get a
  maintenance target and a suggestion to talk to a doctor or dietitian;
- surplus capped at 500 kcal a day.
These are estimates for healthy adults, not medical advice.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ACTIVITY = {
    "sedentary": (1.2, "sedentary (desk job, little exercise)"),
    "light": (1.375, "lightly active (exercise 1-3 days a week)"),
    "moderate": (1.55, "moderately active (exercise 3-5 days a week)"),
    "active": (1.725, "very active (hard exercise 6-7 days a week)"),
    "very_active": (1.9, "extremely active (physical job plus training)"),
}
SEX_CONSTANT = {"male": 5, "female": -161, "": -78}
CALORIE_FLOOR = {"male": 1500, "female": 1200, "": 1350}
KCAL_PER_KG = 7700
DEFAULT_RATE = {"lose": 0.5, "gain": 0.25, "maintain": 0.0}

REQUIRED = ("age", "height_cm", "weight_kg", "activity", "goal")
FIELD_PROMPTS = {
    "age": "your age",
    "height_cm": "your height",
    "weight_kg": "your current weight",
    "activity": "how active you are (desk job? how often you exercise?)",
    "goal": "whether you want to lose, maintain or gain weight",
}


@dataclass
class Targets:
    bmr: float
    tdee: float
    calories: int
    protein_g: int
    fat_g: int
    carbs_g: int
    rate_kg_week: float               # signed: negative = losing
    notes: list[str] = field(default_factory=list)


def missing_fields(profile: dict) -> list[str]:
    return [f for f in REQUIRED if not profile.get(f)]


def bmi(profile: dict) -> float:
    h = profile["height_cm"] / 100
    return profile["weight_kg"] / (h * h)


def calculate(profile: dict) -> Targets:
    """Targets for a complete profile (see missing_fields)."""
    sex = profile.get("sex") or ""
    sex = sex if sex in SEX_CONSTANT else ""
    w, h, age = float(profile["weight_kg"]), float(profile["height_cm"]), int(profile["age"])
    factor = ACTIVITY.get(profile["activity"], ACTIVITY["sedentary"])[0]
    bmr = 10 * w + 6.25 * h - 5 * age + SEX_CONSTANT[sex]
    tdee = bmr * factor
    notes: list[str] = []

    goal = profile["goal"]
    rate = float(profile.get("rate_kg_week") or DEFAULT_RATE.get(goal, 0))
    if goal == "lose" and (age < 18 or bmi(profile) < 18.5):
        why = "you're under 18" if age < 18 else f"your BMI is {bmi(profile):.1f}, below 18.5"
        notes.append(f"I won't plan weight loss because {why}. This is a maintenance target; "
                     "a doctor or dietitian can help with anything more.")
        goal, rate = "maintain", 0.0

    if goal == "lose":
        max_rate = min(1.0, 0.01 * w)
        if rate > max_rate:
            notes.append(f"I capped the pace at {max_rate:.1f} kg a week; faster loss is hard to "
                         "sustain and costs muscle.")
            rate = max_rate
        deficit = rate * KCAL_PER_KG / 7
        max_deficit = min(1000, 0.25 * tdee)
        if deficit > max_deficit:
            deficit = max_deficit
            rate = deficit * 7 / KCAL_PER_KG
            notes.append(f"I limited the deficit to {deficit:.0f} kcal a day, about {rate:.2f} "
                         "kg a week.")
        calories = tdee - deficit
        floor = CALORIE_FLOOR[sex]
        if calories < floor:
            calories = floor
            rate = max(0.0, (tdee - floor) * 7 / KCAL_PER_KG)
            notes.append(f"Calories are set at the {floor:,} kcal minimum, so expect about "
                         f"{rate:.2f} kg a week.")
        signed_rate = -rate
    elif goal == "gain":
        surplus = min(500, rate * KCAL_PER_KG / 7)
        calories = tdee + surplus
        signed_rate = surplus * 7 / KCAL_PER_KG
    else:
        calories, signed_rate = tdee, 0.0

    calories = int(round(calories / 10) * 10)
    protein_per_kg = 1.6 if goal == "maintain" else 1.8
    protein = int(round(protein_per_kg * w))
    fat = int(round(calories * 0.27 / 9))
    carbs = int(max(0, round((calories - protein * 4 - fat * 9) / 4)))
    return Targets(bmr, tdee, calories, protein, fat, carbs, signed_rate, notes)


def profile_card(profile: dict, t: Targets) -> str:
    sex = profile.get("sex") or "not given"
    act = ACTIVITY.get(profile["activity"], ACTIVITY["sedentary"])[1]
    if t.rate_kg_week < 0:
        plan = f"lose about {-t.rate_kg_week:.2f} kg a week"
    elif t.rate_kg_week > 0:
        plan = f"gain about {t.rate_kg_week:.2f} kg a week"
    else:
        plan = "maintain your weight"
    lines = [
        "🎯 <b>Your targets</b>",
        f"<b>{t.calories:,} kcal</b> a day · protein {t.protein_g} g · carbs {t.carbs_g} g · "
        f"fat {t.fat_g} g",
        f"Plan: {plan}"
        + (f", toward {profile['target_weight_kg']:g} kg" if profile.get("target_weight_kg") else ""),
        "",
        "<b>How I got there</b>",
        f"You: {profile['age']} years, {sex}, {profile['height_cm']:g} cm, "
        f"{profile['weight_kg']:g} kg, {act}",
        f"BMR (Mifflin-St Jeor): {t.bmr:,.0f} kcal · TDEE (× activity): {t.tdee:,.0f} kcal",
    ]
    if t.rate_kg_week < 0:
        lines.append(f"Deficit: {t.tdee - t.calories:,.0f} kcal a day")
    elif t.rate_kg_week > 0:
        lines.append(f"Surplus: {t.calories - t.tdee:,.0f} kcal a day")
    lines += [f"⚠️ {n}" for n in t.notes]
    tone = profile.get("coach_tone") or "encouraging"
    lines += ["",
              f"Coaching on your meals: <b>{tone}</b>. Say “be tough on me”, “be encouraging” "
              "or “no coaching” to change it.",
              "<i>Estimates for healthy adults, not medical advice.</i>"]
    return "\n".join(lines)
