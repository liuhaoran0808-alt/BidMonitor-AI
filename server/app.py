"""
BidMonitor 服务器端主应用
基于 FastAPI 构建的 RESTful API 服务
"""
import os
import sys
import json
import asyncio
import logging
import threading
import copy
import tempfile
from local_security import credentials, redacted, preserve_secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
import secrets

# 添加 src 目录到路径
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(BASE_DIR, 'src')
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# 导入原有模块
from monitor_core import MonitorCore, get_default_sites
from database.storage import Storage, BidInfo
from ai_guard import AIGuard
from competitor import CompetitorService, PLATFORMS, parse_companies

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

# 全局状态
class AppState:
    def __init__(self):
        self.is_running = False
        self.continuous = False
        self.task_lock = threading.Lock()
        self.task_token = None
        self.monitor_core: Optional[MonitorCore] = None
        self.scheduler: Optional[AsyncIOScheduler] = None
        self.last_run_time: Optional[datetime] = None
        self.next_run_time: Optional[datetime] = None
        self.logs: List[str] = []
        self.config: Dict[str, Any] = {}
        self.storage = Storage()
        self.stop_event = threading.Event()  # 停止事件，用于中断正在运行的任务
        self.current_task_running = False  # 标记当前是否有任务正在执行
        self.today_rounds = 0  # 今日监控轮数
        self.today_date = datetime.now().strftime('%Y-%m-%d')  # 今日日期
        # 进度跟踪
        self.progress_current = 0  # 当前爬取的网站序号
        self.progress_total = 0    # 总网站数
        self.progress_site = ""    # 当前正在爬取的网站名称
        
    def add_log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_entry = f"[{timestamp}] {message}"
        self.logs.append(log_entry)
        # 只保留最近200条日志
        if len(self.logs) > 200:
            self.logs = self.logs[-200:]
        logger.info(message)

app_state = AppState()
competitor_service = CompetitorService(app_state.storage.db_path, log=app_state.add_log)

# 配置文件路径
CONFIG_FILE = os.path.join(BASE_DIR, 'server', 'server_config.json')

# HTTP Basic 认证配置
security = HTTPBasic()
AUTH_USERNAME, AUTH_PASSWORD = credentials()

def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    """验证用户名和密码"""
    correct_username = secrets.compare_digest(credentials.username, AUTH_USERNAME)
    correct_password = secrets.compare_digest(credentials.password, AUTH_PASSWORD)
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=401,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

def load_config() -> Dict[str, Any]:
    """加载配置"""
    default_config = {
        'keywords': '光伏,风电,风力发电,光伏巡检,风电巡检,无人机巡检,光伏无人机,风机巡检,风力发电巡检,光伏电站无人机,风电场无人机,光伏运维,风机运维,叶片巡检,红外巡检,新能源巡检',
        'exclude': '大疆',
        'must_contain': '无人机',
        'interval': 10,
        'enabled_sites': [
            'chinabidding', 'dlzb', 'chinabiddingcc', 'gdtzb', 'cpeinet', 'espic',
            'chng', 'powerchina', 'powerchina_bid', 'powerchina_ec', 'powerchina_scm',
            'powerchina_idx', 'powerchina_nw', 'ceec', 'chdtp', 'chec_gys', 'chinazbcg',
            'cdt', 'ebidding', 'neep', 'ceic', 'sgcc', 'cecep', 'gdg', 'crpower', 'crc',
            'longi', 'cgnpc', 'dongfang', 'zjycgzx', 'ctg', 'sdicc', 'csg', 'sgccetp',
            'powerbeijing', 'ccccltd', 'jchc', 'minmetals', 'sunwoda', 'cnbm', 'hghn',
            'xcmg', 'xinecai', 'ariba', 'faw'
        ],
        'email_enabled': True,
        'sms_enabled': True,
        'voice_enabled': False,
        'wechat_enabled': False,
        'ai_enabled': False,
        'email_configs': [],  # 开源版本默认空
        'sms_config': {
            'provider': 'aliyun',
            'sign_name': '',
            'template_code': '',
            'access_key_id': '',
            'access_key_secret': ''
        },
        'voice_config': {
            'provider': 'aliyun',
            'access_key_id': '',
            'access_key_secret': '',
            'called_show_number': '',
            'tts_code': ''
        },
        'wechat_config': {
            'provider': 'pushplus',
            'token': ''
        },
        'ai_config': {
            'enable': False,
            'base_url': 'https://api.deepseek.com/chat/completions',
            'api_key': '',  # 请填入您的API Key
            'model': 'deepseek-chat'
        },
        'contacts': [],  # 开源版本默认空
        'use_selenium': True  # Selenium浏览器模式开关
    }
    
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                saved_config = json.load(f)
                default_config.update(saved_config)
        except Exception as e:
            logger.error(f"加载配置失败: {e}")
    
    return default_config

