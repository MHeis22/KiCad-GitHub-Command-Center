"""Overlays automatic dimensions onto a top/bottom PCB render.

Values are parsed straight from the .kicad_pcb (never guessed): overall width &
height from the Edge.Cuts bounding box, corner radius when the outline is a
rounded rectangle, and each mounting hole's diameter + position. The board->pixel
transform is recovered from the render itself by detecting the board silhouette,
so it needs no knowledge of kicad-cli's internal camera framing.

Adapts to the board:
  * clean, symmetric hole pattern  -> one annotated hole + "xN" and edge offsets
  * irregular / asymmetric holes    -> every hole gets its own diameter + position
  * non-rectangular outline         -> the true outline is traced and only the
                                       bounding envelope (W x H) is dimensioned
Only depends on numpy + Pillow (both ship inside KiCad's Python).
"""
import os
import re
import glob
import math

IMAGE_SUBDIR = "docs"


# ----------------------------------------------------------------- s-expr parse
def _blocks(src, tag):
    """Yield each balanced-paren '(tag ...)' block in src."""
    out, i, key = [], 0, "(" + tag
    while True:
        j = src.find(key, i)
        if j < 0:
            break
        depth = 0
        for k in range(j, len(src)):
            if src[k] == '(':
                depth += 1
            elif src[k] == ')':
                depth -= 1
                if depth == 0:
                    out.append(src[j:k+1])
                    i = k + 1
                    break
        else:
            break
    return out


def _layer(b):
    m = re.search(r'\(layer\s+"([^"]+)"', b)
    return m.group(1) if m else None


def _pts(b, kind):
    return [(float(x), float(y)) for x, y in
            re.findall(r'\(%s\s+(-?[\d.]+)\s+(-?[\d.]+)\)' % kind, b)]


def _circumradius(a, b, c):
    (ax, ay), (bx, by), (cx, cy) = a, b, c
    d = 2 * (ax*(by-cy) + bx*(cy-ay) + cx*(ay-by))
    if abs(d) < 1e-9:
        return None
    ux = ((ax*ax+ay*ay)*(by-cy) + (bx*bx+by*by)*(cy-ay) + (cx*cx+cy*cy)*(ay-by)) / d
    uy = ((ax*ax+ay*ay)*(cx-bx) + (bx*bx+by*by)*(ax-cx) + (cx*cx+cy*cy)*(bx-ax)) / d
    return math.hypot(ax-ux, ay-uy)


def parse_outline(pcb_text):
    """Returns dict: bbox (x0,y0,x1,y1), w, h, radius (None if not a rounded
    rect), is_rect (simple rectangle outline), and segs (polyline pts for
    tracing an irregular outline)."""
    xs, ys, radii, segs, allpts = [], [], [], [], []
    rect = None

    def add(px, py):
        xs.append(px); ys.append(py); allpts.append((px, py))

    for b in _blocks(pcb_text, "gr_rect"):
        if _layer(b) != "Edge.Cuts":
            continue
        p = _pts(b, "start") + _pts(b, "end")
        if len(p) >= 2:
            (sx, sy), (ex, ey) = p[0], p[1]
            for x, y in ((sx, sy), (ex, ey)):
                add(x, y)
            mr = re.search(r'\(radius\s+([\d.]+)\)', b)
            rect = {"x0": min(sx, ex), "y0": min(sy, ey), "x1": max(sx, ex),
                    "y1": max(sy, ey), "radius": float(mr.group(1)) if mr else 0.0}

    for b in _blocks(pcb_text, "gr_line"):
        if _layer(b) != "Edge.Cuts":
            continue
        p = _pts(b, "start") + _pts(b, "end")
        if len(p) >= 2:
            add(*p[0]); add(*p[1])
            segs.append(("line", p[0], p[1]))

    for b in _blocks(pcb_text, "gr_arc"):
        if _layer(b) != "Edge.Cuts":
            continue
        s = _pts(b, "start"); m = _pts(b, "mid"); e = _pts(b, "end")
        if s and m and e:
            for pt in (s[0], m[0], e[0]):
                add(*pt)
            r = _circumradius(s[0], m[0], e[0])
            if r:
                radii.append(r)
            segs.append(("arc", s[0], m[0], e[0]))

    for b in _blocks(pcb_text, "gr_poly"):
        if _layer(b) != "Edge.Cuts":
            continue
        p = _pts(b, "xy")
        for pt in p:
            add(*pt)
        if len(p) >= 2:
            segs.append(("poly", p))

    for b in _blocks(pcb_text, "gr_circle"):
        if _layer(b) != "Edge.Cuts":
            continue
        c = _pts(b, "center") or _pts(b, "start")
        e = _pts(b, "end")
        if c and e:
            r = math.hypot(c[0][0]-e[0][0], c[0][1]-e[0][1])
            add(c[0][0]-r, c[0][1]-r); add(c[0][0]+r, c[0][1]+r)

    if not xs or not ys:
        return None

    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    # A single gr_rect with no other Edge.Cuts geometry is a clean rectangle.
    is_rect = rect is not None and not segs
    radius = None
    if is_rect and rect["radius"] > 0:
        radius = rect["radius"]
    elif radii and (max(radii) - min(radii)) < 0.05 and not rect:
        radius = sum(radii) / len(radii)   # uniform fillet -> rounded rectangle

    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "w": x1 - x0, "h": y1 - y0, "radius": radius,
            "is_rect": is_rect, "segs": segs, "pts": allpts}


