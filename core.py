"""
BreakdownStudio 3D v2.0 Pro — Core 3-Pass Breakdown Engine for Blender 4.2+ & 5.x
Provides:
1. Read-only subject mesh discovery (never mutates visibility during UI draw)
2. Full non-destructive SceneSnapshot & Restore (preserves original World, lights,
   camera, resolution, frame range, materials, and exact Subsurf/Multires levels)
3. True Quad-Topology Wireframe mode (using non-destructive Wireframe modifier with
   automatic shader fallback) + Sculptor Clay + Final PBR
4. Studio Stage + HDRI (with offline procedural studio lighting fallback)
"""

import math
from pathlib import Path
import bpy
import bmesh
from mathutils import Vector


def _get_bone_widget_names() -> set[str]:
    widgets = set()
    for obj in bpy.data.objects:
        if obj.type == "ARMATURE" and obj.pose:
            for pb in obj.pose.bones:
                if pb.custom_shape:
                    widgets.add(pb.custom_shape.name)
    return widgets


def get_subject_meshes(
    hide_helpers: bool = False,
    target_scope: str = "ALL_SCENE",
) -> list[bpy.types.Object]:
    """
    Returns visible subject mesh objects according to `target_scope`:
      - `ALL_SCENE`         : All visible subject meshes in the scene (1 or many models)
      - `SELECTED_ONLY`     : Only currently selected mesh objects (plus mesh children of
                              any selected Empty/Armature parent). Falls back to `ALL_SCENE`
                              if nothing is selected.
      - `ACTIVE_COLLECTION` : Only meshes inside the active collection (and its sub-collections).
    By default (`hide_helpers=False`), this function is 100% read-only and safe to call
    inside UI `draw()` methods without mutating scene objects.
    """
    bone_widgets = _get_bone_widget_names()
    ignore_prefixes = ("bs_studio_", "cs_", "wgt_", "wgts_", "ucx_")

    allowed_set: set[str] | None = None
    if target_scope == "SELECTED_ONLY":
        sel_objs = getattr(bpy.context, "selected_objects", None) or []
        expanded = set()
        for o in sel_objs:
            if o.type == "MESH":
                expanded.add(o.name)
            for ch in o.children_recursive:
                if ch.type == "MESH":
                    expanded.add(ch.name)
        if expanded:
            allowed_set = expanded
    elif target_scope == "ACTIVE_COLLECTION":
        act_col = getattr(bpy.context, "collection", None)
        if act_col and act_col != bpy.context.scene.collection:
            col_names = {o.name for o in act_col.all_objects if o.type == "MESH"}
            if col_names:
                allowed_set = col_names

    subject = []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or obj.data is None:
            continue
        lname = obj.name.lower()
        is_helper = (
            obj.name in bone_widgets
            or lname.startswith(ignore_prefixes)
            or obj.display_type in ("WIRE", "BOUNDS")
            or len(obj.data.polygons) == 0
        )
        if is_helper:
            if hide_helpers and not lname.startswith("bs_studio_"):
                obj.hide_render = True
                obj.hide_viewport = True
            continue
        if not obj.hide_render and not obj.hide_get():
            if allowed_set is None or obj.name in allowed_set:
                subject.append(obj)
    return subject


def get_scene_poly_stats(meshes: list[bpy.types.Object], use_evaluated: bool = False) -> dict:
    """Computes vertex, polygon, and triangle counts across subject meshes."""
    verts = 0
    polys = 0
    tris = 0
    depsgraph = bpy.context.evaluated_depsgraph_get() if use_evaluated else None
    for obj in meshes:
        mesh_data = obj.evaluated_get(depsgraph).to_mesh() if depsgraph else obj.data
        if mesh_data:
            verts += len(mesh_data.vertices)
            polys += len(mesh_data.polygons)
            for p in mesh_data.polygons:
                tris += max(1, len(p.vertices) - 2)
        if depsgraph and mesh_data:
            obj.evaluated_get(depsgraph).to_mesh_clear()
    return {
        "objects": len(meshes),
        "vertices": verts,
        "polygons": polys,
        "triangles": tris,
    }


def compute_scene_bounds(meshes: list[bpy.types.Object]) -> dict:
    """Computes evaluated world-space bounding box, center, and bounding radius of the subject."""
    if not meshes:
        return {
            "min": Vector((-0.5, -0.5, 0.0)),
            "max": Vector((0.5, 0.5, 1.0)),
            "center": Vector((0.0, 0.0, 0.5)),
            "size": Vector((1.0, 1.0, 1.0)),
            "radius": 0.866,
        }

    depsgraph = None
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
    except Exception:
        depsgraph = None

    world_pts = []
    for obj in meshes:
        eval_obj = obj.evaluated_get(depsgraph) if depsgraph is not None else obj
        bbox = getattr(eval_obj, "bound_box", None) or obj.bound_box
        mw = getattr(eval_obj, "matrix_world", None) or obj.matrix_world
        for corner in bbox:
            world_pts.append(mw @ Vector(corner))

    xs = [p.x for p in world_pts]
    ys = [p.y for p in world_pts]
    zs = [p.z for p in world_pts]

    vmin = Vector((min(xs), min(ys), min(zs)))
    vmax = Vector((max(xs), max(ys), max(zs)))
    size = vmax - vmin
    center = (vmin + vmax) * 0.5
    radius = max(size.length * 0.5, 0.01)

    return {
        "min": vmin,
        "max": vmax,
        "center": center,
        "size": size,
        "radius": radius,
    }


STAGE_THEMES = {
    "DARK_OBSIDIAN": {"color": (0.022, 0.023, 0.026, 1.0), "roughness": 0.26, "transparent": False},
    "NEUTRAL_GRAY": {"color": (0.14, 0.145, 0.155, 1.0), "roughness": 0.38, "transparent": False},
    "WARM_CREAM": {"color": (0.36, 0.33, 0.29, 1.0), "roughness": 0.42, "transparent": False},
    "TRANSPARENT_ALPHA": {"color": (0.022, 0.023, 0.026, 1.0), "roughness": 0.26, "transparent": True},
}


def save_scene_backup(scene: bpy.types.Scene):
    """
    Saves original scene settings (World pointer, active Camera, resolution, FPS,
    frame range, film_transparent, and light visibility) into scene custom properties
    if not already saved, so the user's scene can be restored 100% losslessly.
    """
    if "_bs_backup_saved" in scene and scene["_bs_backup_saved"]:
        return

    scene["_bs_backup_saved"] = True
    scene["_bs_orig_world"] = scene.world.name if (scene.world and not scene.world.name.startswith("BS_Studio_")) else ""
    scene["_bs_orig_camera"] = scene.camera.name if (scene.camera and not scene.camera.name.startswith("BS_Studio_")) else ""
    scene["_bs_orig_res_x"] = int(scene.render.resolution_x)
    scene["_bs_orig_res_y"] = int(scene.render.resolution_y)
    scene["_bs_orig_fps"] = int(scene.render.fps)
    scene["_bs_orig_frame_start"] = int(scene.frame_start)
    scene["_bs_orig_frame_end"] = int(scene.frame_end)
    scene["_bs_orig_frame_current"] = int(scene.frame_current)
    scene["_bs_orig_film_transparent"] = bool(scene.render.film_transparent)
    scene["_bs_orig_use_compositing"] = bool(scene.render.use_compositing)
    scene["_bs_orig_use_sequencer"] = bool(scene.render.use_sequencer)
    scene["_bs_orig_engine"] = str(scene.render.engine)
    scene["_bs_orig_view_transform"] = str(scene.view_settings.view_transform)
    scene["_bs_orig_look"] = str(scene.view_settings.look)
    scene["_bs_orig_exposure"] = float(scene.view_settings.exposure)

    for obj in scene.objects:
        if obj.type == "LIGHT" and not obj.name.startswith("BS_Studio_"):
            obj["_bs_orig_hide_render"] = bool(obj.hide_render)
            obj["_bs_orig_hide_viewport"] = bool(obj.hide_viewport)


def restore_full_scene(scene: bpy.types.Scene, meshes: list[bpy.types.Object], orig_mats: dict):
    """
    Restores all materials, modifier levels, removes temporary wireframe modifiers,
    clears Breakdown Maker assembly delta keyframes, removes BS_Studio_* rig objects,
    and restores the user's original World, lights, camera, and render settings.
    """
    apply_pass_materials("FINAL", meshes, orig_mats)
    clear_breakdown_assembly_animation(meshes)

    for obj in list(bpy.data.objects):
        if obj.name.startswith("BS_Studio_"):
            bpy.data.objects.remove(obj, do_unlink=True)

    if scene.get("_bs_backup_saved"):
        orig_world_name = scene.get("_bs_orig_world", "")
        if orig_world_name and orig_world_name in bpy.data.worlds:
            scene.world = bpy.data.worlds[orig_world_name]
        orig_cam_name = scene.get("_bs_orig_camera", "")
        if orig_cam_name and orig_cam_name in bpy.data.objects:
            scene.camera = bpy.data.objects[orig_cam_name]

        scene.render.resolution_x = int(scene.get("_bs_orig_res_x", scene.render.resolution_x))
        scene.render.resolution_y = int(scene.get("_bs_orig_res_y", scene.render.resolution_y))
        scene.render.fps = int(scene.get("_bs_orig_fps", scene.render.fps))
        scene.frame_start = int(scene.get("_bs_orig_frame_start", scene.frame_start))
        scene.frame_end = int(scene.get("_bs_orig_frame_end", scene.frame_end))
        scene.frame_set(int(scene.get("_bs_orig_frame_current", scene.frame_current)))
        scene.render.film_transparent = bool(scene.get("_bs_orig_film_transparent", False))
        scene.render.use_compositing = bool(scene.get("_bs_orig_use_compositing", True))
        scene.render.use_sequencer = bool(scene.get("_bs_orig_use_sequencer", False))
        try:
            scene.render.engine = str(scene.get("_bs_orig_engine", scene.render.engine))
            scene.view_settings.view_transform = str(scene.get("_bs_orig_view_transform", "AgX"))
            scene.view_settings.look = str(scene.get("_bs_orig_look", "None"))
            scene.view_settings.exposure = float(scene.get("_bs_orig_exposure", 0.0))
        except Exception:
            pass

        for obj in scene.objects:
            if obj.type == "LIGHT" and not obj.name.startswith("BS_Studio_"):
                if "_bs_orig_hide_render" in obj:
                    obj.hide_render = bool(obj["_bs_orig_hide_render"])
                    del obj["_bs_orig_hide_render"]
                if "_bs_orig_hide_viewport" in obj:
                    obj.hide_viewport = bool(obj["_bs_orig_hide_viewport"])
                    del obj["_bs_orig_hide_viewport"]

        scene["_bs_backup_saved"] = False


