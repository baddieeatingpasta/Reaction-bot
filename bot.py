import logging
import os
import re
from typing import Optional

import discord
import emoji as emoji_lib  # pip install -U emoji  (needs 2.0+)
from aiohttp import web
from discord import app_commands

TOKEN = os.environ["DISCORD_TOKEN"]
MAX_REACTIONS = 20

log = logging.getLogger("reactbot")

VS16 = "\ufe0f"  # variation selector, makes symbols render as emoji

# Each character maps to a list of emojis, tried in order.
# A repeated character uses the next one not already used on the message.
REACTION_MAP: dict[str, list[str]] = {
    chr(c): [chr(0x1F1E6 + c - ord("A"))]  # regional indicators 🇦-🇿
    for c in range(ord("A"), ord("Z") + 1)
}

ALTERNATES = {
    "A": "\U0001F170" + VS16,  # 🅰️
    "B": "\U0001F171" + VS16,  # 🅱️
    "O": "\U0001F17E" + VS16,  # 🅾️
    "P": "\U0001F17F" + VS16,  # 🅿️
    "I": "\u2139" + VS16,      # ℹ️
    "M": "\u24C2" + VS16,      # Ⓜ️
    "X": "\u274C",             # ❌
}
for _letter, _alt in ALTERNATES.items():
    REACTION_MAP[_letter].append(_alt)

REACTION_MAP.update({
    " ": ["\U0001F7E6"],                             # 🟦
    "!": ["\u2757", "\u203C" + VS16],                # ❗ ‼️
    "?": ["\u2753", "\u2049" + VS16],                # ❓ ⁉️
    "+": ["\u2795"],                                 # ➕
    "-": ["\u2796"],                                 # ➖
    "/": ["\u2797"],                                 # ➗
    "*": ["*" + VS16 + "\u20E3", "\u2716" + VS16],   # *️⃣ ✖️
    "#": ["#" + VS16 + "\u20E3"],                    # #️⃣
    "$": ["\U0001F4B2"],                             # 💲
})
for _d in "0123456789":
    REACTION_MAP[_d] = [_d + VS16 + "\u20E3"]        # 0️⃣-9️⃣

CUSTOM_EMOJI = re.compile(r"<a?:\w+:\d+>")


def tokenize(text: str):
    """Yield (kind, value) in order: 'custom', 'unicode' or 'char'."""
    spans = {}
    for m in CUSTOM_EMOJI.finditer(text):
        spans[m.start()] = (m.end(), "custom", m.group())
    for e in emoji_lib.emoji_list(text):
        spans[e["match_start"]] = (e["match_end"], "unicode", e["emoji"])

    i = 0
    while i < len(text):
        if i in spans:
            end, kind, value = spans[i]
            yield kind, value
            i = end
        else:
            yield "char", text[i]
            i += 1


def build_reactions(text: str, limit: int):
    """Return (reactions, dropped_chars, hit_cap)."""
    reactions, used, dropped = [], set(), []

    for kind, value in tokenize(text):
        if kind == "custom":
            emoji = discord.PartialEmoji.from_str(value)
            key = emoji.id
        elif kind == "unicode":
            emoji = key = value
        else:
            options = REACTION_MAP.get(value.upper())
            if options is None:
                continue  # unsupported character, ignore
            emoji = next((o for o in options if o not in used), None)
            if emoji is None:
                dropped.append(value.upper())  # no unused alternate left
                continue
            key = emoji

        if key in used:
            continue  # duplicate explicit emoji

        if len(reactions) >= limit:
            return reactions, dropped, True

        reactions.append(emoji)
        used.add(key)

    return reactions, dropped, False


async def apply_reactions(message: discord.Message, text: str) -> str:
    remaining = MAX_REACTIONS - len(message.reactions)
    if remaining <= 0:
        return "That message already has 20 reactions."

    reactions, dropped, capped = build_reactions(text, remaining)
    if not reactions:
        return "I couldn't find anything to react with."

    added = 0
    problems = []

    for emoji in reactions:
        try:
            await message.add_reaction(emoji)
            added += 1
        except discord.Forbidden as e:
            log.warning("Forbidden on message %s: %s", message.id, e)
            problems.append(
                "I'm missing permissions (Add Reactions, Read Message History, "
                "or Use External Emojis)."
            )
            break
        except discord.NotFound as e:
            if e.code == 10008:  # message was deleted
                problems.append("The message no longer exists.")
                break
            # 10014: unknown emoji (e.g. from a server the bot isn't in)
            log.warning("Unknown emoji %s: %s", emoji, e)
            problems.append(f"Couldn't use {emoji} (is the bot in that server?).")
        except discord.HTTPException as e:
            if e.code == 30010:  # max reactions reached
                capped = True
                break
            log.warning("Failed to add %s: %s %s", emoji, e.status, e.text)

    parts = [f"Added {added} reaction{'s' if added != 1 else ''} to {message.jump_url}."]
    if capped:
        parts.append("Stopped at Discord's 20-reaction limit.")
    if dropped:
        parts.append("No unused alternate for: " + " ".join(sorted(set(dropped))) + ".")
    parts.extend(problems)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Tiny web server so hosts that require an open port see the bot as healthy
