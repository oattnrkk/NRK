#! python2
# -*- coding: utf-8 -*-
"""Sheet Manager & Title Block Parameter Filter Matcher

แปลงจาก Dynamo Python Script (sheet_manager_by_nrk.dyn) มาเป็น pyRevit pushbutton
- ใช้ WPF UI เดิมทุกจุด (XAML เดิมไม่ได้แก้ไข)
- แทนที่ RevitServices.Persistence/Transactions (ของ Dynamo) ด้วย pyrevit.revit
"""
__title__ = "Sheet\nManager"
__author__ = "nrk"
__doc__ = "จัดการ Sheet: Duplicate / ลบคำว่า -Copy / บันทึกชื่อ-เลข / ดูด Title Block Properties / เปลี่ยนกรอบ / สร้าง Sheet ใหม่"

import clr
import re

clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
clr.AddReference("RevitAPI")
clr.AddReference("System.Data")

import System
from System import Windows
from System.Windows import Window, Application, Controls, Markup
from System.Windows.Media import VisualTreeHelper
from System.Data import DataTable
import Autodesk
from Autodesk.Revit.DB import *

from pyrevit import revit

doc = revit.doc
uidoc = revit.uidoc

# --- XAML UI Layout (ถอด GroupBox คัดลอก Text Note ออกแล้ว) ---
xaml_str = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Sheet Manager &amp; Title Block Parameter Filter Matcher" Height="820" Width="1080" 
        WindowStartupLocation="CenterScreen" ResizeMode="NoResize"
        Background="#F5F6F8" FontFamily="Segoe UI" FontSize="13">
    <Grid Margin="20">
        <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/> 
            <RowDefinition Height="*"/>    
            <RowDefinition Height="Auto"/> 
            <RowDefinition Height="Auto"/> 
        </Grid.RowDefinitions>

        <StackPanel Grid.Row="0" Margin="0,0,0,12">
            <TextBlock Text="🔍 ค้นหาด่วนในตาราง (พิมพ์เลขหรือชื่อ Sheet):" Margin="0,0,0,5" FontWeight="SemiBold" Foreground="#333333"/>
            <TextBox x:Name="TxtSearch" Height="30" VerticalContentAlignment="Center" Padding="5,0"/>
        </StackPanel>

        <Grid Grid.Row="1" Margin="0,0,0,15">
            <DataGrid x:Name="GridSheets" AutoGenerateColumns="False" Background="White" 
                      BorderBrush="#CCCCCC" BorderThickness="1" CanUserAddRows="False"
                      RowHeight="30" VerticalGridLinesBrush="#E0E0E0" HorizontalGridLinesBrush="#E0E0E0"
                      SelectionMode="Extended" SelectionUnit="FullRow">
                <DataGrid.Columns>
                    <DataGridCheckBoxColumn Header=" เลือก (Apply) " Binding="{Binding Selected, Mode=TwoWay}" Width="130"/>
                    <DataGridTextColumn Header=" Sheet Number " Binding="{Binding SheetNumber, Mode=TwoWay}" Width="220" FontWeight="SemiBold"/>
                    <DataGridTextColumn Header=" Sheet Name " Binding="{Binding SheetName, Mode=TwoWay}" Width="*"/>
                </DataGrid.Columns>
            </DataGrid>
        </Grid>

        <Grid Grid.Row="2" Margin="0,0,0,15">
            <Grid.ColumnDefinitions>
                <ColumnDefinition Width="1.2*"/>
                <ColumnDefinition Width="1.5*"/>
                <ColumnDefinition Width="1.3*"/>
            </Grid.ColumnDefinitions>
            <Button x:Name="BtnDuplicate" Grid.Column="0" Content="✨ Duplicate ที่ติ๊กถูก" 
                    Height="38" Background="#4A90E2" Foreground="White" BorderThickness="0" FontWeight="Bold" Margin="0,0,8,0"/>
            <Button x:Name="BtnCleanCopy" Grid.Column="1" Content="✂️ ลบคำว่า '-Copy' ในชีทที่ติ๊กถูก" 
                    Height="38" Background="#E06666" Foreground="White" BorderThickness="0" FontWeight="Bold" Margin="8,0,8,0"/>
            <Button x:Name="BtnSaveEdits" Grid.Column="2" Content="💾 บันทึกการเปลี่ยนชื่อลง Revit" 
                    Height="38" Background="#28A745" Foreground="White" BorderThickness="0" FontWeight="Bold" Margin="8,0,0,0"/>
        </Grid>

        <Grid Grid.Row="3">
            <Grid.ColumnDefinitions>
                <ColumnDefinition Width="58*"/>
                <ColumnDefinition Width="42*"/>
            </Grid.ColumnDefinitions>
            
            <GroupBox Grid.Column="0" Header=" 🔬 ระบบดูดและเลือกหมวดหมู่คัดลอก Title Block Properties " Background="White" Padding="12" Margin="0,0,10,0">
                <Grid>
                    <Grid.RowDefinitions>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="*"/>
                    </Grid.RowDefinitions>
                    
                    <Grid Grid.Row="0" Margin="0,0,0,12">
                        <Grid.ColumnDefinitions>
                            <ColumnDefinition Width="140"/>
                            <ColumnDefinition Width="*"/>
                        </Grid.ColumnDefinitions>
                        <TextBlock Grid.Column="0" Text="เลข Sheet ต้นแบบ:" VerticalAlignment="Center" FontWeight="SemiBold"/>
                        <TextBox x:Name="TxtSourceSheetNum" Grid.Column="1" Height="28" VerticalContentAlignment="Center" Padding="5,0"/>
                    </Grid>
                    
                    <StackPanel Grid.Row="1" Margin="0,0,0,12">
                        <TextBlock Text="🎯 เลือกหมวดหมู่พารามิเตอร์ที่ต้องการดูดไปวาง:" FontWeight="SemiBold" Foreground="#555555" Margin="0,0,0,6"/>
                        <CheckBox x:Name="ChkIdentity" Content="ข้อมูลผู้ออกแบบ/ตรวจแบบ (Checked By, Approved By, Designed, Drawn)" IsChecked="True" Margin="5,3,0,3"/>
                        <CheckBox x:Name="ChkDates" Content="ข้อมูลวันที่และสถานะชีท (Sheet Issue Date, Project Status)" IsChecked="True" Margin="5,3,0,3"/>
                        <CheckBox x:Name="ChkCustom" Content="พารามิเตอร์อื่นๆ ที่สร้างเองเพิ่มเติม (Custom / Shared Parameters)" IsChecked="True" Margin="5,3,0,3"/>
                        <CheckBox x:Name="ChkSheetMeta" Content="คัดลอกชื่อและเลข Sheet ให้เหมือนต้นแบบด้วย (Sheet Name / Number)" IsChecked="False" Margin="5,3,0,3" Foreground="#E06666"/>
                    </StackPanel>
                    
                    <Button x:Name="BtnMatchTBProps" Grid.Row="2" Content="⚡ ดูดข้อมูลเฉพาะหมวดที่เลือก -> พ่นใส่ Sheet ที่ติ๊กถูก" 
                            Height="38" Background="#007ACC" Foreground="White" BorderThickness="0" FontWeight="Bold" Cursor="Hand"/>
                </Grid>
            </GroupBox>

            <GroupBox Grid.Column="1" Header=" 🖼️ เปลี่ยนกรอบ Title Block / สร้างชีทใหม่ " Background="White" Padding="10" Margin="10,0,0,0">
                <TabControl BorderThickness="0" Background="Transparent">
                    <TabItem Header="เปลี่ยนประเภทกรอบ">
                        <Grid Margin="0,10,0,0">
                            <Grid.ColumnDefinitions>
                                <ColumnDefinition Width="*"/>
                                <ColumnDefinition Width="130"/>
                            </Grid.ColumnDefinitions>
                            <ComboBox x:Name="ComboTitleBlocks" Grid.Column="0" Height="28" VerticalContentAlignment="Center" Margin="0,0,8,0"/>
                            <Button x:Name="BtnChangeTitleBlock" Grid.Column="1" Content="เปลี่ยนประเภทกรอบ" 
                                    Height="28" Background="#FFC107" Foreground="#212529" BorderThickness="0" FontWeight="Bold"/>
                        </Grid>
                    </TabItem>
                    <TabItem Header="สร้าง Sheet ใหม่">
                        <Grid Margin="0,10,0,0">
                            <Grid.ColumnDefinitions>
                                <ColumnDefinition Width="60"/>
                                <ColumnDefinition Width="*"/>
                                <ColumnDefinition Width="80"/>
                            </Grid.ColumnDefinitions>
                            <TextBox x:Name="TxtNewNum" Grid.Column="0" Height="28" VerticalContentAlignment="Center" Margin="0,0,5,0"/>
                            <TextBox x:Name="TxtNewName" Grid.Column="1" Height="28" VerticalContentAlignment="Center" Margin="0,0,8,0"/>
                            <Button x:Name="BtnCreate" Grid.Column="2" Content="สร้างชีท" 
                                    Height="28" Background="#6C757D" Foreground="White" BorderThickness="0" FontWeight="Bold"/>
                        </Grid>
                    </TabItem>
                </TabControl>
            </GroupBox>
        </Grid>
    </Grid>
