from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from http.server import HTTPServer
import subprocess
import json
import os
import tempfile
import shutil
import threading
import uuid
import time
import traceback

import config
import digikala_client
import downloader
import frame_extractor
import history_store

QC_PROVIDER = config.get('qc_provider', 'QC_PROVIDER', 'claude').strip().lower()
if QC_PROVIDER == 'groq':
    import qc_analyzer_groq as qc_analyzer
    print("🟢 Analysis engine: Groq (free)")
elif QC_PROVIDER == 'openai':
    import qc_analyzer_openai as qc_analyzer
    print("🟡 Analysis engine: OpenAI (GPT-5 Mini)")
elif QC_PROVIDER == 'gemini':
    import qc_analyzer_gemini as qc_analyzer
    print("🟣 Analysis engine: Google (Gemini 2.5 Flash)")
else:
    import qc_analyzer
    print("🔵 Analysis engine: Claude (Anthropic)")

PORT = 8765

WORK_ROOT = os.path.join(tempfile.gettempdir(), 'dkp-qc-jobs')
os.makedirs(WORK_ROOT, exist_ok=True)

# --------------------------------------------------------------- job store -
# پردازش دسته‌ای (batch) در یک ترد جداگانه اجرا می‌شود و وضعیتش این‌جا نگه داشته می‌شود.
JOBS = {}
JOBS_LOCK = threading.Lock()


def _run_single_item(dkp: str, video_link: str, job_dir: str, progress_cb=None) -> dict:
    """دانلود + استخراج فریم + تحلیل برای یک آیتم. اگر لینک فولدر با چند فایل بود، استثنا می‌زند."""
    t0 = time.time()
    timing = {}

    def _mark(stage):
        timing[stage] = round(time.time() - t0, 1)

    def _cb(msg):
        if progress_cb:
            progress_cb(f"{msg}  ({round(time.time() - t0, 1)}s)")

    _cb("Fetching product info from Digikala…")
    product_info = digikala_client.get_product_info(dkp)
    _mark('product_info_sec')

    _cb("Downloading video…")
    dl_result = downloader.download_video(video_link, os.path.join(job_dir, 'download'))
    _mark('download_sec')
    if dl_result['status'] == 'needs_selection':
        return {
            'status': 'needs_selection',
            'link_type': dl_result['link_type'],
            'files': dl_result['files'],
            'product_info': product_info,
        }

    video_path = dl_result['path']
    video_type, duration_sec, frames = frame_extractor.extract_frames(
        video_path, os.path.join(job_dir, 'frames'), progress_cb=_cb
    )
    _mark('frames_sec')
    result = qc_analyzer.analyze(product_info, video_type, duration_sec, frames, progress_cb=_cb)
    _mark('analysis_sec')

    timing['total_sec'] = round(time.time() - t0, 1)
    print(f"  ⏱  DKP-{dkp} timing: product_info={timing['product_info_sec']}s, "
          f"download={round(timing['download_sec']-timing['product_info_sec'],1)}s, "
          f"frames={round(timing['frames_sec']-timing['download_sec'],1)}s, "
          f"analysis={round(timing['analysis_sec']-timing['frames_sec'],1)}s, "
          f"TOTAL={timing['total_sec']}s")

    result['product_info'] = product_info
    result['video_type'] = video_type
    result['duration_sec'] = duration_sec
    result['status'] = 'done'
    result['_timing'] = timing
    return result