def save_config(config: Dict[str, Any]):
    """Atomic replacement; error must reach the API, never acknowledge unsaved data."""
    name = None
    try:
        directory = os.path.dirname(CONFIG_FILE)
        os.makedirs(directory, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=directory,prefix='.config-',suffix='.tmp',delete=False) as f:
            name = f.name
            json.dump(config, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, CONFIG_FILE)
    except (OSError, TypeError, ValueError):
        logger.error('保存配置失败；原配置未更新')
        raise HTTPException(status_code=500, detail='配置写入失败，未保存，请检查磁盘空间和文件权限')
    finally:
        if name and os.path.isfile(name):
            os.unlink(name)


def commit_config(changes):
    candidate = copy.deepcopy(app_state.config)
    candidate.update(preserve_secrets(changes, candidate))
    for contact in candidate.get('contacts', []):
        contact.setdefault('id', secrets.token_hex(12))
    save_config(candidate)
    app_state.config = candidate


# Pydantic 模型
class ConfigModel(BaseModel):
    keywords: Optional[str] = None
    exclude: Optional[str] = None
    must_contain: Optional[str] = None
    interval: Optional[int] = Field(default=None, ge=1, le=10080)
    enabled_sites: Optional[List[str]] = None
    email_enabled: Optional[bool] = None
    sms_enabled: Optional[bool] = None
    voice_enabled: Optional[bool] = None
    wechat_enabled: Optional[bool] = None
    ai_enabled: Optional[bool] = None
    use_selenium: Optional[bool] = None  # Selenium浏览器模式开关

class StatusResponse(BaseModel):
    is_running: bool
    last_run_time: Optional[str]
    next_run_time: Optional[str]
    total_bids: int
    today_new: int
    interval: int

# 定时任务：执行监控
def reserve_monitor():
    if not app_state.task_lock.acquire(blocking=False):
        return None
    token = object()
    app_state.task_token = token
    app_state.current_task_running = True
    return token


def release_monitor(token):
    if app_state.task_token is token:
        app_state.task_token = None
        app_state.current_task_running = False
        app_state.task_lock.release()


async def run_monitor_task(token=None):
    if token is None:
        if not app_state.continuous or app_state.stop_event.is_set():
            return
        token = reserve_monitor()
        if token is None:
            return
    elif app_state.task_token is not token:
        return
    worker = None
    try:
        if app_state.stop_event.is_set():
            return
        worker = asyncio.create_task(_execute_monitor_task())
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            # run_in_executor cancellation does not stop its thread. Retain the reservation.
            app_state.stop_event.set()
            app_state.continuous = app_state.is_running = False
            await worker
            raise
    finally:
        if not app_state.continuous:
            app_state.is_running = False
            app_state.next_run_time = None
        release_monitor(token)


