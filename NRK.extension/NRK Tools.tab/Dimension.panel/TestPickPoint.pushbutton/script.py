#! python2
# -*- coding: utf-8 -*-
__title__ = "Test\nPickPoint"
__author__ = "nrk"
__doc__ = "ปุ่มทดสอบเปล่าๆ เรียก PickPoint อย่างเดียว ไม่มีโค้ดอื่นเลย เพื่อเช็คว่าช้าเพราะ PickPoint ล้วนๆ หรือเปล่า"

import time
from Autodesk.Revit.UI.Selection import ObjectSnapTypes
from pyrevit import revit, forms

uidoc = revit.uidoc

t0 = time.time()
try:
    no_snap = getattr(ObjectSnapTypes, "None")
    pt = uidoc.Selection.PickPoint(no_snap, "คลิกที่ไหนก็ได้ (ทดสอบ)")
except Exception as ex:
    forms.alert("ยกเลิก/error: {}".format(ex), title="Test PickPoint")
    pt = None
t1 = time.time()

forms.alert(
    "PickPoint ใช้เวลา: {:.2f} วินาที\n(นับตั้งแต่เรียกจนกดเมาส์เสร็จ)".format(t1 - t0),
    title="Test PickPoint",
)
