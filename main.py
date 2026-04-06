#!/usr/bin/env python3
# ================================================================
# ASTUBE Backend v2.2
# Improvements:
#   - Persistent disk cache (survives restarts)
#   - Request deduplication (concurrent same-video calls share one yt-dlp)
#   - Smarter cache TTL (URLs expire in 4h, formats in 12h, info in 24h)
#   - /get-info reuses format cache to avoid double yt-dlp calls
#   - LRU eviction to keep memory low on Termux
#   - Compressed responses (gzip)
# ================================================================

import json, subprocess, shutil, time, logging, os, re, gzip, threading
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from functools import lru_cache
from collections import OrderedDict

# ── Config ──────────────────────────────────────────────────────
try:
    with open('config.json') as f:
        CONFIG = json.load(f)
except FileNotFoundError:
    CONFIG = {}

PORT        = int(os.environ.get('PORT', CONFIG.get('port', 5000)))
URL_TTL     = 4  * 3600   # stream URLs expire in 4h
FMT_TTL     = 12 * 3600   # format lists expire in 12h
INFO_TTL    = 24 * 3600   # video info expires in 24h
MAX_CACHED  = 100          # max videos in memory cache (LRU)
DISK_CACHE  = os.path.expanduser('~/.astube_cache')
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.txt')

# ── Logging ─────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('astube')

# ── Disk cache dir ───────────────────────────────────────────────
os.makedirs(DISK_CACHE, exist_ok=True)

# ── Binary detection ────────────────────────────────────────────
def find_bin(names):
    for name in names:
        for p in [
            shutil.which(name),
            os.path.expanduser(f'~/.local/bin/{name}'),
            f'/data/data/com.termux/files/usr/bin/{name}',
            f'/usr/local/bin/{name}', f'/usr/bin/{name}',
        ]:
            if p and os.path.isfile(p): return p
    return None

YTDLP = find_bin(['yt-dlp'])
log.info(f'yt-dlp: {YTDLP or "NOT FOUND ⚠️"}')
log.info(f'cookies.txt: {"FOUND ✅" if os.path.isfile(COOKIES_FILE) else "NOT FOUND ⚠️"}')

# ── Flask ────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app, origins='*', allow_headers=['Content-Type'], methods=['GET','POST','OPTIONS'])

# ================================================================
# LRU CACHE (thread-safe, max MAX_CACHED entries)
# ================================================================
class LRUCache:
    def __init__(self, maxsize=100):
        self._cache   = OrderedDict()
        self._maxsize = maxsize
        self._lock    = threading.Lock()

    def get(self, key, ttl):
        with self._lock:
            if key not in self._cache: return None
            entry = self._cache[key]
            if time.time() - entry['ts'] > ttl:
                del self._cache[key]; return None
            self._cache.move_to_end(key)
            return entry['val']

    def set(self, key, val):
        with self._lock:
            if key in self._cache: self._cache.move_to_end(key)
            self._cache[key] = {'val': val, 'ts': time.time()}
            if len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)  # evict oldest

    def count(self):
        return len(self._cache)

url_cache  = LRUCache(MAX_CACHED)
fmt_cache  = LRUCache(50)
info_cache = LRUCache(50)

# ================================================================
# DISK CACHE — persists across restarts
# ================================================================
def disk_get(key, ttl):
    path = os.path.join(DISK_CACHE, key.replace(':', '_') + '.json')
    try:
        if not os.path.exists(path): return None
        if time.time() - os.path.getmtime(path) > ttl:
            os.remove(path); return None
        with open(path) as f: return json.load(f)
    except: return None

def disk_set(key, val):
    path = os.path.join(DISK_CACHE, key.replace(':', '_') + '.json')
    try:
        with open(path, 'w') as f: json.dump(val, f)
    except: pass

# ================================================================
# REQUEST DEDUPLICATION
# Prevents 3 users requesting same video from spawning 3 yt-dlp processes
# ================================================================
_inflight      = {}   # key -> threading.Event
_inflight_res  = {}   # key -> result
_inflight_lock = threading.Lock()

