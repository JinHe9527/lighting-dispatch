#!/usr/bin/env python3
"""
灯光派工系统 · 企业级后端
================================
- 身份:飞书 OAuth 登录(真名+头像)
- 权限:服务器端 RBAC 强制 —— team(默认)/staff(灯光师)/admin(管理员)
- 数据:SQLite app/dispatch.db(WAL),首启自动从 data.json 迁移 teams+sla
- 审计:所有变更记录 谁/何时/干了什么
- 通知:飞书机器人私信(派单/抢单)
- 依赖:Python 3.9 标准库,零 pip

运行:  python3 app/server.py     → http://<本机IP>:8848
环境(.env,项目根):
  DISPATCH_APP_ID / DISPATCH_APP_SECRET   「灯光派工」飞书应用凭据(配好后启用真实登录+通知)
  DISPATCH_BASE_URL                        对外地址,如 http://192.168.1.10:8848(OAuth 回调用)
  DISPATCH_HOST / DISPATCH_PORT            默认 0.0.0.0 / 8848
"""
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
DB_PATH = ROOT / "dispatch.db"
LEGACY_JSON = ROOT / "data.json"
FEISHU = "https://open.feishu.cn/open-apis"
AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"

SESSION_DAYS = 30
TECH_STATUS = ("free", "busy", "check", "rest", "off")
ORDER_STATUS = ("wait", "working", "done")
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
HOST = os.environ.get("DISPATCH_HOST", "0.0.0.0")
PORT = int(os.environ.get("DISPATCH_PORT", "8848"))
TEST_MODE = os.environ.get("DISPATCH_TEST_MODE", "").strip().lower() in ("1", "true", "yes", "on")
SERVER_VERSION = "v0720h"  # 与 index.html 的 APP_VERSION 同步改;前端会校验,不一致提示重启服务
DB_PATH = ROOT / ("dispatch-test.db" if TEST_MODE else "dispatch.db")
FEISHU_CONFIGURED = bool(APP_ID and APP_SECRET) and not TEST_MODE
MAX_REQUEST_BYTES = 64 * 1024
MEDIA_DIR = ROOT / "media"
MEDIA_MAX_BYTES = 30 * 1024 * 1024
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
  rehearsal TEXT DEFAULT '', onair TEXT DEFAULT ''
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
  status TEXT NOT NULL DEFAULT 'wait',
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
    MEDIA_DIR.mkdir(exist_ok=True)
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
    })
    ensure_columns(conn, "users", {"grp": "TEXT DEFAULT ''", "lead": "INTEGER DEFAULT 0",
                                    "intern": "INTEGER DEFAULT 0", "leave_until": "TEXT DEFAULT ''",
                                    "alias": "TEXT DEFAULT ''",
                                    "approved": "INTEGER DEFAULT 0"})
    # 准入门:老用户一次性放行;此后新的飞书登录默认「待审核」,管理员在人员页放行
    if conn.execute("SELECT v FROM settings WHERE k='approved_migrated'").fetchone() is None:
        conn.execute("UPDATE users SET approved=1")
        conn.execute("INSERT OR REPLACE INTO settings(k,v) VALUES('approved_migrated','1')")
    ensure_columns(conn, "sla", {"grp": "TEXT DEFAULT ''"})
    ensure_columns(conn, "groups", {"auto": "INTEGER DEFAULT 0"})
    # 组:种子三个组 + 历史数据归入首个组 + 空组补默认任务分类
    conn.execute("DROP INDEX IF EXISTS idx_sla_task_type")  # 任务名允许跨组重复
    if conn.execute("SELECT COUNT(*) c FROM groups").fetchone()["c"] == 0:
        conn.executemany("INSERT INTO groups(name,color,sort) VALUES(?,?,?)", DEFAULT_GROUPS)
    conn.execute("UPDATE sla SET grp=? WHERE grp='' OR grp IS NULL", (FIRST_GROUP,))
    conn.execute("UPDATE users SET grp=? WHERE role='staff' AND (grp='' OR grp IS NULL)", (FIRST_GROUP,))
    conn.execute("UPDATE orders SET grp=? WHERE grp='' OR grp IS NULL", (FIRST_GROUP,))
    for gname, tasks in DEFAULT_GROUP_SLA.items():
        if conn.execute("SELECT COUNT(*) c FROM sla WHERE grp=?", (gname,)).fetchone()["c"] == 0:
            conn.executemany("INSERT INTO sla(task_type,minutes,buffer,note,grp) VALUES(?,?,?,?,?)",
                             [(t[0], t[1], t[2], t[3], gname) for t in tasks])
    if not conn.execute("SELECT 1 FROM sla WHERE grp='灯光组' AND task_type='编歌'").fetchone():
        conn.execute("INSERT INTO sla(task_type,minutes,buffer,note,grp) VALUES('编歌',60,10,'需附歌曲音频或视频','灯光组')")
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
    if conn.execute("SELECT COUNT(*) c FROM sla").fetchone()["c"] == 0:
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


def audit_log(user, action, detail=""):
    try:
        conn = db()
        conn.execute("INSERT INTO audit(ts,open_id,name,action,detail) VALUES(?,?,?,?,?)",
                     (now_iso(), user["open_id"] if user else "",
                      user["name"] if user else "系统", action, detail[:500]))
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


def pick_auto_assignee(conn, grp, order_date, arrive):
    """自动派单:挑本组 空闲+在班+非实习+未请假 且当天已派单最少的人(公平轮转);无则 None。"""
    cands = conn.execute(
        "SELECT * FROM users WHERE grp=? AND role!='team' AND status='free' "
        "AND COALESCE(intern,0)=0", (grp,)).fetchall()
    best, best_load = None, None
    for u in cands:
        if assign_block(conn, u, order_date, arrive):
            continue
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
    """登录准入。open=自动放行(自建应用只有本公司飞书账号能登录,公司外的人根本进不来);
    dept=登录时按部门自动校验(需通讯录权限+部门ID);manual=新账号需管理员人工放行。
    返回 None=放行继续登录,返回字符串=拒绝登录并显示原因。"""
    mode = setting("gate_mode", "open")
    conn = db()
    try:
        if mode == "open":
            conn.execute("UPDATE users SET approved=1 WHERE open_id=?", (open_id,))
            conn.commit()
            return None
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
    """工单/派单可见与可操作的组:管理员/团队=None(全部),组员/组长=本组名。"""
    if user["role"] in ("admin", "team"):
        return None
    return (user.get("grp") or "") or "__none__"


