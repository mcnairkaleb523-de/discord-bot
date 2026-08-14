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
  VERIFIED_ROLE_NAME       — defaults to "✅ Hood Member" (must match bot.py's VERIFIED_ROLE)
  UNVERIFIED_ROLE_NAME     — defaults to "🚫 Unverified" (must match bot.py's UNVERIFIED_ROLE)
"""

import os
import logging
from urllib.parse import quote

from aiohttp import web, ClientSession, ClientTimeout
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("verify-oauth")

CLIENT_ID     = os.getenv("1468003600620982458")
CLIENT_SECRET = os.getenv("Client_Secret")
BOT_TOKEN     = os.getenv("Bot Token")
REDIRECT_URI  = os.getenv("https://discord.com/channels/1523188439980179557/1523274401741803602")
PORT          = int(os.getenv("PORT", "8080"))

VERIFIED_ROLE_NAME   = os.getenv("VERIFIED_ROLE_NAME", "✅ Hood Member")
UNVERIFIED_ROLE_NAME = os.getenv("UNVERIFIED_ROLE_NAME", "🚫 Unverified")

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


def _page(title: str, message: str, ok: bool = True) -> str:
    color = "#2ecc71" if ok else "#e74c3c"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
  body {{ background:#23272a; color:#fff; font-family: -apple-system, Segoe UI, sans-serif;
          display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
  .card {{ background:#2c2f33; padding:2.5rem 3rem; border-radius:12px; text-align:center;
           border-top: 4px solid {color}; max-width: 420px; }}
  h1 {{ margin:0 0 .5rem; font-size:1.4rem; }}
  p {{ color:#b9bbbe; line-height:1.5; }}
</style></head>
<body><div class="card"><h1>{title}</h1><p>{message}</p></div></body></html>"""


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


async def handle_callback(request: web.Request) -> web.Response:
    error = request.query.get("error")
    if error:
        return web.Response(
            text=_page("Verification Cancelled", "You declined the authorization request. You can close this tab and click the button again if that was a mistake.", ok=False),
            content_type="text/html", status=200,
        )

    code  = request.query.get("code")
    state = request.query.get("state", "")
    if not code:
        return web.Response(text=_page("Missing Code", "Discord didn't send an authorization code. Try clicking the button again.", ok=False),
                             content_type="text/html", status=400)

    # state = "<origin_guild_id>" or "<origin_guild_id>:<backup_guild_id>"
    parts = state.split(":", 1)
    origin_guild_id = parts[0] if parts and parts[0] else None
    backup_guild_id = parts[1] if len(parts) > 1 and parts[1] else None
    if not origin_guild_id or not origin_guild_id.isdigit():
        return web.Response(text=_page("Invalid Request", "This verification link is malformed or expired. Ask staff to re-post the verify panel.", ok=False),
                             content_type="text/html", status=400)

    timeout = ClientTimeout(total=15)
    async with ClientSession(timeout=timeout) as session:
        token_data = await _exchange_code(session, code)
        if not token_data or "access_token" not in token_data:
            return web.Response(text=_page("Authorization Failed", "Discord rejected that authorization code (it may have expired — codes are single-use and short-lived). Please try again.", ok=False),
                                 content_type="text/html", status=400)
        access_token = token_data["access_token"]

        identity = await _get_identity(session, access_token)
        if not identity:
            return web.Response(text=_page("Couldn't Identify You", "Got an access token but couldn't fetch your Discord profile. Please try again.", ok=False),
                                 content_type="text/html", status=502)
        user_id = identity["id"]
        username = identity.get("username", "there")

        roles = await _get_guild_roles(session, origin_guild_id)
        verified_role_id   = next((r["id"] for r in roles if r["name"] == VERIFIED_ROLE_NAME), None)
        unverified_role_id = next((r["id"] for r in roles if r["name"] == UNVERIFIED_ROLE_NAME), None)

        if verified_role_id:
            await _set_role(session, origin_guild_id, user_id, verified_role_id, add=True)
        else:
            log.warning("Verified role %r not found in guild %s — role not granted", VERIFIED_ROLE_NAME, origin_guild_id)
        if unverified_role_id:
            await _set_role(session, origin_guild_id, user_id, unverified_role_id, add=False)

        joined_backup = False
        if backup_guild_id:
            joined_backup = await _join_backup_guild(session, backup_guild_id, user_id, access_token)

    extra = " You've also been added to our backup server, so you won't lose your place if this one ever goes away." if joined_backup else ""
    return web.Response(
        text=_page("You're Verified! ✅", f"Welcome, {username} — you now have full access back in Discord.{extra} You can close this tab."),
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
