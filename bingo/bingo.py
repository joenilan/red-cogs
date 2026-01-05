import datetime as dt
import random
import re
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

import discord
from PIL import Image, ImageDraw, ImageFont
from redbot.core import Config, commands
from redbot.core.data_manager import cog_data_path

DEFAULT_TASKS = [
    "Demo an opponent",
    "Be MVP of the match",
    "Score with 0 boost",
    "Fake kickoff goal",
    "Win overtime",
    "Backboard read",
    "0 second goal",
    "Double tap goal",
    "Flip reset goal",
    "Demo same person in < 10 seconds",
    "Own goal (opponent)",
    "Entire team plays meme car (Merc, Scarab, etc)",
    "Same car preset",
    "Rule 1",
    "Low five",
    "High five",
    "Brazil",
    "Get What A Save'd by entire team",
    "Score a 70 mph goal (120 kph)",
    "Everyone in team uses bunny topper",
    "Squishy save",
    "Member in team reaches 1k points",
    "Hit a funny number (67, 69, 420, 666)",
    "Triple reset",
    "Triple commit",
    "Pink and cyan car colors - DZ colors",
    "Dribble for +3 seconds",
    "Team pinch",
    "+2 comeback",
    "Mid air demo",
    "Musty flick goal",
    "Score while driving backwards",
]

# Pixel bounds detected from the provided template (card.jpg).
CELL_COLS = [(340, 800), (896, 1352), (1444, 1904), (1996, 2460), (2552, 3012)]
CELL_ROWS = [(1600, 2060), (2180, 2640), (2760, 3220), (3340, 3800), (3916, 4380)]

CENTER_CELL = (2, 2)
CELL_PADDING = 20
TEXT_COLOR = (20, 20, 20)


