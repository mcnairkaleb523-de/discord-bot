from dotenv import load_dotenv
load_dotenv()

import os
import sys
import time
import random
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
    "vc": 1480434885410947164,
    "messages": 1480434885410947164,
    "joins": 1480436332240310453,
    "leaves": 1480436501622947860,
    "raids": 1480437429306396672,
    "mod": 1480439660810342610,
    "roles": 1480439660810342610,
    "boost": 1480440939276144721,
    "jail": 1480991299820851351,
}

WELCOME_CHANNEL = "welcome"

UNVERIFIED_ROLE = "🚫💨 Unverified Smoker"
VERIFIED_ROLE = "✅💨 Verified Smoker"
JAIL_ROLE = "🔒💨 Jailed"

JOIN_TO_CREATE_CHANNEL_NAME = "➕ Create VC"
TEMP_VC_CATEGORY_NAME = "🎤 Private VCs"

levels = {}
xp = {}
xp_cooldown = {}
XP_COOLDOWN = 10

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

ANTI_RAID_ENABLED = True
jailed_users = {}

LEVEL_ROLES = {
    5: "Level 5 — 💨 𝓡𝓲𝓼𝓲𝓷𝓰 𝓢𝓶𝓸𝓴𝓮",
    10: "Level 10 — 💨 𝓢𝓶𝓸𝓴𝓮 𝓢𝓽𝓪𝓻𝓽𝓮𝓻",
    15: "Level 15 — 🌫️ 𝓒𝓪𝓶𝓹 𝓢𝓶𝓸𝓴𝓮𝓻",
    20: "Level 20 — 💨 𝓢𝓶𝓸𝓴𝓮 𝓡𝓾𝓷𝓷𝓮𝓻",
    25: "Level 25 — 🌴 𝓘𝓼𝓵𝓪𝓷𝓭 𝓢𝓶𝓸𝓴𝓮𝓻",
    30: "Level 30 — 💨 𝓢𝓶𝓸𝓴𝓮 𝓒𝓱𝓲𝓮𝓯",
    35: "Level 35 — 💨 𝓢𝓶𝓸𝓴𝓮 𝓦𝓪𝓻𝓭𝓮𝓷",
    40: "Level 40 — 🌴 𝓘𝓼𝓵𝓪𝓷𝓭 𝓣𝓲𝓽𝓪𝓷",
    45: "Level 45 — 💨 𝓢𝓶𝓸𝓴𝓮 𝓔𝓶𝓹𝓮𝓻𝓸𝓻",
    50: "Level 50 — 👑💨 𝓚𝓲𝓷𝓰 𝓸𝓯 𝓢𝓶𝓸𝓴𝓮𝓻𝓼"
}


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


