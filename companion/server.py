#!/usr/bin/env python3
"""Reel local. Local-only HTTP UI and cancellable yt-dlp child processes."""
import argparse
import atexit
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

BASE = Path(__file__).resolve().parent
WEB = BASE / 'web'
OUTPUT = Path(os.environ.get('REEL_OUTPUT_DIR', str(Path.home() / 'Downloads' / 'Reel'))).resolve()
PORT = int(os.environ.get('REEL_PORT', '8765'))
TOKEN = secrets.token_urlsafe(32)
JOBS = {}
LOCK = threading.RLock()
MAX_JOBS = 100
TERMINAL = {'done', 'error', 'cancelled'}


def validate_url(value):
    if not isinstance(value, str) or len(value) > 8192:
        raise ValueError('Link inválido ou muito longo.')
    p = urlsplit(value)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('Use um link completo com http:// ou https://.')
    # This service is intended for internet videos, never local network resources.
    import ipaddress
    try:
        addresses = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80))
        if any(not ipaddress.ip_address(addr[4][0]).is_global for addr in addresses):
            raise ValueError('Use um link público da internet.')
    except socket.gaierror:
        raise ValueError('Não foi possível localizar o site. Verifique o link e a internet.')
    return value


def public(job):
    return {k: v for k, v in job.items() if k not in {'process', 'work', 'cancel', 'created'}}


def update(job, **values):
    with LOCK:
        job.update(values)


def kill_process(process):
    if not process or process.poll() is not None:
        return
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
        except OSError:
            pass


def translate_error(text):
    text = re.sub(r'\x1b\[[0-9;]*m', '', str(text))
    lower = text.lower()
    if any(s in lower for s in ('private video', 'login required', 'log in', 'sign in', 'cookies', 'authentication', 'confirm you', 'members-only')):
        return 'A plataforma exige login ou verificação para este vídeo. Esta versão trabalha com vídeos públicos e não importa sua sessão do navegador.'
    if any(s in lower for s in ('drm', 'protected')):
        return 'Este vídeo tem proteção que a aplicação não consegue processar.'
    if any(s in lower for s in ('unsupported url', 'no video', 'not find', 'not supported', 'unable to extract')):
        return 'Não foi possível encontrar um vídeo acessível neste link. Use o endereço direto da publicação, em vez do perfil ou da coleção. Alguns sites, como Cosmos, podem não expor o arquivo.'
    if any(s in lower for s in ('403', '429', 'blocked', 'forbidden', 'rate-limit')):
        return 'A plataforma bloqueou ou limitou esta tentativa. Tente novamente mais tarde; a disponibilidade depende do site.'
    if any(s in lower for s in ('requested format is not available', 'no video formats')):
        return 'Não há um formato de vídeo acessível nesta qualidade. Experimente Original ou outro link.'
    if any(s in lower for s in ('ffmpeg', 'conversion failed', 'postprocessing', 'libpostproc')):
        return 'Não foi possível finalizar o formato. Tente a opção Original ou execute Atualizar para reparar as dependências.'
    if any(s in lower for s in ('timed out', 'connection', 'network', 'resolve')):
        return 'Falha de conexão com a plataforma. Verifique a internet e tente novamente.'
    return ('O download não foi concluído. ' + text.replace('ERROR: ', ''))[:650]