class Bingo(commands.Cog):
    """Generate bingo cards from a template image."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=8352147791)
        self.config.register_global(tasks=[], font_path=None)
        self.config.register_guild(editor_role_id=None, cards={})
        self.template_path = self._resolve_template_path()

    async def cog_load(self):
        tasks = await self.config.tasks()
        if not tasks:
            await self.config.tasks.set(DEFAULT_TASKS)
        await self._migrate_editor_role()
        self._hide_non_help_commands()

    async def _migrate_editor_role(self) -> None:
        all_guilds = await self.config.all_guilds()
        for guild_id, data in (all_guilds or {}).items():
            if data.get("editor_role_id"):
                continue
            legacy_role = data.get("captain_role_id")
            if legacy_role:
                await self.config.guild_from_id(guild_id).editor_role_id.set(legacy_role)

    def _hide_non_help_commands(self) -> None:
        for command in self.walk_commands():
            if command.name != "bingohelp":
                command.hidden = True

    @commands.group(name="bingo")
    @commands.guild_only()
    @commands.admin_or_permissions(administrator=True)
    async def bingo(self, ctx: commands.Context) -> None:
        """Bingo card generator."""
        if ctx.invoked_subcommand is None:
            await ctx.send("Use `!bingohelp` for bingo commands.")

    @commands.command(name="bingohelp")
    @commands.guild_only()
    @commands.admin_or_permissions(administrator=True)
    async def bingohelp(self, ctx: commands.Context) -> None:
        """Display help for bingo commands."""
        embed = discord.Embed(title="Bingo Help", color=0x5FD6FF)
        embed.add_field(
            name="Card Generation",
            value=(
                "`bingo setup` - open the interactive generator\n"
                "`bingo generate <count> [seed]` - generate a batch of cards\n"
                "`bingo tasks` - list current task pool"
            ),
            inline=False,
        )
        embed.add_field(
            name="Editor Controls",
            value=(
                "`bingo editrole @role` - set a default editor role\n"
                "`bingo mark <index>` - mark a square (1-25)\n"
                "`bingo unmark <index>` - unmark a square"
            ),
            inline=False,
        )
        embed.add_field(
            name="Task Pool",
            value=(
                "`bingo addtask <task>` - add a task to the pool\n"
                "`bingo removetask <index>` - remove a task by number"
            ),
            inline=False,
        )
        embed.add_field(
            name="Style",
            value="`bingo setfont <path>` - set a custom TTF font path",
            inline=False,
        )
        await ctx.send(embed=embed)

    @bingo.command(name="tasks")
    async def bingo_tasks(self, ctx: commands.Context) -> None:
        """List current task pool."""
        tasks = await self.config.tasks()
        if not tasks:
            await ctx.send("No tasks configured yet.")
            return
        lines = [f"{idx + 1}. {task}" for idx, task in enumerate(tasks)]
        message = "Current tasks:\n" + "\n".join(lines)
        await ctx.send(message)

    @bingo.command(name="addtask")
    async def bingo_addtask(self, ctx: commands.Context, *, task: str) -> None:
        """Add a task to the pool."""
        task = task.strip()
        if not task:
            await ctx.send("Task cannot be empty.")
            return
        tasks = await self.config.tasks()
        tasks.append(task)
        await self.config.tasks.set(tasks)
        await ctx.send(f"Added task #{len(tasks)}.")

    @bingo.command(name="removetask")
    async def bingo_removetask(self, ctx: commands.Context, index: int) -> None:
        """Remove a task by its number (1-based)."""
        tasks = await self.config.tasks()
        if index < 1 or index > len(tasks):
            await ctx.send("Invalid task number.")
            return
        removed = tasks.pop(index - 1)
        await self.config.tasks.set(tasks)
        await ctx.send(f"Removed: {removed}")

    @bingo.command(name="setfont")
    async def bingo_setfont(self, ctx: commands.Context, *, font_path: Optional[str] = None) -> None:
        """Set a custom TTF font path (or clear it)."""
        if not font_path:
            await self.config.font_path.set(None)
            await ctx.send("Font override cleared.")
            return
        font_file = Path(font_path)
        if not font_file.exists():
            await ctx.send("Font path not found.")
            return
        await self.config.font_path.set(str(font_file))
        await ctx.send("Font override saved.")

    @bingo.command(name="editrole")
    async def bingo_editrole(
        self, ctx: commands.Context, role: Optional[discord.Role] = None
    ) -> None:
        """Set the default editor role required to edit bingo cards."""
        await self._set_editor_role(ctx, role)

    async def _set_editor_role(
        self, ctx: commands.Context, role: Optional[discord.Role]
    ) -> None:
        if role is None:
            await self.config.guild(ctx.guild).editor_role_id.set(None)
            await ctx.send("Editor role requirement cleared.")
            return
        await self.config.guild(ctx.guild).editor_role_id.set(role.id)
        await ctx.send(f"Editor role set to {role.mention}.")

    @bingo.command(name="generate")
    async def bingo_generate(
        self,
        ctx: commands.Context,
        count: int,
        seed: Optional[int] = None,
    ) -> None:
        """Generate one or more bingo cards."""
        if count < 1 or count > 50:
            await ctx.send("Count must be between 1 and 50.")
            return

        tasks, font_path = await self._load_generation_inputs(ctx)
        if tasks is None:
            return

        async with ctx.typing():
            output_files, seed, out_dir = self._generate_cards(count, seed, tasks, font_path)

        if count == 1:
            await ctx.send(
                f"Generated card with seed {seed}.",
                file=discord.File(str(output_files[0]), filename=output_files[0].name),
            )
            return
        await self._send_zip_if_possible(ctx.channel, output_files, seed, count, out_dir)

    @bingo.command(name="setup")
    async def bingo_setup(self, ctx: commands.Context) -> None:
        """Open the interactive generator modal."""
        view = BingoSetupView(self, ctx.author.id)
        await ctx.send("Click to post a bingo card:", view=view)

    @bingo.command(name="mark")
    async def bingo_mark(self, ctx: commands.Context, index: int) -> None:
        """Mark a square on the current channel's card."""
        await self._update_card_mark(ctx, index, True)

    @bingo.command(name="unmark")
    async def bingo_unmark(self, ctx: commands.Context, index: int) -> None:
        """Unmark a square on the current channel's card."""
        await self._update_card_mark(ctx, index, False)

    def _render_card(
        self,
        tasks: List[str],
        output_path: Path,
        font_path: Optional[str],
        marked: Optional[set] = None,
    ) -> None:
        base = Image.open(self.template_path).convert("RGBA")
        draw = ImageDraw.Draw(base)
        task_iter = iter(tasks)

        for row_idx, row in enumerate(CELL_ROWS):
            for col_idx, col in enumerate(CELL_COLS):
                index = row_idx * 5 + col_idx + 1
                if (row_idx, col_idx) == CENTER_CELL:
                    continue
                task = next(task_iter)
                box = self._cell_box(col, row)
                self._draw_wrapped_text(draw, task, box, font_path)
                if marked and index in marked:
                    self._draw_checkmark(draw, col, row)

        base.save(output_path, format="PNG")

    def _cell_box(self, col: Tuple[int, int], row: Tuple[int, int]) -> Tuple[int, int, int, int]:
        x0, x1 = col
        y0, y1 = row
        return (
            x0 + CELL_PADDING,
            y0 + CELL_PADDING,
            x1 - CELL_PADDING,
            y1 - CELL_PADDING,
        )

    def _draw_wrapped_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        box: Tuple[int, int, int, int],
        font_path: Optional[str],
    ) -> None:
        x0, y0, x1, y1 = box
        max_width = x1 - x0
        max_height = y1 - y0

        for size in range(48, 17, -2):
            font = self._load_font(font_path, size)
            lines = self._wrap_text(draw, text, font, max_width)
            line_height = self._line_height(draw, font)
            total_height = line_height * len(lines) + (len(lines) - 1) * 4
            max_line_width = max(draw.textlength(line, font=font) for line in lines)
            if total_height <= max_height and max_line_width <= max_width:
                break
        else:
            font = self._load_font(font_path, 18)
            lines = self._wrap_text(draw, text, font, max_width)
            line_height = self._line_height(draw, font)
            total_height = line_height * len(lines) + (len(lines) - 1) * 4

        y = y0 + (max_height - total_height) / 2
        for line in lines:
            line_width = draw.textlength(line, font=font)
            x = x0 + (max_width - line_width) / 2
            draw.text((x, y), line, font=font, fill=TEXT_COLOR)
            y += line_height + 4

    def _wrap_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont,
        max_width: int,
    ) -> List[str]:
        words = text.split()
        lines: List[str] = []
        current = ""
        for word in words:
            trial = f"{current} {word}".strip()
            if draw.textlength(trial, font=font) <= max_width:
                current = trial
                continue
            if current:
                lines.append(current)
            if draw.textlength(word, font=font) <= max_width:
                current = word
            else:
                current = self._break_long_word(draw, word, font, max_width, lines)
        if current:
            lines.append(current)
        return lines

    def _break_long_word(
        self,
        draw: ImageDraw.ImageDraw,
        word: str,
        font: ImageFont.FreeTypeFont,
        max_width: int,
        lines: List[str],
    ) -> str:
        fragment = ""
        for ch in word:
            trial = fragment + ch
            if draw.textlength(trial, font=font) <= max_width:
                fragment = trial
                continue
            if fragment:
                lines.append(fragment)
            fragment = ch
        return fragment

    def _line_height(self, draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont) -> int:
        box = draw.textbbox((0, 0), "Ag", font=font)
        return box[3] - box[1]

    def _load_font(self, font_path: Optional[str], size: int) -> ImageFont.FreeTypeFont:
        if font_path:
            try:
                return ImageFont.truetype(font_path, size)
            except OSError:
                pass

        candidates = [
            "DejaVuSans.ttf",
            "arial.ttf",
            str(Path("C:/Windows/Fonts/arial.ttf")),
            str(Path("C:/Windows/Fonts/calibri.ttf")),
        ]
        for candidate in candidates:
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
        return ImageFont.load_default()

    def _resolve_template_path(self) -> Path:
        folder = Path(__file__).parent
        png_path = folder / "card.png"
        if png_path.exists():
            return png_path
        return folder / "card.jpg"

    def _draw_checkmark(
        self,
        draw: ImageDraw.ImageDraw,
        col: Tuple[int, int],
        row: Tuple[int, int],
    ) -> None:
        x0, x1 = col
        y0, y1 = row
        inset = 60
        start = (x0 + inset, y0 + (y1 - y0) * 0.55)
        mid = (x0 + (x1 - x0) * 0.45, y0 + y1 - inset)
        end = (x1 - inset, y0 + inset)
        draw.line([start, mid, end], fill=(22, 153, 74, 220), width=22, joint="curve")

    async def _load_generation_inputs(
        self, ctx: commands.Context
    ) -> Tuple[Optional[List[str]], Optional[str]]:
        if not self.template_path.exists():
            await ctx.send("Template image not found (card.jpg).")
            return None, None

        tasks = await self.config.tasks()
        if len(tasks) < 24:
            await ctx.send("You need at least 24 tasks to generate a card.")
            return None, None

        font_path = await self.config.font_path()
        return tasks, font_path

    def _generate_cards(
        self,
        count: int,
        seed: Optional[int],
        tasks: List[str],
        font_path: Optional[str],
    ) -> Tuple[List[Path], int, Path]:
        if seed is None:
            seed = random.SystemRandom().randint(1, 2**32 - 1)

        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = cog_data_path(self) / "generated" / timestamp
        out_dir.mkdir(parents=True, exist_ok=True)

        output_files: List[Path] = []
        for i in range(count):
            card_seed = seed + i
            rng = random.Random(card_seed)
            card_tasks = rng.sample(tasks, k=24)
            output_path = out_dir / f"bingo_card_{i + 1:02d}.png"
            self._render_card(card_tasks, output_path, font_path)
            output_files.append(output_path)

        return output_files, seed, out_dir

    async def _send_zip_if_possible(
        self,
        channel: discord.abc.Messageable,
        output_files: List[Path],
        seed: int,
        count: int,
        out_dir: Path,
        role: Optional[discord.Role] = None,
    ) -> Optional[Path]:
        zip_path = out_dir / "bingo_cards.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in output_files:
                zf.write(path, arcname=path.name)

        zip_size = zip_path.stat().st_size
        max_upload = 8 * 1024 * 1024
        if zip_size > max_upload:
            await channel.send(
                f"Generated {count} cards with base seed {seed}. "
                f"Zip is too large to upload ({zip_size / (1024 * 1024):.1f} MB). "
                f"Find files at: {zip_path}"
            )
            return None

        content = None
        allowed_mentions = discord.AllowedMentions.none()
        if role:
            content = role.mention
            allowed_mentions = discord.AllowedMentions(roles=[role])

        await channel.send(
            content=content,
            file=discord.File(str(zip_path), filename=zip_path.name),
            allowed_mentions=allowed_mentions,
        )
        return zip_path

    async def _update_card_mark(self, ctx: commands.Context, index: int, mark: bool) -> None:
        if index < 1 or index > 25:
            await ctx.send("Index must be between 1 and 25.")
            return
        if index == 13:
            await ctx.send("Center is the free space and cannot be marked.")
            return

        cards = await self.config.guild(ctx.guild).cards()
        card = cards.get(str(ctx.channel.id))
        if not card:
            await ctx.send("No tracked bingo card in this channel. Use `!bingo setup`.")
            return
        if not await self._require_editor(ctx, card):
            return

        marked = set(card.get("marked") or [])
        if mark:
            marked.add(index)
        else:
            marked.discard(index)
        card["marked"] = sorted(marked)
        cards[str(ctx.channel.id)] = card
        await self.config.guild(ctx.guild).cards.set(cards)

        output_path = self._active_card_path(ctx.channel.id)
        font_path = await self.config.font_path()
        tasks = card.get("tasks") or []
        if len(tasks) != 24:
            await ctx.send("Stored card data is invalid. Regenerate the card.")
            return

        async with ctx.typing():
            self._render_card(tasks, output_path, font_path, marked)
            await self._send_or_update_card(ctx.channel, card, output_path)

    async def _require_editor(self, ctx: commands.Context, card: dict) -> bool:
        if ctx.author.guild_permissions.administrator:
            return True
        editor_user_id = card.get("editor_user_id")
        if editor_user_id:
            if ctx.author.id == editor_user_id:
                return True
            await ctx.send("Only the assigned editor can edit this card.")
            return False

        editor_role_id = card.get("editor_role_id")
        if editor_role_id:
            role = ctx.guild.get_role(editor_role_id)
            if role and role in ctx.author.roles:
                return True
            await ctx.send("Only the assigned role can edit this card.")
            return False

        role_id = await self.config.guild(ctx.guild).editor_role_id()
        if not role_id:
            await ctx.send("No editor role is configured. Use `!bingo editrole`.")
            return False
        role = ctx.guild.get_role(role_id)
        if role and role in ctx.author.roles:
            return True
        await ctx.send("Only the editor role can edit this card.")
        return False

    async def _send_or_update_card(
        self,
        channel: discord.TextChannel,
        card: dict,
        output_path: Path,
        *,
        mention_role: Optional[discord.Role] = None,
        mention_user_id: Optional[int] = None,
    ) -> None:
        message_id = card.get("message_id")
        allowed_mentions = discord.AllowedMentions.none()
        content = None
        if mention_role:
            content = mention_role.mention
            allowed_mentions = discord.AllowedMentions(roles=[mention_role])
        elif mention_user_id:
            member = channel.guild.get_member(mention_user_id)
            if member:
                content = member.mention
                allowed_mentions = discord.AllowedMentions(users=[member])

        if message_id:
            try:
                message = await channel.fetch_message(int(message_id))
                await message.edit(
                    content=content,
                    attachments=[discord.File(str(output_path), filename=output_path.name)],
                    allowed_mentions=allowed_mentions,
                )
                return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        message = await channel.send(
            content=content,
            file=discord.File(str(output_path), filename=output_path.name),
            allowed_mentions=allowed_mentions,
        )
        card["message_id"] = message.id

    def _active_card_path(self, channel_id: int) -> Path:
        out_dir = cog_data_path(self) / "generated" / "active"
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / f"bingo_card_{channel_id}.png"


