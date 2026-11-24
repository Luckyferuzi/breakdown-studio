"""
BreakdownStudio 3D — Zero-Dependency NumPy + Blender Native Video Compositor
Uses Blender's built-in NumPy and `bpy.data.images` + Blender's internal Video
Sequencer (with optional system FFmpeg acceleration) so every user on Windows,
macOS, or Linux gets:
- Anti-aliased 3-way diagonal split posters (preserving transparent alpha if enabled)
- Pure-NumPy Technical Spec HUD bar (zero external font or FFmpeg dependency)
- Smooth MP4 wipe & turntable videos (via Blender's native H.264 encoder or FFmpeg)
"""

import shutil
import subprocess
from pathlib import Path
import numpy as np

try:
    import bpy
except ImportError:
    bpy = None


def find_ffmpeg() -> str | None:
    """Checks if `ffmpeg` is available on the system PATH."""
    return shutil.which("ffmpeg")


def smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, float(t)))
    return t * t * (3.0 - 2.0 * t)


def load_image_rgba(path: str | Path) -> np.ndarray:
    """
    Loads an image file into a (H, W, 4) float32 array in [0..1] (top-to-bottom row order)
    using Blender's native `bpy.data.images`.
    """
    img = bpy.data.images.load(str(Path(path).resolve()), check_existing=False)
    w, h = img.size[0], img.size[1]
    buf = np.empty(w * h * 4, dtype=np.float32)
    img.pixels.foreach_get(buf)
    bpy.data.images.remove(img)
    arr = buf.reshape((h, w, 4))[::-1, :, :].copy()
    return arr


def save_image_rgba(arr: np.ndarray, path: str | Path):
    """Saves a (H, W, 4) float32 array [0..1] to disk as an sRGB PNG via `bpy.data.images`."""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    h, w, _ = arr.shape
    flipped = np.ascontiguousarray(arr[::-1, :, :], dtype=np.float32).ravel()

    img = bpy.data.images.new(name="_BS_Temp_Out", width=w, height=h, alpha=True, float_buffer=False)
    img.colorspace_settings.name = "sRGB"
    img.pixels.foreach_set(flipped)
    img.filepath_raw = str(path)
    img.file_format = "PNG"
    img.save()
    bpy.data.images.remove(img)


def _detect_subject_x_span(arr_wire: np.ndarray, arr_clay: np.ndarray) -> tuple[float, float]:
    """Finds the horizontal pixel span (x_left, x_right) of the 3D subject."""
    h, w, _ = arr_wire.shape
    diff = np.max(np.abs(arr_wire[..., :3] - arr_clay[..., :3]), axis=-1)
    col_active = np.percentile(diff, 92, axis=0) > 0.035
    xs = np.where(col_active)[0]
    if len(xs) < 10:
        return (w * 0.25, w * 0.75)
    x_left = float(np.percentile(xs, 3))
    x_right = float(np.percentile(xs, 97))
    if (x_right - x_left) < w * 0.15:
        cx = 0.5 * (x_left + x_right)
        return (cx - w * 0.12, cx + w * 0.12)
    return (x_left, x_right)


def composite_three_way_numpy(
    arr_wire: np.ndarray,
    arr_clay: np.ndarray,
    arr_final: np.ndarray,
) -> np.ndarray:
    """
    Vectorized anti-aliased 3-way diagonal split composite in pure NumPy.
    Preserves true RGBA alpha when Transparent Alpha stage theme is used.
    """
    h, w, _ = arr_final.shape
    yy = np.linspace(-0.5, 0.5, h, dtype=np.float32)[:, None]
    xx = np.arange(w, dtype=np.float32)[None, :]

    x_left, x_right = _detect_subject_x_span(arr_wire, arr_clay)
    span = x_right - x_left
    tilt = max(span * 0.22, w * 0.06)

    x1_mid = x_left + span * 0.35
    x2_mid = x_left + span * 0.66

    b1 = x1_mid - yy * tilt
    b2 = x2_mid - yy * tilt

    w_clay = np.clip((xx - (b1 - 1.0)) / 2.0, 0.0, 1.0)[..., None]
    w_final = np.clip((xx - (b2 - 1.0)) / 2.0, 0.0, 1.0)[..., None]

    out = arr_wire * (1.0 - w_clay) + arr_clay * w_clay
    out = out * (1.0 - w_final) + arr_final * w_final

    cyan = np.array([0.37, 0.88, 1.0, 1.0], dtype=np.float32)
    gold = np.array([0.98, 0.84, 0.51, 1.0], dtype=np.float32)

    line1 = np.clip(2.2 - np.abs(xx - b1), 0.0, 1.0)[..., None]
    line2 = np.clip(2.2 - np.abs(xx - b2), 0.0, 1.0)[..., None]

    out = out * (1.0 - line1) + cyan * line1
    out = out * (1.0 - line2) + gold * line2
    return np.clip(out, 0.0, 1.0)


