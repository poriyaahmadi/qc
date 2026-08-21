# -*- coding: utf-8 -*-
"""
تحلیل QC با GPT-5 Mini (OpenAI). بر اساس تحقیق قیمت‌ها، این مدل ارزان‌ترین گزینه‌ای
بود که هم توانایی vision دارد هم داخل بودجه‌ی تعیین‌شده جا می‌شود.

نیاز به:
  - pip install requests
  - متغیر محیطی OPENAI_API_KEY یا کلید در config.json (فیلد openai_api_key)
"""

import os
import json
import base64
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
import pricing
from guideline import SYSTEM_PROMPT, build_user_content

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-5-mini"

# برای احتیاط (حجم درخواست و پایداری)، فریم‌ها را دسته‌دسته می‌فرستیم؛ سقف مستندشده‌ی
# رسمی OpenAI برای تعداد عکس هر درخواست را ندیدیم، پس محافظه‌کارانه عمل می‌کنیم.
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
    key = config.get('openai_api_key', 'OPENAI_API_KEY')
    if not key:
        raise QCAnalysisError(
            "کلید OpenAI تنظیم نشده. آن را در config.json (فیلد openai_api_key) وارد کن، "
            "یا متغیر محیطی OPENAI_API_KEY را ست کن."
        )
    return key


def _encode_image_data_url(path: str) -> str:
    with open(path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode('utf-8')
    return f"data:image/jpeg;base64,{b64}"


def _call_openai(api_key: str, model: str, system_prompt: str, content_blocks: list, max_tokens: int = 2000):
    """خروجی: (parsed_json, usage_dict)"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content_blocks}
        ],
        "max_completion_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        # مدل‌های GPT-5 بخشی از سقف توکن را صرف «تفکر پنهان» (reasoning) می‌کنند که هم باعث
        # قطع‌شدن JSON واقعی می‌شد هم کند بود؛ با minimal این مصرف پنهان به حداقل می‌رسد.
        "reasoning_effort": "minimal",
    }
    resp = requests.post(
        OPENAI_API_URL,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        data=json.dumps(payload),
        timeout=180
    )
    if resp.status_code != 200:
        raise QCAnalysisError(f"خطای OpenAI API ({resp.status_code}): {resp.text[:500]}")

    data = resp.json()
    usage_raw = data.get('usage', {}) or {}
    usage = {
        'input_tokens': usage_raw.get('prompt_tokens', 0),
        'output_tokens': usage_raw.get('completion_tokens', 0),
    }
    raw_text = data['choices'][0]['message']['content'].strip()
    cleaned = raw_text.replace('```json', '').replace('```', '').strip()
    try:
        return json.loads(cleaned), usage
    except json.JSONDecodeError:
        truncated_hint = ""
        if raw_text and not raw_text.rstrip().endswith('}'):
            truncated_hint = (" (به نظر می‌رسد پاسخ قطع شده — احتمالاً به max_tokens بیشتری "
                               "نیاز است، این را در qc_analyzer_openai.py افزایش بده)")
        raise QCAnalysisError(f"پاسخ OpenAI به‌صورت JSON معتبر نبود{truncated_hint}:\n{raw_text[:800]}")


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
        return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
    except Exception:
        return None


def analyze(product_info: dict, video_type: str, duration_sec: float, frames: list, progress_cb=None) -> dict:
    """همون امضا و همون خروجی qc_analyzer.analyze (نسخه‌ی Claude)، تا frontend بدون تغییر کار کند."""
    api_key = _get_api_key()
    model = config.get('openai_model', 'OPENAI_MODEL', DEFAULT_MODEL)

    base_context = build_user_content(product_info, video_type, len(frames), duration_sec)
    product_image_block = _get_product_image_block(product_info)

    batches = list(_chunk(frames, FRAMES_PER_BATCH))
    total_batches = len(batches)

    total_in = [0]
    total_out = [0]

    def _accumulate(usage):
        total_in[0] += usage.get('input_tokens', 0)
        total_out[0] += usage.get('output_tokens', 0)

    if total_batches <= 1:
        if progress_cb:
            progress_cb(f"Sending {len(frames)} frames to GPT-5 Mini…")
        content = []
        if product_image_block:
            content.append({"type": "text", "text": "تصویر اصلی محصول از سایت دیجی‌کالا:"})
            content.append(product_image_block)
        content.append({"type": "text", "text": base_context})
        content.append({"type": "text", "text": "فریم‌های ویدیو به ترتیب زمانی:"})
        for fr in frames:
            content.append({"type": "text", "text": f"فریم در ثانیه {fr['timestamp']:.1f}:"})
            content.append({"type": "image_url", "image_url": {"url": _encode_image_data_url(fr['path'])}})

        if progress_cb:
            progress_cb("Parsing verdict…")
        result, usage = _call_openai(api_key, model, SYSTEM_PROMPT, content, max_tokens=10000)
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
                content.append({"type": "image_url", "image_url": {"url": _encode_image_data_url(fr['path'])}})
            return _call_openai(api_key, model, SYSTEM_PROMPT, content, max_tokens=4000)

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
        result, usage = _call_openai(api_key, model, SYSTEM_PROMPT,
                                      [{"type": "text", "text": synthesis_text}], max_tokens=8000)
        _accumulate(usage)

    result['_model_used'] = model
    result['_frame_count'] = len(frames)
    result['_input_tokens'] = total_in[0]
    result['_output_tokens'] = total_out[0]
    result['_cost_usd'] = pricing.compute_cost(model, total_in[0], total_out[0])
    return result
