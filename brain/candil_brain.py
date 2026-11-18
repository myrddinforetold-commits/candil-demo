"""Candil brain on Modal: the real lastbook engine (library search, cited answers) behind one /ask endpoint,
with speech-to-text on the way in and Candil's voice on the way out.

  modal run candil_brain.py::setup_library     # once: downloads and checks lastbook's Small library pack into a Volume
  modal deploy candil_brain.py                 # the web endpoint

The model runs on OpenRouter (the same open weights the device runs): 'quick' is Gemma 4 26B A4B, 'deep' is DeepSeek V4 Flash 0731.
Lastbook only talks to a model on its own computer, so a small relay listens on 127.0.0.1:19443 inside the container and
forwards to OpenRouter with the key from the Modal secret 'openrouter'.
"""
import modal

LASTBOOK_COMMIT = '9542e80fbdb6e6ee2a0472beabab0bbd706f0c15'
DATA = '/data'
LIB = DATA + '/LastbookData'
MODELS = {'quick': 'google/gemma-4-26b-a4b-it', 'deep': 'deepseek/deepseek-v4-flash-0731'}
MODEL_LABEL = {'quick': 'Quick · Gemma 4 26B', 'deep': 'Deep · DeepSeek V4 Flash'}
WHISPER = 'small.en'
VOICE = 'bf_emma'           # Kokoro (Apache-2.0), British English

app = modal.App('candil-brain')
vol = modal.Volume.from_name('candil-library', create_if_missing=True)


def _bake_models():
    from faster_whisper import WhisperModel
    WhisperModel(WHISPER, device='cpu', compute_type='int8', download_root='/models/whisper')
    try:   # the voice is optional: if it fails to build, answers still come back as text
        from kokoro import KPipeline
        p = KPipeline(lang_code='b')
        for _ in p('Ready.', voice=VOICE):
            pass
    except Exception as e:
        print('voice not baked:', e)


image = (
    modal.Image.debian_slim(python_version='3.12')
    .apt_install('git', 'ffmpeg', 'poppler-utils', 'espeak-ng')
    .pip_install('libzim==3.13.1', 'onnxruntime==1.31.1', 'tokenizers==0.23.3', 'numpy', 'beautifulsoup4==4.15.0',
                 'pypdfium2==5.14.0', 'fastapi[standard]', 'httpx', 'faster-whisper', 'soundfile')
    .pip_install('torch', index_url='https://download.pytorch.org/whl/cpu')
    .pip_install('kokoro>=0.9', 'misaki[en]')
    .run_commands(f'git clone https://github.com/Immortal-Protocols/lastbook /app/lastbook && cd /app/lastbook && git checkout {LASTBOOK_COMMIT}')
    .env({'HF_HOME': '/models/hf', 'LASTBOOK_DATA': LIB, 'SURVIVAL_PDF_ENGINE': 'pdfium', 'SURVIVAL_DISABLE_INDEX_WATCH': '1',
          'SURVIVAL_MODEL_BASE': 'http://127.0.0.1:19443', 'SURVIVAL_MODEL': 'quick', 'SURVIVAL_REASONING_EFFORT': 'low'})
    .run_function(_bake_models)
)


# ------------------------------------------------------------------ library
@app.function(image=image, volumes={DATA: vol}, timeout=3 * 3600, cpu=2, memory=4096)
def setup_library(pack: str = 'small'):
    """Download lastbook's library pack from downloads.lastbook.org, check every SHA-256, unpack into the volume."""
    import hashlib, json, os, shutil, urllib.request, zipfile
    from pathlib import Path
    # an honest client name: the download site turns away Python's default one (the user approved fetching the public pack)
    UA = {'User-Agent': 'CandilDemo/0.1 (lastbook setup; Small pack)', 'Accept-Encoding': 'identity'}
    get = lambda url, t: urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=t)
    try:
        m = json.load(get('https://downloads.lastbook.org/manifest-v1.json', 60))
    except Exception as e:
        raise RuntimeError(f'manifest: {e}') from None
    p = next(x for x in m['packs'] if x['id'] == pack)
    work = Path(DATA) / 'work'; work.mkdir(parents=True, exist_ok=True)
    for f in p['files']:
        dst = work / f['name']
        if not (dst.exists() and dst.stat().st_size == f['size']):
            print('downloading', f['name'], round(f['size'] / 1e9, 2), 'GB', flush=True)
            try:
                with get(f['url'], 120) as r, open(dst, 'wb') as out:
                    shutil.copyfileobj(r, out, 1 << 22)
            except Exception as e:
                raise RuntimeError(f'{f["name"]}: {e}') from None
        h = hashlib.sha256()
        with open(dst, 'rb') as fh:
            for b in iter(lambda: fh.read(1 << 22), b''):
                h.update(b)
        assert h.hexdigest() == f['sha256'], f'checksum mismatch: {f["name"]}'
        print('checked', f['name'], flush=True)
    unpack = work / 'unpack'
    shutil.rmtree(unpack, ignore_errors=True); unpack.mkdir()
    for f in p['files']:
        with zipfile.ZipFile(work / f['name']) as z:
            for n in z.namelist():   # same guard as lastbook's installer: no absolute paths or ..
                assert not n.startswith('/') and '..' not in Path(n).parts, n
            z.extractall(unpack)
    cat = next(unpack.rglob('library-data/catalog.json'))
    root = cat.parent.parent
    shutil.rmtree(LIB, ignore_errors=True)
    shutil.move(str(root), LIB)
    (Path(LIB) / 'derived').mkdir(exist_ok=True)
    shutil.rmtree(work)
    vol.commit()
    print('library ready at', LIB, flush=True)


