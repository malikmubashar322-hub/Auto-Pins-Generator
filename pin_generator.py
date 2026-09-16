"""
Pinterest Pin Generator
------------------------
Reads a Google Sheet (published as CSV). Two modes, auto-detected from
your sheet's columns:

  MODE A - article link only (recommended for you):
    column: article_url
    The script visits each article page and automatically pulls out its
    headline and main photo (the same ones the site shows when you share
    it on Facebook/Twitter - called "Open Graph" tags).

  MODE B - you already have the image + title yourself:
    columns: image_url, title

For each row, builds a pin (2:3 ratio, Pinterest's preferred size):
    [ image ]
    [ random-colored gradient band with wrapped title,
      text auto-switches black/white for readability ]
    [ same image again ]
    [ small "Mubashar" watermark badge, bottom-right corner ]
and saves it as a compressed JPEG in the output folder (small file size,
no visible quality loss - see JPEG_QUALITY below).

If GOOGLE_SERVICE_ACCOUNT_JSON and SHEET_ID are set, it also writes each
pin's public GitHub link back into a "Pin Link" column in your live
Google Sheet (this part is optional - the script works fine without it,
it just skips this step).

It also always saves "pinterest_bulk_upload.csv" next to the pins -
formatted exactly for Pinterest's bulk-upload tool (Title, Media URL,
Pinterest board, Thumbnail, Description, Link, Publish date, Keywords).

Two things are asked before it starts (typed prompt if you run this
locally with `python pin_generator.py`; shown as fillable fields on
GitHub's "Run workflow" button if run through Actions):
  - how many pins per day (controls the Publish date grouping)
  - what UTM parameters to append to each article link

If there are more than CSV_CHUNK_SIZE rows (default 100), the CSV is
split into multiple files (pinterest_bulk_upload_part1.csv, _part2.csv,
...) - each one's publish dates restart fresh from "tomorrow" again,
rather than continuing on from the previous file.

Usage:
    python pin_generator.py

Config is controlled via environment variables (set as GitHub Actions
secrets/vars or workflow inputs, or just export them locally). Leave
PINS_PER_DAY / UTM_QUERY unset to be prompted for them instead:
    SHEET_CSV_URL             - required. The published CSV link from your Google Sheet.
    OUTPUT_DIR                - optional. Defaults to "pins".
    ARTICLE_COL               - optional. Defaults to "article_url".
    IMAGE_COL                 - optional. Defaults to "image_url".
    TITLE_COL                 - optional. Defaults to "title".
    WATERMARK_TEXT            - optional. Defaults to "Mubashar".
    JPEG_QUALITY              - optional. 1-95. Defaults to 85 (high quality, small file).
    PINTEREST_BOARD           - optional. Defaults to "Boredpanda Viral".
    PINTEREST_THUMBNAIL       - optional. Defaults to "0:00" (matches Pinterest's template).
    PINS_PER_DAY              - optional. If unset, you'll be prompted (default if not interactive: 10).
    UTM_QUERY                 - optional. If unset, you'll be prompted. Set to an empty
                                value to add no UTM parameters at all.
                                Default: "utm_source=pinterest&utm_medium=link&utm_campaign=folklore"
    PUBLISH_START_OFFSET_DAYS - optional. Defaults to 1 (schedule starts tomorrow).
    CSV_CHUNK_SIZE            - optional. Defaults to 100 (max rows per CSV file before splitting).
    GOOGLE_SERVICE_ACCOUNT_JSON - optional, advanced. The full contents of your service
                                account's JSON key file, pasted as one secret. Only
                                needed if you want links written directly into your
                                live Sheet instead of just using the CSV.
    SHEET_ID                  - optional, advanced. Required together with
                                GOOGLE_SERVICE_ACCOUNT_JSON to write links back.
    PIN_LINK_COL              - optional. Defaults to "Pin Link".
"""

