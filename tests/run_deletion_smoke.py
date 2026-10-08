"""Real localhost HTTP evidence for stage 2, separate from the running preview."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import psycopg
from psycopg.conninfo import conninfo_to_dict


def run_http_smoke(back: Path, output: Path, env: dict, admin: str, reader: str, writer: str):
    for dsn in (admin, reader, writer):
        info=conninfo_to_dict(dsn)
        assert info.get('host')=='127.0.0.1' and info.get('port')=='56429' and info.get('dbname','').startswith('ee_manual_')
    port=8108
    with socket.socket() as sock:sock.bind(('127.0.0.1',port))
    base='http://127.0.0.1:'+str(port)
    environment={**env,'PROJECTS_DATABASE_URL':reader,'PROJECTS_WRITE_DATABASE_URL':writer,
        'BUSINESS_WRITES_ENABLED':'true','PROJECTS_CREATE_SUBJECTS':'engineer01','PROJECTS_WRITE_ORIGINS':base}
    report={'transport':'real localhost HTTP, no browser','status':'RUNNING','checks':[]}
    flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
    process=None
    with (output/'http-api.log').open('wb') as log:
        try:
            process=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(port)],cwd=back,env=environment,stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,creationflags=flags)
            with httpx.Client(base_url=base,timeout=10) as c:
                deadline=time.monotonic()+30
                while True:
                    try:
                        if c.get('/health').status_code==200:break
                    except httpx.TransportError:pass
                    if process.poll() is not None or time.monotonic()>deadline:raise RuntimeError('Isolated HTTP server did not start')
                    time.sleep(.2)
                def check(method,path,expected=200,**kwargs):
                    r=c.request(method,path,**kwargs)
                    assert r.status_code==expected,(method,path,r.status_code)
                    report['checks'].append({'method':method,'path':path,'status':r.status_code})
                    return r
                for path in ('/health','/docs','/redoc'):check('GET',path)
                schema=check('GET','/openapi.json').json()
                (output/'openapi.json').write_text(json.dumps(schema,ensure_ascii=False,indent=2),encoding='utf8')
                check('GET','/v1/auth/me',401)
                check('POST','/v1/auth/login',json={'username':environment['DEMO_USERNAME'],'password':environment['DEMO_PASSWORD']})
                check('GET','/v1/auth/me')
                c.headers.update({'Origin':base,'X-Project-Request':'1','Content-Type':'application/json'})
                p=check('POST','/v1/projects',201,json={'creation_id':str(uuid4()),'name':'阶段2 HTTP验收项目'}).json()['id']
                f=check('POST',f'/v1/projects/{p}/functions',201,json={'creation_id':str(uuid4()),'name':'阶段2 HTTP验收功能'}).json()['function_id']
                root=f'/v1/projects/{p}/artifacts/'
                create_path=f'/v1/projects/{p}/functions/{f}/artifacts'
                old=check('POST',create_path,201,json={'creation_id':str(uuid4()),'kind':'PRD','name':'第一份PRD'}).json()
                check('POST',root+old['artifact_id']+'/accept',201,json={'mutation_id':str(uuid4()),'revision_id':old['revision_id']})
                uc=check('POST',create_path,201,json={'creation_id':str(uuid4()),'kind':'UC','name':'保护删除的UC','prd_revision_id':old['revision_id']}).json()
                info=check('GET',root+old['artifact_id']+'/deletion-impact').json()
                blocked=check('DELETE',root+old['artifact_id'],409,params={'mutation_id':str(uuid4()),'base_lock_version':info['target']['lock_version']}).json()
                assert info['blocker_count']==1 and blocked['detail']['code']=='deletion_blocked'
                report['blocked_prd']={'impact':info,'delete_response':blocked}
                def remove(path):
                    info=check('GET',path+'/deletion-impact').json()
                    assert not info['blocked']
                    params={'mutation_id':str(uuid4()),'base_lock_version':info['target']['lock_version']}
                    result=check('DELETE',path,params=params).json()
                    assert check('DELETE',path,params=params).json()==result
                    return result
                report['deleted_uc']=remove(root+uc['artifact_id'])
                report['deleted_prd']=remove(root+old['artifact_id'])
                new=check('POST',create_path,201,json={'creation_id':str(uuid4()),'kind':'PRD','name':'全新PRD身份'}).json()
                assert new['artifact_id']!=old['artifact_id']
                ws=check('GET',f'/v1/projects/{p}/workspace').json()
                assert ws['functions'][0]['prd']['id']==new['artifact_id'] and not ws['functions'][0]['can_create_prd']
                with psycopg.connect(admin) as db:
                    archived,accepted=db.execute('SELECT archived_at,accepted_revision_id FROM ee.artifacts WHERE id=%s',(old['artifact_id'],)).fetchone()
                    history=db.execute('SELECT count(*) FROM ee.prd_versions WHERE prd_id=%s',(old['artifact_id'],)).fetchone()[0]
                assert archived and str(accepted)==old['revision_id'] and history==1
                report['recreated_prd']={'old_id':old['artifact_id'],'new_id':new['artifact_id'],'old_history_rows':history,'old_accepted_revision_id':str(accepted),'visible_prd_id':ws['functions'][0]['prd']['id']}
                report['deleted_recreated_prd']=remove(root+new['artifact_id'])
                report['deleted_empty_function']=remove(f'/v1/projects/{p}/functions/{f}')
                # Exercise actual HTTP + cookie/Origin + rw transaction for the stage-4 API.
                bf=check('POST',f'/v1/projects/{p}/functions',201,json={'creation_id':str(uuid4()),'name':'批量HTTP验收'}).json()['function_id']
                ba=check('POST',f'/v1/projects/{p}/functions/{bf}/artifacts',201,json={'creation_id':str(uuid4()),'kind':'FR','description':'批量删除需求'}).json()['artifact_id']
                bp=f'/v1/projects/{p}/deletion-batch'
                preview=check('POST',bp+'/impact',json={'items':[{'id':bf,'kind':'function'}]}).json()
                assert preview['blocked']
                selected=[{'id':bf,'kind':'function'},{'id':ba,'kind':'FR'}]
                preview=check('POST',bp+'/impact',json={'items':selected}).json()
                assert not preview['blocked']
                payload={'mutation_id':str(uuid4()),'items':[{'id':i['target']['id'],'kind':i['target']['kind'],'base_lock_version':i['target']['lock_version']} for i in preview['items']]}
                result=check('POST',bp,json=payload).json()
                assert check('POST',bp,json=payload).json()==result
                assert [i['kind'] for i in result['items']]==['FR','function']
                assert not check('GET',f'/v1/projects/{p}/workspace').json()['functions']
                report['batch_delete']={'impact':preview,'result':result,'replay_equal':True,'workspace_empty':True}
                report['deleted_empty_project']=remove(f'/v1/projects/{p}')
                check('GET',f'/v1/projects/{p}',404)
                check('POST','/v1/auth/logout',204)
                check('GET','/v1/auth/me',401)
                report['status']='PASS'
        finally:
            if process and process.poll() is None:
                process.terminate();process.wait(timeout=15)
            report['http_process_stopped']=process is None or process.poll() is not None
            (output/'http-smoke.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
