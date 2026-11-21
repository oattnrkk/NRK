# -*- coding: utf-8 -*-
"""ลบ Imported DWG (Import CAD) ออกจากโมเดล Revit 2026
วิธีใช้: วางโฟลเดอร์ DeleteImportedDWG.pushbutton ไว้ใน pyRevit extension
แล้ว Reload pyRevit
"""
import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
from System.Windows import Controls, Thickness, Visibility
from pyrevit import revit, DB, forms, script

doc = revit.doc
output = script.get_output()


def id_val(i):
    # Revit 2026 ใช้ .Value (IntegerValue ถูกถอดออกแล้ว)
    return i.Value if hasattr(i, "Value") else i.IntegerValue


def type_is_linked(t):
    """CADLinkType: ใน API ชื่อ property คือ Linked (บางรุ่นอาจต่างกัน)
    ถ้าตรวจไม่ได้ให้ถือว่าเป็น Link เพื่อไม่ลบโดยไม่ตั้งใจ"""
    for name in ("Linked", "IsLinked"):
        if hasattr(t, name):
            try:
                return bool(getattr(t, name))
            except Exception:
                pass
    try:
        ref = t.GetExternalFileReference()
        return ref.ExternalFileReferenceType == DB.ExternalFileReferenceType.CADLink
    except Exception:
        return True


def view_name(imp):
    if imp.ViewSpecific:
        v = doc.GetElement(imp.OwnerViewId)
        return v.Name if v else "-"
    return "(Model)"


def cad_name(imp):
    t = doc.GetElement(imp.GetTypeId())
    if t:
        p = t.get_Parameter(DB.BuiltInParameter.ALL_MODEL_TYPE_NAME)
        if p:
            return p.AsString()
    return "?"


class Row(object):
    def __init__(self, imp):
        self.imp = imp
        self.label = u"{} | {} | {} | id {}".format(
            cad_name(imp), "Link" if imp.IsLinked else "Import",
            view_name(imp), id_val(imp.Id))


XAML = u"""
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Delete Imported DWG" Width="760" Height="580"
        WindowStartupLocation="CenterScreen" ShowInTaskbar="False">
  <Grid Margin="12">
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
      <RowDefinition Height="Auto"/>
    </Grid.RowDefinitions>
    <TextBlock x:Name="count_text" Grid.Row="0" FontSize="18" FontWeight="Bold"
               Foreground="#C0392B" Margin="0,0,0,8"/>
    <DockPanel Grid.Row="1" Margin="0,0,0,8">
      <Button Content="Check All" Click="check_all" Width="90" Margin="0,0,6,0"/>
      <Button Content="Uncheck All" Click="uncheck_all" Width="90" Margin="0,0,12,0"/>
      <TextBlock Text="Filter:" VerticalAlignment="Center" Margin="0,0,6,0"/>
      <TextBox x:Name="filter_box" TextChanged="filter_changed" Height="24"
               VerticalContentAlignment="Center"/>
    </DockPanel>
    <Border Grid.Row="2" BorderBrush="#BBB" BorderThickness="1">
      <ScrollViewer VerticalScrollBarVisibility="Auto">
        <StackPanel x:Name="list_panel" Margin="6"/>
      </ScrollViewer>
    </Border>
    <StackPanel Grid.Row="3" Orientation="Horizontal" HorizontalAlignment="Right"
                Margin="0,10,0,0">
      <Button x:Name="delete_btn" Content="ลบที่เลือก" Click="do_delete"
              Width="120" Height="30" Margin="0,0,8,0"/>
      <Button Content="ยกเลิก" Click="do_cancel" Width="90" Height="30"/>
    </StackPanel>
  </Grid>
</Window>
"""