# ---------------------------------------------------------------------------
async def health(request: web.Request) -> web.Response:
    return web.Response(text="Bot is online!")


async def start_web_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    log.info("Web server running on port %s", port)
    return runner


class ReactBot(discord.Client):
    def __init__(self):
        # Default intents only: no privileged intents needed.
        # Messages that @mention the bot still include their text.
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.web_runner: Optional[web.AppRunner] = None

    async def setup_hook(self):
        # Start the web server first so the port opens right away
        self.web_runner = await start_web_server()
        await self.tree.sync()

    async def close(self):
        if self.web_runner is not None:
            await self.web_runner.cleanup()
        await super().close()

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)


bot = ReactBot()


# ---------------------------------------------------------------------------
# Mention flow: reply to a message, @Bot <text>, bot reacts and deletes command
# ---------------------------------------------------------------------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or bot.user not in message.mentions:
        return

    # Must be a reply to the message you want reacted on
    if message.reference is None or message.reference.message_id is None:
        return

    # Text after the bot mention
    command = (
        message.content
        .replace(f"<@{bot.user.id}>", "")
        .replace(f"<@!{bot.user.id}>", "")
        .strip()
    )
    if not command:
        return

    # Resolve the message being replied to
    target = message.reference.resolved
    if not isinstance(target, discord.Message):
        try:
            target = await message.channel.fetch_message(message.reference.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            log.warning("Couldn't fetch replied-to message: %s", e)
            return

    # Delete the command so it isn't left in chat (needs Manage Messages)
    try:
        await message.delete()
    except (discord.Forbidden, discord.NotFound):
        log.info("Couldn't delete command message %s", message.id)

    result = await apply_reactions(target, command)
    log.info("Mention command on %s: %s", target.id, result)

    # Only speak up if something went wrong; the note removes itself
    if not result.startswith("Added"):
        try:
            await message.channel.send(result, delete_after=8)
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------------------
# Context menu: long-press / right-click a message -> Apps -> Add reactions
# ---------------------------------------------------------------------------
class ReactModal(discord.ui.Modal, title="Add reactions"):
    text = discord.ui.TextInput(
        label="Text, numbers, emojis",
        placeholder="hello 123 <:Cry:1525473214548017202> 😀",
        max_length=500,
    )

    def __init__(self, target: discord.Message):
        super().__init__()
        self.target = target

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await apply_reactions(self.target, self.text.value)
        await interaction.followup.send(result, ephemeral=True)


@bot.tree.context_menu(name="Add reactions")
async def react_menu(interaction: discord.Interaction, message: discord.Message):
    await interaction.response.send_modal(ReactModal(message))


# ---------------------------------------------------------------------------
# Slash command: /react message:<link or ID> text:<...>
# ---------------------------------------------------------------------------
async def resolve_message(
    interaction: discord.Interaction, ref: str
) -> Optional[discord.Message]:
    channel = interaction.channel
    if channel is None:
        return None

    m = re.search(r"(\d+)/?$", ref.strip())  # accepts an ID or a message link
    if not m:
        return None

    try:
        return await channel.fetch_message(int(m.group(1)))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        log.warning("Couldn't resolve target message: %s", e)
        return None


@bot.tree.command(name="react", description="Spell text with reactions on a specific message")
@app_commands.describe(
    message="Link or ID of the message (right-click it, Copy Message Link)",
    text="Letters, numbers, and emojis to react with",
)
async def react(interaction: discord.Interaction, message: str, text: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    target = await resolve_message(interaction, message)
    if target is None:
        await interaction.followup.send(
            "I couldn't find that message in this channel. "
            "Paste a message link or ID from this channel.",
            ephemeral=True,
        )
        return
    result = await apply_reactions(target, text)
    await interaction.followup.send(result, ephemeral=True)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    bot.run(TOKEN, log_handler=None)  # use our logging config instead of discord.py's
