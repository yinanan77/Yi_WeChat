"""Minimal, key-gated, free-model task orchestrator; no third-party dependencies."""
import concurrent.futures
import datetime as dt
import hmac
import html
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

LOG = logging.getLogger('orchestrator')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
AUTH_TOKEN = os.environ.get('ORCH_TOKEN', '')
API_KEY = os.environ.get('OPENROUTER_API_KEY', '')
DEFAULT_MODEL = os.environ.get('MODEL', 'openrouter/free')
CONTROLLER_MODEL = os.environ.get('CONTROLLER_MODEL', DEFAULT_MODEL)
API_URL = os.environ.get('LLM_API_URL', 'https://openrouter.ai/api/v1/chat/completions')
BOT_TOKEN = os.environ.get('TG_BOT_TOKEN', '')
TELEGRAM_SECRET = os.environ.get('TG_WEBHOOK_SECRET', '')
PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL', '')
ALLOWED_TG_USERS = {x.strip() for x in os.environ.get('TG_ALLOWED_USER_IDS', '').split(',') if x.strip()}
MAX_PARALLEL = min(max(int(os.environ.get('MAX_PARALLEL', '2')), 1), 3)
DAILY_BUDGET = min(max(int(os.environ.get('DAILY_CALL_LIMIT', '35')), 1), 50)
MAX_JOBS = 50
MAX_BODY = 18000
jobs = {}
lock = threading.RLock()
executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix='job')
call_times = []
call_daily = {}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def push_log(job, message):
    with lock:
        job['events'].append({'at': now(), 'event': message})
        job['updated'] = now()


def set_status(job, status):
    with lock:
        job['status'] = status
        job['updated'] = now()


def reserve_call():
    """Conservatively count attempts, including failures, and throttle below free RPM cap."""
    with lock:
        day = dt.datetime.now(dt.timezone.utc).date().isoformat()
        for old in list(call_daily):
            if old != day:
                del call_daily[old]
        if call_daily.get(day, 0) >= DAILY_BUDGET:
            raise RuntimeError('已达到本系统今日免费模型调用上限，请明天再试。')
        t = time.monotonic()
        call_times[:] = [x for x in call_times if t - x < 60]
        if len(call_times) >= 16:
            raise RuntimeError('模型请求过于密集，请约一分钟后再试。')
        call_times.append(t)
        call_daily[day] = call_daily.get(day, 0) + 1


def llm(messages, model=None, key_override=None):
    key = key_override or API_KEY
    if not key:
        raise RuntimeError('未配置 OpenRouter API Key。请在 Render 环境变量中设置 OPENROUTER_API_KEY，或在本页单次任务里填写。')
    model = model or DEFAULT_MODEL
    if not (model == 'openrouter/free' or model.endswith(':free')):
        if os.environ.get('ALLOW_PAID_MODELS') != 'true':
            raise RuntimeError('为防止意外收费，系统拒绝使用非免费模型。')
    reserve_call()
    payload = json.dumps({'model': model, 'messages': messages, 'temperature': 0.3, 'max_tokens': 1400}, ensure_ascii=False).encode('utf-8')
    if len(payload) > 32000:
        raise RuntimeError('模型请求内容过长，请拆分目标。')
    req = urllib.request.Request(API_URL, data=payload, method='POST', headers={
        'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key,
        'HTTP-Referer': 'https://openrouter.ai', 'X-Title': 'Yi Task Orchestrator',
    })
    try:
        with urllib.request.urlopen(req, timeout=65) as res:
            data = json.loads(res.read(160000))
        content = data['choices'][0]['message']['content']
        if isinstance(content, list):
            content = '\n'.join(x.get('text', '') for x in content if isinstance(x, dict))
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError('模型未返回文字内容。')
        return content.strip()
    except urllib.error.HTTPError as e:
        # Discard provider error body: it might contain private prompt details.
        e.read(600)
        if e.code == 401:
            raise RuntimeError('模型 API Key 无效或无权限。') from None
        if e.code == 429:
            raise RuntimeError('免费模型触发限流，请稍后重试。') from None
        raise RuntimeError(f'模型接口 HTTP {e.code}，请检查配置或模型是否可用。') from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError('连接模型接口失败或超时。') from e


def retry_llm(messages, model, job, label, key_override=None):
    for attempt in range(2):
        if job.get('cancel_requested'):
            raise RuntimeError('任务已取消')
        try:
            return llm(messages, model, key_override)
        except RuntimeError as e:
            if attempt or any(x in str(e) for x in ('上限', 'Key', '拒绝', '过于密集')):
                raise
            push_log(job, label + ' 失败，进行一次重试：' + str(e)[:100])
            time.sleep(2)