class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Verify Now",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="smokers_island_verify_button"
    )
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        member = interaction.user

        if guild is None:
            await interaction.response.send_message(
                "❌ This button only works in a server.",
                ephemeral=True
            )
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
            embed.set_footer(text="TrapAI Security • Access Denied")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if not verified_role:
            await interaction.response.send_message(
                f"❌ The role **{VERIFIED_ROLE}** was not found.",
                ephemeral=True
            )
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
                    "Welcome to **Smokers Island** 🌴💨"
                ),
                color=discord.Color.green(),
                timestamp=datetime.utcnow()
            )
            success_embed.set_footer(text="TrapAI Security • Access Granted")

            await interaction.edit_original_response(embed=success_embed)

            await log(
                guild,
                LOG_CHANNELS["roles"],
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
                "Welcome to the **Smokers Island** command panel.\n\n"
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
                "📊 Levels & Stats\n"
                "🎤 VC Controls\n"
                "⚙️ Admin"
            ),
            inline=False
        )

        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        embed.set_footer(text="TrapAI Security • Interactive Command Menu")
        return embed

    def moderation_embed(self, guild: discord.Guild):
        embed = discord.Embed(
            title="🛡 Moderation Commands",
            color=discord.Color.red(),
            timestamp=datetime.utcnow()
        )
        embed.add_field(
            name="Commands",
            value=(
                "`,kick @user [reason]`\n"
                "`,ban @user [reason]`\n"
                "`,timeout @user minutes [reason]`\n"
                "`,clear amount`\n"
                "`,lock`\n"
                "`,unlock`\n"
                "`,nuke`\n"
                "`,lockdown`\n"
                "`,unlockdown`"
            ),
            inline=False
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text="TrapAI Security • Moderation Panel")
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
        embed.set_footer(text="TrapAI Security • Jail Panel")
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
        embed.set_footer(text="TrapAI Security • Security Panel")
        return embed

    def levels_embed(self, guild: discord.Guild):
        embed = discord.Embed(
            title="📊 Levels & Stats Commands",
            color=discord.Color.gold(),
            timestamp=datetime.utcnow()
        )
        embed.add_field(
            name="Commands",
            value=(
                "`,level`\n"
                "`,leaderboard`\n"
                "`,vcstats`\n"
                "`,whois @user`\n"
                "`,ping`"
            ),
            inline=False
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text="TrapAI Security • Levels Panel")
        return embed

    def vc_embed(self, guild: discord.Guild):
        embed = discord.Embed(
            title="🎤 VC Control Commands",
            color=discord.Color.blue(),
            timestamp=datetime.utcnow()
        )
        embed.add_field(
            name="Commands",
            value=(
                "`,vclock`\n"
                "`,vcunlock`\n"
                "`,vchide`\n"
                "`,vcshow`\n"
                "`,vcname <name>`\n"
                "`,vclimit <number>`\n"
                "`,vckick @user`\n"
                "`,vcban @user`\n"
                "`,vcunban @user`\n"
                "`,vctransfer @user`\n"
                "`,vcpermit @user`\n"
                "`,vcmod @user`"
            ),
            inline=False
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text="TrapAI Security • VC Panel")
        return embed

    def admin_embed(self, guild: discord.Guild):
        embed = discord.Embed(
            title="⚙️ Admin Commands",
            color=discord.Color.blurple(),
            timestamp=datetime.utcnow()
        )
        embed.add_field(
            name="Commands",
            value=(
                "`,setup`\n"
                "`,rules`\n"
                "`,restart`\n"
                "`,roleall @role`\n"
                "`,strip @user`"
            ),
            inline=False
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text="TrapAI Security • Admin Panel")
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

    @discord.ui.button(label="Levels", style=discord.ButtonStyle.primary, emoji="📊")
    async def levels_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.levels_embed(interaction.guild), view=self)

    @discord.ui.button(label="VC", style=discord.ButtonStyle.primary, emoji="🎤")
    async def vc_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.vc_embed(interaction.guild), view=self)

    @discord.ui.button(label="Admin", style=discord.ButtonStyle.primary, emoji="⚙️")
    async def admin_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.admin_embed(interaction.guild), view=self)


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


def make_bar(current, total, length=12):
    if total <= 0:
        total = 1
    filled = int((current / total) * length)
    filled = max(0, min(filled, length))
    return "█" * filled + "░" * (length - filled)


def get_owned_temp_vc(member: discord.Member):
    voice = member.voice
    if not voice or not voice.channel:
        return None

    channel = voice.channel
    owner_id = temp_vc_owners.get(channel.id)

    if owner_id != member.id:
        return None

    return channel