async def _execute_monitor_task():
    """执行一次监控任务"""
    # 检查是否应该运行
    if not app_state.is_running:
        return
    
    # 检查是否被中断
    if app_state.stop_event.is_set():
        app_state.add_log("检索任务被中断")
        return
    
    # 标记任务正在运行
    app_state.add_log("=" * 40)
    app_state.add_log("开始执行检索任务...")
    app_state.last_run_time = datetime.now()
    
    try:
        config = copy.deepcopy(app_state.config)
        keywords = [k.strip() for k in config.get('keywords', '').split(',') if k.strip()]
        exclude = [k.strip() for k in config.get('exclude', '').split(',') if k.strip()]
        must_contain = [k.strip() for k in config.get('must_contain', '').split(',') if k.strip()]
        
        # AI 配置
        ai_config = None
        if config.get('ai_enabled') and config.get('ai_config'):
            ai_config = config['ai_config']
            ai_config['enable'] = True
        
        # 创建监控核心
        monitor = MonitorCore(
            keywords=keywords,
            exclude_keywords=exclude,
            must_contain_keywords=must_contain,
            log_callback=app_state.add_log,
            ai_config=ai_config, db_path=app_state.storage.db_path
        )
        
  
        # 设置启用的网站
        monitor.config['crawler'] = monitor.config.get('crawler', {})
        monitor.config['crawler']['enabled_sites'] = config.get('enabled_sites', [])
        monitor.config['custom_sites'] = config.get('custom_sites', [])
        # 使用配置中的Selenium设置
        monitor.config['crawler']['use_selenium'] = config.get('use_selenium', False)
        if config.get('use_selenium'):
            app_state.add_log("✅ Selenium浏览器模式已启用")
        else:
            app_state.add_log("📄 使用普通HTTP模式")
        
        # 重新初始化爬虫
        monitor.crawlers = monitor._init_crawlers()
        
        # 设置爬虫总数
        app_state.progress_total = len(monitor.crawlers)
        app_state.progress_current = 0
        app_state.progress_site = ""
        
        # 进度回调函数
        def progress_callback(current, total, site_name):
            app_state.progress_current = current
            app_state.progress_total = total
            app_state.progress_site = site_name
        
        # 在线程池中执行同步的爬虫任务，防止阻塞事件循环
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,  # 使用默认线程池
            lambda: monitor.run_once(progress_callback=progress_callback, stop_event=app_state.stop_event)
        )
        
        # 检查是否被中断
        if app_state.stop_event.is_set():
            app_state.add_log("检索任务被中断")
            return
        
        new_count = result.get('new_count', 0)
        app_state.add_log(f"检索完成，新增 {new_count} 条匹配招标信息")
        
        # 发送通知（如果有新结果且未被中断）
        if new_count > 0 and not app_state.stop_event.is_set():
            await send_notifications(config, new_count)
        
    except Exception as e:
        app_state.add_log(f"检索任务异常: {e}")
        logger.exception("Monitor task error")
    finally:
        # 清除进度信息
        app_state.progress_current = 0
        app_state.progress_total = 0
        app_state.progress_site = ""
    
    # 增加今日监控轮数（如果日期变化则重置）
    today = datetime.now().strftime('%Y-%m-%d')
    if today != app_state.today_date:
        app_state.today_date = today
        app_state.today_rounds = 0
    app_state.today_rounds += 1
    app_state.add_log(f"📊 今日已完成第 {app_state.today_rounds} 轮监控")
    
    # 任务完成后，调度下一次执行（仅在仍在运行时）
    if app_state.continuous and not app_state.stop_event.is_set():
        interval = app_state.config.get('interval', 20)
        from datetime import timedelta
        from apscheduler.triggers.date import DateTrigger
        
        next_run = datetime.now() + timedelta(minutes=interval)
        app_state.next_run_time = next_run
        
        # 调度下一次任务
        if app_state.scheduler and app_state.scheduler.running:
            # 移除旧任务（如果存在）
            try:
                app_state.scheduler.remove_job('monitor_job')
            except:
                pass
            # 添加新的一次性任务
            app_state.scheduler.add_job(
                run_monitor_task,
                trigger=DateTrigger(run_date=next_run),
                id='monitor_job',
                replace_existing=True
            )
            app_state.add_log(f"⏰ 下次检索时间: {next_run.strftime('%H:%M:%S')}")

