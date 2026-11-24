"""演示期次种子数据（仅当库中没有任何期次时写入）。

期次 DEMO-2023（内部旧区驱动的常规调档）
-----------------------------------------
旧区完工底账：O1=30, O2=24, O3=18, O4=10
新区调档费用：N1=12, N2=7, N3=3, N4=10
重叠范围（楼栋总数）：
    N1-O1=20, N1-O2=10
    N2-O1=15, N2-O3=12
    N3-O2=16, N3-O3=8
    N4-O3=10, N4-O4=10

冲突图（共享「内部旧区」的新区相连）：
    N1-N2 (O1), N1-N3 (O2), N2-N3 (O2/O3), N2-N4 (O3), N3-N4 (O3)
最大权独立集 {N1, N4}（费用 22 可省），故最小费用待调名单 = {N2, N3}，金额 10。

期次 DEMO-FORCE（边界旧区：登记后无需再调档）
----------------------------------------------
旧区完工底账：OF1=0（余数为零，未查边全部压成 0）、
             OF2=18（等于未查容量 10+8，未查边全部顶到容量）、
             OF3=12、OF4=8（均为内部旧区）。
新区费用：A1=9, A2=9, A3=2, A4=6。
重叠范围：
    A1-OF1=6, A2-OF1=6              -> OF1 边界，A1/A2 不冲突、无需调档
    A1-OF2=10, A2-OF2=8             -> OF2 边界，A1/A2 不冲突、无需调档
    A2-OF3=12, A3-OF3=12            -> OF3 内部，A2-A3 冲突
    A3-OF4=8, A4-OF4=8              -> OF4 内部，A3-A4 冲突
内部冲突图为链 A2-A3-A4，最小费用顶点覆盖 = {A3}（费用 2）；A1 只挂在边界
旧区上，永不进名单。只调 A3 一份档案后，OF3/OF4 各剩一条未查边，任何相容
查回结果都能唯一确定全部新区完工数，计划随即变为空名单、零费用。
旧实现把 OF1/OF2 也当成冲突（链 A1-A2-A3-A4），会多调一份 A2 或 A1 的档案。
"""
from __future__ import annotations

import sqlite3

from .schemas import (
    NewDistrictIn,
    OldDistrictIn,
    OverlapIn,
    PeriodSetupIn,
)
from .services import create_period

DEMO = PeriodSetupIn(
    code="DEMO-2023",
    name="2023 年度改造补算演示期",
    old_districts=[
        OldDistrictIn(code="O1", name="旧一区", completed=30),
        OldDistrictIn(code="O2", name="旧二区", completed=24),
        OldDistrictIn(code="O3", name="旧三区", completed=18),
        OldDistrictIn(code="O4", name="旧四区", completed=10),
    ],
    new_districts=[
        NewDistrictIn(code="N1", name="新一街道", fee=12),
        NewDistrictIn(code="N2", name="新二街道", fee=7),
        NewDistrictIn(code="N3", name="新三街道", fee=3),
        NewDistrictIn(code="N4", name="新四街道", fee=10),
    ],
    overlaps=[
        OverlapIn(new_code="N1", old_code="O1", building_total=20),
        OverlapIn(new_code="N1", old_code="O2", building_total=10),
        OverlapIn(new_code="N2", old_code="O1", building_total=15),
        OverlapIn(new_code="N2", old_code="O3", building_total=12),
        OverlapIn(new_code="N3", old_code="O2", building_total=16),
        OverlapIn(new_code="N3", old_code="O3", building_total=8),
        OverlapIn(new_code="N4", old_code="O3", building_total=10),
        OverlapIn(new_code="N4", old_code="O4", building_total=10),
    ],
)

DEMO_FORCE = PeriodSetupIn(
    code="DEMO-FORCE",
    name="边界旧区免调档演示期",
    old_districts=[
        OldDistrictIn(code="OF1", name="边界旧区一（底账为零）", completed=0),
        OldDistrictIn(code="OF2", name="边界旧区二（底账等于容量）", completed=18),
        OldDistrictIn(code="OF3", name="内部旧区三", completed=12),
        OldDistrictIn(code="OF4", name="内部旧区四", completed=8),
    ],
    new_districts=[
        NewDistrictIn(code="A1", name="新区甲", fee=9),
        NewDistrictIn(code="A2", name="新区乙", fee=9),
        NewDistrictIn(code="A3", name="新区丙", fee=2),
        NewDistrictIn(code="A4", name="新区丁", fee=6),
    ],
    overlaps=[
        OverlapIn(new_code="A1", old_code="OF1", building_total=6),
        OverlapIn(new_code="A2", old_code="OF1", building_total=6),
        OverlapIn(new_code="A1", old_code="OF2", building_total=10),
        OverlapIn(new_code="A2", old_code="OF2", building_total=8),
        OverlapIn(new_code="A2", old_code="OF3", building_total=12),
        OverlapIn(new_code="A3", old_code="OF3", building_total=12),
        OverlapIn(new_code="A3", old_code="OF4", building_total=8),
        OverlapIn(new_code="A4", old_code="OF4", building_total=8),
    ],
)

# 与 DEMO-FORCE 底账相容的一份 A3 查回数。登记后：
# OF3 余 8 全落在唯一未查边 A2 上（A2-OF3=8）；
# OF4 余 5 全落在唯一未查边 A4 上（A4-OF4=5）；
# OF1/OF2 为边界旧区，A1/A2 的边被压成 0 或顶到容量——全部新区完工数唯一确定。
FORCE_COUNTS = {
    "A3": {"OF3": 4, "OF4": 3},
}


def seed_demo(conn: sqlite3.Connection) -> None:
    existing = {
        r["code"] for r in conn.execute("SELECT code FROM period").fetchall()
    }
    for payload in (DEMO, DEMO_FORCE):
        if payload.code in existing:
            continue
        try:
            create_period(conn, payload)
        except sqlite3.IntegrityError:
            # 并发启动下另一进程已建档，忽略
            pass


# 一组与底账相容的查回数，供 /docs 试用与测试参考：
# N2: O1=10, O3=6 ；N3: O2=14, O3=2
# 登记后余数：O1=20（唯一未查边 N1）、O2=10（唯一未查边 N1）、
#            O3=10（唯一未查边 N4）、O4=10（唯一未查边 N4），全部唯一确定。
COMPATIBLE_COUNTS = {
    "N2": {"O1": 10, "O3": 6},
    "N3": {"O2": 14, "O3": 2},
}
