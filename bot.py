from dotenv import load_dotenv
load_dotenv()

import os
import sys
import time
import asyncio
import discord
from discord.ext import commands
from datetime import datetime, timedelta

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix=",", intents=intents)

BAD_WORDS = ["badword1", "badword2"]
LINKS = ["http", "https", "discord.gg"]

LOG_CHANNELS = {
    "vc":             1523261277156151367,
    "messages":       1523261361281433631,
    "joins":          1523252987508555897,
    "leaves":         1523266648134520873,
    "raids":          1523259059313053766,
    "mod":            1523260160972296334,
    "roles":          1523260471694852196,
    "boost":          1523260549910102086,
    "jail":           1523260616150618192,
    "nicknames":      1523261128820527184,
    "role_create":    1523260767900663888,
    "role_delete":    1523260976122826872,
    "channel_create": 1523261277156151367,
    "channel_delete": 1523261361281433631,
    "channel_update": 1523261449391177758,
    "emoji":          1523261532467757086,
    "stickers":       1523261607910838432,
    "bans":           1523261741000298606,
    "kicks":          1523261842162454568,
    "timeouts":       1523261910277947482,
    "strips":         1523260265821376516,
    "lockdowns":      1523262126141997166,
    "unlockdowns":    1523262247550320743,
    "clears":         1523262357508325567,
    "roleall":        1523262478580973698,
    "verification":   1523262557039628358,
    "warns":          1523265069109219430,
    "tickets":        1523258882917666910,
    "mutes":          1523267887480049714,
    "hides":          1523265166777782385,
    "purges":         1523265629883338822,
    "massroles":      1523265768513470464,
    "invites":        1523606974674108527,
}

WELCOME_CHANNEL = "welcome"

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

# TICKETS[guild_id][user_id] = channel_id
TICKETS = {}


# ============================================================
# LOGGING
# ============================================================
async def log(guild, channel_id, title, description, color):
    channel = guild.get_channel(channel_id)
    if not channel:
        print(f"[LOG ERROR] Channel ID not found: {channel_id}")
        return

    embed = discord.Embed(
        title=f"📋 {title}",
        description=description,
        color=color,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"{guild.name} • Logging System")
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    try:
        await channel.send(embed=embed)
        print(f"[LOG SUCCESS] Sent log to channel ID {channel_id}")
    except discord.Forbidden:
        print(f"[LOG ERROR] Missing permission in channel ID {channel_id}")
    except discord.HTTPException as e:
        print(f"[LOG ERROR] Failed to send log: {e}")


# ============================================================
# TICKET CLOSE VIEW (persistent)
# ============================================================
class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="trapai_ticket_close"
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        guild = interaction.guild

        confirm_embed = discord.Embed(
            title="🔒 Closing Ticket",
            description="This ticket will be deleted in **5 seconds**.",
            color=discord.Color.red(),
            timestamp=datetime.utcnow()
        )
        confirm_embed.set_footer(text=f"Closed by {interaction.user}")
        await interaction.response.send_message(embed=confirm_embed)

        await log(
            guild,
            LOG_CHANNELS["tickets"],
            "Ticket Closed",
            f"Channel: {channel.name}\nClosed By: {interaction.user.mention}",
            discord.Color.red()
        )

        await asyncio.sleep(5)

        # Remove from tracking
        for uid, cid in list(TICKETS.get(guild.id, {}).items()):
            if cid == channel.id:
                TICKETS[guild.id].pop(uid, None)
                break

        try:
            await channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.HTTPException:
            pass


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
                timestamp=datetime.utcnow()
            )
            embed.set_footer(text="TrapAI Security • Access Denied • The Hood")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if not verified_role:
            await interaction.response.send_message(f"❌ The role **{VERIFIED_ROLE}** was not found.", ephemeral=True)
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
            timestamp=datetime.utcnow()
        )
        scan_embed.set_footer(text="TrapAI Security • Initializing")
        await interaction.response.send_message(embed=scan_embed, ephemeral=True)

        try:
            await asyncio.sleep(2)

            if unverified_role and unverified_role in member.roles:
                await member.remove_roles(unverified_role, reason="TrapAI button verification")

            if verified_role not in member.roles:
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
                    "Welcome to **The Hood** 🏘️🔥"
                ),
                color=discord.Color.green(),
                timestamp=datetime.utcnow()
            )
            success_embed.set_footer(text="TrapAI Security • Access Granted • The Hood")
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
                timestamp=datetime.utcnow()
            )
            fail_embed.set_footer(text="TrapAI Security • Sync Failed")
            await interaction.edit_original_response(embed=fail_embed)

        except discord.HTTPException:
            fail_embed = discord.Embed(
                title="❌ TrapAI Verification Failed",
                description="Something went wrong while processing your verification.",
                color=discord.Color.red(),
                timestamp=datetime.utcnow()
            )
            fail_embed.set_footer(text="TrapAI Security • System Error")
            await interaction.edit_original_response(embed=fail_embed)


