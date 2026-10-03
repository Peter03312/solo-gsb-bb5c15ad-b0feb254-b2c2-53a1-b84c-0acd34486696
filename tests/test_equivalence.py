"""课程互认组：规划引擎单元测试与 HTTP API 集成测试（独立临时数据库）。"""
import os

import pytest
from fastapi.testclient import TestClient

from app.planner import Course, PlanError, build_plan


def C(code, credits, seasons=("SPRING", "FALL"), groups=(), coreqs=()):
    return Course(code, code, credits, frozenset(seasons),
                  tuple(tuple(g) for g in groups), tuple(coreqs))


def cat(*courses):
    return {c.code: c for c in courses}


def codes(result):
    return [[c["code"] for c in s["courses"]] for s in result["semesters"]]


# ---------------------------------------------------------------- 引擎：目标课程互认

def test_target_satisfied_by_completed_equivalent():
    catalog = cat(C("CS201", 3), C("CS202", 3))
    r = build_plan(catalog, {"CS202"}, ["CS201"], "2026-SPRING", 10, 4,
                   equiv_groups=[["CS201", "CS202"]])
    # 已修同组课程视为已满足，不重复排课
    assert r["semesters"] == []
    assert r["completion_semester"] is None


def test_target_substitution_picks_actual_course():
    catalog = cat(C("CS201", 3, seasons=("FALL",)), C("CS202", 3))
    r = build_plan(catalog, set(), ["CS201"], "2026-SPRING", 10, 4,
                   equiv_groups=[["CS201", "CS202"]])
    # 未修时择一实际修读：CS202 春季开课可更早完成，方案列出实际修读课程
    assert codes(r) == [["CS202"]]
    assert r["completion_semester"] == "2026-SPRING"


def test_target_without_groups_keeps_legacy_behavior():
    catalog = cat(C("CS201", 3, seasons=("FALL",)), C("CS202", 3))
    r = build_plan(catalog, set(), ["CS201"], "2026-SPRING", 10, 4)
    assert codes(r) == [[], ["CS201"]]  # 未声明互认组：只能修读指定课程


def test_lexicographic_prefers_smaller_code_substitute():
    catalog = cat(C("B", 3), C("A", 3))
    r = build_plan(catalog, set(), ["B"], "2026-SPRING", 10, 4,
                   equiv_groups=[["A", "B"]])
    # 同完成学期按实际修读课程代码序列字典序取优
    assert codes(r) == [["A"]]


def test_multiple_targets_in_same_group_satisfied_by_one_course():
    catalog = cat(C("T1", 3), C("T2", 3))
    r = build_plan(catalog, set(), ["T1", "T2"], "2026-SPRING", 10, 4,
                   equiv_groups=[["T1", "T2"]])
    # 同组课程被多个目标引用时一次修读即可满足
    assert codes(r) == [["T1"]]
    assert r["total_credits"] == 3


def test_substitute_respects_own_seasons():
    catalog = cat(C("T", 3), C("A", 3, seasons=("FALL",)))
    r = build_plan(catalog, set(), ["T"], "2026-SPRING", 10, 4,
                   equiv_groups=[["A", "T"]])
    # 替代课自身仅 FALL 开课，不如直接修 T 早 -> 仍选 T
    assert codes(r) == [["T"]]


def test_substitute_brings_own_coreqs():
    catalog = cat(
        C("T", 3, seasons=("FALL",)),
        C("A", 3, coreqs=("AL",)), C("AL", 1),
    )
    r = build_plan(catalog, set(), ["T"], "2026-SPRING", 10, 4,
                   equiv_groups=[["A", "T"]])
    # 选择替代课 A：其同修课 AL 须同学期修读
    assert codes(r) == [["A", "AL"]]
    assert r["completion_semester"] == "2026-SPRING"


# ---------------------------------------------------------------- 引擎：先修组互认

def test_prereq_satisfied_by_completed_equivalent():
    catalog = cat(C("P", 3), C("Q", 3), C("X", 3, groups=(("P",),)))
    r = build_plan(catalog, {"Q"}, ["X"], "2026-SPRING", 10, 4,
                   equiv_groups=[["P", "Q"]])
    assert codes(r) == [["X"]]


