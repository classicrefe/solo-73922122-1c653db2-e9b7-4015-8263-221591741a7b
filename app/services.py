"""核心业务逻辑。

数学模型
========
对每个期次：
- 旧区 O 底账完工数 c_O；
- 新区 N 与旧区 O 的重叠范围有楼栋总数 t_{N,O}；
- 未知量 x_{N,O}：现行新区 N 在原旧区 O 范围内的完工楼栋数；
  约束 0 <= x_{N,O} <= t_{N,O}，且对每个旧区 Σ_N x_{N,O} = c_O。
- 调一份新区 N 的档案，即得知该新区所有边上的 x_{N,O}，费用 f_N（每期最多 18 个新区）。

唯一性（关键修正）
------------------
设旧区 O 的完工余数 r_O = c_O - 已查总数，未查边集合 E'，未查楼栋总数
T' = Σ_{N∈E'} t_{N,O}。相容前提下 0 <= r_O <= T'：

- r_O = 0：每条未查边都被唯一压成 0；
- r_O = T'：每条未查边都被唯一顶到容量 t_{N,O}；
- |E'| <= 1：0 条边时余数必为 0；1 条边时其值就是 r_O；
- 其余情形（|E'| >= 2 且 0 < r_O < T'，下称「内部旧区」）：每条边至少可取
  两个不同值，存在多组与底账相容的分配，整体无法唯一确定。

调档后某旧区仍唯一确定的充要条件：调档后它不是内部旧区。对内部旧区，调走
任一非空边子集后仍可能留下内部余数（可行余数构成整数区间
[max(0, r-T_调), min(T_未查, r)]，区间内每个整数都可达），故必须把它的未查
新区调到至多一个——这些新区两两构成「冲突」。边界旧区（r=0 或 r=T'）在任何
相容查回结果下仍为边界，不产生冲突。因此待查集合 = 当前内部旧区冲突图的
**最小费用顶点覆盖**（补集为最大权独立集），照单调档后任何相容查回结果都能
唯一确定全部未知量。

tie-break（同费用名单）：先选档案少（顶点覆盖小，即独立集大）的，再选按新区
编码排序后字典序最靠前的名单（即独立集字典序最大）。

可行性
------
登记后对每个旧区独立判断：
    0 <= 余数 r_O = c_O - Σ已查 x <= Σ未查边 t
（再结合各边 0 <= x <= t，整数边容量分配必然可凑足）。

求解只在新区集合（k <= 18）上做 O(2^k) 位掩码 DP，不逐栋枚举，也不遍历任何
可能的查回数。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .db import immediate_transaction
from .schemas import PeriodSetupIn, RetrievalIn


class AppError(Exception):
    """业务错误，由路由层映射为 HTTP 状态码。"""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class Edge:
    new_code: str
    old_code: str
    building_total: int


@dataclass
class PeriodData:
    code: str
    name: str
    old_codes: list[str]
    old_names: dict[str, str]
    old_completed: dict[str, int]
    new_codes: list[str]
    new_names: dict[str, str]
    new_fee: dict[str, int]
    # old_code -> [(new_code, total)]
    old_edges: dict[str, list[tuple[str, int]]]
    # new_code -> {old_code: total}
    new_edges: dict[str, dict[str, int]]
    # new_code -> {old_code: registered completed count}
    registered: dict[str, dict[str, int]]


def _load_period(conn: sqlite3.Connection, period_code: str) -> PeriodData:
    prow = conn.execute(
        "SELECT id, code, name FROM period WHERE code = ?", (period_code,)
    ).fetchone()
    if prow is None:
        raise AppError(404, f"期次不存在: {period_code}")
    pid = prow["id"]

    old_rows = conn.execute(
        "SELECT code, name, completed FROM old_district WHERE period_id = ? ORDER BY id",
        (pid,),
    ).fetchall()
    new_rows = conn.execute(
        "SELECT code, name, fee FROM new_district WHERE period_id = ? ORDER BY id",
        (pid,),
    ).fetchall()
    overlap_rows = conn.execute(
        """
        SELECT n.code AS new_code, o.code AS old_code, x.building_total
        FROM overlap x
        JOIN new_district n ON n.id = x.new_district_id
        JOIN old_district o ON o.id = x.old_district_id
        WHERE x.period_id = ?
        ORDER BY x.id
        """,
        (pid,),
    ).fetchall()
    retrieval_rows = conn.execute(
        """
        SELECT n.code AS new_code, o.code AS old_code, r.completed_count
        FROM retrieval r
        JOIN new_district n ON n.id = r.new_district_id
        JOIN old_district o ON o.id = r.old_district_id
        WHERE r.period_id = ?
        ORDER BY r.id
        """,
        (pid,),
    ).fetchall()

    old_codes = [r["code"] for r in old_rows]
    new_codes = [r["code"] for r in new_rows]
    old_edges: dict[str, list[tuple[str, int]]] = {c: [] for c in old_codes}
    new_edges: dict[str, dict[str, int]] = {c: {} for c in new_codes}
    for r in overlap_rows:
        old_edges[r["old_code"]].append((r["new_code"], r["building_total"]))
        new_edges[r["new_code"]][r["old_code"]] = r["building_total"]

    registered: dict[str, dict[str, int]] = {}
    for r in retrieval_rows:
        registered.setdefault(r["new_code"], {})[r["old_code"]] = r["completed_count"]

    return PeriodData(
        code=prow["code"],
        name=prow["name"],
        old_codes=old_codes,
        old_names={r["code"]: r["name"] for r in old_rows},
        old_completed={r["code"]: r["completed"] for r in old_rows},
        new_codes=new_codes,
        new_names={r["code"]: r["name"] for r in new_rows},
        new_fee={r["code"]: r["fee"] for r in new_rows},
        old_edges=old_edges,
        new_edges=new_edges,
        registered=registered,
    )


# ---------------------------------------------------------------- 期次建档

def create_period(conn: sqlite3.Connection, payload: PeriodSetupIn) -> None:
    old_codes = [o.code for o in payload.old_districts]
    new_codes = [n.code for n in payload.new_districts]
    if len(set(old_codes)) != len(old_codes):
        raise AppError(422, "旧区编码存在重复")
    if len(set(new_codes)) != len(new_codes):
        raise AppError(422, "新区编码存在重复")
    if len(new_codes) > 18:
        raise AppError(422, "每期最多 18 个新区")

    old_set, new_set = set(old_codes), set(new_codes)
    seen_edges: set[tuple[str, str]] = set()
    for ov in payload.overlaps:
        if ov.old_code not in old_set:
            raise AppError(422, f"重叠范围引用了不存在的旧区: {ov.old_code}")
        if ov.new_code not in new_set:
            raise AppError(422, f"重叠范围引用了不存在的新区: {ov.new_code}")
        key = (ov.new_code, ov.old_code)
        if key in seen_edges:
            raise AppError(422, f"重叠范围重复: {ov.new_code}/{ov.old_code}")
        seen_edges.add(key)

    # 每个旧区、新区都必须至少参与一个重叠范围，否则底账/档案无意义
    covered_old = {ov.old_code for ov in payload.overlaps}
    covered_new = {ov.new_code for ov in payload.overlaps}
    missing_old = old_set - covered_old
    missing_new = new_set - covered_new
    if missing_old:
        raise AppError(422, f"旧区缺少重叠范围: {sorted(missing_old)}")
    if missing_new:
        raise AppError(422, f"新区缺少重叠范围: {sorted(missing_new)}")

    # 底账本身必须可由全部楼栋凑足：0 <= c_O <= Σ t_{N,O}
    capacity: dict[str, int] = {c: 0 for c in old_codes}
    for ov in payload.overlaps:
        capacity[ov.old_code] += ov.building_total
    for o in payload.old_districts:
        if o.completed > capacity[o.code]:
            raise AppError(
                422,
                f"旧区 {o.code} 底账完工数 {o.completed} 超过重叠楼栋总数 "
                f"{capacity[o.code]}，底账不可行",
            )

    with immediate_transaction(conn):
        cur = conn.execute(
            "INSERT INTO period(code, name) VALUES (?, ?)",
            (payload.code, payload.name),
        )
        pid = cur.lastrowid
        old_id: dict[str, int] = {}
        for o in payload.old_districts:
            cur = conn.execute(
                "INSERT INTO old_district(period_id, code, name, completed) "
                "VALUES (?, ?, ?, ?)",
                (pid, o.code, o.name, o.completed),
            )
            old_id[o.code] = cur.lastrowid
        new_id: dict[str, int] = {}
        for n in payload.new_districts:
            cur = conn.execute(
                "INSERT INTO new_district(period_id, code, name, fee) "
                "VALUES (?, ?, ?, ?)",
                (pid, n.code, n.name, n.fee),
            )
            new_id[n.code] = cur.lastrowid
        for ov in payload.overlaps:
            conn.execute(
                "INSERT INTO overlap(period_id, new_district_id, old_district_id, "
                "building_total) VALUES (?, ?, ?, ?)",
                (pid, new_id[ov.new_code], old_id[ov.old_code], ov.building_total),
            )


# ---------------------------------------------------------------- 登记调档

def register_retrieval(
    conn: sqlite3.Connection, period_code: str, payload: RetrievalIn
) -> dict[str, dict[str, int]]:
    """登记一份新区档案。

    校验（全部在同一个 BEGIN IMMEDIATE 事务内）：
    1. 新区存在，且 keys 恰好等于该新区涉及的全部旧区；
    2. 各查回数 0 <= x <= 重叠楼栋总数；
    3. 合入后各旧区：已查总数 <= 底账，且完工余数能由未查楼栋凑足；
    4. 同新区同值重送幂等成功，异值退回（409，原记录保留）。
    """
    with immediate_transaction(conn):
        data = _load_period(conn, period_code)
        if payload.new_code not in data.new_edges:
            raise AppError(404, f"新区不存在: {payload.new_code}")

        expected_olds = set(data.new_edges[payload.new_code])
        got_olds = set(payload.counts)
        if got_olds != expected_olds:
            missing = sorted(expected_olds - got_olds)
            extra = sorted(got_olds - expected_olds)
            raise AppError(
                422,
                f"查回旧区集合必须恰好为 {sorted(expected_olds)}；"
                f"缺少 {missing}，多余 {extra}",
            )

        # 规则 2：单条范围上下界
        for old_code, value in payload.counts.items():
            if value < 0:
                raise AppError(422, f"查回数不能为负: {old_code}={value}")
            total = data.new_edges[payload.new_code][old_code]
            if value > total:
                raise AppError(
                    422,
                    f"查回数 {value} 超过范围 {payload.new_code}/{old_code} "
                    f"楼栋总数 {total}",
                )

        # 规则 4：同新区重送，相同结果幂等，异值冲突
        previous = data.registered.get(payload.new_code)
        if previous is not None and previous != dict(payload.counts):
            raise AppError(
                409,
                f"新区 {payload.new_code} 已登记不同结果 {previous}，"
                f"异值退回，原记录保留",
            )

        # 模拟合入后的已查总数，逐旧区做底账相容性判断（规则 3）
        merged: dict[str, dict[str, int]] = {
            n: dict(v) for n, v in data.registered.items()
        }
        merged[payload.new_code] = dict(payload.counts)

        _check_feasible(data, merged)

        if previous is None:
            pid = conn.execute(
                "SELECT id FROM period WHERE code = ?", (period_code,)
            ).fetchone()["id"]
            nid = conn.execute(
                "SELECT id FROM new_district WHERE period_id = ? AND code = ?",
                (pid, payload.new_code),
            ).fetchone()["id"]
            for old_code, value in payload.counts.items():
                oid = conn.execute(
                    "SELECT id FROM old_district WHERE period_id = ? AND code = ?",
                    (pid, old_code),
                ).fetchone()["id"]
                conn.execute(
                    "INSERT INTO retrieval(period_id, new_district_id, old_district_id, "
                    "completed_count) VALUES (?, ?, ?, ?)",
                    (pid, nid, oid, value),
                )
        # previous 相同：幂等重送，不重复写入
        return merged


def _old_state(
    data: PeriodData, registered: dict[str, dict[str, int]]
) -> dict[str, dict[str, object]]:
    """计算每个旧区的已查总数、完工余数与未查边容量。"""
    state: dict[str, dict[str, object]] = {}
    for old in data.old_codes:
        used = sum(reg.get(old, 0) for reg in registered.values())
        remaining = data.old_completed[old] - used
        unretrieved_edges = [
            (n, t) for (n, t) in data.old_edges[old] if n not in registered
        ]
        free_capacity = sum(t for _, t in unretrieved_edges)
        state[old] = {
            "used": used,
            "remaining": remaining,
            "free_capacity": free_capacity,
            "unretrieved_edges": unretrieved_edges,
        }
    return state


def _check_feasible(
    data: PeriodData, registered: dict[str, dict[str, int]]
) -> dict[str, dict[str, object]]:
    """底账相容性：各旧区余数介于 0 与未查楼栋总数之间。"""
    state = _old_state(data, registered)
    for old, s in state.items():
        remaining = s["remaining"]
        if remaining < 0:
            raise AppError(
                409,
                f"旧区 {old} 已查完工数 {s['used']} 超过底账 {data.old_completed[old]}，"
                "与底账不相容，登记退回",
            )
        if remaining > s["free_capacity"]:
            raise AppError(
                409,
                f"旧区 {old} 完工余数 {remaining} 无法由未查楼栋 "
                f"（总容量 {s['free_capacity']}）凑足，与底账不相容，登记退回",
            )
    return state


# ---------------------------------------------------------------- 计划与核算

def _interior_conflict_adj(
    data: PeriodData,
    state: dict[str, dict[str, object]],
    registered: dict[str, dict[str, int]],
) -> list[int]:
    """冲突图（位掩码邻接表，位序与 data.new_codes 一致）。

    只有「内部旧区」（未查边 >= 2 且 0 < 完工余数 < 未查楼栋总数）下的未查
    新区两两冲突；边界旧区（余数为 0 或等于未查容量）的边已被唯一压定，不
    产生冲突，调它们只会白花档案费。
    """
    n = len(data.new_codes)
    index = {code: i for i, code in enumerate(data.new_codes)}
    adj = [0] * n
    for old in data.old_codes:
        s = state[old]
        free_edges: list[tuple[str, int]] = s["unretrieved_edges"]  # type: ignore[assignment]
        remaining = int(s["remaining"])
        free_capacity = int(s["free_capacity"])
        if len(free_edges) < 2:
            continue
        if remaining <= 0 or remaining >= free_capacity:
            # 边界旧区：每条未查边分别被压成 0 或顶到容量，无需调档
            continue
        members = [index[ncode] for ncode, _ in free_edges if ncode not in registered]
        for a in members:
            for b in members:
                if a != b:
                    adj[a] |= 1 << b
    return adj


def min_cost_plan(
    data: PeriodData,
    registered: dict[str, dict[str, int]],
    state: dict[str, dict[str, object]] | None = None,
) -> tuple[list[str], int]:
    """求费用最低的待调名单。

    在内部旧区冲突图上求最小权顶点覆盖（补集为最大权独立集）。新区数
    k <= 18，位掩码 DP 求最大权独立集，复杂度 O(2^k) 整数运算，不逐栋枚举、
    不遍历查回数。

    tie-break（对顶点覆盖，即待调名单）：
      1. 总费用低；
      2. 待调个数少（等价于独立集个数多）；
      3. 按编码排序后名单字典序靠前（等价于独立集字典序靠后）。
    候选新区统一按编码排序后再编号，使结果与新区建档顺序无关。
    """
    if state is None:
        state = _old_state(data, registered)
    raw_adj = _interior_conflict_adj(data, state, registered)
    code_index = {code: i for i, code in enumerate(data.new_codes)}

    # 按编码排序确定规范位序，消除建档顺序对 tie-break 的影响
    candidates = sorted(n for n in data.new_codes if n not in registered)
    k = len(candidates)
    if k == 0:
        return [], 0

    adj = [0] * k
    for j, code in enumerate(candidates):
        m = raw_adj[code_index[code]]
        for h, other in enumerate(candidates):
            if m & (1 << code_index[other]):
                adj[j] |= 1 << h
    weights = [data.new_fee[code] for code in candidates]

    size = 1 << k
    # dp_weight[mask]：诱导子图 mask 上最优独立集的权重和
    # dp_size[mask]：同权重下独立集最大个数
    # dp_set[mask]：同权重/同个数下字典序最大的独立集（位掩码）
    dp_weight = [0] * size
    dp_size = [0] * size
    dp_set = [0] * size
    for mask in range(1, size):
        bit = mask & -mask
        j = bit.bit_length() - 1
        rest = mask ^ bit
        # 不取 j
        w0, s0, set0 = dp_weight[rest], dp_size[rest], dp_set[rest]
        # 取 j：连同 j 的邻居一并移出
        sub = mask & ~(bit | adj[j])
        w1 = weights[j] + dp_weight[sub]
        s1 = dp_size[sub] + 1
        set1 = dp_set[sub] | bit
        if w1 > w0 or (w1 == w0 and (s1 > s0 or (s1 == s0 and set1 > set0))):
            dp_weight[mask], dp_size[mask], dp_set[mask] = w1, s1, set1
        else:
            dp_weight[mask], dp_size[mask], dp_set[mask] = w0, s0, set0

    independent = dp_set[size - 1]
    cover_mask = (size - 1) ^ independent
    plan = [candidates[j] for j in range(k) if cover_mask & (1 << j)]
    total_fee = sum(weights) - dp_weight[size - 1]
    return plan, total_fee


def evaluate(
    data: PeriodData,
) -> tuple[str, dict[str, dict[str, object]], dict[str, dict[str, int]], list[str], int]:
    """在当前已提交登记状态上评估：可行性、待调计划、是否可唯一核算。"""
    # 先算各旧区状态，再在其上做相容性判断；不可行时状态仍可供排障展示
    state = _old_state(data, data.registered)
    _check_feasible(data, data.registered)

    # 唯一确定：不存在内部旧区（未查边 >= 2 且 0 < 余数 < 未查容量）
    determined = all(
        len(s["unretrieved_edges"]) <= 1
        or int(s["remaining"]) <= 0
        or int(s["remaining"]) >= int(s["free_capacity"])
        for s in state.values()
    )
    if determined:
        # 各新区完工数已能唯一确定：空名单、零费用，不再让年报人员花钱调档
        return "completed", state, dict(data.registered), [], 0

    plan, total_fee = min_cost_plan(data, data.registered, state)
    return "retrieval_required", state, dict(data.registered), plan, total_fee


def solve_completed(
    data: PeriodData, state: dict[str, dict[str, object]]
) -> list[tuple[str, str, int]]:
    """唯一核算各新区完工数。

    已查边取查回数；未查边按旧区状态唯一压定：
    - 余数为 0：所有未查边取 0；
    - 余数等于未查容量：每条未查边取其楼栋总数（全部完工）；
    - 仅一条未查边：取旧区完工余数。
    """
    values: dict[tuple[str, str], int] = {}
    for ncode, reg in data.registered.items():
        for old_code, v in reg.items():
            values[(ncode, old_code)] = v
    for old, s in state.items():
        edges: list[tuple[str, int]] = s["unretrieved_edges"]  # type: ignore[assignment]
        remaining = int(s["remaining"])
        if len(edges) == 1:
            values[(edges[0][0], old)] = remaining
        elif remaining == 0:
            for ncode, _t in edges:
                values[(ncode, old)] = 0
        elif remaining == int(s["free_capacity"]):
            for ncode, t in edges:
                values[(ncode, old)] = t
        # 可行性 + completed 状态保证不会出现其他情形
    result = []
    for ncode in data.new_codes:
        total = sum(v for (n, _), v in values.items() if n == ncode)
        result.append((ncode, data.new_names[ncode], total))
    return result


def reset_retrievals(conn: sqlite3.Connection, period_code: str) -> None:
    with immediate_transaction(conn):
        prow = conn.execute(
            "SELECT id FROM period WHERE code = ?", (period_code,)
        ).fetchone()
        if prow is None:
            raise AppError(404, f"期次不存在: {period_code}")
        conn.execute("DELETE FROM retrieval WHERE period_id = ?", (prow["id"],))
