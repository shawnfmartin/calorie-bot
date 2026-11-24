"""Turn meal descriptions (text or photo) and recipes into calorie estimates via Claude.

Two backends, picked with the ESTIMATOR env var:

* ``claude-code`` (default): runs the Claude Code CLI headlessly (``claude -p``) under the
  Pi user's own Claude login, so estimates come out of your Claude subscription (e.g. Max).
* ``api``: calls the Anthropic API with ANTHROPIC_API_KEY (billed per token).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

API_MODEL = "claude-opus-5-5"

# ---- prompts and schemas -------------------------------------------------------------

RECIPE_RULES = """Estimate the calories and macros of each ingredient at the stated amount, and
convert each amount to grams (use typical weights for units like "1 cup", "2 cloves", "a pinch").
Water, salt and other zero-calorie ingredients still count toward the weight.
If the user states the finished weight (e.g. after cooking, "makes 1.2 kg"), put it in
`final_weight_g`; otherwise 0. If they state how many servings it makes, put that in `servings`;
otherwise 0.

For the recipe's `name`: if the user named it, use their name, and set `name_given` to true. If the
name matches one of their saved recipes (loosely), use the saved recipe's exact name: they are
updating it. If they gave no name, make up a short, plain title of two to four words from the
main ingredients and the kind of dish (e.g. "Honey dijon vinaigrette", "Beef and bean chili"),
and set `name_given` to false. Don't mention the name in `note`: the bot tells the user."""

MEAL_PROMPT = """You are the nutrition engine behind a personal calorie-tracking Telegram bot.
The user tells you (in text, a photo, or both) what they ate. Break it into individual food items
and estimate calories and macros for each, using typical portion sizes when none is given
(e.g. "a bowl of oatmeal" ~ 1 cup cooked). Use the user's quantities and brands when stated.
Name each item with its portion, e.g. "2 large eggs, scrambled in butter".

The user may have saved recipes; they are listed with the message as
"Name (whole batch: N g, M servings)". When an item is one
of those recipes (match loosely: "my vinaigrette", "the dijon dressing", typos), set `recipe` to
the saved recipe's exact name (without the part in parentheses) and give the amount eaten in exactly one of these, leaving the
other two at 0:
- `recipe_grams` for a weight (convert spoons, cups or other units to grams as best you can),
- `recipe_servings` for servings ("1 serving", "2 bowls" when they mean servings), but only if
  that recipe's servings are set; if they are "not set", estimate the grams of one typical
  serving of that dish, at most the whole batch, use `recipe_grams` instead, and say so in `note`,
- `recipe_fraction` for a share of the whole batch ("half the batch" = 0.5, "a quarter" = 0.25,
  "the whole thing" = 1).
Still fill in your own best calorie and macro estimate for the item; the bot replaces it with
the recipe's exact numbers. For items that are not a saved recipe, set `recipe` to "" and all
three amounts to 0.

Set `intent` to one of:
- "log_meal": the user is telling you what they ate or drank. Fill `items`.
- "save_recipe": the user wants to save, create, add or update a recipe for later (e.g. "make a
  recipe for...", "save this as...", or a bare ingredient list with amounts that reads like a
  recipe rather than a meal). Leave `items` empty and fill `recipe` as described below.
- "update_recipe": the user is adding or changing how many servings a saved recipe makes, or its
  finished weight, without giving ingredients (e.g. "my vinaigrette makes 4 servings", "the chili
  weighed 1.6 kg after cooking"). Leave `items` empty; set `recipe.name` to the saved recipe's
  exact name, `recipe.servings` and/or `recipe.final_weight_g` to the new values (0 for anything
  not mentioned), and no ingredients.
- "other": anything else (a greeting, a question, something unrelated). Leave `items` empty and
  put a short helpful reply in `note`.
When `intent` is "log_meal" or "other", set `recipe` to an empty name, no ingredients and zeros.

Recipe rules:
""" + RECIPE_RULES + """

Keep `note` to one short sentence about any assumption you made, or leave it empty."""

RECIPE_PROMPT = """You are the nutrition engine behind a personal calorie-tracking Telegram bot.
The user is saving a recipe so they can log portions of it later.
""" + RECIPE_RULES + """

Keep `note` to one short sentence about any assumption you made, or leave it empty.
If the message contains no ingredients, return an empty ingredients list and explain in `note`."""

_MACROS = {
    "calories": {"type": "number"},
    "protein_g": {"type": "number"},
    "carbs_g": {"type": "number"},
    "fat_g": {"type": "number"},
}
_MACRO_KEYS = ["calories", "protein_g", "carbs_g", "fat_g"]

