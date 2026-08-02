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
        conn.execute("DELETE FROM time_reviews")
        conn.execute("DELETE FROM work_sessions")
        conn.execute("DELETE FROM transfers")
        conn.execute("DELETE FROM order_events")
        conn.execute("DELETE FROM uploads")
        conn.execute("DELETE FROM orders")
        conn.execute("DELETE FROM notification_outbox")
        conn.execute("DELETE FROM staff_shift_overrides")
        conn.execute("UPDATE teams SET lighting_owner_id=''")
        conn.execute("UPDATE groups SET auto=0")
        conn.execute(
            """UPDATE users SET status='free',task='',approved=1,blocked=0,read_only=0,
                      absence_type='',absence_start='',absence_end='',team_id=NULL
               WHERE open_id LIKE 'test:%'"""
        )
        for profile in server.TEST_PROFILES.values():
            conn.execute(
                """UPDATE users SET role=?,grp=?,lead=?,intern=0
                   WHERE open_id=?""",
                (
                    profile["role"],
                    profile.get("grp", ""),
                    profile.get("lead", 0),
                    profile["open_id"],
                ),
            )
        conn.execute(
            """UPDATE users SET team_id=(SELECT id FROM teams ORDER BY id LIMIT 1)
               WHERE open_id='test:team'"""
        )
        conn.execute("DELETE FROM sessions WHERE open_id IN ('test:outside','test:other-group')")
        conn.execute("DELETE FROM users WHERE open_id IN ('test:outside','test:other-group')")
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

    def test_account_admission_blocking_and_session_revocation(self):
        conn = server.db()
        self.assertEqual(
            conn.execute("SELECT v FROM settings WHERE k='gate_mode'").fetchone()["v"],
            "manual",
        )
        conn.execute(
            """INSERT INTO users(open_id,name,role,approved,blocked,read_only,created_at,last_login)
               VALUES('test:outside','成都分公司账号','team',0,0,0,?,?)""",
            (server.now_iso(), server.now_iso()),
        )
        conn.commit()
        outside = dict(conn.execute("SELECT * FROM users WHERE open_id='test:outside'").fetchone())
        conn.close()

        self.assertIsNone(server.apply_login_gate(outside["open_id"], outside["name"]))
        self.assertFalse(server.Handler._approved(outside))
        code, message = server.do_save(
            self.admin,
            {"entity": "users", "record": {"open_id": outside["open_id"], "approved": "1"}},
        )
        self.assertEqual(code, 400)
        self.assertIn("绑定直播团队", message)
        self.assertEqual(
            server.do_save(
                self.admin,
                {
                    "entity": "users",
                    "record": {
                        "open_id": outside["open_id"],
                        "approved": "1",
                        "team_id": self.team_row["id"],
                    },
                },
            ),
            (200, None),
        )
        sid = server.new_session(outside["open_id"])
        self.assertIsNotNone(server.session_user(sid))
        self.assertEqual(
            server.do_save(
                self.admin,
                {"entity": "users", "record": {"open_id": outside["open_id"], "blocked": "1"}},
            ),
            (200, None),
        )
        self.assertIsNone(server.session_user(sid))
        blocked = self.user(outside["open_id"])
        self.assertFalse(server.Handler._approved(blocked))
        self.assertIn("未获准使用", server.apply_login_gate(blocked["open_id"], blocked["name"]))

    def test_account_usage_identity_and_scope_are_saved_atomically(self):
        self.assertEqual(
            server.do_save(
                self.admin,
                {
                    "entity": "users",
                    "record": {
                        "open_id": self.staff["open_id"],
                        "role": "team",
                        "team_id": self.team_row["id"],
                        "approved": "1",
                        "blocked": "0",
                    },
                },
            ),
            (200, None),
        )
        team_account = self.user(self.staff["open_id"])
        self.assertEqual(team_account["role"], "team")
        self.assertEqual(team_account["team_id"], self.team_row["id"])
        self.assertEqual(team_account["grp"], "")
        self.assertEqual(team_account["lead"], 0)
        self.assertEqual(team_account["intern"], 0)
        self.assertEqual(team_account["read_only"], 0)
        self.assertEqual(team_account["status"], "off")

        self.assertEqual(
            server.do_save(
                self.admin,
                {
                    "entity": "users",
                    "record": {
                        "open_id": self.staff["open_id"],
                        "role": "staff",
                        "grp": "灯光组",
                        "lead": "1",
                        "intern": "0",
                        "approved": "1",
                    },
                },
            ),
            (200, None),
        )
        lead_account = self.user(self.staff["open_id"])
        self.assertEqual(lead_account["role"], "staff")
        self.assertIsNone(lead_account["team_id"])
        self.assertEqual(lead_account["grp"], "灯光组")
        self.assertEqual(lead_account["lead"], 1)
        self.assertEqual(lead_account["read_only"], 0)

    def test_lighting_owner_is_preferred_but_never_restricts_managers(self):
        self.assertEqual(
            server.do_save(
                self.admin,
                {
                    "entity": "teams",
                    "record": {
                        "id": self.team_row["id"],
                        "lighting_owner_id": self.staff["open_id"],
                    },
                },
            ),
            (200, None),
        )
        conn = server.db()
        conn.execute("UPDATE groups SET auto=1 WHERE name='灯光组'")
        conn.commit()
        conn.close()

        self.assertEqual(server.SERVICE.order(self.admin, self.order_body(15, "admin")), (200, None))
        self.assertEqual(self.latest_order()["assignee_id"], self.staff["open_id"])

        body = self.order_body(17, "admin")
        body["assignee"] = self.lead["open_id"]
        self.assertEqual(server.SERVICE.order(self.admin, body), (200, None))
        self.assertEqual(self.latest_order()["assignee_id"], self.lead["open_id"])

    def test_unavailable_lighting_owner_falls_back_and_invalid_owner_is_cleared(self):
        self.assertEqual(
            server.do_save(
                self.admin,
                {
                    "entity": "teams",
                    "record": {
                        "id": self.team_row["id"],
                        "lighting_owner_id": self.staff["open_id"],
                    },
                },
            ),
            (200, None),
        )
        work_date = self.order_body(18, "admin")["date"]
        conn = server.db()
        conn.execute("UPDATE groups SET auto=1 WHERE name='灯光组'")
        conn.commit()
        conn.close()
        self.assertEqual(
            server.do_schedule_adjust(
                self.admin,
                {
                    "date": work_date,
                    "changes": [{"worker_id": self.staff["open_id"], "mode": "off"}],
                },
            ),
            (200, None),
        )

        self.assertEqual(server.SERVICE.order(self.admin, self.order_body(18, "admin")), (200, None))
        self.assertEqual(self.latest_order()["assignee_id"], self.lead["open_id"])

        code, message = server.do_save(
            self.admin,
            {
                "entity": "teams",
                "record": {
                    "id": self.team_row["id"],
                    "lighting_owner_id": self.admin["open_id"],
                },
            },
        )
        self.assertEqual(code, 400)
        self.assertIn("灯光组正式成员", message)

        self.assertEqual(
            server.do_save(
                self.admin,
                {
                    "entity": "users",
                    "record": {
                        "open_id": self.staff["open_id"],
                        "role": "team",
                        "team_id": self.team_row["id"],
                    },
                },
            ),
            (200, None),
        )
        conn = server.db()
        owner_id = conn.execute(
            "SELECT lighting_owner_id FROM teams WHERE id=?", (self.team_row["id"],)
        ).fetchone()["lighting_owner_id"]
        conn.close()
        self.assertEqual(owner_id, "")

    def test_init_db_normalizes_legacy_account_scope_fields(self):
        conn = server.db()
        conn.executemany(
            """INSERT INTO users(
                   open_id,name,role,grp,lead,intern,team_id,approved,blocked,
                   read_only,status,task,created_at,last_login
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                (
                    "legacy:admin", "旧管理员", "admin", "视觉组", 1, 1,
                    self.team_row["id"], 1, 0, 0, "free", "旧任务",
                    server.now_iso(), server.now_iso(),
                ),
                (
                    "legacy:team", "旧团队账号", "team", "灯光组", 1, 1,
                    None, 1, 0, 1, "free", "旧任务",
                    server.now_iso(), server.now_iso(),
                ),
                (
                    "legacy:staff", "旧工作人员", "staff", "灯光组", 0, 0,
                    self.team_row["id"], 1, 0, 1, "free", "",
                    server.now_iso(), server.now_iso(),
                ),
            ),
        )
        conn.commit()
        conn.close()

        server.init_db()

        admin = self.user("legacy:admin")
        self.assertEqual(admin["grp"], "")
        self.assertEqual(admin["lead"], 0)
        self.assertEqual(admin["intern"], 0)
        self.assertIsNone(admin["team_id"])
        self.assertEqual(admin["status"], "off")
        self.assertEqual(admin["task"], "")

        team = self.user("legacy:team")
        self.assertEqual(team["grp"], "")
        self.assertEqual(team["lead"], 0)
        self.assertEqual(team["intern"], 0)
        self.assertEqual(team["read_only"], 0)
        self.assertEqual(team["status"], "off")
        self.assertEqual(team["task"], "")
        self.assertEqual(team["approved"], 0)

        staff = self.user("legacy:staff")
        self.assertIsNone(staff["team_id"])
        self.assertEqual(staff["read_only"], 0)
        conn = server.db()
        conn.execute("DELETE FROM users WHERE open_id LIKE 'legacy:%'")
        conn.commit()
        conn.close()

    def test_management_read_only_sees_admin_state_without_write_permission(self):
        self.assertEqual(
            server.do_save(
                self.admin,
                {
                    "entity": "users",
                    "record": {
                        "open_id": self.team["open_id"],
                        "role": "admin",
                        "read_only": "1",
                        "approved": "1",
                    },
                },
            ),
            (200, None),
        )
        manager = self.user(self.team["open_id"])
        state = server.api_state(manager)
        self.assertEqual(state["me"]["read_only"], 1)
        self.assertIn("users", state)
        self.assertTrue(server.Handler._approved(manager))
        self.assertFalse(server.Handler._can_write(manager))
        self.assertTrue(server.Handler._can_write(self.admin))

    def test_business_trip_blocks_assignment_and_conflicting_trip_is_rejected(self):
        trip_date = (datetime.now().date() + timedelta(days=1)).isoformat()
        self.assertEqual(
            server.do_save(
                self.admin,
                {
                    "entity": "users",
                    "record": {
                        "open_id": self.staff["open_id"],
                        "absence_type": "travel",
                        "absence_start": trip_date,
                        "absence_end": trip_date,
                    },
                },
            ),
            (200, None),
        )
        state = server.api_state(self.admin)
        staff = next(item for item in state["staff"] if item["open_id"] == self.staff["open_id"])
        self.assertEqual(staff["absence_type"], "travel")

        self.assertEqual(server.SERVICE.order(self.admin, self.order_body(15, "admin"))[0], 200)
        order = self.latest_order()
        code, message = server.SERVICE.assign(
            self.lead, {"order_id": order["id"], "user_id": self.staff["open_id"]}
        )
        self.assertEqual(code, 409)
        self.assertIn("出差", message)

        self.assertEqual(
            server.do_save(
                self.admin,
                {
                    "entity": "users",
                    "record": {
                        "open_id": self.staff["open_id"],
                        "absence_type": "",
                        "absence_start": "",
                        "absence_end": "",
                    },
                },
            ),
            (200, None),
        )
        self.assertEqual(
            server.SERVICE.assign(
                self.lead, {"order_id": order["id"], "user_id": self.staff["open_id"]}
            )[0],
            200,
        )
        code, message = server.do_save(
            self.admin,
            {
                "entity": "users",
                "record": {
                    "open_id": self.staff["open_id"],
                    "absence_type": "travel",
                    "absence_start": trip_date,
                    "absence_end": trip_date,
                },
            },
        )
        self.assertEqual(code, 409)
        self.assertIn("先改派", message)

    def test_daily_shift_override_controls_assignment_and_can_restore_default(self):
        work_date = (datetime.now().date() + timedelta(days=1)).isoformat()
        conn = server.db()
        early_shift = dict(conn.execute("SELECT * FROM shifts WHERE name='早班'").fetchone())
        conn.close()
        self.assertEqual(
            server.do_schedule_adjust(
                self.lead,
                {
                    "date": work_date,
                    "changes": [{
                        "worker_id": self.staff["open_id"],
                        "mode": "shift",
                        "shift_id": early_shift["id"],
                    }],
                },
            ),
            (200, None),
        )
        self.assertEqual(server.SERVICE.order(self.admin, self.order_body(20, "admin"))[0], 200)
        order = self.latest_order()
        code, message = server.SERVICE.assign(
            self.lead, {"order_id": order["id"], "user_id": self.staff["open_id"]}
        )
        self.assertEqual(code, 409)
        self.assertIn("超出", message)

        self.assertEqual(
            server.do_schedule_adjust(
                self.lead,
                {
                    "date": work_date,
                    "changes": [{"worker_id": self.staff["open_id"], "mode": "default"}],
                },
            ),
            (200, None),
        )
        self.assertEqual(
            server.SERVICE.assign(
                self.lead, {"order_id": order["id"], "user_id": self.staff["open_id"]}
            )[0],
            200,
        )
        schedule = server.api_schedule(
            self.lead, {"range": "day", "date": work_date}
        )[1]
        assignment = next(
            item for item in schedule["assignments"]
            if item["worker_id"] == self.staff["open_id"]
        )
        self.assertEqual(assignment["source"], "default")
        self.assertEqual(assignment["name"], "中班")

    def test_daily_off_blocks_assignment_and_existing_work_blocks_schedule_change(self):
        work_date = (datetime.now().date() + timedelta(days=1)).isoformat()
        self.assertEqual(
            server.do_schedule_adjust(
                self.lead,
                {
                    "date": work_date,
                    "changes": [{"worker_id": self.staff["open_id"], "mode": "off"}],
                },
            ),
            (200, None),
        )
        self.assertEqual(server.SERVICE.order(self.admin, self.order_body(15, "admin"))[0], 200)
        order = self.latest_order()
        code, message = server.SERVICE.assign(
            self.lead, {"order_id": order["id"], "user_id": self.staff["open_id"]}
        )
        self.assertEqual(code, 409)
        self.assertIn("排休", message)

        self.assertEqual(
            server.do_schedule_adjust(
                self.lead,
                {
                    "date": work_date,
                    "changes": [{"worker_id": self.staff["open_id"], "mode": "default"}],
                },
            ),
            (200, None),
        )
        self.assertEqual(
            server.SERVICE.assign(
                self.lead, {"order_id": order["id"], "user_id": self.staff["open_id"]}
            )[0],
            200,
        )
        code, message = server.do_schedule_adjust(
            self.lead,
            {
                "date": work_date,
                "changes": [{"worker_id": self.staff["open_id"], "mode": "off"}],
            },
        )
        self.assertEqual(code, 409)
        self.assertIn("先改派", message)

    def test_schedule_adjustment_enforces_manager_scope_and_self_protection(self):
        work_date = (datetime.now().date() + timedelta(days=1)).isoformat()
        code, message = server.do_schedule_adjust(
            self.staff,
            {
                "date": work_date,
                "changes": [{"worker_id": self.lead["open_id"], "mode": "off"}],
            },
        )
        self.assertEqual(code, 403)
        self.assertIn("仅管理员或组长", message)
        code, message = server.do_schedule_adjust(
            self.lead,
            {
                "date": work_date,
                "changes": [{"worker_id": self.lead["open_id"], "mode": "off"}],
            },
        )
        self.assertEqual(code, 403)
        self.assertIn("不能调整自己的排班", message)

    def test_three_day_schedule_returns_continuous_dates(self):
        start = (datetime.now().date() + timedelta(days=8)).isoformat()
        code, schedule = server.api_schedule(
            self.lead, {"range": "three", "date": start}
        )
        self.assertEqual(code, 200)
        self.assertEqual(schedule["range"], "three")
        self.assertEqual(schedule["from"], start)
        self.assertEqual(
            schedule["to"],
            (datetime.fromisoformat(start).date() + timedelta(days=2)).isoformat(),
        )
        worker_dates = {
            item["date"]
            for item in schedule["assignments"]
            if item["worker_id"] == self.staff["open_id"]
        }
        self.assertEqual(
            worker_dates,
            {
                start,
                (datetime.fromisoformat(start).date() + timedelta(days=1)).isoformat(),
                (datetime.fromisoformat(start).date() + timedelta(days=2)).isoformat(),
            },
        )

    def test_staff_state_only_contains_own_group_shift_overrides(self):
        work_date = (datetime.now().date() + timedelta(days=1)).isoformat()
        conn = server.db()
        conn.execute(
            """INSERT INTO users(
                 open_id,name,role,grp,lead,intern,approved,blocked,shift,status,created_at,last_login
               ) VALUES(?,?,?,?,0,0,1,0,'中班','free',?,?)""",
            (
                "test:other-group",
                "其他组成员",
                "staff",
                "设备组",
                server.now_iso(),
                server.now_iso(),
            ),
        )
        conn.commit()
        conn.close()
        self.assertEqual(
            server.do_schedule_adjust(
                self.admin,
                {
                    "date": work_date,
                    "changes": [{"worker_id": "test:other-group", "mode": "off"}],
                },
            ),
            (200, None),
        )

        admin_state = server.api_state(self.admin)
        staff_state = server.api_state(self.staff)
        self.assertTrue(
            any(item["worker_id"] == "test:other-group" for item in admin_state["shift_overrides"])
        )
        self.assertFalse(
            any(item["worker_id"] == "test:other-group" for item in staff_state["shift_overrides"])
        )

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

    @staticmethod
    def latest_review():
        conn = server.db()
        row = dict(conn.execute("SELECT * FROM time_reviews ORDER BY id DESC LIMIT 1").fetchone())
        conn.close()
        return row

    def test_team_binding_choice_and_reservation_caps(self):
        body = self.order_body()
        body["assignee"] = self.staff["open_id"]
        code, message = server.SERVICE.order(self.team, body)
        self.assertEqual((code, message), (403, "普通下单人员不能指定工作人员"))

        team_order = self.order_body(15)
        team_order["urgent"] = "1"
        self.assertEqual(server.SERVICE.order(self.team, team_order)[0], 200)
        self.assertEqual(self.latest_order()["urgent"], 0)
        self.assertEqual(server.SERVICE.order(self.team, self.order_body(16))[0], 200)
        code, message = server.SERVICE.order(self.team, self.order_body(17))
        self.assertEqual(code, 409)
        self.assertIn("上限", message)

    def test_staff_order_is_always_assigned_to_self(self):
        body = self.order_body(15, "staff")
        self.assertEqual(server.SERVICE.order(self.staff, body), (200, None))
        order = self.latest_order()
        self.assertEqual(order["creator_id"], self.staff["open_id"])
        self.assertEqual(order["assignee_id"], self.staff["open_id"])
        self.assertEqual(order["status"], "assigned")

        other_body = self.order_body(17, "staff")
        other_body["assignee"] = self.lead["open_id"]
        code, message = server.SERVICE.order(self.staff, other_body)
        self.assertEqual(code, 403)
        self.assertIn("只能给自己下单", message)

    def test_booking_duration_uses_selected_minutes_and_custom_task(self):
        conn = server.db()
        standard_minutes = conn.execute(
            "SELECT minutes FROM sla WHERE grp='灯光组' AND task_type=?", (self.task,)
        ).fetchone()["minutes"]
        conn.close()

        standard = self.order_body(14)
        self.assertEqual(server.SERVICE.order(self.team, standard)[0], 200)
        self.assertEqual(self.latest_order()["minutes"], standard_minutes)

        selected = self.order_body(17, "admin")
        selected["minutes"] = 180
        self.assertEqual(server.SERVICE.order(self.admin, selected)[0], 200)
        selected_order = self.latest_order()
        self.assertEqual(selected_order["minutes"], 180)
        self.assertEqual(
            datetime.fromisoformat(selected_order["scheduled_end"])
            - datetime.fromisoformat(selected_order["scheduled_start"]),
            timedelta(minutes=180),
        )

        custom = self.order_body(21, "admin")
        custom.update({"task": "直播间整场灯光重做", "custom_task": "1", "minutes": 150})
        self.assertEqual(server.SERVICE.order(self.admin, custom)[0], 200)
        self.assertEqual(self.latest_order()["task_type"], "直播间整场灯光重做")
        self.assertEqual(self.latest_order()["minutes"], 150)

        missing_flag = self.order_body(12, "admin")
        missing_flag["task"] = "未登记的临时任务"
        code, message = server.SERVICE.order(self.admin, missing_flag)
        self.assertEqual(code, 400)
        self.assertIn("尚未配置", message)

        source = (Path(server.ROOT) / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="qsMinutes"', source)
        self.assertIn('id="reqMinutes"', source)
        self.assertIn("自定义任务…", source)

    def test_over_four_hours_requires_manager_review_before_assignment(self):
        body = self.order_body(14)
        body["minutes"] = 300
        self.assertEqual(server.SERVICE.order(self.team, body), (200, None))
        order = self.latest_order()
        self.assertEqual(order["status"], "pending")
        self.assertEqual(order["duration_review"], "pending")

        code, message = server.SERVICE.assign(
            self.lead, {"order_id": order["id"], "user_id": self.staff["open_id"]}
        )
        self.assertEqual(code, 409)
        self.assertIn("先由组长或管理员审核", message)

        self.assertEqual(
            server.SERVICE.review_duration(
                self.lead, {"order_id": order["id"], "decision": "approve"}
            ),
            (200, None),
        )
        approved = self.latest_order()
        self.assertEqual(approved["duration_review"], "approved")
        self.assertEqual(approved["status"], "pending")
        self.assertEqual(
            server.SERVICE.assign(
                self.lead, {"order_id": order["id"], "user_id": self.staff["open_id"]}
            ),
            (200, None),
        )

    def test_staff_long_self_order_stays_assigned_to_self_after_review(self):
        body = self.order_body(14, "staff")
        body["minutes"] = 300
        self.assertEqual(server.SERVICE.order(self.staff, body), (200, None))
        pending = self.latest_order()
        self.assertEqual(pending["duration_review"], "pending")
        self.assertEqual(pending["assignee_id"], "")
        self.assertEqual(pending["duration_assignee_id"], self.staff["open_id"])
        self.assertEqual(
            server.SERVICE.review_duration(
                self.lead, {"order_id": pending["id"], "decision": "approve"}
            ),
            (200, None),
        )
        approved = self.latest_order()
        self.assertEqual(approved["duration_review"], "approved")
        self.assertEqual(approved["status"], "assigned")
        self.assertEqual(approved["assignee_id"], self.staff["open_id"])

    def test_pending_order_edit_supports_custom_task_and_duration_review(self):
        self.assertEqual(server.SERVICE.order(self.team, self.order_body(14)), (200, None))
        order = self.latest_order()
        self.assertEqual(
            server.SERVICE.edit(
                self.team,
                {
                    "order_id": order["id"],
                    "task": "临时舞台灯光整体调整",
                    "custom_task": "1",
                    "minutes": 180,
                },
            ),
            (200, None),
        )
        edited = self.latest_order()
        self.assertEqual(edited["task_type"], "临时舞台灯光整体调整")
        self.assertEqual(edited["minutes"], 180)
        self.assertEqual(edited["duration_review"], "approved")

        self.assertEqual(
            server.SERVICE.edit(
                self.team,
                {
                    "order_id": order["id"],
                    "task": "临时舞台灯光整体调整",
                    "custom_task": "1",
                    "minutes": 300,
                },
            ),
            (200, None),
        )
        awaiting = self.latest_order()
        self.assertEqual(awaiting["minutes"], 300)
        self.assertEqual(awaiting["duration_review"], "pending")
        code, message = server.SERVICE.edit(
            self.team,
            {
                "order_id": order["id"],
                "task": "临时舞台灯光整体调整",
                "custom_task": "1",
                "minutes": 180,
            },
        )
        self.assertEqual(code, 409)
        self.assertIn("审核中", message)

    def test_edit_cannot_bypass_booking_cap_or_biange_attachment_rule(self):
        self.assertEqual(server.SERVICE.order(self.team, self.order_body(14)), (200, None))
        first = self.latest_order()
        self.assertEqual(server.SERVICE.order(self.team, self.order_body(18)), (200, None))
        code, message = server.SERVICE.edit(
            self.team,
            {"order_id": first["id"], "task": self.task, "minutes": 200},
        )
        self.assertEqual(code, 409)
        self.assertIn("当天预约", message)

        code, message = server.SERVICE.edit(
            self.team,
            {
                "order_id": first["id"],
                "task": "编歌临时需求",
                "custom_task": "1",
                "minutes": 60,
            },
        )
        self.assertEqual(code, 400)
        self.assertIn("必须上传", message)

    def test_manager_long_task_skips_review_and_rejection_cancels_team_request(self):
        manager_body = self.order_body(14, "admin")
        manager_body.update({"minutes": 300, "assignee": self.staff["open_id"]})
        self.assertEqual(server.SERVICE.order(self.admin, manager_body), (200, None))
        manager_order = self.latest_order()
        self.assertEqual(manager_order["duration_review"], "approved")
        self.assertEqual(manager_order["status"], "assigned")

        team_body = self.order_body(22)
        team_body["minutes"] = 300
        self.assertEqual(server.SERVICE.order(self.team, team_body), (200, None))
        team_order = self.latest_order()
        self.assertEqual(
            server.SERVICE.review_duration(
                self.lead,
                {"order_id": team_order["id"], "decision": "reject", "reason": "人员不足"},
            ),
            (200, None),
        )
        rejected = self.latest_order()
        self.assertEqual(rejected["status"], "cancelled")
        self.assertEqual(rejected["duration_review"], "rejected")
        self.assertEqual(rejected["cancel_reason"], "人员不足")

    def test_deleted_task_is_not_seeded_again_on_restart(self):
        conn = server.db()
        task = dict(
            conn.execute(
                "SELECT task_type,minutes,buffer,note,grp FROM sla WHERE grp='灯光组' AND task_type=?",
                (self.task,),
            ).fetchone()
        )
        conn.execute("DELETE FROM sla WHERE grp=? AND task_type=?", (task["grp"], task["task_type"]))
        conn.commit()
        conn.close()
        try:
            server.init_db()
            conn = server.db()
            restored = conn.execute(
                "SELECT 1 FROM sla WHERE grp=? AND task_type=?", (task["grp"], task["task_type"])
            ).fetchone()
            conn.close()
            self.assertIsNone(restored)
        finally:
            conn = server.db()
            conn.execute(
                "INSERT INTO sla(task_type,minutes,buffer,note,grp) VALUES(?,?,?,?,?)",
                (task["task_type"], task["minutes"], task["buffer"], task["note"], task["grp"]),
            )
            conn.commit()
            conn.close()

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

    def test_member_cannot_modify_own_status(self):
        code, message = server.do_save(
            self.staff,
            {"entity": "users", "record": {"open_id": self.staff["open_id"], "status": "rest"}},
        )
        self.assertEqual(code, 403)
        self.assertIn("管理员或组长", message)
        code, message = server.do_save(
            self.lead,
            {"entity": "users", "record": {"open_id": self.lead["open_id"], "status": "rest"}},
        )
        self.assertEqual(code, 403)
        self.assertIn("不能修改自己", message)
        source = (Path(server.ROOT) / "index.html").read_text(encoding="utf-8")
        server_source = (Path(server.ROOT) / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("data-status", source)
        self.assertNotIn("/api/tech-status", server_source)
        self.assertIn("状态由组长或管理员维护", source)

    def test_manager_cannot_modify_own_absence_or_approve_own_review(self):
        day = datetime.now().date().isoformat()
        code, message = server.do_save(
            self.lead,
            {
                "entity": "users",
                "record": {
                    "open_id": self.lead["open_id"],
                    "absence_type": "leave",
                    "absence_start": day,
                    "absence_end": day,
                },
            },
        )
        self.assertEqual(code, 403)
        self.assertIn("离岗安排", message)

        body = {
            "team_id": self.team_row["id"],
            "group": "灯光组",
            "task": self.task,
            "when": "now",
            "assignee": self.lead["open_id"],
        }
        self.assertEqual(server.SERVICE.order(self.admin, body)[0], 200)
        order = self.latest_order()
        self.assertEqual(server.SERVICE.start(self.lead, {"order_id": order["id"]}), (200, None))
        self.assertEqual(
            server.SERVICE.complete(
                self.lead,
                {"order_id": order["id"], "note": "申请结束", "reason": "现场提前结束"},
            )[0],
            200,
        )
        review = self.latest_review()
        code, message = server.SERVICE.resolve_time_review(
            self.lead,
            {"review_id": review["id"], "decision": "approve", "note": "本人确认"},
        )
        self.assertEqual(code, 403)
        self.assertIn("不能审核自己", message)

    def test_retro_start_review_rechecks_current_availability(self):
        body = {
            "team_id": self.team_row["id"],
            "group": "灯光组",
            "task": self.task,
            "when": "now",
            "assignee": self.staff["open_id"],
        }
        self.assertEqual(server.SERVICE.order(self.admin, body)[0], 200)
        order = self.latest_order()
        self.assertEqual(
            server.SERVICE.report_start(
                self.staff,
                {
                    "order_id": order["id"],
                    "started_at": server.business.now_iso(),
                    "reason": "忘记点开始",
                },
            )[0],
            200,
        )
        conn = server.db()
        conn.execute("UPDATE users SET status='rest' WHERE open_id=?", (self.staff["open_id"],))
        conn.commit()
        conn.close()
        review = self.latest_review()
        code, message = server.SERVICE.resolve_time_review(
            self.lead,
            {"review_id": review["id"], "decision": "approve", "note": "核实补报"},
        )
        self.assertEqual(code, 409)
        self.assertIn("小休", message)

    def test_immediate_assignment_and_reassignment_recheck_current_status(self):
        conn = server.db()
        conn.execute("UPDATE users SET status='rest' WHERE open_id=?", (self.staff["open_id"],))
        conn.commit()
        conn.close()
        body = {
            "team_id": self.team_row["id"],
            "group": "灯光组",
            "task": self.task,
            "when": "now",
            "assignee": self.staff["open_id"],
        }
        code, message = server.SERVICE.order(self.admin, body)
        self.assertEqual(code, 409)
        self.assertIn("小休", message)

        body.pop("assignee")
        self.assertEqual(server.SERVICE.order(self.admin, body)[0], 200)
        order = self.latest_order()
        code, message = server.SERVICE.reassign(
            self.lead, {"order_id": order["id"], "user_id": self.staff["open_id"]}
        )
        self.assertEqual(code, 409)
        self.assertIn("小休", message)

    def test_transfer_requires_independent_approval_and_notifies_global_admin(self):
        body = {
            "team_id": self.team_row["id"],
            "group": "灯光组",
            "task": self.task,
            "when": "now",
            "assignee": self.lead["open_id"],
        }
        self.assertEqual(server.SERVICE.order(self.admin, body)[0], 200)
        order = self.latest_order()
        self.assertEqual(server.SERVICE.start(self.lead, {"order_id": order["id"]}), (200, None))
        self.assertEqual(
            server.SERVICE.request_transfer(
                self.lead,
                {
                    "order_id": order["id"],
                    "reason_code": "临时生病",
                    "target_id": self.staff["open_id"],
                },
            )[0],
            200,
        )
        conn = server.db()
        transfer = dict(conn.execute("SELECT * FROM transfers ORDER BY id DESC LIMIT 1").fetchone())
        notified = conn.execute(
            "SELECT 1 FROM notification_outbox WHERE dedupe_key=?",
            (f"transfer:{transfer['id']}:approval:{self.admin['open_id']}",),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(notified)
        code, message = server.SERVICE.approve_transfer(
            self.lead, {"transfer_id": transfer["id"]}
        )
        self.assertEqual(code, 403)
        self.assertIn("不能审批自己", message)
        self.assertEqual(
            server.SERVICE.approve_transfer(self.admin, {"transfer_id": transfer["id"]}),
            (200, None),
        )

    def test_team_lead_flag_never_grants_management_permission(self):
        conn = server.db()
        conn.execute(
            "UPDATE users SET lead=1,grp='灯光组' WHERE open_id=?", (self.team["open_id"],)
        )
        conn.commit()
        conn.close()
        team_with_bad_data = self.user(self.team["open_id"])
        self.assertFalse(server.business.is_manager(team_with_bad_data, "灯光组"))
        self.assertEqual(
            server.do_save(
                team_with_bad_data,
                {
                    "entity": "users",
                    "record": {"open_id": self.staff["open_id"], "status": "rest"},
                },
            )[0],
            403,
        )

    def test_manager_cannot_mark_imminent_assignee_unavailable(self):
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
        code, message = server.do_save(
            self.lead,
            {"entity": "users", "record": {"open_id": self.staff["open_id"], "status": "rest"}},
        )
        self.assertEqual(code, 409)
        self.assertIn("忙碌", message)
        self.assertEqual(server.SERVICE.start(self.staff, {"order_id": order["id"]}), (200, None))

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
        self.assertEqual(
            server.SERVICE.complete(
                self.lead,
                {"order_id": order["id"], "note": "任务完成", "reason": "现场提前结束"},
            )[0],
            200,
        )
        self.assertEqual(self.latest_order()["status"], "completion_review")
        review = self.latest_review()
        self.assertEqual(
            server.SERVICE.resolve_time_review(
                self.admin,
                {"review_id": review["id"], "decision": "approve", "note": "现场确认结束"},
            )[0],
            200,
        )
        self.assertEqual(self.latest_order()["status"], "completed")
        conn = server.db()
        sessions = conn.execute("SELECT * FROM work_sessions WHERE order_id=? ORDER BY id", (order["id"],)).fetchall()
        conn.close()
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0]["end_reason"], "transferred")
        self.assertEqual(sessions[1]["end_reason"], "completed")
        code, stats = server.api_stats(self.admin, {"range": "all"})
        self.assertEqual(code, 200)
        names = {item["open_id"] for item in stats["byPerson"]}
        self.assertIn(self.staff["open_id"], names)
        self.assertIn(self.lead["open_id"], names)

    def test_early_end_waits_for_manager_without_releasing_worker(self):
        order = self.create_running_order()
        code, marker = server.SERVICE.complete(
            self.staff,
            {"order_id": order["id"], "note": "现场提前收工", "reason": "比赛提前结束"},
        )
        self.assertEqual((code, marker), (200, "review"))
        self.assertEqual(self.latest_order()["status"], "completion_review")
        conn = server.db()
        worker = conn.execute("SELECT status FROM users WHERE open_id=?", (self.staff["open_id"],)).fetchone()
        session = conn.execute(
            "SELECT ended_at FROM work_sessions WHERE order_id=? ORDER BY id DESC LIMIT 1", (order["id"],)
        ).fetchone()
        conn.close()
        self.assertEqual(worker["status"], "busy")
        self.assertEqual(session["ended_at"], "")

        review = self.latest_review()
        self.assertEqual(
            server.SERVICE.resolve_time_review(
                self.lead,
                {"review_id": review["id"], "decision": "approve", "note": "已联系直播间确认"},
            )[0],
            200,
        )
        self.assertEqual(self.latest_order()["status"], "completed")
        conn = server.db()
        worker = conn.execute("SELECT status FROM users WHERE open_id=?", (self.staff["open_id"],)).fetchone()
        session = conn.execute(
            "SELECT ended_at FROM work_sessions WHERE order_id=? ORDER BY id DESC LIMIT 1", (order["id"],)
        ).fetchone()
        conn.close()
        self.assertEqual(worker["status"], "free")
        self.assertTrue(session["ended_at"])
        self.assertEqual(
            server.SERVICE.resolve_time_review(
                self.admin,
                {"review_id": review["id"], "decision": "approve", "note": "重复处理"},
            )[0],
            409,
        )

    def test_rejected_early_end_returns_to_running(self):
        order = self.create_running_order()
        self.assertEqual(
            server.SERVICE.complete(
                self.staff,
                {"order_id": order["id"], "note": "申请结束", "reason": "误以为比赛结束"},
            )[0],
            200,
        )
        review = self.latest_review()
        self.assertEqual(
            server.SERVICE.resolve_time_review(
                self.lead,
                {"review_id": review["id"], "decision": "reject", "note": "比赛仍在继续"},
            )[0],
            200,
        )
        self.assertEqual(self.latest_order()["status"], "in_progress")
        conn = server.db()
        open_session = conn.execute(
            "SELECT 1 FROM work_sessions WHERE order_id=? AND ended_at=''", (order["id"],)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(open_session)

    def test_retro_start_requires_review_before_timing(self):
        body = {"team_id": self.team_row["id"], "group": "灯光组", "task": self.task, "when": "now"}
        self.assertEqual(server.SERVICE.order(self.admin, body)[0], 200)
        order = self.latest_order()
        self.assertEqual(
            server.SERVICE.assign(
                self.lead, {"order_id": order["id"], "user_id": self.staff["open_id"]}
            )[0],
            200,
        )
        started_at = server.business.format_dt(datetime.now())
        self.assertEqual(
            server.SERVICE.report_start(
                self.staff,
                {"order_id": order["id"], "started_at": started_at, "reason": "到岗后忘记点开始"},
            )[0],
            200,
        )
        self.assertEqual(self.latest_order()["status"], "assigned")
        review = self.latest_review()
        self.assertEqual(review["kind"], "retro_start")
        self.assertEqual(
            server.SERVICE.resolve_time_review(
                self.lead,
                {"review_id": review["id"], "decision": "approve", "note": "现场确认已到岗"},
            )[0],
            200,
        )
        self.assertEqual(self.latest_order()["status"], "in_progress")

    def test_team_can_dispute_and_manager_reopen_completed_order(self):
        order = self.create_running_order()
        self.assertEqual(
            server.SERVICE.complete(
                self.staff,
                {"order_id": order["id"], "note": "已结束", "reason": "比赛提前结束"},
            )[0],
            200,
        )
        review = self.latest_review()
        self.assertEqual(
            server.SERVICE.resolve_time_review(
                self.lead,
                {"review_id": review["id"], "decision": "approve", "note": "当时确认结束"},
            )[0],
            200,
        )
        self.assertEqual(
            server.SERVICE.dispute_completion(
                self.team, {"order_id": order["id"], "reason": "比赛实际仍在继续"}
            )[0],
            200,
        )
        dispute = self.latest_review()
        self.assertEqual(dispute["kind"], "team_dispute")
        conn = server.db()
        conn.execute("UPDATE users SET status='rest' WHERE open_id=?", (self.staff["open_id"],))
        conn.commit()
        conn.close()
        code, message = server.SERVICE.resolve_time_review(
            self.admin,
            {"review_id": dispute["id"], "decision": "reopen", "note": "尝试重新打开"},
        )
        self.assertEqual(code, 409)
        self.assertIn("小休", message)
        conn = server.db()
        conn.execute("UPDATE users SET status='free' WHERE open_id=?", (self.staff["open_id"],))
        conn.commit()
        conn.close()
        self.assertEqual(
            server.SERVICE.resolve_time_review(
                self.admin,
                {"review_id": dispute["id"], "decision": "reopen", "note": "团队和负责人均确认未结束"},
            )[0],
            200,
        )
        self.assertEqual(self.latest_order()["status"], "in_progress")

    def test_time_correction_cannot_overlap_a_later_work_session(self):
        first = self.create_running_order()
        first_started = server.business.format_dt(datetime.now() - timedelta(hours=2))
        first_ended = server.business.format_dt(datetime.now() - timedelta(hours=1))
        conn = server.db()
        conn.execute("UPDATE orders SET started_at=? WHERE id=?", (first_started, first["id"]))
        conn.execute(
            "UPDATE work_sessions SET started_at=? WHERE order_id=? AND ended_at=''",
            (first_started, first["id"]),
        )
        conn.commit()
        conn.close()
        self.assertEqual(
            server.SERVICE.complete(
                self.staff,
                {
                    "order_id": first["id"],
                    "ended_at": first_ended,
                    "note": "第一场结束",
                    "reason": "忘记及时点结束",
                },
            )[0],
            200,
        )
        review = self.latest_review()
        self.assertEqual(
            server.SERVICE.resolve_time_review(
                self.lead,
                {"review_id": review["id"], "decision": "approve", "note": "已核对第一场结束时间"},
            )[0],
            200,
        )

        second = self.create_running_order()
        second_started = server.business.format_dt(datetime.now() - timedelta(seconds=30))
        conn = server.db()
        conn.execute("UPDATE orders SET started_at=? WHERE id=?", (second_started, second["id"]))
        conn.execute(
            "UPDATE work_sessions SET started_at=? WHERE order_id=? AND ended_at=''",
            (second_started, second["id"]),
        )
        conn.commit()
        conn.close()
        self.assertEqual(
            server.SERVICE.dispute_completion(
                self.team, {"order_id": first["id"], "reason": "结束时间需要重新核对"}
            )[0],
            200,
        )
        dispute = self.latest_review()
        code, message = server.SERVICE.resolve_time_review(
            self.admin,
            {
                "review_id": dispute["id"],
                "decision": "approve",
                "corrected_at": server.business.now_iso(),
                "note": "尝试延长到第二场开始之后",
            },
        )
        self.assertEqual(code, 409)
        self.assertIn(f"#{second['id']}", message)
        self.assertEqual(self.latest_review()["status"], "pending")

    def test_long_overtime_only_reminds_and_never_auto_closes(self):
        order = self.create_running_order()
        old = server.business.format_dt(datetime.now() - timedelta(hours=8))
        conn = server.db()
        conn.execute(
            "UPDATE orders SET started_at=?,minutes=30,overrun_notified=0 WHERE id=?", (old, order["id"])
        )
        conn.execute("UPDATE work_sessions SET started_at=? WHERE order_id=?", (old, order["id"]))
        conn.commit()
        conn.close()
        server.run_scheduled_jobs()
        updated = self.latest_order()
        self.assertEqual(updated["status"], "in_progress")
        self.assertEqual(updated["overrun_notified"], 1)
        conn = server.db()
        open_session = conn.execute(
            "SELECT 1 FROM work_sessions WHERE order_id=? AND ended_at=''", (order["id"],)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(open_session)

    def test_pending_retro_start_review_is_not_auto_released(self):
        tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
        body = {
            "team_id": self.team_row["id"],
            "group": "灯光组",
            "task": self.task,
            "when": "book",
            "date": tomorrow,
            "at": "15:00",
            "assignee": self.staff["open_id"],
        }
        self.assertEqual(server.SERVICE.order(self.admin, body)[0], 200)
        order = self.latest_order()
        conn = server.db()
        created = server.business.format_dt(datetime.now() - timedelta(hours=2))
        stale = server.business.format_dt(datetime.now() - timedelta(hours=1))
        conn.execute(
            "UPDATE orders SET created_at=?,scheduled_start=? WHERE id=?",
            (created, stale, order["id"]),
        )
        conn.commit()
        conn.close()
        self.assertEqual(
            server.SERVICE.report_start(
                self.staff,
                {
                    "order_id": order["id"],
                    "started_at": server.business.format_dt(datetime.now() - timedelta(minutes=30)),
                    "reason": "现场已到岗但忘记点开始",
                },
            )[0],
            200,
        )
        server.run_scheduled_jobs()
        self.assertEqual(self.latest_order()["status"], "assigned")
        self.assertEqual(self.latest_review()["status"], "pending")

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
        self.assertNotIn("lighting_owner_id", state["teams"][0])
        self.assertTrue(all(order["team_id"] == self.team_row["id"] for order in state["orders"]))
        code, stats = server.api_stats(self.team, {"range": "30d"})
        self.assertEqual(code, 200)
        self.assertEqual(stats["byPerson"], [])
        self.assertEqual(stats["summary"]["people"], 0)

    def test_worker_skill_matrix_is_fully_removed(self):
        conn = server.db()
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker_skills'"
        ).fetchone()
        conn.close()
        self.assertIsNone(table)
        self.assertNotIn("skills", server.api_state(self.admin))
        page_source = (Path(server.ROOT) / "index.html").read_text(encoding="utf-8")
        server_source = (Path(server.ROOT) / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("人员能力矩阵", page_source)
        self.assertNotIn("state.skills", page_source)
        self.assertNotIn("/api/skills", server_source)

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
        self.assertIn("状态由组长或管理员维护", (Path(server.ROOT) / "index.html").read_text(encoding="utf-8"))

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

    def test_order_changes_always_queue_calendar_sync(self):
        self.assertEqual(server.SERVICE.order(self.admin, self.order_body(15, "admin"))[0], 200)
        order = self.latest_order()
        conn = server.db()
        created = conn.execute(
            "SELECT COUNT(*) count FROM notification_outbox WHERE kind='calendar_sync'"
        ).fetchone()["count"]
        conn.close()
        self.assertGreaterEqual(created, 1)

        self.assertEqual(
            server.SERVICE.assign(
                self.lead, {"order_id": order["id"], "user_id": self.staff["open_id"]}
            )[0],
            200,
        )
        conn = server.db()
        queued = conn.execute(
            "SELECT payload FROM notification_outbox WHERE kind='calendar_sync' ORDER BY id"
        ).fetchall()
        conn.close()
        self.assertGreater(len(queued), created)
        self.assertTrue(all(json.loads(row["payload"])["order_id"] == order["id"] for row in queued))

    def test_calendar_sync_creates_updates_and_removes_event(self):
        conn = server.db()
        conn.execute("DELETE FROM settings WHERE k='feishu_calendar_id'")
        conn.commit()
        conn.close()
        self.assertEqual(server.SERVICE.order(self.admin, self.order_body(15, "admin"))[0], 200)
        order = self.latest_order()
        calls = []

        def create_call(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if path == "/calendar/v4/calendars":
                return {"code": 0, "data": {"calendar": {"calendar_id": "calendar-1"}}}
            return {"code": 0, "data": {"event": {"event_id": "event-1"}}}

        with mock.patch.object(server, "feishu_api", side_effect=create_call), \
                mock.patch.object(server, "tenant_token", return_value="token"):
            server._sync_calendar_now({}, {"order_id": order["id"]})
        self.assertEqual(calls[0][0:2], ("POST", "/calendar/v4/calendars"))
        self.assertEqual(calls[1][0], "POST")
        self.assertIn("/events", calls[1][1])
        event_body = calls[1][2]["body"]
        self.assertIn("待派", event_body["summary"])
        self.assertEqual(event_body["visibility"], "public")
        self.assertEqual(
            int(event_body["end_time"]["timestamp"]) - int(event_body["start_time"]["timestamp"]),
            order["minutes"] * 60,
        )
        synced = self.latest_order()
        self.assertEqual(synced["feishu_calendar_id"], "calendar-1")
        self.assertEqual(synced["feishu_event_id"], "event-1")

        self.assertEqual(
            server.SERVICE.assign(
                self.lead, {"order_id": order["id"], "user_id": self.staff["open_id"]}
            )[0],
            200,
        )
        update_calls = []

        def update_call(method, path, **kwargs):
            update_calls.append((method, path, kwargs))
            return {"code": 0}

        with mock.patch.object(server, "feishu_api", side_effect=update_call), \
                mock.patch.object(server, "tenant_token", return_value="token"):
            server._sync_calendar_now({}, {"order_id": order["id"]})
        self.assertEqual(update_calls[0][0], "PATCH")
        self.assertIn("已安排", update_calls[0][2]["body"]["summary"])
        self.assertIn(self.staff["name"], update_calls[0][2]["body"]["description"])

        self.assertEqual(
            server.SERVICE.cancel(
                self.admin, {"order_id": order["id"], "reason": "日历删除测试"}
            )[0],
            200,
        )
        delete_calls = []

        def delete_call(method, path, **kwargs):
            delete_calls.append((method, path, kwargs))
            return {"code": 0}

        with mock.patch.object(server, "feishu_api", side_effect=delete_call), \
                mock.patch.object(server, "tenant_token", return_value="token"):
            server._sync_calendar_now({}, {"order_id": order["id"]})
        self.assertEqual(delete_calls[0][0], "DELETE")
        self.assertIn("event-1", delete_calls[0][1])
        removed = self.latest_order()
        self.assertEqual(removed["feishu_event_id"], "")
        self.assertEqual(removed["feishu_calendar_id"], "")

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

    def test_mobile_home_screen_assets_are_complete(self):
        root = Path(server.ROOT)
        manifest = json.loads((root / "manifest.webmanifest").read_text(encoding="utf-8"))
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["scope"], "/")
        self.assertEqual({icon["sizes"] for icon in manifest["icons"]}, {"192x192", "512x512"})
        for icon in manifest["icons"]:
            path = root / icon["src"].removeprefix("/")
            self.assertTrue(path.is_file())
            self.assertTrue(path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue((root / "app-icon-180.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

        page_source = (root / "index.html").read_text(encoding="utf-8")
        self.assertIn('<link rel="manifest" href="/manifest.webmanifest">', page_source)
        self.assertIn('<link rel="apple-touch-icon" href="/app-icon-180.png">', page_source)
        self.assertIn('<meta name="apple-mobile-web-app-capable" content="yes">', page_source)
        self.assertIn("-webkit-text-size-adjust:100%", page_source)


if __name__ == "__main__":
    unittest.main()
