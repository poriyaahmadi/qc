# -*- coding: utf-8 -*-
"""
ارسال فریم‌های ویدیو + اطلاعات محصول به Claude API و گرفتن تحلیل نهایی QC.

نکته‌ی مهم: طبق مستندات رسمی Anthropic، هر درخواست حداکثر ۱۰۰ عکس می‌پذیرد. برای
ویدیوهای «توضیحات» (تا ۵ دقیقه با فاصله‌ی پیش‌فرض ۳ ثانیه) این عدد ممکن است به ~۱۰۰
فریم برسد، پس اینجا هم مثل نسخه‌ی Groq از batching (با batch های بزرگ‌تر، چون سقف
Claude خیلی بالاتر از Groq است) + یک مرحله‌ی جمع‌بندی نهایی استفاده می‌شود.

برای کاهش هزینه‌ی تکرار system prompt در چند batch، از Prompt Caching استفاده می‌شود
(system prompt طولانی‌مان تقریباً ثابت است، پس cache می‌خورد).

نیاز به:
  - pip install requests
  - متغیر محیطی ANTHROPIC_API_KEY  (از console.anthropic.com بساز)
  - اختیاری: متغیر محیطی ANTHROPIC_MODEL برای تعیین مدل (پیش‌فرض ارزان‌تر برای تست)
"""

import os
import json
import base64
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
import pricing
from guideline import SYSTEM_PROMPT, build_user_content

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

# پیش‌فرض: مدل ارزان‌تر برای تست اولیه. وقتی مطمئن شدی دقت کافیه،
# با متغیر محیطی ANTHROPIC_MODEL=claude-sonnet-5 عوضش کن.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# سقف واقعی API برابر ۱۰۰ عکس در هر درخواست است؛ برای اطمینان کمی پایین‌تر می‌گیریم.
FRAMES_PER_BATCH = 80
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


def _encode_image(path: str) -> str:
    with open(path, 'rb') as f:
        return base64.b64encode(f.read()).decode('utf-8')


def _image_block(base64_data: str, media_type: str = 'image/jpeg') -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": base64_data}
    }


def _call_claude(api_key: str, model: str, content: list, max_tokens: int = 2000):
    """خروجی: (parsed_json, usage_dict)"""
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        # cache_control روی system prompt: چون این متن بین همه‌ی batch ها و حتی بین
        # ویدیوهای مختلف یکسان است، بعد از اولین بار با ۹۰٪ تخفیف محاسبه می‌شود.
        "system": [
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
        ],
        "messages": [{"role": "user", "content": content}]
    }
    resp = requests.post(
        ANTHROPIC_API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        },
        data=json.dumps(payload),
        timeout=180
    )
    if resp.status_code != 200:
        raise QCAnalysisError(f"خطای Claude API ({resp.status_code}): {resp.text[:500]}")

    data = resp.json()
    usage_raw = data.get('usage', {}) or {}
    usage = {
        'input_tokens': usage_raw.get('input_tokens', 0),
        'output_tokens': usage_raw.get('output_tokens', 0),
        'cache_creation_input_tokens': usage_raw.get('cache_creation_input_tokens', 0),
        'cache_read_input_tokens': usage_raw.get('cache_read_input_tokens', 0),
    }
    text_parts = [b['text'] for b in data.get('content', []) if b.get('type') == 'text']
    raw_text = "\n".join(text_parts).strip()
    cleaned = raw_text.replace('```json', '').replace('```', '').strip()
    try:
        return json.loads(cleaned), usage
    except json.JSONDecodeError:
        truncated_hint = ""
        if raw_text and not raw_text.rstrip().endswith('}'):
            truncated_hint = (" (به نظر می‌رسد پاسخ قطع شده — احتمالاً به max_tokens بیشتری "
                               "نیاز است، این را در qc_analyzer.py افزایش بده)")
        raise QCAnalysisError(f"پاسخ مدل به‌صورت JSON معتبر نبود{truncated_hint}. پاسخ خام:\n{raw_text[:1000]}")


