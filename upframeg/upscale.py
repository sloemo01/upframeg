import os
import re
import subprocess
import shutil
import time
import queue
import threading

def enqueue_output(out, q):
    try:
        for line in iter(out.readline, ''):
            q.put(line)
        out.close()
    except Exception:
        pass


def get_image_files(directory):
    """Get sorted list of image files in a directory."""
    valid_exts = {'.png', '.jpg', '.jpeg', '.tga', '.tif', '.tiff', '.exr'}
    files = []
    if not os.path.exists(directory):
        return []
    for f in os.listdir(directory):
        ext = os.path.splitext(f)[1].lower()
        if ext in valid_exts:
            files.append(f)
    return sorted(files)

def run_upscale_pass(cmd, start_progress, progress_scale, missing_frames, total_input, total_missing, output_dir, state):
    startupinfo = None
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0 # SW_HIDE

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            startupinfo=startupinfo,
            cwd=os.path.dirname(cmd[0])
        )
    except Exception as e:
        state.error_message = f"Failed to start Real-ESRGAN subprocess: {str(e)}"
        state.logs.append(f"[Error] {state.error_message}")
        return -1

    progress_pattern = re.compile(r"(\d+\.\d+)%")
    start_time = time.time()
    
    q = queue.Queue()
    t = threading.Thread(target=enqueue_output, args=(process.stdout, q))
    t.daemon = True
    t.start()

    while True:
        if state.cancel_requested:
            state.logs.append("[Upscale] Cancellation requested. Terminating subprocess...")
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
            return -2

        try:
            line = q.get_nowait()
        except queue.Empty:
            line = None

        if line is None:
            if process.poll() is not None:
                if q.empty():
                    break
            time.sleep(0.05)
            continue
            
        clean_line = line.strip()
        if clean_line:
            state.logs.append(f"[Real-ESRGAN] {clean_line}")
            if len(state.logs) > 100:
                state.logs.pop(0)

        # Parse progress
        match = progress_pattern.search(clean_line)
        if match:
            val = float(match.group(1))
            try:
                existing_files = set(get_image_files(output_dir))
                completed_count = sum(1 for frame in missing_frames if frame in existing_files and os.path.getsize(os.path.join(output_dir, frame)) > 0)
            except Exception:
                completed_count = 0
                
            already_done = total_input - total_missing
            current_frame_progress = min(1.0, val / 100.0)
            
            frames_done_fraction = already_done + completed_count + current_frame_progress
            pass_progress = (frames_done_fraction / total_input) * 100.0
            state.progress = min(99.9, start_progress + (pass_progress * progress_scale))
            
            # Estimate remaining time
            elapsed = time.time() - start_time
            if state.progress > 0:
                total_est = elapsed / (state.progress / 100.0)
                rem = max(0.0, total_est - elapsed)
                state.eta = time.strftime("%H:%M:%S", time.gmtime(rem))

    rc = process.poll()
    return rc