async def send_vc_control_panel(channel, owner, voice_channel):
    embed = discord.Embed(
        title="🎛 TrapAI VC Dashboard",
        description=(
            f"Welcome to your private VC, {owner.mention}.\n\n"
            f"**Voice Channel:** `{voice_channel.name}`\n"
            f"**Owner:** {owner.mention}\n\n"
            "Use the commands below in this chat to control your VC."
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.utcnow()
    )

    embed.add_field(
        name="🔒 Privacy Controls",
        value=(
            "`,vclock` — lock VC\n"
            "`,vcunlock` — unlock VC\n"
            "`,vchide` — hide VC\n"
            "`,vcshow` — show VC\n"
            "`,vcpermit @user` — allow a user in"
        ),
        inline=False
    )

    embed.add_field(
        name="👑 Ownership Controls",
        value=(
            "`,vctransfer @user` — transfer VC owner\n"
            "`,vcmod @user` — give VC mod powers"
        ),
        inline=False
    )

    embed.add_field(
        name="⚙️ Channel Controls",
        value=(
            "`,vcname new name` — rename VC\n"
            "`,vclimit 5` — set user limit"
        ),
        inline=False
    )

    embed.add_field(
        name="🛡 Member Controls",
        value=(
            "`,vckick @user` — kick from VC\n"
            "`,vcban @user` — ban from VC\n"
            "`,vcunban @user` — unban from VC"
        ),
        inline=False
    )

    embed.add_field(
        name="📌 Notes",
        value=(
            "• These commands only work if you're the current VC owner.\n"
            "• Your VC and chat delete automatically when empty.\n"
            "• Transfer ownership before leaving if you want someone else to keep control."
        ),
        inline=False
    )

    if owner.guild.icon:
        embed.set_thumbnail(url=owner.guild.icon.url)

    embed.set_footer(text="TrapAI VC System • Auto Control Panel")

    await channel.send(embed=embed)


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
            embed.set_footer(text="Smokers Island Anti-Spam")

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
                embed.set_footer(text="Smokers Island Anti-Spam")

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


@bot.event
async def on_ready():
    bot.add_view(VerifyView())
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

    levels.setdefault(member.id, 1)
    xp.setdefault(member.id, 0)

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
                f"Welcome {member.mention} to **Smokers Island** 🌴💨\n\n"
                "```yaml\n"
                "Identity Scan: DETECTED\n"
                "Threat Analysis: ACTIVE\n"
                "Server Access: LOCKED\n"
                "Verification Status: REQUIRED\n"
                "```\n"
                "Your account has entered the **TrapAI arrival zone**.\n\n"
                "To unlock the island:\n"
                "1. Read the rules\n"
                "2. Go to **#verify**\n"
                "3. Press the **Verify Now** button\n\n"
                "Until then, your access stays restricted."
            ),
            color=discord.Color.green(),
            timestamp=datetime.utcnow()
        )

        embed.add_field(name="🔒 Current Access", value="Arrival zone only", inline=True)
        embed.add_field(name="✅ Required Step", value="Complete verification", inline=True)
        embed.add_field(name="🛡 Security", value="TrapAI Enabled", inline=True)

        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text="TrapAI Security • New Arrival Registered")

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
    if before.roles == after.roles:
        return

    removed_roles = [role for role in before.roles if role not in after.roles]
    added_roles = [role for role in after.roles if role not in before.roles]

    if added_roles:
        for role in added_roles:
            await log(
                after.guild,
                LOG_CHANNELS["roles"],
                "Role Added",
                f"User: {after.mention}\nRole: {role.mention}",
                discord.Color.green()
            )

    if removed_roles:
        for role in removed_roles:
            await log(
                after.guild,
                LOG_CHANNELS["roles"],
                "Role Removed",
                f"User: {after.mention}\nRole: {role.mention}",
                discord.Color.red()
            )


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
        await log(
            member.guild,
            LOG_CHANNELS["vc"],
            "VC Join",
            f"{member.mention} joined **{after.channel.name}**",
            discord.Color.blue()
        )

    elif before.channel is not None and after.channel is None:
        joined = vc_join_time.pop(member.id, None)
        if joined:
            vc_stats[member.id] = vc_stats.get(member.id, 0) + int(now - joined)

        await log(
            member.guild,
            LOG_CHANNELS["vc"],
            "VC Leave",
            f"{member.mention} left **{before.channel.name}**",
            discord.Color.red()
        )

    elif before.channel != after.channel and before.channel is not None and after.channel is not None:
        joined = vc_join_time.pop(member.id, None)
        if joined:
            vc_stats[member.id] = vc_stats.get(member.id, 0) + int(now - joined)

        vc_join_time[member.id] = now

        await log(
            member.guild,
            LOG_CHANNELS["vc"],
            "VC Moved",
            f"{member.mention} moved from **{before.channel.name}** to **{after.channel.name}**",
            discord.Color.gold()
        )

    guild = member.guild

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

    user_id = message.author.id
    current_time = time.time()

    levels.setdefault(user_id, 1)
    xp.setdefault(user_id, 0)

    last_xp_time = xp_cooldown.get(user_id, 0)
    if current_time - last_xp_time >= XP_COOLDOWN:
        xp[user_id] += 5
        xp_cooldown[user_id] = current_time

        required_xp = levels[user_id] * 100

        if xp[user_id] >= required_xp:
            xp[user_id] -= required_xp
            levels[user_id] += 1

            new_level = levels[user_id]

            level_messages = [
                f"🌫 {message.author.mention} just rose through the smoke!",
                f"🔥 {message.author.mention}'s fire just got stronger!",
                f"💨 {message.author.mention} leveled up on Smokers Island!",
                f"🌴 {message.author.mention} climbed higher in the smoke ranks!"
            ]

            embed = discord.Embed(
                title="💨🔥 Level Up!",
                description=random.choice(level_messages),
                color=discord.Color.purple(),
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="🏝 Server", value="Smokers Island", inline=True)
            embed.add_field(name="📈 New Level", value=f"**Level {new_level}**", inline=True)
            embed.add_field(name="⭐ XP Left", value=f"**{xp[user_id]}/{new_level * 100}**", inline=True)
            embed.set_thumbnail(url=message.author.display_avatar.url)
            embed.set_footer(text="Keep chatting to rise through the smoke 💨")

            role_name = LEVEL_ROLES.get(new_level)
            if role_name:
                role = discord.utils.get(message.guild.roles, name=role_name)
                if role:
                    try:
                        await message.author.add_roles(role, reason=f"Reached level {new_level}")
                        embed.add_field(name="🎁 Reward", value=f"Received role: {role.mention}", inline=False)

                        await log(
                            message.guild,
                            LOG_CHANNELS["mod"],
                            "Level Role Awarded",
                            f"User: {message.author.mention}\nLevel: {new_level}\nRole: {role.name}",
                            discord.Color.green()
                        )
                    except discord.Forbidden:
                        embed.add_field(name="⚠ Reward", value=f"Could not give role **{role_name}**", inline=False)
                    except discord.HTTPException:
                        embed.add_field(name="⚠ Reward", value=f"Error giving role **{role_name}**", inline=False)

            await message.channel.send(embed=embed)

    await bot.process_commands(message)