# ============================================================
# TICKET OPEN VIEW (persistent)
# ============================================================
class TicketOpenView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Open Ticket",
        style=discord.ButtonStyle.primary,
        emoji="🎫",
        custom_id="trapai_ticket_open"
    )
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        member = interaction.user

        guild_tickets = TICKETS.setdefault(guild.id, {})

        if member.id in guild_tickets:
            existing = guild.get_channel(guild_tickets[member.id])
            if existing:
                await interaction.response.send_message(
                    f"❌ You already have an open ticket: {existing.mention}",
                    ephemeral=True
                )
                return
            else:
                del guild_tickets[member.id]

        ticket_category = discord.utils.get(guild.categories, name="🎫 Tickets")
        if ticket_category is None:
            ticket_category = await guild.create_category("🎫 Tickets")

        ticket_name = f"ticket-{member.name}".lower().replace(" ", "-")

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True
            )
        }

        # Give staff (anyone with manage_messages) access
        for role in guild.roles:
            if role.permissions.manage_messages or role.permissions.administrator:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True
                )

        ticket_channel = await guild.create_text_channel(
            name=ticket_name,
            category=ticket_category,
            overwrites=overwrites,
            reason=f"Ticket opened by {member}"
        )

        guild_tickets[member.id] = ticket_channel.id

        embed = discord.Embed(
            title="🎫 Ticket Opened",
            description=(
                f"Welcome {member.mention}!\n\n"
                "Support staff will be with you shortly.\n"
                "Please describe your issue in detail.\n\n"
                "Click **Close Ticket** when your issue is resolved."
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.utcnow()
        )
        embed.set_footer(text="TrapAI Ticket System")

        await ticket_channel.send(embed=embed, view=TicketCloseView())
        await ticket_channel.send(member.mention, delete_after=3)

        await interaction.response.send_message(
            f"✅ Ticket created: {ticket_channel.mention}",
            ephemeral=True
        )

        await log(
            guild,
            LOG_CHANNELS["tickets"],
            "Ticket Opened",
            f"User: {member.mention}\nChannel: {ticket_channel.mention}",
            discord.Color.blurple()
        )


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
        embed = discord.Embed(title=f"🎤 {vc.name}", color=discord.Color.dark_grey(), timestamp=datetime.utcnow())
        embed.add_field(name="👑 Owner", value=owner.mention if owner else "Unknown", inline=True)
        embed.add_field(name="👥 Count", value=f"{len(vc.members)}/{limit_str}", inline=True)
        embed.add_field(name="🔒 Locked", value="Yes" if locked else "No", inline=True)
        embed.add_field(name="👻 Hidden", value="Yes" if hidden else "No", inline=True)
        embed.add_field(name="🔊 Bitrate", value=f"{vc.bitrate // 1000}kbps", inline=True)
        embed.add_field(name="🌐 Region", value=str(vc.rtc_region) if vc.rtc_region else "Auto", inline=True)
        embed.add_field(name="🛡 VC Mods", value=mod_mentions, inline=False)
        embed.add_field(name="🚫 Banned", value=banned_mentions, inline=False)
        embed.add_field(name="🎙️ Members", value=members_str, inline=False)
        embed.set_footer(text="TrapAI VC System • The Hood")
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
        embed = discord.Embed(description=message, color=discord.Color.dark_grey(), timestamp=datetime.utcnow())
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
# CMDS VIEW
# ============================================================
class CmdsView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=120)
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ You can't use someone else's command menu.",
                ephemeral=True
            )
            return False
        return True

    def home_embed(self, guild: discord.Guild):
        embed = discord.Embed(
            title="🤖 TrapAI Command Center",
            description=(
                "Welcome to **The Hood** command panel.\n\n"
                "Use the buttons below to view command categories."
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.utcnow()
        )
        embed.add_field(
            name="📂 Categories",
            value=(
                "🛡 Moderation\n"
                "🔒 Jail System\n"
                "🤖 TrapAI Security\n"
                "📊 Stats\n"
                "🎤 VC Controls\n"
                "⚙️ Admin"
            ),
            inline=False
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text="TrapAI Security • The Hood Command Menu")
        return embed

    def moderation_embed(self, guild: discord.Guild):
        embed = discord.Embed(
            title="🛡 Moderation Commands",
            color=discord.Color.red(),
            timestamp=datetime.utcnow()
        )
        embed.add_field(
            name="Punishment",
            value=(
                "`,kick @user [reason]`\n"
                "`,ban @user [reason]`\n"
                "`,timeout @user minutes [reason]`\n"
                "`,mute @user [reason]`\n"
                "`,unmute @user [reason]`\n"
                "`,warn @user [reason]`\n"
                "`,warnings [@user]`\n"
                "`,clearwarnings @user`"
            ),
            inline=False
        )
        embed.add_field(
            name="Channel Management",
            value=(
                "`,clear amount`\n"
                "`,purge amount [@user]`\n"
                "`,lock`\n"
                "`,unlock`\n"
                "`,hide`\n"
                "`,unhide`\n"
                "`,slowmode seconds`\n"
                "`,nuke`\n"
                "`,lockdown`\n"
                "`,unlockdown`"
            ),
            inline=False
        )
        embed.add_field(
            name="Member Management",
            value="`,nickname @user [new_nick]`",
            inline=False
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text="TrapAI Security • The Hood Moderation Panel")
        return embed

    def jail_embed(self, guild: discord.Guild):
        embed = discord.Embed(
            title="🔒 Jail System Commands",
            color=discord.Color.dark_red(),
            timestamp=datetime.utcnow()
        )
        embed.add_field(
            name="Commands",
            value=(
                "`,jail @user time [reason]`\n"
                "`,unjail @user [reason]`"
            ),
            inline=False
        )
        embed.add_field(
            name="Time Formats",
            value="`30s 10m 2h 3d 1w 1mo 1y`",
            inline=False
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text="TrapAI Security • The Hood Jail Panel")
        return embed

    def security_embed(self, guild: discord.Guild):
        embed = discord.Embed(
            title="🤖 TrapAI Security Commands",
            color=discord.Color.green(),
            timestamp=datetime.utcnow()
        )
        embed.add_field(
            name="Commands",
            value=(
                "`,sendverify`\n"
                "`,verify @user`\n"
                "`,unverify @user`\n"
                "`,denyverify @user [reason]`\n"
                "`,trapwarn @user [reason]`\n"
                "`,trapscan @user`"
            ),
            inline=False
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text="TrapAI Security • The Hood Security Panel")
        return embed

    def stats_embed(self, guild: discord.Guild):
        embed = discord.Embed(
            title="📊 Stats Commands",
            color=discord.Color.gold(),
            timestamp=datetime.utcnow()
        )
        embed.add_field(
            name="Commands",
            value=(
                "`,vcstats [@user]`\n"
                "`,whois [@user]`\n"
                "`,ping`"
            ),
            inline=False
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text="TrapAI Security • The Hood Stats Panel")
        return embed

    def vc_embed(self, guild: discord.Guild):
        embed = discord.Embed(
            title="🎤 VC Control Commands",
            description="All commands work in your VC's private text chat. 👑 = owner only.",
            color=discord.Color.dark_grey(),
            timestamp=datetime.utcnow()
        )
        embed.add_field(
            name="🔒 Privacy",
            value="`,vclock` `,vcunlock` `,vchide` `,vcshow`",
            inline=False
        )
        embed.add_field(
            name="⚙️ Channel",
            value="`,vcname <name>` `,vclimit <0-99>` `,vcbitrate <kbps>` `,vcregion <region>`",
            inline=False
        )
        embed.add_field(
            name="👥 Members",
            value=(
                "`,vcpermit @user` — whitelist\n"
                "`,vckick @user` `,vcban @user` `,vcunban @user`\n"
                "`,vcmute @user` `,vcunmute @user`\n"
                "`,vcdeafen @user` `,vcundeafen @user`"
            ),
            inline=False
        )
        embed.add_field(
            name="👑 Ownership",
            value="`,vctransfer @user` `,vcmod @user` `,vcremovemod @user`",
            inline=False
        )
        embed.add_field(
            name="🖱️ Buttons",
            value="Every command above is also a **button** in the VC text chat.",
            inline=False
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text="TrapAI Security • The Hood VC Panel")
        return embed

    def admin_embed(self, guild: discord.Guild):
        embed = discord.Embed(
            title="⚙️ Admin Commands",
            color=discord.Color.blurple(),
            timestamp=datetime.utcnow()
        )
        embed.add_field(
            name="Setup",
            value=(
                "`,setup`\n"
                "`,setupvc [category]`\n"
                "`,rules`\n"
                "`,sendverify`\n"
                "`,sendtickets`\n"
                "`,restart`"
            ),
            inline=False
        )
        embed.add_field(
            name="Role Management",
            value=(
                "`,roleall @role`\n"
                "`,massrole @role [@filter_role]`\n"
                "`,massunrole @role [@filter_role]`\n"
                "`,strip @user`"
            ),
            inline=False
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text="TrapAI Security • The Hood Admin Panel")
        return embed

    @discord.ui.button(label="Home", style=discord.ButtonStyle.secondary, emoji="🏠")
    async def home_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.home_embed(interaction.guild), view=self)

    @discord.ui.button(label="Moderation", style=discord.ButtonStyle.danger, emoji="🛡")
    async def moderation_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.moderation_embed(interaction.guild), view=self)

    @discord.ui.button(label="Jail", style=discord.ButtonStyle.danger, emoji="🔒")
    async def jail_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.jail_embed(interaction.guild), view=self)

    @discord.ui.button(label="Security", style=discord.ButtonStyle.success, emoji="🤖")
    async def security_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.security_embed(interaction.guild), view=self)

    @discord.ui.button(label="Stats", style=discord.ButtonStyle.primary, emoji="📊")
    async def stats_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.stats_embed(interaction.guild), view=self)

    @discord.ui.button(label="VC", style=discord.ButtonStyle.primary, emoji="🎤")
    async def vc_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.vc_embed(interaction.guild), view=self)

    @discord.ui.button(label="Admin", style=discord.ButtonStyle.primary, emoji="⚙️")
    async def admin_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.admin_embed(interaction.guild), view=self)


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
        timestamp=datetime.utcnow()
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
    embed.set_footer(text="TrapAI VC System • The Hood")
    await channel.send(embed=embed, view=VCControlView())


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
                timestamp=datetime.utcnow()
            )
            embed.set_footer(text="The Hood Anti-Spam")
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
                    timestamp=datetime.utcnow()
                )
                embed.set_footer(text="The Hood Anti-Spam")
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
# EVENTS
# ============================================================
@bot.event
async def on_ready():
    bot.add_view(VerifyView())
    bot.add_view(TicketOpenView())
    bot.add_view(TicketCloseView())
    bot.add_view(VCControlView())
    print(f"Logged in as {bot.user}")


