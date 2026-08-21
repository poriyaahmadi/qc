# -*- coding: utf-8 -*-
"""
دانلود ویدیو از لینک Mega یا Google Drive.
وابستگی‌های سیستمی لازم (باید جدا نصب شوند):
  - megatools  (برای مگا: دستورهای megadl و megals)   -> نصب: apt install megatools  (مک: brew install megatools)
  - yt-dlp     (برای گوگل درایو)                        -> نصب: pip install yt-dlp
"""

import subprocess
import os
import sys
import json
import shutil

VIDEO_EXT = ('.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v')


class DownloadError(Exception):
    pass


def detect_link_type(link: str) -> str:
    if 'mega.nz' in link or 'mega.co.nz' in link:
        return 'mega'
    if 'drive.google.com' in link or 'docs.google.com' in link:
        return 'drive'
    return 'unknown'


def is_folder_link(link: str, link_type: str) -> bool:
    if link_type == 'mega':
        return '/folder/' in link or '#F!' in link
    if link_type == 'drive':
        return '/folders/' in link
    return False


def _check_binary(name: str):
    if shutil.which(name) is None:
        raise DownloadError(
            f"ابزار «{name}» روی سیستم شما نصب نیست. لطفاً طبق راهنمای README نصبش کن."
        )


def _check_ytdlp():
    """
    به‌جای جست‌وجوی باینری yt-dlp در PATH سیستم (که وقتی داخل یک virtualenv نصب
    شده ممکن است پیدا نشود)، مستقیماً همان پایتونی که سرور با آن اجرا شده را
    برای اجرای ماژول yt_dlp به کار می‌بریم. این یعنی هر جا pip install -r
    requirements.txt کار کرده باشد، این هم کار می‌کند.
    """
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        raise DownloadError(
            "پکیج yt-dlp نصب نیست. داخل پوشه‌ی پروژه دوباره اسکریپت run.sh / run.bat را اجرا کن "
            "تا وابستگی‌ها نصب شوند."
        )


def _ytdlp_cmd(*args):
    return [sys.executable, '-m', 'yt_dlp', *args]


def _find_newest_video(dest_dir: str) -> str:
    files = [
        os.path.join(dest_dir, f) for f in os.listdir(dest_dir)
        if f.lower().endswith(VIDEO_EXT)
    ]
    if not files:
        raise DownloadError("بعد از دانلود، هیچ فایل ویدیویی در پوشه پیدا نشد.")
    return max(files, key=os.path.getmtime)


# ---------------------------------------------------------------- Mega -----

def list_mega_folder(link: str):
    _check_binary('megals')
    result = subprocess.run(
        ['megals', '-e', link],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        raise DownloadError(f"خطا در خواندن فولدر مگا: {result.stderr.strip()}")
    all_lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    return [ln for ln in all_lines if ln.lower().endswith(VIDEO_EXT)]


def download_mega(link: str, dest_dir: str) -> str:
    _check_binary('megadl')
    os.makedirs(dest_dir, exist_ok=True)
    result = subprocess.run(
        ['megadl', '--path', dest_dir, link],
        capture_output=True, text=True, timeout=1800
    )
    if result.returncode != 0:
        raise DownloadError(f"دانلود از مگا شکست خورد: {result.stderr.strip()}")
    return _find_newest_video(dest_dir)


# -------------------------------------------------------------- Drive ------

def list_drive_folder(link: str):
    _check_ytdlp()
    result = subprocess.run(
        _ytdlp_cmd('--flat-playlist', '-J', link),
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        raise DownloadError(f"خطا در خواندن فولدر گوگل‌درایو: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise DownloadError("خروجی yt-dlp قابل خواندن نبود؛ لینک فولدر را بررسی کن.")
    entries = data.get('entries', []) or []
    return [
        {'id': e.get('id'), 'title': e.get('title') or e.get('id'), 'url': e.get('url') or e.get('webpage_url')}
        for e in entries
    ]


def download_drive(link: str, dest_dir: str) -> str:
    _check_ytdlp()
    os.makedirs(dest_dir, exist_ok=True)
    result = subprocess.run(
        _ytdlp_cmd('-o', os.path.join(dest_dir, '%(title)s.%(ext)s'), link),
        capture_output=True, text=True, timeout=1800
    )
    if result.returncode != 0:
        raise DownloadError(f"دانلود از گوگل‌درایو شکست خورد: {result.stderr.strip()}")
    return _find_newest_video(dest_dir)


# ------------------------------------------------------------- Entrypoint --

def download_video(link: str, dest_dir: str) -> dict:
    """
    خروجی یکی از این دو حالت است:
      {'status': 'downloaded', 'path': '/local/path/video.mp4'}
      {'status': 'needs_selection', 'link_type': 'mega'|'drive', 'files': [...]}
    """
    link_type = detect_link_type(link)
    if link_type == 'unknown':
        raise DownloadError("لینک نه مگا است نه گوگل‌درایو. لینک را بررسی کن.")

    if link_type == 'mega':
        if is_folder_link(link, link_type):
            files = list_mega_folder(link)
            if len(files) == 0:
                raise DownloadError("هیچ فایل ویدیویی داخل فولدر مگا پیدا نشد.")
            if len(files) > 1:
                return {'status': 'needs_selection', 'link_type': 'mega', 'files': files}
            path = download_mega(link, dest_dir)
            return {'status': 'downloaded', 'path': path}
        path = download_mega(link, dest_dir)
        return {'status': 'downloaded', 'path': path}

    # drive
    if is_folder_link(link, link_type):
        entries = list_drive_folder(link)
        if len(entries) == 0:
            raise DownloadError("هیچ فایلی داخل فولدر گوگل‌درایو پیدا نشد.")
        if len(entries) > 1:
            return {'status': 'needs_selection', 'link_type': 'drive', 'files': entries}
        path = download_drive(entries[0]['url'], dest_dir)
        return {'status': 'downloaded', 'path': path}
    path = download_drive(link, dest_dir)
    return {'status': 'downloaded', 'path': path}


def download_selected_file(link_type: str, file_ref: str, dest_dir: str) -> str:
    """وقتی از یک فولدر، کاربر یک فایل مشخص را انتخاب کرد."""
    if link_type == 'mega':
        return download_mega(file_ref, dest_dir)
    return download_drive(file_ref, dest_dir)
