import os
import sys
import bpy

def get_addon_name():
    return __package__ if __package__ else "upframeg"

class AIPostProcessPreferences(bpy.types.AddonPreferences):
    bl_idname = get_addon_name()

    realesrgan_path: bpy.props.StringProperty(
        name="Real-ESRGAN Path",
        description="Path to the Real-ESRGAN executable (e.g. realesrgan-ncnn-vulkan.exe)",
        subtype='FILE_PATH',
        default=""
    )

    rife_path: bpy.props.StringProperty(
        name="RIFE Path",
        description="Path to the RIFE executable (e.g. rife-ncnn-vulkan.exe)",
        subtype='FILE_PATH',
        default=""
    )

    ffmpeg_path: bpy.props.StringProperty(
        name="FFmpeg Path",
        description="Path to the FFmpeg executable (e.g. ffmpeg.exe)",
        subtype='FILE_PATH',
        default="ffmpeg"
    )

    cache_dir: bpy.props.StringProperty(
        name="Cache Directory",
        description="Directory to store intermediate frames. If blank, uses a subfolder in the blend file directory",
        subtype='DIR_PATH',
        default=""
    )

    auto_clean_cache: bpy.props.BoolProperty(
        name="Auto Clean Cache",
        description="Delete intermediate frame caches (upscaled/interpolated) after encoding completes",
        default=False
    )

    has_run_setup: bpy.props.BoolProperty(
        name="Has Run Setup",
        description="Whether the first-time setup has been run",
        default=False
    )

    # Download state properties
    download_progress: bpy.props.FloatProperty(
        name="Download Progress",
        default=0.0,
        min=0.0,
        max=100.0
    )

    download_status: bpy.props.StringProperty(
        name="Download Status",
        default=""
    )

    is_downloading: bpy.props.BoolProperty(
        name="Is Downloading",
        default=False
    )

    active_download: bpy.props.StringProperty(
        name="Active Download",
        default=""
    )

    def auto_detect_binaries(self):
        # Only run if something is not set/valid
        is_realesrgan_valid = self.realesrgan_path and os.path.isfile(bpy.path.abspath(self.realesrgan_path))
        is_rife_valid = self.rife_path and os.path.isfile(bpy.path.abspath(self.rife_path))
        
        is_ffmpeg_valid = False
        if self.ffmpeg_path:
            if self.ffmpeg_path == "ffmpeg":
                is_ffmpeg_valid = True
            else:
                is_ffmpeg_valid = os.path.isfile(bpy.path.abspath(self.ffmpeg_path))
                
        if is_realesrgan_valid and is_rife_valid and is_ffmpeg_valid:
            return False

        config_dir = bpy.utils.user_resource('CONFIG', create=True)
        binaries_dir = os.path.join(config_dir, "upframeg_binaries")
        if not os.path.exists(binaries_dir):
            return False
            
        is_win = sys.platform.startswith("win")
        
        realesrgan_name = "realesrgan-ncnn-vulkan.exe" if is_win else "realesrgan-ncnn-vulkan"
        rife_name = "rife-ncnn-vulkan.exe" if is_win else "rife-ncnn-vulkan"
        ffmpeg_name = "ffmpeg.exe" if is_win else "ffmpeg"
        
        found_realesrgan = None
        found_rife = None
        found_ffmpeg = None
        
        for root, dirs, files in os.walk(binaries_dir):
            if not is_realesrgan_valid and realesrgan_name in files:
                found_realesrgan = os.path.abspath(os.path.join(root, realesrgan_name))
            if not is_rife_valid and rife_name in files:
                found_rife = os.path.abspath(os.path.join(root, rife_name))
            if not is_ffmpeg_valid and ffmpeg_name in files:
                found_ffmpeg = os.path.abspath(os.path.join(root, ffmpeg_name))
                
        changes_made = False
        if found_realesrgan:
            self.realesrgan_path = found_realesrgan
            changes_made = True
        if found_rife:
            self.rife_path = found_rife
            changes_made = True
        if found_ffmpeg:
            self.ffmpeg_path = found_ffmpeg
            changes_made = True
            
        return changes_made

    def draw(self, context):
        layout = self.layout
        
        # Header/Description
        box = layout.box()
        col = box.column(align=True)
        col.label(text="AI Post Processing Addon Configuration", icon='PREFERENCES')
        col.label(text="Configure the paths to the external executables required for upscaling, interpolation, and encoding.", icon='INFO')
        
        # Download progress panel
        if self.is_downloading:
            dl_box = layout.box()
            dl_col = dl_box.column(align=True)
            dl_col.label(text=f"Status: {self.download_status}", icon='IMPORT')
            dl_col.progress(factor=self.download_progress / 100.0, text=f"{self.download_progress:.1f}%")
            dl_col.separator()
        
        # Executables section
        box = layout.box()
        col = box.column(align=True)
        col.label(text="External Executables Paths", icon='CONSOLE')
        col.separator()
        
        # Real-ESRGAN Row
        row = col.row(align=True)
        row.prop(self, "realesrgan_path")
        is_realesrgan_valid = self.realesrgan_path and os.path.isfile(bpy.path.abspath(self.realesrgan_path))
        if is_realesrgan_valid:
            row.label(text="", icon='CHECKMARK')
        else:
            row.label(text="", icon='ERROR' if self.realesrgan_path else 'QUESTION')
            if not self.is_downloading:
                op = row.operator("aipost.download_binary", text="Download", icon='IMPORT')
                op.binary_type = "realesrgan"
            
        # RIFE Row
        row = col.row(align=True)
        row.prop(self, "rife_path")
        is_rife_valid = self.rife_path and os.path.isfile(bpy.path.abspath(self.rife_path))
        if is_rife_valid:
            row.label(text="", icon='CHECKMARK')
        else:
            row.label(text="", icon='ERROR' if self.rife_path else 'QUESTION')
            if not self.is_downloading:
                op = row.operator("aipost.download_binary", text="Download", icon='IMPORT')
                op.binary_type = "rife"
            
        # FFmpeg Row
        row = col.row(align=True)
        row.prop(self, "ffmpeg_path")
        ffmpeg_resolved = bpy.path.abspath(self.ffmpeg_path)
        is_ffmpeg_valid = (self.ffmpeg_path and os.path.isfile(ffmpeg_resolved)) or self.ffmpeg_path == "ffmpeg"
        if is_ffmpeg_valid:
            row.label(text="", icon='CHECKMARK')
        else:
            row.label(text="", icon='ERROR')
            if not self.is_downloading:
                op = row.operator("aipost.download_binary", text="Download", icon='IMPORT')
                op.binary_type = "ffmpeg"

        # Auto-detect Binaries button
        col.separator()
        row = col.row(align=True)
        row.operator("aipost.auto_detect_binaries", text="Auto-detect Downloaded Binaries", icon='FILE_REFRESH')

        # Cache settings
        box = layout.box()
        col = box.column(align=True)
        col.label(text="Cache & Directory Settings", icon='FILE_FOLDER')
        col.separator()
        
        row = col.row(align=True)
        row.prop(self, "cache_dir")
        if self.cache_dir and os.path.isdir(bpy.path.abspath(self.cache_dir)):
            row.label(text="", icon='CHECKMARK')
        else:
            row.label(text="", icon='QUESTION')
            
        col.prop(self, "auto_clean_cache")