@bot.event
async def on_member_join(member):
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
                f"{member} was banned (raid protection)",
                discord.Color.red()
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
                f"Could not give {UNVERIFIED_ROLE} to {member.mention}. Check bot role position.",
                discord.Color.red()
            )

    await log(
        member.guild,
        LOG_CHANNELS["joins"],
        "Member Joined",
        f"{member.mention}\nAccount created: {member.created_at.strftime('%Y-%m-%d')}",
        discord.Color.green()
    )

    ch = discord.utils.get(member.guild.text_channels, name=WELCOME_CHANNEL)
    if ch:
        embed = discord.Embed(
            title="🤖 TrapAI Arrival Scan",
            description=(
                f"Welcome {member.mention} to **The Hood** 🏘️🔥\n\n"
                "```yaml\n"
                "Identity Scan: DETECTED\n"
                "Threat Analysis: ACTIVE\n"
                "Server Access: LOCKED\n"
                "Verification Status: REQUIRED\n"
                "```\n"
                "Your account has entered the **TrapAI arrival zone**.\n\n"
                "To get in:\n"
                "1. Read the rules\n"
                "2. Go to **#verify**\n"
                "3. Press the **Verify Now** button\n\n"
                "Until then, your access stays restricted."
            ),
            color=discord.Color.dark_grey(),
            timestamp=datetime.utcnow()
        )
        embed.add_field(name="🔒 Current Access", value="Arrival zone only", inline=True)
        embed.add_field(name="✅ Required Step", value="Complete verification", inline=True)
        embed.add_field(name="🛡 Security", value="TrapAI Enabled", inline=True)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text="TrapAI Security • The Hood • New Arrival")
        await ch.send(embed=embed)