</Window>
"""

class SheetManagerGrid(object):
    def __init__(self):
        self.win = Markup.XamlReader.Parse(xaml_str)
        
        self.txt_search = self.win.FindName("TxtSearch")
        self.grid_sheets = self.win.FindName("GridSheets")
        self.btn_duplicate = self.win.FindName("BtnDuplicate")
        self.btn_clean_copy = self.win.FindName("BtnCleanCopy")
        self.btn_save_edits = self.win.FindName("BtnSaveEdits")
        self.txt_new_num = self.win.FindName("TxtNewNum")
        self.txt_new_name = self.win.FindName("TxtNewName")
        self.btn_create = self.win.FindName("BtnCreate")
        self.combo_title_blocks = self.win.FindName("ComboTitleBlocks")
        self.btn_change_title_block = self.win.FindName("BtnChangeTitleBlock")
        
        self.txt_source_sheet_num = self.win.FindName("TxtSourceSheetNum")
        self.btn_match_tb_props = self.win.FindName("BtnMatchTBProps")
        self.chk_identity = self.win.FindName("ChkIdentity")
        self.chk_dates = self.win.FindName("ChkDates")
        self.chk_custom = self.win.FindName("ChkCustom")
        self.chk_sheet_meta = self.win.FindName("ChkSheetMeta")
        
        self.txt_search.TextChanged += self.on_search_changed
        self.btn_duplicate.Click += self.on_duplicate_click
        self.btn_clean_copy.Click += self.on_clean_copy_click
        self.btn_save_edits.Click += self.on_save_edits_click
        self.btn_create.Click += self.on_create_click
        self.btn_change_title_block.Click += self.on_change_title_block_click
        self.btn_match_tb_props.Click += self.on_match_tb_props_click
        self.grid_sheets.PreviewMouseLeftButtonDown += self.on_grid_preview_left_click
        
        self.dt = DataTable()
        self.dt.Columns.Add("Selected", System.Boolean)
        self.dt.Columns.Add("SheetNumber", System.String)
        self.dt.Columns.Add("SheetName", System.String)
        self.dt.Columns.Add("ElementId", System.Object) 
        
        self.refresh_sheet_data()
        self.load_available_title_blocks()

    def refresh_sheet_data(self):
        sheets_elements = FilteredElementCollector(doc).OfClass(ViewSheet).ToElements()
        self.all_sheets = sorted(sheets_elements, key=lambda s: s.SheetNumber)
        self.dt.Rows.Clear()
        for s in self.all_sheets:
            elem_id = s.Id.Value if hasattr(s.Id, "Value") else s.Id.IntegerValue
            self.dt.Rows.Add(False, s.SheetNumber, s.Name, elem_id)
        self.grid_sheets.ItemsSource = self.dt.DefaultView

    def load_available_title_blocks(self):
        self.combo_title_blocks.Items.Clear()
        self.tblock_ids = []
        tblock_types = FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_TitleBlocks).WhereElementIsElementType().ToElements()
        tblock_list = []
        for tb in tblock_types:
            f_name = ""
            p_fam = tb.get_Parameter(BuiltInParameter.ALL_MODEL_FAMILY_NAME)
            if p_fam and p_fam.HasValue: f_name = p_fam.AsString()
            if not f_name:
                try: f_name = tb.FamilyName
                except: f_name = "TitleBlock"
            
            t_name = ""
            p_type = tb.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM)
            if p_type and p_type.HasValue: t_name = p_type.AsString()
            if not t_name:
                try: t_name = tb.Name
                except: t_name = "Default Type"
                
            display_name = "{} : {}".format(f_name, t_name)
            tblock_list.append((display_name, tb.Id))
            
        sorted_tblocks = sorted(tblock_list, key=lambda x: x[0])
        for display_name, tb_id in sorted_tblocks:
            self.combo_title_blocks.Items.Add(display_name)
            self.tblock_ids.append(tb_id)
        if self.combo_title_blocks.Items.Count > 0:
            self.combo_title_blocks.SelectedIndex = 0

    def on_grid_preview_left_click(self, sender, event):
        dep = event.OriginalSource
        while dep is not None and not isinstance(dep, Controls.DataGridCell):
            dep = VisualTreeHelper.GetParent(dep)
        if dep is not None:
            cell = dep
            if cell.Column.DisplayIndex == 0:
                row_view = cell.DataContext
                if row_view:
                    current_status = row_view.Row["Selected"]
                    target_status = not current_status
                    selected_items = list(self.grid_sheets.SelectedItems)
                    is_clicked_row_selected = any(item.Row == row_view.Row for item in selected_items)
                    
                    if len(selected_items) > 1 and is_clicked_row_selected:
                        for item in selected_items:
                            item.Row["Selected"] = target_status
                    else:
                        row_view.Row["Selected"] = target_status
                    
                    self.grid_sheets.Items.Refresh()
                    event.Handled = True

    def on_search_changed(self, sender, event):
        search_text = self.txt_search.Text.replace("'", "''").strip()
        if search_text:
            self.dt.DefaultView.RowFilter = "SheetNumber LIKE '%{}%' OR SheetName LIKE '%{}%'".format(search_text, search_text)
        else:
            self.dt.DefaultView.RowFilter = ""

    def on_clean_copy_click(self, sender, event):
        self.grid_sheets.CommitEdit(Controls.DataGridEditingUnit.Cell, True)
        self.grid_sheets.CommitEdit(Controls.DataGridEditingUnit.Row, True)
        cleaned_count = 0
        pattern = re.compile(r"\s*-\s*copy", re.IGNORECASE)
        for row in self.dt.Rows:
            if bool(row["Selected"]):
                old_num, old_name = str(row["SheetNumber"]), str(row["SheetName"])
                new_num, new_name = pattern.sub("", old_num).strip(), pattern.sub("", old_name).strip()
                if old_num != new_num or old_name != new_name:
                    row["SheetNumber"], row["SheetName"] = new_num, new_name
                    cleaned_count += 1
        if cleaned_count > 0:
            self.grid_sheets.Items.Refresh()
            Windows.MessageBox.Show("ปรับแก้คำว่า '-Copy' บนตารางสำเร็จ {} รายการครับคุณชาย\n⚠️ อย่าลืมกดบันทึกข้อมูลลง Revit นะครับ".format(cleaned_count), "สำเร็จ")
        else:
            Windows.MessageBox.Show("ไม่พบคำว่า '-Copy' ในรายการที่เลือกครับ", "แจ้งเตือน")

    def on_save_edits_click(self, sender, event):
        self.grid_sheets.CommitEdit(Controls.DataGridEditingUnit.Cell, True)
        self.grid_sheets.CommitEdit(Controls.DataGridEditingUnit.Row, True)
        success_count, error_count = 0, 0
        with revit.Transaction("Save Sheet Number/Name Edits"):
            for row in self.dt.Rows:
                elem_id = ElementId(System.Int64(int(row["ElementId"])))
                sheet = doc.GetElement(elem_id)
                if sheet:
                    new_num, new_name = str(row["SheetNumber"]).strip(), str(row["SheetName"]).strip()
                    if sheet.SheetNumber != new_num or sheet.Name != new_name:
                        try:
                            sheet.SheetNumber = new_num
                            sheet.Name = new_name
                            success_count += 1
                        except:
                            error_count += 1
        self.refresh_sheet_data()
        if error_count > 0:
            Windows.MessageBox.Show("บันทึกสำเร็จ {} รายการ\n❌ ล้มเหลว {} รายการ (เลขชีทอาจซ้ำครับคุณชาย)".format(success_count, error_count), "แจ้งเตือน")
        else:
            Windows.MessageBox.Show("บันทึกข้อมูลลง Revit เรียบร้อยครับ!", "สำเร็จ")

    def should_copy_parameter(self, param_name):
        name_lower = param_name.lower()
        identity_keywords = ["checked by", "approved by", "designed by", "drawn by", "author", "checker"]
        if any(k in name_lower for k in identity_keywords):
            return bool(self.chk_identity.IsChecked)
        date_keywords = ["date", "status", "issue"]
        if any(k in name_lower for k in date_keywords):
            return bool(self.chk_dates.IsChecked)
        sheet_meta_keywords = ["sheet number", "sheet name"]
        if any(k in name_lower for k in sheet_meta_keywords):
            return bool(self.chk_sheet_meta.IsChecked)
        return bool(self.chk_custom.IsChecked)

    def on_match_tb_props_click(self, sender, event):
        self.grid_sheets.CommitEdit(Controls.DataGridEditingUnit.Cell, True)
        self.grid_sheets.CommitEdit(Controls.DataGridEditingUnit.Row, True)
        src_num = self.txt_source_sheet_num.Text.strip()
        if not src_num:
            Windows.MessageBox.Show("กรุณากรอกเลข Sheet ต้นแบบที่ต้องการดูดข้อมูลก่อนครับคุณชาย", "แจ้งเตือน")
            return
        checked_sheet_ids = [int(row["ElementId"]) for row in self.dt.Rows if bool(row["Selected"])]
        if not checked_sheet_ids:
            Windows.MessageBox.Show("กรุณาติ๊กเลือก Sheet ปลายทางในตารางก่อนครับคุณชาย", "แจ้งเตือน")
            return
        src_sheet = None
        for s in self.all_sheets:
            if s.SheetNumber.strip() == src_num:
                src_sheet = s
                break
        if not src_sheet:
            Windows.MessageBox.Show("ไม่พบ Sheet เลขที่ [{}] ในโปรเจกต์นี้ครับ".format(src_num), "แจ้งเตือน")
            return
            
        all_tblocks = FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_TitleBlocks).WhereElementIsNotElementType().ToElements()
        src_tblocks = [tb for tb in all_tblocks if tb.OwnerViewId == src_sheet.Id]
        if not src_tblocks:
            Windows.MessageBox.Show("ไม่พบแผ่น Title Block วางอยู่ใน Sheet ต้นแบบครับคุณชาย", "แจ้งเตือน")
            return
        src_tb = src_tblocks[0]
        
        updated_sheets_count = 0
        updated_params_count = 0
        
        try:
            with revit.Transaction("Match Title Block Properties"):
                for target_id in checked_sheet_ids:
                    tgt_sheet = doc.GetElement(ElementId(System.Int64(target_id)))
                    if tgt_sheet and tgt_sheet.Id != src_sheet.Id:
                        tgt_tblocks = [tb for tb in all_tblocks if tb.OwnerViewId == tgt_sheet.Id]
                        for tgt_tb in tgt_tblocks:
                            for src_param in src_tb.Parameters:
                                if src_param.HasValue and not src_param.IsReadOnly:
                                    p_name = src_param.Definition.Name
                                    if p_name in ["Scale"]: continue
                                    if self.should_copy_parameter(p_name):
                                        tgt_param = tgt_tb.LookupParameter(p_name)
                                        if tgt_param and not tgt_param.IsReadOnly:
                                            if src_param.StorageType == StorageType.String:
                                                tgt_param.Set(src_param.AsString())
                                            elif src_param.StorageType == StorageType.Integer:
                                                tgt_param.Set(src_param.AsInteger())
                                            elif src_param.StorageType == StorageType.Double:
                                                tgt_param.Set(src_param.AsDouble())
                                            elif src_param.StorageType == StorageType.ElementId:
                                                tgt_param.Set(src_param.AsElementId())
                                            updated_params_count += 1
                        updated_sheets_count += 1
            Windows.MessageBox.Show("คัดลอก Properties เฉพาะหมวดที่เลือกสำเร็จ!\n🎯 อัปเดตชีทปลายทางรวม: {} ชีท\n📝 จำนวนค่าพารามิเตอร์ที่เปลี่ยน: {} จุด".format(updated_sheets_count, updated_params_count), "สำเร็จ")
        except Exception as ex:
            Windows.MessageBox.Show("เกิดข้อผิดพลาด: \n" + str(ex), "Error")

    def on_change_title_block_click(self, sender, event):
        self.grid_sheets.CommitEdit(Controls.DataGridEditingUnit.Cell, True)
        self.grid_sheets.CommitEdit(Controls.DataGridEditingUnit.Row, True)
        sel_index = self.combo_title_blocks.SelectedIndex
        if sel_index < 0: return
        new_tb_id = self.tblock_ids[sel_index]
        checked_sheets_ids = [int(row["ElementId"]) for row in self.dt.Rows if bool(row["Selected"])]
        if not checked_sheets_ids: return
        success_count = 0
        all_tblocks = FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_TitleBlocks).WhereElementIsNotElementType().ToElements()
        with revit.Transaction("Change Title Block Type"):
            for e_id in checked_sheets_ids:
                sheet = doc.GetElement(ElementId(System.Int64(e_id)))
                if sheet:
                    try:
                        tblocks = [tb for tb in all_tblocks if tb.OwnerViewId == sheet.Id]
                        if tblocks:
                            for tb in tblocks:
                                if tb.Pinned: tb.Pinned = False
                                p_type = tb.get_Parameter(BuiltInParameter.ELEM_TYPE_PARAM)
                                if p_type: p_type.Set(new_tb_id)
                            success_count += 1
                    except: pass
        self.refresh_sheet_data()
        Windows.MessageBox.Show("เปลี่ยนกรอบ Title Block สำเร็จ {} รายการ".format(success_count), "สำเร็จ")

    def copy_sheet_parameters(self, source, target):
        for param in source.Parameters:
            if not param.IsReadOnly and param.HasValue:
                if param.Definition.BuiltInParameter in [BuiltInParameter.SHEET_NUMBER, BuiltInParameter.SHEET_NAME]:
                    continue
                target_param = target.get_Parameter(param.Definition)
                if target_param and not target_param.IsReadOnly:
                    if param.StorageType == StorageType.String: target_param.Set(param.AsString())
                    elif param.StorageType == StorageType.Integer: target_param.Set(param.AsInteger())
                    elif param.StorageType == StorageType.Double: target_param.Set(param.AsDouble())
                    elif param.StorageType == StorageType.ElementId: target_param.Set(param.AsElementId())

    def on_duplicate_click(self, sender, event):
        self.grid_sheets.CommitEdit(Controls.DataGridEditingUnit.Cell, True)
        self.grid_sheets.CommitEdit(Controls.DataGridEditingUnit.Row, True)
        checked_sheets_ids = [int(row["ElementId"]) for row in self.dt.Rows if bool(row["Selected"])]
        if not checked_sheets_ids: return
        success_count = 0
        with revit.Transaction("Duplicate Sheets"):
            for e_id in checked_sheets_ids:
                source_sheet = doc.GetElement(ElementId(System.Int64(e_id)))
                if source_sheet:
                    try:
                        col = FilteredElementCollector(doc, source_sheet.Id).OfCategory(BuiltInCategory.OST_TitleBlocks).ToElements()
                        tb_id = col[0].GetTypeId() if col else FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_TitleBlocks).WhereElementIsElementType().ToElements()[0].Id
                        new_sheet = ViewSheet.Create(doc, tb_id)
                        self.copy_sheet_parameters(source_sheet, new_sheet)
                        new_sheet.SheetNumber = source_sheet.SheetNumber + "-Copy"
                        new_sheet.Name = source_sheet.Name + " - Copy"
                        success_count += 1
                    except: pass
        self.refresh_sheet_data()
        Windows.MessageBox.Show("Duplicate Sheet สำเร็จ {} รายการครับคุณชาย".format(success_count), "สำเร็จ")

    def on_create_click(self, sender, event):
        num = self.txt_new_num.Text.strip()
        name = self.txt_new_name.Text.strip()
        if not num or not name: return
        tblocks = FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_TitleBlocks).WhereElementIsElementType().ToElements()
        if not tblocks: return
        try:
            with revit.Transaction("Create New Sheet"):
                new_sheet = ViewSheet.Create(doc, tblocks[0].Id)
                new_sheet.SheetNumber = num
                new_sheet.Name = name
            self.txt_new_num.Text, self.txt_new_name.Text = "", ""
            self.refresh_sheet_data()
            Windows.MessageBox.Show("สร้าง Sheet ใหม่เรียบร้อยครับ", "สำเร็จ")
        except:
            pass

    def show(self):
        self.win.ShowDialog()

manager = SheetManagerGrid()
manager.show()