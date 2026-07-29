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

    @staticmethod
    def _timestamp(value):
        text = str(value or "").strip().replace("T", " ")
        if len(text) == 16:
            text += ":00"
        return business.parse_dt(text)

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

    @staticmethod
    def _open_session(conn, order_id, worker_id=""):
        sql = "SELECT * FROM work_sessions WHERE order_id=? AND ended_at=''"
        args = [order_id]
        if worker_id:
            sql += " AND worker_id=?"
            args.append(worker_id)
        return conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()

    @staticmethod
    def _worker_has_open_session(conn, worker_id, ignore_order_id=None):
        sql = "SELECT order_id FROM work_sessions WHERE worker_id=? AND ended_at=''"
        args = [worker_id]
        if ignore_order_id:
            sql += " AND order_id!=?"
            args.append(ignore_order_id)
        return conn.execute(sql + " LIMIT 1", args).fetchone()

    @staticmethod
    def _session_overlap(conn, worker_id, started_at, ended_at, ignore_session_id=None):
        sql = (
            "SELECT order_id FROM work_sessions WHERE worker_id=? "
            "AND started_at<? AND (ended_at='' OR ended_at>?)"
        )
        args = [worker_id, business.format_dt(ended_at), business.format_dt(started_at)]
        if ignore_session_id:
            sql += " AND id!=?"
            args.append(ignore_session_id)
        return conn.execute(sql + " ORDER BY started_at LIMIT 1", args).fetchone()

    def _queue_time_review(self, conn, order, review_id, label):
        managers = conn.execute(
            "SELECT open_id FROM users WHERE role='admin' OR (role='staff' AND lead=1 AND grp=?)",
            (order["grp"],),
        ).fetchall()
        review = conn.execute(
            "SELECT requested_by,worker_id FROM time_reviews WHERE id=?", (review_id,)
        ).fetchone()
        excluded = {review["requested_by"], review["worker_id"]} if review else set()
        for manager in managers:
            if manager["open_id"] in excluded:
                continue
            business.queue_notification(
                conn,
                manager["open_id"],
                f"{label}：#{order['id']} {order['team']} · {order['task_type']} · {order['assignee_name']}",
                f"time-review:{review_id}:manager:{manager['open_id']}",
                urgent=True,
            )

    def _create_time_review(self, conn, order, user, kind, proposed_at, note="", reason="", original_at=""):
        cursor = conn.execute(
            """INSERT INTO time_reviews(
                 order_id,worker_id,kind,proposed_at,original_at,note,reason,status,
                 requested_by,requested_name,requested_at
               ) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)""",
            (
                order["id"], order["assignee_id"], kind, business.format_dt(proposed_at),
                original_at or "", note[:300], reason[:500], user["open_id"], user["name"],
                business.now_iso(),
            ),
        )
        return cursor.lastrowid

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
            urgent = 1 if (
                business.is_manager(user, grp)
                and str(body.get("urgent") or "") in ("1", "true", "on", "yes")
            ) else 0
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
                # 预约占用时长始终由任务标准工时决定。
                buffer = 0
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
                require_now = start <= datetime.now() + timedelta(minutes=15)
                block = business.assignment_block(
                    conn, worker, order, require_available_now=require_now
                )
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
            worker = conn.execute("SELECT * FROM users WHERE open_id=?", (user["open_id"],)).fetchone()
            if not worker:
                return 404, "工作人员不存在"
            block = business.assignment_block(
                conn, worker, order, ignore_order_id=order_id, require_available_now=True
            )
            if block:
                return 409, block
            active = self._worker_has_open_session(conn, user["open_id"], order_id)
            if active:
                return 409, f"你还有进行中的工单 #{active['order_id']}，结束或转包后才能开始新任务"
            if business.active_time_review(conn, order_id):
                return 409, "该订单有待处理的时间核实，不能重复开始"
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

    def report_start(self, user, body):
        order_id = self._record_id(body.get("order_id"))
        proposed = self._timestamp(body.get("started_at"))
        reason = (body.get("reason") or "").strip()
        if not order_id:
            return 400, "非法工单编号"
        if not proposed:
            return 400, "补报开始时间无效"
        if not reason:
            return 400, "补报开始必须填写原因"
        conn = self.db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                return 404, "工单不存在"
            if order["status"] != "assigned" or order["started_at"]:
                return 409, "只有尚未开始的已指派订单可以补报开始"
            if order["assignee_id"] != user["open_id"]:
                return 403, "只能补报自己负责的订单"
            if business.active_time_review(conn, order_id):
                return 409, "该订单已有待处理的时间核实"
            now = datetime.now()
            created = business.parse_dt(order["created_at"])
            if proposed > now or (created and proposed < created):
                return 400, "补报时间必须在下单之后且不能晚于现在"
            active = self._worker_has_open_session(conn, user["open_id"], order_id)
            if active:
                return 409, f"补报时间与进行中的工单 #{active['order_id']} 冲突"
            review_id = self._create_time_review(
                conn, order, user, "retro_start", proposed, reason=reason
            )
            business.add_event(
                conn, order_id, "补报开始待核实", user, "assigned", "assigned",
                f"申报 {business.format_dt(proposed)} · {reason}",
            )
            self._queue_time_review(conn, order, review_id, "补报开始待核实")
            conn.commit()
            return 200, None
        finally:
            conn.close()

    def complete(self, user, body):
        order_id = self._record_id(body.get("order_id"))
        note = (body.get("note") or "").strip()
        reason = (body.get("reason") or "").strip()
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
            if order["assignee_id"] != user["open_id"]:
                return 403, "只能完成自己负责的订单"
            if business.active_time_review(conn, order_id):
                return 409, "该订单已有待处理的时间核实"
            now = datetime.now()
            proposed = self._timestamp(body.get("ended_at")) or now
            started = business.parse_dt(order["started_at"])
            if not started or proposed < started or proposed > now:
                return 400, "结束时间必须在开始时间之后且不能晚于现在"
            scheduled_end = business.parse_dt(order["scheduled_end"])
            retroactive = abs((now - proposed).total_seconds()) > 120
            early = bool(scheduled_end and proposed < scheduled_end)
            if (early or retroactive) and not reason:
                return 400, "提前或补报结束必须填写原因"
            if early or retroactive:
                kind = "retro_end" if retroactive else "early_end"
                review_id = self._create_time_review(
                    conn, order, user, kind, proposed, note=note,
                    reason=reason,
                )
                conn.execute(
                    "UPDATE orders SET status='completion_review',version=version+1 WHERE id=? AND status='in_progress'",
                    (order_id,),
                )
                label = "补报结束待核实" if retroactive else "提前结束待核实"
                business.add_event(
                    conn, order_id, label, user, "in_progress", "completion_review",
                    f"申报 {business.format_dt(proposed)} · {reason}",
                )
                self._queue_time_review(conn, order, review_id, label)
                conn.commit()
                return 200, "review"
            stamp = business.format_dt(proposed)
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
            if order["creator_id"]:
                business.queue_notification(
                    conn, order["creator_id"],
                    f"跟播已结束：#{order_id} {order['team']} · {order['task_type']} · {stamp}；如时间不正确请在工单详情中反馈",
                    f"order:{order_id}:completed:creator:v{order['version']}",
                )
            if order["feishu_task_guid"]:
                business.queue_task_operation(
                    conn, "task_delete", "", {"guid": order["feishu_task_guid"], "order_id": order_id},
                    f"order:{order_id}:task-delete:completed",
                )
            conn.commit()
            return 200, None
        finally:
            conn.close()

    def dispute_completion(self, user, body):
        order_id = self._record_id(body.get("order_id"))
        reason = (body.get("reason") or "").strip()
        if not order_id:
            return 400, "非法工单编号"
        if not reason:
            return 400, "请说明结束时间有什么问题"
        conn = self.db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                return 404, "工单不存在"
            if user["role"] != "team" or int(order["team_id"] or 0) != business.bound_team_id(user):
                return 403, "只有该团队的账号可以提出异议"
            if order["status"] != "completed" or not order["done_at"]:
                return 409, "只有已完成订单可以提出结束异议"
            if business.active_time_review(conn, order_id):
                return 409, "该订单已有待处理的时间异议"
            proposed = business.parse_dt(order["done_at"])
            review_id = self._create_time_review(
                conn, order, user, "team_dispute", proposed, note=order["done_note"],
                reason=reason, original_at=order["done_at"],
            )
            business.add_event(
                conn, order_id, "团队提出结束异议", user, "completed", "completed", reason
            )
            self._queue_time_review(conn, order, review_id, "团队提出结束异议")
            conn.commit()
            return 200, None
        finally:
            conn.close()

    def resolve_time_review(self, user, body):
        review_id = self._record_id(body.get("review_id"))
        decision = (body.get("decision") or "").strip()
        resolution_note = (body.get("note") or "").strip()
        corrected = self._timestamp(body.get("corrected_at"))
        if not review_id:
            return 400, "非法核实编号"
        if decision not in ("approve", "reject", "reopen"):
            return 400, "处理结果无效"
        if not resolution_note:
            return 400, "处理时间核实必须填写说明"
        conn = self.db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            review = conn.execute("SELECT * FROM time_reviews WHERE id=?", (review_id,)).fetchone()
            if not review:
                return 404, "时间核实记录不存在"
            if review["status"] != "pending":
                return 409, "该时间核实已经处理"
            order = conn.execute("SELECT * FROM orders WHERE id=?", (review["order_id"],)).fetchone()
            if not order:
                return 404, "工单不存在"
            if not business.can_manage_order(user, order):
                return 403, "只有本组组长或管理员可以处理"
            if user["open_id"] in (review["requested_by"], review["worker_id"]):
                return 403, "不能审核自己发起或由自己执行的时间核实"
            stamp = business.now_iso()
            proposed = corrected or business.parse_dt(review["proposed_at"])

            if review["kind"] == "retro_start":
                if order["status"] != "assigned" or order["started_at"]:
                    return 409, "工单开始状态已经变化"
                if decision == "reopen":
                    return 400, "补报开始不支持重新打开"
                if decision == "approve":
                    created = business.parse_dt(order["created_at"])
                    if not proposed or proposed > datetime.now() or (created and proposed < created):
                        return 400, "核实后的开始时间无效"
                    worker = conn.execute(
                        "SELECT * FROM users WHERE open_id=?", (review["worker_id"],)
                    ).fetchone()
                    if not worker:
                        return 404, "负责人不存在"
                    block = business.assignment_block(
                        conn,
                        worker,
                        order,
                        ignore_order_id=order["id"],
                        require_available_now=True,
                    )
                    if block:
                        return 409, block
                    active = self._worker_has_open_session(conn, review["worker_id"], order["id"])
                    if active:
                        return 409, f"该人员已有进行中的工单 #{active['order_id']}"
                    conflict = self._session_overlap(
                        conn, review["worker_id"], proposed, datetime.now()
                    )
                    if conflict:
                        return 409, f"核实时间与工单 #{conflict['order_id']} 的工作时段重叠"
                    conn.execute(
                        "UPDATE orders SET status='in_progress',started_at=?,version=version+1 WHERE id=? AND status='assigned'",
                        (business.format_dt(proposed), order["id"]),
                    )
                    conn.execute(
                        "INSERT INTO work_sessions(order_id,worker_id,started_at) VALUES(?,?,?)",
                        (order["id"], review["worker_id"], business.format_dt(proposed)),
                    )
                    conn.execute(
                        "UPDATE users SET status='busy',task=? WHERE open_id=?",
                        (f"{order['team']} · {order['room']}", review["worker_id"]),
                    )
                    business.add_event(
                        conn, order["id"], "确认补报开始", user, "assigned", "in_progress",
                        f"{business.format_dt(proposed)} · {resolution_note}",
                    )
                else:
                    business.add_event(
                        conn, order["id"], "驳回补报开始", user, "assigned", "assigned", resolution_note
                    )

            elif review["kind"] in ("early_end", "retro_end"):
                if order["status"] != "completion_review":
                    return 409, "工单结束核实状态已经变化"
                if decision == "reopen":
                    decision = "reject"
                if decision == "approve":
                    session = self._open_session(conn, order["id"], review["worker_id"])
                    started = business.parse_dt(session["started_at"]) if session else None
                    if not session or not proposed or proposed < started or proposed > datetime.now():
                        return 400, "核实后的结束时间无效"
                    conflict = self._session_overlap(
                        conn, review["worker_id"], started, proposed, session["id"]
                    )
                    if conflict:
                        return 409, f"核实时间与工单 #{conflict['order_id']} 的工作时段重叠"
                    conn.execute(
                        "UPDATE orders SET status='completed',done_at=?,done_note=?,version=version+1 WHERE id=?",
                        (business.format_dt(proposed), review["note"][:300], order["id"]),
                    )
                    conn.execute(
                        "UPDATE work_sessions SET ended_at=?,end_reason='completed' WHERE id=?",
                        (business.format_dt(proposed), session["id"]),
                    )
                    business.close_active_transfers(conn, order["id"])
                    self._release_worker(conn, review["worker_id"])
                    business.add_event(
                        conn, order["id"], "确认结束时间", user, "completion_review", "completed",
                        f"{business.format_dt(proposed)} · {resolution_note}",
                    )
                    if order["feishu_task_guid"]:
                        business.queue_task_operation(
                            conn, "task_delete", "",
                            {"guid": order["feishu_task_guid"], "order_id": order["id"]},
                            f"order:{order['id']}:task-delete:review:{review_id}",
                        )
                else:
                    conn.execute(
                        "UPDATE orders SET status='in_progress',version=version+1 WHERE id=? AND status='completion_review'",
                        (order["id"],),
                    )
                    business.add_event(
                        conn, order["id"], "驳回结束申请", user, "completion_review", "in_progress",
                        resolution_note,
                    )
                    business.queue_notification(
                        conn, review["worker_id"],
                        f"结束申请被驳回：#{order['id']} {order['team']}；{resolution_note}",
                        f"time-review:{review_id}:rejected:worker",
                    )

            elif review["kind"] == "team_dispute":
                if order["status"] != "completed":
                    return 409, "工单完成状态已经变化"
                if decision == "reopen":
                    worker = conn.execute(
                        "SELECT * FROM users WHERE open_id=?", (review["worker_id"],)
                    ).fetchone()
                    if not worker:
                        return 404, "负责人不存在"
                    block = business.assignment_block(
                        conn,
                        worker,
                        order,
                        ignore_order_id=order["id"],
                        require_available_now=True,
                    )
                    if block:
                        return 409, block
                    active = self._worker_has_open_session(conn, review["worker_id"], order["id"])
                    if active:
                        return 409, f"负责人已经在执行工单 #{active['order_id']}，不能直接重新打开"
                    session = conn.execute(
                        "SELECT * FROM work_sessions WHERE order_id=? AND worker_id=? ORDER BY id DESC LIMIT 1",
                        (order["id"], review["worker_id"]),
                    ).fetchone()
                    if not session:
                        return 409, "找不到可以重新打开的工作时段"
                    started = business.parse_dt(session["started_at"])
                    conflict = self._session_overlap(
                        conn, review["worker_id"], started, datetime.now(), session["id"]
                    ) if started else None
                    if conflict:
                        return 409, f"重新打开会与工单 #{conflict['order_id']} 的工作时段重叠，请改为修正结束时间"
                    conn.execute(
                        "UPDATE work_sessions SET ended_at='',end_reason='' WHERE id=?", (session["id"],)
                    )
                    conn.execute(
                        "UPDATE orders SET status='in_progress',done_at='',done_note='',version=version+1 WHERE id=?",
                        (order["id"],),
                    )
                    conn.execute(
                        "UPDATE users SET status='busy',task=? WHERE open_id=?",
                        (f"{order['team']} · {order['room']}", review["worker_id"]),
                    )
                    business.add_event(
                        conn, order["id"], "根据团队异议重新打开", user, "completed", "in_progress",
                        resolution_note,
                    )
                    worker = conn.execute(
                        "SELECT * FROM users WHERE open_id=?", (review["worker_id"],)
                    ).fetchone()
                    if worker:
                        reopened = self._order_dict(
                            conn.execute("SELECT * FROM orders WHERE id=?", (order["id"],)).fetchone()
                        )
                        self._queue_assignment(conn, reopened, worker, "工单已重新打开")
                elif decision == "approve":
                    session = conn.execute(
                        "SELECT * FROM work_sessions WHERE order_id=? AND worker_id=? ORDER BY id DESC LIMIT 1",
                        (order["id"], review["worker_id"]),
                    ).fetchone()
                    started = business.parse_dt(session["started_at"]) if session else None
                    if not session or not proposed or proposed < started or proposed > datetime.now():
                        return 400, "修正后的结束时间无效"
                    conflict = self._session_overlap(
                        conn, review["worker_id"], started, proposed, session["id"]
                    )
                    if conflict:
                        return 409, f"修正时间与工单 #{conflict['order_id']} 的工作时段重叠"
                    conn.execute("UPDATE orders SET done_at=?,version=version+1 WHERE id=?", (business.format_dt(proposed), order["id"]))
                    conn.execute("UPDATE work_sessions SET ended_at=? WHERE id=?", (business.format_dt(proposed), session["id"]))
                    business.add_event(
                        conn, order["id"], "修正结束时间", user, "completed", "completed",
                        f"{review['original_at']} → {business.format_dt(proposed)} · {resolution_note}",
                    )
                else:
                    business.add_event(
                        conn, order["id"], "驳回团队异议", user, "completed", "completed", resolution_note
                    )

            review_status = "approved" if decision in ("approve", "reopen") else "rejected"
            changed = conn.execute(
                """UPDATE time_reviews SET status=?,resolved_by=?,resolved_name=?,resolved_at=?,
                   resolution_note=? WHERE id=? AND status='pending'""",
                (review_status, user["open_id"], user["name"], stamp, resolution_note[:500], review_id),
            ).rowcount
            if changed != 1:
                return 409, "该时间核实已被其他人处理"
            result_label = {
                "approve": "已确认",
                "reject": "已驳回",
                "reopen": "已重新打开",
            }[decision]
            recipients = {review["requested_by"], review["worker_id"], order["creator_id"]}
            for recipient in recipients:
                if recipient and recipient != user["open_id"]:
                    business.queue_notification(
                        conn,
                        recipient,
                        f"时间核实{result_label}：#{order['id']} {order['team']} · {resolution_note}",
                        f"time-review:{review_id}:resolved:{recipient}",
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
            """SELECT team,room FROM orders WHERE assignee_id=?
               AND status IN ('in_progress','completion_review') ORDER BY started_at,id LIMIT 1""",
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
                start, _ = business.order_bounds(order)
                require_now = bool(start and start <= datetime.now() + timedelta(minutes=15))
                block = business.assignment_block(
                    conn,
                    worker,
                    order,
                    ignore_order_id=order_id,
                    require_available_now=require_now,
                )
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
                if order["status"] in ("in_progress", "completion_review"):
                    return 409, "任务已经开始，请联系组长处理"
            elif not business.can_manage_order(user, order):
                return 403, "无权取消该订单"
            stamp = business.now_iso()
            old_status = order["status"]
            terminal_status = "abnormal" if old_status in ("in_progress", "completion_review") else "cancelled"
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
            conn.execute(
                """UPDATE time_reviews SET status='withdrawn',resolved_by=?,resolved_name=?,resolved_at=?,
                   resolution_note=? WHERE order_id=? AND status='pending'""",
                (user["open_id"], user["name"], stamp, reason[:500], order_id),
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
            start, _ = business.order_bounds(order)
            require_available_now = order["status"] == "in_progress" or bool(
                start and start <= datetime.now() + timedelta(minutes=15)
            )
            urgent_window = bool(start and start - datetime.now() <= timedelta(hours=2))
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
                block = business.assignment_block(
                    conn,
                    target,
                    order,
                    ignore_order_id=order_id,
                    require_available_now=require_available_now,
                )
                if block:
                    return 409, block
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
                leads = conn.execute(
                    "SELECT open_id FROM users WHERE role='admin' OR (role='staff' AND lead=1 AND grp=?)",
                    (order["grp"],),
                ).fetchall()
                for lead in leads:
                    if lead["open_id"] in (user["open_id"], target_id):
                        continue
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
            if user["open_id"] in (transfer["requester_id"], transfer["target_id"]):
                return 403, "转包的发起人和目标接手人不能审批自己的申请"
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
            start, _ = business.order_bounds(order)
            require_available_now = order["status"] == "in_progress" or bool(
                start and start <= datetime.now() + timedelta(minutes=15)
            )
            block = business.assignment_block(
                conn,
                worker,
                order,
                ignore_order_id=order["id"],
                require_available_now=require_available_now,
            )
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
