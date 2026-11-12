"""Real FFmpeg output verification: A/V, audio-only and numbered JPEG ZIPs."""
import importlib.util,json,subprocess,tempfile,zipfile
from pathlib import Path
root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('reel',root/'companion/server.py')
e=importlib.util.module_from_spec(spec);spec.loader.exec_module(e)
ff='ffmpeg';probe='ffprobe'
def run(*args):subprocess.run([ff,'-v','error','-y',*args],check=True)
with tempfile.TemporaryDirectory() as tmp:
 p=Path(tmp); src=p/'fixture.mkv'
 run('-f','lavfi','-i','testsrc2=size=320x180:rate=12:duration=3','-f','lavfi','-i','sine=frequency=440:duration=3','-c:v','libvpx-vp9','-threads','1','-c:a','libopus','-shortest',str(src))
 for format in ['mp4','mp3','jpg']:
  final,info,count=e.finalize_media(src,format,ff,probe,3,'1')
  assert final.is_file() and final.stat().st_size>0
  if format=='jpg':
   with zipfile.ZipFile(final) as z:
    images=[x for x in z.namelist() if x.endswith('.jpg')]
    assert len(images)==3 and images[0]=='frames/frame_000001.jpg'
    assert z.read(images[0])[:2]==b'\xff\xd8'
  else:
   types={x['codec_type'] for x in info['streams']}
   assert types==({'video','audio'} if format=='mp4' else {'audio'})
   assert e.media_duration(info)>2.9
  print('PASS',format,'valid complete output',count)
 # All original frames are retained, with original timing count.
 allsrc=p/'all.mkv';allsrc.write_bytes(src.read_bytes())
 final,_,count=e.finalize_media(allsrc,'jpg',ff,probe,3,'all');assert count==36,count
 print('PASS all-frames count',count)
 # Missing audio must never be silently delivered as a successful MP4.
 silent=p/'silent.mp4';run('-f','lavfi','-i','color=size=320x180:duration=2','-c:v','libx264','-threads','1',str(silent))
 for format in ['mp4','mp3']:
  try:e.finalize_media(silent,format,ff,probe,2)
  except RuntimeError as error:assert 'áudio' in str(error)
  else:raise AssertionError('silent source was falsely accepted: '+format)
 try:e.finalize_media(src,'mp4',ff,probe,30)
 except RuntimeError as error:assert 'incompleto' in str(error)
 else:raise AssertionError('truncated source was accepted')
 print('PASS missing audio and incomplete duration rejected')
