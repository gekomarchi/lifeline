"""Builds the demo video from the footage: the picture, the subtitles, the sound and the script.

    python demo/compose.py [--out <file>] [--from <seconds>] [--to <seconds>] [--frames <t,t,...>]

Input (nothing here is typed by hand):
    demo/shots.py                 the shot list: surfaces, camera, cursor, marks
    demo/narration.json           the narration text, the voice and the spoken forms
    demo/footage/voice/           the voice clips with the time of each character     (demo/voice.py)
    demo/footage/web/             the pictures of the web surfaces with their regions  (demo/surfaces.py)
    demo/footage/capture.*        the terminal recording of the capture step           (demo/record_terminal.sh)

Output:
    demo/build/lifeline-demo.mp4  the video, 1920x1080, 30 frames per second (not committed)
    demo/subtitles.srt            the subtitles of this cut
    demo/build/script.md          the shot list with the narration and the times of this cut (not committed)
    demo/build/timeline.json      the times of each clip, shot, cue and claim anchor

The same footage gives the same cut: no random value and no clock is used. `--frames` writes single pictures
to demo/build/frames/ and no video: for a check of one moment. `--from/--to` renders a part with no sound.

What is added on top of a surface, in the style of `board/template.html` (its dark tokens, Inter, JetBrains
Mono): the subtitles, the address of the surface at the bottom left, a thin outline around one element, the
label of the terminal recording, and the cursor (the standard arrow of Windows).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shots as shot_list  # noqa: E402

Image.MAX_IMAGE_PIXELS = None
HERE = Path(__file__).resolve().parent
FOOTAGE, WEB, VOICE, BUILD = HERE / "footage", HERE / "footage" / "web", HERE / "footage" / "voice", HERE / "build"
W, H, FPS = 1920, 1080, 30
# The part of the picture for the element that a shot is about: the subtitles are below it.
SAFE = (90, 64, 1830, 884)
FADE = 0.2  # the cross-fade between two shots, the same each time
LIMIT = 180.0  # the rule of the buildathon: a demo video under three minutes


# ----------------------------------------------------------------------------------------------
# The tokens of the board (dark theme of board/template.html)
# ----------------------------------------------------------------------------------------------


def oklch(lightness: float, chroma: float, hue: float) -> tuple:
    """An OKLCH colour of the style sheet of the board as sRGB bytes."""
    a, b = chroma * math.cos(math.radians(hue)), chroma * math.sin(math.radians(hue))
    l_, m_, s_ = lightness + 0.3963377774 * a + 0.2158037573 * b, lightness - 0.1055613458 * a - 0.0638541728 * b, \
        lightness - 0.0894841775 * a - 1.2914855480 * b
    l3, m3, s3 = l_ ** 3, m_ ** 3, s_ ** 3
    linear = (4.0767416621 * l3 - 3.3077115913 * m3 + 0.2309699292 * s3,
              -1.2684380046 * l3 + 2.6097574011 * m3 - 0.3413193965 * s3,
              -0.0041960863 * l3 - 0.7034186147 * m3 + 1.7076147010 * s3)
    return tuple(round(255 * min(max(12.92 * v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055, 0), 1))
                 for v in linear)


CANVAS = oklch(0.16, 0.002, 265)     # --canvas: gray-975
SURFACE = oklch(0.22, 0.002, 265)    # --surface-1: gray-925
INK_1 = oklch(0.985, 0, 0)           # --ink-1
INK_3 = oklch(0.76, 0.002, 265)      # --ink-3: gray-400
ACCENT = oklch(0.71, 0.17, 263)      # --accent: accent-400
LINE_2 = tuple(round(s + (i - s) * 0.14) for s, i in zip(SURFACE, INK_1))  # --line-2 on the surface
RADIUS = 15                          # --r-sm (10px) at the scale of the picture, 1.5


def font(family: str, size: int) -> ImageFont.FreeTypeFont:
    path = subprocess.run(["fc-match", "-f", "%{file}", family], capture_output=True, text=True, check=True).stdout
    if family.split(":")[0].lower() not in Path(path).name.lower().replace("-", " ").replace("mono", " mono"):
        raise SystemExit(f"the font {family} is not installed (fc-match gave {path})")
    return ImageFont.truetype(path, size)


@lru_cache(maxsize=None)
def fonts() -> dict:
    return {"subtitle": font("Inter:medium", 38), "chip": font("JetBrains Mono:regular", 19)}


def panel(width: int, height: int, fill=SURFACE, alpha=255, ring=LINE_2, radius=RADIUS, stroke=0, outline=None):
    """A rounded box as a picture with transparency, with smooth corners (drawn three times as large)."""
    k = 3
    big = Image.new("RGBA", (width * k, height * k), (0, 0, 0, 0))
    draw = ImageDraw.Draw(big)
    if outline is None:
        draw.rounded_rectangle([0, 0, width * k - 1, height * k - 1], radius * k, fill=(*ring, alpha))
        draw.rounded_rectangle([k, k, width * k - 1 - k, height * k - 1 - k], (radius - 1) * k, fill=(*fill, alpha))
    else:
        draw.rounded_rectangle([0, 0, width * k - 1, height * k - 1], radius * k, outline=(*outline, 255),
                               width=stroke * k)
    return big.resize((width, height), Image.LANCZOS)


# ----------------------------------------------------------------------------------------------
# Narration, timeline and anchors
# ----------------------------------------------------------------------------------------------


def spoken_map(text: str, spoken: dict) -> tuple:
    """The text as sent to the voice engine, and for each character of the narration text the place of its
    first character in the text as sent (one more entry for the end)."""
    sent, places, i = [], [], 0
    terms = sorted(spoken, key=len, reverse=True)
    while i < len(text):
        term = next((t for t in terms if text.startswith(t, i)), None)
        if term:
            places += [len(sent)] * len(term)
            sent += list(spoken[term])
            i += len(term)
        else:
            places.append(len(sent))
            sent.append(text[i])
            i += 1
    places.append(len(sent))
    return "".join(sent), places


class Timeline:
    """The start of each voice clip in the video, and the anchors of the shot list."""

    def __init__(self):
        script = json.loads((HERE / "narration.json").read_text())
        self.voice, self.spoken = script["voice"], script["spoken"]
        self.clips, cursor = {}, shot_list.LEAD_IN
        for index, clip in enumerate(script["clips"]):
            record_path = VOICE / f"{clip['id']}.json"
            if not record_path.exists():
                raise SystemExit(f"no voice clip {clip['id']}: run `python demo/voice.py` (needs the ElevenLabs key)")
            record = json.loads(record_path.read_text())
            sent, places = spoken_map(clip["text"], self.spoken)
            if record["sent"] != sent or "".join(record["characters"]) != sent:
                raise SystemExit(f"the voice clip {clip['id']} is of another text: run `python demo/voice.py`")
            if index:
                cursor += shot_list.PAUSE.get(clip["id"], shot_list.DEFAULT_PAUSE)
            self.clips[clip["id"]] = {**clip, "sent": sent, "places": places, "start": cursor,
                                      "end": cursor + record["seconds"], "starts": record["starts"],
                                      "ends": record["ends"], "audio": VOICE / f"{clip['id']}.mp3"}
            cursor += record["seconds"]
        self.total = cursor + shot_list.TAIL

    def at(self, anchor) -> float:
        """The time of an anchor: see the head of `shots.py`."""
        if isinstance(anchor, (int, float)):
            return float(anchor)
        base, _, offset = anchor.partition("|")
        shift = float(offset) if offset else 0.0
        if re.fullmatch(r"[0-9.]+", base):
            return float(base) + shift
        match = re.fullmatch(r"(n\d+)(\$|@.+)?", base)
        if not match or match.group(1) not in self.clips:
            raise SystemExit(f"shots.py: the anchor {anchor!r} names no clip")
        clip, mark = self.clips[match.group(1)], match.group(2)
        if mark is None:
            return clip["start"] + shift
        if mark == "$":
            return clip["end"] + shift
        place = clip["sent"].lower().find(mark[1:].lower())
        if place < 0:
            raise SystemExit(f"shots.py: the anchor {anchor!r}: clip {clip['id']} has no words {mark[1:]!r}")
        return clip["start"] + clip["starts"][place] + shift


# ----------------------------------------------------------------------------------------------
# Subtitles
# ----------------------------------------------------------------------------------------------

CUE_CHARACTERS = 46  # one line that a viewer reads at double speed


def cue_spans(text: str) -> list:
    """Cuts a narration text into cues: (start, end) character places. A cue is a sentence, or a part of a
    sentence that ends at a comma or at the word nearest to its middle."""

    def cut(start: int, end: int) -> list:
        while start < end and text[start] == " ":
            start += 1
        if end - start <= CUE_CHARACTERS:
            return [(start, end)]
        middle = (start + end) / 2
        commas = [m.end() for m in re.finditer(r"[,;:]\s", text[start:end])]
        spaces = [m.end() for m in re.finditer(r"\s", text[start:end])]
        choices = [c for c in commas if 12 <= c <= end - start - 12] or [s for s in spaces if 8 <= s <= end - start - 8]
        place = start + min(choices, key=lambda c: abs(start + c - middle))
        return cut(start, place) + cut(place, end)

    spans, start = [], 0
    for match in re.finditer(r"[.!?:]\s+", text):
        spans += cut(start, match.end())
        start = match.end()
    return [(a, b) for a, b in spans + cut(start, len(text)) if text[a:b].strip()]


def build_cues(timeline: Timeline) -> list:
    cues = []
    for clip in timeline.clips.values():
        for a, b in cue_spans(clip["text"]):
            words = clip["text"][a:b].strip()
            first, last = clip["places"][a], clip["places"][a + len(clip["text"][a:b].rstrip())] - 1
            cues.append({"clip": clip["id"], "text": words, "start": clip["start"] + clip["starts"][first],
                         "spoken_end": clip["start"] + clip["ends"][last]})
    # A cue starts a moment before its words, so that it is never late. It stays a moment after them, and it
    # ends before the next one starts.
    for cue in cues:
        cue["start"] = max(cue["start"] - 0.15, 0)
    for cue, following in zip(cues, cues[1:] + [None]):
        cue["end"] = cue["spoken_end"] + 0.4
        if following is not None:
            cue["end"] = min(cue["end"], following["start"] - 0.06)
    return cues


def write_srt(cues: list, path: Path) -> None:
    def stamp(seconds: float) -> str:
        ms = round(seconds * 1000)
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    path.write_text("".join(f"{n}\n{stamp(c['start'])} --> {stamp(c['end'])}\n{c['text']}\n\n"
                            for n, c in enumerate(cues, 1)))


@lru_cache(maxsize=None)
def subtitle_sprite(text: str) -> Image.Image:
    face = fonts()["subtitle"]
    left, top, right, bottom = face.getbbox(text)
    width, height = right - left + 2 * 26, 66
    sprite = panel(width, height)
    ImageDraw.Draw(sprite).text((width / 2, height / 2 + 1), text, font=face, fill=(*INK_1, 255), anchor="mm")
    return sprite


@lru_cache(maxsize=None)
def chip_sprite(lines: tuple) -> Image.Image:
    """The address of a surface, or the label of the terminal recording: one or two lines."""
    face = fonts()["chip"]
    width = max(round(face.getlength(line)) for line in lines) + 2 * 16
    height = 16 + 26 * len(lines)
    sprite = panel(width, height, radius=10)
    draw = ImageDraw.Draw(sprite)
    for n, line in enumerate(lines):
        draw.text((16, 8 + 26 * n + 13), line, font=face, fill=(*(INK_1 if n == 0 else INK_3), 255), anchor="lm")
    return sprite


@lru_cache(maxsize=None)
def cursor_sprite() -> Image.Image:
    """The standard arrow of Windows: a white arrow with a black edge. The hot spot is its tip, at (1, 1)."""
    k, unit = 8, 1.5  # drawn 8 times as large; 1.5 picture pixels for one pixel of the 12x19 arrow
    outline = [(0, 0), (0, 16.4), (3.75, 12.9), (6.35, 19.0), (8.75, 18.0), (6.2, 12.0), (11.3, 12.0)]
    size = (round(13.5 * unit) + 2, round(21 * unit) + 2)
    big = Image.new("RGBA", (size[0] * k, size[1] * k), (0, 0, 0, 0))
    points = [((x * unit + 1) * k, (y * unit + 1) * k) for x, y in outline]
    draw = ImageDraw.Draw(big)
    draw.polygon(points, fill=(255, 255, 255, 255))
    draw.line(points + [points[0]], fill=(0, 0, 0, 255), width=round(1.15 * unit * k), joint="curve")
    return big.resize(size, Image.LANCZOS)


# ----------------------------------------------------------------------------------------------
# Surfaces
# ----------------------------------------------------------------------------------------------


def short_hex(text: str) -> str:
    return re.sub(r"0x([0-9a-fA-F]{4})[0-9a-fA-F]{32,}([0-9a-fA-F]{4})", r"0x\1…\2", text)


class Page:
    """One picture of a web surface with the rectangles of its elements (CSS pixels)."""

    def __init__(self, name: str):
        record = json.loads((WEB / f"{name}.json").read_text())
        self.name, self.url, self.regions, self.k = name, record["url"], record["regions"], record["scale"]
        self.size = record["page"]
        self.captured = record["capturedAt"]
        self._image = None

    @property
    def image(self) -> Image.Image:
        if self._image is None:
            self._image = Image.open(WEB / f"{self.name}.png").convert("RGB")
        return self._image

    def address(self) -> tuple:
        host, _, path = self.url.split("://")[1].partition("/")
        path = short_hex(path.split("#")[0].split("?")[0])
        if host == "github.com":
            parts = path.split("/")
            host, path = "/".join([host, *parts[:2]]), parts[-1] if len(parts) > 2 and "." in parts[-1] else "README.md"
        elif host.endswith("github.io"):
            path = path.rstrip("/")
        return (host, "/" + path) if path else (host,)

    @lru_cache(maxsize=64)
    def frame(self, cx: float, cy: float, scale: float) -> Image.Image:
        k, half_w, half_h = self.k, W / 2 / scale, H / 2 / scale
        box = ((cx - half_w) * k, (cy - half_h) * k, (cx + half_w) * k, (cy + half_h) * k)
        if box[0] < -0.5 or box[1] < -0.5 or box[2] > self.image.width + 0.5 or box[3] > self.image.height + 0.5:
            raise SystemExit(f"{self.name}: the view {box} is outside the picture {self.image.size}")
        box = (max(box[0], 0), max(box[1], 0), min(box[2], self.image.width), min(box[3], self.image.height))
        return self.image.resize((W, H), Image.LANCZOS, box=box)


class Pages:
    def __init__(self):
        self._pages = {}

    def get(self, name: str) -> Page:
        if name not in self._pages:
            if not (WEB / f"{name}.json").exists():
                raise SystemExit(f"no picture of the surface {name}: run `python demo/surfaces.py {name}`")
            self._pages[name] = Page(name)
        return self._pages[name]

    def region(self, surface: str, spec) -> tuple:
        """The rectangle (x, y, width, height) of a region: a name, a list of names (their union), or
        (name, changes) with `w`, `h` (another width or height) and `up` (more above)."""
        if isinstance(spec, list):
            rects = [self.region(surface, one) for one in spec]
            x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)
            return (x0, y0, max(r[0] + r[2] for r in rects) - x0, max(r[1] + r[3] for r in rects) - y0)
        changes = {}
        if isinstance(spec, tuple):
            spec, changes = spec
        own = self.get(surface).regions
        if spec in own:
            x, y, w, h = own[spec]
        else:
            other, _, name = spec.partition(":")
            if not name or name not in self.get(other).regions:
                raise SystemExit(f"shots.py: the surface {surface} has no region {spec!r}")
            x, y, w, h = self.get(other).regions[name]
        w, h = changes.get("w", w), changes.get("h", h)
        up = changes.get("up", 0)
        return (x, y - up, w, h + up)


class Terminal:
    """The screen recording of one command, and the times of its output lines in the recording."""

    FIT = (128, 0, 1664, 936)  # the place of the recording in the picture: above the subtitles

    def __init__(self, name: str):
        self.name, self.path = name, FOOTAGE / f"{name}.mkv"
        self.meta = json.loads((FOOTAGE / f"{name}.json").read_text())
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(self.path)], capture_output=True, text=True, check=True)
        self.duration = float(probe.stdout)
        # The typescript: each output of the terminal with its time; the recorder started a moment later.
        data = (FOOTAGE / f"{name}.typescript").read_bytes()
        place, clock, offset = data.index(b"\n") + 1, 0.0, self.meta["recorderStart"] - self.meta["terminalStart"]
        self.events = []
        for line in (FOOTAGE / f"{name}.timing").read_text().splitlines():
            delay, count = line.split()[-2:]
            clock += float(delay)
            chunk = data[place:place + int(count)].decode("utf-8", "replace")
            place += int(count)
            self.events.append((clock - offset, re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", chunk)))
        self._reader, self._next, self._last = None, None, None

    def when(self, words: str, n: int = 0) -> float:
        times = [t for t, text in self.events if words in text]
        if len(times) <= n:
            raise SystemExit(f"{self.name}: the terminal output has no line {words!r} ({n})")
        return times[n]

    def lines(self, seconds: float) -> list:
        """The lines that the terminal had shown at a time of the recording, oldest first."""
        text = "".join(chunk for t, chunk in self.events if t <= seconds)
        return [line for line in (part.split("\r")[-1] if part.split("\r")[-1] else part.replace("\r", "")
                                  for part in text.split("\n")) if line.strip()]

    def line_box(self, seconds: float, words: str) -> tuple:
        """The rectangle in the picture (x, y, width, height) of the line with these words, at a time of
        the recording. It comes from the picture itself: the rows with text, from the bottom, are the last
        lines that the terminal showed."""
        picture = self.frame(seconds).crop((self.FIT[0], self.FIT[1], self.FIT[0] + self.FIT[2], self.FIT[3]))
        mask = picture.convert("L").point(lambda v: 255 if v > 70 else 0)
        rows = [mask.crop((0, y, mask.width, y + 1)).getbbox() is not None for y in range(mask.height)]
        bands, start = [], None
        for y, lit in enumerate(rows + [False]):
            if lit and start is None:
                start = y
            elif not lit and start is not None:
                bands.append((start, y))
                start = None
        shown = self.lines(seconds)[-len(bands):]
        matches = [n for n, line in enumerate(shown) if words in line]
        if len(matches) != 1 or len(shown) != len(bands):
            raise SystemExit(f"{self.name}: no single line {words!r} on the screen at {seconds:.2f} s")
        top, bottom = bands[matches[0]]
        left, _, right, _ = mask.crop((0, top, mask.width, bottom)).getbbox()
        return (self.FIT[0] + left, self.FIT[1] + top, right - left, bottom - top)

    def frame(self, seconds: float) -> Image.Image:
        """The picture of the recording at a time of the recording. The calls go forward in time; a jump
        starts the decoder again at that time."""
        index = min(max(round(seconds * FPS), 0), int(self.duration * FPS) - 1)
        if self._last is not None and self._last[0] == index:
            return self._last[1]
        if self._reader is None or index != self._next:
            if self._reader is not None:
                self._reader.kill()
            self._reader = subprocess.Popen(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", f"{index / FPS:.4f}", "-i", str(self.path),
                 "-vf", f"scale={self.FIT[2]}:{self.FIT[3]}:flags=lanczos", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        raw = self._reader.stdout.read(self.FIT[2] * self.FIT[3] * 3)
        if len(raw) < self.FIT[2] * self.FIT[3] * 3:
            raise SystemExit(f"{self.name}: no picture at {seconds:.2f} s")
        self._next = index + 1
        picture = Image.new("RGB", (W, H), CANVAS)
        picture.paste(Image.frombytes("RGB", self.FIT[2:], raw), self.FIT[:2])
        self._last = (index, picture)
        return picture


# ----------------------------------------------------------------------------------------------
# Shots
# ----------------------------------------------------------------------------------------------


def smooth(u: float) -> float:
    u = min(max(u, 0.0), 1.0)
    return u * u * u * (u * (u * 6 - 15) + 10)


class Shot:
    def __init__(self, spec: dict, timeline: Timeline, pages: Pages, end: float):
        self.spec, self.id, self.pages, self.t = spec, spec["id"], pages, timeline
        self.start, self.end = timeline.at(spec["start"]), end
        self.surface = spec["surface"]
        self.is_terminal = self.surface.startswith("terminal:")
        self.frozen = None
        if self.is_terminal:
            self._plan_terminal(spec["terminal"])
            return
        self.page = pages.get(self.surface)
        self.views = [(timeline.at(a), d, self._view(v)) for a, d, v in spec["views"]]
        self.states = [(timeline.at(a), name) for a, name in spec.get("states", [])]
        # A mark that would stay after the shot goes with the shot.
        self.marks = [(timeline.at(a), min(timeline.at(b), end), region) for a, b, region in spec.get("marks", [])]
        self.moves = [(timeline.at(a), d, region, click) for a, d, region, click in
                      spec.get("cursor", {}).get("moves", [])]
        for begin, finish, _ in self.marks:
            if self.camera(begin) != self.camera(finish) or begin < self.start or finish <= begin:
                raise SystemExit(f"shots.py: {self.id}: a mark is outside the shot or inside a move of the camera")

    # The camera -----------------------------------------------------------------------------

    def _view(self, view: dict) -> tuple:
        """(centre x, centre y, scale): CSS pixels of the page, and picture pixels for one CSS pixel."""
        page_w, page_h = self.page.size
        if "at" in view:
            scale = view["scale"]
            cx, cy = view["at"][0] + W / 2 / scale, view["at"][1] + H / 2 / scale
        elif "top" in view:
            scale = view["scale"]
            x, y, w, _ = self.pages.region(self.surface, view["top"])
            cx, cy = x + w / 2, y + view["dy"] + H / 2 / scale
        else:
            x, y, w, h = self.pages.region(self.surface, view["fit"])
            pad = view["pad"]
            x, y, w, h = x - pad, y - pad, w + 2 * pad, h + 2 * pad
            scale = min((SAFE[2] - SAFE[0]) / w, (SAFE[3] - SAFE[1]) / h)
            scale = min(max(scale, view["least"]), view["most"])
            # The centre of the regions at the centre of the area above the subtitles.
            cx = x + w / 2 + (W / 2 - (SAFE[0] + SAFE[2]) / 2) / scale
            cy = y + h / 2 + (H / 2 - (SAFE[1] + SAFE[3]) / 2) / scale
        if scale < W / page_w:
            raise SystemExit(f"shots.py: {self.id}: the scale {scale} shows more than the width of the page")
        cx = min(max(cx, W / 2 / scale), page_w - W / 2 / scale)
        cy = min(max(cy, H / 2 / scale), page_h - H / 2 / scale)
        return (round(cx, 3), round(cy, 3), round(scale, 4))

    def camera(self, t: float) -> tuple:
        current = self.views[0][2]
        for begin, seconds, view in self.views[1:]:
            if t >= begin + seconds:
                current = view
            elif t > begin:
                u = smooth((t - begin) / seconds)
                scale = math.exp(math.log(current[2]) + (math.log(view[2]) - math.log(current[2])) * u)
                return (current[0] + (view[0] - current[0]) * u, current[1] + (view[1] - current[1]) * u, scale)
        return current

    def on_screen(self, t: float, x: float, y: float) -> tuple:
        cx, cy, scale = self.camera(t)
        return ((x - cx) * scale + W / 2, (y - cy) * scale + H / 2)

    def state(self, t: float) -> str:
        name = self.surface
        for begin, other in self.states:
            if t >= begin:
                name = other
        return name

    # The cursor -----------------------------------------------------------------------------

    def cursor(self, t: float):
        if not self.moves:
            return None
        here = self.spec["cursor"]["from"]
        for arrive, seconds, region, _ in self.moves:
            x, y, w, h = self.pages.region(self.surface, region)
            there = self.on_screen(t, x + w * 0.42, y + h * 0.56)
            if t >= arrive:
                here = there
                continue
            begin = arrive - seconds
            if t <= begin:
                return here
            # A path with a small curve, slow at its start and at its end, as a hand moves a mouse.
            u = smooth((t - begin) / seconds)
            dx, dy = there[0] - here[0], there[1] - here[1]
            bend = 0.11 * math.hypot(dx, dy)
            nx, ny = (-dy, dx) if dx >= 0 else (dy, -dx)
            norm = math.hypot(nx, ny) or 1.0
            p1 = (here[0] + dx * 0.3 + nx / norm * bend, here[1] + dy * 0.3 + ny / norm * bend)
            p2 = (here[0] + dx * 0.78 + nx / norm * bend * 0.45, here[1] + dy * 0.78 + ny / norm * bend * 0.45)
            v = 1 - u
            return (v ** 3 * here[0] + 3 * v * v * u * p1[0] + 3 * v * u * u * p2[0] + u ** 3 * there[0],
                    v ** 3 * here[1] + 3 * v * v * u * p1[1] + 3 * v * u * u * p2[1] + u ** 3 * there[1])
        return here

    # The terminal ---------------------------------------------------------------------------

    def _plan_terminal(self, plan: dict) -> None:
        """The pieces of the recording: (time in the video, time in the recording). Inside a piece the recording
        runs at real speed. Between two pieces the wait for the node is cut."""
        at, self.terminal = self.t.at, Terminal(self.surface.split(":")[1])
        term, meta = self.terminal, self.terminal.meta
        typing, pokes, real = at(plan["typing"]), at(plan["pokes"]), at(plan["real"])
        typed, entered = meta["typed"] - meta["recorderStart"], meta["entered"] - meta["recorderStart"]
        pieces = [(typing, typed - 0.35), (pokes - 0.9, term.when("capture: arm C") - 0.3)]
        if pokes - 0.9 - typing < entered - typed + 0.45:
            raise SystemExit(f"shots.py: {self.id}: the cut after the typing comes before the Enter key")
        # One wait for each poke() with a callback before the debt event. A capture can have none.
        waits = sum(1 for _, text in term.events if "callback Pong(" in text)
        step = (real - pokes) / max(waits, 1)
        for n in range(waits):
            pieces.append((pokes + n * step, term.when("poke()", n) - 0.2))
            pieces.append((pokes + (n + 0.5) * step, term.when("callback Pong(", n) - 0.15))
        # From here to the end: one piece, at real speed. The line of the debt event comes at `debtEventAt`.
        start_of_real = term.when("1 debt event") - (at(plan["debtEventAt"]) - real)
        if start_of_real <= pieces[-1][1]:
            raise SystemExit(f"shots.py: {self.id}: the real-speed part would start before the last cut")
        pieces.append((real, start_of_real))
        self.pieces, self.real_from = pieces, real
        self.cut_from = pokes - 0.9
        self.real_span = (start_of_real, term.when("4 debt() is 0"))
        # After the end of the recording the picture is its last one: the screen of the prompt, which is still.
        if term.duration < term.when("capture: saved") + 1.0:
            raise SystemExit(f"shots.py: {self.id}: the recording ends before the screen is still")
        # The marks of the four steps: on the screen of the end of the capture, which does not change again.
        still = term.when("capture: saved") + 0.25
        self.line_marks = []
        for begin, finish, words in plan.get("marks", []):
            if self.recording_time_of(pieces, at(begin)) < still:
                raise SystemExit(f"shots.py: {self.id}: the mark {words!r} starts before the screen is still")
            self.line_marks.append((at(begin), min(at(finish), self.end), term.line_box(still + 0.75, words)))

    @staticmethod
    def recording_time_of(pieces: list, t: float) -> float:
        current = pieces[0]
        for piece in pieces:
            if t >= piece[0] - 1e-6:
                current = piece
        return current[1] + (t - current[0])

    def recording_time(self, t: float) -> float:
        return self.recording_time_of(self.pieces, t)

    # The picture ----------------------------------------------------------------------------

    def chip(self, t: float):
        if not self.is_terminal:
            return self.page.address()
        if t >= self.real_from:
            return ("real speed", "no cut")
        return ("waits cut",) if t >= self.cut_from else None

    def render(self, t: float) -> Image.Image:
        """The picture of the shot at a time of the video, with its marks and its cursor. After the end of
        the shot it is the last picture."""
        if t >= self.end and self.frozen is not None:
            return self.frozen
        t = min(max(t, self.start), self.end)
        if self.is_terminal:
            picture = self.terminal.frame(self.recording_time(t)).copy()
            for begin, finish, (x, y, w, h) in self.line_marks:
                draw_mark(picture, t, begin, finish, (x - 14, y - 9, w + 28, h + 18))
        else:
            cx, cy, scale = self.camera(t)
            picture = self.pages.get(self.state(t)).frame(cx, cy, scale).copy()
            for begin, finish, region in self.marks:
                x, y, w, h = self.pages.region(self.surface, region)
                left, top = self.on_screen(t, x, y)
                draw_mark(picture, t, begin, finish,
                          (round(left) - 7, round(top) - 6, round(w * scale) + 14, round(h * scale) + 12))
            place = self.cursor(t)
            if place is not None:
                arrow = cursor_sprite()
                picture.paste(arrow, (round(place[0]) - 1, round(place[1]) - 1), arrow)
        chip = self.chip(t)
        if chip:
            sprite = chip_sprite(tuple(chip))
            picture.paste(sprite, (36, H - 36 - sprite.height), sprite)
        if t >= self.end - 1e-6:
            self.frozen = picture
        return picture


@lru_cache(maxsize=64)
def mark_sprite(width: int, height: int) -> Image.Image:
    return panel(width, height, outline=ACCENT, stroke=3, radius=9)


def draw_mark(picture: Image.Image, t: float, begin: float, finish: float, box: tuple) -> None:
    """A thin outline around one element. It enters and leaves with a short fade."""
    alpha = min(smooth((t - begin) / 0.18), smooth((finish - t) / 0.18))
    if alpha <= 0:
        return
    outline = mark_sprite(box[2], box[3])
    if alpha < 1:
        outline = outline.copy()
        outline.putalpha(outline.getchannel("A").point(lambda v: round(v * alpha)))
    picture.paste(outline, box[:2], outline)


# ----------------------------------------------------------------------------------------------
# The cut
# ----------------------------------------------------------------------------------------------


class Cut:
    def __init__(self):
        self.timeline = Timeline()
        self.pages = Pages()
        specs = shot_list.SHOTS
        starts = [self.timeline.at(spec["start"]) for spec in specs]
        if starts != sorted(starts):
            raise SystemExit("shots.py: the shots are not in the order of their start times")
        self.shots = [Shot(spec, self.timeline, self.pages, end)
                      for spec, end in zip(specs, starts[1:] + [self.timeline.total])]
        self.cues = build_cues(self.timeline)
        self.total = self.timeline.total
        if self.total >= LIMIT:
            raise SystemExit(f"the cut is {self.total:.1f} s: the limit is less than {LIMIT:g} s")
        chips = max(chip_sprite(tuple(shot.page.address())).width for shot in self.shots if not shot.is_terminal)
        if any(b["start"] < a["end"] or a["end"] - a["start"] < 0.8 for a, b in zip(self.cues, self.cues[1:])):
            raise SystemExit("two subtitles are on the screen at one time, or one is shorter than 0.8 s")
        for cue in self.cues:
            if len(cue["text"]) > CUE_CHARACTERS + 4 or (W - subtitle_sprite(cue["text"]).width) / 2 < 36 + chips + 16:
                raise SystemExit(f"the subtitle {cue['text']!r} is too long for one line beside the address")

    def frame(self, t: float) -> Image.Image:
        index = max(i for i, shot in enumerate(self.shots) if shot.start <= t + 1e-9)
        shot = self.shots[index]
        picture = shot.render(t)
        if index and t - shot.start < FADE:
            before = self.shots[index - 1].render(t)
            picture = Image.blend(before, picture, smooth((t - shot.start) / FADE))
        else:
            picture = picture.copy()
        for cue in self.cues:
            if cue["start"] <= t < cue["end"]:
                sprite = subtitle_sprite(cue["text"])
                picture.paste(sprite, ((W - sprite.width) // 2, H - 36 - sprite.height), sprite)
        return picture

    # Sound ----------------------------------------------------------------------------------

    def mix(self, path: Path) -> None:
        """The voice clips at their times, in one file, at -16 LUFS."""
        clips = list(self.timeline.clips.values())
        inputs, graph = [], []
        for n, clip in enumerate(clips):
            inputs += ["-i", str(clip["audio"])]
            delay = round(clip["start"] * 1000)
            graph.append(f"[{n}]aresample=48000,adelay={delay}|{delay}[a{n}]")
        graph.append("".join(f"[a{n}]" for n in range(len(clips)))
                     + f"amix=inputs={len(clips)}:normalize=0:dropout_transition=0,apad,atrim=0:{self.total:.3f},"
                       "highpass=f=70,loudnorm=I=-16:TP=-1.5:LRA=11,"
                       "aformat=sample_rates=48000:channel_layouts=stereo[out]")
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", *inputs, "-filter_complex", ";".join(graph),
                        "-map", "[out]", "-c:a", "pcm_s16le", str(path)], check=True)

    # Documents ------------------------------------------------------------------------------

    def spoken_in(self, begin: float, end: float) -> list:
        """The narration inside a time span: (text, text as sent) of each clip, cut at words."""
        parts = []
        for clip in self.timeline.clips.values():
            text, places = clip["text"], clip["places"]
            words = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
            inside = [(a, b) for a, b in words if begin <= clip["start"] + clip["starts"][places[a]] < end]
            if inside:
                a, b = inside[0][0], inside[-1][1]
                parts.append((text[a:b], clip["sent"][places[a]:places[b]]))
        return parts

    def surface_text(self, shot: Shot) -> str:
        if shot.is_terminal:
            return f"Terminal: `{shot.terminal.meta['command']}`"
        return shot.page.url

    def write_script(self, path: Path) -> None:
        voice = self.timeline.voice
        lines = [
            "# Demo video: shot list and narration", "",
            "This file is written by `python demo/compose.py` from `demo/shots.py`, `demo/narration.json` and the "
            "footage. Do not edit it by hand: change the sources and build the cut again.", "",
            f"Length of this cut: {self.total:.1f} s (the limit is less than {LIMIT:g} s).", "",
            "## Voice", "",
            f"- Engine: {voice['engine']}, text to speech with the time of each character.",
            f"- Voice: {voice['name']} (voice ID `{voice['voiceId']}`), model `{voice['model']}`, "
            f"seed {voice['seed']}.",
            "- Settings, the same for each clip: "
            + ", ".join(f"`{key}` {value}" for key, value in voice["settings"].items())
            + f"; output `{voice['outputFormat']}`.",
            "- Each clip gets the text of the clip before it and after it as context "
            "(`previous_text`, `next_text`).", "",
            "## Spoken forms", "",
            "A number is written in words in the narration text. A term has one spoken form:", "",
            "| Term | As sent to the voice engine |", "|---|---|",
            *[f"| {term} | {form} |" for term, form in self.timeline.spoken.items()], "",
            "`depositTo`, `vaultUp` and `callback proxy` are on screen and are not spoken in this cut.", "",
            "## Shots", "",
            "| Start | Duration | Part | Surface (URL or command) | On-screen action | Narration | "
            "Text as sent to the voice engine, if it differs |", "|---|---|---|---|---|---|---|",
        ]
        for shot in self.shots:
            said = self.spoken_in(shot.start, shot.end)
            text = " ".join(part for part, _ in said)
            sent = " ".join(sent for part, sent in said if part != sent)
            lines.append(f"| {shot.start:.1f} s | {shot.end - shot.start:.1f} s | "
                         f"{shot.spec['part']}. {shot_list.PARTS[shot.spec['part']]} | {self.surface_text(shot)} | "
                         f"{shot.spec['action']} | {text} | {sent or '-'} |")
        lines += ["", "The first sentence (part 1) is spoken over the first shot of part 2.", ""]
        path.write_text("\n".join(lines))

    def write_timeline(self, path: Path) -> None:
        record = {
            "total": round(self.total, 3),
            "clips": [{"id": c["id"], "start": round(c["start"], 3), "end": round(c["end"], 3), "text": c["text"]}
                      for c in self.timeline.clips.values()],
            "shots": [{"id": s.id, "start": round(s.start, 3), "end": round(s.end, 3), "surface": s.surface,
                       "url": None if s.is_terminal else s.page.url,
                       "capturedAt": None if s.is_terminal else s.page.captured} for s in self.shots],
            "cues": [{"start": round(c["start"], 3), "end": round(c["end"], 3), "text": c["text"]} for c in self.cues],
        }
        terminal = next((s for s in self.shots if s.is_terminal), None)
        if terminal is not None:
            record["terminal"] = {"pieces": [[round(a, 3), round(b, 3)] for a, b in terminal.pieces],
                                  "realSpeed": {"videoFrom": round(terminal.real_from, 3),
                                                "recordingFrom": round(terminal.real_span[0], 3),
                                                "debtZeroAtRecording": round(terminal.real_span[1], 3)}}
        path.write_text(json.dumps(record, indent=1) + "\n")


def encode(cut: Cut, begin: float, end: float, silent: Path) -> None:
    writer = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
         "-r", str(FPS), "-i", "-", "-vf", "scale=out_color_matrix=bt709:out_range=tv", "-c:v", "libx264",
         "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709",
         "-color_trc", "bt709", "-g", "60", "-movflags", "+faststart", str(silent)], stdin=subprocess.PIPE)
    first, last = round(begin * FPS), round(end * FPS)
    for n in range(first, last):
        writer.stdin.write(cut.frame(n / FPS).tobytes())
        if (n - first) % 300 == 0:
            print(f"  picture {n / FPS:6.1f} s of {end:.1f} s", flush=True)
    writer.stdin.close()
    if writer.wait() != 0:
        raise SystemExit("ffmpeg did not write the video")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(BUILD / "lifeline-demo.mp4"))
    parser.add_argument("--from", dest="begin", type=float)
    parser.add_argument("--to", dest="end", type=float)
    parser.add_argument("--frames", help="times in seconds, with commas: write pictures and no video")
    args = parser.parse_args()
    BUILD.mkdir(parents=True, exist_ok=True)
    cut = Cut()
    print(f"cut: {cut.total:.2f} s, {len(cut.shots)} shots, {len(cut.cues)} subtitles")
    if args.frames:
        (BUILD / "frames").mkdir(exist_ok=True)
        for value in args.frames.split(","):
            cut.frame(float(value)).save(BUILD / "frames" / f"{float(value):07.2f}.png")
        return 0
    write_srt(cut.cues, HERE / "subtitles.srt")
    cut.write_script(BUILD / "script.md")
    cut.write_timeline(BUILD / "timeline.json")
    part = args.begin is not None or args.end is not None
    out = Path(args.out)
    silent = out.with_suffix(".silent.mp4")
    encode(cut, args.begin or 0.0, min(args.end or cut.total, cut.total), out if part else silent)
    if part:
        return 0
    cut.mix(BUILD / "narration.wav")
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(silent), "-i",
                    str(BUILD / "narration.wav"), "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac",
                    "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(out)], check=True)
    silent.unlink()
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