import csv
import json
import os
import random
import re
import shutil
import sys
import unicodedata
from datetime import date, timedelta
from io import BytesIO
from typing import Tuple
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SHEET_CSV_URL = os.environ.get("SHEET_CSV_URL", "").strip()
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "pins")
LATEST_RUN_DIR = os.environ.get("LATEST_RUN_DIR", "latest_run")
ARTICLE_COL = os.environ.get("ARTICLE_COL", "article_url")
IMAGE_COL = os.environ.get("IMAGE_COL", "image_url")
TITLE_COL = os.environ.get("TITLE_COL", "title")
WATERMARK_TEXT = os.environ.get("WATERMARK_TEXT", "Mubashar")
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "85"))

# Pinterest bulk-upload CSV formatting
PINTEREST_BOARD = os.environ.get("PINTEREST_BOARD", "Boredpanda Viral")
PINTEREST_THUMBNAIL = os.environ.get("PINTEREST_THUMBNAIL", "0:00")
DEFAULT_UTM_QUERY = "utm_source=pinterest&utm_medium=link&utm_campaign=folklore"
DEFAULT_PINS_PER_DAY = 10
PUBLISH_START_OFFSET_DAYS = int(os.environ.get("PUBLISH_START_OFFSET_DAYS", "1"))
CSV_CHUNK_SIZE = int(os.environ.get("CSV_CHUNK_SIZE", "100"))

# Optional: write pin links back into the Google Sheet
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
SHEET_ID = os.environ.get("SHEET_ID", "").strip()
PIN_LINK_COL = os.environ.get("PIN_LINK_COL", "Pin Link")

# Set automatically by GitHub Actions - used to build each pin's public raw link
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "").strip()   # e.g. "Mubashar/PinterestPins"
GITHUB_REF_NAME = os.environ.get("GITHUB_REF_NAME", "main").strip()   # e.g. "main"

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

# Canvas / layout - Pinterest's standard 2:3 ratio (1000 x 1500)
PIN_WIDTH = 1000
PIN_HEIGHT = 1500
IMAGE_HEIGHT = 600        # height of each of the two photo blocks
TITLE_BAND_HEIGHT = PIN_HEIGHT - (IMAGE_HEIGHT * 2)  # 300px middle band

FONT_PATH = os.path.join(os.path.dirname(__file__), "assets", "Poppins-Bold.ttf")
MAX_FONT_SIZE = 92
MIN_FONT_SIZE = 42
SIDE_PADDING = 50

# A pool of background color pairs (start -> end of the gradient band).
# One is picked at random for every pin so a batch of 200 doesn't look identical.
COLOR_PALETTES = [
    ((236, 64, 122), (255, 200, 60)),    # pink -> yellow
    ((79, 172, 254), (0, 242, 254)),     # blue -> cyan
    ((131, 58, 180), (253, 29, 29)),     # purple -> red
    ((17, 153, 142), (56, 239, 125)),    # teal -> green
    ((255, 94, 77), (255, 195, 113)),    # coral -> peach
    ((102, 126, 234), (118, 75, 162)),   # indigo -> purple
    ((250, 139, 255), (0, 219, 222)),    # magenta -> aqua
    ((247, 151, 30), (255, 210, 0)),     # orange -> gold
    ((0, 201, 255), (146, 254, 157)),    # sky -> mint
    ((238, 9, 121), (255, 106, 0)),      # hot pink -> orange
    ((30, 60, 114), (42, 82, 152)),      # navy -> steel blue
    ((252, 92, 125), (106, 130, 251)),   # rose -> periwinkle
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(text: str, max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:max_len] or "pin"


def fetch_image(url: str) -> Image.Image:
    resp = requests.get(url, timeout=20, headers=REQUEST_HEADERS)
    resp.raise_for_status()
    img = Image.open(BytesIO(resp.content)).convert("RGB")
    return img


def extract_title_and_image(article_url: str) -> Tuple[str, str]:
    """
    Visit an article page and pull out its headline + main photo.
    Most sites (including BoredPanda) tag these with "Open Graph" meta tags
    - the same tags Facebook/Twitter use to build a preview card when you
    paste a link. We read those tags directly instead of guessing.
    Falls back to the <title> tag and the first big <img> if OG tags are missing.
    """
    resp = requests.get(article_url, timeout=20, headers=REQUEST_HEADERS)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    def meta(*names):
        for name in names:
            tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return None

    title = meta("og:title", "twitter:title")
    if not title and soup.title:
        title = soup.title.get_text(strip=True)

    image_url = meta("og:image", "og:image:secure_url", "twitter:image")
    if not image_url:
        # fallback: first reasonably sized <img> in the page
        for img_tag in soup.find_all("img"):
            src = img_tag.get("src") or img_tag.get("data-src")
            if src and src.startswith("http"):
                image_url = src
                break

    if not title or not image_url:
        raise ValueError(f"Could not find title/image on page (title={bool(title)}, image={bool(image_url)})")

    return title, image_url


def cover_resize(img: Image.Image, width: int, height: int) -> Image.Image:
    """Resize + center-crop an image to exactly fill width x height (like CSS object-fit: cover)."""
    src_w, src_h = img.size
    src_ratio = src_w / src_h
    dst_ratio = width / height

    if src_ratio > dst_ratio:
        # source is wider than target -> match height, crop width
        new_h = height
        new_w = int(new_h * src_ratio)
    else:
        new_w = width
        new_h = int(new_w / src_ratio)

    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - width) // 2
    top = (new_h - height) // 2
    return img.crop((left, top, left + width, top + height))


