bl_info = {
    "name": "Custom Crop Render Regions",
    "author": "Codebuff",
    "version": (1, 2),
    "blender": (4, 0, 0),
    "location": "Render Properties > Custom Crop Regions",
    "description": "Precisely control render crop regions numerically. Set crop position and size, automatically scales render resolution to maintain target output size and aspect ratio. Includes interactive viewport border drawing with live overlay.",
    "warning": "",
    "doc_url": "",
    "category": "Render",
}

import bpy
import mathutils
from bpy.types import Panel, PropertyGroup, Operator
from bpy.props import FloatProperty, IntProperty, BoolProperty, StringProperty

# GPU drawing modules
import gpu
import blf
from gpu_extras.batch import batch_for_shader
from bpy_extras.view3d_utils import location_3d_to_region_2d
from bpy_extras.object_utils import world_to_camera_view

# Lazily created shader for GPU drawing. Creating it on demand is friendlier
# to Blender reloads and future GPU backend changes.
_SHADER_2D = None

# ---------------------------------------------------------------------------
# Module-level update guard — prevents infinite recursion when synced
# properties write back to each other.
# ---------------------------------------------------------------------------
_updating = False


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------

def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


def _simplify_ratio(w: int, h: int):
    """Return (numerator, denominator) of the ratio w:h in lowest terms."""
    if w <= 0 or h <= 0:
        return w, h
    d = _gcd(w, h)
    return w // d, h // d


def _get_2d_shader():
    """Return the cached 2D uniform-color shader."""
    global _SHADER_2D
    if _SHADER_2D is None:
        _SHADER_2D = gpu.shader.from_builtin('UNIFORM_COLOR')
    return _SHADER_2D


# ---------------------------------------------------------------------------
# Core update logic
# ---------------------------------------------------------------------------

def _clamp_crop(props) -> None:
    """Ensure the crop rectangle stays inside [0, 1] and has a minimum size."""
    props.norm_w = max(0.001, min(1.0, props.norm_w))
    props.norm_h = max(0.001, min(1.0, props.norm_h))
    # Slide position left/up if right/bottom edge overflows
    if props.norm_x + props.norm_w > 1.0:
        props.norm_x = 1.0 - props.norm_w
    if props.norm_y + props.norm_h > 1.0:
        props.norm_y = 1.0 - props.norm_h
    props.norm_x = max(0.0, props.norm_x)
    props.norm_y = max(0.0, props.norm_y)


def _sync_pixel_from_norm(context, props) -> None:
    """Write computed pixel values from the stored normalized values."""
    rx = max(1, context.scene.render.resolution_x)
    ry = max(1, context.scene.render.resolution_y)
    props.pixel_x = round(props.norm_x * rx)
    props.pixel_y = round(props.norm_y * ry)
    props.pixel_w = round(props.norm_w * rx)
    props.pixel_h = round(props.norm_h * ry)
    if not props.lock_aspect:
        props.target_w = props.pixel_w
        props.target_h = props.pixel_h


def _sync_norm_from_pixel(context, props) -> None:
    """Write normalized values from the stored pixel values."""
    rx = max(1, context.scene.render.resolution_x)
    ry = max(1, context.scene.render.resolution_y)
    props.norm_x = max(0.0, min(1.0, props.pixel_x / rx))
    props.norm_y = max(0.0, min(1.0, props.pixel_y / ry))
    props.norm_w = max(0.001, min(1.0, props.pixel_w / rx))
    props.norm_h = max(0.001, min(1.0, props.pixel_h / ry))
    if not props.lock_aspect:
        props.target_w = props.pixel_w
        props.target_h = props.pixel_h


def _capture_original_resolution(context, props) -> None:
    """Remember the user's render resolution before addon scaling starts."""
    if props.has_original_resolution:
        return
    rd = context.scene.render
    props.original_w = max(1, rd.resolution_x)
    props.original_h = max(1, rd.resolution_y)
    props.has_original_resolution = True


def _restore_original_resolution(context, props) -> None:
    """Restore the render resolution captured when the crop was enabled."""
    rd = context.scene.render
    if props.has_original_resolution:
        rd.resolution_x = max(1, props.original_w)
        rd.resolution_y = max(1, props.original_h)
        props.has_original_resolution = False


def _fit_crop_to_target_aspect(context, props) -> None:
    """Fit the current crop rectangle inside itself to match target aspect."""
    rx = props.original_w if props.has_original_resolution else max(1, context.scene.render.resolution_x)
    ry = props.original_h if props.has_original_resolution else max(1, context.scene.render.resolution_y)
    target_ratio = (props.target_w / max(1, props.target_h)) * (ry / rx)
    current_ratio = props.norm_w / max(0.001, props.norm_h)

    cx = props.norm_x + props.norm_w / 2.0
    cy = props.norm_y + props.norm_h / 2.0

    if current_ratio > target_ratio:
        props.norm_w = max(0.001, props.norm_h * target_ratio)
    else:
        props.norm_h = max(0.001, props.norm_w / max(0.001, target_ratio))

    props.norm_x = cx - props.norm_w / 2.0
    props.norm_y = cy - props.norm_h / 2.0
    _clamp_crop(props)


def _expand_crop_to_target_aspect(context, props) -> None:
    """Expand the crop around its center to match target aspect."""
    rx = props.original_w if props.has_original_resolution else max(1, context.scene.render.resolution_x)
    ry = props.original_h if props.has_original_resolution else max(1, context.scene.render.resolution_y)
    target_ratio = (props.target_w / max(1, props.target_h)) * (ry / rx)
    current_ratio = props.norm_w / max(0.001, props.norm_h)

    cx = props.norm_x + props.norm_w / 2.0
    cy = props.norm_y + props.norm_h / 2.0

    if current_ratio > target_ratio:
        props.norm_h = min(1.0, props.norm_w / max(0.001, target_ratio))
    else:
        props.norm_w = min(1.0, props.norm_h * target_ratio)

    props.norm_x = cx - props.norm_w / 2.0
    props.norm_y = cy - props.norm_h / 2.0
    _clamp_crop(props)


def _base_crop_pixel_size(context, props):
    """Return crop pixel size at the user's original/base render resolution."""
    rd = context.scene.render
    base_w = props.original_w if props.has_original_resolution else rd.resolution_x
    base_h = props.original_h if props.has_original_resolution else rd.resolution_y
    return (
        max(1, round(props.norm_w * max(1, base_w))),
        max(1, round(props.norm_h * max(1, base_h))),
    )


def _apply_to_render(context, props, *, scale_resolution=True) -> None:
    """Push the addon's crop settings into Blender's render settings."""
    rd = context.scene.render
    nw = max(0.001, min(1.0, props.norm_w))
    nh = max(0.001, min(1.0, props.norm_h))
    nx = max(0.0, min(1.0 - nw, props.norm_x))
    ny = max(0.0, min(1.0 - nh, props.norm_y))

    rd.border_min_x = nx
    rd.border_min_y = ny
    rd.border_max_x = nx + nw
    rd.border_max_y = ny + nh
    rd.use_border = props.enabled
    rd.use_crop_to_border = props.crop_to_border

    if scale_resolution and props.enabled and nw > 0.0 and nh > 0.0:
        rd.resolution_x = max(1, round(props.target_w / nw))
        rd.resolution_y = max(1, round(props.target_h / nh))

        # Sync pixel fields with the new scaled resolution to prevent UI mismatch
        global _updating
        old_updating = _updating
        _updating = True
        try:
            _sync_pixel_from_norm(context, props)
        finally:
            _updating = old_updating


# ---------------------------------------------------------------------------
# Per-property update callbacks
# ---------------------------------------------------------------------------

def _cap_update_norm_x(self, context):
    global _updating
    if _updating:
        return
    _updating = True
    try:
        p = context.scene.ccr_props
        _clamp_crop(p)
        _sync_pixel_from_norm(context, p)
        if p.enabled:
            _apply_to_render(context, p)
    finally:
        _updating = False


