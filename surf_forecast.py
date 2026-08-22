#!/usr/bin/env python3
"""Render GoSurf's Tel Aviv forecast and upload it to TRMNL.

Install dependencies:
    python -m pip install requests beautifulsoup4 Pillow python-bidi

Example (PowerShell):
    $env:TRMNL_WEBHOOK_UUID = "your-plugin-setting-uuid"
    $env:FONT_PATH = "C:\\fonts\\Rubik-Bold.ttf"
    python surf_forecast.py

Use ``--no-push`` to create a local preview without calling TRMNL.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import math
import os
import re
import sys
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, features

try:
    from bidi.algorithm import get_display as bidi_get_display
except ImportError:  # Pillow with libraqm does not need python-bidi.
    bidi_get_display = None


GOSURF_URL = os.getenv(
    "GOSURF_URL", "https://gosurf.co.il/forecast/tel-aviv"
)
WEBHOOK_UUID = os.getenv("TRMNL_WEBHOOK_UUID", "")
TRMNL_WEBHOOK_URL = os.getenv("TRMNL_WEBHOOK_URL", "")
FONT_PATH = os.getenv("FONT_PATH", "Rubik-Bold.ttf")

WIDTH = 800
HEIGHT = 480
REQUEST_TIMEOUT = (5, 25)  # connect timeout, read timeout
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)

HEBREW_WEEKDAYS = {
    0: "שני",
    1: "שלישי",
    2: "רביעי",
    3: "חמישי",
    4: "שישי",
    5: "שבת",
    6: "ראשון",
}

DAY_KEYS = {
    "day",
    "dayname",
    "daytitle",
    "weekday",
    "weekdayname",
    "label",
    "name",
}
DATE_KEYS = {
    "date",
    "daydate",
    "displaydate",
    "forecastdate",
    "formatteddate",
    "localdate",
}
HEIGHT_KEYS = {
    "height",
    "heightcm",
    "wave",
    "waveheight",
    "waveheightcm",
    "wavesize",
    "wavesizecm",
}

LOG = logging.getLogger("trmnl-waves")


class ForecastError(RuntimeError):
    """The forecast could not be downloaded or parsed."""


class RenderError(RuntimeError):
    """The image could not be rendered."""


class UploadError(RuntimeError):
    """TRMNL rejected the image upload."""


@dataclass(frozen=True)
class ForecastDay:
    day_name: str
    date: str
    wave_height_cm: int


def _normalise_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _walk_leaves(
    value: Any, path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], Any]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield from _walk_leaves(child, path + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_leaves(child, path + (str(index),))
    else:
        yield path, value


def _walk_lists(
    value: Any, path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], list[Any]]]:
    if isinstance(value, list):
        yield path, value
        for index, child in enumerate(value):
            yield from _walk_lists(child, path + (str(index),))
    elif isinstance(value, Mapping):
        for key, child in value.items():
            yield from _walk_lists(child, path + (str(key),))


def _find_leaf(
    record: Mapping[str, Any], accepted_keys: set[str]
) -> tuple[Any, str] | None:
    matches: list[tuple[int, Any, str]] = []
    for path, value in _walk_leaves(record):
        if path and _normalise_key(path[-1]) in accepted_keys:
            matches.append((len(path), value, path[-1]))
    if not matches:
        return None
    _, value, key = min(matches, key=lambda item: item[0])
    return value, key


def _parse_date(value: Any) -> tuple[str, Date | None] | None:
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0
        try:
            parsed = datetime.fromtimestamp(timestamp).date()
            return parsed.strftime("%d/%m"), parsed
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    iso_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if iso_match:
        try:
            parsed = Date(*(int(part) for part in iso_match.groups()))
        except ValueError:
            return None
        return parsed.strftime("%d/%m"), parsed

    match = re.search(r"(?<!\d)(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?", text)
    if match:
        day, month = int(match.group(1)), int(match.group(2))
        year_text = match.group(3)
        year = int(year_text) if year_text else datetime.now().year
        if year < 100:
            year += 2000
        try:
            parsed = Date(year, month, day)
        except ValueError:
            return None
        return f"{day:02d}/{month:02d}", parsed

    return None


def _round_to_ten(value: float) -> int:
    return int(math.floor(value / 10.0 + 0.5) * 10)


def _parse_height_cm(value: Any, source_key: str = "") -> int | None:
    if isinstance(value, bool):
        return None

    numbers: list[float]
    if isinstance(value, (int, float)):
        numbers = [float(value)]
    elif isinstance(value, str):
        numbers = [
            float(item.replace(",", "."))
            for item in re.findall(r"\d+(?:[.,]\d+)?", value)
        ]
        if not numbers:
            return None
    else:
        return None

    height = sum(numbers[:2]) / min(2, len(numbers))
    key = _normalise_key(source_key)
    text = str(value).casefold()
    explicitly_cm = "cm" in key or "ס״מ" in text or 'ס"מ' in text
    explicitly_metres = not explicitly_cm and (
        "meter" in key
        or "metre" in key
        or key.endswith("m")
        or bool(re.search(r"(?:^|\s)\d+(?:[.,]\d+)?\s*(?:m|מ(?:טר)?)\b", text))
    )
    if explicitly_metres or (not explicitly_cm and 0 < height <= 5):
        height *= 100

    if not math.isfinite(height) or height < 0 or height > 2_000:
        return None
    return int(round(height))


def _record_to_day(record: Mapping[str, Any]) -> ForecastDay | None:
    date_leaf = _find_leaf(record, DATE_KEYS)
    height_leaf = _find_leaf(record, HEIGHT_KEYS)
    if not date_leaf or not height_leaf:
        return None

    parsed_date = _parse_date(date_leaf[0])
    height = _parse_height_cm(height_leaf[0], height_leaf[1])
    if not parsed_date or height is None:
        return None

    display_date, date_value = parsed_date
    day_leaf = _find_leaf(record, DAY_KEYS)
    day_name = str(day_leaf[0]).strip() if day_leaf else ""
    if not day_name and date_value:
        day_name = HEBREW_WEEKDAYS[date_value.weekday()]
    if date_value == datetime.now().date():
        day_name = "היום"
    if not day_name:
        return None

    return ForecastDay(day_name, display_date, height)


def _deduplicate_days(records: Iterable[ForecastDay]) -> list[ForecastDay]:
    grouped: OrderedDict[str, list[ForecastDay]] = OrderedDict()
    for record in records:
        grouped.setdefault(record.date, []).append(record)

    result: list[ForecastDay] = []
    for same_date in grouped.values():
        heights = sorted(item.wave_height_cm for item in same_date)
        middle = len(heights) // 2
        median = (
            heights[middle]
            if len(heights) % 2
            else (heights[middle - 1] + heights[middle]) / 2
        )
        representative = same_date[0]
        height = representative.wave_height_cm
        if len(same_date) > 1:
            height = _round_to_ten(float(median))
        result.append(
            ForecastDay(representative.day_name, representative.date, height)
        )
    return result


def _forecast_from_next_data(next_data: Mapping[str, Any]) -> list[ForecastDay]:
    """Find the seven-day collection without coupling to one Next.js build."""
    candidates: list[tuple[int, list[ForecastDay]]] = []
    for path, value in _walk_lists(next_data):
        if len(value) < 7 or not all(isinstance(item, Mapping) for item in value):
            continue

        parsed = [
            day
            for item in value
            if (day := _record_to_day(item)) is not None
        ]
        parsed = _deduplicate_days(parsed)
        if len(parsed) < 7:
            continue

        path_text = ".".join(path).casefold()
        score = 10 if len(parsed) == 7 else 0
        score += 5 if "forecast" in path_text else 0
        score += 3 if "day" in path_text or "daily" in path_text else 0
        score += 2 if "wave" in path_text else 0
        candidates.append((score, parsed[:7]))

    if not candidates:
        raise ForecastError(
            "Found __NEXT_DATA__, but no seven-day records with day/date/wave-height fields"
        )
    return max(candidates, key=lambda item: item[0])[1]


def _weekly_chart_heights(soup: BeautifulSoup) -> list[int]:
    for script in soup.find_all("script"):
        source = script.string or script.get_text()
        if "weeklyData" not in source:
            continue

        labels_match = re.search(r"\blabels\s*:\s*(\[[^\]]*\])", source, re.DOTALL)
        if labels_match:
            try:
                labels = json.loads(labels_match.group(1))
            except json.JSONDecodeError:
                labels = []
            heights = [
                height
                for label in labels
                if (height := _parse_height_cm(label)) is not None
            ]
            if len(heights) == 7:
                # Chart.js stores points from the screen's left edge to its
                # right edge, while GoSurf's Hebrew day cells are emitted in
                # logical RTL order (today first/rightmost). Align the two.
                return list(reversed(heights))

        data_match = re.search(
            r"\bvar\s+weeklyData\s*=\s*(\[[^\]]+\])", source, re.DOTALL
        )
        if not data_match:
            continue
        try:
            samples = [float(item) for item in json.loads(data_match.group(1))]
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if len(samples) >= 7 and len(samples) % 7 == 0:
            samples_per_day = len(samples) // 7
            midpoint = samples_per_day // 2
            chart_order = [
                _round_to_ten(samples[index * samples_per_day + midpoint])
                for index in range(7)
            ]
            return list(reversed(chart_order))

    return []


def _forecast_from_current_html(soup: BeautifulSoup) -> list[ForecastDay]:
    """Parse GoSurf's current non-Next.js weekly table and Chart.js labels."""
    section = soup.select_one("#website_forecast_weekly_cont")
    if section is None:
        raise ForecastError("GoSurf weekly forecast section was not found")

    table = section.select_one("table.weekly_cont")
    row = table.find("tr") if table else None
    cells = row.find_all("td", recursive=False) if row else []
    if len(cells) < 7:
        raise ForecastError("GoSurf weekly forecast does not contain seven day columns")

    headings: list[tuple[str, str]] = []
    for cell in cells[:7]:
        fragments = list(cell.stripped_strings)
        day_name = fragments[0] if fragments else ""
        date_match = re.search(r"\b\d{1,2}/\d{1,2}\b", " ".join(fragments))
        if not day_name or not date_match:
            raise ForecastError("A GoSurf day name or date is missing")
        day, month = (int(part) for part in date_match.group(0).split("/"))
        headings.append((day_name, f"{day:02d}/{month:02d}"))

    heights = _weekly_chart_heights(soup)
    if len(heights) != 7:
        raise ForecastError("GoSurf's weekly wave-height labels were not found")

    return [
        ForecastDay(day_name, display_date, height)
        for (day_name, display_date), height in zip(headings, heights)
    ]