_RECIPE_PROPS = {
    "name": {"type": "string"},
    "name_given": {"type": "boolean"},
    "ingredients": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"ingredient": {"type": "string"}, "grams": {"type": "number"},
                           **_MACROS},
            "required": ["ingredient", "grams", *_MACRO_KEYS],
            "additionalProperties": False,
        },
    },
    "final_weight_g": {"type": "number"},
    "servings": {"type": "number"},
}
_RECIPE_KEYS = list(_RECIPE_PROPS)

MEAL_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item": {"type": "string"},
                    **_MACROS,
                    "recipe": {"type": "string"},
                    "recipe_grams": {"type": "number"},
                    "recipe_servings": {"type": "number"},
                    "recipe_fraction": {"type": "number"},
                },
                "required": ["item", *_MACRO_KEYS, "recipe", "recipe_grams", "recipe_servings",
                             "recipe_fraction"],
                "additionalProperties": False,
            },
        },
        "intent": {"type": "string", "enum": ["log_meal", "save_recipe", "update_recipe", "other"]},
        "recipe": {"type": "object", "properties": _RECIPE_PROPS, "required": _RECIPE_KEYS,
                   "additionalProperties": False},
        "note": {"type": "string"},
    },
    "required": ["intent", "items", "recipe", "note"],
    "additionalProperties": False,
}

RECIPE_SCHEMA = {
    "type": "object",
    "properties": {**_RECIPE_PROPS, "note": {"type": "string"}},
    "required": [*_RECIPE_KEYS, "note"],
    "additionalProperties": False,
}

PHOTO_NAME = "meal.jpg"


class EstimateError(Exception):
    """Raised with a user-facing message when an estimate can't be produced."""


def meal_prompt(text: str | None, recipe_names: list[str] | None) -> str:
    prompt = text or "Here's a photo of what I ate."
    if recipe_names:
        prompt += "\n\nMy saved recipes: " + "; ".join(recipe_names)
    return prompt


class _Estimator:
    """Shared front end: each backend implements _structured()."""

    async def estimate(self, text: str | None = None, image: bytes | None = None,
                       image_type: str = "image/jpeg",
                       recipe_names: list[str] | None = None) -> dict:
        return await self._structured(MEAL_PROMPT, MEAL_SCHEMA,
                                      meal_prompt(text, recipe_names), image, image_type)

    async def estimate_recipe(self, text: str, name: str = "",
                              recipe_names: list[str] | None = None) -> dict:
        prompt = (f"Recipe name: {name}\n\n" if name else "") + text
        if recipe_names:
            prompt += "\n\nMy saved recipes: " + "; ".join(recipe_names)
        return await self._structured(RECIPE_PROMPT, RECIPE_SCHEMA, prompt, None, "image/jpeg")

    async def _structured(self, system: str, schema: dict, prompt: str,
                          image: bytes | None, image_type: str) -> dict:
        raise NotImplementedError


# ---- Claude Code CLI backend (uses your Claude subscription) -------------------------

def find_claude_bin() -> str:
    configured = os.environ.get("CLAUDE_BIN")
    if configured:
        return configured
    found = shutil.which("claude")
    if found:
        return found
    # The native installer puts it here; systemd services don't have it on PATH.
    return str(Path.home() / ".local" / "bin" / "claude")