def test_prereq_substitute_scheduled_before_dependent():
    catalog = cat(
        C("P", 3, groups=(("R",),)), C("R", 3),
        C("Q", 3),
        C("X", 3, groups=(("P",),)),
    )
    r = build_plan(catalog, set(), ["X"], "2026-SPRING", 10, 4,
                   equiv_groups=[["P", "Q"]])
    # 替代先修 Q 无需再修 R，且必须排在被依赖课程 X 之前
    assert codes(r) == [["Q"], ["X"]]
    assert r["completion_semester"] == "2026-FALL"


def test_shared_equivalent_satisfies_multiple_prereq_groups():
    catalog = cat(
        C("P1", 3), C("P2", 3),
        C("X", 3, groups=(("P1",), ("P2",))),
    )
    r = build_plan(catalog, set(), ["X"], "2026-SPRING", 10, 4,
                   equiv_groups=[["P1", "P2"]])
    # 同组课程被多个先修组引用时一次修读即可满足
    assert codes(r) == [["P1"], ["X"]]
    assert r["total_credits"] == 6


def test_prereq_substitute_respects_own_prereqs():
    catalog = cat(
        C("P", 3), C("Q", 3, groups=(("S",),)), C("S", 3),
        C("X", 3, groups=(("P",),)),
    )
    r = build_plan(catalog, set(), ["X"], "2026-SPRING", 10, 4,
                   equiv_groups=[["P", "Q"]])
    # 替代课 Q 自身有先修 S，直接修 P 更早完成
    assert codes(r) == [["P"], ["X"]]


# ---------------------------------------------------------------- 引擎：互认不豁免同修与锁定

def test_coreq_not_exempted_by_equivalence():
    catalog = cat(
        C("X", 3, coreqs=("XL",)), C("XL", 1), C("XB", 1),
    )
    r = build_plan(catalog, {"XB"}, ["X"], "2026-SPRING", 10, 4,
                   equiv_groups=[["XB", "XL"]])
    # 已修 XL 的同组课 XB 不能豁免同修：XL 仍须与 X 同学期修读
    assert codes(r) == [["X", "XL"]]


def test_lock_not_exempted_by_equivalence():
    catalog = cat(C("L", 3), C("LB", 3))
    r = build_plan(catalog, {"LB"}, ["L"], "2026-SPRING", 10, 4,
                   locked=[("L", "2026-FALL")],
                   equiv_groups=[["L", "LB"]])
    # 已修同组课 LB 不能豁免锁定：L 仍须在锁定学期修读
    assert codes(r) == [[], ["L"]]


def test_locked_course_satisfies_target_in_same_group():
    catalog = cat(C("T", 3), C("L", 3))
    r = build_plan(catalog, set(), ["T"], "2026-SPRING", 10, 4,
                   locked=[("L", "2026-SPRING")],
                   equiv_groups=[["L", "T"]])
    # 锁定课 L 实际修读后，同组目标 T 一并满足，无需另修
    assert codes(r) == [["L"]]


# ---------------------------------------------------------------- 引擎：无解与冲突说明

def test_no_feasible_substitute_within_horizon():
    catalog = cat(C("T", 3, seasons=("FALL",)), C("A", 3, seasons=("FALL",)))
    with pytest.raises(PlanError) as e:
        build_plan(catalog, set(), ["T"], "2026-SPRING", 10, 1,
                   equiv_groups=[["A", "T"]])
    # 组内两门课都仅 FALL 开课，期限内唯一的 SPRING 学期排不下 -> 期限内无解
    assert e.value.code == "NO_SOLUTION"


def test_broken_course_substituted_via_equivalence():
    catalog = cat(
        C("X", 2, seasons=("SPRING",), coreqs=("XL",)),
        C("XL", 2, seasons=("FALL",)),
        C("XB", 2),
    )
    # X 自身因同修课季节互斥不可排，但同组替代课 XB 可行 -> 整体有解
    r = build_plan(catalog, set(), ["X"], "2026-SPRING", 10, 4,
                   equiv_groups=[["X", "XB"]])
    assert codes(r) == [["XB"]]


