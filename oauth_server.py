"""
TrapAI Verification OAuth Callback Server
==========================================
Standalone web service that completes the real Discord OAuth2 flow behind
the "Authenticate via Discord" verify button in bot.py. Deploy this
separately from the bot (Railway, Render, Fly.io, a VPS, etc.) — it just
needs to be reachable at a public https:// URL and share the same bot
token as bot.py, since both talk to Discord's REST API independently.

What it does, when someone clicks Authorize:
  1. Discord redirects their browser here with a one-time `code`.
  2. Exchange that code for an OAuth access token.
  3. Look up who they are (GET /users/@me with that token).
  4. Grant them the Verified role (and remove Unverified) in whichever
     server sent them here — encoded in the `state` param by ,sendverify,
     no shared database/filesystem with bot.py needed.
  5. If a backup server was configured (via ,setverifybackup), add them to
     it too, using the guilds.join scope — this is what actually delivers
     on the panel's "you won't lose your place" promise.

Required environment variables (put these in THIS service's own .env or
host's env var settings — never the same file as the bot's, and never
paste the client secret in chat/commits):
  DISCORD_CLIENT_ID       — from the Discord Developer Portal (OAuth2 tab)
  DISCORD_CLIENT_SECRET   — same page, click "Reset Secret" to reveal
  DISCORD_TOKEN           — the SAME bot token bot.py uses
  DISCORD_OAUTH_REDIRECT_URI — this service's own public URL + /callback,
                                e.g. https://your-service.up.railway.app/callback
                                Must exactly match a Redirect URI you've
                                added on the same OAuth2 Developer Portal page.
Optional:
  PORT                    — defaults to 8080 (most hosts inject this themselves)
  VERIFIED_ROLE_NAME       — defaults to "✅ Ballin Member" — MUST match bot.py's
                              VERIFIED_ROLE constant exactly, character for
                              character. If you ever rename that role again,
                              update this env var here too (or verification
                              silently stops granting anything, since this
                              service has no way to know the role was renamed
                              — it doesn't share bot.py's code or config).
  UNVERIFIED_ROLE_NAME     — defaults to "🚫 Unverified" (must match bot.py's UNVERIFIED_ROLE)
  VERIFICATION_LOG_CHANNEL_NAME — defaults to "verification-logs" — the text
                              channel name this service posts a log embed to
                              after each verification attempt (found by name
                              per-guild, since there's no shared config with
                              bot.py's per-guild log channel overrides).
"""

import os
import logging
from datetime import datetime, timezone
from urllib.parse import quote

from aiohttp import web, ClientSession, ClientTimeout
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("verify-oauth")

CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
BOT_TOKEN     = os.getenv("DISCORD_TOKEN")
REDIRECT_URI  = os.getenv("DISCORD_OAUTH_REDIRECT_URI")
PORT          = int(os.getenv("PORT", "8080"))

VERIFIED_ROLE_NAME   = os.getenv("VERIFIED_ROLE_NAME", "✅ Ballin Member")
UNVERIFIED_ROLE_NAME = os.getenv("UNVERIFIED_ROLE_NAME", "🚫 Unverified")
LOG_CHANNEL_NAME      = os.getenv("VERIFICATION_LOG_CHANNEL_NAME", "verification-logs")

DISCORD_API = "https://discord.com/api/v10"

for _name, _val in (
    ("DISCORD_CLIENT_ID", CLIENT_ID),
    ("DISCORD_CLIENT_SECRET", CLIENT_SECRET),
    ("DISCORD_TOKEN", BOT_TOKEN),
    ("DISCORD_OAUTH_REDIRECT_URI", REDIRECT_URI),
):
    if not _val:
        raise RuntimeError(
            f"{_name} is not set. Add it to this service's environment "
            f"(its own .env file or your host's env var settings)."
        )