def _release_stale_bookings(conn):
    """预约到点 grace 分钟仍未点「开始」→ 自动取消释放人并通知(防约了不用白占)。"""
    try:
        grace = int(setting("book_grace_min", "15") or 15)
    except (TypeError, ValueError):
        grace = 15
    if grace <= 0:
        return  # 规则关闭:不自动释放
    now = datetime.now()
    rows = conn.execute(
        "SELECT * FROM orders WHERE COALESCE(booked,0)=1 AND status IN ('wait','working') "
        "AND COALESCE(started_at,'')=''").fetchall()
    released = []
    for o in rows:
        try:
            d = datetime.strptime(f"{o['date']} {o['arrive']}", "%Y-%m-%d %H:%M")
            if (o["arrive"] or "") < "05:00":
                d += timedelta(days=1)  # 排班日约定:凌晨时段属次日
        except (TypeError, ValueError):
            continue
        if now <= d + timedelta(minutes=grace):
            continue
        conn.execute("DELETE FROM orders WHERE id=?", (o["id"],))
        if o["assignee_id"]:
            _release_worker(conn, o["assignee_id"])
        released.append(dict(o))
    if released:
        conn.commit()
        for o in released:
            audit_log(None, "自动释放预约",
                      f"#{o['id']} {o['team']} {o['date']} {o['arrive']} {o['task_type']} 超{grace}分钟未开始")
            msg = (f"预约已自动释放:{o['date']} {o['arrive']} {o['team']} · {o['task_type']}"
                   f"(到点 {grace} 分钟未开始)。需要的话请重新下单。")
            try:
                if o.get("creator_id"):
                    notify(o["creator_id"], msg)
                if o.get("assignee_id") and o.get("assignee_id") != o.get("creator_id"):
                    notify(o["assignee_id"], msg)
            except Exception:
                pass


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


def _handle_overruns(conn):
    """开始后超过 计划工时+N分钟 未完成:先提醒一次;再超过 M 分钟自动关单释放人。
    防止「点了开始忘了点完成」把计时挂十几个小时、人一直占着。"""
    try:
        remind = int(setting("overrun_remind_min", "30") or 30)
    except (TypeError, ValueError):
        remind = 30
    try:
        close = int(setting("overrun_close_min", "180") or 180)
    except (TypeError, ValueError):
        close = 180
    if remind <= 0 and close <= 0:
        return
    now = datetime.now()
    rows = conn.execute(
        "SELECT * FROM orders WHERE status='working' AND COALESCE(started_at,'')!=''").fetchall()
    closed, reminded = [], []
    for o in rows:
        st = parse_ts(o["started_at"])
        if not st:
            continue
        overdue = (now - st).total_seconds() / 60 - (o["minutes"] or 30)
        if close > 0 and overdue >= close:
            conn.execute("UPDATE orders SET status='done',done_at=?,done_note=? WHERE id=?",
                         (now.strftime("%Y-%m-%d %H:%M:%S"),
                          f"超时自动关单:开始后超过计划工时 {close} 分钟仍未点完成", o["id"]))
            if o["assignee_id"]:
                _release_worker(conn, o["assignee_id"])
            closed.append(dict(o))
        elif remind > 0 and overdue >= remind and not o["overrun_notified"]:
            conn.execute("UPDATE orders SET overrun_notified=1 WHERE id=?", (o["id"],))
            reminded.append(dict(o))
    if closed or reminded:
        conn.commit()
    for o in closed:
        audit_log(None, "超时自动关单",
                  f"#{o['id']} {o['team']} {o['task_type']} 开始于{str(o['started_at'])[5:16]} 超计划{close}分钟未完成")
        try:
            if o.get("assignee_id"):
                notify(o["assignee_id"], f"你的工单已超时自动关单:{o['team']} · {o['task_type']}。"
                                          f"下次干完记得点「完成」哦。")
            if o.get("feishu_task_guid"):
                delete_task_reminder(o["feishu_task_guid"])
        except Exception:
            pass
    for o in reminded:
        try:
            if o.get("assignee_id"):
                notify(o["assignee_id"], f"提醒:{o['team']} · {o['task_type']} 已超过计划工时,"
                                          f"完成了请点「完成」;确实还在干可以继续,超 "
                                          f"{setting('overrun_close_min','180')} 分钟会自动关单。")
        except Exception:
            pass


def api_state(user):
    conn = db()
    _release_stale_bookings(conn)
    _handle_overruns(conn)
    amap = alias_map(conn)
    groups = [dict(r) for r in conn.execute("SELECT id,name,color,sort,COALESCE(auto,0) auto FROM groups ORDER BY sort,id")]
    staff = [dict(r) for r in conn.execute(
        """SELECT open_id,name,COALESCE(alias,'') alias,avatar,grp,lead,intern,leave_until,shift,status,task,work_hours
           FROM users WHERE grp!='' AND role!='team' ORDER BY grp,shift,name""")]
    for s in staff:
        s["fs_name"] = s["name"]
        s["name"] = dn(s)
    teams = [dict(r) for r in conn.execute("SELECT * FROM teams ORDER BY id")]
    sla = [dict(r) for r in conn.execute("SELECT * FROM sla ORDER BY grp,id")]
    shifts = [dict(r) for r in conn.execute("SELECT id,name,start,end FROM shifts ORDER BY sort,id")]
    scope = user_scope(user)
    if scope is None:
        orders = [dict(r) for r in conn.execute(
            "SELECT * FROM orders WHERE date=? OR status!='done' ORDER BY arrive,id", (today(),))]
    else:
        orders = [dict(r) for r in conn.execute(
            "SELECT * FROM orders WHERE (date=? OR status!='done') AND grp=? ORDER BY arrive,id",
            (today(), scope))]
    for o in orders:
        if o.get("assignee_id") and o["assignee_id"] in amap:
            o["assignee_name"] = amap[o["assignee_id"]]
        if o.get("creator_id") and o["creator_id"] in amap:
            o["creator_name"] = amap[o["creator_id"]]
    me = {k: user[k] for k in ("open_id", "name", "avatar", "role", "shift", "status", "task")}
    me["fs_name"] = me["name"]
    me["name"] = amap.get(user["open_id"], me["name"])
    me["grp"] = user.get("grp") or ""
    me["lead"] = user.get("lead") or 0
    me["approved"] = 1 if (user.get("approved", 1) or user["role"] == "admin" or setting("gate_mode", "open") == "open") else 0
    out = {"me": me, "groups": groups, "staff": staff, "teams": teams, "sla": sla,
           "shifts": shifts, "orders": orders, "test_mode": TEST_MODE,
           "server_version": SERVER_VERSION,
           "bootstrap_open": setting("bootstrap_used") != "1",
           "reminder": dict(zip(("on", "minutes"), reminder_conf())),
           "urgent_on": setting("urgent_on", "0") == "1",
           "rules": {"book_lock_sla": setting("rule_book_lock_sla", "1"),
                     "team_cap": setting("book_team_cap", "2"),
                     "team_day_min": setting("book_team_day_min", "240"),
                     "grace_min": setting("book_grace_min", "15"),
                     "max_min": setting("book_max_min", "720"),
                     "dev_note": setting("rule_dev_note", "1"),
                     "biange_media": setting("rule_biange_media", "1"),
                     "done_note": setting("rule_done_note", "1"),
                     "nav_team": setting("nav_team", ""),
                     "nav_staff": setting("nav_staff", ""),
                     "gate_mode": setting("gate_mode", "open"),
                     "allow_dept_ids": setting("allow_dept_ids", ""),
                     "overrun_remind_min": setting("overrun_remind_min", "30"),
                     "overrun_close_min": setting("overrun_close_min", "180")}}
    if user["role"] == "admin":
        out["users"] = [dict(r) for r in conn.execute(
            "SELECT open_id,name,COALESCE(alias,'') alias,avatar,role,grp,lead,intern,leave_until,shift,status,last_login,COALESCE(approved,1) approved FROM users ORDER BY role DESC,grp,name")]
        for u2 in out["users"]:
            u2["fs_name"] = u2["name"]
            u2["name"] = dn(u2)
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
    """本周/本月排班图:返回日期区间内工单(按组过滤,与看板一致)。"""
    rng = (params.get("range") or "week").strip()
    now = biz_now()  # 周/月区间与「今天」都按排班日,凌晨不跳日
    if rng == "month":
        frm = now.replace(day=1)
        nxt = frm.replace(year=frm.year + 1, month=1) if frm.month == 12 \
            else frm.replace(month=frm.month + 1)
        to = nxt - timedelta(days=1)
    else:
        rng = "week"
        frm = now - timedelta(days=now.weekday())  # 本周一
        to = frm + timedelta(days=6)
    frm_s, to_s = frm.strftime("%Y-%m-%d"), to.strftime("%Y-%m-%d")
    scope = user_scope(user)
    conn = db()
    sql = ("SELECT id,date,team,room,task_type,grp,arrive,minutes,assignee_id,assignee_name,status,urgent "
           "FROM orders WHERE date>=? AND date<=?")
    args = [frm_s, to_s]
    if scope is not None:
        sql += " AND grp=?"
        args.append(scope)
    sql += " ORDER BY date,arrive,id"
    rows = [dict(r) for r in conn.execute(sql, args)]
    amap = alias_map(conn)
    for r in rows:
        if r.get("assignee_id") and r["assignee_id"] in amap:
            r["assignee_name"] = amap[r["assignee_id"]]
    conn.close()
    return 200, {"range": rng, "from": frm_s, "to": to_s,
                 "today": now.strftime("%Y-%m-%d"), "orders": rows}


