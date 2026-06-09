bl_info = {
    "name": "UpFrameG",
    "author": "Sloemo",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > UpFrameG",
    "description": "Post-process render sequences with Real-ESRGAN upscaling, RIFE interpolation, and FFmpeg encoding asynchronously.",
    "warning": "",
    "doc_url": "",
    "category": "Render",
}

import sys
import importlib

# Clean reload of submodules to bypass Python's sys.modules caching
submodules = ["preferences", "upscale", "interpolate", "encode", "operators", "ui"]
for sub in submodules:
    full_name = f"{__package__}.{sub}" if __package__ else sub
    if full_name in sys.modules:
        try:
            importlib.reload(sys.modules[full_name])
        except Exception as e:
            print(f"[UpFrameG] Failed to reload submodule {full_name}: {e}")

from . import preferences
from . import upscale
from . import interpolate
from . import encode
from . import operators
from . import ui

import bpy

def register():
    preferences.register()
    operators.register()
    ui.register()

def unregister():
    ui.unregister()
    operators.unregister()
    preferences.unregister()

if __name__ == "__main__":
    register()