def _validate_forecast(days: Sequence[ForecastDay]) -> list[ForecastDay]:
    if len(days) != 7:
        raise ForecastError(f"Expected seven forecast days, received {len(days)}")
    for index, day in enumerate(days, start=1):
        if not day.day_name or not re.fullmatch(r"\d{2}/\d{2}", day.date):
            raise ForecastError(f"Forecast day {index} has an invalid name or date")
        if not 0 <= day.wave_height_cm <= 2_000:
            raise ForecastError(f"Forecast day {index} has an invalid wave height")
    return list(days)


def fetch_forecast(
    url: str = GOSURF_URL,
    *,
    session: requests.Session | None = None,
    timeout: tuple[int, int] = REQUEST_TIMEOUT,
) -> list[ForecastDay]:
    """Fetch and return seven GoSurf forecast days.

    ``__NEXT_DATA__`` is preferred when GoSurf provides it. The site currently
    serves its seven-day values in a weekly HTML table and an inline Chart.js
    array, so that representation is supported as a live-site fallback.
    """
    client = session or requests.Session()
    try:
        response = client.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "he-IL,he;q=0.9,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=timeout,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ForecastError(f"Could not fetch GoSurf: {exc}") from exc

    soup = BeautifulSoup(response.text, "html.parser")
    next_script = soup.find("script", id="__NEXT_DATA__")
    if next_script is not None:
        try:
            payload = json.loads(next_script.string or next_script.get_text())
            days = _forecast_from_next_data(payload)
            LOG.info("Parsed forecast from __NEXT_DATA__")
            return _validate_forecast(days)
        except (json.JSONDecodeError, ForecastError) as exc:
            LOG.warning("Could not use __NEXT_DATA__ (%s); trying live HTML", exc)

    days = _forecast_from_current_html(soup)
    LOG.info("Parsed forecast from GoSurf's weekly HTML/Chart.js data")
    return _validate_forecast(days)