def api_stats(user, params):
    """统计:跨全部历史工单聚合。range=all|7d|30d|month;person=<open_id> 时返回该人明细。"""
    rng = params.get("range") or "30d"
    person = (params.get("person") or "").strip()
    grp = (params.get("group") or "").strip()
    if user["role"] not in ("admin", "team"):
        grp = (user.get("grp") or "").strip()  # 组员/组长:统计锁定本组;管理员和团队可看全部组
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
        prow = conn.execute("SELECT name,COALESCE(alias,'') alias FROM users WHERE open_id=?", (person,)).fetchone()
        rows = [dict(r) for r in conn.execute(
            """SELECT id,date,team,room,task_type,minutes,arrive,status,note,media,media_kind,media2,media2_kind,
                      started_at,done_at
               FROM orders WHERE assignee_id=? AND date>=? ORDER BY date DESC,arrive DESC""",
            (person, frm))]
        conn.close()
        for r in rows:
            r["used"] = used_minutes(r.get("started_at"), r.get("done_at"))
        return 200, {"range": rng, "from": frm, "to": to, "person": person,
                     "name": dn(prow) if prow else person, "orders": rows}
    if grp:
        rows = [dict(r) for r in conn.execute(
            """SELECT date,task_type,minutes,status,assignee_id,assignee_name,started_at,done_at
               FROM orders WHERE date>=? AND grp=?""", (frm, grp))]
    else:
        rows = [dict(r) for r in conn.execute(
            """SELECT date,task_type,minutes,status,assignee_id,assignee_name,started_at,done_at
               FROM orders WHERE date>=?""", (frm,))]
    conn.close()
    summary = {"orders": len(rows), "done": 0, "working": 0, "wait": 0, "minutes": 0,
               "days": len({r["date"] for r in rows}), "people": 0,
               "usedSum": 0.0, "timed": 0}
    by_person, by_type, by_day, people = {}, {}, {}, set()
    for r in rows:
        st, mins = r["status"], (r["minutes"] or 0)
        summary["minutes"] += mins
        if st in ("done", "working", "wait"):
            summary[st] += 1
        used = used_minutes(r.get("started_at"), r.get("done_at"))
        if used is not None:
            summary["usedSum"] += used
            summary["timed"] += 1
        t = by_type.setdefault(r["task_type"],
                               {"task_type": r["task_type"], "orders": 0, "minutes": 0,
                                "usedSum": 0.0, "timed": 0})
        t["orders"] += 1
        t["minutes"] += mins
        if used is not None:
            t["usedSum"] += used
            t["timed"] += 1
        d = by_day.setdefault(r["date"], {"date": r["date"], "orders": 0, "done": 0})
        d["orders"] += 1
        if st == "done":
            d["done"] += 1
        aid = r["assignee_id"]
        if aid:
            people.add(aid)
            p = by_person.setdefault(aid, {"open_id": aid, "name": amap.get(aid) or r["assignee_name"] or aid,
                                           "orders": 0, "done": 0, "minutes": 0, "types": {}, "lastDate": ""})
            p["orders"] += 1
            p["minutes"] += mins
            if st == "done":
                p["done"] += 1
            p["types"][r["task_type"]] = p["types"].get(r["task_type"], 0) + 1
            if r["date"] > p["lastDate"]:
                p["lastDate"] = r["date"]
    summary["people"] = len(people)
    summary["avgUsed"] = round(summary["usedSum"] / summary["timed"], 1) if summary["timed"] else None
    bp = []
    for p in by_person.values():
        top = max(p["types"].items(), key=lambda kv: kv[1])[0] if p["types"] else ""
        bp.append({"open_id": p["open_id"], "name": p["name"], "orders": p["orders"],
                   "done": p["done"], "minutes": p["minutes"], "topType": top, "lastDate": p["lastDate"]})
    bp.sort(key=lambda x: x["orders"], reverse=True)
    bt = []
    for t in by_type.values():
        bt.append({"task_type": t["task_type"], "orders": t["orders"], "minutes": t["minutes"],
                   "timed": t["timed"],
                   "avgUsed": round(t["usedSum"] / t["timed"], 1) if t["timed"] else None,
                   "plannedAvg": round(t["minutes"] / t["orders"]) if t["orders"] else None})
    bt.sort(key=lambda x: x["orders"], reverse=True)
    return 200, {"range": rng, "from": frm, "to": to, "summary": summary, "byPerson": bp,
                 "byType": bt,
                 "byDay": sorted(by_day.values(), key=lambda x: x["date"])}