class BingoSetupView(discord.ui.View):
    def __init__(self, cog: Bingo, author_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.author_id = author_id
        self.add_item(BingoSetupButton(cog))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the command invoker can use this setup.", ephemeral=True
            )
            return False
        return True


class BingoSetupButton(discord.ui.Button):
    def __init__(self, cog: Bingo):
        super().__init__(label="Post Bingo Card", style=discord.ButtonStyle.green)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BingoPostModal(self.cog, interaction.guild))


class BingoPostModal(discord.ui.Modal, title="Post Bingo Card"):
    def __init__(self, cog: Bingo, guild: Optional[discord.Guild]):
        super().__init__()
        self.cog = cog
        self.guild = guild

        self.seed = discord.ui.TextInput(
            label="Seed (optional)",
            required=False,
        )
        self.editor_user = discord.ui.TextInput(
            label="Editor @mention or user ID (optional)",
            required=False,
        )
        self.add_item(self.seed)
        self.add_item(self.editor_user)

        self.channel_select = self._build_channel_select()
        if self.channel_select:
            label = discord.ui.Label(
                text="Target channel",
                description="Select where the card should be posted.",
                component=self.channel_select,
            )
            self.add_item(label)

        self.editor_role_select = self._build_editor_role_select()
        if self.editor_role_select:
            label = discord.ui.Label(
                text="Editor role (optional)",
                description="Role that can edit the card (also pinged).",
                component=self.editor_role_select,
            )
            self.add_item(label)

    def _build_channel_select(self) -> Optional[discord.ui.Select]:
        if not self.guild:
            return None
        options = [
            discord.SelectOption(label=f"#{ch.name}", value=str(ch.id))
            for ch in self.guild.text_channels[:25]
        ]
        if not options:
            return None
        select_cls = getattr(discord.ui, "StringSelect", discord.ui.Select)
        return select_cls(
            placeholder="Select a channel",
            options=options,
            min_values=1,
            max_values=1,
        )

    def _build_editor_role_select(self) -> Optional[discord.ui.Select]:
        if not self.guild:
            return None
        roles = [r for r in self.guild.roles if r.name != "@everyone"]
        roles = sorted(roles, key=lambda r: r.position, reverse=True)[:24]
        if not roles:
            return None
        options = [discord.SelectOption(label="No editor role", value="none")]
        options.extend(discord.SelectOption(label=r.name, value=str(r.id)) for r in roles)
        select_cls = getattr(discord.ui, "StringSelect", discord.ui.Select)
        return select_cls(
            placeholder="Select an editor role",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        seed = None
        seed_value = self.seed.value.strip()
        if seed_value:
            try:
                seed = int(seed_value)
            except ValueError:
                await interaction.response.send_message("Seed must be a number.", ephemeral=True)
                return

        channel_id = None
        if self.channel_select and self.channel_select.values:
            channel_id = int(self.channel_select.values[0])
        if not channel_id:
            await interaction.response.send_message("Select a target channel.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(channel_id)
        if channel is None:
            await interaction.response.send_message("Channel not found.", ephemeral=True)
            return

        editor_role = None
        if self.editor_role_select and self.editor_role_select.values:
            selected_role = self.editor_role_select.values[0]
            if selected_role != "none":
                role_id = int(selected_role)
                editor_role = interaction.guild.get_role(role_id)

        editor_user_id = None
        editor_user_raw = (self.editor_user.value or "").strip()
        if editor_user_raw:
            match = re.match(r"<@!?(\\d+)>", editor_user_raw)
            if match:
                editor_user_id = int(match.group(1))
            elif editor_user_raw.isdigit():
                editor_user_id = int(editor_user_raw)
            else:
                await interaction.response.send_message(
                    "Editor user must be a user ID or @mention.", ephemeral=True
                )
                return
        if not interaction.guild.get_member(editor_user_id):
            await interaction.response.send_message(
                "Editor user not found in this server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        tasks = await self.cog.config.tasks()
        if len(tasks) < 24:
            await interaction.followup.send(
                "You need at least 24 tasks to generate a card.", ephemeral=True
            )
            return
        if not self.cog.template_path.exists():
            await interaction.followup.send(
                "Template image not found (card.jpg).", ephemeral=True
            )
            return
        if editor_user_id is None and editor_role is None:
            default_editor_role = await self.cog.config.guild(interaction.guild).editor_role_id()
            if not default_editor_role:
                await interaction.followup.send(
                    "Pick an editor user/role, or set a default with `!bingo editrole`.",
                    ephemeral=True,
                )
                return

        font_path = await self.cog.config.font_path()
        card_seed = seed or random.SystemRandom().randint(1, 2**32 - 1)
        rng = random.Random(card_seed)
        card_tasks = rng.sample(tasks, k=24)

        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = cog_data_path(self.cog) / "generated" / timestamp
        out_dir.mkdir(parents=True, exist_ok=True)
        output_path = out_dir / "bingo_card_01.png"
        self.cog._render_card(card_tasks, output_path, font_path, marked=set())

        card_entry = {
            "tasks": card_tasks,
            "marked": [],
            "seed": card_seed,
            "message_id": None,
            "editor_role_id": editor_role.id if editor_role else None,
            "editor_user_id": editor_user_id,
        }

        await self.cog._send_or_update_card(
            channel,
            card_entry,
            output_path,
            mention_role=editor_role,
            mention_user_id=editor_user_id,
        )

        cards = await self.cog.config.guild(interaction.guild).cards()
        cards[str(channel.id)] = card_entry
        await self.cog.config.guild(interaction.guild).cards.set(cards)

        await interaction.followup.send(
            f"Posted the card to {channel.mention}.",
            ephemeral=True,
        )