async def send_notifications(config: Dict, new_count: int):
    """发送通知"""
    # 使用最新的配置（支持运行期间修改配置立即生效）
    config = app_state.config
    
    # 复用原有的通知模块
    try:
        contacts = config.get('contacts', [])
        
        # 获取新增的招标信息用于通知
        unnotified_bids = app_state.storage.get_unnotified() if hasattr(app_state.storage, 'get_unnotified') else []
        
        for contact in contacts:
            if not contact.get('enabled', True):
                continue
            
            name = contact.get('name', '未知')
            
            # 邮件通知
            if config.get('email_enabled') and contact.get('email') and contact.get('email_password'):
                try:
                    email_type = contact.get('email_type', 'QQ邮箱')
                    smtp_configs = {
                        'QQ邮箱': {'smtp_server': 'smtp.qq.com', 'smtp_port': 465, 'use_ssl': True},
                        '163邮箱': {'smtp_server': 'smtp.163.com', 'smtp_port': 465, 'use_ssl': True},
                        'Gmail': {'smtp_server': 'smtp.gmail.com', 'smtp_port': 587, 'use_ssl': False},
                        'Outlook': {'smtp_server': 'smtp.office365.com', 'smtp_port': 587, 'use_ssl': False},
                        '企业邮箱': {'smtp_server': 'smtp.exmail.qq.com', 'smtp_port': 465, 'use_ssl': True},
                    }
                    smtp_config = smtp_configs.get(email_type, smtp_configs['QQ邮箱'])
                    
                    email_config_full = {
                        'smtp_server': smtp_config['smtp_server'],
                        'smtp_port': smtp_config['smtp_port'],
                        'use_ssl': smtp_config['use_ssl'],
                        'sender': contact['email'],
                        'password': contact['email_password'],
                        'receiver': contact['email'],
                    }
                    from notifier.email import EmailNotifier
                    notifier = EmailNotifier(email_config_full)
                    if notifier.send(unnotified_bids[:10]):  # 最多发送10条
                        app_state.add_log(f"📧 邮件通知成功: {name}")
                    else:
                        app_state.add_log(f"❌ 邮件通知失败: {name}")
                except Exception as e:
                    app_state.add_log(f"❌ 邮件通知异常 {name}: {e}")
            
            # 短信通知
            if config.get('sms_enabled') and contact.get('phone'):
                try:
                    sms_config = config.get('sms_config', {})
                    if sms_config.get('access_key_id') and sms_config.get('template_code'):
                        from notifier.sms import SMSNotifier
                        notifier = SMSNotifier(sms_config)
                        summary = {'count': new_count, 'source': '招标网站'}
                        if notifier.send(contact['phone'], summary=summary):
                            app_state.add_log(f"📱 短信通知成功: {name}")
                        else:
                            app_state.add_log(f"❌ 短信通知失败: {name}")
                except Exception as e:
                    app_state.add_log(f"❌ 短信通知异常 {name}: {e}")
            
            # 语音通知
            if config.get('voice_enabled') and contact.get('phone'):
                try:
                    from notifier.voice import VoiceNotifier
                    import time
                    time.sleep(3)  # 延迟3秒让网络恢复
                    voice_config = config.get('voice_config', {})
                    if voice_config.get('tts_code'):
                        notifier = VoiceNotifier(voice_config)
                        if notifier.call(contact['phone'], count=new_count, source="招标网站"):
                            app_state.add_log(f"📞 语音呼叫成功: {name}")
                        else:
                            app_state.add_log(f"❌ 语音呼叫失败: {name}")
                except Exception as e:
                    app_state.add_log(f"❌ 语音通知异常 {name}: {e}")
            
            # 微信通知
            if config.get('wechat_enabled') and contact.get('wechat_token'):
                try:
                    from notifier.wechat import WeChatNotifier
                    notifier = WeChatNotifier({
                        'provider': 'pushplus',
                        'token': contact['wechat_token']
                    })
                    if notifier.send(unnotified_bids[:10]):  # 最多发送10条
                        app_state.add_log(f"💬 微信通知成功: {name}")
                    else:
                        app_state.add_log(f"❌ 微信通知失败: {name}")
                except Exception as e:
                    app_state.add_log(f"❌ 微信通知异常 {name}: {e}")
                        
    except Exception as e:
        app_state.add_log(f"发送通知异常: {e}")

# 生命周期管理
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时
    app_state.config = load_config()
    for contact in app_state.config.get('contacts', []):
        contact.setdefault('id', secrets.token_hex(12))
    app_state.add_log("BidMonitor 服务器已启动")
    
    yield
    
    # 关闭时
    if app_state.scheduler and app_state.scheduler.running:
        app_state.scheduler.shutdown()
    competitor_service.close()
    app_state.add_log("BidMonitor 服务器已关闭")

# 创建 FastAPI 应用
app = FastAPI(
    title="BidMonitor API",
    description="招标监控系统服务端 API",
    version="1.6",
    lifespan=lifespan
)

# 添加CORS中间件，允许前端跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=[x.strip() for x in os.environ.get('BIDMONITOR_ALLOWED_ORIGINS', '').split(',') if x.strip()],  # 默认仅同源
    allow_credentials=True,
    allow_methods=["*"],  # 允许所有HTTP方法
    allow_headers=["*"],  # 允许所有请求头
)

# HTTP Basic 认证中间件
import base64
from starlette.middleware.base import BaseHTTPMiddleware