def do_tech_status(user, body):
    if user["role"] == "team":
        return 403, "团队账号不能报状态"
    target = body.get("user_id") or user["open_id"]
    status = body.get("status", "")
    if status not in TECH_STATUS:
        return 400, "非法状态"
    task = body.get("task", "") if status in ("busy", "check") else ""
    conn = db()
    target_user = conn.execute(
        "SELECT name,role,grp FROM users WHERE open_id=?", (target,)
    ).fetchone()
    if not target_user or not (target_user["grp"] or "") or target_user["role"] == "team":
        conn.close()
        return 400, "目标不是可派工成员"
    if target != user["open_id"]:
        allowed = user["role"] == "admin" or (user.get("lead") and user.get("grp") == target_user["grp"])
        if not allowed:
            conn.close()
            return 403, "只能改自己的状态(其他人由本组组长或管理员改)"
    active = conn.execute(
        "SELECT COUNT(*) c FROM orders WHERE assignee_id=? AND status='working'", (target,)
    ).fetchone()["c"]
    if active and status in ("free", "rest", "off"):
        conn.close()
        return 409, "该人员还有进行中工单,完成工单后才能切换到此状态"
    conn.execute("UPDATE users SET status=?,task=? WHERE open_id=?", (status, task, target))
    conn.commit()
    conn.close()
    audit_log(user, "改状态", f"{target_user['name']} → {status} {task}")
    return 200, None


def do_order(user, body):
    team_name = (body.get("team") or "").strip()
    task_type = (body.get("task") or "").strip()
    grp = (body.get("group") or "").strip()
    conn = db()
    if not conn.execute("SELECT 1 FROM groups WHERE name=?", (grp,)).fetchone():
        conn.close()
        return 400, "请选择处理该单的组"
    # 组长只能给本组下单
    scope = user_scope(user)
    if scope is not None and grp != scope:
        conn.close()
        return 403, "只能给本组下单"
    tm = conn.execute("SELECT * FROM teams WHERE name=?", (team_name,)).fetchone()
    if not tm:
        conn.close()
        return 400, "团队不存在"
    if not task_type:
        conn.close()
        return 400, "请填写任务"
    sl = conn.execute("SELECT * FROM sla WHERE task_type=? AND grp=?", (task_type, grp)).fetchone()
    try:
        cm = int(body.get("minutes")) if body.get("minutes") not in (None, "") else None
    except (TypeError, ValueError):
        cm = None
    sl_minutes = sl["minutes"] if sl else (cm if cm and cm > 0 else 30)  # 自定义任务默认30分钟
    sl_buffer = sl["buffer"] if sl else 0
    when = (body.get("when") or "auto").strip()
    urgent_flag = 0
    book_minutes = None
    if when == "now":
        arrive = datetime.now().strftime("%H:%M")
        order_date = today()
        base = arrive
        urgent_flag = 1
    elif when == "book":
        order_date = valid_future_date(body.get("date"))
        arrive = first_schedule_time(body.get("at"))
        if not order_date:
            conn.close()
            return 400, "预约日期无效(不能早于今天)"
        if not arrive:
            conn.close()
            return 400, "请填写有效的到岗时间,如 18:30"
        end_t = first_schedule_time(body.get("end") or "")
        if end_t:
            dur = ((hm_to_min(end_t) - hm_to_min(arrive)) % 1440 + 1440) % 1440
            if dur < 10:
                conn.close()
                return 400, "预约时长至少 10 分钟(结束需晚于开始)"
            try:
                bmax = max(60, min(1440, int(setting("book_max_min", "720") or 720)))
            except (TypeError, ValueError):
                bmax = 720
            if dur > bmax:
                conn.close()
                return 400, f"预约时长不能超过 {bmax/60:g} 小时"
            book_minutes = dur
        else:
            book_minutes = None
        base = arrive
    else:  # auto:按团队彩排/开播倒推(今天)
        base = first_schedule_time(tm["rehearsal"]) or first_schedule_time(tm["onair"])
        if not base:
            conn.close()
            return 400, "该团队没有排期时间,请改用「立即」或「预约」下单"
        arrive = minus(base, sl_minutes + sl_buffer)
        order_date = today()
    if when == "book" and user["role"] == "team":
        if setting("rule_book_lock_sla", "1") == "1":
            book_minutes = None  # 规则:团队预约锁任务标准工时(防包段囤人)
        try:
            cap = int(setting("book_team_cap", "2") or 2)
        except (TypeError, ValueError):
            cap = 2
        if cap > 0:
            held = conn.execute(
                "SELECT COUNT(*) c FROM orders WHERE team=? AND COALESCE(booked,0)=1 "
                "AND status IN ('wait','working') AND COALESCE(started_at,'')=''",
                (team_name,)).fetchone()["c"]
            if held >= cap:
                conn.close()
                return 400, f"该团已有 {held} 个未开始的预约（上限 {cap} 个）——先用掉或取消，别囤时段"
        try:
            daycap = int(setting("book_team_day_min", "240") or 240)
        except (TypeError, ValueError):
            daycap = 240
        if daycap > 0:  # 规则:每团每个排班日预约总时长封顶(0=不限)
            used_day = conn.execute(
                "SELECT COALESCE(SUM(minutes),0) m FROM orders WHERE team=? AND date=? "
                "AND COALESCE(booked,0)=1 AND status!='cancelled'",
                (team_name, order_date)).fetchone()["m"]
            eff = book_minutes or sl_minutes
            if used_day + eff > daycap:
                conn.close()
                return 400, (f"该团 {order_date} 已预约 {used_day} 分钟,再约 {eff} 分钟将超过"
                             f"每日上限 {daycap} 分钟（约 {daycap/60:g} 小时）——把时段留给别的团吧")
    if book_minutes:
        sl_minutes = book_minutes  # 预约时间段:时长=结束-开始(管理员/组长)
        sl_buffer = 0
    assignee = (body.get("assignee") or "").strip()
    if when == "book" and assignee:
        _au = conn.execute("SELECT name,shift FROM users WHERE open_id=?", (assignee,)).fetchone()
        _sh = (conn.execute("SELECT start,end FROM shifts WHERE name=?", (_au["shift"],)).fetchone()
               if _au and _au["shift"] else None)
        if _sh and _sh["start"] and _sh["end"]:
            _nzm = lambda m: m + 1440 if m < 300 else m
            _s0 = _nzm(hm_to_min(_sh["start"]))
            _s1 = _nzm(hm_to_min(_sh["end"]))
            _a0 = _nzm(hm_to_min(arrive))
            if _s1 > _s0 and (_a0 < _s0 or _a0 + sl_minutes > _s1):
                _nm = _au["name"] if _au else "该成员"
                conn.close()
                return 400, f"预约超出 {_nm} 的班次（{_sh['start']}–{_sh['end']}），最晚只能排到 {_sh['end']} 下班"
    note = (body.get("note") or "").strip()[:2000]
    if grp == "设备组" and not note and setting("rule_dev_note", "1") == "1":
        conn.close()
        return 400, "设备组下单必须填写问题描述（设备出了什么问题）"
    def _chk_media(mv, kv):
        mv = (mv or "").strip()
        kv = (kv or "").strip()
        if not mv:
            return "", "", None
        fn = os.path.basename(mv)
        if not mv.startswith("/media/") or not (MEDIA_DIR / fn).is_file():
            return None, None, "附件无效,请重新上传"
        if kv not in ("audio", "video"):
            kv = "video" if fn.rsplit(".", 1)[-1] in ("mp4", "webm", "mov", "mkv") else "audio"
        return mv, kv, None
    media, media_kind, _me = _chk_media(body.get("media"), body.get("media_kind"))
    if _me:
        conn.close()
        return 400, _me
    media2, media2_kind, _me = _chk_media(body.get("media2"), body.get("media2_kind"))
    if _me:
        conn.close()
        return 400, _me
    if (grp == "灯光组" and "编歌" in task_type and not media and not media2
            and setting("rule_biange_media", "1") == "1"):
        conn.close()
        return 400, "编歌必须上传歌曲音频或视频（最好都传），灯光师要跟着歌编灯"
    cur = conn.execute("""INSERT INTO orders(date,team,room,task_type,minutes,base_time,arrive,grp,
                    status,creator_id,creator_name,note,media,media_kind,media2,media2_kind,created_at,urgent,booked)
                    VALUES(?,?,?,?,?,?,?,?,'wait',?,?,?,?,?,?,?,?,?,?)""",
                 (order_date, tm["name"], tm["room"], task_type, sl_minutes, base, arrive, grp,
                  user["open_id"], user["name"], note, media, media_kind, media2, media2_kind, now_iso(), urgent_flag,
                  1 if when == "book" else 0))
    oid = cur.lastrowid
    assigned = None
    auto_flag = False
    can_assign = user["role"] == "admin" or (user.get("lead") and user.get("grp") == grp)
    if assignee and can_assign:
        tech = conn.execute("SELECT * FROM users WHERE open_id=? AND grp=? AND role!='team'",
                            (assignee, grp)).fetchone()
        if not tech:
            conn.close()
            return 400, f"只能指派给{grp}的成员"
        blk = assign_block(conn, tech, order_date, arrive)
        if blk:
            conn.close()
            return 409, blk
        conn.execute("UPDATE orders SET assignee_id=?,assignee_name=?,status='working',accepted_at=? WHERE id=?",
                     (tech["open_id"], dn(tech), now_iso(), oid))
        conn.execute("UPDATE users SET status='busy',task=? WHERE open_id=?",
                     (f"{tm['name']} · {tm['room']}", tech["open_id"]))
        assigned = dn(tech)
    if not assigned:  # 自动派单:该组开了自动 且 没手动指派 → 自动挑人
        grow = conn.execute("SELECT COALESCE(auto,0) auto FROM groups WHERE name=?", (grp,)).fetchone()
        if grow and grow["auto"]:
            tech = pick_auto_assignee(conn, grp, order_date, arrive)
            if tech:
                conn.execute("UPDATE orders SET assignee_id=?,assignee_name=?,status='working',accepted_at=? WHERE id=?",
                             (tech["open_id"], dn(tech), now_iso(), oid))
                conn.execute("UPDATE users SET status='busy',task=? WHERE open_id=?",
                             (f"{tm['name']} · {tm['room']}", tech["open_id"]))
                assigned = dn(tech)
                assignee = tech["open_id"]
                auto_flag = True
    conn.commit()
    conn.close()
    extra = (" · 含语音/视频说明" if (media or media2) else "") + (f" · 备注:{note[:30]}" if note else "")
    urg = "【紧急·尽快到岗】" if urgent_flag else ""
    day_txt = "" if order_date == today() else f"{order_date} "
    when_txt = f"{urg}{day_txt}{arrive}"
    if assigned:
        audit_log(user, "自动派单" if auto_flag else "下单并派单",
                  f"[{grp}] {team_name} {task_type} → {assigned} 到岗{when_txt}{extra}")
        notify(assignee, f"{urg}{'自动派单给你' if auto_flag else '新派单给你'}:{grp} · {team_name} · {tm['room']}间 · {task_type} · 建议到岗 {day_txt}{arrive}{extra}\n{BASE_URL}", urgent=True)
        create_task_reminder(oid, assignee,
                             f"{urg}{grp}派单 · {team_name} · {tm['room']}间 · {task_type}",
                             f"建议到岗 {day_txt}{arrive}{extra}", order_date, arrive)
    else:
        audit_log(user, "下单", f"[{grp}] {team_name} {task_type} 到岗{when_txt}{extra}")
        notify_free_staff(f"{urg}新工单:{grp} · {team_name} · {tm['room']}间 · {task_type} · 建议到岗 {day_txt}{arrive}{extra}\n"
                          f"{grp}空闲成员可进 App 抢单:{BASE_URL}", grp, arrive, urgent=True)
    return 200, None


