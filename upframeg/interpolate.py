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

def count_contiguous_frames(directory):
    """Count contiguous output frames named 00000001.png, 00000002.png, etc."""
    if not os.path.exists(directory):
        return 0
    count = 0
    while True:
        next_index = count + 1
        filename = f"{next_index:08d}.png"  # RIFE standard output format is 8-digit zero-padded png
        filepath = os.path.join(directory, filename)
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            count += 1
        else:
            break
    return count

def run_interpolate_pass(cmd, start_progress, progress_scale, remaining_intervals, total_intervals, aligned_count, stride, state):
    startupinfo = None
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            startupinfo=startupinfo
        )
    except Exception as e:
        state.error_message = f"Failed to start RIFE subprocess: {str(e)}"
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
            state.logs.append("[Interpolate] Cancellation requested. Terminating subprocess...")
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
            state.logs.append(f"[RIFE] {clean_line}")
            if len(state.logs) > 100:
                state.logs.pop(0)

        # Parse progress
        match = progress_pattern.search(clean_line)
        if match:
            val = float(match.group(1))
            if aligned_count >= stride:
                # Adjust progress for resume
                ratio = remaining_intervals / total_intervals
                already_done = (total_intervals - remaining_intervals) / total_intervals * 100.0
                pass_progress = already_done + (val * ratio)
            else:
                pass_progress = val
            state.progress = min(99.9, start_progress + (pass_progress * progress_scale))
            
            # Estimate remaining time
            elapsed = time.time() - start_time
            if state.progress > 0:
                total_est = elapsed / (state.progress / 100.0)
                rem = max(0.0, total_est - elapsed)
                state.eta = time.strftime("%H:%M:%S", time.gmtime(rem))

    rc = process.poll()
    return rc


