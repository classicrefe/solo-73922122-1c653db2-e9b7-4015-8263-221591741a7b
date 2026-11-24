"""修复点专项测试：边界旧区免调档、tie-break、建档顺序无关、暴力交叉验证。

暴力校验只在新区集合（<= 10 个）上枚举顶点覆盖子集，绝不逐栋枚举或遍历
查回数；随机相容查回数通过有界组合的增量采样生成。
"""
from __future__ import annotations

import itertools
import random
import time

import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import app
from app.seed import FORCE_COUNTS
from app.services import _load_period, _old_state, min_cost_plan


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


def _setup(client, code, old, new, overlaps):
    payload = {
        "code": code,
        "name": code,
        "old_districts": [
            {"code": o, "name": o, "completed": c} for o, c in old
        ],
        "new_districts": [
            {"code": n, "name": n, "fee": f} for n, f in new
        ],
        "overlaps": [
            {"new_code": n, "old_code": o, "building_total": t}
            for n, o, t in overlaps
        ],
    }
    r = client.post("/periods/", json=payload)
    assert r.status_code == 201, r.text
    return payload


def _load(code):
    conn = connect()
    try:
        return _load_period(conn, code)
    finally:
        conn.close()


def _bruteforce_best(data, registered):
    """暴力枚举候选子集，返回 (费用, 个数, 排序后名单) 的最小者。"""
    state = _old_state(data, registered)
    candidates = sorted(n for n in data.new_codes if n not in registered)

    def is_cover(picked):
        picked_set = set(picked)
        for old in data.old_codes:
            s = state[old]
            edges = s["unretrieved_edges"]
            r, cap = int(s["remaining"]), int(s["free_capacity"])
            if len(edges) < 2 or r <= 0 or r >= cap:
                continue  # 边界/单边旧区不产生冲突
            free = [n for n, _ in edges if n not in registered]
            if sum(1 for n in free if n not in picked_set) >= 2:
                return False
        return True

    best = None
    for k in range(len(candidates) + 1):
        for sub in itertools.combinations(candidates, k):
            if not is_cover(sub):
                continue
            fee = sum(data.new_fee[n] for n in sub)
            key = (fee, len(sub), list(sub))
            if best is None or key < best:
                best = key
    return best


# ---------------------------------------------------------- 边界旧区免调档

def test_force_seed_initial_plan_excludes_boundary_districts(client):
    body = client.get("/periods/DEMO-FORCE/plan").json()
    # 旧实现会把边界旧区 OF1/OF2 上的 A1/A2 也当成冲突，给出 8 元双覆盖
    assert body["status"] == "retrieval_required"
    assert body["plan"] == ["A3"]
    assert body["total_fee"] == 2
    assert "A1" not in body["plan"] and "A2" not in body["plan"]


def test_zero_remainder_old_district_needs_no_retrieval(client):
    # OF: 底账 0，两条未查边 -> 全部压成 0，计划必须为空
    _setup(
        client, "B-ZERO",
        old=[("O1", 0)],
        new=[("X1", 5), ("X2", 7)],
        overlaps=[("X1", "O1", 4), ("X2", "O1", 6)],
    )
    body = client.get("/periods/B-ZERO/plan").json()
    assert body["status"] == "completed"
    assert body["plan"] == [] and body["total_fee"] == 0
    acc = client.get("/periods/B-ZERO/accounting").json()
    got = {x["new_code"]: x["completed"] for x in acc["new_completed"]}
    assert got == {"X1": 0, "X2": 0} and acc["total_completed"] == 0


def test_full_capacity_old_district_needs_no_retrieval(client):
    # 底账等于未查容量 4+6=10 -> 每条边顶到容量，计划必须为空
    _setup(
        client, "B-FULL",
        old=[("O1", 10)],
        new=[("X1", 5), ("X2", 7)],
        overlaps=[("X1", "O1", 4), ("X2", "O1", 6)],
    )
    body = client.get("/periods/B-FULL/plan").json()
    assert body["status"] == "completed"
    assert body["plan"] == [] and body["total_fee"] == 0
    acc = client.get("/periods/B-FULL/accounting").json()
    got = {x["new_code"]: x["completed"] for x in acc["new_completed"]}
    assert got == {"X1": 4, "X2": 6} and acc["total_completed"] == 10


