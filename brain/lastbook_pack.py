"""Read lastbook's library packs selectively.

A pack is one large ZIP on downloads.lastbook.org. With HTTP range requests we read only the ZIP's central directory
(its table of contents) and then only the files we want, so taking the full library without its videos does not mean
downloading the videos. Every extracted file is checked against the CRC-32 the ZIP records for it.

    python3 lastbook_pack.py probe all          # what is in the pack, by folder and type
"""
import json, os, struct, sys, threading, time, urllib.request, zlib
from concurrent.futures import ThreadPoolExecutor

BASE = 'https://downloads.lastbook.org/'
UA = {'User-Agent': 'CandilDemo/0.1 (lastbook setup; selected files from a pack, no videos)', 'Accept-Encoding': 'identity'}
VIDEO_EXT = ('.mp4', '.webm', '.mkv', '.mov', '.m4v', '.avi')


def manifest():
    with urllib.request.urlopen(urllib.request.Request(BASE + 'manifest-v1.json', headers=UA), timeout=60) as r:
        return json.load(r)


def pack_files(pack_id):
    """The pack's files as the manifest lists them: [{name, size, url, sha256}]."""
    p = next(x for x in manifest()['packs'] if x['id'] == pack_id)
    return p['files'], p


def get_range(url, start, end, tries=6):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={**UA, 'Range': f'bytes={start}-{end}'})
            with urllib.request.urlopen(req, timeout=120) as r:
                if r.status != 206:
                    raise IOError(f'server answered {r.status}, not a partial response: ranges unsupported')
                return r.read()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def total_size(url):
    req = urllib.request.Request(url, headers={**UA, 'Range': 'bytes=0-0'})
    with urllib.request.urlopen(req, timeout=60) as r:
        cr = r.headers.get('Content-Range', '')
        return r.status, int(cr.split('/')[-1]) if '/' in cr else int(r.headers.get('Content-Length', 0)), dict(r.headers)


def central_directory(url):
    status, size, headers = total_size(url)
    if status != 206:
        raise SystemExit(f'ranges not supported (status {status}); headers {headers}')
    tail = get_range(url, max(0, size - 131072), size - 1)
    i = tail.rfind(b'PK\x05\x06')
    entries = struct.unpack('<H', tail[i + 10:i + 12])[0]
    cd_size, cd_off = struct.unpack('<II', tail[i + 12:i + 20])
    j = tail.rfind(b'PK\x06\x07')                      # ZIP64 locator: the real numbers are in the ZIP64 end record
    if j >= 0:
        z64_off = struct.unpack('<Q', tail[j + 8:j + 16])[0]
        rec = get_range(url, z64_off, z64_off + 55)
        entries, cd_size, cd_off = struct.unpack('<QQQ', rec[32:56])
    cd = get_range(url, cd_off, cd_off + cd_size - 1)
    out, p = [], 0
    while p < len(cd) and cd[p:p + 4] == b'PK\x01\x02':
        (method, crc, csize, usize, nlen, xlen, clen, off) = (
            struct.unpack('<H', cd[p + 10:p + 12])[0], struct.unpack('<I', cd[p + 16:p + 20])[0],
            struct.unpack('<I', cd[p + 20:p + 24])[0], struct.unpack('<I', cd[p + 24:p + 28])[0],
            struct.unpack('<H', cd[p + 28:p + 30])[0], struct.unpack('<H', cd[p + 30:p + 32])[0],
            struct.unpack('<H', cd[p + 32:p + 34])[0], struct.unpack('<I', cd[p + 42:p + 46])[0])
        name = cd[p + 46:p + 46 + nlen].decode('utf-8', 'replace')
        extra = cd[p + 46 + nlen:p + 46 + nlen + xlen]
        q = 0
        while q + 4 <= len(extra):                     # ZIP64 extra field: sizes and offset that did not fit
            tag, ln = struct.unpack('<HH', extra[q:q + 4]); body = extra[q + 4:q + 4 + ln]
            if tag == 1:
                vals, k = [], 0
                for want in (usize == 0xFFFFFFFF, csize == 0xFFFFFFFF, off == 0xFFFFFFFF):
                    if want:
                        vals.append(struct.unpack('<Q', body[k:k + 8])[0]); k += 8
                    else:
                        vals.append(None)
                usize = vals[0] if vals[0] is not None else usize
                csize = vals[1] if vals[1] is not None else csize
                off = vals[2] if vals[2] is not None else off
            q += 4 + ln
        out.append({'name': name, 'method': method, 'crc': crc, 'csize': csize, 'usize': usize, 'off': off})
        p += 46 + nlen + xlen + clen
    return size, out


def is_video(name):
    return name.lower().endswith(VIDEO_EXT)


def summary(entries):
    groups = {}
    for e in entries:
        parts = e['name'].split('/')
        top = '/'.join(parts[:3]) if len(parts) > 3 else '/'.join(parts[:-1]) or '(root)'
        ext = os.path.splitext(e['name'])[1].lower() or '(none)'
        g = groups.setdefault(top, {'files': 0, 'gb': 0.0, 'ext': {}})
        g['files'] += 1; g['gb'] += e['usize'] / 1e9
        g['ext'][ext] = g['ext'].get(ext, 0) + e['usize'] / 1e9
    return groups


def spans(entries, size, max_gap=16 << 20):
    """Group wanted entries into stretches of the ZIP that can each be fetched as one stream. A stretch is cut where
    an unwanted gap (a video, say) is longer than max_gap, so videos are never downloaded in bulk."""
    out = []
    for e in sorted(entries, key=lambda e: e['off']):
        end = min(size, e['off'] + 30 + len(e['name'].encode()) + 4096 + e['csize'])
        if out and e['off'] - out[-1][1] <= max_gap:
            out[-1][1] = max(out[-1][1], end); out[-1][2].append(e)
        else:
            out.append([e['off'], end, [e]])
    return out