def _cap_update_norm_y(self, context):
    global _updating
    if _updating:
        return
    _updating = True
    try:
        p = context.scene.ccr_props
        _clamp_crop(p)
        _sync_pixel_from_norm(context, p)
        if p.enabled:
            _apply_to_render(context, p)
    finally:
        _updating = False


def _cap_update_norm_w(self, context):
    global _updating
    if _updating:
        return
    _updating = True
    try:
        p = context.scene.ccr_props
        if p.lock_aspect:
            rx = max(1, context.scene.render.resolution_x)
            ry = max(1, context.scene.render.resolution_y)
            p.norm_h = p.norm_w * (p.target_h / max(1, p.target_w)) * (rx / ry)
        _clamp_crop(p)
        _sync_pixel_from_norm(context, p)
        if p.enabled:
            _apply_to_render(context, p)
    finally:
        _updating = False


def _cap_update_norm_h(self, context):
    global _updating
    if _updating:
        return
    _updating = True
    try:
        p = context.scene.ccr_props
        if p.lock_aspect:
            rx = max(1, context.scene.render.resolution_x)
            ry = max(1, context.scene.render.resolution_y)
            p.norm_w = p.norm_h * (p.target_w / max(1, p.target_h)) * (ry / rx)
        _clamp_crop(p)
        _sync_pixel_from_norm(context, p)
        if p.enabled:
            _apply_to_render(context, p)
    finally:
        _updating = False


def _cap_update_pixel_x(self, context):
    global _updating
    if _updating:
        return
    _updating = True
    try:
        p = context.scene.ccr_props
        _sync_norm_from_pixel(context, p)
        _clamp_crop(p)
        _sync_pixel_from_norm(context, p)
        if p.enabled:
            _apply_to_render(context, p)
    finally:
        _updating = False


def _cap_update_pixel_y(self, context):
    global _updating
    if _updating:
        return
    _updating = True
    try:
        p = context.scene.ccr_props
        _sync_norm_from_pixel(context, p)
        _clamp_crop(p)
        _sync_pixel_from_norm(context, p)
        if p.enabled:
            _apply_to_render(context, p)
    finally:
        _updating = False


def _cap_update_pixel_w(self, context):
    global _updating
    if _updating:
        return
    _updating = True
    try:
        p = context.scene.ccr_props
        _sync_norm_from_pixel(context, p)
        if p.lock_aspect:
            rx = max(1, context.scene.render.resolution_x)
            ry = max(1, context.scene.render.resolution_y)
            p.norm_h = p.norm_w * (p.target_h / max(1, p.target_w)) * (rx / ry)
        _clamp_crop(p)
        _sync_pixel_from_norm(context, p)
        if p.enabled:
            _apply_to_render(context, p)
    finally:
        _updating = False


def _cap_update_pixel_h(self, context):
    global _updating
    if _updating:
        return
    _updating = True
    try:
        p = context.scene.ccr_props
        _sync_norm_from_pixel(context, p)
        if p.lock_aspect:
            rx = max(1, context.scene.render.resolution_x)
            ry = max(1, context.scene.render.resolution_y)
            p.norm_w = p.norm_h * (p.target_w / max(1, p.target_h)) * (ry / rx)
        _clamp_crop(p)
        _sync_pixel_from_norm(context, p)
        if p.enabled:
            _apply_to_render(context, p)
    finally:
        _updating = False


def _cap_update_enabled(self, context):
    global _updating
    if _updating:
        return
    p = context.scene.ccr_props
    rd = context.scene.render
    if p.enabled:
        _capture_original_resolution(context, p)
        _apply_to_render(context, p)
    else:
        rd.use_border = False
        _restore_original_resolution(context, p)


def _cap_update_crop_to_border(self, context):
    global _updating
    if _updating:
        return
    p = context.scene.ccr_props
    if p.enabled:
        context.scene.render.use_crop_to_border = p.crop_to_border


def _cap_update_target_w(self, context):
    global _updating
    if _updating:
        return
    _updating = True
    try:
        p = context.scene.ccr_props
        p.target_w = max(1, p.target_w)
        if p.enabled:
            _apply_to_render(context, p)
    finally:
        _updating = False


def _cap_update_target_h(self, context):
    global _updating
    if _updating:
        return
    _updating = True
    try:
        p = context.scene.ccr_props
        p.target_h = max(1, p.target_h)
        if p.enabled:
            _apply_to_render(context, p)
    finally:
        _updating = False


def _cap_update_lock_aspect(self, context):
    global _updating
    if _updating:
        return
    _updating = True
    try:
        p = context.scene.ccr_props
        if not p.lock_aspect:
            p.target_w = p.pixel_w
            p.target_h = p.pixel_h
        else:
            rx = max(1, context.scene.render.resolution_x)
            ry = max(1, context.scene.render.resolution_y)
            p.norm_h = p.norm_w * (p.target_h / max(1, p.target_w)) * (rx / ry)
            _clamp_crop(p)
            _sync_pixel_from_norm(context, p)
            if p.enabled:
                _apply_to_render(context, p)
    finally:
        _updating = False


# ---------------------------------------------------------------------------
# Custom PropertyGroup
# ---------------------------------------------------------------------------

class CCR_Properties(PropertyGroup):
    """Persistent addon settings stored per scene."""

    # -- Normalised crop (0..1) ------------------------------------------------
    norm_x: FloatProperty(
        name="X",
        description="Crop left edge (normalised 0\u20131)",
        default=0.0, min=0.0, max=1.0,
        subtype='FACTOR', precision=3,
        update=_cap_update_norm_x,
    )
    norm_y: FloatProperty(
        name="Y",
        description="Crop bottom edge (normalised 0\u20131)",
        default=0.0, min=0.0, max=1.0,
        subtype='FACTOR', precision=3,
        update=_cap_update_norm_y,
    )
    norm_w: FloatProperty(
        name="W",
        description="Crop width (normalised 0\u20131)",
        default=1.0, min=0.001, max=1.0,
        subtype='FACTOR', precision=3,
        update=_cap_update_norm_w,
    )
    norm_h: FloatProperty(
        name="H",
        description="Crop height (normalised 0\u20131)",
        default=1.0, min=0.001, max=1.0,
        subtype='FACTOR', precision=3,
        update=_cap_update_norm_h,
    )

    # -- Pixel crop (relative to scene render resolution) ----------------------
    pixel_x: IntProperty(
        name="X (px)",
        description="Crop left edge (pixels)",
        default=0, min=0,
        update=_cap_update_pixel_x,
    )
    pixel_y: IntProperty(
        name="Y (px)",
        description="Crop bottom edge (pixels)",
        default=0, min=0,
        update=_cap_update_pixel_y,
    )
    pixel_w: IntProperty(
        name="W (px)",
        description="Crop width (pixels)",
        default=1920, min=1,
        update=_cap_update_pixel_w,
    )
    pixel_h: IntProperty(
        name="H (px)",
        description="Crop height (pixels)",
        default=1080, min=1,
        update=_cap_update_pixel_h,
    )

    # -- Target output resolution ----------------------------------------------
    target_w: IntProperty(
        name="Target W",
        description="Desired final output width (pixels)",
        default=1920, min=1,
        update=_cap_update_target_w,
    )
    target_h: IntProperty(
        name="Target H",
        description="Desired final output height (pixels)",
        default=1080, min=1,
        update=_cap_update_target_h,
    )

    # -- Behaviour toggles -----------------------------------------------------
    enabled: BoolProperty(
        name="Enabled",
        description="Activate the crop region",
        default=False,
        update=_cap_update_enabled,
    )
    lock_aspect: BoolProperty(
        name="Lock Aspect",
        description="Keep the crop at the same aspect ratio as the target output",
        default=True,
        update=_cap_update_lock_aspect,
    )
    crop_to_border: BoolProperty(
        name="Crop Output",
        description="Crop the rendered image to the border region (instead of rendering the full frame)",
        default=True,
        update=_cap_update_crop_to_border,
    )
    original_w: IntProperty(
        name="Original W",
        description="Original scene width before crop scaling",
        default=1920, min=1,
    )
    original_h: IntProperty(
        name="Original H",
        description="Original scene height before crop scaling",
        default=1080, min=1,
    )
    has_original_resolution: BoolProperty(
        name="Has Original Resolution",
        description="Internal flag indicating that the addon owns a saved resolution to restore",
        default=False,
        options={'HIDDEN'},
    )
    auto_margin: FloatProperty(
        name="Margin",
        description="Extra padding for automatic crops as a percentage of the detected bounds",
        default=5.0, min=0.0, max=100.0,
        subtype='PERCENTAGE',
        precision=1,
    )
    auto_fit_target_aspect: BoolProperty(
        name="Fit Target Aspect",
        description="Expand automatic crops to preserve the target output aspect ratio",
        default=True,
    )


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class CCR_OT_reset(Operator):
    """Reset the crop to full frame and disable the addon."""
    bl_idname = "ccr.reset"
    bl_label = "Reset Crop"
    bl_description = "Reset crop to full frame and disable"

    def execute(self, context):
        p = context.scene.ccr_props
        rd = context.scene.render

        p.norm_x = 0.0
        p.norm_y = 0.0
        p.norm_w = 1.0
        p.norm_h = 1.0
        p.pixel_x = 0
        p.pixel_y = 0
        
        # Disable first to restore original resolution via the _cap_update_enabled callback
        p.enabled = False
        
        # Reset properties to match restored original resolution
        p.target_w = p.original_w
        p.target_h = p.original_h
        p.pixel_w = p.original_w
        p.pixel_h = p.original_h

        rd.use_border = False

        return {'FINISHED'}