def parse_plan(response, goal):
    match = re.search(r'\{[\s\S]*\}', response)
    if match:
        try:
            data = json.loads(match.group(0))
            subs = data.get('tasks')
            if isinstance(subs, list):
                filtered = [str(s).strip()[:280] for s in subs if isinstance(s, str) and str(s).strip()]
                if 1 <= len(filtered) <= 3:
                    return filtered
        except (ValueError, TypeError):
            pass
    return [goal]  # explicit fallback, no claim of multi-agent plan


def snapshot(job):
    with lock:
        return {k: v for k, v in job.items() if k not in ('tg_chat',)}


def run_job(job, api_key=None):
    goal = job['goal']
    try:
        set_status(job, 'planning')
        push_log(job, '主控正在拆解任务')
        plan = retry_llm([
            {'role': 'system', 'content': '你是任务编排器。将用户目标拆成最多2个可以并行、互不依赖的子任务。只返回严格 JSON：{"tasks":["任务1","任务2"]}。每个子任务具体、可验证，不虚构已有外部权限，不做危险操作。'},
            {'role': 'user', 'content': goal}
        ], CONTROLLER_MODEL, job, '拆解阶段', api_key)
        tasks = parse_plan(plan, goal)
        with lock:
            job['tasks'] = [{'task': task, 'status': 'pending', 'output': ''} for task in tasks]
        set_status(job, 'running')
        push_log(job, f'已拆成 {len(tasks)} 个任务，开始并行执行')
        def worker(index, item):
            with lock:
                item['status'] = 'running'
            try:
                result = retry_llm([
                    {'role': 'system', 'content': '你是独立任务执行者，只完成当前子任务。必须区分已验证事实与推测，不要声称已执行你没有工具权限的外部操作；结果要具体。'},
                    {'role': 'user', 'content': '总目标：' + goal + '\n你的子任务：' + item['task']}
                ], DEFAULT_MODEL, job, f'子任务{index+1}', api_key)
                with lock:
                    item['status'] = 'done'
                    item['output'] = result
                push_log(job, f'子任务 {index+1} 已完成')
            except Exception as e:
                with lock:
                    item['status'] = 'failed'
                    item['output'] = str(e)[:250]
                push_log(job, f'子任务 {index+1} 失败')
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(tasks))) as pool:
            pending = [pool.submit(worker, i, item) for i, item in enumerate(job['tasks'])]
            concurrent.futures.wait(pending)
        if job.get('cancel_requested'):
            set_status(job, 'cancelled')
            push_log(job, '已取消')
            return
        successes = [item for item in job['tasks'] if item['status'] == 'done']
        if not successes:
            raise RuntimeError('所有子任务失败，请检查任务日志或免费额度。')
        set_status(job, 'reviewing')
        push_log(job, '正在合并结果并检查遗漏')
        compiled = '\n\n'.join(f"【{x['task']}】\n{x['output']}" for x in successes)[:16000]
        result = retry_llm([
            {'role': 'system', 'content': '你是最终审核者。根据子任务结果生成一个清晰可执行的最终答复，去重、纠错、标记未经证实的内容。不得假称你执行了外部操作。只依据已提供内容。'},
            {'role': 'user', 'content': '用户目标：' + goal + '\n子任务结果：\n' + compiled}
        ], CONTROLLER_MODEL, job, '汇总阶段', api_key)
        with lock:
            job['result'] = result
            job['status'] = 'done' if len(successes) == len(tasks) else 'partial'
            job['updated'] = now()
        push_log(job, '已完成交付' if job['status'] == 'done' else '部分完成（存在失败子任务）')
    except Exception as e:
        with lock:
            job['error'] = str(e)[:500]
            job['status'] = 'cancelled' if job.get('cancel_requested') else 'failed'
        push_log(job, '任务结束：' + job['status'])
    finally:
        # The per-job key is not persisted in job records or output.
        if job.get('tg_chat'):
            result = job.get('result') or job.get('error') or '任务未完成'
            notify_telegram(job['tg_chat'], f"任务 {job['id']}（{job['status']}）\n{result[:3300]}")


