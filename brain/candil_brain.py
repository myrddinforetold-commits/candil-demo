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
          'SURVIVAL_MODEL_BASE': 'http://127.0.0.1:19443', 'SURVIVAL_MODEL': 'quick', 'SURVIVAL_REASONING_EFFORT': 'none'})
    .run_function(_bake_models)
    .add_local_python_source('lastbook_pack')
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


@app.function(image=image, volumes={DATA: vol}, timeout=6 * 3600, cpu=32, memory=32768)
def build_index(lib: str = LIB):
    """Lastbook's meaning-based search. Packs ship without the embedding model, so fetch BAAI bge-small-en-v1.5 (ONNX,
    MIT licence, the official BAAI repository) into the library, then embed every document once with lastbook's own
    retrieval.py. Afterwards the brain searches by meaning as well as by keyword."""
    import os, shutil, subprocess, sys
    from huggingface_hub import hf_hub_download
    dst = lib + '/derived/models/bge-small-en-v1.5'
    os.makedirs(dst, exist_ok=True)
    for src, name in (('onnx/model.onnx', 'model.onnx'), ('tokenizer.json', 'tokenizer.json')):
        if not os.path.exists(f'{dst}/{name}'):
            shutil.copy(hf_hub_download('BAAI/bge-small-en-v1.5', src), f'{dst}/{name}')
    vol.commit()
    r = subprocess.run([sys.executable, 'retrieval.py'], cwd='/app/lastbook', env={**os.environ, 'LASTBOOK_DATA': lib})
    vol.commit()
    print('index build exit', r.returncode, flush=True)
    return r.returncode


FULL_LIB = DATA + '/full/LastbookData'     # the full library without its videos or its US maps


@app.function(image=image, volumes={DATA: vol}, timeout=12 * 3600, cpu=4, memory=8192)
def setup_full_library():
    """Lastbook's full library (pack "all") without its videos (232 GB) or its US maps (80 GB): documents, courses,
    transcripts, encyclopedias and search databases, about 76 GB. Read straight out of the pack with range requests
    (lastbook_pack.py), every file checked against its CRC-32, into a separate folder; then the meaning-based index is
    built over it. The brain keeps using the small library until LIB is switched to FULL_LIB."""
    import time
    import lastbook_pack as L
    keep = lambda e: not L.is_video(e['name']) and not e['name'].startswith('LastbookData/derived/geo/')
    files, meta = L.pack_files('all')
    for f in files:
        size, entries = L.central_directory(f['url'])
        sel = [e for e in entries if keep(e)]
        print(f"{f['name']}: taking {len(sel)} of {len(entries)} files, {sum(e['usize'] for e in sel) / 1e9:.1f} GB", flush=True)
        small = [e for e in sel if e['usize'] <= (1 << 30)]
        big = [e for e in sel if e['usize'] > (1 << 30)]
        for i in range(0, len(small), 3000):                 # save to the volume as it goes, in case the job restarts
            L.extract(f['url'], small[i:i + 3000], DATA + '/full', threads=16)
            vol.commit()
            print(f'saved {min(i + 3000, len(small))} of {len(small)} files', flush=True)
        for e in big:
            L.extract(f['url'], [e], DATA + '/full', threads=1)
            vol.commit()
    t = time.time()
    code = build_index.local(FULL_LIB)
    print('index built in', round(time.time() - t), 's, exit', code, flush=True)


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
            if tier == 'quick':   # Gemma spends a whole round thinking at 'low'; lastbook's own note says 'none' answers straight away
                req['reasoning'] = {'effort': 'none', 'exclude': True}
            else:                 # DeepSeek thinks a little; its thinking counts against the token budget
                req['reasoning'] = {'effort': 'low', 'exclude': True}
                req['max_tokens'] = max(int(req.get('max_tokens') or 0), 4000)
            req['provider'] = {'data_collection': 'deny', 'ignore': ['DeepSeek']}   # hosts that keep no data; not DeepSeek's own servers
            hdr = {'Authorization': f'Bearer {api_key}', 'HTTP-Referer': 'https://candil-demo.vercel.app', 'X-Title': 'Candil demo', 'Accept-Encoding': 'identity'}
            with httpx.Client(timeout=httpx.Timeout(180, connect=20)) as c:
                for attempt in (1, 2):
                    with c.stream('POST', 'https://openrouter.ai/api/v1/chat/completions', json=req, headers=hdr) as r:
                        if r.status_code >= 400 and attempt == 1:
                            r.read(); req.pop('provider', None); continue   # no matching host: let OpenRouter choose
                        self.send_response(r.status_code)
                        self.send_header('Content-Type', r.headers.get('content-type', 'text/event-stream'))
                        self.end_headers()
                        for chunk in r.iter_bytes():   # decoded: lastbook reads plain SSE lines
                            self.wfile.write(chunk); self.wfile.flush()
                        return

    srv = ThreadingHTTPServer(('127.0.0.1', 19443), Relay)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


