# -*- coding: utf-8 -*-
"""
تحلیل QC با Gemini 2.5 Flash (Google). گزینه‌ی دوم داخل بودجه (بعد از GPT-5 Mini)،
با این تفاوت که فرمت REST API گوگل با OpenAI/Anthropic فرق دارد (system instruction
جدا، بخش‌های عکس با inline_data).

نیاز به:
  - pip install requests
  - متغیر محیطی GEMINI_API_KEY یا کلید در config.json (فیلد gemini_api_key)
    (از https://aistudio.google.com/apikey رایگان قابل ساخت است)
"""

import json
import base64
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
import pricing
from guideline import SYSTEM_PROMPT, build_user_content

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-2.5-flash"

FRAMES_PER_BATCH = 50
PARALLEL_BATCHES = 3

BATCH_SCHEMA_HINT = """
فقط این بخش از فریم‌ها را که الان می‌بینی بررسی کن (نه کل ویدیو، چون بقیه در batch های دیگر
بررسی می‌شوند). فقط JSON زیر را برگردان، بدون هیچ متن اضافه:
{
  "product_match_issues": ["..."],
  "visual_issues": ["..."],
  "subtitle_issues": [{"timestamp_sec": 0.0, "problem": "...", "correction": "..."}],
  "content_issues": ["..."]
}
اگر مشکلی در این بخش نبود، آرایه‌ی مربوطه را خالی بگذار.
"""

FINAL_SYNTHESIS_HINT = """
حالا تمام یافته‌های جزئی از تمام بخش‌های ویدیو (که در بالا جمع شده) را داری. طبق تمام قوانین
گایدلاین، تصمیم نهایی را بگیر. فقط JSON زیر را برگردان، بدون هیچ متن اضافه:
{
  "product_match": {"matches": true/false, "confidence": "high"|"medium"|"low", "issues": [...]},
  "visual_quality": {"ok": true/false, "issues": [...]},
  "subtitle_check": {"ok": true/false, "issues": [...]},
  "content_rules": {"ok": true/false, "issues": [...]},
  "final_decision": "accept" | "reject",
  "rejection_reasons": ["..."],
  "required_corrections": ["..."],
  "estimated_fix_time_minutes": 0,
  "needs_human_review": false,
  "human_review_reason": ""
}
"""


class QCAnalysisError(Exception):
    pass


def _get_api_key():
    key = config.get('gemini_api_key', 'GEMINI_API_KEY')
    if not key:
        raise QCAnalysisError(
            "کلید Gemini تنظیم نشده. آن را در config.json (فیلد gemini_api_key) وارد کن، "
            "یا متغیر محیطی GEMINI_API_KEY را ست کن. (رایگان از aistudio.google.com/apikey)"
        )
    return key


def _image_part(path: str) -> dict:
    with open(path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode('utf-8')
    return {"inline_data": {"mime_type": "image/jpeg", "data": b64}}


def _image_part_from_bytes(raw: bytes) -> dict:
    return {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(raw).decode('utf-8')}}


def _call_gemini(api_key: str, model: str, parts: list, max_tokens: int = 2000):
    """خروجی: (parsed_json, usage_dict)"""
    url = f"{GEMINI_API_BASE}/{model}:generateContent?key={api_key}"
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "maxOutputTokens": max_tokens,
            "temperature": 0.2,
            # thinking پیش‌فرض بخشی از سقف توکن خروجی را برای «تفکر پنهان» مصرف می‌کند و
            # همین باعث قطع‌شدن JSON واقعی می‌شد؛ غیرفعالش می‌کنیم تا کل سقف صرف پاسخ واقعی شود
            # (هم دقیق‌تر می‌ماند، هم معمولاً سریع‌تر است).
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    resp = requests.post(url, headers={"Content-Type": "application/json"},
                          data=json.dumps(payload), timeout=180)
    if resp.status_code != 200:
        raise QCAnalysisError(f"خطای Gemini API ({resp.status_code}): {resp.text[:500]}")

    data = resp.json()
    usage_raw = data.get('usageMetadata', {}) or {}
    usage = {
        'input_tokens': usage_raw.get('promptTokenCount', 0),
        'output_tokens': usage_raw.get('candidatesTokenCount', 0),
    }

    candidates = data.get('candidates', [])
    if not candidates:
        raise QCAnalysisError(f"Gemini پاسخی برنگرداند (احتمالاً فیلتر ایمنی): {json.dumps(data)[:500]}")

    text_parts = [p.get('text', '') for p in candidates[0].get('content', {}).get('parts', [])]
    raw_text = "".join(text_parts).strip()
    cleaned = raw_text.replace('```json', '').replace('```', '').strip()
    try:
        return json.loads(cleaned), usage
    except json.JSONDecodeError:
        truncated_hint = ""
        if raw_text and not raw_text.rstrip().endswith('}'):
            truncated_hint = (" (به نظر می‌رسد پاسخ قطع شده — احتمالاً به max_tokens بیشتری "
                               "نیاز است، این را در qc_analyzer_gemini.py افزایش بده)")
        raise QCAnalysisError(f"پاسخ Gemini به‌صورت JSON معتبر نبود{truncated_hint}:\n{raw_text[:800]}")


