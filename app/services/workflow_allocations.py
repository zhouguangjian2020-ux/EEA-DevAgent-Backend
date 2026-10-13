"""Versioned FR allocations: explicit save/accept only."""
from psycopg.types.json import Jsonb
from app.schemas.manual_work import WriteResult
from app.services.manual_work import write_transaction, conflict, fingerprint, lock_request, feature, type_version, current_source, audit
from app.services.resource_locks import lock_project
from app.services.business_access import authorize

def save_allocation(subject,project_id,function_id,payload,request_id):
    with write_transaction() as db:
        return save_allocation_in_db(db,subject,project_id,function_id,payload,request_id)

def save_allocation_in_db(db,subject,project_id,function_id,payload,request_id,*,source_digest_cache=None):
    lock_project(db,project_id);feature(db,project_id,function_id)
    actor=authorize(db,subject,project_id,'FR_ALLOCATION.update' if payload.base_revision_id else 'FR_ALLOCATION.create',function_id=function_id)
    lock_request(db,payload.mutation_id)
    digest=fingerprint(project_id,function_id,payload)
    old=db.execute('SELECT * FROM ee.artifact_revisions WHERE id=%s',(payload.mutation_id,)).fetchone()
    if old:
        if old['artifact_id']!=payload.allocation_id or old['created_by']!=actor or old['metadata'].get('manual_mutation_hash')!=digest:conflict('保存标识已使用')
        return WriteResult(function_id=function_id,artifact_id=payload.allocation_id,revision_id=payload.mutation_id,created=False)
    if payload.source_version is not None:
        from app.services.workflow_sources import context_in_db, digest as source_digest
        key = (subject, project_id, function_id)
        actual_digest = source_digest_cache.get(key) if source_digest_cache is not None else None
        if actual_digest is None:
            actual_digest = source_digest(context_in_db(db,subject,project_id,function_id,'FR'))
            if source_digest_cache is not None:
                source_digest_cache[key] = actual_digest
        if actual_digest != payload.source_version:
            conflict('生成来源或子系统目录已变化，请重新生成')
    current_source(db,project_id,function_id,'FR',payload.fr_revision_id,payload.fr_id)
    fr=db.execute('SELECT head_revision_id FROM ee.artifacts WHERE id=%s',(payload.fr_id,)).fetchone()
    if fr['head_revision_id']!=payload.fr_revision_id:conflict('FR有新的未确认修订，请核对来源')
    design=db.execute('SELECT current_version_id FROM ee.project_subsystem_designs WHERE project_id=%s FOR SHARE',(project_id,)).fetchone()
    if not design or design['current_version_id']!=payload.design_version_id:conflict('子系统目录版本已变化')
    valid={r['subsystem_id'] for r in db.execute('SELECT subsystem_id FROM ee.subsystem_version_items WHERE project_id=%s AND design_version_id=%s AND is_allocatable',(project_id,payload.design_version_id)).fetchall()}
    if not {x.subsystem_id for x in payload.targets}<=valid:conflict('目标子系统不存在或不可分配')
    a=db.execute('SELECT * FROM ee.artifacts WHERE id=%s FOR UPDATE',(payload.allocation_id,)).fetchone()
    if a:
        if a['project_id']!=project_id or a['function_id']!=function_id or a['type_code']!='FR_ALLOCATION' or a['archived_at'] or a['head_revision_id']!=payload.base_revision_id:conflict('分配已变化，请保留输入并刷新')
        relation=db.execute('SELECT fr_id FROM ee.fr_allocations WHERE artifact_id=%s',(a['id'],)).fetchone()
        if not relation or relation['fr_id']!=payload.fr_id:conflict('分配不能更换所属FR')
        base=db.execute('SELECT revision_no,type_version_id FROM ee.artifact_revisions WHERE id=%s',(a['head_revision_id'],)).fetchone()
        number=base['revision_no']+1;typ=base['type_version_id']
    else:
        if payload.base_revision_id:conflict('分配不存在')
        if db.execute('SELECT 1 FROM ee.fr_allocations WHERE project_id=%s AND function_id=%s AND fr_id=%s',(project_id,function_id,payload.fr_id)).fetchone():conflict('此FR已有分配（可能已归档）；活动分配请刷新编辑，归档记录本轮不提供恢复或重建')
        number=1;typ=type_version(db,'FR_ALLOCATION')
        db.execute("""INSERT INTO ee.artifacts(id,project_id,function_id,type_code,code,head_revision_id,created_by,metadata)
            VALUES(%s,%s,%s,'FR_ALLOCATION',%s,%s,%s,%s)""",(payload.allocation_id,project_id,function_id,'ALLOC-'+payload.allocation_id.hex[:12].upper(),payload.mutation_id,actor,Jsonb({'manual_creation_hash':digest})))
        db.execute('INSERT INTO ee.fr_allocations(artifact_id,project_id,function_id,fr_id) VALUES(%s,%s,%s,%s)',(payload.allocation_id,project_id,function_id,payload.fr_id))
    db.execute("""INSERT INTO ee.artifact_revisions(id,project_id,function_id,artifact_id,type_code,revision_no,base_revision_id,type_version_id,created_by,metadata)
        VALUES(%s,%s,%s,%s,'FR_ALLOCATION',%s,%s,%s,%s,%s)""",(payload.mutation_id,project_id,function_id,payload.allocation_id,number,payload.base_revision_id,typ,actor,Jsonb({'manual_mutation_hash':digest})))
    db.execute('INSERT INTO ee.allocation_versions(revision_id,project_id,function_id,allocation_id,fr_id,fr_revision_id,design_version_id,note) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',(payload.mutation_id,project_id,function_id,payload.allocation_id,payload.fr_id,payload.fr_revision_id,payload.design_version_id,payload.note))
    for target in payload.targets:
        db.execute('INSERT INTO ee.allocation_targets(project_id,function_id,allocation_revision_id,design_version_id,subsystem_id,rationale) VALUES(%s,%s,%s,%s,%s,%s)',(project_id,function_id,payload.mutation_id,payload.design_version_id,target.subsystem_id,target.rationale))
    db.execute('UPDATE ee.artifacts SET head_revision_id=%s WHERE id=%s',(payload.mutation_id,payload.allocation_id))
    audit(db,project_id,actor,'allocation.save',request_id,function_id,payload.allocation_id)
    return WriteResult(function_id=function_id,artifact_id=payload.allocation_id,revision_id=payload.mutation_id)