def do_assign(user, body):
    oid = record_id(body.get("order_id"))
    if not oid:
        return 400, "非法工单编号"
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    order = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not order:
        conn.close()
        return 400, "工单不存在"
    if order["status"] != "wait":
        conn.close()
        return 409, "该单已被接走"
    grp = order["grp"] or FIRST_GROUP
    by_manager = bool(body.get("user_id")) and (
        user["role"] == "admin" or (user.get("lead") and user.get("grp") == grp))
    if by_manager:
        tgt = conn.execute("SELECT * FROM users WHERE open_id=? AND grp=? AND role!='team'",
                           (body["user_id"], grp)).fetchone()
        if not tgt:
            conn.close()
            return 400, f"只能指派给{grp}的成员"
    elif user.get("grp") == grp and user["role"] != "team":
        tgt = conn.execute("SELECT * FROM users WHERE open_id=?", (user["open_id"],)).fetchone()
    else:
        conn.close()
        return 403, f"只有{grp}的成员或管理员/组长能处理这单"
    blk = assign_block(conn, tgt, order["date"], order["arrive"])
    if blk:
        conn.close()
        return 409, blk
    if not by_manager and tgt["status"] != "free":
        conn.close()
        return 409, "只有空闲状态才能接单"
    changed = conn.execute(
        """UPDATE orders SET assignee_id=?,assignee_name=?,status='working',accepted_at=?
           WHERE id=? AND status='wait'""",
        (tgt["open_id"], dn(tgt), now_iso(), oid),
    ).rowcount
    if changed != 1:
        conn.close()
        return 409, "该单已被接走"
    conn.execute("UPDATE users SET status='busy',task=? WHERE open_id=?",
                 (f"{order['team']} · {order['room']}", tgt["open_id"]))
    conn.commit()
    conn.close()
    verb = "接单" if tgt["open_id"] == user["open_id"] else f"派单给 {dn(tgt)}"
    audit_log(user, verb, f"#{oid} {order['team']} {order['task_type']}")
    notify(tgt["open_id"], f"{'你已接单' if tgt['open_id'] == user['open_id'] else '新派单给你'}:"
                           f"{order['team']} · {order['room']}间 · {order['task_type']} · "
                           f"建议到岗 {order['arrive']}\n{BASE_URL}",
           urgent=(tgt["open_id"] != user["open_id"]))
    create_task_reminder(oid, tgt["open_id"],
                         f"灯光派单 · {order['team']} · {order['room']}间 · {order['task_type']}",
                         f"建议到岗 {order['arrive']}", order["date"], order["arrive"])
    return 200, None


