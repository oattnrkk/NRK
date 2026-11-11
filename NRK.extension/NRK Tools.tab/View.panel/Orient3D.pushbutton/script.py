# -*- coding: utf-8 -*-
__title__ = u"Orient\n3D"
__doc__ = (u"Orient to View ของแปลนที่เปิดอยู่: ตั้ง Section Box ของ 3D view "
           u"ให้ตรงกับ Crop Region / View Range ของแปลนนั้น แล้วเปิด 3D ให้เลย")

# สคริปต์นี้ไม่มี shebang จึงรันด้วย IronPython engine ของ pyRevit
# (เลี่ยง error '3.12.3' ของ CPython engine) ถ้าอยากใช้ CPython3 ให้เติมบรรทัด
# shebang ของ python3 เป็นบรรทัดแรกสุดของไฟล์ โค้ดข้างล่างรองรับทั้งสองแบบ

from pyrevit import revit, DB, forms

doc = revit.doc
uidoc = revit.uidoc

VIEW_NAME = u"{3D - narongkuV4UU9}"   # ชื่อ 3D view ที่ใช้ซ้ำทุกครั้ง
LOOK_DOWN = True    # True = มองจากด้านบนเหมือนแปลน, False = คงมุมกล้อง 3D เดิม
PAD_FT = 0.0        # เผื่อขอบ Section Box (หน่วย ft)
TRIM_PCT = 2.0      # ตัด element ที่อยู่ไกลสุดขอบออกด้านละ x% (เฉพาะกรณีไม่ได้เปิด Crop) 0 = ไม่ตัด


def get_xy_bounds(view, zmin, zmax):
    """คืน (xmin, ymin, xmax, ymax) ในพิกัดโมเดล"""
    if view.CropBoxActive:
        # Crop Region อยู่ในพิกัดของ view -> แปลงเป็นพิกัดโมเดลด้วย Transform
        cb = view.CropBox
        t = cb.Transform
        xs, ys = [], []
        for x in (cb.Min.X, cb.Max.X):
            for y in (cb.Min.Y, cb.Max.Y):
                p = t.OfPoint(DB.XYZ(x, y, 0.0))
                xs.append(p.X)
                ys.append(p.Y)
        return min(xs), min(ys), max(xs), max(ys)

    # ไม่ได้เปิด Crop -> ใช้ขอบเขตของ element (เฉพาะ Model category) ที่เห็นในแปลน
    # เอาเฉพาะที่อยู่ในช่วงความสูงของ View Range แล้วตัด element ที่หลุดไกลออกไป
    items = []
    col = DB.FilteredElementCollector(doc, view.Id).WhereElementIsNotElementType()
    for el in col:
        cat = el.Category
        if cat is None or cat.CategoryType != DB.CategoryType.Model:
            continue
        bb = el.get_BoundingBox(None)
        if bb is None:
            continue
        if bb.Max.Z < zmin or bb.Min.Z > zmax:
            continue
        items.append((bb.Min.X, bb.Min.Y, bb.Max.X, bb.Max.Y))
    if not items:
        return None

    if TRIM_PCT > 0 and len(items) > 20:
        n = len(items)
        k = int(n * TRIM_PCT / 100.0)
        cxs = sorted((i[0] + i[2]) / 2.0 for i in items)
        cys = sorted((i[1] + i[3]) / 2.0 for i in items)
        xlo, xhi = cxs[k], cxs[n - 1 - k]
        ylo, yhi = cys[k], cys[n - 1 - k]
        kept = [i for i in items
                if xlo <= (i[0] + i[2]) / 2.0 <= xhi and ylo <= (i[1] + i[3]) / 2.0 <= yhi]
        if kept:
            items = kept

    return (min(i[0] for i in items), min(i[1] for i in items),
            max(i[2] for i in items), max(i[3] for i in items))