# Plain (non-f-string) constant — its braces are literal CSS/JS syntax, never
# interpreted by Python, so nothing here needs escaping. _page() interpolates
# this whole block in as one variable rather than writing CSS/JS directly
# inside an f-string, which is what keeps that f-string itself brace-free.
_STYLE_AND_SCRIPT = """
<style>
  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; }
  body {
    margin: 0; position: relative; overflow-x: hidden;
    background: #0a0a0d;
    color: #fff; font-family: 'Inter', -apple-system, "Segoe UI", sans-serif;
  }
  a { color: inherit; }
  code { font-family: 'SFMono-Regular', Consolas, monospace; background: rgba(255, 255, 255, .08);
         border-radius: 4px; padding: .1rem .35rem; font-size: .9em; }

  /* ── Nav ─────────────────────────────────────────────── */
  .nav {
    display: flex; align-items: center; justify-content: space-between;
    padding: 1.1rem clamp(1.25rem, 5vw, 3rem);
    border-bottom: 1px solid rgba(255, 255, 255, .06);
    position: sticky; top: 0; z-index: 10;
    background: rgba(10, 10, 13, .85); backdrop-filter: blur(12px);
  }
  .nav-brand { display: flex; align-items: center; gap: .55rem; font-weight: 800; font-size: 1.05rem; color: var(--gold); }
  .nav-logo { font-size: 1.3rem; }
  .nav-right { display: flex; align-items: center; gap: 1.5rem; }
  .nav-link { font-size: .85rem; font-weight: 600; color: #c7cad0; text-decoration: none; transition: color .2s ease; }
  .nav-link:hover { color: var(--gold); }
  .nav-badge {
    display: flex; align-items: center; gap: .4rem; font-size: .75rem; font-weight: 600;
    color: #9a9ea5; border: 1px solid rgba(255, 255, 255, .08); border-radius: 20px; padding: .35rem .8rem;
  }
  .nav-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 8px var(--accent); }
  @media (max-width: 480px) { .nav-link { display: none; } }

  /* ── Hero ────────────────────────────────────────────── */
  .hero {
    position: relative; overflow: hidden;
    padding: clamp(3rem, 8vw, 5.5rem) 1.5rem clamp(2.5rem, 6vw, 4rem);
    display: flex; flex-direction: column; align-items: center; text-align: center;
  }
  .bg-deco { position: absolute; inset: 0; overflow: hidden; z-index: 0; pointer-events: none; }
  .bg-tri { position: absolute; opacity: .1; }
  .bg-tri.left { top: 10%; left: -6%; width: 260px; height: 260px; transform: rotate(-12deg); }
  .bg-tri.right { top: 18%; right: -8%; width: 320px; height: 320px; transform: rotate(20deg); }
  .bg-dot { position: absolute; border-radius: 50%; background: var(--accent); opacity: .5; }
  #confetti { position: fixed; inset: 0; width: 100%; height: 100%; pointer-events: none; z-index: 20; }

  .hero > *:not(.bg-deco) { position: relative; z-index: 1; }

  .pill {
    display: inline-flex; align-items: center; gap: .5rem; font-size: .78rem; font-weight: 700;
    letter-spacing: .02em; color: var(--accent); background: var(--accent-soft);
    border: 1px solid var(--accent-soft2); border-radius: 20px; padding: .45rem 1rem; margin-bottom: 1.6rem;
    opacity: 0; animation: dropIn .5s ease forwards;
  }

  .avatar-wrap { position: relative; width: 84px; height: 84px; margin: 0 auto 1rem;
                 opacity: 0; animation: popIn .5s .12s cubic-bezier(.34, 1.56, .64, 1) forwards; }
  .icon { width: 100%; height: 100%; border-radius: 50%; object-fit: cover; display: block;
          border: 3px solid var(--accent); box-shadow: 0 0 0 6px var(--accent-soft), 0 0 34px var(--accent-soft); }
  .icon-fallback { display: flex; align-items: center; justify-content: center; font-size: 2.1rem; background: #16171b; }
  .badge { position: absolute; bottom: -2px; right: -2px; width: 28px; height: 28px; border-radius: 50%;
           background: var(--accent); color: #0b0b0d; display: flex; align-items: center; justify-content: center;
           font-weight: 800; font-size: .95rem; border: 3px solid #0a0a0d; transform: scale(0);
           animation: popIn .4s .4s cubic-bezier(.34, 1.56, .64, 1) forwards; }

  .server-name { color: #8e9297; font-size: .78rem; letter-spacing: .12em; text-transform: uppercase; margin-bottom: .9rem;
                 opacity: 0; animation: dropIn .5s .2s ease forwards; }

  .hero-title { margin: 0 0 1rem; font-weight: 800; letter-spacing: -.02em; line-height: 1.12;
                font-size: clamp(1.9rem, 6vw, 3.1rem);
                opacity: 0; animation: dropIn .55s .28s ease forwards; }
  .hero-title .accent { color: var(--accent); display: block; }
  .hero-title .sub-line { color: #f2f3f5; display: block; }

  .hero-sub { max-width: 480px; color: #a7abb3; line-height: 1.65; font-size: 1rem; margin: 0 0 1.8rem;
              opacity: 0; animation: dropIn .5s .36s ease forwards; }

  .steps { list-style: none; padding: 0; margin: 0 0 1.6rem; text-align: left; display: flex; flex-direction: column;
           gap: .5rem; width: 100%; max-width: 360px; }
  .step { display: flex; align-items: center; gap: .6rem; background: rgba(255, 255, 255, .04);
          border: 1px solid rgba(255, 255, 255, .07); border-radius: 10px; padding: .55rem .8rem;
          font-size: .85rem; color: #d4d6da; opacity: 0; transform: translateY(8px); animation: dropIn .45s ease forwards; }
  .step:nth-child(1) { animation-delay: .48s; } .step:nth-child(2) { animation-delay: .58s; } .step:nth-child(3) { animation-delay: .68s; }
  .step-icon { width: 20px; height: 20px; border-radius: 50%; display: flex; align-items: center; justify-content: center;
               font-size: .7rem; font-weight: 800; flex-shrink: 0; }
  .step.done .step-icon { background: var(--gold); color: #0b0b0d; }
  .step.pending .step-icon { background: #4f4f57; color: #dcddde; }

  .btn { display: inline-flex; align-items: center; gap: .45rem;
         background: var(--accent); color: #0b0b0d; text-decoration: none; font-weight: 700;
         padding: .85rem 1.8rem; border-radius: 10px; font-size: .95rem; box-shadow: 0 10px 28px var(--accent-soft);
         transition: transform .2s ease, box-shadow .2s ease;
         opacity: 0; animation: dropIn .5s .78s ease forwards; }
  .btn:hover { transform: translateY(-3px); box-shadow: 0 14px 34px var(--accent-soft); }
  .btn .arrow { transition: transform .2s ease; }
  .btn:hover .arrow { transform: translateX(4px); }

  /* ── Stats bar ───────────────────────────────────────── */
  .stats-bar {
    display: flex; flex-wrap: wrap; justify-content: center; gap: clamp(2rem, 6vw, 4.5rem);
    padding: 2.2rem 1.5rem; border-top: 1px solid rgba(255, 255, 255, .06); border-bottom: 1px solid rgba(255, 255, 255, .06);
  }
  .stat-item { text-align: center; }
  .stat-value { font-size: clamp(1.4rem, 3.5vw, 1.9rem); font-weight: 800; color: var(--gold); }
  .stat-label { margin-top: .25rem; font-size: .72rem; letter-spacing: .1em; color: #8a8d94; text-transform: uppercase; }

  /* ── Commands section ────────────────────────────────── */
  .commands { padding: clamp(3rem, 7vw, 4.5rem) 1.5rem; text-align: center; border-top: 1px solid rgba(255, 255, 255, .06); }
  .commands h2 { margin: 0 0 .5rem; font-size: clamp(1.4rem, 3.5vw, 1.9rem); font-weight: 800; color: #f2f3f5; }
  .cmd-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 1.1rem;
              max-width: 1000px; margin: 0 auto; }
  .cmd-card { background: rgba(255, 255, 255, .03); border: 1px solid rgba(255, 255, 255, .06); border-radius: 14px;
              padding: 1.4rem; text-align: left; }
  .cmd-card-head { display: flex; align-items: center; gap: .6rem; margin-bottom: 1rem; }
  .cmd-icon { font-size: 1.3rem; }
  .cmd-card h3 { margin: 0; font-size: .95rem; font-weight: 700; color: #f2f3f5; }
  .cmd-tags { display: flex; flex-wrap: wrap; gap: .4rem; }
  .cmd-tag { font-family: 'SFMono-Regular', Consolas, monospace; font-size: .72rem; background: rgba(255, 198, 41, .08);
             color: var(--gold); border: 1px solid rgba(255, 198, 41, .18); border-radius: 6px; padding: .28rem .55rem; }

  /* ── Trust section ───────────────────────────────────── */
  .trust { padding: clamp(3rem, 7vw, 4.5rem) 1.5rem; text-align: center; }
  .trust h2 { margin: 0 0 .5rem; font-size: clamp(1.4rem, 3.5vw, 1.9rem); font-weight: 800; color: #f2f3f5; }
  .trust-sub { margin: 0 auto 2.5rem; color: #8a8d94; font-size: .95rem; max-width: 420px; }
  .trust-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 1.1rem;
                max-width: 1000px; margin: 0 auto; }
  .trust-card { background: rgba(255, 255, 255, .03); border: 1px solid rgba(255, 255, 255, .06); border-radius: 14px;
                padding: 1.6rem 1.4rem; text-align: left; transition: border-color .2s ease, transform .2s ease; }
  .trust-card:hover { border-color: rgba(255, 198, 41, .35); transform: translateY(-2px); }
  .trust-icon { font-size: 1.5rem; margin-bottom: .7rem; }
  .trust-card h3 { margin: 0 0 .4rem; font-size: .98rem; font-weight: 700; color: #f2f3f5; }
  .trust-card p { margin: 0; font-size: .85rem; line-height: 1.55; color: #8a8d94; }

  .site-footer { text-align: center; padding: 2rem 1.5rem 2.5rem; font-size: .75rem; color: #5c5f66; letter-spacing: .02em; }

  @keyframes dropIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
  @keyframes popIn { to { opacity: 1; transform: scale(1); } }

  @media (prefers-reduced-motion: reduce) {
    * { animation-duration: .01ms !important; animation-iteration-count: 1 !important; transition: none !important; }
  }
</style>
<script>
document.addEventListener('DOMContentLoaded', function () {
  if (!document.body.classList.contains('ok')) return;
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  var canvas = document.getElementById('confetti');
  if (!canvas) return;
  var ctx = canvas.getContext('2d');
  function resize() { canvas.width = window.innerWidth; canvas.height = window.innerHeight; }
  resize();
  window.addEventListener('resize', resize);
  var colors = ['#FFC629', '#F2B90D', '#ffffff', '#5865F2', '#57F287'];
  var particles = [];
  for (var i = 0; i < 130; i++) {
    particles.push({
      x: Math.random() * canvas.width,
      y: -20 - Math.random() * canvas.height * 0.5,
      w: 6 + Math.random() * 6,
      h: 8 + Math.random() * 10,
      color: colors[Math.floor(Math.random() * colors.length)],
      speed: 2 + Math.random() * 3,
      drift: (Math.random() - 0.5) * 2,
      rot: Math.random() * 360,
      rotSpeed: (Math.random() - 0.5) * 10,
      opacity: 1
    });
  }
  var start = Date.now();
  function frame() {
    var elapsed = Date.now() - start;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    var alive = false;
    particles.forEach(function (p) {
      p.y += p.speed;
      p.x += p.drift;
      p.rot += p.rotSpeed;
      if (elapsed > 2200) p.opacity -= 0.03;
      if (p.opacity > 0 && p.y < canvas.height + 30) alive = true;
      if (p.opacity <= 0) return;
      ctx.save();
      ctx.globalAlpha = Math.max(p.opacity, 0);
      ctx.translate(p.x, p.y);
      ctx.rotate(p.rot * Math.PI / 180);
      ctx.fillStyle = p.color;
      ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h);
      ctx.restore();
    });
    if (alive) {
      requestAnimationFrame(frame);
    } else {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
    }
  }
  requestAnimationFrame(frame);
});
</script>
"""


