"""派工领域规则、数据库迁移与权限边界。"""

import json
import sqlite3
from datetime import datetime, timedelta


ORDER_STATUS = (
    "pending",
    "assigned",
    "in_progress",
    "completed",
    "cancelled",
    "abnormal",
)
ACTIVE_ORDER_STATUS = ("assigned", "in_progress")
VISIBLE_ORDER_STATUS = ("pending", "assigned", "in_progress")
TRANSFER_REASON = (
    "临时生病",
    "家庭急事",
    "交通受阻",
    "设备故障",
    "排班临时冲突",
    "能力不匹配",
    "派单错误",
    "其他紧急情况",
)
TRANSFER_STATUS = ("awaiting_lead", "open", "accepted", "rejected", "withdrawn", "expired")


def table_columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_columns(conn, table, columns):
    existing = table_columns(conn, table)
    for name, declaration in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _assert_unique(conn, table, fields, label):
    cols = ",".join(fields)
    duplicate = conn.execute(
        f"SELECT {cols},COUNT(*) count FROM {table} GROUP BY {cols} HAVING COUNT(*)>1 LIMIT 1"
    ).fetchone()
    if duplicate:
        values = " / ".join(str(duplicate[field]) for field in fields)
        raise RuntimeError(f"{label}存在重名数据：{values}，请先由管理员确认后再迁移")