def _chunk(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def _get_product_image_block(product_info: dict):
    url = product_info.get('image')
    if not url:
        return None
    try:
        img_resp = requests.get(url, timeout=15)
        img_resp.raise_for_status()
        b64 = base64.b64encode(img_resp.content).decode('utf-8')
        return _image_block(b64)
    except Exception:
        return None


def analyze(product_info: dict, video_type: str, duration_sec: float, frames: list, progress_cb=None) -> dict:
    """
    frames: [{'timestamp': float, 'path': str}, ...]
    خروجی: دیکشنری پارس‌شده طبق اسکیمای guideline.SYSTEM_PROMPT + فیلدهای هزینه/توکن
    """
    api_key = config.get('anthropic_api_key', 'ANTHROPIC_API_KEY')
    if not api_key:
        raise QCAnalysisError(
            "کلید Anthropic تنظیم نشده. آن را در config.json (فیلد anthropic_api_key) وارد کن، "
            "یا متغیر محیطی ANTHROPIC_API_KEY را ست کن."
        )
    model = config.get('anthropic_model', 'ANTHROPIC_MODEL', DEFAULT_MODEL)

    base_context = build_user_content(product_info, video_type, len(frames), duration_sec)
    product_image_block = _get_product_image_block(product_info)

    batches = list(_chunk(frames, FRAMES_PER_BATCH))
    total_batches = len(batches)

    total_in = [0]
    total_out = [0]
    total_cache_write = [0]
    total_cache_read = [0]

    def _accumulate(usage):
        total_in[0] += usage.get('input_tokens', 0)
        total_out[0] += usage.get('output_tokens', 0)
        total_cache_write[0] += usage.get('cache_creation_input_tokens', 0)
        total_cache_read[0] += usage.get('cache_read_input_tokens', 0)

    # ------------------------------------------------------- fast path ----
    # اکثر ویدیوها (تیزرها و اکثر توضیحات با تنظیمات پیش‌فرض) در یک batch جا می‌شوند؛
    # یعنی نیازی به مرحله‌ی جداگانه‌ی جمع‌بندی نیست و یک تماس کافی است.
    if total_batches <= 1:
        if progress_cb:
            progress_cb(f"Sending {len(frames)} frames to Claude…")
        content = []
        if product_image_block:
            content.append({"type": "text", "text": "تصویر اصلی محصول از سایت دیجی‌کالا:"})
            content.append(product_image_block)
        content.append({"type": "text", "text": base_context})
        content.append({"type": "text", "text": "فریم‌های ویدیو به ترتیب زمانی:"})
        for fr in frames:
            content.append({"type": "text", "text": f"فریم در ثانیه {fr['timestamp']:.1f}:"})
            content.append(_image_block(_encode_image(fr['path'])))

        if progress_cb:
            progress_cb("Parsing verdict…")
        result, usage = _call_claude(api_key, model, content, max_tokens=10000)
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
            content = []
            if product_image_block:
                content.append({"type": "text", "text": "تصویر اصلی محصول از سایت دیجی‌کالا:"})
                content.append(product_image_block)
            content.append({"type": "text", "text": base_context + BATCH_SCHEMA_HINT +
                             f"\n(این batch شماره {batch_idx+1} از {total_batches} است)"})
            for fr in batch:
                content.append({"type": "text", "text": f"فریم در ثانیه {fr['timestamp']:.1f}:"})
                content.append(_image_block(_encode_image(fr['path'])))
            return _call_claude(api_key, model, content, max_tokens=4000)

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
        result, usage = _call_claude(api_key, model, [{"type": "text", "text": synthesis_text}], max_tokens=8000)
        _accumulate(usage)

    result['_model_used'] = model
    result['_frame_count'] = len(frames)
    result['_input_tokens'] = total_in[0]
    result['_output_tokens'] = total_out[0]
    result['_cache_write_tokens'] = total_cache_write[0]
    result['_cache_read_tokens'] = total_cache_read[0]
    result['_cost_usd'] = pricing.compute_cost(
        model, total_in[0], total_out[0], total_cache_write[0], total_cache_read[0]
    )
    return result
