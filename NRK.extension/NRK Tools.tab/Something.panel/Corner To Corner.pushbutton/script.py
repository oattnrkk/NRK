#! python2
# -*- coding: utf-8 -*-
"""Connect Gaps
เลือก Detail Line เส้นตรงอย่างน้อย 2 เส้นก่อนกดปุ่มนี้ (หรือไม่เลือกไว้ก่อนก็ได้
โปรแกรมจะให้คลิกเลือกบนหน้าจอแทน) จากนั้นจะหาคู่ปลายเส้นที่มี gap อยู่ในระยะที่
กำหนด แล้วลากเส้นทแยงเชื่อมให้อัตโนมัติทุกมุมในทีเดียว

Engine: IronPython 2.7 (`#! python2`) — ใช้ตัวนี้แทน CPython3 เพราะ CPython3
engine ในเครื่องนี้ยังตั้งค่าไม่สมบูรณ์ (Active CPython Engine Version: 0)
"""
__title__ = "Connect\nGaps"
__author__ = "Oat"
__doc__ = "เชื่อม gap ระหว่างปลาย Detail Line ที่อยู่ใกล้กันด้วยเส้นทแยงอัตโนมัติ"

from Autodesk.Revit.DB import CurveElement, CurveElementType, FilteredElementCollector, Line
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from pyrevit import revit, forms, script

doc = revit.doc
uidoc = revit.uidoc
logger = script.get_logger()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# ไม่มีการจำกัดระยะ gap สูงสุด — เพราะ user เป็นคนเลือกเส้นที่ต้องการเชื่อมเอง
# อยู่แล้ว จึงถือว่าทุกช่องว่างระหว่างปลายเส้นที่เลือกมาคือ gap ที่ต้องการต่อ
# ทั้งหมด ไม่ว่าจะห่างแค่ไหน
COINCIDENT_TOL_FT = 1e-6  # ปลายเส้นที่ใกล้กันกว่านี้ถือว่าชนกันอยู่แล้ว ไม่ต้องเชื่อม


class DetailLineSelectionFilter(ISelectionFilter):
    """จำกัดให้คลิกเลือกได้เฉพาะ Detail Line ที่เป็นเส้นตรง"""

    def AllowElement(self, elem):
        if isinstance(elem, CurveElement) and \
                elem.CurveElementType == CurveElementType.DetailCurve:
            return isinstance(elem.GeometryCurve, Line)
        return False

    def AllowReference(self, ref, point):
        return True


def get_selected_detail_lines():
    """ใช้ selection ที่มีอยู่แล้วถ้ามี Detail Line อยู่ในนั้น
    ไม่งั้นให้ผู้ใช้คลิกเลือกบนหน้าจอ"""
    selection = uidoc.Selection
    selected_ids = selection.GetElementIds()
    lines = []

    for eid in selected_ids:
        el = doc.GetElement(eid)
        if isinstance(el, CurveElement) and \
                el.CurveElementType == CurveElementType.DetailCurve and \
                isinstance(el.GeometryCurve, Line):
            lines.append(el)

    if not lines:
        try:
            refs = selection.PickObjects(
                ObjectType.Element,
                DetailLineSelectionFilter(),
                "Select straight Detail Lines to connect gaps between"
            )
        except Exception:
            return []
        for r in refs:
            lines.append(doc.GetElement(r.ElementId))

    return lines


def get_endpoints(curve_elem):
    curve = curve_elem.GeometryCurve
    return [curve.GetEndPoint(0), curve.GetEndPoint(1)]


DUPLICATE_TOL_FT = 0.01  # ~3mm — ปลายเส้นในระยะนี้ถือว่าเป็นเส้นเชื่อมเดิมที่มีอยู่แล้ว


def get_existing_view_lines(view):
    """เก็บ Detail Line เส้นตรงทั้งหมดที่มีอยู่แล้วในวิวนี้ (รวมเส้นทแยงที่เคย
    สร้างไว้จากการรันครั้งก่อน) ไว้เช็คกันสร้างซ้ำ"""
    existing = []
    collector = FilteredElementCollector(doc, view.Id).OfClass(CurveElement)
    for el in collector:
        if el.CurveElementType != CurveElementType.DetailCurve:
            continue
        curve = el.GeometryCurve
        if isinstance(curve, Line):
            existing.append((curve.GetEndPoint(0), curve.GetEndPoint(1)))
    return existing


def connection_already_exists(existing_lines, pt_a, pt_b, tol=DUPLICATE_TOL_FT):
    """เช็คว่ามีเส้นที่ปลายตรงกับ (pt_a, pt_b) นี้อยู่แล้วหรือยัง (ไม่สนทิศทาง)"""
    for end0, end1 in existing_lines:
        same_order = end0.DistanceTo(pt_a) < tol and end1.DistanceTo(pt_b) < tol
        reversed_order = end0.DistanceTo(pt_b) < tol and end1.DistanceTo(pt_a) < tol
        if same_order or reversed_order:
            return True
    return False