@app.function(image=image, volumes={DATA: vol}, timeout=3 * 3600, cpu=2, memory=4096)
def ensure_library(pack: str = 'small'):
    """Quick when the library is already in the volume; otherwise downloads it (first deploy only)."""
    import os
    if os.path.exists(LIB + '/library-data/catalog.json'):
        print('library present:', LIB, flush=True)
        return
    setup_library.local(pack)


# ------------------------------------------------------------------ relay to OpenRouter (loopback only)
def start_relay(api_key):
    import json, threading, httpx
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Relay(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'

        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps({'object': 'list', 'data': [{'id': k} for k in MODELS]}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(body)

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
            tier = req.get('model') if req.get('model') in MODELS else 'quick'
            req['model'] = MODELS[tier]
            req.pop('reasoning_effort', None)
            req['reasoning'] = {'effort': 'low', 'exclude': True}
            req['provider'] = {'data_collection': 'deny', 'ignore': ['DeepSeek']}   # hosts that keep no data; not DeepSeek's own servers
            hdr = {'Authorization': f'Bearer {api_key}', 'HTTP-Referer': 'https://candil.demo', 'X-Title': 'Candil demo'}
            with httpx.Client(timeout=httpx.Timeout(180, connect=20)) as c:
                for attempt in (1, 2):
                    with c.stream('POST', 'https://openrouter.ai/api/v1/chat/completions', json=req, headers=hdr) as r:
                        if r.status_code >= 400 and attempt == 1:
                            r.read(); req.pop('provider', None); continue   # no matching host: let OpenRouter choose
                        self.send_response(r.status_code)
                        self.send_header('Content-Type', r.headers.get('content-type', 'text/event-stream'))
                        self.end_headers()
                        for chunk in r.iter_raw():
                            self.wfile.write(chunk); self.wfile.flush()
                        return

    srv = ThreadingHTTPServer(('127.0.0.1', 19443), Relay)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


# ------------------------------------------------------------------ answer -> page
def to_page(md, sources):
    """Lastbook's cited markdown answer, shaped for Candil's e-paper page and its voice."""
    import re
    clean = lambda s: re.sub(r'\s+([.,;:!?])', r'\1', re.sub(r'\s+', ' ', re.sub(r'\*\*|__|`|\s*\[(?:S|M)\d+\]', '', s))).strip()
    lines = [l.strip() for l in md.splitlines()]
    steps = [clean(re.sub(r'^\d+[.)]\s*', '', l)) for l in lines if re.match(r'^\d+[.)]\s+', l)]
    if not steps:
        steps = [clean(l[2:]) for l in lines if l.startswith(('- ', '* '))]
    head = next((clean(l) for l in lines if l and not l.startswith(('#', '-', '*', '|')) and not re.match(r'^\d+[.)]', l)), '')
    head = re.split(r'(?<=[.!?])\s', head, 1)[0] if head else (steps[0] if steps else '')
    src = sources[0] if sources else {}
    name = src.get('title') or src.get('publisher') or 'the Candil library'
    if src.get('publisher') and src.get('title') and src['publisher'] not in src['title']:
        name = f"{src['publisher']}: {src['title']}"
    if src.get('page'):
        name += f", page {src['page']}"
    return {'headline': head, 'steps': steps[:6], 'source': name}


# ------------------------------------------------------------------ the web endpoint
@app.cls(image=image, volumes={DATA: vol}, secrets=[modal.Secret.from_name('openrouter')], cpu=4, memory=8192,
         scaledown_window=900, timeout=300)
@modal.concurrent(max_inputs=4)
class Brain:
    @modal.enter()
    def boot(self):
        import os, sys, threading
        sys.path.insert(0, '/app/lastbook'); os.chdir('/app/lastbook')
        start_relay(os.environ['OPENROUTER_API_KEY'])
        import server, assistant
        server.init(no_index=True)
        assistant.SYSTEM += ('\n\nThis answer is shown on Candil, a small offline device with an e-paper page and a voice. '
                             'Begin with one short sentence that answers the question. Then give at most 6 numbered steps of '
                             'one sentence each, most urgent first, each ending with its source key. Then the warnings.')
        self.assistant, self.lock = assistant, threading.Lock()
        from faster_whisper import WhisperModel
        self.stt = WhisperModel(WHISPER, device='cpu', compute_type='int8', download_root='/models/whisper')
        try:
            from kokoro import KPipeline
            self.tts = KPipeline(lang_code='b')
        except Exception as e:
            print('voice off:', e); self.tts = None

    def hear(self, path):
        segs, _ = self.stt.transcribe(path, beam_size=1, vad_filter=True, language='en')
        return ' '.join(s.text.strip() for s in segs).strip()

    def speak(self, text):
        if not self.tts or not text:
            return None
        import base64, io, subprocess, numpy as np, soundfile as sf
        audio = np.concatenate([a for _, _, a in self.tts(text, voice=VOICE, speed=0.95)])
        wav = io.BytesIO(); sf.write(wav, audio, 24000, format='WAV')
        mp3 = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'wav', '-i', '-', '-b:a', '96k', '-f', 'mp3', '-'],
                             input=wav.getvalue(), capture_output=True).stdout
        return base64.b64encode(mp3).decode()

    def answer(self, question, tier):
        events = []
        with self.lock:
            self.assistant.MODEL = tier
            self.assistant.chat([{'role': 'user', 'content': question}], None, events.append, lambda: False)
        done = next((e for e in events if e.get('type') == 'done'), None)
        if not done:
            err = next((e for e in events if e.get('type') == 'error'), {})
            return {'error': err.get('text') or 'No answer came back.'}
        page = to_page(done['answer'], done['sources'])
        return {'answer_md': done['answer'], 'sources': done['sources'], 'page': page, 'latency': done.get('latency'),
                'tools': done.get('tools')}

    @modal.method()
    def ask_text(self, question: str, tier: str = 'quick'):
        t0 = __import__('time').time()
        out = self.answer(question, tier if tier in MODELS else 'quick')
        out['seconds'] = round(__import__('time').time() - t0, 2)
        return {'question': question, **out}

    @modal.asgi_app()
    def web(self):
        import tempfile, time
        from fastapi import FastAPI, File, Form, UploadFile
        from fastapi.middleware.cors import CORSMiddleware
        api = FastAPI(title='Candil brain')
        api.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['GET', 'POST'], allow_headers=['*'])

        @api.get('/health')
        def health():
            st = self.assistant.model_status()
            return {'ok': st['online'], 'model': st['model'], 'library': LIB}

        @api.get('/debug/model')
        def debug_model(tier: str = 'quick', stream: int = 1):
            # temporary: what the relay sends back for a tiny question, as lastbook would see it
            import json, urllib.request
            body = {'model': tier, 'messages': [{'role': 'user', 'content': 'Say hello in five words.'}], 'stream': bool(stream),
                    'max_tokens': 300, 'reasoning_effort': 'low'}
            req = urllib.request.Request('http://127.0.0.1:19443/v1/chat/completions', json.dumps(body).encode(), {'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    raw = r.read().decode('utf-8', 'replace')
                return {'status': 200, 'raw': raw[:6000]}
            except Exception as e:
                return {'error': repr(e), 'body': getattr(e, 'read', lambda: b'')().decode('utf-8', 'replace')[:3000]}

        @api.post('/ask')
        async def ask(audio: UploadFile | None = File(None), question: str | None = Form(None), tier: str = Form('quick')):
            t0 = time.time(); heard_s = None
            if audio is not None:
                with tempfile.NamedTemporaryFile(suffix='.' + (audio.filename or 'q.mp3').rsplit('.', 1)[-1]) as f:
                    f.write(await audio.read()); f.flush()
                    question = self.hear(f.name)
                heard_s = round(time.time() - t0, 2)
            question = (question or '').strip()[:600]
            if not question:
                return {'error': 'I did not hear a question.'}
            tier = tier if tier in MODELS else 'quick'
            out = self.answer(question, tier)
            if 'error' in out:
                return {'question': question, **out}
            p = out['page']
            spoken = ' '.join([p['headline']] + [f'{i + 1}. {s}' for i, s in enumerate(p['steps'])])
            t1 = time.time()
            return {'question': question, 'model': MODEL_LABEL[tier], **out, 'voice_mp3_b64': self.speak(spoken),
                    'timing': {'heard_s': heard_s, 'answer_s': out.get('latency', {}).get('total_s'),
                               'voice_s': round(time.time() - t1, 2), 'total_s': round(time.time() - t0, 2)}}

        return api


@app.local_entrypoint()
def smoke(question: str = 'The Wi-Fi is out. What should I do?', tier: str = 'quick'):
    """Ask the deployed brain one question from the command line and print what Candil would show."""
    import json
    Brain = modal.Cls.from_name('candil-brain', 'Brain')
    print(json.dumps(Brain().ask_text.remote(question, tier), indent=1)[:6000])
