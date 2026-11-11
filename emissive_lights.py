# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 IngeTrazo contributors.
"""Emissive light materials for IngeTrazo -- with a smooth glow.

IngeTrazo lights the model with one sun and has no lamps. This extension adds
the missing half of a lighting setup the SketchUp way: paint a face with a
*light material* and it reads as a source of light.

What it does:

- six emissive material TYPES, named ``Light · <type>`` and registered in the
  document's material registry (``scene.materials``), so they survive save,
  copy and export like any other named material;
- a **smooth glow**: three additive layers with a Gaussian falloff, with no
  hard rim and no arbitrary cut-off radius -- the halo fades to nothing;
- a **colour control**: any hue can be given to the light, and it is kept on
  the face (``Light · Custom``) so it reopens with the colour it had;
- optionally, the **pool of light** the lamp throws on the floor;
- the same glow on **every picture the host renders off-screen**: Export
  image, the sheet composer's frames, and the PDF / image / print of a sheet
  built from them. A vector frame (pure line drawing) keeps its clean paper.

The glow is a screen simulation, not a real light: nothing else in the scene
is lit by it. For a physically lit image, use **Render with Blender** and add
real lamps there.

Everything it changes goes through the command layer: one undo step, and
``Ctrl+Z`` takes the light back off.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QRectF, QSettings, Qt
from PySide6.QtGui import (QBrush, QColor, QIcon, QPainter, QPen, QPixmap,
                           QRadialGradient, QVector3D)
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QColorDialog,
                               QGridLayout, QHBoxLayout, QLabel, QPushButton,
                               QSlider, QVBoxLayout, QWidget)

from core.history import Command, _dirty_group_chunks
from core.i18n import tr
from core.materials import Material

#: Prefix that marks a material as one of ours. It is part of the name, and
#: the name is what the .igz keeps -- a face painted with "Light · Warm bulb"
#: is still a light when the document is opened again, even if the registry
#: entry was lost.
PREFIX = "Light \u00b7 "

#: How many glows the overlay draws at most, so a model painted all over with
#: light material cannot turn every frame into a slideshow.
_MAX_GLOWS = 120


# ---------------------------------------------------------------------------
# 1. The light types
# ---------------------------------------------------------------------------

def _kelvin_rgb(kelvin: float) -> tuple:
    """Approximate black-body colour (Tanner Helland's fit), floats 0-1."""
    t = max(1000.0, float(kelvin)) / 100.0
    if t <= 66.0:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
    else:
        r = 329.698727446 * ((t - 60.0) ** -0.1332047592)
        g = 288.1221695283 * ((t - 60.0) ** -0.0755148492)
    if t >= 66.0:
        b = 255.0
    elif t <= 19.0:
        b = 0.0
    else:
        b = 138.5177312231 * math.log(t - 10.0) - 305.0447927307
    return tuple(max(0.0, min(1.0, v / 255.0)) for v in (r, g, b))


def _face_rgb(rgb: tuple) -> tuple:
    """The colour the face itself is painted: the light's hue, lifted toward
    white so it reads as a lamp and not as a coloured wall.

    The lift tops out at exactly 1.0 (``0.20 + 0.80·c``), so a pure channel
    survives the round trip through ``_custom_spec`` with no clamping."""
    return tuple(0.20 + 0.80 * float(c) for c in rgb)


#: (label, kelvin or None, intensity). Intensity scales the glow, not the
#: physical watts: it is what makes a candle read dimmer than a neon tube.
_SPECS = (
    ("Candle", 1900.0, 0.55),
    ("Warm bulb", 2700.0, 1.00),
    ("Neutral", 4000.0, 1.10),
    ("Cool LED", 6500.0, 1.20),
    ("Neon", None, 1.40),
    ("Screen", 8000.0, 0.85),
)

#: name -> {"rgb", "face", "intensity", "kelvin"}. Ordered: the panel shows
#: them in this order.
PRESETS: dict = {}
for _label, _kelvin, _intensity in _SPECS:
    _rgb = (0.98, 0.22, 0.72) if _label == "Neon" else _kelvin_rgb(_kelvin)
    PRESETS[PREFIX + _label] = {
        "rgb": _rgb,
        "face": _face_rgb(_rgb),
        "intensity": float(_intensity),
        "kelvin": _kelvin,
    }

#: The material a light painted with a colour of its own wears. The exact
#: colour lives on the face (``attrs["color"]``), which is what the .igz
#: keeps; the name only has to say "this is a light".
CUSTOM = PREFIX + "Custom"


def _custom_spec(color) -> dict:
    """The spec of a light whose colour was picked by hand.

    The face carries the *lifted* colour (``_face_rgb``), because a lamp
    has to read bright; the glow colour is recovered by undoing that lift."""
    face = tuple(float(c) for c in (color or (1.0, 1.0, 1.0))[:3])
    rgb = tuple(max(0.0, min(1.0, (c - 0.20) / 0.80)) for c in face)
    return {"rgb": rgb, "face": face, "intensity": 1.0, "kelvin": None}


# ---------------------------------------------------------------------------
# 2. The commands (one undo step each)
# ---------------------------------------------------------------------------

class SetEmissiveCommand(Command):
    """Paint ``faces`` with the emissive material ``name``.

    Sets the bright face colour and the ``attrs["mat"]`` identity, and makes
    sure the registry carries the material (``scene.materials``). Undo puts
    every face's attrs back exactly as they were and removes the registry
    entry if this command created it."""

    def __init__(self, faces, name: str, color: tuple) -> None:
        self._faces = list(faces)
        self._name = str(name)
        self._color = tuple(float(c) for c in color)
        self._old = None
        self._had_material = False
        self._old_material = None
        self._old_colour = None

    def do(self, scene) -> None:
        if self._old is None:
            self._old = [(f, dict(f.attrs)) for f in self._faces]
            self._had_material = self._name in scene.materials
            self._old_material = scene.materials.get(self._name)
            self._old_colour = (None if self._old_material is None
                                else self._old_material.color)
        material = scene.materials.get(self._name)
        if material is None:
            scene.materials[self._name] = Material(
                name=self._name, color=self._color, finish="matte")
        else:
            # A custom-colour light reuses one registry entry: keep its
            # swatch on the colour of the last face painted with it.
            material.color = self._color
        for f in self._faces:
            f.attrs["color"] = self._color
            f.attrs["mat"] = self._name
            # A material replaces the paint. Leaving a texture on the face
            # would keep the image drawn there and the light colour would
            # never appear -- which is exactly what happened on a textured,
            # imported model. Undo restores the attrs, texture included.
            f.attrs.pop("texture", None)
        _dirty_group_chunks(scene)
        scene.version += 1

    def undo(self, scene) -> None:
        for f, attrs in self._old or ():
            f.attrs.clear()
            f.attrs.update(attrs)
        if not self._had_material:
            scene.materials.pop(self._name, None)
        elif self._old_material is not None:
            self._old_material.color = self._old_colour
            scene.materials[self._name] = self._old_material
        _dirty_group_chunks(scene)
        scene.version += 1


class ClearEmissiveCommand(Command):
    """Take the light paint off ``faces``: back to the default material.

    Only faces that actually wear one of our materials are touched, so
    running it with a mixed selection cannot strip an unrelated paint."""

    def __init__(self, faces) -> None:
        self._faces = list(faces)
        self._old = None

    def do(self, scene) -> None:
        if self._old is None:
            self._old = [(f, dict(f.attrs)) for f in self._faces]
        for f in self._faces:
            if str(f.attrs.get("mat") or "").startswith(PREFIX):
                f.attrs.pop("mat", None)
                f.attrs.pop("color", None)
        _dirty_group_chunks(scene)
        scene.version += 1

    def undo(self, scene) -> None:
        for f, attrs in self._old or ():
            f.attrs.clear()
            f.attrs.update(attrs)
        _dirty_group_chunks(scene)
        scene.version += 1


# ---------------------------------------------------------------------------
# 3. Reading the document
# ---------------------------------------------------------------------------

def _group_faces(group) -> list:
    faces = list(getattr(getattr(group, "mesh", None), "faces", ()) or ())
    for child in getattr(group, "children", ()) or ():
        faces.extend(_group_faces(child))
    return faces


def selected_faces(scene) -> list:
    """Every face in the selection: faces picked one by one, or the whole
    mesh (children included) of every selected object."""
    from core.mesh import Face
    out, seen = [], set()
    for entry in scene.selection:
        if isinstance(entry, Face):
            faces = [entry]
        elif getattr(entry, "mesh", None) is not None:
            faces = _group_faces(entry)
        else:
            continue
        for f in faces:
            if id(f) not in seen:
                seen.add(id(f))
                out.append(f)
    return out


#: Last computed light list, per scene version. Recomputing it every frame
#: would walk every face of the model on every paint.
_CACHE = {"scene": None, "version": -1, "lights": ()}


def _world_point(p, xform):
    """A vertex position in WORLD metres: through the instance's transform
    when it wears one. A component's ``mesh`` is a prototype in LOCAL
    coordinates shared by every copy, so the glow used to sit at the
    prototype's place instead of at the instance standing in the model."""
    if xform is None:
        return p
    try:
        from PySide6.QtGui import QVector4D
        v = xform.map(QVector4D(p.x(), p.y(), p.z(), 1.0))
        w = v.w()
        if w != 0.0 and abs(w - 1.0) > 1e-9:
            return QVector3D(v.x() / w, v.y() / w, v.z() / w)
        return QVector3D(v.x(), v.y(), v.z())
    except Exception:
        return p                    # an exotic transform: better lit than lost


def _owners_visible(scene, owners) -> bool:
    """Whether the glow's object is drawn at all — hidden by Hide, by a
    scene, or carried by a layer that is off (a layer set not to print
    leaves the sheet too). Asked when the glow is DRAWN, not collected:
    layer visibility and scenes do not bump the scene's version, so the
    cached light list cannot carry the answer."""
    for g in owners:
        try:
            if not scene.entity_visible(g):
                return False
        except Exception:
            return True     # a scene without that API keeps the old glow
    return True


def _collect(scene) -> list:
    """[(centre QVector3D, radius m, preset dict, owners tuple)] -- one entry
    per object wearing a light material (its emissive faces merged).
    ``owners`` is the chain of containers from the scene root down to the
    object, what :func:`_owners_visible` reads; an empty tuple is loose
    geometry, which no layer can turn off."""
    buckets: dict = {}

    def eat(mesh, owner, owners, xform) -> None:
        for f in getattr(mesh, "faces", ()) or ():
            name = f.attrs.get("mat")
            if not name or not name.startswith(PREFIX):
                continue
            spec = PRESETS.get(name) or _custom_spec(f.attrs.get("color"))
            key = id(owner) if owner is not None else ("loose", name)
            b = buckets.get(key)
            if b is None:
                b = buckets[key] = {
                    "spec": spec,
                    "owners": owners,
                    "lo": [1e30, 1e30, 1e30],
                    "hi": [-1e30, -1e30, -1e30],
                }
            lo, hi = b["lo"], b["hi"]
            for v in f.vertices:          # QVector3D positions already
                p = _world_point(v, xform)
                for i, c in enumerate((p.x(), p.y(), p.z())):
                    if c < lo[i]:
                        lo[i] = c
                    if c > hi[i]:
                        hi[i] = c

    def eat_group(g, owners=(), xform=None) -> None:
        # A nested placement's own transform is given in its parent's
        # coordinates: composed with everything above it, so a light inside
        # a component's repetition lands where that copy stands.
        own = getattr(g, "xform", None)
        total = xform if own is None else (
            own if xform is None else xform * own)
        eat(getattr(g, "mesh", None), g, owners + (g,), total)
        for child in getattr(g, "children", ()) or ():
            eat_group(child, owners + (g,), total)

    for g in getattr(scene, "groups", ()) or ():
        eat_group(g)
    eat(getattr(scene, "loose_mesh", None), None, (), None)

    lights = []
    for b in buckets.values():
        lo, hi = b["lo"], b["hi"]
        if lo[0] > hi[0]:
            continue
        centre = QVector3D((lo[0] + hi[0]) / 2.0,
                           (lo[1] + hi[1]) / 2.0,
                           (lo[2] + hi[2]) / 2.0)
        radius = 0.5 * max(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])
        lights.append((centre, max(radius, 0.03), b["spec"], b["owners"]))
    return lights


