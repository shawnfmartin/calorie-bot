"""The in-chat help: an overview, plus one page per topic with how to set things up.

Telegram limits a message to 4,096 characters, so each topic is its own page.
"""

from __future__ import annotations

import re

OVERVIEW = """👋 <b>How this bot works</b>
Just talk to me in plain words, no commands needed. I log your food, work out calories and
macros, and keep track of your day.

<b>What I can do</b> (say “help” and the topic for details):
🍽 <b>help logging</b>: log food by text, photo or barcode, fix or remove entries
📋 <b>help day</b>: see today, a meal, yesterday, or a 7-day chart
🎯 <b>help goals</b>: work out your calorie and macro targets, coaching, weigh-ins
📖 <b>help recipes</b>: save recipes and log portions, raw vs cooked weight
⏰ <b>help reminders</b>: meal reminders and the Sunday summary
❤️ <b>help health</b>: send your days to Apple Health
🔒 <b>help limits</b>: what's free, daily limits, privacy

<b>Quick start</b>
1. Tell me what you ate: <i>2 eggs and toast for breakfast</i>
2. Say <i>set up my goals</i> to get your targets and coaching
3. Ask <i>how am I doing today?</i> or <i>summary</i> any time"""

TOPICS = {
    "logging": """🍽 <b>Logging food</b>

<b>Log a meal</b>: tell me what you ate, as specific as you like.
• <i>2 eggs and toast with butter for breakfast</i>
• <i>chipotle chicken bowl and a diet coke</i>
• <i>150g rice, 200g chicken breast, 10g olive oil</i>
• Send a <b>photo</b> of your plate; a caption makes it more accurate.
• Packaged food: send a photo of the <b>barcode</b> (or type its number). I look it up in Open
  Food Facts, show the label's numbers, and ask how much you had: <i>1 can</i>, <i>40g</i>,
  <i>2 bars</i>, <i>half the bag</i>. Calories come straight from the label.

<b>Meals</b>: say which (<i>for lunch</i>, <i>as a snack</i>), or I go by the time: before 11:00
breakfast, 11–16 lunch, 16–22 dinner, otherwise a snack. If you answer one of my reminders,
it goes to that meal.

<b>Fix things</b>
• <i>actually it was 2 eggs, not 3</i>: replaces the entry, keeps its time
• <i>remove the toast</i> / <i>I didn't eat the cookie</i>
• <i>undo</i>: removes the last thing you logged
• <i>the salad was actually dinner</i>: moves it to another meal
• <i>I skipped lunch</i>: marks it skipped (no reminder, and the day still counts)

<b>Questions</b>: <i>how much protein is in an egg?</i> gets a short answer and logs nothing.""",

    "day": """📋 <b>Your day and week</b>

<b>Today</b>: <i>today</i>, <i>what did I eat today?</i>, <i>how many calories left?</i>
Shows each meal with its total, your goal and what's left, and your macros (against your
targets once you've set up goals).

<b>One meal or another day</b>
• <i>what did I have for lunch?</i>
• <i>what did I eat yesterday?</i> · <i>dinner on Monday?</i> (up to 30 days back)

<b>7-day chart</b>: <i>summary</i> or <i>how did I do this week?</i>
A line chart of the last 7 days with your goal and average, plus averages, macros, and
your best and worst days.
Only <b>complete</b> days are charted: breakfast, lunch and dinner each logged or skipped.
Days with a meal missing are left out so they don't drag the chart down; the caption says
which meal was missing.

<b>End of the day</b>: when your last main meal is in, I tell you if you hit your target,
went over, or ate too little.""",

    "goals": """🎯 <b>Goals, targets and coaching</b>

<b>Set up</b>: say <i>set up my goals</i>, or answer in one message:
<i>lose weight, 34, male, 5'11, 190 lbs, desk job, gym 4 days a week, be tough on me</i>
Pounds, feet and stone are fine. I ask for anything missing.

<b>How I work it out</b> (the same formula most online calculators use):
• BMR (Mifflin-St Jeor) from weight, height, age and sex
• × activity level = what you burn a day (TDEE)
• minus ~550 kcal per 0.5 kg a week you want to lose (plus, to gain)
• protein 1.6–1.8 g per kg, fat 27%, carbs the rest
This becomes your daily goal. Say <i>my targets</i> to see it with the working.

<b>Safety limits</b>: weight loss is capped at 1 kg (or 1% of your weight) a week, calories
never go below 1,500 (men) / 1,200 (women), and there's no weight-loss plan under 18 or when
underweight. Estimates, not medical advice.

<b>Coaching</b>: after each meal, a sentence or two on how it fits your goal.
• <i>be encouraging</i> · <i>be tough on me</i> (blunt, never about your body) · <i>no coaching</i>

<b>Updates</b>
• <i>I weigh 83 kg today</i>: logs it and updates your targets
• <i>I'm more lightly active</i> · <i>I want to lose 0.25 kg a week</i>
• <i>set my goal to 1800</i>: sets the calorie goal by hand""",

    "recipes": """📖 <b>Recipes</b>

<b>Save one</b>: send the ingredients with amounts and say it's a recipe:
<i>save this as my chili, makes 6 servings:
500g ground beef, 2 cans kidney beans, 1 onion, 1 can tomatoes</i>
No name? I make one up. A pasted table works too.

<b>Log a portion</b>, worked out from the saved recipe, not re-estimated:
• by weight: <i>150g of the chili</i> · <i>2 tbsp of my dressing</i>
• by serving: <i>1 serving of chili</i>
• by share: <i>half the batch</i>
Items from a recipe are marked 📖.

<b>Raw vs cooked</b>: ingredient weights are raw. For cooked dishes I estimate the cooked
weight (chicken keeps ~75%, dry rice triples), so <i>100g of chicken</i> off your plate is
counted right. For exact numbers weigh the finished dish once:
<i>the chili weighed 1.6 kg cooked</i>. Say <i>100g raw …</i> for a raw weight.

<b>Manage</b>
• <i>show my recipes</i> · <i>show the chili</i>
• <i>the chili makes 8 servings</i>
• <i>rename it to Beef chili</i> · <i>delete the chili recipe</i> (I ask you to confirm)
• To change ingredients, send the recipe again with the same name.""",

    "reminders": """⏰ <b>Reminders and the weekly summary</b>

<b>Meal reminders</b>: if breakfast, lunch or dinner isn't logged by its time, I remind you
<b>once</b>. Defaults: breakfast 10:30, lunch 14:30, dinner 20:30.
• <i>reminders</i>: see yours
• <i>remind me about lunch at 1pm</i>
• <i>no breakfast reminders</i> · <i>turn off reminders</i> · <i>turn reminders back on</i>
• <i>I skipped dinner</i>: no reminder for it today
Reply to a reminder with your food and it goes to that meal.

<b>Weekly summary</b>: every Sunday at 9:00 I send a chart of your past 7 days.
• <i>send my weekly summary at 6pm</i>
• <i>turn off the weekly summary</i>

Each person's reminders are their own.""",

    "health": """❤️ <b>Apple Health</b>

I can send each finished day into the Health app on your iPhone: calories, protein, carbs and
fat for each meal.

<b>Set up</b>: say <i>set up Apple Health</i>. I reply with your personal link and the steps to
build a Shortcut. It takes a few minutes, once.
• The Shortcut runs when you open Telegram (iOS won't let anything write to Health while the
  phone is locked).
• Only finished days are sent, so today's meals appear tomorrow and fixes during the day
  never leave stray entries.
• Each day is sent once. The link works on your home Wi-Fi only; keep it private.
• Don't also log the same food straight into Health, or it counts twice.""",

    "limits": """🔒 <b>Limits, cost and privacy</b>

<b>Instant and free</b> (no Claude needed): <i>today</i>, <i>summary</i>, <i>undo</i>,
<i>help</i>, <i>reminders</i>, <i>my targets</i>, <i>my recipes</i>, hi/thanks/ok.

<b>Everything else</b> goes to Claude, which takes 5–10 seconds.
• I only handle food, nutrition and your log. Other requests get a one-line no.
• Each person has a daily limit of messages that need Claude (100 by default); the instant
  replies keep working past it.
• Very long messages are turned away.

<b>Privacy</b>
• Your log, recipes and goals are stored on the Pi this bot runs on.
• Messages pass through Telegram (bot chats aren't end-to-end encrypted) and go to Anthropic's
  Claude to be estimated.
• Only the Telegram accounts the owner allowed can use this bot, and each person's data is
  separate.""",
}

