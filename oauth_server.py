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

── Stripe billing (optional — subscription/lifetime sales for the ticket
   system, see the "BILLING" section below) ──────────────────────────────
  STRIPE_SECRET_KEY        — from the Stripe Dashboard (Developers > API keys).
                              Leave unset to run this service with billing
                              disabled — every /checkout, /portal, and
                              /stripe/webhook route then returns a friendly
                              "billing not configured" response instead of
                              erroring, so verification keeps working either way.
  STRIPE_WEBHOOK_SECRET    — from the Stripe Dashboard webhook endpoint you
                              create pointing at https://your-domain/stripe/webhook
                              (Developers > Webhooks > Add endpoint > select
                              checkout.session.completed, invoice.paid,
                              invoice.payment_failed, customer.subscription.updated,
                              customer.subscription.deleted > reveal signing secret).
  INTERNAL_API_SECRET      — a random string YOU generate, shared between this
                              service and bot.py's INTERNAL_API_SECRET env var.
                              Authenticates the /internal/* endpoints bot.py
                              calls to check subscription status and generate
                              checkout/portal links — never expose these routes
                              or this secret publicly.
Optional (billing):
  PRODUCT_BASE_URL         — this service's own public https:// URL, e.g.
                              https://your-service.up.railway.app — used to
                              build Stripe success/cancel/return URLs. Falls
                              back to the incoming request's own origin if unset.
  BOT_INVITE_PERMISSIONS   — permission integer used in the "Add TrapAI to your
                              server" link shown after checkout. Defaults to 8
                              (Administrator) — tighten this once you know the
                              exact permission set the ticket system needs.
  SUBSCRIPTION_GRACE_PERIOD_DAYS — days a server keeps ticket access after a
                              failed payment before being suspended. Defaults to 5.
  SUPPORT_CONTACT          — how customers should reach you for support, shown
                              on the /terms page. Defaults to a generic placeholder.
"""

import os
import html
import json
import time
import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import quote

import stripe
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

# ============================================================
# BILLING — Stripe-backed subscriptions for the ticket system
# ============================================================
# Entirely optional: every route below checks STRIPE_SECRET_KEY at request
# time and returns a friendly "billing not configured" response instead of
# erroring when it's unset, so verification keeps working with zero Stripe
# setup. See the module docstring above for what each env var does.
STRIPE_SECRET_KEY     = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
INTERNAL_API_SECRET   = os.getenv("INTERNAL_API_SECRET")
PRODUCT_BASE_URL      = os.getenv("PRODUCT_BASE_URL", "").rstrip("/")
BOT_INVITE_PERMISSIONS = os.getenv("BOT_INVITE_PERMISSIONS", "8")
SUBSCRIPTION_GRACE_PERIOD_DAYS = int(os.getenv("SUBSCRIPTION_GRACE_PERIOD_DAYS", "5"))
SUPPORT_CONTACT       = os.getenv("SUPPORT_CONTACT", "the TrapAI team (see the server you got this bot from)")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# Local JSON persistence — same on-disk convention as bot.py's _save_data /
# _load_data, but this is a completely separate process/deployment with no
# shared filesystem, so it keeps its own copy here.
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)


def _data_path(name: str) -> str:
    return os.path.join(DATA_DIR, f"{name}.json")


def _save_json(name: str, value) -> None:
    try:
        with open(_data_path(name), "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2)
    except OSError:
        pass


def _load_json(name: str, default):
    path = _data_path(name)
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


# Two products, each with its own tiers. "ticket_bot" is the ticket/support
# system only (Starter/Pro/Premium recur monthly, Lifetime is one-time).
# "whole_bot" is the full bot — moderation, economy, everything — sold ONLY
# as one-time purchases (no subscription at all for it), which is what
# "lifetime" means for this product: buy once, keep it, no recurring charge.
# Keys here are the canonical "product"/"tier" strings used everywhere:
# Stripe metadata, the subscription store, and bot.py's ,subscribe command.
PRODUCTS = {
    "ticket_bot": {
        "label": "Ticket Bot",
        "description": "Just the ticket/support system for your server.",
        "tiers": {
            "starter":  {"label": "Starter",  "price_cents": 500,  "interval": "month", "best_value": False},
            "pro":      {"label": "Pro",      "price_cents": 1000, "interval": "month", "best_value": False},
            "premium":  {"label": "Premium",  "price_cents": 2000, "interval": "month", "best_value": False},
            "lifetime": {"label": "Lifetime", "price_cents": 7500, "interval": None,    "best_value": True},
        },
    },
    "whole_bot": {
        "label": "Whole Bot",
        "description": "The full TrapAI bot — moderation, economy, tickets, everything. One-time purchase, never expires, no subscription.",
        "tiers": {
            "regular": {"label": "Regular", "price_cents": 3000, "interval": None, "best_value": False},
            "premium": {"label": "Premium", "price_cents": 5000, "interval": None, "best_value": True},
        },
    },
}


def _tier_cfg(product: str, tier: str):
    return PRODUCTS.get(product, {}).get("tiers", {}).get(tier)


def _price_key(product: str, tier: str) -> str:
    return f"{product}:{tier}"


# "product:tier" -> Stripe Price ID, created once and cached here across
# restarts so re-running this service doesn't spawn duplicate Products/
# Prices in Stripe.
STRIPE_PRICE_IDS: dict = _load_json("stripe_price_ids", {})

# SUBSCRIPTIONS[str(guild_id)] = {
#   "product": "ticket_bot"|"whole_bot",
#   "tier": "starter"|"pro"|"premium"|"lifetime" (ticket_bot) or "regular"|"premium" (whole_bot),
#   "status": "active"|"past_due"|"suspended"|"canceled"|"lifetime",
#   "stripe_customer_id": str, "stripe_subscription_id": str | None,
#   "owner_discord_user_id": str,
#   "current_period_end": unix_ts | None, "grace_period_ends_at": unix_ts | None,
#   "last_reminder_sent_at": unix_ts | None, "created_at": unix_ts, "updated_at": unix_ts,
# } — this file is the single source of truth for who has paid access;
# checkout.session.completed and invoice.paid are what actually provision
# and maintain it, straight from Stripe's webhook, never from the client-side
# success page (which is just a friendly confirmation). A guild that buys
# "whole_bot" (any tier, any status in the active set) is meant to graduate
# out of ticket-only restriction entirely on bot.py's side — see this
# file's PRODUCTS docstring / bot.py's _ticket_only_mode_gate.
SUBSCRIPTIONS: dict = _load_json("subscriptions", {})


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
  .see-all-link { display: inline-block; margin-top: 1.8rem; color: var(--gold); font-weight: 700;
                   font-size: .9rem; text-decoration: none; transition: opacity .2s ease; }
  .see-all-link:hover { opacity: .8; }

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

# Extra CSS/JS only the /commands page needs (search box, category tabs,
# filterable card grid, copy-to-clipboard). Kept separate from
# _STYLE_AND_SCRIPT so the verify page doesn't ship unused styles.
_COMMANDS_PAGE_STYLE_AND_SCRIPT = """
<style>
  .cmd-page-header { padding: clamp(2.5rem, 6vw, 4rem) 1.5rem 1.5rem; text-align: center; }
  .cmd-page-header h1 { font-size: clamp(1.8rem, 5vw, 2.6rem); font-weight: 800; margin: 0 0 .5rem; color: #f2f3f5; }
  .cmd-page-header p { color: #8a8d94; margin: 0 0 2rem; }

  .search-wrap { max-width: 520px; margin: 0 auto 1.5rem; position: relative; }
  .search-input {
    width: 100%; padding: .9rem 1.2rem .9rem 2.8rem; border-radius: 12px; box-sizing: border-box;
    border: 1px solid rgba(255, 255, 255, .1); background: rgba(255, 255, 255, .04);
    color: #fff; font-size: .95rem; font-family: inherit;
  }
  .search-input:focus { outline: none; border-color: var(--gold); box-shadow: 0 0 0 3px rgba(255, 198, 41, .15); }
  .search-icon { position: absolute; left: 1rem; top: 50%; transform: translateY(-50%); opacity: .5; pointer-events: none; }
  .search-hint { font-size: .72rem; color: #5c5f66; margin-top: .6rem; }
  .search-hint kbd { font-family: monospace; background: rgba(255, 255, 255, .08); border-radius: 4px; padding: .05rem .35rem; }

  .cat-tabs { display: flex; flex-wrap: wrap; gap: .5rem; justify-content: center; max-width: 950px;
              margin: 0 auto 1rem; padding: 0 1.5rem; }
  .cat-tab {
    font-family: inherit; font-size: .8rem; font-weight: 600; color: #c7cad0;
    background: rgba(255, 255, 255, .04); border: 1px solid rgba(255, 255, 255, .08);
    border-radius: 20px; padding: .5rem 1rem; cursor: pointer; transition: all .15s ease; white-space: nowrap;
  }
  .cat-tab:hover { border-color: rgba(255, 198, 41, .4); color: #fff; }
  .cat-tab.active { background: var(--gold); color: #0b0b0d; border-color: var(--gold); }
  @media (max-width: 600px) {
    .cat-tabs { flex-wrap: nowrap; overflow-x: auto; justify-content: flex-start; -webkit-overflow-scrolling: touch;
                padding-bottom: .5rem; }
  }

  .result-count { text-align: center; color: #5c5f66; font-size: .82rem; margin: 1.5rem 0 1rem; }

  .cmd-page-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(270px, 1fr)); gap: 1rem;
                    max-width: 1100px; margin: 0 auto; padding: 0 1.5rem 4rem; }
  .cmd-page-card {
    background: rgba(255, 255, 255, .03); border: 1px solid rgba(255, 255, 255, .07); border-radius: 14px;
    padding: 1.2rem; transition: border-color .15s ease, transform .15s ease;
  }
  .cmd-page-card:hover { border-color: rgba(255, 198, 41, .3); transform: translateY(-2px); }
  .cmd-page-card.hidden { display: none; }
  .cmd-page-card-top { display: flex; align-items: flex-start; justify-content: space-between; gap: .5rem; margin-bottom: .5rem; }
  .cmd-page-name { font-family: 'SFMono-Regular', Consolas, monospace; font-size: 1rem; font-weight: 700; color: var(--gold); }
  .copy-btn {
    background: none; border: 1px solid rgba(255, 255, 255, .1); border-radius: 6px; color: #8a8d94;
    font-size: .7rem; padding: .25rem .5rem; cursor: pointer; font-family: inherit;
    transition: all .15s ease; flex-shrink: 0;
  }
  .copy-btn:hover { border-color: var(--gold); color: var(--gold); }
  .cmd-page-desc { font-size: .85rem; color: #a7abb3; line-height: 1.5; margin: 0 0 .6rem; }
  .cmd-page-meta { display: flex; flex-wrap: wrap; gap: .35rem; align-items: center; }
  .cmd-page-cat-tag { font-size: .68rem; background: rgba(255, 255, 255, .05); color: #8a8d94; border-radius: 5px; padding: .15rem .45rem; }
  .cmd-page-alias { font-size: .68rem; color: #5c5f66; font-family: monospace; }
  .cmd-page-sig { font-size: .72rem; color: #6b6e75; font-family: monospace; margin-top: .5rem; }

  .empty-state { text-align: center; color: #5c5f66; padding: 3rem 1.5rem; grid-column: 1 / -1; display: none; }
</style>
<script>
document.addEventListener('DOMContentLoaded', function () {
  var search = document.getElementById('cmd-search');
  var tabs = document.querySelectorAll('.cat-tab');
  var cards = document.querySelectorAll('.cmd-page-card');
  var countEl = document.getElementById('result-count');
  var emptyEl = document.getElementById('empty-state');
  var activeCat = 'all';

  function applyFilter() {
    var q = (search.value || '').trim().toLowerCase();
    var shown = 0;
    cards.forEach(function (card) {
      var matchesCat = activeCat === 'all' || card.dataset.cat === activeCat;
      var matchesSearch = !q || card.dataset.search.indexOf(q) !== -1;
      var visible = matchesCat && matchesSearch;
      card.classList.toggle('hidden', !visible);
      if (visible) shown++;
    });
    countEl.textContent = 'Showing ' + shown + ' of ' + cards.length + ' commands';
    emptyEl.style.display = shown === 0 ? 'block' : 'none';
  }

  if (search) search.addEventListener('input', applyFilter);
  tabs.forEach(function (tab) {
    tab.addEventListener('click', function () {
      tabs.forEach(function (t) { t.classList.remove('active'); });
      tab.classList.add('active');
      activeCat = tab.dataset.cat;
      applyFilter();
    });
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === '/' && document.activeElement !== search) {
      e.preventDefault();
      search.focus();
    }
  });

  document.querySelectorAll('.copy-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var cmd = btn.dataset.cmd;
      var original = btn.textContent;
      function done(ok) {
        btn.textContent = ok ? 'Copied!' : 'Failed';
        setTimeout(function () { btn.textContent = original; }, 1200);
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(cmd).then(function () { done(true); }, function () { done(false); });
      } else {
        done(false);
      }
    });
  });

  applyFilter();
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

# Static command showcase — a small teaser used on the verify page, linking
# through to the full /commands site for the real thing (COMMAND_CATALOG
# below). This service is a separate deployment from bot.py with no shared
# state/API, so it can't introspect the bot's live command list — both of
# these are hand-authored and kept in sync manually with HELP_CATEGORIES /
# the real registered commands in bot.py (see that file's comment on
# HELP_CATEGORIES for the other half of this pairing).
COMMAND_CATEGORIES = [
    ("🛡️", "Moderation", ["kick", "ban", "mute", "timeout", "warn", "jail"]),
    ("🎤", "Voice Channels", ["vclock", "vckick", "vctransfer", "vcclaim"]),
    ("🎫", "Tickets", ["ticket", "claimticket", "closeticket"]),
    ("📊", "Stats & Economy", ["chatstats", "vcstats", "daily", "blackjack"]),
    ("🏷️", "Roles", ["role", "roleall", "massrole", "br"]),
    ("🎉", "Community", ["poll", "giveaway", "birthday", "snipe"]),
]

# COMMAND_CATALOG entries: (category, icon, name, aliases, usage_signature,
# description) — every real command in bot.py, for the full /commands page.
COMMAND_CATALOG = [
    ('Moderation', '🛡️', 'kick', [], '<member> [reason=No reason provided]', 'Kick a member from the server.'),
    ('Moderation', '🛡️', 'ban', [], '<member> [reason=No reason provided]', 'Ban a member from the server.'),
    ('Moderation', '🛡️', 'mute', [], '<member> [reason=No reason provided]', 'Apply the mute role to a member.'),
    ('Moderation', '🛡️', 'unmute', [], '<member> [reason=No reason provided]', 'Remove the mute role from a member.'),
    ('Moderation', '🛡️', 'timeout', [], '<member> <minutes> [reason=No reason provided]', 'Apply a native Discord timeout to a member.'),
    ('Moderation', '🛡️', 'warn', [], '<member> [reason=No reason provided]', 'Log a formal warning against a member.'),
    ('Moderation', '🛡️', 'warnings', [], '[member]', "View a member's warning history."),
    ('Moderation', '🛡️', 'clearwarnings', [], '<member>', "Clear all of a member's logged warnings."),
    ('Moderation', '🛡️', 'modhistory', ['history', 'modlogs'], '[member] [filter_action]', "View a member's full moderation history — warn, jail, ban, kick, timeout, mute, and more."),
    ('Moderation', '🛡️', 'hardban', [], '<user> [reason=No reason provided]', 'Permanently hard-ban a user — they will be instantly re-banned if they rejoin.'),
    ('Moderation', '🛡️', 'unhardban', [], '<user_id> [reason=No reason provided]', 'Remove a hard-ban and unban the user.'),
    ('Moderation', '🛡️', 'hardbans', [], '', 'List all hard-banned users in this server.'),
    ('Moderation', '🛡️', 'clear', [], '<amount>', 'Bulk-delete recent messages in this channel.'),
    ('Moderation', '🛡️', 'purge', [], '<amount> [member]', 'Bulk-delete messages, optionally from a specific user.'),
    ('Moderation', '🛡️', 'lock', [], '', "Lock this channel so @everyone can't send messages."),
    ('Moderation', '🛡️', 'unlock', [], '', 'Unlock this channel so @everyone can send again.'),
    ('Moderation', '🛡️', 'hide', [], '', 'Hide this channel from @everyone.'),
    ('Moderation', '🛡️', 'unhide', [], '', 'Make a hidden channel visible again.'),
    ('Moderation', '🛡️', 'slowmode', [], '<seconds> [channel]', "Set this channel's slowmode delay."),
    ('Moderation', '🛡️', 'nuke', [], '', 'Clone and delete a channel to wipe its history.'),
    ('Moderation', '🛡️', 'lockdown', [], '', 'Lock every text channel on the server at once.'),
    ('Moderation', '🛡️', 'unlockdown', [], '', 'Unlock every text channel on the server at once.'),
    ('Moderation', '🛡️', 'nickname', [], '<member> [new_nick]', "Set or clear a member's nickname."),
    ('Moderation', '🛡️', 'strip', [], '<member>', "Strip every role from a member (except any above the bot's own)."),
    ('Moderation', '🛡️', 'trapwarn', [], '<member> [reason=Suspicious activity detected]', 'Send a formatted security warning to a member.'),
    ('Moderation', '🛡️', 'trapscan', [], '<member>', "Scan a member's account for red flags."),
    ('Moderation', '🛡️', 'restart', [], '', 'Restart the bot process — no need to Ctrl+C and re-run python bot.py.'),
    ('Jail & Anti-Raid', '🔒', 'jail', [], '<member> <duration> [reason=No reason provided]', "Strip a member's roles and confine them until unjailed."),
    ('Jail & Anti-Raid', '🔒', 'unjail', [], '<member> [reason=No reason provided]', 'Release a member from jail early.'),
    ('Jail & Anti-Raid', '🔒', 'setupjail', [], '[channel]', 'Set up the jail system.'),
    ('Jail & Anti-Raid', '🔒', 'antiraid', [], '[state]', "Turn this server's anti-raid auto-ban on or off."),
    ('Jail & Anti-Raid', '🔒', 'raidwhitelist', [], '[action] [member]', 'Manage members exempt from the anti-raid auto-ban.'),
    ('Verification', '🤖', 'verify', [], '<member>', 'Manually verify a member (non-OAuth fallback).'),
    ('Verification', '🤖', 'unverify', [], '<member>', "Revoke a member's verified status."),
    ('Verification', '🤖', 'denyverify', [], '<member> [reason=Verification denied by staff]', 'Deny a pending manual verification request.'),
    ('Verification', '🤖', 'sendverify', [], '', 'Post the verification panel (real Discord OAuth, or a simple fallback button).'),
    ('Verification', '🤖', 'setverifybackup', [], '[guild_id]', 'Set the backup server verified members get auto-joined to.'),
    ('Roles', '🏷️', 'role', [], '', 'Role management commands. Use ,role <subcommand>.'),
    ('Roles', '🏷️', 'roleall', [], '<role>', 'Give a role to every member in the server.'),
    ('Roles', '🏷️', 'massrole', [], '<role> [filter_role]', 'Give a role to every member matching a filter.'),
    ('Roles', '🏷️', 'massunrole', [], '<role> [filter_role]', 'Remove a role from every member matching a filter.'),
    ('Roles', '🏷️', 'restoreallroles', [], '<member>', 'Restore all roles that were snapshotted by ,strip.'),
    ('Roles', '🏷️', 'autorole', [], '[action] [role]', 'Manage roles automatically given to every new member on join.'),
    ('Roles', '🏷️', 'setgifrole', ['setmemberrole'], '[role]', "Set which role is exempt from automod's GIF/link filter."),
    ('Roles', '🏷️', 'protectedrole', [], '[action] [role]', 'Manage the list of roles that can ONLY be granted via ,vouch — never manually.'),
    ('Roles', '🏷️', 'br', ['boosterrole', 'boostrole'], '[action] [arg]', 'Manage your custom booster role — a perk for active server boosters.'),
    ('Voice Channels', '🎤', 'vclock', [], '', 'Lock your temp VC so nobody new can join.'),
    ('Voice Channels', '🎤', 'vcunlock', [], '', 'Unlock your temp VC.'),
    ('Voice Channels', '🎤', 'vchide', [], '', 'Hide your temp VC from the channel list.'),
    ('Voice Channels', '🎤', 'vcshow', [], '', 'Make your hidden temp VC visible again.'),
    ('Voice Channels', '🎤', 'vcname', [], '<new_name>', 'Rename your temp VC.'),
    ('Voice Channels', '🎤', 'vclimit', [], '<limit>', "Set your VC's user limit."),
    ('Voice Channels', '🎤', 'vcbitrate', [], '<kbps>', "Set your temp VC's bitrate."),
    ('Voice Channels', '🎤', 'vcregion', [], '[region=auto]', "Set your VC's voice region."),
    ('Voice Channels', '🎤', 'vckick', [], '<member>', 'Kick a member from your VC.'),
    ('Voice Channels', '🎤', 'vcban', [], '<member>', 'Ban a member from your temp VC.'),
    ('Voice Channels', '🎤', 'vcunban', [], '<member>', 'Unban a member from your VC.'),
    ('Voice Channels', '🎤', 'vcpermit', [], '<member>', 'Allow a specific user into your locked VC.'),
    ('Voice Channels', '🎤', 'vcmute', [], '<member>', 'Server-mute a member in your VC.'),
    ('Voice Channels', '🎤', 'vcunmute', [], '<member>', "Remove a member's server-mute in your VC."),
    ('Voice Channels', '🎤', 'vcdeafen', [], '<member>', 'Server-deafen a member in your VC.'),
    ('Voice Channels', '🎤', 'vcundeafen', [], '<member>', "Remove a member's server-deafen in your VC."),
    ('Voice Channels', '🎤', 'vctransfer', [], '<member>', 'Transfer ownership of your temp VC to someone else.'),
    ('Voice Channels', '🎤', 'vcclaim', [], '', 'Claim ownership of your current temp VC if the owner has left it.'),
    ('Voice Channels', '🎤', 'vcmod', [], '<member>', 'Add a moderator to your temp VC.'),
    ('Voice Channels', '🎤', 'vcremovemod', [], '<member>', 'Remove a moderator from your temp VC.'),
    ('Voice Channels', '🎤', 'vcstats', [], '[target]', 'View voice chat time — a personal rank card by default.'),
    ('Voice Channels', '🎤', 'setupvc', [], '[category_name]', 'Create the ➕ Create VC trigger channel in this server.'),
    ('Voice Channels', '🎤', 'setunmutevc', [], '[action] [channel]', "Manage self-service VCs that instantly clear a member's mute/deafen."),
    ('Tickets', '🎫', 'sendtickets', [], '', 'Send the ticket panel to the current channel.'),
    ('Tickets', '🎫', 'addticketcategory', [], '<key> [rest]', 'Add or update a custom ticket category for this server.'),
    ('Tickets', '🎫', 'removeticketcategory', [], '<key>', 'Remove a custom ticket category from THIS server.'),
    ('Tickets', '🎫', 'ticketcategories', [], '', 'List all ticket categories active in this server.'),
    ('Tickets', '🎫', 'setticketformat', [], '<key> [template]', 'Set the application-form text posted when a ticket of this category opens.'),
    ('Tickets', '🎫', 'claimticket', [], '', 'Claim the current ticket channel.'),
    ('Tickets', '🎫', 'closeticket', [], '', 'Close and delete the current ticket channel.'),
    ('Stats & Info', '📊', 'whois', [], '[member]', 'Show detailed profile info for a member.'),
    ('Stats & Info', '📊', 'chatstats', [], '[target]', 'View chat stats — a personal rank card by default.'),
    ('Stats & Info', '📊', 'serverstats', ['ss', 'sinfo'], '', 'Show a full interactive multi-page server stats report.'),
    ('Stats & Info', '📊', 'invites', [], '[member]', 'Show how many people a user has invited.'),
    ('Stats & Info', '📊', 'invitelogs', [], '[member]', 'Show invite join logs for a user.'),
    ('Stats & Info', '📊', 'inviteleaderboard', [], '', 'Show the top inviters in the server.'),
    ('Stats & Info', '📊', 'setinvite', [], '[invite_link]', "Set the server's permanent invite link used in DM notifications."),
    ('Stats & Info', '📊', 'milestones', [], '', 'Show all configured milestones and the current announcement channel.'),
    ('Stats & Info', '📊', 'setmilestone', [], '[channel]', 'Set the channel where milestone announcements are posted.'),
    ('Stats & Info', '📊', 'testmilestone', [], '', 'Send a preview milestone announcement in the configured channel.'),
    ('Stats & Info', '📊', 'ping', [], '', "Check the bot's latency."),
    ('Stats & Info', '📊', 'exitsurveys', ['exitreasons'], '[limit=10]', 'View recent exit survey responses — why members said they left.'),
    ('Vouch', '✅', 'vouch', [], '[member] [role] [reason=No reason provided]', 'Vouch for a member, optionally requesting a protected role for them.'),
    ('Vouch', '✅', 'unvouch', [], '<member> [reason=No reason provided]', 'Remove a vouch from a member, stripping any protected roles it earned.'),
    ('Vouch', '✅', 'cancelvouch', [], '[member] [role]', 'Cancel a pending vouch-role request.'),
    ('Vouch', '✅', 'pendingvouches', [], '', 'List all open vouch-role requests awaiting owner approval.'),
    ('Vouch', '✅', 'vouches', [], '[member]', 'Show vouch count and history for a member.'),
    ('Vouch', '✅', 'vouchleaderboard', [], '', 'Show the top vouched members in the server.'),
    ('Vouch', '✅', 'vouchstats', [], '', 'Server-wide vouch analytics.'),
    ('Vouch', '✅', 'vouchconfig', [], '[setting] [value]', 'Configure the vouch system.'),
    ('Giveaways & Polls', '🎉', 'giveaway', [], '<duration> <winners> <prize>', 'Start a giveaway.'),
    ('Giveaways & Polls', '🎉', 'giveawayend', [], '[message_id]', 'Force-end a giveaway early.'),
    ('Giveaways & Polls', '🎉', 'giveaways', [], '', 'List all active giveaways.'),
    ('Giveaways & Polls', '🎉', 'poll', [], '<rest>', 'Create a button-based poll with a live-updating results bar chart.'),
    ('Giveaways & Polls', '🎉', 'pollend', [], '[message_id]', 'Force-close a poll early.'),
    ('Economy & Games', '💰', 'balance', ['bal', 'wallet', 'money'], '[member]', "Check your (or someone's) wallet balance."),
    ('Economy & Games', '💰', 'daily', [], '', 'Claim your daily coin reward.'),
    ('Economy & Games', '💰', 'weekly', [], '', 'Claim your weekly coin reward.'),
    ('Economy & Games', '💰', 'work', [], '', 'Work a job for a small coin reward.'),
    ('Economy & Games', '💰', 'rob', [], '[member]', 'Attempt to steal coins from another member.'),
    ('Economy & Games', '💰', 'give', ['pay', 'transfer'], '[member] [amount]', 'Give coins to another member.'),
    ('Economy & Games', '💰', 'deposit', ['dep'], '[amount]', 'Move coins from your wallet into your bank.'),
    ('Economy & Games', '💰', 'withdraw', ['with'], '[amount]', 'Move coins from your bank back into your wallet.'),
    ('Economy & Games', '💰', 'leaderboard', ['lb', 'rich'], '', 'Show the richest members on the server.'),
    ('Economy & Games', '💰', 'gamblers', [], '', 'Show the biggest gamblers on the server.'),
    ('Economy & Games', '💰', 'slots', [], '[bet]', 'Spin the slot machine and bet your coins.'),
    ('Economy & Games', '💰', 'blackjack', ['bj'], '[bet]', 'Play a hand of blackjack against the house.'),
    ('Economy & Games', '💰', 'coinflip', ['cf', 'flip'], '[bet] [choice]', 'Flip a coin and bet on the result.'),
    ('Economy & Games', '💰', 'dice', [], '[bet] [guess]', 'Roll the dice and bet on the outcome.'),
    ('Economy & Games', '💰', '8ball', ['eightball'], '[question]', 'Ask the magic 8-ball a yes/no question.'),
    ('Economy & Games', '💰', 'trivia', [], '', 'Answer a trivia question for a coin reward.'),
    ('Economy & Games', '💰', 'hangman', [], '', 'Play a game of hangman.'),
    ('Economy & Games', '💰', 'tictactoe', ['ttt'], '[opponent]', 'Play tic-tac-toe against the bot or another member.'),
    ('Economy & Games', '💰', 'numguess', ['ng', 'guess'], '', 'Guess the secret number in as few tries as possible.'),
    ('Economy & Games', '💰', 'rockpaperscissors', ['rps'], '[choice]', 'Play rock-paper-scissors against the bot.'),
    ('Economy & Games', '💰', 'highlow', ['hl'], '[bet]', 'Guess whether the next number is higher or lower.'),
    ('Economy & Games', '💰', 'crash', [], '[bet]', 'Play the crash multiplier gambling game.'),
    ('Economy & Games', '💰', 'games', ['gamelist', 'gamemenu'], '', 'Show the interactive game center with all commands.'),
    ('Birthdays', '🎂', 'birthday', [], '[member]', "Show a member's saved birthday."),
    ('Birthdays', '🎂', 'removebirthday', [], '', 'Remove your saved birthday.'),
    ('Birthdays', '🎂', 'setbirthday', [], '[date]', 'Set your birthday.'),
    ('Birthdays', '🎂', 'setbirthdaychannel', [], '[channel]', 'Set the channel birthday announcements are posted to.'),
    ('Birthdays', '🎂', 'birthdaylist', ['birthdays'], '', 'List all upcoming birthdays in the server.'),
    ('Birthdays', '🎂', 'settimezone', ['mytimezone'], '[offset]', 'Set your timezone so birthday announcements fire at YOUR local midnight.'),
    ('Boosts & Vanity', '🚀', 'setboostchannel', [], '[channel]', 'Configure where the public boost thank-you message posts.'),
    ('Boosts & Vanity', '🚀', 'setvanitycode', [], '[code]', 'Manually override the vanity invite code to watch for in statuses.'),
    ('Boosts & Vanity', '🚀', 'setvanityrole', [], '[role]', "Set the role auto-granted for repping this server's vanity link."),
    ('Boosts & Vanity', '🚀', 'vanityconfig', [], '', "Show this server's current vanity role tracking configuration."),
    ('Staff Tools', '📋', 'staffpsa', [], '[psa_type=info] <message>', 'Post a richly styled staff PSA with an acknowledge button.'),
    ('Staff Tools', '📋', 'task', [], '[priority=medium] [assigned] <title_and_desc>', 'Create a staff task card with full interactive buttons.'),
    ('Staff Tools', '📋', 'tasklist', [], '[filter_status]', 'View the staff task board.'),
    ('Admin & Setup', '⚙️', 'setup', [], '', 'Run full first-time server setup — categories, channels, roles.'),
    ('Admin & Setup', '⚙️', 'backup', [], '[label]', 'Take a snapshot of the server structure.'),
    ('Admin & Setup', '⚙️', 'restore', [], '<label>', 'Restore the server from a backup. Adds missing channels/roles — does NOT delete existing ones.'),
    ('Admin & Setup', '⚙️', 'listbackups', [], '', 'List all available backups for this server.'),
    ('Admin & Setup', '⚙️', 'deletebackup', [], '<label>', 'Delete a saved backup.'),
    ('Admin & Setup', '⚙️', 'exportconfig', [], '', 'Dump a readable summary of everything configured for THIS server.'),
    ('Admin & Setup', '⚙️', 'setlogchannel', [], '[key] [channel]', 'Pin a specific channel for a log key.'),
    ('Admin & Setup', '⚙️', 'setwelcome', [], '[channel] [option]', 'Configure the auto-welcome system.'),
    ('Admin & Setup', '⚙️', 'disablewelcome', [], '', 'Disable the auto-welcome message for new members.'),
    ('Admin & Setup', '⚙️', 'sendwelcome', [], '<member>', 'Send the full welcome card for a specific member in the current channel.'),
    ('Admin & Setup', '⚙️', 'welcome', [], '[member]', 'Re-send or preview the welcome card.'),
    ('Admin & Setup', '⚙️', 'sendinvite', [], '[user_target] [message]', "Send the server invite + optional personal message to any user's DMs."),
    ('Admin & Setup', '⚙️', 'announce', ['ann'], '[channel] [text]', 'Send a polished announcement embed to any channel.'),
    ('Fun & Utility', '🎲', 'snipe', ['s'], '[index=1]', 'Show a recently deleted message, including any attached image.'),
    ('Fun & Utility', '🎲', 'clearsnipe', [], '', 'Clear the snipe/editsnipe cache for this channel.'),
    ('Fun & Utility', '🎲', 'editsnipe', ['es'], '[index=1]', "Show a recently edited message's before/after history."),
    ('Fun & Utility', '🎲', 'quote', [], '[target]', 'Quote a message as a beautiful embed card.'),
    ('Fun & Utility', '🎲', 'rules', [], '', 'Post the server rules.'),
    ('Fun & Utility', '🎲', 'cmds', [], '', 'Full command reference in one message (same as ,help).'),
    ('Fun & Utility', '🎲', 'help', [], '', 'Full command reference in one message — every command, grouped by category.'),
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
  <p class="trust-sub">A taste of what TrapAI can do.</p>
  <div class="cmd-grid">{commands_html}</div>
  <a class="see-all-link" href="/commands">See the full interactive command list →</a>
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


def _command_card_html(cat: str, icon: str, name: str, aliases: list, sig: str, desc: str) -> str:
    alias_text = " ".join(f",{a}" for a in aliases)
    haystack = html.escape(" ".join([name, alias_text, desc, cat]).lower())
    alias_html = f'<span class="cmd-page-alias">aka {html.escape(alias_text)}</span>' if aliases else ""
    usage = f",{name} {sig}".strip() if sig else f",{name}"
    return (
        f'<div class="cmd-page-card" data-cat="{html.escape(cat)}" data-search="{haystack}">'
        f'<div class="cmd-page-card-top">'
        f'<span class="cmd-page-name">,{html.escape(name)}</span>'
        f'<button class="copy-btn" data-cmd="{html.escape(f",{name}")}">Copy</button>'
        f'</div>'
        f'<p class="cmd-page-desc">{html.escape(desc)}</p>'
        f'<div class="cmd-page-meta"><span class="cmd-page-cat-tag">{icon} {html.escape(cat)}</span>{alias_html}</div>'
        f'<div class="cmd-page-sig">{html.escape(usage)}</div>'
        f'</div>'
    )


def _commands_page() -> str:
    total = len(COMMAND_CATALOG)
    cats_ordered = []
    seen = set()
    counts = {}
    for cat, icon, *_ in COMMAND_CATALOG:
        counts[cat] = counts.get(cat, 0) + 1
        if cat not in seen:
            seen.add(cat)
            cats_ordered.append((cat, icon))

    tabs_html = f'<button class="cat-tab active" data-cat="all">All ({total})</button>' + "".join(
        f'<button class="cat-tab" data-cat="{html.escape(cat)}">{icon} {html.escape(cat)} ({counts[cat]})</button>'
        for cat, icon in cats_ordered
    )

    cards_html = "".join(
        _command_card_html(cat, icon, name, aliases, sig, desc)
        for cat, icon, name, aliases, sig, desc in COMMAND_CATALOG
    )

    accent_style = f"<style>:root {{ --accent: {BRAND_GOLD}; --accent-soft: {BRAND_GOLD}22; --gold: {BRAND_GOLD}; }}</style>"

    nav_html = (
        '<nav class="nav">'
        '<div class="nav-brand"><span class="nav-logo">🛡️</span> TrapAI</div>'
        '<div class="nav-right"><span class="nav-badge">🌐 Command Reference</span></div>'
        '</nav>'
    )

    return f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TrapAI Commands</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
{accent_style}
{_STYLE_AND_SCRIPT}
{_COMMANDS_PAGE_STYLE_AND_SCRIPT}
</head>
<body class="ok">
{nav_html}
<div class="cmd-page-header">
  <h1>Every TrapAI Command</h1>
  <p>{total} commands across {len(cats_ordered)} categories — search, filter, or browse by category.</p>
  <div class="search-wrap">
    <span class="search-icon">🔍</span>
    <input id="cmd-search" class="search-input" type="text" placeholder="Search commands..." autocomplete="off">
    <div class="search-hint">Press <kbd>/</kbd> to search</div>
  </div>
</div>
<div class="cat-tabs">{tabs_html}</div>
<div class="result-count" id="result-count">Showing {total} of {total} commands</div>
<div class="cmd-page-grid">
  {cards_html}
  <div class="empty-state" id="empty-state">No commands match your search.</div>
</div>
<footer class="site-footer">Secured by Discord OAuth2 • TrapAI</footer>
</body></html>"""


async def handle_commands_page(request: web.Request) -> web.Response:
    return web.Response(text=_commands_page(), content_type="text/html", status=200)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def handle_landing_page(request: web.Request) -> web.Response:
    return web.Response(text=_landing_page(), content_type="text/html", status=200)


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


def build_bot_invite_url(guild_id) -> str:
    """A one-click 'add TrapAI to your server' link, pre-scoped to the
    server they just paid for via guild_id + disable_guild_select — the
    closest thing Discord's platform allows to an "automatic" join. There
    is no API for a bot to add itself to a guild; a human with Manage
    Server there always has to click Authorize."""
    return (
        "https://discord.com/api/oauth2/authorize"
        f"?client_id={CLIENT_ID}"
        f"&permissions={BOT_INVITE_PERMISSIONS}"
        "&scope=bot%20applications.commands"
        f"&guild_id={guild_id}"
        "&disable_guild_select=true"
    )


def _resolve_base_url(request: web.Request) -> str:
    return PRODUCT_BASE_URL or str(request.url.origin())


def _bootstrap_stripe_prices_sync():
    """Idempotent — skips any product/tier that already has a cached Price
    ID, so restarting this service never spawns duplicate Products/Prices
    in Stripe. Runs on startup; also safe to call again if one is missing
    (e.g. STRIPE_PRICE_IDS' backing file was deleted)."""
    if not STRIPE_SECRET_KEY:
        return
    changed = False
    for product, product_cfg in PRODUCTS.items():
        for tier, cfg in product_cfg["tiers"].items():
            key = _price_key(product, tier)
            if STRIPE_PRICE_IDS.get(key):
                continue
            try:
                stripe_product = stripe.Product.create(
                    name=f"TrapAI — {product_cfg['label']} — {cfg['label']}",
                    metadata={"trapai_product": product, "trapai_tier": tier},
                )
                price_kwargs = dict(
                    unit_amount=cfg["price_cents"],
                    currency="usd",
                    product=stripe_product.id,
                    metadata={"trapai_product": product, "trapai_tier": tier},
                )
                if cfg["interval"]:
                    price_kwargs["recurring"] = {"interval": cfg["interval"]}
                price = stripe.Price.create(**price_kwargs)
                STRIPE_PRICE_IDS[key] = price.id
                changed = True
                log.info("Created Stripe product/price for %s: %s", key, price.id)
            except stripe.error.StripeError as e:
                log.error("Failed to create Stripe product/price for %s: %s", key, e)
    if changed:
        _save_json("stripe_price_ids", STRIPE_PRICE_IDS)


def _create_checkout_session_sync(product: str, tier: str, guild_id: str, discord_user_id: str, base_url: str):
    cfg = _tier_cfg(product, tier)
    key = _price_key(product, tier)
    price_id = STRIPE_PRICE_IDS.get(key)
    if not cfg or not price_id:
        raise RuntimeError(f"No Stripe price configured for {key!r} yet")
    metadata = {
        "guild_id": str(guild_id), "discord_user_id": str(discord_user_id),
        "product": product, "tier": tier,
    }
    common = dict(
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{base_url}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base_url}/checkout/cancel",
        metadata=metadata,
        client_reference_id=str(guild_id),
    )
    if not cfg["interval"]:  # one-time: ticket_bot's lifetime, or any whole_bot tier
        return stripe.checkout.Session.create(mode="payment", **common)
    return stripe.checkout.Session.create(
        mode="subscription",
        subscription_data={"metadata": metadata},
        **common,
    )


def _product_tier_for_price_id(price_id: str):
    for key, pid in STRIPE_PRICE_IDS.items():
        if pid == price_id:
            product, _, tier = key.partition(":")
            return product, tier
    return None, None


def _find_by_subscription_id(sub_id: str):
    for gid, rec in SUBSCRIPTIONS.items():
        if rec.get("stripe_subscription_id") == sub_id:
            return rec, gid
    return None, None


def _to_plain_dict(obj):
    """Newer stripe-python versions return StripeObject instances that
    deliberately raise on .get() (attribute/subscript access only, by
    design — see stripe's own AttributeError message pointing at
    .to_dict()). Every webhook event's data object and every retrieved
    Session/Subscription goes through here once, so the rest of this file
    can keep using plain-dict .get() everywhere without re-litigating this
    per call site."""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return obj


async def _send_dm(session: ClientSession, user_id: str, content: str):
    """Same raw-REST DM pattern as the verification log posting above — a
    bot token can open/send a DM by user ID with no gateway connection."""
    headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}
    async with session.post(f"{DISCORD_API}/users/@me/channels", headers=headers, json={"recipient_id": user_id}) as resp:
        if resp.status != 200:
            log.warning("Opening DM channel with %s failed (%s): %s", user_id, resp.status, await resp.text())
            return
        dm = await resp.json()
    async with session.post(f"{DISCORD_API}/channels/{dm['id']}/messages", headers=headers, json={"content": content[:2000]}) as resp2:
        if resp2.status not in (200, 201):
            log.warning("Sending DM to %s failed (%s): %s", user_id, resp2.status, await resp2.text())


# ── Webhook event handlers ──────────────────────────────────
# Per Stripe's own guidance, checkout.session.completed and invoice.paid
# are what actually provision/maintain access — never the client-side
# success page, which only ever shows a friendly confirmation.

async def _handle_checkout_completed(obj: dict, session: ClientSession):
    metadata = obj.get("metadata") or {}
    guild_id = metadata.get("guild_id")
    discord_user_id = metadata.get("discord_user_id")
    product = metadata.get("product")
    tier = metadata.get("tier")
    cfg = _tier_cfg(product, tier)
    if not guild_id or not cfg:
        log.warning("checkout.session.completed missing/invalid metadata on session %s", obj.get("id"))
        return

    rec = SUBSCRIPTIONS.setdefault(guild_id, {})
    rec.setdefault("created_at", time.time())
    rec.update({
        "product": product,
        "tier": tier,
        "owner_discord_user_id": discord_user_id,
        "stripe_customer_id": obj.get("customer"),
        "updated_at": time.time(),
    })
    if obj.get("mode") == "payment":  # one-time — ticket_bot's lifetime, or any whole_bot tier
        rec["status"] = "lifetime"
        rec["stripe_subscription_id"] = None
        rec["grace_period_ends_at"] = None
    else:
        rec["status"] = "active"
        rec["stripe_subscription_id"] = obj.get("subscription")
        rec["grace_period_ends_at"] = None
    _save_json("subscriptions", SUBSCRIPTIONS)

    if discord_user_id:
        product_label = PRODUCTS[product]["label"]
        kind = "one-time purchase" if obj.get("mode") == "payment" else "subscription"
        access_note = (
            "Full bot access unlocks as soon as the bot joins — every command, no ticket-only restriction."
            if product == "whole_bot" else
            "The ticket system unlocks as soon as the bot joins."
        )
        invite_url = build_bot_invite_url(guild_id)
        await _send_dm(session, discord_user_id, (
            f"✅ **Payment received — thank you!**\n\n"
            f"Your **{product_label} — {cfg['label']}** {kind} is now active for server `{guild_id}`.\n\n"
            f"**Add TrapAI to your server:** {invite_url}\n\n"
            f"{access_note}"
        ))


async def _handle_invoice_paid(obj: dict, session: ClientSession):
    sub_id = obj.get("subscription")
    if not sub_id:
        return
    rec, gid = _find_by_subscription_id(sub_id)
    if not rec:
        return
    was_past_due = rec.get("status") == "past_due"
    rec["status"] = "active"
    rec["grace_period_ends_at"] = None
    lines = obj.get("lines", {}).get("data") or []
    period_end = lines[0].get("period", {}).get("end") if lines else None
    if period_end:
        rec["current_period_end"] = period_end
    rec["updated_at"] = time.time()
    _save_json("subscriptions", SUBSCRIPTIONS)

    if was_past_due and rec.get("owner_discord_user_id"):
        await _send_dm(session, rec["owner_discord_user_id"], (
            "✅ **Payment received — your TrapAI subscription is active again!**\n"
            "The ticket system has been restored for your server."
        ))


async def _handle_invoice_payment_failed(obj: dict, session: ClientSession):
    sub_id = obj.get("subscription")
    if not sub_id:
        return
    rec, gid = _find_by_subscription_id(sub_id)
    if not rec:
        return
    if rec.get("status") == "past_due":
        return  # already in grace period — don't reset the clock on a second failed retry
    rec["status"] = "past_due"
    rec["grace_period_ends_at"] = time.time() + SUBSCRIPTION_GRACE_PERIOD_DAYS * 86400
    rec["last_reminder_sent_at"] = time.time()
    rec["updated_at"] = time.time()
    _save_json("subscriptions", SUBSCRIPTIONS)

    if rec.get("owner_discord_user_id"):
        cfg = _tier_cfg(rec.get("product"), rec.get("tier"))
        label = cfg["label"] if cfg else "TrapAI"
        portal_hint = f"{PRODUCT_BASE_URL}/portal?guild_id={gid}" if PRODUCT_BASE_URL else "the Manage Subscription link from ,managesubscription"
        await _send_dm(session, rec["owner_discord_user_id"], (
            f"⚠️ **TrapAI payment failed.**\n\n"
            f"Your card was declined for your **{label}** subscription (server `{gid}`).\n"
            f"You have **{SUBSCRIPTION_GRACE_PERIOD_DAYS} days** to update your payment method before "
            "the ticket system is disabled for that server.\n\n"
            f"Update billing: {portal_hint}"
        ))


async def _handle_subscription_updated(obj: dict, session: ClientSession):
    sub_id = obj.get("id")
    rec, gid = _find_by_subscription_id(sub_id)
    if not rec:
        return
    items = (obj.get("items") or {}).get("data") or []
    if items:
        price_id = (items[0].get("price") or {}).get("id")
        product, tier = _product_tier_for_price_id(price_id)
        if product and tier:
            rec["product"] = product
            rec["tier"] = tier
    # Best-effort fallback sync from Stripe's own subscription status —
    # invoice.paid / invoice.payment_failed are the primary source of truth
    # for active/past_due, this just catches drift.
    if obj.get("status") == "active" and rec.get("status") == "past_due":
        rec["status"] = "active"
        rec["grace_period_ends_at"] = None
    rec["updated_at"] = time.time()
    _save_json("subscriptions", SUBSCRIPTIONS)


async def _handle_subscription_deleted(obj: dict, session: ClientSession):
    sub_id = obj.get("id")
    rec, gid = _find_by_subscription_id(sub_id)
    if not rec:
        return
    rec["status"] = "canceled"
    rec["grace_period_ends_at"] = None
    rec["updated_at"] = time.time()
    _save_json("subscriptions", SUBSCRIPTIONS)

    if rec.get("owner_discord_user_id"):
        await _send_dm(session, rec["owner_discord_user_id"], (
            "❌ **TrapAI subscription canceled.**\n\n"
            "This server's TrapAI subscription has ended, and the ticket system is now disabled for it.\n"
            "Resubscribe anytime to restore access."
        ))


async def _run_grace_period_sweep_once():
    """One pass over SUBSCRIPTIONS: suspends any server whose grace period
    has run out, and sends one reminder DM per day while still inside the
    grace period. Split out from the loop below so it's independently
    callable/testable without waiting on the real sleep interval."""
    if not SUBSCRIPTIONS:
        return
    now = time.time()
    changed = False
    timeout = ClientTimeout(total=15)
    async with ClientSession(timeout=timeout) as session:
        for gid, rec in list(SUBSCRIPTIONS.items()):
            if rec.get("status") != "past_due":
                continue
            grace_end = rec.get("grace_period_ends_at")
            if not grace_end:
                continue
            owner = rec.get("owner_discord_user_id")
            if now >= grace_end:
                rec["status"] = "suspended"
                rec["updated_at"] = now
                changed = True
                if owner:
                    await _send_dm(session, owner, (
                        "🚫 **TrapAI subscription suspended.**\n\n"
                        f"The grace period for server `{gid}` has ended without a successful payment — "
                        "the ticket system is now disabled for it.\n"
                        "Update your payment method and the next successful charge will restore access automatically."
                    ))
            elif now - rec.get("last_reminder_sent_at", 0) >= 86400:
                rec["last_reminder_sent_at"] = now
                changed = True
                if owner:
                    days_left = max(1, round((grace_end - now) / 86400))
                    await _send_dm(session, owner, (
                        f"⏳ **Reminder: TrapAI payment still failing** for server `{gid}`.\n"
                        f"**{days_left} day(s)** left in your grace period before the ticket system is suspended.\n"
                        "Please update your payment method to avoid interruption."
                    ))
    if changed:
        _save_json("subscriptions", SUBSCRIPTIONS)


async def _grace_period_sweep_loop():
    """Runs for the lifetime of the process."""
    while True:
        await asyncio.sleep(6 * 3600)
        await _run_grace_period_sweep_once()


def _check_internal_auth(request: web.Request) -> bool:
    return bool(INTERNAL_API_SECRET) and request.headers.get("X-Internal-Secret") == INTERNAL_API_SECRET


# ============================================================
# BILLING PAGES — pricing/checkout, success/cancel, terms
# ============================================================
_BILLING_PAGE_STYLE = """
<style>
  .billing-header { padding: clamp(2.5rem, 6vw, 4rem) 1.5rem 1rem; text-align: center; }
  .billing-header h1 { font-size: clamp(1.8rem, 5vw, 2.6rem); font-weight: 800; margin: 0 0 .5rem; color: #f2f3f5; }
  .billing-header p { color: #8a8d94; margin: 0; }

  .product-tabs { display: flex; justify-content: center; gap: .6rem; margin: 0 0 .8rem; }
  .product-tab {
    font-family: inherit; font-size: .9rem; font-weight: 700; color: #c7cad0;
    background: rgba(255, 255, 255, .04); border: 1px solid rgba(255, 255, 255, .1);
    border-radius: 20px; padding: .6rem 1.3rem; cursor: pointer; transition: all .15s ease;
  }
  .product-tab:hover { border-color: rgba(255, 198, 41, .4); color: #fff; }
  .product-tab.active { background: var(--gold); color: #0b0b0d; border-color: var(--gold); }
  .product-desc { text-align: center; color: #8a8d94; font-size: .88rem; max-width: 520px; margin: 0 auto 1.5rem; }

  .billing-form { max-width: 900px; margin: 2rem auto 4rem; padding: 0 1.5rem; }
  .tier-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 2rem; }
  .tier-card {
    position: relative; background: rgba(255, 255, 255, .03); border: 2px solid rgba(255, 255, 255, .08);
    border-radius: 14px; padding: 1.4rem 1.2rem; cursor: pointer; transition: all .15s ease;
  }
  .tier-card:hover { border-color: rgba(255, 198, 41, .35); transform: translateY(-2px); }
  .tier-card input { position: absolute; opacity: 0; }
  .tier-card.best { border-color: var(--gold); }
  .tier-card input:checked + .tier-body { color: #fff; }
  .tier-card:has(input:checked) { border-color: var(--gold); background: rgba(255, 198, 41, .06); }
  .tier-best-badge {
    position: absolute; top: -11px; left: 50%; transform: translateX(-50%);
    background: var(--gold); color: #0b0b0d; font-size: .68rem; font-weight: 800;
    letter-spacing: .04em; padding: .25rem .7rem; border-radius: 20px; white-space: nowrap;
  }
  .tier-name { font-weight: 800; font-size: 1.05rem; color: #f2f3f5; margin-bottom: .3rem; }
  .tier-price { font-size: 1.5rem; font-weight: 800; color: var(--gold); }
  .tier-price small { font-size: .7rem; font-weight: 600; color: #8a8d94; }

  .field-group { margin-bottom: 1.2rem; }
  .field-group label { display: block; font-size: .85rem; font-weight: 600; color: #c7cad0; margin-bottom: .4rem; }
  .field-group input[type=text] {
    width: 100%; padding: .8rem 1rem; border-radius: 10px; box-sizing: border-box;
    border: 1px solid rgba(255, 255, 255, .1); background: rgba(255, 255, 255, .04);
    color: #fff; font-size: .95rem; font-family: inherit;
  }
  .field-hint { font-size: .75rem; color: #5c5f66; margin-top: .35rem; }

  .agree-row { display: flex; align-items: flex-start; gap: .6rem; margin: 1.4rem 0; font-size: .85rem; color: #a7abb3; }
  .agree-row a { color: var(--gold); }

  .billing-submit {
    display: block; width: 100%; text-align: center; background: var(--gold); color: #0b0b0d;
    font-weight: 800; font-size: 1rem; padding: 1rem; border: none; border-radius: 10px; cursor: pointer;
    font-family: inherit; transition: transform .15s ease;
  }
  .billing-submit:hover { transform: translateY(-2px); }

  .billing-note { max-width: 900px; margin: 0 auto 3rem; padding: 0 1.5rem; text-align: center; color: #5c5f66; font-size: .82rem; }

  .result-page { max-width: 560px; margin: 4rem auto; padding: 0 1.5rem; text-align: center; }
  .result-page h1 { font-size: clamp(1.6rem, 4vw, 2.2rem); font-weight: 800; margin: 0 0 1rem; color: #f2f3f5; }
  .result-page p { color: #a7abb3; line-height: 1.6; margin: 0 0 1.6rem; }

  .terms-page { max-width: 720px; margin: 0 auto; padding: 2rem 1.5rem 4rem; color: #c7cad0; line-height: 1.7; }
  .terms-page h1 { color: #f2f3f5; font-size: clamp(1.6rem, 4vw, 2.2rem); margin-bottom: 1.5rem; }
  .terms-page h2 { color: #f2f3f5; font-size: 1.1rem; margin-top: 2rem; }
  .terms-page p, .terms-page li { color: #a7abb3; font-size: .92rem; }
</style>
"""


def _billing_page_shell(title: str, body_html: str) -> str:
    accent_style = f"<style>:root {{ --accent: {BRAND_GOLD}; --accent-soft: {BRAND_GOLD}22; --gold: {BRAND_GOLD}; }}</style>"
    nav_html = (
        '<nav class="nav">'
        '<div class="nav-brand"><span class="nav-logo">🛡️</span> TrapAI</div>'
        '<div class="nav-right"><a class="nav-link" href="/checkout">Pricing</a><a class="nav-link" href="/terms">Terms</a></div>'
        '</nav>'
    )
    return f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
{accent_style}
{_STYLE_AND_SCRIPT}
{_BILLING_PAGE_STYLE}
</head>
<body class="ok">
{nav_html}
{body_html}
<footer class="site-footer">Secured by Stripe • TrapAI</footer>
</body></html>"""


def _tier_cards_html(product: str, tier_order: list, selected: bool) -> str:
    cards = []
    for i, tier in enumerate(tier_order):
        cfg = PRODUCTS[product]["tiers"][tier]
        price_str = f"${cfg['price_cents'] / 100:.0f}"
        period_str = "one-time" if not cfg["interval"] else f"/{cfg['interval']}"
        best_badge = '<div class="tier-best-badge">⭐ BEST VALUE</div>' if cfg["best_value"] else ""
        cards.append(
            f'<label class="tier-card{" best" if cfg["best_value"] else ""}">'
            f'{best_badge}'
            f'<input type="radio" name="tier_{product}" value="{tier}"{" checked" if selected and i == 0 else ""}'
            f'{"" if selected else " disabled"}>'
            f'<div class="tier-body">'
            f'<div class="tier-name">{html.escape(cfg["label"])}</div>'
            f'<div class="tier-price">{price_str} <small>{period_str}</small></div>'
            f'</div></label>'
        )
    return "".join(cards)


def _pricing_page(error: str = None) -> str:
    error_html = f'<p style="color:#ED4245;text-align:center;margin-bottom:1.5rem;">{html.escape(error)}</p>' if error else ""

    ticket_bot_cards = _tier_cards_html("ticket_bot", ["starter", "pro", "premium", "lifetime"], selected=True)
    whole_bot_cards = _tier_cards_html("whole_bot", ["regular", "premium"], selected=False)

    body = f"""
<div class="billing-header">
  <h1>Get TrapAI</h1>
  <p>Choose what you want, pick a plan, and get instant access as soon as you pay.</p>
</div>
<div class="product-tabs">
  <button type="button" class="product-tab active" data-product="ticket_bot">🎫 Ticket Bot</button>
  <button type="button" class="product-tab" data-product="whole_bot">🤖 Whole Bot</button>
</div>
<p class="product-desc" id="product-desc">{html.escape(PRODUCTS['ticket_bot']['description'])}</p>
<form class="billing-form" method="post" action="/checkout/session">
  {error_html}
  <input type="hidden" name="product" id="product-field" value="ticket_bot">
  <div class="tier-grid product-panel" data-product="ticket_bot">{ticket_bot_cards}</div>
  <div class="tier-grid product-panel" data-product="whole_bot" style="display:none;">{whole_bot_cards}</div>
  <div class="field-group">
    <label for="guild_id">Your Discord Server ID</label>
    <input type="text" id="guild_id" name="guild_id" inputmode="numeric" pattern="[0-9]+" required
           placeholder="e.g. 123456789012345678">
    <div class="field-hint">Enable Developer Mode in Discord, right-click your server icon → Copy Server ID.</div>
  </div>
  <div class="field-group">
    <label for="discord_user_id">Your Discord User ID</label>
    <input type="text" id="discord_user_id" name="discord_user_id" inputmode="numeric" pattern="[0-9]+" required
           placeholder="e.g. 987654321098765432">
    <div class="field-hint">Right-click your own name in Discord → Copy User ID. We'll DM your invite link and billing updates here.</div>
  </div>
  <div class="agree-row">
    <input type="checkbox" name="agree_tos" required style="margin-top:.2rem;">
    <span>I agree to the <a href="/terms" target="_blank">Terms of Service</a>, including that this is a license to use TrapAI, not a sale of its source code, and that one-time purchases are non-refundable.</span>
  </div>
  <button type="submit" class="billing-submit">Continue to Secure Checkout →</button>
</form>
<p class="billing-note">Payments are processed securely by Stripe — we never see or store your card details. Subscriptions renew automatically until canceled; one-time plans are a single charge with no recurring billing.</p>
<script>
document.addEventListener('DOMContentLoaded', function () {{
  var tabs = document.querySelectorAll('.product-tab');
  var panels = document.querySelectorAll('.product-panel');
  var field = document.getElementById('product-field');
  var desc = document.getElementById('product-desc');
  var descriptions = {{
    ticket_bot: {json.dumps(PRODUCTS['ticket_bot']['description'])},
    whole_bot: {json.dumps(PRODUCTS['whole_bot']['description'])}
  }};
  tabs.forEach(function (tab) {{
    tab.addEventListener('click', function () {{
      var product = tab.dataset.product;
      tabs.forEach(function (t) {{ t.classList.remove('active'); }});
      tab.classList.add('active');
      field.value = product;
      if (desc && descriptions[product]) desc.textContent = descriptions[product];
      panels.forEach(function (panel) {{
        var isActive = panel.dataset.product === product;
        panel.style.display = isActive ? '' : 'none';
        panel.querySelectorAll('input[type=radio]').forEach(function (r) {{ r.disabled = !isActive; }});
      }});
    }});
  }});
}});
</script>
"""
    return _billing_page_shell("TrapAI Pricing", body)


def _landing_page() -> str:
    """The site's front door — a real, shareable "get started" link that
    works with zero Discord/bot context: no server ID, no bot invite, no
    guild.get_member() lookup. Just pricing, checkout, and where the bot
    itself lives once someone's ready to add it. This is what should be
    linked from anywhere OUTSIDE Discord (a website, socials, another
    server's #resources channel) instead of telling people to run
    ,subscribe, which only works once the bot is already present."""
    body = """
<div class="result-page">
  <h1>🛡️ TrapAI</h1>
  <p>Server protection, tickets, moderation, and more — done right.</p>
  <p>Get started without adding the bot first: pick a plan, pay securely, then invite TrapAI straight to your server.</p>
  <a class="btn" href="/checkout" style="opacity:1;animation:none;">Get Started — See Pricing →</a>
  <p style="margin-top:2rem;"><a href="/commands">Browse every command</a> &nbsp;•&nbsp; <a href="/terms">Terms of Service</a></p>
</div>
"""
    return _billing_page_shell("TrapAI — Server Protection, Done Right", body)


def _billing_not_configured_page() -> str:
    body = """
<div class="result-page">
  <h1>Billing Isn't Live Yet</h1>
  <p>This TrapAI deployment hasn't had its Stripe keys configured yet. Check back soon, or contact whoever runs this bot.</p>
</div>
"""
    return _billing_page_shell("Billing Unavailable", body)


def _checkout_result_page(title: str, message: str, *, invite_url: str = None) -> str:
    button_html = (
        f'<a class="btn" href="{invite_url}" style="opacity:1;animation:none;">Add TrapAI to Your Server →</a>'
        if invite_url else ""
    )
    body = f"""
<div class="result-page">
  <h1>{html.escape(title)}</h1>
  <p>{message}</p>
  {button_html}
</div>
"""
    return _billing_page_shell(title, body)


def _terms_page() -> str:
    ticket_lifetime_price = PRODUCTS["ticket_bot"]["tiers"]["lifetime"]["price_cents"] / 100
    whole_regular_price = PRODUCTS["whole_bot"]["tiers"]["regular"]["price_cents"] / 100
    whole_premium_price = PRODUCTS["whole_bot"]["tiers"]["premium"]["price_cents"] / 100
    body = f"""
<div class="terms-page">
  <h1>TrapAI — Terms of Service &amp; License</h1>
  <p>This page explains what you're agreeing to when you subscribe to or purchase TrapAI — the Ticket Bot (support/ticket system only) or the Whole Bot (everything) — including the one-time plans.</p>

  <h2>1. What you're buying</h2>
  <p>A subscription or one-time purchase grants your Discord server a <strong>license to use</strong> the TrapAI product and tier you selected. It does not transfer ownership of, or any rights to, TrapAI's source code, which remains completely private. You may not decompile, extract, resell, or redistribute the bot itself.</p>

  <h2>2. One-time plans</h2>
  <p>Ticket Bot's Lifetime plan (${ticket_lifetime_price:.0f}) and both Whole Bot plans — Regular (${whole_regular_price:.0f}) and Premium (${whole_premium_price:.0f}) — are single one-time charges. No subscription is created and you will never be charged again for them. In exchange, you get access to that product for as long as TrapAI is offered and maintained (see Section 4). The Whole Bot plans include every feature of the full bot, not just the ticket system.</p>

  <h2>3. No refunds</h2>
  <p>All payments — subscription charges and one-time purchases alike — are final and non-refundable, except where required by law.</p>

  <h2>4. Service availability &amp; discontinuation</h2>
  <p>TrapAI is provided on a best-effort basis. The service may be modified, interrupted, or discontinued at any time, for any reason, without notice. If TrapAI is discontinued, including for one-time purchasers, no refunds or other compensation will be provided.</p>

  <h2>5. No warranty</h2>
  <p>TrapAI is provided "as is," without warranty of any kind. We do not guarantee the bot will be free of bugs, errors, downtime, or unexpected behavior. You assume all risk from using it.</p>

  <h2>6. Subscription billing (Ticket Bot: Starter / Pro / Premium)</h2>
  <p>These plans bill automatically on a recurring monthly basis until canceled. If a payment fails, you'll get a grace period of {SUBSCRIPTION_GRACE_PERIOD_DAYS} days to update your payment method before the ticket system is suspended for your server. You can manage or cancel your subscription anytime via the Manage Subscription / customer portal link. The Whole Bot plans are one-time purchases only and never bill on a recurring basis.</p>

  <h2>7. Support</h2>
  <p>If you run into any errors or issues, contact TrapAI support: {html.escape(SUPPORT_CONTACT)}.</p>
</div>
"""
    return _billing_page_shell("TrapAI Terms of Service", body)


async def handle_checkout_page(request: web.Request) -> web.Response:
    if not STRIPE_SECRET_KEY:
        return web.Response(text=_billing_not_configured_page(), content_type="text/html", status=503)
    return web.Response(text=_pricing_page(), content_type="text/html", status=200)


async def handle_create_checkout_session(request: web.Request) -> web.Response:
    if not STRIPE_SECRET_KEY:
        return web.Response(text=_billing_not_configured_page(), content_type="text/html", status=503)

    form = await request.post()
    guild_id = str(form.get("guild_id", "")).strip()
    discord_user_id = str(form.get("discord_user_id", "")).strip()
    product = str(form.get("product", "")).strip()
    tier = str(form.get(f"tier_{product}", "")).strip()
    agreed = form.get("agree_tos") is not None

    if not guild_id.isdigit() or not discord_user_id.isdigit() or not _tier_cfg(product, tier) or not agreed:
        return web.Response(
            text=_pricing_page(error="Please fill in a valid Server ID, User ID, pick a plan, and agree to the Terms of Service."),
            content_type="text/html", status=400,
        )

    base_url = _resolve_base_url(request)
    try:
        session_obj = await asyncio.to_thread(_create_checkout_session_sync, product, tier, guild_id, discord_user_id, base_url)
    except Exception as e:
        log.error("Failed to create checkout session: %s", e)
        return web.Response(text=_pricing_page(error="Couldn't start checkout — please try again in a moment."),
                             content_type="text/html", status=502)

    raise web.HTTPSeeOther(location=session_obj.url)


async def handle_checkout_success(request: web.Request) -> web.Response:
    if not STRIPE_SECRET_KEY:
        return web.Response(text=_billing_not_configured_page(), content_type="text/html", status=503)

    session_id = request.query.get("session_id")
    guild_id = None
    if session_id:
        try:
            session_obj = await asyncio.to_thread(stripe.checkout.Session.retrieve, session_id)
            session_data = _to_plain_dict(session_obj)
            guild_id = (session_data.get("metadata") or {}).get("guild_id")
        except stripe.error.StripeError as e:
            log.warning("Couldn't retrieve checkout session %s: %s", session_id, e)

    invite_url = build_bot_invite_url(guild_id) if guild_id else None
    message = (
        "Payment confirmed — thanks for subscribing to TrapAI! Click below to add the bot to your server. "
        "We've also DMed you this link on Discord."
        if invite_url else
        "Payment confirmed — thanks! Access is being set up now; check your Discord DMs for your server invite link."
    )
    return web.Response(text=_checkout_result_page("✅ Payment Successful", message, invite_url=invite_url),
                         content_type="text/html", status=200)


async def handle_checkout_cancel(request: web.Request) -> web.Response:
    return web.Response(
        text=_checkout_result_page("Checkout Canceled", "No charge was made. You can restart checkout anytime from the pricing page."),
        content_type="text/html", status=200,
    )


async def handle_portal_redirect(request: web.Request) -> web.Response:
    if not STRIPE_SECRET_KEY:
        return web.Response(text=_billing_not_configured_page(), content_type="text/html", status=503)

    guild_id = request.query.get("guild_id", "")
    rec = SUBSCRIPTIONS.get(guild_id)
    customer_id = rec.get("stripe_customer_id") if rec else None
    if not customer_id:
        return web.Response(
            text=_checkout_result_page("No Billing Account Found", "This server doesn't have a billing account on file yet — subscribe first from the pricing page."),
            content_type="text/html", status=404,
        )

    base_url = _resolve_base_url(request)
    try:
        portal = await asyncio.to_thread(
            stripe.billing_portal.Session.create, customer=customer_id, return_url=f"{base_url}/checkout/success",
        )
    except stripe.error.StripeError as e:
        log.error("Failed to create portal session for guild %s: %s", guild_id, e)
        return web.Response(
            text=_checkout_result_page("Couldn't Open Billing Portal", "Something went wrong opening your billing portal — please try again shortly."),
            content_type="text/html", status=502,
        )
    raise web.HTTPSeeOther(location=portal.url)


async def handle_terms_page(request: web.Request) -> web.Response:
    return web.Response(text=_terms_page(), content_type="text/html", status=200)


async def handle_stripe_webhook(request: web.Request) -> web.Response:
    if not STRIPE_WEBHOOK_SECRET:
        return web.json_response({"error": "billing not configured"}, status=503)

    payload = await request.read()
    sig_header = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        log.warning("Stripe webhook signature verification failed: %s", e)
        return web.json_response({"error": "invalid signature"}, status=400)

    event_type = event["type"]
    data_obj = _to_plain_dict(event["data"]["object"])

    timeout = ClientTimeout(total=15)
    async with ClientSession(timeout=timeout) as session:
        handler = {
            "checkout.session.completed": _handle_checkout_completed,
            "invoice.paid": _handle_invoice_paid,
            "invoice.payment_failed": _handle_invoice_payment_failed,
            "customer.subscription.updated": _handle_subscription_updated,
            "customer.subscription.deleted": _handle_subscription_deleted,
        }.get(event_type)
        if handler:
            await handler(data_obj, session)
        else:
            log.info("Ignoring unhandled Stripe event type: %s", event_type)

    return web.json_response({"received": True})


async def handle_internal_get_subscription(request: web.Request) -> web.Response:
    if not _check_internal_auth(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    guild_id = request.match_info["guild_id"]
    rec = SUBSCRIPTIONS.get(guild_id)
    if not rec:
        return web.json_response({"status": "none"})
    return web.json_response({
        "product": rec.get("product"),
        "tier": rec.get("tier"),
        "status": rec.get("status"),
        "current_period_end": rec.get("current_period_end"),
        "grace_period_ends_at": rec.get("grace_period_ends_at"),
    })


async def handle_internal_checkout_link(request: web.Request) -> web.Response:
    if not _check_internal_auth(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    if not STRIPE_SECRET_KEY:
        return web.json_response({"error": "billing not configured"}, status=503)

    body = await request.json()
    guild_id = str(body.get("guild_id", ""))
    discord_user_id = str(body.get("discord_user_id", ""))
    product = body.get("product")
    tier = body.get("tier")
    if not guild_id.isdigit() or not discord_user_id.isdigit() or not _tier_cfg(product, tier):
        return web.json_response({"error": "guild_id, discord_user_id, and a valid product/tier are required"}, status=400)

    base_url = _resolve_base_url(request)
    try:
        session_obj = await asyncio.to_thread(_create_checkout_session_sync, product, tier, guild_id, discord_user_id, base_url)
    except Exception as e:
        log.error("Failed to create checkout session via internal API: %s", e)
        return web.json_response({"error": "failed to create checkout session"}, status=502)
    return web.json_response({"url": session_obj.url})


async def handle_internal_portal_link(request: web.Request) -> web.Response:
    if not _check_internal_auth(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    if not STRIPE_SECRET_KEY:
        return web.json_response({"error": "billing not configured"}, status=503)

    body = await request.json()
    guild_id = str(body.get("guild_id", ""))
    rec = SUBSCRIPTIONS.get(guild_id)
    customer_id = rec.get("stripe_customer_id") if rec else None
    if not customer_id:
        return web.json_response({"error": "no billing account on file for this server"}, status=404)

    base_url = _resolve_base_url(request)
    try:
        portal = await asyncio.to_thread(
            stripe.billing_portal.Session.create, customer=customer_id, return_url=f"{base_url}/checkout/success",
        )
    except stripe.error.StripeError as e:
        log.error("Failed to create portal session via internal API: %s", e)
        return web.json_response({"error": "failed to create billing portal session"}, status=502)
    return web.json_response({"url": portal.url})


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/callback", handle_callback)
    app.router.add_get("/commands", handle_commands_page)
    app.router.add_get("/checkout", handle_checkout_page)
    app.router.add_post("/checkout/session", handle_create_checkout_session)
    app.router.add_get("/checkout/success", handle_checkout_success)
    app.router.add_get("/checkout/cancel", handle_checkout_cancel)
    app.router.add_get("/portal", handle_portal_redirect)
    app.router.add_get("/terms", handle_terms_page)
    app.router.add_post("/stripe/webhook", handle_stripe_webhook)
    app.router.add_get("/internal/subscription/{guild_id}", handle_internal_get_subscription)
    app.router.add_post("/internal/checkout-link", handle_internal_checkout_link)
    app.router.add_post("/internal/portal-link", handle_internal_portal_link)
    app.router.add_get("/", handle_landing_page)
    app.router.add_get("/health", handle_health)
    app.on_startup.append(_on_startup)
    return app


async def _on_startup(app: web.Application):
    if STRIPE_SECRET_KEY:
        await asyncio.to_thread(_bootstrap_stripe_prices_sync)
    asyncio.create_task(_grace_period_sweep_loop())


if __name__ == "__main__":
    log.info("Starting verify OAuth callback server on port %s", PORT)
    log.info("Redirect URI configured as: %s", REDIRECT_URI)
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