def setup_hdri_and_stage(
    hdri_path: str | Path | None,
    bounds: dict,
    hdri_strength: float = 1.15,
    hdri_rotation_z: float = 0.65,
    stage_theme: str = "DARK_OBSIDIAN",
):
    """
    Sets up a dedicated `BS_Studio_World` environment shader (never overwriting the
    user's original World datablock) plus a studio cyclorama and 3-point rim/key lights.
    """
    scene = bpy.context.scene
    save_scene_backup(scene)

    bs_world = bpy.data.worlds.get("BS_Studio_World")
    if bs_world is None:
        bs_world = bpy.data.worlds.new("BS_Studio_World")
    bs_world.use_nodes = True
    scene.world = bs_world

    nt = bs_world.node_tree
    nodes = nt.nodes
    links = nt.links
    nodes.clear()

    out = nodes.new("ShaderNodeOutputWorld")
    out.location = (420, 0)

    bg = nodes.new("ShaderNodeBackground")
    bg.location = (180, 0)
    bg.inputs["Strength"].default_value = hdri_strength

    has_hdri = bool(hdri_path and Path(hdri_path).exists())
    if has_hdri:
        tex_coord = nodes.new("ShaderNodeTexCoord")
        tex_coord.location = (-580, 0)

        mapping = nodes.new("ShaderNodeMapping")
        mapping.location = (-360, 0)
        mapping.inputs["Rotation"].default_value = (0.0, 0.0, hdri_rotation_z)

        env_tex = nodes.new("ShaderNodeTexEnvironment")
        env_tex.location = (-120, 0)
        env_tex.image = bpy.data.images.load(str(Path(hdri_path).resolve()), check_existing=True)

        links.new(tex_coord.outputs["Generated"], mapping.inputs["Vector"])
        links.new(mapping.outputs["Vector"], env_tex.inputs["Vector"])
        links.new(env_tex.outputs["Color"], bg.inputs["Color"])
    else:
        bg.inputs["Color"].default_value = (0.22, 0.23, 0.26, 1.0)

    # Use Light Path 'Is Camera Ray' so HDRI illuminates & reflects on the 3D model at full power,
    # while direct camera rays behind the stage see a clean dark studio void (never leaking raw HDRI rooms!).
    theme_cfg = STAGE_THEMES.get(stage_theme, STAGE_THEMES["DARK_OBSIDIAN"])
    lp = nodes.new("ShaderNodeLightPath")
    bg_cam = nodes.new("ShaderNodeBackground")
    bg_cam.inputs["Color"].default_value = theme_cfg["color"]
    bg_cam.inputs["Strength"].default_value = 1.0
    mix_bg = nodes.new("ShaderNodeMixShader")
    links.new(lp.outputs["Is Camera Ray"], mix_bg.inputs["Fac"])
    links.new(bg.outputs["Background"], mix_bg.inputs[1])
    links.new(bg_cam.outputs["Background"], mix_bg.inputs[2])
    links.new(mix_bg.outputs["Shader"], out.inputs["Surface"])

    # Remove old BS_Studio_* objects if rebuilding, and temporarily hide user lights
    for obj in list(bpy.data.objects):
        if obj.name.startswith("BS_Studio_"):
            bpy.data.objects.remove(obj, do_unlink=True)
        elif obj.type == "LIGHT":
            if "_bs_orig_hide_render" not in obj:
                obj["_bs_orig_hide_render"] = bool(obj.hide_render)
                obj["_bs_orig_hide_viewport"] = bool(obj.hide_viewport)
            obj.hide_render = True
            obj.hide_viewport = True

    scene.render.use_compositing = False
    scene.render.use_sequencer = False

    scene.render.film_transparent = bool(theme_cfg["transparent"])

    r = bounds["radius"]
    cx, cy, _ = bounds["center"]
    z_floor = bounds["min"].z

    if not theme_cfg["transparent"]:
        stage_mat = bpy.data.materials.get("BS_Studio_Stage_Mat") or bpy.data.materials.new("BS_Studio_Stage_Mat")
        stage_mat.use_nodes = True
        s_nt = stage_mat.node_tree
        s_nt.nodes.clear()
        s_out = s_nt.nodes.new("ShaderNodeOutputMaterial")
        s_bsdf = s_nt.nodes.new("ShaderNodeBsdfPrincipled")
        s_bsdf.inputs["Base Color"].default_value = theme_cfg["color"]
        s_bsdf.inputs["Roughness"].default_value = theme_cfg["roughness"]
        if "Specular IOR Level" in s_bsdf.inputs:
            s_bsdf.inputs["Specular IOR Level"].default_value = 0.45

        # Smooth radial vignette from lit contact-shadow floor (0..1.8*r) to pure seamless studio void (>4.2*r)
        # so rotating Area Lights never cast a 180-degree horizon line across the distant cyclorama wall!
        s_geom = s_nt.nodes.new("ShaderNodeNewGeometry")
        s_dist = s_nt.nodes.new("ShaderNodeVectorMath")
        s_dist.operation = "DISTANCE"
        s_dist.inputs[1].default_value = (float(cx), float(cy), float(z_floor))
        s_nt.links.new(s_geom.outputs["Position"], s_dist.inputs[0])

        s_map = s_nt.nodes.new("ShaderNodeMapRange")
        s_map.clamp = True
        s_map.inputs["From Min"].default_value = float(r * 1.6)
        s_map.inputs["From Max"].default_value = float(r * 4.5)
        s_map.inputs["To Min"].default_value = 0.0
        s_map.inputs["To Max"].default_value = 1.0
        s_nt.links.new(s_dist.outputs["Value"], s_map.inputs["Value"])

        s_bg = s_nt.nodes.new("ShaderNodeBackground")
        tc = theme_cfg["color"]
        s_bg.inputs["Color"].default_value = (tc[0] * 0.55, tc[1] * 0.55, tc[2] * 0.58, 1.0)
        s_bg.inputs["Strength"].default_value = 1.0

        s_mix = s_nt.nodes.new("ShaderNodeMixShader")
        s_nt.links.new(s_map.outputs["Result"], s_mix.inputs["Fac"])
        s_nt.links.new(s_bsdf.outputs["BSDF"], s_mix.inputs[1])
        s_nt.links.new(s_bg.outputs["Background"], s_mix.inputs[2])
        s_nt.links.new(s_mix.outputs["Shader"], s_out.inputs["Surface"])

        # 360-degree radial infinity studio cyclorama bowl with a wide flat stage (8.0 * r)
        # so the camera is NEVER behind or underneath the cyclorama wall at any aspect ratio!
        bm = bmesh.new()
        grid_size = r * 36.0
        bmesh.ops.create_grid(bm, x_segments=64, y_segments=64, size=grid_size)
        for v in bm.verts:
            v.co.x += cx
            v.co.y += cy
            v.co.z = z_floor - 0.0002
            dist_xy = math.hypot(v.co.x - cx, v.co.y - cy)
            dr = dist_xy - (r * 8.0)
            if dr > 0:
                v.co.z += (dr ** 1.55) / (r * 4.5)

        floor_mesh = bpy.data.meshes.new("BS_Studio_Cyclorama")
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        bm.to_mesh(floor_mesh)
        bm.free()

        floor_obj = bpy.data.objects.new("BS_Studio_Cyclorama", floor_mesh)
        for poly in floor_obj.data.polygons:
            poly.use_smooth = True
        floor_obj.data.materials.append(stage_mat)
        bpy.context.scene.collection.objects.link(floor_obj)

    light_pivot = bpy.data.objects.get("BS_Studio_Light_Pivot")
    if light_pivot is None:
        light_pivot = bpy.data.objects.new("BS_Studio_Light_Pivot", None)
        bpy.context.scene.collection.objects.link(light_pivot)
    light_pivot.location = bounds["center"]
    light_pivot.rotation_euler = (0.0, 0.0, 0.0)

    def add_scaled_light(name, offset_vec, energy_mult, color, size_mult):
        ld = bpy.data.lights.new(name=name, type="AREA")
        ld.energy = energy_mult * (r ** 2) * 35.0
        ld.color = color
        ld.shape = "RECTANGLE"
        ld.size = r * size_mult
        ld.size_y = r * size_mult * 1.4
        lo = bpy.data.objects.new(name=name, object_data=ld)
        lo.parent = light_pivot
        lo.location = offset_vec * r
        direction = -offset_vec
        lo.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
        bpy.context.scene.collection.objects.link(lo)

    # Boost key light automatically if offline (no HDRI)
    key_boost = 1.0 if has_hdri else 2.2
    add_scaled_light("BS_Studio_Rim_Warm", Vector((-1.9, 1.6, 1.3)), 2.2 * key_boost, (1.0, 0.88, 0.72), 1.4)
    add_scaled_light("BS_Studio_Rim_Cool", Vector((2.0, 1.3, 1.1)), 1.6 * key_boost, (0.82, 0.90, 1.0), 1.3)
    if not has_hdri:
        add_scaled_light("BS_Studio_Front_Fill", Vector((0.3, -2.2, 1.2)), 2.8, (0.98, 0.98, 1.0), 1.8)

    try:
        scene.view_settings.view_transform = "AgX"
        scene.view_settings.look = "AgX - Medium High Contrast"
    except Exception:
        pass
    scene.view_settings.exposure = 0.1


