#!/usr/bin/env python3
import os
import json
import datetime
import tkinter as tk
from tkinter import ttk, filedialog, simpledialog, messagebox
from PIL import Image, ImageTk, ImageOps

try:
    import pytesseract
    OCR_AVAILABLE = True
except Exception:
    pytesseract = None
    OCR_AVAILABLE = False

# Output directories
TEMPLATES_DIR = "Frame_Templates"
TEMPLATES_IMAGES_DIR = os.path.join(TEMPLATES_DIR, "images")
os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(TEMPLATES_IMAGES_DIR, exist_ok=True)

# ---------- Region container ----------
class Region:
    def __init__(self, name, x1, y1, x2, y2, mode="text", expected_text=None, expected_image=None):
        self.name = name
        self.x1 = int(x1)
        self.y1 = int(y1)
        self.x2 = int(x2)
        self.y2 = int(y2)
        self.mode = mode  # "text" or "image"
        self.expected_text = expected_text
        self.expected_image = expected_image
        self.canvas_ids = {"rect": None, "label": None, "highlight": None}

    def normalized(self, img_w, img_h):
        return {
            "x": self.x1 / img_w,
            "y": self.y1 / img_h,
            "w": (self.x2 - self.x1) / img_w,
            "h": (self.y2 - self.y1) / img_h
        }

    def to_json(self, img_w, img_h):
        j = {
            "name": self.name,
            "mode": self.mode,
            "coords": self.normalized(img_w, img_h)
        }
        if self.mode == "text":
            j["expected_text"] = self.expected_text
        if self.mode == "image":
            j["expected_image"] = self.expected_image
        return j