def migrate(conn):
    """把历史库原地迁移为具备状态、权限和责任链的结构。"""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS order_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          order_id INTEGER NOT NULL,
          event_type TEXT NOT NULL,
          from_status TEXT DEFAULT '',
          to_status TEXT DEFAULT '',
          actor_id TEXT DEFAULT '',
          actor_name TEXT DEFAULT '',
          detail TEXT DEFAULT '',
          created_at TEXT NOT NULL,
          FOREIGN KEY(order_id) REFERENCES orders(id)
        );
        CREATE TABLE IF NOT EXISTS transfers(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          order_id INTEGER NOT NULL,
          requester_id TEXT NOT NULL,
          requester_name TEXT NOT NULL,
          target_id TEXT DEFAULT '',
          target_name TEXT DEFAULT '',
          reason_code TEXT NOT NULL,
          reason_detail TEXT DEFAULT '',
          status TEXT NOT NULL,
          requires_lead INTEGER NOT NULL DEFAULT 0,
          approved_by TEXT DEFAULT '',
          approved_at TEXT DEFAULT '',
          accepted_by TEXT DEFAULT '',
          accepted_at TEXT DEFAULT '',
          resolved_at TEXT DEFAULT '',
          created_at TEXT NOT NULL,
          FOREIGN KEY(order_id) REFERENCES orders(id),
          FOREIGN KEY(requester_id) REFERENCES users(open_id)
        );
        CREATE TABLE IF NOT EXISTS uploads(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          token TEXT NOT NULL UNIQUE,
          path TEXT NOT NULL UNIQUE,
          kind TEXT NOT NULL,
          owner_id TEXT NOT NULL,
          order_id INTEGER,
          status TEXT NOT NULL DEFAULT 'pending',
          size INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          claimed_at TEXT DEFAULT '',
          FOREIGN KEY(owner_id) REFERENCES users(open_id),
          FOREIGN KEY(order_id) REFERENCES orders(id)
        );
        CREATE TABLE IF NOT EXISTS worker_skills(
          open_id TEXT NOT NULL,
          grp TEXT NOT NULL,
          task_type TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY(open_id,grp,task_type),
          FOREIGN KEY(open_id) REFERENCES users(open_id)
        );
        CREATE TABLE IF NOT EXISTS work_sessions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          order_id INTEGER NOT NULL,
          worker_id TEXT NOT NULL,
          started_at TEXT NOT NULL,
          ended_at TEXT DEFAULT '',
          end_reason TEXT DEFAULT '',
          FOREIGN KEY(order_id) REFERENCES orders(id),
          FOREIGN KEY(worker_id) REFERENCES users(open_id)
        );
        CREATE TABLE IF NOT EXISTS notification_outbox(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT NOT NULL DEFAULT 'message',
          recipient_id TEXT DEFAULT '',
          payload TEXT NOT NULL,
          dedupe_key TEXT NOT NULL UNIQUE,
          status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0,
          available_at TEXT NOT NULL,
          sent_at TEXT DEFAULT '',
          last_error TEXT DEFAULT '',
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS system_health(
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_order_events_order ON order_events(order_id,id);
        CREATE INDEX IF NOT EXISTS idx_transfers_order_status ON transfers(order_id,status);
        CREATE INDEX IF NOT EXISTS idx_uploads_owner_status ON uploads(owner_id,status);
        CREATE INDEX IF NOT EXISTS idx_work_sessions_order ON work_sessions(order_id,id);
        CREATE INDEX IF NOT EXISTS idx_outbox_status_time ON notification_outbox(status,available_at,id);
        """
    )
    ensure_columns(
        conn,
        "users",
        {
            "team_id": "INTEGER",
            "last_assigned_at": "TEXT DEFAULT ''",
        },
    )
    ensure_columns(
        conn,
        "orders",
        {
            "team_id": "INTEGER",
            "scheduled_start": "TEXT DEFAULT ''",
            "scheduled_end": "TEXT DEFAULT ''",
            "cancelled_at": "TEXT DEFAULT ''",
            "cancelled_by": "TEXT DEFAULT ''",
            "cancel_reason": "TEXT DEFAULT ''",
            "abnormal_at": "TEXT DEFAULT ''",
            "abnormal_reason": "TEXT DEFAULT ''",
            "version": "INTEGER NOT NULL DEFAULT 1",
        },
    )
    ensure_columns(
        conn,
        "notification_outbox",
        {
            "claimed_at": "TEXT DEFAULT ''",
            "progress": "TEXT DEFAULT ''",
            "external_id": "TEXT DEFAULT ''",
        },
    )

    _assert_unique(conn, "groups", ("name",), "工作组")
    _assert_unique(conn, "sla", ("grp", "task_type"), "同组任务类型")
    _assert_unique(conn, "shifts", ("name",), "班次")
    _assert_unique(conn, "teams", ("name",), "团队")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_groups_name ON groups(name)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sla_group_task ON sla(grp,task_type)")

    # 历史状态只迁移一次，保留时间戳与负责人。
    conn.execute("UPDATE orders SET status='pending' WHERE status='wait'")
    conn.execute(
        "UPDATE orders SET status=CASE WHEN COALESCE(started_at,'')='' THEN 'assigned' ELSE 'in_progress' END "
        "WHERE status='working'"
    )
    conn.execute("UPDATE orders SET status='completed' WHERE status='done'")
    conn.execute(
        "UPDATE orders SET team_id=(SELECT id FROM teams WHERE teams.name=orders.team) "
        "WHERE team_id IS NULL"
    )
    for order in conn.execute(
        "SELECT id,date,arrive,minutes,scheduled_start,scheduled_end FROM orders "
        "WHERE COALESCE(scheduled_start,'')='' OR COALESCE(scheduled_end,'')=''"
    ).fetchall():
        start, end = schedule_bounds(order["date"], order["arrive"], order["minutes"] or 30)
        if start and end:
            conn.execute(
                "UPDATE orders SET scheduled_start=?,scheduled_end=? WHERE id=?",
                (format_dt(start), format_dt(end), order["id"]),
            )

    # 现有工作人员默认具备本组现有任务能力，管理员后续可以收窄。
    stamp = now_iso()
    conn.execute(
        """INSERT OR IGNORE INTO worker_skills(open_id,grp,task_type,created_at)
           SELECT users.open_id,sla.grp,sla.task_type,?
           FROM users JOIN sla ON sla.grp=users.grp
           WHERE users.role!='team' AND COALESCE(users.intern,0)=0""",
        (stamp,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO system_health(key,value,updated_at) VALUES('schema_version','2',?)",
        (stamp,),
    )


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(value):
    try:
        return datetime.strptime(str(value or ""), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def format_dt(value):
    return value.strftime("%Y-%m-%d %H:%M:%S")


def minute_of_day(value):
    try:
        hour, minute = map(int, str(value or "").split(":"))
    except (TypeError, ValueError):
        return None
    return hour * 60 + minute if 0 <= hour <= 23 and 0 <= minute <= 59 else None


def schedule_start(order_date, arrive):
    try:
        value = datetime.strptime(f"{order_date} {arrive}", "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None
    if value.hour < 5:
        value += timedelta(days=1)
    return value


def schedule_bounds(order_date, arrive, minutes):
    start = schedule_start(order_date, arrive)
    try:
        duration = int(minutes)
    except (TypeError, ValueError):
        return None, None
    if not start or duration < 1:
        return None, None
    return start, start + timedelta(minutes=duration)


def order_bounds(order):
    start = parse_dt(order["scheduled_start"] if "scheduled_start" in order.keys() else "")
    end = parse_dt(order["scheduled_end"] if "scheduled_end" in order.keys() else "")
    if start and end:
        return start, end
    return schedule_bounds(order["date"], order["arrive"], order["minutes"])


def shift_bounds(order_date, start_text, end_text):
    start_min, end_min = minute_of_day(start_text), minute_of_day(end_text)
    if start_min is None or end_min is None:
        return None, None
    base = datetime.strptime(order_date, "%Y-%m-%d")
    start = base + timedelta(minutes=start_min)
    if start_min < 5 * 60:
        start += timedelta(days=1)
    end = base + timedelta(minutes=end_min)
    if end_min < 5 * 60 or end <= start:
        end += timedelta(days=1)
    return start, end


def is_manager(user, grp=None):
    if user["role"] == "admin":
        return True
    return bool(user.get("lead")) and (grp is None or (user.get("grp") or "") == grp)


def bound_team_id(user):
    try:
        return int(user.get("team_id") or 0)
    except (TypeError, ValueError):
        return 0


def can_view_order(user, order):
    if user["role"] == "admin":
        return True
    if user["role"] == "team":
        team_id = bound_team_id(user)
        return bool(team_id) and int(order["team_id"] or 0) == team_id
    if user.get("lead"):
        return (order["grp"] or "") == (user.get("grp") or "")
    return (
        (order["grp"] or "") == (user.get("grp") or "")
        and (
            order["assignee_id"] == user["open_id"]
            or order["creator_id"] == user["open_id"]
            or order["status"] == "pending"
        )
    )


def can_manage_order(user, order):
    return is_manager(user, order["grp"])


def add_event(conn, order_id, event_type, user, from_status="", to_status="", detail=""):
    conn.execute(
        """INSERT INTO order_events(
             order_id,event_type,from_status,to_status,actor_id,actor_name,detail,created_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            order_id,
            event_type,
            from_status or "",
            to_status or "",
            user["open_id"] if user else "",
            user["name"] if user else "系统",
            str(detail or "")[:1000],
            now_iso(),
        ),
    )
    conn.execute(
        "INSERT INTO audit(ts,open_id,name,action,detail) VALUES(?,?,?,?,?)",
        (
            now_iso(),
            user["open_id"] if user else "",
            user["name"] if user else "系统",
            event_type,
            f"#{order_id} {str(detail or '')[:450]}",
        ),
    )


def assignment_block(conn, worker, order, ignore_order_id=None, require_available_now=False):
    grp = order["grp"] or ""
    if worker["role"] == "team" or not (worker["grp"] or ""):
        return "对方不是可派工成员"
    if (worker["grp"] or "") != grp:
        return f"只能指派给{grp}成员"
    if worker["intern"]:
        return f"{display_name(worker)} 是实习成员，不可独立派单"
    if not worker["approved"]:
        return f"{display_name(worker)} 尚未通过账号审核"
    if worker["leave_until"] and worker["leave_until"] >= order["date"]:
        return f"{display_name(worker)} 当天请假"

    shift = conn.execute("SELECT start,end FROM shifts WHERE name=?", (worker["shift"] or "",)).fetchone()
    if not shift:
        return f"{display_name(worker)} 的班次配置不存在"
    start, end = order_bounds(order)
    try:
        shift_start, shift_end = shift_bounds(order["date"], shift["start"], shift["end"])
    except ValueError:
        return "订单日期无效"
    if not start or not end:
        return "订单时间无效"
    if not shift_start or not shift_end:
        return f"{display_name(worker)} 的班次时间无效"
    if start < shift_start or end > shift_end:
        return f"订单 {start:%H:%M}–{end:%H:%M} 超出 {display_name(worker)} 的班次"

    skill_exists = conn.execute(
        "SELECT 1 FROM worker_skills WHERE open_id=? AND grp=? AND task_type=?",
        (worker["open_id"], grp, order["task_type"]),
    ).fetchone()
    is_known_task = conn.execute(
        "SELECT 1 FROM sla WHERE grp=? AND task_type=?", (grp, order["task_type"])
    ).fetchone()
    if is_known_task and not skill_exists:
        return f"{display_name(worker)} 尚未配置“{order['task_type']}”能力"

    args = [worker["open_id"], format_dt(end), format_dt(start)]
    sql = (
        "SELECT id,team,scheduled_start,scheduled_end FROM orders "
        "WHERE assignee_id=? AND status IN ('assigned','in_progress') "
        "AND scheduled_start<? AND scheduled_end>?"
    )
    if ignore_order_id:
        sql += " AND id!=?"
        args.append(ignore_order_id)
    conflict = conn.execute(sql + " LIMIT 1", args).fetchone()
    if conflict:
        return f"与工单 #{conflict['id']}（{conflict['team']}）时间冲突"

    if require_available_now and worker["status"] in ("rest", "off", "check", "busy"):
        labels = {"rest": "小休", "off": "下班", "check": "巡检", "busy": "忙碌"}
        return f"{display_name(worker)} 当前状态为{labels.get(worker['status'], worker['status'])}"
    return None


def display_name(user):
    try:
        alias = (user["alias"] or "").strip()
    except (KeyError, IndexError, TypeError):
        alias = ""
    return alias or user["name"]


def eligible_workers(conn, order, ignore_order_id=None):
    result = []
    rows = conn.execute(
        "SELECT * FROM users WHERE grp=? AND role!='team' AND COALESCE(approved,0)=1",
        (order["grp"],),
    ).fetchall()
    start, _ = order_bounds(order)
    require_now = bool(start and start <= datetime.now() + timedelta(minutes=15))
    for worker in rows:
        if not assignment_block(conn, worker, order, ignore_order_id, require_now):
            result.append(worker)
    return result


def fair_assignee(conn, order):
    candidates = eligible_workers(conn, order)
    if not candidates:
        return None
    try:
        window_end = (datetime.strptime(order["date"], "%Y-%m-%d") + timedelta(days=6)).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        window_end = order["date"]
    scored = []
    for worker in candidates:
        day_load = conn.execute(
            """SELECT COALESCE(SUM(minutes),0) minutes,COUNT(*) orders
               FROM orders WHERE assignee_id=? AND date=?
               AND status IN ('assigned','in_progress','completed')""",
            (worker["open_id"], order["date"]),
        ).fetchone()
        window_minutes = conn.execute(
            """SELECT COALESCE(SUM(minutes),0) minutes FROM orders
               WHERE assignee_id=? AND date>=? AND date<=?
               AND status IN ('assigned','in_progress','completed')""",
            (worker["open_id"], order["date"], window_end),
        ).fetchone()["minutes"]
        scored.append(
            (
                int(day_load["minutes"] or 0),
                int(window_minutes or 0),
                int(day_load["orders"] or 0),
                worker["last_assigned_at"] or "",
                worker["open_id"],
                worker,
            )
        )
    scored.sort(key=lambda item: item[:5])
    return scored[0][5]


def queue_notification(conn, recipient_id, text, dedupe_key, urgent=False):
    payload = json.dumps({"text": text, "urgent": bool(urgent)}, ensure_ascii=False)
    conn.execute(
        """INSERT OR IGNORE INTO notification_outbox(
             kind,recipient_id,payload,dedupe_key,status,attempts,available_at,created_at
           ) VALUES('message',?,?,?,'pending',0,?,?)""",
        (recipient_id, payload, dedupe_key, now_iso(), now_iso()),
    )


def queue_task_operation(conn, kind, recipient_id, payload, dedupe_key):
    conn.execute(
        """INSERT OR IGNORE INTO notification_outbox(
             kind,recipient_id,payload,dedupe_key,status,attempts,available_at,created_at
           ) VALUES(?,?,?,?,'pending',0,?,?)""",
        (kind, recipient_id or "", json.dumps(payload, ensure_ascii=False), dedupe_key, now_iso(), now_iso()),
    )


def claim_upload(conn, token, owner_id, order_id):
    if not token:
        return None
    row = conn.execute(
        "SELECT * FROM uploads WHERE token=? AND owner_id=? AND status='pending'",
        (token, owner_id),
    ).fetchone()
    if not row:
        return None
    conn.execute(
        "UPDATE uploads SET order_id=?,status='claimed',claimed_at=? WHERE id=?",
        (order_id, now_iso(), row["id"]),
    )
    return row


def active_transfer(conn, order_id):
    return conn.execute(
        "SELECT * FROM transfers WHERE order_id=? AND status IN ('awaiting_lead','open') ORDER BY id DESC LIMIT 1",
        (order_id,),
    ).fetchone()


def close_active_transfers(conn, order_id, status="expired"):
    if status not in ("rejected", "withdrawn", "expired"):
        raise ValueError("非法转包终止状态")
    stamp = now_iso()
    return conn.execute(
        """UPDATE transfers SET status=?,resolved_at=?
           WHERE order_id=? AND status IN ('awaiting_lead','open')""",
        (status, stamp, order_id),
    ).rowcount


def transfer_count_30d(conn, requester_id):
    since = format_dt(datetime.now() - timedelta(days=30))
    return conn.execute(
        "SELECT COUNT(*) count FROM transfers WHERE requester_id=? AND created_at>=?",
        (requester_id, since),
    ).fetchone()["count"]


def attach_transfer_state(conn, orders):
    if not orders:
        return orders
    ids = [order["id"] for order in orders]
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM transfers WHERE order_id IN ({marks}) ORDER BY id DESC", ids
    ).fetchall()
    latest = {}
    for row in rows:
        latest.setdefault(row["order_id"], dict(row))
    for order in orders:
        order["transfer"] = latest.get(order["id"])
    return orders
