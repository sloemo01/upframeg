import os
import bpy

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

def check_cache_exists(context):
    from .operators import resolve_cache_dir
    cache_dir = resolve_cache_dir(context)
    
    upscaled_dir = os.path.join(cache_dir, "upscaled")
    interpolated_dir = os.path.join(cache_dir, "interpolated")
    
    upscale_count = 0
    if os.path.isdir(upscaled_dir):
        upscale_count = len([f for f in os.listdir(upscaled_dir) if os.path.isfile(os.path.join(upscaled_dir, f))])
        
    interpolate_count = 0
    if os.path.isdir(interpolated_dir):
        interpolate_count = len([f for f in os.listdir(interpolated_dir) if os.path.isfile(os.path.join(interpolated_dir, f))])
        
    if upscale_count > 0 or interpolate_count > 0:
        return True
        
    # Also check the user's output directory
    scene = context.scene
    if scene:
        out_dir = bpy.path.abspath(scene.ai_post_output_dir)
        in_dir = bpy.path.abspath(scene.ai_post_input_dir)
        if out_dir and os.path.isdir(out_dir) and out_dir != in_dir:
            from .upscale import get_image_files
            out_files = get_image_files(out_dir)
            if len(out_files) > 0:
                return True
                
    return False

def check_cache_size(context):
    from .operators import resolve_cache_dir
    cache_dir = resolve_cache_dir(context)
    if not os.path.exists(cache_dir):
        return "0.0 MB"
    total_size = 0
    for root, dirs, files in os.walk(cache_dir):
        for f in files:
            fp = os.path.join(root, f)
            if os.path.exists(fp):
                total_size += os.path.getsize(fp)
    return f"{total_size / (1024*1024):.1f} MB"

