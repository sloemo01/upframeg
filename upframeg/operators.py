import os
import shutil
import tempfile
import threading
import re
import json
import time
import subprocess
import zipfile
import urllib.request
import bpy

from .upscale import run_upscale
from .interpolate import run_interpolate
from .encode import run_encode
from .preferences import get_addon_name

# OS Balloon Toast Notification Helper
def show_toast_notification(title, message):
    import sys
    if os.name == 'nt':
        escaped_title = title.replace("'", "''")
        escaped_message = message.replace("'", "''")
        ps_script = f"""
        [void] [System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms')
        $notification = New-Object System.Windows.Forms.NotifyIcon
        $notification.Icon = [System.Drawing.SystemIcons]::Information
        $notification.BalloonTipTitle = '{escaped_title}'
        $notification.BalloonTipText = '{escaped_message}'
        $notification.Visible = $true
        $notification.ShowBalloonTip(5000)
        """
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", ps_script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
        except Exception as e:
            print(f"Failed to show OS notification: {e}")
    elif sys.platform.startswith("darwin"):
        escaped_title = title.replace('"', '\\"')
        escaped_message = message.replace('"', '\\"')
        as_script = f'display notification "{escaped_message}" with title "{escaped_title}"'
        try:
            subprocess.Popen(
                ["osascript", "-e", as_script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            print(f"Failed to show macOS notification: {e}")

# Binary Downloader State
class AIPostDownloadState:
    def __init__(self):
        self.progress = 0.0
        self.status = "IDLE"
        self.active_download = ""  # 'realesrgan', 'rife', 'ffmpeg'
        self.is_downloading = False
        self.error_message = ""
        self.found_path = ""
        self.found_paths = {}

_download_state = AIPostDownloadState()
_download_thread = None

def download_binary_thread(binary_types, target_dir):
    global _download_state
    
    # Support both single string or list of strings
    if isinstance(binary_types, str):
        binary_types = [binary_types]
        
    _download_state.is_downloading = True
    _download_state.progress = 0.0
    _download_state.found_paths = {}
    
    import sys
    is_mac = sys.platform.startswith("darwin")
    if is_mac:
        urls = {
            'realesrgan': "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-macos.zip",
            'rife': "https://github.com/nihui/rife-ncnn-vulkan/releases/download/20221029/rife-ncnn-vulkan-20221029-macos.zip",
            'ffmpeg': "https://evermeet.cx/ffmpeg/getrelease/zip"
        }
    else:
        urls = {
            'realesrgan': "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip",
            'rife': "https://github.com/nihui/rife-ncnn-vulkan/releases/download/20221029/rife-ncnn-vulkan-20221029-windows.zip",
            'ffmpeg': "https://github.com/GyanD/codexffmpeg/releases/download/8.1.1/ffmpeg-8.1.1-essentials_build.zip"
        }
    
    total_types = len(binary_types)
    
    for idx, binary_type in enumerate(binary_types):
        _download_state.active_download = binary_type
        _download_state.status = f"Downloading {binary_type} ({idx+1}/{total_types})..."
        _download_state.progress = (idx / total_types) * 100.0
        
        url = urls.get(binary_type)
        if not url:
            _download_state.status = "ERROR"
            _download_state.error_message = f"Unknown binary type: {binary_type}"
            _download_state.is_downloading = False
            return
            
        try:
            os.makedirs(target_dir, exist_ok=True)
            zip_path = os.path.join(target_dir, f"temp_{binary_type}.zip")
            
            # Download chunk-by-chunk with progress
            req = urllib.request.Request(
                url, 
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            )
            with urllib.request.urlopen(req) as response:
                total_size = int(response.info().get('Content-Length', 0))
                downloaded = 0
                block_size = 1024 * 512 # 512 KB
                with open(zip_path, 'wb') as f:
                    while True:
                        chunk = response.read(block_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            sub_prog = downloaded / total_size
                            overall_prog = ((idx + sub_prog) / total_types) * 100.0
                            _download_state.progress = overall_prog
                            _download_state.status = f"Downloading {binary_type} ({idx+1}/{total_types}): {sub_prog * 100.0:.1f}%"
                            
            _download_state.status = f"Extracting {binary_type} ({idx+1}/{total_types})...."
            _download_state.progress = ((idx + 0.95) / total_types) * 100.0
            
            extract_dir = os.path.join(target_dir, binary_type)
            os.makedirs(extract_dir, exist_ok=True)
            
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
                
            try:
                os.remove(zip_path)
            except Exception:
                pass
                
            import sys
            is_win = sys.platform.startswith("win")
            exec_name = {
                'realesrgan': "realesrgan-ncnn-vulkan.exe" if is_win else "realesrgan-ncnn-vulkan",
                'rife': "rife-ncnn-vulkan.exe" if is_win else "rife-ncnn-vulkan",
                'ffmpeg': "ffmpeg.exe" if is_win else "ffmpeg"
            }[binary_type]
            
            found_path = None
            for root, dirs, files in os.walk(extract_dir):
                if exec_name in files:
                    found_path = os.path.abspath(os.path.join(root, exec_name))
                    break
                    
            if found_path:
                if not is_win:
                    try:
                        import stat
                        st = os.stat(found_path)
                        os.chmod(found_path, st.st_mode | stat.S_IEXEC)
                    except Exception as perm_err:
                        print(f"[UpFrameG] Warning: Could not set execute permission on {found_path}: {perm_err}")
                    
                    if sys.platform.startswith("darwin"):
                        try:
                            # Remove macOS Gatekeeper quarantine flag to run binary automatically
                            subprocess.run(["xattr", "-d", "com.apple.quarantine", found_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        except Exception as xattr_err:
                            print(f"[UpFrameG] Warning: Could not clear macOS quarantine attribute: {xattr_err}")
                _download_state.found_paths[binary_type] = found_path
                _download_state.found_path = found_path
            else:
                _download_state.status = "ERROR"
                _download_state.error_message = f"Executable {exec_name} not found in extracted files."
                _download_state.is_downloading = False
                return
                
        except Exception as e:
            _download_state.status = "ERROR"
            _download_state.error_message = str(e)
            _download_state.is_downloading = False
            return
            
    _download_state.status = "FINISHED"
    _download_state.progress = 100.0

# Persistent Presets Storage Helpers
def get_presets_filepath():
    config_dir = bpy.utils.user_resource('CONFIG', create=True)
    presets_dir = os.path.join(config_dir, "upframeg_presets")
    os.makedirs(presets_dir, exist_ok=True)
    return os.path.join(presets_dir, "custom_presets.json")

def load_custom_presets():
    filepath = get_presets_filepath()
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_custom_presets(presets):
    filepath = get_presets_filepath()
    try:
        with open(filepath, 'w') as f:
            json.dump(presets, f, indent=4)
    except Exception as e:
        print(f"Failed to save custom presets: {e}")

def get_preset_items_cb(self, context):
    items = [
        ('CUSTOM', 'Custom', 'Manual adjustments allowed'),
        ('YOUTUBE_4K', 'YouTube 4K', '4x Upscaling + 2x Interpolation + H.264 High Quality (60 FPS)'),
        ('MOTION_GRAPHICS', 'Motion Graphics', '4x Anime Upscaling + 4x Interpolation + ProRes HQ (60 FPS)'),
        ('FAST_PREVIEW', 'Fast Preview', '2x Fast Upscaling + No Interpolation + H.264 Medium Quality')
    ]
    custom = load_custom_presets()
    for preset_id, preset_data in custom.items():
        items.append((preset_id, preset_data['name'], preset_data['description']))
    return items

class PostProcessState:
    def __init__(self):
        self.status = "IDLE"          # IDLE, UPSCALING, INTERPOLATING, ENCODING, FINISHED, CANCELLED, ERROR
        self.progress = 0.0          # 0.0 to 100.0
        self.current_frame = 0
        self.total_frames = 0
        self.eta = "00:00:00"
        self.logs = []               # list of log strings
        self.cancel_requested = False
        self.error_message = ""

# Global worker variables
_state = None
_thread = None

def get_preferences(context):
    # Scan for any registered addon keys matching our addon package name
    for key in context.preferences.addons.keys():
        if key.endswith("upframeg"):
            return context.preferences.addons[key].preferences
    # Fallback
    addon_name = __package__ if __package__ else "upframeg"
    if addon_name in context.preferences.addons:
        return context.preferences.addons[addon_name].preferences
    return None

def resolve_cache_dir(context):
    prefs = get_preferences(context)
    cache_dir = prefs.cache_dir if prefs else ""
    if cache_dir:
        return bpy.path.abspath(cache_dir)
        
    # Fallback to local blend file dir
    if bpy.data.is_saved:
        return bpy.path.abspath("//ai_post_cache")
    else:
        # If blend file is not saved, use temp directory
        import tempfile
        return os.path.join(tempfile.gettempdir(), "blender_ai_post_cache")

def update_mode_cb(self, context):
    mode = self.ai_post_mode
    if mode == 'FULL':
        self.ai_post_use_upscale = True
        self.ai_post_use_interpolate = True
        self.ai_post_use_encode = True
    elif mode == 'UPSCALE_ONLY':
        self.ai_post_use_upscale = True
        self.ai_post_use_interpolate = False
        self.ai_post_use_encode = False
    elif mode == 'INTERPOLATE_ONLY':
        self.ai_post_use_upscale = False
        self.ai_post_use_interpolate = True
        self.ai_post_use_encode = False
    elif mode == 'ENCODE_ONLY':
        self.ai_post_use_upscale = False
        self.ai_post_use_interpolate = False
        self.ai_post_use_encode = True
    elif mode == 'UPSCALE_ENCODE':
        self.ai_post_use_upscale = True
        self.ai_post_use_interpolate = False
        self.ai_post_use_encode = True
    elif mode == 'INTERPOLATE_ENCODE':
        self.ai_post_use_upscale = False
        self.ai_post_use_interpolate = True
        self.ai_post_use_encode = True

def update_preset_cb(self, context):
    preset = self.ai_post_preset
    if preset == 'CUSTOM':
        return
        
    if preset == 'YOUTUBE_4K':
        self.ai_post_mode = 'FULL'
        self.ai_post_use_upscale = True
        self.ai_post_upscale_scale = '4'
        self.ai_post_upscale_model = 'realesr-animevideov3'
        self.ai_post_use_interpolate = True
        self.ai_post_interpolate_mult = '2'
        self.ai_post_use_encode = True
        self.ai_post_encode_codec = 'H264'
        self.ai_post_encode_quality = 'HIGH'
        self.ai_post_encode_fps = 60
        self.ai_post_encode_gpu_accel = 'NONE'
    elif preset == 'MOTION_GRAPHICS':
        self.ai_post_mode = 'FULL'
        self.ai_post_use_upscale = True
        self.ai_post_upscale_scale = '4'
        self.ai_post_upscale_model = 'realesr-animevideov3'
        self.ai_post_use_interpolate = True
        self.ai_post_interpolate_mult = '4'
        self.ai_post_use_encode = True
        self.ai_post_encode_codec = 'PRORES'
        self.ai_post_encode_quality = 'HIGH'
        self.ai_post_encode_fps = 60
        self.ai_post_encode_gpu_accel = 'NONE'
    elif preset == 'FAST_PREVIEW':
        self.ai_post_mode = 'UPSCALE_ENCODE'
        self.ai_post_use_upscale = True
        self.ai_post_upscale_scale = '2'
        self.ai_post_upscale_model = 'realesr-animevideov3'
        self.ai_post_use_interpolate = False
        self.ai_post_use_encode = True
        self.ai_post_encode_codec = 'H264'
        self.ai_post_encode_quality = 'MEDIUM'
        self.ai_post_encode_fps = 24
        self.ai_post_encode_gpu_accel = 'NONE'
    else:
        custom = load_custom_presets()
        if preset in custom:
            p = custom[preset]
            self.ai_post_use_upscale = p.get('use_upscale', True)
            self.ai_post_upscale_scale = p.get('upscale_scale', '4')
            self.ai_post_upscale_model = p.get('upscale_model', 'realesr-animevideov3')
            self.ai_post_use_interpolate = p.get('use_interpolate', True)
            self.ai_post_interpolate_mult = p.get('interpolate_mult', '2')
            self.ai_post_use_encode = p.get('use_encode', True)
            self.ai_post_encode_codec = p.get('encode_codec', 'H264')
            self.ai_post_encode_quality = p.get('encode_quality', 'HIGH')
            self.ai_post_encode_fps = p.get('encode_fps', 24)
            self.ai_post_encode_gpu_accel = p.get('encode_gpu_accel', 'NONE')

def timer_callback():
    global _state, _thread, _download_state
    
    # 1. Sync Download State
    is_download_active = False
    if _download_state and _download_state.is_downloading:
        is_download_active = True
        prefs = get_preferences(bpy.context)
        if prefs:
            prefs.download_status = _download_state.status
            prefs.download_progress = _download_state.progress
            prefs.is_downloading = _download_state.is_downloading
            prefs.active_download = _download_state.active_download
            
            # Apply any resolved paths from the background thread safely
            if hasattr(_download_state, 'found_paths') and _download_state.found_paths:
                for b_type, path in list(_download_state.found_paths.items()):
                    path_attr = {
                        'realesrgan': 'realesrgan_path',
                        'rife': 'rife_path',
                        'ffmpeg': 'ffmpeg_path'
                    }.get(b_type)
                    if path_attr:
                        setattr(prefs, path_attr, path)
                _download_state.found_paths.clear()
            
            if _download_state.status == "FINISHED":
                show_toast_notification("UpFrameG", "Successfully downloaded and installed all components!")
                
                _download_state.is_downloading = False
                _download_state.active_download = ""
                prefs.is_downloading = False
                prefs.active_download = ""
                
            elif _download_state.status == "ERROR":
                prefs.download_status = f"Error: {_download_state.error_message}"
                show_toast_notification("UpFrameG", f"Failed to download components: {_download_state.error_message}")
                _download_state.is_downloading = False
                prefs.is_downloading = False
                _download_state.active_download = ""
                prefs.active_download = ""
                
            # Force redraw of preferences area
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == 'PREFERENCES':
                        area.tag_redraw()

    # 2. Sync Pipeline State
    if not _state:
        if not is_download_active:
            return None  # Unregister timer
        return 0.1
        
    scene = None
    if bpy.context and bpy.context.scene:
        scene = bpy.context.scene
    elif bpy.data.scenes:
        scene = bpy.data.scenes[0]
        
    if not scene:
        return 0.1

    # Sync state to Blender properties
    scene.ai_post_status = _state.status
    scene.ai_post_progress = _state.progress
    scene.ai_post_eta = _state.eta
    scene.ai_post_error_msg = _state.error_message
    
    # Sync logs
    last_lines = _state.logs[-8:]
    scene.ai_post_last_logs = "\n".join(last_lines)
    
    # Redraw UI areas to show progress updating
    if bpy.context and hasattr(bpy.context, 'window_manager'):
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type in {'VIEW_3D', 'PROPERTIES'}:
                    area.tag_redraw()

    # Handle completion
    if _state.status in {"FINISHED", "CANCELLED", "ERROR"}:
        status = _state.status
        error_msg = _state.error_message
        
        # Import movie strip to Sequencer VSE if finished
        if status == "FINISHED" and scene.ai_post_auto_import and scene.ai_post_use_encode:
            output_file = bpy.path.abspath(scene.ai_post_output_file)
            if os.path.exists(output_file):
                if not scene.sequence_editor:
                    scene.sequence_editor_create()
                try:
                    start_frame = scene.frame_start
                    if scene.ai_post_use_custom_range:
                        start_frame = scene.ai_post_frame_start
                    scene.sequence_editor.sequences.new_movie(
                        name=os.path.basename(output_file),
                        filepath=output_file,
                        channel=1,
                        frame_start=start_frame
                    )
                except Exception as e:
                    print(f"Failed to auto-import movie strip: {e}")
                    
        # Cleanup temporary audio file if exists
        if hasattr(scene, "ai_post_temp_audio_file") and scene.ai_post_temp_audio_file:
            if tempfile.gettempdir() in scene.ai_post_temp_audio_file:
                try:
                    if os.path.exists(scene.ai_post_temp_audio_file):
                        os.remove(scene.ai_post_temp_audio_file)
                except Exception:
                    pass
            scene.ai_post_temp_audio_file = ""
            
        scene.ai_post_is_running = False
        _thread = None
        _state = None
        
        # Handle batch queue sequential processing
        if scene.ai_post_is_batch_running:
            active_item = None
            for item in scene.ai_post_batch_queue:
                if item.status == 'PROCESSING':
                    active_item = item
                    break
                    
            if active_item:
                active_item.status = 'FINISHED' if status == "FINISHED" else 'FAILED'
                
            if status == "CANCELLED":
                scene.ai_post_is_batch_running = False
                show_toast_notification("UpFrameG", "Batch processing cancelled.")
            else:
                next_item = None
                for item in scene.ai_post_batch_queue:
                    if item.status == 'QUEUED':
                        next_item = item
                        break
                        
                if next_item:
                    # Setup and trigger next item
                    scene.ai_post_input_dir = next_item.input_dir
                    scene.ai_post_output_dir = next_item.output_dir
                    scene.ai_post_output_file = next_item.output_file
                    scene.ai_post_mode = next_item.mode
                    scene.ai_post_preset = next_item.preset
                    next_item.status = 'PROCESSING'
                    
                    bpy.ops.aipost.run()
                    show_toast_notification("UpFrameG", f"Batch: started processing {next_item.name}...")
                else:
                    scene.ai_post_is_batch_running = False
                    show_toast_notification("UpFrameG", "Batch Queue completed!")
        else:
            # Single run notification
            if status == "FINISHED":
                show_toast_notification("UpFrameG", "Processing completed successfully!")
            elif status == "CANCELLED":
                show_toast_notification("UpFrameG", "Processing was cancelled.")
            elif status == "ERROR":
                show_toast_notification("UpFrameG", f"Processing failed: {error_msg}")
                
        return None  # Unregister timer
        
    return 0.1

def run_pipeline(realesrgan_path, rife_path, ffmpeg_path, cache_dir, auto_clean, settings, state):
    try:
        input_dir = settings['input_dir']
        output_file = settings['output_file']
        
        # Step 1: Upscale
        current_input = input_dir
        if settings['use_upscale']:
            state.logs.append("[Pipeline] Step 1/3: Upscaling...")
            if settings['use_interpolate']:
                upscale_out = os.path.join(cache_dir, "upscaled")
            else:
                upscale_out = settings['output_dir']
            success = run_upscale(
                realesrgan_path,
                current_input,
                upscale_out,
                settings['upscale_scale'],
                settings['upscale_model'],
                settings,
                state
            )
            if not success:
                if state.status != "CANCELLED":
                    state.status = "ERROR"
                return
            current_input = upscale_out

        # Step 2: Interpolate
        if settings['use_interpolate']:
            state.logs.append("[Pipeline] Step 2/3: Interpolation...")
            interpolate_out = settings['output_dir']
            success = run_interpolate(
                rife_path,
                current_input,
                interpolate_out,
                settings['interpolate_mult'],
                settings['interpolate_model'],
                settings,
                state
            )
            if not success:
                if state.status != "CANCELLED":
                    state.status = "ERROR"
                return
            current_input = interpolate_out

        # Step 3: Encode
        if settings['use_encode']:
            state.logs.append("[Pipeline] Step 3/3: Encoding...")
            out_parent = os.path.dirname(output_file)
            if out_parent:
                os.makedirs(out_parent, exist_ok=True)
                
            success = run_encode(
                ffmpeg_path,
                current_input,
                output_file,
                settings['encode_fps'],
                settings['encode_codec'],
                settings['encode_quality'],
                settings['encode_gpu_accel'],
                settings['audio_file'],
                settings['use_grain'],
                settings['grain_strength'],
                settings,
                state
            )
            if not success:
                if state.status != "CANCELLED":
                    state.status = "ERROR"
                return

        # Auto clean
        if auto_clean:
            state.logs.append("[Pipeline] Cleaning up intermediate cache directories...")
            shutil.rmtree(os.path.join(cache_dir, "upscaled"), ignore_errors=True)
            shutil.rmtree(os.path.join(cache_dir, "interpolated"), ignore_errors=True)
            
        state.status = "FINISHED"
        state.progress = 100.0
        state.logs.append("[Pipeline] Processing completed successfully!")
        
    except Exception as e:
        state.status = "ERROR"
        state.error_message = f"Pipeline failed: {str(e)}"
        state.logs.append(f"[Error] {state.error_message}")

class AIPOST_OT_run(bpy.types.Operator):
    bl_idname = "aipost.run"
    bl_label = "Process Sequence"
    bl_description = "Start processing the image sequence through the AI pipeline"
    
    @classmethod
    def poll(cls, context):
        return not context.scene.ai_post_is_running

    def execute(self, context):
        global _state, _thread
        scene = context.scene
        prefs = get_preferences(context)
        
        # Validation
        input_dir = bpy.path.abspath(scene.ai_post_input_dir)
        if not input_dir or not os.path.isdir(input_dir):
            self.report({'ERROR'}, "Please specify a valid input directory containing frame images.")
            return {'CANCELLED'}
            
        if scene.ai_post_use_upscale or scene.ai_post_use_interpolate:
            output_dir = bpy.path.abspath(scene.ai_post_output_dir)
            if not output_dir:
                self.report({'ERROR'}, "Please specify a valid output directory for processed frames.")
                return {'CANCELLED'}
            os.makedirs(output_dir, exist_ok=True)
        else:
            output_dir = ""

        if scene.ai_post_use_encode:
            output_file = bpy.path.abspath(scene.ai_post_output_file)
            if not output_file:
                self.report({'ERROR'}, "Please specify a valid output video path.")
                return {'CANCELLED'}
            
            # Resolve directory output and missing file extensions
            is_dir = os.path.isdir(output_file) or output_file.endswith('\\') or output_file.endswith('/')
            ext = '.mov' if scene.ai_post_encode_codec == 'PRORES' else '.mp4'
            if is_dir:
                output_file = os.path.join(output_file, f"output{ext}")
            elif not os.path.splitext(output_file)[1]:
                output_file = f"{output_file}{ext}"
        else:
            output_file = ""

        # Validate Executables
        realesrgan_path = bpy.path.abspath(prefs.realesrgan_path)
        rife_path = bpy.path.abspath(prefs.rife_path)
        ffmpeg_path = bpy.path.abspath(prefs.ffmpeg_path) if prefs.ffmpeg_path != "ffmpeg" else "ffmpeg"
        
        if scene.ai_post_use_upscale:
            if not realesrgan_path or not os.path.isfile(realesrgan_path):
                self.report({'ERROR'}, "Real-ESRGAN path is invalid in preferences.")
                return {'CANCELLED'}
                
        if scene.ai_post_use_interpolate:
            if not rife_path or not os.path.isfile(rife_path):
                self.report({'ERROR'}, "RIFE path is invalid in preferences.")
                return {'CANCELLED'}
                
        if scene.ai_post_use_encode:
            if prefs.ffmpeg_path != "ffmpeg" and (not ffmpeg_path or not os.path.isfile(ffmpeg_path)):
                self.report({'ERROR'}, "FFmpeg path is invalid in preferences.")
                return {'CANCELLED'}

        # Prepare cache
        cache_dir = resolve_cache_dir(context)
        os.makedirs(cache_dir, exist_ok=True)
        
        # Extract audio on main thread if requested
        temp_audio_file = ""
        if scene.ai_post_use_audio:
            if scene.ai_post_audio_source == 'SCENE':
                if scene.sequence_editor:
                    has_sound = any(s.type == 'SOUND' for s in scene.sequence_editor.sequences)
                    if has_sound:
                        temp_audio_file = os.path.join(tempfile.gettempdir(), f"ai_post_scene_audio_{int(time.time())}.wav")
                        try:
                            bpy.ops.sound.mixdown(filepath=temp_audio_file, container='WAV', codec='PCM')
                        except Exception as e:
                            self.report({'WARNING'}, f"Failed to mixdown scene audio: {e}")
                            temp_audio_file = ""
                    else:
                         self.report({'WARNING'}, "No sound strips found in Sequence Editor.")
                else:
                    self.report({'WARNING'}, "Sequence Editor is not initialized.")
            elif scene.ai_post_audio_source == 'EXTERNAL':
                ext_path = bpy.path.abspath(scene.ai_post_external_audio)
                if ext_path and os.path.isfile(ext_path):
                    temp_audio_file = ext_path
                else:
                    self.report({'WARNING'}, "External audio file path is invalid.")

        scene.ai_post_temp_audio_file = temp_audio_file

        # Read settings
        settings = {
            'input_dir': input_dir,
            'output_dir': output_dir,
            'output_file': output_file,
            'use_upscale': scene.ai_post_use_upscale,
            'upscale_scale': int(scene.ai_post_upscale_scale),
            'upscale_model': scene.ai_post_upscale_model,
            'use_interpolate': scene.ai_post_use_interpolate,
            'interpolate_mult': int(scene.ai_post_interpolate_mult),
            'interpolate_model': "rife-v4",  # Use rife-v4 which supports custom numframe (-n)
            'use_encode': scene.ai_post_use_encode,
            'encode_fps': scene.ai_post_encode_fps,
            'encode_codec': scene.ai_post_encode_codec,
            'encode_quality': scene.ai_post_encode_quality,
            'encode_gpu_accel': scene.ai_post_encode_gpu_accel,
            'use_custom_range': scene.ai_post_use_custom_range,
            'frame_start': scene.ai_post_frame_start,
            'frame_end': scene.ai_post_frame_end,
            'gpu_device': scene.ai_post_gpu_device,
            'use_grain': scene.ai_post_use_grain,
            'grain_strength': scene.ai_post_grain_strength,
            'audio_file': temp_audio_file,
            'upscale_tile': scene.ai_post_upscale_tile,
            'ffmpeg_path': ffmpeg_path,
            'preserve_alpha': scene.ai_post_preserve_alpha,
            'use_deflicker': scene.ai_post_use_deflicker,
            'use_denoise': scene.ai_post_use_denoise,
            'denoise_strength': scene.ai_post_denoise_strength
        }

        # Clear diagnostics and initialize state
        scene.ai_post_diag_status = ""
        scene.ai_post_diag_info = ""
        _state = PostProcessState()
        _state.status = "STARTING"
        _state.logs.append("[Pipeline] Preparing pipeline execution thread...")
        
        # Start thread
        _thread = threading.Thread(
            target=run_pipeline,
            args=(realesrgan_path, rife_path, ffmpeg_path, cache_dir, prefs.auto_clean_cache, settings, _state),
            daemon=True
        )
        
        scene.ai_post_is_running = True
        _thread.start()
        
        # Register timer
        bpy.app.timers.register(timer_callback)
        self.report({'INFO'}, "UpFrameG pipeline started.")
        
        return {'FINISHED'}

class AIPOST_OT_cancel(bpy.types.Operator):
    bl_idname = "aipost.cancel"
    bl_label = "Cancel Processing"
    bl_description = "Cancel the active post-processing pipeline"
    
    @classmethod
    def poll(cls, context):
        return context.scene.ai_post_is_running

    def execute(self, context):
        global _state
        if _state:
            _state.cancel_requested = True
            _state.logs.append("[Pipeline] Cancel requested by user. Terminating processes...")
            self.report({'INFO'}, "Cancellation requested.")
        return {'FINISHED'}

class AIPOST_OT_clear_cache(bpy.types.Operator):
    bl_idname = "aipost.clear_cache"
    bl_label = "Clear Cache"
    bl_description = "Delete all temporary cached upscaled and interpolated frames to free disk space"
    
    @classmethod
    def poll(cls, context):
        return not context.scene.ai_post_is_running

    def execute(self, context):
        cache_dir = resolve_cache_dir(context)
        if os.path.exists(cache_dir):
            try:
                shutil.rmtree(cache_dir, ignore_errors=True)
                self.report({'INFO'}, "Cache directory cleared successfully.")
            except Exception as e:
                self.report({'ERROR'}, f"Failed to clear cache: {e}")
        else:
            self.report({'INFO'}, "Cache is already empty.")
        return {'FINISHED'}

class AIPOST_OT_detect_sequence(bpy.types.Operator):
    bl_idname = "aipost.detect_sequence"
    bl_label = "Detect Render Folder"
    bl_description = "Automatically detect the current Blender render output directory"
    
    def execute(self, context):
        scene = context.scene
        # Get Blender render output directory
        render_path = scene.render.filepath
        if not render_path:
            self.report({'WARNING'}, "Render output filepath is empty.")
            return {'CANCELLED'}
            
        abs_path = bpy.path.abspath(render_path)
        # If the path points to a file, take its directory
        if not abs_path.endswith(('/', '\\')) and os.path.isfile(abs_path):
            abs_dir = os.path.dirname(abs_path)
        else:
            # It's a directory path
            abs_dir = abs_path
            
        if os.path.isdir(abs_dir):
            scene.ai_post_input_dir = abs_dir
            scene.ai_post_output_dir = os.path.join(abs_dir, "processed_frames")
            scene.ai_post_output_file = os.path.join(abs_dir, "output_processed.mp4")
            self.report({'INFO'}, f"Detected render directory: {abs_dir}")
        else:
            self.report({'WARNING'}, f"Detected path does not exist or is not a directory: {abs_dir}")
            
        return {'FINISHED'}

class AIPOST_OT_save_preset(bpy.types.Operator):
    bl_idname = "aipost.save_preset"
    bl_label = "Save Preset"
    bl_description = "Save current configuration settings as a new custom preset profile"

    @classmethod
    def poll(cls, context):
        return context.scene.ai_post_new_preset_name.strip() != ""

    def execute(self, context):
        scene = context.scene
        name = scene.ai_post_new_preset_name.strip()
        
        # Create a clean ID
        preset_id = re.sub(r'[^a-zA-Z0-9_]', '', name.upper().replace(' ', '_'))
        if not preset_id:
            self.report({'ERROR'}, "Invalid preset name.")
            return {'CANCELLED'}
            
        custom = load_custom_presets()
        
        if preset_id in {'CUSTOM', 'YOUTUBE_4K', 'MOTION_GRAPHICS', 'FAST_PREVIEW'}:
            self.report({'ERROR'}, "Cannot overwrite built-in presets.")
            return {'CANCELLED'}
            
        desc = []
        if scene.ai_post_use_upscale:
            desc.append(f"{scene.ai_post_upscale_scale}x Upscale ({scene.ai_post_upscale_model})")
        if scene.ai_post_use_interpolate:
            desc.append(f"{scene.ai_post_interpolate_mult}x Interpolation")
        if scene.ai_post_use_encode:
            desc.append(f"{scene.ai_post_encode_codec} ({scene.ai_post_encode_fps} FPS)")
        desc_str = " + ".join(desc) if desc else "No stages active"
        
        custom[preset_id] = {
            'name': name,
            'description': desc_str,
            'mode': scene.ai_post_mode,
            'use_upscale': scene.ai_post_use_upscale,
            'upscale_scale': scene.ai_post_upscale_scale,
            'upscale_model': scene.ai_post_upscale_model,
            'use_interpolate': scene.ai_post_use_interpolate,
            'interpolate_mult': scene.ai_post_interpolate_mult,
            'use_encode': scene.ai_post_use_encode,
            'encode_codec': scene.ai_post_encode_codec,
            'encode_quality': scene.ai_post_encode_quality,
            'encode_fps': scene.ai_post_encode_fps,
            'encode_gpu_accel': scene.ai_post_encode_gpu_accel
        }
        
        save_custom_presets(custom)
        
        scene.ai_post_new_preset_name = ""
        scene.ai_post_preset = preset_id
        
        self.report({'INFO'}, f"Saved preset '{name}' successfully.")
        return {'FINISHED'}

class AIPOST_OT_delete_preset(bpy.types.Operator):
    bl_idname = "aipost.delete_preset"
    bl_label = "Delete Preset"
    bl_description = "Delete the currently selected custom preset profile"

    @classmethod
    def poll(cls, context):
        preset = context.scene.ai_post_preset
        return preset not in {'CUSTOM', 'YOUTUBE_4K', 'MOTION_GRAPHICS', 'FAST_PREVIEW'}

    def execute(self, context):
        scene = context.scene
        preset = scene.ai_post_preset
        
        custom = load_custom_presets()
        if preset in custom:
            name = custom[preset]['name']
            del custom[preset]
            save_custom_presets(custom)
            self.report({'INFO'}, f"Deleted preset '{name}'.")
        
        scene.ai_post_preset = 'CUSTOM'
        return {'FINISHED'}

class AIPOST_OT_run_diagnostics(bpy.types.Operator):
    bl_idname = "aipost.run_diagnostics"
    bl_label = "Scan Sequence Diagnostics"
    bl_description = "Scan the input directory to verify frame sequence spacing, sizes, and formats"

    @classmethod
    def poll(cls, context):
        return not context.scene.ai_post_is_running

    def execute(self, context):
        scene = context.scene
        input_dir = bpy.path.abspath(scene.ai_post_input_dir)
        
        if not input_dir or not os.path.isdir(input_dir):
            scene.ai_post_diag_status = 'ERROR'
            scene.ai_post_diag_info = "Input directory is invalid or empty. Please set a valid folder first."
            self.report({'ERROR'}, "Invalid input directory.")
            return {'CANCELLED'}
            
        from .upscale import get_image_files
        files = get_image_files(input_dir)
        
        if not files:
            scene.ai_post_diag_status = 'ERROR'
            scene.ai_post_diag_info = "No supported image frames found in input directory."
            self.report({'WARNING'}, "No image files found.")
            return {'FINISHED'}
            
        scene.ai_post_diag_status = 'OK'
        info_lines = []
        info_lines.append(f"Found {len(files)} image frames.")
        
        frame_numbers = []
        for f in files:
            num_match = re.search(r"(\d+)(?=\.[a-zA-Z0-9]+$)", f)
            if num_match:
                frame_numbers.append(int(num_match.group(1)))
                
        if len(frame_numbers) == len(files):
            frame_numbers.sort()
            gaps = []
            for i in range(len(frame_numbers) - 1):
                diff = frame_numbers[i+1] - frame_numbers[i]
                if diff > 1:
                    gaps.extend(range(frame_numbers[i] + 1, frame_numbers[i+1]))
            
            if gaps:
                scene.ai_post_diag_status = 'WARNING'
                if len(gaps) <= 5:
                    gap_str = ", ".join(str(g) for g in gaps)
                    info_lines.append(f"Gap detected! Missing frame numbers: {gap_str}")
                else:
                    gap_str = ", ".join(str(g) for g in gaps[:5])
                    info_lines.append(f"Gap detected! Missing {len(gaps)} frames (including: {gap_str}...)")
            else:
                info_lines.append("Sequence is contiguous (no missing frames).")
        else:
            scene.ai_post_diag_status = 'WARNING'
            info_lines.append("Warning: Could not parse frame numbers from all image names. Ensure they end in numbers.")
            
        first_file = os.path.join(input_dir, files[0])
        try:
            bl_img = bpy.data.images.load(first_file, check_existing=True)
            base_w, base_h = bl_img.size
            base_format = bl_img.file_format
            bpy.data.images.remove(bl_img)
            
            info_lines.append(f"Frame Format: {base_format} ({base_w}x{base_h})")
            
            sample_rate = max(1, len(files) // 20)
            resolution_mismatch = False
            format_mismatch = False
            
            for i in range(0, len(files), sample_rate):
                img_path = os.path.join(input_dir, files[i])
                try:
                    img = bpy.data.images.load(img_path, check_existing=True)
                    w, h = img.size
                    fmt = img.file_format
                    bpy.data.images.remove(img)
                    if w != base_w or h != base_h:
                        resolution_mismatch = True
                    if fmt != base_format:
                        format_mismatch = True
                except Exception:
                    pass
                    
            if resolution_mismatch:
                scene.ai_post_diag_status = 'ERROR'
                info_lines.append("Error: Resolution mismatch detected! Frames must all have matching dimensions.")
            if format_mismatch:
                scene.ai_post_diag_status = 'WARNING'
                info_lines.append("Warning: Image format mismatch detected in sequence files.")
        except Exception as e:
            info_lines.append(f"Note: Could not check image sizes natively: {e}")
            
        scene.ai_post_diag_info = "\n".join(info_lines)
        self.report({'INFO'}, "Diagnostics scan completed.")
        return {'FINISHED'}

class AIPostQueueItem(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="Name", default="Queue Item")
    input_dir: bpy.props.StringProperty(name="Input Dir", subtype='DIR_PATH')
    output_dir: bpy.props.StringProperty(name="Output Dir", subtype='DIR_PATH')
    output_file: bpy.props.StringProperty(name="Output File", subtype='FILE_PATH')
    mode: bpy.props.StringProperty(name="Mode", default="FULL")
    preset: bpy.props.StringProperty(name="Preset", default="CUSTOM")
    status: bpy.props.EnumProperty(
        name="Status",
        items=[
            ('QUEUED', 'Queued', 'Item is in queue'),
            ('PROCESSING', 'Processing', 'Item is being processed'),
            ('FINISHED', 'Finished', 'Item processed successfully'),
            ('FAILED', 'Failed', 'Item processing failed')
        ],
        default='QUEUED'
    )

class AIPOST_OT_queue_add(bpy.types.Operator):
    bl_idname = "aipost.queue_add"
    bl_label = "Add Current to Queue"
    bl_description = "Add current settings to the batch processing queue"
    
    def execute(self, context):
        scene = context.scene
        item = scene.ai_post_batch_queue.add()
        
        folder_name = os.path.basename(bpy.path.abspath(scene.ai_post_input_dir).rstrip('/\\'))
        item.name = folder_name if folder_name else "Render Sequence"
        
        item.input_dir = scene.ai_post_input_dir
        item.output_dir = scene.ai_post_output_dir
        item.output_file = scene.ai_post_output_file
        item.mode = scene.ai_post_mode
        item.preset = scene.ai_post_preset
        item.status = 'QUEUED'
        
        scene.ai_post_queue_index = len(scene.ai_post_batch_queue) - 1
        return {'FINISHED'}

class AIPOST_OT_queue_remove(bpy.types.Operator):
    bl_idname = "aipost.queue_remove"
    bl_label = "Remove Selected"
    bl_description = "Remove the selected item from the batch queue"
    
    @classmethod
    def poll(cls, context):
        scene = context.scene
        return len(scene.ai_post_batch_queue) > 0 and not scene.ai_post_is_running
        
    def execute(self, context):
        scene = context.scene
        idx = scene.ai_post_queue_index
        scene.ai_post_batch_queue.remove(idx)
        scene.ai_post_queue_index = min(max(0, idx - 1), len(scene.ai_post_batch_queue) - 1)
        return {'FINISHED'}

class AIPOST_OT_queue_clear(bpy.types.Operator):
    bl_idname = "aipost.queue_clear"
    bl_label = "Clear Queue"
    bl_description = "Clear all items from the batch queue"
    
    @classmethod
    def poll(cls, context):
        scene = context.scene
        return len(scene.ai_post_batch_queue) > 0 and not scene.ai_post_is_running
        
    def execute(self, context):
        scene = context.scene
        scene.ai_post_batch_queue.clear()
        scene.ai_post_queue_index = 0
        return {'FINISHED'}

class AIPOST_OT_queue_process(bpy.types.Operator):
    bl_idname = "aipost.queue_process"
    bl_label = "Process Queue"
    bl_description = "Start sequential background processing of all queued items"
    
    @classmethod
    def poll(cls, context):
        scene = context.scene
        has_queued = any(item.status == 'QUEUED' for item in scene.ai_post_batch_queue)
        return has_queued and not scene.ai_post_is_running
        
    def execute(self, context):
        scene = context.scene
        scene.ai_post_is_batch_running = True
        
        next_item = None
        for item in scene.ai_post_batch_queue:
            if item.status == 'QUEUED':
                next_item = item
                break
                
        if next_item:
            scene.ai_post_input_dir = next_item.input_dir
            scene.ai_post_output_dir = next_item.output_dir
            scene.ai_post_output_file = next_item.output_file
            scene.ai_post_mode = next_item.mode
            scene.ai_post_preset = next_item.preset
            
            next_item.status = 'PROCESSING'
            bpy.ops.aipost.run()
            self.report({'INFO'}, f"Batch processing started: {next_item.name}")
            
        return {'FINISHED'}

class AIPOST_OT_view_comparison(bpy.types.Operator):
    bl_idname = "aipost.view_comparison"
    bl_label = "Compare Frames Side-by-Side"
    bl_description = "Open a side-by-side view comparing original and processed frames"
    
    def execute(self, context):
        scene = context.scene
        input_dir = bpy.path.abspath(scene.ai_post_input_dir)
        output_dir = bpy.path.abspath(scene.ai_post_output_dir)
        
        if not input_dir or not os.path.isdir(input_dir):
            self.report({'ERROR'}, "Invalid input directory.")
            return {'CANCELLED'}
            
        if not output_dir or not os.path.isdir(output_dir):
            self.report({'ERROR'}, "Invalid output directory.")
            return {'CANCELLED'}
            
        from .upscale import get_image_files
        in_files = get_image_files(input_dir)
        out_files = get_image_files(output_dir)
        
        if not in_files or not out_files:
            self.report({'ERROR'}, "No frames to compare. Run the pipeline first.")
            return {'CANCELLED'}
            
        sample_name = in_files[0]
        if sample_name not in out_files:
            out_sample = out_files[0]
        else:
            out_sample = sample_name
            
        in_img_path = os.path.join(input_dir, sample_name)
        out_img_path = os.path.join(output_dir, out_sample)
        
        try:
            in_img = bpy.data.images.load(in_img_path, check_existing=True)
            out_img = bpy.data.images.load(out_img_path, check_existing=True)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to load comparison images: {e}")
            return {'CANCELLED'}
            
        window = context.window
        screen = context.screen
        
        view_3d_area = None
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                view_3d_area = area
                break
                
        if not view_3d_area:
            view_3d_area = screen.areas[0]
            
        try:
            old_areas = list(screen.areas)
            
            if hasattr(context, "temp_override"):
                with context.temp_override(area=view_3d_area):
                    bpy.ops.screen.area_split(direction='VERTICAL', factor=0.5)
            else:
                ctx = context.copy()
                ctx['area'] = view_3d_area
                bpy.ops.screen.area_split(ctx, direction='VERTICAL', factor=0.5)
                
            new_area = None
            for area in screen.areas:
                if area not in old_areas:
                    new_area = area
                    break
                    
            if not new_area:
                view_3d_area.type = 'IMAGE_EDITOR'
                view_3d_area.spaces.active.image = out_img
                self.report({'INFO'}, "Showing processed frame in Image Editor.")
                return {'FINISHED'}
                
            view_3d_area.type = 'IMAGE_EDITOR'
            view_3d_area.spaces.active.image = in_img
            
            new_area.type = 'IMAGE_EDITOR'
            new_area.spaces.active.image = out_img
            
            self.report({'INFO'}, "Side-by-side comparison opened. Left: Original | Right: Processed.")
            
        except Exception as e:
            try:
                view_3d_area.type = 'IMAGE_EDITOR'
                view_3d_area.spaces.active.image = out_img
                self.report({'INFO'}, f"Opened processed image: {e}")
            except Exception:
                self.report({'ERROR'}, f"Could not split screen: {e}")
                
        return {'FINISHED'}

class AIPOST_OT_download_binary(bpy.types.Operator):
    bl_idname = "aipost.download_binary"
    bl_label = "Download Binary"
    bl_description = "Download and install missing binary files automatically"
    
    binary_type: bpy.props.StringProperty()
    
    def execute(self, context):
        global _download_state, _download_thread
        
        if _download_state.is_downloading:
            self.report({'WARNING'}, "A download is already in progress.")
            return {'CANCELLED'}
            
        config_dir = bpy.utils.user_resource('CONFIG', create=True)
        binaries_dir = os.path.join(config_dir, "upframeg_binaries")
        
        _download_state = AIPostDownloadState()
        _download_state.is_downloading = True
        _download_state.active_download = self.binary_type
        
        _download_thread = threading.Thread(
            target=download_binary_thread,
            args=(self.binary_type, binaries_dir),
            daemon=True
        )
        _download_thread.start()
        
        bpy.app.timers.register(timer_callback)
        self.report({'INFO'}, f"Started downloading {self.binary_type}...")
        return {'FINISHED'}

class AIPOST_OT_auto_detect_binaries(bpy.types.Operator):
    bl_idname = "aipost.auto_detect_binaries"
    bl_label = "Auto-detect Binaries"
    bl_description = "Scan Blender config folder for downloaded AI binaries"
    
    def execute(self, context):
        prefs = context.preferences.addons[get_addon_name()].preferences
        changes_made = prefs.auto_detect_binaries()
        if changes_made:
            self.report({'INFO'}, "UpFrameG: Successfully detected and updated binary paths!")
        else:
            self.report({'INFO'}, "UpFrameG: Scanning finished. No new binaries found.")
        return {'FINISHED'}

class AIPOST_OT_auto_setup(bpy.types.Operator):
    bl_idname = "aipost.auto_setup"
    bl_label = "Auto-setup UpFrameG"
    bl_description = "Automatically download and setup all missing binary dependencies in the background"
    
    def execute(self, context):
        global _download_state, _download_thread
        
        if _download_state.is_downloading:
            return {'CANCELLED'}
            
        prefs = get_preferences(context)
        if not prefs:
            return {'CANCELLED'}
            
        # Check what is missing
        missing = []
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
                
        if not is_realesrgan_valid:
            missing.append('realesrgan')
        if not is_rife_valid:
            missing.append('rife')
        if not is_ffmpeg_valid:
            missing.append('ffmpeg')
            
        if not missing:
            self.report({'INFO'}, "UpFrameG: All dependencies are already set up!")
            return {'FINISHED'}
            
        config_dir = bpy.utils.user_resource('CONFIG', create=True)
        binaries_dir = os.path.join(config_dir, "upframeg_binaries")
        
        _download_state = AIPostDownloadState()
        _download_state.is_downloading = True
        _download_state.active_download = missing[0]
        
        _download_thread = threading.Thread(
            target=download_binary_thread,
            args=(missing, binaries_dir),
            daemon=True
        )
        _download_thread.start()
        
        bpy.app.timers.register(timer_callback)
        self.report({'INFO'}, f"Started automatic setup: downloading {', '.join(missing)}...")
        return {'FINISHED'}

# Register scene properties
classes = (
    AIPostQueueItem,
    AIPOST_OT_run,
    AIPOST_OT_cancel,
    AIPOST_OT_clear_cache,
    AIPOST_OT_detect_sequence,
    AIPOST_OT_save_preset,
    AIPOST_OT_delete_preset,
    AIPOST_OT_run_diagnostics,
    AIPOST_OT_queue_add,
    AIPOST_OT_queue_remove,
    AIPOST_OT_queue_clear,
    AIPOST_OT_queue_process,
    AIPOST_OT_view_comparison,
    AIPOST_OT_download_binary,
    AIPOST_OT_auto_detect_binaries,
    AIPOST_OT_auto_setup,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
        
    bpy.types.Scene.ai_post_input_dir = bpy.props.StringProperty(
        name="Input Directory",
        description="Folder containing rendered frames to process",
        subtype='DIR_PATH',
        default=""
    )
    
    bpy.types.Scene.ai_post_output_dir = bpy.props.StringProperty(
        name="Output Directory",
        description="Folder to save the final processed frames",
        subtype='DIR_PATH',
        default=""
    )
    
    bpy.types.Scene.ai_post_output_file = bpy.props.StringProperty(
        name="Output File",
        description="Path to save the compiled video file",
        subtype='FILE_PATH',
        default=""
    )
    
    bpy.types.Scene.ai_post_use_custom_range = bpy.props.BoolProperty(
        name="Custom Frame Range",
        description="Upscale and interpolate only a specific subset of frames",
        default=False
    )
    
    bpy.types.Scene.ai_post_frame_start = bpy.props.IntProperty(
        name="Start Frame",
        description="First frame of the sequence to process",
        default=1,
        min=1
    )
    
    bpy.types.Scene.ai_post_frame_end = bpy.props.IntProperty(
        name="End Frame",
        description="Last frame of the sequence to process",
        default=250,
        min=1
    )
    
    bpy.types.Scene.ai_post_use_upscale = bpy.props.BoolProperty(
        name="Enable Upscale",
        description="Upscale frames using Real-ESRGAN",
        default=True
    )
    
    bpy.types.Scene.ai_post_upscale_scale = bpy.props.EnumProperty(
        name="Scale Factor",
        description="Real-ESRGAN scale factor multiplier",
        items=[
            ('2', '2x', 'Scale up by 2x'),
            ('3', '3x', 'Scale up by 3x'),
            ('4', '4x', 'Scale up by 4x')
        ],
        default='2'
    )
    
    bpy.types.Scene.ai_post_upscale_model = bpy.props.EnumProperty(
        name="Model",
        description="Real-ESRGAN model to use",
        items=[
            ('realesr-animevideov3', 'Anime Video (realesr-animevideov3)', 'Ultra-fast video model, extremely low VRAM footprint, allows tile size 2000+ to prevent grid seams')
        ],
        default='realesr-animevideov3'
    )
    
    bpy.types.Scene.ai_post_use_interpolate = bpy.props.BoolProperty(
        name="Enable Interpolation",
        description="Interpolate frames using RIFE to increase frame rate",
        default=True
    )
    
    bpy.types.Scene.ai_post_interpolate_mult = bpy.props.EnumProperty(
        name="Multiplier",
        description="RIFE frame interpolation multiplier",
        items=[
            ('2', '2x', 'Double the frame rate'),
            ('4', '4x', 'Quadruple the frame rate'),
            ('8', '8x', 'Octuple the frame rate')
        ],
        default='2'
    )
    
    bpy.types.Scene.ai_post_use_encode = bpy.props.BoolProperty(
        name="Enable Encoding",
        description="Compile the frames into a video using FFmpeg",
        default=True
    )
    
    bpy.types.Scene.ai_post_encode_fps = bpy.props.IntProperty(
        name="Target FPS",
        description="Framerate of the output video",
        default=24,
        min=1,
        max=240
    )
    
    bpy.types.Scene.ai_post_encode_codec = bpy.props.EnumProperty(
        name="Codec",
        description="Video codec to encode with",
        items=[
            ('H264', 'H.264 (MP4)', 'Standard compression H.264 codec'),
            ('H265', 'H.265 (MP4)', 'High efficiency H.265 codec'),
            ('PRORES', 'Apple ProRes (MOV)', 'Professional ProRes 422 HQ codec for editing')
        ],
        default='H264'
    )
    
    bpy.types.Scene.ai_post_encode_quality = bpy.props.EnumProperty(
        name="Quality",
        description="Encoding quality preset",
        items=[
            ('LOW', 'Low', 'Fast encoding, lower file size'),
            ('MEDIUM', 'Medium', 'Good balance between speed and quality'),
            ('HIGH', 'High', 'High quality, clean output'),
            ('LOSSLESS', 'Lossless', 'Maximum quality, huge file sizes')
        ],
        default='HIGH'
    )
    
    bpy.types.Scene.ai_post_encode_gpu_accel = bpy.props.EnumProperty(
        name="GPU Encoder",
        description="Hardware acceleration encoding engine for H.264/H.265 (ProRes is CPU-only)",
        items=[
            ('NONE', 'None (CPU)', 'Standard software CPU encoding'),
            ('NVENC', 'Nvidia NVENC', 'Hardware acceleration using Nvidia graphics card'),
            ('AMF', 'AMD AMF', 'Hardware acceleration using AMD graphics card'),
            ('QSV', 'Intel QSV', 'Hardware acceleration using Intel QuickSync')
        ],
        default='NONE'
    )
    
    bpy.types.Scene.ai_post_mode = bpy.props.EnumProperty(
        name="Process Mode",
        description="Select which stages of the post-processing pipeline to run",
        items=[
            ('FULL', 'Upscale + Interpolate + Encode', 'Run the full pipeline'),
            ('UPSCALE_ONLY', 'Upscale Only', 'Upscale input frames and save to output directory'),
            ('INTERPOLATE_ONLY', 'Interpolate Only', 'Interpolate input frames and save to output directory'),
            ('ENCODE_ONLY', 'Encode Only', 'Compile input frames directly into video'),
            ('UPSCALE_ENCODE', 'Upscale + Encode', 'Upscale frames and compile into video'),
            ('INTERPOLATE_ENCODE', 'Interpolate + Encode', 'Interpolate frames and compile into video'),
            ('CUSTOM', 'Custom', 'Manually select stages via checkboxes')
        ],
        default='FULL',
        update=update_mode_cb
    )
    
    bpy.types.Scene.ai_post_preset = bpy.props.EnumProperty(
        name="Preset",
        description="Addon preset profiles",
        items=get_preset_items_cb,
        default=0,
        update=update_preset_cb
    )
    
    bpy.types.Scene.ai_post_new_preset_name = bpy.props.StringProperty(
        name="New Preset Name",
        description="Name of the custom preset to save",
        default=""
    )
    
    bpy.types.Scene.ai_post_diag_status = bpy.props.StringProperty(
        name="Diagnostics Status",
        default=""
    )
    
    bpy.types.Scene.ai_post_diag_info = bpy.props.StringProperty(
        name="Diagnostics Report",
        default=""
    )
    
    # Progress properties
    bpy.types.Scene.ai_post_is_running = bpy.props.BoolProperty(name="Is Running", default=False)
    bpy.types.Scene.ai_post_progress = bpy.props.FloatProperty(name="Progress", default=0.0, min=0.0, max=100.0)
    bpy.types.Scene.ai_post_status = bpy.props.StringProperty(name="Status", default="IDLE")
    bpy.types.Scene.ai_post_eta = bpy.props.StringProperty(name="ETA", default="00:00:00")
    bpy.types.Scene.ai_post_last_logs = bpy.props.StringProperty(name="Logs", default="")
    bpy.types.Scene.ai_post_error_msg = bpy.props.StringProperty(name="Error Message", default="")

    # New advanced features properties
    bpy.types.Scene.ai_post_gpu_device = bpy.props.EnumProperty(
        name="GPU Device Index",
        description="GPU Device to use for Vulkan/CPU processing",
        items=[
            ('0', 'GPU 0 (Primary)', 'First available GPU device'),
            ('1', 'GPU 1', 'Second available GPU device'),
            ('2', 'GPU 2', 'Third available GPU device'),
            ('-1', 'CPU (Safe, No Lag)', 'Run processing on CPU')
        ],
        default='0'
    )
    
    bpy.types.Scene.ai_post_use_grain = bpy.props.BoolProperty(
        name="Add Film Grain",
        description="Mux subtle cinematic grain to mask upscaling noise",
        default=False
    )
    
    bpy.types.Scene.ai_post_preserve_alpha = bpy.props.BoolProperty(
        name="Preserve Transparency",
        description="Preserve alpha channel using two-pass split and merge (takes 2x longer, supports transparency)",
        default=False
    )
    
    bpy.types.Scene.ai_post_grain_strength = bpy.props.IntProperty(
        name="Grain Strength",
        description="Amount of film grain to add (1-100)",
        default=15,
        min=1,
        max=100
    )
    
    bpy.types.Scene.ai_post_use_deflicker = bpy.props.BoolProperty(
        name="Temporal Deflicker",
        description="Apply temporal deflickering filter to smooth out exposure variations",
        default=False
    )
    
    bpy.types.Scene.ai_post_use_denoise = bpy.props.BoolProperty(
        name="Temporal Denoise",
        description="Apply temporal denoising (hqdn3d) to clean up noise across frames",
        default=False
    )
    
    bpy.types.Scene.ai_post_denoise_strength = bpy.props.IntProperty(
        name="Denoise Strength",
        description="Strength of the temporal denoising filter (1-100)",
        default=15,
        min=1,
        max=100
    )
    
    bpy.types.Scene.ai_post_use_audio = bpy.props.BoolProperty(
        name="Mux Audio Strip",
        description="Extract scene sound sequence or external audio into the final video",
        default=False
    )
    
    bpy.types.Scene.ai_post_audio_source = bpy.props.EnumProperty(
        name="Audio Source",
        items=[
            ('SCENE', 'Active Scene Sequencer', 'Use audio tracks from active VSE sequencer'),
            ('EXTERNAL', 'External Audio File', 'Choose an external file')
        ],
        default='SCENE'
    )
    
    bpy.types.Scene.ai_post_external_audio = bpy.props.StringProperty(
        name="Audio File",
        description="External audio file path",
        subtype='FILE_PATH',
        default=""
    )
    
    bpy.types.Scene.ai_post_auto_import = bpy.props.BoolProperty(
        name="Import back to Sequencer",
        description="Auto import processed video strip into active Scene Sequencer channel",
        default=False
    )
    
    bpy.types.Scene.ai_post_temp_audio_file = bpy.props.StringProperty(
        name="Temp Audio File",
        default=""
    )
    
    bpy.types.Scene.ai_post_is_batch_running = bpy.props.BoolProperty(
        name="Is Batch Running",
        default=False
    )
    
    bpy.types.Scene.ai_post_queue_index = bpy.props.IntProperty(
        name="Queue Index",
        default=0
    )
    
    bpy.types.Scene.ai_post_batch_queue = bpy.props.CollectionProperty(
        type=AIPostQueueItem
    )
    
    bpy.types.Scene.ai_post_upscale_tile = bpy.props.IntProperty(
        name="Tile Size",
        description="Tile size for processing (>=32, 0 for auto)",
        default=0,
        min=0
    )

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
        
    del bpy.types.Scene.ai_post_input_dir
    del bpy.types.Scene.ai_post_output_dir
    del bpy.types.Scene.ai_post_output_file
    del bpy.types.Scene.ai_post_use_custom_range
    del bpy.types.Scene.ai_post_frame_start
    del bpy.types.Scene.ai_post_frame_end
    del bpy.types.Scene.ai_post_use_upscale
    del bpy.types.Scene.ai_post_upscale_scale
    del bpy.types.Scene.ai_post_upscale_model
    del bpy.types.Scene.ai_post_use_interpolate
    del bpy.types.Scene.ai_post_interpolate_mult
    del bpy.types.Scene.ai_post_use_encode
    del bpy.types.Scene.ai_post_encode_fps
    del bpy.types.Scene.ai_post_encode_codec
    del bpy.types.Scene.ai_post_encode_quality
    del bpy.types.Scene.ai_post_encode_gpu_accel
    del bpy.types.Scene.ai_post_mode
    del bpy.types.Scene.ai_post_preset
    del bpy.types.Scene.ai_post_new_preset_name
    del bpy.types.Scene.ai_post_diag_status
    del bpy.types.Scene.ai_post_diag_info
    
    del bpy.types.Scene.ai_post_is_running
    del bpy.types.Scene.ai_post_progress
    del bpy.types.Scene.ai_post_status
    del bpy.types.Scene.ai_post_eta
    del bpy.types.Scene.ai_post_last_logs
    del bpy.types.Scene.ai_post_error_msg

    # New properties unregister
    del bpy.types.Scene.ai_post_gpu_device
    del bpy.types.Scene.ai_post_use_grain
    del bpy.types.Scene.ai_post_preserve_alpha
    del bpy.types.Scene.ai_post_use_deflicker
    del bpy.types.Scene.ai_post_use_denoise
    del bpy.types.Scene.ai_post_denoise_strength
    del bpy.types.Scene.ai_post_grain_strength
    del bpy.types.Scene.ai_post_use_audio
    del bpy.types.Scene.ai_post_audio_source
    del bpy.types.Scene.ai_post_external_audio
    del bpy.types.Scene.ai_post_auto_import
    del bpy.types.Scene.ai_post_temp_audio_file
    del bpy.types.Scene.ai_post_is_batch_running
    del bpy.types.Scene.ai_post_queue_index
    del bpy.types.Scene.ai_post_batch_queue
    del bpy.types.Scene.ai_post_upscale_tile