# ------------------------------------------------------------------ answer -> page
ZIM_LABEL = {'wikipedia': 'Wikipedia', 'wikibooks': 'Wikibooks', 'mdwiki': 'WikiMed (medical Wikipedia)',
             'appropedia': 'Appropedia'}


def add_encyclopedia_search(A):
    """Give lastbook's assistant a search_encyclopedia tool. Lastbook ships its encyclopedias (Kiwix ZIM files with a
    full-text index) for browsing, but its assistant only searches documents and transcripts; this lets Candil's answers
    draw on them too. Each article found is a citable source, like a library passage."""
    import glob, os, re, threading
    from libzim.reader import Archive
    from libzim.search import Searcher, Query
    from bs4 import BeautifulSoup
    paths = sorted(glob.glob(os.environ['LASTBOOK_DATA'] + '/library-data/collections/*.zim'))
    order = lambda p: next((i for i, k in enumerate(('mdwiki', 'wikipedia', 'wikibooks', 'appropedia')) if os.path.basename(p).startswith(k)), 9)
    paths.sort(key=order)
    arcs, lock = {}, threading.Lock()
    print('encyclopedias:', [os.path.basename(p) for p in paths], flush=True)

    def arc(p):
        with lock:
            if p not in arcs:
                arcs[p] = Archive(p)
            return arcs[p]

    def label(p):
        n = os.path.basename(p).lower()
        return next((v for k, v in ZIM_LABEL.items() if n.startswith(k)), os.path.basename(p))

    def passage(a, entry_path, q):
        e = a.get_entry_by_path(entry_path)
        if e.is_redirect:
            e = e.get_redirect_entry()
        soup = BeautifulSoup(bytes(e.get_item().content).decode('utf-8', 'replace'), 'html.parser')
        for t in soup(['script', 'style', 'table', 'sup', 'figure', 'nav']):
            t.decompose()
        paras = [x.get_text(' ', strip=True) for x in soup.find_all(['p', 'li'])]
        paras = [x for x in paras if len(x) > 60][:80]
        if not paras:
            return e.title, ''
        words = set(re.findall(r'[a-z]{4,}', q.lower()))
        best = sorted(paras[1:], key=lambda x: -len(words & set(re.findall(r'[a-z]{4,}', x.lower()))))[:3]
        return e.title, ' '.join([paras[0]] + best)[:2000]

    def search(q, k=4):
        per = []
        for p in paths:
            try:
                a = arc(p)
                if not a.has_fulltext_index:
                    continue
                hits = []
                for ep in Searcher(a).search(Query().set_query(q)).getResults(0, 2):
                    title, text = passage(a, ep, q)
                    if text:
                        hits.append({'zim': f'{os.path.basename(p)}:{ep}', 'title': title, 'publisher': label(p),
                                     'kind': 'encyclopedia', 'trust': 'encyclopedia', 'excerpt': text})
                per.append(hits)
            except Exception as ex:   # one damaged file must not cost the answers from the others
                print('encyclopedia search skipped', os.path.basename(p), type(ex).__name__, flush=True)
        out = []
        for rank in range(2):                                  # the best article from each encyclopedia in turn
            out += [h[rank] for h in per if rank < len(h)]
        return out[:k]

    A.TOOLS.append({'type': 'function', 'function': {'name': 'search_encyclopedia', 'description':
        'Search the offline encyclopedias (Wikipedia, Wikibooks, WikiMed medical Wikipedia, Appropedia) for background '
        'knowledge the manuals do not cover. Returns article passages with citation keys like S3. Use a few plain keywords.',
        'parameters': {'type': 'object', 'properties': {'query': {'type': 'string', 'description': 'A few plain keywords'}},
                       'required': ['query']}}})
    plain = A.run_tool
    plain_add = A.Session.add

    def add(self, d):
        # Encyclopedia articles are not in lastbook's catalogue, so they carry no doc_id (lastbook would drop a
        # citation whose doc_id it cannot find) and are told apart by their article instead.
        if d.get('kind') != 'encyclopedia':
            return plain_add(self, d)
        for k, v in self.sources.items():
            if v.get('zim') == d.get('zim'):
                return k
        self.n += 1
        k = f'S{self.n}'
        self.sources[k] = dict(d, key=k)
        return k
    A.Session.add = add

    def run_tool(name, args, sess, extra_msgs):
        if name != 'search_encyclopedia':
            return plain(name, args, sess, extra_msgs)
        q = str(args.get('query', ''))[:200]
        items = search(q)
        if not items:
            return 'Nothing in the encyclopedias matched. Try other words.', {'query': q, 'count': 0}
        return '\n\n'.join(A._src_block(sess.add(it), it, it['excerpt']) for it in items), {'query': q, 'count': len(items)}
    A.run_tool = run_tool