def lights(scene) -> list:
    version = int(getattr(scene, "version", 0))
    if _CACHE["scene"] is scene and _CACHE["version"] == version:
        return _CACHE["lights"]
    found = _collect(scene)
    _CACHE.update(scene=scene, version=version, lights=found)
    return found


# ---------------------------------------------------------------------------
# 4. The overlay: the smooth glow
# ---------------------------------------------------------------------------

_STATE = {"glow": True, "strength": 1.0, "pool": True}


def _glow_colour(rgb: tuple, alpha: float) -> QColor:
    c = QColor.fromRgbF(*rgb)
    c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


def _smooth_glow(painter: QPainter, x: float, y: float, radius: float,
                 rgb: tuple, peak: float, steps: int = 16,
                 squash: float = 1.0) -> None:
    """One Gaussian halo: alpha follows ``exp(-k t²)·(1-t)``, so it leaves no
    rim and reaches zero exactly at ``radius`` -- no hard edge, no cut.

    ``squash`` below 1 flattens the circle into an ellipse (the pool of light
    on the floor), transform included, so the gradient follows it."""
    if radius <= 0.5 or peak <= 0.003:
        return
    painter.save()
    if squash != 1.0:
        painter.translate(x, y)
        painter.scale(1.0, squash)
        x = y = 0.0
    grad = QRadialGradient(x, y, radius)
    for i in range(steps + 1):
        t = i / steps
        a = peak * math.exp(-3.4 * t * t) * (1.0 - t) ** 0.7
        grad.setColorAt(t, _glow_colour(rgb, a))
    painter.setBrush(QBrush(grad))
    painter.drawEllipse(QRectF(x - radius, y - radius,
                               radius * 2.0, radius * 2.0))
    painter.restore()