def auto_detect_on_startup():
    try:
        addon_name = get_addon_name()
        prefs = bpy.context.preferences.addons[addon_name].preferences
        
        # 1. Run auto-detection first in case they already have the files
        prefs.auto_detect_binaries()
        
        # 2. Check if setup should run
        if not prefs.has_run_setup:
            is_realesrgan_valid = prefs.realesrgan_path and os.path.isfile(bpy.path.abspath(prefs.realesrgan_path))
            is_rife_valid = prefs.rife_path and os.path.isfile(bpy.path.abspath(prefs.rife_path))
            
            is_ffmpeg_valid = False
            if prefs.ffmpeg_path:
                if prefs.ffmpeg_path == "ffmpeg":
                    import shutil
                    if shutil.which("ffmpeg"):
                        is_ffmpeg_valid = True
                else:
                    is_ffmpeg_valid = os.path.isfile(bpy.path.abspath(prefs.ffmpeg_path))
                    
            if not (is_realesrgan_valid and is_rife_valid and is_ffmpeg_valid):
                bpy.ops.aipost.auto_setup()
                
            # Set flag to True so we don't repeat this check on subsequent launches
            prefs.has_run_setup = True
            
    except Exception as e:
        print(f"[UpFrameG] Startup binary auto-setup/detection skipped: {e}")
    return None

def register():
    bpy.utils.register_class(AIPostProcessPreferences)
    bpy.app.timers.register(auto_detect_on_startup, first_interval=0.1)

def unregister():
    bpy.utils.unregister_class(AIPostProcessPreferences)

