from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class DomainError(ValueError):
    """Business rule violation."""


ELEMENT_KINDS = {"character", "costume", "prop", "injury"}
RULES = {"stable", "monotonic", "allowed"}
SHOT_STATUSES = {"planned", "locked", "relock_pending"}


def _valid_shoot_date(value: str) -> str:
    """Normalize and validate a YYYY-MM-DD shooting date."""
    text = (value or "").strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise DomainError("拍摄日必须是 YYYY-MM-DD 格式") from exc


class ContinuityDB:
    """Non-linear film continuity checker with versioned reshoot states."""

    def __init__(self, path: str = "continuity.db") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self._schema()
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              role TEXT NOT NULL CHECK(role IN ('producer','continuity','reviewer'))
            );
            CREATE TABLE IF NOT EXISTS productions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scenes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              production_id INTEGER NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
              scene_number TEXT NOT NULL,
              title TEXT NOT NULL,
              narrative_order INTEGER NOT NULL CHECK(narrative_order > 0),
              UNIQUE(production_id,scene_number),
              UNIQUE(production_id,narrative_order)
            );
            CREATE TABLE IF NOT EXISTS shots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              scene_id INTEGER NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
              shot_code TEXT NOT NULL,
              shoot_order INTEGER NOT NULL CHECK(shoot_order > 0),
              narrative_order INTEGER NOT NULL CHECK(narrative_order > 0),
              description TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','locked','relock_pending')),
              version INTEGER NOT NULL DEFAULT 0,
              relock_reason TEXT NOT NULL DEFAULT '',
              updated_by INTEGER NOT NULL REFERENCES users(id),
              updated_at TEXT NOT NULL,
              UNIQUE(scene_id,shot_code),
              UNIQUE(scene_id,narrative_order)
            );
            CREATE TABLE IF NOT EXISTS elements (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              production_id INTEGER NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
              name TEXT NOT NULL,
              kind TEXT NOT NULL CHECK(kind IN ('character','costume','prop','injury')),
              rule TEXT NOT NULL CHECK(rule IN ('stable','monotonic','allowed')),
              description TEXT NOT NULL DEFAULT '',
              UNIQUE(production_id,name)
            );
            CREATE TABLE IF NOT EXISTS element_transitions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              element_id INTEGER NOT NULL REFERENCES elements(id) ON DELETE CASCADE,
              from_state TEXT NOT NULL,
              to_state TEXT NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              UNIQUE(element_id,from_state,to_state)
            );
            CREATE TABLE IF NOT EXISTS element_state_versions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              shot_id INTEGER NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
              element_id INTEGER NOT NULL REFERENCES elements(id) ON DELETE CASCADE,
              version INTEGER NOT NULL,
              state_value TEXT NOT NULL,
              numeric_value REAL,
              shoot_date TEXT NOT NULL,
              reason TEXT NOT NULL,
              submitted_by INTEGER NOT NULL REFERENCES users(id),
              status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded')),
              created_at TEXT NOT NULL,
              UNIQUE(shot_id,element_id,version)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_state_version_active
              ON element_state_versions(shot_id,element_id) WHERE status='active';
            CREATE TABLE IF NOT EXISTS conflicts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              scene_id INTEGER NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
              element_id INTEGER NOT NULL REFERENCES elements(id) ON DELETE CASCADE,
              from_shot_id INTEGER NOT NULL REFERENCES shots(id),
              to_shot_id INTEGER NOT NULL REFERENCES shots(id),
              from_version_id INTEGER,
              to_version_id INTEGER,
              kind TEXT NOT NULL,
              detail TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','exempted','resolved')),
              active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
              fingerprint TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS adjustment_plans (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              conflict_id INTEGER NOT NULL UNIQUE REFERENCES conflicts(id),
              shot_id INTEGER NOT NULL REFERENCES shots(id),
              element_id INTEGER NOT NULL REFERENCES elements(id),
              new_value TEXT NOT NULL,
              numeric_value REAL,
              shoot_date TEXT NOT NULL DEFAULT '',
              reason TEXT NOT NULL,
              proposed_by INTEGER NOT NULL REFERENCES users(id),
              status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
              reviewed_by INTEGER REFERENCES users(id),
              review_note TEXT NOT NULL DEFAULT '',
              proposed_at TEXT NOT NULL,
              reviewed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS exemptions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              conflict_id INTEGER NOT NULL UNIQUE REFERENCES conflicts(id),
              reason TEXT NOT NULL,
              approved_by INTEGER NOT NULL REFERENCES users(id),
              approved_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shot_locks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              shot_id INTEGER NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
              version INTEGER NOT NULL,
              basis TEXT NOT NULL DEFAULT '{}',
              locked_by INTEGER NOT NULL REFERENCES users(id),
              locked_at TEXT NOT NULL,
              released_at TEXT,
              release_reason TEXT
            );
            """
        )
        self.conn.commit()

    def _has_column(self, table: str, column: str) -> bool:
        return any(row["name"] == column for row in self.conn.execute(f"PRAGMA table_info({table})"))

    def _migrate(self) -> None:
        """Bring databases created by the pre-versioning schema up to date."""
        with self.transaction():
            if not self._has_column("shots", "relock_reason"):
                self.conn.execute("ALTER TABLE shots ADD COLUMN relock_reason TEXT NOT NULL DEFAULT ''")
            if not self._has_column("conflicts", "from_version_id"):
                self.conn.execute("ALTER TABLE conflicts ADD COLUMN from_version_id INTEGER")
                self.conn.execute("ALTER TABLE conflicts ADD COLUMN to_version_id INTEGER")
            if not self._has_column("adjustment_plans", "shoot_date"):
                self.conn.execute("ALTER TABLE adjustment_plans ADD COLUMN shoot_date TEXT NOT NULL DEFAULT ''")
            # Fold legacy single-row element_states into version 1 of the new table.
            legacy = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='element_states'"
            ).fetchone()
            if legacy:
                rows = self.conn.execute("SELECT * FROM element_states").fetchall()
                for row in rows:
                    shot = self.conn.execute("SELECT version FROM shots WHERE id=?", (row["shot_id"],)).fetchone()
                    version = max(1, shot["version"] if shot else 1)
                    exists = self.conn.execute(
                        "SELECT 1 FROM element_state_versions WHERE shot_id=? AND element_id=? AND version=?",
                        (row["shot_id"], row["element_id"], version),
                    ).fetchone()
                    if not exists:
                        self.conn.execute(
                            "INSERT INTO element_state_versions(shot_id,element_id,version,state_value,numeric_value,"
                            "shoot_date,reason,submitted_by,status,created_at) VALUES(?,?,?,?,?,?,?,?,'active',?)",
                            (row["shot_id"], row["element_id"], version, row["state_value"], row["numeric_value"],
                             row["updated_at"][:10], "旧数据迁移", row["updated_by"], row["updated_at"]),
                        )
                self.conn.execute("DROP TABLE element_states")

    # ------------------------------------------------------------------ setup

    def seed_demo(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        producer = self.add_user("制片", "producer")
        continuity = self.add_user("场记", "continuity")
        reviewer = self.add_user("审片", "reviewer")
        production = self.create_production("雨夜追踪", "非线性拍摄出的连续性示例", producer)
        scene = self.add_scene(production, "S01", "巷口相遇", 1)
        s01 = self.add_shot(scene, "S01-01", 2, 1, "角色受伤后", continuity)
        s02 = self.add_shot(scene, "S01-02", 1, 2, "角色尚未受伤", continuity)
        injury = self.add_element(production, "主角左臂伤痕", "injury", "monotonic", "伤痕严重程度只能递增")
        self.set_element_state(s01, injury, "重度", 3, "2026-09-20", "首次拍摄：受伤后造型", continuity)
        self.set_element_state(s02, injury, "轻度", 1, "2026-09-21", "首次拍摄：受伤前造型", continuity)
        self.check_scene(scene)

    def add_user(self, name: str, role: str) -> int:
        if not name.strip() or role not in {"producer", "continuity", "reviewer"}:
            raise DomainError("用户名或角色无效")
        with self.transaction():
            try:
                cur = self.conn.execute("INSERT INTO users(name,role) VALUES(?,?)", (name.strip(), role))
            except sqlite3.IntegrityError as exc:
                raise DomainError("用户名已存在") from exc
        return int(cur.lastrowid)

    def create_production(self, title: str, description: str, user_id: int) -> int:
        user = self.conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or user["role"] != "producer" or not title.strip():
            raise DomainError("只有制片人可以创建项目")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO productions(title,description,created_by,created_at) VALUES(?,?,?,?)",
                (title.strip(), description.strip(), user_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def _production_for_user(self, production_id: int, user_id: int) -> sqlite3.Row:
        user = self.conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise DomainError("用户不存在")
        if user["role"] == "reviewer":
            raise DomainError("审片人员只能审核方案和豁免，不能直接编排")
        return user

    def add_scene(self, production_id: int, scene_number: str, title: str, narrative_order: int) -> int:
        if not self.conn.execute("SELECT 1 FROM productions WHERE id=?", (production_id,)).fetchone():
            raise DomainError("项目不存在")
        if not scene_number.strip() or not title.strip() or narrative_order <= 0:
            raise DomainError("场次参数无效")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO scenes(production_id,scene_number,title,narrative_order) VALUES(?,?,?,?)",
                    (production_id, scene_number.strip(), title.strip(), narrative_order),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("场次编号或叙事顺序重复") from exc
        return int(cur.lastrowid)

    def add_shot(self, scene_id: int, shot_code: str, shoot_order: int, narrative_order: int,
                 description: str, user_id: int) -> int:
        scene = self.conn.execute("SELECT production_id FROM scenes WHERE id=?", (scene_id,)).fetchone()
        if not scene:
            raise DomainError("场次不存在")
        user = self._production_for_user(scene["production_id"], user_id)
        if user["role"] not in {"producer", "continuity"}:
            raise DomainError("无权创建镜头")
        if not shot_code.strip() or shoot_order <= 0 or narrative_order <= 0:
            raise DomainError("镜头参数无效")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO shots(scene_id,shot_code,shoot_order,narrative_order,description,updated_by,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (scene_id, shot_code.strip(), shoot_order, narrative_order, description.strip(), user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("场次内镜头编号或叙事顺序重复") from exc
        return int(cur.lastrowid)

    def add_element(self, production_id: int, name: str, kind: str, rule: str, description: str = "") -> int:
        if not self.conn.execute("SELECT 1 FROM productions WHERE id=?", (production_id,)).fetchone():
            raise DomainError("项目不存在")
        if not name.strip() or kind not in ELEMENT_KINDS or rule not in RULES:
            raise DomainError("连续性元素参数无效")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO elements(production_id,name,kind,rule,description) VALUES(?,?,?,?,?)",
                    (production_id, name.strip(), kind, rule, description.strip()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("项目内元素名称不能重复") from exc
        return int(cur.lastrowid)

    def add_transition(self, element_id: int, from_state: str, to_state: str, note: str = "") -> int:
        element = self.conn.execute("SELECT rule FROM elements WHERE id=?", (element_id,)).fetchone()
        if not element or element["rule"] != "allowed":
            raise DomainError("只有 allowed 规则元素需要配置状态转移")
        if not from_state.strip() or not to_state.strip() or from_state == to_state:
            raise DomainError("状态转移必须包含两个不同状态")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO element_transitions(element_id,from_state,to_state,note) VALUES(?,?,?,?)",
                    (element_id, from_state.strip(), to_state.strip(), note.strip()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("该状态转移已存在") from exc
        return int(cur.lastrowid)

    # ------------------------------------------------------- versioned states

    def _active_state(self, shot_id: int, element_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM element_state_versions WHERE shot_id=? AND element_id=? AND status='active'",
            (shot_id, element_id),
        ).fetchone()

    def _scene_pending_plans(self, scene_id: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM adjustment_plans p JOIN conflicts c ON c.id=p.conflict_id "
            "WHERE c.scene_id=? AND p.status='pending'",
            (scene_id,),
        ).fetchone()[0]

    def _insert_state_version(self, shot_id: int, element_id: int, state_value: str,
                              numeric_value: float | None, shoot_date: str, reason: str,
                              user_id: int) -> int:
        """Supersede the current active version and append the next one. Caller holds the transaction."""
        current = self._active_state(shot_id, element_id)
        version = 1 if current is None else current["version"] + 1
        if current is not None:
            self.conn.execute("UPDATE element_state_versions SET status='superseded' WHERE id=?", (current["id"],))
        cur = self.conn.execute(
            "INSERT INTO element_state_versions(shot_id,element_id,version,state_value,numeric_value,"
            "shoot_date,reason,submitted_by,status,created_at) VALUES(?,?,?,?,?,?,?,?,'active',?)",
            (shot_id, element_id, version, state_value, numeric_value, shoot_date, reason, user_id,
             datetime.now().isoformat()),
        )
        return int(cur.lastrowid)

    def _release_scene_locks(self, scene_id: int, reason: str, exclude_shot_id: int) -> list[int]:
        """Cascade: a reshoot version invalidates every lock in the scene; history stays queryable."""
        released: list[int] = []
        now = datetime.now().isoformat()
        for shot in self.conn.execute(
            "SELECT id FROM shots WHERE scene_id=? AND status='locked' AND id!=?", (scene_id, exclude_shot_id)
        ).fetchall():
            self.conn.execute(
                "UPDATE shot_locks SET released_at=?,release_reason=? WHERE shot_id=? AND released_at IS NULL",
                (now, reason, shot["id"]),
            )
            self.conn.execute(
                "UPDATE shots SET status='relock_pending',relock_reason=?,updated_at=? WHERE id=?",
                (reason, now, shot["id"]),
            )
            released.append(shot["id"])
        return released

    def set_element_state(self, shot_id: int, element_id: int, state_value: str, numeric_value: float | None,
                          shoot_date: str, reason: str, user_id: int) -> dict:
        shot = self.conn.execute("SELECT s.*,sc.production_id FROM shots s JOIN scenes sc ON sc.id=s.scene_id WHERE s.id=?", (shot_id,)).fetchone()
        element = self.conn.execute("SELECT * FROM elements WHERE id=?", (element_id,)).fetchone()
        if not shot or not element or shot["production_id"] != element["production_id"]:
            raise DomainError("镜头与元素不属于同一项目")
        user = self._production_for_user(shot["production_id"], user_id)
        if user["role"] not in {"producer", "continuity"}:
            raise DomainError("无权修改连续性状态")
        if not state_value.strip():
            raise DomainError("状态值不能为空")
        shoot_date = _valid_shoot_date(shoot_date)
        reason = reason.strip()
        if len(reason) < 3:
            raise DomainError("每次状态提交必须写清替换原因")
        if element["rule"] == "monotonic" and numeric_value is None:
            raise DomainError("单调规则必须提供 numeric_value")
        pending = self._scene_pending_plans(shot["scene_id"])
        if pending:
            raise DomainError(f"场次有 {pending} 个调整方案正在审核，审核期间不收新版本")
        released: list[int] = []
        with self.transaction():
            version_row_id = self._insert_state_version(
                shot_id, element_id, state_value.strip(), numeric_value, shoot_date, reason, user_id
            )
            new_version = shot["version"] + 1
            if shot["status"] == "locked":
                # 补拍版本替换已锁定镜头：本镜头解除锁定，同场次镜头一起退回待锁定。
                release_reason = f"镜头 {shot['shot_code']} 提交补拍版本 v{new_version}（{reason}），原锁定作废，待重新锁定"
                self.conn.execute(
                    "UPDATE shot_locks SET released_at=?,release_reason=? WHERE shot_id=? AND released_at IS NULL",
                    (datetime.now().isoformat(), release_reason, shot_id),
                )
                self.conn.execute(
                    "UPDATE shots SET version=?,status='relock_pending',relock_reason=?,updated_by=?,updated_at=? WHERE id=?",
                    (new_version, release_reason, user_id, datetime.now().isoformat(), shot_id),
                )
                released = self._release_scene_locks(shot["scene_id"], release_reason, shot_id)
            else:
                self.conn.execute(
                    "UPDATE shots SET version=?,updated_by=?,updated_at=? WHERE id=?",
                    (new_version, user_id, datetime.now().isoformat(), shot_id),
                )
            self._sync_conflicts(shot["scene_id"])
        return {
            "shot_id": shot_id,
            "element_id": element_id,
            "version": new_version,
            "state_version_id": version_row_id,
            "released_locks": released,
            "conflicts": self.list_conflicts(shot["scene_id"]),
        }

    def list_state_versions(self, shot_id: int, include_superseded: bool = True) -> list[dict]:
        if not self.conn.execute("SELECT 1 FROM shots WHERE id=?", (shot_id,)).fetchone():
            raise DomainError("镜头不存在")
        clause = "" if include_superseded else "AND v.status='active'"
        return [dict(row) for row in self.conn.execute(
            "SELECT v.*,e.name AS element_name,u.name AS submitted_by_name "
            "FROM element_state_versions v JOIN elements e ON e.id=v.element_id JOIN users u ON u.id=v.submitted_by "
            f"WHERE v.shot_id=? {clause} ORDER BY v.element_id,v.version", (shot_id,)
        ).fetchall()]

    # -------------------------------------------------------------- conflicts

    def _detect_conflicts(self, scene_id: int) -> list[dict]:
        scene = self.conn.execute("SELECT * FROM scenes WHERE id=?", (scene_id,)).fetchone()
        if not scene:
            raise DomainError("场次不存在")
        shots = self.conn.execute(
            "SELECT * FROM shots WHERE scene_id=? ORDER BY narrative_order", (scene_id,)
        ).fetchall()
        elements = self.conn.execute("SELECT * FROM elements WHERE production_id=? ORDER BY id", (scene["production_id"],)).fetchall()
        detected: list[dict] = []
        for element in elements:
            sequence = []
            for shot in shots:
                state = self._active_state(shot["id"], element["id"])
                if state:
                    sequence.append((shot, state))
            for (prev_shot, prev), (shot, current) in zip(sequence, sequence[1:]):
                kind = None
                detail = ""
                if element["rule"] == "stable":
                    if current["state_value"] != prev["state_value"]:
                        kind = "state_changed"
                        detail = f"{element['name']} 应为稳定状态，却从 {prev['state_value']} 变为 {current['state_value']}"
                elif element["rule"] == "monotonic":
                    if current["numeric_value"] is None or prev["numeric_value"] is None:
                        kind = "missing_numeric_value"
                        detail = f"{element['name']} 缺少可比较的数值"
                    elif current["numeric_value"] < prev["numeric_value"]:
                        kind = "regression"
                        detail = f"{element['name']} 在叙事顺序中从 {prev['numeric_value']} 回退到 {current['numeric_value']}"
                else:
                    allowed = self.conn.execute(
                        "SELECT 1 FROM element_transitions WHERE element_id=? AND from_state=? AND to_state=?",
                        (element["id"], prev["state_value"], current["state_value"]),
                    ).fetchone()
                    if not allowed:
                        kind = "transition_not_allowed"
                        detail = f"{element['name']} 不允许从 {prev['state_value']} 变为 {current['state_value']}"
                if kind:
                    fingerprint = (
                        f"{scene_id}:{element['id']}:{prev_shot['id']}:{shot['id']}:{kind}"
                        f":v{prev['version']}:v{current['version']}"
                    )
                    detected.append({
                        "scene_id": scene_id, "element_id": element["id"], "element_name": element["name"],
                        "from_shot_id": prev_shot["id"], "to_shot_id": shot["id"], "kind": kind,
                        "detail": detail, "fingerprint": fingerprint,
                        "from_version_id": prev["id"], "to_version_id": current["id"],
                        "from_version": prev["version"], "to_version": current["version"],
                    })
        return detected

    def _sync_conflicts(self, scene_id: int) -> None:
        detected = self._detect_conflicts(scene_id)
        active_fingerprints = {row["fingerprint"] for row in detected}
        for row in self.conn.execute("SELECT * FROM conflicts WHERE scene_id=? AND active=1", (scene_id,)).fetchall():
            if row["fingerprint"] not in active_fingerprints:
                self.conn.execute(
                    "UPDATE conflicts SET active=0,status='resolved',updated_at=? WHERE id=?",
                    (datetime.now().isoformat(), row["id"]),
                )
        for issue in detected:
            existing = self.conn.execute("SELECT * FROM conflicts WHERE fingerprint=?", (issue["fingerprint"],)).fetchone()
            if existing:
                status = "exempted" if existing["status"] == "exempted" else "open"
                self.conn.execute(
                    "UPDATE conflicts SET active=1,status=?,detail=?,from_version_id=?,to_version_id=?,updated_at=? WHERE id=?",
                    (status, issue["detail"], issue["from_version_id"], issue["to_version_id"],
                     datetime.now().isoformat(), existing["id"]),
                )
            else:
                self.conn.execute(
                    "INSERT INTO conflicts(scene_id,element_id,from_shot_id,to_shot_id,from_version_id,to_version_id,"
                    "kind,detail,status,active,fingerprint,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'open',1,?,?,?)",
                    (issue["scene_id"], issue["element_id"], issue["from_shot_id"], issue["to_shot_id"],
                     issue["from_version_id"], issue["to_version_id"], issue["kind"], issue["detail"],
                     issue["fingerprint"], datetime.now().isoformat(), datetime.now().isoformat()),
                )

    def check_scene(self, scene_id: int) -> list[dict]:
        if not self.conn.execute("SELECT 1 FROM scenes WHERE id=?", (scene_id,)).fetchone():
            raise DomainError("场次不存在")
        with self.transaction():
            self._sync_conflicts(scene_id)
        return self.list_conflicts(scene_id)

    def list_conflicts(self, scene_id: int, include_resolved: bool = False) -> list[dict]:
        clause = "" if include_resolved else "AND c.active=1"
        return [dict(row) for row in self.conn.execute(
            "SELECT c.*,e.name AS element_name,fs.shot_code AS from_shot_code,ts.shot_code AS to_shot_code,"
            "fv.version AS from_version,tv.version AS to_version "
            "FROM conflicts c JOIN elements e ON e.id=c.element_id JOIN shots fs ON fs.id=c.from_shot_id JOIN shots ts ON ts.id=c.to_shot_id "
            "LEFT JOIN element_state_versions fv ON fv.id=c.from_version_id "
            "LEFT JOIN element_state_versions tv ON tv.id=c.to_version_id "
            f"WHERE c.scene_id=? {clause} ORDER BY c.id", (scene_id,)
        ).fetchall()]

    # ---------------------------------------------------- plans and exemption

    def propose_adjustment(self, conflict_id: int, new_value: str, numeric_value: float | None,
                           reason: str, user_id: int, shoot_date: str = "") -> int:
        conflict = self.conn.execute("SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
        if not conflict or not conflict["active"]:
            raise DomainError("冲突不存在或已解决")
        if conflict["status"] != "open":
            raise DomainError("已豁免冲突不能提交状态调整方案")
        shot = self.conn.execute("SELECT * FROM shots WHERE id=?", (conflict["to_shot_id"],)).fetchone()
        element = self.conn.execute("SELECT * FROM elements WHERE id=?", (conflict["element_id"],)).fetchone()
        user = self._production_for_user(element["production_id"], user_id)
        if user["role"] not in {"producer", "continuity"}:
            raise DomainError("无权提出调整方案")
        if shot["status"] == "locked":
            raise DomainError("目标镜头已锁定")
        if not new_value.strip() or len(reason.strip()) < 3:
            raise DomainError("新状态和调整理由必须填写")
        if element["rule"] == "monotonic" and numeric_value is None:
            raise DomainError("单调规则调整必须提供 numeric_value")
        shoot_date = _valid_shoot_date(shoot_date) if shoot_date.strip() else datetime.now().date().isoformat()
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO adjustment_plans(conflict_id,shot_id,element_id,new_value,numeric_value,shoot_date,reason,proposed_by,proposed_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (conflict_id, shot["id"], element["id"], new_value.strip(), numeric_value, shoot_date,
                     reason.strip(), user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("该冲突已有调整方案") from exc
        return int(cur.lastrowid)

    def review_adjustment(self, plan_id: int, approve: bool, reviewer_id: int, note: str = "") -> dict:
        reviewer = self.conn.execute("SELECT role FROM users WHERE id=?", (reviewer_id,)).fetchone()
        if not reviewer or reviewer["role"] != "reviewer":
            raise DomainError("只有审片人可以审核调整方案")
        plan = self.conn.execute("SELECT * FROM adjustment_plans WHERE id=?", (plan_id,)).fetchone()
        if not plan or plan["status"] != "pending":
            raise DomainError("调整方案不存在或已审核")
        if plan["proposed_by"] == reviewer_id:
            raise DomainError("提案人不能审核自己的方案")
        shot = self.conn.execute("SELECT * FROM shots WHERE id=?", (plan["shot_id"],)).fetchone()
        if shot["status"] == "locked":
            raise DomainError("目标镜头已锁定")
        with self.transaction():
            status = "approved" if approve else "rejected"
            self.conn.execute(
                "UPDATE adjustment_plans SET status=?,reviewed_by=?,review_note=?,reviewed_at=? WHERE id=?",
                (status, reviewer_id, note.strip(), datetime.now().isoformat(), plan_id),
            )
            new_version = None
            if approve:
                # 审片通过后才按方案生成新版本，审核期间版本保持不变。
                new_version = shot["version"] + 1
                self._insert_state_version(
                    plan["shot_id"], plan["element_id"], plan["new_value"], plan["numeric_value"],
                    plan["shoot_date"] or datetime.now().date().isoformat(),
                    f"调整方案 #{plan_id}：{plan['reason']}", reviewer_id,
                )
                self.conn.execute(
                    "UPDATE shots SET version=?,updated_by=?,updated_at=? WHERE id=?",
                    (new_version, reviewer_id, datetime.now().isoformat(), plan["shot_id"]),
                )
                self.conn.execute(
                    "UPDATE conflicts SET active=0,status='resolved',updated_at=? WHERE id=?",
                    (datetime.now().isoformat(), plan["conflict_id"]),
                )
                self._sync_conflicts(shot["scene_id"])
        return {"plan_id": plan_id, "status": status, "version": new_version,
                "conflicts": self.list_conflicts(shot["scene_id"])}

    def approve_exemption(self, conflict_id: int, reason: str, reviewer_id: int) -> int:
        reviewer = self.conn.execute("SELECT role FROM users WHERE id=?", (reviewer_id,)).fetchone()
        conflict = self.conn.execute("SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
        if not conflict or not conflict["active"] or not reviewer or reviewer["role"] != "reviewer":
            raise DomainError("冲突或审片人无效")
        if len(reason.strip()) < 8:
            raise DomainError("豁免理由至少8个字符")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO exemptions(conflict_id,reason,approved_by,approved_at) VALUES(?,?,?,?)",
                    (conflict_id, reason.strip(), reviewer_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("该冲突已经豁免") from exc
            self.conn.execute("UPDATE conflicts SET status='exempted',updated_at=? WHERE id=?", (datetime.now().isoformat(), conflict_id))
        return int(cur.lastrowid)

    # ------------------------------------------------------------------ locks

    def lock_shot(self, shot_id: int, user_id: int) -> dict:
        shot = self.conn.execute("SELECT s.*,sc.production_id FROM shots s JOIN scenes sc ON sc.id=s.scene_id WHERE s.id=?", (shot_id,)).fetchone()
        if not shot:
            raise DomainError("镜头不存在")
        user = self._production_for_user(shot["production_id"], user_id)
        if user["role"] not in {"producer", "continuity"}:
            raise DomainError("无权锁定镜头")
        if shot["status"] == "locked":
            raise DomainError("镜头已处于锁定状态")
        pending = self._scene_pending_plans(shot["scene_id"])
        if pending:
            raise DomainError(f"场次有 {pending} 个调整方案正在审核，审核期间不能锁定")
        with self.transaction():
            self._sync_conflicts(shot["scene_id"])
            blocking = self.conn.execute(
                "SELECT COUNT(*) FROM conflicts WHERE scene_id=? AND active=1 AND status!='exempted'", (shot["scene_id"],)
            ).fetchone()[0]
            if blocking:
                raise DomainError(f"场次仍有 {blocking} 个未处理冲突，不能锁定")
            basis = {
                str(row["element_id"]): {"version": row["version"], "state_version_id": row["id"]}
                for row in self.conn.execute(
                    "SELECT id,element_id,version FROM element_state_versions WHERE shot_id=? AND status='active'",
                    (shot_id,),
                ).fetchall()
            }
            self.conn.execute(
                "INSERT INTO shot_locks(shot_id,version,basis,locked_by,locked_at) VALUES(?,?,?,?,?)",
                (shot_id, shot["version"], json.dumps(basis, ensure_ascii=False), user_id, datetime.now().isoformat()),
            )
            self.conn.execute(
                "UPDATE shots SET status='locked',relock_reason='',updated_by=?,updated_at=? WHERE id=?",
                (user_id, datetime.now().isoformat(), shot_id),
            )
        return {"shot_id": shot_id, "locked_version": shot["version"]}

    def list_locks(self, shot_id: int | None = None, scene_id: int | None = None) -> list[dict]:
        where, params = "", ()
        if shot_id is not None:
            where, params = "WHERE l.shot_id=?", (shot_id,)
        elif scene_id is not None:
            where, params = "WHERE s.scene_id=?", (scene_id,)
        return [dict(row) for row in self.conn.execute(
            "SELECT l.*,s.shot_code,u.name AS locked_by_name FROM shot_locks l "
            "JOIN shots s ON s.id=l.shot_id JOIN users u ON u.id=l.locked_by "
            f"{where} ORDER BY l.id", params
        ).fetchall()]

    # ---------------------------------------------------------------- reports

    def continuity_report(self, production_id: int) -> dict:
        production = self.conn.execute("SELECT * FROM productions WHERE id=?", (production_id,)).fetchone()
        if not production:
            raise DomainError("项目不存在")
        scenes = []
        for scene in self.conn.execute("SELECT * FROM scenes WHERE production_id=? ORDER BY narrative_order", (production_id,)).fetchall():
            shots = []
            for shot in self.conn.execute("SELECT * FROM shots WHERE scene_id=? ORDER BY narrative_order", (scene["id"],)).fetchall():
                states = [dict(r) for r in self.conn.execute(
                    "SELECT v.id AS state_version_id,v.element_id,e.name AS element_name,v.version,v.state_value,"
                    "v.numeric_value,v.shoot_date,v.reason,u.name AS submitted_by_name "
                    "FROM element_state_versions v JOIN elements e ON e.id=v.element_id JOIN users u ON u.id=v.submitted_by "
                    "WHERE v.shot_id=? AND v.status='active' ORDER BY e.id", (shot["id"],)
                )]
                locks = self.list_locks(shot_id=shot["id"])
                shots.append({**dict(shot), "states": states, "locks": locks})
            conflicts = self.list_conflicts(scene["id"], include_resolved=True)
            scenes.append({**dict(scene), "shots": shots, "conflicts": conflicts})
        return {
            "production": dict(production),
            "elements": [dict(r) for r in self.conn.execute("SELECT * FROM elements WHERE production_id=? ORDER BY id", (production_id,))],
            "scenes": scenes,
            "open_conflicts": sum(1 for scene in scenes for c in scene["conflicts"] if c["active"] and c["status"] == "open"),
            "exempted_conflicts": sum(1 for scene in scenes for c in scene["conflicts"] if c["active"] and c["status"] == "exempted"),
        }

    def snapshot(self) -> dict:
        return {
            "users": [dict(r) for r in self.conn.execute("SELECT id,name,role FROM users ORDER BY id")],
            "productions": [dict(r) for r in self.conn.execute("SELECT * FROM productions ORDER BY id")],
            "scenes": [dict(r) for r in self.conn.execute("SELECT * FROM scenes ORDER BY production_id,narrative_order")],
            "shots": [dict(r) for r in self.conn.execute("SELECT * FROM shots ORDER BY scene_id,narrative_order")],
            "state_versions": [dict(r) for r in self.conn.execute("SELECT * FROM element_state_versions ORDER BY shot_id,element_id,version")],
            "locks": [dict(r) for r in self.conn.execute("SELECT * FROM shot_locks ORDER BY id")],
        }