def question_parts(question):
    """The separate questions in one spoken request, when there is more than one."""
    import re
    parts = [p.strip() for p in re.split(r'(?<=[?.!])\s+', question) if len(p.strip()) > 12]
    return parts if len(parts) > 1 else []


def outage_search(question):
    """For questions about the internet or phone network being down: an extra library search for staying informed."""
    import re
    if re.search(r"\b(wi-?fi|internet|online|broadband|router|network|no signal|mobile data|phones? (are|is) (down|out|dead))\b", question, re.I):
        return ('internet and phone networks down in an emergency: battery or hand-crank radio for news and alerts, '
                'text instead of calling, save phone battery, family communication plan and meeting place')
    return None


def nice_source(src):
    """Some saved web pages are titled by their address; give them a readable title and publisher."""
    import re
    src = dict(src)
    t = src.get('title') or ''
    if t.startswith('http'):
        host = re.sub(r'^www\.', '', t.split('/')[2]) if t.count('/') >= 2 else ''
        slug = re.sub(r'^(fema|ready)[_-]', '', t.rstrip('/').rsplit('/', 1)[-1].rsplit('.', 1)[0])
        src['title'] = slug.replace('-', ' ').replace('_', ' ').strip().capitalize()
        src['publisher'] = {'ready.gov': 'Ready.gov', 'fema.gov': 'FEMA'}.get(host, src.get('publisher') or host)
    return src