BRAND_GOLD = "#FFC629"

# Static decorative background markup (faint triangle outlines + scattered
# dots, echoing the landing-page look this was modeled on). No dynamic
# content, so — like _STYLE_AND_SCRIPT — this is a plain string, not an
# f-string, and its braces (there are none here, but the SVG attributes
# below use CSS var() safely via inline style=) need no escaping.
_BG_DECO = """<div class="bg-deco">
  <svg class="bg-tri left" viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg"><polygon points="50,3 97,97 3,97" style="fill:none;stroke:var(--accent);stroke-width:1.4"/></svg>
  <svg class="bg-tri right" viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg"><polygon points="50,3 97,97 3,97" style="fill:none;stroke:var(--accent);stroke-width:1.4"/></svg>
  <span class="bg-dot" style="top:14%;left:12%;width:5px;height:5px;"></span>
  <span class="bg-dot" style="top:28%;left:84%;width:4px;height:4px;"></span>
  <span class="bg-dot" style="top:62%;left:6%;width:6px;height:6px;"></span>
  <span class="bg-dot" style="top:74%;left:90%;width:4px;height:4px;"></span>
  <span class="bg-dot" style="top:8%;left:55%;width:3px;height:3px;"></span>
  <span class="bg-dot" style="top:48%;left:46%;width:3px;height:3px;"></span>
  <span class="bg-dot" style="top:85%;left:35%;width:4px;height:4px;"></span>
  <span class="bg-dot" style="top:20%;left:28%;width:3px;height:3px;"></span>
</div>"""

