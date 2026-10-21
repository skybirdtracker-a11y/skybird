"""Renders the four daily reels (1080x1920, 30 fps, H.264, no audio) in the SkyBird - Omaha
board style (docs/SKYBIRD-BRAND-SPEC.md; the reference is skybird-example-commercial-9x16.mp4).

  Departures 7.5 s  county-level zoom on the field pulls back to the US; planes leave in real order
  Arrivals   7.5 s  whole map; planes land at their real times; camera flies back into Omaha
  Stats      6.0 s  skyline band + badge, headline, 4 stat tiles, top-5 cities, 4 fact tiles
  End card   3.5 s  skyline band, navy SkyBird circle, end line, sub-line, follow pill
Military (Offutt) is counts only: Offutt zoom 5.5 s -> stats with weekly bars 6 s -> end card.

    python -m skybird render [--date YYYY-MM-DD] [--demo]
"""
from __future__ import annotations

import gzip
import json
import math
from datetime import date, datetime
from pathlib import Path

import numpy as np

from . import flights as fl
from .config import Config
from .state import log

W, H, FPS = 1080, 1920, 30
PX = 0.72  # matplotlib points per pixel at dpi=100

NAVY, BIRD, SKY, ORANGE, MINT = "#014291", "#0999EA", "#1E8BE0", "#F28F23", "#AEE1D8"
CLOUD, INK, MUTED, LINE, ICE, SEA, FOREIGN, WHITE = ("#F6F2E7", "#0A2540", "#46607A", "#D9DEE5",
                                                     "#E6F3FC", "#F3F7FB", "#EEF2F6", "#FFFFFF")
FILES = {"commercial": "post1_commercial.mp4", "private": "post2_private.mp4",
         "military": "post3_military.mp4", "all": "post4_all.mp4"}
ASSETS = Path(__file__).parent / "assets"

POSTS = {
    "commercial": {"kicker": "EPPLEY · OMA", "fields": ["KOMA"],
                   "dep": ("Where did Omaha", "fly yesterday?"), "arr": ("And who flew", "into OMA."),
                   "stats": ("Eppley, by the", "numbers."), "colors": {"dep": BIRD, "arr": ORANGE}},
    "private": {"kicker": "PRIVATE JETS · METRO", "fields": ["KOMA", "KMLE", "KCBF"],
                "dep": ("Where Omaha's", "private jets went."), "arr": ("Private jets", "flying in."),
                "stats": ("Private jets, by", "the numbers."), "colors": {"dep": ORANGE, "arr": NAVY}},
    "military": {"kicker": "OFFUTT · OFF · MILITARY", "fields": ["KOFF"],
                 "dep": ("What flew at", "Offutt yesterday?"), "stats": ("Offutt, by the", "numbers.")},
    "all": {"kicker": "ALL AIRPORTS · METRO", "fields": ["KOMA", "KMLE", "KCBF", "KOFF"],
            "dep": ("Every flight", "out of Omaha."), "arr": ("And every flight", "coming home."),
            "stats": ("The whole metro,", "by the numbers."),
            "colors": {"airline": BIRD, "bizjet": ORANGE, "prop": NAVY}},
}
TOWNS = [("Omaha", -95.9345, 41.2565, "big"), ("Lincoln", -96.7026, 40.8136, "big"),
         ("Council Bluffs", -95.8608, 41.2619, ""), ("Bellevue", -95.9145, 41.1370, ""),
         ("Papillion", -96.0422, 41.1544, "left"), ("Fremont", -96.4981, 41.4333, "left"),
         ("Blair", -96.1253, 41.5444, ""), ("Glenwood", -95.7425, 41.0469, "")]
PAPER_PLANE = ([(4, 38), (116, 4), (62, 76), (50, 48), (4, 38)], [(50, 48), (116, 4)])  # 120x80 viewBox


def ease(t: float) -> float:  # cubic in-out
    t = max(0.0, min(1.0, t))
    return 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def pop(t: float) -> float:  # 0..1 with slight overshoot
    t = max(0.0, min(1.0, t))
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


# ---- projection (Albers conic, km) ------------------------------------------------------

R_E, LON0, LAT0, SP1, SP2 = 6371.0, -96.0, 37.5, 29.5, 45.5
_n = (math.sin(math.radians(SP1)) + math.sin(math.radians(SP2))) / 2
_C = math.cos(math.radians(SP1)) ** 2 + 2 * _n * math.sin(math.radians(SP1))
_rho0 = R_E * math.sqrt(_C - 2 * _n * math.sin(math.radians(LAT0))) / _n


def proj(lon, lat):
    lon, lat = np.asarray(lon, float), np.asarray(lat, float)
    rho = R_E * np.sqrt(np.maximum(_C - 2 * _n * np.sin(np.radians(lat)), 0)) / _n
    th = _n * np.radians(lon - LON0)
    return rho * np.sin(th), _rho0 - rho * np.cos(th)


_MAP = None


def mapdata():
    global _MAP
    if _MAP is None:
        with gzip.open(ASSETS / "map_light.json.gz", "rb") as f:
            raw = json.loads(f.read())
        _MAP = {k: [np.column_stack(proj(*np.array(r).T)) for r in v if len(r) > 2] for k, v in raw.items()}
    return _MAP


# ---- fonts -----------------------------------------------------------------------------

class Fonts:
    def __init__(self):
        from matplotlib.font_manager import FontProperties as FP
        f = ASSETS / "fonts"
        self.head = FP(fname=str(f / "Archivo-900-Italic.ttf"))
        self.body = FP(fname=str(f / "Archivo-600.ttf"))
        self.mono = FP(fname=str(f / "JetBrainsMono-700.ttf"))
        self.mono6 = FP(fname=str(f / "JetBrainsMono-600.ttf"))


# ---- the canvas: map axes underneath, pixel-space UI axes on top ---------------------------------

