"""
Cut the reference crops that Screen_Detection.py matches against.

Usage:
    python Build_References.py <screen> <frame.png> [<screen> <frame.png> ...]

Give it a full screenshot (any resolution, 16:9) of each screen listed in
Screen_References/screens.json and it saves one crop per anchor. Re-run it with a
fresh screenshot if Apex changes its UI or detection starts missing a screen.
"""
import sys

import cv2

from Screen_Detection import load_config, reference_path, to_detect, scale_box, crop

def build(screen, frame_path, config):
    if screen not in config["screens"]:
        sys.exit(f"Unknown screen '{screen}'. Options: {', '.join(config['screens'])}")
    frame = cv2.imread(frame_path)
    if frame is None:
        sys.exit(f"Could not read {frame_path}")
    gray = to_detect(frame, config)
    for anchor, box in config["screens"][screen]["anchors"].items():
        ref = crop(gray, scale_box(box, config, gray.shape[1]))
        out = reference_path(screen, anchor)
        cv2.imwrite(str(out), ref)
        print(f"Saved {out.name} ({ref.shape[1]}x{ref.shape[0]})")

def main():
    args = sys.argv[1:]
    if not args or len(args) % 2:
        sys.exit(__doc__)
    config = load_config()
    for screen, frame_path in zip(args[::2], args[1::2]):
        build(screen, frame_path, config)

if __name__ == "__main__":
    main()
