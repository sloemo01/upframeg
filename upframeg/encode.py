import os
import re
import subprocess
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

def detect_sequence_pattern(directory):
    """
    Detects the FFmpeg sequence pattern, start number, and extension.
    Returns: (pattern, start_number, total_frames, extension)
    """
    files = get_image_files(directory)
    if not files:
        return None, 0, 0, ""

    first_file = files[0]
    total_frames = len(files)
    
    # We match the number at the end of the file name (right before extension)
    match = re.search(r"^(.*?)([0-9]+)(\.[a-zA-Z0-9]+)$", first_file)
    if not match:
        # Fallback if no numbers (e.g. single frame, or non-numbered files)
        return first_file, 0, total_frames, os.path.splitext(first_file)[1]

    prefix = match.group(1)
    digits = match.group(2)
    ext = match.group(3)
    
    padding = len(digits)
    start_number = int(digits)
    pattern = f"{prefix}%0{padding}d{ext}"
    
    return pattern, start_number, total_frames, ext

def run_encode(exec_path, input_dir, output_file, fps, codec_type, quality, gpu_accel, audio_file, use_grain, grain_strength, settings, state):
    """
    Compiles the final image sequence into a video using FFmpeg.
    """
    state.status = "ENCODING"
    state.progress = 0.0
    state.logs.append(f"[Encode] Starting FFmpeg encoding...")
    state.logs.append(f"[Encode] Input Dir: {input_dir}")
    state.logs.append(f"[Encode] Output File: {output_file}")
    state.logs.append(f"[Encode] Framerate: {fps} FPS")
    state.logs.append(f"[Encode] Codec: {codec_type}")
    state.logs.append(f"[Encode] GPU Acceleration: {gpu_accel}")
    state.logs.append(f"[Encode] Audio File: {audio_file}")
    use_deflicker = settings.get('use_deflicker', False)
    use_denoise = settings.get('use_denoise', False)
    denoise_strength = settings.get('denoise_strength', 15)
    
    state.logs.append(f"[Encode] Film Grain: {use_grain} (Strength: {grain_strength})")
    state.logs.append(f"[Encode] Temporal Deflicker: {use_deflicker}")
    state.logs.append(f"[Encode] Temporal Denoise: {use_denoise} (Strength: {denoise_strength})")
    
    # Resolve executable path (now pre-resolved on main thread)
    ffmpeg_resolved = exec_path
    
    # Detect sequence pattern
    pattern, start_number, total_frames, ext = detect_sequence_pattern(input_dir)
    if not pattern:
        state.error_message = f"No image sequence found in input directory: {input_dir}"
        state.status = "ERROR"
        state.logs.append(f"[Error] {state.error_message}")
        return False

    use_range = settings.get('use_custom_range', False)
    if use_range:
        start_number = settings.get('frame_start', 1)
        total_frames = settings.get('frame_end', 250) - start_number + 1

    state.logs.append(f"[Encode] Detected sequence pattern: {pattern} (Start frame: {start_number}, Total frames: {total_frames})")

    # Map codec settings
    # quality can be: 'LOW', 'MEDIUM', 'HIGH', 'LOSSLESS'
    # gpu_accel can be: 'NONE', 'NVENC', 'AMF', 'QSV'
    cmd = [
        ffmpeg_resolved,
        "-y",               # Overwrite output files without asking
        "-framerate", str(fps),
        "-start_number", str(start_number),
        "-i", os.path.join(input_dir, pattern)
    ]

    # If audio file is provided and valid, add as second input
    if audio_file and os.path.exists(audio_file):
        cmd.extend(["-i", audio_file])

    # Codec-specific arguments
    if gpu_accel == 'NVENC' and codec_type in {'H264', 'H265'}:
        codec = "h264_nvenc" if codec_type == 'H264' else "hevc_nvenc"
        cq_map = {'LOW': '28', 'MEDIUM': '23', 'HIGH': '18', 'LOSSLESS': '0'}
        cq = cq_map.get(quality, '18')
        cmd.extend([
            "-c:v", codec,
            "-rc", "constqp",
            "-qp", cq,
            "-pix_fmt", "yuv420p"
        ])
    elif gpu_accel == 'AMF' and codec_type in {'H264', 'H265'}:
        codec = "h264_amf" if codec_type == 'H264' else "hevc_amf"
        cmd.extend([
            "-c:v", codec,
            "-pix_fmt", "yuv420p"
        ])
    elif gpu_accel == 'QSV' and codec_type in {'H264', 'H265'}:
        codec = "h264_qsv" if codec_type == 'H264' else "hevc_qsv"
        q_map = {'LOW': '30', 'MEDIUM': '25', 'HIGH': '20', 'LOSSLESS': '15'}
        q = q_map.get(quality, '20')
        cmd.extend([
            "-c:v", codec,
            "-global_quality", q,
            "-pix_fmt", "nv12"
        ])
    else:
        # Fallback to CPU libx264/libx265
        if codec_type == 'H264':
            crf_map = {'LOW': '28', 'MEDIUM': '23', 'HIGH': '18', 'LOSSLESS': '0'}
            crf = crf_map.get(quality, '18')
            cmd.extend([
                "-c:v", "libx264",
                "-crf", crf,
                "-preset", "medium",
                "-pix_fmt", "yuv420p"
            ])
        elif codec_type == 'H265':
            crf_map = {'LOW': '30', 'MEDIUM': '25', 'HIGH': '20', 'LOSSLESS': '0'}
            crf = crf_map.get(quality, '20')
            cmd.extend([
                "-c:v", "libx265",
                "-crf", crf,
                "-preset", "medium",
                "-pix_fmt", "yuv420p"
            ])
        elif codec_type == 'PRORES':
            profile_map = {'LOW': '0', 'MEDIUM': '1', 'HIGH': '3', 'LOSSLESS': '4'}
            profile = profile_map.get(quality, '3')  # 3 is ProRes 422 HQ
            
            # If preserving transparency (Alpha channel), force ProRes 4444 profile and YUV+Alpha pixel format
            preserve_alpha = settings.get('preserve_alpha', False)
            if preserve_alpha:
                profile = '4'  # Force ProRes 4444
                pix_fmt = 'yuva444p10le'
            else:
                pix_fmt = 'yuv422p10le'
                
            cmd.extend([
                "-c:v", "prores_ks",
                "-profile:v", profile,
                "-vendor", "ap10",
                "-pix_fmt", pix_fmt
            ])
        else:
            cmd.extend([
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p"
            ])

    # Apply dynamic filter chain (Deflicker, Denoise, Film Grain)
    filters = []
    if use_deflicker:
        filters.append("deflicker")
    if use_denoise:
        spatial = max(0.1, denoise_strength * 0.1)
        temporal = max(0.1, denoise_strength * 0.2)
        filters.append(f"hqdn3d={spatial:.2f}:{spatial:.2f}:{temporal:.2f}:{temporal:.2f}")
    if use_grain:
        strength_val = max(1, int(grain_strength * 0.3))
        filters.append(f"noise=alls={strength_val}:allf=t")

    if filters:
        cmd.extend(["-vf", ",".join(filters)])

    # Mux audio stream if audio file exists
    if audio_file and os.path.exists(audio_file):
        cmd.extend([
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:a", "aac",
            "-shortest"
        ])

    if use_range:
        cmd.extend(["-vframes", str(total_frames)])

    cmd.append(output_file)

    state.logs.append(f"[Encode] Executing command: {' '.join(cmd)}")

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
            startupinfo=startupinfo
        )
    except Exception as e:
        state.error_message = f"Failed to start FFmpeg subprocess: {str(e)}"
        state.status = "ERROR"
        state.logs.append(f"[Error] {state.error_message}")
        return False

    frame_pattern = re.compile(r"frame=\s*(\d+)")
    start_time = time.time()

    q = queue.Queue()
    t = threading.Thread(target=enqueue_output, args=(process.stdout, q))
    t.daemon = True
    t.start()

    while True:
        if state.cancel_requested:
            state.logs.append("[Encode] Cancellation requested. Terminating subprocess...")
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
            state.status = "CANCELLED"
            return False

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
            state.logs.append(f"[FFmpeg] {clean_line}")
            if len(state.logs) > 100:
                state.logs.pop(0)

        # Parse frame progress
        match = frame_pattern.search(clean_line)
        if match:
            current_frame = int(match.group(1))
            if total_frames > 0:
                val = (current_frame / total_frames) * 100.0
                state.progress = min(100.0, val)
                
                # Estimate remaining time
                elapsed = time.time() - start_time
                if current_frame > 0:
                    total_est = elapsed / (current_frame / total_frames)
                    rem = max(0.0, total_est - elapsed)
                    state.eta = time.strftime("%H:%M:%S", time.gmtime(rem))

    rc = process.poll()
    if rc != 0 and not state.cancel_requested:
        state.error_message = f"FFmpeg exited with error code {rc}"
        state.status = "ERROR"
        state.logs.append(f"[Error] {state.error_message}")
        return False

    state.logs.append("[Encode] FFmpeg encoding completed successfully.")
    state.progress = 100.0
    return True