def _chunk(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def _get_product_image_part(product_info: dict):
    url = product_info.get('image')
    if not url:
        return None
    try:
        img_resp = requests.get(url, timeout=15)
        img_resp.raise_for_status()
        return _image_part_from_bytes(img_resp.content)
    except Exception:
        return None


def analyze(product_info: dict, video_type: str, duration_sec: float, frames: list, progress_cb=None) -> dict:
    """همون امضا و همون خروجی qc_analyzer.analyze (نسخه‌ی Claude)، تا frontend بدون تغییر کار کند."""
    api_key = _get_api_key()
    model = config.get('gemini_model', 'GEMINI_MODEL', DEFAULT_MODEL)

    base_context = build_user_content(product_info, video_type, len(frames), duration_sec)
    product_image_part = _get_product_image_part(product_info)

    batches = list(_chunk(frames, FRAMES_PER_BATCH))
    total_batches = len(batches)

    total_in = [0]
    total_out = [0]

    def _accumulate(usage):
        total_in[0] += usage.get('input_tokens', 0)
        total_out[0] += usage.get('output_tokens', 0)

    if total_batches <= 1:
        if progress_cb:
            progress_cb(f"Sending {len(frames)} frames to Gemini…")
        parts = []
        if product_image_part:
            parts.append({"text": "تصویر اصلی محصول از سایت دیجی‌کالا:"})
            parts.append(product_image_part)
        parts.append({"text": base_context})
        parts.append({"text": "فریم‌های ویدیو به ترتیب زمانی:"})
        for fr in frames:
            parts.append({"text": f"فریم در ثانیه {fr['timestamp']:.1f}:"})
            parts.append(_image_part(fr['path']))

        if progress_cb:
            progress_cb("Parsing verdict…")
        result, usage = _call_gemini(api_key, model, parts, max_tokens=10000)
        _accumulate(usage)

    else:
        if progress_cb:
            progress_cb(f"Analyzing {total_batches} batch(es) of frames…")

        merged = {
            'product_match_issues': [], 'visual_issues': [],
            'subtitle_issues': [], 'content_issues': [],
        }
        completed = [0]

        def _job(batch_idx, batch):
            parts = []
            if product_image_part:
                parts.append({"text": "تصویر اصلی محصول از سایت دیجی‌کالا:"})
                parts.append(product_image_part)
            parts.append({"text": base_context + BATCH_SCHEMA_HINT +
                          f"\n(این batch شماره {batch_idx+1} از {total_batches} است)"})
            for fr in batch:
                parts.append({"text": f"فریم در ثانیه {fr['timestamp']:.1f}:"})
                parts.append(_image_part(fr['path']))
            return _call_gemini(api_key, model, parts, max_tokens=4000)

        with ThreadPoolExecutor(max_workers=PARALLEL_BATCHES) as pool:
            futures = {pool.submit(_job, i, b): i for i, b in enumerate(batches)}
            for fut in as_completed(futures):
                partial, usage = fut.result()
                _accumulate(usage)
                completed[0] += 1
                if progress_cb:
                    progress_cb(f"Analyzing batch {completed[0]}/{total_batches}…")
                merged['product_match_issues'].extend(partial.get('product_match_issues', []) or [])
                merged['visual_issues'].extend(partial.get('visual_issues', []) or [])
                merged['subtitle_issues'].extend(partial.get('subtitle_issues', []) or [])
                merged['content_issues'].extend(partial.get('content_issues', []) or [])

        if progress_cb:
            progress_cb("Finalizing verdict…")
        synthesis_text = base_context + f"""
یافته‌های جزئی جمع‌شده از تمام بخش‌های ویدیو:

مشکلات تطبیق محصول: {json.dumps(merged['product_match_issues'], ensure_ascii=False)}
مشکلات کیفیت بصری: {json.dumps(merged['visual_issues'], ensure_ascii=False)}
مشکلات زیرنویس: {json.dumps(merged['subtitle_issues'], ensure_ascii=False)}
مشکلات قوانین محتوایی: {json.dumps(merged['content_issues'], ensure_ascii=False)}

{FINAL_SYNTHESIS_HINT}
"""
        result, usage = _call_gemini(api_key, model, [{"text": synthesis_text}], max_tokens=8000)
        _accumulate(usage)

    result['_model_used'] = model
    result['_frame_count'] = len(frames)
    result['_input_tokens'] = total_in[0]
    result['_output_tokens'] = total_out[0]
    result['_cost_usd'] = pricing.compute_cost(model, total_in[0], total_out[0])
    return result
