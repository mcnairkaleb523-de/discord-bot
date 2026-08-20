from dotenv import load_dotenv
load_dotenv()

import os
import sys
import io
import re
import time
import asyncio
import json
import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timedelta
from collections import deque

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True
intents.invites = True
intents.presences = True

bot = commands.Bot(command_prefix=",", intents=intents)

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


def _save_guild_invite():
    _save_data("guild_invite", _dump_depth(GUILD_INVITE, 1))


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
    _save_mod_history()
    _save_tickets()
    _save_guild_ticket_types()
    _save_guild_ticket_formats()
    _save_vouches()
    _save_vouch_log()
    _save_vouch_config()
    _save_role_vouch_pending()
    _save_protected_roles()
    _save_autorole()
    _save_hard_banned()
    _save_role_snapshots()
    _save_jail_role_snapshots()
    _save_guild_invite()
    _save_gif_exempt_role()
    _save_vanity_role()
    _save_vanity_code_override()
    _save_polls()
    _save_raid_whitelist()
    _save_anti_raid_enabled()
    _save_invite_data()
    _save_verify_backup_guild()
    _save_unmute_vc_channels()
    _save_welcome_config()
    _save_boost_channel()
    _save_booster_roles()
    _save_economy()
    _save_cooldowns()
    _save_gamble_wins()
    _save_jail_expiry()
    _save_milestones()
    _save_tasks()
    _save_giveaways()
    _save_chat_stats()
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
VERIFIED_ROLE = "✅ Ballin Member"
JAIL_ROLE = "🔒 Jailed"
MUTED_ROLE = "🔇 Muted"

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


def _save_raid_whitelist():
    _save_data("raid_whitelist", {str(gid): list(uids) for gid, uids in WHITELIST.items()})


def _save_anti_raid_enabled():
    _save_data("anti_raid_enabled", _dump_depth(ANTI_RAID_ENABLED, 1))

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


def _save_temp_vcs():
    _save_data("temp_vc_owners", _dump_depth(temp_vc_owners, 1))
    _save_data("temp_vc_text_channels", _dump_depth(temp_vc_text_channels, 1))
    _save_data("vc_banned", {str(vid): list(users) for vid, users in vc_banned.items()})
    _save_data("vc_mods", {str(vid): list(users) for vid, users in vc_mods.items()})

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


# WARNINGS[guild_id][user_id] = [ {reason, moderator, moderator_id, time}, ... ]
WARNINGS = _load_warnings()

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

# ── Invite tracking ────────────────────────────────────────
# INVITE_CACHE[guild_id] = { code: uses } — session-only, re-derived from
# the guild's live invites on_ready, doesn't need persistence.
INVITE_CACHE: dict[int, dict[str, int]] = {}
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
NUKE_WINDOW       = 10  # seconds

# ── Server invite config ─────────────────────────────────────
# GUILD_INVITE[guild_id] = "https://discord.gg/..."
GUILD_INVITE: dict[int, str] = _load_depth(_load_data("guild_invite", {}), 1)

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
# VANITY_ROLE[guild_id] = role_id — role granted while a member's custom
# status contains this server's vanity invite link.
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


