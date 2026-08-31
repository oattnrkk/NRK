# -*- coding: utf-8 -*-
"""Export sheets to DWG and/or PDF with custom naming rules,
sheet set filtering, search, and hide-unchecked toggle."""
__title__ = "Export\nDWG / PDF"
__author__ = "Oat"

import clr
import os
import json
import System

clr.AddReference('System.Windows.Forms')
clr.AddReference('System.Drawing')
import System.Windows.Forms as WinForms
import System.Drawing as Drawing
from System.Windows.Forms import *
from System.Drawing import *

clr.AddReference('RevitAPI')
from Autodesk.Revit.DB import *

clr.AddReference('RevitAPIUI')
from Autodesk.Revit.UI import TaskDialog

from pyrevit import revit

doc = revit.doc

# ป้องกันชื่อชนกับ Autodesk.Revit.DB (RevitAPI import ทับชื่อของ WinForms/System.Drawing)
Form = WinForms.Form
Label = WinForms.Label
ComboBox = WinForms.ComboBox
TextBox = WinForms.TextBox
CheckedListBox = WinForms.CheckedListBox
CheckBox = WinForms.CheckBox
DataGridView = WinForms.DataGridView
Button = WinForms.Button
FormStartPosition = WinForms.FormStartPosition
ComboBoxStyle = WinForms.ComboBoxStyle
MessageBox = WinForms.MessageBox
Control = WinForms.Control
Keys = WinForms.Keys
CheckState = WinForms.CheckState
FolderBrowserDialog = WinForms.FolderBrowserDialog
DialogResult = WinForms.DialogResult
ProgressBar = WinForms.ProgressBar
MessageBoxButtons = WinForms.MessageBoxButtons
MessageBoxIcon = WinForms.MessageBoxIcon
WinFormsApp = WinForms.Application

Color = Drawing.Color
Point = Drawing.Point
Size = Drawing.Size
Font = Drawing.Font
FontStyle = Drawing.FontStyle

# ---------- helper: ทำความสะอาดชื่อไฟล์ ----------
INVALID_CHARS = '\\/:*?"<>|'
def sanitize(text):
    if text is None:
        return ""
    for c in INVALID_CHARS:
        text = text.replace(c, "_")
    return text

# 1. เตรียมข้อมูลชีททั้งหมดในโมเดล
all_sheets = FilteredElementCollector(doc).OfClass(ViewSheet).ToElements()
all_sheets = sorted(all_sheets, key=lambda s: s.SheetNumber)

setup_names = []
collector = FilteredElementCollector(doc).OfClass(ExportDWGSettings).ToElements()
for s in collector:
    if s.Name and not s.Name.startswith(" "):
        setup_names.append(s.Name)
if not setup_names:
    setup_names = ["Standard_DWG"]

desktop_path = os.path.join(os.path.expanduser('~'), 'Desktop')

# ---------- helper: จำค่า config ล่าสุด (setup, output folder, naming rows, sheet set, formats) ----------
CONFIG_DIR = os.path.join(os.environ.get('APPDATA', desktop_path), 'PyRevitExportDWGPDF')
CONFIG_PATH = os.path.join(CONFIG_DIR, 'last_config.json')

def load_last_config():
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, 'r') as f:
                return json.load(f)
    except:
        pass
    return None

def save_last_config(cfg):
    try:
        if not os.path.exists(CONFIG_DIR):
            os.makedirs(CONFIG_DIR)
        with open(CONFIG_PATH, 'w') as f:
            json.dump(cfg, f)
    except:
        pass

last_config = load_last_config()

# 2. รวบรวมรายชื่อ parameter ที่มีให้เลือก (จากชีทแรกที่มีในโมเดล)
param_names = []
if all_sheets:
    sample_sheet = all_sheets[0]
    for p in sample_sheet.Parameters:
        try:
            nm = p.Definition.Name
            if nm and nm not in param_names:
                param_names.append(nm)
        except:
            pass
    param_names = sorted(param_names)

if "Sheet Number" not in param_names:
    param_names.insert(0, "Sheet Number")