def parse_mounting_holes(pcb_text):
    """Returns [{ref, x, y, d, slot}] for every MountingHole footprint."""
    holes = []
    for b in _blocks(pcb_text, "footprint"):
        if "MountingHole" not in b:
            continue
        at = re.search(r'\(at\s+(-?[\d.]+)\s+(-?[\d.]+)', b)
        ref = re.search(r'\(property\s+"Reference"\s+"([^"]+)"', b)
        # oval/slot drills look like: (drill oval W H ...)
        slot = bool(re.search(r'\(drill\s+oval', b))
        drills = [float(x) for x in re.findall(r'\(drill\s+(?:oval\s+)?([\d.]+)', b)]
        if at and drills:
            holes.append({"ref": ref.group(1) if ref else "?",
                          "x": float(at.group(1)), "y": float(at.group(2)),
                          "d": max(drills), "slot": slot})
    return holes


def _is_regular(holes, cx, cy, tol=0.25):
    """True when holes share one diameter and form a symmetric pattern about the
    board centre (so one annotated hole + 'xN' faithfully describes them all)."""
    if len(holes) < 2:
        return False
    d0 = holes[0]["d"]
    if any(abs(h["d"] - d0) > 0.05 for h in holes):
        return False
    if any(h["slot"] for h in holes):
        return False
    key0 = (round(abs(holes[0]["x"] - cx), 2), round(abs(holes[0]["y"] - cy), 2))
    return all(abs(abs(h["x"]-cx) - key0[0]) < tol and
               abs(abs(h["y"]-cy) - key0[1]) < tol for h in holes)


