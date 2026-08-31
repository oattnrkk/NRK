#! python2
# -*- coding: utf-8 -*-
__title__ = "Auto Dimension\nby Select"
__author__ = "nrk"
__doc__ = """เลือก Grid หรือ Detail Line อย่างน้อย 2 ชิ้นก่อนกดปุ่มนี้

จากนั้นคลิกเลือกตำแหน่งที่ต้องการวางเส้นบอกขนาด (Dimension) บนหน้าจอ
ทิศทางของเส้นจะคำนวณให้อัตโนมัติจากวัตถุที่เลือก"""

import time
from Autodesk.Revit.DB import (
    Grid,
    DetailCurve,
    Reference,
    ReferenceArray,
    Line,
    SketchPlane,
    Plane,
    XYZ,
)
from Autodesk.Revit.UI.Selection import ObjectSnapTypes
from pyrevit import revit, forms

t_start = time.time()

doc = revit.doc
uidoc = revit.uidoc
view = doc.ActiveView

# 1. ตรวจสอบว่ามีการเลือกวัตถุมาก่อนหรือยัง
selection_ids = uidoc.Selection.GetElementIds()

if not selection_ids:
    forms.alert(
        "กรุณาเลือก Grid หรือ Detail Line อย่างน้อย 2 ชิ้นก่อนกดปุ่มนี้",
        title="Auto Dimension",
        exitscript=True,
    )

# 2. เก็บ Reference และหาทิศทางของวัตถุที่เลือก (ใช้กำหนดแนวเส้น Dimension)
t_loop_start = time.time()
ref_array = ReferenceArray()
direction_vector = None

for eid in selection_ids:
    element = doc.GetElement(eid)

    if isinstance(element, Grid):
        ref_array.Append(Reference(element))
        grid_curve = element.Curve
        if grid_curve and direction_vector is None:
            p0 = grid_curve.GetEndPoint(0)
            p1 = grid_curve.GetEndPoint(1)
            direction_vector = p1.Subtract(p0).Normalize()

    elif isinstance(element, DetailCurve):
        ref_array.Append(Reference(element))
        curve = element.GeometryCurve
        if direction_vector is None:
            p0 = curve.GetEndPoint(0)
            p1 = curve.GetEndPoint(1)
            direction_vector = p1.Subtract(p0).Normalize()
t_loop_end = time.time()

if ref_array.Size < 2:
    forms.alert(
        "ไม่พบ Grid หรือ Detail Line ที่เลือกอย่างน้อย 2 ชิ้น\n"
        "กรุณาเลือกวัตถุที่รองรับแล้วลองใหม่อีกครั้ง",
        title="Auto Dimension",
        exitscript=True,
    )

# 3. บางวิว (เช่น Drafting View / Legend) ไม่มี Work Plane ผูกไว้อัตโนมัติ
#    ต้องสร้าง/กำหนด Work Plane ให้วิวก่อน ไม่งั้น PickPoint จะ error
try:
    needs_sketch_plane = view.SketchPlane is None
except Exception:
    needs_sketch_plane = False

t_wp_start = time.time()
if needs_sketch_plane:
    try:
        with revit.Transaction("Set temporary work plane for view"):
            # หา origin ของวิว ถ้าวิวไม่มี property นี้ (เช่น Floor Plan) ให้ใช้ (0,0,0) แทน
            try:
                plane_origin = view.Origin
            except Exception:
                plane_origin = XYZ.Zero

            # ใช้ทิศทางการมองของวิวเป็นแนวตั้งฉากของ Work Plane
            plane = Plane.CreateByNormalAndOrigin(view.ViewDirection, plane_origin)
            temp_sketch_plane = SketchPlane.Create(doc, plane)
            view.SketchPlane = temp_sketch_plane
    except Exception as ex:
        forms.alert(
            "ไม่สามารถตั้ง Work Plane ให้วิวนี้ได้\n\nรายละเอียด error:\n{}".format(ex),
            title="Auto Dimension - Debug",
            exitscript=True,
        )
t_wp_end = time.time()

# 4. ให้ผู้ใช้คลิกเลือกตำแหน่งที่จะวางเส้น Dimension เอง
t_before_pick = time.time()
try:
    # หมายเหตุ: ใช้ getattr เพราะ "None" เป็นคำสงวนของ Python
    # เขียนแบบ ObjectSnapTypes.None ตรงๆ จะ syntax error
    no_snap = getattr(ObjectSnapTypes, "None")
    pick_point = uidoc.Selection.PickPoint(
        no_snap, "คลิกเลือกตำแหน่งที่จะวางเส้นบอกขนาด"
    )
except Exception as ex:
    # แสดง error จริงออกมา แทนที่จะสรุปเหมารวมว่าเป็นการยกเลิก
    # (ถ้าเป็นการกด Esc จริงๆ ข้อความ error มักจะมีคำว่า "cancel" อยู่ในนั้น)
    forms.alert(
        "ไม่สามารถเลือกตำแหน่งได้\n\nรายละเอียด error:\n{}".format(ex),
        title="Auto Dimension - Debug",
        exitscript=True,
    )
    pick_point = None
t_after_pick = time.time()

# 5. สร้าง Dimension ตามตำแหน่งที่เลือก
t_dim_start = time.time()
if pick_point:
    with revit.Transaction("Auto Dimension on Select"):
        if direction_vector is None:
            # ถ้าหาทิศทางของวัตถุไม่ได้ ให้ใช้ทิศทางของหน้าจอแทน
            dim_direction = view.RightDirection
        else:
            # หาเวกเตอร์ที่ตั้งฉากกับวัตถุ (Cross Product กับ View Direction)
            dim_direction = direction_vector.CrossProduct(view.ViewDirection).Normalize()

        line_end_point = pick_point.Add(dim_direction.Multiply(0.5))
        line = Line.CreateBound(pick_point, line_end_point)

        doc.Create.NewDimension(view, line, ref_array)
    t_dim_end = time.time()

    # สรุปเวลาที่ใช้ในแต่ละขั้นตอน (หน่วยวินาที) เพื่อหาว่าจุดไหนช้าจริงๆ
    # หมายเหตุ: "รอคลิกจุด" นับเวลาที่ผู้ใช้ใช้เลือกตำแหน่งเองด้วย ไม่ใช่เวลาประมวลผลล้วนๆ
    timing_report = (
        "สรุปเวลาแต่ละขั้นตอน (วินาที):\n"
        "- อ่านค่าที่เลือกไว้: {:.2f}\n"
        "- ตั้ง Work Plane: {:.2f}\n"
        "- รอคลิกจุด (รวมเวลาที่ผู้ใช้เล็ง): {:.2f}\n"
        "- สร้าง Dimension จริง: {:.2f}\n"
        "- รวมทั้งหมดตั้งแต่กดปุ่ม: {:.2f}"
    ).format(
        t_loop_end - t_loop_start,
        t_wp_end - t_wp_start,
        t_after_pick - t_before_pick,
        t_dim_end - t_dim_start,
        t_dim_end - t_start,
    )

    forms.alert(
        "สร้าง Dimension ตามตำแหน่งที่คุณเลือกเรียบร้อยแล้วครับ!\n\n" + timing_report,
        title="Auto Dimension",
    )