def do_start(user, body):
    """灯光师开始任务:开始计时(写 started_at)。像外卖骑手"取到餐、开始配送"。"""
    oid = record_id(body.get("order_id"))
    if not oid:
        return 400, "非法工单编号"
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    order = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not order:
        conn.close()
        return 400, "工单不存在"
    if order["status"] != "working":
        conn.close()
        return 409, "只有已接单的工单可以开始"
    if order["assignee_id"] != user["open_id"]:
        conn.close()
        return 403, "只有负责这单的人能点「开始」（像骑手取餐后自己开始）"
    if order["started_at"]:
        conn.close()
        return 409, "任务已经开始计时"
    conn.execute("UPDATE orders SET started_at=? WHERE id=? AND status='working'", (now_iso(), oid))
    conn.commit()
    conn.close()
    audit_log(user, "开始任务", f"#{oid} {order['team']} {order['task_type']}")
    return 200, None


def do_complete(user, body):
    oid = record_id(body.get("order_id"))
    if not oid:
        return 400, "非法工单编号"
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    order = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not order:
        conn.close()
        return 400, "工单不存在"
    if order["status"] != "working":
        conn.close()
        return 409, "只有进行中的工单可以完成"
    if user["role"] != "admin" and order["assignee_id"] != user["open_id"]:
        conn.close()
        return 403, "只能完成自己的单"
    done_note = (body.get("note") or "").strip()
    if not done_note and setting("rule_done_note", "1") == "1":
        conn.close()
        return 400, "完成前请填写完成说明（做了什么 / 处理结果）"
    stamp = now_iso()
    conn.execute("UPDATE orders SET status='done',done_at=?,done_note=? WHERE id=? AND status='working'",
                 (stamp, done_note[:300], oid))
    if order["assignee_id"]:
        remaining = conn.execute(
            """SELECT team,room FROM orders
               WHERE assignee_id=? AND status='working' ORDER BY arrive,id LIMIT 1""",
            (order["assignee_id"],),
        ).fetchone()
        if remaining:
            conn.execute(
                "UPDATE users SET status='busy',task=? WHERE open_id=?",
                (f"{remaining['team']} · {remaining['room']}", order["assignee_id"]),
            )
        else:
            conn.execute(
                "UPDATE users SET status='free',task='' WHERE open_id=?", (order["assignee_id"],)
            )
    conn.commit()
    conn.close()
    used = used_minutes(order["started_at"], stamp)
    used_txt = f" · 用时{used:g}分" if used is not None else ""
    audit_log(user, "完成工单", f"#{oid} {order['team']} {order['task_type']}{used_txt}")
    delete_task_reminder(order["feishu_task_guid"] if "feishu_task_guid" in order.keys() else "")
    return 200, None


def _release_worker(conn, open_id):
    """把某人从当前任务释放:还有其他进行中单则保持忙碌(切到那单),否则置空闲。"""
    if not open_id:
        return
    nxt = conn.execute(
        "SELECT team,room FROM orders WHERE assignee_id=? AND status='working' ORDER BY arrive,id LIMIT 1",
        (open_id,)).fetchone()
    if nxt:
        conn.execute("UPDATE users SET status='busy',task=? WHERE open_id=?",
                     (f"{nxt['team']} · {nxt['room']}", open_id))
    else:
        conn.execute("UPDATE users SET status='free',task='' WHERE open_id=?", (open_id,))


def _can_manage_order(user, grp):
    return user["role"] == "admin" or (user.get("lead") and user.get("grp") == grp)


def do_reassign(user, body):
    """改派:把工单换给本组另一个人;user_id 为空=撤回指派回到待派工。"""
    oid = record_id(body.get("order_id"))
    if not oid:
        return 400, "非法工单编号"
    new_uid = (body.get("user_id") or "").strip()
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    order = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not order:
        conn.close()
        return 400, "工单不存在"
    grp = order["grp"] or FIRST_GROUP
    if not _can_manage_order(user, grp):
        conn.close()
        return 403, "只有管理员或本组组长能改派"
    if order["status"] == "done":
        conn.close()
        return 409, "已完成的工单不能改派"
    old = order["assignee_id"]
    guid = order["feishu_task_guid"] if "feishu_task_guid" in order.keys() else ""
    if not new_uid:
        conn.execute("UPDATE orders SET assignee_id='',assignee_name='',status='wait',"
                     "accepted_at='',started_at='' WHERE id=?", (oid,))
        _release_worker(conn, old)
        conn.commit()
        conn.close()
        if guid:
            delete_task_reminder(guid)
        audit_log(user, "撤回指派", f"#{oid} {order['team']} {order['task_type']}")
        return 200, None
    tgt = conn.execute("SELECT * FROM users WHERE open_id=? AND grp=? AND role!='team'",
                       (new_uid, grp)).fetchone()
    if not tgt:
        conn.close()
        return 400, f"只能指派给{grp}的成员"
    if tgt["open_id"] == old:
        conn.close()
        return 400, "已是该成员负责"
    blk = assign_block(conn, tgt, order["date"], order["arrive"])
    if blk:
        conn.close()
        return 409, blk
    conn.execute("UPDATE orders SET assignee_id=?,assignee_name=?,status='working',"
                 "accepted_at=?,started_at='' WHERE id=?",
                 (tgt["open_id"], dn(tgt), now_iso(), oid))
    conn.execute("UPDATE users SET status='busy',task=? WHERE open_id=?",
                 (f"{order['team']} · {order['room']}", tgt["open_id"]))
    if old and old != tgt["open_id"]:
        _release_worker(conn, old)
    conn.commit()
    conn.close()
    audit_log(user, "改派", f"#{oid} {order['team']} {order['task_type']} → {dn(tgt)}")
    notify(tgt["open_id"], f"改派给你:{order['team']} · {order['room']}间 · {order['task_type']} · "
                           f"建议到岗 {order['arrive']}\n{BASE_URL}", urgent=True)
    return 200, None


def do_order_cancel(user, body):
    """取消工单:删除并释放负责人。"""
    oid = record_id(body.get("order_id"))
    if not oid:
        return 400, "非法工单编号"
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    order = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not order:
        conn.close()
        return 400, "工单不存在"
    grp = order["grp"] or FIRST_GROUP
    if not _can_manage_order(user, grp):
        conn.close()
        return 403, "只有管理员或本组组长能取消工单"
    old = order["assignee_id"]
    guid = order["feishu_task_guid"] if "feishu_task_guid" in order.keys() else ""
    conn.execute("DELETE FROM orders WHERE id=?", (oid,))
    _release_worker(conn, old)
    conn.commit()
    conn.close()
    if guid:
        delete_task_reminder(guid)
    audit_log(user, "取消工单", f"#{oid} {order['team']} {order['task_type']}")
    if old:
        notify(old, f"工单已取消:{order['team']} · {order['room']}间 · {order['task_type']}")
    return 200, None