def make_gradient(width: int, height: int, start_rgb, end_rgb) -> Image.Image:
    """Diagonal-ish gradient built as a fast left-to-right horizontal blend."""
    base = Image.new("RGB", (width, 1), color=0)
    draw = ImageDraw.Draw(base)
    for x in range(width):
        t = x / max(width - 1, 1)
        r = int(start_rgb[0] + (end_rgb[0] - start_rgb[0]) * t)
        g = int(start_rgb[1] + (end_rgb[1] - start_rgb[1]) * t)
        b = int(start_rgb[2] + (end_rgb[2] - start_rgb[2]) * t)
        draw.point((x, 0), fill=(r, g, b))
    return base.resize((width, height))


def pick_text_color(start_rgb, end_rgb):
    """Choose black or white text depending on how light/dark the gradient is,
    so the title always stays readable no matter which random color was picked."""
    avg = tuple((a + b) // 2 for a, b in zip(start_rgb, end_rgb))
    luminance = 0.299 * avg[0] + 0.587 * avg[1] + 0.114 * avg[2]
    return (24, 24, 24) if luminance > 150 else (255, 255, 255)


def wrap_text_to_width(draw, text, font, max_width):
    """Word-wrap using real measured pixel widths (accurate, unlike character-count guessing)."""
    words = text.split()
    if not words:
        return [text]
    lines = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def fit_font_and_wrap(draw, text, max_width, max_height, font_path):
    """Pick the largest font size (within bounds) whose wrapped text fits the box."""
    best = None
    for size in range(MAX_FONT_SIZE, MIN_FONT_SIZE - 1, -2):
        font = ImageFont.truetype(font_path, size)
        lines = wrap_text_to_width(draw, text, font, max_width)
        line_gap = max(8, int(size * 0.22))

        line_heights, max_line_w, total_h = [], 0, 0
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            line_heights.append(h)
            max_line_w = max(max_line_w, w)
            total_h += h
        total_h += line_gap * (len(lines) - 1)

        if total_h <= max_height and max_line_w <= max_width:
            return font, lines, line_heights, line_gap
        best = (font, lines, line_heights, line_gap)  # remember smallest as fallback

    return best


def add_watermark(canvas: Image.Image, text: str) -> Image.Image:
    """Stamp a small semi-transparent name badge in the bottom-right corner of the pin."""
    canvas = canvas.convert("RGBA")
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    font = ImageFont.truetype(FONT_PATH, 30)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]

    pad_x, pad_y, margin = 26, 14, 28
    badge_w, badge_h = text_w + pad_x * 2, text_h + pad_y * 2
    x0 = canvas.width - badge_w - margin
    y0 = canvas.height - badge_h - margin

    draw.rounded_rectangle((x0, y0, x0 + badge_w, y0 + badge_h), radius=badge_h // 2, fill=(0, 0, 0, 140))
    draw.text((x0 + pad_x, y0 + pad_y - bbox[1]), text, font=font, fill=(255, 255, 255, 235))

    return Image.alpha_composite(canvas, overlay).convert("RGB")


def build_pin(image: Image.Image, title: str) -> Image.Image:
    canvas = Image.new("RGB", (PIN_WIDTH, PIN_HEIGHT), "white")

    photo = cover_resize(image, PIN_WIDTH, IMAGE_HEIGHT)
    canvas.paste(photo, (0, 0))
    canvas.paste(photo, (0, IMAGE_HEIGHT + TITLE_BAND_HEIGHT))

    start_rgb, end_rgb = random.choice(COLOR_PALETTES)
    text_color = pick_text_color(start_rgb, end_rgb)

    gradient = make_gradient(PIN_WIDTH, TITLE_BAND_HEIGHT, start_rgb, end_rgb)
    canvas.paste(gradient, (0, IMAGE_HEIGHT))

    draw = ImageDraw.Draw(canvas)
    max_text_width = PIN_WIDTH - 2 * SIDE_PADDING
    max_text_height = TITLE_BAND_HEIGHT - 60
    font, lines, line_heights, line_gap = fit_font_and_wrap(draw, title, max_text_width, max_text_height, FONT_PATH)

    total_h = sum(line_heights) + line_gap * (len(lines) - 1)
    y = IMAGE_HEIGHT + (TITLE_BAND_HEIGHT - total_h) // 2

    for line, lh in zip(lines, line_heights):
        bbox = draw.textbbox((0, 0), line, font=font)
        w = bbox[2] - bbox[0]
        x = (PIN_WIDTH - w) // 2
        draw.text((x, y), line, font=font, fill=text_color)
        y += lh + line_gap

    canvas = add_watermark(canvas, WATERMARK_TEXT)
    return canvas


def get_pins_per_day() -> int:
    """
    How many pins per day for the Publish date grouping. Priority:
    1) PINS_PER_DAY env var / workflow input, if set
    2) an interactive typed prompt, if running in a real terminal (local run)
    3) the default (10)
    """
    env_val = os.environ.get("PINS_PER_DAY", "").strip()
    if env_val:
        try:
            n = int(env_val)
            if n > 0:
                return n
        except ValueError:
            pass
        print(f"Invalid PINS_PER_DAY '{env_val}', using default {DEFAULT_PINS_PER_DAY}.", file=sys.stderr)
        return DEFAULT_PINS_PER_DAY

    if sys.stdin.isatty():
        raw = input(f"How many pins do you want to upload per day? [{DEFAULT_PINS_PER_DAY}]: ").strip()
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
        return DEFAULT_PINS_PER_DAY

    return DEFAULT_PINS_PER_DAY


def get_utm_query() -> str:
    """
    The UTM query string appended to each article link (without the
    leading '?'), e.g. "utm_source=pinterest&utm_medium=link&utm_campaign=folklore".
    Priority:
    1) UTM_QUERY env var / workflow input, if set (an empty value means "no UTM at all")
    2) an interactive typed prompt, if running in a real terminal (local run)
    3) the default
    """
    if "UTM_QUERY" in os.environ:
        return os.environ["UTM_QUERY"].strip().lstrip("?")

    if sys.stdin.isatty():
        raw = input(f"UTM parameters to append to each article link? [{DEFAULT_UTM_QUERY}]: ").strip()
        return raw.lstrip("?") if raw else DEFAULT_UTM_QUERY

    return DEFAULT_UTM_QUERY


def build_pin_link(filename: str) -> str:
    """Public raw GitHub URL for a pin file, once it's committed into the repo (requires a public repo)."""
    if not GITHUB_REPOSITORY:
        return ""
    return f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/{GITHUB_REF_NAME}/{OUTPUT_DIR}/{filename}"


def add_utm_params(url: str, utm_query: str) -> str:
    """Appends the given UTM query string to a URL without breaking any existing query params."""
    if not url or not utm_query:
        return url
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query))
    query.update(parse_qsl(utm_query))
    return urlunparse(parts._replace(query=urlencode(query)))


