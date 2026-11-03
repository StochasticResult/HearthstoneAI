"""Recover the same paid background review across slow responses and app restarts."""
import gzip
import hashlib
import http.client
import json
import os
import re
import tempfile
import time
from pathlib import Path

from coach_policy import atomic, read_json

LIMIT = 16*1024*1024


class TrainingError(Exception):
    pass


class HTTPFailure(TrainingError):
    def __init__(self, status, label):
        super().__init__(f'API HTTP {status} ({label})')
        self.status = status


def wire(key, method, path, body=None):
    connection = http.client.HTTPSConnection('api.openai.com',timeout=30)
    try:
        connection.request(method,path,body,{'Authorization':'Bearer '+key,'Content-Type':'application/json'})
        response = connection.getresponse()
        raw = response.read(512*1024+1)
        if response.status not in ((200,201,202) if method=='POST' else (200,)):
            try:
                code = json.loads(raw)['error']['code']
            except (ValueError,TypeError,KeyError):
                code = None
            label = code if isinstance(code,str) and re.fullmatch(r'[\w.-]{1,80}',code) else 'request_failed'
            raise HTTPFailure(response.status,label)
        if len(raw)>512*1024:
            raise TrainingError('API 回包超过上限')
        return json.loads(raw)
    finally:
        connection.close()


def ledger(learning):
    return read_json(learning/'coach-delivery.json',1024*1024,{})


def begin(learning, prefs, games, skipped):
    data = json.dumps([prefs,games,skipped],ensure_ascii=False,separators=(',',':')).encode()
    if len(data)>LIMIT:
        raise TrainingError('整批公开回放超出保存上限')
    path = learning/'coach-pending.gz'
    learning.mkdir(mode=0o700,parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=learning,delete=False) as f:
        temp = Path(f.name)
        try:
            f.write(gzip.compress(data,compresslevel=1,mtime=0))
            f.flush()
            os.replace(temp,path)
        finally:
            temp.unlink(missing_ok=True)
    job = {'status':'open','through':games[-1]['run'],'prior_through':ledger(learning).get('through',''),
           'runs':[g['run'] for g in games],
           'snapshot':hashlib.sha256(data).hexdigest(),'parts':{}}
    atomic(learning/'coach-delivery.json',job)
    return job


def restore(learning, job):
    path=learning/'coach-pending.gz'
    if path.is_symlink() or path.stat().st_size>LIMIT:
        raise TrainingError('待取回评审的回放无效')
    with gzip.open(path,'rb') as f:
        data=f.read(LIMIT+1)
    if len(data)>LIMIT or hashlib.sha256(data).hexdigest()!=job['snapshot']:
        raise TrainingError('待取回评审的回放不匹配')
    return json.loads(data)


def finish(learning, job, status):
    # Keep the covered run IDs, without treating a lost reply as learned strategy.
    through = job.get('prior_through','') if status=='rejected' else job['through']
    atomic(learning/'coach-delivery.json',{'status':status,'through':through,'runs':job['runs']})
    (learning/'coach-pending.gz').unlink(missing_ok=True)


def unpack(reply, expected):
    model = reply.get('model','')
    if reply.get('status')!='completed' or not (model==expected or model.startswith(expected+'-')):
        raise TrainingError('模型不匹配或评审未完整返回')
    content=[c['text'] for m in reply.get('output',[]) if m.get('type')=='message'
             for c in m.get('content',[]) if c.get('type')=='output_text']
    if len(content)!=1:
        raise TrainingError('未收到完整结构化评审')
    return json.loads(content[0]),model


def fetch(learning, job, index, key, payload, *, send=wire, pause=time.sleep):
    path=learning/'coach-delivery.json'
    body=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode()
    digest=hashlib.sha256(body).hexdigest()
    part=job['parts'].get(str(index))
    if part and part['digest']!=digest:
        raise TrainingError('已提交的复盘内容发生变化，未重新付费提交')
    if part and 'result' in part:
        return part['result'],part['model']
    if part and not part.get('response_id'):
        finish(learning,job,'unknown')
        raise TrainingError('上次提交未返回任务编号，费用未知；该批不自动重发')
    if not part:
        part=job['parts'][str(index)]={'digest':digest}
        atomic(path,job)
        try:
            reply=send(key,'POST','/v1/responses',body)
        except HTTPFailure as exc:
            if exc.status in (400,401,403,422,429):
                # No response was created. Keep already completed parts available.
                del job['parts'][str(index)]
                if job['parts']:
                    atomic(path,job)
                else:
                    finish(learning,job,'rejected')
            else:
                finish(learning,job,'unknown')
            raise
        except (OSError, http.client.HTTPException, ValueError, TrainingError):
            finish(learning,job,'unknown')
            raise TrainingError('未收到任务编号，费用未知；该批不自动重发') from None
        response_id=reply.get('id')
        if not isinstance(response_id,str) or not re.fullmatch(r'resp_[A-Za-z0-9_-]{1,200}',response_id):
            finish(learning,job,'unknown')
            raise TrainingError('未取得有效评审任务编号；该批不自动重发')
        part['response_id']=response_id
        atomic(path,job)
    else:
        reply={'status':'in_progress'}
    while reply.get('status') in ('queued','in_progress'):
        pause(4)
        try:
            reply=send(key,'GET','/v1/responses/'+part['response_id'])
        except HTTPFailure as exc:
            if exc.status==404:
                finish(learning,job,'unavailable')
                raise TrainingError('原评审已无法取回；该批不自动重发') from None
            raise
        if reply.get('id')!=part['response_id']:
            raise TrainingError('返回的评审任务编号不匹配')
    try:
        result,model=unpack(reply,payload['model'])
    except (ValueError,KeyError,TypeError,TrainingError):
        finish(learning,job,'unavailable')
        raise
    part.update(result=result,model=model)
    usage=reply.get('usage') or {}
    part['usage']={k:v for k,v in {'input_tokens':usage.get('input_tokens'),'output_tokens':usage.get('output_tokens'),
        'reasoning_tokens':(usage.get('output_tokens_details') or {}).get('reasoning_tokens')}.items() if type(v) is int and v>=0}
    atomic(path,job)
    return result,model
