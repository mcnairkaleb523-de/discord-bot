from dotenv import load_dotenv
load_dotenv()

import os
import sys
import time
import asyncio
import json
import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timedelta

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True
intents.invites = True

bot = commands.Bot(command_prefix=",", intents=intents)

BAD_WORDS = ["badword1", "badword2"]
LINKS = ["http", "https", "discord.gg"]

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
}

# Per-guild channel overrides: LOG_CHANNEL_OVERRIDES[guild_id][key] = channel_id
# Set via ,setlogchannel — takes priority over the name-based lookup above.
LOG_CHANNEL_OVERRIDES: dict[int, dict[str, int]] = {}

WELCOME_CHANNEL = "welcome"
ANNOUNCEMENTS_CHANNEL = "announcements"

# Milestones that trigger an announcement (member counts)
MEMBER_MILESTONES = {
    10, 25, 50, 100, 150, 200, 250, 300, 400, 500,
    750, 1000, 1500, 2000, 2500, 3000, 4000, 5000,
    7500, 10000, 15000, 20000, 25000, 50000, 100000,
}

UNVERIFIED_ROLE = "🚫 Unverified"
VERIFIED_ROLE = "✅ Hood Member"
JAIL_ROLE = "🔒 Jailed"
MUTED_ROLE = "🔇 Muted"

JOIN_TO_CREATE_CHANNEL_NAME = "➕ Create VC"
TEMP_VC_CATEGORY_NAME = "🎤 Private VCs"

spam_tracker = {}
spam_warnings = {}

SPAM_MESSAGE_LIMIT = 5
SPAM_TIME_WINDOW = 6
SPAM_WARNING_LIMIT = 3
SPAM_TIMEOUT_MINUTES = 5

WHITELIST = set()
RAID_JOINS = []
RAID_TIME = 15
RAID_LIMIT = 5

vc_stats = {}
vc_join_time = {}

temp_vc_owners = {}
temp_vc_text_channels = {}
# vc_banned[vc_id] = {user_id, ...}  — users explicitly banned from a VC
vc_banned = {}
# vc_mods[vc_id] = {user_id, ...}  — users with VC-mod privileges
vc_mods = {}

ANTI_RAID_ENABLED = True
jailed_users = {}

# WARNINGS[guild_id][user_id] = [ {reason, moderator, moderator_id, time}, ... ]
WARNINGS = {}

# ── Chat stats ─────────────────────────────────────────────
# CHAT_STATS[guild_id][user_id] = message_count
CHAT_STATS: dict[int, dict[int, int]] = {}

# ── Invite tracking ────────────────────────────────────────
# INVITE_CACHE[guild_id] = { code: uses }
INVITE_CACHE: dict[int, dict[str, int]] = {}
# INVITE_DATA[guild_id][inviter_id] = { "uses": int, "logs": [str, ...] }
INVITE_DATA: dict[int, dict[int, dict]] = {}

# TICKETS[guild_id][user_id] = channel_id
TICKETS = {}

# TICKET_CLAIMED[channel_id] = member_id  — who claimed the ticket
TICKET_CLAIMED = {}

# TICKET_TYPE[channel_id] = str  — category label chosen at open
TICKET_TYPE = {}

# TICKET_PRIORITY[channel_id] = str  — "🟢 Low" | "🟡 Medium" | "🔴 High" | "🚨 Critical"
TICKET_PRIORITY: dict[int, str] = {}

# TICKET_LOCKED[channel_id] = bool  — whether the ticket is locked for the opener
TICKET_LOCKED: dict[int, bool] = {}

# ── Vouch system ────────────────────────────────────────────
# VOUCHES[guild_id][user_id] = count (net vouch score)
VOUCHES: dict[int, dict[int, int]] = {}
# VOUCH_LOG[guild_id][user_id] = [ {by, by_id, action, time}, ... ]
VOUCH_LOG: dict[int, dict[int, list]] = {}
# VOUCH_CONFIG[guild_id] = { "threshold": int }
VOUCH_CONFIG: dict[int, dict] = {}

# ── Protected roles system ───────────────────────────────────
# PROTECTED_ROLES[guild_id] = {role_id, ...}
# Roles in this set can ONLY be granted via ,vouch — manual grants are auto-stripped.
PROTECTED_ROLES: dict[int, set] = {}

# Pending vouch-role requests awaiting owner approval
# ROLE_VOUCH_PENDING[guild_id][token] = {
#   "member_id", "role_id", "requester_id", "reason", "message_id", "channel_id"
# }
ROLE_VOUCH_PENDING: dict[int, dict[str, dict]] = {}

# ── Auto-role system ─────────────────────────────────────────
# AUTOROLE[guild_id] = [role_id, ...]  — roles given to every new member on join
AUTOROLE: dict[int, list] = {}

# Temp whitelist so on_member_update knows a grant is bot-approved
# _VOUCH_ROLE_APPROVED[guild_id] = {(member_id, role_id), ...}
_VOUCH_ROLE_APPROVED: dict[int, set] = {}

# ── Hard-ban system ─────────────────────────────────────────
# HARD_BANNED[guild_id] = { user_id: reason }
HARD_BANNED: dict[int, dict[int, str]] = {}

# ── Role snapshots (strip → restore) ────────────────────────
# ROLE_SNAPSHOTS[guild_id][user_id] = [role_id, ...]
ROLE_SNAPSHOTS: dict[int, dict[int, list]] = {}

# ── Anti-nuke tracker ────────────────────────────────────────
# NUKE_TRACKER[guild_id][user_id] = [timestamp, ...]
NUKE_TRACKER: dict[int, dict[int, list]] = {}
NUKE_ROLE_LIMIT   = 3   # max role deletes within window
NUKE_CHAN_LIMIT   = 3   # max channel deletes within window
NUKE_WINDOW       = 10  # seconds

# ── Server invite config ─────────────────────────────────────
# GUILD_INVITE[guild_id] = "https://discord.gg/..."
GUILD_INVITE: dict[int, str] = {}

# ── Welcome config ───────────────────────────────────────────
# WELCOME_CONFIG[guild_id] = { "channel_id": int, "enabled": bool }
WELCOME_CONFIG: dict[int, dict] = {}


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
    "vc":           "🎤", "messages":   "💬", "tickets":    "🎫",
    "nicknames":    "✏️",  "channel_create":"📁","channel_delete":"🗑️",
    "channel_update":"🔧","emoji":      "😀", "stickers":   "🖼️",
    "lockdowns":    "🔐", "unlockdowns":"🔓", "clears":     "🧹",
    "purges":       "🗑️",  "hides":      "👁️",  "strips":     "⚔️",
    "massroles":    "📦", "roleall":    "📢", "invites":    "📨",
}