def run_job(job, request):
    work = Path(job['work'])
    process = None
    try:
        with LOCK:
            if job['cancel']:
                return
            kwargs = {'stdin': subprocess.PIPE, 'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE, 'text': True, 'encoding': 'utf-8', 'errors': 'replace'}
            if os.name == 'nt':
                kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs['start_new_session'] = True
            process = subprocess.Popen([sys.executable, str(BASE / 'server.py'), '--worker'], **kwargs)
            job['process'] = process
        payload = dict(request, work=str(work))
        process.stdin.write(json.dumps(payload) + '\n')
        process.stdin.close()
        errors = []

        def read_errors():
            for line in process.stderr:
                errors.append(line.strip())
                if len(errors) > 40:
                    errors.pop(0)

        t = threading.Thread(target=read_errors, daemon=True)
        t.start()
        result = None
        for line in process.stdout:
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            with LOCK:
                if job['cancel']:
                    break
                if event.get('event') == 'result':
                    result = event
                elif event.get('event') == 'error':
                    errors.append(event.get('error', 'Falha ao baixar.'))
                else:
                    event.pop('event', None)
                    job.update(event)
        process.wait()
        t.join(timeout=2)
        with LOCK:
            if job['cancel']:
                return
            if process.returncode != 0 or not result:
                raise RuntimeError(errors[-1] if errors else 'A plataforma não retornou um vídeo.')
            candidate = Path(result['path']).resolve()
            if candidate.parent != work.resolve() or not candidate.is_file():
                raise RuntimeError('O arquivo final não foi encontrado.')
            OUTPUT.mkdir(parents=True, exist_ok=True)
            filename = candidate.name
            final = OUTPUT / filename
            n = 1
            while final.exists():
                final = OUTPUT / f'{candidate.stem} ({n}){candidate.suffix}'
                n += 1
            candidate.replace(final)
            job.update(status='done', percent=100, size=final.stat().st_size, filename=final.name, folder='Downloads/Reel', title=result.get('title') or final.stem, resolution=result.get('resolution') or job.get('resolution', ''))
    except Exception as error:
        with LOCK:
            if not job['cancel']:
                job.update(status='error', error=translate_error(error))
    finally:
        if process and process.poll() is None:
            kill_process(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        shutil.rmtree(work, ignore_errors=True)
        with LOCK:
            job['process'] = None
            if job['cancel']:
                job['status'] = 'cancelled'


def worker():
    import contextlib
    import yt_dlp
    request = json.loads(sys.stdin.readline())
    validate_url(request['url'])
    work = Path(request['work'])
    work.mkdir(parents=True, exist_ok=True)

    def emit(**data):
        print(json.dumps(data, ensure_ascii=True), flush=True)

    class Logger:
        def debug(self, message):
            pass
        def warning(self, message):
            print(message, file=sys.stderr, flush=True)
        def error(self, message):
            print(message, file=sys.stderr, flush=True)

    # res is min(width,height), so portrait 1080x1920 is correctly treated as 1080p.
    quality = request.get('quality', 'best')
    sort = ['res' if quality == 'best' else f'res:{quality}', 'fps', 'hdr:12']
    ffmpeg = shutil.which('ffmpeg')
    media_config = BASE / '.tools' / 'media-tools.json'
    if media_config.exists():
        configured = json.loads(media_config.read_text(encoding='utf-8')).get('ffmpeg')
        if configured and Path(configured).is_file():
            ffmpeg = configured
    bundled = BASE / '.tools' / ('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
    if bundled.exists():
        ffmpeg = str(bundled)
    if not ffmpeg:
        raise RuntimeError('FFmpeg não está instalado. Execute Iniciar novamente.')
    last = [0.0]
    parts = {'index': 0, 'name': None, 'total': 1}
    title = ['Vídeo']
    selected_size = [0]

    def progress(data):
        if data.get('status') == 'finished':
            emit(event='progress', status='processing', percent=100)
            return
        if data.get('status') != 'downloading':
            return
        if data.get('filename') != parts['name']:
            parts['name'] = data.get('filename')
            parts['index'] += 1
        now = time.monotonic()
        if now - last[0] < .25:
            return
        last[0] = now
        size = data.get('total_bytes') or data.get('total_bytes_estimate') or 0
        got = data.get('downloaded_bytes') or 0
        fraction = min(1, got / size) if size else 0
        percent = min(99.9, ((parts['index'] - 1 + fraction) / parts['total']) * 100)
        emit(event='progress', status='downloading', percent=percent, size=selected_size[0] or size,
             speed=data.get('speed'), eta=data.get('eta'), part=f'{parts["index"]}/{parts["total"]}' if parts['total'] > 1 else '')

    def postprocess(data):
        emit(event='progress', status='processing', percent=100)

    opts = {
        'noplaylist': True, 'playlist_items': '1', 'quiet': True, 'no_warnings': True,
        'logger': Logger(), 'socket_timeout': 20, 'retries': 2, 'fragment_retries': 2,
        'outtmpl': str(work / '%(title).100B [%(id).40B].%(ext)s'),
        'windowsfilenames': True, 'restrictfilenames': False,
        'format': 'bv*+ba/b', 'format_sort': sort,
        'ffmpeg_location': ffmpeg, 'progress_hooks': [progress],
        'postprocessor_hooks': [postprocess], 'cachedir': False,
        'allow_unplayable_formats': False,
    }
    deno = BASE / '.tools' / ('deno.exe' if os.name == 'nt' else 'deno')
    if deno.exists():
        opts['js_runtimes'] = {'deno': {'path': str(deno)}}
    elif shutil.which('deno'):
        opts['js_runtimes'] = {'deno': {}}
    elif shutil.which('node'):
        opts['js_runtimes'] = {'node': {}}
    if request.get('format') == 'mp4':
        opts['merge_output_format'] = 'mp4'
        opts['postprocessors'] = [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}]
    try:
        # Keep stdout a strict event stream, including when third-party extractors print.
        with yt_dlp.YoutubeDL(opts) as ydl:
            with contextlib.redirect_stdout(sys.stderr):
                info = ydl.extract_info(request['url'], download=False)
                if info and info.get('_type') in ('playlist', 'multi_video'):
                    info = next((i for i in info.get('entries', []) if i), None)
                    if info:
                        info = ydl.process_ie_result(info, download=False)
            if not info or not (info.get('formats') or info.get('url')):
                raise RuntimeError('No video formats available')
            if info.get('is_live'):
                raise RuntimeError('Transmissões ao vivo não são suportadas nesta versão. Use um vídeo já publicado.')
            if info.get('has_drm'):
                raise RuntimeError('DRM protected video')
            title[0] = info.get('title') or 'Vídeo'
            selected = info.get('requested_downloads') or [info]
            chosen = selected[0]
            streams = chosen.get('requested_formats') or [chosen]
            parts['total'] = max(1, len(streams))
            selected_size[0] = sum(f.get('filesize') or f.get('filesize_approx') or 0 for f in streams)
            w, h = chosen.get('width') or info.get('width'), chosen.get('height') or info.get('height')
            if quality != 'best' and w and h and min(w, h) > int(quality):
                raise RuntimeError('Requested format is not available: não há resolução igual ou inferior ao limite selecionado. Tente Original.')
            emit(event='metadata', status='downloading', title=title[0], platform=info.get('extractor_key', ''),
                 resolution=f'{w} × {h}' if w and h else 'Resolução informada pela fonte', size=selected_size[0], percent=0)
            # Hooks must write to the original stdout even while extractor chatter goes to stderr.
            raw_stdout = sys.stdout
            def safe_emit(**data):
                print(json.dumps(data, ensure_ascii=True), file=raw_stdout, flush=True)
            emit = safe_emit
            with contextlib.redirect_stdout(sys.stderr):
                ydl.process_info(info)
        candidates = [p for p in work.iterdir() if p.is_file() and p.suffix.lower() in ('.mp4', '.webm', '.mkv', '.mov', '.m4v', '.avi', '.flv', '.ts', '.3gp')]
        if not candidates:
            raise RuntimeError('Arquivo final não encontrado.')
        final = max(candidates, key=lambda p: p.stat().st_mtime)
        probe = Path(ffmpeg).parent / ('ffprobe.exe' if os.name == 'nt' else 'ffprobe')
        if not probe.exists():
            raise RuntimeError('FFprobe não encontrado. Execute Atualizar para reparar o FFmpeg.')
        measured = subprocess.run([str(probe), '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'json', str(final)], capture_output=True, text=True, timeout=30, check=True)
        dimensions = json.loads(measured.stdout).get('streams', [{}])[0]
        actual_w, actual_h = dimensions.get('width'), dimensions.get('height')
        if quality != 'best' and actual_w and actual_h and min(actual_w, actual_h) > int(quality):
            raise RuntimeError('Requested format is not available: o único arquivo da fonte excede a resolução escolhida. Tente Original.')
        emit(event='result', path=str(final), title=title[0], resolution=f'{actual_w} × {actual_h}' if actual_w and actual_h else '')
    except Exception as error:
        emit(event='error', error=str(error))
        sys.exit(1)


class Handler(BaseHTTPRequestHandler):
    server_version = 'Reel/1.0'

    def log_message(self, fmt, *args):
        pass

    def reply(self, data, status=200):
        body = json.dumps(data, ensure_ascii=True).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def safe_host(self):
        return self.headers.get('Host') in (f'127.0.0.1:{PORT}', f'localhost:{PORT}')

    def authorized(self):
        origin = self.headers.get('Origin')
        return self.safe_host() and self.headers.get('X-Reel-Token') == TOKEN and (not origin or origin in (f'http://127.0.0.1:{PORT}', f'http://localhost:{PORT}'))

    def do_GET(self):
        if not self.safe_host():
            return self.reply({'error': 'Host inválido.'}, 403)
        path = urlsplit(self.path).path
        if path.startswith('/api/'):
            if not self.authorized():
                return self.reply({'error': 'Sessão inválida. Recarregue a página local.'}, 403)
            if path == '/api/health':
                return self.reply({'ok': True, 'version': '1.0'})
            if re.fullmatch(r'/api/jobs/[a-f0-9]{24}', path):
                with LOCK:
                    job = JOBS.get(path.rsplit('/', 1)[-1])
                    if job:
                        return self.reply(public(job))
            return self.reply({'error': 'Download não encontrado. Inicie novamente.'}, 404)
        assets = {'/': ('index.html', 'text/html; charset=utf-8'), '/index.html': ('index.html', 'text/html; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8')}
        if path not in assets:
            return self.reply({'error': 'Página não encontrada.'}, 404)
        name, mime = assets[path]
        body = (WEB / name).read_bytes()
        if name == 'index.html':
            body = body.replace(b'window.__REEL_CONFIG__ = null;', ('window.__REEL_CONFIG__ = ' + json.dumps({'token': TOKEN}) + ';').encode())
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.authorized():
            return self.reply({'error': 'Sessão inválida. Recarregue a página local.'}, 403)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > 16384:
                return self.reply({'error': 'Requisição inválida.'}, 400)
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('Requisição inválida.')
            path = urlsplit(self.path).path
            if path == '/api/jobs':
                url = data.get('url')
                # DNS resolution runs in the cancellable job thread, not this request.
                parsed = urlsplit(url) if isinstance(url, str) else None
                if not parsed or parsed.scheme not in ('http', 'https') or not parsed.hostname or len(url) > 8192:
                    raise ValueError('Cole um link público válido com http:// ou https://.')
                if data.get('quality') not in ('best', '1080', '720', '480') or data.get('format') not in ('mp4', 'original'):
                    raise ValueError('Escolha uma qualidade e um formato válidos.')
                with LOCK:
                    if any(j['status'] not in TERMINAL for j in JOBS.values()):
                        return self.reply({'error': 'Já existe um download em andamento. Conclua ou cancele antes de iniciar outro.'}, 409)
                    if len(JOBS) >= MAX_JOBS:
                        del JOBS[next(iter(JOBS))]
                    jid = secrets.token_hex(12)
                    work = OUTPUT / '.temporarios' / jid
                    job = {'id': jid, 'status': 'analyzing', 'percent': 0, 'cancel': False, 'process': None, 'work': str(work), 'created': time.time()}
                    JOBS[jid] = job
                    threading.Thread(target=run_job, args=(job, data), daemon=True).start()
                    return self.reply(public(job), 202)
            match = re.fullmatch(r'/api/jobs/([a-f0-9]{24})/cancel', path)
            if match:
                with LOCK:
                    job = JOBS.get(match.group(1))
                    if not job:
                        return self.reply({'error': 'Download não encontrado.'}, 404)
                    if job['status'] in TERMINAL:
                        return self.reply(public(job))
                    job['cancel'] = True
                    job['status'] = 'cancelling'
                    process = job['process']
                kill_process(process)
                return self.reply(public(job), 202)
            if path == '/api/folder':
                OUTPUT.mkdir(parents=True, exist_ok=True)
                if sys.platform == 'darwin':
                    subprocess.Popen(['open', str(OUTPUT)])
                elif os.name == 'nt':
                    os.startfile(str(OUTPUT))
                else:
                    subprocess.Popen(['xdg-open', str(OUTPUT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return self.reply({'ok': True})
            return self.reply({'error': 'Operação não encontrada.'}, 404)
        except (ValueError, TypeError) as e:
            return self.reply({'error': str(e) or 'Dados inválidos.'}, 400)
        except Exception:
            return self.reply({'error': 'Não foi possível completar a operação. Confira a janela do componente.'}, 500)


def cleanup():
    with LOCK:
        pending = [j for j in JOBS.values() if j['status'] not in TERMINAL]
        for job in pending:
            job['cancel'] = True
    for job in pending:
        kill_process(job['process'])
        shutil.rmtree(job['work'], ignore_errors=True)


def serve(no_browser=False):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    # The owner lock prevents two instances and accidental cleanup of another instance.
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    temporary = OUTPUT / '.temporarios'
    if temporary.exists():
        shutil.rmtree(temporary, ignore_errors=True)
    atexit.register(cleanup)
    address = f'http://127.0.0.1:{PORT}/'
    print(f'\n  reel. está pronto: {address}\n  Vídeos salvos em: {OUTPUT}\n  Mantenha esta janela aberta. Ctrl+C para encerrar.\n', flush=True)
    if not no_browser:
        threading.Timer(.6, lambda: webbrowser.open(address)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        cleanup()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if args.worker:
        worker()
    else:
        try:
            serve(args.no_browser)
        except OSError as e:
            print(f'Não foi possível iniciar na porta {PORT}: {e}. Se o reel já estiver aberto, use http://127.0.0.1:{PORT}/', file=sys.stderr)
            sys.exit(1)