@bot.event
async def on_member_remove(member):
    await log(
        member.guild,
        LOG_CHANNELS["leaves"],
        "Member Left",
        f"{member}",
        discord.Color.red()
    )


@bot.event
async def on_member_update(before, after):
    # Boost detection
    if before.premium_since is None and after.premium_since is not None:
        await log(
            after.guild,
            LOG_CHANNELS["boost"],
            "Server Boosted 🚀",
            f"{after.mention} just boosted the server!\nTotal Boosts: **{after.guild.premium_subscription_count}**",
            discord.Color.nitro_pink() if hasattr(discord.Color, "nitro_pink") else discord.Color.purple()
        )

    if before.roles != after.roles:
        removed_roles = [role for role in before.roles if role not in after.roles]
        added_roles = [role for role in after.roles if role not in before.roles]

        for role in added_roles:
            await log(
                after.guild,
                LOG_CHANNELS["roles"],
                "Role Added",
                f"User: {after.mention}\nRole: {role.mention}",
                discord.Color.green()
            )

        for role in removed_roles:
            await log(
                after.guild,
                LOG_CHANNELS["roles"],
                "Role Removed",
                f"User: {after.mention}\nRole: {role.mention}",
                discord.Color.red()
            )

    if before.nick != after.nick:
        await log(
            after.guild,
            LOG_CHANNELS["nicknames"],
            "Nickname Changed",
            f"User: {after.mention}\nBefore: {before.nick or before.name}\nAfter: {after.nick or after.name}",
            discord.Color.blurple()
        )


@bot.event
async def on_guild_role_create(role):
    await log(
        role.guild,
        LOG_CHANNELS["role_create"],
        "Role Created",
        f"Role: {role.mention}\nName: {role.name}",
        discord.Color.green()
    )


@bot.event
async def on_guild_role_delete(role):
    await log(
        role.guild,
        LOG_CHANNELS["role_delete"],
        "Role Deleted",
        f"Role Name: {role.name}",
        discord.Color.red()
    )


@bot.event
async def on_guild_channel_create(channel):
    await log(
        channel.guild,
        LOG_CHANNELS["channel_create"],
        "Channel Created",
        f"Channel: {channel.mention if hasattr(channel, 'mention') else channel.name}\nType: {channel.type}",
        discord.Color.green()
    )


@bot.event
async def on_guild_channel_delete(channel):
    await log(
        channel.guild,
        LOG_CHANNELS["channel_delete"],
        "Channel Deleted",
        f"Channel Name: {channel.name}\nType: {channel.type}",
        discord.Color.red()
    )


@bot.event
async def on_guild_channel_update(before, after):
    changes = []
    if before.name != after.name:
        changes.append(f"Name: `{before.name}` → `{after.name}`")
    if isinstance(before, discord.TextChannel) and isinstance(after, discord.TextChannel):
        if before.topic != after.topic:
            changes.append("Topic changed")
        if before.slowmode_delay != after.slowmode_delay:
            changes.append(f"Slowmode: `{before.slowmode_delay}s` → `{after.slowmode_delay}s`")

    if changes:
        await log(
            after.guild,
            LOG_CHANNELS["channel_update"],
            "Channel Updated",
            f"Channel: {after.mention if hasattr(after, 'mention') else after.name}\n" + "\n".join(changes),
            discord.Color.gold()
        )