# Color palette per action type (override color arg when set)
_ACTION_COLORS = {
    "join":     0x2ECC71,  # green
    "leave":    0xE74C3C,  # red
    "ban":      0x992D22,  # dark red
    "kick":     0xE67E22,  # orange
    "timeout":  0xF1C40F,  # gold
    "mute":     0xE74C3C,
    "unmute":   0x2ECC71,
    "warn":     0xE67E22,
    "jail":     0x992D22,
    "unjail":   0x2ECC71,
    "mod":      0x3498DB,  # blue
    "create":   0x2ECC71,
    "delete":   0xE74C3C,
    "update":   0xF1C40F,
    "boost":    0xFF73FA,
    "verify":   0x2ECC71,
    "unverify": 0xE67E22,
    "raid":     0xFF0000,
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
            icon_url=actor.display_avatar.url if hasattr(actor, "display_avatar") else discord.Embed.Empty
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
            LOG_CHANNELS["tickets"],
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
            LOG_CHANNELS["tickets"],
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
            LOG_CHANNELS["tickets"],
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

        await asyncio.sleep(5)

        # Clean up tracking
        if guild.id in TICKETS and owner_id:
            TICKETS[guild.id].pop(owner_id, None)
        TICKET_CLAIMED.pop(channel.id, None)
        TICKET_TYPE.pop(channel.id, None)
        TICKET_PRIORITY.pop(channel.id, None)
        TICKET_LOCKED.pop(channel.id, None)

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
        lines = []
        async for msg in channel.history(limit=500, oldest_first=True):
            ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
            content = msg.content or ""
            embeds_note = f" [+{len(msg.embeds)} embed(s)]" if msg.embeds else ""
            lines.append(f"[{ts}] {msg.author} ({msg.author.id}): {content}{embeds_note}")
        text = "\n".join(lines) or "(no messages)"
        ticket_type = TICKET_TYPE.get(channel.id, "ticket")
        filename = f"transcript-{channel.name}.txt"
        file = discord.File(
            fp=__import__("io").BytesIO(text.encode()),
            filename=filename
        )
        await interaction.followup.send(
            content=f"📄 Transcript for **{channel.name}** (`{len(lines)}` messages):",
            file=file,
            ephemeral=True
        )
        await log(
            interaction.guild,
            LOG_CHANNELS["tickets"],
            "Ticket Transcript Saved",
            (
                f"**Channel:** {channel.mention}\n"
                f"**Saved By:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                f"**Messages:** {len(lines)}\n"
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
        embed = discord.Embed(
            description=f"Priority set to **{label}** for this ticket.",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Set by {interaction.user}")
        await interaction.response.send_message(embed=embed)
        await log(
            interaction.guild,
            LOG_CHANNELS["tickets"],
            "Ticket Priority Set",
            (
                f"**Channel:** {interaction.channel.mention}\n"
                f"**Priority:** {label}\n"
                f"**Set By:** {interaction.user.mention} (`{interaction.user.id}`)"
            ),
            discord.Color.orange()
        )


# ============================================================
# VERIFY VIEW (persistent)
# ============================================================
class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Verify Now",
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
                LOG_CHANNELS["verification"],
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

TICKET_TYPES = {
    "general":   ("🎫 General Support",    "General questions or help",           discord.Color.blurple()),
    "report":    ("🚨 Report a Member",    "Report rule-breaking behaviour",       discord.Color.red()),
    "appeal":    ("📝 Ban / Mute Appeal",  "Appeal a moderation action",           discord.Color.orange()),
    "alliance":  ("🤝 Form a Alliance",    "Alliance or collab requests",          discord.Color.green()),
    "bug":       ("🐛 Bug Report",         "Report a bot or server bug",           discord.Color.dark_orange()),
    "unban":     ("🔓 Unban Request",      "Request to be unbanned",               discord.Color.dark_red()),
    "staff":     ("📋 Staff Application",  "Apply to join the staff team",         discord.Color.from_rgb(88, 101, 242)),
}


async def _create_ticket_channel(guild, member, ticket_key: str):
    """Create the ticket channel and post the control panel. Returns the channel."""
    label, description, color = TICKET_TYPES[ticket_key]

    guild_tickets = TICKETS.setdefault(guild.id, {})
    if member.id in guild_tickets:
        existing = guild.get_channel(guild_tickets[member.id])
        if existing:
            return None, existing  # already open

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

    guild_tickets[member.id] = ticket_channel.id
    TICKET_TYPE[ticket_channel.id] = label

    # Header embed
    embed = discord.Embed(
        title=f"{label}",
        description=(
            f"Welcome {member.mention}! 👋\n\n"
            f"**Category:** {label}\n"
            f"**About:** {description}\n\n"
            "A staff member will be with you shortly.\n"
            "Please describe your issue in detail.\n\n"
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
    await ticket_channel.send(member.mention, delete_after=3)

    await log(
        guild,
        LOG_CHANNELS["tickets"],
        "Ticket Opened",
        (
            f"**User:** {member.mention} (`{member.id}`)\n"
            f"**Channel:** {ticket_channel.mention}\n"
            f"**Type:** {label}"
        ),
        color
    )

    return ticket_channel, None


class TicketTypeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label="General Support",
                value="general",
                emoji="🎫",
                description="General questions or help"
            ),
            discord.SelectOption(
                label="Report a Member",
                value="report",
                emoji="🚨",
                description="Report rule-breaking behaviour"
            ),
            discord.SelectOption(
                label="Ban / Mute Appeal",
                value="appeal",
                emoji="📝",
                description="Appeal a moderation action"
            ),
            discord.SelectOption(
                label="Form a Alliance",
                value="alliance",
                emoji="🤝",
                description="Alliance or collab requests"
            ),
            discord.SelectOption(
                label="Bug Report",
                value="bug",
                emoji="🐛",
                description="Report a bot or server bug"
            ),
            discord.SelectOption(
                label="Unban Request",
                value="unban",
                emoji="🔓",
                description="Request to be unbanned"
            ),
            discord.SelectOption(
                label="Staff Application",
                value="staff",
                emoji="📋",
                description="Apply to join the staff team"
            ),
        ]
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

        if already:
            await interaction.followup.send(
                f"❌ You already have an open ticket: {already.mention}", ephemeral=True
            )
            return

        await interaction.followup.send(
            f"✅ Ticket created: {ticket_channel.mention}", ephemeral=True
        )


class TicketOpenView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketTypeSelect())


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
    new_name = discord.ui.TextInput(label="New name", placeholder="e.g. Hood Hangout", min_length=1, max_length=100)

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
        temp_vc_owners[self.vc.id] = member.id
        await self.vc.set_permissions(member, manage_channels=True, manage_permissions=True, move_members=True, connect=True, speak=True)
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
                "`,clearwarnings @user` — clear all warnings"
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
                "`,chatstats [@user]` — message leaderboard or per-user count\n"
                "`,vcstats [@user]` — total VC time this session\n"
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
        pending_count = sum(len(v) for v in ROLE_VOUCH_PENDING.get(guild.id, {}).values()) if isinstance(ROLE_VOUCH_PENDING.get(guild.id), dict) else len(ROLE_VOUCH_PENDING.get(guild.id, {}))
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
                "**Categories (7):** General • Report • Appeal • Form a Alliance • Bug • Unban • Staff App"
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
            "`,vctransfer` `,vcmod` `,vcremovemod`"
        ),
        inline=False
    )
    embed.add_field(
        name="📌 Notes",
        value=(
            "• Buttons announce every action in this chat automatically.\n"
            "• Members see 🟢 join / 🔴 leave messages here in real-time.\n"
            "• Both the VC and this chat are deleted when the VC empties.\n"
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


async def auto_unjail(guild_id: int, user_id: int, delay: int, reason: str = "Jail timer expired"):
    await asyncio.sleep(delay)

    guild = bot.get_guild(guild_id)
    if guild is None:
        jailed_users.pop(user_id, None)
        return

    member = guild.get_member(user_id)
    if member is None:
        jailed_users.pop(user_id, None)
        return

    jail_role = discord.utils.get(guild.roles, name=JAIL_ROLE)
    unverified_role = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE)

    if jail_role and jail_role in member.roles:
        try:
            await member.remove_roles(jail_role, reason=reason)
            if unverified_role and unverified_role not in member.roles:
                await member.add_roles(unverified_role, reason="Returned to unverified after jail")
            await _remove_jail_overwrites(member)
            await log(
                guild,
                LOG_CHANNELS["jail"],
                "Member Auto Unjailed",
                f"User: {member.mention}\nReason: {reason}",
                discord.Color.green()
            )
        except discord.Forbidden:
            await log(
                guild,
                LOG_CHANNELS["jail"],
                "Auto Unjail Failed",
                f"Could not unjail {member.mention}. Check bot permissions and role position.",
                discord.Color.red()
            )
        except discord.HTTPException:
            pass

    jailed_users.pop(user_id, None)


async def handle_spam(message):
    user_id = message.author.id
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
                LOG_CHANNELS["mod"],
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
                    LOG_CHANNELS["mod"],
                    "User Auto Timed Out",
                    f"User: {message.author.mention}\nReason: Reached {SPAM_WARNING_LIMIT} spam warnings\nDuration: {SPAM_TIMEOUT_MINUTES} minute(s)\nChannel: {message.channel.mention}",
                    discord.Color.red()
                )
            except discord.Forbidden:
                await log(
                    message.guild,
                    LOG_CHANNELS["mod"],
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
_milestone_channel_overrides: dict[int, int] = {}

# Track the last milestone fired per guild so rapid join/leave churn
# doesn't double-fire the same milestone
_last_milestone_fired: dict[int, int] = {}


async def _check_milestone(guild: discord.Guild):
    count = guild.member_count
    if count not in MEMBER_MILESTONES:
        return

    # Suppress duplicate fires for the same milestone
    if _last_milestone_fired.get(guild.id) == count:
        return
    _last_milestone_fired[guild.id] = count

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
# EVENTS
# ============================================================
@bot.event
async def on_ready():
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
            name="discord.gg/hood"
        )
    )
    # Cache current invite use-counts for all guilds
    for guild in bot.guilds:
        try:
            invites = await guild.invites()
            INVITE_CACHE[guild.id] = {inv.code: inv.uses for inv in invites}
        except (discord.Forbidden, discord.HTTPException):
            pass
    print(f"Logged in as {bot.user}")


