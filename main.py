import asyncio
import calendar
import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

PLUGIN_NAME = "astrbot_plugin_deer_calendar"
PLUGIN_VERSION = "1.2.0"
DEER = "🦌"
DATA_VERSION = 1
DATA_FILE_NAME = "deer_calendar.json"

BACKGROUND = (248, 250, 252)
CARD = (255, 255, 255)
CARD_ALT = (241, 245, 249)
BORDER = (226, 232, 240)
PRIMARY = (22, 101, 52)
PRIMARY_LIGHT = (220, 252, 231)
TEXT = (15, 23, 42)
MUTED = (100, 116, 139)
WARNING = (180, 83, 9)
BAR = (34, 197, 94)

HELP_TEXT = """🎮 鹿管日记命令列表

🦌帮助
显示命令使用方法

🦌 / 🦌🦌 ...
执行每日打卡，鹿的数量会累加到当天记录中

🦌日历
查询自己本月的打卡日历，不记录打卡

🦌年历 / 🦌年历 2024
查看本年度或指定年份的完整打卡日历

🦌报告 / 🦌报告 11 / 🦌报告 2025
分析本月、指定月份或指定年份的打卡数据

🦌月历 11
查看当前年份指定月份的打卡日历

🦌排行
查看当前平台本月打卡排行榜前20名

🦌生涯
生成个人生涯数据分析报告

🦌补签 1 18 / 🦌补签 1
给当月某天补签指定次数，默认1次

🦌撤销 1 1 / 🦌撤销 1
撤销当月某天指定次数，默认1次"""

DEFAULT_REPORT_ANALYSIS_PROMPT = """你是一个群聊打卡数据分析助手。请根据下面的 🦌 打卡统计，为用户生成一段 60 字以内、轻松但不夸张的中文分析。不要输出标题，不要使用列表。

用户：{user_name}
周期：{period_label}
统计：
{stats_text}"""


@dataclass(frozen=True, slots=True)
class DeerCommand:
    """Parsed deer command data.

    Args:
        kind: Command kind used by the event handler.
        amount: Deer count delta for check-in, makeup, or revoke commands.
        day: Day of month for makeup or revoke commands.
        month: Month number for month calendar or monthly report commands.
        year: Year number for yearly calendar or yearly report commands.
        error: User-facing error text for invalid commands.
    """

    kind: str
    amount: int = 0
    day: int | None = None
    month: int | None = None
    year: int | None = None
    error: str = ""


_FONT_CACHE: dict[tuple[int, bool], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}
_EMOJI_FONT_CACHE: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont | None] = {}


def parse_deer_command(message: str) -> DeerCommand | None:
    """Parse a raw message into a deer command.

    Args:
        message: Plain message text from AstrBot.

    Returns:
        A parsed command, an error command, or None when the message is not
        handled by this plugin.
    """
    text = message.strip()
    if not text.startswith(DEER):
        return None

    if re.fullmatch(f"{DEER}+", text):
        return DeerCommand(kind="checkin", amount=text.count(DEER))

    body = text[len(DEER) :].strip()
    if body == "帮助":
        return DeerCommand(kind="help")
    if body == "日历":
        return DeerCommand(kind="month_calendar")
    if body == "排行":
        return DeerCommand(kind="ranking")
    if body == "生涯":
        return DeerCommand(kind="career")

    year_match = re.fullmatch(r"年历(?:\s+(\d{4}))?", body)
    if year_match:
        year = int(year_match.group(1)) if year_match.group(1) else None
        return DeerCommand(kind="year_calendar", year=year)

    month_match = re.fullmatch(r"月历\s*(\d{1,2})", body)
    if month_match:
        month = int(month_match.group(1))
        if 1 <= month <= 12:
            return DeerCommand(kind="specific_month_calendar", month=month)
        return DeerCommand(kind="error", error="月份必须是 1 到 12。")

    report_match = re.fullmatch(r"报告(?:\s+(\d{1,4}))?", body)
    if report_match:
        value = report_match.group(1)
        if value is None:
            return DeerCommand(kind="month_report")
        number = int(value)
        if 1 <= number <= 12:
            return DeerCommand(kind="month_report", month=number)
        if 1000 <= number <= 9999:
            return DeerCommand(kind="year_report", year=number)
        return DeerCommand(kind="error", error="报告参数必须是月份或四位年份。")

    makeup_match = re.fullmatch(r"补签\s*(\d{1,2})(?:\s+(\d+))?", body)
    if makeup_match:
        return _parse_day_amount_command("makeup", makeup_match)

    revoke_match = re.fullmatch(r"撤销\s*(\d{1,2})(?:\s+(\d+))?", body)
    if revoke_match:
        return _parse_day_amount_command("revoke", revoke_match)

    return DeerCommand(kind="error", error="未知命令，请发送 🦌帮助 查看用法。")


def _parse_day_amount_command(kind: str, match: re.Match[str]) -> DeerCommand:
    """Parse commands that target one day in the current month.

    Args:
        kind: Parsed command kind.
        match: Regex match with day and optional amount groups.

    Returns:
        A parsed command or an error command.
    """
    day = int(match.group(1))
    amount = int(match.group(2)) if match.group(2) else 1
    if not 1 <= day <= 31:
        return DeerCommand(kind="error", error="日期必须是当月有效日期。")
    if amount <= 0:
        return DeerCommand(kind="error", error="次数必须大于 0。")
    return DeerCommand(kind=kind, day=day, amount=amount)


def _empty_data() -> dict[str, Any]:
    """Create an empty storage document.

    Returns:
        Empty plugin storage data.
    """
    return {"version": DATA_VERSION, "users": {}}


def _normalize_data(data: Any) -> dict[str, Any]:
    """Normalize storage data loaded from JSON.

    Args:
        data: Raw decoded JSON data.

    Returns:
        A storage dict with required top-level keys.
    """
    if not isinstance(data, dict):
        return _empty_data()

    users = data.get("users")
    if not isinstance(users, dict):
        users = {}

    normalized = {"version": DATA_VERSION, "users": users}
    for user in users.values():
        if not isinstance(user, dict):
            continue
        if not isinstance(user.get("records"), dict):
            user["records"] = {}
        if not isinstance(user.get("name"), str):
            user["name"] = "匿名用户"
    return normalized


def _records_as_dates(records: dict[str, Any]) -> dict[date, int]:
    """Convert persisted record keys into date objects.

    Args:
        records: Mapping of ISO date strings to counts.

    Returns:
        Mapping of date objects to positive counts.
    """
    parsed: dict[date, int] = {}
    for day_text, raw_count in records.items():
        try:
            day = date.fromisoformat(day_text)
            count = int(raw_count)
        except (TypeError, ValueError):
            continue
        if count > 0:
            parsed[day] = count
    return parsed


def _period_records(
    records: dict[str, Any],
    start: date,
    end: date,
) -> dict[date, int]:
    """Filter records to an inclusive date range.

    Args:
        records: Mapping of ISO date strings to counts.
        start: Inclusive start date.
        end: Inclusive end date.

    Returns:
        Filtered date/count mapping.
    """
    parsed = _records_as_dates(records)
    return {day: count for day, count in parsed.items() if start <= day <= end}


def _month_record_counts(
    records: dict[str, Any],
    year: int,
    month: int,
) -> dict[date, int]:
    """Get records for one month.

    Args:
        records: Mapping of ISO date strings to counts.
        year: Target year.
        month: Target month.

    Returns:
        Date/count mapping for the target month.
    """
    last_day = calendar.monthrange(year, month)[1]
    return _period_records(records, date(year, month, 1), date(year, month, last_day))


