"""Regression for YouTube's reported Brazil-only response."""
import importlib.util
from pathlib import Path
import tomllib

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('reel_region', root / 'companion/server.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
error = ('[youtube] dABOAonHlR8: The uploader has not made this video available in your country '
         'This video is available in Brazil. You might want to use a VPN or a proxy server (with --proxy) to workaround.')
assert app.is_region_restricted(error)
message = app.translate_error(error)
assert 'no Brasil' in message and '--proxy' not in message
assert 'Trocar a qualidade' in message
assert app.is_region_restricted('HTTP Error 403: Video is geo-restricted')
assert not app.is_region_restricted('HTTP Error 403: Forbidden')
assert not app.is_region_restricted('Sign in to confirm you are not a bot')
assert 'login' in app.translate_error('Sign in to confirm you are not a bot')
assert 'bloqueou' in app.translate_error('HTTP Error 403: Forbidden')
config = tomllib.loads((root / 'fly.toml').read_text())
assert config['primary_region'] == 'gru'
assert config['http_service']['auto_stop_machines'] == 'off'
assert config['http_service']['internal_port'] == 10000
assert config['build']['dockerfile'] == 'cloud/Dockerfile'
print('PASS Brazil-only error classification, other platform errors and Brazil deployment template')
