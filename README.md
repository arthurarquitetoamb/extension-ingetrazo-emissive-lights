[README.md](https://github.com/user-attachments/files/34666636/README.md)
# Emissive Lights for IngeTrazo

<img width="489" height="346" alt="emissive_lights" src="https://github.com/user-attachments/assets/cf7a7592-49ee-4a59-ad4b-fad3c0245733" />

Emissive light **materials** for IngeTrazo: paint a face and it reads as a
source of light, with a **smooth glow** drawn in the viewport.

IngeTrazo lights the model with one sun and has no lamps. This extension adds
the other half of a lighting setup, the SketchUp way: a face painted with a
*light material* becomes the light.

Version 1.0.0 · GPL-3.0-or-later · IngeTrazo 0.5.x (API 2)

## Light types

| Material | Colour temperature | Intensity |
|---|---|---|
| `Light · Candle` | 1900 K | 0.55 |
| `Light · Warm bulb` | 2700 K | 1.00 |
| `Light · Neutral` | 4000 K | 1.10 |
| `Light · Cool LED` | 6500 K | 1.20 |
| `Light · Neon` | fixed magenta | 1.40 |
| `Light · Screen` | 8000 K | 0.85 |

Colours come from a black-body (Kelvin → RGB) approximation; the face itself is
painted a lightened version of that colour, so it reads as a lamp even with the
glow turned off.

## Install

Copy `emissive_lights.py` into the per-user plugins folder and restart
IngeTrazo:

- **Windows** — `%APPDATA%\ingetrazo\plugins\`
- **Linux** — `~/.local/share/ingetrazo/plugins/` (or `$XDG_DATA_HOME/ingetrazo/plugins/`)
- **macOS** — `~/Library/Application Support/ingetrazo/plugins/`

Or use **Extensions → Open plugins folder** inside IngeTrazo, drop the file
there, and restart.

The plugin then appears as:

- a **Lights** tab in the side tray;
- **Extensions → Emissive lights…**, which brings that tab forward.

## Use

1. Select faces, or a whole object.
2. Pick a light type in the **Lights** panel.
3. Press **Apply to selection**.

**Clear light paint** takes the material off the selected faces. Both actions
are a single undo step — `Ctrl+Z` works as usual.

### Assigning a colour

The **Colour** button opens the system colour picker: pick any hue and the glow,
the face and the pool of light all follow it. The light is then registered as
`Light · Custom`.

- **Reset** goes back to the colour of the selected type.
- Clicking a type button also drops the custom colour, so the type's own colour
  is used again.
- The exact colour is stored on the face (`attrs["color"]`), which is what the
  `.igz` keeps — so a custom light reopens with the colour it had.
- If the selection already wears a light, picking a colour repaints it at once;
  no second click on **Apply**.

Display options (saved with your preferences):

- **Smooth glow** — draw the halo at all.
- **Strength** — 20 % to 200 %.
- **Pool of light on the ground** — the soft ellipse under the lamp.

## How it works

### The material

- Registered in the document (`scene.materials`) and carried by the face as
  `attrs["mat"]`, so it survives save, copy, export and reopen.
- `SetEmissiveCommand` / `ClearEmissiveCommand` go through the command layer:
  one undo step, and the registry entry this extension created is removed on
  undo.
- Every name starting with `Light · ` is a light. Presets carry their own
  colour and intensity; a `Light · Custom` face recovers its glow colour from
  the face colour, undoing the lift that made it bright.
- **A light material replaces the paint.** Applying it takes the face's
  `texture` off, because a textured face is drawn with its image and the
  colour would never show — the bug behind "the colour does not appear".
  Undo puts the texture back. This is how every material in IngeTrazo
  behaves: the keys a material does not carry are removed.

### The glow

Three additive layers with a Gaussian falloff — `exp(-3.4·t²)·(1-t)^0.7` —
so the alpha reaches **zero exactly at the radius**: no rim, no hard edge, no
cut. Each light is sized from the real radius of its object projected on
screen, in perspective and parallel projection alike.

| Layer | Radius | Role |
|---|---|---|
| core | `core × 1.4` | the white filament |
| halo | `core × 4.2` | the lamp's light |
| bloom | `core × 9.0` | the wide spread |

The list of lights is cached per `scene.version`: editing the model recomputes
it, orbiting the camera only re-projects it. At most 120 glows are drawn.

## Changes

- **2.4.0** — **the glow reaches the sheets and the exports.** It used to be
  drawn only over the live model viewport: a sheet's frames, Export image and
  everything printed or exported from a sheet came back without it, because
  those pictures are rendered off-screen, where the host never paints
  extension overlays. `Viewport.render_image` is now wrapped once and the
  glow is painted onto the finished picture, with the camera the caller set
  (the frame's own camera for a sheet). A **vector** frame keeps its clean
  line-art paper. Two fidelity fixes came with it: a light inside a
  **component instance** glows where the copy stands (not at the prototype),
  and the glow now follows **visibility** — an object hidden by Hide, by a
  scene, or carried by a layer that is off (or set not to print) does not
  glow.
- **2.3.0** — **the colour shows now.** A face with a texture was drawn with
  its image and the light colour never appeared; applying a light takes the
  texture off (undo restores it). The face colour is a touch more saturated
  (`0.20 + 0.80·c`, exactly reversible), and picking a colour on an
  already-lit selection repaints it immediately.
- **2.2.0** — a **Colour** control: pick any colour for the light, stored on
  the face as `Light · Custom`, with **Reset** back to the type colour.
- **2.1.0** — the projected shadow silhouettes were removed; the smooth glow
  stays, now with the pool drawn as a proper flattened gradient.
- **2.0.0** — smooth Gaussian glow (no clipped rim).
- **1.0.0** — first release: six light materials and a radial glow.

## Limitations

- **The glow is a screen simulation.** It is drawn over the picture and does
  not light other faces: the shader still has only the sun. For physically
  lit images use **Render with Blender** and add real lamps there.
- **No cast shadows.** A light does not darken the objects behind it; adding
  that would need the core's shader, not a plugin.
- **The material name is the identity.** Renaming a `Light · …` material turns
  it back into an ordinary bright material; it stops glowing.
- **One registry entry for all custom colours.** `Light · Custom` is a single
  material whose swatch shows the last colour painted; the colour that matters
  lives on each face.
- **A light takes the texture off the face.** That is what makes the colour
  visible on an imported model; if the face was a photo or a neon sign, its
  image is replaced. Undo brings it back.
- A model painted all over with light material is not what this is for — use it
  for lamps, windows at night, neon signs and screens.

## Uninstall

Delete `emissive_lights.py` from the plugins folder and restart IngeTrazo.
Materials already painted stay in the document as ordinary bright materials.