def setup_turntable_camera(
    bounds: dict,
    res_x: int = 1080,
    res_y: int = 1920,
    frame_start: int = 1,
    frame_end: int = 90,
    start_angle_deg: float = -32.0,
    end_angle_deg: float = 32.0,
    fps: int = 30,
    dolly_push_ratio: float = 0.10,
    light_sweep_deg: float = 110.0,
) -> bpy.types.Object:
    """
    Creates a smooth 3D orbital camera rig centered on the subject's true 3D center,
    with subtle cinematic dolly push-in and synchronized studio lighting / HDRI rotation
    ("Breakdown Maker++" dynamic lighting sweep).
    """
    scene = bpy.context.scene
    save_scene_backup(scene)

    scene.render.resolution_x = res_x
    scene.render.resolution_y = res_y
    scene.render.resolution_percentage = 100
    if hasattr(scene.render.image_settings, "media_type"):
        scene.render.image_settings.media_type = "IMAGE"
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.frame_start = frame_start
    scene.frame_end = frame_end
    scene.render.fps = fps

    r = bounds["radius"]
    center = bounds["center"]

    target = bpy.data.objects.get("BS_Studio_Target")
    if target is None:
        target = bpy.data.objects.new("BS_Studio_Target", None)
        bpy.context.scene.collection.objects.link(target)
    target.location = center

    pivot = bpy.data.objects.get("BS_Studio_Pivot")
    if pivot is None:
        pivot = bpy.data.objects.new("BS_Studio_Pivot", None)
        bpy.context.scene.collection.objects.link(pivot)
    pivot.location = center

    cam_obj = bpy.data.objects.get("BS_Studio_Camera")
    if cam_obj is None:
        cam_data = bpy.data.cameras.new("BS_Studio_Camera")
        cam_obj = bpy.data.objects.new("BS_Studio_Camera", cam_data)
        bpy.context.scene.collection.objects.link(cam_obj)
    else:
        cam_data = cam_obj.data

    cam_obj.parent = pivot
    lens_mm = 65.0
    cam_data.lens = lens_mm
    cam_data.sensor_width = 36.0
    cam_data.sensor_fit = "HORIZONTAL"
    cam_data.clip_start = max(0.001, r * 0.02)
    cam_data.clip_end = r * 100.0
    cam_data.dof.use_dof = True
    cam_data.dof.focus_object = target
    cam_data.dof.aperture_fstop = 4.0

    tan_half_fov_x = 18.0 / lens_mm
    tan_half_fov_y = tan_half_fov_x * (res_y / max(1.0, float(res_x)))

    size_vec = bounds["size"]
    horiz_span = math.hypot(size_vec.x, size_vec.y)
    vert_span = size_vec.z

    dist_for_width = (horiz_span * 0.5 * 1.30) / tan_half_fov_x
    dist_for_height = (vert_span * 0.5 * 1.40) / tan_half_fov_y
    cam_dist = max(dist_for_width, dist_for_height, r * 2.4)

    cam_obj.animation_data_clear()
    d_start = cam_dist * (1.0 + dolly_push_ratio * 0.5)
    d_end = cam_dist * (1.0 - dolly_push_ratio * 0.5)
    cam_obj.location = Vector((0.0, -d_start, cam_dist * 0.25))
    cam_obj.keyframe_insert(data_path="location", frame=frame_start)
    cam_obj.location = Vector((0.0, -d_end, cam_dist * 0.19))
    cam_obj.keyframe_insert(data_path="location", frame=frame_end)

    cam_obj.constraints.clear()
    track = cam_obj.constraints.new(type="TRACK_TO")
    track.target = target
    track.track_axis = "TRACK_NEGATIVE_Z"
    track.up_axis = "UP_Y"

    scene.camera = cam_obj

    base_yaw = 32.0 if size_vec.y > size_vec.x * 1.35 else 0.0

    pivot.animation_data_clear()
    pivot.rotation_euler = (0.0, 0.0, math.radians(base_yaw + start_angle_deg))
    pivot.keyframe_insert(data_path="rotation_euler", frame=frame_start)
    pivot.rotation_euler = (0.0, 0.0, math.radians(base_yaw + end_angle_deg))
    pivot.keyframe_insert(data_path="rotation_euler", frame=frame_end)

    # Set LINEAR interpolation on turntable camera & pivot so orbital speed is 100% constant and silky-smooth
    for anim_obj in (pivot, cam_obj):
        if anim_obj.animation_data and anim_obj.animation_data.action and hasattr(anim_obj.animation_data.action, "fcurves"):
            for fc in anim_obj.animation_data.action.fcurves:
                for kp in fc.keyframe_points:
                    kp.interpolation = "LINEAR"
                fc.update()

    # Animate studio light pivot & HDRI rotation for dynamic specular highlight sweep
    light_pivot = bpy.data.objects.get("BS_Studio_Light_Pivot")
    if light_pivot is not None:
        light_pivot.animation_data_clear()
        light_pivot.rotation_euler = (0.0, 0.0, math.radians(-light_sweep_deg * 0.5))
        light_pivot.keyframe_insert(data_path="rotation_euler", frame=frame_start)
        light_pivot.rotation_euler = (0.0, 0.0, math.radians(light_sweep_deg * 0.5))
        light_pivot.keyframe_insert(data_path="rotation_euler", frame=frame_end)

    bs_world = bpy.data.worlds.get("BS_Studio_World")
    if bs_world and bs_world.use_nodes and bs_world.node_tree:
        if bs_world.node_tree.animation_data:
            bs_world.node_tree.animation_data_clear()
        for node in bs_world.node_tree.nodes:
            if node.type == "MAPPING":
                rot_sock = node.inputs["Rotation"]
                rot_sock.default_value = (0.0, 0.0, 0.65 - math.radians(light_sweep_deg * 0.4))
                rot_sock.keyframe_insert(data_path="default_value", frame=frame_start)
                rot_sock.default_value = (0.0, 0.0, 0.65 + math.radians(light_sweep_deg * 0.4))
                rot_sock.keyframe_insert(data_path="default_value", frame=frame_end)

    return cam_obj