# Static command showcase — a representative slice of what TrapAI can do,
# grouped by category. This service is a separate deployment from bot.py
# with no shared state/API, so it can't introspect the bot's live command
# list — this is a hand-picked, honest sample, not the full list (that's
# what ,help inside Discord is for).
COMMAND_CATEGORIES = [
    ("🛡️", "Moderation", ["kick", "ban", "mute", "timeout", "warn", "jail"]),
    ("🎤", "Voice Channels", ["vclock", "vckick", "vctransfer", "vcclaim"]),
    ("🎫", "Tickets", ["ticket", "claimticket", "closeticket"]),
    ("📊", "Stats & Economy", ["chatstats", "vcstats", "daily", "blackjack"]),
    ("🏷️", "Roles", ["role", "roleall", "massrole", "br"]),
    ("🎉", "Community", ["poll", "giveaway", "birthday", "snipe"]),
]

# Static trust-grid content — verification value props, not guild-specific.
TRUST_ITEMS = [
    ("🛡️", "Raid Protection", "Verified members are shielded from mass-join raids and impersonation attempts."),
    ("🔐", "No Password Needed", "OAuth2 confirms who you are without this bot ever seeing your Discord password."),
    ("🔄", "Backup Server Safety", "If this server ever goes down, verified members keep their spot in our backup."),
    ("⚡", "Instant Access", "Your role is granted the moment you authorize — no waiting on staff to approve you."),
]