class CCR_OT_match_scene(Operator):
    """Copy the current scene render resolution into the target output fields."""
    bl_idname = "ccr.match_scene"
    bl_label = "Match Scene"
    bl_description = "Set target resolution to current scene render resolution"

    def execute(self, context):
        rd = context.scene.render
        p = context.scene.ccr_props
        p.target_w = rd.resolution_x
        p.target_h = rd.resolution_y
        return {'FINISHED'}


class CCR_OT_target_multiplier(Operator):
    """Set target output to a multiple of the base crop size."""
    bl_idname = "ccr.target_multiplier"
    bl_label = "Target Multiplier"
    bl_description = "Set target output to a multiple of the current crop size"

    factor: IntProperty(name="Factor", default=1, min=1, max=8)

    def execute(self, context):
        global _updating
        p = context.scene.ccr_props
        base_w, base_h = _base_crop_pixel_size(context, p)

        _updating = True
        try:
            p.target_w = max(1, base_w * self.factor)
            p.target_h = max(1, base_h * self.factor)
        finally:
            _updating = False

        if p.enabled:
            _apply_to_render(context, p)
        return {'FINISHED'}


class CCR_OT_sync_from_render_border(Operator):
    """Copy Blender's current render border into the addon controls."""
    bl_idname = "ccr.sync_from_render_border"
    bl_label = "Sync From Render Border"
    bl_description = "Use Blender's current render border as the custom crop region"

    def execute(self, context):
        global _updating
        rd = context.scene.render
        p = context.scene.ccr_props

        nx = max(0.0, min(1.0, rd.border_min_x))
        ny = max(0.0, min(1.0, rd.border_min_y))
        nw = max(0.001, min(1.0 - nx, rd.border_max_x - rd.border_min_x))
        nh = max(0.001, min(1.0 - ny, rd.border_max_y - rd.border_min_y))

        _updating = True
        try:
            p.norm_x = nx
            p.norm_y = ny
            p.norm_w = nw
            p.norm_h = nh
            p.crop_to_border = rd.use_crop_to_border
            if rd.use_border:
                _capture_original_resolution(context, p)
            p.enabled = rd.use_border
        finally:
            _updating = False

        _clamp_crop(p)
        _sync_pixel_from_norm(context, p)
        if p.enabled:
            _apply_to_render(context, p)
        return {'FINISHED'}


class CCR_OT_target_preset(Operator):
    """Apply a common final output size."""
    bl_idname = "ccr.target_preset"
    bl_label = "Target Preset"
    bl_description = "Set a common target output size"

    preset: StringProperty(name="Preset", default="HD")
    fit_crop: BoolProperty(
        name="Fit Crop",
        description="Adjust the current crop to match the target aspect ratio",
        default=True,
    )

    def execute(self, context):
        p = context.scene.ccr_props
        rd = context.scene.render
        presets = {
            "SCENE": (p.original_w if p.has_original_resolution else rd.resolution_x, p.original_h if p.has_original_resolution else rd.resolution_y),
            "HD": (1920, 1080),
            "UHD": (3840, 2160),
            "SQUARE": (1080, 1080),
            "STORY": (1080, 1920),
            "POST_4_5": (1080, 1350),
            "CINEMA": (2560, 1080),
        }

        if self.preset == "SWAP":
            p.target_w, p.target_h = p.target_h, p.target_w
        elif self.preset in presets:
            p.target_w, p.target_h = presets[self.preset]
        else:
            self.report({'ERROR'}, "Unknown target preset")
            return {'CANCELLED'}

        if self.fit_crop:
            _fit_crop_to_target_aspect(context, p)
            _sync_pixel_from_norm(context, p)
        if p.enabled:
            _apply_to_render(context, p)
        return {'FINISHED'}


class CCR_OT_fit_crop_to_target(Operator):
    """Fit the current crop to the current target aspect."""
    bl_idname = "ccr.fit_crop_to_target"
    bl_label = "Fit Crop To Target"
    bl_description = "Trim the current crop around its center to match the target aspect ratio"

    def execute(self, context):
        p = context.scene.ccr_props
        _fit_crop_to_target_aspect(context, p)
        _sync_pixel_from_norm(context, p)
        if p.enabled:
            _apply_to_render(context, p)
        return {'FINISHED'}


class CCR_OT_crop_to_selection(Operator):
    """Crop the camera frame to the selected objects."""
    bl_idname = "ccr.crop_to_selection"
    bl_label = "Crop To Selection"
    bl_description = "Set the crop region from selected object bounds in the active camera"

    def execute(self, context):
        global _updating
        scene = context.scene
        camera = scene.camera
        p = scene.ccr_props

        if camera is None:
            self.report({'ERROR'}, "Scene has no active camera")
            return {'CANCELLED'}

        # Ensure original resolution is captured
        _capture_original_resolution(context, p)

        # Store active state, temporarily revert scene settings to uncropped original resolution for correct projection aspect
        old_use_border = context.scene.render.use_border
        old_res_x = context.scene.render.resolution_x
        old_res_y = context.scene.render.resolution_y

        context.scene.render.use_border = False
        context.scene.render.resolution_x = p.original_w
        context.scene.render.resolution_y = p.original_h

        # Force Blender dependency graph update to recalculate camera projection matrices
        context.view_layer.update()

        coords = []
        try:
            for obj in context.selected_objects:
                if obj.type == 'CAMERA' or not hasattr(obj, "bound_box"):
                    continue
                for corner in obj.bound_box:
                    world = obj.matrix_world @ mathutils.Vector(corner)
                    co = world_to_camera_view(scene, camera, world)
                    if co.z > 0.0:
                        coords.append(co)
        finally:
            # Revert scene render settings to previous state
            context.scene.render.use_border = old_use_border
            context.scene.render.resolution_x = old_res_x
            context.scene.render.resolution_y = old_res_y
            context.view_layer.update()

        if not coords:
            self.report({'ERROR'}, "No selected object bounds are visible to the camera")
            return {'CANCELLED'}

        min_x = min(co.x for co in coords)
        max_x = max(co.x for co in coords)
        min_y = min(co.y for co in coords)
        max_y = max(co.y for co in coords)

        width = max(0.001, max_x - min_x)
        height = max(0.001, max_y - min_y)
        margin = max(0.0, p.auto_margin) / 100.0
        pad_x = width * margin
        pad_y = height * margin

        nx = _clamp01(min_x - pad_x)
        ny = _clamp01(min_y - pad_y)
        nw = min(1.0 - nx, width + pad_x * 2.0)
        nh = min(1.0 - ny, height + pad_y * 2.0)

        # Apply properties atomically under update guard
        _updating = True
        try:
            p.norm_x = nx
            p.norm_y = ny
            p.norm_w = max(0.001, nw)
            p.norm_h = max(0.001, nh)
            if p.auto_fit_target_aspect:
                _expand_crop_to_target_aspect(context, p)
            p.enabled = True
        finally:
            _updating = False

        _clamp_crop(p)
        _sync_pixel_from_norm(context, p)
        _apply_to_render(context, p)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Presets  \u2014 quick crop region templates
