import discord
from discord.ext import commands, tasks
from datetime import datetime
import time

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix=",", intents=intents)

BAD_WORDS = ["badword1", "badword2"]
LINKS = ["http", "https", "discord.gg"]

LOG_CHANNELS = {
    "vc": "vc-logs",
    "messages": "message-logs",
    "joins": "server-joins",
    "leaves": "server-leaves",
    "raids": "raid-attempts",
}

WELCOME_CHANNEL = "welcome"
ANNOUNCE_CHANNEL = "announcements"

xp = {}
user_messages = {}

WHITELIST = set()
RAID_JOINS = []
RAID_TIME = 15
RAID_LIMIT = 5

vc_stats = {}
vc_join_time = {}

ANTI_RAID_ENABLED = True

async def log(guild, channel_name, title, description, color):
    channel = discord.utils.get(guild.text_channels, name=channel_name)
    if not channel:
        return
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.utcnow()
    )
    await channel.send(embed=embed)

@bot.event
async def on_ready():
    weekly_leaderboard.start()
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

    await log(
        member.guild,
        LOG_CHANNELS["joins"],
        "Member Joined",
        f"{member.mention}\nAccount created: {member.created_at.strftime('%Y-%m-%d')}",
        discord.Color.green()
    )

    ch = discord.utils.get(member.guild.text_channels, name=WELCOME_CHANNEL)
    if ch:
        await ch.send(f"Welcome {member.mention}")

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
    if before.author.bot or before.content == after.content:
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

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    msg = message.content.lower()
    if any(w in msg for w in BAD_WORDS) or any(l in msg for l in LINKS):
        await message.delete()
        return

    xp[message.author.id] = xp.get(message.author.id, 0) + 5
    await bot.process_commands(message)

@bot.command()
async def ping(ctx):
    await ctx.send("pong")

@bot.command()
@commands.has_permissions(manage_messages=True)
async def clear(ctx, amount: int):
    await ctx.channel.purge(limit=amount + 1)

@bot.command()
@commands.has_permissions(manage_channels=True)
async def lock(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False)
    await ctx.send("🔒 Channel locked")

@bot.command()
@commands.has_permissions(manage_channels=True)
async def unlock(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=True)
    await ctx.send("🔓 Channel unlocked")

@bot.command()
async def leaderboard(ctx):
    top = sorted(xp.items(), key=lambda x: x[1], reverse=True)[:10]
    desc = "\n".join([f"**#{i+1}** <@{u}> — `{v} XP`" for i, (u, v) in enumerate(top)])
    embed = discord.Embed(
        title="🏆 Server Leaderboard",
        description=desc or "No data yet",
        color=discord.Color.purple()
    )
    await ctx.send(embed=embed)

@tasks.loop(hours=168)
async def weekly_leaderboard():
    for guild in bot.guilds:
        channel = discord.utils.get(guild.text_channels, name=ANNOUNCE_CHANNEL)
        if channel:
            top = sorted(xp.items(), key=lambda x: x[1], reverse=True)[:5]
            desc = "\n".join([f"**#{i+1}** <@{u}> — `{v} XP`" for i, (u, v) in enumerate(top)])
            embed = discord.Embed(
                title="🏆 Weekly Leaderboard",
                description=desc or "No data this week",
                color=discord.Color.gold()
            )
            await channel.send(embed=embed)

@bot.command()
async def vcstats(ctx, member: discord.Member = None):
    member = member or ctx.author
    seconds = vc_stats.get(member.id, 0)
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)

    embed = discord.Embed(
        title=f"VC Stats — {member}",
        description=f"Time in VC: **{hours}h {minutes}m {sec}s**",
        color=discord.Color.blue()
    )
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

    await member.remove_roles(*staff_roles, reason=f"Stripped by {ctx.author}")
    await ctx.send(f"✅ Removed all staff roles from {member.mention}")

@bot.command()
async def whois(ctx, member: discord.Member = None):
    member = member or ctx.author
    roles = ", ".join(r.mention for r in member.roles if r.name != "@everyone") or "None"

    embed = discord.Embed(
        title=f"User Info — {member}",
        color=discord.Color.blue(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="ID", value=member.id)
    embed.add_field(name="Created", value=member.created_at.strftime('%Y-%m-%d'))
    embed.add_field(name="Joined", value=member.joined_at.strftime('%Y-%m-%d'))
    embed.add_field(name="Roles", value=roles, inline=False)

    await ctx.send(embed=embed)
 bot.run("MTQ2ODAzOTU0NDgwODAxODA1MQ.GYmhnP.ACQdvPo30ivZZ16hQdRslrvfutSpR_5GYs1wh8")