class BasicAuthMiddleware(BaseHTTPMiddleware):
    """HTTP Basic 认证中间件"""
    async def dispatch(self, request: Request, call_next):
        # 检查Authorization头
        auth_header = request.headers.get("Authorization")
        
        if auth_header:
            try:
                scheme, credentials = auth_header.split()
                if scheme.lower() == "basic":
                    decoded = base64.b64decode(credentials).decode("utf-8")
                    username, password = decoded.split(":", 1)
                    if secrets.compare_digest(username, AUTH_USERNAME) and secrets.compare_digest(password, AUTH_PASSWORD):
                        return await call_next(request)
            except Exception:
                pass
        
        # 认证失败，返回401
        return Response(
            content="认证失败，请输入正确的用户名和密码",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="BidMonitor"'},
            media_type="text/plain"
        )

app.add_middleware(BasicAuthMiddleware)

# 静态文件
STATIC_DIR = os.path.join(os.path.dirname(__file__), 'static')
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# API 路由
@app.get("/", response_class=HTMLResponse)
async def root():
    """返回主页"""
    index_path = os.path.join(STATIC_DIR, 'index.html')
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return HTMLResponse("<h1>BidMonitor 服务正在运行</h1><p>请访问 /static/index.html</p>")

def _local_created_at(bid):
    """SQLite CURRENT_TIMESTAMP 为 UTC；展示和今日统计统一使用 UTC+8。"""
    value = getattr(bid, 'created_at', None)
    if not value:
        return None
    try:
        created = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created.astimezone(timezone(timedelta(hours=8)))


def _result_item(bid):
    created = _local_created_at(bid)
    return {
        'title': bid.title,
        'url': bid.url,
        'source': bid.source,
        'pub_date': bid.publish_date or None,
        'collected_at': created.strftime('%Y-%m-%d %H:%M:%S') if created else None,
    }


class CompetitorQueryRequest(BaseModel):
    company_text: str
    platforms: List[str]
    notice_urls: List[str] = []


class CompetitorManualRequest(BaseModel):
    platform: str
    company: str


@app.get("/api/competitors/config")
async def competitor_config():
    return {'company_text': app_state.config.get('competitor_companies', ''),
            'platforms': app_state.config.get('competitor_platforms', list(PLATFORMS)),
            'available': [{'id': key, 'name': value['name'], 'url': value['url']} for key, value in PLATFORMS.items()]}


@app.post("/api/competitors/query")
async def competitor_query(req: CompetitorQueryRequest, background_tasks: BackgroundTasks):
    try:
        state = competitor_service.reserve(req.company_text, req.platforms, getattr(req, 'notice_urls', []))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        commit_config({'competitor_companies': '\n'.join(state['companies']), 'competitor_platforms': req.platforms})
    except Exception:
        with competitor_service.lock:
            competitor_service.state['running'] = False
            for entry in competitor_service.state['entries']:
                entry.update(status='cancelled', message='配置未保存，查询未启动')
        raise
    async def execute():
        await asyncio.get_running_loop().run_in_executor(None, competitor_service.run)
    background_tasks.add_task(execute)
    return state


@app.get("/api/competitors/status")
async def competitor_status():
    return competitor_service.snapshot()


@app.post("/api/competitors/stop")
async def competitor_stop():
    competitor_service.stop.set()
    return {'message': '已请求停止，将在当前页面操作结束后停止'}


@app.get("/api/competitors/results")
async def competitor_results(company_text: str = ''):
    try:
        companies = parse_companies(company_text) if company_text.strip() else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return dict(competitor_service.records.counts(companies), items=competitor_service.records.list(companies, limit=200),
                summary=competitor_service.records.summary(companies), display_limit=200)


@app.post("/api/competitors/manual")
async def competitor_manual(req: CompetitorManualRequest):
    try:
        return await asyncio.get_running_loop().run_in_executor(None, competitor_service.open_manual, req.platform, req.company)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=503, detail='无法打开本机浏览器：'+type(exc).__name__)


@app.get("/api/status")
async def get_status():
    """获取监控状态"""
    # 今日新增按实际入库时间统计，不能用来源的发布日期代替。
    today = datetime.now(timezone(timedelta(hours=8))).date()
    all_bids = app_state.storage.get_all() if hasattr(app_state.storage, 'get_all') else []
    today_new = 0
    for b in all_bids:
        created = _local_created_at(b)
        if created and created.date() == today:
            today_new += 1
    
    service = globals().get('competitor_service')
    competitor_counts = service.records.counts() if service else {'total': 0, 'today_new': 0}
    return {
        "is_running": app_state.is_running,
        "last_run_time": app_state.last_run_time.strftime("%Y-%m-%d %H:%M:%S") if app_state.last_run_time else None,
        "next_run_time": app_state.next_run_time.strftime("%Y-%m-%d %H:%M:%S") if app_state.next_run_time else None,
        "total_bids": len(all_bids) + competitor_counts['total'],
        "today_new": today_new + competitor_counts['today_new'],
        "today_rounds": app_state.today_rounds,
        "interval": app_state.config.get('interval', 20),
        # 进度信息
        "progress_current": app_state.progress_current,
        "progress_total": app_state.progress_total,
        "progress_site": app_state.progress_site,
        "is_crawling": app_state.current_task_running
    }