if "Sheet Name" not in param_names:
    param_names.insert(1, "Sheet Name")

def get_param_value(sheet_obj, name):
    p = sheet_obj.LookupParameter(name)
    if p is None:
        return ""
    try:
        if p.StorageType == StorageType.String:
            return p.AsString() or ""
        else:
            return p.AsValueString() or ""
    except:
        return ""

# ---------- ดึง Sheet Set ที่มีอยู่แล้วในโมเดล (จาก Print Setup / Sheet Issue/Revisions) ----------
sheet_id_to_index = {}
for i, s in enumerate(all_sheets):
    sheet_id_to_index[s.Id] = i

MANUAL_SELECTION_LABEL = "(Manual Selection - เลือกเอง)"
sheet_set_names_all = []
sheet_set_map = {}  # name -> list of index ใน all_sheets

try:
    all_view_sheet_sets = FilteredElementCollector(doc).OfClass(ViewSheetSet).ToElements()
    for vss in all_view_sheet_sets:
        indices = []
        try:
            for v in vss.Views:
                if isinstance(v, ViewSheet) and v.Id in sheet_id_to_index:
                    indices.append(sheet_id_to_index[v.Id])
        except:
            pass
        if indices:
            sheet_set_names_all.append(vss.Name)
            sheet_set_map[vss.Name] = indices
except:
    pass

sheetset_options = [MANUAL_SELECTION_LABEL] + sheet_set_names_all

# ---------- helper: สร้าง control แบบปลอดภัยสำหรับ .NET 8 hosting ----------
def new_ctrl(ctrl_type, **kwargs):
    obj = System.Activator.CreateInstance[ctrl_type]()
    for k, v in kwargs.items():
        setattr(obj, k, v)
    return obj

# ---------- Form ----------
win = new_ctrl(Form)
win.Text = "Export DWG / PDF - Setup Naming Rules"
win.Size = Size(1100, 660)
win.MinimumSize = Size(1100, 660)
win.StartPosition = FormStartPosition.CenterScreen
win.BackColor = Color.FromArgb(240, 240, 240)
win.Font = Font("Segoe UI", 9)
win.TopMost = True

# 1. ส่วนเลือก Export Setup & Folder
lbl_setup = new_ctrl(Label, Text="Export Setup:", Location=Point(20, 20), Size=Size(100, 23))
cb_setup = new_ctrl(ComboBox, Location=Point(120, 17), Size=Size(200, 23), DropDownStyle=ComboBoxStyle.DropDownList)
cb_setup.Items.AddRange(System.Array[System.Object](setup_names))
if last_config and last_config.get("setup") in setup_names:
    cb_setup.SelectedIndex = setup_names.index(last_config["setup"])
else:
    cb_setup.SelectedIndex = 0

lbl_folder = new_ctrl(Label, Text="Output Folder:", Location=Point(350, 20), Size=Size(90, 23))
initial_folder = desktop_path
if last_config and last_config.get("output_folder"):
    initial_folder = last_config["output_folder"]
txt_folder = new_ctrl(TextBox, Location=Point(440, 17), Size=Size(320, 23), Text=initial_folder, ReadOnly=True)
btn_browse = new_ctrl(Button, Text="Browse...", Location=Point(770, 16), Size=Size(90, 25))

def browse_clicked(sender, event):
    dlg = new_ctrl(FolderBrowserDialog)
    dlg.Description = "เลือกโฟลเดอร์ปลายทางสำหรับ Export"
    dlg.SelectedPath = txt_folder.Text
    if dlg.ShowDialog() == DialogResult.OK:
        txt_folder.Text = dlg.SelectedPath
btn_browse.Click += browse_clicked

# 1b. ส่วนเลือก Sheet Set ที่มีอยู่แล้ว + เลือกฟอร์แมตที่จะ export
lbl_sheetset = new_ctrl(Label, Text="Sheet Set:", Location=Point(20, 50), Size=Size(90, 23))
cb_sheetset = new_ctrl(ComboBox, Location=Point(115, 47), Size=Size(230, 23), DropDownStyle=ComboBoxStyle.DropDownList)
cb_sheetset.Items.AddRange(System.Array[System.Object](sheetset_options))
restore_sheetset = MANUAL_SELECTION_LABEL
if last_config and last_config.get("sheet_set") in sheetset_options:
    restore_sheetset = last_config["sheet_set"]