# ---------------------------------------------------------------------------


class CCR_OT_apply_preset(Operator):
    """Apply a predefined crop region template."""
    bl_idname = "ccr.apply_preset"
    bl_label = "Apply Preset"
    bl_description = "Apply a crop region preset"

    preset: StringProperty(
        name="Preset",
        description="Which preset to apply",
    )

    def execute(self, context):
        global _updating
        p = context.scene.ccr_props
        rd = context.scene.render
        rx = p.original_w if p.has_original_resolution else max(1, rd.resolution_x)
        ry = p.original_h if p.has_original_resolution else max(1, rd.resolution_y)

        nx, ny, nw, nh = 0.0, 0.0, 1.0, 1.0
        tw, th = rx, ry

        # --- Fixed-size presets (norm_x, norm_y, norm_w, norm_h, target_w, target_h) ---
        fixed = {
            "FULL":        (0.0,   0.0,   1.0,   1.0,   rx,   ry),
            "CENTER_25":   (0.375, 0.375, 0.25,  0.25,  rx,   ry),
            "CENTER_50":   (0.25,  0.25,  0.5,   0.5,   rx,   ry),
            "CENTER_75":   (0.125, 0.125, 0.75,  0.75,  rx,   ry),
            "TOP_HALF":    (0.0,   0.5,   1.0,   0.5,   rx,   ry),
            "BOTTOM_HALF": (0.0,   0.0,   1.0,   0.5,   rx,   ry),
            "LEFT_THIRD":  (0.0,   0.0,   0.333, 1.0,   rx,   ry),
            "RIGHT_THIRD": (0.667, 0.0,   0.333, 1.0,   rx,   ry),
        }

        if self.preset in fixed:
            nx, ny, nw, nh, tw, th = fixed[self.preset]

        elif self.preset == "SQUARE_1_1":
            sz = min(rx, ry)
            tw, th = sz, sz
            if rx >= ry:
                nw = ry / rx
                nx = (1.0 - nw) / 2.0
                ny = 0.0
                nh = 1.0
            else:
                nh = rx / ry
                ny = (1.0 - nh) / 2.0
                nx = 0.0
                nw = 1.0

        elif self.preset == "SOCIAL_9_16":
            tw, th = 1080, 1920
            nw = (tw / th) * (ry / rx)
            if nw > 1.0:
                nw = 1.0
                nh = 1.0 / ((tw / th) * (ry / rx))
            else:
                nh = 1.0
            nx = (1.0 - nw) / 2.0
            ny = (1.0 - nh) / 2.0

        elif self.preset == "CINEMA_21_9":
            tw, th = 2560, 1080
            nh = (th / tw) * (rx / ry)
            if nh > 1.0:
                nh = 1.0
                nw = 1.0 / ((th / tw) * (rx / ry))
            else:
                nw = 1.0
            nx = (1.0 - nw) / 2.0
            ny = (1.0 - nh) / 2.0

        # Apply all properties atomically under the update guard
        _updating = True
        try:
            p.norm_x = nx
            p.norm_y = ny
            p.norm_w = nw
            p.norm_h = nh
            p.target_w = tw
            p.target_h = th
            _capture_original_resolution(context, p)
            p.enabled = True
        finally:
            _updating = False

        _clamp_crop(p)
        _sync_pixel_from_norm(context, p)
        if p.enabled:
            _apply_to_render(context, p)
        else:
            context.scene.render.use_border = False
            _restore_original_resolution(context, p)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Bookmarks  \u2014 user-saved crop configurations
# ---------------------------------------------------------------------------


class CCR_Bookmark(PropertyGroup):
    """A single saved crop configuration."""
    name: StringProperty(name="Name", default="Bookmark")
    norm_x: FloatProperty(default=0.0)
    norm_y: FloatProperty(default=0.0)
    norm_w: FloatProperty(default=1.0)
    norm_h: FloatProperty(default=1.0)
    target_w: IntProperty(default=1920)
    target_h: IntProperty(default=1080)
    enabled: BoolProperty(default=True)
    crop_to_border: BoolProperty(default=True)


class CCR_OT_bookmark_save(Operator):
    """Save current crop region as a bookmark."""
    bl_idname = "ccr.bookmark_save"
    bl_label = "Save Bookmark"
    bl_description = "Save current crop settings as a named bookmark"

    def execute(self, context):
        p = context.scene.ccr_props
        bookmarks = context.scene.ccr_bookmarks
        name = context.scene.ccr_bookmark_name.strip() or f"Bookmark {len(bookmarks) + 1}"

        # Overwrite existing bookmark with the same name
        for bm in bookmarks:
            if bm.name == name:
                bm.norm_x = p.norm_x
                bm.norm_y = p.norm_y
                bm.norm_w = p.norm_w
                bm.norm_h = p.norm_h
                bm.target_w = p.target_w
                bm.target_h = p.target_h
                bm.enabled = p.enabled
                bm.crop_to_border = p.crop_to_border
                self.report({'INFO'}, f"Bookmark '{name}' updated")
                return {'FINISHED'}

        bm = bookmarks.add()
        bm.name = name
        bm.norm_x = p.norm_x
        bm.norm_y = p.norm_y
        bm.norm_w = p.norm_w
        bm.norm_h = p.norm_h
        bm.target_w = p.target_w
        bm.target_h = p.target_h
        bm.enabled = p.enabled
        bm.crop_to_border = p.crop_to_border

        self.report({'INFO'}, f"Bookmark '{name}' saved")
        return {'FINISHED'}


class CCR_OT_bookmark_load(Operator):
    """Load a bookmarked crop region."""
    bl_idname = "ccr.bookmark_load"
    bl_label = "Load Bookmark"
    bl_description = "Load this bookmarked crop configuration"

    index: IntProperty(name="Index", description="Bookmark index to load", default=0)

    def execute(self, context):
        global _updating
        bookmarks = context.scene.ccr_bookmarks
        if self.index < 0 or self.index >= len(bookmarks):
            self.report({'ERROR'}, "Invalid bookmark index")
            return {'CANCELLED'}

        bm = bookmarks[self.index]
        p = context.scene.ccr_props

        # Apply all properties atomically under the update guard
        _updating = True
        try:
            p.norm_x = bm.norm_x
            p.norm_y = bm.norm_y
            p.norm_w = bm.norm_w
            p.norm_h = bm.norm_h
            p.target_w = bm.target_w
            p.target_h = bm.target_h
            if bm.enabled:
                _capture_original_resolution(context, p)
            p.enabled = bm.enabled
            p.crop_to_border = bm.crop_to_border
        finally:
            _updating = False

        _clamp_crop(p)
        _sync_pixel_from_norm(context, p)
        _apply_to_render(context, p)
        return {'FINISHED'}


class CCR_OT_bookmark_delete(Operator):
    """Delete a bookmark."""
    bl_idname = "ccr.bookmark_delete"
    bl_label = "Delete Bookmark"
    bl_description = "Delete this bookmark"

    index: IntProperty(name="Index", description="Bookmark index to delete", default=0)

    def execute(self, context):
        bookmarks = context.scene.ccr_bookmarks
        if 0 <= self.index < len(bookmarks):
            bookmarks.remove(self.index)
        return {'FINISHED'}


# ===================================================================
# VIEWPORT BORDER DRAWING TOOL
# ===================================================================


