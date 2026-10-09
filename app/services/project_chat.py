"""Private per-user/project threads with durable, idempotent turns."""
import json
from uuid import uuid4
from psycopg.types.json import Jsonb
from app.services.manual_work import write_transaction
from app.services import task_timing
from app.services.workflow_sources import read_db, visible, object_content, digest, fail, allowed

def owned(db,subject,project_id,thread_id,lock=False):
    actor=visible(db,subject,project_id)
    t=db.execute('SELECT * FROM ee.chat_threads WHERE id=%s AND project_id=%s AND user_id=%s'+(' FOR UPDATE' if lock else ''),(thread_id,project_id,actor)).fetchone()
    if not t:fail(404,'thread_not_found','会话不存在或不属于当前用户与项目')
    return t

def first_question_title(prompt, fallback='新会话'):
    # Keep the creation title unchanged for create-request idempotency.
    text=' '.join(prompt.split()) if prompt else ''
    return text[:120] or fallback

def list_threads(subject,project_id,offset=0):
    with read_db() as db:
        actor=visible(db,subject,project_id)
        rows=db.execute('SELECT id,title,created_at,updated_at FROM ee.chat_threads WHERE project_id=%s AND user_id=%s ORDER BY updated_at DESC,id LIMIT 51 OFFSET %s',(project_id,actor,offset)).fetchall()
        if not rows:return rows
        first=db.execute('SELECT DISTINCT ON (thread_id) thread_id,prompt FROM ee.chat_turns WHERE thread_id=ANY(%s) ORDER BY thread_id,created_at,id',([row['id'] for row in rows],)).fetchall()
        prompts={turn['thread_id']:turn['prompt'] for turn in first}
        return [{**row,'title':first_question_title(prompts.get(row['id']),row['title'])} for row in rows]

def create_thread(subject,project_id,payload):
    with write_transaction() as db:
        actor=visible(db,subject,project_id)
        db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(str(project_id)+str(actor)+str(payload.client_request_id),))
        t=db.execute('SELECT * FROM ee.chat_threads WHERE project_id=%s AND user_id=%s AND client_request_id=%s',(project_id,actor,payload.client_request_id)).fetchone()
        if t:
            if t['title']!=payload.title:fail(409,'request_conflict','创建标识已绑定其他会话标题')
            return t
        return db.execute('INSERT INTO ee.chat_threads(id,project_id,user_id,client_request_id,title) VALUES(%s,%s,%s,%s,%s) RETURNING *',(uuid4(),project_id,actor,payload.client_request_id,payload.title)).fetchone()

def history(subject,project_id,thread_id):
    with read_db() as db:
        t=owned(db,subject,project_id,thread_id)
        rows=db.execute("""SELECT client_request_id,prompt,body,status,attachments,created_at,updated_at,
            to_jsonb(t)->'duration_ms' AS duration_ms FROM ee.chat_turns t
            WHERE thread_id=%s ORDER BY created_at,id LIMIT 101""",(thread_id,)).fetchall()
        rows=[{**row,'duration_ms':task_timing.duration_value(row.get('duration_ms'),row.get('status',''))} for row in rows]
        return {'thread':{**t,'title':first_question_title(rows[0]['prompt'] if rows else None,t['title'])},'turns':rows}