@bot.command()
async def ping(ctx):
    await ctx.send("pong")


@bot.command(name="cmds")
async def cmds(ctx):
    view = CmdsView(ctx.author.id)
    embed = view.home_embed(ctx.guild)
    await ctx.send(embed=embed, view=view)


@bot.command()
async def vclock(ctx):
    channel = get_owned_temp_vc(ctx.author)
    if not channel:
        await ctx.send("❌ You must be in your own temporary VC.")
        return

    await channel.set_permissions(ctx.guild.default_role, connect=False)
    await ctx.send(f"🔒 Locked **{channel.name}**")


@bot.command()
async def vcunlock(ctx):
    channel = get_owned_temp_vc(ctx.author)
    if not channel:
        await ctx.send("❌ You must be in your own temporary VC.")
        return

    await channel.set_permissions(ctx.guild.default_role, connect=True)
    await ctx.send(f"🔓 Unlocked **{channel.name}**")


@bot.command()
async def vchide(ctx):
    channel = get_owned_temp_vc(ctx.author)
    if not channel:
        await ctx.send("❌ You must be in your own temporary VC.")
        return

    await channel.set_permissions(ctx.guild.default_role, view_channel=False)
    await ctx.send(f"👻 Hid **{channel.name}**")


@bot.command()
async def vcshow(ctx):
    channel = get_owned_temp_vc(ctx.author)
    if not channel:
        await ctx.send("❌ You must be in your own temporary VC.")
        return

    await channel.set_permissions(ctx.guild.default_role, view_channel=True)
    await ctx.send(f"👀 Made **{channel.name}** visible")


@bot.command()
async def vcname(ctx, *, new_name: str):
    channel = get_owned_temp_vc(ctx.author)
    if not channel:
        await ctx.send("❌ You must be in your own temporary VC.")
        return

    await channel.edit(name=new_name[:100])
    await ctx.send(f"✏ Renamed VC to **{new_name[:100]}**")


@bot.command()
async def vclimit(ctx, limit: int):
    channel = get_owned_temp_vc(ctx.author)
    if not channel:
        await ctx.send("❌ You must be in your own temporary VC.")
        return

    if limit < 0 or limit > 99:
        await ctx.send("❌ Limit must be between 0 and 99.")
        return

    await channel.edit(user_limit=limit)
    await ctx.send(f"👥 Set VC limit to **{limit}**")


@bot.command()
async def vckick(ctx, member: discord.Member):
    channel = get_owned_temp_vc(ctx.author)
    if not channel:
        await ctx.send("❌ You must be in your own temporary VC.")
        return

    if not member.voice or member.voice.channel != channel:
        await ctx.send("❌ That user is not in your VC.")
        return

    await member.move_to(None)
    await ctx.send(f"👢 Kicked {member.mention} from **{channel.name}**")