def _has_raqm() -> bool:
    try:
        return bool(features.check_feature("raqm"))
    except (TypeError, ValueError):
        return False


HAVE_RAQM = _has_raqm()


def _resolve_font_path(configured_path: str | os.PathLike[str]) -> Path:
    script_dir = Path(__file__).resolve().parent
    configured = Path(configured_path).expanduser()
    candidates = [configured]
    if not configured.is_absolute():
        candidates = [script_dir / configured, Path.cwd() / configured]
    candidates.extend(
        [
            script_dir / "Rubik-Bold.ttf",
            script_dir / "Assistant-Bold.ttf",
            Path("/usr/share/fonts/truetype/noto/NotoSansHebrew-Bold.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
            Path("C:/Windows/Fonts/arialbd.ttf"),
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    checked = ", ".join(str(item) for item in candidates)
    raise RenderError(
        "No Hebrew-capable bold TTF font was found. Set FONT_PATH or --font. "
        f"Checked: {checked}"
    )


def _load_font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    layout = ImageFont.Layout.RAQM if HAVE_RAQM else ImageFont.Layout.BASIC
    try:
        return ImageFont.truetype(str(path), size=size, layout_engine=layout)
    except OSError as exc:
        raise RenderError(f"Could not load font {path}: {exc}") from exc


def _display_text(text: str, rtl: bool) -> tuple[str, dict[str, str]]:
    if not rtl:
        return text, ({"direction": "ltr"} if HAVE_RAQM else {})
    if HAVE_RAQM:
        return text, {"direction": "rtl", "language": "he"}
    if bidi_get_display is None:
        raise RenderError(
            "This Pillow build has no libraqm. Install the fallback with "
            "'python -m pip install python-bidi'."
        )
    return bidi_get_display(text), {}


def _draw_centered(
    draw: ImageDraw.ImageDraw,
    position: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    *,
    rtl: bool,
) -> None:
    display, layout = _display_text(text, rtl)
    draw.text(
        position,
        display,
        fill=0,
        font=font,
        anchor="mm",
        stroke_width=0,
        **layout,
    )


def _text_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    *,
    rtl: bool,
) -> int:
    display, layout = _display_text(text, rtl)
    left, _, right, _ = draw.textbbox((0, 0), display, font=font, **layout)
    return right - left


def generate_image(
    forecast: Sequence[ForecastDay],
    font_path: str | os.PathLike[str] = FONT_PATH,
) -> io.BytesIO:
    """Render an exact 800x480, black-and-white PNG into memory."""
    days = _validate_forecast(forecast)
    resolved_font = _resolve_font_path(font_path)

    scale = 2
    sx = lambda value: int(round(value * scale))
    canvas = Image.new("L", (WIDTH * scale, HEIGHT * scale), color=255)
    draw = ImageDraw.Draw(canvas)

    header_font = _load_font(resolved_font, sx(38))
    day_font = _load_font(resolved_font, sx(25))
    date_font = _load_font(resolved_font, sx(18))

    _draw_centered(
        draw,
        (sx(WIDTH / 2), sx(36)),
        "תחזית גלים - תל אביב",
        header_font,
        rtl=True,
    )
    draw.line((0, sx(70), sx(WIDTH), sx(70)), fill=0, width=sx(2))

    edges = [round(index * WIDTH / 7) for index in range(8)]
    centers = [
        round((edges[6 - index] + edges[7 - index]) / 2) for index in range(7)
    ]
    for edge in edges[1:-1]:
        draw.line(
            (sx(edge), sx(80), sx(edge), sx(260)), fill=0, width=sx(1)
        )
    draw.line((0, sx(260), sx(WIDTH), sx(260)), fill=0, width=sx(1))

    height_texts = [f"{day.wave_height_cm} ס״מ" for day in days]
    column_width = min(b - a for a, b in zip(edges, edges[1:]))
    height_font: ImageFont.FreeTypeFont | None = None
    for size in range(36, 23, -1):
        candidate = _load_font(resolved_font, sx(size))
        if all(
            _text_width(draw, label, candidate, rtl=True)
            <= sx(column_width - 10)
            for label in height_texts
        ):
            height_font = candidate
            break
    if height_font is None:
        raise RenderError("Wave-height labels do not fit the seven-column layout")

    for center, day, height_text in zip(centers, days, height_texts):
        _draw_centered(
            draw, (sx(center), sx(105)), day.day_name, day_font, rtl=True
        )
        _draw_centered(
            draw, (sx(center), sx(142)), day.date, date_font, rtl=False
        )
        _draw_centered(
            draw, (sx(center), sx(207)), height_text, height_font, rtl=True
        )

    plot_top = 290
    plot_bottom = 435
    max_height = max(day.wave_height_cm for day in days)
    chart_max = max(20, int(math.ceil(max_height / 20.0) * 20))
    points = [
        (
            sx(center),
            sx(
                plot_bottom
                - (day.wave_height_cm / chart_max) * (plot_bottom - plot_top)
            ),
        )
        for center, day in zip(centers, days)
    ]

    draw.line(
        (sx(20), sx(plot_bottom), sx(WIDTH - 20), sx(plot_bottom)),
        fill=0,
        width=sx(1),
    )
    draw.line(points, fill=0, width=sx(4), joint="curve")
    radius = sx(6)
    outline_width = sx(3)
    for x, y in points:
        draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            fill=255,
            outline=0,
            width=outline_width,
        )

    reduced = canvas.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    monochrome = reduced.point(lambda pixel: 255 if pixel >= 180 else 0, mode="1")
    if monochrome.size != (WIDTH, HEIGHT) or monochrome.mode != "1":
        raise RenderError("Internal error: output is not an 800x480 1-bit image")

    output = io.BytesIO()
    monochrome.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


def _build_webhook_url(webhook_uuid: str, webhook_url: str = "") -> str:
    if webhook_url:
        url = webhook_url.strip()
    else:
        value = webhook_uuid.strip()
        if value.startswith(("https://", "http://")):
            url = value
        elif value:
            url = (
                "https://trmnl.com/api/plugin_settings/"
                f"{quote(value, safe='')}/image"
            )
        else:
            raise UploadError(
                "Set TRMNL_WEBHOOK_UUID/TRMNL_WEBHOOK_URL, or pass "
                "--webhook-uuid/--webhook-url"
            )
    if not url.startswith("https://"):
        raise UploadError("TRMNL webhook URL must use HTTPS")
    return url


def send_to_trmnl(
    image: io.BytesIO,
    webhook_uuid: str = WEBHOOK_UUID,
    *,
    webhook_url: str = TRMNL_WEBHOOK_URL,
    timeout: tuple[int, int] = REQUEST_TIMEOUT,
) -> requests.Response:
    """POST raw PNG bytes to the TRMNL Webhook Image endpoint."""
    url = _build_webhook_url(webhook_uuid, webhook_url)
    payload = image.getvalue()
    try:
        response = requests.post(
            url,
            headers={"Content-Type": "image/png"},
            data=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise UploadError(f"Could not upload image to TRMNL: {exc}") from exc

    if response.status_code == 429:
        raise UploadError("TRMNL rate limit reached (12 image uploads per hour)")
    if response.status_code == 422:
        raise UploadError(
            "TRMNL rejected the PNG (HTTP 422); check format and the 5 MB limit"
        )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        body = response.text.strip()[:300]
        detail = f": {body}" if body else ""
        raise UploadError(f"TRMNL returned HTTP {response.status_code}{detail}") from exc
    return response


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render GoSurf Tel Aviv's seven-day forecast for TRMNL"
    )
    parser.add_argument("--url", default=GOSURF_URL, help="GoSurf forecast URL")
    parser.add_argument(
        "--font", default=FONT_PATH, help="Hebrew-capable bold TTF font path"
    )
    parser.add_argument(
        "--webhook-uuid", default=WEBHOOK_UUID, help="TRMNL plugin-setting UUID"
    )
    parser.add_argument(
        "--webhook-url",
        default=TRMNL_WEBHOOK_URL,
        help="Complete Webhook Image URL (preferred when supplied by TRMNL)",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("forecast.png"), help="Preview PNG path"
    )
    parser.add_argument(
        "--no-push", action="store_true", help="Render locally without uploading"
    )
    parser.add_argument("--debug", action="store_true", help="Show debug logging")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    try:
        forecast = fetch_forecast(args.url)
        for day in forecast:
            LOG.info("%s %s: %d cm", day.day_name, day.date, day.wave_height_cm)

        png = generate_image(forecast, args.font)
        args.output.write_bytes(png.getvalue())
        LOG.info("Wrote %s (%d bytes)", args.output, len(png.getvalue()))

        if not args.no_push:
            send_to_trmnl(
                png,
                args.webhook_uuid,
                webhook_url=args.webhook_url,
            )
            LOG.info("TRMNL accepted the image")
        return 0
    except (ForecastError, RenderError, UploadError, OSError) as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