def draw_glow(app, viewport, painter) -> None:
    """A smooth additive glow at every emissive object, plus the pool of
    light it throws on the ground.

    Called for the live widget (an ``add_overlay`` callback) AND for the
    off-screen pictures (:func:`_paint_glow_on_image`): everything it asks
    comes from the ``viewport`` handed in -- its scene, its camera, its own
    ``world_to_pixels`` -- so a picture taken with another camera (a sheet
    frame inside the composer's ``_with_frame_camera``) is lit at the right
    place and the right size."""
    if not _STATE["glow"]:
        return
    try:
        scene = viewport.scene
        found = lights(scene)[:_MAX_GLOWS]
        if not found:
            return
        cam = viewport.camera
        height = max(1, int(viewport.height()))
        fov = math.radians(float(getattr(cam, "fov_deg", 45.0)))
        eye = cam.eye()
        strength = float(_STATE["strength"])
        pool = bool(_STATE["pool"])

        points = [[c.x(), c.y(), c.z()] for c, _r, _s, _o in found]
        px, py, front = viewport.world_to_pixels(points)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setCompositionMode(QPainter.CompositionMode_Plus)
        painter.setPen(Qt.NoPen)
        for i, (centre, radius, spec, owners) in enumerate(found):
            if owners and not _owners_visible(scene, owners):
                continue
            if not bool(front[i]):
                continue
            x, y = float(px[i]), float(py[i])
            if not (math.isfinite(x) and math.isfinite(y)):
                continue
            if getattr(cam, "perspective", True):
                distance = max((centre - eye).length(), 0.05)
            else:
                distance = max(float(getattr(cam, "distance", 20.0)), 0.05)
            px_per_m = height / (2.0 * distance * math.tan(fov / 2.0))
            core = max(radius * px_per_m, 1.2)
            peak = min(0.85, 0.42 * float(spec["intensity"]) * strength)

            # Three smooth layers: a tight core, the halo, and a wide bloom.
            # Each fades to zero on its own, so nothing is clipped.
            _smooth_glow(painter, x, y, max(core * 1.4, 2.0),
                         (1.0, 1.0, 1.0), peak * 0.95)
            _smooth_glow(painter, x, y, max(core * 4.2, 8.0),
                         spec["rgb"], peak * 0.75)
            _smooth_glow(painter, x, y, max(core * 9.0, 18.0),
                         spec["rgb"], peak * 0.26)

            if pool and centre.z() > 0.4:
                gpx, gpy, gfront = viewport.world_to_pixels(
                    [[centre.x(), centre.y(), 0.0]])
                gx, gy = float(gpx[0]), float(gpy[0])
                if bool(gfront[0]) and math.isfinite(gx) and math.isfinite(gy):
                    _smooth_glow(painter, gx, gy, max(core * 5.0, 20.0),
                                 spec["rgb"], peak * 0.30, steps=12,
                                 squash=0.42)
        painter.restore()
    except Exception:
        # An overlay must never break the frame; the host would drop it for
        # the rest of the session.
        return