def to_page(md, sources):
    """Lastbook's cited markdown answer, shaped for Candil's e-paper page and its voice."""
    import re
    clean = lambda s: re.sub(r'\s+([.,;:!?])', r'\1', re.sub(r'\s+', ' ', re.sub(r'\*\*|__|`|\s*\[(?:S|M)\d+\]', '', s))).strip()
    lines = [l.strip() for l in md.splitlines()]
    # Lines under "Not found in the library", "My own reasoning", "Sources disagree" or "Warnings" never become steps.
    is_item = lambda l: bool(re.match(r'^([-*]\s|\d+[.)]\s)', l))
    body, skip = [], False
    for l in lines:
        if l and not is_item(l):
            bare = re.sub(r'[#*_]', '', l).strip().lower()
            if l.startswith('#') or bare.endswith(':'):
                skip = bare.startswith(('not found', 'my own reasoning', 'sources disagree', 'warning'))
                continue
        if not skip:
            body.append(l)
    raw = [re.sub(r'^\d+[.)]\s*', '', l) for l in body if re.match(r'^\d+[.)]\s+', l)]
    if not raw:
        raw = [l[2:] for l in body if l.startswith(('- ', '* '))]
    # The page shows only steps the library backs: a step with no source key is the model's own aside.
    if any(re.search(r'\[S\d+\]', r) for r in raw):
        raw = [r for r in raw if re.search(r'\[S\d+\]', r)]
    steps = [clean(re.sub(r'\[(?:Note|Source)[^\]]*\]', '', r)) for r in raw]
    steps = [re.sub(r'^[^:]{2,40}:\s*', '', st) if ':' in st[:42] else st for st in steps]
    steps = [st[:1].upper() + st[1:] for st in steps if st]
    head = next((clean(l) for l in lines if l and not l.startswith(('#', '-', '*', '|')) and not re.match(r'^\d+[.)]', l)), '')
    sents = re.split(r'(?<=[.!?])\s', head) if head else []
    sorry = re.compile(r"^(i am sorry|i'm sorry|sorry|unfortunately|i cannot|i can't|i can not|the library does not|the library doesn't)", re.I)
    sents = [x for x in sents if not sorry.match(x)]
    head = sents[0] if sents else (steps[0] if steps else '')
    warning = ''
    m = re.search(r'(?im)^\**\s*warnings?\b.*$', md)
    if m:
        for l in md[m.end():].splitlines():
            l = l.strip()
            if l.startswith(('* ', '- ')) or re.match(r'^\d+[.)]\s', l):
                w = clean(re.sub(r'^(\*|-|\d+[.)])\s*', '', l))
                w = re.sub(r'^[^:]{2,40}:\s*', '', w)          # drop a label such as "Carbon monoxide danger:"
                warning = re.split(r'(?<=[.!?])\s', w, 1)[0]
                break
            if l and not l.startswith(('*', '-')) and warning == '' and l.lower().startswith(('not found', 'my own')):
                break
    src = nice_source(sources[0]) if sources else {}
    name = src.get('title') or src.get('publisher') or 'the Candil library'
    if src.get('publisher') and src.get('title') and src['publisher'] not in src['title']:
        name = f"{src['publisher']}: {src['title']}"
    if src.get('page'):
        name += f", page {src['page']}"
    return {'headline': head, 'steps': steps[:6], 'warning': warning, 'source': name}


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
        add_encyclopedia_search(assistant)
        assistant.SYSTEM += ('\n\nThis answer is shown on Candil, a small offline device with an e-paper page and a voice. '
                             'Begin with one short sentence that answers the question. Then give at most 6 numbered steps of '
                             'one sentence each, most urgent first, each ending with its source key. Then the warnings.'
                             '\n\nThe first sentence gives the most useful thing to do, taken from the library. Never begin with '
                             'an apology or with what the library lacks; anything missing goes only under "Not found in the '
                             'library:" at the end. Every numbered step is a complete instruction that says what to do, never '
                             'a fragment, a definition or a fact on its own. If the initial results do not directly answer the '
                             'question, call search_library again with the task in plain words (for example "start a fire '
                             'without matches") before answering. Use at most two extra searches, and answer as soon as you have '
                             'enough.'
                             '\n\nAnswer for the situation the person describes. Never assume a disaster they have not mentioned '
                             '(such as a nuclear attack, fallout, a pandemic or a flood) and leave out advice that belongs only to '
                             'one. When the power is out, leave out anything that needs mains electricity, such as electric '
                             'heaters.'
                             '\n\nCandil is kept for emergencies. When the person says the internet, Wi-Fi, phone network or power '
                             'is out, treat it as a possible wider outage, not only a fault in their home network: before answering, '
                             'call search_library for how to stay informed and communicate when networks are down (for example a '
                             'battery or hand-crank radio, emergency alerts, texting instead of calling, saving phone battery, a '
                             'family communication plan) and answer from what that finds, alongside any quick check of the home '
                             'equipment the library gives.'
                             '\n\nFor background knowledge the manuals do not cover (what a plant, animal, illness, medicine or '
                             'material is, how something works, history and geography), call search_encyclopedia, which '
                             'searches the offline encyclopedias (Wikipedia, Wikibooks, WikiMed, Appropedia). Prefer the manuals '
                             'and health guides for safety steps; cite encyclopedia passages with their source keys too.'
                             '\n\nKeep every step true to its source, including any condition the source attaches to it '
                             '(for example, advice for when the internet works but the mobile network does not). Leave out a '
                             'step whose condition does not fit the person\'s situation.')
        self.assistant, self.lock = assistant, threading.Lock()
        from faster_whisper import WhisperModel
        self.stt = WhisperModel(WHISPER, device='cpu', compute_type='int8', download_root='/models/whisper')
        try:
            from kokoro import KPipeline
            self.tts = KPipeline(lang_code='b')
        except Exception as e:
            print('voice off:', e); self.tts = None

    def hear(self, path):
        # Decode with ffmpeg ourselves: faster-whisper's PyAV decoder breaks on newer PyAV releases.
        import subprocess, numpy as np
        pcm = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-ac', '1', '-ar', '16000', '-f', 'f32le', '-'],
                             capture_output=True, check=True).stdout
        samples = np.frombuffer(pcm, dtype=np.float32)
        segs, _ = self.stt.transcribe(samples, beam_size=1, vad_filter=True, language='en')
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
            A = self.assistant
            plain = A.hybrid
            extra = [q for q in [outage_search(question)] if q] + question_parts(question)
            if extra:
                # The first library search also runs for each part of a multi-part question and, for "the Wi-Fi
                # is out", for how to stay informed during an outage (Candil is an emergency device). The best
                # results of each search are taken in turn; the answer is still written only from what they return.
                first = {'done': False}
                def hybrid(q, k=8, *a, **kw):
                    hits = plain(q, k, *a, **kw)
                    if first['done'] or q != question[:300]:
                        return hits
                    first['done'] = True
                    lists = [plain(e, k) for e in extra] + [hits]
                    seen, out = set(), []
                    for rank in range(k):                          # take the best of each search in turn
                        for lst in lists:
                            if rank < len(lst):
                                h = lst[rank]; key = (h.get('doc_id'), h.get('page'))
                                if key not in seen:
                                    seen.add(key); out.append(h)
                    return out[:k]
                A.hybrid = hybrid
            try:
                A.chat([{'role': 'user', 'content': question}], None, events.append, lambda: False)
            finally:
                A.hybrid = plain
        done = next((e for e in events if e.get('type') == 'done'), None)
        if not done:
            err = next((e for e in events if e.get('type') == 'error'), {})
            return {'error': err.get('text') or 'No answer came back.'}
        page = to_page(done['answer'], done['sources'])
        return {'answer_md': done['answer'], 'sources': [nice_source(x) for x in done['sources']], 'page': page, 'latency': done.get('latency'),
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

        @api.post('/speak')
        def speak(text: str = Form(...)):
            from fastapi import Response
            import base64
            b64 = self.speak(text.strip()[:600])
            return Response(base64.b64decode(b64) if b64 else b'', media_type='audio/mpeg')

        @api.post('/ask')
        async def ask(audio: UploadFile | None = File(None), question: str | None = Form(None), tier: str = Form('quick'),
                      voice: int = Form(1)):
            t0 = time.time(); heard_s = None
            if audio is not None:
                try:
                    data = await audio.read()
                    with tempfile.NamedTemporaryFile(suffix='.' + (audio.filename or 'q.mp3').rsplit('.', 1)[-1]) as f:
                        f.write(data); f.flush()
                        question = self.hear(f.name)
                except Exception as e:
                    import traceback; traceback.print_exc()
                    return {'error': f'Candil could not hear that ({type(e).__name__}: {e})'[:300]}
                heard_s = round(time.time() - t0, 2)
            question = (question or '').strip()[:600]
            if not question:
                return {'error': 'I did not hear a question.'}
            tier = tier if tier in MODELS else 'quick'
            try:
                out = self.answer(question, tier)
            except Exception as e:
                import traceback; traceback.print_exc()
                return {'question': question, 'error': f'The answer failed ({type(e).__name__}: {e})'[:300]}
            if 'error' in out:
                return {'question': question, **out}
            p = out['page']
            spoken = ' '.join([p['headline']] + [f'{i + 1}. {s}' for i, s in enumerate(p['steps'])] + ([f"Warning. {p['warning']}"] if p.get('warning') else []))
            t1 = time.time()
            return {'question': question, 'model': MODEL_LABEL[tier], **out, 'voice_mp3_b64': self.speak(spoken) if voice else None,
                    'timing': {'heard_s': heard_s, 'answer_s': out.get('latency', {}).get('total_s'),
                               'voice_s': round(time.time() - t1, 2), 'total_s': round(time.time() - t0, 2)}}

        return api


@app.local_entrypoint()
def trial(tier: str = 'deep', questions: str = ''):
    """Ask several questions (separated by |) and print what Candil's page would show, for checking answer quality."""
    Brain = modal.Cls.from_name('candil-brain', 'Brain')
    b = Brain()
    for q in [x.strip() for x in questions.split('|') if x.strip()]:
        out = b.ask_text.remote(q, tier)
        p = out.get('page') or {}
        print(f"\n=== {q}   [{tier}, {out.get('seconds')} s]", flush=True)
        print('HEADLINE:', p.get('headline'))
        for i, st in enumerate(p.get('steps') or [], 1):
            print(f'  {i}. {st}')
        print('WARNING:', p.get('warning'))
        print('SOURCES:', ' | '.join(f"{x.get('publisher')}: {x.get('title')} p{x.get('page')}" for x in (out.get('sources') or [])[:6]))
        print('TOOLS:', [f"{t.get('name')}: {t.get('args', {}).get('query') or t.get('args', {}).get('doc_id')} -> {(t.get('summary') or {}).get('count')}" for t in (out.get('tools') or [])], flush=True)


@app.local_entrypoint()
def smoke(question: str = 'The Wi-Fi is out. What should I do?', tier: str = 'quick'):
    """Ask the deployed brain one question from the command line and print what Candil would show."""
    import json
    Brain = modal.Cls.from_name('candil-brain', 'Brain')
    print(json.dumps(Brain().ask_text.remote(question, tier), indent=1)[:6000])
