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


def extract(url, entries, dest, threads=16, log=print):
    """Fetch the chosen entries into dest, keeping their paths; skip files already there with the right size."""
    done = {'n': 0, 'bytes': 0}
    lock = threading.Lock()
    t0 = time.time()

    def one(e):
        parts = e['name'].split('/')
        if e['name'].startswith('/') or '..' in parts:     # the same guard as lastbook's installer
            raise IOError(f'unsafe path in pack: {e["name"]}')
        path = os.path.join(dest, e['name'])
        if e['name'].endswith('/'):
            os.makedirs(path, exist_ok=True); return
        if os.path.exists(path) and os.path.getsize(path) == e['usize']:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        hdr = get_range(url, e['off'], e['off'] + 29)
        nlen, xlen = struct.unpack('<HH', hdr[26:30])
        start = e['off'] + 30 + nlen + xlen
        tmp = path + '.part'
        crc, step = 0, 64 << 20
        dec = zlib.decompressobj(-15) if e['method'] == 8 else None
        with open(tmp, 'wb') as f:
            for a in range(start, start + e['csize'], step):
                chunk = get_range(url, a, min(a + step, start + e['csize']) - 1)
                data = dec.decompress(chunk) if dec else chunk
                crc = zlib.crc32(data, crc); f.write(data)
            if dec:
                data = dec.flush(); crc = zlib.crc32(data, crc); f.write(data)
        if (crc & 0xFFFFFFFF) != e['crc']:
            os.remove(tmp); raise IOError(f'CRC mismatch: {e["name"]}')
        os.replace(tmp, path)
        with lock:
            done['n'] += 1; done['bytes'] += e['usize']
            if done['n'] % 200 == 0:
                log(f'{done["n"]} files, {done["bytes"] / 1e9:.1f} GB, {time.time() - t0:.0f} s', flush=True)

    big = [e for e in entries if e['usize'] > (1 << 30)]
    small = [e for e in entries if e['usize'] <= (1 << 30)]
    with ThreadPoolExecutor(threads) as ex:
        list(ex.map(one, small))
    for e in big:                                     # very large files (encyclopedias) one at a time, in 64 MB ranges
        log(f'large file {e["name"]} {e["usize"] / 1e9:.1f} GB', flush=True)
        one(e)
    log(f'done: {done["n"]} files, {done["bytes"] / 1e9:.1f} GB in {time.time() - t0:.0f} s', flush=True)


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
        nv = [e for e in allv if not is_video(e['name'])]
        print(f'without videos: {sum(e["usize"] for e in nv) / 1e9:.2f} GB in {len(nv)} files')