class Canvas:
    def __init__(self, fonts: Fonts):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import patches, collections
        self.plt, self.P, self.C, self.f = plt, patches, collections, fonts
        self.fig = plt.figure(figsize=(W / 100, H / 100), dpi=100, facecolor=SEA)
        self.map = self.fig.add_axes([0, 0, 1, 1], zorder=1)
        self.map.set_facecolor(SEA)
        self.map.axis("off")
        self.ui = self.fig.add_axes([0, 0, 1, 1], zorder=5)
        self.ui.set_xlim(0, W), self.ui.set_ylim(H, 0), self.ui.axis("off")
        self.ui.patch.set_alpha(0)
        self._r = self.fig.canvas.get_renderer()

    def frame(self) -> bytes:
        self.fig.canvas.draw()
        return np.asarray(self.fig.canvas.buffer_rgba())[:, :, :3].tobytes()

    def close(self):
        self.plt.close(self.fig)

    # text helpers (sizes in pixels)
    def text(self, x, y, s, px, color, font, ha="left", va="baseline", alpha=1.0, z=10, ax=None):
        return (ax or self.ui).text(x, y, s, fontsize=px * PX, color=color, fontproperties=font,
                                    ha=ha, va=va, alpha=alpha, zorder=z)

    def width(self, s, px, font) -> float:
        t = self.ui.text(0, 0, s, fontsize=px * PX, fontproperties=font)
        w = t.get_window_extent(renderer=self._r).width
        t.remove()
        return w

    def spaced(self, x, y, s, px, color, font=None, spacing=3.0, ha="left", z=10, alpha=1.0):
        """Mono text with letter-spacing (JetBrains Mono advance = 0.6 em)."""
        font = font or self.f.mono
        adv = 0.6 * px + spacing
        total = adv * len(s) - spacing
        x0 = x - total if ha == "right" else x - total / 2 if ha == "center" else x
        return [self.text(x0 + i * adv, y, ch, px, color, font, z=z, alpha=alpha) for i, ch in enumerate(s) if ch != " "]

    def rbox(self, x, y, w, h, r, fc, ec="none", lw=0, z=8, alpha=1.0, shadow=False):
        arts = []
        if shadow:
            for i, a in enumerate((0.05, 0.04, 0.03)):
                d = 4 + i * 5
                arts.append(self.ui.add_patch(self.P.FancyBboxPatch(
                    (x + r - d / 2, y + r + 6), w - 2 * r + d, h - 2 * r + d / 2,
                    boxstyle=f"round,pad={r}", fc=INK, ec="none", alpha=a * alpha, zorder=z - 1)))
        arts.append(self.ui.add_patch(self.P.FancyBboxPatch(
            (x + r, y + r), max(w - 2 * r, 0.1), max(h - 2 * r, 0.1), boxstyle=f"round,pad={r}",
            fc=fc, ec=ec, lw=lw * PX, alpha=alpha, zorder=z)))
        return arts

    def paper_plane(self, x, y, w, fc=WHITE, ec=BIRD, lw=2.5, z=12, alpha=1.0):
        """The brand paper airplane, top-left at (x, y), width w px."""
        k = w / 120
        body, fold = PAPER_PLANE
        a = self.ui.add_patch(self.P.Polygon([(x + px_ * k, y + py * k) for px_, py in body], closed=True,
                                             fc=fc, ec=ec, lw=lw * PX, joinstyle="round", zorder=z, alpha=alpha))
        b, = self.ui.plot([x + fold[0][0] * k, x + fold[1][0] * k], [y + fold[0][1] * k, y + fold[1][1] * k],
                          color=ec, lw=lw * PX, zorder=z + 0.1, solid_capstyle="round", alpha=alpha)
        return [a, b]

    def image(self, path, x, y, w, h, z=3, crop=None, alpha=1.0):
        from PIL import Image
        img = Image.open(path).convert("RGBA")
        if crop:
            img = img.crop(crop)
        return self.ui.imshow(np.asarray(img), extent=(x, x + w, y + h, y), zorder=z, alpha=alpha, interpolation="bilinear")


# ---- airliner silhouette for moving planes ------------------------------------------------

def _plane_path():
    from matplotlib.path import Path as MPath
    half = [(0, 1.0), (0.09, 0.86), (0.1, 0.3), (0.95, -0.02), (0.95, -0.16), (0.1, -0.02), (0.08, -0.6),
            (0.36, -0.78), (0.36, -0.9), (0.05, -0.84), (0, -0.95)]
    pts = half + [(-x, y) for x, y in reversed(half[1:-1])]
    return MPath(pts + [pts[0]], closed=True)


PLANE = None


def plane_marker(angle_deg):
    global PLANE
    from matplotlib.markers import MarkerStyle
    from matplotlib.transforms import Affine2D
    if PLANE is None:
        PLANE = _plane_path()
    return MarkerStyle(PLANE, transform=Affine2D().rotate_deg(angle_deg))


# ---- camera ---------------------------------------------------------------------------------

VIEW_TOP, VIEW_BOTTOM = 470, 1370   # between the title card and the HUD


class Camera:
    def __init__(self, canvas: Canvas):
        self.c = canvas
        self.x0 = self.x1 = self.y0 = self.y1 = 0

    def set(self, cx, cy, span):
        """Center (km) placed at the middle of the visible map band; span = km across the width."""
        sy = span * H / W
        mid_px = (VIEW_TOP + VIEW_BOTTOM) / 2
        self.x0, self.x1 = cx - span / 2, cx + span / 2
        top = cy + (mid_px / H) * sy
        self.y0, self.y1 = top - sy, top
        self.c.map.set_xlim(self.x0, self.x1)
        self.c.map.set_ylim(self.y0, self.y1)
        self.span = span

    def px(self, x, y):
        return ((x - self.x0) / (self.x1 - self.x0) * W, (self.y1 - y) / (self.y1 - self.y0) * H)

    @staticmethod
    def fit(pts, min_span=900):
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        wspan = (max(xs) - min(xs)) * 1.18 + 120
        hspan = ((max(ys) - min(ys)) * 1.12 + 120) * W / (VIEW_BOTTOM - VIEW_TOP) * 1.0
        return cx, cy, max(wspan, hspan, min_span)


def lerp_view(v0, v1, z):
    span = math.exp(math.log(v0[2]) + (math.log(v1[2]) - math.log(v0[2])) * z)
    w = (span - v1[2]) / (v0[2] - v1[2]) if abs(v0[2] - v1[2]) > 1e-6 else 1 - z
    return v1[0] + (v0[0] - v1[0]) * w, v1[1] + (v0[1] - v1[1]) * w, span