def create_job(goal, api_key=None, tg_chat=None):
    goal = goal.strip()
    if not 3 <= len(goal) <= 5000:
        raise ValueError('目标需为 3 至 5000 个字符')
    with lock:
        active = sum(x['status'] not in ('done', 'failed', 'partial', 'cancelled') for x in jobs.values())
        if active >= 4:
            raise ValueError('任务已排队过多（最多 4 个），稍后重试。')
        # bounded memory, only evict completed jobs
        if len(jobs) >= MAX_JOBS:
            for k in list(jobs):
                if jobs[k]['status'] in ('done', 'failed', 'partial', 'cancelled'):
                    del jobs[k]
                    break
            if len(jobs) >= MAX_JOBS:
                raise ValueError('任务队列暂满。')
        jid = secrets.token_hex(5)
        job = {'id': jid, 'goal': goal, 'status': 'queued', 'created': now(), 'updated': now(),
               'tasks': [], 'result': '', 'error': '', 'events': [{'at': now(), 'event': '任务已接收'}],
               'cancel_requested': False}
        if tg_chat:
            job['tg_chat'] = str(tg_chat)
        jobs[jid] = job
    executor.submit(run_job, job, api_key)
    return jid


def notify_telegram(chat_id, text):
    if not BOT_TOKEN:
        return
    payload = json.dumps({'chat_id': chat_id, 'text': text[:3900]}).encode()
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
    try:
        with urllib.request.urlopen(urllib.request.Request(url, payload, headers={'Content-Type':'application/json'}), timeout=12) as res:
            res.read(1000)
    except Exception as e:
        LOG.warning('Telegram notify failed: %s', type(e).__name__)