@app.post('/api/start')
async def start_monitor(background_tasks: BackgroundTasks):
    if app_state.is_running:
        raise HTTPException(409, '监控已在运行')
    token = reserve_monitor()
    if token is None:
        raise HTTPException(409, '上一任务仍在执行或停止中，请等待')
    try:
        app_state.stop_event.clear()
        app_state.continuous = app_state.is_running = True
        if not app_state.scheduler or not app_state.scheduler.running:
            app_state.scheduler = AsyncIOScheduler()
            app_state.scheduler.start()
        app_state.next_run_time = None
        background_tasks.add_task(run_monitor_task, token)
    except Exception:
        app_state.continuous = app_state.is_running = False
        release_monitor(token)
        raise
    return {'success': True, 'message': '监控已启动'}


@app.post('/api/stop')
async def stop_monitor():
    app_state.stop_event.set()
    app_state.continuous = app_state.is_running = False
    # Remove queued schedule; do not cancel a running executor and falsely release its lock.
    if app_state.scheduler and app_state.scheduler.running:
        app_state.scheduler.remove_all_jobs()
    app_state.next_run_time = None
    return {'success': True, 'message': '已请求停止，等待当前任务退出' if app_state.task_token else '监控已停止'}


@app.post('/api/run-once')
async def run_once(background_tasks: BackgroundTasks):
    token = reserve_monitor()
    if token is None:
        raise HTTPException(409, '已有检索任务执行或停止中，请勿重复提交')
    app_state.stop_event.clear()
    app_state.is_running = True
    app_state.next_run_time = None
    if app_state.scheduler and app_state.scheduler.running:
        app_state.scheduler.remove_all_jobs()
    background_tasks.add_task(run_monitor_task, token)
    app_state.add_log('手动检索已受理')
    return {'success': True, 'message': '已开始检索'}


@app.get("/api/config")
async def get_config():
    """获取配置"""
    return redacted(app_state.config)

@app.post("/api/config")
async def update_config(config: ConfigModel):
    """更新配置"""
    update_data = config.model_dump(exclude_unset=True, exclude_none=True)
    commit_config(update_data)
    
    # 如果正在运行且间隔时间改变，重新调度
    if app_state.is_running and 'interval' in update_data:
        new_interval = update_data['interval']
        if app_state.scheduler and app_state.scheduler.get_job('monitor_job'):
            app_state.scheduler.reschedule_job(
                'monitor_job',
                trigger=IntervalTrigger(minutes=new_interval)
            )
            app_state.add_log(f"⏱️ 检索间隔已调整为 {new_interval} 分钟")
    
    return {"success": True, "message": "配置已更新"}

@app.get("/api/sites")
async def get_sites():
    """获取可用网站列表"""
    sites = get_default_sites()
    enabled = app_state.config.get('enabled_sites', [])
    
    result = []
    for key, info in sites.items():
        result.append({
            "key": key,
            "name": info['name'],
            "url": info['url'],
            "enabled": key in enabled
        })
    
    return result

@app.post("/api/sites")
async def update_sites(enabled_sites: List[str]):
    """更新启用的网站"""
    commit_config({'enabled_sites': enabled_sites})
    return {"success": True, "message": "网站配置已更新"}

@app.get("/api/custom-sites")
async def get_custom_sites():
    """获取自定义网站列表"""
    return app_state.config.get('custom_sites', [])

@app.post("/api/custom-sites")
async def update_custom_sites(custom_sites: List[Dict[str, Any]]):
    """更新自定义网站列表"""
    commit_config({'custom_sites': custom_sites})
    app_state.add_log(f"📋 自定义网站已更新，共 {len(custom_sites)} 个")
    return {"success": True, "message": "自定义网站已更新"}

