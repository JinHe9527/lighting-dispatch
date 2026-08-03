#!/usr/bin/env python3
"""
灯光派工系统 · 企业级后端
================================
- 身份:飞书 OAuth 登录(真名+头像)
- 权限:服务器端 RBAC 强制 —— team/staff/lead/admin
- 数据:SQLite app/dispatch.db(WAL),首启自动从 data.json 迁移 teams+sla
- 审计:所有变更记录 谁/何时/干了什么
- 通知:飞书机器人私信(派单/抢单)
- 依赖:Python 3.9 标准库,零 pip

运行:  python3 app/server.py     → http://<本机IP>:8848
环境(.env,项目根):
  DISPATCH_APP_ID / DISPATCH_APP_SECRET   「灯光派工」飞书应用凭据(配好后启用真实登录+通知)
  DISPATCH_BASE_URL                        HTTPS 对外地址(OAuth 回调用)
  DISPATCH_HOST / DISPATCH_PORT            默认 0.0.0.0 / 8848
"""
import ipaddress
import json
import logging
import os
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from . import business, service
except ImportError:
    import business
    import service

PROJECT = ROOT.parent
LEGACY_JSON = ROOT / "data.json"
FEISHU = "https://open.feishu.cn/open-apis"
AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"

SESSION_DAYS = 30
TECH_STATUS = ("free", "busy", "check", "rest", "off")
ORDER_STATUS = business.ORDER_STATUS
ROLES = ("team", "staff", "admin")
TECH_ROLES = ("灯光师", "学徒")
SHIFTS = ("早班", "中班", "晚班", "机动")
TEST_PROFILES = {
    "team": {"open_id": "test:team", "name": "测试团队", "role": "team", "status": "off", "grp": "", "lead": 0},
    "staff": {"open_id": "test:staff", "name": "测试师傅", "role": "staff", "status": "free", "grp": "灯光组", "lead": 0},
    "lead": {"open_id": "test:lead", "name": "测试组长", "role": "staff", "status": "free", "grp": "灯光组", "lead": 1},
    "admin": {"open_id": "test:admin", "name": "测试管理员", "role": "admin", "status": "free", "grp": "视觉组", "lead": 1},
}


# ───────────────────────── env ─────────────────────────
def load_dotenv():
    p = PROJECT / ".env"
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()
APP_ID = os.environ.get("DISPATCH_APP_ID", "").strip()
APP_SECRET = os.environ.get("DISPATCH_APP_SECRET", "").strip()
BASE_URL = os.environ.get("DISPATCH_BASE_URL", "").strip().rstrip("/") or "http://localhost:8848"
COOKIE_SECURE = "; Secure" if BASE_URL.lower().startswith("https://") else ""
HOST = os.environ.get("DISPATCH_HOST", "0.0.0.0")
PORT = int(os.environ.get("DISPATCH_PORT", "8848"))
TEST_MODE = os.environ.get("DISPATCH_TEST_MODE", "").strip().lower() in ("1", "true", "yes", "on")
SERVER_VERSION = "v0803c"  # 与 index.html 的 APP_VERSION 同步改;前端会校验,不一致提示重启服务
DATA_ROOT = Path(os.environ.get("DISPATCH_DATA_DIR", "").strip()) if os.environ.get("DISPATCH_DATA_DIR", "").strip() \
    else ROOT
DB_PATH = Path(os.environ.get("DISPATCH_DB_PATH", "").strip()) if os.environ.get("DISPATCH_DB_PATH", "").strip() \
    else DATA_ROOT / ("dispatch-test.db" if TEST_MODE else "dispatch.db")
FEISHU_CONFIGURED = bool(APP_ID and APP_SECRET) and not TEST_MODE
MAX_REQUEST_BYTES = 64 * 1024
MEDIA_DIR = DATA_ROOT / "media"
MEDIA_MAX_BYTES = 30 * 1024 * 1024
MEDIA_PENDING_FILE_LIMIT = 6
MEDIA_PENDING_BYTES = 180 * 1024 * 1024
MEDIA_DISK_RESERVE_BYTES = 512 * 1024 * 1024
LOG_DIR = DATA_ROOT / "logs"
BACKUP_DIR = Path(os.environ.get("DISPATCH_BACKUP_DIR", "").strip()) if os.environ.get("DISPATCH_BACKUP_DIR", "").strip() \
    else DATA_ROOT / "backups"
BACKUP_MIRROR_DIR = Path(os.environ.get("DISPATCH_BACKUP_MIRROR_DIR", "").strip()) \
    if os.environ.get("DISPATCH_BACKUP_MIRROR_DIR", "").strip() else None
PROCESS_STARTED = time.time()
# 上传 content-type → 落盘扩展名
MEDIA_EXT = {
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a", "audio/aac": "m4a",
    "audio/x-m4a": "m4a", "audio/wav": "wav", "audio/x-wav": "wav", "audio/webm": "weba",
    "audio/ogg": "ogg", "audio/3gpp": "3gp",
    "video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov",
    "video/3gpp": "3gp", "video/x-matroska": "mkv",
}
# 扩展名 → 回放 content-type
EXT_CONTENT_TYPE = {
    "mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav", "weba": "audio/webm",
    "ogg": "audio/ogg", "3gp": "video/3gpp", "mp4": "video/mp4", "webm": "video/webm",
    "mov": "video/quicktime", "mkv": "video/x-matroska",
}