class AIPOST_UL_queue_list(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        status_icons = {
            'QUEUED': 'TIME',
            'PROCESSING': 'PLAY',
            'FINISHED': 'CHECKMARK',
            'FAILED': 'ERROR'
        }
        status_icon = status_icons.get(item.status, 'QUESTION')
        row.label(text=item.name, icon='FILE_FOLDER')
        row.label(text=f"[{item.mode}]")
        row.label(text="", icon=status_icon)

class AIPOST_PT_panel(bpy.types.Panel):
    bl_label = "UpFrameG"
    bl_idname = "AIPOST_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'UpFrameG'
    
    def draw(self, context):
        layout = self.layout
        scene = context.scene
        is_running = scene.ai_post_is_running
        preset_custom = (scene.ai_post_preset == 'CUSTOM')
        settings_active = not is_running

        # ----------------------------------------------------
        # 1. Preset Profile Selection
        # ----------------------------------------------------
        box = layout.box()
        col = box.column(align=True)
        col.label(text="Preset Profile", icon='PRESET')
        col.prop(scene, "ai_post_preset", text="")
        
        # Custom Preset Actions
        preset_id = scene.ai_post_preset
        if preset_id not in {'CUSTOM', 'YOUTUBE_4K', 'MOTION_GRAPHICS', 'FAST_PREVIEW'}:
            col.operator("aipost.delete_preset", text="Delete Preset", icon='TRASH')
        elif preset_id == 'CUSTOM' and not is_running:
            col.label(text="Save Custom Preset:", icon='PRESET')
            row = col.row(align=True)
            row.prop(scene, "ai_post_new_preset_name", text="")
            row.operator("aipost.save_preset", text="Save", icon='ADD')
            
        col.separator()
        row_mode = col.row()
        row_mode.active = settings_active
        row_mode.label(text="Process Mode", icon='COLLAPSEMENU')
        col.prop(scene, "ai_post_mode", text="")
            
        col.separator()

        # ----------------------------------------------------
        # 2. Input & Output Selection
        # ----------------------------------------------------
        box = layout.box()
        col = box.column(align=True)
        col.active = not is_running
        col.label(text="Input & Output Settings", icon='IMAGE_DATA')
        col.prop(scene, "ai_post_input_dir", text="Input Directory")
        col.prop(scene, "ai_post_output_dir", text="Output Directory")
        col.operator("aipost.detect_sequence", text="Auto-Detect Render Folders", icon='ZOOM_ALL')
        
        # Hardware GPU Selection
        row_gpu = col.row()
        row_gpu.prop(scene, "ai_post_gpu_device", text="GPU Index")
        
        # Custom Range Properties
        sub = col.column(align=True)
        sub.prop(scene, "ai_post_use_custom_range", text="Custom Frame Range")
        if scene.ai_post_use_custom_range:
            row = sub.row(align=True)
            row.prop(scene, "ai_post_frame_start", text="Start")
            row.prop(scene, "ai_post_frame_end", text="End")
            
        col.separator()
        col.operator("aipost.run_diagnostics", text="Scan Sequence Diagnostics", icon='INFO')
        
        # Display Diagnostics report if available
        diag_status = scene.ai_post_diag_status
        if diag_status:
            diag_box = col.box()
            if diag_status == 'ERROR':
                diag_box.alert = True
                diag_box.label(text="Diagnostics: Error found!", icon='ERROR')
            elif diag_status == 'WARNING':
                diag_box.label(text="Diagnostics: Warning!", icon='WARNING')
            else:
                diag_box.label(text="Diagnostics: Healthy!", icon='CHECKMARK')
                
            lines = scene.ai_post_diag_info.split('\n')
            for line in lines:
                if line:
                    diag_box.label(text=line)
        
        # ----------------------------------------------------
        # 3. Upscaling (Real-ESRGAN)
        # ----------------------------------------------------
        box = layout.box()
        col = box.column(align=True)
        
        # Checkbox header
        row = col.row()
        row.active = settings_active and (scene.ai_post_mode == 'CUSTOM')
        row.prop(scene, "ai_post_use_upscale", text="Upscaling (Real-ESRGAN)", icon='ZOOM_IN')
        
        if scene.ai_post_use_upscale:
            sub = col.column(align=True)
            sub.active = settings_active
            sub.prop(scene, "ai_post_upscale_scale", text="Scale Factor")
            sub.prop(scene, "ai_post_upscale_model", text="Model")
            sub.prop(scene, "ai_post_upscale_tile", text="Tile Size (0 = Auto)")
            sub.prop(scene, "ai_post_preserve_alpha", text="Preserve Transparency")

        # ----------------------------------------------------
        # 4. Interpolation (RIFE)
        # ----------------------------------------------------
        box = layout.box()
        col = box.column(align=True)
        
        row = col.row()
        row.active = settings_active and (scene.ai_post_mode == 'CUSTOM')
        row.prop(scene, "ai_post_use_interpolate", text="Frame Interpolation (RIFE)", icon='TIME')
        
        if scene.ai_post_use_interpolate:
            sub = col.column(align=True)
            sub.active = settings_active
            sub.prop(scene, "ai_post_interpolate_mult", text="Multiplier")
            sub.prop(scene, "ai_post_preserve_alpha", text="Preserve Transparency")

        # ----------------------------------------------------
        # 5. Encoding (FFmpeg)
        # ----------------------------------------------------
        box = layout.box()
        col = box.column(align=True)
        
        row = col.row()
        row.active = settings_active and (scene.ai_post_mode == 'CUSTOM')
        row.prop(scene, "ai_post_use_encode", text="Encode to Video (FFmpeg)", icon='RENDER_ANIMATION')
        
        if scene.ai_post_use_encode:
            sub = col.column(align=True)
            sub.active = settings_active
            sub.prop(scene, "ai_post_output_file", text="Output Path")
            sub.prop(scene, "ai_post_encode_fps", text="Target FPS")
            sub.prop(scene, "ai_post_encode_codec", text="Codec")
            sub.prop(scene, "ai_post_encode_quality", text="Quality")
            if scene.ai_post_encode_codec in {'H264', 'H265'}:
                sub.prop(scene, "ai_post_encode_gpu_accel", text="GPU Encoder")
            
            # Advanced Video Filters
            sub.separator()
            sub.label(text="Advanced Video Filters:", icon='FILTER')
            sub.prop(scene, "ai_post_use_grain", text="Add Film Grain")
            if scene.ai_post_use_grain:
                sub.prop(scene, "ai_post_grain_strength", text="Grain Strength")
            sub.prop(scene, "ai_post_use_deflicker", text="Temporal Deflicker")
            sub.prop(scene, "ai_post_use_denoise", text="Temporal Denoise")
            if scene.ai_post_use_denoise:
                sub.prop(scene, "ai_post_denoise_strength", text="Denoise Strength")
                
            # Audio Muxing & Sequencer Import Settings
            sub.separator()
            sub.prop(scene, "ai_post_use_audio", text="Mux Audio Strip")
            if scene.ai_post_use_audio:
                sub.prop(scene, "ai_post_audio_source", text="Audio Source")
                if scene.ai_post_audio_source == 'EXTERNAL':
                    sub.prop(scene, "ai_post_external_audio", text="External Audio")
            sub.prop(scene, "ai_post_auto_import", text="Import back to Sequencer")

        # ----------------------------------------------------
        # 6. Pipeline Execution Actions & Progress
        # ----------------------------------------------------
        layout.separator()
        
        if is_running:
            # Active Progress UI
            box = layout.box()
            col = box.column(align=True)
            col.label(text=f"Status: {scene.ai_post_status}", icon='CONSOLE')
            
            # Draw native Blender progress bar
            col.progress(factor=scene.ai_post_progress / 100.0, text=f"{scene.ai_post_progress:.1f}%")
            col.label(text=f"ETA: {scene.ai_post_eta}", icon='TIME')
            col.separator()
            
            # Cancel Button
            col.operator("aipost.cancel", text="Cancel Processing", icon='CANCEL')
            
            # Scrolling log console
            col.separator()
            col.label(text="Process Logs:")
            log_box = col.box()
            log_col = log_box.column(align=True)
            log_lines = scene.ai_post_last_logs.split('\n')
            for line in log_lines:
                if line:
                    log_col.label(text=line)
                    
        else:
            # Pipeline is IDLE or Finished
            has_cache = check_cache_exists(context)
            
            # Cache Management Panel
            cache_box = layout.box()
            cache_col = cache_box.column(align=True)
            cache_col.label(text="Cache Management", icon='FILE_FOLDER')
            cache_row = cache_col.row(align=True)
            cache_row.label(text=f"Cache Size: {check_cache_size(context)}")
            cache_row.operator("aipost.clear_cache", text="Clear Cache", icon='TRASH')
            cache_col.separator()
            
            # Process / Resume Button
            if has_cache:
                layout.operator("aipost.run", text="Resume Processing", icon='PLAY')
                layout.operator("aipost.view_comparison", text="Compare Frames Side-by-Side", icon='IMAGE_DATA')
                layout.label(text="Cached frames found. Resuming is supported.", icon='INFO')
            else:
                layout.operator("aipost.run", text="Process Sequence", icon='PLAY')

            # Render Status Feedback Boxes
            status = scene.ai_post_status
            if status == "FINISHED":
                box = layout.box()
                col = box.column(align=True)
                col.label(text="Processing completed successfully!", icon='CHECKMARK')
            elif status == "CANCELLED":
                box = layout.box()
                col = box.column(align=True)
                col.label(text="Processing was cancelled.", icon='CANCEL')
            elif status == "ERROR":
                box = layout.box()
                box.alert = True
                col = box.column(align=True)
                col.label(text="Processing failed with errors:", icon='ERROR')
                col.label(text=scene.ai_post_error_msg)
                
            # Draw logs if they exist and we are not running
            if scene.ai_post_last_logs and status in {"ERROR", "CANCELLED"}:
                layout.separator()
                layout.label(text="Last Process Logs:", icon='CONSOLE')
                log_box = layout.box()
                log_col = log_box.column(align=True)
                log_lines = scene.ai_post_last_logs.split('\n')
                for line in log_lines[-15:]: # show last 15 lines to avoid UI clutter
                    if line:
                        log_col.label(text=line)
                
            # ----------------------------------------------------
            # 7. Batch Processing Queue Panel
            # ----------------------------------------------------
            q_box = layout.box()
            q_col = q_box.column(align=True)
            q_col.label(text="Batch Processing Queue", icon='COLLAPSEMENU')
            q_col.template_list("AIPOST_UL_queue_list", "", scene, "ai_post_batch_queue", scene, "ai_post_queue_index")
            
            q_row = q_col.row(align=True)
            q_row.operator("aipost.queue_add", text="Add Current", icon='ADD')
            q_row.operator("aipost.queue_remove", text="Remove Selected", icon='REMOVE')
            q_row.operator("aipost.queue_clear", text="Clear All", icon='TRASH')
            
            q_col.separator()
            if scene.ai_post_is_batch_running:
                q_col.label(text="Batch process in progress...", icon='PLAY')
            else:
                q_col.operator("aipost.queue_process", text="Process Queue", icon='PLAY')

def register():
    bpy.utils.register_class(AIPOST_UL_queue_list)
    bpy.utils.register_class(AIPOST_PT_panel)

def unregister():
    bpy.utils.unregister_class(AIPOST_PT_panel)
    bpy.utils.unregister_class(AIPOST_UL_queue_list)
