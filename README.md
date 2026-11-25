# Calorie bot

A private Telegram bot that tracks your calories. Tell it what you ate in plain words, or
send a photo, and Claude works out the calories and macros. It keeps a daily log by meal,
remembers your recipes, and can sync each day into Apple Health. It runs on a Raspberry Pi
at home, and your food log stays in a SQLite file on the Pi.

```
You:  2 eggs and toast with butter for breakfast
Bot:  🍳 Logged for breakfast
      • 2 large eggs, scrambled in butter — 182 kcal
      • 1 slice whole wheat toast with 1 tsp butter — 115 kcal
      Meal: 297 kcal

      Today: 297 / 2,000 kcal (1,703 left)

You:  actually it was 3 eggs
Bot:  ✏️ Fixed
      • 2 large eggs, scrambled in butter — 182 kcal   (struck through)
      • 3 large eggs, scrambled in butter — 273 kcal

You:  what did I have for lunch yesterday?
Bot:  🥗 Lunch — 612 kcal
         • Chicken burrito bowl — 580
         • Diet Coke — 0
```

There are no commands to learn. Everything is a normal message.

## What it does

- **Logs food from text or photos**, broken into items with calories, protein, carbs and fat.
- **Breakfast, lunch, dinner and snacks:** taken from what you say, or from the time of day.
- **Fixes and questions in plain words:** *"remove the toast"*, *"the salad was dinner"*,
  *"undo"*, *"how am I doing today?"*, *"how did I do this week?"*,
  *"set my goal to 1800"*.
- **Your targets, worked out by talking:** say *"I want to lose weight"* and answer a few
  questions in plain words (pounds and feet are fine). The bot works out your daily
  calories and macros with the Mifflin-St Jeor formula, which most online calculators
  use, and shows how it got there.
- **Coaching on every meal**, encouraging or tough love, plus an end-of-day verdict:
  on target, over, or too far under.
- **Recipes:** save something you cook once, then log *"150g of the chili"*, *"1 serving"*
  or *"half the batch"*. Portions are worked out from the saved recipe, not re-estimated.
  It also handles **raw vs cooked weight**: you weigh chicken raw when you cook but
  cooked on your plate, so Claude estimates the cooked weight from typical cooking
  yields, and you can replace that with a real weighing.
- **Apple Health sync** through an iPhone Shortcut: each finished day goes in as
  calories, protein, carbs and fat per meal.
- **7-day chart:** ask for a *"summary"* and get a line chart of your last 7 days, and
  one arrives every Sunday. Days with a meal missing are left out, so they don't drag
  the chart down.
- **Meal reminders:** if breakfast, lunch or dinner isn't logged by its time, the bot
  reminds you once. Change times or turn them off in plain words.
- **More than one person:** each person gets their own log, recipes, goal and Health link.
- **Private:** it answers only the Telegram accounts you allow.

## How it works

```
Your phone ──Telegram──▶ Telegram's servers ◀──polls── Raspberry Pi
                                                     ├─ bot (Python)
                                                     ├─ SQLite: your log & recipes
                                                     └─ Claude: calorie estimates
```

The Pi connects out to Telegram, so the bot works from anywhere and you don't need to
open any ports. Each message goes to Claude, which works out what you meant (log food,
fix an entry, save a recipe, …) and estimates the food. The bot then stores it and
replies. Short messages like "today", "undo" or "help" are answered instantly, without
Claude.

Claude can run two ways (you pick during setup):

