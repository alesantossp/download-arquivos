"""Exercise the actual yt-dlp worker on controlled local media, including separate streams."""
import json,subprocess,tempfile,threading,sys
from pathlib import Path
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as tmp:
 p=Path(tmp);fixtures=p/'fixtures';fixtures.mkdir()
 subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=320x180:rate=12:duration=3','-f','lavfi','-i','sine=duration=3','-c:v','libx264','-threads','1','-c:a','aac','-shortest',str(fixtures/'clip.mp4')],check=True)
 subprocess.run(['ffmpeg','-v','error','-y','-i',str(fixtures/'clip.mp4'),'-c','copy','-f','dash',str(fixtures/'split.mpd')],check=True)
 class Handler(SimpleHTTPRequestHandler):
  def __init__(self,*args,**kw):super().__init__(*args,directory=str(fixtures),**kw)
  def log_message(self,*args):pass
 http=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=http.serve_forever,daemon=True).start()
 try:
  # Test-only source injection permits this loopback fixture; production stays unchanged.
  source=(root/'companion/server.py').read_text().replace('    socket.getaddrinfo = public_resolver','    pass # test-only loopback fixture')
  code="import sys; sys.path.insert(0,"+repr(str(root/'companion'))+"); namespace={'__name__':'fixture_engine','__file__':"+repr(str(root/'companion/server.py'))+"}; exec("+repr(source)+",namespace); namespace['validate_url']=lambda value:value; namespace['worker']()"
  for fmt,path in [('mp4','split.mpd'),('mp3','split.mpd'),('jpg','clip.mp4')]:
   work=p/('work-'+fmt)
   result=subprocess.run([sys.executable,'-c',code],input=json.dumps({'url':f'http://127.0.0.1:{http.server_port}/{path}','quality':'best','format':fmt,'frame_interval':'1','work':str(work)})+'\n',text=True,capture_output=True,timeout=90)
   events=[json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
   final=next((x for x in events if x.get('event')=='result'),None)
   assert result.returncode==0 and final,(fmt,result.stderr,events)
   assert Path(final['path']).suffix==('.zip' if fmt=='jpg' else '.'+fmt),final
   print('PASS full worker',fmt,'with',path,'duration',final['duration'])
 finally:http.shutdown()