def _paint_glow_on_image(app, viewport, image) -> None:
    """The glow painted onto a finished off-screen picture.

    ``Viewport.render_image`` renders the 3D scene into a framebuffer and
    never runs the widget's 2D pass, where extension overlays live -- so
    Export image and the sheet composer's frames came back without the
    glow. The overlay's coordinates are the widget's logical pixels and
    the camera is the one the caller left on the viewport (the live camera
    for an export, the frame's own inside the composer), so the picture is
    painted exactly the way ``render_image`` scales its own annotation
    pass: widget pixels onto the image, proportionally thickened."""
    if image is None or image.isNull():
        return
    lw = max(int(viewport.width()), 1)
    lh = max(int(viewport.height()), 1)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.scale(image.width() / lw, image.height() / lh)
        draw_glow(app, viewport, painter)
    finally:
        painter.end()


def _hook_offscreen_glow(app) -> None:
    """Wrap ``Viewport.render_image`` so the glow reaches every picture the
    host renders off the widget: Export image, the composer's raster frames
    (``sombreado``, ``tecnico``, ``lineas``, a style, a perspective), and
    the PDF / image / print of a sheet, which are built from those same
    cached pictures. A vector frame keeps its line-art paper: it renders
    through the hidden-line pass and never calls ``render_image``.

    Patched once on the CLASS, so every viewport works -- the project tabs
    and the multi-view extras included -- and never twice: the extension
    manager applies an enable or disable on restart, and the guard would
    keep a second ``setup`` single anyway. The wrapper adds nothing to the
    contract: whatever ``render_image`` returns comes back, glow on it."""
    from views.viewport import Viewport
    if getattr(Viewport.render_image, "_emissive_lights", False):
        return
    original = Viewport.render_image

    def render_image_with_glow(self, *args, **kwargs):
        image = original(self, *args, **kwargs)
        try:
            _paint_glow_on_image(app, self, image)
        except Exception:
            pass          # a picture must never fail because of the glow
        return image

    render_image_with_glow._emissive_lights = True
    Viewport.render_image = render_image_with_glow