def _status_has_vanity(member: discord.Member, code: str) -> bool:
    if not code:
        return False
    target = f"discord.gg/{code}".lower()
    for act in member.activities:
        name = getattr(act, "name", None)
        if name and target in name.lower():
            return True
    return False

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
    "timeout":  ("⏳ You have been timed out",       discord.Color.gold(),     "⏳", "Timed out in"),
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
    invite = GUILD_INVITE.get(guild.id, "")

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
        file, msg_count = await _build_ticket_transcript(channel)
        await interaction.followup.send(
            content=f"📄 Transcript for **{channel.name}** (`{msg_count}` messages):",
            file=file,
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
            color=discord.Color.gold(),
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


async def _build_ticket_transcript(channel) -> tuple[discord.File, int]:
    """Render a ticket channel's message history into a downloadable .txt transcript."""
    lines = []
    async for msg in channel.history(limit=500, oldest_first=True):
        ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
        content = msg.content or ""
        embeds_note = f" [+{len(msg.embeds)} embed(s)]" if msg.embeds else ""
        lines.append(f"[{ts}] {msg.author} ({msg.author.id}): {content}{embeds_note}")
    text = "\n".join(lines) or "(no messages)"
    filename = f"transcript-{channel.name}.txt"
    file = discord.File(fp=io.BytesIO(text.encode()), filename=filename)
    return file, len(lines)


async def _send_closed_ticket_transcript(guild, channel, closer, owner_id, claimer, ticket_type):
    """Post the full transcript to the ticket log channel when a ticket closes."""
    log_channel = _resolve_log_channel(guild, "tickets")
    if not log_channel:
        return
    try:
        file, msg_count = await _build_ticket_transcript(channel)
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
    try:
        await log_channel.send(embed=embed, file=file)
    except (discord.Forbidden, discord.HTTPException):
        pass


def _split_emoji_label(label: str):
    """Split a 'EMOJI Rest of label' string into (emoji, rest)."""
    parts = label.split(" ", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
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

        # Duplicate check
        guild_tickets = TICKETS.get(guild.id, {})
        if member.id in guild_tickets:
            existing = guild.get_channel(guild_tickets[member.id])
            if existing:
                await interaction.response.send_message(
                    f"❌ You already have an open ticket: {existing.mention}",
                    ephemeral=True
                )
                return

        await interaction.response.defer(ephemeral=True, thinking=True)
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
        await vc.set_permissions(interaction.guild.default_role, connect=False)
        embed = discord.Embed(description="🔒 VC **locked** — only permitted users can join.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await _vc_announce(interaction.guild, vc, f"🔒 **{interaction.user.display_name}** locked the VC.")

    @discord.ui.button(label="🔓 Unlock", style=discord.ButtonStyle.success, custom_id="vc_btn_unlock", row=0)
    async def btn_unlock(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._check(interaction)
        if not vc:
            return
        await vc.set_permissions(interaction.guild.default_role, connect=True)
        embed = discord.Embed(description="🔓 VC **unlocked** — anyone can join.", color=discord.Color.green())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await _vc_announce(interaction.guild, vc, f"🔓 **{interaction.user.display_name}** unlocked the VC.")

    @discord.ui.button(label="👻 Hide", style=discord.ButtonStyle.secondary, custom_id="vc_btn_hide", row=0)
    async def btn_hide(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._check(interaction)
        if not vc:
            return
        await vc.set_permissions(interaction.guild.default_role, view_channel=False)
        embed = discord.Embed(description="👻 VC **hidden** from everyone.", color=discord.Color.dark_grey())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await _vc_announce(interaction.guild, vc, f"👻 **{interaction.user.display_name}** hid the VC.")

    @discord.ui.button(label="👀 Show", style=discord.ButtonStyle.secondary, custom_id="vc_btn_show", row=0)
    async def btn_show(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._check(interaction)
        if not vc:
            return
        await vc.set_permissions(interaction.guild.default_role, view_channel=True)
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
# MODALS — one per button action that needs input
# ============================================================

class VCRenameModal(discord.ui.Modal, title="✏️ Rename VC"):
    new_name = discord.ui.TextInput(label="New name", placeholder="e.g. Ballin Hangout", min_length=1, max_length=100)

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
        await self.vc.set_permissions(member, connect=True, view_channel=True)
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
        if not member.voice or member.voice.channel != self.vc:
            await interaction.response.send_message("❌ That user is not in your VC.", ephemeral=True)
            return
        await member.move_to(None)
        await interaction.response.send_message(f"👢 **{member.display_name}** was kicked from the VC.", ephemeral=True)
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
        if _is_vc_owner(member, self.vc):
            await interaction.response.send_message("❌ You can't ban the VC owner.", ephemeral=True)
            return
        await self.vc.set_permissions(member, connect=False, view_channel=False)
        vc_banned.setdefault(self.vc.id, set()).add(member.id)
        if member.voice and member.voice.channel == self.vc:
            await member.move_to(None)
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
        await self.vc.set_permissions(member, overwrite=None)
        vc_banned.get(self.vc.id, set()).discard(member.id)
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
        if not member.voice or member.voice.channel != self.vc:
            await interaction.response.send_message("❌ That user must be in the VC.", ephemeral=True)
            return

        old_owner_id = temp_vc_owners.get(self.vc.id)
        old_owner = self.guild.get_member(old_owner_id) if old_owner_id else None

        temp_vc_owners[self.vc.id] = member.id
        await self.vc.set_permissions(member, manage_channels=True, manage_permissions=True, move_members=True, connect=True, speak=True)
        # Revoke the previous owner's channel-admin overwrite (manage_channels/
        # manage_permissions/move_members) — otherwise they'd keep full native
        # Discord control (rename, delete, edit overwrites, move anyone) even
        # though the bot now considers someone else the owner.
        if old_owner and old_owner.id != member.id:
            try:
                await self.vc.set_permissions(old_owner, overwrite=None)
            except (discord.Forbidden, discord.HTTPException):
                pass
        # Grant new owner text access
        text_id = temp_vc_text_channels.get(self.vc.id)
        if text_id:
            text_ch = self.guild.get_channel(text_id)
            if text_ch:
                await text_ch.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
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
        if not member.voice or member.voice.channel != self.vc:
            await interaction.response.send_message("❌ That user must be in the VC.", ephemeral=True)
            return
        vc_mods.setdefault(self.vc.id, set()).add(member.id)
        await self.vc.set_permissions(member, move_members=True, mute_members=True, deafen_members=True, manage_channels=True)
        await interaction.response.send_message(f"🛡 **{member.display_name}** is now a VC moderator.", ephemeral=True)
        await _vc_announce(interaction.guild, self.vc, f"🛡 **{interaction.user.display_name}** made **{member.display_name}** a VC moderator.")


# ============================================================
# CMDS VIEW  — full command reference with every command
# ============================================================

_CMDS_DIVIDER = "────────────────────────────"

class CmdsView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=180)
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ You can't use someone else's command menu.",
                ephemeral=True
            )
            return False
        return True

    # ─────────────────────────────────────────────────────────
    # HOME
    # ─────────────────────────────────────────────────────────
    def home_embed(self, guild: discord.Guild):
        embed = discord.Embed(
            color=discord.Color.from_rgb(88, 101, 242),
            timestamp=discord.utils.utcnow()
        )
        embed.set_author(
            name="TrapAI Command Center",
            icon_url=guild.icon.url if guild.icon else None
        )
        embed.description = (
            f"**{guild.name}** — full bot command reference.\n"
            "Use the buttons below to browse every category.\n\n"
            f"{_CMDS_DIVIDER}"
        )
        cats = [
            ("🛡", "Moderation",  "kick, ban, mute, timeout, warn, clear, purge…"),
            ("🔒", "Jail",        "jail, unjail with auto-timer"),
            ("🤖", "Security",    "verify, unverify, trapwarn, trapscan…"),
            ("📊", "Stats",       "whois, chatstats, serverstats, invites…"),
            ("🏷️", "Roles",       "role add/remove/create/delete/info/color…"),
            ("🎤", "VC",          "vclock, vcname, vckick, vcban, vctransfer…"),
            ("🎉", "Giveaways",   "giveaway, giveawayend, giveaways"),
            ("✅", "Vouch",       "vouch, protectedrole, unvouch, vouches, vouchleaderboard…"),
            ("📋", "Staff",       "staffpsa, task, tasklist"),
            ("⚙️", "Admin",       "setup, setupjail, setlogchannel, backup, hardban…"),
            ("🎮", "Games",        "slots, coinflip, blackjack, rps, trivia, bal, work…"),
        ]
        lines = "\n".join(f"> {e} **{n}** — *{d}*" for e, n, d in cats)
        embed.add_field(name="📂 Categories", value=lines, inline=False)
        embed.add_field(
            name="⌨️ Prefix",
            value="All commands use `,` prefix  •  e.g. `,ban @user`",
            inline=False
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text=f"TrapAI • {guild.name}  •  {guild.member_count:,} members")
        return embed

    # ─────────────────────────────────────────────────────────
    # MODERATION
    # ─────────────────────────────────────────────────────────
    def moderation_embed(self, guild: discord.Guild):
        embed = discord.Embed(
            title="🛡 Moderation Commands",
            color=discord.Color.from_rgb(237, 66, 69),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="⚔️ Punishment",
            value=(
                "`,kick @user [reason]` — kick a member\n"
                "`,ban @user [reason]` — ban a member\n"
                "`,timeout @user <mins> [reason]` — Discord timeout\n"
                "`,mute @user [reason]` — apply mute role\n"
                "`,unmute @user [reason]` — remove mute role\n"
                "`,warn @user [reason]` — log a warning\n"
                "`,warnings [@user]` — view warnings\n"
                "`,clearwarnings @user` — clear all warnings\n"
                "`,modhistory [@user] [action]` — full mod history: warn/jail/ban/kick/timeout/mute/hardban/strip"
            ),
            inline=False
        )
        embed.add_field(
            name="🔨 Hard-Ban",
            value=(
                "`,hardban @user <reason>` — permanent ban + rejoin block\n"
                "`,unhardban <user_id> [reason]` — lift hard-ban\n"
                "`,hardbans` — list all hard-banned users"
            ),
            inline=False
        )
        embed.add_field(
            name="📺 Channel Management",
            value=(
                "`,clear <amount>` — delete N messages (incl. command)\n"
                "`,purge <amount> [@user]` — targeted message purge\n"
                "`,lock` — prevent @everyone from sending\n"
                "`,unlock` — restore send permissions\n"
                "`,hide` — hide channel from @everyone\n"
                "`,unhide` — make channel visible again\n"
                "`,slowmode <secs>` — set channel slowmode (0 = off)\n"
                "`,nuke` — clone + delete channel\n"
                "`,lockdown` — lock ALL text channels\n"
                "`,unlockdown` — unlock ALL text channels"
            ),
            inline=False
        )
        embed.add_field(
            name="👤 Member Management",
            value=(
                "`,nickname @user [nick]` — set/clear nickname\n"
                "`,restart` — restart the bot (admin only)"
            ),
            inline=False
        )
        embed.add_field(
            name="📨 DM Notifications",
            value=(
                "Auto-DMs the user when: `ban` `kick` `jail` `timeout` `hardban`\n"
                "DM contains: action, reason, moderator, server invite link."
            ),
            inline=False
        )
        embed.add_field(
            name="🔗 Invite Link",
            value=(
                "`,setinvite <link>` — set server invite used in DMs\n"
                "`,setinvite` — view current invite\n"
                "`,sendinvite @user [msg]` — manually DM invite to anyone"
            ),
            inline=False
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text=f"TrapAI • {guild.name}  •  Moderation")
        return embed

    # ─────────────────────────────────────────────────────────
    # JAIL
    # ─────────────────────────────────────────────────────────
    def jail_embed(self, guild: discord.Guild):
        embed = discord.Embed(
            title="🔒 Jail System",
            color=discord.Color.from_rgb(180, 0, 0),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="Commands",
            value=(
                "`,jail @user <time> [reason]` — jail with auto-release timer\n"
                "`,unjail @user [reason]` — release early"
            ),
            inline=False
        )
        embed.add_field(
            name="⏱️ Time Formats",
            value=(
                "`30s` `10m` `2h` `3d` `1w` `1mo` `1y`\n"
                "*Examples: `,jail @user 1h spamming` • `,jail @user 3d ban appeal`*"
            ),
            inline=False
        )
        embed.add_field(
            name="ℹ️ How it works",
            value=(
                "• Removes Verified + Unverified roles\n"
                "• Applies `🔒 Jailed` role\n"
                "• Hides all channels except `#jail`\n"
                "• Auto-unjails when timer expires\n"
                "• DMs the user with reason + duration"
            ),
            inline=False
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text=f"TrapAI • {guild.name}  •  Jail System")
        return embed

    # ─────────────────────────────────────────────────────────
    # SECURITY
    # ─────────────────────────────────────────────────────────
    def security_embed(self, guild: discord.Guild):
        embed = discord.Embed(
            title="🤖 TrapAI Security",
            color=discord.Color.from_rgb(87, 242, 135),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="✅ Verification",
            value=(
                "`,sendverify` — post the verify button panel\n"
                "`,verify @user` — manually verify a member\n"
                "`,unverify @user` — remove verified role\n"
                "`,denyverify @user [reason]` — block verification"
            ),
            inline=False
        )
        embed.add_field(
            name="🔍 Threat Detection",
            value=(
                "`,trapwarn @user [reason]` — TrapAI flag a user\n"
                "`,trapscan @user` — full threat analysis report"
            ),
            inline=False
        )
        embed.add_field(
            name="🚨 Anti-Nuke (Automatic)",
            value=(
                f"Auto-strips roles if any non-admin deletes:\n"
                f"• **{NUKE_ROLE_LIMIT}+ roles** within **{NUKE_WINDOW}s**\n"
                f"• **{NUKE_CHAN_LIMIT}+ channels** within **{NUKE_WINDOW}s**\n"
                "Logs to mod channel with full details."
            ),
            inline=False
        )
        embed.add_field(
            name="🛡 Anti-Raid (Automatic)",
            value=(
                f"Auto-bans if **{RAID_LIMIT}+ joins** happen within **{RAID_TIME}s**.\n"
                "New arrivals are checked against the whitelist."
            ),
            inline=False
        )
        embed.add_field(
            name="🔇 Anti-Spam (Automatic)",
            value=(
                f"Warns after **{SPAM_MESSAGE_LIMIT} messages** in **{SPAM_TIME_WINDOW}s**.\n"
                f"Auto-timeout after **{SPAM_WARNING_LIMIT} warnings**."
            ),
            inline=False
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text=f"TrapAI • {guild.name}  •  Security")
        return embed

    # ─────────────────────────────────────────────────────────
    # STATS
    # ─────────────────────────────────────────────────────────
    def stats_embed(self, guild: discord.Guild):
        embed = discord.Embed(
            title="📊 Stats & Info Commands",
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="👤 User Stats",
            value=(
                "`,whois [@user]` — full profile: roles, join date, chat/vc stats\n"
                "`,chatstats [@user]` — personal rank card (or `,chatstats leaderboard`)\n"
                "`,vcstats [@user]` — personal VC time rank card (or `,vcstats leaderboard`)\n"
                "`,ping` — bot latency"
            ),
            inline=False
        )
        embed.add_field(
            name="🏠 Server Stats",
            value=(
                "`,serverstats` (alias `,ss`) — 5-page interactive report\n"
                "🏠 Overview · 👥 Members · 💬 Channels · 🚀 Boosts · 🏆 Leaderboards"
            ),
            inline=False
        )
        embed.add_field(
            name="📨 Invite Tracking",
            value=(
                "`,invites [@user]` — how many people a user has invited\n"
                "`,inviteleaderboard` — top inviters server-wide\n"
                "`,invitelogs [@user]` — full join history per invite code"
            ),
            inline=False
        )
        embed.add_field(
            name="💬 Quote",
            value=(
                "`,quote` — reply to a message to quote it as a card\n"
                "`,quote <message link>` — quote from a link\n"
                "`,quote <message id>` — quote by ID in current channel\n"
                '`,quote "text" @user` — custom attributed quote card'
            ),
            inline=False
        )
        embed.add_field(
            name="🎂 Birthdays",
            value=(
                "`,setbirthday <month-day>` — set your birthday (e.g. `03-15` or `March 15`)\n"
                "`,settimezone <state or offset>` — e.g. `California`, `TX`, or `-5`, `+8`, `UTC+5:30`\n"
                "`,removebirthday` — remove your saved birthday\n"
                "`,birthday [@user]` — view a birthday + timezone\n"
                "`,birthdaylist` — upcoming birthdays in the server\n"
                "`,setbirthdaychannel [#channel]` — admin: set announcement channel\n"
                "*Announced automatically at YOUR local midnight (defaults to UTC if `,settimezone` isn't set).*"
            ),
            inline=False
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text=f"TrapAI • {guild.name}  •  Stats")
        return embed

    # ─────────────────────────────────────────────────────────
    # ROLES
    # ─────────────────────────────────────────────────────────
    def roles_embed(self, guild: discord.Guild):
        embed = discord.Embed(
            title="🏷️ Role Commands",
            color=discord.Color.from_rgb(114, 137, 218),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="👤 Per-Member",
            value=(
                "`,role add @user @role` — give a role\n"
                "`,role remove @user @role` — take a role\n"
                "`,role user [@user]` — list all of a user's roles"
            ),
            inline=False
        )
        embed.add_field(
            name="🔧 Role Administration",
            value=(
                "`,role create <name> [#hex] [hoist]` — create a role\n"
                "`,role delete @role` — delete a role\n"
                "`,role color @role #hex` — change role colour\n"
                "`,role hoist @role` — toggle hoisting on/off\n"
                "`,role info @role` — detailed role info\n"
                "`,role list` — list all server roles\n"
                "`,role members @role` — who has this role"
            ),
            inline=False
        )
        embed.add_field(
            name="📦 Bulk Role Tools",
            value=(
                "`,roleall @role` — give role to every member\n"
                "`,massrole @role [@filter]` — give role to filtered members\n"
                "`,massunrole @role [@filter]` — remove role from filtered members\n"
                "`,strip @user` — remove all staff roles + save snapshot\n"
                "`,restoreallroles @user` — restore snapshot from `,strip`"
            ),
            inline=False
        )
        embed.add_field(
            name="🌟 Booster Role (self-service)",
            value=(
                "`,br` — show your custom booster role\n"
                "`,br create <name>` — create it (boosters only)\n"
                "`,br name <new name>` — rename it\n"
                "`,br color <hex>` — recolor it\n"
                "`,br delete` — remove it"
            ),
            inline=False
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text=f"TrapAI • {guild.name}  •  Roles")
        return embed

    # ─────────────────────────────────────────────────────────
    # VC
    # ─────────────────────────────────────────────────────────
    def vc_embed(self, guild: discord.Guild):
        embed = discord.Embed(
            title="🎤 Private VC Commands",
            description="Use commands **or** the button panel in your VC text channel.  👑 = owner only.",
            color=discord.Color.dark_grey(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="🔒 Privacy",
            value=(
                "`,vclock` — lock VC (no new joins)\n"
                "`,vcunlock` — unlock VC\n"
                "`,vchide` — hide VC from everyone\n"
                "`,vcshow` — make VC visible"
            ),
            inline=True
        )
        embed.add_field(
            name="⚙️ Channel Settings",
            value=(
                "`,vcname <name>` — rename the VC\n"
                "`,vclimit <0-99>` — set user limit\n"
                "`,vcbitrate <kbps>` — set bitrate\n"
                "`,vcregion <region>` — set voice region"
            ),
            inline=True
        )
        embed.add_field(
            name="👥 Member Access",
            value=(
                "`,vcpermit @user` — whitelist a user\n"
                "`,vckick @user` — kick from VC\n"
                "`,vcban @user` — ban from VC\n"
                "`,vcunban @user` — unban from VC\n"
                "`,vcmute @user` — server-mute\n"
                "`,vcunmute @user` — server-unmute\n"
                "`,vcdeafen @user` — server-deafen\n"
                "`,vcundeafen @user` — undeafen"
            ),
            inline=False
        )
        embed.add_field(
            name="👑 Ownership",
            value=(
                "`,vctransfer @user` 👑 — transfer ownership\n"
                "`,vcclaim` 👑 — claim ownership if the owner left\n"
                "`,vcmod @user` 👑 — add a VC moderator\n"
                "`,vcremovemod @user` 👑 — remove VC mod"
            ),
            inline=False
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text=f"TrapAI • {guild.name}  •  Private VCs")
        return embed

    # ─────────────────────────────────────────────────────────
    # GIVEAWAYS
    # ─────────────────────────────────────────────────────────
    def giveaway_embed(self, guild: discord.Guild):
        embed = discord.Embed(
            title="🎉 Giveaway Commands",
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="Commands",
            value=(
                "`,giveaway <duration> <winners> <prize>`\n"
                "> Start a giveaway with a real countdown timer\n"
                "> *e.g.* `,giveaway 24h 1 Discord Nitro`\n\n"
                "`,giveawayend [message_id]` — force-end early\n"
                "`,giveaways` — list all active giveaways"
            ),
            inline=False
        )
        embed.add_field(
            name="⏱️ Duration Formats",
            value="`30s` `10m` `2h` `3d`  *(minimum 10s)*",
            inline=False
        )
        embed.add_field(
            name="🎟️ How it works",
            value=(
                "• Click **🎉 Enter Giveaway** to join, click again to leave\n"
                "• Click **👥 Entries** to see current count\n"
                "• Winners are drawn randomly when the timer ends\n"
                "• Winners are announced + mentioned in channel"
            ),
            inline=False
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text=f"TrapAI • {guild.name}  •  Giveaways")
        return embed

    # ─────────────────────────────────────────────────────────
    # VOUCH
    # ─────────────────────────────────────────────────────────
    def vouch_embed(self, guild: discord.Guild):
        embed = discord.Embed(
            title="✅ Vouch System",
            color=discord.Color.from_rgb(87, 242, 135),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="✅ Standard Vouch",
            value=(
                "`,vouch @user [reason]` — vouch for a member\n"
                "`,unvouch @user [reason]` — remove a vouch\n"
                "`,vouches [@user]` — profile with progress bar, trust badge & history\n"
                "`,vouchleaderboard` — top vouched members server-wide\n"
                "`,vouchconfig threshold <N>` — set required vouches (admin)"
            ),
            inline=False
        )
        embed.add_field(
            name="🔒 Protected Role Vouch",
            value=(
                "`,vouch @user @role [reason]` — request a protected role for a member\n"
                "  → Owner gets a DM with full member info + ✅ Approve / ❌ Reject buttons\n"
                "  → Member is DM'd the outcome with the owner's decision note\n"
                "  → Requester is notified when their request is actioned\n\n"
                "`,protectedrole list` — see all protected roles & holder counts\n"
                "`,protectedrole add @role` — lock a role (admin only)\n"
                "`,protectedrole remove @role` — unlock a role (admin only)"
            ),
            inline=False
        )
        embed.add_field(
            name="📋 Pending & Tracking",
            value=(
                "`,pendingvouches` — view all open vouch-role requests (admin)\n"
                "`,cancelvouch @user @role` — withdraw a pending request (admin)\n"
                "`,vouchstats` — server-wide analytics: approval rate, top vouchers, strip count"
            ),
            inline=False
        )
        threshold     = VOUCH_CONFIG.get(guild.id, {}).get("threshold", 3)
        protected_count = len(PROTECTED_ROLES.get(guild.id, set()))
        embed.add_field(
            name="⚙️ Current Config",
            value=(
                f"Threshold: **{threshold}** vouches required\n"
                f"Protected roles: **{protected_count}** configured\n"
                f"Pending requests: **{len(ROLE_VOUCH_PENDING.get(guild.id, {}))}** awaiting owner"
            ),
            inline=False
        )
        embed.add_field(
            name="ℹ️ How protected roles work",
            value=(
                "• Manual grants of protected roles are **instantly auto-stripped**\n"
                "• The member & the granter both get a DM explaining it was blocked\n"
                "• Staff use `,vouch @user @role reason` to submit a proper request\n"
                "• Server **owner** gets a full context DM — ✅ Approve / ❌ Reject\n"
                "• Owner can add a **note** when actioning — sent in the member's DM\n"
                "• Requester is also notified of the final decision"
            ),
            inline=False
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text=f"TrapAI • {guild.name}  •  Vouch System")
        return embed

    # ─────────────────────────────────────────────────────────
    # STAFF TOOLS
    # ─────────────────────────────────────────────────────────
    def staff_embed(self, guild: discord.Guild):
        embed = discord.Embed(
            title="📋 Staff Tools",
            color=discord.Color.from_rgb(254, 231, 92),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="📢 Announce",
            value=(
                "`,announce [#channel] <message>` — send a rich announcement embed\n"
                "`,ann` is a shortcut alias\n\n"
                "**Optional flags** (separate with `|`):\n"
                "`--title <text>` — set a title\n"
                "`--color <name/hex>` — red green blue gold purple teal orange pink\n"
                "`--image <url>` — attach a large image\n"
                "`--ping everyone/here` — ping @everyone or @here\n\n"
                "*e.g.* `,announce #announcements --title 🔥 Update | --ping here | New features are live!`"
            ),
            inline=False
        )
        embed.add_field(
            name="📢 Staff PSA",
            value=(
                "`,staffpsa <type> <message>` — broadcast a styled staff announcement\n\n"
                "**Types:** `info` `warning` `urgent` `critical` `update` `rules` `shutdown` `reminder`\n"
                "• `urgent` pings `@here`  •  `critical` pings `@everyone`\n"
                "• Each PSA has a **✅ Got it** acknowledge button\n"
                "*e.g.* `,staffpsa urgent Raid incoming — lock all channels`"
            ),
            inline=False
        )
        embed.add_field(
            name="📋 Task Board",
            value=(
                "`,task <priority> [@user] <title> [— description]`\n"
                "> Create a task card with interactive status buttons\n"
                "> *e.g.* `,task high @mod Fix verify — test on mobile`\n\n"
                "**Priorities:** `low` `medium` `high` `critical`\n\n"
                "**Task Buttons:**\n"
                "⚙️ In Progress  •  🔍 Review  •  ✅ Done\n"
                "🚫 Blocked  •  📋 Reopen  •  💬 Add Note  •  👤 Reassign  •  🗑️ Delete\n\n"
                "`,tasklist [status|@user]` — view/filter the task board\n"
                "> *e.g.* `,tasklist blocked` • `,tasklist done` • `,tasklist @mod`"
            ),
            inline=False
        )
        embed.add_field(
            name="🏘️ Welcome",
            value=(
                "`,welcome [@user]` — re-send welcome card for a member\n"
                "`,sendwelcome @user` — send welcome to a specific user\n"
                "`,disablewelcome` — disable auto-welcome for new members"
            ),
            inline=False
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text=f"TrapAI • {guild.name}  •  Staff Tools")
        return embed

    # ─────────────────────────────────────────────────────────
    # ADMIN
    # ─────────────────────────────────────────────────────────
    def admin_embed(self, guild: discord.Guild):
        embed = discord.Embed(
            title="⚙️ Admin Commands",
            color=discord.Color.from_rgb(88, 101, 242),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="🔧 Server Setup",
            value=(
                "`,setup` — create all server channels & roles\n"
                "`,setupvc [category]` — set up the private VC system\n"
                "`,setupjail [#channel]` — set up the jail system & restricted category\n"
                "`,autorole` — view auto-roles given to every new member\n"
                "`,autorole add @role` — add a join role\n"
                "`,autorole remove @role` — remove a join role\n"
                "`,autorole clear` — clear all join roles\n"
                "`,rules` — post the server rules embed\n"
                "`,sendverify` — post the verify button panel\n"
                "`,sendtickets` — post the ticket panel with dropdown\n"
                "`,restart` — restart TrapAI"
            ),
            inline=False
        )
        embed.add_field(
            name="🎫 Tickets",
            value=(
                "`,sendtickets` — post ticket panel\n"
                "`,claimticket` — claim the current ticket\n"
                "`,closeticket` — close & delete the current ticket\n"
                "**Inside ticket channels:**\n"
                "Row 1: 🙋 Claim  ↩️ Unclaim  ➕ Add User  ➖ Remove\n"
                "Row 2: 🔒 Close  ✏️ Rename  🔴 Priority  📄 Transcript\n"
                "Row 3: 🔐 Lock  🔓 Unlock\n"
                "**Default categories:** General • Report • Appeal • Form a Alliance • Bug • Unban • Staff App\n"
                "`,addticketcategory` / `,removeticketcategory` / `,ticketcategories` — manage categories for THIS server only (they don't appear in other servers)"
            ),
            inline=False
        )
        embed.add_field(
            name="📋 Log Channels",
            value=(
                "`,setlogchannel` — view all log keys & their current channels\n"
                "`,setlogchannel <key> #channel` — pin a key to a specific channel\n"
                "`,setlogchannel <key> reset` — clear pin, fall back to name lookup\n"
                "*The bot auto-finds channels by name (e.g. `mod-logs`, `ban-logs`).\n"
                "No hardcoded IDs needed — just name your channels correctly.*"
            ),
            inline=False
        )
        embed.add_field(
            name="💾 Backup & Restore",
            value=(
                "`,backup [label]` — snapshot server structure\n"
                "`,listbackups` — view all saved backups\n"
                "`,restore <label>` — rebuild from backup\n"
                "`,deletebackup <label>` — delete a backup"
            ),
            inline=False
        )
        embed.add_field(
            name="🎯 Milestones",
            value=(
                "`,milestones` — view milestone config\n"
                "`,setmilestone [#channel]` — set announcement channel\n"
                "`,testmilestone` — preview a milestone announcement"
            ),
            inline=False
        )
        embed.add_field(
            name="🔗 Invite / Welcome",
            value=(
                "`,setinvite <link>` — set server invite used in DMs\n"
                "`,welcome [@user]` — send welcome card\n"
                "`,sendwelcome @user` — send welcome to specific user\n"
                "`,disablewelcome` — disable auto-welcome"
            ),
            inline=False
        )
        embed.add_field(
            name="🖼️ GIF AutoMod Exemption",
            value=(
                "`,setgifrole @role` — allow this role to post GIFs without automod deleting them (this server only)\n"
                "`,setgifrole` — reset to the default fallback role"
            ),
            inline=False
        )
        embed.add_field(
            name="🚨 Anti-Nuke Config",
            value=(
                f"Role delete threshold: **{NUKE_ROLE_LIMIT}** in **{NUKE_WINDOW}s**\n"
                f"Channel delete threshold: **{NUKE_CHAN_LIMIT}** in **{NUKE_WINDOW}s**\n"
                "Trigger: auto-strip all roles from suspect (admins exempt)"
            ),
            inline=False
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text=f"TrapAI • {guild.name}  •  Admin")
        return embed

    # ─────────────────────────────────────────────────────────
    # GAMES
    # ─────────────────────────────────────────────────────────
    def games_embed(self, guild: discord.Guild):
        embed = discord.Embed(
            title="🎮 Games & Economy",
            color=discord.Color.from_rgb(87, 242, 135),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="💰 Economy",
            value=(
                "`,balance [@user]` — check your wallet & bank\n"
                "`,work` — earn coins (1h cooldown)\n"
                "`,daily` — claim daily reward (24h cooldown)\n"
                "`,weekly` — claim weekly reward (7d cooldown)\n"
                "`,deposit <amount|all>` — move coins to bank\n"
                "`,withdraw <amount|all>` — take coins from bank\n"
                "`,give @user <amount>` — send coins to someone\n"
                "`,leaderboard` — top 10 richest members\n"
                "`,rob @user` — attempt to rob someone (risky!)"
            ),
            inline=False
        )
        embed.add_field(
            name="🎰 Casino Games",
            value=(
                "`,slots <bet>` — spin the slot machine\n"
                "`,coinflip <bet> <heads|tails>` — flip a coin\n"
                "`,blackjack <bet>` — play blackjack vs the dealer\n"
                "`,dice <bet> <1-6>` — guess the dice roll\n"
                "`,crash <bet>` — cash out before the rocket crashes\n"
                "`,highlow <bet>` — higher or lower card game"
            ),
            inline=False
        )
        embed.add_field(
            name="🎯 Fun Games",
            value=(
                "`,rps <rock|paper|scissors>` — rock paper scissors vs bot\n"
                "`,trivia` — random trivia question\n"
                "`,hangman` — guess the word letter by letter\n"
                "`,numguess` — guess a number 1–100\n"
                "`,8ball <question>` — ask the magic 8-ball\n"
                "`,wordchain` — start a word chain game\n"
                "`,tictactoe @user` — challenge someone to tic-tac-toe"
            ),
            inline=False
        )
        embed.add_field(
            name="🏆 Leaderboards",
            value=(
                "`,leaderboard` — richest members\n"
                "`,gamblers` — top casino winners"
            ),
            inline=False
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text=f"TrapAI • {guild.name}  •  Games & Economy")
        return embed

    # ─────────────────────────────────────────────────────────
    # BUTTONS  —  Row 0 (5) + Row 1 (5) + Row 2 (1)
    # ─────────────────────────────────────────────────────────
    @discord.ui.button(label="Home",       style=discord.ButtonStyle.secondary, emoji="🏠",  row=0)
    async def home_btn(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.edit_message(embed=self.home_embed(i.guild), view=self)

    @discord.ui.button(label="Moderation", style=discord.ButtonStyle.danger,    emoji="🛡",  row=0)
    async def mod_btn(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.edit_message(embed=self.moderation_embed(i.guild), view=self)

    @discord.ui.button(label="Jail",       style=discord.ButtonStyle.danger,    emoji="🔒",  row=0)
    async def jail_btn(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.edit_message(embed=self.jail_embed(i.guild), view=self)

    @discord.ui.button(label="Security",   style=discord.ButtonStyle.success,   emoji="🤖",  row=0)
    async def sec_btn(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.edit_message(embed=self.security_embed(i.guild), view=self)

    @discord.ui.button(label="Stats",      style=discord.ButtonStyle.primary,   emoji="📊",  row=0)
    async def stats_btn(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.edit_message(embed=self.stats_embed(i.guild), view=self)

    @discord.ui.button(label="Roles",      style=discord.ButtonStyle.primary,   emoji="🏷️",  row=1)
    async def roles_btn(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.edit_message(embed=self.roles_embed(i.guild), view=self)

    @discord.ui.button(label="VC",         style=discord.ButtonStyle.primary,   emoji="🎤",  row=1)
    async def vc_btn(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.edit_message(embed=self.vc_embed(i.guild), view=self)

    @discord.ui.button(label="Giveaways",  style=discord.ButtonStyle.success,   emoji="🎉",  row=1)
    async def gw_btn(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.edit_message(embed=self.giveaway_embed(i.guild), view=self)

    @discord.ui.button(label="Staff",      style=discord.ButtonStyle.secondary, emoji="📋",  row=1)
    async def staff_btn(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.edit_message(embed=self.staff_embed(i.guild), view=self)

    @discord.ui.button(label="Admin",      style=discord.ButtonStyle.primary,   emoji="⚙️",  row=1)
    async def admin_btn(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.edit_message(embed=self.admin_embed(i.guild), view=self)

    @discord.ui.button(label="Games",      style=discord.ButtonStyle.success,   emoji="🎮",  row=2)
    async def games_btn(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.edit_message(embed=self.games_embed(i.guild), view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


# ============================================================
# HELPERS
# ============================================================
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
        for channel in guild.channels:
            try:
                if isinstance(channel, discord.TextChannel):
                    await channel.set_permissions(role, send_messages=False, add_reactions=False)
                elif isinstance(channel, discord.VoiceChannel):
                    await channel.set_permissions(role, speak=False)
            except discord.HTTPException:
                pass
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
    """Hide every channel from this member, except channels named 'jail'."""
    guild = member.guild
    for channel in guild.channels:
        # Skip the jail channel itself so they can still see/type there
        if channel.name == "jail":
            continue
        # Skip channels the bot can't manage
        if not channel.permissions_for(guild.me).manage_permissions:
            continue
        try:
            await channel.set_permissions(
                member,
                view_channel=False,
                reason="Jailed — channel hidden"
            )
        except (discord.Forbidden, discord.HTTPException):
            pass


async def _remove_jail_overwrites(member: discord.Member):
    """Remove the jail-applied view_channel overwrite from every channel."""
    guild = member.guild
    for channel in guild.channels:
        ow = channel.overwrites_for(member)
        # Only remove if we set a deny on view_channel — leave anything else untouched
        if ow.view_channel is False:
            try:
                # Clear just the view_channel bit; preserve any other bits
                ow.view_channel = None
                if ow.is_empty():
                    await channel.set_permissions(member, overwrite=None, reason="Unjailed — channel access restored")
                else:
                    await channel.set_permissions(member, overwrite=ow, reason="Unjailed — channel access restored")
            except (discord.Forbidden, discord.HTTPException):
                pass


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
    unverified_role = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE)

    if jail_role and jail_role in member.roles:
        try:
            await member.remove_roles(jail_role, reason=reason)
            if unverified_role and unverified_role not in member.roles:
                await member.add_roles(unverified_role, reason="Returned to unverified after jail")
            await _restore_jail_role_snapshot(guild, member)
            await _remove_jail_overwrites(member)
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
        color=discord.Color.gold(),
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
            name="discord.gg/ballin"
        )
    )
    # Cache current invite use-counts for all guilds
    for guild in bot.guilds:
        try:
            invites = await guild.invites()
            INVITE_CACHE[guild.id] = {inv.code: inv.uses for inv in invites}
        except (discord.Forbidden, discord.HTTPException):
            pass

    # One-time startup work — on_ready can re-fire on gateway reconnects,
    # so guard resume logic to avoid double-scheduling jail/giveaway tasks.
    if not _startup_resumed:
        _startup_resumed = True
        asyncio.create_task(_autosave_loop())
        asyncio.create_task(_birthday_loop())
        asyncio.create_task(_vanity_sweep_loop())

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
    _guild_invite_dm = GUILD_INVITE.get(member.guild.id, "")
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
                    ("👤 Member",  f"{after.mention} (`{after.id}`)", True),
                    ("🏷️ Role",    f"{role.mention} (`{role.id}`)",   True),
                    ("📌 Position", str(role.position),                True),
                ],
                target=after
            )

        for role in removed_roles:
            await log(
                after.guild,
                "roles",
                "Role Removed",
                None,
                discord.Color.red(),
                fields=[
                    ("👤 Member",  f"{after.mention} (`{after.id}`)", True),
                    ("🏷️ Role",    f"{role.mention} (`{role.id}`)",   True),
                    ("📌 Position", str(role.position),                True),
                ],
                target=after
            )

    if before.nick != after.nick:
        await log(
            after.guild,
            "nicknames",
            "Nickname Changed",
            None,
            discord.Color.blurple(),
            fields=[
                ("👤 Member",    f"{after.mention} (`{after.id}`)",  True),
                ("📝 Old Nick",  before.nick or before.name,          True),
                ("📝 New Nick",  after.nick  or after.name,           True),
            ],
            target=after
        )


@bot.event
async def on_guild_role_create(role):
    await log(
        role.guild,
        "role_create",
        "Role Created",
        None,
        discord.Color.green(),
        fields=[
            ("✨ Role",       f"{role.mention} (`{role.id}`)", True),
            ("🎨 Color",      str(role.color),                 True),
            ("📌 Hoisted",    str(role.hoist),                 True),
            ("💬 Mentionable", str(role.mentionable),          True),
        ]
    )


@bot.event
async def on_guild_role_delete(role):
    await log(
        role.guild,
        "role_delete",
        "Role Deleted",
        None,
        discord.Color.red(),
        fields=[
            ("🗑️ Role Name", f"`{role.name}`",  True),
            ("🆔 Role ID",   str(role.id),       True),
            ("🎨 Color",     str(role.color),    True),
            ("👥 Had Members", str(len(role.members)), True),
        ]
    )

    # ── Anti-nuke: track rapid role deletions ─────────────────
    guild = role.guild
    try:
        entry = None
        async for e in guild.audit_logs(limit=1, action=discord.AuditLogAction.role_delete):
            entry = e
            break
        if not entry or entry.user.bot:
            return
        actor = entry.user
        if actor.guild_permissions.administrator:
            return
        now = time.time()
        tracker = NUKE_TRACKER.setdefault(guild.id, {}).setdefault(actor.id, [])
        tracker.append(now)
        NUKE_TRACKER[guild.id][actor.id] = [t for t in tracker if now - t <= NUKE_WINDOW]
        if len(NUKE_TRACKER[guild.id][actor.id]) >= NUKE_ROLE_LIMIT:
            NUKE_TRACKER[guild.id][actor.id].clear()
            # Strip all non-default roles
            roles_to_remove = [r for r in actor.roles if not r.is_default() and r < guild.me.top_role]
            if roles_to_remove:
                await actor.remove_roles(*roles_to_remove, reason="🚨 Anti-nuke: rapid role deletion detected")
            await log(guild, "mod", "🚨 Anti-Nuke Triggered — Role Deletions", None,
                      discord.Color.red(),
                      fields=[
                          ("⚠️ Action",       "Rapid Role Deletes Detected",                          True),
                          ("👤 Suspect",       f"{actor.mention} (`{actor.id}`)",                      True),
                          ("🔢 Deletes",       f"{NUKE_ROLE_LIMIT}+ roles deleted in {NUKE_WINDOW}s", True),
                          ("⚔️ Roles Stripped", ", ".join(r.name for r in roles_to_remove)[:512] or "None", False),
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
    guild = channel.guild
    try:
        entry = None
        async for e in guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_delete):
            entry = e
            break
        if not entry or entry.user.bot:
            return
        actor = entry.user
        if actor.guild_permissions.administrator:
            return
        now = time.time()
        tracker = NUKE_TRACKER.setdefault(guild.id, {}).setdefault(actor.id + 1_000_000_000, [])
        tracker.append(now)
        NUKE_TRACKER[guild.id][actor.id + 1_000_000_000] = [t for t in tracker if now - t <= NUKE_WINDOW]
        if len(NUKE_TRACKER[guild.id][actor.id + 1_000_000_000]) >= NUKE_CHAN_LIMIT:
            NUKE_TRACKER[guild.id][actor.id + 1_000_000_000].clear()
            roles_to_remove = [r for r in actor.roles if not r.is_default() and r < guild.me.top_role]
            if roles_to_remove:
                await actor.remove_roles(*roles_to_remove, reason="🚨 Anti-nuke: rapid channel deletion detected")
            await log(guild, "mod", "🚨 Anti-Nuke Triggered — Channel Deletions", None,
                      discord.Color.red(),
                      fields=[
                          ("⚠️ Action",       "Rapid Channel Deletes Detected",                          True),
                          ("👤 Suspect",       f"{actor.mention} (`{actor.id}`)",                         True),
                          ("🔢 Deletes",       f"{NUKE_CHAN_LIMIT}+ channels deleted in {NUKE_WINDOW}s",  True),
                          ("⚔️ Roles Stripped", ", ".join(r.name for r in roles_to_remove)[:512] or "None", False),
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
            discord.Color.gold(),
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
        discord.Color.gold(),
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
    embed = discord.Embed(color=discord.Color.gold(), timestamp=datetime.fromtimestamp(entry["edited_at"]))
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
@commands.has_permissions(manage_messages=True)
async def clearsnipe(ctx):
    """Clear the snipe/editsnipe cache for this channel. Usage: ,clearsnipe"""
    had_snipe = SNIPE_CACHE.pop(ctx.channel.id, None) is not None
    had_edit  = EDIT_SNIPE_CACHE.pop(ctx.channel.id, None) is not None
    if not had_snipe and not had_edit:
        await ctx.send("❌ Nothing cached here to clear.", delete_after=8)
        return
    await ctx.send("🧹 Snipe cache cleared for this channel.", delete_after=6)


@bot.event
async def on_presence_update(before, after):
    guild = after.guild
    if guild is None or after.bot:
        return
    role_id = VANITY_ROLE.get(guild.id)
    if not role_id:
        return
    role = guild.get_role(role_id)
    if not role:
        return
    code = _resolve_vanity_code(guild)
    if not code:
        return

    has_it  = _status_has_vanity(after, code)
    already = role in after.roles
    if has_it and not already:
        try:
            await after.add_roles(role, reason="Vanity URL detected in status")
            await log(guild, "vanity", "Vanity Role Granted",
                      f"{after.mention} is repping **discord.gg/{code}** in their status and received {role.mention}.",
                      discord.Color.green(), target=after)
        except (discord.Forbidden, discord.HTTPException):
            pass
    elif already and not has_it:
        try:
            await after.remove_roles(role, reason="Vanity URL no longer in status")
            await log(guild, "vanity", "Vanity Role Removed",
                      f"{after.mention} no longer has **discord.gg/{code}** in their status — {role.mention} removed.",
                      discord.Color.orange(), target=after)
        except (discord.Forbidden, discord.HTTPException):
            pass


async def _vanity_sweep_loop():
    """Periodic full sweep as a safety net — catches any member whose status
    already had the vanity link before the bot started, or any presence
    update the gateway happened to miss."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        for guild in bot.guilds:
            role_id = VANITY_ROLE.get(guild.id)
            if not role_id:
                continue
            role = guild.get_role(role_id)
            if not role:
                continue
            code = _resolve_vanity_code(guild)
            if not code:
                continue
            for member in guild.members:
                if member.bot:
                    continue
                has_it  = _status_has_vanity(member, code)
                already = role in member.roles
                try:
                    if has_it and not already:
                        await member.add_roles(role, reason="Vanity URL detected (periodic sweep)")
                    elif already and not has_it:
                        await member.remove_roles(role, reason="Vanity URL no longer present (periodic sweep)")
                except (discord.Forbidden, discord.HTTPException):
                    pass
        await asyncio.sleep(900)


async def _find_voice_mod(guild, action, *, member=None, channel=None, attr=None, expected=None, window=6):
    """
    Best-effort audit-log lookup for who performed a recent voice action
    (disconnect, move, server mute/deafen) affecting `member`. Discord's audit
    log doesn't always link move/disconnect entries to a specific member, so
    this matches on recency (+ channel/attribute where possible) and returns
    (None, None) when nothing plausible is found within `window` seconds —
    meaning the member most likely triggered it themselves.
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

    # ── Self-service unmute: joining a registered unmute VC instantly
    # clears their VC server mute AND server deafen, no staff needed, then
    # bounces them back out so the channel/slot is free for the next person.
    if after.channel and after.channel.id in UNMUTE_VC_CHANNELS.get(guild.id, []):
        if after.mute or after.deaf:
            try:
                await member.edit(mute=False, deafen=False, reason="Self-unmute via unmute VC")
            except (discord.Forbidden, discord.HTTPException):
                pass
            else:
                _log_mod_action(guild.id, member.id, "unmute", "Self-service (unmute VC)", f"Joined {after.channel.name}")
                await log(guild, "vc", "Member Self-Unmuted/Undeafened", None, discord.Color.green(),
                          fields=[
                              ("🔊 User", f"{member.mention} (`{member.id}`)", True),
                              ("🎤 Via",  after.channel.mention,               True),
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

        mod, reason = await _find_voice_mod(guild, discord.AuditLogAction.member_disconnect, member=member)
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

        mod, reason = await _find_voice_mod(guild, discord.AuditLogAction.member_move, member=member, channel=after.channel)
        if mod:
            await log(guild, "vc", "VC Moved (by Staff)",
                      f"**{member.display_name}** was moved from {before.channel.mention} to {after.channel.mention} by {mod.mention}.",
                      discord.Color.gold(),
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
                      discord.Color.gold(),
                      fields=[
                          ("👤 Member",         f"{member.mention} (`{member.id}`)", True),
                          ("📤 From",           before.channel.mention,              True),
                          ("📥 To",             after.channel.mention,               True),
                          ("👥 In New Channel", _vc_occupancy(after.channel),        False),
                      ], target=member)

    # ── Server mute / unmute (staff-imposed only; only while still connected) ──
    if before.channel is not None and after.channel is not None and before.mute != after.mute:
        mod, reason = await _find_voice_mod(
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
        mod, reason = await _find_voice_mod(
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


@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    # Track chat stats
    guild_stats = CHAT_STATS.setdefault(message.guild.id, {})
    guild_stats[message.author.id] = guild_stats.get(message.author.id, 0) + 1

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
        await ctx.send("❌ You don't have permission to use that command.")
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
    elif isinstance(error, commands.CheckFailure):
        await ctx.send("❌ You don't have permission to use that command.")
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


@bot.command(name="cmds")
async def cmds(ctx):
    view = CmdsView(ctx.author.id)
    embed = view.home_embed(ctx.guild)
    await ctx.send(embed=embed, view=view)


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
    invite      = GUILD_INVITE.get(guild.id, "")
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
@commands.has_permissions(manage_messages=True)
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
@commands.has_permissions(manage_messages=True)
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
@commands.has_permissions(manage_guild=True)
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
@commands.has_permissions(manage_guild=True)
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

    is_booster = member.premium_since is not None

    if action is None:
        embed = discord.Embed(
            title="🌟 Booster Role",
            color=role.color if role else discord.Color.gold(),
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


@bot.command()
@commands.has_permissions(manage_guild=True)
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
    await ch.set_permissions(ctx.guild.default_role, connect=False)
    await ctx.send(embed=_vc_embed("🔒 VC Locked", f"Only permitted users can now join **{ch.name}**."))
    await _vc_announce(ctx.guild, ch, f"🔒 **{ctx.author.display_name}** locked the VC.")


@bot.command()
async def vcunlock(ctx):
    ch = get_owned_temp_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be in a VC you own or moderate.")
        return
    await ch.set_permissions(ctx.guild.default_role, connect=True)
    await ctx.send(embed=_vc_embed("🔓 VC Unlocked", f"**{ch.name}** is now open to everyone.", discord.Color.green()))
    await _vc_announce(ctx.guild, ch, f"🔓 **{ctx.author.display_name}** unlocked the VC.")


@bot.command()
async def vchide(ctx):
    ch = get_owned_temp_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be in a VC you own or moderate.")
        return
    await ch.set_permissions(ctx.guild.default_role, view_channel=False)
    await ctx.send(embed=_vc_embed("👻 VC Hidden", f"**{ch.name}** is now invisible to everyone."))
    await _vc_announce(ctx.guild, ch, f"👻 **{ctx.author.display_name}** hid the VC.")


@bot.command()
async def vcshow(ctx):
    ch = get_owned_temp_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be in a VC you own or moderate.")
        return
    await ch.set_permissions(ctx.guild.default_role, view_channel=True)
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
    if not member.voice or member.voice.channel != ch:
        await ctx.send("❌ That user is not in your VC.")
        return
    await member.move_to(None)
    await ctx.send(embed=_vc_embed("👢 Member Kicked", f"{member.mention} was kicked from **{ch.name}**.", discord.Color.orange()))
    await _vc_announce(ctx.guild, ch, f"👢 **{ctx.author.display_name}** kicked **{member.display_name}** from the VC.")


@bot.command()
async def vcban(ctx, member: discord.Member):
    ch = get_owned_temp_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be in a VC you own or moderate.")
        return
    if member == ctx.author:
        await ctx.send("❌ You cannot VC ban yourself.")
        return
    if _is_vc_owner(member, ch):
        await ctx.send("❌ You can't ban the VC owner.")
        return
    await ch.set_permissions(member, connect=False, view_channel=False)
    vc_banned.setdefault(ch.id, set()).add(member.id)
    if member.voice and member.voice.channel == ch:
        await member.move_to(None)
    await ctx.send(embed=_vc_embed("🚫 VC Ban Applied", f"{member.mention} was banned from **{ch.name}**.", discord.Color.red()))
    await _vc_announce(ctx.guild, ch, f"🚫 **{ctx.author.display_name}** banned **{member.display_name}** from the VC.")


@bot.command()
async def vcunban(ctx, member: discord.Member):
    ch = get_owned_temp_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be in a VC you own or moderate.")
        return
    await ch.set_permissions(member, overwrite=None)
    vc_banned.get(ch.id, set()).discard(member.id)
    await ctx.send(embed=_vc_embed("✔️ VC Ban Removed", f"{member.mention} can join **{ch.name}** again.", discord.Color.green()))
    await _vc_announce(ctx.guild, ch, f"✔️ **{ctx.author.display_name}** unbanned **{member.display_name}**.")


@bot.command()
async def vcpermit(ctx, member: discord.Member):
    ch = get_owned_temp_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be in a VC you own or moderate.")
        return
    await ch.set_permissions(member, connect=True, view_channel=True)
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
    if not member.voice or member.voice.channel != ch:
        await ctx.send("❌ That user must be in your VC.")
        return

    old_owner_id = temp_vc_owners.get(ch.id)
    old_owner = ctx.guild.get_member(old_owner_id) if old_owner_id else None

    temp_vc_owners[ch.id] = member.id
    await ch.set_permissions(member, manage_channels=True, manage_permissions=True, move_members=True, connect=True, speak=True)
    # Revoke the previous owner's channel-admin overwrite — see VCTransferModal
    # for why this matters (otherwise they keep native Discord control).
    if old_owner and old_owner.id != member.id:
        try:
            await ch.set_permissions(old_owner, overwrite=None)
        except (discord.Forbidden, discord.HTTPException):
            pass
    text_id = temp_vc_text_channels.get(ch.id)
    if text_id:
        text_ch = ctx.guild.get_channel(text_id)
        if text_ch:
            await text_ch.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
    await ctx.send(embed=_vc_embed("👑 Ownership Transferred", f"{member.mention} is now the owner of **{ch.name}**.", discord.Color.gold()))
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

    temp_vc_owners[ch.id] = ctx.author.id
    await ch.set_permissions(ctx.author, manage_channels=True, manage_permissions=True, move_members=True, connect=True, speak=True)
    # Revoke the old owner's channel-admin overwrite — see VCTransferModal
    # for why this matters (otherwise they keep native Discord control).
    if old_owner and old_owner.id != ctx.author.id:
        try:
            await ch.set_permissions(old_owner, overwrite=None)
        except (discord.Forbidden, discord.HTTPException):
            pass
    text_id = temp_vc_text_channels.get(ch.id)
    if text_id:
        text_ch = ctx.guild.get_channel(text_id)
        if text_ch:
            await text_ch.set_permissions(ctx.author, view_channel=True, send_messages=True, read_message_history=True)

    await ctx.send(embed=_vc_embed("👑 Ownership Claimed", f"{ctx.author.mention} claimed ownership of **{ch.name}** — the previous owner left.", discord.Color.gold()))
    await _vc_announce(ctx.guild, ch, f"👑 **{ctx.author.display_name}** claimed ownership — the previous owner left the VC.")
    await log(
        ctx.guild,
        "vc",
        "VC Ownership Claimed",
        f"{ctx.author.mention} claimed **{ch.name}** after the owner left.",
        discord.Color.gold(),
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
    if not member.voice or member.voice.channel != ch:
        await ctx.send("❌ That user must be in your VC.")
        return
    vc_mods.setdefault(ch.id, set()).add(member.id)
    await ch.set_permissions(member, move_members=True, mute_members=True, deafen_members=True, manage_channels=True)
    await ctx.send(embed=_vc_embed("🛡 VC Mod Granted", f"{member.mention} is now a VC moderator in **{ch.name}**.", discord.Color.blurple()))
    await _vc_announce(ctx.guild, ch, f"🛡 **{ctx.author.display_name}** made **{member.display_name}** a VC moderator.")


@bot.command()
async def vcremovemod(ctx, member: discord.Member):
    ch = get_strictly_owned_vc(ctx.author)
    if not ch:
        await ctx.send("❌ You must be the **owner** of a temporary VC.")
        return
    vc_mods.get(ch.id, set()).discard(member.id)
    await ch.set_permissions(member, overwrite=None)
    await ctx.send(embed=_vc_embed("🗑️ VC Mod Removed", f"{member.mention} is no longer a VC moderator in **{ch.name}**.", discord.Color.orange()))
    await _vc_announce(ctx.guild, ch, f"🗑️ **{ctx.author.display_name}** removed **{member.display_name}** as VC moderator.")


# ============================================================
# ADMIN COMMANDS
# ============================================================
@bot.command()
@commands.has_permissions(administrator=True)
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

    island_category = discord.utils.get(guild.categories, name="🏘️ Ballin")
    if island_category is None:
        island_category = await guild.create_category("🏘️ Ballin")

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
    await get_or_create_voice_channel("🔥 Ballin VC", island_category)
    await get_or_create_voice_channel("🎮 Chill VC", island_category)

    await welcome_channel.edit(topic="🏘️ Arrival Zone • New members are scanned by TrapAI before entering Ballin")
    await rules_channel.edit(topic="📜 TrapAI server rules and enforcement")
    await verify_channel.edit(topic="🌐 Server Verification System • Click Authenticate via Discord below to enter Ballin")
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


@bot.command()
@commands.has_permissions(administrator=True)
async def setupvc(ctx, category_name: str = None):
    """Create the ➕ Create VC trigger channel in this server.
    Optionally pass a category name to place it in: ,setupvc "Ballin VCs"
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
@commands.has_permissions(administrator=True)
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


@bot.command(name="setlogchannel")
@commands.has_permissions(administrator=True)
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
@commands.has_permissions(administrator=True)
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
@commands.has_permissions(administrator=True)
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
@commands.has_permissions(administrator=True)
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
    "gold": discord.Color.gold(), "purple": discord.Color.purple(), "teal": discord.Color.teal(),
    "orange": discord.Color.orange(), "pink": discord.Color.from_rgb(255, 105, 180),
    "dark_red": discord.Color.dark_red(), "dark_teal": discord.Color.dark_teal(),
    "dark_gold": discord.Color.dark_gold(), "blurple": discord.Color.blurple(),
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
@commands.has_permissions(administrator=True)
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
@commands.has_permissions(administrator=True)
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
@commands.has_permissions(administrator=True)
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
@commands.has_permissions(manage_messages=True)
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
@commands.has_permissions(manage_messages=True)
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


@bot.command()
@commands.has_permissions(administrator=True)
async def rules(ctx):
    embed = discord.Embed(
        title="🤖 TrapAI Server Rules",
        description=(
            f"Welcome to **{ctx.guild.name}** 🏘️🔥\n\n"
            f"To stay in **{ctx.guild.name}**, all members must follow the rules below.\n\n"
            "```yaml\n"
            "TrapAI Status: ACTIVE\n"
            "Rule Enforcement: ENABLED\n"
            "Violation Response: WARNING / TIMEOUT / JAIL / BAN\n"
            "```"
        ),
        color=discord.Color.dark_grey(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="1️⃣ Respect Everyone", value="No harassment, racism, hate speech, threats, or bullying.", inline=False)
    embed.add_field(name="2️⃣ No Spamming", value="Do not flood chats, mass mention, or spam messages, emojis, or reactions.", inline=False)
    embed.add_field(name="3️⃣ No Ads or Links", value="No self-promo, invite links, or outside advertising without staff approval.", inline=False)
    embed.add_field(name="4️⃣ Keep It Clean", value="No harmful content, scams, doxxing, or anything meant to harm the server.", inline=False)
    embed.add_field(name="5️⃣ Use Channels Correctly", value="Keep topics in the right channels and follow staff directions.", inline=False)
    embed.add_field(name="6️⃣ VC Rules", value="No mic spam, earrape, screaming, or trolling in voice channels.", inline=False)
    embed.add_field(name="7️⃣ No Impersonation", value="Do not impersonate other members, staff, or bots. Violations may result in an immediate ban.", inline=False)
    embed.add_field(name="8️⃣ Staff Decisions", value="Arguing with moderation actions in public may lead to more punishment. Contact staff calmly.", inline=False)
    embed.add_field(
        name="⚠ TrapAI Enforcement",
        value="Breaking rules may result in:\n• Warning\n• Timeout\n• Jail\n• Ban",
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
@commands.has_permissions(manage_roles=True)
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
            description=f"{member.mention} is now a **Ballin Member** 🏘️🔥",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Verified by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        await log(ctx.guild, "verification", "Member Verified", None, discord.Color.green(),
                  fields=[("🛡 Staff", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("✅ User", f"{member.mention} (`{member.id}`)", True)],
                  actor=ctx.author, target=member)

    except discord.Forbidden:
        await ctx.send("❌ I can't manage that member's roles. Move my bot role higher.")
    except discord.HTTPException:
        await ctx.send("❌ Something went wrong while verifying that member.")


@bot.command()
@commands.has_permissions(manage_roles=True)
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
            description=f"{member.mention} has been moved back to **Unverified Smoker**.",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Unverified by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        await log(ctx.guild, "verification", "Member Unverified", None, discord.Color.orange(),
                  fields=[("🛡 Staff", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("🚫 User", f"{member.mention} (`{member.id}`)", True)],
                  actor=ctx.author, target=member)

    except discord.Forbidden:
        await ctx.send("❌ I can't manage that member's roles. Move my bot role higher.")
    except discord.HTTPException:
        await ctx.send("❌ Something went wrong while unverifying that member.")


@bot.command()
@commands.has_permissions(manage_roles=True)
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
        await ctx.send("❌ I can't manage that member's roles.")
    except discord.HTTPException:
        await ctx.send("❌ Something went wrong while denying verification.")


# ============================================================
# SECURITY COMMANDS
# ============================================================
@bot.command()
@commands.has_permissions(manage_messages=True)
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
@commands.has_permissions(manage_messages=True)
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
        color=discord.Color.gold(),
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
        threat_color  = discord.Color.gold()
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
@commands.has_permissions(manage_roles=True)
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

            if other_roles:
                await member.remove_roles(*other_roles, reason=f"Jailed by {ctx.author} — roles stripped")
            await member.add_roles(jail_role, reason=f"Jailed by {ctx.author} | {reason}")

        await _apply_jail_overwrites(member)

        old_task = jailed_users.get((ctx.guild.id, member.id))
        if old_task:
            old_task.cancel()

        jailed_users[(ctx.guild.id, member.id)] = asyncio.create_task(
            auto_unjail(ctx.guild.id, member.id, seconds, reason)
        )

        JAIL_EXPIRY.setdefault(ctx.guild.id, {})[member.id] = {
            "expires_at": time.time() + seconds,
            "reason": reason,
        }
        _save_jail_expiry()
        _log_mod_action(ctx.guild.id, member.id, "jail", ctx.author, reason, extra=f"Duration: {duration}")

        # DM the user so they know they've been jailed
        await _dm_action(member, ctx.guild, "jail", ctx.author, reason,
                         extra=f"Duration: **{duration}**")

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
                "```\n"
                f"🏷️ {roles_note}\n"
                "🙈 All channels hidden except #jail."
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
                      ("📝 Reason",    reason,                                         False),
                      ("🏷️ Roles",     "Stripped (will auto-restore)" if not already_jailed else "Unchanged", True),
                      ("📨 DM Sent",   "✅ Notified via DM",                           True),
                  ],
                  actor=ctx.author, target=member)

    except discord.Forbidden:
        await ctx.send("❌ I can't manage that member's roles. Move my bot role higher.")
    except discord.HTTPException:
        await ctx.send("❌ Something went wrong while jailing that member.")


@bot.command()
@commands.has_permissions(manage_roles=True)
async def unjail(ctx, member: discord.Member, *, reason="No reason provided"):
    jail_role = discord.utils.get(ctx.guild.roles, name=JAIL_ROLE)
    unverified_role = discord.utils.get(ctx.guild.roles, name=UNVERIFIED_ROLE)

    if not jail_role:
        await ctx.send(f"❌ Role **{JAIL_ROLE}** was not found.")
        return
    if jail_role not in member.roles:
        await ctx.send("❌ That user is not jailed.")
        return

    try:
        await member.remove_roles(jail_role, reason=f"Unjailed by {ctx.author} | {reason}")
        if unverified_role and unverified_role not in member.roles:
            await member.add_roles(unverified_role, reason="Returned to unverified after unjail")

        await _restore_jail_role_snapshot(ctx.guild, member)
        await _remove_jail_overwrites(member)

        old_task = jailed_users.get((ctx.guild.id, member.id))
        if old_task:
            old_task.cancel()
            jailed_users.pop((ctx.guild.id, member.id), None)
        _clear_jail_expiry(ctx.guild.id, member.id)
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
        await ctx.send("❌ I can't manage that member's roles. Move my bot role higher.")
    except discord.HTTPException:
        await ctx.send("❌ Something went wrong while unjailing that member.")


# ============================================================
# MODERATION COMMANDS
# ============================================================
@bot.command()
@commands.has_permissions(manage_messages=True)
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
@commands.has_permissions(manage_channels=True)
async def lock(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False)
    await ctx.send("🔒 Channel locked")
    await log(ctx.guild, "mod", "Channel Locked", None, discord.Color.red(),
              fields=[("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("🔐 Channel", ctx.channel.mention, True)],
              actor=ctx.author)


@bot.command()
@commands.has_permissions(manage_channels=True)
async def unlock(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=True)
    await ctx.send("🔓 Channel unlocked")
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
@commands.has_permissions(kick_members=True)
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
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason="No reason provided"):
    # DM before ban so the message actually reaches them
    await _dm_action(member, ctx.guild, "ban", ctx.author, reason)
    await member.ban(reason=reason)
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
@commands.has_permissions(moderate_members=True)
async def timeout(ctx, member: discord.Member, minutes: int, *, reason="No reason provided"):
    until      = discord.utils.utcnow() + timedelta(minutes=minutes)
    expire_str = discord.utils.format_dt(until, "F")
    # DM before applying so they understand what happened
    await _dm_action(member, ctx.guild, "timeout", ctx.author, reason,
                     extra=f"Duration: **{minutes} minute(s)**\nExpires: {expire_str}")
    await member.timeout(until, reason=reason)
    _log_mod_action(ctx.guild.id, member.id, "timeout", ctx.author, reason, extra=f"Duration: {minutes} minute(s)")
    embed = discord.Embed(
        title="⏳ Member Timed Out",
        description=f"{member.mention} has been timed out in **{ctx.guild.name}**.",
        color=discord.Color.gold(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="📝 Reason",    value=reason,            inline=False)
    embed.add_field(name="🛡 Moderator", value=ctx.author.mention, inline=True)
    embed.add_field(name="⏱️ Duration",  value=f"{minutes} min",   inline=True)
    embed.add_field(name="🗓️ Expires",   value=expire_str,          inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"TrapAI Moderation • {ctx.guild.name}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)
    await log(ctx.guild, "timeouts", "Member Timed Out", None, discord.Color.gold(),
              fields=[
                  ("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("⏳ User",      f"{member.mention} (`{member.id}`)",          True),
                  ("⏱️ Duration",  f"{minutes} minute(s)",                        True),
                  ("🗓️ Expires",   expire_str,                                    False),
                  ("📝 Reason",    reason,                                         False),
                  ("📨 DM Sent",   "✅ Notified via DM",                           True),
              ],
              actor=ctx.author, target=member)


@bot.command()
@commands.has_permissions(administrator=True)
async def strip(ctx, member: discord.Member):
    """
    Strip EVERY role from a member (except @everyone and anything above my
    own top role, which I physically can't touch) — they'll show no roles
    on their profile until restored. Usage: ,strip @user
    """
    removable = [
        role for role in member.roles
        if role.name != "@everyone" and role < ctx.guild.me.top_role
    ]
    if not removable:
        await ctx.send("❌ That user has no roles I can remove.")
        return

    # Snapshot ALL non-everyone roles (including any above my top role that
    # couldn't be removed now) for full restoration later.
    snapshot = [r.id for r in member.roles if r.name != "@everyone"]
    ROLE_SNAPSHOTS.setdefault(ctx.guild.id, {})[member.id] = snapshot
    _save_role_snapshots()

    role_names = ", ".join(role.name for role in removable)
    await member.remove_roles(*removable, reason=f"Stripped by {ctx.author}")
    _log_mod_action(ctx.guild.id, member.id, "strip", ctx.author, f"Removed: {role_names}")
    await ctx.send(
        f"✅ Removed **all {len(removable)} role(s)** from {member.mention} — their profile now shows no roles.\n"
        f"📸 Snapshot saved — use `,restoreallroles {member.mention}` to restore."
    )
    await log(ctx.guild, "strips", "All Roles Stripped", None, discord.Color.dark_red(),
              fields=[
                  ("👑 Admin",         f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("⚔️ User",          f"{member.mention} (`{member.id}`)",         True),
                  ("🏷️ Roles Removed", role_names[:512] or "*None*",                False),
                  ("📸 Snapshot",      f"{len(snapshot)} role(s) saved",             True),
              ],
              actor=ctx.author, target=member)


@bot.command()
@commands.has_permissions(administrator=True)
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
@commands.has_permissions(administrator=True)
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


@bot.command()
@commands.has_permissions(administrator=True)
async def lockdown(ctx):
    for channel in ctx.guild.text_channels:
        await channel.set_permissions(ctx.guild.default_role, send_messages=False)
    await ctx.send("🔒 Server lockdown activated.")
    await log(ctx.guild, "lockdowns", "Server Lockdown Enabled", "All text channels locked.", discord.Color.red(),
              fields=[("👑 Admin", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("🔐 Channels Locked", str(len(ctx.guild.text_channels)), True)],
              actor=ctx.author)


@bot.command()
@commands.has_permissions(administrator=True)
async def unlockdown(ctx):
    for channel in ctx.guild.text_channels:
        await channel.set_permissions(ctx.guild.default_role, send_messages=True)
    await ctx.send("🔓 Server lockdown removed.")
    await log(ctx.guild, "unlockdowns", "Server Lockdown Removed", "All text channels unlocked.", discord.Color.green(),
              fields=[("👑 Admin", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("🔓 Channels Unlocked", str(len(ctx.guild.text_channels)), True)],
              actor=ctx.author)


@bot.command()
@commands.has_permissions(administrator=True)
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
    acol  = discord.Color.gold()

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
        color=discord.Color.gold(),
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
@commands.has_permissions(manage_guild=True)
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
@commands.has_permissions(manage_roles=True)
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
@commands.has_permissions(manage_roles=True)
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
    await log(ctx.guild, "roles", "Role Manually Added",
              f"**Staff:** {ctx.author.mention}\n**User:** {member.mention}\n**Role:** {role.mention}",
              discord.Color.green())


@role_group.command(name="remove")
@commands.has_permissions(manage_roles=True)
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
    await log(ctx.guild, "roles", "Role Manually Removed",
              f"**Staff:** {ctx.author.mention}\n**User:** {member.mention}\n**Role:** {role.mention}",
              discord.Color.orange())


@role_group.command(name="create")
@commands.has_permissions(manage_roles=True)
async def role_create(ctx, name: str, color: str = None, hoist: bool = False):
    """
    Create a new role. Color is optional — a name (red, blue, gold, ...) or
    hex (#ff0000) both work, and an unrecognized/invalid color never blocks
    creation, it just falls back to no color.
    Usage: ,role create "Ballin Member" [color] [hoist]
    """
    disc_color = discord.Color.default()
    color_note = ""
    if color:
        parsed = _parse_ticket_color(color)
        if parsed is not None:
            disc_color = parsed
        else:
            color_note = f"\n⚠️ Didn't recognize color `{color}` — created with no color instead."

    new_role = await ctx.guild.create_role(
        name=name, color=disc_color, hoist=hoist,
        reason=f"Role created by {ctx.author}"
    )
    embed = discord.Embed(
        title="✅ Role Created",
        description=f"{new_role.mention} has been created.{color_note}",
        color=new_role.color,
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="Name",  value=new_role.name,  inline=True)
    embed.add_field(name="Color", value=str(new_role.color), inline=True)
    embed.add_field(name="Hoist", value=str(hoist),     inline=True)
    embed.set_footer(text=f"By {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)
    await log(ctx.guild, "role_create", "Role Created (cmd)",
              f"**Staff:** {ctx.author.mention}\n**Role:** {new_role.mention}\n**Color:** {new_role.color}\n**Hoist:** {hoist}",
              discord.Color.green())


@role_group.command(name="delete")
@commands.has_permissions(manage_roles=True)
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
    await log(ctx.guild, "role_delete", "Role Deleted (cmd)",
              f"**Staff:** {ctx.author.mention}\n**Role Name:** `{name}`",
              discord.Color.red())


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


@role_group.command(name="list")
async def role_list(ctx):
    """List all roles in the server. Usage: ,role list"""
    roles = sorted(ctx.guild.roles[1:], key=lambda r: r.position, reverse=True)
    lines = [f"{r.mention} — `{r.id}` — {len(r.members)} member(s)" for r in roles]
    page = "\n".join(lines[:20])
    if len(lines) > 20:
        page += f"\n… and {len(lines) - 20} more roles"
    embed = discord.Embed(
        title=f"🏷️ Roles in {ctx.guild.name}",
        description=page,
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"{len(roles)} total roles • Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


@role_group.command(name="color")
@commands.has_permissions(manage_roles=True)
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
    await log(ctx.guild, "roles", "Role Color Changed",
              f"**Staff:** {ctx.author.mention}\n**Role:** {role.mention}\n**Old:** `{old_color}`\n**New:** `{new_color}`",
              discord.Color.blurple())


@role_group.command(name="hoist")
@commands.has_permissions(manage_roles=True)
async def role_hoist(ctx, role: discord.Role):
    """Toggle role hoisting. Usage: ,role hoist @role"""
    if role >= ctx.guild.me.top_role:
        await ctx.send("❌ I can't edit a role higher than or equal to my own top role.")
        return
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
@commands.has_permissions(moderate_members=True)
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


@bot.command()
@commands.has_permissions(moderate_members=True)
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
# MUTE SYSTEM
# ============================================================
@bot.command()
@commands.has_permissions(moderate_members=True)
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
        await ctx.send("❌ I can't manage that member's roles. Move my bot role higher.")


@bot.command()
@commands.has_permissions(moderate_members=True)
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
        await ctx.send("❌ I can't manage that member's roles. Move my bot role higher.")


@bot.command()
@commands.has_permissions(administrator=True)
async def setunmutevc(ctx, action: str = None, channel: discord.VoiceChannel = None):
    """
    Manage self-service unmute voice channels — a member who's currently
    VC server-muted and/or server-deafened and joins one instantly gets
    both cleared, no staff needed, then gets bounced back out so the
    channel's free for the next person. Register the actual VC(s) you've
    already created for this (create as many as you want — e.g. two
    duplicate "unmute me" VCs for capacity).

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
        await ctx.send(f"✅ {channel.mention} is now a self-service unmute VC — joining it removes the Muted role instantly.")
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
@commands.has_permissions(manage_nicknames=True)
async def nickname(ctx, member: discord.Member, *, new_nick: str = None):
    if member.top_role >= ctx.guild.me.top_role:
        await ctx.send("❌ I can't change that user's nickname because their role is higher than mine.")
        return
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
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to change that member's nickname.")


@bot.command()
@commands.has_permissions(manage_channels=True)
async def hide(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, view_channel=False)
    await ctx.send(f"👻 {ctx.channel.mention} is now hidden from everyone.")
    await log(ctx.guild, "hides", "Channel Hidden", None, discord.Color.orange(),
              fields=[("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("👁️ Channel", ctx.channel.mention, True)],
              actor=ctx.author)


@bot.command()
@commands.has_permissions(manage_channels=True)
async def unhide(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, view_channel=True)
    await ctx.send(f"👀 {ctx.channel.mention} is now visible to everyone.")
    await log(ctx.guild, "hides", "Channel Unhidden", None, discord.Color.green(),
              fields=[("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("👁️ Channel", ctx.channel.mention, True)],
              actor=ctx.author)


@bot.command()
@commands.has_permissions(manage_channels=True)
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
@commands.has_permissions(manage_messages=True)
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
@commands.has_permissions(administrator=True)
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
@commands.has_permissions(administrator=True)
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
@commands.has_permissions(administrator=True)
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
@commands.has_permissions(administrator=True)
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
@commands.has_permissions(administrator=True)
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
@commands.has_permissions(administrator=True)
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
@commands.has_permissions(administrator=True)
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
@commands.has_permissions(administrator=True)
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
@commands.has_permissions(administrator=True)
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
        color=discord.Color.gold(),
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
@commands.has_permissions(administrator=True)
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
@commands.has_permissions(administrator=True)
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


# ============================================================
# VANITY URL ROLE TRACKING
# ============================================================

@bot.command()
@commands.has_permissions(administrator=True)
async def setvanityrole(ctx, role: discord.Role = None):
    """
    Set the role auto-granted to members who put this server's vanity
    invite link (discord.gg/<code>) in their custom status. Scoped to
    THIS server only. Run with no argument to disable the feature here.
    Usage: ,setvanityrole @role
    """
    guild = ctx.guild
    if role is None:
        VANITY_ROLE.pop(guild.id, None)
        _save_vanity_role()
        embed = discord.Embed(
            title="↩️ Vanity Role Disabled",
            description="Vanity URL role tracking is now **off** for this server.",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow()
        )
    else:
        VANITY_ROLE[guild.id] = role.id
        _save_vanity_role()
        code = _resolve_vanity_code(guild)
        code_note = f"`discord.gg/{code}`" if code else "*(no vanity code set or detected — set one with `,setvanitycode`)*"
        embed = discord.Embed(
            title="✅ Vanity Role Set",
            description=(
                f"Members with **{code_note}** in their custom status will now automatically "
                f"receive {role.mention}, and lose it when it's removed."
            ),
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
    embed.set_footer(text=f"Set by {ctx.author} • TrapAI", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def setvanitycode(ctx, code: str = None):
    """
    Manually set/override the vanity invite code to look for in members'
    statuses (the part after discord.gg/). Only needed if this server
    doesn't have Discord's native boosted vanity URL. Scoped to THIS
    server only. Run with no argument to clear the override and fall
    back to this server's native vanity URL (if any).
    Usage: ,setvanitycode ballin
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
@commands.has_permissions(manage_guild=True)
async def vanityconfig(ctx):
    """Show this server's current vanity role tracking configuration. Usage: ,vanityconfig"""
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
@commands.has_permissions(administrator=True)
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

    invite_txt = GUILD_INVITE.get(guild.id) or "*Not set*"

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
    winners  = _random.sample(entries, n_win) if entries else []
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
@commands.has_permissions(manage_guild=True)
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
        color=discord.Color.gold(),
        timestamp=ends_dt
    )
    embed.add_field(name="🎟️ Entries",  value="0",              inline=True)
    embed.add_field(name="🏆 Winners",   value=str(winners),     inline=True)
    embed.add_field(name="🎙️ Hosted By", value=ctx.author.mention, inline=True)
    embed.set_footer(text=f"TrapAI Giveaway System • {ctx.guild.name} | Ends at")
    if ctx.guild.icon:
        embed.set_thumbnail(url=ctx.guild.icon.url)

    msg = await ctx.send(embed=embed, view=GiveawayView())

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
@commands.has_permissions(manage_guild=True)
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
@commands.has_permissions(manage_guild=True)
async def giveaways(ctx):
    """List all active giveaways. Usage: ,giveaways"""
    active = [(mid, d) for mid, d in GIVEAWAYS.items() if d["guild_id"] == ctx.guild.id]
    if not active:
        await ctx.send("📭 No active giveaways right now.")
        return
    embed = discord.Embed(title="🎉 Active Giveaways", color=discord.Color.gold(), timestamp=discord.utils.utcnow())
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
@commands.has_permissions(manage_messages=True)
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
@commands.has_permissions(manage_messages=True)
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
@commands.has_permissions(manage_messages=True)
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
    "in-progress": ("⚙️",  "In Progress", discord.Color.gold()),
    "done":        ("✅", "Done",        discord.Color.green()),
    "blocked":     ("🚫", "Blocked",     discord.Color.red()),
    "review":      ("🔍", "In Review",   discord.Color.from_rgb(114, 137, 218)),
}


def _task_embed(task: dict, guild: discord.Guild) -> discord.Embed:
    s_icon, s_label, _ = TASK_STATUSES.get(task["status"], ("📋", task["status"], discord.Color.blurple()))
    p_icon, p_label, p_color = TASK_PRIORITIES.get(task["priority"], ("🟡", task["priority"], discord.Color.gold()))

    # Color driven by priority
    color = p_color

    assigned = guild.get_member(task["assigned_to"]) if task["assigned_to"] else None
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
        task["assigned_to"] = uid
        task["updated_at"]  = discord.utils.utcnow()
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
@commands.has_permissions(manage_messages=True)
async def task(ctx, priority: str = "medium", assigned: discord.Member = None, *, title_and_desc: str):
    """
    Create a staff task card with full interactive buttons.

    Usage:
      ,task high @user Fix the verification flow — test it on mobile too
      ,task critical    Server is under raid — respond now
      ,task medium @mod Write the updated server rules

    Priority: low | medium | high | critical
    Separate title and description with  ` — `
    """
    priority = priority.lower()
    if priority not in TASK_PRIORITIES:
        await ctx.send("❌ Priority must be: `low`, `medium`, `high`, `critical`", delete_after=8)
        return

    if " — " in title_and_desc:
        title, description = title_and_desc.split(" — ", 1)
    else:
        title, description = title_and_desc[:100], ""

    now = discord.utils.utcnow()
    task_data = {
        "id":          _new_task_id(),
        "title":       title.strip()[:100],
        "description": description.strip()[:500],
        "assigned_to": assigned.id if assigned else None,
        "priority":    priority,
        "status":      "open",
        "created_by":  ctx.author.id,
        "created_at":  now,
        "updated_at":  now,
        "due_at":      None,
        "notes":       [],
    }
    TASKS.setdefault(ctx.guild.id, []).append(task_data)

    msg = await ctx.send(
        embed=_task_embed(task_data, ctx.guild),
        view=TaskView(task_data["id"], ctx.guild.id)
    )

    # DM assigned member
    if assigned:
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
@commands.has_permissions(manage_messages=True)
async def tasklist(ctx, filter_status: str = None):
    """
    View the staff task board.
    Usage: ,tasklist          — show all active tasks
           ,tasklist done     — show completed tasks
           ,tasklist blocked  — show only blocked tasks
           ,tasklist @user    — show tasks assigned to a specific member
    """
    all_tasks = TASKS.get(ctx.guild.id, [])

    # Filter by status keyword
    if filter_status and filter_status.lower() in TASK_STATUSES:
        guild_tasks = [t for t in all_tasks if t["status"] == filter_status.lower()]
        title_suffix = f" — {TASK_STATUSES[filter_status.lower()][1]}"
    elif filter_status:
        # Try to parse as a member mention/ID
        try:
            uid = int(filter_status.strip("<@!>"))
            guild_tasks = [t for t in all_tasks if t["assigned_to"] == uid]
            m = ctx.guild.get_member(uid)
            title_suffix = f" — {m.display_name if m else f'User {uid}'}"
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
        asgn     = ctx.guild.get_member(t["assigned_to"]) if t["assigned_to"] else None
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

            await log(guild, "mod", "Vouch-Role Request Approved", None, discord.Color.green(),
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
                await log(guild, "mod", "Vouch-Role Request Rejected", None, discord.Color.red(),
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
@commands.has_permissions(manage_messages=True)
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
            color=discord.Color.gold(),
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
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow()
        )
        confirm.add_field(name="👤 Member",   value=member.mention, inline=True)
        confirm.add_field(name="🏷️ Role",     value=role.mention,   inline=True)
        confirm.add_field(name="📝 Reason",   value=reason,         inline=False)
        confirm.set_footer(text=f"Awaiting owner approval • TrapAI • {ctx.guild.name}")
        await ctx.send(embed=confirm)

        await log(ctx.guild, "mod", "Vouch-Role Request Submitted", None, discord.Color.gold(),
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

    await log(ctx.guild, "mod", "Member Vouched", None, discord.Color.green(),
              fields=[
                  ("🛡 By",     f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("✅ For",    f"{member.mention} (`{member.id}`)",           True),
                  ("🔢 Score",  f"{score} / {threshold}",                      True),
                  ("📝 Reason", reason,                                         False),
              ],
              actor=ctx.author, target=member)


@bot.command()
@commands.has_permissions(manage_messages=True)
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

    await log(ctx.guild, "mod", "Vouch Removed", None, discord.Color.orange(),
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
        color=discord.Color.gold(),
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
@commands.has_permissions(administrator=True)
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
@commands.has_permissions(administrator=True)
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


@bot.command()
@commands.has_permissions(administrator=True)
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
@commands.has_permissions(administrator=True)
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


# ============================================================
# VOUCH EXTENDED COMMANDS
# ============================================================

@bot.command()
@commands.has_permissions(administrator=True)
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
        color=discord.Color.gold(),
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
@commands.has_permissions(administrator=True)
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

    await log(guild, "mod", "Vouch Request Cancelled", None, discord.Color.orange(),
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
@commands.has_permissions(administrator=True)
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
# HARD-BAN SYSTEM
# ============================================================

@bot.command()
@commands.has_permissions(ban_members=True)
async def hardban(ctx, user: discord.User, *, reason: str = "No reason provided"):
    """
    Permanently hard-ban a user — they will be instantly re-banned if they rejoin.
    Usage: ,hardban @user reason
    """
    guild = ctx.guild
    HARD_BANNED.setdefault(guild.id, {})[user.id] = reason
    _save_hard_banned()
    _log_mod_action(guild.id, user.id, "hardban", ctx.author, reason)

    # DM before banning so they receive the notification
    await _dm_action(user, guild, "hardban", ctx.author, reason)

    try:
        await guild.ban(user, reason=f"Hard-ban by {ctx.author}: {reason}", delete_message_days=1)
    except discord.HTTPException:
        pass  # Already banned or left — still record it

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
@commands.has_permissions(ban_members=True)
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


@bot.command()
@commands.has_permissions(ban_members=True)
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
@commands.has_permissions(manage_guild=True)
async def setinvite(ctx, invite_link: str = None):
    """
    Set the server's permanent invite link used in DM notifications.
    Usage: ,setinvite https://discord.gg/yourcode
    Run with no argument to view the current invite.
    """
    if invite_link is None:
        current = GUILD_INVITE.get(ctx.guild.id, "")
        embed = discord.Embed(
            title="🔗 Server Invite Link",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        if current:
            embed.add_field(name="Current Link", value=current, inline=False)
            embed.description = "This link is included in all moderation DMs."
        else:
            embed.description = (
                "No invite link set yet.\n"
                "Use `,setinvite https://discord.gg/yourcode` to set one."
            )
        embed.set_footer(text=f"TrapAI • {ctx.guild.name}")
        await ctx.send(embed=embed)
        return

    # Basic validation
    if not (invite_link.startswith("https://discord.gg/") or invite_link.startswith("discord.gg/")):
        await ctx.send("❌ That doesn't look like a Discord invite. Use `https://discord.gg/yourcode`.", delete_after=8)
        return

    GUILD_INVITE[ctx.guild.id] = invite_link
    _save_guild_invite()
    embed = discord.Embed(
        title="✅ Server Invite Set",
        description=(
            f"Invite link updated to:\n**{invite_link}**\n\n"
            "This link will now appear in all moderation DMs (ban, kick, jail, timeout, hard-ban)."
        ),
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"Set by {ctx.author} • TrapAI", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)
    await log(ctx.guild, "mod", "Server Invite Link Set", None, discord.Color.green(),
              fields=[
                  ("🛡 Admin",  f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("🔗 Link",   invite_link,                                   True),
              ],
              actor=ctx.author)


@bot.command()
@commands.has_permissions(manage_messages=True)
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

    invite = GUILD_INVITE.get(ctx.guild.id, "")

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
        embed.add_field(name="⚠️ No invite set", value="Ask a staff member to set one with `,setinvite`", inline=False)

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
@commands.has_permissions(manage_messages=True)
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
                "gold":   discord.Color.gold(),
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
        color = discord.Color.gold()
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

def _eco(guild_id: int, user_id: int) -> dict:
    return ECONOMY.setdefault(guild_id, {}).setdefault(user_id, {"wallet": 0, "bank": 0})

def _add_wallet(guild_id, user_id, amount):
    _eco(guild_id, user_id)["wallet"] += amount

def _eco_embed(member, guild_id):
    data = _eco(guild_id, member.id)
    embed = discord.Embed(
        title=f"💰 {member.display_name}'s Balance",
        color=discord.Color.gold(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="👛 Wallet", value=f"**{data['wallet']:,}** coins", inline=True)
    embed.add_field(name="🏦 Bank",   value=f"**{data['bank']:,}** coins",   inline=True)
    embed.add_field(name="💎 Total",  value=f"**{data['wallet']+data['bank']:,}** coins", inline=True)
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


# ── Work ─────────────────────────────────────────────────────
@bot.command()
async def work(ctx):
    cd = _on_cooldown(ctx.guild.id, ctx.author.id, "work", 3600)
    if cd:
        m, s = divmod(cd, 60)
        await ctx.send(f"⏳ You're tired. Come back in **{m}m {s}s**.", delete_after=8)
        return
    earned = random.randint(50, 250)
    _add_wallet(ctx.guild.id, ctx.author.id, earned)
    jobs = ["delivered packages", "fixed some code", "walked dogs", "cooked meals",
            "drove Uber", "sold lemonade", "wrote a report", "cleaned the streets"]
    embed = discord.Embed(
        title="💼 Work Complete",
        description=f"You **{random.choice(jobs)}** and earned **{earned:,} coins**!",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text="Come back in 1 hour • TrapAI Economy")
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
    _add_wallet(ctx.guild.id, ctx.author.id, earned)
    embed = discord.Embed(
        title="📅 Daily Reward",
        description=f"You claimed your daily reward of **{earned:,} coins**! Come back tomorrow.",
        color=discord.Color.gold(),
        timestamp=discord.utils.utcnow()
    )
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
    _add_wallet(ctx.guild.id, ctx.author.id, earned)
    embed = discord.Embed(
        title="📆 Weekly Reward",
        description=f"You claimed your weekly reward of **{earned:,} coins**! Come back next week.",
        color=discord.Color.from_rgb(255, 215, 0),
        timestamp=discord.utils.utcnow()
    )
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
        await ctx.send(f"❌ You only have **{data['wallet']:,}** coins in your wallet.", delete_after=6)
        return
    data["wallet"] -= amt
    data["bank"]   += amt
    await ctx.send(f"🏦 Deposited **{amt:,} coins** into your bank. Bank: **{data['bank']:,}**")


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
        await ctx.send(f"❌ You only have **{data['bank']:,}** coins in your bank.", delete_after=6)
        return
    data["bank"]   -= amt
    data["wallet"] += amt
    await ctx.send(f"👛 Withdrew **{amt:,} coins** to your wallet. Wallet: **{data['wallet']:,}**")


# ── Give ─────────────────────────────────────────────────────
@bot.command(aliases=["pay", "transfer"])
async def give(ctx, member: discord.Member = None, amount: int = None):
    if member is None or amount is None or amount <= 0:
        await ctx.send("❌ Usage: `,give @user <amount>`", delete_after=6)
        return
    if member == ctx.author:
        await ctx.send("❌ You can't give coins to yourself.", delete_after=6)
        return
    data = _eco(ctx.guild.id, ctx.author.id)
    if amount > data["wallet"]:
        await ctx.send(f"❌ You only have **{data['wallet']:,}** coins in your wallet.", delete_after=6)
        return
    data["wallet"] -= amount
    _add_wallet(ctx.guild.id, member.id, amount)
    await ctx.send(f"✅ Sent **{amount:,} coins** to {member.mention}.")


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
        await ctx.send(f"🦹 You successfully robbed **{stolen:,} coins** from {member.mention}!")
    else:
        fine = random.randint(50, 200)
        robber["wallet"] = max(0, robber["wallet"] - fine)
        await ctx.send(f"🚔 You got caught! You paid a **{fine:,} coin** fine.")


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
        color=discord.Color.gold(),
        timestamp=discord.utils.utcnow()
    )
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    lines = []
    for i, (uid, data) in enumerate(sorted_users):
        member = ctx.guild.get_member(uid)
        name = member.display_name if member else f"<@{uid}>"
        total = data["wallet"] + data["bank"]
        lines.append(f"{medals[i]} **{name}** — {total:,} coins")
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
        lines.append(f"{medals[i]} **{name}** — {sign}{net:,} coins")
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
        await ctx.send(f"❌ You only have **{data['wallet']:,}** coins.", delete_after=6)
        return
    reels = [random.choice(SLOT_SYMBOLS) for _ in range(3)]
    data["wallet"] -= bet
    if reels[0] == reels[1] == reels[2]:
        mult = SLOT_MULTIPLIERS[reels[0]]
        win = bet * mult
        data["wallet"] += win
        GAMBLE_WINS.setdefault(ctx.guild.id, {})[ctx.author.id] = \
            GAMBLE_WINS.setdefault(ctx.guild.id, {}).get(ctx.author.id, 0) + (win - bet)
        result = f"🎉 **JACKPOT!** `{' '.join(reels)}` — Won **{win:,} coins** (×{mult})!"
        color = discord.Color.gold()
    elif reels[0] == reels[1] or reels[1] == reels[2]:
        win = bet
        data["wallet"] += win
        result = f"😊 **Small Win!** `{' '.join(reels)}` — Got your bet back!"
        color = discord.Color.green()
    else:
        GAMBLE_WINS.setdefault(ctx.guild.id, {})[ctx.author.id] = \
            GAMBLE_WINS.setdefault(ctx.guild.id, {}).get(ctx.author.id, 0) - bet
        result = f"😢 **Lost!** `{' '.join(reels)}` — Lost **{bet:,} coins**."
        color = discord.Color.red()
    embed = discord.Embed(title="🎰 Slot Machine", description=result, color=color, timestamp=discord.utils.utcnow())
    embed.add_field(name="👛 Wallet", value=f"{data['wallet']:,} coins", inline=True)
    embed.set_footer(text=f"Bet: {bet:,} • TrapAI Casino")
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
        await ctx.send(f"❌ You only have **{data['wallet']:,}** coins.", delete_after=6)
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
            f"{'✅ You **won** ' if won else '❌ You **lost** '}**{bet:,} coins**!"
        ),
        color=color,
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="👛 Wallet", value=f"{data['wallet']:,} coins", inline=True)
    embed.set_footer(text=f"Bet: {bet:,} • TrapAI Casino")
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
        await ctx.send(f"❌ You only have **{data['wallet']:,}** coins.", delete_after=6)
        return
    roll = random.randint(1, 6)
    dice_faces = {1:"1️⃣", 2:"2️⃣", 3:"3️⃣", 4:"4️⃣", 5:"5️⃣", 6:"6️⃣"}
    if roll == guess:
        win = bet * 5
        data["wallet"] += win
        GAMBLE_WINS.setdefault(ctx.guild.id, {})[ctx.author.id] = \
            GAMBLE_WINS.setdefault(ctx.guild.id, {}).get(ctx.author.id, 0) + win
        desc = f"{dice_faces[roll]} Rolled **{roll}** — You guessed right! Won **{win:,} coins** (×5)!"
        color = discord.Color.gold()
    else:
        data["wallet"] -= bet
        GAMBLE_WINS.setdefault(ctx.guild.id, {})[ctx.author.id] = \
            GAMBLE_WINS.setdefault(ctx.guild.id, {}).get(ctx.author.id, 0) - bet
        desc = f"{dice_faces[roll]} Rolled **{roll}** — You guessed **{guess}**. Lost **{bet:,} coins**."
        color = discord.Color.red()
    embed = discord.Embed(title="🎲 Dice Roll", description=desc, color=color, timestamp=discord.utils.utcnow())
    embed.add_field(name="👛 Wallet", value=f"{data['wallet']:,} coins", inline=True)
    embed.set_footer(text=f"Bet: {bet:,} • TrapAI Casino")
    await ctx.send(embed=embed)


# ── High-Low ─────────────────────────────────────────────────
@bot.command(aliases=["hl"])
async def highlow(ctx, bet: int = None):
    if bet is None or bet <= 0:
        await ctx.send("❌ Usage: `,highlow <bet>`", delete_after=6)
        return
    data = _eco(ctx.guild.id, ctx.author.id)
    if bet > data["wallet"]:
        await ctx.send(f"❌ You only have **{data['wallet']:,}** coins.", delete_after=6)
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
    embed.set_footer(text=f"Bet: {bet:,} • TrapAI Casino")
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
            f"{'✅ Correct! Won' if won else '❌ Wrong! Lost'} **{bet:,} coins**!"
        ),
        color=color,
        timestamp=discord.utils.utcnow()
    )
    embed2.add_field(name="👛 Wallet", value=f"{data['wallet']:,} coins", inline=True)
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
        await ctx.send(f"❌ You only have **{data['wallet']:,}** coins.", delete_after=6)
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
        e.set_footer(text=f"Bet: {bet:,}  •  Hit: `h`  Stand: `s`  •  TrapAI Casino")
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
        await ctx.send(f"❌ You only have **{data['wallet']:,}** coins.", delete_after=6)
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
    embed.set_footer(text=f"Bet: {bet:,} • TrapAI Casino")
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
                description=f"Cashed out at **×{multiplier:.2f}** — Won **{won:,} coins**!",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow()
            )
            fin.add_field(name="👛 Wallet", value=f"{data['wallet']:,} coins", inline=True)
            await ctx.send(embed=fin)
            return
        except asyncio.TimeoutError:
            pass

    GAMBLE_WINS.setdefault(ctx.guild.id, {})[ctx.author.id] = \
        GAMBLE_WINS.setdefault(ctx.guild.id, {}).get(ctx.author.id, 0) - bet
    fin = discord.Embed(
        title="💥 Crashed!",
        description=f"The rocket crashed at **×{multiplier:.2f}**. You lost **{bet:,} coins**!",
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow()
    )
    fin.add_field(name="👛 Wallet", value=f"{data['wallet']:,} coins", inline=True)
    await ctx.send(embed=fin)


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
        description=f"**{question}**\n\nYou have **30 seconds** to answer! Correct = **{reward} coins**",
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
        _add_wallet(ctx.guild.id, ctx.author.id, reward)
        await ctx.send(f"✅ Correct! You earned **{reward} coins**! 🎉")
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
        f"You have **{attempts} attempts**. Correct = **{reward} coins**!"
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
            _add_wallet(ctx.guild.id, ctx.author.id, reward)
            await ctx.send(f"🎉 **Correct in {attempt} attempt(s)!** You earned **{reward} coins**!")
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
        f"{HANGMAN_STAGES[0]}\n`{display()}`\nWrong: 0/{max_wrong} | Correct = **{reward} coins**"
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
                _add_wallet(ctx.guild.id, ctx.author.id, reward)
                await ctx.send(
                    f"{HANGMAN_STAGES[wrong]}\n✅ **You got it!** The word was **{word}**!\n"
                    f"Earned **{reward} coins**! 🎉"
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
        ("💰", "Economy",        "Earn, save, spend & transfer coins",                  "work · daily · weekly · balance · deposit · withdraw · give · rob"),
        ("🎰", "Casino",         "Gamble your coins in high-stakes games",              "slots · coinflip · blackjack · dice · crash · highlow"),
        ("🎯", "Fun Games",      "Casual games — no bet needed, coins for winning",     "rps · trivia · hangman · numguess · 8ball · tictactoe"),
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
        title="💰  Economy — Earn & Manage Coins",
        description="Build your fortune, save it, spend it, or steal it.\nCoins are stored **per server** — separate on every Discord.",
        color=discord.Color.gold(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(
        name="📊  Check Balance",
        value=(
            "`,balance` / `,bal`\n"
            "Shows your **wallet** (spendable) and **bank** (safe) balance.\n"
            "`,balance @user` — view someone else's balance."
        ),
        inline=False
    )
    embed.add_field(
        name="💼  Earn Coins",
        value=(
            "`,work`  **50–250 coins**  ·  ⏳ 1 hour cooldown\n"
            "`,daily`  **200–500 coins**  ·  ⏳ 24 hour cooldown\n"
            "`,weekly`  **1,000–2,500 coins**  ·  ⏳ 7 day cooldown"
        ),
        inline=False
    )
    embed.add_field(
        name="🏦  Banking",
        value=(
            "`,deposit <amount|all>` — move coins from wallet → bank (safe from robbery)\n"
            "`,withdraw <amount|all>` — move coins from bank → wallet (to spend)"
        ),
        inline=False
    )
    embed.add_field(
        name="🤝  Transfer",
        value=(
            "`,give @user <amount>` / `,pay` / `,transfer`\n"
            "Send coins directly to another member's wallet."
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
        description="Bet your wallet coins. You can win big — or lose it all.\n⚠️ **Only coins in your wallet can be bet** — bank is safe.",
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
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text=f"TrapAI Casino  •  {guild.name}  •  Gamble responsibly")
    return embed


def _games_fun_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="🎯  Fun Games — Play & Earn",
        description="No bets required — just play and win coins for correct answers!",
        color=discord.Color.from_rgb(88, 101, 242),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(
        name="✊✋✌️  Rock Paper Scissors  —  `,rps <choice>`",
        value=(
            "Beat the bot at rock paper scissors.\n"
            "Choices: `rock` / `paper` / `scissors`  (or `r` / `p` / `s`)\n"
            "No coins involved — just glory."
        ),
        inline=False
    )
    embed.add_field(
        name="🧠  Trivia  —  `,trivia`",
        value=(
            "Answer a random question correctly within **30 seconds**.\n"
            "**Reward: 30–100 coins** for a correct answer!\n"
            "20 questions across science, geography, history & more."
        ),
        inline=False
    )
    embed.add_field(
        name="🪢  Hangman  —  `,hangman`",
        value=(
            "Guess a hidden word one letter at a time.\n"
            "6 wrong guesses allowed before you're hanged.\n"
            "**Reward: 200 coins** for guessing the word!"
        ),
        inline=False
    )
    embed.add_field(
        name="🔢  Number Guess  —  `,numguess` / `,guess`",
        value=(
            "Guess a number between **1 and 100** in 7 attempts.\n"
            "The bot tells you if you're too high or too low.\n"
            "**Reward: 150 coins** for guessing correctly!"
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
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text=f"TrapAI Fun Games  •  {guild.name}")
    return embed


def _games_lb_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="🏆  Leaderboards",
        description="See who's on top across all economy and casino activity.",
        color=discord.Color.from_rgb(255, 215, 0),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(
        name="💎  Richest Members  —  `,leaderboard` / `,lb`",
        value=(
            "Top 10 members ranked by **wallet + bank** total.\n"
            "Earn coins via `,work`, `,daily`, `,weekly`, and casino wins."
        ),
        inline=False
    )
    embed.add_field(
        name="🎰  Top Gamblers  —  `,gamblers`",
        value=(
            "Top 10 members ranked by **net casino winnings**.\n"
            "Shows total coins won minus coins lost across all casino games.\n"
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
            "• Answer `,trivia` for free coins"
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

bot.run(DISCORD_TOKEN)
