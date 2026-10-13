import builtins,hashlib,importlib.metadata as md,io,json,os,sys
from pathlib import Path
root=Path(__file__).resolve().parent
os.environ.clear(); os.environ.update({'PYTHONDONTWRITEBYTECODE':'1'})
if os.name=='nt':os.environ.update({'SystemRoot':'C:/Windows','WINDIR':'C:/Windows'})
sys.dont_write_bytecode=True; sys.path.insert(0,str(root))
def deny(original):
 def safe(file,*a,**kw):
  if not isinstance(file,int) and Path(file).name.startswith('.env'): raise RuntimeError('ENV_READ_BLOCKED')
  return original(file,*a,**kw)
 return safe
builtins.open=deny(builtins.open); io.open=deny(io.open)
from pydantic_settings.sources import DotEnvSettingsSource
DotEnvSettingsSource._read_env_files=lambda self:{}
import socket,psycopg
socket.create_connection=lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('NETWORK_BLOCKED'))
psycopg.connect=lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('DATABASE_BLOCKED'))
from app.main import app
schema=app.openapi(); raw=json.dumps(schema,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
files={}
for entry in ['app','tools','requirements.txt']:
 p=root/entry
 for f in sorted(p.rglob('*') if p.is_dir() else [p]):
  if not f.is_file() or '__pycache__' in f.parts:continue
  files[f.relative_to(root).as_posix()]=hashlib.sha256(f.read_bytes()).hexdigest()
result=json.loads((root/'release-info.json').read_bytes())
result.update(source_manifest_sha256=hashlib.sha256((root/'source-manifest.json').read_bytes()).hexdigest(),python=sys.version.split()[0],packages={d.metadata['Name']:d.version for d in md.distributions()},files=files,openapi_sha256=hashlib.sha256(raw).hexdigest())
print(json.dumps(result,sort_keys=True))