class SelectWindow(forms.WPFWindow):
    def __init__(self, rows):
        forms.WPFWindow.__init__(self, XAML, literal_string=True)
        self.boxes = []
        self.selected = []
        for r in rows:
            cb = Controls.CheckBox()
            cb.Content = r.label
            cb.Tag = r
            cb.Margin = Thickness(2)
            cb.Checked += self.on_change
            cb.Unchecked += self.on_change
            self.list_panel.Children.Add(cb)
            self.boxes.append(cb)
        self.update_count()

    def visible_boxes(self):
        return [b for b in self.boxes if b.Visibility == Visibility.Visible]

    def update_count(self, *args):
        total = len(self.boxes)
        checked = len([b for b in self.boxes if b.IsChecked])
        shown = len(self.visible_boxes())
        txt = u"เลือกแล้ว {} / {} รายการ".format(checked, total)
        if shown != total:
            txt += u"   (กำลังแสดง {} รายการ)".format(shown)
        self.count_text.Text = txt
        self.delete_btn.IsEnabled = checked > 0
        self.delete_btn.Content = u"ลบที่เลือก ({})".format(checked)

    def on_change(self, sender, args):
        self.update_count()

    def check_all(self, sender, args):
        # เลือกเฉพาะรายการที่แสดงอยู่ (ตาม Filter)
        for b in self.visible_boxes():
            b.IsChecked = True
        self.update_count()

    def uncheck_all(self, sender, args):
        for b in self.boxes:
            b.IsChecked = False
        self.update_count()

    def filter_changed(self, sender, args):
        key = (self.filter_box.Text or u"").lower()
        for b in self.boxes:
            ok = key in b.Content.lower()
            b.Visibility = Visibility.Visible if ok else Visibility.Collapsed
        self.update_count()

    def do_delete(self, sender, args):
        self.selected = [b.Tag for b in self.boxes if b.IsChecked]
        self.Close()

    def do_cancel(self, sender, args):
        self.selected = []
        self.Close()


# 1) เลือกขอบเขต
choice = forms.CommandSwitchWindow.show(
    [u"เฉพาะ Imported CAD (แนะนำ)", u"Imported + Linked CAD"],
    message=u"เลือกขอบเขตการลบ")
if not choice:
    script.exit()
include_linked = choice.startswith(u"Imported +")

# 2) รวบรวม ImportInstance (รวมที่อยู่ใน view-specific)
items = [i for i in DB.FilteredElementCollector(doc).OfClass(DB.ImportInstance)
         if include_linked or not i.IsLinked]
if not items:
    forms.alert(u"ไม่พบ DWG ที่ Import ไว้ในโมเดลนี้", exitscript=True)

rows = sorted([Row(i) for i in items], key=lambda r: r.label)

# 3) หน้าต่างเลือก พร้อมตัวนับ
win = SelectWindow(rows)
win.ShowDialog()
picked = win.selected
if not picked:
    script.exit()

if not forms.alert(u"ยืนยันลบ {} รายการ?".format(len(picked)), yes=True, no=True):
    script.exit()

# 4) ลบ
ok, fail = 0, []
with revit.Transaction(u"Delete Imported DWG"):
    for r in picked:
        try:
            if r.imp.Pinned:
                r.imp.Pinned = False
            doc.Delete(r.imp.Id)
            ok += 1
        except Exception as ex:
            fail.append((r.label, str(ex)))

    # เก็บกวาด CAD Type ที่ไม่มี instance เหลือ
    used_types = set(id_val(i.GetTypeId()) for i in
                     DB.FilteredElementCollector(doc).OfClass(DB.ImportInstance))
    for t in DB.FilteredElementCollector(doc).OfClass(DB.CADLinkType):
        if id_val(t.Id) in used_types:
            continue
        if type_is_linked(t) and not include_linked:
            continue
        try:
            doc.Delete(t.Id)
        except Exception:
            pass

output.print_md(u"## เสร็จสิ้น\n- ลบสำเร็จ: **{}**\n- ล้มเหลว: **{}**".format(ok, len(fail)))
for label, msg in fail:
    output.print_md(u"- {} → {}".format(label, msg))