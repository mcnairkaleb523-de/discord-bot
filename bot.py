from dotenv import load_dotenv
load_dotenv()

import os
import sys
import io
import re
import time
import asyncio
import json
import aiohttp
import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timedelta
from collections import deque
from typing import Union

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True
intents.invites = True
intents.presences = True

bot = commands.Bot(command_prefix=",", intents=intents, help_command=None)

# ── ,restart "back online" notification ───────────────────────
# os.execv wipes all in-memory state, so the only way the fresh process
# can know where to report back is through its own argv. Parsed once here;
# consumed once in on_ready's startup-only block below.
_RESTART_NOTIFY_CHANNEL_ID = None
_RESTART_NOTIFY_AT = None
if "--restart-notify" in sys.argv:
    try:
        _idx = sys.argv.index("--restart-notify")
        _RESTART_NOTIFY_CHANNEL_ID = int(sys.argv[_idx + 1])
        _RESTART_NOTIFY_AT = float(sys.argv[_idx + 2])
    except (IndexError, ValueError):
        pass


def _clean_restart_argv() -> list:
    """sys.argv minus any previous --restart-notify marker, so repeated
    ,restart calls don't accumulate duplicate flags forever."""
    args = list(sys.argv)
    if "--restart-notify" in args:
        i = args.index("--restart-notify")
        del args[i:i + 3]
    return args

BAD_WORDS = ["badword1", "badword2"]
LINKS = ["http", "https", "discord.gg"]

# Domains/extensions that count as a GIF for the automod GIF exemption below.
# Discord's built-in GIF picker can post links from any of these providers
# depending on client/region (Tenor and Klipy are both currently in use).
GIF_DOMAINS = ("tenor.com", "giphy.com", "gph.is", "klipy.com")


def _is_gif_message(message: discord.Message) -> bool:
    """True if this message is a GIF — a link from a known GIF provider (what
    Discord's built-in GIF picker posts), a direct .gif link, or a .gif attachment."""
    content = message.content.lower()