# ---------- Main application ----------
class TemplateCreator:
    def __init__(self, root):
        self.root = root
        root.title("Template Creator v3")
        self.img_path = None
        self.img_pil = None
        self.img_tk = None
        self.img_w = 0
        self.img_h = 0
        self.display_scale = 1.0

        self.regions = []
        self.undo_stack = []  # tuples ("add"/"del", Region)
        self.selected_idx = None

        # drawing state
        self.is_drawing = False
        self.draw_start = None
        self.temp_rect_id = None
        self.current_new_name = None
        self.current_new_mode = "text"

        self._build_ui()
        self._bind_shortcuts()

    def _build_ui(self):
        # left control
        left = ttk.Frame(self.root, width=320)
        left.pack(side="left", fill="y", padx=6, pady=6)
        ttk.Label(left, text="Template Creator v3", font=("Segoe UI", 12, "bold")).pack(anchor="w")

        ttk.Button(left, text="Load Frame Image...", command=self.load_image).pack(fill="x", pady=6)
        ttk.Button(left, text="Load Template (JSON)...", command=self.load_template).pack(fill="x", pady=0)

        ttk.Separator(left).pack(fill="x", pady=6)

        ttk.Button(left, text="Add Region (N)", command=self.add_region_prompt).pack(fill="x")
        ttk.Button(left, text="Save Template (Ctrl+S)", command=self.save_template).pack(fill="x", pady=4)
        ttk.Button(left, text="Undo (Ctrl+Z)", command=self.undo).pack(fill="x")

        ttk.Separator(left).pack(fill="x", pady=6)

        ttk.Label(left, text="Regions:").pack(anchor="w")
        self.regions_listbox = tk.Listbox(left, exportselection=False)
        self.regions_listbox.pack(fill="both", expand=True)
        self.regions_listbox.bind("<<ListboxSelect>>", self.on_region_select)

        btns_frame = ttk.Frame(left)
        btns_frame.pack(fill="x", pady=6)
        ttk.Button(btns_frame, text="Rename", command=self.rename_selected).pack(side="left", fill="x", expand=True)
        ttk.Button(btns_frame, text="Delete (Del)", command=self.delete_selected).pack(side="left", fill="x", expand=True)

        ttk.Label(left, text="Mode for new region:").pack(anchor="w", pady=(6,0))
        self.new_mode_var = tk.StringVar(value="text")
        ttk.Radiobutton(left, text="Text (OCR)", variable=self.new_mode_var, value="text").pack(anchor="w")
        ttk.Radiobutton(left, text="Image (Template)", variable=self.new_mode_var, value="image").pack(anchor="w")

        ttk.Separator(left).pack(fill="x", pady=6)
        ttk.Label(left, text="Selected region details:").pack(anchor="w")
        self.sel_name_var = tk.StringVar()
        ttk.Entry(left, textvariable=self.sel_name_var).pack(fill="x", pady=(4,2))
        ttk.Button(left, text="Apply Name", command=self.apply_name).pack(fill="x")
        ttk.Label(left, text="Expected text (edit) [text mode only]:").pack(anchor="w", pady=(6,0))
        self.expected_text_var = tk.StringVar()
        ttk.Entry(left, textvariable=self.expected_text_var).pack(fill="x")
        ttk.Button(left, text="Set Expected Text", command=self.set_expected_text).pack(fill="x", pady=4)
        ttk.Button(left, text="Toggle Mode (I)", command=self.toggle_mode).pack(fill="x", pady=(0,6))

        ttk.Label(left, text="OCR Preview:").pack(anchor="w")
        self.ocr_preview = tk.Text(left, height=6, wrap="word")
        self.ocr_preview.pack(fill="both")

        ttk.Separator(left).pack(fill="x", pady=6)
        instructions = (
            "Hotkeys:\n"
            "N = New region\n"
            "Del = Delete selected\n"
            "I = Toggle mode (selected)\n"
            "Ctrl+S = Save template\n"
            "Ctrl+Z = Undo\n\n"
            "Workflow:\n1) Load image\n2) Add Region -> name -> drag box\n3) Edit selected region (rename, set expected text)\n4) Save template\n"
        )
        ttk.Label(left, text=instructions, justify="left").pack(anchor="w", pady=(6,0))

        # right canvas
        right = ttk.Frame(self.root)
        right.pack(side="right", fill="both", expand=True)
        self.canvas = tk.Canvas(right, bg="#111111")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self.canvas_mouse_down)
        self.canvas.bind("<B1-Motion>", self.canvas_mouse_move)
        self.canvas.bind("<ButtonRelease-1>", self.canvas_mouse_up)
        self.canvas.bind("<Button-3>", self.canvas_right_click)

        self.status_var = tk.StringVar(value="Load an image to begin.")
        ttk.Label(self.root, textvariable=self.status_var, relief="sunken", anchor="w").pack(side="bottom", fill="x")

    # ---------- Image loading ----------
    def load_image(self):
        path = filedialog.askopenfilename(title="Select frame image", filetypes=[("Images","*.png;*.jpg;*.jpeg;*.bmp"),("All","*.*")])
        if not path:
            return
        self.img_path = path
        self.img_pil = Image.open(path).convert("RGBA")
        self.img_w, self.img_h = self.img_pil.size
        self._prepare_display_image()
        self.redraw_all()
        self.status_var.set(f"Loaded {os.path.basename(path)} ({self.img_w}x{self.img_h})")

    def _prepare_display_image(self):
        # scale to fit a reasonable size
        max_dim = 1200
        scale = min(max_dim / max(self.img_w, self.img_h), 1.0)
        self.display_scale = scale
        disp_w = int(self.img_w * scale)
        disp_h = int(self.img_h * scale)
        disp = self.img_pil.resize((disp_w, disp_h), Image.LANCZOS)
        self.img_tk = ImageTk.PhotoImage(disp)
        self.canvas.config(width=disp_w, height=disp_h)
        self.canvas.delete("bgimg")
        self.canvas.create_image(0,0,anchor="nw",image=self.img_tk, tags="bgimg")
        # raise regions above bg
        self.canvas.tag_raise("region")

    # ---------- Region management ----------
    def add_region_prompt(self):
        if not self.img_pil:
            messagebox.showwarning("No image", "Load a frame image first.")
            return
        name = simpledialog.askstring("Region name", "Enter region name (unique):", parent=self.root)
        if not name:
            return
        # store current new mode
        mode = self.new_mode_var.get()
        self.current_new_name = name
        self.current_new_mode = mode
        self.status_var.set(f"Draw region for: {name} (mode={mode}). Click and drag on image.")
        # next mouse draw will create region

    def canvas_mouse_down(self, event):
        if not self.img_pil:
            return
        # if we are in "create new region" state
        if self.current_new_name:
            self.is_drawing = True
            self.draw_start = (event.x, event.y)
            # temp rectangle
            self.temp_rect_id = self.canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#00FF00", width=2, dash=(4,2), tags="temp")
        else:
            # selection by click
            self.select_region_by_point(event.x, event.y)

    def canvas_mouse_move(self, event):
        if self.is_drawing and self.temp_rect_id:
            x1,y1 = self.draw_start
            self.canvas.coords(self.temp_rect_id, x1, y1, event.x, event.y)

    def canvas_mouse_up(self, event):
        if self.is_drawing and self.temp_rect_id:
            x1,y1 = self.draw_start
            x2,y2 = event.x, event.y
            self.canvas.delete(self.temp_rect_id)
            self.temp_rect_id = None
            self.is_drawing = False
            self.draw_start = None
            # normalize and convert to image coords
            sx1, sy1 = min(x1,x2), min(y1,y2)
            sx2, sy2 = max(x1,x2), max(y1,y2)
            if abs(sx2-sx1) < 6 or abs(sy2-sy1) < 6:
                self.status_var.set("Region too small; canceled.")
                self.current_new_name = None
                return
            scale = self.display_scale
            img_x1 = int(sx1 / scale)
            img_y1 = int(sy1 / scale)
            img_x2 = int(sx2 / scale)
            img_y2 = int(sy2 / scale)
            # create region object
            r = Region(self.current_new_name, img_x1, img_y1, img_x2, img_y2, mode=self.current_new_mode)
            self.regions.append(r)
            self.undo_stack.append(("add", r))
            self.current_new_name = None
            # If image mode, save template image immediately
            if r.mode == "image":
                self._save_region_image(r)
            # add to listbox and draw
            self.regions_listbox.insert("end", f"{r.name} ({r.mode})")
            self._draw_region_on_canvas(r)
            self.selected_idx = len(self.regions) - 1
            self.update_selected_details()
            self.status_var.set(f"Region added: {r.name}")
            # if text, run OCR preview
            if r.mode == "text":
                self._run_ocr_for_region(r)

    def canvas_right_click(self, event):
        # right-click selects region
        self.select_region_by_point(event.x, event.y)

    def select_region_by_point(self, x, y):
        if not self.regions:
            return
        # convert to image coords
        scale = self.display_scale
        ix = int(x / scale); iy = int(y / scale)
        for i, r in enumerate(self.regions):
            if r.x1 <= ix <= r.x2 and r.y1 <= iy <= r.y2:
                self.selected_idx = i
                self.update_selected_details()
                return
        # none found
        self.selected_idx = None
        self.update_selected_details()

    def _draw_region_on_canvas(self, region):
        scale = self.display_scale
        sx1 = int(region.x1 * scale); sy1 = int(region.y1 * scale)
        sx2 = int(region.x2 * scale); sy2 = int(region.y2 * scale)
        rect_id = self.canvas.create_rectangle(sx1, sy1, sx2, sy2, outline="#00FF00", width=2, tags=("region",))
        label_id = self.canvas.create_text(sx1 + 6, sy1 + 6, anchor="nw", text=region.name, fill="#00FF00", font=("Arial", 10, "bold"), tags=("region",))
        region.canvas_ids["rect"] = rect_id
        region.canvas_ids["label"] = label_id

    def redraw_all(self):
        self.canvas.delete("all")
        if self.img_tk:
            self.canvas.create_image(0,0,anchor="nw",image=self.img_tk, tags="bgimg")
        for r in self.regions:
            self._draw_region_on_canvas(r)
        # refresh listbox
        self.regions_listbox.delete(0, "end")
        for r in self.regions:
            self.regions_listbox.insert("end", f"{r.name} ({r.mode})")
        self.update_selected_details()

    def on_region_select(self, event):
        sel = self.regions_listbox.curselection()
        if not sel:
            self.selected_idx = None
        else:
            self.selected_idx = sel[0]
        self.update_selected_details()

    def update_selected_details(self):
        # clear highlight
        for r in self.regions:
            if r.canvas_ids.get("highlight"):
                try:
                    self.canvas.delete(r.canvas_ids["highlight"])
                except Exception:
                    pass
                r.canvas_ids["highlight"] = None
        if self.selected_idx is None or self.selected_idx < 0 or self.selected_idx >= len(self.regions):
            self.sel_name_var.set("")
            self.expected_text_var.set("")
            self.ocr_preview.delete("1.0", "end")
            return
        r = self.regions[self.selected_idx]
        self.sel_name_var.set(r.name)
        self.expected_text_var.set(r.expected_text or "")
        # draw highlight
        scale = self.display_scale
        sx1 = int(r.x1 * scale); sy1 = int(r.y1 * scale)
        sx2 = int(r.x2 * scale); sy2 = int(r.y2 * scale)
        hid = self.canvas.create_rectangle(sx1, sy1, sx2, sy2, outline="#00AAFF", width=2, tags="highlight")
        r.canvas_ids["highlight"] = hid
        # OCR preview if text mode
        if r.mode == "text":
            self._run_ocr_for_region(r)
        else:
            self.ocr_preview.delete("1.0", "end")
            if r.expected_image:
                self.ocr_preview.insert("1.0", f"(Image template saved: {r.expected_image})")

    def rename_selected(self):
        if self.selected_idx is None:
            messagebox.showinfo("Rename", "No region selected.")
            return
        name = simpledialog.askstring("Rename region", "Enter new name:", initialvalue=self.regions[self.selected_idx].name, parent=self.root)
        if not name:
            return
        self.regions[self.selected_idx].name = name
        self.regions_listbox.delete(self.selected_idx)
        self.regions_listbox.insert(self.selected_idx, f"{name} ({self.regions[self.selected_idx].mode})")
        # update canvas label
        try:
            self.canvas.itemconfig(self.regions[self.selected_idx].canvas_ids["label"], text=name)
        except Exception:
            pass
        self.update_selected_details()

    def apply_name(self):
        if self.selected_idx is None:
            return
        new = self.sel_name_var.get().strip()
        if not new:
            return
        self.regions[self.selected_idx].name = new
        self.regions_listbox.delete(self.selected_idx)
        self.regions_listbox.insert(self.selected_idx, f"{new} ({self.regions[self.selected_idx].mode})")
        try:
            self.canvas.itemconfig(self.regions[self.selected_idx].canvas_ids["label"], text=new)
        except Exception:
            pass

    def delete_selected(self):
        if self.selected_idx is None:
            messagebox.showinfo("Delete", "No region selected.")
            return
        r = self.regions.pop(self.selected_idx)
        self.undo_stack.append(("del", r))
        # remove graphics
        try:
            for cid in r.canvas_ids.values():
                if cid:
                    self.canvas.delete(cid)
        except Exception:
            pass
        self.regions_listbox.delete(self.selected_idx)
        self.selected_idx = None
        self.update_selected_details()
        self.status_var.set(f"Deleted region: {r.name}")

    def set_expected_text(self):
        if self.selected_idx is None:
            return
        txt = self.expected_text_var.get().strip()
        self.regions[self.selected_idx].expected_text = txt
        self.status_var.set("Expected text set.")

    def toggle_mode(self):
        if self.selected_idx is None:
            return
        r = self.regions[self.selected_idx]
        r.mode = "image" if r.mode == "text" else "text"
        # update listbox label
        self.regions_listbox.delete(self.selected_idx)
        self.regions_listbox.insert(self.selected_idx, f"{r.name} ({r.mode})")
        # if switching to image, save image template
        if r.mode == "image":
            self._save_region_image(r)
        else:
            # clear image reference
            r.expected_image = None
        self.update_selected_details()
        self.status_var.set(f"Region {r.name} mode now {r.mode}")

    def _save_region_image(self, region):
        # crop region from original image and save
        crop = self.img_pil.crop((region.x1, region.y1, region.x2, region.y2))
        safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in region.name)
        fname = f"{safe_name}_{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}.png"
        full = os.path.join(TEMPLATES_IMAGES_DIR, fname)
        crop.save(full)
        region.expected_image = os.path.relpath(full, TEMPLATES_DIR)
        self.ocr_preview.delete("1.0", "end")
        self.ocr_preview.insert("1.0", f"Saved template image: {region.expected_image}")

    def _run_ocr_for_region(self, region):
        self.ocr_preview.delete("1.0", "end")
        if not OCR_AVAILABLE:
            self.ocr_preview.insert("1.0", "OCR not available. Install pytesseract + tesseract for OCR preview.")
            return
        crop = self.img_pil.crop((region.x1, region.y1, region.x2, region.y2))
        gray = ImageOps.grayscale(crop)
        # upscale small crops for better OCR
        up = max(1, int(300 / max(gray.size)))
        if up > 1:
            gray = gray.resize((gray.width * up, gray.height * up), Image.LANCZOS)
        try:
            text = pytesseract.image_to_string(gray, config="--psm 6")
        except Exception as e:
            text = f"OCR error: {e}"
        self.ocr_preview.insert("1.0", text)
        # offer to set expected_text automatically (do not overwrite existing)
        if not region.expected_text:
            region.expected_text = text.strip()
            self.expected_text_var.set(region.expected_text)

    def undo(self):
        if not self.undo_stack:
            self.status_var.set("Nothing to undo.")
            return
        action, region = self.undo_stack.pop()
        if action == "add":
            # remove region
            try:
                idx = self.regions.index(region)
                self.regions.pop(idx)
                self.regions_listbox.delete(idx)
                for cid in region.canvas_ids.values():
                    if cid:
                        try:
                            self.canvas.delete(cid)
                        except Exception:
                            pass
                self.status_var.set("Undo: removed last added region.")
            except ValueError:
                self.status_var.set("Undo failed: region not found.")
        elif action == "del":
            # re-add region
            self.regions.append(region)
            self.regions_listbox.insert("end", f"{region.name} ({region.mode})")
            self._draw_region_on_canvas(region)
            self.status_var.set("Undo: restored deleted region.")
        self.selected_idx = None
        self.update_selected_details()

    # ---------- Save/load template ----------
    def save_template(self):
        if not self.img_pil:
            messagebox.showwarning("No image", "Load a frame image first.")
            return
        out = filedialog.asksaveasfilename(defaultextension=".json", initialdir=TEMPLATES_DIR, filetypes=[("JSON","*.json")], title="Save template JSON")
        if not out:
            return
        data = {
            "source_image": os.path.basename(self.img_path) if self.img_path else None,
            "image_size": {"w": self.img_w, "h": self.img_h},
            "regions": [r.to_json(self.img_w, self.img_h) for r in self.regions]
        }
        with open(out, "w") as f:
            json.dump(data, f, indent=4)
        self.status_var.set(f"Saved template: {out}")
        messagebox.showinfo("Saved", f"Template saved to:\n{out}")

    def load_template(self):
        path = filedialog.askopenfilename(title="Select template JSON", filetypes=[("JSON","*.json")], initialdir=TEMPLATES_DIR)
        if not path:
            return
        with open(path, "r") as f:
            data = json.load(f)
        # clear current
        self.regions = []
        self.regions_listbox.delete(0, "end")
        self.canvas.delete("all")
        if self.img_tk:
            self.canvas.create_image(0,0,anchor="nw",image=self.img_tk, tags="bgimg")
        # if template contains different image size, we still load normalized coords
        img_size = data.get("image_size", {})
        tmpl_w = img_size.get("w", self.img_w) or self.img_w
        tmpl_h = img_size.get("h", self.img_h) or self.img_h
        for reg in data.get("regions", []):
            name = reg.get("name")
            mode = reg.get("mode", "text")
            coords = reg.get("coords", {})
            x = int(coords.get("x", 0) * self.img_w)
            y = int(coords.get("y", 0) * self.img_h)
            w = int(coords.get("w", 0) * self.img_w)
            h = int(coords.get("h", 0) * self.img_h)
            region = Region(name, x, y, x + w, y + h, mode=mode)
            if mode == "text":
                region.expected_text = reg.get("expected_text")
            else:
                region.expected_image = reg.get("expected_image")
            self.regions.append(region)
            self.regions_listbox.insert("end", f"{region.name} ({region.mode})")
            self._draw_region_on_canvas(region)
        self.status_var.set(f"Loaded template: {os.path.basename(path)}")
        self.selected_idx = None
        self.update_selected_details()

    # ---------- Shortcuts ----------
    def _bind_shortcuts(self):
        self.root.bind("<Key-n>", lambda e: self.add_region_prompt())
        self.root.bind("<Key-N>", lambda e: self.add_region_prompt())
        self.root.bind("<Delete>", lambda e: self.delete_selected())
        self.root.bind("<Key-i>", lambda e: self.toggle_mode())
        self.root.bind("<Key-I>", lambda e: self.toggle_mode())
        self.root.bind("<Control-s>", lambda e: self.save_template())
        self.root.bind("<Control-S>", lambda e: self.save_template())
        self.root.bind("<Control-z>", lambda e: self.undo())
        self.root.bind("<Control-Z>", lambda e: self.undo())

# ----------------- main -----------------
def main():
    root = tk.Tk()
    app = TemplateCreator(root)
    # initial window size
    root.geometry("1400x900")
    root.mainloop()

if __name__ == "__main__":
    main()
