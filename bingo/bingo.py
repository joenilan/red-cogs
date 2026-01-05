import datetime as dt
import random
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
        self.template_path = self._resolve_template_path()

    async def cog_load(self):
        tasks = await self.config.tasks()
        if not tasks:
            await self.config.tasks.set(DEFAULT_TASKS)

    @commands.group(name="bingo")
    @commands.guild_only()
    @commands.admin_or_permissions(administrator=True)
    async def bingo(self, ctx: commands.Context) -> None:
        """Bingo card generator."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

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
        if not self.template_path.exists():
            await ctx.send("Template image not found (card.jpg).")
            return

        tasks = await self.config.tasks()
        if len(tasks) < 24:
            await ctx.send("You need at least 24 tasks to generate a card.")
            return

        font_path = await self.config.font_path()
        if seed is None:
            seed = random.SystemRandom().randint(1, 2**32 - 1)

        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = cog_data_path(self) / "generated" / timestamp
        out_dir.mkdir(parents=True, exist_ok=True)

        output_files: List[Path] = []
        async with ctx.typing():
            for i in range(count):
                card_seed = seed + i
                rng = random.Random(card_seed)
                card_tasks = rng.sample(tasks, k=24)
                output_path = out_dir / f"bingo_card_{i + 1:02d}.png"
                self._render_card(card_tasks, output_path, font_path)
                output_files.append(output_path)

        if count == 1:
            await ctx.send(
                f"Generated card with seed {seed}.",
                file=discord.File(str(output_files[0]), filename=output_files[0].name),
            )
            return

        zip_path = out_dir / "bingo_cards.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in output_files:
                zf.write(path, arcname=path.name)

        zip_size = zip_path.stat().st_size
        max_upload = 8 * 1024 * 1024
        if zip_size > max_upload:
            await ctx.send(
                f"Generated {count} cards with base seed {seed}. "
                f"Zip is too large to upload ({zip_size / (1024 * 1024):.1f} MB). "
                f"Find files at: {zip_path}"
            )
            return

        await ctx.send(
            f"Generated {count} cards with base seed {seed}.",
            file=discord.File(str(zip_path), filename=zip_path.name),
        )

    def _render_card(self, tasks: List[str], output_path: Path, font_path: Optional[str]) -> None:
        base = Image.open(self.template_path).convert("RGBA")
        draw = ImageDraw.Draw(base)
        task_iter = iter(tasks)

        for row_idx, row in enumerate(CELL_ROWS):
            for col_idx, col in enumerate(CELL_COLS):
                if (row_idx, col_idx) == CENTER_CELL:
                    continue
                task = next(task_iter)
                box = self._cell_box(col, row)
                self._draw_wrapped_text(draw, task, box, font_path)

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