def start_turn(subject,project_id,thread_id,payload):
    with write_transaction() as db:
        owned(db,subject,project_id,thread_id,True)
        db.execute("UPDATE ee.chat_turns SET status='interrupted',updated_at=now() WHERE thread_id=%s AND status='running' AND updated_at<now()-interval '15 minutes'",(thread_id,))
        fp=digest(payload.model_dump(mode='json',exclude={'retry'} | (set() if payload.auto_intent else {'auto_intent'})))
        previous=db.execute('SELECT * FROM ee.chat_turns WHERE thread_id=%s AND client_request_id=%s',(thread_id,payload.client_request_id)).fetchone()
        if previous:
            if previous['request_hash']!=fp:fail(409,'request_conflict','原请求标识已绑定其他输入')
            if previous['status']=='running':fail(409,'turn_running','原请求仍在执行，请勿重复发送')
            if previous['status']=='completed' or not payload.retry:return {'replay':True,'turn':previous}
        # A crashed/abandoned stream is recorded honestly; no implicit Agent retry.
        if db.execute("SELECT 1 FROM ee.chat_turns WHERE thread_id=%s AND status='running'",(thread_id,)).fetchone():fail(409,'thread_busy','当前会话已有请求执行中')
        turns=db.execute('SELECT prompt,body,status,attachments FROM ee.chat_turns WHERE thread_id=%s AND client_request_id<>%s ORDER BY created_at,id LIMIT 101',(thread_id,payload.client_request_id)).fetchall()
        if len(turns)>=100:fail(413,'conversation_full','此会话已达到100轮，请新建会话；历史仍保留')
        attachments=[]
        for ref in payload.attachments:
            a=object_content(db,project_id,ref.artifact_id,ref.revision_id)
            if a['head_revision_id']!=ref.revision_id:fail(409,'source_changed','附件已有新版本，请重新添加')
            if payload.mode=='modify':allowed(db,subject,project_id,a['function_id'],a['content']['kind']+'.update')
            attachments.append(json.loads(json.dumps(a,default=str)))
        messages=[]
        for turn in turns:
            messages.append({'role':'user','content':turn['prompt'],'attachments':turn['attachments']})
            if turn['body']:messages.append({'role':'assistant','content':turn['body'],'status':turn['status']})
        from app.services.chat_tasks import context_in_db
        context={'project_id':str(project_id),'thread_id':str(thread_id),'messages':messages,
                 'attachments':attachments,'task_receipts':context_in_db(db,subject,project_id,thread_id)}
        if len(json.dumps(context,ensure_ascii=False).encode())>1_500_000:fail(413,'context_too_large','会话上下文过大，请新建会话；未截断历史')
        enabled = task_timing.has_columns(previous) if previous else task_timing.schema_available(db, 'ee.chat_turns')
        timing = task_timing.new_attempt() if enabled else {}
        if previous:
            clause, values = task_timing.update_part(timing)
            db.execute(f"UPDATE ee.chat_turns SET status='running',body='',updated_at=now(){clause} WHERE thread_id=%s AND client_request_id=%s",
                       (*values,thread_id,payload.client_request_id))
        else:
            columns, placeholders, values = task_timing.insert_parts(timing)
            db.execute(f"INSERT INTO ee.chat_turns(id,thread_id,client_request_id,request_hash,prompt,mode,attachments,status{columns}) VALUES(%s,%s,%s,%s,%s,%s,%s,'running'{placeholders})",
                       (uuid4(),thread_id,payload.client_request_id,fp,payload.message,payload.mode,Jsonb(attachments),*values))
        db.execute('UPDATE ee.chat_threads SET updated_at=now() WHERE id=%s',(thread_id,))
        return {'replay':False,'context':context,'attempt_id':timing.get('attempt_id')}

def finish_turn(subject,project_id,thread_id,client_request_id,body,status,*,expected_attempt_id=None):
    if status not in {'completed','failed','interrupted'}:
        raise ValueError('Unsupported terminal turn status')
    with write_transaction() as db:
        owned(db,subject,project_id,thread_id,True)
        row=db.execute('SELECT * FROM ee.chat_turns WHERE thread_id=%s AND client_request_id=%s FOR UPDATE',
                       (thread_id,client_request_id)).fetchone()
        if not row or row.get('attempt_id') != expected_attempt_id:
            fail(409,'turn_observation_stale','较早请求不能覆盖当前重试的回复')
        if row['status'] != 'running':
            if row['status'] == status and row['body'] == body:
                return {'duration_ms':task_timing.duration_value(row.get('duration_ms'),status)}
            fail(409,'turn_observation_stale','原请求已结束，不能覆盖其回复')
        timing=task_timing.completion(row,task_timing.utcnow(),expected_attempt_id) if status!='interrupted' else {}
        clause,values=task_timing.update_part(timing)
        changed=db.execute(f"UPDATE ee.chat_turns SET body=%s,status=%s,updated_at=now(){clause} WHERE thread_id=%s AND client_request_id=%s AND status='running' RETURNING *",
                           (body,status,*values,thread_id,client_request_id)).fetchone()
        if not changed:
            fail(409,'turn_observation_stale','原请求状态已改变，不能标记完成')
        db.execute('UPDATE ee.chat_threads SET updated_at=now() WHERE id=%s',(thread_id,))
        return {'duration_ms':task_timing.duration_value(changed.get('duration_ms'),status)}


def authorize_modifications(subject,project_id,attachments):
    if not attachments:
        fail(422,'modification_attachment_required','请先从工作空间添加需要修改的对象；不会自动使用当前标签或历史附件')
    with read_db() as db:
        for a in attachments:
            from uuid import UUID
            current=object_content(db,project_id,UUID(a['artifact_id']),UUID(a['revision_id']))
            if str(current['head_revision_id'])!=a['revision_id']:
                fail(409,'source_changed','附件已有新版本，请重新添加后再修改')
            allowed(db,subject,project_id,current['function_id'],current['content']['kind']+'.update')