# ---------------------------------------------------------------------------
# 5. The panel
# ---------------------------------------------------------------------------

def _swatch(rgb: tuple) -> QIcon:
    pm = QPixmap(16, 16)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(QPen(QColor(0, 0, 0, 70), 1.0))
    p.setBrush(QColor.fromRgbF(*rgb))
    p.drawEllipse(1.5, 1.5, 13.0, 13.0)
    p.end()
    return QIcon(pm)


class LightsPanel(QWidget):
    """Pick a light type, paint it on the selection, tune the glow."""

    def __init__(self, app) -> None:
        super().__init__()
        self._app = app
        self._current = next(iter(PRESETS))
        self._custom = None
        self._build()

    # ---- UI ----------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        hint = QLabel(tr("Select faces or an object, then pick a light type."))
        hint.setWordWrap(True)
        root.addWidget(hint)

        grid = QGridLayout()
        grid.setSpacing(4)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for i, (name, spec) in enumerate(PRESETS.items()):
            btn = QPushButton(name[len(PREFIX):])
            btn.setCheckable(True)
            btn.setIcon(_swatch(spec["rgb"]))
            btn.setToolTip(tr("Paint the selection with {name}.", name=name))
            btn.clicked.connect(lambda _=False, n=name: self._choose(n))
            self._group.addButton(btn)
            grid.addWidget(btn, i // 2, i % 2)
            if name == self._current:
                btn.setChecked(True)
        root.addLayout(grid)

        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Colour")))
        self._colour_btn = QPushButton(tr("Type colour"))
        self._colour_btn.setIcon(_swatch(self._current_rgb()))
        self._colour_btn.setToolTip(tr(
            "Pick a colour of your own for this light."))
        self._colour_btn.clicked.connect(self._pick_colour)
        row.addWidget(self._colour_btn, 1)
        self._reset_btn = QPushButton(tr("Reset"))
        self._reset_btn.setToolTip(tr(
            "Back to the colour of the selected light type."))
        self._reset_btn.clicked.connect(self._reset_colour)
        row.addWidget(self._reset_btn)
        root.addLayout(row)

        self._apply_btn = QPushButton(tr("Apply to selection"))
        self._apply_btn.clicked.connect(self._apply)
        root.addWidget(self._apply_btn)

        self._clear_btn = QPushButton(tr("Clear light paint"))
        self._clear_btn.setToolTip(tr(
            "Take the light material off the selected faces (undoable)."))
        self._clear_btn.clicked.connect(self._clear)
        root.addWidget(self._clear_btn)

        row = QHBoxLayout()
        self._glow = QCheckBox(tr("Smooth glow"))
        self._glow.setChecked(bool(int(QSettings().value(
            "extensions/emissive_lights/glow", 1))))
        self._glow.toggled.connect(self._on_glow)
        row.addWidget(self._glow)
        row.addStretch(1)
        root.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Strength")))
        self._strength = QSlider(Qt.Horizontal)
        self._strength.setRange(20, 200)
        self._strength.setValue(int(QSettings().value(
            "extensions/emissive_lights/strength", 100)))
        self._strength.valueChanged.connect(self._on_strength)
        row.addWidget(self._strength, 1)
        root.addLayout(row)

        self._pool = QCheckBox(tr("Pool of light on the ground"))
        self._pool.setChecked(bool(int(QSettings().value(
            "extensions/emissive_lights/pool", 1))))
        self._pool.toggled.connect(self._on_pool)
        root.addWidget(self._pool)

        self._count = QLabel("")
        self._count.setWordWrap(True)
        root.addWidget(self._count)
        root.addStretch(1)

        self._on_glow(self._glow.isChecked())
        self._on_strength(self._strength.value())
        self._on_pool(self._pool.isChecked())

    # ---- state -------------------------------------------------------
    def _choose(self, name: str) -> None:
        self._current = name
        self._custom = None
        self._update_colour_button()

    def _current_rgb(self) -> tuple:
        return self._custom or PRESETS[self._current]["rgb"]

    def _pick_colour(self) -> None:
        start = QColor.fromRgbF(*self._current_rgb())
        picked = QColorDialog.getColor(start, self, tr("Light colour"))
        if not picked.isValid():
            return
        self._custom = (picked.redF(), picked.greenF(), picked.blueF())
        self._update_colour_button()
        # Already-lit faces follow the pick at once: choosing a colour and
        # seeing nothing happen is the one thing this control must not do.
        lit = [f for f in selected_faces(self._app.scene)
               if str(f.attrs.get("mat") or "").startswith(PREFIX)]
        if lit:
            self._apply()

    def _reset_colour(self) -> None:
        self._custom = None
        self._update_colour_button()

    def _update_colour_button(self) -> None:
        self._colour_btn.setIcon(_swatch(self._current_rgb()))
        self._colour_btn.setText(
            tr("Custom colour") if self._custom is not None
            else tr("Type colour"))

    def _on_glow(self, on: bool) -> None:
        _STATE["glow"] = bool(on)
        QSettings().setValue("extensions/emissive_lights/glow", 1 if on else 0)
        self._app.viewport.update()

    def _on_strength(self, value: int) -> None:
        _STATE["strength"] = max(0.2, value / 100.0)
        QSettings().setValue("extensions/emissive_lights/strength", int(value))
        self._app.viewport.update()

    def _on_pool(self, on: bool) -> None:
        _STATE["pool"] = bool(on)
        QSettings().setValue("extensions/emissive_lights/pool", 1 if on else 0)
        self._app.viewport.update()

    # ---- actions -----------------------------------------------------
    def _apply(self) -> None:
        app = self._app
        viewport = app.viewport
        scene = app.scene
        faces = selected_faces(scene)
        if not faces:
            viewport.flash_status(
                tr("Select a face or an object first, then apply the light."),
                3000)
            return
        rgb = self._current_rgb()
        name = self._current if self._custom is None else CUSTOM
        face = _face_rgb(rgb)
        viewport.history.execute(SetEmissiveCommand(faces, name, face))
        viewport.notify_scene_changed()
        viewport.flash_status(
            tr("{n} face(s) painted with {name}.",
               n=len(faces), name=name), 3000)
        self.refresh()

    def _clear(self) -> None:
        app = self._app
        viewport = app.viewport
        scene = app.scene
        faces = [f for f in selected_faces(scene)
                 if str(f.attrs.get("mat") or "").startswith(PREFIX)]
        if not faces:
            viewport.flash_status(
                tr("The selection wears no light material."), 3000)
            return
        viewport.history.execute(ClearEmissiveCommand(faces))
        viewport.notify_scene_changed()
        viewport.flash_status(
            tr("Light paint removed from {n} face(s).", n=len(faces)), 3000)
        self.refresh()

    # ---- refresh -----------------------------------------------------
    def refresh(self) -> None:
        try:
            scene = self._app.scene
            total = len(lights(scene))
            picked = len(selected_faces(scene))
            self._count.setText(
                tr("{lights} light(s) · {faces} face(s) selected.",
                   lights=total, faces=picked))
            self._apply_btn.setEnabled(picked > 0)
        except Exception:
            self._count.setText("")


# ---------------------------------------------------------------------------
# 6. Entry point
# ---------------------------------------------------------------------------

def setup(app) -> None:
    panel = LightsPanel(app)
    dock = app.add_panel(tr("Lights"), panel)
    app.add_menu_action(
        tr("Emissive lights\u2026"),
        lambda: app.show_panel(dock),
        tip=tr("Paint faces with light-emitting materials and see them glow."))
    app.add_overlay(lambda vp, painter: draw_glow(app, vp, painter))
    _hook_offscreen_glow(app)
    app.on_document_changed(panel.refresh)
    panel.refresh()