HTML = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Yi · 多模型任务主控</title><style>
:root{color-scheme:dark}body{font:15px/1.6 -apple-system,BlinkMacSystemFont,"Noto Sans SC",system-ui,sans-serif;background:#0c1120;color:#e7edf8;margin:0;padding:26px 15px}main{max-width:760px;margin:auto}h1{font-size:26px;letter-spacing:.4px;margin:0}p{color:#a4afc4;margin:6px 0 18px}.panel{border:1px solid #26354f;border-radius:15px;background:#151e31;padding:19px;margin:16px 0}.eyebrow{font-size:11px;color:#7aa7ed;letter-spacing:2px;text-transform:uppercase}input,textarea{background:#0d1525;border:1px solid #34445f;color:#edf3ff;border-radius:9px;padding:12px;font:inherit;box-sizing:border-box;width:100%}textarea{min-height:110px;resize:vertical}label{display:block;margin:12px 0 7px;font-size:13px;color:#bdcbe0}button{background:#386fdd;color:white;border:0;border-radius:8px;padding:10px 17px;font:inherit;cursor:pointer;margin:10px 6px 0 0}button:disabled{opacity:.5;cursor:default}.secondary{background:#253652}.output{white-space:pre-wrap;word-break:break-word;background:#0d1525;border-radius:8px;padding:12px;margin-top:9px}.small{font-size:12px;color:#9daec7}strong{color:#b3d1ff}.status{display:inline-block;background:#243d63;padding:4px 10px;border-radius:16px}details{margin:10px 0}hr{border:0;border-top:1px solid #273551}#messages{min-height:20px;color:#eeb573}
</style></head><body><main><div class="eyebrow">Task orchestrator · zero paid models by default</div><h1>Yi 多模型任务主控</h1><p>拆解任务 → 并行免费模型 → 去重审核 → 统一交付。非 ChatGPT 账号自动调用。</p><section class="panel"><div class="eyebrow">01 · 访问验证</div><label>控制台访问令牌（Render 环境变量 ORCH_TOKEN）</label><input id="token" type="password" placeholder="仅保存在当前页面内存" autocomplete="off"><button class="secondary" onclick="check()">检查连接</button><div id="status" class="small"></div></section><section class="panel"><div class="eyebrow">02 · 创建任务</div><label>具体目标</label><textarea id="goal" placeholder="例如：分析我的男士发型视频获客流程，分别审查内容、转化问题并给出行动方案"></textarea><label>OpenRouter Key（可选，只用于这次任务；优先推荐在 Render 环境变量里配置）</label><input id="apiKey" type="password" placeholder="未配置环境变量时填写 sk-or-v1-…" autocomplete="off"><button id="submit" onclick="submitJob()">拆解并执行</button><div id="messages"></div></section><section class="panel"><div class="eyebrow">03 · 执行状态</div><div id="jobs" class="small">当前没有任务</div><button class="secondary" onclick="listJobs()">刷新任务</button></section><p class="small">⚠ 免费 Render 服务会休眠且任务记录不会永久保留；敏感材料不建议通过免费第三方模型处理。免费模型有速率限制。</p></main><script>
const $=id=>document.getElementById(id);const escapeHtml=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function call(path,opt={}){const headers={'X-Orch-Token':$('token').value,...(opt.headers||{})};let r=await fetch(path,{...opt,headers});let x=await r.json().catch(()=>({error:'非法响应'}));if(!r.ok)throw Error(x.error||'请求失败 '+r.status);return x}
async function check(){try{let x=await call('/api/config');$('status').textContent='连接成功 · 环境密钥 '+(x.api_key_set?'已配置':'未配置')+' · 每日调用上限 '+x.daily_limit+' · 今日已调用 '+x.today_calls;listJobs()}catch(e){$('status').textContent=e.message}}
async function submitJob(){let goal=$('goal').value.trim();if(!goal)return $('messages').textContent='先填写任务目标';$('submit').disabled=true;$('messages').textContent='正在提交…';try{let j=await call('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({goal,api_key:$('apiKey').value.trim()})});$('apiKey').value='';$('messages').textContent='已创建任务 '+j.id;listJobs()}catch(e){$('messages').textContent=e.message}finally{$('submit').disabled=false}}
async function listJobs(){try{let x=await call('/api/jobs');let outer=$('jobs');outer.innerHTML='';if(!x.jobs.length){outer.textContent='没有任务';return}for(let j of x.jobs){let p=document.createElement('div');p.className='panel';let title=document.createElement('div');title.innerHTML='<strong>'+escapeHtml(j.goal.slice(0,120))+'</strong>　<span class="status">'+escapeHtml(j.status)+'</span>';p.append(title);let small=document.createElement('div');small.className='small';small.textContent=j.id+' · '+j.updated;p.append(small);let btn=document.createElement('button');btn.className='secondary';btn.textContent='查看任务与日志';btn.onclick=()=>detail(j.id,p);p.append(btn);outer.append(p)} }catch(e){$('jobs').textContent=e.message}}
async function detail(id,target){try{let j=await call('/api/jobs/'+id);let old=target.querySelector('.detail');if(old)old.remove();let div=document.createElement('div');div.className='detail';let tasks=j.tasks.map((t,i)=>'\n'+(i+1)+'. '+t.task+' ['+t.status+']\n'+t.output).join('\n');let box=document.createElement('div');box.className='output';box.textContent='状态：'+j.status+'\n\n'+(j.result||j.error||'任务执行中')+'\n\n子任务：'+tasks+'\n\n执行记录：\n'+j.events.map(e=>e.at+' '+e.event).join('\n');div.append(box);if(!['done','failed','partial','cancelled'].includes(j.status)){let stop=document.createElement('button');stop.className='secondary';stop.textContent='取消后续执行';stop.onclick=async()=>{await call('/api/jobs/'+id+'/cancel',{method:'POST'});detail(id,target)};div.append(stop)}target.append(div)}catch(e){alert(e.message)}}
setInterval(()=>{if($('token').value)listJobs()},12000);
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Avoid logging paths containing secrets; only log generic request status.
        LOG.info('HTTP %s', self.command)

    def send_json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def ok_auth(self):
        if not AUTH_TOKEN:
            self.send_json(503, {'error':'管理员令牌未配置。请在 Render 中设置 ORCH_TOKEN。'})
            return False
        provided = self.headers.get('X-Orch-Token','')
        if not hmac.compare_digest(provided, AUTH_TOKEN):
            self.send_json(401, {'error': '访问令牌不正确'})
            return False
        return True

    def body(self):
        n = int(self.headers.get('Content-Length','0'))
        if n <= 0 or n > MAX_BODY:
            raise ValueError('提交内容不合法或过大')
        return json.loads(self.rfile.read(n))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/':
            raw = HTML.encode()
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header('X-Frame-Options','DENY')
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == '/health':
            return self.send_json(200, {'status':'ok', 'service':'yi-task-orchestrator'})
        if not self.ok_auth():return
        if path == '/api/config':
            today = dt.datetime.now(dt.timezone.utc).date().isoformat()
            return self.send_json(200, {'api_key_set':bool(API_KEY), 'telegram_connected':bool(BOT_TOKEN and ALLOWED_TG_USERS and TELEGRAM_SECRET), 'model':DEFAULT_MODEL, 'max_parallel':MAX_PARALLEL, 'daily_limit':DAILY_BUDGET, 'today_calls':call_daily.get(today,0), 'ephemeral_storage':True})
        if path == '/api/jobs':
            with lock:
                j = [ {'id':x['id'],'goal':x['goal'],'status':x['status'],'updated':x['updated']} for x in reversed(list(jobs.values())) ]
            return self.send_json(200,{'jobs':j})
        m = re.fullmatch(r'/api/jobs/([a-f0-9]{10})',path)
        if m:
            with lock:
                item = jobs.get(m.group(1))
            return self.send_json(200, snapshot(item)) if item else self.send_json(404, {'error':'任务不存在'})
        return self.send_json(404,{'error':'接口不存在'})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/telegram/webhook':
            if not BOT_TOKEN or not TELEGRAM_SECRET or not ALLOWED_TG_USERS:
                return self.send_json(503, {'error':'Telegram 未启用'})
            if not hmac.compare_digest(self.headers.get('X-Telegram-Bot-Api-Secret-Token',''), TELEGRAM_SECRET):
                return self.send_json(401,{'error':'not authorized'})
            try:
                data = self.body()
                msg = data.get('message') or {}
                user = str((msg.get('from') or {}).get('id',''))
                if user not in ALLOWED_TG_USERS:
                    return self.send_json(200,{'ok':True})
                text = str(msg.get('text') or '').strip()
                chat = str((msg.get('chat') or {}).get('id',''))
                if text.startswith('/start') or text.startswith('/help'):
                    notify_telegram(chat,'发一条文字任务给我，我会拆解、并行执行并发送最终结果。/status 任务ID 查询状态。')
                elif text.startswith('/status '):
                    jid = text.split(' ',1)[1].strip()
                    with lock:
                        j = jobs.get(jid)
                    if j and str(j.get('tg_chat','')) == chat:
                        notify_telegram(chat, f'{jid}: '+j['status']+'\n'+(j['result'] or j['error'])[:2500])
                    else:
                        notify_telegram(chat,'未找到这个任务或任务已因休眠清空。')
                elif text and not text.startswith('/'):
                    try:
                        jid = create_job(text, tg_chat=chat)
                        notify_telegram(chat,'已接收任务：'+jid+'\n处理完成后会通知你。')
                    except ValueError as e:
                        notify_telegram(chat,str(e))
                return self.send_json(200,{'ok':True})
            except Exception:
                return self.send_json(400,{'error':'bad request'})
        if not self.ok_auth():return
        if path == '/api/jobs':
            try:
                data = self.body()
                goal = data.get('goal','')
                if not isinstance(goal,str):raise ValueError('goal 必须是文本')
                key = data.get('api_key','')
                if not isinstance(key,str) or len(key)>300:raise ValueError('API Key 格式不正确')
                jid = create_job(goal, api_key=key or None)
                return self.send_json(202, {'id':jid,'status':'queued'})
            except (ValueError,json.JSONDecodeError) as e:
                return self.send_json(400, {'error':str(e)})
        m = re.fullmatch(r'/api/jobs/([a-f0-9]{10})/cancel',path)
        if m:
            with lock:
                item = jobs.get(m.group(1))
                if item:
                    item['cancel_requested']=True
            return self.send_json(200, {'ok':True}) if item else self.send_json(404,{'error':'任务不存在'})
        if path == '/api/telegram/register':
            if not (BOT_TOKEN and TELEGRAM_SECRET and ALLOWED_TG_USERS and PUBLIC_BASE_URL.startswith('https://')):
                return self.send_json(400,{'error':'Telegram 环境变量未配齐'})
            payload = json.dumps({'url':PUBLIC_BASE_URL.rstrip('/')+'/telegram/webhook','secret_token':TELEGRAM_SECRET,'drop_pending_updates':False}).encode()
            url = f'https://api.telegram.org/bot{BOT_TOKEN}/setWebhook'
            try:
                with urllib.request.urlopen(urllib.request.Request(url,payload,headers={'Content-Type':'application/json'}),timeout=15) as r:
                    response = json.loads(r.read(2000))
                return self.send_json(200, {'ok':bool(response.get('ok'))})
            except Exception:
                return self.send_json(502,{'error':'Telegram 注册失败'})
        return self.send_json(404,{'error':'接口不存在'})


def main():
    port = int(os.environ.get('PORT','8000'))
    LOG.info('Yi task orchestrator starting on :%s',port)
    ThreadingHTTPServer(('0.0.0.0',port),Handler).serve_forever()

if __name__ == '__main__':
    main()
