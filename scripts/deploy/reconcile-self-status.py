#!/usr/bin/env python3
import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path


SELF_STATUS_ACTIONS = {"修改人员状态", "改状态"}


def parse_args():
    parser = argparse.ArgumentParser(description="纠正成员通过旧接口自行设置的休假状态")
    parser.add_argument("--db", default="/var/lib/wuyou-dispatch/dispatch.db")
    parser.add_argument("--backup-dir", default="/var/backups/wuyou-dispatch")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def display_name(user):
    return (user["alias"] or user["name"] or "").strip()


def audited_status_change(conn, user):
    target_name = display_name(user)
    rows = conn.execute(
        """SELECT id,ts,open_id,name,action,detail
           FROM audit
           WHERE action IN ('修改人员状态','改状态','编辑users')
           ORDER BY id DESC"""
    )
    for row in rows:
        detail = row["detail"] or ""
        if row["action"] in SELF_STATUS_ACTIONS:
            prefix = f"{target_name} → "
            if not detail.startswith(prefix):
                continue
            return row, detail[len(prefix):].split(" ", 1)[0]
        prefix = f"{user['name']} "
        if not detail.startswith(prefix):
            continue
        try:
            fields = json.loads(detail[len(prefix):])
        except json.JSONDecodeError:
            continue
        if "status" in fields:
            return row, fields["status"]
    return None, None


def invalid_self_rest_users(conn):
    users = conn.execute(
        """SELECT open_id,name,COALESCE(alias,'') alias,status
           FROM users
           WHERE status='rest' AND role!='team'
           ORDER BY name"""
    ).fetchall()
    candidates = []
    for user in users:
        audit, status = audited_status_change(conn, user)
        if (
            audit
            and status == "rest"
            and audit["action"] in SELF_STATUS_ACTIONS
            and audit["open_id"] == user["open_id"]
        ):
            active = conn.execute(
                "SELECT 1 FROM work_sessions WHERE worker_id=? AND ended_at='' LIMIT 1",
                (user["open_id"],),
            ).fetchone()
            candidates.append((user, audit, "busy" if active else "free"))
    return candidates


def create_backup(source, backup_dir):
    backup_root = Path(backup_dir)
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_path = backup_root / f"pre-self-status-reconcile-{datetime.now():%Y%m%d%H%M%S}.db"
    target = sqlite3.connect(backup_path)
    source.backup(target)
    target.close()
    return backup_path


def main():
    args = parse_args()
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"数据库完整性检查失败: {integrity}")

    candidates = invalid_self_rest_users(conn)
    print(f"INVALID_SELF_REST_COUNT={len(candidates)}")
    for user, audit, desired in candidates:
        print(
            "CANDIDATE "
            f"name={display_name(user)} desired={desired} audit_id={audit['id']} audit_time={audit['ts']}"
        )

    if not args.apply:
        conn.close()
        return

    backup_path = create_backup(conn, args.backup_dir)
    conn.execute("BEGIN IMMEDIATE")
    changed = 0
    for user, audit, desired in candidates:
        updated = conn.execute(
            "UPDATE users SET status=?,task='' WHERE open_id=? AND status='rest'",
            (desired, user["open_id"]),
        ).rowcount
        if not updated:
            continue
        conn.execute(
            "INSERT INTO audit(ts,open_id,name,action,detail) VALUES(?,?,?,?,?)",
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "system",
                "生产修复",
                "纠正非法自改状态",
                f"{display_name(user)} → {desired}（原状态 rest；依据审计 #{audit['id']}）",
            ),
        )
        changed += 1
    conn.commit()
    conn.close()
    print(f"BACKUP={backup_path}")
    print(f"CHANGED={changed}")


if __name__ == "__main__":
    main()
