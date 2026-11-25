# Calorie bot

A private Telegram bot that tracks your daily calories. You message it what you ate
("2 eggs, toast with butter and a latte") or send a photo. Claude splits the meal into
items and estimates calories and macros for each, and the bot keeps a running daily total
in SQLite. It runs 24/7 on your Raspberry Pi (`jarvis.local`).

Estimates run through **Claude Code on the Pi, logged in with your Claude Max plan**, so
there's no per-meal API bill. Meals count against your Max usage limits, the same limits
your Claude Code use draws from. Each estimate takes about 5–10 seconds.

```
You:  chicken burrito bowl from chipotle and a diet coke
Bot:  🍽 Logged
      • Chipotle chicken (4 oz) — 180 kcal
      • Cilantro-lime white rice (4 oz) — 210 kcal
      • ...
      • Diet Coke, 12 oz — 0 kcal
      Meal: 660 kcal

      Today: 1,420 / 2,000 kcal (580 left)
```

## Talking to it

There are no commands. Everything is a plain message:

| To… | Say something like |
|---|---|
| Log food | *"2 eggs and toast for breakfast"*, or send a photo |
| See today | *"what did I eat today?"*, *"how many calories left?"* |
| Ask about a meal | *"what did I have for lunch today?"*, *"dinner yesterday?"* |
| Move to another meal | *"the salad was actually dinner"* |
| See the week | *"how did I do this week?"* |
| Fix an entry | *"actually it was 2 eggs, not 3"*, *"the rice was 150g"* |
| Remove an entry | *"remove the toast"*, *"undo"* |
| Set a goal | *"set my goal to 2000"*, *"what's my goal?"*, *"no more goal"* |
| Save a recipe | *"save this as my vinaigrette:"* and the ingredients |
| Manage recipes | *"show my recipes"*, *"show the vinaigrette"*, *"rename it to …"*, *"delete the chili recipe"* |
| Sync to Apple Health | *"set up Apple Health"* |
| Get help | *"help"* |