def run_upscale(exec_path, input_dir, output_dir, scale, model, settings, state):
    """
    Runs Real-ESRGAN upscaling.
    Supports smart resume and RGBA transparency preservation.
    """
    state.status = "UPSCALING"
    state.progress = 0.0
    state.logs.append(f"[Upscale] Starting Real-ESRGAN upscaling...")
    state.logs.append(f"[Upscale] Input Dir: {input_dir}")
    state.logs.append(f"[Upscale] Output Dir: {output_dir}")
    
    if not os.path.exists(exec_path) or not os.path.isfile(exec_path):
        state.error_message = f"Real-ESRGAN executable not found at: {exec_path}"
        state.status = "ERROR"
        state.logs.append(f"[Error] {state.error_message}")
        return False

    # Get input frames
    input_frames = get_image_files(input_dir)
    
    # Filter input frames by range if custom range is enabled
    use_range = settings.get('use_custom_range', False)
    if use_range:
        start_f = settings.get('frame_start', 1)
        end_f = settings.get('frame_end', 250)
        filtered = []
        for f in input_frames:
            num_match = re.search(r"(\d+)(?=\.[a-zA-Z0-9]+$)", f)
            if num_match:
                f_num = int(num_match.group(1))
                if start_f <= f_num <= end_f:
                    filtered.append(f)
        input_frames = filtered

    if not input_frames:
        if use_range:
            state.error_message = f"No image frames found in custom range [{start_f}, {end_f}] in: {input_dir}"
        else:
            state.error_message = f"No image frames found in input directory: {input_dir}"
        state.status = "ERROR"
        state.logs.append(f"[Error] {state.error_message}")
        return False

    os.makedirs(output_dir, exist_ok=True)
    
    # Check for existing upscaled frames (smart resume)
    existing_output_frames = get_image_files(output_dir)
    missing_frames = []
    for frame in input_frames:
        out_path = os.path.join(output_dir, frame)
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            missing_frames.append(frame)

    total_input = len(input_frames)
    total_missing = len(missing_frames)

    state.logs.append(f"[Upscale] Total frames: {total_input}, Missing: {total_missing}")
    
    if total_missing == 0:
        state.logs.append("[Upscale] All frames already upscaled. Skipping.")
        state.progress = 100.0
        return True

    # Check if input frames have an alpha channel (RGBA PNG)
    has_alpha = False
    if input_frames:
        first_frame_path = os.path.join(input_dir, input_frames[0])
        ext = os.path.splitext(first_frame_path)[1].lower()
        if ext == '.png':
            try:
                with open(first_frame_path, 'rb') as f:
                    f.seek(25)
                    color_type = f.read(1)[0]
                    if color_type in (4, 6):
                        has_alpha = True
                        state.logs.append("[Upscale] RGBA input sequence detected.")
            except Exception:
                pass

    preserve_alpha = settings.get('preserve_alpha', True)
    two_pass = preserve_alpha and has_alpha

    # Force temporary directories if doing range-filter, partial resume, or two-pass transparency
    use_temp_dir = use_range or (total_missing < total_input) or has_alpha
    
    run_dir = input_dir
    temp_dir = None
    temp_dir_rgb = None
    temp_dir_alpha = None
    temp_out_rgb = None
    temp_out_alpha = None

    ffmpeg_path = settings.get('ffmpeg_path', 'ffmpeg')
    startupinfo = None
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE

    if use_temp_dir:
        temp_dir = os.path.join(output_dir, "temp_resume_input")
        os.makedirs(temp_dir, exist_ok=True)
        
        if two_pass:
            state.logs.append("[Upscale] Preparing channels for transparent upscaling...")
            temp_dir_rgb = os.path.join(output_dir, "temp_resume_input_rgb")
            temp_dir_alpha = os.path.join(output_dir, "temp_resume_input_alpha")
            temp_out_rgb = os.path.join(output_dir, "temp_resume_output_rgb")
            temp_out_alpha = os.path.join(output_dir, "temp_resume_output_alpha")
            os.makedirs(temp_dir_rgb, exist_ok=True)
            os.makedirs(temp_dir_alpha, exist_ok=True)
            os.makedirs(temp_out_rgb, exist_ok=True)
            os.makedirs(temp_out_alpha, exist_ok=True)

        for frame in missing_frames:
            if state.cancel_requested:
                shutil.rmtree(temp_dir, ignore_errors=True)
                if temp_dir_rgb:
                    shutil.rmtree(temp_dir_rgb, ignore_errors=True)
                    shutil.rmtree(temp_dir_alpha, ignore_errors=True)
                    shutil.rmtree(temp_out_rgb, ignore_errors=True)
                    shutil.rmtree(temp_out_alpha, ignore_errors=True)
                state.status = "CANCELLED"
                return False
            
            src = os.path.join(input_dir, frame)
            dst = os.path.join(temp_dir, frame)
            
            if two_pass:
                # Split RGBA into RGB and Alpha
                dst_rgb = os.path.join(temp_dir_rgb, frame)
                dst_alpha = os.path.join(temp_dir_alpha, frame)
                cmd_rgb = [ffmpeg_path, "-y", "-i", src, "-pix_fmt", "rgb24", "-update", "1", dst_rgb]
                cmd_alpha = [ffmpeg_path, "-y", "-i", src, "-vf", "alphaextract", "-pix_fmt", "gray", "-update", "1", dst_alpha]
                try:
                    subprocess.run(cmd_rgb, startupinfo=startupinfo, capture_output=True)
                    subprocess.run(cmd_alpha, startupinfo=startupinfo, capture_output=True)
                except Exception as e:
                    state.logs.append(f"[Warning] Failed to split frame {frame}: {e}")
                    shutil.copy2(src, dst_rgb)
            elif has_alpha:
                # Strip alpha, copy to temp_dir
                cmd_conv = [ffmpeg_path, "-y", "-i", src, "-pix_fmt", "rgb24", "-update", "1", dst]
                try:
                    subprocess.run(cmd_conv, startupinfo=startupinfo, capture_output=True)
                except Exception as e:
                    state.logs.append(f"[Warning] Failed to convert frame {frame} to RGB: {e}")
                    shutil.copy2(src, dst)
            else:
                shutil.copy2(src, dst)
                
        run_dir = temp_dir

    gpu_id = settings.get('gpu_device', '0')
    if gpu_id == '-1':
        state.logs.append("[Upscale] Note: Real-ESRGAN does not support CPU mode. Defaulting to GPU 0 for upscaling.")
        gpu_id = '0'
        
    tile_size = settings.get('upscale_tile', 0)

    rc = 0
    if two_pass:
        # Pass 1: RGB Upscale
        state.logs.append("[Upscale] Pass 1/2: Upscaling RGB channels...")
        cmd_rgb = [
            exec_path,
            "-i", temp_dir_rgb,
            "-o", temp_out_rgb,
            "-s", str(scale),
            "-n", model,
            "-g", gpu_id,
            "-t", str(tile_size)
        ]
        rc = run_upscale_pass(cmd_rgb, 0.0, 0.5, missing_frames, total_input, total_missing, temp_out_rgb, state)
        
        if rc == 0 and not state.cancel_requested:
            # Pass 2: Alpha Upscale
            state.logs.append("[Upscale] Pass 2/2: Upscaling Alpha channels...")
            cmd_alpha = [
                exec_path,
                "-i", temp_dir_alpha,
                "-o", temp_out_alpha,
                "-s", str(scale),
                "-n", model,
                "-g", gpu_id,
                "-t", str(tile_size)
            ]
            rc = run_upscale_pass(cmd_alpha, 50.0, 0.5, missing_frames, total_input, total_missing, temp_out_alpha, state)
            
        if rc == 0 and not state.cancel_requested:
            # Merge RGB and Alpha back into output_dir
            state.logs.append("[Upscale] Merging upscaled RGB and Alpha channels to RGBA...")
            rgb_files = get_image_files(temp_out_rgb)
            for f in rgb_files:
                if state.cancel_requested:
                    break
                src_rgb = os.path.join(temp_out_rgb, f)
                src_alpha = os.path.join(temp_out_alpha, f)
                dst_rgba = os.path.join(output_dir, f)
                
                cmd_merge = [
                    ffmpeg_path, "-y",
                    "-i", src_rgb,
                    "-i", src_alpha,
                    "-filter_complex", "[0:v][1:v]alphamerge",
                    "-pix_fmt", "rgba",
                    "-update", "1",
                    dst_rgba
                ]
                try:
                    subprocess.run(cmd_merge, startupinfo=startupinfo, capture_output=True)
                except Exception as e:
                    state.logs.append(f"[Warning] Failed to merge frame {f}: {e}")
    else:
        # Standard Single-Pass Upscale
        cmd = [
            exec_path,
            "-i", run_dir,
            "-o", output_dir,
            "-s", str(scale),
            "-n", model,
            "-g", gpu_id,
            "-t", str(tile_size)
        ]
        rc = run_upscale_pass(cmd, 0.0, 1.0, missing_frames, total_input, total_missing, output_dir, state)

    # Clean up temp directories
    shutil.rmtree(temp_dir, ignore_errors=True)
    if temp_dir_rgb:
        shutil.rmtree(temp_dir_rgb, ignore_errors=True)
        shutil.rmtree(temp_dir_alpha, ignore_errors=True)
        shutil.rmtree(temp_out_rgb, ignore_errors=True)
        shutil.rmtree(temp_out_alpha, ignore_errors=True)
        
    if rc != 0 and not state.cancel_requested:
        state.error_message = f"Real-ESRGAN exited with error code {rc}"
        state.status = "ERROR"
        state.logs.append(f"[Error] {state.error_message}")
        return False

    state.logs.append("[Upscale] Upscaling completed successfully.")
    state.progress = 100.0
    return True