# ---- one reel ----------------------------------------------------------------------------------

class Reel:
    def __init__(self, cfg: Config, slot: str, day: date, post: dict, fonts: Fonts, demo: bool,
                 week: list | None = None):
        self.cfg, self.slot, self.day, self.post, self.f, self.demo = cfg, slot, day, post, fonts, demo
        self.spec = POSTS[slot]
        self.s = post["stats"]
        self.week = week or []
        self.handle = cfg.general.get("handle", "@skybirdomaha")
        self.date_label = f"{day.strftime('%a').upper()} · {day.strftime('%b').upper()} {day.day}"
        self.fields = [fl.place(i) for i in self.spec["fields"]]
        hub = self.fields[0]
        self.hub_icao = self.spec["fields"][0]
        self.hub = tuple(float(v) for v in proj(hub["lon"], hub["lat"]))
        self.near = (self.hub[0] - 8, self.hub[1] - 4, 225.0)

    # -- shared pieces
    def _map_base(self, cv: Canvas):
        m = mapdata()
        ax = cv.map
        ax.add_collection(cv.C.PolyCollection(m["land"], fc=FOREIGN, ec="none", zorder=1))
        ax.add_collection(cv.C.PolyCollection(m["states"], fc=ICE, ec=WHITE, lw=1.6 * PX, zorder=2))
        ax.add_collection(cv.C.PolyCollection(m["lakes"], fc=SEA, ec="none", zorder=2.5))
        counties = cv.C.LineCollection(m["counties"], colors="#C3D2E2", lw=1.0 * PX, zorder=2.6)
        ax.add_collection(counties)
        towns = []
        for name, lon, lat, kind in TOWNS:
            x, y = proj(lon, lat)
            if kind == "big":
                t = cv.text(float(x) - 6, float(y), name, 34, "#3A4E66", cv.f.head, ha="right", va="center", z=3, ax=ax)
            else:
                t = cv.text(float(x) + (-2 if kind == "left" else 2), float(y) + (-2 if kind == "left" else 0), name, 22,
                            "#3A4E66", cv.f.body, ha="right" if kind == "left" else "left", va="center", z=3, ax=ax)
            t.set_clip_on(True)
            towns.append(t)
        return counties, towns

    def _zoom_detail(self, counties, towns, span):
        a = max(0.0, min(1.0, (700 - span) / 400))
        counties.set_alpha(a)
        for t in towns:
            t.set_alpha(a)

    def _title(self, cv: Canvas, which: str):
        line1, line2 = self.spec[which]
        accent = ORANGE if which == "arr" and self.slot == "commercial" else BIRD
        kick_color = ORANGE if which == "arr" and self.slot == "commercial" else BIRD
        arts = cv.rbox(45, 150, 990, 300, 30, WHITE, z=20, shadow=True)
        arts += cv.paper_plane(82, 178, 40, fc=WHITE, ec=BIRD, lw=2.4, z=22)
        name_w = cv.width("SkyBird", 34, cv.f.head)
        arts.append(cv.text(134, 207, "SkyBird", 34, NAVY, cv.f.head, z=22))
        arts.append(cv.text(134 + name_w + 10, 207, "- Omaha", 34, BIRD, cv.f.head, z=22))
        arts += cv.spaced(995, 205, self.date_label, 22, NAVY, cv.f.mono6, spacing=3, ha="right", z=22)
        kick = f"{self.spec['kicker']} · {'DEPARTURES' if which == 'dep' else 'ARRIVALS'}" if self.slot != "military" else self.spec["kicker"]
        arts += cv.spaced(82, 250, kick, 21, kick_color, cv.f.mono, spacing=3.2, z=22)
        arts.append(cv.text(78, 330, line1, 76, NAVY, cv.f.head, z=22))
        arts.append(cv.text(78, 410, line2, 76, accent, cv.f.head, z=22))
        return arts

    def _hud(self, cv: Canvas, label: str, total: int, clock: bool = True):
        x = 50
        parts = {}
        if clock:
            cv.rbox(x, 1395, 420, 140, 22, WHITE, z=20, shadow=True)
            cv.spaced(x + 30, 1438, "LOCAL TIME", 18, MUTED, cv.f.mono6, spacing=2.4, z=22)
            parts["clock"] = cv.text(x + 26, 1510, "", 66, NAVY, cv.f.head, z=22)
            x += 440
        w = 330 if total < 1000 else 380
        cv.rbox(x, 1395, w, 140, 22, WHITE, z=20, shadow=True)
        cv.spaced(x + 30, 1438, label, 18, MUTED, cv.f.mono6, spacing=2.4, z=22)
        parts["count"] = cv.text(x + 26, 1510, "0", 66, NAVY, cv.f.head, z=22)
        parts["total"] = cv.text(x + 26, 1510, f"/ {total}", 26, MUTED, cv.f.mono6, z=22)
        parts["x"] = x
        return parts

    def _set_count(self, cv, hud, n):
        hud["count"].set_text(str(n))
        hud["total"].set_x(hud["x"] + 26 + cv.width(str(n), 66, cv.f.head) + 14)

    def _fade(self, cv: Canvas):
        return cv.ui.add_patch(cv.P.Rectangle((0, 0), W, H, fc=CLOUD, ec="none", zorder=90, alpha=0))

    def _sample_stamp(self, cv: Canvas):
        if self.demo:
            cv.text(W / 2, 1600, "SAMPLE DATA · NOT REAL FLIGHTS", 30, "#C0392B", cv.f.mono, ha="center", z=95)

    # -- hub pins
    def _pins(self, cv: Canvas):
        pins = []
        for p in self.fields:
            x, y = (float(v) for v in proj(p["lon"], p["lat"]))
            dot, = cv.map.plot([x], [y], "o", ms=16 * PX, color=NAVY, mec=WHITE, mew=4 * PX, zorder=30)
            ring, = cv.map.plot([x], [y], "o", ms=20 * PX, mfc="none", mec=ORANGE, mew=3 * PX, zorder=29)
            box = cv.rbox(0, 0, 10, 10, 18, NAVY, z=31)[0]
            txt = cv.text(0, 0, p["code"], 24, WHITE, cv.f.mono, ha="center", va="center", z=32)
            pins.append({"x": x, "y": y, "dot": dot, "ring": ring, "box": box, "txt": txt, "code": p["code"]})
        city_box = cv.rbox(0, 0, 10, 10, 18, NAVY, z=31)[0]
        city_txt = cv.text(0, 0, "OMAHA", 24, WHITE, cv.f.mono, ha="center", va="center", z=32)
        return {"pins": pins, "city": (city_box, city_txt)}

    def _update_pins(self, cv, cam: Camera, pins, t):
        multi = len(pins["pins"]) > 1
        zoomed_out = cam.span > 600
        for i, p in enumerate(pins["pins"]):
            px, py = cam.px(p["x"], p["y"])
            phase = (t * 0.85 + i * 0.25) % 1
            p["ring"].set_markersize((20 + 46 * phase) * PX)
            p["ring"].set_alpha(1 - phase)
            show_label = (not multi) or (not zoomed_out)
            if multi and zoomed_out and i > 0:
                p["dot"].set_alpha(0), p["ring"].set_alpha(0)
            else:
                p["dot"].set_alpha(1)
            w = 24 * 0.6 * len(p["code"]) + 34
            bx, by = px - w - 4, py - 62
            _move(p["box"], bx, by, w, 46, 18)
            p["txt"].set_position((bx + w / 2, by + 23))
            p["box"].set_alpha(1 if show_label else 0), p["txt"].set_alpha(1 if show_label else 0)
        cb, ct = pins["city"]
        if multi and zoomed_out:
            p = pins["pins"][0]
            px, py = cam.px(p["x"], p["y"])
            w = 24 * 0.6 * 5 + 34
            _move(cb, px - w - 4, py - 62, w, 46, 18)
            ct.set_position((px - w / 2 - 4, py - 39))
            cb.set_alpha(1), ct.set_alpha(1)
        else:
            cb.set_alpha(0), ct.set_alpha(0)

    # -- labels
    def _make_label(self, cv, code, color):
        w = 0.6 * 24 * len(code) + 32
        box = cv.rbox(0, 0, w, 44, 22, WHITE, ec=color, lw=2.5, z=26)[0]
        txt = cv.text(0, 0, code, 24, INK, cv.f.mono, va="center", z=27)
        cnt = cv.text(0, 0, "", 18, color, cv.f.mono6, va="center", z=27)
        return {"box": box, "txt": txt, "cnt": cnt, "code": code, "w": w, "n": 0, "born": None}

    def _place_label(self, cv, lab, px, py, off, t):
        ox, oy = off
        w = lab["w"] + (0.6 * 18 * (len(f"×{lab['n']}") + 1) if lab["n"] > 1 else 0)
        s = pop((t - lab["born"]) / 0.35) if lab["born"] is not None else 0
        x, y = px + ox - (w if ox < 0 else 0), py + oy - 22
        _move(lab["box"], x, y, w, 44, 21)
        lab["txt"].set_position((x + 16, y + 23))
        lab["cnt"].set_position((x + 16 + 0.6 * 24 * len(lab["code"]) + 8, y + 24))
        lab["cnt"].set_text(f"×{lab['n']}" if lab["n"] > 1 else "")
        a = min(1.0, max(0.0, s))
        for k in ("box", "txt", "cnt"):
            lab[k].set_alpha(a)

    @staticmethod
    def _dodge(anchors, hub_px):
        """Pick an offset per label (right, left, up, down) so pills don't overlap each other or the hub."""
        placed = [(hub_px[0] - 140, hub_px[1] - 70, hub_px[0] + 20, hub_px[1] + 20)]
        out = {}
        for key, (x, y, w) in anchors:
            for ox, oy in ((14, 0), (-14, 0), (14, -34), (-14, -34), (14, 34), (-14, 34), (30, -64), (-30, 64)):
                x0 = x + ox - (w if ox < 0 else 0)
                box = (x0, y + oy - 22, x0 + w, y + oy + 22)
                if box[0] < 18 or box[2] > W - 18 or box[1] < VIEW_TOP - 10 or box[3] > VIEW_BOTTOM + 10:
                    continue
                if box[2] > 940 and box[3] > 1000:
                    continue
                if all(box[2] < b[0] or box[0] > b[2] or box[3] < b[1] or box[1] > b[3] for b in placed):
                    placed.append(box)
                    out[key] = (ox, oy)
                    break
            else:
                out[key] = None
        return out

    # ---- map scene (departures or arrivals) ----
    def map_scene(self, which: str, write, secs: float = 7.5, first_scene: bool = False):
        cv = Canvas(self.f)
        counties, towns = self._map_base(cv)
        cam = Camera(cv)
        moves = [m for m in self.post["moves"] if m["dir"] == which and m["other"] and m["group"] != "military"
                 and m.get("t") and m.get("t0") and m.get("t1")]
        moves.sort(key=lambda m: m["t"])
        total = self.s["departures" if which == "dep" else "arrivals"]
        colors = self.spec["colors"]
        color_of = (lambda m: colors[m["group"]]) if self.slot == "all" else (lambda m: colors[which])
        self._title(cv, which)
        hud = self._hud(cv, "DEPARTURES" if which == "dep" else "ARRIVALS", total)
        if self.slot == "all":
            self._legend(cv)
        pins = self._pins(cv)
        self._sample_stamp(cv)
        fade = self._fade(cv)

        # endpoints + arcs
        ends, routes = {}, []
        per_route = {}
        for m in moves:
            o = m["other"]
            ex, ey = (float(v) for v in proj(o["lon"], o["lat"]))
            mp = m["metro_place"]
            hx, hy = (float(v) for v in proj(mp["lon"], mp["lat"]))
            key = o["code"]
            if key not in ends:
                dot, = cv.map.plot([ex], [ey], "o", ms=11 * PX, mfc=WHITE, mec=color_of(m), mew=2.6 * PX, zorder=12, alpha=0)
                ends[key] = {"x": ex, "y": ey, "color": color_of(m), "label": None, "count": 0, "dot": dot}
            k = per_route.get(key, 0)
            per_route[key] = k + 1
            bend = (0.14 + 0.05 * ((k % 5) - 2) / 2) * (1 if (k % 2 == 0) else 0.8)
            a, b = ((hx, hy), (ex, ey)) if which == "dep" else ((ex, ey), (hx, hy))
            dx, dy = b[0] - a[0], b[1] - a[1]
            cx, cy = (a[0] + b[0]) / 2 - dy * bend, (a[1] + b[1]) / 2 + dx * bend
            tt = np.linspace(0, 1, 90)[:, None]
            pts = (1 - tt) ** 2 * np.array(a) + 2 * (1 - tt) * tt * np.array([cx, cy]) + tt ** 2 * np.array(b)
            col = color_of(m)
            trail, = cv.map.plot([], [], color=col, lw=3 * PX, solid_capstyle="round", zorder=10, alpha=0.95)
            plane, = cv.map.plot([], [], ls="none", marker=plane_marker(0), ms=38 * PX, mfc=NAVY, mec=WHITE,
                                 mew=1.8 * PX, zorder=20)
            routes.append({"m": m, "pts": pts, "trail": trail, "plane": plane, "end": key})

        # timeline: compress the day into the scene
        t_lo = min([m["t"] for m in moves] or [0])
        t_hi = max([m["t"] for m in moves] or [1])
        span_day = max(t_hi - t_lo, 3600)
        run0, run1 = (0.9, 6.9) if which == "dep" else (0.4, 5.6)
        k = (run1 - run0) / span_day
        for r in routes:
            m = r["m"]
            dur = min(2.2, max(0.6, (m["t1"] - m["t0"]) * k))
            if which == "dep":
                r["start"] = run0 + (m["t"] - t_lo) * k
            else:
                r["start"] = run0 + (m["t"] - t_lo) * k - dur
            r["end_t"] = r["start"] + dur

        wide = Camera.fit([self.hub] + [(e["x"], e["y"]) for e in ends.values()]) if ends else (self.hub[0], self.hub[1], 1200)
        cam.set(*wide)
        hub_px = cam.px(*self.hub)
        order = sorted(ends.items(), key=lambda kv: -sum(1 for r in routes if r["end"] == kv[0]))
        offs = self._dodge([(key, (*cam.px(e["x"], e["y"]), 0.6 * 24 * len(key) + 32 + 44)) for key, e in order], hub_px)
        for key, e in ends.items():
            if offs.get(key) is not None:
                e["label"] = self._make_label(cv, key, e["color"])
                e["off"] = offs[key]

        frames = int(secs * FPS)
        for fi in range(frames):
            t = fi / FPS
            if which == "dep":
                v = lerp_view(self.near, wide, ease((t - 1.0) / 2.8))
            else:
                v = lerp_view(wide, self.near, ease((t - 5.6) / 1.7))
            cam.set(*v)
            self._zoom_detail(counties, towns, cam.span)
            self._update_pins(cv, cam, pins, t)
            landed = 0
            for e in ends.values():
                e["count"] = 0
            for r in routes:
                p = (t - r["start"]) / (r["end_t"] - r["start"])
                if p <= 0:
                    r["trail"].set_data([], []), r["plane"].set_data([], [])
                    continue
                if which == "arr":
                    ends[r["end"]]["count"] += 1  # origin label counts flights that have left
                if p >= 1:
                    r["trail"].set_data(r["pts"][:, 0], r["pts"][:, 1])
                    r["trail"].set_alpha(0.45)
                    r["plane"].set_data([], [])
                    landed += 1
                    if which == "dep":
                        ends[r["end"]]["count"] += 1
                    continue
                q = ease(p) * 0.85 + p * 0.15
                i = max(1, int(q * (len(r["pts"]) - 1)))
                seg = r["pts"][: i + 1]
                r["trail"].set_data(seg[:, 0], seg[:, 1]), r["trail"].set_alpha(0.95)
                (x1, y1), (x0, y0) = seg[-1], seg[-2]
                ang = math.degrees(math.atan2(y1 - y0, x1 - x0)) - 90
                r["plane"].set_data([x1], [y1])
                r["plane"].set_marker(plane_marker(ang))
            # labels
            for key, e in ends.items():
                e["dot"].set_alpha(1 if e["count"] > 0 else 0)
                lab = e["label"]
                if not lab:
                    continue
                if e["count"] > 0 and lab["born"] is None:
                    lab["born"] = t
                lab["n"] = e["count"]
                px, py = cam.px(e["x"], e["y"])
                vis_ok = cam.span > 700
                if not vis_ok:
                    for kk in ("box", "txt", "cnt"):
                        lab[kk].set_alpha(0)
                    continue
                self._place_label(cv, lab, px, py, e["off"], t)
            # HUD
            started = sum(1 for r in routes if t >= r["start"]) if which == "dep" else landed
            if which == "dep":
                shown = min(total, started + max(0, total - len(routes)) * (1 if t >= run1 else 0))
                clock_ts = t_lo + (min(max(t, run0), run1) - run0) / k
            else:
                shown = min(total, landed + max(0, total - len(routes)) * (1 if t >= run1 + 0.4 else 0))
                clock_ts = t_lo + (min(max(t, run0), run1) - run0) / k
            self._set_count(cv, hud, shown)
            hud["clock"].set_text(datetime.fromtimestamp(clock_ts, self.cfg.tz).strftime("%I:%M %p").lstrip("0") if moves else "--")
            fade.set_alpha(0 if first_scene else max(0.0, 1 - t / 0.3))
            write(cv.frame())
        cv.close()

    def _legend(self, cv):
        items = [("Airline", BIRD), ("Private jet", ORANGE), ("Props + other", NAVY)]
        x, y = 50, 1345
        cv.rbox(x, y - 28, 560, 44, 22, WHITE, z=20, shadow=False)
        xx = x + 22
        for name, col in items:
            cv.ui.plot([xx, xx + 26], [y - 6, y - 6], color=col, lw=5 * PX, solid_capstyle="round", zorder=22)
            cv.text(xx + 34, y + 1, name, 20, INK, cv.f.body, z=22)
            xx += 34 + cv.width(name, 20, cv.f.body) + 30

    # ---- military: Offutt zoom, counts only ----
    def military_scene(self, write, secs: float = 5.5):
        cv = Canvas(self.f)
        counties, towns = self._map_base(cv)
        cam = Camera(cv)
        self._title(cv, "dep")
        total = self.s["flights"]
        hud = self._hud(cv, "MOVEMENTS", total, clock=False)
        hud["total"].set_text("at least")
        pins = self._pins(cv)
        self._sample_stamp(cv)
        v0, v1 = (self.hub[0], self.hub[1], 260.0), (self.hub[0], self.hub[1] + 6, 150.0)
        for fi in range(int(secs * FPS)):
            t = fi / FPS
            cam.set(*lerp_view(v0, v1, ease(t / secs)))
            self._zoom_detail(counties, towns, cam.span)
            self._update_pins(cv, cam, pins, t)
            n = int(round(total * ease((t - 0.8) / 2.5)))
            hud["count"].set_text(str(n))
            hud["total"].set_x(hud["x"] + 26 + cv.width(str(n), 66, cv.f.head) + 14)
            write(cv.frame())
        cv.close()

    # ---- stats ----
    def _band(self, cv):
        from PIL import Image
        img = Image.open(ASSETS / "backdrop.jpg")
        iw, ih = img.size
        band_h = 330
        crop_h = int(iw * band_h / W)
        top = int(ih * 0.12)
        cv.image(ASSETS / "backdrop.jpg", 0, 0, W, band_h, z=3, crop=(0, top, iw, min(ih, top + crop_h)))
        # badge overlapping the band
        circ = cv.P.Circle((130, 330), 76, transform=cv.ui.transData)
        im = cv.image(ASSETS / "badge.png", 54, 254, 152, 152, z=6)
        im.set_clip_path(circ)
        cv.ui.add_patch(cv.P.Circle((130, 330), 76, fc="none", ec=CLOUD, lw=6 * PX, zorder=7))

    def _tile(self, cv, x, y, w, h, value, label, color, fc=WHITE, t=0, born=0):
        s = pop((t - born) / 0.35)
        if s <= 0:
            return
        cv.rbox(x, y, w, h, 22, fc, z=10, alpha=min(1, s))
        cv.text(x + 26, y + h * 0.58, value, 62, color, cv.f.head, z=12, alpha=min(1, s))
        cv.spaced(x + 28, y + h * 0.85, label, 16, MUTED, cv.f.mono6, spacing=2.2, z=12, alpha=min(1, s))

    def stats_scene(self, write, secs: float = 6.0):
        s = self.s
        tiles, rows, facts = self._stats_content()
        for fi in range(int(secs * FPS)):
            t = fi / FPS
            cv = Canvas(self.f)
            cv.fig.set_facecolor(CLOUD)
            cv.map.set_facecolor(CLOUD)
            self._band(cv)
            cv.spaced(62, 440, f"{self._stats_kicker()} · {self.date_label}", 21, BIRD, cv.f.mono, spacing=3.2, z=12)
            l1, l2 = self.spec["stats"]
            cv.text(58, 520, l1, 80, NAVY, cv.f.head, z=12)
            cv.text(58, 604, l2, 80, BIRD, cv.f.head, z=12)
            for i, (val, label, color) in enumerate(tiles):
                x = 62 + (i % 2) * 486
                y = 650 + (i // 2) * 166
                prog = ease((t - 0.3 - i * 0.12) / 1.0)
                shown = _count_text(val, prog)
                self._tile(cv, x, y, 470, 150, shown, label, color, t=t, born=0.25 + i * 0.12)
            if self.slot == "military":
                self._week_bars(cv, t)
            else:
                self._city_rows(cv, rows, t)
            for i, (label, value) in enumerate(facts):
                x = 62 + (i % 2) * 486
                y = 1330 + (i // 2) * 110 if self.slot != "military" else 1330 + (i // 2) * 110
                sp = pop((t - 2.2 - i * 0.1) / 0.35)
                if sp <= 0:
                    continue
                cv.rbox(x, y, 470, 96, 18, ICE, z=10, alpha=min(1, sp))
                cv.spaced(x + 24, y + 32, label, 15, MUTED, cv.f.mono6, spacing=2.2, z=12, alpha=min(1, sp))
                cv.text(x + 22, y + 78, value, 36 if len(value) < 22 else 28, NAVY, cv.f.head, z=12, alpha=min(1, sp))
            self._sample_stamp(cv)
            f = self._fade(cv)
            f.set_alpha(max(0.0, 1 - t / 0.3))
            write(cv.frame())
            cv.close()

    def _stats_kicker(self):
        return {"commercial": "EPPLEY · OMA", "private": "PRIVATE JETS · METRO", "military": "OFFUTT · OFF",
                "all": "ALL AIRPORTS · METRO"}[self.slot]

    def _stats_content(self):
        s = self.s
        if self.slot == "military":
            week_total = sum(d["count"] or 0 for d in self.week)
            tiles = [(s["flights"], "MOVEMENTS · AT LEAST", NAVY), (s["departures"], "DEPARTURES", NAVY),
                     (s["arrivals"], "ARRIVALS", ORANGE), (week_total, "LAST 7 DAYS", NAVY)]
            facts = [("COUNTS ARE A FLOOR", "Many don't broadcast.")]
            busiest = max(self.week, key=lambda d: d["count"] or 0) if self.week else None
            if busiest and busiest["count"]:
                facts.append(("BUSIEST DAY THIS WEEK", f"{busiest['label'].title()} · {busiest['count']}"))
            return tiles, [], facts
        noun = {"commercial": "AIRLINE FLIGHTS", "private": "PRIVATE JETS", "all": "FLIGHTS, ALL FIELDS"}[self.slot]
        tiles = [(s["flights"], noun, NAVY), (s["departures"], "DEPARTURES", NAVY),
                 (s["arrivals"], "ARRIVALS", ORANGE), (s.get("cities_linked", 0), "CITIES LINKED", NAVY)]
        rows = s.get("top_destinations", [])[:5]
        facts = []
        if s.get("first_wheels_up"):
            facts.append(("FIRST WHEELS-UP", s["first_wheels_up"]))
        if s.get("busiest_hour"):
            facts.append(("BUSIEST HOUR", f"{s['busiest_hour']} · {s.get('busiest_hour_count', '')}".rstrip(" ·")))
        if s.get("farthest"):
            facts.append(("FARTHEST", f"{s['farthest']['code']} · {s['farthest']['miles']:,} mi"))
        if self.slot == "commercial" and s.get("top_airlines"):
            a = s["top_airlines"][0]
            facts.append(("TOP AIRLINE", f"{a['name']} · {a['count']}"))
        elif self.slot == "private" and (s.get("top_types") or s.get("top_operators")):
            a = (s.get("top_types") or s.get("top_operators"))[0]
            facts.append(("TOP TYPE" if s.get("top_types") else "TOP OPERATOR", f"{a['name']} · {a['count']}"))
        elif self.slot == "all" and s.get("airports"):
            a = max(s["airports"], key=lambda r: r["count"])
            facts.append(("BUSIEST FIELD", f"{a['code']} · {a['count']}"))
        return tiles, rows, facts[:4]

    def _city_rows(self, cv, rows, t):
        if not rows:
            return
        cv.spaced(62, 1010, "TOP CITIES", 16, MUTED, cv.f.mono6, spacing=2.4, z=12)
        mx = max(r["count"] for r in rows)
        for i, r in enumerate(rows):
            y = 1035 + i * 56
            g = ease((t - 1.3 - i * 0.08) / 0.8)
            if g <= 0:
                continue
            cv.text(62, y + 30, r["code"], 26, BIRD, cv.f.mono, z=12, alpha=min(1, g * 2))
            w = max(160, 700 * r["count"] / mx) * g
            cv.rbox(160, y + 2, w, 42, 10, "#D7ECFB", z=10)
            cv.text(180, y + 31, r["city"], 22, INK, cv.f.body, z=12, alpha=min(1, g * 2))
            cv.text(1018, y + 31, str(r["count"]), 26, NAVY, cv.f.mono, ha="right", z=12, alpha=min(1, g * 2))

    def _week_bars(self, cv, t):
        cv.spaced(62, 1010, "LAST 7 DAYS · MOVEMENTS", 16, MUTED, cv.f.mono6, spacing=2.4, z=12)
        vals = [d["count"] for d in self.week] or []
        mx = max([v or 0 for v in vals] or [1]) or 1
        for i, d in enumerate(self.week):
            x = 70 + i * 136
            g = ease((t - 1.3 - i * 0.07) / 0.8)
            v = d["count"]
            h = 190 * ((v or 0) / mx) * g
            base = 1265
            cv.rbox(x, 1050, 104, 215, 12, ICE, z=9)
            if v is not None and h > 2:
                cv.rbox(x, base - h, 104, h, 12, NAVY if i == len(self.week) - 1 else BIRD, z=10)
            cv.text(x + 52, base + 36, d["label"], 20, MUTED, cv.f.mono6, ha="center", z=12)
            cv.text(x + 52, 1040, "–" if v is None else str(v), 24, NAVY, cv.f.mono, ha="center", z=12, alpha=min(1, g * 2))

    # ---- end card ----
    def end_scene(self, write, secs: float = 3.5):
        from PIL import Image
        img = Image.open(ASSETS / "backdrop.jpg")
        iw, ih = img.size
        band = int(H * 0.46)
        for fi in range(int(secs * FPS)):
            t = fi / FPS
            cv = Canvas(self.f)
            cv.fig.set_facecolor(CLOUD)
            cv.map.set_facecolor(CLOUD)
            z = 1 + 0.06 * (t / secs)  # slow push-in
            cw = min(iw, ih * W / band) / z
            ch = cw * band / W
            cx, cy = iw / 2 + iw * 0.05, ih * 0.5
            crop = (int(cx - cw / 2), int(max(0, cy - ch / 2)), int(cx + cw / 2), int(min(ih, cy + ch / 2)))
            cv.image(ASSETS / "backdrop.jpg", 0, 0, W, band, z=3, crop=crop)
            s = pop((t - 0.15) / 0.45)
            r = 150 * max(0.01, s)
            cv.ui.add_patch(cv.P.Circle((W / 2, band), r + 10, fc=CLOUD, ec="none", zorder=20))
            cv.ui.add_patch(cv.P.Circle((W / 2, band), r, fc=NAVY, ec="none", zorder=21))
            if s > 0.6:
                a = min(1, (s - 0.6) / 0.4)
                cv.paper_plane(W / 2 - 52, band - 104, 104, fc=WHITE, ec=WHITE, lw=1, z=23, alpha=a)
                # the fold line in navy so the plane reads on white
                cv.ui.plot([W / 2 - 52 + 50 * 104 / 120, W / 2 - 52 + 116 * 104 / 120],
                           [band - 104 + 48 * 104 / 120, band - 104 + 4 * 104 / 120], color=NAVY, lw=3 * PX, zorder=24, alpha=a)
                cv.text(W / 2, band + 44, "SkyBird", 62, WHITE, cv.f.head, ha="center", z=23, alpha=a)
                cv.spaced(W / 2, band + 88, "OMAHA", 20, MINT, cv.f.mono, spacing=6, ha="center", z=23, alpha=a)
            g = pop((t - 0.5) / 0.45)
            if g > 0:
                a = min(1, g)
                cv.text(W / 2, 1150, "Every bird flying", 84, NAVY, cv.f.head, ha="center", z=12, alpha=a)
                w_in = cv.width("in ", 84, cv.f.head)
                w_om = cv.width("Omaha.", 84, cv.f.head)
                x0 = W / 2 - (w_in + w_om) / 2
                cv.text(x0, 1245, "in ", 84, NAVY, cv.f.head, z=12, alpha=a)
                cv.text(x0 + w_in, 1245, "Omaha.", 84, BIRD, cv.f.head, z=12, alpha=a)
                cv.text(W / 2, 1320, "SkyBird - Omaha · new board every morning.", 30, INK, cv.f.body, ha="center", z=12, alpha=a)
            p = pop((t - 0.8) / 0.45)
            if p > 0:
                a = min(1, p)
                label = f"FOLLOW {self.handle}"
                tw = 0.6 * 30 * len(label) + 2.2 * (len(label) - 1)
                pw = tw + 150
                cv.rbox(W / 2 - pw / 2, 1375, pw, 88, 44, NAVY, z=12, alpha=a)
                cv.paper_plane(W / 2 - pw / 2 + 40, 1403, 46, fc=WHITE, ec=WHITE, lw=1, z=14, alpha=a)
                cv.spaced(W / 2 - pw / 2 + 108, 1430, label, 30, WHITE, cv.f.mono, spacing=2.2, z=14, alpha=a)
            self._sample_stamp(cv)
            f = self._fade(cv)
            f.set_alpha(max(0.0, 1 - t / 0.3))
            write(cv.frame())
            cv.close()

    # ---- whole reel ----
    def render(self, out: Path):
        import imageio_ffmpeg
        tmp = out.with_suffix(".tmp.mp4")
        w = imageio_ffmpeg.write_frames(str(tmp), (W, H), fps=FPS, codec="libx264", pix_fmt_in="rgb24",
                                        output_params=["-crf", "20", "-preset", "veryfast", "-movflags", "+faststart"],
                                        macro_block_size=1)
        w.send(None)
        if self.slot == "military":
            self.military_scene(w.send)
        else:
            self.map_scene("dep", w.send, first_scene=True)
            self.map_scene("arr", w.send)
        self.stats_scene(w.send)
        self.end_scene(w.send)
        w.close()
        tmp.replace(out)


def _move(box, x, y, w, h, r):
    """Reposition a rounded box made by Canvas.rbox (FancyBboxPatch with pad=r)."""
    box.set_x(x + r), box.set_y(y + r)
    box.set_width(max(w - 2 * r, 0.1)), box.set_height(max(h - 2 * r, 0.1))


def _count_text(val, prog):
    if isinstance(val, (int, float)):
        return f"{int(round(val * prog)):,}"
    return str(val)


def _save_flight_list(cfg: Config, post_date: date, flight_day: date, raw: list[dict]) -> None:
    """The day's master flight list (stats only: no tails or owners are shown anywhere),
    kept next to the videos so the numbers can be checked against the airport's board."""
    keep = ("callsign", "estDepartureAirport", "estArrivalAirport", "firstSeen", "lastSeen", "inferred", "moved_to_eppley")
    airlines, mil, excl = fl._lists(cfg)
    rows = []
    for f in sorted(raw, key=lambda f: f.get("lastSeen") or f.get("firstSeen") or 0):
        r = {k: f.get(k) for k in keep if f.get(k) is not None}
        kind = fl.classify(f, airlines, mil, excl)
        r["type"] = {"commercial": "airline"}.get(kind, kind)
        if kind != "commercial":
            r.pop("callsign", None)   # private/military callsigns can be tail numbers: never kept
        for k in ("firstSeen", "lastSeen"):
            if r.get(k):
                r[k] = datetime.fromtimestamp(r[k], cfg.tz).strftime("%m-%d %I:%M %p")
        rows.append(r)
    out = {"flight_day": flight_day.isoformat(), "flights": len(rows),
           "inferred_arrivals": sum(1 for f in raw if f.get("inferred")), "list": rows}
    cfg.media_file(post_date, "flights.json").write_text(json.dumps(out, indent=1), encoding="utf-8")


def render_day(cfg: Config, post_date: date, *, demo: bool = False, slots: list[str] | None = None) -> Path:
    """Fetch the flight day shown on `post_date` and write the reels + stats.json."""
    import requests
    flight_day = cfg.data_date(post_date)
    folder = cfg.media_folder(post_date)
    folder.mkdir(parents=True, exist_ok=True)
    log(cfg, f"Rendering {'SAMPLE ' if demo else ''}videos for flight day {flight_day} into {folder}")
    session = requests.Session()
    if demo:
        raw, info = fl.demo_raw(flight_day, cfg), {}
    else:
        # 1. All planes: every flight at the metro fields, plus the landings OpenSky missed.
        #    This one list is the master record; the other posts are cut from it.
        raw = fl.fetch_opensky(cfg, flight_day, session)
        fl.fix_airline_fields(cfg, raw)
        raw += fl.infer_arrivals(cfg, raw, flight_day, session)
        info = fl.aircraft_info(cfg, {f.get("icao24") for f in raw if f.get("icao24")}, session)
    _save_flight_list(cfg, post_date, flight_day, raw)
    day = fl.build_day(cfg, raw, info)
    week = fl.military_week(cfg, flight_day, day["posts"]["military"]["stats"]["flights"],
                            session=None if demo else session, backfill=not demo)
    from . import VERSION
    log(cfg, f"Video style: SkyBird board (renderer {VERSION})")
    stats = {"data_date": flight_day.isoformat(), "source": "sample" if demo else "opensky", "renderer": VERSION,
             "complete": day["complete"], "incomplete_reason": day["why"],
             "posts": {k: v["stats"] for k, v in day["posts"].items()}}
    if demo:  # sample data: make up the earlier days too so the preview shows full bars
        import random
        rnd = random.Random(3)
        for d in week[:-1]:
            d["count"] = d["count"] if d["count"] is not None else rnd.randint(4, 14)
    stats["posts"]["military"]["week"] = week
    cfg.media_file(post_date, "stats.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    if not day["complete"]:
        log(cfg, "Heads up: " + day["why"])
    stats["inferred_arrivals"] = sum(1 for f in raw if f.get("inferred"))
    cfg.media_file(post_date, "stats.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    fonts = Fonts()
    # Made in this order: All planes first (the full picture), then Eppley, private, military.
    order = ["all", "commercial", "private", "military"]
    for slot in [k for k in order if k in (slots or list(FILES))]:
        Reel(cfg, slot, flight_day, day["posts"][slot], fonts, demo, week=week).render(cfg.media_file(post_date, FILES[slot]))
        log(cfg, f"  rendered {cfg.media_file(post_date, FILES[slot]).name} ({day['posts'][slot]['stats']['flights']} movements)")
    return folder