def run_interpolate(exec_path, input_dir, output_dir, multiplier, model, settings, state):
    """
    Runs RIFE frame interpolation.
    Multiplier can be 2, 4, 8.
    In RIFE CLI:
    -n target_frame_count (total number of output frames).
    """
    state.status = "INTERPOLATING"
    state.progress = 0.0
    state.logs.append(f"[Interpolate] Starting RIFE frame interpolation...")
    state.logs.append(f"[Interpolate] Input Dir: {input_dir}")
    state.logs.append(f"[Interpolate] Output Dir: {output_dir}")
    state.logs.append(f"[Interpolate] Multiplier: {multiplier}x")
    
    if not os.path.exists(exec_path) or not os.path.isfile(exec_path):
        state.error_message = f"RIFE executable not found at: {exec_path}"
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
            state.error_message = f"No upscaled image frames found in custom range [{start_f}, {end_f}] in: {input_dir}"
        else:
            state.error_message = f"No upscaled image frames found in input directory: {input_dir}"
        state.status = "ERROR"
        state.logs.append(f"[Error] {state.error_message}")
        return False

    total_input = len(input_frames)
    if total_input < 2:
        state.error_message = "RIFE requires at least 2 frames to interpolate."
        state.status = "ERROR"
        state.logs.append(f"[Error] {state.error_message}")
        return False

    # Map multiplier to stride
    stride = multiplier

    os.makedirs(output_dir, exist_ok=True)

    # Resume Logic
    contiguous_count = count_contiguous_frames(output_dir)
    state.logs.append(f"[Interpolate] Found {contiguous_count} existing contiguous output frames.")

    # Align contiguous_count to stride
    aligned_count = (contiguous_count // stride) * stride
    
    last_input_idx = 1
    if aligned_count >= stride:
        last_input_idx = 1 + (aligned_count // stride)

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
                        state.logs.append("[Interpolate] RGBA input sequence detected.")
            except Exception:
                pass

    preserve_alpha = settings.get('preserve_alpha', True)
    two_pass = preserve_alpha and has_alpha
    
    use_temp_dirs = use_range or (aligned_count >= stride) or has_alpha or two_pass
    
    temp_in_dir = None
    temp_out_dir = None
    temp_dir_rgb = None
    temp_dir_alpha = None
    temp_out_rgb = None
    temp_out_alpha = None
    
    run_in_dir = input_dir
    run_out_dir = output_dir
    offset = 0

    if use_temp_dirs:
        temp_in_dir = os.path.join(output_dir, "temp_rife_input")
        temp_out_dir = os.path.join(output_dir, "temp_rife_output")
        os.makedirs(temp_in_dir, exist_ok=True)
        os.makedirs(temp_out_dir, exist_ok=True)
        
        if two_pass:
            state.logs.append("[Interpolate] Preparing channels for transparent interpolation...")
            temp_dir_rgb = os.path.join(output_dir, "temp_rife_input_rgb")
            temp_dir_alpha = os.path.join(output_dir, "temp_rife_input_alpha")
            temp_out_rgb = os.path.join(output_dir, "temp_rife_output_rgb")
            temp_out_alpha = os.path.join(output_dir, "temp_rife_output_alpha")
            os.makedirs(temp_dir_rgb, exist_ok=True)
            os.makedirs(temp_dir_alpha, exist_ok=True)
            os.makedirs(temp_out_rgb, exist_ok=True)
            os.makedirs(temp_out_alpha, exist_ok=True)

        if aligned_count >= stride:
            start_list_idx = last_input_idx - 1
            if start_list_idx >= total_input:
                state.logs.append("[Interpolate] All frames already interpolated. Skipping.")
                state.progress = 100.0
                shutil.rmtree(temp_in_dir, ignore_errors=True)
                shutil.rmtree(temp_out_dir, ignore_errors=True)
                if temp_dir_rgb:
                    shutil.rmtree(temp_dir_rgb, ignore_errors=True)
                    shutil.rmtree(temp_dir_alpha, ignore_errors=True)
                    shutil.rmtree(temp_out_rgb, ignore_errors=True)
                    shutil.rmtree(temp_out_alpha, ignore_errors=True)
                return True
            state.logs.append(f"[Interpolate] Resuming from input frame index {last_input_idx} (out of {total_input})")
            offset = aligned_count
            
            # Clean up frames after the aligned stride to prevent duplicate or corrupt files
            for i in range(aligned_count + 1, contiguous_count + 1):
                bad_file = os.path.join(output_dir, f"{i:08d}.png")
                if os.path.exists(bad_file):
                    os.remove(bad_file)
        else:
            start_list_idx = 0
            state.logs.append("[Interpolate] Starting from scratch. Clearing output directory...")
            for f in os.listdir(output_dir):
                fp = os.path.join(output_dir, f)
                if os.path.isfile(fp):
                    os.remove(fp)
            offset = 0
            
        state.logs.append(f"[Interpolate] Copying {total_input - start_list_idx} frames for interpolation run...")
        ffmpeg_path = settings.get('ffmpeg_path', 'ffmpeg')
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0  # SW_HIDE

        for idx in range(start_list_idx, total_input):
            if state.cancel_requested:
                shutil.rmtree(temp_in_dir, ignore_errors=True)
                shutil.rmtree(temp_out_dir, ignore_errors=True)
                if temp_dir_rgb:
                    shutil.rmtree(temp_dir_rgb, ignore_errors=True)
                    shutil.rmtree(temp_dir_alpha, ignore_errors=True)
                    shutil.rmtree(temp_out_rgb, ignore_errors=True)
                    shutil.rmtree(temp_out_alpha, ignore_errors=True)
                state.status = "CANCELLED"
                return False
            frame_name = input_frames[idx]
            src_path = os.path.join(input_dir, frame_name)
            dst_path = os.path.join(temp_in_dir, frame_name)
            
            if two_pass:
                dst_rgb = os.path.join(temp_dir_rgb, frame_name)
                dst_alpha = os.path.join(temp_dir_alpha, frame_name)
                cmd_rgb = [ffmpeg_path, "-y", "-i", src_path, "-pix_fmt", "rgb24", "-update", "1", dst_rgb]
                cmd_alpha = [ffmpeg_path, "-y", "-i", src_path, "-vf", "alphaextract", "-pix_fmt", "gray", "-update", "1", dst_alpha]
                try:
                    subprocess.run(cmd_rgb, startupinfo=startupinfo, capture_output=True)
                    subprocess.run(cmd_alpha, startupinfo=startupinfo, capture_output=True)
                except Exception as e:
                    state.logs.append(f"[Warning] Failed to split frame {frame_name}: {e}")
                    shutil.copy2(src_path, dst_rgb)
            elif has_alpha:
                cmd_conv = [ffmpeg_path, "-y", "-i", src_path, "-pix_fmt", "rgb24", "-update", "1", dst_path]
                try:
                    subprocess.run(cmd_conv, startupinfo=startupinfo, capture_output=True)
                except Exception as e:
                    state.logs.append(f"[Warning] Failed to convert frame {frame_name} to RGB: {e}")
                    shutil.copy2(src_path, dst_path)
            else:
                shutil.copy2(src_path, dst_path)
            
        run_in_dir = temp_in_dir
        run_out_dir = temp_out_dir
    else:
        state.logs.append("[Interpolate] Starting from scratch. Clearing output directory...")
        for f in os.listdir(output_dir):
            fp = os.path.join(output_dir, f)
            if os.path.isfile(fp):
                os.remove(fp)
        offset = 0

    # Calculate target frame count
    if two_pass:
        run_input_files = get_image_files(temp_dir_rgb)
    else:
        run_input_files = get_image_files(run_in_dir)
    num_input_frames = len(run_input_files)
    target_frame_count = num_input_frames * multiplier

    gpu_id = settings.get('gpu_device', '0')
    ffmpeg_path = settings.get('ffmpeg_path', 'ffmpeg')
    startupinfo = None
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE

    # Calculate progress scales
    total_intervals = total_input - 1
    start_input_idx = last_input_idx if (aligned_count >= stride) else 1
    remaining_intervals = total_input - start_input_idx

    rc = 0
    if two_pass:
        # Pass 1: RGB RIFE
        state.logs.append("[Interpolate] Pass 1/2: Interpolating RGB channels...")
        cmd_rgb = [
            exec_path,
            "-i", temp_dir_rgb,
            "-o", temp_out_rgb,
            "-n", str(target_frame_count)
        ]
        if model:
            cmd_rgb.extend(["-m", model])
        cmd_rgb.extend(["-g", gpu_id])
        
        state.logs.append(f"[Interpolate] Executing RGB command: {' '.join(cmd_rgb)}")
        rc = run_interpolate_pass(cmd_rgb, 0.0, 0.5, remaining_intervals, total_intervals, aligned_count, stride, state)
        
        if rc == 0 and not state.cancel_requested:
            # Pass 2: Alpha RIFE
            state.logs.append("[Interpolate] Pass 2/2: Interpolating Alpha channels...")
            cmd_alpha = [
                exec_path,
                "-i", temp_dir_alpha,
                "-o", temp_out_alpha,
                "-n", str(target_frame_count)
            ]
            if model:
                cmd_alpha.extend(["-m", model])
            cmd_alpha.extend(["-g", gpu_id])
            
            state.logs.append(f"[Interpolate] Executing Alpha command: {' '.join(cmd_alpha)}")
            rc = run_interpolate_pass(cmd_alpha, 50.0, 0.5, remaining_intervals, total_intervals, aligned_count, stride, state)
            
        if rc == 0 and not state.cancel_requested:
            # Merge RGB and Alpha back into temp_out_dir
            state.logs.append("[Interpolate] Merging RIFE RGB and Alpha channels to RGBA...")
            rgb_files = get_image_files(temp_out_rgb)
            for f in rgb_files:
                if state.cancel_requested:
                    break
                src_rgb = os.path.join(temp_out_rgb, f)
                src_alpha = os.path.join(temp_out_alpha, f)
                dst_rgba = os.path.join(temp_out_dir, f)
                
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
        cmd = [
            exec_path,
            "-i", run_in_dir,
            "-o", run_out_dir,
            "-n", str(target_frame_count)
        ]
        if model:
            cmd.extend(["-m", model])
        cmd.extend(["-g", gpu_id])
        
        state.logs.append(f"[Interpolate] Executing command: {' '.join(cmd)}")
        rc = run_interpolate_pass(cmd, 0.0, 1.0, remaining_intervals, total_intervals, aligned_count, stride, state)

    # If we ran in temp directories, rename and copy files to main output folder
    if rc == 0 and use_temp_dirs and not state.cancel_requested:
        state.logs.append("[Interpolate] Run completed. Renaming and merging files...")
        temp_out_files = get_image_files(temp_out_dir)
        
        for f in temp_out_files:
            if state.cancel_requested:
                break
            num_match = re.search(r"(\d+)", f)
            if num_match:
                local_idx = int(num_match.group(1))
                global_idx = local_idx + offset
                global_name = f"{global_idx:08d}.png"
                src_path = os.path.join(temp_out_dir, f)
                dst_path = os.path.join(output_dir, global_name)
                shutil.move(src_path, dst_path)

    # Clean up temp directories
    if temp_in_dir:
        shutil.rmtree(temp_in_dir, ignore_errors=True)
        shutil.rmtree(temp_out_dir, ignore_errors=True)
    if temp_dir_rgb:
        shutil.rmtree(temp_dir_rgb, ignore_errors=True)
        shutil.rmtree(temp_dir_alpha, ignore_errors=True)
        shutil.rmtree(temp_out_rgb, ignore_errors=True)
        shutil.rmtree(temp_out_alpha, ignore_errors=True)

    if state.cancel_requested:
        state.status = "CANCELLED"
        return False

    if rc != 0:
        state.error_message = f"RIFE exited with error code {rc}"
        state.status = "ERROR"
        state.logs.append(f"[Error] {state.error_message}")
        return False

    state.logs.append("[Interpolate] Interpolation completed successfully.")
    state.progress = 100.0
    return True