def test_conflict_reported_when_no_substitute_helps():
    catalog = cat(
        C("T", 2, seasons=("SPRING",), coreqs=("TL",)),
        C("TL", 2, seasons=("FALL",)),
        C("A", 2, seasons=("SPRING",), coreqs=("AL",)),
        C("AL", 2, seasons=("FALL",)),
    )
    with pytest.raises(PlanError) as e:
        build_plan(catalog, set(), ["T"], "2026-SPRING", 10, 4,
                   equiv_groups=[["A", "T"]])
    # 目标课及其同组替代课均因同修课季节互斥不可排 -> 说明条件冲突
    assert e.value.code == "REQUIREMENT_CONFLICT"


# ---------------------------------------------------------------- API 集成测试

@pytest.fixture()
def client(tmp_path):
    os.environ["DB_PATH"] = str(tmp_path / "test.db")
    from app.main import app
    with TestClient(app) as c:
        yield c


def _equiv_catalog(**kw):
    payload = {
        "note": "含互认组的目录",
        "courses": [
            {"code": "CS201", "name": "数据结构", "credits": 3,
             "semesters": ["FALL"]},
            {"code": "CS202", "name": "数据结构（双语）", "credits": 3,
             "semesters": ["SPRING", "FALL"]},
            {"code": "CS301", "name": "算法设计与分析", "credits": 3,
             "semesters": ["SPRING", "FALL"], "prereq_groups": [["CS201"]]},
        ],
        "equivalence_groups": [["CS201", "CS202"]],
    }
    payload.update(kw)
    return payload


def _plan_payload(**kw):
    payload = {
        "completed_courses": [],
        "target_courses": ["CS301"],
        "start_semester": "2026-SPRING",
        "max_credits_per_semester": 12,
        "num_semesters": 4,
    }
    payload.update(kw)
    return payload


def test_catalog_with_equivalence_groups_roundtrip(client):
    r = client.post("/catalog/versions", json=_equiv_catalog())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["equivalence_groups"] == [["CS201", "CS202"]]
    vid = body["version_id"]

    cur = client.get("/catalog/current").json()
    assert cur["version_id"] == vid
    assert cur["equivalence_groups"] == [["CS201", "CS202"]]

    hist = client.get(f"/catalog/versions/{vid}").json()
    assert hist["equivalence_groups"] == [["CS201", "CS202"]]
    # 种子版本（v1）未声明互认组：回看保持原语义
    v1 = client.get("/catalog/versions/1").json()
    assert v1["equivalence_groups"] == []


def test_catalog_group_with_unknown_course_rejected(client):
    payload = _equiv_catalog(equivalence_groups=[["CS201", "NOPE999"]])
    r = client.post("/catalog/versions", json=payload)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_INPUT"
    # 不生成新版本：当前版本仍是种子版本 1
    assert client.get("/catalog/current").json()["version_id"] == 1


def test_catalog_group_duplicate_assignment_rejected(client):
    payload = _equiv_catalog(
        equivalence_groups=[["CS201", "CS202"], ["CS202", "CS301"]])
    r = client.post("/catalog/versions", json=payload)
    assert r.status_code == 422
    assert "CS202" in str(r.json()["detail"]["errors"])
    assert client.get("/catalog/current").json()["version_id"] == 1


def test_catalog_group_with_single_course_rejected(client):
    payload = _equiv_catalog(equivalence_groups=[["CS201"]])
    r = client.post("/catalog/versions", json=payload)
    assert r.status_code == 422
    assert client.get("/catalog/current").json()["version_id"] == 1


def test_plan_substitutes_target_via_equivalence(client):
    client.post("/catalog/versions", json=_equiv_catalog())
    r = client.post("/plans", json=_plan_payload())
    assert r.status_code == 201, r.text
    body = r.json()
    # CS201 仅 FALL 开课；同组 CS202 春秋均开课，替代后可提前完成
    sems = [[c["code"] for c in s["courses"]] for s in body["result"]["semesters"]]
    assert sems == [["CS202"], ["CS301"]]
    assert body["result"]["completion_semester"] == "2026-FALL"