cb_sheetset.SelectedIndex = sheetset_options.index(restore_sheetset)

default_export_dwg = True
default_export_pdf = True
if last_config:
    default_export_dwg = last_config.get("export_dwg", True)
    default_export_pdf = last_config.get("export_pdf", True)

chk_export_dwg = new_ctrl(CheckBox, Text="Export DWG", Location=Point(360, 49), Size=Size(100, 23), Checked=default_export_dwg)
chk_export_pdf = new_ctrl(CheckBox, Text="Export PDF", Location=Point(460, 49), Size=Size(100, 23), Checked=default_export_pdf)
lbl_sheetset_hint = new_ctrl(Label, Text="(เลือก Sheet Set แล้วระบบจะติ๊กชีทให้อัตโนมัติ ปรับเพิ่ม/ลดต่อได้)", Location=Point(560, 50), Size=Size(500, 23), ForeColor=Color.DimGray, Font=Font("Segoe UI", 8))

# 2. โซนเลือก Sheets (ซ้ายมือ)
lbl_sheets = new_ctrl(Label, Text="Select Sheets to Export:", Location=Point(20, 90), Size=Size(150, 23))
btn_check_all = new_ctrl(Button, Text="Check All", Location=Point(175, 88), Size=Size(75, 23))
btn_uncheck_all = new_ctrl(Button, Text="Uncheck All", Location=Point(255, 88), Size=Size(80, 23))
lbl_selected_count = new_ctrl(Label, Text="Selected: 0 / 0", Location=Point(340, 90), Size=Size(110, 23), ForeColor=Color.DimGray)
chk_hide_unchecked = new_ctrl(CheckBox, Text="Hide un-checked sheets", Location=Point(460, 91), Size=Size(180, 23))

txt_search = new_ctrl(TextBox, Location=Point(20, 115), Size=Size(345, 23))
btn_search = new_ctrl(Button, Text="Search", Location=Point(370, 114), Size=Size(80, 25))

chk_list_sheets = new_ctrl(CheckedListBox, Location=Point(20, 145), Size=Size(430, 380), CheckOnClick=True)

lbl_progress = new_ctrl(Label, Text="", Location=Point(20, 530), Size=Size(430, 20), ForeColor=Color.DarkBlue, Visible=False)
progress_bar = new_ctrl(ProgressBar, Location=Point(20, 552), Size=Size(430, 20), Minimum=0, Maximum=100, Value=0, Visible=False)

def sheet_label(s):
    return "{0} - {1}".format(s.SheetNumber, s.Name)

sheet_checked_state = {}
for i in range(len(all_sheets)):
    sheet_checked_state[i] = False

displayed_indices = []
last_checked_display_index = [-1]
is_range_updating = [False]
is_rebuilding = [False]
pending_hide_refresh = [False]

def update_selected_count():
    checked_count = sum(1 for v in sheet_checked_state.values() if v)
    lbl_selected_count.Text = "Selected: {0} / {1}".format(checked_count, len(all_sheets))

def sync_checked_state():
    for display_i in range(chk_list_sheets.Items.Count):
        if display_i < len(displayed_indices):
            orig_idx = displayed_indices[display_i]
            sheet_checked_state[orig_idx] = chk_list_sheets.GetItemChecked(display_i)

def rebuild_sheet_list(filter_text=""):
    # is_rebuilding กันไม่ให้ ItemCheck ที่ถูกยิงจาก Items.Add(label, checked) ไปสั่ง rebuild ซ้อนตัวเอง
    is_rebuilding[0] = True
    try:
        displayed_indices[:] = []
        chk_list_sheets.Items.Clear()
        last_checked_display_index[0] = -1
        ft = (filter_text or "").lower().strip()
        hide_unchecked = chk_hide_unchecked.Checked
        for i, s in enumerate(all_sheets):
            label = sheet_label(s)
            is_checked = sheet_checked_state.get(i, False)
            if hide_unchecked and not is_checked:
                continue
            if ft == "" or ft in label.lower():
                chk_list_sheets.Items.Add(label, is_checked)
                displayed_indices.append(i)
        update_selected_count()
    finally:
        is_rebuilding[0] = False