@app.get("/api/results")
async def get_results(limit: int = 50, offset: int = 0):
    """获取招标结果"""
    all_bids = app_state.storage.get_all() if hasattr(app_state.storage, 'get_all') else []
    # 新采集记录优先显示；没有发布日期的企业记录也不会沉到历史结果底部。
    all_bids.sort(key=lambda b: _local_created_at(b) or
                  datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    
    total = len(all_bids)
    bids = all_bids[offset:offset + limit]
    
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [_result_item(b) for b in bids]
    }

@app.get("/api/logs")
async def get_logs(limit: int = 100):
    """获取最近的日志"""
    return {
        "logs": app_state.logs[-limit:]
    }

@app.delete("/api/logs")
async def clear_logs():
    """清空日志"""
    app_state.logs = []
    return {"success": True, "message": "日志已清空"}

@app.delete("/api/history")
async def clear_history(scope: str = '', confirm: bool = False):
    if scope not in ('bids', 'competitors', 'all') or not confirm:
        raise HTTPException(400, '请选择清理范围并明确确认')
    token = reserve_monitor()
    if token is None:
        raise HTTPException(409, '检索执行中，不能清理历史')
    try:
        with competitor_service.lock:
            if competitor_service.state['running']:
                raise HTTPException(409, '竞品查询执行中，不能清理历史')
            app_state.storage.clear_all(scope)
    finally:
        release_monitor(token)
    labels = {'bids':'普通标讯','competitors':'竞品记录及版本','all':'全部标讯、竞品记录及版本'}
    app_state.add_log('已清理：' + labels[scope])
    return {'success': True, 'message': '已清理：' + labels[scope]}

@app.get("/api/contacts")
async def get_contacts():
    """获取联系人列表"""
    return redacted(app_state.config.get('contacts', []))

@app.post("/api/contacts")
async def update_contacts(contacts: List[Dict[str, Any]]):
    """更新联系人列表"""
    commit_config({'contacts': contacts})
    app_state.add_log(f"📋 联系人配置已更新，共 {len(contacts)} 人")
    return {"success": True, "message": "联系人已更新"}

@app.post("/api/config/full")
async def update_full_config(config: Dict[str, Any]):
    """更新完整配置（包括通知配置）"""
    if 'interval' in config and (not isinstance(config['interval'], int) or not 1 <= config['interval'] <= 10080):
        raise HTTPException(400, '检索间隔必须为1至10080分钟')
    commit_config(config)
    return {"success": True, "message": "配置已更新"}

# 测试通知请求模型
class TestNotifyRequest(BaseModel):
    phone: Optional[str] = None
    email: Optional[str] = None
    token: Optional[str] = None

@app.post("/api/test/voice")
async def test_voice(req: TestNotifyRequest):
    """测试语音呼叫"""
    if not req.phone:
        raise HTTPException(status_code=400, detail="请输入测试手机号")
    
    voice_config = app_state.config.get('voice_config', {})
    if not voice_config.get('access_key_id') or not voice_config.get('tts_code'):
        raise HTTPException(status_code=400, detail="请先配置语音API参数")
    
    try:
        from notifier.voice import VoiceNotifier
        notifier = VoiceNotifier(voice_config)
        success = notifier.call(req.phone, count=1, source="测试")
        if success:
            app_state.add_log(f"✅ 测试语音呼叫成功: {req.phone}")
            return {"success": True, "message": f"语音呼叫已发送到 {req.phone}"}
        else:
            app_state.add_log(f"❌ 测试语音呼叫失败: {req.phone}")
            return {"success": False, "message": "语音呼叫失败，请检查配置"}
    except Exception as e:
        app_state.add_log(f"❌ 测试语音呼叫异常: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/test/sms")
async def test_sms(req: TestNotifyRequest):
    """测试短信发送"""
    if not req.phone:
        raise HTTPException(status_code=400, detail="请输入测试手机号")
    
    sms_config = app_state.config.get('sms_config', {})
    if not sms_config.get('access_key_id') or not sms_config.get('template_code'):
        raise HTTPException(status_code=400, detail="请先配置短信API参数")
    
    try:
        from notifier.sms import SMSNotifier
        notifier = SMSNotifier(sms_config)
        success = notifier.send_test(req.phone)
        if success:
            app_state.add_log(f"✅ 测试短信发送成功: {req.phone}")
            return {"success": True, "message": f"测试短信已发送到 {req.phone}"}
        else:
            app_state.add_log(f"❌ 测试短信发送失败: {req.phone}")
            return {"success": False, "message": "短信发送失败，请检查配置"}
    except Exception as e:
        app_state.add_log(f"❌ 测试短信发送异常: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/test/email")
