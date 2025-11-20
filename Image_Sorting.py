import cv2
import pytesseract
import json
import os
from pathlib import Path
import shutil

# ---------------------------
# CONFIGURATION
# ---------------------------

FRAMES_FOLDER = "frames"       # Input frames
TEMPLATES_FOLDER = "Frame_Templates" # JSON templates
SORTED_FOLDER = "Sorted"       # Output folder structure
OCR_CONFIG = "--psm 7"         # Single-line OCR, faster and more accurate for UI text

# ---------------------------
# UTILITIES
# ---------------------------

def load_templates():
    templates = []
    for file in Path(TEMPLATES_FOLDER).glob("*.json"):
        with open(file) as f:
            template = json.load(f)
            # Count regions for sorting
            template["region_count"] = len(template["regions"])
            templates.append(template)
    # Sort descending by number of regions
    templates.sort(key=lambda x: x["region_count"], reverse=True)
    return templates

def crop_region(frame, coords, frame_size):
    h, w = frame_size
    x, y, rw, rh = coords["x"], coords["y"], coords["w"], coords["h"]
    x1 = int(x * w)
    y1 = int(y * h)
    x2 = int((x + rw) * w)
    y2 = int((y + rh) * h)
    return frame[y1:y2, x1:x2]

def match_template(frame, template):
    frame_h, frame_w = frame.shape[:2]
    for region in template["regions"]:
        if region["mode"] != "text":
            continue  # Skip non-text for now
        crop = crop_region(frame, region["coords"], (frame_h, frame_w))
        text = pytesseract.image_to_string(crop, config=OCR_CONFIG)
        if region.get("expected_text") not in text:
            return False
    return True

def ensure_folder(path):
    os.makedirs(path, exist_ok=True)

# ---------------------------
# MAIN SCRIPT
# ---------------------------

def main():
    templates = load_templates()
    ensure_folder(SORTED_FOLDER)

    for frame_file in sorted(Path(FRAMES_FOLDER).glob("*.png")):
        frame = cv2.imread(str(frame_file))
        matched = False

        for template in templates:
            if match_template(frame, template):
                # Use the template filename as folder name (remove .json)
                folder_name = f"{template['source_image'].replace('.png','')}"
                target_folder = Path(SORTED_FOLDER) / folder_name
                ensure_folder(target_folder)
                shutil.move(str(frame_file), target_folder / frame_file.name)
                matched = True
                break

        if not matched:
            misc_folder = Path(SORTED_FOLDER) / "misc"
            ensure_folder(misc_folder)
            shutil.move(str(frame_file), misc_folder / frame_file.name)

        print(f"Processed {frame_file.name} → {'matched' if matched else 'misc'}")

if __name__ == "__main__":
    main()
