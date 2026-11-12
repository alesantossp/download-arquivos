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
import math
import zipfile
from fractions import Fraction
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
MAX_BYTES = 500 * 1024 * 1024
MAX_FRAMES = 5000
FRAME_RATES = {'1': '1', '2': '2', '5': '1/5', 'all': None}


def media_probe(path, probe):
    result = subprocess.run([str(probe), '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)], capture_output=True, text=True, timeout=45, check=True)
    return json.loads(result.stdout)


def media_duration(info):
    values = [info.get('format', {}).get('duration')] + [x.get('duration') for x in info.get('streams', [])]
    return max([float(x) for x in values if x not in (None, 'N/A', '')] or [0])


def finalize_media(source, output_format, ffmpeg, probe, expected_duration=0, frame_interval='1'):
    measured = media_probe(source, probe)
    streams = measured.get('streams', [])
    video = next((x for x in streams if x.get('codec_type') == 'video' and not x.get('disposition', {}).get('attached_pic')), None)
    audio = next((x for x in streams if x.get('codec_type') == 'audio'), None)
    duration = media_duration(measured)
    if expected_duration and duration and duration < float(expected_duration) - max(2, float(expected_duration) * .02):
        raise RuntimeError('O arquivo recebido está incompleto. A duração é menor que a do vídeo da fonte. Tente novamente.')
    if output_format in ('mp4', 'jpg') and not video:
        raise RuntimeError('A fonte não forneceu uma faixa de imagem. Este arquivo não será entregue como vídeo.')
    if output_format in ('mp4', 'mp3') and not audio:
        raise RuntimeError('A fonte não forneceu uma faixa de áudio. Não é possível entregar o arquivo com som. Você pode escolher JPG para extrair as imagens.')
    stem = source.stem
    final = source.with_name(stem + (' - frames.zip' if output_format == 'jpg' else ' - Reel.' + output_format))
    args = [str(ffmpeg), '-hide_banner', '-nostdin', '-v', 'error', '-xerror', '-y', '-i', str(source)]
    frames_count = 0
    if output_format == 'mp4':
        codec = ['-c:v', 'copy'] if video.get('codec_name') == 'h264' else ['-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p']
        args += ['-map', '0:v:0', '-map', '0:a:0', *codec, '-c:a', 'aac', '-b:a', '192k', '-threads', '2', '-movflags', '+faststart', str(final)]
    elif output_format == 'mp3':
        args += ['-map', '0:a:0', '-vn', '-c:a', 'libmp3lame', '-b:a', '192k', str(final)]
    elif output_format == 'jpg':
        rate = FRAME_RATES[frame_interval]
        fps = float(Fraction(video.get('avg_frame_rate') or '0/1')) if rate is None else float(Fraction(rate))
        if duration and duration * fps > MAX_FRAMES:
            raise RuntimeError('Esta frequência geraria mais de 5.000 frames. Escolha uma frequência menor ou um vídeo mais curto.')
        folder = source.parent / (source.stem + '-frames')
        folder.mkdir()
        if rate is not None:
            args += ['-vf', 'fps=' + rate]
        else:
            args += ['-fps_mode', 'passthrough']
        args += ['-map', '0:v:0', '-an', '-q:v', '2', '-threads', '2', '-frames:v', str(MAX_FRAMES + 1), str(folder / 'frame_%06d.jpg')]
    elif output_format == 'original':
        return source, measured, 0
    else:
        raise ValueError('Formato inválido.')
    try:
        subprocess.run(args, capture_output=True, text=True, timeout=1800, check=True)
    except subprocess.CalledProcessError as error:
        raise RuntimeError('A conversão não pôde ser concluída. ' + (error.stderr or '')[-300:]) from error
    if output_format == 'jpg':
        frames = sorted(folder.glob('*.jpg'))
        if not frames or len(frames) > MAX_FRAMES:
            raise RuntimeError('Não foi possível gerar todos os frames dentro do limite de 5.000 imagens. Escolha uma frequência menor.')
        frames_count = len(frames)
        if sum(frame.stat().st_size for frame in frames) > MAX_BYTES:
            raise RuntimeError('Os frames excederam 500 MB. Escolha uma frequência ou qualidade menor.')
        with zipfile.ZipFile(final, 'w', compression=zipfile.ZIP_STORED) as archive:
            for frame in frames:
                archive.write(frame, 'frames/' + frame.name)
            archive.writestr('frames/LEIA-ME.txt', f'Frames do vídeo: {stem}\nQuantidade: {frames_count}\nDuração: {duration:.3f}s\nFrequência: {frame_interval} (1 = um por segundo; 2 = dois por segundo; 5 = um a cada 5s; all = todos).\n')
        return final, measured, frames_count
    result = media_probe(final, probe)
    result_streams = result.get('streams', [])
    if not any(x.get('codec_type') == 'audio' for x in result_streams):
        raise RuntimeError('A validação final não encontrou áudio no arquivo convertido.')
    if output_format == 'mp4' and not any(x.get('codec_type') == 'video' for x in result_streams):
        raise RuntimeError('A validação final não encontrou imagem no MP4.')
    if duration and media_duration(result) < duration - max(2, duration * .02):
        raise RuntimeError('A conversão gerou um arquivo incompleto. Tente novamente.')
    # Decode the entire completed file. Do not deliver a corrupt or truncated MP4/MP3.
    subprocess.run([str(ffmpeg), '-nostdin', '-v', 'error', '-xerror', '-i', str(final), '-f', 'null', '-'], capture_output=True, timeout=1800, check=True)
    return final, result, frames_count


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


def is_region_restricted(text):
    lower = str(text).lower()
    return any(s in lower for s in (
        'not available in your country',
        'not made this video available in your country',
        'not available from your location',
        'not available in your location',
        'geo-restricted', 'geo restricted', 'geographic restriction',
    ))


def translate_error(text):
    text = re.sub(r'\x1b\[[0-9;]*m', '', str(text))
    lower = text.lower()
    if is_region_restricted(text):
        allowed = 'no Brasil' if 'available in brazil' in lower else 'em um país autorizado pela plataforma'
        return (f'Este vídeo tem restrição de país. O servidor de download precisa estar {allowed}. '
                'A hospedagem atual não conseguiu acessá-lo. Trocar a qualidade ou usar VPN no seu computador não resolve a localização do servidor.')
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
        return 'Não foi possível finalizar ou validar a conversão. Tente novamente ou escolha outro vídeo.'
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
            if candidate.stat().st_size > MAX_BYTES:
                raise RuntimeError('O arquivo final excedeu 500 MB. Escolha uma qualidade ou frequência menor.')
            OUTPUT.mkdir(parents=True, exist_ok=True)
            filename = candidate.name
            final = OUTPUT / filename
            n = 1
            while final.exists():
                final = OUTPUT / f'{candidate.stem} ({n}){candidate.suffix}'
                n += 1
            candidate.replace(final)
            job.update(status='done', percent=100, size=final.stat().st_size, filename=final.name, folder='Downloads/Reel', title=result.get('title') or final.stem, resolution=result.get('resolution') or job.get('resolution', ''), format=result.get('format', request.get('format', 'mp4')), frames_count=result.get('frames_count', 0), duration=result.get('duration', 0))
    except Exception as error:
        with LOCK:
            if not job['cancel']:
                job.update(status='error', error=translate_error(error))
                if is_region_restricted(error):
                    job['error_code'] = 'region_restricted'
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
    # Check every connection as well as the submitted URL, including extractor redirects.
    import ipaddress
    original_resolver = socket.getaddrinfo
    def public_resolver(host, port, *args, **kwargs):
        answers = original_resolver(host, port, *args, **kwargs)
        if any(not ipaddress.ip_address(answer[4][0]).is_global for answer in answers):
            raise ValueError('Use somente endereços públicos da internet.')
        return answers
    socket.getaddrinfo = public_resolver
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
    output_format = request.get('format', 'mp4')
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
        if got > MAX_BYTES:
            raise RuntimeError('O vídeo excede o limite de 500 MB. Escolha uma qualidade menor.')
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
        'format': {'mp3': 'ba/b', 'jpg': 'bv/b'}.get(output_format, 'bv+ba/b'), 'format_sort': sort,
        'max_filesize': MAX_BYTES, 'hls_use_mpegts': False,
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
    opts['merge_output_format'] = 'mkv'
    from yt_dlp.extractor.common import InfoExtractor
    class ReferenceVideoIE(InfoExtractor):
        IE_NAME = 'ReelReference'
        _VALID_URL = r'https?://(?:www\.)?(?:cosmos\.so/e/|savee\.(?:it|com)/(?:i|s)/)(?P<id>[\w-]+)'
        def _real_extract(self, url):
            video_id = self._match_id(url)
            page = self._download_webpage(url, video_id)
            candidates = []
            meta = self._og_search_video_url(page, secure=False, default=None)
            if meta:
                candidates.append(meta)
            # Only structured VideoObject data for this publication; never recommended cards.
            for raw in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', page, re.S | re.I):
                try:
                    data = json.loads(raw)
                except ValueError:
                    continue
                nodes = data if isinstance(data, list) else [data]
                for node in nodes:
                    if not isinstance(node, dict):
                        continue
                    for item in [node, *(node.get('@graph') or [])]:
                        if isinstance(item, dict) and item.get('@type') == 'VideoObject' and item.get('contentUrl'):
                            candidates.append(item['contentUrl'])
            direct = list(dict.fromkeys(x for x in candidates if isinstance(x, str) and re.search(r'\.(?:mp4|webm|mov|m3u8)(?:[?#]|$)', x, re.I)))
            if not direct:
                raise yt_dlp.utils.ExtractorError('Não foi encontrado um arquivo público de vídeo neste item. Use o link original da publicação de origem, se disponível.', expected=True)
            target = validate_url(direct[0])
            formats = self._extract_m3u8_formats(target, video_id, ext='mp4') if '.m3u8' in urlsplit(target).path else [{'url': target}]
            return {'id':video_id, 'title':self._og_search_title(page, default=video_id), 'formats':formats, 'http_headers': {'Referer':url}}

    try:
        # Keep stdout a strict event stream, including when third-party extractors print.
        with yt_dlp.YoutubeDL(opts, auto_init=False) as ydl:
            ydl.add_info_extractor(ReferenceVideoIE())
            ydl.add_default_info_extractors()
            with contextlib.redirect_stdout(sys.stderr):
                info = ydl.extract_info(request['url'], download=False)
                if info and info.get('_type') in ('playlist', 'multi_video'):
                    info = next((i for i in info.get('entries', []) if i), None)
                    if info:
                        info = ydl.process_ie_result(info, download=False)
            if not info or not (info.get('formats') or info.get('url')):
                raise RuntimeError('No video formats available')
            if (info.get('duration') or 0) > 3600:
                raise RuntimeError('Use um vídeo de até 60 minutos para o processamento online.')
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
            if output_format != 'mp3' and quality != 'best' and w and h and min(w, h) > int(quality):
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
        candidates = [p for p in work.iterdir() if p.is_file() and p.suffix.lower() in ('.mp4', '.webm', '.mkv', '.mov', '.m4v', '.avi', '.flv', '.ts', '.3gp', '.m4a', '.mp3', '.opus', '.ogg', '.wav', '.aac')]
        if not candidates:
            raise RuntimeError('Arquivo final não encontrado.')
        final = max(candidates, key=lambda p: p.stat().st_mtime)
        probe = Path(ffmpeg).parent / ('ffprobe.exe' if os.name == 'nt' else 'ffprobe')
        if not probe.exists():
            raise RuntimeError('FFprobe não encontrado. Execute Atualizar para reparar o FFmpeg.')
        emit(event='progress', status='processing', percent=100)
        final, measured, frames_count = finalize_media(final, output_format, ffmpeg, probe, info.get('duration') or 0, request.get('frame_interval', '1'))
        dimensions = next((x for x in measured.get('streams', []) if x.get('codec_type') == 'video'), {})
        actual_w, actual_h = dimensions.get('width'), dimensions.get('height')
        if output_format != 'mp3' and quality != 'best' and actual_w and actual_h and min(actual_w, actual_h) > int(quality):
            raise RuntimeError('Requested format is not available: o arquivo da fonte excede a resolução escolhida. Tente Original.')
        emit(event='result', path=str(final), title=title[0], resolution=f'{actual_w} × {actual_h}' if actual_w and actual_h else '', format=output_format, frames_count=frames_count, duration=media_duration(measured))
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
