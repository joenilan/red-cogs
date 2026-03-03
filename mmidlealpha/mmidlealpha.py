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


async def _respond_interaction(interaction: discord.Interaction, message: str, *, ephemeral: bool = True) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(message, ephemeral=ephemeral)


class MMAlphaRolePanelSelect(discord.ui.Select):
    def __init__(self, cog: "MMIdleAlpha", guild_id: int, options: list[discord.SelectOption]) -> None:
        super().__init__(
            custom_id=f"mmalpha:panel:select:{guild_id}",
            placeholder="Pick your MMIdle roles",
            min_values=1,
            max_values=min(len(options), 25),
            options=options[:25],
        )
        self.cog = cog
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        selected_ids = {int(value) for value in self.values if value.isdigit()}
        await self.cog._apply_panel_role_selection(
            interaction=interaction,
            guild_id=self.guild_id,
            selected_role_ids=selected_ids,
        )


class MMAlphaRolePanelClearButton(discord.ui.Button):
    def __init__(self, cog: "MMIdleAlpha", guild_id: int) -> None:
        super().__init__(
            custom_id=f"mmalpha:panel:clear:{guild_id}",
            style=discord.ButtonStyle.secondary,
            label="Clear MMIdle Roles",
        )
        self.cog = cog
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog._apply_panel_role_selection(
            interaction=interaction,
            guild_id=self.guild_id,
            selected_role_ids=set(),
        )


class MMAlphaRolePanelView(discord.ui.View):
    def __init__(self, cog: "MMIdleAlpha", guild_id: int, options: list[discord.SelectOption]) -> None:
        super().__init__(timeout=None)
        self.add_item(MMAlphaRolePanelSelect(cog=cog, guild_id=guild_id, options=options))
        self.add_item(MMAlphaRolePanelClearButton(cog=cog, guild_id=guild_id))