def _get_view3d_space(area):
    """Return the first VIEW_3D space in an area, or None."""
    if area is None:
        return None
    for space in area.spaces:
        if space.type == 'VIEW_3D':
            return space
    return None


def _get_view_camera(context, space):
    """Return the camera used by this 3D view."""
    if space is None:
        return None
    if getattr(space, "use_local_camera", False) and space.camera:
        return space.camera
    return context.scene.camera


def _get_camera_frame_in_region(context, region, area):
    """Compute the displayed camera frame in region pixel coordinates."""
    if region is None:
        return 0, 0, 1, 1

    space = _get_view3d_space(area)
    rv3d = space.region_3d if space else None
    if rv3d is None or rv3d.view_perspective != 'CAMERA':
        return 0, 0, region.width, region.height

    camera = _get_view_camera(context, space)
    if camera is None or camera.type != 'CAMERA' or camera.data is None:
        return 0, 0, region.width, region.height

    try:
        frame = camera.data.view_frame(scene=context.scene)
        points = [
            location_3d_to_region_2d(region, rv3d, camera.matrix_world @ corner)
            for corner in frame
        ]
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return 0, 0, region.width, region.height

    if not points or any(point is None for point in points):
        return 0, 0, region.width, region.height

    xs = [point.x for point in points]
    ys = [point.y for point in points]
    cam_x = min(xs)
    cam_y = min(ys)
    cam_w = max(xs) - cam_x
    cam_h = max(ys) - cam_y

    if cam_w <= 0 or cam_h <= 0:
        return 0, 0, region.width, region.height

    return cam_x, cam_y, cam_w, cam_h


def _mouse_to_render_norm(context, event, region, area):
    """Convert mouse position to render-border normalized coordinates (0–1).

    Maps through the camera frame so that (0,0) is the bottom-left of
    the camera frame and (1,1) is the top-right, regardless of viewport
    letterboxing.
    """
    cam_x, cam_y, cam_w, cam_h = _get_camera_frame_in_region(context, region, area)
    mx = event.mouse_x - region.x
    my = event.mouse_y - region.y
    nx = (mx - cam_x) / max(1, cam_w)
    ny = (my - cam_y) / max(1, cam_h)
    return nx, ny


def _clamp01(value):
    """Clamp a normalized coordinate to the render frame."""
    return max(0.0, min(1.0, value))


def _rect_from_points(x1, y1, x2, y2, *, centered=False):
    """Return a clamped normalized rectangle from two points."""
    if centered:
        dx = abs(x2 - x1)
        dy = abs(y2 - y1)
        nx = x1 - dx
        ny = y1 - dy
        nw = dx * 2.0
        nh = dy * 2.0
    else:
        nx = min(x1, x2)
        ny = min(y1, y2)
        nw = abs(x2 - x1)
        nh = abs(y2 - y1)

    nx = _clamp01(nx)
    ny = _clamp01(ny)
    nw = max(0.001, min(1.0 - nx, nw))
    nh = max(0.001, min(1.0 - ny, nh))
    return nx, ny, nw, nh
# This replaces / supplements Blender's built-in Ctrl+B with a
# custom modal operator that shows live dimension information as
# an overlay in the 3D viewport.
# ===================================================================

# ---------------------------------------------------------------------------
# GPU overlay drawing helpers
# ---------------------------------------------------------------------------

# Color theme (matching Blender's orange selection)
ORANGE = (1.0, 0.6, 0.0, 0.85)
ORANGE_FILL = (1.0, 0.6, 0.0, 0.08)
WHITE = (1.0, 1.0, 1.0, 0.9)
DIM_WHITE = (1.0, 1.0, 1.0, 0.5)
TEXT_BG = (0.05, 0.05, 0.05, 0.55)


def _draw_text_2d(x, y, text, color, font_size):
    """Draw a single line of text at (x, y) in POST_PIXEL coordinates."""
    blf.color(0, *color)
    blf.size(0, font_size)
    blf.position(0, x, y, 0)
    blf.draw(0, text)


def _draw_text_block_bg(x, y, lines, font_size, line_spacing=4, padding=4):
    """Draw a text block with a dark background rect for readability."""
    line_h = font_size + line_spacing
    block_h = len(lines) * line_h + padding * 2
    # Measure actual text width using blf for accuracy with proportional fonts
    blf.size(0, font_size)
    max_w = 0
    for line in lines:
        w, _ = blf.dimensions(0, line)
        max_w = max(max_w, w)
    bg_x1 = x - padding
    bg_y1 = y + 2  # slightly above baseline
    bg_x2 = x + max_w + padding
    bg_y2 = bg_y1 - block_h

    # Draw background
    coords = [(bg_x1, bg_y1), (bg_x2, bg_y1), (bg_x2, bg_y2), (bg_x1, bg_y2)]
    shader = _get_2d_shader()
    batch = batch_for_shader(shader, 'TRI_FAN', {"pos": coords})
    gpu.state.blend_set('ALPHA')
    shader.bind()
    shader.uniform_float("color", TEXT_BG)
    batch.draw(shader)

    # Draw text
    for i, line in enumerate(lines):
        ly = y - i * line_h
        _draw_text_2d(x, ly, line, WHITE, font_size)


# ---------------------------------------------------------------------------
# Modal operator for drawing crop border in the 3D viewport
# ---------------------------------------------------------------------------