def find_gap_pairs(lines):
    """คืนค่า (pairs, stranded_count)

    pairs = list ของ (pointA, pointB) หนึ่งคู่ต่อหนึ่ง gap ที่จะเชื่อม
    stranded_count = จำนวนปลายเส้นที่มี gap อยู่ในระยะที่กำหนดจริง แต่จับคู่
    ไม่ได้ เพราะฝั่งตรงข้ามถูก "แย่ง" ไปจับคู่กับปลายเส้นอื่นก่อน (เช่น 3 เส้น
    มาบรรจบใกล้กันที่มุมเดียว จะมีได้แค่ 1 คู่ อีกเส้นจะไม่มีคู่ให้เชื่อม)

    อัลกอริทึม 2 รอบ เพื่อลดปัญหาปลายเส้นถูกแย่งคู่แบบ greedy ล้วนๆ:
    1) จับคู่แบบ Mutual Nearest Neighbor ก่อน — จับเฉพาะคู่ที่ต่างฝ่าย
       ต่างมองกันเป็นตัวที่ใกล้ที่สุด (แม่นกว่า, ไม่ค่อยแย่งกันผิดคู่)
    2) ที่เหลือค่อยจับคู่แบบ greedy ตามระยะใกล้สุด (พฤติกรรมแบบเดิม) เพื่อ
       เก็บตกให้ได้มากที่สุดเท่าที่จะทำได้
    """
    endpoints = []
    for line_elem in lines:
        for idx, pt in enumerate(get_endpoints(line_elem)):
            endpoints.append((line_elem.Id, idx, pt))

    n = len(endpoints)

    # หา "เพื่อนบ้าน" ของแต่ละปลายเส้น (ทุกจุดที่ไม่ได้ชนกันอยู่แล้ว) เรียงใกล้ไปไกล
    neighbor_lists = [[] for _ in range(n)]
    for i in range(n):
        id_a, idx_a, pt_a = endpoints[i]
        for j in range(n):
            if i == j:
                continue
            id_b, idx_b, pt_b = endpoints[j]
            if id_b == id_a:
                continue  # ข้ามปลายเส้นของเส้นเดียวกัน
            dist = pt_a.DistanceTo(pt_b)
            if dist > COINCIDENT_TOL_FT:
                neighbor_lists[i].append((dist, j))
    for lst in neighbor_lists:
        lst.sort(key=lambda x: x[0])

    used = set()
    pairs = []

    def nearest_unused(i):
        for dist, j in neighbor_lists[i]:
            if j not in used:
                return j
        return None

    # รอบ 1: Mutual Nearest Neighbor (ทำวนซ้ำ เพราะพอจับคู่ไปแล้วปลายเส้น
    # ที่เหลือจะมี "ตัวใกล้สุด" ใหม่เกิดขึ้นเรื่อยๆ)
    changed = True
    while changed:
        changed = False
        for i in range(n):
            if i in used:
                continue
            j = nearest_unused(i)
            if j is None:
                continue
            k = nearest_unused(j)
            if k == i:
                pairs.append((endpoints[i][2], endpoints[j][2]))
                used.add(i)
                used.add(j)
                changed = True

    # รอบ 2: greedy ตามระยะใกล้สุด เก็บตกที่เหลือ (พฤติกรรมแบบเดิม)
    remaining_candidates = []
    for i in range(n):
        if i in used:
            continue
        for dist, j in neighbor_lists[i]:
            if j <= i or j in used:
                continue
            remaining_candidates.append((dist, i, j))
    remaining_candidates.sort(key=lambda c: c[0])
    for dist, i, j in remaining_candidates:
        if i in used or j in used:
            continue
        pairs.append((endpoints[i][2], endpoints[j][2]))
        used.add(i)
        used.add(j)

    # นับปลายเส้นที่ "มี gap ในระยะที่กำหนดจริง" แต่สุดท้ายจับคู่ไม่ได้เลย
    stranded = 0
    for i in range(n):
        if i not in used and neighbor_lists[i]:
            stranded += 1

    return pairs, stranded


def main():
    lines = get_selected_detail_lines()
    if not lines:
        forms.alert(
            "ไม่พบ Detail Line ที่เลือกไว้",
            title="Connect Gaps",
            exitscript=True,
        )

    active_view = doc.ActiveView
    pairs, stranded = find_gap_pairs(lines)

    if not pairs:
        forms.alert(
            "ไม่พบ gap ที่ต้องเชื่อมระหว่างเส้นที่เลือก",
            title="Connect Gaps",
            exitscript=True,
        )

    existing_lines = get_existing_view_lines(active_view)

    created = 0
    skipped_duplicate = 0
    with revit.Transaction("Connect Detail Line Gaps"):
        for pt_a, pt_b in pairs:
            if connection_already_exists(existing_lines, pt_a, pt_b):
                skipped_duplicate += 1
                continue
            try:
                new_line = Line.CreateBound(pt_a, pt_b)
                doc.Create.NewDetailCurve(active_view, new_line)
                existing_lines.append((pt_a, pt_b))  # กันไม่ให้คู่ซ้ำในรอบเดียวกันสร้างซ้ำอีก
                created += 1
            except Exception as ex:
                logger.debug("Skipped pair {} -> {}: {}".format(pt_a, pt_b, ex))

    message = "เชื่อม gap สำเร็จ {} จุด\nข้ามเพราะมีเส้นเชื่อมอยู่แล้ว {} จุด".format(
        created, skipped_duplicate
    )
    if stranded:
        message += (
            "\nมี {} จุดที่เจอ gap ในระยะที่กำหนด แต่จับคู่ไม่ได้ "
            "(น่าจะมี 3 เส้นขึ้นไปมาบรรจบใกล้กันที่มุมเดียว) "
            "กรุณาตรวจสอบและต่อเองด้วยมือ".format(stranded)
        )
    forms.alert(message, title="Connect Gaps")


if __name__ == "__main__":
    main()