def do_order_edit(user, body):
    """编辑待派工的工单:改任务类型/到岗方式/紧急。进行中的请用改派或取消。"""
    oid = record_id(body.get("order_id"))
    if not oid:
        return 400, "非法工单编号"
    conn = db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not order:
        conn.close()
        return 400, "工单不存在"
    grp = order["grp"] or FIRST_GROUP
    if not _can_manage_order(user, grp):
        conn.close()
        return 403, "只有管理员或本组组长能编辑工单"
    if order["status"] != "wait":
        conn.close()
        return 409, "只有待派工的工单可编辑;进行中的请用改派或取消"
    task_type = (body.get("task") or order["task_type"]).strip()
    if not task_type:
        conn.close()
        return 400, "请填写任务"
    sl = conn.execute("SELECT * FROM sla WHERE task_type=? AND grp=?", (task_type, grp)).fetchone()
    try:
        cm = int(body.get("minutes")) if body.get("minutes") not in (None, "") else None
    except (TypeError, ValueError):
        cm = None
    sl_minutes = sl["minutes"] if sl else (cm if cm and cm > 0 else (order["minutes"] or 30))
    sl_buffer = sl["buffer"] if sl else 0
    tm = conn.execute("SELECT * FROM teams WHERE name=?", (order["team"],)).fetchone()
    when = (body.get("when") or "keep").strip()
    urgent_flag = order["urgent"]
    if "urgent" in body:
        urgent_flag = 1 if str(body.get("urgent")) in ("1", "true", "on", "yes") else 0
    order_date, arrive, base = order["date"], order["arrive"], order["base_time"]
    if when == "now":
        arrive = datetime.now().strftime("%H:%M")
        order_date = today()
        base = arrive
        urgent_flag = 1
    elif when == "book":
        order_date = valid_future_date(body.get("date"))
        arrive = first_schedule_time(body.get("at"))
        if not order_date:
            conn.close()
            return 400, "预约日期无效(不能早于今天)"
        if not arrive:
            conn.close()
            return 400, "请填写有效的到岗时间"
        base = arrive
    elif when == "auto":
        if not tm:
            conn.close()
            return 400, "该团队不存在"
        base = first_schedule_time(tm["rehearsal"]) or first_schedule_time(tm["onair"])
        if not base:
            conn.close()
            return 400, "该团队没有排期时间"
        arrive = minus(base, sl_minutes + sl_buffer)
        order_date = today()
    conn.execute("UPDATE orders SET task_type=?,minutes=?,base_time=?,arrive=?,date=?,urgent=? WHERE id=?",
                 (task_type, sl_minutes, base, arrive, order_date, urgent_flag, oid))
    conn.commit()
    conn.close()
    audit_log(user, "编辑工单", f"#{oid} {order['team']} → {task_type} 到岗{order_date} {arrive}")
    return 200, None


SAVE_WHITELIST = {
    "teams": {"name", "room", "rehearsal", "onair"},
    "sla": {"task_type", "minutes", "buffer", "note", "grp"},
    "shifts": {"name", "start", "end", "sort"},
    "users": {"role", "grp", "lead", "shift", "status", "work_hours", "intern", "leave_until", "alias", "approved"},
    "groups": {"name", "color", "sort", "auto"},
}
LEAD_USER_FIELDS = {"shift", "status", "intern", "leave_until"}  # 组长只能改本组成员这几项


def do_save(user, body):
    entity = body.get("entity")
    rec = body.get("record") or {}
    is_admin = user["role"] == "admin"
    is_lead = bool(user.get("lead"))
    # 管理员改一切;组长只能改本组人员的班次/状态/实习/请假
    if not is_admin and not (is_lead and entity == "users"):
        return 403, "仅管理员或组长可编辑"
    if entity not in SAVE_WHITELIST:
        return 400, "非法对象"
    allowed = SAVE_WHITELIST[entity] if is_admin else LEAD_USER_FIELDS
    fields = {k: v for k, v in rec.items() if k in allowed}
    if not fields:
        return 400, "没有可保存的字段"
    conn = db()
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
        if oid == user["open_id"] and ({"role", "lead", "intern", "shift", "status", "grp"} & set(fields.keys())):
            conn.close()
            return 403, "不能修改自己的身份 / 组别 / 班次，请让管理员或其他组长操作"
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
        if "leave_until" in fields:
            lv = str(fields["leave_until"] or "").strip()
            if lv and not valid_future_date(lv) and lv != today():
                try:
                    datetime.strptime(lv, "%Y-%m-%d")
                except ValueError:
                    conn.close()
                    return 400, "请假日期格式应为 YYYY-MM-DD"
            fields["leave_until"] = lv
        if fields.get("shift"):
            valid_shifts = {r["name"] for r in conn.execute("SELECT name FROM shifts")}
            if fields["shift"] not in valid_shifts:
                conn.close()
                return 400, "非法班次"
        if fields.get("status") and fields["status"] not in TECH_STATUS:
            conn.close()
            return 400, "非法状态"
        active = conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE assignee_id=? AND status='working'", (oid,)
        ).fetchone()["c"]
        if active and fields.get("status") in ("free", "rest", "off"):
            conn.close()
            return 409, "该人员还有进行中工单,完成工单后才能切换到此状态"
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
        sets = ",".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE users SET {sets} WHERE open_id=?", (*fields.values(), oid))
        target_desc = target_user["name"]
    else:
        rid = record_id(rec.get("id"))
        grp_old_name = None
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
        if entity == "shifts":
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
    conn.commit()
    conn.close()
    audit_log(user, f"编辑{entity}", f"{target_desc} {json.dumps(fields, ensure_ascii=False)[:200]}")
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
    task_guid = ""
    if entity == "orders":
        order = conn.execute("SELECT status,feishu_task_guid FROM orders WHERE id=?", (rid,)).fetchone()
        if order and order["status"] == "working":
            conn.close()
            return 409, "进行中工单不能删除,请先完成"
        if order:
            task_guid = order["feishu_task_guid"]
    if entity == "groups":
        grow = conn.execute("SELECT name FROM groups WHERE id=?", (rid,)).fetchone()
        if grow:
            gname = grow["name"]
            n_u = conn.execute("SELECT COUNT(*) c FROM users WHERE grp=?", (gname,)).fetchone()["c"]
            n_o = conn.execute("SELECT COUNT(*) c FROM orders WHERE grp=? AND status!='done'",
                               (gname,)).fetchone()["c"]
            if n_u or n_o:
                conn.close()
                return 409, f"该组还有 {n_u} 名成员 / {n_o} 张未完成工单,请先转移再删"
            if conn.execute("SELECT COUNT(*) c FROM groups").fetchone()["c"] <= 1:
                conn.close()
                return 400, "至少保留一个组"
    if conn.execute(f"DELETE FROM {entity} WHERE id=?", (rid,)).rowcount != 1:
        conn.close()
        return 400, "记录不存在"
    conn.commit()
    conn.close()
    if task_guid:
        delete_task_reminder(task_guid)
    audit_log(user, f"删除{entity}", f"#{rid}")
    return 200, None


SETTING_WHITELIST = {"reminder_on", "reminder_minutes", "urgent_on",
                     "rule_book_lock_sla", "book_team_cap", "book_grace_min", "book_max_min",
                     "book_team_day_min", "rule_dev_note", "rule_biange_media", "rule_done_note",
                     "nav_team", "nav_staff", "gate_mode", "allow_dept_ids", "overrun_remind_min", "overrun_close_min"}