def test_plan_completed_equivalent_satisfies_target_prereq(client):
    client.post("/catalog/versions", json=_equiv_catalog())
    r = client.post("/plans", json=_plan_payload(completed_courses=["CS202"]))
    assert r.status_code == 201, r.text
    sems = [[c["code"] for c in s["courses"]] for s in r.json()["result"]["semesters"]]
    # 已修 CS202 满足 CS301 的先修组 [CS201]，直接修 CS301
    assert sems == [["CS301"]]


def test_plan_without_groups_unchanged_on_seed_catalog(client):
    r = client.post("/plans", json={
        "completed_courses": [],
        "target_courses": ["CS401"],
        "start_semester": "2026-SPRING",
        "max_credits_per_semester": 12,
        "num_semesters": 6,
    })
    assert r.status_code == 201
    sems = [[c["code"] for c in s["courses"]] for s in r.json()["result"]["semesters"]]
    assert sems == [["CS101", "CS101L"], ["CS201", "MATH103"], ["CS301"], ["CS401"]]


def test_old_version_plan_review_keeps_original_semantics(client):
    # v1（无互认组）上创建方案
    plan = client.post("/plans", json={
        "completed_courses": ["CS202"],
        "target_courses": ["CS201"],
        "start_semester": "2026-SPRING",
        "max_credits_per_semester": 12,
        "num_semesters": 4,
    }).json()
    assert plan["catalog_version"] == 1
    sems = [[c["code"] for c in s["courses"]] for s in plan["result"]["semesters"]]
    # v1 无互认：已修 CS202 不能替代 CS201，CS201 及其先修仍须实际修读
    assert sems == [["CS101", "CS101L"], ["CS201"]]

    # 修订目录加入互认组后，原方案回看不变
    client.post("/catalog/versions", json=_equiv_catalog())
    again = client.get(f"/plans/{plan['plan_id']}").json()
    assert again["catalog_version"] == 1
    assert again["result"] == plan["result"]


def test_replan_uses_current_catalog_equivalence(client):
    # v1 上创建并确认目标 CS201 的方案
    plan = client.post("/plans", json={
        "completed_courses": [],
        "target_courses": ["CS201"],
        "start_semester": "2026-SPRING",
        "max_credits_per_semester": 12,
        "num_semesters": 4,
    }).json()
    assert client.post(f"/plans/{plan['plan_id']}/confirm").status_code == 200

    # v2：CS201 改为仅 SPRING 开课，并声明 CS201~CS202B 互认（CS202B 春秋均开课）
    current = client.get("/catalog/current").json()
    courses = current["courses"]
    next(c for c in courses if c["code"] == "CS201")["semesters"] = ["SPRING"]
    courses.append({"code": "CS202B", "name": "数据结构（双语）", "credits": 3,
                    "semesters": ["SPRING", "FALL"], "prereq_groups": [], "coreqs": []})
    r = client.post("/catalog/versions", json={
        "note": "v2 互认", "courses": courses,
        "equivalence_groups": [["CS201", "CS202B"]],
    })
    assert r.status_code == 201, r.text

    # 执行 1 学期后重排：按当前目录（v2）判断互认，可用 CS202B 在秋季替代 CS201
    r = client.post(f"/plans/{plan['plan_id']}/replan", json={
        "elapsed_semesters": 1,
        "completed_courses": ["CS101", "CS101L"],
    })
    assert r.status_code == 201, r.text
    draft = r.json()
    assert draft["catalog_version"] == 2
    sems = [[c["code"] for c in s["courses"]] for s in draft["result"]["semesters"]]
    assert sems == [["CS202B"]]
    assert draft["result"]["completion_semester"] == "2026-FALL"
    # 与原方案剩余学期的差异：2026-FALL 的 CS201 换成 CS202B
    diff = draft["replan"]["difference"]
    assert diff["added_courses"] == ["CS202B"]
    assert diff["removed_courses"] == ["CS201"]
    # 原方案保持原版本语义不变
    orig = client.get(f"/plans/{plan['plan_id']}").json()
    assert orig["catalog_version"] == 1
    orig_sems = [[c["code"] for c in s["courses"]] for s in orig["result"]["semesters"]]
    assert orig_sems == [["CS101", "CS101L"], ["CS201"]]