@bot.event
async def on_guild_emojis_update(guild, before, after):
    added = [e for e in after if e not in before]
    removed = [e for e in before if e not in after]

    for emoji in added:
        await log(guild, LOG_CHANNELS["emoji"], "Emoji Added", f"Emoji: {emoji} (`{emoji.name}`)", discord.Color.green())

    for emoji in removed:
        await log(guild, LOG_CHANNELS["emoji"], "Emoji Removed", f"Name: `{emoji.name}`", discord.Color.red())


@bot.event
async def on_guild_stickers_update(guild, before, after):
    added = [s for s in after if s not in before]
    removed = [s for s in before if s not in after]

    for sticker in added:
        await log(guild, LOG_CHANNELS["stickers"], "Sticker Added", f"Sticker: `{sticker.name}`", discord.Color.green())

    for sticker in removed:
        await log(guild, LOG_CHANNELS["stickers"], "Sticker Removed", f"Name: `{sticker.name}`", discord.Color.red())


@bot.event
async def on_message_delete(message):
    if not message.guild or message.author.bot:
        return
    await log(
        message.guild,
        LOG_CHANNELS["messages"],
        "Message Deleted",
        f"Author: {message.author}\nChannel: {message.channel.mention}\nContent: {message.content or 'None'}",
        discord.Color.red()
    )


@bot.event
async def on_message_edit(before, after):
    if before.author.bot or before.content == after.content or not before.guild:
        return
    await log(
        before.guild,
        LOG_CHANNELS["messages"],
        "Message Edited",
        f"Author: {before.author}\nChannel: {before.channel.mention}\nBefore: {before.content}\nAfter: {after.content}",
        discord.Color.gold()
    )


@bot.event
async def on_voice_state_update(member, before, after):
    now = time.time()

    if before.channel is None and after.channel is not None:
        vc_join_time[member.id] = now
        await log(member.guild, LOG_CHANNELS["vc"], "VC Join", f"{member.mention} joined **{after.channel.name}**", discord.Color.blue())

    elif before.channel is not None and after.channel is None:
        joined = vc_join_time.pop(member.id, None)
        if joined:
            vc_stats[member.id] = vc_stats.get(member.id, 0) + int(now - joined)
        await log(member.guild, LOG_CHANNELS["vc"], "VC Leave", f"{member.mention} left **{before.channel.name}**", discord.Color.red())

    elif before.channel != after.channel and before.channel is not None and after.channel is not None:
        joined = vc_join_time.pop(member.id, None)
        if joined:
            vc_stats[member.id] = vc_stats.get(member.id, 0) + int(now - joined)
        vc_join_time[member.id] = now
        await log(member.guild, LOG_CHANNELS["vc"], "VC Moved", f"{member.mention} moved from **{before.channel.name}** to **{after.channel.name}**", discord.Color.gold())

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
                timestamp=datetime.utcnow()
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
                timestamp=datetime.utcnow()
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
# BASIC COMMANDS
# ============================================================
@bot.command()
async def ping(ctx):
    latency = round(bot.latency * 1000)
    embed = discord.Embed(
        title="🏓 Pong!",
        description=f"Latency: **{latency}ms**",
        color=discord.Color.green(),
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text="TrapAI")
    await ctx.send(embed=embed)


@bot.command(name="cmds")
async def cmds(ctx):
    view = CmdsView(ctx.author.id)
    embed = view.home_embed(ctx.guild)
    await ctx.send(embed=embed, view=view)


# ============================================================
# VC COMMANDS  (all upgraded — owner or VC-mod unless noted)
# ============================================================