# --------------------------------------------------------------- image + draw
class DimensionAnnotator:
    def __init__(self, project_dir):
        self.project_dir = project_dir
        pcbs = glob.glob(os.path.join(project_dir, "*.kicad_pcb"))
        self.pcb = pcbs[0] if pcbs else None

    def available(self):
        return self.pcb is not None

    def annotate(self, render_path, side="top", out_path=None, text_px=60):
        """Draw dimensions onto render_path; overwrites in place unless out_path
        is given. Returns the output path, or None if it couldn't be produced.

        Imports numpy/Pillow lazily so a missing lib degrades to 'no overlay'
        rather than breaking the render."""
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont

        if not self.pcb or not os.path.exists(render_path):
            return None
        text = open(self.pcb, encoding="utf-8", errors="ignore").read()
        outline = parse_outline(text)
        if not outline:
            return None
        holes = parse_mounting_holes(text)

        img = Image.open(render_path).convert("RGBA")

        # --- recover board pixel rectangle from the render silhouette ---
        arr = np.asarray(img)
        alpha = arr[:, :, 3]
        if int(alpha.min()) < 250:
            mask = alpha > 20
        else:  # opaque bg: everything that differs from the sampled corners
            rgb = arr[:, :, :3].astype(np.int16)
            bg = np.median(np.array([rgb[0, 0], rgb[0, -1], rgb[-1, 0], rgb[-1, -1]]), axis=0)
            mask = np.abs(rgb - bg).sum(2) > 60
        col = mask.sum(0); row = mask.sum(1)
        if col.max() == 0 or row.max() == 0:
            return None
        cxs = np.where(col > 0.5 * col.max())[0]
        rys = np.where(row > 0.5 * row.max())[0]
        xL, xR, yT, yB = int(cxs.min()), int(cxs.max()), int(rys.min()), int(rys.max())

        X0, Y0, X1, Y1 = outline["x0"], outline["y0"], outline["x1"], outline["y1"]
        SX = (xR - xL) / (X1 - X0) if X1 > X0 else 1.0
        SY = (yB - yT) / (Y1 - Y0) if Y1 > Y0 else 1.0
        mirror = (side == "bottom")

        M = int(text_px * 4.2)          # margin scales with text
        canvas = Image.new("RGBA", (img.width + 2*M, img.height + 2*M), (255, 255, 255, 255))
        canvas.alpha_composite(img, (M, M))
        d = ImageDraw.Draw(canvas)

        def PX(mx, my):
            px = (xR - (mx - X0) * SX) if mirror else (xL + (mx - X0) * SX)
            return px + M, yT + (my - Y0) * SY + M
        bL, bR = (PX(X1, Y0)[0], PX(X0, Y0)[0]) if mirror else (PX(X0, Y0)[0], PX(X1, Y0)[0])
        bT, bBt = PX(X0, Y0)[1], PX(X0, Y1)[1]

        INK = (36, 46, 64, 255)
        ACC = (200, 70, 40, 255)        # subtle accent for a traced outline
        try:
            f = ImageFont.truetype("arialbd.ttf", text_px)
            fs = ImageFont.truetype("arial.ttf", int(text_px * 0.85))
        except Exception:
            f = fs = ImageFont.load_default()
        LW = max(2, text_px // 22)
        AR = int(text_px * 0.42)

        def arrow(x, y, dx, dy):
            ang = math.atan2(dy, dx); w = 0.42
            d.polygon([(x, y),
                       (x-AR*math.cos(ang-w), y-AR*math.sin(ang-w)),
                       (x-AR*math.cos(ang+w), y-AR*math.sin(ang+w))], fill=INK)

        def label(cx, cy, s, font=f, box=True):
            l, t, r, b = d.textbbox((0, 0), s, font=font)
            w, h = r-l, b-t
            if box:
                d.rectangle([cx-w/2-6, cy-h/2-6, cx+w/2+6, cy+h/2+6], fill=(255, 255, 255, 255))
            d.text((cx-w/2-l, cy-h/2-t), s, font=font, fill=INK)

        def vlabel(cx, cy, s, font=f):
            tmp = Image.new("RGBA", (text_px*8, text_px*2), (255, 255, 255, 255))
            td = ImageDraw.Draw(tmp)
            l, t, r, b = td.textbbox((0, 0), s, font=font)
            td.text((0, 0), s, font=font, fill=INK)
            tmp = tmp.crop((l-4, t-4, r+4, b+4)).rotate(90, expand=True)
            canvas.alpha_composite(tmp, (int(cx-tmp.width/2), int(cy-tmp.height/2)))

        def hdim(x1, x2, y, feat_y, s):
            for x in (x1, x2):
                d.line([(x, feat_y), (x, y + (8 if y > feat_y else -8))], fill=INK, width=1)
            d.line([(x1, y), (x2, y)], fill=INK, width=LW)
            arrow(x1, y, -1, 0); arrow(x2, y, 1, 0)
            label((x1+x2)/2, y - text_px*0.7, s)

        def vdim(y1, y2, x, feat_x, s):
            for y in (y1, y2):
                d.line([(feat_x, y), (x + (8 if x > feat_x else -8), y)], fill=INK, width=1)
            d.line([(x, y1), (x, y2)], fill=INK, width=LW)
            arrow(x, y1, 0, -1); arrow(x, y2, 0, 1)
            vlabel(x + text_px*0.7, (y1+y2)/2, s)

        def leader(px, py, tx, ty, s, anchor_left=True):
            d.line([(px, py), (tx, ty)], fill=INK, width=LW)
            d.ellipse([px-5, py-5, px+5, py+5], fill=INK)
            l, t, r, b = d.textbbox((0, 0), s, font=fs)
            w, h = r-l, b-t
            lx = tx if anchor_left else tx - w
            d.rectangle([lx-6, ty-h-8, lx+w+6, ty+8], fill=(255, 255, 255, 255))
            d.text((lx-l, ty-h-4-t), s, font=fs, fill=INK)

        # --- outline trace for non-rectangular boards (shows the true shape) ---
        if not outline["is_rect"] and outline["segs"]:
            def seg_line(p1, p2):
                d.line([PX(*p1), PX(*p2)], fill=ACC, width=LW)
            for seg in outline["segs"]:
                if seg[0] == "line":
                    seg_line(seg[1], seg[2])
                elif seg[0] == "poly":
                    pl = seg[1]
                    for i in range(len(pl)):
                        seg_line(pl[i], pl[(i+1) % len(pl)])
                elif seg[0] == "arc":
                    s, m, e = seg[1], seg[2], seg[3]
                    prev = s
                    for tt in (0.25, 0.5, 0.75, 1.0):     # coarse quadratic tessellation
                        qx = (1-tt)**2*s[0] + 2*(1-tt)*tt*m[0] + tt*tt*e[0]
                        qy = (1-tt)**2*s[1] + 2*(1-tt)*tt*m[1] + tt*tt*e[1]
                        seg_line(prev, (qx, qy)); prev = (qx, qy)

        # --- overall width & height (bounding envelope) ---
        hdim(bL, bR, bBt + int(text_px*1.6), bBt, f"{outline['w']:.1f}")
        vdim(bT, bBt, bR + int(text_px*1.6), bR, f"{outline['h']:.1f}")

        # --- corner radius (only when the outline is genuinely a rounded rect) ---
        if outline["radius"]:
            R = outline["radius"]
            cxp, cyp = PX(X0 + R*0.29, Y0 + R*0.29)
            leader(cxp, cyp, bL - int(text_px*2), bT - int(text_px*1.2), f"R{R:.1f}")

        cx, cy = (X0+X1)/2, (Y0+Y1)/2
        regular = _is_regular(holes, cx, cy)

        if holes and regular:
            # one representative hole + xN, plus its two edge offsets in the margins
            h = min(holes, key=lambda h: (h["x"], h["y"]))
            hx, hy = PX(h["x"], h["y"]); rp = h["d"]/2 * SX
            d.ellipse([hx-rp, hy-rp, hx+rp, hy+rp], outline=INK, width=LW)
            dyt = bT - int(text_px*1.3)
            xedge = bR if mirror else bL
            d.line([(xedge, bT), (xedge, dyt-6)], fill=INK, width=1)
            d.line([(hx, hy), (hx, dyt-6)], fill=INK, width=1)
            d.line([(xedge, dyt), (hx, dyt)], fill=INK, width=LW)
            arrow(xedge, dyt, -1 if xedge < hx else 1, 0); arrow(hx, dyt, 1 if hx > xedge else -1, 0)
            label((xedge+hx)/2, dyt - text_px*0.62, f"{abs(h['x']-X0):.1f}", fs)
            dxl = bL - int(text_px*1.3)
            d.line([(bL, bT), (dxl-6, bT)], fill=INK, width=1)
            d.line([(hx, hy), (dxl-6, hy)], fill=INK, width=1)
            d.line([(dxl, bT), (dxl, hy)], fill=INK, width=LW)
            arrow(dxl, bT, 0, -1); arrow(dxl, hy, 0, 1)
            vlabel(dxl - text_px*0.62, (bT+hy)/2, f"{abs(h['y']-Y0):.1f}", fs)
            leader(hx - rp*0.71, hy + rp*0.71, bL - int(text_px*2.6), hy + int(text_px*1.6),
                   f"Ø{h['d']:.1f}  x{len(holes)}")
        else:
            # irregular / asymmetric: label every hole with its own d + position
            for h in holes:
                hx, hy = PX(h["x"], h["y"]); rp = h["d"]/2 * SX
                d.ellipse([hx-rp, hy-rp, hx+rp, hy+rp], outline=INK, width=LW)
                dx, dy = abs(h["x"]-X0), abs(h["y"]-Y0)
                tag = ("slot " if h["slot"] else "Ø") + f"{h['d']:.1f}"
                # push the label toward the nearest horizontal margin
                if hx < (bL + bR) / 2:
                    tx, al = bL - int(text_px*2.6), True
                else:
                    tx, al = bR + int(text_px*2.6), False
                leader(hx, hy, tx, hy, f"{tag}  ({dx:.1f}, {dy:.1f})", anchor_left=al)

        # --- outline features: internal cutouts & perimeter notches ---
        # Any fully-transparent region inside the board bbox is either a cut-out
        # (window/slot) or an edge notch. Detect them from the render alpha, drop
        # the mounting holes (already dimensioned) and rounded-corner slivers,
        # then dimension what's left. Skipped for a plain rectangle outline.
        if not outline["is_rect"]:
            try:
                self._annotate_features(np, arr, d, holes, outline, PX,
                                        xL, xR, yT, yB, SX, SY, M,
                                        bL, bR, ACC, LW, text_px, leader)
            except Exception as e:
                print(f"GitHub Command Center: outline-feature dimensioning skipped ({e})")

        out = out_path or render_path
        canvas.convert("RGB").save(out)
        return out

    def _annotate_features(self, np, arr, d, holes, outline, PX,
                           xL, xR, yT, yB, SX, SY, M, bL, bR, ACC, LW, text_px, leader):
        X0, Y0 = outline["x0"], outline["y0"]
        alpha = arr[yT:yB, xL:xR, 3]
        fac = max(1, int((xR - xL) / 500))          # work on a downsampled grid
        holes_ds = alpha[::fac, ::fac] < 40          # fully-transparent = cutout/notch
        Hh, Ww = holes_ds.shape

        # remove the mounting holes so they don't read as cut-outs
        for h in holes:
            hxp, hyp = PX(h["x"], h["y"])
            gx = int((hxp - M - xL) / fac); gy = int((hyp - M - yT) / fac)
            rg = int((h["d"]/2 * SX) / fac) + 3
            holes_ds[max(0, gy-rg):gy+rg+1, max(0, gx-rg):gx+rg+1] = False

        # connected components (iterative flood fill)
        seen = np.zeros_like(holes_ds)
        comps = []
        for (yy, xx) in np.argwhere(holes_ds):
            if seen[yy, xx]:
                continue
            stack = [(yy, xx)]; seen[yy, xx] = True; pts = []
            while stack:
                cy, cx = stack.pop(); pts.append((cy, cx))
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = cy+dy, cx+dx
                    if 0 <= ny < Hh and 0 <= nx < Ww and holes_ds[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True; stack.append((ny, nx))
            comps.append(pts)

        R = outline["radius"] or 0.0
        feats = []
        for pts in comps:
            ys = [p[0] for p in pts]; xs = [p[1] for p in pts]
            gx0, gx1, gy0, gy1 = min(xs), max(xs), min(ys), max(ys)
            w_mm = (gx1 - gx0 + 1) * fac / SX
            h_mm = (gy1 - gy0 + 1) * fac / SY
            if min(w_mm, h_mm) < 2.5 or w_mm * h_mm < 6:
                continue
            tl, tr = gx0 <= 1, gx1 >= Ww - 2
            tt, tb = gy0 <= 1, gy1 >= Hh - 2
            notch = tl or tr or tt or tb
            # skip rounded-corner slivers (a corner region roughly the size of R)
            if notch and (tl or tr) and (tt or tb) and R and max(w_mm, h_mm) < 1.4 * R:
                continue
            # An internal region is only a real board cut-out if Edge.Cuts
            # geometry surrounds it; otherwise it's a through-hole pad/via that
            # happens to be transparent, so drop it.
            if not notch:
                mx0 = (gx0 * fac) / SX + X0; mx1 = ((gx1 + 1) * fac) / SX + X0
                my0 = (gy0 * fac) / SY + Y0; my1 = ((gy1 + 1) * fac) / SY + Y0
                mgn = 3.0
                near = any(mx0-mgn <= px <= mx1+mgn and my0-mgn <= py <= my1+mgn
                           for px, py in outline["pts"])
                if not near:
                    continue
            feats.append((w_mm * h_mm, gx0, gx1, gy0, gy1, w_mm, h_mm, notch))

        feats.sort(reverse=True)     # biggest first; cap to avoid clutter
        for _, gx0, gx1, gy0, gy1, w_mm, h_mm, notch in feats[:6]:
            fx0 = xL + gx0 * fac + M; fx1 = xL + (gx1 + 1) * fac + M
            fy0 = yT + gy0 * fac + M; fy1 = yT + (gy1 + 1) * fac + M
            d.rectangle([fx0, fy0, fx1, fy1], outline=ACC, width=LW)
            cxm, cym = (fx0 + fx1) / 2, (fy0 + fy1) / 2
            if notch:
                lbl = f"{w_mm:.1f} x {h_mm:.1f}"
                if cxm < (bL + bR) / 2:
                    leader(fx0, cym, bL - int(text_px * 2.6), cym, lbl, anchor_left=True)
                else:
                    leader(fx1, cym, bR + int(text_px * 2.6), cym, lbl, anchor_left=False)
            else:
                x_off = (min(fx0, fx1) - M - xL) / SX
                y_off = (fy0 - M - yT) / SY
                leader(cxm, cym, cxm, fy0 - int(text_px * 1.4),
                       f"{w_mm:.1f} x {h_mm:.1f}  ({x_off:.1f}, {y_off:.1f})")