class RangeStream:
    """Sequential reader over bytes [start, end) of url. If the connection drops or the server is busy, it waits
    (longer each time, as the server asks) and carries on from where it was."""

    def __init__(self, url, start, end):
        self.url, self.pos, self.end, self.r = url, start, end, None

    def _open(self):
        for i in range(12):
            try:
                req = urllib.request.Request(self.url, headers={**UA, 'Range': f'bytes={self.pos}-{self.end - 1}'})
                self.r = urllib.request.urlopen(req, timeout=120)
                if self.r.status != 206:
                    raise IOError(f'server answered {self.r.status}, not a partial response')
                return
            except Exception as ex:
                wait = min(300, 10 * 2 ** i)
                ra = getattr(ex, 'headers', None) and ex.headers.get('Retry-After')
                if ra and str(ra).isdigit():
                    wait = max(wait, int(ra))
                print(f'download busy ({type(ex).__name__}: {ex}); waiting {wait} s', flush=True)
                time.sleep(wait)
        raise IOError('the download server stayed unavailable')

    def read(self, n):
        out = bytearray()
        while len(out) < n and self.pos < self.end:
            if self.r is None:
                self._open()
            try:
                b = self.r.read(min(n - len(out), 1 << 20))
                if not b:
                    raise IOError('connection closed early')
                out += b; self.pos += len(b)
            except Exception as ex:
                print(f'connection lost at byte {self.pos} ({type(ex).__name__}); resuming', flush=True)
                self.r = None
                time.sleep(5)
        return bytes(out)

    def skip_to(self, pos):
        if pos - self.pos > (8 << 20):                    # far ahead: a new request is cheaper than reading through
            self.r, self.pos = None, pos
        else:
            while self.pos < pos:
                self.read(min(pos - self.pos, 1 << 20))


def extract(url, entries, dest, size, log=print, after_span=None):
    """Fetch the chosen entries into dest, keeping their paths, one stream per stretch of the ZIP. Files already
    there with the right size are kept; every new file is checked against its CRC-32."""
    t0, got = time.time(), 0
    sp = spans(entries, size)
    log(f'{len(entries)} files in {len(sp)} stretches, {sum(b - a for a, b, _ in sp) / 1e9:.1f} GB to read', flush=True)
    for n, (a, b, ents) in enumerate(sp, 1):
        todo = []
        for e in ents:
            parts = e['name'].split('/')
            if e['name'].startswith('/') or '..' in parts:     # the same guard as lastbook's installer
                raise IOError(f'unsafe path in pack: {e["name"]}')
            path = os.path.join(dest, e['name'])
            if e['name'].endswith('/'):
                os.makedirs(path, exist_ok=True)
            elif not (os.path.exists(path) and os.path.getsize(path) == e['usize']):
                todo.append((e, path))
        if not todo:
            continue
        rs = RangeStream(url, todo[0][0]['off'], b)
        for e, path in todo:
            rs.skip_to(e['off'])
            hdr = rs.read(30)
            if hdr[:4] != b'PK\x03\x04':
                raise IOError(f'no local header for {e["name"]}')
            nlen, xlen = struct.unpack('<HH', hdr[26:30])
            rs.read(nlen + xlen)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp, crc, left = path + '.part', 0, e['csize']
            dec = zlib.decompressobj(-15) if e['method'] == 8 else None
            with open(tmp, 'wb') as f:
                while left:
                    chunk = rs.read(min(left, 4 << 20)); left -= len(chunk)
                    data = dec.decompress(chunk) if dec else chunk
                    crc = zlib.crc32(data, crc); f.write(data)
                if dec:
                    data = dec.flush(); crc = zlib.crc32(data, crc); f.write(data)
            if (crc & 0xFFFFFFFF) != e['crc']:
                os.remove(tmp); raise IOError(f'CRC mismatch: {e["name"]}')
            os.replace(tmp, path)
            got += e['usize']
        log(f'stretch {n}/{len(sp)} done: {got / 1e9:.1f} GB so far, {time.time() - t0:.0f} s', flush=True)
        if after_span:
            after_span()
    log(f'done: {got / 1e9:.1f} GB in {time.time() - t0:.0f} s', flush=True)


if __name__ == '__main__':
    if sys.argv[1] == 'probe':
        files, meta = pack_files(sys.argv[2])
        print('pack', json.dumps({k: v for k, v in meta.items() if k != 'files'})[:600])
        allv = []
        for f in files:
            size, entries = central_directory(f['url'])
            print(f"file {f['name']}: {size / 1e9:.2f} GB, {len(entries)} entries")
            allv += entries
        g = summary(allv)
        for top, v in sorted(g.items(), key=lambda kv: -kv[1]['gb'])[:45]:
            ext = ', '.join(f'{k} {gb:.2f}' for k, gb in sorted(v['ext'].items(), key=lambda kv: -kv[1])[:4])
            print(f'{v["gb"]:9.2f} GB {v["files"]:7d} files  {top}   [{ext}]')
        nv = [e for e in allv if not is_video(e['name']) and not e['name'].startswith('LastbookData/derived/geo/')]
        print(f'without videos or maps: {sum(e["usize"] for e in nv) / 1e9:.2f} GB in {len(nv)} files')
        sp = spans(nv, size)
        print(f'{len(sp)} stretches, {sum(b - a for a, b, _ in sp) / 1e9:.2f} GB to read; largest gap read through 16 MB')
