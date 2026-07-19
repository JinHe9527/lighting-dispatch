"""派工写入服务。所有关键校验、状态迁移和审计在同一事务内完成。"""

from datetime import datetime, timedelta

try:
    from . import business
except ImportError:
    import business


class DispatchService:
    def __init__(self, db_factory, setting, today, base_url):
        self.db = db_factory
        self.setting = setting
        self.today = today
        self.base_url = base_url

    @staticmethod
    def _record_id(value):
        try:
            result = int(value)
        except (TypeError, ValueError):
            return None
        return result if result > 0 else None

    @staticmethod
    def _hm(value):
        text = str(value or "").strip()
        try:
            hour, minute = map(int, text.split(":"))
        except (TypeError, ValueError):
            return ""
        return f"{hour:02d}:{minute:02d}" if 0 <= hour <= 23 and 0 <= minute <= 59 else ""

    @staticmethod
    def _subtract(value, minutes):
        total = business.minute_of_day(value)
        if total is None:
            return ""
        result = (total - int(minutes)) % 1440
        return f"{result // 60:02d}:{result % 60:02d}"

    def _team(self, conn, user, body):
        if user["role"] == "team":
            team_id = business.bound_team_id(user)
            if not team_id:
                return None, "团队账号尚未绑定团队，请联系管理员"
            row = conn.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
            return (row, None) if row else (None, "绑定的团队不存在，请联系管理员")
        team_id = self._record_id(body.get("team_id"))
        if team_id:
            row = conn.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM teams WHERE name=?", ((body.get("team") or "").strip(),)).fetchone()
        return (row, None) if row else (None, "团队不存在")

    @staticmethod
    def _task(conn, grp, task_type, custom_minutes=None):
        row = conn.execute("SELECT * FROM sla WHERE grp=? AND task_type=?", (grp, task_type)).fetchone()
        if row:
            return row["minutes"], row["buffer"], True
        try:
            minutes = int(custom_minutes or 30)
        except (TypeError, ValueError):
            minutes = 30
        return max(10, min(720, minutes)), 0, False

    @staticmethod
    def _order_dict(row):
        return dict(row) if row else None

    def _queue_assignment(self, conn, order, worker, action):
        text = (
            f"{action}：{order['grp']} · {order['team']} · {order['room']}间 · "
            f"{order['task_type']} · 到岗 {order['date']} {order['arrive']}\n{self.base_url}"
        )
        business.queue_notification(
            conn, worker["open_id"], text, f"order:{order['id']}:assignment:v{order['version']}",
            urgent=bool(order["urgent"]),
        )
        business.queue_task_operation(
            conn,
            "task_create",
            worker["open_id"],
            {
                "order_id": order["id"],
                "summary": f"{order['grp']}派工 · {order['team']} · {order['task_type']}",
                "description": f"{order['room']}间 · 到岗 {order['date']} {order['arrive']}",
                "date": order["date"],
                "arrive": order["arrive"],
            },
            f"order:{order['id']}:task-create:v{order['version']}",
        )

    def _queue_open_order(self, conn, order):
        for worker in business.eligible_workers(conn, order):
            business.queue_notification(
                conn,
                worker["open_id"],
                f"新待派工：{order['grp']} · {order['team']} · {order['room']}间 · "
                f"{order['task_type']} · 到岗 {order['date']} {order['arrive']}\n{self.base_url}",
                f"order:{order['id']}:open:{worker['open_id']}",
                urgent=bool(order["urgent"]),
            )

    def _claim_order_uploads(self, conn, user, order_id, body):
        tokens = body.get("upload_tokens") or []
        if isinstance(tokens, str):
            tokens = [tokens]
        tokens = [str(token or "").strip() for token in tokens if str(token or "").strip()][:2]
        claimed = []
        for token in tokens:
            row = business.claim_upload(conn, token, user["open_id"], order_id)
            if not row:
                return None, "附件无效、已被使用或不属于当前账号，请重新上传"
            claimed.append(row)
        return claimed, None

    def order(self, user, body):
        grp = (body.get("group") or "").strip()
        task_type = (body.get("task") or "").strip()
        if not grp or not task_type:
            return 400, "请选择处理组并填写任务"
        conn = self.db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM groups WHERE name=?", (grp,)).fetchone():
                return 400, "处理组不存在"
            if user["role"] == "staff" and (user.get("grp") or "") != grp:
                return 403, "工作人员和组长只能给本组下单"
            team, error = self._team(conn, user, body)
            if error:
                return 403 if user["role"] == "team" else 400, error

            minutes, buffer, known_task = self._task(conn, grp, task_type, body.get("minutes"))
            if not known_task:
                return 400, "任务类型尚未配置，请先由管理员在规则库中添加"
            when = (body.get("when") or "auto").strip()
            urgent = 1 if str(body.get("urgent") or "") in ("1", "true", "on", "yes") else 0
            booked = 0
            if when == "now":
                current = datetime.now()
                order_date = self.today()
                arrive = current.strftime("%H:%M")
                base_time = arrive
            elif when == "book":
                booked = 1
                order_date = str(body.get("date") or "").strip()
                arrive = self._hm(body.get("at"))
                try:
                    date_value = datetime.strptime(order_date, "%Y-%m-%d").date()
                    today_value = datetime.strptime(self.today(), "%Y-%m-%d").date()
                except ValueError:
                    return 400, "预约日期无效"
                if date_value < today_value:
                    return 400, "预约日期不能早于今天"
                if user["role"] == "team":
                    horizon = max(1, min(31, int(self.setting("book_horizon_days", "7") or 7)))
                    if date_value > today_value + timedelta(days=horizon):
                        return 400, f"团队账号只能预约未来 {horizon} 天内的时段"
                if not arrive:
                    return 400, "预约开始时间无效"
                if user["role"] == "team" and self.setting("rule_book_lock_sla", "1") == "1":
                    duration = minutes
                else:
                    end_text = self._hm(body.get("end"))
                    if not end_text:
                        return 400, "请选择预约结束时间"
                    start_min, end_min = business.minute_of_day(arrive), business.minute_of_day(end_text)
                    duration = (end_min - start_min) % 1440
                    if duration < 10:
                        return 400, "预约时长至少 10 分钟"
                    max_minutes = max(60, min(1440, int(self.setting("book_max_min", "720") or 720)))
                    if duration > max_minutes:
                        return 400, f"预约时长不能超过 {max_minutes // 60} 小时"
                minutes, buffer = duration, 0
                base_time = arrive
            elif when == "auto":
                base_time = self._hm(str(team["rehearsal"] or "").split("/", 1)[0]) or self._hm(team["onair"])
                if not base_time:
                    return 400, "该团队没有排期时间，请改用立即或预约"
                arrive = self._subtract(base_time, minutes + buffer)
                order_date = self.today()
            else:
                return 400, "下单时间方式无效"

            start, end = business.schedule_bounds(order_date, arrive, minutes)
            if not start or not end:
                return 400, "订单时间无效"
            if when == "book" and start <= datetime.now():
                return 400, "预约开始时间必须晚于当前时间"

            if booked and user["role"] == "team":
                cap = max(0, min(10, int(self.setting("book_team_cap", "2") or 2)))
                held = conn.execute(
                    """SELECT COUNT(*) count FROM orders
                       WHERE team_id=? AND booked=1 AND status IN ('pending','assigned')""",
                    (team["id"],),
                ).fetchone()["count"]
                if cap and held >= cap:
                    return 409, f"该团队已有 {held} 个未开始预约，上限为 {cap} 个"
                day_cap = max(0, min(1440, int(self.setting("book_team_day_min", "240") or 240)))
                used = conn.execute(
                    """SELECT COALESCE(SUM(minutes),0) minutes FROM orders
                       WHERE team_id=? AND date=? AND booked=1
                       AND status NOT IN ('cancelled','abnormal')""",
                    (team["id"], order_date),
                ).fetchone()["minutes"]
                if day_cap and used + minutes > day_cap:
                    return 409, f"该团队当天预约将超过 {day_cap} 分钟上限"

            note = (body.get("note") or "").strip()[:2000]
            if grp == "设备组" and not note and self.setting("rule_dev_note", "1") == "1":
                return 400, "设备组下单必须填写问题描述"
            tokens = body.get("upload_tokens") or []
            has_upload = bool(tokens if isinstance(tokens, list) else str(tokens).strip())
            if grp == "灯光组" and "编歌" in task_type and not has_upload \
                    and self.setting("rule_biange_media", "1") == "1":
                return 400, "编歌必须上传歌曲音频或视频"

            cursor = conn.execute(
                """INSERT INTO orders(
                     date,team_id,team,room,task_type,minutes,base_time,arrive,grp,status,
                     creator_id,creator_name,note,created_at,urgent,booked,scheduled_start,scheduled_end,version
                   ) VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?,?,1)""",
                (
                    order_date, team["id"], team["name"], team["room"], task_type, minutes,
                    base_time, arrive, grp, user["open_id"], user["name"], note,
                    business.now_iso(), urgent, booked, business.format_dt(start), business.format_dt(end),
                ),
            )
            order_id = cursor.lastrowid
            claimed, error = self._claim_order_uploads(conn, user, order_id, body)
            if error:
                return 400, error
            media = claimed[0]["path"] if claimed else ""
            media_kind = claimed[0]["kind"] if claimed else ""
            media2 = claimed[1]["path"] if len(claimed) > 1 else ""
            media2_kind = claimed[1]["kind"] if len(claimed) > 1 else ""
            conn.execute(
                "UPDATE orders SET media=?,media_kind=?,media2=?,media2_kind=? WHERE id=?",
                (media, media_kind, media2, media2_kind, order_id),
            )
            order = self._order_dict(conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())

            requested_assignee = (body.get("assignee") or "").strip()
            can_choose = business.is_manager(user, grp)
            worker = None
            if requested_assignee and can_choose:
                worker = conn.execute("SELECT * FROM users WHERE open_id=?", (requested_assignee,)).fetchone()
                if not worker:
                    return 400, "指派人员不存在"
                block = business.assignment_block(conn, worker, order)
                if block:
                    return 409, block
            elif requested_assignee and not can_choose:
                return 403, "普通下单人员不能指定工作人员"
            elif conn.execute("SELECT COALESCE(auto,0) auto FROM groups WHERE name=?", (grp,)).fetchone()["auto"]:
                worker = business.fair_assignee(conn, order)

            if worker:
                conn.execute(
                    """UPDATE orders SET assignee_id=?,assignee_name=?,status='assigned',accepted_at=?,version=version+1
                       WHERE id=?""",
                    (worker["open_id"], business.display_name(worker), business.now_iso(), order_id),
                )
                conn.execute("UPDATE users SET last_assigned_at=? WHERE open_id=?", (business.now_iso(), worker["open_id"]))
                order = self._order_dict(conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())
                business.add_event(conn, order_id, "下单并指派", user, "", "assigned", f"{team['name']} → {business.display_name(worker)}")
                self._queue_assignment(conn, order, worker, "新派工")
            else:
                business.add_event(conn, order_id, "创建工单", user, "", "pending", f"{team['name']} · {task_type}")
                self._queue_open_order(conn, order)
            conn.commit()
            return 200, None
        except (ValueError, TypeError):
            conn.rollback()
            return 400, "订单规则参数无效"
        finally:
            conn.close()

    def assign(self, user, body):
        order_id = self._record_id(body.get("order_id"))
        if not order_id:
            return 400, "非法工单编号"
        conn = self.db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                return 404, "工单不存在"
            if order["status"] != "pending":
                return 409, "只有待派工订单可以接单或指派"
            target_id = (body.get("user_id") or "").strip()
            if target_id:
                if not business.can_manage_order(user, order):
                    return 403, "只有管理员或本组组长可以指定负责人"
            else:
                if user["role"] == "team" or (user.get("grp") or "") != order["grp"]:
                    return 403, "只能接本组待派工订单"
                target_id = user["open_id"]
            worker = conn.execute("SELECT * FROM users WHERE open_id=?", (target_id,)).fetchone()
            if not worker:
                return 400, "负责人不存在"
            start, _ = business.order_bounds(order)
            require_now = bool(start and start <= datetime.now() + timedelta(minutes=15))
            block = business.assignment_block(conn, worker, order, require_available_now=require_now)
            if block:
                return 409, block
            stamp = business.now_iso()
            changed = conn.execute(
                """UPDATE orders SET assignee_id=?,assignee_name=?,status='assigned',accepted_at=?,version=version+1
                   WHERE id=? AND status='pending'""",
                (worker["open_id"], business.display_name(worker), stamp, order_id),
            ).rowcount
            if changed != 1:
                return 409, "该订单已被其他人接走"
            conn.execute("UPDATE users SET last_assigned_at=? WHERE open_id=?", (stamp, worker["open_id"]))
            updated = self._order_dict(conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())
            event = "接单" if worker["open_id"] == user["open_id"] else "管理指派"
            business.add_event(conn, order_id, event, user, "pending", "assigned", business.display_name(worker))
            self._queue_assignment(conn, updated, worker, "你已接单" if event == "接单" else "新派工")
            conn.commit()
            return 200, None
        finally:
            conn.close()

    def start(self, user, body):
        order_id = self._record_id(body.get("order_id"))
        if not order_id:
            return 400, "非法工单编号"
        conn = self.db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                return 404, "工单不存在"
            if order["status"] != "assigned":
                return 409, "只有已指派且未开始的订单可以开始"
            if order["assignee_id"] != user["open_id"]:
                return 403, "只有当前负责人可以开始任务"
            start, _ = business.order_bounds(order)
            early = max(0, min(120, int(self.setting("start_early_min", "30") or 30)))
            if start and datetime.now() < start - timedelta(minutes=early):
                return 409, f"距离到岗时间过早，最多提前 {early} 分钟开始"
            stamp = business.now_iso()
            conn.execute(
                "UPDATE orders SET status='in_progress',started_at=?,version=version+1 WHERE id=? AND status='assigned'",
                (stamp, order_id),
            )
            conn.execute("UPDATE users SET status='busy',task=? WHERE open_id=?", (f"{order['team']} · {order['room']}", user["open_id"]))
            conn.execute(
                "INSERT INTO work_sessions(order_id,worker_id,started_at) VALUES(?,?,?)",
                (order_id, user["open_id"], stamp),
            )
            business.add_event(conn, order_id, "开始任务", user, "assigned", "in_progress", order["task_type"])
            conn.commit()
            return 200, None
        finally:
            conn.close()

    def complete(self, user, body):
        order_id = self._record_id(body.get("order_id"))
        note = (body.get("note") or "").strip()
        if not order_id:
            return 400, "非法工单编号"
        if self.setting("rule_done_note", "1") == "1" and not note:
            return 400, "完成前请填写完成说明"
        conn = self.db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                return 404, "工单不存在"
            if order["status"] != "in_progress" or not order["started_at"]:
                return 409, "只有已经开始的订单可以完成"
            if user["role"] != "admin" and order["assignee_id"] != user["open_id"]:
                return 403, "只能完成自己负责的订单"
            stamp = business.now_iso()
            conn.execute(
                "UPDATE orders SET status='completed',done_at=?,done_note=?,version=version+1 WHERE id=?",
                (stamp, note[:300], order_id),
            )
            conn.execute(
                "UPDATE work_sessions SET ended_at=?,end_reason='completed' WHERE order_id=? AND ended_at=''",
                (stamp, order_id),
            )
            business.close_active_transfers(conn, order_id)
            self._release_worker(conn, order["assignee_id"])
            business.add_event(conn, order_id, "完成工单", user, "in_progress", "completed", note[:300])
            if order["feishu_task_guid"]:
                business.queue_task_operation(
                    conn, "task_delete", "", {"guid": order["feishu_task_guid"], "order_id": order_id},
                    f"order:{order_id}:task-delete:completed",
                )
            conn.commit()
            return 200, None
        finally:
            conn.close()

    @staticmethod
    def _release_worker(conn, open_id):
        if not open_id:
            return
        active = conn.execute(
            "SELECT team,room FROM orders WHERE assignee_id=? AND status='in_progress' ORDER BY started_at,id LIMIT 1",
            (open_id,),
        ).fetchone()
        if active:
            conn.execute("UPDATE users SET status='busy',task=? WHERE open_id=?", (f"{active['team']} · {active['room']}", open_id))
        else:
            current = conn.execute("SELECT status FROM users WHERE open_id=?", (open_id,)).fetchone()
            if current and current["status"] == "busy":
                conn.execute("UPDATE users SET status='free',task='' WHERE open_id=?", (open_id,))

    def reassign(self, user, body):
        order_id = self._record_id(body.get("order_id"))
        if not order_id:
            return 400, "非法工单编号"
        target_id = (body.get("user_id") or "").strip()
        conn = self.db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                return 404, "工单不存在"
            if not business.can_manage_order(user, order):
                return 403, "只有管理员或本组组长可以改派"
            if order["status"] not in ("pending", "assigned"):
                return 409, "任务开始后必须使用转包流程，不能直接改派"
            if business.active_transfer(conn, order_id):
                return 409, "该订单有待处理的转包申请，请先审批、接受或撤回"
            old_id = order["assignee_id"] or ""
            old_status = order["status"]
            if not target_id:
                if old_status != "assigned":
                    return 409, "订单已经处于待派工状态"
                conn.execute(
                    """UPDATE orders SET assignee_id='',assignee_name='',status='pending',accepted_at='',
                       version=version+1 WHERE id=?""",
                    (order_id,),
                )
                business.add_event(conn, order_id, "撤回指派", user, "assigned", "pending", "重新进入待派池")
                self._queue_open_order(conn, self._order_dict(conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()))
            else:
                if target_id == old_id:
                    return 400, "该成员已经是负责人"
                worker = conn.execute("SELECT * FROM users WHERE open_id=?", (target_id,)).fetchone()
                if not worker:
                    return 400, "目标人员不存在"
                block = business.assignment_block(conn, worker, order, ignore_order_id=order_id)
                if block:
                    return 409, block
                stamp = business.now_iso()
                conn.execute(
                    """UPDATE orders SET assignee_id=?,assignee_name=?,status='assigned',accepted_at=?,
                       version=version+1 WHERE id=?""",
                    (target_id, business.display_name(worker), stamp, order_id),
                )
                conn.execute("UPDATE users SET last_assigned_at=? WHERE open_id=?", (stamp, target_id))
                updated = self._order_dict(conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())
                business.add_event(conn, order_id, "改派", user, old_status, "assigned", f"{order['assignee_name'] or '未指派'} → {business.display_name(worker)}")
                self._queue_assignment(conn, updated, worker, "订单已改派给你")
            if order["feishu_task_guid"]:
                business.queue_task_operation(
                    conn, "task_delete", "", {"guid": order["feishu_task_guid"], "order_id": order_id},
                    f"order:{order_id}:task-delete:reassign:v{order['version']}",
                )
            if old_id:
                business.queue_notification(
                    conn, old_id,
                    f"工单已{'改派' if target_id else '撤回'}：#{order_id} {order['team']} · {order['task_type']}",
                    f"order:{order_id}:reassign:old:{order['version']}",
                )
            conn.commit()
            return 200, None
        finally:
            conn.close()

    def cancel(self, user, body):
        order_id = self._record_id(body.get("order_id"))
        reason = (body.get("reason") or "").strip()
        if not order_id:
            return 400, "非法工单编号"
        if not reason:
            return 400, "取消订单必须填写原因"
        conn = self.db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                return 404, "工单不存在"
            if order["status"] in ("completed", "cancelled", "abnormal"):
                return 409, "当前订单状态不能取消"
            if user["role"] == "team":
                if order["creator_id"] != user["open_id"] or int(order["team_id"] or 0) != business.bound_team_id(user):
                    return 403, "只能取消自己创建的本团队订单"
                if order["status"] == "in_progress":
                    return 409, "任务已经开始，请联系组长处理"
            elif not business.can_manage_order(user, order):
                return 403, "无权取消该订单"
            if order["status"] == "in_progress" and user["role"] != "admin":
                return 409, "进行中的订单只能由管理员异常终止"
            stamp = business.now_iso()
            old_status = order["status"]
            terminal_status = "abnormal" if old_status == "in_progress" else "cancelled"
            if terminal_status == "abnormal":
                conn.execute(
                    """UPDATE orders SET status='abnormal',abnormal_at=?,abnormal_reason=?,
                       version=version+1 WHERE id=?""",
                    (stamp, reason[:300], order_id),
                )
            else:
                conn.execute(
                    """UPDATE orders SET status='cancelled',cancelled_at=?,cancelled_by=?,cancel_reason=?,
                       version=version+1 WHERE id=?""",
                    (stamp, user["open_id"], reason[:300], order_id),
                )
            conn.execute(
                "UPDATE work_sessions SET ended_at=?,end_reason=? WHERE order_id=? AND ended_at=''",
                (stamp, terminal_status, order_id),
            )
            business.close_active_transfers(conn, order_id)
            self._release_worker(conn, order["assignee_id"])
            event_type = "异常终止" if terminal_status == "abnormal" else "取消工单"
            business.add_event(conn, order_id, event_type, user, old_status, terminal_status, reason[:300])
            if order["assignee_id"]:
                business.queue_notification(
                    conn, order["assignee_id"],
                    f"工单已{'异常终止' if terminal_status == 'abnormal' else '取消'}："
                    f"{order['team']} · {order['task_type']}；原因：{reason}",
                    f"order:{order_id}:{terminal_status}:v{order['version']}",
                )
            if order["feishu_task_guid"]:
                business.queue_task_operation(
                    conn, "task_delete", "", {"guid": order["feishu_task_guid"], "order_id": order_id},
                    f"order:{order_id}:task-delete:{terminal_status}",
                )
            conn.commit()
            return 200, None
        finally:
            conn.close()

    def edit(self, user, body):
        order_id = self._record_id(body.get("order_id"))
        if not order_id:
            return 400, "非法工单编号"
        conn = self.db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                return 404, "工单不存在"
            creator_can_edit = (
                user["role"] == "team" and order["creator_id"] == user["open_id"]
                and int(order["team_id"] or 0) == business.bound_team_id(user)
            )
            if not creator_can_edit and not business.can_manage_order(user, order):
                return 403, "无权编辑该订单"
            if order["status"] != "pending":
                return 409, "只有待派工订单可以编辑"
            task_type = (body.get("task") or order["task_type"] or "").strip()
            if not task_type:
                return 400, "任务不能为空"
            minutes, buffer, known_task = self._task(conn, order["grp"], task_type, body.get("minutes"))
            if not known_task:
                return 400, "任务类型尚未配置，请先由管理员在规则库中添加"
            arrive = order["arrive"]
            base_time = order["base_time"]
            if not order["booked"]:
                team = conn.execute("SELECT * FROM teams WHERE id=?", (order["team_id"],)).fetchone()
                if base_time and team:
                    arrive = self._subtract(base_time, minutes + buffer)
            start, end = business.schedule_bounds(order["date"], arrive, minutes)
            if not start or not end:
                return 400, "更新后的订单时间无效"
            urgent = order["urgent"]
            if business.is_manager(user, order["grp"]) and "urgent" in body:
                urgent = 1 if str(body.get("urgent")) in ("1", "true", "on", "yes") else 0
            conn.execute(
                """UPDATE orders SET task_type=?,minutes=?,arrive=?,urgent=?,scheduled_start=?,scheduled_end=?,
                   version=version+1 WHERE id=?""",
                (task_type, minutes, arrive, urgent, business.format_dt(start), business.format_dt(end), order_id),
            )
            business.add_event(conn, order_id, "编辑工单", user, "pending", "pending", f"{task_type} · {arrive}")
            conn.commit()
            return 200, None
        finally:
            conn.close()

    def tech_status(self, user, body):
        if user["role"] == "team":
            return 403, "团队账号不能修改工作人员状态"
        target_id = (body.get("user_id") or user["open_id"]).strip()
        status = (body.get("status") or "").strip()
        if status not in ("free", "busy", "check", "rest", "off"):
            return 400, "状态无效"
        conn = self.db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            target = conn.execute("SELECT * FROM users WHERE open_id=?", (target_id,)).fetchone()
            if not target or target["role"] == "team":
                return 404, "工作人员不存在"
            if target_id != user["open_id"] and not business.is_manager(user, target["grp"]):
                return 403, "只能修改自己或本组成员状态"
            active = conn.execute(
                "SELECT COUNT(*) count FROM orders WHERE assignee_id=? AND status='in_progress'", (target_id,)
            ).fetchone()["count"]
            if active and status != "busy":
                return 409, "该人员有正在执行的订单，不能切换到其他状态"
            task = (body.get("task") or "").strip()[:100] if status in ("busy", "check") else ""
            conn.execute("UPDATE users SET status=?,task=? WHERE open_id=?", (status, task, target_id))
            conn.execute(
                "INSERT INTO audit(ts,open_id,name,action,detail) VALUES(?,?,?,?,?)",
                (business.now_iso(), user["open_id"], user["name"], "修改人员状态", f"{business.display_name(target)} → {status}"),
            )
            conn.commit()
            return 200, None
        finally:
            conn.close()

    def request_transfer(self, user, body):
        order_id = self._record_id(body.get("order_id"))
        reason_code = (body.get("reason_code") or "").strip()
        reason_detail = (body.get("reason_detail") or "").strip()
        target_id = (body.get("target_id") or "").strip()
        if not order_id:
            return 400, "非法工单编号"
        if reason_code not in business.TRANSFER_REASON:
            return 400, "请选择有效的转包原因"
        if reason_code == "其他紧急情况" and not reason_detail:
            return 400, "其他紧急情况必须填写说明"
        conn = self.db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                return 404, "工单不存在"
            if order["assignee_id"] != user["open_id"]:
                return 403, "只有当前负责人可以发起转包"
            if order["status"] not in ("assigned", "in_progress"):
                return 409, "当前订单状态不能转包"
            if business.active_transfer(conn, order_id):
                return 409, "该订单已经有待处理的转包申请"
            count = business.transfer_count_30d(conn, user["open_id"])
            if target_id == user["open_id"]:
                return 400, "不能转包给自己"
            if target_id:
                target = conn.execute("SELECT * FROM users WHERE open_id=?", (target_id,)).fetchone()
                if not target:
                    return 404, "目标人员不存在"
                previous_owner = conn.execute(
                    """SELECT 1 FROM transfers WHERE order_id=?
                       AND (requester_id=? OR accepted_by=?) LIMIT 1""",
                    (order_id, target_id, target_id),
                ).fetchone()
                if previous_owner:
                    return 409, "不能把订单转回历史负责人"
                block = business.assignment_block(conn, target, order, ignore_order_id=order_id)
                if block:
                    return 409, block
            start, _ = business.order_bounds(order)
            urgent_window = bool(start and start - datetime.now() <= timedelta(hours=2))
            order_transfer_count = conn.execute(
                "SELECT COUNT(*) count FROM transfers WHERE order_id=?", (order_id,)
            ).fetchone()["count"]
            requires_lead = count >= 1 or order_transfer_count > 0 or urgent_window or order["status"] == "in_progress"
            transfer_status = "awaiting_lead" if requires_lead else "open"
            cursor = conn.execute(
                """INSERT INTO transfers(
                     order_id,requester_id,requester_name,target_id,target_name,reason_code,reason_detail,
                     status,requires_lead,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    order_id, user["open_id"], user["name"], target_id,
                    business.display_name(target) if target_id else "", reason_code, reason_detail[:500],
                    transfer_status, 1 if requires_lead else 0, business.now_iso(),
                ),
            )
            transfer_id = cursor.lastrowid
            business.add_event(
                conn, order_id, "发起转包", user, order["status"], order["status"],
                f"转包 #{transfer_id} · {reason_code} · {transfer_status}",
            )
            if requires_lead:
                leads = conn.execute("SELECT open_id FROM users WHERE grp=? AND (lead=1 OR role='admin')", (order["grp"],)).fetchall()
                for lead in leads:
                    business.queue_notification(
                        conn, lead["open_id"], f"转包待审批：#{order_id} {order['team']} · {reason_code}",
                        f"transfer:{transfer_id}:approval:{lead['open_id']}", urgent=urgent_window,
                    )
            elif target_id:
                business.queue_notification(
                    conn, target_id, f"收到转包邀请：#{order_id} {order['team']} · {order['task_type']}",
                    f"transfer:{transfer_id}:target", urgent=urgent_window,
                )
            else:
                for worker in business.eligible_workers(conn, order, ignore_order_id=order_id):
                    if worker["open_id"] != user["open_id"]:
                        business.queue_notification(
                            conn, worker["open_id"], f"可接转包：#{order_id} {order['team']} · {order['task_type']}",
                            f"transfer:{transfer_id}:open:{worker['open_id']}", urgent=urgent_window,
                        )
            conn.commit()
            return 200, None
        finally:
            conn.close()

    def approve_transfer(self, user, body):
        transfer_id = self._record_id(body.get("transfer_id"))
        approve = str(body.get("approve") or "1") in ("1", "true", "on", "yes")
        if not transfer_id:
            return 400, "非法转包编号"
        conn = self.db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            transfer = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            if not transfer:
                return 404, "转包申请不存在"
            order = conn.execute("SELECT * FROM orders WHERE id=?", (transfer["order_id"],)).fetchone()
            if not order or not business.can_manage_order(user, order):
                return 403, "只有本组组长或管理员可以审批"
            if transfer["status"] != "awaiting_lead":
                return 409, "该转包申请不在待审批状态"
            stamp = business.now_iso()
            if not approve:
                conn.execute(
                    "UPDATE transfers SET status='rejected',approved_by=?,approved_at=?,resolved_at=? WHERE id=?",
                    (user["open_id"], stamp, stamp, transfer_id),
                )
                business.add_event(conn, order["id"], "拒绝转包", user, order["status"], order["status"], f"转包 #{transfer_id}")
                business.queue_notification(
                    conn, transfer["requester_id"],
                    f"转包申请被拒绝：#{order['id']} {order['team']} · {order['task_type']}",
                    f"transfer:{transfer_id}:rejected",
                )
            else:
                conn.execute(
                    "UPDATE transfers SET status='open',approved_by=?,approved_at=? WHERE id=?",
                    (user["open_id"], stamp, transfer_id),
                )
                business.add_event(conn, order["id"], "批准转包", user, order["status"], order["status"], f"转包 #{transfer_id}")
                business.queue_notification(
                    conn, transfer["requester_id"],
                    f"转包申请已批准：#{order['id']} {order['team']} · {order['task_type']}",
                    f"transfer:{transfer_id}:approved:requester",
                )
                targets = []
                if transfer["target_id"]:
                    targets = [transfer["target_id"]]
                else:
                    targets = [worker["open_id"] for worker in business.eligible_workers(conn, order, ignore_order_id=order["id"])
                               if worker["open_id"] != transfer["requester_id"]]
                for target_id in targets:
                    business.queue_notification(
                        conn, target_id, f"转包已开放：#{order['id']} {order['team']} · {order['task_type']}",
                        f"transfer:{transfer_id}:approved:{target_id}", urgent=True,
                    )
            conn.commit()
            return 200, None
        finally:
            conn.close()

    def accept_transfer(self, user, body):
        transfer_id = self._record_id(body.get("transfer_id"))
        if not transfer_id:
            return 400, "非法转包编号"
        conn = self.db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            transfer = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            if not transfer:
                return 404, "转包申请不存在"
            if transfer["status"] != "open":
                return 409, "该转包申请尚未开放或已经处理"
            order = conn.execute("SELECT * FROM orders WHERE id=?", (transfer["order_id"],)).fetchone()
            if not order or order["status"] not in ("assigned", "in_progress"):
                return 409, "原订单状态已经变化"
            if transfer["target_id"] and transfer["target_id"] != user["open_id"]:
                return 403, "该转包只邀请了指定人员"
            if user["open_id"] == transfer["requester_id"]:
                return 403, "发起人不能接受自己的转包"
            previous_owner = conn.execute(
                """SELECT 1 FROM transfers WHERE order_id=? AND id!=?
                   AND (requester_id=? OR accepted_by=?) LIMIT 1""",
                (order["id"], transfer_id, user["open_id"], user["open_id"]),
            ).fetchone()
            if previous_owner:
                return 409, "历史负责人不能再次接回同一订单"
            worker = conn.execute("SELECT * FROM users WHERE open_id=?", (user["open_id"],)).fetchone()
            block = business.assignment_block(conn, worker, order, ignore_order_id=order["id"])
            if block:
                return 409, block
            stamp = business.now_iso()
            old_id = order["assignee_id"]
            conn.execute(
                """UPDATE orders SET assignee_id=?,assignee_name=?,accepted_at=?,version=version+1
                   WHERE id=?""",
                (user["open_id"], business.display_name(worker), stamp, order["id"]),
            )
            conn.execute(
                """UPDATE transfers SET status='accepted',accepted_by=?,accepted_at=?,resolved_at=?
                   WHERE id=? AND status='open'""",
                (user["open_id"], stamp, stamp, transfer_id),
            )
            if order["status"] == "in_progress":
                conn.execute(
                    "UPDATE work_sessions SET ended_at=?,end_reason='transferred' WHERE order_id=? AND ended_at=''",
                    (stamp, order["id"]),
                )
                conn.execute(
                    "INSERT INTO work_sessions(order_id,worker_id,started_at) VALUES(?,?,?)",
                    (order["id"], user["open_id"], stamp),
                )
                conn.execute("UPDATE users SET status='busy',task=? WHERE open_id=?", (f"{order['team']} · {order['room']}", user["open_id"]))
                self._release_worker(conn, old_id)
            conn.execute("UPDATE users SET last_assigned_at=? WHERE open_id=?", (stamp, user["open_id"]))
            business.add_event(
                conn, order["id"], "接受转包", user, order["status"], order["status"],
                f"{order['assignee_name']} → {business.display_name(worker)}",
            )
            updated = self._order_dict(conn.execute("SELECT * FROM orders WHERE id=?", (order["id"],)).fetchone())
            if order["feishu_task_guid"]:
                business.queue_task_operation(
                    conn, "task_delete", "", {"guid": order["feishu_task_guid"], "order_id": order["id"]},
                    f"order:{order['id']}:task-delete:transfer:{transfer_id}",
                )
            self._queue_assignment(conn, updated, worker, "你已接受转包")
            business.queue_notification(
                conn, old_id, f"转包已由 {business.display_name(worker)} 接受：#{order['id']} {order['team']}",
                f"transfer:{transfer_id}:accepted:requester",
            )
            conn.commit()
            return 200, None
        finally:
            conn.close()

    def withdraw_transfer(self, user, body):
        transfer_id = self._record_id(body.get("transfer_id"))
        if not transfer_id:
            return 400, "非法转包编号"
        conn = self.db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            transfer = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            if not transfer:
                return 404, "转包申请不存在"
            if transfer["requester_id"] != user["open_id"]:
                return 403, "只能撤回自己发起的转包"
            if transfer["status"] not in ("awaiting_lead", "open"):
                return 409, "该转包申请已经处理"
            stamp = business.now_iso()
            conn.execute("UPDATE transfers SET status='withdrawn',resolved_at=? WHERE id=?", (stamp, transfer_id))
            order = conn.execute("SELECT * FROM orders WHERE id=?", (transfer["order_id"],)).fetchone()
            if order:
                business.add_event(conn, order["id"], "撤回转包", user, order["status"], order["status"], f"转包 #{transfer_id}")
            conn.commit()
            return 200, None
        finally:
            conn.close()