def refresh_sheet_list(filter_text=""):
    sync_checked_state()
    rebuild_sheet_list(filter_text)

def on_item_check(sender, e):
    if is_range_updating[0] or is_rebuilding[0]:
        return
    shift_held = (Control.ModifierKeys & Keys.Shift) == Keys.Shift
    idx = e.Index
    new_val = (e.NewValue == CheckState.Checked)
    if shift_held and last_checked_display_index[0] != -1:
        start = min(last_checked_display_index[0], idx)
        end = max(last_checked_display_index[0], idx)
        is_range_updating[0] = True
        for i in range(start, end + 1):
            chk_list_sheets.SetItemChecked(i, new_val)
            if i < len(displayed_indices):
                sheet_checked_state[displayed_indices[i]] = new_val
        is_range_updating[0] = False
    else:
        if idx < len(displayed_indices):
            sheet_checked_state[displayed_indices[idx]] = new_val
    last_checked_display_index[0] = idx
    update_selected_count()
    # เลื่อนไป refresh หลังจาก event ปัจจุบันจบ และรวมหลายๆ ครั้งให้เหลือ 1 คิวเดียว กัน infinite loop / ค้าง
    if chk_hide_unchecked.Checked and not pending_hide_refresh[0]:
        pending_hide_refresh[0] = True
        def do_hide_refresh():
            pending_hide_refresh[0] = False
            rebuild_sheet_list(txt_search.Text)
        chk_list_sheets.BeginInvoke(System.Action(do_hide_refresh))

chk_list_sheets.ItemCheck += on_item_check

def hide_unchecked_toggled(sender, event):
    rebuild_sheet_list(txt_search.Text)
chk_hide_unchecked.CheckedChanged += hide_unchecked_toggled

refresh_sheet_list("")

txt_search.TextChanged += lambda s, e: refresh_sheet_list(txt_search.Text)
btn_search.Click += lambda s, e: refresh_sheet_list(txt_search.Text)

def check_all_clicked(sender, event):
    for i in range(len(all_sheets)):
        sheet_checked_state[i] = True
    rebuild_sheet_list(txt_search.Text)
btn_check_all.Click += check_all_clicked

def uncheck_all_clicked(sender, event):
    for i in range(len(all_sheets)):
        sheet_checked_state[i] = False
    rebuild_sheet_list(txt_search.Text)
btn_uncheck_all.Click += uncheck_all_clicked

# ---------- เลือก Sheet Set -> ติ๊กชีทที่อยู่ใน Sheet Set นั้นให้อัตโนมัติ ----------
def apply_sheet_set_selection(sender, event):
    selected_name = cb_sheetset.SelectedItem
    if selected_name is None or str(selected_name) == MANUAL_SELECTION_LABEL:
        return
    target_indices = set(sheet_set_map.get(str(selected_name), []))
    for i in range(len(all_sheets)):
        sheet_checked_state[i] = (i in target_indices)
    rebuild_sheet_list(txt_search.Text)

cb_sheetset.SelectedIndexChanged += apply_sheet_set_selection

# ถ้ามี config เก่าที่จำ sheet set ไว้ (ไม่ใช่ Manual) ให้ apply ทันทีตอนเปิดฟอร์ม
if str(cb_sheetset.SelectedItem) != MANUAL_SELECTION_LABEL:
    apply_sheet_set_selection(None, None)

