import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
