from contextlib import nullcontext
from uuid import uuid4
from app.services import project_chat

def test_first_question_title_collapses_whitespace_and_limits_length():
    assert project_chat.first_question_title('  第一行'+chr(10)+'第二行  ')=='第一行 第二行'
    assert len(project_chat.first_question_title('问题'*100))==120
    assert project_chat.first_question_title(None)=='新会话'
    assert project_chat.first_question_title('', '既有空会话')=='既有空会话'

def test_thread_list_reads_first_prompts_only_for_owned_thread_ids(monkeypatch):
    tid=uuid4();rows=[{'id':tid,'title':'新会话'}];calls=[]
    class Result:
        def __init__(self,rows):self.rows=rows
        def fetchall(self):return self.rows
    class Db:
        def execute(self,query,args):
            calls.append((query,args))
            if 'DISTINCT ON' in query:
                assert args==([tid],)
                return Result([{'thread_id':tid,'prompt':'用户的第一次问题'}])
            return Result(rows)
    monkeypatch.setattr(project_chat,'read_db',lambda:nullcontext(Db()))
    monkeypatch.setattr(project_chat,'visible',lambda *args:'current-user')
    result=project_chat.list_threads('engineer01','current-project')
    assert result[0]['title']=='用户的第一次问题'
    assert rows[0]['title']=='新会话'
    assert len(calls)==2 and all('UPDATE' not in q for q,_ in calls)
    assert calls[0][1]==('current-project','current-user',0)

def test_history_title_uses_first_question_not_later_question(monkeypatch):
    thread={'id':uuid4(),'title':'新会话'}
    turns=[{'prompt':'最初的问题'},{'prompt':'后续的问题'}]
    class Db:
        def execute(self,*args):return self
        def fetchall(self):return turns
    monkeypatch.setattr(project_chat,'read_db',lambda:nullcontext(Db()))
    monkeypatch.setattr(project_chat,'owned',lambda *args:thread)
    result=project_chat.history('engineer01','project',thread['id'])
    assert result['thread']['title']=='最初的问题'
    assert thread['title']=='新会话'
    assert result['turns']==turns