@bot.command()
async def vcban(ctx, member: discord.Member):
    channel = get_owned_temp_vc(ctx.author)

    if not channel:
        await ctx.send("❌ You must be in your own temporary VC.")
        return

    if member == ctx.author:
        await ctx.send("❌ You cannot VC ban yourself.")
        return

    try:
        await channel.set_permissions(member, connect=False, view_channel=False)

        if member.voice and member.voice.channel == channel:
            await member.move_to(None)

        embed = discord.Embed(
            title="🔨 VC Ban Applied",
            description=f"{member.mention} has been banned from **{channel.name}**.",
            color=discord.Color.red(),
            timestamp=datetime.utcnow()
        )

        embed.set_footer(text=f"VC Owner: {ctx.author}")

        await ctx.send(embed=embed)

    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to do that.")


@bot.command()
async def vcunban(ctx, member: discord.Member):
    channel = get_owned_temp_vc(ctx.author)

    if not channel:
        await ctx.send("❌ You must be in your own temporary VC.")
        return

    try:
        await channel.set_permissions(member, overwrite=None)

        embed = discord.Embed(
            title="✅ VC Ban Removed",
            description=f"{member.mention} can now join **{channel.name}** again.",
            color=discord.Color.green(),
            timestamp=datetime.utcnow()
        )

        embed.set_footer(text=f"VC Owner: {ctx.author}")

        await ctx.send(embed=embed)

    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to do that.")


@bot.command()
async def vctransfer(ctx, member: discord.Member):
    channel = get_owned_temp_vc(ctx.author)

    if not channel:
        await ctx.send("❌ You must be in your own temporary VC.")
        return

    if not member.voice or member.voice.channel != channel:
        await ctx.send("❌ That user must be in your VC.")
        return

    temp_vc_owners[channel.id] = member.id

    await channel.set_permissions(
        member,
        manage_channels=True,
        manage_permissions=True,
        move_members=True,
        connect=True,
        speak=True
    )

    embed = discord.Embed(
        title="👑 VC Ownership Transferred",
        description=f"{member.mention} is now the owner of **{channel.name}**.",
        color=discord.Color.gold(),
        timestamp=datetime.utcnow()
    )

    embed.set_footer(text=f"Transferred by {ctx.author}")
    await ctx.send(embed=embed)


@bot.command()
async def vcpermit(ctx, member: discord.Member):
    channel = get_owned_temp_vc(ctx.author)

    if not channel:
        await ctx.send("❌ You must be in your own temporary VC.")
        return

    try:
        await channel.set_permissions(member, connect=True, view_channel=True)

        embed = discord.Embed(
            title="✅ VC Access Granted",
            description=f"{member.mention} can now join **{channel.name}**.",
            color=discord.Color.green(),
            timestamp=datetime.utcnow()
        )

        await ctx.send(embed=embed)

    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to do that.")


@bot.command()
async def vcmod(ctx, member: discord.Member):
    channel = get_owned_temp_vc(ctx.author)

    if not channel:
        await ctx.send("❌ You must be in your own temporary VC.")
        return

    if not member.voice or member.voice.channel != channel:
        await ctx.send("❌ That user must be in your VC.")
        return

    try:
        await channel.set_permissions(
            member,
            move_members=True,
            mute_members=True,
            deafen_members=True,
            manage_channels=True
        )

        embed = discord.Embed(
            title="🛡 VC Moderator Granted",
            description=f"{member.mention} is now a VC moderator in **{channel.name}**.",
            color=discord.Color.blurple(),
            timestamp=datetime.utcnow()
        )

        await ctx.send(embed=embed)

    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to do that.")