def get_z_range(view):
    """คืน (zmin, zmax, used_fallback) จาก View Range ทั้ง 4 ระนาบ"""
    vr = view.GetViewRange()
    elevs = sorted(l.Elevation for l in DB.FilteredElementCollector(doc).OfClass(DB.Level))
    gen = view.GenLevel
    base = gen.Elevation if gen is not None else (elevs[0] if elevs else 0.0)

    def above(b):
        higher = [e for e in elevs if e > b + 1e-6]
        return min(higher) if higher else b + 13.0

    def below(b):
        lower = [e for e in elevs if e < b - 1e-6]
        return max(lower) if lower else b - 3.0

    planes = (
        (DB.PlanViewPlane.TopClipPlane, True),
        (DB.PlanViewPlane.CutPlane, True),
        (DB.PlanViewPlane.BottomClipPlane, False),
        (DB.PlanViewPlane.ViewDepthPlane, False),
    )
    zs = []
    fallback = False
    for plane, upward in planes:
        lvl = None
        try:
            lvl = doc.GetElement(vr.GetLevelId(plane))
        except Exception:
            pass
        if lvl is not None and isinstance(lvl, DB.Level):
            zs.append(lvl.Elevation + vr.GetOffset(plane))
        else:
            # Unlimited / Level Above / Level Below -> ประมาณจาก Level ถัดไป
            zs.append(above(base) if upward else below(base))
            fallback = True
    zmin, zmax = min(zs), max(zs)
    if zmax - zmin < 0.01:
        zmax = zmin + 1.0
    return zmin, zmax, fallback


def find_view3d():
    for v in DB.FilteredElementCollector(doc).OfClass(DB.View3D):
        if not v.IsTemplate and v.Name == VIEW_NAME:
            return v
    return None


def create_view3d():
    vft = None
    for t in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType):
        if t.ViewFamily == DB.ViewFamily.ThreeDimensional:
            vft = t
            break
    if vft is None:
        return None
    v3 = DB.View3D.CreateIsometric(doc, vft.Id)
    v3.Name = VIEW_NAME
    return v3


def main():
    view = doc.ActiveView
    ok_types = (DB.ViewType.FloorPlan, DB.ViewType.CeilingPlan, DB.ViewType.EngineeringPlan)
    if not isinstance(view, DB.ViewPlan) or view.ViewType not in ok_types:
        forms.alert(u"เปิดแปลน (Floor / Ceiling / Structural Plan) ก่อนแล้วค่อยกดปุ่มนี้ครับ",
                    exitscript=True)

    zmin, zmax, fallback = get_z_range(view)
    bounds = get_xy_bounds(view, zmin, zmax)
    if bounds is None:
        forms.alert(u"ไม่พบขอบเขตของแปลนนี้ (ไม่มี Crop และไม่มี element ในมุมมอง)",
                    exitscript=True)
    xmin, ymin, xmax, ymax = bounds

    t = DB.Transaction(doc, "Orient 3D to plan")
    t.Start()
    try:
        v3 = find_view3d()
        if v3 is None:
            v3 = create_view3d()
        if v3 is None:
            raise Exception(u"ไม่พบ View Family Type แบบ 3D ในโปรเจกต์")

        # ถ้า 3D ถูกล็อกมุมมองไว้ ต้องปลดก่อนถึงจะเปลี่ยนมุมกล้องได้
        try:
            if v3.IsLocked:
                v3.Unlock()
        except Exception:
            pass

        bb = DB.BoundingBoxXYZ()
        bb.Min = DB.XYZ(xmin - PAD_FT, ymin - PAD_FT, zmin)
        bb.Max = DB.XYZ(xmax + PAD_FT, ymax + PAD_FT, zmax)
        # ปิดแล้วเปิดใหม่ เพื่อให้ Revit สร้างตัวกล่อง (Section Box element) ขึ้นมาใหม่ให้เห็น
        v3.IsSectionBoxActive = False
        doc.Regenerate()
        v3.SetSectionBox(bb)
        v3.IsSectionBoxActive = True
        doc.Regenerate()

        if LOOK_DOWN:
            eye = DB.XYZ((xmin + xmax) / 2.0, (ymin + ymax) / 2.0, zmax + 10.0)
            v3.SetOrientation(DB.ViewOrientation3D(eye, DB.XYZ(0, 1, 0), DB.XYZ(0, 0, -1)))
        t.Commit()
    except Exception as ex:
        t.RollBack()
        forms.alert(u"สร้าง/อัปเดต 3D ไม่สำเร็จ:\n{}".format(ex), exitscript=True)

    # สลับไปเปิด 3D หลังปิด transaction แล้ว แล้ว Zoom to Fit
    try:
        uidoc.ActiveView = v3
    except Exception:
        uidoc.RequestViewChange(v3)
    for uv in uidoc.GetOpenUIViews():
        if uv.ViewId.Equals(v3.Id):
            uv.ZoomToFit()
            break

    if fallback:
        forms.alert(u"View Range ของแปลนนี้เป็น Unlimited / Level Above-Below\n"
                    u"ความสูง Section Box จึงประมาณจาก Level ถัดไปแทนครับ")


main()
