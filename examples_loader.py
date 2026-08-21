# -*- coding: utf-8 -*-
"""
خواندن فایل اکسل نمونه‌های واقعی اصلاحیه (که تیم QC انسانی قبلاً ثبت کرده) و ساختن
یک بلوک «نمونه‌های واقعی» که به system prompt اضافه می‌شود. هدف: AI سبک نوشتن،
سطح سخت‌گیری، و انواع واقعی ایرادهایی که رخ می‌دهد را از روی نمونه‌های واقعی یاد بگیرد
(few-shot)، نه فقط از روی قوانین خشک.

فایل اکسل باید کنار server.py باشد و این ستون‌ها را داشته باشد (ردیف اول/دوم هدر است):
  برند | دسته‌بندی | DKP | شرح اصلاحات | تعداد اصلاحیه

اگر فایل پیدا نشود، این ماژول بی‌سروصدا رشته‌ی خالی برمی‌گرداند (یعنی سیستم بدون این
مثال‌ها هم کار می‌کند، فقط دقتش کمتر کالیبره شده است).
"""

import os
import re

EXCEL_FILENAME = 'eslahie_for_qc.xlsx'
EXAMPLES_PER_CATEGORY = 5
MAX_EXAMPLE_LENGTH = 220  # نمونه‌های خیلی طولانی را کوتاه می‌کنیم تا prompt سنگین نشود

# دسته‌بندی بر اساس کلیدواژه‌های پرتکرار (بر اساس تحلیل واقعی داده‌ها)
CATEGORIES = [
    ('accessories_disclaimer', ['اقلام همراه', 'بسته بندی', 'بسته‌بندی']),
    ('product_mismatch', ['مغایرت', 'یکی نیست', 'مطابقت ندارد']),
    ('logo_body_color', ['لوگو', 'بدنه', 'رنگ', 'فونت']),
    ('translation_grammar', ['ترجمه', 'جمله', 'فعل', 'کاما', 'نیم فاصله', 'غلط املایی']),
    ('subtitle_format', ['زیرنویس']),
    ('visual_quality', ['کیفیت', 'تار', 'رزولوشن']),
    ('watermark', ['واترمارک', 'QR']),
    ('content_rules', ['مقایسه', 'تکراری', 'قیمت', 'صدا', 'موسیقی']),
]


def _clean_text(t: str) -> str:
    t = str(t).strip()
    t = re.sub(r'\s+', ' ', t)
    if len(t) > MAX_EXAMPLE_LENGTH:
        t = t[:MAX_EXAMPLE_LENGTH].rstrip() + '…'
    return t


def _categorize(text: str) -> str:
    for cat_name, keywords in CATEGORIES:
        if any(kw in text for kw in keywords):
            return cat_name
    return 'other'


def _load_raw_rows():
    excel_path = os.path.join(os.path.dirname(__file__), EXCEL_FILENAME)
    if not os.path.exists(excel_path):
        return []
    try:
        import openpyxl
    except ImportError:
        print("⚠️  openpyxl نصب نیست؛ نمونه‌های اصلاحیه بارگذاری نمی‌شوند (pip install openpyxl)")
        return []

    try:
        wb = openpyxl.load_workbook(excel_path, data_only=True, read_only=True)
        ws = wb.worksheets[0]
        rows = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i < 2:  # دو ردیف اول هدر فارسی/انگلیسی است
                continue
            if len(row) >= 4 and row[3] and str(row[3]).strip():
                rows.append(_clean_text(row[3]))
        return rows
    except Exception as e:
        print(f"⚠️  خواندن {EXCEL_FILENAME} با خطا مواجه شد ({e}); نمونه‌های اصلاحیه بارگذاری نمی‌شوند")
        return []


_examples_block_cache = None


def build_examples_block() -> str:
    """
    خروجی: یک رشته‌ی متنی آماده برای اضافه‌شدن به system prompt، حاوی چند نمونه‌ی واقعی
    از هر دسته‌بندی ایراد. اگر فایل اکسل موجود نبود، رشته‌ی خالی برمی‌گرداند.
    """
    global _examples_block_cache
    if _examples_block_cache is not None:
        return _examples_block_cache

    texts = _load_raw_rows()
    if not texts:
        _examples_block_cache = ''
        return ''

    buckets = {}
    seen = set()
    for t in texts:
        key = t[:40]  # دیدوپلیکیت ساده برای جلوگیری از نمونه‌های تقریبا تکراری
        if key in seen:
            continue
        seen.add(key)
        cat = _categorize(t)
        buckets.setdefault(cat, []).append(t)

    lines = [
        "\n===================================================================",
        "نمونه‌های واقعی اصلاحیه (ثبت‌شده توسط تیم QC انسانی در گذشته)",
        "===================================================================",
        "این‌ها نمونه‌های واقعی دلایل رد/اصلاحیه هستند که قبلاً توسط بازبین‌های انسانی روی",
        "ویدیوهای واقعی ثبت شده‌اند. سبک نوشتن، سطح دقت، و نوع ایرادهایی که واقعاً رخ می‌دهد",
        "را از این نمونه‌ها یاد بگیر و در تحلیل خودت از همین سطح دقت و لحن استفاده کن:\n",
    ]

    cat_titles = {
        'accessories_disclaimer': 'اقلام همراه / بسته‌بندی',
        'product_mismatch': 'مغایرت محصول',
        'logo_body_color': 'لوگو / بدنه / رنگ / فونت',
        'translation_grammar': 'ترجمه و نگارش',
        'subtitle_format': 'قالب‌بندی زیرنویس',
        'visual_quality': 'کیفیت بصری',
        'watermark': 'واترمارک / QR',
        'content_rules': 'قوانین محتوایی (مقایسه، تکراری، قیمت، صدا)',
        'other': 'سایر',
    }

    for cat_name, _ in CATEGORIES + [('other', [])]:
        items = buckets.get(cat_name, [])
        if not items:
            continue
        sample = items[:EXAMPLES_PER_CATEGORY]
        lines.append(f"▸ {cat_titles.get(cat_name, cat_name)}:")
        for s in sample:
            lines.append(f"  - {s}")
        lines.append("")

    block = "\n".join(lines)
    _examples_block_cache = block
    return block
