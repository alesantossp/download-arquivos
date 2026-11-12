import importlib.util
from pathlib import Path
spec=importlib.util.spec_from_file_location('reel',Path(__file__).resolve().parents[1]/'companion/server.py');app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)
cases={"Sign in to confirm you're not a bot. See cookies FAQ":'verification_required','HTTP Error 429: Too Many Requests':'rate_limited','Too Many Requests':'rate_limited','HTTP Error 403: Forbidden; cookies FAQ':'access_denied','Private video. Sign in':'authentication_required','not available in your country':'region_restricted'}
for error,expected in cases.items():
 assert app.error_code(error)==expected,(error,app.error_code(error))
assert 'Isso não confirma' in app.translate_error('Too Many Requests')
assert 'verificação à conexão do servidor' in app.translate_error("Sign in to confirm you're not a bot")
assert app.translate_error('Private video. Sign in').startswith('A plataforma exige login')
print('PASS account login, server verification, rate limiting and HTTP refusal remain distinct')