class CCR_OT_draw_border(Operator):
    """Click and drag in the 3D viewport to define a crop region."""
    bl_idname = "ccr.draw_border"
    bl_label = "Draw Crop Border"
    bl_description = "Click and drag in the 3D viewport to interactively define a crop region with live dimensions"

    # Internal state (regular Python attributes, not bpy.props)
    _handle = None

    def _reset_state(self):
        self._active = False
        self._is_dragging = False
        self._norm_x1 = 0.0
        self._norm_y1 = 0.0
        self._norm_x2 = 0.0
        self._norm_y2 = 0.0
        self._drag_region = None
        self._drag_area = None
        self._alt_held = False
        self._shift_held = False
        self._ctrl_held = False

    @classmethod
    def poll(cls, context):
        # Requires at least one 3D viewport
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                return True
        return False

    def invoke(self, context, event):
        self._reset_state()
        self._active = True

        # Store pre-draw settings so we can restore them if cancelled
        p = context.scene.ccr_props
        self._pre_draw_res_x = context.scene.render.resolution_x
        self._pre_draw_res_y = context.scene.render.resolution_y
        self._pre_draw_use_border = context.scene.render.use_border
        self._pre_draw_border_min_x = context.scene.render.border_min_x
        self._pre_draw_border_min_y = context.scene.render.border_min_y
        self._pre_draw_border_max_x = context.scene.render.border_max_x
        self._pre_draw_border_max_y = context.scene.render.border_max_y

        if p.enabled and p.has_original_resolution:
            # Temporarily restore original resolution during drawing to prevent aspect jumps
            context.scene.render.resolution_x = p.original_w
            context.scene.render.resolution_y = p.original_h
            context.view_layer.update()

        # Register GPU draw handler on SpaceView3D
        args = (self,)
        self._handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_border_callback, args, 'WINDOW', 'POST_PIXEL'
        )

        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    @staticmethod
    def _find_view3d_region(context, event):
        """Find the 3D viewport WINDOW region under the mouse cursor.

        Returns (region, area) or (None, None).
        """
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for region in area.regions:
                    if region.type == 'WINDOW':
                        rx, ry = region.x, region.y
                        rw, rh = region.width, region.height
                        if rx <= event.mouse_x <= rx + rw and ry <= event.mouse_y <= ry + rh:
                            return region, area
        return None, None

    def modal(self, context, event):
        if not self._active:
            self._cleanup(context)
            return {'FINISHED'}

        region, area = self._find_view3d_region(context, event)

        if event.type == 'LEFTMOUSE':
            if event.value == 'PRESS':
                if not region:
                    return {'RUNNING_MODAL'}
                self._is_dragging = True
                self._drag_region = region
                self._drag_area = area
                self._alt_held = event.alt
                self._shift_held = event.shift
                self._ctrl_held = event.ctrl
                # Map mouse position through camera frame
                nx, ny = _mouse_to_render_norm(context, event, region, area)
                self._norm_x1 = nx
                self._norm_y1 = ny
                self._norm_x2 = nx
                self._norm_y2 = ny
                area.tag_redraw()
                return {'RUNNING_MODAL'}

            elif event.value == 'RELEASE':
                if self._is_dragging:
                    r = self._drag_region or region
                    a = self._drag_area or area
                    if r:
                        self._norm_x2, self._norm_y2 = self._get_constrained_coords(context, event, r, a)
                self._finalize_crop(context, event)
                self._cleanup(context)
                return {'FINISHED'}

        elif event.type == 'MOUSEMOVE':
            if self._is_dragging:
                self._alt_held = event.alt
                self._shift_held = event.shift
                self._ctrl_held = event.ctrl
                r = self._drag_region or region
                a = self._drag_area or area
                if r:
                    self._norm_x2, self._norm_y2 = self._get_constrained_coords(context, event, r, a)
                    self._live_update(context, event)
                    a.tag_redraw()
                return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}

        elif event.type in {'LEFT_SHIFT', 'RIGHT_SHIFT', 'LEFT_ALT', 'RIGHT_ALT', 'LEFT_CTRL', 'RIGHT_CTRL'}:
            # Handle modifier key press/release state changes when mouse is stationary
            if self._is_dragging:
                self._alt_held = event.alt
                self._shift_held = event.shift
                self._ctrl_held = event.ctrl
                r = self._drag_region or region
                a = self._drag_area or area
                if r:
                    self._norm_x2, self._norm_y2 = self._get_constrained_coords(context, event, r, a)
                    self._live_update(context, event)
                    a.tag_redraw()
                return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}

        elif event.type in {'RIGHTMOUSE', 'ESC'}:
            self._cleanup(context)
            return {'CANCELLED'}

        return {'PASS_THROUGH'}

    def _get_constrained_coords(self, context, event, region, area):
        nx2, ny2 = _mouse_to_render_norm(context, event, region, area)

        # Snap to a 5% grid (0.05 steps) when Ctrl is held
        if event.ctrl:
            nx2 = round(nx2 * 20.0) / 20.0
            ny2 = round(ny2 * 20.0) / 20.0

        p = context.scene.ccr_props
        lock_aspect = p.lock_aspect
        if event.shift:
            lock_aspect = not lock_aspect

        if lock_aspect and p.target_w > 0 and p.target_h > 0:
            rx = max(1, context.scene.render.resolution_x)
            ry = max(1, context.scene.render.resolution_y)
            aspect = (p.target_w / p.target_h) * (ry / rx)
            
            # Constrain to 1:1 square aspect when Shift is held if aspect lock was OFF
            if event.shift and not p.lock_aspect:
                aspect = ry / rx

            dnw = nx2 - self._norm_x1
            dnh = ny2 - self._norm_y1
            dnh_locked = abs(dnw) / aspect
            if dnh < 0:
                ny2 = self._norm_y1 - dnh_locked
            else:
                ny2 = self._norm_y1 + dnh_locked
        return nx2, ny2

    def _live_update(self, context, event):
        """Update CCR properties in real-time during drag for live preview."""
        global _updating
        p = context.scene.ccr_props

        # Compute crop rectangle using original helper to handle Alt centered drawing
        nx, ny, nw, nh = _rect_from_points(
            self._norm_x1, self._norm_y1, self._norm_x2, self._norm_y2,
            centered=event.alt
        )

        # Suppress callbacks, set all values, then apply manually
        _updating = True
        try:
            p.norm_x = nx
            p.norm_y = ny
            p.norm_w = nw
            p.norm_h = nh
            _capture_original_resolution(context, p)
            p.enabled = True
            _clamp_crop(p)
            _sync_pixel_from_norm(context, p)
        finally:
            _updating = False

        _apply_to_render(context, p, scale_resolution=False)

    def _finalize_crop(self, context, event):
        """Apply the final crop to CCR settings with proper callbacks."""
        self._finalized = True
        p = context.scene.ccr_props

        # Compute crop rectangle using original helper to handle Alt centered drawing
        nx, ny, nw, nh = _rect_from_points(
            self._norm_x1, self._norm_y1, self._norm_x2, self._norm_y2,
            centered=event.alt
        )

        # Ignore tiny crops
        if nw < 0.005 or nh < 0.005:
            return

        # Suppress callbacks, set all values, then apply manually to prevent loops
        global _updating
        _updating = True
        try:
            p.norm_x = nx
            p.norm_y = ny
            p.norm_w = nw
            p.norm_h = nh
            _capture_original_resolution(context, p)
            p.enabled = True
            _clamp_crop(p)
            _sync_pixel_from_norm(context, p)
        finally:
            _updating = False

        _apply_to_render(context, p)

    def _cleanup(self, context):
        """Remove draw handler and redraw viewports."""
        self._active = False
        if self._handle is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(self._handle, 'WINDOW')
            except (ValueError, TypeError):
                pass
            self._handle = None

        # If cancelled (i.e. did not finalize crop), restore the pre-draw settings
        if not getattr(self, "_finalized", False):
            if hasattr(self, "_pre_draw_res_x"):
                context.scene.render.resolution_x = self._pre_draw_res_x
                context.scene.render.resolution_y = self._pre_draw_res_y
                context.scene.render.use_border = self._pre_draw_use_border
                context.scene.render.border_min_x = self._pre_draw_border_min_x
                context.scene.render.border_min_y = self._pre_draw_border_min_y
                context.scene.render.border_max_x = self._pre_draw_border_max_x
                context.scene.render.border_max_y = self._pre_draw_border_max_y
                context.view_layer.update()

        # Redraw all 3D viewports
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()

    def cancel(self, context):
        """Called when the operator is cancelled externally."""
        self._cleanup(context)


# ---------------------------------------------------------------------------
# GPU draw callback for the border overlay
# ---------------------------------------------------------------------------


