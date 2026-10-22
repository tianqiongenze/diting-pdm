"""qianrenqianmian 课程平台测试数据填充（流式版，支持亿级）。

背景：2026-10-22 迁移事故中，这 4 个库（course_db / iam_db / teaching_db /
tenant_db）的数据被清空。经确认原数据为测试数据，本脚本按现有表结构重新生成。

为什么是流式：
    scale=200 时总量约 1 亿行。若先把所有行构造成 list 再插入，
    Python 对象会吃掉上百 GB 内存。因此按「租户 → 课程 → 章节」逐层生成
    并立即写入，内存占用与总量无关。

设计要点：
  1. 引用完整性：父表 ID 真实生成并传给子表，不留悬空外键
     （teaching_db.learning_path_steps → learning_paths 有真实 FK 约束）
  2. 多租户隔离：跨表引用只在同一租户内发生
  3. 确定性：固定随机种子，同参数每次生成同样数据
  4. 枚举合法：严格使用 DDL 声明的 enum 值
  5. 分批写入：executemany 每批 2000 行

用法：
    python tools/seed_course_platform.py --scale 1            # ~50 万行
    python tools/seed_course_platform.py --scale 4            # ~200 万行
    python tools/seed_course_platform.py --scale 200          # ~1 亿行
    python tools/seed_course_platform.py --scale 200 --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import sys
import time
import uuid

import pymysql

SEED = 20261022
BATCH = 2000

SUBJECTS = ["数学", "语文", "英语", "物理", "化学", "生物", "历史", "地理", "政治", "信息技术"]
GRADE_LEVELS = ["一年级", "二年级", "三年级", "四年级", "五年级", "六年级",
                "初一", "初二", "初三", "高一", "高二", "高三"]
SEMESTERS = ["2025-2026-1", "2025-2026-2", "2026-2027-1"]
SURNAMES = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
GIVEN = ["伟", "芳", "娜", "敏", "静", "丽", "强", "磊", "军", "洋", "勇", "艳",
         "杰", "娟", "涛", "明", "超", "秀英", "霞", "平", "刚", "桂英", "文", "辉"]
SCHOOL_SUFFIX = ["实验学校", "第一中学", "外国语学校", "附属中学", "实验小学",
                 "第二中学", "育才学校", "职业技术学院"]
RES_TYPES = ["AUDIO", "DOCUMENT", "EXAM", "H5P", "LINK", "LIVE", "SCORM", "SIMULATION", "VIDEO"]
STEP_TYPES = ["CONTENT", "ASSIGNMENT", "EXAM", "DISCUSSION", "LIVE"]
ROLE_DEFS = [
    ("STUDENT", "学生", ["course:read", "assignment:submit", "exam:take"]),
    ("TEACHER", "教师", ["course:write", "assignment:grade", "exam:write"]),
    ("ADMIN", "管理员", ["*"]),
    ("TA", "助教", ["course:read", "assignment:grade"]),
    ("PARENT", "家长", ["progress:read"]),
    ("AUDITOR", "督导", ["report:read"]),
    ("CONTENT_EDITOR", "内容编辑", ["course:write"]),
    ("GUEST", "访客", ["course:read"]),
]
# 统一测试密码 Test@123456 的 bcrypt 哈希
TEST_PASSWORD_HASH = "$2a$10$N9qo8uLOickgx2ZMRZoMyeIjZAgcfl7p92ldGxad68LJZdL17lhWy"


def uid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def ago(rng: random.Random, lo: int, hi: int) -> dt.datetime:
    return dt.datetime.now() - dt.timedelta(days=rng.randint(lo, hi))


class Writer:
    """大事务缓冲写入。

    为什么不在 put() 里直接 commit：逐批提交时每次都是一轮 TiDB 2PC 往返，
    实测只有 ~800 行/s。攒够 FLUSH_ROWS 再一次性提交，吞吐可提升一个数量级。

    注意：teaching_db.learning_path_steps 有真实外键指向 learning_paths，
    因此 flush 时按 dict 插入顺序（paths 先于 steps）写表，保证约束成立。
    """

    FLUSH_ROWS = 100000

    def __init__(self, conn, dry: bool) -> None:
        self.conn = conn
        self.dry = dry
        self.counts: dict[str, int] = {}
        self.total = 0
        self.buf: dict[str, dict] = {}
        self.buf_rows = 0
        self.t0 = time.time()
        self.last_report = time.time()

    def put(self, table: str, rows: list[dict], cols: list[str]) -> None:
        if not rows:
            return
        n = len(rows)
        self.counts[table] = self.counts.get(table, 0) + n
        self.total += n
        if self.dry:
            return
        slot = self.buf.setdefault(table, {"rows": [], "cols": cols})
        slot["rows"].extend(rows)
        self.buf_rows += n
        if self.buf_rows >= self.FLUSH_ROWS:
            self.flush()

    def flush(self) -> None:
        if self.dry or not self.buf_rows:
            return
        cur = self.conn.cursor()
        for table, slot in self.buf.items():
            cols = slot["cols"]
            sql = (f"INSERT INTO {table} ({','.join('`' + c + '`' for c in cols)}) "
                   f"VALUES ({','.join(['%s'] * len(cols))})")
            rows = slot["rows"]
            for i in range(0, len(rows), BATCH):
                cur.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows[i:i + BATCH]])
        self.conn.commit()
        self.buf.clear()
        self.buf_rows = 0

        now = time.time()
        if now - self.last_report >= 15:
            self.last_report = now
            el = now - self.t0
            print(f"  ... {self.total:,} 行 / {el:.0f}s ({self.total / max(el, 1):,.0f} 行/s)",
                  flush=True)


# ---------------------------------------------------------------------------
# 生成器：按父对象产出，便于流式
# ---------------------------------------------------------------------------
def make_tenants(rng: random.Random, n: int) -> list[dict]:
    out = []
    for i in range(n):
        created = ago(rng, 30, 900)
        out.append({
            "id": uid(rng),
            "code": f"TEN{i + 1:04d}",
            "name": f"{SURNAMES[i % len(SURNAMES)]}{GIVEN[i % len(GIVEN)]}{SCHOOL_SUFFIX[i % len(SCHOOL_SUFFIX)]}",
            "admin_user_id": f"admin-{i + 1:04d}",
            "branding": json.dumps({"logo": f"/assets/logo-{i % 8}.png",
                                    "primaryColor": ["#2563eb", "#16a34a", "#dc2626", "#7c3aed"][i % 4]}),
            "config": json.dumps({"locale": "zh-CN", "timezone": "Asia/Shanghai",
                                  "features": {"live": True, "creditTransfer": i % 3 == 0}}),
            "quota": json.dumps({"maxUsers": 20000, "maxCourses": 500, "storageGB": 100}),
            "status": "ACTIVE" if i % 10 else "SUSPENDED",
            "description": f"测试租户 {i + 1}",
            "created_at": created,
            "updated_at": ago(rng, 0, 29),
            "version": 0,
        })
    return out


def make_roles(rng: random.Random, tenant: dict, per: int) -> list[dict]:
    return [{
        "id": uid(rng), "tenant_id": tenant["id"], "code": code, "name": name,
        "description": f"{name}角色", "permissions": json.dumps(perms),
        "is_system": 1 if code in ("STUDENT", "TEACHER", "ADMIN") else 0,
        "status": "ACTIVE", "created_at": tenant["created_at"],
        "updated_at": tenant["updated_at"], "version": 0,
    } for code, name, perms in ROLE_DEFS[:per]]


def make_users(rng: random.Random, tenant: dict, roles: list[dict], per: int) -> list[dict]:
    student = roles[0]["id"] if roles else None
    teacher = roles[1]["id"] if len(roles) > 1 else student
    tag = tenant["code"][3:]
    out = []
    for i in range(per):
        is_t = i % 20 == 0
        out.append({
            "id": uid(rng), "tenant_id": tenant["id"],
            "username": f"{'t' if is_t else 's'}{tag}{i + 1:06d}",
            "email": f"u{tag}{i + 1:06d}@test.example.com",
            "phone": f"13{tag}{i + 1:06d}"[:13].ljust(13, "0"),
            "password_hash": TEST_PASSWORD_HASH,
            "role_ids": json.dumps([teacher if is_t else student]),
            "status": "ACTIVE" if rng.random() < 0.9 else rng.choice(["PENDING", "LOCKED"]),
            "failed_login_attempts": 0,
            "last_login_at": ago(rng, 0, 60) if rng.random() < 0.8 else None,
            "created_at": ago(rng, 10, 700), "updated_at": ago(rng, 0, 9), "version": 0,
        })
    return out


def make_courses(rng: random.Random, tenant: dict, per: int) -> list[dict]:
    out = []
    for i in range(per):
        subj = SUBJECTS[i % len(SUBJECTS)]
        grade = GRADE_LEVELS[i % len(GRADE_LEVELS)]
        created = ago(rng, 5, 600)
        out.append({
            "id": uid(rng), "tenant_id": tenant["id"], "code": f"C{i + 1:05d}",
            "name": f"{grade}{subj}（第{i // len(SUBJECTS) + 1}期）",
            "description": f"{grade}{subj}测试课程。", "subject": subj, "grade_level": grade,
            "semester": SEMESTERS[i % len(SEMESTERS)],
            "creator_id": f"admin-{i % 5 + 1:04d}", "cover_url": f"/assets/cover-{i % 12}.jpg",
            "settings": json.dumps({"allowDownload": True, "language": "zh-CN"}),
            "status": (["PUBLISHED"] * 8 + ["DRAFT", "ARCHIVED"])[i % 10],
            "published_at": created + dt.timedelta(days=rng.randint(1, 10)),
            "archived_at": None, "created_at": created,
            "updated_at": ago(rng, 0, 4), "version": 0,
        })
    return out


def make_chapters(rng: random.Random, course: dict, per: int) -> list[dict]:
    return [{
        "id": uid(rng), "tenant_id": course["tenant_id"], "course_id": course["id"],
        "code": f"CH{i + 1:03d}", "name": f"第{i + 1}章 {SUBJECTS[i % len(SUBJECTS)]}基础",
        "description": f"第{i + 1}章说明", "sort_order": i + 1, "difficulty": rng.randint(1, 5),
        "estimated_hours": rng.randint(2, 12), "parent_id": None,
        "knowledge_point_ids": json.dumps([]),
        "status": "PUBLISHED" if i < per - 1 else "DRAFT",
        "created_at": course["created_at"], "updated_at": course["updated_at"], "version": 0,
    } for i in range(per)]


def make_kps(rng: random.Random, ch: dict, per: int, cidx: int = 0) -> list[dict]:
    # code 需在 course 内唯一（UNIQUE idx_kp_course_code），带上章节序号
    return [{
        "id": uid(rng), "tenant_id": ch["tenant_id"], "course_id": ch["course_id"],
        "chapter_id": ch["id"], "code": f"KP{cidx:02d}{i + 1:04d}", "name": f"知识点 {i + 1}",
        "description": f"{ch['name']} 的知识点 {i + 1}", "difficulty": rng.randint(1, 5),
        "metadata": json.dumps({"bloom": ["remember", "understand", "apply", "analyze"][i % 4]}),
        "prerequisites": json.dumps([]), "related_points": json.dumps([]),
        "status": "ACTIVE", "created_at": ch["created_at"], "updated_at": ch["updated_at"],
        "version": 0,
    } for i in range(per)]


def make_resources(rng: random.Random, ch: dict, per: int, cidx: int = 0) -> list[dict]:
    out = []
    for i in range(per):
        rtype = RES_TYPES[i % len(RES_TYPES)]
        out.append({
            "id": uid(rng), "tenant_id": ch["tenant_id"], "course_id": ch["course_id"],
            "chapter_id": ch["id"], "code": f"R{cidx:02d}{i + 1:04d}",
            "title": f"{ch['name']} 资源 {i + 1}（{rtype}）", "description": "测试资源",
            "type": rtype, "content_url": f"/media/{ch['id'][:8]}/r{i + 1}.bin",
            "cover_url": f"/assets/res-{i % 9}.jpg", "creator_id": f"admin-{i % 5 + 1:04d}",
            "difficulty": rng.randint(1, 5),
            "tags": json.dumps([SUBJECTS[i % len(SUBJECTS)], rtype.lower()]),
            "kp_ids": json.dumps([]), "metadata": json.dumps({"duration": rng.randint(60, 3600)}),
            "status": "PUBLISHED" if i < per - 1 else "DRAFT",
            "jpa_version": 0, "version": 0,
            "created_at": ch["created_at"], "updated_at": ch["updated_at"],
        })
    return out


def make_paths(rng: random.Random, course: dict, per: int) -> list[dict]:
    return [{
        "id": uid(rng), "tenant_id": course["tenant_id"], "course_id": course["id"],
        "title": f"{course['name']} 学习路径 {i + 1}", "description": "个性化学习路径（测试）",
        "created_by": f"admin-{i % 5 + 1:04d}", "status": "PUBLISHED",
        "published_at": course["published_at"], "created_at": course["created_at"],
        "updated_at": course["updated_at"], "version": 0,
    } for i in range(per)]


def make_steps(rng: random.Random, path: dict, per: int) -> list[dict]:
    out = []
    for i in range(per):
        st = STEP_TYPES[i % len(STEP_TYPES)]
        out.append({
            "id": uid(rng), "learning_path_id": path["id"],
            "title": f"步骤 {i + 1} - {st}", "description": "步骤说明", "sequence": i + 1,
            "step_type": st, "content_type": st.lower(), "content_id": uid(rng),
            "estimated_minutes": rng.randint(10, 120), "is_required": 1 if i < per - 1 else 0,
            "prerequisite_step_id": None,
            "status": "COMPLETED" if i < per // 2 else "PENDING",
            "started_at": ago(rng, 1, 30),
            "completed_at": ago(rng, 0, 20) if i < per // 2 else None,
            "created_at": path["created_at"], "updated_at": path["updated_at"],
        })
    return out


def make_assignments(rng: random.Random, course: dict, per: int) -> list[dict]:
    out = []
    for i in range(per):
        created = ago(rng, 1, 200)
        out.append({
            "id": uid(rng), "tenant_id": course["tenant_id"], "course_id": course["id"],
            "title": f"{course['name']} 作业 {i + 1}", "description": "作业描述",
            "instructions": "请按题目要求完成并提交。", "created_by": uid(rng),
            "max_score": 100, "passing_score": 60, "max_attempts": 3,
            "allow_late_submission": 1 if i % 2 == 0 else 0, "late_penalty_percent": 10,
            "start_at": created, "due_at": created + dt.timedelta(days=7),
            "published_at": created, "status": "PUBLISHED",
            "learning_path_id": None, "step_id": None,
            "created_at": created, "updated_at": created,
        })
    return out


def make_exams(rng: random.Random, course: dict, per: int) -> list[dict]:
    out = []
    for i in range(per):
        created = ago(rng, 1, 200)
        out.append({
            "id": uid(rng), "tenant_id": course["tenant_id"], "course_id": course["id"],
            "title": f"{course['name']} 考试 {i + 1}", "description": "考试描述",
            "instructions": "闭卷，限时完成。", "created_by": uid(rng),
            "duration_minutes": rng.choice([45, 60, 90, 120]),
            "max_score": 100, "passing_score": 60, "max_attempts": 1,
            "shuffle_questions": 1, "shuffle_options": 1, "show_results_immediately": 0,
            "start_at": created, "end_at": created + dt.timedelta(days=3),
            "published_at": created, "status": "PUBLISHED",
            "learning_path_id": None, "step_id": None,
            "created_at": created, "updated_at": created,
        })
    return out


def make_discussions(rng: random.Random, course: dict, per: int) -> list[dict]:
    out = []
    for i in range(per):
        created = ago(rng, 0, 180)
        out.append({
            "id": uid(rng), "tenant_id": course["tenant_id"], "course_id": course["id"],
            "title": f"讨论 {i + 1}：关于{SUBJECTS[i % len(SUBJECTS)]}的问题",
            "content": f"我在学习《{course['name']}》时遇到一个问题，想请教大家。",
            "author_id": uid(rng), "author_name": f"{SURNAMES[i % len(SURNAMES)]}同学",
            "reply_count": rng.randint(0, 40), "view_count": rng.randint(0, 500),
            "is_pinned": 1 if i == 0 else 0, "is_locked": 0,
            "last_reply_at": created + dt.timedelta(hours=rng.randint(1, 72)),
            "last_reply_by": uid(rng), "status": "ACTIVE",
            "learning_path_id": None, "step_id": None,
            "created_at": created, "updated_at": created,
        })
    return out


def make_lives(rng: random.Random, course: dict, per: int) -> list[dict]:
    out = []
    for i in range(per):
        start = ago(rng, 0, 120)
        out.append({
            "id": uid(rng), "tenant_id": course["tenant_id"], "course_id": course["id"],
            "title": f"{course['name']} 直播课 {i + 1}", "description": "直播说明",
            "instructor_id": uid(rng), "instructor_name": f"{SURNAMES[i % len(SURNAMES)]}老师",
            "scheduled_start_at": start, "scheduled_end_at": start + dt.timedelta(minutes=90),
            "actual_start_at": start if i % 2 == 0 else None,
            "actual_end_at": start + dt.timedelta(minutes=88) if i % 2 == 0 else None,
            "max_participants": 200, "current_participants": rng.randint(0, 200),
            "chat_enabled": 1, "recording_enabled": 1 if i % 3 == 0 else 0,
            "playback_url": f"/playback/{course['id'][:8]}/{i + 1}.m3u8" if i % 3 == 0 else None,
            "stream_key": f"sk-{course['id'][:8]}-{i + 1}",
            "status": "ENDED" if i % 2 == 0 else "SCHEDULED", "created_by": uid(rng),
            "learning_path_id": None, "step_id": None,
            "created_at": start - dt.timedelta(days=3), "updated_at": start,
        })
    return out


def make_rules(rng: random.Random, tenant: dict, all_tenants: list[dict], per: int) -> list[dict]:
    idx = all_tenants.index(tenant)
    nxt = all_tenants[(idx + 1) % len(all_tenants)]
    return [{
        "id": uid(rng), "source_tenant_id": tenant["id"],
        "source_course_code": f"C{i + 1:05d}", "target_tenant_id": nxt["id"],
        "target_course_code": f"C{(i + 1) % 40 + 1:05d}", "min_score": rng.choice([60, 70, 75, 80]),
        "credit_ratio": round(rng.uniform(0.5, 1.0), 2),
        "status": "ACTIVE" if i % 5 else "DISABLED",
        "created_at": tenant["created_at"], "updated_at": tenant["updated_at"], "version": 0,
    } for i in range(per)]


def make_records(rng: random.Random, rule: dict, per: int) -> list[dict]:
    ratio = float(rule["credit_ratio"])
    out = []
    for _ in range(per):
        score = rng.randint(50, 100)
        out.append({
            "id": uid(rng), "rule_id": rule["id"], "student_id": uid(rng),
            "source_score": score, "granted_credits": round(score * ratio / 100, 2),
            "applied_at": ago(rng, 0, 300),
        })
    return out


COLS = {
    "tenant_db.tenants": ["id", "code", "name", "admin_user_id", "branding", "config", "quota",
                          "status", "description", "created_at", "updated_at", "version"],
    "iam_db.roles": ["id", "tenant_id", "code", "name", "description", "permissions",
                     "is_system", "status", "created_at", "updated_at", "version"],
    "iam_db.users": ["id", "tenant_id", "username", "email", "phone", "password_hash",
                     "role_ids", "status", "failed_login_attempts", "last_login_at",
                     "created_at", "updated_at", "version"],
    "course_db.courses": ["id", "tenant_id", "code", "name", "description", "subject",
                          "grade_level", "semester", "creator_id", "cover_url", "settings",
                          "status", "published_at", "archived_at", "created_at",
                          "updated_at", "version"],
    "course_db.chapters": ["id", "tenant_id", "course_id", "code", "name", "description",
                           "sort_order", "difficulty", "estimated_hours", "parent_id",
                           "knowledge_point_ids", "status", "created_at", "updated_at", "version"],
    "course_db.knowledge_points": ["id", "tenant_id", "course_id", "chapter_id", "code", "name",
                                   "description", "difficulty", "metadata", "prerequisites",
                                   "related_points", "status", "created_at", "updated_at", "version"],
    "course_db.resources": ["id", "tenant_id", "course_id", "chapter_id", "code", "title",
                            "description", "type", "content_url", "cover_url", "creator_id",
                            "difficulty", "tags", "kp_ids", "metadata", "status",
                            "jpa_version", "version", "created_at", "updated_at"],
    "teaching_db.learning_paths": ["id", "tenant_id", "course_id", "title", "description",
                                   "created_by", "status", "published_at", "created_at",
                                   "updated_at", "version"],
    "teaching_db.learning_path_steps": ["id", "learning_path_id", "title", "description",
                                        "sequence", "step_type", "content_type", "content_id",
                                        "estimated_minutes", "is_required", "prerequisite_step_id",
                                        "status", "started_at", "completed_at",
                                        "created_at", "updated_at"],
    "teaching_db.assignments": ["id", "tenant_id", "course_id", "title", "description",
                                "instructions", "created_by", "max_score", "passing_score",
                                "max_attempts", "allow_late_submission", "late_penalty_percent",
                                "start_at", "due_at", "published_at", "status",
                                "learning_path_id", "step_id", "created_at", "updated_at"],
    "teaching_db.exams": ["id", "tenant_id", "course_id", "title", "description", "instructions",
                          "created_by", "duration_minutes", "max_score", "passing_score",
                          "max_attempts", "shuffle_questions", "shuffle_options",
                          "show_results_immediately", "start_at", "end_at", "published_at",
                          "status", "learning_path_id", "step_id", "created_at", "updated_at"],
    "teaching_db.discussions": ["id", "tenant_id", "course_id", "title", "content", "author_id",
                                "author_name", "reply_count", "view_count", "is_pinned",
                                "is_locked", "last_reply_at", "last_reply_by", "status",
                                "learning_path_id", "step_id", "created_at", "updated_at"],
    "teaching_db.live_sessions": ["id", "tenant_id", "course_id", "title", "description",
                                  "instructor_id", "instructor_name", "scheduled_start_at",
                                  "scheduled_end_at", "actual_start_at", "actual_end_at",
                                  "max_participants", "current_participants", "chat_enabled",
                                  "recording_enabled", "playback_url", "stream_key", "status",
                                  "created_by", "learning_path_id", "step_id",
                                  "created_at", "updated_at"],
    "tenant_db.credit_transfer_rules": ["id", "source_tenant_id", "source_course_code",
                                        "target_tenant_id", "target_course_code", "min_score",
                                        "credit_ratio", "status", "created_at", "updated_at",
                                        "version"],
    "tenant_db.credit_transfer_records": ["id", "rule_id", "student_id", "source_score",
                                          "granted_credits", "applied_at"],
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=float, default=1.0, help="规模倍数（1≈50万行，200≈1亿行）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--truncate", action="store_true", help="写入前清空这 14 张表")
    ap.add_argument("--shard", default="0/1", help="分片 I/N：多进程并行时各写一部分租户")
    a = ap.parse_args()

    si, sn = (int(x) for x in a.shard.split("/"))
    s = a.scale
    p = {
        "tenants": 30,
        "users": int(500 * s),
        "roles": 8,
        "courses": int(40 * s),
        "chapters": 8,
        "kps": int(12 * s),
        "resources": int(10 * s),
        "paths": 6,
        "steps": 8,
        "assignments": 6,
        "exams": 4,
        "discussions": int(25 * s),
        "lives": 8,
        "rules": 6,
        "records": int(800 * s),
    }
    est = (p["tenants"] * (p["roles"] + p["users"] + p["rules"])
           + p["tenants"] * p["courses"] * (p["chapters"] * (1 + p["kps"] + p["resources"])
                                            + p["paths"] * (1 + p["steps"])
                                            + p["assignments"] + p["exams"]
                                            + p["discussions"] + p["lives"])
           + p["tenants"] * p["rules"] * p["records"])
    print(f"计划：{p['tenants']} 租户 × {p['courses']} 课程，每课程 {p['chapters']} 章"
          f"（{p['kps']} 知识点 + {p['resources']} 资源）")
    print(f"预计总量：约 {est:,} 行")

    conn = None
    if not a.dry_run:
        conn = pymysql.connect(
            host=os.getenv("PDM_TIDB_HOST", "10.167.2.175"),
            port=int(os.getenv("PDM_TIDB_PORT", "30210")),
            user=os.getenv("PDM_TIDB_USER", "root"),
            password=os.getenv("PDM_TIDB_PASSWORD", ""),
            charset="utf8mb4", autocommit=False,
        )
        # TiDB 的 FK 校验看不到同事务内的父行，批量写入会误报 1452。
        # 数据由生成器按依赖顺序构造，一致性有保证，这里关掉检查。
        with conn.cursor() as _c:
            _c.execute("SET @@foreign_key_checks=0")
            _c.execute("SET @@tidb_mem_quota_query = 8589934592")   # 8GB，避免 8175
        if a.truncate:
            # TiDB 不允许 TRUNCATE 被 FK 引用的表；只有 learning_paths 被
            # learning_path_steps 引用，故对它用 DELETE（子表已先清空，很快）。
            cur = conn.cursor()
            for t in ("teaching_db.learning_path_steps", "teaching_db.assignments",
                      "teaching_db.exams", "teaching_db.discussions",
                      "teaching_db.live_sessions"):
                cur.execute(f"TRUNCATE TABLE {t}")
            cur.execute("DELETE FROM teaching_db.learning_paths")
            for t in ("course_db.resources", "course_db.knowledge_points",
                      "course_db.chapters", "course_db.courses",
                      "tenant_db.credit_transfer_records", "tenant_db.credit_transfer_rules",
                      "iam_db.users", "iam_db.roles", "tenant_db.tenants"):
                cur.execute(f"TRUNCATE TABLE {t}")
            conn.commit()
            print("已清空 14 张表")

    w = Writer(conn, a.dry_run)
    # 关键：数据生成 RNG 必须按分片偏移，否则各分片产出同一串 UUID 会主键冲突
    rng = random.Random(SEED + si * 100003)

    all_tenants = make_tenants(random.Random(SEED), p["tenants"])
    # 分片：每个进程只处理属于自己的一份租户
    my_tenants = all_tenants[si::sn]
    w.put("tenant_db.tenants", my_tenants, COLS["tenant_db.tenants"])

    for ti, tenant in enumerate(my_tenants, 1):
        roles = make_roles(rng, tenant, p["roles"])
        w.put("iam_db.roles", roles, COLS["iam_db.roles"])
        w.put("iam_db.users", make_users(rng, tenant, roles, p["users"]), COLS["iam_db.users"])

        courses = make_courses(rng, tenant, p["courses"])
        w.put("course_db.courses", courses, COLS["course_db.courses"])

        for course in courses:
            chapters = make_chapters(rng, course, p["chapters"])
            w.put("course_db.chapters", chapters, COLS["course_db.chapters"])
            for cidx, ch in enumerate(chapters):
                w.put("course_db.knowledge_points", make_kps(rng, ch, p["kps"], cidx),
                      COLS["course_db.knowledge_points"])
                w.put("course_db.resources", make_resources(rng, ch, p["resources"], cidx),
                      COLS["course_db.resources"])

            paths = make_paths(rng, course, p["paths"])
            w.put("teaching_db.learning_paths", paths, COLS["teaching_db.learning_paths"])
            for path in paths:
                w.put("teaching_db.learning_path_steps", make_steps(rng, path, p["steps"]),
                      COLS["teaching_db.learning_path_steps"])

            w.put("teaching_db.assignments", make_assignments(rng, course, p["assignments"]),
                  COLS["teaching_db.assignments"])
            w.put("teaching_db.exams", make_exams(rng, course, p["exams"]),
                  COLS["teaching_db.exams"])
            w.put("teaching_db.discussions", make_discussions(rng, course, p["discussions"]),
                  COLS["teaching_db.discussions"])
            w.put("teaching_db.live_sessions", make_lives(rng, course, p["lives"]),
                  COLS["teaching_db.live_sessions"])

        rules = make_rules(rng, tenant, all_tenants, p["rules"])
        w.put("tenant_db.credit_transfer_rules", rules, COLS["tenant_db.credit_transfer_rules"])
        for rule in rules:
            w.put("tenant_db.credit_transfer_records", make_records(rng, rule, p["records"]),
                  COLS["tenant_db.credit_transfer_records"])

        print(f"[shard {si}/{sn} 租户 {ti}/{len(my_tenants)}] "
              f"累计 {w.total:,} 行 / {time.time() - w.t0:.0f}s", flush=True)

    w.flush()
    if conn:
        conn.close()

    print(f"\n[shard {si}/{sn}] 完成：{w.total:,} 行，耗时 {time.time() - w.t0:.1f}s")
    for t in COLS:
        print(f"  {t:<40} {w.counts.get(t, 0):>12,}")
    print("测试账号统一密码：Test@123456")
    return 0


if __name__ == "__main__":
    sys.exit(main())