def _page(title: str, message: str, ok: bool = True, *, guild_name: str = None,
          guild_icon_url: str = None, guild_id: str = None, role_name: str = None,
          member_count: int = None, steps: list = None) -> str:
    color = BRAND_GOLD if ok else "#ED4245"
    accent_style = (
        f"<style>:root {{ --accent: {color}; --accent-soft: {color}22; "
        f"--accent-soft2: {color}4d; --gold: {BRAND_GOLD}; }}</style>"
    )

    nav_html = (
        '<nav class="nav">'
        '<div class="nav-brand"><span class="nav-logo">🛡️</span> TrapAI</div>'
        '<div class="nav-right">'
        '<a class="nav-link" href="#commands">Commands</a>'
        f'<div class="nav-badge"><span class="nav-dot"></span>{"Verified" if ok else "Action Needed"}</div>'
        '</div>'
        '</nav>'
    )

    pill_html = f'<div class="pill">{"🟢 Verification Successful" if ok else "🔴 Verification Failed"}</div>'

    icon_html = (
        f'<img class="icon" src="{guild_icon_url}" alt="">' if guild_icon_url
        else '<div class="icon icon-fallback">🌐</div>'
    )
    icon_html = f'<div class="avatar-wrap">{icon_html}<div class="badge">{"✓" if ok else "✕"}</div></div>'

    server_line = f'<div class="server-name">{guild_name}</div>' if guild_name else ""

    sub_line = "You're all set — welcome back." if ok else "Let's get this sorted."
    hero_title_html = f'<h1 class="hero-title"><span class="accent">{title}</span><span class="sub-line">{sub_line}</span></h1>'

    steps_html = ""
    if steps:
        items = "".join(
            f'<li class="step {"done" if done else "pending"}">'
            f'<span class="step-icon">{"✓" if done else "✕"}</span>{label}</li>'
            for done, label in steps
        )
        steps_html = f'<ul class="steps">{items}</ul>'

    button_html = (
        f'<a class="btn" href="https://discord.com/channels/{guild_id}" target="_blank">'
        f'Return to Discord <span class="arrow">→</span></a>'
        if ok and guild_id else ""
    )

    confetti_html = '<canvas id="confetti"></canvas>' if ok else ""

    # Stats bar — real numbers only, never fabricated. Always shows at least
    # the OAuth2 trust badge; adds member count / granted role when known.
    stat_pairs = []
    if member_count:
        stat_pairs.append((f"{member_count:,}", "MEMBERS"))
    if role_name:
        stat_pairs.append(("✓", role_name.upper()))
    stat_pairs.append(("OAuth2", "SECURED VIA"))
    stats_html = "".join(
        f'<div class="stat-item"><div class="stat-value">{v}</div><div class="stat-label">{l}</div></div>'
        for v, l in stat_pairs
    )

    trust_html = "".join(
        f'<div class="trust-card"><div class="trust-icon">{icon}</div><h3>{h}</h3><p>{d}</p></div>'
        for icon, h, d in TRUST_ITEMS
    )

    commands_html = "".join(
        '<div class="cmd-card"><div class="cmd-card-head">'
        f'<span class="cmd-icon">{icon}</span><h3>{cat}</h3></div>'
        '<div class="cmd-tags">' + "".join(f'<span class="cmd-tag">,{cmd}</span>' for cmd in cmds) + '</div>'
        '</div>'
        for icon, cat, cmds in COMMAND_CATEGORIES
    )

    return f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
{accent_style}
{_STYLE_AND_SCRIPT}
</head>
<body class="{'ok' if ok else 'err'}">
{nav_html}
<section class="hero">
  {_BG_DECO}
  {pill_html}
  {icon_html}
  {server_line}
  {hero_title_html}
  <p class="hero-sub">{message}</p>
  {steps_html}
  {button_html}
  {confetti_html}
