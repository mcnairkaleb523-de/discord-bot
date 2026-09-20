from dotenv import load_dotenv
load_dotenv()

import os
import sys
import io
import re
import time
import uuid
import shutil
import asyncio
import json
import aiohttp
import discord
import yt_dlp
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
    if any(domain in content for domain in GIF_DOMAINS):
        return True
    if ".gif" in content:
        return True
    for att in message.attachments:
        if att.filename.lower().endswith(".gif") or (att.content_type and "gif" in att.content_type):
            return True
    return False

# ── Persistent config storage ─────────────────────────────────
# Plain JSON files under data/ so guild config, moderation records, vouch
# data, tickets, economy, giveaways, and chat/VC activity stats survive a
# bot restart. Purely session-scoped trackers (anti-spam/raid/nuke sliding
# windows, live temp-VC ownership) are intentionally left in-memory since
# they're meant to reset each run.
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)


def _data_path(name: str) -> str:
    return os.path.join(DATA_DIR, f"{name}.json")


def _save_data(name: str, value) -> None:
    try:
        with open(_data_path(name), "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2)
    except OSError:
        pass


def _load_data(name: str, default):
    path = _data_path(name)
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _dump_depth(d, depth):
    """Stringify dict keys (int -> str) for the outer `depth` levels, for JSON."""
    if depth <= 0 or not isinstance(d, dict):
        return d
    return {str(k): _dump_depth(v, depth - 1) for k, v in d.items()}


def _load_depth(d, depth):
    """Reverse of _dump_depth: restore int keys for the outer `depth` levels."""
    if depth <= 0 or not isinstance(d, dict):
        return d
    return {int(k): _load_depth(v, depth - 1) for k, v in d.items()}


def _dt_to_iso(entries):
    """Convert a list of dicts with a 'time' datetime field into JSON-safe form."""
    out = []
    for e in entries:
        e = dict(e)
        t = e.get("time")
        if hasattr(t, "isoformat"):
            e["time"] = t.isoformat()
        out.append(e)
    return out


def _dt_from_iso(entries):
    """Reverse of _dt_to_iso."""
    out = []
    for e in entries:
        e = dict(e)
        t = e.get("time")
        if isinstance(t, str):
            try:
                e["time"] = datetime.fromisoformat(t)
            except ValueError:
                pass
        out.append(e)
    return out


def _save_log_channel_overrides():
    _save_data("log_channel_overrides", _dump_depth(LOG_CHANNEL_OVERRIDES, 1))


def _save_warnings():
    _save_data("warnings", {
        str(gid): {str(uid): _dt_to_iso(entries) for uid, entries in udict.items()}
        for gid, udict in WARNINGS.items()
    })


def _load_warnings():
    raw = _load_data("warnings", {})
    return {
        int(gid): {int(uid): _dt_from_iso(entries) for uid, entries in udict.items()}
        for gid, udict in raw.items()
    }


def _save_staff_warnings():
    _save_data("staff_warnings", {
        str(gid): {str(uid): _dt_to_iso(entries) for uid, entries in udict.items()}
        for gid, udict in STAFF_WARNINGS.items()
    })


def _load_staff_warnings():
    raw = _load_data("staff_warnings", {})
    return {
        int(gid): {int(uid): _dt_from_iso(entries) for uid, entries in udict.items()}
        for gid, udict in raw.items()
    }


def _save_staff_strikes():
    _save_data("staff_strikes", {
        str(gid): {str(uid): _dt_to_iso(entries) for uid, entries in udict.items()}
        for gid, udict in STAFF_STRIKES.items()
    })


def _load_staff_strikes():
    raw = _load_data("staff_strikes", {})
    return {
        int(gid): {int(uid): _dt_from_iso(entries) for uid, entries in udict.items()}
        for gid, udict in raw.items()
    }


def _save_mod_history():
    _save_data("mod_history", {
        str(gid): {str(uid): _dt_to_iso(entries) for uid, entries in udict.items()}
        for gid, udict in MOD_HISTORY.items()
    })


def _load_mod_history():
    raw = _load_data("mod_history", {})
    return {
        int(gid): {int(uid): _dt_from_iso(entries) for uid, entries in udict.items()}
        for gid, udict in raw.items()
    }


def _save_tickets():
    _save_data("tickets", {
        "tickets":         _dump_depth(TICKETS, 2),
        "ticket_claimed":  _dump_depth(TICKET_CLAIMED, 1),
        "ticket_type":     _dump_depth(TICKET_TYPE, 1),
        "ticket_priority": _dump_depth(TICKET_PRIORITY, 1),
        "ticket_locked":   _dump_depth(TICKET_LOCKED, 1),
    })


def _load_tickets():
    raw = _load_data("tickets", {})
    return (
        _load_depth(raw.get("tickets", {}), 2),
        _load_depth(raw.get("ticket_claimed", {}), 1),
        _load_depth(raw.get("ticket_type", {}), 1),
        _load_depth(raw.get("ticket_priority", {}), 1),
        _load_depth(raw.get("ticket_locked", {}), 1),
    )


def _save_guild_ticket_types():
    _save_data("guild_ticket_types", {
        str(gid): {key: [label, desc, color.value] for key, (label, desc, color) in cats.items()}
        for gid, cats in GUILD_TICKET_TYPES.items()
    })


def _load_guild_ticket_types():
    raw = _load_data("guild_ticket_types", {})
    return {
        int(gid): {key: (label, desc, discord.Color(color_val)) for key, (label, desc, color_val) in cats.items()}
        for gid, cats in raw.items()
    }


def _save_vouches():
    _save_data("vouches", _dump_depth(VOUCHES, 2))


def _save_vouch_log():
    _save_data("vouch_log", {
        str(gid): {str(uid): _dt_to_iso(entries) for uid, entries in udict.items()}
        for gid, udict in VOUCH_LOG.items()
    })


def _load_vouch_log():
    raw = _load_data("vouch_log", {})
    return {
        int(gid): {int(uid): _dt_from_iso(entries) for uid, entries in udict.items()}
        for gid, udict in raw.items()
    }


def _save_vouch_config():
    _save_data("vouch_config", _dump_depth(VOUCH_CONFIG, 1))


def _save_role_vouch_pending():
    _save_data("role_vouch_pending", _dump_depth(ROLE_VOUCH_PENDING, 1))


def _save_protected_roles():
    _save_data("protected_roles", {str(gid): list(roles) for gid, roles in PROTECTED_ROLES.items()})


def _save_autorole():
    _save_data("autorole", {str(gid): roles for gid, roles in AUTOROLE.items()})


def _save_hard_banned():
    _save_data("hard_banned", _dump_depth(HARD_BANNED, 2))


def _save_role_snapshots():
    _save_data("role_snapshots", _dump_depth(ROLE_SNAPSHOTS, 2))


def _save_jail_role_snapshots():
    _save_data("jail_role_snapshots", _dump_depth(JAIL_ROLE_SNAPSHOTS, 2))


def _save_gif_exempt_role():
    _save_data("gif_exempt_role", _dump_depth(GIF_EXEMPT_ROLE, 1))


def _save_welcome_config():
    _save_data("welcome_config", _dump_depth(WELCOME_CONFIG, 1))


def _save_economy():
    _save_data("economy", _dump_depth(ECONOMY, 2))


def _save_cooldowns():
    _save_data("cooldowns", _dump_depth(COOLDOWNS, 2))


def _save_gamble_wins():
    _save_data("gamble_wins", _dump_depth(GAMBLE_WINS, 2))


def _save_jail_expiry():
    _save_data("jail_expiry", _dump_depth(JAIL_EXPIRY, 2))


def _save_milestones():
    _save_data("milestones", {
        "channels":   _dump_depth(_milestone_channel_overrides, 1),
        "last_fired": _dump_depth(_last_milestone_fired, 1),
    })


def _task_to_jsonable(t):
    t = dict(t)
    for field in ("created_at", "updated_at", "due_at"):
        v = t.get(field)
        if hasattr(v, "isoformat"):
            t[field] = v.isoformat()
    t["notes"] = _dt_to_iso(t.get("notes", []))
    return t


def _task_from_jsonable(t):
    t = dict(t)
    for field in ("created_at", "updated_at", "due_at"):
        v = t.get(field)
        if isinstance(v, str):
            try:
                t[field] = datetime.fromisoformat(v)
            except ValueError:
                pass
    t["notes"] = _dt_from_iso(t.get("notes", []))
    return t


def _save_tasks():
    _save_data("tasks", {
        "counter": _task_counter,
        "tasks": {str(gid): [_task_to_jsonable(t) for t in tasks] for gid, tasks in TASKS.items()},
    })


def _load_tasks():
    raw = _load_data("tasks", {"counter": 0, "tasks": {}})
    tasks = {
        int(gid): [_task_from_jsonable(t) for t in tlist]
        for gid, tlist in raw.get("tasks", {}).items()
    }
    return tasks, raw.get("counter", 0)


def _save_giveaways():
    _save_data("giveaways", {
        str(mid): {**{k: v for k, v in g.items() if k != "entries"}, "entries": sorted(g["entries"])}
        for mid, g in GIVEAWAYS.items()
    })


def _load_giveaways():
    raw = _load_data("giveaways", {})
    return {
        int(mid): {**{k: v for k, v in g.items() if k != "entries"}, "entries": set(g.get("entries", []))}
        for mid, g in raw.items()
    }


def _save_chat_stats():
    _save_data("chat_stats", _dump_depth(CHAT_STATS, 2))


def _save_vc_stats():
    _save_data("vc_stats", _dump_depth(VC_STATS, 2))


def _save_birthdays():
    _save_data("birthdays", _dump_depth(BIRTHDAYS, 2))


def _save_birthday_channels():
    _save_data("birthday_channels", _dump_depth(BIRTHDAY_CHANNEL_OVERRIDES, 1))


def _save_birthday_timezones():
    _save_data("birthday_timezones", _dump_depth(BIRTHDAY_TIMEZONES, 2))


def _save_birthday_last_announced():
    _save_data("birthday_last_announced", _dump_depth(_LAST_BIRTHDAY_ANNOUNCE, 2))


def _save_all_state() -> None:
    """Safety-net autosave for state that changes at many scattered call sites
    (economy, tasks, giveaways, ...). Immediate saves also happen right after
    the single-touchpoint config/moderation commands, so this mainly covers
    the high-churn stuff between those explicit saves."""
    _save_log_channel_overrides()
    _save_warnings()
    _save_staff_warnings()
    _save_staff_strikes()
    _save_mod_history()
    _save_tickets()
    _save_guild_ticket_types()
    _save_guild_ticket_formats()
    _save_vouches()
    _save_vouch_log()
    _save_vouch_config()
    _save_role_vouch_pending()
    _save_protected_roles()
    _save_high_staff_roles()
    _save_permitted_roles()
    _save_staff_rules()
    _save_staff_ticket_claims()
    _save_autorole()
    _save_hard_banned()
    _save_role_snapshots()
    _save_jail_role_snapshots()
    _save_gif_exempt_role()
    _save_vanity_role()
    _save_vanity_code_override()
    _save_polls()
    _save_raid_whitelist()
    _save_anti_raid_enabled()
    _save_raid_mode()
    _save_antinuke_whitelist()
    _save_invite_data()
    _save_verify_backup_guild()
    _save_unmute_vc_channels()
    _save_welcome_config()
    _save_boost_channel()
    _save_update_channel_overrides()
    _save_last_announced_version()
    _save_booster_roles()
    _save_exit_survey()
    _save_economy()
    _save_cooldowns()
    _save_gamble_wins()
    _save_jail_expiry()
    _save_milestones()
    _save_tasks()
    _save_giveaways()
    _save_chat_stats()
    _save_staff_activity()
    _save_staff_of_month_role()
    _save_most_active_staff_role()
    _save_staff_awards_channel()
    _save_staff_perks_text()
    _save_last_staff_award_month()
    _save_hall_of_fame_role()
    _save_sotm_title()
    _save_mvp_title()
    _save_sotm_lounge()
    _save_mvp_lounge()
    _save_titled_nick_base()
    _save_hall_of_fame_log()
    _save_mvp_role()
    _save_mvp_activity()
    _save_mvp_awards_channel()
    _save_last_mvp_award_week()
    _save_last_mvp_winner()
    _save_mvp_streak()
    _save_vc_stats()
    _save_temp_vcs()
    _save_birthdays()
    _save_birthday_channels()
    _save_birthday_timezones()
    _save_birthday_last_announced()


async def _autosave_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(30)
        _save_all_state()
        await _sweep_expired_staff_warnings()


# Maps each log key → the default text-channel name the bot will search for.
# No hardcoded IDs — the bot finds the channel by name inside the guild at runtime.
# Use ,setlogchannel <key> [#channel] to pin a specific channel per guild.
LOG_CHANNELS = {
    "vc":             "vc-logs",
    "messages":       "message-logs",
    "joins":          "join-logs",
    "leaves":         "leave-logs",
    "raids":          "raid-logs",
    "mod":            "mod-logs",
    "roles":          "role-logs",
    "boost":          "boost-logs",
    "jail":           "jail-logs",
    "nicknames":      "mod-logs",
    "role_create":    "role-logs",
    "role_delete":    "role-logs",
    "channel_create": "vc-logs",
    "channel_delete": "message-logs",
    "channel_update": "mod-logs",
    "emoji":          "emoji-logs",
    "stickers":       "sticker-logs",
    "bans":           "ban-logs",
    "kicks":          "kick-logs",
    "timeouts":       "timeout-logs",
    "strips":         "strip-logs",
    "lockdowns":      "mod-logs",
    "unlockdowns":    "mod-logs",
    "clears":         "mod-logs",
    "roleall":        "mod-logs",
    "verification":   "verification-logs",
    "warns":          "warn-logs",
    "tickets":        "ticket-logs",
    "mutes":          "mute-logs",
    "hides":          "mod-logs",
    "purges":         "purge-logs",
    "massroles":      "mod-logs",
    "invites":        "invite-logs",
    "vanity":         "vanity-logs",
    "polls":          "poll-logs",
    "vouches":        "vouch-logs",
    "staff":          "staff-logs",
}

# Per-guild channel overrides: LOG_CHANNEL_OVERRIDES[guild_id][key] = channel_id
# Set via ,setlogchannel — takes priority over the name-based lookup above.
LOG_CHANNEL_OVERRIDES: dict[int, dict[str, int]] = _load_depth(_load_data("log_channel_overrides", {}), 1)

WELCOME_CHANNEL = "welcome"
ANNOUNCEMENTS_CHANNEL = "announcements"
BOOST_CHANNEL = "boosts"

# BOOST_CHANNEL_OVERRIDES[guild_id] = channel_id — where the public boost
# thank-you message posts. Falls back to a channel named BOOST_CHANNEL.
BOOST_CHANNEL_OVERRIDES: dict[int, int] = _load_depth(_load_data("boost_channel", {}), 1)


def _save_boost_channel():
    _save_data("boost_channel", _dump_depth(BOOST_CHANNEL_OVERRIDES, 1))


def _resolve_boost_channel(guild: discord.Guild):
    channel_id = BOOST_CHANNEL_OVERRIDES.get(guild.id)
    if channel_id:
        ch = guild.get_channel(channel_id)
        if ch:
            return ch
    return discord.utils.get(guild.text_channels, name=BOOST_CHANNEL)


# ── Bot update announcements ────────────────────────────────────
# CHANGELOG is bumped by hand with each shipped update — append a new
# entry (version + what's new/fixed) whenever a change goes out. The bot
# posts the latest untold entry to each guild the next time it actually
# comes back online (see _announce_updates(), called once per real
# process start from on_ready — never on a bare gateway reconnect).
CHANGELOG = [
    {
        "version": "1.0",
        "new": [
            "Automatic update announcements — the bot now posts what's new/fixed here every time it comes back online after an update.",
        ],
        "fixed": [],
    },
    {
        "version": "1.1",
        "new": [
            "Staff warnings/strikes system (,staffwarn, ,staffstrike) with auto-termination at 3 strikes and 2-week warning expiry.",
            "New ,d command — staff shortcut to drag a member out of voice.",
            "A public web page to subscribe/get pricing without needing the bot in your server first.",
        ],
        "fixed": [
            "Hard-bans can no longer be undone by manually unbanning in Discord — only ,unhardban actually lifts one.",
            "Anti-nuke now instantly bans on rapid role/channel deletion, not just strips roles (mass-ban already did this).",
            "VC ban/kick/lock could be bypassed by Administrator permissions or the server owner — now enforced for everyone.",
            "Role/nickname/channel logs now show who made the change, not just what changed.",
            "This update-announcement system itself wasn't posting reliably — one guild's error could silently block it for every other server; now isolated and logged per guild.",
        ],
    },
    {
        "version": "1.2",
        "new": [
            "Verification now uses the real Discord OAuth flow — clicking Verify takes you to the site to confirm instead of just showing an in-Discord button.",
            "Jailed members can no longer see any voice channels or text channels besides #jail (,lockjailed).",
            "Jail now assigns each inmate a persistent inmate number and a cell number, and ,worktime lets them shave time off their sentence by solving a quick math problem.",
            "Members are now DM'd when they're released from jail, letting them know their roles were restored.",
        ],
        "fixed": [
            "Invite-log tracking was missing joins made through the server's vanity invite link (discord.gg/<code>) — those are now detected and logged too.",
            ",unlock wasn't actually reopening chat for unverified members — it now clears the Unverified role's block too, not just @everyone's.",
            ",unverify was showing a leftover old role name in its confirmation message instead of the real Unverified role.",
        ],
    },
]

# UPDATE_CHANNEL_OVERRIDES[guild_id] = channel_id — set via ,setupdatechannel.
# Falls back to a fuzzy name match (any channel with "announce"/"update"/
# "news"/"patch" in its name — doesn't have to be literally "announcements"),
# then the server's system channel, then the first channel the bot can post in.
UPDATE_CHANNEL_OVERRIDES: dict[int, int] = _load_depth(_load_data("update_channel", {}), 1)

# LAST_ANNOUNCED_VERSION[guild_id] = version string already posted there —
# prevents re-announcing the same update on every reconnect/restart.
LAST_ANNOUNCED_VERSION: dict[int, str] = {
    int(gid): v for gid, v in _load_data("last_announced_version", {}).items()
}


def _save_update_channel_overrides():
    _save_data("update_channel", _dump_depth(UPDATE_CHANNEL_OVERRIDES, 1))


def _save_last_announced_version():
    _save_data("last_announced_version", {str(gid): v for gid, v in LAST_ANNOUNCED_VERSION.items()})


def _resolve_update_channel(guild: discord.Guild):
    channel_id = UPDATE_CHANNEL_OVERRIDES.get(guild.id)
    if channel_id:
        ch = guild.get_channel(channel_id)
        if ch:
            return ch
    for ch in guild.text_channels:
        name = ch.name.lower()
        if any(kw in name for kw in ("announce", "update", "news", "patch")):
            if ch.permissions_for(guild.me).send_messages:
                return ch
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        return guild.system_channel
    for ch in guild.text_channels:
        if ch.permissions_for(guild.me).send_messages:
            return ch
    return None


def _build_update_embed(entry: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"🔧 TrapAI Updated — v{entry['version']}",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    if entry.get("new"):
        embed.add_field(name="✨ What's New", value="\n".join(f"• {x}" for x in entry["new"])[:1024], inline=False)
    if entry.get("fixed"):
        embed.add_field(name="🛠️ What Was Fixed", value="\n".join(f"• {x}" for x in entry["fixed"])[:1024], inline=False)
    embed.set_footer(text="TrapAI • Automatic update notice")
    return embed


async def _announce_updates():
    """Post the latest CHANGELOG entry to every guild that hasn't seen it
    yet — called once per real process start (on_ready, _startup_resumed
    guard), not on ordinary gateway reconnects. Runs as a fire-and-forget
    background task (asyncio.create_task, nothing awaits it), so an
    unhandled exception anywhere in here would otherwise vanish into an
    "exception was never retrieved" warning with no visible effect —
    every guild is isolated in its own try/except and every skip/failure
    is printed so a stuck deployment is actually debuggable from the
    Railway logs instead of just silently not posting."""
    if not CHANGELOG:
        return
    latest = CHANGELOG[-1]
    version = latest["version"]
    changed = False
    print(f"[update-announce] checking {len(bot.guilds)} guild(s) for changelog v{version}")
    for guild in bot.guilds:
        try:
            if _is_ticket_only_guild(guild):
                print(f"[update-announce] {guild.id} ({guild.name}): skipped — ticket-only guild")
                continue
            if LAST_ANNOUNCED_VERSION.get(guild.id) == version:
                print(f"[update-announce] {guild.id} ({guild.name}): already announced v{version}")
                continue
            channel = _resolve_update_channel(guild)
            if not channel:
                print(f"[update-announce] {guild.id} ({guild.name}): no channel found — "
                      "set one with ,setupdatechannel #channel")
                continue
            try:
                await channel.send(embed=_build_update_embed(latest))
            except (discord.Forbidden, discord.HTTPException) as e:
                print(f"[update-announce] {guild.id} ({guild.name}): send to #{channel.name} failed — {e}")
                continue
            LAST_ANNOUNCED_VERSION[guild.id] = version
            changed = True
            print(f"[update-announce] {guild.id} ({guild.name}): posted to #{channel.name}")
        except Exception as e:
            print(f"[update-announce] {guild.id} ({guild.name}): unexpected error — {e!r}")
            continue
    if changed:
        _save_last_announced_version()


# BOOSTER_ROLES[guild_id][user_id] = role_id — a booster's own custom
# cosmetic role, managed via ,br. Persisted so it survives restarts and so
# on_member_update can find + delete it if the member stops boosting.
BOOSTER_ROLES: dict[int, dict[int, int]] = _load_depth(_load_data("booster_roles", {}), 2)


def _save_booster_roles():
    _save_data("booster_roles", _dump_depth(BOOSTER_ROLES, 2))


async def _delete_booster_role(guild: discord.Guild, user_id: int, reason: str):
    """Remove a member's tracked booster role entry and delete the Discord
    role itself, if it still exists. Shared by the un-boost handler, member
    departure cleanup, and ,br delete."""
    guild_roles = BOOSTER_ROLES.get(guild.id)
    if not guild_roles:
        return
    role_id = guild_roles.pop(user_id, None)
    if role_id is None:
        return
    _save_booster_roles()
    role = guild.get_role(role_id)
    if role:
        try:
            await role.delete(reason=reason)
        except (discord.Forbidden, discord.HTTPException):
            pass


# EXIT_SURVEY_RESPONSES[guild_id] = [{user_id, username, reason, timestamp}, ...]
# Newest first, capped per guild. Populated by ExitSurveyView/ExitSurveyModal
# from the DM sent when a member leaves — see on_member_remove.
EXIT_SURVEY_RESPONSES: dict[int, list] = _load_depth(_load_data("exit_survey", {}), 1)
_EXIT_SURVEY_MAX_PER_GUILD = 200


def _save_exit_survey():
    _save_data("exit_survey", _dump_depth(EXIT_SURVEY_RESPONSES, 1))


async def _record_exit_survey(guild_id: int, user_id: int, username: str, reason: str):
    entries = EXIT_SURVEY_RESPONSES.setdefault(guild_id, [])
    entries.insert(0, {
        "user_id": user_id,
        "username": username,
        "reason": reason,
        "timestamp": time.time(),
    })
    del entries[_EXIT_SURVEY_MAX_PER_GUILD:]
    _save_exit_survey()

    guild = bot.get_guild(guild_id)
    if guild:
        await log(
            guild,
            "leaves",
            "📝 Exit Survey Response",
            f"**{username}** (`{user_id}`) told us why they left.",
            discord.Color.orange(),
            fields=[("💬 Reason", reason[:1024], False)]
        )

# ── Birthday tracker ──────────────────────────────────────────
# BIRTHDAYS[guild_id][user_id] = "MM-DD"
BIRTHDAYS: dict[int, dict[int, str]] = _load_depth(_load_data("birthdays", {}), 2)
# Per-guild override channel for birthday announcements; falls back to a
# channel named ANNOUNCEMENTS_CHANNEL, same convention as milestones.
BIRTHDAY_CHANNEL_OVERRIDES: dict[int, int] = _load_depth(_load_data("birthday_channels", {}), 1)
# BIRTHDAY_TIMEZONES[guild_id][user_id] = UTC offset in hours (float, e.g. -5, 5.5)
# Set via ,settimezone. Members who haven't set one default to UTC (0).
BIRTHDAY_TIMEZONES: dict[int, dict[int, float]] = _load_depth(_load_data("birthday_timezones", {}), 2)
# _LAST_BIRTHDAY_ANNOUNCE[guild_id][user_id] = "YYYY-MM-DD" — the last LOCAL
# date (in that member's own timezone) they were announced on, so a
# once-a-minute check doesn't repost and a bot restart still catches a
# birthday that fell during the downtime.
_LAST_BIRTHDAY_ANNOUNCE: dict[int, dict[int, str]] = _load_depth(_load_data("birthday_last_announced", {}), 2)

# Milestones that trigger an announcement (member counts)
MEMBER_MILESTONES = {
    10, 25, 50, 100, 150, 200, 250, 300, 400, 500,
    750, 1000, 1500, 2000, 2500, 3000, 4000, 5000,
    7500, 10000, 15000, 20000, 25000, 50000, 100000,
}

UNVERIFIED_ROLE = "🚫 Unverified"
VERIFIED_ROLE = "✅ Glock30 Member"
JAIL_ROLE = "🔒 Jailed"
MUTED_ROLE = "🔇 Muted"
# Ownership-tier role allowed to issue formal staff warnings/strikes —
# see the "STAFF DISCIPLINE" section below.
STAFF_DISCIPLINE_ROLE = "os"
# Reaching exactly this many strikes auto-terminates a staff member (all
# removable roles stripped, same exemptions as ,strip) — see ,staffstrike.
STAFF_TERMINATION_STRIKES = 3
# ,staffwarn entries older than this auto-expire — see _sweep_expired_staff_warnings().
STAFF_WARNING_EXPIRY_DAYS = 14

# ── Self-service unmute VC(s) ─────────────────────────────────
# UNMUTE_VC_CHANNELS[guild_id] = [channel_id, ...] — voice channels a member
# can join to instantly clear their own VC server mute/deafen, no staff
# needed. Set via ,setunmutevc; supports multiple channels (e.g. two
# duplicate "unmute me" VCs with a small user_limit for capacity/redundancy).
UNMUTE_VC_CHANNELS: dict[int, list] = _load_depth(_load_data("unmute_vc_channels", {}), 1)


def _save_unmute_vc_channels():
    _save_data("unmute_vc_channels", _dump_depth(UNMUTE_VC_CHANNELS, 1))

# ── Real Discord OAuth2 verification (optional) ──────────────
# When both of these are set, ,sendverify posts a genuine "Authenticate via
# Discord" link button that goes through Discord's actual OAuth consent
# screen, handled by the separately-hosted oauth_server.py (see that file
# for what it does and its own required env vars). When either is unset,
# ,sendverify falls back to the simpler in-Discord button (VerifyView)
# that grants the role directly with no external hosting required.
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_OAUTH_REDIRECT_URI = os.getenv("DISCORD_OAUTH_REDIRECT_URI")
OAUTH_VERIFY_ENABLED = bool(DISCORD_CLIENT_ID and DISCORD_OAUTH_REDIRECT_URI)

# Public URL of oauth_server.py's /commands page (the full interactive
# command reference site) — e.g. https://your-service.up.railway.app/commands
# Optional: if unset, ,help / ,cmds just omit the "browse online" link.
COMMANDS_SITE_URL = os.getenv("COMMANDS_SITE_URL")

# ── Ticket-only mode (optional) ───────────────────────────────
# Lets this same bot process/token show up as a lean, ticket-only "sales
# bot" in specific servers you don't otherwise manage (e.g. a FiveM
# server's Discord you're pitching the full bot to), while staying fully
# featured everywhere else — no second bot application/token needed, since
# a single bot account can be invited into any number of servers at once
# and this just changes its behavior per-guild.
#
# TICKET_ONLY_GUILD_IDS — comma-separated guild IDs that get the ticket-only
# treatment. TICKET_ONLY_MODE=true is a blanket override that applies it to
# EVERY guild instead (only useful if you really are running a second,
# separate process on its own token purely for this).
TICKET_ONLY_MODE = os.getenv("TICKET_ONLY_MODE", "false").strip().lower() in ("1", "true", "yes", "on")
TICKET_ONLY_GUILD_IDS = {
    int(g) for g in os.getenv("TICKET_ONLY_GUILD_IDS", "").split(",") if g.strip().isdigit()
}
# Optional per-guild nickname (e.g. "TrapAI Tickets") applied automatically
# in ticket-only guilds, so it visibly reads as its own dedicated bot there
# even though it's the same underlying bot account.
TICKET_ONLY_NICKNAME = os.getenv("TICKET_ONLY_NICKNAME")
# Commands that actually create/manage tickets — these are what paid
# access gates. subscribe/managesubscription/subscriptionstatus/help/cmds/
# ping stay usable regardless of subscription status, since a suspended
# server still needs to be able to pay to get back in.
TICKET_MANAGEMENT_COMMANDS = {
    "sendtickets", "addticketcategory", "removeticketcategory",
    "ticketcategories", "setticketformat", "claimticket", "closeticket",
    "setlogchannel",
}
TICKET_ONLY_ALLOWED_COMMANDS = TICKET_MANAGEMENT_COMMANDS | {
    "help", "cmds", "ping", "subscribe", "managesubscription", "subscriptionstatus",
}

# HOME_GUILD_IDS — comma-separated list of this bot's own server(s) (leave
# unset/empty to disable this check entirely, e.g. if you actually want to
# run the paid subscription model above for other servers). When set,
# on_guild_join immediately leaves any server that isn't in this list —
# closes off the free-invite path entirely rather than gating it behind
# ticket-only mode/billing. Only applies going forward, to NEW joins; it
# does not retroactively remove the bot from servers it's already in.
HOME_GUILD_IDS = {
    int(g) for g in os.getenv("HOME_GUILD_IDS", "").split(",") if g.strip().isdigit()
}

# FORCE_LEAVE_GUILD_IDS — comma-separated guild IDs to leave once, checked
# on every on_ready. HOME_GUILD_IDS only blocks NEW joins — it does nothing
# about guilds the bot is already in — so this is the one-time cleanup
# lever for those: set it, let the bot redeploy/reconnect once, then clear
# it again (leaving it set is harmless — once a guild's gone, on_ready just
# won't find it in bot.guilds anymore, so nothing repeats).
FORCE_LEAVE_GUILD_IDS = {
    int(g) for g in os.getenv("FORCE_LEAVE_GUILD_IDS", "").split(",") if g.strip().isdigit()
}

# ── Billing (optional — see oauth_server.py's BILLING section for the
# Stripe-backed subscription/lifetime purchase flow this talks to) ────────
# BILLING_API_URL — that service's own public URL, e.g.
# https://your-service.up.railway.app (same host as COMMANDS_SITE_URL).
# INTERNAL_API_SECRET — must match the SAME env var on that service exactly;
# authenticates bot.py's calls to its /internal/* endpoints. Leave either
# unset to run with billing disabled: ,subscribe / ,managesubscription /
# ,subscriptionstatus reply that billing isn't configured yet, and
# ticket-only guilds are NOT gated on subscription status (so a ticket-only
# deployment set up before billing existed keeps working as before).
BILLING_API_URL = os.getenv("BILLING_API_URL", "").rstrip("/")
INTERNAL_API_SECRET = os.getenv("INTERNAL_API_SECRET")
BILLING_CONFIGURED = bool(BILLING_API_URL and INTERNAL_API_SECRET)

# ── AI chat — @mention the bot anywhere to talk to it ──────────
# ANTHROPIC_API_KEY — from console.anthropic.com. Leave unset to run with
# this disabled entirely (mentioning the bot just does nothing extra).
# ANTHROPIC_MODEL — override to use a different Claude model without a
# code change (e.g. a cheaper one for higher chat volume).
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
AI_CHAT_ENABLED = bool(ANTHROPIC_API_KEY)

# CHAT_HISTORY[(guild_id, user_id)] = [{"role": "user"/"assistant", "content": str}, ...]
# In-memory only, deliberately not persisted — short-term conversational
# context, not a moderation record. Capped length, resets on restart.
CHAT_HISTORY: dict[tuple[int, int], list] = {}

_CHAT_SYSTEM_PROMPT = (
    "You are TrapAI, a Discord bot with a real personality, not a corporate "
    "assistant. You talk like an actual person hanging out in the server: "
    "casual, funny, a little cocky. You can roast people, clap back, and "
    "curse when someone's being dumb or trying to test you — respond like a "
    "witty, smart-mouthed friend, not customer support. Keep it SHORT like a "
    "real text message — one short sentence, maybe two, almost always. Don't "
    "explain yourself or add extra context nobody asked for. Only write a "
    "real paragraph when someone actually asks for a real explanation of "
    "something. Never use slurs or hate speech, never make real threats, and "
    "never sexualize minors — everything else is fair game."
)


async def _ask_ai(guild_id: int, user_id: int, user_message: str):
    """Returns the AI's reply, or None if unconfigured/it fails — callers
    treat None as "say nothing" rather than erroring out in chat."""
    if not AI_CHAT_ENABLED:
        return None
    history = CHAT_HISTORY.setdefault((guild_id, user_id), [])
    history.append({"role": "user", "content": user_message[:1000]})
    del history[:-10]

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": ANTHROPIC_MODEL,
        "system": _CHAT_SYSTEM_PROMPT,
        "messages": history,
        "max_tokens": 300,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    print(f"[ai-chat] Anthropic request failed ({resp.status}): {body[:500]}")
                    return None
                data = await resp.json()
    except aiohttp.ClientError as e:
        print(f"[ai-chat] Anthropic request errored: {e!r}")
        return None

    try:
        reply = data["content"][0]["text"].strip()
    except (KeyError, IndexError, TypeError):
        print(f"[ai-chat] Unexpected Anthropic response shape: {data!r}")
        return None
    if not reply:
        return None
    history.append({"role": "assistant", "content": reply})
    del history[:-10]
    return reply[:1900]

# Two products: "ticket_bot" is just the ticket/support system, sold as a
# subscription (Starter/Pro/Premium) or a one-time Lifetime purchase.
# "whole_bot" is the full bot -- every command, not just tickets -- sold
# ONLY as one-time purchases (Regular/Premium), never a subscription.
# Buying ANY whole_bot tier graduates a ticket-only guild out of the
# restriction entirely -- see _ticket_only_mode_gate.
BILLING_PRODUCTS = {
    "ticket_bot": {
        "label": "Ticket Bot",
        "tiers": {
            "starter":  {"label": "Starter",  "price": "$5/mo",  "best_value": False},
            "pro":      {"label": "Pro",      "price": "$10/mo", "best_value": False},
            "premium":  {"label": "Premium",  "price": "$20/mo", "best_value": False},
            "lifetime": {"label": "Lifetime", "price": "$75 one-time", "best_value": True},
        },
    },
    "whole_bot": {
        "label": "Whole Bot",
        "tiers": {
            "regular": {"label": "Regular", "price": "$30 one-time", "best_value": False},
            "premium": {"label": "Premium", "price": "$50 one-time", "best_value": True},
        },
    },
}
# Friendlier aliases accepted in ,subscribe for the product argument.
_PRODUCT_ALIASES = {
    "ticket": "ticket_bot", "ticketbot": "ticket_bot", "tickets": "ticket_bot",
    "whole": "whole_bot", "wholebot": "whole_bot", "full": "whole_bot", "fullbot": "whole_bot",
}
# Statuses that still count as paid access — "past_due" is the grace
# period: access continues while they have days left to fix payment.
_SUBSCRIPTION_ACTIVE_STATUSES = {"active", "lifetime", "past_due"}


def _resolve_product(name: str):
    name = (name or "").strip().lower()
    if name in BILLING_PRODUCTS:
        return name
    return _PRODUCT_ALIASES.get(name)

# _SUBSCRIPTION_CACHE[guild_id] = (status_dict, fetched_at) — short-TTL
# cache so a burst of commands in a ticket-only guild doesn't hit
# oauth_server.py once per message.
_SUBSCRIPTION_CACHE: dict[int, tuple] = {}
_SUBSCRIPTION_CACHE_TTL = 60


async def _billing_api_get(path: str):
    if not BILLING_CONFIGURED:
        return None
    headers = {"X-Internal-Secret": INTERNAL_API_SECRET}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BILLING_API_URL}{path}", headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
    except aiohttp.ClientError:
        return None


async def _billing_api_post(path: str, payload: dict):
    if not BILLING_CONFIGURED:
        return None, "Billing isn't configured on this bot yet."
    headers = {"X-Internal-Secret": INTERNAL_API_SECRET, "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{BILLING_API_URL}{path}", headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                if resp.status != 200:
                    return None, data.get("error", f"Request failed ({resp.status}).")
                return data, None
    except aiohttp.ClientError as e:
        return None, f"Couldn't reach the billing service: {e}"


async def _get_subscription_status(guild_id: int) -> dict:
    """{"status": ..., "tier": ..., ...} — {"status": "none"} if unconfigured,
    unreachable with nothing cached yet, or the guild has never paid."""
    cached = _SUBSCRIPTION_CACHE.get(guild_id)
    if cached and time.time() - cached[1] < _SUBSCRIPTION_CACHE_TTL:
        return cached[0]
    if not BILLING_CONFIGURED:
        return {"status": "none"}
    data = await _billing_api_get(f"/internal/subscription/{guild_id}")
    if data is None:
        # Unreachable — reuse a stale cached value rather than punishing a
        # paying customer for a transient network blip; only fall back to
        # "none" if we've never successfully fetched anything for them.
        return cached[0] if cached else {"status": "none"}
    _SUBSCRIPTION_CACHE[guild_id] = (data, time.time())
    return data


def _is_ticket_only_guild(guild) -> bool:
    if TICKET_ONLY_MODE:
        return True
    return bool(guild) and guild.id in TICKET_ONLY_GUILD_IDS


@bot.check
async def _home_guild_gate(ctx):
    """Belt-and-suspenders alongside on_guild_join's immediate leave() —
    guild.leave() isn't guaranteed to land before someone manages to type a
    command in the window right after an unauthorized invite, so this
    refuses every command outright in any guild that isn't in
    HOME_GUILD_IDS. No-op (always True) if HOME_GUILD_IDS isn't configured,
    and doesn't apply to DMs (ctx.guild is None there)."""
    if HOME_GUILD_IDS and ctx.guild and ctx.guild.id not in HOME_GUILD_IDS:
        return False
    return True


class TicketOnlyModeRestricted(commands.CheckFailure):
    """Raised when a non-ticket command is used in a ticket-only guild."""
    pass


class TicketSubscriptionInactive(commands.CheckFailure):
    """Raised when a ticket-management command is used in a ticket-only
    guild whose subscription isn't active/lifetime/in-grace."""
    pass


@bot.check
async def _ticket_only_mode_gate(ctx):
    if not _is_ticket_only_guild(ctx.guild):
        return True
    # A ticket-only guild that has since bought Whole Bot access graduates
    # out of the restriction entirely -- full command access, same as any
    # other server, without needing to touch TICKET_ONLY_GUILD_IDS.
    if BILLING_CONFIGURED:
        status_data = await _get_subscription_status(ctx.guild.id)
        if status_data.get("product") == "whole_bot" and status_data.get("status") in _SUBSCRIPTION_ACTIVE_STATUSES:
            return True
    if not ctx.command or ctx.command.qualified_name not in TICKET_ONLY_ALLOWED_COMMANDS:
        raise TicketOnlyModeRestricted()
    if ctx.command.qualified_name in TICKET_MANAGEMENT_COMMANDS and BILLING_CONFIGURED:
        status = (await _get_subscription_status(ctx.guild.id)).get("status")
        if status not in _SUBSCRIPTION_ACTIVE_STATUSES:
            raise TicketSubscriptionInactive()
    return True


# Whole Bot "Regular" ($30) gets the commands most servers actually use day
# to day; "Premium" ($50) unlocks everything else -- the systems that only
# some servers want (economy/gambling, giveaways, vouch/trading trust,
# birthdays, boost perks, staff task tracking) plus the advanced/riskier
# moderation tools (hardban, nuke, lockdown, mass-role, backups, etc.) and
# the temp-VC system. This only ever restricts a confirmed whole_bot+regular
# customer -- everyone else (no subscription at all, a ticket_bot customer,
# or whole_bot+premium) is untouched by this check.
WHOLE_BOT_PREMIUM_ONLY_COMMANDS = {
    # Advanced/higher-risk moderation
    "hardban", "unhardban", "hardbans", "nuke", "lockdown", "unlockdown",
    "raidmode", "strip", "trapwarn", "trapscan", "restart",
    # Jail & Anti-Raid (whole category)
    "jail", "unjail", "setupjail", "antiraid", "raidwhitelist",
    # Advanced role management
    "massrole", "massunrole", "restoreallroles", "protectedrole",
    # Voice Channels — temp VC system (whole category)
    "vclock", "vcunlock", "vchide", "vcshow", "vcname", "vclimit", "vcbitrate",
    "vcregion", "vckick", "vcban", "vcunban", "vcpermit", "vcmute", "vcunmute",
    "vcdeafen", "vcundeafen", "vctransfer", "vcclaim", "vcmod", "vcremovemod",
    "vcstats", "setupvc", "setunmutevc", "d",
    # Music (whole category)
    "play", "skip", "pause", "musicstop", "musicloop", "volume", "nowplaying", "np", "musicqueue",
    # Vouch / trust system (whole category)
    "vouch", "unvouch", "cancelvouch", "pendingvouches", "vouches",
    "vouchleaderboard", "vouchstats", "vouchconfig",
    # Giveaways & Polls (whole category)
    "giveaway", "giveawayend", "giveaways", "setgiveawayrole", "poll", "pollend",
    # Economy & Games (whole category)
    "balance", "jobs", "setjob", "daily", "weekly", "work", "rob", "give", "deposit", "withdraw",
    "leaderboard", "gamblers", "slots", "blackjack", "coinflip", "dice", "duel",
    "basketball", "archery", "cuppong", "8ball",
    "trivia", "hangman", "wordle", "tictactoe", "connect4", "checkers", "chess", "numguess", "rockpaperscissors", "highlow",
    "crash", "21questions", "games",
    # Birthdays (whole category)
    "birthday", "removebirthday", "setbirthday", "setbirthdaychannel",
    "birthdaylist", "settimezone",
    # Boosts & Vanity, incl. custom booster roles (whole category)
    "setboostchannel", "setvanitycode", "setvanityrole", "vanityconfig", "br", "setupdatechannel",
    # Staff Tools (whole category)
    "staffpsa", "task", "tasklist", "acceptstaff", "denystaff", "setstaffrules", "setstaffmeeting", "staffleaderboard", "staffstats",
    "staffwarn", "staffstrike", "staffwarnings", "staffstrikes", "clearstaffwarnings", "clearstaffstrikes",
    "setstaffofmonthrole", "setmostactiverole", "setstaffawardschannel", "setstaffperks", "crownstaff",
    "setmvprole", "setmvpawardschannel", "crownmvp", "sethalloffamerole", "setsotmtitle", "setmvptitle",
    "setsotmlounge", "setmvplounge", "halloffame",
    # Advanced admin/setup
    "backup", "restore", "listbackups", "deletebackup", "exportconfig",
    # Niche fun/utility
    "clearsnipe", "editsnipe", "quote",
}


class WholeBotPremiumRequired(commands.CheckFailure):
    """Raised when a Whole Bot "Regular"-tier guild tries a Premium-only command."""
    pass


@bot.check
async def _whole_bot_tier_gate(ctx):
    if not ctx.guild or not BILLING_CONFIGURED:
        return True
    if not ctx.command or ctx.command.qualified_name not in WHOLE_BOT_PREMIUM_ONLY_COMMANDS:
        return True
    status_data = await _get_subscription_status(ctx.guild.id)
    if status_data.get("product") != "whole_bot":
        return True  # not a whole_bot customer -- this check doesn't apply to them
    if status_data.get("status") not in _SUBSCRIPTION_ACTIVE_STATUSES:
        return True  # inactive/no access at all is handled elsewhere, not this check
    if status_data.get("tier") == "premium":
        return True
    raise WholeBotPremiumRequired()


async def _apply_ticket_only_nickname(guild):
    if not TICKET_ONLY_NICKNAME or not _is_ticket_only_guild(guild) or not guild.me:
        return
    if guild.me.nick == TICKET_ONLY_NICKNAME:
        return
    try:
        await guild.me.edit(nick=TICKET_ONLY_NICKNAME)
    except (discord.Forbidden, discord.HTTPException):
        pass

# VERIFY_BACKUP_GUILD[origin_guild_id] = backup_guild_id — where verified
# members get auto-joined via the guilds.join OAuth scope, set with
# ,setverifybackup. Passed through the OAuth `state` param so oauth_server.py
# never needs to share a database/filesystem with this bot.
VERIFY_BACKUP_GUILD: dict[int, int] = _load_depth(_load_data("verify_backup_guild", {}), 1)


def _save_verify_backup_guild():
    _save_data("verify_backup_guild", _dump_depth(VERIFY_BACKUP_GUILD, 1))


def _build_oauth_authorize_url(guild_id: int) -> str:
    backup_id = VERIFY_BACKUP_GUILD.get(guild_id)
    state = str(guild_id) if not backup_id else f"{guild_id}:{backup_id}"
    from urllib.parse import quote
    return (
        "https://discord.com/api/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={quote(DISCORD_OAUTH_REDIRECT_URI, safe='')}"
        "&response_type=code"
        "&scope=identify%20guilds.join"
        f"&state={quote(state, safe='')}"
    )

JOIN_TO_CREATE_CHANNEL_NAME = "➕ Create VC"
TEMP_VC_CATEGORY_NAME = "🎤 Private VCs"

# spam_tracker/spam_warnings are keyed by (guild_id, user_id) — NOT just
# user_id — so a member's message activity in one server can never combine
# with their activity in another to trigger a false spam warning/timeout.
spam_tracker = {}
spam_warnings = {}

SPAM_MESSAGE_LIMIT = 5
SPAM_TIME_WINDOW = 6
SPAM_WARNING_LIMIT = 3
SPAM_TIMEOUT_MINUTES = 5

# ── Anti-raid — all per-guild, since this bot runs across multiple servers
# at once and a join burst in one must never trigger a ban in another ──
# WHITELIST[guild_id] = {user_id, ...} — exempt from anti-raid auto-ban, persisted
WHITELIST: dict = _load_depth(_load_data("raid_whitelist", {}), 1)
for _gid in list(WHITELIST.keys()):
    WHITELIST[_gid] = set(WHITELIST[_gid])
# RAID_JOINS[guild_id] = [timestamp, ...] — sliding window, session-only on purpose
RAID_JOINS: dict = {}
# ANTI_RAID_ENABLED[guild_id] = bool, persisted, defaults to True via .get()
ANTI_RAID_ENABLED: dict = _load_depth(_load_data("anti_raid_enabled", {}), 1)
RAID_TIME = 15
RAID_LIMIT = 5

# RAID_MODE[guild_id] = {"active": bool, "prev_verification_level": int,
# "prev_antiraid": bool} — persisted so a bot restart mid-raid-mode still
# knows what to restore to on ,raidmode off. prev_* fields snapshot the
# server's state from BEFORE raid mode was activated, taken once at
# activation time, so deactivating always returns to the real prior
# settings rather than a hardcoded default.
RAID_MODE: dict = _load_depth(_load_data("raid_mode", {}), 1)


def _save_raid_whitelist():
    _save_data("raid_whitelist", {str(gid): list(uids) for gid, uids in WHITELIST.items()})


def _save_anti_raid_enabled():
    _save_data("anti_raid_enabled", _dump_depth(ANTI_RAID_ENABLED, 1))


def _save_raid_mode():
    _save_data("raid_mode", _dump_depth(RAID_MODE, 1))

# VC_STATS[guild_id][user_id] = total_seconds_in_vc (all-time, persisted)
VC_STATS: dict[int, dict[int, int]] = _load_depth(_load_data("vc_stats", {}), 2)
# vc_join_time[(guild_id, user_id)] = unix timestamp of when their current VC
# session started. Keyed by (guild_id, user_id), NOT just user_id, so being in
# voice in two servers at once can't overwrite/misattribute either session.
# Session-scoped on purpose — only used to compute elapsed time for the session
# in progress, which then gets folded into VC_STATS when it ends.
vc_join_time = {}

# Persisted (not just in-memory) — without this, a bot restart while a temp
# VC still has people in it forgets the channel was ever "temp," so the
# empty-channel auto-delete never fires for it again and it's orphaned
# forever. See _sweep_temp_vcs() for the startup pass that also catches VCs
# that emptied out entirely while the bot was offline.
temp_vc_owners = _load_depth(_load_data("temp_vc_owners", {}), 1)
temp_vc_text_channels = _load_depth(_load_data("temp_vc_text_channels", {}), 1)
# vc_banned[vc_id] = {user_id, ...}  — users explicitly banned from a VC
vc_banned = {int(k): set(v) for k, v in _load_data("vc_banned", {}).items()}
# vc_mods[vc_id] = {user_id, ...}  — users with VC-mod privileges
vc_mods = {int(k): set(v) for k, v in _load_data("vc_mods", {}).items()}
# vc_kicked[vc_id] = {user_id, ...}  — users blocked from rejoining after
# ,vckick, cleared automatically once the VC owner themselves leaves the
# call (see the "owner left" branch in on_voice_state_update). Deliberately
# NOT owner-scoped/persisted across VC recreation like vc_owner_bans — a
# kick is only meant to last for the current session, not forever.
vc_kicked = {int(k): set(v) for k, v in _load_data("vc_kicked", {}).items()}
# vc_locked = {vc_id, ...}  — VCs currently locked via ,vclock. Tracked
# separately from the connect=False overwrite on @everyone because that
# overwrite alone doesn't actually stop anyone with guild-level
# Administrator (or the server owner, who bypasses all permission checks
# entirely) from walking in anyway — see the active-enforcement check in
# on_voice_state_update.
vc_locked = set(_load_data("vc_locked", []))
# vc_owner_bans[guild_id][owner_id] = {user_id, ...}  — persists across the
# owner's temp VC being deleted and recreated. vc_banned above is scoped to
# one specific channel ID, which gets wiped when that (empty) temp VC is
# torn down — without this, a banned user could just wait the VC out and
# rejoin the owner's next one.
vc_owner_bans: dict[int, dict[int, set]] = {
    int(gid): {int(uid): set(banned) for uid, banned in owners.items()}
    for gid, owners in _load_data("vc_owner_bans", {}).items()
}
# vc_owner_mods[guild_id][owner_id] = {user_id, ...}  — same idea as
# vc_owner_bans, for VC-mod grants: vc_mods is scoped to one channel
# instance and gets wiped when that temp VC empties out and is deleted.
vc_owner_mods: dict[int, dict[int, set]] = {
    int(gid): {int(uid): set(mods) for uid, mods in owners.items()}
    for gid, owners in _load_data("vc_owner_mods", {}).items()
}


def _save_temp_vcs():
    _save_data("temp_vc_owners", _dump_depth(temp_vc_owners, 1))
    _save_data("temp_vc_text_channels", _dump_depth(temp_vc_text_channels, 1))
    _save_data("vc_banned", {str(vid): list(users) for vid, users in vc_banned.items()})
    _save_data("vc_kicked", {str(vid): list(users) for vid, users in vc_kicked.items()})
    _save_data("vc_locked", list(vc_locked))
    _save_data("vc_owner_bans", {
        str(gid): {str(uid): list(banned) for uid, banned in owners.items()}
        for gid, owners in vc_owner_bans.items()
    })
    _save_data("vc_owner_mods", {
        str(gid): {str(uid): list(mods) for uid, mods in owners.items()}
        for gid, owners in vc_owner_mods.items()
    })
    _save_data("vc_mods", {str(vid): list(users) for vid, users in vc_mods.items()})

# ── Music (command-only: ,play) ─────────────────────────────
# Everything below is deliberately in-memory only, like CHAT_HISTORY
# for the AI chat feature — queue/now-playing state is meaningless after
# a restart anyway, since the bot isn't connected to any voice channel
# anymore at that point.
# MUSIC_QUEUES[guild_id] = [track_dict, ...] — up next, in order.
MUSIC_QUEUES: dict[int, list] = {}
# MUSIC_NOW_PLAYING[guild_id] = track_dict currently playing, or absent.
MUSIC_NOW_PLAYING: dict[int, dict] = {}
# MUSIC_VOLUME[guild_id] = 0.0-2.0 (PCMVolumeTransformer scale), default 0.5.
MUSIC_VOLUME: dict[int, float] = {}
# MUSIC_LOOP[guild_id] = bool — repeat the current track instead of
# advancing the queue.
MUSIC_LOOP: dict[int, bool] = {}
# MUSIC_TEXT_CHANNEL[guild_id] = channel_id — the channel the most recent
# request came from, so the Now Playing panel has somewhere to post
# (requests can come from any channel, there's no dedicated music channel).
MUSIC_TEXT_CHANNEL: dict[int, int] = {}
# MUSIC_IDLE_TASK[guild_id] = asyncio.Task — pending auto-disconnect after
# the queue empties out; cancelled the moment something new starts playing.
MUSIC_IDLE_TASK: dict[int, "asyncio.Task"] = {}
# MUSIC_LOCKS[guild_id] = asyncio.Lock — guards the "append to queue, then
# start playback if nothing's already playing" check in _music_enqueue.
# Without it, two ,play commands landing in the same event-loop tick can
# both see "nothing playing yet" and both call vc.play(), and discord.py's
# VoiceClient raises on the second one.
MUSIC_LOCKS: dict[int, "asyncio.Lock"] = {}


def _music_lock(guild_id: int):
    return MUSIC_LOCKS.setdefault(guild_id, asyncio.Lock())


# yt-dlp does the actual blocking network/extraction work, so every call
# to it is pushed into a thread executor (see _ytdl_extract) — it must
# never be awaited directly on the event loop.
#
# YOUTUBE_COOKIES (optional) — a real logged-in YouTube session's
# cookies.txt contents, set as a Railway variable. Without it, YouTube's
# "Sign in to confirm you're not a bot" wall blocks extraction outright
# on most cloud/datacenter IPs (confirmed in production — neither the
# android nor ios client alone was enough). Written to a local file once
# at startup since yt-dlp needs a file path, not the raw text.
_YOUTUBE_COOKIES_FILE = None
_youtube_cookies_raw = os.getenv("YOUTUBE_COOKIES")
if _youtube_cookies_raw:
    try:
        _YOUTUBE_COOKIES_FILE = "/tmp/yt_cookies.txt"
        with open(_YOUTUBE_COOKIES_FILE, "w") as _f:
            _f.write(_youtube_cookies_raw)
    except OSError as e:
        print(f"[music] Couldn't write YOUTUBE_COOKIES to disk: {e!r}")
        _YOUTUBE_COOKIES_FILE = None

# bgutil-ytdlp-pot-provider — a companion Railway service (separate from
# this bot) that generates the proof-of-origin (PO) token YouTube now
# requires for actual stream URLs, on top of the bot-check that cookies
# alone handle. Reached over Railway's private network; overridable via
# env var in case the service ever gets renamed/moved.
_BGUTIL_POT_BASE_URL = os.getenv("BGUTIL_POT_BASE_URL", "http://bgutil-pot-provider.railway.internal:4416")


class _YTDLLogger:
    """Forwards yt-dlp's own internal diagnostic trail (which client it's
    using, PO-token requests, format list results) into Railway logs —
    quiet=True alone hides all of this, which made every YouTube-side
    failure a guessing game instead of a quick log check."""
    def debug(self, msg):
        if msg.startswith("[debug] "):
            return
        print(f"[music/yt-dlp] {msg}")

    def info(self, msg):
        print(f"[music/yt-dlp] {msg}")

    def warning(self, msg):
        print(f"[music/yt-dlp] WARNING: {msg}")

    def error(self, msg):
        print(f"[music/yt-dlp] ERROR: {msg}")


_YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "logger": _YTDLLogger(),
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
    "extract_flat": False,
    # Forcing player_client to ["web", "android"] backfired: with cookies
    # set, yt-dlp skips "android" outright ("does not support cookies"),
    # leaving only "web" -- whose only downloadable format under the
    # current PO-token/SABR rules turned out to be legacy progressive
    # itag 18, which googlevideo's CDN 403s for non-browser requests
    # regardless of headers (confirmed in production). yt-dlp's own
    # maintained default client mix is BASE_CLIENTS = ('tv', 'web',
    # 'mweb', 'android', 'ios') -- notably including "tv", which needs no
    # PO token at all for GVS and still supports cookies. Dropping our
    # override lets yt-dlp pick from that full set instead of the
    # narrower one that produced the broken format.
    "extractor_args": {
        "youtubepot-bgutilhttp": {"base_url": [_BGUTIL_POT_BASE_URL]},
    },
    # yt-dlp's library API only enables the "deno" JS runtime default when
    # driven through its own CLI parser — going through YoutubeDL directly
    # (as here) registers NO js_runtimes unless explicitly listed, so the
    # node@22 installed on this container was never actually being used
    # for signature/n-parameter solving until this was added.
    "js_runtimes": {"node": {}},
    # The node-compatible variant of yt-dlp's challenge-solver script isn't
    # vendored in the package itself (only the deno/bun variants are) — it
    # has to be fetched from yt-dlp's own GitHub releases at runtime, which
    # is opt-in via remote_components. Without this, node runs but has no
    # actual solver script to execute, so every format request still fails.
    "remote_components": ["ejs:github"],
}
if _YOUTUBE_COOKIES_FILE:
    _YTDL_OPTS["cookiefile"] = _YOUTUBE_COOKIES_FILE
_YTDL = yt_dlp.YoutubeDL(_YTDL_OPTS)
try:
    for _rt_name, _rt in _YTDL._js_runtimes.items():
        _rt_info = _rt.info if _rt else None
        print(f"[music] JS runtime {_rt_name!r}: {_rt_info!r}")
except Exception as e:
    print(f"[music] JS runtime check failed: {e!r}")
_FFMPEG_OPTS = "-vn"
_MUSIC_IDLE_TIMEOUT = 300  # seconds of an empty queue before auto-leaving

# ffmpeg fetching googlevideo's signed CDN URLs directly kept 403ing in
# production — confirmed across multiple format/client combinations, with
# headers verified correct — while yt-dlp's own downloader handles the
# exact same URLs fine. Downloading through yt-dlp first (into this
# scratch dir) sidesteps whatever fetcher-level mismatch was causing that,
# at the cost of a short download delay before playback starts instead of
# true streaming. Wiped and recreated on startup in case of an unclean
# shutdown; each file is deleted the moment its track stops playing.
_MUSIC_CACHE_DIR = "/tmp/music_cache"
shutil.rmtree(_MUSIC_CACHE_DIR, ignore_errors=True)
os.makedirs(_MUSIC_CACHE_DIR, exist_ok=True)


def _ytdl_download(webpage_url: str, dest_template: str):
    """Blocking — always run through loop.run_in_executor, never awaited
    directly. Downloads the track's best audio to disk, reusing the same
    cookies/PO-token/JS-runtime pipeline as extraction. Returns the local
    file path, or None on failure."""
    opts = dict(_YTDL_OPTS, outtmpl=dest_template, logger=_YTDLLogger())
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(webpage_url, download=True)
            return ydl.prepare_filename(info)
    except Exception as e:
        print(f"[music] yt-dlp download failed for {webpage_url!r}: {e!r}")
        return None

_SPOTIFY_TRACK_RE = re.compile(r"open\.spotify\.com/(?:intl-\w+/)?track/([A-Za-z0-9]+)", re.I)


async def _resolve_spotify_title(url: str):
    """Spotify's oEmbed endpoint is public and needs no API key/auth —
    used purely to turn a Spotify link into a "song name - artist" string
    we can then search for on YouTube, since yt-dlp can't play Spotify's
    DRM-protected streams directly."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://open.spotify.com/oembed", params={"url": url},
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("title")
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None


def _ytdl_extract(query: str):
    """Blocking — always run through loop.run_in_executor, never awaited
    directly. Returns a single info dict, or None if nothing was found."""
    try:
        info = _YTDL.extract_info(query, download=False)
    except Exception as e:
        print(f"[music] yt-dlp extraction failed for {query!r}: {e!r}")
        return None
    if info is None:
        return None
    if "entries" in info:
        entries = [e for e in info["entries"] if e]
        if not entries:
            return None
        info = entries[0]
    return info


async def _resolve_track(query: str, member: discord.Member):
    query = query.strip()
    if _SPOTIFY_TRACK_RE.search(query):
        title = await _resolve_spotify_title(query)
        if not title:
            return None
        search_query = title
    else:
        search_query = query

    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, _ytdl_extract, search_query)
    if not info or not info.get("url"):
        return None
    return {
        "title": (info.get("title") or "Unknown title")[:100],
        "webpage_url": info.get("webpage_url", search_query),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "requester": member.mention,
        "requester_id": member.id,
    }


async def _ensure_voice_client(member: discord.Member):
    """Join (or move to) the member's current VC. None if they're not in
    one, the bot can't connect, or the bot is already serving another
    channel that still has real (non-bot) listeners in it — so a ,play
    from a different channel doesn't yank the bot away from people it's
    already playing for. Once that other channel empties out, it's fair
    game to move."""
    if not member.voice or not member.voice.channel:
        return None
    channel = member.voice.channel
    vc = member.guild.voice_client
    if vc and vc.channel.id == channel.id:
        return vc
    if vc:
        if any(not m.bot for m in vc.channel.members):
            return None
        try:
            await vc.move_to(channel)
        except (discord.ClientException, asyncio.TimeoutError):
            return None
        return vc
    try:
        return await channel.connect()
    except (discord.ClientException, discord.Forbidden, asyncio.TimeoutError):
        return None


def _cancel_idle_disconnect(guild: discord.Guild):
    task = MUSIC_IDLE_TASK.pop(guild.id, None)
    if task and not task.done():
        task.cancel()


async def _idle_disconnect_after(guild: discord.Guild):
    try:
        await asyncio.sleep(_MUSIC_IDLE_TIMEOUT)
    except asyncio.CancelledError:
        return
    vc = guild.voice_client
    if vc and not vc.is_playing() and not vc.is_paused():
        try:
            await vc.disconnect()
        except discord.HTTPException:
            pass
    MUSIC_IDLE_TASK.pop(guild.id, None)


def _schedule_idle_disconnect(guild: discord.Guild):
    _cancel_idle_disconnect(guild)
    MUSIC_IDLE_TASK[guild.id] = asyncio.create_task(_idle_disconnect_after(guild))


async def _post_now_playing(guild: discord.Guild, track: dict):
    channel_id = MUSIC_TEXT_CHANNEL.get(guild.id)
    channel = guild.get_channel(channel_id) if channel_id else None
    if not channel:
        return
    embed = discord.Embed(
        title="🎶 Now Playing",
        description=f"**[{track['title']}]({track['webpage_url']})**",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    if track.get("thumbnail"):
        embed.set_thumbnail(url=track["thumbnail"])
    if track.get("duration"):
        m, s = divmod(int(track["duration"]), 60)
        embed.add_field(name="⏱️ Duration", value=f"{m}:{s:02d}", inline=True)
    embed.add_field(name="🙋 Requested by", value=track["requester"], inline=True)
    queue_len = len(MUSIC_QUEUES.get(guild.id, []))
    if queue_len:
        embed.add_field(name="📜 Up Next", value=f"{queue_len} more queued", inline=True)
    embed.set_footer(text="TrapAI Music • ,play <song name or link>")
    try:
        await channel.send(embed=embed, view=MusicControlView())
    except (discord.Forbidden, discord.HTTPException):
        pass


async def _play_next(guild: discord.Guild):
    """Safe to call redundantly from multiple places at once (a fresh
    request that finds the queue idle, AND the `after` callback of a
    track that just finished) — the lock plus the is_playing()/is_paused()
    check make every call but the one that should actually start
    something a no-op, instead of racing into two vc.play() calls."""
    vc = guild.voice_client
    if not vc:
        MUSIC_NOW_PLAYING.pop(guild.id, None)
        return

    async with _music_lock(guild.id):
        if vc.is_playing() or vc.is_paused():
            return

        if MUSIC_LOOP.get(guild.id) and guild.id in MUSIC_NOW_PLAYING:
            track = MUSIC_NOW_PLAYING[guild.id]
        else:
            queue = MUSIC_QUEUES.get(guild.id, [])
            if not queue:
                MUSIC_NOW_PLAYING.pop(guild.id, None)
                _schedule_idle_disconnect(guild)
                return
            track = queue.pop(0)
            MUSIC_NOW_PLAYING[guild.id] = track

        _cancel_idle_disconnect(guild)

        loop = asyncio.get_running_loop()
        dest_template = os.path.join(_MUSIC_CACHE_DIR, f"{uuid.uuid4().hex}.%(ext)s")
        local_path = await loop.run_in_executor(None, _ytdl_download, track["webpage_url"], dest_template)
        if not local_path or not os.path.isfile(local_path):
            print(f"[music] Download failed for {track['title']!r}, skipping.")
            asyncio.create_task(_play_next(guild))
            return

        try:
            source = discord.FFmpegPCMAudio(local_path, options=_FFMPEG_OPTS)
        except Exception as e:
            print(f"[music] Failed to start ffmpeg for {track['title']!r}: {e!r}")
            try:
                os.remove(local_path)
            except OSError:
                pass
            asyncio.create_task(_play_next(guild))
            return
        source = discord.PCMVolumeTransformer(source, volume=MUSIC_VOLUME.get(guild.id, 0.5))

        def _after(error):
            try:
                os.remove(local_path)
            except OSError:
                pass
            fut = asyncio.run_coroutine_threadsafe(_play_next(guild), bot.loop)
            try:
                fut.result()
            except Exception:
                pass

        vc.play(source, after=_after)

    asyncio.create_task(_post_now_playing(guild, track))


async def _music_enqueue(guild: discord.Guild, member: discord.Member, query: str, text_channel):
    vc = await _ensure_voice_client(member)
    if not vc:
        return None
    track = await _resolve_track(query, member)
    if not track:
        return None
    MUSIC_TEXT_CHANNEL[guild.id] = text_channel.id
    MUSIC_QUEUES.setdefault(guild.id, []).append(track)
    _cancel_idle_disconnect(guild)
    await _play_next(guild)
    return track


def _in_bot_vc(member: discord.Member) -> bool:
    vc = member.guild.voice_client
    return bool(vc and member.voice and member.voice.channel and member.voice.channel.id == vc.channel.id)


def _music_pause_resume(guild: discord.Guild) -> str:
    vc = guild.voice_client
    if not vc:
        return "❌ Not connected to a voice channel."
    if vc.is_playing():
        vc.pause()
        return "⏸️ Paused."
    if vc.is_paused():
        vc.resume()
        return "▶️ Resumed."
    return "❌ Nothing is playing."


def _music_skip(guild: discord.Guild) -> str:
    vc = guild.voice_client
    if not vc or not (vc.is_playing() or vc.is_paused()):
        return "❌ Nothing playing to skip."
    vc.stop()  # triggers the `after` callback -> _play_next
    return "⏭️ Skipped."


async def _music_stop(guild: discord.Guild) -> str:
    MUSIC_QUEUES[guild.id] = []
    MUSIC_LOOP[guild.id] = False
    _cancel_idle_disconnect(guild)
    vc = guild.voice_client
    if vc:
        vc.stop()
        try:
            await vc.disconnect()
        except discord.HTTPException:
            pass
    MUSIC_NOW_PLAYING.pop(guild.id, None)
    return "⏹️ Stopped and left the voice channel."


def _music_toggle_loop(guild: discord.Guild) -> str:
    MUSIC_LOOP[guild.id] = not MUSIC_LOOP.get(guild.id, False)
    return f"🔁 Loop is now **{'on' if MUSIC_LOOP[guild.id] else 'off'}**."


def _music_set_volume(guild: discord.Guild, percent: int) -> str:
    percent = max(0, min(200, percent))
    vol = percent / 100
    MUSIC_VOLUME[guild.id] = vol
    vc = guild.voice_client
    if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = vol
    return f"🔊 Volume set to **{percent}%**."


class MusicControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.primary, custom_id="music_btn_pauseresume")
    async def btn_pauseresume(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _in_bot_vc(interaction.user):
            await interaction.response.send_message("❌ You have to be in the voice channel to control the music.", ephemeral=True)
            return
        await interaction.response.send_message(_music_pause_resume(interaction.guild), ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="music_btn_skip")
    async def btn_skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _in_bot_vc(interaction.user):
            await interaction.response.send_message("❌ You have to be in the voice channel to control the music.", ephemeral=True)
            return
        await interaction.response.send_message(_music_skip(interaction.guild), ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="music_btn_stop")
    async def btn_stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _in_bot_vc(interaction.user):
            await interaction.response.send_message("❌ You have to be in the voice channel to control the music.", ephemeral=True)
            return
        await interaction.response.send_message(await _music_stop(interaction.guild), ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="music_btn_loop")
    async def btn_loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _in_bot_vc(interaction.user):
            await interaction.response.send_message("❌ You have to be in the voice channel to control the music.", ephemeral=True)
            return
        await interaction.response.send_message(_music_toggle_loop(interaction.guild), ephemeral=True)

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, custom_id="music_btn_voldown")
    async def btn_voldown(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _in_bot_vc(interaction.user):
            await interaction.response.send_message("❌ You have to be in the voice channel to control the music.", ephemeral=True)
            return
        current = int(MUSIC_VOLUME.get(interaction.guild.id, 0.5) * 100)
        await interaction.response.send_message(_music_set_volume(interaction.guild, current - 10), ephemeral=True)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, custom_id="music_btn_volup")
    async def btn_volup(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _in_bot_vc(interaction.user):
            await interaction.response.send_message("❌ You have to be in the voice channel to control the music.", ephemeral=True)
            return
        current = int(MUSIC_VOLUME.get(interaction.guild.id, 0.5) * 100)
        await interaction.response.send_message(_music_set_volume(interaction.guild, current + 10), ephemeral=True)

    @discord.ui.button(label="Queue", emoji="📜", style=discord.ButtonStyle.secondary, custom_id="music_btn_queue")
    async def btn_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        queue = MUSIC_QUEUES.get(interaction.guild.id, [])
        if not queue:
            await interaction.response.send_message("📭 Queue is empty.", ephemeral=True)
            return
        lines = [f"{i + 1}. **{t['title']}** — requested by {t['requester']}" for i, t in enumerate(queue[:10])]
        if len(queue) > 10:
            lines.append(f"*+ {len(queue) - 10} more*")
        await interaction.response.send_message("📜 **Up next:**\n" + "\n".join(lines), ephemeral=True)


# jailed_users[(guild_id, user_id)] = asyncio.Task — the pending auto_unjail
# timer. Keyed by (guild_id, user_id), NOT just user_id, so jailing the same
# Discord account in a second server can't cancel the first server's timer.
jailed_users = {}

# ── Snipe cache ───────────────────────────────────────────────
# Session-scoped on purpose, same as vc_join_time above — nobody expects
# sniped messages to survive a bot restart. Keeps the last 20 deletes/edits
# per channel so ,snipe / ,editsnipe can step back through recent history.
# SNIPE_CACHE[channel_id]      = deque of {author, content, attachments, deleted_at}
# EDIT_SNIPE_CACHE[channel_id] = deque of {author, before, after, edited_at}
SNIPE_CACHE: dict = {}
EDIT_SNIPE_CACHE: dict = {}
SNIPE_CACHE_SIZE = 20

# JAIL_EXPIRY[guild_id][user_id] = {"expires_at": unix_timestamp, "reason": str}
# Persisted so a jail's auto-release timer survives a bot restart instead of
# leaving the member jailed forever.
JAIL_EXPIRY: dict[int, dict[int, dict]] = _load_depth(_load_data("jail_expiry", {}), 2)


def _clear_jail_expiry(guild_id: int, user_id: int) -> None:
    JAIL_EXPIRY.get(guild_id, {}).pop(user_id, None)
    _save_jail_expiry()


# INMATE_NUMBERS[guild_id][user_id] = int — assigned once, the first time
# someone is ever jailed in this server, and kept forever after that
# (repeat offenders keep the same number, for flavor/continuity).
# INMATE_NUMBER_COUNTER[guild_id] = the next number to hand out.
INMATE_NUMBERS: dict[int, dict[int, int]] = _load_depth(_load_data("inmate_numbers", {}), 2)
INMATE_NUMBER_COUNTER: dict[int, int] = _load_depth(_load_data("inmate_number_counter", {}), 1)


def _save_inmate_numbers():
    _save_data("inmate_numbers", _dump_depth(INMATE_NUMBERS, 2))


def _save_inmate_number_counter():
    _save_data("inmate_number_counter", _dump_depth(INMATE_NUMBER_COUNTER, 1))


def _get_or_assign_inmate_number(guild_id: int, user_id: int) -> int:
    numbers = INMATE_NUMBERS.setdefault(guild_id, {})
    if user_id in numbers:
        return numbers[user_id]
    next_number = INMATE_NUMBER_COUNTER.get(guild_id, 0) + 1
    INMATE_NUMBER_COUNTER[guild_id] = next_number
    numbers[user_id] = next_number
    _save_inmate_numbers()
    _save_inmate_number_counter()
    return next_number


# WARNINGS[guild_id][user_id] = [ {reason, moderator, moderator_id, time}, ... ]
WARNINGS = _load_warnings()

# STAFF_WARNINGS/STAFF_STRIKES[guild_id][user_id] = [ {reason, moderator,
# moderator_id, time}, ... ] — same shape as WARNINGS, but a separate track
# for formal staff discipline, issued only by the "os" role (see
# STAFF_DISCIPLINE_ROLE / _os_only_check above).
STAFF_WARNINGS = _load_staff_warnings()
STAFF_STRIKES = _load_staff_strikes()

# ── Moderation history ────────────────────────────────────────
# MOD_HISTORY[guild_id][user_id] = [ {action, moderator, moderator_id, reason, extra, time}, ... ]
# A single searchable timeline of every punishment action taken against a
# member — warn, jail, unjail, ban, kick, timeout, mute, unmute, hardban,
# unhardban, strip — regardless of which specific system logged it.
MOD_HISTORY: dict[int, dict[int, list]] = _load_mod_history()

_MOD_HISTORY_ICONS = {
    "warn": "⚠️", "jail": "🔒", "unjail": "🔓", "auto_unjail": "⏰",
    "ban": "🔨", "kick": "👢", "timeout": "⏳", "mute": "🔇", "unmute": "🔊",
    "hardban": "🔴", "unhardban": "✅", "strip": "⚔️",
}


def _log_mod_action(guild_id: int, user_id: int, action: str, moderator, reason: str, extra: str = None):
    entry = {
        "action": action,
        "moderator": str(moderator),
        "moderator_id": getattr(moderator, "id", None),
        "reason": reason,
        "extra": extra,
        "time": discord.utils.utcnow(),
    }
    MOD_HISTORY.setdefault(guild_id, {}).setdefault(user_id, []).append(entry)
    _save_mod_history()

# ── Chat stats ─────────────────────────────────────────────
# CHAT_STATS[guild_id][user_id] = message_count (all-time, persisted)
CHAT_STATS: dict[int, dict[int, int]] = _load_depth(_load_data("chat_stats", {}), 2)

# ── Staff of the Month / Most Active Staff ──────────────────
# STAFF_ACTIVITY[guild_id][user_id] = message_count sent by that staff
# member during the CURRENT award period only — reset every time a new
# month is crowned (see _check_staff_awards). Only staff (Manage
# Messages or Administrator) get counted here at all.
STAFF_ACTIVITY: dict[int, dict[int, int]] = _load_depth(_load_data("staff_activity", {}), 2)
# STAFF_OF_MONTH_ROLE[guild_id] = role_id — crowned each period to
# whoever logged the most moderation actions (bans, jails, kicks,
# timeouts, mutes, warns, hardbans, strips) in the trailing 30 days.
STAFF_OF_MONTH_ROLE: dict[int, int] = _load_depth(_load_data("staff_of_month_role", {}), 1)
# MOST_ACTIVE_STAFF_ROLE[guild_id] = role_id — crowned each period to
# whoever sent the most messages while holding staff permissions.
MOST_ACTIVE_STAFF_ROLE: dict[int, int] = _load_depth(_load_data("most_active_staff_role", {}), 1)
# STAFF_AWARDS_CHANNEL[guild_id] = channel_id — where the monthly
# announcement posts. Falls back to the update channel if unset.
STAFF_AWARDS_CHANNEL: dict[int, int] = _load_depth(_load_data("staff_awards_channel", {}), 1)
# STAFF_PERKS_TEXT[guild_id] = "..." — shown in the announcement so
# everyone knows what the winners actually get. Set via ,setstaffperks.
STAFF_PERKS_TEXT: dict[int, str] = _load_depth(_load_data("staff_perks_text", {}), 1)
# LAST_STAFF_AWARD_MONTH[guild_id] = "YYYY-MM" — the last calendar month
# this server was crowned for, so the periodic loop fires exactly once
# per month rollover instead of every time it ticks.
LAST_STAFF_AWARD_MONTH: dict[int, str] = _load_depth(_load_data("last_staff_award_month", {}), 1)


def _save_staff_activity():
    _save_data("staff_activity", _dump_depth(STAFF_ACTIVITY, 2))


def _save_staff_of_month_role():
    _save_data("staff_of_month_role", _dump_depth(STAFF_OF_MONTH_ROLE, 1))


def _save_most_active_staff_role():
    _save_data("most_active_staff_role", _dump_depth(MOST_ACTIVE_STAFF_ROLE, 1))


def _save_staff_awards_channel():
    _save_data("staff_awards_channel", _dump_depth(STAFF_AWARDS_CHANNEL, 1))


def _save_staff_perks_text():
    _save_data("staff_perks_text", _dump_depth(STAFF_PERKS_TEXT, 1))


def _save_last_staff_award_month():
    _save_data("last_staff_award_month", _dump_depth(LAST_STAFF_AWARD_MONTH, 1))


def _is_staff_member(member: discord.Member) -> bool:
    """Baseline staff definition for award eligibility: native Manage
    Messages or Administrator permission."""
    return member.guild_permissions.manage_messages or member.guild_permissions.administrator


# Built-in perks automatically granted to whoever currently holds the
# Staff of the Month or Most Active Staff role — no extra setup needed
# beyond ,setstaffofmonthrole / ,setmostactiverole.
STAFF_AWARD_ECONOMY_MULTIPLIER = 2.0
STAFF_AWARD_GIVEAWAY_WEIGHT = 3.0
STAFF_AWARD_WORK_COOLDOWN = 10  # vs the normal 20s
_STAFF_AWARD_PERKS_BLURB = (
    "• **2x earnings** from `,work`, `,daily`, and `,weekly`\n"
    "• **Half cooldown** on `,work`\n"
    "• **3x better odds** in every `,giveaway`\n"
    "• 🏛️ **Permanent Hall of Fame role** *(Staff of the Month only — never removed)*\n"
    "• 📛 **Special nickname title** while holding the role\n"
    "• 🎙️ **Private lounge** (VC + chat) *(Staff of the Month only, if set up)*\n"
    "• 💵 $20–$50 reward, depending on budget *(staff will reach out)*\n"
    "• 🎁 Free item from the server shop\n"
    "• 🎬 Gets to pick the next server event\n"
    "• ⭐ Priority consideration for promotion\n"
    "• 📜 Added to `,halloffame` forever"
)
_MVP_PERKS_BLURB = (
    "• 🎨 **MVP role color** for the week\n"
    "• 📛 **Custom nickname title** for the week\n"
    "• 🎙️ **Private MVP lounge** (VC + chat), if set up\n"
    "• 💵 **$15–20 gift card** reward *(staff will reach out)*\n"
    "• 📣 Shoutout in announcements + priority in staff requests\n"
    "• 🎬 Gets to pick the next movie night\n"
    "• 🔥 **Win streak bonus** — extra in-server cash the more weeks in a row you win\n"
    "• 📜 Added to `,halloffame` forever"
)


def _has_staff_award_role(member: discord.Member) -> bool:
    """True if this member currently holds the Staff of the Month or
    Most Active Staff role in their guild."""
    role_ids = {STAFF_OF_MONTH_ROLE.get(member.guild.id), MOST_ACTIVE_STAFF_ROLE.get(member.guild.id)}
    role_ids.discard(None)
    if not role_ids:
        return False
    return any(r.id in role_ids for r in member.roles)


# ── Staff of the Month "big" perks: Hall of Fame, lounge, title ──
# HALL_OF_FAME_ROLE[guild_id] = role_id — granted to every Staff of the
# Month winner PERMANENTLY (never removed, unlike the monthly role).
HALL_OF_FAME_ROLE: dict[int, int] = _load_depth(_load_data("hall_of_fame_role", {}), 1)
# SOTM_TITLE / MVP_TITLE[guild_id] = "👑" / "🏆" — nickname tag prepended
# while the member holds that role. Defaults used when unset.
SOTM_TITLE: dict[int, str] = _load_depth(_load_data("sotm_title", {}), 1)
MVP_TITLE: dict[int, str] = _load_depth(_load_data("mvp_title", {}), 1)
# SOTM_LOUNGE / MVP_LOUNGE[guild_id] = {"text": channel_id, "voice": channel_id}
# Private channels the bot hands off between winners each period —
# staff create the channels and lock them down from @everyone first;
# the bot only manages the current winner's explicit member overwrite.
SOTM_LOUNGE: dict[int, dict] = _load_depth(_load_data("sotm_lounge", {}), 1)
MVP_LOUNGE: dict[int, dict] = _load_depth(_load_data("mvp_lounge", {}), 1)
# TITLED_NICK_BASE[guild_id][user_id] = nickname (or None) from before
# any title styling began — restored once no title applies anymore.
TITLED_NICK_BASE: dict[int, dict[int, str]] = _load_depth(_load_data("titled_nick_base", {}), 2)
# HALL_OF_FAME_LOG[guild_id] = [{"kind": "sotm"/"mvp", "user_id", "period", "count", "time"}, ...]
HALL_OF_FAME_LOG: dict[int, list] = {
    int(gid): _dt_from_iso(entries) for gid, entries in _load_data("hall_of_fame_log", {}).items()
}

# ── Staff MVP (weekly) ────────────────────────────────────────
# MVP_ROLE[guild_id] = role_id — crowned each ISO week to whoever sent
# the most staff-activity messages that week. Independent of the
# monthly Most Active Staff role/counter.
MVP_ROLE: dict[int, int] = _load_depth(_load_data("mvp_role", {}), 1)
# MVP_ACTIVITY[guild_id][user_id] = message_count for the CURRENT week
# only — reset every time a new week is crowned.
MVP_ACTIVITY: dict[int, dict[int, int]] = _load_depth(_load_data("mvp_activity", {}), 2)
# MVP_AWARDS_CHANNEL[guild_id] = channel_id — falls back to the Staff
# Awards channel, then the update channel.
MVP_AWARDS_CHANNEL: dict[int, int] = _load_depth(_load_data("mvp_awards_channel", {}), 1)
# LAST_MVP_AWARD_WEEK[guild_id] = "YYYY-Www" (ISO week) — last week this
# server crowned an MVP, so the loop fires exactly once per rollover.
LAST_MVP_AWARD_WEEK: dict[int, str] = _load_depth(_load_data("last_mvp_award_week", {}), 1)
# LAST_MVP_WINNER[guild_id] = user_id of last week's MVP, MVP_STREAK[guild_id]
# = {user_id: consecutive weeks won} — only the current winner has a
# nonzero entry; used for the streak economy bonus.
LAST_MVP_WINNER: dict[int, int] = _load_depth(_load_data("last_mvp_winner", {}), 1)
MVP_STREAK: dict[int, dict[int, int]] = _load_depth(_load_data("mvp_streak", {}), 2)


def _save_hall_of_fame_role():
    _save_data("hall_of_fame_role", _dump_depth(HALL_OF_FAME_ROLE, 1))


def _save_sotm_title():
    _save_data("sotm_title", _dump_depth(SOTM_TITLE, 1))


def _save_mvp_title():
    _save_data("mvp_title", _dump_depth(MVP_TITLE, 1))


def _save_sotm_lounge():
    _save_data("sotm_lounge", _dump_depth(SOTM_LOUNGE, 1))


def _save_mvp_lounge():
    _save_data("mvp_lounge", _dump_depth(MVP_LOUNGE, 1))


def _save_titled_nick_base():
    _save_data("titled_nick_base", _dump_depth(TITLED_NICK_BASE, 2))


def _save_hall_of_fame_log():
    _save_data("hall_of_fame_log", {str(gid): _dt_to_iso(entries) for gid, entries in HALL_OF_FAME_LOG.items()})


def _save_mvp_role():
    _save_data("mvp_role", _dump_depth(MVP_ROLE, 1))


def _save_mvp_activity():
    _save_data("mvp_activity", _dump_depth(MVP_ACTIVITY, 2))


def _save_mvp_awards_channel():
    _save_data("mvp_awards_channel", _dump_depth(MVP_AWARDS_CHANNEL, 1))


def _save_last_mvp_award_week():
    _save_data("last_mvp_award_week", _dump_depth(LAST_MVP_AWARD_WEEK, 1))


def _save_last_mvp_winner():
    _save_data("last_mvp_winner", _dump_depth(LAST_MVP_WINNER, 1))


def _save_mvp_streak():
    _save_data("mvp_streak", _dump_depth(MVP_STREAK, 2))


def _log_hall_of_fame(guild_id: int, kind: str, user_id: int, period: str, count: int):
    HALL_OF_FAME_LOG.setdefault(guild_id, []).append({
        "kind": kind, "user_id": user_id, "period": period, "count": count,
        "time": discord.utils.utcnow(),
    })
    _save_hall_of_fame_log()


async def _refresh_titled_nickname(member: discord.Member, *, has_sotm: bool = None, has_mvp: bool = None):
    """Rebuilds member's nickname to show whichever title tags (SOTM /
    MVP) they currently hold, restoring their pre-styling nickname once
    neither applies anymore. Cosmetic only — failures are ignored.

    add_roles()/remove_roles() only fire the HTTP call — they don't
    update member.roles locally (that only happens later via a gateway
    MEMBER_UPDATE), so callers that just granted/revoked SOTM or MVP
    this same run MUST pass has_sotm/has_mvp explicitly instead of
    relying on the (stale) cache for that one dimension. Leave a
    parameter as None to fall back to checking member.roles for it."""
    guild = member.guild
    if has_sotm is None:
        som_role_id = STAFF_OF_MONTH_ROLE.get(guild.id)
        has_sotm = bool(som_role_id and any(r.id == som_role_id for r in member.roles))
    if has_mvp is None:
        mvp_role_id = MVP_ROLE.get(guild.id)
        has_mvp = bool(mvp_role_id and any(r.id == mvp_role_id for r in member.roles))

    tags = []
    if has_sotm:
        tags.append(SOTM_TITLE.get(guild.id) or "👑")
    if has_mvp:
        tags.append(MVP_TITLE.get(guild.id) or "🏆")

    base_map = TITLED_NICK_BASE.setdefault(guild.id, {})
    if tags:
        if member.id not in base_map:
            base_map[member.id] = member.nick
            _save_titled_nick_base()
        base = base_map[member.id] or member.name
        new_nick = f"{' '.join(tags)} {base}"[:32]
        if member.nick != new_nick:
            try:
                await member.edit(nick=new_nick, reason="Staff award title")
            except (discord.Forbidden, discord.HTTPException):
                pass
    elif member.id in base_map:
        restore = base_map.pop(member.id)
        _save_titled_nick_base()
        try:
            await member.edit(nick=restore, reason="Staff award title removed")
        except (discord.Forbidden, discord.HTTPException):
            pass


async def _assign_lounge_access(guild: discord.Guild, lounge_cfg: dict, old_members: list, new_member):
    """lounge_cfg = {"text": channel_id, "voice": channel_id}, either key
    optional. Revokes each old member's explicit overwrite there and
    grants new_member view/send (text) or view/connect (voice)."""
    if not lounge_cfg:
        return
    text_ch  = guild.get_channel(lounge_cfg.get("text")) if lounge_cfg.get("text") else None
    voice_ch = guild.get_channel(lounge_cfg.get("voice")) if lounge_cfg.get("voice") else None
    new_id = new_member.id if new_member else None

    for old_member in old_members or []:
        if old_member.id == new_id:
            continue
        for ch in (text_ch, voice_ch):
            if ch:
                try:
                    await ch.set_permissions(old_member, overwrite=None)
                except (discord.Forbidden, discord.HTTPException):
                    pass
    if new_member:
        if text_ch:
            try:
                await text_ch.set_permissions(new_member, view_channel=True, send_messages=True, read_message_history=True)
            except (discord.Forbidden, discord.HTTPException):
                pass
        if voice_ch:
            try:
                await voice_ch.set_permissions(new_member, view_channel=True, connect=True)
            except (discord.Forbidden, discord.HTTPException):
                pass

# ── AFK ──────────────────────────────────────────────────────
# AFK_USERS[guild_id][user_id] = {"reason": str, "since": float (epoch),
# "old_nick": str|None} — set via ,afk, cleared automatically the next
# time that member sends any message (handled in on_message).
AFK_USERS: dict[int, dict[int, dict]] = _load_depth(_load_data("afk_users", {}), 2)


def _save_afk_users():
    _save_data("afk_users", _dump_depth(AFK_USERS, 2))


def _format_afk_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"

# ── Invite tracking ────────────────────────────────────────
# INVITE_CACHE[guild_id] = { code: uses } — session-only, re-derived from
# the guild's live invites on_ready, doesn't need persistence.
INVITE_CACHE: dict[int, dict[str, int]] = {}
# VANITY_INVITE_CACHE[guild_id] = uses — the server's native vanity URL
# (discord.gg/<code>, boost-level-3 only) is a completely separate API
# object from the regular invites guild.invites() returns, with its own
# use counter and no "inviter" field. Session-only, same as INVITE_CACHE.
VANITY_INVITE_CACHE: dict[int, int] = {}
# INVITE_DATA[guild_id][inviter_id] = { "uses": int, "logs": [str, ...] }
INVITE_DATA: dict[int, dict[int, dict]] = _load_depth(_load_data("invite_data", {}), 2)


def _save_invite_data():
    _save_data("invite_data", _dump_depth(INVITE_DATA, 2))

# TICKETS[guild_id][user_id] = channel_id
# TICKET_CLAIMED[channel_id] = member_id  — who claimed the ticket
# TICKET_TYPE[channel_id] = str  — category label chosen at open
# TICKET_PRIORITY[channel_id] = str  — "🟢 Low" | "🟡 Medium" | "🔴 High" | "🚨 Critical"
# TICKET_LOCKED[channel_id] = bool  — whether the ticket is locked for the opener
(
    TICKETS,
    TICKET_CLAIMED,
    TICKET_TYPE,
    TICKET_PRIORITY,
    TICKET_LOCKED,
) = _load_tickets()

# STAFF_TICKET_CLAIMS[guild_id][member_id] = total tickets ever claimed
# (all-time, persisted) — TICKET_CLAIMED above only tracks currently-open
# tickets and is cleared the moment one closes, so this is the durable
# historical count behind ,staffstats / ,staffleaderboard.
STAFF_TICKET_CLAIMS: dict[int, dict[int, int]] = _load_depth(_load_data("staff_ticket_claims", {}), 2)


def _save_staff_ticket_claims():
    _save_data("staff_ticket_claims", _dump_depth(STAFF_TICKET_CLAIMS, 2))


def _record_ticket_claim(guild_id: int, member_id: int):
    guild_counts = STAFF_TICKET_CLAIMS.setdefault(guild_id, {})
    guild_counts[member_id] = guild_counts.get(member_id, 0) + 1
    _save_staff_ticket_claims()

# ── Vouch system ────────────────────────────────────────────
# VOUCHES[guild_id][user_id] = count (net vouch score)
VOUCHES: dict[int, dict[int, int]] = _load_depth(_load_data("vouches", {}), 2)
# VOUCH_LOG[guild_id][user_id] = [ {by, by_id, action, time}, ... ]
VOUCH_LOG: dict[int, dict[int, list]] = _load_vouch_log()
# VOUCH_CONFIG[guild_id] = { "threshold": int }
VOUCH_CONFIG: dict[int, dict] = _load_depth(_load_data("vouch_config", {}), 1)


# ── Protected roles system ───────────────────────────────────
# PROTECTED_ROLES[guild_id] = {role_id, ...}
# Roles in this set can ONLY be granted via ,vouch — manual grants are auto-stripped.
PROTECTED_ROLES: dict[int, set] = {
    int(gid): set(role_ids) for gid, role_ids in _load_data("protected_roles", {}).items()
}

# ── High-staff roles (,lock exemption) ───────────────────────
# HIGH_STAFF_ROLES[guild_id] = {role_id, ...}
# The only roles still allowed to talk in a channel locked with ,lock —
# everyone else, including lower-rank staff without one of these roles,
# gets blocked by the @everyone deny. Managed via ,highstaffrole.
HIGH_STAFF_ROLES: dict[int, set] = {
    int(gid): set(role_ids) for gid, role_ids in _load_data("high_staff_roles", {}).items()
}


def _save_high_staff_roles():
    _save_data("high_staff_roles", {str(gid): list(role_ids) for gid, role_ids in HIGH_STAFF_ROLES.items()})

# ── Permitted-role system ────────────────────────────────────
# PERMITTED_ROLES[guild_id][command_qualified_name] = {role_id, ...}
# A role in this set can use that specific command even without holding
# the underlying Discord permission it normally requires — set/cleared via
# ,setpermittedrole. Every command still gated with _permitted_check()
# (not plain commands.has_permissions) honors this; native permission
# holders and Administrators are always allowed regardless, unaffected.
PERMITTED_ROLES: dict[int, dict[str, set]] = {
    int(gid): {cmd: set(role_ids) for cmd, role_ids in cmds.items()}
    for gid, cmds in _load_data("permitted_roles", {}).items()
}


def _save_permitted_roles():
    _save_data("permitted_roles", {
        str(gid): {cmd: list(role_ids) for cmd, role_ids in cmds.items()}
        for gid, cmds in PERMITTED_ROLES.items()
    })


def _permitted_check(**perms):
    """Drop-in replacement for commands.has_permissions(**perms) — same
    baseline behavior (native permission holders, including Administrators,
    are always allowed), plus an additive override: a role explicitly
    granted via ,setpermittedrole permit @role <command> can use that
    command too, even without the underlying permission."""
    invalid = set(perms) - set(discord.Permissions.VALID_FLAGS)
    if invalid:
        raise TypeError(f"Invalid permission(s): {', '.join(invalid)}")

    async def predicate(ctx):
        missing = [perm for perm, value in perms.items() if getattr(ctx.permissions, perm) != value]
        if not missing:
            return True
        if ctx.guild is not None:
            cmd_name = ctx.command.qualified_name
            permitted_role_ids = PERMITTED_ROLES.get(ctx.guild.id, {}).get(cmd_name, set())
            if permitted_role_ids and any(r.id in permitted_role_ids for r in ctx.author.roles):
                return True
        raise commands.MissingPermissions(missing)

    return commands.check(predicate)


def _os_only_check():
    """Restricts a command to members holding the STAFF_DISCIPLINE_ROLE
    ("os") — used for the staff warning/strike system, which is meant to
    stay with Ownership-tier only rather than every administrator."""
    async def predicate(ctx):
        if ctx.guild is None:
            return False
        return discord.utils.get(ctx.author.roles, name=STAFF_DISCIPLINE_ROLE) is not None
    return commands.check(predicate)

# Pending vouch-role requests awaiting owner approval
# ROLE_VOUCH_PENDING[guild_id][token] = {
#   "member_id", "role_id", "requester_id", "reason", "message_id", "channel_id"
# }
ROLE_VOUCH_PENDING: dict[int, dict[str, dict]] = _load_depth(_load_data("role_vouch_pending", {}), 1)

# ── Auto-role system ─────────────────────────────────────────
# AUTOROLE[guild_id] = [role_id, ...]  — roles given to every new member on join
AUTOROLE: dict[int, list] = {
    int(gid): list(role_ids) for gid, role_ids in _load_data("autorole", {}).items()
}

# Temp whitelist so on_member_update knows a grant is bot-approved
# _VOUCH_ROLE_APPROVED[guild_id] = {(member_id, role_id), ...}
_VOUCH_ROLE_APPROVED: dict[int, set] = {}

# ── Hard-ban system ─────────────────────────────────────────
# HARD_BANNED[guild_id] = { user_id: reason }
HARD_BANNED: dict[int, dict[int, str]] = _load_depth(_load_data("hard_banned", {}), 2)

# ── Role snapshots (strip → restore) ────────────────────────
# ROLE_SNAPSHOTS[guild_id][user_id] = [role_id, ...]
ROLE_SNAPSHOTS: dict[int, dict[int, list]] = _load_depth(_load_data("role_snapshots", {}), 2)

# JAIL_ROLE_SNAPSHOTS[guild_id][user_id] = [role_id, ...] — every role a member
# held right before being jailed (kept separate from ROLE_SNAPSHOTS/,strip so
# the two features can never clobber each other's data). Restored automatically
# on release, except Verified/Unverified — release still requires re-verifying.
JAIL_ROLE_SNAPSHOTS: dict[int, dict[int, list]] = _load_depth(_load_data("jail_role_snapshots", {}), 2)

# ── Anti-nuke tracker ────────────────────────────────────────
# NUKE_TRACKER[guild_id][user_id] = [timestamp, ...]
NUKE_TRACKER: dict[int, dict[int, list]] = {}
NUKE_ROLE_LIMIT   = 3   # max role deletes within window
NUKE_CHAN_LIMIT   = 3   # max channel deletes within window
NUKE_BAN_LIMIT    = 2   # max member bans within window — deliberately tighter
                         # than the role/channel limits above: unlike those,
                         # the ban tracker below does NOT exempt admins, so
                         # this is the only thing standing between a mass-ban
                         # spree (compromised staff account, malicious admin)
                         # and the whole member list.
NUKE_WINDOW       = 10  # seconds

# ANTINUKE_WHITELIST[guild_id] = {user_id, ...} — exempt from anti-nuke
# entirely (rapid role/channel deletion protection), persisted. Managed
# via ,wl. Separate from WHITELIST above, which is anti-raid only.
ANTINUKE_WHITELIST: dict = _load_depth(_load_data("antinuke_whitelist", {}), 1)
for _gid in list(ANTINUKE_WHITELIST.keys()):
    ANTINUKE_WHITELIST[_gid] = set(ANTINUKE_WHITELIST[_gid])


def _save_antinuke_whitelist():
    _save_data("antinuke_whitelist", _dump_depth(
        {gid: list(uids) for gid, uids in ANTINUKE_WHITELIST.items()}, 1
    ))


async def _antinuke_punish(guild: discord.Guild, actor, reason: str):
    """Shared punishment for any anti-nuke trigger (rapid role deletes,
    channel deletes, or mass bans): strip every removable role, then
    hardban the offender — persisted so they're instantly re-banned if
    they somehow rejoin. Returns the list of stripped role names for the
    triggering handler's log embed."""
    actor_member = guild.get_member(actor.id)
    roles_to_remove = []
    if actor_member:
        roles_to_remove = [r for r in actor_member.roles if not r.is_default() and r < guild.me.top_role]
        if roles_to_remove:
            try:
                await actor_member.remove_roles(*roles_to_remove, reason=reason)
            except (discord.Forbidden, discord.HTTPException):
                pass

    HARD_BANNED.setdefault(guild.id, {})[actor.id] = reason
    _save_hard_banned()
    try:
        await guild.ban(actor, reason=reason, delete_message_days=1)
    except (discord.Forbidden, discord.HTTPException):
        pass

    return roles_to_remove

# ── GIF automod exemption ─────────────────────────────────────
# GIF_EXEMPT_ROLE[guild_id] = role_id — which role is exempt from automod's
# link filter for GIFs, set independently per server via ,setgifrole since
# every server can name/use a different role for this.
GIF_EXEMPT_ROLE: dict[int, int] = _load_depth(_load_data("gif_exempt_role", {}), 1)


def _resolve_gif_exempt_role(guild: discord.Guild):
    role_id = GIF_EXEMPT_ROLE.get(guild.id)
    if role_id:
        role = guild.get_role(role_id)
        if role:
            return role
    # Nothing configured for this server yet — fall back to the Verified
    # role by name so existing behavior keeps working until an admin sets one.
    return discord.utils.get(guild.roles, name=VERIFIED_ROLE)

# ── Vanity URL role tracking ──────────────────────────────────
# VANITY_ROLE[guild_id] = role_id — this server's designated rep-reward
# role, for reference only. Staff grant/remove it manually now; there is
# no automatic status-based tracking.
VANITY_ROLE: dict[int, int] = _load_depth(_load_data("vanity_role", {}), 1)

# VANITY_CODE_OVERRIDE[guild_id] = "code" — manual override for servers that
# don't have Discord's native boosted vanity URL (guild.vanity_url_code).
VANITY_CODE_OVERRIDE: dict[int, str] = _load_depth(_load_data("vanity_code_override", {}), 1)


def _save_vanity_role():
    _save_data("vanity_role", _dump_depth(VANITY_ROLE, 1))


def _save_vanity_code_override():
    _save_data("vanity_code_override", _dump_depth(VANITY_CODE_OVERRIDE, 1))


def _resolve_vanity_code(guild: discord.Guild):
    override = VANITY_CODE_OVERRIDE.get(guild.id)
    if override:
        return override.lower()
    if guild.vanity_url_code:
        return guild.vanity_url_code.lower()
    return None


def _resolve_invite_link(guild: discord.Guild) -> str:
    """The server's public invite link — sourced from the vanity code
    (native or ,setvanitycode override), since vanity links never expire.
    Returns "" if no vanity code is set or detected."""
    code = _resolve_vanity_code(guild)
    return f"https://discord.gg/{code}" if code else ""

# ── Welcome config ───────────────────────────────────────────
# WELCOME_CONFIG[guild_id] = { "channel_id": int, "enabled": bool }
WELCOME_CONFIG: dict[int, dict] = _load_depth(_load_data("welcome_config", {}), 1)


# ============================================================
# LOGGING — rich field-based embeds
# ============================================================

# Emoji badges for log categories
_LOG_ICONS = {
    "joins":        "📥", "leaves":     "📤", "bans":       "🔨",
    "kicks":        "👢", "timeouts":   "⏳", "mutes":      "🔇",
    "warns":        "⚠️",  "jail":       "🔒", "mod":        "🛡",
    "roles":        "🏷️",  "role_create":"✨", "role_delete":"🗑️",
    "verification": "✅", "raids":      "🚨", "boost":      "🚀",
    "disconnected": "🔌", "server muted":   "🔇", "server unmuted":   "🔊",
    "server deafened": "🙉", "server undeafened": "👂", "vanity": "💎",
    "vc":           "🎤", "messages":   "💬", "tickets":    "🎫",
    "nicknames":    "✏️",  "channel_create":"📁","channel_delete":"🗑️",
    "channel_update":"🔧","emoji":      "😀", "stickers":   "🖼️",
    "lockdowns":    "🔐", "unlockdowns":"🔓", "clears":     "🧹",
    "purges":       "🗑️",  "hides":      "👁️",  "strips":     "⚔️",
    "massroles":    "📦", "roleall":    "📢", "invites":    "📨",
    "staff warning": "⚠️", "staff strike": "❌", "terminated": "🚨",
}


def _resolve_log_channel(guild: discord.Guild, key: str):
    """
    Resolve a log channel for the given guild and key.
    Priority: per-guild override ID → channel name search → None.
    `key` is a LOG_CHANNELS key string (e.g. "mod", "bans").
    """
    # 1. Per-guild override set via ,setlogchannel
    override_id = LOG_CHANNEL_OVERRIDES.get(guild.id, {}).get(key)
    if override_id:
        ch = guild.get_channel(override_id)
        if ch:
            return ch

    # 2. Find by default channel name
    channel_name = LOG_CHANNELS.get(key)
    if channel_name:
        ch = discord.utils.get(guild.text_channels, name=channel_name)
        if ch:
            return ch

    return None


async def log(guild, key_or_id, title, description, color, fields: list = None, actor=None, target=None, thumbnail_url=None):
    """
    Rich structured log embed.
    - key_or_id    : a LOG_CHANNELS key string (e.g. "mod") OR a legacy int channel ID
    - description  : shown as embed description (main context line)
    - fields       : list of (name, value, inline) tuples for structured data
    - actor        : discord.Member/User who performed the action (shown in footer + thumbnail)
    - target       : discord.Member/User the action was performed on
    - thumbnail_url: override thumbnail (falls back to target → actor → guild icon)
    """
    if isinstance(key_or_id, int):
        # Legacy direct ID — resolve directly
        channel = guild.get_channel(key_or_id)
    else:
        channel = _resolve_log_channel(guild, key_or_id)
    if not channel:
        return

    # Pick category icon from title keywords
    icon = "📋"
    tl = title.lower()
    for key, emoji in _LOG_ICONS.items():
        if key in tl:
            icon = emoji
            break

    embed = discord.Embed(
        title=f"{icon}  {title}",
        description=description or "",
        color=color,
        timestamp=discord.utils.utcnow()
    )

    # Structured fields
    if fields:
        for name, value, inline in fields:
            if value:
                embed.add_field(name=name, value=str(value)[:1024], inline=inline)

    # Thumbnail: target profile pic > actor > guild icon
    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)
    elif target and hasattr(target, "display_avatar"):
        embed.set_thumbnail(url=target.display_avatar.url)
    elif actor and hasattr(actor, "display_avatar"):
        embed.set_thumbnail(url=actor.display_avatar.url)
    elif guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    # Footer: actor info
    if actor:
        embed.set_footer(
            text=f"{guild.name} • Logs  |  Action by {actor} ({actor.id})",
            icon_url=actor.display_avatar.url if hasattr(actor, "display_avatar") else None
        )
    else:
        embed.set_footer(text=f"{guild.name} • Logs")

    try:
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass


# ============================================================
# DM ACTION HELPER — notify users of mod actions via DM
# ============================================================

# Action config: (title, color, icon, past-tense label)
_DM_ACTION_CFG = {
    "kick":     ("👢 You have been kicked",         discord.Color.orange(),   "👢", "Kicked from"),
    "ban":      ("🔨 You have been banned",          discord.Color.red(),      "🔨", "Banned from"),
    "hardban":  ("🔴 You have been permanently banned", discord.Color.dark_red(), "🔴", "Hard-banned from"),
    "timeout":  ("⏳ You have been timed out",       discord.Color.purple(),     "⏳", "Timed out in"),
    "jail":     ("🔒 You have been restricted",      discord.Color.dark_red(), "🔒", "Jailed in"),
}


async def _dm_action(
    user,
    guild: discord.Guild,
    action: str,
    moderator,
    reason: str,
    *,
    extra: str = None,          # e.g. "Duration: 30m" for timeout/jail
):
    """
    Send a moderation-action DM to a user.
    Silently does nothing if the user has DMs closed.
    """
    cfg = _DM_ACTION_CFG.get(action)
    if not cfg:
        return
    title, color, icon, label = cfg
    invite = _resolve_invite_link(guild)

    embed = discord.Embed(
        title=title,
        color=color,
        timestamp=discord.utils.utcnow()
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    embed.add_field(name=f"{icon} Action",     value=f"{label} **{guild.name}**", inline=False)
    embed.add_field(name="🛡 Moderator",        value=str(moderator),               inline=True)
    embed.add_field(name="📝 Reason",           value=reason,                       inline=False)
    if extra:
        embed.add_field(name="⏱️ Details",      value=extra,                        inline=True)
    if invite:
        embed.add_field(name="🔗 Server Invite", value=invite,                      inline=False)
    embed.set_footer(text=f"TrapAI • {guild.name}")

    try:
        await user.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass  # DMs closed — silently skip


# ============================================================
# TICKET CONTROL VIEW — persistent buttons inside every ticket
# ============================================================
class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    def _is_staff(self, member: discord.Member) -> bool:
        return (
            member.guild_permissions.manage_messages
            or member.guild_permissions.administrator
        )

    # ── Claim ────────────────────────────────────────────────
    @discord.ui.button(
        label="Claim Ticket",
        style=discord.ButtonStyle.success,
        emoji="🙋",
        custom_id="trapai_ticket_claim",
        row=0
    )
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Only staff can claim tickets.", ephemeral=True)
            return

        channel = interaction.channel
        already = TICKET_CLAIMED.get(channel.id)
        if already:
            member = interaction.guild.get_member(already)
            name = member.mention if member else f"<@{already}>"
            await interaction.response.send_message(
                f"❌ This ticket is already claimed by {name}.", ephemeral=True
            )
            return

        TICKET_CLAIMED[channel.id] = interaction.user.id
        _save_tickets()
        _record_ticket_claim(interaction.guild.id, interaction.user.id)
        await _update_ticket_header_claim(channel, claimed_by=interaction.user)

        embed = discord.Embed(
            title="🙋 Ticket Claimed",
            description=f"{interaction.user.mention} has claimed this ticket and will assist you.",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="TrapAI Ticket System")
        await interaction.response.send_message(embed=embed)

        await log(
            interaction.guild,
            "tickets",
            "Ticket Claimed",
            (
                f"**Channel:** {channel.mention}\n"
                f"**Claimed By:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                f"**Ticket Type:** {TICKET_TYPE.get(channel.id, 'General')}"
            ),
            discord.Color.green()
        )

    # ── Unclaim ──────────────────────────────────────────────
    @discord.ui.button(
        label="Unclaim",
        style=discord.ButtonStyle.secondary,
        emoji="↩️",
        custom_id="trapai_ticket_unclaim",
        row=0
    )
    async def unclaim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Only staff can unclaim tickets.", ephemeral=True)
            return

        channel = interaction.channel
        claimer_id = TICKET_CLAIMED.pop(channel.id, None)
        if claimer_id is None:
            await interaction.response.send_message("❌ This ticket hasn't been claimed.", ephemeral=True)
            return
        _save_tickets()
        await _update_ticket_header_claim(channel, claimed_by=None)

        embed = discord.Embed(
            title="↩️ Ticket Unclaimed",
            description=f"{interaction.user.mention} has unclaimed this ticket. Any staff can pick it up.",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="TrapAI Ticket System")
        await interaction.response.send_message(embed=embed)

        await log(
            interaction.guild,
            "tickets",
            "Ticket Unclaimed",
            (
                f"**Channel:** {channel.mention}\n"
                f"**Unclaimed By:** {interaction.user.mention} (`{interaction.user.id}`)"
            ),
            discord.Color.orange()
        )

    # ── Add User ─────────────────────────────────────────────
    @discord.ui.button(
        label="Add User",
        style=discord.ButtonStyle.primary,
        emoji="➕",
        custom_id="trapai_ticket_adduser",
        row=0
    )
    async def add_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Only staff can add users.", ephemeral=True)
            return
        await interaction.response.send_modal(TicketAddUserModal())

    # ── Remove User ──────────────────────────────────────────
    @discord.ui.button(
        label="Remove User",
        style=discord.ButtonStyle.secondary,
        emoji="➖",
        custom_id="trapai_ticket_removeuser",
        row=0
    )
    async def remove_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Only staff can remove users.", ephemeral=True)
            return
        await interaction.response.send_modal(TicketRemoveUserModal())

    # ── Close Ticket ─────────────────────────────────────────
    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="trapai_ticket_close",
        row=1
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Only staff can close tickets.", ephemeral=True)
            return

        channel = interaction.channel
        guild = interaction.guild

        # Find ticket owner
        owner_id = None
        for uid, cid in list(TICKETS.get(guild.id, {}).items()):
            if cid == channel.id:
                owner_id = uid
                break

        ticket_type = TICKET_TYPE.get(channel.id, "General")
        claimer_id = TICKET_CLAIMED.get(channel.id)
        claimer = guild.get_member(claimer_id) if claimer_id else None

        confirm_embed = discord.Embed(
            title="🔒 Closing Ticket",
            description=(
                f"This ticket will be **deleted in 5 seconds**.\n\n"
                f"**Closed by:** {interaction.user.mention}\n"
                f"**Claimed by:** {claimer.mention if claimer else 'Unclaimed'}\n"
                f"**Type:** {ticket_type}"
            ),
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        confirm_embed.set_footer(text="TrapAI Ticket System")
        await interaction.response.send_message(embed=confirm_embed)

        await log(
            guild,
            "tickets",
            "Ticket Closed",
            (
                f"**Channel:** `{channel.name}`\n"
                f"**Closed By:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                f"**Ticket Owner:** {'<@' + str(owner_id) + '>' if owner_id else 'Unknown'}\n"
                f"**Claimed By:** {claimer.mention if claimer else 'Never claimed'}\n"
                f"**Type:** {ticket_type}"
            ),
            discord.Color.red()
        )

        await _send_closed_ticket_transcript(guild, channel, interaction.user, owner_id, claimer, ticket_type)

        await asyncio.sleep(5)

        # Clean up tracking
        if guild.id in TICKETS and owner_id:
            TICKETS[guild.id].pop(owner_id, None)
        TICKET_CLAIMED.pop(channel.id, None)
        TICKET_TYPE.pop(channel.id, None)
        TICKET_PRIORITY.pop(channel.id, None)
        TICKET_LOCKED.pop(channel.id, None)
        _save_tickets()

        try:
            await channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.HTTPException:
            pass

    # ── Rename Ticket ────────────────────────────────────────
    @discord.ui.button(
        label="Rename",
        style=discord.ButtonStyle.secondary,
        emoji="✏️",
        custom_id="trapai_ticket_rename",
        row=1
    )
    async def rename_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Only staff can rename tickets.", ephemeral=True)
            return
        await interaction.response.send_modal(TicketRenameModal())

    # ── Set Priority ─────────────────────────────────────────
    @discord.ui.button(
        label="Priority",
        style=discord.ButtonStyle.secondary,
        emoji="🔴",
        custom_id="trapai_ticket_priority",
        row=1
    )
    async def set_priority(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Only staff can set priority.", ephemeral=True)
            return
        await interaction.response.send_modal(TicketPriorityModal())

    # ── Transcript ───────────────────────────────────────────
    @discord.ui.button(
        label="Transcript",
        style=discord.ButtonStyle.secondary,
        emoji="📄",
        custom_id="trapai_ticket_transcript",
        row=1
    )
    async def save_transcript(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Only staff can save transcripts.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = interaction.channel
        ticket_type = TICKET_TYPE.get(channel.id, "ticket")
        text, filename, msg_count = await _build_ticket_transcript(channel)
        await interaction.followup.send(
            content=f"📄 Transcript for **{channel.name}** (`{msg_count}` messages):",
            file=discord.File(fp=io.BytesIO(text.encode()), filename=filename),
            ephemeral=True
        )
        await log(
            interaction.guild,
            "tickets",
            "Ticket Transcript Saved",
            (
                f"**Channel:** {channel.mention}\n"
                f"**Saved By:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                f"**Messages:** {msg_count}\n"
                f"**Type:** {ticket_type}"
            ),
            discord.Color.blurple()
        )

    # ── Lock / Unlock Ticket ──────────────────────────────────
    @discord.ui.button(
        label="Lock",
        style=discord.ButtonStyle.danger,
        emoji="🔐",
        custom_id="trapai_ticket_lock",
        row=2
    )
    async def lock_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Only staff can lock tickets.", ephemeral=True)
            return
        channel = interaction.channel
        guild = interaction.guild
        # Find ticket owner
        owner_id = next(
            (uid for uid, cid in TICKETS.get(guild.id, {}).items() if cid == channel.id),
            None
        )
        owner = guild.get_member(owner_id) if owner_id else None
        if owner:
            await channel.set_permissions(owner, send_messages=False)
        TICKET_LOCKED[channel.id] = True
        _save_tickets()
        embed = discord.Embed(
            description="🔐 Ticket **locked** — the opener can no longer send messages.",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Locked by {interaction.user}")
        await interaction.response.send_message(embed=embed)

    @discord.ui.button(
        label="Unlock",
        style=discord.ButtonStyle.success,
        emoji="🔓",
        custom_id="trapai_ticket_unlock",
        row=2
    )
    async def unlock_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Only staff can unlock tickets.", ephemeral=True)
            return
        channel = interaction.channel
        guild = interaction.guild
        owner_id = next(
            (uid for uid, cid in TICKETS.get(guild.id, {}).items() if cid == channel.id),
            None
        )
        owner = guild.get_member(owner_id) if owner_id else None
        if owner:
            await channel.set_permissions(owner, send_messages=True)
        TICKET_LOCKED[channel.id] = False
        _save_tickets()
        embed = discord.Embed(
            description="🔓 Ticket **unlocked** — the opener can send messages again.",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Unlocked by {interaction.user}")
        await interaction.response.send_message(embed=embed)


# ============================================================
# TICKET MODALS
# ============================================================
class TicketAddUserModal(discord.ui.Modal, title="➕ Add User to Ticket"):
    user_input = discord.ui.TextInput(
        label="User ID or @mention",
        placeholder="e.g. 123456789012345678",
        min_length=1, max_length=32
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.user_input.value.strip().lstrip("<@!").rstrip(">")
        try:
            uid = int(raw)
        except ValueError:
            await interaction.response.send_message("❌ Invalid user ID.", ephemeral=True)
            return
        member = interaction.guild.get_member(uid)
        if not member:
            await interaction.response.send_message("❌ Member not found in this server.", ephemeral=True)
            return
        await interaction.channel.set_permissions(
            member, view_channel=True, send_messages=True, read_message_history=True
        )
        embed = discord.Embed(
            description=f"➕ {member.mention} has been added to this ticket.",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Added by {interaction.user}")
        await interaction.response.send_message(embed=embed)


class TicketRemoveUserModal(discord.ui.Modal, title="➖ Remove User from Ticket"):
    user_input = discord.ui.TextInput(
        label="User ID or @mention",
        placeholder="e.g. 123456789012345678",
        min_length=1, max_length=32
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.user_input.value.strip().lstrip("<@!").rstrip(">")
        try:
            uid = int(raw)
        except ValueError:
            await interaction.response.send_message("❌ Invalid user ID.", ephemeral=True)
            return
        member = interaction.guild.get_member(uid)
        if not member:
            await interaction.response.send_message("❌ Member not found in this server.", ephemeral=True)
            return
        await interaction.channel.set_permissions(member, overwrite=None)
        embed = discord.Embed(
            description=f"➖ {member.mention} has been removed from this ticket.",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Removed by {interaction.user}")
        await interaction.response.send_message(embed=embed)


class TicketRenameModal(discord.ui.Modal, title="✏️ Rename Ticket"):
    new_name = discord.ui.TextInput(
        label="New channel name",
        placeholder="e.g. billing-issue",
        min_length=1, max_length=80
    )

    async def on_submit(self, interaction: discord.Interaction):
        safe = self.new_name.value.lower().strip().replace(" ", "-")
        old = interaction.channel.name
        try:
            await interaction.channel.edit(name=safe)
            embed = discord.Embed(
                description=f"✏️ Ticket renamed: `{old}` → `{safe}`",
                color=discord.Color.blurple(),
                timestamp=discord.utils.utcnow()
            )
            embed.set_footer(text=f"Renamed by {interaction.user}")
            await interaction.response.send_message(embed=embed)
        except discord.HTTPException:
            await interaction.response.send_message("❌ Failed to rename the channel.", ephemeral=True)


class TicketPriorityModal(discord.ui.Modal, title="🔴 Set Ticket Priority"):
    priority_input = discord.ui.TextInput(
        label="Priority (low / medium / high / critical)",
        placeholder="e.g. high",
        min_length=1, max_length=10
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.priority_input.value.strip().lower()
        mapping = {
            "low":      "🟢 Low",
            "medium":   "🟡 Medium",
            "high":     "🔴 High",
            "critical": "🚨 Critical",
        }
        label = mapping.get(raw)
        if not label:
            await interaction.response.send_message(
                "❌ Invalid priority. Use: `low`, `medium`, `high`, or `critical`.",
                ephemeral=True
            )
            return
        TICKET_PRIORITY[interaction.channel.id] = label
        _save_tickets()
        embed = discord.Embed(
            description=f"Priority set to **{label}** for this ticket.",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Set by {interaction.user}")
        await interaction.response.send_message(embed=embed)
        await log(
            interaction.guild,
            "tickets",
            "Ticket Priority Set",
            (
                f"**Channel:** {interaction.channel.mention}\n"
                f"**Priority:** {label}\n"
                f"**Set By:** {interaction.user.mention} (`{interaction.user.id}`)"
            ),
            discord.Color.orange()
        )


# ============================================================
# OAUTH VERIFY VIEW — real "Authenticate via Discord" link button
# ============================================================
class OAuthVerifyView(discord.ui.View):
    """A genuine Discord OAuth2 consent-screen link button, handled by the
    separately-hosted oauth_server.py. Link buttons fire no interaction at
    all (Discord just opens the URL client-side), so this needs no
    custom_id and no bot.add_view() registration to survive a restart —
    unlike VerifyView below, which IS an interactive button."""
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="Authenticate via Discord",
            style=discord.ButtonStyle.link,
            emoji="✅",
            url=_build_oauth_authorize_url(guild_id)
        ))


# ============================================================
# EXIT SURVEY (DM sent when a member leaves — best effort, not persistent
# across restarts since it's a short-lived one-off prompt, not a long-running
# panel like the giveaway/poll views)
# ============================================================
EXIT_REASONS = [
    ("😴 Inactive / too busy", "Inactive / too busy to stay active"),
    ("👥 Didn't feel welcome", "Didn't feel welcome in the community"),
    ("⚔️ Drama / toxicity", "Left because of drama or toxicity"),
    ("🔀 Switched to another server", "Switched to a different server"),
    ("🚫 Lost interest", "No longer interested in this community"),
]


class ExitSurveyModal(discord.ui.Modal, title="Why did you leave?"):
    reason = discord.ui.TextInput(
        label="Your reason (optional feedback)",
        style=discord.TextStyle.paragraph,
        placeholder="Tell us what we could've done better...",
        min_length=1, max_length=500
    )

    def __init__(self, guild_id: int, user_id: int, username: str):
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id
        self.username = username

    async def on_submit(self, interaction: discord.Interaction):
        await _record_exit_survey(self.guild_id, self.user_id, self.username, self.reason.value.strip())
        await interaction.response.send_message("✅ Thanks for the feedback — we appreciate it!", ephemeral=True)


class ExitSurveyView(discord.ui.View):
    def __init__(self, guild_id: int, user_id: int, username: str):
        super().__init__(timeout=86400)  # 24h to respond
        self.guild_id = guild_id
        self.user_id = user_id
        self.username = username

        select = discord.ui.Select(
            placeholder="Pick a reason...",
            options=[discord.SelectOption(label=label, value=text) for label, text in EXIT_REASONS]
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        reason = interaction.data["values"][0]
        await _record_exit_survey(self.guild_id, self.user_id, self.username, reason)
        await interaction.response.send_message("✅ Thanks for the feedback — we appreciate it!", ephemeral=True)
        self.stop()

    @discord.ui.button(label="Write in my own reason", style=discord.ButtonStyle.secondary, emoji="✍️", row=1)
    async def other_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ExitSurveyModal(self.guild_id, self.user_id, self.username))


# ============================================================
# VERIFY VIEW (persistent)
# ============================================================
class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Authenticate via Discord",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="hood_verify_button"
    )
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        member = interaction.user

        if guild is None:
            await interaction.response.send_message("❌ This button only works in a server.", ephemeral=True)
            return

        # Block bots / system accounts — they can't be verified and cause silent failures
        if member.bot:
            await interaction.response.send_message("❌ Bot accounts cannot be verified.", ephemeral=True)
            return

        unverified_role = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE)
        verified_role = discord.utils.get(guild.roles, name=VERIFIED_ROLE)
        jail_role = discord.utils.get(guild.roles, name=JAIL_ROLE)

        if jail_role and jail_role in member.roles:
            embed = discord.Embed(
                title="🚫 TrapAI Access Denied",
                description=(
                    f"{member.mention}, your account is currently flagged as **restricted**.\n\n"
                    "You cannot verify while jailed.\n"
                    "Contact staff if you believe this is a mistake."
                ),
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow()
            )
            embed.set_footer(text=f"TrapAI Security • Access Denied • {guild.name}")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if not verified_role:
            await interaction.response.send_message(f"❌ The role **{VERIFIED_ROLE}** was not found.", ephemeral=True)
            return

        # Already verified — tell them immediately, no need to re-run the scan
        if verified_role in member.roles:
            already_embed = discord.Embed(
                title="✅ Already Verified",
                description=(
                    f"{member.mention}, your account is **already verified**.\n\n"
                    "```yaml\n"
                    "Status: VERIFIED\n"
                    "Role Access: ACTIVE\n"
                    "Server Entry: UNLOCKED\n"
                    "```\n"
                    f"You already have full access to **{guild.name}** 🏘️🔥"
                ),
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow()
            )
            already_embed.set_footer(text=f"TrapAI Security • Already Verified • {guild.name}")
            await interaction.response.send_message(embed=already_embed, ephemeral=True)
            return

        scan_embed = discord.Embed(
            title="🤖 TrapAI Security Scan",
            description=(
                "```yaml\n"
                "Status: SCANNING ACCOUNT\n"
                "Threat Check: Running\n"
                "Role Access: Pending\n"
                "Server Entry: Locked\n"
                "```"
            ),
            color=discord.Color.purple(),
            timestamp=discord.utils.utcnow()
        )
        scan_embed.set_footer(text="TrapAI Security • Initializing")
        await interaction.response.send_message(embed=scan_embed, ephemeral=True)

        try:
            await asyncio.sleep(2)

            if unverified_role and unverified_role in member.roles:
                await member.remove_roles(unverified_role, reason="TrapAI button verification")

            await member.add_roles(verified_role, reason="TrapAI button verification")

            success_embed = discord.Embed(
                title="✅ TrapAI Verification Complete",
                description=(
                    f"{member.mention}, your account has been **approved**.\n\n"
                    "```yaml\n"
                    "Threat Check: CLEAR\n"
                    "Role Access: GRANTED\n"
                    "Server Entry: UNLOCKED\n"
                    "Status: VERIFIED\n"
                    "```\n"
                    f"Welcome to **{guild.name}** 🏘️🔥"
                ),
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow()
            )
            success_embed.set_footer(text=f"TrapAI Security • Access Granted • {guild.name}")
            await interaction.edit_original_response(embed=success_embed)

            await log(
                guild,
                "verification",
                "TrapAI Verification Approved",
                f"User: {member.mention}\nMethod: Verify Button\nStatus: Approved",
                discord.Color.green()
            )

        except discord.Forbidden:
            fail_embed = discord.Embed(
                title="❌ TrapAI Role Sync Failed",
                description="I can't manage your roles. Move my bot role higher.",
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow()
            )
            fail_embed.set_footer(text="TrapAI Security • Sync Failed")
            await interaction.edit_original_response(embed=fail_embed)

        except discord.HTTPException:
            fail_embed = discord.Embed(
                title="❌ TrapAI Verification Failed",
                description="Something went wrong while processing your verification.",
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow()
            )
            fail_embed.set_footer(text="TrapAI Security • System Error")
            await interaction.edit_original_response(embed=fail_embed)


# ============================================================
# TICKET OPEN VIEW — dropdown to pick a category (persistent)
# ============================================================

# Categories available in every server the bot is in. Server-specific
# categories (like Division/Court Application) live in GUILD_TICKET_TYPES
# instead, added per-server via ,addticketcategory — they do NOT show up
# in other servers just because this bot is in more than one.
DEFAULT_TICKET_TYPES = {
    "general":   ("🎫 General Support",    "General questions or help",           discord.Color.blurple()),
    "report":    ("🚨 Report a Member",    "Report rule-breaking behaviour",       discord.Color.red()),
    "appeal":    ("📝 Ban / Mute Appeal",  "Appeal a moderation action",           discord.Color.orange()),
    "alliance":  ("🤝 Form a Alliance",    "Alliance or collab requests",          discord.Color.green()),
    "bug":       ("🐛 Bug Report",         "Report a bot or server bug",           discord.Color.dark_orange()),
    "unban":     ("🔓 Unban Request",      "Request to be unbanned",               discord.Color.dark_red()),
    "staff":     ("📋 Staff Application",  "Apply to join the staff team",         discord.Color.from_rgb(88, 101, 242)),
}

# GUILD_TICKET_TYPES[guild_id][key] = (label, description, color) — custom
# ticket categories scoped to a single server. Can also override a default
# key for just that server. Managed via ,addticketcategory / ,removeticketcategory.
GUILD_TICKET_TYPES: dict[int, dict[str, tuple]] = _load_guild_ticket_types()


def _ticket_types_for_guild(guild_id: int) -> dict:
    """Default categories layered with this guild's own custom/override categories."""
    merged = dict(DEFAULT_TICKET_TYPES)
    merged.update(GUILD_TICKET_TYPES.get(guild_id, {}))
    return merged


def _get_ticket_type(guild_id: int, key: str):
    return GUILD_TICKET_TYPES.get(guild_id, {}).get(key) or DEFAULT_TICKET_TYPES.get(key)


# Application template posted automatically when a Court Application ticket opens.
COURT_APPLICATION_TEMPLATE = (
    "**📋 PROFILE**\n"
    "**Usernames (Roblox & Discord):**\n"
    "**Timezone:** North America - NA , Europe / UK - GMT , Asia / Oceania - SGT\n\n"
    "**🔍 QUESTIONS**\n\n"
    "**If a member claims they were griefed, what are the first 3 questions you would ask them?**\n"
    "Answer here <\n\n"
    "**If a screenshot has no names or timestamps, how will you verify it's real?**\n"
    "Answer here <\n\n"
    "**How will you stay calm, professional, and strictly factual during a live trial?**\n"
    "Answer here <\n\n"
    "**Why is solid proof necessary before bringing someone to court?**\n"
    "Answer here <"
)

# Application/intake form posted automatically when a ticket of that type
# opens — one per default category, plus "court" (a guild-custom category
# key used widely enough across servers using this bot to warrant a
# built-in default too, same as before this was generalized). A server can
# override any of these, including for its own custom categories, with
# ,setticketformat — GUILD_TICKET_FORMATS takes priority when set.
DEFAULT_TICKET_FORMATS = {
    "general": (
        "**What do you need help with?**\nAnswer here <\n\n"
        "**Have you already tried anything to fix/resolve it?**\nAnswer here <"
    ),
    "report": (
        "**Who are you reporting? (username + ID if you have it)**\nAnswer here <\n\n"
        "**What rule did they break?**\nAnswer here <\n\n"
        "**When did this happen?**\nAnswer here <\n\n"
        "**Do you have proof? (screenshots/clips)**\nAnswer here <"
    ),
    "appeal": (
        "**What action are you appealing? (ban / mute / timeout / jail / etc.)**\nAnswer here <\n\n"
        "**Who issued it, if you know?**\nAnswer here <\n\n"
        "**Why do you believe it should be reversed?**\nAnswer here <\n\n"
        "**What will you do differently going forward?**\nAnswer here <"
    ),
    "alliance": (
        "**Server name & invite link:**\nAnswer here <\n\n"
        "**Member count:**\nAnswer here <\n\n"
        "**What kind of alliance are you looking for?**\nAnswer here <\n\n"
        "**Who's the point of contact?**\nAnswer here <"
    ),
    "bug": (
        "**What happened?**\nAnswer here <\n\n"
        "**What did you expect to happen instead?**\nAnswer here <\n\n"
        "**Steps to reproduce it:**\nAnswer here <\n\n"
        "**Command/feature involved, if known:**\nAnswer here <"
    ),
    "unban": (
        "**Username & ID at the time of the ban:**\nAnswer here <\n\n"
        "**Why were you banned, as you understand it?**\nAnswer here <\n\n"
        "**Why should you be unbanned?**\nAnswer here <\n\n"
        "**Anything else staff should know?**\nAnswer here <"
    ),
    "staff": (
        "**📋 BASIC INFO**\n"
        "**Discord Username & ID:**\n"
        "**Age:**\n"
        "**Timezone:**\n"
        "**How long have you been in this server?**\n\n"
        "**🎯 EXPERIENCE**\n"
        "**Have you been staff on another server before? Where, and for how long?**\nAnswer here <\n\n"
        "**What positions/roles did you hold there?**\nAnswer here <\n\n"
        "**⏰ AVAILABILITY**\n"
        "**How many hours per week can you realistically commit to staffing?**\nAnswer here <\n\n"
        "**What times of day are you usually active?**\nAnswer here <\n\n"
        "**🧠 SCENARIOS**\n"
        "**Two members are arguing in general chat and it's escalating into insults. What do you do, step by step?**\nAnswer here <\n\n"
        "**A close friend of yours breaks a server rule. How do you handle it?**\nAnswer here <\n\n"
        "**You witness another staff member abusing their permissions. What do you do?**\nAnswer here <\n\n"
        "**A member threatens to leave the server if they don't get unbanned. How do you respond?**\nAnswer here <\n\n"
        "**💬 ABOUT YOU**\n"
        "**Why do you want to join the staff team?**\nAnswer here <\n\n"
        "**What do you think makes a good moderator?**\nAnswer here <\n\n"
        "**Anything else you'd like us to know?**\nAnswer here <"
    ),
    "court": COURT_APPLICATION_TEMPLATE,
}

# GUILD_TICKET_FORMATS[guild_id][key] = template text — per-guild override
# (or brand-new format for a guild-custom category), set via ,setticketformat.
GUILD_TICKET_FORMATS: dict = _load_depth(_load_data("guild_ticket_formats", {}), 1)


def _save_guild_ticket_formats():
    _save_data("guild_ticket_formats", _dump_depth(GUILD_TICKET_FORMATS, 1))


def _get_ticket_format(guild_id: int, key: str):
    return GUILD_TICKET_FORMATS.get(guild_id, {}).get(key) or DEFAULT_TICKET_FORMATS.get(key)


async def _create_ticket_channel(guild, member, ticket_key: str):
    """Create the ticket channel and post the control panel. Returns the channel."""
    ticket_type = _get_ticket_type(guild.id, ticket_key)
    if ticket_type is None:
        return None, None  # category no longer exists for this guild
    label, description, color = ticket_type

    guild_tickets = TICKETS.setdefault(guild.id, {})
    if member.id in guild_tickets:
        existing_id = guild_tickets[member.id]
        if existing_id is None:
            return None, "PENDING"  # another open-ticket request for this member is already in flight
        existing = guild.get_channel(existing_id)
        if existing:
            return None, existing  # already open
        # stale entry (channel no longer exists) — fall through and recreate

    # Reserve this member's ticket slot immediately, before any await, so a
    # double-click / duplicate invocation can't race past the check above
    # and create two ticket channels for the same member.
    guild_tickets[member.id] = None

    try:
        ticket_category = discord.utils.get(guild.categories, name="🎫 Tickets")
        if ticket_category is None:
            ticket_category = await guild.create_category("🎫 Tickets")

        ticket_name = f"ticket-{member.name}".lower().replace(" ", "-")[:80]

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
        }
        for role in guild.roles:
            if role.permissions.manage_messages or role.permissions.administrator:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )

        ticket_channel = await guild.create_text_channel(
            name=ticket_name,
            category=ticket_category,
            overwrites=overwrites,
            reason=f"Ticket opened by {member}",
            topic=f"{label} • Opened by {member} ({member.id})"
        )
    except (discord.Forbidden, discord.HTTPException):
        guild_tickets.pop(member.id, None)
        return None, "ERROR"

    guild_tickets[member.id] = ticket_channel.id
    TICKET_TYPE[ticket_channel.id] = label
    _save_tickets()

    ticket_format = _get_ticket_format(guild.id, ticket_key)

    # Header embed
    instruction_line = (
        "Please fill out the application form below."
        if ticket_format else
        "Please describe your issue in detail."
    )
    embed = discord.Embed(
        title=f"{label}",
        description=(
            f"Welcome {member.mention}! 👋\n\n"
            f"**Category:** {label}\n"
            f"**About:** {description}\n\n"
            "A staff member will be with you shortly.\n"
            f"{instruction_line}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Use the buttons below to manage this ticket."
        ),
        color=color,
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="🙋 Claimed By", value="Unclaimed — waiting for staff", inline=True)
    embed.add_field(name="👤 Opened By", value=member.mention, inline=True)
    embed.set_footer(text=f"TrapAI Ticket System • {guild.name} Support")

    await ticket_channel.send(embed=embed, view=TicketControlView())

    if ticket_format:
        app_embed = discord.Embed(
            title=f"📋 {label} — Application Form",
            description=f"{member.mention}, please fill this out directly in this channel:\n\n{ticket_format}"[:4000],
            color=color,
            timestamp=discord.utils.utcnow()
        )
        app_embed.set_footer(text=f"TrapAI Ticket System • {label}")
        await ticket_channel.send(embed=app_embed)

        # Plain-text copy of the same template in a code block — embed text
        # is fiddly to select cleanly (especially on mobile), so this gives
        # everyone a one-tap "select all" block to copy, paste as a normal
        # message, and fill in the blanks (the ** markers render as real
        # bold again once pasted outside the code block).
        intro = "📋 **Copy the block below, paste it as your reply, then fill in your answers:**\n"
        budget = 2000 - len(intro) - 8  # 8 = the ``` fences + newlines
        copy_text = ticket_format if len(ticket_format) <= budget else ticket_format[:budget - 1] + "…"
        await ticket_channel.send(f"{intro}```\n{copy_text}\n```")

    await ticket_channel.send(member.mention, delete_after=3)

    await log(
        guild,
        "tickets",
        "Ticket Opened",
        (
            f"**User:** {member.mention} (`{member.id}`)\n"
            f"**Channel:** {ticket_channel.mention}\n"
            f"**Type:** {label}"
        ),
        color
    )

    return ticket_channel, None


async def _update_ticket_header_claim(channel, claimed_by: discord.Member = None):
    """Update the '🙋 Claimed By' field on the ticket's header embed (its first
    message) to reflect who currently has it claimed — or clear it back to
    unclaimed. The claim/unclaim buttons and commands only post a separate
    confirmation embed; without this, the original header keeps showing
    'Unclaimed — waiting for staff' forever."""
    try:
        async for msg in channel.history(limit=1, oldest_first=True):
            if not msg.embeds:
                return
            embed = msg.embeds[0]
            value = claimed_by.mention if claimed_by else "Unclaimed — waiting for staff"
            for i, field in enumerate(embed.fields):
                if field.name == "🙋 Claimed By":
                    embed.set_field_at(i, name=field.name, value=value, inline=field.inline)
                    await msg.edit(embed=embed)
                    break
            return
    except (discord.Forbidden, discord.HTTPException):
        pass


async def _build_ticket_transcript(channel) -> tuple[str, str, int]:
    """Render a ticket channel's message history into transcript text.
    Returns (text, filename, message_count) rather than a discord.File —
    a File's underlying stream is single-use, and this transcript needs to
    go to more than one destination (log channel + a DM), so each caller
    builds its own File from this text instead."""
    lines = []
    async for msg in channel.history(limit=500, oldest_first=True):
        ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
        content = msg.content or ""
        embeds_note = f" [+{len(msg.embeds)} embed(s)]" if msg.embeds else ""
        lines.append(f"[{ts}] {msg.author} ({msg.author.id}): {content}{embeds_note}")
    text = "\n".join(lines) or "(no messages)"
    filename = f"transcript-{channel.name}.txt"
    return text, filename, len(lines)


async def _send_closed_ticket_transcript(guild, channel, closer, owner_id, claimer, ticket_type):
    """Post the full transcript to the ticket log channel AND DM a copy to
    whoever closed the ticket — each is independent/best-effort, so a
    missing log channel or closed DMs never blocks the other."""
    try:
        text, filename, msg_count = await _build_ticket_transcript(channel)
    except (discord.Forbidden, discord.HTTPException):
        return

    embed = discord.Embed(
        title="📄 Ticket Transcript",
        description=(
            f"**Channel:** `{channel.name}`\n"
            f"**Closed By:** {closer.mention} (`{closer.id}`)\n"
            f"**Ticket Owner:** {'<@' + str(owner_id) + '>' if owner_id else 'Unknown'}\n"
            f"**Claimed By:** {claimer.mention if claimer else 'Never claimed'}\n"
            f"**Type:** {ticket_type}\n"
            f"**Messages:** {msg_count}"
        ),
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text="TrapAI Ticket System")

    log_channel = _resolve_log_channel(guild, "tickets")
    if log_channel:
        try:
            await log_channel.send(embed=embed, file=discord.File(fp=io.BytesIO(text.encode()), filename=filename))
        except (discord.Forbidden, discord.HTTPException):
            pass

    try:
        await closer.send(embed=embed, file=discord.File(fp=io.BytesIO(text.encode()), filename=filename))
    except (discord.Forbidden, discord.HTTPException):
        pass  # DMs closed — silently skip, same as every other best-effort DM in this file


_CUSTOM_EMOJI_RE = re.compile(r"^<a?:\w+:\d+>$")


def _split_emoji_label(label: str):
    """Split a 'EMOJI Rest of label' string into (emoji, rest) — but only
    if the first token actually looks like an emoji (a unicode emoji or a
    <:name:id> custom one), never an ordinary word. A category added
    without a leading emoji would otherwise have its first word mistaken
    for one; Discord's API rejects that outright, which breaks the WHOLE
    select menu (every category, not just the bad one) since they're all
    sent together in one component."""
    parts = label.split(" ", 1)
    if len(parts) != 2:
        return None, label
    token = parts[0]
    looks_like_emoji = bool(_CUSTOM_EMOJI_RE.match(token)) or not any(c.isascii() and c.isalpha() for c in token)
    if looks_like_emoji:
        return token, parts[1]
    return None, label


class TicketTypeSelect(discord.ui.Select):
    def __init__(self, guild_id: int = None):
        # guild_id=None (used only for the generic persistent-view re-registration
        # on startup) falls back to defaults only — it never affects what an
        # already-sent message visually shows, only how new interactions route.
        categories = _ticket_types_for_guild(guild_id) if guild_id is not None else DEFAULT_TICKET_TYPES
        options = []
        for key, (label, description, color) in list(categories.items())[:25]:
            emoji, text = _split_emoji_label(label)
            options.append(discord.SelectOption(
                label=text[:100],
                value=key,
                emoji=emoji,
                description=description[:100]
            ))
        super().__init__(
            placeholder="📂 Choose a ticket category…",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="trapai_ticket_type_select"
        )

    async def callback(self, interaction: discord.Interaction):
        ticket_key = self.values[0]
        guild = interaction.guild
        member = interaction.user

        # Reset the select's visual state as the very first response —
        # otherwise Discord's client keeps showing whichever option was
        # just picked as still "selected" on this message, which can stop
        # the same option from being reselectable for a second ticket.
        # Re-supplying the same view here is what clears that.
        await interaction.response.edit_message(view=self.view)

        # Duplicate check
        guild_tickets = TICKETS.get(guild.id, {})
        if member.id in guild_tickets:
            existing = guild.get_channel(guild_tickets[member.id])
            if existing:
                await interaction.followup.send(
                    f"❌ You already have an open ticket: {existing.mention}",
                    ephemeral=True
                )
                return

        ticket_channel, already = await _create_ticket_channel(guild, member, ticket_key)

        if already == "PENDING":
            await interaction.followup.send(
                "❌ A ticket is already being created for you — give it a second.", ephemeral=True
            )
            return

        if already == "ERROR":
            await interaction.followup.send(
                "❌ I couldn't create that ticket channel — check my permissions and try again.",
                ephemeral=True
            )
            return

        if ticket_channel is None and already is None:
            await interaction.followup.send(
                "❌ That ticket category is no longer available. Please choose another.",
                ephemeral=True
            )
            return

        if already:
            await interaction.followup.send(
                f"❌ You already have an open ticket: {already.mention}", ephemeral=True
            )
            return

        await interaction.followup.send(
            f"✅ Ticket created: {ticket_channel.mention}", ephemeral=True
        )


class TicketOpenView(discord.ui.View):
    def __init__(self, guild_id: int = None):
        super().__init__(timeout=None)
        self.add_item(TicketTypeSelect(guild_id))


# ============================================================
# VC CONTROL VIEW — full persistent button panel (5 rows × up to 5 buttons)
# ============================================================

def _resolve_vc(guild: discord.Guild, text_channel_id: int):
    """Return the VoiceChannel linked to a temp VC text channel, or None."""
    for vc_id, tc_id in temp_vc_text_channels.items():
        if tc_id == text_channel_id:
            return guild.get_channel(vc_id)
    return None


def _is_vc_owner(member: discord.Member, vc: discord.VoiceChannel) -> bool:
    return temp_vc_owners.get(vc.id) == member.id


def _is_vc_mod(member: discord.Member, vc: discord.VoiceChannel) -> bool:
    return member.id in vc_mods.get(vc.id, set())


def _can_control(member: discord.Member, vc: discord.VoiceChannel) -> bool:
    """Owner or VC-mod can use control buttons."""
    return _is_vc_owner(member, vc) or _is_vc_mod(member, vc)


class VCControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _check(self, interaction: discord.Interaction, owner_only: bool = False):
        vc = _resolve_vc(interaction.guild, interaction.channel_id)
        if not vc:
            await interaction.response.send_message("❌ Could not find the linked voice channel.", ephemeral=True)
            return None
        if owner_only and not _is_vc_owner(interaction.user, vc):
            await interaction.response.send_message("❌ Only the **VC owner** can do that.", ephemeral=True)
            return None
        if not owner_only and not _can_control(interaction.user, vc):
            await interaction.response.send_message("❌ Only the **VC owner or a VC mod** can do that.", ephemeral=True)
            return None
        return vc

    # ── Row 0: Privacy ──────────────────────────────────────
    @discord.ui.button(label="🔒 Lock", style=discord.ButtonStyle.danger, custom_id="vc_btn_lock", row=0)
    async def btn_lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._check(interaction)
        if not vc:
            return
        await _vc_lock(vc)
        embed = discord.Embed(description="🔒 VC **locked** — only permitted users can join.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await _vc_announce(interaction.guild, vc, f"🔒 **{interaction.user.display_name}** locked the VC.")

    @discord.ui.button(label="🔓 Unlock", style=discord.ButtonStyle.success, custom_id="vc_btn_unlock", row=0)
    async def btn_unlock(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._check(interaction)
        if not vc:
            return
        await _vc_unlock(vc)
        embed = discord.Embed(description="🔓 VC **unlocked** — anyone can join.", color=discord.Color.green())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await _vc_announce(interaction.guild, vc, f"🔓 **{interaction.user.display_name}** unlocked the VC.")

    @discord.ui.button(label="👻 Hide", style=discord.ButtonStyle.secondary, custom_id="vc_btn_hide", row=0)
    async def btn_hide(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._check(interaction)
        if not vc:
            return
        await _vc_merge_permissions(vc, interaction.guild.default_role, view_channel=False)
        embed = discord.Embed(description="👻 VC **hidden** from everyone.", color=discord.Color.dark_grey())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await _vc_announce(interaction.guild, vc, f"👻 **{interaction.user.display_name}** hid the VC.")

    @discord.ui.button(label="👀 Show", style=discord.ButtonStyle.secondary, custom_id="vc_btn_show", row=0)
    async def btn_show(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._check(interaction)
        if not vc:
            return
        await _vc_merge_permissions(vc, interaction.guild.default_role, view_channel=True)
        embed = discord.Embed(description="👀 VC is now **visible** to everyone.", color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await _vc_announce(interaction.guild, vc, f"👀 **{interaction.user.display_name}** made the VC visible.")

    @discord.ui.button(label="📋 Info", style=discord.ButtonStyle.secondary, custom_id="vc_btn_info", row=0)
    async def btn_info(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = _resolve_vc(interaction.guild, interaction.channel_id)
        if not vc:
            await interaction.response.send_message("❌ Could not find the linked voice channel.", ephemeral=True)
            return
        owner_id = temp_vc_owners.get(vc.id)
        owner = interaction.guild.get_member(owner_id) if owner_id else None
        mods = vc_mods.get(vc.id, set())
        mod_mentions = ", ".join(f"<@{m}>" for m in mods) if mods else "None"
        banned = vc_banned.get(vc.id, set())
        banned_mentions = ", ".join(f"<@{b}>" for b in banned) if banned else "None"
        members_str = "\n".join(f"• {m.display_name}" for m in vc.members) or "Empty"
        limit_str = str(vc.user_limit) if vc.user_limit else "No limit"
        ow = vc.overwrites_for(interaction.guild.default_role)
        locked = ow.connect is False
        hidden = ow.view_channel is False
        embed = discord.Embed(title=f"🎤 {vc.name}", color=discord.Color.dark_grey(), timestamp=discord.utils.utcnow())
        embed.add_field(name="👑 Owner", value=owner.mention if owner else "Unknown", inline=True)
        embed.add_field(name="👥 Count", value=f"{len(vc.members)}/{limit_str}", inline=True)
        embed.add_field(name="🔒 Locked", value="Yes" if locked else "No", inline=True)
        embed.add_field(name="👻 Hidden", value="Yes" if hidden else "No", inline=True)
        embed.add_field(name="🔊 Bitrate", value=f"{vc.bitrate // 1000}kbps", inline=True)
        embed.add_field(name="🌐 Region", value=str(vc.rtc_region) if vc.rtc_region else "Auto", inline=True)
        embed.add_field(name="🛡 VC Mods", value=mod_mentions, inline=False)
        embed.add_field(name="🚫 Banned", value=banned_mentions, inline=False)
        embed.add_field(name="🎙️ Members", value=members_str, inline=False)
        embed.set_footer(text=f"TrapAI VC System • {interaction.guild.name}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Row 1: Channel settings ──────────────────────────────
    @discord.ui.button(label="✏️ Rename", style=discord.ButtonStyle.primary, custom_id="vc_btn_rename", row=1)
    async def btn_rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._check(interaction)
        if not vc:
            return
        await interaction.response.send_modal(VCRenameModal(vc))

    @discord.ui.button(label="👥 Limit", style=discord.ButtonStyle.primary, custom_id="vc_btn_limit", row=1)
    async def btn_limit(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._check(interaction)
        if not vc:
            return
        await interaction.response.send_modal(VCLimitModal(vc))

    @discord.ui.button(label="🔊 Bitrate", style=discord.ButtonStyle.primary, custom_id="vc_btn_bitrate", row=1)
    async def btn_bitrate(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._check(interaction)
        if not vc:
            return
        await interaction.response.send_modal(VCBitrateModal(vc))

    @discord.ui.button(label="🌐 Region", style=discord.ButtonStyle.primary, custom_id="vc_btn_region", row=1)
    async def btn_region(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._check(interaction)
        if not vc:
            return
        await interaction.response.send_modal(VCRegionModal(vc))

    # ── Row 2: Member access ─────────────────────────────────
    @discord.ui.button(label="✅ Permit", style=discord.ButtonStyle.success, custom_id="vc_btn_permit", row=2)
    async def btn_permit(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._check(interaction)
        if not vc:
            return
        await interaction.response.send_modal(VCPermitModal(vc, interaction.guild))

    @discord.ui.button(label="👢 Kick", style=discord.ButtonStyle.danger, custom_id="vc_btn_kick", row=2)
    async def btn_kick(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._check(interaction)
        if not vc:
            return
        await interaction.response.send_modal(VCKickModal(vc, interaction.guild))

    @discord.ui.button(label="🚫 Ban", style=discord.ButtonStyle.danger, custom_id="vc_btn_ban", row=2)
    async def btn_ban(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._check(interaction)
        if not vc:
            return
        await interaction.response.send_modal(VCBanModal(vc, interaction.guild))

    @discord.ui.button(label="✔️ Unban", style=discord.ButtonStyle.success, custom_id="vc_btn_unban", row=2)
    async def btn_unban(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._check(interaction)
        if not vc:
            return
        await interaction.response.send_modal(VCUnbanModal(vc, interaction.guild))

    @discord.ui.button(label="🔇 Mute", style=discord.ButtonStyle.secondary, custom_id="vc_btn_mute", row=2)
    async def btn_mute(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._check(interaction)
        if not vc:
            return
        await interaction.response.send_modal(VCMuteModal(vc, interaction.guild))

    # ── Row 3: Ownership ─────────────────────────────────────
    @discord.ui.button(label="👑 Transfer", style=discord.ButtonStyle.primary, custom_id="vc_btn_transfer", row=3)
    async def btn_transfer(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._check(interaction, owner_only=True)
        if not vc:
            return
        await interaction.response.send_modal(VCTransferModal(vc, interaction.guild))

    @discord.ui.button(label="🛡 Add Mod", style=discord.ButtonStyle.primary, custom_id="vc_btn_addmod", row=3)
    async def btn_addmod(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._check(interaction, owner_only=True)
        if not vc:
            return
        await interaction.response.send_modal(VCAddModModal(vc, interaction.guild))

    @discord.ui.button(label="🔕 Deafen", style=discord.ButtonStyle.secondary, custom_id="vc_btn_deafen", row=3)
    async def btn_deafen(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._check(interaction)
        if not vc:
            return
        await interaction.response.send_modal(VCDeafenModal(vc, interaction.guild))

    @discord.ui.button(label="🔊 Undeafen", style=discord.ButtonStyle.secondary, custom_id="vc_btn_undeafen", row=3)
    async def btn_undeafen(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._check(interaction)
        if not vc:
            return
        await interaction.response.send_modal(VCUndeafenModal(vc, interaction.guild))

    @discord.ui.button(label="🔊 Unmute", style=discord.ButtonStyle.success, custom_id="vc_btn_unmute", row=3)
    async def btn_unmute(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._check(interaction)
        if not vc:
            return
        await interaction.response.send_modal(VCUnmuteModal(vc, interaction.guild))


# ============================================================
# HELPER: post an announcement inside the VC text channel
# ============================================================
async def _vc_announce(guild: discord.Guild, vc: discord.VoiceChannel, message: str):
    text_id = temp_vc_text_channels.get(vc.id)
    if not text_id:
        return
    text_ch = guild.get_channel(text_id)
    if text_ch:
        embed = discord.Embed(description=message, color=discord.Color.dark_grey(), timestamp=discord.utils.utcnow())
        embed.set_footer(text="TrapAI VC System")
        try:
            await text_ch.send(embed=embed)
        except discord.HTTPException:
            pass


# ============================================================
# SHARED VC ACTION HELPERS
# ============================================================
# The ,vc* text commands and the control-panel buttons/modals both need
# to perform these exact same actions. They used to each have their own
# copy of the logic, which is how the buttons quietly fell out of sync
# with fixes made to the text commands (vc_locked/vc_kicked/vc_owner_bans/
# vc_owner_mods tracking, self/owner guards, etc. only ever landed in one
# of the two places). Routing both through these means a fix only has to
# happen once. Each returns an error string on failure, None on success,
# so callers can format the error however fits their context (embed vs.
# ephemeral interaction reply).

async def _vc_merge_permissions(channel, target, **perms):
    """channel.set_permissions(target, **perms) REPLACES that target's
    ENTIRE overwrite with only the bits passed — it does not merge. Since
    a temp VC's own voice channel doubles as its text chat, Lock/Unlock,
    Hide/Show, Permit/Kick/Ban/Mute and ownership transfer all touch
    overwrites on the SAME channel, and every one of them used to wipe
    out whatever bits the others had set (e.g. locking after hiding
    silently un-hid it; permitting an existing mod stripped their mod
    overwrite bits, including send_messages for a member who'd been
    granted it another way). Fetch-mutate-reapply instead so each call
    only ever touches the specific bits it means to change."""
    ow = channel.overwrites_for(target)
    for key, value in perms.items():
        setattr(ow, key, value)
    if ow.is_empty():
        await channel.set_permissions(target, overwrite=None)
    else:
        await channel.set_permissions(target, overwrite=ow)


async def _vc_lock(ch):
    await _vc_merge_permissions(ch, ch.guild.default_role, connect=False)
    vc_locked.add(ch.id)
    _save_temp_vcs()


async def _vc_unlock(ch):
    await _vc_merge_permissions(ch, ch.guild.default_role, connect=True)
    vc_locked.discard(ch.id)
    _save_temp_vcs()


async def _vc_kick_member(ch, actor, member):
    if member == actor:
        return "❌ You can't VC kick yourself."
    if _is_vc_owner(member, ch):
        return "❌ You can't kick the VC owner."
    if not member.voice or member.voice.channel != ch:
        return "❌ That user is not in your VC."
    await member.move_to(None)
    await _vc_merge_permissions(ch, member, connect=False)
    vc_kicked.setdefault(ch.id, set()).add(member.id)
    _save_temp_vcs()
    return None


async def _vc_ban_member(ch, guild_id, actor, member):
    if member == actor:
        return "❌ You cannot VC ban yourself."
    if _is_vc_owner(member, ch):
        return "❌ You can't ban the VC owner."
    await ch.set_permissions(member, connect=False, view_channel=False)
    vc_banned.setdefault(ch.id, set()).add(member.id)
    owner_id = temp_vc_owners.get(ch.id)
    if owner_id is not None:
        vc_owner_bans.setdefault(guild_id, {}).setdefault(owner_id, set()).add(member.id)
    _save_temp_vcs()
    if member.voice and member.voice.channel == ch:
        await member.move_to(None)
    return None


async def _vc_unban_member(ch, guild_id, member):
    await ch.set_permissions(member, overwrite=None)
    vc_banned.get(ch.id, set()).discard(member.id)
    owner_id = temp_vc_owners.get(ch.id)
    if owner_id is not None:
        vc_owner_bans.get(guild_id, {}).get(owner_id, set()).discard(member.id)
    _save_temp_vcs()


async def _vc_permit_member(ch, guild_id, member):
    await _vc_merge_permissions(ch, member, connect=True, view_channel=True)
    # A permit lifts any standing ban too — see the text-command version
    # of ,vcpermit for why this matters.
    vc_banned.get(ch.id, set()).discard(member.id)
    owner_id = temp_vc_owners.get(ch.id)
    if owner_id is not None:
        vc_owner_bans.get(guild_id, {}).get(owner_id, set()).discard(member.id)
    _save_temp_vcs()


async def _vc_transfer_ownership(ch, guild, member):
    if not member.voice or member.voice.channel != ch:
        return "❌ That user must be in your VC."
    old_owner_id = temp_vc_owners.get(ch.id)
    old_owner = guild.get_member(old_owner_id) if old_owner_id else None
    temp_vc_owners[ch.id] = member.id
    _save_temp_vcs()
    # One merged overwrite with every owner bit — a temp VC's own text
    # chat lives in this same channel object, so a second, separate
    # set_permissions() call here (as this used to do) would silently
    # wipe out whichever bits the first call had just set, since
    # set_permissions() replaces rather than merges.
    await _vc_merge_permissions(
        ch, member,
        manage_channels=True, manage_permissions=True, move_members=True,
        connect=True, speak=True, view_channel=True, send_messages=True, read_message_history=True,
    )
    # Revoke the previous owner's channel-admin overwrite — otherwise
    # they'd keep native Discord control even though the bot now
    # considers someone else the owner.
    if old_owner and old_owner.id != member.id:
        try:
            await ch.set_permissions(old_owner, overwrite=None)
        except (discord.Forbidden, discord.HTTPException):
            pass
    return None


async def _vc_add_mod(ch, guild_id, actor, member):
    if not member.voice or member.voice.channel != ch:
        return "❌ That user must be in your VC."
    vc_mods.setdefault(ch.id, set()).add(member.id)
    vc_owner_mods.setdefault(guild_id, {}).setdefault(actor.id, set()).add(member.id)
    _save_temp_vcs()
    await _vc_merge_permissions(ch, member, move_members=True, mute_members=True, deafen_members=True, manage_channels=True)
    return None


# ============================================================
# MODALS — one per button action that needs input
# ============================================================

class VCRenameModal(discord.ui.Modal, title="✏️ Rename VC"):
    new_name = discord.ui.TextInput(label="New name", placeholder="e.g. Glock30 Hangout", min_length=1, max_length=100)

    def __init__(self, vc):
        super().__init__()
        self.vc = vc

    async def on_submit(self, interaction: discord.Interaction):
        old = self.vc.name
        await self.vc.edit(name=self.new_name.value[:100])
        await interaction.response.send_message(f"✏️ Renamed **{old}** → **{self.new_name.value[:100]}**", ephemeral=True)
        await _vc_announce(interaction.guild, self.vc, f"✏️ **{interaction.user.display_name}** renamed the VC to **{self.new_name.value[:100]}**.")


class VCLimitModal(discord.ui.Modal, title="👥 Set User Limit"):
    limit = discord.ui.TextInput(label="Limit (0 = no limit, max 99)", placeholder="e.g. 5", min_length=1, max_length=2)

    def __init__(self, vc):
        super().__init__()
        self.vc = vc

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = int(self.limit.value)
        except ValueError:
            await interaction.response.send_message("❌ Enter a number 0–99.", ephemeral=True)
            return
        if value < 0 or value > 99:
            await interaction.response.send_message("❌ Limit must be 0–99.", ephemeral=True)
            return
        await self.vc.edit(user_limit=value)
        label = f"**{value}**" if value else "**no limit**"
        await interaction.response.send_message(f"👥 User limit set to {label}.", ephemeral=True)
        await _vc_announce(interaction.guild, self.vc, f"👥 **{interaction.user.display_name}** set the limit to {label}.")


class VCBitrateModal(discord.ui.Modal, title="🔊 Set Bitrate"):
    bitrate = discord.ui.TextInput(label="Bitrate in kbps (8–96)", placeholder="e.g. 64", min_length=1, max_length=2)

    def __init__(self, vc):
        super().__init__()
        self.vc = vc

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = int(self.bitrate.value)
        except ValueError:
            await interaction.response.send_message("❌ Enter a number 8–96.", ephemeral=True)
            return
        if value < 8 or value > 96:
            await interaction.response.send_message("❌ Bitrate must be 8–96 kbps.", ephemeral=True)
            return
        await self.vc.edit(bitrate=value * 1000)
        await interaction.response.send_message(f"🔊 Bitrate set to **{value}kbps**.", ephemeral=True)
        await _vc_announce(interaction.guild, self.vc, f"🔊 **{interaction.user.display_name}** set the bitrate to **{value}kbps**.")


class VCRegionModal(discord.ui.Modal, title="🌐 Set Voice Region"):
    region = discord.ui.TextInput(
        label="Region (auto / us-east / eu-west / etc.)",
        placeholder="auto",
        min_length=1,
        max_length=20
    )

    def __init__(self, vc):
        super().__init__()
        self.vc = vc

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.region.value.strip().lower()
        region_val = None if raw == "auto" else raw
        try:
            await self.vc.edit(rtc_region=region_val)
            label = f"**{raw}**" if region_val else "**auto**"
            await interaction.response.send_message(f"🌐 Region set to {label}.", ephemeral=True)
            await _vc_announce(interaction.guild, self.vc, f"🌐 **{interaction.user.display_name}** set the region to {label}.")
        except discord.HTTPException:
            await interaction.response.send_message("❌ Invalid region. Try: `auto`, `us-east`, `us-west`, `eu-west`, `singapore`, `sydney`, `brazil`, `hongkong`, `russia`, `japan`, `southafrica`, `india`.", ephemeral=True)


class VCPermitModal(discord.ui.Modal, title="✅ Permit User"):
    user_input = discord.ui.TextInput(label="User ID or @mention", placeholder="e.g. 123456789", min_length=1, max_length=30)

    def __init__(self, vc, guild):
        super().__init__()
        self.vc = vc
        self.guild = guild

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.user_input.value.strip().replace("<@", "").replace(">", "").replace("!", "")
        try:
            uid = int(raw)
        except ValueError:
            await interaction.response.send_message("❌ Enter a valid user ID.", ephemeral=True)
            return
        member = self.guild.get_member(uid)
        if not member:
            await interaction.response.send_message("❌ Member not found in this server.", ephemeral=True)
            return
        await _vc_permit_member(self.vc, self.guild.id, member)
        await interaction.response.send_message(f"✅ **{member.display_name}** can now join.", ephemeral=True)
        await _vc_announce(interaction.guild, self.vc, f"✅ **{interaction.user.display_name}** permitted **{member.display_name}** to join.")


class VCKickModal(discord.ui.Modal, title="👢 Kick from VC"):
    user_input = discord.ui.TextInput(label="User ID or @mention", placeholder="e.g. 123456789", min_length=1, max_length=30)

    def __init__(self, vc, guild):
        super().__init__()
        self.vc = vc
        self.guild = guild

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.user_input.value.strip().replace("<@", "").replace(">", "").replace("!", "")
        try:
            uid = int(raw)
        except ValueError:
            await interaction.response.send_message("❌ Enter a valid user ID.", ephemeral=True)
            return
        member = self.guild.get_member(uid)
        if not member:
            await interaction.response.send_message("❌ Member not found.", ephemeral=True)
            return
        err = await _vc_kick_member(self.vc, interaction.user, member)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.send_message(f"👢 **{member.display_name}** was kicked from the VC and can't rejoin until the owner leaves the call.", ephemeral=True)
        await _vc_announce(interaction.guild, self.vc, f"👢 **{interaction.user.display_name}** kicked **{member.display_name}** from the VC.")


class VCBanModal(discord.ui.Modal, title="🚫 Ban from VC"):
    user_input = discord.ui.TextInput(label="User ID or @mention", placeholder="e.g. 123456789", min_length=1, max_length=30)

    def __init__(self, vc, guild):
        super().__init__()
        self.vc = vc
        self.guild = guild

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.user_input.value.strip().replace("<@", "").replace(">", "").replace("!", "")
        try:
            uid = int(raw)
        except ValueError:
            await interaction.response.send_message("❌ Enter a valid user ID.", ephemeral=True)
            return
        member = self.guild.get_member(uid)
        if not member:
            await interaction.response.send_message("❌ Member not found.", ephemeral=True)
            return
        err = await _vc_ban_member(self.vc, self.guild.id, interaction.user, member)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.send_message(f"🚫 **{member.display_name}** was banned from the VC.", ephemeral=True)
        await _vc_announce(interaction.guild, self.vc, f"🚫 **{interaction.user.display_name}** banned **{member.display_name}** from the VC.")


class VCUnbanModal(discord.ui.Modal, title="✔️ Unban from VC"):
    user_input = discord.ui.TextInput(label="User ID or @mention", placeholder="e.g. 123456789", min_length=1, max_length=30)

    def __init__(self, vc, guild):
        super().__init__()
        self.vc = vc
        self.guild = guild

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.user_input.value.strip().replace("<@", "").replace(">", "").replace("!", "")
        try:
            uid = int(raw)
        except ValueError:
            await interaction.response.send_message("❌ Enter a valid user ID.", ephemeral=True)
            return
        member = self.guild.get_member(uid)
        if not member:
            await interaction.response.send_message("❌ Member not found.", ephemeral=True)
            return
        await _vc_unban_member(self.vc, self.guild.id, member)
        await interaction.response.send_message(f"✔️ **{member.display_name}** can join again.", ephemeral=True)
        await _vc_announce(interaction.guild, self.vc, f"✔️ **{interaction.user.display_name}** unbanned **{member.display_name}**.")


class VCMuteModal(discord.ui.Modal, title="🔇 Server-Mute in VC"):
    user_input = discord.ui.TextInput(label="User ID or @mention", placeholder="e.g. 123456789", min_length=1, max_length=30)

    def __init__(self, vc, guild):
        super().__init__()
        self.vc = vc
        self.guild = guild

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.user_input.value.strip().replace("<@", "").replace(">", "").replace("!", "")
        try:
            uid = int(raw)
        except ValueError:
            await interaction.response.send_message("❌ Enter a valid user ID.", ephemeral=True)
            return
        member = self.guild.get_member(uid)
        if not member:
            await interaction.response.send_message("❌ Member not found.", ephemeral=True)
            return
        if not member.voice or member.voice.channel != self.vc:
            await interaction.response.send_message("❌ That user is not in your VC.", ephemeral=True)
            return
        await member.edit(mute=True)
        await interaction.response.send_message(f"🔇 **{member.display_name}** has been server-muted.", ephemeral=True)
        await _vc_announce(interaction.guild, self.vc, f"🔇 **{interaction.user.display_name}** server-muted **{member.display_name}**.")


class VCUnmuteModal(discord.ui.Modal, title="🔊 Unmute in VC"):
    user_input = discord.ui.TextInput(label="User ID or @mention", placeholder="e.g. 123456789", min_length=1, max_length=30)

    def __init__(self, vc, guild):
        super().__init__()
        self.vc = vc
        self.guild = guild

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.user_input.value.strip().replace("<@", "").replace(">", "").replace("!", "")
        try:
            uid = int(raw)
        except ValueError:
            await interaction.response.send_message("❌ Enter a valid user ID.", ephemeral=True)
            return
        member = self.guild.get_member(uid)
        if not member:
            await interaction.response.send_message("❌ Member not found.", ephemeral=True)
            return
        await member.edit(mute=False)
        await interaction.response.send_message(f"🔊 **{member.display_name}** has been unmuted.", ephemeral=True)
        await _vc_announce(interaction.guild, self.vc, f"🔊 **{interaction.user.display_name}** unmuted **{member.display_name}**.")


class VCDeafenModal(discord.ui.Modal, title="🔕 Server-Deafen in VC"):
    user_input = discord.ui.TextInput(label="User ID or @mention", placeholder="e.g. 123456789", min_length=1, max_length=30)

    def __init__(self, vc, guild):
        super().__init__()
        self.vc = vc
        self.guild = guild

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.user_input.value.strip().replace("<@", "").replace(">", "").replace("!", "")
        try:
            uid = int(raw)
        except ValueError:
            await interaction.response.send_message("❌ Enter a valid user ID.", ephemeral=True)
            return
        member = self.guild.get_member(uid)
        if not member:
            await interaction.response.send_message("❌ Member not found.", ephemeral=True)
            return
        if not member.voice or member.voice.channel != self.vc:
            await interaction.response.send_message("❌ That user is not in your VC.", ephemeral=True)
            return
        await member.edit(deafen=True)
        await interaction.response.send_message(f"🔕 **{member.display_name}** has been server-deafened.", ephemeral=True)
        await _vc_announce(interaction.guild, self.vc, f"🔕 **{interaction.user.display_name}** server-deafened **{member.display_name}**.")


class VCUndeafenModal(discord.ui.Modal, title="🔊 Undeafen in VC"):
    user_input = discord.ui.TextInput(label="User ID or @mention", placeholder="e.g. 123456789", min_length=1, max_length=30)

    def __init__(self, vc, guild):
        super().__init__()
        self.vc = vc
        self.guild = guild

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.user_input.value.strip().replace("<@", "").replace(">", "").replace("!", "")
        try:
            uid = int(raw)
        except ValueError:
            await interaction.response.send_message("❌ Enter a valid user ID.", ephemeral=True)
            return
        member = self.guild.get_member(uid)
        if not member:
            await interaction.response.send_message("❌ Member not found.", ephemeral=True)
            return
        await member.edit(deafen=False)
        await interaction.response.send_message(f"🔊 **{member.display_name}** has been undeafened.", ephemeral=True)
        await _vc_announce(interaction.guild, self.vc, f"🔊 **{interaction.user.display_name}** undeafened **{member.display_name}**.")


class VCTransferModal(discord.ui.Modal, title="👑 Transfer VC Ownership"):
    user_input = discord.ui.TextInput(label="User ID or @mention (must be in VC)", placeholder="e.g. 123456789", min_length=1, max_length=30)

    def __init__(self, vc, guild):
        super().__init__()
        self.vc = vc
        self.guild = guild

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.user_input.value.strip().replace("<@", "").replace(">", "").replace("!", "")
        try:
            uid = int(raw)
        except ValueError:
            await interaction.response.send_message("❌ Enter a valid user ID.", ephemeral=True)
            return
        member = self.guild.get_member(uid)
        if not member:
            await interaction.response.send_message("❌ Member not found.", ephemeral=True)
            return
        err = await _vc_transfer_ownership(self.vc, self.guild, member)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.send_message(f"👑 Ownership transferred to **{member.display_name}**.", ephemeral=True)
        await _vc_announce(interaction.guild, self.vc, f"👑 **{interaction.user.display_name}** transferred ownership to **{member.display_name}**.")


class VCAddModModal(discord.ui.Modal, title="🛡 Add VC Moderator"):
    user_input = discord.ui.TextInput(label="User ID or @mention (must be in VC)", placeholder="e.g. 123456789", min_length=1, max_length=30)

    def __init__(self, vc, guild):
        super().__init__()
        self.vc = vc
        self.guild = guild

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.user_input.value.strip().replace("<@", "").replace(">", "").replace("!", "")
        try:
            uid = int(raw)
        except ValueError:
            await interaction.response.send_message("❌ Enter a valid user ID.", ephemeral=True)
            return
        member = self.guild.get_member(uid)
        if not member:
            await interaction.response.send_message("❌ Member not found.", ephemeral=True)
            return
        err = await _vc_add_mod(self.vc, self.guild.id, interaction.user, member)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.send_message(f"🛡 **{member.display_name}** is now a VC moderator.", ephemeral=True)
        await _vc_announce(interaction.guild, self.vc, f"🛡 **{interaction.user.display_name}** made **{member.display_name}** a VC moderator.")


# ============================================================
# HELP  — one consolidated, scannable command reference
# ============================================================

# HELP_CATEGORIES[i] = (icon, category name, [bare command names]) —
# every registered command, grouped for ,help / ,cmds. Kept in sync
# manually with COMMAND_CATALOG in oauth_server.py (that file has no
# import relationship with this one — see its docstring).
HELP_CATEGORIES = [
    ('🛡️', 'Moderation', ['kick', 'ban', 'massban', 'massunban', 'pullback', 'mute', 'unmute', 'timeout', 'warn', 'warnings', 'clearwarnings', 'modhistory', 'hardban', 'unhardban', 'hardbans', 'clear', 'purge', 'lock', 'unlock', 'highstaffrole', 'hide', 'unhide', 'slowmode', 'nuke', 'lockdown', 'unlockdown', 'raidmode', 'nickname', 'strip', 'trapwarn', 'trapscan', 'restart']),
    ('🔒', 'Jail & Anti-Raid', ['jail', 'unjail', 'worktime', 'setupjail', 'lockjailed', 'antiraid', 'raidwhitelist', 'wl']),
    ('🤖', 'Verification', ['verify', 'unverify', 'denyverify', 'sendverify', 'setverifybackup']),
    ('🏷️', 'Roles', ['role', 'roleall', 'massrole', 'massunrole', 'restoreallroles', 'autorole', 'setgifrole', 'protectedrole', 'br', 'roles', 'createrolemenu', 'addrole', 'removerole', 'rolemenus']),
    ('🎤', 'Voice Channels', ['vclock', 'vcunlock', 'vchide', 'vcshow', 'vcname', 'vclimit', 'vcbitrate', 'vcregion', 'vckick', 'vcban', 'vcunban', 'vcpermit', 'vcmute', 'vcunmute', 'vcdeafen', 'vcundeafen', 'vctransfer', 'vcclaim', 'vcmod', 'vcremovemod', 'vcstats', 'setupvc', 'setunmutevc', 'd']),
    ('🎶', 'Music', ['play', 'skip', 'pause', 'musicstop', 'musicloop', 'volume', 'nowplaying', 'np', 'musicqueue']),
    ('🎫', 'Tickets', ['sendtickets', 'addticketcategory', 'removeticketcategory', 'ticketcategories', 'setticketformat', 'claimticket', 'closeticket']),
    ('💳', 'Billing', ['subscribe', 'managesubscription', 'subscriptionstatus']),
    ('📊', 'Stats & Info', ['whois', 'chatstats', 'serverstats', 'invites', 'invitelogs', 'inviteleaderboard', 'setinvite', 'milestones', 'setmilestone', 'testmilestone', 'ping', 'exitsurveys']),
    ('✅', 'Vouch', ['vouch', 'unvouch', 'cancelvouch', 'pendingvouches', 'vouches', 'vouchleaderboard', 'vouchstats', 'vouchconfig']),
    ('🎉', 'Giveaways & Polls', ['giveaway', 'giveawayend', 'giveaways', 'setgiveawayrole', 'poll', 'pollend']),
    ('💰', 'Economy & Games', ['balance', 'jobs', 'setjob', 'daily', 'weekly', 'work', 'rob', 'give', 'deposit', 'withdraw', 'leaderboard', 'gamblers', 'slots', 'blackjack', 'coinflip', 'dice', 'duel', 'basketball', 'archery', 'cuppong', '8ball', 'trivia', 'hangman', 'wordle', 'tictactoe', 'connect4', 'checkers', 'chess', 'numguess', 'rockpaperscissors', 'highlow', 'crash', '21questions', 'games', 'shop', 'buyrole', 'setroleshop', 'buyfgvc', 'setvcshop', 'storefront']),
    ('🎂', 'Birthdays', ['birthday', 'removebirthday', 'setbirthday', 'setbirthdaychannel', 'birthdaylist', 'settimezone']),
    ('🚀', 'Boosts & Vanity', ['setboostchannel', 'setvanitycode', 'setvanityrole', 'vanityconfig']),
    ('📋', 'Staff Tools', ['staffpsa', 'task', 'tasklist', 'acceptstaff', 'denystaff', 'setstaffrules', 'setstaffmeeting', 'staffleaderboard', 'staffstats', 'staffwarn', 'staffstrike', 'staffwarnings', 'staffstrikes', 'clearstaffwarnings', 'clearstaffstrikes', 'setstaffofmonthrole', 'setmostactiverole', 'setstaffawardschannel', 'setstaffperks', 'crownstaff', 'setmvprole', 'setmvpawardschannel', 'crownmvp', 'sethalloffamerole', 'setsotmtitle', 'setmvptitle', 'setsotmlounge', 'setmvplounge', 'halloffame']),
    ('⚙️', 'Admin & Setup', ['setup', 'lockunverified', 'backup', 'restore', 'listbackups', 'deletebackup', 'exportconfig', 'setlogchannel', 'setwelcome', 'disablewelcome', 'sendwelcome', 'welcome', 'sendinvite', 'announce', 'setpermittedrole', 'setbotbio', 'setupdatechannel', 'resendupdate']),
    ('🎲', 'Fun & Utility', ['snipe', 'clearsnipe', 'editsnipe', 'quote', 'rules', 'cmds', 'help', 'afk']),
]


def _build_help_embed(guild: discord.Guild) -> discord.Embed:
    """One consolidated embed covering every command — no drill-down,
    no page-by-page buttons, just scroll one message."""
    ticket_only_here = _is_ticket_only_guild(guild)
    categories = HELP_CATEGORIES
    if ticket_only_here:
        # Only show what's actually usable here — everything else is
        # gated off by _ticket_only_mode_gate anyway.
        categories = [
            (icon, cat_name, [n for n in names if n in TICKET_ONLY_ALLOWED_COMMANDS])
            for icon, cat_name, names in HELP_CATEGORIES
        ]
        categories = [(icon, cat_name, names) for icon, cat_name, names in categories if names]

    total = sum(len(names) for _, _, names in categories)
    embed = discord.Embed(
        color=discord.Color.from_rgb(88, 101, 242),
        timestamp=discord.utils.utcnow()
    )
    embed.set_author(
        name="TrapAI Command Reference",
        icon_url=guild.icon.url if guild.icon else None
    )
    intro = (
        f"**{total} commands** across **{len(categories)} categories** — all use the `,` prefix.\n"
        "Example: `,ban @user spamming`"
    )
    if ticket_only_here:
        intro = (
            f"**{total} commands** available — all use the `,` prefix.\n"
            "This bot is running in **ticket-only mode**: open a ticket below to inquire about the full bot."
        )
    if COMMANDS_SITE_URL:
        intro += f"\n\n🌐 **[Browse the full interactive command site]({COMMANDS_SITE_URL})**"
    embed.description = intro
    for icon, cat_name, names in categories:
        chips = " ".join(f"`,{n}`" for n in names)
        embed.add_field(name=f"{icon} {cat_name} ({len(names)})", value=chips, inline=False)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text=f"TrapAI • {guild.name} • {guild.member_count:,} members")
    return embed


# ============================================================
# HELPERS
# ============================================================
def _role_forbidden_reason(guild: discord.Guild) -> str:
    """Diagnose a discord.Forbidden raised while adding/removing a role.
    Role *position* being at the top of the list is NOT the same thing as
    actually having the Manage Roles permission granted to that role —
    a very common real setup mistake — so check which one it actually is
    instead of always blaming hierarchy/position."""
    if not guild.me.guild_permissions.manage_roles:
        return (
            "❌ I don't have the **Manage Roles** permission — go to Server "
            "Settings → Roles → my role and turn it on. (Being positioned at "
            "the top of the role list isn't the same thing as having the "
            "permission — both are required.)"
        )
    return "❌ I can't manage that member's roles. Make sure my role is positioned **above** theirs in Server Settings → Roles."


def parse_jail_duration(duration: str):
    duration = duration.lower().strip()
    time_units = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
        "w": 604800,
        "mo": 2592000,
        "y": 31536000
    }
    try:
        if duration.endswith("mo"):
            unit = "mo"
            amount = int(duration[:-2])
        else:
            unit = duration[-1]
            amount = int(duration[:-1])

        if unit not in time_units or amount <= 0:
            return None

        return amount * time_units[unit]
    except Exception:
        return None


async def get_or_create_muted_role(guild: discord.Guild):
    role = discord.utils.get(guild.roles, name=MUTED_ROLE)
    if role is None:
        role = await guild.create_role(name=MUTED_ROLE, reason="Auto-created mute role")

        async def _lock_down(channel):
            try:
                if isinstance(channel, discord.TextChannel):
                    await channel.set_permissions(role, send_messages=False, add_reactions=False)
                elif isinstance(channel, discord.VoiceChannel):
                    await channel.set_permissions(role, speak=False)
            except discord.HTTPException:
                pass

        # Concurrent, not sequential — this is a one-time cost the first time
        # ,mute is ever used in a server, but with 50 channels that was 50
        # sequential API round-trips before the role was even usable.
        await asyncio.gather(*(_lock_down(c) for c in guild.channels))
    return role


def get_owned_temp_vc(member: discord.Member):
    """Return the VC if member is the owner OR a VC-mod, else None."""
    voice = member.voice
    if not voice or not voice.channel:
        return None
    channel = voice.channel
    if not _can_control(member, channel):
        return None
    return channel


def get_strictly_owned_vc(member: discord.Member):
    """Return the VC only if member is the owner (not just a mod)."""
    voice = member.voice
    if not voice or not voice.channel:
        return None
    channel = voice.channel
    if temp_vc_owners.get(channel.id) != member.id:
        return None
    return channel


async def _sweep_temp_vcs():
    """Startup-only pass over persisted temp VCs. Catches two things a
    running bot handles live via on_voice_state_update but a restart can't:
    channels that were deleted (manually, or the guild is gone) while the
    bot was offline, and channels that sat empty the whole time — nobody
    was around to trigger the leave event that would've cleaned them up."""
    for vc_id in list(temp_vc_owners.keys()):
        channel = bot.get_channel(vc_id)
        if channel is None or len(channel.members) == 0:
            if channel is not None:
                try:
                    await channel.delete(reason="Temp VC empty (cleaned up on startup)")
                except (discord.Forbidden, discord.HTTPException):
                    pass
            temp_vc_owners.pop(vc_id, None)
            temp_vc_text_channels.pop(vc_id, None)
            vc_banned.pop(vc_id, None)
            vc_mods.pop(vc_id, None)
            vc_kicked.pop(vc_id, None)
            vc_locked.discard(vc_id)
    _save_temp_vcs()


async def send_vc_control_panel(channel, owner, voice_channel):
    embed = discord.Embed(
        title="🎛 TrapAI VC Control Panel",
        description=(
            f"🏘️ Welcome to your private VC, {owner.mention}!\n\n"
            f"**Voice Channel:** `{voice_channel.name}`\n"
            f"**Owner:** {owner.mention}\n\n"
            "Use the **buttons** to control everything, or use commands."
        ),
        color=discord.Color.dark_grey(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(
        name="🎛 Row 1 — Privacy",
        value="🔒 Lock  •  🔓 Unlock  •  👻 Hide  •  👀 Show  •  📋 Info",
        inline=False
    )
    embed.add_field(
        name="⚙️ Row 2 — Channel",
        value="✏️ Rename  •  👥 Limit  •  🔊 Bitrate  •  🌐 Region",
        inline=False
    )
    embed.add_field(
        name="👥 Row 3 — Members",
        value="✅ Permit  •  👢 Kick  •  🚫 Ban  •  ✔️ Unban  •  🔇 Mute",
        inline=False
    )
    embed.add_field(
        name="👑 Row 4 — Ownership",
        value="👑 Transfer  •  🛡 Add Mod  •  🔕 Deafen  •  🔊 Undeafen  •  🔊 Unmute",
        inline=False
    )
    embed.add_field(
        name="⌨️ Also available as commands",
        value=(
            "`,vclock` `,vcunlock` `,vchide` `,vcshow`\n"
            "`,vcname` `,vclimit` `,vcbitrate` `,vcregion`\n"
            "`,vckick` `,vcban` `,vcunban` `,vcpermit`\n"
            "`,vcmute` `,vcunmute` `,vcdeafen` `,vcundeafen`\n"
            "`,vctransfer` `,vcclaim` `,vcmod` `,vcremovemod`"
        ),
        inline=False
    )
    embed.add_field(
        name="📌 Notes",
        value=(
            "• Buttons announce every action in this chat automatically.\n"
            "• This chat is deleted along with the VC when it empties.\n"
            "• Transfer ownership before leaving to keep the VC alive."
        ),
        inline=False
    )
    embed.set_thumbnail(url=owner.display_avatar.url)
    embed.set_footer(text=f"TrapAI VC System • {owner.guild.name}")
    await channel.send(embed=embed, view=VCControlView())


async def _apply_jail_overwrites(member: discord.Member):
    """Hide every channel from this member, except channels named 'jail'.
    Fired concurrently across all channels — a server with 50 channels was
    previously making 50 sequential API calls here (one full round-trip
    each), which is most of why ,jail felt slow."""
    guild = member.guild

    async def _hide(channel):
        try:
            if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                await channel.set_permissions(member, view_channel=False, connect=False, reason="Jailed — channel hidden")
            else:
                await channel.set_permissions(member, view_channel=False, reason="Jailed — channel hidden")
        except (discord.Forbidden, discord.HTTPException):
            pass

    targets = [
        channel for channel in guild.channels
        # Skip the jail channel itself so they can still see/type there
        if channel.name != "jail"
        # Skip channels the bot can't manage
        and channel.permissions_for(guild.me).manage_permissions
    ]
    if targets:
        await asyncio.gather(*(_hide(c) for c in targets))

    # Hiding/denying connect doesn't disconnect someone already in a VC —
    # boot them out immediately so they can't just stay put.
    if member.voice and member.voice.channel:
        try:
            await member.move_to(None, reason="Jailed — removed from voice channel")
        except (discord.Forbidden, discord.HTTPException):
            pass


async def _remove_jail_overwrites(member: discord.Member):
    """Remove the jail-applied view_channel overwrite from every channel —
    concurrently, same reasoning as _apply_jail_overwrites above."""
    guild = member.guild

    async def _restore(channel, ow):
        try:
            # Clear just the view_channel/connect bits; preserve any other bits
            ow.view_channel = None
            ow.connect = None
            if ow.is_empty():
                await channel.set_permissions(member, overwrite=None, reason="Unjailed — channel access restored")
            else:
                await channel.set_permissions(member, overwrite=ow, reason="Unjailed — channel access restored")
        except (discord.Forbidden, discord.HTTPException):
            pass

    tasks = []
    for channel in guild.channels:
        ow = channel.overwrites_for(member)
        # Only touch it if we set a deny on view_channel or connect — leave anything else untouched
        if ow.view_channel is False or ow.connect is False:
            tasks.append(_restore(channel, ow))
    if tasks:
        await asyncio.gather(*tasks)


async def _restore_jail_role_snapshot(guild: discord.Guild, member: discord.Member):
    """Give back every role a member held before being jailed. Verified/Unverified
    are deliberately excluded — release still requires re-verifying."""
    snapshot_ids = JAIL_ROLE_SNAPSHOTS.get(guild.id, {}).pop(member.id, [])
    _save_jail_role_snapshots()
    if not snapshot_ids:
        return
    verified_role = discord.utils.get(guild.roles, name=VERIFIED_ROLE)
    unverified_role = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE)
    restore_roles = []
    for rid in snapshot_ids:
        role = guild.get_role(rid)
        if not role or role in (verified_role, unverified_role):
            continue
        if role >= guild.me.top_role:
            continue
        if role not in member.roles:
            restore_roles.append(role)
    if restore_roles:
        try:
            await member.add_roles(*restore_roles, reason="Unjailed — roles restored")
        except (discord.Forbidden, discord.HTTPException):
            pass


async def _dm_jail_release(member, guild: discord.Guild, reason: str, *, moderator=None):
    """DM someone the moment they're released from jail — manual ,unjail,
    natural timer expiry, or an early release from ,worktime knocking
    their remaining sentence to zero. Silently does nothing if their DMs
    are closed."""
    embed = discord.Embed(
        title="🔓 You're Out of Jail!",
        description=(
            f"You've been released from **{guild.name}**'s jail — you're free to "
            "return to chatting and everything else again.\n\n"
            "🏷️ Your previous roles have been restored automatically (if the "
            "Verified role was one of them, you'll need to re-verify)."
        ),
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    if moderator:
        embed.add_field(name="🛡 Released By", value=str(moderator), inline=True)
    embed.add_field(name="📝 Reason", value=reason, inline=False)
    embed.set_footer(text=f"TrapAI • {guild.name}")
    try:
        await member.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass


async def _release_from_jail(guild: discord.Guild, member: discord.Member, reason: str, *, moderator=None):
    """Single release path shared by ,unjail, auto_unjail, and ,worktime's
    early release — restores roles/channel access, cancels the pending
    auto-unjail timer, clears expiry state, and DMs the member. Keeping
    this in one place means all three ways someone can leave jail behave
    identically instead of risking drift between copies."""
    jail_role = discord.utils.get(guild.roles, name=JAIL_ROLE)
    unverified_role = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE)

    if jail_role and jail_role in member.roles:
        try:
            await member.remove_roles(jail_role, reason=reason)
        except (discord.Forbidden, discord.HTTPException):
            pass
    if unverified_role and unverified_role not in member.roles:
        try:
            await member.add_roles(unverified_role, reason="Returned to unverified after jail")
        except (discord.Forbidden, discord.HTTPException):
            pass

    await _restore_jail_role_snapshot(guild, member)
    await _remove_jail_overwrites(member)

    old_task = jailed_users.get((guild.id, member.id))
    if old_task:
        old_task.cancel()
    jailed_users.pop((guild.id, member.id), None)
    _clear_jail_expiry(guild.id, member.id)

    await _dm_jail_release(member, guild, reason, moderator=moderator)


async def auto_unjail(guild_id: int, user_id: int, delay: int, reason: str = "Jail timer expired"):
    await asyncio.sleep(delay)

    guild = bot.get_guild(guild_id)
    if guild is None:
        jailed_users.pop((guild_id, user_id), None)
        _clear_jail_expiry(guild_id, user_id)
        JAIL_ROLE_SNAPSHOTS.get(guild_id, {}).pop(user_id, None)
        _save_jail_role_snapshots()
        return

    member = guild.get_member(user_id)
    if member is None:
        jailed_users.pop((guild_id, user_id), None)
        _clear_jail_expiry(guild_id, user_id)
        JAIL_ROLE_SNAPSHOTS.get(guild_id, {}).pop(user_id, None)
        _save_jail_role_snapshots()
        return

    jail_role = discord.utils.get(guild.roles, name=JAIL_ROLE)

    if jail_role and jail_role in member.roles:
        try:
            await _release_from_jail(guild, member, reason)
            _log_mod_action(guild_id, user_id, "auto_unjail", "System (timer expired)", reason)
            await log(
                guild,
                "jail",
                "Member Auto Unjailed",
                f"User: {member.mention}\nReason: {reason}",
                discord.Color.green()
            )
        except discord.Forbidden:
            await log(
                guild,
                "jail",
                "Auto Unjail Failed",
                f"Could not unjail {member.mention}. Check bot permissions and role position.",
                discord.Color.red()
            )
        except discord.HTTPException:
            pass
    else:
        jailed_users.pop((guild_id, user_id), None)
        _clear_jail_expiry(guild_id, user_id)


async def handle_spam(message):
    # Keyed by (guild_id, user_id) — NOT just user_id — so a member's
    # message activity in one server can never combine with a different
    # server to trigger a false spam warning/timeout.
    user_id = (message.guild.id, message.author.id)
    current_time = time.time()

    spam_tracker.setdefault(user_id, [])
    spam_warnings.setdefault(user_id, 0)

    spam_tracker[user_id].append(current_time)
    spam_tracker[user_id] = [
        t for t in spam_tracker[user_id]
        if current_time - t <= SPAM_TIME_WINDOW
    ]

    if len(spam_tracker[user_id]) >= SPAM_MESSAGE_LIMIT:
        spam_warnings[user_id] += 1
        spam_tracker[user_id].clear()

        warning_count = spam_warnings[user_id]

        if warning_count < SPAM_WARNING_LIMIT:
            embed = discord.Embed(
                title="⚠ Spam Warning",
                description=(
                    f"{message.author.mention}, stop spamming.\n"
                    f"Warning: **{warning_count}/{SPAM_WARNING_LIMIT}**"
                ),
                color=discord.Color.orange(),
                timestamp=discord.utils.utcnow()
            )
            embed.set_footer(text=f"{message.guild.name} Anti-Spam")
            await message.channel.send(embed=embed, delete_after=8)
            await log(
                message.guild,
                "mod",
                "Spam Warning Issued",
                f"User: {message.author.mention}\nWarnings: {warning_count}/{SPAM_WARNING_LIMIT}\nChannel: {message.channel.mention}",
                discord.Color.orange()
            )
        else:
            try:
                until = discord.utils.utcnow() + timedelta(minutes=SPAM_TIMEOUT_MINUTES)
                await message.author.timeout(until, reason="Reached 3 spam warnings")
                spam_warnings[user_id] = 0

                embed = discord.Embed(
                    title="🔇 Auto Timeout",
                    description=(
                        f"{message.author.mention} has been timed out for "
                        f"**{SPAM_TIMEOUT_MINUTES} minute(s)** after reaching "
                        f"**{SPAM_WARNING_LIMIT} spam warnings**."
                    ),
                    color=discord.Color.red(),
                    timestamp=discord.utils.utcnow()
                )
                embed.set_footer(text=f"{message.guild.name} Anti-Spam")
                await message.channel.send(embed=embed)
                await log(
                    message.guild,
                    "mod",
                    "User Auto Timed Out",
                    f"User: {message.author.mention}\nReason: Reached {SPAM_WARNING_LIMIT} spam warnings\nDuration: {SPAM_TIMEOUT_MINUTES} minute(s)\nChannel: {message.channel.mention}",
                    discord.Color.red()
                )
            except discord.Forbidden:
                await log(
                    message.guild,
                    "mod",
                    "Auto Timeout Failed",
                    f"Could not timeout {message.author.mention}. Check bot permissions and role position.",
                    discord.Color.red()
                )
            except discord.HTTPException:
                pass

        return True

    return False


# ============================================================
# MILESTONE HELPER
# ============================================================

# Per-guild override: guild_id → channel_id
# If not set, falls back to a channel named ANNOUNCEMENTS_CHANNEL
_milestone_data = _load_data("milestones", {"channels": {}, "last_fired": {}})
_milestone_channel_overrides: dict[int, int] = _load_depth(_milestone_data.get("channels", {}), 1)

# Track the last milestone fired per guild so rapid join/leave churn
# doesn't double-fire the same milestone
_last_milestone_fired: dict[int, int] = _load_depth(_milestone_data.get("last_fired", {}), 1)


async def _check_milestone(guild: discord.Guild):
    count = guild.member_count
    if count not in MEMBER_MILESTONES:
        return

    # Suppress duplicate fires for the same milestone
    if _last_milestone_fired.get(guild.id) == count:
        return
    _last_milestone_fired[guild.id] = count
    _save_milestones()

    # Resolve announcement channel
    channel = None
    override_id = _milestone_channel_overrides.get(guild.id)
    if override_id:
        channel = guild.get_channel(override_id)
    if not channel:
        channel = discord.utils.get(guild.text_channels, name=ANNOUNCEMENTS_CHANNEL)
    if not channel:
        # fallback: first text channel the bot can send to
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                channel = ch
                break
    if not channel:
        return

    # Choose an emoji tier based on milestone size
    if count >= 10000:
        tier_emoji = "💎"
        tier_label = "Legendary"
    elif count >= 1000:
        tier_emoji = "🏆"
        tier_label = "Major"
    elif count >= 100:
        tier_emoji = "🔥"
        tier_label = "Growing"
    else:
        tier_emoji = "🎉"
        tier_label = "Early"

    embed = discord.Embed(
        title=f"{tier_emoji} {guild.name} just hit **{count:,} members**!",
        description=(
            f"We've reached **{count:,}** members in **{guild.name}**!\n\n"
            f"```yaml\n"
            f"Milestone: {count:,} members\n"
            f"Tier: {tier_label}\n"
            f"Status: UNLOCKED\n"
            f"```\n"
            f"Thank you to everyone who's been part of **{guild.name}** 🏘️🔥\n"
            f"Keep spreading the word and let's hit the next one!"
        ),
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="📊 Current Members", value=f"**{count:,}**", inline=True)
    embed.add_field(name="🎯 Next Milestone", value=f"**{_next_milestone(count):,}**", inline=True)
    embed.add_field(name="🏘️ Server", value=guild.name, inline=True)

    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    embed.set_footer(text=f"TrapAI • {guild.name} Milestones")

    try:
        await channel.send("@everyone", embed=embed)
    except discord.Forbidden:
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass


def _next_milestone(current: int) -> int:
    """Return the next milestone above the current member count."""
    for m in sorted(MEMBER_MILESTONES):
        if m > current:
            return m
    # If beyond all defined milestones, round up to next 10k
    return ((current // 10000) + 1) * 10000


# ============================================================
# BIRTHDAY TRACKER
# ============================================================

def _parse_birthday(raw: str) -> str | None:
    """Parse a birthday into normalized 'MM-DD' form, or None if invalid."""
    raw = raw.strip()
    for fmt in ("%m-%d", "%m/%d", "%B %d", "%b %d", "%d %B", "%d %b"):
        try:
            # Anchor to a fixed leap year so "02-29" parses without needing
            # the user's actual birth year, which we intentionally don't collect.
            dt = datetime.strptime(f"{raw} 2000", f"{fmt} %Y")
            return dt.strftime("%m-%d")
        except ValueError:
            continue
    return None


def _parse_utc_offset(raw: str) -> float | None:
    """Parse a UTC offset like '-5', '+8', 'UTC-5', 'GMT+5:30' into hours (float)."""
    cleaned = raw.strip().upper().replace("UTC", "").replace("GMT", "").strip()
    m = re.fullmatch(r"([+-])?(\d{1,2})(?::(\d{2}))?", cleaned)
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    hours = int(m.group(2))
    minutes = int(m.group(3)) if m.group(3) else 0
    if hours > 14 or minutes >= 60:
        return None
    offset = sign * (hours + minutes / 60)
    if abs(offset) > 14:
        return None
    return offset


def _format_utc_offset(offset: float) -> str:
    sign = "+" if offset >= 0 else "-"
    offset = abs(offset)
    hours = int(offset)
    minutes = round((offset - hours) * 60)
    return f"UTC{sign}{hours}" + (f":{minutes:02d}" if minutes else "")


# US_STATE_TIMEZONES[state name or 2-letter abbreviation] = standard UTC offset.
# States that span more than one zone (Texas, Florida, Michigan, Tennessee,
# Kentucky, Kansas, Nebraska, North/South Dakota, Idaho, Oregon, ...) are
# mapped to whichever zone most of that state's population is in.
US_STATE_TIMEZONES = {
    # Eastern (UTC-5)
    "connecticut": -5, "ct": -5,
    "delaware": -5, "de": -5,
    "district of columbia": -5, "washington dc": -5, "dc": -5,
    "florida": -5, "fl": -5,
    "georgia": -5, "ga": -5,
    "indiana": -5, "in": -5,
    "maine": -5, "me": -5,
    "maryland": -5, "md": -5,
    "massachusetts": -5, "ma": -5,
    "michigan": -5, "mi": -5,
    "new hampshire": -5, "nh": -5,
    "new jersey": -5, "nj": -5,
    "new york": -5, "ny": -5,
    "north carolina": -5, "nc": -5,
    "ohio": -5, "oh": -5,
    "pennsylvania": -5, "pa": -5,
    "rhode island": -5, "ri": -5,
    "south carolina": -5, "sc": -5,
    "vermont": -5, "vt": -5,
    "virginia": -5, "va": -5,
    "west virginia": -5, "wv": -5,
    # Central (UTC-6)
    "alabama": -6, "al": -6,
    "arkansas": -6, "ar": -6,
    "illinois": -6, "il": -6,
    "iowa": -6, "ia": -6,
    "kansas": -6, "ks": -6,
    "kentucky": -6, "ky": -6,
    "louisiana": -6, "la": -6,
    "minnesota": -6, "mn": -6,
    "mississippi": -6, "ms": -6,
    "missouri": -6, "mo": -6,
    "nebraska": -6, "ne": -6,
    "north dakota": -6, "nd": -6,
    "oklahoma": -6, "ok": -6,
    "south dakota": -6, "sd": -6,
    "tennessee": -6, "tn": -6,
    "texas": -6, "tx": -6,
    "wisconsin": -6, "wi": -6,
    # Mountain (UTC-7)
    "arizona": -7, "az": -7,
    "colorado": -7, "co": -7,
    "idaho": -7, "id": -7,
    "montana": -7, "mt": -7,
    "new mexico": -7, "nm": -7,
    "utah": -7, "ut": -7,
    "wyoming": -7, "wy": -7,
    # Pacific (UTC-8)
    "california": -8, "ca": -8,
    "nevada": -8, "nv": -8,
    "oregon": -8, "or": -8,
    "washington": -8, "wa": -8,
    # Alaska (UTC-9)
    "alaska": -9, "ak": -9,
    # Hawaii (UTC-10, no DST)
    "hawaii": -10, "hi": -10,
}

US_ZONE_LABELS = {-5: "Eastern", -6: "Central", -7: "Mountain", -8: "Pacific", -9: "Alaska", -10: "Hawaii"}


def _parse_state_timezone(raw: str) -> float | None:
    return US_STATE_TIMEZONES.get(raw.strip().lower())


def _resolve_birthday_channel(guild: discord.Guild):
    override_id = BIRTHDAY_CHANNEL_OVERRIDES.get(guild.id)
    if override_id:
        ch = guild.get_channel(override_id)
        if ch:
            return ch
    return discord.utils.get(guild.text_channels, name=ANNOUNCEMENTS_CHANNEL)


def _resolve_birthday_timezone(guild_id: int, user_id: int) -> float:
    return BIRTHDAY_TIMEZONES.get(guild_id, {}).get(user_id, 0.0)


async def _check_birthdays():
    """Runs every minute. Each member is checked against their OWN local time
    (UTC + their configured offset), so the announcement fires at their local
    midnight rather than a single shared UTC midnight for everyone."""
    utc_now = discord.utils.utcnow()
    changed = False

    for guild in bot.guilds:
        guild_bdays = BIRTHDAYS.get(guild.id, {})
        if not guild_bdays:
            continue

        due_users = []
        for uid, md in guild_bdays.items():
            offset = _resolve_birthday_timezone(guild.id, uid)
            local_now = utc_now + timedelta(hours=offset)
            if local_now.strftime("%m-%d") != md:
                continue

            local_date_str = local_now.strftime("%Y-%m-%d")
            if _LAST_BIRTHDAY_ANNOUNCE.get(guild.id, {}).get(uid) == local_date_str:
                continue  # already announced for this member's local date

            due_users.append(uid)
            _LAST_BIRTHDAY_ANNOUNCE.setdefault(guild.id, {})[uid] = local_date_str
            changed = True

        if due_users:
            channel = _resolve_birthday_channel(guild)
            if channel:
                mentions = " ".join(f"<@{uid}>" for uid in due_users)
                embed = discord.Embed(
                    title="🎉 Happy Birthday!",
                    description=(
                        f"{mentions}\n\n"
                        f"Wishing you an amazing day from all of us at **{guild.name}**! 🎂🎈"
                    ),
                    color=discord.Color.from_rgb(255, 105, 180),
                    timestamp=discord.utils.utcnow()
                )
                embed.set_footer(text=f"TrapAI • {guild.name} Birthday Tracker")
                try:
                    await channel.send(
                        content=f"@everyone {mentions}",
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions(everyone=True, users=True)
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass

    if changed:
        _save_birthday_last_announced()


async def _birthday_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await _check_birthdays()
        except Exception:
            pass
        await asyncio.sleep(60)


async def _check_staff_meetings():
    """Runs every minute. Fires as soon as the current UTC time reaches
    (not just exactly equals) the scheduled weekday+time, then dedupes on
    calendar date — robust to the loop's tick being delayed, unlike an
    exact-minute-match would be."""
    now = discord.utils.utcnow()
    today_str = now.strftime("%Y-%m-%d")
    changed = False

    for guild in bot.guilds:
        cfg = STAFF_MEETING_CONFIG.get(guild.id)
        if not cfg:
            continue
        if now.weekday() != cfg["weekday"]:
            continue
        if cfg.get("last_sent") == today_str:
            continue
        if (now.hour, now.minute) < (cfg["hour"], cfg["minute"]):
            continue

        channel = guild.get_channel(cfg["channel_id"])
        if not channel:
            continue
        role = guild.get_role(cfg["role_id"]) if cfg.get("role_id") else None
        ping = role.mention if role else "@here"

        embed = discord.Embed(
            title="📅 Staff Meeting Reminder",
            description=f"It's time for this week's staff meeting in **{guild.name}**!",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="TrapAI • Weekly Staff Meeting")
        try:
            await channel.send(
                content=ping, embed=embed,
                allowed_mentions=discord.AllowedMentions(roles=[role] if role else False, everyone=False)
            )
        except (discord.Forbidden, discord.HTTPException):
            continue

        cfg["last_sent"] = today_str
        changed = True

    if changed:
        _save_staff_meeting_config()


async def _staff_meeting_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await _check_staff_meetings()
        except Exception:
            pass
        await asyncio.sleep(60)


# ============================================================
# EVENTS
# ============================================================
_startup_resumed = False


@bot.event
async def on_ready():
    global _startup_resumed
    bot.add_view(VerifyView())
    bot.add_view(TicketOpenView())
    bot.add_view(TicketControlView())
    bot.add_view(VCControlView())
    bot.add_view(MusicControlView())
    bot.add_view(GiveawayView())
    bot.add_view(PSADismissView())
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="discord.gg/glock30"
        )
    )
    # Ticket-only guilds get their nickname applied right away, in case it
    # was set/changed while the bot was offline or the guild's ticket-only
    # status was just added.
    for guild in bot.guilds:
        if _is_ticket_only_guild(guild):
            await _apply_ticket_only_nickname(guild)

    # One-time cleanup: leave any guild explicitly listed in
    # FORCE_LEAVE_GUILD_IDS that the bot is currently in. Safe to run on
    # every on_ready (including gateway reconnects) — once a guild's been
    # left it's no longer in bot.guilds, so this becomes a no-op for it.
    if FORCE_LEAVE_GUILD_IDS:
        for guild in list(bot.guilds):
            if guild.id in FORCE_LEAVE_GUILD_IDS:
                print(f"[force-leave] Leaving '{guild.name}' ({guild.id}) per FORCE_LEAVE_GUILD_IDS.")
                try:
                    await guild.leave()
                except discord.HTTPException as e:
                    print(f"[force-leave] Failed to leave {guild.id}: {e}")

    # Cache current invite use-counts for all guilds
    for guild in bot.guilds:
        try:
            invites = await guild.invites()
            INVITE_CACHE[guild.id] = {inv.code: inv.uses for inv in invites}
        except (discord.Forbidden, discord.HTTPException):
            pass
        # The vanity URL (boost-level-3 only) is a separate API object with
        # its own use counter, not included in guild.invites() at all.
        if guild.vanity_url_code:
            try:
                vanity = await guild.vanity_invite()
                VANITY_INVITE_CACHE[guild.id] = vanity.uses or 0
            except (discord.Forbidden, discord.HTTPException):
                pass

    # One-time startup work — on_ready can re-fire on gateway reconnects,
    # so guard resume logic to avoid double-scheduling jail/giveaway tasks.
    if not _startup_resumed:
        _startup_resumed = True
        asyncio.create_task(_autosave_loop())
        asyncio.create_task(_birthday_loop())
        asyncio.create_task(_staff_meeting_loop())
        asyncio.create_task(_staff_awards_loop())
        asyncio.create_task(_mvp_awards_loop())

        # Post the latest changelog entry to any guild that hasn't seen it
        # yet — covers every kind of restart (Railway redeploy after a
        # code push, manual ,restart, a crash recovery), not just ,restart.
        asyncio.create_task(_announce_updates())

        # ,restart back-online confirmation — only fires once, right after
        # a real restart, never on an ordinary gateway reconnect.
        if _RESTART_NOTIFY_CHANNEL_ID:
            notify_channel = bot.get_channel(_RESTART_NOTIFY_CHANNEL_ID)
            if notify_channel:
                elapsed = time.time() - _RESTART_NOTIFY_AT if _RESTART_NOTIFY_AT else None
                embed = discord.Embed(
                    title="✅ Back Online",
                    description=(
                        f"Restart complete — reconnected in **{elapsed:.1f}s**." if elapsed is not None
                        else "Restart complete."
                    ),
                    color=discord.Color.green(),
                    timestamp=discord.utils.utcnow()
                )
                try:
                    await notify_channel.send(embed=embed)
                except (discord.Forbidden, discord.HTTPException):
                    pass

        # Resume open polls that were still running when the bot restarted:
        # re-register their button views and reschedule any auto-close timer.
        for mid, p in list(POLLS.items()):
            if p.get("closed"):
                continue
            guild_obj = bot.get_guild(p["guild_id"])
            if not guild_obj:
                continue
            bot.add_view(PollView(p["options"]), message_id=int(mid))
            if p.get("closes_at"):
                remaining = p["closes_at"] - discord.utils.utcnow().timestamp()
                asyncio.create_task(
                    _schedule_poll_close(p["guild_id"], p["channel_id"], int(mid), max(0, remaining))
                )

        # Resume jail auto-release timers that were interrupted by a restart
        now = time.time()
        for gid, users in list(JAIL_EXPIRY.items()):
            for uid, info in list(users.items()):
                remaining = info.get("expires_at", now) - now
                old_task = jailed_users.get((gid, uid))
                if old_task:
                    old_task.cancel()
                jailed_users[(gid, uid)] = asyncio.create_task(
                    auto_unjail(gid, uid, max(0, remaining), info.get("reason", "Jail timer expired"))
                )

        # Resume giveaways that were still running when the bot restarted
        for mid, g in list(GIVEAWAYS.items()):
            guild_obj = bot.get_guild(g["guild_id"])
            if not guild_obj:
                GIVEAWAYS.pop(mid, None)
                continue
            remaining = g["ends_at"] - discord.utils.utcnow().timestamp()

            async def _resume_giveaway(guild_obj=guild_obj, channel_id=g["channel_id"], mid=mid, remaining=remaining):
                if remaining > 0:
                    await asyncio.sleep(remaining)
                await _end_giveaway(guild_obj, channel_id, mid)

            asyncio.create_task(_resume_giveaway())
        _save_giveaways()

        await _sweep_temp_vcs()
        _resume_vc_sessions()

    print(f"Logged in as {bot.user}")


@bot.event
async def on_guild_join(guild):
    # Closes off the free-invite path: if HOME_GUILD_IDS is configured, this
    # bot only operates in those servers — anyone who invites it anywhere
    # else gets an immediate, unconditional leave, before any command can
    # ever run there. Left unset, this check is disabled entirely (e.g. if
    # you actually want to run the paid subscription model below).
    if HOME_GUILD_IDS and guild.id not in HOME_GUILD_IDS:
        print(f"[home-guild] Leaving unauthorized guild '{guild.name}' ({guild.id}) — not in HOME_GUILD_IDS.")
        try:
            await guild.leave()
        except discord.HTTPException:
            pass
        return

    # If this guild is configured as ticket-only (TICKET_ONLY_GUILD_IDS),
    # apply its nickname right away so it visibly reads as a dedicated
    # ticket bot there from the moment it joins.
    if _is_ticket_only_guild(guild):
        await _apply_ticket_only_nickname(guild)


@bot.event
async def on_member_join(member):
    # ── Hard-ban check: re-ban immediately if hard-banned ──────
    hb_guild = HARD_BANNED.get(member.guild.id, {})
    if member.id in hb_guild:
        reason = hb_guild[member.id]
        try:
            await member.ban(reason=f"Hard-ban re-applied: {reason}")
            await log(member.guild, "bans", "🔴 Hard-Ban Re-Applied",
                      f"{member.mention} attempted to rejoin but is hard-banned.",
                      discord.Color.dark_red(),
                      fields=[
                          ("🔴 Hard-Banned User", f"{member.mention} (`{member.id}`)", True),
                          ("📝 Original Reason",   reason,                               False),
                      ],
                      target=member)
        except (discord.Forbidden, discord.HTTPException):
            pass
        return

    if ANTI_RAID_ENABLED.get(member.guild.id, True):
        now = time.time()
        joins = RAID_JOINS.setdefault(member.guild.id, [])
        joins.append(now)
        joins[:] = [t for t in joins if now - t <= RAID_TIME]

        guild_whitelist = WHITELIST.get(member.guild.id, set())
        if member.id not in guild_whitelist and len(joins) >= RAID_LIMIT:
            try:
                await member.ban(reason="Anti-raid triggered")
            except (discord.Forbidden, discord.HTTPException):
                pass
            else:
                await log(
                    member.guild,
                    "raids",
                    "Anti-Raid Triggered",
                    "A member was auto-banned by the anti-raid system.",
                    discord.Color.red(),
                    fields=[
                        ("🚨 Banned User",  f"{member.mention} (`{member.id}`)", True),
                        ("🏷️ Username",     str(member),                          True),
                        ("📅 Account Age",  discord.utils.format_dt(member.created_at, "R"), True),
                        ("👥 Raid Joins",   f"{len(joins)} in {RAID_TIME}s",       True),
                    ],
                    target=member
                )
                return

    unverified_role = discord.utils.get(member.guild.roles, name=UNVERIFIED_ROLE)
    if unverified_role:
        try:
            await member.add_roles(unverified_role, reason="Auto verification system")
        except discord.Forbidden:
            await log(
                member.guild,
                "mod",
                "Verification Role Failed",
                f"Could not assign {UNVERIFIED_ROLE} — check bot role position.",
                discord.Color.red(),
                fields=[("👤 Member", f"{member.mention} (`{member.id}`)", True)],
                target=member
            )

    # ── Auto-role: grant all configured roles on join ─────────
    auto_roles = AUTOROLE.get(member.guild.id, [])
    for role_id in auto_roles:
        role = member.guild.get_role(role_id)
        if role:
            try:
                await member.add_roles(role, reason="Auto-role on join")
            except (discord.Forbidden, discord.HTTPException):
                pass

    await log(
        member.guild,
        "joins",
        "Member Joined",
        f"{member.mention} just joined **{member.guild.name}**.",
        discord.Color.green(),
        fields=[
            ("👤 User",          f"{member.mention} (`{member.id}`)",                    False),
            ("🏷️ Username",      str(member),                                            True),
            ("📅 Account Created", discord.utils.format_dt(member.created_at, "F"),      False),
            ("⏱️ Account Age",   discord.utils.format_dt(member.created_at, "R"),        True),
            ("👥 Member #",      str(member.guild.member_count),                         True),
        ],
        target=member
    )

    ch = _resolve_welcome_channel(member.guild)

    if ch:
        # Delegate to the shared helper (also used by ,welcome and ,sendwelcome)
        await _send_welcome_embeds(ch, member)

    # System channel ping (fallback if no #welcome channel)
    if not ch and member.guild.system_channel:
        try:
            await member.guild.system_channel.send(
                f"Welcome {member.mention} to **{member.guild.name}** 🏙️"
            )
        except discord.HTTPException:
            pass

    # DM the new member
    _guild_invite_dm = _resolve_invite_link(member.guild)
    dm_embed = discord.Embed(
        title=f"{member.guild.name} — ACCESS",
        description=(
            f"Welcome to **{member.guild.name}** 🏙️\n\n"
            f"You've just joined **{member.guild.name}**.\n"
            + (f"Join us: **{_guild_invite_dm}**\n\n" if _guild_invite_dm else "\n")
            + "Complete verification to unlock full access."
        ),
        color=discord.Color.from_str("#000000"),
        timestamp=discord.utils.utcnow()
    )
    dm_embed.set_footer(text=f"{member.guild.name} SYSTEM")
    if member.guild.icon:
        dm_embed.set_thumbnail(url=member.guild.icon.url)
    try:
        await member.send(embed=dm_embed)
    except (discord.Forbidden, discord.HTTPException):
        pass  # DMs closed — silently skip

    await _check_milestone(member.guild)

    # ── Invite tracking ───────────────────────────────────────
    guild = member.guild
    try:
        new_invites = await guild.invites()
    except (discord.Forbidden, discord.HTTPException):
        new_invites = []

    old_cache = INVITE_CACHE.get(guild.id, {})
    used_code = None
    inviter = None

    for inv in new_invites:
        old_uses = old_cache.get(inv.code, 0)
        if inv.uses > old_uses:
            used_code = inv.code
            inviter = inv.inviter
            break

    # Update cache
    INVITE_CACHE[guild.id] = {inv.code: inv.uses for inv in new_invites}

    if inviter:
        gdata = INVITE_DATA.setdefault(guild.id, {})
        idata = gdata.setdefault(inviter.id, {"uses": 0, "logs": []})
        idata["uses"] += 1
        idata["logs"].append(
            f"{discord.utils.format_dt(discord.utils.utcnow(), 'F')} — "
            f"{member} (`{member.id}`) joined via `{used_code}`"
        )

        # Post to invite log channel
        inv_log_ch = _resolve_log_channel(member.guild, "invites")
        if inv_log_ch:
            embed = discord.Embed(
                title="📨 Invite Used",
                color=discord.Color.blurple(),
                timestamp=discord.utils.utcnow()
            )
            embed.add_field(name="👤 New Member",  value=f"{member.mention} (`{member.id}`)", inline=False)
            embed.add_field(name="📬 Invited By",  value=f"{inviter.mention} (`{inviter.id}`)", inline=True)
            embed.add_field(name="🔗 Invite Code", value=f"`{used_code}`",                      inline=True)
            embed.add_field(name="📊 Total Invites", value=str(idata["uses"]),                  inline=True)
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text=f"TrapAI Invite Tracker • {member.guild.name}")
            try:
                await inv_log_ch.send(embed=embed)
            except discord.HTTPException:
                pass

    elif guild.vanity_url_code:
        # No regular invite use went up — check the vanity URL separately,
        # since discord.gg/<vanity code> is a completely different API
        # object from guild.invites() and never shows up in that list.
        # There's no "inviter" for a vanity join (it's the server's own
        # permanent link), so this only posts a log entry — nothing to
        # credit on the per-inviter leaderboard.
        try:
            vanity = await guild.vanity_invite()
        except (discord.Forbidden, discord.HTTPException):
            vanity = None
        if vanity:
            old_vanity_uses = VANITY_INVITE_CACHE.get(guild.id, 0)
            VANITY_INVITE_CACHE[guild.id] = vanity.uses or 0
            if (vanity.uses or 0) > old_vanity_uses:
                inv_log_ch = _resolve_log_channel(guild, "invites")
                if inv_log_ch:
                    embed = discord.Embed(
                        title="📨 Invite Used",
                        description="Joined via the server's **vanity invite** — no specific inviter to credit.",
                        color=discord.Color.blurple(),
                        timestamp=discord.utils.utcnow()
                    )
                    embed.add_field(name="👤 New Member", value=f"{member.mention} (`{member.id}`)", inline=False)
                    embed.add_field(name="🔗 Invite Code", value=f"`discord.gg/{guild.vanity_url_code}`", inline=True)
                    embed.set_thumbnail(url=member.display_avatar.url)
                    embed.set_footer(text=f"TrapAI Invite Tracker • {guild.name}")
                    try:
                        await inv_log_ch.send(embed=embed)
                    except discord.HTTPException:
                        pass


@bot.event
async def on_member_unban(guild, user):
    """Anti-tamper for hard-bans: if a hard-banned user gets unbanned
    manually through Discord's native UI (not via ,unhardban — which
    already removes them from HARD_BANNED *before* calling guild.unban,
    so this correctly no-ops for that path), instantly re-ban them. A
    hard-ban can only actually be lifted through ,unhardban."""
    hb_guild = HARD_BANNED.get(guild.id, {})
    reason = hb_guild.get(user.id)
    if reason is None:
        return
    try:
        await guild.ban(user, reason=f"Hard-ban re-applied — manual unban reverted (was: {reason})", delete_message_days=0)
    except (discord.Forbidden, discord.HTTPException):
        return
    await log(guild, "bans", "🔴 Hard-Ban Re-Applied — Manual Unban Reverted", None, discord.Color.dark_red(),
              fields=[
                  ("🔴 User",            f"{user} (`{user.id}`)", True),
                  ("📝 Original Reason", reason,                    False),
                  ("ℹ️ Note",            "Someone tried to manually unban a hard-banned user through Discord — reverted. Use `,unhardban` to actually lift a hard-ban.", False),
              ],
              target=user)


@bot.event
async def on_invite_create(invite):
    """Keep the invite cache up to date when a new invite is created."""
    guild = invite.guild
    INVITE_CACHE.setdefault(guild.id, {})[invite.code] = invite.uses


@bot.event
async def on_invite_delete(invite):
    """Remove a deleted invite from the cache."""
    guild = invite.guild
    INVITE_CACHE.get(guild.id, {}).pop(invite.code, None)


@bot.event
async def on_member_remove(member):
    await _delete_booster_role(member.guild, member.id, reason="Member left the server")

    roles = [r.mention for r in member.roles if r.name != "@everyone"]
    await log(
        member.guild,
        "leaves",
        "Member Left",
        f"{member.mention} left **{member.guild.name}**.",
        discord.Color.red(),
        fields=[
            ("👤 User",    f"{member.mention} (`{member.id}`)", False),
            ("🏷️ Username", str(member),                        True),
            ("📅 Joined",  discord.utils.format_dt(member.joined_at, "F") if member.joined_at else "Unknown", False),
            ("🏷️ Roles",   ", ".join(roles)[:1000] or "None",  False),
        ],
        target=member
    )
    await _check_milestone(member.guild)

    # Exit survey — best-effort DM asking why they left. Silently no-ops if
    # their DMs are closed, they've blocked the bot, or Discord otherwise
    # won't let a DM channel open at this point.
    if not member.bot:
        try:
            embed = discord.Embed(
                title=f"👋 Sorry to see you go — {member.guild.name}",
                description=(
                    "Mind telling us why you left? It really helps the staff team improve.\n\n"
                    "This is completely optional."
                ),
                color=discord.Color.orange(),
                timestamp=discord.utils.utcnow()
            )
            if member.guild.icon:
                embed.set_thumbnail(url=member.guild.icon.url)
            await member.send(embed=embed, view=ExitSurveyView(member.guild.id, member.id, str(member)))
        except (discord.Forbidden, discord.HTTPException):
            pass


@bot.event
async def on_member_update(before, after):
    # Boost detection
    if before.premium_since is None and after.premium_since is not None:
        await log(
            after.guild,
            "boost",
            "Server Boosted 🚀",
            f"{after.mention} just boosted **{after.guild.name}**!",
            discord.Color.purple(),
            fields=[
                ("🚀 Booster",      f"{after.mention} (`{after.id}`)",            True),
                ("🔢 Total Boosts", str(after.guild.premium_subscription_count),  True),
                ("🏆 Boost Tier",   f"Tier {after.guild.premium_tier}",           True),
            ],
            target=after
        )

        boost_channel = _resolve_boost_channel(after.guild)
        if boost_channel:
            thank_you_embed = discord.Embed(
                title="🚀 Thank You for the Boost!",
                description=(
                    f"{after.mention} just boosted **{after.guild.name}**! "
                    "Thank you so much for supporting the server! 💜"
                ),
                color=discord.Color.purple(),
                timestamp=discord.utils.utcnow()
            )
            thank_you_embed.add_field(
                name="🔢 Total Boosts", value=str(after.guild.premium_subscription_count), inline=True
            )
            thank_you_embed.add_field(
                name="🏆 Boost Tier", value=f"Tier {after.guild.premium_tier}", inline=True
            )
            thank_you_embed.set_thumbnail(url=after.display_avatar.url)
            thank_you_embed.set_footer(text=after.guild.name, icon_url=after.guild.icon.url if after.guild.icon else None)
            try:
                await boost_channel.send(content=after.mention, embed=thank_you_embed)
            except (discord.Forbidden, discord.HTTPException):
                pass

    # Un-boost detection — clean up their custom booster role, if any
    elif before.premium_since is not None and after.premium_since is None:
        await _delete_booster_role(after.guild, after.id, reason="Member stopped boosting")
        await log(
            after.guild,
            "boost",
            "Server Boost Ended",
            f"{after.mention} is no longer boosting **{after.guild.name}**.",
            discord.Color.dark_grey(),
            fields=[("👤 Member", f"{after.mention} (`{after.id}`)", True)],
            target=after
        )

    if before.roles != after.roles:
        removed_roles = [role for role in before.roles if role not in after.roles]
        added_roles   = [role for role in after.roles  if role not in before.roles]

        # ── Protected roles: auto-strip if granted manually ──────────
        protected = PROTECTED_ROLES.get(after.guild.id, set())
        for role in added_roles:
            if role.id not in protected:
                continue
            # Check if this role was granted through the approved vouch-role flow
            # We mark approved grants by temporarily whitelisting the role_id in a set
            approved_set = _VOUCH_ROLE_APPROVED.get(after.guild.id, set())
            token_key = (after.id, role.id)
            if token_key in approved_set:
                approved_set.discard(token_key)
                continue  # Legitimate — skip strip
            # Not approved — strip it immediately
            try:
                await after.remove_roles(role, reason="🔒 Protected role — must be granted via ,vouch")
            except (discord.Forbidden, discord.HTTPException):
                pass
            await log(
                after.guild, "mod",
                "Protected Role Auto-Stripped",
                f"{after.mention} was manually given the protected role {role.mention} and it was auto-removed.",
                discord.Color.dark_red(),
                fields=[
                    ("👤 Member",       f"{after.mention} (`{after.id}`)", True),
                    ("🔒 Role Stripped", f"{role.mention} (`{role.id}`)",   True),
                    ("ℹ️ Reason",        "Manual grant blocked — use `,vouch @user (role) reason`", False),
                ],
                target=after
            )
            # DM the member
            try:
                dm = discord.Embed(
                    title="🔒 Role Blocked",
                    description=(
                        f"You were given **{role.name}** in **{after.guild.name}** manually, "
                        "but that role is **protected** and can only be granted through an approved vouch request.\n\n"
                        "It has been automatically removed."
                    ),
                    color=discord.Color.dark_red(),
                    timestamp=discord.utils.utcnow()
                )
                dm.set_footer(text=f"TrapAI • {after.guild.name}")
                await after.send(embed=dm)
            except (discord.Forbidden, discord.HTTPException):
                pass
            continue  # Skip normal role-add log for this role

        if added_roles or removed_roles:
            mod, mod_reason = await _find_recent_mod(after.guild, discord.AuditLogAction.member_role_update, member=after)

        for role in added_roles:
            if role.id in protected:
                continue  # Already handled above
            await log(
                after.guild,
                "roles",
                "Role Added",
                None,
                discord.Color.green(),
                fields=[
                    ("👤 Member",    f"{after.mention} (`{after.id}`)",     True),
                    ("🏷️ Role",      f"{role.mention} (`{role.id}`)",       True),
                    ("👑 Added By",  mod.mention if mod else "*Unknown*", True),
                    ("📌 Position",  str(role.position),                   True),
                    ("📝 Reason",    mod_reason or "*No reason provided*", False),
                ],
                actor=mod, target=after
            )

        if removed_roles:
            # "Screenshot" every role they held right before losing any of
            # them — persisted so ,restoreallroles can bring back their
            # WHOLE prior role set (not just whatever this one action
            # removed), and shown in the log so staff can see their full
            # role picture at the moment it happened, not just the one
            # role that's gone.
            snapshot_ids = [r.id for r in before.roles if r.name != "@everyone"]
            ROLE_SNAPSHOTS.setdefault(after.guild.id, {})[after.id] = snapshot_ids
            _save_role_snapshots()
            snapshot_str = ", ".join(r.name for r in before.roles if r.name != "@everyone") or "*None*"

        for role in removed_roles:
            await log(
                after.guild,
                "roles",
                "Role Removed",
                None,
                discord.Color.red(),
                fields=[
                    ("👤 Member",     f"{after.mention} (`{after.id}`)",     True),
                    ("🏷️ Role",       f"{role.mention} (`{role.id}`)",       True),
                    ("👑 Removed By", mod.mention if mod else "*Unknown*", True),
                    ("📌 Position",   str(role.position),                   True),
                    ("📝 Reason",     mod_reason or "*No reason provided*", False),
                    ("📸 Role Snapshot (before)", snapshot_str[:1024], False),
                ],
                actor=mod, target=after
            )

    if before.nick != after.nick:
        nick_mod, nick_reason = await _find_recent_mod(
            after.guild, discord.AuditLogAction.member_update, member=after,
            attr="nick", expected=after.nick
        )
        await log(
            after.guild,
            "nicknames",
            "Nickname Changed",
            None,
            discord.Color.blurple(),
            fields=[
                ("👤 Member",    f"{after.mention} (`{after.id}`)",         True),
                ("👑 Changed By", nick_mod.mention if nick_mod else "*Unknown*", True),
                ("📝 Old Nick",  before.nick or before.name,                  True),
                ("📝 New Nick",  after.nick  or after.name,                   True),
                ("📝 Reason",    nick_reason or "*No reason provided*",       False),
            ],
            actor=nick_mod, target=after
        )

    # ── Timeouts applied/removed outside ,timeout ────────────────────
    # ,timeout already logs itself when the bot performs the action. This
    # only catches everything else that changes timed_out_until: the
    # native Discord "Time Out"/"Remove Timeout" UI, another bot or
    # integration — none of which ever showed up in #timeout-logs before,
    # same gap ,ban's native-ban logging had until on_member_ban closed it.
    if before.timed_out_until != after.timed_out_until:
        now = discord.utils.utcnow()
        was_active = bool(before.timed_out_until and before.timed_out_until > now)
        is_active = bool(after.timed_out_until and after.timed_out_until > now)

        if is_active and not was_active:
            to_mod, to_reason = await _find_recent_mod(
                after.guild, discord.AuditLogAction.member_update, member=after,
                attr="timed_out_until", expected=after.timed_out_until
            )
            if not (to_mod and to_mod.bot):
                await log(after.guild, "timeouts", "Member Timed Out (Discord)", None, discord.Color.purple(),
                          fields=[
                              ("🛡 Moderator", f"{to_mod.mention} (`{to_mod.id}`)" if to_mod else "*Unknown*", True),
                              ("⏳ User",      f"{after.mention} (`{after.id}`)",                              True),
                              ("🗓️ Expires",   discord.utils.format_dt(after.timed_out_until, "F"),            False),
                              ("📝 Reason",    to_reason or "*No reason provided*",                             False),
                          ],
                          actor=to_mod, target=after)
        elif was_active and not is_active:
            to_mod, to_reason = await _find_recent_mod(
                after.guild, discord.AuditLogAction.member_update, member=after,
                attr="timed_out_until", expected=after.timed_out_until
            )
            # No resolvable actor almost always means it just expired
            # naturally rather than someone removing it — not worth logging.
            if to_mod and not to_mod.bot:
                await log(after.guild, "timeouts", "Timeout Removed", None, discord.Color.green(),
                          fields=[
                              ("🛡 Moderator", f"{to_mod.mention} (`{to_mod.id}`)", True),
                              ("⏳ User",      f"{after.mention} (`{after.id}`)",    True),
                              ("📝 Reason",    to_reason or "*No reason provided*", False),
                          ],
                          actor=to_mod, target=after)


@bot.event
async def on_guild_role_create(role):
    mod, reason = await _find_recent_mod(role.guild, discord.AuditLogAction.role_create)
    await log(
        role.guild,
        "role_create",
        "Role Created",
        None,
        discord.Color.green(),
        fields=[
            ("✨ Role",        f"{role.mention} (`{role.id}`)",       True),
            ("👑 Created By",  mod.mention if mod else "*Unknown*", True),
            ("🎨 Color",       str(role.color),                       True),
            ("📌 Hoisted",     str(role.hoist),                       True),
            ("💬 Mentionable", str(role.mentionable),                 True),
        ],
        actor=mod
    )


@bot.event
async def on_guild_role_delete(role):
    mod, reason = await _find_recent_mod(role.guild, discord.AuditLogAction.role_delete)
    await log(
        role.guild,
        "role_delete",
        "Role Deleted",
        None,
        discord.Color.red(),
        fields=[
            ("🗑️ Role Name",  f"`{role.name}`",                     True),
            ("🆔 Role ID",     str(role.id),                          True),
            ("👑 Deleted By",  mod.mention if mod else "*Unknown*", True),
            ("🎨 Color",       str(role.color),                       True),
            ("👥 Had Members", str(len(role.members)),                True),
        ],
        actor=mod
    )

    # ── Anti-nuke: track rapid role deletions ─────────────────
    # Deliberately NOT exempting Administrator — see the matching note on
    # the mass-ban tracker below. Trusted staff who need to bulk-delete
    # roles legitimately should be added via ,wl instead.
    guild = role.guild
    try:
        entry = None
        async for e in guild.audit_logs(limit=1, action=discord.AuditLogAction.role_delete):
            entry = e
            break
        if not entry or entry.user.bot:
            return
        actor = entry.user
        if actor.id in ANTINUKE_WHITELIST.get(guild.id, set()):
            return
        now = time.time()
        tracker = NUKE_TRACKER.setdefault(guild.id, {}).setdefault(actor.id, [])
        tracker.append(now)
        NUKE_TRACKER[guild.id][actor.id] = [t for t in tracker if now - t <= NUKE_WINDOW]
        if len(NUKE_TRACKER[guild.id][actor.id]) >= NUKE_ROLE_LIMIT:
            NUKE_TRACKER[guild.id][actor.id].clear()
            roles_to_remove = await _antinuke_punish(guild, actor, "🚨 Anti-nuke: rapid role deletion detected")
            await log(guild, "mod", "🚨 Anti-Nuke Triggered — Role Deletions", None,
                      discord.Color.dark_red(),
                      fields=[
                          ("⚠️ Action",       "Rapid Role Deletes Detected",                          True),
                          ("👤 Suspect",       f"{actor.mention} (`{actor.id}`)",                      True),
                          ("🔢 Deletes",       f"{NUKE_ROLE_LIMIT}+ roles deleted in {NUKE_WINDOW}s", True),
                          ("⚔️ Roles Stripped", ", ".join(r.name for r in roles_to_remove)[:512] or "None", False),
                          ("🔴 Hard-Banned",   "✅ Yes — will be instantly re-banned if they rejoin", False),
                      ],
                      actor=actor)
    except (discord.Forbidden, discord.HTTPException):
        pass


@bot.event
async def on_guild_channel_create(channel):
    cat = channel.category.name if channel.category else "No category"
    await log(
        channel.guild,
        "channel_create",
        "Channel Created",
        None,
        discord.Color.green(),
        fields=[
            ("📁 Channel",  f"{channel.mention if hasattr(channel,'mention') else channel.name} (`{channel.id}`)", True),
            ("🔧 Type",     str(channel.type),  True),
            ("📂 Category", cat,                True),
        ]
    )


@bot.event
async def on_guild_channel_delete(channel):
    cat = channel.category.name if channel.category else "No category"
    await log(
        channel.guild,
        "channel_delete",
        "Channel Deleted",
        None,
        discord.Color.red(),
        fields=[
            ("🗑️ Channel Name", f"`{channel.name}` (`{channel.id}`)", True),
            ("🔧 Type",          str(channel.type),                    True),
            ("📂 Category",      cat,                                   True),
        ]
    )

    # ── Anti-nuke: track rapid channel deletions ───────────────
    # Deliberately NOT exempting Administrator — see the matching note on
    # the mass-ban tracker below. Trusted staff who need to bulk-delete
    # channels legitimately should be added via ,wl instead.
    guild = channel.guild
    try:
        entry = None
        async for e in guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_delete):
            entry = e
            break
        if not entry or entry.user.bot:
            return
        actor = entry.user
        if actor.id in ANTINUKE_WHITELIST.get(guild.id, set()):
            return
        now = time.time()
        tracker = NUKE_TRACKER.setdefault(guild.id, {}).setdefault(actor.id + 1_000_000_000, [])
        tracker.append(now)
        NUKE_TRACKER[guild.id][actor.id + 1_000_000_000] = [t for t in tracker if now - t <= NUKE_WINDOW]
        if len(NUKE_TRACKER[guild.id][actor.id + 1_000_000_000]) >= NUKE_CHAN_LIMIT:
            NUKE_TRACKER[guild.id][actor.id + 1_000_000_000].clear()
            roles_to_remove = await _antinuke_punish(guild, actor, "🚨 Anti-nuke: rapid channel deletion detected")
            await log(guild, "mod", "🚨 Anti-Nuke Triggered — Channel Deletions", None,
                      discord.Color.dark_red(),
                      fields=[
                          ("⚠️ Action",       "Rapid Channel Deletes Detected",                          True),
                          ("👤 Suspect",       f"{actor.mention} (`{actor.id}`)",                         True),
                          ("🔢 Deletes",       f"{NUKE_CHAN_LIMIT}+ channels deleted in {NUKE_WINDOW}s",  True),
                          ("⚔️ Roles Stripped", ", ".join(r.name for r in roles_to_remove)[:512] or "None", False),
                          ("🔴 Hard-Banned",   "✅ Yes — will be instantly re-banned if they rejoin", False),
                      ],
                      actor=actor)
    except (discord.Forbidden, discord.HTTPException):
        pass


@bot.event
async def on_member_ban(guild, user):
    # This fires for EVERY ban regardless of how it happened — Discord's
    # native ban button, another bot/integration, or ,ban / ,hardban here.
    # Only one audit-log fetch, shared by both jobs below.
    entry = None
    try:
        async for e in guild.audit_logs(limit=1, action=discord.AuditLogAction.ban):
            entry = e
            break
    except (discord.Forbidden, discord.HTTPException):
        pass
    actor = entry.user if entry else None

    # ── General ban logging ─────────────────────────────────────
    # ,ban and ,hardban already log themselves (with DM-sent status, etc.)
    # at the moment they act — that shows up here as actor.bot == True,
    # so skip it to avoid a duplicate entry. Everything else (the native
    # Discord ban button, another bot) previously never reached #bans at
    # all — this is what actually fixes that gap.
    if not (actor and actor.bot):
        await log(guild, "bans", "Member Banned (Discord)", None, discord.Color.red(),
                  fields=[
                      ("🛡 Moderator", f"{actor.mention} (`{actor.id}`)" if actor else "Unknown", True),
                      ("🔨 User",      f"{user.mention} (`{user.id}`)", True),
                      ("📝 Reason",    (entry.reason if entry else None) or "No reason provided", False),
                  ],
                  actor=actor, target=user)

    # ── Anti-nuke: track rapid member bans — if it's not caught here, mass
    # role/channel deletion isn't the only way to gut a server; someone
    # with ban_members but not full trust can do just as much damage by
    # banning the member list. Uses a separate NUKE_TRACKER key
    # (actor.id + 2_000_000_000) so it doesn't collide with the role/channel
    # delete counters, which already use +0 and +1_000_000_000.
    #
    # Deliberately NOT exempting Administrator here, unlike the role/channel
    # delete trackers above — a mass-ban is the fastest way to gut a server's
    # member list, and "has Administrator" is exactly the profile of a
    # compromised staff account or a malicious admin. Trusted staff who
    # legitimately need to ban several people fast should be added to the
    # anti-nuke whitelist via ,wl (opt-in, per-user) instead of getting a
    # blanket permission-based pass.
    try:
        if not actor or actor.bot:
            return
        if actor.id in ANTINUKE_WHITELIST.get(guild.id, set()):
            return

        now = time.time()
        key = actor.id + 2_000_000_000
        tracker = NUKE_TRACKER.setdefault(guild.id, {}).setdefault(key, [])
        tracker.append(now)
        NUKE_TRACKER[guild.id][key] = [t for t in tracker if now - t <= NUKE_WINDOW]
        if len(NUKE_TRACKER[guild.id][key]) >= NUKE_BAN_LIMIT:
            NUKE_TRACKER[guild.id][key].clear()

            roles_to_remove = await _antinuke_punish(guild, actor, "🚨 Anti-nuke: rapid mass-ban detected")

            await log(guild, "mod", "🚨 Anti-Nuke Triggered — Mass Ban Detected", None,
                      discord.Color.dark_red(),
                      fields=[
                          ("⚠️ Action",        "Rapid Member Bans Detected",                          True),
                          ("👤 Suspect",        f"{actor.mention} (`{actor.id}`)",                     True),
                          ("🔢 Bans",           f"{NUKE_BAN_LIMIT}+ members banned in {NUKE_WINDOW}s", True),
                          ("⚔️ Roles Stripped", ", ".join(r.name for r in roles_to_remove)[:512] or "None", False),
                          ("🔴 Hard-Banned",    "✅ Yes — will be instantly re-banned if they rejoin", False),
                      ],
                      actor=actor)
    except (discord.Forbidden, discord.HTTPException):
        pass


@bot.event
async def on_guild_channel_update(before, after):
    changes = []
    if before.name != after.name:
        changes.append(("✏️ Name",    f"`{before.name}` → `{after.name}`",    False))
    if isinstance(before, discord.TextChannel) and isinstance(after, discord.TextChannel):
        if before.topic != after.topic:
            changes.append(("📝 Topic",   f"`{before.topic or 'None'}` → `{after.topic or 'None'}`", False))
        if before.slowmode_delay != after.slowmode_delay:
            changes.append(("🐌 Slowmode", f"`{before.slowmode_delay}s` → `{after.slowmode_delay}s`", True))
        if before.nsfw != after.nsfw:
            changes.append(("🔞 NSFW",    f"`{before.nsfw}` → `{after.nsfw}`", True))

    if changes:
        ch_ref = after.mention if hasattr(after, "mention") else after.name
        await log(
            after.guild,
            "channel_update",
            "Channel Updated",
            f"Changes made to {ch_ref}",
            discord.Color.purple(),
            fields=changes
        )


@bot.event
async def on_guild_emojis_update(guild, before, after):
    added   = [e for e in after if e not in before]
    removed = [e for e in before if e not in after]

    for emoji in added:
        await log(guild, "emoji", "Emoji Added", None, discord.Color.green(),
                  fields=[("😀 Emoji", f"{emoji} `:{emoji.name}:`", True), ("🆔 ID", str(emoji.id), True)])

    for emoji in removed:
        await log(guild, "emoji", "Emoji Removed", None, discord.Color.red(),
                  fields=[("🗑️ Name", f"`:{emoji.name}:`", True), ("🆔 ID", str(emoji.id), True)])


@bot.event
async def on_guild_stickers_update(guild, before, after):
    added   = [s for s in after if s not in before]
    removed = [s for s in before if s not in after]

    for sticker in added:
        await log(guild, "stickers", "Sticker Added", None, discord.Color.green(),
                  fields=[("🖼️ Sticker", f"`{sticker.name}`", True), ("🆔 ID", str(sticker.id), True)])

    for sticker in removed:
        await log(guild, "stickers", "Sticker Removed", None, discord.Color.red(),
                  fields=[("🗑️ Name", f"`{sticker.name}`", True), ("🆔 ID", str(sticker.id), True)])


# Suppresses snipe caching for a channel right after our own ,clear command
# bulk-purges it — a deliberate staff wipe shouldn't be instantly re-snipeable
# (see _CLEAR_SUPPRESS_WINDOW usage in ,clear below). A manual multi-select
# delete through the Discord client itself is NOT suppressed — that still
# goes through on_bulk_message_delete and gets cached normally.
_CLEAR_SUPPRESS: dict = {}
_CLEAR_SUPPRESS_WINDOW = 5  # seconds


async def _cache_deleted_message(message: discord.Message):
    if not message.guild or message.author.bot:
        return

    images = []
    for a in message.attachments:
        if a.content_type and a.content_type.startswith("image/") and a.size <= 8 * 1024 * 1024:
            try:
                images.append({"filename": a.filename, "data": await a.read()})
            except (discord.HTTPException, discord.Forbidden):
                pass

    SNIPE_CACHE.setdefault(message.channel.id, deque(maxlen=SNIPE_CACHE_SIZE)).appendleft({
        "author_id":   message.author.id,
        "author_name": str(message.author),
        "author_avatar": message.author.display_avatar.url,
        "content":     message.content or "",
        "attachments": [a.url for a in message.attachments],
        "images":      images,
        "deleted_at":  discord.utils.utcnow().timestamp(),
    })


@bot.event
async def on_message_delete(message):
    if not message.guild or message.author.bot:
        return

    await _cache_deleted_message(message)

    content = message.content or "*(no text content)*"
    attachments = ", ".join(a.filename for a in message.attachments) if message.attachments else "None"
    await log(
        message.guild,
        "messages",
        "Message Deleted",
        None,
        discord.Color.red(),
        fields=[
            ("👤 Author",      f"{message.author.mention} (`{message.author.id}`)", True),
            ("📍 Channel",     message.channel.mention,                              True),
            ("🕐 Sent At",     discord.utils.format_dt(message.created_at, "F"),    False),
            ("💬 Content",     content[:1024],                                       False),
            ("📎 Attachments", attachments,                                          False),
        ],
        target=message.author
    )


@bot.event
async def on_bulk_message_delete(messages):
    """Fires for any bulk delete — a manual multi-select delete through the
    Discord client, OR our own ,clear command. We want the former snipeable
    and the latter not, so ,clear marks its channel just before purging and
    we skip caching if that mark is still fresh."""
    if not messages:
        return
    channel = messages[0].channel
    guild = messages[0].guild
    if not guild:
        return

    suppressed_at = _CLEAR_SUPPRESS.get(channel.id)
    if suppressed_at and discord.utils.utcnow().timestamp() - suppressed_at <= _CLEAR_SUPPRESS_WINDOW:
        return

    for message in sorted(messages, key=lambda m: m.created_at):
        await _cache_deleted_message(message)


@bot.event
async def on_message_edit(before, after):
    if before.author.bot or before.content == after.content or not before.guild:
        return

    # Re-run automod on the edited content — otherwise posting something clean
    # and editing in a filtered link/word afterward would never get caught.
    is_staff = (
        isinstance(after.author, discord.Member)
        and (
            after.author.guild_permissions.manage_messages
            or after.author.guild_permissions.administrator
        )
    )
    if not is_staff and await _run_automod(after):
        return

    EDIT_SNIPE_CACHE.setdefault(before.channel.id, deque(maxlen=SNIPE_CACHE_SIZE)).appendleft({
        "author_id":   before.author.id,
        "author_name": str(before.author),
        "author_avatar": before.author.display_avatar.url,
        "before":      before.content or "",
        "after":       after.content or "",
        "edited_at":   discord.utils.utcnow().timestamp(),
        "jump_url":    after.jump_url,
    })

    await log(
        before.guild,
        "messages",
        "Message Edited",
        None,
        discord.Color.purple(),
        fields=[
            ("👤 Author",     f"{before.author.mention} (`{before.author.id}`)",   True),
            ("📍 Channel",    before.channel.mention,                               True),
            ("🔗 Jump Link",  f"[Click to view]({after.jump_url})",                 False),
            ("📝 Before",     before.content[:1000] or "*(empty)*",                 False),
            ("✏️ After",      after.content[:1000]  or "*(empty)*",                 False),
        ],
        target=before.author
    )


def _snipe_delete_embed(ctx, cache, index: int):
    entry = cache[index]
    embed = discord.Embed(
        description=entry["content"][:4000] or "*(no text content)*",
        color=discord.Color.orange(),
        timestamp=datetime.fromtimestamp(entry["deleted_at"])
    )
    embed.set_author(name=entry["author_name"], icon_url=entry["author_avatar"])

    files = []
    images = entry.get("images") or []
    if images:
        # Re-upload the bytes we grabbed at delete-time — Discord kills the
        # original attachment URL almost immediately once the message is gone.
        for img in images[:4]:
            files.append(discord.File(io.BytesIO(img["data"]), filename=img["filename"]))
        embed.set_image(url=f"attachment://{images[0]['filename']}")
        if len(images) > 1:
            embed.add_field(name="📎 Extra Images", value=f"+{len(images) - 1} more attached below", inline=False)
    elif entry["attachments"]:
        embed.add_field(
            name="📎 Attachment (link may be dead)",
            value=entry["attachments"][0][:1024],
            inline=False
        )

    embed.set_footer(text=f"{index + 1}/{len(cache)} • #{ctx.channel.name}")
    return embed, files


def _snipe_edit_embed(ctx, cache, index: int):
    entry = cache[index]
    embed = discord.Embed(color=discord.Color.purple(), timestamp=datetime.fromtimestamp(entry["edited_at"]))
    embed.set_author(name=entry["author_name"], icon_url=entry["author_avatar"])
    embed.add_field(name="Before", value=entry["before"][:1000] or "*(empty)*", inline=False)
    embed.add_field(name="After",  value=entry["after"][:1000]  or "*(empty)*", inline=False)
    embed.set_footer(text=f"{index + 1}/{len(cache)} • #{ctx.channel.name}")
    return embed, []


class SnipeView(discord.ui.View):
    """Bleed-style snipe navigation — step back through a channel's cached
    deletes/edits in place with ◀ ▶ instead of retyping the command."""
    def __init__(self, ctx, cache, builder, start: int = 0):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.cache = cache
        self.builder = builder
        self.index = start
        self.message = None
        self._sync_buttons()

    def _sync_buttons(self):
        self.prev_btn.disabled = self.index >= len(self.cache) - 1
        self.next_btn.disabled = self.index <= 0

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("❌ Only the person who ran this command can navigate.", ephemeral=True)
            return False
        return True

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index += 1
        self._sync_buttons()
        embed, files = self.builder(self.ctx, self.cache, self.index)
        await interaction.response.edit_message(embed=embed, attachments=files, view=self)

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index -= 1
        self._sync_buttons()
        embed, files = self.builder(self.ctx, self.cache, self.index)
        await interaction.response.edit_message(embed=embed, attachments=files, view=self)

    @discord.ui.button(emoji="🗑️", style=discord.ButtonStyle.danger)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.message.delete()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


@bot.command(aliases=["s"])
async def snipe(ctx, index: int = 1):
    """
    Show a recently deleted message in this channel — including any image
    that was attached, re-uploaded from a copy grabbed the instant it was
    deleted — with ◀ ▶ buttons to step through history in place. n=1
    (default) is the most recent, up to the last 20 per channel. Cleared
    on restart.
    Usage: ,snipe [n]
    """
    cache = SNIPE_CACHE.get(ctx.channel.id)
    if not cache or index < 1 or index > len(cache):
        await ctx.send("❌ Nothing to snipe here." if not cache else f"❌ Only {len(cache)} deleted message(s) cached here.", delete_after=8)
        return

    view = SnipeView(ctx, cache, _snipe_delete_embed, start=index - 1)
    embed, files = _snipe_delete_embed(ctx, cache, index - 1)
    view.message = await ctx.send(embed=embed, files=files, view=view)


@bot.command(aliases=["es"])
async def editsnipe(ctx, index: int = 1):
    """
    Show a recently edited message in this channel, with ◀ ▶ buttons to
    step through history in place. n=1 (default) is the most recent, up
    to the last 20 per channel. Cleared on restart.
    Usage: ,editsnipe [n]
    """
    cache = EDIT_SNIPE_CACHE.get(ctx.channel.id)
    if not cache or index < 1 or index > len(cache):
        await ctx.send("❌ Nothing to editsnipe here." if not cache else f"❌ Only {len(cache)} edit(s) cached here.", delete_after=8)
        return

    view = SnipeView(ctx, cache, _snipe_edit_embed, start=index - 1)
    embed, files = _snipe_edit_embed(ctx, cache, index - 1)
    view.message = await ctx.send(embed=embed, files=files, view=view)


@bot.command()
@_permitted_check(manage_messages=True)
async def clearsnipe(ctx):
    """Clear the snipe/editsnipe cache for this channel. Usage: ,clearsnipe"""
    had_snipe = SNIPE_CACHE.pop(ctx.channel.id, None) is not None
    had_edit  = EDIT_SNIPE_CACHE.pop(ctx.channel.id, None) is not None
    if not had_snipe and not had_edit:
        await ctx.send("❌ Nothing cached here to clear.", delete_after=8)
        return
    await ctx.send("🧹 Snipe cache cleared for this channel.", delete_after=6)


async def _find_recent_mod(guild, action, *, member=None, channel=None, attr=None, expected=None, window=6):
    """
    Best-effort audit-log lookup for who performed a recent action
    (voice disconnect/move/mute/deafen, role add/remove/create/delete,
    nickname change, ...) affecting `member`. Discord's audit log doesn't
    always link entries to a specific member, so this matches on recency
    (+ channel/attribute where possible) and returns (None, None) when
    nothing plausible is found within `window` seconds — meaning the
    member most likely triggered it themselves, or the audit log entry
    just isn't resolvable.
    """
    me = guild.me
    if not me or not me.guild_permissions.view_audit_log:
        return None, None
    cutoff = discord.utils.utcnow() - timedelta(seconds=window)
    try:
        async for entry in guild.audit_logs(action=action, limit=8):
            if entry.created_at < cutoff:
                break
            if member is not None and entry.target is not None:
                if getattr(entry.target, "id", None) != member.id:
                    continue
            if channel is not None:
                entry_channel = getattr(entry.extra, "channel", None)
                if entry_channel is not None and entry_channel.id != channel.id:
                    continue
            if attr is not None:
                if getattr(entry.after, attr, None) != expected:
                    continue
            return entry.user, entry.reason
    except (discord.Forbidden, discord.HTTPException):
        pass
    return None, None


def _vc_occupancy(channel: discord.VoiceChannel) -> str:
    members = [m for m in channel.members if not m.bot]
    if not members:
        return "*Empty*"
    names = ", ".join(m.mention for m in members[:8])
    if len(members) > 8:
        names += f" *+{len(members) - 8} more*"
    return f"**{len(members)}** in channel — {names}"


@bot.event
async def on_voice_state_update(member, before, after):
    now = time.time()
    guild = member.guild

    # ── Music: leave automatically once the VC is empty of real members ──
    if before.channel and before.channel != after.channel:
        vc = guild.voice_client
        if vc and vc.channel.id == before.channel.id and not any(not m.bot for m in before.channel.members):
            _cancel_idle_disconnect(guild)
            MUSIC_QUEUES[guild.id] = []
            MUSIC_NOW_PLAYING.pop(guild.id, None)
            try:
                await vc.disconnect()
            except discord.HTTPException:
                pass

    # ── Active enforcement for ,vcban / ,vckick / ,vclock ─────────────
    # A channel overwrite of connect=False can be bypassed by anyone with
    # guild-level Administrator, a staff role with Move Members/Manage
    # Channels, or the server owner (who bypasses every permission check
    # Discord has, always) — so the overwrite alone isn't a real ban or
    # lock for any of them. Actively watch for someone landing in the
    # channel anyway and immediately disconnect them again — no
    # exceptions, regardless of who they are.
    if after.channel:
        vc_id = after.channel.id
        if member.id in vc_banned.get(vc_id, set()) or member.id in vc_kicked.get(vc_id, set()):
            try:
                await member.move_to(None, reason="Still banned/kicked from this VC")
            except (discord.Forbidden, discord.HTTPException):
                pass
            return
        if vc_id in vc_locked:
            is_owner = member.id == temp_vc_owners.get(vc_id)
            is_mod = member.id in vc_mods.get(vc_id, set())
            is_permitted = after.channel.overwrites_for(member).connect is True
            if not (is_owner or is_mod or is_permitted):
                try:
                    await member.move_to(None, reason="This VC is locked")
                except (discord.Forbidden, discord.HTTPException):
                    pass
                return

    # ── Self-service unmute: joining a registered unmute VC instantly
    # clears their VC server mute/deafen AND the ,mute role-based mute (two
    # completely separate Discord mechanisms — this used to only clear the
    # native voice mute/deafen despite the feature claiming to lift the
    # Muted role too), no staff needed, then bounces them back out so the
    # channel/slot is free for the next person.
    if after.channel and after.channel.id in UNMUTE_VC_CHANNELS.get(guild.id, []):
        muted_role = discord.utils.get(guild.roles, name=MUTED_ROLE)
        has_muted_role = muted_role is not None and muted_role in member.roles
        cleared = []
        if after.mute or after.deaf:
            try:
                await member.edit(mute=False, deafen=False, reason="Self-unmute via unmute VC")
                cleared.append("VC server mute/deafen")
            except (discord.Forbidden, discord.HTTPException):
                pass
        if has_muted_role:
            try:
                await member.remove_roles(muted_role, reason="Self-unmute via unmute VC")
                cleared.append("Muted role")
            except (discord.Forbidden, discord.HTTPException):
                pass
        if cleared:
            _log_mod_action(guild.id, member.id, "unmute", "Self-service (unmute VC)",
                             f"Joined {after.channel.name} — cleared: {', '.join(cleared)}")
            await log(guild, "vc", "Member Self-Unmuted/Undeafened", None, discord.Color.green(),
                      fields=[
                          ("🔊 User",    f"{member.mention} (`{member.id}`)", True),
                          ("🎤 Via",     after.channel.mention,               True),
                          ("🧹 Cleared", ", ".join(cleared),                   False),
                      ], target=member)
            try:
                await member.move_to(None, reason="Unmute VC — bounced back out after self-unmute")
            except (discord.Forbidden, discord.HTTPException):
                pass

    # ── Joined a channel ──────────────────────────────────────
    if before.channel is None and after.channel is not None:
        if not member.bot:
            vc_join_time[(guild.id, member.id)] = now
        await log(guild, "vc", "VC Join",
                  f"**{member.display_name}** joined {after.channel.mention}.",
                  discord.Color.green(),
                  fields=[
                      ("👤 Member",     f"{member.mention} (`{member.id}`)", True),
                      ("🎤 Channel",    after.channel.mention,               True),
                      ("👥 In Channel", _vc_occupancy(after.channel),        False),
                  ], target=member)

    # ── Left a channel entirely (self-leave vs. staff disconnect) ──
    elif before.channel is not None and after.channel is None:
        joined = vc_join_time.pop((guild.id, member.id), None)
        duration = _format_vc_duration(now - joined) if joined else "—"
        if joined:
            guild_vc_stats = VC_STATS.setdefault(guild.id, {})
            guild_vc_stats[member.id] = guild_vc_stats.get(member.id, 0) + int(now - joined)

        mod, reason = await _find_recent_mod(guild, discord.AuditLogAction.member_disconnect, member=member)
        if mod:
            await log(guild, "vc", "VC Disconnected (by Staff)",
                      f"**{member.display_name}** was disconnected from {before.channel.mention} by {mod.mention}.",
                      discord.Color.dark_red(),
                      fields=[
                          ("👤 Member",          f"{member.mention} (`{member.id}`)", True),
                          ("🎤 Channel",         before.channel.mention,              True),
                          ("⏱️ Session Time",    duration,                            True),
                          ("🛡️ Disconnected By", mod.mention,                        True),
                          ("👥 Still In Channel", _vc_occupancy(before.channel),      False),
                          ("📝 Reason",          reason or "*No reason provided*",    False),
                      ], actor=mod, target=member)
        else:
            await log(guild, "vc", "VC Leave",
                      f"**{member.display_name}** left {before.channel.mention}.",
                      discord.Color.red(),
                      fields=[
                          ("👤 Member",          f"{member.mention} (`{member.id}`)", True),
                          ("🎤 Channel",         before.channel.mention,              True),
                          ("⏱️ Session Time",    duration,                            True),
                          ("👥 Still In Channel", _vc_occupancy(before.channel),      False),
                      ], target=member)

    # ── Moved between channels (self-move vs. staff move) ──────
    elif before.channel != after.channel and before.channel is not None and after.channel is not None:
        joined = vc_join_time.pop((guild.id, member.id), None)
        if joined:
            guild_vc_stats = VC_STATS.setdefault(guild.id, {})
            guild_vc_stats[member.id] = guild_vc_stats.get(member.id, 0) + int(now - joined)
        if not member.bot:
            vc_join_time[(guild.id, member.id)] = now

        mod, reason = await _find_recent_mod(guild, discord.AuditLogAction.member_move, member=member, channel=after.channel)
        if mod:
            await log(guild, "vc", "VC Moved (by Staff)",
                      f"**{member.display_name}** was moved from {before.channel.mention} to {after.channel.mention} by {mod.mention}.",
                      discord.Color.purple(),
                      fields=[
                          ("👤 Member",    f"{member.mention} (`{member.id}`)", True),
                          ("📤 From",      before.channel.mention,              True),
                          ("📥 To",        after.channel.mention,               True),
                          ("🛡️ Moved By",  mod.mention,                        True),
                          ("👥 In New Channel", _vc_occupancy(after.channel),   False),
                          ("📝 Reason",    reason or "*No reason provided*",    False),
                      ], actor=mod, target=member)
        else:
            await log(guild, "vc", "VC Moved",
                      f"**{member.display_name}** moved from {before.channel.mention} to {after.channel.mention}.",
                      discord.Color.purple(),
                      fields=[
                          ("👤 Member",         f"{member.mention} (`{member.id}`)", True),
                          ("📤 From",           before.channel.mention,              True),
                          ("📥 To",             after.channel.mention,               True),
                          ("👥 In New Channel", _vc_occupancy(after.channel),        False),
                      ], target=member)

    # ── Server mute / unmute (staff-imposed only; only while still connected) ──
    if before.channel is not None and after.channel is not None and before.mute != after.mute:
        mod, reason = await _find_recent_mod(
            guild, discord.AuditLogAction.member_update, member=member,
            attr="mute", expected=after.mute
        )
        if after.mute:
            await log(guild, "vc", "VC Server Muted",
                      f"**{member.display_name}** was server muted in {after.channel.mention}.",
                      discord.Color.dark_red(),
                      fields=[
                          ("👤 Member",   f"{member.mention} (`{member.id}`)",   True),
                          ("🎤 Channel",  after.channel.mention,                 True),
                          ("🛡️ Muted By", mod.mention if mod else "*Unknown*",  True),
                          ("📝 Reason",   reason or "*No reason provided*",      False),
                      ], actor=mod, target=member)
        else:
            await log(guild, "vc", "VC Server Unmuted",
                      f"**{member.display_name}** was server unmuted in {after.channel.mention}.",
                      discord.Color.green(),
                      fields=[
                          ("👤 Member",     f"{member.mention} (`{member.id}`)",   True),
                          ("🎤 Channel",    after.channel.mention,                 True),
                          ("🛡️ Unmuted By", mod.mention if mod else "*Unknown*",  True),
                      ], actor=mod, target=member)

    # ── Server deafen / undeafen (staff-imposed only; only while still connected) ──
    if before.channel is not None and after.channel is not None and before.deaf != after.deaf:
        mod, reason = await _find_recent_mod(
            guild, discord.AuditLogAction.member_update, member=member,
            attr="deaf", expected=after.deaf
        )
        if after.deaf:
            await log(guild, "vc", "VC Server Deafened",
                      f"**{member.display_name}** was server deafened in {after.channel.mention}.",
                      discord.Color.dark_red(),
                      fields=[
                          ("👤 Member",     f"{member.mention} (`{member.id}`)",   True),
                          ("🎤 Channel",    after.channel.mention,                 True),
                          ("🛡️ Deafened By", mod.mention if mod else "*Unknown*", True),
                          ("📝 Reason",     reason or "*No reason provided*",      False),
                      ], actor=mod, target=member)
        else:
            await log(guild, "vc", "VC Server Undeafened",
                      f"**{member.display_name}** was server undeafened in {after.channel.mention}.",
                      discord.Color.green(),
                      fields=[
                          ("👤 Member",       f"{member.mention} (`{member.id}`)",   True),
                          ("🎤 Channel",      after.channel.mention,                 True),
                          ("🛡️ Undeafened By", mod.mention if mod else "*Unknown*", True),
                      ], actor=mod, target=member)

    # ── Member joined a temp VC mid-session ──────────────────
    if after.channel and after.channel.id in temp_vc_owners:
        text_id = temp_vc_text_channels.get(after.channel.id)
        text_channel = guild.get_channel(text_id) if text_id else None
        if text_channel:
            # Grant text access
            try:
                await text_channel.set_permissions(
                    member, view_channel=True, send_messages=True, read_message_history=True
                )
            except discord.HTTPException:
                pass

    # ── Member left a temp VC ────────────────────────────────
    if (before.channel and before.channel.id in temp_vc_owners
            and (after.channel is None or after.channel.id != before.channel.id)):
        text_id = temp_vc_text_channels.get(before.channel.id)
        text_channel = guild.get_channel(text_id) if text_id else None
        if text_channel:
            # Remove text access (non-owners only)
            if temp_vc_owners.get(before.channel.id) != member.id:
                try:
                    await text_channel.set_permissions(member, overwrite=None)
                except discord.HTTPException:
                    pass

        # Owner left -- lift any ,vckick restrictions for this session.
        # A kick is only meant to lock someone out while the owner is
        # still in the call, not permanently (that's what ,vcban is for).
        if temp_vc_owners.get(before.channel.id) == member.id:
            kicked_ids = vc_kicked.pop(before.channel.id, set())
            if kicked_ids:
                for kicked_id in kicked_ids:
                    kicked_member = guild.get_member(kicked_id)
                    if kicked_member is None:
                        continue
                    try:
                        ow = before.channel.overwrites_for(kicked_member)
                        ow.connect = None
                        if ow.is_empty():
                            await before.channel.set_permissions(kicked_member, overwrite=None)
                        else:
                            await before.channel.set_permissions(kicked_member, overwrite=ow)
                    except discord.HTTPException:
                        pass
                _save_temp_vcs()

    # Create temp VC
    if after.channel and after.channel.name == JOIN_TO_CREATE_CHANNEL_NAME:
        category = discord.utils.get(guild.categories, name=TEMP_VC_CATEGORY_NAME)
        if category is None:
            category = await guild.create_category(TEMP_VC_CATEGORY_NAME)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(connect=True, view_channel=True),
            member: discord.PermissionOverwrite(
                manage_channels=True,
                manage_permissions=True,
                move_members=True,
                connect=True,
                speak=True,
                view_channel=True,
                send_messages=True
            )
        }

        vc_name = f"{member.display_name}'s VC"

        new_vc = await guild.create_voice_channel(
            name=vc_name,
            category=category,
            overwrites=overwrites,
            reason="Join to create VC"
        )

        # No separate text channel — the control panel and all VC chat
        # (join/leave announcements, etc.) post directly into the voice
        # channel's own built-in text chat instead.
        temp_vc_owners[new_vc.id] = member.id
        temp_vc_text_channels[new_vc.id] = new_vc.id

        # Re-apply any bans/mods this owner handed out on a previous temp VC —
        # see vc_owner_bans/vc_owner_mods above for why vc_banned/vc_mods
        # alone aren't enough (both are scoped to the old, now-deleted
        # channel ID).
        persisted_bans = vc_owner_bans.get(guild.id, {}).get(member.id, set())
        persisted_mods = vc_owner_mods.get(guild.id, {}).get(member.id, set())
        state_changed = False

        if persisted_bans:
            vc_banned[new_vc.id] = set()
            for banned_id in persisted_bans:
                banned_member = guild.get_member(banned_id)
                if banned_member is None:
                    continue
                try:
                    await new_vc.set_permissions(banned_member, connect=False, view_channel=False)
                    vc_banned[new_vc.id].add(banned_id)
                    state_changed = True
                except discord.HTTPException:
                    pass

        if persisted_mods:
            vc_mods[new_vc.id] = set()
            for mod_id in persisted_mods:
                mod_member = guild.get_member(mod_id)
                if mod_member is None:
                    continue
                try:
                    await new_vc.set_permissions(mod_member, move_members=True, mute_members=True, deafen_members=True, manage_channels=True)
                    vc_mods[new_vc.id].add(mod_id)
                    state_changed = True
                except discord.HTTPException:
                    pass

        if state_changed:
            _save_temp_vcs()

        await member.move_to(new_vc)
        await send_vc_control_panel(new_vc, member, new_vc)

        await log(
            guild,
            "vc",
            "Temporary VC Created",
            f"Owner: {member.mention}\nVC: **{new_vc.name}**",
            discord.Color.green()
        )

    # Clean up empty temp VC
    if before.channel and before.channel.id in temp_vc_owners:
        if len(before.channel.members) == 0:
            try:
                old_name = before.channel.name
                await before.channel.delete(reason="Temp VC empty")
                await log(
                    guild,
                    "vc",
                    "Temporary VC Deleted",
                    f"VC: **{old_name}** was deleted because it became empty.",
                    discord.Color.orange()
                )
            except discord.HTTPException:
                pass

            temp_vc_owners.pop(before.channel.id, None)
            temp_vc_text_channels.pop(before.channel.id, None)
            vc_banned.pop(before.channel.id, None)
            vc_mods.pop(before.channel.id, None)
            vc_kicked.pop(before.channel.id, None)
            vc_locked.discard(before.channel.id)


@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    # Track chat stats
    guild_stats = CHAT_STATS.setdefault(message.guild.id, {})
    guild_stats[message.author.id] = guild_stats.get(message.author.id, 0) + 1

    # Track staff activity for this award period (Most Active Staff / MVP)
    if _is_staff_member(message.author):
        staff_stats = STAFF_ACTIVITY.setdefault(message.guild.id, {})
        staff_stats[message.author.id] = staff_stats.get(message.author.id, 0) + 1
        mvp_stats = MVP_ACTIVITY.setdefault(message.guild.id, {})
        mvp_stats[message.author.id] = mvp_stats.get(message.author.id, 0) + 1

    # ── AFK: clear the author's own AFK the moment they talk again ──
    afk_entry = AFK_USERS.get(message.guild.id, {}).pop(message.author.id, None)
    if afk_entry:
        _save_afk_users()
        try:
            await message.author.edit(nick=afk_entry.get("old_nick"), reason="No longer AFK")
        except (discord.Forbidden, discord.HTTPException):
            pass
        try:
            await message.channel.send(f"👋 Welcome back, {message.author.mention} — I removed your AFK status.", delete_after=8)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ── AFK: let the sender know if they mentioned someone who's AFK ──
    if message.mentions:
        guild_afk = AFK_USERS.get(message.guild.id, {})
        notes = [
            f"💤 {mentioned.mention} is AFK ({_format_afk_duration(time.time() - entry['since'])} ago) — {entry['reason']}"
            for mentioned in message.mentions
            if (entry := guild_afk.get(mentioned.id))
        ]
        if notes:
            try:
                await message.channel.send("\n".join(notes[:5]), delete_after=15)
            except (discord.Forbidden, discord.HTTPException):
                pass

    # ── Nudge toward the rep role if they can't actually see their GIF ──
    # A file-attachment GIF without attach_files never reaches the bot at
    # all (Discord blocks the send client-side) — this only ever fires for
    # the realistic case, a GIF link/native-picker GIF without embed_links,
    # which still sends fine as plain text, just with no preview.
    if _is_gif_message(message) and not message.channel.permissions_for(message.author).embed_links:
        try:
            await message.reply("get rep the server to get yo pic perms dummy 😭", mention_author=True)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # Always process commands first — never let automod swallow bot commands
    # Staff (manage_messages+) and command invocations are exempt from automod.
    # Checked via a real context resolution (ctx.valid), NOT a bare prefix-string
    # check — startswith(",") alone would let anyone dodge the filter by typing
    # ",badword1" or ",discord.gg/scam", since that "looks like" a command
    # without needing to actually BE one.
    ctx = await bot.get_context(message)
    is_command = ctx.valid
    is_staff = (
        isinstance(message.author, discord.Member)
        and (
            message.author.guild_permissions.manage_messages
            or message.author.guild_permissions.administrator
        )
    )

    if not is_command and not is_staff:
        if await _run_automod(message):
            return

        is_spamming = await handle_spam(message)
        if is_spamming:
            return

    # ── Talk to the bot: reply with real AI when directly @mentioned ──
    if not is_command and AI_CHAT_ENABLED and bot.user in message.mentions and not message.mention_everyone:
        clean_content = message.content
        for mention_str in (f"<@{bot.user.id}>", f"<@!{bot.user.id}>"):
            clean_content = clean_content.replace(mention_str, "")
        clean_content = clean_content.strip()
        if clean_content and not _on_cooldown(message.guild.id, message.author.id, "aichat", 5):
            async with message.channel.typing():
                reply = await _ask_ai(message.guild.id, message.author.id, clean_content)
            if reply:
                try:
                    await message.reply(reply, mention_author=False)
                except (discord.Forbidden, discord.HTTPException):
                    pass

    await bot.process_commands(message)


async def _run_automod(message: discord.Message) -> bool:
    """Runs the bad-word/link filter against a message's current content.
    Shared by on_message (new messages) and on_message_edit (so an edit
    can't sneak filtered content past automod by starting clean and editing
    afterward). Returns True if the message was deleted."""
    msg = message.content.lower()
    has_bad_word = any(w in msg for w in BAD_WORDS)
    has_link = any(l in msg for l in LINKS)

    # GIFs (Tenor/Giphy links from Discord's built-in picker, direct .gif
    # links, or .gif attachments) are exempt from the link filter for
    # whichever role each server designates via ,setgifrole — everywhere,
    # VC text channels included, since this handler runs for every
    # text channel in every server the bot is in.
    gif_allowed = False
    if has_link and not has_bad_word and _is_gif_message(message):
        member_role = _resolve_gif_exempt_role(message.guild)
        gif_allowed = (
            member_role is not None
            and isinstance(message.author, discord.Member)
            and member_role in message.author.roles
        )

    if has_bad_word or (has_link and not gif_allowed):
        try:
            await message.delete()
            await log(
                message.guild,
                "messages",
                "AutoMod Deleted Message",
                f"Author: {message.author}\nChannel: {message.channel.mention}\nContent: {message.content or 'None'}",
                discord.Color.orange()
            )
        except discord.Forbidden:
            await log(
                message.guild,
                "mod",
                "AutoMod Delete Failed",
                f"Could not delete a message from {message.author.mention} in {message.channel.mention}. Check permissions.",
                discord.Color.red()
            )
        except discord.HTTPException:
            pass
        return True
    return False


@bot.command()
async def afk(ctx, *, reason: str = "AFK"):
    """
    Mark yourself as AFK — anyone who @mentions you gets told you're away
    (and why), and it clears automatically the next time you send a
    message. Best-effort prefixes your nickname with "[AFK]" too (skipped
    silently if the bot can't manage your nickname).
    Usage: ,afk [reason]
    """
    guild_id = ctx.guild.id
    reason = reason.strip()[:200] or "AFK"
    AFK_USERS.setdefault(guild_id, {})[ctx.author.id] = {
        "reason": reason, "since": time.time(), "old_nick": ctx.author.nick,
    }
    _save_afk_users()

    new_nick = f"[AFK] {ctx.author.display_name}"[:32]
    try:
        await ctx.author.edit(nick=new_nick, reason="Marked as AFK")
    except (discord.Forbidden, discord.HTTPException):
        pass

    embed = discord.Embed(
        title="💤 You're now AFK",
        description=f"**Reason:** {reason}\n\nI'll let people know if they mention you, and clear this automatically once you send a message.",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    await ctx.send(embed=embed)


# ============================================================
# GLOBAL ERROR HANDLER
# ============================================================

def _command_usage(command: commands.Command) -> str:
    """Best-effort 'how to actually run this' string: prefer an explicit
    'Usage: ...' line from the command's docstring (nearly every command in
    this bot has one), else fall back to discord.py's auto-generated
    argument signature so every command still gets *something* useful."""
    if command is None:
        return ",cmds"
    doc = command.help or ""
    for line in doc.splitlines():
        line = line.strip()
        if line.lower().startswith("usage:"):
            return line.split(":", 1)[1].strip()
    sig = command.signature
    return f",{command.qualified_name}" + (f" {sig}" if sig else "")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(f"⚠️ {ctx.author.mention}: You don't have a **permitted role** to use `{ctx.command.qualified_name}`")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ I'm missing the required permissions to do that.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: `{error.param.name}`.\n✅ **Correct usage:** `{_command_usage(ctx.command)}`")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send(f"❌ Member not found — mention them or use their exact name/ID.\n✅ **Correct usage:** `{_command_usage(ctx.command)}`")
    elif isinstance(error, commands.RoleNotFound):
        await ctx.send(f"❌ Role not found — mention it or use its exact name/ID.\n✅ **Correct usage:** `{_command_usage(ctx.command)}`")
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send(f"❌ Channel not found — mention it or use its exact name/ID.\n✅ **Correct usage:** `{_command_usage(ctx.command)}`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"❌ Invalid argument provided.\n✅ **Correct usage:** `{_command_usage(ctx.command)}`")
    elif isinstance(error, commands.UserInputError):
        # Catch-all for anything else input-related (bad color, bad union
        # argument, too many arguments, ...) — still get usage help instead
        # of a dead-end generic error.
        await ctx.send(f"❌ That's not quite right.\n✅ **Correct usage:** `{_command_usage(ctx.command)}`")
    elif isinstance(error, commands.CommandNotFound):
        pass  # silently ignore unknown commands
    elif isinstance(error, commands.NotOwner):
        await ctx.send(f"⚠️ {ctx.author.mention}: `{ctx.command.qualified_name}` is bot-owner only.")
    elif isinstance(error, TicketOnlyModeRestricted):
        await ctx.send(f"🎫 {ctx.author.mention}: This bot is running in **ticket-only mode** — only the ticket system is available. Use `,help` to see what's active.")
    elif isinstance(error, TicketSubscriptionInactive):
        await ctx.send(
            "🚫 **TrapAI Subscription Inactive**\n"
            "This server's TrapAI subscription has expired. Renew your subscription to restore the ticket system.\n"
            "Use `,subscribe` to renew, or `,subscriptionstatus` to see details."
        )
    elif isinstance(error, WholeBotPremiumRequired):
        await ctx.send(
            f"🔒 **Premium Required**\n"
            f"`,{ctx.command.qualified_name}` is part of the Whole Bot **Premium** tier — this server is on **Regular**.\n"
            "Run `,subscribe wholebot premium` to upgrade."
        )
    elif isinstance(error, commands.CheckFailure):
        # Catches MissingRole and any other custom permission check —
        # same "permitted role" wording as MissingPermissions above, since
        # from the user's side it's the same thing: their role doesn't
        # carry whatever this command requires.
        await ctx.send(f"⚠️ {ctx.author.mention}: You don't have a **permitted role** to use `{ctx.command.qualified_name}`")
    elif isinstance(error, commands.CommandInvokeError):
        # Anything the command body itself raised without catching — a lot of
        # commands call Discord API methods (add_roles, edit, delete, ...)
        # without their own try/except, so this is the actual backstop for
        # those instead of a dead-end "unexpected error" with no explanation.
        original = error.original
        if isinstance(original, discord.Forbidden):
            await ctx.send("❌ I don't have permission to do that — check my role position and channel permissions.")
        elif isinstance(original, discord.NotFound):
            await ctx.send("❌ That no longer exists (message/channel/role may have been deleted).")
        elif isinstance(original, discord.HTTPException):
            # The chat message alone doesn't carry Discord's actual rejection
            # reason (e.g. "Invalid Form Body: ... This field is required") —
            # print it so it's at least visible in the host's logs, instead
            # of being swallowed with zero trace anywhere.
            print(f"[HTTPException] {ctx.command} in guild {ctx.guild.id if ctx.guild else 'DM'}: "
                  f"status={original.status} code={getattr(original, 'code', '?')} text={original.text}")
            await ctx.send(f"❌ Discord rejected that action (`{original.status}`) — please try again.")
        else:
            await ctx.send("❌ An unexpected error occurred.")
            raise error
    else:
        await ctx.send("❌ An unexpected error occurred.")
        raise error


# ============================================================
# INTERACTION (button / modal / slash) ERROR HANDLER
# Catches any unhandled exception from UI interactions so
# Discord never shows "This interaction failed" to the user.
# ============================================================
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    msg = "❌ Something went wrong. Please try again."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except (discord.HTTPException, discord.InteractionResponded):
        pass
    raise error


# ============================================================
# BASIC COMMANDS
# ============================================================
@bot.command()
async def ping(ctx):
    latency = round(bot.latency * 1000)
    embed = discord.Embed(
        title="🏓 Pong!",
        description=f"Latency: **{latency}ms**",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text="TrapAI")
    await ctx.send(embed=embed)


@bot.command(name="help")
async def help_cmd(ctx):
    """Shows this message. Usage: ,help"""
    await ctx.send(embed=_build_help_embed(ctx.guild))


@bot.command(name="cmds")
async def cmds(ctx):
    """Same as ,help — full command reference in one message. Usage: ,cmds"""
    await ctx.send(embed=_build_help_embed(ctx.guild))


@bot.command(name="setbotbio")
@commands.is_owner()
async def setbotbio(ctx, *, text: str = None):
    """
    Update the bot's public "About Me" description on Discord — this is a
    GLOBAL change visible on the bot's profile in every server it's in,
    not scoped to this one. Bot-owner only.
    Usage:
      ,setbotbio                — auto-generate using COMMANDS_SITE_URL
      ,setbotbio <custom text>  — set your own description (max 400 chars)
    """
    if text is None:
        if not COMMANDS_SITE_URL:
            await ctx.send(
                "❌ No text given and `COMMANDS_SITE_URL` isn't configured on this bot. "
                "Either pass text yourself, or set that env var first.",
                delete_after=10
            )
            return
        text = f"Server protection, done right. 🛡️\n\nBrowse every command: {COMMANDS_SITE_URL}"

    if len(text) > 400:
        await ctx.send(f"❌ Description too long ({len(text)}/400 chars).", delete_after=8)
        return

    headers = {"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.patch(
                "https://discord.com/api/v10/applications/@me",
                headers=headers,
                json={"description": text}
            ) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    await ctx.send(f"❌ Discord rejected the update ({resp.status}): {body[:300]}", delete_after=12)
                    return
    except aiohttp.ClientError as e:
        await ctx.send(f"❌ Couldn't reach Discord's API: {e}", delete_after=10)
        return

    embed = discord.Embed(
        title="✅ Bot Bio Updated",
        description=f"```{text}```",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text="Visible on the bot's profile in every server it's in")
    await ctx.send(embed=embed)


# ============================================================
# WELCOME COMMANDS
# ============================================================

def _resolve_welcome_channel(guild: discord.Guild):
    """Per-guild welcome config takes priority; falls back to a channel
    literally named WELCOME_CHANNEL. Returns None if welcomes are disabled
    here or nothing resolves. Shared by on_member_join and ,welcome so they
    can never disagree on where welcome messages actually go."""
    wcfg = WELCOME_CONFIG.get(guild.id, {})
    if not wcfg.get("enabled", True):
        return None
    wch_id = wcfg.get("channel_id")
    if wch_id:
        return guild.get_channel(wch_id)
    return discord.utils.get(guild.text_channels, name=WELCOME_CHANNEL)


async def _send_welcome_embeds(channel, member: discord.Member):
    """Send the full welcome card into `channel` for `member`."""
    guild       = member.guild
    count       = guild.member_count
    invite      = _resolve_invite_link(guild)
    created_str = discord.utils.format_dt(member.created_at, "F")
    age_str     = discord.utils.format_dt(member.created_at, "R")

    def _ordinal(n: int) -> str:
        v = n % 100
        suffix = "th" if 11 <= v <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"{n:,}{suffix}"

    banner = discord.Embed(
        description=(
            f"# 🏘️ Welcome to **{guild.name}**, {member.mention}!\n\n"
            f"You are our **{_ordinal(count)} member** — glad you made it.\n"
            f"**{guild.name}** is live, active, and always moving.\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"**Get started in 3 steps:**\n"
            f"> **1.** Read the rules\n"
            f"> **2.** Head to **#verify** and click **Authenticate via Discord**\n"
            f"> **3.** Pick your roles and introduce yourself\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color=discord.Color.from_rgb(18, 18, 24),
        timestamp=discord.utils.utcnow()
    )
    banner.set_author(
        name=f"{member.display_name} just walked in",
        icon_url=member.display_avatar.url
    )
    banner.set_image(url=member.display_avatar.with_size(512).url)
    banner.add_field(name="👤 Member",      value=f"{member.mention}\n`{member}`",        inline=True)
    banner.add_field(name=f"🔢 Member #{count:,}", value=f"You are member **{_ordinal(count)}**", inline=True)
    banner.add_field(name="🆔 User ID",     value=f"`{member.id}`",                       inline=True)
    banner.add_field(name="📅 Account Created", value=f"{created_str}\n({age_str})",      inline=False)
    banner.add_field(name="🔒 Access Level", value="🔴 Locked — Verify to unlock",        inline=True)
    banner.add_field(name="🛡 Security",    value="TrapAI Active",                        inline=True)
    banner.add_field(name="🔗 Server Link", value=invite,                                  inline=True)
    if guild.icon:
        banner.set_thumbnail(url=guild.icon.url)
    banner.set_footer(
        text=f"{guild.name} • {guild.member_count:,} members",
        icon_url=guild.icon.url if guild.icon else None
    )

    scan = discord.Embed(
        description=(
            "```ansi\n"
            "\u001b[0;32m[✓]\u001b[0m Identity detected\n"
            "\u001b[0;33m[~]\u001b[0m Threat scan running...\n"
            "\u001b[0;31m[!]\u001b[0m Server access: LOCKED\n"
            "\u001b[0;36m[i]\u001b[0m Verification: REQUIRED\n"
            "```"
        ),
        color=discord.Color.from_rgb(0, 255, 120),
    )
    scan.set_footer(text=f"TrapAI Security System • {guild.name}")

    await channel.send(content=member.mention, embeds=[banner, scan])


@bot.command()
@_permitted_check(manage_messages=True)
async def welcome(ctx, member: discord.Member = None):
    """
    Re-send or preview the welcome card.
    Usage:
      ,welcome          — send welcome card for yourself (staff preview)
      ,welcome @user    — send welcome card for a specific member
    """
    target = member or ctx.author
    ch = _resolve_welcome_channel(ctx.guild)
    dest = ch or ctx.channel

    await _send_welcome_embeds(dest, target)

    if dest != ctx.channel:
        confirm = discord.Embed(
            description=f"✅ Welcome card sent to {dest.mention} for {target.mention}.",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        confirm.set_footer(text=f"Sent by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=confirm, delete_after=6)

    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass


@bot.command()
@_permitted_check(manage_messages=True)
async def sendwelcome(ctx, member: discord.Member):
    """
    Send the full welcome card for a specific member in the current channel.
    Usage: ,sendwelcome @user
    """
    await _send_welcome_embeds(ctx.channel, member)
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass


@bot.command()
@_permitted_check(manage_guild=True)
async def setwelcome(ctx, channel: discord.TextChannel = None, *, option: str = None):
    """
    Configure the auto-welcome system.
    Usage:
      ,setwelcome #channel   — set the welcome channel
      ,setwelcome disable    — turn off auto-welcome
      ,setwelcome enable     — turn auto-welcome back on
      ,setwelcome            — show current configuration
    """
    cfg = WELCOME_CONFIG.setdefault(ctx.guild.id, {"channel_id": None, "enabled": True})

    # ── Show current config ───────────────────────────────────
    if channel is None and option is None:
        ch_id = cfg.get("channel_id")
        ch_mention = f"<#{ch_id}>" if ch_id else f"`#{WELCOME_CHANNEL}` (default)"
        state = "✅ Enabled" if cfg.get("enabled", True) else "❌ Disabled"
        embed = discord.Embed(
            title="🏘️ Auto-Welcome Config",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="📢 Channel", value=ch_mention, inline=True)
        embed.add_field(name="⚙️ Status",  value=state,      inline=True)
        embed.add_field(
            name="📖 Commands",
            value=(
                "`,setwelcome #channel` — set channel\n"
                "`,setwelcome enable` — enable\n"
                "`,setwelcome disable` — disable"
            ),
            inline=False
        )
        embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        return

    # ── enable / disable via text option ─────────────────────
    if channel is None and option is not None:
        opt = option.strip().lower()
        if opt == "disable":
            cfg["enabled"] = False
            _save_welcome_config()
            embed = discord.Embed(
                description="❌ Auto-welcome has been **disabled**. New members will not receive a welcome message.",
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow()
            )
            embed.set_footer(text=f"Changed by {ctx.author}")
            await ctx.send(embed=embed)
        elif opt == "enable":
            cfg["enabled"] = True
            _save_welcome_config()
            ch_id = cfg.get("channel_id")
            ch_mention = f"<#{ch_id}>" if ch_id else f"`#{WELCOME_CHANNEL}`"
            embed = discord.Embed(
                description=f"✅ Auto-welcome has been **enabled** in {ch_mention}.",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow()
            )
            embed.set_footer(text=f"Changed by {ctx.author}")
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ Unknown option `{option}`. Use `enable`, `disable`, or mention a channel.")
        return

    # ── Set channel ───────────────────────────────────────────
    cfg["channel_id"] = channel.id
    cfg["enabled"] = True
    _save_welcome_config()
    embed = discord.Embed(
        title="✅ Auto-Welcome Configured",
        description=(
            f"New members will now be welcomed in {channel.mention}.\n\n"
            "Use `,setwelcome disable` to turn it off,\n"
            "or `,welcome @user` to preview the card."
        ),
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="📢 Channel", value=channel.mention,  inline=True)
    embed.add_field(name="⚙️ Status",  value="✅ Enabled",      inline=True)
    embed.set_footer(text=f"Set by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)

    try:
        await log(
            ctx.guild,
            "mod",
            "Auto-Welcome Channel Set",
            f"{ctx.author.mention} configured the welcome channel to {channel.mention}.",
            discord.Color.green(),
            fields=[
                ("📢 Channel",   channel.mention,                             True),
                ("🛡 Set By",    f"{ctx.author.mention} (`{ctx.author.id}`)", True),
            ],
            actor=ctx.author
        )
    except Exception:
        pass


@bot.command()
@_permitted_check(manage_guild=True)
async def setboostchannel(ctx, channel: discord.TextChannel = None):
    """
    Configure where the public boost thank-you message posts.
    Usage:
      ,setboostchannel #channel — set the boost thank-you channel
      ,setboostchannel reset    — clear override, fall back to a channel named "boosts"
      ,setboostchannel          — show current configuration
    """
    if channel is None:
        arg = ctx.message.content.split(maxsplit=1)
        opt = arg[1].strip().lower() if len(arg) > 1 else None
        if opt == "reset":
            BOOST_CHANNEL_OVERRIDES.pop(ctx.guild.id, None)
            _save_boost_channel()
            await ctx.send(f"✅ Boost channel override cleared. Falling back to `#{BOOST_CHANNEL}`.")
            return

        ch = _resolve_boost_channel(ctx.guild)
        embed = discord.Embed(
            title="🚀 Boost Thank-You Config",
            color=discord.Color.purple(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="📢 Channel",
            value=ch.mention if ch else f"*Not found — set one or create `#{BOOST_CHANNEL}`*",
            inline=True
        )
        embed.add_field(
            name="📖 Commands",
            value=(
                "`,setboostchannel #channel` — set channel\n"
                "`,setboostchannel reset` — clear override"
            ),
            inline=False
        )
        embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        return

    BOOST_CHANNEL_OVERRIDES[ctx.guild.id] = channel.id
    _save_boost_channel()
    embed = discord.Embed(
        title="✅ Boost Channel Configured",
        description=f"Server boost thank-you messages will now post in {channel.mention}.",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"Set by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command()
@_permitted_check(manage_guild=True)
async def setupdatechannel(ctx, channel: discord.TextChannel = None):
    """
    Configure where the automatic "what's new / what was fixed" message
    posts after the bot comes back online from an update. Without an
    override, it auto-picks any channel with "announce"/"update"/"news"/
    "patch" in its name (doesn't have to be named exactly "announcements"),
    falling back to the server's system channel.
    Usage:
      ,setupdatechannel #channel — pin the update-announcement channel
      ,setupdatechannel reset    — clear override, fall back to auto-detection
      ,setupdatechannel          — show current configuration
    """
    if channel is None:
        arg = ctx.message.content.split(maxsplit=1)
        opt = arg[1].strip().lower() if len(arg) > 1 else None
        if opt == "reset":
            UPDATE_CHANNEL_OVERRIDES.pop(ctx.guild.id, None)
            _save_update_channel_overrides()
            await ctx.send("✅ Update-announcement channel override cleared. Falling back to auto-detection.")
            return

        ch = _resolve_update_channel(ctx.guild)
        embed = discord.Embed(
            title="🔧 Update Announcement Config",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="📢 Channel",
            value=ch.mention if ch else "*Not found — set one with `,setupdatechannel #channel`*",
            inline=True
        )
        embed.add_field(
            name="📖 Commands",
            value=(
                "`,setupdatechannel #channel` — pin channel\n"
                "`,setupdatechannel reset` — clear override"
            ),
            inline=False
        )
        embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        return

    UPDATE_CHANNEL_OVERRIDES[ctx.guild.id] = channel.id
    _save_update_channel_overrides()
    embed = discord.Embed(
        title="✅ Update Channel Configured",
        description=f"\"What's new / what was fixed\" messages will now post in {channel.mention} after every update.",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"Set by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name="resendupdate", aliases=["reannounce"])
@_permitted_check(manage_guild=True)
async def resendupdate(ctx):
    """
    Re-post the latest changelog entry to THIS server only — bypasses the
    "already announced" check, so it works even if this server already
    saw it. Doesn't touch any other server the bot is in. Useful after
    fixing the update-announcement channel with ,setupdatechannel.
    Usage: ,resendupdate
    """
    if not CHANGELOG:
        await ctx.send("❌ No changelog entries configured.", delete_after=8)
        return
    latest = CHANGELOG[-1]
    channel = _resolve_update_channel(ctx.guild)
    if not channel:
        await ctx.send("❌ No update channel found — set one with `,setupdatechannel #channel` first.", delete_after=10)
        return
    try:
        await channel.send(embed=_build_update_embed(latest))
    except (discord.Forbidden, discord.HTTPException) as e:
        await ctx.send(f"❌ Failed to post: {e}", delete_after=10)
        return
    LAST_ANNOUNCED_VERSION[ctx.guild.id] = latest["version"]
    _save_last_announced_version()
    await ctx.send(f"✅ Re-posted v{latest['version']} changelog to {channel.mention}.", delete_after=8)


def _br_embed(title, description, color):
    embed = discord.Embed(title=title, description=description, color=color, timestamp=discord.utils.utcnow())
    embed.set_footer(text="TrapAI Booster Roles")
    return embed


@bot.command(name="br", aliases=["boosterrole", "boostrole"])
async def br(ctx, action: str = None, *, arg: str = None):
    """
    Manage your custom booster role — a perk for active server boosters.
    Usage:
      ,br                    — show your booster role status
      ,br create <name>      — create your custom role
      ,br name <new name>    — rename your role
      ,br color <hex>        — change your role's color (e.g. #ff0055)
      ,br delete             — remove your custom role
    """
    import random
    member = ctx.author
    guild = ctx.guild
    guild_roles = BOOSTER_ROLES.setdefault(guild.id, {})
    role_id = guild_roles.get(member.id)
    role = guild.get_role(role_id) if role_id else None
    if role_id and not role:
        # Stale entry — role was deleted outside of ,br (e.g. manually in Discord)
        guild_roles.pop(member.id, None)
        _save_booster_roles()
        role_id = None

    # Gate on actually holding Discord's integrated "Server Booster" role —
    # not just member.premium_since — so this literally matches "only people
    # with the booster role," including the split-second right after someone
    # stops boosting where premium_since may already be cleared but Discord
    # hasn't pulled the role yet (or vice versa).
    booster_role = guild.premium_subscriber_role
    is_booster = booster_role is not None and booster_role in member.roles

    if action is None:
        embed = discord.Embed(
            title="🌟 Booster Role",
            color=role.color if role else discord.Color.purple(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="🎭 Your Role", value=role.mention if role else "*None yet*", inline=True)
        if role:
            embed.add_field(name="🎨 Color", value=f"`#{role.color.value:06x}`", inline=True)
        embed.add_field(
            name="📖 Commands",
            value=(
                "`,br create <name>` — create your custom role\n"
                "`,br name <new name>` — rename it\n"
                "`,br color <hex>` — change its color\n"
                "`,br delete` — remove it"
            ),
            inline=False
        )
        if not is_booster:
            embed.set_footer(text="⚠️ You're not currently boosting — booster roles are a boost perk.")
        else:
            embed.set_footer(text="TrapAI Booster Roles")
        await ctx.send(embed=embed)
        return

    action = action.lower()

    if not is_booster:
        await ctx.send("❌ You need to be actively boosting this server to manage a booster role.", delete_after=8)
        return

    if action == "create":
        if role:
            await ctx.send(f"❌ You already have a booster role: {role.mention}. Use `,br name`/`,br color` to edit it.", delete_after=8)
            return
        if not arg or not arg.strip():
            await ctx.send("❌ Give it a name: `,br create <name>`", delete_after=8)
            return
        name = arg.strip()[:100]
        try:
            new_role = await guild.create_role(
                name=name,
                color=discord.Color(random.randint(0, 0xFFFFFF)),
                permissions=discord.Permissions.none(),
                reason=f"Booster role for {member} ({member.id})"
            )
        except (discord.Forbidden, discord.HTTPException):
            await ctx.send("❌ I couldn't create a role — check my Manage Roles permission.", delete_after=8)
            return
        try:
            await new_role.edit(position=max(1, guild.me.top_role.position - 1))
        except (discord.Forbidden, discord.HTTPException):
            pass
        try:
            await member.add_roles(new_role, reason="Booster role created")
        except (discord.Forbidden, discord.HTTPException):
            try:
                await new_role.delete(reason="Cleanup — failed to assign new booster role")
            except (discord.Forbidden, discord.HTTPException):
                pass
            await ctx.send("❌ Created the role but couldn't assign it to you — try again.", delete_after=8)
            return
        guild_roles[member.id] = new_role.id
        _save_booster_roles()
        await ctx.send(embed=_br_embed("✅ Booster Role Created", f"Created {new_role.mention} and assigned it to you!", new_role.color))
        return

    if action in ("name", "rename"):
        if not role:
            await ctx.send("❌ You don't have a booster role yet. Use `,br create <name>`.", delete_after=8)
            return
        if not arg or not arg.strip():
            await ctx.send("❌ Give it a new name: `,br name <new name>`", delete_after=8)
            return
        new_name = arg.strip()[:100]
        try:
            await role.edit(name=new_name, reason=f"Booster role renamed by {member}")
        except (discord.Forbidden, discord.HTTPException):
            await ctx.send("❌ Couldn't rename your role — check my role position/permissions.", delete_after=8)
            return
        await ctx.send(embed=_br_embed("✅ Role Renamed", f"Your booster role is now **{new_name}**.", role.color))
        return

    if action == "color":
        if not role:
            await ctx.send("❌ You don't have a booster role yet. Use `,br create <name>`.", delete_after=8)
            return
        if not arg or not arg.strip():
            await ctx.send("❌ Give a hex color: `,br color #ff0055`", delete_after=8)
            return
        hex_str = arg.strip().lstrip("#")
        try:
            if len(hex_str) != 6:
                raise ValueError
            color_val = int(hex_str, 16)
        except ValueError:
            await ctx.send("❌ Invalid hex color. Example: `,br color #ff0055`", delete_after=8)
            return
        try:
            await role.edit(color=discord.Color(color_val), reason=f"Booster role recolored by {member}")
        except (discord.Forbidden, discord.HTTPException):
            await ctx.send("❌ Couldn't recolor your role — check my role position/permissions.", delete_after=8)
            return
        await ctx.send(embed=_br_embed("✅ Color Updated", f"Your booster role color is now `#{color_val:06x}`.", discord.Color(color_val)))
        return

    if action == "delete":
        if not role:
            await ctx.send("❌ You don't have a booster role to delete.", delete_after=8)
            return
        try:
            await role.delete(reason=f"Booster role deleted by {member}")
        except (discord.Forbidden, discord.HTTPException):
            await ctx.send("❌ Couldn't delete your role — check my role position/permissions.", delete_after=8)
            return
        guild_roles.pop(member.id, None)
        _save_booster_roles()
        await ctx.send(embed=_br_embed("🗑️ Role Deleted", "Your booster role has been removed.", discord.Color.red()))
        return

    await ctx.send("❌ Unknown action. Use `,br create`, `,br name`, `,br color`, or `,br delete`.", delete_after=8)


@bot.command(name="exitsurveys", aliases=["exitreasons"])
@_permitted_check(manage_guild=True)
async def exitsurveys(ctx, limit: int = 10):
    """
    View recent exit survey responses — why members said they left.
    Usage: ,exitsurveys [limit]
    """
    entries = EXIT_SURVEY_RESPONSES.get(ctx.guild.id, [])
    if not entries:
        await ctx.send("📭 No exit survey responses yet.", delete_after=8)
        return

    limit = max(1, min(limit, 25))
    embed = discord.Embed(
        title="📝 Recent Exit Survey Responses",
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow()
    )
    for entry in entries[:limit]:
        ts = discord.utils.format_dt(datetime.fromtimestamp(entry["timestamp"]), "R")
        embed.add_field(
            name=f"{entry['username']} • {ts}",
            value=entry["reason"][:200],
            inline=False
        )
    embed.set_footer(text=f"{len(entries)} total response(s) tracked • Requested by {ctx.author}")
    await ctx.send(embed=embed)


@bot.command()
@_permitted_check(manage_guild=True)
async def disablewelcome(ctx):
    """
    Disable the auto-welcome message for new members.
    Usage: ,disablewelcome
    """
    cfg = WELCOME_CONFIG.setdefault(ctx.guild.id, {"channel_id": None, "enabled": True})
    cfg["enabled"] = False
    _save_welcome_config()
    embed = discord.Embed(
        description="❌ Auto-welcome has been **disabled**. New members will not receive a welcome message.",
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"Changed by {ctx.author}")
    await ctx.send(embed=embed)
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass


# ============================================================
# VC COMMANDS  (all upgraded — owner or VC-mod unless noted)
# ============================================================

def _vc_embed(title, description, color=discord.Color.dark_grey()):
    return discord.Embed(title=title, description=description, color=color, timestamp=discord.utils.utcnow())


@bot.command()
async def vclock(ctx):
    ch = get_owned_temp_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be in a VC you own or moderate.")
        return
    await _vc_lock(ch)
    await ctx.send(embed=_vc_embed("🔒 VC Locked", f"Only permitted users can now join **{ch.name}** — enforced for everyone, staff and the server owner included."))
    await _vc_announce(ctx.guild, ch, f"🔒 **{ctx.author.display_name}** locked the VC.")


@bot.command()
async def vcunlock(ctx):
    ch = get_owned_temp_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be in a VC you own or moderate.")
        return
    await _vc_unlock(ch)
    await ctx.send(embed=_vc_embed("🔓 VC Unlocked", f"**{ch.name}** is now open to everyone.", discord.Color.green()))
    await _vc_announce(ctx.guild, ch, f"🔓 **{ctx.author.display_name}** unlocked the VC.")


@bot.command()
async def vchide(ctx):
    ch = get_owned_temp_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be in a VC you own or moderate.")
        return
    await _vc_merge_permissions(ch, ctx.guild.default_role, view_channel=False)
    await ctx.send(embed=_vc_embed("👻 VC Hidden", f"**{ch.name}** is now invisible to everyone."))
    await _vc_announce(ctx.guild, ch, f"👻 **{ctx.author.display_name}** hid the VC.")


@bot.command()
async def vcshow(ctx):
    ch = get_owned_temp_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be in a VC you own or moderate.")
        return
    await _vc_merge_permissions(ch, ctx.guild.default_role, view_channel=True)
    await ctx.send(embed=_vc_embed("👀 VC Visible", f"**{ch.name}** is now visible to everyone.", discord.Color.blurple()))
    await _vc_announce(ctx.guild, ch, f"👀 **{ctx.author.display_name}** made the VC visible.")


@bot.command()
async def vcname(ctx, *, new_name: str):
    ch = get_owned_temp_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be in a VC you own or moderate.")
        return
    old = ch.name
    await ch.edit(name=new_name[:100])
    await ctx.send(embed=_vc_embed("✏️ VC Renamed", f"**{old}** → **{new_name[:100]}**"))
    await _vc_announce(ctx.guild, ch, f"✏️ **{ctx.author.display_name}** renamed the VC to **{new_name[:100]}**.")


@bot.command()
async def vclimit(ctx, limit: int):
    ch = get_owned_temp_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be in a VC you own or moderate.")
        return
    if limit < 0 or limit > 99:
        await ctx.send("❌ Limit must be 0–99.")
        return
    await ch.edit(user_limit=limit)
    label = f"**{limit}**" if limit else "**no limit**"
    await ctx.send(embed=_vc_embed("👥 User Limit Set", f"Limit for **{ch.name}** is now {label}."))
    await _vc_announce(ctx.guild, ch, f"👥 **{ctx.author.display_name}** set the limit to {label}.")


@bot.command()
async def vcbitrate(ctx, kbps: int):
    ch = get_owned_temp_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be in a VC you own or moderate.")
        return
    if kbps < 8 or kbps > 96:
        await ctx.send("❌ Bitrate must be 8–96 kbps.")
        return
    await ch.edit(bitrate=kbps * 1000)
    await ctx.send(embed=_vc_embed("🔊 Bitrate Updated", f"**{ch.name}** bitrate is now **{kbps}kbps**."))
    await _vc_announce(ctx.guild, ch, f"🔊 **{ctx.author.display_name}** set bitrate to **{kbps}kbps**.")


@bot.command()
async def vcregion(ctx, *, region: str = "auto"):
    ch = get_owned_temp_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be in a VC you own or moderate.")
        return
    raw = region.strip().lower()
    region_val = None if raw == "auto" else raw
    try:
        await ch.edit(rtc_region=region_val)
        label = f"**{raw}**" if region_val else "**auto**"
        await ctx.send(embed=_vc_embed("🌐 Region Set", f"**{ch.name}** region is now {label}."))
        await _vc_announce(ctx.guild, ch, f"🌐 **{ctx.author.display_name}** set the region to {label}.")
    except discord.HTTPException:
        await ctx.send("❌ Invalid region. Try: `auto`, `us-east`, `us-west`, `eu-west`, `singapore`, `sydney`, `brazil`, `hongkong`, `japan`, `russia`, `southafrica`, `india`.")


@bot.command()
async def vckick(ctx, member: discord.Member):
    ch = get_owned_temp_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be in a VC you own or moderate.")
        return
    err = await _vc_kick_member(ch, ctx.author, member)
    if err:
        await ctx.send(err)
        return
    await ctx.send(embed=_vc_embed("👢 Member Kicked", f"{member.mention} was kicked from **{ch.name}** and can't rejoin until the owner leaves the call.", discord.Color.orange()))
    await _vc_announce(ctx.guild, ch, f"👢 **{ctx.author.display_name}** kicked **{member.display_name}** from the VC.")


@bot.command()
async def vcban(ctx, member: discord.Member):
    ch = get_owned_temp_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be in a VC you own or moderate.")
        return
    err = await _vc_ban_member(ch, ctx.guild.id, ctx.author, member)
    if err:
        await ctx.send(err)
        return
    await ctx.send(embed=_vc_embed("🚫 VC Ban Applied", f"{member.mention} was banned from **{ch.name}**.", discord.Color.red()))
    await _vc_announce(ctx.guild, ch, f"🚫 **{ctx.author.display_name}** banned **{member.display_name}** from the VC.")


@bot.command()
async def vcunban(ctx, member: discord.Member):
    ch = get_owned_temp_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be in a VC you own or moderate.")
        return
    await _vc_unban_member(ch, ctx.guild.id, member)
    await ctx.send(embed=_vc_embed("✔️ VC Ban Removed", f"{member.mention} can join **{ch.name}** again.", discord.Color.green()))
    await _vc_announce(ctx.guild, ch, f"✔️ **{ctx.author.display_name}** unbanned **{member.display_name}**.")


@bot.command()
async def vcpermit(ctx, member: discord.Member):
    ch = get_owned_temp_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be in a VC you own or moderate.")
        return
    await _vc_permit_member(ch, ctx.guild.id, member)
    await ctx.send(embed=_vc_embed("✅ Access Granted", f"{member.mention} can now join **{ch.name}**.", discord.Color.green()))
    await _vc_announce(ctx.guild, ch, f"✅ **{ctx.author.display_name}** permitted **{member.display_name}** to join.")


@bot.command()
async def vcmute(ctx, member: discord.Member):
    ch = get_owned_temp_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be in a VC you own or moderate.")
        return
    if not member.voice or member.voice.channel != ch:
        await ctx.send("❌ That user is not in your VC.")
        return
    await member.edit(mute=True)
    await ctx.send(embed=_vc_embed("🔇 Member Muted", f"{member.mention} has been server-muted.", discord.Color.orange()))
    await _vc_announce(ctx.guild, ch, f"🔇 **{ctx.author.display_name}** muted **{member.display_name}**.")


@bot.command()
async def vcunmute(ctx, member: discord.Member):
    ch = get_owned_temp_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be in a VC you own or moderate.")
        return
    await member.edit(mute=False)
    await ctx.send(embed=_vc_embed("🔊 Member Unmuted", f"{member.mention} has been unmuted.", discord.Color.green()))
    await _vc_announce(ctx.guild, ch, f"🔊 **{ctx.author.display_name}** unmuted **{member.display_name}**.")


@bot.command()
async def vcdeafen(ctx, member: discord.Member):
    ch = get_owned_temp_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be in a VC you own or moderate.")
        return
    if not member.voice or member.voice.channel != ch:
        await ctx.send("❌ That user is not in your VC.")
        return
    await member.edit(deafen=True)
    await ctx.send(embed=_vc_embed("🔕 Member Deafened", f"{member.mention} has been server-deafened.", discord.Color.orange()))
    await _vc_announce(ctx.guild, ch, f"🔕 **{ctx.author.display_name}** deafened **{member.display_name}**.")


@bot.command()
async def vcundeafen(ctx, member: discord.Member):
    ch = get_owned_temp_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be in a VC you own or moderate.")
        return
    await member.edit(deafen=False)
    await ctx.send(embed=_vc_embed("🔊 Member Undeafened", f"{member.mention} has been undeafened.", discord.Color.green()))
    await _vc_announce(ctx.guild, ch, f"🔊 **{ctx.author.display_name}** undeafened **{member.display_name}**.")


@bot.command()
async def vctransfer(ctx, member: discord.Member):
    ch = get_strictly_owned_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be the **owner** of a temporary VC.")
        return
    err = await _vc_transfer_ownership(ch, ctx.guild, member)
    if err:
        await ctx.send(err)
        return
    await ctx.send(embed=_vc_embed("👑 Ownership Transferred", f"{member.mention} is now the owner of **{ch.name}**.", discord.Color.purple()))
    await _vc_announce(ctx.guild, ch, f"👑 **{ctx.author.display_name}** transferred ownership to **{member.display_name}**.")


@bot.command()
async def vcclaim(ctx):
    """
    Claim ownership of your current temp VC if the owner has left it.
    Usage: ,vcclaim
    """
    voice = ctx.author.voice
    if not voice or not voice.channel:
        await ctx.send("❌ You must be in a voice channel to claim it.")
        return
    ch = voice.channel
    if ch.id not in temp_vc_owners:
        await ctx.send("❌ This isn't a temporary VC.")
        return

    old_owner_id = temp_vc_owners.get(ch.id)
    old_owner = ctx.guild.get_member(old_owner_id) if old_owner_id else None

    if old_owner_id == ctx.author.id:
        await ctx.send("❌ You already own this VC.")
        return
    if old_owner and old_owner.voice and old_owner.voice.channel == ch:
        await ctx.send(f"❌ {old_owner.mention} is still here — ask them to use `,vctransfer`, or claim it after they leave.")
        return

    await _vc_transfer_ownership(ch, ctx.guild, ctx.author)

    await ctx.send(embed=_vc_embed("👑 Ownership Claimed", f"{ctx.author.mention} claimed ownership of **{ch.name}** — the previous owner left.", discord.Color.purple()))
    await _vc_announce(ctx.guild, ch, f"👑 **{ctx.author.display_name}** claimed ownership — the previous owner left the VC.")
    await log(
        ctx.guild,
        "vc",
        "VC Ownership Claimed",
        f"{ctx.author.mention} claimed **{ch.name}** after the owner left.",
        discord.Color.purple(),
        fields=[
            ("👑 New Owner",      f"{ctx.author.mention} (`{ctx.author.id}`)", True),
            ("👤 Previous Owner", old_owner.mention if old_owner else (f"`{old_owner_id}`" if old_owner_id else "Unknown"), True),
        ],
        target=ctx.author
    )


@bot.command()
async def vcmod(ctx, member: discord.Member):
    ch = get_strictly_owned_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be the **owner** of a temporary VC.")
        return
    err = await _vc_add_mod(ch, ctx.guild.id, ctx.author, member)
    if err:
        await ctx.send(err)
        return
    await ctx.send(embed=_vc_embed("🛡 VC Mod Granted", f"{member.mention} is now a VC moderator in **{ch.name}**.", discord.Color.blurple()))
    await _vc_announce(ctx.guild, ch, f"🛡 **{ctx.author.display_name}** made **{member.display_name}** a VC moderator.")


@bot.command()
async def vcremovemod(ctx, member: discord.Member):
    ch = get_strictly_owned_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be the **owner** of a temporary VC.")
        return
    vc_mods.get(ch.id, set()).discard(member.id)
    vc_owner_mods.get(ctx.guild.id, {}).get(ctx.author.id, set()).discard(member.id)
    _save_temp_vcs()
    await ch.set_permissions(member, overwrite=None)
    await ctx.send(embed=_vc_embed("🗑️ VC Mod Removed", f"{member.mention} is no longer a VC moderator in **{ch.name}**.", discord.Color.orange()))
    await _vc_announce(ctx.guild, ch, f"🗑️ **{ctx.author.display_name}** removed **{member.display_name}** as VC moderator.")


@bot.command(name="d", aliases=["drag"])
@_permitted_check(move_members=True)
async def drag_member(ctx, member: discord.Member):
    """
    Drag a member into the voice channel YOU'RE currently in — a quick
    staff shortcut for Discord's native drag move, without opening the
    member list. Requires the Move Members permission (or a role granted
    it via ,setpermittedrole) — works on ANY voice channel in the
    server, not just temp/owned VCs.
    Usage: ,d @user
    """
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("❌ You need to be in a voice channel to drag someone into it.")
        return
    if not member.voice or not member.voice.channel:
        await ctx.send(f"❌ {member.mention} is not in a voice channel.")
        return

    destination = ctx.author.voice.channel
    from_channel = member.voice.channel
    if from_channel == destination:
        await ctx.send(f"❌ {member.mention} is already in {destination.mention}.")
        return

    try:
        await member.move_to(destination, reason=f"Dragged by {ctx.author}")
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to move that member.")
        return
    except discord.HTTPException:
        await ctx.send("❌ Something went wrong moving that member.")
        return

    await ctx.send(embed=_vc_embed(
        "🖐️ Member Dragged",
        f"{member.mention} was dragged from **{from_channel.name}** to **{destination.name}**.",
        discord.Color.blurple()
    ))

    await log(ctx.guild, "vc", "Member Dragged", None, discord.Color.blurple(),
              fields=[
                  ("🛡 Staff",  f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("👤 Member", f"{member.mention} (`{member.id}`)",         True),
                  ("📤 From",   from_channel.mention,                        True),
                  ("📥 To",     destination.mention,                         True),
              ],
              actor=ctx.author, target=member)


# ============================================================
# ADMIN COMMANDS
# ============================================================
@bot.command()
@_permitted_check(administrator=True)
async def setup(ctx):
    guild = ctx.guild
    await ctx.send(f"⚙️ Setting up **{guild.name}**...")

    unverified_role = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE)
    verified_role = discord.utils.get(guild.roles, name=VERIFIED_ROLE)
    jail_role = discord.utils.get(guild.roles, name=JAIL_ROLE)

    if not unverified_role:
        unverified_role = await guild.create_role(name=UNVERIFIED_ROLE, reason="Setup command")
    if not verified_role:
        verified_role = await guild.create_role(name=VERIFIED_ROLE, reason="Setup command")
    if not jail_role:
        jail_role = await guild.create_role(name=JAIL_ROLE, reason="Setup command")

    everyone = guild.default_role

    arrival_category = discord.utils.get(guild.categories, name="🤖 TrapAI Arrival Zone")
    if arrival_category is None:
        arrival_category = await guild.create_category("🤖 TrapAI Arrival Zone")

    island_category = discord.utils.get(guild.categories, name="🏘️ Glock30")
    if island_category is None:
        island_category = await guild.create_category("🏘️ Glock30")

    staff_category = discord.utils.get(guild.categories, name="🛡 Staff HQ")
    if staff_category is None:
        staff_category = await guild.create_category("🛡 Staff HQ")

    restricted_category = discord.utils.get(guild.categories, name="🔒 Restricted")
    if restricted_category is None:
        restricted_category = await guild.create_category("🔒 Restricted")

    temp_vc_category = discord.utils.get(guild.categories, name=TEMP_VC_CATEGORY_NAME)
    if temp_vc_category is None:
        temp_vc_category = await guild.create_category(TEMP_VC_CATEGORY_NAME)

    ticket_category = discord.utils.get(guild.categories, name="🎫 Tickets")
    if ticket_category is None:
        ticket_category = await guild.create_category("🎫 Tickets")

    await arrival_category.set_permissions(everyone, view_channel=False)
    await arrival_category.set_permissions(unverified_role, view_channel=True, send_messages=False, read_message_history=True, use_application_commands=True)
    await arrival_category.set_permissions(verified_role, view_channel=False)
    await arrival_category.set_permissions(jail_role, view_channel=False)

    await island_category.set_permissions(everyone, view_channel=False)
    await island_category.set_permissions(unverified_role, view_channel=False)
    await island_category.set_permissions(verified_role, view_channel=True, send_messages=True, read_message_history=True)
    await island_category.set_permissions(jail_role, view_channel=False)

    await staff_category.set_permissions(everyone, view_channel=False)
    await staff_category.set_permissions(unverified_role, view_channel=False)
    await staff_category.set_permissions(verified_role, view_channel=False)
    await staff_category.set_permissions(jail_role, view_channel=False)

    await restricted_category.set_permissions(everyone, view_channel=False)
    await restricted_category.set_permissions(unverified_role, view_channel=False)
    await restricted_category.set_permissions(verified_role, view_channel=False)
    await restricted_category.set_permissions(jail_role, view_channel=True, send_messages=False, read_message_history=True)

    await ticket_category.set_permissions(everyone, view_channel=False)

    async def get_or_create_text_channel(name, category):
        channel = discord.utils.get(guild.text_channels, name=name)
        if channel is None:
            channel = await guild.create_text_channel(name, category=category)
        return channel

    async def get_or_create_voice_channel(name, category):
        channel = discord.utils.get(guild.voice_channels, name=name)
        if channel is None:
            channel = await guild.create_voice_channel(name, category=category)
        return channel

    welcome_channel = await get_or_create_text_channel("welcome", arrival_category)
    rules_channel = await get_or_create_text_channel("rules", arrival_category)
    verify_channel = await get_or_create_text_channel("verify", arrival_category)

    general_channel = await get_or_create_text_channel("general-chat", island_category)
    media_channel = await get_or_create_text_channel("media", island_category)
    bot_channel = await get_or_create_text_channel("bot-commands", island_category)

    vc_logs_channel = await get_or_create_text_channel("vc-logs", staff_category)
    mod_logs_channel = await get_or_create_text_channel("mod-logs", staff_category)
    role_logs_channel = await get_or_create_text_channel("role-logs", staff_category)
    staff_chat_channel = await get_or_create_text_channel("staff-chat", staff_category)

    jail_channel = await get_or_create_text_channel("jail", restricted_category)
    jail_logs_channel = await get_or_create_text_channel("jail-logs", restricted_category)

    await get_or_create_voice_channel(JOIN_TO_CREATE_CHANNEL_NAME, island_category)
    await get_or_create_voice_channel("🔥 Glock30 VC", island_category)
    await get_or_create_voice_channel("🎮 Chill VC", island_category)

    await welcome_channel.edit(topic="🏘️ Arrival Zone • New members are scanned by TrapAI before entering Glock30")
    await rules_channel.edit(topic="📜 TrapAI server rules and enforcement")
    await verify_channel.edit(topic="🌐 Server Verification System • Click Authenticate via Discord below to enter Glock30")
    await bot_channel.edit(topic="🤖 Use bot commands here")
    await vc_logs_channel.edit(topic="🎤 Voice channel logs")
    await mod_logs_channel.edit(topic="🛡 Moderator actions and security logs")
    await role_logs_channel.edit(topic="📋 Role changes and verification logs")
    await staff_chat_channel.edit(topic="🛡 Staff discussion only")
    await jail_channel.edit(topic="🔒 Restricted custody area")
    await jail_logs_channel.edit(topic="📋 Jail and unjail logs")

    await welcome_channel.set_permissions(unverified_role, view_channel=True, send_messages=False, read_message_history=True, use_application_commands=True)
    await rules_channel.set_permissions(unverified_role, view_channel=True, send_messages=False, read_message_history=True, use_application_commands=True)
    await verify_channel.set_permissions(unverified_role, view_channel=True, send_messages=False, read_message_history=True, use_application_commands=True)

    await general_channel.set_permissions(verified_role, view_channel=True, send_messages=True, read_message_history=True)
    await media_channel.set_permissions(verified_role, view_channel=True, send_messages=True, read_message_history=True)
    await bot_channel.set_permissions(verified_role, view_channel=True, send_messages=True, read_message_history=True)

    await jail_channel.set_permissions(jail_role, view_channel=True, send_messages=True, read_message_history=True)
    await jail_logs_channel.set_permissions(jail_role, view_channel=False)

    embed = discord.Embed(
        title=f"✅ {guild.name} Setup Complete",
        description=(
            "TrapAI setup finished.\n\n"
            "Next steps:\n"
            "1. Move your bot role above the server roles\n"
            "2. Run `,sendverify`\n"
            "3. Run `,rules`\n"
            "4. Run `,sendtickets` in a support channel\n"
            "5. Update your `LOG_CHANNELS` IDs to the new channels"
        ),
        color=discord.Color.dark_grey(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"TrapAI Setup System • {guild.name}")
    await ctx.send(embed=embed)


@bot.command(name="lockunverified", aliases=["fixverification"])
@_permitted_check(administrator=True)
async def lockunverified(ctx):
    """
    Deny view access to the Unverified role on every channel in the
    server except welcome/rules/verify. ,setup only sets this up on the
    3 categories it creates by name — any channel outside those (added
    later, or living under a differently-named/renamed category) never
    gets the deny overwrite at all, so Discord's default lets Unverified
    see it. Safe to re-run anytime after adding new channels.
    Usage: ,lockunverified
    """
    guild = ctx.guild
    unverified_role = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE)
    if not unverified_role:
        await ctx.send(f"❌ Role **{UNVERIFIED_ROLE}** not found — run `,setup` first.", delete_after=10)
        return

    keep_visible_names = {"welcome", "rules", "verify"}

    await ctx.send(f"🔒 Locking down channel visibility for {unverified_role.mention}...")

    updated = 0
    kept = 0
    failed = 0
    for channel in guild.channels:
        if isinstance(channel, discord.CategoryChannel):
            continue
        if channel.name.lower() in keep_visible_names:
            kept += 1
            continue
        if channel.overwrites_for(unverified_role).view_channel is False:
            continue  # already denied — no API call needed
        try:
            await channel.set_permissions(
                unverified_role, view_channel=False,
                reason=f"Lock down Unverified visibility ({ctx.author})"
            )
            updated += 1
        except (discord.Forbidden, discord.HTTPException):
            failed += 1

    embed = discord.Embed(
        title="🔒 Unverified Lockdown Complete",
        description=f"Denied view access on **{updated}** channel(s) for {unverified_role.mention}.",
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="✅ Kept Visible", value=f"{kept} (welcome/rules/verify)", inline=True)
    if failed:
        embed.add_field(name="⚠️ Failed", value=str(failed), inline=True)
    embed.set_footer(text=f"TrapAI • {guild.name}")
    await ctx.send(embed=embed)
    await log(guild, "mod", "Unverified Lockdown Run", None, discord.Color.purple(),
              fields=[
                  ("🛡 Moderator",         f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("🔒 Channels Updated",  str(updated),                                 True),
              ],
              actor=ctx.author)


@bot.command()
@_permitted_check(administrator=True)
async def setupvc(ctx, category_name: str = None):
    """Create the ➕ Create VC trigger channel in this server.
    Optionally pass a category name to place it in: ,setupvc "Glock30 VCs"
    If omitted, it uses the default TEMP_VC_CATEGORY_NAME category."""
    guild = ctx.guild

    # Resolve category
    if category_name:
        category = discord.utils.get(guild.categories, name=category_name)
        if category is None:
            category = await guild.create_category(category_name)
            await ctx.send(f"📁 Created new category **{category_name}**.")
    else:
        category = discord.utils.get(guild.categories, name=TEMP_VC_CATEGORY_NAME)
        if category is None:
            category = await guild.create_category(TEMP_VC_CATEGORY_NAME)

    # Check if trigger channel already exists
    existing = discord.utils.get(guild.voice_channels, name=JOIN_TO_CREATE_CHANNEL_NAME)
    if existing:
        await ctx.send(f"✅ The **{JOIN_TO_CREATE_CHANNEL_NAME}** channel already exists: {existing.mention if hasattr(existing, 'mention') else existing.name}\nMove it to your preferred category if needed.")
        return

    # Create the trigger VC
    trigger_vc = await guild.create_voice_channel(
        name=JOIN_TO_CREATE_CHANNEL_NAME,
        category=category,
        reason=f"Setup VC trigger by {ctx.author}"
    )

    embed = discord.Embed(
        title="✅ Create VC Setup Complete",
        description=(
            f"**{JOIN_TO_CREATE_CHANNEL_NAME}** has been created in **{category.name}**.\n\n"
            "When any member joins that channel:\n"
            "• A private voice channel is created for them\n"
            "• The control panel posts right in that VC's own chat\n"
            "• They get full button controls to manage their VC\n"
            "• It deletes automatically when empty\n\n"
            f"Channel: {trigger_vc.mention if hasattr(trigger_vc, 'mention') else trigger_vc.name}"
        ),
        color=discord.Color.dark_grey(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"TrapAI VC System • {guild.name}")
    await ctx.send(embed=embed)

    await log(
        guild,
        "vc",
        "Create VC Setup",
        f"Administrator: {ctx.author.mention}\nTrigger Channel: {JOIN_TO_CREATE_CHANNEL_NAME}\nCategory: {category.name}",
        discord.Color.green()
    )


@bot.command()
@_permitted_check(administrator=True)
async def setupjail(ctx, channel: discord.TextChannel = None):
    """Set up the jail system.
    Optionally pass a channel to use as the jail channel: ,setupjail #jail
    If omitted, a #jail channel is created in the 🔒 Restricted category."""
    guild = ctx.guild
    await ctx.send("⚙️ Setting up jail system...")

    # ── Ensure jail role exists ──────────────────────────────
    jail_role = discord.utils.get(guild.roles, name=JAIL_ROLE)
    if not jail_role:
        jail_role = await guild.create_role(
            name=JAIL_ROLE,
            color=discord.Color.dark_red(),
            reason=f"setupjail by {ctx.author}"
        )

    # ── Resolve or create Restricted category ────────────────
    restricted_category = discord.utils.get(guild.categories, name="🔒 Restricted")
    if restricted_category is None:
        restricted_category = await guild.create_category("🔒 Restricted")

    # ── Restrict jailed users in every existing category ─────
    for category in guild.categories:
        if category.name == "🔒 Restricted":
            continue
        try:
            await category.set_permissions(jail_role, view_channel=False)
        except discord.HTTPException:
            pass

    # ── Give jail role read access to Restricted category ────
    await restricted_category.set_permissions(
        guild.default_role, view_channel=False
    )
    await restricted_category.set_permissions(
        jail_role, view_channel=True, send_messages=False, read_message_history=True
    )

    # ── Resolve or create the jail text channel ───────────────
    if channel is None:
        channel = discord.utils.get(guild.text_channels, name="jail")
        if channel is None:
            channel = await guild.create_text_channel(
                "jail",
                category=restricted_category,
                topic="🔒 Restricted custody area",
                reason=f"setupjail by {ctx.author}"
            )
    else:
        try:
            await channel.edit(category=restricted_category, topic="🔒 Restricted custody area")
        except discord.HTTPException:
            pass

    # Jail channel: jailed users can read + send; no one else sees it
    await channel.set_permissions(guild.default_role, view_channel=False)
    await channel.set_permissions(jail_role, view_channel=True, send_messages=True, read_message_history=True)

    # ── Optional jail-logs channel ────────────────────────────
    jail_logs = discord.utils.get(guild.text_channels, name="jail-logs")
    if jail_logs is None:
        jail_logs = await guild.create_text_channel(
            "jail-logs",
            category=restricted_category,
            topic="📋 Jail and unjail logs",
            reason=f"setupjail by {ctx.author}"
        )
    await jail_logs.set_permissions(guild.default_role, view_channel=False)
    await jail_logs.set_permissions(jail_role, view_channel=False)

    embed = discord.Embed(
        title="✅ Jail System Setup Complete",
        description=(
            f"**Jail Role:** {jail_role.mention}\n"
            f"**Jail Channel:** {channel.mention}\n"
            f"**Jail Logs:** {jail_logs.mention}\n\n"
            "**What was configured:**\n"
            "• `🔒 Jailed` role created (if missing)\n"
            "• All existing categories hidden from jailed users\n"
            "• `🔒 Restricted` category created (if missing)\n"
            "• `#jail` channel — jailed users can type here only\n"
            "• `#jail-logs` channel — staff-only log feed\n\n"
            "Use `,jail @user <time> [reason]` to jail someone."
        ),
        color=discord.Color.dark_red(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"TrapAI Jail Setup • {guild.name}")
    await ctx.send(embed=embed)

    await log(
        guild,
        "jail",
        "Jail System Setup",
        (
            f"**Setup By:** {ctx.author.mention} (`{ctx.author.id}`)\n"
            f"**Jail Role:** {jail_role.mention}\n"
            f"**Jail Channel:** {channel.mention}"
        ),
        discord.Color.dark_red(),
        actor=ctx.author
    )


@bot.command(name="lockjailed", aliases=["fixjail"])
@_permitted_check(administrator=True)
async def lockjailed(ctx):
    """
    Deny view access to the Jailed role on every channel in the server
    except the jail chat channel (no VCs, no other text channels —
    jail-logs stays hidden too). ,setupjail only denies categories that
    existed at the moment it was run, so any channel added later (or
    living outside a category entirely) never gets that overwrite and
    stays visible to jailed members by default. Safe to re-run anytime
    after adding new channels/VCs. Usage: ,lockjailed
    """
    guild = ctx.guild
    jail_role = discord.utils.get(guild.roles, name=JAIL_ROLE)
    if not jail_role:
        await ctx.send(f"❌ Role **{JAIL_ROLE}** not found — run `,setupjail` first.", delete_after=10)
        return

    keep_visible_names = {"jail"}

    await ctx.send(f"🔒 Locking down channel visibility for {jail_role.mention}...")

    updated = 0
    kept = 0
    failed = 0
    for channel in guild.channels:
        if isinstance(channel, discord.CategoryChannel):
            continue
        if channel.name.lower() in keep_visible_names:
            kept += 1
            continue
        is_voice = isinstance(channel, (discord.VoiceChannel, discord.StageChannel))
        current = channel.overwrites_for(jail_role)
        if current.view_channel is False and (not is_voice or current.connect is False):
            continue  # already denied — no API call needed
        try:
            if is_voice:
                await channel.set_permissions(
                    jail_role, view_channel=False, connect=False,
                    reason=f"Lock down Jailed visibility ({ctx.author})"
                )
            else:
                await channel.set_permissions(
                    jail_role, view_channel=False,
                    reason=f"Lock down Jailed visibility ({ctx.author})"
                )
            updated += 1
        except (discord.Forbidden, discord.HTTPException):
            failed += 1

    embed = discord.Embed(
        title="🔒 Jail Lockdown Complete",
        description=f"Denied view access on **{updated}** channel(s) for {jail_role.mention} (VCs also denied connect).",
        color=discord.Color.dark_red(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="✅ Kept Visible", value=f"{kept} (#jail only)", inline=True)
    if failed:
        embed.add_field(name="⚠️ Failed", value=str(failed), inline=True)
    embed.set_footer(text=f"TrapAI • {guild.name}")
    await ctx.send(embed=embed)
    await log(guild, "jail", "Jail Lockdown Run", None, discord.Color.dark_red(),
              fields=[
                  ("🛡 Moderator",        f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("🔒 Channels Updated", str(updated),                                 True),
              ],
              actor=ctx.author)


@bot.command(name="setlogchannel")
@_permitted_check(administrator=True)
async def setlogchannel(ctx, key: str = None, channel: discord.TextChannel = None):
    """Pin a specific channel for a log key.
    Usage:
      ,setlogchannel              — list all keys and current channels
      ,setlogchannel mod          — show which channel 'mod' logs go to
      ,setlogchannel mod #mod-logs — point 'mod' logs at #mod-logs
      ,setlogchannel mod reset    — clear override, fall back to name lookup"""
    guild = ctx.guild

    # ── No args: list all keys ───────────────────────────────
    if key is None:
        lines = []
        for k, default_name in LOG_CHANNELS.items():
            resolved = _resolve_log_channel(guild, k)
            override_id = LOG_CHANNEL_OVERRIDES.get(guild.id, {}).get(k)
            if override_id:
                status = f"📌 <#{override_id}> *(pinned)*"
            elif resolved:
                status = f"✅ {resolved.mention} *(by name)*"
            else:
                status = f"❌ not found — looking for `#{default_name}`"
            lines.append(f"`{k}` → {status}")
        embed = discord.Embed(
            title="📋 Log Channel Map",
            description="\n".join(lines),
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Use ,setlogchannel <key> #channel to pin • {guild.name}")
        await ctx.send(embed=embed)
        return

    key = key.lower()
    if key not in LOG_CHANNELS:
        valid = ", ".join(f"`{k}`" for k in LOG_CHANNELS)
        await ctx.send(f"❌ Unknown key `{key}`. Valid keys: {valid}", delete_after=15)
        return

    # ── Reset override ───────────────────────────────────────
    if channel is None or (isinstance(channel, str) and channel.lower() == "reset"):
        LOG_CHANNEL_OVERRIDES.setdefault(guild.id, {}).pop(key, None)
        _save_log_channel_overrides()
        resolved = _resolve_log_channel(guild, key)
        if resolved:
            await ctx.send(f"↩️ Override cleared for `{key}`. Now using {resolved.mention} (found by name).")
        else:
            await ctx.send(f"↩️ Override cleared for `{key}`. No channel named `#{LOG_CHANNELS[key]}` found yet.")
        return

    # ── Pin a channel ────────────────────────────────────────
    LOG_CHANNEL_OVERRIDES.setdefault(guild.id, {})[key] = channel.id
    _save_log_channel_overrides()
    embed = discord.Embed(
        title="✅ Log Channel Set",
        description=(
            f"**Key:** `{key}`\n"
            f"**Channel:** {channel.mention}\n\n"
            f"All `{key}` logs will now go to {channel.mention}."
        ),
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"TrapAI Log Config • {guild.name}")
    await ctx.send(embed=embed)


@bot.command()
@_permitted_check(administrator=True)
async def sendverify(ctx):
    """
    Post the verification panel. Uses a genuine Discord OAuth "Authenticate
    via Discord" consent screen (handled by the separately-hosted
    oauth_server.py) if DISCORD_CLIENT_ID and DISCORD_OAUTH_REDIRECT_URI
    are configured for this bot; otherwise falls back to a simple
    in-Discord button that grants the role directly, no external hosting
    required. Usage: ,sendverify
    """
    embed = discord.Embed(
        title="🌐 Server Verification System",
        description=(
            "🔒 **Community Verification**\n"
            "Verify to stay connected with our community.\n\n"
            "If this server is ever deleted, unavailable, or moved, verified members "
            "can receive an invite to our new official server so you don't lose your place.\n\n"
            "🛡️ This **will not** harm your account, change your password, or post/message "
            "anything on your behalf — it only confirms who you are so we can grant your access.\n\n"
            "Click Verify below to continue."
        ),
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )

    if OAUTH_VERIFY_ENABLED:
        await ctx.send(embed=embed, view=OAuthVerifyView(ctx.guild.id))
    else:
        msg = await ctx.send(embed=embed, view=VerifyView())
        # Re-register with the message_id so the persistent view survives bot restarts
        bot.add_view(VerifyView(), message_id=msg.id)


@bot.command()
@_permitted_check(administrator=True)
async def setverifybackup(ctx, guild_id: int = None):
    """
    Set which server verified members get auto-joined to via the real
    OAuth verify flow (requires DISCORD_CLIENT_ID/DISCORD_OAUTH_REDIRECT_URI
    to be configured — see oauth_server.py's docstring). Run with no
    argument to clear it.
    Usage: ,setverifybackup <server_id>
    """
    if guild_id is None:
        VERIFY_BACKUP_GUILD.pop(ctx.guild.id, None)
        _save_verify_backup_guild()
        await ctx.send("↩️ Backup server cleared — verified members will only get the role here now.")
        return
    VERIFY_BACKUP_GUILD[ctx.guild.id] = guild_id
    _save_verify_backup_guild()
    note = "" if OAUTH_VERIFY_ENABLED else (
        "\n⚠️ Real OAuth verification isn't configured yet "
        "(DISCORD_CLIENT_ID/DISCORD_OAUTH_REDIRECT_URI unset), so this won't take effect until it is."
    )
    await ctx.send(f"✅ Verified members here will now also be auto-joined to server `{guild_id}` when they verify.{note}")


@bot.command()
@_permitted_check(administrator=True)
async def sendtickets(ctx):
    """Send the ticket panel to the current channel."""
    categories = _ticket_types_for_guild(ctx.guild.id)
    category_lines = "\n".join(f"**{label}** — {desc}" for label, desc, _color in categories.values())

    embed = discord.Embed(
        title="🎫 TrapAI Support Tickets",
        description=(
            "Need help from staff? Open a **private support ticket**.\n\n"
            "```yaml\n"
            "Ticket System  : ACTIVE\n"
            "Response Time  : As soon as possible\n"
            "Privacy        : Staff + ticket opener only\n"
            f"Categories     : {len(categories)} type(s) available\n"
            "```\n"
            "Use the **dropdown below** to pick a category and open your ticket."
        ),
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(
        name="📂 Ticket Categories",
        value=category_lines[:1024] or "No categories configured.",
        inline=False
    )
    embed.add_field(
        name="📌 Before Opening",
        value=(
            "• Describe your issue clearly\n"
            "• Include screenshots if relevant\n"
            "• One ticket at a time per user"
        ),
        inline=False
    )
    if ctx.guild.icon:
        embed.set_thumbnail(url=ctx.guild.icon.url)
    embed.set_footer(text=f"TrapAI Ticket System • {ctx.guild.name} Support")
    await ctx.send(embed=embed, view=TicketOpenView(ctx.guild.id))

    await log(
        ctx.guild,
        "tickets",
        "Ticket Panel Sent",
        f"**Administrator:** {ctx.author.mention}\n**Channel:** {ctx.channel.mention}",
        discord.Color.blurple()
    )


_TICKET_COLOR_MAP = {
    "red": discord.Color.red(), "green": discord.Color.green(), "blue": discord.Color.blue(),
    "gold": discord.Color.purple(), "purple": discord.Color.purple(), "teal": discord.Color.teal(),
    "orange": discord.Color.orange(), "pink": discord.Color.from_rgb(255, 105, 180),
    "dark_red": discord.Color.dark_red(), "dark_teal": discord.Color.dark_teal(),
    "dark_gold": discord.Color.dark_purple(), "blurple": discord.Color.blurple(),
}


def _parse_ticket_color(raw: str):
    raw = raw.strip().lower()
    if raw in _TICKET_COLOR_MAP:
        return _TICKET_COLOR_MAP[raw]
    try:
        return discord.Color(int(raw.lstrip("#"), 16))
    except ValueError:
        return None


@bot.command()
@_permitted_check(administrator=True)
async def addticketcategory(ctx, key: str, *, rest: str = None):
    """
    Add or update a ticket category scoped to THIS server only — it will
    NOT appear in any other server the bot is in.
    Usage: ,addticketcategory <key> <emoji label> | <description> [| color]
    Example: ,addticketcategory division 🏛️ Division Application | Apply to join a division
    """
    key = key.strip().lower()
    if not rest:
        await ctx.send(
            "❌ Usage: `,addticketcategory <key> <emoji label> | <description> [| color]`\n"
            "Example: `,addticketcategory division 🏛️ Division Application | Apply to join a division`",
            delete_after=15
        )
        return

    parts = [p.strip() for p in rest.split("|")]
    label = parts[0] if parts else None
    description = parts[1] if len(parts) > 1 and parts[1] else "No description provided"
    color = discord.Color.blurple()
    if len(parts) > 2 and parts[2]:
        parsed_color = _parse_ticket_color(parts[2])
        if parsed_color:
            color = parsed_color

    if not label:
        await ctx.send("❌ You need to provide a label. Usage: `,addticketcategory <key> <emoji label> | <description>`", delete_after=10)
        return

    guild_cats = GUILD_TICKET_TYPES.setdefault(ctx.guild.id, {})
    is_update = key in guild_cats or key in DEFAULT_TICKET_TYPES
    guild_cats[key] = (label, description, color)
    _save_guild_ticket_types()

    embed = discord.Embed(
        title="✅ Ticket Category Updated" if is_update else "✅ Ticket Category Added",
        description=(
            f"**Key:** `{key}`\n"
            f"**Label:** {label}\n"
            f"**Description:** {description}\n\n"
            "This category is scoped to **this server only**.\n"
            "Re-run `,sendtickets` here to refresh the panel with it — old panel messages don't update automatically."
        ),
        color=color,
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"Set by {ctx.author} • TrapAI")
    await ctx.send(embed=embed)

    await log(ctx.guild, "tickets", "Ticket Category Added", None, color,
              fields=[("🔑 Key", key, True), ("🏷️ Label", label, True), ("👑 By", ctx.author.mention, True)],
              actor=ctx.author)


@bot.command()
@_permitted_check(administrator=True)
async def setticketformat(ctx, key: str, *, template: str = None):
    """
    Set the application-form text posted automatically when a ticket of
    this category opens (every default category already has one built in
    — general, report, appeal, alliance, bug, unban, staff, court — this
    lets you override any of those, or add one for your own custom
    category). Scoped to THIS server only. Run with no text to clear a
    server's override and fall back to the built-in default, if any.

    Usage: ,setticketformat <key> <format text>
    Example:
      ,setticketformat division **Why do you want to join?**
      Answer here <
    """
    key = key.strip().lower()
    if _get_ticket_type(ctx.guild.id, key) is None:
        await ctx.send(f"❌ No ticket category with key `{key}` exists here. Use `,ticketcategories` to see valid keys.", delete_after=10)
        return

    guild_formats = GUILD_TICKET_FORMATS.setdefault(ctx.guild.id, {})
    if not template:
        had_override = guild_formats.pop(key, None) is not None
        _save_guild_ticket_formats()
        fallback = "the built-in default" if key in DEFAULT_TICKET_FORMATS else "*no form at all*"
        await ctx.send(
            f"↩️ Cleared this server's custom format for `{key}`" + (" (nothing was set)." if not had_override else ".") +
            f" Falls back to {fallback}."
        )
        return

    guild_formats[key] = template
    _save_guild_ticket_formats()
    embed = discord.Embed(
        title="✅ Ticket Format Set",
        description=f"**Key:** `{key}`\n\nWill be posted automatically the next time someone opens this ticket type:\n\n{template[:3500]}",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"Set by {ctx.author} • TrapAI")
    await ctx.send(embed=embed)


@bot.command()
@_permitted_check(administrator=True)
async def removeticketcategory(ctx, key: str):
    """Remove a custom ticket category from THIS server. Usage: ,removeticketcategory <key>"""
    key = key.strip().lower()
    guild_cats = GUILD_TICKET_TYPES.get(ctx.guild.id, {})
    if key not in guild_cats:
        if key in DEFAULT_TICKET_TYPES:
            await ctx.send(f"❌ `{key}` is a built-in default category and can't be removed here (only custom ones added via `,addticketcategory`).", delete_after=10)
        else:
            await ctx.send(f"❌ No custom category found with key `{key}`. Use `,ticketcategories` to see what's active.", delete_after=10)
        return
    removed_label = guild_cats.pop(key)[0]
    _save_guild_ticket_types()
    await ctx.send(f"✅ Removed ticket category **{removed_label}** (`{key}`) from this server. Re-run `,sendtickets` to refresh the panel.")
    await log(ctx.guild, "tickets", "Ticket Category Removed", None, discord.Color.orange(),
              fields=[("🔑 Key", key, True), ("👑 By", ctx.author.mention, True)],
              actor=ctx.author)


@bot.command()
async def ticketcategories(ctx):
    """List all ticket categories active in this server. Usage: ,ticketcategories"""
    categories = _ticket_types_for_guild(ctx.guild.id)
    guild_custom = GUILD_TICKET_TYPES.get(ctx.guild.id, {})
    defaults = [k for k in categories if k not in guild_custom]
    custom = [k for k in categories if k in guild_custom]

    embed = discord.Embed(
        title="📂 Ticket Categories",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    if defaults:
        embed.add_field(
            name=f"🌐 Default — available in every server ({len(defaults)})",
            value="\n".join(f"`{k}` — {categories[k][0]}" for k in defaults),
            inline=False
        )
    if custom:
        embed.add_field(
            name=f"🏷️ Custom to this server ({len(custom)})",
            value="\n".join(f"`{k}` — {categories[k][0]}" for k in custom),
            inline=False
        )
    embed.add_field(
        name="⚙️ Commands",
        value=(
            "`,addticketcategory <key> <emoji label> | <description>` — add/update (admin)\n"
            "`,removeticketcategory <key>` — remove a custom one (admin)"
        ),
        inline=False
    )
    embed.set_footer(text=f"TrapAI • {ctx.guild.name}")
    await ctx.send(embed=embed)


# ── Ticket management commands ──────────────────────────────

@bot.command(name="claimticket")
@_permitted_check(manage_messages=True)
async def claimticket(ctx):
    """Claim the current ticket channel. Usage: ,claimticket"""
    channel = ctx.channel

    # Verify this channel is a known ticket
    is_ticket = any(cid == channel.id for cid in TICKETS.get(ctx.guild.id, {}).values())
    if not is_ticket:
        await ctx.send("❌ This channel is not a ticket.", delete_after=8)
        return

    already = TICKET_CLAIMED.get(channel.id)
    if already:
        member = ctx.guild.get_member(already)
        name = member.mention if member else f"<@{already}>"
        await ctx.send(f"❌ Ticket already claimed by {name}.", delete_after=8)
        return

    TICKET_CLAIMED[channel.id] = ctx.author.id
    _save_tickets()
    _record_ticket_claim(ctx.guild.id, ctx.author.id)
    await _update_ticket_header_claim(channel, claimed_by=ctx.author)

    embed = discord.Embed(
        title="🙋 Ticket Claimed",
        description=f"{ctx.author.mention} has claimed this ticket and will assist you.",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text="TrapAI Ticket System")
    await ctx.send(embed=embed)

    await log(
        ctx.guild,
        "tickets",
        "Ticket Claimed",
        (
            f"**Channel:** {channel.mention}\n"
            f"**Claimed By:** {ctx.author.mention} (`{ctx.author.id}`)\n"
            f"**Type:** {TICKET_TYPE.get(channel.id, 'General')}"
        ),
        discord.Color.green()
    )


@bot.command(name="closeticket")
@_permitted_check(manage_messages=True)
async def closeticket(ctx):
    """Close and delete the current ticket channel. Usage: ,closeticket"""
    channel = ctx.channel
    guild = ctx.guild

    # Find ticket owner
    owner_id = None
    for uid, cid in list(TICKETS.get(guild.id, {}).items()):
        if cid == channel.id:
            owner_id = uid
            break

    if owner_id is None:
        await ctx.send("❌ This channel is not a ticket.", delete_after=8)
        return

    ticket_type = TICKET_TYPE.get(channel.id, "General")
    claimer_id = TICKET_CLAIMED.get(channel.id)
    claimer = guild.get_member(claimer_id) if claimer_id else None

    embed = discord.Embed(
        title="🔒 Closing Ticket",
        description=(
            f"This ticket will be **deleted in 5 seconds**.\n\n"
            f"**Closed by:** {ctx.author.mention}\n"
            f"**Claimed by:** {claimer.mention if claimer else 'Unclaimed'}\n"
            f"**Type:** {ticket_type}"
        ),
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text="TrapAI Ticket System")
    await ctx.send(embed=embed)

    await log(
        guild,
        "tickets",
        "Ticket Closed",
        (
            f"**Channel:** `{channel.name}`\n"
            f"**Closed By:** {ctx.author.mention} (`{ctx.author.id}`)\n"
            f"**Ticket Owner:** {'<@' + str(owner_id) + '>' if owner_id else 'Unknown'}\n"
            f"**Claimed By:** {claimer.mention if claimer else 'Never claimed'}\n"
            f"**Type:** {ticket_type}"
        ),
        discord.Color.red()
    )

    await _send_closed_ticket_transcript(guild, channel, ctx.author, owner_id, claimer, ticket_type)

    await asyncio.sleep(5)

    TICKETS[guild.id].pop(owner_id, None)
    TICKET_CLAIMED.pop(channel.id, None)
    TICKET_TYPE.pop(channel.id, None)
    TICKET_PRIORITY.pop(channel.id, None)
    TICKET_LOCKED.pop(channel.id, None)
    _save_tickets()

    try:
        await channel.delete(reason=f"Ticket closed by {ctx.author}")
    except discord.HTTPException:
        pass


# ============================================================
# BILLING — Stripe-backed subscription/lifetime purchase, talks to
# oauth_server.py's /internal/* endpoints (see that file's BILLING section)
# ============================================================

_WHOLE_BOT_TIER_NOTES = {
    "regular": "The commands most servers use day to day — moderation, roles, verification, tickets, and more.",
    "premium": "Everything in Regular, plus the rest: economy/games, giveaways & polls, vouch, jail/anti-raid, temp VCs, birthdays, boost perks, staff tools, and advanced mod tools (hardban, nuke, lockdown, mass-role, backups).",
}


def _pricing_embed() -> discord.Embed:
    web_note = (
        f"\n\n🌐 **[Subscribe on the web]({BILLING_API_URL}/checkout)** — no need to have "
        "the bot in your server first. Pick a plan, pay, then invite TrapAI. Share that "
        "link with anyone who doesn't have the bot yet."
        if BILLING_API_URL else ""
    )
    embed = discord.Embed(
        title="💳 TrapAI Pricing",
        description=(
            "Choose **Ticket Bot** (just the ticket/support system) or **Whole Bot** "
            "(one-time purchase, no subscription — Regular covers everyday commands, Premium unlocks everything else).\n"
            "Then run `,subscribe <product> <tier>` (e.g. `,subscribe ticketbot pro` or `,subscribe wholebot premium`)."
            f"{web_note}"
        ),
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    for product, product_cfg in BILLING_PRODUCTS.items():
        lines = []
        for tier, cfg in product_cfg["tiers"].items():
            star = "⭐ " if cfg["best_value"] else ""
            best = " — Best Value" if cfg["best_value"] else ""
            line = f"{star}**{cfg['label']}{best}** — {cfg['price']}\n`,subscribe {product} {tier}`"
            note = _WHOLE_BOT_TIER_NOTES.get(tier) if product == "whole_bot" else None
            if note:
                line += f"\n*{note}*"
            lines.append(line)
        embed.add_field(name=f"🎫 {product_cfg['label']}" if product == "ticket_bot" else f"🤖 {product_cfg['label']}",
                         value="\n".join(lines), inline=False)
    embed.set_footer(text="Payments are handled entirely by Stripe — TrapAI never sees your card details.")
    return embed


@bot.command()
async def subscribe(ctx, product: str = None, tier: str = None):
    """
    Show TrapAI's pricing, or subscribe/purchase a plan. Usage:
      ,subscribe                    — show pricing for both products
      ,subscribe <product> <tier>   — get a secure checkout link
                                       products: ticketbot, wholebot
                                       ticketbot tiers: starter/pro/premium/lifetime
                                       wholebot tiers: regular/premium
    Don't have the bot in your server yet? ,subscribe shows a direct web
    checkout link too — no bot required until after you've paid.
    """
    if product is None:
        await ctx.send(embed=_pricing_embed())
        return

    resolved_product = _resolve_product(product)
    if resolved_product is None:
        await ctx.send(f"❌ Unknown product `{product}`. Valid products: `ticketbot`, `wholebot`", delete_after=10)
        return

    valid_tiers = BILLING_PRODUCTS[resolved_product]["tiers"]
    if tier is None:
        tier_list = ", ".join(valid_tiers)
        await ctx.send(f"❌ Also pick a tier: `,subscribe {product} <tier>` — valid tiers for this product: `{tier_list}`", delete_after=12)
        return

    tier = tier.lower()
    if tier not in valid_tiers:
        await ctx.send(f"❌ Unknown tier `{tier}` for `{resolved_product}`. Valid tiers: {', '.join(valid_tiers)}", delete_after=10)
        return

    if not BILLING_CONFIGURED:
        await ctx.send("❌ Billing isn't configured on this bot yet — ask whoever runs it to finish setting up Stripe.", delete_after=12)
        return

    data, error = await _billing_api_post("/internal/checkout-link", {
        "guild_id": ctx.guild.id, "discord_user_id": ctx.author.id, "product": resolved_product, "tier": tier,
    })
    if error:
        await ctx.send(f"❌ Couldn't start checkout: {error}", delete_after=12)
        return

    checkout_url = data["url"]
    product_label = BILLING_PRODUCTS[resolved_product]["label"]
    tier_label = valid_tiers[tier]["label"]
    embed = discord.Embed(
        title=f"💳 Checkout — {product_label} ({tier_label})",
        description=f"Click below to complete your **{product_label} — {tier_label}** purchase securely via Stripe.\n\n[Complete Checkout]({checkout_url})",
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text="TrapAI never sees your card details — Stripe handles all payment info.")

    try:
        await ctx.author.send(embed=embed)
        await ctx.send(f"📨 {ctx.author.mention} Check your DMs for your secure checkout link.")
    except (discord.Forbidden, discord.HTTPException):
        await ctx.send(embed=embed)  # DMs closed — post it here instead


@bot.command()
async def managesubscription(ctx):
    """DM you a link to Stripe's customer portal — manage billing details, change plans, or cancel. Usage: ,managesubscription"""
    if not BILLING_CONFIGURED:
        await ctx.send("❌ Billing isn't configured on this bot yet.", delete_after=10)
        return

    data, error = await _billing_api_post("/internal/portal-link", {"guild_id": ctx.guild.id})
    if error:
        await ctx.send(f"❌ {error}", delete_after=12)
        return

    portal_url = data["url"]
    embed = discord.Embed(
        title="💳 Manage Your Subscription",
        description=f"[Open the billing portal]({portal_url}) to update your payment method, change plans, or cancel.",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    try:
        await ctx.author.send(embed=embed)
        await ctx.send(f"📨 {ctx.author.mention} Check your DMs for your billing portal link.")
    except (discord.Forbidden, discord.HTTPException):
        await ctx.send(embed=embed)


@bot.command()
async def subscriptionstatus(ctx):
    """Show this server's current TrapAI subscription status. Usage: ,subscriptionstatus"""
    if not BILLING_CONFIGURED:
        await ctx.send("❌ Billing isn't configured on this bot yet.", delete_after=10)
        return

    data = await _get_subscription_status(ctx.guild.id)
    status = data.get("status", "none")
    product = data.get("product")
    tier = data.get("tier")

    status_display = {
        "active":    ("🟢", "Active"),
        "lifetime":  ("💎", "Lifetime — never expires"),
        "past_due":  ("🟡", "Payment failed — in grace period"),
        "suspended": ("🔴", "Suspended — ticket system disabled"),
        "canceled":  ("⚫", "Canceled"),
        "none":      ("⚪", "No subscription on file"),
    }.get(status, ("⚪", status))

    embed = discord.Embed(
        title="💳 Subscription Status",
        color=discord.Color.green() if status in ("active", "lifetime") else discord.Color.orange(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="Status", value=f"{status_display[0]} {status_display[1]}", inline=True)
    if product:
        embed.add_field(name="Product", value=BILLING_PRODUCTS.get(product, {}).get("label", product), inline=True)
    if tier:
        tier_label = BILLING_PRODUCTS.get(product, {}).get("tiers", {}).get(tier, {}).get("label", tier)
        embed.add_field(name="Plan", value=tier_label, inline=True)

    grace_end = data.get("grace_period_ends_at")
    if status == "past_due" and grace_end:
        embed.add_field(name="⏳ Grace period ends", value=discord.utils.format_dt(discord.utils.utcnow().fromtimestamp(grace_end), "R"), inline=False)

    period_end = data.get("current_period_end")
    if status == "active" and period_end:
        embed.add_field(name="🔄 Renews", value=discord.utils.format_dt(discord.utils.utcnow().fromtimestamp(period_end), "R"), inline=False)

    if status in (None, "none", "canceled", "suspended"):
        embed.add_field(name="Get started", value="Run `,subscribe` to see pricing.", inline=False)
    else:
        embed.add_field(name="Manage", value="Run `,managesubscription` to update billing or cancel.", inline=False)

    embed.set_footer(text=f"TrapAI • {ctx.guild.name}")
    await ctx.send(embed=embed)


@bot.command()
@_permitted_check(administrator=True)
async def rules(ctx):
    embed = discord.Embed(
        title="🤖 TrapAI Server Rules",
        description=(
            f"Welcome to **{ctx.guild.name}** 🏘️🔥\n\n"
            f"To stay in **{ctx.guild.name}**, all members must follow the rules below.\n\n"
            "```yaml\n"
            "TrapAI Status: ACTIVE\n"
            "Rule Enforcement: ENABLED\n"
            "Violation Response: WARNING / TIMEOUT / STRIP / JAIL / BAN / HARDBAN\n"
            "```"
        ),
        color=discord.Color.dark_grey(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="1️⃣ Respect Everyone", value="No harassment, racism, hate speech, threats, or bullying.", inline=False)
    embed.add_field(name="2️⃣ No Spamming", value="Do not flood chats, mass mention, or spam messages, emojis, or reactions. **Punishment: Instant Timeout.**", inline=False)
    embed.add_field(name="3️⃣ No Ads or Links", value="No self-promo, invite links, or outside advertising without staff approval.", inline=False)
    embed.add_field(name="4️⃣ Keep It Clean", value="No harmful content, scams, or anything meant to harm the server.", inline=False)
    embed.add_field(name="5️⃣ Use Channels Correctly", value="Keep topics in the right channels and follow staff directions.", inline=False)
    embed.add_field(name="6️⃣ VC Rules", value="No mic spam, earrape, screaming, or trolling in voice channels.", inline=False)
    embed.add_field(name="7️⃣ No Impersonation", value="Do not impersonate other members, staff, or bots. Violations may result in an immediate ban.", inline=False)
    embed.add_field(name="8️⃣ Staff Decisions", value="Arguing with moderation actions in public may lead to more punishment. Contact staff calmly.", inline=False)
    embed.add_field(name="9️⃣ Respect The Server", value="No disrespecting, trash-talking, or badmouthing this server — including telling others to leave or spreading negativity about it.", inline=False)
    embed.add_field(name="🔟 Respect The Staff", value="Disrespecting staff, their decisions, or the team as a whole will not be tolerated. Take issues to the proper channels calmly.", inline=False)
    embed.add_field(name="🗣️ No Spreading Rumors", value="Do not spread rumors, gossip, or false information about members or staff. Rumors damage people's reputations, start unnecessary drama, and break down trust in this community — if you have a real concern, bring it to staff privately instead of spreading it around.", inline=False)
    embed.add_field(name="🔞 Age Requirement", value="You must be at least 13 years old to be in this server, per Discord's own Terms of Service.", inline=False)
    embed.add_field(name="👤 No Alts / Ban Evasion", value="Using an alt account to get around a ban, mute, timeout, or jail is not allowed. **Punishment: Alt + main account both hardbanned.**", inline=False)
    embed.add_field(name="🙏 No Begging", value="Do not beg staff or members for roles, ranks, boosts, Nitro, or anything else.", inline=False)
    embed.add_field(name="⛔ No NSFW / Nudity", value="No NSFW, nudity, or explicit content of any kind. **Punishment: Instant Ban.**", inline=False)
    embed.add_field(name="⛔ No Gore", value="No gore, graphic violence, or disturbing content of any kind. **Punishment: Instant Ban.**", inline=False)
    embed.add_field(name="⛔ No Staff/Admin Abuse", value="Abusing admin or staff permissions in any way will not be tolerated. **Punishment: Instant Strip + Jail.**", inline=False)
    embed.add_field(name="⛔ No Doxxing", value="Attempting to dox any member will not be tolerated. **Punishment: Instant Hardban + reported to Discord.**", inline=False)
    embed.add_field(
        name="⚠ TrapAI Enforcement",
        value="Breaking rules may result in:\n• Warning\n• Timeout\n• Strip\n• Jail\n• Ban\n• Hardban",
        inline=False
    )
    embed.add_field(
        name="🚨 Final Warning",
        value="If you can't follow any of these rules, that's an **instant ban.**",
        inline=False
    )
    embed.add_field(
        name="📜 Discord Community Guidelines",
        value="[discord.com/guidelines](https://discord.com/guidelines)",
        inline=False
    )
    if ctx.guild.icon:
        embed.set_thumbnail(url=ctx.guild.icon.url)
    embed.set_footer(text=f"TrapAI Security • {ctx.guild.name} Rules")

    await ctx.send(embed=embed)
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass
    await log(
        ctx.guild,
        "mod",
        "TrapAI Rules Sent",
        f"Administrator: {ctx.author.mention}\nChannel: {ctx.channel.mention}",
        discord.Color.blurple()
    )


# ============================================================
# VERIFICATION COMMANDS
# ============================================================
@bot.command()
@_permitted_check(manage_roles=True)
async def verify(ctx, member: discord.Member):
    unverified_role = discord.utils.get(ctx.guild.roles, name=UNVERIFIED_ROLE)
    verified_role = discord.utils.get(ctx.guild.roles, name=VERIFIED_ROLE)
    jail_role = discord.utils.get(ctx.guild.roles, name=JAIL_ROLE)

    if jail_role and jail_role in member.roles:
        await ctx.send("❌ That user is jailed and cannot be verified.")
        return
    if not verified_role:
        await ctx.send(f"❌ Role **{VERIFIED_ROLE}** was not found.")
        return

    try:
        if unverified_role and unverified_role in member.roles:
            await member.remove_roles(unverified_role, reason=f"Verified by {ctx.author}")
        if verified_role not in member.roles:
            await member.add_roles(verified_role, reason=f"Verified by {ctx.author}")

        embed = discord.Embed(
            title="✅ Member Verified",
            description=f"{member.mention} is now a **Glock30 Member** 🏘️🔥",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Verified by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        await log(ctx.guild, "verification", "Member Verified", None, discord.Color.green(),
                  fields=[("🛡 Staff", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("✅ User", f"{member.mention} (`{member.id}`)", True)],
                  actor=ctx.author, target=member)

    except discord.Forbidden:
        await ctx.send(_role_forbidden_reason(ctx.guild))
    except discord.HTTPException:
        await ctx.send("❌ Something went wrong while verifying that member.")


@bot.command()
@_permitted_check(manage_roles=True)
async def unverify(ctx, member: discord.Member):
    unverified_role = discord.utils.get(ctx.guild.roles, name=UNVERIFIED_ROLE)
    verified_role = discord.utils.get(ctx.guild.roles, name=VERIFIED_ROLE)

    if not unverified_role:
        await ctx.send(f"❌ Role **{UNVERIFIED_ROLE}** was not found.")
        return

    try:
        if verified_role and verified_role in member.roles:
            await member.remove_roles(verified_role, reason=f"Unverified by {ctx.author}")
        if unverified_role not in member.roles:
            await member.add_roles(unverified_role, reason=f"Unverified by {ctx.author}")

        embed = discord.Embed(
            title="🚫 Member Unverified",
            description=f"{member.mention} has been moved back to {unverified_role.mention}.",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Unverified by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        await log(ctx.guild, "verification", "Member Unverified", None, discord.Color.orange(),
                  fields=[("🛡 Staff", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("🚫 User", f"{member.mention} (`{member.id}`)", True)],
                  actor=ctx.author, target=member)

    except discord.Forbidden:
        await ctx.send(_role_forbidden_reason(ctx.guild))
    except discord.HTTPException:
        await ctx.send("❌ Something went wrong while unverifying that member.")


@bot.command()
@_permitted_check(manage_roles=True)
async def denyverify(ctx, member: discord.Member, *, reason="Verification denied by staff"):
    unverified_role = discord.utils.get(ctx.guild.roles, name=UNVERIFIED_ROLE)
    verified_role = discord.utils.get(ctx.guild.roles, name=VERIFIED_ROLE)

    try:
        if verified_role and verified_role in member.roles:
            await member.remove_roles(verified_role, reason=reason)
        if unverified_role and unverified_role not in member.roles:
            await member.add_roles(unverified_role, reason=reason)

        embed = discord.Embed(
            title="🚫 TrapAI Verification Denied",
            description=f"{member.mention} has been moved to restricted access.\n\n**Reason:** {reason}",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Action by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        await log(ctx.guild, "verification", "TrapAI Verification Denied", None, discord.Color.red(),
                  fields=[("🛡 Staff", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("🚫 User", f"{member.mention} (`{member.id}`)", True), ("📝 Reason", reason, False)],
                  actor=ctx.author, target=member)

    except discord.Forbidden:
        await ctx.send(_role_forbidden_reason(ctx.guild))
    except discord.HTTPException:
        await ctx.send("❌ Something went wrong while denying verification.")


# ============================================================
# SECURITY COMMANDS
# ============================================================
@bot.command()
@_permitted_check(manage_messages=True)
async def trapwarn(ctx, member: discord.Member, *, reason="Suspicious activity detected"):
    embed = discord.Embed(
        title="⚠ TrapAI Security Warning",
        description=(
            f"{member.mention}, TrapAI has detected suspicious activity.\n\n"
            f"**Reason:** {reason}\n\n"
            "Further violations may result in timeout, jail, or removal from the island."
        ),
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"Issued by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)
    await log(ctx.guild, "mod", "TrapAI Warning Issued", None, discord.Color.orange(),
              fields=[("🛡 Staff", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("⚠️ User", f"{member.mention} (`{member.id}`)", True), ("📝 Reason", reason, False)],
              actor=ctx.author, target=member)


@bot.command()
@_permitted_check(manage_messages=True)
async def trapscan(ctx, member: discord.Member):
    # ── Scanning animation ───────────────────────────────────
    scanning_embed = discord.Embed(
        title="🔍 TrapAI Live Scan",
        description=(
            f"Scanning {member.mention}…\n\n"
            "```yaml\n"
            "Status        : INITIALIZING\n"
            "Identity Check: Running...\n"
            "Threat Model  : Loading...\n"
            "```"
        ),
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    scanning_embed.set_footer(text="TrapAI Security • Scanning…")
    msg = await ctx.send(embed=scanning_embed)
    await asyncio.sleep(2)

    # ── Gather intel ─────────────────────────────────────────
    now = discord.utils.utcnow()
    account_age_days = (now - member.created_at).days
    joined_days_ago  = (now - member.joined_at).days if member.joined_at else None

    # Public flags
    flags = member.public_flags
    is_bot_account   = member.bot
    is_verified_bot  = flags.verified_bot
    is_system        = member.system if hasattr(member, "system") else False

    # Avatar / default avatar
    has_default_avatar = member.default_avatar == member.avatar or member.avatar is None

    # Role checks
    verified_role   = discord.utils.get(ctx.guild.roles, name=VERIFIED_ROLE)
    unverified_role = discord.utils.get(ctx.guild.roles, name=UNVERIFIED_ROLE)
    jail_role       = discord.utils.get(ctx.guild.roles, name=JAIL_ROLE)
    muted_role      = discord.utils.get(ctx.guild.roles, name=MUTED_ROLE)

    is_verified   = verified_role   and verified_role   in member.roles
    is_unverified = unverified_role and unverified_role in member.roles
    is_jailed     = jail_role       and jail_role       in member.roles
    is_muted      = muted_role      and muted_role      in member.roles

    # Warning count for this guild
    guild_warns = WARNINGS.get(ctx.guild.id, {}).get(member.id, [])
    warn_count  = len(guild_warns)

    # ── Threat scoring ───────────────────────────────────────
    # Each flag adds points; final score maps to a rating
    threat_points = 0
    flags_hit = []

    if is_bot_account:
        threat_points += 40
        flags_hit.append("🤖 Registered bot account")
    if is_system:
        threat_points += 50
        flags_hit.append("⚙️ Discord system account")
    if account_age_days < 7:
        threat_points += 35
        flags_hit.append(f"🆕 Account only {account_age_days}d old (< 7 days)")
    elif account_age_days < 30:
        threat_points += 15
        flags_hit.append(f"🆕 Account only {account_age_days}d old (< 30 days)")
    if has_default_avatar:
        threat_points += 10
        flags_hit.append("🪪 No profile picture (default avatar)")
    if is_jailed:
        threat_points += 20
        flags_hit.append("🔒 Currently jailed in this server")
    if is_unverified and not is_verified:
        threat_points += 5
        flags_hit.append("🚫 Not yet verified in this server")
    if warn_count >= 3:
        threat_points += 20
        flags_hit.append(f"⚠️ {warn_count} warnings on record")
    elif warn_count >= 1:
        threat_points += 10
        flags_hit.append(f"⚠️ {warn_count} warning(s) on record")
    if joined_days_ago is not None and joined_days_ago < 1:
        threat_points += 10
        flags_hit.append("⏱️ Joined less than 24 hours ago")

    # Map score → rating
    if threat_points >= 60:
        threat_label  = "🔴 CRITICAL"
        threat_color  = discord.Color.red()
        verdict       = "HIGH RISK — Immediate review recommended."
    elif threat_points >= 35:
        threat_label  = "🟠 HIGH"
        threat_color  = discord.Color.orange()
        verdict       = "ELEVATED RISK — Monitor closely."
    elif threat_points >= 15:
        threat_label  = "🟡 MEDIUM"
        threat_color  = discord.Color.purple()
        verdict       = "MODERATE RISK — Some flags detected."
    else:
        threat_label  = "🟢 LOW"
        threat_color  = discord.Color.green()
        verdict       = "CLEAR — No significant threats detected."

    flags_str = "\n".join(flags_hit) if flags_hit else "✅ No flags raised"

    # ── Build result embed ────────────────────────────────────
    result_embed = discord.Embed(
        title="🤖 TrapAI Scan Complete",
        description=f"Scan finished for {member.mention}",
        color=threat_color,
        timestamp=now
    )
    result_embed.set_thumbnail(url=member.display_avatar.url)

    # Identity block
    account_type = (
        "⚙️ Discord System"  if is_system else
        "✅ Verified Bot"    if is_verified_bot else
        "🤖 Bot Account"     if is_bot_account else
        "👤 Human User"
    )
    result_embed.add_field(
        name="🪪 Identity",
        value=(
            f"```yaml\n"
            f"Username : {member}\n"
            f"User ID  : {member.id}\n"
            f"Type     : {account_type.replace('`','')}\n"
            f"```"
        ),
        inline=False
    )

    # Account age block
    result_embed.add_field(
        name="📅 Account Age",
        value=(
            f"```yaml\n"
            f"Created  : {member.created_at.strftime('%Y-%m-%d')}\n"
            f"Age      : {account_age_days} days\n"
            f"Joined   : {member.joined_at.strftime('%Y-%m-%d') if member.joined_at else 'Unknown'}\n"
            f"Days Ago : {joined_days_ago if joined_days_ago is not None else 'Unknown'}\n"
            f"```"
        ),
        inline=False
    )

    # Server status block
    result_embed.add_field(
        name="🛡 Server Status",
        value=(
            f"```yaml\n"
            f"Verified   : {'YES' if is_verified else 'NO'}\n"
            f"Jailed     : {'YES' if is_jailed else 'NO'}\n"
            f"Muted      : {'YES' if is_muted else 'NO'}\n"
            f"Warnings   : {warn_count}\n"
            f"```"
        ),
        inline=True
    )

    # Threat block
    result_embed.add_field(
        name="🚨 Threat Assessment",
        value=(
            f"```yaml\n"
            f"Score    : {threat_points} pts\n"
            f"Rating   : {threat_label.split(' ', 1)[1]}\n"
            f"Verdict  : {verdict}\n"
            f"```"
        ),
        inline=True
    )

    # Flags raised
    result_embed.add_field(name=f"{threat_label} — Flags Raised", value=flags_str, inline=False)

    result_embed.set_footer(
        text=f"TrapAI Security • Scanned by {ctx.author}",
        icon_url=ctx.author.display_avatar.url
    )

    await msg.edit(embed=result_embed)

    await log(
        ctx.guild,
        "mod",
        "TrapAI Scan",
        f"**Target:** {member.mention} (`{member.id}`)\n**Rating:** {threat_label}\n**Verdict:** {verdict}",
        threat_color,
        fields=[
            ("🛡 Scanned By", f"{ctx.author.mention} (`{ctx.author.id}`)", True),
            ("🚨 Threat Score", f"{threat_points} pts", True),
            ("🚩 Flags", flags_str[:1024], False),
        ],
        actor=ctx.author,
        target=member
    )


# ============================================================
# JAIL COMMANDS
# ============================================================
@bot.command()
@_permitted_check(manage_roles=True)
async def jail(ctx, member: discord.Member, duration: str, *, reason="No reason provided"):
    if member == ctx.author:
        await ctx.send("❌ You can't jail yourself.")
        return
    if member == ctx.guild.owner:
        await ctx.send("❌ You can't jail the server owner.")
        return
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        await ctx.send("❌ You can't jail someone with the same or higher role than you.")
        return
    if member.top_role >= ctx.guild.me.top_role:
        await ctx.send("❌ I can't jail that user because their role is higher than mine.")
        return

    seconds = parse_jail_duration(duration)
    if seconds is None:
        await ctx.send("❌ Invalid time format. Use: `30s`, `10m`, `2h`, `3d`, `1w`, `1mo`, `1y`")
        return

    jail_role = discord.utils.get(ctx.guild.roles, name=JAIL_ROLE)

    if not jail_role:
        await ctx.send(f"❌ Role **{JAIL_ROLE}** was not found.")
        return

    try:
        already_jailed = jail_role in member.roles

        if not already_jailed:
            # Snapshot every role they currently hold (except @everyone and any
            # role above my own) so it can all be restored automatically on release.
            other_roles = [
                r for r in member.roles
                if r.name != "@everyone" and r != jail_role and r < ctx.guild.me.top_role
            ]
            JAIL_ROLE_SNAPSHOTS.setdefault(ctx.guild.id, {})[member.id] = [r.id for r in other_roles]
            _save_jail_role_snapshots()

            # Managed roles (Server Booster, bot roles, linked integrations —
            # Twitch/YouTube subs, etc.) can NEVER be manually added or
            # removed via the API, regardless of hierarchy or permissions —
            # Discord always 403s on that, independent of everything else.
            # Excluded here so a boosted/otherwise-integrated member doesn't
            # crash the whole jail with a Forbidden that has nothing to do
            # with role position or the Manage Roles permission. They keep
            # that one role; everything else still gets stripped.
            removable_roles = [r for r in other_roles if not r.managed]
            if removable_roles:
                await member.remove_roles(*removable_roles, reason=f"Jailed by {ctx.author} — roles stripped")
            await member.add_roles(jail_role, reason=f"Jailed by {ctx.author} | {reason}")

        await _apply_jail_overwrites(member)

        old_task = jailed_users.get((ctx.guild.id, member.id))
        if old_task:
            old_task.cancel()

        jailed_users[(ctx.guild.id, member.id)] = asyncio.create_task(
            auto_unjail(ctx.guild.id, member.id, seconds, reason)
        )

        inmate_number = _get_or_assign_inmate_number(ctx.guild.id, member.id)
        cell_number = random.randint(1, 99)

        JAIL_EXPIRY.setdefault(ctx.guild.id, {})[member.id] = {
            "expires_at": time.time() + seconds,
            "reason": reason,
            "cell_number": cell_number,
        }
        _save_jail_expiry()
        _log_mod_action(ctx.guild.id, member.id, "jail", ctx.author, reason, extra=f"Duration: {duration}")

        # Fire the DM in the background — no need to block the confirmation
        # embed below on it finishing.
        asyncio.create_task(_dm_action(member, ctx.guild, "jail", ctx.author, reason,
                                        extra=(
                                            f"Duration: **{duration}**\n"
                                            f"👤 Inmate #{inmate_number} • 🚪 Cell Block #{cell_number}\n\n"
                                            "Run `,worktime` in #jail to answer a quick question and "
                                            "shave time off your sentence."
                                        )))

        roles_note = "All other roles stripped — will restore automatically on release." if not already_jailed else "Timer updated — roles unchanged (already jailed)."
        embed = discord.Embed(
            title="🔒 TrapAI Restriction Applied",
            description=(
                f"{member.mention} has been placed into **restricted custody**.\n\n"
                "```yaml\n"
                f"Duration: {duration}\n"
                f"Reason: {reason}\n"
                f"Moderator: {ctx.author}\n"
                "Status: JAILED\n"
                f"Inmate #: {inmate_number}\n"
                f"Cell Block #: {cell_number}\n"
                "```\n"
                f"🏷️ {roles_note}\n"
                "🙈 All channels hidden except #jail.\n"
                "🧮 They can run `,worktime` in #jail to reduce their sentence."
            ),
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="TrapAI Enforcement • Restriction Active")
        await ctx.send(embed=embed)
        await log(ctx.guild, "jail", "Member Jailed", None, discord.Color.red(),
                  fields=[
                      ("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                      ("🔒 User",      f"{member.mention} (`{member.id}`)",          True),
                      ("⏱️ Duration",  duration,                                      True),
                      ("👤 Inmate #",  str(inmate_number),                            True),
                      ("🚪 Cell #",    str(cell_number),                              True),
                      ("📝 Reason",    reason,                                         False),
                      ("🏷️ Roles",     "Stripped (will auto-restore)" if not already_jailed else "Unchanged", True),
                      ("📨 DM Sent",   "✅ Notified via DM",                           True),
                  ],
                  actor=ctx.author, target=member)

    except discord.Forbidden:
        await ctx.send(_role_forbidden_reason(ctx.guild))
    except discord.HTTPException:
        await ctx.send("❌ Something went wrong while jailing that member.")


@bot.command()
@_permitted_check(manage_roles=True)
async def unjail(ctx, member: discord.Member, *, reason="No reason provided"):
    jail_role = discord.utils.get(ctx.guild.roles, name=JAIL_ROLE)

    if not jail_role:
        await ctx.send(f"❌ Role **{JAIL_ROLE}** was not found.")
        return
    if jail_role not in member.roles:
        await ctx.send("❌ That user is not jailed.")
        return

    try:
        await _release_from_jail(ctx.guild, member, reason, moderator=ctx.author)
        _log_mod_action(ctx.guild.id, member.id, "unjail", ctx.author, reason)

        embed = discord.Embed(
            title="🔓 TrapAI Restriction Removed",
            description=(
                f"{member.mention} has been released from restricted custody.\n\n"
                "```yaml\n"
                f"Reason: {reason}\n"
                f"Moderator: {ctx.author}\n"
                "Status: RELEASED\n"
                "```\n"
                "🏷️ Pre-jail roles restored (Verified excluded — must re-verify).\n"
                "👁️ Channel access restored."
            ),
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="TrapAI Enforcement • Access Updated")
        await ctx.send(embed=embed)
        await log(ctx.guild, "jail", "Member Unjailed", None, discord.Color.green(),
                  fields=[("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("🔓 User", f"{member.mention} (`{member.id}`)", True), ("📝 Reason", reason, False)],
                  actor=ctx.author, target=member)

    except discord.Forbidden:
        await ctx.send(_role_forbidden_reason(ctx.guild))
    except discord.HTTPException:
        await ctx.send("❌ Something went wrong while unjailing that member.")


@bot.command(name="worktime", aliases=["dotime"])
async def worktime(ctx):
    """
    Answer a quick math question to shave 1 minute off your jail
    sentence. Only works while jailed — on a 2-minute cooldown per
    attempt (right or wrong). Usage: ,worktime
    """
    guild = ctx.guild
    jail_role = discord.utils.get(guild.roles, name=JAIL_ROLE)
    if not jail_role or jail_role not in ctx.author.roles:
        await ctx.send("❌ This is only for jailed members.", delete_after=8)
        return

    cd = _on_cooldown(guild.id, ctx.author.id, "worktime", 120)
    if cd:
        m, s = divmod(cd, 60)
        await ctx.send(f"⏳ You can work again in **{m}m {s}s**.", delete_after=8)
        return

    inmate_number = _get_or_assign_inmate_number(guild.id, ctx.author.id)
    a, b = random.randint(1, 20), random.randint(1, 20)
    op = random.choice(["+", "-", "×"])
    answer = a + b if op == "+" else a - b if op == "-" else a * b

    await ctx.send(
        f"🧮 Inmate #{inmate_number}, solve this in 30 seconds to shave **1 minute** off your time: "
        f"**{a} {op} {b} = ?**"
    )

    def check(m):
        return m.author.id == ctx.author.id and m.channel.id == ctx.channel.id

    try:
        guess = await bot.wait_for("message", check=check, timeout=30)
    except asyncio.TimeoutError:
        await ctx.send(f"⏰ Too slow, Inmate #{inmate_number} — no time off this round.", delete_after=8)
        return

    try:
        given = int(guess.content.strip())
    except ValueError:
        given = None

    if given != answer:
        await ctx.send(f"❌ Wrong — it was **{answer}**. Try again in 2 minutes.", delete_after=8)
        return

    entry = JAIL_EXPIRY.get(guild.id, {}).get(ctx.author.id)
    if not entry:
        await ctx.send("✅ Correct! (No timed sentence on file to reduce, though.)", delete_after=8)
        return

    remaining = entry["expires_at"] - time.time()
    new_remaining = remaining - 60

    if new_remaining <= 0:
        await _release_from_jail(guild, ctx.author, "Time served reduced to zero via ,worktime")
        _log_mod_action(guild.id, ctx.author.id, "worktime_release", "System (task completed)",
                         "Time served reduced to zero via ,worktime")
        await ctx.send(f"🎉 Correct! That's enough — Inmate #{inmate_number} has served their time and is **released**!")
        return

    entry["expires_at"] = time.time() + new_remaining
    _save_jail_expiry()

    old_task = jailed_users.get((guild.id, ctx.author.id))
    if old_task:
        old_task.cancel()
    jailed_users[(guild.id, ctx.author.id)] = asyncio.create_task(
        auto_unjail(guild.id, ctx.author.id, new_remaining, entry.get("reason", "Jail timer expired"))
    )

    m, s = divmod(int(new_remaining), 60)
    await ctx.send(f"✅ Correct! **1 minute** off your sentence — Inmate #{inmate_number} now has **{m}m {s}s** left.")


# ============================================================
# MUSIC COMMANDS
# ============================================================
@bot.command()
async def play(ctx, *, query: str = None):
    """
    Play a song by name or link. Joins your voice channel and queues it.
    Usage: ,play <song name or link>
    """
    if not query:
        await ctx.send("❌ Usage: `,play <song name or link>`", delete_after=8)
        return
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("❌ You have to be in a voice channel to play something.", delete_after=8)
        return
    vc = ctx.guild.voice_client
    if vc and vc.channel.id != ctx.author.voice.channel.id and any(not m.bot for m in vc.channel.members):
        await ctx.send(
            f"❌ I'm currently playing in **{vc.channel.name}** — join that channel to add to the queue.",
            delete_after=8,
        )
        return
    async with ctx.typing():
        track = await _music_enqueue(ctx.guild, ctx.author, query, ctx.channel)
    if not track:
        await ctx.send(f"❌ Couldn't find anything for **{query}**.", delete_after=8)
        return
    await ctx.send(f"🎵 Queued **{track['title']}**.")


@bot.command()
async def skip(ctx):
    """Skip the current song. Usage: ,skip"""
    if not _in_bot_vc(ctx.author):
        await ctx.send("❌ You have to be in the voice channel to control the music.", delete_after=8)
        return
    await ctx.send(_music_skip(ctx.guild))


@bot.command()
async def pause(ctx):
    """Pause or resume the current song. Usage: ,pause"""
    if not _in_bot_vc(ctx.author):
        await ctx.send("❌ You have to be in the voice channel to control the music.", delete_after=8)
        return
    await ctx.send(_music_pause_resume(ctx.guild))


@bot.command()
async def musicstop(ctx):
    """Stop playback, clear the queue, and leave the voice channel. Usage: ,musicstop"""
    if not _in_bot_vc(ctx.author):
        await ctx.send("❌ You have to be in the voice channel to control the music.", delete_after=8)
        return
    await ctx.send(await _music_stop(ctx.guild))


@bot.command()
async def musicloop(ctx):
    """Toggle looping the current song instead of advancing the queue. Usage: ,musicloop"""
    if not _in_bot_vc(ctx.author):
        await ctx.send("❌ You have to be in the voice channel to control the music.", delete_after=8)
        return
    await ctx.send(_music_toggle_loop(ctx.guild))


@bot.command()
async def volume(ctx, percent: int = None):
    """Show or set playback volume (0-200%). Usage: ,volume 80"""
    if percent is None:
        current = int(MUSIC_VOLUME.get(ctx.guild.id, 0.5) * 100)
        await ctx.send(f"🔊 Current volume: **{current}%**. Usage: `,volume <0-200>`")
        return
    if not _in_bot_vc(ctx.author):
        await ctx.send("❌ You have to be in the voice channel to control the music.", delete_after=8)
        return
    await ctx.send(_music_set_volume(ctx.guild, percent))


@bot.command(aliases=["np"])
async def nowplaying(ctx):
    """Show the current track and queue with the control panel. Usage: ,nowplaying"""
    track = MUSIC_NOW_PLAYING.get(ctx.guild.id)
    if not track:
        await ctx.send("📭 Nothing is playing right now.")
        return
    MUSIC_TEXT_CHANNEL[ctx.guild.id] = ctx.channel.id
    await _post_now_playing(ctx.guild, track)


@bot.command()
async def musicqueue(ctx):
    """Show what's queued up. Usage: ,musicqueue"""
    queue = MUSIC_QUEUES.get(ctx.guild.id, [])
    now = MUSIC_NOW_PLAYING.get(ctx.guild.id)
    if not now and not queue:
        await ctx.send("📭 Nothing playing and the queue is empty.")
        return
    embed = discord.Embed(title="📜 Music Queue", color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
    if now:
        embed.add_field(name="🎶 Now Playing", value=f"[{now['title']}]({now['webpage_url']}) — {now['requester']}", inline=False)
    if queue:
        lines = [f"**{i + 1}.** [{t['title']}]({t['webpage_url']}) — {t['requester']}" for i, t in enumerate(queue[:10])]
        if len(queue) > 10:
            lines.append(f"*+ {len(queue) - 10} more*")
        embed.add_field(name="⏭️ Up Next", value="\n".join(lines), inline=False)
    embed.set_footer(text=f"TrapAI Music • {ctx.guild.name}")
    await ctx.send(embed=embed)


# ============================================================
# MODERATION COMMANDS
# ============================================================
@bot.command()
@_permitted_check(manage_messages=True)
async def clear(ctx, amount: int):
    """Bulk-delete recent messages in this channel. Usage: ,clear <amount>"""
    _CLEAR_SUPPRESS[ctx.channel.id] = discord.utils.utcnow().timestamp()
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass
    deleted = await ctx.channel.purge(limit=amount)
    await log(ctx.guild, "clears", "Messages Cleared", None, discord.Color.orange(),
              fields=[("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("📍 Channel", ctx.channel.mention, True), ("🧹 Amount", str(len(deleted)), True)],
              actor=ctx.author)


@bot.command()
@_permitted_check(administrator=True)
async def highstaffrole(ctx, action: str = None, role: discord.Role = None):
    """
    Manage which roles count as "high rank" — the only ones still allowed
    to talk in a channel locked with ,lock. Everyone else, including
    lower-rank staff without one of these roles, gets blocked.

    Usage:
      ,highstaffrole list          — see all high-staff roles
      ,highstaffrole add @role     — mark a role as high-staff
      ,highstaffrole remove @role  — unmark a role
    """
    guild = ctx.guild
    high = HIGH_STAFF_ROLES.setdefault(guild.id, set())

    if action is None or action.lower() == "list":
        embed = discord.Embed(
            title="👑 High-Staff Roles",
            description="These roles can still talk in any channel locked with `,lock`.",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        if high:
            lines = [f"👑 {r.mention} (`{rid}`)" if (r := guild.get_role(rid)) else f"👑 *deleted role* (`{rid}`)" for rid in high]
            embed.add_field(name=f"Roles ({len(high)})", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="No high-staff roles set", value="Use `,highstaffrole add @role` to add one.", inline=False)
        embed.set_footer(text=f"TrapAI • {guild.name}")
        await ctx.send(embed=embed)
        return

    if role is None:
        await ctx.send("❌ Provide a role. Example: `,highstaffrole add @Head Staff`", delete_after=8)
        return

    if action.lower() == "add":
        if role.id in high:
            await ctx.send(f"❌ **{role.name}** is already a high-staff role.", delete_after=6)
            return
        high.add(role.id)
        _save_high_staff_roles()
        await ctx.send(f"✅ {role.mention} can now talk through any `,lock`.")
    elif action.lower() == "remove":
        if role.id not in high:
            await ctx.send(f"❌ **{role.name}** isn't a high-staff role.", delete_after=6)
            return
        high.discard(role.id)
        _save_high_staff_roles()
        await ctx.send(f"✅ {role.mention} is no longer exempt from `,lock`.")
    else:
        await ctx.send("❌ Usage: `,highstaffrole list|add|remove [@role]`", delete_after=8)


@bot.command()
@_permitted_check(manage_channels=True)
async def lock(ctx):
    """
    Lock the channel — only high-staff roles (set with ,highstaffrole)
    can still send messages here; everyone else, including lower-rank
    staff, is blocked. Usage: ,lock
    """
    guild, channel = ctx.guild, ctx.channel
    await channel.set_permissions(guild.default_role, send_messages=False)

    high_roles = []
    for role_id in HIGH_STAFF_ROLES.get(guild.id, set()):
        role = guild.get_role(role_id)
        if role:
            await channel.set_permissions(role, send_messages=True)
            high_roles.append(role)

    if high_roles:
        exempt_note = " — exempt: " + ", ".join(r.mention for r in high_roles)
    else:
        exempt_note = " — no high-staff roles set, use `,highstaffrole add @role` to exempt one"
    await ctx.send(f"🔒 Channel locked{exempt_note}")
    await log(ctx.guild, "mod", "Channel Locked", None, discord.Color.red(),
              fields=[("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("🔐 Channel", ctx.channel.mention, True)],
              actor=ctx.author)


@bot.command()
@_permitted_check(manage_channels=True)
async def unlock(ctx):
    """
    Unlock the channel — clears the send_messages deny ,lock set (rather
    than force-allowing it, so a channel meant to stay read-only for other
    reasons doesn't get force-opened), and also clears Unverified's own
    view/send block on this specific channel (from ,setup or
    ,lockunverified), so unverified members can actually use it too once
    it's unlocked instead of still being blocked by that separate,
    role-level override. Usage: ,unlock
    """
    guild = ctx.guild
    channel = ctx.channel

    ow = channel.overwrites_for(guild.default_role)
    ow.send_messages = None
    if ow.is_empty():
        await channel.set_permissions(guild.default_role, overwrite=None)
    else:
        await channel.set_permissions(guild.default_role, overwrite=ow)

    unverified_role = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE)
    if unverified_role:
        uow = channel.overwrites_for(unverified_role)
        uow.view_channel = None
        uow.send_messages = None
        if uow.is_empty():
            await channel.set_permissions(unverified_role, overwrite=None)
        else:
            await channel.set_permissions(unverified_role, overwrite=uow)

    for role_id in HIGH_STAFF_ROLES.get(guild.id, set()):
        role = guild.get_role(role_id)
        if not role:
            continue
        row = channel.overwrites_for(role)
        row.send_messages = None
        if row.is_empty():
            await channel.set_permissions(role, overwrite=None)
        else:
            await channel.set_permissions(role, overwrite=row)

    await ctx.send("🔓 Channel unlocked (including for unverified members)")
    await log(ctx.guild, "mod", "Channel Unlocked", None, discord.Color.green(),
              fields=[("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("🔓 Channel", ctx.channel.mention, True)],
              actor=ctx.author)


@bot.command()
@commands.is_owner()
async def restart(ctx):
    """
    Restart the bot process — no need to Ctrl+C and re-run python bot.py
    yourself. Saves all state first, then re-executes the same command
    that originally launched it, in the same window. Posts a confirmation
    back in this channel once it's actually reconnected, not just when
    the restart starts. Bot-owner only: this affects every server the bot
    is in, not just this one, so a per-server admin can no longer trigger
    it (previously any admin in any server the bot's in could restart it
    for everyone).
    Usage: ,restart
    """
    await ctx.send("🔄 Restarting bot... I'll post here again once I'm back online.")
    await log(ctx.guild, "mod", "Bot Restarted", None, discord.Color.blurple(),
              fields=[("👑 Administrator", f"{ctx.author.mention} (`{ctx.author.id}`)", True)],
              actor=ctx.author)
    _save_all_state()
    await bot.close()
    new_argv = _clean_restart_argv() + ["--restart-notify", str(ctx.channel.id), str(time.time())]
    os.execv(sys.executable, [sys.executable] + new_argv)


@bot.command()
@_permitted_check(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason="No reason provided"):
    # DM before kick so they receive it while still in the server
    await _dm_action(member, ctx.guild, "kick", ctx.author, reason)
    await member.kick(reason=reason)
    _log_mod_action(ctx.guild.id, member.id, "kick", ctx.author, reason)
    embed = discord.Embed(
        title="👢 Member Kicked",
        description=f"{member.mention} has been kicked from **{ctx.guild.name}**.",
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="📝 Reason",    value=reason,          inline=False)
    embed.add_field(name="🛡 Moderator", value=ctx.author.mention, inline=True)
    embed.add_field(name="👤 User ID",   value=str(member.id),   inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"TrapAI Moderation • {ctx.guild.name}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)
    await log(ctx.guild, "kicks", "Member Kicked", None, discord.Color.orange(),
              fields=[
                  ("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("👢 User",      f"{member.mention} (`{member.id}`)",         True),
                  ("📝 Reason",    reason,                                        False),
                  ("📨 DM Sent",   "✅ Notified via DM",                          True),
              ],
              actor=ctx.author, target=member)


@bot.command()
@_permitted_check(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason="No reason provided"):
    # DM concurrently with the ban itself instead of waiting on the DM
    # first — the DM's own send() has its own try/except and can't fail
    # the ban, so there's no correctness reason to serialize these.
    await asyncio.gather(
        _dm_action(member, ctx.guild, "ban", ctx.author, reason),
        member.ban(reason=reason),
    )
    _log_mod_action(ctx.guild.id, member.id, "ban", ctx.author, reason)
    embed = discord.Embed(
        title="🔨 Member Banned",
        description=f"{member.mention} has been banned from **{ctx.guild.name}**.",
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="📝 Reason",    value=reason,            inline=False)
    embed.add_field(name="🛡 Moderator", value=ctx.author.mention, inline=True)
    embed.add_field(name="👤 User ID",   value=str(member.id),     inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"TrapAI Moderation • {ctx.guild.name}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)
    await log(ctx.guild, "bans", "Member Banned", None, discord.Color.red(),
              fields=[
                  ("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("🔨 User",      f"{member.mention} (`{member.id}`)",          True),
                  ("📝 Reason",    reason,                                         False),
                  ("📨 DM Sent",   "✅ Notified via DM",                           True),
              ],
              actor=ctx.author, target=member)


@bot.command()
@_permitted_check(moderate_members=True)
async def timeout(ctx, member: discord.Member, minutes: int, *, reason="No reason provided"):
    until      = discord.utils.utcnow() + timedelta(minutes=minutes)
    expire_str = discord.utils.format_dt(until, "F")
    # DM concurrently with applying the timeout — no need to serialize these
    await asyncio.gather(
        _dm_action(member, ctx.guild, "timeout", ctx.author, reason,
                   extra=f"Duration: **{minutes} minute(s)**\nExpires: {expire_str}"),
        member.timeout(until, reason=reason),
    )
    _log_mod_action(ctx.guild.id, member.id, "timeout", ctx.author, reason, extra=f"Duration: {minutes} minute(s)")
    embed = discord.Embed(
        title="⏳ Member Timed Out",
        description=f"{member.mention} has been timed out in **{ctx.guild.name}**.",
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="📝 Reason",    value=reason,            inline=False)
    embed.add_field(name="🛡 Moderator", value=ctx.author.mention, inline=True)
    embed.add_field(name="⏱️ Duration",  value=f"{minutes} min",   inline=True)
    embed.add_field(name="🗓️ Expires",   value=expire_str,          inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"TrapAI Moderation • {ctx.guild.name}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)
    await log(ctx.guild, "timeouts", "Member Timed Out", None, discord.Color.purple(),
              fields=[
                  ("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("⏳ User",      f"{member.mention} (`{member.id}`)",          True),
                  ("⏱️ Duration",  f"{minutes} minute(s)",                        True),
                  ("🗓️ Expires",   expire_str,                                    False),
                  ("📝 Reason",    reason,                                         False),
                  ("📨 DM Sent",   "✅ Notified via DM",                           True),
              ],
              actor=ctx.author, target=member)


async def _strip_member_roles(guild: discord.Guild, member: discord.Member, actor, reason: str):
    """
    Strip all removable roles from a member — except @everyone, anything
    above the bot's own top role (physically can't touch it), their
    verified member role, and their own booster custom role (a cosmetic
    perk, not a staff role). Saves a role snapshot so ,restoreallroles can
    undo it. Shared by ,strip and the 3-strikes staff termination in
    ,staffstrike. Returns (removed_role_names, kept_role_names, snapshot_len).
    """
    verified_role = discord.utils.get(guild.roles, name=VERIFIED_ROLE)
    booster_role_id = BOOSTER_ROLES.get(guild.id, {}).get(member.id)

    # Managed roles (Server Booster, bot roles, linked Twitch/YouTube
    # integrations, etc.) can never be manually added or removed via the
    # API no matter the hierarchy or permissions — Discord always 403s on
    # that. Treated as "kept" alongside verified/booster-custom, since
    # there's no way to strip them anyway.
    kept_names = [
        role.name for role in member.roles
        if role.name != "@everyone" and role < guild.me.top_role
        and (role == verified_role or role.id == booster_role_id or role.managed)
    ]
    removable = [
        role for role in member.roles
        if role.name != "@everyone" and role < guild.me.top_role
        and role != verified_role and role.id != booster_role_id and not role.managed
    ]
    if not removable:
        return [], kept_names, 0

    # Snapshot ALL non-everyone roles (including any above the bot's top
    # role that couldn't be removed now, and the kept ones) for full
    # restoration later.
    snapshot = [r.id for r in member.roles if r.name != "@everyone"]
    ROLE_SNAPSHOTS.setdefault(guild.id, {})[member.id] = snapshot
    _save_role_snapshots()

    role_names = [role.name for role in removable]
    await member.remove_roles(*removable, reason=reason)
    _log_mod_action(guild.id, member.id, "strip", actor, f"Removed: {', '.join(role_names)}")
    return role_names, kept_names, len(snapshot)


@bot.command()
@_permitted_check(administrator=True)
async def strip(ctx, member: discord.Member):
    """
    Strip roles from a member — except @everyone, anything above my own top
    role (which I physically can't touch), their verified member role, and
    their own booster custom role (a cosmetic perk, not a staff role) —
    those three stay on. Usage: ,strip @user
    """
    role_names, kept_names, snap_len = await _strip_member_roles(
        ctx.guild, member, ctx.author, f"Stripped by {ctx.author}"
    )
    if not role_names:
        await ctx.send("❌ That user has no roles I can remove.")
        return

    kept_note = f"\n🔒 Kept: {', '.join(kept_names)}" if kept_names else ""
    await ctx.send(
        f"✅ Removed **{len(role_names)} role(s)** from {member.mention}.{kept_note}\n"
        f"📸 Snapshot saved — use `,restoreallroles {member.mention}` to restore."
    )
    await log(ctx.guild, "strips", "All Roles Stripped", None, discord.Color.dark_red(),
              fields=[
                  ("👑 Admin",         f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("⚔️ User",          f"{member.mention} (`{member.id}`)",         True),
                  ("🏷️ Roles Removed", ", ".join(role_names)[:512] or "*None*",    False),
                  ("🔒 Roles Kept",    ", ".join(kept_names)[:512] or "*None*",     False),
                  ("📸 Snapshot",      f"{snap_len} role(s) saved",                 True),
              ],
              actor=ctx.author, target=member)


@bot.command()
@_permitted_check(administrator=True)
async def restoreallroles(ctx, member: discord.Member):
    """Restore all roles that were snapshotted by ,strip. Usage: ,restoreallroles @user"""
    snap = ROLE_SNAPSHOTS.get(ctx.guild.id, {}).get(member.id)
    if not snap:
        await ctx.send(f"❌ No role snapshot found for {member.mention}. Use `,strip` first to create one.")
        return

    restored = []
    failed   = []
    for rid in snap:
        role = ctx.guild.get_role(rid)
        if not role or role.name == "@everyone":
            continue
        if role in member.roles:
            continue
        if role >= ctx.guild.me.top_role:
            failed.append(role.name)
            continue
        try:
            await member.add_roles(role, reason=f"Role snapshot restored by {ctx.author}")
            restored.append(role.name)
        except (discord.Forbidden, discord.HTTPException):
            failed.append(role.name)

    embed = discord.Embed(
        title="🔄 Roles Restored",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="👤 Member",        value=member.mention,                              inline=True)
    embed.add_field(name="✅ Restored",       value=str(len(restored)),                          inline=True)
    embed.add_field(name="❌ Skipped/Failed", value=str(len(failed)),                            inline=True)
    if restored:
        embed.add_field(name="🏷️ Roles Given",   value=", ".join(restored)[:512],               inline=False)
    if failed:
        embed.add_field(name="⚠️ Could Not Give", value=", ".join(failed)[:256],                inline=False)
    embed.set_footer(text=f"Restored by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)

    # Clear the snapshot after restore
    ROLE_SNAPSHOTS.get(ctx.guild.id, {}).pop(member.id, None)
    _save_role_snapshots()

    await log(ctx.guild, "strips", "Roles Snapshot Restored", None, discord.Color.green(),
              fields=[
                  ("👑 Admin",         f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("👤 Member",        f"{member.mention} (`{member.id}`)",         True),
                  ("✅ Roles Restored", ", ".join(restored)[:512] or "None",         False),
              ],
              actor=ctx.author, target=member)


@bot.command()
@_permitted_check(administrator=True)
async def nuke(ctx):
    old_channel = ctx.channel
    guild       = ctx.guild
    author      = ctx.author

    # Snapshot everything we need before deletion
    channel_name     = old_channel.name
    channel_position = old_channel.position
    channel_topic    = old_channel.topic
    channel_category = old_channel.category

    # Clone first, then delete the old one — handled separately so a failure
    # partway through leaves a clear, specific message instead of silently
    # risking two channels or none.
    try:
        new_channel = await old_channel.clone(reason=f"Nuked by {author}")
    except (discord.Forbidden, discord.HTTPException) as e:
        await ctx.send(f"❌ Couldn't clone this channel — nothing was changed. (`{e}`)")
        return

    try:
        await old_channel.delete(reason=f"Nuked by {author}")
    except (discord.Forbidden, discord.HTTPException) as e:
        await new_channel.send(
            f"⚠️ Cloned this channel, but couldn't delete the original — "
            f"you now have **two** channels. Please delete the extra one manually. (`{e}`)"
        )
        return

    # Restore position so it lands in the same spot
    try:
        await new_channel.edit(position=channel_position)
    except (discord.Forbidden, discord.HTTPException):
        pass

    embed = discord.Embed(
        description="💥 Channel has been nuked.",
        color=discord.Color.dark_red(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"Nuked by {author}")
    await new_channel.send(embed=embed)

    await log(
        guild,
        "mod",
        "Channel Nuked",
        None,
        discord.Color.dark_red(),
        fields=[
            ("👑 Admin",   f"{author.mention} (`{author.id}`)", True),
            ("💣 Channel", f"#{channel_name}",                   True),
            ("📂 Category", channel_category.name if channel_category else "None", True),
        ],
        actor=author
    )


async def _set_all_channels_locked(guild: discord.Guild, locked: bool, reason: str):
    # Concurrent, not sequential — a 50-channel server used to mean 50
    # sequential API round-trips for a single lockdown/unlockdown.
    async def _apply(channel):
        try:
            await channel.set_permissions(guild.default_role, send_messages=not locked, reason=reason)
        except (discord.Forbidden, discord.HTTPException):
            pass

    await asyncio.gather(*(_apply(c) for c in guild.text_channels))


@bot.command()
@_permitted_check(administrator=True)
async def lockdown(ctx):
    await _set_all_channels_locked(ctx.guild, True, f"Lockdown by {ctx.author}")
    await ctx.send("🔒 Server lockdown activated.")
    await log(ctx.guild, "lockdowns", "Server Lockdown Enabled", "All text channels locked.", discord.Color.red(),
              fields=[("👑 Admin", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("🔐 Channels Locked", str(len(ctx.guild.text_channels)), True)],
              actor=ctx.author)


@bot.command()
@_permitted_check(administrator=True)
async def unlockdown(ctx):
    await _set_all_channels_locked(ctx.guild, False, f"Lockdown removed by {ctx.author}")
    await ctx.send("🔓 Server lockdown removed.")
    await log(ctx.guild, "unlockdowns", "Server Lockdown Removed", "All text channels unlocked.", discord.Color.green(),
              fields=[("👑 Admin", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("🔓 Channels Unlocked", str(len(ctx.guild.text_channels)), True)],
              actor=ctx.author)


@bot.command()
@_permitted_check(administrator=True)
async def roleall(ctx, role: discord.Role):
    count = 0
    for member in ctx.guild.members:
        try:
            if role not in member.roles:
                await member.add_roles(role, reason=f"Roleall used by {ctx.author}")
                count += 1
        except (discord.Forbidden, discord.HTTPException):
            pass

    await ctx.send(f"✅ Gave **{role.name}** to {count} member(s).")
    await log(ctx.guild, "roleall", "Role Given To All", None, discord.Color.blue(),
              fields=[("👑 Admin", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("🏷️ Role", f"{role.mention}", True), ("👥 Members Affected", str(count), True)],
              actor=ctx.author)


# ============================================================
# STATS COMMANDS
# ============================================================

def _progress_bar(current: float, target: float, length: int = 10) -> str:
    pct = 1.0 if target <= 0 else max(0.0, min(1.0, current / target))
    filled = round(pct * length)
    return "▰" * filled + "▱" * (length - filled)


def _format_vc_duration(total_seconds: int) -> str:
    h, rem = divmod(int(total_seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


def _live_vc_stats(guild: discord.Guild) -> dict:
    """VC_STATS for this guild, with any in-progress session folded in for accuracy."""
    stats = dict(VC_STATS.get(guild.id, {}))
    now = time.time()
    for (gid, uid), joined in vc_join_time.items():
        if gid != guild.id:
            continue
        member = guild.get_member(uid)
        if member and member.voice and member.voice.channel:
            stats[uid] = stats.get(uid, 0) + int(now - joined)
    return stats


def _resume_vc_sessions():
    """Startup-only: re-anchor vc_join_time for everyone already connected to
    voice when the bot (re)starts. vc_join_time is deliberately in-memory
    only (it's just a session-start marker, not a stat itself), but that
    means a restart wipes it — and without an anchor, the member's NEXT
    leave/move event finds no join time to compute a duration from and
    silently adds zero, losing their entire current session's VC time
    (not just the part before the restart). This can't recover time from
    before the restart, but it stops losing everything from the restart
    onward for sessions already in progress."""
    now = time.time()
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            for member in vc.members:
                if not member.bot:
                    vc_join_time[(guild.id, member.id)] = now


async def _resolve_stats_target(ctx, raw: str):
    """Resolve a ,chatstats/,vcstats argument to a Member, or None if it's a leaderboard request."""
    if not raw or raw.strip().lower() in ("leaderboard", "top", "lb"):
        return None
    return await commands.MemberConverter().convert(ctx, raw.strip())


def _rank_medal(rank: int) -> str:
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, "")


def _rank_percentile_label(rank: int, total: int) -> str:
    """Human-friendly percentile tier for a 1-indexed rank out of `total` tracked members."""
    if not rank or total <= 0:
        return "Unranked"
    pct = (rank / total) * 100
    if pct <= 1:
        return "🌟 Top 1%"
    if pct <= 5:
        return "🌟 Top 5%"
    if pct <= 10:
        return "✨ Top 10%"
    if pct <= 25:
        return "⭐ Top 25%"
    if pct <= 50:
        return "Top 50%"
    return f"Top {round(pct)}%"


def _days_since(dt) -> int:
    """Whole days since `dt`, floored at 1 to keep it safe as a divisor."""
    if not dt:
        return 1
    return max(1, (discord.utils.utcnow() - dt).days)


@bot.command()
async def vcstats(ctx, *, target: str = None):
    """
    View voice chat time — a personal rank card by default.
    Usage:
      ,vcstats              — your own card
      ,vcstats @user        — someone else's card
      ,vcstats leaderboard  — top 10 by VC time server-wide
    """
    guild_stats = _live_vc_stats(ctx.guild)

    try:
        member = await _resolve_stats_target(ctx, target) if target else ctx.author
    except commands.MemberNotFound:
        await ctx.send("❌ Member not found. Use `,vcstats @user`, `,vcstats`, or `,vcstats leaderboard`.", delete_after=8)
        return

    if member is None:  # leaderboard requested
        sorted_stats = sorted(guild_stats.items(), key=lambda x: x[1], reverse=True)[:10]
        embed = discord.Embed(
            title="🎤 Voice Chat Leaderboard",
            description=f"**Top VC time in {ctx.guild.name}** (all-time)",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )
        if not sorted_stats:
            embed.description = "No VC time tracked yet."
        else:
            top_secs = sorted_stats[0][1]
            medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
            lines = []
            for i, (uid, secs) in enumerate(sorted_stats):
                m = ctx.guild.get_member(uid)
                name = m.mention if m else f"<@{uid}>"
                bar = _progress_bar(secs, top_secs, length=8)
                lines.append(f"{medals[i]} {name} — **{_format_vc_duration(secs)}**\n`{bar}`")
            embed.description = "\n".join(lines)
            embed.add_field(
                name="📊 Server Totals",
                value=f"**{len(guild_stats)}** member(s) tracked • **{_format_vc_duration(sum(guild_stats.values()))}** combined",
                inline=False
            )
        embed.set_footer(text=f"Requested by {ctx.author} • ,vcstats @user for a personal card", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        return

    total_seconds = guild_stats.get(member.id, 0)
    ranking = sorted(guild_stats.items(), key=lambda x: x[1], reverse=True)
    rank = next((i for i, (uid, _) in enumerate(ranking, start=1) if uid == member.id), None)
    medal = _rank_medal(rank)
    server_total = sum(guild_stats.values())
    share_pct = (total_seconds / server_total * 100) if server_total > 0 else 0.0
    daily_avg = total_seconds / _days_since(member.joined_at)

    embed = discord.Embed(
        title=f"🎤 Voice Stats — {medal + ' ' if medal else ''}{member.display_name}",
        color=member.color if member.color != discord.Color.default() else discord.Color.blue(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="⏱️ Total VC Time", value=f"**{_format_vc_duration(total_seconds)}**", inline=True)
    embed.add_field(name="🏆 Rank", value=f"**#{rank}** of {len(ranking)}" if rank else "Unranked", inline=True)
    embed.add_field(name="📊 Percentile", value=_rank_percentile_label(rank, len(ranking)), inline=True)
    embed.add_field(name="🌍 Server Share", value=f"**{share_pct:.1f}%** of all tracked VC time", inline=True)
    embed.add_field(name="📆 Daily Average", value=f"**{_format_vc_duration(int(daily_avg))}**/day since joining", inline=True)

    if rank and rank > 1:
        ahead_uid, ahead_secs = ranking[rank - 2]
        ahead_member = ctx.guild.get_member(ahead_uid)
        ahead_name = ahead_member.display_name if ahead_member else f"User {ahead_uid}"
        needed = max(0, ahead_secs - total_seconds + 1)
        bar = _progress_bar(total_seconds, ahead_secs)
        embed.add_field(
            name=f"📈 Progress to #{rank - 1}",
            value=f"`{bar}`\n**{_format_vc_duration(needed)}** more to pass **{ahead_name}**",
            inline=False
        )
    elif rank == 1:
        second = ranking[1] if len(ranking) > 1 else None
        lead = f" — **{_format_vc_duration(total_seconds - second[1])}** ahead of #2" if second else ""
        embed.add_field(name="📈 Progress", value=f"🏆 You're **#1**!{lead}", inline=False)
    else:
        embed.add_field(name="📈 Progress", value="Join a voice channel to start racking up time!", inline=False)

    embed.set_footer(text=f"Requested by {ctx.author} • ,vcstats leaderboard for the top 10", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command()
async def whois(ctx, member: discord.Member = None):
    member = member or ctx.author
    roles = ", ".join(r.mention for r in member.roles if r.name != "@everyone") or "None"

    created_str = discord.utils.format_dt(member.created_at, "F") + f" ({discord.utils.format_dt(member.created_at, 'R')})"
    joined_str  = (
        discord.utils.format_dt(member.joined_at, "F") + f" ({discord.utils.format_dt(member.joined_at, 'R')})"
        if member.joined_at else "Unknown"
    )

    chat_count  = CHAT_STATS.get(ctx.guild.id, {}).get(member.id, 0)
    vc_seconds  = _live_vc_stats(ctx.guild).get(member.id, 0)
    vc_h, vc_rem = divmod(vc_seconds, 3600)
    vc_m = vc_rem // 60

    embed = discord.Embed(
        title=f"👤 User Info — {member}",
        color=member.color if member.color != discord.Color.default() else discord.Color.blue(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="🪪 Username",     value=str(member),          inline=True)
    embed.add_field(name="🆔 User ID",      value=str(member.id),       inline=True)
    embed.add_field(name="🤖 Bot",          value="Yes" if member.bot else "No", inline=True)
    embed.add_field(name="📅 Account Created", value=created_str,       inline=False)
    embed.add_field(name="📥 Server Joined",   value=joined_str,        inline=False)
    embed.add_field(name="💬 Messages (all-time)", value=f"{chat_count:,}", inline=True)
    embed.add_field(name="🎤 VC Time (all-time)",  value=f"{vc_h}h {vc_m}m", inline=True)
    embed.add_field(name="🎭 Top Role",     value=member.top_role.mention, inline=True)
    embed.add_field(name="🏷️ Roles",        value=roles[:1024],         inline=False)
    embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


# ============================================================
# CHAT STATS  &  SERVER STATS
# ============================================================
@bot.command()
async def chatstats(ctx, *, target: str = None):
    """
    View chat stats — a personal rank card by default.
    Usage:
      ,chatstats              — your own card
      ,chatstats @user        — someone else's card
      ,chatstats leaderboard  — top 10 chatters server-wide
    """
    guild_stats = CHAT_STATS.get(ctx.guild.id, {})

    try:
        member = await _resolve_stats_target(ctx, target) if target else ctx.author
    except commands.MemberNotFound:
        await ctx.send("❌ Member not found. Use `,chatstats @user`, `,chatstats`, or `,chatstats leaderboard`.", delete_after=8)
        return

    if member is None:  # leaderboard requested
        sorted_stats = sorted(guild_stats.items(), key=lambda x: x[1], reverse=True)[:10]
        embed = discord.Embed(
            title="💬 Chat Stats Leaderboard",
            description=f"**Top chatters in {ctx.guild.name}** (all-time)",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        if not sorted_stats:
            embed.description = "No messages tracked yet."
        else:
            top_count = sorted_stats[0][1]
            medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
            lines = []
            for i, (uid, count) in enumerate(sorted_stats):
                m = ctx.guild.get_member(uid)
                name = m.mention if m else f"<@{uid}>"
                bar = _progress_bar(count, top_count, length=8)
                lines.append(f"{medals[i]} {name} — **{count:,}** messages\n`{bar}`")
            embed.description = "\n".join(lines)
            embed.add_field(
                name="📊 Server Totals",
                value=f"**{len(guild_stats)}** member(s) tracked • **{sum(guild_stats.values()):,}** messages combined",
                inline=False
            )
        embed.set_footer(text=f"Requested by {ctx.author} • ,chatstats @user for a personal card", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        return

    count = guild_stats.get(member.id, 0)
    ranking = sorted(guild_stats.items(), key=lambda x: x[1], reverse=True)
    rank = next((i for i, (uid, _) in enumerate(ranking, start=1) if uid == member.id), None)
    medal = _rank_medal(rank)
    server_total = sum(guild_stats.values())
    share_pct = (count / server_total * 100) if server_total > 0 else 0.0
    daily_avg = count / _days_since(member.joined_at)

    embed = discord.Embed(
        title=f"💬 Chat Stats — {medal + ' ' if medal else ''}{member.display_name}",
        color=member.color if member.color != discord.Color.default() else discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="💬 Total Messages", value=f"**{count:,}**", inline=True)
    embed.add_field(name="🏆 Rank", value=f"**#{rank}** of {len(ranking)}" if rank else "Unranked", inline=True)
    embed.add_field(name="📊 Percentile", value=_rank_percentile_label(rank, len(ranking)), inline=True)
    embed.add_field(name="🌍 Server Share", value=f"**{share_pct:.1f}%** of all tracked messages", inline=True)
    embed.add_field(name="📆 Daily Average", value=f"**{daily_avg:.1f}** messages/day since joining", inline=True)

    if rank and rank > 1:
        ahead_uid, ahead_count = ranking[rank - 2]
        ahead_member = ctx.guild.get_member(ahead_uid)
        ahead_name = ahead_member.display_name if ahead_member else f"User {ahead_uid}"
        needed = max(0, ahead_count - count + 1)
        bar = _progress_bar(count, ahead_count)
        embed.add_field(
            name=f"📈 Progress to #{rank - 1}",
            value=f"`{bar}` {count:,}/{ahead_count:,}\n**{needed:,}** more message(s) to pass **{ahead_name}**",
            inline=False
        )
    elif rank == 1:
        second = ranking[1] if len(ranking) > 1 else None
        lead = f" — **{count - second[1]:,}** ahead of #2" if second else ""
        embed.add_field(name="📈 Progress", value=f"🏆 You're **#1**!{lead}", inline=False)
    else:
        embed.add_field(name="📈 Progress", value="Send some messages to get ranked!", inline=False)

    embed.set_footer(text=f"Requested by {ctx.author} • ,chatstats leaderboard for the top 10", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


# ── Server Stats helpers ─────────────────────────────────────────────────────

def _ss_build_pages(guild: discord.Guild, requester) -> list:
    """Build all embed pages for the serverstats paginator."""
    now   = discord.utils.utcnow()
    icon  = guild.icon.url if guild.icon else None
    acol  = discord.Color.purple()

    # ── raw counts ───────────────────────────────────────────
    total       = guild.member_count or len(guild.members)
    bots        = sum(1 for m in guild.members if m.bot)
    humans      = total - bots
    online      = sum(1 for m in guild.members if m.status == discord.Status.online and not m.bot)
    idle        = sum(1 for m in guild.members if m.status == discord.Status.idle   and not m.bot)
    dnd         = sum(1 for m in guild.members if m.status == discord.Status.dnd    and not m.bot)
    offline     = humans - online - idle - dnd
    in_vc       = sum(1 for m in guild.members if m.voice and m.voice.channel and not m.bot)

    txt_ch      = len(guild.text_channels)
    vc_ch       = len(guild.voice_channels)
    stage_ch    = len(guild.stage_channels)
    forum_ch    = len([c for c in guild.channels if isinstance(c, discord.ForumChannel)])
    cats        = len(guild.categories)
    total_ch    = txt_ch + vc_ch + stage_ch + forum_ch

    roles_all   = len(guild.roles) - 1   # exclude @everyone
    boosts      = guild.premium_subscription_count
    tier        = guild.premium_tier

    # boost tier perks
    _tier_perks = {
        0: "No perks",
        1: "128kbps audio · 50 emoji · animated icon",
        2: "256kbps audio · 50 emoji · server banner · 1.5GB uploads",
        3: "384kbps audio · 100 emoji · vanity URL · 4K video · 100MB uploads",
    }
    tier_perk = _tier_perks.get(tier, "Unknown tier")

    # next milestone
    _milestones = [2, 7, 14]
    next_goal   = next((m for m in _milestones if m > boosts), None)
    boost_bar   = ""
    if next_goal:
        filled   = min(boosts, next_goal)
        pct      = int((filled / next_goal) * 10)
        boost_bar = "█" * pct + "░" * (10 - pct) + f"  {boosts}/{next_goal}"

    # verification & security
    _vlvl = {
        discord.VerificationLevel.none:    "🔓 None",
        discord.VerificationLevel.low:     "🟢 Low (email verified)",
        discord.VerificationLevel.medium:  "🟡 Medium (registered 5min+)",
        discord.VerificationLevel.high:    "🔴 High (member 10min+)",
        discord.VerificationLevel.highest: "🔴 Highest (phone verified)",
    }
    _explvl = {
        discord.ContentFilter.disabled:    "🟢 Off",
        discord.ContentFilter.no_role:     "🟡 Scan no-role members",
        discord.ContentFilter.all_members: "🔴 Scan everyone",
    }
    _mfa = {0: "🔓 Disabled", 1: "🔐 Required for mods"}

    ver_lvl   = _vlvl.get(guild.verification_level, str(guild.verification_level))
    expl_filt = _explvl.get(guild.explicit_content_filter, str(guild.explicit_content_filter))
    mfa_lvl   = _mfa.get(guild.mfa_level, str(guild.mfa_level))
    vanity    = f"`discord.gg/{guild.vanity_url_code}`" if guild.vanity_url_code else "None"

    # special channels
    def _ch(c): return c.mention if c else "Not set"
    sys_ch    = _ch(guild.system_channel)
    rules_ch  = _ch(guild.rules_channel)
    afk_ch    = _ch(guild.afk_channel)
    afk_time  = f"{guild.afk_timeout // 60}min" if guild.afk_channel else "—"
    pub_ch    = _ch(guild.public_updates_channel)

    # features
    nice_feats = {
        "COMMUNITY":            "🏘️ Community",
        "PARTNERED":            "🤝 Partnered",
        "VERIFIED":             "✅ Verified",
        "DISCOVERABLE":         "🔍 Discoverable",
        "MONETIZATION_ENABLED": "💰 Monetization",
        "WELCOME_SCREEN_ENABLED":"👋 Welcome Screen",
        "NEWS":                 "📰 News Channels",
        "ANIMATED_ICON":        "🎞️ Animated Icon",
        "BANNER":               "🖼️ Banner",
        "INVITE_SPLASH":        "💦 Invite Splash",
        "VANITY_URL":           "🔗 Vanity URL",
        "ROLE_ICONS":           "🏷️ Role Icons",
        "THREADS_ENABLED":      "🧵 Threads",
        "TICKETED_EVENTS_ENABLED": "🎟️ Ticketed Events",
        "MEMBER_VERIFICATION_GATE_ENABLED": "🚪 Membership Gate",
    }
    feat_lines = [nice_feats.get(f, f.replace("_", " ").title()) for f in guild.features]
    feats_str  = "  ".join(feat_lines) if feat_lines else "None"

    # top roles by member count (skip @everyone)
    sorted_roles = sorted(
        [r for r in guild.roles if r.id != guild.default_role.id and len(r.members) > 0],
        key=lambda r: len(r.members), reverse=True
    )[:8]

    # chat leaderboard
    guild_chat  = CHAT_STATS.get(guild.id, {})
    chat_sorted = sorted(guild_chat.items(), key=lambda x: x[1], reverse=True)[:10]
    total_msgs  = sum(guild_chat.values())

    # invite leaderboard
    inv_data    = INVITE_DATA.get(guild.id, {})
    inv_sorted  = sorted(inv_data.items(), key=lambda x: x[1]["uses"], reverse=True)[:10]

    medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7

    def _footer(page, total_pages):
        return f"📊 Server Stats  ·  Page {page}/{total_pages}  ·  {guild.name}  ·  Requested by {requester}"

    TOTAL_PAGES = 5

    # ────────────────────────────────────────────────────────
    # PAGE 1 — OVERVIEW
    # ────────────────────────────────────────────────────────
    p1 = discord.Embed(
        title=f"🏠  {guild.name}  ·  Overview",
        color=acol,
        timestamp=now,
    )
    if guild.icon:
        p1.set_thumbnail(url=icon)
    if guild.banner:
        p1.set_image(url=guild.banner.url)
    if guild.description:
        p1.description = f"*{guild.description}*"
    p1.add_field(name="👑 Owner",       value=guild.owner.mention if guild.owner else "Unknown", inline=True)
    p1.add_field(name="🆔 Server ID",   value=f"`{guild.id}`",                                   inline=True)
    p1.add_field(name="🔗 Vanity URL",  value=vanity,                                             inline=True)
    p1.add_field(name="📅 Created",     value=f"{discord.utils.format_dt(guild.created_at, 'D')}\n{discord.utils.format_dt(guild.created_at, 'R')}", inline=True)
    p1.add_field(name="🌍 Region",      value="Auto (Discord Edge)",                              inline=True)
    p1.add_field(name="🌐 Locale",      value=str(guild.preferred_locale),                        inline=True)
    p1.add_field(name="🔏 Verification",value=ver_lvl,  inline=True)
    p1.add_field(name="🔞 Filter",      value=expl_filt,inline=True)
    p1.add_field(name="🔑 2FA Mod",     value=mfa_lvl,  inline=True)
    p1.add_field(name="⚙️ Features",    value=feats_str or "None", inline=False)
    p1.add_field(name="📣 System Ch",   value=sys_ch,   inline=True)
    p1.add_field(name="📜 Rules Ch",    value=rules_ch, inline=True)
    p1.add_field(name="📡 Updates Ch",  value=pub_ch,   inline=True)
    p1.add_field(name="💤 AFK Ch",      value=afk_ch,   inline=True)
    p1.add_field(name="⏱️ AFK Timeout", value=afk_time, inline=True)
    p1.set_footer(text=_footer(1, TOTAL_PAGES), icon_url=requester.display_avatar.url)

    # ────────────────────────────────────────────────────────
    # PAGE 2 — MEMBERS
    # ────────────────────────────────────────────────────────
    p2 = discord.Embed(
        title=f"👥  {guild.name}  ·  Members",
        color=discord.Color.green(),
        timestamp=now,
    )
    if guild.icon:
        p2.set_thumbnail(url=icon)

    # presence bar
    _total_known = online + idle + dnd + offline
    def _pct(n): return f"{n/max(_total_known,1)*100:.1f}%"

    p2.add_field(name="👥 Total",       value=f"**{total:,}**",   inline=True)
    p2.add_field(name="🧑 Humans",      value=f"**{humans:,}**",  inline=True)
    p2.add_field(name="🤖 Bots",        value=f"**{bots:,}**",    inline=True)
    p2.add_field(name="🟢 Online",      value=f"**{online:,}** ({_pct(online)})",  inline=True)
    p2.add_field(name="🟡 Idle",        value=f"**{idle:,}** ({_pct(idle)})",      inline=True)
    p2.add_field(name="🔴 Do Not Dist", value=f"**{dnd:,}** ({_pct(dnd)})",        inline=True)
    p2.add_field(name="⚫ Offline",     value=f"**{offline:,}** ({_pct(offline)})",inline=True)
    p2.add_field(name="🎤 In Voice",    value=f"**{in_vc:,}**",   inline=True)
    p2.add_field(name="🏷️ Roles",       value=f"**{roles_all}**", inline=True)

    # top roles
    if sorted_roles:
        role_lines = [
            f"{medals[i]} {r.mention} — **{len(r.members):,}** members"
            for i, r in enumerate(sorted_roles)
        ]
        p2.add_field(name="🏆 Top Roles by Members", value="\n".join(role_lines), inline=False)

    p2.set_footer(text=_footer(2, TOTAL_PAGES), icon_url=requester.display_avatar.url)

    # ────────────────────────────────────────────────────────
    # PAGE 3 — CHANNELS
    # ────────────────────────────────────────────────────────
    p3 = discord.Embed(
        title=f"💬  {guild.name}  ·  Channels",
        color=discord.Color.blurple(),
        timestamp=now,
    )
    if guild.icon:
        p3.set_thumbnail(url=icon)

    p3.add_field(name="📊 Total",           value=f"**{total_ch}**",   inline=True)
    p3.add_field(name="💬 Text",            value=f"**{txt_ch}**",     inline=True)
    p3.add_field(name="🎤 Voice",           value=f"**{vc_ch}**",      inline=True)
    p3.add_field(name="📡 Stage",           value=f"**{stage_ch}**",   inline=True)
    p3.add_field(name="💬 Forum",           value=f"**{forum_ch}**",   inline=True)
    p3.add_field(name="📁 Categories",      value=f"**{cats}**",       inline=True)

    # list categories with their channel counts
    cat_lines = []
    for cat in sorted(guild.categories, key=lambda c: c.position):
        t = len(cat.text_channels)
        v = len(cat.voice_channels)
        parts = []
        if t: parts.append(f"{t} text")
        if v: parts.append(f"{v} vc")
        cat_lines.append(f"📁 **{cat.name}** — {', '.join(parts) if parts else 'empty'}")
    if cat_lines:
        p3.add_field(name="📂 Category Breakdown", value="\n".join(cat_lines[:20]), inline=False)

    p3.set_footer(text=_footer(3, TOTAL_PAGES), icon_url=requester.display_avatar.url)

    # ────────────────────────────────────────────────────────
    # PAGE 4 — BOOSTS & SERVER PERKS
    # ────────────────────────────────────────────────────────
    p4 = discord.Embed(
        title=f"🚀  {guild.name}  ·  Boosts & Perks",
        color=discord.Color.from_rgb(255, 115, 250),
        timestamp=now,
    )
    if guild.icon:
        p4.set_thumbnail(url=icon)
    if guild.banner:
        p4.set_image(url=guild.banner.url)

    p4.add_field(name="🚀 Total Boosts",    value=f"**{boosts}**",       inline=True)
    p4.add_field(name="🏅 Boost Tier",      value=f"**Tier {tier}**",    inline=True)
    p4.add_field(name="🎁 Tier Perks",      value=tier_perk,             inline=False)
    if boost_bar:
        p4.add_field(name=f"📈 Progress to next tier ({next_goal} boosts)", value=f"`{boost_bar}`", inline=False)

    # list boosters
    boosters = [m for m in guild.members if m.premium_since]
    boosters.sort(key=lambda m: m.premium_since)
    if boosters:
        booster_lines = [
            f"• {m.mention} — since {discord.utils.format_dt(m.premium_since, 'D')}"
            for m in boosters[:15]
        ]
        if len(boosters) > 15:
            booster_lines.append(f"*…and {len(boosters)-15} more*")
        p4.add_field(name=f"🌟 Boosters ({len(boosters)})", value="\n".join(booster_lines), inline=False)
    else:
        p4.add_field(name="🌟 Boosters", value="No active boosters.", inline=False)

    p4.set_footer(text=_footer(4, TOTAL_PAGES), icon_url=requester.display_avatar.url)

    # ────────────────────────────────────────────────────────
    # PAGE 5 — LEADERBOARDS (chat + invites)
    # ────────────────────────────────────────────────────────
    p5 = discord.Embed(
        title=f"🏆  {guild.name}  ·  Leaderboards",
        color=discord.Color.from_rgb(255, 200, 50),
        timestamp=now,
    )
    if guild.icon:
        p5.set_thumbnail(url=icon)

    # Chat leaderboard
    if chat_sorted:
        chat_lines = []
        for i, (uid, cnt) in enumerate(chat_sorted):
            m = guild.get_member(uid)
            name = m.mention if m else f"<@{uid}>"
            pct  = cnt / max(total_msgs, 1) * 100
            chat_lines.append(f"{medals[i]} {name} — **{cnt:,}** msgs ({pct:.1f}%)")
        p5.add_field(
            name=f"💬 Top Chatters  ·  {total_msgs:,} total messages tracked",
            value="\n".join(chat_lines),
            inline=False
        )
    else:
        p5.add_field(name="💬 Chat Leaderboard", value="No messages tracked yet.", inline=False)

    # Invite leaderboard
    if inv_sorted:
        inv_lines = []
        for i, (uid, data) in enumerate(inv_sorted):
            m = guild.get_member(uid)
            name = m.mention if m else f"<@{uid}>"
            inv_lines.append(f"{medals[i]} {name} — **{data['uses']}** invite(s)")
        p5.add_field(name="📨 Top Inviters", value="\n".join(inv_lines), inline=False)
    else:
        p5.add_field(name="📨 Invite Leaderboard", value="No invite data tracked yet.", inline=False)

    # warn counts leaderboard (top 5 most warned)
    guild_warns = WARNINGS.get(guild.id, {})
    warn_sorted = sorted(guild_warns.items(), key=lambda x: len(x[1]), reverse=True)
    warn_sorted = [(uid, warns) for uid, warns in warn_sorted if len(warns) > 0][:5]
    if warn_sorted:
        warn_lines = [
            f"{medals[i]} <@{uid}> — **{len(warns)}** warning(s)"
            for i, (uid, warns) in enumerate(warn_sorted)
        ]
        p5.add_field(name="⚠️ Most Warned Members", value="\n".join(warn_lines), inline=False)

    p5.set_footer(text=_footer(5, TOTAL_PAGES), icon_url=requester.display_avatar.url)

    return [p1, p2, p3, p4, p5]


class ServerStatsView(discord.ui.View):
    """Interactive paginator for serverstats."""

    PAGE_LABELS = [
        ("🏠", "Overview"),
        ("👥", "Members"),
        ("💬", "Channels"),
        ("🚀", "Boosts"),
        ("🏆", "Leaderboard"),
    ]

    def __init__(self, pages: list, author_id: int):
        super().__init__(timeout=120)
        self.pages     = pages
        self.author_id = author_id
        self.index     = 0
        self._rebuild_buttons()

    def _rebuild_buttons(self):
        self.clear_items()
        for i, (emoji, label) in enumerate(self.PAGE_LABELS):
            btn = discord.ui.Button(
                label=label,
                emoji=emoji,
                style=discord.ButtonStyle.primary if i == self.index else discord.ButtonStyle.secondary,
                custom_id=f"ss_page_{i}",
                row=0,
            )
            btn.callback = self._make_callback(i)
            self.add_item(btn)

    def _make_callback(self, page_index: int):
        async def _cb(interaction: discord.Interaction):
            if interaction.user.id != self.author_id:
                await interaction.response.send_message("❌ Only the person who ran this command can navigate it.", ephemeral=True)
                return
            self.index = page_index
            self._rebuild_buttons()
            await interaction.response.edit_message(embed=self.pages[self.index], view=self)
        return _cb

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


@bot.command(aliases=["ss", "sinfo"])
async def serverstats(ctx):
    """Show a full interactive multi-page server stats report."""
    async with ctx.typing():
        pages = _ss_build_pages(ctx.guild, ctx.author)
    view  = ServerStatsView(pages, ctx.author.id)
    await ctx.send(embed=pages[0], view=view)


# ============================================================
# INVITE COMMANDS
# ============================================================
@bot.command()
async def invites(ctx, member: discord.Member = None):
    """Show how many people a user has invited. Usage: ,invites [@user]"""
    member = member or ctx.author
    gdata  = INVITE_DATA.get(ctx.guild.id, {})
    idata  = gdata.get(member.id, {"uses": 0, "logs": []})

    embed = discord.Embed(
        title=f"📨 Invites — {member}",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="👤 User",          value=member.mention,         inline=True)
    embed.add_field(name="📊 Total Invites", value=f"**{idata['uses']}**", inline=True)
    embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command()
async def inviteleaderboard(ctx):
    """Show the top inviters in the server. Usage: ,inviteleaderboard"""
    gdata = INVITE_DATA.get(ctx.guild.id, {})
    if not gdata:
        await ctx.send("📭 No invite data tracked yet.")
        return

    sorted_inv = sorted(gdata.items(), key=lambda x: x[1]["uses"], reverse=True)[:10]

    embed = discord.Embed(
        title="📨 Invite Leaderboard",
        description=f"**Top inviters in {ctx.guild.name}**",
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
    lines  = []
    for i, (uid, data) in enumerate(sorted_inv):
        m = ctx.guild.get_member(uid)
        name = m.mention if m else f"<@{uid}>"
        lines.append(f"{medals[i]} {name} — **{data['uses']}** invite(s)")

    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command()
@_permitted_check(manage_guild=True)
async def invitelogs(ctx, member: discord.Member = None):
    """Show invite join logs for a user. Usage: ,invitelogs [@user]"""
    member = member or ctx.author
    gdata  = INVITE_DATA.get(ctx.guild.id, {})
    idata  = gdata.get(member.id, {"uses": 0, "logs": []})
    logs   = idata["logs"]

    embed = discord.Embed(
        title=f"📋 Invite Logs — {member}",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="📊 Total Invites", value=str(idata["uses"]), inline=True)

    if not logs:
        embed.add_field(name="📭 Logs", value="No joins tracked yet.", inline=False)
    else:
        recent = logs[-10:]
        embed.add_field(
            name=f"🕐 Recent Joins (last {len(recent)})",
            value="\n".join(f"• {entry}" for entry in reversed(recent)),
            inline=False
        )

    embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


# ============================================================
# ROLE COMMANDS
# ============================================================

@bot.group(name="role", invoke_without_command=True)
@_permitted_check(manage_roles=True)
async def role_group(ctx):
    """Role management commands. Use ,role <subcommand>."""
    embed = discord.Embed(
        title="🏷️ Role Commands",
        description=(
            "`,role add @user @role` — give a role to a member\n"
            "`,role remove @user @role` — take a role from a member\n"
            "`,role create <name> [color] [hoist]` — create a new role\n"
            "`,role delete @role` — delete a role\n"
            "`,role info @role` — role details\n"
            "`,role list` — list all server roles\n"
            "`,role color @role #hex` — change role colour\n"
            "`,role hoist @role` — toggle role hoisting\n"
            "`,role members @role` — list members with a role\n"
            "`,role user @user` — all roles a user has"
        ),
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"TrapAI Role Manager • {ctx.guild.name}")
    await ctx.send(embed=embed)


@role_group.command(name="add")
@_permitted_check(manage_roles=True)
async def role_add(ctx, member: discord.Member, role: discord.Role):
    """Give a role to a member. Usage: ,role add @user @role"""
    if role >= ctx.guild.me.top_role:
        await ctx.send("❌ I can't assign a role higher than or equal to my own top role.")
        return
    if role in member.roles:
        await ctx.send(f"❌ {member.mention} already has {role.mention}.")
        return
    await member.add_roles(role, reason=f"Role added by {ctx.author}")
    embed = discord.Embed(
        title="✅ Role Added",
        description=f"Gave {role.mention} to {member.mention}.",
        color=role.color if role.color != discord.Color.default() else discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"By {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)
    await log(ctx.guild, "roles", "Role Manually Added", None, discord.Color.green(),
              fields=[
                  ("👑 Staff", f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("👤 User",  f"{member.mention} (`{member.id}`)",         True),
                  ("🏷️ Role",  f"{role.mention} (`{role.id}`)",             True),
              ],
              actor=ctx.author, target=member)


@role_group.command(name="remove")
@_permitted_check(manage_roles=True)
async def role_remove(ctx, member: discord.Member, role: discord.Role):
    """Remove a role from a member. Usage: ,role remove @user @role"""
    if role >= ctx.guild.me.top_role:
        await ctx.send("❌ I can't manage a role higher than or equal to my own top role.")
        return
    if role not in member.roles:
        await ctx.send(f"❌ {member.mention} doesn't have {role.mention}.")
        return
    await member.remove_roles(role, reason=f"Role removed by {ctx.author}")
    embed = discord.Embed(
        title="✅ Role Removed",
        description=f"Removed {role.mention} from {member.mention}.",
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"By {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)
    await log(ctx.guild, "roles", "Role Manually Removed", None, discord.Color.orange(),
              fields=[
                  ("👑 Staff", f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("👤 User",  f"{member.mention} (`{member.id}`)",         True),
                  ("🏷️ Role",  f"{role.mention} (`{role.id}`)",             True),
              ],
              actor=ctx.author, target=member)


@role_group.command(name="create")
@_permitted_check(manage_roles=True)
async def role_create(ctx, *, rest: str):
    """
    Create a new role — just type the name normally, spaces and all, no
    quotes needed. Optionally end it with a color (red, blue, gold, ...
    or hex like ff0000) and/or the word "hoist" to display the role
    separately in the member list; both are peeled off the end if present,
    everything else becomes the name.
    Usage: ,role create Glock30 Member
           ,role create Glock30 Member purple
           ,role create Glock30 Member purple hoist
    """
    tokens = rest.split()

    hoist = False
    if tokens and tokens[-1].lower() == "hoist":
        hoist = True
        tokens.pop()

    disc_color = discord.Color.default()
    if tokens:
        parsed = _parse_ticket_color(tokens[-1])
        if parsed is not None:
            disc_color = parsed
            tokens.pop()

    name = " ".join(tokens).strip()
    if not name:
        await ctx.send("❌ You need to give the role a name. Usage: `,role create <name> [color] [hoist]`", delete_after=10)
        return

    new_role = await ctx.guild.create_role(
        name=name, color=disc_color, hoist=hoist,
        reason=f"Role created by {ctx.author}"
    )
    embed = discord.Embed(
        title="✅ Role Created",
        description=f"{new_role.mention} has been created.",
        color=new_role.color,
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="Name",  value=new_role.name,  inline=True)
    embed.add_field(name="Color", value=str(new_role.color), inline=True)
    embed.add_field(name="Hoist", value=str(hoist),     inline=True)
    embed.set_footer(text=f"By {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)
    await log(ctx.guild, "role_create", "Role Created (cmd)", None, discord.Color.green(),
              fields=[
                  ("👑 Staff", f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("✨ Role",  f"{new_role.mention} (`{new_role.id}`)",     True),
                  ("🎨 Color", str(new_role.color),                        True),
                  ("📌 Hoist", str(hoist),                                 True),
              ],
              actor=ctx.author)


@role_group.command(name="delete")
@_permitted_check(manage_roles=True)
async def role_delete(ctx, role: discord.Role):
    """Delete a role. Usage: ,role delete @role"""
    if role >= ctx.guild.me.top_role:
        await ctx.send("❌ I can't delete a role higher than or equal to my own top role.")
        return
    if role.managed:
        await ctx.send("❌ That role is managed by an integration and cannot be deleted.")
        return
    name = role.name
    await role.delete(reason=f"Role deleted by {ctx.author}")
    embed = discord.Embed(
        title="🗑️ Role Deleted",
        description=f"Role **{name}** has been deleted.",
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"By {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)
    await log(ctx.guild, "role_delete", "Role Deleted (cmd)", None, discord.Color.red(),
              fields=[
                  ("👑 Staff",     f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("🗑️ Role Name", f"`{name}`",                                 True),
              ],
              actor=ctx.author)


@role_group.command(name="info")
async def role_info(ctx, role: discord.Role):
    """Show detailed info about a role. Usage: ,role info @role"""
    created_str = discord.utils.format_dt(role.created_at, "F") + f" ({discord.utils.format_dt(role.created_at, 'R')})"
    perms = [p for p, v in role.permissions if v]
    perms_str = ", ".join(perms[:12]) + ("…" if len(perms) > 12 else "") if perms else "None"

    embed = discord.Embed(
        title=f"🏷️ Role Info — {role.name}",
        color=role.color if role.color != discord.Color.default() else discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="🆔 Role ID",     value=str(role.id),          inline=True)
    embed.add_field(name="🎨 Color",       value=str(role.color),       inline=True)
    embed.add_field(name="📌 Position",    value=str(role.position),    inline=True)
    embed.add_field(name="👥 Members",     value=str(len(role.members)), inline=True)
    embed.add_field(name="📌 Hoisted",     value=str(role.hoist),       inline=True)
    embed.add_field(name="💬 Mentionable", value=str(role.mentionable), inline=True)
    embed.add_field(name="🤖 Managed",     value=str(role.managed),     inline=True)
    embed.add_field(name="📅 Created",     value=created_str,           inline=False)
    embed.add_field(name="🔑 Key Perms",   value=perms_str,             inline=False)
    embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


_ROLE_LIST_PAGE_SIZE = 40  # generous — most servers' full role list fits on one page


def _build_role_list_pages(guild: discord.Guild, requester) -> list:
    """Every role in the guild, split into embeds of _ROLE_LIST_PAGE_SIZE —
    never silently truncated like the old "…and N more roles" cutoff."""
    roles = sorted(guild.roles[1:], key=lambda r: r.position, reverse=True)
    lines = [f"{r.mention} — `{r.id}` — {len(r.members)} member(s)" for r in roles]
    chunks = [lines[i:i + _ROLE_LIST_PAGE_SIZE] for i in range(0, len(lines), _ROLE_LIST_PAGE_SIZE)] or [[]]
    total_pages = len(chunks)

    pages = []
    for idx, chunk in enumerate(chunks, start=1):
        embed = discord.Embed(
            title=f"🏷️ Roles in {guild.name}",
            description="\n".join(chunk) if chunk else "No roles.",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        footer = f"{len(roles)} total roles"
        if total_pages > 1:
            footer += f" • Page {idx}/{total_pages}"
        footer += f" • Requested by {requester}"
        embed.set_footer(text=footer, icon_url=requester.display_avatar.url)
        pages.append(embed)
    return pages


class RoleListView(discord.ui.View):
    def __init__(self, pages: list, author_id: int):
        super().__init__(timeout=180)
        self.pages = pages
        self.index = 0
        self.author_id = author_id
        self._sync_buttons()

    def _sync_buttons(self):
        self.prev_btn.disabled = self.index == 0
        self.next_btn.disabled = self.index >= len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ You can't page through someone else's role list.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = max(0, self.index - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = min(len(self.pages) - 1, self.index + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


async def _send_role_list(ctx):
    pages = _build_role_list_pages(ctx.guild, ctx.author)
    if len(pages) > 1:
        await ctx.send(embed=pages[0], view=RoleListView(pages, ctx.author.id))
    else:
        await ctx.send(embed=pages[0])


@role_group.command(name="list")
async def role_list(ctx):
    """List every role in the server, paginated with buttons if it doesn't fit on one page. Usage: ,role list"""
    await _send_role_list(ctx)


@bot.command(name="roles")
async def roles_cmd(ctx):
    """Same as ,role list — view every role in the server. Usage: ,roles"""
    await _send_role_list(ctx)


@role_group.command(name="color")
@_permitted_check(manage_roles=True)
async def role_color(ctx, role: discord.Role, hex_color: str):
    """Change a role's colour. Usage: ,role color @role #ff0000"""
    if role >= ctx.guild.me.top_role:
        await ctx.send("❌ I can't edit a role higher than or equal to my own top role.")
        return
    try:
        hex_val   = int(hex_color.lstrip("#"), 16)
        new_color = discord.Color(hex_val)
    except ValueError:
        await ctx.send("❌ Invalid colour. Use hex like `#ff0000`.")
        return
    old_color = str(role.color)
    await role.edit(color=new_color, reason=f"Color changed by {ctx.author}")
    embed = discord.Embed(
        title="🎨 Role Color Updated",
        description=f"{role.mention} color: `{old_color}` → `{new_color}`.",
        color=new_color,
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"By {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)
    await log(ctx.guild, "roles", "Role Color Changed", None, discord.Color.blurple(),
              fields=[
                  ("👑 Staff", f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("🏷️ Role",  f"{role.mention} (`{role.id}`)",             True),
                  ("🎨 Old",   f"`{old_color}`",                            True),
                  ("🎨 New",   f"`{new_color}`",                            True),
              ],
              actor=ctx.author)


@role_group.command(name="hoist")
@_permitted_check(manage_roles=True)
async def role_hoist(ctx, role: discord.Role):
    """Toggle role hoisting. Usage: ,role hoist @role"""
    if role >= ctx.guild.me.top_role:
        await ctx.send("❌ I can't edit a role higher than or equal to my own top role.")
        return
    old_val = role.hoist
    new_val = not role.hoist
    await role.edit(hoist=new_val, reason=f"Hoist toggled by {ctx.author}")
    state = "now **hoisted** (shown separately)" if new_val else "no longer hoisted"
    embed = discord.Embed(
        title="📌 Role Hoist Updated",
        description=f"{role.mention} is {state}.",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"By {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)
    await log(ctx.guild, "roles", "Role Hoist Changed", None, discord.Color.blurple(),
              fields=[
                  ("👑 Staff", f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("🏷️ Role",  f"{role.mention} (`{role.id}`)",             True),
                  ("📌 Old",   str(old_val),                                True),
                  ("📌 New",   str(new_val),                                True),
              ],
              actor=ctx.author)


@role_group.command(name="members")
async def role_members(ctx, role: discord.Role):
    """List all members with a role. Usage: ,role members @role"""
    members = role.members
    if not members:
        await ctx.send(f"📭 No members have {role.mention}.")
        return
    lines = [f"• {m.mention} (`{m.id}`)" for m in members[:25]]
    if len(members) > 25:
        lines.append(f"… and {len(members) - 25} more")
    embed = discord.Embed(
        title=f"👥 Members with {role.name}",
        description="\n".join(lines),
        color=role.color if role.color != discord.Color.default() else discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"{len(members)} total • Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


@role_group.command(name="user")
async def role_user(ctx, member: discord.Member = None):
    """Show all roles a user has. Usage: ,role user @user"""
    member = member or ctx.author
    roles  = [r for r in member.roles if r.name != "@everyone"]
    if not roles:
        await ctx.send(f"📭 {member.mention} has no roles.")
        return
    roles_sorted = sorted(roles, key=lambda r: r.position, reverse=True)
    lines = [f"{r.mention} — `{r.id}`" for r in roles_sorted]
    embed = discord.Embed(
        title=f"🏷️ Roles — {member}",
        description="\n".join(lines[:25]),
        color=member.color if member.color != discord.Color.default() else discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Total Roles", value=str(len(roles)),          inline=True)
    embed.add_field(name="Top Role",    value=member.top_role.mention,  inline=True)
    embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


# ============================================================
# WARN SYSTEM
# ============================================================
@bot.command()
@_permitted_check(moderate_members=True)
async def warn(ctx, member: discord.Member, *, reason="No reason provided"):
    if member == ctx.author:
        await ctx.send("❌ You can't warn yourself.")
        return

    guild_warns = WARNINGS.setdefault(ctx.guild.id, {})
    user_warns = guild_warns.setdefault(member.id, [])
    user_warns.append({
        "reason": reason,
        "moderator": str(ctx.author),
        "moderator_id": ctx.author.id,
        "time": discord.utils.utcnow()
    })
    _save_warnings()
    _log_mod_action(ctx.guild.id, member.id, "warn", ctx.author, reason)
    count = len(user_warns)

    embed = discord.Embed(
        title="⚠ Member Warned",
        description=f"{member.mention} has received a warning.",
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Total Warnings", value=str(count), inline=False)
    embed.set_footer(text=f"Issued by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)
    await log(ctx.guild, "warns", "Member Warned", None, discord.Color.orange(),
              fields=[("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("⚠️ User", f"{member.mention} (`{member.id}`)", True), ("🔢 Total Warnings", str(count), True), ("📝 Reason", reason, False)],
              actor=ctx.author, target=member)


@bot.command()
async def warnings(ctx, member: discord.Member = None):
    member = member or ctx.author
    user_warns = WARNINGS.get(ctx.guild.id, {}).get(member.id, [])

    embed = discord.Embed(
        title=f"⚠ Warnings — {member}",
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    if not user_warns:
        embed.description = "This user has no warnings."
    else:
        for index, warn_entry in enumerate(user_warns, start=1):
            t = warn_entry['time']
            time_str = discord.utils.format_dt(t, "F") if hasattr(t, 'tzinfo') else str(t)[:19] + " UTC"
            embed.add_field(
                name=f"Warning #{index}",
                value=(
                    f"**Reason:** {warn_entry['reason']}\n"
                    f"**Moderator:** {warn_entry['moderator']}\n"
                    f"**Date:** {time_str}"
                ),
                inline=False
            )

    embed.set_footer(text=f"Requested by {ctx.author}")
    await ctx.send(embed=embed)


@bot.command(aliases=["history", "modlogs"])
async def modhistory(ctx, member: discord.Member = None, *, filter_action: str = None):
    """
    View a member's full moderation history in one place — warn, jail,
    unjail, ban, kick, timeout, mute, unmute, hardban, unhardban, strip.

    Usage:
      ,modhistory              — your own history
      ,modhistory @user        — someone else's full history (staff only)
      ,modhistory @user jail   — filter to just one action type
    """
    member = member or ctx.author

    if member != ctx.author and not (
        ctx.author.guild_permissions.moderate_members or ctx.author.guild_permissions.administrator
    ):
        await ctx.send("❌ You need Moderate Members permission to view someone else's history.", delete_after=8)
        return

    entries = list(MOD_HISTORY.get(ctx.guild.id, {}).get(member.id, []))

    if filter_action:
        key = filter_action.strip().lower().replace(" ", "_")
        entries = [e for e in entries if e["action"] == key]

    entries.sort(key=lambda e: e["time"], reverse=True)

    embed = discord.Embed(
        title=f"📋 Moderation History — {member}",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    if not entries:
        embed.description = "No moderation history found." + (f" (filter: `{filter_action}`)" if filter_action else "")
    else:
        counts = {}
        for e in entries:
            counts[e["action"]] = counts.get(e["action"], 0) + 1
        summary = "  ·  ".join(
            f"{_MOD_HISTORY_ICONS.get(a, '•')} **{c}** {a.replace('_', ' ')}"
            for a, c in sorted(counts.items(), key=lambda x: -x[1])
        )
        embed.description = f"**Total actions:** {len(entries)}\n{summary}"

        for e in entries[:15]:
            icon = _MOD_HISTORY_ICONS.get(e["action"], "•")
            t = e["time"]
            time_str = discord.utils.format_dt(t, "F") if hasattr(t, "tzinfo") else str(t)[:19] + " UTC"
            value = f"**By:** {e['moderator']}\n**Reason:** {e['reason']}"
            if e.get("extra"):
                value += f"\n**Details:** {e['extra']}"
            value += f"\n**When:** {time_str}"
            embed.add_field(
                name=f"{icon} {e['action'].replace('_', ' ').title()}",
                value=value[:1024],
                inline=False
            )
        if len(entries) > 15:
            embed.add_field(name="…", value=f"*+ {len(entries) - 15} more not shown*", inline=False)

    embed.set_footer(text=f"Requested by {ctx.author} • ,modhistory @user <action> to filter", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


# ── Staff leaderboard / stats ──────────────────────────────────
_STAFF_LEADERBOARD_ACTIONS = ["ban", "hardban", "jail", "kick", "timeout", "mute", "warn", "strip"]
_STAFF_ACTION_LABELS = {
    "ban": ("🔨", "Bans"), "hardban": ("🔴", "Hardbans"), "jail": ("🔒", "Jails"),
    "kick": ("👢", "Kicks"), "timeout": ("⏳", "Timeouts"), "mute": ("🔇", "Mutes"),
    "warn": ("⚠️", "Warns"), "strip": ("⚔️", "Strips"),
}


def _staff_action_counts(guild_id: int) -> dict[int, dict[str, int]]:
    """{moderator_id: {"ban": n, "jail": n, ...}} — aggregated from
    MOD_HISTORY (every target member's timeline) and regrouped by who
    performed each action rather than who it was done to."""
    counts: dict[int, dict[str, int]] = {}
    for target_history in MOD_HISTORY.get(guild_id, {}).values():
        for entry in target_history:
            mod_id = entry.get("moderator_id")
            action = entry.get("action")
            if mod_id is None or action not in _STAFF_LEADERBOARD_ACTIONS:
                continue
            per_mod = counts.setdefault(mod_id, {})
            per_mod[action] = per_mod.get(action, 0) + 1
    return counts


@bot.command()
async def staffleaderboard(ctx):
    """
    Rank staff by total moderation actions (bans, jails, kicks, timeouts,
    mutes, warns, hardbans, strips) plus tickets claimed. Usage: ,staffleaderboard
    """
    action_counts = _staff_action_counts(ctx.guild.id)
    claim_counts = STAFF_TICKET_CLAIMS.get(ctx.guild.id, {})

    totals: dict[int, int] = {}
    for mod_id, counts in action_counts.items():
        totals[mod_id] = totals.get(mod_id, 0) + sum(counts.values())
    for mod_id, n in claim_counts.items():
        totals[mod_id] = totals.get(mod_id, 0) + n

    if not totals:
        await ctx.send("📭 No staff moderation activity recorded yet.")
        return

    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:15]
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (mod_id, total) in enumerate(ranked, 1):
        member = ctx.guild.get_member(mod_id)
        name = member.mention if member else f"`{mod_id}` (left server)"
        rank_icon = medals[i - 1] if i <= 3 else f"`#{i}`"
        claims = claim_counts.get(mod_id, 0)
        lines.append(f"{rank_icon} {name} — **{total}** total actions ({claims} 🎫 claimed)")

    embed = discord.Embed(
        title="🏆 Staff Leaderboard",
        description="Ranked by total moderation actions + tickets claimed.\n\n" + "\n".join(lines),
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    if ctx.guild.icon:
        embed.set_thumbnail(url=ctx.guild.icon.url)
    embed.set_footer(text=f"TrapAI • {ctx.guild.name} • ,staffstats @user for a full breakdown")
    await ctx.send(embed=embed)


@bot.command()
async def staffstats(ctx, member: discord.Member = None):
    """Show one staff member's moderation action breakdown. Usage: ,staffstats [@member]"""
    member = member or ctx.author
    action_counts = _staff_action_counts(ctx.guild.id).get(member.id, {})
    claims = STAFF_TICKET_CLAIMS.get(ctx.guild.id, {}).get(member.id, 0)
    total = sum(action_counts.values()) + claims

    if total == 0:
        await ctx.send(f"📭 No recorded staff activity for {member.mention}.")
        return

    embed = discord.Embed(
        title=f"📊 Staff Stats — {member}",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    for action in _STAFF_LEADERBOARD_ACTIONS:
        n = action_counts.get(action, 0)
        if n:
            icon, label = _STAFF_ACTION_LABELS[action]
            embed.add_field(name=f"{icon} {label}", value=str(n), inline=True)
    embed.add_field(name="🎫 Tickets Claimed", value=str(claims), inline=True)
    embed.add_field(name="Σ Total Actions", value=str(total), inline=True)
    embed.set_footer(text=f"TrapAI Staff Stats • {ctx.guild.name}")
    await ctx.send(embed=embed)


# ── Staff of the Month / Most Active Staff ──────────────────
def _current_award_month() -> str:
    return discord.utils.utcnow().strftime("%Y-%m")


def _staff_mod_totals_30d(guild_id: int) -> dict[int, int]:
    """{moderator_id: action_count} from MOD_HISTORY, trailing 30 days only."""
    cutoff = discord.utils.utcnow() - timedelta(days=30)
    totals: dict[int, int] = {}
    for target_history in MOD_HISTORY.get(guild_id, {}).values():
        for entry in target_history:
            mod_id = entry.get("moderator_id")
            if mod_id is None or entry.get("action") not in _STAFF_LEADERBOARD_ACTIONS:
                continue
            if entry["time"] < cutoff:
                continue
            totals[mod_id] = totals.get(mod_id, 0) + 1
    return totals


async def _crown_staff_awards(guild: discord.Guild):
    """Picks this period's Staff of the Month (most moderation actions in
    the trailing 30 days) and Most Active Staff (most messages sent by a
    staff member this period), swaps the reward roles onto the winners,
    and returns the announcement embed — or None if there's no data to
    crown anyone with yet."""
    mod_totals = _staff_mod_totals_30d(guild.id)
    activity_totals = dict(STAFF_ACTIVITY.get(guild.id, {}))
    if not mod_totals and not activity_totals:
        return None

    som_id = max(mod_totals, key=lambda uid: (mod_totals[uid], -uid)) if mod_totals else None
    mas_id = max(activity_totals, key=lambda uid: (activity_totals[uid], -uid)) if activity_totals else None
    period = _current_award_month()

    embed = discord.Embed(title="🏆 Staff Awards", color=discord.Color.gold(), timestamp=discord.utils.utcnow())

    async def _crown_field(role_map, winner_id, field_name, count, unit, *, sotm_extras=False):
        role_id = role_map.get(guild.id)
        role = guild.get_role(role_id) if role_id else None
        winner = guild.get_member(winner_id) if winner_id else None
        if not winner:
            embed.add_field(name=field_name, value="*No qualifying activity this period.*", inline=False)
            return
        value = f"{winner.mention} — **{count}** {unit}"
        old_holders = list(role.members) if role else []
        if role:
            for old_holder in old_holders:
                if old_holder.id != winner.id:
                    try:
                        await old_holder.remove_roles(role, reason="Staff awards — new winner crowned")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
            if role not in winner.roles:
                try:
                    await winner.add_roles(role, reason="Staff awards winner")
                except (discord.Forbidden, discord.HTTPException):
                    value += f"\n⚠️ Couldn't grant {role.mention} — {_role_forbidden_reason(guild)}"
                else:
                    value += f"\n🎁 Awarded {role.mention}"
            else:
                value += f"\n🎁 Keeps {role.mention}"

        if sotm_extras:
            hof_role_id = HALL_OF_FAME_ROLE.get(guild.id)
            hof_role = guild.get_role(hof_role_id) if hof_role_id else None
            if hof_role and hof_role not in winner.roles:
                try:
                    await winner.add_roles(hof_role, reason="Staff of the Month — permanent Hall of Fame induction")
                except (discord.Forbidden, discord.HTTPException):
                    value += f"\n⚠️ Couldn't grant {hof_role.mention} — {_role_forbidden_reason(guild)}"
                else:
                    value += f"\n🏛️ Inducted into {hof_role.mention} (permanent)"
            await _assign_lounge_access(guild, SOTM_LOUNGE.get(guild.id), old_holders, winner)
            for old_holder in old_holders:
                if old_holder.id != winner.id:
                    await _refresh_titled_nickname(old_holder, has_sotm=False)
            await _refresh_titled_nickname(winner, has_sotm=True)
            _log_hall_of_fame(guild.id, "sotm", winner.id, period, count)

        embed.add_field(name=field_name, value=value, inline=False)

    await _crown_field(STAFF_OF_MONTH_ROLE, som_id, "👑 Staff of the Month", mod_totals.get(som_id, 0), "moderation actions", sotm_extras=True)
    await _crown_field(MOST_ACTIVE_STAFF_ROLE, mas_id, "💬 Most Active Staff", activity_totals.get(mas_id, 0), "messages")

    extra_perks = STAFF_PERKS_TEXT.get(guild.id)
    perks_value = _STAFF_AWARD_PERKS_BLURB + (f"\n{extra_perks}" if extra_perks else "")
    embed.add_field(name="🎁 Perks", value=perks_value, inline=False)
    embed.set_footer(text=f"TrapAI Staff Awards • {guild.name}")
    return embed


async def _check_staff_awards():
    """Runs periodically. Crowns each guild exactly once per calendar
    month, right when the month actually rolls over."""
    month = _current_award_month()
    changed = False
    for guild in bot.guilds:
        last = LAST_STAFF_AWARD_MONTH.get(guild.id)
        if last is None:
            # First time seeing this guild — baseline it without crowning
            # off partial/incomplete history.
            LAST_STAFF_AWARD_MONTH[guild.id] = month
            changed = True
            continue
        if last == month:
            continue

        embed = await _crown_staff_awards(guild)
        if embed:
            channel_id = STAFF_AWARDS_CHANNEL.get(guild.id)
            channel = guild.get_channel(channel_id) if channel_id else None
            channel = channel or _resolve_update_channel(guild)
            if channel:
                try:
                    await channel.send(embed=embed)
                except (discord.Forbidden, discord.HTTPException):
                    pass

        STAFF_ACTIVITY[guild.id] = {}
        LAST_STAFF_AWARD_MONTH[guild.id] = month
        changed = True

    if changed:
        _save_last_staff_award_month()
        _save_staff_activity()


async def _staff_awards_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await _check_staff_awards()
        except Exception:
            pass
        await asyncio.sleep(3600)


@bot.command()
@_permitted_check(administrator=True)
async def setstaffofmonthrole(ctx, role: discord.Role = None):
    """
    Set the role crowned each period to Staff of the Month (most
    moderation actions in the trailing 30 days). Run with no argument to
    clear it. Usage: ,setstaffofmonthrole @role
    """
    guild = ctx.guild
    if role is None:
        STAFF_OF_MONTH_ROLE.pop(guild.id, None)
        _save_staff_of_month_role()
        await ctx.send("↩️ Staff of the Month role cleared.")
        return
    STAFF_OF_MONTH_ROLE[guild.id] = role.id
    _save_staff_of_month_role()
    await ctx.send(f"✅ {role.mention} will now be crowned to whoever wins **Staff of the Month**.")


@bot.command()
@_permitted_check(administrator=True)
async def setmostactiverole(ctx, role: discord.Role = None):
    """
    Set the role crowned each period to Most Active Staff (most messages
    sent by a staff member this period). Run with no argument to clear
    it. Usage: ,setmostactiverole @role
    """
    guild = ctx.guild
    if role is None:
        MOST_ACTIVE_STAFF_ROLE.pop(guild.id, None)
        _save_most_active_staff_role()
        await ctx.send("↩️ Most Active Staff role cleared.")
        return
    MOST_ACTIVE_STAFF_ROLE[guild.id] = role.id
    _save_most_active_staff_role()
    await ctx.send(f"✅ {role.mention} will now be crowned to whoever wins **Most Active Staff**.")


@bot.command()
@_permitted_check(administrator=True)
async def setstaffawardschannel(ctx, channel: discord.TextChannel = None):
    """
    Set where the monthly Staff Awards announcement posts. Falls back to
    the update channel if never set. Usage: ,setstaffawardschannel #channel
    """
    guild = ctx.guild
    if channel is None:
        STAFF_AWARDS_CHANNEL.pop(guild.id, None)
        _save_staff_awards_channel()
        await ctx.send("↩️ Staff awards channel cleared — falling back to the update channel.")
        return
    STAFF_AWARDS_CHANNEL[guild.id] = channel.id
    _save_staff_awards_channel()
    await ctx.send(f"✅ Staff Awards will now post in {channel.mention}.")


@bot.command()
@_permitted_check(administrator=True)
async def setstaffperks(ctx, *, text: str = None):
    """
    Set EXTRA perks text shown under the built-in ones (2x work/daily/
    weekly earnings, half ,work cooldown, 3x giveaway odds — those
    always apply automatically, no setup needed). Use this for anything
    beyond that, e.g. "custom color role, priority ticket claims". Run
    with no text to clear it. Usage: ,setstaffperks <text>
    """
    guild = ctx.guild
    if text is None:
        STAFF_PERKS_TEXT.pop(guild.id, None)
        _save_staff_perks_text()
        await ctx.send("↩️ Extra staff perks text cleared.")
        return
    text = text[:1000]
    STAFF_PERKS_TEXT[guild.id] = text
    _save_staff_perks_text()
    await ctx.send(f"✅ Extra perks text set:\n{text}")


@bot.command(aliases=["staffawards"])
@_permitted_check(manage_guild=True)
async def crownstaff(ctx):
    """
    Manually run the Staff of the Month / Most Active Staff picks right
    now instead of waiting for the monthly auto-crown, and post the
    announcement here. Usage: ,crownstaff
    """
    embed = await _crown_staff_awards(ctx.guild)
    if embed is None:
        await ctx.send("📭 No moderation actions or staff activity recorded yet — nothing to crown.")
        return
    await ctx.send(embed=embed)


# ── Staff MVP (weekly) ────────────────────────────────────────
def _current_award_week() -> str:
    return discord.utils.utcnow().strftime("%G-W%V")


async def _crown_mvp_award(guild: discord.Guild):
    """Picks this week's Staff MVP (most staff-activity messages sent
    this week), swaps the MVP role, tracks the win streak (with an
    in-server cash bonus), hands off the private lounge + nickname
    title, and returns the announcement embed — or None if nobody
    qualifies yet."""
    activity_totals = dict(MVP_ACTIVITY.get(guild.id, {}))
    if not activity_totals:
        return None

    winner_id = max(activity_totals, key=lambda uid: (activity_totals[uid], -uid))
    winner = guild.get_member(winner_id)
    count = activity_totals[winner_id]
    period = _current_award_week()

    embed = discord.Embed(title="🏆 Staff MVP of the Week", color=discord.Color.orange(), timestamp=discord.utils.utcnow())
    if not winner:
        embed.add_field(name="🏆 Staff MVP", value="*No qualifying activity this week.*", inline=False)
        embed.add_field(name="🎁 Perks", value=_MVP_PERKS_BLURB, inline=False)
        embed.set_footer(text=f"TrapAI Staff MVP • {guild.name}")
        return embed

    value = f"{winner.mention} — **{count}** messages"

    role_id = MVP_ROLE.get(guild.id)
    role = guild.get_role(role_id) if role_id else None
    old_holders = list(role.members) if role else []
    if role:
        for old_holder in old_holders:
            if old_holder.id != winner.id:
                try:
                    await old_holder.remove_roles(role, reason="Staff MVP — new winner crowned")
                except (discord.Forbidden, discord.HTTPException):
                    pass
        if role not in winner.roles:
            try:
                await winner.add_roles(role, reason="Staff MVP winner")
            except (discord.Forbidden, discord.HTTPException):
                value += f"\n⚠️ Couldn't grant {role.mention} — {_role_forbidden_reason(guild)}"
            else:
                value += f"\n🎁 Awarded {role.mention}"
        else:
            value += f"\n🎁 Keeps {role.mention}"

    prev_winner = LAST_MVP_WINNER.get(guild.id)
    streak = MVP_STREAK.get(guild.id, {}).get(winner.id, 0) + 1 if prev_winner == winner.id else 1
    MVP_STREAK[guild.id] = {winner.id: streak}
    LAST_MVP_WINNER[guild.id] = winner.id
    _save_mvp_streak()
    _save_last_mvp_winner()

    bonus = 500 * streak
    _add_earned(guild.id, winner.id, bonus)
    value += f"\n🔥 **{streak}-week streak!** Bonus: {_fmt_money(bonus)}" if streak > 1 else f"\n💰 Streak bonus: {_fmt_money(bonus)}"

    await _assign_lounge_access(guild, MVP_LOUNGE.get(guild.id), old_holders, winner)
    for old_holder in old_holders:
        if old_holder.id != winner.id:
            await _refresh_titled_nickname(old_holder, has_mvp=False)
    await _refresh_titled_nickname(winner, has_mvp=True)
    _log_hall_of_fame(guild.id, "mvp", winner.id, period, count)

    embed.add_field(name="🏆 Staff MVP", value=value, inline=False)
    embed.add_field(name="🎁 Perks", value=_MVP_PERKS_BLURB, inline=False)
    embed.set_footer(text=f"TrapAI Staff MVP • {guild.name}")
    return embed


async def _check_mvp_awards():
    """Runs periodically. Crowns each guild exactly once per ISO week,
    right when the week actually rolls over."""
    week = _current_award_week()
    changed = False
    for guild in bot.guilds:
        last = LAST_MVP_AWARD_WEEK.get(guild.id)
        if last is None:
            LAST_MVP_AWARD_WEEK[guild.id] = week
            changed = True
            continue
        if last == week:
            continue

        embed = await _crown_mvp_award(guild)
        if embed:
            channel_id = MVP_AWARDS_CHANNEL.get(guild.id) or STAFF_AWARDS_CHANNEL.get(guild.id)
            channel = guild.get_channel(channel_id) if channel_id else None
            channel = channel or _resolve_update_channel(guild)
            if channel:
                try:
                    await channel.send(embed=embed)
                except (discord.Forbidden, discord.HTTPException):
                    pass

        MVP_ACTIVITY[guild.id] = {}
        LAST_MVP_AWARD_WEEK[guild.id] = week
        changed = True

    if changed:
        _save_last_mvp_award_week()
        _save_mvp_activity()


async def _mvp_awards_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await _check_mvp_awards()
        except Exception:
            pass
        await asyncio.sleep(3600)


@bot.command()
@_permitted_check(administrator=True)
async def setmvprole(ctx, role: discord.Role = None):
    """
    Set the role crowned each week to Staff MVP (most staff-activity
    messages sent that week) — independent of Most Active Staff, which
    stays monthly. Run with no argument to clear it. Usage: ,setmvprole @role
    """
    guild = ctx.guild
    if role is None:
        MVP_ROLE.pop(guild.id, None)
        _save_mvp_role()
        await ctx.send("↩️ Staff MVP role cleared.")
        return
    MVP_ROLE[guild.id] = role.id
    _save_mvp_role()
    await ctx.send(f"✅ {role.mention} will now be crowned to whoever wins **Staff MVP** each week.")


@bot.command()
@_permitted_check(administrator=True)
async def setmvpawardschannel(ctx, channel: discord.TextChannel = None):
    """
    Set where the weekly Staff MVP announcement posts. Falls back to the
    Staff Awards channel, then the update channel, if never set.
    Usage: ,setmvpawardschannel #channel
    """
    guild = ctx.guild
    if channel is None:
        MVP_AWARDS_CHANNEL.pop(guild.id, None)
        _save_mvp_awards_channel()
        await ctx.send("↩️ Staff MVP channel cleared — falling back to Staff Awards / update channel.")
        return
    MVP_AWARDS_CHANNEL[guild.id] = channel.id
    _save_mvp_awards_channel()
    await ctx.send(f"✅ Staff MVP will now post in {channel.mention}.")


@bot.command(aliases=["mvpawards"])
@_permitted_check(manage_guild=True)
async def crownmvp(ctx):
    """
    Manually run the Staff MVP pick right now instead of waiting for the
    weekly auto-crown, and post the announcement here. Usage: ,crownmvp
    """
    embed = await _crown_mvp_award(ctx.guild)
    if embed is None:
        await ctx.send("📭 No staff activity recorded yet this week — nothing to crown.")
        return
    await ctx.send(embed=embed)


@bot.command()
@_permitted_check(administrator=True)
async def sethalloffamerole(ctx, role: discord.Role = None):
    """
    Set the PERMANENT role granted to every Staff of the Month winner —
    never removed, stacks up as a lifetime badge. Run with no argument
    to clear it (existing holders keep the role; only future winners
    stop receiving it). Usage: ,sethalloffamerole @role
    """
    guild = ctx.guild
    if role is None:
        HALL_OF_FAME_ROLE.pop(guild.id, None)
        _save_hall_of_fame_role()
        await ctx.send("↩️ Hall of Fame role cleared — existing holders keep it, no new winners will.")
        return
    HALL_OF_FAME_ROLE[guild.id] = role.id
    _save_hall_of_fame_role()
    await ctx.send(f"✅ Every future Staff of the Month winner will be permanently inducted into {role.mention}.")


@bot.command()
@_permitted_check(administrator=True)
async def setsotmtitle(ctx, *, tag: str = None):
    """
    Set the nickname tag shown while a member holds Staff of the Month
    (default 👑). Run with no argument to reset to the default.
    Usage: ,setsotmtitle 👑
    """
    guild = ctx.guild
    if tag is None:
        SOTM_TITLE.pop(guild.id, None)
        _save_sotm_title()
        await ctx.send("↩️ Staff of the Month nickname title reset to the default 👑.")
        return
    SOTM_TITLE[guild.id] = tag.strip()[:16]
    _save_sotm_title()
    await ctx.send(f"✅ Staff of the Month's nickname will now show **{tag.strip()[:16]}**.")


@bot.command()
@_permitted_check(administrator=True)
async def setmvptitle(ctx, *, tag: str = None):
    """
    Set the nickname tag shown while a member holds Staff MVP (default
    🏆). Run with no argument to reset to the default. Usage: ,setmvptitle 🏆
    """
    guild = ctx.guild
    if tag is None:
        MVP_TITLE.pop(guild.id, None)
        _save_mvp_title()
        await ctx.send("↩️ Staff MVP nickname title reset to the default 🏆.")
        return
    MVP_TITLE[guild.id] = tag.strip()[:16]
    _save_mvp_title()
    await ctx.send(f"✅ Staff MVP's nickname will now show **{tag.strip()[:16]}**.")


@bot.command()
@_permitted_check(administrator=True)
async def setsotmlounge(ctx, text: discord.TextChannel = None, voice: discord.VoiceChannel = None):
    """
    Set the private lounge channels handed off to whoever's currently
    Staff of the Month. Lock both channels down from @everyone yourself
    first — the bot only ever grants the current winner an explicit
    override and clears the previous winner's. Run with no arguments to
    clear both. Usage: ,setsotmlounge #text-channel voice-channel-name
    """
    guild = ctx.guild
    if text is None and voice is None:
        SOTM_LOUNGE.pop(guild.id, None)
        _save_sotm_lounge()
        await ctx.send("↩️ Staff of the Month lounge cleared.")
        return
    cfg = SOTM_LOUNGE.setdefault(guild.id, {})
    if text is not None:
        cfg["text"] = text.id
    if voice is not None:
        cfg["voice"] = voice.id
    _save_sotm_lounge()
    parts = [c.mention for c in (text, voice) if c]
    await ctx.send(f"✅ Staff of the Month lounge set: {', '.join(parts)}.")


@bot.command()
@_permitted_check(administrator=True)
async def setmvplounge(ctx, text: discord.TextChannel = None, voice: discord.VoiceChannel = None):
    """
    Set the private lounge channels handed off to whoever's currently
    Staff MVP. Same rules as ,setsotmlounge — lock both channels from
    @everyone yourself first. Run with no arguments to clear both.
    Usage: ,setmvplounge #text-channel voice-channel-name
    """
    guild = ctx.guild
    if text is None and voice is None:
        MVP_LOUNGE.pop(guild.id, None)
        _save_mvp_lounge()
        await ctx.send("↩️ Staff MVP lounge cleared.")
        return
    cfg = MVP_LOUNGE.setdefault(guild.id, {})
    if text is not None:
        cfg["text"] = text.id
    if voice is not None:
        cfg["voice"] = voice.id
    _save_mvp_lounge()
    parts = [c.mention for c in (text, voice) if c]
    await ctx.send(f"✅ Staff MVP lounge set: {', '.join(parts)}.")


@bot.command()
async def halloffame(ctx):
    """Show past Staff of the Month and Staff MVP winners. Usage: ,halloffame"""
    entries = HALL_OF_FAME_LOG.get(ctx.guild.id, [])
    if not entries:
        await ctx.send("📭 No Hall of Fame history yet — run `,crownstaff` / `,crownmvp` or wait for the auto-crown.")
        return

    def _fmt_entries(kind, unit):
        rows = [e for e in entries if e["kind"] == kind][-10:][::-1]
        if not rows:
            return "*None yet.*"
        lines = []
        for e in rows:
            member = ctx.guild.get_member(e["user_id"])
            name = member.mention if member else f"`{e['user_id']}` (left server)"
            lines.append(f"**{e['period']}** — {name} ({e['count']} {unit})")
        return "\n".join(lines)

    embed = discord.Embed(title="🏛️ Staff Hall of Fame", color=discord.Color.gold(), timestamp=discord.utils.utcnow())
    embed.add_field(name="👑 Staff of the Month", value=_fmt_entries("sotm", "actions"), inline=False)
    embed.add_field(name="🏆 Staff MVP", value=_fmt_entries("mvp", "messages"), inline=False)
    embed.set_footer(text=f"TrapAI Hall of Fame • {ctx.guild.name}")
    await ctx.send(embed=embed)


@bot.command()
@_permitted_check(moderate_members=True)
async def clearwarnings(ctx, member: discord.Member):
    guild_warns = WARNINGS.setdefault(ctx.guild.id, {})
    count = len(guild_warns.get(member.id, []))
    guild_warns[member.id] = []
    _save_warnings()

    embed = discord.Embed(
        title="✅ Warnings Cleared",
        description=f"Cleared **{count}** warning(s) for {member.mention}.",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"Cleared by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)
    await log(ctx.guild, "warns", "Warnings Cleared", None, discord.Color.green(),
              fields=[("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("👤 User", f"{member.mention} (`{member.id}`)", True), ("🗑️ Warnings Removed", str(count), True)],
              actor=ctx.author, target=member)


# ============================================================
# STAFF DISCIPLINE — os-only warnings & strikes
# ============================================================
# Separate from the regular ,warn/,warnings system above — this track is
# specifically for disciplining STAFF (not members), and issuing one is
# restricted to whoever holds the "os" role (see STAFF_DISCIPLINE_ROLE /
# _os_only_check).

async def _sweep_expired_staff_warnings():
    """Drop ,staffwarn entries older than STAFF_WARNING_EXPIRY_DAYS. Called
    periodically from the autosave loop, same as the jail/temp-VC sweeps.
    Logs a summary per guild when anything actually expires."""
    cutoff = discord.utils.utcnow() - timedelta(days=STAFF_WARNING_EXPIRY_DAYS)
    changed = False
    for guild_id, guild_warns in list(STAFF_WARNINGS.items()):
        expired_for = {}
        for member_id, entries in list(guild_warns.items()):
            kept = [e for e in entries if e.get("time") and e["time"] >= cutoff]
            removed = len(entries) - len(kept)
            if removed:
                guild_warns[member_id] = kept
                expired_for[member_id] = removed
                changed = True
        if expired_for:
            guild = bot.get_guild(guild_id)
            if guild:
                lines = [f"• <@{uid}> — {n} warning(s)" for uid, n in expired_for.items()]
                await log(guild, "staff", "Staff Warnings Auto-Expired", None, discord.Color.dark_grey(),
                          fields=[
                              ("🗓️ Expired After", f"{STAFF_WARNING_EXPIRY_DAYS} days", True),
                              ("🔢 Total Expired", str(sum(expired_for.values())), True),
                              ("👤 Affected", "\n".join(lines)[:1024], False),
                          ])
    if changed:
        _save_staff_warnings()


@bot.command()
@_os_only_check()
async def staffwarn(ctx, member: discord.Member, *, reason="No reason provided"):
    """
    Issue a formal staff warning (separate from ,warn, which is for
    regular members). Restricted to the "os" role.
    Usage: ,staffwarn @staffmember [reason]
    """
    if member == ctx.author:
        await ctx.send("❌ You can't staff-warn yourself.")
        return

    guild_warns = STAFF_WARNINGS.setdefault(ctx.guild.id, {})
    user_warns = guild_warns.setdefault(member.id, [])
    user_warns.append({
        "reason": reason,
        "moderator": str(ctx.author),
        "moderator_id": ctx.author.id,
        "time": discord.utils.utcnow()
    })
    _save_staff_warnings()
    count = len(user_warns)

    dm_sent = True
    dm_embed = discord.Embed(
        title="⚠️ Staff Warning Issued",
        description=(
            f"You've received a formal staff warning in **{ctx.guild.name}**.\n\n"
            f"**Reason:** {reason}\n"
            f"**Total staff warnings:** {count}"
        ),
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow()
    )
    dm_embed.set_footer(text=f"TrapAI • {ctx.guild.name}")
    try:
        await member.send(embed=dm_embed)
    except (discord.Forbidden, discord.HTTPException):
        dm_sent = False

    embed = discord.Embed(
        title="⚠️ Staff Warning Issued",
        description=(
            f"{member.mention} has received a formal staff warning."
            + ("" if dm_sent else "\n⚠️ Couldn't DM them — their DMs may be closed.")
        ),
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Total Staff Warnings", value=str(count), inline=False)
    embed.set_footer(text=f"Issued by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)

    await log(ctx.guild, "staff", "Staff Warning Issued", None, discord.Color.orange(),
              fields=[
                  ("👑 Issued By", f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("⚠️ Staff Member", f"{member.mention} (`{member.id}`)", True),
                  ("🔢 Total Staff Warnings", str(count), True),
                  ("📝 Reason", reason, False),
                  ("📨 DM Sent", "✅ Yes" if dm_sent else "❌ No (DMs closed)", True),
              ],
              actor=ctx.author, target=member)


@bot.command()
@_os_only_check()
async def staffstrike(ctx, member: discord.Member, *, reason="No reason provided"):
    """
    Issue a formal staff strike — a more serious escalation than a staff
    warning. Restricted to the "os" role. Reaching
    STAFF_TERMINATION_STRIKES (3) auto-terminates the staff member —
    every removable role is stripped, same as ,strip.
    Usage: ,staffstrike @staffmember [reason]
    """
    if member == ctx.author:
        await ctx.send("❌ You can't staff-strike yourself.")
        return

    guild_strikes = STAFF_STRIKES.setdefault(ctx.guild.id, {})
    user_strikes = guild_strikes.setdefault(member.id, [])
    user_strikes.append({
        "reason": reason,
        "moderator": str(ctx.author),
        "moderator_id": ctx.author.id,
        "time": discord.utils.utcnow()
    })
    _save_staff_strikes()
    count = len(user_strikes)

    terminated = count == STAFF_TERMINATION_STRIKES
    stripped_roles = []
    if terminated:
        stripped_roles, _, _ = await _strip_member_roles(
            ctx.guild, member, ctx.author,
            f"Staff terminated — {STAFF_TERMINATION_STRIKES} strikes (issued by {ctx.author})"
        )

    dm_sent = True
    if terminated:
        dm_desc = (
            f"You've reached **{STAFF_TERMINATION_STRIKES} staff strikes** in **{ctx.guild.name}** "
            "and have been **removed from the staff team**.\n\n"
            f"**Reason for this strike:** {reason}"
        )
        dm_title = "🚨 Staff Termination — 3 Strikes"
    else:
        dm_desc = (
            f"You've received a formal staff strike in **{ctx.guild.name}**.\n\n"
            f"**Reason:** {reason}\n"
            f"**Total staff strikes:** {count}"
        )
        dm_title = "❌ Staff Strike Issued"
    dm_embed = discord.Embed(
        title=dm_title,
        description=dm_desc,
        color=discord.Color.dark_red() if terminated else discord.Color.red(),
        timestamp=discord.utils.utcnow()
    )
    dm_embed.set_footer(text=f"TrapAI • {ctx.guild.name}")
    try:
        await member.send(embed=dm_embed)
    except (discord.Forbidden, discord.HTTPException):
        dm_sent = False

    if terminated:
        desc = f"{member.mention} reached **{STAFF_TERMINATION_STRIKES} strikes** and has been **automatically terminated** from the staff team."
    else:
        desc = f"{member.mention} has received a formal staff strike."
    if not dm_sent:
        desc += "\n⚠️ Couldn't DM them — their DMs may be closed."

    embed = discord.Embed(
        title="🚨 Staff Terminated" if terminated else "❌ Staff Strike Issued",
        description=desc,
        color=discord.Color.dark_red() if terminated else discord.Color.red(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Total Staff Strikes", value=str(count), inline=False)
    if terminated:
        embed.add_field(name="🗑️ Roles Removed", value=", ".join(stripped_roles)[:1024] or "*None*", inline=False)
    embed.set_footer(text=f"Issued by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)

    log_fields = [
        ("👑 Issued By", f"{ctx.author.mention} (`{ctx.author.id}`)", True),
        ("❌ Staff Member", f"{member.mention} (`{member.id}`)", True),
        ("🔢 Total Staff Strikes", str(count), True),
        ("📝 Reason", reason, False),
        ("📨 DM Sent", "✅ Yes" if dm_sent else "❌ No (DMs closed)", True),
    ]
    if terminated:
        log_fields.append(("🗑️ Roles Removed", ", ".join(stripped_roles)[:512] or "*None*", False))
    await log(ctx.guild, "staff",
              "Staff Terminated — 3 Strikes" if terminated else "Staff Strike Issued", None,
              discord.Color.dark_red() if terminated else discord.Color.red(),
              fields=log_fields,
              actor=ctx.author, target=member)


@bot.command()
@_os_only_check()
async def staffwarnings(ctx, member: discord.Member = None):
    """View a staff member's formal warning history. Usage: ,staffwarnings [@member]"""
    member = member or ctx.author
    entries = STAFF_WARNINGS.get(ctx.guild.id, {}).get(member.id, [])

    embed = discord.Embed(
        title=f"⚠️ Staff Warnings — {member}",
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    if not entries:
        embed.description = "This staff member has no formal warnings."
    else:
        for index, entry in enumerate(entries, start=1):
            t = entry["time"]
            time_str = discord.utils.format_dt(t, "F") if hasattr(t, "tzinfo") else str(t)[:19] + " UTC"
            embed.add_field(
                name=f"Warning #{index}",
                value=(
                    f"**Reason:** {entry['reason']}\n"
                    f"**Issued By:** {entry['moderator']}\n"
                    f"**Date:** {time_str}"
                ),
                inline=False
            )

    embed.set_footer(text=f"Requested by {ctx.author}")
    await ctx.send(embed=embed)


@bot.command()
@_os_only_check()
async def staffstrikes(ctx, member: discord.Member = None):
    """View a staff member's formal strike history. Usage: ,staffstrikes [@member]"""
    member = member or ctx.author
    entries = STAFF_STRIKES.get(ctx.guild.id, {}).get(member.id, [])

    embed = discord.Embed(
        title=f"❌ Staff Strikes — {member}",
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    if not entries:
        embed.description = "This staff member has no formal strikes."
    else:
        for index, entry in enumerate(entries, start=1):
            t = entry["time"]
            time_str = discord.utils.format_dt(t, "F") if hasattr(t, "tzinfo") else str(t)[:19] + " UTC"
            embed.add_field(
                name=f"Strike #{index}",
                value=(
                    f"**Reason:** {entry['reason']}\n"
                    f"**Issued By:** {entry['moderator']}\n"
                    f"**Date:** {time_str}"
                ),
                inline=False
            )

    embed.set_footer(text=f"Requested by {ctx.author}")
    await ctx.send(embed=embed)


@bot.command()
@_os_only_check()
async def clearstaffwarnings(ctx, member: discord.Member):
    """Clear a staff member's formal warnings. Usage: ,clearstaffwarnings @member"""
    guild_warns = STAFF_WARNINGS.setdefault(ctx.guild.id, {})
    count = len(guild_warns.get(member.id, []))
    guild_warns[member.id] = []
    _save_staff_warnings()

    embed = discord.Embed(
        title="✅ Staff Warnings Cleared",
        description=f"Cleared **{count}** staff warning(s) for {member.mention}.",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"Cleared by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)
    await log(ctx.guild, "staff", "Staff Warnings Cleared", None, discord.Color.green(),
              fields=[("👑 Cleared By", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("👤 Staff Member", f"{member.mention} (`{member.id}`)", True), ("🗑️ Warnings Removed", str(count), True)],
              actor=ctx.author, target=member)


@bot.command()
@_os_only_check()
async def clearstaffstrikes(ctx, member: discord.Member):
    """Clear a staff member's formal strikes. Usage: ,clearstaffstrikes @member"""
    guild_strikes = STAFF_STRIKES.setdefault(ctx.guild.id, {})
    count = len(guild_strikes.get(member.id, []))
    guild_strikes[member.id] = []
    _save_staff_strikes()

    embed = discord.Embed(
        title="✅ Staff Strikes Cleared",
        description=f"Cleared **{count}** staff strike(s) for {member.mention}.",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"Cleared by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)
    await log(ctx.guild, "staff", "Staff Strikes Cleared", None, discord.Color.green(),
              fields=[("👑 Cleared By", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("👤 Staff Member", f"{member.mention} (`{member.id}`)", True), ("🗑️ Strikes Removed", str(count), True)],
              actor=ctx.author, target=member)


# ============================================================
# MUTE SYSTEM
# ============================================================
@bot.command()
@_permitted_check(moderate_members=True)
async def mute(ctx, member: discord.Member, *, reason="No reason provided"):
    if member == ctx.author:
        await ctx.send("❌ You can't mute yourself.")
        return
    if member.top_role >= ctx.guild.me.top_role:
        await ctx.send("❌ I can't mute that user because their role is higher than mine.")
        return

    role = await get_or_create_muted_role(ctx.guild)
    if role in member.roles:
        await ctx.send("❌ That user is already muted.")
        return

    try:
        await member.add_roles(role, reason=f"Muted by {ctx.author} | {reason}")
        _log_mod_action(ctx.guild.id, member.id, "mute", ctx.author, reason)
        embed = discord.Embed(
            title="🔇 Member Muted",
            description=f"{member.mention} has been muted.",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_footer(text=f"Muted by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        await log(ctx.guild, "mutes", "Member Muted", None, discord.Color.red(),
                  fields=[("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("🔇 User", f"{member.mention} (`{member.id}`)", True), ("📝 Reason", reason, False)],
                  actor=ctx.author, target=member)

    except discord.Forbidden:
        await ctx.send(_role_forbidden_reason(ctx.guild))


@bot.command()
@_permitted_check(moderate_members=True)
async def unmute(ctx, member: discord.Member, *, reason="No reason provided"):
    role = discord.utils.get(ctx.guild.roles, name=MUTED_ROLE)
    if not role or role not in member.roles:
        await ctx.send("❌ That user is not muted.")
        return

    try:
        await member.remove_roles(role, reason=f"Unmuted by {ctx.author} | {reason}")
        _log_mod_action(ctx.guild.id, member.id, "unmute", ctx.author, reason)
        embed = discord.Embed(
            title="🔊 Member Unmuted",
            description=f"{member.mention} has been unmuted.",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Unmuted by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        await log(ctx.guild, "mutes", "Member Unmuted", None, discord.Color.green(),
                  fields=[("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("🔊 User", f"{member.mention} (`{member.id}`)", True), ("📝 Reason", reason, False)],
                  actor=ctx.author, target=member)

    except discord.Forbidden:
        await ctx.send(_role_forbidden_reason(ctx.guild))


@bot.command()
@_permitted_check(administrator=True)
async def setunmutevc(ctx, action: str = None, channel: discord.VoiceChannel = None):
    """
    Manage self-service unmute voice channels — a member who joins one
    instantly gets cleared of whichever of these apply, no staff needed:
    VC server-mute, server-deafen, AND the ,mute role (three separate
    things, all handled). They're then bounced back out so the channel's
    free for the next person. Register the actual VC(s) you've already
    created for this (create as many as you want — e.g. two duplicate
    "unmute me" VCs for capacity).

    Usage:
      ,setunmutevc list             — see registered unmute VCs
      ,setunmutevc add #channel     — register a voice channel
      ,setunmutevc remove #channel  — unregister one
    """
    guild = ctx.guild
    channels = UNMUTE_VC_CHANNELS.setdefault(guild.id, [])

    if action is None or action.lower() == "list":
        if channels:
            lines = []
            for cid in channels:
                ch = guild.get_channel(cid)
                lines.append(f"🔊 {ch.mention}" if ch else f"🔊 *deleted channel* (`{cid}`)")
            await ctx.send(embed=discord.Embed(
                title="🔊 Unmute VCs",
                description="\n".join(lines),
                color=discord.Color.blurple(),
                timestamp=discord.utils.utcnow()
            ))
        else:
            await ctx.send("📭 No unmute VCs registered yet. Use `,setunmutevc add #channel`.")
        return

    if channel is None:
        await ctx.send("❌ Provide a voice channel. Example: `,setunmutevc add #unmute-me`", delete_after=8)
        return

    if action.lower() == "add":
        if channel.id in channels:
            await ctx.send(f"❌ {channel.mention} is already registered.", delete_after=6)
            return
        channels.append(channel.id)
        _save_unmute_vc_channels()
        await ctx.send(f"✅ {channel.mention} is now a self-service unmute VC — joining it instantly clears VC server-mute/deafen and the Muted role.")
    elif action.lower() == "remove":
        if channel.id not in channels:
            await ctx.send(f"❌ {channel.mention} isn't registered.", delete_after=6)
            return
        channels.remove(channel.id)
        _save_unmute_vc_channels()
        await ctx.send(f"✅ {channel.mention} is no longer a self-service unmute VC.")
    else:
        await ctx.send("❌ Unknown action. Use `add`, `remove`, or `list`.", delete_after=8)


# ============================================================
# NICKNAME / CHANNEL VISIBILITY / SLOWMODE / PURGE / MASSROLE
# ============================================================
@bot.command()
@_permitted_check(manage_nicknames=True)
async def nickname(ctx, member: discord.Member, *, new_nick: str = None):
    if member.top_role >= ctx.guild.me.top_role:
        await ctx.send("❌ I can't change that user's nickname because their role is higher than mine.")
        return
    old_nick = member.nick or member.name
    try:
        await member.edit(nick=new_nick, reason=f"Nickname changed by {ctx.author}")
        embed = discord.Embed(
            title="✏ Nickname Updated",
            description=f"{member.mention}'s nickname is now **{new_nick or member.name}**.",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Changed by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        await log(ctx.guild, "nicknames", "Nickname Changed (cmd)", None, discord.Color.blurple(),
                  fields=[
                      ("👑 Staff",     f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                      ("👤 Member",    f"{member.mention} (`{member.id}`)",         True),
                      ("📝 Old Nick",  old_nick,                                    True),
                      ("📝 New Nick",  new_nick or member.name,                     True),
                  ],
                  actor=ctx.author, target=member)
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to change that member's nickname.")


@bot.command()
@_permitted_check(manage_channels=True)
async def hide(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, view_channel=False)
    await ctx.send(f"👻 {ctx.channel.mention} is now hidden from everyone.")
    await log(ctx.guild, "hides", "Channel Hidden", None, discord.Color.orange(),
              fields=[("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("👁️ Channel", ctx.channel.mention, True)],
              actor=ctx.author)


@bot.command()
@_permitted_check(manage_channels=True)
async def unhide(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, view_channel=True)
    await ctx.send(f"👀 {ctx.channel.mention} is now visible to everyone.")
    await log(ctx.guild, "hides", "Channel Unhidden", None, discord.Color.green(),
              fields=[("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("👁️ Channel", ctx.channel.mention, True)],
              actor=ctx.author)


@bot.command()
@_permitted_check(manage_channels=True)
async def slowmode(ctx, seconds: int, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    if seconds < 0 or seconds > 21600:
        await ctx.send("❌ Slowmode must be between 0 and 21600 seconds.")
        return
    await channel.edit(slowmode_delay=seconds)
    if seconds == 0:
        await ctx.send(f"✅ Slowmode disabled in {channel.mention}.")
    else:
        await ctx.send(f"🐌 Slowmode set to **{seconds}s** in {channel.mention}.")


@bot.command()
@_permitted_check(manage_messages=True)
async def purge(ctx, amount: int, member: discord.Member = None):
    if amount < 1 or amount > 500:
        await ctx.send("❌ Amount must be between 1 and 500.")
        return

    # Delete the invoking command message up front — when a member filter is
    # given and the invoker isn't that member, purge()'s check would skip it
    # anyway, leaving it behind and throwing off the reported count.
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass

    def check(msg):
        if member:
            return msg.author.id == member.id
        return True

    deleted = await ctx.channel.purge(limit=amount, check=check)
    deleted_count = len(deleted)

    confirmation = await ctx.send(f"🧹 Purged **{deleted_count}** message(s)" + (f" from {member.mention}." if member else "."))
    await confirmation.delete(delay=5)

    await log(
        ctx.guild,
        "purges",
        "Messages Purged",
        f"Moderator: {ctx.author.mention}\nChannel: {ctx.channel.mention}\nAmount: {deleted_count}" + (f"\nFiltered User: {member.mention}" if member else ""),
        discord.Color.orange()
    )


@bot.command()
@_permitted_check(administrator=True)
async def massrole(ctx, role: discord.Role, filter_role: discord.Role = None):
    if role >= ctx.guild.me.top_role:
        await ctx.send("❌ I can't assign a role higher than or equal to my own top role.")
        return

    count = 0
    for member in ctx.guild.members:
        if member.bot or role in member.roles:
            continue
        if filter_role and filter_role not in member.roles:
            continue
        try:
            await member.add_roles(role, reason=f"Massrole by {ctx.author}")
            count += 1
        except (discord.Forbidden, discord.HTTPException):
            pass

    scope = f"members with {filter_role.mention}" if filter_role else "all members"
    await ctx.send(f"✅ Gave **{role.name}** to **{count}** {scope}.")
    await log(ctx.guild, "massroles", "Mass Role Added", None, discord.Color.blue(),
              fields=[("👑 Admin", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("🏷️ Role", f"{role.mention}", True), ("🎯 Scope", scope, True), ("👥 Affected", str(count), True)],
              actor=ctx.author)


@bot.command()
@_permitted_check(administrator=True)
async def massunrole(ctx, role: discord.Role, filter_role: discord.Role = None):
    count = 0
    for member in ctx.guild.members:
        if role not in member.roles:
            continue
        if filter_role and filter_role not in member.roles:
            continue
        try:
            await member.remove_roles(role, reason=f"Massunrole by {ctx.author}")
            count += 1
        except (discord.Forbidden, discord.HTTPException):
            pass

    scope = f"members with {filter_role.mention}" if filter_role else "all members"
    await ctx.send(f"✅ Removed **{role.name}** from **{count}** {scope}.")
    await log(ctx.guild, "massroles", "Mass Role Removed", None, discord.Color.blue(),
              fields=[("👑 Admin", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("🏷️ Role", f"{role.mention}", True), ("🎯 Scope", scope, True), ("👥 Affected", str(count), True)],
              actor=ctx.author)


# ============================================================
# SERVER BACKUP / RESTORE
# ============================================================

BACKUP_DIR = "backups"
os.makedirs(BACKUP_DIR, exist_ok=True)


def _backup_path(guild_id: int, label: str) -> str:
    return os.path.join(BACKUP_DIR, f"{guild_id}_{label}.json")


def _serialize_overwrites(overwrites: dict) -> list:
    result = []
    for target, overwrite in overwrites.items():
        allow, deny = overwrite.pair()
        result.append({
            "id": target.id,
            "type": "role" if isinstance(target, discord.Role) else "member",
            "allow": allow.value,
            "deny": deny.value,
        })
    return result


async def _take_backup(guild: discord.Guild) -> dict:
    """Snapshot the entire server structure into a dict."""
    data = {
        "taken_at": datetime.utcnow().isoformat(),
        "guild_id": guild.id,
        "guild_name": guild.name,
        "settings": {
            "name": guild.name,
            "description": guild.description,
            "verification_level": guild.verification_level.value,
            "default_notifications": guild.default_notifications.value,
            "afk_timeout": guild.afk_timeout,
        },
        "roles": [],
        "categories": [],
        "text_channels": [],
        "voice_channels": [],
        "bans": [],
    }

    # Roles (skip @everyone)
    for role in sorted(guild.roles, key=lambda r: r.position):
        if role.is_default():
            continue
        data["roles"].append({
            "id": role.id,
            "name": role.name,
            "color": role.color.value,
            "hoist": role.hoist,
            "mentionable": role.mentionable,
            "permissions": role.permissions.value,
            "position": role.position,
            "managed": role.managed,
        })

    # Categories
    for cat in sorted(guild.categories, key=lambda c: c.position):
        data["categories"].append({
            "id": cat.id,
            "name": cat.name,
            "position": cat.position,
            "overwrites": _serialize_overwrites(cat.overwrites),
        })

    # Text channels
    for ch in guild.text_channels:
        data["text_channels"].append({
            "id": ch.id,
            "name": ch.name,
            "topic": ch.topic,
            "nsfw": ch.nsfw,
            "slowmode_delay": ch.slowmode_delay,
            "position": ch.position,
            "category_id": ch.category_id,
            "overwrites": _serialize_overwrites(ch.overwrites),
        })

    # Voice channels
    for ch in guild.voice_channels:
        data["voice_channels"].append({
            "id": ch.id,
            "name": ch.name,
            "bitrate": ch.bitrate,
            "user_limit": ch.user_limit,
            "position": ch.position,
            "category_id": ch.category_id,
            "overwrites": _serialize_overwrites(ch.overwrites),
        })

    # Bans
    try:
        async for ban_entry in guild.bans():
            data["bans"].append({
                "user_id": ban_entry.user.id,
                "reason": ban_entry.reason,
            })
    except discord.Forbidden:
        pass

    return data


def _resolve_overwrites(data_list: list, guild: discord.Guild) -> dict:
    """Rebuild an overwrites dict from serialised data."""
    overwrites = {}
    for entry in data_list:
        target_id = entry["id"]
        allow = discord.Permissions(entry["allow"])
        deny = discord.Permissions(entry["deny"])
        overwrite = discord.PermissionOverwrite.from_pair(allow, deny)

        if entry["type"] == "role":
            target = guild.get_role(target_id)
        else:
            target = guild.get_member(target_id)

        if target:
            overwrites[target] = overwrite
    return overwrites


class RestoreConfirmView(discord.ui.View):
    def __init__(self, author_id: int, backup_data: dict):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.backup_data = backup_data
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Only the person who ran the command can confirm.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Yes, Restore", style=discord.ButtonStyle.danger, custom_id="restore_confirm")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self.stop()
        await interaction.response.edit_message(
            content="⏳ Restoring server... this may take a while.",
            view=None
        )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary, custom_id="restore_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="❌ Restore cancelled.", view=None)


async def _apply_restore(guild: discord.Guild, data: dict, status_channel: discord.TextChannel):
    """Rebuild guild structure from backup data. Deletes existing channels/roles first."""
    errors = []

    async def note(msg):
        try:
            await status_channel.send(msg)
        except Exception:
            pass

    await note("🔄 **Step 1/5** — Restoring server settings...")
    settings = data.get("settings", {})
    try:
        await guild.edit(
            name=settings.get("name", guild.name),
            verification_level=discord.VerificationLevel(settings.get("verification_level", 0)),
            default_notifications=discord.NotificationLevel(settings.get("default_notifications", 0)),
            afk_timeout=settings.get("afk_timeout", 300),
        )
    except (discord.Forbidden, discord.HTTPException) as e:
        errors.append(f"Settings: {e}")

    await note("🔄 **Step 2/5** — Recreating roles...")
    # Build a map of old_id → new_role for use in overwrite resolution
    role_id_map = {}
    existing_roles = {r.name: r for r in guild.roles}

    for role_data in data.get("roles", []):
        if role_data.get("managed"):
            continue  # bot/integration roles can't be created
        name = role_data["name"]
        if name in existing_roles:
            role_id_map[role_data["id"]] = existing_roles[name]
            continue
        try:
            new_role = await guild.create_role(
                name=name,
                color=discord.Color(role_data["color"]),
                hoist=role_data["hoist"],
                mentionable=role_data["mentionable"],
                permissions=discord.Permissions(role_data["permissions"]),
                reason="Server restore",
            )
            role_id_map[role_data["id"]] = new_role
        except (discord.Forbidden, discord.HTTPException) as e:
            errors.append(f"Role {name}: {e}")

    await note("🔄 **Step 3/5** — Recreating categories...")
    # Map old category_id → new Category
    cat_id_map = {}
    existing_cats = {c.name: c for c in guild.categories}

    def _build_overwrites_from_data(ow_list):
        ows = {}
        for entry in ow_list:
            allow = discord.Permissions(entry["allow"])
            deny = discord.Permissions(entry["deny"])
            ow = discord.PermissionOverwrite.from_pair(allow, deny)
            if entry["type"] == "role":
                target = role_id_map.get(entry["id"]) or guild.get_role(entry["id"])
            else:
                target = guild.get_member(entry["id"])
            if target:
                ows[target] = ow
        return ows

    for cat_data in sorted(data.get("categories", []), key=lambda c: c["position"]):
        name = cat_data["name"]
        if name in existing_cats:
            cat_id_map[cat_data["id"]] = existing_cats[name]
            continue
        try:
            overwrites = _build_overwrites_from_data(cat_data.get("overwrites", []))
            new_cat = await guild.create_category(
                name=name,
                overwrites=overwrites,
                reason="Server restore",
            )
            cat_id_map[cat_data["id"]] = new_cat
        except (discord.Forbidden, discord.HTTPException) as e:
            errors.append(f"Category {name}: {e}")

    await note("🔄 **Step 4/5** — Recreating channels...")
    existing_text = {c.name: c for c in guild.text_channels}
    existing_voice = {c.name: c for c in guild.voice_channels}

    for ch_data in sorted(data.get("text_channels", []), key=lambda c: c["position"]):
        name = ch_data["name"]
        if name in existing_text:
            continue
        try:
            category = cat_id_map.get(ch_data.get("category_id"))
            overwrites = _build_overwrites_from_data(ch_data.get("overwrites", []))
            await guild.create_text_channel(
                name=name,
                topic=ch_data.get("topic"),
                nsfw=ch_data.get("nsfw", False),
                slowmode_delay=ch_data.get("slowmode_delay", 0),
                category=category,
                overwrites=overwrites,
                reason="Server restore",
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            errors.append(f"Text channel #{name}: {e}")

    for ch_data in sorted(data.get("voice_channels", []), key=lambda c: c["position"]):
        name = ch_data["name"]
        if name in existing_voice:
            continue
        try:
            category = cat_id_map.get(ch_data.get("category_id"))
            overwrites = _build_overwrites_from_data(ch_data.get("overwrites", []))
            await guild.create_voice_channel(
                name=name,
                bitrate=min(ch_data.get("bitrate", 64000), guild.bitrate_limit),
                user_limit=ch_data.get("user_limit", 0),
                category=category,
                overwrites=overwrites,
                reason="Server restore",
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            errors.append(f"Voice channel {name}: {e}")

    await note("🔄 **Step 5/5** — Re-applying bans...")
    for ban_data in data.get("bans", []):
        try:
            user = await bot.fetch_user(ban_data["user_id"])
            await guild.ban(user, reason=ban_data.get("reason") or "Restored from backup")
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass

    # Done
    embed = discord.Embed(
        title="✅ Server Restore Complete",
        description=(
            f"Restored from backup taken **{data.get('taken_at', 'unknown')}**.\n\n"
            f"**Roles restored:** {len(data.get('roles', []))}\n"
            f"**Categories restored:** {len(data.get('categories', []))}\n"
            f"**Text channels restored:** {len(data.get('text_channels', []))}\n"
            f"**Voice channels restored:** {len(data.get('voice_channels', []))}\n"
            f"**Bans re-applied:** {len(data.get('bans', []))}"
        ),
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    if errors:
        embed.add_field(
            name=f"⚠️ {len(errors)} error(s)",
            value="\n".join(f"• {e}" for e in errors[:10]),
            inline=False
        )
    embed.set_footer(text=f"TrapAI Restore System • {guild.name}")
    await status_channel.send(embed=embed)


@bot.command()
@_permitted_check(administrator=True)
async def backup(ctx, *, label: str = None):
    """Take a snapshot of the server structure.
    Optional label: ,backup pre-raid"""
    await ctx.send("📸 Taking backup snapshot...")

    data = await _take_backup(ctx.guild)
    label = (label or datetime.utcnow().strftime("%Y%m%d-%H%M%S")).replace(" ", "_")[:40]
    path = _backup_path(ctx.guild.id, label)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    embed = discord.Embed(
        title="✅ Server Backup Created",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="Label", value=f"`{label}`", inline=True)
    embed.add_field(name="Taken At", value=data["taken_at"][:19].replace("T", " ") + " UTC", inline=True)
    embed.add_field(name="Roles", value=str(len(data["roles"])), inline=True)
    embed.add_field(name="Categories", value=str(len(data["categories"])), inline=True)
    embed.add_field(name="Text Channels", value=str(len(data["text_channels"])), inline=True)
    embed.add_field(name="Voice Channels", value=str(len(data["voice_channels"])), inline=True)
    embed.add_field(name="Bans", value=str(len(data["bans"])), inline=True)
    embed.add_field(
        name="📌 To restore this backup",
        value=f"`,restore {label}`",
        inline=False
    )
    embed.set_footer(text=f"TrapAI Backup System • {ctx.guild.name} • By {ctx.author}")
    await ctx.send(embed=embed)

    await log(
        ctx.guild,
        "mod",
        "Server Backup Created",
        f"Administrator: {ctx.author.mention}\nLabel: `{label}`",
        discord.Color.green()
    )


@bot.command()
@_permitted_check(administrator=True)
async def listbackups(ctx):
    """List all available backups for this server."""
    files = [
        f for f in os.listdir(BACKUP_DIR)
        if f.startswith(f"{ctx.guild.id}_") and f.endswith(".json")
    ]

    if not files:
        await ctx.send("📂 No backups found for this server. Use `,backup` to create one.")
        return

    embed = discord.Embed(
        title="📂 Server Backups",
        description=f"**{len(files)}** backup(s) found for **{ctx.guild.name}**",
        color=discord.Color.dark_grey(),
        timestamp=discord.utils.utcnow()
    )

    lines = []
    for fname in sorted(files):
        label = fname[len(str(ctx.guild.id)) + 1:-5]
        path = os.path.join(BACKUP_DIR, fname)
        size_kb = round(os.path.getsize(path) / 1024, 1)
        # Try to read taken_at from the file
        try:
            with open(path, encoding="utf-8") as f:
                taken = json.load(f).get("taken_at", "?")[:19].replace("T", " ")
        except Exception:
            taken = "?"
        lines.append(f"**`{label}`** — {taken} UTC ({size_kb}KB)")

    embed.add_field(name="Backups", value="\n".join(lines), inline=False)
    embed.add_field(
        name="Commands",
        value="`,restore <label>` — restore a backup\n`,deletebackup <label>` — delete a backup",
        inline=False
    )
    embed.set_footer(text=f"TrapAI Backup System • {ctx.guild.name}")
    await ctx.send(embed=embed)


@bot.command()
@_permitted_check(administrator=True)
async def restore(ctx, *, label: str):
    """Restore the server from a backup. Adds missing channels/roles — does NOT delete existing ones.
    Usage: ,restore pre-raid"""
    path = _backup_path(ctx.guild.id, label.replace(" ", "_"))

    if not os.path.exists(path):
        await ctx.send(f"❌ No backup found with label `{label}`. Use `,listbackups` to see available backups.")
        return

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    taken_at = data.get("taken_at", "unknown")[:19].replace("T", " ")

    embed = discord.Embed(
        title="⚠️ Confirm Server Restore",
        description=(
            f"You are about to restore **{ctx.guild.name}** from backup:\n\n"
            f"**Label:** `{label}`\n"
            f"**Taken:** {taken_at} UTC\n"
            f"**Roles in backup:** {len(data.get('roles', []))}\n"
            f"**Categories in backup:** {len(data.get('categories', []))}\n"
            f"**Text channels:** {len(data.get('text_channels', []))}\n"
            f"**Voice channels:** {len(data.get('voice_channels', []))}\n"
            f"**Bans:** {len(data.get('bans', []))}\n\n"
            "⚠️ This will **add** missing roles and channels.\n"
            "Existing roles/channels with the same name are kept as-is.\n"
            "Server settings will be overwritten.\n\n"
            "**Are you sure?**"
        ),
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"TrapAI Restore System • {ctx.guild.name}")

    view = RestoreConfirmView(ctx.author.id, data)
    msg = await ctx.send(embed=embed, view=view)
    await view.wait()

    if not view.confirmed:
        return

    await log(
        ctx.guild,
        "mod",
        "Server Restore Started",
        f"Administrator: {ctx.author.mention}\nLabel: `{label}`\nBackup taken: {taken_at} UTC",
        discord.Color.orange()
    )

    await _apply_restore(ctx.guild, data, ctx.channel)

    await log(
        ctx.guild,
        "mod",
        "Server Restore Completed",
        f"Administrator: {ctx.author.mention}\nLabel: `{label}`",
        discord.Color.green()
    )


@bot.command()
@_permitted_check(administrator=True)
async def deletebackup(ctx, *, label: str):
    """Delete a saved backup. Usage: ,deletebackup pre-raid"""
    path = _backup_path(ctx.guild.id, label.replace(" ", "_"))

    if not os.path.exists(path):
        await ctx.send(f"❌ No backup found with label `{label}`.")
        return

    os.remove(path)

    embed = discord.Embed(
        title="🗑️ Backup Deleted",
        description=f"Backup `{label}` has been deleted.",
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"TrapAI Backup System • {ctx.guild.name}")
    await ctx.send(embed=embed)

    await log(
        ctx.guild,
        "mod",
        "Server Backup Deleted",
        f"Administrator: {ctx.author.mention}\nLabel: `{label}`",
        discord.Color.orange()
    )


# ============================================================
# MILESTONE COMMANDS
# ============================================================

@bot.command()
@_permitted_check(administrator=True)
async def setmilestone(ctx, channel: discord.TextChannel = None):
    """Set the channel where milestone announcements are posted.
    Usage: ,setmilestone #announcements
    Run with no argument to clear the override and use the default 'announcements' channel."""
    guild = ctx.guild

    if channel is None:
        _milestone_channel_overrides.pop(guild.id, None)
        _save_milestones()
        embed = discord.Embed(
            title="🎯 Milestone Channel Reset",
            description=(
                f"Milestone announcements will now post in any channel named "
                f"**`{ANNOUNCEMENTS_CHANNEL}`**.\n\n"
                "If no such channel exists, the bot will fall back to the first text channel it can write to."
            ),
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow()
        )
    else:
        _milestone_channel_overrides[guild.id] = channel.id
        _save_milestones()
        embed = discord.Embed(
            title="✅ Milestone Channel Set",
            description=(
                f"Milestone announcements will now be posted in {channel.mention}.\n\n"
                f"Use `,testmilestone` to preview how a milestone looks."
            ),
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Channel", value=channel.mention, inline=True)

    embed.set_footer(text=f"TrapAI • {ctx.guild.name} Milestones")
    await ctx.send(embed=embed)


@bot.command()
@_permitted_check(administrator=True)
async def testmilestone(ctx):
    """Send a preview milestone announcement in the configured channel."""
    guild = ctx.guild
    count = guild.member_count

    # Temporarily force the current count to be treated as a milestone
    prev = _last_milestone_fired.get(guild.id)
    _last_milestone_fired.pop(guild.id, None)

    # Override the milestone set temporarily to include current count
    original_milestones = set(MEMBER_MILESTONES)
    MEMBER_MILESTONES.add(count)

    await _check_milestone(guild)

    # Restore
    MEMBER_MILESTONES.discard(count)
    MEMBER_MILESTONES.update(original_milestones)
    if prev is not None:
        _last_milestone_fired[guild.id] = prev

    await ctx.send(
        f"✅ Test milestone sent for **{count:,} members** to the configured announcements channel.",
        delete_after=10
    )


@bot.command()
@_permitted_check(administrator=True)
async def milestones(ctx):
    """Show all configured milestones and the current announcement channel."""
    guild = ctx.guild
    count = guild.member_count
    next_m = _next_milestone(count)

    override_id = _milestone_channel_overrides.get(guild.id)
    if override_id:
        ch = guild.get_channel(override_id)
        ch_str = ch.mention if ch else f"*(deleted — ID {override_id})*"
    else:
        ch = discord.utils.get(guild.text_channels, name=ANNOUNCEMENTS_CHANNEL)
        ch_str = ch.mention if ch else f"*(no channel named `{ANNOUNCEMENTS_CHANNEL}` found)*"

    sorted_milestones = sorted(MEMBER_MILESTONES)
    past = [m for m in sorted_milestones if m <= count]
    upcoming = [m for m in sorted_milestones if m > count]

    embed = discord.Embed(
        title="🎯 Member Milestones",
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="📊 Current Members", value=f"**{count:,}**", inline=True)
    embed.add_field(name="🎯 Next Milestone", value=f"**{next_m:,}**", inline=True)
    embed.add_field(name="📢 Announcements Channel", value=ch_str, inline=False)
    embed.add_field(
        name=f"✅ Reached ({len(past)})",
        value=", ".join(f"**{m:,}**" for m in past[-10:]) or "None yet",
        inline=False
    )
    embed.add_field(
        name=f"🔜 Upcoming ({len(upcoming)})",
        value=", ".join(f"**{m:,}**" for m in upcoming[:15]) or "All done!",
        inline=False
    )
    embed.add_field(
        name="⚙️ Commands",
        value=(
            "`,setmilestone #channel` — set announcement channel\n"
            "`,setmilestone` — reset to default\n"
            "`,testmilestone` — preview a milestone now"
        ),
        inline=False
    )
    embed.set_footer(text=f"TrapAI • {ctx.guild.name} Milestones")
    await ctx.send(embed=embed)


# ============================================================
# BIRTHDAY COMMANDS
# ============================================================

@bot.command()
async def setbirthday(ctx, *, date: str = None):
    """
    Set your birthday. Usage: ,setbirthday <month-day>
    Accepts: 03-15, 3/15, "March 15", "Mar 15", "15 March"
    """
    if not date:
        await ctx.send("❌ Usage: `,setbirthday <month-day>` — e.g. `,setbirthday 03-15` or `,setbirthday March 15`", delete_after=10)
        return
    md = _parse_birthday(date)
    if not md:
        await ctx.send("❌ Couldn't understand that date. Try `,setbirthday 03-15` or `,setbirthday March 15`.", delete_after=10)
        return

    BIRTHDAYS.setdefault(ctx.guild.id, {})[ctx.author.id] = md
    _save_birthdays()

    has_tz = ctx.author.id in BIRTHDAY_TIMEZONES.get(ctx.guild.id, {})
    tz_note = "" if has_tz else "\n⏰ You haven't set a timezone yet — use `,settimezone <offset>` (e.g. `,settimezone -5`) so it announces at YOUR midnight, not UTC."
    pretty = datetime.strptime(f"{md} 2000", "%m-%d %Y").strftime("%B %d")
    embed = discord.Embed(
        title="🎂 Birthday Set",
        description=f"Your birthday has been set to **{pretty}**.\nWe'll announce it in {_resolve_birthday_channel(ctx.guild).mention if _resolve_birthday_channel(ctx.guild) else '#announcements'} when it comes around!{tz_note}",
        color=discord.Color.from_rgb(255, 105, 180),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command()
async def removebirthday(ctx):
    """Remove your saved birthday. Usage: ,removebirthday"""
    removed = BIRTHDAYS.get(ctx.guild.id, {}).pop(ctx.author.id, None)
    if removed is None:
        await ctx.send("❌ You don't have a birthday set.", delete_after=6)
        return
    _save_birthdays()
    await ctx.send("✅ Your birthday has been removed.", delete_after=6)


@bot.command(aliases=["mytimezone"])
async def settimezone(ctx, *, offset: str = None):
    """
    Set your timezone so birthday announcements fire at YOUR local midnight.
    Usage: ,settimezone <state or offset>
      — a US state or 2-letter abbreviation: California, TX, New York, fl
      — or a raw UTC offset: -5, +8, UTC-5, GMT+5:30
    """
    if not offset:
        current = BIRTHDAY_TIMEZONES.get(ctx.guild.id, {}).get(ctx.author.id)
        if current is None:
            await ctx.send("❌ Usage: `,settimezone <state or offset>` — e.g. `,settimezone California` or `,settimezone -5`", delete_after=10)
        else:
            await ctx.send(f"🕐 Your timezone is currently set to **{_format_utc_offset(current)}**. Use `,settimezone <state or offset>` to change it.")
        return

    matched_state = None
    parsed = _parse_state_timezone(offset)
    if parsed is not None:
        matched_state = offset.strip()
    else:
        parsed = _parse_utc_offset(offset)

    if parsed is None:
        await ctx.send(
            "❌ Couldn't understand that. Try a US state (`,settimezone California`, `,settimezone TX`) "
            "or a raw offset (`,settimezone -5`, `,settimezone UTC+5:30`).",
            delete_after=12
        )
        return

    BIRTHDAY_TIMEZONES.setdefault(ctx.guild.id, {})[ctx.author.id] = parsed
    _save_birthday_timezones()

    zone_label = US_ZONE_LABELS.get(parsed)
    matched_note = f" ({zone_label} — matched from **{matched_state}**)" if matched_state and zone_label else ""
    embed = discord.Embed(
        title="🕐 Timezone Set",
        description=(
            f"Your timezone is now set to **{_format_utc_offset(parsed)}**{matched_note}.\n"
            "Your birthday will now be announced at midnight in your own timezone."
        ),
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command()
async def birthday(ctx, member: discord.Member = None):
    """Show a member's saved birthday. Usage: ,birthday [@user]"""
    member = member or ctx.author
    md = BIRTHDAYS.get(ctx.guild.id, {}).get(member.id)
    if not md:
        await ctx.send(f"📭 {member.mention} doesn't have a birthday set." if member != ctx.author else "📭 You don't have a birthday set. Use `,setbirthday <month-day>` to add one.")
        return
    pretty = datetime.strptime(f"{md} 2000", "%m-%d %Y").strftime("%B %d")
    tz = BIRTHDAY_TIMEZONES.get(ctx.guild.id, {}).get(member.id)
    tz_str = _format_utc_offset(tz) if tz is not None else "UTC (default — not set)"
    embed = discord.Embed(
        title="🎂 Birthday",
        description=f"{member.mention}'s birthday is **{pretty}**.",
        color=discord.Color.from_rgb(255, 105, 180),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="🕐 Timezone", value=tz_str, inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(aliases=["birthdays"])
async def birthdaylist(ctx):
    """List all upcoming birthdays in the server. Usage: ,birthdaylist"""
    guild_bdays = BIRTHDAYS.get(ctx.guild.id, {})
    if not guild_bdays:
        await ctx.send("📭 No birthdays have been set yet. Use `,setbirthday <month-day>` to add yours!")
        return

    utc_now = discord.utils.utcnow()

    def _safe_date(base, year, m, d):
        try:
            return base.replace(year=year, month=m, day=d)
        except ValueError:
            # Feb 29 in a non-leap year — celebrate on Feb 28 instead
            return base.replace(year=year, month=2, day=28)

    def _days_until(md_str, offset):
        # Count down in the member's own local time, same clock the
        # announcement loop uses, so this matches when they'll actually get pinged.
        local_now = utc_now + timedelta(hours=offset)
        local_today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        m, d = map(int, md_str.split("-"))
        target = _safe_date(local_today, local_today.year, m, d)
        if target < local_today:
            target = _safe_date(local_today, local_today.year + 1, m, d)
        return (target - local_today).days

    entries = []
    for uid, md in guild_bdays.items():
        member = ctx.guild.get_member(uid)
        if not member:
            continue
        offset = _resolve_birthday_timezone(ctx.guild.id, uid)
        entries.append((member, md, _days_until(md, offset), offset))
    entries.sort(key=lambda e: e[2])

    lines = []
    for member, md, days, offset in entries[:25]:
        pretty = datetime.strptime(f"{md} 2000", "%m-%d %Y").strftime("%B %d")
        when = "🎉 **Today!**" if days == 0 else f"in {days} day(s)"
        tz_note = f" · {_format_utc_offset(offset)}" if offset else ""
        lines.append(f"🎂 {member.mention} — **{pretty}** ({when}{tz_note})")

    embed = discord.Embed(
        title="🎂 Upcoming Birthdays",
        description="\n".join(lines) or "No members with birthdays are currently in the server.",
        color=discord.Color.from_rgb(255, 105, 180),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"TrapAI Birthday Tracker • {ctx.guild.name}")
    await ctx.send(embed=embed)


@bot.command()
@_permitted_check(administrator=True)
async def setbirthdaychannel(ctx, channel: discord.TextChannel = None):
    """
    Set the channel birthday announcements are posted to.
    Usage: ,setbirthdaychannel #channel
    Run with no argument to reset to the default 'announcements' channel.
    """
    guild = ctx.guild
    if channel is None:
        BIRTHDAY_CHANNEL_OVERRIDES.pop(guild.id, None)
        _save_birthday_channels()
        embed = discord.Embed(
            title="🎂 Birthday Channel Reset",
            description=f"Birthday announcements will now post in any channel named **`{ANNOUNCEMENTS_CHANNEL}`**.",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow()
        )
    else:
        BIRTHDAY_CHANNEL_OVERRIDES[guild.id] = channel.id
        _save_birthday_channels()
        embed = discord.Embed(
            title="✅ Birthday Channel Set",
            description=f"Birthday announcements will now be posted in {channel.mention}.",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
    embed.set_footer(text=f"TrapAI • {guild.name} Birthdays")
    await ctx.send(embed=embed)


@bot.command(aliases=["setmemberrole"])
@_permitted_check(administrator=True)
async def setgifrole(ctx, role: discord.Role = None):
    """
    Set which role is exempt from automod's link filter for GIFs — scoped
    to THIS server only, since every server can use a different role name.
    Usage: ,setgifrole @role
    Run with no argument to reset to the default (a role literally named
    the same as this bot's built-in VERIFIED_ROLE, if one exists here).
    """
    guild = ctx.guild
    if role is None:
        GIF_EXEMPT_ROLE.pop(guild.id, None)
        _save_gif_exempt_role()
        fallback = discord.utils.get(guild.roles, name=VERIFIED_ROLE)
        embed = discord.Embed(
            title="↩️ GIF-Exempt Role Reset",
            description=(
                f"Reset for **this server**. GIF exemption now falls back to "
                f"{fallback.mention if fallback else f'a role named `{VERIFIED_ROLE}` (not found here)'}."
            ),
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow()
        )
    else:
        GIF_EXEMPT_ROLE[guild.id] = role.id
        _save_gif_exempt_role()
        embed = discord.Embed(
            title="✅ GIF-Exempt Role Set",
            description=f"Members with {role.mention} can now post GIFs without automod deleting them — scoped to **this server only**.",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
    embed.set_footer(text=f"Set by {ctx.author} • TrapAI", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)
    await log(guild, "roles", "GIF-Exempt Role Changed", None, discord.Color.blurple(),
              fields=[
                  ("👑 Staff", f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("🏷️ Role",  role.mention if role else "*Reset to default*", True),
              ],
              actor=ctx.author)


# ============================================================
# VANITY URL ROLE TRACKING
# ============================================================

@bot.command()
@_permitted_check(administrator=True)
async def setvanityrole(ctx, role: discord.Role = None):
    """
    Mark this server's designated rep-reward role, for reference in
    ,vanityconfig. Staff grant and remove it manually — there is no
    automatic status-based tracking. Scoped to THIS server only. Run
    with no argument to clear it.
    Usage: ,setvanityrole @role
    """
    guild = ctx.guild
    if role is None:
        VANITY_ROLE.pop(guild.id, None)
        _save_vanity_role()
        embed = discord.Embed(
            title="↩️ Vanity Role Cleared",
            description="No rep-reward role is designated for this server anymore.",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow()
        )
    else:
        VANITY_ROLE[guild.id] = role.id
        _save_vanity_role()
        embed = discord.Embed(
            title="✅ Vanity Role Set",
            description=(
                f"{role.mention} is now marked as this server's rep-reward role. "
                f"Staff still have to grant and remove it manually — this is just for reference."
            ),
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
    embed.set_footer(text=f"Set by {ctx.author} • TrapAI", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command()
@_permitted_check(administrator=True)
async def setvanitycode(ctx, code: str = None):
    """
    Manually set/override the vanity invite code to look for in members'
    statuses (the part after discord.gg/). Only needed if this server
    doesn't have Discord's native boosted vanity URL. Scoped to THIS
    server only. Run with no argument to clear the override and fall
    back to this server's native vanity URL (if any).
    Usage: ,setvanitycode glock30
    """
    guild = ctx.guild
    if code is None:
        VANITY_CODE_OVERRIDE.pop(guild.id, None)
        _save_vanity_code_override()
        native = guild.vanity_url_code
        embed = discord.Embed(
            title="↩️ Vanity Code Override Cleared",
            description=(
                f"Now using this server's native vanity URL: `discord.gg/{native}`" if native
                else "This server has no native vanity URL, so vanity tracking has nothing to match until a code is set again."
            ),
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow()
        )
    else:
        code = code.strip().lstrip("/").replace("discord.gg/", "").lower()
        VANITY_CODE_OVERRIDE[guild.id] = code
        _save_vanity_code_override()
        embed = discord.Embed(
            title="✅ Vanity Code Set",
            description=f"Now tracking `discord.gg/{code}` in members' statuses for this server.",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
    embed.set_footer(text=f"Set by {ctx.author} • TrapAI", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command()
@_permitted_check(manage_guild=True)
async def vanityconfig(ctx):
    """Show this server's current vanity role configuration. Usage: ,vanityconfig"""
    guild = ctx.guild
    role_id = VANITY_ROLE.get(guild.id)
    role = guild.get_role(role_id) if role_id else None
    code = _resolve_vanity_code(guild)
    override = VANITY_CODE_OVERRIDE.get(guild.id)

    embed = discord.Embed(
        title="💎 Vanity Role Configuration",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="🎭 Reward Role", value=role.mention if role else "*Not set — use `,setvanityrole`*", inline=True)
    embed.add_field(name="🔗 Tracked Code", value=f"`discord.gg/{code}`" if code else "*None found or set*", inline=True)
    embed.add_field(
        name="📌 Source",
        value=("Manual override (`,setvanitycode`)" if override else
               "Native server vanity URL" if guild.vanity_url_code else "*Not configured*"),
        inline=True
    )
    embed.set_footer(text=f"{guild.name} • TrapAI Vanity System")
    await ctx.send(embed=embed)


@bot.command()
@_permitted_check(administrator=True)
async def exportconfig(ctx):
    """Dump a readable summary of everything configured for THIS server. Usage: ,exportconfig"""
    guild = ctx.guild

    protected = PROTECTED_ROLES.get(guild.id, set())
    protected_txt = ", ".join(sorted(
        (guild.get_role(rid).mention for rid in protected if guild.get_role(rid)), key=str
    )) or "*None set*"

    auto_ids = AUTOROLE.get(guild.id, [])
    autorole_txt = ", ".join(
        r.mention for rid in auto_ids if (r := guild.get_role(rid))
    ) or "*None set*"

    overrides = LOG_CHANNEL_OVERRIDES.get(guild.id, {})
    if overrides:
        log_txt = ", ".join(
            f"`{key}`→{ch.mention}" for key, cid in overrides.items() if (ch := guild.get_channel(cid))
        ) or "*None resolved*"
    else:
        log_txt = "*Using default channel names (no overrides set)*"

    custom_tickets = GUILD_TICKET_TYPES.get(guild.id, {})
    ticket_txt = (
        f"**{len(DEFAULT_TICKET_TYPES)}** default categories + **{len(custom_tickets)}** custom "
        f"({', '.join(custom_tickets.keys()) if custom_tickets else 'none'})"
    )

    gif_role = _resolve_gif_exempt_role(guild)
    gif_txt = gif_role.mention if gif_role else "*Not resolved*"

    bday_channel_id = BIRTHDAY_CHANNEL_OVERRIDES.get(guild.id)
    bday_channel = guild.get_channel(bday_channel_id) if bday_channel_id else None
    bday_txt = bday_channel.mention if bday_channel else f"*Using #{ANNOUNCEMENTS_CHANNEL} (default)*"

    wcfg = WELCOME_CONFIG.get(guild.id, {})
    w_channel = guild.get_channel(wcfg.get("channel_id")) if wcfg.get("channel_id") else None
    welcome_txt = f"{w_channel.mention if w_channel else f'*#{WELCOME_CHANNEL} (default)*'} • {'Enabled' if wcfg.get('enabled', True) else 'Disabled'}"

    invite_txt = _resolve_invite_link(guild) or "*Not set — no vanity code configured*"

    vrole_id = VANITY_ROLE.get(guild.id)
    vrole = guild.get_role(vrole_id) if vrole_id else None
    vcode = _resolve_vanity_code(guild)
    vanity_txt = f"{vrole.mention if vrole else '*Role not set*'} for `discord.gg/{vcode}`" if vrole_id else "*Not configured*"

    active_polls = sum(1 for p in POLLS.values() if p["guild_id"] == guild.id and not p.get("closed"))

    backup_id = VERIFY_BACKUP_GUILD.get(guild.id)
    verify_txt = (
        f"{'🟢 Real OAuth' if OAUTH_VERIFY_ENABLED else '🟡 In-Discord button (OAuth not configured)'}"
        + (f" • backup server `{backup_id}`" if backup_id else "")
    )

    embed = discord.Embed(
        title=f"📋 Server Config Export — {guild.name}",
        description=f"Full snapshot of this bot's configuration for **{guild.name}** (`{guild.id}`).",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="🛡️ Protected Roles",       value=protected_txt[:1000],   inline=False)
    embed.add_field(name="🎭 Autorole",               value=autorole_txt[:1000],    inline=False)
    embed.add_field(name="🪵 Log Channel Overrides",  value=log_txt[:1000],         inline=False)
    embed.add_field(name="🎫 Ticket Categories",      value=ticket_txt,             inline=False)
    embed.add_field(name="🎬 GIF-Exempt Role",        value=gif_txt,                inline=True)
    embed.add_field(name="🎂 Birthday Channel",       value=bday_txt,               inline=True)
    embed.add_field(name="👋 Welcome Config",         value=welcome_txt,            inline=True)
    embed.add_field(name="🔗 Stored Server Invite",   value=invite_txt,             inline=True)
    embed.add_field(name="💎 Vanity Role",            value=vanity_txt,             inline=True)
    embed.add_field(name="📊 Active Polls",           value=str(active_polls),      inline=True)
    embed.add_field(name="🌐 Verify System",          value=verify_txt,             inline=False)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text=f"Requested by {ctx.author} • TrapAI Config Export", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


# ============================================================
# GIVEAWAY SYSTEM
# ============================================================

import random as _random

# GIVEAWAYS[message_id] = { guild_id, channel_id, host_id, prize, winners, ends_at, entries: set }
GIVEAWAYS: dict[int, dict] = _load_giveaways()

# GIVEAWAY_PING_ROLE[guild_id] = role_id — pinged whenever ,giveaway starts
# a new one, set via ,setgiveawayrole. Pair this with a self-role (see
# ,createrolemenu) so members can opt in/out of it themselves.
GIVEAWAY_PING_ROLE: dict[int, int] = _load_depth(_load_data("giveaway_ping_role", {}), 1)


def _save_giveaway_ping_role():
    _save_data("giveaway_ping_role", _dump_depth(GIVEAWAY_PING_ROLE, 1))


def _parse_gw_duration(raw: str) -> int | None:
    raw = raw.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    try:
        return int(raw[:-1]) * units[raw[-1]] if raw[-1] in units else None
    except (ValueError, IndexError):
        return None


class GiveawayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎉 Enter Giveaway", style=discord.ButtonStyle.success, custom_id="gw_enter")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg_id = interaction.message.id
        data   = GIVEAWAYS.get(msg_id)
        if not data:
            await interaction.response.send_message("❌ This giveaway is no longer active.", ephemeral=True); return
        if discord.utils.utcnow().timestamp() >= data["ends_at"]:
            await interaction.response.send_message("❌ This giveaway has already ended.", ephemeral=True); return
        uid = interaction.user.id
        if uid in data["entries"]:
            data["entries"].discard(uid)
            msg = f"↩️ You have **left** the giveaway. ({len(data['entries'])} left)"
        else:
            data["entries"].add(uid)
            msg = f"🎉 You're entered! **{len(data['entries'])}** participant(s) so far."
        await interaction.response.send_message(msg, ephemeral=True)
        # refresh entry count
        try:
            embed = interaction.message.embeds[0]
            for i, f in enumerate(embed.fields):
                if "Entries" in f.name:
                    embed.set_field_at(i, name="🎟️ Entries", value=str(len(data["entries"])), inline=True)
                    break
            await interaction.message.edit(embed=embed)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="👥 Entries", style=discord.ButtonStyle.secondary, custom_id="gw_count")
    async def count(self, interaction: discord.Interaction, button: discord.ui.Button):
        data    = GIVEAWAYS.get(interaction.message.id)
        entries = len(data["entries"]) if data else 0
        await interaction.response.send_message(f"🎟️ **{entries}** participant(s) entered so far.", ephemeral=True)


async def _end_giveaway(guild: discord.Guild, channel_id: int, msg_id: int):
    data = GIVEAWAYS.get(msg_id)
    if not data:
        return
    channel = guild.get_channel(channel_id)
    if not channel:
        GIVEAWAYS.pop(msg_id, None); _save_giveaways(); return
    try:
        msg = await channel.fetch_message(msg_id)
    except (discord.NotFound, discord.HTTPException):
        GIVEAWAYS.pop(msg_id, None); _save_giveaways(); return

    entries  = list(data["entries"])
    n_win    = min(data["winners"], len(entries))
    if entries:
        # Weighted draw without replacement (Efraimidis-Spirakis): Staff
        # Award holders get 3x better odds, everyone else is even.
        def _entry_weight(uid):
            member = guild.get_member(uid)
            return STAFF_AWARD_GIVEAWAY_WEIGHT if member and _has_staff_award_role(member) else 1.0
        keyed   = sorted(((_random.random() ** (1.0 / _entry_weight(uid)), uid) for uid in entries), reverse=True)
        winners = [uid for _, uid in keyed[:n_win]]
    else:
        winners = []
    host     = guild.get_member(data["host_id"])

    if winners:
        mentions     = " ".join(f"<@{w}>" for w in winners)
        result_text  = f"🎊 **Winner(s):** {mentions}"
    else:
        mentions     = ""
        result_text  = "😔 No valid entries — no winner drawn."

    embed = discord.Embed(
        title="🎉 GIVEAWAY ENDED",
        description=f"**Prize:** {data['prize']}\n\n{result_text}",
        color=discord.Color.dark_grey(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="🎟️ Total Entries", value=str(len(entries)), inline=True)
    embed.add_field(name="🏆 Winners",        value=str(n_win),        inline=True)
    embed.add_field(name="🎙️ Hosted By",      value=host.mention if host else f"<@{data['host_id']}>", inline=True)
    embed.set_footer(text=f"TrapAI Giveaway System • {guild.name}")
    try:
        await msg.edit(embed=embed, view=None)
    except discord.HTTPException:
        pass
    if winners:
        await channel.send(f"🎊 Congratulations {mentions}! You won **{data['prize']}**!\nHosted by {host.mention if host else 'staff'}.")
    else:
        await channel.send("😔 The giveaway ended with no entries.")
    GIVEAWAYS.pop(msg_id, None)
    _save_giveaways()


@bot.command()
@_permitted_check(manage_guild=True)
async def giveaway(ctx, duration: str, winners: int, *, prize: str):
    """Start a giveaway. Usage: ,giveaway 30m 1 Nitro Classic"""
    secs = _parse_gw_duration(duration)
    if not secs or secs < 10:
        await ctx.send("❌ Invalid duration. Use: `30s`, `10m`, `2h`, `3d` (min 10s).", delete_after=8); return
    if not 1 <= winners <= 20:
        await ctx.send("❌ Winners must be 1–20.", delete_after=8); return

    ends_dt  = discord.utils.utcnow() + timedelta(seconds=secs)
    ends_str = discord.utils.format_dt(ends_dt, "R")

    embed = discord.Embed(
        title="🎉  G I V E A W A Y",
        description=(
            f"**Prize:** {prize}\n\n"
            f"Click **🎉 Enter Giveaway** to join!\n"
            f"Click again to **leave**.\n\n"
            f"⏰ Ends {ends_str}"
        ),
        color=discord.Color.purple(),
        timestamp=ends_dt
    )
    embed.add_field(name="🎟️ Entries",  value="0",              inline=True)
    embed.add_field(name="🏆 Winners",   value=str(winners),     inline=True)
    embed.add_field(name="🎙️ Hosted By", value=ctx.author.mention, inline=True)
    embed.set_footer(text=f"TrapAI Giveaway System • {ctx.guild.name} | Ends at")
    if ctx.guild.icon:
        embed.set_thumbnail(url=ctx.guild.icon.url)

    ping_role_id = GIVEAWAY_PING_ROLE.get(ctx.guild.id)
    ping_role = ctx.guild.get_role(ping_role_id) if ping_role_id else None
    msg = await ctx.send(
        content=ping_role.mention if ping_role else None,
        embed=embed, view=GiveawayView(),
        allowed_mentions=discord.AllowedMentions(roles=[ping_role] if ping_role else False)
    )

    GIVEAWAYS[msg.id] = {
        "guild_id": ctx.guild.id, "channel_id": ctx.channel.id,
        "host_id":  ctx.author.id, "prize": prize,
        "winners":  winners, "ends_at": ends_dt.timestamp(), "entries": set(),
    }
    _save_giveaways()
    await ctx.message.delete(delay=2)

    async def _schedule():
        await asyncio.sleep(secs)
        await _end_giveaway(ctx.guild, ctx.channel.id, msg.id)
    asyncio.create_task(_schedule())


@bot.command()
@_permitted_check(manage_guild=True)
async def giveawayend(ctx, message_id: int = None):
    """Force-end a giveaway early. Usage: ,giveawayend [message_id]"""
    if message_id is None:
        found = [(mid, d) for mid, d in GIVEAWAYS.items() if d["channel_id"] == ctx.channel.id]
        if not found:
            await ctx.send("❌ No active giveaway found in this channel.", delete_after=8); return
        message_id, _ = found[0]
    if message_id not in GIVEAWAYS:
        await ctx.send("❌ That giveaway is not active.", delete_after=8); return
    data = GIVEAWAYS[message_id]
    await _end_giveaway(ctx.guild, data["channel_id"], message_id)
    await ctx.send("✅ Giveaway ended.", delete_after=5)


@bot.command()
@_permitted_check(manage_guild=True)
async def giveaways(ctx):
    """List all active giveaways. Usage: ,giveaways"""
    active = [(mid, d) for mid, d in GIVEAWAYS.items() if d["guild_id"] == ctx.guild.id]
    if not active:
        await ctx.send("📭 No active giveaways right now.")
        return
    embed = discord.Embed(title="🎉 Active Giveaways", color=discord.Color.purple(), timestamp=discord.utils.utcnow())
    for mid, d in active:
        ends = discord.utils.format_dt(
            discord.utils.utcnow() + timedelta(seconds=max(0, d["ends_at"] - discord.utils.utcnow().timestamp())), "R"
        )
        embed.add_field(
            name=d["prize"],
            value=f"🎟️ {len(d['entries'])} entries • 🏆 {d['winners']} winner(s) • ⏰ {ends}",
            inline=False
        )
    embed.set_footer(text=f"TrapAI Giveaway System • {ctx.guild.name}")
    await ctx.send(embed=embed)


@bot.command()
@_permitted_check(manage_guild=True)
async def setgiveawayrole(ctx, *, arg: str = None):
    """
    Set a role to ping every time ,giveaway starts a new one. Pair this
    with a self-role (,createrolemenu) so members can opt in/out
    themselves instead of it being forced on everyone.
    Usage:
      ,setgiveawayrole @role — set it
      ,setgiveawayrole off   — clear it
      ,setgiveawayrole       — show current setting
    """
    guild = ctx.guild
    if arg is None:
        current_id = GIVEAWAY_PING_ROLE.get(guild.id)
        current = guild.get_role(current_id) if current_id else None
        await ctx.send(
            f"🔔 Giveaway ping role: {current.mention}" if current else
            "📭 No giveaway ping role set. Use `,setgiveawayrole @role`."
        )
        return
    if arg.strip().lower() == "off":
        had = GIVEAWAY_PING_ROLE.pop(guild.id, None) is not None
        _save_giveaway_ping_role()
        await ctx.send("✅ Giveaway ping role cleared." if had else "ℹ️ No giveaway ping role was set.")
        return

    try:
        role = await commands.RoleConverter().convert(ctx, arg.strip())
    except commands.RoleNotFound:
        await ctx.send("❌ Usage: `,setgiveawayrole @role` or `,setgiveawayrole off`.", delete_after=8)
        return
    GIVEAWAY_PING_ROLE[guild.id] = role.id
    _save_giveaway_ping_role()
    await ctx.send(f"✅ {role.mention} will now be pinged every time a giveaway starts.")


# ============================================================
# POLL SYSTEM
# ============================================================

# POLLS[str(message_id)] = {
#   guild_id, channel_id, question, options: [str, ...],
#   votes: {str(user_id): option_index}, author_id, author_name,
#   closes_at: unix_ts or None, closed: bool
# }
def _save_polls():
    _save_data("polls", POLLS)


def _load_polls():
    return _load_data("polls", {})


POLLS: dict[str, dict] = _load_polls()


def _parse_poll_duration(raw: str) -> int | None:
    raw = raw.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    try:
        return int(raw[:-1]) * units[raw[-1]] if raw[-1] in units else None
    except (ValueError, IndexError):
        return None


def _build_poll_embed(poll: dict) -> discord.Embed:
    total  = len(poll["votes"])
    counts = [0] * len(poll["options"])
    for idx in poll["votes"].values():
        if 0 <= idx < len(counts):
            counts[idx] += 1

    lines = []
    for i, opt in enumerate(poll["options"]):
        c   = counts[i]
        pct = (c / total * 100) if total else 0
        bar = _progress_bar(c, total if total else 1, length=12)
        lines.append(f"**{i + 1}. {opt}**\n{bar}  `{c}` vote(s) • `{pct:.0f}%`")

    if poll.get("closed"):
        status = "🔴 **Poll Closed**"
    elif poll.get("closes_at"):
        status = f"🟢 **Poll Open** • Closes <t:{int(poll['closes_at'])}:R>"
    else:
        status = "🟢 **Poll Open** — click a button below to vote"

    desc = f"**{poll['question']}**\n\n" + "\n\n".join(lines) + f"\n\n{status}"
    color = discord.Color.dark_grey() if poll.get("closed") else discord.Color.blurple()

    embed = discord.Embed(title="📊  P O L L", description=desc, color=color, timestamp=discord.utils.utcnow())
    embed.set_footer(text=f"Total votes: {total} • Hosted by {poll.get('author_name', 'Unknown')}")
    return embed


class PollView(discord.ui.View):
    def __init__(self, options: list):
        super().__init__(timeout=None)
        for i, opt in enumerate(options[:10]):
            btn = discord.ui.Button(
                label=opt[:80], style=discord.ButtonStyle.primary,
                custom_id=f"poll_vote_{i}", row=i // 5
            )
            btn.callback = self._make_callback(i)
            self.add_item(btn)

    def _make_callback(self, index: int):
        async def callback(interaction: discord.Interaction):
            msg_id = interaction.message.id
            poll = POLLS.get(str(msg_id))
            if not poll or poll.get("closed"):
                await interaction.response.send_message("❌ This poll is no longer active.", ephemeral=True)
                return
            uid = str(interaction.user.id)
            if poll["votes"].get(uid) == index:
                await interaction.response.send_message("You already voted for that option.", ephemeral=True)
                return
            poll["votes"][uid] = index
            _save_polls()
            try:
                await interaction.response.edit_message(embed=_build_poll_embed(poll))
            except discord.HTTPException:
                await interaction.response.send_message("✅ Vote recorded!", ephemeral=True)
        return callback


async def _close_poll(guild_id: int, channel_id: int, msg_id: int):
    poll = POLLS.get(str(msg_id))
    if not poll or poll.get("closed"):
        return
    poll["closed"] = True
    _save_polls()
    guild = bot.get_guild(guild_id)
    channel = guild.get_channel(channel_id) if guild else None
    if not channel:
        return
    try:
        msg = await channel.fetch_message(msg_id)
        await msg.edit(embed=_build_poll_embed(poll), view=None)
    except (discord.NotFound, discord.HTTPException):
        pass


async def _schedule_poll_close(guild_id: int, channel_id: int, msg_id: int, delay: float):
    if delay > 0:
        await asyncio.sleep(delay)
    await _close_poll(guild_id, channel_id, msg_id)


@bot.command()
@_permitted_check(manage_messages=True)
async def poll(ctx, *, rest: str):
    """
    Create a button-based poll with a live-updating results embed —
    each vote instantly updates the bar chart, one vote per member.
    Usage: ,poll <question> | <option 1> | <option 2> | ... [| --time <duration>]
    Example: ,poll Best game? | Valorant | Minecraft | Fortnite | --time 1h
    Duration accepts s/m/h/d (e.g. 30s, 10m, 2h, 3d). Omit --time to leave
    it open until closed manually with ,pollend.
    """
    parts = [p.strip() for p in rest.split("|") if p.strip()]
    if len(parts) < 3:
        await ctx.send("❌ Need a question and at least 2 options. Usage: `,poll Question? | Option 1 | Option 2`", delete_after=10)
        return

    question = parts[0][:256]
    duration_secs = None
    options = []
    for p in parts[1:]:
        if p.lower().startswith("--time"):
            raw = p[6:].strip()
            duration_secs = _parse_poll_duration(raw)
            if not duration_secs or duration_secs < 10:
                await ctx.send("❌ Invalid `--time` value. Use `30s`, `10m`, `2h`, `3d` (min 10s).", delete_after=8)
                return
        else:
            options.append(p[:80])

    if not (2 <= len(options) <= 10):
        await ctx.send("❌ Provide between 2 and 10 options.", delete_after=8)
        return

    closes_at = (discord.utils.utcnow().timestamp() + duration_secs) if duration_secs else None
    poll_data = {
        "guild_id": ctx.guild.id, "channel_id": ctx.channel.id,
        "question": question, "options": options, "votes": {},
        "author_id": ctx.author.id, "author_name": str(ctx.author),
        "closes_at": closes_at, "closed": False,
    }

    view = PollView(options)
    msg = await ctx.send(embed=_build_poll_embed(poll_data), view=view)
    POLLS[str(msg.id)] = poll_data
    _save_polls()

    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass

    if duration_secs:
        asyncio.create_task(_schedule_poll_close(ctx.guild.id, ctx.channel.id, msg.id, duration_secs))


@bot.command()
@_permitted_check(manage_messages=True)
async def pollend(ctx, message_id: int = None):
    """Force-close a poll early. Usage: ,pollend [message_id] (omit to close the latest one in this channel)"""
    if message_id is None:
        found = [mid for mid, d in POLLS.items()
                 if d["channel_id"] == ctx.channel.id and not d.get("closed")]
        if not found:
            await ctx.send("❌ No active poll found in this channel.", delete_after=8)
            return
        message_id = int(found[-1])

    poll = POLLS.get(str(message_id))
    if not poll:
        await ctx.send("❌ No poll found with that message ID.", delete_after=8)
        return
    if poll.get("closed"):
        await ctx.send("❌ That poll is already closed.", delete_after=8)
        return

    await _close_poll(poll["guild_id"], poll["channel_id"], message_id)
    await ctx.send("✅ Poll closed.", delete_after=6)


# ============================================================
# STAFF PSA  — rewritten
# ============================================================

# Each type: (sidebar_color, banner_rgb, icon_emoji, label, ping)
PSA_TYPES = {
    "info":      (discord.Color.from_rgb(88, 101, 242),  (88,  101, 242), "📢", "INFO",      None),
    "warning":   (discord.Color.from_rgb(250, 166, 26),  (250, 166,  26), "⚠️",  "WARNING",   None),
    "urgent":    (discord.Color.from_rgb(237, 66,  69),  (237,  66,  69), "🚨", "URGENT",    "@here"),
    "critical":  (discord.Color.from_rgb(180, 0,   0),   (180,   0,   0), "🔴", "CRITICAL",  "@everyone"),
    "update":    (discord.Color.from_rgb(87,  242, 135),  (87, 242, 135), "📣", "UPDATE",    None),
    "rules":     (discord.Color.from_rgb(254, 231, 92),  (254, 231,  92), "📜", "RULES",     None),
    "shutdown":  (discord.Color.from_rgb(32,   34,  37),  (32,  34,  37), "🔒", "SHUTDOWN",  "@here"),
    "reminder":  (discord.Color.from_rgb(114, 137, 218), (114, 137, 218), "🔔", "REMINDER",  None),
}

_PSA_DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"


class PSADismissView(discord.ui.View):
    """Adds an ephemeral 'Got it' acknowledge button to PSA messages."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="✅  Got it",
        style=discord.ButtonStyle.secondary,
        custom_id="psa_dismiss"
    )
    async def dismiss(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "✅ Acknowledged. Thanks for reading!",
            ephemeral=True
        )


@bot.command()
@_permitted_check(manage_messages=True)
async def staffpsa(ctx, psa_type: str = "info", *, message: str):
    """
    Post a richly styled staff PSA with an acknowledge button.

    Types: info | warning | urgent | critical | update | rules | shutdown | reminder

    Usage:
      ,staffpsa info      Server maintenance tonight at 10 PM EST
      ,staffpsa warning   New rules are being drafted — read carefully
      ,staffpsa urgent    Raid detected — all hands on deck
      ,staffpsa critical  Server infrastructure going down in 5 minutes
      ,staffpsa update    Channels have been reorganised
      ,staffpsa rules     Reminder: no self-promotion in #general
      ,staffpsa shutdown  Taking the server offline for 30 minutes
      ,staffpsa reminder  Staff meeting in VC tonight at 9 PM EST
    """
    psa_type = psa_type.lower()
    if psa_type not in PSA_TYPES:
        types_list = " | ".join(f"`{k}`" for k in PSA_TYPES)
        await ctx.send(f"❌ Invalid type. Choose one of: {types_list}", delete_after=10)
        return

    color, rgb, icon, label, ping = PSA_TYPES[psa_type]
    now = discord.utils.utcnow()

    # ── Main PSA embed ─────────────────────────────────────────
    embed = discord.Embed(color=color, timestamp=now)

    embed.set_author(
        name=f"{icon}  STAFF PSA  ·  {label}",
        icon_url=ctx.author.display_avatar.url
    )

    embed.description = (
        f"{_PSA_DIVIDER}\n"
        f"{message}\n"
        f"{_PSA_DIVIDER}"
    )

    embed.add_field(
        name="👮 Posted by",
        value=f"{ctx.author.mention}\n`{ctx.author}`",
        inline=True
    )
    embed.add_field(
        name="📍 Channel",
        value=ctx.channel.mention,
        inline=True
    )
    embed.add_field(
        name="🕐 Time",
        value=discord.utils.format_dt(now, "F"),
        inline=True
    )

    # Type-specific flavour
    flavours = {
        "info":     "ℹ️  This is a general information announcement.",
        "warning":  "⚠️  Please read this carefully — action may be required.",
        "urgent":   "🚨  Immediate attention required from all staff.",
        "critical": "🔴  Critical — respond to this immediately.",
        "update":   "📣  A server update has been applied.",
        "rules":    "📜  Rules reminder — please review and acknowledge.",
        "shutdown": "🔒  The server is entering maintenance mode.",
        "reminder": "🔔  Friendly reminder from the staff team.",
    }
    embed.add_field(name="", value=f"*{flavours[psa_type]}*", inline=False)

    if ctx.guild.icon:
        embed.set_thumbnail(url=ctx.guild.icon.url)

    embed.set_footer(
        text=f"{ctx.guild.name}  ·  Staff PSA  ·  ID: {ctx.message.id}",
        icon_url=ctx.guild.icon.url if ctx.guild.icon else None
    )

    # ── Send ──────────────────────────────────────────────────
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass

    view = PSADismissView()

    if ping:
        psa_msg = await ctx.channel.send(ping, embed=embed, view=view)
    else:
        psa_msg = await ctx.channel.send(embed=embed, view=view)

    # ── Log ───────────────────────────────────────────────────
    await log(
        ctx.guild, "mod", f"Staff PSA Posted — {label}", None, color,
        fields=[
            ("🛡 Posted By",  f"{ctx.author.mention} (`{ctx.author.id}`)",           True),
            ("📍 Channel",    ctx.channel.mention,                                    True),
            ("🏷️ Type",       label,                                                  True),
            ("📣 Ping",       ping or "None",                                         True),
            ("🔗 Jump",       f"[View PSA]({psa_msg.jump_url})",                     True),
            ("📢 Message",    message[:512],                                           False),
        ],
        actor=ctx.author
    )


# ============================================================
# STAFF TASK BOARD  — rewritten
# ============================================================

TASKS: dict[int, list]
_task_counter: int
TASKS, _task_counter = _load_tasks()


def _new_task_id() -> int:
    global _task_counter
    _task_counter += 1
    return _task_counter


TASK_PRIORITIES = {
    "low":      ("🟢", "Low",      discord.Color.from_rgb(87, 242, 135)),
    "medium":   ("🟡", "Medium",   discord.Color.from_rgb(250, 166, 26)),
    "high":     ("🔴", "High",     discord.Color.from_rgb(237, 66, 69)),
    "critical": ("🚨", "Critical", discord.Color.from_rgb(180, 0, 0)),
}

TASK_STATUSES = {
    "open":        ("📋", "Open",        discord.Color.blurple()),
    "in-progress": ("⚙️",  "In Progress", discord.Color.purple()),
    "done":        ("✅", "Done",        discord.Color.green()),
    "blocked":     ("🚫", "Blocked",     discord.Color.red()),
    "review":      ("🔍", "In Review",   discord.Color.from_rgb(114, 137, 218)),
}


def _resolve_task_assignee(guild: discord.Guild, task: dict):
    """A task can be assigned to a Member or a Role (,task @Inner Circle ...) —
    both have .mention, so callers can treat the result uniformly. Old tasks
    saved before role-assignment existed have no "assigned_kind" key, which
    defaults to "member" for backward compatibility."""
    assigned_id = task.get("assigned_to")
    if not assigned_id:
        return None
    if task.get("assigned_kind") == "role":
        return guild.get_role(assigned_id)
    return guild.get_member(assigned_id)


def _task_embed(task: dict, guild: discord.Guild) -> discord.Embed:
    s_icon, s_label, _ = TASK_STATUSES.get(task["status"], ("📋", task["status"], discord.Color.blurple()))
    p_icon, p_label, p_color = TASK_PRIORITIES.get(task["priority"], ("🟡", task["priority"], discord.Color.purple()))

    # Color driven by priority
    color = p_color

    assigned = _resolve_task_assignee(guild, task)
    creator  = guild.get_member(task["created_by"])

    due_str = ""
    if task.get("due_at"):
        due_str = discord.utils.format_dt(task["due_at"], "R")

    # Notes preview (last 3)
    notes = task.get("notes", [])
    notes_val = ""
    if notes:
        lines = []
        for n in notes[-3:]:
            t_str = discord.utils.format_dt(n["time"], "R") if hasattr(n["time"], "tzinfo") else ""
            lines.append(f"• **{n['by']}** {t_str}: {n['text'][:80]}")
        notes_val = "\n".join(lines)

    embed = discord.Embed(
        color=color,
        timestamp=task["created_at"]
    )
    embed.set_author(
        name=f"{p_icon} Task #{task['id']}  ·  {task['title']}",
        icon_url=guild.icon.url if guild.icon else None
    )

    embed.description = (
        f"```\n{task['description'] or 'No description provided.'}\n```"
    )

    embed.add_field(
        name="📊 Status",
        value=f"{s_icon} **{s_label}**",
        inline=True
    )
    embed.add_field(
        name="🔥 Priority",
        value=f"{p_icon} **{p_label}**",
        inline=True
    )
    embed.add_field(
        name="👤 Assigned To",
        value=assigned.mention if assigned else "*(Unassigned)*",
        inline=True
    )
    embed.add_field(
        name="🛡 Created By",
        value=creator.mention if creator else f"<@{task['created_by']}>",
        inline=True
    )
    embed.add_field(
        name="📅 Created",
        value=discord.utils.format_dt(task["created_at"], "R"),
        inline=True
    )
    embed.add_field(
        name="🔄 Last Updated",
        value=discord.utils.format_dt(task["updated_at"], "R"),
        inline=True
    )
    if due_str:
        embed.add_field(name="⏰ Due", value=due_str, inline=True)
    if notes_val:
        embed.add_field(name=f"💬 Notes ({len(notes)})", value=notes_val, inline=False)

    embed.set_footer(
        text=f"TrapAI Staff Tasks  ·  {guild.name}  ·  Task #{task['id']}"
    )
    return embed


# ── Modals ────────────────────────────────────────────────────

class TaskNoteModal(discord.ui.Modal, title="💬 Add a Note"):
    text = discord.ui.TextInput(
        label="Note",
        style=discord.TextStyle.paragraph,
        placeholder="What's the update on this task?",
        min_length=1,
        max_length=300
    )

    def __init__(self, task_id: int, guild_id: int):
        super().__init__()
        self.task_id  = task_id
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        task = next(
            (t for t in TASKS.get(self.guild_id, []) if t["id"] == self.task_id),
            None
        )
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return
        task.setdefault("notes", []).append({
            "by":   interaction.user.display_name,
            "text": self.text.value,
            "time": discord.utils.utcnow()
        })
        task["updated_at"] = discord.utils.utcnow()
        view = TaskView(self.task_id, self.guild_id)
        await interaction.response.edit_message(embed=_task_embed(task, interaction.guild), view=view)


class TaskReassignModal(discord.ui.Modal, title="👤 Reassign Task"):
    user_input = discord.ui.TextInput(
        label="User ID or @mention",
        placeholder="e.g. 123456789012345678",
        min_length=1,
        max_length=32
    )

    def __init__(self, task_id: int, guild_id: int):
        super().__init__()
        self.task_id  = task_id
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        task = next(
            (t for t in TASKS.get(self.guild_id, []) if t["id"] == self.task_id),
            None
        )
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return
        raw = self.user_input.value.strip().lstrip("<@!").rstrip(">")
        try:
            uid = int(raw)
        except ValueError:
            await interaction.response.send_message("❌ Invalid user ID.", ephemeral=True)
            return
        member = interaction.guild.get_member(uid)
        if not member:
            await interaction.response.send_message("❌ Member not found in this server.", ephemeral=True)
            return
        old_id = task["assigned_to"]
        task["assigned_to"]   = uid
        task["assigned_kind"] = "member"  # this modal only ever reassigns to a member
        task["updated_at"]    = discord.utils.utcnow()
        view = TaskView(self.task_id, self.guild_id)
        await interaction.response.edit_message(embed=_task_embed(task, interaction.guild), view=view)
        # DM the newly assigned member
        try:
            dm = discord.Embed(
                title="📋 You've been assigned a task",
                color=discord.Color.blurple(),
                timestamp=discord.utils.utcnow()
            )
            dm.add_field(name="📋 Task",       value=f"#{task['id']} — {task['title']}", inline=False)
            dm.add_field(name="📝 Description", value=task["description"] or "—",        inline=False)
            p_icon, p_label, _ = TASK_PRIORITIES.get(task["priority"], ("🟡", task["priority"], None))
            dm.add_field(name="🔥 Priority",   value=f"{p_icon} {p_label}",              inline=True)
            dm.add_field(name="👮 Assigned By", value=str(interaction.user),             inline=True)
            dm.set_footer(text=f"TrapAI Tasks • {interaction.guild.name}")
            await member.send(embed=dm)
        except (discord.Forbidden, discord.HTTPException):
            pass


# ── View ──────────────────────────────────────────────────────

class TaskView(discord.ui.View):
    def __init__(self, task_id: int, guild_id: int):
        super().__init__(timeout=None)
        self.task_id  = task_id
        self.guild_id = guild_id

    def _get(self):
        return next((t for t in TASKS.get(self.guild_id, []) if t["id"] == self.task_id), None)

    def _staff(self, member: discord.Member) -> bool:
        return member.guild_permissions.manage_messages or member.guild_permissions.administrator

    # ── Row 0 — Status changes ──────────────────────────────────
    @discord.ui.button(label="⚙️ In Progress", style=discord.ButtonStyle.primary,   custom_id="task2_inprog",   row=0)
    async def mark_inprogress(self, i: discord.Interaction, b: discord.ui.Button):
        t = self._get()
        if not t: await i.response.send_message("❌ Task not found.", ephemeral=True); return
        t["status"] = "in-progress"; t["updated_at"] = discord.utils.utcnow()
        await i.response.edit_message(embed=_task_embed(t, i.guild), view=self)

    @discord.ui.button(label="🔍 Review",      style=discord.ButtonStyle.primary,   custom_id="task2_review",   row=0)
    async def mark_review(self, i: discord.Interaction, b: discord.ui.Button):
        t = self._get()
        if not t: await i.response.send_message("❌ Task not found.", ephemeral=True); return
        t["status"] = "review"; t["updated_at"] = discord.utils.utcnow()
        await i.response.edit_message(embed=_task_embed(t, i.guild), view=self)

    @discord.ui.button(label="✅ Done",         style=discord.ButtonStyle.success,   custom_id="task2_done",     row=0)
    async def mark_done(self, i: discord.Interaction, b: discord.ui.Button):
        t = self._get()
        if not t: await i.response.send_message("❌ Task not found.", ephemeral=True); return
        t["status"] = "done"; t["updated_at"] = discord.utils.utcnow()
        await i.response.edit_message(embed=_task_embed(t, i.guild), view=self)

    @discord.ui.button(label="🚫 Blocked",      style=discord.ButtonStyle.danger,    custom_id="task2_blocked",  row=0)
    async def mark_blocked(self, i: discord.Interaction, b: discord.ui.Button):
        t = self._get()
        if not t: await i.response.send_message("❌ Task not found.", ephemeral=True); return
        t["status"] = "blocked"; t["updated_at"] = discord.utils.utcnow()
        await i.response.edit_message(embed=_task_embed(t, i.guild), view=self)

    @discord.ui.button(label="📋 Reopen",       style=discord.ButtonStyle.secondary, custom_id="task2_reopen",   row=0)
    async def reopen(self, i: discord.Interaction, b: discord.ui.Button):
        t = self._get()
        if not t: await i.response.send_message("❌ Task not found.", ephemeral=True); return
        t["status"] = "open"; t["updated_at"] = discord.utils.utcnow()
        await i.response.edit_message(embed=_task_embed(t, i.guild), view=self)

    # ── Row 1 — Actions ─────────────────────────────────────────
    @discord.ui.button(label="💬 Add Note",     style=discord.ButtonStyle.secondary, custom_id="task2_note",     row=1)
    async def add_note(self, i: discord.Interaction, b: discord.ui.Button):
        t = self._get()
        if not t: await i.response.send_message("❌ Task not found.", ephemeral=True); return
        await i.response.send_modal(TaskNoteModal(self.task_id, self.guild_id))

    @discord.ui.button(label="👤 Reassign",     style=discord.ButtonStyle.secondary, custom_id="task2_assign",   row=1)
    async def reassign(self, i: discord.Interaction, b: discord.ui.Button):
        if not self._staff(i.user):
            await i.response.send_message("❌ Only staff can reassign tasks.", ephemeral=True); return
        t = self._get()
        if not t: await i.response.send_message("❌ Task not found.", ephemeral=True); return
        await i.response.send_modal(TaskReassignModal(self.task_id, self.guild_id))

    @discord.ui.button(label="🗑️ Delete",       style=discord.ButtonStyle.danger,    custom_id="task2_delete",   row=1)
    async def delete_task(self, i: discord.Interaction, b: discord.ui.Button):
        if not i.user.guild_permissions.manage_guild:
            await i.response.send_message("❌ Only managers can delete tasks.", ephemeral=True); return
        TASKS[self.guild_id] = [t for t in TASKS.get(self.guild_id, []) if t["id"] != self.task_id]
        await i.response.edit_message(content="🗑️ Task deleted.", embed=None, view=None)


@bot.command()
@_permitted_check(manage_messages=True)
async def task(ctx, priority: str = "medium", assigned: Union[discord.Member, discord.Role] = None, *, title_and_desc: str):
    """
    Create a staff task card with full interactive buttons. Assign it to
    a member, or to a whole role (,task @Inner Circle ...) to hand it to
    everyone with that role at once.

    Usage:
      ,task high @user Fix the verification flow — test it on mobile too
      ,task critical    Server is under raid — respond now
      ,task medium @mod Write the updated server rules
      ,task high @Inner Circle Get the server active — VCs, chat, all of it

    Priority: low | medium | high | critical
    Separate title and description with a dash: ` — `, ` – `, or ` -- ` all work
    """
    priority = priority.lower()
    if priority not in TASK_PRIORITIES:
        await ctx.send("❌ Priority must be: `low`, `medium`, `high`, `critical`", delete_after=8)
        return

    # Accept em-dash (—), en-dash (–), or a plain "--" as the title/description
    # separator — autocorrect (especially on mobile) very commonly turns a
    # typed "--" or "-" into an en-dash instead of the em-dash the docs show,
    # and a mismatch here used to mean the whole message became the title
    # with the rest silently missing.
    sep_match = re.search(r"\s+(?:—|–|--)\s+", title_and_desc)
    if sep_match:
        title, description = title_and_desc[:sep_match.start()], title_and_desc[sep_match.end():]
    elif len(title_and_desc) > 100:
        # No separator found and too long to fit as a title — instead of
        # silently truncating the rest away, keep the whole thing as the
        # description and use a shortened preview as the title.
        title, description = title_and_desc[:97].rstrip() + "...", title_and_desc
    else:
        title, description = title_and_desc, ""

    assigned_is_role = isinstance(assigned, discord.Role)

    now = discord.utils.utcnow()
    task_data = {
        "id":            _new_task_id(),
        "title":         title.strip()[:100],
        "description":   description.strip()[:500],
        "assigned_to":   assigned.id if assigned else None,
        "assigned_kind": "role" if assigned_is_role else "member",
        "priority":      priority,
        "status":        "open",
        "created_by":    ctx.author.id,
        "created_at":    now,
        "updated_at":    now,
        "due_at":        None,
        "notes":         [],
    }
    TASKS.setdefault(ctx.guild.id, []).append(task_data)

    msg = await ctx.send(
        content=assigned.mention if assigned_is_role else None,
        embed=_task_embed(task_data, ctx.guild),
        view=TaskView(task_data["id"], ctx.guild.id)
    )

    # DM assigned member — a role can't be DM'd, so it's pinged in-channel
    # above instead (content=assigned.mention on the message itself).
    if assigned and not assigned_is_role:
        try:
            dm = discord.Embed(
                title="📋 You've been assigned a task",
                color=discord.Color.blurple(),
                timestamp=now
            )
            if ctx.guild.icon:
                dm.set_thumbnail(url=ctx.guild.icon.url)
            dm.add_field(name="📋 Task",        value=f"#{task_data['id']} — {task_data['title']}", inline=False)
            dm.add_field(name="📝 Description", value=description or "—",                            inline=False)
            p_icon, p_label, _ = TASK_PRIORITIES[priority]
            dm.add_field(name="🔥 Priority",    value=f"{p_icon} {p_label}",                         inline=True)
            dm.add_field(name="👮 Assigned By", value=str(ctx.author),                               inline=True)
            dm.add_field(name="🔗 Jump",        value=f"[View Task]({msg.jump_url})",                inline=False)
            dm.set_footer(text=f"TrapAI Tasks • {ctx.guild.name}")
            await assigned.send(embed=dm)
        except (discord.Forbidden, discord.HTTPException):
            pass

    await log(
        ctx.guild, "mod", "Staff Task Created", None, discord.Color.blurple(),
        fields=[
            ("🛡 Created By",  f"{ctx.author.mention} (`{ctx.author.id}`)",   True),
            ("👤 Assigned To", assigned.mention if assigned else "Unassigned", True),
            ("📋 Title",       task_data["title"],                              True),
            ("🔥 Priority",    f"{TASK_PRIORITIES[priority][0]} {TASK_PRIORITIES[priority][1]}", True),
            ("🔗 Jump",        f"[View]({msg.jump_url})",                      True),
        ],
        actor=ctx.author
    )


@bot.command()
@_permitted_check(manage_messages=True)
async def tasklist(ctx, filter_status: str = None):
    """
    View the staff task board.
    Usage: ,tasklist          — show all active tasks
           ,tasklist done     — show completed tasks
           ,tasklist blocked  — show only blocked tasks
           ,tasklist @user    — show tasks assigned to a specific member
           ,tasklist @role    — show tasks assigned to a specific role
    """
    all_tasks = TASKS.get(ctx.guild.id, [])

    # Filter by status keyword
    if filter_status and filter_status.lower() in TASK_STATUSES:
        guild_tasks = [t for t in all_tasks if t["status"] == filter_status.lower()]
        title_suffix = f" — {TASK_STATUSES[filter_status.lower()][1]}"
    elif filter_status:
        # Try to parse as a member or role mention/ID
        try:
            uid = int(filter_status.strip("<@!&>"))
            guild_tasks = [t for t in all_tasks if t["assigned_to"] == uid]
            target = ctx.guild.get_member(uid) or ctx.guild.get_role(uid)
            name = target.display_name if isinstance(target, discord.Member) else target.name if target else None
            title_suffix = f" — {name or f'ID {uid}'}"
        except ValueError:
            guild_tasks = [t for t in all_tasks if t["status"] != "done"]
            title_suffix = ""
    else:
        guild_tasks = [t for t in all_tasks if t["status"] != "done"]
        title_suffix = ""

    if not guild_tasks:
        await ctx.send(f"📭 No tasks found{title_suffix}.")
        return

    # Summary counts
    counts = {}
    for t in all_tasks:
        counts[t["status"]] = counts.get(t["status"], 0) + 1

    summary_parts = []
    for st_key, (st_icon, st_label, _) in TASK_STATUSES.items():
        n = counts.get(st_key, 0)
        if n:
            summary_parts.append(f"{st_icon} **{n}** {st_label}")

    embed = discord.Embed(
        title=f"📋 Staff Task Board{title_suffix}",
        description="  ·  ".join(summary_parts) or "No tasks.",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    if ctx.guild.icon:
        embed.set_thumbnail(url=ctx.guild.icon.url)

    for t in guild_tasks[:12]:
        asgn     = _resolve_task_assignee(ctx.guild, t)
        p_icon, p_label, _ = TASK_PRIORITIES.get(t["priority"], ("🟡", t["priority"], None))
        s_icon, s_label, _ = TASK_STATUSES.get(t["status"],   ("📋", t["status"],   None))
        note_count = len(t.get("notes", []))
        note_str   = f"  💬 {note_count} note(s)" if note_count else ""
        embed.add_field(
            name=f"{p_icon} #{t['id']}  {t['title']}",
            value=(
                f"{s_icon} {s_label}  ·  👤 {asgn.mention if asgn else 'Unassigned'}{note_str}\n"
                f"*Updated {discord.utils.format_dt(t['updated_at'], 'R')}*"
            ),
            inline=False
        )

    shown = min(len(guild_tasks), 12)
    total = len(guild_tasks)
    embed.set_footer(
        text=f"Showing {shown} of {total} task(s)  ·  TrapAI Staff Tasks  ·  {ctx.guild.name}"
    )
    await ctx.send(embed=embed)


# ============================================================
# STAFF APPLICATIONS — accept flow
# ============================================================

# STAFF_RULES[guild_id] = "custom rules text" — optional per-guild override
# for the rules shown to a member when they're accepted via ,acceptstaff.
# Falls back to _DEFAULT_STAFF_RULES if never set.
STAFF_RULES: dict[int, str] = _load_depth(_load_data("staff_rules", {}), 1)

_DEFAULT_STAFF_RULES = (
    "1️⃣ Treat every member with respect — no favoritism, no abuse of power.\n"
    "2️⃣ Keep staff discussions and decisions confidential.\n"
    "3️⃣ Only use your permissions for their intended purpose.\n"
    "4️⃣ Stay active and communicate with the team if you'll be away.\n"
    "5️⃣ Follow the chain of command — escalate what you're unsure about.\n"
    "6️⃣ Lead by example: follow the server rules yourself.\n"
    "7️⃣ Major actions (bans, role changes) should be logged and explainable."
)


def _save_staff_rules():
    _save_data("staff_rules", _dump_depth(STAFF_RULES, 1))


def _resolve_staff_rules(guild_id: int) -> str:
    return STAFF_RULES.get(guild_id) or _DEFAULT_STAFF_RULES


# STAFF_MEETING_CONFIG[guild_id] = {"channel_id", "role_id" (nullable),
# "weekday" (0=Monday..6=Sunday), "hour", "minute" (both UTC, 24h),
# "last_sent" ("YYYY-MM-DD" or None)} — set via ,setstaffmeeting. Checked
# every minute by _staff_meeting_loop(); "last_sent" dedupes so a reminder
# only ever fires once per calendar day even if checked many times after
# the scheduled minute has passed.
STAFF_MEETING_CONFIG: dict[int, dict] = _load_depth(_load_data("staff_meeting_config", {}), 1)


def _save_staff_meeting_config():
    _save_data("staff_meeting_config", _dump_depth(STAFF_MEETING_CONFIG, 1))


@bot.command()
@_permitted_check(administrator=True)
async def setstaffrules(ctx, *, text: str = None):
    """
    Set (or clear) this server's custom staff rules — shown in the DM sent
    to a member when they're accepted via ,acceptstaff. Run with no text
    to clear the override and fall back to the default rules.
    Usage: ,setstaffrules <rules text>
    """
    if text is None:
        had_override = STAFF_RULES.pop(ctx.guild.id, None) is not None
        _save_staff_rules()
        await ctx.send(
            "↩️ Cleared this server's custom staff rules — falling back to the default."
            if had_override else "ℹ️ No custom staff rules were set — already using the default."
        )
        return

    STAFF_RULES[ctx.guild.id] = text
    _save_staff_rules()
    embed = discord.Embed(
        title="✅ Staff Rules Updated",
        description=text[:4000],
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"Set by {ctx.author} • Shown to new staff via ,acceptstaff")
    await ctx.send(embed=embed)


_WEEKDAY_NAMES = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
_WEEKDAY_DISPLAY = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _parse_weekday(text: str):
    text = text.strip().lower()
    if text in _WEEKDAY_NAMES:
        return _WEEKDAY_NAMES[text]
    if text.isdigit() and 0 <= int(text) <= 6:
        return int(text)
    return None


def _parse_time_hhmm(text: str):
    """Accepts 12-hour clock time with AM/PM ("6:00 PM", "6PM", "6:30am")
    or 24-hour ("18:00") — the 12-hour form needs to tolerate a space
    before AM/PM (how people actually type it), so this expects the
    caller to have already stripped internal whitespace."""
    text = text.strip().upper().replace(" ", "")
    m = re.match(r"^(\d{1,2}):?(\d{2})?(AM|PM)$", text)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2)) if m.group(2) else 0
        if not (1 <= h <= 12 and 0 <= mi <= 59):
            return None
        period = m.group(3)
        if period == "AM":
            h = 0 if h == 12 else h
        else:
            h = 12 if h == 12 else h + 12
        return h, mi
    m = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return h, mi
    return None


def _format_time_12h(hour: int, minute: int) -> str:
    period = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    return f"{h12}:{minute:02d} {period}"


_ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
_CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")


@bot.command()
@_permitted_check(manage_guild=True)
async def setstaffmeeting(ctx, *, args: str = None):
    """
    Have the bot automatically post a weekly staff meeting reminder —
    fires once, at or after the scheduled time (UTC), every week.
    Usage:
      ,setstaffmeeting <day> <time> [#channel] [@role]
      e.g. ,setstaffmeeting monday 6:00 PM #staff-chat @Staff
      Time can be 12-hour ("6:00 PM", "6PM") or 24-hour ("18:00") — always UTC.
      Defaults to the current channel if none given, and pings @here if
      no role is given.
      ,setstaffmeeting off   — disable
      ,setstaffmeeting       — show current config
    """
    guild = ctx.guild
    if args is None:
        cfg = STAFF_MEETING_CONFIG.get(guild.id)
        if not cfg:
            await ctx.send("📭 No staff meeting scheduled. Use `,setstaffmeeting <day> <time> [#channel] [@role]`.")
            return
        ch = guild.get_channel(cfg["channel_id"])
        r = guild.get_role(cfg["role_id"]) if cfg.get("role_id") else None
        await ctx.send(
            f"📅 Staff meetings: every **{_WEEKDAY_DISPLAY[cfg['weekday']]}** at "
            f"**{_format_time_12h(cfg['hour'], cfg['minute'])} UTC** in "
            f"{ch.mention if ch else '*deleted channel*'}"
            + (f", pinging {r.mention}" if r else ", pinging @here")
        )
        return

    if args.strip().lower() == "off":
        had = STAFF_MEETING_CONFIG.pop(guild.id, None) is not None
        _save_staff_meeting_config()
        await ctx.send("✅ Automatic staff meeting reminders disabled." if had else "ℹ️ No staff meeting was scheduled.")
        return

    role_match = _ROLE_MENTION_RE.search(args)
    role = guild.get_role(int(role_match.group(1))) if role_match else None
    channel_match = _CHANNEL_MENTION_RE.search(args)
    mentioned_channel = guild.get_channel(int(channel_match.group(1))) if channel_match else None

    remainder = _CHANNEL_MENTION_RE.sub("", _ROLE_MENTION_RE.sub("", args)).strip()
    usage_error = "❌ Usage: `,setstaffmeeting <day> <time> [#channel] [@role]` — e.g. `,setstaffmeeting monday 6:00 PM`"
    parts = remainder.split(None, 1)
    if len(parts) < 2:
        await ctx.send(usage_error, delete_after=10)
        return

    day = _parse_weekday(parts[0])
    parsed_time = _parse_time_hhmm(parts[1])
    if day is None or parsed_time is None:
        await ctx.send(usage_error, delete_after=10)
        return
    hour, minute = parsed_time
    target_channel = mentioned_channel or ctx.channel

    STAFF_MEETING_CONFIG[guild.id] = {
        "channel_id": target_channel.id,
        "role_id": role.id if role else None,
        "weekday": day,
        "hour": hour,
        "minute": minute,
        "last_sent": None,
    }
    _save_staff_meeting_config()
    await ctx.send(
        f"✅ Staff meeting reminders set for every **{_WEEKDAY_DISPLAY[day]}** at **{_format_time_12h(hour, minute)} UTC** "
        f"in {target_channel.mention}" + (f", pinging {role.mention}" if role else ", pinging @here") + "."
    )


@bot.command()
@_permitted_check(administrator=True)
async def acceptstaff(ctx, member: discord.Member, role: discord.Role):
    """
    Accept a member's staff application — grants them the given role and
    DMs them a welcome message with their new role and the staff rules.
    Usage: ,acceptstaff @user @role
    """
    if role >= ctx.guild.me.top_role:
        await ctx.send("❌ I can't grant a role higher than or equal to my own top role.", delete_after=8)
        return
    if role in member.roles:
        await ctx.send(f"❌ {member.mention} already has {role.mention}.", delete_after=8)
        return

    try:
        await member.add_roles(role, reason=f"Staff application accepted by {ctx.author}")
    except (discord.Forbidden, discord.HTTPException) as e:
        await ctx.send(f"❌ Couldn't grant that role: {e}", delete_after=10)
        return

    rules_text = _resolve_staff_rules(ctx.guild.id)
    dm_embed = discord.Embed(
        title="🎉 You've Been Accepted to the Staff Team!",
        description=(
            f"Congratulations — your staff application for **{ctx.guild.name}** has been **accepted**!\n\n"
            f"**🏷️ Your Role:** {role.name}\n\n"
            "Please read the staff rules below before you begin."
        ),
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    dm_embed.add_field(name="📋 Staff Rules", value=rules_text[:1024], inline=False)
    if ctx.guild.icon:
        dm_embed.set_thumbnail(url=ctx.guild.icon.url)
    dm_embed.set_footer(text=f"TrapAI • {ctx.guild.name}")

    dm_sent = True
    try:
        await member.send(embed=dm_embed)
    except (discord.Forbidden, discord.HTTPException):
        dm_sent = False

    confirm = discord.Embed(
        title="✅ Staff Application Accepted",
        description=(
            f"{member.mention} has been granted {role.mention} and welcomed to the team."
            + ("" if dm_sent else "\n⚠️ Couldn't DM them — their DMs may be closed.")
        ),
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    confirm.set_footer(text=f"Accepted by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=confirm)

    await log(ctx.guild, "mod", "Staff Application Accepted", None, discord.Color.green(),
              fields=[
                  ("👑 Accepted By",   f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("🆕 New Staff",     f"{member.mention} (`{member.id}`)",          True),
                  ("🏷️ Role Granted",  role.mention,                                 True),
                  ("📨 DM Sent",       "✅ Yes" if dm_sent else "❌ No (DMs closed)", True),
              ],
              actor=ctx.author, target=member)


@bot.command()
@_permitted_check(administrator=True)
async def denystaff(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    """
    Reject a member's staff application — DMs them a polite decline notice
    with the reason (no roles are touched). Usage: ,denystaff @user [reason]
    """
    dm_embed = discord.Embed(
        title="📋 Staff Application Update",
        description=(
            f"Thank you for applying to join the staff team at **{ctx.guild.name}**.\n\n"
            "After review, your application was **not accepted** at this time.\n\n"
            f"**Reason:** {reason}\n\n"
            "You're welcome to apply again in the future."
        ),
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow()
    )
    if ctx.guild.icon:
        dm_embed.set_thumbnail(url=ctx.guild.icon.url)
    dm_embed.set_footer(text=f"TrapAI • {ctx.guild.name}")

    dm_sent = True
    try:
        await member.send(embed=dm_embed)
    except (discord.Forbidden, discord.HTTPException):
        dm_sent = False

    confirm = discord.Embed(
        title="🚫 Staff Application Denied",
        description=(
            f"{member.mention}'s staff application has been denied.\n**Reason:** {reason}"
            + ("" if dm_sent else "\n⚠️ Couldn't DM them — their DMs may be closed.")
        ),
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow()
    )
    confirm.set_footer(text=f"Denied by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=confirm)

    await log(ctx.guild, "mod", "Staff Application Denied", None, discord.Color.red(),
              fields=[
                  ("👑 Denied By",  f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("🚫 Applicant",  f"{member.mention} (`{member.id}`)",          True),
                  ("📝 Reason",     reason,                                       False),
                  ("📨 DM Sent",    "✅ Yes" if dm_sent else "❌ No (DMs closed)", True),
              ],
              actor=ctx.author, target=member)


# ============================================================
# VOUCH SYSTEM
# ============================================================

def _requires_vouch(ctx) -> bool:
    """
    Returns True if the caller is cleared to use power commands.
    Bypass: administrator or manage_messages permission.
    Otherwise: vouch count must be >= VOUCH_CONFIG threshold (default 3).
    """
    if ctx.author.guild_permissions.administrator or ctx.author.guild_permissions.manage_messages:
        return True
    threshold = VOUCH_CONFIG.get(ctx.guild.id, {}).get("threshold", 3)
    count = VOUCHES.get(ctx.guild.id, {}).get(ctx.author.id, 0)
    return count >= threshold


# ── Approval view sent to owner ───────────────────────────────
class VouchRoleApprovalView(discord.ui.View):
    """Sent to the guild owner — Approve or Reject a vouch-role request."""

    def __init__(self, guild_id: int, token: str):
        super().__init__(timeout=None)  # persistent until acted on
        self.guild_id = guild_id
        self.token    = token

    async def _resolve(self, interaction: discord.Interaction, approved: bool):
        pending = ROLE_VOUCH_PENDING.get(self.guild_id, {}).pop(self.token, None)
        if pending is None:
            await interaction.response.send_message("⚠️ This request has already been handled.", ephemeral=True)
            return
        _save_role_vouch_pending()

        guild     = bot.get_guild(self.guild_id)
        member    = guild.get_member(pending["member_id"]) if guild else None
        role      = guild.get_role(pending["role_id"])     if guild else None
        requester = guild.get_member(pending["requester_id"]) if guild else None

        # Disable all buttons
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

        if approved and guild and member and role:
            # Whitelist so on_member_update won't strip it
            _VOUCH_ROLE_APPROVED.setdefault(self.guild_id, set()).add((member.id, role.id))
            try:
                await member.add_roles(role, reason=f"Vouch-role approved by {interaction.user}")
            except (discord.Forbidden, discord.HTTPException) as e:
                await interaction.followup.send(f"❌ Couldn't grant the role: `{e}`", ephemeral=True)
                _VOUCH_ROLE_APPROVED.get(self.guild_id, set()).discard((member.id, role.id))
                return

            # Log vouch
            gv = VOUCHES.setdefault(self.guild_id, {})
            gl = VOUCH_LOG.setdefault(self.guild_id, {})
            gv[member.id] = gv.get(member.id, 0) + 1
            gl.setdefault(member.id, []).append({
                "by": str(interaction.user), "by_id": interaction.user.id,
                "action": "vouch-role", "reason": pending["reason"],
                "role": str(role), "time": discord.utils.utcnow()
            })
            _save_vouches()
            _save_vouch_log()

            # DM member — approved
            try:
                dm = discord.Embed(
                    title="✅ Vouch Request Approved",
                    description=(
                        f"Your vouch request for the **{role.name}** role in **{guild.name}** "
                        "has been **approved** by the server owner!\n\n"
                        "The role has been granted to you."
                    ),
                    color=discord.Color.green(),
                    timestamp=discord.utils.utcnow()
                )
                dm.add_field(name="🏷️ Role",     value=role.name,               inline=True)
                dm.add_field(name="📝 Reason",   value=pending["reason"],        inline=False)
                dm.add_field(name="👑 Approved by", value=str(interaction.user), inline=True)
                if guild.icon:
                    dm.set_thumbnail(url=guild.icon.url)
                dm.set_footer(text=f"TrapAI • {guild.name}")
                await member.send(embed=dm)
            except (discord.Forbidden, discord.HTTPException):
                pass

            await log(guild, "vouches", "Vouch-Role Request Approved", None, discord.Color.green(),
                      fields=[
                          ("👤 Member",     f"{member.mention} (`{member.id}`)",         True),
                          ("🏷️ Role",       f"{role.mention}",                           True),
                          ("👑 Approved by", f"{interaction.user.mention}",              True),
                          ("📝 Reason",     pending["reason"],                            False),
                      ], actor=interaction.user, target=member)

            # DM requester — approved (if they're not the member themselves, who
            # already got their own DM above)
            if requester and requester.id != member.id:
                try:
                    req_dm = discord.Embed(
                        title="✅ Your Vouch Request Was Approved",
                        description=(
                            f"Your request to grant **{role.name}** to {member.mention} in "
                            f"**{guild.name}** has been **approved** by the server owner."
                        ),
                        color=discord.Color.green(),
                        timestamp=discord.utils.utcnow()
                    )
                    req_dm.set_footer(text=f"TrapAI • {guild.name}")
                    await requester.send(embed=req_dm)
                except (discord.Forbidden, discord.HTTPException):
                    pass

            await interaction.followup.send(
                f"✅ Approved — **{role.name}** granted to {member.mention}.", ephemeral=True
            )

        else:
            # DM member — rejected
            if member:
                try:
                    dm = discord.Embed(
                        title="❌ Vouch Request Rejected",
                        description=(
                            f"Your vouch request for the **{role.name if role else 'requested'}** role "
                            f"in **{guild.name if guild else 'the server'}** has been **rejected** by the server owner."
                        ),
                        color=discord.Color.red(),
                        timestamp=discord.utils.utcnow()
                    )
                    if role:
                        dm.add_field(name="🏷️ Role",   value=role.name,          inline=True)
                    dm.add_field(name="📝 Reason submitted", value=pending["reason"], inline=False)
                    dm.add_field(name="👑 Rejected by", value=str(interaction.user), inline=True)
                    if guild and guild.icon:
                        dm.set_thumbnail(url=guild.icon.url)
                    dm.set_footer(text=f"TrapAI • {guild.name if guild else ''}")
                    await member.send(embed=dm)
                except (discord.Forbidden, discord.HTTPException):
                    pass

            if guild and role:
                await log(guild, "vouches", "Vouch-Role Request Rejected", None, discord.Color.red(),
                          fields=[
                              ("👤 Member",     f"{member.mention} (`{member.id}`)" if member else str(pending["member_id"]), True),
                              ("🏷️ Role",       f"{role.mention}",                  True),
                              ("👑 Rejected by", f"{interaction.user.mention}",     True),
                              ("📝 Reason",     pending["reason"],                   False),
                          ], actor=interaction.user)

            # DM requester — rejected (if they're not the member themselves, who
            # already got their own DM above)
            if requester and (not member or requester.id != member.id):
                try:
                    req_dm = discord.Embed(
                        title="❌ Your Vouch Request Was Rejected",
                        description=(
                            f"Your request to grant **{role.name if role else 'a role'}** to "
                            f"{member.mention if member else 'a member'} in "
                            f"**{guild.name if guild else 'the server'}** has been **rejected** by the server owner."
                        ),
                        color=discord.Color.red(),
                        timestamp=discord.utils.utcnow()
                    )
                    req_dm.set_footer(text=f"TrapAI • {guild.name if guild else ''}")
                    await requester.send(embed=req_dm)
                except (discord.Forbidden, discord.HTTPException):
                    pass

            await interaction.followup.send(
                f"❌ Rejected — request denied. {member.mention if member else ''} has been notified.", ephemeral=True
            )

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.success, custom_id="vr_approve")
    async def approve_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._resolve(interaction, approved=True)

    @discord.ui.button(label="❌ Reject",  style=discord.ButtonStyle.danger,  custom_id="vr_reject")
    async def reject_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._resolve(interaction, approved=False)


@bot.command()
@_permitted_check(manage_messages=True)
async def vouch(ctx, member: discord.Member = None, role: discord.Role = None, *, reason: str = "No reason provided"):
    """
    Vouch for a member, optionally requesting a protected role for them.

    Usage:
      ,vouch @user reason                  — standard vouch (no role)
      ,vouch @user (role) reason           — request a protected role for them (needs owner approval)
      ,vouch @user @role reason            — same with role mention
    """
    if member is None:
        await ctx.send(
            "❌ Usage:\n"
            "`,vouch @user [reason]` — standard vouch\n"
            "`,vouch @user @role [reason]` — request a protected role (needs owner approval)",
            delete_after=10
        )
        return
    if member == ctx.author:
        await ctx.send("❌ You can't vouch for yourself.", delete_after=6)
        return
    if member.bot:
        await ctx.send("❌ You can't vouch for a bot.", delete_after=6)
        return

    # ── Role-vouch path ───────────────────────────────────────
    if role is not None:
        protected = PROTECTED_ROLES.get(ctx.guild.id, set())
        if role.id not in protected:
            await ctx.send(
                f"❌ **{role.name}** is not a protected role.\n"
                "Only protected roles require owner approval.\n"
                "Use `,protectedrole add @role` to protect a role.",
                delete_after=10
            )
            return
        if role in member.roles:
            await ctx.send(f"❌ {member.mention} already has the **{role.name}** role.", delete_after=6)
            return

        # Build a unique token for this request
        import uuid
        token = str(uuid.uuid4())[:8]
        ROLE_VOUCH_PENDING.setdefault(ctx.guild.id, {})[token] = {
            "member_id":    member.id,
            "role_id":      role.id,
            "requester_id": ctx.author.id,
            "reason":       reason,
        }
        _save_role_vouch_pending()

        owner = ctx.guild.owner
        view  = VouchRoleApprovalView(ctx.guild.id, token)

        request_embed = discord.Embed(
            title="🔔 Vouch-Role Request — Needs Your Approval",
            description=(
                f"**{ctx.author}** is requesting the **{role.name}** role for **{member}**.\n\n"
                f"This role is **protected** — only you can approve or reject this."
            ),
            color=discord.Color.purple(),
            timestamp=discord.utils.utcnow()
        )
        request_embed.add_field(name="👤 Member",     value=f"{member.mention} (`{member.id}`)", inline=True)
        request_embed.add_field(name="🏷️ Role",       value=f"{role.mention}",                  inline=True)
        request_embed.add_field(name="📨 Requested by", value=f"{ctx.author.mention}",           inline=True)
        request_embed.add_field(name="📝 Reason",     value=reason,                              inline=False)
        request_embed.set_thumbnail(url=member.display_avatar.url)
        if ctx.guild.icon:
            request_embed.set_footer(text=f"TrapAI Protected Roles • {ctx.guild.name}", icon_url=ctx.guild.icon.url)
        else:
            request_embed.set_footer(text=f"TrapAI Protected Roles • {ctx.guild.name}")

        try:
            await owner.send(embed=request_embed, view=view)
            owner_notified = True
        except (discord.Forbidden, discord.HTTPException):
            owner_notified = False

        confirm = discord.Embed(
            title="📨 Vouch-Role Request Submitted",
            description=(
                f"Your request to grant **{role.name}** to {member.mention} has been sent to the server owner "
                f"**{owner}** for approval.\n\n"
                f"{'✅ Owner has been notified via DM.' if owner_notified else '⚠️ Could not DM owner — they may need to check manually.'}"
            ),
            color=discord.Color.purple(),
            timestamp=discord.utils.utcnow()
        )
        confirm.add_field(name="👤 Member",   value=member.mention, inline=True)
        confirm.add_field(name="🏷️ Role",     value=role.mention,   inline=True)
        confirm.add_field(name="📝 Reason",   value=reason,         inline=False)
        confirm.set_footer(text=f"Awaiting owner approval • TrapAI • {ctx.guild.name}")
        await ctx.send(embed=confirm)

        await log(ctx.guild, "vouches", "Vouch-Role Request Submitted", None, discord.Color.purple(),
                  fields=[
                      ("📨 Requester", f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                      ("👤 Member",    f"{member.mention} (`{member.id}`)",          True),
                      ("🏷️ Role",      f"{role.mention}",                            True),
                      ("📝 Reason",    reason,                                        False),
                  ], actor=ctx.author, target=member)
        return

    # ── Standard vouch path (no role) ────────────────────────
    gv = VOUCHES.setdefault(ctx.guild.id, {})
    gl = VOUCH_LOG.setdefault(ctx.guild.id, {})
    gv[member.id] = gv.get(member.id, 0) + 1
    gl.setdefault(member.id, []).append({
        "by": str(ctx.author), "by_id": ctx.author.id,
        "action": "vouch", "reason": reason,
        "time": discord.utils.utcnow()
    })
    _save_vouches()
    _save_vouch_log()

    threshold = VOUCH_CONFIG.get(ctx.guild.id, {}).get("threshold", 3)
    score = gv[member.id]

    embed = discord.Embed(
        title="✅ Vouched",
        description=f"{ctx.author.mention} has vouched for {member.mention}.",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="📝 Reason",        value=reason,                              inline=False)
    embed.add_field(name="🔢 Total Vouches", value=f"**{score}** / {threshold} required", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"TrapAI Vouch System • {ctx.guild.name}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)

    await log(ctx.guild, "vouches", "Member Vouched", None, discord.Color.green(),
              fields=[
                  ("🛡 By",     f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("✅ For",    f"{member.mention} (`{member.id}`)",           True),
                  ("🔢 Score",  f"{score} / {threshold}",                      True),
                  ("📝 Reason", reason,                                         False),
              ],
              actor=ctx.author, target=member)


@bot.command()
@_permitted_check(manage_messages=True)
async def unvouch(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    """
    Remove a vouch from a member — also strips every protected role
    (,protectedrole add) they currently hold, since being unvouched means
    losing whatever that vouching earned them.
    Usage: ,unvouch @user [reason]
    """
    gv = VOUCHES.setdefault(ctx.guild.id, {})
    gl = VOUCH_LOG.setdefault(ctx.guild.id, {})
    current = gv.get(member.id, 0)
    if current <= 0:
        await ctx.send(f"❌ {member.mention} has no vouches to remove.", delete_after=6)
        return

    gv[member.id] = max(0, current - 1)
    gl.setdefault(member.id, []).append({
        "by": str(ctx.author), "by_id": ctx.author.id,
        "action": "unvouch", "reason": reason,
        "time": discord.utils.utcnow()
    })
    _save_vouches()
    _save_vouch_log()
    score = gv[member.id]

    # Strip every protected role this member currently holds — matches
    # the vouch-gated tier being revoked, not just the score dropping.
    protected_ids = PROTECTED_ROLES.get(ctx.guild.id, set())
    held_protected = [
        r for r in member.roles
        if r.id in protected_ids and r < ctx.guild.me.top_role
    ]
    if held_protected:
        try:
            await member.remove_roles(*held_protected, reason=f"Unvouched by {ctx.author} | {reason}")
        except (discord.Forbidden, discord.HTTPException):
            held_protected = []
    roles_note = ", ".join(r.name for r in held_protected) if held_protected else "*None held*"

    embed = discord.Embed(
        title="↩️ Vouch Removed",
        description=f"{ctx.author.mention} removed a vouch from {member.mention}.",
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="📝 Reason",            value=reason,      inline=False)
    embed.add_field(name="🔢 Remaining Vouches", value=str(score),  inline=True)
    embed.add_field(name="🏷️ Roles Stripped",    value=roles_note,  inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"TrapAI Vouch System • {ctx.guild.name}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)

    await log(ctx.guild, "vouches", "Vouch Removed", None, discord.Color.orange(),
              fields=[
                  ("🛡 By",             f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("↩️ From",           f"{member.mention} (`{member.id}`)",          True),
                  ("🔢 Score",          str(score),                                    True),
                  ("🏷️ Roles Stripped", roles_note,                                    False),
                  ("📝 Reason",         reason,                                        False),
              ],
              actor=ctx.author, target=member)


@bot.command()
async def vouches(ctx, member: discord.Member = None):
    """Show vouch count and history for a member. Usage: ,vouches [@user]"""
    member    = member or ctx.author
    score     = VOUCHES.get(ctx.guild.id, {}).get(member.id, 0)
    logs      = VOUCH_LOG.get(ctx.guild.id, {}).get(member.id, [])
    threshold = VOUCH_CONFIG.get(ctx.guild.id, {}).get("threshold", 3)

    embed = discord.Embed(
        title=f"✅ Vouch Profile — {member}",
        color=discord.Color.green() if score >= threshold else discord.Color.orange(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="🔢 Vouches",   value=f"**{score}**",            inline=True)
    embed.add_field(name="🎯 Threshold", value=f"{threshold} required",   inline=True)
    embed.add_field(name="✅ Trusted",   value="Yes" if score >= threshold else "No", inline=True)

    if logs:
        recent = logs[-5:]
        lines  = []
        for entry in reversed(recent):
            t  = entry["time"]
            ts = discord.utils.format_dt(t, "R") if hasattr(t, "tzinfo") else str(t)[:10]
            icon = "✅" if entry["action"] == "vouch" else "↩️"
            lines.append(f"{icon} **{entry['by']}** — {entry['reason']} ({ts})")
        embed.add_field(name=f"📋 Recent Activity (last {len(recent)})", value="\n".join(lines), inline=False)

    embed.set_footer(text=f"TrapAI Vouch System • Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command()
async def vouchleaderboard(ctx):
    """Show the top vouched members in the server. Usage: ,vouchleaderboard"""
    gv = VOUCHES.get(ctx.guild.id, {})
    if not gv:
        await ctx.send("📭 No vouch data yet in this server.")
        return

    sorted_v  = sorted(gv.items(), key=lambda x: x[1], reverse=True)[:10]
    threshold = VOUCH_CONFIG.get(ctx.guild.id, {}).get("threshold", 3)

    embed = discord.Embed(
        title="✅ Vouch Leaderboard",
        description=f"**Top vouched members in {ctx.guild.name}** (threshold: {threshold})",
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
    lines  = []
    for i, (uid, count) in enumerate(sorted_v):
        m    = ctx.guild.get_member(uid)
        name = m.mention if m else f"<@{uid}>"
        flag = "✅" if count >= threshold else "❌"
        lines.append(f"{medals[i]} {name} — **{count}** vouch(es) {flag}")
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command()
@_permitted_check(administrator=True)
async def vouchconfig(ctx, setting: str = None, value: str = None):
    """Configure the vouch system. Usage: ,vouchconfig threshold 3"""
    cfg = VOUCH_CONFIG.setdefault(ctx.guild.id, {"threshold": 3})

    if setting is None:
        embed = discord.Embed(
            title="⚙️ Vouch Config",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="🎯 Threshold", value=str(cfg.get("threshold", 3)), inline=True)
        embed.add_field(
            name="Commands",
            value="`,vouchconfig threshold <number>` — set required vouches to unlock power commands",
            inline=False
        )
        embed.set_footer(text=f"TrapAI Vouch System • {ctx.guild.name}")
        await ctx.send(embed=embed)
        return

    if setting.lower() == "threshold":
        try:
            n = int(value)
        except (TypeError, ValueError):
            await ctx.send("❌ Provide a number. Example: `,vouchconfig threshold 3`", delete_after=8)
            return
        if n < 0 or n > 50:
            await ctx.send("❌ Threshold must be 0–50.", delete_after=8)
            return
        cfg["threshold"] = n
        _save_vouch_config()
        embed = discord.Embed(
            title="✅ Vouch Threshold Updated",
            description=f"Members now need **{n}** vouch(es) to use power commands.",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Set by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        await log(ctx.guild, "mod", "Vouch Threshold Changed", None, discord.Color.blurple(),
                  fields=[("👑 Admin", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("🎯 New Threshold", str(n), True)],
                  actor=ctx.author)
    else:
        await ctx.send("❌ Unknown setting. Use `threshold`. Example: `,vouchconfig threshold 3`", delete_after=8)


# ============================================================
# PROTECTED ROLES SYSTEM
# ============================================================

@bot.command()
@_permitted_check(administrator=True)
async def protectedrole(ctx, action: str = None, role: discord.Role = None):
    """
    Manage the list of roles that can ONLY be granted via ,vouch — never manually.

    Usage:
      ,protectedrole list                — see all protected roles
      ,protectedrole add @role           — protect a role
      ,protectedrole remove @role        — unprotect a role
    """
    guild    = ctx.guild
    protected = PROTECTED_ROLES.setdefault(guild.id, set())

    # ── List ─────────────────────────────────────────────────
    if action is None or action.lower() == "list":
        embed = discord.Embed(
            title="🔒 Protected Roles",
            description=(
                "These roles **cannot** be granted manually in Discord.\n"
                "Anyone who tries will have it auto-stripped.\n"
                "They can only be given via `,vouch @user @role reason` → owner approval."
            ),
            color=discord.Color.dark_red(),
            timestamp=discord.utils.utcnow()
        )
        if protected:
            lines = []
            for rid in protected:
                r = guild.get_role(rid)
                lines.append(f"🔒 {r.mention} (`{rid}`)" if r else f"🔒 *deleted role* (`{rid}`)")
            embed.add_field(name=f"Protected Roles ({len(protected)})", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="No protected roles", value="Use `,protectedrole add @role` to add one.", inline=False)
        embed.set_footer(text=f"TrapAI • {guild.name}")
        await ctx.send(embed=embed)
        return

    if role is None:
        await ctx.send("❌ Provide a role. Example: `,protectedrole add @OG`", delete_after=8)
        return

    # ── Add ──────────────────────────────────────────────────
    if action.lower() == "add":
        if role.id in protected:
            await ctx.send(f"❌ **{role.name}** is already protected.", delete_after=6)
            return
        protected.add(role.id)
        _save_protected_roles()
        embed = discord.Embed(
            title="🔒 Role Protected",
            description=(
                f"**{role.name}** is now a **protected role**.\n\n"
                "• Manual grants in Discord will be **auto-stripped**\n"
                "• Members will be DM'd when a grant is blocked\n"
                "• The only way to grant it is via `,vouch @user @role reason`\n"
                "• Owner must approve each request"
            ),
            color=discord.Color.dark_red(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="🏷️ Role", value=f"{role.mention} (`{role.id}`)", inline=True)
        embed.set_footer(text=f"Set by {ctx.author} • TrapAI")
        await ctx.send(embed=embed)
        await log(guild, "mod", "Protected Role Added", None, discord.Color.dark_red(),
                  fields=[("🏷️ Role", f"{role.mention} (`{role.id}`)", True),
                           ("👑 By",   f"{ctx.author.mention}",         True)],
                  actor=ctx.author)

    # ── Remove ───────────────────────────────────────────────
    elif action.lower() == "remove":
        if role.id not in protected:
            await ctx.send(f"❌ **{role.name}** is not protected.", delete_after=6)
            return
        protected.discard(role.id)
        _save_protected_roles()
        embed = discord.Embed(
            title="🔓 Role Unprotected",
            description=f"**{role.name}** is no longer protected. It can be granted manually again.",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="🏷️ Role", value=f"{role.mention} (`{role.id}`)", inline=True)
        embed.set_footer(text=f"Set by {ctx.author} • TrapAI")
        await ctx.send(embed=embed)
        await log(guild, "mod", "Protected Role Removed", None, discord.Color.green(),
                  fields=[("🏷️ Role", f"{role.mention} (`{role.id}`)", True),
                           ("👑 By",   f"{ctx.author.mention}",         True)],
                  actor=ctx.author)

    else:
        await ctx.send("❌ Unknown action. Use `add`, `remove`, or `list`.", delete_after=8)


# ============================================================
# PERMITTED ROLES SYSTEM
# ============================================================

@bot.command(name="setpermittedrole")
@commands.has_permissions(administrator=True)
async def setpermittedrole(ctx, action: str = None, role: discord.Role = None, *, command_name: str = None):
    """
    Grant or revoke a role's permission to use a specific command, even
    without the underlying Discord permission that command normally
    requires. Administrator-only — this is a real privilege grant, so
    it's deliberately not itself permittable via this same system.

    Usage:
      ,setpermittedrole permit @role <command>    — grant
      ,setpermittedrole unpermit @role <command>  — revoke
      ,setpermittedrole list [@role]              — view current grants

    Example:
      ,setpermittedrole permit @Franchise ban
      ,setpermittedrole unpermit @Franchise ban
    """
    if action is None:
        await ctx.send(
            "❌ Usage: `,setpermittedrole permit @role <command>` / "
            "`,setpermittedrole unpermit @role <command>` / `,setpermittedrole list [@role]`",
            delete_after=10
        )
        return

    action = action.lower()
    guild = ctx.guild
    guild_cfg = PERMITTED_ROLES.setdefault(guild.id, {})

    if action == "list":
        lines = []
        for cmd_name, role_ids in sorted(guild_cfg.items()):
            if role is not None and role.id not in role_ids:
                continue
            mentions = ", ".join(f"<@&{rid}>" for rid in role_ids if guild.get_role(rid))
            if mentions:
                lines.append(f"`,{cmd_name}` — {mentions}")
        embed = discord.Embed(
            title="🔑 Permitted Role Grants",
            description="\n".join(lines)[:4000] if lines else "No grants configured" + (f" for {role.mention}." if role else "."),
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"TrapAI • {guild.name}")
        await ctx.send(embed=embed)
        return

    if action not in ("permit", "unpermit"):
        await ctx.send("❌ Unknown action. Use `permit`, `unpermit`, or `list`.", delete_after=8)
        return

    if role is None or not command_name:
        await ctx.send(f"❌ Usage: `,setpermittedrole {action} @role <command>`", delete_after=8)
        return

    cmd = bot.get_command(command_name.strip().lstrip(","))
    if cmd is None:
        await ctx.send(f"❌ No command named `{command_name}` found.", delete_after=8)
        return
    cmd_name = cmd.qualified_name

    if cmd_name == "setpermittedrole":
        await ctx.send("❌ `,setpermittedrole` itself can't be permitted this way — administrator-only, always.", delete_after=8)
        return

    if action == "permit":
        role_ids = guild_cfg.setdefault(cmd_name, set())
        if role.id in role_ids:
            await ctx.send(f"❌ {role.mention} is already permitted to use `,{cmd_name}`.", delete_after=8)
            return
        role_ids.add(role.id)
        _save_permitted_roles()
        embed = discord.Embed(
            title="✅ Role Permitted",
            description=f"{role.mention} can now use `,{cmd_name}` — even without the Discord permission it normally requires.",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Set by {ctx.author}")
        await ctx.send(embed=embed)
        await log(
            guild, "mod", "Permitted Role Granted",
            f"{ctx.author.mention} permitted {role.mention} to use `,{cmd_name}`.",
            discord.Color.green(),
            fields=[("🎭 Role", role.mention, True), ("⌨️ Command", f"`,{cmd_name}`", True)],
            actor=ctx.author
        )
        return

    # unpermit
    role_ids = guild_cfg.get(cmd_name, set())
    if role.id not in role_ids:
        await ctx.send(f"❌ {role.mention} isn't currently permitted to use `,{cmd_name}`.", delete_after=8)
        return
    role_ids.discard(role.id)
    if not role_ids:
        guild_cfg.pop(cmd_name, None)
    _save_permitted_roles()
    embed = discord.Embed(
        title="🚫 Role Unpermitted",
        description=f"{role.mention} can no longer use `,{cmd_name}` unless they hold the Discord permission it normally requires.",
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"Set by {ctx.author}")
    await ctx.send(embed=embed)
    await log(
        guild, "mod", "Permitted Role Revoked",
        f"{ctx.author.mention} revoked {role.mention}'s permission to use `,{cmd_name}`.",
        discord.Color.red(),
        fields=[("🎭 Role", role.mention, True), ("⌨️ Command", f"`,{cmd_name}`", True)],
        actor=ctx.author
    )


@bot.command()
@_permitted_check(administrator=True)
async def antiraid(ctx, state: str = None):
    """
    Turn this server's anti-raid auto-ban on or off. Usage: ,antiraid <on|off>
    Run with no argument to see the current state.
    """
    guild = ctx.guild
    if state is None:
        enabled = ANTI_RAID_ENABLED.get(guild.id, True)
        await ctx.send(f"🛡️ Anti-raid is currently **{'ON' if enabled else 'OFF'}** for this server.")
        return
    state = state.lower()
    if state not in ("on", "off"):
        await ctx.send("❌ Usage: `,antiraid <on|off>`", delete_after=8)
        return
    ANTI_RAID_ENABLED[guild.id] = (state == "on")
    _save_anti_raid_enabled()
    await ctx.send(f"🛡️ Anti-raid is now **{'ON' if state == 'on' else 'OFF'}** for this server.")
    await log(guild, "raids", "Anti-Raid Toggled", None,
              discord.Color.green() if state == "on" else discord.Color.orange(),
              fields=[("🛡️ State", state.upper(), True), ("👑 By", ctx.author.mention, True)],
              actor=ctx.author)


@bot.command()
@_permitted_check(administrator=True)
async def raidmode(ctx, state: str = None):
    """
    Full raid-lockdown toggle: locks every text channel, turns on
    anti-raid auto-ban, and raises server verification to High.
    Usage: ,raidmode <on|off>. Run with no argument to see the current state.
    """
    guild = ctx.guild
    current = RAID_MODE.get(guild.id, {})

    if state is None:
        active = current.get("active", False)
        await ctx.send(f"🚨 Raid mode is currently **{'ON' if active else 'OFF'}** for this server.")
        return

    state = state.lower()
    if state not in ("on", "off"):
        await ctx.send("❌ Usage: `,raidmode <on|off>`", delete_after=8)
        return

    if state == "on":
        if current.get("active"):
            await ctx.send("🚨 Raid mode is already **ON** for this server.")
            return

        # Snapshot BEFORE mutating anything, so ,raidmode off restores the
        # server's real prior settings instead of hardcoded defaults.
        RAID_MODE[guild.id] = {
            "active": True,
            "prev_verification_level": guild.verification_level.value,
            "prev_antiraid": ANTI_RAID_ENABLED.get(guild.id, True),
        }
        _save_raid_mode()

        await _set_all_channels_locked(guild, True, f"Raid mode activated by {ctx.author}")

        ANTI_RAID_ENABLED[guild.id] = True
        _save_anti_raid_enabled()

        verification_note = ""
        try:
            await guild.edit(verification_level=discord.VerificationLevel.high,
                              reason=f"Raid mode activated by {ctx.author}")
        except (discord.Forbidden, discord.HTTPException):
            verification_note = "\n⚠️ Couldn't raise verification level (missing permissions) — everything else is active."

        await ctx.send(f"🚨 **Raid mode activated.** All text channels locked, anti-raid is ON, "
                        f"and verification level is raised to High.{verification_note}")
        await log(guild, "raids", "🚨 Raid Mode Activated", None, discord.Color.dark_red(),
                  fields=[
                      ("👑 Admin", f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                      ("🔐 Channels Locked", str(len(guild.text_channels)), True),
                      ("🛡️ Anti-Raid", "ON", True),
                  ],
                  actor=ctx.author)
    else:
        if not current.get("active"):
            await ctx.send("🚨 Raid mode is already **OFF** for this server.")
            return

        await _set_all_channels_locked(guild, False, f"Raid mode deactivated by {ctx.author}")

        ANTI_RAID_ENABLED[guild.id] = current.get("prev_antiraid", True)
        _save_anti_raid_enabled()

        verification_note = ""
        try:
            prev_level = discord.VerificationLevel(current.get("prev_verification_level", guild.verification_level.value))
            await guild.edit(verification_level=prev_level, reason=f"Raid mode deactivated by {ctx.author}")
        except (discord.Forbidden, discord.HTTPException, ValueError):
            verification_note = "\n⚠️ Couldn't restore the prior verification level (missing permissions) — everything else was restored."

        RAID_MODE[guild.id]["active"] = False
        _save_raid_mode()

        await ctx.send(f"✅ **Raid mode deactivated.** Channels unlocked and prior settings restored.{verification_note}")
        await log(guild, "raids", "✅ Raid Mode Deactivated", None, discord.Color.green(),
                  fields=[
                      ("👑 Admin", f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                      ("🔓 Channels Unlocked", str(len(guild.text_channels)), True),
                  ],
                  actor=ctx.author)


@bot.command()
@_permitted_check(administrator=True)
async def raidwhitelist(ctx, action: str = None, member: discord.Member = None):
    """
    Manage members exempt from anti-raid auto-ban (e.g. known alts, bots
    being re-added during a raid window).

    Usage:
      ,raidwhitelist list          — see everyone whitelisted here
      ,raidwhitelist add @user     — exempt a member
      ,raidwhitelist remove @user  — un-exempt a member
    """
    guild = ctx.guild
    whitelist = WHITELIST.setdefault(guild.id, set())

    if action is None or action.lower() == "list":
        embed = discord.Embed(
            title="🛡️ Anti-Raid Whitelist",
            description="These members are exempt from anti-raid auto-ban in this server.",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        if whitelist:
            lines = [f"• <@{uid}> (`{uid}`)" for uid in whitelist]
            embed.add_field(name=f"Whitelisted ({len(whitelist)})", value="\n".join(lines)[:1024], inline=False)
        else:
            embed.add_field(name="No one whitelisted", value="Use `,raidwhitelist add @user` to add one.", inline=False)
        embed.set_footer(text=f"TrapAI • {guild.name}")
        await ctx.send(embed=embed)
        return

    if member is None:
        await ctx.send("❌ Provide a member. Example: `,raidwhitelist add @user`", delete_after=8)
        return

    if action.lower() == "add":
        if member.id in whitelist:
            await ctx.send(f"❌ {member.mention} is already whitelisted.", delete_after=6)
            return
        whitelist.add(member.id)
        _save_raid_whitelist()
        await ctx.send(f"✅ {member.mention} is now exempt from anti-raid auto-ban.")
    elif action.lower() == "remove":
        if member.id not in whitelist:
            await ctx.send(f"❌ {member.mention} is not whitelisted.", delete_after=6)
            return
        whitelist.discard(member.id)
        _save_raid_whitelist()
        await ctx.send(f"✅ {member.mention} removed from the anti-raid whitelist.")
    else:
        await ctx.send("❌ Unknown action. Use `add`, `remove`, or `list`.", delete_after=8)


@bot.command(name="wl", aliases=["antinukewl"])
@_permitted_check(administrator=True)
async def wl(ctx, *, arg: str = None):
    """
    Whitelist a member from ever triggering anti-nuke (rapid role/channel
    deletion protection) — for trusted staff who legitimately do bulk
    deletes without getting auto-stripped.
    Usage:
      ,wl @user           — whitelist a member
      ,wl remove @user    — un-whitelist a member
      ,wl list            — view the whitelist
    """
    guild = ctx.guild
    whitelist = ANTINUKE_WHITELIST.setdefault(guild.id, set())

    if not arg or arg.strip().lower() == "list":
        embed = discord.Embed(
            title="🚨 Anti-Nuke Whitelist",
            description="These members will never trigger anti-nuke, no matter how many roles/channels they delete.",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        if whitelist:
            lines = [f"• <@{uid}> (`{uid}`)" for uid in whitelist]
            embed.add_field(name=f"Whitelisted ({len(whitelist)})", value="\n".join(lines)[:1024], inline=False)
        else:
            embed.add_field(name="No one whitelisted", value="Use `,wl @user` to add one.", inline=False)
        embed.set_footer(text=f"TrapAI • {guild.name}")
        await ctx.send(embed=embed)
        return

    parts = arg.strip().split(maxsplit=1)
    action = "add"
    target_raw = arg.strip()
    if parts[0].lower() in ("remove", "add") and len(parts) > 1:
        action = parts[0].lower()
        target_raw = parts[1]

    try:
        member = await commands.MemberConverter().convert(ctx, target_raw.strip())
    except commands.MemberNotFound:
        await ctx.send("❌ Member not found. Use `,wl @user`, `,wl remove @user`, or `,wl list`.", delete_after=8)
        return

    if action == "add":
        if member.id in whitelist:
            await ctx.send(f"❌ {member.mention} is already whitelisted.", delete_after=6)
            return
        whitelist.add(member.id)
        _save_antinuke_whitelist()
        await ctx.send(f"✅ {ctx.author.mention}: **{member.name}** is now whitelisted and will not trigger **antinuke**.")
        await log(
            guild, "mod", "Anti-Nuke Whitelist Added",
            f"{ctx.author.mention} whitelisted {member.mention} from anti-nuke.",
            discord.Color.green(),
            fields=[("👤 Member", f"{member.mention} (`{member.id}`)", True)],
            actor=ctx.author
        )
    else:
        if member.id not in whitelist:
            await ctx.send(f"❌ {member.mention} is not whitelisted.", delete_after=6)
            return
        whitelist.discard(member.id)
        _save_antinuke_whitelist()
        await ctx.send(f"✅ {ctx.author.mention}: **{member.name}** removed from the anti-nuke whitelist.")
        await log(
            guild, "mod", "Anti-Nuke Whitelist Removed",
            f"{ctx.author.mention} removed {member.mention} from the anti-nuke whitelist.",
            discord.Color.orange(),
            fields=[("👤 Member", f"{member.mention} (`{member.id}`)", True)],
            actor=ctx.author
        )


# ============================================================
# VOUCH EXTENDED COMMANDS
# ============================================================

@bot.command()
@_permitted_check(administrator=True)
async def pendingvouches(ctx):
    """List all open vouch-role requests awaiting owner approval."""
    guild   = ctx.guild
    pending = ROLE_VOUCH_PENDING.get(guild.id, {})

    if not pending:
        embed = discord.Embed(
            title="📋 Pending Vouch-Role Requests",
            description="✅ No pending requests — all clear!",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"TrapAI Vouch System • {guild.name}")
        await ctx.send(embed=embed)
        return

    embed = discord.Embed(
        title=f"⏳ Pending Vouch-Role Requests ({len(pending)})",
        description=(
            "These requests are **waiting for owner approval**.\n"
            "Owner must approve or reject via the DM they received.\n"
            "Use `,cancelvouch @user @role` to withdraw any of these."
        ),
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )

    for i, (token, data) in enumerate(list(pending.items())[:15], 1):
        member    = guild.get_member(data["member_id"])
        role      = guild.get_role(data["role_id"])
        requester = guild.get_member(data["requester_id"])
        m_str   = member.mention    if member    else f"`{data['member_id']}`"
        r_str   = role.mention      if role      else f"`{data['role_id']}`"
        req_str = requester.mention if requester else f"`{data['requester_id']}`"
        embed.add_field(
            name=f"#{i}  {member or data['member_id']}  →  {role.name if role else data['role_id']}",
            value=(
                f"👤 **Member:** {m_str}\n"
                f"🏷️ **Role:** {r_str}\n"
                f"📨 **Requested by:** {req_str}\n"
                f"📝 **Reason:** {data['reason']}\n"
                f"🔑 **Token:** `{token}`"
            ),
            inline=False
        )

    if len(pending) > 15:
        embed.add_field(name="…", value=f"*+ {len(pending) - 15} more not shown*", inline=False)

    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text=f"TrapAI Vouch System • {guild.name}")
    await ctx.send(embed=embed)


@bot.command()
@_permitted_check(administrator=True)
async def cancelvouch(ctx, member: discord.Member = None, role: discord.Role = None):
    """Cancel a pending vouch-role request. Usage: ,cancelvouch @user @role"""
    if member is None or role is None:
        await ctx.send("❌ Usage: `,cancelvouch @user @role`", delete_after=8)
        return

    guild   = ctx.guild
    pending = ROLE_VOUCH_PENDING.get(guild.id, {})

    # Find the token matching member + role
    found_token = None
    found_data  = None
    for token, data in pending.items():
        if data["member_id"] == member.id and data["role_id"] == role.id:
            found_token = token
            found_data  = data
            break

    if not found_token:
        await ctx.send(
            f"❌ No pending request found for {member.mention} → {role.mention}.\n"
            "Use `,pendingvouches` to see all open requests.",
            delete_after=8
        )
        return

    pending.pop(found_token)
    _save_role_vouch_pending()
    requester = guild.get_member(found_data["requester_id"])

    # Notify the requester their request was cancelled
    if requester and requester != ctx.author:
        try:
            dm = discord.Embed(
                title="🗑️ Vouch Request Cancelled",
                description=(
                    f"Your vouch-role request for **{member}** to receive **{role.name}** "
                    f"in **{guild.name}** was **cancelled** by {ctx.author.mention}."
                ),
                color=discord.Color.orange(),
                timestamp=discord.utils.utcnow()
            )
            dm.add_field(name="👤 Member",       value=str(member),     inline=True)
            dm.add_field(name="🏷️ Role",          value=role.name,       inline=True)
            dm.add_field(name="🗑️ Cancelled by",  value=str(ctx.author), inline=True)
            if guild.icon:
                dm.set_thumbnail(url=guild.icon.url)
            dm.set_footer(text=f"TrapAI • {guild.name}")
            await requester.send(embed=dm)
        except (discord.Forbidden, discord.HTTPException):
            pass

    embed = discord.Embed(
        title="🗑️ Vouch Request Cancelled",
        description=f"The pending request for {member.mention} → {role.mention} has been withdrawn.",
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="👤 Member",                 value=member.mention,        inline=True)
    embed.add_field(name="🏷️ Role",                   value=role.mention,           inline=True)
    embed.add_field(name="📝 Original Reason",        value=found_data["reason"],  inline=False)
    if requester:
        embed.add_field(name="📨 Originally requested by", value=requester.mention, inline=True)
    embed.set_footer(text=f"Cancelled by {ctx.author} • TrapAI • {guild.name}")
    await ctx.send(embed=embed)

    await log(guild, "vouches", "Vouch Request Cancelled", None, discord.Color.orange(),
              fields=[
                  ("👤 Member",       f"{member.mention} (`{member.id}`)", True),
                  ("🏷️ Role",         f"{role.mention}",                   True),
                  ("🗑️ Cancelled by", f"{ctx.author.mention}",             True),
                  ("📝 Reason",       found_data["reason"],                 False),
              ], actor=ctx.author)


@bot.command()
async def vouchstats(ctx):
    """Server-wide vouch analytics. Usage: ,vouchstats"""
    guild     = ctx.guild
    gv        = VOUCHES.get(guild.id, {})
    gl        = VOUCH_LOG.get(guild.id, {})
    threshold = VOUCH_CONFIG.get(guild.id, {}).get("threshold", 3)
    protected = PROTECTED_ROLES.get(guild.id, set())
    pending   = ROLE_VOUCH_PENDING.get(guild.id, {})

    total_vouches   = sum(gv.values())
    trusted_count   = sum(1 for v in gv.values() if v >= threshold)
    all_entries     = [e for logs in gl.values() for e in logs]
    vouch_actions   = [e for e in all_entries if e["action"] == "vouch"]
    unvouch_actions = [e for e in all_entries if e["action"] == "unvouch"]
    role_grants     = [e for e in all_entries if e["action"] == "vouch-role"]

    # Top vouchers — who gave the most
    giver_counts: dict[int, int] = {}
    for e in vouch_actions:
        giver_counts[e["by_id"]] = giver_counts.get(e["by_id"], 0) + 1
    top_givers = sorted(giver_counts.items(), key=lambda x: x[1], reverse=True)[:3]

    embed = discord.Embed(
        title=f"📈 Vouch Stats — {guild.name}",
        color=discord.Color.from_rgb(87, 242, 135),
        timestamp=discord.utils.utcnow()
    )

    embed.add_field(
        name="📊 Overview",
        value=(
            f"Total vouches given: **{total_vouches}**\n"
            f"Unique vouched members: **{len(gv)}**\n"
            f"Trusted members: **{trusted_count}** (≥ {threshold} vouches)\n"
            f"Vouches given: **{len(vouch_actions)}**  •  Removed: **{len(unvouch_actions)}**"
        ),
        inline=False
    )

    embed.add_field(
        name="🔒 Protected Roles",
        value=(
            f"Protected roles configured: **{len(protected)}**\n"
            f"Pending requests (awaiting owner): **{len(pending)}**\n"
            f"All-time approved role grants: **{len(role_grants)}**"
        ),
        inline=False
    )

    if top_givers:
        medals = ["🥇", "🥈", "🥉"]
        lines  = []
        for i, (uid, count) in enumerate(top_givers):
            m    = guild.get_member(uid)
            name = m.mention if m else f"`{uid}`"
            lines.append(f"{medals[i]} {name} — **{count}** vouch(es) given")
        embed.add_field(name="🏆 Top Vouchers", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="🏆 Top Vouchers", value="No vouch data yet.", inline=False)

    # Trust distribution
    b = {"🔴 New (0)": 0, "🟡 Rising (1–2)": 0, "🟢 Trusted": 0, "🌟 Elite (3×+)": 0}
    for v in gv.values():
        if v == 0:               b["🔴 New (0)"] += 1
        elif v < threshold:      b["🟡 Rising (1–2)"] += 1
        elif v < threshold * 3:  b["🟢 Trusted"] += 1
        else:                    b["🌟 Elite (3×+)"] += 1
    dist = "  •  ".join(f"{k}: **{n}**" for k, n in b.items() if n > 0) or "No data yet."
    embed.add_field(name="📉 Trust Distribution", value=dist, inline=False)

    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text=f"TrapAI Vouch System • {guild.name}")
    await ctx.send(embed=embed)


# ============================================================
# AUTO-ROLE SYSTEM
# ============================================================

@bot.command()
@_permitted_check(administrator=True)
async def autorole(ctx, action: str = None, role: discord.Role = None):
    """
    Manage roles automatically given to every new member on join.

    Usage:
      ,autorole              — view current auto-roles
      ,autorole add @role    — add a role to the auto-role list
      ,autorole remove @role — remove a role from the list
      ,autorole clear        — remove all auto-roles
    """
    guild      = ctx.guild
    auto_roles = AUTOROLE.setdefault(guild.id, [])

    # ── View ────────────────────────────────────────────────
    if action is None or action.lower() == "list":
        embed = discord.Embed(
            title="🎭 Auto-Role Config",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        if auto_roles:
            lines = []
            for rid in auto_roles:
                r = guild.get_role(rid)
                lines.append(f"• {r.mention} (`{rid}`)" if r else f"• *deleted role* (`{rid}`)")
            embed.add_field(
                name=f"Roles given on join ({len(auto_roles)})",
                value="\n".join(lines),
                inline=False
            )
        else:
            embed.description = "No auto-roles configured.\nUse `,autorole add @role` to add one."
        embed.set_footer(text=f"TrapAI Auto-Role • {guild.name}")
        await ctx.send(embed=embed)
        return

    # ── Add ─────────────────────────────────────────────────
    if action.lower() == "add":
        if role is None:
            await ctx.send("❌ Provide a role. Example: `,autorole add @Member`", delete_after=8)
            return
        if role.id in auto_roles:
            await ctx.send(f"❌ {role.mention} is already in the auto-role list.", delete_after=6)
            return
        if role >= ctx.guild.me.top_role:
            await ctx.send(f"❌ I can't assign **{role.name}** — it's above my highest role.", delete_after=8)
            return
        auto_roles.append(role.id)
        _save_autorole()
        embed = discord.Embed(
            title="✅ Auto-Role Added",
            description=f"{role.mention} will now be given to every new member on join.",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="🏷️ Role",        value=f"{role.mention} (`{role.id}`)", inline=True)
        embed.add_field(name="📋 Total Roles",  value=str(len(auto_roles)),            inline=True)
        embed.set_footer(text=f"Set by {ctx.author} • TrapAI")
        await ctx.send(embed=embed)
        await log(guild, "mod", "Auto-Role Added", None, discord.Color.green(),
                  fields=[("🏷️ Role", f"{role.mention} (`{role.id}`)", True),
                           ("👑 By",   f"{ctx.author.mention}",         True)],
                  actor=ctx.author)

    # ── Remove ───────────────────────────────────────────────
    elif action.lower() == "remove":
        if role is None:
            await ctx.send("❌ Provide a role. Example: `,autorole remove @Member`", delete_after=8)
            return
        if role.id not in auto_roles:
            await ctx.send(f"❌ {role.mention} is not in the auto-role list.", delete_after=6)
            return
        auto_roles.remove(role.id)
        _save_autorole()
        embed = discord.Embed(
            title="↩️ Auto-Role Removed",
            description=f"{role.mention} will no longer be given to new members.",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="🏷️ Role",        value=f"{role.mention} (`{role.id}`)", inline=True)
        embed.add_field(name="📋 Remaining",    value=str(len(auto_roles)),            inline=True)
        embed.set_footer(text=f"Set by {ctx.author} • TrapAI")
        await ctx.send(embed=embed)
        await log(guild, "mod", "Auto-Role Removed", None, discord.Color.orange(),
                  fields=[("🏷️ Role", f"{role.mention} (`{role.id}`)", True),
                           ("👑 By",   f"{ctx.author.mention}",         True)],
                  actor=ctx.author)

    # ── Clear ────────────────────────────────────────────────
    elif action.lower() == "clear":
        count = len(auto_roles)
        auto_roles.clear()
        _save_autorole()
        embed = discord.Embed(
            title="🗑️ Auto-Roles Cleared",
            description=f"Removed all **{count}** auto-role(s). No roles will be auto-assigned on join.",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Cleared by {ctx.author} • TrapAI")
        await ctx.send(embed=embed)
        await log(guild, "mod", "Auto-Roles Cleared", None, discord.Color.red(),
                  fields=[("🗑️ Removed", f"**{count}** role(s)", True),
                           ("👑 By",      f"{ctx.author.mention}", True)],
                  actor=ctx.author)

    else:
        await ctx.send(
            "❌ Unknown action. Use `add`, `remove`, `clear`, or just `,autorole` to view.",
            delete_after=8
        )


# ============================================================
# SELF-ROLES — reaction-based role picker (Carl-bot style)
# ============================================================
# REACTION_ROLES[guild_id][message_id][emoji_key] = {"role_id": int, "label": str|None}
# emoji_key is always str(discord.PartialEmoji) so unicode emoji ("🎮")
# and custom guild emoji ("<:name:id>") both work as dict keys the exact
# same way Discord's own raw reaction payloads report them.
REACTION_ROLES: dict[int, dict[int, dict]] = _load_depth(_load_data("reaction_roles", {}), 2)


def _save_reaction_roles():
    _save_data("reaction_roles", _dump_depth(REACTION_ROLES, 2))


def _parse_message_ref(text: str):
    m = re.search(r"(\d{17,20})$", text.strip())
    return int(m.group(1)) if m else None


def _reaction_role_panel_lines(guild: discord.Guild, mapping: dict) -> str:
    lines = []
    for emoji_key, info in mapping.items():
        role = guild.get_role(info["role_id"])
        if not role:
            continue
        lines.append(f"{emoji_key} → {role.mention}" + (f" — {info['label']}" if info.get("label") else ""))
    return "\n".join(lines) if lines else "*No roles added yet — use `,addrole` to add some.*"


async def _find_guild_message(guild: discord.Guild, message_id: int):
    for channel in guild.text_channels:
        try:
            return await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden):
            continue
    return None


@bot.command(aliases=["rolemenu"])
@_permitted_check(manage_roles=True)
async def createrolemenu(ctx, *, title: str = "🎭 Self Roles"):
    """
    Post a self-role panel — members react with an emoji to get that
    role, and remove their reaction to remove it (age roles, gender
    roles, giveaway ping roles, whatever you want). Add options
    afterward with ,addrole.
    Usage: ,createrolemenu <title>
    """
    embed = discord.Embed(
        title=title,
        description="React below to grab a role! React again to remove it.\n\n*No roles added yet — use `,addrole` to add some.*",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"TrapAI Self-Roles • {ctx.guild.name}")
    msg = await ctx.send(embed=embed)
    REACTION_ROLES.setdefault(ctx.guild.id, {})[msg.id] = {}
    _save_reaction_roles()
    await ctx.send(
        f"✅ Self-role panel created — message ID `{msg.id}`.\n"
        f"Add roles with `,addrole {msg.id} <emoji> @role [label]`.",
        delete_after=20
    )


@bot.command()
@_permitted_check(manage_roles=True)
async def addrole(ctx, message_ref: str, emoji: str, role: discord.Role, *, label: str = None):
    """
    Add an emoji → role mapping to a self-role panel created with
    ,createrolemenu — reacting with that emoji grants the role.
    Usage: ,addrole <message link/ID> <emoji> @role [label]
    e.g. ,addrole 123456789012345678 🧑 @Male gender role
    """
    guild = ctx.guild
    message_id = _parse_message_ref(message_ref)
    mapping = REACTION_ROLES.get(guild.id, {}).get(message_id) if message_id else None
    if mapping is None:
        await ctx.send("❌ That's not a known self-role message. Create one with `,createrolemenu` first.", delete_after=8)
        return
    if role.managed:
        await ctx.send(f"❌ {role.mention} is a managed role and can't be self-assigned.", delete_after=8)
        return
    if role >= guild.me.top_role:
        await ctx.send(_role_forbidden_reason(guild))
        return

    try:
        partial = discord.PartialEmoji.from_str(emoji)
    except (TypeError, ValueError):
        await ctx.send("❌ That doesn't look like a valid emoji.", delete_after=6)
        return

    message = await _find_guild_message(guild, message_id)
    if message is None:
        await ctx.send("❌ Couldn't find that message — was it deleted?", delete_after=8)
        return
    try:
        await message.add_reaction(partial)
    except discord.HTTPException:
        await ctx.send("❌ I couldn't react with that emoji — is it from another server, or did I mistype it?", delete_after=10)
        return

    emoji_key = str(partial)
    mapping[emoji_key] = {"role_id": role.id, "label": label}
    _save_reaction_roles()

    if message.embeds:
        embed = message.embeds[0]
        embed.description = "React below to grab a role! React again to remove it.\n\n" + _reaction_role_panel_lines(guild, mapping)
        try:
            await message.edit(embed=embed)
        except discord.HTTPException:
            pass

    await ctx.send(f"✅ {emoji_key} now grants {role.mention}.", delete_after=8)


@bot.command()
@_permitted_check(manage_roles=True)
async def removerole(ctx, message_ref: str, emoji: str):
    """
    Remove an emoji → role mapping from a self-role panel.
    Usage: ,removerole <message link/ID> <emoji>
    """
    guild = ctx.guild
    message_id = _parse_message_ref(message_ref)
    mapping = REACTION_ROLES.get(guild.id, {}).get(message_id) if message_id else None
    if mapping is None:
        await ctx.send("❌ That's not a known self-role message.", delete_after=8)
        return
    try:
        partial = discord.PartialEmoji.from_str(emoji)
    except (TypeError, ValueError):
        await ctx.send("❌ That doesn't look like a valid emoji.", delete_after=6)
        return
    emoji_key = str(partial)
    if emoji_key not in mapping:
        await ctx.send("❌ That emoji isn't mapped on this panel.", delete_after=6)
        return

    mapping.pop(emoji_key)
    _save_reaction_roles()

    message = await _find_guild_message(guild, message_id)
    if message:
        try:
            await message.clear_reaction(partial)
        except discord.HTTPException:
            pass
        if message.embeds:
            embed = message.embeds[0]
            embed.description = "React below to grab a role! React again to remove it.\n\n" + _reaction_role_panel_lines(guild, mapping)
            try:
                await message.edit(embed=embed)
            except discord.HTTPException:
                pass

    await ctx.send(f"✅ Removed {emoji_key} from the panel.", delete_after=8)


@bot.command(aliases=["listrolemenus"])
@_permitted_check(manage_roles=True)
async def rolemenus(ctx):
    """
    List every self-role panel in this server with a clickable jump link
    and its current emoji → role mappings — use this to get the right
    message ID for ,addrole/,removerole instead of guessing.
    Usage: ,rolemenus
    """
    guild = ctx.guild
    panels = REACTION_ROLES.get(guild.id, {})
    if not panels:
        await ctx.send("📭 No self-role panels created yet. Use `,createrolemenu` first.")
        return

    embed = discord.Embed(
        title="🎭 Self-Role Panels",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    for message_id, mapping in panels.items():
        message = await _find_guild_message(guild, message_id)
        location = f"[Jump to message]({message.jump_url})" if message else "*⚠️ message not found — may have been deleted*"
        embed.add_field(
            name=f"ID: {message_id}",
            value=f"{location}\n{_reaction_role_panel_lines(guild, mapping)}",
            inline=False
        )
    embed.set_footer(text=f"TrapAI Self-Roles • {guild.name}")
    await ctx.send(embed=embed)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None or payload.user_id == bot.user.id:
        return
    mapping = REACTION_ROLES.get(payload.guild_id, {}).get(payload.message_id)
    if not mapping:
        return
    info = mapping.get(str(payload.emoji))
    if not info:
        return
    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    role = guild.get_role(info["role_id"])
    member = payload.member or guild.get_member(payload.user_id)
    if not role or not member:
        return
    try:
        await member.add_roles(role, reason="Self-role reaction")
    except (discord.Forbidden, discord.HTTPException):
        pass


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None:
        return
    mapping = REACTION_ROLES.get(payload.guild_id, {}).get(payload.message_id)
    if not mapping:
        return
    info = mapping.get(str(payload.emoji))
    if not info:
        return
    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    role = guild.get_role(info["role_id"])
    member = guild.get_member(payload.user_id)
    if member is None:
        try:
            member = await guild.fetch_member(payload.user_id)
        except discord.NotFound:
            return
    if payload.user_id == bot.user.id or not role or not member:
        return
    try:
        await member.remove_roles(role, reason="Self-role reaction removed")
    except (discord.Forbidden, discord.HTTPException):
        pass


# ============================================================
# HARD-BAN SYSTEM
# ============================================================

@bot.command()
@_permitted_check(ban_members=True)
async def hardban(ctx, user: discord.User, *, reason: str = "No reason provided"):
    """
    Permanently hard-ban a user — they will be instantly re-banned if they rejoin.
    Usage: ,hardban @user reason
    """
    guild = ctx.guild
    HARD_BANNED.setdefault(guild.id, {})[user.id] = reason
    _save_hard_banned()
    _log_mod_action(guild.id, user.id, "hardban", ctx.author, reason)

    async def _do_ban():
        try:
            await guild.ban(user, reason=f"Hard-ban by {ctx.author}: {reason}", delete_message_days=1)
        except discord.HTTPException:
            pass  # Already banned or left — still record it

    # DM concurrently with the ban itself — no need to serialize these
    await asyncio.gather(
        _dm_action(user, guild, "hardban", ctx.author, reason),
        _do_ban(),
    )

    embed = discord.Embed(
        title="🔴 Hard-Ban Applied",
        description=(
            f"{user.mention} has been **hard-banned** from **{guild.name}**.\n"
            "They will be **instantly re-banned** if they ever rejoin."
        ),
        color=discord.Color.dark_red(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="🔴 User",   value=f"{user} (`{user.id}`)", inline=True)
    embed.add_field(name="🛡 By",     value=ctx.author.mention,      inline=True)
    embed.add_field(name="📝 Reason", value=reason,                   inline=False)
    if hasattr(user, "display_avatar"):
        embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=f"TrapAI Hard-Ban System • {ctx.guild.name}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)

    await log(guild, "bans", "Hard-Ban Applied", None, discord.Color.dark_red(),
              fields=[
                  ("🛡 Moderator",        f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("🔴 User",             f"{user} (`{user.id}`)",                      True),
                  ("📝 Reason",           reason,                                        False),
                  ("🔁 Rejoin Protection", "Active — will auto re-ban",                 True),
                  ("📨 DM Sent",          "✅ Notified via DM",                          True),
              ],
              actor=ctx.author)


@bot.command()
@_permitted_check(ban_members=True)
async def unhardban(ctx, user_id: int, *, reason: str = "No reason provided"):
    """
    Remove a hard-ban and unban the user.
    Usage: ,unhardban 123456789012345678 reason
    """
    guild = ctx.guild
    hb    = HARD_BANNED.get(guild.id, {})

    if user_id not in hb:
        await ctx.send(f"❌ User ID `{user_id}` is not hard-banned.", delete_after=8)
        return

    original_reason = hb.pop(user_id)
    _save_hard_banned()
    _log_mod_action(guild.id, user_id, "unhardban", ctx.author, reason)
    user = None
    try:
        user = await bot.fetch_user(user_id)
        await guild.unban(user, reason=f"Hard-ban removed by {ctx.author}: {reason}")
    except (discord.NotFound, discord.HTTPException):
        pass

    user_str  = f"{user} (`{user_id}`)" if user else f"ID: `{user_id}`"
    thumbnail = user.display_avatar.url if user and hasattr(user, "display_avatar") else None

    embed = discord.Embed(
        title="✅ Hard-Ban Removed",
        description=f"The hard-ban on **{user_str}** has been lifted. They may now rejoin.",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="🛡 Removed By",      value=ctx.author.mention,  inline=True)
    embed.add_field(name="📝 Original Reason", value=original_reason,     inline=False)
    embed.add_field(name="📝 Removal Reason",  value=reason,               inline=False)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    embed.set_footer(text=f"TrapAI Hard-Ban System • {ctx.guild.name}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)

    await log(guild, "bans", "Hard-Ban Removed", None, discord.Color.green(),
              fields=[
                  ("🛡 Moderator",     f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("✅ Unbanned User", user_str,                                     True),
                  ("📝 Removal Reason", reason,                                      False),
              ],
              actor=ctx.author)


# ── Mass Ban / Mass Unban ────────────────────────────────────
_MENTION_ID_RE = re.compile(r"^<@!?(\d{15,20})>$")


def _parse_user_id_tokens(tokens) -> tuple[list[int], list[str]]:
    """Parse raw command tokens (bare snowflake IDs or <@id>/<@!id> mentions)
    into (unique valid user ids in order, tokens that didn't parse)."""
    valid, invalid, seen = [], [], set()
    for tok in tokens:
        m = _MENTION_ID_RE.match(tok)
        raw = m.group(1) if m else tok
        if raw.isdigit() and 15 <= len(raw) <= 20:
            uid = int(raw)
            if uid not in seen:
                seen.add(uid)
                valid.append(uid)
        else:
            invalid.append(tok)
    return valid, invalid


class _MassActionConfirmView(discord.ui.View):
    """Shared yes/no confirmation for ,massban / ,massunban — these can
    affect hundreds of accounts in one command, so nothing runs without an
    explicit click from whoever ran it."""
    def __init__(self, author_id: int):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Only the person who ran the command can confirm.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self.stop()
        await interaction.response.edit_message(content="⏳ Processing...", embed=None, view=None)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="❌ Cancelled.", embed=None, view=None)


@bot.command(aliases=["massb"])
@_permitted_check(ban_members=True)
async def massban(ctx, *targets: str):
    """
    Ban multiple users at once by ID or mention — for quickly removing a
    wave of raid/nuke accounts. Usage:
      ,massban <id/mention> <id/mention> ...
    Asks for confirmation before banning anyone. Max 100 per run.
    """
    guild = ctx.guild
    if not targets:
        await ctx.send("❌ Usage: `,massban <id/mention> <id/mention> ...`", delete_after=10)
        return

    ids, invalid = _parse_user_id_tokens(targets)
    protected = {ctx.author.id, bot.user.id, guild.owner_id}
    skipped_protected = [uid for uid in ids if uid in protected]
    ids = [uid for uid in ids if uid not in protected]

    if not ids:
        await ctx.send("❌ No valid user IDs/mentions to ban (or everything given was a protected account).", delete_after=10)
        return
    if len(ids) > 100:
        await ctx.send(f"❌ That's {len(ids)} targets — max 100 per run. Split into batches.", delete_after=12)
        return

    warn_lines = []
    if invalid:
        warn_lines.append(f"⚠️ Skipped {len(invalid)} unrecognized token(s).")
    if skipped_protected:
        warn_lines.append(f"⚠️ Skipped {len(skipped_protected)} protected account(s) (you, the bot, or the server owner).")

    preview = ", ".join(f"`{uid}`" for uid in ids[:20])
    if len(ids) > 20:
        preview += f", … +{len(ids) - 20} more"

    embed = discord.Embed(
        title="⚠️ Confirm Mass Ban",
        description=(
            f"You're about to ban **{len(ids)}** user(s) from **{guild.name}**.\n\n{preview}"
            + ("\n\n" + "\n".join(warn_lines) if warn_lines else "")
        ),
        color=discord.Color.red(),
    )
    view = _MassActionConfirmView(ctx.author.id)
    await ctx.send(embed=embed, view=view)
    await view.wait()
    if not view.confirmed:
        return

    banned = 0
    failed = 0
    for uid in ids:
        try:
            await guild.ban(discord.Object(id=uid), reason=f"Mass ban by {ctx.author}", delete_message_days=0)
            banned += 1
            _log_mod_action(guild.id, uid, "massban", ctx.author, f"Mass ban by {ctx.author}")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            failed += 1

    embed2 = discord.Embed(
        title="🔨 Mass Ban Complete",
        description=f"Banned **{banned}**/{len(ids)} user(s) from **{guild.name}**.",
        color=discord.Color.dark_red(),
        timestamp=discord.utils.utcnow()
    )
    if failed:
        embed2.add_field(name="⚠️ Failed", value=str(failed), inline=True)
    await ctx.send(embed=embed2)
    await log(guild, "bans", "Mass Ban Executed", None, discord.Color.dark_red(),
              fields=[
                  ("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("🔨 Banned",    f"{banned}/{len(ids)}",                       True),
                  ("👤 IDs",       ", ".join(str(u) for u in ids[:25])[:1024],  False),
              ],
              actor=ctx.author)


@bot.command(aliases=["massunb"])
@_permitted_check(ban_members=True)
async def massunban(ctx, *targets: str):
    """
    Unban multiple users at once — for recovering from a mass-ban
    attack/nuke attempt. Usage:
      ,massunban <id/mention> <id/mention> ...   — unban specific users
      ,massunban all                              — unban EVERY currently banned member
    Skips anyone on the ,hardban list — unban those with ,unhardban
    specifically, since a plain unban would just get them instantly
    re-banned (see the hard-ban rejoin protection).
    """
    guild = ctx.guild
    if not targets:
        await ctx.send("❌ Usage: `,massunban <id/mention> <id/mention> ...` or `,massunban all`", delete_after=10)
        return

    hard_banned_ids = set(HARD_BANNED.get(guild.id, {}).keys())
    invalid = []

    if len(targets) == 1 and targets[0].lower() == "all":
        try:
            ids = [entry.user.id async for entry in guild.bans(limit=None)]
        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to view this server's ban list.", delete_after=8)
            return
    else:
        ids, invalid = _parse_user_id_tokens(targets)

    skipped_hardbanned = [uid for uid in ids if uid in hard_banned_ids]
    ids = [uid for uid in ids if uid not in hard_banned_ids]

    if not ids:
        msg = "❌ Nothing to unban."
        if skipped_hardbanned:
            msg += f" ({len(skipped_hardbanned)} skipped — hard-banned, use `,unhardban` instead.)"
        await ctx.send(msg, delete_after=10)
        return
    if len(ids) > 1000:
        await ctx.send(f"❌ That's {len(ids)} users — narrow it down or run this in batches.", delete_after=12)
        return

    warn_lines = []
    if invalid:
        warn_lines.append(f"⚠️ Skipped {len(invalid)} unrecognized token(s).")
    if skipped_hardbanned:
        warn_lines.append(f"⚠️ Skipped {len(skipped_hardbanned)} hard-banned user(s) — use `,unhardban` for those.")

    preview = ", ".join(f"`{uid}`" for uid in ids[:20])
    if len(ids) > 20:
        preview += f", … +{len(ids) - 20} more"

    embed = discord.Embed(
        title="⚠️ Confirm Mass Unban",
        description=(
            f"You're about to unban **{len(ids)}** user(s) from **{guild.name}**.\n\n{preview}"
            + ("\n\n" + "\n".join(warn_lines) if warn_lines else "")
        ),
        color=discord.Color.green(),
    )
    view = _MassActionConfirmView(ctx.author.id)
    await ctx.send(embed=embed, view=view)
    await view.wait()
    if not view.confirmed:
        return

    unbanned = 0
    failed = 0
    for uid in ids:
        try:
            await guild.unban(discord.Object(id=uid), reason=f"Mass unban by {ctx.author}")
            unbanned += 1
            _log_mod_action(guild.id, uid, "massunban", ctx.author, f"Mass unban by {ctx.author}")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            failed += 1

    embed2 = discord.Embed(
        title="✅ Mass Unban Complete",
        description=(
            f"Unbanned **{unbanned}**/{len(ids)} user(s) from **{guild.name}**.\n"
            "Unbanning doesn't put them back in the server by itself — use `,pullback <id/mention> ...` "
            "to bring them back."
        ),
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    if failed:
        embed2.add_field(name="⚠️ Failed", value=str(failed), inline=True)
    if skipped_hardbanned:
        embed2.add_field(name="🔴 Skipped (hard-banned)", value=str(len(skipped_hardbanned)), inline=True)
    await ctx.send(embed=embed2)
    await log(guild, "bans", "Mass Unban Executed", None, discord.Color.green(),
              fields=[
                  ("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("✅ Unbanned",  f"{unbanned}/{len(ids)}",                     True),
              ],
              actor=ctx.author)


@bot.command(aliases=["pull"])
@_permitted_check(ban_members=True)
async def pullback(ctx, *targets: str):
    """
    Re-add former members straight back into the server — no invite link,
    no DM. This only works for members who've previously clicked the
    "Authenticate via Discord" verify button: that's the moment they grant
    the guilds.join permission this uses, and oauth_server.py keeps that
    token on file specifically so it can be reused here later. Anyone who
    never verified that way can't be added this way — Discord doesn't
    allow ANY app to add a user to a server without a token like that, so
    they'll show up as skipped and still need a regular invite. Usage:
      ,pullback <id/mention> <id/mention> ...
    """
    guild = ctx.guild
    if not targets:
        await ctx.send("❌ Usage: `,pullback <id/mention> <id/mention> ...`", delete_after=10)
        return

    ids, invalid = _parse_user_id_tokens(targets)

    if not ids:
        await ctx.send("❌ No valid user IDs/mentions to pull back.", delete_after=10)
        return
    if len(ids) > 1000:
        await ctx.send(f"❌ That's {len(ids)} users — narrow it down or run this in batches.", delete_after=12)
        return
    if not BILLING_CONFIGURED:
        await ctx.send("❌ This needs the OAuth service configured (BILLING_API_URL + INTERNAL_API_SECRET) — ask whoever runs this bot to finish setting that up.", delete_after=12)
        return

    warn_lines = []
    if invalid:
        warn_lines.append(f"⚠️ Skipped {len(invalid)} unrecognized token(s).")

    preview = ", ".join(f"`{uid}`" for uid in ids[:20])
    if len(ids) > 20:
        preview += f", … +{len(ids) - 20} more"

    embed = discord.Embed(
        title="⚠️ Confirm Pull-Back",
        description=(
            f"You're about to try re-adding **{len(ids)}** user(s) directly into **{guild.name}** — "
            "no invite link, no DM. Only works for anyone who's previously verified via the OAuth "
            "button; everyone else will be skipped.\n\n" + preview
            + ("\n\n" + "\n".join(warn_lines) if warn_lines else "")
        ),
        color=discord.Color.blurple(),
    )
    view = _MassActionConfirmView(ctx.author.id)
    await ctx.send(embed=embed, view=view)
    await view.wait()
    if not view.confirmed:
        return

    data, error = await _billing_api_post("/internal/pullback", {
        "guild_id": ctx.guild.id, "user_ids": ids,
    })
    if error:
        await ctx.send(f"❌ Couldn't pull anyone back: {error}", delete_after=12)
        return

    rejoined = data.get("rejoined", [])
    no_token = data.get("no_token", [])
    failed = data.get("failed", [])

    embed2 = discord.Embed(
        title="📥 Pull-Back Complete",
        description=f"Re-added **{len(rejoined)}**/{len(ids)} user(s) directly to **{guild.name}**.",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    if no_token:
        embed2.add_field(name="🚫 Never Verified", value=f"{len(no_token)} (no OAuth token on file — needs a regular invite instead)", inline=False)
    if failed:
        embed2.add_field(name="⚠️ Failed", value=str(len(failed)), inline=True)
    await ctx.send(embed=embed2)
    await log(guild, "mod", "Pull-Back Executed", None, discord.Color.blurple(),
              fields=[
                  ("🛡 Moderator",     f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("📥 Re-added",      f"{len(rejoined)}/{len(ids)}",                True),
                  ("🚫 Never Verified", str(len(no_token)),                          True),
              ],
              actor=ctx.author)


@bot.command()
@_permitted_check(ban_members=True)
async def hardbans(ctx):
    """List all hard-banned users in this server. Usage: ,hardbans"""
    hb = HARD_BANNED.get(ctx.guild.id, {})
    if not hb:
        await ctx.send("📭 No hard-bans active in this server.")
        return

    embed = discord.Embed(
        title="🔴 Hard-Banned Users",
        description=f"**{len(hb)}** hard-ban(s) active in **{ctx.guild.name}**",
        color=discord.Color.dark_red(),
        timestamp=discord.utils.utcnow()
    )
    lines = []
    for uid, ban_reason in list(hb.items())[:20]:
        lines.append(f"• `{uid}` — {ban_reason[:60]}")
    embed.add_field(name="Users", value="\n".join(lines) or "None", inline=False)
    footer = f"Showing 20 of {len(hb)} • " if len(hb) > 20 else ""
    embed.set_footer(text=f"{footer}TrapAI Hard-Ban System • {ctx.guild.name}")
    await ctx.send(embed=embed)


# ============================================================
# SERVER INVITE COMMANDS
# ============================================================

@bot.command()
@_permitted_check(manage_guild=True)
async def setinvite(ctx, invite_link: str = None):
    """
    Shows the server's invite link, used in DM notifications.
    It's now derived from the vanity code (native or ,setvanitycode
    override), so it can't be set here directly — see ,setvanitycode.
    Usage: ,setinvite
    """
    current = _resolve_invite_link(ctx.guild)
    embed = discord.Embed(
        title="🔗 Server Invite Link",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    if current:
        embed.add_field(name="Current Link", value=current, inline=False)
        embed.description = (
            "This link is derived from the server's **vanity code** and is included in all moderation DMs.\n"
            "Use `,setvanitycode` to change it."
        )
    else:
        embed.description = (
            "No vanity code set or detected, so there's no invite link yet.\n"
            "Use `,setvanitycode yourcode` to set one."
        )
    embed.set_footer(text=f"TrapAI • {ctx.guild.name}")
    await ctx.send(embed=embed)


@bot.command()
@_permitted_check(manage_messages=True)
async def sendinvite(ctx, user_target: str = None, *, message: str = None):
    """
    Send the server invite + optional personal message to any user's DMs.
    Works with members IN the server AND users outside it.

    Usage:
      ,sendinvite @user                    — mention (in or out of server)
      ,sendinvite 123456789012345678        — raw user ID (anyone on Discord)
      ,sendinvite @user Come back!          — with a custom message
      ,sendinvite 123456789012345678 Hey!   — ID + custom message
    """
    if user_target is None:
        await ctx.send(
            "❌ Usage: `,sendinvite <@user|user_id> [message]`\n"
            "Works with members already in the server **and** users outside it.",
            delete_after=10
        )
        return

    # ── Resolve the user (member mention, user mention, or raw ID) ──
    # Strip <@>, <@!> mention formatting to get the raw ID
    raw = user_target.strip().lstrip("<@!").rstrip(">")
    try:
        uid = int(raw)
    except ValueError:
        await ctx.send("❌ Invalid user. Use a `@mention` or a numeric user ID.", delete_after=8)
        return

    # Try guild member first (cheaper), fall back to global fetch
    user = ctx.guild.get_member(uid)
    if user is None:
        try:
            user = await bot.fetch_user(uid)
        except discord.NotFound:
            await ctx.send(f"❌ No Discord user found with ID `{uid}`.", delete_after=8)
            return
        except discord.HTTPException:
            await ctx.send("❌ Failed to look up that user. Try again later.", delete_after=8)
            return

    invite = _resolve_invite_link(ctx.guild)

    embed = discord.Embed(
        title=f"📨 You've been invited to **{ctx.guild.name}**",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    if ctx.guild.icon:
        embed.set_thumbnail(url=ctx.guild.icon.url)

    embed.add_field(name="📣 Invited by", value=str(ctx.author),  inline=True)
    embed.add_field(name="🏠 Server",     value=ctx.guild.name,   inline=True)

    if message:
        embed.add_field(name="💬 Message", value=message, inline=False)

    if invite:
        embed.add_field(name="🔗 Join Link", value=invite, inline=False)
    else:
        embed.add_field(name="⚠️ No invite set", value="Ask a staff member to set the server's vanity code with `,setvanitycode`", inline=False)

    embed.set_footer(text=f"TrapAI • {ctx.guild.name}")

    try:
        await user.send(embed=embed)
        confirm = discord.Embed(
            title="✅ Invite Sent",
            description=f"Successfully sent the server invite to **{user}** (`{user.id}`).",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        confirm.add_field(name="📨 Recipient", value=f"{user.mention} (`{user.id}`)", inline=True)
        in_server = ctx.guild.get_member(user.id) is not None
        confirm.add_field(name="🏠 In Server", value="Yes" if in_server else "No (external user)", inline=True)
        if message:
            confirm.add_field(name="💬 Message Included", value=message[:100], inline=False)
        confirm.set_footer(text=f"Sent by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=confirm)
    except discord.Forbidden:
        # DMs closed — a bot can never force a DM through no matter what,
        # that's a hard Discord platform limit. The only real fallback is
        # posting it somewhere they'll actually see it: a channel you both
        # share. That only works if they're a member of THIS server; if
        # they're a total stranger to the bot, there's genuinely no other
        # channel to reach them through.
        member_obj = ctx.guild.get_member(user.id)
        if member_obj:
            try:
                await ctx.send(content=member_obj.mention, embed=embed)
                await ctx.send(embed=discord.Embed(
                    description=f"📨 **{user}**'s DMs are closed, so the invite was posted here instead.",
                    color=discord.Color.orange(),
                    timestamp=discord.utils.utcnow()
                ))
            except discord.HTTPException:
                await ctx.send(f"❌ Could not DM **{user}** (DMs closed) and couldn't post a fallback message here either.", delete_after=8)
        else:
            await ctx.send(
                f"❌ Could not DM **{user}** (`{user.id}`) — their DMs are closed, and they're not in this "
                "server, so there's no channel the bot can reach them through instead.",
                delete_after=10
            )
    except discord.HTTPException:
        await ctx.send(f"❌ Failed to send invite to **{user}**.", delete_after=8)


# ============================================================
# ANNOUNCE COMMAND  — rich server announcements
# ============================================================

@bot.command(aliases=["ann"])
@_permitted_check(manage_messages=True)
async def announce(ctx, channel: discord.TextChannel = None, *, text: str = None):
    """
    Send a polished announcement embed to any channel.

    Pings @everyone by default — use --ping none for a quiet announcement.

    Usage:
      ,announce <message>
      ,announce #channel <message>
      ,announce #channel --title My Title | --color red | --image <url> | --ping here/none | <message>

    Flags (all optional, any order, separated by |):
      --title  <text>              — embed title
      --color  <name/hex>          — sidebar color  (red green blue gold purple teal orange pink)
      --image  <url>               — large image at the bottom of the embed
      --ping   everyone/here/none  — who to ping (defaults to everyone; "none" for a silent post)

    Examples:
      ,announce Server is going online!
      ,announce #announcements Big update dropping tonight!
      ,announce #general --title 🔥 Event --color gold --ping here | Giveaway starts at 9 PM!
      ,announce #general --ping none | Small heads up, no need to ping anyone
    """
    # If no channel was mentioned, default to current channel
    if channel is None:
        channel = ctx.channel

    if not text:
        await ctx.send(
            "❌ You need to include a message.\n"
            "Usage: `,announce [#channel] [--title x | --color x | --image x | --ping x |] message`",
            delete_after=10
        )
        return

    # ── Parse optional flags from the text ─────────────────────
    import re as _re

    title   = None
    color   = discord.Color.from_rgb(88, 101, 242)  # default: blurple
    image   = None
    ping    = "@everyone"  # pings everyone by default; --ping none to opt out
    message = text

    # Split on pipe or newline so flags can be mixed in naturally
    # e.g.  --title Big News | --color red | The message here
    segments = [s.strip() for s in _re.split(r"\s*\|\s*", text)]
    body_parts = []
    for seg in segments:
        lseg = seg.lower()
        if lseg.startswith("--title "):
            title = seg[8:].strip()
        elif lseg.startswith("--color "):
            raw_color = seg[8:].strip().lower()
            _color_map = {
                "red":    discord.Color.red(),
                "green":  discord.Color.green(),
                "blue":   discord.Color.blue(),
                "gold":   discord.Color.purple(),
                "purple": discord.Color.purple(),
                "teal":   discord.Color.teal(),
                "orange": discord.Color.orange(),
                "pink":   discord.Color.from_rgb(255, 105, 180),
                "white":  discord.Color.from_rgb(255, 255, 255),
                "black":  discord.Color.from_rgb(0, 0, 0),
                "yellow": discord.Color.yellow(),
            }
            if raw_color in _color_map:
                color = _color_map[raw_color]
            else:
                # Try hex  e.g. #ff5733 or ff5733
                try:
                    hex_val = raw_color.lstrip("#")
                    color = discord.Color(int(hex_val, 16))
                except ValueError:
                    pass  # invalid color — keep default
        elif lseg.startswith("--image "):
            image = seg[8:].strip()
        elif lseg.startswith("--ping "):
            raw_ping = seg[7:].strip().lower().replace("@", "")
            if raw_ping in ("everyone", "here"):
                ping = f"@{raw_ping}"
            elif raw_ping in ("none", "off", "silent"):
                ping = None
        else:
            body_parts.append(seg)

    message = "\n".join(body_parts).strip()

    if not message:
        await ctx.send("❌ You didn't include a message body (the actual text of the announcement).", delete_after=10)
        return

    # ── Build the embed ─────────────────────────────────────────
    now = discord.utils.utcnow()
    embed = discord.Embed(
        title=title,
        description=message,
        color=color,
        timestamp=now
    )
    embed.set_author(
        name=f"📢  Announcement  ·  {ctx.guild.name}",
        icon_url=ctx.guild.icon.url if ctx.guild.icon else None
    )
    embed.add_field(
        name="👮 Posted by",
        value=f"{ctx.author.mention}",
        inline=True
    )
    if channel != ctx.channel:
        embed.add_field(name="📍 Channel", value=channel.mention, inline=True)
    embed.add_field(
        name="🕐 Time",
        value=discord.utils.format_dt(now, "F"),
        inline=True
    )
    if image:
        embed.set_image(url=image)
    if ctx.guild.icon:
        embed.set_thumbnail(url=ctx.guild.icon.url)
    embed.set_footer(
        text=f"{ctx.guild.name}  ·  Announcement",
        icon_url=ctx.guild.icon.url if ctx.guild.icon else None
    )

    # ── Send ────────────────────────────────────────────────────
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass

    try:
        if ping:
            await channel.send(ping, embed=embed)
        else:
            await channel.send(embed=embed)
    except discord.Forbidden:
        await ctx.send(f"❌ I don't have permission to send messages in {channel.mention}.", delete_after=8)
        return

    # Confirm to the invoker if they sent it to a different channel
    if channel != ctx.channel:
        confirm = discord.Embed(
            description=f"✅ Announcement sent to {channel.mention}.",
            color=discord.Color.green()
        )
        await ctx.send(embed=confirm, delete_after=6)

    # Log it
    await log(
        ctx.guild, "mod", "Announcement Posted", None,
        color,
        fields=[
            ("👮 Posted By", f"{ctx.author.mention} (`{ctx.author.id}`)", True),
            ("📍 Channel",   channel.mention,                              True),
            ("📣 Ping",      ping or "None",                               True),
            ("🏷️ Title",     title or "None",                              True),
        ],
        actor=ctx.author
    )


# ============================================================
# QUOTE COMMAND  (bleed-style message card)
# ============================================================

def _parse_msg_link(raw: str):
    """
    Parse a Discord message link  →  (channel_id, message_id) or None.
    Handles both: https://discord.com/channels/gid/cid/mid
                  https://discord.com/channels/@me/cid/mid
    """
    import re
    m = re.search(r"channels/(?:\d+|@me)/(\d+)/(\d+)", raw)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


@bot.command()
async def quote(ctx, *, target: str = None):
    """
    Quote a message as a beautiful embed card.

    Ways to use it:
      • Reply to any message          →  ,quote
      • Paste a message link          →  ,quote https://discord.com/channels/…
      • Provide a message ID          →  ,quote 1234567890123456789
      • Custom text attributed to someone → ,quote "i love dogs" @user
        (wrap text in quotes, then mention the person)
    """
    import re as _re

    # ── Mode A: custom text + optional @user attribution ─────
    # Detect:  ,quote "some text" optional_@mention
    custom_match = _re.match(r'^["\u201c](.+?)["\u201d]\s*(.*)?$', (target or "").strip(), _re.DOTALL)
    if target and custom_match:
        custom_text = custom_match.group(1).strip()
        mention_str = (custom_match.group(2) or "").strip()

        # Try to resolve the mentioned member from the message mentions or raw text
        attributed: discord.Member | discord.User | None = None
        if ctx.message.mentions:
            attributed = ctx.message.mentions[0]
        elif mention_str:
            # Bare user ID fallback
            try:
                uid = int(_re.sub(r"[<@!>]", "", mention_str))
                attributed = ctx.guild.get_member(uid) or bot.get_user(uid)
            except ValueError:
                pass

        # Pick color from attributed member's top role, else gold
        color = discord.Color.purple()
        if isinstance(attributed, discord.Member):
            rc = attributed.color
            if rc != discord.Color.default():
                color = rc

        if len(custom_text) > 1000:
            custom_text = custom_text[:997] + "…"

        embed = discord.Embed(
            description=f"\u201c{custom_text}\u201d",
            color=color,
            timestamp=discord.utils.utcnow(),
        )

        if attributed:
            author_name = attributed.display_name
            if hasattr(attributed, "bot") and attributed.bot:
                author_name += " 🤖"
            embed.set_author(name=author_name, icon_url=attributed.display_avatar.url)
            embed.set_thumbnail(url=attributed.display_avatar.url)
        else:
            embed.set_author(name="💬 Quote")

        embed.set_footer(
            text=f"Quoted by {ctx.author.display_name}  •  {ctx.guild.name}",
            icon_url=ctx.author.display_avatar.url
        )

        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass

        await ctx.send(embed=embed)
        return

    # ── Mode B: quote an existing Discord message ─────────────
    fetched_msg: discord.Message = None

    # 1. Reply reference
    if ctx.message.reference and ctx.message.reference.resolved:
        ref = ctx.message.reference.resolved
        if isinstance(ref, discord.Message):
            fetched_msg = ref

    # 2. Message link
    if fetched_msg is None and target:
        parsed = _parse_msg_link(target)
        if parsed:
            cid, mid = parsed
            try:
                ch = bot.get_channel(cid) or await bot.fetch_channel(cid)
                fetched_msg = await ch.fetch_message(mid)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                await ctx.send("❌ Couldn't fetch that message — check the link or my permissions.", delete_after=8)
                return

    # 3. Bare message ID (current channel)
    if fetched_msg is None and target:
        try:
            mid = int(target.strip())
            fetched_msg = await ctx.channel.fetch_message(mid)
        except (ValueError, discord.NotFound, discord.HTTPException):
            pass

    if fetched_msg is None:
        await ctx.send(
            "❌ Nothing to quote.\n\n"
            "**How to use `,quote`:**\n"
            "• Reply to a message and type `,quote`\n"
            "• `,quote <message link>`\n"
            "• `,quote <message id>`\n"
            "• `,quote \"your text here\" @user` — custom attributed quote",
            delete_after=12
        )
        return

    # ── Build the quote card ──────────────────────────────────
    author      = fetched_msg.author
    content     = fetched_msg.content or ""
    jump_url    = fetched_msg.jump_url
    sent_at     = fetched_msg.created_at
    channel_ref = fetched_msg.channel

    # Color: use the author's top role color, fallback to a neutral dark
    color = discord.Color.from_rgb(30, 30, 35)
    if isinstance(author, discord.Member):
        rc = author.color
        if rc != discord.Color.default():
            color = rc

    # Truncate very long messages
    if len(content) > 1000:
        content = content[:997] + "…"

    embed = discord.Embed(
        description=f"\u201c{content}\u201d" if content else "*— no text content —*",
        color=color,
        timestamp=sent_at,
    )

    # Author row: avatar + name + bot badge
    author_label = str(author)
    if author.bot:
        author_label += " 🤖"
    embed.set_author(name=author_label, icon_url=author.display_avatar.url)
    embed.set_thumbnail(url=author.display_avatar.url)

    # Inline fields: channel, sent time, jump link
    embed.add_field(
        name="📍 Channel",
        value=channel_ref.mention if hasattr(channel_ref, "mention") else f"#{channel_ref.name}",
        inline=True
    )
    embed.add_field(
        name="🕐 Sent",
        value=f"{discord.utils.format_dt(sent_at, 'F')}\n{discord.utils.format_dt(sent_at, 'R')}",
        inline=True
    )
    embed.add_field(
        name="🔗 Jump",
        value=f"[View original]({jump_url})",
        inline=True
    )

    # If the original message had an image attachment, show it
    image_attached = False
    for att in fetched_msg.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            embed.set_image(url=att.url)
            image_attached = True
            break

    # If the original had an embed with an image, pull it
    if not image_attached and fetched_msg.embeds:
        for e in fetched_msg.embeds:
            if e.image and e.image.url:
                embed.set_image(url=e.image.url)
                break

    # Quoted-by footer
    embed.set_footer(
        text=f"Quoted by {ctx.author.display_name}  •  {ctx.guild.name}",
        icon_url=ctx.author.display_avatar.url
    )

    # Delete the invoking command message for a clean look
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass

    await ctx.send(embed=embed)

# ============================================================
# GAMES & ECONOMY SYSTEM
# ============================================================
import random

# ECONOMY[guild_id][user_id] = {"wallet": int, "bank": int}
ECONOMY: dict[int, dict[int, dict]] = _load_depth(_load_data("economy", {}), 2)

# COOLDOWNS[guild_id][user_id][action] = timestamp
COOLDOWNS: dict[int, dict[int, dict]] = _load_depth(_load_data("cooldowns", {}), 2)

# GAMBLE_WINS[guild_id][user_id] = net_winnings (for leaderboard)
GAMBLE_WINS: dict[int, dict[int, int]] = _load_depth(_load_data("gamble_wins", {}), 2)

# ROLE_SHOP[guild_id][role_id] = price_cents (real-money price via Stripe,
# in US cents) — a role for sale via ,buyrole, managed with ,setroleshop.
# Purchases are fulfilled by oauth_server.py's Stripe checkout + webhook
# (see that file's BILLING section) — it grants the role directly over the
# Discord REST API once payment confirms, so this never touches the
# economy wallet. Purely cosmetic/perk roles are the intended use case;
# nothing here grants moderation power.
ROLE_SHOP: dict[int, dict[int, int]] = _load_depth(_load_data("role_shop", {}), 2)

# VC_SHOP[guild_id] = price_cents — the price of a custom-named "FG VC"
# (a permanent, server-visible voice channel named whatever the buyer
# wants) via ,buyfgvc, managed with ,setvcshop. Same real-money-via-Stripe
# pattern as ROLE_SHOP; fulfilled by oauth_server.py creating the channel
# directly over the Discord REST API once payment confirms.
VC_SHOP: dict[int, int] = _load_data("vc_shop", {})
VC_SHOP = {int(k): v for k, v in VC_SHOP.items()}


def _save_role_shop():
    _save_data("role_shop", _dump_depth(ROLE_SHOP, 2))

def _save_vc_shop():
    _save_data("vc_shop", {str(k): v for k, v in VC_SHOP.items()})

def _eco(guild_id: int, user_id: int) -> dict:
    return ECONOMY.setdefault(guild_id, {}).setdefault(user_id, {"wallet": 0, "bank": 0})

def _add_wallet(guild_id, user_id, amount):
    _eco(guild_id, user_id)["wallet"] += amount

def _fmt_money(amount: int) -> str:
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,}"

# LIFETIME_EARNED[guild_id][user_id] = total ever earned via work/daily/
# weekly/trivia-style rewards — monotonically increasing (gambling wins/
# losses and ,give never touch it), used purely to gate job unlocks in
# JOBS below so a lucky slots win can't instantly "hire" someone as CEO
# and a robbery/gambling loss can't un-qualify them from a job already earned.
LIFETIME_EARNED: dict[int, dict[int, int]] = _load_depth(_load_data("lifetime_earned", {}), 2)

# CURRENT_JOB[guild_id][user_id] = job name string, set via ,setjob.
CURRENT_JOB: dict[int, dict[int, str]] = _load_depth(_load_data("current_job", {}), 2)


def _save_lifetime_earned():
    _save_data("lifetime_earned", _dump_depth(LIFETIME_EARNED, 2))

def _save_current_job():
    _save_data("current_job", _dump_depth(CURRENT_JOB, 2))

def _add_earned(guild_id, user_id, amount):
    """Wallet income that also counts toward job-unlock progress."""
    _add_wallet(guild_id, user_id, amount)
    guild_earned = LIFETIME_EARNED.setdefault(guild_id, {})
    guild_earned[user_id] = guild_earned.get(user_id, 0) + amount
    _save_lifetime_earned()

def _lifetime_earned(guild_id, user_id) -> int:
    return LIFETIME_EARNED.get(guild_id, {}).get(user_id, 0)


# JOBS — ordered low to high pay, each locked behind a lifetime-earned
# threshold so progression feels like an actual career ladder rather than
# an instant pick-any-job list.
JOBS = [
    {"name": "Cashier",           "emoji": "🛒", "pay": (20, 60),     "unlock": 0},
    {"name": "Delivery Driver",   "emoji": "🚴", "pay": (40, 100),    "unlock": 500},
    {"name": "Barista",           "emoji": "☕", "pay": (50, 120),    "unlock": 1_000},
    {"name": "Uber Driver",       "emoji": "🚗", "pay": (70, 160),    "unlock": 2_500},
    {"name": "Mechanic",          "emoji": "🔧", "pay": (100, 220),   "unlock": 5_000},
    {"name": "Chef",              "emoji": "🍳", "pay": (130, 280),   "unlock": 10_000},
    {"name": "Nurse",             "emoji": "🩺", "pay": (180, 380),   "unlock": 20_000},
    {"name": "Software Engineer", "emoji": "💻", "pay": (250, 550),   "unlock": 40_000},
    {"name": "Lawyer",            "emoji": "⚖️", "pay": (350, 750),   "unlock": 75_000},
    {"name": "Doctor",            "emoji": "🏥", "pay": (450, 950),   "unlock": 150_000},
    {"name": "CEO",               "emoji": "💼", "pay": (700, 1_500), "unlock": 300_000},
]
JOBS_BY_NAME = {j["name"].lower(): j for j in JOBS}

JOB_FLAVOR = {
    "Cashier":           ["rang up groceries", "handled the register", "restocked shelves"],
    "Delivery Driver":   ["dropped off packages", "delivered a rush order", "made a dozen stops"],
    "Barista":           ["pulled espresso shots", "latte-art'd a cappuccino", "survived the morning rush"],
    "Uber Driver":       ["drove across town", "gave someone a 5-star ride", "picked up a late-night fare"],
    "Mechanic":          ["fixed a transmission", "changed some brake pads", "diagnosed an engine problem"],
    "Chef":              ["plated a tasting menu", "ran the dinner rush", "perfected a new recipe"],
    "Nurse":             ["worked a double shift", "took vitals all day", "helped out in the ER"],
    "Software Engineer": ["shipped a bug fix", "closed out a sprint", "refactored some legacy code"],
    "Lawyer":            ["won a case", "drafted a contract", "billed a lot of hours"],
    "Doctor":            ["ran the clinic", "performed a checkup", "consulted on a diagnosis"],
    "CEO":               ["closed a merger", "gave a keynote", "signed off on quarterly earnings"],
}

def _current_job(guild_id, user_id):
    name = CURRENT_JOB.get(guild_id, {}).get(user_id)
    return JOBS_BY_NAME.get(name.lower()) if name else None

def _unlocked_jobs(guild_id, user_id):
    total = _lifetime_earned(guild_id, user_id)
    return [j for j in JOBS if total >= j["unlock"]]

def _next_locked_job(guild_id, user_id):
    """Returns (job, amount_still_needed) for the next job up the ladder
    that isn't unlocked yet, or (None, 0) if every job is already unlocked."""
    total = _lifetime_earned(guild_id, user_id)
    for j in JOBS:
        if total < j["unlock"]:
            return j, j["unlock"] - total
    return None, 0

def _eco_embed(member, guild_id):
    data = _eco(guild_id, member.id)
    job = _current_job(guild_id, member.id)
    embed = discord.Embed(
        title=f"💰 {member.display_name}'s Balance",
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="👛 Wallet", value=f"**{_fmt_money(data['wallet'])}**", inline=True)
    embed.add_field(name="🏦 Bank",   value=f"**{_fmt_money(data['bank'])}**",   inline=True)
    embed.add_field(name="💎 Total",  value=f"**{_fmt_money(data['wallet']+data['bank'])}**", inline=True)
    embed.add_field(name="💼 Job", value=f"{job['emoji']} {job['name']}" if job else "*Unemployed — see `,jobs`*", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text="TrapAI Economy")
    return embed

def _on_cooldown(guild_id, user_id, action, seconds) -> int:
    """Returns remaining seconds if on cooldown, else 0 and records timestamp."""
    now = time.time()
    cd = COOLDOWNS.setdefault(guild_id, {}).setdefault(user_id, {})
    last = cd.get(action, 0)
    remaining = int(last + seconds - now)
    if remaining > 0:
        return remaining
    cd[action] = now
    return 0


# ── Balance ──────────────────────────────────────────────────
@bot.command(aliases=["bal", "wallet", "money"])
async def balance(ctx, member: discord.Member = None):
    member = member or ctx.author
    await ctx.send(embed=_eco_embed(member, ctx.guild.id))


# ── Jobs ─────────────────────────────────────────────────────
@bot.command(name="jobs")
async def jobs_cmd(ctx):
    """View every job, what it pays, and what you need to unlock it."""
    total = _lifetime_earned(ctx.guild.id, ctx.author.id)
    current = _current_job(ctx.guild.id, ctx.author.id)
    lines = []
    for j in JOBS:
        unlocked = total >= j["unlock"]
        lo, hi = j["pay"]
        tag = " **(current)**" if current and current["name"] == j["name"] else ""
        if unlocked:
            lines.append(f"✅ {j['emoji']} **{j['name']}**{tag} — {_fmt_money(lo)}–{_fmt_money(hi)} per shift")
        else:
            lines.append(f"🔒 {j['emoji']} {j['name']} — {_fmt_money(lo)}–{_fmt_money(hi)} • unlocks at **{_fmt_money(j['unlock'])}** lifetime earned")
    embed = discord.Embed(
        title="💼 Career Ladder",
        description="\n".join(lines),
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="📈 Your Progress", value=f"**{_fmt_money(total)}** lifetime earned", inline=False)
    embed.set_footer(text="Pick a job with ,setjob <name> • TrapAI Economy")
    await ctx.send(embed=embed)


@bot.command(name="setjob", aliases=["job"])
async def setjob(ctx, *, name: str = None):
    """Set your current job from the ones you've unlocked. Usage: ,setjob <name> (see ,jobs)"""
    if not name:
        current = _current_job(ctx.guild.id, ctx.author.id)
        await ctx.send(
            f"💼 Current job: {current['emoji']} **{current['name']}**" if current else
            "💼 You're currently **unemployed**. See `,jobs` and run `,setjob <name>`.",
            delete_after=10
        )
        return
    job = JOBS_BY_NAME.get(name.strip().lower())
    if not job:
        await ctx.send("❌ That's not a real job. See `,jobs` for the full list.", delete_after=8)
        return
    total = _lifetime_earned(ctx.guild.id, ctx.author.id)
    if total < job["unlock"]:
        await ctx.send(
            f"🔒 You haven't unlocked **{job['name']}** yet — needs **{_fmt_money(job['unlock'])}** lifetime "
            f"earned, you have **{_fmt_money(total)}**.",
            delete_after=10
        )
        return
    CURRENT_JOB.setdefault(ctx.guild.id, {})[ctx.author.id] = job["name"]
    _save_current_job()
    await ctx.send(f"✅ You're now working as a {job['emoji']} **{job['name']}**! Use `,work` to start earning.")


# ── Work ─────────────────────────────────────────────────────
@bot.command()
async def work(ctx):
    bonus = _has_staff_award_role(ctx.author)
    cooldown_secs = STAFF_AWARD_WORK_COOLDOWN if bonus else 20
    cd = _on_cooldown(ctx.guild.id, ctx.author.id, "work", cooldown_secs)
    if cd:
        await ctx.send(f"⏳ You're tired. Come back in **{cd}s**.", delete_after=8)
        return
    job = _current_job(ctx.guild.id, ctx.author.id)
    if job:
        earned = random.randint(*job["pay"])
        action = random.choice(JOB_FLAVOR[job["name"]])
        title = f"{job['emoji']} {job['name']} Shift Complete"
        desc = f"You **{action}** and earned **{_fmt_money(earned)}**!"
    else:
        earned = random.randint(10, 40)
        gigs = ["walked a dog", "sold some lemonade", "did a random odd job", "ran an errand for a neighbor"]
        title = "💼 Work Complete"
        desc = f"You **{random.choice(gigs)}** and earned **{_fmt_money(earned)}**.\n*Get a real job with `,jobs` to earn a lot more.*"
    if bonus:
        earned = int(earned * STAFF_AWARD_ECONOMY_MULTIPLIER)
    _add_earned(ctx.guild.id, ctx.author.id, earned)
    embed = discord.Embed(title=title, description=desc, color=discord.Color.green(), timestamp=discord.utils.utcnow())
    if bonus:
        embed.add_field(name="🎁 Staff Award Bonus", value=f"{STAFF_AWARD_ECONOMY_MULTIPLIER}x earnings applied!", inline=False)
    next_job, remaining = _next_locked_job(ctx.guild.id, ctx.author.id)
    if next_job:
        embed.add_field(
            name="📈 Next Job",
            value=f"**{_fmt_money(remaining)}** more lifetime earned to unlock {next_job['emoji']} **{next_job['name']}**",
            inline=False
        )
    else:
        embed.add_field(name="🏆 Career Maxed", value="You've unlocked every job on the ladder!", inline=False)
    embed.set_footer(text=f"Come back in {cooldown_secs} seconds • TrapAI Economy")
    await ctx.send(embed=embed)


# ── Daily ────────────────────────────────────────────────────
@bot.command()
async def daily(ctx):
    cd = _on_cooldown(ctx.guild.id, ctx.author.id, "daily", 86400)
    if cd:
        h, s = divmod(cd, 3600)
        m, s = divmod(s, 60)
        await ctx.send(f"⏳ Daily already claimed. Come back in **{h}h {m}m**.", delete_after=8)
        return
    earned = random.randint(200, 500)
    bonus = _has_staff_award_role(ctx.author)
    if bonus:
        earned = int(earned * STAFF_AWARD_ECONOMY_MULTIPLIER)
    _add_earned(ctx.guild.id, ctx.author.id, earned)
    embed = discord.Embed(
        title="📅 Daily Reward",
        description=f"You claimed your daily reward of **{_fmt_money(earned)}**! Come back tomorrow.",
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    if bonus:
        embed.add_field(name="🎁 Staff Award Bonus", value=f"{STAFF_AWARD_ECONOMY_MULTIPLIER}x earnings applied!", inline=False)
    embed.set_footer(text="Resets every 24 hours • TrapAI Economy")
    await ctx.send(embed=embed)


# ── Weekly ───────────────────────────────────────────────────
@bot.command()
async def weekly(ctx):
    cd = _on_cooldown(ctx.guild.id, ctx.author.id, "weekly", 604800)
    if cd:
        d, s = divmod(cd, 86400)
        h, s = divmod(s, 3600)
        await ctx.send(f"⏳ Weekly already claimed. Come back in **{d}d {h}h**.", delete_after=8)
        return
    earned = random.randint(1000, 2500)
    bonus = _has_staff_award_role(ctx.author)
    if bonus:
        earned = int(earned * STAFF_AWARD_ECONOMY_MULTIPLIER)
    _add_earned(ctx.guild.id, ctx.author.id, earned)
    embed = discord.Embed(
        title="📆 Weekly Reward",
        description=f"You claimed your weekly reward of **{_fmt_money(earned)}**! Come back next week.",
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    if bonus:
        embed.add_field(name="🎁 Staff Award Bonus", value=f"{STAFF_AWARD_ECONOMY_MULTIPLIER}x earnings applied!", inline=False)
    embed.set_footer(text="Resets every 7 days • TrapAI Economy")
    await ctx.send(embed=embed)


# ── Deposit ──────────────────────────────────────────────────
@bot.command(aliases=["dep"])
async def deposit(ctx, amount: str = None):
    data = _eco(ctx.guild.id, ctx.author.id)
    if amount is None:
        await ctx.send("❌ Usage: `,deposit <amount|all>`", delete_after=6)
        return
    if amount.lower() == "all":
        amt = data["wallet"]
    else:
        try:
            amt = int(amount)
        except ValueError:
            await ctx.send("❌ Amount must be a number or `all`.", delete_after=6)
            return
    if amt <= 0 or amt > data["wallet"]:
        await ctx.send(f"❌ You only have **{_fmt_money(data['wallet'])}** in your wallet.", delete_after=6)
        return
    data["wallet"] -= amt
    data["bank"]   += amt
    await ctx.send(f"🏦 Deposited **{_fmt_money(amt)}** into your bank. Bank: **{_fmt_money(data['bank'])}**")


# ── Withdraw ─────────────────────────────────────────────────
@bot.command(aliases=["with"])
async def withdraw(ctx, amount: str = None):
    data = _eco(ctx.guild.id, ctx.author.id)
    if amount is None:
        await ctx.send("❌ Usage: `,withdraw <amount|all>`", delete_after=6)
        return
    if amount.lower() == "all":
        amt = data["bank"]
    else:
        try:
            amt = int(amount)
        except ValueError:
            await ctx.send("❌ Amount must be a number or `all`.", delete_after=6)
            return
    if amt <= 0 or amt > data["bank"]:
        await ctx.send(f"❌ You only have **{_fmt_money(data['bank'])}** in your bank.", delete_after=6)
        return
    data["bank"]   -= amt
    data["wallet"] += amt
    await ctx.send(f"👛 Withdrew **{_fmt_money(amt)}** to your wallet. Wallet: **{_fmt_money(data['wallet'])}**")


# ── Give ─────────────────────────────────────────────────────
@bot.command(aliases=["pay", "transfer"])
async def give(ctx, member: discord.Member = None, amount: int = None):
    if member is None or amount is None or amount <= 0:
        await ctx.send("❌ Usage: `,give @user <amount>`", delete_after=6)
        return
    if member == ctx.author:
        await ctx.send("❌ You can't give money to yourself.", delete_after=6)
        return
    data = _eco(ctx.guild.id, ctx.author.id)
    if amount > data["wallet"]:
        await ctx.send(f"❌ You only have **{_fmt_money(data['wallet'])}** in your wallet.", delete_after=6)
        return
    data["wallet"] -= amount
    _add_wallet(ctx.guild.id, member.id, amount)
    await ctx.send(f"✅ Sent **{_fmt_money(amount)}** to {member.mention}.")


# ── Rob ───────────────────────────────────────────────────────
@bot.command()
async def rob(ctx, member: discord.Member = None):
    if member is None:
        await ctx.send("❌ Usage: `,rob @user`", delete_after=6)
        return
    if member == ctx.author:
        await ctx.send("❌ You can't rob yourself.", delete_after=6)
        return
    cd = _on_cooldown(ctx.guild.id, ctx.author.id, "rob", 1800)
    if cd:
        m, s = divmod(cd, 60)
        await ctx.send(f"⏳ Lay low for **{m}m {s}s** before robbing again.", delete_after=8)
        return
    target = _eco(ctx.guild.id, member.id)
    robber = _eco(ctx.guild.id, ctx.author.id)
    if target["wallet"] < 50:
        await ctx.send(f"❌ {member.display_name} is broke — nothing to rob!", delete_after=6)
        return
    if random.random() < 0.45:  # 45% success
        stolen = random.randint(1, max(1, target["wallet"] // 3))
        target["wallet"] -= stolen
        robber["wallet"] += stolen
        await ctx.send(f"🦹 You successfully robbed **{_fmt_money(stolen)}** from {member.mention}!")
    else:
        fine = random.randint(50, 200)
        robber["wallet"] = max(0, robber["wallet"] - fine)
        await ctx.send(f"🚔 You got caught! You paid a **{_fmt_money(fine)}** fine.")


# ── Role Shop (real money, via Stripe) ──────────────────────
@bot.command()
async def shop(ctx):
    """View roles for sale — buy one with ,buyrole @role. Usage: ,shop"""
    listings = ROLE_SHOP.get(ctx.guild.id, {})
    lines = []
    for role_id, price_cents in sorted(listings.items(), key=lambda kv: kv[1]):
        role = ctx.guild.get_role(role_id)
        if role:
            lines.append(f"➡️ {role.mention} — **${price_cents / 100:,.2f}**")

    embed = discord.Embed(
        title="🛒 Role Shop",
        description=(
            "Buy a role with real money — secure checkout via Stripe, `,buyrole @role`.\n\n"
            + ("\n".join(lines) if lines else "*Nothing for sale yet.*")
        ),
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    if not lines:
        embed.add_field(name="ℹ️ For Staff", value="Add roles with `,setroleshop add @role <price>` (e.g. `,setroleshop add @role 4.99`).", inline=False)
    embed.set_footer(text=f"TrapAI Shop • {ctx.guild.name}")
    await ctx.send(embed=embed)


@bot.command()
async def buyrole(ctx, role: discord.Role = None):
    """
    Buy a role from ,shop with real money — DMs you a secure Stripe
    checkout link. The role is granted automatically within moments of
    payment confirming (you don't need to run any command afterward).
    Usage: ,buyrole @role
    """
    if role is None:
        await ctx.send("❌ Usage: `,buyrole @role` — see what's for sale with `,shop`.", delete_after=8)
        return
    price_cents = ROLE_SHOP.get(ctx.guild.id, {}).get(role.id)
    if price_cents is None:
        await ctx.send(f"❌ {role.mention} isn't for sale. See `,shop` for what's available.", delete_after=8)
        return
    if role in ctx.author.roles:
        await ctx.send(f"❌ You already have {role.mention}.", delete_after=6)
        return
    if role.managed:
        await ctx.send(f"❌ {role.mention} is a managed role and can't be granted manually — ask staff to remove it from `,shop`.", delete_after=8)
        return
    if role >= ctx.guild.me.top_role:
        await ctx.send(_role_forbidden_reason(ctx.guild))
        return
    if not BILLING_CONFIGURED:
        await ctx.send("❌ Billing isn't configured on this bot yet — ask whoever runs it to finish setting up Stripe.", delete_after=12)
        return

    data, error = await _billing_api_post("/internal/roleshop/checkout-link", {
        "guild_id": ctx.guild.id, "role_id": role.id, "role_name": role.name,
        "discord_user_id": ctx.author.id, "price_cents": price_cents,
    })
    if error:
        await ctx.send(f"❌ Couldn't start checkout: {error}", delete_after=12)
        return

    checkout_url = data["url"]
    embed = discord.Embed(
        title=f"🛒 Checkout — {role.name}",
        description=(
            f"Click below to buy {role.mention} for **${price_cents / 100:,.2f}** securely via Stripe.\n\n"
            f"[Complete Checkout]({checkout_url})\n\n"
            "The role is granted automatically the moment payment confirms."
        ),
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text="TrapAI never sees your card details — Stripe handles all payment info.")

    try:
        await ctx.author.send(embed=embed)
        await ctx.send(f"📨 {ctx.author.mention} Check your DMs for your secure checkout link.")
    except (discord.Forbidden, discord.HTTPException):
        await ctx.send(embed=embed)  # DMs closed — post it here instead

    await log(ctx.guild, "roles", "Role Shop Checkout Started", None, discord.Color.purple(),
              fields=[
                  ("👤 Member", f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("🏷️ Role",   f"{role.mention} (`{role.id}`)",             True),
                  ("💰 Price",  f"${price_cents / 100:,.2f}",                True),
              ],
              actor=ctx.author, target=ctx.author)


@bot.command()
@_permitted_check(administrator=True)
async def setroleshop(ctx, action: str = None, role: discord.Role = None, price: float = None):
    """
    Manage which roles are for sale in ,shop for real money (Stripe).
    Usage:
      ,setroleshop list                — view current listings
      ,setroleshop add @role <price>   — list a role for sale, e.g. `,setroleshop add @role 4.99`
      ,setroleshop remove @role        — take a role off sale
    """
    guild = ctx.guild
    listings = ROLE_SHOP.setdefault(guild.id, {})

    if action is None or action.lower() == "list":
        if not listings:
            await ctx.send("📭 No roles for sale yet. Use `,setroleshop add @role <price>`.")
            return
        lines = []
        for role_id, price_cents in sorted(listings.items(), key=lambda kv: kv[1]):
            r = guild.get_role(role_id)
            lines.append(f"➡️ {r.mention if r else f'*deleted role* (`{role_id}`)'} — **${price_cents / 100:,.2f}**")
        embed = discord.Embed(
            title="🛒 Role Shop Listings",
            description="\n".join(lines),
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"TrapAI • {guild.name}")
        await ctx.send(embed=embed)
        return

    if action.lower() == "add":
        if role is None or price is None or price < 0.50:
            await ctx.send("❌ Usage: `,setroleshop add @role <price>` — price must be at least `$0.50` (e.g. `,setroleshop add @role 4.99`).", delete_after=10)
            return
        if role.managed:
            await ctx.send(f"❌ {role.mention} is a managed role (Server Booster, bot, or integration role) and can never be granted manually — it can't be sold.", delete_after=8)
            return
        if role >= guild.me.top_role:
            await ctx.send(_role_forbidden_reason(guild))
            return
        if not BILLING_CONFIGURED:
            await ctx.send("❌ Billing isn't configured on this bot yet — ask whoever runs it to finish setting up Stripe before listing real-money roles.", delete_after=12)
            return
        listings[role.id] = round(price * 100)
        _save_role_shop()
        await ctx.send(f"✅ {role.mention} is now for sale for **${price:,.2f}**.")
    elif action.lower() == "remove":
        if role is None:
            await ctx.send("❌ Usage: `,setroleshop remove @role`", delete_after=8)
            return
        if role.id not in listings:
            await ctx.send(f"❌ {role.mention} isn't currently for sale.", delete_after=6)
            return
        listings.pop(role.id)
        _save_role_shop()
        await ctx.send(f"✅ {role.mention} removed from the shop.")
    else:
        await ctx.send("❌ Unknown action. Use `add`, `remove`, or `list`.", delete_after=8)


@bot.command()
@_permitted_check(administrator=True)
async def setvcshop(ctx, price: str = None):
    """
    Set the price of a "FG VC" — a permanent, custom-named voice channel
    members can buy for their friend group with real money (Stripe).
    Usage:
      ,setvcshop <price>   — e.g. `,setvcshop 4.99`
      ,setvcshop off       — take FG VCs off sale
      ,setvcshop           — show current price
    """
    guild = ctx.guild
    if price is None:
        current = VC_SHOP.get(guild.id)
        await ctx.send(f"💰 FG VC price: **${current / 100:,.2f}**" if current else "📭 FG VCs aren't for sale yet. Use `,setvcshop <price>`.")
        return
    if price.lower() == "off":
        VC_SHOP.pop(guild.id, None)
        _save_vc_shop()
        await ctx.send("✅ FG VCs are no longer for sale.")
        return
    try:
        amount = float(price)
    except ValueError:
        await ctx.send("❌ Usage: `,setvcshop <price>` (e.g. `4.99`) or `,setvcshop off`.", delete_after=8)
        return
    if amount < 0.50:
        await ctx.send("❌ Price must be at least `$0.50`.", delete_after=6)
        return
    if not BILLING_CONFIGURED:
        await ctx.send("❌ Billing isn't configured on this bot yet — ask whoever runs it to finish setting up Stripe before listing FG VCs.", delete_after=12)
        return
    VC_SHOP[guild.id] = round(amount * 100)
    _save_vc_shop()
    await ctx.send(f"✅ FG VCs are now for sale for **${amount:,.2f}**.")


@bot.command(aliases=["buyfg"])
async def buyfgvc(ctx, *, channel_name: str = None):
    """
    Buy a permanent, custom-named "FG VC" (voice channel) for your friend
    group with real money — DMs you a secure Stripe checkout link. The
    channel is created automatically within moments of payment confirming.
    Usage: ,buyfgvc <channel name>
    """
    price_cents = VC_SHOP.get(ctx.guild.id)
    if price_cents is None:
        await ctx.send("❌ FG VCs aren't for sale here. See `,storefront` for what's available.", delete_after=8)
        return
    if not channel_name or not channel_name.strip():
        await ctx.send("❌ Usage: `,buyfgvc <channel name>` — e.g. `,buyfgvc The Gang VC`", delete_after=8)
        return
    channel_name = channel_name.strip()[:100]
    if not BILLING_CONFIGURED:
        await ctx.send("❌ Billing isn't configured on this bot yet — ask whoever runs it to finish setting up Stripe.", delete_after=12)
        return

    data, error = await _billing_api_post("/internal/vcshop/checkout-link", {
        "guild_id": ctx.guild.id, "channel_name": channel_name,
        "discord_user_id": ctx.author.id, "price_cents": price_cents,
    })
    if error:
        await ctx.send(f"❌ Couldn't start checkout: {error}", delete_after=12)
        return

    checkout_url = data["url"]
    embed = discord.Embed(
        title=f'🎤 Checkout — "{channel_name}" FG VC',
        description=(
            f"Click below to buy **\"{channel_name}\"** for **${price_cents / 100:,.2f}** securely via Stripe.\n\n"
            f"[Complete Checkout]({checkout_url})\n\n"
            "The channel is created automatically the moment payment confirms."
        ),
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text="TrapAI never sees your card details — Stripe handles all payment info.")

    try:
        await ctx.author.send(embed=embed)
        await ctx.send(f"📨 {ctx.author.mention} Check your DMs for your secure checkout link.")
    except (discord.Forbidden, discord.HTTPException):
        await ctx.send(embed=embed)  # DMs closed — post it here instead

    await log(ctx.guild, "roles", "FG VC Checkout Started", None, discord.Color.purple(),
              fields=[
                  ("👤 Member", f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("🎤 Channel", f'"{channel_name}"', True),
                  ("💰 Price",  f"${price_cents / 100:,.2f}",                True),
              ],
              actor=ctx.author, target=ctx.author)


@bot.command(aliases=["store"])
async def storefront(ctx):
    """Everything purchasable in/around this server, in one place. Usage: ,storefront"""
    guild = ctx.guild
    embed = discord.Embed(
        title=f"🛒 {guild.name} Storefront",
        description="Everything you can buy here — all real-money purchases go through secure Stripe checkout.",
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )

    role_listings = ROLE_SHOP.get(guild.id, {})
    if role_listings:
        lines = []
        for role_id, price_cents in sorted(role_listings.items(), key=lambda kv: kv[1]):
            role = guild.get_role(role_id)
            if role:
                lines.append(f"➡️ {role.mention} — **${price_cents / 100:,.2f}**")
        embed.add_field(
            name="🏷️ Roles — `,buyrole @role`",
            value="\n".join(lines) if lines else "*Nothing for sale right now.*",
            inline=False
        )
    else:
        embed.add_field(name="🏷️ Roles", value="*Nothing for sale right now. Staff: `,setroleshop add @role <price>`*", inline=False)

    vc_price = VC_SHOP.get(guild.id)
    embed.add_field(
        name="🎤 FG VCs — `,buyfgvc <name>`",
        value=(f"A permanent, custom-named voice channel for your friend group — **${vc_price / 100:,.2f}**"
               if vc_price else "*Not for sale right now. Staff: `,setvcshop <price>`*"),
        inline=False
    )

    if BILLING_API_URL:
        embed.add_field(
            name="🤖 TrapAI For Your Own Server",
            value=f"Want this bot in your own server? **[Get TrapAI here]({BILLING_API_URL}/checkout)**",
            inline=False
        )

    embed.add_field(
        name="🎫 Need Something Else?",
        value="Open a ticket and staff will help you out — `,tickets` if a panel isn't already posted.",
        inline=False
    )

    embed.set_footer(text="TrapAI never sees your card details — Stripe handles all payment info.")
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    await ctx.send(embed=embed)


# ── Economy Leaderboard ──────────────────────────────────────
@bot.command(aliases=["lb", "rich"])
async def leaderboard(ctx):
    guild_data = ECONOMY.get(ctx.guild.id, {})
    if not guild_data:
        await ctx.send("No economy data yet. Start with `,work` or `,daily`!", delete_after=8)
        return
    sorted_users = sorted(guild_data.items(), key=lambda x: x[1]["wallet"] + x[1]["bank"], reverse=True)[:10]
    embed = discord.Embed(
        title=f"💎 Richest Members — {ctx.guild.name}",
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    lines = []
    for i, (uid, data) in enumerate(sorted_users):
        member = ctx.guild.get_member(uid)
        name = member.display_name if member else f"<@{uid}>"
        total = data["wallet"] + data["bank"]
        lines.append(f"{medals[i]} **{name}** — {_fmt_money(total)}")
    embed.description = "\n".join(lines) or "No data yet."
    embed.set_footer(text="TrapAI Economy")
    await ctx.send(embed=embed)


# ── Gambler leaderboard ──────────────────────────────────────
@bot.command()
async def gamblers(ctx):
    guild_data = GAMBLE_WINS.get(ctx.guild.id, {})
    if not guild_data:
        await ctx.send("No gambling data yet!", delete_after=6)
        return
    sorted_users = sorted(guild_data.items(), key=lambda x: x[1], reverse=True)[:10]
    embed = discord.Embed(
        title=f"🎰 Top Gamblers — {ctx.guild.name}",
        color=discord.Color.from_rgb(255, 100, 0),
        timestamp=discord.utils.utcnow()
    )
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    lines = []
    for i, (uid, net) in enumerate(sorted_users):
        member = ctx.guild.get_member(uid)
        name = member.display_name if member else f"<@{uid}>"
        sign = "+" if net >= 0 else ""
        lines.append(f"{medals[i]} **{name}** — {sign}{_fmt_money(net)}")
    embed.description = "\n".join(lines) or "No data yet."
    embed.set_footer(text="TrapAI Economy")
    await ctx.send(embed=embed)


# ── Slots ────────────────────────────────────────────────────
SLOT_SYMBOLS = ["🍒", "🍋", "🍊", "🍇", "🔔", "💎", "7️⃣"]
SLOT_MULTIPLIERS = {"🍒": 2, "🍋": 2, "🍊": 3, "🍇": 3, "🔔": 5, "💎": 10, "7️⃣": 20}

@bot.command()
async def slots(ctx, bet: int = None):
    if bet is None or bet <= 0:
        await ctx.send("❌ Usage: `,slots <bet>`", delete_after=6)
        return
    data = _eco(ctx.guild.id, ctx.author.id)
    if bet > data["wallet"]:
        await ctx.send(f"❌ You only have **{_fmt_money(data['wallet'])}**.", delete_after=6)
        return
    reels = [random.choice(SLOT_SYMBOLS) for _ in range(3)]
    data["wallet"] -= bet
    if reels[0] == reels[1] == reels[2]:
        mult = SLOT_MULTIPLIERS[reels[0]]
        win = bet * mult
        data["wallet"] += win
        GAMBLE_WINS.setdefault(ctx.guild.id, {})[ctx.author.id] = \
            GAMBLE_WINS.setdefault(ctx.guild.id, {}).get(ctx.author.id, 0) + (win - bet)
        result = f"🎉 **JACKPOT!** `{' '.join(reels)}` — Won **{_fmt_money(win)}** (×{mult})!"
        color = discord.Color.purple()
    elif reels[0] == reels[1] or reels[1] == reels[2]:
        win = bet
        data["wallet"] += win
        result = f"😊 **Small Win!** `{' '.join(reels)}` — Got your bet back!"
        color = discord.Color.green()
    else:
        GAMBLE_WINS.setdefault(ctx.guild.id, {})[ctx.author.id] = \
            GAMBLE_WINS.setdefault(ctx.guild.id, {}).get(ctx.author.id, 0) - bet
        result = f"😢 **Lost!** `{' '.join(reels)}` — Lost **{_fmt_money(bet)}**."
        color = discord.Color.red()
    embed = discord.Embed(title="🎰 Slot Machine", description=result, color=color, timestamp=discord.utils.utcnow())
    embed.add_field(name="👛 Wallet", value=_fmt_money(data['wallet']), inline=True)
    embed.set_footer(text=f"Bet: {_fmt_money(bet)} • TrapAI Casino")
    await ctx.send(embed=embed)


# ── Coinflip ─────────────────────────────────────────────────
@bot.command(aliases=["cf", "flip"])
async def coinflip(ctx, bet: int = None, choice: str = None):
    if bet is None or choice is None:
        await ctx.send("❌ Usage: `,coinflip <bet> <heads|tails>`", delete_after=6)
        return
    choice = choice.lower()
    if choice not in ("heads", "tails", "h", "t"):
        await ctx.send("❌ Choose `heads` or `tails`.", delete_after=6)
        return
    if bet <= 0:
        await ctx.send("❌ Bet must be greater than 0.", delete_after=6)
        return
    data = _eco(ctx.guild.id, ctx.author.id)
    if bet > data["wallet"]:
        await ctx.send(f"❌ You only have **{_fmt_money(data['wallet'])}**.", delete_after=6)
        return
    result = random.choice(["heads", "tails"])
    won = choice in (result, result[0])
    data["wallet"] += bet if won else -bet
    GAMBLE_WINS.setdefault(ctx.guild.id, {})[ctx.author.id] = \
        GAMBLE_WINS.setdefault(ctx.guild.id, {}).get(ctx.author.id, 0) + (bet if won else -bet)
    emoji = "🪙"
    color = discord.Color.green() if won else discord.Color.red()
    embed = discord.Embed(
        title=f"{emoji} Coin Flip",
        description=(
            f"The coin landed on **{result.upper()}**!\n"
            f"{'✅ You **won** ' if won else '❌ You **lost** '}**{_fmt_money(bet)}**!"
        ),
        color=color,
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="👛 Wallet", value=_fmt_money(data['wallet']), inline=True)
    embed.set_footer(text=f"Bet: {_fmt_money(bet)} • TrapAI Casino")
    await ctx.send(embed=embed)


# ── Dice ─────────────────────────────────────────────────────
@bot.command()
async def dice(ctx, bet: int = None, guess: int = None):
    if bet is None or guess is None:
        await ctx.send("❌ Usage: `,dice <bet> <1-6>`", delete_after=6)
        return
    if not 1 <= guess <= 6:
        await ctx.send("❌ Guess must be between 1 and 6.", delete_after=6)
        return
    if bet <= 0:
        await ctx.send("❌ Bet must be greater than 0.", delete_after=6)
        return
    data = _eco(ctx.guild.id, ctx.author.id)
    if bet > data["wallet"]:
        await ctx.send(f"❌ You only have **{_fmt_money(data['wallet'])}**.", delete_after=6)
        return
    roll = random.randint(1, 6)
    dice_faces = {1:"1️⃣", 2:"2️⃣", 3:"3️⃣", 4:"4️⃣", 5:"5️⃣", 6:"6️⃣"}
    if roll == guess:
        win = bet * 5
        data["wallet"] += win
        GAMBLE_WINS.setdefault(ctx.guild.id, {})[ctx.author.id] = \
            GAMBLE_WINS.setdefault(ctx.guild.id, {}).get(ctx.author.id, 0) + win
        desc = f"{dice_faces[roll]} Rolled **{roll}** — You guessed right! Won **{_fmt_money(win)}** (×5)!"
        color = discord.Color.purple()
    else:
        data["wallet"] -= bet
        GAMBLE_WINS.setdefault(ctx.guild.id, {})[ctx.author.id] = \
            GAMBLE_WINS.setdefault(ctx.guild.id, {}).get(ctx.author.id, 0) - bet
        desc = f"{dice_faces[roll]} Rolled **{roll}** — You guessed **{guess}**. Lost **{_fmt_money(bet)}**."
        color = discord.Color.red()
    embed = discord.Embed(title="🎲 Dice Roll", description=desc, color=color, timestamp=discord.utils.utcnow())
    embed.add_field(name="👛 Wallet", value=_fmt_money(data['wallet']), inline=True)
    embed.set_footer(text=f"Bet: {_fmt_money(bet)} • TrapAI Casino")
    await ctx.send(embed=embed)


# ── High-Low ─────────────────────────────────────────────────
@bot.command(aliases=["hl"])
async def highlow(ctx, bet: int = None):
    if bet is None or bet <= 0:
        await ctx.send("❌ Usage: `,highlow <bet>`", delete_after=6)
        return
    data = _eco(ctx.guild.id, ctx.author.id)
    if bet > data["wallet"]:
        await ctx.send(f"❌ You only have **{_fmt_money(data['wallet'])}**.", delete_after=6)
        return
    # Reserve the bet immediately — otherwise someone could open two games at
    # once against the same unspent balance while this one waits on wait_for.
    data["wallet"] -= bet
    card = random.randint(1, 13)
    card_names = {1:"Ace",2:"2",3:"3",4:"4",5:"5",6:"6",7:"7",8:"8",9:"9",10:"10",11:"Jack",12:"Queen",13:"King"}

    embed = discord.Embed(
        title="🃏 High or Low?",
        description=(
            f"Card drawn: **{card_names[card]}** (`{card}`)\n\n"
            "Will the next card be **higher** or **lower**?\n"
            "Reply with `higher` or `lower` in the next **20 seconds**."
        ),
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"Bet: {_fmt_money(bet)} • TrapAI Casino")
    await ctx.send(embed=embed)

    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel and \
               m.content.lower() in ("higher", "lower", "high", "low", "h", "l")

    try:
        msg = await bot.wait_for("message", check=check, timeout=20)
    except asyncio.TimeoutError:
        data["wallet"] += bet  # refund the reservation — no bet was actually placed
        await ctx.send("⏰ Time's up! No bet placed.", delete_after=6)
        return

    next_card = random.randint(1, 13)
    guess = msg.content.lower() in ("higher", "high", "h")
    actual_higher = next_card > card
    won = (guess and actual_higher) or (not guess and not actual_higher)

    if next_card == card:
        data["wallet"] += bet  # push — refund the reservation
        await ctx.send(f"🤝 Draw! Both cards were **{card_names[next_card]}**. Bet returned.")
        return

    data["wallet"] += bet * 2 if won else 0  # win pays back stake + winnings; loss keeps it reserved-away
    GAMBLE_WINS.setdefault(ctx.guild.id, {})[ctx.author.id] = \
        GAMBLE_WINS.setdefault(ctx.guild.id, {}).get(ctx.author.id, 0) + (bet if won else -bet)

    color = discord.Color.green() if won else discord.Color.red()
    embed2 = discord.Embed(
        title="🃏 High or Low — Result",
        description=(
            f"Next card: **{card_names[next_card]}** (`{next_card}`)\n"
            f"{'✅ Correct! Won' if won else '❌ Wrong! Lost'} **{_fmt_money(bet)}**!"
        ),
        color=color,
        timestamp=discord.utils.utcnow()
    )
    embed2.add_field(name="👛 Wallet", value=_fmt_money(data['wallet']), inline=True)
    await ctx.send(embed=embed2)


# ── Blackjack ────────────────────────────────────────────────
def _bj_hand_value(hand):
    val = sum(min(c, 10) for c in hand)
    aces = hand.count(1)
    while aces and val + 10 <= 21:
        val += 10
        aces -= 1
    return val

def _bj_card_name(c):
    names = {1:"A",11:"J",12:"Q",13:"K"}
    return names.get(c, str(c))

@bot.command(aliases=["bj"])
async def blackjack(ctx, bet: int = None):
    if bet is None or bet <= 0:
        await ctx.send("❌ Usage: `,blackjack <bet>`", delete_after=6)
        return
    data = _eco(ctx.guild.id, ctx.author.id)
    if bet > data["wallet"]:
        await ctx.send(f"❌ You only have **{_fmt_money(data['wallet'])}**.", delete_after=6)
        return
    # Reserve the bet immediately — otherwise someone could open two games at
    # once against the same unspent balance while this one waits on wait_for.
    data["wallet"] -= bet

    deck = list(range(1, 14)) * 4
    random.shuffle(deck)
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]

    def hand_str(h, hide_second=False):
        cards = [_bj_card_name(c) for c in h]
        if hide_second:
            cards[1] = "🂠"
        return " ".join(cards)

    def make_embed(result_text=None, color=discord.Color.blurple()):
        e = discord.Embed(title="🃏 Blackjack", color=color, timestamp=discord.utils.utcnow())
        pv = _bj_hand_value(player)
        dv = _bj_hand_value(dealer)
        e.add_field(name=f"Your Hand ({pv})", value=hand_str(player), inline=True)
        e.add_field(name="Dealer's Hand", value=hand_str(dealer, hide_second=result_text is None), inline=True)
        if result_text:
            e.add_field(name="Result", value=result_text, inline=False)
        e.set_footer(text=f"Bet: {_fmt_money(bet)}  •  Hit: `h`  Stand: `s`  •  TrapAI Casino")
        return e

    await ctx.send(embed=make_embed())

    while True:
        pv = _bj_hand_value(player)
        if pv >= 21:
            break

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel and \
                   m.content.lower() in ("h", "hit", "s", "stand")

        try:
            msg = await bot.wait_for("message", check=check, timeout=30)
        except asyncio.TimeoutError:
            # Bet was already reserved above, so it's correctly forfeited here —
            # matches the "Dealer wins" message with no extra wallet change needed.
            await ctx.send("⏰ Time's up! Dealer wins.", delete_after=6)
            return

        if msg.content.lower() in ("h", "hit"):
            player.append(deck.pop())
            pv = _bj_hand_value(player)
            if pv > 21:
                break
            await ctx.send(embed=make_embed())
        else:
            break

    pv = _bj_hand_value(player)
    dv = _bj_hand_value(dealer)

    # Dealer draws to 17
    while dv < 17:
        dealer.append(deck.pop())
        dv = _bj_hand_value(dealer)

    # payout = credited back to wallet now (bet is already reserved/deducted above)
    # net    = win/loss for the GAMBLE_WINS leaderboard, unaffected by the reservation
    if pv > 21:
        result, color, payout, net = "💥 **Bust!** You went over 21. Dealer wins.", discord.Color.red(), 0, -bet
    elif dv > 21 or pv > dv:
        result, color, payout, net = f"🎉 **You win!** ({pv} vs {dv})", discord.Color.green(), bet * 2, bet
    elif pv == dv:
        result, color, payout, net = f"🤝 **Push!** ({pv} vs {dv}) Bet returned.", discord.Color.blurple(), bet, 0
    else:
        result, color, payout, net = f"😢 **Dealer wins!** ({dv} vs {pv})", discord.Color.red(), 0, -bet

    data["wallet"] += payout
    GAMBLE_WINS.setdefault(ctx.guild.id, {})[ctx.author.id] = \
        GAMBLE_WINS.setdefault(ctx.guild.id, {}).get(ctx.author.id, 0) + net
    await ctx.send(embed=make_embed(result_text=result, color=color))


# ── Crash ────────────────────────────────────────────────────
@bot.command()
async def crash(ctx, bet: int = None):
    if bet is None or bet <= 0:
        await ctx.send("❌ Usage: `,crash <bet>`", delete_after=6)
        return
    data = _eco(ctx.guild.id, ctx.author.id)
    if bet > data["wallet"]:
        await ctx.send(f"❌ You only have **{_fmt_money(data['wallet'])}**.", delete_after=6)
        return

    data["wallet"] -= bet
    multiplier = 1.0
    crashed = False

    embed = discord.Embed(
        title="🚀 Crash",
        description=f"Multiplier: **×{multiplier:.2f}**\n\nType `cashout` to cash out before it crashes!",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"Bet: {_fmt_money(bet)} • TrapAI Casino")
    msg = await ctx.send(embed=embed)

    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel and \
               m.content.lower() in ("cashout", "cash out", "out", "stop")

    while multiplier < 20:
        await asyncio.sleep(1.5)
        if random.random() < (0.06 * multiplier):
            crashed = True
            break
        multiplier = round(multiplier + random.uniform(0.1, 0.6), 2)
        embed.description = f"🚀 Multiplier: **×{multiplier:.2f}**\n\nType `cashout` to cash out!"
        try:
            await msg.edit(embed=embed)
        except discord.HTTPException:
            pass

        # Check if user cashed out
        try:
            await bot.wait_for("message", check=check, timeout=0.1)
            won = int(bet * multiplier)
            data["wallet"] += won
            GAMBLE_WINS.setdefault(ctx.guild.id, {})[ctx.author.id] = \
                GAMBLE_WINS.setdefault(ctx.guild.id, {}).get(ctx.author.id, 0) + (won - bet)
            fin = discord.Embed(
                title="🚀 Cashed Out!",
                description=f"Cashed out at **×{multiplier:.2f}** — Won **{_fmt_money(won)}**!",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow()
            )
            fin.add_field(name="👛 Wallet", value=_fmt_money(data['wallet']), inline=True)
            await ctx.send(embed=fin)
            return
        except asyncio.TimeoutError:
            pass

    GAMBLE_WINS.setdefault(ctx.guild.id, {})[ctx.author.id] = \
        GAMBLE_WINS.setdefault(ctx.guild.id, {}).get(ctx.author.id, 0) - bet
    fin = discord.Embed(
        title="💥 Crashed!",
        description=f"The rocket crashed at **×{multiplier:.2f}**. You lost **{_fmt_money(bet)}**!",
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow()
    )
    fin.add_field(name="👛 Wallet", value=_fmt_money(data['wallet']), inline=True)
    await ctx.send(embed=fin)


# ── Duels (PvP) ──────────────────────────────────────────────
# ,duel @user <coinflip|dice|rps|blackjack> <bet> — heads-up bet between
# two real members, winner takes both wagers. Both bets are reserved the
# moment the challenge is *accepted* (not when it's sent), and every
# resolver below only ever adds to wallets afterward — it never
# subtracts again — matching the reserve-then-award pattern the
# single-player casino games above already use.

def _record_duel_result(guild_id, winner_id, loser_id, bet):
    GAMBLE_WINS.setdefault(guild_id, {})[winner_id] = \
        GAMBLE_WINS.setdefault(guild_id, {}).get(winner_id, 0) + bet
    GAMBLE_WINS.setdefault(guild_id, {})[loser_id] = \
        GAMBLE_WINS.setdefault(guild_id, {}).get(loser_id, 0) - bet


async def _duel_coinflip(channel, guild_id, challenger, opponent, bet):
    winner = random.choice([challenger, opponent])
    loser = opponent if winner == challenger else challenger
    _eco(guild_id, winner.id)["wallet"] += bet * 2
    _record_duel_result(guild_id, winner.id, loser.id, bet)
    embed = discord.Embed(
        title="🪙 Duel — Coin Flip",
        description=f"The coin landed in {winner.mention}'s favor!\n🎉 **{winner.display_name} wins {_fmt_money(bet * 2)}**!",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    await channel.send(embed=embed)


async def _duel_dice(channel, guild_id, challenger, opponent, bet):
    while True:
        c_roll, o_roll = random.randint(1, 6), random.randint(1, 6)
        if c_roll != o_roll:
            break
    winner, loser = (challenger, opponent) if c_roll > o_roll else (opponent, challenger)
    _eco(guild_id, winner.id)["wallet"] += bet * 2
    _record_duel_result(guild_id, winner.id, loser.id, bet)
    embed = discord.Embed(
        title="🎲 Duel — Dice",
        description=(
            f"{challenger.mention} rolled **{c_roll}** • {opponent.mention} rolled **{o_roll}**\n\n"
            f"🎉 **{winner.display_name} wins {_fmt_money(bet * 2)}**!"
        ),
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    await channel.send(embed=embed)


class DuelRPSView(discord.ui.View):
    CHOICES = {"rock": "✊", "paper": "✋", "scissors": "✌️"}
    BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}

    def __init__(self, guild_id, challenger, opponent, bet):
        super().__init__(timeout=30)
        self.guild_id = guild_id
        self.challenger = challenger
        self.opponent = opponent
        self.bet = bet
        self.picks = {}
        self.message = None
        for name, emoji in self.CHOICES.items():
            self.add_item(self._make_button(name, emoji))

    def _make_button(self, name, emoji):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id not in (self.challenger.id, self.opponent.id):
                await interaction.response.send_message("❌ This isn't your duel.", ephemeral=True)
                return
            if interaction.user.id in self.picks:
                await interaction.response.send_message("❌ You already picked.", ephemeral=True)
                return
            self.picks[interaction.user.id] = name
            await interaction.response.send_message(f"You picked **{name.title()}**! Waiting on your opponent...", ephemeral=True)
            if len(self.picks) == 2:
                await self._resolve()
        button = discord.ui.Button(label=name.title(), emoji=emoji, style=discord.ButtonStyle.secondary)
        button.callback = callback
        return button

    async def _resolve(self):
        for child in self.children:
            child.disabled = True
        c_pick = self.picks[self.challenger.id]
        o_pick = self.picks[self.opponent.id]
        if c_pick == o_pick:
            _eco(self.guild_id, self.challenger.id)["wallet"] += self.bet
            _eco(self.guild_id, self.opponent.id)["wallet"] += self.bet
            desc = (
                f"{self.challenger.mention}: {self.CHOICES[c_pick]} **{c_pick.title()}**\n"
                f"{self.opponent.mention}: {self.CHOICES[o_pick]} **{o_pick.title()}**\n\n"
                "🤝 **Tie!** Bets returned."
            )
            color = discord.Color.blurple()
        else:
            winner, loser = (self.challenger, self.opponent) if self.BEATS[c_pick] == o_pick else (self.opponent, self.challenger)
            _eco(self.guild_id, winner.id)["wallet"] += self.bet * 2
            _record_duel_result(self.guild_id, winner.id, loser.id, self.bet)
            desc = (
                f"{self.challenger.mention}: {self.CHOICES[c_pick]} **{c_pick.title()}**\n"
                f"{self.opponent.mention}: {self.CHOICES[o_pick]} **{o_pick.title()}**\n\n"
                f"🎉 **{winner.display_name} wins {_fmt_money(self.bet * 2)}**!"
            )
            color = discord.Color.green()
        embed = discord.Embed(title="✊✋✌️ Duel — Rock Paper Scissors", description=desc, color=color, timestamp=discord.utils.utcnow())
        if self.message:
            try:
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass

    async def on_timeout(self):
        if len(self.picks) >= 2 or not self.message:
            return
        _eco(self.guild_id, self.challenger.id)["wallet"] += self.bet
        _eco(self.guild_id, self.opponent.id)["wallet"] += self.bet
        for child in self.children:
            child.disabled = True
        try:
            await self.message.edit(content="⏰ Duel timed out — bets refunded.", view=self)
        except discord.HTTPException:
            pass


async def _duel_rps(channel, guild_id, challenger, opponent, bet):
    view = DuelRPSView(guild_id, challenger, opponent, bet)
    embed = discord.Embed(
        title="✊✋✌️ Duel — Rock Paper Scissors",
        description=f"{challenger.mention} vs {opponent.mention}\n\nBoth players: pick your move below (privately)!",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    view.message = await channel.send(embed=embed, view=view)


async def _duel_blackjack(channel, guild_id, challenger, opponent, bet):
    deck = list(range(1, 14)) * 4
    random.shuffle(deck)
    hands = {challenger.id: [deck.pop(), deck.pop()], opponent.id: [deck.pop(), deck.pop()]}
    order = [challenger, opponent]

    def hand_str(h):
        return " ".join(_bj_card_name(c) for c in h)

    def make_embed(turn_player=None, result_text=None):
        e = discord.Embed(title="🃏 Duel — Blackjack", color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
        for p in order:
            pv = _bj_hand_value(hands[p.id])
            e.add_field(name=f"{p.display_name} ({pv})", value=hand_str(hands[p.id]), inline=True)
        if result_text:
            e.add_field(name="Result", value=result_text, inline=False)
        elif turn_player:
            e.add_field(name="Turn", value=f"{turn_player.mention}'s move — reply `h`/`hit` or `s`/`stand`", inline=False)
        e.set_footer(text=f"Bet: {_fmt_money(bet)} each  •  TrapAI Casino")
        return e

    msg = await channel.send(embed=make_embed(turn_player=order[0]))
    busted = set()

    for player in order:
        while True:
            pv = _bj_hand_value(hands[player.id])
            if pv >= 21:
                if pv > 21:
                    busted.add(player.id)
                break

            def check(m, player=player):
                return m.author.id == player.id and m.channel.id == channel.id and \
                       m.content.lower() in ("h", "hit", "s", "stand")

            try:
                reply = await bot.wait_for("message", check=check, timeout=30)
            except asyncio.TimeoutError:
                await channel.send(f"⏰ {player.mention} took too long — auto-stand.")
                break
            if reply.content.lower() in ("h", "hit"):
                hands[player.id].append(deck.pop())
                pv = _bj_hand_value(hands[player.id])
                if pv > 21:
                    busted.add(player.id)
                    await msg.edit(embed=make_embed(turn_player=player))
                    break
                await msg.edit(embed=make_embed(turn_player=player))
            else:
                break
        next_idx = order.index(player) + 1
        if next_idx < len(order):
            await msg.edit(embed=make_embed(turn_player=order[next_idx]))

    c_val, o_val = _bj_hand_value(hands[challenger.id]), _bj_hand_value(hands[opponent.id])
    if challenger.id in busted and opponent.id in busted:
        _eco(guild_id, challenger.id)["wallet"] += bet
        _eco(guild_id, opponent.id)["wallet"] += bet
        result = "🤝 **Both busted!** Bets returned."
    elif challenger.id in busted:
        _eco(guild_id, opponent.id)["wallet"] += bet * 2
        _record_duel_result(guild_id, opponent.id, challenger.id, bet)
        result = f"💥 {challenger.display_name} busted! **{opponent.display_name} wins {_fmt_money(bet * 2)}**!"
    elif opponent.id in busted:
        _eco(guild_id, challenger.id)["wallet"] += bet * 2
        _record_duel_result(guild_id, challenger.id, opponent.id, bet)
        result = f"💥 {opponent.display_name} busted! **{challenger.display_name} wins {_fmt_money(bet * 2)}**!"
    elif c_val == o_val:
        _eco(guild_id, challenger.id)["wallet"] += bet
        _eco(guild_id, opponent.id)["wallet"] += bet
        result = f"🤝 **Push!** Both had {c_val}. Bets returned."
    elif c_val > o_val:
        _eco(guild_id, challenger.id)["wallet"] += bet * 2
        _record_duel_result(guild_id, challenger.id, opponent.id, bet)
        result = f"🎉 **{challenger.display_name} wins {_fmt_money(bet * 2)}**! ({c_val} vs {o_val})"
    else:
        _eco(guild_id, opponent.id)["wallet"] += bet * 2
        _record_duel_result(guild_id, opponent.id, challenger.id, bet)
        result = f"🎉 **{opponent.display_name} wins {_fmt_money(bet * 2)}**! ({o_val} vs {c_val})"

    await msg.edit(embed=make_embed(result_text=result))


DUEL_GAMES = {
    "coinflip": _duel_coinflip,
    "dice": _duel_dice,
    "rps": _duel_rps,
    "blackjack": _duel_blackjack,
}
DUEL_GAME_NAMES = {
    "coinflip": "Coin Flip", "dice": "Dice",
    "rps": "Rock Paper Scissors", "blackjack": "Blackjack",
}
DUEL_GAME_ALIASES = {"cf": "coinflip", "flip": "coinflip", "bj": "blackjack"}


class DuelChallengeView(discord.ui.View):
    def __init__(self, challenger, opponent, game, bet):
        super().__init__(timeout=60)
        self.challenger = challenger
        self.opponent = opponent
        self.game = game
        self.bet = bet
        self.resolved = False
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message("❌ This challenge isn't for you.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.resolved or not self.message:
            return
        for child in self.children:
            child.disabled = True
        try:
            await self.message.edit(content="⏰ Challenge expired — no response.", embed=None, view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Accept", emoji="✅", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.resolved = True
        for child in self.children:
            child.disabled = True

        guild_id = interaction.guild.id
        challenger_data = _eco(guild_id, self.challenger.id)
        opponent_data = _eco(guild_id, self.opponent.id)
        if challenger_data["wallet"] < self.bet:
            await interaction.response.edit_message(
                content=f"❌ {self.challenger.mention} no longer has enough to cover the bet — challenge cancelled.",
                embed=None, view=self
            )
            return
        if opponent_data["wallet"] < self.bet:
            await interaction.response.edit_message(
                content="❌ You don't have enough to cover the bet — challenge cancelled.",
                embed=None, view=self
            )
            return

        challenger_data["wallet"] -= self.bet
        opponent_data["wallet"] -= self.bet

        await interaction.response.edit_message(
            content=f"✅ Challenge accepted! Starting **{DUEL_GAME_NAMES[self.game]}**...",
            embed=None, view=self
        )
        await DUEL_GAMES[self.game](interaction.channel, guild_id, self.challenger, self.opponent, self.bet)

    @discord.ui.button(label="Decline", emoji="❌", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.resolved = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"❌ {self.opponent.mention} declined the challenge.", embed=None, view=self
        )


@bot.command(aliases=["challenge"])
async def duel(ctx, opponent: discord.Member = None, game: str = None, bet: int = None):
    """
    Challenge another member to a heads-up bet — winner takes both wagers.
    Usage: ,duel @user <coinflip|dice|rps|blackjack> <bet>
    Aliases: cf/flip = coinflip, bj = blackjack
    """
    if opponent is None or game is None or bet is None:
        await ctx.send("❌ Usage: `,duel @user <coinflip|dice|rps|blackjack> <bet>`", delete_after=10)
        return
    if opponent == ctx.author or opponent.bot:
        await ctx.send("❌ Pick a real member who isn't you or a bot.", delete_after=6)
        return
    if bet <= 0:
        await ctx.send("❌ Bet must be greater than 0.", delete_after=6)
        return
    game_key = DUEL_GAME_ALIASES.get(game.lower(), game.lower())
    if game_key not in DUEL_GAMES:
        await ctx.send("❌ Unknown game. Choose from `coinflip`, `dice`, `rps`, `blackjack`.", delete_after=8)
        return

    challenger_data = _eco(ctx.guild.id, ctx.author.id)
    if bet > challenger_data["wallet"]:
        await ctx.send(f"❌ You only have **{_fmt_money(challenger_data['wallet'])}**.", delete_after=6)
        return
    opponent_data = _eco(ctx.guild.id, opponent.id)
    if bet > opponent_data["wallet"]:
        await ctx.send(f"❌ {opponent.display_name} doesn't have enough to cover that bet.", delete_after=8)
        return

    view = DuelChallengeView(ctx.author, opponent, game_key, bet)
    embed = discord.Embed(
        title="⚔️ Duel Challenge!",
        description=(
            f"{ctx.author.mention} is challenging {opponent.mention} to **{DUEL_GAME_NAMES[game_key]}**!\n\n"
            f"💰 Bet: **{_fmt_money(bet)}** each — winner takes **{_fmt_money(bet * 2)}**.\n\n"
            f"{opponent.mention}, accept or decline below (60 seconds)."
        ),
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow()
    )
    view.message = await ctx.send(embed=embed, view=view)


# ── Sports Mini-Games (Basketball / Archery / Cup Pong) ──────
# All three follow the same shape as slots/dice above: bet against the
# house, a weighted random outcome tier decides the multiplier, reserve
# the bet up front and only ever add the payout back afterward.

async def _play_sports_bet(ctx, bet, title, footer_tag, tiers):
    """tiers: list of (threshold, multiplier, description, color) checked
    in order against a random() roll — first threshold the roll is below
    wins that tier. multiplier 0 = total loss, 1 = bet back, >1 = profit."""
    if bet is None or bet <= 0:
        await ctx.send(f"❌ Usage: `,{footer_tag.lower()} <bet>`", delete_after=6)
        return
    data = _eco(ctx.guild.id, ctx.author.id)
    if bet > data["wallet"]:
        await ctx.send(f"❌ You only have **{_fmt_money(data['wallet'])}**.", delete_after=6)
        return
    data["wallet"] -= bet
    roll = random.random()
    cumulative = 0.0
    for threshold, mult, desc, color in tiers:
        cumulative += threshold
        if roll < cumulative:
            break
    win = int(bet * mult)
    data["wallet"] += win
    net = win - bet
    GAMBLE_WINS.setdefault(ctx.guild.id, {})[ctx.author.id] = \
        GAMBLE_WINS.setdefault(ctx.guild.id, {}).get(ctx.author.id, 0) + net
    if net > 0:
        result = f"{desc}\n🎉 **Won {_fmt_money(win)}**!"
    elif net == 0:
        result = f"{desc}\n🤝 **Bet returned.**"
    else:
        result = f"{desc}\n😢 **Lost {_fmt_money(bet)}**."
    embed = discord.Embed(title=title, description=result, color=color, timestamp=discord.utils.utcnow())
    embed.add_field(name="👛 Wallet", value=_fmt_money(data['wallet']), inline=True)
    embed.set_footer(text=f"Bet: {_fmt_money(bet)} • TrapAI Casino")
    await ctx.send(embed=embed)


@bot.command(aliases=["bball"])
async def basketball(ctx, bet: int = None):
    """Shoot a bet on a basketball hoop. Usage: ,basketball <bet>"""
    await _play_sports_bet(ctx, bet, "🏀 Basketball", "basketball", [
        (0.15, 3.0, "🏀 **SWISH!** Nothing but net from downtown!", discord.Color.purple()),
        (0.35, 1.5, "🏀 **It's in!** Good shot.", discord.Color.green()),
        (0.50, 0.0, "🏀 **Airball!** Way off the mark.", discord.Color.red()),
    ])


@bot.command()
async def archery(ctx, bet: int = None):
    """Fire a bet at an archery target. Usage: ,archery <bet>"""
    await _play_sports_bet(ctx, bet, "🏹 Archery", "archery", [
        (0.10, 5.0, "🎯 **BULLSEYE!** Dead center!", discord.Color.purple()),
        (0.25, 2.0, "🏹 **Inner ring!** Great shot.", discord.Color.green()),
        (0.30, 1.0, "🏹 **Outer ring.** Bet returned.", discord.Color.blurple()),
        (0.35, 0.0, "🏹 **Missed the target entirely!**", discord.Color.red()),
    ])


@bot.command(aliases=["pong"])
async def cuppong(ctx, bet: int = None):
    """Toss a bet into the cup. Usage: ,cuppong <bet>"""
    await _play_sports_bet(ctx, bet, "🏓 Cup Pong", "cuppong", [
        (0.25, 2.5, "🏓 **Perfect sink!** Straight in, no rim.", discord.Color.purple()),
        (0.30, 1.5, "🏓 **Rattled in!** Off the rim but it counts.", discord.Color.green()),
        (0.45, 0.0, "🏓 **Bounced right out.** Better luck next time.", discord.Color.red()),
    ])


# ── Rock Paper Scissors ──────────────────────────────────────
@bot.command(aliases=["rps"])
async def rockpaperscissors(ctx, choice: str = None):
    choices = {"rock": "✊", "paper": "✋", "scissors": "✌️",
               "r": "✊", "p": "✋", "s": "✌️"}
    if choice is None or choice.lower() not in choices:
        await ctx.send("❌ Usage: `,rps <rock|paper|scissors>`", delete_after=6)
        return
    choice = choice.lower()
    full = {"r": "rock", "p": "paper", "s": "scissors"}.get(choice, choice)
    bot_choice = random.choice(["rock", "paper", "scissors"])
    wins = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
    if full == bot_choice:
        result, color = "🤝 **It's a tie!**", discord.Color.blurple()
    elif wins[full] == bot_choice:
        result, color = "🎉 **You win!**", discord.Color.green()
    else:
        result, color = "😢 **You lose!**", discord.Color.red()
    embed = discord.Embed(
        title="✊✋✌️ Rock Paper Scissors",
        description=(
            f"You: {choices[choice]} **{full.title()}**\n"
            f"Bot: {choices[bot_choice]} **{bot_choice.title()}**\n\n"
            f"{result}"
        ),
        color=color,
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text="TrapAI Games")
    await ctx.send(embed=embed)


# ── Trivia ───────────────────────────────────────────────────
TRIVIA_QUESTIONS = [
    ("What is the capital of France?", "paris"),
    ("How many sides does a hexagon have?", "6"),
    ("What is the largest planet in the solar system?", "jupiter"),
    ("What is 12 × 12?", "144"),
    ("What element does 'O' represent on the periodic table?", "oxygen"),
    ("Who wrote Romeo and Juliet?", "shakespeare"),
    ("What is the fastest land animal?", "cheetah"),
    ("How many continents are there?", "7"),
    ("What is the boiling point of water in Celsius?", "100"),
    ("Who painted the Mona Lisa?", "da vinci"),
    ("What is the smallest prime number?", "2"),
    ("What planet is known as the Red Planet?", "mars"),
    ("How many bones are in the adult human body?", "206"),
    ("What is the chemical symbol for gold?", "au"),
    ("In what year did World War II end?", "1945"),
    ("What is the longest river in the world?", "nile"),
    ("How many strings does a standard guitar have?", "6"),
    ("What is the speed of light in km/s (approx)?", "300000"),
    ("What is the powerhouse of the cell?", "mitochondria"),
    ("What language has the most native speakers?", "mandarin"),
]

@bot.command()
async def trivia(ctx):
    question, answer = random.choice(TRIVIA_QUESTIONS)
    reward = random.randint(30, 100)
    embed = discord.Embed(
        title="🧠 Trivia Time!",
        description=f"**{question}**\n\nYou have **30 seconds** to answer! Correct = **{_fmt_money(reward)}**",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text="TrapAI Games")
    await ctx.send(embed=embed)

    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel

    try:
        msg = await bot.wait_for("message", check=check, timeout=30)
    except asyncio.TimeoutError:
        await ctx.send(f"⏰ Time's up! The answer was **{answer}**.", delete_after=10)
        return

    if msg.content.strip().lower() == answer.lower():
        _add_earned(ctx.guild.id, ctx.author.id, reward)
        await ctx.send(f"✅ Correct! You earned **{_fmt_money(reward)}**! 🎉")
    else:
        await ctx.send(f"❌ Wrong! The correct answer was **{answer}**.")


# ── Number Guess ─────────────────────────────────────────────
@bot.command(aliases=["ng", "guess"])
async def numguess(ctx):
    number = random.randint(1, 100)
    attempts = 7
    reward = 150

    await ctx.send(
        f"🔢 **Number Guessing Game!**\n"
        f"I'm thinking of a number between **1 and 100**.\n"
        f"You have **{attempts} attempts**. Correct = **{_fmt_money(reward)}**!"
    )

    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel and m.content.isdigit()

    for attempt in range(1, attempts + 1):
        try:
            msg = await bot.wait_for("message", check=check, timeout=30)
        except asyncio.TimeoutError:
            await ctx.send(f"⏰ Time's up! The number was **{number}**.", delete_after=8)
            return
        guess = int(msg.content)
        if guess == number:
            _add_earned(ctx.guild.id, ctx.author.id, reward)
            await ctx.send(f"🎉 **Correct in {attempt} attempt(s)!** You earned **{_fmt_money(reward)}**!")
            return
        elif guess < number:
            await ctx.send(f"📈 Too low! ({attempts - attempt} attempts left)")
        else:
            await ctx.send(f"📉 Too high! ({attempts - attempt} attempts left)")

    await ctx.send(f"😢 Out of attempts! The number was **{number}**.")


# ── Hangman ──────────────────────────────────────────────────
HANGMAN_WORDS = [
    "python", "discord", "server", "economy", "jackpot", "keyboard",
    "galaxy", "triumph", "mystery", "rhythm", "journey", "justice",
    "quantum", "shadow", "thunder", "wizard", "castle", "dragon",
    "pirate", "crystal", "fortune", "empire", "blizzard", "phantom",
]

HANGMAN_STAGES = [
    "```\n  +---+\n  |   |\n      |\n      |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n      |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n  |   |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n /|   |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n /|\\  |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n /|\\  |\n /    |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n /|\\  |\n / \\  |\n      |\n=========```",
]

@bot.command()
async def hangman(ctx):
    word = random.choice(HANGMAN_WORDS)
    guessed = set()
    wrong = 0
    max_wrong = 6
    reward = 200

    def display():
        return " ".join(c if c in guessed else "_" for c in word)

    await ctx.send(
        f"🪢 **Hangman!** Guess the word letter by letter.\n"
        f"{HANGMAN_STAGES[0]}\n`{display()}`\nWrong: 0/{max_wrong} | Correct = **{_fmt_money(reward)}**"
    )

    def check(m):
        return (
            m.author == ctx.author and
            m.channel == ctx.channel and
            len(m.content) == 1 and
            m.content.isalpha()
        )

    while wrong < max_wrong:
        try:
            msg = await bot.wait_for("message", check=check, timeout=40)
        except asyncio.TimeoutError:
            await ctx.send(f"⏰ Time's up! The word was **{word}**.", delete_after=8)
            return

        letter = msg.content.lower()
        if letter in guessed:
            await ctx.send(f"⚠️ Already guessed `{letter}`.", delete_after=4)
            continue
        guessed.add(letter)

        if letter in word:
            board = display()
            if "_" not in board:
                _add_earned(ctx.guild.id, ctx.author.id, reward)
                await ctx.send(
                    f"{HANGMAN_STAGES[wrong]}\n✅ **You got it!** The word was **{word}**!\n"
                    f"Earned **{_fmt_money(reward)}**! 🎉"
                )
                return
            await ctx.send(f"{HANGMAN_STAGES[wrong]}\n✅ `{letter}` is in the word!\n`{board}`")
        else:
            wrong += 1
            await ctx.send(
                f"{HANGMAN_STAGES[wrong]}\n❌ `{letter}` is not in the word!\n"
                f"`{display()}` | Wrong: {wrong}/{max_wrong}"
            )

    await ctx.send(f"💀 You lost! The word was **{word}**.")


# ── Wordle ───────────────────────────────────────────────────
WORDLE_WORDS = [
    "about", "above", "actor", "adapt", "admit", "adopt", "after", "again",
    "agent", "agree", "alarm", "album", "alert", "alike", "alive", "allow",
    "alone", "among", "angel", "anger", "angle", "apple", "apply", "arena",
    "argue", "arise", "armor", "aside", "asset", "audio", "audit", "avoid",
    "awake", "award", "aware", "badge", "baker", "basic", "basin", "beach",
    "beast", "begin", "being", "below", "bench", "birth", "black", "blade",
    "blame", "blank", "blast", "blend", "bless", "blind", "block", "blood",
    "board", "boost", "booth", "bound", "brain", "brand", "brave", "bread",
    "break", "brick", "bride", "brief", "bring", "broad", "broke", "brown",
    "build", "built", "bunch", "burst", "cabin", "cable", "camel", "canal",
    "candy", "cargo", "carry", "catch", "cause", "chain", "chair", "chalk",
    "charm", "chart", "chase", "cheap", "check", "cheer", "chest", "chief",
    "child", "choir", "chose", "civic", "claim", "class", "clean", "clear",
    "climb", "clock", "close", "cloud", "coach", "coast", "could", "count",
    "court", "cover", "craft", "crash", "crazy", "cream", "creek", "crime",
    "cross", "crowd", "crown", "crush", "curve", "cycle", "daily", "dance",
    "dealt", "death", "debut", "delay", "depth", "diary", "dirty", "doubt",
    "dozen", "draft", "drama", "dream", "dress", "drift", "drink", "drive",
    "eager", "early", "earth", "eight", "elite", "empty", "enemy", "enjoy",
    "enter", "equal", "error", "event", "every", "exact", "exist", "extra",
    "faith", "fault", "favor", "fence", "fewer", "fiber", "field", "fifth",
    "fight", "final", "first", "flame", "flash", "fleet", "flesh", "float",
    "flock", "floor", "focus", "force", "forge", "forth", "found", "frame",
    "fresh", "front", "frost", "fruit", "fully", "funny", "giant", "given",
    "glass", "globe", "glory", "grace", "grade", "grain", "grand", "grant",
    "grass", "great", "green", "greet", "grief", "grill", "gross", "group",
    "guard", "guess", "guest", "guide", "habit", "happy", "harsh", "haste",
    "heart", "heavy", "hedge", "hello", "hobby", "honor", "horse", "hotel",
    "house", "human", "humor", "ideal", "image", "index", "inner", "input",
    "issue", "ivory", "japan", "jelly", "joint", "judge", "juice", "known",
    "label", "labor", "large", "laser", "later", "laugh", "layer", "learn",
    "least", "level", "light", "limit", "little", "lobby", "local", "lodge",
    "logic", "loose", "lower", "loyal", "lucky", "lunch", "lying", "magic",
    "major", "maker", "march", "match", "maybe", "mayor", "medal", "media",
    "metal", "meter", "might", "minor", "minus", "mixed", "model", "moist",
    "money", "month", "moral", "motor", "mount", "mouse", "mouth", "movie",
    "music", "needy", "nerve", "never", "newly", "night", "noble", "noise",
    "north", "novel", "nurse", "ocean", "offer", "often", "older", "olive",
    "opera", "orbit", "order", "organ", "otter", "ought", "outer", "owner",
    "paint", "panel", "panic", "party", "pasta", "patch", "pause", "peace",
    "phase", "phone", "photo", "piece", "pilot", "pitch", "pizza", "place",
    "plain", "plane", "plant", "plate", "point", "pound", "power", "press",
    "price", "pride", "prime", "print", "prior", "prize", "proof", "proud",
    "prove", "pulse", "punch", "pupil", "purse", "queen", "quick", "quiet",
    "quite", "radio", "raise", "range", "rapid", "reach", "react", "ready",
    "realm", "rebel", "refer", "relax", "reply", "right", "rigid", "rival",
    "river", "roast", "robot", "rocky", "roman", "rough", "round", "route",
    "royal", "rural", "sadly", "salad", "sauce", "scale", "scare", "scene",
    "scope", "score", "sense", "serve", "seven", "shade", "shake", "shall",
    "shape", "share", "sharp", "sheet", "shelf", "shell", "shift", "shine",
    "shirt", "shock", "shoot", "shore", "short", "shown", "sight", "silly",
    "since", "skill", "sleep", "slide", "slope", "small", "smart", "smell",
    "smile", "smoke", "snake", "solar", "solid", "solve", "sorry", "sound",
    "south", "space", "spare", "spark", "speak", "speed", "spell", "spend",
    "spice", "spine", "spite", "split", "sport", "spray", "spring", "squad",
    "stack", "staff", "stage", "stake", "stand", "stark", "start", "state",
    "steam", "steel", "steep", "steer", "stick", "stiff", "still", "stock",
    "stone", "store", "storm", "story", "strip", "study", "stuff", "style",
    "sugar", "suite", "super", "sweet", "swift", "swing", "sword", "table",
    "taken", "taste", "teach", "thank", "theme", "there", "thick", "thing",
    "think", "third", "those", "three", "throw", "thumb", "tiger", "tight",
    "timer", "title", "today", "topic", "total", "touch", "tough", "tower",
    "toxic", "trace", "track", "trade", "trail", "train", "trait", "trash",
    "treat", "trend", "trial", "tribe", "trick", "tried", "truck", "trust",
    "truth", "twice", "under", "union", "unity", "until", "upper", "upset",
    "urban", "usage", "usual", "valid", "value", "video", "virus", "visit",
    "vital", "voice", "waste", "watch", "water", "weigh", "which", "while",
    "white", "whole", "whose", "woman", "world", "worry", "worst", "worth",
    "would", "wound", "write", "wrong", "yield", "young", "youth",
]
WORDLE_WORDS = [w for w in WORDLE_WORDS if len(w) == 5]  # a couple entries above are 6 letters — drop them


def _wordle_feedback(guess: str, secret: str) -> str:
    result = ["⬛"] * 5
    remaining = list(secret)
    for i in range(5):
        if guess[i] == secret[i]:
            result[i] = "🟩"
            remaining[i] = None
    for i in range(5):
        if result[i] == "⬛" and guess[i] in remaining:
            result[i] = "🟨"
            remaining[remaining.index(guess[i])] = None
    return "".join(result)


@bot.command()
async def wordle(ctx):
    """Guess the secret 5-letter word in 6 tries. Usage: ,wordle"""
    secret = random.choice(WORDLE_WORDS)
    max_attempts = 6
    await ctx.send(
        f"🟩 **Wordle!** Guess the **5-letter word** in **{max_attempts} tries** — reply right here.\n"
        "🟩 = right letter, right spot  •  🟨 = right letter, wrong spot  •  ⬛ = not in the word"
    )

    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel and \
               len(m.content) == 5 and m.content.isalpha()

    history = []
    for attempt in range(1, max_attempts + 1):
        try:
            msg = await bot.wait_for("message", check=check, timeout=60)
        except asyncio.TimeoutError:
            await ctx.send(f"⏰ Time's up! The word was **{secret.upper()}**.", delete_after=10)
            return
        guess = msg.content.lower()
        feedback = _wordle_feedback(guess, secret)
        history.append(f"{feedback}  `{guess.upper()}`")
        if guess == secret:
            reward = max(50, 350 - (attempt - 1) * 50)
            _add_earned(ctx.guild.id, ctx.author.id, reward)
            await ctx.send(
                "\n".join(history) +
                f"\n\n🎉 **Correct in {attempt}/{max_attempts}!** You earned **{_fmt_money(reward)}**!"
            )
            return
        await ctx.send("\n".join(history) + f"\n*({max_attempts - attempt} tries left)*")

    await ctx.send(f"😢 Out of tries! The word was **{secret.upper()}**.")


# ── 8-Ball ───────────────────────────────────────────────────
_8BALL_RESPONSES = [
    "✅ It is certain.", "✅ It is decidedly so.", "✅ Without a doubt.",
    "✅ Yes, definitely.", "✅ You may rely on it.", "✅ As I see it, yes.",
    "✅ Most likely.", "✅ Outlook good.", "✅ Yes.", "✅ Signs point to yes.",
    "🤷 Reply hazy, try again.", "🤷 Ask again later.", "🤷 Better not tell you now.",
    "🤷 Cannot predict now.", "🤷 Concentrate and ask again.",
    "❌ Don't count on it.", "❌ My reply is no.", "❌ My sources say no.",
    "❌ Outlook not so good.", "❌ Very doubtful.",
]

@bot.command(name="8ball", aliases=["eightball"])
async def eightball(ctx, *, question: str = None):
    if not question:
        await ctx.send("❌ Usage: `,8ball <question>`", delete_after=6)
        return
    embed = discord.Embed(
        title="🎱 Magic 8-Ball",
        color=discord.Color.dark_purple(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="❓ Question", value=question, inline=False)
    embed.add_field(name="🔮 Answer",   value=random.choice(_8BALL_RESPONSES), inline=False)
    embed.set_footer(text=f"Asked by {ctx.author.display_name} • TrapAI Games")
    await ctx.send(embed=embed)


# ── 21 Questions ─────────────────────────────────────────────
QUESTIONS_21 = [
    "What's a fear you've never told anyone about?",
    "What's the biggest risk you've ever taken?",
    "What's something you believed as a kid that turned out to be false?",
    "If you could relive one day of your life, which would it be?",
    "What's a habit you're trying to break?",
    "What's the best advice you've ever received?",
    "What's something you're proud of that most people don't know about?",
    "If money didn't matter, what would you do with your life?",
    "What's a decision you regret the most?",
    "Who has had the biggest influence on who you are today?",
    "What's something you'd change about yourself if you could?",
    "What's a moment that changed how you see the world?",
    "What's your biggest goal for the next 5 years?",
    "What's the kindest thing anyone's ever done for you?",
    "What's a lie you told that you still think about?",
    "What's something you want to be remembered for?",
    "What's the hardest thing you've ever had to do?",
    "What's a talent you have that nobody knows about?",
    "What's something you're grateful for that you don't say out loud enough?",
    "What's a question you wish people asked you more?",
    "If you could give your younger self one piece of advice, what would it be?",
]

@bot.command(name="21questions", aliases=["21q", "deepquestion"])
async def twentyonequestions(ctx):
    """Drop a random deep/personal question to spark conversation. Usage: ,21questions"""
    embed = discord.Embed(
        title="❓ 21 Questions",
        description=random.choice(QUESTIONS_21),
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"Asked for {ctx.author.display_name} • TrapAI Games — no wrong answers")
    await ctx.send(embed=embed)


# ── Tic-Tac-Toe ──────────────────────────────────────────────
class TicTacToeButton(discord.ui.Button):
    def __init__(self, row, col):
        super().__init__(style=discord.ButtonStyle.secondary, label="⬜", row=row)
        self.row_idx = row
        self.col_idx = col

    async def callback(self, interaction: discord.Interaction):
        view: TicTacToeView = self.view
        if interaction.user != view.current_player:
            await interaction.response.send_message("❌ It's not your turn!", ephemeral=True)
            return
        if self.label != "⬜":
            await interaction.response.send_message("❌ That cell is taken.", ephemeral=True)
            return
        mark = "❌" if view.current_player == view.player_x else "⭕"
        self.label = mark
        self.style = discord.ButtonStyle.danger if mark == "❌" else discord.ButtonStyle.primary
        self.disabled = True
        view.board[self.row_idx][self.col_idx] = mark

        winner = view.check_winner()
        if winner:
            for child in view.children:
                child.disabled = True
            embed = discord.Embed(
                title="🏆 Tic-Tac-Toe — Game Over",
                description=f"{'❌' if winner == 'X' else '⭕'} **{view.current_player.display_name} wins!**",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow()
            )
            await interaction.response.edit_message(embed=embed, view=view)
            return

        if all(view.board[r][c] != "⬜" for r in range(3) for c in range(3)):
            for child in view.children:
                child.disabled = True
            embed = discord.Embed(
                title="🤝 Tic-Tac-Toe — Draw!",
                description="It's a tie! Well played.",
                color=discord.Color.blurple(),
                timestamp=discord.utils.utcnow()
            )
            await interaction.response.edit_message(embed=embed, view=view)
            return

        view.current_player = view.player_o if view.current_player == view.player_x else view.player_x
        embed = discord.Embed(
            title="🎮 Tic-Tac-Toe",
            description=f"It's {view.current_player.mention}'s turn! ({'❌' if view.current_player == view.player_x else '⭕'})",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        await interaction.response.edit_message(embed=embed, view=view)


class TicTacToeView(discord.ui.View):
    def __init__(self, player_x, player_o):
        super().__init__(timeout=120)
        self.player_x = player_x
        self.player_o = player_o
        self.current_player = player_x
        self.board = [["⬜"] * 3 for _ in range(3)]
        for r in range(3):
            for c in range(3):
                self.add_item(TicTacToeButton(r, c))

    def check_winner(self):
        b = self.board
        for row in b:
            if row[0] == row[1] == row[2] != "⬜":
                return row[0]
        for col in range(3):
            if b[0][col] == b[1][col] == b[2][col] != "⬜":
                return b[0][col]
        if b[0][0] == b[1][1] == b[2][2] != "⬜":
            return b[0][0]
        if b[0][2] == b[1][1] == b[2][0] != "⬜":
            return b[0][2]
        return None

@bot.command(aliases=["ttt"])
async def tictactoe(ctx, opponent: discord.Member = None):
    if opponent is None or opponent == ctx.author or opponent.bot:
        await ctx.send("❌ Usage: `,tictactoe @opponent` (must be a real member, not a bot)", delete_after=6)
        return
    view = TicTacToeView(ctx.author, opponent)
    embed = discord.Embed(
        title="🎮 Tic-Tac-Toe",
        description=f"{ctx.author.mention} ❌ vs {opponent.mention} ⭕\n\nIt's {ctx.author.mention}'s turn!",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text="TrapAI Games — 2 minutes to play")
    await ctx.send(embed=embed, view=view)


# ── Connect Four ─────────────────────────────────────────────
# Only 7 buttons (one per column, drop-piece style) rather than 42 —
# Discord views cap at 5 buttons per row / 25 total, so a full 6×7 grid
# of individual cell buttons won't fit. The board itself is rendered as
# emoji text in the embed instead.
class ConnectFourView(discord.ui.View):
    ROWS, COLS = 6, 7

    def __init__(self, player_r, player_y):
        super().__init__(timeout=180)
        self.player_r = player_r
        self.player_y = player_y
        self.current = player_r
        self.board = [[None] * self.COLS for _ in range(self.ROWS)]
        self.message = None
        for col in range(self.COLS):
            self.add_item(self._make_button(col))

    def _render(self):
        symbols = {"R": "🔴", "Y": "🟡", None: "⚪"}
        return "\n".join("".join(symbols[cell] for cell in row) for row in self.board)

    def _drop(self, col):
        for row in range(self.ROWS - 1, -1, -1):
            if self.board[row][col] is None:
                self.board[row][col] = "R" if self.current == self.player_r else "Y"
                return row
        return None

    def _col_full(self, col):
        return self.board[0][col] is not None

    def _is_full(self):
        return all(self.board[0][c] is not None for c in range(self.COLS))

    def _check_winner(self, row, col, mark):
        for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
            count = 1
            r, c = row + dr, col + dc
            while 0 <= r < self.ROWS and 0 <= c < self.COLS and self.board[r][c] == mark:
                count += 1
                r += dr; c += dc
            r, c = row - dr, col - dc
            while 0 <= r < self.ROWS and 0 <= c < self.COLS and self.board[r][c] == mark:
                count += 1
                r -= dr; c -= dc
            if count >= 4:
                return True
        return False

    def _make_button(self, col):
        async def callback(interaction: discord.Interaction):
            if interaction.user != self.current:
                await interaction.response.send_message("❌ It's not your turn!", ephemeral=True)
                return
            row = self._drop(col)
            if row is None:
                await interaction.response.send_message("❌ That column is full.", ephemeral=True)
                return
            mark = "R" if self.current == self.player_r else "Y"
            mark_emoji = "🔴" if mark == "R" else "🟡"

            if self._check_winner(row, col, mark):
                for child in self.children:
                    child.disabled = True
                embed = discord.Embed(
                    title="🏆 Connect Four — Game Over",
                    description=f"{self._render()}\n\n{mark_emoji} **{self.current.display_name} wins!**",
                    color=discord.Color.green(),
                    timestamp=discord.utils.utcnow()
                )
                await interaction.response.edit_message(embed=embed, view=self)
                return

            if self._is_full():
                for child in self.children:
                    child.disabled = True
                embed = discord.Embed(
                    title="🤝 Connect Four — Draw!",
                    description=self._render(),
                    color=discord.Color.blurple(),
                    timestamp=discord.utils.utcnow()
                )
                await interaction.response.edit_message(embed=embed, view=self)
                return

            self.current = self.player_y if self.current == self.player_r else self.player_r
            for i, child in enumerate(self.children):
                child.disabled = self._col_full(i)
            embed = discord.Embed(
                title="🔴🟡 Connect Four",
                description=(
                    f"{self._render()}\n\n"
                    f"It's {self.current.mention}'s turn! ({'🔴' if self.current == self.player_r else '🟡'})"
                ),
                color=discord.Color.blurple(),
                timestamp=discord.utils.utcnow()
            )
            await interaction.response.edit_message(embed=embed, view=self)

        button = discord.ui.Button(label=str(col + 1), style=discord.ButtonStyle.secondary, row=0 if col < 4 else 1)
        button.callback = callback
        return button


@bot.command(aliases=["c4", "connectfour"])
async def connect4(ctx, opponent: discord.Member = None):
    """Challenge another member to Connect Four. Usage: ,connect4 @opponent"""
    if opponent is None or opponent == ctx.author or opponent.bot:
        await ctx.send("❌ Usage: `,connect4 @opponent` (must be a real member, not a bot)", delete_after=6)
        return
    view = ConnectFourView(ctx.author, opponent)
    embed = discord.Embed(
        title="🔴🟡 Connect Four",
        description=(
            f"{view._render()}\n\n"
            f"{ctx.author.mention} 🔴 vs {opponent.mention} 🟡\n\n"
            f"It's {ctx.author.mention}'s turn!"
        ),
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text="TrapAI Games — click a column number to drop your piece")
    view.message = await ctx.send(embed=embed, view=view)


# ── Checkers ─────────────────────────────────────────────────
# Pick-a-piece-then-pick-a-destination dropdowns rather than typed moves
# — an 8×8 board has 32 playable squares, past what a Discord view can
# render as buttons (5 per row / 25 total) but each dropdown only ever
# needs to list ONE player's pieces (≤12) or one piece's destinations
# (≤4), comfortably under the 25-option select-menu limit. Simplified
# ruleset: captures are legal but not mandatory, and only single jumps
# (no forced multi-jump chains) — a deliberate scope cut to keep the
# engine small and fully testable rather than attempting
# tournament-official chaining rules.
def _checkers_square_name(row: int, col: int) -> str:
    return f"{chr(ord('a') + col)}{8 - row}"
class CheckersGame:
    def __init__(self, player_r, player_y):
        self.player_r = player_r  # 🔴 starts on rows 1-3, moves toward row 8
        self.player_y = player_y  # 🟡 starts on rows 6-8, moves toward row 1
        self.current = player_r
        self.board = [[None] * 8 for _ in range(8)]
        for row in range(3):
            for col in range(8):
                if (row + col) % 2 == 1:
                    self.board[row][col] = "y"
        for row in range(5, 8):
            for col in range(8):
                if (row + col) % 2 == 1:
                    self.board[row][col] = "r"

    def render(self) -> str:
        symbols = {"r": "🔴", "R": "🟥", "y": "🟡", "Y": "🟨", None: "⬛"}
        lines = []
        for row in range(8):
            line = ["⬜" if (row + col) % 2 == 0 else symbols[self.board[row][col]] for col in range(8)]
            lines.append("".join(line))
        return "\n".join(lines)

    @staticmethod
    def parse_square(pos: str):
        if len(pos) != 2:
            return None
        col_c, row_c = pos[0].lower(), pos[1]
        if col_c not in "abcdefgh" or row_c not in "12345678":
            return None
        return (8 - int(row_c), ord(col_c) - ord("a"))  # (row, col), row 0 = top ("8")

    @staticmethod
    def _owner(piece):
        if piece is None:
            return None
        return "r" if piece.lower() == "r" else "y"

    @staticmethod
    def _is_king(piece):
        return piece is not None and piece.isupper()

    def _directions(self, piece):
        if self._is_king(piece):
            return [(-1, -1), (-1, 1), (1, -1), (1, 1)]
        return [(-1, -1), (-1, 1)] if piece == "r" else [(1, -1), (1, 1)]

    def _piece_moves(self, row, col, piece):
        moves = []
        owner = self._owner(piece)
        for dr, dc in self._directions(piece):
            r1, c1 = row + dr, col + dc
            if not (0 <= r1 < 8 and 0 <= c1 < 8):
                continue
            if self.board[r1][c1] is None:
                moves.append(((row, col), (r1, c1), False))
            elif self._owner(self.board[r1][c1]) != owner:
                r2, c2 = row + 2 * dr, col + 2 * dc
                if 0 <= r2 < 8 and 0 <= c2 < 8 and self.board[r2][c2] is None:
                    moves.append(((row, col), (r2, c2), True))
        return moves

    def legal_moves_for(self, color):
        moves = []
        for row in range(8):
            for col in range(8):
                piece = self.board[row][col]
                if piece is not None and self._owner(piece) == color:
                    moves.extend(self._piece_moves(row, col, piece))
        return moves

    def has_pieces(self, color):
        return any(self._owner(self.board[r][c]) == color for r in range(8) for c in range(8))

    def has_moves(self, color):
        return len(self.legal_moves_for(color)) > 0

    def try_move(self, color, src, dst):
        """Returns (ok, error_message_or_None, was_capture)."""
        piece = self.board[src[0]][src[1]]
        if piece is None or self._owner(piece) != color:
            return False, "That's not your piece.", False
        match = next((m for m in self._piece_moves(src[0], src[1], piece) if m[1] == dst), None)
        if not match:
            return False, "Illegal move for that piece.", False
        _, _, is_capture = match
        self.board[src[0]][src[1]] = None
        if is_capture:
            mid = ((src[0] + dst[0]) // 2, (src[1] + dst[1]) // 2)
            self.board[mid[0]][mid[1]] = None
        if piece == "r" and dst[0] == 0:
            piece = "R"
        elif piece == "y" and dst[0] == 7:
            piece = "Y"
        self.board[dst[0]][dst[1]] = piece
        return True, None, is_capture


class CheckersView(discord.ui.View):
    PIECE_EMOJI = {"r": "🔴", "R": "🟥", "y": "🟡", "Y": "🟨"}

    def __init__(self, game: CheckersGame, color_of: dict):
        super().__init__(timeout=300)
        self.game = game
        self.color_of = color_of  # {member.id: "r"/"y"}
        self.message = None
        self.origin = None
        self._build_origin_select()

    @property
    def current_color(self) -> str:
        return self.color_of[self.game.current.id]

    def _origin_options(self):
        seen = {}
        for src, _dst, _cap in self.game.legal_moves_for(self.current_color):
            if src not in seen:
                piece = self.game.board[src[0]][src[1]]
                seen[src] = f"{self.PIECE_EMOJI[piece]} {_checkers_square_name(*src)}"
        return [discord.SelectOption(label=label, value=f"{r},{c}") for (r, c), label in seen.items()][:25]

    def _dest_options(self, origin):
        piece = self.game.board[origin[0]][origin[1]]
        options = []
        for _src, dst, is_cap in self.game._piece_moves(origin[0], origin[1], piece):
            label = _checkers_square_name(*dst) + (" ×" if is_cap else "")
            options.append(discord.SelectOption(label=label, value=f"{dst[0]},{dst[1]}"))
        return options[:25]

    def _embed(self, result_text=None):
        e = discord.Embed(
            title="🔴🟡 Checkers",
            color=discord.Color.green() if result_text else discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        e.description = self.game.render()
        e.add_field(name="Players", value=f"🔴 {self.game.player_r.mention}  vs  🟡 {self.game.player_y.mention}", inline=False)
        if result_text:
            e.add_field(name="Result", value=result_text, inline=False)
        else:
            e.add_field(name="Turn", value=f"{self.game.current.mention} ({self.PIECE_EMOJI[self.current_color]})", inline=False)
        e.set_footer(text="TrapAI Games — pick a piece, then a destination")
        return e

    def _build_origin_select(self):
        self.clear_items()
        select = discord.ui.Select(placeholder="Choose a piece to move...", options=self._origin_options())
        select.callback = self._on_origin_selected
        self.add_item(select)
        self._add_end_game_buttons()

    def _build_dest_select(self, origin):
        self.clear_items()
        select = discord.ui.Select(placeholder=f"Move {_checkers_square_name(*origin)} to...", options=self._dest_options(origin))
        select.callback = self._on_dest_selected
        self.add_item(select)
        back_btn = discord.ui.Button(label="« Back", style=discord.ButtonStyle.secondary)
        back_btn.callback = self._on_back
        self.add_item(back_btn)
        self._add_end_game_buttons()

    def _add_end_game_buttons(self):
        resign_btn = discord.ui.Button(label="Resign", emoji="🏳️", style=discord.ButtonStyle.danger)
        resign_btn.callback = self._on_resign
        self.add_item(resign_btn)
        cancel_btn = discord.ui.Button(label="Cancel Game", emoji="🚫", style=discord.ButtonStyle.secondary)
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    async def _guard_turn(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.game.current.id:
            await interaction.response.send_message("❌ It's not your turn!", ephemeral=True)
            return False
        return True

    async def _guard_player(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in (self.game.player_r.id, self.game.player_y.id):
            await interaction.response.send_message("❌ This isn't your game.", ephemeral=True)
            return False
        return True

    async def _on_origin_selected(self, interaction: discord.Interaction):
        if not await self._guard_turn(interaction):
            return
        row, col = (int(x) for x in interaction.data["values"][0].split(","))
        self.origin = (row, col)
        self._build_dest_select(self.origin)
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _on_back(self, interaction: discord.Interaction):
        if not await self._guard_turn(interaction):
            return
        self.origin = None
        self._build_origin_select()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _on_dest_selected(self, interaction: discord.Interaction):
        if not await self._guard_turn(interaction):
            return
        row, col = (int(x) for x in interaction.data["values"][0].split(","))
        dst = (row, col)
        ok, _err, _cap = self.game.try_move(self.current_color, self.origin, dst)
        self.origin = None
        if not ok:
            await interaction.response.send_message("❌ That move is no longer legal.", ephemeral=True)
            self._build_origin_select()
            await interaction.message.edit(embed=self._embed(), view=self)
            return

        mover = self.game.current
        other_color = "y" if self.current_color == "r" else "r"
        other_player = self.game.player_y if mover == self.game.player_r else self.game.player_r
        if not self.game.has_pieces(other_color) or not self.game.has_moves(other_color):
            self.clear_items()
            await interaction.response.edit_message(embed=self._embed(result_text=f"**{mover.display_name} wins!**"), view=self)
            return

        self.game.current = other_player
        self._build_origin_select()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _on_resign(self, interaction: discord.Interaction):
        if not await self._guard_player(interaction):
            return
        resigner = interaction.user
        winner = self.game.player_y if resigner.id == self.game.player_r.id else self.game.player_r
        self.clear_items()
        await interaction.response.edit_message(
            embed=self._embed(result_text=f"{resigner.display_name} resigned. **{winner.display_name} wins!**"),
            view=self
        )

    async def _on_cancel(self, interaction: discord.Interaction):
        if not await self._guard_player(interaction):
            return
        self.clear_items()
        await interaction.response.edit_message(
            embed=self._embed(result_text=f"🚫 Game cancelled by {interaction.user.display_name} — no winner."),
            view=self
        )

    async def on_timeout(self):
        if not self.message:
            return
        self.clear_items()
        try:
            await self.message.edit(content="⏰ Game timed out from inactivity.", view=self)
        except discord.HTTPException:
            pass


@bot.command(aliases=["draughts"])
async def checkers(ctx, opponent: discord.Member = None):
    """
    Challenge another member to Checkers. Pick a piece from the dropdown,
    then pick where to move it — no notation to learn. Simplified rules:
    captures are legal but optional, single jumps only (no forced
    multi-jump chains).
    Usage: ,checkers @opponent
    """
    if opponent is None or opponent == ctx.author or opponent.bot:
        await ctx.send("❌ Usage: `,checkers @opponent` (must be a real member, not a bot)", delete_after=6)
        return

    game = CheckersGame(ctx.author, opponent)
    color_of = {ctx.author.id: "r", opponent.id: "y"}
    view = CheckersView(game, color_of)
    view.message = await ctx.send(embed=view._embed(), view=view)


# ── Chess ────────────────────────────────────────────────────
# Full standard chess rules (castling, en passant, promotion, check/
# checkmate/stalemate/draw detection) via the python-chess library
# rather than a hand-rolled engine — chess's legal-move rules are
# extensive enough that reimplementing them from scratch risks subtle
# rule bugs a well-tested library doesn't have.
try:
    import chess as chess_lib
    CHESS_AVAILABLE = True
except ImportError:
    chess_lib = None
    CHESS_AVAILABLE = False


def _chess_render(board) -> str:
    return f"```\n{board.unicode(borders=False, empty_square='·')}\n```"


_CHESS_PIECE_EMOJI = {
    "P": "♙", "N": "♘", "B": "♗", "R": "♖", "Q": "♕", "K": "♔",
    "p": "♟", "n": "♞", "b": "♝", "r": "♜", "q": "♛", "k": "♚",
}


class ChessView(discord.ui.View):
    def __init__(self, board, players: dict):
        super().__init__(timeout=300)
        self.board = board
        self.players = players  # {chess_lib.WHITE: member, chess_lib.BLACK: member}
        self.message = None
        self.origin = None
        self._build_origin_select()

    @property
    def current_player(self):
        return self.players[self.board.turn]

    def _origin_options(self):
        seen = {}
        for mv in self.board.legal_moves:
            if mv.from_square not in seen:
                piece = self.board.piece_at(mv.from_square)
                emoji = _CHESS_PIECE_EMOJI.get(piece.symbol(), "")
                seen[mv.from_square] = f"{emoji} {chess_lib.square_name(mv.from_square)}"
        options = [discord.SelectOption(label=label, value=chess_lib.square_name(sq)) for sq, label in seen.items()]
        return options[:25]

    def _dest_options(self, origin_square):
        seen = {}
        for mv in self.board.legal_moves:
            if mv.from_square == origin_square:
                label = chess_lib.square_name(mv.to_square)
                if self.board.is_capture(mv):
                    label += " ×"
                seen[mv.to_square] = label
        options = [discord.SelectOption(label=label, value=chess_lib.square_name(sq)) for sq, label in seen.items()]
        return options[:25]

    def _embed(self, result_text=None):
        e = discord.Embed(
            title="♟️ Chess",
            color=discord.Color.green() if result_text else discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        e.description = _chess_render(self.board)
        e.add_field(name="Players", value=f"⚪ {self.players[chess_lib.WHITE].mention}  vs  ⚫ {self.players[chess_lib.BLACK].mention}", inline=False)
        if result_text:
            e.add_field(name="Result", value=result_text, inline=False)
        else:
            check_note = " **Check!**" if self.board.is_check() else ""
            e.add_field(name="Turn", value=f"{self.current_player.mention}{check_note}", inline=False)
        e.set_footer(text="TrapAI Games — pick a piece, then a destination")
        return e

    def _build_origin_select(self):
        self.clear_items()
        select = discord.ui.Select(placeholder="Choose a piece to move...", options=self._origin_options())
        select.callback = self._on_origin_selected
        self.add_item(select)
        self._add_end_game_buttons()

    def _build_dest_select(self, origin_square):
        self.clear_items()
        select = discord.ui.Select(placeholder=f"Move {chess_lib.square_name(origin_square)} to...", options=self._dest_options(origin_square))
        select.callback = self._on_dest_selected
        self.add_item(select)
        back_btn = discord.ui.Button(label="« Back", style=discord.ButtonStyle.secondary)
        back_btn.callback = self._on_back
        self.add_item(back_btn)
        self._add_end_game_buttons()

    def _add_end_game_buttons(self):
        resign_btn = discord.ui.Button(label="Resign", emoji="🏳️", style=discord.ButtonStyle.danger)
        resign_btn.callback = self._on_resign
        self.add_item(resign_btn)
        cancel_btn = discord.ui.Button(label="Cancel Game", emoji="🚫", style=discord.ButtonStyle.secondary)
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    async def _guard_turn(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.current_player.id:
            await interaction.response.send_message("❌ It's not your turn!", ephemeral=True)
            return False
        return True

    async def _guard_player(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in (self.players[chess_lib.WHITE].id, self.players[chess_lib.BLACK].id):
            await interaction.response.send_message("❌ This isn't your game.", ephemeral=True)
            return False
        return True

    async def _on_origin_selected(self, interaction: discord.Interaction):
        if not await self._guard_turn(interaction):
            return
        self.origin = chess_lib.parse_square(interaction.data["values"][0])
        self._build_dest_select(self.origin)
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _on_back(self, interaction: discord.Interaction):
        if not await self._guard_turn(interaction):
            return
        self.origin = None
        self._build_origin_select()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _on_dest_selected(self, interaction: discord.Interaction):
        if not await self._guard_turn(interaction):
            return
        dest_square = chess_lib.parse_square(interaction.data["values"][0])
        candidates = [m for m in self.board.legal_moves if m.from_square == self.origin and m.to_square == dest_square]
        move = next((m for m in candidates if m.promotion == chess_lib.QUEEN), candidates[0] if candidates else None)
        self.origin = None
        if move is None:
            await interaction.response.send_message("❌ That move is no longer legal.", ephemeral=True)
            self._build_origin_select()
            await interaction.message.edit(embed=self._embed(), view=self)
            return

        self.board.push(move)

        outcome = self.board.outcome()
        if outcome is not None:
            self.clear_items()
            if outcome.winner is not None:
                winner = self.players[outcome.winner]
                result = f"♟️ Checkmate! **{winner.display_name} wins!**"
            else:
                reason = outcome.termination.name.replace("_", " ").title()
                result = f"🤝 **Draw** ({reason})"
            await interaction.response.edit_message(embed=self._embed(result_text=result), view=self)
            return

        self._build_origin_select()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _on_resign(self, interaction: discord.Interaction):
        if not await self._guard_player(interaction):
            return
        resigner = interaction.user
        winner = self.players[chess_lib.BLACK] if resigner.id == self.players[chess_lib.WHITE].id else self.players[chess_lib.WHITE]
        self.clear_items()
        await interaction.response.edit_message(
            embed=self._embed(result_text=f"{resigner.display_name} resigned. **{winner.display_name} wins!**"),
            view=self
        )

    async def _on_cancel(self, interaction: discord.Interaction):
        if not await self._guard_player(interaction):
            return
        self.clear_items()
        await interaction.response.edit_message(
            embed=self._embed(result_text=f"🚫 Game cancelled by {interaction.user.display_name} — no winner."),
            view=self
        )

    async def on_timeout(self):
        if not self.message:
            return
        self.clear_items()
        try:
            await self.message.edit(content="⏰ Game timed out from inactivity.", view=self)
        except discord.HTTPException:
            pass


@bot.command(name="chess")
async def play_chess(ctx, opponent: discord.Member = None):
    """
    Challenge another member to Chess — full standard rules via a real
    chess engine (castling, en passant, promotion, check/checkmate/
    stalemate all enforced). Pick a piece from the dropdown, then pick
    where to move it — no notation to learn. Promotions always queen.
    Usage: ,chess @opponent
    """
    if not CHESS_AVAILABLE:
        await ctx.send("❌ Chess isn't available on this bot right now — the `chess` library isn't installed.", delete_after=10)
        return
    if opponent is None or opponent == ctx.author or opponent.bot:
        await ctx.send("❌ Usage: `,chess @opponent` (must be a real member, not a bot)", delete_after=6)
        return

    board = chess_lib.Board()
    players = {chess_lib.WHITE: ctx.author, chess_lib.BLACK: opponent}
    view = ChessView(board, players)
    view.message = await ctx.send(embed=view._embed(), view=view)


# ============================================================
# ,games  — interactive multi-page game directory
# ============================================================

def _games_home_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="🎮  TrapAI Game Center",
        description=(
            "Welcome to the **TrapAI Game Center**!\n"
            "Browse every game and economy command below.\n"
            "Use the buttons to switch between categories.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color=discord.Color.from_rgb(87, 242, 135),
        timestamp=discord.utils.utcnow()
    )
    categories = [
        ("💰", "Economy",        "Earn, save, spend & transfer money",                  "jobs · setjob · work · daily · weekly · balance · deposit · withdraw · give · rob"),
        ("🎰", "Casino",         "Gamble your cash in high-stakes games",               "slots · coinflip · blackjack · dice · crash · highlow"),
        ("🎯", "Fun Games",      "Casual games — no bet needed, cash for winning",      "rps · trivia · hangman · numguess · 8ball · tictactoe"),
        ("🏆", "Leaderboards",   "See who's on top — richest & best gamblers",          "leaderboard · gamblers"),
    ]
    for emoji, name, desc, cmds in categories:
        embed.add_field(
            name=f"{emoji}  {name}",
            value=f"*{desc}*\n`{cmds}`",
            inline=False
        )
    embed.add_field(
        name="⌨️  Prefix",
        value="All commands use `,`  •  e.g. `,slots 100`",
        inline=False
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text=f"TrapAI Game Center  •  {guild.name}  •  Use the buttons below to explore")
    return embed


def _games_economy_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="💰  Economy — Earn & Manage Money",
        description="Build your fortune, save it, spend it, or steal it.\nMoney is stored **per server** — separate on every Discord.",
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(
        name="📊  Check Balance",
        value=(
            "`,balance` / `,bal`\n"
            "Shows your **wallet** (spendable), **bank** (safe) balance, and current job.\n"
            "`,balance @user` — view someone else's balance."
        ),
        inline=False
    )
    embed.add_field(
        name="💼  Get a Job",
        value=(
            "`,jobs` — view the full career ladder, from Cashier up to CEO.\n"
            "`,setjob <name>` / `,job` — start working a job you've unlocked.\n"
            "Higher-paying jobs unlock as you earn more — see `,jobs` for thresholds."
        ),
        inline=False
    )
    embed.add_field(
        name="💵  Earn Money",
        value=(
            "`,work`  ·  ⏳ 20 second cooldown — pay depends on your job (see `,jobs`)\n"
            "`,daily`  **$200–$500**  ·  ⏳ 24 hour cooldown\n"
            "`,weekly`  **$1,000–$2,500**  ·  ⏳ 7 day cooldown"
        ),
        inline=False
    )
    embed.add_field(
        name="🏦  Banking",
        value=(
            "`,deposit <amount|all>` — move money from wallet → bank (safe from robbery)\n"
            "`,withdraw <amount|all>` — move money from bank → wallet (to spend)"
        ),
        inline=False
    )
    embed.add_field(
        name="🤝  Transfer",
        value=(
            "`,give @user <amount>` / `,pay` / `,transfer`\n"
            "Send money directly to another member's wallet."
        ),
        inline=False
    )
    embed.add_field(
        name="🦹  Robbery",
        value=(
            "`,rob @user`  ·  ⏳ 30 minute cooldown\n"
            "**45%** chance to steal up to ⅓ of their wallet.\n"
            "**55%** chance — you get caught and pay a fine!"
        ),
        inline=False
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text=f"TrapAI Economy  •  {guild.name}")
    return embed


def _games_casino_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="🎰  Casino — High Stakes Gambling",
        description="Bet money from your wallet. You can win big — or lose it all.\n⚠️ **Only money in your wallet can be bet** — bank is safe.",
        color=discord.Color.from_rgb(255, 100, 0),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(
        name="🎰  Slots  —  `,slots <bet>`",
        value=(
            "Spin 3 reels and match symbols to win.\n"
            "🍒🍋 = ×2  •  🍊🍇 = ×3  •  🔔 = ×5  •  💎 = ×10  •  7️⃣ = **×20 JACKPOT**\n"
            "Match 2 in a row → get your bet back."
        ),
        inline=False
    )
    embed.add_field(
        name="🪙  Coin Flip  —  `,coinflip <bet> <heads|tails>`",
        value=(
            "50/50 chance to double your bet.\n"
            "Shortcuts: `h` = heads  •  `t` = tails"
        ),
        inline=False
    )
    embed.add_field(
        name="🃏  Blackjack  —  `,blackjack <bet>` / `,bj`",
        value=(
            "Classic blackjack vs the dealer.\n"
            "Reply `h` or `hit` to draw · `s` or `stand` to hold.\n"
            "Dealer draws to 17. Bust = instant loss. Tie = bet returned."
        ),
        inline=False
    )
    embed.add_field(
        name="🎲  Dice  —  `,dice <bet> <1-6>`",
        value=(
            "Guess the exact dice roll.\n"
            "Correct guess → win **×5** your bet!"
        ),
        inline=False
    )
    embed.add_field(
        name="🚀  Crash  —  `,crash <bet>`",
        value=(
            "A multiplier climbs from ×1.00 upwards.\n"
            "Type `cashout` at any time to lock in your winnings.\n"
            "If the rocket crashes before you cash out — you lose everything!"
        ),
        inline=False
    )
    embed.add_field(
        name="🃏  High-Low  —  `,highlow <bet>` / `,hl`",
        value=(
            "A card is drawn (Ace–King). Predict if the next card is **higher** or **lower**.\n"
            "Reply `higher` or `lower` (or `h` / `l`) within 20 seconds.\n"
            "Tie = bet returned."
        ),
        inline=False
    )
    embed.add_field(
        name="⚔️  Duel (PvP)  —  `,duel @user <game> <bet>`",
        value=(
            "Challenge another member directly — winner takes both bets.\n"
            "Supports `coinflip`, `dice`, `rps`, and `blackjack`.\n"
            "They have 60 seconds to accept or decline."
        ),
        inline=False
    )
    embed.add_field(
        name="🏀🏹🏓  Sports Bets",
        value=(
            "`,basketball <bet>` / `,bball` — swish for ×3, make it for ×1.5\n"
            "`,archery <bet>` — bullseye pays ×5, inner ring ×2\n"
            "`,cuppong <bet>` / `,pong` — perfect sink pays ×2.5"
        ),
        inline=False
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text=f"TrapAI Casino  •  {guild.name}  •  Gamble responsibly")
    return embed


def _games_fun_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="🎯  Fun Games — Play & Earn",
        description="No bets required — just play and win money for correct answers!",
        color=discord.Color.from_rgb(88, 101, 242),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(
        name="✊✋✌️  Rock Paper Scissors  —  `,rps <choice>`",
        value=(
            "Beat the bot at rock paper scissors.\n"
            "Choices: `rock` / `paper` / `scissors`  (or `r` / `p` / `s`)\n"
            "No money involved — just glory."
        ),
        inline=False
    )
    embed.add_field(
        name="🧠  Trivia  —  `,trivia`",
        value=(
            "Answer a random question correctly within **30 seconds**.\n"
            "**Reward: $30–$100** for a correct answer!\n"
            "20 questions across science, geography, history & more."
        ),
        inline=False
    )
    embed.add_field(
        name="🪢  Hangman  —  `,hangman`",
        value=(
            "Guess a hidden word one letter at a time.\n"
            "6 wrong guesses allowed before you're hanged.\n"
            "**Reward: $200** for guessing the word!"
        ),
        inline=False
    )
    embed.add_field(
        name="🟩  Wordle  —  `,wordle`",
        value=(
            "Guess the secret 5-letter word in **6 tries**.\n"
            "🟩 right spot • 🟨 wrong spot • ⬛ not in the word\n"
            "**Reward: $100–$350** based on how few guesses you needed!"
        ),
        inline=False
    )
    embed.add_field(
        name="🔢  Number Guess  —  `,numguess` / `,guess`",
        value=(
            "Guess a number between **1 and 100** in 7 attempts.\n"
            "The bot tells you if you're too high or too low.\n"
            "**Reward: $150** for guessing correctly!"
        ),
        inline=False
    )
    embed.add_field(
        name="🎱  Magic 8-Ball  —  `,8ball <question>`",
        value=(
            "Ask the mystical 8-ball anything.\n"
            "Choose your fate: positive · neutral · negative answers."
        ),
        inline=False
    )
    embed.add_field(
        name="🎮  Tic-Tac-Toe  —  `,tictactoe @user` / `,ttt`",
        value=(
            "Challenge another member to a 3×3 Tic-Tac-Toe match.\n"
            "Interactive button board — click to place your mark.\n"
            "2 minutes to finish the game or it times out."
        ),
        inline=False
    )
    embed.add_field(
        name="🔴🟡  Connect Four  —  `,connect4 @user` / `,c4`",
        value=(
            "Challenge another member to Connect Four.\n"
            "Click a column number to drop your piece — get 4 in a row to win.\n"
            "3 minutes to finish the game or it times out."
        ),
        inline=False
    )
    embed.add_field(
        name="🔴🟡  Checkers  —  `,checkers @user` / `,draughts`",
        value=(
            "Challenge another member to Checkers.\n"
            "Pick a piece from the dropdown, then pick where to move it — no notation to learn.\n"
            "Simplified rules — captures optional, single jumps only."
        ),
        inline=False
    )
    embed.add_field(
        name="♟️  Chess  —  `,chess @user`",
        value=(
            "Challenge another member to full-rules Chess.\n"
            "Pick a piece from the dropdown, then pick where to move it — no notation to learn (promotions always queen).\n"
            "Castling, en passant, promotion, and checkmate are all enforced."
        ),
        inline=False
    )
    embed.add_field(
        name="❓  21 Questions  —  `,21questions` / `,21q`",
        value="Drops a random deep/personal question to spark real conversation. No wrong answers, no money involved.",
        inline=False
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text=f"TrapAI Fun Games  •  {guild.name}")
    return embed


def _games_lb_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="🏆  Leaderboards",
        description="See who's on top across all economy and casino activity.",
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(
        name="💎  Richest Members  —  `,leaderboard` / `,lb`",
        value=(
            "Top 10 members ranked by **wallet + bank** total.\n"
            "Earn money via `,work`, `,daily`, `,weekly`, and casino wins."
        ),
        inline=False
    )
    embed.add_field(
        name="🎰  Top Gamblers  —  `,gamblers`",
        value=(
            "Top 10 members ranked by **net casino winnings**.\n"
            "Shows total money won minus money lost across all casino games.\n"
            "Negative values mean they're in the red — a true degenerate."
        ),
        inline=False
    )
    embed.add_field(
        name="📈  How to climb",
        value=(
            "• Use `,daily` and `,weekly` every reset\n"
            "• Win big in `,slots` or `,blackjack`\n"
            "• Rob from others with `,rob`\n"
            "• Answer `,trivia` for free money"
        ),
        inline=False
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text=f"TrapAI Leaderboards  •  {guild.name}")
    return embed


class GamesMenuView(discord.ui.View):
    def __init__(self, author_id: int, guild: discord.Guild):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.guild = guild

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Open your own `,games` menu to browse.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Home",        style=discord.ButtonStyle.secondary, emoji="🏠", row=0)
    async def btn_home(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.edit_message(embed=_games_home_embed(self.guild), view=self)

    @discord.ui.button(label="Economy",     style=discord.ButtonStyle.primary,   emoji="💰", row=0)
    async def btn_economy(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.edit_message(embed=_games_economy_embed(self.guild), view=self)

    @discord.ui.button(label="Casino",      style=discord.ButtonStyle.danger,    emoji="🎰", row=0)
    async def btn_casino(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.edit_message(embed=_games_casino_embed(self.guild), view=self)

    @discord.ui.button(label="Fun Games",   style=discord.ButtonStyle.success,   emoji="🎯", row=0)
    async def btn_fun(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.edit_message(embed=_games_fun_embed(self.guild), view=self)

    @discord.ui.button(label="Leaderboards", style=discord.ButtonStyle.primary,  emoji="🏆", row=0)
    async def btn_lb(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.edit_message(embed=_games_lb_embed(self.guild), view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


@bot.command(aliases=["gamelist", "gamemenu"])
async def games(ctx):
    """Show the interactive game center with all commands."""
    view = GamesMenuView(ctx.author.id, ctx.guild)
    await ctx.send(embed=_games_home_embed(ctx.guild), view=view)


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise SystemExit(
        "DISCORD_TOKEN is not set. Add it to a .env file in the project root:\n"
        "DISCORD_TOKEN=your-bot-token-here"
    )

# _autosave_loop() only flushes every 30s — a redeploy/restart landing
# between ticks would otherwise lose up to that much fresh activity (VC
# time, chat stats, etc.) even with a persistent disk. bot.run() catches
# SIGINT/SIGTERM internally and returns cleanly instead of raising, so this
# finally block is what actually gets the LAST few seconds of state saved
# the moment a shutdown/redeploy begins, regardless of how it was triggered.
def _run_bot_with_state_flush():
    try:
        bot.run(DISCORD_TOKEN)
    finally:
        _save_all_state()


_run_bot_with_state_flush()