@bot.event
async def on_member_join(member):
    # ── Hard-ban check: re-ban immediately if hard-banned ──────
    hb_guild = HARD_BANNED.get(member.guild.id, {})
    if member.id in hb_guild:
        reason = hb_guild[member.id]
        try:
            await member.ban(reason=f"Hard-ban re-applied: {reason}")
            await log(member.guild, LOG_CHANNELS["bans"], "🔴 Hard-Ban Re-Applied",
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

    if ANTI_RAID_ENABLED:
        now = time.time()
        RAID_JOINS.append(now)
        RAID_JOINS[:] = [t for t in RAID_JOINS if now - t <= RAID_TIME]

        if member.id not in WHITELIST and len(RAID_JOINS) >= RAID_LIMIT:
            await member.ban(reason="Anti-raid triggered")
            await log(
                member.guild,
                LOG_CHANNELS["raids"],
                "Anti-Raid Triggered",
                "A member was auto-banned by the anti-raid system.",
                discord.Color.red(),
                fields=[
                    ("🚨 Banned User",  f"{member.mention} (`{member.id}`)", True),
                    ("🏷️ Username",     str(member),                          True),
                    ("📅 Account Age",  discord.utils.format_dt(member.created_at, "R"), True),
                    ("👥 Raid Joins",   f"{len(RAID_JOINS)} in {RAID_TIME}s",  True),
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
                LOG_CHANNELS["mod"],
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
        LOG_CHANNELS["joins"],
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

    # Per-guild welcome config takes priority; fall back to the default channel name
    _wcfg = WELCOME_CONFIG.get(member.guild.id, {})
    if _wcfg.get("enabled", True):
        _wch_id = _wcfg.get("channel_id")
        ch = member.guild.get_channel(_wch_id) if _wch_id else discord.utils.get(
            member.guild.text_channels, name=WELCOME_CHANNEL
        )
    else:
        ch = None

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
    roles = [r.mention for r in member.roles if r.name != "@everyone"]
    await log(
        member.guild,
        LOG_CHANNELS["leaves"],
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
            LOG_CHANNELS["boost"],
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
                after.guild, LOG_CHANNELS["mod"],
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
                LOG_CHANNELS["roles"],
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
                LOG_CHANNELS["roles"],
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
            LOG_CHANNELS["nicknames"],
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
        LOG_CHANNELS["role_create"],
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
        LOG_CHANNELS["role_delete"],
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
            await log(guild, LOG_CHANNELS["mod"], "🚨 Anti-Nuke Triggered — Role Deletions", None,
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
        LOG_CHANNELS["channel_create"],
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
        LOG_CHANNELS["channel_delete"],
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
            await log(guild, LOG_CHANNELS["mod"], "🚨 Anti-Nuke Triggered — Channel Deletions", None,
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
            LOG_CHANNELS["channel_update"],
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
        await log(guild, LOG_CHANNELS["emoji"], "Emoji Added", None, discord.Color.green(),
                  fields=[("😀 Emoji", f"{emoji} `:{emoji.name}:`", True), ("🆔 ID", str(emoji.id), True)])

    for emoji in removed:
        await log(guild, LOG_CHANNELS["emoji"], "Emoji Removed", None, discord.Color.red(),
                  fields=[("🗑️ Name", f"`:{emoji.name}:`", True), ("🆔 ID", str(emoji.id), True)])


@bot.event
async def on_guild_stickers_update(guild, before, after):
    added   = [s for s in after if s not in before]
    removed = [s for s in before if s not in after]

    for sticker in added:
        await log(guild, LOG_CHANNELS["stickers"], "Sticker Added", None, discord.Color.green(),
                  fields=[("🖼️ Sticker", f"`{sticker.name}`", True), ("🆔 ID", str(sticker.id), True)])

    for sticker in removed:
        await log(guild, LOG_CHANNELS["stickers"], "Sticker Removed", None, discord.Color.red(),
                  fields=[("🗑️ Name", f"`{sticker.name}`", True), ("🆔 ID", str(sticker.id), True)])


@bot.event
async def on_message_delete(message):
    if not message.guild or message.author.bot:
        return
    content = message.content or "*(no text content)*"
    attachments = ", ".join(a.filename for a in message.attachments) if message.attachments else "None"
    await log(
        message.guild,
        LOG_CHANNELS["messages"],
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
async def on_message_edit(before, after):
    if before.author.bot or before.content == after.content or not before.guild:
        return
    await log(
        before.guild,
        LOG_CHANNELS["messages"],
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


@bot.event
async def on_voice_state_update(member, before, after):
    now = time.time()

    if before.channel is None and after.channel is not None:
        vc_join_time[member.id] = now
        await log(member.guild, LOG_CHANNELS["vc"], "VC Join", None, discord.Color.blue(),
                  fields=[
                      ("👤 Member",  f"{member.mention} (`{member.id}`)", True),
                      ("🎤 Channel", f"**{after.channel.name}**",         True),
                  ], target=member)

    elif before.channel is not None and after.channel is None:
        joined = vc_join_time.pop(member.id, None)
        duration = ""
        if joined:
            secs = int(now - joined)
            vc_stats[member.id] = vc_stats.get(member.id, 0) + secs
            h, rem = divmod(secs, 3600); m = rem // 60; s = rem % 60
            duration = f"{h}h {m}m {s}s"
        await log(member.guild, LOG_CHANNELS["vc"], "VC Left", None, discord.Color.red(),
                  fields=[
                      ("👤 Member",    f"{member.mention} (`{member.id}`)", True),
                      ("🎤 Channel",   f"**{before.channel.name}**",        True),
                      ("⏱️ Session",   duration or "—",                     True),
                  ], target=member)

    elif before.channel != after.channel and before.channel is not None and after.channel is not None:
        joined = vc_join_time.pop(member.id, None)
        if joined:
            vc_stats[member.id] = vc_stats.get(member.id, 0) + int(now - joined)
        vc_join_time[member.id] = now
        await log(member.guild, LOG_CHANNELS["vc"], "VC Moved", None, discord.Color.gold(),
                  fields=[
                      ("👤 Member", f"{member.mention} (`{member.id}`)", True),
                      ("📤 From",   f"**{before.channel.name}**",        True),
                      ("📥 To",     f"**{after.channel.name}**",         True),
                  ], target=member)

    guild = member.guild

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
            # Post join announcement
            join_embed = discord.Embed(
                description=f"🟢 **{member.display_name}** joined the VC.",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow()
            )
            join_embed.set_thumbnail(url=member.display_avatar.url)
            join_embed.set_footer(text="TrapAI VC System")
            try:
                await text_channel.send(embed=join_embed)
            except discord.HTTPException:
                pass

    # ── Member left a temp VC ────────────────────────────────
    if (before.channel and before.channel.id in temp_vc_owners
            and (after.channel is None or after.channel.id != before.channel.id)):
        text_id = temp_vc_text_channels.get(before.channel.id)
        text_channel = guild.get_channel(text_id) if text_id else None
        if text_channel:
            # Post leave announcement
            leave_embed = discord.Embed(
                description=f"🔴 **{member.display_name}** left the VC.",
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow()
            )
            leave_embed.set_thumbnail(url=member.display_avatar.url)
            leave_embed.set_footer(text="TrapAI VC System")
            try:
                await text_channel.send(embed=leave_embed)
            except discord.HTTPException:
                pass
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
                view_channel=True
            )
        }

        vc_name = f"{member.display_name}'s VC"
        text_name = f"vc-{member.name}".lower().replace(" ", "-")

        new_vc = await guild.create_voice_channel(
            name=vc_name,
            category=category,
            overwrites=overwrites,
            reason="Join to create VC"
        )

        text_overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        }

        new_text = await guild.create_text_channel(
            name=text_name,
            category=category,
            overwrites=text_overwrites,
            reason="Join to create VC text chat"
        )

        temp_vc_owners[new_vc.id] = member.id
        temp_vc_text_channels[new_vc.id] = new_text.id

        await member.move_to(new_vc)
        await send_vc_control_panel(new_text, member, new_vc)

        await log(
            guild,
            LOG_CHANNELS["vc"],
            "Temporary VC Created",
            f"Owner: {member.mention}\nVC: **{new_vc.name}**\nText Chat: {new_text.mention}",
            discord.Color.green()
        )

    # Clean up empty temp VC
    if before.channel and before.channel.id in temp_vc_owners:
        if len(before.channel.members) == 0:
            text_id = temp_vc_text_channels.get(before.channel.id)
            text_channel = guild.get_channel(text_id) if text_id else None

            try:
                if text_channel:
                    await text_channel.delete(reason="Temp VC empty")
            except discord.HTTPException:
                pass

            try:
                old_name = before.channel.name
                await before.channel.delete(reason="Temp VC empty")
                await log(
                    guild,
                    LOG_CHANNELS["vc"],
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
    # Staff (manage_messages+) and command invocations are exempt from automod
    is_command = message.content.startswith(bot.command_prefix)
    is_staff = (
        isinstance(message.author, discord.Member)
        and (
            message.author.guild_permissions.manage_messages
            or message.author.guild_permissions.administrator
        )
    )

    if not is_command and not is_staff:
        msg = message.content.lower()
        if any(w in msg for w in BAD_WORDS) or any(l in msg for l in LINKS):
            try:
                await message.delete()
                await log(
                    message.guild,
                    LOG_CHANNELS["messages"],
                    "AutoMod Deleted Message",
                    f"Author: {message.author}\nChannel: {message.channel.mention}\nContent: {message.content or 'None'}",
                    discord.Color.orange()
                )
            except discord.Forbidden:
                await log(
                    message.guild,
                    LOG_CHANNELS["mod"],
                    "AutoMod Delete Failed",
                    f"Could not delete a message from {message.author.mention} in {message.channel.mention}. Check permissions.",
                    discord.Color.red()
                )
            except discord.HTTPException:
                pass
            return

        is_spamming = await handle_spam(message)
        if is_spamming:
            return

    await bot.process_commands(message)


# ============================================================
# GLOBAL ERROR HANDLER
# ============================================================
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use that command.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ I'm missing the required permissions to do that.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: `{error.param.name}`. Use `,cmds` to see usage.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Invalid argument provided. Make sure you're mentioning a valid user or role.")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ Member not found. Make sure you mention them correctly.")
    elif isinstance(error, commands.RoleNotFound):
        await ctx.send("❌ Role not found. Make sure the role name or mention is correct.")
    elif isinstance(error, commands.CommandNotFound):
        pass  # silently ignore unknown commands
    elif isinstance(error, commands.CheckFailure):
        await ctx.send("❌ You don't have permission to use that command.")
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

async def _send_welcome_embeds(channel, member: discord.Member):
    """Send the full welcome card into `channel` for `member`."""
    guild       = member.guild
    count       = guild.member_count
    invite      = GUILD_INVITE.get(guild.id, "")
    created_str = discord.utils.format_dt(member.created_at, "F")
    age_str     = discord.utils.format_dt(member.created_at, "R")

    def _ordinal(n: int) -> str:
        s = ["th", "st", "nd", "rd"]
        v = n % 100
        return f"{n:,}{s[min(v - 20, v, 3) if v > 3 else 0] if v > 20 else s[min(v, 3)]}"

    banner = discord.Embed(
        description=(
            f"# 🏘️ Welcome to **{guild.name}**, {member.mention}!\n\n"
            f"You are our **{_ordinal(count)} member** — glad you made it.\n"
            f"**{guild.name}** is live, active, and always moving.\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"**Get started in 3 steps:**\n"
            f"> **1.** Read the rules\n"
            f"> **2.** Head to **#verify** and click **Verify Now**\n"
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
    ch = discord.utils.get(ctx.guild.text_channels, name=WELCOME_CHANNEL)
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
            embed = discord.Embed(
                description="❌ Auto-welcome has been **disabled**. New members will not receive a welcome message.",
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow()
            )
            embed.set_footer(text=f"Changed by {ctx.author}")
            await ctx.send(embed=embed)
        elif opt == "enable":
            cfg["enabled"] = True
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
            LOG_CHANNELS["mod"],
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
async def disablewelcome(ctx):
    """
    Disable the auto-welcome message for new members.
    Usage: ,disablewelcome
    """
    cfg = WELCOME_CONFIG.setdefault(ctx.guild.id, {"channel_id": None, "enabled": True})
    cfg["enabled"] = False
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
    temp_vc_owners[ch.id] = member.id
    await ch.set_permissions(member, manage_channels=True, manage_permissions=True, move_members=True, connect=True, speak=True)
    text_id = temp_vc_text_channels.get(ch.id)
    if text_id:
        text_ch = ctx.guild.get_channel(text_id)
        if text_ch:
            await text_ch.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
    await ctx.send(embed=_vc_embed("👑 Ownership Transferred", f"{member.mention} is now the owner of **{ch.name}**.", discord.Color.gold()))
    await _vc_announce(ctx.guild, ch, f"👑 **{ctx.author.display_name}** transferred ownership to **{member.display_name}**.")


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

    island_category = discord.utils.get(guild.categories, name="🏘️ The Hood")
    if island_category is None:
        island_category = await guild.create_category("🏘️ The Hood")

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
    await get_or_create_voice_channel("🔥 Hood VC", island_category)
    await get_or_create_voice_channel("🎮 Chill VC", island_category)

    await welcome_channel.edit(topic="🏘️ Arrival Zone • New members are scanned by TrapAI before entering The Hood")
    await rules_channel.edit(topic="📜 TrapAI server rules and enforcement")
    await verify_channel.edit(topic="🤖 TrapAI Security Gateway • Click the verify button below to enter The Hood")
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
    Optionally pass a category name to place it in: ,setupvc "Hood VCs"
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
            "• A private text chat is created alongside it\n"
            "• They get full button controls to manage their VC\n"
            "• Both channels delete automatically when empty\n\n"
            f"Channel: {trigger_vc.mention if hasattr(trigger_vc, 'mention') else trigger_vc.name}"
        ),
        color=discord.Color.dark_grey(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"TrapAI VC System • {guild.name}")
    await ctx.send(embed=embed)

    await log(
        guild,
        LOG_CHANNELS["vc"],
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
        LOG_CHANNELS["jail"],
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
        resolved = _resolve_log_channel(guild, key)
        if resolved:
            await ctx.send(f"↩️ Override cleared for `{key}`. Now using {resolved.mention} (found by name).")
        else:
            await ctx.send(f"↩️ Override cleared for `{key}`. No channel named `#{LOG_CHANNELS[key]}` found yet.")
        return

    # ── Pin a channel ────────────────────────────────────────
    LOG_CHANNEL_OVERRIDES.setdefault(guild.id, {})[key] = channel.id
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
    embed = discord.Embed(
        title="🤖 TrapAI Security Gateway",
        description=(
            f"Welcome to **{ctx.guild.name}** 🏘️🔥\n\n"
            "Before entering, your account must pass **TrapAI Security Verification**.\n\n"
            "### Access Requirements\n"
            "• Account must not be restricted\n"
            "• Verification must be completed\n"
            "• Entry is locked until approved\n\n"
            "Click the button below to begin your scan and get access."
        ),
        color=discord.Color.dark_grey(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(
        name="🔓 What You Unlock",
        value=(
            "💬 General Chat\n"
            "🎤 Voice Channels\n"
            "🔥 Hood Community Access\n"
            "🛡 Protected Server Entry"
        ),
        inline=False
    )
    embed.add_field(
        name="⚠ Security Notice",
        value="Unverified users remain locked in the arrival zone until TrapAI approves access.",
        inline=False
    )
    if ctx.guild.icon:
        embed.set_thumbnail(url=ctx.guild.icon.url)
    embed.set_footer(text=f"TrapAI Security • {ctx.guild.name} Protection")
    msg = await ctx.send(embed=embed, view=VerifyView())
    # Re-register with the message_id so the persistent view survives bot restarts
    bot.add_view(VerifyView(), message_id=msg.id)


@bot.command()
@commands.has_permissions(administrator=True)
async def sendtickets(ctx):
    """Send the ticket panel to the current channel."""
    embed = discord.Embed(
        title="🎫 TrapAI Support Tickets",
        description=(
            "Need help from staff? Open a **private support ticket**.\n\n"
            "```yaml\n"
            "Ticket System  : ACTIVE\n"
            "Response Time  : As soon as possible\n"
            "Privacy        : Staff + ticket opener only\n"
            "Categories     : 10 types available\n"
            "```\n"
            "Use the **dropdown below** to pick a category and open your ticket."
        ),
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(
        name="📂 Ticket Categories",
        value=(
            "🎫 **General Support** — General questions or help\n"
            "💳 **Billing / Purchase** — Payments, refunds\n"
            "🚨 **Report a Member** — Report rule-breaking\n"
            "📝 **Ban / Mute Appeal** — Appeal a mod action\n"
            "🤝 **Partnership** — Collab requests\n"
            "🐛 **Bug Report** — Report a bot or server bug\n"
            "🔓 **Unban Request** — Request to be unbanned\n"
            "📋 **Staff Application** — Apply to join the staff team\n"
            "💰 **Trade / Deal** — Trade enquiry or deal verification\n"
            "❓ **Other** — Anything that doesn't fit above"
        ),
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
    await ctx.send(embed=embed, view=TicketOpenView())

    await log(
        ctx.guild,
        LOG_CHANNELS["tickets"],
        "Ticket Panel Sent",
        f"**Administrator:** {ctx.author.mention}\n**Channel:** {ctx.channel.mention}",
        discord.Color.blurple()
    )


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
        LOG_CHANNELS["tickets"],
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
        LOG_CHANNELS["tickets"],
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

    await asyncio.sleep(5)

    TICKETS[guild.id].pop(owner_id, None)
    TICKET_CLAIMED.pop(channel.id, None)
    TICKET_TYPE.pop(channel.id, None)
    TICKET_PRIORITY.pop(channel.id, None)
    TICKET_LOCKED.pop(channel.id, None)

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
        LOG_CHANNELS["mod"],
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
            description=f"{member.mention} is now a **Hood Member** 🏘️🔥",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Verified by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        await log(ctx.guild, LOG_CHANNELS["verification"], "Member Verified", None, discord.Color.green(),
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
        await log(ctx.guild, LOG_CHANNELS["verification"], "Member Unverified", None, discord.Color.orange(),
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
        await log(ctx.guild, LOG_CHANNELS["verification"], "TrapAI Verification Denied", None, discord.Color.red(),
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
    await log(ctx.guild, LOG_CHANNELS["mod"], "TrapAI Warning Issued", None, discord.Color.orange(),
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
        LOG_CHANNELS["mod"],
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
    verified_role = discord.utils.get(ctx.guild.roles, name=VERIFIED_ROLE)
    unverified_role = discord.utils.get(ctx.guild.roles, name=UNVERIFIED_ROLE)

    if not jail_role:
        await ctx.send(f"❌ Role **{JAIL_ROLE}** was not found.")
        return

    try:
        if verified_role and verified_role in member.roles:
            await member.remove_roles(verified_role, reason=f"Jailed by {ctx.author}")
        if unverified_role and unverified_role in member.roles:
            await member.remove_roles(unverified_role, reason=f"Jailed by {ctx.author}")
        if jail_role not in member.roles:
            await member.add_roles(jail_role, reason=f"Jailed by {ctx.author} | {reason}")

        await _apply_jail_overwrites(member)

        old_task = jailed_users.get(member.id)
        if old_task:
            old_task.cancel()

        jailed_users[member.id] = asyncio.create_task(
            auto_unjail(ctx.guild.id, member.id, seconds)
        )

        # DM the user so they know they've been jailed
        await _dm_action(member, ctx.guild, "jail", ctx.author, reason,
                         extra=f"Duration: **{duration}**")

        embed = discord.Embed(
            title="🔒 TrapAI Restriction Applied",
            description=(
                f"{member.mention} has been placed into **restricted custody**.\n\n"
                "```yaml\n"
                f"Duration: {duration}\n"
                f"Reason: {reason}\n"
                f"Moderator: {ctx.author}\n"
                "Status: JAILED\n"
                "```"
            ),
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="TrapAI Enforcement • Restriction Active")
        await ctx.send(embed=embed)
        await log(ctx.guild, LOG_CHANNELS["jail"], "Member Jailed", None, discord.Color.red(),
                  fields=[
                      ("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                      ("🔒 User",      f"{member.mention} (`{member.id}`)",          True),
                      ("⏱️ Duration",  duration,                                      True),
                      ("📝 Reason",    reason,                                         False),
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

        await _remove_jail_overwrites(member)

        old_task = jailed_users.get(member.id)
        if old_task:
            old_task.cancel()
            jailed_users.pop(member.id, None)

        embed = discord.Embed(
            title="🔓 TrapAI Restriction Removed",
            description=(
                f"{member.mention} has been released from restricted custody.\n\n"
                "```yaml\n"
                f"Reason: {reason}\n"
                f"Moderator: {ctx.author}\n"
                "Status: RELEASED\n"
                "```"
            ),
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="TrapAI Enforcement • Access Updated")
        await ctx.send(embed=embed)
        await log(ctx.guild, LOG_CHANNELS["jail"], "Member Unjailed", None, discord.Color.green(),
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
    await ctx.channel.purge(limit=amount + 1)
    await log(ctx.guild, LOG_CHANNELS["clears"], "Messages Cleared", None, discord.Color.orange(),
              fields=[("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("📍 Channel", ctx.channel.mention, True), ("🧹 Amount", str(amount), True)],
              actor=ctx.author)


@bot.command()
@commands.has_permissions(manage_channels=True)
async def lock(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False)
    await ctx.send("🔒 Channel locked")
    await log(ctx.guild, LOG_CHANNELS["mod"], "Channel Locked", None, discord.Color.red(),
              fields=[("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("🔐 Channel", ctx.channel.mention, True)],
              actor=ctx.author)


@bot.command()
@commands.has_permissions(manage_channels=True)
async def unlock(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=True)
    await ctx.send("🔓 Channel unlocked")
    await log(ctx.guild, LOG_CHANNELS["mod"], "Channel Unlocked", None, discord.Color.green(),
              fields=[("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("🔓 Channel", ctx.channel.mention, True)],
              actor=ctx.author)


@bot.command()
@commands.has_permissions(administrator=True)
async def restart(ctx):
    await ctx.send("🔄 Restarting bot...")
    await log(ctx.guild, LOG_CHANNELS["mod"], "Bot Restarted", None, discord.Color.blurple(),
              fields=[("👑 Administrator", f"{ctx.author.mention} (`{ctx.author.id}`)", True)],
              actor=ctx.author)
    os.execv(sys.executable, [sys.executable] + sys.argv)


@bot.command()
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason="No reason provided"):
    # DM before kick so they receive it while still in the server
    await _dm_action(member, ctx.guild, "kick", ctx.author, reason)
    await member.kick(reason=reason)
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
    await log(ctx.guild, LOG_CHANNELS["kicks"], "Member Kicked", None, discord.Color.orange(),
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
    await log(ctx.guild, LOG_CHANNELS["bans"], "Member Banned", None, discord.Color.red(),
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
    await log(ctx.guild, LOG_CHANNELS["timeouts"], "Member Timed Out", None, discord.Color.gold(),
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
    staff_roles = [
        role for role in member.roles
        if role.name != "@everyone" and (
            role.permissions.administrator or
            role.permissions.manage_guild or
            role.permissions.manage_roles or
            role.permissions.manage_channels
        )
    ]
    if not staff_roles:
        await ctx.send("❌ That user has no staff roles.")
        return

    # Snapshot ALL non-everyone roles for potential restoration
    snapshot = [r.id for r in member.roles if r.name != "@everyone"]
    ROLE_SNAPSHOTS.setdefault(ctx.guild.id, {})[member.id] = snapshot

    role_names = ", ".join([role.name for role in staff_roles])
    await member.remove_roles(*staff_roles, reason=f"Stripped by {ctx.author}")
    await ctx.send(
        f"✅ Removed all staff roles from {member.mention}\n"
        f"📸 Snapshot saved — use `,restoreallroles {member.mention}` to restore."
    )
    await log(ctx.guild, LOG_CHANNELS["strips"], "Staff Roles Stripped", None, discord.Color.dark_red(),
              fields=[
                  ("👑 Admin",         f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("⚔️ User",          f"{member.mention} (`{member.id}`)",         True),
                  ("🏷️ Roles Removed", role_names[:512],                             False),
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

    await log(ctx.guild, LOG_CHANNELS["strips"], "Roles Snapshot Restored", None, discord.Color.green(),
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

    # Clone first, then delete the old one
    new_channel = await old_channel.clone(reason=f"Nuked by {author}")
    await old_channel.delete(reason=f"Nuked by {author}")

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
        LOG_CHANNELS["mod"],
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
    await log(ctx.guild, LOG_CHANNELS["lockdowns"], "Server Lockdown Enabled", "All text channels locked.", discord.Color.red(),
              fields=[("👑 Admin", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("🔐 Channels Locked", str(len(ctx.guild.text_channels)), True)],
              actor=ctx.author)


@bot.command()
@commands.has_permissions(administrator=True)
async def unlockdown(ctx):
    for channel in ctx.guild.text_channels:
        await channel.set_permissions(ctx.guild.default_role, send_messages=True)
    await ctx.send("🔓 Server lockdown removed.")
    await log(ctx.guild, LOG_CHANNELS["unlockdowns"], "Server Lockdown Removed", "All text channels unlocked.", discord.Color.green(),
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
    await log(ctx.guild, LOG_CHANNELS["roleall"], "Role Given To All", None, discord.Color.blue(),
              fields=[("👑 Admin", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("🏷️ Role", f"{role.mention}", True), ("👥 Members Affected", str(count), True)],
              actor=ctx.author)


# ============================================================
# STATS COMMANDS
# ============================================================
@bot.command()
async def vcstats(ctx, member: discord.Member = None):
    member = member or ctx.author
    total_seconds = vc_stats.get(member.id, 0)
    if member.id in vc_join_time:
        total_seconds += int(time.time() - vc_join_time[member.id])

    minutes, sec = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)

    embed = discord.Embed(
        title="🎤 Voice Chat Stats",
        color=discord.Color.blue(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="User", value=member.mention, inline=False)
    embed.add_field(name="Time in VC", value=f"**{hours}h {minutes}m {sec}s**", inline=False)
    embed.add_field(name="User ID", value=str(member.id), inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
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
    vc_seconds  = vc_stats.get(member.id, 0)
    if member.id in vc_join_time:
        vc_seconds += int(time.time() - vc_join_time[member.id])
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
    embed.add_field(name="💬 Messages (session)", value=f"{chat_count:,}", inline=True)
    embed.add_field(name="🎤 VC Time (session)",  value=f"{vc_h}h {vc_m}m", inline=True)
    embed.add_field(name="🎭 Top Role",     value=member.top_role.mention, inline=True)
    embed.add_field(name="🏷️ Roles",        value=roles[:1024],         inline=False)
    embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


# ============================================================
# CHAT STATS  &  SERVER STATS
# ============================================================
@bot.command()
async def chatstats(ctx, member: discord.Member = None):
    """Show message count stats. Usage: ,chatstats [@user]"""
    guild_stats = CHAT_STATS.get(ctx.guild.id, {})

    if member:
        count = guild_stats.get(member.id, 0)
        embed = discord.Embed(
            title="💬 Chat Stats",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="👤 User",     value=member.mention, inline=True)
        embed.add_field(name="💬 Messages", value=f"**{count:,}**", inline=True)
        embed.set_footer(text=f"Session only • Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        return

    # Server-wide leaderboard (top 10)
    sorted_stats = sorted(guild_stats.items(), key=lambda x: x[1], reverse=True)[:10]

    embed = discord.Embed(
        title="💬 Chat Stats Leaderboard",
        description=f"**Top chatters in {ctx.guild.name}** (this session)",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    if not sorted_stats:
        embed.description = "No messages tracked yet this session."
    else:
        medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
        lines = []
        for i, (uid, count) in enumerate(sorted_stats):
            m = ctx.guild.get_member(uid)
            name = m.mention if m else f"<@{uid}>"
            lines.append(f"{medals[i]} {name} — **{count:,}** messages")
        embed.description = "\n".join(lines)

    embed.set_footer(text=f"Session only • Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
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
        p5.add_field(name="💬 Chat Leaderboard", value="No messages tracked yet this session.", inline=False)

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
    await log(ctx.guild, LOG_CHANNELS["roles"], "Role Manually Added",
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
    await log(ctx.guild, LOG_CHANNELS["roles"], "Role Manually Removed",
              f"**Staff:** {ctx.author.mention}\n**User:** {member.mention}\n**Role:** {role.mention}",
              discord.Color.orange())


@role_group.command(name="create")
@commands.has_permissions(manage_roles=True)
async def role_create(ctx, name: str, color: str = "#000000", hoist: bool = False):
    """Create a new role. Usage: ,role create "Hood Member" #ff0000 true"""
    try:
        hex_val   = int(color.lstrip("#"), 16)
        disc_color = discord.Color(hex_val)
    except ValueError:
        await ctx.send("❌ Invalid colour. Use hex like `#ff0000`.")
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
    await log(ctx.guild, LOG_CHANNELS["role_create"], "Role Created (cmd)",
              f"**Staff:** {ctx.author.mention}\n**Role:** {new_role.mention}\n**Color:** {color}\n**Hoist:** {hoist}",
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
    await log(ctx.guild, LOG_CHANNELS["role_delete"], "Role Deleted (cmd)",
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
    await log(ctx.guild, LOG_CHANNELS["roles"], "Role Color Changed",
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
    await log(ctx.guild, LOG_CHANNELS["warns"], "Member Warned", None, discord.Color.orange(),
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


@bot.command()
@commands.has_permissions(moderate_members=True)
async def clearwarnings(ctx, member: discord.Member):
    guild_warns = WARNINGS.setdefault(ctx.guild.id, {})
    count = len(guild_warns.get(member.id, []))
    guild_warns[member.id] = []

    embed = discord.Embed(
        title="✅ Warnings Cleared",
        description=f"Cleared **{count}** warning(s) for {member.mention}.",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"Cleared by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)
    await log(ctx.guild, LOG_CHANNELS["warns"], "Warnings Cleared", None, discord.Color.green(),
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
        embed = discord.Embed(
            title="🔇 Member Muted",
            description=f"{member.mention} has been muted.",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_footer(text=f"Muted by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        await log(ctx.guild, LOG_CHANNELS["mutes"], "Member Muted", None, discord.Color.red(),
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
        embed = discord.Embed(
            title="🔊 Member Unmuted",
            description=f"{member.mention} has been unmuted.",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Unmuted by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        await log(ctx.guild, LOG_CHANNELS["mutes"], "Member Unmuted", None, discord.Color.green(),
                  fields=[("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("🔊 User", f"{member.mention} (`{member.id}`)", True), ("📝 Reason", reason, False)],
                  actor=ctx.author, target=member)

    except discord.Forbidden:
        await ctx.send("❌ I can't manage that member's roles. Move my bot role higher.")


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
    await log(ctx.guild, LOG_CHANNELS["hides"], "Channel Hidden", None, discord.Color.orange(),
              fields=[("🛡 Moderator", f"{ctx.author.mention} (`{ctx.author.id}`)", True), ("👁️ Channel", ctx.channel.mention, True)],
              actor=ctx.author)


@bot.command()
@commands.has_permissions(manage_channels=True)
async def unhide(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, view_channel=True)
    await ctx.send(f"👀 {ctx.channel.mention} is now visible to everyone.")
    await log(ctx.guild, LOG_CHANNELS["hides"], "Channel Unhidden", None, discord.Color.green(),
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

    def check(msg):
        if member:
            return msg.author.id == member.id
        return True

    deleted = await ctx.channel.purge(limit=amount + 1, check=check)
    deleted_count = max(len(deleted) - 1, 0)

    confirmation = await ctx.send(f"🧹 Purged **{deleted_count}** message(s)" + (f" from {member.mention}." if member else "."))
    await confirmation.delete(delay=5)

    await log(
        ctx.guild,
        LOG_CHANNELS["purges"],
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
    await log(ctx.guild, LOG_CHANNELS["massroles"], "Mass Role Added", None, discord.Color.blue(),
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
    await log(ctx.guild, LOG_CHANNELS["massroles"], "Mass Role Removed", None, discord.Color.blue(),
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
        LOG_CHANNELS["mod"],
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
        LOG_CHANNELS["mod"],
        "Server Restore Started",
        f"Administrator: {ctx.author.mention}\nLabel: `{label}`\nBackup taken: {taken_at} UTC",
        discord.Color.orange()
    )

    await _apply_restore(ctx.guild, data, ctx.channel)

    await log(
        ctx.guild,
        LOG_CHANNELS["mod"],
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
        LOG_CHANNELS["mod"],
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
# GIVEAWAY SYSTEM
# ============================================================

import random as _random

# GIVEAWAYS[message_id] = { guild_id, channel_id, host_id, prize, winners, ends_at, entries: set }
GIVEAWAYS: dict[int, dict] = {}


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
        GIVEAWAYS.pop(msg_id, None); return
    try:
        msg = await channel.fetch_message(msg_id)
    except (discord.NotFound, discord.HTTPException):
        GIVEAWAYS.pop(msg_id, None); return

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
        ctx.guild, LOG_CHANNELS["mod"], f"Staff PSA Posted — {label}", None, color,
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

TASKS: dict[int, list] = {}
_task_counter = 0


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
        ctx.guild, LOG_CHANNELS["mod"], "Staff Task Created", None, discord.Color.blurple(),
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

            await log(guild, LOG_CHANNELS["mod"], "Vouch-Role Request Approved", None, discord.Color.green(),
                      fields=[
                          ("👤 Member",     f"{member.mention} (`{member.id}`)",         True),
                          ("🏷️ Role",       f"{role.mention}",                           True),
                          ("👑 Approved by", f"{interaction.user.mention}",              True),
                          ("📝 Reason",     pending["reason"],                            False),
                      ], actor=interaction.user, target=member)

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
                await log(guild, LOG_CHANNELS["mod"], "Vouch-Role Request Rejected", None, discord.Color.red(),
                          fields=[
                              ("👤 Member",     f"{member.mention} (`{member.id}`)" if member else str(pending["member_id"]), True),
                              ("🏷️ Role",       f"{role.mention}",                  True),
                              ("👑 Rejected by", f"{interaction.user.mention}",     True),
                              ("📝 Reason",     pending["reason"],                   False),
                          ], actor=interaction.user)

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

        await log(ctx.guild, LOG_CHANNELS["mod"], "Vouch-Role Request Submitted", None, discord.Color.gold(),
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

    await log(ctx.guild, LOG_CHANNELS["mod"], "Member Vouched", None, discord.Color.green(),
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
    """Remove a vouch from a member. Usage: ,unvouch @user [reason]"""
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
    score = gv[member.id]

    embed = discord.Embed(
        title="↩️ Vouch Removed",
        description=f"{ctx.author.mention} removed a vouch from {member.mention}.",
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="📝 Reason",            value=reason,    inline=False)
    embed.add_field(name="🔢 Remaining Vouches", value=str(score), inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"TrapAI Vouch System • {ctx.guild.name}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)

    await log(ctx.guild, LOG_CHANNELS["mod"], "Vouch Removed", None, discord.Color.orange(),
              fields=[
                  ("🛡 By",     f"{ctx.author.mention} (`{ctx.author.id}`)", True),
                  ("↩️ From",   f"{member.mention} (`{member.id}`)",          True),
                  ("🔢 Score",  str(score),                                    True),
                  ("📝 Reason", reason,                                        False),
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
        embed = discord.Embed(
            title="✅ Vouch Threshold Updated",
            description=f"Members now need **{n}** vouch(es) to use power commands.",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Set by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        await log(ctx.guild, LOG_CHANNELS["mod"], "Vouch Threshold Changed", None, discord.Color.blurple(),
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
        await log(guild, LOG_CHANNELS["mod"], "Protected Role Added", None, discord.Color.dark_red(),
                  fields=[("🏷️ Role", f"{role.mention} (`{role.id}`)", True),
                           ("👑 By",   f"{ctx.author.mention}",         True)],
                  actor=ctx.author)

    # ── Remove ───────────────────────────────────────────────
    elif action.lower() == "remove":
        if role.id not in protected:
            await ctx.send(f"❌ **{role.name}** is not protected.", delete_after=6)
            return
        protected.discard(role.id)
        embed = discord.Embed(
            title="🔓 Role Unprotected",
            description=f"**{role.name}** is no longer protected. It can be granted manually again.",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="🏷️ Role", value=f"{role.mention} (`{role.id}`)", inline=True)
        embed.set_footer(text=f"Set by {ctx.author} • TrapAI")
        await ctx.send(embed=embed)
        await log(guild, LOG_CHANNELS["mod"], "Protected Role Removed", None, discord.Color.green(),
                  fields=[("🏷️ Role", f"{role.mention} (`{role.id}`)", True),
                           ("👑 By",   f"{ctx.author.mention}",         True)],
                  actor=ctx.author)

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

    await log(guild, LOG_CHANNELS["mod"], "Vouch Request Cancelled", None, discord.Color.orange(),
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
        await log(guild, LOG_CHANNELS["mod"], "Auto-Role Added", None, discord.Color.green(),
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
        await log(guild, LOG_CHANNELS["mod"], "Auto-Role Removed", None, discord.Color.orange(),
                  fields=[("🏷️ Role", f"{role.mention} (`{role.id}`)", True),
                           ("👑 By",   f"{ctx.author.mention}",         True)],
                  actor=ctx.author)

    # ── Clear ────────────────────────────────────────────────
    elif action.lower() == "clear":
        count = len(auto_roles)
        auto_roles.clear()
        embed = discord.Embed(
            title="🗑️ Auto-Roles Cleared",
            description=f"Removed all **{count}** auto-role(s). No roles will be auto-assigned on join.",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Cleared by {ctx.author} • TrapAI")
        await ctx.send(embed=embed)
        await log(guild, LOG_CHANNELS["mod"], "Auto-Roles Cleared", None, discord.Color.red(),
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

    await log(guild, LOG_CHANNELS["bans"], "Hard-Ban Applied", None, discord.Color.dark_red(),
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

    await log(guild, LOG_CHANNELS["bans"], "Hard-Ban Removed", None, discord.Color.green(),
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
    await log(ctx.guild, LOG_CHANNELS["mod"], "Server Invite Link Set", None, discord.Color.green(),
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
        await ctx.send(f"❌ Could not DM **{user}** (`{user.id}`) — their DMs are closed.", delete_after=8)
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

    Usage:
      ,announce <message>
      ,announce #channel <message>
      ,announce #channel --title My Title | --color red | --image <url> | --ping everyone/here | <message>

    Flags (all optional, any order, separated by |):
      --title  <text>         — embed title
      --color  <name/hex>     — sidebar color  (red green blue gold purple teal orange pink)
      --image  <url>          — large image at the bottom of the embed
      --ping   everyone/here  — ping @everyone or @here

    Examples:
      ,announce Server is going online!
      ,announce #announcements Big update dropping tonight!
      ,announce #general --title 🔥 Event --color gold --ping here | Giveaway starts at 9 PM!
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
    ping    = None
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
ECONOMY: dict[int, dict[int, dict]] = {}

# COOLDOWNS[guild_id][user_id][action] = timestamp
COOLDOWNS: dict[int, dict[int, dict]] = {}

# GAMBLE_WINS[guild_id][user_id] = net_winnings (for leaderboard)
GAMBLE_WINS: dict[int, dict[int, int]] = {}

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
        await ctx.send("⏰ Time's up! No bet placed.", delete_after=6)
        return

    next_card = random.randint(1, 13)
    guess = msg.content.lower() in ("higher", "high", "h")
    actual_higher = next_card > card
    won = (guess and actual_higher) or (not guess and not actual_higher)

    if next_card == card:
        await ctx.send(f"🤝 Draw! Both cards were **{card_names[next_card]}**. Bet returned.")
        return

    data["wallet"] += bet if won else -bet
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

    if pv > 21:
        result, color, delta = "💥 **Bust!** You went over 21. Dealer wins.", discord.Color.red(), -bet
    elif dv > 21 or pv > dv:
        result, color, delta = f"🎉 **You win!** ({pv} vs {dv})", discord.Color.green(), bet
    elif pv == dv:
        result, color, delta = f"🤝 **Push!** ({pv} vs {dv}) Bet returned.", discord.Color.blurple(), 0
    else:
        result, color, delta = f"😢 **Dealer wins!** ({dv} vs {pv})", discord.Color.red(), -bet

    data["wallet"] += delta
    GAMBLE_WINS.setdefault(ctx.guild.id, {})[ctx.author.id] = \
        GAMBLE_WINS.setdefault(ctx.guild.id, {}).get(ctx.author.id, 0) + delta
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


bot.run()
