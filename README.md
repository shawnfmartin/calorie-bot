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
| See the week | *"how did I do this week?"* |
| Fix an entry | *"actually it was 2 eggs, not 3"*, *"the rice was 150g"* |
| Remove an entry | *"remove the toast"*, *"undo"* |
| Set a goal | *"set my goal to 2000"*, *"what's my goal?"*, *"no more goal"* |
| Save a recipe | *"save this as my vinaigrette:"* and the ingredients |
| Manage recipes | *"show my recipes"*, *"show the vinaigrette"*, *"rename it to …"*, *"delete the chili recipe"* |
| Get help | *"help"* |

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
- **Cooked dishes:** if the finished weight differs from the raw ingredients (water cooks
  off), include it: *"weighed 1.8 kg cooked"*. You can also add it later: *"the chili
  weighed 1.6 kg after cooking"*. Portions in grams are then measured against that weight.
- **Changing a recipe:** send it again under the same name with the new ingredients.

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
- `calorie_bot/db.py`: SQLite. One row per food item, grouped by meal. "Today" is computed
  in your `TIMEZONE`, so a midnight snack counts toward the right day.

## Tests
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests
```
