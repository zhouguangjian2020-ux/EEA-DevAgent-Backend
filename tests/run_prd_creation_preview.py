# Local-only fixture; owned processes are cleaned up in finally.
import json,os,shutil,socket,subprocess,sys,time
from uuid import uuid4
import httpx
from psycopg.conninfo import conninfo_to_dict

def run_preview(back,front,output,env,admin,reader,writer):
    info=conninfo_to_dict(admin)
    expected_port = '56431' if os.getenv('S5_COMBINED') == '1' else '56429'
    assert info['host']=='127.0.0.1' and info['port']==expected_port and info['dbname'].startswith('ee_manual_')
    for port in (3109,8109,8419):
        with socket.socket() as sock:sock.bind(('127.0.0.1',port))
    node=shutil.which('node');assert node
    origin='http://127.0.0.1:3109';api='http://127.0.0.1:8109';mock='http://127.0.0.1:8419'
    e={**env,'PROJECTS_DATABASE_URL':reader,'PROJECTS_WRITE_DATABASE_URL':writer,'BUSINESS_WRITES_ENABLED':'true','PROJECTS_CREATE_SUBJECTS':'engineer01','PROJECTS_WRITE_ORIGINS':origin,'CORS_ORIGINS':origin,'NEXT_PUBLIC_USE_MOCKS':'false','NEXT_PUBLIC_API_BASE_URL':'/api','BACKEND_URL':api,'NEXT_DIST_DIR':'tmp/prd-creation-s3-next','WORKFLOW_MOCK_ALLOWED':'1','AGENT_INVOKE_URL':mock+'/api/v1/agent/invoke/stream','AGENT_GENERATION_INVOKE_URL':mock+'/api/v1/agent/invoke','AGENT_API_KEY':'isolated-contract-key','AGENT_CALLBACK_KEY':'isolated-callback-key','DIAGNOSTICS_ENABLED':'false','EE_BASE_URL':origin,'EE_WORKFLOW_OUTPUT':str(output/'prd-creation-browser'),'PLAYWRIGHT_MODULE_PATH':'C:/Users/zhouguangjian/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright','PLAYWRIGHT_BROWSER_PATH':'C:/Program Files/Google/Chrome/Application/chrome.exe'}
    e['NEXT_TELEMETRY_DISABLED']='1'
    children=[];logs=[];flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
    def launch(name,args,cwd):
        log=(output/(name+'.log')).open('wb');logs.append(log)
        children.append(subprocess.Popen(args,cwd=cwd,env=e,stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,creationflags=flags))
    def wait(url):
        end=time.monotonic()+120
        while time.monotonic()<end:
            try:
                if httpx.get(url,timeout=10,trust_env=False).status_code==200:return
            except httpx.TransportError:pass
            if any(c.poll() is not None for c in children):raise RuntimeError('Local child exited; inspect local logs')
            time.sleep(.5)
        raise RuntimeError('Local service readiness timed out')
    def fixture_project(name,filename):
        with httpx.Client(base_url=api,trust_env=False,timeout=20) as client:
            login=client.post('/v1/auth/login',json={'username':'engineer01','password':e['DEMO_PASSWORD']});assert login.status_code==200,login.text
            client.headers.update({'Origin':origin,'X-Project-Request':'1'})
            def post(url,body):
                result=client.post(url,json=body);assert result.status_code in (200,201),(url,result.status_code,result.text);return result.json()
            project=post('/v1/projects',{'creation_id':str(uuid4()),'name':name})
            prefix='/v1/projects/'+project['id']
            fid=post(prefix+'/functions',{'creation_id':str(uuid4()),'name':'工作流验收'})['function_id']
            def create(kind,**fields):return post(prefix+'/functions/'+fid+'/artifacts',{'kind':kind,'creation_id':str(uuid4()),**fields})
            def accept(result):post(prefix+'/artifacts/'+result['artifact_id']+'/accept',{'mutation_id':str(uuid4()),'revision_id':result['revision_id']})
            prd=create('PRD',name='验收PRD');accept(prd)
            uc=create('UC',name='记录事件',prd_revision_id=prd['revision_id'],description='完整用例正文',main_flow='用户启用记录；系统保存事件');accept(uc)
        fixture={'project':project,'function_id':fid,'prd':prd,'uc':uc}
        file=output/filename;file.write_text(json.dumps(fixture,ensure_ascii=False),encoding='utf-8');e['EE_WORKFLOW_FIXTURE']=str(file)
        return fixture
    try:
        launch('agent',[sys.executable,'-m','uvicorn','tests.prd_creation_mock_agent:app','--host','127.0.0.1','--port','8419'],back)
        launch('api',[sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8109'],back)
        wait(mock+'/health');wait(api+'/health')
        fixture=fixture_project('000 PRD自动创建S3本地验收','prd-creation-fixture.json')
        project=fixture['project']
        launch('web',[node,'node_modules/next/dist/bin/next','dev','--hostname','127.0.0.1','--port','3109'],front)
        wait(origin+'/login')
        if os.getenv('UC_CHAT_SKIP_BROWSER')=='1':
            print('BROWSER_REGRESSION_NOT_RUN: manual local preview remains available.',flush=True)
        else:
            with (output/'prd-creation-browser.log').open('wb') as log:
                result=subprocess.run([node,'tests/browser/prd-creation.mjs'],cwd=front,env=e,stdout=log,stderr=subprocess.STDOUT,creationflags=flags,timeout=480)
            print((output/'prd-creation-browser.log').read_text(encoding='utf-8'),flush=True)
            assert result.returncode==0,'Local browser acceptance failed'
            # Reuse the same owned localhost services, with a new independent project.
            # This avoids rebinding ports while Windows is still closing their old sockets.
            fixture_project('000 UC与FR追加和对话本地验收','workflow-fixture.json')
            e['EE_WORKFLOW_OUTPUT']=str(output/'browser')
            with (output/'uc-fr-chat-browser.log').open('wb') as log:
                result=subprocess.run([node,'tests/browser/uc-fr-chat.mjs'],cwd=front,env=e,stdout=log,stderr=subprocess.STDOUT,creationflags=flags,timeout=300)
            print((output/'uc-fr-chat-browser.log').read_text(encoding='utf-8'),flush=True)
            assert result.returncode==0,'Existing UC/FR/chat browser regression failed'

        (output/'prd-creation-agent-requests.json').write_text(json.dumps(httpx.get(mock+'/requests',trust_env=False).json(),ensure_ascii=False,indent=2),encoding='utf-8')
        if os.getenv('UC_CHAT_KEEP_PREVIEW')=='1':
            assert e['APP_ENV']=='development' and e['DEMO_USERNAME']=='engineer01'
            assert e['DEMO_PASSWORD']=='change-me-before-demo'
            print('LOCAL_FIXTURE_LOGIN: engineer01 / change-me-before-demo (localhost only)',flush=True)
            print('LOCAL_PREVIEW_READY: '+origin+'/?project_id='+project['id'],flush=True)
            print('Local contract Mock only. Keep this process open. Ctrl+C stops owned services and isolated database.',flush=True)
            if os.getenv('PRD_PREVIEW_SMOKE_EXIT') == '1':
                # Verify the actual quick-start branch without leaving an orphaned local service.
                with httpx.Client(base_url=origin,trust_env=False,timeout=20) as browser_client:
                    for endpoint in ('/api/health','/api/docs','/api/redoc'):
                        assert browser_client.get(endpoint).status_code == 200
                    assert browser_client.post('/api/v1/auth/login',json={'username':'engineer01','password':e['DEMO_PASSWORD']}).status_code == 200
                    assert browser_client.get('/api/v1/auth/me').status_code == 200
                    assert browser_client.get('/api/v1/projects/'+project['id']+'/workspace').status_code == 200
                    assert browser_client.post('/api/v1/auth/logout').status_code == 204
                print('QUICK_PREVIEW_HTTP_SMOKE_OK: frontend proxy login/read/logout; owned services will stop.',flush=True)
                return

            try:
                while True:time.sleep(1)
            except KeyboardInterrupt:print('Stopping only owned preview services.',flush=True)
    finally:
        for child in reversed(children):
            if child.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],capture_output=True,creationflags=flags)
                else:
                    child.terminate()
                try:child.wait(timeout=12)
                except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
        for log in logs:log.close()
