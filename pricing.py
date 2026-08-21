# -*- coding: utf-8 -*-
"""
جدول قیمت مدل‌ها (دلار به‌ازای هر یک میلیون توکن) و محاسبه‌ی دقیق هزینه بر اساس
مصرف واقعی توکن که از پاسخ خود API برمی‌گردد (نه تخمین از روی تعداد فریم).

منبع قیمت‌ها: مستندات رسمی Anthropic و Groq (بررسی‌شده در اوت ۲۰۲۶).
⚠️ Claude Sonnet 5 تا 2026-08-31 قیمت مقدماتی ($2/$10) دارد و از 2026-09-01 به $3/$15
   تغییر می‌کند؛ اگر بعد از آن تاریخ استفاده می‌کنی، PRICING را به‌روزرسانی کن.
"""

from datetime import date

# (نرخ ورودی، نرخ خروجی) به ازای هر ۱ میلیون توکن، به دلار
PRICING = {
    # --- Anthropic ---
    'claude-haiku-4-5-20251001': (1.00, 5.00),
    'claude-haiku-4-5': (1.00, 5.00),
    'claude-sonnet-5': (2.00, 10.00) if date.today() <= date(2026, 8, 31) else (3.00, 15.00),
    'claude-opus-5': (5.00, 25.00),

    # --- Groq (قیمت پایه؛ اگر داخل سقف رایگان باشی عملاً چیزی کم نمی‌شود) ---
    'qwen/qwen3.6-27b': (0.60, 3.00),

    # --- OpenAI ---
    'gpt-5-mini': (0.25, 2.00),

    # --- Google ---
    'gemini-2.5-flash': (0.30, 2.50),
    'gemini-3-flash': (0.50, 3.00),
}


def compute_cost(model: str, input_tokens: int, output_tokens: int,
                  cache_write_tokens: int = 0, cache_read_tokens: int = 0):
    """
    خروجی: عدد هزینه به دلار (float) یا None اگر مدل در جدول قیمت نبود.

    محاسبه‌ی دقیق با احتساب Prompt Caching (طبق مستندات Anthropic):
      - توکن‌های عادی ورودی: نرخ کامل input
      - نوشتن در cache (cache_write): ۱.۲۵ برابر نرخ input (هزینه‌ی یک‌بار ذخیره‌سازی)
      - خواندن از cache (cache_read): ۱۰٪ نرخ input (تخفیف حدود ۹۰٪)
      - خروجی: نرخ کامل output
    """
    rates = PRICING.get(model)
    if not rates:
        return None
    in_rate, out_rate = rates
    cost = (
        (input_tokens / 1_000_000) * in_rate
        + (cache_write_tokens / 1_000_000) * in_rate * 1.25
        + (cache_read_tokens / 1_000_000) * in_rate * 0.10
        + (output_tokens / 1_000_000) * out_rate
    )
    return round(cost, 6)