@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx):
    guild = ctx.guild

    await ctx.send("⚙️ Setting up Smokers Island...")

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

    island_category = discord.utils.get(guild.categories, name="🌴 Smokers Island")
    if island_category is None:
        island_category = await guild.create_category("🌴 Smokers Island")

    staff_category = discord.utils.get(guild.categories, name="🛡 Staff HQ")
    if staff_category is None:
        staff_category = await guild.create_category("🛡 Staff HQ")

    restricted_category = discord.utils.get(guild.categories, name="🔒 Restricted")
    if restricted_category is None:
        restricted_category = await guild.create_category("🔒 Restricted")

    temp_vc_category = discord.utils.get(guild.categories, name=TEMP_VC_CATEGORY_NAME)
    if temp_vc_category is None:
        temp_vc_category = await guild.create_category(TEMP_VC_CATEGORY_NAME)

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
    await get_or_create_voice_channel("💨 General VC", island_category)
    await get_or_create_voice_channel("🌴 Chill VC", island_category)

    await welcome_channel.edit(topic="🌴 Arrival Zone • New users are scanned by TrapAI before entering the island")
    await rules_channel.edit(topic="📜 TrapAI server rules and enforcement")
    await verify_channel.edit(topic="🤖 TrapAI Security Gateway • Click the verify button below to unlock Smokers Island")
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
        title="✅ Smokers Island Setup Complete",
        description=(
            "TrapAI setup finished.\n\n"
            "Next steps:\n"
            "1. Move your bot role above the server roles\n"
            "2. Run `,sendverify`\n"
            "3. Run `,rules`\n"
            "4. Update your `LOG_CHANNELS` IDs to the new channels"
        ),
        color=discord.Color.green(),
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text="TrapAI Setup System")

    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def sendverify(ctx):
    embed = discord.Embed(
        title="🤖 TrapAI Security Gateway",
        description=(
            "Welcome to **Smokers Island** 🌴💨\n\n"
            "Before entering the island, your account must pass **TrapAI Security Verification**.\n\n"
            "### Access Requirements\n"
            "• Account must not be restricted\n"
            "• Verification must be completed\n"
            "• Entry is locked until approved\n\n"
            "Click the button below to begin your scan and unlock the full server."
        ),
        color=discord.Color.green(),
        timestamp=datetime.utcnow()
    )

    embed.add_field(
        name="🔓 What You Unlock",
        value=(
            "💬 General Chat\n"
            "🎤 Voice Channels\n"
            "📈 Level System\n"
            "🔥 Community Access\n"
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

    embed.set_footer(text="TrapAI Security • Smokers Island Protection")

    await ctx.send(embed=embed, view=VerifyView())


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
            "Welcome to **Smokers Island** 🌴💨\n\n"
            "Before entering deeper into the island, all members must follow the rules below.\n\n"
            "```yaml\n"
            "TrapAI Status: ACTIVE\n"
            "Rule Enforcement: ENABLED\n"
            "Violation Response: WARNING / TIMEOUT / JAIL / BAN\n"
            "```"
        ),
        color=discord.Color.green(),
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

    embed.set_footer(text="TrapAI Security • Smokers Island Rules")

    await rules_channel.send(embed=embed)
    await ctx.send(f"✅ TrapAI rules sent to {rules_channel.mention}")

    await log(
        ctx.guild,
        LOG_CHANNELS["mod"],
        "TrapAI Rules Sent",
        f"Administrator: {ctx.author.mention}\nChannel: {rules_channel.mention}",
        discord.Color.blurple()
    )


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
            title="💨 Member Verified",
            description=f"{member.mention} is now a **Verified Smoker** 🌴🔥",
            color=discord.Color.green(),
            timestamp=datetime.utcnow()
        )
        embed.set_footer(text=f"Verified by {ctx.author}", icon_url=ctx.author.display_avatar.url)

        await ctx.send(embed=embed)

        await log(
            ctx.guild,
            LOG_CHANNELS["roles"],
            "Member Verified",
            f"Staff: {ctx.author.mention}\nUser: {member.mention}",
            discord.Color.green()
        )

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

        await log(
            ctx.guild,
            LOG_CHANNELS["roles"],
            "Member Unverified",
            f"Staff: {ctx.author.mention}\nUser: {member.mention}",
            discord.Color.orange()
        )

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
            description=(
                f"{member.mention} has been moved to restricted access.\n\n"
                f"**Reason:** {reason}"
            ),
            color=discord.Color.red(),
            timestamp=datetime.utcnow()
        )
        embed.set_footer(text=f"Action by {ctx.author}", icon_url=ctx.author.display_avatar.url)

        await ctx.send(embed=embed)

        await log(
            ctx.guild,
            LOG_CHANNELS["roles"],
            "TrapAI Verification Denied",
            f"Staff: {ctx.author.mention}\nUser: {member.mention}\nReason: {reason}",
            discord.Color.red()
        )

    except discord.Forbidden:
        await ctx.send("❌ I can't manage that member's roles.")
    except discord.HTTPException:
        await ctx.send("❌ Something went wrong while denying verification.")


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

    await log(
        ctx.guild,
        LOG_CHANNELS["mod"],
        "TrapAI Warning Issued",
        f"Staff: {ctx.author.mention}\nUser: {member.mention}\nReason: {reason}",
        discord.Color.orange()
    )


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

        await log(
            ctx.guild,
            LOG_CHANNELS["jail"],
            "Member Jailed",
            f"Moderator: {ctx.author.mention}\nUser: {member.mention}\nDuration: {duration}\nReason: {reason}",
            discord.Color.red()
        )

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

        await log(
            ctx.guild,
            LOG_CHANNELS["jail"],
            "Member Unjailed",
            f"Moderator: {ctx.author.mention}\nUser: {member.mention}\nReason: {reason}",
            discord.Color.green()
        )

    except discord.Forbidden:
        await ctx.send("❌ I can't manage that member's roles. Move my bot role higher.")
    except discord.HTTPException:
        await ctx.send("❌ Something went wrong while unjailing that member.")