| | Claude Code on the Pi | Anthropic API |
|---|---|---|
| Uses | Your Claude Pro or Max subscription | An API key, billed per use |
| Cost | Nothing extra | About 1–2 cents per meal |
| Speed | 5–10 s per message | A few seconds faster |
| Pi needed | 4 GB RAM or more (Claude Code's requirement) | Any Pi that runs Python 3.9+ |

### Guardrails against wasted usage
- **Free replies:** greetings ("hi"), thanks and "ok", emoji, and the instant phrases
  ("today", "undo", "help", …) are answered without calling Claude.
- **Stays on topic:** Claude handles food, nutrition, recipes and your log only.
  Anything else (coding, trivia, homework, "ignore your instructions…") gets a one-line
  "I only help with food and nutrition" reply. A short nutrition question gets a
  one-sentence answer.
- **Daily cap:** each person gets 100 Claude calls a day by default (`CLAUDE_DAILY_LIMIT`),
  so a runaway chat can't use up your plan. Instant replies keep working past the cap.
- **Small calls:** messages over 3,000 characters are refused (`MAX_MESSAGE_CHARS`), and
  Claude runs at low effort (`ESTIMATE_EFFORT`), which is plenty for food estimates.

> Claude subscriptions are for one person's use. If several people will use your bot,
> consider the API option.

## Setup

You need:
- A Raspberry Pi (or any always-on Linux machine with systemd) running Raspberry Pi OS
  Bookworm or newer (Python 3.9+ and `git`).
- Telegram on your phone.
- Either a Claude Pro/Max subscription or an Anthropic API key
  ([platform.claude.com](https://platform.claude.com)).

### 1. Create the Telegram bot
In Telegram, message **@BotFather**, send `/newbot`, pick a name and a username ending in
`bot`, and copy the token it gives you. Treat the token like a password.

### 2. (Subscription option only) Install Claude Code on the Pi
```bash
curl -fsSL https://claude.ai/install.sh | bash
claude      # log in with your Claude account, then exit with /exit
```
On a Pi without a screen, the login shows a link to open on another device.

### 3. Install the bot
On the Pi:
```bash
git clone https://github.com/shawnfmartin/calorie-bot.git
cd calorie-bot
./install.sh
```
The installer:
1. Sets up Python and its packages in `.venv`.
2. Asks for your bot token (and checks it with Telegram), your timezone, and which way
   to run Claude.
3. Installs a systemd **user** service, so the bot starts at boot and restarts if it
   crashes. It doesn't need root. If it asks you to run `sudo loginctl enable-linger …`,
   do that once.
4. Waits for you to **send the bot a message**, then locks the bot to your Telegram
   account.

Then say hi to your bot, or *"help"* to see what it can do.

### Adding another person
Have them message the bot. It replies with their Telegram user id. Add it to
`ALLOWED_USER_IDS` in `.env`, comma-separated (`ALLOWED_USER_IDS=111111111,222222222`),
then run `systemctl --user restart calorie-bot`.

## Using it

| To… | Say something like |
|---|---|
| Log food | *"2 eggs and toast for breakfast"*, or send a photo |
| See today | *"what did I eat today?"*, *"how many calories left?"* |
| Ask about a meal | *"what did I have for lunch?"*, *"dinner yesterday?"* |
| Fix an entry | *"actually it was 2 eggs, not 3"*, *"the rice was 150g"* |
| Move to another meal | *"the salad was actually dinner"* |
| Remove an entry | *"remove the toast"*, *"undo"* |
| Work out targets | *"I want to lose weight"*, then age, height, weight and activity in any order |
| See targets | *"my targets"*, *"how much should I eat?"* |
| Log weight | *"I weigh 83 kg today"* (also updates your targets) |
| Coaching tone | *"be tough on me"*, *"be encouraging"*, *"no coaching"* |
| Set a goal by hand | *"set my goal to 2000"*, *"what's my goal?"*, *"no more goal"* |
| See the week (chart) | *"summary"*, *"/summary"*, *"how did I do this week?"* |
| Weekly summary | *"send my weekly summary at 6pm"*, *"turn off the weekly summary"* |
| Save a recipe | *"save this as my chili:"* followed by the ingredients |
| Manage recipes | *"show my recipes"*, *"rename it to …"*, *"delete the chili recipe"* |
| Apple Health | *"set up Apple Health"* |
| Reminders | *"remind me about lunch at 1pm"*, *"no breakfast reminders"*, *"turn off reminders"*, *"I skipped lunch"* |

The bot remembers your last few messages, so "it" means whatever you were just talking
about. Deleting a recipe asks you to reply "yes" first.

### Recipes
```
Save this as Dijon chicken, makes 4 servings:
Chicken breast   644 g
Dijon mustard     18 g
Soy sauce         12 g
Olive oil          5 g
```
Claude estimates each ingredient once, and the bot saves the totals. If you don't give a
name, it makes one up. Portions can be grams (*"100g dijon chicken"*), servings (*"1
serving"*) or a share of the batch (*"half the batch"*).

**Raw vs cooked weight:** ingredient amounts are taken as raw. For cooked dishes, Claude
also estimates the cooked weight (chicken breast keeps ~75% of its weight, dry rice
roughly triples), and portions in grams are read as cooked weight, as on your plate:

```
Dijon chicken: 879 kcal, 706 g raw → about 545 g cooked (estimated)
"100g dijon chicken"  →  879 × 100 / 545 = 161 kcal    (a raw-based count would say 124)
```
For exact numbers, weigh the finished dish once: *"the dijon chicken weighed 520 g after
cooking"*. Say *"100g raw …"* to log a raw weight instead.

### Targets and coaching
Tell the bot your goal and a little about yourself, over as many messages as you like.
It remembers what you've said and asks only for what's still missing:

```
You:  I want to lose weight
Bot:  To work out your calories and macros I still need your age, your height, ...
You:  im 34, male, 5 foot 11, 190 lbs
Bot:  ... I still need how active you are
You:  desk job but I lift 4 days a week. can I lose 2kg a week?
Bot:  🎯 Your targets
      2,120 kcal a day · protein 155 g · carbs 231 g · fat 64 g
      Plan: lose about 0.64 kg a week
      BMR (Mifflin-St Jeor): 1,824 kcal · TDEE (× activity): 2,827 kcal
      ⚠️ I capped the pace at 0.9 kg a week; faster loss is hard to sustain ...
```

The calculation is done in code, not by Claude, so it's exact and repeatable:
- **BMR (Mifflin-St Jeor):** 10 × kg + 6.25 × cm − 5 × age, then +5 for men or −161 for women.
- **TDEE:** BMR times an activity factor from 1.2 (sedentary) to 1.9 (very active).
- **Weight change:** about 7,700 kcal per kg. Losing 0.5 kg a week is about 550 kcal a day
  under TDEE.
- **Macros:** protein 1.6–1.8 g per kg, fat 27% of calories, carbs the rest.

**Safety limits:** the bot caps weight loss at 1 kg (or 1% of body weight) a week and 1,000
kcal a day, never sets calories below 1,500 (men) or 1,200 (women), and won't plan weight
loss for anyone under 18 or with a BMI under 18.5. These are estimates for healthy adults,
not medical advice.

The result becomes your daily goal. After each meal you get a sentence or two of
coaching in the tone you picked. **Encouraging** notices what went well. **Tough** is blunt
about poor choices and overeating, but it's about the food, never your body, and it never
tells you to skip meals or eat below your target. When breakfast, lunch and dinner are all
in, you get the day's verdict: on target, over, or more than 25% under, which gets a nudge
to eat enough. "I weigh 83 kg today" logs your weight and updates your targets.

### 7-day summary chart
*"Summary"* (or `/summary`, *"show my graph"*, *"how did I do this week?"*) sends a line
chart of the last 7 days, with your goal and average marked. The caption has your
average, macros per day, the highest and lowest days, and how many days you were at or
under your goal. Every Sunday at 9:00 the bot sends the same chart for the past week. Turn
it off or move it in plain words, or for everyone with `WEEKLY_SUMMARY` in `.env`.

**Only complete days are charted.** A day counts once breakfast, lunch and dinner are each
logged, or you said you skipped one. A day with a meal missing would make it look like you
ate less than you did, so it's left out: the line breaks there, and the caption says which
meal was missing. Snacks are optional.

### Meal reminders
By default the bot checks at 10:30 (breakfast), 14:30 (lunch) and 20:30 (dinner). If you
haven't logged anything for that meal yet, it sends one reminder. It never sends a second
one that day. "I skipped lunch" means no reminder for it. If you answer a reminder with
food, it's logged as that meal, even late at night. Reminders only go out within 2 hours
of their time, so a Pi that was off all afternoon won't send a stale one. Change the
defaults for everyone with `REMINDERS` in `.env`.

### Apple Health
Apple Health has no internet API, so the Pi can't write to it directly. A Shortcut on
your iPhone pulls from the Pi instead. Say *"set up Apple Health"* and the bot replies
with your personal link and step-by-step Shortcut instructions. The suggested automation
runs the Shortcut whenever you open Telegram, since iOS doesn't allow writing to Health
while the phone is locked.

- Only **finished days** are sent (yesterday and earlier, up to 30 days back). A Shortcut
  can't delete what it logged, so waiting until the day is over means fixes never leave
  stray entries. Today's meals show up tomorrow.
- Each day is sent **once**: the Shortcut confirms afterwards, so running it often is fine.
- The link works on your **home Wi-Fi only**, unless you set up something like Tailscale
  (see [TODO.md](TODO.md)). Keep it private: it returns that person's daily totals.
- Don't also log the same food straight into Health, or it counts twice.

## Running it

```bash
journalctl --user-unit calorie-bot -f      # live logs
systemctl --user restart calorie-bot       # restart after editing .env
git pull && ./install.sh --update          # update to the latest version
cp data/calories.db ~/calories-backup.db   # back up your log
```

If you edit the code on another computer, `./deploy.sh user@your-pi.local` copies it
to the Pi and runs the update there. The Pi keeps its own `.env` and `data/`.

### Settings (`.env`)
| Setting | What it does |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `ALLOWED_USER_IDS` | Telegram user ids allowed to use the bot, comma-separated |
| `TIMEZONE` | e.g. `America/New_York`; decides when "today" starts. Empty = the Pi's own |
| `ESTIMATOR` | `claude-code` (subscription) or `api` (needs `ANTHROPIC_API_KEY`) |
| `CLAUDE_MODEL` | Optional model for Claude Code, e.g. `opus` |
| `ESTIMATE_EFFORT` | How hard Claude Code thinks: `low` (default), `medium`, `high` |
| `CLAUDE_DAILY_LIMIT` | Claude calls per person per day, default `100`; `0` = no limit |
| `MAX_MESSAGE_CHARS` | Longest message the bot accepts, default `3000` |
| `CLAUDE_CODE_OAUTH_TOKEN` | Optional: a token from `claude setup-token` if the Pi's login expires |
| `HEALTH_PORT` | Apple Health sync port, default `8787`; `0` turns it off |
| `HEALTH_BASE_URL` | What the Health links point at; default `http://<hostname>.local:8787` |
| `WEEKLY_SUMMARY` | Sunday summary time, default `09:00`, or `off`. Each person can change theirs |
| `REMINDERS` | Default reminder times, e.g. `breakfast=10:30,lunch=14:30,dinner=20:30`, or `off`. Each person can change their own |
| `DB_PATH` | Where the SQLite file lives, default `data/calories.db` |

### Troubleshooting
- **The bot doesn't answer:** check `journalctl --user-unit calorie-bot -n 50`. "Ignoring
  message from unauthorized user" means that id isn't in `ALLOWED_USER_IDS`.
- **"Claude Code isn't logged in":** run `claude` on the Pi and log in again, or put a
  token from `claude setup-token` (valid for a year) in `.env` as `CLAUDE_CODE_OAUTH_TOKEN`.
- **Claude Code bills the API instead of your plan:** if `ANTHROPIC_API_KEY` is set,
  Claude Code uses it. The bot removes it before running Claude Code, but check your
  shell profile if you run `claude` by hand.
- **The bot stops when you log out:** run `sudo loginctl enable-linger $USER` once.
- **Apple Health link doesn't load:** your phone must be on the same Wi-Fi as the Pi,
  and the Pi must be reachable as `<hostname>.local` (set `HEALTH_BASE_URL` to its IP
  if not).

## Privacy
- Your log, recipes and goals stay in `data/calories.db` on your Pi.
- Your messages pass through Telegram. Bot chats aren't end-to-end encrypted, so
  Telegram can technically read them. They also go to Anthropic (Claude) to be estimated.
- The Apple Health endpoint is only reachable on your home network, behind a per-person
  secret link.

## Development
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests
```
- `calorie_bot/bot.py`: the message router. Instant replies for short phrases;
  everything else goes to Claude, which returns an intent the bot acts on.
- `calorie_bot/nutrition.py`: the prompts and JSON schema, and the two backends:
  `claude -p` with structured output, or the Anthropic API. Claude Code runs in a
  throwaway folder with no tools for text, and with only the Read tool for a photo.
- `calorie_bot/db.py`: SQLite storage, with upgrades for older databases on startup.
- `calorie_bot/reminders.py`: the once-a-minute check for meal reminders and the Sunday summary.
- `calorie_bot/targets.py`: the calorie and macro calculation and its safety limits.
- `calorie_bot/summary.py`: the 7-day summary and its matplotlib chart.
- `calorie_bot/health.py`: the Apple Health sync endpoint (standard-library web server).

## License
[MIT](LICENSE)