# 3. โซนจัดสเต็ปการตั้งชื่อ (ขวามือ)
lbl_naming = new_ctrl(Label, Text="Name Parameters (In Order):", Location=Point(480, 90), Size=Size(250, 23))
dgv = new_ctrl(DataGridView, Location=Point(480, 115), Size=Size(580, 220), AllowUserToAddRows=False, RowHeadersVisible=False)
dgv.ColumnCount = 5
dgv.Columns[0].Name = "Name"
dgv.Columns[0].ReadOnly = True
dgv.Columns[0].Width = 140
dgv.Columns[1].Name = "Prefix"
dgv.Columns[1].Width = 100
dgv.Columns[2].Name = "Sample Value"
dgv.Columns[2].ReadOnly = True
dgv.Columns[2].Width = 120
dgv.Columns[3].Name = "Suffix"
dgv.Columns[3].Width = 100
dgv.Columns[4].Name = "Separator"
dgv.Columns[4].Width = 100

def sample_value_for(name):
    if all_sheets:
        return get_param_value(all_sheets[0], name)
    return name

def build_filename(sheet_obj, rows):
    parts = []
    row_count = len(rows)
    for i, row in enumerate(rows):
        value = sanitize(get_param_value(sheet_obj, row["name"]))
        content = "{0}{1}{2}".format(row["prefix"], value, row["suffix"])
        if i < row_count - 1:
            parts.append(content + row["separator"])
        else:
            parts.append(content)
    return "".join(parts)

def add_row(name, prefix="", suffix="", separator="_"):
    i = dgv.Rows.Add()
    row = dgv.Rows[i]
    row.Cells[0].Value = name
    row.Cells[1].Value = prefix
    row.Cells[2].Value = sample_value_for(name)
    row.Cells[3].Value = suffix
    row.Cells[4].Value = separator

if last_config and last_config.get("rows"):
    for r in last_config["rows"]:
        add_row(r.get("name", ""), prefix=r.get("prefix", ""), suffix=r.get("suffix", ""), separator=r.get("separator", "_"))
else:
    add_row("Sheet Number", prefix="CHB2A-", separator="_")
    add_row("Sheet Name", separator="")

# 3b. แถบเพิ่ม/ลบ/ย้ายลำดับ parameter
lbl_add = new_ctrl(Label, Text="Add Parameter:", Location=Point(480, 345), Size=Size(100, 23))
cb_add_param = new_ctrl(ComboBox, Location=Point(480, 368), Size=Size(200, 23), DropDownStyle=ComboBoxStyle.DropDownList)
cb_add_param.Items.AddRange(System.Array[System.Object](param_names))
if param_names:
    cb_add_param.SelectedIndex = 0

def add_param_clicked(sender, event):
    if cb_add_param.SelectedItem:
        add_row(str(cb_add_param.SelectedItem))
        update_preview(None, None)
btn_add_param = new_ctrl(Button, Text="Add >>", Location=Point(690, 367), Size=Size(80, 25))
btn_add_param.Click += add_param_clicked

def remove_row_clicked(sender, event):
    if dgv.CurrentRow is not None and dgv.Rows.Count > 1:
        dgv.Rows.RemoveAt(dgv.CurrentRow.Index)
        update_preview(None, None)
btn_remove_row = new_ctrl(Button, Text="Remove Row", Location=Point(780, 367), Size=Size(100, 25))
btn_remove_row.Click += remove_row_clicked

def move_row(direction):
    if dgv.CurrentRow is None:
        return
    i = dgv.CurrentRow.Index
    j = i + direction
    if j < 0 or j >= dgv.Rows.Count:
        return
    vals_i = [dgv.Rows[i].Cells[k].Value for k in range(5)]
    vals_j = [dgv.Rows[j].Cells[k].Value for k in range(5)]
    for k in range(5):
        dgv.Rows[i].Cells[k].Value = vals_j[k]
        dgv.Rows[j].Cells[k].Value = vals_i[k]
    dgv.CurrentCell = dgv.Rows[j].Cells[0]
    update_preview(None, None)

btn_up = new_ctrl(Button, Text="Move Up", Location=Point(480, 400), Size=Size(100, 25))
btn_up.Click += lambda s, e: move_row(-1)
btn_down = new_ctrl(Button, Text="Move Down", Location=Point(590, 400), Size=Size(100, 25))
btn_down.Click += lambda s, e: move_row(1)

