# -*- coding: utf-8 -*-
"""
استخراج فریم با فاصله‌ی زمانی ثابت (پیش‌فرض هر ۳ ثانیه یک فریم)، طبق درخواست تیم.
مثال: ویدیوی ۵ دقیقه‌ای (۳۰۰ ثانیه) / ۳ = ۱۰۰ فریم.

قابل تنظیم از config.json -> "frame_interval_seconds" یا env FRAME_INTERVAL_SECONDS.
یک سقف ایمنی (MAX_FRAMES_SAFETY_CAP) هم هست تا اگر یک ویدیو غیرعادی بلند بود، هزینه/زمان
از کنترل خارج نشود.

وابستگی سیستمی لازم: ffmpeg و ffprobe (باید در PATH باشند)
"""

import subprocess
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed

import config

DEFAULT_INTERVAL_SECONDS = 3
MAX_FRAMES_SAFETY_CAP = 200
EXTRACTION_WORKERS = 8  # ffmpeg هر فراخوانی یک پردازش جدا و مستقل است، پس موازی‌سازی امن است


class FrameExtractionError(Exception):
    pass


def _check_ffmpeg():
    if shutil.which('ffmpeg') is None or shutil.which('ffprobe') is None:
        raise FrameExtractionError("ffmpeg / ffprobe روی سیستم نصب نیست. طبق README نصبش کن.")


def get_duration_seconds(video_path: str) -> float:
    _check_ffmpeg()
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', video_path],
        capture_output=True, text=True, timeout=30
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        raise FrameExtractionError(f"مدت زمان ویدیو قابل خواندن نبود: {result.stderr.strip()}")


def classify_video_type(duration_sec: float) -> str:
    # این مقدار مستقیم داخل system prompt فارسی مدل استفاده می‌شود، عمداً فارسی نگه داشته شده.
    if duration_sec <= 130:  # کمی بیشتر از ۲ دقیقه برای اغماض
        return 'تیزر'
    return 'توضیحات'


def _get_interval_seconds() -> float:
    try:
        val = float(config.get('frame_interval_seconds', 'FRAME_INTERVAL_SECONDS', DEFAULT_INTERVAL_SECONDS))
    except (TypeError, ValueError):
        val = DEFAULT_INTERVAL_SECONDS
    return val if val > 0 else DEFAULT_INTERVAL_SECONDS


def _extract_frame_at(video_path: str, timestamp: float, out_path: str):
    cmd = [
        'ffmpeg', '-y', '-ss', str(timestamp), '-i', video_path,
        '-frames:v', '1', '-q:v', '3',
        '-vf', 'scale=960:-2',  # ریسایز برای کنترل حجم/توکن قبل از ارسال به AI
        out_path
    ]
    subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def extract_frames(video_path: str, work_dir: str, progress_cb=None):
    """
    خروجی: (video_type, duration_sec, frames)
      frames = [{'timestamp': float, 'path': str}, ...]  به ترتیب زمانی، با فاصله‌ی ثابت
    progress_cb (اختیاری): تابعی که با یک رشته‌ی متنی برای نمایش پیشرفت صدا زده می‌شود.
    """
    _check_ffmpeg()
    os.makedirs(work_dir, exist_ok=True)

    duration_sec = get_duration_seconds(video_path)
    video_type = classify_video_type(duration_sec)
    interval = _get_interval_seconds()

    timestamps = []
    t = 0.0
    while t < duration_sec:
        timestamps.append(round(t, 2))
        t += interval
    # همیشه نزدیک به انتهای ویدیو هم یک فریم داشته باشیم (برای چک «ابتدا و انتهای مشخص»)
    if not timestamps or (duration_sec - timestamps[-1]) > 0.5:
        timestamps.append(round(max(duration_sec - 0.3, 0.0), 2))

    if len(timestamps) > MAX_FRAMES_SAFETY_CAP:
        step = len(timestamps) / MAX_FRAMES_SAFETY_CAP
        sampled = [timestamps[int(i * step)] for i in range(MAX_FRAMES_SAFETY_CAP)]
        timestamps = sorted(set(sampled + [timestamps[0], timestamps[-1]]))

    frames_dir = os.path.join(work_dir, 'final_frames')
    os.makedirs(frames_dir, exist_ok=True)

    total = len(timestamps)
    if progress_cb:
        progress_cb(f"Extracting {total} frames…")

    results = {}
    done_count = [0]

    def _job(i, ts):
        out_path = os.path.join(frames_dir, f'frame_{i:04d}_{ts:.2f}s.jpg')
        _extract_frame_at(video_path, ts, out_path)
        return i, ts, out_path

    with ThreadPoolExecutor(max_workers=EXTRACTION_WORKERS) as pool:
        futures = [pool.submit(_job, i, ts) for i, ts in enumerate(timestamps)]
        for fut in as_completed(futures):
            i, ts, out_path = fut.result()
            if os.path.exists(out_path):
                results[i] = {'timestamp': ts, 'path': out_path}
            done_count[0] += 1
            if progress_cb and done_count[0] % 10 == 0:
                progress_cb(f"Extracting frames… {done_count[0]}/{total}")

    frames = [results[i] for i in sorted(results.keys())]

    if not frames:
        raise FrameExtractionError("هیچ فریمی از ویدیو استخراج نشد؛ فایل ویدیو را بررسی کن.")

    if progress_cb:
        progress_cb(f"Extracted {len(frames)} frames")

    return video_type, duration_sec, frames
