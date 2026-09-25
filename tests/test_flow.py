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
        self.db.set_element_state(self.s1,self.injury,"重度",3,"2026-09-20","首拍受伤后",self.continuity)
        self.db.set_element_state(self.s2,self.injury,"轻度",1,"2026-09-21","首拍受伤前",self.continuity)
    def tearDown(self): self.db.close(); os.unlink(self.path)
    def test_conflict_plan_review_and_lock_flow(self):
        conflicts=self.db.check_scene(self.scene)
        self.assertEqual("regression",conflicts[0]["kind"])
        plan=self.db.propose_adjustment(conflicts[0]["id"],"重度",3,"将伤势调整到叙事顺序上的中间状态",self.continuity,"2026-09-22")
        # 审核期间冻结新版本
        with self.assertRaisesRegex(DomainError,"审核期间不收新版本"):
            self.db.set_element_state(self.s2,self.injury,"中度",2,"2026-09-22","想直接补拍",self.continuity)
        result=self.db.review_adjustment(plan,True,self.reviewer,"通过")
        self.assertEqual([],result["conflicts"])
        self.assertEqual(2,result["version"])
        self.db.lock_shot(self.s1,self.continuity); self.db.lock_shot(self.s2,self.continuity)
        # 锁定镜头收到补拍新版本：不再被静默替换，而是作废当前锁定并级联退回
        r=self.db.set_element_state(self.s2,self.injury,"重度",3,"2026-09-23","补拍重做同状态",self.continuity)
        self.assertIn(self.s1,r["released_locks"])
        shots={s["id"]:s for s in self.db.snapshot()["shots"]}
        self.assertEqual("relock_pending",shots[self.s1]["status"])
        self.assertEqual("relock_pending",shots[self.s2]["status"])
    def test_exemption_and_validation_failures(self):
        conflicts=self.db.check_scene(self.scene)
        with self.assertRaisesRegex(DomainError,"单调规则"):
            self.db.set_element_state(self.s1,self.injury,"未知",None,"2026-09-22","补拍试错",self.continuity)
        with self.assertRaisesRegex(DomainError,"拍摄日"):
            self.db.set_element_state(self.s1,self.injury,"未知",3,"2026/09/22","补拍试错",self.continuity)
        with self.assertRaisesRegex(DomainError,"替换原因"):
            self.db.set_element_state(self.s1,self.injury,"未知",3,"2026-09-22","短",self.continuity)
        self.db.approve_exemption(conflicts[0]["id"],"闪回镜头中伤痕表现属于刻意叙事误差",self.reviewer)
        self.db.lock_shot(self.s1,self.continuity); self.db.lock_shot(self.s2,self.continuity)
        with self.assertRaisesRegex(DomainError,"只有审片人"):
            self.db.review_adjustment(999,True,self.continuity,"无权审核")

    def test_versions_increment_and_superseded_not_checked(self):
        # 补拍 s1：重度(3) -> 轻度(1)，与 s2 一致，冲突消失
        r=self.db.set_element_state(self.s1,self.injury,"轻度",1,"2026-09-23","补拍：改造型与受伤前衔接",self.continuity)
        self.assertEqual(2,r["version"])
        conflicts=self.db.check_scene(self.scene)
        self.assertEqual([],conflicts)
        versions=self.db.list_state_versions(self.s1)
        self.assertEqual(["superseded","active"],[v["status"] for v in versions])
        self.assertEqual([1,2],[v["version"] for v in versions])
        self.assertEqual("场记",versions[1]["submitted_by_name"])
        self.assertEqual("补拍：改造型与受伤前衔接",versions[1]["reason"])
        self.assertEqual("2026-09-23",versions[1]["shoot_date"])
        # 再补拍回重度，冲突按新版本重新出现，旧冲突仅留档不参与判断
        self.db.set_element_state(self.s1,self.injury,"重度",3,"2026-09-24","导演要求恢复",self.continuity)
        conflicts=self.db.check_scene(self.scene)
        self.assertEqual(1,len(conflicts))
        self.assertEqual(3,conflicts[0]["from_version"]); self.assertEqual(1,conflicts[0]["to_version"])
        archived=self.db.list_conflicts(self.scene,include_resolved=True)
        self.assertIn(False,[bool(c["active"]) for c in archived])

    def test_lock_binds_version_and_reshoot_cascades_relock(self):
        self.db.check_scene(self.scene)
        # 用方案消掉冲突（s2 伤势调整为重度，与叙事顺序前的 s1 一致）后锁定两个镜头
        plan=self.db.propose_adjustment(
            self.db.list_conflicts(self.scene)[0]["id"],"重度",3,"统一为受伤后造型",self.continuity,"2026-09-22")
        self.db.review_adjustment(plan,True,self.reviewer,"通过")
        lock1=self.db.lock_shot(self.s1,self.continuity)
        lock2=self.db.lock_shot(self.s2,self.continuity)
        self.assertEqual(1,lock1["locked_version"]); self.assertEqual(2,lock2["locked_version"])
        # s1 补拍新版本：s1 与同场次 s2 一起退回待锁定
        r=self.db.set_element_state(self.s1,self.injury,"重度",3,"2026-09-25","技术问题重拍同状态",self.continuity)
        self.assertIn(self.s2,r["released_locks"])
        shots={s["id"]:s for s in self.db.snapshot()["shots"]}
        self.assertEqual("relock_pending",shots[self.s1]["status"])
        self.assertEqual("relock_pending",shots[self.s2]["status"])
        self.assertIn("v2",shots[self.s2]["relock_reason"])
        # 旧锁定仍可查，带作废原因
        locks=self.db.list_locks(scene_id=self.scene)
        self.assertEqual(2,len(locks)); self.assertTrue(all(l["released_at"] for l in locks))
        self.assertTrue(all(l["release_reason"] for l in locks))
        # 重新检查后无冲突即可重新锁定
        self.assertEqual([],self.db.check_scene(self.scene))
        relock=self.db.lock_shot(self.s1,self.continuity)
        self.assertEqual(2,relock["locked_version"])

    def test_report_and_conflicts_annotate_basis_versions(self):
        conflicts=self.db.check_scene(self.scene)
        self.assertEqual(1,conflicts[0]["from_version"]); self.assertEqual(1,conflicts[0]["to_version"])
        report=self.db.continuity_report(self.production)
        scene=report["scenes"][0]
        shot1=scene["shots"][0]
        self.assertEqual(1,shot1["states"][0]["version"])
        self.assertEqual("2026-09-20",shot1["states"][0]["shoot_date"])
        plan=self.db.propose_adjustment(conflicts[0]["id"],"重度",3,"统一造型",self.continuity,"2026-09-22")
        self.db.review_adjustment(plan,True,self.reviewer,"通过")
        self.db.lock_shot(self.s1,self.continuity)
        report=self.db.continuity_report(self.production)
        shot1=report["scenes"][0]["shots"][0]
        self.assertEqual(1,shot1["version"])
        self.assertEqual("locked",shot1["status"])
        self.assertEqual(1,shot1["locks"][0]["version"])

if __name__=="__main__": unittest.main()