def create_clay_material(clay_tint: tuple = (0.42, 0.37, 0.33, 1.0), highlight_tint: tuple = (0.72, 0.65, 0.58, 1.0)) -> bpy.types.Material:
    """Creates a Sculptor's Clay + Ambient Occlusion Cavity material with Pointiness ridge highlights."""
    name = "BS_Pass_Clay_Mat"
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links
    nodes.clear()

    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (520, 0)

    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (220, 0)
    bsdf.inputs["Base Color"].default_value = (0.54, 0.47, 0.41, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.38
    if "Specular IOR Level" in bsdf.inputs:
        bsdf.inputs["Specular IOR Level"].default_value = 0.38
    if "Coat Weight" in bsdf.inputs:
        bsdf.inputs["Coat Weight"].default_value = 0.12
        bsdf.inputs["Coat Roughness"].default_value = 0.22

    geom = nodes.new("ShaderNodeNewGeometry")
    geom.location = (-460, 80)

    ramp = nodes.new("ShaderNodeMapRange")
    ramp.location = (-260, 80)
    ramp.inputs["From Min"].default_value = 0.45
    ramp.inputs["From Max"].default_value = 0.56
    ramp.inputs["To Min"].default_value = 0.0
    ramp.inputs["To Max"].default_value = 1.0
    ramp.clamp = True

    lw = nodes.new("ShaderNodeLayerWeight")
    lw.location = (-460, -140)
    lw.inputs["Blend"].default_value = 0.25

    ao_math = nodes.new("ShaderNodeMath")
    ao_math.operation = "MULTIPLY"
    ao_math.location = (-240, -140)

    mix_cavity = nodes.new("ShaderNodeMix")
    mix_cavity.data_type = "RGBA"
    mix_cavity.location = (-40, 0)
    mix_cavity.inputs["A"].default_value = (0.20, 0.17, 0.15, 1.0)
    mix_cavity.inputs["B"].default_value = highlight_tint

    links.new(geom.outputs["Pointiness"], ramp.inputs["Value"])
    links.new(ramp.outputs["Result"], ao_math.inputs[0])
    links.new(lw.outputs["Facing"], ao_math.inputs[1])
    links.new(ao_math.outputs["Value"], mix_cavity.inputs["Factor"])
    links.new(mix_cavity.outputs["Result"], bsdf.inputs["Base Color"])
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    return mat


def create_silhouette_rim_material() -> bpy.types.Material:
    """
    Pass 01 // RIM LIGHTING SILHOUETTE:
    Obsidian matte body with razor-sharp grazing-angle Fresnel rim glow to showcase pure form & contour.
    """
    name = "BS_Pass_Silhouette_Mat"
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links
    nodes.clear()

    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (480, 0)

    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (180, 0)
    bsdf.inputs["Base Color"].default_value = (0.015, 0.017, 0.022, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.22
    bsdf.inputs["Metallic"].default_value = 0.35

    lw = nodes.new("ShaderNodeLayerWeight")
    lw.location = (-380, -80)
    lw.inputs["Blend"].default_value = 0.32

    ramp = nodes.new("ShaderNodeMapRange")
    ramp.location = (-180, -80)
    ramp.inputs["From Min"].default_value = 0.42
    ramp.inputs["From Max"].default_value = 0.92
    ramp.inputs["To Min"].default_value = 0.0
    ramp.inputs["To Max"].default_value = 2.4
    ramp.clamp = True

    bsdf.inputs["Emission Color"].default_value = (0.45, 0.82, 1.0, 1.0)
    links.new(lw.outputs["Facing"], ramp.inputs["Value"])
    links.new(ramp.outputs["Result"], bsdf.inputs["Emission Strength"])
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    return mat


def create_normal_curvature_material() -> bpy.types.Material:
    """
    Pass 04 // NORMAL & CURVATURE MAP + UV CHECKER GRID:
    Combines baked-style World/Camera Normal RGB shading with procedural UV/Generated technical checker lines
    and Pointiness curvature so it looks like a studio LookDev Normal + Texel Density pass.
    """
    name = "BS_Pass_NormalUV_Mat"
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links
    nodes.clear()

    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (560, 0)

    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (260, 0)
    bsdf.inputs["Roughness"].default_value = 0.32
    bsdf.inputs["Metallic"].default_value = 0.1

    geom = nodes.new("ShaderNodeNewGeometry")
    geom.location = (-520, 120)

    vec_mul = nodes.new("ShaderNodeVectorMath")
    vec_mul.operation = "MULTIPLY_ADD"
    vec_mul.location = (-300, 120)
    vec_mul.inputs[1].default_value = (0.5, 0.5, 0.5)
    vec_mul.inputs[2].default_value = (0.5, 0.5, 0.5)

    tex_coord = nodes.new("ShaderNodeTexCoord")
    tex_coord.location = (-520, -120)

    checker = nodes.new("ShaderNodeTexChecker")
    checker.location = (-300, -120)
    checker.inputs["Scale"].default_value = 24.0
    checker.inputs["Color1"].default_value = (0.82, 0.85, 0.92, 1.0)
    checker.inputs["Color2"].default_value = (0.52, 0.56, 0.68, 1.0)

    mix_norm_uv = nodes.new("ShaderNodeMix")
    mix_norm_uv.data_type = "RGBA"
    mix_norm_uv.blend_type = "MULTIPLY"
    mix_norm_uv.location = (-40, 0)
    mix_norm_uv.inputs["Factor"].default_value = 0.55

    links.new(geom.outputs["Normal"], vec_mul.inputs[0])
    links.new(tex_coord.outputs["Generated"], checker.inputs["Vector"])
    links.new(vec_mul.outputs["Vector"], mix_norm_uv.inputs["A"])
    links.new(checker.outputs["Color"], mix_norm_uv.inputs["B"])
    links.new(mix_norm_uv.outputs["Result"], bsdf.inputs["Base Color"])
    links.new( mix_norm_uv.outputs["Result"], bsdf.inputs["Emission Color"])
    bsdf.inputs["Emission Strength"].default_value = 0.28
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    return mat


def create_albedo_proxy_for_material(orig_mat: bpy.types.Material | None, idx: int = 0) -> bpy.types.Material:
    """
    Pass 05 // SHADER ALBEDO / DIFFUSE MATCAP:
    Extracts the original material's Base Color (or procedural color) and renders a clean,
    flat-roughness diffuse Albedo pass with zero specular glare.
    """
    base_col = (0.68, 0.64, 0.58, 1.0)
    img_node_src = None
    if orig_mat and orig_mat.use_nodes and orig_mat.node_tree:
        for n in orig_mat.node_tree.nodes:
            if n.type == "BSDF_PRINCIPLED":
                bc_in = n.inputs.get("Base Color")
                if bc_in:
                    base_col = tuple(bc_in.default_value)
                    if bc_in.is_linked:
                        from_node = bc_in.links[0].from_node
                        if from_node.type == "TEX_IMAGE" and from_node.image:
                            img_node_src = from_node.image
                break

    mat_name = f"BS_Pass_Albedo_{orig_mat.name if orig_mat else idx}"
    mat = bpy.data.materials.get(mat_name) or bpy.data.materials.new(mat_name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.inputs["Roughness"].default_value = 0.95
    bsdf.inputs["Metallic"].default_value = 0.0
    if "Specular IOR Level" in bsdf.inputs:
        bsdf.inputs["Specular IOR Level"].default_value = 0.05

    if img_node_src is not None:
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = img_node_src
        nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
        nt.links.new(tex.outputs["Color"], bsdf.inputs["Emission Color"])
    else:
        bsdf.inputs["Base Color"].default_value = base_col
        bsdf.inputs["Emission Color"].default_value = base_col
    bsdf.inputs["Emission Strength"].default_value = 0.18
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    return mat


WIREFRAME_STYLES = {
    "CYAN_SLATE": {
        "face_color": (0.09, 0.10, 0.13, 1.0),
        "edge_color": (0.35, 0.86, 1.0, 1.0),
        "emission_color": (0.28, 0.78, 0.98, 1.0),
        "emission_strength": 0.45,
        "roughness": 0.38,
    },
    "WHITE_TECH": {
        "face_color": (0.11, 0.115, 0.125, 1.0),
        "edge_color": (0.95, 0.96, 0.98, 1.0),
        "emission_color": (0.95, 0.96, 0.98, 1.0),
        "emission_strength": 0.35,
        "roughness": 0.42,
    },
    "GOLD_LUXURY": {
        "face_color": (0.06, 0.06, 0.07, 1.0),
        "edge_color": (1.0, 0.76, 0.36, 1.0),
        "emission_color": (0.98, 0.72, 0.28, 1.0),
        "emission_strength": 0.50,
        "roughness": 0.30,
    },
    "CLAY_WIRE": {
        "face_color": (0.52, 0.46, 0.40, 1.0),
        "edge_color": (0.08, 0.08, 0.09, 1.0),
        "emission_color": (0.0, 0.0, 0.0, 1.0),
        "emission_strength": 0.0,
        "roughness": 0.44,
    },
}


def create_quad_wire_materials(wire_style: str = "CYAN_SLATE") -> tuple[bpy.types.Material, bpy.types.Material]:
    """
    Creates a two-material pair `(face_mat, edge_mat)` for true quad-preserving
    Wireframe modifier rendering (no diagonal triangulation lines across quads).
    """
    style = WIREFRAME_STYLES.get(wire_style, WIREFRAME_STYLES["CYAN_SLATE"])

    face_name = "BS_Pass_QuadFace_Mat"
    face_mat = bpy.data.materials.get(face_name) or bpy.data.materials.new(face_name)
    face_mat.use_nodes = True
    f_nt = face_mat.node_tree
    f_nt.nodes.clear()
    f_out = f_nt.nodes.new("ShaderNodeOutputMaterial")
    f_bsdf = f_nt.nodes.new("ShaderNodeBsdfPrincipled")
    f_bsdf.inputs["Base Color"].default_value = style["face_color"]
    f_bsdf.inputs["Roughness"].default_value = style["roughness"]
    f_bsdf.inputs["Metallic"].default_value = 0.12 if wire_style != "CLAY_WIRE" else 0.0
    f_nt.links.new(f_bsdf.outputs["BSDF"], f_out.inputs["Surface"])

    edge_name = "BS_Pass_QuadEdge_Mat"
    edge_mat = bpy.data.materials.get(edge_name) or bpy.data.materials.new(edge_name)
    edge_mat.use_nodes = True
    e_nt = edge_mat.node_tree
    e_nt.nodes.clear()
    e_out = e_nt.nodes.new("ShaderNodeOutputMaterial")
    e_bsdf = e_nt.nodes.new("ShaderNodeBsdfPrincipled")
    e_bsdf.inputs["Base Color"].default_value = style["edge_color"]
    e_bsdf.inputs["Roughness"].default_value = 0.25
    e_bsdf.inputs["Emission Color"].default_value = style["emission_color"]
    e_bsdf.inputs["Emission Strength"].default_value = style["emission_strength"]
    e_nt.links.new(e_bsdf.outputs["BSDF"], e_out.inputs["Surface"])

    return face_mat, edge_mat


def create_wireframe_material(wire_px: float = 0.95, wire_style: str = "CYAN_SLATE") -> bpy.types.Material:
    """Creates a shader-based wireframe material (used for triangulated/dense mesh mode)."""
    style = WIREFRAME_STYLES.get(wire_style, WIREFRAME_STYLES["CYAN_SLATE"])
    name = "BS_Pass_Wireframe_Mat"
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links
    nodes.clear()

    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (520, 0)

    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (180, 0)
    bsdf.inputs["Base Color"].default_value = style["face_color"]
    bsdf.inputs["Roughness"].default_value = style["roughness"]
    bsdf.inputs["Metallic"].default_value = 0.15 if wire_style != "CLAY_WIRE" else 0.0

    wire = nodes.new("ShaderNodeWireframe")
    wire.location = (-260, -120)
    wire.use_pixel_size = True
    wire.inputs["Size"].default_value = wire_px

    em_math = nodes.new("ShaderNodeMath")
    em_math.operation = "MULTIPLY"
    em_math.location = (-40, -140)
    em_math.inputs[1].default_value = style["emission_strength"]

    mix_col = nodes.new("ShaderNodeMix")
    mix_col.data_type = "RGBA"
    mix_col.location = (-20, 40)
    mix_col.inputs["A"].default_value = style["face_color"]
    mix_col.inputs["B"].default_value = style["edge_color"]

    links.new(wire.outputs["Fac"], mix_col.inputs["Factor"])
    links.new(mix_col.outputs["Result"], bsdf.inputs["Base Color"])
    links.new(wire.outputs["Fac"], em_math.inputs[0])
    links.new(em_math.outputs["Value"], bsdf.inputs["Emission Strength"])
    bsdf.inputs["Emission Color"].default_value = style["emission_color"]
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    return mat


def snapshot_original_materials(meshes: list[bpy.types.Object]) -> dict:
    """
    Saves the exact original material slots (including empty slot count) and exact
    Subsurf/Multires modifier levels so restoration is 100% lossless.
    """
    store = {}
    for obj in meshes:
        orig_slot_count = len(obj.material_slots)
        orig_mats = [slot.material for slot in obj.material_slots]
        mod_levels = {}
        for m in obj.modifiers:
            if m.type in ("SUBSURF", "MULTIRES") and hasattr(m, "render_levels"):
                mod_levels[m.name] = (int(m.levels), int(m.render_levels))
        store[obj.name] = {
            "slot_count": orig_slot_count,
            "mats": orig_mats,
            "mods": mod_levels,
        }
    return store


def _cleanup_temp_quad_wire_modifier(obj: bpy.types.Object):
    mod = obj.modifiers.get("BS_Temp_QuadWire")
    if mod is not None:
        obj.modifiers.remove(mod)


def apply_pass_materials(
    pass_mode: str,
    meshes: list[bpy.types.Object],
    original_mats: dict,
    wire_style: str = "CYAN_SLATE",
    use_quad_wire: bool = True,
):
    """
    Switches the scene between:
    - 'WIREFRAME': True Quad-Topology Wireframe (via temporary Wireframe modifier) or shader wireframe
    - 'CLAY': Sculptor's AO clay shader (preserving glass lenses)
    - 'FINAL': Restores 100% original PBR materials, slot counts, and exact Subsurf levels
    """
    total_verts = sum(len(obj.data.vertices) for obj in meshes if obj.data)
    wire_px = 0.52 if total_verts > 80000 else (0.75 if total_verts > 25000 else 1.05)
    wire_shader_mat = create_wireframe_material(wire_px=wire_px, wire_style=wire_style)
    quad_face_mat, quad_edge_mat = create_quad_wire_materials(wire_style=wire_style)
    clay_mat = create_clay_material()
    bounds = compute_scene_bounds(meshes)
    base_thickness = max(0.00025, bounds["radius"] * 0.0018)

    for obj in meshes:
        _cleanup_temp_quad_wire_modifier(obj)

        entry = original_mats.get(obj.name, {})
        if isinstance(entry, list):
            orig_list = entry
            orig_slot_count = len(orig_list)
            mod_levels = {}
        else:
            orig_list = entry.get("mats", [])
            orig_slot_count = entry.get("slot_count", len(orig_list))
            mod_levels = entry.get("mods", {})

        n_verts = len(obj.data.vertices) if obj.data else 0
        for m in obj.modifiers:
            if m.name in mod_levels and hasattr(m, "render_levels"):
                orig_vp, orig_rnd = mod_levels[m.name]
                if pass_mode == "WIREFRAME":
                    target_lvl = 0 if n_verts >= 300 else 1
                    m.levels = min(orig_vp, target_lvl)
                    m.render_levels = min(orig_rnd, target_lvl)
                else:
                    # Restore exact user subdivision levels without capping at 2
                    m.levels = orig_vp
                    m.render_levels = orig_rnd

        if pass_mode == "FINAL":
            # Trim any temporary extra slots added during wireframe/clay passes
            while len(obj.data.materials) > orig_slot_count:
                obj.data.materials.pop()
            while len(obj.data.materials) < orig_slot_count:
                obj.data.materials.append(None)
            for idx, mat in enumerate(orig_list):
                if idx < len(obj.material_slots):
                    obj.material_slots[idx].material = mat

        elif pass_mode == "SILHOUETTE":
            sil_mat = create_silhouette_rim_material()
            if len(obj.material_slots) == 0:
                obj.data.materials.append(sil_mat)
            else:
                while len(obj.data.materials) > max(1, orig_slot_count):
                    obj.data.materials.pop()
                for idx in range(len(obj.material_slots)):
                    obj.material_slots[idx].material = sil_mat

        elif pass_mode == "CLAY":
            if len(obj.material_slots) == 0:
                obj.data.materials.append(clay_mat)
            else:
                while len(obj.data.materials) > max(1, orig_slot_count):
                    obj.data.materials.pop()
                for idx in range(len(obj.material_slots)):
                    mat = orig_list[idx] if idx < len(orig_list) else None
                    if mat and "lens" in mat.name.lower() and "body" not in mat.name.lower():
                        obj.material_slots[idx].material = mat
                    else:
                        obj.material_slots[idx].material = clay_mat

        elif pass_mode == "UV_NORMAL":
            norm_mat = create_normal_curvature_material()
            if len(obj.material_slots) == 0:
                obj.data.materials.append(norm_mat)
            else:
                while len(obj.data.materials) > max(1, orig_slot_count):
                    obj.data.materials.pop()
                for idx in range(len(obj.material_slots)):
                    obj.material_slots[idx].material = norm_mat

        elif pass_mode == "ALBEDO":
            if len(obj.material_slots) == 0:
                obj.data.materials.append(create_albedo_proxy_for_material(None, 0))
            else:
                while len(obj.data.materials) > max(1, orig_slot_count):
                    obj.data.materials.pop()
                for idx in range(len(obj.material_slots)):
                    orig_m = orig_list[idx] if idx < len(orig_list) else None
                    obj.material_slots[idx].material = create_albedo_proxy_for_material(orig_m, idx)

        elif pass_mode == "WIREFRAME":
            # Use geometry Wireframe modifier only when total scene vertices < 90k; otherwise use zero-overhead ShaderNodeWireframe
            if use_quad_wire and total_verts < 90000 and n_verts < 50000:
                while len(obj.data.materials) < 2:
                    obj.data.materials.append(None)
                for idx in range(len(obj.material_slots) - 1):
                    obj.material_slots[idx].material = quad_face_mat
                edge_slot_idx = len(obj.material_slots) - 1
                obj.material_slots[edge_slot_idx].material = quad_edge_mat

                wmod = obj.modifiers.new(name="BS_Temp_QuadWire", type="WIREFRAME")
                wmod.use_replace = False
                wmod.use_even_offset = False
                wmod.use_relative_offset = False
                wmod.use_boundary = True
                scale_max = max(obj.scale.x, obj.scale.y, obj.scale.z, 0.001)
                wmod.thickness = base_thickness / scale_max
                wmod.material_offset = edge_slot_idx
            else:
                if len(obj.material_slots) == 0:
                    obj.data.materials.append(wire_shader_mat)
                else:
                    for idx in range(len(obj.material_slots)):
                        obj.material_slots[idx].material = wire_shader_mat


# ==============================================================================
# BREAKDOWN MAKER++ 3D SCENE ASSEMBLY ANIMATION ENGINE (6 STUDIO PRESETS)
# ==============================================================================

def decompose_multi_material_meshes_for_breakdown(meshes: list[bpy.types.Object]) -> list[bpy.types.Object]:
    """
    If a scene has relatively few mesh objects (e.g., <= 24) and some meshes contain
    multiple distinct material slots (such as a Sneaker with Sole, Midsole, Upper, Lining
    joined into 1 mesh, or a Camera with Body, Lens Barrel, Glass, Dials joined into 1 mesh),
    separates multi-material meshes by material so each real physical component (Sole,
    Upper, Tongue, Swoosh, Laces, Lens, Body) animates independently in 3D space!
    """
    valid = [o for o in meshes if o and o.name in bpy.data.objects and o.type == "MESH"]
    if not valid or len(valid) > 24:
        return valid

    need_split = [o for o in valid if len(o.material_slots) > 1 and len(o.data.polygons) > 10]
    if not need_split:
        return valid

    try:
        bpy.ops.object.mode_set(mode="OBJECT")
    except Exception:
        pass

    for obj in need_split:
        try:
            bpy.ops.object.select_all(action="DESELECT")
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj
            bpy.ops.mesh.separate(type="MATERIAL")
        except Exception:
            pass

    return get_subject_meshes(hide_helpers=True)


def _stabilize_mirror_modifiers(meshes: list[bpy.types.Object]):
    """
    Many hard-surface models (e.g., HardOps / Boxcutter mechs like Percival) use another
    subject mesh as a `mirror_object`. If that reference mesh is animated (scaled to 0 or
    moved), all 170+ mirrored parts explode!
    This function creates a static Empty at the exact `matrix_world` of any referenced
    `mirror_object` and re-targets the Mirror modifier to the static Empty during the build.
    """
    ref_map: dict[str, bpy.types.Object] = {}
    for obj in meshes:
        for mod in obj.modifiers:
            if mod.type == "MIRROR" and getattr(mod, "mirror_object", None) is not None:
                m_obj = mod.mirror_object
                if m_obj.name.startswith("BS_Studio_MirrorRef_"):
                    continue
                helper_name = f"BS_Studio_MirrorRef_{m_obj.name}"
                helper = bpy.data.objects.get(helper_name)
                if helper is None:
                    helper = bpy.data.objects.new(helper_name, None)
                    bpy.context.scene.collection.objects.link(helper)
                    helper.empty_display_size = 0.01
                    helper.hide_viewport = True
                    helper.hide_render = True
                helper.matrix_world = m_obj.matrix_world.copy()
                obj[f"_bs_orig_mirror_{mod.name}"] = m_obj.name
                mod.mirror_object = helper
                ref_map[m_obj.name] = helper


def _restore_mirror_modifiers(meshes: list[bpy.types.Object]):
    for obj in meshes:
        if not obj or obj.name not in bpy.data.objects:
            continue
        for mod in obj.modifiers:
            key = f"_bs_orig_mirror_{mod.name}"
            if mod.type == "MIRROR" and key in obj:
                orig_name = obj[key]
                orig_obj = bpy.data.objects.get(orig_name)
                if orig_obj is not None:
                    mod.mirror_object = orig_obj
                del obj[key]
    for o in list(bpy.data.objects):
        if o.name.startswith("BS_Studio_MirrorRef_"):
            bpy.data.objects.remove(o, do_unlink=True)


def _group_meshes_for_breakdown(
    meshes: list[bpy.types.Object],
    max_groups: int = 28,
    build_direction: str = "BOTTOM_UP",
) -> list[list[bpy.types.Object]]:
    """
    Smartly orders scene meshes into up to `max_groups` fine-grained progressive waves
    according to `build_direction`:
      - `BOTTOM_UP`       : Bottom-to-top (+Z) foundation upward
      - `TOP_DOWN`        : Top-to-bottom (-Z) crown downward
      - `SIDE_LEFT_RIGHT` : Sideways Left-to-Right (+X)
      - `SIDE_RIGHT_LEFT` : Sideways Right-to-Left (-X)
      - `FRONT_TO_BACK`   : Front-to-Back (+Y depth)
      - `INSIDE_OUT`      : Core inside-to-out (3D radial distance from center)
      - `OUTSIDE_IN`      : Outer shell inward to core
    """
    import re
    if not meshes:
        return []
    if len(meshes) == 1:
        return [meshes]

    bounds = compute_scene_bounds(meshes)
    scene_center = bounds["center"]

    scored_items = []
    for obj in meshes:
        pts = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
        xs = [p.x for p in pts]
        ys = [p.y for p in pts]
        zs = [p.z for p in pts]
        x_center = (min(xs) + max(xs)) * 0.5 if xs else 0.0
        y_center = (min(ys) + max(ys)) * 0.5 if ys else 0.0
        z_min = min(zs) if zs else 0.0
        z_center = (min(zs) + max(zs)) * 0.5 if zs else 0.0
        x_rad = (min( abs(x) for x in xs ) + max( abs(x) for x in xs )) * 0.5 if xs else 0.0
        rad_dist = (Vector((x_center, y_center, z_center)) - scene_center).length

        mat_name = obj.material_slots[0].material.name if (obj.material_slots and obj.material_slots[0].material) else ""
        low_tag = (obj.name + " " + mat_name).lower()
        priority_bias = 0.0
        if any(k in low_tag for k in ("sole", "outsole", "foot", "base", "floor", "pedestal")):
            priority_bias = -10.0
        elif any(k in low_tag for k in ("insole", "lower leg", "ankle")):
            priority_bias = -5.0
        elif any(k in low_tag for k in ("main body", "upper leg", "lower body")):
            priority_bias = -2.0
        elif any(k in low_tag for k in ("inside", "flap", "tongue")):
            priority_bias = 2.0
        elif any(k in low_tag for k in ("logo", "swoosh", "tag", "shoulder", "hand")):
            priority_bias = 5.0
        elif any(k in low_tag for k in ("lace", "head", "helmet", "strap", "lens")):
            priority_bias = 9.0

        if build_direction == "TOP_DOWN":
            sort_primary = (-round(z_center, 2), -round(z_min, 2), -priority_bias)
            pair_lr = True
        elif build_direction == "SIDE_LEFT_RIGHT":
            sort_primary = (round(x_center, 2), round(z_min, 2), round(y_center, 2))
            pair_lr = False
        elif build_direction == "SIDE_RIGHT_LEFT":
            sort_primary = (-round(x_center, 2), round(z_min, 2), round(y_center, 2))
            pair_lr = False
        elif build_direction == "FRONT_TO_BACK":
            sort_primary = (round(y_center, 2), round(z_min, 2), round(x_rad, 2))
            pair_lr = True
        elif build_direction == "INSIDE_OUT":
            sort_primary = (round(rad_dist, 2), round(z_min, 2), priority_bias)
            pair_lr = True
        elif build_direction == "OUTSIDE_IN":
            sort_primary = (-round(rad_dist, 2), -round(z_min, 2), -priority_bias)
            pair_lr = True
        else:  # BOTTOM_UP (Default)
            sort_primary = (priority_bias, round(z_min, 2), round(z_center, 2), round(x_rad, 2))
            pair_lr = True

        clean = re.sub(r"\b(left|right|l|r)\b", "", obj.name, flags=re.IGNORECASE).strip(" ._-") if pair_lr else obj.name
        sym_key = f"{sort_primary}_{mat_name}_{clean}"
        scored_items.append((sort_primary, sym_key, obj))

    scored_items.sort(key=lambda item: (item[0], item[1]))

    sym_buckets: list[list[bpy.types.Object]] = []
    cur_key = None
    for item in scored_items:
        s_key = (item[0], item[1])
        if cur_key == s_key and sym_buckets:
            sym_buckets[-1].append(item[2])
        else:
            sym_buckets.append([item[2]])
            cur_key = s_key

    if len(sym_buckets) > max_groups:
        merged = []
        chunk = len(sym_buckets) / float(max_groups)
        for i in range(max_groups):
            i0 = int(round(i * chunk))
            i1 = int(round((i + 1) * chunk))
            bucket = []
            for sub in sym_buckets[i0:i1]:
                bucket.extend(sub)
            if bucket:
                merged.append(bucket)
        return merged

    return sym_buckets


def _unparent_subject_hierarchy(meshes: list[bpy.types.Object]):
    """
    Temporarily unparents subject meshes that are parented to other subject meshes
    (while preserving their exact `matrix_world`) so that animating a parent mesh's
    `delta_scale` and `delta_location` (e.g., `Nike Air Force Left`) NEVER double-scales,
    drags, or warps its child meshes (e.g., `Laces`, `Swoosh`, `Shoe Flap`)!
    Restored 100% losslessly by `_restore_subject_hierarchy`.
    """
    mesh_set = {o.name for o in meshes if o and o.name in bpy.data.objects}
    # Snapshot world matrices first before detaching any parent
    world_mats = {o.name: o.matrix_world.copy() for o in meshes if o and o.name in bpy.data.objects}
    for obj in meshes:
        if not obj or obj.name not in bpy.data.objects:
            continue
        if obj.parent is not None and (obj.parent.name in mesh_set or obj.parent.type in ("MESH", "EMPTY")):
            if "_bs_orig_parent_name" not in obj:
                obj["_bs_orig_parent_name"] = obj.parent.name
                obj["_bs_orig_parent_type"] = obj.parent_type
                obj["_bs_orig_parent_bone"] = obj.parent_bone or ""
                obj["_bs_orig_local_loc"] = list(obj.location)
                obj["_bs_orig_local_rot"] = list(obj.rotation_euler)
                obj["_bs_orig_local_scale"] = list(obj.scale)
                obj["_bs_orig_mpi"] = [list(row) for row in obj.matrix_parent_inverse]
            obj.parent = None
            obj.matrix_world = world_mats[obj.name]


def _restore_subject_hierarchy(meshes: list[bpy.types.Object]):
    from mathutils import Matrix
    for obj in meshes:
        if not obj or obj.name not in bpy.data.objects:
            continue
        if "_bs_orig_parent_name" in obj:
            p_name = obj["_bs_orig_parent_name"]
            p_obj = bpy.data.objects.get(p_name)
            if p_obj is not None:
                obj.parent = p_obj
                obj.parent_type = obj.get("_bs_orig_parent_type", "OBJECT")
                if obj.get("_bs_orig_parent_bone"):
                    obj.parent_bone = obj["_bs_orig_parent_bone"]
                if "_bs_orig_mpi" in obj:
                    obj.matrix_parent_inverse = Matrix(obj["_bs_orig_mpi"])
            if "_bs_orig_local_loc" in obj:
                obj.location = Vector(obj["_bs_orig_local_loc"])
            if "_bs_orig_local_rot" in obj:
                obj.rotation_euler = tuple(obj["_bs_orig_local_rot"])
            if "_bs_orig_local_scale" in obj:
                obj.scale = Vector(obj["_bs_orig_local_scale"])
            for k in (
                "_bs_orig_parent_name",
                "_bs_orig_parent_type",
                "_bs_orig_parent_bone",
                "_bs_orig_local_loc",
                "_bs_orig_local_rot",
                "_bs_orig_local_scale",
                "_bs_orig_mpi",
            ):
                if k in obj:
                    del obj[k]


def _build_live_3d_breakdown_material(
    orig_mat: bpy.types.Material | None,
    slot_idx: int,
    ctrl_obj: bpy.types.Object,
    bounds: dict,
    effective_dir: str = "BOTTOM_UP",
    wire_px: float = 0.85,
    wire_style: str = "CYAN_SLATE",
) -> bpy.types.Material:
    """
    Wraps `orig_mat` non-destructively into a real-time 3D Breakdown Shader (`BS_Pass_Live_*`)
    that smoothly sweeps:
      Stage 1: Blueprint Wireframe
         |  (Glowing Cyan 3D Laser Ring)
      Stage 2: Sculptor's Clay + Cavity AO
         |  (Glowing Warm Gold 3D Laser Ring)
      Stage 3: 100% Exact Original Final PBR Material
    across the 3D model in World Space (`Bottom-Up`, `Top-Down`, `Sideways`, or `Radial`)
    at 60 FPS in the Blender 3D Viewport!
    """
    base_key = orig_mat.name if orig_mat else f"DefaultSlot_{slot_idx}"
    live_name = f"BS_Pass_Live_{base_key}"
    old_live = bpy.data.materials.get(live_name)
    if old_live is not None:
        bpy.data.materials.remove(old_live, do_unlink=True)

    if orig_mat is not None and orig_mat.use_nodes and orig_mat.node_tree:
        live_mat = orig_mat.copy()
        live_mat.name = live_name
    else:
        live_mat = bpy.data.materials.new(live_name)
        live_mat.use_nodes = True

    nt = live_mat.node_tree
    nodes = nt.nodes
    links = nt.links

    out_node = None
    for n in nodes:
        if n.type == "OUTPUT_MATERIAL" and n.is_active_output:
            out_node = n
            break
    if out_node is None:
        for n in nodes:
            if n.type == "OUTPUT_MATERIAL":
                out_node = n
                break
    if out_node is None:
        out_node = nodes.new("ShaderNodeOutputMaterial")

    final_socket = None
    if out_node.inputs["Surface"].is_linked:
        final_socket = out_node.inputs["Surface"].links[0].from_socket
    if final_socket is None:
        fallback_bsdf = nodes.new("ShaderNodeBsdfPrincipled")
        if orig_mat is not None:
            fallback_bsdf.inputs["Base Color"].default_value = tuple(orig_mat.diffuse_color)
        else:
            fallback_bsdf.inputs["Base Color"].default_value = (0.72, 0.74, 0.78, 1.0)
        fallback_bsdf.inputs["Roughness"].default_value = 0.32
        final_socket = fallback_bsdf.outputs["BSDF"]

    style = WIREFRAME_STYLES.get(wire_style, WIREFRAME_STYLES["CYAN_SLATE"])

    # 1. Stage 1: Blueprint Wireframe BSDF (world-space thickness scaled by model radius for crisp lines at any density)
    wire_node = nodes.new("ShaderNodeWireframe")
    wire_node.use_pixel_size = False
    wire_node.inputs["Size"].default_value = max(0.00015, float(bounds["radius"]) * wire_px * 0.0016)

    w_mix = nodes.new("ShaderNodeMix")
    w_mix.data_type = "RGBA"
    w_mix.inputs["A"].default_value = style["face_color"]
    w_mix.inputs["B"].default_value = style["edge_color"]
    links.new(wire_node.outputs["Fac"], w_mix.inputs["Factor"])

    w_em = nodes.new("ShaderNodeMath")
    w_em.operation = "MULTIPLY"
    w_em.inputs[1].default_value = style["emission_strength"]
    links.new(wire_node.outputs["Fac"], w_em.inputs[0])

    w_bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    w_bsdf.inputs["Roughness"].default_value = style["roughness"]
    w_bsdf.inputs["Metallic"].default_value = 0.15
    w_bsdf.inputs["Emission Color"].default_value = style["emission_color"]
    links.new(w_mix.outputs["Result"], w_bsdf.inputs["Base Color"])
    links.new(w_em.outputs["Value"], w_bsdf.inputs["Emission Strength"])

    # 2. Stage 2: Sculptor's Clay + Cavity BSDF
    c_bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    c_bsdf.inputs["Base Color"].default_value = (0.55, 0.48, 0.42, 1.0)
    c_bsdf.inputs["Roughness"].default_value = 0.36
    if "Specular IOR Level" in c_bsdf.inputs:
        c_bsdf.inputs["Specular IOR Level"].default_value = 0.38
    if "Coat Weight" in c_bsdf.inputs:
        c_bsdf.inputs["Coat Weight"].default_value = 0.12
        c_bsdf.inputs["Coat Roughness"].default_value = 0.22

    # 3. 3D World Coordinate Field u in [0, 1] along `effective_dir`
    geom = nodes.new("ShaderNodeNewGeometry")
    u_map = nodes.new("ShaderNodeMapRange")
    u_map.clamp = False
    u_map.inputs["To Min"].default_value = 0.0
    u_map.inputs["To Max"].default_value = 1.0

    vmin = bounds["min"]
    vmax = bounds["max"]
    center = bounds["center"]
    rad = max(0.05, float(bounds["radius"]))

    if effective_dir in ("INSIDE_OUT", "OUTSIDE_IN", "RADIAL"):
        dist_node = nodes.new("ShaderNodeVectorMath")
        dist_node.operation = "DISTANCE"
        dist_node.inputs[1].default_value = (float(center.x), float(center.y), float(center.z))
        links.new(geom.outputs["Position"], dist_node.inputs[0])
        u_map.inputs["From Min"].default_value = 0.0
        u_map.inputs["From Max"].default_value = rad
        links.new(dist_node.outputs["Value"], u_map.inputs["Value"])
    else:
        sep_pos = nodes.new("ShaderNodeSeparateXYZ")
        links.new(geom.outputs["Position"], sep_pos.inputs["Vector"])
        if effective_dir == "TOP_DOWN":
            span = max(0.01, float(vmax.z - vmin.z))
            u_map.inputs["From Min"].default_value = float(vmax.z)
            u_map.inputs["From Max"].default_value = float(vmax.z - span)
            links.new(sep_pos.outputs["Z"], u_map.inputs["Value"])
        elif effective_dir in ("SIDE_LEFT_RIGHT", "SIDEWAYS"):
            span = max(0.01, float(vmax.x - vmin.x))
            u_map.inputs["From Min"].default_value = float(vmin.x)
            u_map.inputs["From Max"].default_value = float(vmin.x + span)
            links.new(sep_pos.outputs["X"], u_map.inputs["Value"])
        elif effective_dir == "SIDE_RIGHT_LEFT":
            span = max(0.01, float(vmax.x - vmin.x))
            u_map.inputs["From Min"].default_value = float(vmax.x)
            u_map.inputs["From Max"].default_value = float(vmax.x - span)
            links.new(sep_pos.outputs["X"], u_map.inputs["Value"])
        else:  # BOTTOM_UP
            span = max(0.01, float(vmax.z - vmin.z))
            u_map.inputs["From Min"].default_value = float(vmin.z)
            u_map.inputs["From Max"].default_value = float(vmin.z + span)
            links.new(sep_pos.outputs["Z"], u_map.inputs["Value"])

    # 4. Read hardware-accelerated animated wave fronts (w1, w2) from `ctrl_obj`
    tc = nodes.new("ShaderNodeTexCoord")
    tc.object = ctrl_obj
    v_sub = nodes.new("ShaderNodeVectorMath")
    v_sub.operation = "SUBTRACT"
    links.new(geom.outputs["Position"], v_sub.inputs[0])
    links.new(tc.outputs["Object"], v_sub.inputs[1])
    sep_ctrl = nodes.new("ShaderNodeSeparateXYZ")
    links.new(v_sub.outputs["Vector"], sep_ctrl.inputs["Vector"])

    # 5. Wave 1 (Wire -> Clay) & Wave 2 (Clay -> Final) transitions + 3D Laser Rings
    sub_d1 = nodes.new("ShaderNodeMath")
    sub_d1.operation = "SUBTRACT"
    links.new(u_map.outputs["Result"], sub_d1.inputs[0])
    links.new(sep_ctrl.outputs["X"], sub_d1.inputs[1])

    fac1 = nodes.new("ShaderNodeMapRange")
    fac1.clamp = True
    fac1.inputs["From Min"].default_value = 0.018
    fac1.inputs["From Max"].default_value = -0.018
    fac1.inputs["To Min"].default_value = 0.0
    fac1.inputs["To Max"].default_value = 1.0
    links.new(sub_d1.outputs["Value"], fac1.inputs["Value"])

    abs_d1 = nodes.new("ShaderNodeMath")
    abs_d1.operation = "ABSOLUTE"
    links.new(sub_d1.outputs["Value"], abs_d1.inputs[0])

    las1 = nodes.new("ShaderNodeMapRange")
    las1.clamp = True
    las1.inputs["From Min"].default_value = 0.0
    las1.inputs["From Max"].default_value = 0.013
    las1.inputs["To Min"].default_value = 1.0
    las1.inputs["To Max"].default_value = 0.0
    links.new(abs_d1.outputs["Value"], las1.inputs["Value"])

    sub_d2 = nodes.new("ShaderNodeMath")
    sub_d2.operation = "SUBTRACT"
    links.new(u_map.outputs["Result"], sub_d2.inputs[0])
    links.new(sep_ctrl.outputs["Y"], sub_d2.inputs[1])

    fac2 = nodes.new("ShaderNodeMapRange")
    fac2.clamp = True
    fac2.inputs["From Min"].default_value = 0.018
    fac2.inputs["From Max"].default_value = -0.018
    fac2.inputs["To Min"].default_value = 0.0
    fac2.inputs["To Max"].default_value = 1.0
    links.new(sub_d2.outputs["Value"], fac2.inputs["Value"])

    abs_d2 = nodes.new("ShaderNodeMath")
    abs_d2.operation = "ABSOLUTE"
    links.new(sub_d2.outputs["Value"], abs_d2.inputs[0])

    las2 = nodes.new("ShaderNodeMapRange")
    las2.clamp = True
    las2.inputs["From Min"].default_value = 0.0
    las2.inputs["From Max"].default_value = 0.013
    las2.inputs["To Min"].default_value = 1.0
    las2.inputs["To Max"].default_value = 0.0
    links.new(abs_d2.outputs["Value"], las2.inputs["Value"])

    # 6. Mix Shaders: Wireframe -> Clay -> Final PBR + Glowing Laser Contour Rings
    mix_sh1 = nodes.new("ShaderNodeMixShader")
    links.new(fac1.outputs["Result"], mix_sh1.inputs["Fac"])
    links.new(w_bsdf.outputs["BSDF"], mix_sh1.inputs[1])
    links.new(c_bsdf.outputs["BSDF"], mix_sh1.inputs[2])

    mix_sh2 = nodes.new("ShaderNodeMixShader")
    links.new(fac2.outputs["Result"], mix_sh2.inputs["Fac"])
    links.new(mix_sh1.outputs["Shader"], mix_sh2.inputs[1])
    links.new(final_socket, mix_sh2.inputs[2])

    em_cyan = nodes.new("ShaderNodeEmission")
    em_cyan.inputs["Color"].default_value = (0.20, 0.90, 1.0, 1.0)
    em_cyan.inputs["Strength"].default_value = 5.0

    em_gold = nodes.new("ShaderNodeEmission")
    em_gold.inputs["Color"].default_value = (1.0, 0.72, 0.22, 1.0)
    em_gold.inputs["Strength"].default_value = 5.0

    mix_las1 = nodes.new("ShaderNodeMixShader")
    links.new(las1.outputs["Result"], mix_las1.inputs["Fac"])
    links.new(mix_sh2.outputs["Shader"], mix_las1.inputs[1])
    links.new(em_cyan.outputs["Emission"], mix_las1.inputs[2])

    mix_las2 = nodes.new("ShaderNodeMixShader")
    links.new(las2.outputs["Result"], mix_las2.inputs["Fac"])
    links.new(mix_las1.outputs["Shader"], mix_las2.inputs[1])
    links.new(em_gold.outputs["Emission"], mix_las2.inputs[2])

    links.new(mix_las2.outputs["Shader"], out_node.inputs["Surface"])
    return live_mat


def clear_breakdown_assembly_animation(
    meshes: list[bpy.types.Object] | None = None,
    original_mats: dict | None = None,
):
    """
    Removes all temporary Breakdown keyframes/controllers and restores exact
    original transforms, parent hierarchy, mirror modifiers, and original materials.
    """
    if meshes is None:
        meshes = get_subject_meshes()
    _restore_mirror_modifiers(meshes)
    for obj in meshes:
        if obj.animation_data and obj.animation_data.action:
            act = obj.animation_data.action
            if hasattr(act, "fcurves"):
                for fc in list(act.fcurves):
                    if fc.data_path in ("delta_location", "delta_scale", "delta_rotation_euler"):
                        act.fcurves.remove(fc)
        if "_bs_orig_delta_loc" in obj:
            obj.delta_location = Vector(obj["_bs_orig_delta_loc"])
            del obj["_bs_orig_delta_loc"]
        else:
            obj.delta_location = (0.0, 0.0, 0.0)
        if "_bs_orig_delta_scale" in obj:
            obj.delta_scale = Vector(obj["_bs_orig_delta_scale"])
            del obj["_bs_orig_delta_scale"]
        else:
            obj.delta_scale = (1.0, 1.0, 1.0)
        if "_bs_orig_delta_rot" in obj:
            obj.delta_rotation_euler = tuple(obj["_bs_orig_delta_rot"])
            del obj["_bs_orig_delta_rot"]
        else:
            obj.delta_rotation_euler = (0.0, 0.0, 0.0)
        if "_bs_live_active" in obj:
            del obj["_bs_live_active"]
    _restore_subject_hierarchy(meshes)

    if original_mats:
        apply_pass_materials("FINAL", meshes, original_mats)

    ctrl = bpy.data.objects.get("BS_Studio_Wipe_Ctrl")
    if ctrl is not None:
        bpy.data.objects.remove(ctrl, do_unlink=True)

    for mat in list(bpy.data.materials):
        if mat.name.startswith("BS_Pass_Live_"):
            bpy.data.materials.remove(mat, do_unlink=True)


def setup_breakdown_assembly_animation(
    meshes: list[bpy.types.Object],
    preset: str = "BOTTOM_UP",
    frame_start: int = 1,
    assembly_end_frame: int = 140,
    build_direction: str = "BOTTOM_UP",
    frames_per_part: int = 0,
    stagger_frames: int = 0,
    original_mats: dict | None = None,
    wire_style: str = "CYAN_SLATE",
    live_shader: bool = True,
):
    """
    Applies a 100% smooth, intact-model 3D Breakdown Sweep (`Wireframe -> Clay -> Final PBR`)
    across `meshes` along `build_direction` (`BOTTOM_UP`, `TOP_DOWN`, `SIDE_LEFT_RIGHT`,
    `INSIDE_OUT`) with ZERO mesh distortion, ZERO detached sub-parts, and ZERO floating crumbs!
    """
    clear_breakdown_assembly_animation(meshes, original_mats=original_mats)
    if not meshes or not live_shader:
        return

    effective_dir = build_direction
    if preset == "SIDEWAYS":
        effective_dir = "SIDE_LEFT_RIGHT"
    elif preset in ("RADIAL", "SLIDE_RADIAL"):
        effective_dir = "INSIDE_OUT"

    bounds = compute_scene_bounds(meshes)
    total_verts = sum(len(obj.data.vertices) for obj in meshes if obj.data)
    wire_px = 0.38 if total_verts > 150000 else (0.55 if total_verts > 60000 else (0.75 if total_verts > 20000 else 1.0))

    ctrl = bpy.data.objects.get("BS_Studio_Wipe_Ctrl")
    if ctrl is None:
        ctrl = bpy.data.objects.new("BS_Studio_Wipe_Ctrl", None)
        bpy.context.scene.collection.objects.link(ctrl)
    ctrl.empty_display_size = 0.01
    ctrl.hide_viewport = True
    ctrl.hide_render = True
    ctrl.rotation_euler = (0.0, 0.0, 0.0)
    ctrl.scale = (1.0, 1.0, 1.0)
    ctrl.animation_data_clear()

    total_frames = max(30, assembly_end_frame - frame_start)
    sweep_end_frame = frame_start + int(round(total_frames * 0.86))

    # Animate Wave 1 (location.x: Wire->Clay) and Wave 2 (location.y: Clay->Final)
    # separated by 0.42 so all 3 stages (Wireframe, Clay, Final PBR) are visible simultaneously!
    ctrl.location = (-0.06, -0.48, 0.0)
    ctrl.keyframe_insert(data_path="location", frame=frame_start)

    ctrl.location = (1.48, 1.06, 0.0)
    ctrl.keyframe_insert(data_path="location", frame=sweep_end_frame)

    ctrl.location = (1.52, 1.10, 0.0)
    ctrl.keyframe_insert(data_path="location", frame=assembly_end_frame)

    if ctrl.animation_data and ctrl.animation_data.action and hasattr(ctrl.animation_data.action, "fcurves"):
        for fc in ctrl.animation_data.action.fcurves:
            for kp in fc.keyframe_points:
                kp.interpolation = "LINEAR"
            fc.update()

    # Build and assign cached Live 3D Breakdown Materials per unique original material
    built_live_mats: dict[str, bpy.types.Material] = {}
    for obj in meshes:
        obj["_bs_live_active"] = True
        entry = (original_mats or {}).get(obj.name, {})
        orig_list = entry.get("mats", []) if isinstance(entry, dict) else (entry if isinstance(entry, list) else [])

        if len(obj.material_slots) == 0:
            key = "__NONE_SLOT_0__"
            if key not in built_live_mats:
                built_live_mats[key] = _build_live_3d_breakdown_material(
                    None, 0, ctrl, bounds, effective_dir=effective_dir, wire_px=wire_px, wire_style=wire_style
                )
            obj.data.materials.append(built_live_mats[key])
        else:
            for idx in range(len(obj.material_slots)):
                orig_m = orig_list[idx] if idx < len(orig_list) else obj.material_slots[idx].material
                if orig_m and orig_m.name.startswith("BS_Pass_"):
                    orig_m = None
                key = orig_m.name if orig_m else f"__NONE_SLOT_{idx}__"
                if key not in built_live_mats:
                    built_live_mats[key] = _build_live_3d_breakdown_material(
                        orig_m, idx, ctrl, bounds, effective_dir=effective_dir, wire_px=wire_px, wire_style=wire_style
                    )
                obj.material_slots[idx].material = built_live_mats[key]