def build_publish_dates(count: int, pins_per_day: int):
    """
    One date string per row, in Pinterest's DD/MM/YYYY format. Starts
    PUBLISH_START_OFFSET_DAYS days from today, and advances by one day
    every `pins_per_day` rows - counting every row (even ones whose pin
    failed), so the schedule never skips or leaves a gap.
    """
    start = date.today() + timedelta(days=PUBLISH_START_OFFSET_DAYS)
    dates = []
    for i in range(count):
        day_offset = i // pins_per_day
        dates.append((start + timedelta(days=day_offset)).strftime("%d/%m/%Y"))
    return dates


def save_pinterest_bulk_csv(rows, path, pins_per_day: int):
    """
    rows: list of dicts with keys "title", "media_url", "link" - one per
    attempted row (title/media_url blank if that row's pin failed).
    Writes a CSV ready to import into Pinterest's bulk-upload tool.
    """
    dates = build_publish_dates(len(rows), pins_per_day)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Title", "Media URL", "Pinterest board", "Thumbnail", "Description", "Link", "Publish date", "Keywords"])
        for row, publish_date in zip(rows, dates):
            writer.writerow([
                row.get("title", ""),
                row.get("media_url", ""),
                PINTEREST_BOARD,
                PINTEREST_THUMBNAIL,
                "",
                row.get("link", ""),
                publish_date,
                "",
            ])
    return path


