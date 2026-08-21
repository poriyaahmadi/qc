# Project Hezar — AI Video QC Tool

Internal tool for automatically reviewing product videos against the
Project Hezar QC guideline, using AI. Everything runs **locally** on your
own machine — nothing is hosted anywhere.

---

## 1) One-time setup

### Install ffmpeg (required)
- **macOS:** `brew install ffmpeg`
- **Windows:** download from [ffmpeg.org](https://ffmpeg.org/download.html), unzip, and add its `bin` folder to your PATH.
- **Linux:** `sudo apt install ffmpeg`

### Install megatools (only if you'll use Mega links)
- **macOS:** `brew install megatools`
- **Linux:** `sudo apt install megatools`
- **Windows:** download from [megatools.megous.com](https://megatools.megous.com/)

### Python
Python 3.10+ is required. Get it from [python.org](https://www.python.org/downloads/) if you don't have it.

That's it — you do **not** need to manually `pip install` anything; the
run script below sets up an isolated environment and installs everything
for you automatically.

---

## 2) Four options for the AI analysis engine

### Recommended for tight budgets — GPT-5 Mini (OpenAI)
Cheapest option that still handles vision + Persian text well. Fits comfortably
within a ~12,000 Toman/video budget (at $1 ≈ 200,000 Toman).
1. Go to [platform.openai.com](https://platform.openai.com) → sign up → add a small amount of billing credit.
2. **API Keys** → **Create new secret key** → copy it.
3. In `config.json`: set `"qc_provider": "openai"` and paste your key into `"openai_api_key"`.

### Backup budget option — Gemini 2.5 Flash (Google)
Very close in price to GPT-5 Mini, worth having configured as a fallback/comparison.
1. Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey) → sign in with a Google account → create a key (free to create; usage is billed once you attach a Cloud billing account).
2. In `config.json`: set `"qc_provider": "gemini"` and paste your key into `"gemini_api_key"`.

### Free option — Groq
1. Go to [console.groq.com](https://console.groq.com) → sign up (no credit card).
2. **API Keys** → **Create Key** → copy it.
3. In `config.json`: set `"qc_provider": "groq"` and paste your key into `"groq_api_key"`.

⚠️ Free, but ends up more expensive per video than GPT-5 Mini/Gemini if you were
paying — Groq's vision model only accepts 5 images/request, forcing many small
batched calls that each repeat the full guideline prompt.

### Most accurate — Claude (Anthropic)
1. Go to [console.anthropic.com](https://console.anthropic.com) → sign up.
2. **API Keys** → **Create Key** → copy it (looks like `sk-ant-...`).
3. In `config.json`: set `"qc_provider": "claude"` and paste your key into `"anthropic_api_key"`.

⚠️ Noticeably more expensive per video than GPT-5 Mini/Gemini — use this if
accuracy on subtle guideline judgment calls matters more than cost.

You only need to fill in the key for whichever provider you set as `qc_provider`.

---

## 3) Run it

### macOS / Linux
Double-click `run.sh`, or in Terminal:
```
./run.sh
```

### Windows
Double-click `run.bat`.

**The first time you run it**, it will create a `config.json` file for you
and stop, telling you to fill in your API key. Open `config.json` in any
text editor:

```json
{
  "qc_provider": "openai",
  "openai_api_key": "PASTE_YOUR_OPENAI_KEY_HERE",
  "openai_model": "gpt-5-mini",
  "gemini_api_key": "",
  "gemini_model": "gemini-2.5-flash",
  "groq_api_key": "",
  "anthropic_api_key": "",
  "anthropic_model": "claude-haiku-4-5-20251001",
  "frame_interval_seconds": 3
}
```

- Paste your key into whichever `*_api_key` field matches your `qc_provider`.
- `frame_interval_seconds` controls how often a frame is sampled from the
  video (default: every 3 seconds — a 5-minute video → ~100 frames).

Save the file, then run `run.sh` / `run.bat` again. It will set up
everything automatically and start the server. Leave that window open
while you use the tool.

Then open your browser to:
```
http://127.0.0.1:8765
```

---

## 4) How to use it

The whole tool works off **one text box**. Paste one line per video, in
the format:
```
DKP_CODE, VIDEO_LINK
```
For example:
```
12345678, https://drive.google.com/file/d/xxxxx/view
87654321, https://mega.nz/file/yyyyy
55555555, https://drive.google.com/file/d/zzzzz/view
```
- **One line = one video.** Works the same whether you paste 1 line or 20.
- As you type, the tool automatically looks up each DKP and shows you the
  product thumbnail/title so you can sanity-check the codes before running.
- Click **Run QC** — every row processes in the background. You'll see a
  live status per row (Queued → Processing → Accept/Reject/Error).
- Click any finished row to expand the full breakdown: Product match,
  Visual quality, Subtitles, Content rules — plus rejection reasons and
  required fixes if it was rejected. The exact cost of that one run (in USD,
  computed from the real token usage the API reported — not an estimate)
  is shown in the metrics row.
- Click **📊 History & budget** at the bottom to see every DKP you've ever
  run through the tool, with date, decision, and cost — plus a running
  total spend across everything. This is stored locally in `history.jsonl`
  and grows with every run (a few thousand entries is only a couple of MB).
- If a link turns out to be a **folder** with multiple videos in it, that
  row will show "Needs file pick" — re-run that one DKP by itself (as its
  own single line) so you can choose the right file.

---

## Notes / known limitations

- Mega/Drive links must be **public** ("Anyone with the link"); private
  links that require login won't download.
- Teaser vs. Description classification is based purely on video
  **duration** (~2 minutes = teaser cutoff).
- Frame sampling is a fixed interval (default every 3 seconds), not
  scene-aware — if you find it's missing something on very fast-changing
  videos, lower `frame_interval_seconds` in `config.json` (at the cost of
  more frames = more time/money per video).
- If a result comes back with `needs_human_review: true`, the AI itself
  wasn't confident — always double-check that one manually.
- Before presenting results to your manager, spot-check a handful of
  results (both Accept and Reject) manually to confirm the AI's accuracy
  on your real videos.
- Groq's free vision model only accepts 5 images per request, so the tool
  batches frames automatically for it; this means Groq runs involve more
  API calls (still free) and may be a bit less consistent than Claude on
  nuanced judgment calls (especially reading Persian subtitle text).

---

## File structure

```
run.sh / run.bat       one-command launcher (sets up venv, installs deps, starts server)
server.py              main server (all endpoints, incl. the batch job queue)
config.py              loads config.json (or falls back to env vars)
config.example.json    template — copied to config.json on first run
digikala_client.py     fetch product info from Digikala
downloader.py          download from Mega/Drive (single file or folder)
frame_extractor.py     fixed-interval frame extraction
qc_analyzer.py         sends frames to Claude and parses the verdict
qc_analyzer_groq.py    free alternative — sends frames to Groq (batched) and parses the verdict
qc_analyzer_openai.py  budget alternative — sends frames to GPT-5 Mini (batched) and parses the verdict
qc_analyzer_gemini.py  budget alternative — sends frames to Gemini 2.5 Flash (batched) and parses the verdict
guideline.py           the full QC ruleset, used as the AI system prompt
examples_loader.py     loads real past correction examples from eslahie_for_qc.xlsx (few-shot)
eslahie_for_qc.xlsx    real historical correction data — must stay next to server.py
pricing.py              model price table + exact cost calculation from real token usage
history_store.py        append-only local history of every DKP ever checked, with cost
qc.html                the web interface
requirements.txt       Python dependencies (installed automatically by run.sh/run.bat)
```
