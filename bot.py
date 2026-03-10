from dotenv import load_dotenv
load_dotenv()

import os
import sys
import time
import random
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
    "vc": "vc-logs",
    "messages": "msg-logs",
    "joins": "join-logs",
    "leaves": "leave-logs",
    "raids": "raid-attempts",
    "mod": "server-logs",
    "roles": "role-logs"
}

WELCOME_CHANNEL = "welcome"

UNVERIFIED_ROLE = "🚫💨 Unverified Smoker"
VERIFIED_ROLE = "✅💨 Verified Smoker"

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

ANTI_RAID_ENABLED = True

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

async def log(guild, channel_name, title, description, color):
    channel = discord.utils.get(guild.text_channels, name=channel_name)
    if not channel:
        print(f"[LOG ERROR] Channel not found: {channel_name}")
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
        print(f"[LOG SUCCESS] Sent log to #{channel_name}")
    except discord.Forbidden:
        print(f"[LOG ERROR] Missing permission in #{channel_name}")
    except discord.HTTPException as e:
        print(f"[LOG ERROR] Failed to send log in #{channel_name}: {e}")

class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Verify",
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

        if not verified_role:
            await interaction.response.send_message(
                f"❌ The role **{VERIFIED_ROLE}** was not found.",
                ephemeral=True
            )
            return

        try:
            if unverified_role and unverified_role in member.roles:
                await member.remove_roles(unverified_role, reason="Button verification")

            if verified_role not in member.roles:
                await member.add_roles(verified_role, reason="Button verification")

            embed = discord.Embed(
                title="✅ Verification Complete",
                description=f"{member.mention}, you are now a **Verified Smoker** 💨",
                color=discord.Color.green(),
                timestamp=datetime.utcnow()
            )
            embed.set_footer(text="Welcome to Smokers Island 🌴")

            await interaction.response.send_message(embed=embed, ephemeral=True)

            await log(
                guild,
                LOG_CHANNELS["mod"],
                "Member Verified",
                f"User: {member.mention}\nMethod: Verification Button",
                discord.Color.green()
            )

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I can't manage your roles. Move my bot role higher.",
                ephemeral=True
            )
        except discord.HTTPException:
            await interaction.response.send_message(
                "❌ Something went wrong while verifying you.",
                ephemeral=True
            )

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
            title="🌴💨 Welcome to Smokers Island!",
            description=(
                f"💨 Welcome {member.mention} to **Smokers Island**!\n\n"
                f"🔥 Light up the chat and enjoy your stay.\n"
                f"📜 Make sure to check **#rules**.\n"
                f"💬 Jump into **#general-chat** and meet everyone.\n"
                f"📈 Stay active to level up!"
            ),
            color=discord.Color.green(),
            timestamp=datetime.utcnow()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text="Enjoy the island 🌴")
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

@bot.command()
@commands.has_permissions(administrator=True)
async def sendverify(ctx):
    embed = discord.Embed(
        title="💨 Smokers Island Verification",
        description=(
            "Click the button below to verify yourself and gain access to the server.\n\n"
            f"You will receive the **{VERIFIED_ROLE}** role."
        ),
        color=discord.Color.green(),
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text="Smokers Island Verification System 🌴")

    await ctx.send(embed=embed, view=VerifyView())

@bot.command()
@commands.has_permissions(manage_roles=True)
async def verify(ctx, member: discord.Member):
    unverified_role = discord.utils.get(ctx.guild.roles, name=UNVERIFIED_ROLE)
    verified_role = discord.utils.get(ctx.guild.roles, name=VERIFIED_ROLE)

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
            LOG_CHANNELS["mod"],
            "Member Verified",
            f"Staff: {ctx.author.mention}\nUser: {member.mention}",
            discord.Color.green()
        )

    except discord.Forbidden:
        await ctx.send("❌ I can't manage that member's roles. Move my bot role higher.")
    except discord.HTTPException:
        await ctx.send("❌ Something went wrong while verifying that member.")

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
