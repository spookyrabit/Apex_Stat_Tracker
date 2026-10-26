import json
from pathlib import Path

import cv2
import numpy as np

# ---------------------------
# CONFIGURATION
# ---------------------------

REFERENCES_FOLDER = Path(__file__).parent / "Screen_References"
SCREENS_FILE = REFERENCES_FOLDER / "screens.json"

# ---------------------------
# UTILITIES
# ---------------------------

def load_config():
    with open(SCREENS_FILE) as f:
        return json.load(f)

def reference_path(screen, anchor):
    return REFERENCES_FOLDER / f"{screen}__{anchor}.png"

def to_detect(frame, config):
    """Downscale a frame to the detection size and convert to grayscale."""
    base_w, base_h = config["base_size"]
    w = config["detect_width"]
    h = round(w * base_h / base_w)
    if frame.shape[1] != w or frame.shape[0] != h:
        frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
    if frame.ndim == 3:
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return frame

def scale_box(box, config, frame_w, margin=0):
    """Convert a [x1, y1, x2, y2] box from base_size pixels to frame pixels."""
    s = frame_w / config["base_size"][0]
    x1, y1, x2, y2 = (round(v * s) for v in box)
    return x1 - margin, y1 - margin, x2 + margin, y2 + margin

def crop(frame, box):
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = box
    return frame[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]

# ---------------------------
# CLASSIFIER
# ---------------------------

class ScreenClassifier:
    """Decides which Apex screen a frame shows by template-matching small UI anchors."""

    def __init__(self, config=None):
        self.config = config or load_config()
        self.margin = self.config["search_margin"]
        self.references = {}
        for screen, spec in self.config["screens"].items():
            for anchor in spec["anchors"]:
                path = reference_path(screen, anchor)
                ref = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
                if ref is None:
                    print(f"[Screen_Detection] Missing reference {path.name}; {screen} can't be detected until it's built.")
                    continue
                self.references[(screen, anchor)] = ref

    def anchor_score(self, gray, screen, anchor):
        ref = self.references.get((screen, anchor))
        if ref is None:
            return 0.0
        spec = self.config["screens"][screen]
        # An anchor can say where to look for it (a tab that shifts when other tabs aren't there)
        box = spec.get("search", {}).get(anchor)
        region = (crop(gray, scale_box(box, self.config, gray.shape[1])) if box else
                  crop(gray, scale_box(spec["anchors"][anchor], self.config, gray.shape[1], self.margin)))
        if region.shape[0] < ref.shape[0] or region.shape[1] < ref.shape[1]:
            return 0.0
        # A flat region (black screen, fade) makes CCOEFF meaningless
        if region.std() < 4:
            return 0.0
        result = cv2.matchTemplate(region, ref, cv2.TM_CCOEFF_NORMED)
        return float(result.max())

    def scores(self, gray):
        """Score every screen. Returns {screen: score}."""
        out = {}
        for screen, spec in self.config["screens"].items():
            anchor_scores = [self.anchor_score(gray, screen, a) for a in spec["anchors"]]
            out[screen] = max(anchor_scores) if spec.get("match") == "any" else min(anchor_scores)
        return out

    def classify(self, frame):
        """Return (screen name or None, scores). `frame` may be full size or already a detection frame."""
        gray = to_detect(frame, self.config)
        scores = self.scores(gray)
        for screen, spec in self.config["screens"].items():
            if scores[screen] >= spec["threshold"]:
                return screen, scores
        return None, scores
