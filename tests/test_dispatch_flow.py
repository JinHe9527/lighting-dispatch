import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock


_TEMP_DIR = tempfile.TemporaryDirectory(prefix="dispatch-tests-")
os.environ["DISPATCH_TEST_MODE"] = "1"
os.environ["DISPATCH_DB_PATH"] = str(Path(_TEMP_DIR.name) / "dispatch-test.db")

from app import server  # noqa: E402


class DispatchFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.init_db()
        conn = server.db()
        conn.execute("UPDATE shifts SET start='05:00',end='04:59' WHERE name='中班'")
        conn.execute("UPDATE users SET shift='中班',approved=1,status='free' WHERE open_id LIKE 'test:%'")
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        _TEMP_DIR.cleanup()

    def setUp(self):
        conn = server.db()
        conn.execute("DELETE FROM work_sessions")
        conn.execute("DELETE FROM transfers")
        conn.execute("DELETE FROM order_events")
        conn.execute("DELETE FROM uploads")
        conn.execute("DELETE FROM orders")
        conn.execute("DELETE FROM notification_outbox")
        conn.execute("UPDATE users SET status='free',task='' WHERE open_id LIKE 'test:%'")
        conn.commit()
        conn.close()
        self.admin = self.user("test:admin")
        self.team = self.user("test:team")
        self.staff = self.user("test:staff")
        self.lead = self.user("test:lead")
        conn = server.db()
        self.team_row = dict(conn.execute("SELECT * FROM teams ORDER BY id LIMIT 1").fetchone())
        self.task = conn.execute(
            "SELECT task_type FROM sla WHERE grp='灯光组' AND task_type!='编歌' ORDER BY id LIMIT 1"
        ).fetchone()["task_type"]
        conn.close()

    @staticmethod
    def user(open_id):
        conn = server.db()
        row = dict(conn.execute("SELECT * FROM users WHERE open_id=?", (open_id,)).fetchone())
        conn.close()
        return row

    def order_body(self, hour=15, creator="team"):
        date = (datetime.now().date() + timedelta(days=1)).isoformat()
        body = {
            "team": self.team_row["name"],
            "group": "灯光组",
            "task": self.task,
            "when": "book",
            "date": date,
            "at": f"{hour:02d}:00",
            "end": f"{hour + 1:02d}:00",
        }
        if creator == "admin":
            body["team_id"] = self.team_row["id"]
        return body

    def create_running_order(self):
        body = {
            "team_id": self.team_row["id"],
            "group": "灯光组",
            "task": self.task,
            "when": "now",
        }
        self.assertEqual(server.SERVICE.order(self.admin, body)[0], 200)
        order = self.latest_order()
        self.assertEqual(
            server.SERVICE.assign(
                self.lead, {"order_id": order["id"], "user_id": self.staff["open_id"]}
            )[0],
            200,
        )
        self.assertEqual(server.SERVICE.start(self.staff, {"order_id": order["id"]})[0], 200)
        return self.latest_order()

    @staticmethod
    def latest_order():
        conn = server.db()
        row = dict(conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 1").fetchone())
        conn.close()
        return row

    def test_team_binding_choice_and_reservation_caps(self):
        body = self.order_body()
        body["assignee"] = self.staff["open_id"]
        code, message = server.SERVICE.order(self.team, body)
        self.assertEqual((code, message), (403, "普通下单人员不能指定工作人员"))

        self.assertEqual(server.SERVICE.order(self.team, self.order_body(15))[0], 200)
        self.assertEqual(server.SERVICE.order(self.team, self.order_body(16))[0], 200)
        code, message = server.SERVICE.order(self.team, self.order_body(17))
        self.assertEqual(code, 409)
        self.assertIn("上限", message)

    def test_assignment_conflict_and_early_start(self):
        self.assertEqual(server.SERVICE.order(self.admin, self.order_body(15, "admin"))[0], 200)
        first = self.latest_order()
        self.assertEqual(
            server.SERVICE.assign(self.lead, {"order_id": first["id"], "user_id": self.staff["open_id"]})[0],
            200,
        )
        self.assertEqual(server.SERVICE.order(self.admin, self.order_body(15, "admin"))[0], 200)
        second = self.latest_order()
        code, message = server.SERVICE.assign(
            self.lead, {"order_id": second["id"], "user_id": self.staff["open_id"]}
        )
        self.assertEqual(code, 409)
        self.assertIn("时间冲突", message)
        code, message = server.SERVICE.start(self.staff, {"order_id": first["id"]})
        self.assertEqual(code, 409)
        self.assertIn("提前", message)

    def test_state_machine_and_transfer_handoff(self):
        body = {
            "team_id": self.team_row["id"],
            "group": "灯光组",
            "task": self.task,
            "when": "now",
        }
        self.assertEqual(server.SERVICE.order(self.admin, body)[0], 200)
        order = self.latest_order()
        self.assertEqual(
            server.SERVICE.assign(self.lead, {"order_id": order["id"], "user_id": self.staff["open_id"]})[0],
            200,
        )
        self.assertEqual(server.SERVICE.complete(self.staff, {"order_id": order["id"], "note": ""})[0], 400)
        self.assertEqual(server.SERVICE.start(self.staff, {"order_id": order["id"]})[0], 200)
        self.assertEqual(server.SERVICE.start(self.staff, {"order_id": order["id"]})[0], 409)
        self.assertEqual(
            server.SERVICE.request_transfer(
                self.staff,
                {"order_id": order["id"], "reason_code": "临时生病", "target_id": self.lead["open_id"]},
            )[0],
            200,
        )
        conn = server.db()
        transfer = dict(conn.execute("SELECT * FROM transfers ORDER BY id DESC LIMIT 1").fetchone())
        conn.close()
        self.assertEqual(transfer["status"], "awaiting_lead")
        self.assertEqual(server.SERVICE.approve_transfer(self.admin, {"transfer_id": transfer["id"]})[0], 200)
        self.assertEqual(server.SERVICE.accept_transfer(self.lead, {"transfer_id": transfer["id"]})[0], 200)
        updated = self.latest_order()
        self.assertEqual(updated["assignee_id"], self.lead["open_id"])
        self.assertEqual(updated["status"], "in_progress")
        code, message = server.SERVICE.request_transfer(
            self.lead,
            {"order_id": order["id"], "reason_code": "排班临时冲突", "target_id": self.staff["open_id"]},
        )
        self.assertEqual(code, 409)
        self.assertIn("历史负责人", message)
        self.assertEqual(server.SERVICE.complete(self.lead, {"order_id": order["id"], "note": "任务完成"})[0], 200)
        self.assertEqual(self.latest_order()["status"], "completed")
        conn = server.db()
        sessions = conn.execute("SELECT * FROM work_sessions WHERE order_id=? ORDER BY id", (order["id"],)).fetchall()
        conn.close()
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0]["end_reason"], "transferred")
        self.assertEqual(sessions[1]["end_reason"], "completed")

    def test_repeated_transfers_require_lead_approval(self):
        for index, hour in enumerate((15, 16, 17)):
            self.assertEqual(server.SERVICE.order(self.admin, self.order_body(hour, "admin"))[0], 200)
            order = self.latest_order()
            self.assertEqual(
                server.SERVICE.assign(self.lead, {"order_id": order["id"], "user_id": self.staff["open_id"]})[0],
                200,
            )
            code, message = server.SERVICE.request_transfer(
                self.staff, {"order_id": order["id"], "reason_code": "家庭急事"}
            )
            self.assertEqual((code, message), (200, None))
            conn = server.db()
            transfer = dict(conn.execute("SELECT * FROM transfers ORDER BY id DESC LIMIT 1").fetchone())
            conn.close()
            if index == 0:
                self.assertEqual(transfer["status"], "open")
            else:
                self.assertEqual(transfer["status"], "awaiting_lead")
            if index < 2:
                self.assertEqual(
                    server.SERVICE.withdraw_transfer(self.staff, {"transfer_id": transfer["id"]})[0], 200
                )

    def test_team_scope_hides_people_and_other_orders(self):
        self.assertEqual(server.SERVICE.order(self.team, self.order_body(15))[0], 200)
        state = server.api_state(self.team)
        self.assertEqual(state["staff"], [])
        self.assertEqual(len(state["teams"]), 1)
        self.assertTrue(all(order["team_id"] == self.team_row["id"] for order in state["orders"]))
        code, stats = server.api_stats(self.team, {"range": "30d"})
        self.assertEqual(code, 200)
        self.assertEqual(stats["byPerson"], [])
        self.assertEqual(stats["summary"]["people"], 0)

    def test_running_transfer_requires_currently_available_target(self):
        order = self.create_running_order()
        conn = server.db()
        conn.execute("UPDATE users SET status='off' WHERE open_id=?", (self.lead["open_id"],))
        conn.commit()
        conn.close()
        code, message = server.SERVICE.request_transfer(
            self.staff,
            {"order_id": order["id"], "reason_code": "临时生病", "target_id": self.lead["open_id"]},
        )
        self.assertEqual(code, 409)
        self.assertIn("下班", message)

    def test_transfer_rechecks_target_status_when_accepting(self):
        order = self.create_running_order()
        self.assertEqual(
            server.SERVICE.request_transfer(
                self.staff,
                {"order_id": order["id"], "reason_code": "家庭急事", "target_id": self.lead["open_id"]},
            )[0],
            200,
        )
        conn = server.db()
        transfer = dict(conn.execute("SELECT * FROM transfers ORDER BY id DESC LIMIT 1").fetchone())
        conn.close()
        self.assertEqual(server.SERVICE.approve_transfer(self.admin, {"transfer_id": transfer["id"]})[0], 200)
        conn = server.db()
        conn.execute("UPDATE users SET status='rest' WHERE open_id=?", (self.lead["open_id"],))
        conn.commit()
        conn.close()
        code, message = server.SERVICE.accept_transfer(self.lead, {"transfer_id": transfer["id"]})
        self.assertEqual(code, 409)
        self.assertIn("小休", message)
        self.assertEqual(self.latest_order()["assignee_id"], self.staff["open_id"])

    def test_admin_cannot_break_active_worker_status(self):
        self.assertEqual(
            server.do_save(
                self.admin,
                {"entity": "users", "record": {"open_id": self.staff["open_id"], "status": ""}},
            )[0],
            400,
        )
        self.create_running_order()
        code, message = server.do_save(
            self.admin,
            {"entity": "users", "record": {"open_id": self.staff["open_id"], "status": "check"}},
        )
        self.assertEqual(code, 409)
        self.assertIn("忙碌", message)
        self.assertIn('status:"free"', (Path(server.ROOT) / "index.html").read_text(encoding="utf-8"))

    def test_active_task_rule_cannot_be_renamed_or_moved(self):
        self.assertEqual(server.SERVICE.order(self.admin, self.order_body(15, "admin"))[0], 200)
        conn = server.db()
        rule = dict(
            conn.execute(
                "SELECT * FROM sla WHERE grp='灯光组' AND task_type=?", (self.task,)
            ).fetchone()
        )
        conn.close()
        code, message = server.do_save(
            self.admin,
            {"entity": "sla", "record": {"id": rule["id"], "task_type": self.task + "改名"}},
        )
        self.assertEqual(code, 409)
        self.assertIn("未结束订单", message)

    def test_shift_change_cannot_invalidate_assigned_order(self):
        self.assertEqual(server.SERVICE.order(self.admin, self.order_body(15, "admin"))[0], 200)
        order = self.latest_order()
        self.assertEqual(
            server.SERVICE.assign(
                self.lead, {"order_id": order["id"], "user_id": self.staff["open_id"]}
            )[0],
            200,
        )
        conn = server.db()
        shift = dict(conn.execute("SELECT * FROM shifts WHERE name='中班'").fetchone())
        conn.close()
        code, message = server.do_save(
            self.admin,
            {"entity": "shifts", "record": {"id": shift["id"], "start": "16:00"}},
        )
        self.assertEqual(code, 409)
        self.assertIn(f"#{order['id']}", message)

    def test_stale_outbox_is_recovered(self):
        conn = server.db()
        conn.execute(
            """INSERT INTO notification_outbox(
                 kind,recipient_id,payload,dedupe_key,status,attempts,available_at,claimed_at,created_at
               ) VALUES('message',?,?,?,'sending',0,?,?,?)""",
            (
                self.staff["open_id"],
                json.dumps({"text": "恢复测试"}, ensure_ascii=False),
                "outbox:stale",
                server.now_iso(),
                server.business.format_dt(datetime.now() - timedelta(minutes=20)),
                server.now_iso(),
            ),
        )
        conn.commit()
        conn.close()
        server.process_outbox_once()
        conn = server.db()
        row = conn.execute("SELECT * FROM notification_outbox WHERE dedupe_key='outbox:stale'").fetchone()
        conn.close()
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["claimed_at"], "")

    def test_message_retry_continues_from_urgent_stage(self):
        conn = server.db()
        conn.execute(
            """INSERT INTO notification_outbox(
                 kind,recipient_id,payload,dedupe_key,status,attempts,available_at,created_at
               ) VALUES('message',?,?,?,'sending',0,?,?)""",
            (
                self.staff["open_id"],
                json.dumps({"text": "加急测试", "urgent": True}, ensure_ascii=False),
                "message:urgent:retry",
                server.now_iso(),
                server.now_iso(),
            ),
        )
        conn.commit()
        row = dict(conn.execute("SELECT * FROM notification_outbox WHERE dedupe_key='message:urgent:retry'").fetchone())
        conn.close()
        calls = []

        def first_call(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if method == "POST":
                return {"code": 0, "data": {"message_id": "message-1"}}
            return {"code": 1, "msg": "加急暂时失败"}

        with mock.patch.object(server, "feishu_api", side_effect=first_call), \
                mock.patch.object(server, "tenant_token", return_value="token"), \
                mock.patch.object(server, "setting", return_value="1"):
            with self.assertRaises(RuntimeError):
                server._send_message_now(row, json.loads(row["payload"]))
        self.assertEqual(calls[0][2]["body"]["uuid"], "message:urgent:retry")

        conn = server.db()
        retry_row = dict(conn.execute("SELECT * FROM notification_outbox WHERE id=?", (row["id"],)).fetchone())
        conn.close()
        self.assertEqual(retry_row["progress"], "message_sent")
        retry_calls = []

        def retry_call(method, path, **kwargs):
            retry_calls.append((method, path))
            return {"code": 0}

        with mock.patch.object(server, "feishu_api", side_effect=retry_call), \
                mock.patch.object(server, "tenant_token", return_value="token"), \
                mock.patch.object(server, "setting", return_value="1"):
            server._send_message_now(retry_row, json.loads(retry_row["payload"]))
        self.assertEqual(retry_calls, [("PATCH", "/im/v1/messages/message-1/urgent_app")])

    def test_task_retry_does_not_create_second_task(self):
        self.assertEqual(server.SERVICE.order(self.admin, self.order_body(15, "admin"))[0], 200)
        order = self.latest_order()
        self.assertEqual(
            server.SERVICE.assign(
                self.lead, {"order_id": order["id"], "user_id": self.staff["open_id"]}
            )[0],
            200,
        )
        payload = {
            "order_id": order["id"],
            "summary": "任务重试测试",
            "description": "只应创建一次",
            "date": order["date"],
            "arrive": order["arrive"],
        }
        conn = server.db()
        conn.execute(
            """INSERT INTO notification_outbox(
                 kind,recipient_id,payload,dedupe_key,status,attempts,available_at,created_at
               ) VALUES('task_create',?,?,?,'sending',0,?,?)""",
            (
                self.staff["open_id"],
                json.dumps(payload, ensure_ascii=False),
                "task:create:retry",
                server.now_iso(),
                server.now_iso(),
            ),
        )
        conn.commit()
        row = dict(conn.execute("SELECT * FROM notification_outbox WHERE dedupe_key='task:create:retry'").fetchone())
        conn.close()

        def first_call(method, path, **kwargs):
            if path == "/task/v2/tasks":
                return {"code": 0, "data": {"task": {"guid": "task-1"}}}
            return {"code": 1, "msg": "成员设置暂时失败"}

        with mock.patch.object(server, "feishu_api", side_effect=first_call), \
                mock.patch.object(server, "tenant_token", return_value="token"):
            with self.assertRaises(RuntimeError):
                server._create_task_now(row, payload)

        conn = server.db()
        retry_row = dict(conn.execute("SELECT * FROM notification_outbox WHERE id=?", (row["id"],)).fetchone())
        conn.close()
        self.assertEqual(retry_row["progress"], "task_created")
        paths = []

        def retry_call(method, path, **kwargs):
            paths.append(path)
            return {"code": 0}

        with mock.patch.object(server, "feishu_api", side_effect=retry_call), \
                mock.patch.object(server, "tenant_token", return_value="token"):
            server._create_task_now(retry_row, payload)
        self.assertEqual(paths, ["/task/v2/tasks/task-1/add_members"])
        self.assertEqual(self.latest_order()["feishu_task_guid"], "task-1")

    def test_upload_reservation_enforces_pending_file_limit(self):
        conn = server.db()
        for index in range(server.MEDIA_PENDING_FILE_LIMIT):
            conn.execute(
                """INSERT INTO uploads(token,path,kind,owner_id,status,size,created_at)
                   VALUES(?,?,? ,?,'pending',?,?)""",
                (
                    f"existing-{index}",
                    f"/media/existing-{index}.mp4",
                    "video",
                    self.team["open_id"],
                    1024,
                    server.now_iso(),
                ),
            )
        conn.commit()
        conn.close()
        code, message = server.reserve_upload(
            self.team["open_id"], "blocked-upload", "/media/blocked.mp4", "video", 1024
        )
        self.assertEqual(code, 429)
        self.assertIn("未使用附件", message)
        conn = server.db()
        exists = conn.execute("SELECT 1 FROM uploads WHERE token='blocked-upload'").fetchone()
        conn.close()
        self.assertIsNone(exists)


if __name__ == "__main__":
    unittest.main()
