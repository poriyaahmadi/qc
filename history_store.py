# -*- coding: utf-8 -*-
"""
ذخیره‌ی تاریخچه‌ی کدهای بررسی‌شده در یک فایل JSONL (append-only). هر خط یک رکورد
JSON کامل است — سبک، سریع برای اضافه‌کردن، و فایل خیلی حجیمی نمی‌شود (چند ده هزار
رکورد هم چند مگابایت بیشتر نمی‌شود).
"""

import json
import os
import threading
from datetime import datetime, timezone

HISTORY_PATH = os.path.join(os.path.dirname(__file__), 'history.jsonl')
_lock = threading.Lock()


def append_record(record: dict):
    record = dict(record)
    record.setdefault('timestamp', datetime.now(timezone.utc).isoformat())
    with _lock:
        with open(HISTORY_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')


def get_all(limit: int = None):
    """جدیدترین رکوردها اول برگردانده می‌شوند."""
    if not os.path.exists(HISTORY_PATH):
        return []
    records = []
    with _lock:
        with open(HISTORY_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    records.reverse()
    if limit:
        records = records[:limit]
    return records


def get_summary():
    records = get_all()
    total_cost = sum(r.get('cost_usd') or 0 for r in records)
    total_videos = len(records)
    accepted = sum(1 for r in records if r.get('final_decision') == 'accept')
    rejected = sum(1 for r in records if r.get('final_decision') == 'reject')
    errors = sum(1 for r in records if r.get('status') == 'error')
    return {
        'total_cost_usd': round(total_cost, 6),
        'total_videos': total_videos,
        'accepted': accepted,
        'rejected': rejected,
        'errors': errors,
    }