def _run_batch_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS[job_id]
    for idx, item in enumerate(job['items']):
        dkp = item['dkp']
        link = item['video_link']

        def _progress(msg, _item=item):
            _item['stage'] = msg

        try:
            item['status'] = 'processing'
            job_dir = os.path.join(WORK_ROOT, f"batch_{job_id}_{idx}")
            if os.path.exists(job_dir):
                shutil.rmtree(job_dir)
            os.makedirs(job_dir, exist_ok=True)
            outcome = _run_single_item(dkp, link, job_dir, progress_cb=_progress)
            if outcome.get('status') == 'needs_selection':
                item['status'] = 'needs_selection'
                item['link_type'] = outcome['link_type']
                item['files'] = outcome['files']
                item['note'] = 'This link is a folder with multiple videos — run it individually to pick the right file.'
            else:
                item['status'] = 'done'
                item['result'] = outcome
                p = outcome.get('product_info', {})
                history_store.append_record({
                    'dkp': dkp,
                    'title': p.get('title_fa') or p.get('title_en') or '',
                    'brand': p.get('brand', ''),
                    'video_link': link,
                    'status': 'done',
                    'final_decision': outcome.get('final_decision'),
                    'model_used': outcome.get('_model_used'),
                    'input_tokens': outcome.get('_input_tokens'),
                    'output_tokens': outcome.get('_output_tokens'),
                    'cost_usd': outcome.get('_cost_usd'),
                    'frame_count': outcome.get('_frame_count'),
                    'duration_sec': outcome.get('duration_sec'),
                    'timing': outcome.get('_timing'),
                })
            item['stage'] = ''
        except Exception as e:
            item['status'] = 'error'
            item['error'] = str(e)
            item['stage'] = ''
            history_store.append_record({
                'dkp': dkp,
                'video_link': link,
                'status': 'error',
                'error': str(e),
            })
    with JOBS_LOCK:
        job['status'] = 'done'
        job['finished_at'] = time.time()


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class ProxyHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"[DKP-QC] {args[0]} {args[1]} {args[2]}")

    # ------------------------------------------------------------ CORS ----

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()

    def send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')

    # ------------------------------------------------------------- GET ----

    def do_GET(self):
        if self.path.startswith('/product/'):
            dkp = self.path.replace('/product/', '').strip('/')
            self.fetch_digikala(dkp)
        elif self.path.startswith('/youtube'):
            self.proxy_youtube()
        elif self.path.startswith('/qc/batch-status/'):
            job_id = self.path.split('/qc/batch-status/', 1)[1].strip('/')
            self.qc_batch_status(job_id)
        elif self.path == '/history':
            self.get_history()
        elif self.path in ('/', '/index.html', '/qc', '/qc.html'):
            self.serve_html('qc.html')
        elif self.path == '/dkp-link.html':
            self.serve_html('dkp-link.html')
        else:
            self.send_response(404)
            self.send_cors_headers()
            self.end_headers()

    # ------------------------------------------------------------- POST ---

    def do_POST(self):
        try:
            if self.path == '/groq':
                self.proxy_groq()
            elif self.path == '/qc/start':
                self.qc_start()
            elif self.path == '/qc/select-file':
                self.qc_select_file()
            elif self.path == '/qc/batch-start':
                self.qc_batch_start()
            else:
                self.send_response(404)
                self.send_cors_headers()
                self.end_headers()
        except Exception as e:
            traceback.print_exc()
            self.send_json({'error': str(e)}, 500)

    def _read_json_body(self):
        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length).decode('utf-8')
        return json.loads(raw) if raw else {}

    # -------------------------------------------------------- digikala ----

    def fetch_digikala(self, dkp):
        try:
            info = digikala_client.get_product_info(dkp)
            print(f"  ✓ DKP-{dkp}: {info['title_en'] or info['title_fa']} [{info['category']}]")
            self.send_json(info)
        except digikala_client.ProductNotFoundError as e:
            self.send_json({'error': str(e)}, 404)
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def proxy_youtube(self):
        query = self.path.replace('/youtube', '')
        yt_url = f"https://www.googleapis.com/youtube/v3{query}"
        result = subprocess.run([
            'curl', '-s',
            '-A', 'Mozilla/5.0',
            '-H', 'Accept: application/json',
            yt_url
        ], capture_output=True, text=True, timeout=15)
        self.send_raw(result.stdout)

    def proxy_groq(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode('utf-8')
        auth = self.headers.get('Authorization', '')
        result = subprocess.run([
            'curl', '-s', '-X', 'POST',
            'https://api.groq.com/openai/v1/chat/completions',
            '-H', 'Content-Type: application/json',
            '-H', f'Authorization: {auth}',
            '-d', body
        ], capture_output=True, text=True, timeout=30)
        self.send_raw(result.stdout)

    # ---------------------------------------------------------- QC flow ---

    def qc_start(self):
        body = self._read_json_body()
        dkp = str(body.get('dkp', '')).strip()
        video_link = str(body.get('video_link', '')).strip()
        if not dkp or not video_link:
            self.send_json({'error': 'DKP code and video link are both required'}, 400)
            return

        job_dir = os.path.join(WORK_ROOT, f"job_{dkp}_{abs(hash(video_link)) % 100000}")
        if os.path.exists(job_dir):
            shutil.rmtree(job_dir)
        os.makedirs(job_dir, exist_ok=True)

        print(f"  ▶ Starting QC for DKP-{dkp} ...")
        try:
            outcome = _run_single_item(dkp, video_link, job_dir)
        except Exception as e:
            self.send_json({'error': str(e)}, 500)
            return

        if outcome.get('status') == 'needs_selection':
            self.send_json({
                'status': 'needs_selection',
                'link_type': outcome['link_type'],
                'files': outcome['files'],
                'dkp': dkp,
            })
            return

        print(f"  ✅ Result: {outcome.get('final_decision')}")
        self.send_json(outcome)

    def qc_select_file(self):
        body = self._read_json_body()
        dkp = str(body.get('dkp', '')).strip()
        link_type = body.get('link_type')
        file_ref = body.get('file_ref')
        if not (dkp and link_type and file_ref):
            self.send_json({'error': 'Incomplete file-selection info'}, 400)
            return

        job_dir = os.path.join(WORK_ROOT, f"job_{dkp}_sel_{abs(hash(file_ref)) % 100000}")
        os.makedirs(job_dir, exist_ok=True)

        try:
            product_info = digikala_client.get_product_info(dkp)
            video_path = downloader.download_selected_file(link_type, file_ref, os.path.join(job_dir, 'download'))
            video_type, duration_sec, frames = frame_extractor.extract_frames(
                video_path, os.path.join(job_dir, 'frames')
            )
            result = qc_analyzer.analyze(product_info, video_type, duration_sec, frames)
            result['product_info'] = product_info
            result['video_type'] = video_type
            result['duration_sec'] = duration_sec
        except Exception as e:
            self.send_json({'error': str(e)}, 500)
            return

        self.send_json(result)

    # ------------------------------------------------------------ batch ---

    def qc_batch_start(self):
        body = self._read_json_body()
        items_in = body.get('items', [])
        items_in = [i for i in items_in if str(i.get('dkp', '')).strip() and str(i.get('video_link', '')).strip()]
        if not items_in:
            self.send_json({'error': 'No valid DKP/link rows provided'}, 400)
            return

        job_id = uuid.uuid4().hex[:12]
        items = [
            {
                'dkp': str(i.get('dkp', '')).strip(),
                'video_link': str(i.get('video_link', '')).strip(),
                'status': 'pending',
            }
            for i in items_in
        ]
        with JOBS_LOCK:
            JOBS[job_id] = {'items': items, 'status': 'running', 'created_at': time.time()}

        t = threading.Thread(target=_run_batch_job, args=(job_id,), daemon=True)
        t.start()
        print(f"  ▶ Batch job {job_id} started with {len(items)} item(s)")
        self.send_json({'job_id': job_id, 'total': len(items)})

    def qc_batch_status(self, job_id):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if not job:
                self.send_json({'error': 'Job not found'}, 404)
                return
            # کپی سبک برای جلوگیری از تغییر همزمان حین serialize
            snapshot = {
                'status': job['status'],
                'items': list(job['items']),
            }
        self.send_json(snapshot)

    def get_history(self):
        records = history_store.get_all(limit=500)
        summary = history_store.get_summary()
        self.send_json({'summary': summary, 'records': records})

    # --------------------------------------------------------- helpers ----

    def serve_html(self, filename):
        html_path = os.path.join(os.path.dirname(__file__), filename)
        try:
            with open(html_path, 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_json({'error': f'{filename} not found'}, 404)

    def send_raw(self, text):
        body = text.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', PORT), ProxyHandler)
    print(f"✅ DKP-QC Server running on http://127.0.0.1:{PORT}")
    print(f"📂 qc.html must be next to this file")
    print(f"🌐 Browser: http://127.0.0.1:{PORT}")
    print(f"⛔ Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n⛔ Server stopped.")