def write_pin_links_to_sheet(pin_links_in_row_order):
    """
    Writes a 'Pin Link' column back into the live Google Sheet, one value per
    data row, in the same order the rows were read. Skipped/failed rows get
    a blank cell. Does nothing (safely) if Sheets credentials aren't configured.
    """
    if not GOOGLE_SERVICE_ACCOUNT_JSON or not SHEET_ID:
        print("Skipping sheet write-back (GOOGLE_SERVICE_ACCOUNT_JSON / SHEET_ID not set).")
        return

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        print("Missing packages for Sheets write-back. Install with: pip install gspread google-auth", file=sys.stderr)
        return

    try:
        creds_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(
            creds_info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        client = gspread.authorize(creds)
        ws = client.open_by_key(SHEET_ID).sheet1

        headers = ws.row_values(1)
        headers_lower = [h.strip().lower() for h in headers]
        if PIN_LINK_COL.lower() in headers_lower:
            col_index = headers_lower.index(PIN_LINK_COL.lower()) + 1
        else:
            col_index = len(headers) + 1
            ws.update_cell(1, col_index, PIN_LINK_COL)

        col_letter = gspread.utils.rowcol_to_a1(1, col_index).rstrip("0123456789")
        start_row, end_row = 2, len(pin_links_in_row_order) + 1
        range_name = f"{col_letter}{start_row}:{col_letter}{end_row}"
        values = [[link] for link in pin_links_in_row_order]

        ws.update(range_name=range_name, values=values)
        print(f"Wrote {len(values)} pin link(s) back into the '{PIN_LINK_COL}' column of your Sheet.")
    except Exception as e:
        print(f"Could not write pin links back to the Sheet: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not SHEET_CSV_URL:
        print("ERROR: SHEET_CSV_URL environment variable is not set.", file=sys.stderr)
        print("Publish your Google Sheet as CSV (File > Share > Publish to web) and set that URL.", file=sys.stderr)
        sys.exit(1)

    pins_per_day = get_pins_per_day()
    utm_query = get_utm_query()
    print(f"Pins per day: {pins_per_day}")
    print(f"UTM parameters: {utm_query or '(none)'}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(LATEST_RUN_DIR, exist_ok=True)

    print(f"Fetching sheet: {SHEET_CSV_URL}")
    df = pd.read_csv(SHEET_CSV_URL)
    df.columns = [c.strip().lower() for c in df.columns]

    article_col = ARTICLE_COL.lower()
    image_col = IMAGE_COL.lower()
    title_col = TITLE_COL.lower()

    use_article_mode = article_col in df.columns
    use_direct_mode = image_col in df.columns and title_col in df.columns

    if not use_article_mode and not use_direct_mode:
        print(
            f"ERROR: sheet needs either an '{ARTICLE_COL}' column, "
            f"or both '{IMAGE_COL}' and '{TITLE_COL}' columns. Found: {list(df.columns)}",
            file=sys.stderr,
        )
        sys.exit(1)

    mode_name = "article-link" if use_article_mode else "direct image+title"
    print(f"Mode: {mode_name}")

    success, failed = 0, 0
    pinterest_rows = []           # one entry per attempted row - for the Pinterest bulk-upload CSV
    pin_links_in_row_order = []   # one entry per EVERY sheet row (including blanks) - for the optional Sheets write-back

    for i, row in df.iterrows():
        pin_link = ""
        row_title = ""
        row_article_link = ""

        if use_article_mode:
            article_url = str(row.get(article_col, "")).strip()
            if not article_url or article_url.lower() == "nan":
                pin_links_in_row_order.append(pin_link)
                continue  # nothing in this row at all - skip entirely, don't count it
            row_article_link = add_utm_params(article_url, utm_query)
        else:
            image_url = str(row.get(image_col, "")).strip()
            title_value = str(row.get(title_col, "")).strip()
            if not image_url or image_url.lower() == "nan":
                pin_links_in_row_order.append(pin_link)
                continue

        try:
            if use_article_mode:
                title, image_url = extract_title_and_image(article_url)
            else:
                title = title_value

            out_name = f"{i+1:04d}-{slugify(title)}.jpg"
            out_path = os.path.join(OUTPUT_DIR, out_name)

            img = fetch_image(image_url)
            pin = build_pin(img, title)
            pin.save(out_path, "JPEG", quality=JPEG_QUALITY, optimize=True)
            shutil.copy2(out_path, os.path.join(LATEST_RUN_DIR, out_name))  # copy for this run's download only - pins/ (permanent hosting) is untouched
            pin_link = build_pin_link(out_name)
            row_title = title
            success += 1
            print(f"[OK]   {out_name}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] row {i+1}: {e}", file=sys.stderr)

        pinterest_rows.append({"title": row_title, "media_url": pin_link, "link": row_article_link})
        pin_links_in_row_order.append(pin_link)

    print(f"\nDone. {success} pins created, {failed} failed. Output: {OUTPUT_DIR}/")

    if pinterest_rows:
        chunks = [pinterest_rows[i:i + CSV_CHUNK_SIZE] for i in range(0, len(pinterest_rows), CSV_CHUNK_SIZE)]
    else:
        chunks = []

    if len(chunks) <= 1:
        csv_path = os.path.join(OUTPUT_DIR, "pinterest_bulk_upload.csv")
        save_pinterest_bulk_csv(pinterest_rows, csv_path, pins_per_day)
        shutil.copy2(csv_path, os.path.join(LATEST_RUN_DIR, os.path.basename(csv_path)))
        print(f"Saved Pinterest bulk-upload CSV to: {csv_path}")
    else:
        for part_num, chunk in enumerate(chunks, start=1):
            csv_path = os.path.join(OUTPUT_DIR, f"pinterest_bulk_upload_part{part_num}.csv")
            save_pinterest_bulk_csv(chunk, csv_path, pins_per_day)  # dates restart fresh for each file
            shutil.copy2(csv_path, os.path.join(LATEST_RUN_DIR, os.path.basename(csv_path)))
            print(f"Saved Pinterest bulk-upload CSV part {part_num} ({len(chunk)} rows) to: {csv_path}")

    write_pin_links_to_sheet(pin_links_in_row_order)


if __name__ == "__main__":
    main()
