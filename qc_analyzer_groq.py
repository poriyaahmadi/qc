# -*- coding: utf-8 -*-
"""
نسخه‌ی رایگان تحلیل QC با Groq (به‌جای Claude).
مدل vision فعلی Groq (qwen/qwen3.6-27b) حداکثر ۵ عکس در هر درخواست قبول می‌کند،
پس فریم‌ها را دسته‌دسته (batch) می‌فرستیم — به‌صورت موازی برای سرعت بیشتر — و در
پایان یک مرحله‌ی «جمع‌بندی نهایی» (فقط متنی، بدون عکس) نتیجه‌ی Accept/Reject را
طبق کل گایدلاین مشخص می‌کند.

نیاز به:
  - pip install requests
  - متغیر محیطی GROQ_API_KEY  (رایگان از console.groq.com)
"""

import json
import base64
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
import pricing
from guideline import SYSTEM_PROMPT, build_user_content

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
VISION_MODEL = "qwen/qwen3.6-27b"

# هر batch حداکثر ۴ فریم + ۱ عکس محصول = ۵ عکس (سقف Groq)
FRAMES_PER_BATCH = 4
# چند batch هم‌زمان بفرستیم. عدد بالاتر = سریع‌تر ولی احتمال برخورد به rate limit رایگان بیشتر.
PARALLEL_BATCHES = 4
MAX_RETRIES = 3

BATCH_SCHEMA_HINT = """
فقط این بخش از فریم‌ها را که الان می‌بینی بررسی کن (نه کل ویدیو، چون بقیه در batch های دیگر
بررسی می‌شوند). فقط JSON زیر را برگردان، بدون هیچ متن اضافه:
{
  "product_match_issues": ["..."],
  "visual_issues": ["..."],
  "subtitle_issues": [{"timestamp_sec": 0.0, "problem": "...", "correction": "..."}],
  "content_issues": ["..."]
}
اگر مشکلی در این بخش نبود، آرایه‌ی مربوطه را خالی بگذار. فقط مشکلات واقعی و مشخص را گزارش کن.
"""

FINAL_SYNTHESIS_HINT = """
حالا تمام یافته‌های جزئی از تمام بخش‌های ویدیو (که در بالا جمع شده) را داری. طبق تمام قوانین
گایدلاین (در system prompt)، تصمیم نهایی را بگیر. فقط JSON زیر را برگردان، بدون هیچ متن اضافه:
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
    key = config.get('groq_api_key', 'GROQ_API_KEY')
    if not key:
        raise QCAnalysisError(
            "کلید Groq تنظیم نشده. آن را در config.json (فیلد groq_api_key) وارد کن، "
            "یا متغیر محیطی GROQ_API_KEY را ست کن."
        )
    return key


def _encode_image_data_url(path: str) -> str:
    with open(path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode('utf-8')
    return f"data:image/jpeg;base64,{b64}"


def _call_groq(system_prompt: str, content_blocks: list, api_key: str):
    """خروجی: (parsed_json, usage_dict) که usage_dict شامل input_tokens/output_tokens است."""
    payload = {
        "model": VISION_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content_blocks}
        ],
        "temperature": 0.2,
        "max_completion_tokens": 2000,
        "response_format": {"type": "json_object"},
    }

    last_err = None
    for attempt in range(MAX_RETRIES):
        resp = requests.post(
            GROQ_API_URL,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            data=json.dumps(payload),
            timeout=120
        )
        if resp.status_code == 429 or resp.status_code >= 500:
            # rate limit یا خطای موقت سرور -> کمی صبر کن و دوباره امتحان کن
            last_err = f"خطای Groq API ({resp.status_code}): {resp.text[:300]}"
            time.sleep(1.5 * (attempt + 1))
            continue
        if resp.status_code != 200:
            raise QCAnalysisError(f"خطای Groq API ({resp.status_code}): {resp.text[:500]}")

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
            raise QCAnalysisError(f"پاسخ Groq به‌صورت JSON معتبر نبود:\n{raw_text[:800]}")

    raise QCAnalysisError(last_err or "خطای نامشخص در تماس با Groq")


def _chunk(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def analyze(product_info: dict, video_type: str, duration_sec: float, frames: list, progress_cb=None) -> dict:
    """
    همون امضا و همون خروجی qc_analyzer.analyze (نسخه‌ی Claude)، تا frontend بدون تغییر کار کنه.
    progress_cb (اختیاری): تابعی که با یک رشته‌ی متنی برای نمایش پیشرفت صدا زده می‌شود.
    """
    api_key = _get_api_key()

    product_image_data_url = None
    product_image_url = product_info.get('image')
    if product_image_url:
        try:
            img_resp = requests.get(product_image_url, timeout=15)
            img_resp.raise_for_status()
            b64 = base64.b64encode(img_resp.content).decode('utf-8')
            product_image_data_url = f"data:image/jpeg;base64,{b64}"
        except Exception:
            product_image_data_url = None

    base_context = build_user_content(product_info, video_type, len(frames), duration_sec)

    def _build_batch_blocks(batch_idx, batch, total_batches):
        blocks = [{"type": "text", "text": base_context + BATCH_SCHEMA_HINT +
                   f"\n(این batch شماره {batch_idx+1} از {total_batches} است)"}]
        if product_image_data_url:
            blocks.append({"type": "image_url", "image_url": {"url": product_image_data_url}})
        for fr in batch:
            blocks.append({"type": "text", "text": f"فریم در ثانیه {fr['timestamp']:.1f}:"})
            blocks.append({"type": "image_url", "image_url": {"url": _encode_image_data_url(fr['path'])}})
        return blocks

    batches = list(_chunk(frames, FRAMES_PER_BATCH))
    total_batches = len(batches)

    merged = {
        'product_match_issues': [],
        'visual_issues': [],
        'subtitle_issues': [],
        'content_issues': [],
    }

    if progress_cb:
        progress_cb(f"Analyzing {total_batches} batch(es) of frames…")

    completed = [0]
    total_input_tokens = [0]
    total_output_tokens = [0]

    def _job(batch_idx, batch):
        blocks = _build_batch_blocks(batch_idx, batch, total_batches)
        try:
            return _call_groq(SYSTEM_PROMPT, blocks, api_key)
        except QCAnalysisError:
            # اگر یک batch خطا داد، رد می‌شویم ولی بقیه را ادامه می‌دهیم
            return None, {'input_tokens': 0, 'output_tokens': 0}

    with ThreadPoolExecutor(max_workers=PARALLEL_BATCHES) as pool:
        futures = {pool.submit(_job, i, b): i for i, b in enumerate(batches)}
        for fut in as_completed(futures):
            partial, usage = fut.result()
            total_input_tokens[0] += usage.get('input_tokens', 0)
            total_output_tokens[0] += usage.get('output_tokens', 0)
            completed[0] += 1
            if progress_cb:
                progress_cb(f"Analyzing batch {completed[0]}/{total_batches}…")
            if not partial:
                continue
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
    final, synth_usage = _call_groq(SYSTEM_PROMPT, [{"type": "text", "text": synthesis_text}], api_key)
    total_input_tokens[0] += synth_usage.get('input_tokens', 0)
    total_output_tokens[0] += synth_usage.get('output_tokens', 0)

    final['_model_used'] = f"{VISION_MODEL} (Groq)"
    final['_frame_count'] = len(frames)
    final['_input_tokens'] = total_input_tokens[0]
    final['_output_tokens'] = total_output_tokens[0]
    final['_cost_usd'] = pricing.compute_cost(VISION_MODEL, total_input_tokens[0], total_output_tokens[0])
    return final
