import cv2
import json
import os

# --- GLOBALS ---
drawing = False
ix = iy = 0
rectangles = []
current_image = None
display_image = None
scale_x = 1
scale_y = 1

def mouse_callback(event, x, y, flags, param):
    global ix, iy, drawing, display_image

    if event == cv2.EVENT_LBUTTONDOWN:
        drawing = True
        ix, iy = x, y

    elif event == cv2.EVENT_MOUSEMOVE and drawing:
        img_copy = display_image.copy()
        cv2.rectangle(img_copy, (ix, iy), (x, y), (0, 255, 0), 1)
        cv2.imshow("Template Creator", img_copy)

    elif event == cv2.EVENT_LBUTTONUP:
        drawing = False
        rectangles.append((ix, iy, x, y))
        cv2.rectangle(display_image, (ix, iy), (x, y), (0, 255, 0), 1)
        cv2.imshow("Template Creator", display_image)


def select_regions(image_path, output_json):
    global current_image, display_image, scale_x, scale_y, rectangles

    current_image = cv2.imread(image_path)
    if current_image is None:
        raise ValueError("Image not found.")

    h, w = current_image.shape[:2]

    # downscale large images for usability
    max_dim = 1080
    factor = min(max_dim / h, max_dim / w, 1.0)

    resized = cv2.resize(current_image, None, fx=factor, fy=factor)
    display_image = resized.copy()

    scale_x = w / resized.shape[1]
    scale_y = h / resized.shape[0]

    cv2.namedWindow("Template Creator")
    cv2.setMouseCallback("Template Creator", mouse_callback)

    while True:
        cv2.imshow("Template Creator", display_image)
        key = cv2.waitKey(1) & 0xFF

        if key == ord('q'):
            break

    cv2.destroyAllWindows()

    # Region definitions
    region_data = []

    for i, (x1, y1, x2, y2) in enumerate(rectangles):
        # restore true coordinates
        X1 = int(x1 * scale_x)
        Y1 = int(y1 * scale_y)
        X2 = int(x2 * scale_x)
        Y2 = int(y2 * scale_y)

        # convert to relative %
        rel_x = X1 / w
        rel_y = Y1 / h
        rel_w = (X2 - X1) / w
        rel_h = (Y2 - Y1) / h

        print(f"\nRegion #{i+1}: {X1,Y1} to {X2,Y2}")
        name = input("Name this region: ").strip()
        expected_text = input("Expected text (optional): ").strip() or None
        expected_image = input("Expected template image (optional path): ").strip() or None

        region_data.append({
            "name": name,
            "x": rel_x,
            "y": rel_y,
            "w": rel_w,
            "h": rel_h,
            "expected_text": expected_text,
            "expected_image": expected_image
        })

    print(f"\nSaving template to {output_json}")
    os.makedirs(os.path.dirname(output_json), exist_ok=True)

    with open(output_json, "w") as f:
        json.dump({
            "frame_type": os.path.splitext(os.path.basename(output_json))[0],
            "regions": region_data
        }, f, indent=4)

    print("Done.")


if __name__ == "__main__":
    image = input("Path to frame image: ").strip()
    output = input("Output JSON template path: ").strip()
    select_regions(image, output)