def dedup_run(key, fn):
    """Run fn() once even if called concurrently with same key."""
    with _inflight_lock:
        if key in _inflight:
            ev = _inflight[key]
            is_leader = False
        else:
            ev = threading.Event()
            _inflight[key] = ev
            is_leader = True

    if not is_leader:
        # Wait for leader to finish (max 60s)
        ev.wait(timeout=60)
        with _inflight_lock:
            return _inflight_res.get(key)

    # Leader: run the function
    try:
        result = fn()
        with _inflight_lock:
            _inflight_res[key] = result
        return result
    finally:
        with _inflight_lock:
            _inflight.pop(key, None)
            _inflight_res.pop(key, None)
        ev.set()

# ================================================================
# HELPERS
# ================================================================
def build_ytdlp_args(extra_args):
    """Prepend --cookies flag if cookies.txt exists."""
    args = []
    if os.path.isfile(COOKIES_FILE):
        args += ['--cookies', COOKIES_FILE]
    return args + extra_args

def run_ytdlp(args, timeout=45):
    if not YTDLP: return None, 'yt-dlp not installed', 1
    try:
        r = subprocess.run([YTDLP] + args,
            capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return None, f'Timed out after {timeout}s', 408
    except Exception as e:
        return None, str(e), 1

def ytdlp_error(stderr):
    s = stderr.lower()
    if 'private'       in s: return 'Video is private', 404
    if 'unavailable'   in s: return 'Video unavailable', 404
    if 'sign in'       in s: return 'Requires sign-in', 403
    if 'not available' in s: return 'Not available in your region', 404
    return 'Could not extract video — update yt-dlp', 500

def fmt_dur(s):
    s = int(s or 0)
    h, m, sec = s//3600, (s%3600)//60, s%60
    return f'{h}:{m:02d}:{sec:02d}' if h else f'{m}:{sec:02d}'

def best_thumb(info):
    yt_id  = info.get('id', '')
    thumbs = [t for t in (info.get('thumbnails') or []) if t.get('url') and t.get('width')]
    if thumbs:
        return sorted(thumbs, key=lambda x: x.get('width',0)*x.get('height',0), reverse=True)[0]['url']
    return f'https://i.ytimg.com/vi/{yt_id}/hqdefault.jpg'

def validate_id(yt_id):
    return bool(yt_id and len(yt_id)==11 and all(c.isalnum() or c in '-_' for c in yt_id))

QUALITY_LABELS = {
    '17':'144p','160':'144p','18':'360p','396':'360p',
    '22':'720p','136':'720p','37':'1080p','137':'1080p',
    '135':'480p','244':'480p','134':'360p','243':'360p',
    '133':'240p','242':'240p',
}

def label_from_height(h):
    if not h: return '?p'
    h = int(h)
    for res in [2160,1440,1080,720,480,360,240,144]:
        if h >= res: return f'{res}p'
    return f'{h}p'

def gzip_json(data):
    """Return gzip-compressed JSON response."""

    payload = json.dumps(data, separators=(',',':')).encode()
    if 'gzip' in request.headers.get('Accept-Encoding',''):
        compressed = gzip.compress(payload, compresslevel=6)
        return Response(compressed, mimetype='application/json',
            headers={'Content-Encoding':'gzip','Vary':'Accept-Encoding'})
    return Response(payload, mimetype='application/json')

# ── Parse formats from yt-dlp info ──────────────────────────────
def parse_formats(info):
    raw = info.get('formats') or []
    combined = []
    for f in raw:
        if f.get('vcodec','') in ('none','') or f.get('acodec','') in ('none',''): continue
        itag   = str(f.get('format_id',''))
        height = f.get('height') or 0
        label  = QUALITY_LABELS.get(itag) or label_from_height(height)
        fsize  = f.get('filesize') or f.get('filesize_approx') or 0
        existing = next((x for x in combined if x['label']==label), None)
        if existing:
            if fsize > existing.get('filesize',0): combined.remove(existing)
            else: continue
        combined.append({
            'itag': itag, 'label': label,
            'height': height, 'width': f.get('width') or 0,
            'ext': f.get('ext','mp4'), 'fps': f.get('fps') or 0,
            'filesize': fsize, 'note': f.get('format_note',''),
        })
    combined.sort(key=lambda x: x['height'], reverse=True)
    default_itag = '18' if any(f['itag']=='18' for f in combined) else (combined[-1]['itag'] if combined else '18')
    for f in combined: f['default'] = (f['itag'] == default_itag)
    return {
        'formats': combined, 'default_itag': default_itag,
        'title': info.get('title',''), 'thumbnail': best_thumb(info),
    }

# ================================================================
# ENDPOINTS
# ================================================================

@app.route('/health')
def health():
    return gzip_json({
        'status':'ok','server':'ASTUBE','version':'2.2',
        'timestamp': int(time.time()),
        'cached_urls': url_cache.count(),
        'cached_fmts': fmt_cache.count(),
        'ytdlp': bool(YTDLP),
        'cookies': os.path.isfile(COOKIES_FILE),
    })


@app.route('/get-formats')
def get_formats():
    yt_id = request.args.get('ytId','').strip()
    if not validate_id(yt_id):
        return jsonify({'error':'Invalid ytId'}), 400

    # 1. Memory cache
    cached = fmt_cache.get(yt_id, FMT_TTL)
    if cached: return gzip_json({'formats': cached, 'cached': True})

    # 2. Disk cache
    cached = disk_get(f'fmt_{yt_id}', FMT_TTL)
    if cached:
        fmt_cache.set(yt_id, cached)
        return gzip_json({'formats': cached, 'cached': True})

    # 3. Fetch — deduplicated
    def fetch():
        log.info(f'Fetching formats for {yt_id}…')
        stdout, stderr, code = run_ytdlp(
            build_ytdlp_args(['--dump-json','--no-playlist', f'https://www.youtube.com/watch?v={yt_id}']),
            timeout=45)
        if code != 0: return None, ytdlp_error(stderr)
        try: info = json.loads(stdout)
        except: return None, ('Failed to parse output', 500)
        result = parse_formats(info)
        # Also cache info separately so /get-info can reuse it
        info_data = {
            'ytId': info.get('id', yt_id), 'title': info.get('title',''),
            'description': (info.get('description') or '').strip()[:500],
            'thumbnail': best_thumb(info),
            'duration': int(info.get('duration') or 0),
            'durationFormatted': fmt_dur(info.get('duration')),
            'channel': info.get('channel') or info.get('uploader') or 'Unknown',
            'uploadDate': info.get('upload_date',''),
            'viewCount': info.get('view_count', 0),
        }
        info_cache.set(yt_id, info_data)
        disk_set(f'info_{yt_id}', info_data)
        return result, None

    result, err = dedup_run(f'fmt_{yt_id}', fetch) or (None, ('Failed', 500))
    if result is None:
        msg, status = err if err else ('Unknown error', 500)
        return jsonify({'error': msg}), status

    fmt_cache.set(yt_id, result)
    disk_set(f'fmt_{yt_id}', result)
    log.info(f'✅ {len(result.get("formats",[]))} formats for {yt_id}')
    return gzip_json({'formats': result, 'cached': False})


@app.route('/get-url')
def get_url():
    yt_id = request.args.get('ytId','').strip()
    itag  = request.args.get('itag','18').strip()
    if not validate_id(yt_id):
        return jsonify({'error':'Invalid ytId'}), 400

    cache_key = f'{yt_id}:{itag}'

    # 1. Memory cache
    cached = url_cache.get(cache_key, URL_TTL)
    if cached: return gzip_json({'url': cached, 'cached': True, 'ytId': yt_id, 'itag': itag})

    # 2. Disk cache
    cached = disk_get(f'url_{cache_key}', URL_TTL)
    if cached:
        url_cache.set(cache_key, cached)
        return gzip_json({'url': cached, 'cached': True, 'ytId': yt_id, 'itag': itag})

    # 3. Fetch — deduplicated
    def fetch():
        log.info(f'Extracting URL for {yt_id} itag={itag}…')
        stdout, stderr, code = run_ytdlp(
            build_ytdlp_args(['-f', itag, '-g', '--no-playlist', f'https://www.youtube.com/watch?v={yt_id}']),
            timeout=30)
        if code == 408: return None, ('Timed out', 504)
        if code != 0:
            if itag != '18':
                log.warning(f'itag {itag} failed, falling back to 18')
                s2, se2, c2 = run_ytdlp(
                    build_ytdlp_args(['-f','18','-g','--no-playlist', f'https://www.youtube.com/watch?v={yt_id}']),
                    timeout=30)
                if c2 == 0 and s2 and s2.startswith('http'):
                    return s2, None
            return None, ytdlp_error(stderr)
        if not stdout or not stdout.startswith('http'):
            return None, ('No valid URL', 500)
        return stdout, None

    result, err = dedup_run(f'url_{cache_key}', fetch) or (None, ('Dedup failed', 500))
    if err: return jsonify({'error': err[0]}), err[1]

    url_cache.set(cache_key, result)
    disk_set(f'url_{cache_key}', result)
    log.info(f'✅ URL ready for {yt_id} itag={itag}')
    return gzip_json({'url': result, 'cached': False, 'ytId': yt_id, 'itag': itag})


@app.route('/get-info')
def get_info():
    yt_id = request.args.get('ytId','').strip()
    if not validate_id(yt_id):
        return jsonify({'error':'Invalid ytId'}), 400

    # 1. Memory cache
    cached = info_cache.get(yt_id, INFO_TTL)
    if cached: return gzip_json(cached)

    # 2. Disk cache
    cached = disk_get(f'info_{yt_id}', INFO_TTL)
    if cached:
        info_cache.set(yt_id, cached)
        return gzip_json(cached)

    # 3. Reuse format cache data if already fetched (avoids double yt-dlp)
    fmt = fmt_cache.get(yt_id, FMT_TTL) or disk_get(f'fmt_{yt_id}', FMT_TTL)
    if fmt and fmt.get('title'):
        info_data = {
            'ytId': yt_id, 'title': fmt.get('title',''),
            'thumbnail': fmt.get('thumbnail',''),
            'description': '', 'duration': 0, 'durationFormatted': '0:00',
            'channel': 'Unknown', 'uploadDate': '', 'viewCount': 0,
        }
        info_cache.set(yt_id, info_data)
        return gzip_json(info_data)

    # 4. Full fetch
    log.info(f'Fetching info for {yt_id}…')
    stdout, stderr, code = run_ytdlp(
        build_ytdlp_args(['--dump-json','--no-playlist', f'https://www.youtube.com/watch?v={yt_id}']),
        timeout=45)
    if code == 408: return jsonify({'error':'Timed out'}), 504
    if code != 0:
        msg, status = ytdlp_error(stderr)
        return jsonify({'error': msg}), status

    try: info = json.loads(stdout)
    except: return jsonify({'error':'Failed to parse output'}), 500

    dur = int(info.get('duration') or 0)
    result = {
        'ytId': info.get('id', yt_id), 'title': info.get('title',''),
        'description': (info.get('description') or '').strip()[:500],
        'thumbnail': best_thumb(info), 'duration': dur,
        'durationFormatted': fmt_dur(dur),
        'channel': info.get('channel') or info.get('uploader') or 'Unknown',
        'uploadDate': info.get('upload_date',''), 'viewCount': info.get('view_count', 0),
    }
    info_cache.set(yt_id, result)
    disk_set(f'info_{yt_id}', result)
    return gzip_json(result)


@app.route('/clear-cache')
def clear_cache():
    """Emergency cache clear endpoint."""
    try:
        for f in os.listdir(DISK_CACHE):
            os.remove(os.path.join(DISK_CACHE, f))
    except: pass
    return jsonify({'status':'ok','message':'Cache cleared'})



if __name__ == '__main__':
    if not YTDLP: log.error('yt-dlp not found!')
    log.info(f'🚀 ASTUBE v2.2 on port {PORT}')
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)