@bot.command()
@commands.has_permissions(manage_messages=True)
async def clear(ctx, amount: int):
    await ctx.channel.purge(limit=amount + 1)
    await log(
        ctx.guild,
        LOG_CHANNELS["mod"],
        "Messages Cleared",
        f"Moderator: {ctx.author.mention}\nChannel: {ctx.channel.mention}\nAmount: {amount}",
        discord.Color.orange()
    )


@bot.command()
@commands.has_permissions(manage_channels=True)
async def lock(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False)
    await ctx.send("🔒 Channel locked")
    await log(
        ctx.guild,
        LOG_CHANNELS["mod"],
        "Channel Locked",
        f"Moderator: {ctx.author.mention}\nChannel: {ctx.channel.mention}",
        discord.Color.red()
    )


@bot.command()
@commands.has_permissions(manage_channels=True)
async def unlock(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=True)
    await ctx.send("🔓 Channel unlocked")
    await log(
        ctx.guild,
        LOG_CHANNELS["mod"],
        "Channel Unlocked",
        f"Moderator: {ctx.author.mention}\nChannel: {ctx.channel.mention}",
        discord.Color.green()
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def restart(ctx):
    await ctx.send("🔄 Restarting bot...")
    await log(
        ctx.guild,
        LOG_CHANNELS["mod"],
        "Bot Restarted",
        f"Administrator: {ctx.author.mention}",
        discord.Color.blurple()
    )
    os.execv(sys.executable, [sys.executable] + sys.argv)


@bot.command()
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason="No reason provided"):
    await member.kick(reason=reason)
    await ctx.send(f"👢 {member} was kicked.\nReason: {reason}")
    await log(
        ctx.guild,
        LOG_CHANNELS["mod"],
        "Member Kicked",
        f"Moderator: {ctx.author.mention}\nUser: {member}\nReason: {reason}",
        discord.Color.orange()
    )


@bot.command()
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason="No reason provided"):
    await member.ban(reason=reason)
    await ctx.send(f"🔨 {member} was banned.\nReason: {reason}")
    await log(
        ctx.guild,
        LOG_CHANNELS["mod"],
        "Member Banned",
        f"Moderator: {ctx.author.mention}\nUser: {member}\nReason: {reason}",
        discord.Color.red()
    )


@bot.command()
@commands.has_permissions(moderate_members=True)
async def timeout(ctx, member: discord.Member, minutes: int, *, reason="No reason provided"):
    until = discord.utils.utcnow() + timedelta(minutes=minutes)
    await member.timeout(until, reason=reason)
    await ctx.send(f"⏳ {member} was timed out for {minutes} minute(s).\nReason: {reason}")
    await log(
        ctx.guild,
        LOG_CHANNELS["mod"],
        "Member Timed Out",
        f"Moderator: {ctx.author.mention}\nUser: {member}\nDuration: {minutes} minute(s)\nReason: {reason}",
        discord.Color.gold()
    )


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

    if member.avatar:
        embed.set_thumbnail(url=member.avatar.url)
    else:
        embed.set_thumbnail(url=member.default_avatar.url)

    embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command()
