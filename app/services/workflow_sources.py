"""Trusted complete snapshots; never truncate UC branches or trust browser bodies."""
from contextlib import contextmanager
from uuid import UUID
import hashlib, json
import psycopg
from app.core.database import project_connection, ProjectDatabaseNotConfigured, UnsafeProjectDatabaseRole
from app.services.projects import ProjectReadError, _SELECT
from app.services.project_read_errors import database_read_error

def fail(status, code, message):
    raise ProjectReadError(status, code, message)

@contextmanager
def read_db():
    try:
        with project_connection(repeatable_read=True) as db:
            yield db
    except ProjectReadError:
        raise
    except (psycopg.Error, ProjectDatabaseNotConfigured, UnsafeProjectDatabaseRole) as error:
        raise database_read_error(error) from error

def visible(db, subject, project_id):
    if not db.execute(_SELECT + ' AND p.id=%s', (subject, project_id)).fetchone():
        fail(404, 'project_not_found', '项目不存在或无权访问')
    return db.execute("SELECT id FROM ee.app_users WHERE external_subject=%s AND state='active'", (subject,)).fetchone()['id']

def allowed(db, subject, project_id, function_id, capability):
    visible(db, subject, project_id)
    row = db.execute("""SELECT 1 FROM ee.app_users u
        JOIN ee.project_members m ON m.user_id=u.id AND m.project_id=%s AND m.state='active'
        JOIN ee.access_bindings b ON b.user_id=u.id AND b.project_id=m.project_id
        JOIN ee.access_roles r ON r.id=b.role_id AND r.enabled
        JOIN ee.role_permissions rp ON rp.role_id=r.id
        JOIN ee.permission_definitions pd ON pd.code=rp.permission_code AND pd.enabled
        WHERE u.external_subject=%s AND u.state='active'
        AND pd.resource_kind||'.'||pd.action=%s
        AND ((b.scope_kind='project' AND 'project'=ANY(pd.allowed_scopes))
         OR (b.scope_kind='function' AND b.function_id=%s AND 'function'=ANY(pd.allowed_scopes)))
        LIMIT 1""", (project_id, subject, capability, function_id)).fetchone()
    if not row:
        fail(403, 'workflow_forbidden', '当前账号没有此功能的操作权限')

def object_content(db, project_id, artifact_id, revision_id=None):
    a = db.execute("""SELECT a.* FROM ee.artifacts a JOIN ee.business_functions f ON f.id=a.function_id
        WHERE a.id=%s AND a.project_id=%s AND a.archived_at IS NULL AND f.archived_at IS NULL""", (artifact_id, project_id)).fetchone()
    if not a or a['type_code'] not in ('PRD','UC','FR'):
        fail(404, 'artifact_not_found', '对象不存在或已删除')
    rev = revision_id or a['head_revision_id']
    r = db.execute('SELECT * FROM ee.artifact_revisions WHERE id=%s AND artifact_id=%s AND project_id=%s', (rev,artifact_id,project_id)).fetchone()
    if not r:
        fail(409,'source_changed','对象修订不存在，请刷新来源')
    kind=a['type_code']
    if kind == 'PRD':
        v=db.execute('SELECT markdown_body FROM ee.prd_versions WHERE revision_id=%s',(rev,)).fetchone()
        content={'kind':kind,**v}
    elif kind == 'FR':
        v=db.execute('SELECT description FROM ee.fr_versions WHERE revision_id=%s',(rev,)).fetchone()
        sources=db.execute('SELECT uc_id,uc_revision_id FROM ee.fr_uc_sources WHERE fr_revision_id=%s ORDER BY uc_id',(rev,)).fetchall()
        content={'kind':kind,**v,'acceptance_criteria':r['metadata'].get('acceptance_criteria',''),'sources':sources}
    else:
        v=db.execute('SELECT * FROM ee.uc_versions WHERE revision_id=%s',(rev,)).fetchone()
        cases=db.execute('SELECT kind,body FROM ee.uc_scenario_items WHERE uc_revision_id=%s ORDER BY kind,position,item_id',(rev,)).fetchall()
        mapping={'actors':'actors_text','extensions':'extensions_text','hmi':'hmi_logic'}
        fields=['name','description','actors','preconditions','main_flow','postconditions','extensions','hmi','regulations','performance','nonfunctional']
        content={'kind':kind,'prd_revision_id':v['prd_revision_id'],**{x:v[mapping.get(x,x)] or '' for x in fields},
            'branches':[x['body'] for x in cases if x['kind']=='branch'],
            'exceptions':[x['body'] for x in cases if x['kind']=='exception']}
    return {'artifact_id':a['id'],'function_id':a['function_id'],'revision_id':rev,
            'head_revision_id':a['head_revision_id'],'accepted_revision_id':a['accepted_revision_id'],
            'number':a['code'],'status':'accepted' if a['accepted_revision_id']==rev else 'pending','content':content}

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()

def generation_context(subject, project_id, function_id, kind):
    with read_db() as db:
        return context_in_db(db,subject,project_id,function_id,kind)

def context_in_db(db,subject,project_id,function_id,kind):
    allowed(db,subject,project_id,function_id,'FR.create' if kind=='UC' else 'FR_ALLOCATION.create')
    if not db.execute('SELECT 1 FROM ee.business_functions WHERE id=%s AND project_id=%s AND archived_at IS NULL',(function_id,project_id)).fetchone():
        fail(404,'function_not_found','功能不存在或无权访问')
    rows=db.execute("""SELECT id,head_revision_id,accepted_revision_id FROM ee.artifacts
        WHERE project_id=%s AND function_id=%s AND type_code=%s AND archived_at IS NULL ORDER BY id""",(project_id,function_id,kind)).fetchall()
    if not rows or any(not x['accepted_revision_id'] or x['head_revision_id']!=x['accepted_revision_id'] for x in rows):
        fail(409,'sources_not_confirmed','当前功能全部有效来源必须先确认，且至少有一个来源')
    sources=[object_content(db,project_id,x['id'],x['accepted_revision_id']) for x in rows]
    catalogue=[]
    if kind=='FR':
        catalogue=db.execute("""SELECT i.subsystem_id,i.name,i.description,i.design_version_id
            FROM ee.project_subsystem_designs d JOIN ee.subsystem_version_items i
            ON i.project_id=d.project_id AND i.design_version_id=d.current_version_id
            WHERE d.project_id=%s AND i.is_allocatable ORDER BY i.subsystem_id""",(project_id,)).fetchall()
        if not catalogue:fail(409,'subsystem_catalogue_empty','请先初始化项目预置子系统目录')
    result={'project_id':str(project_id),'function_id':str(function_id),'sources':sources,'subsystems':catalogue}
    return json.loads(json.dumps(result,default=str))