def _draw_border_callback(op):
    """Called per-frame to draw the crop overlay in all 3D viewports.

    Uses gpu.state.viewport_get() for viewport dimensions (reliable in draw
    handlers) and reads render-border values directly from rd.border_min/max_x/y
    to guarantee the overlay matches Blender's built-in render border display.
    """
    if not op._active:
        return

    shader = _get_2d_shader()
    gpu.state.blend_set('ALPHA')

    # Use gpu.state.viewport_get() for dimensions — always correct for the
    # current drawing context, unlike bpy.context.region which can reference
    # a different region.
    vp = gpu.state.viewport_get()
    width, height = vp[2], vp[3]

    if width < 2 or height < 2:
        return

    # Compute from Blender's actual projected camera frame. This keeps the
    # overlay aligned with Blender's dashed render border when camera view is
    # zoomed, panned, shifted, or using custom sensor/aspect settings.
    cam_x, cam_y, cam_w, cam_h = 0, 0, width, height
    try:
        context = bpy.context
        region = context.region
        area = context.area
        if region and area and area.type == 'VIEW_3D':
            width, height = region.width, region.height
            cam_x, cam_y, cam_w, cam_h = _get_camera_frame_in_region(
                context, region, area
            )
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass

    # Read normalized coords from Blender's actual render border settings.
    # This guarantees the overlay uses the EXACT same values Blender uses
    # for its built-in render border display, so they cannot diverge.
    try:
        rd = bpy.context.scene.render
        if op._is_dragging:
            nx1, ny1, nw, nh = _rect_from_points(
                op._norm_x1, op._norm_y1, op._norm_x2, op._norm_y2,
                centered=getattr(op, "_alt_held", False)
            )
            nx2 = nx1 + nw
            ny2 = ny1 + nh
        elif rd.use_border:
            nx1 = rd.border_min_x
            ny1 = rd.border_min_y
            nx2 = rd.border_max_x
            ny2 = rd.border_max_y
        else:
            nx1 = op._norm_x1
            ny1 = op._norm_y1
            nx2 = op._norm_x2
            ny2 = op._norm_y2
    except (AttributeError, RuntimeError):
        if op._is_dragging:
            nx1, ny1, nw, nh = _rect_from_points(
                op._norm_x1, op._norm_y1, op._norm_x2, op._norm_y2,
                centered=getattr(op, "_alt_held", False)
            )
            nx2 = nx1 + nw
            ny2 = ny1 + nh
        else:
            nx1, ny1 = op._norm_x1, op._norm_y1
            nx2, ny2 = op._norm_x2, op._norm_y2

    # Convert render-border normalized coords to pixel coords via camera frame
    x1 = int(cam_x + nx1 * cam_w)
    y1 = int(cam_y + ny1 * cam_h)
    x2 = int(cam_x + nx2 * cam_w)
    y2 = int(cam_y + ny2 * cam_h)

    rx1, rx2 = min(x1, x2), max(x1, x2)
    ry1, ry2 = min(y1, y2), max(y1, y2)

    rect_w = rx2 - rx1
    rect_h = ry2 - ry1

    if not op._is_dragging or rect_w < 2 or rect_h < 2:
        # Not dragging yet — show instruction text centered in viewport
        _draw_text_2d(
            width // 2 - 145, height // 2 + 6,
            "Click & drag to define crop region  (Esc to cancel)",
            DIM_WHITE, 13,
        )
        return

    # ----- Draw rectangle fill -----
    fill_coords = [(rx1, ry1), (rx2, ry1), (rx2, ry2), (rx1, ry2)]
    fill_batch = batch_for_shader(shader, 'TRI_FAN', {"pos": fill_coords})
    shader.bind()
    shader.uniform_float("color", ORANGE_FILL)
    fill_batch.draw(shader)

    # ----- Draw rectangle outline -----
    outline_coords = [(rx1, ry1), (rx2, ry1), (rx2, ry2), (rx1, ry2)]
    outline_indices = [(0, 1), (1, 2), (2, 3), (3, 0)]
    outline_batch = batch_for_shader(shader, 'LINES', {"pos": outline_coords}, indices=outline_indices)
    shader.uniform_float("color", ORANGE)
    outline_batch.draw(shader)

    # ----- Draw corner handles -----
    hs = 5  # handle half-size
    handle_coords = [
        (rx1 - hs, ry1 - hs), (rx1 + hs, ry1 + hs),  # bottom-left
        (rx2 - hs, ry1 - hs), (rx2 + hs, ry1 + hs),  # bottom-right
        (rx2 - hs, ry2 - hs), (rx2 + hs, ry2 + hs),  # top-right
        (rx1 - hs, ry2 - hs), (rx1 + hs, ry2 + hs),  # top-left
    ]
    handle_indices = [
        (0, 1),  # bottom-left
        (2, 3),  # bottom-right
        (4, 5),  # top-right
        (6, 7),  # top-left
    ]
    handle_batch = batch_for_shader(shader, 'LINES', {"pos": handle_coords}, indices=handle_indices)
    shader.uniform_float("color", ORANGE)
    handle_batch.draw(shader)

    # ----- Calculate overlay info (render-normalized, matching rd.border) -----
    crop_norm_w = abs(nx2 - nx1)
    crop_norm_h = abs(ny2 - ny1)
    crop_norm_x = min(nx1, nx2)
    crop_norm_y = min(ny1, ny2)

    # Pixel dimensions at scene render resolution
    try:
        rd = bpy.context.scene.render
        p = bpy.context.scene.ccr_props
        rx = max(1, rd.resolution_x)
        ry = max(1, rd.resolution_y)
        crop_px_w = round(crop_norm_w * rx)
        crop_px_h = round(crop_norm_h * ry)
        aw, ah = _simplify_ratio(crop_px_w, crop_px_h)
        req_w = round(p.target_w / max(0.001, crop_norm_w))
        req_h = round(p.target_h / max(0.001, crop_norm_h))
    except (AttributeError, RuntimeError, TypeError):
        # Context not available — skip info overlay for this frame
        return

    # ----- Draw text overlay -----
    # Position the text block just above the top-right corner
    text_x = min(rx2 + 10, width - 240)
    text_y = min(ry2 - 4, height - 8)

    # If text would extend outside viewport, move it inside
    if text_x < 8:
        text_x = max(rx1, 8)
    if text_y < 40:
        text_y = max(ry1 + 4, 40)

    lines = [
        f"{crop_px_w} \u00d7 {crop_px_h} px  (crop)",
        f"Aspect: {aw}:{ah}",
        f"Required Render: {req_w} \u00d7 {req_h} px",
        f"Norm: ({crop_norm_x:.3f}, {crop_norm_y:.3f}) \u2192 ({crop_norm_x+crop_norm_w:.3f}, {crop_norm_y+crop_norm_h:.3f})",
    ]
    _draw_text_block_bg(text_x, text_y, lines, 12)

    # ----- Draw mouse coordinates at bottom-left -----
    _draw_text_2d(
        8, 8,
        f"Viewport: ({x1}, {y1}) \u2192 ({x2}, {y2})",
        DIM_WHITE, 10,
    )


# ===================================================================
# END VIEWPORT BORDER DRAWING TOOL
# ===================================================================


# ---------------------------------------------------------------------------
# Panel  (appears in Properties Editor → Output tab)
# ---------------------------------------------------------------------------
class CCR_PT_main(Panel):
    bl_label = "Custom Crop Regions"
    bl_idname = "CCR_PT_main"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'output'

    @staticmethod
    def _draw_toggles(layout, props):
        row = layout.row(align=True)
        row.prop(props, "enabled", text="Enable", toggle=True)
        row.prop(props, "lock_aspect", text="Lock Aspect", toggle=True)
        row.prop(props, "crop_to_border", text="Crop", toggle=True)

    @staticmethod
    def _draw_normalised(layout, props):
        box = layout.box()
        box.label(text="Crop Region (Normalised 0\u20131)", icon='VIEWZOOM')
        col = box.column(align=True)
        row = col.row(align=True)
        row.prop(props, "norm_x", text="X")
        row.prop(props, "norm_y", text="Y")
        row = col.row(align=True)
        row.prop(props, "norm_w", text="W")
        row.prop(props, "norm_h", text="H")

    @staticmethod
    def _draw_pixels(layout, props):
        box = layout.box()
        box.label(text="Crop Region (Pixels)", icon='IMAGE_DATA')
        col = box.column(align=True)
        row = col.row(align=True)
        row.prop(props, "pixel_x", text="X")
        row.prop(props, "pixel_y", text="Y")
        row = col.row(align=True)
        row.prop(props, "pixel_w", text="W")
        row.prop(props, "pixel_h", text="H")

    @staticmethod
    def _draw_target(layout, props):
        box = layout.box()
        box.label(text="Target Output", icon='OUTPUT')
        col = box.column(align=True)
        row = col.row(align=True)
        row.prop(props, "target_w", text="Width")
        row.prop(props, "target_h", text="Height")
        row = col.row(align=True)
        for factor in (1, 2, 3, 4):
            op = row.operator("ccr.target_multiplier", text=f"{factor}x")
            op.factor = factor
        col.operator("ccr.match_scene", text="Match Scene Resolution",
                      icon='ARROW_LEFTRIGHT')
        col.operator("ccr.fit_crop_to_target", text="Fit Crop to Target Aspect",
                      icon='FULLSCREEN_ENTER')

    @staticmethod
    def _draw_results(layout, context, props, rd):
        if not (props.enabled and props.norm_w > 0.0 and props.norm_h > 0.0):
            return

        box = layout.box()
        box.label(text="Results", icon='INFO')
        col = box.column(align=True)

        # Required render resolution so cropped area == target
        req_w = round(props.target_w / props.norm_w)
        req_h = round(props.target_h / props.norm_h)

        if props.has_original_resolution:
            row = col.row()
            row.label(text="Base Render Res.:")
            row.label(text=f"{props.original_w} \u00d7 {props.original_h}")

        base_crop_w, base_crop_h = _base_crop_pixel_size(context, props)
        row = col.row()
        row.label(text="Base Crop Size:")
        row.label(text=f"{base_crop_w} \u00d7 {base_crop_h}")

        row = col.row()
        row.label(text="Scaled Render Res.:")
        row.label(text=f"{req_w} \u00d7 {req_h}")

        # Current scene render resolution (may differ from required)
        cur_crop_w = round(props.norm_w * rd.resolution_x)
        cur_crop_h = round(props.norm_h * rd.resolution_y)
        row = col.row()
        row.label(text="Current Crop Size:")
        row.label(text=f"{cur_crop_w} \u00d7 {cur_crop_h}")

        row = col.row()
        row.label(text="Target Crop Size:")
        row.label(text=f"{props.target_w} \u00d7 {props.target_h}")

        # Aspect ratio
        aw, ah = _simplify_ratio(props.target_w, props.target_h)
        af = props.target_w / max(1, props.target_h)
        row = col.row()
        row.label(text="Aspect Ratio:")
        row.label(text=f"{aw}:{ah}  ({af:.3f})")

        # Frame usage
        usage = props.norm_w * props.norm_h * 100.0
        row = col.row()
        row.label(text="Frame Usage:")
        row.label(text=f"{usage:.1f}%")

        if req_w > 16384 or req_h > 16384:
            row = col.row()
            row.alert = True
            row.label(text="High render resolution", icon='ERROR')

    def draw(self, context):
        layout = self.layout
        props = context.scene.ccr_props
        rd = context.scene.render

        # --- Viewport border button (prominent, at the top) ---
        row = layout.row(align=True)
        row.scale_y = 1.6
        row.operator("ccr.draw_border", text="Draw Crop in Viewport",
                      icon='VIEW3D')
        layout.operator("ccr.sync_from_render_border",
                         text="Sync From Blender Render Border",
                         icon='RECOVER_LAST')
        layout.separator()

        self._draw_toggles(layout, props)
        layout.separator()
        self._draw_normalised(layout, props)
        self._draw_pixels(layout, props)
        self._draw_target(layout, props)
        self._draw_results(layout, context, props, rd)

        layout.separator()
        layout.operator("ccr.reset", text="Reset to Full Frame",
                         icon='LOOP_BACK')
        layout.separator()
        col = layout.column(align=True)
        col.label(text=f"Scene:  {rd.resolution_x} \u00d7 {rd.resolution_y}",
                  icon='CAMERA_DATA')


