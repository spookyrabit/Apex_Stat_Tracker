"""
The Maps tab's layers at any zoom: each layer is a picture for what's smooth (heat, areas) plus "marks" for what has
edges (dots, ring outlines, lines, counts, item icons), kept as shapes in map-picture px and drawn sharp at whatever
zoom the view is at. Also writes them as SVG (the picture embedded, the marks as vectors) to zoom into in a browser.

A layer is Maps/<Map>/layer_<name>.png (transparent, Heatmap.Layer's size) + layer_<name>.json:
    {"size": [w, h] of the map picture, "marks": [...]}, each mark one of
    {"type": "dot", "x", "y", "px", "fill", "edge"}                    a dot `px` screen px across (radius), any zoom
    {"type": "circle", "x", "y", "r", "colour", "width", "opacity"}    a circle of radius `r` map px, `width` screen px
    {"type": "poly", "points", "colour", "width", "fill", "halo", "closed"}   outline `width` screen px (0: none) on a
                                                                       dark `halo` px wider; `fill`: opacity (0: none)
    {"type": "text", "x", "y", "text", "colour", "px", "anchor"}       PIL anchor ("mm", "lm"), `px` font size
    {"type": "item", "x", "y", "kind", "px"}                           Match_Map.draw_item's icon, half width `px`
    {"type": "key", "entries": [{"colour" or "kind", "text"}]}         a key in the view's top left corner
    {"type": "cycle", "every", "frames": [[marks], ...]}               one frame's marks at a time, `every` s each
Colours are "#rrggbb". Positions are map-picture (reference) px.
"""
import json
from html import escape
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

import Match_Map

MAX_ZOOM = 6.0                 # screen px per map px at the most (the map picture itself is ~1030 px across)


def hex_colour(bgr):
    """(b, g, r) -> "#rrggbb"."""
    b, g, r = (int(v) for v in bgr)
    return f"#{r:02x}{g:02x}{b:02x}"


def rgb(colour):
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))


def load_layer(png):
    """(BGRA array, marks) of a layer file; marks [] for a layer saved before marks existed."""
    png = Path(png)
    marks_file = png.with_suffix(".json")
    marks = json.loads(marks_file.read_text())["marks"] if marks_file.exists() else []
    picture = cv2.imread(str(png), cv2.IMREAD_UNCHANGED)
    if picture is None:
        raise OSError(f"can't read {png}")
    return (picture if picture.shape[2] == 4 else cv2.cvtColor(picture, cv2.COLOR_BGR2BGRA)), marks


