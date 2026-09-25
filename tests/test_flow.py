import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database import ContinuityDB, DomainError

class ContinuityFlowTest(unittest.TestCase):
    def setUp(self):
        fd,self.path=tempfile.mkstemp(suffix=".db"); os.close(fd); self.db=ContinuityDB(self.path)
        self.producer=self.db.add_user("制片","producer"); self.continuity=self.db.add_user("场记","continuity"); self.reviewer=self.db.add_user("审片","reviewer")
        self.production=self.db.create_production("测试影片","非线性拍摄",self.producer)
        self.scene=self.db.add_scene(self.production,"S01","雨夜",1)
        self.s1=self.db.add_shot(self.scene,"S01-01",2,1,"受伤后",self.continuity)
        self.s2=self.db.add_shot(self.scene,"S01-02",1,2,"受伤前",self.continuity)
        self.injury=self.db.add_element(self.production,"手臂伤痕","injury","monotonic","只能加重")
        self.db.set_element_state(self.s1,self.injury,"重度",3,"",self.continuity,"2026-09-20","首次录入")
        self.db.set_element_state(self.s2,self.injury,"轻度",1,"",self.continuity,"2026-09-20","首次录入")
    def tearDown(self): self.db.close(); os.unlink(self.path)

    def test_conflict_plan_review_and_lock_flow(self):
        conflicts=self.db.check_scene(self.scene)
        self.assertEqual("regression",conflicts[0]["kind"])
        self.assertEqual(1,conflicts[0]["from_version"]); self.assertEqual(1,conflicts[0]["to_version"])
        plan=self.db.propose_adjustment(conflicts[0]["id"],"重度",3,"将伤势调整到叙事顺序上的中间状态",self.continuity,"2026-09-21")
        with self.assertRaisesRegex(DomainError,"审核期间"):
            self.db.set_element_state(self.s2,self.injury,"中度",2,"重做",self.continuity,"2026-09-22","补拍")
        result=self.db.review_adjustment(plan,True,self.reviewer,"通过")
        self.assertEqual([],result["conflicts"])
        versions=self.db.list_shot_versions(self.s2)
        self.assertEqual(2,len(versions))
        enabled=[v for v in versions if v["status"]=="enabled"][0]
        self.assertEqual(2,enabled["version"]); self.assertEqual("plan",enabled["source"]); self.assertEqual(plan,enabled["plan_id"])
        self.assertEqual("重度",enabled["state_value"]); self.assertEqual("2026-09-21",enabled["shoot_date"])
        self.assertEqual("superseded",[v for v in versions if v["version"]==1][0]["status"])
        r1=self.db.lock_shot(self.s1,self.continuity); r2=self.db.lock_shot(self.s2,self.continuity)
        self.assertEqual(1,r1["version"]); self.assertEqual(2,r2["version"])
        with self.assertRaisesRegex(DomainError,"已锁定"):
            self.db.lock_shot(self.s1,self.continuity)

    def test_reshoot_version_supersedes_and_releases_scene_locks(self):
        res=self.db.set_element_state(self.s2,self.injury,"重度",3,"",self.continuity,"2026-09-21","补拍：与上一镜头伤势一致")
        self.assertEqual(2,res["version"])
        self.assertEqual([],self.db.check_scene(self.scene))
        self.db.lock_shot(self.s1,self.continuity); self.db.lock_shot(self.s2,self.continuity)
        res=self.db.set_element_state(self.s2,self.injury,"特重",4,"",self.continuity,"2026-09-22","补拍：伤痕加重")
        self.assertEqual(2,res["released_locks"])
        shots={s["id"]:s for s in self.db.snapshot()["shots"]}
        self.assertEqual("pending",shots[self.s1]["status"]); self.assertEqual("pending",shots[self.s2]["status"])
        self.assertIn("补拍版本",shots[self.s1]["status_note"]); self.assertIn("补拍版本",shots[self.s2]["status_note"])
        locks=self.db.list_shot_locks(self.s2)
        self.assertEqual("released",locks[0]["status"]); self.assertIn("补拍版本",locks[0]["released_reason"])
        again=self.db.lock_shot(self.s2,self.continuity)
        self.assertEqual(res["shot_version"],again["version"])
        self.assertEqual(2,len(self.db.list_shot_locks(self.s2)))

    def test_versions_archive_and_report_marks_basis(self):
        self.db.set_element_state(self.s2,self.injury,"重度",3,"",self.continuity,"2026-09-21","补拍")
        versions=self.db.list_shot_versions(self.s2)
        self.assertEqual({1:"superseded",2:"enabled"},{v["version"]:v["status"] for v in versions})
        self.assertEqual("轻度",[v for v in versions if v["version"]==1][0]["state_value"])
        self.db.lock_shot(self.s1,self.continuity); self.db.lock_shot(self.s2,self.continuity)
        report=self.db.continuity_report(self.production)
        shot2=[s for s in report["scenes"][0]["shots"] if s["id"]==self.s2][0]
        self.assertEqual(2,shot2["version"]); self.assertEqual(2,shot2["active_lock"]["shot_version"])
        state=[st for st in shot2["states"] if st["element_id"]==self.injury][0]
        self.assertEqual(2,state["version"]); self.assertEqual("2026-09-21",state["shoot_date"]); self.assertEqual("补拍",state["reason"])

    def test_plan_rejection_reopens_submissions(self):
        conflicts=self.db.check_scene(self.scene)
        plan=self.db.propose_adjustment(conflicts[0]["id"],"重度",3,"调整理由足够长",self.continuity,"2026-09-21")
        self.db.review_adjustment(plan,False,self.reviewer,"驳回")
        res=self.db.set_element_state(self.s2,self.injury,"重度",3,"",self.continuity,"2026-09-22","补拍：按现场调整")
        self.assertEqual(2,res["version"])

    def test_exemption_and_validation_failures(self):
        conflicts=self.db.check_scene(self.scene)
        with self.assertRaisesRegex(DomainError,"单调规则"):
            self.db.set_element_state(self.s1,self.injury,"未知",None,"",self.continuity,"2026-09-21","补拍")
        with self.assertRaisesRegex(DomainError,"替换原因"):
            self.db.set_element_state(self.s1,self.injury,"重度",3,"",self.continuity,"2026-09-21","")
        with self.assertRaisesRegex(DomainError,"拍摄日"):
            self.db.set_element_state(self.s1,self.injury,"重度",3,"",self.continuity,"","补拍")
        with self.assertRaisesRegex(DomainError,"拍摄日"):
            self.db.propose_adjustment(conflicts[0]["id"],"重度",3,"调整理由足够长",self.continuity,"")
        self.db.approve_exemption(conflicts[0]["id"],"闪回镜头中伤痕表现属于刻意叙事误差",self.reviewer)
        self.db.lock_shot(self.s1,self.continuity); self.db.lock_shot(self.s2,self.continuity)
        with self.assertRaisesRegex(DomainError,"只有审片人"):
            self.db.review_adjustment(999,True,self.continuity,"无权审核")

if __name__=="__main__": unittest.main()