def composite_moving_wipe_numpy(
    arr_wire: np.ndarray,
    arr_clay: np.ndarray,
    arr_final: np.ndarray,
    progress: float,
) -> np.ndarray:
    """Vectorized anti-aliased moving wipe frame at normalized `progress` in [0.0 .. 1.0]."""
    h, w, _ = arr_final.shape
    yy = np.linspace(-0.5, 0.5, h, dtype=np.float32)[:, None]
    xx = np.arange(w, dtype=np.float32)[None, :]

    x_left, x_right = _detect_subject_x_span(arr_wire, arr_clay)
    span = x_right - x_left
    tilt = max(span * 0.22, w * 0.06)

    cyan = np.array([0.37, 0.88, 1.0, 1.0], dtype=np.float32)
    gold = np.array([0.98, 0.84, 0.51, 1.0], dtype=np.float32)

    if progress < 0.32:
        p = smoothstep(progress / 0.32)
        x_mid = -tilt + p * (w + 2.0 * tilt)
        b_edge = x_mid - yy * tilt
        w_reveal = np.clip((b_edge - xx + 1.0) / 2.0, 0.0, 1.0)[..., None]
        out = arr_wire * (1.0 - w_reveal) + arr_clay * w_reveal
        line = np.clip(2.4 - np.abs(xx - b_edge), 0.0, 1.0)[..., None]
        out = out * (1.0 - line) + cyan * line

    elif progress < 0.68:
        p = smoothstep((progress - 0.32) / 0.36)
        x_mid = -tilt + p * (w + 2.0 * tilt)
        b_edge = x_mid - yy * tilt
        w_reveal = np.clip((b_edge - xx + 1.0) / 2.0, 0.0, 1.0)[..., None]
        out = arr_clay * (1.0 - w_reveal) + arr_final * w_reveal
        line = np.clip(2.4 - np.abs(xx - b_edge), 0.0, 1.0)[..., None]
        out = out * (1.0 - line) + gold * line

    else:
        p = smoothstep(min(1.0, (progress - 0.68) / 0.22))
        x1_target = x_left + span * 0.35
        x2_target = x_left + span * 0.66
        x1_mid = -tilt + p * (x1_target + tilt)
        x2_mid = -tilt + p * (x2_target + tilt)
        b1 = x1_mid - yy * tilt
        b2 = x2_mid - yy * tilt

        w_clay = np.clip((xx - (b1 - 1.0)) / 2.0, 0.0, 1.0)[..., None]
        w_final = np.clip((xx - (b2 - 1.0)) / 2.0, 0.0, 1.0)[..., None]

        out = arr_wire * (1.0 - w_clay) + arr_clay * w_clay
        out = out * (1.0 - w_final) + arr_final * w_final

        if p > 0.01:
            line1 = np.clip(2.2 - np.abs(xx - b1), 0.0, 1.0)[..., None]
            line2 = np.clip(2.2 - np.abs(xx - b2), 0.0, 1.0)[..., None]
            out = out * (1.0 - line1) + cyan * line1
            out = out * (1.0 - line2) + gold * line2

    return np.clip(out, 0.0, 1.0)


