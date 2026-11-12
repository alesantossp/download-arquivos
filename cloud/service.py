"""Private online download engine. Access only through the authenticated Site."""
import hmac
import subprocess
import tempfile
from collections import deque
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'companion'))
os.environ.setdefault('REEL_OUTPUT_DIR', '/tmp/reel')
import server as engine

API_KEY = os.environ.get('REEL_INTERNAL_KEY', '')
TTL = 3600
CAPACITY = 20
PROCESS_SLOT = threading.Semaphore(1)
INSPECTION_SLOTS = threading.BoundedSemaphore(2)
INSPECTION_CACHE = {}
INSPECTION_RATE = {}


def inspect_video(data):
    with tempfile.TemporaryDirectory(prefix='reel-inspect-') as work:
        payload = dict(data, work=work, inspect_only=True)
        process = subprocess.Popen([sys.executable, str(engine.BASE / 'server.py'), '--worker'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            output, errors = process.communicate(json.dumps(payload)+'\n', timeout=35)
        except subprocess.TimeoutExpired:
            engine.kill_process(process)
            process.communicate()
            raise RuntimeError('A análise demorou mais que o esperado. Tente novamente ou inicie o download.')
        messages = []
        for line in output.splitlines():
            try:
                message = json.loads(line)
                if message.get('event') == 'inspection':
                    return message['data']
                if message.get('event') == 'error':
                    messages.append(message.get('error',''))
            except (ValueError, TypeError):
                pass
        raise RuntimeError(engine.translate_error(messages[-1] if messages else errors[-500:]))


def process_job(job, request):
    while True:
        with engine.LOCK:
            if job.get('cancel'):
                job['status'] = 'cancelled'
                return
            waiting = sorted((x for x in engine.JOBS.values() if x['status'] == 'queued'), key=lambda x:x['created'])
            if waiting and waiting[0]['id'] == job['id'] and PROCESS_SLOT.acquire(blocking=False):
                job['status'] = 'analyzing'
                break
        time.sleep(.2)
    try:
        if job.get('cancel'):
            job['status'] = 'cancelled'
            return
        job['status'] = 'analyzing'
        engine.run_job(job, request)
    finally:
        PROCESS_SLOT.release()


def expire():
    with engine.LOCK:
        outdated = [j for j in engine.JOBS.values() if time.time() - j['created'] > TTL]
        for j in outdated:
            j['cancel'] = True
            engine.kill_process(j.get('process'))
            if j.get('filename'):
                (engine.OUTPUT / j['filename']).unlink(missing_ok=True)
            shutil.rmtree(j['work'], ignore_errors=True)
            engine.JOBS.pop(j['id'], None)


class Handler(BaseHTTPRequestHandler):
    server_version = 'ReelOnline/1.0'

    def log_message(self, *args):
        pass

    def authorized(self):
        return bool(API_KEY) and hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + API_KEY)

    def owner(self):
        value = self.headers.get('X-Reel-Owner', '')
        return value if re.fullmatch(r'[a-f0-9]{64}', value) else None

    def job(self, jid):
        with engine.LOCK:
            j = engine.JOBS.get(jid)
            return j if j and j.get('owner') == self.owner() else None

    def reply(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        if os.environ.get('FLY_MACHINE_ID'):
            self.send_header('X-Reel-Instance', os.environ['FLY_MACHINE_ID'])
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/health':
            return self.reply({'ok': True, 'version': '2.1', 'inspection': True, 'region': os.environ.get('FLY_REGION', ''), 'formats': ['mp4', 'mp3', 'jpg']})
        if not self.authorized() or not self.owner():
            return self.reply({'error': 'Acesso não autorizado.'}, 403)
        expire()
        if path == '/api/health':
            return self.reply({'ok': True, 'online': True, 'version': '2.1', 'inspection': True, 'region': os.environ.get('FLY_REGION', ''), 'formats': ['mp4', 'mp3', 'jpg'], 'frame_intervals': list(engine.FRAME_RATES), 'max_frames': engine.MAX_FRAMES})
        match = re.fullmatch(r'/api/jobs/([a-f0-9]{24})(/file)?', path)
        if not match:
            return self.reply({'error': 'Não encontrado.'}, 404)
        j = self.job(match.group(1))
        if not j:
            return self.reply({'error': 'Download não encontrado ou expirado.'}, 404)
        if not match.group(2):
            data = engine.public(j)
            data.pop('owner', None)
            data.pop('filename', None)
            data['folder'] = ''
            if j['status'] == 'queued':
                data['queue_position'] = 1 + sum(x['status'] == 'queued' and x['created'] < j['created'] for x in engine.JOBS.values())
            return self.reply(data)
        if j['status'] != 'done' or j.get('cancel'):
            return self.reply({'error': 'O arquivo ainda não está pronto ou foi cancelado.'}, 409)
        file = engine.OUTPUT / j['filename']
        if not file.is_file():
            return self.reply({'error': 'O arquivo expirou. Inicie novamente.'}, 410)
        length = file.stat().st_size
        start, end = 0, length - 1
        requested = self.headers.get('Range', '')
        if requested:
            r = re.fullmatch(r'bytes=(\d+)-(\d*)', requested)
            if not r:
                return self.reply({'error': 'Intervalo inválido.'}, 416)
            start = int(r.group(1)); end = min(length-1, int(r.group(2)) if r.group(2) else length-1)
            if start > end or start >= length:
                return self.reply({'error': 'Intervalo inválido.'}, 416)
        self.send_response(206 if requested else 200)
        self.send_header('Content-Type', {'.mp4':'video/mp4', '.mp3':'audio/mpeg', '.zip':'application/zip'}.get(file.suffix, 'application/octet-stream'))
        self.send_header('Content-Disposition', "attachment; filename=video" + file.suffix + "; filename*=UTF-8''" + quote(file.name))
        self.send_header('Content-Length', str(end-start+1))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Cache-Control', 'private, no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if requested:
            self.send_header('Content-Range', f'bytes {start}-{end}/{length}')
        self.end_headers()
        try:
            with file.open('rb') as f:
                f.seek(start)
                remaining = end-start+1
                while remaining > 0 and not j.get('cancel'):
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if not self.authorized() or not self.owner():
            return self.reply({'error': 'Acesso não autorizado.'}, 403)
        expire()
        try:
            count = int(self.headers.get('Content-Length', '0'))
            if not 0 < count <= 16384:
                raise ValueError('Dados inválidos.')
            data = json.loads(self.rfile.read(count))
            path = urlsplit(self.path).path
            if path == '/api/inspect':
                if data.get('quality') not in ('best', '1080', '720', '480') or data.get('format') not in ('mp4', 'mp3', 'jpg') or data.get('frame_interval', '1') not in engine.FRAME_RATES:
                    return self.reply({'error':'Selecione qualidade e formato válidos.'},400)
                url = data.get('url','')
                parsed = urlsplit(url)
                if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or len(url)>8192:
                    return self.reply({'error':'Cole um link público completo.'},400)
                key = (self.owner(), url, data['quality'], data['format'], data.get('frame_interval','1'))
                now = time.time()
                with engine.LOCK:
                    cached = INSPECTION_CACHE.get(key)
                    if cached and cached[0] > now:
                        return self.reply(cached[1])
                    for old_key in list(INSPECTION_CACHE):
                        if INSPECTION_CACHE[old_key][0] <= now:
                            INSPECTION_CACHE.pop(old_key,None)
                    for owner in list(INSPECTION_RATE):
                        if not INSPECTION_RATE[owner] or INSPECTION_RATE[owner][-1] < now-60:
                            INSPECTION_RATE.pop(owner,None)
                    rate = INSPECTION_RATE.setdefault(self.owner(), deque(maxlen=20))
                    if len(rate)==20 and rate[0]>now-60:
                        return self.reply({'error':'Muitas análises em sequência. Aguarde um minuto e tente novamente.'},429)
                    if len(INSPECTION_RATE)>1000 or not INSPECTION_SLOTS.acquire(blocking=False):
                        return self.reply({'error':'A análise está ocupada. Aguarde alguns segundos e tente novamente.'},429)
                    rate.append(now)
                try:
                    result = inspect_video(data)
                    with engine.LOCK:
                        if len(INSPECTION_CACHE)>=128:
                            INSPECTION_CACHE.pop(next(iter(INSPECTION_CACHE)),None)
                        INSPECTION_CACHE[key]=(time.time()+300,result)
                    return self.reply(result)
                except Exception as error:
                    return self.reply({'error':str(error)},422)
                finally:
                    INSPECTION_SLOTS.release()
            if path == '/api/jobs':
                if data.get('quality') not in ('best', '1080', '720', '480') or data.get('format') not in ('mp4', 'mp3', 'jpg', 'original'):
                    raise ValueError('Selecione qualidade e formato válidos.')
                if data.get('frame_interval', '1') not in engine.FRAME_RATES:
                    raise ValueError('Frequência de frames inválida.')
                url = data.get('url', '')
                parsed = urlsplit(url)
                if not parsed.hostname or parsed.scheme not in ('http', 'https') or len(url) > 8192:
                    raise ValueError('Cole um link público válido.')
                with engine.LOCK:
                    pending = [j for j in engine.JOBS.values() if j['status'] not in engine.TERMINAL]
                    if any(j['owner'] == self.owner() for j in pending):
                        return self.reply({'error': 'Você já tem um download em andamento. Aguarde ou cancele antes de iniciar outro.'}, 409)
                    if len(pending) >= 8:
                        return self.reply({'error': 'A fila está cheia. Tente novamente em instantes.'}, 429)
                    usage = sum(j['created'] > time.time() - 3600 and j['owner'] == self.owner() for j in engine.JOBS.values())
                    if usage >= 10:
                        return self.reply({'error': 'Você atingiu 10 solicitações nesta hora. Aguarde para iniciar outras.'}, 429)
                    if len(engine.JOBS) >= CAPACITY:
                        return self.reply({'error': 'O serviço está no limite temporário. Tente novamente mais tarde.'}, 429)
                    jid = secrets.token_hex(12)
                    j = {'id': jid, 'owner': self.owner(), 'status': 'queued', 'format': data.get('format'), 'percent': 0, 'cancel': False, 'process': None, 'work': str(engine.OUTPUT / '.temporarios' / jid), 'created': time.time()}
                    engine.JOBS[jid] = j
                    threading.Thread(target=process_job, args=(j, data), daemon=True).start()
                return self.reply({'id': jid, 'status': j['status']}, 202)
            match = re.fullmatch(r'/api/jobs/([a-f0-9]{24})/cancel', path)
            if match:
                j = self.job(match.group(1))
                if not j:
                    return self.reply({'error': 'Download não encontrado.'}, 404)
                with engine.LOCK:
                    if j['status'] in ('error', 'cancelled'):
                        return self.reply({'status': j['status']})
                    j['cancel'] = True
                    if j['status'] == 'done':
                        (engine.OUTPUT / j['filename']).unlink(missing_ok=True)
                        j['status'] = 'cancelled'
                    else:
                        j['status'] = 'cancelling'
                    process = j.get('process')
                engine.kill_process(process)
                return self.reply({'status': j['status']}, 202)
            return self.reply({'error': 'Não encontrado.'}, 404)
        except (ValueError, TypeError, AttributeError):
            return self.reply({'error': 'Dados inválidos. Verifique o link, a qualidade e o formato.'}, 400)


if __name__ == '__main__':
    if len(API_KEY) < 32:
        raise SystemExit('REEL_INTERNAL_KEY must contain at least 32 characters.')
    engine.OUTPUT.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(engine.OUTPUT / '.temporarios', ignore_errors=True)
    import atexit
    atexit.register(engine.cleanup)
    def sweeper():
        while True:
            time.sleep(60)
            expire()
    threading.Thread(target=sweeper, daemon=True).start()
    httpd = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '10000'))), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        engine.cleanup()