def test_interior_old_district_collapses_to_boundary_after_retrieval(client):
    # O1 底账 10 = 容量 4+6（边界，X1/X2 不冲突、无需调档）；
    # O2 底账 6，容量 3+9（内部，X2-X3 冲突）。调 X2 一份后 O2 只剩一条
    # 未查边，冲突消失，计划由 [X2] 收缩为空名单、零费用。
    _setup(
        client, "B-SHIFT",
        old=[("O1", 10), ("O2", 6)],
        new=[("X1", 5), ("X2", 7), ("X3", 8)],
        overlaps=[
            ("X1", "O1", 4), ("X2", "O1", 6),
            ("X2", "O2", 3), ("X3", "O2", 9),
        ],
    )
    body = client.get("/periods/B-SHIFT/plan").json()
    assert body["plan"] == ["X2"] and body["total_fee"] == 7
    # X2: O1 取 6（顶满边界旧区）、O2 取 1；O2 余 5 全落 X3
    r = client.post(
        "/periods/B-SHIFT/retrievals",
        json={"new_code": "X2", "counts": {"O1": 6, "O2": 1}},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "completed" and body["plan"] == []
    acc = client.get("/periods/B-SHIFT/accounting").json()
    got = {x["new_code"]: x["completed"] for x in acc["new_completed"]}
    # X1-O1 被边界压成 4；X3-O2 = 6-1 = 5
    assert got == {"X1": 4, "X2": 7, "X3": 5}
    assert acc["total_completed"] == 16  # 10+6


def test_force_seed_full_flow_completes_with_one_retrieval(client):
    client.delete("/periods/DEMO-FORCE/retrievals")
    r = client.post(
        "/periods/DEMO-FORCE/retrievals",
        json={"new_code": "A3", "counts": FORCE_COUNTS["A3"]},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "completed" and body["plan"] == [] and body["total_fee"] == 0
    acc = client.get("/periods/DEMO-FORCE/accounting").json()
    got = {x["new_code"]: x["completed"] for x in acc["new_completed"]}
    assert got == {"A1": 10, "A2": 16, "A3": 7, "A4": 5}
    assert acc["total_completed"] == 38  # 0+18+12+8
    client.delete("/periods/DEMO-FORCE/retrievals")


# ---------------------------------------------------------- tie-break

def test_equal_fee_prefers_fewer_files(client):
    # 冲突图：Y1-Y2 与 Y3-Y4 两条独立边，所有新区同价 5。
    # 最小费用 10 有 4 种取法（每条边二选一），档案数都为 2；
    # 再构造一条三选二覆盖场景验证「同价先选档案少」：
    # 旧实现的 DP 只比权重，等权时可能选到更大的覆盖。
    _setup(
        client, "T-SIZE",
        old=[("O1", 1), ("O2", 1)],
        new=[("Y1", 5), ("Y2", 5), ("Y3", 5), ("Y4", 5)],
        overlaps=[
            ("Y1", "O1", 5), ("Y2", "O1", 5),
            ("Y3", "O2", 5), ("Y4", "O2", 5),
        ],
    )
    data = _load("T-SIZE")
    plan, fee = min_cost_plan(data, {})
    assert fee == 10 and len(plan) == 2
    assert plan == ["Y1", "Y3"]  # 字典序最靠前的双档案名单


def test_equal_fee_prefers_fewer_files_explicitly(client):
    # 冲突链 A-B-C：覆盖 {B} 费用 10（1 份档案），覆盖 {A,C} 费用 5+5=10
    # （2 份档案）。同价时必须选档案少的 {B}。
    _setup(
        client, "T-FEW",
        old=[("O1", 1), ("O2", 1)],
        new=[("A", 5), ("B", 10), ("C", 5)],
        overlaps=[
            ("A", "O1", 5), ("B", "O1", 5),
            ("B", "O2", 5), ("C", "O2", 5),
        ],
    )
    data = _load("T-FEW")
    plan, fee = min_cost_plan(data, {})
    assert fee == 10 and plan == ["B"]
    # 把 B 调到 11 后费用主导，改选便宜的双档案 {A,C}
    data.new_fee["B"] = 11
    plan, fee = min_cost_plan(data, {})
    assert fee == 10 and plan == ["A", "C"]


def test_equal_fee_size_then_lexicographic(client):
    # 三角形冲突（同一内部旧区三条边），同价 -> 覆盖固定为 2 个，
    # 取字典序最靠前的两个；改价后覆盖费用相同但大小不同的两种方案。
    _setup(
        client, "T-LEX",
        old=[("O1", 1)],
        new=[("Z1", 4), ("Z2", 4), ("Z3", 4)],
        overlaps=[("Z1", "O1", 5), ("Z2", "O1", 5), ("Z3", "O1", 5)],
    )
    data = _load("T-LEX")
    plan, fee = min_cost_plan(data, {})
    assert (plan, fee) == (["Z1", "Z2"], 8)


def test_cheaper_larger_cover_vs_pricier_smaller_one(client):
    # 三角形：Z1=1, Z2=10, Z3=10。三选二覆盖：
    # {Z2,Z3}=20（省 Z1），{Z1,Z2}=11、{Z1,Z3}=11 -> 费用优先取 11 的双档案，
    # 不会为了档案少（单档案无法覆盖三角形，此处仅验证费用主导）。
    _setup(
        client, "T-FEE",
        old=[("O1", 1)],
        new=[("Z1", 1), ("Z2", 10), ("Z3", 10)],
        overlaps=[("Z1", "O1", 5), ("Z2", "O1", 5), ("Z3", "O1", 5)],
    )
    data = _load("T-FEE")
    plan, fee = min_cost_plan(data, {})
    assert fee == 11 and plan == ["Z1", "Z2"]


def test_plan_independent_of_creation_order(client):
    overlaps = [("R1", "O1", 5), ("R2", "O1", 5), ("R3", "O1", 5)]
    _setup(
        client, "ORDER-A",
        old=[("O1", 1)],
        new=[("R1", 3), ("R2", 3), ("R3", 3)],
        overlaps=overlaps,
    )
    _setup(
        client, "ORDER-B",
        old=[("O1", 1)],
        new=[("R3", 3), ("R1", 3), ("R2", 3)],  # 建档顺序打乱
        overlaps=overlaps,
    )
    pa = client.get("/periods/ORDER-A/plan").json()["plan"]
    pb = client.get("/periods/ORDER-B/plan").json()["plan"]
    assert pa == pb == ["R1", "R2"]


# ---------------------------------------------------------- 暴力交叉验证

def _random_instance(rng, code):
    n_new = rng.randint(2, 9)
    n_old = rng.randint(1, 5)
    news = [f"M{i}" for i in range(n_new)]
    olds = [f"O{j}" for j in range(n_old)]
    # 随机边：保证每个新区、旧区至少出现一次
    edge_set = {(news[rng.randrange(n_new)], olds[rng.randrange(n_old)])
                for _ in range(n_new + n_old)}
    for n in news:
        edge_set.add((n, olds[rng.randrange(n_old)]))
    for o in olds:
        edge_set.add((news[rng.randrange(n_new)], o))
    caps = {}
    for n, o in edge_set:
        caps[(n, o)] = rng.randint(1, 12)
    ledger = {}
    for o in olds:
        cap = sum(t for (n, oo), t in caps.items() if oo == o)
        ledger[o] = rng.randint(0, cap)  # 含 0 与满容量边界
    fees = {n: rng.randint(1, 20) for n in news}
    return {
        "code": code,
        "name": code,
        "old_districts": [
            {"code": o, "name": o, "completed": ledger[o]} for o in olds
        ],
        "new_districts": [
            {"code": n, "name": n, "fee": fees[n]} for n in news
        ],
        "overlaps": [
            {"new_code": n, "old_code": o, "building_total": t}
            for (n, o), t in sorted(caps.items())
        ],
    }


def _bounded_composition(rng, totals, target):
    """在 0 <= x_i <= totals[i] 且和为 target 下均匀-ish 采样一个整数组合。"""
    remaining = target
    out = []
    for i, cap in enumerate(totals):
        later = sum(totals[i + 1:])
        lo = max(0, remaining - later)
        hi = min(cap, remaining)
        v = rng.randint(lo, hi)
        out.append(v)
        remaining -= v
    assert remaining == 0
    return out


def test_randomized_matches_bruteforce_and_guarantees_uniqueness(client):
    rng = random.Random(20261124)
    for it in range(40):
        code = f"RAND-{it}"
        payload = _random_instance(rng, code)
        r = client.post("/periods/", json=payload)
        assert r.status_code == 201, r.text
        data = _load(code)
        plan, fee = min_cost_plan(data, {})
        bf = _bruteforce_best(data, {})
        assert bf is not None
        assert (fee, len(plan), plan) == bf

        # 照单登记一份「随机但相容」的查回结果，验证调完后必然唯一确定
        registered = {}
        for n in plan:
            counts = {}
            for o in data.old_codes:
                if o in data.new_edges[n]:
                    cap = data.new_edges[n][o]
                    used = sum(v.get(o, 0) for v in registered.values())
                    r_o = data.old_completed[o] - used
                    others = [
                        data.new_edges[m][o]
                        for m in data.new_codes
                        if m != n and m not in registered and o in data.new_edges[m]
                    ]
                    lo = max(0, r_o - sum(others))
                    hi = min(cap, r_o)
                    counts[o] = rng.randint(lo, hi)
            registered[n] = counts
            resp = client.post(
                f"/periods/{code}/retrievals",
                json={"new_code": n, "counts": counts},
            )
            assert resp.status_code == 201, (code, n, resp.text)
        final = client.get(f"/periods/{code}/plan").json()
        assert final["status"] == "completed" and final["plan"] == []
        acc = client.get(f"/periods/{code}/accounting").json()
        assert acc["determined"] is True
        # 合计恒等于底账合计
        assert acc["total_completed"] == sum(
            o["completed"] for o in payload["old_districts"]
        )


def test_randomized_partial_registrations_vs_bruteforce(client):
    rng = random.Random(424242)
    for it in range(20):
        code = f"PART-{it}"
        payload = _random_instance(rng, code)
        client.post("/periods/", json=payload)
        data = _load(code)
        # 随机挑一个新区，生成相容查回数登记后重算计划
        target = rng.choice(data.new_codes)
        counts = {}
        feasible = True
        for o, cap in data.new_edges[target].items():
            r_o = data.old_completed[o]
            others = [
                data.new_edges[m][o]
                for m in data.new_codes if m != target and o in data.new_edges[m]
            ]
            v = rng.randint(max(0, r_o - sum(others)), min(cap, r_o))
            counts[o] = v
            if v > r_o:
                feasible = False
        resp = client.post(
            f"/periods/{code}/retrievals",
            json={"new_code": target, "counts": counts},
        )
        if not feasible:
            assert resp.status_code in (409, 422)
            continue
        assert resp.status_code == 201, (code, resp.text)
        data2 = _load(code)
        plan, fee = min_cost_plan(data2, data2.registered)
        bf = _bruteforce_best(data2, data2.registered)
        assert (fee, len(plan), plan) == bf


def test_plan_guarantees_uniqueness_for_every_compatible_outcome(client):
    """照单调档后，枚举所有「与底账相容的查回结果」（实例规模刻意压小，
    只在边值 0..容量 的组合上枚举），验证每种结果下各新区完工数都被唯一
    确定，且核算值恰好等于该结果对应的唯一可行分配。"""
    rng = random.Random(7)
    for it in range(12):
        code = f"GUAR-{it}"
        # 小规模：2~4 个新区、1~2 个旧区，每边容量 1~3
        n_new = rng.randint(2, 4)
        n_old = rng.randint(1, 2)
        news = [f"G{i}" for i in range(n_new)]
        olds = [f"O{j}" for j in range(n_old)]
        edge_set = {(news[rng.randrange(n_new)], olds[rng.randrange(n_old)])
                    for _ in range(n_new + n_old)}
        for n in news:
            edge_set.add((n, olds[rng.randrange(n_old)]))
        for o in olds:
            edge_set.add((news[rng.randrange(n_new)], o))
        caps = {e: rng.randint(1, 3) for e in edge_set}
        ledger = {
            o: rng.randint(0, sum(t for (n, oo), t in caps.items() if oo == o))
            for o in olds
        }
        fees = {n: rng.randint(1, 9) for n in news}
        payload = {
            "code": code,
            "name": code,
            "old_districts": [
                {"code": o, "name": o, "completed": ledger[o]} for o in olds
            ],
            "new_districts": [
                {"code": n, "name": n, "fee": fees[n]} for n in news
            ],
            "overlaps": [
                {"new_code": n, "old_code": o, "building_total": t}
                for (n, o), t in sorted(caps.items())
            ],
        }
        assert client.post("/periods/", json=payload).status_code == 201
        data = _load(code)
        plan, _fee = min_cost_plan(data, {})

        # 逐旧区枚举相容边值分配（笛卡尔积后按底账过滤），再拼成查回结果
        per_old_options = {}
        for o in olds:
            edges_o = [(n, caps[(n, o)]) for n in news if (n, o) in caps]
            opts = []
            for vals in itertools.product(*(range(t + 1) for _, t in edges_o)):
                if sum(vals) == ledger[o]:
                    opts.append(dict(zip((n for n, _ in edges_o), vals)))
            assert opts  # 建档可行性保证
            per_old_options[o] = opts

        keys = list(per_old_options)
        n_outcomes = 1
        for o in keys:
            n_outcomes *= len(per_old_options[o])
        checked = 0
        for combo in itertools.product(*(per_old_options[o] for o in keys)):
            full_values = {n: {} for n in news}
            for o, assignment in zip(keys, combo):
                for n, v in assignment.items():
                    full_values[n][o] = v
            client.delete(f"/periods/{code}/retrievals")
            for n in plan:
                resp = client.post(
                    f"/periods/{code}/retrievals",
                    json={"new_code": n, "counts": full_values[n]},
                )
                assert resp.status_code == 201, (code, n, resp.text)
            body = client.get(f"/periods/{code}/plan").json()
            assert body["status"] == "completed" and body["plan"] == []
            # 唯一相容分配（全边界）时计划本就应为空
            if n_outcomes == 1:
                assert plan == []
            acc = client.get(f"/periods/{code}/accounting").json()
            got = {x["new_code"]: x["completed"] for x in acc["new_completed"]}
            expect = {n: sum(full_values[n].values()) for n in news}
            assert got == expect
            checked += 1
        assert checked == n_outcomes and checked >= 1
        client.delete(f"/periods/{code}/retrievals")


def test_performance_18_districts_complete_graph_under_two_seconds(client):
    payload = {
        "code": "PERF18",
        "name": "perf",
        "old_districts": [{"code": "O1", "name": "o", "completed": 1}],
        "new_districts": [
            {"code": f"N{i:02d}", "name": f"n{i}", "fee": (i % 7) + 1}
            for i in range(18)
        ],
        "overlaps": [
            {"new_code": f"N{i:02d}", "old_code": "O1", "building_total": 10}
            for i in range(18)
        ],
    }
    client.post("/periods/", json=payload)
    start = time.perf_counter()
    body = client.get("/periods/PERF18/plan").json()
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0
    # 完全图内部旧区：留 1 个不查，覆盖 17 个；名单按编码排序
    assert len(body["plan"]) == 17
    data = _load("PERF18")
    bf = _bruteforce_best(data, {})  # 18 个点暴力 2^18 仍可接受，仅此处用
    assert (body["total_fee"], len(body["plan"]), body["plan"]) == bf


def test_solver_does_not_enumerate_buildings_or_retrieval_values(client):
    # 容量放大到千级，求解耗时与小容量一致（DP 只依赖冲突图结构与费用）
    _setup(
        client, "BIGCAP",
        old=[("O1", 5000), ("O2", 3000)],
        new=[("B1", 5), ("B2", 6), ("B3", 7), ("B4", 8)],
        overlaps=[
            ("B1", "O1", 2000), ("B2", "O1", 3000),
            ("B2", "O2", 1500), ("B3", "O2", 1500), ("B4", "O2", 1000),
        ],
    )
    start = time.perf_counter()
    body = client.get("/periods/BIGCAP/plan").json()
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0
    # 链 B1-B2-B3-B4（B3/B4 同挂 O2），最小覆盖取中间便宜节点
    assert body["total_fee"] > 0
    data = _load("BIGCAP")
    bf = _bruteforce_best(data, {})
    assert (body["total_fee"], len(body["plan"]), body["plan"]) == bf