# Compact 5x7 bitmap font for pure-NumPy cross-platform HUD rendering (zero external dependencies)
_FONT_5X7 = {
    " ": ["00000", "00000", "00000", "00000", "00000", "00000", "00000"],
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00110", "01000", "10000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["01110", "10000", "11110", "10001", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00001", "01110"],
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
    "D": ["11100", "10010", "10001", "10001", "10001", "10010", "11100"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01110", "10001", "10000", "10111", "10001", "10001", "01110"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["01110", "00100", "00100", "00100", "00100", "00100", "01110"],
    "J": ["00111", "00010", "00010", "00010", "10010", "10010", "01100"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "Q": ["01110", "10001", "10001", "10001", "10101", "10010", "01101"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "11011", "10001"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "Z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
    "/": ["00001", "00010", "00010", "00100", "01000", "01000", "10000"],
    ":": ["00000", "00100", "00100", "00000", "00100", "00100", "00000"],
    "|": ["00100", "00100", "00100", "00100", "00100", "00100", "00100"],
    ",": ["00000", "00000", "00000", "00000", "00100", "00100", "01000"],
    ".": ["00000", "00000", "00000", "00000", "00000", "00110", "00110"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    "+": ["00000", "00100", "00100", "11111", "00100", "00100", "00000"],
}


def _draw_text_5x7(arr: np.ndarray, text: str, x0: int, y0: int, scale: int, color: tuple[float, float, float, float]):
    h, w, _ = arr.shape
    col_arr = np.array(color, dtype=np.float32)
    cx = x0
    for ch in text.upper():
        glyph = _FONT_5X7.get(ch, _FONT_5X7[" "])
        for r_idx, row_str in enumerate(glyph):
            for c_idx, bit in enumerate(row_str):
                if bit == "1":
                    py0 = y0 + r_idx * scale
                    py1 = min(h, py0 + scale)
                    px0 = cx + c_idx * scale
                    px1 = min(w, px0 + scale)
                    if py0 >= 0 and px0 >= 0 and py0 < h and px0 < w:
                        arr[py0:py1, px0:px1] = col_arr
        cx += 6 * scale


def stamp_technical_spec_hud(poster_png: Path, out_png: Path, poly_stats: dict | None = None) -> bool:
    """
    Stamps a Technical Spec HUD bar at the bottom of the poster in pure NumPy.
    Requires ZERO external FFmpeg or OS font files — works identically on Windows, macOS, and Linux.
    """
    if not poster_png.exists():
        return False
    arr = load_image_rgba(poster_png)
    h, w, _ = arr.shape
    bar_h = max(48, int(h * 0.052))
    y_top = h - bar_h

    # Dark translucent HUD bar + cyan accent line
    bg_col = np.array([0.043, 0.051, 0.071, 0.94], dtype=np.float32)
    arr[y_top:h, :, :3] = arr[y_top:h, :, :3] * 0.12 + bg_col[:3] * 0.88
    arr[y_top:h, :, 3] = np.maximum(arr[y_top:h, :, 3], 0.95)
    arr[y_top : min(h, y_top + 2), :, :] = np.array([0.28, 0.82, 1.0, 1.0], dtype=np.float32)

    scale = 2 if w >= 900 else 1
    text_y = y_top + max(6, (bar_h - 7 * scale) // 2)
    left_label = "01 // WIREFRAME    02 // SCULPTOR CLAY    03 // FINAL PBR"
    _draw_text_5x7(arr, left_label, 28, text_y, scale, (0.96, 0.97, 0.99, 1.0))

    verts = f"{poly_stats.get('vertices', 0):,}" if poly_stats else "0"
    tris = f"{poly_stats.get('triangles', 0):,}" if poly_stats else "0"
    right_label = f"VERTS: {verts}  |  TRIS: {tris}"
    right_w = len(right_label) * 6 * scale
    if w - right_w - 28 > len(left_label) * 6 * scale + 48:
        _draw_text_5x7(arr, right_label, w - right_w - 28, text_y, scale, (0.36, 0.88, 0.92, 1.0))

    save_image_rgba(arr, out_png)
    return out_png.exists()


def _encode_sequence_via_blender_vse(frames_dir: Path, out_mp4: Path, num_frames: int, fps: int = 30) -> bool:
    """
    Encodes a PNG frame sequence (`frame_0000.png` .. `frame_NNNN.png`) into an H.264 `.mp4`
    using Blender's built-in Video Sequencer (`FFMPEG` / `H264`) in a temporary scene.
    Requires ZERO external binaries.
    """
    if bpy is None:
        return False
    first_frame = frames_dir / "frame_0000.png"
    if not first_frame.exists():
        return False

    arr0 = load_image_rgba(first_frame)
    h, w, _ = arr0.shape
    # Ensure even dimensions for H.264
    res_x = w - (w % 2)
    res_y = h - (h % 2)

    orig_window = bpy.context.window
    orig_scene = orig_window.scene if orig_window else bpy.context.scene
    vse_scene = bpy.data.scenes.new("_BS_Temp_VSE_Encode")
    try:
        vse_scene.render.resolution_x = res_x
        vse_scene.render.resolution_y = res_y
        vse_scene.render.resolution_percentage = 100
        vse_scene.render.fps = fps
        vse_scene.frame_start = 1
        vse_scene.frame_end = num_frames
        vse_scene.render.use_sequencer = True
        vse_scene.render.use_compositing = False

        if hasattr(vse_scene.render.image_settings, "media_type"):
            vse_scene.render.image_settings.media_type = "VIDEO"
        vse_scene.render.image_settings.file_format = "FFMPEG"
        vse_scene.render.ffmpeg.format = "MPEG4"
        vse_scene.render.ffmpeg.codec = "H264"
        vse_scene.render.ffmpeg.constant_rate_factor = "HIGH"
        vse_scene.render.ffmpeg.audio_codec = "NONE"
        vse_scene.render.filepath = str(out_mp4)

        vse_scene.sequence_editor_create()
        strips = getattr(vse_scene.sequence_editor, "strips", None) or getattr(vse_scene.sequence_editor, "sequences", None)
        strip = strips.new_image(
            name="BS_Wipe_Seq",
            filepath=str(first_frame),
            channel=1,
            frame_start=1,
        )
        for i in range(1, num_frames):
            f_path = frames_dir / f"frame_{i:04d}.png"
            if f_path.exists():
                strip.elements.append(f_path.name)

        if orig_window:
            orig_window.scene = vse_scene
            bpy.ops.render.render(animation=True)
        else:
            bpy.ops.render.render(animation=True, scene=vse_scene.name)
    finally:
        if orig_window and orig_scene:
            orig_window.scene = orig_scene
        bpy.data.scenes.remove(vse_scene)

    return out_mp4.exists() and out_mp4.stat().st_size > 0


def encode_sequence_to_mp4(frames_dir: Path, out_mp4: Path, num_frames: int, fps: int = 30) -> bool:
    """
    Encodes `frame_0000.png`.. into `out_mp4`. Uses system `ffmpeg` if available on PATH,
    otherwise automatically uses Blender's built-in H.264 Video Sequencer encoder.
    """
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        try:
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-framerate", str(fps),
                    "-i", str(frames_dir / "frame_%04d.png"),
                    "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                    "-c:v", "libx264",
                    "-pix_fmt", "yuv420p",
                    "-crf", "16",
                    str(out_mp4),
                ],
                check=True,
                capture_output=True,
            )
            if out_mp4.exists() and out_mp4.stat().st_size > 0:
                return True
        except Exception:
            pass

    return _encode_sequence_via_blender_vse(frames_dir, out_mp4, num_frames=num_frames, fps=fps)


def build_poster_and_wipe_from_three_passes(
    wire_png: str | Path,
    clay_png: str | Path,
    final_png: str | Path,
    out_dir: str | Path,
    num_wipe_frames: int = 120,
    fps: int = 30,
    poly_stats: dict | None = None,
) -> dict[str, str]:
    """
    Loads the 3 rendered PNGs using Blender's built-in `bpy.data.images` + `numpy`,
    saves `portfolio_breakdown_poster.png` and `portfolio_breakdown_poster_hud.png`,
    and encodes `breakdown_wipe_showcase.mp4`. Only returns paths that exist on disk.
    """
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    arr_w = load_image_rgba(wire_png)
    arr_c = load_image_rgba(clay_png)
    arr_f = load_image_rgba(final_png)

    poster_arr = composite_three_way_numpy(arr_w, arr_c, arr_f)
    poster_path = out_dir / "portfolio_breakdown_poster.png"
    save_image_rgba(poster_arr, poster_path)

    hud_poster_path = out_dir / "portfolio_breakdown_poster_hud.png"
    stamp_technical_spec_hud(poster_path, hud_poster_path, poly_stats=poly_stats)

    seq_dir = out_dir / "_tmp_wipe"
    if seq_dir.exists():
        shutil.rmtree(seq_dir)
    seq_dir.mkdir(parents=True, exist_ok=True)

    # For transparent alpha renders, composite onto dark studio background for MP4 video
    arr_w_vid = arr_w.copy()
    arr_c_vid = arr_c.copy()
    arr_f_vid = arr_f.copy()
    for a in (arr_w_vid, arr_c_vid, arr_f_vid):
        alpha = a[..., 3:4]
        bg = np.array([0.04, 0.045, 0.06], dtype=np.float32)
        a[..., :3] = a[..., :3] * alpha + bg * (1.0 - alpha)
        a[..., 3] = 1.0

    for i in range(num_wipe_frames):
        progress = i / max(1, num_wipe_frames - 1)
        frame_arr = composite_moving_wipe_numpy(arr_w_vid, arr_c_vid, arr_f_vid, progress)
        save_image_rgba(frame_arr, seq_dir / f"frame_{i:04d}.png")

    mp4_path = out_dir / "breakdown_wipe_showcase.mp4"
    encode_sequence_to_mp4(seq_dir, mp4_path, num_frames=num_wipe_frames, fps=fps)
    shutil.rmtree(seq_dir, ignore_errors=True)

    result = {}
    if poster_path.exists():
        result["poster_png"] = str(poster_path)
    if hud_poster_path.exists():
        result["poster_hud_png"] = str(hud_poster_path)
    if mp4_path.exists():
        result["wipe_mp4"] = str(mp4_path)
    return result


def assemble_turntable_breakdown_videos(
    out_dir: str | Path,
    num_frames: int,
    fps: int = 30,
) -> dict[str, str]:
    """
    Assembles 3D orbiting turntable pass sequences (`wire_0001..N`, `clay_0001..N`, `final_0001..N`)
    into `01_turntable_moving_wipe.mp4`, `02_turntable_three_way_split.mp4`, and `portfolio_breakdown_poster.png`.
    """
    out_dir = Path(out_dir).resolve()
    raw_dir = out_dir / "raw_passes"
    wipe_seq_dir = out_dir / "_tmp_wipe_seq"
    split_seq_dir = out_dir / "_tmp_split_seq"

    for d in (wipe_seq_dir, split_seq_dir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)

    mid_idx = max(1, num_frames // 2)
    poster_path = out_dir / "portfolio_breakdown_poster.png"

    for i in range(1, num_frames + 1):
        arr_w = load_image_rgba(raw_dir / f"wire_{i:04d}.png")
        arr_c = load_image_rgba(raw_dir / f"clay_{i:04d}.png")
        arr_f = load_image_rgba(raw_dir / f"final_{i:04d}.png")

        progress = (i - 1) / max(1, num_frames - 1)
        wipe_arr = composite_moving_wipe_numpy(arr_w, arr_c, arr_f, progress)
        save_image_rgba(wipe_arr, wipe_seq_dir / f"frame_{i - 1:04d}.png")

        split_arr = composite_three_way_numpy(arr_w, arr_c, arr_f)
        save_image_rgba(split_arr, split_seq_dir / f"frame_{i - 1:04d}.png")

        if i == mid_idx:
            save_image_rgba(split_arr, poster_path)

    mp4_wipe = out_dir / "01_turntable_moving_wipe.mp4"
    mp4_split = out_dir / "02_turntable_three_way_split.mp4"

    encode_sequence_to_mp4(wipe_seq_dir, mp4_wipe, num_frames=num_frames, fps=fps)
    encode_sequence_to_mp4(split_seq_dir, mp4_split, num_frames=num_frames, fps=fps)

    shutil.rmtree(wipe_seq_dir, ignore_errors=True)
    shutil.rmtree(split_seq_dir, ignore_errors=True)

    result = {}
    if mp4_wipe.exists():
        result["moving_wipe_mp4"] = str(mp4_wipe)
    if mp4_split.exists():
        result["three_way_turntable_mp4"] = str(mp4_split)
    if poster_path.exists():
        result["poster_png"] = str(poster_path)
    return result


# ==============================================================================
# DEEP 6-PASS STUDIO VFX DEMOREEL ENGINE (v3.0)
# Passes:
#   01 // SILHOUETTE  (Grazing Fresnel Rim Contour)
#   02 // CLAY        (Sculptor Clay + Ambient Occlusion Cavity)
#   03 // WIREFRAME   (True Quad-Preserving Topology)
#   04 // UV_NORMAL   (World Normal + Curvature + Technical UV Checker)
#   05 // ALBEDO      (Flat Diffuse Albedo / Texture Paint Layer)
#   06 // FINAL       (Full Raytraced PBR Beauty)
# ==============================================================================

PASS_ORDER_6 = ["SILHOUETTE", "CLAY", "WIREFRAME", "UV_NORMAL", "ALBEDO", "FINAL"]

PASS_META_6 = {
    "SILHOUETTE": {"label": "01 // RIM SILHOUETTE", "color": (0.35, 0.85, 1.0, 1.0)},
    "CLAY":       {"label": "02 // SCULPT CLAY + AO", "color": (0.95, 0.72, 0.45, 1.0)},
    "WIREFRAME":  {"label": "03 // QUAD TOPOLOGY", "color": (0.28, 0.92, 0.98, 1.0)},
    "UV_NORMAL":  {"label": "04 // NORMAL + UV GRID", "color": (0.78, 0.52, 1.0, 1.0)},
    "ALBEDO":     {"label": "05 // SHADER ALBEDO", "color": (0.42, 0.95, 0.62, 1.0)},
    "FINAL":      {"label": "06 // FINAL PBR BEAUTY", "color": (1.0, 0.62, 0.18, 1.0)},
}


def _compute_3d_relief_field(
    passes: dict[str, np.ndarray],
    wipe_direction: str = "LEFT_TO_RIGHT",
) -> tuple[np.ndarray, np.ndarray]:
    """
    Computes a normalized 3D surface relief / contour field `relief` and a
    subject foreground mask `fg_mask` in [0..1] from the rendered passes so laser scan
    transitions wrap around the actual 3D geometry along `wipe_direction`:
      - `LEFT_TO_RIGHT`  : Sideways Left -> Right (+X)
      - `RIGHT_TO_LEFT`  : Sideways Right -> Left (-X)
      - `TOP_TO_BOTTOM`  : Vertical Top -> Bottom (+Y down)
      - `BOTTOM_TO_TOP`  : Vertical Bottom -> Top (-Y up)
    """
    arr_norm = passes.get("UV_NORMAL")
    arr_clay = passes.get("CLAY")
    arr_sil = passes.get("SILHOUETTE")
    arr_wire = passes.get("WIREFRAME")

    h, w, _ = arr_clay.shape

    clay_luma = (
        0.299 * arr_clay[..., 0] + 0.587 * arr_clay[..., 1] + 0.114 * arr_clay[..., 2]
    )
    if arr_norm is not None:
        norm_relief = 0.45 * arr_norm[..., 0] + 0.35 * arr_norm[..., 1] + 0.20 * arr_norm[..., 2]
    else:
        norm_relief = clay_luma

    diff_wc = np.max(np.abs(arr_wire[..., :3] - arr_clay[..., :3]), axis=-1)
    if arr_norm is not None and arr_sil is not None:
        diff_ns = np.max(np.abs(arr_norm[..., :3] - arr_sil[..., :3]), axis=-1)
        fg_mask = np.clip((diff_ns - 0.14) * 5.5, 0.0, 1.0)
    else:
        fg_mask = np.clip((diff_wc - 0.08) * 6.0, 0.0, 1.0)

    ys_fg, xs_fg = np.where(fg_mask > 0.25)
    if len(ys_fg) > 50:
        y_min, y_max = float(np.percentile(ys_fg, 1)), float(np.percentile(ys_fg, 99))
        x_min, x_max = float(np.percentile(xs_fg, 1)), float(np.percentile(xs_fg, 99))
    else:
        y_min, y_max = h * 0.1, h * 0.9
        x_min, x_max = w * 0.25, w * 0.75

    y_norm = (np.arange(h, dtype=np.float32)[:, None] - y_min) / max(20.0, y_max - y_min)
    x_norm = (np.arange(w, dtype=np.float32)[None, :] - x_min) / max(20.0, x_max - x_min)

    if wipe_direction == "RIGHT_TO_LEFT":
        base_sweep = 0.76 * (1.0 - x_norm) + 0.24 * y_norm
    elif wipe_direction == "TOP_TO_BOTTOM":
        base_sweep = 0.78 * y_norm + 0.22 * x_norm
    elif wipe_direction == "BOTTOM_TO_TOP":
        base_sweep = 0.78 * (1.0 - y_norm) + 0.22 * x_norm
    else:  # LEFT_TO_RIGHT
        base_sweep = 0.76 * x_norm + 0.24 * y_norm

    relief = base_sweep + 0.18 * (0.55 * norm_relief + 0.45 * clay_luma - 0.5) * fg_mask
    if len(ys_fg) > 50:
        subj_vals = relief[fg_mask > 0.25]
        r_min, r_max = float(np.percentile(subj_vals, 1)), float(np.percentile(subj_vals, 99))
        relief = (relief - r_min) / max(0.05, r_max - r_min)
    return relief, fg_mask


def _stamp_studio_demoreel_slate(
    arr: np.ndarray,
    active_pass_key: str,
    poly_stats: dict | None = None,
    frame_idx: int = 0,
    fps: int = 30,
):
    """
    Stamps a clean VFX Demoreel Corner Slate + Bottom Telemetry Bar onto `arr` in-place.
    """
    h, w, _ = arr.shape
    meta = PASS_META_6.get(active_pass_key, PASS_META_6["FINAL"])
    accent = np.array(meta["color"], dtype=np.float32)

    scale = 2 if w >= 960 else 1
    pad_x = max(20, int(w * 0.022))
    pad_y = max(18, int(h * 0.022))

    # Top-left active pass pill badge
    pill_text = f"PASS  {meta['label']}"
    pill_w = len(pill_text) * 6 * scale + 28 * scale
    pill_h = 7 * scale + 14 * scale
    y0, y1 = pad_y, min(h, pad_y + pill_h)
    x0, x1 = pad_x, min(w, pad_x + pill_w)

    bg_dark = np.array([0.035, 0.042, 0.058], dtype=np.float32)
    arr[y0:y1, x0:x1, :3] = arr[y0:y1, x0:x1, :3] * 0.18 + bg_dark * 0.82
    arr[y0:y1, x0 : min(w, x0 + 3 * scale), :] = accent
    _draw_text_5x7(
        arr,
        pill_text,
        x0 + 12 * scale,
        y0 + 7 * scale,
        scale,
        (0.97, 0.98, 1.0, 1.0),
    )

    # Bottom studio telemetry bar
    bar_h = max(36, int(h * 0.042))
    by0 = h - bar_h
    arr[by0:h, :, :3] = arr[by0:h, :, :3] * 0.16 + bg_dark * 0.84
    arr[by0 : min(h, by0 + 2), :, :] = accent

    ty = by0 + max(5, (bar_h - 7 * scale) // 2)
    verts = f"{poly_stats.get('vertices', 0):,}" if poly_stats else "0"
    tris = f"{poly_stats.get('triangles', 0):,}" if poly_stats else "0"
    left_str = f"BREAKDOWN STUDIO PRO  |  VERTS: {verts}  TRIS: {tris}"
    _draw_text_5x7(arr, left_str, pad_x, ty, scale, (0.86, 0.90, 0.96, 1.0))

    sec = frame_idx // max(1, fps)
    rem = frame_idx % max(1, fps)
    tc_str = f"TC 00:00:{sec:02d}:{rem:02d}"
    tc_w = len(tc_str) * 6 * scale
    if w - tc_w - pad_x > len(left_str) * 6 * scale + 36:
        _draw_text_5x7(arr, tc_str, w - tc_w - pad_x, ty, scale, tuple(meta["color"]))


def _crop_zoom_pan_numpy(
    arr: np.ndarray,
    zoom: float = 1.0,
    center_x: float = 0.5,
    center_y: float = 0.5,
) -> np.ndarray:
    """
    Fast vectorized bilinear zoom & pan crop on an (H, W, 4) float32 image array
    for cinematic camera push-ins and hero detail close-up cuts.
    """
    if abs(zoom - 1.0) < 1e-4 and abs(center_x - 0.5) < 1e-4 and abs(center_y - 0.5) < 1e-4:
        return arr

    h, w, _ = arr.shape
    span_w = w / max(1.0, float(zoom))
    span_h = h / max(1.0, float(zoom))

    cx = np.clip(center_x * w, span_w * 0.5, w - span_w * 0.5)
    cy = np.clip(center_y * h, span_h * 0.5, h - span_h * 0.5)

    x0 = cx - span_w * 0.5
    y0 = cy - span_h * 0.5

    xs = np.clip(np.linspace(x0, x0 + span_w - 1.0, w, dtype=np.float32), 0.0, w - 1.001)
    ys = np.clip(np.linspace(y0, y0 + span_h - 1.0, h, dtype=np.float32), 0.0, h - 1.001)

    xi = xs.astype(np.int32)
    yi = ys.astype(np.int32)
    xf = (xs - xi)[None, :, None]
    yf = (ys - yi)[:, None, None]

    top = arr[yi[:, None], xi[None, :]] * (1.0 - xf) + arr[yi[:, None], xi[None, :] + 1] * xf
    bot = arr[yi[:, None] + 1, xi[None, :]] * (1.0 - xf) + arr[yi[:, None] + 1, xi[None, :] + 1] * xf
    return top * (1.0 - yf) + bot * yf


def _wipe_two_passes_3d_contour_numpy(
    arr_from: np.ndarray,
    arr_to: np.ndarray,
    wipe_t: float,
    relief: np.ndarray | None = None,
    fg_mask: np.ndarray | None = None,
    accent_color: tuple = (0.28, 0.92, 0.98, 1.0),
    arr_wire: np.ndarray | None = None,
) -> np.ndarray:
    """
    Sweeps from `arr_from` to `arr_to` across the 3D surface contour (`relief`) of the model
    with a crisp 3D surface-hugging laser wavefront that illuminates the 3D subject's geometry
    (and cleanly sweeps the studio floor without ugly 2D boxes).
    """
    if wipe_t <= 0.001:
        return arr_from.copy()
    if wipe_t >= 0.999:
        return arr_to.copy()

    h, w, _ = arr_from.shape
    if relief is None:
        xx = np.linspace(0.0, 1.0, w, dtype=np.float32)[None, :]
        yy = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]
        field = 0.78 * xx + 0.22 * yy
    else:
        field = relief

    thresh = -0.38 + smoothstep(wipe_t) * 1.76
    dist = thresh - field

    w_reveal = np.clip(dist / 0.018 + 0.5, 0.0, 1.0)[..., None]
    out = arr_from * (1.0 - w_reveal) + arr_to * w_reveal

    if arr_wire is not None:
        fringe = np.exp(-((dist / 0.032) ** 2))[..., None]
        if fg_mask is not None:
            fringe = fringe * np.clip(fg_mask[..., None] + 0.25, 0.0, 1.0)
        out = out * (1.0 - fringe * 0.55) + arr_wire * (fringe * 0.55)

    col = np.array(accent_color, dtype=np.float32)
    line_core = np.exp(-((dist / 0.0045) ** 2))[..., None]
    line_glow = np.exp(-((dist / 0.020) ** 2))[..., None]
    if fg_mask is not None:
        # Boost laser intensity on the 3D model surface, subtle on floor
        mod_mask = 0.22 + 0.78 * fg_mask[..., None]
        line_core = line_core * mod_mask
        line_glow = line_glow * mod_mask

    out = out * (1.0 - line_core * 0.92) + col * (line_core * 0.92 + line_glow * 0.30)
    return np.clip(out, 0.0, 1.0)


def composite_six_way_studio_poster_numpy(
    passes: dict[str, np.ndarray],
    poly_stats: dict | None = None,
) -> np.ndarray:
    """
    Creates a 3x2 (6-Up) Widescreen Studio LookDev Sheet showing all 6 passes intact and whole
    (never chopping the model into 6 stripes):
      Top Row:    01 // RIM SILHOUETTE   |  02 // SCULPT CLAY + AO  |  03 // QUAD TOPOLOGY
      Bottom Row: 04 // NORMAL + UV GRID |  05 // SHADER ALBEDO     |  06 // FINAL PBR BEAUTY
    Each cell preserves exact 1:1 pixel aspect ratio by center-cropping around the 3D subject.
    """
    arr_final = passes["FINAL"]
    h, w, _ = arr_final.shape
    bar_h = max(36, int(h * 0.042))
    usable_h = h - bar_h
    cell_h = usable_h // 2
    cell_w = w // 3

    _, fg_mask = _compute_3d_relief_field(passes)
    ys_fg, xs_fg = np.where(fg_mask > 0.25)
    if len(xs_fg) > 50:
        subj_cx = int(0.5 * (np.percentile(xs_fg, 2) + np.percentile(xs_fg, 98)))
    else:
        subj_cx = w // 2

    out = np.zeros_like(arr_final)
    out[..., :3] = np.array([0.025, 0.03, 0.042], dtype=np.float32)
    out[..., 3] = 1.0

    # To downscale 2x without aspect distortion, extract (cell_h * 2, cell_w * 2) centered on subject
    crop_h = min(h - (h % 2), cell_h * 2)
    crop_w = min(w - (w % 2), cell_w * 2)
    y_start = max(0, (h - crop_h) // 2)
    x_start = int(np.clip(subj_cx - crop_w // 2, 0, w - crop_w))
    x_start = x_start - (x_start % 2)

    for idx, key in enumerate(PASS_ORDER_6):
        r_idx = idx // 3
        c_idx = idx % 3
        y0 = r_idx * cell_h
        x0 = c_idx * cell_w

        src = passes.get(key, arr_final)
        sub = src[y_start : y_start + crop_h, x_start : x_start + crop_w, :]
        small = 0.25 * (
            sub[0::2, 0::2, :]
            + sub[1::2, 0::2, :]
            + sub[0::2, 1::2, :]
            + sub[1::2, 1::2, :]
        )
        sh, sw, _ = small.shape
        sh_fit = min(cell_h, sh)
        sw_fit = min(cell_w, sw)
        out[y0 : y0 + sh_fit, x0 : x0 + sw_fit, :] = small[:sh_fit, :sw_fit, :]

        # Stamp pass label pill in top-left of each cell
        meta = PASS_META_6[key]
        scale = 2 if w >= 1600 else 1
        lx = x0 + 12 * scale
        ly = y0 + 10 * scale
        lw_px = len(meta["label"]) * 6 * scale + 14 * scale
        lh_px = 7 * scale + 10 * scale
        bg = np.array([0.03, 0.035, 0.05], dtype=np.float32)
        out[ly : ly + lh_px, lx : lx + lw_px, :3] = (
            out[ly : ly + lh_px, lx : lx + lw_px, :3] * 0.18 + bg * 0.82
        )
        accent = np.array(meta["color"], dtype=np.float32)
        out[ly : ly + lh_px, lx : lx + 2 * scale, :] = accent
        _draw_text_5x7(
            out,
            meta["label"],
            lx + 7 * scale,
            ly + 5 * scale,
            scale,
            (0.96, 0.98, 1.0, 1.0),
        )

    # Crisp grid dividers
    div_col = np.array([0.18, 0.22, 0.30, 1.0], dtype=np.float32)
    out[max(0, cell_h - 1) : min(usable_h, cell_h + 2), :, :] = div_col
    for c_div in (cell_w, cell_w * 2):
        out[:usable_h, max(0, c_div - 1) : min(w, c_div + 2), :] = div_col

    # Bottom telemetry bar
    by0 = h - bar_h
    bg_dark = np.array([0.035, 0.042, 0.058], dtype=np.float32)
    out[by0:h, :, :3] = bg_dark
    out[by0 : min(h, by0 + 2), :, :] = np.array(PASS_META_6["FINAL"]["color"], dtype=np.float32)
    scale = 2 if w >= 960 else 1
    pad_x = max(20, int(w * 0.022))
    ty = by0 + max(5, (bar_h - 7 * scale) // 2)
    verts = f"{poly_stats.get('vertices', 0):,}" if poly_stats else "0"
    tris = f"{poly_stats.get('triangles', 0):,}" if poly_stats else "0"
    left_str = f"BREAKDOWN STUDIO PRO  |  6-PASS LOOKDEV SHEET  |  VERTS: {verts}  TRIS: {tris}"
    _draw_text_5x7(out, left_str, pad_x, ty, scale, (0.88, 0.92, 0.98, 1.0))

    return np.clip(out, 0.0, 1.0)


def composite_4up_studio_grid_numpy(
    passes: dict[str, np.ndarray],
    poly_stats: dict | None = None,
) -> np.ndarray:
    """
    Creates a 2x2 VFX Studio LookDev Quad Grid:
      Top-Left:  02 // SCULPT CLAY + AO      Top-Right:    03 // QUAD TOPOLOGY
      Bot-Left:  04 // NORMAL + UV GRID      Bottom-Right: 06 // FINAL PBR BEAUTY
    """
    arr_final = passes["FINAL"]
    h, w, _ = arr_final.shape
    hh, hw = h // 2, w // 2

    grid_keys = [
        ("CLAY", 0, 0),
        ("WIREFRAME", 0, hw),
        ("UV_NORMAL", hh, 0),
        ("FINAL", hh, hw),
    ]
    out = np.zeros_like(arr_final)
    out[..., 3] = 1.0

    for key, y0, x0 in grid_keys:
        src = passes.get(key, arr_final)
        h_even = hh * 2
        w_even = hw * 2
        cropped = src[:h_even, :w_even, :]
        small = 0.25 * (
            cropped[0::2, 0::2, :]
            + cropped[1::2, 0::2, :]
            + cropped[0::2, 1::2, :]
            + cropped[1::2, 1::2, :]
        )
        sh, sw, _ = small.shape
        out[y0 : y0 + sh, x0 : x0 + sw, :] = small

        meta = PASS_META_6[key]
        scale = 2 if w >= 1280 else 1
        lx = x0 + 16 * scale
        ly = y0 + 14 * scale
        lw_px = len(meta["label"]) * 6 * scale + 16 * scale
        lh_px = 7 * scale + 10 * scale
        bg = np.array([0.03, 0.035, 0.05], dtype=np.float32)
        out[ly : ly + lh_px, lx : lx + lw_px, :3] = (
            out[ly : ly + lh_px, lx : lx + lw_px, :3] * 0.2 + bg * 0.8
        )
        _draw_text_5x7(
            out,
            meta["label"],
            lx + 8 * scale,
            ly + 5 * scale,
            scale,
            meta["color"],
        )

    div_col = np.array([0.22, 0.26, 0.34, 1.0], dtype=np.float32)
    out[max(0, hh - 2) : min(h, hh + 2), :, :] = div_col
    out[:, max(0, hw - 2) : min(w, hw + 2), :] = div_col
    return np.clip(out, 0.0, 1.0)


def composite_3d_depth_scan_frame_numpy(
    passes: dict[str, np.ndarray],
    progress: float,
    relief: np.ndarray | None = None,
    fg_mask: np.ndarray | None = None,
    grid_4up: np.ndarray | None = None,
    poly_stats: dict | None = None,
    frame_idx: int = 0,
    fps: int = 30,
    apply_camera_drift: bool = False,
    wipe_direction: str = "LEFT_TO_RIGHT",
) -> np.ndarray:
    """
    Renders a single frame of the 100% Full-Screen 3D Breakdown Maker Showreel:
    - Zero PowerPoint grid slides tacked onto the end
    - Zero blurry 2D pixel crop cuts
    - Zero bitmap text bars covering the 3D render
    - Continuous 3D Assembly & 360° Turntable flowing seamlessly across:
      FINAL (3D Part Assembly Build) -> CLAY (Sculpt + AO) -> WIREFRAME (Quad Topology)
      -> UV_NORMAL (Surface Normals + UV) -> FINAL (Full Assembled PBR Hero Turntable)
      using 3D surface-contour laser waves (`relief`, `fg_mask`) along `wipe_direction`.
    """
    desired_order = ["FINAL", "CLAY", "WIREFRAME", "UV_NORMAL", "FINAL"]
    reel_seq = [k for k in desired_order if k in passes]
    if len(reel_seq) < 2:
        reel_seq = list(passes.keys())

    if relief is None or fg_mask is None:
        try:
            relief, fg_mask = _compute_3d_relief_field(passes, wipe_direction=wipe_direction)
        except Exception:
            relief, fg_mask = None, None

    # Hold the first 22% in pure FINAL PBR while initial 3D foundation parts drop & bounce in,
    # and hold the final 16% in pure FINAL PBR as the completed turntable finishes.
    if progress <= 0.22:
        out = passes[reel_seq[0]].copy()
        if apply_camera_drift:
            out = _crop_zoom_pan_numpy(out, 1.0 + 0.08 * progress, 0.50, 0.49)
        return out

    if progress >= 0.86:
        out = passes[reel_seq[-1]].copy()
        if apply_camera_drift:
            out = _crop_zoom_pan_numpy(out, 1.0 + 0.08 * progress, 0.50, 0.49)
        return out

    mid_p = (progress - 0.22) / (0.86 - 0.22)
    n_trans = len(reel_seq) - 1
    seg_f = mid_p * n_trans
    seg_idx = min(n_trans - 1, int(seg_f))
    sub_p = seg_f - seg_idx

    k_from = reel_seq[seg_idx]
    k_to = reel_seq[seg_idx + 1]

    # Hold intact pass for 18% of segment, sweep 3D surface contour wave for 64%, hold 18%
    if sub_p < 0.16:
        wipe_t = 0.0
    elif sub_p > 0.84:
        wipe_t = 1.0
    else:
        wipe_t = (sub_p - 0.16) / 0.68

    arr_from = passes[k_from]
    arr_to = passes[k_to]
    arr_wire = passes.get("WIREFRAME")

    if apply_camera_drift:
        z_cur = 1.0 + 0.08 * progress
        arr_from = _crop_zoom_pan_numpy(arr_from, z_cur, 0.50, 0.49)
        arr_to = _crop_zoom_pan_numpy(arr_to, z_cur, 0.50, 0.49)
        if arr_wire is not None:
            arr_wire = _crop_zoom_pan_numpy(arr_wire, z_cur, 0.50, 0.49)

    out = _wipe_two_passes_3d_contour_numpy(
        arr_from,
        arr_to,
        wipe_t,
        relief=relief,
        fg_mask=fg_mask,
        accent_color=PASS_META_6.get(k_to, PASS_META_6["FINAL"])["color"],
        arr_wire=arr_wire if k_to != "WIREFRAME" and k_from != "WIREFRAME" else None,
    )
    return out


def build_deep_6pass_demoreel_outputs(
    pass_paths: dict[str, str | Path],
    out_dir: str | Path,
    num_frames: int = 180,
    fps: int = 30,
    poly_stats: dict | None = None,
) -> dict[str, str]:
    """
    Loads all 6 rendered VFX passes (`SILHOUETTE`, `CLAY`, `WIREFRAME`, `UV_NORMAL`, `ALBEDO`, `FINAL`),
    generates:
      1. `01_6pass_studio_breakdown_poster.png` (3x2 6-Pass Intact Studio LookDev Sheet with Telemetry)
      2. `02_4up_lookdev_grid_poster.png`       (2x2 Studio LookDev Quad Grid Poster)
      3. `03_3way_classic_poster.png`           (Classic 3-Way Wire/Clay/Final Poster)
      4. `04_vfx_demoreel_3d_laser_scan.mp4`    (3-Act Sequential Full-Pass + Close-Up + Grid Showreel MP4)
    """
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    passes = {}
    bg = np.array([0.035, 0.04, 0.055], dtype=np.float32)
    for k, p in pass_paths.items():
        arr = load_image_rgba(p)
        alpha = arr[..., 3:4]
        arr[..., :3] = arr[..., :3] * alpha + bg * (1.0 - alpha)
        arr[..., 3] = 1.0
        passes[k] = arr

    six_poster = composite_six_way_studio_poster_numpy(passes, poly_stats=poly_stats)
    grid_poster = composite_4up_studio_grid_numpy(passes, poly_stats=poly_stats)
    three_poster = composite_three_way_numpy(passes["WIREFRAME"], passes["CLAY"], passes["FINAL"])

    p_six = out_dir / "01_6pass_studio_breakdown_poster.png"
    p_grid = out_dir / "02_4up_lookdev_grid_poster.png"
    p_three = out_dir / "03_3way_classic_poster.png"

    save_image_rgba(six_poster, p_six)
    save_image_rgba(grid_poster, p_grid)
    save_image_rgba(three_poster, p_three)

    seq_dir = out_dir / "_tmp_6pass_reel"
    if seq_dir.exists():
        shutil.rmtree(seq_dir)
    seq_dir.mkdir(parents=True, exist_ok=True)

    for i in range(num_frames):
        prog = i / max(1, num_frames - 1)
        frame_arr = composite_3d_depth_scan_frame_numpy(
            passes,
            prog,
            grid_4up=grid_poster,
            poly_stats=poly_stats,
            frame_idx=i,
            fps=fps,
            apply_camera_drift=True,
        )
        save_image_rgba(frame_arr, seq_dir / f"frame_{i:04d}.png")

    mp4_path = out_dir / "04_vfx_demoreel_3d_laser_scan.mp4"
    encode_sequence_to_mp4(seq_dir, mp4_path, num_frames=num_frames, fps=fps)
    shutil.rmtree(seq_dir, ignore_errors=True)

    result = {}
    if p_six.exists():
        result["six_pass_poster"] = str(p_six)
    if p_grid.exists():
        result["grid_4up_poster"] = str(p_grid)
    if p_three.exists():
        result["three_pass_poster"] = str(p_three)
    if mp4_path.exists():
        result["demoreel_mp4"] = str(mp4_path)
    return result


def assemble_deep_moving_demoreel_videos(
    out_dir: str | Path,
    num_frames: int,
    fps: int = 30,
    poly_stats: dict | None = None,
    wipe_direction: str = "LEFT_TO_RIGHT",
) -> dict[str, str]:
    """
    Assembles a live moving animation or 360° turntable where passes were rendered per frame
    into:
      1. `01_moving_3d_laser_scan_reel.mp4` (Full-Screen 3D Breakdown Maker + 3D Contour Pass Wave Reel)
      2. `02_moving_4up_studio_grid_reel.mp4` (Live 2x2 Quad Grid playing all 4 passes in 3D sync)
      3. `03_6pass_studio_breakdown_poster.png` (Hero Mid-Frame 3x2 6-Pass Intact Studio Sheet)
    """
    out_dir = Path(out_dir).resolve()
    raw_dir = out_dir / "raw_passes"
    scan_seq_dir = out_dir / "_tmp_moving_scan"
    grid_seq_dir = out_dir / "_tmp_moving_grid"

    for d in (scan_seq_dir, grid_seq_dir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)

    mid_idx = max(1, num_frames // 2)
    poster_path = out_dir / "03_6pass_studio_breakdown_poster.png"
    bg = np.array([0.035, 0.04, 0.055], dtype=np.float32)

    prefix_map = {
        "SILHOUETTE": "sil",
        "CLAY": "clay",
        "WIREFRAME": "wire",
        "UV_NORMAL": "norm",
        "ALBEDO": "alb",
        "FINAL": "final",
    }

    for i in range(1, num_frames + 1):
        passes = {}
        for k, pref in prefix_map.items():
            f_p = raw_dir / f"{pref}_{i:04d}.png"
            if f_p.exists():
                arr = load_image_rgba(f_p)
                alpha = arr[..., 3:4]
                arr[..., :3] = arr[..., :3] * alpha + bg * (1.0 - alpha)
                arr[..., 3] = 1.0
                passes[k] = arr

        grid_arr = composite_4up_studio_grid_numpy(passes, poly_stats=poly_stats)
        save_image_rgba(grid_arr, grid_seq_dir / f"frame_{i - 1:04d}.png")

        prog = (i - 1) / max(1, num_frames - 1)
        scan_arr = composite_3d_depth_scan_frame_numpy(
            passes,
            prog,
            grid_4up=grid_arr,
            poly_stats=poly_stats,
            frame_idx=i - 1,
            fps=fps,
            apply_camera_drift=False,
            wipe_direction=wipe_direction,
        )
        save_image_rgba(scan_arr, scan_seq_dir / f"frame_{i - 1:04d}.png")

        if i == mid_idx:
            six_arr = composite_six_way_studio_poster_numpy(passes, poly_stats=poly_stats)
            save_image_rgba(six_arr, poster_path)

    mp4_scan = out_dir / "01_moving_3d_laser_scan_reel.mp4"
    mp4_grid = out_dir / "02_moving_4up_studio_grid_reel.mp4"

    encode_sequence_to_mp4(scan_seq_dir, mp4_scan, num_frames=num_frames, fps=fps)
    encode_sequence_to_mp4(grid_seq_dir, mp4_grid, num_frames=num_frames, fps=fps)

    shutil.rmtree(scan_seq_dir, ignore_errors=True)
    shutil.rmtree(grid_seq_dir, ignore_errors=True)

    result = {}
    if mp4_scan.exists():
        result["moving_scan_mp4"] = str(mp4_scan)
    if mp4_grid.exists():
        result["moving_grid_mp4"] = str(mp4_grid)
    if poster_path.exists():
        result["poster_png"] = str(poster_path)
    return result