def do_set_setting(user, body):
    if user["role"] != "admin":
        return 403, "仅管理员可修改设置"
    k = body.get("key")
    if k not in SETTING_WHITELIST:
        return 400, "不支持的设置项"
    v = str(body.get("value", "")).strip()
    if k in ("reminder_on", "urgent_on", "rule_book_lock_sla", "rule_dev_note",
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
    elif k == "book_max_min":
        try:
            v = str(max(60, min(1440, int(v or 720))))
        except ValueError:
            return 400, "分钟需为数字"
    elif k == "book_team_day_min":
        try:
            v = str(max(0, min(1440, int(v or 240))))  # 0=不限
        except ValueError:
            return 400, "分钟需为数字"
    elif k in ("overrun_remind_min", "overrun_close_min"):
        try:
            v = str(max(0, min(1440, int(v or 0))))
        except ValueError:
            return 400, "分钟需为数字"
    elif k == "gate_mode":
        if v not in ("open", "dept", "manual"):
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
    conn.execute("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (k, v))
    conn.commit()
    conn.close()
    audit_log(user, "改设置", f"{k}={v}")
    return 200, None


def do_purge(user, body):
    """数据维护(仅管理员):清空工单或审计,用于清掉测试期间留下的数据。人员/团队/班次/规则全部保留。"""
    if user["role"] != "admin":
        return 403, "仅管理员可清理数据"
    what = (body.get("what") or "").strip()
    if (body.get("confirm") or "") != "清空":
        return 400, "确认口令不对(需输入「清空」两个字)"
    conn = db()
    if what == "orders":
        n = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
        guids = [r["feishu_task_guid"] for r in conn.execute(
            "SELECT feishu_task_guid FROM orders WHERE COALESCE(feishu_task_guid,'')!=''")]
        conn.execute("DELETE FROM orders")
        conn.execute("UPDATE users SET status=CASE WHEN status IN('busy','check') THEN 'free' ELSE status END, task='' WHERE role!='team'")
        conn.commit()
        conn.close()
        for g in guids:
            delete_task_reminder(g)
        audit_log(user, "清空工单数据", f"删除 {n} 张工单(人员/团队/配置保留)")
        return 200, None
    if what == "audit":
        n = conn.execute("SELECT COUNT(*) c FROM audit").fetchone()["c"]
        conn.execute("DELETE FROM audit")
        conn.commit()
        conn.close()
        audit_log(user, "清空审计日志", f"删除 {n} 条历史记录")
        return 200, None
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
    conn.commit()
    conn.close()
    audit_log(user, "认领管理员", "引导码通过")
    return 200, None


# ───────────────────────── HTTP ─────────────────────────
RATE_LOCK = threading.Lock()
RATE_HITS = {}


def rate_limited(ip, path):
    """简易限流:每 IP 每分钟普通请求 300 次 / 登录相关 30 次,挡住脚本乱打与暴力尝试。"""
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
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, url, cookies=None):
        self.send_response(302)
        self.send_header("Location", url)
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

    def _user(self):
        return session_user(self._cookie("sid"))

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
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not (ctype.startswith("audio/") or ctype.startswith("video/")):
            return self._send(400, {"ok": False, "msg": "只支持音频或视频文件"})
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            return self._send(400, {"ok": False, "msg": "空文件"})
        if n > MEDIA_MAX_BYTES:
            return self._send(413, {"ok": False, "msg": f"文件过大(上限 {MEDIA_MAX_BYTES//1024//1024}MB)"})
        ext = MEDIA_EXT.get(ctype) or ("mp4" if ctype.startswith("video/") else "m4a")
        fname = f"{secrets.token_urlsafe(16)}.{ext}"
        dest = MEDIA_DIR / fname
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
            return self._send(500, {"ok": False, "msg": "上传失败"})
        if remaining > 0:
            dest.unlink(missing_ok=True)
            return self._send(400, {"ok": False, "msg": "上传中断,请重试"})
        kind = "video" if ctype.startswith("video/") else "audio"
        audit_log(u, "上传附件", f"{kind} {fname}")
        return self._send(200, {"ok": True, "url": f"/media/{fname}", "kind": kind})

    def _serve_media(self, path):
        if not self._user():
            return self._send(403, {"ok": False, "msg": "未登录"})
        fname = os.path.basename(urllib.parse.unquote(path))
        dest = MEDIA_DIR / fname
        if not fname or not dest.is_file():
            return self._send(404, b"not found", "text/plain")
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
            if rate_limited(self.client_address[0], path):
                return self._send(429, {"ok": False, "msg": "请求太频繁,稍后再试"})
            if path in ("/", "/index.html"):
                return self._send(200, (ROOT / "index.html").read_bytes(), "text/html")
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
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
                return self._send(200, {"ok": True, "data": api_state(u)})
            if path == "/api/users":
                u = self._user()
                if not u:
                    return self._send(401, {"ok": False, "msg": "未登录"})
                code, result = api_users(u)
                if code != 200:
                    return self._send(code, {"ok": False, "msg": result})
                return self._send(200, {"ok": True, "data": result})
            if path == "/api/stats":
                u = self._user()
                if not u:
                    return self._send(401, {"ok": False, "msg": "未登录"})
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                code, result = api_stats(u, {k: v[0] for k, v in qs.items()})
                if code != 200:
                    return self._send(code, {"ok": False, "msg": result})
                return self._send(200, {"ok": True, "data": result})
            if path == "/api/schedule":
                u = self._user()
                if not u:
                    return self._send(401, {"ok": False, "msg": "未登录"})
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                code, result = api_schedule(u, {k: v[0] for k, v in qs.items()})
                if code != 200:
                    return self._send(code, {"ok": False, "msg": result})
                return self._send(200, {"ok": True, "data": result})
            if path == "/api/audit":
                u = self._user()
                if not u:
                    return self._send(401, {"ok": False, "msg": "未登录"})
                # 审计全员可见:谁下的单/谁派的/谁改的规则,全程留痕
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
                                      cookies=[f"oastate={state}; Path=/; HttpOnly; SameSite=Lax; Max-Age=600"])
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
                    f"sid={sid}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_DAYS*86400}",
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
            if rate_limited(self.client_address[0], path):
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
                            f"sid={sid}; Path=/; HttpOnly; SameSite=Lax; "
                            f"Max-Age={SESSION_DAYS*86400}"
                        )
                    },
                )

            u = self._user()
            if not u:
                return self._send(401, {"ok": False, "msg": "未登录"})
            # 准入门:待审核账号除了认领管理员,什么都不能做
            if setting("gate_mode", "open") != "open" and not u.get("approved", 1) and u.get("role") != "admin" and path != "/api/claim-admin":
                return self._send(403, {"ok": False, "msg": "账号待管理员审核,请联系管理员在「人员」页放行"})

            routes = {
                "/api/claim-admin": do_claim_admin,
                "/api/tech-status": do_tech_status,
                "/api/order": do_order,
                "/api/assign": do_assign,
                "/api/start": do_start,
                "/api/complete": do_complete,
                "/api/save": do_save,
                "/api/remove": do_remove,
                "/api/setting": do_set_setting,
                "/api/reassign": do_reassign,
                "/api/cancel-order": do_order_cancel,
                "/api/edit-order": do_order_edit,
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


def main():
    init_db()
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
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
