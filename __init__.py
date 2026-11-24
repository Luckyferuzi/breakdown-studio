"""
BreakdownStudio 3D v2.0 Pro — True 1-Click Automated Wireframe -> Clay -> Final Breakdown Add-on (Hardware-Locked Edition)
"""

bl_info = {
    "name": "BreakdownStudio 3D Pro (1-Click Breakdown)",
    "author": "Tumex Studio",
    "version": (2, 0, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar (N-Panel) > BreakdownStudio",
    "description": "True 1-Click Wireframe / Clay / Final 3-Pass Breakdown Poster & Turntable Video Generator",
    "category": "Render",
}

import importlib
import os
import subprocess
import sys
from pathlib import Path
import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

_SUBMODULES = ("core", "polyhaven", "compositor", "license_guard")
for _mod_name in _SUBMODULES:
    _full = f"{__package__}.{_mod_name}"
    if _full in sys.modules:
        try:
            importlib.reload(sys.modules[_full])
        except Exception:
            pass

from . import core, polyhaven
from .license_guard import (
    STORE_BUY_URL,
    activate_license_on_this_machine,
    deactivate_license_on_this_machine,
    get_machine_hwid,
    is_machine_licensed,
)

_ORIGINAL_MATS_CACHE: dict[str, dict] = {}


def _ensure_mats_cached(meshes):
    global _ORIGINAL_MATS_CACHE
    snap = core.snapshot_original_materials(meshes)
    for obj in meshes:
        if obj.name not in _ORIGINAL_MATS_CACHE:
            _ORIGINAL_MATS_CACHE[obj.name] = snap[obj.name]
        else:
            cur_mats = [slot.material for slot in obj.material_slots]
            if all(m is None or not m.name.startswith("BS_Pass_") for m in cur_mats):
                _ORIGINAL_MATS_CACHE[obj.name]["mats"] = cur_mats
                _ORIGINAL_MATS_CACHE[obj.name]["slot_count"] = len(cur_mats)
    return _ORIGINAL_MATS_CACHE


def configure_render_engine(engine_choice: str = "CYCLES", samples: int = 32):
    scene = bpy.context.scene
    scene.render.use_persistent_data = True
    if engine_choice == "EEVEE":
        try:
            scene.render.engine = "BLENDER_EEVEE_NEXT"
        except Exception:
            scene.render.engine = "BLENDER_EEVEE"
        if hasattr(scene, "eevee") and hasattr(scene.eevee, "taa_render_samples"):
            scene.eevee.taa_render_samples = samples
    else:
        scene.render.engine = "CYCLES"
        scene.cycles.samples = samples
        scene.cycles.use_adaptive_sampling = True
        scene.cycles.adaptive_threshold = 0.04
        scene.cycles.adaptive_min_samples = min(8, samples)
        scene.cycles.max_bounces = 4
        scene.cycles.diffuse_bounces = 1
        scene.cycles.glossy_bounces = 2
        scene.cycles.transmission_bounces = 2
        scene.cycles.transparent_max_bounces = 4
        scene.cycles.volume_bounces = 0
        scene.cycles.caustics_reflective = False
        scene.cycles.caustics_refractive = False
        scene.cycles.use_denoising = True
        try:
            prefs = bpy.context.preferences.addons["cycles"].preferences
            for dev_type in ("OPTIX", "METAL", "HIP", "ONEAPI", "CUDA"):
                prefs.compute_device_type = dev_type
                prefs.get_devices()
                matching = [d for d in prefs.devices if d.type == dev_type]
                if matching:
                    for d in prefs.devices:
                        d.use = (d.type == dev_type or d.type == "CPU")
                    scene.cycles.device = "GPU"
                    if hasattr(scene.cycles, "denoiser"):
                        try:
                            scene.cycles.denoiser = "OPTIX" if dev_type == "OPTIX" else "OPENIMAGEDENOISE"
                        except Exception:
                            pass
                    break
        except Exception:
            pass


def _resolve_portable_output_dir(raw_dir: str, subfolder: str = "BreakdownStudio_Renders") -> Path:
    if raw_dir and raw_dir.strip():
        resolved_str = bpy.path.abspath(raw_dir)
        if resolved_str and "Program Files" not in resolved_str:
            try:
                p = Path(resolved_str).resolve()
                p.mkdir(parents=True, exist_ok=True)
                return p
            except Exception:
                pass
    if bpy.data.filepath:
        try:
            p = Path(bpy.data.filepath).parent / subfolder
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            pass
    fallback = Path.home() / "Pictures" / subfolder
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _open_folder_cross_platform(folder: Path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(folder))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except Exception:
        pass


def _show_image_in_blender_viewer(context, img_path: Path):
    """Automatically load the finished 3-Way Breakdown Poster into Blender's Render/Image Window."""
    if not img_path or not img_path.exists():
        return
    try:
        loaded = bpy.data.images.load(str(img_path), check_existing=False)
        loaded.name = f"BREAKDOWN_3PASS_{img_path.name}"
        try:
            bpy.ops.render.view_show('INVOKE_DEFAULT')
        except Exception:
            pass
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'IMAGE_EDITOR':
                    for space in area.spaces:
                        if space.type == 'IMAGE_EDITOR':
                            space.image = loaded
    except Exception:
        pass


def _on_interactive_build_update(self, context):
    """
    Live interactive callback: when the user clicks a Target, Direction, or Speed
    button in the panel, automatically syncs duration/frames and updates the viewport!
    """
    if self.speed_preset == "FAST_4S":
        self.reel_duration_sec = 4.0
        self.num_frames = 90
    elif self.speed_preset == "NORMAL_6S":
        self.reel_duration_sec = 6.0
        self.num_frames = 140
    elif self.speed_preset == "SLOW_10S":
        self.reel_duration_sec = 10.0
        self.num_frames = 210

    if self.build_direction in ("SIDE_LEFT_RIGHT", "SIDE_RIGHT_LEFT"):
        self.wipe_direction = "LEFT_TO_RIGHT"
    elif self.build_direction == "TOP_DOWN":
        self.wipe_direction = "TOP_TO_BOTTOM"
    elif self.build_direction == "BOTTOM_UP":
        self.wipe_direction = "BOTTOM_TO_TOP"
    else:
        self.wipe_direction = "LEFT_TO_RIGHT"

    # If the user is actively previewing a 3D breakdown in the viewport, update it live on the spot!
    try:
        if any("_bs_live_active" in o for o in bpy.context.scene.objects if o.type == "MESH"):
            bpy.ops.breakdown_studio.animate_assembly()
    except Exception:
        pass


class BS_PG_Settings(bpy.types.PropertyGroup):
    license_key_input: StringProperty(
        name="License Key",
        description="Paste your Gumroad or Superhive License Key to unlock BreakdownStudio on this PC",
        default="",
    )
    license_status_msg: StringProperty(default="")
    show_customize: BoolProperty(
        name="Customize Style & Options",
        description="Show optional style, aspect ratio, and CC0 model import controls",
        default=False,
    )

    polyhaven_model: EnumProperty(
        name="Poly Haven CC0 Model",
        items=polyhaven.CURATED_MODELS,
        default="Camera_01",
    )

    polyhaven_hdri: EnumProperty(
        name="Studio HDRI",
        items=polyhaven.CURATED_HDRIS,
        default="studio_small_09",
    )

    stage_theme: EnumProperty(
        name="Studio Stage Theme",
        items=[
            ("DARK_OBSIDIAN", "Dark Obsidian Studio", "Sleek dark architectural cyclorama"),
            ("NEUTRAL_GRAY", "Museum Neutral Gray", "Neutral lookdev studio backdrop"),
            ("WARM_CREAM", "Warm Editorial Cream", "Soft warm studio backdrop"),
            ("TRANSPARENT_ALPHA", "Transparent Alpha (No Floor)", "Film transparent PNG background"),
        ],
        default="DARK_OBSIDIAN",
    )

    target_scope: EnumProperty(
        name="Breakdown Target",
        items=[
            ("ALL_SCENE", "All Models", "Break down all visible 3D models in the scene"),
            ("SELECTED_ONLY", "Selected Only", "Break down only the currently selected model(s)"),
        ],
        default="ALL_SCENE",
        update=_on_interactive_build_update,
    )

    auto_split_subparts: BoolProperty(
        name="Auto-Split Joined Model into Sub-Parts",
        description="Optional: separate multi-material meshes before breakdown",
        default=False,
    )

    anim_preset: EnumProperty(
        name="Motion",
        items=[
            ("BOTTOM_UP", "Smooth Silk Glide", "Pure 2-keyframe ease-out studio glide (zero wobble)"),
            ("RADIAL", "360° Radial Glide", "Smooth inward 360-degree radial assembly"),
            ("TURNTABLE_ONLY", "360° Turntable Only", "Keep model unified with pure orbital turntable"),
        ],
        default="BOTTOM_UP",
        update=_on_interactive_build_update,
    )

    build_direction: EnumProperty(
        name="Direction",
        items=[
            ("BOTTOM_UP", "Bottom-Up", "Smoothly glide parts into place from bottom to top"),
            ("TOP_DOWN", "Top-Down", "Smoothly glide parts into place from top to bottom"),
            ("SIDE_LEFT_RIGHT", "Sideways", "Smoothly glide parts into place sideways"),
            ("INSIDE_OUT", "Radial", "Smoothly glide parts inward from 360° around the model"),
        ],
        default="BOTTOM_UP",
        update=_on_interactive_build_update,
    )

    speed_preset: EnumProperty(
        name="Speed",
        items=[
            ("FAST_4S", "Fast", "Snappy studio breakdown"),
            ("NORMAL_6S", "Normal", "Smooth, balanced studio breakdown"),
            ("SLOW_10S", "Slow", "Relaxed, cinematic breakdown"),
        ],
        default="NORMAL_6S",
        update=_on_interactive_build_update,
    )

    wipe_direction: EnumProperty(
        name="Pass Laser Sweep Direction",
        items=[
            ("LEFT_TO_RIGHT", "Sideways: Left -> Right (3D Contour)", "Sweep 3D shader laser wave from left to right"),
            ("RIGHT_TO_LEFT", "Sideways: Right -> Left (3D Contour)", "Sweep 3D shader laser wave from right to left"),
            ("TOP_TO_BOTTOM", "Vertical: Top -> Bottom (3D Contour)", "Sweep 3D shader laser wave from top to bottom"),
            ("BOTTOM_TO_TOP", "Vertical: Bottom -> Top (3D Contour)", "Sweep 3D shader laser wave from bottom to top"),
        ],
        default="LEFT_TO_RIGHT",
    )

    reel_duration_sec: FloatProperty(
        name="Reel Duration (Sec)",
        description="Total duration of the output breakdown reel video in seconds",
        default=6.0,
        min=2.0,
        max=30.0,
    )

    frames_per_part: IntProperty(
        name="Frames Per Part (0=Auto)",
        description="Duration in frames for each part's entrance animation (0 = automatically calculated from total frames)",
        default=0,
        min=0,
        max=120,
    )

    stagger_frames: IntProperty(
        name="Part Delay Stagger (0=Auto)",
        description="Frame delay between consecutive parts/waves triggering (0 = automatically calculated)",
        default=0,
        min=0,
        max=60,
    )

    wire_style: EnumProperty(
        name="Wireframe Style",
        items=[
            ("CYAN_SLATE", "Blueprint Cyan on Dark Slate", "Glowing cyan edges on dark slate faces"),
            ("WHITE_TECH", "ArtStation Technical White", "Crisp white wireframe on matte charcoal"),
            ("GOLD_LUXURY", "Luxury Brushed Gold CAD", "Gold wireframe on obsidian black"),
            ("CLAY_WIRE", "Clay + Dark Wireframe Overlay", "Sculptor's clay with dark charcoal wireframe lines"),
        ],
        default="CYAN_SLATE",
    )

    use_quad_wire: BoolProperty(
        name="True Quad Wireframe (No Triangulation)",
        default=True,
    )

    render_engine: EnumProperty(
        name="Render Engine",
        items=[
            ("CYCLES", "Cycles (GPU / CPU Raytraced)", ""),
            ("EEVEE", "EEVEE (Real-Time Rasterized)", ""),
        ],
        default="CYCLES",
    )

    aspect_preset: EnumProperty(
        name="Aspect Ratio",
        items=[
            ("WIDESCREEN_16_9", "16:9 Widescreen (1920x1080 • YouTube / Desktop)", ""),
            ("PORTRAIT_4_5", "4:5 Portrait (1080x1350 • ArtStation / IG)", ""),
            ("VERTICAL_9_16", "9:16 Vertical (1080x1920 • Shorts / Reels / TikTok)", ""),
            ("SQUARE_1_1", "1:1 Square (1080x1080 • Carousel / Feed)", ""),
        ],
        default="WIDESCREEN_16_9",
    )

    fps_preset: EnumProperty(
        name="Video FPS",
        items=[
            ("30", "30 FPS", ""),
            ("60", "60 FPS", ""),
            ("24", "24 FPS", ""),
        ],
        default="30",
    )

    orbit_degrees: FloatProperty(
        name="Turntable Sweep (°)",
        default=64.0,
        min=0.0,
        max=360.0,
    )

    num_frames: IntProperty(
        name="Turntable Frames",
        default=140,
        min=24,
        max=600,
    )

    samples: IntProperty(
        name="Render Samples",
        default=32,
        min=8,
        max=512,
    )

    output_dir: StringProperty(
        name="Output Folder",
        subtype="DIR_PATH",
        default="//BreakdownStudio_Renders",
    )


def _get_resolution(aspect_preset: str) -> tuple[int, int]:
    if aspect_preset == "VERTICAL_9_16":
        return (1080, 1920)
    if aspect_preset == "WIDESCREEN_16_9":
        return (1920, 1080)
    if aspect_preset == "SQUARE_1_1":
        return (1080, 1080)
    return (1080, 1350)


def _switch_viewport_to_camera_and_shading(shading_type: str = "MATERIAL"):
    try:
        wm = bpy.context.window_manager
        for win in wm.windows:
            for area in win.screen.areas:
                if area.type == "VIEW_3D":
                    for space in area.spaces:
                        if space.type == "VIEW_3D":
                            space.shading.type = shading_type
                            space.shading.use_scene_lights = True
                            space.shading.use_scene_world = True
                            if space.region_3d:
                                space.region_3d.view_perspective = "CAMERA"
    except Exception:
        pass


class BS_OT_ActivateLicense(bpy.types.Operator):
    bl_idname = "breakdown_studio.activate_license"
    bl_label = "Activate License on This PC"

    def execute(self, context):
        cfg = context.scene.bs_settings
        ok, msg = activate_license_on_this_machine(cfg.license_key_input)
        cfg.license_status_msg = msg
        if ok:
            self.report({"INFO"}, msg)
        else:
            self.report({"ERROR"}, msg)
        return {"FINISHED"}


class BS_OT_DeactivateLicense(bpy.types.Operator):
    bl_idname = "breakdown_studio.deactivate_license"
    bl_label = "Lock / Deactivate PC"

    def execute(self, context):
        deactivate_license_on_this_machine()
        context.scene.bs_settings.license_status_msg = "License deactivated on this PC."
        self.report({"INFO"}, "BreakdownStudio locked on this PC.")
        return {"FINISHED"}


class BS_OT_LoadPolyHavenModel(bpy.types.Operator):
    bl_idname = "breakdown_studio.load_polyhaven_model"
    bl_label = "Load Sample CC0 3D Model"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        if not is_machine_licensed():
            self.report({"ERROR"}, "Please activate your License Key first.")
            return {"CANCELLED"}
        cfg = context.scene.bs_settings
        global _ORIGINAL_MATS_CACHE
        _ORIGINAL_MATS_CACHE.clear()
        ok = polyhaven.load_model_into_scene(cfg.polyhaven_model, "2k", clear_existing=True)
        if not ok:
            self.report({"WARNING"}, "Could not download model from Poly Haven.")
            return {"CANCELLED"}
        bpy.ops.breakdown_studio.setup_studio()
        return {"FINISHED"}


class BS_OT_ImportCustomModel(bpy.types.Operator):
    bl_idname = "breakdown_studio.import_custom_model"
    bl_label = "Import 3D File (.glb / .fbx / .obj / .blend)"
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(
        default="*.glb;*.gltf;*.fbx;*.obj;*.stl;*.ply;*.blend",
        options={"HIDDEN"},
    )
    clear_existing: BoolProperty(
        name="Clear Previous Subject Meshes First",
        default=False,
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        if not is_machine_licensed():
            self.report({"ERROR"}, "Please activate your License Key first.")
            return {"CANCELLED"}
        fpath = Path(self.filepath)
        if not fpath.exists() or not fpath.is_file():
            self.report({"WARNING"}, f"File not found: {fpath}")
            return {"CANCELLED"}

        ext = fpath.suffix.lower()
        previous_meshes = list(core.get_subject_meshes()) if self.clear_existing else []

        try:
            if ext in (".glb", ".gltf"):
                bpy.ops.import_scene.gltf(filepath=str(fpath))
            elif ext == ".fbx":
                bpy.ops.import_scene.fbx(filepath=str(fpath))
            elif ext == ".obj":
                bpy.ops.wm.obj_import(filepath=str(fpath))
            elif ext == ".stl":
                bpy.ops.wm.stl_import(filepath=str(fpath))
            elif ext == ".ply":
                bpy.ops.wm.ply_import(filepath=str(fpath))
            elif ext == ".blend":
                with bpy.data.libraries.load(str(fpath), link=False) as (data_from, data_to):
                    data_to.objects = list(data_from.objects)
                for obj in data_to.objects:
                    if obj is not None and obj.type in ("MESH", "ARMATURE", "EMPTY", "CURVE", "FONT", "SURFACE", "META"):
                        if not obj.name.startswith("BS_Studio_") and obj.name not in context.collection.objects:
                            context.collection.objects.link(obj)
        except Exception as e:
            self.report({"ERROR"}, f"Import failed: {e}")
            return {"CANCELLED"}

        if previous_meshes:
            for obj in previous_meshes:
                if obj and obj.name in bpy.data.objects:
                    bpy.data.objects.remove(obj, do_unlink=True)

        global _ORIGINAL_MATS_CACHE
        _ORIGINAL_MATS_CACHE.clear()
        bpy.ops.breakdown_studio.setup_studio()
        return {"FINISHED"}


class BS_OT_SetupStudioAndCamera(bpy.types.Operator):
    bl_idname = "breakdown_studio.setup_studio"
    bl_label = "Auto-Frame Camera & Studio Stage"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        if not is_machine_licensed():
            self.report({"ERROR"}, "Please activate your License Key first.")
            return {"CANCELLED"}
        cfg = context.scene.bs_settings
        meshes = core.get_subject_meshes(hide_helpers=True)
        if not meshes:
            self.report({"WARNING"}, "No visible 3D mesh objects found in scene!")
            return {"CANCELLED"}

        _ensure_mats_cached(meshes)
        bounds = core.compute_scene_bounds(meshes)
        hdri_path = polyhaven.ensure_hdri_downloaded(cfg.polyhaven_hdri, "2k")
        core.setup_hdri_and_stage(hdri_path, bounds, stage_theme=cfg.stage_theme)

        res_x, res_y = _get_resolution(cfg.aspect_preset)
        half_ang = cfg.orbit_degrees * 0.5
        fps_val = int(cfg.fps_preset)
        core.setup_turntable_camera(
            bounds,
            res_x=res_x,
            res_y=res_y,
            frame_start=1,
            frame_end=cfg.num_frames,
            start_angle_deg=-half_ang,
            end_angle_deg=half_ang,
            fps=fps_val,
        )
        configure_render_engine(cfg.render_engine, cfg.samples)
        context.scene.frame_set(max(1, cfg.num_frames // 2))
        _switch_viewport_to_camera_and_shading("MATERIAL")
        return {"FINISHED"}


class BS_OT_RestoreOriginalScene(bpy.types.Operator):
    bl_idname = "breakdown_studio.restore_scene"
    bl_label = "Reset Normal Scene"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        meshes = core.get_subject_meshes()
        orig_mats = _ensure_mats_cached(meshes)
        core.restore_full_scene(context.scene, meshes, orig_mats)
        self.report({"INFO"}, "BreakdownStudio: Original scene restored.")
        return {"FINISHED"}


class BS_OT_SwitchPassMode(bpy.types.Operator):
    bl_idname = "breakdown_studio.switch_pass"
    bl_label = "Preview Pass"
    bl_options = {"REGISTER", "UNDO"}

    mode: StringProperty(default="FINAL")

    def execute(self, context):
        if not is_machine_licensed():
            self.report({"ERROR"}, "Please activate your License Key first.")
            return {"CANCELLED"}
        cfg = context.scene.bs_settings
        meshes = core.get_subject_meshes()
        orig_mats = _ensure_mats_cached(meshes)
        core.apply_pass_materials(
            self.mode,
            meshes,
            orig_mats,
            wire_style=cfg.wire_style,
            use_quad_wire=cfg.use_quad_wire,
        )
        _switch_viewport_to_camera_and_shading("RENDERED" if self.mode == "WIREFRAME" and not cfg.use_quad_wire else "MATERIAL")
        return {"FINISHED"}


def _force_png_output(scene):
    img = scene.render.image_settings
    saved = {
        "file_format": img.file_format,
        "color_mode": img.color_mode,
        "media_type": getattr(img, "media_type", None),
    }
    try:
        if hasattr(img, "media_type"):
            img.media_type = "IMAGE"
        img.file_format = "PNG"
        img.color_mode = "RGBA"
    except Exception:
        pass
    return saved


def _restore_png_output(scene, saved):
    img = scene.render.image_settings
    try:
        if saved.get("media_type") is not None and hasattr(img, "media_type"):
            img.media_type = saved["media_type"]
        img.file_format = saved["file_format"]
        img.color_mode = saved["color_mode"]
    except Exception:
        pass


class BS_OT_RenderBreakdown(bpy.types.Operator):
    bl_idname = "breakdown_studio.render_breakdown"
    bl_label = "1-Click 6-Pass VFX Demoreel (Posters + 3D Laser Video)"
    bl_description = "Automatically sets up camera & studio lighting if missing, renders all 6 VFX passes (Rim Silhouette, Clay+AO, Quad Wire, Normal+UV, Albedo, Final Beauty), generates 6-Pass & 2x2 Grid Posters, and encodes the 3D Contour Laser Scan Demoreel MP4"
    bl_options = {"REGISTER"}

    def execute(self, context):
        if not is_machine_licensed():
            self.report({"ERROR"}, "Please activate your License Key first.")
            return {"CANCELLED"}

        from . import compositor
        importlib.reload(compositor)

        cfg = context.scene.bs_settings
        meshes = core.get_subject_meshes(hide_helpers=True)
        if not meshes:
            self.report({"WARNING"}, "No visible 3D mesh objects found in scene!")
            return {"CANCELLED"}

        if context.scene.camera is None:
            bpy.ops.breakdown_studio.setup_studio()

        orig_mats = _ensure_mats_cached(meshes)
        poly_stats = core.get_scene_poly_stats(meshes)
        out_dir = _resolve_portable_output_dir(cfg.output_dir, "BreakdownStudio_Renders")

        scene = context.scene
        orig_filepath = scene.render.filepath
        saved_fmt = _force_png_output(scene)
        configure_render_engine(cfg.render_engine, cfg.samples)

        paths = {}
        try:
            for pass_name, fname in [
                ("SILHOUETTE", "pass_01_silhouette.png"),
                ("CLAY", "pass_02_clay_ao.png"),
                ("WIREFRAME", "pass_03_wireframe.png"),
                ("UV_NORMAL", "pass_04_normal_uv.png"),
                ("ALBEDO", "pass_05_albedo.png"),
                ("FINAL", "pass_06_final_beauty.png"),
            ]:
                core.apply_pass_materials(
                    pass_name,
                    meshes,
                    orig_mats,
                    wire_style=cfg.wire_style,
                    use_quad_wire=cfg.use_quad_wire,
                )
                p = out_dir / fname
                scene.render.filepath = str(p)
                bpy.ops.render.render(write_still=True)
                paths[pass_name] = p
        finally:
            core.apply_pass_materials("FINAL", meshes, orig_mats, wire_style=cfg.wire_style, use_quad_wire=cfg.use_quad_wire)
            scene.render.filepath = orig_filepath
            _restore_png_output(scene, saved_fmt)

        for req_p in paths.values():
            if not req_p.exists():
                self.report({"ERROR"}, f"Render pass missing: {req_p.name}")
                return {"CANCELLED"}

        fps_val = int(cfg.fps_preset)
        total_reel_frames = max(30, int(round(cfg.reel_duration_sec * fps_val)))
        outputs = compositor.build_deep_6pass_demoreel_outputs(
            paths,
            out_dir=out_dir,
            num_frames=total_reel_frames,
            fps=fps_val,
            poly_stats=poly_stats,
        )

        poster_path = outputs.get("six_pass_poster") or outputs.get("grid_4up_poster")
        if poster_path:
            _show_image_in_blender_viewer(context, Path(poster_path))

        _open_folder_cross_platform(out_dir)
        saved_names = ", ".join(Path(v).name for v in outputs.values())
        self.report({"INFO"}, f"BreakdownStudio Pro: Saved {saved_names} to {out_dir}")
        return {"FINISHED"}


class BS_OT_AnimateAssembly(bpy.types.Operator):
    bl_idname = "breakdown_studio.animate_assembly"
    bl_label = "Play Breakdown in Viewport"
    bl_description = "Frames the camera turntable and plays the live 3D Breakdown Wave (Wireframe -> Clay -> Final) smoothly in the 3D Viewport"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        if not is_machine_licensed():
            self.report({"ERROR"}, "Please activate your License Key first.")
            return {"CANCELLED"}
        cfg = context.scene.bs_settings
        all_meshes = core.get_subject_meshes(hide_helpers=True, target_scope="ALL_SCENE")
        meshes = core.get_subject_meshes(hide_helpers=True, target_scope=cfg.target_scope)
        if not meshes:
            self.report({"WARNING"}, "No visible 3D mesh objects found for target!")
            return {"CANCELLED"}
        orig_mats = _ensure_mats_cached(all_meshes or meshes)

        # Ensure Studio Stage & Smooth Turntable Camera are framed on the target meshes
        bounds = core.compute_scene_bounds(meshes)
        hdri_path = polyhaven.ensure_hdri_downloaded(cfg.polyhaven_hdri, "2k")
        core.setup_hdri_and_stage(hdri_path, bounds, stage_theme=cfg.stage_theme)
        res_x, res_y = _get_resolution(cfg.aspect_preset)
        half_ang = cfg.orbit_degrees * 0.5
        fps_val = int(cfg.fps_preset)
        core.setup_turntable_camera(
            bounds,
            res_x=res_x,
            res_y=res_y,
            frame_start=1,
            frame_end=cfg.num_frames,
            start_angle_deg=-half_ang,
            end_angle_deg=half_ang,
            fps=fps_val,
        )

        context.scene.frame_start = 1
        context.scene.frame_end = cfg.num_frames
        core.setup_breakdown_assembly_animation(
            meshes,
            preset=cfg.anim_preset,
            frame_start=1,
            assembly_end_frame=cfg.num_frames,
            build_direction=cfg.build_direction,
            frames_per_part=cfg.frames_per_part,
            stagger_frames=cfg.stagger_frames,
            original_mats=orig_mats,
            wire_style=cfg.wire_style,
            live_shader=True,
        )
        _switch_viewport_to_camera_and_shading("MATERIAL")
        context.scene.frame_set(1)
        try:
            if not context.screen.is_animation_playing:
                bpy.ops.screen.animation_play()
        except Exception:
            pass
        self.report({"INFO"}, f"BreakdownStudio: Playing smooth 3D breakdown ({cfg.build_direction}) on {len(meshes)} objects.")
        return {"FINISHED"}


class BS_OT_ClearAssembly(bpy.types.Operator):
    bl_idname = "breakdown_studio.clear_assembly"
    bl_label = "Reset Model"
    bl_description = "Stops playback, removes breakdown shader wave, and restores your model's original materials and pose"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            if context.screen and context.screen.is_animation_playing:
                bpy.ops.screen.animation_cancel(restore_frame=False)
        except Exception:
            pass
        meshes = core.get_subject_meshes()
        orig_mats = _ensure_mats_cached(meshes)
        core.clear_breakdown_assembly_animation(meshes, original_mats=orig_mats)
        context.scene.frame_set(1)
        self.report({"INFO"}, "BreakdownStudio: Model restored to original materials and pose.")
        return {"FINISHED"}


class BS_OT_RenderTurntable(bpy.types.Operator):
    bl_idname = "breakdown_studio.render_turntable"
    bl_label = "1-Click Moving / 360° 6-Pass Demoreel Video"
    bl_description = "Renders the smooth 3D turntable + rotating studio lighting across all passes and outputs the Moving Breakdown Showreel + Live 2x2 LookDev Grid Reel"
    bl_options = {"REGISTER"}

    def execute(self, context):
        if not is_machine_licensed():
            self.report({"ERROR"}, "Please activate your License Key first.")
            return {"CANCELLED"}

        from . import compositor
        importlib.reload(compositor)

        cfg = context.scene.bs_settings
        meshes = core.get_subject_meshes(hide_helpers=True, target_scope=cfg.target_scope)
        if not meshes:
            self.report({"WARNING"}, "No visible 3D mesh objects found for target scope!")
            return {"CANCELLED"}

        orig_mats = _ensure_mats_cached(meshes)
        core.clear_breakdown_assembly_animation(meshes, original_mats=orig_mats)

        bounds = core.compute_scene_bounds(meshes)
        hdri_path = polyhaven.ensure_hdri_downloaded(cfg.polyhaven_hdri, "2k")
        core.setup_hdri_and_stage(hdri_path, bounds, stage_theme=cfg.stage_theme)
        res_x, res_y = _get_resolution(cfg.aspect_preset)
        half_ang = cfg.orbit_degrees * 0.5
        fps_val = int(cfg.fps_preset)
        core.setup_turntable_camera(
            bounds,
            res_x=res_x,
            res_y=res_y,
            frame_start=1,
            frame_end=cfg.num_frames,
            start_angle_deg=-half_ang,
            end_angle_deg=half_ang,
            fps=fps_val,
        )

        poly_stats = core.get_scene_poly_stats(meshes)
        out_dir = _resolve_portable_output_dir(cfg.output_dir, "BreakdownStudio_Turntable")
        raw_dir = out_dir / "raw_passes"
        raw_dir.mkdir(parents=True, exist_ok=True)

        scene = context.scene
        orig_filepath = scene.render.filepath
        orig_frame = scene.frame_current
        saved_fmt = _force_png_output(scene)
        configure_render_engine(cfg.render_engine, cfg.samples)

        try:
            for pass_name, prefix in [
                ("SILHOUETTE", "sil"),
                ("CLAY", "clay"),
                ("WIREFRAME", "wire"),
                ("UV_NORMAL", "norm"),
                ("ALBEDO", "alb"),
                ("FINAL", "final"),
            ]:
                core.apply_pass_materials(
                    pass_name,
                    meshes,
                    orig_mats,
                    wire_style=cfg.wire_style,
                    use_quad_wire=cfg.use_quad_wire,
                )
                for f_idx in range(1, cfg.num_frames + 1):
                    scene.frame_set(f_idx)
                    scene.render.filepath = str(raw_dir / f"{prefix}_{f_idx:04d}.png")
                    bpy.ops.render.render(write_still=True)
        finally:
            core.clear_breakdown_assembly_animation(meshes, original_mats=orig_mats)
            core.apply_pass_materials("FINAL", meshes, orig_mats, wire_style=cfg.wire_style, use_quad_wire=cfg.use_quad_wire)
            scene.render.filepath = orig_filepath
            scene.frame_set(orig_frame)
            _restore_png_output(scene, saved_fmt)

        outputs = compositor.assemble_deep_moving_demoreel_videos(
            out_dir,
            num_frames=cfg.num_frames,
            fps=fps_val,
            poly_stats=poly_stats,
            wipe_direction=cfg.wipe_direction,
        )
        _open_folder_cross_platform(out_dir)

        saved_names = ", ".join(Path(v).name for v in outputs.values())
        self.report({"INFO"}, f"BreakdownStudio Pro: Saved {saved_names} to {out_dir}")
        return {"FINISHED"}


class BS_PT_MainPanel(bpy.types.Panel):
    bl_label = "BreakdownStudio Pro (6-Pass VFX Reel)"
    bl_idname = "BS_PT_main_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BreakdownStudio"

    def draw(self, context):
        layout = self.layout
        cfg = context.scene.bs_settings
        hwid = get_machine_hwid()

        if not is_machine_licensed():
            lock_box = layout.box()
            lock_box.label(text="LICENSE ACTIVATION REQUIRED", icon="LOCKED")
            lock_box.label(text=f"PC HWID: {hwid[:10]}...", icon="DESKTOP")
            lock_box.prop(cfg, "license_key_input", text="Key")
            act_row = lock_box.row(align=True)
            act_row.scale_y = 1.6
            act_row.operator(
                "breakdown_studio.activate_license",
                text="ACTIVATE ON THIS PC",
                icon="KEY_HLT",
            )
            if cfg.license_status_msg:
                lock_box.label(text=cfg.license_status_msg, icon="INFO")
            lock_box.separator()
            buy_row = lock_box.row(align=True)
            buy_row.operator(
                "wm.url_open",
                text="Get License Key ($39) ->",
                icon="URL",
            ).url = STORE_BUY_URL
            return

        # ======================================================================
        # ULTRA-MINIMAL 1-CLICK INTERFACE (ZERO DROPDOWNS, ZERO CLUTTER)
        # ======================================================================
        box_bm = layout.box()

        # 1. What to Breakdown: [ All Models | Selected Only ]
        row_t = box_bm.row(align=True)
        row_t.scale_y = 1.2
        row_t.prop(cfg, "target_scope", expand=True)

        # 2. Direction: [ Bottom-Up | Top-Down | Sideways | Radial ]
        row_dir = box_bm.row(align=True)
        row_dir.scale_y = 1.25
        row_dir.prop(cfg, "build_direction", expand=True)

        # 3. Speed: [ Fast | Normal | Slow ]
        row_spd = box_bm.row(align=True)
        row_spd.scale_y = 1.15
        row_spd.prop(cfg, "speed_preset", expand=True)

        # 4. Live Viewport Breakdown Play & Reset
        row_play = box_bm.row(align=True)
        row_play.scale_y = 1.5
        row_play.operator("breakdown_studio.animate_assembly", text="Play Breakdown", icon="PLAY")
        row_play.operator("breakdown_studio.clear_assembly", text="Reset", icon="LOOP_BACK")

        # 5. Simple 3-Button Shading Bar (Clay | Wireframe | Final)
        row_look = layout.row(align=True)
        row_look.scale_y = 1.2
        row_look.operator("breakdown_studio.switch_pass", text="Clay").mode = "CLAY"
        row_look.operator("breakdown_studio.switch_pass", text="Wireframe").mode = "WIREFRAME"
        row_look.operator("breakdown_studio.switch_pass", text="Final").mode = "FINAL"

        # 6. Primary Render Video Button
        col_hero = layout.column(align=True)
        r_main = col_hero.row(align=True)
        r_main.scale_y = 1.85
        r_main.operator(
            "breakdown_studio.render_turntable",
            text="RENDER BREAKDOWN VIDEO",
            icon="RENDER_ANIMATION",
        )

        # 7. Collapsed Optional Studio Settings (Hidden unless clicked)
        box_opt = layout.box()
        box_opt.prop(
            cfg,
            "show_customize",
            text="More Options (Camera, Posters & Studio)",
            icon="TRIA_DOWN" if cfg.show_customize else "TRIA_RIGHT",
            emboss=False,
        )
        if cfg.show_customize:
            row_st = box_opt.row(align=True)
            row_st.operator("breakdown_studio.setup_studio", text="Frame Camera", icon="CAMERA_DATA")
            row_st.operator("breakdown_studio.render_breakdown", text="6-Pass Poster", icon="RENDER_STILL")
            row_p2 = box_opt.row(align=True)
            row_p2.operator("breakdown_studio.switch_pass", text="Rim").mode = "SILHOUETTE"
            row_p2.operator("breakdown_studio.switch_pass", text="UV/Norm").mode = "UV_NORMAL"
            row_p2.operator("breakdown_studio.switch_pass", text="Albedo").mode = "ALBEDO"
            box_opt.separator()
            box_opt.prop(cfg, "stage_theme", text="Stage")
            box_opt.prop(cfg, "wire_style", text="Wire")
            box_opt.prop(cfg, "aspect_preset", text="Aspect")
            Row_eng = box_opt.row(align=True)
            Row_eng.prop(cfg, "render_engine", text="Engine")
            Row_eng.prop(cfg, "samples", text="Samples")
            box_opt.prop(cfg, "output_dir", text="Save")
            box_opt.separator()
            box_opt.operator("breakdown_studio.import_custom_model", text="Import 3D File", icon="FILE_FOLDER")
            box_opt.operator("breakdown_studio.restore_scene", text="Restore Original Scene", icon="LOOP_BACK")

        # Hardware License Status Badge at bottom
        lic_row = layout.row(align=True)
        lic_row.label(text=f"PC Locked: {hwid[:8]}", icon="UNLOCKED")
        lic_row.operator("breakdown_studio.deactivate_license", text="", icon="LOCKED")


CLASSES = (
    BS_PG_Settings,
    BS_OT_ActivateLicense,
    BS_OT_DeactivateLicense,
    BS_OT_LoadPolyHavenModel,
    BS_OT_ImportCustomModel,
    BS_OT_SetupStudioAndCamera,
    BS_OT_RestoreOriginalScene,
    BS_OT_SwitchPassMode,
    BS_OT_AnimateAssembly,
    BS_OT_ClearAssembly,
    BS_OT_RenderBreakdown,
    BS_OT_RenderTurntable,
    BS_PT_MainPanel,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.bs_settings = PointerProperty(type=BS_PG_Settings)


def unregister():
    if hasattr(bpy.types.Scene, "bs_settings"):
        del bpy.types.Scene.bs_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