def _year_record_counts(records: dict[str, Any], year: int) -> dict[date, int]:
    """Get records for one year.

    Args:
        records: Mapping of ISO date strings to counts.
        year: Target year.

    Returns:
        Date/count mapping for the target year.
    """
    return _period_records(records, date(year, 1, 1), date(year, 12, 31))


def _longest_streak(days: set[date]) -> int:
    """Calculate the longest consecutive active-day streak.

    Args:
        days: Active dates.

    Returns:
        Longest streak length.
    """
    longest = 0
    current = 0
    previous: date | None = None
    for day in sorted(days):
        if previous and day == previous + timedelta(days=1):
            current += 1
        else:
            current = 1
        previous = day
        longest = max(longest, current)
    return longest


def _current_streak(days: set[date], today: date) -> int:
    """Calculate the streak ending today.

    Args:
        days: Active dates.
        today: Current local date.

    Returns:
        Current streak length, or zero when today is not active.
    """
    if today not in days:
        return 0

    count = 0
    cursor = today
    while cursor in days:
        count += 1
        cursor -= timedelta(days=1)
    return count


def _best_month(records: dict[str, Any]) -> tuple[str, int]:
    """Find the best month across all records.

    Args:
        records: Mapping of ISO date strings to counts.

    Returns:
        A tuple of YYYY-MM and total count. Empty data returns ("暂无", 0).
    """
    totals: Counter[str] = Counter()
    for day, count in _records_as_dates(records).items():
        totals[f"{day.year:04d}-{day.month:02d}"] += count
    if not totals:
        return "暂无", 0
    return totals.most_common(1)[0]


def _career_title(total: int, active_days: int, longest_streak: int) -> str:
    """Create a title from career statistics.

    Args:
        total: Total deer count.
        active_days: Active day count.
        longest_streak: Longest active-day streak.

    Returns:
        User-facing career title.
    """
    if total >= 1000 or longest_streak >= 100:
        return "🦌神"
    if total >= 500 or active_days >= 120:
        return "🦌王"
    if total >= 100 or longest_streak >= 30:
        return "🦌管大师"
    if total >= 30 or active_days >= 15:
        return "🦌管熟手"
    return "新晋🦌友"


def _short_name(name: str, limit: int = 14) -> str:
    """Shorten a display name for image titles.

    Args:
        name: Original display name.
        limit: Maximum visible characters before ellipsis.

    Returns:
        A shortened display name.
    """
    return f"{name[:limit]}..." if len(name) > limit else name