class CCR_PT_presets(Panel):
    bl_label = "Presets"
    bl_idname = "CCR_PT_presets"
    bl_parent_id = "CCR_PT_main"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'output'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        box = layout.column(align=True)

        # Row 1
        row = box.row(align=True)
        row.operator("ccr.apply_preset", text="Full Frame").preset = "FULL"
        row.operator("ccr.apply_preset", text="Center 25%").preset = "CENTER_25"
        row.operator("ccr.apply_preset", text="Center 50%").preset = "CENTER_50"

        # Row 2
        row = box.row(align=True)
        row.operator("ccr.apply_preset", text="Center 75%").preset = "CENTER_75"
        row.operator("ccr.apply_preset", text="Top Half").preset = "TOP_HALF"
        row.operator("ccr.apply_preset", text="Bottom Half").preset = "BOTTOM_HALF"

        # Row 3
        row = box.row(align=True)
        row.operator("ccr.apply_preset", text="Left Third").preset = "LEFT_THIRD"
        row.operator("ccr.apply_preset", text="Right Third").preset = "RIGHT_THIRD"
        row.operator("ccr.apply_preset", text="Square 1:1").preset = "SQUARE_1_1"

        # Row 4
        row = box.row(align=True)
        row.operator("ccr.apply_preset", text="9:16 Portrait").preset = "SOCIAL_9_16"
        row.operator("ccr.apply_preset", text="21:9 Cinema").preset = "CINEMA_21_9"


class CCR_PT_output_formats(Panel):
    bl_label = "Output Formats"
    bl_idname = "CCR_PT_output_formats"
    bl_parent_id = "CCR_PT_main"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'output'
    bl_options = {'DEFAULT_CLOSED'}

    @staticmethod
    def _target(layout, preset, text):
        op = layout.operator("ccr.target_preset", text=text)
        op.preset = preset
        op.fit_crop = True

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)

        row = col.row(align=True)
        self._target(row, "SCENE", "Scene")
        self._target(row, "HD", "HD")
        self._target(row, "UHD", "4K")

        row = col.row(align=True)
        self._target(row, "SQUARE", "1:1")
        self._target(row, "POST_4_5", "4:5")
        self._target(row, "STORY", "9:16")

        row = col.row(align=True)
        self._target(row, "CINEMA", "21:9")
        op = row.operator("ccr.target_preset", text="Swap")
        op.preset = "SWAP"
        op.fit_crop = True


class CCR_PT_auto_crop(Panel):
    bl_label = "Auto Crop"
    bl_idname = "CCR_PT_auto_crop"
    bl_parent_id = "CCR_PT_main"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'output'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        props = context.scene.ccr_props
        col = layout.column(align=True)
        col.prop(props, "auto_margin")
        col.prop(props, "auto_fit_target_aspect")
        col.operator("ccr.crop_to_selection", text="Crop To Selection",
                     icon='RESTRICT_SELECT_OFF')


class CCR_PT_bookmarks(Panel):
    bl_label = "Bookmarks"
    bl_idname = "CCR_PT_bookmarks"
    bl_parent_id = "CCR_PT_main"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'output'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        box = layout.column(align=True)

        # Save new bookmark
        row = box.row(align=True)
        row.prop(context.scene, "ccr_bookmark_name", text="")
        row.operator("ccr.bookmark_save", text="", icon='ADD')
        box.separator()

        # List saved bookmarks
        bookmarks = context.scene.ccr_bookmarks
        for i, bm in enumerate(bookmarks):
            row = box.row(align=True)
            row.label(text=bm.name, icon='BOOKMARKS')
            op_load = row.operator("ccr.bookmark_load", text="", icon='IMPORT')
            op_load.index = i
            op_del = row.operator("ccr.bookmark_delete", text="", icon='X')
            op_del.index = i

        if not bookmarks:
            row = box.row()
            row.label(text="No bookmarks saved yet", icon='INFO')


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = (
    CCR_Properties,
    CCR_Bookmark,
    CCR_OT_reset,
    CCR_OT_match_scene,
    CCR_OT_target_multiplier,
    CCR_OT_sync_from_render_border,
    CCR_OT_target_preset,
    CCR_OT_fit_crop_to_target,
    CCR_OT_crop_to_selection,
    CCR_OT_apply_preset,
    CCR_OT_bookmark_save,
    CCR_OT_bookmark_load,
    CCR_OT_bookmark_delete,
    CCR_OT_draw_border,
    CCR_PT_main,
    CCR_PT_presets,
    CCR_PT_output_formats,
    CCR_PT_auto_crop,
    CCR_PT_bookmarks,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.ccr_props = bpy.props.PointerProperty(type=CCR_Properties)
    bpy.types.Scene.ccr_bookmarks = bpy.props.CollectionProperty(type=CCR_Bookmark)
    bpy.types.Scene.ccr_bookmark_name = bpy.props.StringProperty(
        name="Bookmark Name",
        description="Name for the new bookmark",
        default="My Crop",
    )


def unregister():
    if hasattr(bpy.types.Scene, "ccr_bookmark_name"):
        del bpy.types.Scene.ccr_bookmark_name
    if hasattr(bpy.types.Scene, "ccr_bookmarks"):
        del bpy.types.Scene.ccr_bookmarks
    if hasattr(bpy.types.Scene, "ccr_props"):
        del bpy.types.Scene.ccr_props
    for cls in reversed(_classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass


if __name__ == "__main__":
    register()