def accept_allocation(subject,project_id,allocation_id,payload,request_id):
    with write_transaction() as db:
        return accept_allocation_in_db(db,subject,project_id,allocation_id,payload,request_id)

def accept_allocation_in_db(db,subject,project_id,allocation_id,payload,request_id,*,expected_function=None):
    lock_project(db,project_id)
    a=db.execute("SELECT * FROM ee.artifacts WHERE id=%s AND project_id=%s AND type_code='FR_ALLOCATION' AND archived_at IS NULL FOR UPDATE",(allocation_id,project_id)).fetchone()
    if not a:conflict('分配不存在')
    if expected_function is not None and a['function_id'] != expected_function:conflict('分配不属于当前功能')
    feature(db,project_id,a['function_id'])
    actor=authorize(db,subject,project_id,'FR_ALLOCATION.accept',function_id=a['function_id'])
    old=db.execute('SELECT revision_id,actor_id FROM ee.review_events WHERE artifact_id=%s AND request_key=%s',(allocation_id,str(payload.mutation_id))).fetchone()
    if old:
        if old['revision_id']!=payload.revision_id or old['actor_id']!=actor:conflict('确认标识已使用')
        return WriteResult(function_id=a['function_id'],artifact_id=allocation_id,revision_id=payload.revision_id,created=False)
    if a['head_revision_id']!=payload.revision_id:conflict('只能确认当前工作版本')
    v=db.execute('SELECT * FROM ee.allocation_versions WHERE revision_id=%s',(payload.revision_id,)).fetchone()
    current_source(db,project_id,a['function_id'],'FR',v['fr_revision_id'],v['fr_id'])
    fr=db.execute('SELECT head_revision_id FROM ee.artifacts WHERE id=%s',(v['fr_id'],)).fetchone()
    design=db.execute('SELECT current_version_id FROM ee.project_subsystem_designs WHERE project_id=%s FOR SHARE',(project_id,)).fetchone()
    if fr['head_revision_id']!=v['fr_revision_id'] or not design or design['current_version_id']!=v['design_version_id']:
        conflict('FR或子系统目录已变化，请重新保存分配草稿')

    db.execute('SELECT ee.accept_workflow_allocation(%s,%s,%s,%s,%s)',(project_id,allocation_id,payload.revision_id,actor,str(payload.mutation_id)))
    audit(db,project_id,actor,'allocation.accept',request_id,a['function_id'],allocation_id)
    return WriteResult(function_id=a['function_id'],artifact_id=allocation_id,revision_id=payload.revision_id)
