from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import aiohttp
import discord
from discord import app_commands
from redbot.core import Config, commands
from redbot.core.bot import Red


CODE_PATTERN = re.compile(r"[^A-Z0-9]")


class MMIdleAlpha(commands.Cog):
    """MMIdle alpha access code redeem and onboarding commands."""

    __author__ = "DreadedZombie"
    __version__ = "0.1.2"

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=8873310042, force_registration=True)
        self.config.register_global(
            api_base_url="https://game.mmidle.com",
            redeem_path="/api/integrations/discord/redeem",
            status_path="/api/integrations/discord/status",
            apply_url="https://game.mmidle.com/apply",
            redeem_url="https://game.mmidle.com/redeem",
            bot_secret="",
            request_timeout_seconds=12,
        )
        self.session: aiohttp.ClientSession | None = None

    async def cog_load(self) -> None:
        self._ensure_session()

    def cog_unload(self) -> None:
        if self.session and not self.session.closed:
            self.bot.loop.create_task(self.session.close())

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=15)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session

    @staticmethod
    def _normalize_code(code: str) -> str:
        return CODE_PATTERN.sub("", code.upper().strip())

    @staticmethod
    def _trimmed_url(value: str) -> str:
        return value.strip().rstrip("/")

    async def _fetch_config(self) -> dict[str, Any]:
        values = await self.config.all()
        return {
            "api_base_url": self._trimmed_url(str(values.get("api_base_url", ""))),
            "redeem_path": str(values.get("redeem_path", "/api/integrations/discord/redeem")).strip(),
            "status_path": str(values.get("status_path", "/api/integrations/discord/status")).strip(),
            "apply_url": str(values.get("apply_url", "https://game.mmidle.com/apply")).strip(),
            "redeem_url": str(values.get("redeem_url", "https://game.mmidle.com/redeem")).strip(),
            "bot_secret": str(values.get("bot_secret", "")).strip(),
            "request_timeout_seconds": int(values.get("request_timeout_seconds", 12)),
        }

    async def _redeem_code(self, code: str, discord_user_id: int) -> tuple[int, dict[str, Any], str]:
        cfg = await self._fetch_config()
        if not cfg["api_base_url"] or not cfg["redeem_path"]:
            raise RuntimeError("API URL is not configured.")
        if not cfg["bot_secret"]:
            raise RuntimeError("Bot secret is not configured.")

        path = cfg["redeem_path"]
        if not path.startswith("/"):
            path = f"/{path}"
        url = f"{cfg['api_base_url']}{path}"
        timeout_seconds = max(5, min(cfg["request_timeout_seconds"], 30))
        payload = {
            "code": code,
            "discord_user_id": str(discord_user_id),
            "source": "discord_bot_red",
        }
        headers = {
            "Authorization": f"Bearer {cfg['bot_secret']}",
            "Content-Type": "application/json",
        }

        session = self._ensure_session()
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with session.post(url, json=payload, headers=headers, timeout=timeout) as response:
            try:
                body = await response.json()
                if not isinstance(body, dict):
                    body = {"message": str(body)}
            except aiohttp.ContentTypeError:
                body = {"message": await response.text()}
            return response.status, body, url

    async def _fetch_status(self, discord_user_id: int) -> tuple[int, dict[str, Any], str]:
        cfg = await self._fetch_config()
        if not cfg["api_base_url"] or not cfg["status_path"]:
            raise RuntimeError("Status endpoint is not configured.")
        if not cfg["bot_secret"]:
            raise RuntimeError("Bot secret is not configured.")

        path = cfg["status_path"]
        if not path.startswith("/"):
            path = f"/{path}"
        url = f"{cfg['api_base_url']}{path}"
        timeout_seconds = max(5, min(cfg["request_timeout_seconds"], 30))
        payload = {"discord_user_id": str(discord_user_id)}
        headers = {
            "Authorization": f"Bearer {cfg['bot_secret']}",
            "Content-Type": "application/json",
        }
        session = self._ensure_session()
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with session.post(url, json=payload, headers=headers, timeout=timeout) as response:
            try:
                body = await response.json()
                if not isinstance(body, dict):
                    body = {"message": str(body)}
            except aiohttp.ContentTypeError:
                body = {"message": await response.text()}
            return response.status, body, url

    async def _send_private_reply(self, ctx: commands.Context, message: str) -> None:
        interaction = getattr(ctx, "interaction", None)
        if interaction is not None:
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
                return
            except discord.HTTPException:
                pass

        if ctx.guild:
            try:
                await ctx.author.send(message)
                await ctx.send("Sent you a DM with the result.", delete_after=15)
                return
            except discord.Forbidden:
                pass
        await ctx.send(message)

    @staticmethod
    def _read_error(payload: dict[str, Any]) -> str:
        return str(payload.get("error") or payload.get("message") or "").strip()

    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    @commands.hybrid_command(name="mmidle", aliases=["mmidlealpha"], with_app_command=True)
    async def mmidle_help(self, ctx: commands.Context) -> None:
        """Show MMIdle alpha command shortcuts."""
        cfg = await self._fetch_config()
        await self._send_private_reply(
            ctx,
            "\n".join(
                [
                    "MMIdle alpha commands:",
                    "- /redeem <code> (or prefix: redeem <code>)",
                    "- /alphastatus",
                    "- /alphalink",
                    "",
                    "Links:",
                    f"- Apply + link Discord: {cfg['apply_url']}",
                    f"- Redeem on web: {cfg['redeem_url']}",
                    "",
                    "Admin config: [p]mmalpha ...",
                ]
            ),
        )

    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.describe(code="One-time MMIdle alpha access code")
    @commands.hybrid_command(name="redeem", with_app_command=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def redeem_alpha_code(self, ctx: commands.Context, code: str) -> None:
        """Redeem your MMIdle alpha code."""
        normalized = self._normalize_code(code)
        if len(normalized) < 6:
            await self._send_private_reply(ctx, "Invalid code format. Use the full alpha code.")
            return

        if ctx.guild and getattr(ctx, "message", None):
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass

        try:
            status, payload, _url = await self._redeem_code(normalized, ctx.author.id)
        except asyncio.TimeoutError:
            await self._send_private_reply(
                ctx,
                "MMIdle API timed out. Try again in a few seconds.",
            )
            return
        except aiohttp.ClientError:
            await self._send_private_reply(
                ctx,
                "Could not reach MMIdle API. Try again in a few seconds.",
            )
            return
        except RuntimeError as err:
            await self._send_private_reply(ctx, f"Bot config error: {err}")
            return

        error_text = self._read_error(payload)
        cfg = await self._fetch_config()
        apply_url = cfg["apply_url"]
        redeem_url = cfg["redeem_url"]

        if status in (200, 201):
            await self._send_private_reply(
                ctx,
                "Alpha access unlocked. You can now sign in at game.mmidle.com.",
            )
            return
        if status == 404:
            await self._send_private_reply(
                ctx,
                f"Your Discord is not linked to an MMIdle account.\nLink it first at {apply_url}, then redeem again.",
            )
            return
        if status == 400:
            details = error_text or "Code is invalid or already used."
            await self._send_private_reply(ctx, f"Redeem failed: {details}\nYou can also redeem on site: {redeem_url}")
            return
        if status == 401:
            await self._send_private_reply(
                ctx,
                "Bot auth failed against MMIdle API. Please notify staff.",
            )
            return
        if status == 429:
            await self._send_private_reply(
                ctx,
                "Redeem is rate-limited right now. Try again in a moment.",
            )
            return

        await self._send_private_reply(
            ctx,
            f"Unexpected API response ({status}). Please try again or notify staff.\nDetails: {error_text or 'No error payload'}",
        )

    @redeem_alpha_code.error
    async def redeem_alpha_code_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            wait_seconds = max(0.5, error.retry_after)
            await self._send_private_reply(
                ctx,
                f"Redeem is on cooldown. Try again in {wait_seconds:.1f}s.",
            )
            return
        raise error

    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    @commands.hybrid_command(name="alphalink", with_app_command=True)
    async def alpha_link_help(self, ctx: commands.Context) -> None:
        """Get MMIdle alpha onboarding links."""
        cfg = await self._fetch_config()
        await self._send_private_reply(
            ctx,
            "\n".join(
                [
                    "MMIdle alpha onboarding:",
                    f"- Apply + link Discord: {cfg['apply_url']}",
                    f"- Redeem code on site: {cfg['redeem_url']}",
                    "- Or redeem directly in Discord with /redeem CODE",
                ]
            ),
        )

    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    @commands.hybrid_command(name="alphastatus", with_app_command=True)
    async def alpha_status(self, ctx: commands.Context) -> None:
        """Show MMIdle alpha/link status for your Discord account."""
        try:
            status, payload, _url = await self._fetch_status(ctx.author.id)
        except asyncio.TimeoutError:
            await self._send_private_reply(ctx, "MMIdle API timed out. Try again shortly.")
            return
        except aiohttp.ClientError:
            await self._send_private_reply(ctx, "Could not reach MMIdle API. Try again shortly.")
            return
        except RuntimeError as err:
            await self._send_private_reply(ctx, f"Bot config error: {err}")
            return

        if status == 401:
            await self._send_private_reply(ctx, "Bot auth failed against MMIdle API. Please notify staff.")
            return
        if status == 400:
            details = self._read_error(payload) or "Bad request."
            await self._send_private_reply(ctx, f"Status request failed: {details}")
            return
        if status >= 500:
            details = self._read_error(payload) or "Unknown API error."
            await self._send_private_reply(ctx, f"MMIdle API error: {details}")
            return

        cfg = await self._fetch_config()
        apply_url = cfg["apply_url"]
        redeem_url = cfg["redeem_url"]

        if not bool(payload.get("linked")):
            await self._send_private_reply(
                ctx,
                f"Discord is not linked to an MMIdle account yet.\nLink it here: {apply_url}",
            )
            return

        app_status = str(payload.get("application_status") or "none")
        has_alpha = bool(payload.get("has_alpha_access"))
        issued = payload.get("issued_code") if isinstance(payload.get("issued_code"), dict) else None
        issued_prefix = str(issued.get("code_prefix") or "") if issued else ""
        remaining = int(issued.get("remaining_redemptions") or 0) if issued else 0

        lines = [
            "MMIdle alpha status:",
            f"- Discord linked: yes",
            f"- Alpha access: {'yes' if has_alpha else 'no'}",
            f"- Application status: {app_status}",
        ]
        if issued_prefix:
            lines.append(f"- Issued code prefix: {issued_prefix} (remaining uses: {remaining})")

        if not has_alpha:
            if app_status in {"pending", "in_review"}:
                lines.append("- Your application is still under review.")
            elif app_status == "code_issued":
                lines.append(f"- A one-time code was issued. Redeem with /redeem CODE or on site: {redeem_url}")
            elif app_status == "rejected":
                lines.append("- Your latest application was rejected. Reapply from the site if enabled.")
            elif app_status == "none":
                lines.append(f"- You can apply for alpha here: {apply_url}")

        await self._send_private_reply(ctx, "\n".join(lines))

    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(user="Discord user to inspect (defaults to yourself)")
    @commands.hybrid_command(name="alphadiag", with_app_command=True)
    @commands.admin_or_permissions(administrator=True)
    async def alpha_diag(self, ctx: commands.Context, user: discord.User | None = None) -> None:
        """Staff diagnostic for MMIdle Discord alpha integration."""
        cfg = await self._fetch_config()
        target_user = user or ctx.author
        start = time.perf_counter()
        try:
            status, payload, endpoint = await self._fetch_status(target_user.id)
            latency_ms = int((time.perf_counter() - start) * 1000)
        except asyncio.TimeoutError:
            await self._send_private_reply(ctx, "Alpha diag: status API timed out.")
            return
        except aiohttp.ClientError as err:
            await self._send_private_reply(ctx, f"Alpha diag: cannot reach status API ({err}).")
            return
        except RuntimeError as err:
            await self._send_private_reply(ctx, f"Alpha diag config error: {err}")
            return

        lines = [
            "MMIdle alpha diag:",
            f"- API base: {cfg['api_base_url'] or '(missing)'}",
            f"- Status endpoint: {endpoint}",
            f"- Bot secret configured: {'yes' if cfg['bot_secret'] else 'no'}",
            f"- Timeout setting: {cfg['request_timeout_seconds']}s",
            f"- HTTP status: {status}",
            f"- Latency: {latency_ms}ms",
            f"- Checked Discord user: {target_user.id}",
        ]

        if status >= 400:
            lines.append(f"- API error: {self._read_error(payload) or 'No payload error'}")
            await self._send_private_reply(ctx, "\n".join(lines))
            return

        linked = bool(payload.get("linked"))
        has_alpha = bool(payload.get("has_alpha_access"))
        app_status = str(payload.get("application_status") or "none")
        alpha_mode = str(payload.get("alpha_permission_mode") or "none")
        issued = payload.get("issued_code") if isinstance(payload.get("issued_code"), dict) else None

        lines.extend(
            [
                f"- Linked: {'yes' if linked else 'no'}",
                f"- Alpha access: {'yes' if has_alpha else 'no'}",
                f"- Alpha permission mode: {alpha_mode}",
                f"- Application status: {app_status}",
            ]
        )
        if issued:
            prefix = str(issued.get("code_prefix") or "(none)")
            remaining = int(issued.get("remaining_redemptions") or 0)
            lines.append(f"- Issued code: {prefix} (remaining uses: {remaining})")

        await self._send_private_reply(ctx, "\n".join(lines))

    @commands.group(name="mmalpha")
    @commands.guild_only()
    @commands.admin_or_permissions(administrator=True)
    async def mmalpha_group(self, ctx: commands.Context) -> None:
        """Configure MMIdle alpha integration for this bot."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @mmalpha_group.command(name="show")
    async def mmalpha_show(self, ctx: commands.Context) -> None:
        """Show current cog configuration (secret redacted)."""
        cfg = await self._fetch_config()
        masked_secret = "configured" if cfg["bot_secret"] else "missing"
        await ctx.send(
            "\n".join(
                [
                    f"API Base URL: {cfg['api_base_url'] or '(missing)'}",
                    f"Redeem Path: {cfg['redeem_path']}",
                    f"Status Path: {cfg['status_path']}",
                    f"Apply URL: {cfg['apply_url']}",
                    f"Redeem URL: {cfg['redeem_url']}",
                    f"Request Timeout: {cfg['request_timeout_seconds']}s",
                    f"Bot Secret: {masked_secret}",
                ]
            )
        )

    @mmalpha_group.command(name="setapi")
    async def mmalpha_set_api(self, ctx: commands.Context, api_base_url: str) -> None:
        """Set API base URL (example: https://game.mmidle.com)."""
        value = self._trimmed_url(api_base_url)
        if not (value.startswith("https://") or value.startswith("http://")):
            await ctx.send("API URL must start with http:// or https://")
            return
        await self.config.api_base_url.set(value)
        await ctx.send(f"MMIdle API base set to: {value}")

    @mmalpha_group.command(name="setredeempath")
    async def mmalpha_set_redeem_path(self, ctx: commands.Context, redeem_path: str) -> None:
        """Set redeem API path (default: /api/integrations/discord/redeem)."""
        value = redeem_path.strip()
        if not value:
            await ctx.send("Redeem path cannot be empty.")
            return
        if not value.startswith("/"):
            value = f"/{value}"
        await self.config.redeem_path.set(value)
        await ctx.send(f"Redeem path set to: {value}")

    @mmalpha_group.command(name="setstatuspath")
    async def mmalpha_set_status_path(self, ctx: commands.Context, status_path: str) -> None:
        """Set status API path (default: /api/integrations/discord/status)."""
        value = status_path.strip()
        if not value:
            await ctx.send("Status path cannot be empty.")
            return
        if not value.startswith("/"):
            value = f"/{value}"
        await self.config.status_path.set(value)
        await ctx.send(f"Status path set to: {value}")

    @mmalpha_group.command(name="setapplyurl")
    async def mmalpha_set_apply_url(self, ctx: commands.Context, apply_url: str) -> None:
        """Set the site URL players use for Discord linking and alpha apply."""
        value = apply_url.strip()
        if not (value.startswith("https://") or value.startswith("http://")):
            await ctx.send("Apply URL must start with http:// or https://")
            return
        await self.config.apply_url.set(value)
        await ctx.send(f"Apply URL set to: {value}")

    @mmalpha_group.command(name="setredeemurl")
    async def mmalpha_set_redeem_url(self, ctx: commands.Context, redeem_url: str) -> None:
        """Set the site URL players use for web redeem fallback."""
        value = redeem_url.strip()
        if not (value.startswith("https://") or value.startswith("http://")):
            await ctx.send("Redeem URL must start with http:// or https://")
            return
        await self.config.redeem_url.set(value)
        await ctx.send(f"Redeem URL set to: {value}")

    @mmalpha_group.command(name="settimeout")
    async def mmalpha_set_timeout(self, ctx: commands.Context, seconds: int) -> None:
        """Set API request timeout in seconds (5-30)."""
        if seconds < 5 or seconds > 30:
            await ctx.send("Timeout must be between 5 and 30 seconds.")
            return
        await self.config.request_timeout_seconds.set(seconds)
        await ctx.send(f"Request timeout set to {seconds}s.")

    @mmalpha_group.command(name="setsecret")
    @commands.is_owner()
    async def mmalpha_set_secret(self, ctx: commands.Context, *, secret: str) -> None:
        """Set API secret used to authenticate redeem requests."""
        value = secret.strip()
        if len(value) < 16:
            await ctx.send("Secret must be at least 16 characters.")
            return
        await self.config.bot_secret.set(value)
        await ctx.send("Bot secret updated.")

    @mmalpha_group.command(name="clearsecret")
    @commands.is_owner()
    async def mmalpha_clear_secret(self, ctx: commands.Context) -> None:
        """Clear bot secret (owner only)."""
        await self.config.bot_secret.set("")
        await ctx.send("Bot secret cleared.")