# ───────────────────────── db ─────────────────────────
def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  open_id   TEXT PRIMARY KEY,
  union_id  TEXT DEFAULT '',
  name      TEXT NOT NULL,
  avatar    TEXT DEFAULT '',
  role      TEXT NOT NULL DEFAULT 'team',
  tech_role TEXT DEFAULT '灯光师',
  shift     TEXT DEFAULT '中班',
  status    TEXT DEFAULT 'off',
  task      TEXT DEFAULT '',
  work_hours TEXT DEFAULT '',
  created_at TEXT,
  last_login TEXT
);
CREATE TABLE IF NOT EXISTS teams(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, room TEXT DEFAULT '',
  rehearsal TEXT DEFAULT '', onair TEXT DEFAULT '',
  lighting_owner_id TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sla(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_type TEXT NOT NULL, minutes INTEGER NOT NULL DEFAULT 30,
  buffer INTEGER NOT NULL DEFAULT 10, note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS shifts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, start TEXT DEFAULT '', end TEXT DEFAULT '', sort INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS groups(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, color TEXT DEFAULT '#8E8E93', sort INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL, team TEXT NOT NULL, room TEXT DEFAULT '',
  task_type TEXT NOT NULL, minutes INTEGER NOT NULL,
  base_time TEXT DEFAULT '', arrive TEXT DEFAULT '',
  assignee_id TEXT DEFAULT '', assignee_name TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  creator_id TEXT DEFAULT '', creator_name TEXT DEFAULT '',
  note TEXT DEFAULT '', media TEXT DEFAULT '', media_kind TEXT DEFAULT '',
  created_at TEXT,
  accepted_at TEXT DEFAULT '', started_at TEXT DEFAULT '', done_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sessions(
  sid TEXT PRIMARY KEY, open_id TEXT NOT NULL,
  created_at TEXT, expires_at TEXT
);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, open_id TEXT, name TEXT,
  action TEXT NOT NULL, detail TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS idx_teams_name ON teams(name);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sla_task_type ON sla(task_type);
CREATE UNIQUE INDEX IF NOT EXISTS idx_shifts_name ON shifts(name);
CREATE INDEX IF NOT EXISTS idx_orders_status_assignee ON orders(status,assignee_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
"""

DEFAULT_SLA = [
    ("常规搭场", 30, 10, "换直播间 / 新场搭建"),
    ("彩排陪场", 45, 10, "彩排全程盯场"),
    ("场景微调", 10, 5, "亮度/色温小改"),
    ("画面检查", 10, 5, "巡查出图效果"),
    ("新团首搭", 60, 15, "新团首次搭光,存预设"),
]

DEFAULT_SHIFTS = [
    ("早班", "09:00", "18:00", 1),
    ("中班", "14:00", "23:00", 2),
    ("晚班", "18:00", "03:00", 3),
]

# 三个组(可在配置页增删/改色)。grp 列存组名字符串。
DEFAULT_GROUPS = [
    ("灯光组", "#FF9F0A", 1),
    ("设备组", "#32ADE6", 2),
    ("视觉组", "#AF52DE", 3),
]
FIRST_GROUP = DEFAULT_GROUPS[0][0]  # 灯光组:历史数据归入此组
DEFAULT_GROUP_SLA = {
    "灯光组": DEFAULT_SLA,
    "设备组": [
        ("设备调试", 20, 10, "设备/线路检查"),
        ("声卡麦调试", 15, 5, "音频设备"),
        ("网络排障", 20, 10, "网络/推流"),
        ("机位搬运", 30, 10, "机位/支架"),
        ("开播前检查", 10, 5, "全套设备巡检"),
    ],
    "视觉组": [
        ("字幕包装", 20, 5, "字幕/包装条"),
        ("画面切换", 15, 5, "导播切换"),
        ("转场/贴纸", 10, 5, "转场特效"),
        ("OBS 场景", 20, 10, "OBS 场景搭建"),
        ("绿幕抠像", 25, 10, "抠像/虚拟背景"),
    ],
}


def ensure_columns(conn, table, cols):
    """给已存在的表补列(SQLite ADD COLUMN 不损数据),用于线上库平滑升级。"""
    have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in cols.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init_db():
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    conn = db()
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    ensure_columns(conn, "orders", {
        "note": "TEXT DEFAULT ''", "media": "TEXT DEFAULT ''", "media_kind": "TEXT DEFAULT ''",
        "accepted_at": "TEXT DEFAULT ''", "started_at": "TEXT DEFAULT ''", "done_at": "TEXT DEFAULT ''",
        "feishu_task_guid": "TEXT DEFAULT ''", "urgent": "INTEGER DEFAULT 0",
        "grp": "TEXT DEFAULT ''", "done_note": "TEXT DEFAULT ''",
        "media2": "TEXT DEFAULT ''", "media2_kind": "TEXT DEFAULT ''",
        "booked": "INTEGER DEFAULT 0",
        "overrun_notified": "INTEGER DEFAULT 0",
        "duration_review": "TEXT DEFAULT ''",
        "duration_assignee_id": "TEXT DEFAULT ''",
    })
    ensure_columns(conn, "users", {"grp": "TEXT DEFAULT ''", "lead": "INTEGER DEFAULT 0",
                                    "intern": "INTEGER DEFAULT 0", "leave_until": "TEXT DEFAULT ''",
                                    "alias": "TEXT DEFAULT ''",
                                    "approved": "INTEGER DEFAULT 0",
                                    "blocked": "INTEGER DEFAULT 0",
                                    "read_only": "INTEGER DEFAULT 0"})
    ensure_columns(conn, "teams", {"lighting_owner_id": "TEXT DEFAULT ''"})
    # 准入门:老用户一次性放行;此后新的飞书登录默认「待审核」,管理员在人员页放行
    if conn.execute("SELECT v FROM settings WHERE k='approved_migrated'").fetchone() is None:
        conn.execute("UPDATE users SET approved=1")
        conn.execute("INSERT OR REPLACE INTO settings(k,v) VALUES('approved_migrated','1')")
    # 内部系统默认人工准入；旧的自动放行设置升级后立即收紧，既有已授权账号不受影响。
    conn.execute("INSERT OR IGNORE INTO settings(k,v) VALUES('gate_mode','manual')")
    conn.execute("UPDATE settings SET v='manual' WHERE k='gate_mode' AND v='open'")
    conn.execute("INSERT OR IGNORE INTO settings(k,v) VALUES('feishu_calendar_name','团播派工排期')")
    ensure_columns(conn, "sla", {"grp": "TEXT DEFAULT ''"})
    ensure_columns(conn, "groups", {"auto": "INTEGER DEFAULT 0"})
    # 组:种子三个组 + 历史数据归入首个组；默认任务只在数据库首次初始化时创建。
    conn.execute("DROP INDEX IF EXISTS idx_sla_task_type")  # 任务名允许跨组重复
    if conn.execute("SELECT COUNT(*) c FROM groups").fetchone()["c"] == 0:
        conn.executemany("INSERT INTO groups(name,color,sort) VALUES(?,?,?)", DEFAULT_GROUPS)
    conn.execute("UPDATE sla SET grp=? WHERE grp='' OR grp IS NULL", (FIRST_GROUP,))
    conn.execute("UPDATE users SET grp=? WHERE role='staff' AND (grp='' OR grp IS NULL)", (FIRST_GROUP,))
    conn.execute("UPDATE orders SET grp=? WHERE grp='' OR grp IS NULL", (FIRST_GROUP,))
    task_defaults_initialized = conn.execute(
        "SELECT v FROM settings WHERE k='task_defaults_initialized'"
    ).fetchone()
    if task_defaults_initialized is None:
        if conn.execute("SELECT COUNT(*) c FROM sla").fetchone()["c"] == 0:
            for gname, tasks in DEFAULT_GROUP_SLA.items():
                conn.executemany(
                    "INSERT INTO sla(task_type,minutes,buffer,note,grp) VALUES(?,?,?,?,?)",
                    [(t[0], t[1], t[2], t[3], gname) for t in tasks],
                )
        conn.execute("INSERT INTO settings(k,v) VALUES('task_defaults_initialized','1')")
    # 早期版本会在每次启动时强制补回“编歌”。只移除该系统种子一次，此后任务完全由管理员维护。
    if conn.execute("SELECT v FROM settings WHERE k='legacy_biange_seed_removed'").fetchone() is None:
        conn.execute(
            """DELETE FROM sla WHERE grp='灯光组' AND task_type='编歌'
               AND minutes=60 AND buffer=10 AND note='需附歌曲音频或视频'"""
        )
        conn.execute("INSERT INTO settings(k,v) VALUES('legacy_biange_seed_removed','1')")
    # 迁移:teams / sla 为空时,从 data.json(真数据)导入,否则用默认 SLA
    if conn.execute("SELECT COUNT(*) c FROM teams").fetchone()["c"] == 0:
        rows = []
        if LEGACY_JSON.exists():
            try:
                legacy = json.loads(LEGACY_JSON.read_text(encoding="utf-8"))
                for t in legacy.get("teams", []):
                    rows.append((t.get("团队名", ""), t.get("直播间", ""),
                                 t.get("彩排时间", ""), t.get("开播时间", "")))
            except Exception as e:
                print(f"[migrate] data.json teams 读取失败: {e}")
        rows = [r for r in rows if r[0]]
        if rows:
            conn.executemany("INSERT INTO teams(name,room,rehearsal,onair) VALUES(?,?,?,?)", rows)
            print(f"[migrate] 已从 data.json 导入 {len(rows)} 个团队")
    if task_defaults_initialized is None and conn.execute(
        "SELECT COUNT(*) c FROM sla"
    ).fetchone()["c"] == 0:
        rows = []
        if LEGACY_JSON.exists():
            try:
                legacy = json.loads(LEGACY_JSON.read_text(encoding="utf-8"))
                for s in legacy.get("sla", []):
                    rows.append((s.get("任务类型", ""), int(s.get("标准工时(分钟)") or 30),
                                 int(s.get("缓冲(分钟)") or 10), s.get("说明", "")))
            except Exception:
                pass
        rows = [r for r in rows if r[0]] or list(DEFAULT_SLA)
        conn.executemany("INSERT INTO sla(task_type,minutes,buffer,note) VALUES(?,?,?,?)", rows)
        print(f"[migrate] SLA {len(rows)} 条就绪")
    if conn.execute("SELECT COUNT(*) c FROM shifts").fetchone()["c"] == 0:
        conn.executemany("INSERT INTO shifts(name,start,end,sort) VALUES(?,?,?,?)", DEFAULT_SHIFTS)
        print(f"[migrate] 班次 {len(DEFAULT_SHIFTS)} 条就绪")
    # 管理员引导码
    if not conn.execute("SELECT v FROM settings WHERE k='bootstrap_code'").fetchone():
        code = f"{secrets.randbelow(900000)+100000}"
        conn.execute("INSERT INTO settings(k,v) VALUES('bootstrap_code',?)", (code,))
        conn.execute("INSERT OR REPLACE INTO settings(k,v) VALUES('bootstrap_used','0')")
    if TEST_MODE:
        for profile in TEST_PROFILES.values():
            conn.execute(
                """INSERT OR IGNORE INTO users(
                       open_id,name,role,status,grp,lead,created_at,last_login
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (profile["open_id"], profile["name"], profile["role"], profile["status"],
                 profile.get("grp", ""), profile.get("lead", 0), now_iso(), now_iso()),
            )
        conn.execute("UPDATE settings SET v='1' WHERE k='bootstrap_used'")
    business.migrate(conn)
    conn.execute(
        """UPDATE teams SET lighting_owner_id=''
           WHERE lighting_owner_id!='' AND NOT EXISTS(
             SELECT 1 FROM users u WHERE u.open_id=teams.lighting_owner_id
             AND u.role='staff' AND u.grp='灯光组' AND COALESCE(u.intern,0)=0
             AND COALESCE(u.approved,0)=1 AND COALESCE(u.blocked,0)=0
           )"""
    )
    # 账号角色决定唯一的数据作用域，清理旧版本留下的跨角色字段。
    conn.execute(
        """UPDATE users
           SET team_id=NULL,read_only=0
           WHERE role='staff'"""
    )
    conn.execute(
        """UPDATE users
           SET grp='',lead=0,intern=0,read_only=0,status='off',task=''
           WHERE role='team'"""
    )
    conn.execute(
        """UPDATE users
           SET team_id=NULL,grp='',lead=0,intern=0,status='off',task=''
           WHERE role='admin'"""
    )
    conn.execute(
        """UPDATE users
           SET approved=0
           WHERE role='team' AND approved=1 AND blocked=0 AND team_id IS NULL"""
    )
    conn.execute(
        """UPDATE notification_outbox
           SET status='retry',available_at=?,claimed_at='',
               last_error=CASE WHEN last_error='' THEN '服务重启后恢复未完成发送' ELSE last_error END
           WHERE status='sending'""",
        (now_iso(),),
    )
    if TEST_MODE:
        conn.execute(
            "UPDATE users SET approved=1 WHERE open_id LIKE 'test:%'"
        )
        conn.execute(
            "UPDATE users SET team_id=(SELECT id FROM teams ORDER BY id LIMIT 1) WHERE open_id='test:team'"
        )
    conn.commit()
    conn.close()


def setting(k, default=""):
    conn = db()
    row = conn.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    conn.close()
    return row["v"] if row else default


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


BIZ_DAY_CUTOFF_H = 5  # 排班日分界:凌晨5点前仍算前一天的排班日(排班跨夜约定,与前端一致)


def biz_now():
    """排班日视角的「现在」:整体回拨5小时,凌晨0-5点归属前一天。"""
    return datetime.now() - timedelta(hours=BIZ_DAY_CUTOFF_H)


def today():
    """排班日「今天」:凌晨5点前返回前一天日期,保证跨夜排班不错位。"""
    return biz_now().strftime("%Y-%m-%d")


SERVICE = service.DispatchService(db, setting, today, BASE_URL)


def parse_ts(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def used_minutes(started, done):
    """实际用时(分钟):开始→完成。缺时间戳或负值返回 None(不计入统计)。"""
    a, b = parse_ts(started), parse_ts(done)
    if not a or not b:
        return None
    d = (b - a).total_seconds() / 60.0
    return round(d, 1) if d >= 0 else None


def insert_audit(conn, user, action, detail=""):
    conn.execute(
        "INSERT INTO audit(ts,open_id,name,action,detail) VALUES(?,?,?,?,?)",
        (
            now_iso(),
            user["open_id"] if user else "",
            user["name"] if user else "系统",
            action,
            str(detail or "")[:500],
        ),
    )


def audit_log(user, action, detail=""):
    try:
        conn = db()
        insert_audit(conn, user, action, detail)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[audit] 写入失败: {e}")


def minus(hhmm, mins):
    try:
        h, m = map(int, str(hhmm).split(":"))
    except Exception:
        return ""
    t = (h * 60 + m - mins) % 1440
    return f"{t // 60:02d}:{t % 60:02d}"


def first_schedule_time(value):
    candidate = str(value or "").split("/", 1)[0].strip()
    try:
        hour, minute = map(int, candidate.split(":"))
    except (TypeError, ValueError):
        return ""
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return ""
    return f"{hour:02d}:{minute:02d}"


def valid_future_date(value):
    """校验预约日期 YYYY-MM-DD,不能早于排班日「今天」(凌晨仍可约昨晚排班日的时段);返回规范化字符串或 None。"""
    try:
        d = datetime.strptime(str(value or "").strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    if d.strftime("%Y-%m-%d") < today():
        return None
    return d.strftime("%Y-%m-%d")


def hm_to_min(value):
    try:
        h, m = map(int, str(value or "").strip().split(":"))
    except (TypeError, ValueError):
        return None
    return h * 60 + m if 0 <= h <= 23 and 0 <= m <= 59 else None


def shift_covers(conn, shift_name, arrive):
    """该班次是否覆盖 arrive(HH:MM)时刻。班次未设或时间无效→True(不拦)。晚班可跨夜。"""
    t = hm_to_min(arrive)
    if t is None:
        return True
    row = conn.execute("SELECT start,end FROM shifts WHERE name=?", (shift_name or "",)).fetchone()
    if not row:
        return True
    a, b = hm_to_min(row["start"]), hm_to_min(row["end"])
    if a is None or b is None:
        return True
    return a <= t < b if b > a else (t >= a or t < b)


def assign_block(conn, u, order_date, arrive):
    """成员 u 能否被指派到 (order_date, arrive) 的工单;返回拦截原因字符串,可派则 None。
    规则:下单方不参与;实习不可派;当天请假不可派;到岗时刻不在其班次(下班)不可派。"""
    if u["role"] == "team":
        return "对方是下单方,不参与派工"
    if u["intern"]:
        return f"{dn(u)} 是实习成员,不可派单"
    lv = u["leave_until"] or ""
    if lv and order_date and lv >= order_date:
        return f"{dn(u)} 当天请假,不可派单"
    if not shift_covers(conn, u["shift"], arrive):
        return f"{dn(u)} 在 {arrive} 不在班(下班),不可派单"
    return None


def pick_auto_assignee(conn, grp, order_date, arrive, preferred_id=""):
    """自动派单:本职负责人可派时优先，否则在本组可派人员中公平轮转。"""
    cands = conn.execute(
        "SELECT * FROM users WHERE grp=? AND role!='team' AND status='free' "
        "AND COALESCE(intern,0)=0", (grp,)).fetchall()
    best, best_load = None, None
    for u in cands:
        if assign_block(conn, u, order_date, arrive):
            continue
        if preferred_id and u["open_id"] == preferred_id:
            return u
        load = conn.execute("SELECT COUNT(*) c FROM orders WHERE assignee_id=? AND date=?",
                            (u["open_id"], order_date)).fetchone()["c"]
        if best is None or load < best_load:
            best, best_load = u, load
    return best


def record_id(value):
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


# ───────────────────────── 飞书 API ─────────────────────────
def feishu_api(method, path, token=None, body=None, query=None, base=FEISHU, retries=2):
    url = base + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                last = e
        except Exception as e:
            last = e
        if attempt < retries:
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"飞书请求失败 {path}: {last}")


_tenant = {"v": None, "exp": 0}


def tenant_token():
    """「灯光派工」应用的 tenant_access_token(发通知用)。"""
    if _tenant["v"] and time.time() < _tenant["exp"]:
        return _tenant["v"]
    r = feishu_api("POST", "/auth/v3/tenant_access_token/internal/",
                   body={"app_id": APP_ID, "app_secret": APP_SECRET})
    if r.get("code") != 0:
        raise RuntimeError(f"tenant_token 失败: {r}")
    _tenant["v"] = r["tenant_access_token"]
    _tenant["exp"] = time.time() + r.get("expire", 7000) - 120
    return _tenant["v"]


def oauth_exchange(code):
    """用授权码换 user_access_token。"""
    r = feishu_api("POST", "/authen/v2/oauth/token", body={
        "grant_type": "authorization_code",
        "client_id": APP_ID, "client_secret": APP_SECRET,
        "code": code, "redirect_uri": BASE_URL + "/auth/callback",
    })
    tok = r.get("access_token") or (r.get("data") or {}).get("access_token")
    if not tok:
        raise RuntimeError(f"OAuth 换 token 失败: {r}")
    return tok


def fetch_user_info(user_token):
    r = feishu_api("GET", "/authen/v1/user_info", token=user_token)
    d = r.get("data") or {}
    if not d.get("open_id"):
        raise RuntimeError(f"user_info 失败: {r}")
    return {"open_id": d["open_id"], "union_id": d.get("union_id", ""),
            "name": d.get("name") or d.get("en_name") or "未命名",
            "avatar": d.get("avatar_url") or d.get("avatar_thumb") or ""}


def user_dept_ids(open_id):
    """查用户所在部门(open_department_id 列表);需应用有通讯录读取权限,失败返回 None。"""
    try:
        r = feishu_api("GET", f"/contact/v3/users/{open_id}", token=tenant_token(),
                       query={"user_id_type": "open_id", "department_id_type": "open_department_id"})
        u = (r.get("data") or {}).get("user") or {}
        ids = u.get("department_ids") or []
        return ids if isinstance(ids, list) else []
    except Exception as e:
        print(f"[gate] 查部门失败: {e}")
        return None


def apply_login_gate(open_id, name):
    """登录准入。dept=登录时按部门自动校验(需通讯录权限+部门ID);
    manual=新账号需管理员人工放行；被禁用账号始终拒绝。
    返回 None=放行继续登录,返回字符串=拒绝登录并显示原因。"""
    mode = setting("gate_mode", "manual")
    conn = db()
    try:
        account = conn.execute(
            "SELECT COALESCE(blocked,0) blocked FROM users WHERE open_id=?", (open_id,)
        ).fetchone()
        if account and account["blocked"]:
            audit_log({"open_id": open_id, "name": name}, "登录被拒", "账号已被管理员禁用")
            return "此账号未获准使用团播派工系统。如有疑问，请联系管理员。"
        if mode == "dept":
            allow = {x.strip() for x in setting("allow_dept_ids", "").split(",") if x.strip()}
            depts = user_dept_ids(open_id) if allow else None
            if depts is not None and allow:
                if allow & set(depts):
                    conn.execute("UPDATE users SET approved=1 WHERE open_id=?", (open_id,))
                    conn.commit()
                    return None
                audit_log({"open_id": open_id, "name": name}, "登录被拒", "飞书账号不在允许的部门内")
                return "此系统仅限无忧传媒团播部门使用:你的飞书账号不在允许的部门内。如有疑问请联系管理员。"
            audit_log({"open_id": open_id, "name": name}, "待审核", "部门校验不可用(缺通讯录权限或未填部门ID),转人工审核")
            return None
        return None  # manual:保持 approved 现状,新账号由管理员在人员页放行
    finally:
        conn.close()


def notify(open_id, text, urgent=False):
    """飞书私信在后台线程发送,发送失败不回滚业务变更。
    urgent=True 且设置里开启「加急」时,对该消息发起飞书应用内加急(强提醒,需 im 加急权限审核通过)。"""
    if TEST_MODE or not FEISHU_CONFIGURED or not open_id:
        return
    do_urgent = urgent and setting("urgent_on", "0") == "1"

    def _send():
        try:
            r = feishu_api("POST", "/im/v1/messages", token=tenant_token(),
                           query={"receive_id_type": "open_id"},
                           body={"receive_id": open_id, "msg_type": "text",
                                 "content": json.dumps({"text": text}, ensure_ascii=False)})
            if do_urgent:
                mid = (r.get("data") or {}).get("message_id")
                if mid:
                    ur = feishu_api("PATCH", f"/im/v1/messages/{mid}/urgent_app",
                                    token=tenant_token(), query={"user_id_type": "open_id"},
                                    body={"user_id_list": [open_id]})
                    if ur.get("code") not in (0, None):
                        print(f"[urgent] 加急失败(可能权限未开通): {ur.get('msg')}")
        except Exception as e:
            print(f"[notify] 发送失败(不影响业务): {e}")

    threading.Thread(target=_send, daemon=True).start()


def notify_free_staff(text, grp, arrive="", urgent=False):
    """通知某组当前可抢单的成员:空闲 + 在班 + 非实习 + 未请假。"""
    conn = db()
    rows = conn.execute(
        "SELECT open_id,shift,leave_until FROM users "
        "WHERE grp=? AND role!='team' AND status='free' AND COALESCE(intern,0)=0", (grp,)
    ).fetchall()
    tdy = today()
    targets = []
    for r in rows:
        lv = r["leave_until"] or ""
        if lv and lv >= tdy:
            continue
        if not shift_covers(conn, r["shift"], arrive):
            continue
        targets.append(r["open_id"])
    conn.close()
    for oid in targets:
        notify(oid, text, urgent=urgent)


def reminder_conf():
    """日程提醒配置:(是否开启, 提前几分钟)。"""
    on = setting("reminder_on", "1") == "1"
    try:
        minutes = max(0, min(180, int(setting("reminder_minutes", "15") or 15)))
    except ValueError:
        minutes = 15
    return on, minutes


def _order_due_ms(date_str, arrive):
    """到岗时间 → 毫秒时间戳;排班日约定:凌晨(05:00前)时段属次日实际日历日。"""
    try:
        dt = datetime.strptime(f"{date_str} {arrive}", "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None
    if (arrive or "") < "05:00":
        dt += timedelta(days=1)
    return int(dt.timestamp()) * 1000


def create_task_reminder(order_id, assignee_open_id, summary, description, date_str, arrive):
    """给灯光师建一条带截止时间 + 提前提醒的飞书任务(后台线程,失败不影响业务)。"""
    on, minutes = reminder_conf()
    if TEST_MODE or not FEISHU_CONFIGURED or not assignee_open_id or not on:
        return
    due_ms = _order_due_ms(date_str, arrive)

    def _run():
        try:
            body = {"summary": summary, "description": description}
            if due_ms:
                body["due"] = {"timestamp": str(due_ms), "is_all_day": False}
                body["reminders"] = [{"relative_fire_minute": minutes}]
            r = feishu_api("POST", "/task/v2/tasks", token=tenant_token(),
                           query={"user_id_type": "open_id"}, body=body)
            guid = ((r.get("data") or {}).get("task") or {}).get("guid")
            if not guid:
                print(f"[task] 建任务失败: {r.get('msg')}")
                return
            feishu_api("POST", f"/task/v2/tasks/{guid}/add_members", token=tenant_token(),
                       query={"user_id_type": "open_id"},
                       body={"members": [{"id": assignee_open_id, "role": "assignee", "type": "user"}]})
            conn = db()
            conn.execute("UPDATE orders SET feishu_task_guid=? WHERE id=?", (guid, order_id))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[task] 提醒创建失败(不影响业务): {e}")

    threading.Thread(target=_run, daemon=True).start()


def delete_task_reminder(guid):
    """完成/删单时移除对应的飞书任务提醒(后台线程)。"""
    if TEST_MODE or not FEISHU_CONFIGURED or not guid:
        return

    def _run():
        try:
            feishu_api("DELETE", f"/task/v2/tasks/{guid}", token=tenant_token())
        except Exception as e:
            print(f"[task] 删除任务失败: {e}")

    threading.Thread(target=_run, daemon=True).start()


def _persist_outbox_progress(row_id, progress, external_id=None):
    conn = db()
    if external_id is None:
        conn.execute(
            "UPDATE notification_outbox SET progress=? WHERE id=?",
            (progress, row_id),
        )
    else:
        conn.execute(
            "UPDATE notification_outbox SET progress=?,external_id=? WHERE id=?",
            (progress, external_id, row_id),
        )
    conn.commit()
    conn.close()


def _send_message_now(row, payload):
    progress = row["progress"] or ""
    message_id = row["external_id"] or ""
    if progress not in ("message_sent", "urgent_sent"):
        result = feishu_api(
            "POST", "/im/v1/messages", token=tenant_token(),
            query={"receive_id_type": "open_id"},
            body={
                "receive_id": row["recipient_id"],
                "msg_type": "text",
                "content": json.dumps({"text": payload.get("text") or ""}, ensure_ascii=False),
                "uuid": row["dedupe_key"][:50],
            },
        )
        if result.get("code") not in (0, None):
            raise RuntimeError(result.get("msg") or "飞书消息发送失败")
        message_id = (result.get("data") or {}).get("message_id") or ""
        if not message_id:
            raise RuntimeError("飞书消息发送成功但未返回消息编号")
        _persist_outbox_progress(row["id"], "message_sent", message_id)
        progress = "message_sent"
    if payload.get("urgent") and setting("urgent_on", "0") == "1" and progress != "urgent_sent":
        urgent_result = feishu_api(
            "PATCH", f"/im/v1/messages/{message_id}/urgent_app", token=tenant_token(),
            query={"user_id_type": "open_id"}, body={"user_id_list": [row["recipient_id"]]},
        )
        if urgent_result.get("code") not in (0, None):
            raise RuntimeError(urgent_result.get("msg") or "飞书加急失败")
        _persist_outbox_progress(row["id"], "urgent_sent", message_id)


def _create_task_now(row, payload):
    recipient_id = row["recipient_id"]
    order_id = record_id(payload.get("order_id"))
    if not order_id:
        return
    conn = db()
    order = conn.execute("SELECT status,assignee_id FROM orders WHERE id=?", (order_id,)).fetchone()
    conn.close()
    guid = row["external_id"] or ""
    if not order or order["assignee_id"] != recipient_id or order["status"] not in ("assigned", "in_progress", "completion_review"):
        if guid:
            result = feishu_api("DELETE", f"/task/v2/tasks/{guid}", token=tenant_token())
            if result.get("code") not in (0, None):
                raise RuntimeError(result.get("msg") or "飞书失效任务清理失败")
            _persist_outbox_progress(row["id"], "task_cleaned", guid)
        return
    progress = row["progress"] or ""
    if not guid:
        due_ms = _order_due_ms(payload.get("date"), payload.get("arrive"))
        body = {"summary": payload.get("summary") or "派工任务", "description": payload.get("description") or ""}
        if due_ms:
            _, minutes = reminder_conf()
            body["due"] = {"timestamp": str(due_ms), "is_all_day": False}
            body["reminders"] = [{"relative_fire_minute": minutes}]
        result = feishu_api(
            "POST", "/task/v2/tasks", token=tenant_token(),
            query={"user_id_type": "open_id"}, body=body,
        )
        guid = ((result.get("data") or {}).get("task") or {}).get("guid")
        if not guid:
            raise RuntimeError(result.get("msg") or "飞书任务创建失败")
        _persist_outbox_progress(row["id"], "task_created", guid)
        progress = "task_created"
    if progress != "member_added":
        member_result = feishu_api(
            "POST", f"/task/v2/tasks/{guid}/add_members", token=tenant_token(),
            query={"user_id_type": "open_id"},
            body={"members": [{"id": recipient_id, "role": "assignee", "type": "user"}]},
        )
        if member_result.get("code") not in (0, None):
            raise RuntimeError(member_result.get("msg") or "飞书任务负责人设置失败")
        _persist_outbox_progress(row["id"], "member_added", guid)
    conn = db()
    changed = conn.execute(
        """UPDATE orders SET feishu_task_guid=?
           WHERE id=? AND assignee_id=? AND status IN ('assigned','in_progress','completion_review')""",
        (guid, order_id, recipient_id),
    ).rowcount
    conn.commit()
    conn.close()
    if changed != 1:
        result = feishu_api("DELETE", f"/task/v2/tasks/{guid}", token=tenant_token())
        if result.get("code") not in (0, None):
            raise RuntimeError(result.get("msg") or "飞书失效任务清理失败")
        _persist_outbox_progress(row["id"], "task_cleaned", guid)


def _delete_task_now(row, payload):
    if row["progress"] == "task_deleted":
        return
    guid = (payload.get("guid") or "").strip()
    if guid:
        result = feishu_api("DELETE", f"/task/v2/tasks/{guid}", token=tenant_token())
        if result.get("code") not in (0, None):
            raise RuntimeError(result.get("msg") or "飞书任务删除失败")
        _persist_outbox_progress(row["id"], "task_deleted", guid)


CHINA_TIMEZONE = timezone(timedelta(hours=8))
CALENDAR_STATUS_LABEL = {
    "pending": "待派",
    "assigned": "已安排",
    "in_progress": "进行中",
    "completion_review": "结束核实",
    "completed": "已完成",
}


def _calendar_bounds(order):
    start = business.parse_dt(order["scheduled_start"])
    end = business.parse_dt(order["scheduled_end"])
    if not start or not end or end <= start:
        start, end = business.schedule_bounds(order["date"], order["arrive"], order["minutes"] or 30)
    if not start or not end:
        raise RuntimeError("工单时间无效，无法同步飞书日历")
    return start.replace(tzinfo=CHINA_TIMEZONE), end.replace(tzinfo=CHINA_TIMEZONE)


def _calendar_room(order):
    room = str(order["room"] or order["team"] or "未指定直播间").strip()
    return room if room.endswith(("间", "房", "室")) else f"{room}间"


def _calendar_event_body(order):
    start, end = _calendar_bounds(order)
    status = (
        "超长待审核"
        if order["duration_review"] == "pending"
        else CALENDAR_STATUS_LABEL.get(order["status"], order["status"] or "待派")
    )
    room = _calendar_room(order)
    assignee = order["assignee_name"] or "待派工"
    description = "\n".join(
        [
            f"工单 #{order['id']}",
            f"工作组：{order['grp']}",
            f"直播团队：{order['team']} · {room}",
            f"负责人：{assignee}",
            f"任务：{order['task_type']}",
            f"预计时长：{order['minutes']}分钟",
            f"状态：{status}",
            f"下单人：{order['creator_name'] or '未记录'}",
            f"备注：{order['note'] or '无'}",
            f"管理入口：{BASE_URL}/?page=dispatch",
        ]
    )
    return {
        "summary": f"【{order['grp']} · {status}】{room} · {order['task_type']}",
        "description": description,
        "start_time": {"timestamp": str(int(start.timestamp())), "timezone": "Asia/Shanghai"},
        "end_time": {"timestamp": str(int(end.timestamp())), "timezone": "Asia/Shanghai"},
        "visibility": "public",
        "free_busy_status": "busy",
        "need_notification": False,
    }


def _ensure_dispatch_calendar():
    calendar_id = setting("feishu_calendar_id", "").strip()
    if calendar_id:
        return calendar_id
    calendar_name = setting("feishu_calendar_name", "团播派工排期").strip() or "团播派工排期"
    result = feishu_api(
        "POST",
        "/calendar/v4/calendars",
        token=tenant_token(),
        body={
            "summary": calendar_name,
            "description": "团播派工系统的统一任务日历。任务请在派工系统内修改。",
            "permissions": "public",
        },
    )
    if result.get("code") not in (0, None):
        raise RuntimeError(result.get("msg") or "飞书共享日历创建失败")
    calendar_id = ((result.get("data") or {}).get("calendar") or {}).get("calendar_id") or ""
    if not calendar_id:
        raise RuntimeError("飞书共享日历创建成功但未返回日历编号")
    conn = db()
    conn.execute("INSERT OR REPLACE INTO settings(k,v) VALUES('feishu_calendar_id',?)", (calendar_id,))
    conn.commit()
    conn.close()
    return calendar_id


def _sync_calendar_now(row, payload):
    order_id = record_id(payload.get("order_id"))
    if not order_id:
        return
    conn = db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    conn.close()
    if not order:
        return
    calendar_id = order["feishu_calendar_id"] or setting("feishu_calendar_id", "").strip()
    event_id = order["feishu_event_id"] or ""
    if order["status"] in ("cancelled", "abnormal"):
        if calendar_id and event_id:
            result = feishu_api(
                "DELETE",
                f"/calendar/v4/calendars/{urllib.parse.quote(calendar_id, safe='')}/events/{urllib.parse.quote(event_id, safe='')}",
                token=tenant_token(),
                query={"need_notification": "false"},
            )
            if result.get("code") not in (0, None) and "not found" not in str(result.get("msg") or "").lower():
                raise RuntimeError(result.get("msg") or "飞书日历事件删除失败")
        conn = db()
        conn.execute(
            "UPDATE orders SET feishu_calendar_id='',feishu_event_id='',calendar_synced_version=? WHERE id=?",
            (order["version"], order_id),
        )
        conn.commit()
        conn.close()
        return

    calendar_id = calendar_id or _ensure_dispatch_calendar()
    body = _calendar_event_body(order)
    if event_id:
        result = feishu_api(
            "PATCH",
            f"/calendar/v4/calendars/{urllib.parse.quote(calendar_id, safe='')}/events/{urllib.parse.quote(event_id, safe='')}",
            token=tenant_token(),
            query={"need_notification": "false"},
            body=body,
        )
        if result.get("code") not in (0, None):
            raise RuntimeError(result.get("msg") or "飞书日历事件更新失败")
    else:
        result = feishu_api(
            "POST",
            f"/calendar/v4/calendars/{urllib.parse.quote(calendar_id, safe='')}/events",
            token=tenant_token(),
            query={"idempotency_key": f"dispatch-calendar-order-{order_id:010d}-version-{order['version']:010d}"},
            body=body,
        )
        if result.get("code") not in (0, None):
            raise RuntimeError(result.get("msg") or "飞书日历事件创建失败")
        event_id = ((result.get("data") or {}).get("event") or {}).get("event_id") or ""
        if not event_id:
            raise RuntimeError("飞书日历事件创建成功但未返回事件编号")
    conn = db()
    conn.execute(
        """UPDATE orders
           SET feishu_calendar_id=?,feishu_event_id=?,calendar_synced_version=?
           WHERE id=?""",
        (calendar_id, event_id, order["version"], order_id),
    )
    conn.commit()
    conn.close()


def process_outbox_once(limit=20):
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    stale_at = business.format_dt(datetime.now() - timedelta(minutes=10))
    conn.execute(
        """UPDATE notification_outbox
           SET status='retry',available_at=?,claimed_at='',
               last_error=CASE WHEN last_error='' THEN '发送超时后自动恢复' ELSE last_error END
           WHERE status='sending' AND (claimed_at='' OR claimed_at<=?)""",
        (now_iso(), stale_at),
    )
    rows = conn.execute(
        """SELECT * FROM notification_outbox
           WHERE status IN ('pending','retry') AND available_at<=?
           ORDER BY id LIMIT ?""",
        (now_iso(), limit),
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE notification_outbox SET status='sending',claimed_at=? WHERE id=?",
            (now_iso(), row["id"]),
        )
    conn.commit()
    conn.close()
    for row in rows:
        error = ""
        try:
            payload = json.loads(row["payload"])
            if not TEST_MODE and FEISHU_CONFIGURED:
                if row["kind"] == "message":
                    _send_message_now(row, payload)
                elif row["kind"] == "task_create":
                    if setting("reminder_on", "1") == "1":
                        _create_task_now(row, payload)
                elif row["kind"] == "task_delete":
                    _delete_task_now(row, payload)
                elif row["kind"] == "calendar_sync":
                    _sync_calendar_now(row, payload)
        except Exception as exc:
            error = str(exc)[:500]
        conn = db()
        if not error:
            conn.execute(
                "UPDATE notification_outbox SET status='sent',sent_at=?,claimed_at='',last_error='' WHERE id=?",
                (now_iso(), row["id"]),
            )
        else:
            attempts = int(row["attempts"] or 0) + 1
            if attempts >= 5:
                conn.execute(
                    "UPDATE notification_outbox SET status='failed',attempts=?,claimed_at='',last_error=? WHERE id=?",
                    (attempts, error, row["id"]),
                )
            else:
                available = datetime.now() + timedelta(minutes=2 ** attempts)
                conn.execute(
                    """UPDATE notification_outbox SET status='retry',attempts=?,available_at=?,claimed_at='',last_error=?
                       WHERE id=?""",
                    (attempts, business.format_dt(available), error, row["id"]),
                )
        conn.commit()
        conn.close()


_LAST_BACKUP_DAY = ""


def _sync_backup_media(target_root):
    media_backup = target_root / "media"
    media_backup.mkdir(parents=True, exist_ok=True)
    if not MEDIA_DIR.exists():
        return
    for source_path in MEDIA_DIR.iterdir():
        if not source_path.is_file():
            continue
        target_path = media_backup / source_path.name
        if not target_path.exists() or target_path.stat().st_size != source_path.stat().st_size:
            shutil.copy2(source_path, target_path)


def backup_database(force=False):
    global _LAST_BACKUP_DAY
    if TEST_MODE:
        return None
    day = datetime.now().strftime("%Y-%m-%d")
    if not force and _LAST_BACKUP_DAY == day:
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUP_DIR / f"dispatch-{datetime.now():%Y%m%d-%H%M%S}.db"
    source = db()
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    backups = sorted(BACKUP_DIR.glob("dispatch-*.db"), key=lambda path: path.stat().st_mtime)
    for old_backup in backups[:-30]:
        old_backup.unlink(missing_ok=True)
    _sync_backup_media(BACKUP_DIR)
    if BACKUP_MIRROR_DIR:
        BACKUP_MIRROR_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, BACKUP_MIRROR_DIR / target.name)
        _sync_backup_media(BACKUP_MIRROR_DIR)
        mirror_backups = sorted(
            BACKUP_MIRROR_DIR.glob("dispatch-*.db"), key=lambda path: path.stat().st_mtime
        )
        for old_backup in mirror_backups[:-30]:
            old_backup.unlink(missing_ok=True)
    _LAST_BACKUP_DAY = day
    return target


def run_scheduled_jobs():
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    now = datetime.now()
    stamp = business.format_dt(now)
    try:
        grace = max(0, min(120, int(setting("book_grace_min", "15") or 15)))
        if grace:
            stale = conn.execute(
                """SELECT * FROM orders WHERE booked=1 AND status IN ('pending','assigned')
                   AND scheduled_start!='' AND scheduled_start<?
                   AND NOT EXISTS (
                     SELECT 1 FROM time_reviews
                     WHERE time_reviews.order_id=orders.id AND time_reviews.status='pending'
                   )""",
                (business.format_dt(now - timedelta(minutes=grace)),),
            ).fetchall()
            for order in stale:
                conn.execute(
                    """UPDATE orders SET status='cancelled',cancelled_at=?,cancelled_by='system',
                       cancel_reason='到点未开始自动释放',version=version+1 WHERE id=?""",
                    (stamp, order["id"]),
                )
                business.add_event(
                    conn, order["id"], "预约自动释放", None, order["status"], "cancelled",
                    f"到岗后 {grace} 分钟仍未开始",
                )
                business.close_active_transfers(conn, order["id"])
                if order["feishu_task_guid"]:
                    business.queue_task_operation(
                        conn, "task_delete", "",
                        {"guid": order["feishu_task_guid"], "order_id": order["id"]},
                        f"order:{order['id']}:task-delete:stale",
                    )
                for recipient in {order["creator_id"], order["assignee_id"]} - {"", None}:
                    business.queue_notification(
                        conn, recipient,
                        f"预约已自动释放：#{order['id']} {order['team']} · {order['task_type']}",
                        f"order:{order['id']}:stale:{recipient}",
                    )

        remind = max(0, min(1440, int(setting("overrun_remind_min", "30") or 30)))
        running = conn.execute(
            "SELECT * FROM orders WHERE status='in_progress' AND started_at!=''"
        ).fetchall()
        for order in running:
            started = business.parse_dt(order["started_at"])
            if not started:
                continue
            overdue = (now - started).total_seconds() / 60 - int(order["minutes"] or 30)
            if remind and overdue >= remind and not order["overrun_notified"]:
                conn.execute("UPDATE orders SET overrun_notified=1 WHERE id=?", (order["id"],))
                business.queue_notification(
                    conn, order["assignee_id"],
                    f"跟播已超过计划时间：#{order['id']} {order['team']} · {order['task_type']}；比赛未结束请继续，结束后按实际时间操作",
                    f"order:{order['id']}:overrun-reminder",
                )

        active_transfers = conn.execute(
            """SELECT transfers.*,orders.status order_status,orders.team,orders.task_type,orders.grp,
                      orders.scheduled_start
               FROM transfers JOIN orders ON orders.id=transfers.order_id
               WHERE transfers.status IN ('awaiting_lead','open')"""
        ).fetchall()
        expired_transfers = []
        for transfer in active_transfers:
            created_at = business.parse_dt(transfer["created_at"])
            scheduled_start = business.parse_dt(transfer["scheduled_start"])
            if not created_at:
                expired_transfers.append(transfer)
                continue
            urgent_transfer = (
                transfer["order_status"] == "in_progress"
                or bool(scheduled_start and scheduled_start - created_at <= timedelta(hours=2))
            )
            deadline = created_at + timedelta(minutes=10 if urgent_transfer else 30)
            if now >= deadline:
                expired_transfers.append(transfer)
        for transfer in expired_transfers:
            conn.execute(
                "UPDATE transfers SET status='expired',resolved_at=? WHERE id=?",
                (stamp, transfer["id"]),
            )
            business.add_event(
                conn, transfer["order_id"], "转包超时", None,
                transfer["order_status"], transfer["order_status"], f"转包 #{transfer['id']} 未及时处理",
            )
            business.queue_notification(
                conn, transfer["requester_id"],
                f"转包申请已超时：#{transfer['order_id']} {transfer['team']} · {transfer['task_type']}",
                f"transfer:{transfer['id']}:expired:requester", urgent=True,
            )
            leads = conn.execute(
                "SELECT open_id FROM users WHERE role='admin' OR (role='staff' AND lead=1 AND grp=?)",
                (transfer["grp"],),
            ).fetchall()
            for lead in leads:
                if lead["open_id"] in (transfer["requester_id"], transfer["target_id"]):
                    continue
                business.queue_notification(
                    conn, lead["open_id"],
                    f"转包超时需人工处理：#{transfer['order_id']} {transfer['team']} · {transfer['task_type']}",
                    f"transfer:{transfer['id']}:expired:{lead['open_id']}", urgent=True,
                )

        old_uploads = conn.execute(
            """SELECT id,path FROM uploads
               WHERE (status='pending' AND created_at<?)
                  OR (status='uploading' AND created_at<?)""",
            (
                business.format_dt(now - timedelta(hours=24)),
                business.format_dt(now - timedelta(hours=1)),
            ),
        ).fetchall()
        conn.executemany("UPDATE uploads SET status='expired' WHERE id=?", [(row["id"],) for row in old_uploads])
        conn.execute("DELETE FROM sessions WHERE expires_at<=?", (stamp,))
        conn.execute(
            "DELETE FROM notification_outbox WHERE status='sent' AND sent_at<?",
            (business.format_dt(now - timedelta(days=30)),),
        )
        conn.execute(
            "DELETE FROM notification_outbox WHERE status='failed' AND created_at<?",
            (business.format_dt(now - timedelta(days=90)),),
        )
        conn.execute(
            "DELETE FROM uploads WHERE status='expired' AND created_at<?",
            (business.format_dt(now - timedelta(days=30)),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO system_health(key,value,updated_at) VALUES('scheduler','ok',?)", (stamp,)
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    for upload in old_uploads:
        path = MEDIA_DIR / os.path.basename(upload["path"])
        if path.is_file():
            path.unlink()
    backup_database()


def background_loop(stop_event):
    next_jobs = 0.0
    while not stop_event.wait(2):
        try:
            process_outbox_once()
            if time.monotonic() >= next_jobs:
                run_scheduled_jobs()
                next_jobs = time.monotonic() + 30
        except Exception:
            logging.exception("后台任务执行失败")


# ───────────────────────── 会话 ─────────────────────────
def new_session(open_id):
    sid = secrets.token_urlsafe(32)
    conn = db()
    conn.execute("DELETE FROM sessions WHERE expires_at<=?", (now_iso(),))
    conn.execute("INSERT INTO sessions(sid,open_id,created_at,expires_at) VALUES(?,?,?,?)",
                 (sid, open_id, now_iso(),
                  (datetime.now() + timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()
    return sid


def session_user(sid):
    if not sid:
        return None
    conn = db()
    row = conn.execute("""SELECT u.* FROM sessions s JOIN users u ON u.open_id=s.open_id
                          WHERE s.sid=? AND s.expires_at>?""", (sid, now_iso())).fetchone()
    conn.close()
    return dict(row) if row else None


def drop_session(sid):
    conn = db()
    conn.execute("DELETE FROM sessions WHERE sid=?", (sid,))
    conn.commit()
    conn.close()


def upsert_user(info):
    conn = db()
    ex = conn.execute("SELECT open_id FROM users WHERE open_id=?", (info["open_id"],)).fetchone()
    if ex:
        conn.execute("UPDATE users SET name=?,avatar=?,union_id=?,last_login=? WHERE open_id=?",
                     (info["name"], info["avatar"], info.get("union_id", ""), now_iso(), info["open_id"]))
    else:
        conn.execute("""INSERT INTO users(open_id,union_id,name,avatar,role,created_at,last_login)
                        VALUES(?,?,?,?,'team',?,?)""",
                     (info["open_id"], info.get("union_id", ""), info["name"], info["avatar"],
                      now_iso(), now_iso()))
    conn.commit()
    conn.close()


# ───────────────────────── 业务 ─────────────────────────
def user_scope(user):
    """返回工作人员的数据组范围；管理员和团队使用独立范围规则。"""
    if user["role"] in ("admin", "team"):
        return None
    return (user.get("grp") or "") or "__none__"


def dn(u):
    """显示名:管理员设置的别名优先,否则飞书名。u 为 dict 或 sqlite Row。"""
    try:
        a = (u["alias"] or "").strip()
    except (KeyError, IndexError, TypeError):
        a = ""
    return a or u["name"]


def alias_map(conn):
    """open_id → 显示名 映射,用于把工单里的历史名字按当前别名显示。"""
    return {r["open_id"]: ((r["alias"] or "").strip() or r["name"])
            for r in conn.execute("SELECT open_id,name,COALESCE(alias,'') alias FROM users")}


def schedule_assignments(conn, workers, from_date, to_date):
    """展开日期区间内每个人的实际班次，供桌面与移动端共用。"""
    assignments = []
    cursor = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date, "%Y-%m-%d")
    while cursor <= end:
        work_date = cursor.strftime("%Y-%m-%d")
        for worker in workers:
            shift = business.effective_shift(conn, worker, work_date)
            if not shift:
                shift = {
                    "worker_id": worker["open_id"],
                    "date": work_date,
                    "shift_id": None,
                    "name": "",
                    "start": "",
                    "end": "",
                    "source": "missing",
                    "is_off": 0,
                }
            item = dict(shift)
            item.update({
                "worker_name": dn(worker),
                "grp": worker["grp"] or "",
                "default_shift": worker["shift"] or "",
                "absence_type": business.absence_type_on(worker, work_date),
            })
            assignments.append(item)
        cursor += timedelta(days=1)
    return assignments


def api_state(user):
    conn = db()
    amap = alias_map(conn)
    settings = {row["k"]: row["v"] for row in conn.execute("SELECT k,v FROM settings")}
    approved = bool(not user.get("blocked", 0) and (user.get("approved", 0) or user["role"] == "admin"))
    me = {k: user.get(k) for k in (
        "open_id", "name", "avatar", "role", "shift", "status", "task", "grp", "lead", "team_id",
        "blocked", "read_only"
    )}
    me["fs_name"] = me["name"]
    me["name"] = amap.get(user["open_id"], me["name"])
    me["approved"] = 1 if approved else 0
    if not approved:
        conn.close()
        return {
            "me": me,
            "groups": [], "staff": [], "teams": [], "sla": [], "shifts": [], "orders": [],
            "test_mode": TEST_MODE, "server_version": SERVER_VERSION, "bootstrap_open": False,
            "rules": {"gate_mode": settings.get("gate_mode", "manual")},
        }

    all_groups = [dict(r) for r in conn.execute(
        "SELECT id,name,color,sort,COALESCE(auto,0) auto FROM groups ORDER BY sort,id"
    )]
    if user["role"] == "staff":
        groups = [group for group in all_groups if group["name"] == (user.get("grp") or "")]
    else:
        groups = all_groups

    staff_sql = (
        "SELECT open_id,name,COALESCE(alias,'') alias,avatar,grp,lead,intern,absence_type,absence_start,absence_end,"
        "shift,status,task,work_hours FROM users WHERE grp!='' AND role!='team' "
        "AND COALESCE(approved,0)=1 AND COALESCE(blocked,0)=0"
    )
    staff_args = []
    if user["role"] == "team":
        staff = []
    else:
        if user["role"] == "staff":
            staff_sql += " AND grp=?"
            staff_args.append(user.get("grp") or "__none__")
        staff = [dict(r) for r in conn.execute(staff_sql + " ORDER BY grp,shift,name", staff_args)]
    for s in staff:
        s["fs_name"] = s["name"]
        s["name"] = dn(s)
    today_assignments = schedule_assignments(conn, staff, today(), today()) if staff else []

    if user["role"] == "team":
        team_id = business.bound_team_id(user)
        teams = [dict(r) for r in conn.execute("SELECT * FROM teams WHERE id=?", (team_id,))] if team_id else []
        for team in teams:
            team.pop("lighting_owner_id", None)
    else:
        teams = [dict(r) for r in conn.execute("SELECT * FROM teams ORDER BY id")]
    if user["role"] == "staff":
        sla = [dict(r) for r in conn.execute("SELECT * FROM sla WHERE grp=? ORDER BY id", (user.get("grp") or "",))]
    else:
        sla = [dict(r) for r in conn.execute("SELECT * FROM sla ORDER BY grp,id")]
    shifts = [] if user["role"] == "team" else [dict(r) for r in conn.execute(
        "SELECT id,name,start,end FROM shifts ORDER BY sort,id"
    )]
    try:
        horizon_days = max(1, min(31, int(settings.get("book_horizon_days", "7"))))
    except ValueError:
        horizon_days = 7
    override_to = (
        datetime.strptime(today(), "%Y-%m-%d") + timedelta(days=horizon_days)
    ).strftime("%Y-%m-%d")
    if user["role"] == "team":
        shift_overrides = []
    else:
        override_sql = """SELECT so.worker_id,so.work_date,so.shift_id,so.is_off,
                                 s.name,s.start,s.end
                          FROM staff_shift_overrides so
                          JOIN users u ON u.open_id=so.worker_id
                          LEFT JOIN shifts s ON s.id=so.shift_id
                          WHERE so.work_date>=? AND so.work_date<=?"""
        override_args = [today(), override_to]
        if user["role"] == "staff":
            override_sql += " AND u.grp=?"
            override_args.append(user.get("grp") or "__none__")
        shift_overrides = [dict(r) for r in conn.execute(
            override_sql + " ORDER BY so.work_date,so.worker_id",
            override_args,
        )]

    order_where = (
        "(date=? OR status IN ('pending','assigned','in_progress','completion_review') "
        "OR id IN (SELECT order_id FROM time_reviews WHERE status='pending'))"
    )
    order_args = [today()]
    if user["role"] == "team":
        order_where += " AND team_id=?"
        order_args.append(business.bound_team_id(user) or -1)
    elif user["role"] != "admin":
        order_where += " AND grp=?"
        order_args.append(user.get("grp") or "__none__")
        if not user.get("lead"):
            order_where += " AND (status='pending' OR assignee_id=? OR creator_id=?)"
            order_args.extend((user["open_id"], user["open_id"]))
    orders = [dict(r) for r in conn.execute(
        f"SELECT * FROM orders WHERE {order_where} ORDER BY date,arrive,id", order_args
    )]
    for o in orders:
        if o.get("assignee_id") and o["assignee_id"] in amap:
            o["assignee_name"] = amap[o["assignee_id"]]
        if o.get("creator_id") and o["creator_id"] in amap:
            o["creator_name"] = amap[o["creator_id"]]
    business.attach_transfer_state(conn, orders)
    business.attach_time_review_state(conn, orders)

    out = {"me": me, "groups": groups, "staff": staff, "today_assignments": today_assignments,
           "shift_overrides": shift_overrides, "teams": teams, "sla": sla,
           "shifts": shifts, "orders": orders, "test_mode": TEST_MODE,
           "server_version": SERVER_VERSION,
           "bootstrap_open": settings.get("bootstrap_used") != "1",
           "reminder": dict(zip(("on", "minutes"), reminder_conf())),
           "urgent_on": settings.get("urgent_on", "0") == "1",
           "rules": {"team_cap": settings.get("book_team_cap", "2"),
                     "team_day_min": settings.get("book_team_day_min", "240"),
                     "book_horizon_days": settings.get("book_horizon_days", "7"),
                     "start_early_min": settings.get("start_early_min", "30"),
                     "grace_min": settings.get("book_grace_min", "15"),
                     "dev_note": settings.get("rule_dev_note", "1"),
                     "biange_media": settings.get("rule_biange_media", "1"),
                     "done_note": settings.get("rule_done_note", "1"),
                     "nav_team": settings.get("nav_team", ""),
                     "nav_staff": settings.get("nav_staff", ""),
                     "gate_mode": settings.get("gate_mode", "manual"),
                     "allow_dept_ids": settings.get("allow_dept_ids", ""),
                     "overrun_remind_min": settings.get("overrun_remind_min", "30")}}
    if user["role"] == "admin":
        out["users"] = [dict(r) for r in conn.execute(
            """SELECT open_id,name,COALESCE(alias,'') alias,avatar,role,grp,lead,intern,
                      absence_type,absence_start,absence_end,
                      shift,status,last_login,COALESCE(approved,0) approved,
                      COALESCE(blocked,0) blocked,COALESCE(read_only,0) read_only,team_id
               FROM users ORDER BY role DESC,grp,name""")]
        for u2 in out["users"]:
            u2["fs_name"] = u2["name"]
            u2["name"] = dn(u2)
        out["operations"] = dict(conn.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END),0) notification_pending,
                 COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),0) notification_failed
               FROM notification_outbox"""
        ).fetchone())
        calendar_queue = conn.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN status IN ('pending','retry','sending') THEN 1 ELSE 0 END),0) pending,
                 COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),0) failed
               FROM notification_outbox WHERE kind='calendar_sync'"""
        ).fetchone()
        out["calendar"] = {
            "name": settings.get("feishu_calendar_name", "团播派工排期"),
            "connected": bool(settings.get("feishu_calendar_id", "")),
            "pending": calendar_queue["pending"],
            "failed": calendar_queue["failed"],
        }
    conn.close()
    return out


def api_users(user):
    if user["role"] != "admin":
        return 403, "仅管理员可查看人员"
    conn = db()
    rows = [dict(r) for r in conn.execute(
        """SELECT open_id,name,avatar,role,tech_role,shift,status,last_login
           FROM users ORDER BY role DESC,name""")]
    conn.close()
    return 200, rows


def api_schedule(user, params):
    """返回指定单日、连续三日、周或月的有效班次和工单。"""
    rng = (params.get("range") or "week").strip()
    now = biz_now()  # 周/月区间与「今天」都按排班日,凌晨不跳日
    base_text = (params.get("date") or now.strftime("%Y-%m-%d")).strip()
    try:
        base = datetime.strptime(base_text, "%Y-%m-%d")
    except ValueError:
        return 400, "排班日期无效"
    if rng == "month":
        frm = base.replace(day=1)
        nxt = frm.replace(year=frm.year + 1, month=1) if frm.month == 12 \
            else frm.replace(month=frm.month + 1)
        to = nxt - timedelta(days=1)
    elif rng == "three":
        frm = base
        to = base + timedelta(days=2)
    elif rng == "day":
        frm = base
        to = base
    else:
        rng = "week"
        frm = base - timedelta(days=base.weekday())
        to = frm + timedelta(days=6)
    frm_s, to_s = frm.strftime("%Y-%m-%d"), to.strftime("%Y-%m-%d")
    conn = db()
    sql = ("SELECT id,date,team,room,task_type,grp,arrive,minutes,assignee_id,assignee_name,status,urgent "
           "FROM orders WHERE date>=? AND date<=?")
    args = [frm_s, to_s]
    if user["role"] == "team":
        sql += " AND team_id=?"
        args.append(business.bound_team_id(user) or -1)
    elif user["role"] != "admin":
        sql += " AND grp=?"
        args.append(user.get("grp") or "__none__")
        if not user.get("lead"):
            sql += " AND (status='pending' OR assignee_id=? OR creator_id=?)"
            args.extend((user["open_id"], user["open_id"]))
    sql += " ORDER BY date,arrive,id"
    rows = [dict(r) for r in conn.execute(sql, args)]
    amap = alias_map(conn)
    for r in rows:
        if r.get("assignee_id") and r["assignee_id"] in amap:
            r["assignee_name"] = amap[r["assignee_id"]]
    workers = []
    if user["role"] != "team":
        worker_sql = (
            "SELECT * FROM users WHERE grp!='' AND role!='team' "
            "AND COALESCE(approved,0)=1 AND COALESCE(blocked,0)=0"
        )
        worker_args = []
        if user["role"] == "staff":
            worker_sql += " AND grp=?"
            worker_args.append(user.get("grp") or "__none__")
        workers = conn.execute(worker_sql + " ORDER BY grp,name", worker_args).fetchall()
    assignments = schedule_assignments(conn, workers, frm_s, to_s) if workers else []
    conn.close()
    return 200, {"range": rng, "from": frm_s, "to": to_s,
                 "today": now.strftime("%Y-%m-%d"), "orders": rows,
                 "assignments": assignments}


def do_schedule_adjust(user, body):
    """保存某一天的班次调整；删除记录即恢复人员默认班次。"""
    if not business.is_manager(user):
        return 403, "仅管理员或组长可调整排班"
    work_date = str(body.get("date") or "").strip()
    try:
        parsed_date = datetime.strptime(work_date, "%Y-%m-%d").date()
    except ValueError:
        return 400, "排班日期无效"
    if parsed_date < datetime.strptime(today(), "%Y-%m-%d").date():
        return 400, "不能修改已经过去的排班"
    changes = body.get("changes")
    if not isinstance(changes, list) or not changes or len(changes) > 100:
        return 400, "请提交 1 至 100 条人员排班调整"

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    seen = set()
    prepared = []
    for change in changes:
        if not isinstance(change, dict):
            conn.close()
            return 400, "排班调整格式无效"
        worker_id = str(change.get("worker_id") or "").strip()
        mode = str(change.get("mode") or "").strip()
        if not worker_id or worker_id in seen:
            conn.close()
            return 400, "人员排班不能重复提交"
        seen.add(worker_id)
        worker = conn.execute("SELECT * FROM users WHERE open_id=?", (worker_id,)).fetchone()
        if (
            not worker
            or worker["role"] == "team"
            or not worker["grp"]
            or not worker["approved"]
            or worker["blocked"]
        ):
            conn.close()
            return 400, "排班人员不存在或当前不可排班"
        if user["role"] != "admin" and (worker["grp"] or "") != (user.get("grp") or ""):
            conn.close()
            return 403, "组长只能调整本组人员排班"
        if worker_id == user["open_id"]:
            conn.close()
            return 403, "不能调整自己的排班，请让其他管理员操作"
        current = conn.execute(
            "SELECT shift_id,is_off FROM staff_shift_overrides WHERE worker_id=? AND work_date=?",
            (worker_id, work_date),
        ).fetchone()
        shift = None
        shift_id = None
        if mode == "default":
            desired = None
            shift = conn.execute(
                "SELECT id,name,start,end FROM shifts WHERE name=?",
                (worker["shift"] or "",),
            ).fetchone()
            if not shift:
                conn.close()
                return 409, f"{dn(worker)} 的默认班次不存在"
        elif mode == "off":
            desired = (None, 1)
        elif mode == "shift":
            shift_id = record_id(change.get("shift_id"))
            shift = conn.execute(
                "SELECT id,name,start,end FROM shifts WHERE id=?", (shift_id,)
            ).fetchone() if shift_id else None
            if not shift:
                conn.close()
                return 400, "指定班次不存在"
            desired = (shift_id, 0)
        else:
            conn.close()
            return 400, "排班调整方式无效"

        current_value = (current["shift_id"], current["is_off"]) if current else None
        if current_value == desired:
            continue
        orders = conn.execute(
            """SELECT * FROM orders
               WHERE assignee_id=? AND date=?
               AND status IN ('assigned','in_progress','completion_review')""",
            (worker_id, work_date),
        ).fetchall()
        if mode == "off" and orders:
            conn.close()
            return 409, f"{dn(worker)} 当天还有 {len(orders)} 张工单，请先改派"
        for order in orders:
            order_start, order_end = business.order_bounds(order)
            shift_start, shift_end = business.shift_bounds(
                work_date, shift["start"], shift["end"]
            )
            if (
                not order_start
                or not order_end
                or not shift_start
                or not shift_end
                or order_start < shift_start
                or order_end > shift_end
            ):
                conn.close()
                return 409, f"{dn(worker)} 的工单 #{order['id']} 超出所选班次，请先改派"
        prepared.append((worker, mode, shift_id, shift))

    stamp = now_iso()
    audit_items = []
    for index, (worker, mode, shift_id, shift) in enumerate(prepared):
        if mode == "default":
            conn.execute(
                "DELETE FROM staff_shift_overrides WHERE worker_id=? AND work_date=?",
                (worker["open_id"], work_date),
            )
            label = f"恢复默认班次（{worker['shift']}）"
        else:
            conn.execute(
                """INSERT INTO staff_shift_overrides(
                     worker_id,work_date,shift_id,is_off,created_by,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(worker_id,work_date) DO UPDATE SET
                     shift_id=excluded.shift_id,is_off=excluded.is_off,
                     created_by=excluded.created_by,updated_at=excluded.updated_at""",
                (
                    worker["open_id"],
                    work_date,
                    shift_id,
                    1 if mode == "off" else 0,
                    user["open_id"],
                    stamp,
                    stamp,
                ),
            )
            label = "排休" if mode == "off" else f"调整为{shift['name']}"
        audit_items.append(f"{dn(worker)}：{label}")
        business.queue_notification(
            conn,
            worker["open_id"],
            f"{work_date} 排班已{label}",
            f"shift:{work_date}:{worker['open_id']}:{stamp}:{index}",
        )
    if prepared:
        insert_audit(conn, user, "调整排班", f"{work_date}；" + "；".join(audit_items))
    conn.commit()
    conn.close()
    return 200, None


def api_stats(user, params):
    """按订单统计业务量，按 work_sessions 统计每名工作人员的实际工时。"""
    rng = params.get("range") or "30d"
    person = (params.get("person") or "").strip()
    grp = (params.get("group") or "").strip()
    if user["role"] == "staff":
        grp = (user.get("grp") or "").strip()
    now = biz_now()  # 统计区间按排班日,凌晨不换天
    if rng == "7d":
        frm = (now - timedelta(days=6)).strftime("%Y-%m-%d")
    elif rng == "month":
        frm = now.strftime("%Y-%m-01")
    elif rng == "all":
        frm = "0000-01-01"
    else:
        rng, frm = "30d", (now - timedelta(days=29)).strftime("%Y-%m-%d")
    to = now.strftime("%Y-%m-%d")
    conn = db()
    amap = alias_map(conn)
    if person:
        if user["role"] == "team":
            conn.close()
            return 403, "团队账号不能查看个人工作明细"
        if user["role"] == "staff" and not user.get("lead") and person != user["open_id"]:
            conn.close()
            return 403, "只能查看自己的工作明细"
        if user["role"] == "staff" and user.get("lead"):
            target = conn.execute("SELECT grp FROM users WHERE open_id=?", (person,)).fetchone()
            if not target or target["grp"] != (user.get("grp") or ""):
                conn.close()
                return 403, "组长只能查看本组人员明细"
        prow = conn.execute("SELECT name,COALESCE(alias,'') alias FROM users WHERE open_id=?", (person,)).fetchone()
        where = "s.worker_id=? AND o.date>=? AND o.date<=?"
        args = [person, frm, to]
        if user["role"] == "staff":
            where += " AND o.grp=?"
            args.append(user.get("grp") or "")
        rows = [dict(r) for r in conn.execute(
            """SELECT o.id,o.date,o.team,o.room,o.task_type,o.minutes,o.arrive,o.status,o.note,
                      o.media,o.media_kind,o.media2,o.media2_kind,s.started_at,s.ended_at done_at,
                      s.end_reason
               FROM work_sessions s JOIN orders o ON o.id=s.order_id WHERE """ + where +
            " ORDER BY o.date DESC,s.started_at DESC", args)]
        conn.close()
        for r in rows:
            r["used"] = used_minutes(r.get("started_at"), r.get("done_at"))
        return 200, {"range": rng, "from": frm, "to": to, "person": person,
                     "name": dn(prow) if prow else person, "orders": rows}
    where = "date>=? AND date<=?"
    args = [frm, to]
    if user["role"] == "team":
        where += " AND team_id=?"
        args.append(business.bound_team_id(user) or -1)
    elif user["role"] == "staff" and not user.get("lead"):
        where += " AND (assignee_id=? OR id IN (SELECT order_id FROM work_sessions WHERE worker_id=?))"
        args.extend((user["open_id"], user["open_id"]))
    elif grp:
        where += " AND grp=?"
        args.append(grp)
    rows = [dict(r) for r in conn.execute(
        """SELECT id,date,task_type,minutes,status,assignee_id,assignee_name,started_at,done_at
           FROM orders WHERE """ + where, args)]
    order_ids = [row["id"] for row in rows]
    sessions = []
    if order_ids:
        marks = ",".join("?" for _ in order_ids)
        sessions = [dict(r) for r in conn.execute(
            f"""SELECT s.order_id,s.worker_id,s.started_at,s.ended_at,s.end_reason,
                       o.date,o.task_type,o.status
                FROM work_sessions s JOIN orders o ON o.id=s.order_id
                WHERE s.order_id IN ({marks}) ORDER BY s.id""", order_ids)]
    conn.close()
    summary = {"orders": len(rows), "pending": 0, "assigned": 0, "in_progress": 0,
               "completion_review": 0, "completed": 0, "cancelled": 0, "abnormal": 0, "minutes": 0,
               "days": len({r["date"] for r in rows}), "people": 0,
               "usedSum": 0.0, "timed": 0}
    by_person, by_type, by_day, people = {}, {}, {}, set()
    order_actual = {}
    for r in rows:
        st, mins = r["status"], (r["minutes"] or 0)
        summary["minutes"] += mins
        if st in business.ORDER_STATUS:
            summary[st] += 1
        t = by_type.setdefault(r["task_type"],
                               {"task_type": r["task_type"], "orders": 0, "minutes": 0,
                                "usedSum": 0.0, "timed": 0})
        t["orders"] += 1
        t["minutes"] += mins
        d = by_day.setdefault(r["date"], {"date": r["date"], "orders": 0, "completed": 0})
        d["orders"] += 1
        if st == "completed":
            d["completed"] += 1
    person_orders = {}
    person_completed = {}
    for session in sessions:
        worker_id = session["worker_id"]
        people.add(worker_id)
        p = by_person.setdefault(
            worker_id,
            {"open_id": worker_id, "name": amap.get(worker_id) or worker_id,
             "orders": 0, "completed": 0, "minutes": 0.0, "types": {}, "lastDate": ""},
        )
        person_orders.setdefault(worker_id, set()).add(session["order_id"])
        if session["status"] == "completed":
            person_completed.setdefault(worker_id, set()).add(session["order_id"])
        p["types"][session["task_type"]] = p["types"].get(session["task_type"], 0) + 1
        p["lastDate"] = max(p["lastDate"], session["date"])
        used = used_minutes(session["started_at"], session["ended_at"])
        if used is not None:
            p["minutes"] += used
            order_actual[session["order_id"]] = order_actual.get(session["order_id"], 0.0) + used
    for worker_id, p in by_person.items():
        p["orders"] = len(person_orders.get(worker_id, set()))
        p["completed"] = len(person_completed.get(worker_id, set()))
    for order in rows:
        used = order_actual.get(order["id"])
        if used is None:
            continue
        summary["usedSum"] += used
        summary["timed"] += 1
        t = by_type[order["task_type"]]
        t["usedSum"] += used
        t["timed"] += 1
    summary["people"] = len(people)
    summary["avgUsed"] = round(summary["usedSum"] / summary["timed"], 1) if summary["timed"] else None
    bp = []
    if user["role"] != "team":
        for p in by_person.values():
            top = max(p["types"].items(), key=lambda kv: kv[1])[0] if p["types"] else ""
            bp.append({"open_id": p["open_id"], "name": p["name"], "orders": p["orders"],
                       "completed": p["completed"], "minutes": p["minutes"], "topType": top,
                       "lastDate": p["lastDate"]})
    else:
        summary["people"] = 0
    bp.sort(key=lambda x: x["orders"], reverse=True)
    bt = []
    for t in by_type.values():
        bt.append({"task_type": t["task_type"], "orders": t["orders"], "minutes": t["usedSum"],
                   "timed": t["timed"],
                   "avgUsed": round(t["usedSum"] / t["timed"], 1) if t["timed"] else None,
                   "plannedAvg": round(t["minutes"] / t["orders"]) if t["orders"] else None})
    bt.sort(key=lambda x: x["orders"], reverse=True)
    return 200, {"range": rng, "from": frm, "to": to, "summary": summary, "byPerson": bp,
                 "byType": bt,
                 "byDay": sorted(by_day.values(), key=lambda x: x["date"])}


SAVE_WHITELIST = {
    "teams": {"name", "room", "rehearsal", "onair", "lighting_owner_id"},
    "sla": {"task_type", "minutes", "buffer", "note", "grp"},
    "shifts": {"name", "start", "end", "sort"},
    "users": {"role", "grp", "lead", "shift", "status", "work_hours", "intern",
              "absence_type", "absence_start", "absence_end", "alias",
              "approved", "blocked", "read_only", "team_id"},
    "groups": {"name", "color", "sort", "auto"},
}
LEAD_USER_FIELDS = {"shift", "status", "intern", "absence_type", "absence_start", "absence_end"}


def do_save(user, body):
    entity = body.get("entity")
    rec = body.get("record") or {}
    is_admin = user["role"] == "admin"
    is_lead = business.is_manager(user) and not is_admin
    # 管理员改一切;组长只能改本组人员的班次/状态/实习/离岗安排
    if not is_admin and not (is_lead and entity == "users"):
        return 403, "仅管理员或组长可编辑"
    if entity not in SAVE_WHITELIST:
        return 400, "非法对象"
    allowed = SAVE_WHITELIST[entity] if is_admin else LEAD_USER_FIELDS
    fields = {k: v for k, v in rec.items() if k in allowed}
    if not fields:
        return 400, "没有可保存的字段"
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    if entity == "users":
        oid = rec.get("open_id")
        if not oid:
            conn.close()
            return 400, "缺 open_id(人员由飞书登录产生,不能手工新建)"
        if not is_admin:
            trow = conn.execute("SELECT grp FROM users WHERE open_id=?", (oid,)).fetchone()
            if not trow or (trow["grp"] or "") != (user.get("grp") or ""):
                conn.close()
                return 403, "组长只能管本组成员"
        target_user = conn.execute("SELECT * FROM users WHERE open_id=?", (oid,)).fetchone()
        if not target_user:
            conn.close()
            return 400, "用户不存在"
        self_managed_fields = {
            "role", "lead", "intern", "shift", "status", "grp", "approved", "blocked",
            "read_only", "absence_type", "absence_start", "absence_end",
        }
        if oid == user["open_id"] and (self_managed_fields & set(fields.keys())):
            conn.close()
            return 403, "不能修改自己的身份、准入、排班、状态或离岗安排，请让其他管理员操作"
        if fields.get("role") and fields["role"] not in ROLES:
            conn.close()
            return 400, "非法角色"
        if "grp" in fields and fields["grp"]:
            if not conn.execute("SELECT 1 FROM groups WHERE name=?", (fields["grp"],)).fetchone():
                conn.close()
                return 400, "非法组别"
        if "lead" in fields:
            fields["lead"] = 1 if str(fields["lead"]) in ("1", "true", "on", "yes") else 0
        if "intern" in fields:
            fields["intern"] = 1 if str(fields["intern"]) in ("1", "true", "on", "yes") else 0
        if "alias" in fields:
            fields["alias"] = str(fields["alias"] or "").strip()[:20]  # 显示名,空=用飞书名
        if "approved" in fields:
            fields["approved"] = 1 if str(fields["approved"]) in ("1", "true", "on", "yes") else 0
        if "blocked" in fields:
            fields["blocked"] = 1 if str(fields["blocked"]) in ("1", "true", "on", "yes") else 0
        if "read_only" in fields:
            fields["read_only"] = 1 if str(fields["read_only"]) in ("1", "true", "on", "yes") else 0
        if fields.get("blocked") == 1:
            fields["approved"] = 0
        elif fields.get("approved") == 1:
            fields["blocked"] = 0
        if fields.get("role") and fields["role"] != "admin":
            fields["read_only"] = 0
        if fields.get("read_only") == 1 and fields.get("role", target_user["role"]) != "admin":
            conn.close()
            return 400, "管理层只读必须使用管理端身份"
        if "team_id" in fields:
            team_id = record_id(fields["team_id"])
            if team_id and not conn.execute("SELECT 1 FROM teams WHERE id=?", (team_id,)).fetchone():
                conn.close()
                return 400, "绑定团队不存在"
            fields["team_id"] = team_id
        final_role = fields.get("role", target_user["role"])
        final_team_id = fields.get("team_id", target_user["team_id"])
        final_group = fields.get("grp", target_user["grp"])
        final_approved = fields.get("approved", target_user["approved"])
        final_blocked = fields.get("blocked", target_user["blocked"])
        if final_approved and not final_blocked:
            if final_role == "team" and not final_team_id:
                conn.close()
                return 400, "团队下单账号必须先绑定直播团队"
            if final_role == "staff" and not final_group:
                conn.close()
                return 400, "工作人员账号必须先选择所属分组"
        absence_fields = {"absence_type", "absence_start", "absence_end"}
        if absence_fields & set(fields):
            absence_type = str(fields.get("absence_type", target_user["absence_type"]) or "").strip()
            absence_start = str(fields.get("absence_start", target_user["absence_start"]) or "").strip()
            absence_end = str(fields.get("absence_end", target_user["absence_end"]) or "").strip()
            if not absence_type and (absence_start or absence_end):
                conn.close()
                return 400, "请选择离岗类型"
            if not absence_type:
                absence_start = ""
                absence_end = ""
            elif absence_type not in business.ABSENCE_TYPES:
                conn.close()
                return 400, "离岗类型无效"
            else:
                try:
                    start_date = datetime.strptime(absence_start, "%Y-%m-%d").date()
                    end_date = datetime.strptime(absence_end, "%Y-%m-%d").date()
                except ValueError:
                    conn.close()
                    return 400, "离岗开始和结束日期必须完整"
                if start_date > end_date:
                    conn.close()
                    return 400, "离岗结束日期不能早于开始日期"
                if end_date < datetime.strptime(today(), "%Y-%m-%d").date():
                    conn.close()
                    return 400, "不能新增已经结束的离岗记录"
            fields.update({
                "absence_type": absence_type,
                "absence_start": absence_start,
                "absence_end": absence_end,
            })
        if fields.get("shift"):
            valid_shifts = {r["name"] for r in conn.execute("SELECT name FROM shifts")}
            if fields["shift"] not in valid_shifts:
                conn.close()
                return 400, "非法班次"
        if "status" in fields and fields["status"] not in TECH_STATUS:
            conn.close()
            return 400, "非法状态"
        active = conn.execute(
            "SELECT COUNT(*) c FROM work_sessions WHERE worker_id=? AND ended_at=''", (oid,)
        ).fetchone()["c"]
        assigned_or_active = conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE assignee_id=? AND status IN ('assigned','in_progress','completion_review')", (oid,)
        ).fetchone()["c"]
        current_or_imminent = conn.execute(
            """SELECT COUNT(*) c FROM orders
               WHERE assignee_id=? AND status IN ('assigned','in_progress','completion_review')
               AND (status IN ('in_progress','completion_review') OR scheduled_start<=?) AND scheduled_end>?""",
            (
                oid,
                business.format_dt(datetime.now() + timedelta(minutes=15)),
                business.format_dt(datetime.now()),
            ),
        ).fetchone()["c"]
        if current_or_imminent and "status" in fields and fields["status"] != "busy":
            conn.close()
            return 409, "该人员有正在执行或即将开始的工单，只能保持忙碌状态"
        invalidates_assignment = (
            fields.get("role") in ("team", "admin")
            or ("grp" in fields and fields["grp"] != (target_user["grp"] or ""))
            or ("shift" in fields and fields["shift"] != (target_user["shift"] or ""))
            or fields.get("intern") == 1
            or fields.get("approved") == 0
            or fields.get("blocked") == 1
        )
        if assigned_or_active and invalidates_assignment:
            conn.close()
            return 409, f"该人员还有 {assigned_or_active} 张待执行或进行中工单，请先改派或完成"
        absence_type = fields.get("absence_type")
        if absence_type:
            absence_conflicts = conn.execute(
                """SELECT COUNT(*) c FROM orders WHERE assignee_id=? AND status IN ('assigned','in_progress','completion_review')
                   AND date>=? AND date<=?""",
                (oid, fields["absence_start"], fields["absence_end"]),
            ).fetchone()["c"]
            if absence_conflicts:
                conn.close()
                label = business.ABSENCE_LABELS[absence_type]
                return 409, f"{label}区间内还有 {absence_conflicts} 张工单，请先改派"
        # 防呆:唯一管理员不能把自己降级
        if fields.get("role") and fields["role"] != "admin" and oid == user["open_id"]:
            n_admin = conn.execute("SELECT COUNT(*) c FROM users WHERE role='admin'").fetchone()["c"]
            if n_admin <= 1:
                conn.close()
                return 400, "你是唯一管理员,先把别人设为管理员再降级自己"
        if fields.get("role") == "team":
            if active:
                conn.close()
                return 409, "该人员还有进行中工单,完成或改派后才能改为团队角色"
            fields["status"] = "off"
            fields["task"] = ""
            fields["grp"] = ""
            fields["lead"] = 0
            fields["intern"] = 0
            fields["read_only"] = 0
        elif fields.get("role") == "admin":
            fields["team_id"] = None
            fields["status"] = "off"
            fields["task"] = ""
            fields["grp"] = ""
            fields["lead"] = 0
            fields["intern"] = 0
        elif fields.get("role") == "staff":
            fields["team_id"] = None
            fields["read_only"] = 0
        sets = ",".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE users SET {sets} WHERE open_id=?", (*fields.values(), oid))
        updated_user = conn.execute("SELECT * FROM users WHERE open_id=?", (oid,)).fetchone()
        if not (
            updated_user["role"] == "staff"
            and (updated_user["grp"] or "") == "灯光组"
            and not updated_user["intern"]
            and updated_user["approved"]
            and not updated_user["blocked"]
        ):
            conn.execute("UPDATE teams SET lighting_owner_id='' WHERE lighting_owner_id=?", (oid,))
        if fields.get("blocked") == 1 or fields.get("approved") == 0:
            conn.execute("DELETE FROM sessions WHERE open_id=?", (oid,))
        target_desc = target_user["name"]
    else:
        rid = record_id(rec.get("id"))
        grp_old_name = None
        shift_old_name = None
        if entity == "teams":
            if "name" in fields:
                fields["name"] = str(fields["name"] or "").strip()
                if not fields["name"]:
                    conn.close()
                    return 400, "团队名不能为空"
            current = conn.execute("SELECT * FROM teams WHERE id=?", (rid,)).fetchone() if rid else None
            rehearsal = fields.get("rehearsal", current["rehearsal"] if current else "")
            onair = fields.get("onair", current["onair"] if current else "")
            if rehearsal and not first_schedule_time(rehearsal):
                conn.close()
                return 400, "彩排时间格式应为 HH:MM,多场用 / 分隔"
            if onair and not first_schedule_time(onair):
                conn.close()
                return 400, "开播时间格式应为 HH:MM"
            if "lighting_owner_id" in fields:
                fields["lighting_owner_id"] = str(fields["lighting_owner_id"] or "").strip()
                if fields["lighting_owner_id"]:
                    owner = conn.execute(
                        """SELECT 1 FROM users WHERE open_id=? AND role='staff' AND grp='灯光组'
                           AND COALESCE(intern,0)=0 AND COALESCE(approved,0)=1
                           AND COALESCE(blocked,0)=0""",
                        (fields["lighting_owner_id"],),
                    ).fetchone()
                    if not owner:
                        conn.close()
                        return 400, "本职灯光师必须是已授权的灯光组正式成员"
        if entity == "sla":
            if "task_type" in fields:
                fields["task_type"] = str(fields["task_type"] or "").strip()
                if not fields["task_type"]:
                    conn.close()
                    return 400, "任务类型不能为空"
            try:
                if "minutes" in fields:
                    fields["minutes"] = int(fields["minutes"])
                if "buffer" in fields:
                    fields["buffer"] = int(fields["buffer"])
            except (TypeError, ValueError):
                conn.close()
                return 400, "工时和缓冲必须是整数"
            current = conn.execute("SELECT * FROM sla WHERE id=?", (rid,)).fetchone() if rid else None
            minutes = fields.get("minutes", current["minutes"] if current else None)
            buffer = fields.get("buffer", current["buffer"] if current else None)
            if minutes is None or minutes <= 0:
                conn.close()
                return 400, "标准工时必须大于 0"
            if buffer is None or buffer < 0:
                conn.close()
                return 400, "缓冲不能小于 0"
            if current:
                next_grp = fields.get("grp", current["grp"])
                next_task_type = fields.get("task_type", current["task_type"])
                if (next_grp, next_task_type) != (current["grp"], current["task_type"]):
                    active_rules = conn.execute(
                        """SELECT COUNT(*) count FROM orders WHERE grp=? AND task_type=?
                           AND status IN ('pending','assigned','in_progress','completion_review')""",
                        (current["grp"], current["task_type"]),
                    ).fetchone()["count"]
                    if active_rules:
                        conn.close()
                        return 409, f"该任务类型还有 {active_rules} 张未结束订单，结束后才能改名或换组"
        if entity == "shifts":
            current = conn.execute("SELECT * FROM shifts WHERE id=?", (rid,)).fetchone() if rid else None
            shift_old_name = current["name"] if current else None
            if "name" in fields:
                fields["name"] = str(fields["name"] or "").strip()
                if not fields["name"]:
                    conn.close()
                    return 400, "班次名不能为空"
            for key, label in (("start", "开始"), ("end", "结束")):
                if key in fields:
                    t = first_schedule_time(fields[key])
                    if not t:
                        conn.close()
                        return 400, f"{label}时间格式应为 HH:MM(如 09:00)"
                    fields[key] = t
            if current and ({"start", "end"} & set(fields)):
                next_start = fields.get("start", current["start"])
                next_end = fields.get("end", current["end"])
                active_orders = conn.execute(
                    """SELECT o.* FROM orders o
                       JOIN users u ON u.open_id=o.assignee_id
                       LEFT JOIN staff_shift_overrides so
                         ON so.worker_id=u.open_id AND so.work_date=o.date
                       WHERE o.status IN ('assigned','in_progress','completion_review')
                       AND (
                         (so.worker_id IS NULL AND u.shift=?)
                         OR (so.is_off=0 AND so.shift_id=?)
                       )""",
                    (current["name"], current["id"]),
                ).fetchall()
                for active_order in active_orders:
                    order_start, order_end = business.order_bounds(active_order)
                    shift_start, shift_end = business.shift_bounds(
                        active_order["date"], next_start, next_end
                    )
                    if (
                        not order_start
                        or not order_end
                        or not shift_start
                        or not shift_end
                        or order_start < shift_start
                        or order_end > shift_end
                    ):
                        conn.close()
                        return 409, f"新班次时间会使工单 #{active_order['id']} 超出排班范围，请先改派"
        if entity == "groups":
            if "name" in fields:
                fields["name"] = str(fields["name"] or "").strip()
                if not fields["name"]:
                    conn.close()
                    return 400, "组名不能为空"
                if rid:
                    _o = conn.execute("SELECT name FROM groups WHERE id=?", (rid,)).fetchone()
                    grp_old_name = _o["name"] if _o else None
            if "sort" in fields:
                try:
                    fields["sort"] = int(fields["sort"])
                except (TypeError, ValueError):
                    fields.pop("sort")
            if "auto" in fields:
                fields["auto"] = 1 if str(fields["auto"]) in ("1", "true", "on", "yes") else 0
            if "color" in fields:
                color = str(fields["color"] or "").strip()
                if len(color) != 7 or not color.startswith("#") or any(
                    ch not in "0123456789abcdefABCDEF" for ch in color[1:]
                ):
                    conn.close()
                    return 400, "组颜色必须是 #RRGGBB 格式"
                fields["color"] = color
        if entity == "sla" and fields.get("grp"):
            if not conn.execute("SELECT 1 FROM groups WHERE name=?", (fields["grp"],)).fetchone():
                conn.close()
                return 400, "非法组别"
        if rid:
            sets = ",".join(f"{k}=?" for k in fields)
            try:
                changed = conn.execute(
                    f"UPDATE {entity} SET {sets} WHERE id=?", (*fields.values(), rid)
                ).rowcount
            except sqlite3.IntegrityError:
                conn.close()
                return 409, "名称已存在"
            if changed != 1:
                conn.close()
                return 400, "记录不存在"
            target_desc = f"#{rid}"
        else:
            required = {"teams": {"name"}, "sla": {"task_type", "minutes", "buffer", "grp"},
                        "shifts": {"name", "start", "end"}, "groups": {"name"}}[entity]
            if not required.issubset(fields):
                conn.close()
                return 400, "新增记录缺少必填字段"
            cols = ",".join(fields)
            marks = ",".join("?" * len(fields))
            try:
                conn.execute(f"INSERT INTO {entity}({cols}) VALUES({marks})", tuple(fields.values()))
            except sqlite3.IntegrityError:
                conn.close()
                return 409, "名称已存在"
            target_desc = "新增"
        if entity == "groups" and grp_old_name and grp_old_name != fields.get("name"):
            newn = fields["name"]
            conn.execute("UPDATE users SET grp=? WHERE grp=?", (newn, grp_old_name))
            conn.execute("UPDATE orders SET grp=? WHERE grp=?", (newn, grp_old_name))
            conn.execute("UPDATE sla SET grp=? WHERE grp=?", (newn, grp_old_name))
        if entity == "shifts" and shift_old_name and fields.get("name") and fields["name"] != shift_old_name:
            conn.execute("UPDATE users SET shift=? WHERE shift=?", (fields["name"], shift_old_name))
    insert_audit(conn, user, f"编辑{entity}", f"{target_desc} {json.dumps(fields, ensure_ascii=False)[:200]}")
    conn.commit()
    conn.close()
    return 200, None


def do_remove(user, body):
    if user["role"] != "admin":
        return 403, "仅管理员可删除"
    entity = body.get("entity")
    rid = record_id(body.get("id"))
    if entity not in ("teams", "sla", "orders", "shifts", "groups"):
        return 400, "该对象不允许删除(人员请改角色为 team)"
    if not rid:
        return 400, "非法记录编号"
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    task_guid = ""
    if entity == "orders":
        order = conn.execute("SELECT status,feishu_task_guid FROM orders WHERE id=?", (rid,)).fetchone()
        if order:
            conn.close()
            return 409, "业务工单不可物理删除，请使用取消或异常终止"
    if entity == "groups":
        grow = conn.execute("SELECT name FROM groups WHERE id=?", (rid,)).fetchone()
        if grow:
            gname = grow["name"]
            n_u = conn.execute("SELECT COUNT(*) c FROM users WHERE grp=?", (gname,)).fetchone()["c"]
            n_o = conn.execute("SELECT COUNT(*) c FROM orders WHERE grp=? AND status IN ('pending','assigned','in_progress','completion_review')",
                               (gname,)).fetchone()["c"]
            n_s = conn.execute("SELECT COUNT(*) c FROM sla WHERE grp=?", (gname,)).fetchone()["c"]
            if n_u or n_o or n_s:
                conn.close()
                return 409, f"该组还有 {n_u} 名成员 / {n_o} 张未完成工单 / {n_s} 条任务规则，请先转移再删"
            if conn.execute("SELECT COUNT(*) c FROM groups").fetchone()["c"] <= 1:
                conn.close()
                return 400, "至少保留一个组"
    if entity == "shifts":
        shift = conn.execute("SELECT name FROM shifts WHERE id=?", (rid,)).fetchone()
        if shift:
            count = conn.execute("SELECT COUNT(*) count FROM users WHERE shift=?", (shift["name"],)).fetchone()["count"]
            override_count = conn.execute(
                "SELECT COUNT(*) count FROM staff_shift_overrides WHERE shift_id=?", (rid,)
            ).fetchone()["count"]
            if count or override_count:
                conn.close()
                return 409, f"该班次仍被 {count} 名人员默认使用 / {override_count} 条按天排班使用，请先调整"
    if entity == "teams":
        bound = conn.execute("SELECT COUNT(*) count FROM users WHERE team_id=?", (rid,)).fetchone()["count"]
        active = conn.execute(
            "SELECT COUNT(*) count FROM orders WHERE team_id=? AND status IN ('pending','assigned','in_progress','completion_review')",
            (rid,),
        ).fetchone()["count"]
        if bound or active:
            conn.close()
            return 409, f"该团队仍绑定 {bound} 个账号 / {active} 张未结束订单，不能删除"
    if entity == "sla":
        rule = conn.execute("SELECT grp,task_type FROM sla WHERE id=?", (rid,)).fetchone()
        if rule:
            active = conn.execute(
                """SELECT COUNT(*) count FROM orders WHERE grp=? AND task_type=?
                   AND status IN ('pending','assigned','in_progress','completion_review')""",
                (rule["grp"], rule["task_type"]),
            ).fetchone()["count"]
            if active:
                conn.close()
                return 409, f"该任务类型仍有 {active} 张未结束订单，不能删除"
    if conn.execute(f"DELETE FROM {entity} WHERE id=?", (rid,)).rowcount != 1:
        conn.close()
        return 400, "记录不存在"
    insert_audit(conn, user, f"删除{entity}", f"#{rid}")
    conn.commit()
    conn.close()
    if task_guid:
        delete_task_reminder(task_guid)
    return 200, None


SETTING_WHITELIST = {"reminder_on", "reminder_minutes", "urgent_on",
                     "book_team_cap", "book_grace_min",
                     "book_team_day_min", "rule_dev_note", "rule_biange_media", "rule_done_note",
                     "book_horizon_days", "start_early_min",
                     "nav_team", "nav_staff", "gate_mode", "allow_dept_ids", "overrun_remind_min"}


def do_set_setting(user, body):
    if user["role"] != "admin":
        return 403, "仅管理员可修改设置"
    k = body.get("key")
    if k not in SETTING_WHITELIST:
        return 400, "不支持的设置项"
    v = str(body.get("value", "")).strip()
    if k in ("reminder_on", "urgent_on", "rule_dev_note",
             "rule_biange_media", "rule_done_note"):
        v = "1" if v in ("1", "true", "on", "yes") else "0"
    elif k == "book_team_cap":
        try:
            v = str(max(0, min(10, int(v or 2))))
        except ValueError:
            return 400, "上限需为数字"
    elif k == "book_grace_min":
        try:
            v = str(max(0, min(120, int(v or 15))))
        except ValueError:
            return 400, "分钟需为数字"
    elif k == "book_horizon_days":
        try:
            v = str(max(1, min(31, int(v or 7))))
        except ValueError:
            return 400, "预约天数需为数字"
    elif k == "start_early_min":
        try:
            v = str(max(0, min(120, int(v or 30))))
        except ValueError:
            return 400, "提前开始分钟需为数字"
    elif k == "book_team_day_min":
        try:
            v = str(max(0, min(1440, int(v or 240))))  # 0=不限
        except ValueError:
            return 400, "分钟需为数字"
    elif k == "overrun_remind_min":
        try:
            v = str(max(0, min(1440, int(v or 0))))
        except ValueError:
            return 400, "分钟需为数字"
    elif k == "gate_mode":
        if v not in ("dept", "manual"):
            return 400, "非法准入模式"
    elif k == "allow_dept_ids":
        v = ",".join(x.strip() for x in v.split(",") if x.strip())[:500]
    elif k in ("nav_team", "nav_staff"):
        allowed = {"request", "orders", "crewboard", "stats", "audit"} if k == "nav_team" \
            else {"work", "open", "crewboard", "stats", "audit"}
        ids = [x for x in dict.fromkeys(v.split(",")) if x in allowed]
        if not ids:
            return 400, "至少保留一个页面"
        v = ",".join(ids)
    elif k == "reminder_minutes":
        try:
            v = str(max(0, min(180, int(v or 15))))
        except ValueError:
            return 400, "提前分钟需为数字"
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (k, v))
    insert_audit(conn, user, "改设置", f"{k}={v}")
    conn.commit()
    conn.close()
    return 200, None


def do_purge(user, body):
    """数据维护(仅管理员):清空工单或审计,用于清掉测试期间留下的数据。人员/团队/班次/规则全部保留。"""
    if user["role"] != "admin":
        return 403, "仅管理员可清理数据"
    if not TEST_MODE:
        return 403, "正式环境禁止清空业务数据"
    what = (body.get("what") or "").strip()
    if (body.get("confirm") or "") != "清空":
        return 400, "确认口令不对(需输入「清空」两个字)"
    conn = db()
    if what == "orders":
        conn.execute("BEGIN IMMEDIATE")
        n = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
        guids = [r["feishu_task_guid"] for r in conn.execute(
            "SELECT feishu_task_guid FROM orders WHERE COALESCE(feishu_task_guid,'')!=''")]
        conn.execute("DELETE FROM time_reviews")
        conn.execute("DELETE FROM work_sessions")
        conn.execute("DELETE FROM transfers")
        conn.execute("DELETE FROM order_events")
        conn.execute("DELETE FROM uploads WHERE order_id IS NOT NULL")
        conn.execute("DELETE FROM orders")
        conn.execute("UPDATE users SET status=CASE WHEN status IN('busy','check') THEN 'free' ELSE status END, task='' WHERE role!='team'")
        insert_audit(conn, user, "清空工单数据", f"删除 {n} 张工单(人员/团队/配置保留)")
        conn.commit()
        conn.close()
        for g in guids:
            delete_task_reminder(g)
        return 200, None
    if what == "audit":
        conn.close()
        return 403, "审计记录不可清空"
    conn.close()
    return 400, "不支持的清理对象"


def do_claim_admin(user, body):
    code = (body.get("code") or "").strip()
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    settings = {r["k"]: r["v"] for r in conn.execute(
        "SELECT k,v FROM settings WHERE k IN ('bootstrap_code','bootstrap_used')"
    )}
    if settings.get("bootstrap_used") == "1":
        conn.close()
        return 400, "引导码已被使用;请找现任管理员在「人员管理」里给你授权"
    if not secrets.compare_digest(code, settings.get("bootstrap_code", "")):
        conn.close()
        time.sleep(1.0)
        return 403, "引导码不对(在启动服务器的终端里找 6 位数字)"
    conn.execute("UPDATE users SET role='admin',approved=1 WHERE open_id=?", (user["open_id"],))
    conn.execute("UPDATE settings SET v='1' WHERE k='bootstrap_used' AND v='0'")
    insert_audit(conn, user, "认领管理员", "引导码通过")
    conn.commit()
    conn.close()
    return 200, None


# ───────────────────────── HTTP ─────────────────────────
RATE_LOCK = threading.Lock()
RATE_HITS = {}


def request_client_ip(peer, forwarded_for=""):
    """只信任本机反向代理传入的首个客户端地址。"""
    try:
        peer_address = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not peer_address.is_loopback or not forwarded_for:
        return peer
    candidate = forwarded_for.split(",", 1)[0].strip()
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return peer
    return candidate


def rate_limited(ip, path):
    """每个已登录账号或未登录 IP 每分钟普通请求 300 次、登录请求 30 次。"""
    is_auth = path.startswith("/auth/")
    burst = 30 if is_auth else 300
    now_min = int(time.time() // 60)
    with RATE_LOCK:
        key = (ip, is_auth)
        m, n = RATE_HITS.get(key, (now_min, 0))
        if m != now_min:
            m, n = now_min, 0
        n += 1
        RATE_HITS[key] = (m, n)
        if len(RATE_HITS) > 4096:  # 防字典无限膨胀
            RATE_HITS.clear()
    return n > burst


def reserve_upload(owner_id, token, path, kind, size):
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        usage = conn.execute(
            """SELECT COUNT(*) count,COALESCE(SUM(size),0) bytes FROM uploads
               WHERE owner_id=? AND status IN ('uploading','pending')""",
            (owner_id,),
        ).fetchone()
        if usage["count"] >= MEDIA_PENDING_FILE_LIMIT:
            return 429, f"你还有 {usage['count']} 个未使用附件，请先完成下单或等待过期清理"
        if usage["bytes"] + size > MEDIA_PENDING_BYTES:
            return 413, "未使用附件总容量已达上限，请先完成下单"
        reserved = conn.execute(
            "SELECT COALESCE(SUM(size),0) bytes FROM uploads WHERE status='uploading'"
        ).fetchone()["bytes"]
        free_bytes = shutil.disk_usage(MEDIA_DIR).free
        if free_bytes - reserved - size < MEDIA_DISK_RESERVE_BYTES:
            return 507, "服务器存储空间不足，请联系管理员"
        conn.execute(
            """INSERT INTO uploads(token,path,kind,owner_id,status,size,created_at)
               VALUES(?,?,?,?,'uploading',?,?)""",
            (token, path, kind, owner_id, size, now_iso()),
        )
        conn.commit()
        return 200, None
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "Dispatch/2.0"

    # —— helpers ——
    def _send(self, code, body, ctype="application/json", extra_headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if COOKIE_SECURE:
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'",
        )
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, url, cookies=None):
        self.send_response(302)
        self.send_header("Location", url)
        if COOKIE_SECURE:
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        for c in (cookies or []):
            self.send_header("Set-Cookie", c)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _cookie(self, name):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == name:
                    return v
        return ""

    def _client_ip(self):
        return request_client_ip(
            self.client_address[0],
            self.headers.get("X-Forwarded-For", ""),
        )

    def _user(self):
        if not hasattr(self, "_cached_user"):
            self._cached_user = session_user(self._cookie("sid"))
        return self._cached_user

    def _rate_key(self):
        user = self._user()
        if user:
            return f"user:{user['open_id']}"
        return f"ip:{self._client_ip()}"

    @staticmethod
    def _approved(user):
        return bool(
            user
            and not user.get("blocked", 0)
            and (user.get("role") == "admin" or user.get("approved", 0))
        )

    @classmethod
    def _can_write(cls, user):
        return cls._approved(user) and not user.get("read_only", 0)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            return {}
        if n > MAX_REQUEST_BYTES:
            raise ValueError("请求内容过大")
        return json.loads(self.rfile.read(n) or b"{}")

    def log_message(self, *a):
        pass

    def _handle_upload(self):
        u = self._user()
        if not u:
            return self._send(401, {"ok": False, "msg": "未登录"})
        if not self._approved(u):
            return self._send(403, {"ok": False, "msg": "账号未获授权或已被禁用"})
        if not self._can_write(u):
            return self._send(403, {"ok": False, "msg": "管理层只读账号不能上传或修改数据"})
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not (ctype.startswith("audio/") or ctype.startswith("video/")):
            return self._send(400, {"ok": False, "msg": "只支持音频或视频文件"})
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            return self._send(400, {"ok": False, "msg": "空文件"})
        if n > MEDIA_MAX_BYTES:
            return self._send(413, {"ok": False, "msg": f"文件过大(上限 {MEDIA_MAX_BYTES//1024//1024}MB)"})
        ext = MEDIA_EXT.get(ctype) or ("mp4" if ctype.startswith("video/") else "m4a")
        token = secrets.token_urlsafe(24)
        fname = f"{secrets.token_urlsafe(16)}.{ext}"
        dest = MEDIA_DIR / fname
        kind = "video" if ctype.startswith("video/") else "audio"
        reserve_code, reserve_error = reserve_upload(
            u["open_id"], token, f"/media/{fname}", kind, n
        )
        if reserve_code != 200:
            return self._send(reserve_code, {"ok": False, "msg": reserve_error})
        remaining = n
        try:
            with open(dest, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
        except Exception:
            dest.unlink(missing_ok=True)
            conn = db()
            conn.execute("UPDATE uploads SET status='expired' WHERE token=? AND status='uploading'", (token,))
            conn.commit()
            conn.close()
            return self._send(500, {"ok": False, "msg": "上传失败"})
        if remaining > 0:
            dest.unlink(missing_ok=True)
            conn = db()
            conn.execute("UPDATE uploads SET status='expired' WHERE token=? AND status='uploading'", (token,))
            conn.commit()
            conn.close()
            return self._send(400, {"ok": False, "msg": "上传中断,请重试"})
        conn = db()
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute(
            "UPDATE uploads SET status='pending' WHERE token=? AND owner_id=? AND status='uploading'",
            (token, u["open_id"]),
        ).rowcount
        if changed != 1:
            conn.rollback()
            conn.close()
            dest.unlink(missing_ok=True)
            return self._send(500, {"ok": False, "msg": "上传记录确认失败，请重试"})
        insert_audit(conn, u, "上传附件", f"{kind} {fname}")
        conn.commit()
        conn.close()
        return self._send(200, {"ok": True, "token": token, "kind": kind})

    def _serve_media(self, path):
        user = self._user()
        if not user:
            return self._send(403, {"ok": False, "msg": "未登录"})
        if not self._approved(user):
            return self._send(403, {"ok": False, "msg": "账号尚未通过管理员审核"})
        fname = os.path.basename(urllib.parse.unquote(path))
        dest = MEDIA_DIR / fname
        if not fname or not dest.is_file():
            return self._send(404, b"not found", "text/plain")
        conn = db()
        upload = conn.execute("SELECT * FROM uploads WHERE path=?", (f"/media/{fname}",)).fetchone()
        allowed = False
        if upload:
            if upload["status"] == "pending":
                allowed = upload["owner_id"] == user["open_id"] or user["role"] == "admin"
            elif upload["order_id"]:
                order = conn.execute("SELECT * FROM orders WHERE id=?", (upload["order_id"],)).fetchone()
                allowed = bool(order and business.can_view_order(user, order))
        else:
            allowed = user["role"] == "admin"
        conn.close()
        if not allowed:
            return self._send(403, {"ok": False, "msg": "无权查看该附件"})
        ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
        ctype = EXT_CONTENT_TYPE.get(ext, "application/octet-stream")
        size = dest.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if rng and rng.startswith("bytes="):
            try:
                s, _, e = rng[6:].partition("-")
                start = int(s) if s else 0
                end = int(e) if e else size - 1
                end = min(end, size - 1)
                if start > end or start >= size:
                    raise ValueError
                partial = True
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "private, max-age=86400")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(dest, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    # —— GET ——
    def do_GET(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            limit_key = self._rate_key()
            if rate_limited(limit_key, path):
                return self._send(429, {"ok": False, "msg": "请求太频繁,稍后再试"})
            if path in ("/", "/index.html"):
                return self._send(200, (ROOT / "index.html").read_bytes(), "text/html")
            if path == "/vendor/lucide.min.js":
                return self._send(200, (ROOT / "vendor" / "lucide.min.js").read_bytes(), "text/javascript")
            if path == "/manifest.webmanifest":
                return self._send(
                    200,
                    (ROOT / "manifest.webmanifest").read_bytes(),
                    "application/manifest+json",
                )
            if path == "/app-icon.svg":
                return self._send(200, (ROOT / "app-icon.svg").read_bytes(), "image/svg+xml")
            if path in ("/app-icon-180.png", "/app-icon-192.png", "/app-icon-512.png"):
                return self._send(200, (ROOT / path.removeprefix("/")).read_bytes(), "image/png")
            if path == "/favicon.ico":
                return self._send(200, (ROOT / "app-icon-192.png").read_bytes(), "image/png")
            if path == "/api/health":
                try:
                    conn = db()
                    conn.execute("SELECT 1").fetchone()
                    pending = conn.execute(
                        "SELECT COUNT(*) count FROM notification_outbox WHERE status IN ('pending','retry')"
                    ).fetchone()["count"]
                    failed = conn.execute(
                        "SELECT COUNT(*) count FROM notification_outbox WHERE status='failed'"
                    ).fetchone()["count"]
                    sending = conn.execute(
                        "SELECT COUNT(*) count FROM notification_outbox WHERE status='sending'"
                    ).fetchone()["count"]
                    stuck = conn.execute(
                        """SELECT COUNT(*) count FROM notification_outbox
                           WHERE status='sending' AND (claimed_at='' OR claimed_at<=?)""",
                        (business.format_dt(datetime.now() - timedelta(minutes=10)),),
                    ).fetchone()["count"]
                    scheduler = conn.execute(
                        "SELECT updated_at FROM system_health WHERE key='scheduler'"
                    ).fetchone()
                    conn.close()
                    scheduler_time = business.parse_dt(scheduler["updated_at"]) if scheduler else None
                    scheduler_ok = bool(
                        scheduler_time and datetime.now() - scheduler_time <= timedelta(seconds=120)
                    )
                    scheduler_starting = not scheduler_time and time.time() - PROCESS_STARTED <= 120
                    free_mb = shutil.disk_usage(DATA_ROOT).free // 1024 // 1024
                    healthy = (
                        (scheduler_ok or scheduler_starting)
                        and failed == 0
                        and stuck == 0
                        and free_mb >= 250
                    )
                    return self._send(200 if healthy else 503, {
                        "ok": healthy,
                        "version": SERVER_VERSION,
                        "db": "ok",
                        "scheduler": "ok" if scheduler_ok else ("starting" if scheduler_starting else "stale"),
                        "pending": pending,
                        "sending": sending,
                        "stuck": stuck,
                        "failed": failed,
                        "free_mb": free_mb,
                    })
                except Exception:
                    return self._send(503, {"ok": False, "version": SERVER_VERSION, "db": "error"})
            if path.startswith("/media/"):
                return self._serve_media(path)
            if path == "/api/me":
                u = self._user()
                if not u:
                    return self._send(401, {"ok": False, "configured": FEISHU_CONFIGURED,
                                            "test_mode": TEST_MODE,
                                            "msg": "未登录"})
                return self._send(200, {"ok": True, "data": {
                    k: u[k] for k in ("open_id", "name", "avatar", "role")}})
            if path == "/api/app-state":
                u = self._user()
                if not u:
                    return self._send(200, {
                        "ok": True,
                        "authenticated": False,
                        "configured": FEISHU_CONFIGURED,
                        "test_mode": TEST_MODE,
                        "server_version": SERVER_VERSION,
                    })
                return self._send(200, {
                    "ok": True,
                    "authenticated": True,
                    "data": api_state(u),
                })
            if path == "/api/state":
                u = self._user()
                if not u:
                    return self._send(401, {"ok": False, "configured": FEISHU_CONFIGURED,
                                            "test_mode": TEST_MODE,
                                            "msg": "未登录"})
                if not self._approved(u):
                    return self._send(403, {"ok": False, "msg": "账号尚未通过管理员审核"})
                return self._send(200, {"ok": True, "data": api_state(u)})
            if path == "/api/users":
                u = self._user()
                if not u:
                    return self._send(401, {"ok": False, "msg": "未登录"})
                if not self._approved(u):
                    return self._send(403, {"ok": False, "msg": "账号尚未通过管理员审核"})
                code, result = api_users(u)
                if code != 200:
                    return self._send(code, {"ok": False, "msg": result})
                return self._send(200, {"ok": True, "data": result})
            if path == "/api/stats":
                u = self._user()
                if not u:
                    return self._send(401, {"ok": False, "msg": "未登录"})
                if not self._approved(u):
                    return self._send(403, {"ok": False, "msg": "账号尚未通过管理员审核"})
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                code, result = api_stats(u, {k: v[0] for k, v in qs.items()})
                if code != 200:
                    return self._send(code, {"ok": False, "msg": result})
                return self._send(200, {"ok": True, "data": result})
            if path == "/api/schedule":
                u = self._user()
                if not u:
                    return self._send(401, {"ok": False, "msg": "未登录"})
                if not self._approved(u):
                    return self._send(403, {"ok": False, "msg": "账号尚未通过管理员审核"})
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                code, result = api_schedule(u, {k: v[0] for k, v in qs.items()})
                if code != 200:
                    return self._send(code, {"ok": False, "msg": result})
                return self._send(200, {"ok": True, "data": result})
            if path == "/api/audit":
                u = self._user()
                if not u:
                    return self._send(401, {"ok": False, "msg": "未登录"})
                if u["role"] != "admin":
                    return self._send(403, {"ok": False, "msg": "仅管理员可查看审计记录"})
                conn = db()
                amap = alias_map(conn)
                rows = [dict(r) for r in conn.execute(
                    "SELECT ts,open_id,name,action,detail FROM audit ORDER BY id DESC LIMIT 500")]
                for r in rows:
                    if r.get("open_id") and r["open_id"] in amap:
                        r["name"] = amap[r["open_id"]]
                    r.pop("open_id", None)
                conn.close()
                return self._send(200, {"ok": True, "data": rows})
            if path == "/auth/start":
                if TEST_MODE:
                    return self._send(403, "当前为独立测试模式,请使用测试身份入口".encode(), "text/plain")
                if not FEISHU_CONFIGURED:
                    return self._send(503, "飞书应用尚未配置,请先填写 DISPATCH_APP_ID 和 DISPATCH_APP_SECRET".encode(), "text/plain")
                state = secrets.token_urlsafe(16)
                q = urllib.parse.urlencode({
                    "app_id": APP_ID,
                    "redirect_uri": BASE_URL + "/auth/callback",
                    "response_type": "code", "state": state})
                return self._redirect(AUTHORIZE_URL + "?" + q,
                                      cookies=[f"oastate={state}; Path=/; HttpOnly; SameSite=Lax{COOKIE_SECURE}; Max-Age=600"])
            if path == "/auth/callback":
                if not FEISHU_CONFIGURED:
                    return self._send(503, "飞书应用尚未配置".encode(), "text/plain")
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                code = (qs.get("code") or [""])[0]
                state = (qs.get("state") or [""])[0]
                if not code or state != self._cookie("oastate"):
                    return self._send(400, "登录校验失败,请回到 App 重试".encode(), "text/plain")
                try:
                    user_tok = oauth_exchange(code)
                    info = fetch_user_info(user_tok)
                except Exception as e:
                    return self._send(502, f"飞书登录失败: {e}".encode(), "text/plain")
                upsert_user(info)
                deny = apply_login_gate(info["open_id"], info["name"])
                if deny:
                    return self._send(403, deny.encode(), "text/plain")
                sid = new_session(info["open_id"])
                audit_log({"open_id": info["open_id"], "name": info["name"]}, "登录", "飞书 OAuth")
                return self._redirect("/", cookies=[
                    f"sid={sid}; Path=/; HttpOnly; SameSite=Lax{COOKIE_SECURE}; Max-Age={SESSION_DAYS*86400}",
                    "oastate=; Path=/; Max-Age=0"])
            if path == "/logout":
                drop_session(self._cookie("sid"))
                return self._redirect("/", cookies=["sid=; Path=/; Max-Age=0"])
            self._send(404, {"ok": False, "msg": "not found"})
        except BrokenPipeError:
            pass
        except Exception as e:
            print(f"[http] GET {self.path} 失败: {e}")
            self._send(500, {"ok": False, "msg": "服务器处理失败"})

    # —— POST ——
    def do_POST(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            limit_key = self._rate_key()
            if rate_limited(limit_key, path):
                return self._send(429, {"ok": False, "msg": "请求太频繁,稍后再试"})
            if path == "/api/upload":
                return self._handle_upload()
            body = self._body()

            if path == "/auth/test":
                if not TEST_MODE:
                    return self._send(404, {"ok": False, "msg": "not found"})
                profile = TEST_PROFILES.get(body.get("profile"))
                if not profile:
                    return self._send(400, {"ok": False, "msg": "非法测试身份"})
                conn = db()
                conn.execute(
                    "UPDATE users SET last_login=? WHERE open_id=?",
                    (now_iso(), profile["open_id"]),
                )
                conn.commit()
                conn.close()
                sid = new_session(profile["open_id"])
                audit_log(profile, "登录", "独立测试模式")
                return self._send(
                    200,
                    {"ok": True},
                    extra_headers={
                        "Set-Cookie": (
                            f"sid={sid}; Path=/; HttpOnly; SameSite=Lax{COOKIE_SECURE}; "
                            f"Max-Age={SESSION_DAYS*86400}"
                        )
                    },
                )

            u = self._user()
            if not u:
                return self._send(401, {"ok": False, "msg": "未登录"})
            # 准入门:待审核或禁用账号不能调用业务接口；管理层只读只能读取 GET 数据。
            if not self._approved(u) and path != "/api/claim-admin":
                return self._send(403, {"ok": False, "msg": "账号未获授权或已被禁用，请联系管理员"})
            if not self._can_write(u):
                return self._send(403, {"ok": False, "msg": "管理层只读账号只能查看，不能执行修改操作"})

            routes = {
                "/api/claim-admin": do_claim_admin,
                "/api/order": SERVICE.order,
                "/api/duration-review": SERVICE.review_duration,
                "/api/assign": SERVICE.assign,
                "/api/start": SERVICE.start,
                "/api/report-start": SERVICE.report_start,
                "/api/complete": SERVICE.complete,
                "/api/completion-dispute": SERVICE.dispute_completion,
                "/api/time-review-resolve": SERVICE.resolve_time_review,
                "/api/schedule-adjust": do_schedule_adjust,
                "/api/save": do_save,
                "/api/remove": do_remove,
                "/api/setting": do_set_setting,
                "/api/reassign": SERVICE.reassign,
                "/api/cancel-order": SERVICE.cancel,
                "/api/edit-order": SERVICE.edit,
                "/api/transfer": SERVICE.request_transfer,
                "/api/transfer-approve": SERVICE.approve_transfer,
                "/api/transfer-accept": SERVICE.accept_transfer,
                "/api/transfer-withdraw": SERVICE.withdraw_transfer,
                "/api/purge": do_purge,
            }
            fn = routes.get(path)
            if not fn:
                return self._send(404, {"ok": False, "msg": "not found"})
            code, err = fn(u, body)
            if code == 200:
                return self._send(200, {"ok": True})
            return self._send(code, {"ok": False, "msg": err})
        except BrokenPipeError:
            pass
        except (json.JSONDecodeError, ValueError) as e:
            self._send(400, {"ok": False, "msg": str(e) or "请求格式错误"})
        except Exception as e:
            print(f"[http] POST {self.path} 失败: {e}")
            self._send(500, {"ok": False, "msg": "服务器处理失败"})


class DispatchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler_class, max_workers=64):
        self._worker_slots = threading.BoundedSemaphore(max_workers)
        super().__init__(address, handler_class)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(30)
        return request, address

    def process_request(self, request, client_address):
        if not self._worker_slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._worker_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()


def configure_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_DIR / "dispatch.log", maxBytes=5 * 1024 * 1024, backupCount=10, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)


def main():
    configure_logging()
    if DB_PATH.exists() and not TEST_MODE:
        backup_database(force=True)
    init_db()
    stop_event = threading.Event()
    worker = threading.Thread(target=background_loop, args=(stop_event,), daemon=True, name="dispatch-background")
    worker.start()
    print("=" * 52)
    print("灯光派工系统 · 企业级后端")
    print("=" * 52)
    if TEST_MODE:
        print("模式: 独立测试模式")
        print("     测试数据与正式库隔离,飞书登录和通知均不启用")
    elif FEISHU_CONFIGURED:
        print("模式: 飞书登录已启用")
        print(f"     回调: {BASE_URL}/auth/callback (需与飞书后台一致)")
    else:
        print("模式: 等待配置飞书应用")
        print("     请填写 DISPATCH_APP_ID、DISPATCH_APP_SECRET、DISPATCH_BASE_URL 后重启")
    if setting("bootstrap_used") != "1":
        print(f"★ 管理员引导码: {setting('bootstrap_code')}   (登录后点「认领管理员」输入,一次有效)")
    print(f"数据库: {DB_PATH}")
    print(f"监听:  http://{HOST}:{PORT}   (Ctrl+C 停止)")
    print("=" * 52)
    server = DispatchHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
