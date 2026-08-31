# -*- coding: utf-8 -*-
"""Reverse Grid/Level Bubbles

สลับสถานะการแสดงหัวลูกศร (bubble) ทั้ง 2 ฝั่ง (End0/End1) ของ Grid และ Level
ที่ถูกเลือกไว้ ในมุมมอง (view) ปัจจุบัน

วิธีใช้: เลือก Grid และ/หรือ Level ในโมเดลก่อน แล้วกดปุ่มนี้
แปลงมาจากไฟล์ Dynamo: reverse_grid-level_by_nrk.dyn
"""
__title__ = "Reverse\nGrid/Level"
__author__ = "nrk"
__doc__ = (
    "สลับสถานะการแสดง bubble (หัวลูกศร) ทั้งสองฝั่งของ Grid/Level "
    "ที่เลือกไว้ ในมุมมองปัจจุบัน\n\n"
    "วิธีใช้: เลือก Grid หรือ Level ก่อน แล้วกดปุ่มนี้"
)

from Autodesk.Revit.DB import Grid, Level, DatumEnds
from pyrevit import revit, forms

doc = revit.doc
uidoc = revit.uidoc
current_view = doc.ActiveView

# 1. ดึง Elements ทั้งหมดที่ถูกเลือกไว้
selection_ids = uidoc.Selection.GetElementIds()

if not selection_ids:
    forms.alert(
        "กรุณาเลือก Grid หรือ Level ก่อนรันคำสั่งนี้ครับ",
        title="ยังไม่ได้เลือก Element",
        exitscript=True,
    )

grid_counter = 0
level_counter = 0

# 2. เริ่มกระบวนการตรวจสอบและสลับฝั่งติ๊กถูก
with revit.Transaction("Reverse Grid/Level Bubbles"):
    for eid in selection_ids:
        element = doc.GetElement(eid)

        # กรองเฉพาะ Element ที่เป็น Grid หรือ Level
        if isinstance(element, Grid) or isinstance(element, Level):

            # ตรวจสอบสถานะการติ๊กถูก ณ ปัจจุบันใน View นี้
            is_end0_checked = element.IsBubbleVisibleInView(DatumEnds.End0, current_view)
            is_end1_checked = element.IsBubbleVisibleInView(DatumEnds.End1, current_view)

            # ฝั่งแรก (End0): ถ้าเปิดอยู่ -> ปิด / ถ้าปิดอยู่ -> เปิด
            if is_end0_checked:
                element.HideBubbleInView(DatumEnds.End0, current_view)
            else:
                element.ShowBubbleInView(DatumEnds.End0, current_view)

            # ฝั่งสอง (End1): ถ้าเปิดอยู่ -> ปิด / ถ้าปิดอยู่ -> เปิด
            if is_end1_checked:
                element.HideBubbleInView(DatumEnds.End1, current_view)
            else:
                element.ShowBubbleInView(DatumEnds.End1, current_view)

            # นับจำนวนแยกประเภทเพื่อรายงานผล
            if isinstance(element, Grid):
                grid_counter += 1
            elif isinstance(element, Level):
                level_counter += 1

# 3. แจ้งเตือนเฉพาะกรณีที่ไม่พบ Grid/Level ในสิ่งที่เลือกไว้ (ทำงานสำเร็จแล้วไม่ต้องเด้งอะไร)
if grid_counter == 0 and level_counter == 0:
    forms.alert(
        "ไม่พบ Grid หรือ Level ในสิ่งที่เลือกไว้ครับ",
        title="ไม่มี Element ที่ตรงเงื่อนไข",
    )
