"""Real localhost workflow browser fixture; no company connections."""
import json,os,shutil,socket,subprocess,sys,time
from uuid import uuid4
from pathlib import Path
import httpx,psycopg
from psycopg.conninfo import conninfo_to_dict

def run_workflow_browser(back,front,output,env,admin,reader,writer):
    info=conninfo_to_dict(admin)
    combined = env.get('TEST_TASK_RECORDS_ENABLED') == '1'
    preview = env.get('S5_KEEP_PREVIEW') == '1'
    assert not preview or combined, 'Task preview requires the isolated task schema'
    expected_port = '56431' if combined else '56429'
    assert info['host']=='127.0.0.1' and info['port']==expected_port and info['dbname'].startswith('ee_manual_')
    for port in (8109,3109,8419,7219):
        with socket.socket() as sock:sock.bind(('127.0.0.1',port))
    node=shutil.which('node');flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
    project_id=str(uuid4())
    origin='http://127.0.0.1:3109';api='http://127.0.0.1:8109';mock='http://127.0.0.1:8419'
    environment={**env,'PROJECTS_DATABASE_URL':reader,'PROJECTS_WRITE_DATABASE_URL':writer,'BUSINESS_WRITES_ENABLED':'true',
      'PROJECTS_CREATE_SUBJECTS':'engineer01','PROJECTS_WRITE_ORIGINS':origin+',http://127.0.0.1:7219','NEXT_PUBLIC_USE_MOCKS':'false',
      'NEXT_PUBLIC_API_BASE_URL':'/api','BACKEND_URL':api,'NEXT_DIST_DIR':'tmp/workflow-next',
      'WORKFLOW_MOCK_ALLOWED':'1','AGENT_INVOKE_URL':mock+'/api/v1/agent/invoke/stream',
      'AGENT_GENERATION_INVOKE_URL':mock+'/api/v1/agent/invoke','AGENT_API_KEY':'isolated-contract-key',
      'AGENT_CALLBACK_KEY':'isolated-callback-key','DIAGNOSTICS_ENABLED':'true','DIAGNOSTICS_KEY':'isolated-diagnostic-key-for-workflow-tests','DIAGNOSTICS_PROJECT_IDS':project_id,
      'PLAYWRIGHT_MODULE_PATH':env.get('PLAYWRIGHT_MODULE_PATH','C:/Users/zhouguangjian/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'),'PLAYWRIGHT_BROWSER_PATH':env.get('PLAYWRIGHT_BROWSER_PATH','C:/Program Files/Google/Chrome/Application/chrome.exe'),'EE_BASE_URL':origin,'EE_WORKFLOW_OUTPUT':str(output/'browser')}
    environment['EE_TASK_RECORDS_ENABLED'] = '1' if combined else '0'
    environment['NEXT_TELEMETRY_DISABLED'] = '1'
    processes=[];logs=[]
    def fixture_project(name, creation_id):
        with httpx.Client(base_url=api,trust_env=False,timeout=30) as client:
            r=client.post('/v1/auth/login',json={'username':'engineer01','password':env['DEMO_PASSWORD']});assert r.status_code==200,r.text
            client.headers.update({'Origin':origin,'X-Project-Request':'1'})
            def post(path,body):
                r=client.post(path,json=body);assert r.status_code in (200,201),(path,r.status_code,r.text);return r.json()
            project=post('/v1/projects',{'creation_id':creation_id,'name':name})
            p='/v1/projects/'+project['id'];fid=post(p+'/functions',{'creation_id':str(uuid4()),'name':'工作流验收'})['function_id']
            def create(kind,**fields):return post(p+'/functions/'+fid+'/artifacts',{'creation_id':str(uuid4()),'kind':kind,**fields})
            def accept(r):post(p+'/artifacts/'+r['artifact_id']+'/accept',{'mutation_id':str(uuid4()),'revision_id':r['revision_id']})
            prd=create('PRD',name='验收PRD');accept(prd)
            uc=create('UC',name='记录事件',prd_revision_id=prd['revision_id'],description='完整用例正文',main_flow='用户启用记录；系统保存事件');accept(uc)
            fixture={'project':project,'function_id':fid,'prd':prd,'uc':uc}
        return fixture
    try:
        for name,args,cwd in [('agent',[sys.executable,'-m','uvicorn',('tests.prd_creation_mock_agent:app' if combined else 'tests.workflow_mock_agent:app'),'--host','127.0.0.1','--port','8419'],back),
                              ('api',[sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8109'],back)]:
            log=(output/(name+'-workflow.log')).open('wb');logs.append(log)
            processes.append(subprocess.Popen(args,cwd=cwd,env=environment,stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,creationflags=flags))
        for url in (mock+'/health',api+'/health'):
            deadline=time.monotonic()+40
            while True:
                try:
                    if httpx.get(url,timeout=2,trust_env=False).status_code==200:break
                except httpx.TransportError:pass
                if time.monotonic()>deadline:raise RuntimeError('Local workflow service not ready')
                time.sleep(.2)
        fixture=fixture_project('000 工作流联合验收',project_id)
        project=fixture['project'];fid=fixture['function_id'];prd=fixture['prd']
        # Execute the delivered preset script against this explicit isolated project.
        with psycopg.connect(admin) as db:system_id=db.execute('SELECT system_identifier::text FROM pg_control_system()').fetchone()[0]
        psql=front/'tmp/metadata-pg18-r2/pgsql/bin/psql.exe'
        result=subprocess.run([str(psql),'-X','-w','-h','127.0.0.1','-p',expected_port,'-U','postgres','-d',info['dbname'],
            '-v','expected_database='+info['dbname'],'-v','expected_system_id='+system_id,'-v','target_project_id='+project['id'],'-v','actor_subject=engineer01',
            '-f',str(front/'docs/current/database/agent-workflows-r1/init-project-presets.sql')],capture_output=True,env=environment,creationflags=flags)
        (output/'workflow-presets.log').write_bytes(result.stdout+result.stderr);assert result.returncode==0,'Preset SQL failed'
        with psycopg.connect(admin) as db:
            original_catalogue = db.execute('SELECT current_version_id FROM ee.project_subsystem_designs WHERE project_id=%s', (project['id'],)).fetchone()[0]
        repeated = subprocess.run(result.args, capture_output=True, env=environment, creationflags=flags)
        (output/'workflow-presets-repeat-rejected.log').write_bytes(repeated.stdout + repeated.stderr)
        assert repeated.returncode != 0, 'Preset repeated initialization must fail'
        with psycopg.connect(admin) as db:
            assert db.execute('SELECT current_version_id FROM ee.project_subsystem_designs WHERE project_id=%s', (project['id'],)).fetchone()[0] == original_catalogue
            assert db.execute('SELECT count(*) FROM ee.subsystems WHERE project_id=%s', (project['id'],)).fetchone()[0] == 7
        file=output/'workflow-fixture.json';file.write_text(json.dumps(fixture,ensure_ascii=False),encoding='utf-8');environment['EE_WORKFLOW_FIXTURE']=str(file)
        console=back.parent/'TS-agent-console'
        console_env={**environment,'LAB_BACKEND_URL':api,'LAB_FRONTEND_URL':origin,'LAB_PUBLIC_ORIGIN':'http://127.0.0.1:7219',
          'LAB_DEMO_MODE':'false','LAB_COOKIE_SECURE':'false','LAB_ALLOWED_USERS':'engineer01','LAB_ALLOWED_PROJECTS':project_id,
          'LAB_PRESET_PROJECT_ID':project_id,'LAB_PRESET_FUNCTION_ID':fid,'LAB_PRESET_PRD_ID':prd['artifact_id'],'LAB_PRESET_PRD_REVISION_ID':prd['revision_id'],
          'LAB_TRACE_KEY':'isolated-diagnostic-key-for-workflow-tests','LAB_CALLBACK_KEY':'isolated-callback-key','LAB_DATA_DIR':str(output/'console-data')}
        console_log=(output/'console-workflow.log').open('wb');logs.append(console_log)
        processes.append(subprocess.Popen([str(console/'.venv/Scripts/python.exe'),'-m','uvicorn','labconsole.main:app','--host','127.0.0.1','--port','7219'],cwd=console,env=console_env,stdout=console_log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,creationflags=flags))
        log=(output/'web-workflow.log').open('wb');logs.append(log)
        processes.append(subprocess.Popen([node,'node_modules/next/dist/bin/next','dev','--hostname','127.0.0.1','--port','3109'],cwd=front,env=environment,stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,creationflags=flags))
        deadline=time.monotonic()+100
        while True:
            try:
                if httpx.get(origin+'/login',timeout=10,trust_env=False).status_code==200:break
            except httpx.TransportError:pass
            if time.monotonic()>deadline:raise RuntimeError('Local workflow frontend not ready')
            time.sleep(.5)
        if preview:
            # Exercise the same proxy login/read/logout path the user will use.
            with httpx.Client(base_url=origin,trust_env=False,timeout=30) as client:
                assert client.post('/api/v1/auth/login',json={'username':'engineer01','password':env['DEMO_PASSWORD']}).status_code==200
                assert client.get('/api/v1/projects/'+project_id+'/workspace').status_code==200
                assert client.post('/api/v1/auth/logout').status_code==204
            print('BROWSER_REGRESSION_NOT_RUN: local manual preview only.',flush=True)
            print('LOCAL_FIXTURE_LOGIN: engineer01 / change-me-before-demo (localhost only)',flush=True)
            print('LOCAL_PREVIEW_READY: '+origin+'/?project_id='+project_id,flush=True)
            if env.get('S5_PREVIEW_SMOKE')!='1':
                try:
                    while True: time.sleep(1)
                except KeyboardInterrupt:
                    print("Stopping only this preview's owned local processes.",flush=True)
        else:
            suites=[('workflows.mjs','workflow-browser','browser')]
            if combined:
                suites += [('prd-creation.mjs','prd-creation-browser','prd-browser'),
                           ('uc-fr-chat.mjs','uc-fr-chat-browser','uc-fr-chat-browser')]
            for script, log_name, artifact_dir in suites:
                if script!='workflows.mjs':
                    extra=fixture_project('000 '+('PRD自动创建与任务验收' if script=='prd-creation.mjs' else 'UC与FR追加和对话本地验收'),str(uuid4()))
                    fixture_file=output/(log_name+'-fixture.json')
                    fixture_file.write_text(json.dumps(extra,ensure_ascii=False),encoding='utf-8')
                    environment['EE_WORKFLOW_FIXTURE']=str(fixture_file)
                environment['EE_WORKFLOW_OUTPUT']=str(output/artifact_dir)
                with (output/(log_name+'.log')).open('wb') as log:
                    result=subprocess.run([node,'tests/browser/'+script],cwd=front,env=environment,stdout=log,stderr=subprocess.STDOUT,creationflags=flags,timeout=480)
                print((output/(log_name+'.log')).read_text(encoding='utf-8'),flush=True)
                assert result.returncode==0,'Local browser regression failed: '+script
            captured=httpx.get(mock+'/requests',trust_env=False).json()
            (output/'mock-agent-requests.json').write_text(json.dumps(captured,ensure_ascii=False,indent=2),encoding='utf-8')
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                if os.name=='nt':subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,creationflags=flags)
                else:process.terminate()
                process.wait(timeout=20)
        for log in logs:log.close()