# Words that pick a topic, checked in order.
KEYWORDS = [
    ("recipes", r"recipe|portion|serving|batch|raw|cooked"),
    ("goals", r"goal|target|macro|coach|tone|tough|encourag|weigh|weight|tdee|bmr|profile|setup|set up"),
    ("reminders", r"remind|weekly|sunday|notification"),
    ("health", r"apple|health|shortcut|iphone"),
    ("day", r"today|yesterday|summary|chart|graph|week|day|history|progress"),
    ("limits", r"limit|cost|free|privacy|private|data|claude"),
    ("logging", r"log|food|meal|photo|picture|fix|undo|remove|delete|edit|skip|track"),
]
HELP_START = re.compile(r"^/?help\b", re.I)


def topic_for(text: str) -> str | None:
    """The help topic a message asks about, or None."""
    lowered = text.lower()
    return next((name for name, pattern in KEYWORDS if re.search(pattern, lowered)), None)


def quick_help(text: str) -> str | None:
    """Page for "help", "help recipes", "/help goals" and the like; None for anything else
    (Claude handles looser wording like "how do reminders work?")."""
    stripped = text.strip()
    if not HELP_START.match(stripped):
        return None
    rest = HELP_START.sub("", stripped, count=1)
    topic = topic_for(rest)
    return TOPICS[topic] if topic else OVERVIEW


def page(topic: str) -> str:
    return TOPICS.get(topic, OVERVIEW)