def _get_font(
    size: int, bold: bool = False
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load and cache a CJK-capable font.

    Args:
        size: Font size.
        bold: Whether a bold font is preferred.

    Returns:
        A PIL font object.

    Raises:
        RuntimeError: If no font can be loaded.
    """
    cache_key = (size, bold)
    if cache_key in _FONT_CACHE:
        return _FONT_CACHE[cache_key]

    candidates: list[str] = []
    data_font = Path(get_astrbot_data_path()) / "font.ttf"
    if data_font.exists():
        candidates.append(str(data_font))

    candidates.extend(
        [
            "msyhbd.ttc" if bold else "msyh.ttc",
            "NotoSansCJK-Bold.ttc" if bold else "NotoSansCJK-Regular.ttc",
            "PingFang.ttc",
            "SimHei.ttf",
            "Arial Bold.ttf" if bold else "Arial.ttf",
            "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        ],
    )

    for font_name in candidates:
        try:
            font = ImageFont.truetype(font_name, size)
        except OSError:
            continue
        _FONT_CACHE[cache_key] = font
        return font

    try:
        return ImageFont.load_default()
    except OSError as exc:
        raise RuntimeError("Unable to load a usable font") from exc


def _get_emoji_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont | None:
    """Load and cache an emoji-capable font.

    Args:
        size: Font size.

    Returns:
        An emoji font, or None when unavailable.
    """
    if size in _EMOJI_FONT_CACHE:
        return _EMOJI_FONT_CACHE[size]

    candidates = [
        "C:/Windows/Fonts/seguiemj.ttf",
        "seguiemj.ttf",
        "Segoe UI Emoji.ttf",
        "NotoColorEmoji.ttf",
        "NotoEmoji-Regular.ttf",
        "Apple Color Emoji.ttc",
    ]
    for font_name in candidates:
        try:
            font = ImageFont.truetype(font_name, size)
        except OSError:
            continue
        _EMOJI_FONT_CACHE[size] = font
        return font

    _EMOJI_FONT_CACHE[size] = None
    return None


def _font_size(font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> int:
    """Best-effort font size lookup.

    Args:
        font: PIL font object.

    Returns:
        Font size in pixels.
    """
    size = getattr(font, "size", None)
    return int(size) if isinstance(size, int | float) else 20


def _plain_text_size(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    embedded_color: bool = False,
) -> tuple[int, int]:
    """Measure text with one concrete font.

    Args:
        draw: PIL drawing context.
        text: Text to measure.
        font: Font used for the text.
        embedded_color: Whether embedded color glyphs should be considered.

    Returns:
        Width and height in pixels.
    """
    try:
        box = draw.textbbox((0, 0), text, font=font, embedded_color=embedded_color)
    except TypeError:
        box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def _deer_icon_size(font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> int:
    """Return fallback deer icon size for a text line.

    Args:
        font: Current text font.

    Returns:
        Icon size in pixels.
    """
    return max(16, int(_font_size(font) * 0.95))


def _draw_deer_icon(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> int:
    """Draw a small fallback deer icon when emoji fonts are unavailable.

    Args:
        draw: PIL drawing context.
        xy: Top-left icon position.
        font: Current text font.

    Returns:
        Drawn icon width.
    """
    size = _deer_icon_size(font)
    x = int(xy[0])
    y = int(xy[1])
    head = (146, 91, 45)
    antler = (120, 83, 42)
    ear = (180, 126, 68)
    eye = (30, 41, 59)
    snout = (245, 222, 179)

    draw.line(
        (x + size * 0.28, y + size * 0.20, x + size * 0.12, y), fill=antler, width=2
    )
    draw.line(
        (x + size * 0.18, y + size * 0.08, x + size * 0.06, y + size * 0.06),
        fill=antler,
        width=2,
    )
    draw.line(
        (x + size * 0.72, y + size * 0.20, x + size * 0.88, y), fill=antler, width=2
    )
    draw.line(
        (x + size * 0.82, y + size * 0.08, x + size * 0.94, y + size * 0.06),
        fill=antler,
        width=2,
    )
    draw.polygon(
        [
            (x + size * 0.18, y + size * 0.32),
            (x + size * 0.02, y + size * 0.45),
            (x + size * 0.26, y + size * 0.52),
        ],
        fill=ear,
    )
    draw.polygon(
        [
            (x + size * 0.82, y + size * 0.32),
            (x + size * 0.98, y + size * 0.45),
            (x + size * 0.74, y + size * 0.52),
        ],
        fill=ear,
    )
    draw.ellipse(
        (x + size * 0.18, y + size * 0.20, x + size * 0.82, y + size * 0.92),
        fill=head,
    )
    draw.ellipse(
        (x + size * 0.35, y + size * 0.58, x + size * 0.65, y + size * 0.90),
        fill=snout,
    )
    dot = max(2, int(size * 0.09))
    draw.ellipse(
        (
            x + size * 0.34,
            y + size * 0.44,
            x + size * 0.34 + dot,
            y + size * 0.44 + dot,
        ),
        fill=eye,
    )
    draw.ellipse(
        (
            x + size * 0.58,
            y + size * 0.44,
            x + size * 0.58 + dot,
            y + size * 0.44 + dot,
        ),
        fill=eye,
    )
    draw.ellipse(
        (x + size * 0.45, y + size * 0.68, x + size * 0.55, y + size * 0.78),
        fill=eye,
    )
    return size + 2


def _text_size(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> tuple[int, int]:
    """Measure text size.

    Args:
        draw: PIL drawing context.
        text: Text to measure.
        font: Font used for the text.

    Returns:
        Width and height in pixels.
    """
    if DEER not in text:
        return _plain_text_size(draw, text, font)

    emoji_font = _get_emoji_font(_font_size(font))
    width = 0
    height = 0
    for segment in re.split(f"({re.escape(DEER)})", text):
        if not segment:
            continue
        if segment == DEER:
            if emoji_font:
                segment_width, segment_height = _plain_text_size(
                    draw,
                    segment,
                    emoji_font,
                    embedded_color=True,
                )
            else:
                segment_width = _deer_icon_size(font) + 2
                segment_height = _deer_icon_size(font)
        else:
            segment_width, segment_height = _plain_text_size(draw, segment, font)
        width += segment_width
        height = max(height, segment_height)
    return width, height


def _draw_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: tuple[int, int, int],
) -> None:
    """Draw text with explicit deer emoji fallback.

    Args:
        draw: PIL drawing context.
        xy: Top-left text position.
        text: Text to draw.
        font: Font used for normal text.
        fill: Normal text color.
    """
    if DEER not in text:
        draw.text(xy, text, font=font, fill=fill)
        return

    emoji_font = _get_emoji_font(_font_size(font))
    x = float(xy[0])
    y = float(xy[1])
    for segment in re.split(f"({re.escape(DEER)})", text):
        if not segment:
            continue
        if segment == DEER:
            if emoji_font:
                try:
                    draw.text((x, y), segment, font=emoji_font, embedded_color=True)
                except TypeError:
                    draw.text((x, y), segment, font=emoji_font, fill=fill)
                advance, _ = _plain_text_size(
                    draw,
                    segment,
                    emoji_font,
                    embedded_color=True,
                )
            else:
                advance = _draw_deer_icon(draw, (x, y), font)
        else:
            draw.text((x, y), segment, font=font, fill=fill)
            advance, _ = _plain_text_size(draw, segment, font)
        x += advance


def _draw_centered_text(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: tuple[int, int, int],
) -> None:
    """Draw text centered inside a rectangle.

    Args:
        draw: PIL drawing context.
        box: Target rectangle.
        text: Text to draw.
        font: Font used for the text.
        fill: Text color.
    """
    width, height = _text_size(draw, text, font)
    x = box[0] + (box[2] - box[0] - width) / 2
    y = box[1] + (box[3] - box[1] - height) / 2 - 2
    _draw_text(draw, (x, y), text, font=font, fill=fill)


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    """Wrap text by rendered width.

    Args:
        draw: PIL drawing context.
        text: Text to wrap.
        font: Font used for measuring.
        max_width: Maximum line width.
        max_lines: Maximum line count.

    Returns:
        Wrapped lines, truncated with ellipsis when needed.
    """
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []

    lines: list[str] = []
    current = ""
    for char in normalized:
        candidate = f"{current}{char}"
        width, _ = _text_size(draw, candidate, font)
        if width <= max_width or not current:
            current = candidate
            continue

        lines.append(current)
        current = char
        if len(lines) >= max_lines:
            break

    if len(lines) < max_lines and current:
        lines.append(current)

    if len(lines) > max_lines:
        lines = lines[:max_lines]
    if lines and len(lines) == max_lines:
        while lines[-1] and _text_size(draw, f"{lines[-1]}...", font)[0] > max_width:
            lines[-1] = lines[-1][:-1]
        if lines[-1] and len("".join(lines)) < len(normalized):
            lines[-1] = f"{lines[-1]}..."
    return lines


def _draw_wrapped_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: tuple[int, int, int],
    max_width: int,
    max_lines: int,
    line_height: int,
) -> None:
    """Draw wrapped text.

    Args:
        draw: PIL drawing context.
        xy: Top-left text position.
        text: Text to draw.
        font: Font used for text.
        fill: Text color.
        max_width: Maximum line width.
        max_lines: Maximum line count.
        line_height: Line height in pixels.
    """
    for index, line in enumerate(_wrap_text(draw, text, font, max_width, max_lines)):
        _draw_text(draw, (xy[0], xy[1] + index * line_height), line, font, fill)


def _draw_card(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    fill: tuple[int, int, int] = CARD,
    outline: tuple[int, int, int] = BORDER,
    radius: int = 18,
    width: int = 1,
) -> None:
    """Draw a rounded card rectangle.

    Args:
        draw: PIL drawing context.
        box: Target rectangle.
        fill: Fill color.
        outline: Border color.
        radius: Corner radius.
        width: Border width.
    """
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _heat_color(count: int, max_count: int) -> tuple[int, int, int]:
    """Return a green heat-map color for a count.

    Args:
        count: Day count.
        max_count: Maximum count in the period.

    Returns:
        RGB fill color.
    """
    if count <= 0:
        return CARD
    ratio = count / max(max_count, 1)
    palette = [
        (220, 252, 231),
        (187, 247, 208),
        (134, 239, 172),
        (74, 222, 128),
        (34, 197, 94),
    ]
    index = min(len(palette) - 1, math.ceil(ratio * len(palette)) - 1)
    return palette[index]


def _draw_stat_card(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    label: str,
    value: str,
) -> None:
    """Draw a small statistic card.

    Args:
        draw: PIL drawing context.
        box: Target card rectangle.
        label: Statistic label.
        value: Statistic value.
    """
    _draw_card(draw, box)
    _draw_text(draw, (box[0] + 18, box[1] + 14), label, _get_font(19), MUTED)
    _draw_text(draw, (box[0] + 18, box[1] + 42), value, _get_font(30, True), TEXT)


def _save_image(image: Image.Image, output_path: Path) -> Path:
    """Save an image as PNG.

    Args:
        image: PIL image to save.
        output_path: Target path.

    Returns:
        The target path.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG")
    return output_path


def render_month_calendar(
    output_path: Path,
    user_name: str,
    records: dict[str, Any],
    year: int,
    month: int,
    today: date,
) -> Path:
    """Render a monthly calendar image.

    Args:
        output_path: Target PNG path.
        user_name: Display name.
        records: User records.
        year: Target year.
        month: Target month.
        today: Current local date.

    Returns:
        The saved PNG path.
    """
    counts = _month_record_counts(records, year, month)
    weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(year, month)
    max_count = max(counts.values(), default=0)
    total = sum(counts.values())
    active_days = len(counts)
    best_day, best_count = ("暂无", 0)
    if counts:
        day, best_count = max(counts.items(), key=lambda item: item[1])
        best_day = f"{day.day}日"

    width = 1100
    margin = 52
    grid_top = 258
    cell_width = (width - margin * 2) // 7
    cell_height = 92
    height = grid_top + len(weeks) * cell_height + 72
    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    title_name = _short_name(user_name)

    _draw_text(
        draw,
        (margin, 42),
        f"{title_name} 的 {year}年{month}月🦌管日历",
        _get_font(42, True),
        TEXT,
    )
    _draw_text(
        draw,
        (margin, 96),
        "周一开始 · 数据按平台用户合并",
        _get_font(22),
        MUTED,
    )

    card_width = (width - margin * 2 - 36) // 4
    stats = [
        ("本月总数", str(total)),
        ("打卡天数", f"{active_days} 天"),
        ("最高单日", f"{best_day} / {best_count}"),
        ("日均", f"{total / max(active_days, 1):.1f}"),
    ]
    for index, (label, value) in enumerate(stats):
        x = margin + index * (card_width + 12)
        _draw_stat_card(draw, (x, 142, x + card_width, 222), label, value)

    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    for index, weekday in enumerate(weekdays):
        x = margin + index * cell_width
        _draw_centered_text(
            draw,
            (x, grid_top - 36, x + cell_width, grid_top - 8),
            weekday,
            _get_font(20, True),
            MUTED,
        )

    for row, week in enumerate(weeks):
        for col, day in enumerate(week):
            count = counts.get(day, 0)
            x = margin + col * cell_width
            y = grid_top + row * cell_height
            box = (x + 6, y + 5, x + cell_width - 6, y + cell_height - 7)
            in_month = day.month == month
            fill = _heat_color(count, max_count) if in_month else CARD_ALT
            outline = PRIMARY if day == today else BORDER
            outline_width = 3 if day == today else 1
            _draw_card(
                draw, box, fill=fill, outline=outline, radius=14, width=outline_width
            )

            day_color = TEXT if in_month else MUTED
            _draw_text(
                draw,
                (box[0] + 14, box[1] + 10),
                str(day.day),
                _get_font(20, True),
                day_color,
            )
            if in_month and count > 0:
                _draw_centered_text(
                    draw,
                    (box[0], box[1] + 34, box[2], box[3] - 8),
                    f"🦌 x{count}",
                    _get_font(24, True),
                    PRIMARY,
                )

    return _save_image(image, output_path)


def render_year_calendar(
    output_path: Path,
    user_name: str,
    records: dict[str, Any],
    year: int,
    max_month: int,
    today: date,
) -> Path:
    """Render a yearly calendar image.

    Args:
        output_path: Target PNG path.
        user_name: Display name.
        records: User records.
        year: Target year.
        max_month: Last month to include.
        today: Current local date.

    Returns:
        The saved PNG path.
    """
    months = list(range(1, max_month + 1))
    cols = 3
    block_width = 330
    block_height = 252
    gap = 24
    margin = 44
    header_height = 144
    rows = math.ceil(len(months) / cols)
    width = margin * 2 + cols * block_width + (cols - 1) * gap
    height = header_height + rows * block_height + max(rows - 1, 0) * gap + 50
    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    title_name = _short_name(user_name)

    year_counts = _year_record_counts(records, year)
    total = sum(count for day, count in year_counts.items() if day.month <= max_month)
    active_days = len([day for day in year_counts if day.month <= max_month])
    max_count = max(year_counts.values(), default=0)

    _draw_text(
        draw,
        (margin, 36),
        f"{title_name} 的 {year}年🦌管年历",
        _get_font(40, True),
        TEXT,
    )
    _draw_text(
        draw,
        (margin, 90),
        f"已显示 1-{max_month} 月 · 总数 {total} · 打卡 {active_days} 天",
        _get_font(22),
        MUTED,
    )

    for index, month in enumerate(months):
        row = index // cols
        col = index % cols
        left = margin + col * (block_width + gap)
        top = header_height + row * (block_height + gap)
        _draw_card(draw, (left, top, left + block_width, top + block_height), radius=16)
        month_counts = _month_record_counts(records, year, month)
        month_total = sum(month_counts.values())
        _draw_text(
            draw,
            (left + 18, top + 14),
            f"{month}月",
            _get_font(26, True),
            TEXT,
        )
        _draw_text(
            draw,
            (left + 102, top + 20),
            f"总数 {month_total}",
            _get_font(18),
            MUTED,
        )

        weekdays = ["一", "二", "三", "四", "五", "六", "日"]
        cell = 38
        grid_left = left + 18
        grid_top = top + 58
        for weekday_index, weekday in enumerate(weekdays):
            _draw_centered_text(
                draw,
                (
                    grid_left + weekday_index * cell,
                    grid_top,
                    grid_left + (weekday_index + 1) * cell,
                    grid_top + 22,
                ),
                weekday,
                _get_font(14, True),
                MUTED,
            )

        for week_index, week in enumerate(
            calendar.Calendar(firstweekday=0).monthdatescalendar(year, month),
        ):
            for day_index, day in enumerate(week):
                count = month_counts.get(day, 0)
                x = grid_left + day_index * cell
                y = grid_top + 28 + week_index * 27
                in_month = day.month == month
                fill = _heat_color(count, max_count) if in_month else CARD_ALT
                outline = PRIMARY if day == today else BORDER
                _draw_card(draw, (x + 3, y, x + cell - 3, y + 23), fill=fill, radius=7)
                if outline == PRIMARY:
                    draw.rounded_rectangle(
                        (x + 3, y, x + cell - 3, y + 23),
                        radius=7,
                        outline=outline,
                        width=2,
                    )
                _draw_centered_text(
                    draw,
                    (x + 3, y, x + cell - 3, y + 23),
                    str(day.day),
                    _get_font(12, True),
                    TEXT if in_month else MUTED,
                )

    return _save_image(image, output_path)


def render_month_report(
    output_path: Path,
    user_name: str,
    records: dict[str, Any],
    year: int,
    month: int,
    today: date,
    analysis_text: str = "",
) -> Path:
    """Render a monthly report image.

    Args:
        output_path: Target PNG path.
        user_name: Display name.
        records: User records.
        year: Target year.
        month: Target month.
        today: Current local date.
        analysis_text: Optional LLM analysis text.

    Returns:
        The saved PNG path.
    """
    counts = _month_record_counts(records, year, month)
    total = sum(counts.values())
    active_days = len(counts)
    longest = _longest_streak(set(counts))
    current = _current_streak(set(counts), today)
    days_in_month = calendar.monthrange(year, month)[1]
    max_day = max(counts.items(), key=lambda item: item[1], default=(None, 0))

    width = 1000
    height = 920
    margin = 54
    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    title_name = _short_name(user_name)

    _draw_text(
        draw,
        (margin, 38),
        f"{title_name} 的 {year}年{month}月🦌管报告",
        _get_font(42, True),
        TEXT,
    )
    _draw_text(draw, (margin, 92), "月度统计与模型分析", _get_font(22), MUTED)

    stats = [
        ("本月总数", str(total)),
        ("打卡天数", f"{active_days}/{days_in_month}"),
        ("最长连续", f"{longest} 天"),
        ("当前连续", f"{current} 天"),
        (
            "最高单日",
            "暂无" if max_day[0] is None else f"{max_day[0].day}日 / {max_day[1]}",
        ),
        ("活跃日均", f"{total / max(active_days, 1):.1f}"),
    ]
    card_width = (width - margin * 2 - 24) // 3
    for index, (label, value) in enumerate(stats):
        row = index // 3
        col = index % 3
        x = margin + col * (card_width + 12)
        y = 140 + row * 92
        _draw_stat_card(draw, (x, y, x + card_width, y + 78), label, value)

    _draw_text(draw, (margin, 340), "周维度统计", _get_font(28, True), TEXT)
    weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(year, month)
    weekly_totals = []
    for week in weeks:
        weekly_totals.append(
            sum(counts.get(day, 0) for day in week if day.month == month)
        )
    max_week = max(weekly_totals, default=0)
    bar_left = margin
    bar_top = 392
    bar_width = width - margin * 2
    for index, total_count in enumerate(weekly_totals):
        y = bar_top + index * 48
        _draw_text(draw, (bar_left, y + 6), f"第{index + 1}周", _get_font(20), MUTED)
        fill_width = int((bar_width - 150) * total_count / max(max_week, 1))
        draw.rounded_rectangle(
            (bar_left + 92, y + 8, bar_left + bar_width - 58, y + 30),
            radius=11,
            fill=BORDER,
        )
        draw.rounded_rectangle(
            (bar_left + 92, y + 8, bar_left + 92 + fill_width, y + 30),
            radius=11,
            fill=BAR,
        )
        _draw_text(
            draw,
            (bar_left + bar_width - 42, y + 5),
            str(total_count),
            _get_font(20, True),
            TEXT,
        )

    analysis_top = 700
    _draw_card(draw, (margin, analysis_top, width - margin, analysis_top + 118))
    _draw_text(
        draw,
        (margin + 24, analysis_top + 18),
        "模型分析",
        _get_font(22, True),
        TEXT,
    )
    analysis = analysis_text or "模型分析未启用，当前仅展示基础统计。"
    _draw_wrapped_text(
        draw,
        (margin + 24, analysis_top + 52),
        analysis,
        _get_font(21),
        MUTED,
        width - margin * 2 - 48,
        3,
        28,
    )

    summary = (
        "本月还没有打卡记录。" if total == 0 else "本月保持记录，继续积累稳定节奏。"
    )
    _draw_card(
        draw, (margin, height - 86, width - margin, height - 30), fill=PRIMARY_LIGHT
    )
    _draw_text(draw, (margin + 24, height - 70), summary, _get_font(24, True), PRIMARY)

    return _save_image(image, output_path)


def render_year_report(
    output_path: Path,
    user_name: str,
    records: dict[str, Any],
    year: int,
    today: date,
    analysis_text: str = "",
) -> Path:
    """Render a yearly report image.

    Args:
        output_path: Target PNG path.
        user_name: Display name.
        records: User records.
        year: Target year.
        today: Current local date.
        analysis_text: Optional LLM analysis text.

    Returns:
        The saved PNG path.
    """
    counts = _year_record_counts(records, year)
    total = sum(counts.values())
    active_days = len(counts)
    longest = _longest_streak(set(counts))
    current = _current_streak(set(counts), today) if today.year == year else 0
    monthly_totals = [
        sum(_month_record_counts(records, year, month).values())
        for month in range(1, 13)
    ]
    best_index = max(range(12), key=lambda index: monthly_totals[index])
    best_value = monthly_totals[best_index]

    width = 1000
    height = 900
    margin = 54
    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    title_name = _short_name(user_name)

    _draw_text(
        draw,
        (margin, 38),
        f"{title_name} 的 {year}年🦌管报告",
        _get_font(42, True),
        TEXT,
    )
    _draw_text(draw, (margin, 92), "年度统计与模型分析", _get_font(22), MUTED)

    stats = [
        ("年度总数", str(total)),
        ("打卡天数", f"{active_days} 天"),
        ("最长连续", f"{longest} 天"),
        ("当前连续", f"{current} 天"),
        ("最佳月份", f"{best_index + 1}月 / {best_value}"),
        ("月均", f"{total / 12:.1f}"),
    ]
    card_width = (width - margin * 2 - 24) // 3
    for index, (label, value) in enumerate(stats):
        row = index // 3
        col = index % 3
        x = margin + col * (card_width + 12)
        y = 140 + row * 92
        _draw_stat_card(draw, (x, y, x + card_width, y + 78), label, value)

    _draw_text(draw, (margin, 340), "月度趋势", _get_font(28, True), TEXT)
    chart_left = margin
    chart_top = 398
    chart_width = width - margin * 2
    chart_height = 300
    max_total = max(monthly_totals, default=0)
    bar_gap = 12
    bar_width = (chart_width - bar_gap * 11) // 12
    for index, month_total in enumerate(monthly_totals):
        x = chart_left + index * (bar_width + bar_gap)
        bar_height = int((chart_height - 48) * month_total / max(max_total, 1))
        y = chart_top + chart_height - 34 - bar_height
        draw.rounded_rectangle(
            (x, chart_top + 28, x + bar_width, chart_top + chart_height - 34),
            radius=10,
            fill=BORDER,
        )
        draw.rounded_rectangle(
            (x, y, x + bar_width, chart_top + chart_height - 34),
            radius=10,
            fill=BAR if month_total else BORDER,
        )
        _draw_centered_text(
            draw,
            (x, chart_top + chart_height - 26, x + bar_width, chart_top + chart_height),
            str(index + 1),
            _get_font(16, True),
            MUTED,
        )
        if month_total:
            _draw_centered_text(
                draw,
                (x - 8, y - 26, x + bar_width + 8, y - 2),
                str(month_total),
                _get_font(16, True),
                TEXT,
            )

    analysis_top = 730
    _draw_card(draw, (margin, analysis_top, width - margin, analysis_top + 120))
    _draw_text(
        draw,
        (margin + 24, analysis_top + 18),
        "模型分析",
        _get_font(22, True),
        TEXT,
    )
    analysis = analysis_text or "模型分析未启用，当前仅展示基础统计。"
    _draw_wrapped_text(
        draw,
        (margin + 24, analysis_top + 52),
        analysis,
        _get_font(21),
        MUTED,
        width - margin * 2 - 48,
        3,
        28,
    )

    return _save_image(image, output_path)


def render_ranking(
    output_path: Path,
    ranking: list[tuple[str, int, int]],
    platform_id: str,
    year: int,
    month: int,
    requester_name: str,
) -> Path:
    """Render a monthly ranking image.

    Args:
        output_path: Target PNG path.
        ranking: List of display name, total count, and active days.
        platform_id: Current platform identifier.
        year: Target year.
        month: Target month.
        requester_name: Name of the user who triggered the command.

    Returns:
        The saved PNG path.
    """
    row_count = max(len(ranking), 1)
    width = 960
    margin = 52
    row_height = 52
    height = 180 + row_count * row_height + 58
    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    title_name = _short_name(requester_name)

    _draw_text(
        draw,
        (margin, 38),
        f"{year}年{month}月🦌管排行",
        _get_font(42, True),
        TEXT,
    )
    _draw_text(
        draw,
        (margin, 92),
        f"平台 {platform_id} · 前20名 · 查询用户：{title_name}",
        _get_font(22),
        MUTED,
    )

    top = 150
    _draw_card(draw, (margin, top, width - margin, height - 40), radius=18)
    header_y = top + 20
    _draw_text(draw, (margin + 24, header_y), "排名", _get_font(20, True), MUTED)
    _draw_text(draw, (margin + 112, header_y), "用户", _get_font(20, True), MUTED)
    _draw_text(
        draw, (width - margin - 220, header_y), "总数", _get_font(20, True), MUTED
    )
    _draw_text(
        draw, (width - margin - 110, header_y), "天数", _get_font(20, True), MUTED
    )

    if not ranking:
        _draw_centered_text(
            draw,
            (margin, top + 62, width - margin, height - 48),
            "本月暂无打卡记录",
            _get_font(26, True),
            MUTED,
        )
        return _save_image(image, output_path)

    for index, (name, total, days) in enumerate(ranking):
        y = top + 58 + index * row_height
        if index % 2 == 0:
            draw.rounded_rectangle(
                (margin + 14, y - 4, width - margin - 14, y + row_height - 10),
                radius=12,
                fill=CARD_ALT,
            )
        rank_color = WARNING if index < 3 else TEXT
        _draw_text(
            draw,
            (margin + 28, y + 6),
            f"#{index + 1}",
            _get_font(22, True),
            rank_color,
        )
        display_name = name[:16] + "..." if len(name) > 16 else name
        _draw_text(draw, (margin + 112, y + 7), display_name, _get_font(22), TEXT)
        _draw_text(
            draw,
            (width - margin - 220, y + 7),
            str(total),
            _get_font(22, True),
            PRIMARY,
        )
        _draw_text(
            draw,
            (width - margin - 110, y + 7),
            str(days),
            _get_font(22),
            MUTED,
        )

    return _save_image(image, output_path)


def render_career_report(
    output_path: Path,
    user_name: str,
    records: dict[str, Any],
    today: date,
) -> Path:
    """Render a career report image.

    Args:
        output_path: Target PNG path.
        user_name: Display name.
        records: User records.
        today: Current local date.

    Returns:
        The saved PNG path.
    """
    counts = _records_as_dates(records)
    total = sum(counts.values())
    active_days = len(counts)
    longest = _longest_streak(set(counts))
    current = _current_streak(set(counts), today)
    best_day, best_day_count = ("暂无", 0)
    if counts:
        day, best_day_count = max(counts.items(), key=lambda item: item[1])
        best_day = day.isoformat()
    best_month, best_month_total = _best_month(records)
    title = _career_title(total, active_days, longest)

    width = 1000
    height = 760
    margin = 54
    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    title_name = _short_name(user_name)

    _draw_text(
        draw,
        (margin, 38),
        f"{title_name} 的 🦌管生涯报告",
        _get_font(42, True),
        TEXT,
    )
    _draw_text(draw, (margin, 92), f"称号：{title}", _get_font(24), PRIMARY)

    stats = [
        ("生涯总数", str(total)),
        ("打卡天数", f"{active_days} 天"),
        ("最长连续", f"{longest} 天"),
        ("当前连续", f"{current} 天"),
        ("最高单日", f"{best_day} / {best_day_count}"),
        ("最佳月份", f"{best_month} / {best_month_total}"),
    ]
    card_width = (width - margin * 2 - 24) // 3
    for index, (label, value) in enumerate(stats):
        row = index // 3
        col = index % 3
        x = margin + col * (card_width + 12)
        y = 148 + row * 92
        _draw_stat_card(draw, (x, y, x + card_width, y + 78), label, value)

    _draw_text(draw, (margin, 350), "最近月份趋势", _get_font(28, True), TEXT)
    month_totals: Counter[str] = Counter()
    for day, count in counts.items():
        month_totals[f"{day.year:04d}-{day.month:02d}"] += count
    recent_months = sorted(month_totals)[-12:]
    if not recent_months:
        _draw_card(draw, (margin, 398, width - margin, 650))
        _draw_centered_text(
            draw,
            (margin, 398, width - margin, 650),
            "暂无生涯数据",
            _get_font(28, True),
            MUTED,
        )
    else:
        chart_left = margin
        chart_top = 408
        chart_width = width - margin * 2
        chart_height = 250
        max_total = max(month_totals[month] for month in recent_months)
        gap = 12
        bar_width = (chart_width - gap * (len(recent_months) - 1)) // len(recent_months)
        for index, month in enumerate(recent_months):
            value = month_totals[month]
            x = chart_left + index * (bar_width + gap)
            bar_height = int((chart_height - 48) * value / max(max_total, 1))
            y = chart_top + chart_height - 34 - bar_height
            draw.rounded_rectangle(
                (x, chart_top + 28, x + bar_width, chart_top + chart_height - 34),
                radius=10,
                fill=BORDER,
            )
            draw.rounded_rectangle(
                (x, y, x + bar_width, chart_top + chart_height - 34),
                radius=10,
                fill=BAR,
            )
            _draw_centered_text(
                draw,
                (
                    x - 6,
                    chart_top + chart_height - 28,
                    x + bar_width + 6,
                    chart_top + chart_height,
                ),
                month[-2:],
                _get_font(15, True),
                MUTED,
            )
            _draw_centered_text(
                draw,
                (x - 8, y - 26, x + bar_width + 8, y - 2),
                str(value),
                _get_font(15, True),
                TEXT,
            )

    return _save_image(image, output_path)


def _validate_current_month_day(today: date, day: int) -> date:
    """Validate and create a date in the current month.

    Args:
        today: Current local date.
        day: Day of month.

    Returns:
        Target date.

    Raises:
        ValueError: If the day is invalid for the current month.
    """
    last_day = calendar.monthrange(today.year, today.month)[1]
    if day < 1 or day > last_day:
        raise ValueError(f"本月只有 1 到 {last_day} 日。")
    return date(today.year, today.month, day)


@register(
    PLUGIN_NAME,
    "Glmg",
    "一个用于记录群友鹿管的 AstrBot 机器人插件。",
    PLUGIN_VERSION,
)
class DeerCalendarPlugin(Star):
    """AstrBot plugin for deer check-in calendars."""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.image_dir = self.data_dir / "images"
        self.data_file = self.data_dir / DATA_FILE_NAME
        self._lock = asyncio.Lock()

    async def initialize(self):
        """Initialize plugin directories.

        Returns:
            None.
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.image_dir.mkdir(parents=True, exist_ok=True)

    def _config_get(self, key: str, default: Any = None) -> Any:
        """Read one plugin config value.

        Args:
            key: Config key.
            default: Default value.

        Returns:
            Config value or default.
        """
        if hasattr(self.config, "get"):
            return self.config.get(key, default)
        return default

    def _config_list(self, key: str) -> list[str]:
        """Read a string list config value.

        Args:
            key: Config key.

        Returns:
            Non-empty stripped string values.
        """
        value = self._config_get(key, [])
        if isinstance(value, str):
            value = [item.strip() for item in value.splitlines()]
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def _config_bool(self, key: str, default: bool) -> bool:
        """Read a bool config value.

        Args:
            key: Config key.
            default: Default value.

        Returns:
            Boolean config value.
        """
        value = self._config_get(key, default)
        return value if isinstance(value, bool) else default

    def _config_str(self, key: str, default: str = "") -> str:
        """Read a string config value.

        Args:
            key: Config key.
            default: Default value.

        Returns:
            String config value.
        """
        value = self._config_get(key, default)
        return value if isinstance(value, str) else default

    def _is_event_allowed(self, event: AstrMessageEvent) -> bool:
        """Check platform and session allow/block config.

        Args:
            event: Incoming AstrBot message event.

        Returns:
            Whether this plugin should handle the event.
        """
        platform_values = {
            event.get_platform_id(),
            event.get_platform_name(),
        }
        session_values = {
            event.unified_msg_origin,
            event.get_session_id(),
            event.get_group_id(),
        }
        platform_values = {value for value in platform_values if value}
        session_values = {value for value in session_values if value}

        platform_blacklist = set(self._config_list("platform_blacklist"))
        session_blacklist = set(self._config_list("session_blacklist"))
        if platform_values & platform_blacklist or session_values & session_blacklist:
            return False

        platform_whitelist = set(self._config_list("platform_whitelist"))
        session_whitelist = set(self._config_list("session_whitelist"))
        if not platform_whitelist and not session_whitelist:
            return True
        return bool(
            platform_values & platform_whitelist or session_values & session_whitelist
        )

    def _clean_analysis_text(self, text: str) -> str:
        """Normalize model output for image rendering.

        Args:
            text: Raw model output.

        Returns:
            Short single-paragraph text.
        """
        cleaned = re.sub(r"^[#>*\-\s]+", "", text.strip())
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned[:160] if len(cleaned) > 160 else cleaned

    def _month_stats_text(
        self,
        records: dict[str, Any],
        year: int,
        month: int,
        today: date,
    ) -> str:
        """Build monthly statistics text for LLM analysis.

        Args:
            records: User records.
            year: Target year.
            month: Target month.
            today: Current local date.

        Returns:
            Plain statistics text.
        """
        counts = _month_record_counts(records, year, month)
        total = sum(counts.values())
        active_days = len(counts)
        longest = _longest_streak(set(counts))
        current = _current_streak(set(counts), today)
        max_day = max(counts.items(), key=lambda item: item[1], default=(None, 0))
        weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(year, month)
        weekly_totals = [
            sum(counts.get(day, 0) for day in week if day.month == month)
            for week in weeks
        ]
        best_day = (
            "暂无" if max_day[0] is None else f"{max_day[0].day}日 {max_day[1]}次"
        )
        return (
            f"总数 {total}；打卡天数 {active_days}；最长连续 {longest} 天；"
            f"当前连续 {current} 天；最高单日 {best_day}；"
            f"周统计 {weekly_totals}。"
        )

    def _year_stats_text(self, records: dict[str, Any], year: int, today: date) -> str:
        """Build yearly statistics text for LLM analysis.

        Args:
            records: User records.
            year: Target year.
            today: Current local date.

        Returns:
            Plain statistics text.
        """
        counts = _year_record_counts(records, year)
        total = sum(counts.values())
        active_days = len(counts)
        longest = _longest_streak(set(counts))
        current = _current_streak(set(counts), today) if today.year == year else 0
        monthly_totals = [
            sum(_month_record_counts(records, year, month).values())
            for month in range(1, 13)
        ]
        best_index = max(range(12), key=lambda index: monthly_totals[index])
        return (
            f"年度总数 {total}；打卡天数 {active_days}；最长连续 {longest} 天；"
            f"当前连续 {current} 天；最佳月份 {best_index + 1}月 "
            f"{monthly_totals[best_index]} 次；月统计 {monthly_totals}。"
        )

    async def _generate_report_analysis(
        self,
        event: AstrMessageEvent,
        user_name: str,
        period_label: str,
        stats_text: str,
    ) -> str:
        """Generate a short LLM analysis for report images (with persona support).

        Args:
            event: Incoming AstrBot message event.
            user_name: Display name.
            period_label: Report period label.
            stats_text: Statistics text.

        Returns:
            Model analysis text or a fallback message.
        """
        if not self._config_bool("enable_llm_report_analysis", True):
            return ""

        provider_id = self._config_str("report_analysis_provider_id").strip()
        try:
            umo = event.unified_msg_origin
            if not provider_id:
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
                
            # ----------------------------------------------------
            # ✨ 新增：尝试获取当前会话正在使用的人设
            # ----------------------------------------------------
            target_persona_id = ""
            conv_mgr = self.context.conversation_manager
            conv_id = await conv_mgr.get_curr_conversation_id(umo)
            if conv_id:
                conv = await conv_mgr.get_conversation(umo, conv_id)
                if conv and getattr(conv, "persona_id", None):
                    target_persona_id = conv.persona_id

            # 构建最终的 System Prompt
            custom_system_prompt = "你只输出适合放入图片的一段中文短分析。" # 默认兜底
            if target_persona_id:
                try:
                    persona_obj = await self.context.persona_manager.get_persona(target_persona_id)
                    if persona_obj and persona_obj.system:
                        # 将人设和绘图要求缝合。注意：由于是画在图片上，必须严控字数，否则文字会溢出图片边界
                        custom_system_prompt = (
                            f"{persona_obj.system}\n\n"
                            "【系统重要指令】：请用你的人设口吻生成内容。因为要渲染到小卡片上，"
                            "你必须且只输出适合放入图片的一段中文短分析（严格控制在60字以内），不要有任何寒暄、标题或废话。"
                        )
                        logger.info(f"🦌日历总结已成功载入人设: {persona_obj.name}")
                except Exception as e:
                    logger.warning(f"获取人格 {target_persona_id} 失败，使用默认设定: {e}")
            # ----------------------------------------------------

            prompt_template = self._config_str(
                "report_analysis_prompt",
                DEFAULT_REPORT_ANALYSIS_PROMPT,
            )
            prompt = prompt_template.format(
                user_name=user_name,
                period_label=period_label,
                stats_text=stats_text,
            )
            
            # 使用包含人格设定的 custom_system_prompt
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=custom_system_prompt,  # ✨ 替换了原本写死的 prompt
            )
            text = self._clean_analysis_text(getattr(response, "completion_text", ""))
            return text or "模型没有返回有效分析，已保留基础统计。"
        except Exception as exc:
            logger.warning(f"🦌报告模型分析失败: {exc}")
            return "模型分析暂不可用，已保留基础统计。"

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def handle_deer_command(self, event: AstrMessageEvent):
        """Handle all deer calendar commands.

        Args:
            event: Incoming AstrBot message event.

        Yields:
            AstrBot message results for matched commands.
        """
        command = parse_deer_command(event.message_str)
        if command is None:
            return

        if not self._is_event_allowed(event):
            return

        event.stop_event()
        today = date.today()
        user_key, platform_id = self._get_user_key(event)
        user_name = self._get_user_name(event)

        try:
            if command.kind == "error":
                yield event.plain_result(command.error)
            elif command.kind == "help":
                yield event.plain_result(HELP_TEXT)
            elif command.kind == "checkin":
                records = await self._change_record(
                    user_key,
                    user_name,
                    today,
                    command.amount,
                )
                path = self._image_path(user_key, "month", today.year, today.month)
                render_month_calendar(
                    path, user_name, records, today.year, today.month, today
                )
                yield event.image_result(str(path))
            elif command.kind == "month_calendar":
                records = await self._get_user_records(user_key)
                path = self._image_path(user_key, "month", today.year, today.month)
                render_month_calendar(
                    path, user_name, records, today.year, today.month, today
                )
                yield event.image_result(str(path))
            elif command.kind == "specific_month_calendar":
                records = await self._get_user_records(user_key)
                month = command.month or today.month
                path = self._image_path(user_key, "month", today.year, month)
                render_month_calendar(
                    path, user_name, records, today.year, month, today
                )
                yield event.image_result(str(path))
            elif command.kind == "year_calendar":
                year = command.year or today.year
                if year > today.year:
                    yield event.plain_result("不能查看未来年份的年历。")
                    return
                records = await self._get_user_records(user_key)
                max_month = today.month if year == today.year else 12
                path = self._image_path(user_key, "year", year, max_month)
                render_year_calendar(path, user_name, records, year, max_month, today)
                yield event.image_result(str(path))
            elif command.kind == "month_report":
                records = await self._get_user_records(user_key)
                month = command.month or today.month
                path = self._image_path(user_key, "month_report", today.year, month)
                analysis = await self._generate_report_analysis(
                    event,
                    user_name,
                    f"{today.year}年{month}月",
                    self._month_stats_text(records, today.year, month, today),
                )
                render_month_report(
                    path,
                    user_name,
                    records,
                    today.year,
                    month,
                    today,
                    analysis,
                )
                yield event.image_result(str(path))
            elif command.kind == "year_report":
                year = command.year or today.year
                if year > today.year:
                    yield event.plain_result("不能分析未来年份的数据。")
                    return
                records = await self._get_user_records(user_key)
                path = self._image_path(user_key, "year_report", year)
                analysis = await self._generate_report_analysis(
                    event,
                    user_name,
                    f"{year}年",
                    self._year_stats_text(records, year, today),
                )
                render_year_report(path, user_name, records, year, today, analysis)
                yield event.image_result(str(path))
            elif command.kind == "ranking":
                data = await self._get_data_snapshot()
                ranking = self._build_ranking(
                    data, platform_id, today.year, today.month
                )
                path = self._image_path(platform_id, "ranking", today.year, today.month)
                render_ranking(
                    path,
                    ranking,
                    platform_id,
                    today.year,
                    today.month,
                    user_name,
                )
                yield event.image_result(str(path))
            elif command.kind == "career":
                records = await self._get_user_records(user_key)
                path = self._image_path(user_key, "career")
                render_career_report(path, user_name, records, today)
                yield event.image_result(str(path))
            elif command.kind == "makeup":
                target_day = _validate_current_month_day(today, command.day or 1)
                records = await self._change_record(
                    user_key,
                    user_name,
                    target_day,
                    command.amount,
                )
                path = self._image_path(user_key, "month", today.year, today.month)
                render_month_calendar(
                    path, user_name, records, today.year, today.month, today
                )
                yield event.image_result(str(path))
            elif command.kind == "revoke":
                target_day = _validate_current_month_day(today, command.day or 1)
                records = await self._change_record(
                    user_key,
                    user_name,
                    target_day,
                    -command.amount,
                )
                path = self._image_path(user_key, "month", today.year, today.month)
                render_month_calendar(
                    path, user_name, records, today.year, today.month, today
                )
                yield event.image_result(str(path))
            else:
                yield event.plain_result("未知命令，请发送 🦌帮助 查看用法。")
        except ValueError as exc:
            yield event.plain_result(str(exc))
        except Exception as exc:
            logger.exception(f"鹿管日记处理失败: {exc}")
            yield event.plain_result("鹿管日记处理失败，请稍后再试。")

    def _get_user_key(self, event: AstrMessageEvent) -> tuple[str, str]:
        """Build the platform-scoped user key.

        Args:
            event: Incoming AstrBot message event.

        Returns:
            Tuple of user key and platform ID.
        """
        platform_id = event.get_platform_id() or event.get_platform_name() or "unknown"
        sender_id = event.get_sender_id() or event.get_session_id() or "unknown"
        return f"{platform_id}:{sender_id}", platform_id

    def _get_user_name(self, event: AstrMessageEvent) -> str:
        """Get a display name for reports.

        Args:
            event: Incoming AstrBot message event.

        Returns:
            Non-empty display name.
        """
        return event.get_sender_name() or event.get_sender_id() or "匿名用户"

    def _image_path(self, key: str, *parts: object) -> Path:
        """Create a stable output path for generated images.

        Args:
            key: User or platform key.
            parts: Image category and period values.

        Returns:
            Safe PNG path under the plugin image directory.
        """
        raw = "::".join([key, *(str(part) for part in parts)])
        digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=12).hexdigest()
        prefix = str(parts[0]) if parts else "image"
        return self.image_dir / f"{prefix}_{digest}.png"

    async def _load_data_unlocked(self) -> dict[str, Any]:
        """Load storage data without acquiring the caller lock.

        Returns:
            Normalized storage data.
        """
        if not self.data_file.exists():
            return _empty_data()
        try:
            raw = self.data_file.read_text(encoding="utf-8")
            return _normalize_data(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise ValueError("数据文件损坏，请联系管理员处理。") from exc

    async def _save_data_unlocked(self, data: dict[str, Any]) -> None:
        """Save storage data atomically without acquiring the caller lock.

        Args:
            data: Storage data to write.

        Returns:
            None.
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temp_file = self.data_file.with_suffix(".json.tmp")
        temp_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_file.replace(self.data_file)

    def _ensure_user(
        self,
        data: dict[str, Any],
        user_key: str,
        user_name: str,
    ) -> dict[str, Any]:
        """Ensure one user object exists in storage.

        Args:
            data: Storage data.
            user_key: Platform-scoped user key.
            user_name: Latest display name.

        Returns:
            User storage object.
        """
        users = data.setdefault("users", {})
        user = users.setdefault(user_key, {"name": user_name, "records": {}})
        user["name"] = user_name
        if not isinstance(user.get("records"), dict):
            user["records"] = {}
        return user

    async def _change_record(
        self,
        user_key: str,
        user_name: str,
        target_day: date,
        delta: int,
    ) -> dict[str, Any]:
        """Change one user's record for one day.

        Args:
            user_key: Platform-scoped user key.
            user_name: Latest display name.
            target_day: Day to update.
            delta: Positive or negative count delta.

        Returns:
            The user's updated records.
        """
        async with self._lock:
            data = await self._load_data_unlocked()
            user = self._ensure_user(data, user_key, user_name)
            records = user["records"]
            key = target_day.isoformat()
            new_count = max(0, int(records.get(key, 0)) + delta)
            if new_count:
                records[key] = new_count
            else:
                records.pop(key, None)
            await self._save_data_unlocked(data)
            return dict(records)

    async def _get_user_records(self, user_key: str) -> dict[str, Any]:
        """Read one user's records.

        Args:
            user_key: Platform-scoped user key.

        Returns:
            User records, or an empty mapping when absent.
        """
        async with self._lock:
            data = await self._load_data_unlocked()
            user = data.get("users", {}).get(user_key, {})
            records = user.get("records", {}) if isinstance(user, dict) else {}
            return dict(records) if isinstance(records, dict) else {}

    async def _get_data_snapshot(self) -> dict[str, Any]:
        """Read the whole storage document.

        Returns:
            Storage data snapshot.
        """
        async with self._lock:
            return await self._load_data_unlocked()

    def _build_ranking(
        self,
        data: dict[str, Any],
        platform_id: str,
        year: int,
        month: int,
    ) -> list[tuple[str, int, int]]:
        """Build current-platform monthly ranking.

        Args:
            data: Storage data snapshot.
            platform_id: Current platform ID.
            year: Target year.
            month: Target month.

        Returns:
            Top 20 ranking rows of display name, total count, and active days.
        """
        rows: list[tuple[str, int, int]] = []
        users = data.get("users", {})
        if not isinstance(users, dict):
            return rows

        prefix = f"{platform_id}:"
        for user_key, user in users.items():
            if not isinstance(user_key, str) or not user_key.startswith(prefix):
                continue
            if not isinstance(user, dict):
                continue
            records = user.get("records", {})
            if not isinstance(records, dict):
                continue
            counts = _month_record_counts(records, year, month)
            total = sum(counts.values())
            if total <= 0:
                continue
            name = user.get("name")
            rows.append(
                (
                    name if isinstance(name, str) and name else "匿名用户",
                    total,
                    len(counts),
                )
            )

        rows.sort(key=lambda row: (-row[1], -row[2], row[0]))
        return rows[:20]

    async def terminate(self):
        """Terminate plugin.

        Returns:
            None.
        """