</section>
<section class="stats-bar">{stats_html}</section>
<section class="commands" id="commands">
  <h2>Commands</h2>
  <p class="trust-sub">A taste of what TrapAI can do — the full list is inside Discord via <code>,help</code>.</p>
  <div class="cmd-grid">{commands_html}</div>
</section>
<section class="trust">
  <h2>Why we ask you to verify</h2>
  <p class="trust-sub">Layered protection, explained.</p>
  <div class="trust-grid">{trust_html}</div>
</section>
<footer class="site-footer">Secured by Discord OAuth2 • TrapAI</footer>
</body></html>"""


async def _exchange_code(session: ClientSession, code: str) -> dict | None:
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    async with session.post(f"{DISCORD_API}/oauth2/token", data=data) as resp:
        if resp.status != 200:
            log.warning("Token exchange failed (%s): %s", resp.status, await resp.text())
            return None
        return await resp.json()


async def _get_identity(session: ClientSession, access_token: str) -> dict | None:
    headers = {"Authorization": f"Bearer {access_token}"}
    async with session.get(f"{DISCORD_API}/users/@me", headers=headers) as resp:
        if resp.status != 200:
            log.warning("Fetching identity failed (%s): %s", resp.status, await resp.text())
            return None
        return await resp.json()


async def _get_guild_info(session: ClientSession, guild_id: str) -> dict | None:
    """Real per-guild name/icon/member count for the success page — this
    bot serves many servers, so the page must reflect whichever one
    actually sent the person here, not a hardcoded name."""
    headers = {"Authorization": f"Bot {BOT_TOKEN}"}
    async with session.get(f"{DISCORD_API}/guilds/{guild_id}?with_counts=true", headers=headers) as resp:
        if resp.status != 200:
            log.warning("Fetching guild info for %s failed (%s): %s", guild_id, resp.status, await resp.text())
            return None
        return await resp.json()


def _guild_icon_url(guild_id: str, icon_hash: str) -> str | None:
    if not icon_hash:
        return None
    ext = "gif" if icon_hash.startswith("a_") else "png"
    return f"https://cdn.discordapp.com/icons/{guild_id}/{icon_hash}.{ext}?size=128"


async def _get_guild_roles(session: ClientSession, guild_id: str) -> list:
    headers = {"Authorization": f"Bot {BOT_TOKEN}"}
    async with session.get(f"{DISCORD_API}/guilds/{guild_id}/roles", headers=headers) as resp:
        if resp.status != 200:
            log.warning("Fetching roles for guild %s failed (%s): %s", guild_id, resp.status, await resp.text())
            return []
        return await resp.json()


async def _set_role(session: ClientSession, guild_id: str, user_id: str, role_id: str, add: bool):
    headers = {"Authorization": f"Bot {BOT_TOKEN}"}
    url = f"{DISCORD_API}/guilds/{guild_id}/members/{user_id}/roles/{role_id}"
    method = session.put if add else session.delete
    async with method(url, headers=headers) as resp:
        if resp.status not in (200, 204):
            log.warning("%s role %s for user %s in guild %s failed (%s): %s",
                        "Adding" if add else "Removing", role_id, user_id, guild_id, resp.status, await resp.text())


async def _join_backup_guild(session: ClientSession, backup_guild_id: str, user_id: str, access_token: str):
    headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}
    url = f"{DISCORD_API}/guilds/{backup_guild_id}/members/{user_id}"
    async with session.put(url, headers=headers, json={"access_token": access_token}) as resp:
        if resp.status not in (200, 201, 204):
            body = await resp.text()
            log.warning("Adding user %s to backup guild %s failed (%s): %s", user_id, backup_guild_id, resp.status, body)
            return False
        return True


async def _find_log_channel(session: ClientSession, guild_id: str):
    """Best-effort: find a text channel named LOG_CHANNEL_NAME in the origin
    guild. No shared config with bot.py's per-guild log overrides, since
    this runs as a completely separate process/deployment — name-based
    lookup is the only option available here."""
    headers = {"Authorization": f"Bot {BOT_TOKEN}"}
    async with session.get(f"{DISCORD_API}/guilds/{guild_id}/channels", headers=headers) as resp:
        if resp.status != 200:
            log.warning("Fetching channels for guild %s failed (%s): %s", guild_id, resp.status, await resp.text())
            return None
        channels = await resp.json()
    for ch in channels:
        if ch.get("type") == 0 and ch.get("name") == LOG_CHANNEL_NAME:
            return ch["id"]
    return None


async def _post_log_embed(session: ClientSession, channel_id, embed: dict):
    if not channel_id:
        return
    headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}
    async with session.post(f"{DISCORD_API}/channels/{channel_id}/messages", headers=headers, json={"embeds": [embed]}) as resp:
        if resp.status not in (200, 201):
            log.warning("Posting verification log to channel %s failed (%s): %s", channel_id, resp.status, await resp.text())


async def handle_callback(request: web.Request) -> web.Response:
    # state = "<origin_guild_id>" or "<origin_guild_id>:<backup_guild_id>" —
    # parsed up front so even the error/declined pages below can show real
    # per-guild branding instead of a generic page.
    state = request.query.get("state", "")
    parts = state.split(":", 1)
    origin_guild_id = parts[0] if parts and parts[0] and parts[0].isdigit() else None
    backup_guild_id = parts[1] if len(parts) > 1 and parts[1] else None

    timeout = ClientTimeout(total=15)
    async with ClientSession(timeout=timeout) as session:
        guild_info = await _get_guild_info(session, origin_guild_id) if origin_guild_id else None
        guild_name = guild_info.get("name") if guild_info else None
        guild_icon = _guild_icon_url(origin_guild_id, guild_info.get("icon")) if guild_info else None
        member_count = guild_info.get("approximate_member_count") if guild_info else None

        def page(title, message, ok=True, role_name=None, steps=None):
            return _page(
                title, message, ok,
                guild_name=guild_name, guild_icon_url=guild_icon,
                guild_id=origin_guild_id, role_name=role_name, member_count=member_count,
                steps=steps,
            )

        error = request.query.get("error")
        if error:
            return web.Response(
                text=page("Verification Cancelled", "You declined the authorization request. You can close this tab and click the button again if that was a mistake.", ok=False),
                content_type="text/html", status=200,
            )

        code = request.query.get("code")
        if not code:
            return web.Response(text=page("Missing Code", "Discord didn't send an authorization code. Try clicking the button again.", ok=False),
                                 content_type="text/html", status=400)

        if not origin_guild_id:
            return web.Response(text=page("Invalid Request", "This verification link is malformed or expired. Ask staff to re-post the verify panel.", ok=False),
                                 content_type="text/html", status=400)

        token_data = await _exchange_code(session, code)
        if not token_data or "access_token" not in token_data:
            return web.Response(text=page("Authorization Failed", "Discord rejected that authorization code (it may have expired — codes are single-use and short-lived). Please try again.", ok=False),
                                 content_type="text/html", status=400)
        access_token = token_data["access_token"]

        identity = await _get_identity(session, access_token)
        if not identity:
            return web.Response(text=page("Couldn't Identify You", "Got an access token but couldn't fetch your Discord profile. Please try again.", ok=False),
                                 content_type="text/html", status=502)
        user_id = identity["id"]
        username = identity.get("username", "there")

        roles = await _get_guild_roles(session, origin_guild_id)
        verified_role_id   = next((r["id"] for r in roles if r["name"] == VERIFIED_ROLE_NAME), None)
        unverified_role_id = next((r["id"] for r in roles if r["name"] == UNVERIFIED_ROLE_NAME), None)

        role_granted = False
        if verified_role_id:
            await _set_role(session, origin_guild_id, user_id, verified_role_id, add=True)
            role_granted = True
        else:
            log.warning("Verified role %r not found in guild %s — role not granted", VERIFIED_ROLE_NAME, origin_guild_id)
        if unverified_role_id:
            await _set_role(session, origin_guild_id, user_id, unverified_role_id, add=False)

        joined_backup = False
        if backup_guild_id:
            joined_backup = await _join_backup_guild(session, backup_guild_id, user_id, access_token)

        # Post a log entry to Discord itself, not just this service's own
        # server-side logs — staff watching #verification-logs (or whatever
        # VERIFICATION_LOG_CHANNEL_NAME is set to) need to see this happened.
        log_channel_id = await _find_log_channel(session, origin_guild_id)
        role_field = "✅ Granted" if role_granted else f"⚠️ Role `{VERIFIED_ROLE_NAME}` not found in this server — nothing granted"
        embed = {
            "title": "✅ Member Verified (OAuth)" if role_granted else "⚠️ Verification Attempted — Role Missing",
            "color": 0x2ECC71 if role_granted else 0xE67E22,
            "fields": [
                {"name": "👤 User", "value": f"<@{user_id}> (`{user_id}` / {username})", "inline": True},
                {"name": "🏷️ Verified Role", "value": role_field, "inline": True},
            ],
            "footer": {"text": "TrapAI Security • OAuth Verification"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if backup_guild_id:
            embed["fields"].append({
                "name": "🔗 Backup Server",
                "value": "✅ Joined" if joined_backup else "❌ Failed to join",
                "inline": True,
            })
        await _post_log_embed(session, log_channel_id, embed)

    extra = " You've also been added to our backup server, so you won't lose your place if this one ever goes away." if joined_backup else ""
    steps = [
        (True, "Discord identity confirmed"),
        (role_granted, "Verified role granted" if role_granted else "Verified role not found on server"),
    ]
    if backup_guild_id:
        steps.append((joined_backup, "Added to backup server" if joined_backup else "Couldn't join backup server"))

    return web.Response(
        text=page(
            "You're Verified! ✅",
            f"Welcome, {username} — you now have full access back in Discord.{extra} You can close this tab.",
            role_name=VERIFIED_ROLE_NAME if role_granted else None,
            steps=steps,
        ),
        content_type="text/html", status=200,
    )


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def build_authorize_url(guild_id: int, backup_guild_id: int | None = None) -> str:
    """Helper mirrored in bot.py — kept here too so this file is a complete,
    standalone reference for the exact URL shape the callback expects."""
    state = str(guild_id) if not backup_guild_id else f"{guild_id}:{backup_guild_id}"
    return (
        "https://discord.com/api/oauth2/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={quote(REDIRECT_URI, safe='')}"
        "&response_type=code"
        "&scope=identify%20guilds.join"
        f"&state={quote(state, safe='')}"
    )


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/callback", handle_callback)
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    return app


if __name__ == "__main__":
    log.info("Starting verify OAuth callback server on port %s", PORT)
    log.info("Redirect URI configured as: %s", REDIRECT_URI)
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
