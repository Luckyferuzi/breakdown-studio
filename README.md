# BreakdownStudio 3D Pro — Multi-Pass Wireframe, Clay & Final PBR Compositing Suite

**BreakdownStudio 3D Pro** (`v2.0.0`) is a Python production pipeline add-on for **Blender 4.2+ / 5.x** that automates multi-pass 3D portfolio breakdowns—generating synchronized **Wireframe**, **Clay/Matcap**, and **Final PBR** render passes, 3-way split breakdown posters, turntable animations, and wipe transition videos in a single click.

---

## Core Engineering Features

* **Automated Multi-Pass Render Engine (`core.py`):**
  * Non-destructive scene state snapshotting and restoration.
  * Automated generation of synchronized **Final PBR**, **Studio Clay**, and **Freestyle / Geometry Wireframe** passes across still renders and 360° turntables.
* **Procedural Compositor & Wipe Video Pipeline (`compositor.py`):**
  * Programmatic construction of Blender Compositor node graphs for split-screen posters, animated wipe transitions, pass labels, and FFmpeg MP4 encoding.
* **Poly Haven Cloud Asset Integration (`polyhaven.py`):**
  * Asynchronous REST integration with the Poly Haven API for on-demand downloading and caching of CC0 studio HDRIs and benchmark 3D assets.
* **Cross-Version Blender 4.2 / 5.x Compatibility (`__init__.py`, `blender_manifest.toml`):**
  * Compliant with Blender's modern Extension Manifest (`schema_version = "1.0.0"`) and legacy add-on registration.

---

## Project Structure

```text
breakdown_studio/
├── __init__.py             # Add-on registration, operators, and N-panel UI
├── blender_manifest.toml   # Blender 4.2+ / 5.x extension manifest
├── core.py                 # Multi-pass rendering, camera framing & state management
├── compositor.py           # Automated node-graph compositing & wipe video encoding
├── polyhaven.py            # Poly Haven REST API client & HDRI/model cache
├── license_guard.py        # Edition & activation verification module
└── LICENSE.txt             # GPL-3.0-or-later license
```

---

## Installation
1. Download or package the `breakdown_studio` folder as a `.zip` file.
2. In Blender, go to **Edit → Preferences → Add-ons → Install from Disk...**
3. Select the `.zip` archive and enable **BreakdownStudio 3D Pro**.

---

## Author
**Khalifa Feruzi Kahamba** (`@Luckyferuzi`)
BSc. Software Engineering
[GitHub Portfolio](https://github.com/Luckyferuzi)