# 4. ส่วน Preview หน้าตาชื่อไฟล์
lbl_preview_title = new_ctrl(Label, Text="Preview of value:", Location=Point(480, 440), Size=Size(110, 23), Font=Font("Segoe UI", 9, FontStyle.Bold))
lbl_preview = new_ctrl(Label, Text="", Location=Point(480, 465), Size=Size(580, 40), ForeColor=Color.DarkBlue)

def update_preview(sender, event):
    try:
        parts = []
        row_count = dgv.Rows.Count
        for i in range(row_count):
            prefix = dgv.Rows[i].Cells[1].Value or ""
            sample = dgv.Rows[i].Cells[2].Value or ""
            suffix = dgv.Rows[i].Cells[3].Value or ""
            sep = dgv.Rows[i].Cells[4].Value or ""
            content = "{0}{1}{2}".format(prefix, sample, suffix)
            if i < row_count - 1:
                parts.append(content + sep)
            else:
                parts.append(content)
        lbl_preview.Text = "".join(parts)
    except:
        pass

dgv.CellValueChanged += update_preview
dgv.CellEndEdit += update_preview
update_preview(None, None)

# 5. ฟังก์ชันปุ่มสั่งการ
def export_clicked(sender, event):
    sync_checked_state()
    checked_indices = [i for i in range(len(all_sheets)) if sheet_checked_state.get(i, False)]

    export_dwg = chk_export_dwg.Checked
    export_pdf = chk_export_pdf.Checked

    if not export_dwg and not export_pdf:
        MessageBox.Show("กรุณาเลือกฟอร์แมตที่จะ Export อย่างน้อย 1 แบบ (DWG หรือ PDF) ครับ", "แจ้งเตือน")
        return
    if len(checked_indices) == 0:
        MessageBox.Show("กรุณาเลือก Sheet อย่างน้อย 1 แผ่นครับ", "แจ้งเตือน")
        return
    if dgv.Rows.Count == 0:
        MessageBox.Show("กรุณาเพิ่ม parameter อย่างน้อย 1 รายการครับ", "แจ้งเตือน")
        return

    rows = []
    for i in range(dgv.Rows.Count):
        rows.append({
            "name": dgv.Rows[i].Cells[0].Value,
            "prefix": dgv.Rows[i].Cells[1].Value or "",
            "suffix": dgv.Rows[i].Cells[3].Value or "",
            "separator": dgv.Rows[i].Cells[4].Value or ""
        })

    # --- เช็คชื่อไฟล์ซ้ำก่อน export จริง (ใช้ชื่อ base เดียวกันทั้ง DWG/PDF) ---
    filename_map = {}
    for idx in checked_indices:
        sheet_obj = all_sheets[idx]
        fname = build_filename(sheet_obj, rows)
        filename_map.setdefault(fname, []).append(sheet_obj.SheetNumber)

    dup_names = dict((k, v) for k, v in filename_map.items() if len(v) > 1)
    if dup_names:
        msg_lines = ["พบชื่อไฟล์ซ้ำกัน {0} กลุ่ม (ไฟล์จะถูกเขียนทับกันถ้า export ต่อ):".format(len(dup_names)), ""]
        shown = 0
        for fname, sheet_numbers in dup_names.items():
            msg_lines.append('"{0}" <- {1}'.format(fname, ", ".join(sheet_numbers)))
            shown += 1
            if shown >= 10:
                break
        if len(dup_names) > 10:
            msg_lines.append("... และอีก {0} กลุ่ม".format(len(dup_names) - 10))
        msg_lines.append("")
        msg_lines.append("ต้องการ export ต่อหรือไม่?")
        result = MessageBox.Show("\n".join(msg_lines), "ชื่อไฟล์ซ้ำกัน", MessageBoxButtons.YesNo, MessageBoxIcon.Warning)
        if result != DialogResult.Yes:
            return

    output_folder = txt_folder.Text
    setup_name = cb_setup.SelectedItem
    sheet_set_name = str(cb_sheetset.SelectedItem) if cb_sheetset.SelectedItem else MANUAL_SELECTION_LABEL

    save_last_config({
        "setup": setup_name,
        "output_folder": output_folder,
        "rows": rows,
        "sheet_set": sheet_set_name,
        "export_dwg": export_dwg,
        "export_pdf": export_pdf
    })

    # --- เตรียม export options ---
    dwg_options = None
    if export_dwg:
        dwg_options = DWGExportOptions()
        for s in FilteredElementCollector(doc).OfClass(ExportDWGSettings):
            if s.Name == setup_name:
                dwg_options = s.GetDWGExportOptions()
                break

    pdf_options = None
    if export_pdf:
        pdf_options = PDFExportOptions()
        pdf_options.Combine = True  # export ทีละชีทโดยกำหนดชื่อไฟล์เองผ่าน FileName
        try:
            pdf_options.PaperFormat = ExportPaperFormat.Default  # ใช้ขนาดกระดาษตาม title block ของแต่ละชีท
        except:
            pass

    btn_export.Enabled = False
    btn_cancel.Enabled = False
    lbl_progress.Visible = True
    progress_bar.Visible = True

    num_formats = (1 if export_dwg else 0) + (1 if export_pdf else 0)
    total = len(checked_indices)
    total_ops = total * num_formats
    progress_bar.Minimum = 0
    progress_bar.Maximum = max(total_ops, 1)
    progress_bar.Value = 0

    tx = Transaction(doc, "Dynamo Custom UI Export Complete")
    tx.Start()

    success_count = 0
    op_count = 0
    for n, idx in enumerate(checked_indices):
        sheet_obj = all_sheets[idx]
        custom_filename = build_filename(sheet_obj, rows)

        if export_dwg:
            lbl_progress.Text = "Exporting DWG {0}/{1}: {2}".format(n + 1, total, custom_filename)
            WinFormsApp.DoEvents()
            view_ids = System.Collections.Generic.List[ElementId]()
            view_ids.Add(sheet_obj.Id)
            doc.Export(output_folder, custom_filename, view_ids, dwg_options)
            op_count += 1
            progress_bar.Value = op_count
            WinFormsApp.DoEvents()

        if export_pdf:
            lbl_progress.Text = "Exporting PDF {0}/{1}: {2}".format(n + 1, total, custom_filename)
            WinFormsApp.DoEvents()
            pdf_options.FileName = custom_filename
            pdf_view_ids = System.Collections.Generic.List[ElementId]()
            pdf_view_ids.Add(sheet_obj.Id)
            doc.Export(output_folder, pdf_view_ids, pdf_options)
            op_count += 1
            progress_bar.Value = op_count
            WinFormsApp.DoEvents()

        success_count += 1

    tx.Commit()

    lbl_progress.Text = "Done: {0}/{1} sheets exported".format(success_count, total)
    WinFormsApp.DoEvents()

    formats_label = ", ".join([f for f, enabled in [("DWG", export_dwg), ("PDF", export_pdf)] if enabled])
    TaskDialog.Show("สำเร็จ", "Export {0} สำเร็จเรียบร้อยแล้วทั้งหมด {1} ชีท ลงที่ {2} ครับคุณชาย!".format(formats_label, success_count, output_folder))
    win.Close()

btn_export = new_ctrl(Button, Text="Export", Location=Point(840, 535), Size=Size(100, 35), BackColor=Color.LightBlue)
btn_export.Click += export_clicked

btn_cancel = new_ctrl(Button, Text="Cancel", Location=Point(960, 535), Size=Size(100, 35))
btn_cancel.Click += lambda s, e: win.Close()

# ประกอบชิ้นส่วนลง Form หลัก
win.Controls.AddRange(System.Array[Control]([
    lbl_setup, cb_setup, lbl_folder, txt_folder, btn_browse,
    lbl_sheetset, cb_sheetset, chk_export_dwg, chk_export_pdf, lbl_sheetset_hint,
    lbl_sheets, btn_check_all, btn_uncheck_all, lbl_selected_count, chk_hide_unchecked,
    txt_search, btn_search, chk_list_sheets, lbl_progress, progress_bar,
    lbl_naming, dgv,
    lbl_add, cb_add_param, btn_add_param, btn_remove_row,
    btn_up, btn_down,
    lbl_preview_title, lbl_preview, btn_export, btn_cancel
]))

win.ShowDialog()