def now_showing(marks, when=None):
    """The marks to draw at `when` (time.time()): a "cycle" mark's frame of the moment in place of the cycle."""
    import time
    when = time.time() if when is None else when
    out = []
    for m in marks:
        if m["type"] == "cycle":
            out += m["frames"][int(when // m["every"]) % len(m["frames"])] if m["frames"] else []
        else:
            out.append(m)
    return out


def cycles(marks):
    """The seconds per frame of any "cycle" marks (the view redraws that often), or None."""
    return min((m["every"] for m in marks if m["type"] == "cycle"), default=None)


def poi_marks(pois, px=18):
    return [{"type": "text", "x": p["x"], "y": p["y"], "text": p["name"].upper(), "colour": "#ebebeb", "px": px,
             "anchor": "mm"} for p in pois]


# ---------------------------
# DRAWING A VIEW
# ---------------------------

def crop_scaled(picture, ref_size, box, out_size, interpolation=cv2.INTER_LINEAR):
    """The part of `picture` (an array of any size covering the whole map picture) inside `box` (map px), at
    out_size (w, h); outside the picture: 0 (black, see-through)."""
    kx, ky = picture.shape[1] / ref_size[0], picture.shape[0] / ref_size[1]
    x0, y0, x1, y1 = box
    sx, sy = (x1 - x0) * kx / out_size[0], (y1 - y0) * ky / out_size[1]       # picture px per view px
    to_picture = np.float32([[sx, 0, x0 * kx + 0.5 * sx - 0.5], [0, sy, y0 * ky + 0.5 * sy - 0.5]])
    return cv2.warpAffine(picture, to_picture, out_size, flags=interpolation | cv2.WARP_INVERSE_MAP,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=0)


def render(base, layers, marks, ref_size, centre, zoom, out_size, dim=1.0):
    """The view: `zoom` screen px per map px around `centre` (map px), out_size (w, h) screen px. `base`: the map
    picture (BGR array), darkened by `dim`; `layers`: BGRA arrays covering the map (load_layer); `marks`: every
    layer's marks. Returns a PIL RGB picture."""
    w, h = out_size
    cx, cy = centre
    box = (cx - w / 2 / zoom, cy - h / 2 / zoom, cx + w / 2 / zoom, cy + h / 2 / zoom)
    canvas = crop_scaled(base, ref_size, box, out_size, cv2.INTER_CUBIC if zoom > 1 else cv2.INTER_LINEAR)
    canvas = canvas.astype(np.float32) * dim
    for layer in layers:
        part = crop_scaled(layer, ref_size, box, out_size).astype(np.float32)
        a = part[..., 3:] / 255
        canvas = canvas * (1 - a) + part[..., :3] * a
    canvas = np.clip(canvas, 0, 255).astype(np.uint8)                 # BGR for cv2's smooth shapes
    P = lambda x, y: (int(round((x - box[0]) * zoom)), int(round((y - box[1]) * zoom)))
    texts, keys = [], []
    for m in now_showing(marks):
        kind = m["type"]
        if kind == "text":
            texts.append(m)
        elif kind == "key":
            keys.append(m)
        elif kind == "dot":
            if m.get("edge"):
                cv2.circle(canvas, P(m["x"], m["y"]), int(m["px"]) + 1, rgb(m["edge"])[::-1], -1, cv2.LINE_AA)
            cv2.circle(canvas, P(m["x"], m["y"]), int(m["px"]), rgb(m["fill"])[::-1], -1, cv2.LINE_AA)
        elif kind == "item":
            Match_Map.draw_item(canvas, P(m["x"], m["y"]), m["kind"], int(m["px"]))
        elif kind == "circle":
            (x, y), r = P(m["x"], m["y"]), max(1, int(m["r"] * zoom))
            if m.get("halo"):
                blend(canvas, lambda im, o: cv2.circle(im, (x - o[0], y - o[1]), r, (0, 0, 0), int(m["width"] + m["halo"]),
                                                       cv2.LINE_AA), 0.8, (x - r, y - r, x + r, y + r), m["width"] + m["halo"])
            shape = lambda im, o: cv2.circle(im, (x - o[0], y - o[1]), r, rgb(m["colour"])[::-1], int(m["width"]), cv2.LINE_AA)
            blend(canvas, shape, m.get("opacity", 1.0), (x - r, y - r, x + r, y + r), m["width"])
        elif kind == "poly":
            if "_points" not in m:                          # (once per mark: the view is drawn again on every move)
                m["_points"] = np.array(m["points"], np.float64)
                m["_box"] = (*m["_points"].min(axis=0), *m["_points"].max(axis=0))
            x0, y0, x1, y1 = m["_box"]
            if x1 < box[0] or y1 < box[1] or x0 > box[2] or y0 > box[3]:
                continue                                    # off the view
            points = np.round((m["_points"] - box[:2]) * zoom).clip(-1e6, 1e6).astype(np.int32)
            box_px = (*points.min(axis=0), *points.max(axis=0))
            shifted = lambda o: [points - np.array(o[:2], np.int32)]
            if m.get("fill"):
                blend(canvas, lambda im, o: cv2.fillPoly(im, shifted(o), rgb(m["colour"])[::-1], cv2.LINE_AA), m["fill"],
                      box_px, 0)
            if m.get("halo"):
                blend(canvas, lambda im, o: cv2.polylines(im, shifted(o), m.get("closed", True), (0, 0, 0),
                                                          int(m["width"] + m["halo"]), cv2.LINE_AA), 0.8, box_px,
                      m["width"] + m["halo"])
            points = [points]
            if m.get("width"):
                cv2.polylines(canvas, points, m.get("closed", True), rgb(m["colour"])[::-1], int(m["width"]), cv2.LINE_AA)
    out = Image.fromarray(canvas[..., ::-1].copy())
    if texts or keys:
        draw = ImageDraw.Draw(out)
        for m in texts:
            x, y = P(m["x"], m["y"])
            if -200 <= x <= w + 200 and -50 <= y <= h + 50:
                draw.text((x, y), m["text"], font=Match_Map.font(int(m["px"])), fill=rgb(m["colour"]),
                          anchor=m.get("anchor", "mm"), stroke_width=3, stroke_fill=(0, 0, 0))
        draw_keys(out, keys)
    return out


def blend(canvas, shape, opacity, box, width):
    """Draw `shape(image, offset)` onto the canvas at `opacity` (cv2 doesn't blend), only inside `box` (x0, y0, x1, y1)
    grown by the line `width`: blending the whole view for each see-through shape took most of a frame."""
    h, w = canvas.shape[:2]
    pad = int(width) + 2
    x0, y0 = max(0, int(box[0]) - pad), max(0, int(box[1]) - pad)
    x1, y1 = min(w, int(box[2]) + pad + 1), min(h, int(box[3]) + pad + 1)
    if x1 <= x0 or y1 <= y0:
        return
    part = canvas[y0:y1, x0:x1]
    if opacity >= 1:
        shape(part, (x0, y0))
        return
    over = part.copy()
    shape(over, (x0, y0))
    cv2.addWeighted(over, opacity, part, 1 - opacity, 0, dst=part)


def draw_keys(image, keys):
    """Every layer's key, one row each, in the top left corner (they stay put while the map zooms)."""
    draw = ImageDraw.Draw(image)
    small = Match_Map.font(16)
    y = 14
    for key in keys:
        x = 14
        for entry in key["entries"]:
            if entry.get("kind"):
                icon = np.zeros((22, 22, 3), np.uint8)
                Match_Map.draw_item(icon, (11, 11), entry["kind"], 9)
                image.paste(Image.fromarray(icon[..., ::-1].copy()), (x, y - 2),
                            Image.fromarray(np.uint8(icon.max(axis=2) > 0) * 255))
                x += 26
            elif entry.get("colour"):
                draw.rectangle((x, y, x + 18, y + 16), fill=rgb(entry["colour"]), outline=(0, 0, 0))
                x += 24
            draw.text((x, y + 8), entry["text"], font=small, fill=(255, 255, 255), anchor="lm", stroke_width=3,
                      stroke_fill=(0, 0, 0))
            x += int(draw.textlength(entry["text"], font=small)) + 20
        y += 26


# ---------------------------
# SVG
# ---------------------------

ITEM_SVG = {  # Match_Map.draw_item's icons as SVG, half width 1 (scaled by the mark's px)
    "care_package": '<path d="M0,-1 L1,0 L0,1 L-1,0 Z" fill="#232323" stroke="#ffffff" stroke-width="0.17"/>'
                    '<circle r="0.33" fill="#ffffff"/>',
    "stronghold": '<polygon points="0.92,0.38 0.38,0.92 -0.38,0.92 -0.92,0.38 -0.92,-0.38 -0.38,-0.92 0.38,-0.92 '
                  '0.92,-0.38" fill="#ff2d6e" stroke="#ffffff" stroke-width="0.09"/>'
                  '<rect x="-0.33" y="-0.33" width="0.66" height="0.66" fill="#5a1428"/>',
    "evo_harvester": '<path d="M0,-1 L1,0 L0,1 L-1,0 Z" fill="#232323" stroke="#ebe128" stroke-width="0.17"/>'
                     '<path d="M-0.4,-0.14 L0,-0.38 L0.4,-0.14 M-0.4,0.36 L0,0.12 L0.4,0.36" fill="none" '
                     'stroke="#ebe128" stroke-width="0.12"/>',
}


def svg_marks(marks, unit=1.0):
    """SVG elements for marks. `unit`: map px per screen px the SVG is drawn for (its sizes in screen px: dots, line
    widths, text), so a 1:1 view in the browser looks like the Maps tab at zoom 1 / unit."""
    out = []
    for m in marks:
        kind = m["type"]
        if kind == "dot":
            edge = f' stroke="{m["edge"]}" stroke-width="{unit:.2f}"' if m.get("edge") else ""
            out.append(f'<circle cx="{m["x"]:.1f}" cy="{m["y"]:.1f}" r="{m["px"] * unit:.2f}" fill="{m["fill"]}"{edge}/>')
        elif kind == "item":
            out.append(f'<g transform="translate({m["x"]:.1f},{m["y"]:.1f}) scale({m["px"] * unit:.2f})">'
                       f'{ITEM_SVG.get(m["kind"], "")}</g>')
        elif kind == "circle":
            if m.get("halo"):
                out.append(f'<circle cx="{m["x"]:.1f}" cy="{m["y"]:.1f}" r="{m["r"]:.1f}" fill="none" stroke="#000000" '
                           f'stroke-opacity="0.8" stroke-width="{(m["width"] + m["halo"]) * unit:.2f}"/>')
            out.append(f'<circle cx="{m["x"]:.1f}" cy="{m["y"]:.1f}" r="{m["r"]:.1f}" fill="none" stroke="{m["colour"]}" '
                       f'stroke-width="{m["width"] * unit:.2f}" opacity="{m.get("opacity", 1.0):.2f}"/>')
        elif kind == "cycle" and m["frames"]:
            # One frame shown at a time, `every` s each (SVG animation: browsers play it)
            n, every = len(m["frames"]), m["every"]
            times = ";".join(f"{i / n:.4f}" for i in range(n))
            for i, frame in enumerate(m["frames"]):
                values = ";".join("visible" if j == i else "hidden" for j in range(n))
                out.append(f'<g visibility="hidden"><animate attributeName="visibility" values="{values}" '
                           f'keyTimes="{times}" dur="{n * every}s" calcMode="discrete" repeatCount="indefinite"/>')
                out += svg_marks(frame, unit)
                out.append("</g>")
        elif kind == "poly":
            d = " ".join(f"{x:.1f},{y:.1f}" for x, y in m["points"])
            tag = "polygon" if m.get("closed", True) else "polyline"
            if m.get("fill"):
                out.append(f'<{tag} points="{d}" fill="{m["colour"]}" fill-opacity="{m["fill"]:.2f}" stroke="none"/>')
            if m.get("halo"):
                out.append(f'<{tag} points="{d}" fill="none" stroke="#000000" stroke-opacity="0.8" '
                           f'stroke-width="{(m["width"] + m["halo"]) * unit:.2f}" stroke-linejoin="round"/>')
            if m.get("width"):
                out.append(f'<{tag} points="{d}" fill="none" stroke="{m["colour"]}" '
                           f'stroke-width="{m["width"] * unit:.2f}" stroke-linejoin="round"/>')
        elif kind == "text":
            anchor = {"m": "middle", "l": "start", "r": "end"}[m.get("anchor", "mm")[0]]
            out.append(f'<text x="{m["x"]:.1f}" y="{m["y"]:.1f}" font-size="{m["px"] * unit:.1f}" fill="{m["colour"]}" '
                       f'text-anchor="{anchor}" dominant-baseline="central" font-family="sans-serif" font-weight="bold" '
                       f'stroke="#000000" stroke-width="{2 * unit:.2f}" paint-order="stroke">{escape(m["text"])}</text>')
        elif kind == "key":
            text = " · ".join(e["text"] for e in m["entries"])
            out.append(f'<!-- key: {escape(text)} -->')
    return out


def write_svg(path, ref_size, pictures, marks, title="", dim=1.0, embed=False):
    """An SVG of the map picture size: `pictures` [(file, opacity)] linked (bottom first: the map, then each layer's
    picture), `marks` on top as vectors; `dim`: the first picture darkened (the Maps tab's map). `embed`: the pictures
    inside the file (a browser may not load a local SVG's pictures from another folder)."""
    import base64
    import os
    from urllib.parse import quote
    path = Path(path)
    w, h = ref_size
    unit = max(w, h) / Match_Map.OUTPUT_SIZE          # drawn for the Maps tab's whole-map size
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
           f'viewBox="0 0 {w} {h}" width="{w * 2}" height="{h * 2}">',
           f'<title>{escape(title)}</title>', f'<rect width="{w}" height="{h}" fill="#000000"/>']
    for i, (picture, opacity) in enumerate(pictures):
        href = ("data:image/png;base64," + base64.b64encode(Path(picture).read_bytes()).decode()) if embed else \
            quote(os.path.relpath(picture, path.parent))
        o = dim if i == 0 and dim != 1.0 else opacity
        out.append(f'<image xlink:href="{href}" href="{href}" x="0" y="0" width="{w}" height="{h}" '
                   f'preserveAspectRatio="none" opacity="{o:.2f}"/>')
    out += svg_marks(marks, unit)
    out.append("</svg>")
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text("\n".join(out))
    tmp.replace(path)
    return path
