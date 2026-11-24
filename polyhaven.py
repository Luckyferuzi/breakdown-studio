"""
BreakdownStudio 3D — Built-in Poly Haven (CC0) Model & Studio HDRI Loader
Fetches CC0 photogrammetry/PBR 3D models and 2K Studio HDRIs from Poly Haven
(https://polyhaven.com) with cross-platform local disk caching and offline fallback.
"""

import json
import tempfile
import time
import urllib.request
from pathlib import Path

try:
    import bpy
except ImportError:
    bpy = None


HEADERS = {"User-Agent": "BreakdownStudio/2.0 (https://tumexstudio.gumroad.com; tumexstudio@gmail.com)"}

CURATED_MODELS = [
    ("Camera_01", "Leica Vintage Rangefinder Camera (Poly Haven CC0)", "27k polys • Leather, Brass & Optical Glass"),
    ("marble_bust_01", "Classical Museum Marble Bust (Poly Haven CC0)", "High-detail museum photogrammetry sculpture"),
    ("Television_01", "Retro Vintage CRT Television (Poly Haven CC0)", "Wood veneer, glass tube & metallic dials"),
    ("alarm_clock_01", "Vintage Twin-Bell Alarm Clock (Poly Haven CC0)", "Painted metal, brass bells & glass face"),
    ("brass_vase_01", "Handcrafted Ornate Brass Vase (Poly Haven CC0)", "Anisotropic metallic reflections & patina"),
]

CURATED_HDRIS = [
    ("studio_small_09", "Soft Commercial Studio 09 (Poly Haven CC0)", "Clean balanced softboxes & rim highlights"),
    ("photo_studio_loft_hall", "Photo Studio Loft Hall (Poly Haven CC0)", "Warm natural window bounce & studio fill"),
    ("studio_small_08", "High-Contrast Dark Studio 08 (Poly Haven CC0)", "Dramatic specular highlights for metal & glass"),
]


def get_cache_dir() -> Path:
    """Returns a portable, cross-platform user cache directory (Windows, macOS, Linux)."""
    if bpy is not None:
        try:
            user_dir = bpy.utils.user_resource("DATAFILES", path="breakdown_studio_cache", create=True)
            if user_dir:
                p = Path(user_dir)
                p.mkdir(parents=True, exist_ok=True)
                return p
        except Exception:
            pass
    fallback = Path.home() / ".breakdown_studio_cache"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
    except Exception:
        tmp = Path(tempfile.gettempdir()) / "breakdown_studio_cache"
        tmp.mkdir(parents=True, exist_ok=True)
        return tmp


def _fetch_json(url: str) -> dict:
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1.0 * (attempt + 1))
    return {}


def _download_file(url: str, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            tmp.write_bytes(data)
            tmp.replace(dest)
            return
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))


def ensure_model_downloaded(asset_id: str = "Camera_01", res: str = "2k") -> Path | None:
    cache_dir = get_cache_dir() / "models" / asset_id
    blend_path = cache_dir / f"{asset_id}_{res}.blend"
    done_marker = cache_dir / f".{asset_id}_{res}.complete"
    if blend_path.exists() and done_marker.exists():
        return blend_path

    try:
        data = _fetch_json(f"https://api.polyhaven.com/files/{asset_id}")
        blend_info = data["blend"][res]["blend"]
        _download_file(blend_info["url"], blend_path)

        for rel_path, meta in blend_info.get("include", {}).items():
            _download_file(meta["url"], cache_dir / rel_path)

        done_marker.write_text("ok", encoding="utf-8")
        return blend_path
    except Exception:
        return None


def ensure_hdri_downloaded(hdri_id: str = "studio_small_09", res: str = "2k") -> Path | None:
    """
    Returns the cached/downloaded HDRI path, or None if offline (so core.py can
    gracefully fall back to procedural studio lighting without crashing).
    """
    hdr_path = get_cache_dir() / "hdris" / f"{hdri_id}_{res}.hdr"
    if hdr_path.exists() and hdr_path.stat().st_size > 0:
        return hdr_path

    try:
        data = _fetch_json(f"https://api.polyhaven.com/files/{hdri_id}")
        hdr_info = data["hdri"][res]["hdr"]
        _download_file(hdr_info["url"], hdr_path)
        return hdr_path
    except Exception:
        return None


def load_model_into_scene(asset_id: str = "Camera_01", res: str = "2k", clear_existing: bool = True) -> bool:
    """
    Downloads the Poly Haven model (if not already cached) and appends its objects
    into the active Blender scene. Only clears existing meshes AFTER confirming download.
    """
    blend_path = ensure_model_downloaded(asset_id, res)
    if not blend_path or not blend_path.exists():
        return False

    with bpy.data.libraries.load(str(blend_path), link=False) as (data_from, data_to):
        data_to.objects = list(data_from.objects)

    loaded_objs = [o for o in data_to.objects if o is not None and o.type in ("MESH", "ARMATURE", "EMPTY")]
    if not loaded_objs:
        return False

    if clear_existing:
        for obj in list(bpy.context.scene.objects):
            if obj.type == "MESH" and not obj.name.startswith("BS_Studio_"):
                bpy.data.objects.remove(obj, do_unlink=True)

    for obj in loaded_objs:
        if obj.name not in bpy.context.scene.collection.objects:
            bpy.context.scene.collection.objects.link(obj)
    return True