def _vc_embed(title, description, color=discord.Color.dark_grey()):
    return discord.Embed(title=title, description=description, color=color, timestamp=datetime.utcnow())


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
    await ctx.send("⚙️ Setting up The Hood...")

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
    await arrival_category.set_permissions(unverified_role, view_channel=True, send_messages=False, read_message_history=True)
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

    await welcome_channel.set_permissions(unverified_role, view_channel=True, send_messages=False, read_message_history=True)
    await rules_channel.set_permissions(unverified_role, view_channel=True, send_messages=False, read_message_history=True)
    await verify_channel.set_permissions(unverified_role, view_channel=True, send_messages=False, read_message_history=True)

    await general_channel.set_permissions(verified_role, view_channel=True, send_messages=True, read_message_history=True)
    await media_channel.set_permissions(verified_role, view_channel=True, send_messages=True, read_message_history=True)
    await bot_channel.set_permissions(verified_role, view_channel=True, send_messages=True, read_message_history=True)

    await jail_channel.set_permissions(jail_role, view_channel=True, send_messages=True, read_message_history=True)
    await jail_logs_channel.set_permissions(jail_role, view_channel=False)

    embed = discord.Embed(
        title="✅ The Hood Setup Complete",
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
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text="TrapAI Setup System • The Hood")
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
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text="TrapAI VC System • The Hood")
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
async def sendverify(ctx):
    embed = discord.Embed(
        title="🤖 TrapAI Security Gateway",
        description=(
            "Welcome to **The Hood** 🏘️🔥\n\n"
            "Before entering, your account must pass **TrapAI Security Verification**.\n\n"
            "### Access Requirements\n"
            "• Account must not be restricted\n"
            "• Verification must be completed\n"
            "• Entry is locked until approved\n\n"
            "Click the button below to begin your scan and get access."
        ),
        color=discord.Color.dark_grey(),
        timestamp=datetime.utcnow()
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
    embed.set_footer(text="TrapAI Security • The Hood Protection")
    await ctx.send(embed=embed, view=VerifyView())


@bot.command()
@commands.has_permissions(administrator=True)
async def sendtickets(ctx):
    """Send the ticket panel to the current channel."""
    embed = discord.Embed(
        title="🎫 TrapAI Support Tickets",
        description=(
            "Need help from staff? Open a private support ticket.\n\n"
            "```yaml\n"
            "Ticket System: ACTIVE\n"
            "Response Time: As soon as possible\n"
            "Privacy: Staff only\n"
            "```\n"
            "Click **Open Ticket** below to create your private ticket channel."
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.utcnow()
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
    embed.set_footer(text="TrapAI Ticket System • The Hood Support")
    await ctx.send(embed=embed, view=TicketOpenView())

    await log(
        ctx.guild,
        LOG_CHANNELS["tickets"],
        "Ticket Panel Sent",
        f"Administrator: {ctx.author.mention}\nChannel: {ctx.channel.mention}",
        discord.Color.blurple()
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def rules(ctx):
    rules_channel = discord.utils.get(ctx.guild.text_channels, name="rules")
    if not rules_channel:
        await ctx.send("❌ I couldn't find a channel named **rules**.")
        return

    embed = discord.Embed(
        title="🤖 TrapAI Server Rules",
        description=(
            "Welcome to **The Hood** 🏘️🔥\n\n"
            "To stay in The Hood, all members must follow the rules below.\n\n"
            "```yaml\n"
            "TrapAI Status: ACTIVE\n"
            "Rule Enforcement: ENABLED\n"
            "Violation Response: WARNING / TIMEOUT / JAIL / BAN\n"
            "```"
        ),
        color=discord.Color.dark_grey(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="1️⃣ Respect Everyone", value="No harassment, racism, hate speech, threats, or bullying.", inline=False)
    embed.add_field(name="2️⃣ No Spamming", value="Do not flood chats, mass mention, or spam messages, emojis, or reactions.", inline=False)
    embed.add_field(name="3️⃣ No Ads or Links", value="No self-promo, invite links, or outside advertising without staff approval.", inline=False)
    embed.add_field(name="4️⃣ Keep It Clean", value="No harmful content, scams, doxxing, or anything meant to harm the server.", inline=False)
    embed.add_field(name="5️⃣ Use Channels Correctly", value="Keep topics in the right channels and follow staff directions.", inline=False)
    embed.add_field(name="6️⃣ VC Rules", value="No mic spam, earrape, screaming, or trolling in voice channels.", inline=False)
    embed.add_field(name="7️⃣ Verification Required", value="Unverified users stay restricted until they pass TrapAI verification.", inline=False)
    embed.add_field(name="8️⃣ Staff Decisions", value="Arguing with moderation actions in public may lead to more punishment. Contact staff calmly.", inline=False)
    embed.add_field(
        name="⚠ TrapAI Enforcement",
        value="Breaking rules may result in:\n• Warning\n• Timeout\n• Jail\n• Ban",
        inline=False
    )
    if ctx.guild.icon:
        embed.set_thumbnail(url=ctx.guild.icon.url)
    embed.set_footer(text="TrapAI Security • The Hood Rules")

    await rules_channel.send(embed=embed)
    await ctx.send(f"✅ TrapAI rules sent to {rules_channel.mention}")
    await log(
        ctx.guild,
        LOG_CHANNELS["mod"],
        "TrapAI Rules Sent",
        f"Administrator: {ctx.author.mention}\nChannel: {rules_channel.mention}",
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
            timestamp=datetime.utcnow()
        )
        embed.set_footer(text=f"Verified by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        await log(ctx.guild, LOG_CHANNELS["verification"], "Member Verified", f"Staff: {ctx.author.mention}\nUser: {member.mention}", discord.Color.green())

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
            timestamp=datetime.utcnow()
        )
        embed.set_footer(text=f"Unverified by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        await log(ctx.guild, LOG_CHANNELS["verification"], "Member Unverified", f"Staff: {ctx.author.mention}\nUser: {member.mention}", discord.Color.orange())

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
            timestamp=datetime.utcnow()
        )
        embed.set_footer(text=f"Action by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        await log(ctx.guild, LOG_CHANNELS["verification"], "TrapAI Verification Denied", f"Staff: {ctx.author.mention}\nUser: {member.mention}\nReason: {reason}", discord.Color.red())

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
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Issued by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)
    await log(ctx.guild, LOG_CHANNELS["mod"], "TrapAI Warning Issued", f"Staff: {ctx.author.mention}\nUser: {member.mention}\nReason: {reason}", discord.Color.orange())


@bot.command()
@commands.has_permissions(administrator=True)
async def trapscan(ctx, member: discord.Member):
    embed = discord.Embed(
        title="🤖 TrapAI Live Scan",
        description=(
            f"Scanning {member.mention}...\n\n"
            "```yaml\n"
            f"Username: {member}\n"
            f"User ID: {member.id}\n"
            f"Account Created: {member.created_at.strftime('%Y-%m-%d')}\n"
            f"Joined Server: {member.joined_at.strftime('%Y-%m-%d') if member.joined_at else 'Unknown'}\n"
            f"Bot Account: {member.bot}\n"
            "Threat Rating: LOW\n"
            "Status: MONITORING\n"
            "```"
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text="TrapAI Security • Live User Analysis")
    await ctx.send(embed=embed)


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

        old_task = jailed_users.get(member.id)
        if old_task:
            old_task.cancel()

        jailed_users[member.id] = asyncio.create_task(
            auto_unjail(ctx.guild.id, member.id, seconds)
        )

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
            timestamp=datetime.utcnow()
        )
        embed.set_footer(text="TrapAI Enforcement • Restriction Active")
        await ctx.send(embed=embed)
        await log(ctx.guild, LOG_CHANNELS["jail"], "Member Jailed", f"Moderator: {ctx.author.mention}\nUser: {member.mention}\nDuration: {duration}\nReason: {reason}", discord.Color.red())

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
            timestamp=datetime.utcnow()
        )
        embed.set_footer(text="TrapAI Enforcement • Access Updated")
        await ctx.send(embed=embed)
        await log(ctx.guild, LOG_CHANNELS["jail"], "Member Unjailed", f"Moderator: {ctx.author.mention}\nUser: {member.mention}\nReason: {reason}", discord.Color.green())

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
    await log(ctx.guild, LOG_CHANNELS["clears"], "Messages Cleared", f"Moderator: {ctx.author.mention}\nChannel: {ctx.channel.mention}\nAmount: {amount}", discord.Color.orange())


@bot.command()
@commands.has_permissions(manage_channels=True)
async def lock(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False)
    await ctx.send("🔒 Channel locked")
    await log(ctx.guild, LOG_CHANNELS["mod"], "Channel Locked", f"Moderator: {ctx.author.mention}\nChannel: {ctx.channel.mention}", discord.Color.red())


@bot.command()
@commands.has_permissions(manage_channels=True)
async def unlock(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=True)
    await ctx.send("🔓 Channel unlocked")
    await log(ctx.guild, LOG_CHANNELS["mod"], "Channel Unlocked", f"Moderator: {ctx.author.mention}\nChannel: {ctx.channel.mention}", discord.Color.green())


@bot.command()
@commands.has_permissions(administrator=True)
async def restart(ctx):
    await ctx.send("🔄 Restarting bot...")
    await log(ctx.guild, LOG_CHANNELS["mod"], "Bot Restarted", f"Administrator: {ctx.author.mention}", discord.Color.blurple())
    os.execv(sys.executable, [sys.executable] + sys.argv)


@bot.command()
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason="No reason provided"):
    await member.kick(reason=reason)
    await ctx.send(f"👢 {member} was kicked.\nReason: {reason}")
    await log(ctx.guild, LOG_CHANNELS["kicks"], "Member Kicked", f"Moderator: {ctx.author.mention}\nUser: {member}\nReason: {reason}", discord.Color.orange())


@bot.command()
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason="No reason provided"):
    await member.ban(reason=reason)
    await ctx.send(f"🔨 {member} was banned.\nReason: {reason}")
    await log(ctx.guild, LOG_CHANNELS["bans"], "Member Banned", f"Moderator: {ctx.author.mention}\nUser: {member}\nReason: {reason}", discord.Color.red())


@bot.command()
@commands.has_permissions(moderate_members=True)
async def timeout(ctx, member: discord.Member, minutes: int, *, reason="No reason provided"):
    until = discord.utils.utcnow() + timedelta(minutes=minutes)
    await member.timeout(until, reason=reason)
    await ctx.send(f"⏳ {member} was timed out for {minutes} minute(s).\nReason: {reason}")
    await log(ctx.guild, LOG_CHANNELS["timeouts"], "Member Timed Out", f"Moderator: {ctx.author.mention}\nUser: {member}\nDuration: {minutes} minute(s)\nReason: {reason}", discord.Color.gold())


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

    role_names = ", ".join([role.name for role in staff_roles])
    await member.remove_roles(*staff_roles, reason=f"Stripped by {ctx.author}")
    await ctx.send(f"✅ Removed all staff roles from {member.mention}")
    await log(ctx.guild, LOG_CHANNELS["strips"], "Staff Roles Stripped", f"Administrator: {ctx.author.mention}\nUser: {member}\nRemoved Roles: {role_names}", discord.Color.dark_red())


@bot.command()
@commands.has_permissions(administrator=True)
async def nuke(ctx):
    old_channel = ctx.channel
    channel_name = old_channel.name
    new_channel = await old_channel.clone(reason=f"Nuked by {ctx.author}")
    await old_channel.delete()
    await new_channel.send("💥 Channel nuked.")
    await log(ctx.guild, LOG_CHANNELS["mod"], "Channel Nuked", f"Administrator: {ctx.author.mention}\nChannel: #{channel_name}", discord.Color.dark_red())


@bot.command()
@commands.has_permissions(administrator=True)
async def lockdown(ctx):
    for channel in ctx.guild.text_channels:
        await channel.set_permissions(ctx.guild.default_role, send_messages=False)
    await ctx.send("🔒 Server lockdown activated.")
    await log(ctx.guild, LOG_CHANNELS["lockdowns"], "Server Lockdown Enabled", f"Administrator: {ctx.author.mention}", discord.Color.red())


@bot.command()
@commands.has_permissions(administrator=True)
async def unlockdown(ctx):
    for channel in ctx.guild.text_channels:
        await channel.set_permissions(ctx.guild.default_role, send_messages=True)
    await ctx.send("🔓 Server lockdown removed.")
    await log(ctx.guild, LOG_CHANNELS["unlockdowns"], "Server Lockdown Removed", f"Administrator: {ctx.author.mention}", discord.Color.green())


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
    await log(ctx.guild, LOG_CHANNELS["roleall"], "Role Given To All", f"Administrator: {ctx.author.mention}\nRole: {role.name}\nMembers Affected: {count}", discord.Color.blue())


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
        timestamp=datetime.utcnow()
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
    embed = discord.Embed(
        title=f"User Info — {member}",
        color=discord.Color.blue(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="ID", value=str(member.id))
    embed.add_field(name="Created", value=member.created_at.strftime('%Y-%m-%d'))
    embed.add_field(name="Joined", value=member.joined_at.strftime('%Y-%m-%d') if member.joined_at else "Unknown")
    embed.add_field(name="Roles", value=roles, inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)
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
        "time": datetime.utcnow()
    })
    count = len(user_warns)

    embed = discord.Embed(
        title="⚠ Member Warned",
        description=f"{member.mention} has received a warning.",
        color=discord.Color.orange(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Total Warnings", value=str(count), inline=False)
    embed.set_footer(text=f"Issued by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)
    await log(ctx.guild, LOG_CHANNELS["warns"], "Member Warned", f"Moderator: {ctx.author.mention}\nUser: {member.mention}\nReason: {reason}\nTotal Warnings: {count}", discord.Color.orange())


@bot.command()
async def warnings(ctx, member: discord.Member = None):
    member = member or ctx.author
    user_warns = WARNINGS.get(ctx.guild.id, {}).get(member.id, [])

    embed = discord.Embed(
        title=f"⚠ Warnings — {member}",
        color=discord.Color.orange(),
        timestamp=datetime.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    if not user_warns:
        embed.description = "This user has no warnings."
    else:
        for index, warn_entry in enumerate(user_warns, start=1):
            embed.add_field(
                name=f"Warning #{index}",
                value=(
                    f"**Reason:** {warn_entry['reason']}\n"
                    f"**Moderator:** {warn_entry['moderator']}\n"
                    f"**Date:** {warn_entry['time'].strftime('%Y-%m-%d %H:%M UTC')}"
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
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Cleared by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)
    await log(ctx.guild, LOG_CHANNELS["warns"], "Warnings Cleared", f"Moderator: {ctx.author.mention}\nUser: {member.mention}\nWarnings Removed: {count}", discord.Color.green())


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
            timestamp=datetime.utcnow()
        )
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_footer(text=f"Muted by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        await log(ctx.guild, LOG_CHANNELS["mutes"], "Member Muted", f"Moderator: {ctx.author.mention}\nUser: {member.mention}\nReason: {reason}", discord.Color.red())

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
            timestamp=datetime.utcnow()
        )
        embed.set_footer(text=f"Unmuted by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        await log(ctx.guild, LOG_CHANNELS["mutes"], "Member Unmuted", f"Moderator: {ctx.author.mention}\nUser: {member.mention}\nReason: {reason}", discord.Color.green())

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
            timestamp=datetime.utcnow()
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
    await log(ctx.guild, LOG_CHANNELS["hides"], "Channel Hidden", f"Moderator: {ctx.author.mention}\nChannel: {ctx.channel.mention}", discord.Color.orange())


@bot.command()
@commands.has_permissions(manage_channels=True)
async def unhide(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, view_channel=True)
    await ctx.send(f"👀 {ctx.channel.mention} is now visible to everyone.")
    await log(ctx.guild, LOG_CHANNELS["hides"], "Channel Unhidden", f"Moderator: {ctx.author.mention}\nChannel: {ctx.channel.mention}", discord.Color.green())


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
    await log(ctx.guild, LOG_CHANNELS["massroles"], "Mass Role Added", f"Administrator: {ctx.author.mention}\nRole: {role.name}\nScope: {scope}\nMembers Affected: {count}", discord.Color.blue())


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
    await log(ctx.guild, LOG_CHANNELS["massroles"], "Mass Role Removed", f"Administrator: {ctx.author.mention}\nRole: {role.name}\nScope: {scope}\nMembers Affected: {count}", discord.Color.blue())


# ============================================================
# RUN
# ============================================================
token = os.getenv("DISCORD_TOKEN")

if not token:
    raise ValueError("DISCORD_TOKEN is not set. Check your .env file or environment variables.")

bot.run(token)