async def test_email(req: TestNotifyRequest):
    """测试邮件发送"""
    if not req.email:
        raise HTTPException(status_code=400, detail="请输入测试邮箱地址")
    
    # 从联系人或配置中获取邮箱配置
    contacts = app_state.config.get('contacts', [])
    contact_config = None
    for contact in contacts:
        if contact.get('email') == req.email and contact.get('email_password'):
            contact_config = contact
            break
    
    if not contact_config:
        raise HTTPException(status_code=400, detail="未找到该邮箱的配置，请先在联系人中配置邮箱和授权码")
    
    # 根据邮箱类型配置SMTP服务器
    email_type = contact_config.get('email_type', 'QQ邮箱')
    smtp_configs = {
        'QQ邮箱': {'smtp_server': 'smtp.qq.com', 'smtp_port': 465, 'use_ssl': True},
        '163邮箱': {'smtp_server': 'smtp.163.com', 'smtp_port': 465, 'use_ssl': True},
        'Gmail': {'smtp_server': 'smtp.gmail.com', 'smtp_port': 587, 'use_ssl': False},
        'Outlook': {'smtp_server': 'smtp.office365.com', 'smtp_port': 587, 'use_ssl': False},
        '企业邮箱': {'smtp_server': 'smtp.exmail.qq.com', 'smtp_port': 465, 'use_ssl': True},
    }
    smtp_config = smtp_configs.get(email_type, smtp_configs['QQ邮箱'])
    
    email_config = {
        'smtp_server': smtp_config['smtp_server'],
        'smtp_port': smtp_config['smtp_port'],
        'use_ssl': smtp_config['use_ssl'],
        'sender': contact_config['email'],
        'password': contact_config['email_password'],
        'receiver': contact_config['email'],  # 发送给自己作为测试
    }
    
    try:
        from notifier.email import EmailNotifier
        notifier = EmailNotifier(email_config)
        success = notifier.send_test()
        if success:
            app_state.add_log(f"✅ 测试邮件发送成功: {req.email}")
            return {"success": True, "message": f"测试邮件已发送到 {req.email}"}
        else:
            app_state.add_log(f"❌ 测试邮件发送失败: {req.email}")
            return {"success": False, "message": "邮件发送失败，请检查授权码是否正确"}
    except Exception as e:
        app_state.add_log(f"❌ 测试邮件发送异常: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/test/wechat")
async def test_wechat(req: TestNotifyRequest):
    """测试微信推送"""
    if not req.token:
        raise HTTPException(status_code=400, detail="请输入PushPlus Token")
    
    try:
        from notifier.wechat import WeChatNotifier
        notifier = WeChatNotifier({'provider': 'pushplus', 'token': req.token})
        success = notifier.send_test()
        if success:
            app_state.add_log(f"✅ 测试微信推送成功")
            return {"success": True, "message": "微信推送已发送，请检查微信"}
        else:
            app_state.add_log(f"❌ 测试微信推送失败")
            return {"success": False, "message": "微信推送失败，请检查Token是否正确"}
    except Exception as e:
        app_state.add_log(f"❌ 测试微信推送异常: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/test/ai")
async def test_ai():
    """测试AI配置"""
    ai_config = app_state.config.get('ai_config', {})
    if not ai_config.get('api_key'):
        raise HTTPException(status_code=400, detail="请先配置AI API Key")
    
    try:
        import requests
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f"Bearer {ai_config['api_key']}"
        }
        data = {
            'model': ai_config.get('model', 'deepseek-chat'),
            'messages': [{'role': 'user', 'content': '你好，这是一条测试消息，请用一句话回复'}],
            'max_tokens': 50
        }
        base_url = ai_config.get('base_url', 'https://api.deepseek.com/chat/completions')
        response = requests.post(base_url, headers=headers, json=data, timeout=30)
        result = response.json()
        
        if response.status_code == 200 and 'choices' in result:
            reply = result['choices'][0]['message']['content']
            app_state.add_log(f"✅ AI测试成功: {reply[:50]}")
            return {"success": True, "message": f"AI测试成功！回复: {reply[:100]}"}
        else:
            error_msg = result.get('error', {}).get('message', str(result))
            app_state.add_log(f"❌ AI测试失败: {error_msg}")
            return {"success": False, "message": f"AI测试失败: {error_msg}"}
    except Exception as e:
        app_state.add_log(f"❌ AI测试异常: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# 主入口
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)