**Meals:** every entry belongs to breakfast, lunch, dinner or a snack. Say which (*"for
lunch"*, *"as a snack"*), or the bot goes by the time of day: before 11:00 breakfast, 11:00
to 16:00 lunch, 16:00 to 22:00 dinner, otherwise a snack. "Today" is grouped by meal with a
subtotal for each:

```
🍳 Breakfast — 255 kcal
   • 3 large eggs — 215
   • ...
🥗 Lunch — 44 kcal
   • Dijon vinaigrette, 33 servings — 29
   • 59 g arugula — 15

Today: 299 kcal
```

Claude reads each message and works out what you want. It sees today's log, so a fix or
removal finds the right entry, and a fix keeps the entry's original time. The bot also
remembers your last few messages, so "it" means whatever you were just talking about. That
memory resets when the bot restarts. A few short phrases ("today", "undo", "help", "my
recipes") are answered instantly, without asking Claude. A message starting with `/` out of
habit ("/today") works the same as without it.

Deleting a recipe is the one thing that asks first: the bot replies *"Delete the recipe …?
Reply yes to delete it or no to keep it."*

## Recipes

Save something you make often once, then log portions of it by weight, by serving, or as a
share of the batch:

```
Save this as Dijon vinaigrette, makes 8 servings:
Dijon mustard            20 g
Apple cider vinegar      30 g
Water                    55 g
Extra virgin olive oil    5 g
Honey                     7 g
```

If you don't give a name, the bot makes up a short one, like "Honey dijon vinaigrette".
A made-up name never overwrites a recipe you already have; it gets a number instead, like
"… 2". If you give the name of a saved recipe, the bot updates that recipe and tells you
what the old numbers were. Meals you already logged keep their old numbers.

Claude estimates each ingredient once, and the bot saves the totals: 90 kcal for 120 g,
or 0.75 kcal/g. Later, a message like *"37g of the dijon vinaigrette on my salad for
lunch"* logs the vinaigrette at exactly 37 × 0.75 = 28 kcal, worked out from the saved
recipe, alongside Claude's estimate for the salad. Loose names work ("my vinaigrette"), as
do spoons and cups ("2 tbsp of my vinaigrette"), which are converted to grams. Recipe
items are marked 📖 in the reply.

You can log a portion of a recipe three ways:

| You say | How it's worked out |
|---|---|
| *"37g of the vinaigrette"*, *"2 tbsp of my vinaigrette"* | grams ÷ the batch's total weight |
| *"1 serving of chili"* | servings ÷ the servings the recipe makes |
| *"half the batch of chili"*, *"a quarter of the vinaigrette"* | that share of the whole batch |

- **Servings:** put `makes 6 servings` (or "serves 6", "totaling 107 servings") in the recipe when you save it, or add it later in a
  plain message: *"the vinaigrette makes 4 servings"*. If you log "1 serving" before any
  servings are set, Claude estimates a typical serving in grams of your recipe and says so
  in the reply.
- **Cooked dishes:** see below.
- **Changing a recipe:** send it again under the same name with the new ingredients.

### Raw ingredients, cooked portions

You weigh ingredients raw, but you weigh your plate cooked, and cooking changes the weight
without changing the calories. Chicken breast loses about a quarter of its weight as water,
while rice soaks up water and roughly triples. So when you save a recipe that gets cooked,
Claude also estimates each ingredient's cooked weight from typical cooking yields, and
portions are measured against the cooked total:

```
Dijon chicken: 879 kcal, 706 g raw → about 545 g cooked (estimated)
"100g dijon chicken for lunch"  →  879 × 100 / 545 = 161 kcal   (raw-based would say 124)
```

- **Weigh it for exact numbers:** *"the dijon chicken weighed 520 g after cooking"*
  replaces the estimate with your weight. You can also give it when saving: *"…weighed
  1.8 kg cooked"*.
- **Raw portions:** *"100g raw dijon chicken"* is measured against the raw weight instead.
- **Uncooked recipes** (dressings, salads, smoothies) just use the raw total.

## Apple Health

Apple Health has no internet API, so the Pi can't write to it. Instead, a Shortcut on your
iPhone pulls from the Pi and logs each meal into Health as calories,
protein, carbs and fat, timed at when you ate. Health's own nutrition views, including
the redesigned Health app's, then show it. Don't also log the same food directly in
Health, or it counts twice.

Say *"set up Apple Health"* to the bot. It replies with your personal sync link and the
Shortcut steps. Each person gets their own link, which works only on your home Wi-Fi.

- **Only finished days are sent** (yesterday and earlier, up to 30 days back). A Shortcut
  can't delete what it logged, so waiting for the day to end means fixes and undos never
  leave stray entries in Health. Today's meals appear tomorrow.
- **When it runs:** the suggested automation runs the Shortcut whenever you open Telegram.
  iOS won't let anything write to Health while the phone is locked, so a timed overnight
  automation would fail, but when you open Telegram the phone is unlocked. The Shortcut
  first checks it's on your home Wi-Fi, since the link only works there, and does
  nothing otherwise.
- **Nothing is sent twice.** After logging, the Shortcut calls the link's `ack_url`, and
  the Pi marks those days as done, so running it many times a day is harmless. Days
  missed while you're away catch up the next time it runs at home.
- **The link is a secret.** Anyone on your Wi-Fi who has it can read that person's daily
  totals. Set `HEALTH_PORT=0` in `.env` to turn the sync off.

What the link returns:

```json
{"samples": [{"day": "2026-11-24", "meal": "breakfast", "date": "Nov 24, 2026 at 9:42 AM",
              "kcal": 255, "protein_g": 22.0, "carbs_g": 6.0, "fat_g": 14.0}, ...],
 "days": ["2026-11-24"],
 "ack_url": "http://jarvis.local:8787/health/<token>/ack?days=2026-11-24"}
```

## Setup

The code is already deployed to `~/calorie-bot` on the Pi, with its `calorie-bot` service
installed. What's left:

### 1. Create the bot in Telegram
1. Message **@BotFather** in Telegram and send `/newbot`. Pick a name and username.
2. Copy the token it gives you.
3. The bot has no commands, so leave its command menu empty. If you set one earlier, send
   BotFather `/setcommands`, pick the bot, and reply `/empty` to clear it.

### 2. Add the token on the Pi
```bash
ssh shawn@jarvis.local
nano ~/calorie-bot/.env        # set TELEGRAM_BOT_TOKEN and TIMEZONE
systemctl --user restart calorie-bot
```

### 3. Keep it running after you log out and across reboots
The bot runs as a systemd *user* service, so it doesn't need root. To keep it running
when you're not logged in, and to start it at boot, run this once on the Pi:
```bash
loginctl enable-linger shawn      # already done; add sudo if it ever asks
```

### 4. Lock the bot to you
Message your bot. It replies with your Telegram user id. Put that id in `ALLOWED_USER_IDS`
in `~/calorie-bot/.env`, then run `systemctl --user restart calorie-bot`. Until you do
this, the bot answers no one. Keep it to just you: Anthropic's terms let you use your own
subscription through Claude Code, but not to serve other people.

## Running it

- **Logs:** `ssh shawn@jarvis.local journalctl --user-unit calorie-bot -f`
- **Restart:** `ssh shawn@jarvis.local systemctl --user restart calorie-bot`
- **Update after code changes:** run `./deploy.sh` on your Mac. Your `.env` and data on the
  Pi are kept. If you keep a `.env` next to `deploy.sh`, it's copied up and replaces the Pi's.
- **Backup:** `scp shawn@jarvis.local:calorie-bot/data/calories.db .`

The bot uses long polling, so the Pi needs outbound internet only. No port forwarding.

### If Claude Code's login expires
The bot uses the login Claude Code already has on the Pi (`claude auth status` shows it).
If estimates start failing with "isn't logged in", either run `claude` on the Pi and log
in again, or run `claude setup-token` and put the token it prints (good for a year) in
`.env` as `CLAUDE_CODE_OAUTH_TOKEN`.

Don't set `ANTHROPIC_API_KEY` for the bot unless you switch to API billing. The bot strips
it from Claude Code's environment anyway, because Claude Code would otherwise bill the API.

### Using an API key instead
Set `ESTIMATOR=api` and `ANTHROPIC_API_KEY=...` in `.env`. The bot then calls the
Anthropic API directly (`claude-opus-5-5`). That's faster but billed per token, at about
1–2 cents a meal.

## How it works

- `calorie_bot/bot.py`: one message router. Short phrases get instant replies; everything
  else goes to Claude, which returns an *intent* (log, fix, remove, goal, save recipe, …)
  and the bot acts on it.
- `calorie_bot/nutrition.py`: the estimate. With `ESTIMATOR=claude-code` the bot runs
  `claude -p` in a throwaway folder. The message goes in on stdin and a JSON schema comes back
  as structured output. Text runs get no tools, and photo runs may only read the photo. At
  most two estimates run at once, to spare the Pi's memory.
- `calorie_bot/health.py`: the Apple Health sync endpoint, a small standard-library web
  server on port 8787 that runs inside the bot's process.
- `calorie_bot/db.py`: SQLite. One row per food item, tagged with its meal and grouped by
  the message it came from. "Today" is computed
  in your `TIMEZONE`, so a midnight snack counts toward the right day.

## Tests
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests
```