class MMAlphaRoleSetupModal(discord.ui.Modal, title="MMIdle Role Panel Setup"):
    def __init__(
        self,
        cog: "MMIdleAlpha",
        guild: discord.Guild,
        *,
        selected_channel_id: int | None,
        selected_role_ids: list[int],
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.channel_select = self._build_channel_select(selected_channel_id)
        if self.channel_select:
            self.add_item(
                discord.ui.Label(
                    text="Post Channel",
                    description="Choose the channel where the role panel message is posted.",
                    component=self.channel_select,
                )
            )
        self.role_select = self._build_role_select(selected_role_ids)
        if self.role_select:
            self.add_item(
                discord.ui.Label(
                    text="Assignable Roles",
                    description="Pick up to 25 roles users can assign to themselves.",
                    component=self.role_select,
                )
            )
        self.mode_select = self._build_mode_select()
        if self.mode_select:
            self.add_item(
                discord.ui.Label(
                    text="Update Mode",
                    description="Add to existing, replace all, or remove from existing.",
                    component=self.mode_select,
                )
            )

    def _build_channel_select(self, selected_channel_id: int | None) -> discord.ui.Select | None:
        channels = self.guild.text_channels[:25]
        if not channels:
            return None
        options = [
            discord.SelectOption(
                label=f"#{ch.name}"[:100],
                value=str(ch.id),
                default=selected_channel_id == ch.id,
            )
            for ch in channels
        ]
        select_cls = getattr(discord.ui, "StringSelect", discord.ui.Select)
        return select_cls(
            placeholder="Select target channel",
            options=options,
            min_values=1,
            max_values=1,
        )

    def _build_role_select(self, selected_role_ids: list[int]) -> discord.ui.Select | None:
        roles = [r for r in self.guild.roles if not r.is_default()]
        roles = sorted(roles, key=lambda r: r.position, reverse=True)[:25]
        if not roles:
            return None
        selected_set = set(selected_role_ids)
        options = [
            discord.SelectOption(
                label=role.name[:100],
                value=str(role.id),
                default=role.id in selected_set,
            )
            for role in roles
        ]
        select_cls = getattr(discord.ui, "StringSelect", discord.ui.Select)
        return select_cls(
            placeholder="Select roles",
            options=options,
            min_values=1,
            max_values=min(25, len(options)),
        )

    def _build_mode_select(self) -> discord.ui.Select:
        select_cls = getattr(discord.ui, "StringSelect", discord.ui.Select)
        return select_cls(
            placeholder="Select update mode",
            options=[
                discord.SelectOption(
                    label="Add Selected Roles",
                    value="add",
                    description="Merge selected roles into current panel roles.",
                    default=True,
                ),
                discord.SelectOption(
                    label="Replace Panel Roles",
                    value="replace",
                    description="Replace all current panel roles with selected roles.",
                ),
                discord.SelectOption(
                    label="Remove Selected Roles",
                    value="remove",
                    description="Remove selected roles from current panel roles.",
                ),
            ],
            min_values=1,
            max_values=1,
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.guild.id != self.guild.id:
            await _respond_interaction(interaction, "This setup modal is only valid in its original server.")
            return
        if interaction.user.guild_permissions.administrator is False:
            await _respond_interaction(interaction, "Admin permission is required.")
            return
        if not self.channel_select or not self.channel_select.values:
            await _respond_interaction(interaction, "Select a target channel.")
            return
        if not self.role_select or not self.role_select.values:
            await _respond_interaction(interaction, "Select at least one role.")
            return
        if not self.mode_select or not self.mode_select.values:
            await _respond_interaction(interaction, "Select an update mode.")
            return

        channel_id = int(self.channel_select.values[0])
        selected_role_ids = [int(role_id) for role_id in self.role_select.values if role_id.isdigit()]
        update_mode = str(self.mode_select.values[0]).strip().lower()
        existing_role_ids = [
            int(role_id)
            for role_id in await self.cog.config.guild(self.guild).roles_panel_role_ids()
            if str(role_id).isdigit()
        ]
        existing_set = set(existing_role_ids)
        selected_set = set(selected_role_ids)
        if update_mode == "replace":
            final_role_ids = list(selected_set)
        elif update_mode == "remove":
            final_role_ids = list(existing_set - selected_set)
        else:
            final_role_ids = list(existing_set | selected_set)

        channel = self.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            await _respond_interaction(interaction, "Selected channel is invalid.")
            return
        if not final_role_ids:
            await _respond_interaction(interaction, "Resulting panel role list is empty; pick at least one role.")
            return

        await self.cog.config.guild(self.guild).roles_panel_channel_id.set(channel.id)
        await self.cog.config.guild(self.guild).roles_panel_role_ids.set(final_role_ids[:25])

        try:
            message, hidden_count = await self.cog._publish_role_panel(self.guild, channel)
        except RuntimeError as err:
            await _respond_interaction(interaction, str(err))
            return

        lines = [f"Role panel published: {message.jump_url}"]
        lines.append(
            f"Mode: {update_mode} | Selected: {len(selected_set)} | Panel Roles Now: {min(len(final_role_ids), 25)}"
        )
        if hidden_count > 0:
            lines.append(f"Note: {hidden_count} role(s) were skipped due the 25-option Discord limit.")
        await _respond_interaction(interaction, "\n".join(lines))


class MMAlphaOpenSetupModalButton(discord.ui.Button):
    def __init__(self, cog: "MMIdleAlpha", guild_id: int, owner_id: int) -> None:
        super().__init__(
            label="Open MMIdle Role Setup",
            style=discord.ButtonStyle.primary,
            custom_id=f"mmalpha:setup:open:{guild_id}:{owner_id}",
        )
        self.cog = cog
        self.guild_id = guild_id
        self.owner_id = owner_id

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or guild.id != self.guild_id:
            await _respond_interaction(interaction, "This setup button is only valid in its original server.")
            return
        if interaction.user.id != self.owner_id and not interaction.user.guild_permissions.administrator:
            await _respond_interaction(interaction, "Only the command invoker or an admin can use this button.")
            return

        guild_cfg = await self.cog.config.guild(guild).all()
        modal = MMAlphaRoleSetupModal(
            cog=self.cog,
            guild=guild,
            selected_channel_id=int(guild_cfg.get("roles_panel_channel_id") or 0) or None,
            selected_role_ids=[int(r) for r in guild_cfg.get("roles_panel_role_ids", []) if str(r).isdigit()],
        )
        await interaction.response.send_modal(modal)


class MMAlphaOpenSetupModalView(discord.ui.View):
    def __init__(self, cog: "MMIdleAlpha", guild_id: int, owner_id: int) -> None:
        super().__init__(timeout=600)
        self.add_item(MMAlphaOpenSetupModalButton(cog=cog, guild_id=guild_id, owner_id=owner_id))


class MMIdleAlpha(commands.Cog):
    """MMIdle alpha access code redeem and onboarding commands."""

    __author__ = "DreadedZombie"
    __version__ = "0.2.0"

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
        self.config.register_guild(
            roles_panel_channel_id=0,
            roles_panel_message_id=0,
            roles_panel_role_ids=[],
            roles_panel_title="MMIdle Role Onboarding",
            roles_panel_description=(
                "Pick your server roles below. "
                "Use the dropdown to add or remove your MMIdle server roles."
            ),
        )
        self.session: aiohttp.ClientSession | None = None

    async def cog_load(self) -> None:
        self._ensure_session()
        await self._restore_role_panel_views()

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

    @staticmethod
    def _dedupe_role_ids(role_ids: list[int]) -> list[int]:
        seen: set[int] = set()
        result: list[int] = []
        for role_id in role_ids:
            if role_id in seen:
                continue
            seen.add(role_id)
            result.append(role_id)
        return result

    async def _restore_role_panel_views(self) -> None:
        guild_configs = await self.config.all_guilds()
        for guild_id_raw, guild_cfg in guild_configs.items():
            try:
                guild_id = int(guild_id_raw)
            except (TypeError, ValueError):
                continue
            message_id = int(guild_cfg.get("roles_panel_message_id") or 0)
            if message_id <= 0:
                continue
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            view, _hidden_count = await self._build_role_panel_view(guild)
            if view is None:
                continue
            self.bot.add_view(view, message_id=message_id)

    async def _build_role_panel_view(self, guild: discord.Guild) -> tuple[MMAlphaRolePanelView | None, int]:
        role_ids = self._dedupe_role_ids(
            [int(r) for r in await self.config.guild(guild).roles_panel_role_ids() if str(r).isdigit()]
        )
        options: list[discord.SelectOption] = []
        for role_id in role_ids:
            role = guild.get_role(role_id)
            if role is None or role.is_default():
                continue
            options.append(
                discord.SelectOption(
                    label=role.name[:100],
                    value=str(role.id),
                )
            )

        hidden_count = 0
        if len(options) > 25:
            hidden_count = len(options) - 25
            options = options[:25]
        if not options:
            return None, hidden_count
        return MMAlphaRolePanelView(cog=self, guild_id=guild.id, options=options), hidden_count

    async def _build_role_panel_embed(self, guild: discord.Guild) -> discord.Embed:
        title = str(await self.config.guild(guild).roles_panel_title()).strip() or "MMIdle Role Onboarding"
        description = str(await self.config.guild(guild).roles_panel_description()).strip()
        if not description:
            description = "Pick your server roles below."
        embed = discord.Embed(
            title=title[:256],
            description=description[:4000],
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="How this works",
            value=(
                "- Select one or more roles in the dropdown.\n"
                "- Submit selection to apply/remove roles instantly.\n"
                "- Use **Clear MMIdle Roles** to remove all roles from this panel."
            ),
            inline=False,
        )
        return embed

    async def _publish_role_panel(
        self, guild: discord.Guild, channel: discord.TextChannel
    ) -> tuple[discord.Message, int]:
        bot_member = guild.me or (guild.get_member(self.bot.user.id) if self.bot.user else None)
        if bot_member is None:
            raise RuntimeError("Bot member context missing; cannot publish role panel.")
        if not channel.permissions_for(bot_member).send_messages:
            raise RuntimeError(f"I cannot send messages in {channel.mention}.")
        if not channel.permissions_for(bot_member).embed_links:
            raise RuntimeError(f"I cannot embed links in {channel.mention}.")
        if not bot_member.guild_permissions.manage_roles:
            raise RuntimeError("I need Manage Roles permission to run this panel.")

        view, hidden_count = await self._build_role_panel_view(guild)
        if view is None:
            raise RuntimeError("No valid roles configured for panel. Use /mmidlerolesetup first.")
        embed = await self._build_role_panel_embed(guild)

        existing_message_id = int(await self.config.guild(guild).roles_panel_message_id() or 0)
        existing_channel_id = int(await self.config.guild(guild).roles_panel_channel_id() or 0)
        message: discord.Message | None = None
        if existing_message_id > 0 and existing_channel_id == channel.id:
            try:
                existing = await channel.fetch_message(existing_message_id)
                await existing.edit(embed=embed, view=view)
                message = existing
            except discord.HTTPException:
                message = None

        if message is None:
            message = await channel.send(embed=embed, view=view)

        await self.config.guild(guild).roles_panel_channel_id.set(channel.id)
        await self.config.guild(guild).roles_panel_message_id.set(message.id)
        self.bot.add_view(view, message_id=message.id)
        return message, hidden_count

    async def _apply_panel_role_selection(
        self,
        *,
        interaction: discord.Interaction,
        guild_id: int,
        selected_role_ids: set[int],
    ) -> None:
        guild = interaction.guild
        if guild is None or guild.id != guild_id:
            await _respond_interaction(interaction, "This role panel is only valid in its original server.")
            return
        member = interaction.user if isinstance(interaction.user, discord.Member) else guild.get_member(interaction.user.id)
        if member is None:
            await _respond_interaction(interaction, "Could not resolve your member record in this server.")
            return
        bot_member = guild.me or (guild.get_member(self.bot.user.id) if self.bot.user else None)
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            await _respond_interaction(interaction, "Bot is missing Manage Roles permission.")
            return

        panel_role_ids = self._dedupe_role_ids(
            [int(r) for r in await self.config.guild(guild).roles_panel_role_ids() if str(r).isdigit()]
        )
        panel_roles: list[discord.Role] = []
        unmanageable: list[str] = []
        for role_id in panel_role_ids:
            role = guild.get_role(role_id)
            if role is None or role.is_default():
                continue
            if role >= bot_member.top_role:
                unmanageable.append(role.name)
                continue
            panel_roles.append(role)
        if not panel_roles:
            await _respond_interaction(interaction, "No manageable roles are configured for this panel.")
            return

        selected_set = {role.id for role in panel_roles if role.id in selected_role_ids}
        to_add = [role for role in panel_roles if role.id in selected_set and role not in member.roles]
        to_remove = [role for role in panel_roles if role.id not in selected_set and role in member.roles]

        try:
            if to_add:
                await member.add_roles(*to_add, reason="MMIdle role panel selection")
            if to_remove:
                await member.remove_roles(*to_remove, reason="MMIdle role panel selection")
        except discord.Forbidden:
            await _respond_interaction(
                interaction,
                "I could not update roles (permission/hierarchy issue). Ask staff to verify my role position.",
            )
            return
        except discord.HTTPException:
            await _respond_interaction(interaction, "Discord rejected the role update. Try again in a moment.")
            return

        added_names = ", ".join(role.name for role in to_add) or "none"
        removed_names = ", ".join(role.name for role in to_remove) or "none"
        lines = [
            "MMIdle role update complete.",
            f"Added: {added_names}",
            f"Removed: {removed_names}",
        ]
        if unmanageable:
            lines.append(f"Skipped (bot role hierarchy): {', '.join(unmanageable)}")
        await _respond_interaction(interaction, "\n".join(lines))

    @commands.command(name="mmidle", aliases=["mmidlealpha"])
    async def mmidle_help(self, ctx: commands.Context) -> None:
        """Show MMIdle alpha command shortcuts."""
        cfg = await self._fetch_config()
        await self._send_private_reply(
            ctx,
            "\n".join(
                [
                    "MMIdle alpha commands (prefix):",
                    "- redeem <code> (slash also: /redeem)",
                    "- alphastatus",
                    "- alphalink",
                    "- rolepanel (show role panel link in this server)",
                    "",
                    "Links:",
                    f"- Apply + link Discord: {cfg['apply_url']}",
                    f"- Redeem on web: {cfg['redeem_url']}",
                    "",
                    "Admin config: [p]mmalpha ...",
                    "Admin role onboarding UI: rolesetup (slash optional for direct modal)",
                ]
            ),
        )

    @commands.command(name="rolepanel", aliases=["mmidleroles"])
    @commands.guild_only()
    async def mmidle_roles(self, ctx: commands.Context) -> None:
        """Show the MMIdle role onboarding panel link."""
        guild = ctx.guild
        if guild is None:
            await self._send_private_reply(ctx, "Use this command inside your server.")
            return
        channel_id = int(await self.config.guild(guild).roles_panel_channel_id() or 0)
        message_id = int(await self.config.guild(guild).roles_panel_message_id() or 0)
        if channel_id <= 0 or message_id <= 0:
            await self._send_private_reply(ctx, "Role onboarding panel is not configured yet.")
            return
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            await self._send_private_reply(ctx, "Configured onboarding channel is invalid. Ask staff to republish.")
            return
        panel_url = f"https://discord.com/channels/{guild.id}/{channel.id}/{message_id}"
        await self._send_private_reply(ctx, f"MMIdle role panel: {panel_url}")

    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.default_permissions(administrator=True)
    @commands.hybrid_command(name="rolesetup", aliases=["mmidlerolesetup"], with_app_command=True)
    @commands.guild_only()
    @commands.admin_or_permissions(administrator=True)
    async def mmidle_roles_setup(self, ctx: commands.Context) -> None:
        """Open MMIdle role panel setup (prefix-friendly + modal)."""
        guild = ctx.guild
        if guild is None:
            await self._send_private_reply(ctx, "Use this command inside your server.")
            return

        interaction = getattr(ctx, "interaction", None)
        if interaction is None:
            view = MMAlphaOpenSetupModalView(cog=self, guild_id=guild.id, owner_id=ctx.author.id)
            await ctx.send("Click the button to open MMIdle role setup modal.", view=view)
            return

        guild_cfg = await self.config.guild(guild).all()
        modal = MMAlphaRoleSetupModal(
            cog=self,
            guild=guild,
            selected_channel_id=int(guild_cfg.get("roles_panel_channel_id") or 0) or None,
            selected_role_ids=[int(r) for r in guild_cfg.get("roles_panel_role_ids", []) if str(r).isdigit()],
        )
        try:
            await interaction.response.send_modal(modal)
        except discord.HTTPException:
            await self._send_private_reply(ctx, "Could not open setup modal. Try again.")

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

    @commands.command(name="alphalink")
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
                    "- Or redeem directly in Discord with redeem CODE or /redeem CODE",
                ]
            ),
        )

    @commands.command(name="alphastatus")
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

    @commands.command(name="alphadiag")
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
        guild_cfg = await self.config.guild(ctx.guild).all() if ctx.guild else {}
        masked_secret = "configured" if cfg["bot_secret"] else "missing"
        role_channel_id = int(guild_cfg.get("roles_panel_channel_id") or 0)
        role_message_id = int(guild_cfg.get("roles_panel_message_id") or 0)
        role_count = len([r for r in guild_cfg.get("roles_panel_role_ids", []) if str(r).isdigit()])
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
                    f"Roles Panel Channel ID: {role_channel_id or '(missing)'}",
                    f"Roles Panel Message ID: {role_message_id or '(missing)'}",
                    f"Roles Configured: {role_count}",
                ]
            )
        )

    @mmalpha_group.command(name="roleshow")
    async def mmalpha_role_show(self, ctx: commands.Context) -> None:
        """Show role onboarding panel configuration for this server."""
        guild = ctx.guild
        guild_cfg = await self.config.guild(guild).all()
        channel_id = int(guild_cfg.get("roles_panel_channel_id") or 0)
        message_id = int(guild_cfg.get("roles_panel_message_id") or 0)
        role_ids = [int(r) for r in guild_cfg.get("roles_panel_role_ids", []) if str(r).isdigit()]
        roles = [guild.get_role(role_id) for role_id in role_ids]
        role_names = [role.name for role in roles if role is not None]
        if not role_names:
            role_display = "(none)"
        else:
            role_display = ", ".join(role_names[:25])
        panel_url = (
            f"https://discord.com/channels/{guild.id}/{channel_id}/{message_id}"
            if channel_id and message_id
            else "(not published)"
        )
        await ctx.send(
            "\n".join(
                [
                    f"Panel Title: {guild_cfg.get('roles_panel_title', 'MMIdle Role Onboarding')}",
                    f"Panel Channel ID: {channel_id or '(missing)'}",
                    f"Panel Message ID: {message_id or '(missing)'}",
                    f"Panel URL: {panel_url}",
                    f"Configured Roles ({len(role_names)}): {role_display}",
                ]
            )
        )

    @mmalpha_group.command(name="publishroles")
    async def mmalpha_publish_roles(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        """Publish or refresh the MMIdle role onboarding panel."""
        guild = ctx.guild
        if channel is None:
            stored_channel_id = int(await self.config.guild(guild).roles_panel_channel_id() or 0)
            if stored_channel_id <= 0:
                await ctx.send("No panel channel configured. Use /mmidlerolesetup first.")
                return
            configured_channel = guild.get_channel(stored_channel_id)
            if not isinstance(configured_channel, discord.TextChannel):
                await ctx.send("Configured panel channel is invalid. Use /mmidlerolesetup.")
                return
            channel = configured_channel
        else:
            await self.config.guild(guild).roles_panel_channel_id.set(channel.id)

        try:
            message, hidden_count = await self._publish_role_panel(guild, channel)
        except RuntimeError as err:
            await ctx.send(str(err))
            return

        lines = [f"Role panel published: {message.jump_url}"]
        if hidden_count > 0:
            lines.append(f"Note: {hidden_count} role(s) were skipped (Discord limit is 25 options).")
        await ctx.send("\n".join(lines))

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