async def level(ctx, member: discord.Member = None):
    member = member or ctx.author

    user_level = levels.get(member.id, 1)
    user_xp = xp.get(member.id, 0)
    required = user_level * 100

    embed = discord.Embed(
        title="📊 Level Stats",
        color=discord.Color.blue(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="User", value=member.mention, inline=False)
    embed.add_field(name="Level", value=f"**{user_level}**", inline=True)
    embed.add_field(name="XP", value=f"**{user_xp}/{required}**", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)

    await ctx.send(embed=embed)


@bot.command()
async def leaderboard(ctx):
    if not levels:
        await ctx.send("❌ No leaderboard data yet.")
        return

    sorted_users = sorted(
        levels.items(),
        key=lambda x: (x[1], xp.get(x[0], 0)),
        reverse=True
    )
    top_users = sorted_users[:10]

    medals = {
        1: "🥇",
        2: "🥈",
        3: "🥉"
    }

    embed = discord.Embed(
        title="🏆 TrapAI Level Leaderboard",
        description="Top Smokers rising through the island ranks.",
        color=discord.Color.gold(),
        timestamp=datetime.utcnow()
    )

    lines = []

    for position, (user_id, user_level) in enumerate(top_users, start=1):
        member = ctx.guild.get_member(user_id)
        if not member:
            continue

        user_xp = xp.get(user_id, 0)
        required_xp = user_level * 100
        progress_bar = make_bar(user_xp, required_xp)

        role_name = LEVEL_ROLES.get(user_level)
        role = discord.utils.get(ctx.guild.roles, name=role_name) if role_name else None
        role_text = role.mention if role else (role_name if role_name else "No role")

        medal = medals.get(position, f"`#{position}`")
        crown = " 👑" if position == 1 else ""

        lines.append(
            f"{medal} **{member.display_name}**{crown}\n"
            f"📈 Level: **{user_level}**\n"
            f"⭐ XP: `{user_xp}/{required_xp}`\n"
            f"📊 `{progress_bar}`\n"
            f"🎖 Role: {role_text}"
        )

    if not lines:
        await ctx.send("❌ No leaderboard members found.")
        return

    embed.description = "\n\n".join(lines)

    if ctx.guild.icon:
        embed.set_thumbnail(url=ctx.guild.icon.url)

    embed.set_footer(text="TrapAI Security • Smokers Island")
    await ctx.send(embed=embed)


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
    await log(
        ctx.guild,
        LOG_CHANNELS["mod"],
        "Staff Roles Stripped",
        f"Administrator: {ctx.author.mention}\nUser: {member}\nRemoved Roles: {role_names}",
        discord.Color.dark_red()
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def nuke(ctx):
    old_channel = ctx.channel
    channel_name = old_channel.name
    new_channel = await old_channel.clone(reason=f"Nuked by {ctx.author}")
    await old_channel.delete()
    await new_channel.send("💥 Channel nuked.")
    await log(
        ctx.guild,
        LOG_CHANNELS["mod"],
        "Channel Nuked",
        f"Administrator: {ctx.author.mention}\nChannel: #{channel_name}",
        discord.Color.dark_red()
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def lockdown(ctx):
    for channel in ctx.guild.text_channels:
        await channel.set_permissions(ctx.guild.default_role, send_messages=False)
    await ctx.send("🔒 Server lockdown activated.")
    await log(
        ctx.guild,
        LOG_CHANNELS["mod"],
        "Server Lockdown Enabled",
        f"Administrator: {ctx.author.mention}",
        discord.Color.red()
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def unlockdown(ctx):
    for channel in ctx.guild.text_channels:
        await channel.set_permissions(ctx.guild.default_role, send_messages=True)
    await ctx.send("🔓 Server lockdown removed.")
    await log(
        ctx.guild,
        LOG_CHANNELS["mod"],
        "Server Lockdown Removed",
        f"Administrator: {ctx.author.mention}",
        discord.Color.green()
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def roleall(ctx, role: discord.Role):
    count = 0
    for member in ctx.guild.members:
        try:
            if role not in member.roles:
                await member.add_roles(role, reason=f"Roleall used by {ctx.author}")
                count += 1
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass

    await ctx.send(f"✅ Gave **{role.name}** to {count} member(s).")
    await log(
        ctx.guild,
        LOG_CHANNELS["mod"],
        "Role Given To All",
        f"Administrator: {ctx.author.mention}\nRole: {role.name}\nMembers Affected: {count}",
        discord.Color.blue()
    )


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
    embed.add_field(name="Level", value=str(levels.get(member.id, 1)))
    embed.add_field(name="XP", value=f"{xp.get(member.id, 0)}/{levels.get(member.id, 1) * 100}")
    embed.add_field(name="Roles", value=roles, inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)

    await ctx.send(embed=embed)


token = os.getenv("DISCORD_TOKEN")

if not token:
    raise ValueError("DISCORD_TOKEN is not set. Check your .env file or environment variables.")

bot.run(token)