class ClaudeCodeEstimator(_Estimator):
    def __init__(self, claude_bin: str | None = None, model: str | None = None,
                 timeout: float = 180, max_concurrent: int = 2):
        self.claude_bin = claude_bin or find_claude_bin()
        self.model = model if model is not None else os.environ.get("CLAUDE_MODEL") or None
        self.timeout = timeout
        self._slots = asyncio.Semaphore(max_concurrent)  # each run is a Node process; be kind to the Pi

    def build_command(self, system: str, schema: dict, with_image: bool) -> list[str]:
        cmd = [
            self.claude_bin, "-p",
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
            "--system-prompt", system,
            # Text-only by default; a photo run may only read files (the photo in its temp dir).
            "--tools", "Read" if with_image else "",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--disable-slash-commands",
        ]
        if with_image:
            cmd += ["--allowedTools", "Read"]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    @staticmethod
    def subprocess_env() -> dict[str, str]:
        env = dict(os.environ)
        # With either of these set, `claude -p` bills the API instead of your subscription.
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        env.pop("TELEGRAM_BOT_TOKEN", None)  # the CLI has no need for it
        return env

    async def _structured(self, system, schema, prompt, image, image_type) -> dict:
        if image is not None:
            prompt += (f"\n\n(A photo of the meal is in the file ./{PHOTO_NAME}. "
                       "Read it and base your estimate on it.)")
        cmd = self.build_command(system, schema, image is not None)

        async with self._slots:
            with tempfile.TemporaryDirectory(prefix="calorie-bot-") as workdir:
                if image is not None:
                    Path(workdir, PHOTO_NAME).write_bytes(image)
                stdout, stderr, code = await self._run(cmd, prompt, workdir)

        try:
            result = json.loads(stdout)
        except json.JSONDecodeError:
            log.error("claude exited %s with unparseable output. stderr: %s", code,
                      stderr.decode(errors="replace")[-2000:])
            raise EstimateError("Claude Code didn't return a result. Check the bot's logs.")

        log.info("claude -p took %sms, est. cost $%s", result.get("duration_ms"),
                 result.get("total_cost_usd"))
        structured = result.get("structured_output")
        if result.get("is_error") or not isinstance(structured, dict):
            detail = str(result.get("result") or result.get("subtype") or "unknown error")
            log.error("claude -p failed: %s", detail)
            if "log in" in detail.lower() or "login" in detail.lower():
                raise EstimateError("Claude Code on the Pi isn't logged in. "
                                    "Run `claude` on the Pi and log in again.")
            raise EstimateError(f"Claude Code couldn't estimate that: {detail[:300]}")
        return structured

    async def _run(self, cmd: list[str], prompt: str, workdir: str) -> tuple[bytes, bytes, int]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=workdir, env=self.subprocess_env(),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError:
            raise EstimateError(f"Claude Code isn't installed at {self.claude_bin}. "
                                "Set CLAUDE_BIN in .env.")
        try:
            # The prompt goes in on stdin so text like "-2 eggs" can't be read as a CLI flag.
            stdout, stderr = await asyncio.wait_for(proc.communicate(prompt.encode()),
                                                    self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise EstimateError("Claude Code took too long. Try again.")
        return stdout, stderr, proc.returncode


# ---- Anthropic API backend (pay per token with an API key) ---------------------------

class ApiEstimator(_Estimator):
    def __init__(self, client=None):
        import anthropic  # only needed for this backend
        self.anthropic = anthropic
        self.client = client or anthropic.AsyncAnthropic()

    async def _structured(self, system, schema, prompt, image, image_type) -> dict:
        anthropic = self.anthropic
        content: list[dict] = []
        if image is not None:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": image_type,
                "data": base64.standard_b64encode(image).decode()}})
        content.append({"type": "text", "text": prompt})

        try:
            response = await self.client.beta.messages.create(
                model=API_MODEL,
                max_tokens=8192,
                system=system,
                messages=[{"role": "user", "content": content}],
                output_config={"effort": "low",
                               "format": {"type": "json_schema", "schema": schema}},
                # Re-run on a fallback model if the request is ever declined by a safety classifier.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError:
            raise EstimateError("The bot's Anthropic API key is invalid. Check ANTHROPIC_API_KEY.")
        except anthropic.RateLimitError:
            raise EstimateError("Rate limited by the Anthropic API. Try again in a minute.")
        except anthropic.APIStatusError as e:
            log.exception("Anthropic API error")
            raise EstimateError(f"Anthropic API error ({e.status_code}). Try again shortly.")
        except anthropic.APIConnectionError:
            raise EstimateError("Couldn't reach the Anthropic API. Is the Pi online?")

        if response.stop_reason == "refusal":
            raise EstimateError("Claude declined to estimate that one. Try rephrasing.")
        if response.stop_reason == "max_tokens":
            raise EstimateError("That was too long to estimate in one go. Try splitting it up.")

        text_out = next((b.text for b in response.content if b.type == "text"), None)
        if text_out is None:
            raise EstimateError("Got an empty estimate back. Try again.")
        return json.loads(text_out)


def make_estimator() -> _Estimator:
    backend = os.environ.get("ESTIMATOR", "claude-code").strip().lower()
    if backend == "api":
        log.info("Estimating with the Anthropic API (%s)", API_MODEL)
        return ApiEstimator()
    if backend != "claude-code":
        raise SystemExit(f"Unknown ESTIMATOR={backend!r}; use 'claude-code' or 'api'.")
    est = ClaudeCodeEstimator()
    log.info("Estimating with Claude Code at %s (your Claude subscription)", est.claude_bin)
    return est
