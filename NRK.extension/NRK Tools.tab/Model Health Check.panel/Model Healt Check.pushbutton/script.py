#! python2
# -*- coding: utf-8 -*-
"""Model Health Check
Reads a BIM Interoperability Tools (Model Checker) check set XML as its
settings and runs every check in Revit: P0 / P1 / P2 / MONITORING.
Put the XML in the same folder as this script, or browse to it."""
__title__ = 'Model Health\nCheck'
__author__ = 'NRK'
__persistentengine__ = True

import clr
import os
import re
import time
import xml.etree.ElementTree as ET

clr.AddReference('System.Windows.Forms')
clr.AddReference('System.Drawing')
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')

import System
from System.Collections.Generic import List, HashSet
import System.Windows.Forms as WF
import System.Drawing as SD
import Autodesk.Revit.DB as DB
import Autodesk.Revit.UI as UI
import System.Diagnostics

# These three are refreshed every time a queued job runs (see _Handler.Execute),
# so the tool keeps working if the user switches to another model.
uiapp = __revit__
uidoc = uiapp.ActiveUIDocument
doc = uidoc.Document if uidoc is not None else None
app = uiapp.Application

def _make_guard():
    """pyRevit can wipe this script's global names once the script ends, but our
    modeless window keeps calling functions afterwards (NameError). The guard keeps
    a snapshot in closure cells (which survive) and puts back any missing names
    before each window/event method runs."""
    G = globals()
    state = {'bk': None}

    def guard(fn):
        def wrapper(*a, **k):
            bk = state['bk']
            if bk is not None:
                for target in (G, getattr(fn, 'func_globals', None)):
                    if target is None:
                        continue
                    for key in bk:
                        if key not in target:
                            target[key] = bk[key]
            return fn(*a, **k)
        wrapper.__name__ = getattr(fn, '__name__', 'wrapper')
        return wrapper

    def snapshot():
        state['bk'] = dict(G)

    return guard, snapshot


_guard, _snapshot = _make_guard()


MAX_ROWS = 5000
FT_TO_MM = 304.8

SETTINGS = {}      # last-used settings, loaded from / saved to a json file (see load_settings)
SETTINGS_FILE = os.path.join(os.environ.get('APPDATA', ''), 'pyRevit', 'NRK_ModelHealthCheck_settings.json')


def data_dir():
    """Folder for the fix log and the score history."""
    d = SETTINGS.get('data_dir') or os.path.join(os.path.expanduser('~'), 'Documents', 'NRK Model Health Check')
    try:
        if not os.path.exists(d):
            os.makedirs(d)
    except Exception:
        pass
    return d


def file_safe(name):
    return re.sub(r'[^\w\-]+', '_', os.path.splitext(name or 'model')[0])


def parse_csv_line(line):
    out = []
    cur = []
    q = False
    i = 0
    while i < len(line):
        c = line[i]
        if q:
            if c == '"':
                if i + 1 < len(line) and line[i + 1] == '"':
                    cur.append('"')
                    i += 1
                else:
                    q = False
            else:
                cur.append(c)
        elif c == '"':
            q = True
        elif c == ',':
            out.append(''.join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append(''.join(cur))
    return out


FIX_CHANGES = []       # (action, ElementId, description, detail) - filled by the fixers, written to the fix log


def append_text(path, text):
    System.IO.File.AppendAllText(path, text, System.Text.UTF8Encoding(True))


def write_fix_log(model, check_name, fixer_label, summary, changes, failures):
    """One CSV per model; every fix appends rows (what changed, what was skipped and why)."""
    try:
        path = os.path.join(data_dir(), 'FixLog_%s.csv' % file_safe(model))
        stamp = time.strftime('%Y-%m-%d %H:%M:%S')
        user = System.Environment.UserName
        rows = []
        if not os.path.exists(path):
            rows.append(('Timestamp', 'User', 'Model', 'Check', 'Fix', 'Result', 'Action', 'ElementId', 'Element', 'Detail', 'Reason'))
        base = (stamp, user, model or '', check_name, fixer_label)
        rows.append(base + ('SUMMARY', '', '', '', summary.replace('\r\n', ' | '), ''))
        for action, eid, desc, detail in changes:
            rows.append(base + ('OK', action, eid_val(eid) if eid is not None else '', desc, detail, ''))
        for eid, desc, reason in failures:
            rows.append(base + ('FAILED', '', eid_val(eid) if eid is not None else '', desc, '', reason))
        append_text(path, '\r\n'.join(','.join(csv_cell(x) for x in r) for r in rows) + '\r\n')
        return path
    except Exception:
        return None


def history_path():
    return os.path.join(data_dir(), 'ScoreHistory.csv')


def append_history(model, t, p):
    try:
        path = history_path()
        rows = []
        if not os.path.exists(path):
            rows.append(('Timestamp', 'Model', 'Score', 'Pass', 'Fail', 'CountList', 'NotRun'))
        rows.append((time.strftime('%Y-%m-%d %H:%M:%S'), model or '', p, t['pass'], t['fail'], t['cl'], t['notrun']))
        append_text(path, '\r\n'.join(','.join(csv_cell(x) for x in r) for r in rows) + '\r\n')
    except Exception:
        pass


def read_history(model):
    """[(timestamp, score, pass, fail, cl, notrun)] for one model, oldest first."""
    out = []
    try:
        path = history_path()
        if os.path.exists(path):
            for line in System.IO.File.ReadAllText(path).splitlines():
                f = parse_csv_line(line)
                if len(f) >= 7 and f[1] == (model or '') and f[2].isdigit():
                    out.append((f[0], int(f[2]), int(f[3]), int(f[4]), int(f[5]), int(f[6])))
    except Exception:
        pass
    return out


def ws_name_of(el):
    try:
        return doc.GetWorksetTable().GetWorkset(el.WorksetId).Name
    except Exception:
        return ''


def load_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            import json
            d = json.loads(open(SETTINGS_FILE).read())
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def save_settings(d):
    try:
        import json
        folder = os.path.dirname(SETTINGS_FILE)
        if folder and not os.path.exists(folder):
            os.makedirs(folder)
        open(SETTINGS_FILE, 'w').write(json.dumps(d, indent=2, sort_keys=True))
    except Exception:
        pass


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------
def eid_val(i):
    """ElementId -> integer, works on old and new Revit APIs."""
    try:
        return i.Value
    except AttributeError:
        return i.IntegerValue


def is_valid_id(i):
    try:
        return i is not None and eid_val(i) != -1
    except Exception:
        return False


def safe_name(el):
    try:
        return el.Name or ''
    except Exception:
        return ''


def clr_type(cls):
    return clr.GetClrType(cls)


def truthy(v):
    return str(v).strip().lower() == 'true'


def file_size_mb(path):
    try:
        return os.path.getsize(path) / 1048576.0
    except Exception:
        return None


def model_file_path():
    """Central file path for workshared models, else the saved path."""
    try:
        if doc.IsWorkshared:
            mp = doc.GetWorksharingCentralModelPath()
            if mp is not None:
                p = DB.ModelPathUtils.ConvertModelPathToUserVisiblePath(mp)
                if p:
                    return p
    except Exception:
        pass
    return doc.PathName or ''


# ----------------------------------------------------------------------------
# check-set XML
# ----------------------------------------------------------------------------
class CheckDef(object):
    def __init__(self):
        self.heading = ''
        self.section = ''
        self.name = ''
        self.type = 'Custom'
        self.result_condition = 'FailMatchingElements'
        self.required = True
        self.checked = True
        self.failure_message = ''
        self.description = ''
        self.filters = []       # list of dict
        self.options = {}       # PrebuiltOptions attributes
        self.xml_checked = True  # tick state written in the XML
        self.extra = False       # True = added by this tool, not in the XML (left out of the score)


def parse_checkset(path):
    root = ET.parse(path).getroot()
    checks = []
    for h in root.findall('Heading'):
        ht = (h.get('HeadingText') or '').strip()
        if ht.startswith('<--'):          # "<--Unused-->" headings
            continue
        for s in h.findall('Section'):
            for c in s.findall('Check'):
                d = CheckDef()
                d.heading = ht
                d.section = (s.get('SectionName') or '').strip()
                d.name = c.get('CheckName') or ''
                d.type = c.get('CheckType') or 'Custom'
                d.result_condition = c.get('ResultCondition') or 'FailMatchingElements'
                d.required = truthy(c.get('IsRequired', 'True'))
                d.checked = truthy(c.get('IsChecked', 'True'))
                d.xml_checked = d.checked
                d.failure_message = c.get('FailureMessage') or ''
                d.description = c.get('Description') or ''
                for f in c.findall('Filter'):
                    d.filters.append({
                        'op': f.get('Operator') or 'And',
                        'cat': f.get('Category') or '',
                        'prop': f.get('Property') or '',
                        'cond': f.get('Condition') or '',
                        'val': f.get('Value') or '',
                        'ci': truthy(f.get('CaseInsensitive', 'False')),
                    })
                po = c.find('PrebuiltOptions')
                if po is not None:
                    d.options = dict(po.attrib)
                checks.append(d)
    return checks, root.get('Name') or os.path.basename(path)


def split_groups(filters):
    """First group = include filters. Every 'Exclude' filter starts a new
    exclusion group; the And-filters after it belong to that group."""
    groups = [[]]
    for f in filters:
        if f['op'] == 'Exclude':
            groups.append([f])
        else:
            groups[-1].append(f)
    return groups


# ----------------------------------------------------------------------------
# results
# ----------------------------------------------------------------------------
class Result(object):
    def __init__(self, status='INFO', message='', items=None):
        self.status = status          # PASS FAIL WARN INFO SKIP ERROR
        self.message = message
        self.items = items or []      # tuples: (text, ElementId or None, extra)
        self.groups = None            # optional: [(label, [ElementId], extra)] one row per family

    @property
    def count(self):
        return len(self.items)


def flag_status(chk):
    return 'FAIL' if chk.required else 'WARN'


# ----------------------------------------------------------------------------
# run context (caches)
# ----------------------------------------------------------------------------
class Ctx(object):
    def __init__(self, progress=None, cancelled=None):
        self._instances = None
        self._ws_names = {}
        self.progress = progress or (lambda t, f=None: None)
        self.cancelled = cancelled or (lambda: False)
        self.workshared = doc.IsWorkshared

    def instances(self):
        if self._instances is None:
            self.progress('Collecting model elements...')
            self._instances = list(
                DB.FilteredElementCollector(doc).WhereElementIsNotElementType().ToElements())
        return self._instances

    def workset_name(self, el):
        if not self.workshared:
            return None
        try:
            wid = el.WorksetId
            key = wid.IntegerValue
            if key not in self._ws_names:
                ws = doc.GetWorksetTable().GetWorkset(wid)
                self._ws_names[key] = ws.Name if ws is not None else ''
            return self._ws_names[key]
        except Exception:
            return None


# ----------------------------------------------------------------------------
# Custom (filter based) checks
# ----------------------------------------------------------------------------
def bic_from_name(name):
    return getattr(DB.BuiltInCategory, name, None)


def bip_from_name(name):
    return getattr(DB.BuiltInParameter, name, None)


def param_text(el, name):
    """Text of a parameter (built-in or by name), falling back to the type."""
    targets = [el]
    try:
        tid = el.GetTypeId()
        if is_valid_id(tid):
            t = doc.GetElement(tid)
            if t is not None:
                targets.append(t)
    except Exception:
        pass
    bip = bip_from_name(name)
    for t in targets:
        p = None
        try:
            if bip is not None:
                p = t.get_Parameter(bip)
            if p is None:
                p = t.LookupParameter(name)
        except Exception:
            p = None
        if p is None:
            continue
        try:
            if p.StorageType == DB.StorageType.String:
                v = p.AsString()
            else:
                v = p.AsValueString()
                if v is None and p.HasValue:
                    if p.StorageType == DB.StorageType.Integer:
                        v = str(p.AsInteger())
                    elif p.StorageType == DB.StorageType.Double:
                        v = str(p.AsDouble())
            if v is not None:
                return v
        except Exception:
            continue
    return None


def family_of(el):
    if isinstance(el, DB.FamilyInstance):
        return el.Symbol.Family
    if isinstance(el, DB.FamilySymbol):
        return el.Family
    if isinstance(el, DB.Family):
        return el
    return None


def prop_value(ctx, el, cat, prop):
    if cat == 'TypeOrInstance':
        return str(isinstance(el, DB.ElementType))
    if cat == 'APIType':
        return el.GetType().FullName
    if cat == 'APIParameter':
        return str(getattr(el, prop))
    if cat == 'Parameter':
        if prop == 'Name':
            return safe_name(el)
        return param_text(el, prop)
    if cat == 'Family':
        fam = family_of(el)
        if prop == 'Is In Place':
            return str(fam.IsInPlace) if fam is not None else 'False'
        if fam is not None:
            return fam.Name
        return param_text(el, 'ELEM_FAMILY_PARAM')
    if cat == 'Workset':
        return ctx.workset_name(el)
    if cat == 'Type':
        if isinstance(el, DB.ElementType):
            return safe_name(el)
        t = doc.GetElement(el.GetTypeId())
        return safe_name(t) if t is not None else None
    return None


def compare(actual, cond, val, ci, cat=''):
    if cond == 'HasValue' or cond == 'HasNoValue':
        has = actual is not None and str(actual) != ''
        want = truthy(val)
        return (has == want) if cond == 'HasValue' else (has != want)
    if actual is None:
        if cat == 'Workset':
            return False
        actual = ''
    actual = str(actual)
    flags = re.IGNORECASE if ci else 0
    if cond == 'Equal':
        return actual.lower() == val.lower()
    if cond == 'NotEqual':
        return actual.lower() != val.lower()
    if cond == 'Contains':
        return val.lower() in actual.lower()
    if cond == 'DoesNotContain':
        return val.lower() not in actual.lower()
    if cond in ('WildCard', 'WildCardNoMatch'):
        try:
            m = re.search(val, actual, flags) is not None
        except re.error:
            m = False
        return m if cond == 'WildCard' else (not m)
    raise ValueError('Unsupported condition: ' + cond)


def category_match(el, f):
    b = bic_from_name(f['prop'])
    if b is None or el.Category is None:
        return False
    try:
        hit = el.Category.BuiltInCategory == b
    except Exception:
        hit = False
    return hit if truthy(f['val']) else (not hit)


def eval_group(ctx, el, group):
    cat_f = [f for f in group if f['cat'] == 'Category' and f['cond'] == 'Included']
    others = [f for f in group if not (f['cat'] == 'Category' and f['cond'] == 'Included')]
    if cat_f:
        # several category filters = the element may be in any of them
        if not any(category_match(el, f) for f in cat_f):
            return False
    res = True
    first = True
    for f in others:
        try:
            v = compare(prop_value(ctx, el, f['cat'], f['prop']),
                        f['cond'], f['val'], f['ci'], f['cat'])
        except Exception:
            v = False
        if first:
            res = v
            first = False
        elif f['op'] == 'Or':
            res = res or v
        else:
            res = res and v
    return res


def collect_candidates(inc):
    bics = []
    want_type = None
    class_name = None
    for f in inc:
        if f['cat'] == 'Category' and f['cond'] == 'Included' and truthy(f['val']):
            b = bic_from_name(f['prop'])
            if b is not None:
                bics.append(b)
        elif f['cat'] == 'TypeOrInstance' and f['cond'] == 'Equal':
            want_type = truthy(f['val'])
        elif f['cat'] == 'APIType' and f['cond'] == 'Equal':
            if f['val'].startswith('Autodesk.Revit.DB.'):
                class_name = f['val'].split('.')[-1]
    out = []
    kinds = [want_type] if want_type is not None else [False, True]
    for as_type in kinds:
        col = DB.FilteredElementCollector(doc)
        if bics:
            lst = List[DB.BuiltInCategory]()
            for b in bics:
                lst.Add(b)
            col = col.WherePasses(DB.ElementMulticategoryFilter(lst))
        cls = getattr(DB, class_name, None) if class_name else None
        if cls is not None:
            col = col.OfClass(clr_type(cls))
        col = col.WhereElementIsElementType() if as_type else col.WhereElementIsNotElementType()
        out.extend(list(col.ToElements()))
    return out


def describe(ctx, el):
    try:
        cat = el.Category.Name if el.Category is not None else ''
    except Exception:
        cat = ''
    fam = family_of(el)
    if fam is not None:
        text = '%s | %s : %s' % (cat, fam.Name, safe_name(el))
    else:
        text = '%s | %s' % (cat, safe_name(el))
    extra = []
    ws = ctx.workset_name(el)
    if ws:
        extra.append('Workset: ' + ws)
    try:
        lid = el.LevelId
        if is_valid_id(lid):
            extra.append('Level: ' + safe_name(doc.GetElement(lid)))
    except Exception:
        pass
    try:
        if is_valid_id(el.GroupId):
            g = group_of(el)
            if g is not None:
                extra.append('In group: ' + group_label(g))
    except Exception:
        pass
    return text, ' | '.join(extra)


def _not_system_family(ctx, el):
    """System families (walls, floors, ducts...) have no family file, so their family name cannot be
    renamed. They are left out of the naming check unless the option is switched off."""
    if not SETTINGS.get('skip_system_families', True):
        return True
    return family_of(el) is not None


POST_FILTERS = {'Family Naming Convention': _not_system_family}


def _run_custom_core(ctx, chk):
    groups = split_groups(chk.filters)
    inc, excl = groups[0], groups[1:]
    ctx.progress('Scanning: ' + chk.name)
    cands = collect_candidates(inc)
    total = max(len(cands), 1)
    matches = []
    for n, el in enumerate(cands):
        if n % 1000 == 0:
            if ctx.cancelled():
                return Result('SKIP', 'Stopped before finishing')
            ctx.progress('Scanning: %s  (%d / %d)' % (chk.name, n, len(cands)), float(n) / total)
        try:
            if not eval_group(ctx, el, inc):
                continue
            if any(eval_group(ctx, el, g) for g in excl):
                continue
            post = POST_FILTERS.get(chk.name)
            if post is not None and not post(ctx, el):
                continue
            matches.append(el)
        except Exception:
            continue
    items = []
    gmap = {}
    gorder = []
    for el in matches:
        text, extra = describe(ctx, el)
        items.append((text, el.Id, extra))
        fam = family_of(el)
        if fam is not None:
            try:
                cat = el.Category.Name if el.Category is not None else ''
            except Exception:
                cat = ''
            key = ('f', eid_val(fam.Id))
            label = '%s | %s' % (cat, fam.Name)
        else:
            key = ('e', eid_val(el.Id))
            label = text
        g = gmap.get(key)
        if g is None:
            gmap[key] = [label, [el.Id], extra]
            gorder.append(key)
        else:
            g[1].append(el.Id)
    items.sort(key=lambda x: x[0])
    ctx.last_groups = None
    if len(gmap) < len(items):                 # something to collapse: many instances of one family
        ctx.last_groups = sorted([tuple(gmap[k]) for k in gorder], key=lambda x: x[0])
    rc = chk.result_condition
    if rc == 'CountAndList':
        return Result('INFO', '%d element(s)' % len(items), items)
    if rc == 'FailNoElements':
        if items:
            return Result('PASS', '%d matching element(s) found' % len(items), items)
        return Result(flag_status(chk), chk.failure_message or 'No matching elements found', [])
    # FailMatchingElements
    if items:
        return Result(flag_status(chk), chk.failure_message or '%d element(s) found' % len(items), items)
    return Result('PASS', 'No issues found', [])


def run_custom(ctx, chk):
    ctx.last_groups = None
    res = _run_custom_core(ctx, chk)
    res.groups = ctx.last_groups
    return res


# ----------------------------------------------------------------------------
# prebuilt checks
# ----------------------------------------------------------------------------
def chk_design_options(ctx, chk):
    opts = list(DB.FilteredElementCollector(doc).OfClass(clr_type(DB.DesignOption)).ToElements())
    if not opts:
        return Result('PASS', 'No design options in the model')
    counts = {}
    if chk.options.get('ShowCount', 'False') == 'True':
        for el in ctx.instances():
            try:
                d = el.DesignOption
                if d is not None:
                    counts[d.Name] = counts.get(d.Name, 0) + 1
            except Exception:
                pass
    items = []
    for o in opts:
        n = counts.get(o.Name)
        items.append((o.Name, o.Id, ('%d element(s)' % n) if n is not None else ''))
    return Result('WARN', '%d design option(s) in the model' % len(items), items)


def chk_project_coordinates(ctx, chk):
    items = []
    for bp in DB.FilteredElementCollector(doc).OfClass(clr_type(DB.BasePoint)).ToElements():
        label = 'Survey Point' if bp.IsShared else 'Project Base Point'
        vals = []
        for title, name in (('E/W', 'BASEPOINT_EASTWEST_PARAM'), ('N/S', 'BASEPOINT_NORTHSOUTH_PARAM'),
                            ('Elev', 'BASEPOINT_ELEVATION_PARAM'), ('Angle to True North', 'BASEPOINT_ANGLETON_PARAM')):
            v = param_text(bp, name)
            if v is not None:
                vals.append('%s = %s' % (title, v))
        items.append((label, bp.Id, ' | '.join(vals)))
    try:
        loc = doc.ActiveProjectLocation
        pos = loc.GetProjectPosition(DB.XYZ.Zero)
        items.append(('Active project location', None, '%s | True North angle = %.4f deg'
                      % (loc.Name, pos.Angle * 180.0 / 3.141592653589793)))
    except Exception:
        pass
    return Result('INFO', 'Base point / survey point / true north', items)


def chk_warnings(ctx, chk):
    warns = list(doc.GetWarnings())
    if not warns:
        return Result('PASS', 'No warnings')
    groups = {}
    for w in warns:
        d = w.GetDescriptionText()
        groups.setdefault(d, []).append(w)
    items = []
    for d, ws in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        ids = list(ws[0].GetFailingElements())
        items.append(('%d x %s' % (len(ws), d), ids[0] if ids else None, ''))
    return Result('WARN', '%d warning(s), %d type(s)' % (len(warns), len(groups)), items)


def chk_file_name(ctx, chk):
    pattern = chk.options.get('FileNamePattern', '')
    path = model_file_path()
    if path:
        name = os.path.splitext(os.path.basename(path))[0]
    else:
        name = os.path.splitext(doc.Title)[0]
    try:
        ok = re.search(pattern, name) is not None
    except re.error:
        return Result('ERROR', 'Invalid FileNamePattern in XML')
    items = [(name, None, 'Checked name')]
    if ok:
        return Result('PASS', 'File name matches the standard', items)
    return Result(flag_status(chk), chk.failure_message or 'File name does not match', items)


def chk_revit_version(ctx, chk):
    items = [('Version', None, app.VersionName), ('Version number', None, app.VersionNumber),
             ('Build', None, app.VersionBuild)]
    return Result('INFO', '%s (build %s)' % (app.VersionName, app.VersionBuild), items)


def chk_file_size(ctx, chk):
    path = model_file_path()
    mb = file_size_mb(path)
    if mb is None:
        return Result('INFO', 'File size not available (file not saved / cloud model)',
                      [(path or doc.Title, None, '')])
    return Result('INFO', '%.1f MB' % mb, [(path, None, '%.1f MB' % mb)])


VIEW_SKIP = ('Schedule', 'Legend', 'DrawingSheet', 'ProjectBrowser', 'SystemBrowser',
             'Internal', 'Undefined', 'Report', 'CostReport', 'LoadsReport',
             'PressureLossReport', 'PanelSchedule', 'ColumnSchedule')


def chk_views_not_on_sheets(ctx, chk):
    placed = set()
    for vp in DB.FilteredElementCollector(doc).OfClass(clr_type(DB.Viewport)).ToElements():
        placed.add(eid_val(vp.ViewId))
    items = []
    for v in DB.FilteredElementCollector(doc).OfClass(clr_type(DB.View)).ToElements():
        try:
            if v.IsTemplate or str(v.ViewType) in VIEW_SKIP:
                continue
            if eid_val(v.Id) in placed:
                continue
            items.append((v.Name, v.Id, str(v.ViewType)))
        except Exception:
            continue
    items.sort(key=lambda x: x[0])
    if items:
        return Result('WARN', '%d view(s) not placed on a sheet' % len(items), items)
    return Result('PASS', 'All views are on sheets')


def chk_phase_elements(ctx, chk):
    counts = {}
    for el in ctx.instances():
        try:
            pid = el.CreatedPhaseId
            if is_valid_id(pid):
                n = safe_name(doc.GetElement(pid))
                counts[n] = counts.get(n, 0) + 1
        except Exception:
            pass
    items = [(k, None, '%d element(s)' % v) for k, v in sorted(counts.items())]
    return Result('INFO', 'Elements created per phase', items)


def _import_instances(linked):
    out = []
    for ii in DB.FilteredElementCollector(doc).OfClass(clr_type(DB.ImportInstance)).ToElements():
        try:
            if ii.IsLinked == linked:
                out.append(ii)
        except Exception:
            pass
    return out


def chk_linked_cad(ctx, chk):
    items = []
    for ii in _import_instances(True):
        t = doc.GetElement(ii.GetTypeId())
        items.append((safe_name(t) or safe_name(ii), ii.Id, ''))
    if items:
        return Result(flag_status(chk), '%d linked CAD file(s)' % len(items), items)
    return Result('PASS', 'No linked CAD files')


def chk_pinned_links(ctx, chk):
    items = []
    for li in DB.FilteredElementCollector(doc).OfClass(clr_type(DB.RevitLinkInstance)).ToElements():
        if not li.Pinned:
            items.append((li.Name, li.Id, 'Revit link'))
    for ii in _import_instances(True):
        if not ii.Pinned:
            items.append((safe_name(ii), ii.Id, 'CAD link'))
    if items:
        return Result(flag_status(chk), '%d link(s) not pinned' % len(items), items)
    return Result('PASS', 'All links are pinned')


def chk_views_hidden(ctx, chk):
    views = []
    for v in DB.FilteredElementCollector(doc).OfClass(clr_type(DB.View)).ToElements():
        try:
            if v.IsTemplate:
                continue
            if str(v.ViewType) in ('FloorPlan', 'CeilingPlan', 'Elevation', 'Section',
                                   'ThreeD', 'EngineeringPlan', 'AreaPlan', 'Detail'):
                views.append(v)
        except Exception:
            pass
    cands = []
    for el in ctx.instances():
        try:
            c = el.Category
            if c is not None and c.CategoryType == DB.CategoryType.Model and not el.ViewSpecific:
                cands.append(el)
        except Exception:
            pass
    items = []
    for n, v in enumerate(views):
        if ctx.cancelled():
            break
        ctx.progress('Hidden elements: view %d / %d' % (n + 1, len(views)), float(n) / max(len(views), 1))
        hidden = 0
        for el in cands:
            try:
                if el.CanBeHidden(v) and el.IsHidden(v):
                    hidden += 1
            except Exception:
                pass
        if hidden:
            items.append((v.Name, v.Id, '%d hidden element(s)' % hidden))
    items.sort(key=lambda x: x[0])
    if items:
        return Result('WARN', '%d view(s) with hidden elements' % len(items), items)
    return Result('PASS', 'No views with permanently hidden elements')


def chk_workset_placement(ctx, chk):
    if not ctx.workshared:
        return Result('INFO', '*Not workshared*')
    wanted = set()
    for ws in DB.FilteredWorksetCollector(doc).OfKind(DB.WorksetKind.UserWorkset):
        n = ws.Name.lower()
        if 'level' in n and 'grid' in n:
            wanted.add(ws.Id.IntegerValue)
    if not wanted:
        return Result('INFO', "No workset with both 'level' and 'grid' in its name")
    items = []
    for el in ctx.instances():
        try:
            if el.WorksetId.IntegerValue in wanted and not isinstance(el, (DB.Level, DB.Grid)):
                text, extra = describe(ctx, el)
                items.append((text, el.Id, extra))
        except Exception:
            pass
    if items:
        return Result(flag_status(chk), '%d element(s) on the Levels and Grids workset that are not levels/grids' % len(items), items)
    return Result('PASS', 'Only levels and grids on that workset')


def chk_line_styles(ctx, chk):
    cat = doc.Settings.Categories.get_Item(DB.BuiltInCategory.OST_Lines)
    items = [(s.Name, None, '') for s in cat.SubCategories]
    items.sort(key=lambda x: x[0])
    return Result('INFO', '%d line style(s)' % len(items), items)


def chk_legends(ctx, chk):
    items = []
    for v in DB.FilteredElementCollector(doc).OfClass(clr_type(DB.View)).ToElements():
        try:
            if str(v.ViewType) == 'Legend':
                items.append((v.Name, v.Id, ''))
        except Exception:
            pass
    items.sort(key=lambda x: x[0])
    return Result('INFO', '%d legend(s)' % len(items), items)


def chk_raster_images(ctx, chk):
    items = []
    for t in DB.FilteredElementCollector(doc).OfClass(clr_type(DB.ImageType)).ToElements():
        items.append((safe_name(t), t.Id, ''))
    items.sort(key=lambda x: x[0])
    return Result('INFO', '%d raster image(s)' % len(items), items)


def chk_keynotes(ctx, chk):
    counts = {}
    for t in DB.FilteredElementCollector(doc).WhereElementIsElementType().ToElements():
        try:
            k = param_text(t, 'KEYNOTE_PARAM')
            if k:
                counts[k] = counts.get(k, 0) + 1
        except Exception:
            pass
    items = [(k, None, '%d type(s)' % v) for k, v in sorted(counts.items())]
    return Result('INFO', '%d keynote(s) used on types' % len(items), items)


def chk_worksets(ctx, chk):
    if not ctx.workshared:
        return Result('INFO', '*Not Workshared*')
    counts = {}
    if chk.options.get('ShowCount', 'False') == 'True':
        for el in ctx.instances():
            try:
                k = el.WorksetId.IntegerValue
                counts[k] = counts.get(k, 0) + 1
            except Exception:
                pass
    items = []
    for ws in DB.FilteredWorksetCollector(doc).OfKind(DB.WorksetKind.UserWorkset):
        n = counts.get(ws.Id.IntegerValue)
        items.append((ws.Name, None, ('%d element(s)' % n) if n is not None else ''))
    items.sort(key=lambda x: x[0])
    return Result('INFO', '%d user workset(s)' % len(items), items)


def chk_unused(ctx, chk):
    ctx.progress('Finding purgeable elements...')
    ids = doc.GetUnusedElements(HashSet[DB.ElementId]())
    items = []
    for i in ids:
        el = doc.GetElement(i)
        if el is None:
            continue
        try:
            cat = el.Category.Name if el.Category is not None else type(el).__name__
        except Exception:
            cat = ''
        items.append(('%s | %s' % (cat, safe_name(el)), i, ''))
    items.sort(key=lambda x: x[0])
    if items:
        return Result('WARN', '%d element(s) can be purged' % len(items), items)
    return Result('PASS', 'Nothing to purge')


def chk_largest_families(ctx, chk):
    fams = []
    for f in DB.FilteredElementCollector(doc).OfClass(clr_type(DB.Family)).ToElements():
        try:
            if not f.IsInPlace and f.IsEditable:
                fams.append(f)
        except Exception:
            pass
    tmp = System.IO.Path.GetTempPath()
    sizes = []
    opts = DB.SaveAsOptions()
    opts.OverwriteExistingFile = True
    for n, f in enumerate(fams):
        if ctx.cancelled():
            break
        ctx.progress('Family size %d / %d: %s' % (n + 1, len(fams), f.Name), float(n) / max(len(fams), 1))
        path = os.path.join(tmp, 'mhc_%d.rfa' % n)
        try:
            fd = doc.EditFamily(f)
            try:
                fd.SaveAs(path, opts)
            finally:
                fd.Close(False)
            sizes.append((os.path.getsize(path), f))
        except Exception:
            pass
        finally:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
    sizes.sort(key=lambda x: -x[0])
    items = [(f.Name, f.Id, '%.2f MB' % (s / 1048576.0)) for s, f in sizes[:10]]
    return Result('INFO', 'Ten largest families (of %d measured)' % len(sizes), items)


CHECK_FUNCS = {
    'Custom': run_custom,
    'DesignOptions': chk_design_options,
    'ProjectCoordinates': chk_project_coordinates,
    'WarningCount': chk_warnings,
    'FileName': chk_file_name,
    'RevitVersion': chk_revit_version,
    'FileSize': chk_file_size,
    'ViewsNotOnSheets': chk_views_not_on_sheets,
    'PhaseElements': chk_phase_elements,
    'LinkedCad': chk_linked_cad,
    'PinnedLinks': chk_pinned_links,
    'ViewsWithHiddenElements': chk_views_hidden,
    'WorksetPlacement': chk_workset_placement,
    'LineStyles': chk_line_styles,
    'Legends': chk_legends,
    'RasterImages': chk_raster_images,
    'Keynotes': chk_keynotes,
    'Worksets': chk_worksets,
    'UnusedElements': chk_unused,
    'TenLargestFamilies': chk_largest_families,
}
SLOW_TYPES = ('ViewsWithHiddenElements', 'UnusedElements', 'TenLargestFamilies')


def chk_imported_dwg(ctx, chk):
    """Every non-linked import, with the evidence shown per row so mismatches can be diagnosed."""
    items = []
    n_dwg = 0
    n_other = 0
    for ii in DB.FilteredElementCollector(doc).OfClass(clr_type(DB.ImportInstance)).ToElements():
        try:
            if ii.IsLinked:
                continue
        except Exception:
            pass
        t = doc.GetElement(ii.GetTypeId())
        try:
            cat = ii.Category.Name if ii.Category is not None else ''
        except Exception:
            cat = ''
        names = [safe_name(ii), safe_name(t), cat]
        is_dwg = 'dwg' in ' '.join(names).lower()          # instance, type or category name; any case
        if is_dwg:
            n_dwg += 1
        else:
            n_other += 1
        label = ''
        for n_ in names:
            if n_ and 'dwg' in n_.lower():
                label = n_
                break
        label = label or safe_name(t) or safe_name(ii) or cat or '(unnamed import)'
        info = ['DWG' if is_dwg else 'NOT named dwg (DXF / SKP / other?)', 'Type: ' + safe_name(t), 'Category: ' + cat]
        try:
            vid = ii.OwnerViewId
            if is_valid_id(vid):
                info.append('2D import in view: ' + safe_name(doc.GetElement(vid)))
            else:
                info.append('model (3D) import')
        except Exception:
            pass
        ws = ctx.workset_name(ii)
        if ws:
            info.append('Workset: ' + ws)
        g = group_of(ii)
        if g is not None:
            info.append('In group: ' + group_label(g))
        items.append((label, ii.Id, ' | '.join(info)))
    items.sort(key=lambda x: x[0])
    if items:
        msg = '%s - %d non-linked import(s): %d DWG, %d other format' % (
            chk.failure_message or 'Imported CAD found', len(items), n_dwg, n_other)
        return Result(flag_status(chk), msg, items)
    return Result('PASS', 'No imported CAD files')


OVERRIDES = {'Imported DWG file': chk_imported_dwg}


def run_check(ctx, chk):
    if chk.type == 'Custom' and chk.name in OVERRIDES:
        return OVERRIDES[chk.name](ctx, chk)
    fn = CHECK_FUNCS.get(chk.type)
    if fn is None:
        return Result('SKIP', 'Check type "%s" is not supported by this tool' % chk.type)
    return fn(ctx, chk)


# ----------------------------------------------------------------------------
# Fixers (the "Fix" buttons)  -  each runs inside one Revit transaction
# ----------------------------------------------------------------------------
class Fixer(object):
    def __init__(self, label, fn, confirm=None, ask=None, plan=None, tip='', group_sensitive=False, destructive=False):
        self.destructive = destructive           # deletes things: locked unless 'Allow delete fixes' is ticked
        self.group_sensitive = group_sensitive   # members of model groups block this fix
        self.label = label
        self.fn = fn              # fn(ids, extra) -> summary text (runs in a transaction)
        self.confirm = confirm    # text with {n}; None = apply without asking
        self.ask = ask            # ask(ids) -> extra object, or None to cancel
        self.plan = plan          # plan(ids, extra) -> list of preview lines
        self.tip = tip


def user_worksets():
    return list(DB.FilteredWorksetCollector(doc).OfKind(DB.WorksetKind.UserWorkset))


def ex_text(ex):
    t = str(ex).strip().splitlines()
    return t[0] if t else ex.__class__.__name__


def short_desc(el):
    try:
        cat = el.Category.Name if el.Category is not None else ''
    except Exception:
        cat = ''
    return '%s | %s (id %s)' % (cat, safe_name(el), eid_val(el.Id))


def add_fail(failures, el, reason):
    failures.append((el.Id, short_desc(el), reason))


def group_of(el):
    """The top-level Group instance that holds el, or None."""
    try:
        gid = el.GroupId
        if not is_valid_id(gid):
            return None
        g = doc.GetElement(gid)
        for _ in range(10):                       # groups can be nested
            pid = g.GroupId
            if is_valid_id(pid):
                g = doc.GetElement(pid)
            else:
                break
        return g
    except Exception:
        return None


def group_label(g):
    try:
        name = g.GroupType.Name
    except Exception:
        name = safe_name(g)
    try:
        n = len(list(g.GetMemberIds()))
    except Exception:
        n = 0
    return '"%s" (%d member(s))' % (name, n)


def ask_ungroup(groups):
    """groups: {key: [Group, selected_count]} -> 'yes' | 'no' | 'cancel'"""
    total = sum(v[1] for v in groups.values())
    lines = ['   %d x in group %s' % (v[1], group_label(v[0])) for v in groups.values()]
    text = ('%d selected element(s) are inside %d group(s). Revit does not let a script (or you) change '
            'group members one by one.\r\n\r\n' % (total, len(groups)))
    text += '\r\n'.join(lines[:10])
    if len(lines) > 10:
        text += '\r\n   ... and %d more group(s)' % (len(lines) - 10)
    text += ('\r\n\r\nYES = ungroup these group instances first, then apply the fix. Only these instances are '
             'ungrouped; the group type and any other copies of it stay as they are.'
             '\r\nNO = skip the grouped elements and fix the rest.\r\nCANCEL = stop.')
    r = WF.MessageBox.Show(text, 'Model Health Check - elements inside groups',
                           WF.MessageBoxButtons.YesNoCancel, WF.MessageBoxIcon.Question)
    if r == WF.DialogResult.Yes:
        return 'yes'
    if r == WF.DialogResult.No:
        return 'no'
    return 'cancel'


def ungroup_for(ids):
    """Ungroup (once each) every group that holds one of ids. Returns (ungrouped, failures, failed_ids)."""
    failures = []
    failed = set()
    tried = set()
    n = 0
    for i in ids:
        for _ in range(10):
            el = doc.GetElement(i)
            if el is None:
                break
            g = group_of(el)
            if g is None:
                break
            k = eid_val(g.Id)
            if k in tried:
                add_fail(failures, el, 'Its group could not be ungrouped (see the first failure for the reason)')
                failed.add(eid_val(i))
                break
            tried.add(k)
            reason = blockers([g.Id], check_pinned=True).get(k)
            if not reason:
                try:
                    glabel = group_label(g)
                    g.UngroupMembers()
                    n += 1
                    FIX_CHANGES.append(('ungrouped', g.Id, glabel, ''))
                    continue
                except Exception as ex:
                    reason = ex_text(ex)
            add_fail(failures, el, 'Could not ungroup %s: %s' % (group_label(g), reason))
            failed.add(eid_val(i))
            break
    return n, failures, failed


def blockers(ids, check_pinned=True, check_group=False):
    """eid -> reason, for elements Revit will not let us change right now."""
    out = {}
    live = [i for i in ids if doc.GetElement(i) is not None]
    if doc.IsWorkshared and len(live) <= 500:
        try:
            col = List[DB.ElementId]()
            for i in live:
                col.Add(i)
            DB.WorksharingUtils.CheckoutElements(doc, col)      # takes ownership of whatever is free
        except Exception:
            pass
        for i in live:
            try:
                st = DB.WorksharingUtils.GetCheckoutStatus(doc, i)
                if st == DB.CheckoutStatus.OwnedByOtherUser:
                    owner = DB.WorksharingUtils.GetWorksharingTooltipInfo(doc, i).Owner
                    out[eid_val(i)] = 'Checked out by %s - ask them to relinquish it or sync, then try again' % owner
                    continue
                if DB.WorksharingUtils.GetModelUpdatesStatus(doc, i) == DB.ModelUpdatesStatus.UpdatedInCentral:
                    out[eid_val(i)] = 'Changed in the central model - Synchronize / Reload Latest first'
            except Exception:
                pass
    for i in live:
        k = eid_val(i)
        if k in out:
            continue
        el = doc.GetElement(i)
        try:
            if check_pinned and el.Pinned:
                out[k] = 'Element is pinned - unpin it first'
            elif check_group and is_valid_id(el.GroupId):
                out[k] = 'Element is inside a group - ungroup it first (the Fix button offers to do that for you)'
        except Exception:
            pass
    return out


class FailLog(DB.IFailuresPreprocessor):
    """Collects Revit's own failure messages so we can show them instead of a silent 'failed'."""
    def __init__(self):
        self.messages = []
        self.has_error = False

    def PreprocessFailures(self, accessor):
        for m in accessor.GetFailureMessages():
            try:
                self.messages.append(m.GetDescriptionText())
                if m.GetSeverity() == DB.FailureSeverity.Error:
                    self.has_error = True
            except Exception:
                pass
        if self.has_error:
            return DB.FailureProcessingResult.ProceedWithRollBack
        accessor.DeleteAllWarnings()
        return DB.FailureProcessingResult.Continue


def show_fix_report(title, summary, failures):
    """failures: list of (ElementId or None, description, reason). Modal report with grouped reasons."""
    groups = {}
    for _, d, r in failures:
        groups[r] = groups.get(r, 0) + 1
    head = summary
    if failures:
        head += '\r\n\r\nWhy some could not be changed:'
        for r, n in sorted(groups.items(), key=lambda kv: -kv[1])[:8]:
            head += '\r\n   %d x  %s' % (n, r)
    else:
        head += '\r\n\r\n(Ctrl+Z in Revit will undo this.)'
    if not failures:
        WF.MessageBox.Show(head, title)
        return
    f = WF.Form()
    f.Text = title
    f.Width = 900
    f.Height = 560
    f.StartPosition = WF.FormStartPosition.CenterParent
    box = WF.TextBox()
    box.Multiline = True
    box.ReadOnly = True
    box.Dock = WF.DockStyle.Top
    box.Height = 150
    box.ScrollBars = WF.ScrollBars.Vertical
    box.Text = head
    lv = WF.ListView()
    lv.Dock = WF.DockStyle.Fill
    lv.View = WF.View.Details
    lv.FullRowSelect = True
    lv.GridLines = True
    lv.Columns.Add('Element', 380)
    lv.Columns.Add('Reason', 460)
    for _, d, r in failures:
        it = WF.ListViewItem(d)
        it.SubItems.Add(r)
        lv.Items.Add(it)
    bar = WF.Panel()
    bar.Dock = WF.DockStyle.Bottom
    bar.Height = 40
    text_all = head + '\r\n\r\n' + '\r\n'.join('%s\t%s' % (d, r) for _, d, r in failures)

    def copy(s_, e_):
        try:
            WF.Clipboard.SetText(text_all)
        except Exception:
            pass
    b1 = WF.Button()
    b1.Text = 'Copy report'
    b1.Left = 8
    b1.Top = 7
    b1.Width = 100
    b1.Click += copy
    bar.Controls.Add(b1)
    b2 = WF.Button()
    b2.Text = 'Close'
    b2.Left = 116
    b2.Top = 7
    b2.DialogResult = WF.DialogResult.OK
    bar.Controls.Add(b2)
    f.Controls.Add(lv)
    f.Controls.Add(box)
    f.Controls.Add(bar)
    f.AcceptButton = b2
    f.ShowDialog()


def delete_ids(ids):
    """Delete elements. Returns (deleted_count, failures); a single failure does not stop the rest."""
    failures = []
    live = [i for i in ids if doc.GetElement(i) is not None]
    blocked = blockers(live, check_pinned=True, check_group=True)
    todo = []
    for i in live:
        r = blocked.get(eid_val(i))
        if r:
            add_fail(failures, doc.GetElement(i), r)
        else:
            todo.append(i)
    if not todo:
        return 0, failures
    descs = dict((eid_val(i), short_desc(doc.GetElement(i))) for i in todo)
    col = List[DB.ElementId]()
    for i in todo:
        col.Add(i)
    try:
        n_deleted = len(doc.Delete(col))
        for i in todo:
            FIX_CHANGES.append(('deleted', i, descs[eid_val(i)], ''))
        return n_deleted, failures
    except Exception:
        done = 0
        for i in todo:
            el = doc.GetElement(i)
            if el is None:                       # already removed together with another element
                continue
            st = DB.SubTransaction(doc)
            st.Start()
            try:
                doc.Delete(i)
                st.Commit()
                done += 1
                FIX_CHANGES.append(('deleted', i, descs.get(eid_val(i), ''), ''))
            except Exception as ex:
                st.RollBack()
                add_fail(failures, el, 'Revit refused to delete it: ' + ex_text(ex))
        return done, failures


def fx_delete(ids, extra):
    done, failures = delete_ids(ids)
    return '%d element(s) deleted, %d skipped' % (done, len(failures)), failures


def fx_pin(ids, extra):
    failures = []
    done = 0
    blocked = blockers(ids, check_pinned=False)
    for i in ids:
        el = doc.GetElement(i)
        if el is None:
            continue
        r = blocked.get(eid_val(i))
        if r:
            add_fail(failures, el, r)
            continue
        try:
            el.Pinned = True
            done += 1
            FIX_CHANGES.append(('pinned', i, short_desc(el), ''))
        except Exception as ex:
            add_fail(failures, el, ex_text(ex))
    return '%d link(s) pinned, %d skipped' % (done, len(failures)), failures


def fx_overlay(ids, extra):
    failures = []
    done = 0
    for i in ids:
        lt = doc.GetElement(i)
        if lt is None:
            continue
        try:
            if getattr(lt, 'IsNestedLink', False):
                add_fail(failures, lt, 'Nested link (linked inside another link) - change it in the parent link\'s own model')
                continue
            lt.AttachmentType = DB.AttachmentType.Overlay
            done += 1
            FIX_CHANGES.append(('set to Overlay', i, short_desc(lt), ''))
        except Exception as ex:
            add_fail(failures, lt, ex_text(ex))
    return '%d link type(s) set to Overlay, %d skipped' % (done, len(failures)), failures


def set_workset(el, ws):
    """Returns None when it worked, otherwise the reason."""
    p = el.get_Parameter(DB.BuiltInParameter.ELEM_PARTITION_PARAM)
    if p is None:
        return 'Element has no workset parameter'
    if p.IsReadOnly:
        return 'Workset is read-only for this element (it may be inside a group or part of another element)'
    try:
        if p.Set(ws.Id.IntegerValue):
            return None
        return 'Revit refused the workset change'
    except Exception as ex:
        return ex_text(ex)


def choose_workset(prompt, exclude):
    """Small modal picker. exclude(name) -> True hides a workset."""
    if not doc.IsWorkshared:
        WF.MessageBox.Show('This model is not workshared.', 'Model Health Check')
        return None
    cands = [w for w in user_worksets() if not (exclude and exclude(w.Name))]
    if not cands:
        WF.MessageBox.Show('No suitable workset found. Create one first (Collaborate > Worksets).',
                           'Model Health Check')
        return None
    f = WF.Form()
    f.Text = 'Choose workset'
    f.Width = 420
    f.Height = 170
    f.StartPosition = WF.FormStartPosition.CenterParent
    f.FormBorderStyle = WF.FormBorderStyle.FixedDialog
    f.MaximizeBox = False
    f.MinimizeBox = False
    lbl = WF.Label()
    lbl.Text = prompt
    lbl.Left = 12
    lbl.Top = 12
    lbl.Width = 380
    f.Controls.Add(lbl)
    cb = WF.ComboBox()
    cb.DropDownStyle = WF.ComboBoxStyle.DropDownList
    cb.Left = 12
    cb.Top = 40
    cb.Width = 380
    for w in cands:
        cb.Items.Add(w.Name)
    cb.SelectedIndex = 0
    for idx, w in enumerate(cands):                       # remember the last choice
        if w.Name == SETTINGS.get('last_workset'):
            cb.SelectedIndex = idx
            break
    f.Controls.Add(cb)
    ok = WF.Button()
    ok.Text = 'OK'
    ok.Left = 232
    ok.Top = 85
    ok.DialogResult = WF.DialogResult.OK
    f.Controls.Add(ok)
    no = WF.Button()
    no.Text = 'Cancel'
    no.Left = 317
    no.Top = 85
    no.DialogResult = WF.DialogResult.Cancel
    f.Controls.Add(no)
    f.AcceptButton = ok
    f.CancelButton = no
    if f.ShowDialog() == WF.DialogResult.OK:
        SETTINGS['last_workset'] = cands[cb.SelectedIndex].Name
        return cands[cb.SelectedIndex]
    return None


def ws_owner_note(ws):
    """Extra hint for a failed move - only when ANOTHER user really owns the target workset."""
    try:
        owner = ws.Owner
        if owner and owner != app.Username:
            return ' (target workset "%s" is owned by %s)' % (ws.Name, owner)
    except Exception:
        pass
    return ''


def fx_move_workset(ids, ws):
    """No pre-check on the target workset: 'Editable: No' only means nobody has it checked out,
    and Revit still lets you move elements into it. We try the move and report what Revit says."""
    failures = []
    done = 0
    blocked = blockers(ids, check_pinned=False, check_group=True)
    note = None
    for i in ids:
        el = doc.GetElement(i)
        if el is None:
            continue
        old = ws_name_of(el)
        r = blocked.get(eid_val(i)) or set_workset(el, ws)
        if r:
            if note is None:
                note = ws_owner_note(ws)
            add_fail(failures, el, r + note)
        else:
            done += 1
            FIX_CHANGES.append(('moved workset', i, short_desc(el), '"%s" -> "%s"' % (old, ws.Name)))
    return '%d element(s) moved to "%s", %d skipped' % (done, ws.Name, len(failures)), failures


def ask_workset_not_link(ids):
    return choose_workset('Move the selected elements to which workset?', lambda n: 'link' in n.lower())


def ask_workset_not_levels_grids(ids):
    return choose_workset('Move the selected elements to which workset?',
                          lambda n: 'level' in n.lower() and 'grid' in n.lower())


LINK_WS_PREFIX = '*LINKED MODEL_'    # e.g. "*LINKED MODEL_CHB-SDT-PNS"


def link_base_name(type_name):
    """'CHB-SDT-PNS.rvt : 1' -> 'CHB-SDT-PNS'"""
    return re.sub(r'(?i)\.rvt.*$', '', (type_name or '').split(' : ')[0]).strip()


def ask_link_plan(ids):
    """Decide (read-only) which workset every link should go to."""
    if not doc.IsWorkshared:
        WF.MessageBox.Show('This model is not workshared, so there are no worksets to move links to.',
                           'Model Health Check')
        return None
    worksets = user_worksets()
    plan = []
    for i in ids:
        li = doc.GetElement(i)
        if li is None:
            continue
        t = doc.GetElement(li.GetTypeId())
        base = link_base_name(safe_name(t) or safe_name(li))
        target = LINK_WS_PREFIX + base
        existing = None
        for w in worksets:                                   # 1) exact name
            if w.Name.strip().lower() == target.lower():
                existing = w
                break
        if existing is None:                                 # 2) a link workset that mentions this link
            for w in worksets:
                n = w.Name.lower()
                if 'link' in n and base.lower() in n:
                    existing = w
                    break
        plan.append((i, base, existing, existing.Name if existing else target))
    return plan


def plan_link_lines(ids, plan):
    lines = []
    for i, base, existing, target in plan:
        lines.append('%s  ->  %s%s' % (base, target, '' if existing else '  (new workset)'))
    return lines


def fx_link_workset(ids, plan):
    failures = []
    created = {}
    done = 0
    blocked = blockers([p[0] for p in plan], check_pinned=False)
    for i, base, existing, target in plan:
        li = doc.GetElement(i)
        if li is None:
            continue
        r = blocked.get(eid_val(i))
        if r:
            add_fail(failures, li, r)
            continue
        try:
            ws = existing
            if ws is None:
                if target not in created:
                    created[target] = DB.Workset.Create(doc, target)
                ws = created[target]
        except Exception as ex:
            add_fail(failures, li, 'Could not create workset "%s": %s' % (target, ex_text(ex)))
            continue
        old = ws_name_of(li)
        r = set_workset(li, ws)
        if r:
            add_fail(failures, li, r)
        else:
            done += 1
            FIX_CHANGES.append(('moved workset', i, short_desc(li), '"%s" -> "%s"' % (old, ws.Name)))
    return '%d link(s) moved, %d new workset(s) created, %d skipped' % (done, len(created), len(failures)), failures


# ---- Family Naming Convention (VDCREF - Revit Family Naming v2) --------------------------
# Family name = [Family Prefix]-[Element]-[Unique Identifier]-[OneGDC Acronym]-[Manufacturer]
# The prefix depends on the Revit category (g-DOR for Doors ...). Only the prefix can be automated.
FAMILY_PREFIX_ROWS = [
    ('Air Terminals', 'g-AIR'),
    ('Generic Annotations', 'g-ANO'),
    ('Cable Trays', 'g-CBT'),
    ('Cable Tray Fittings', 'g-CTF'),
    ('Casework', 'g-CSW'),
    ('Ceilings', 'g-CLG'),
    ('Columns', 'g-COL'),
    ('Communication Devices', 'g-COM'),
    ('Curtain Panels', 'g-CTP'),
    ('Curtain System', 'g-CTS'),
    ('Curtain Wall Mullions', 'g-CTM'),
    ('Data Devices', 'g-DAT'),
    ('Detail Items', 'g-DET'),
    ('Doors', 'g-DOR'),
    ('Duct Accessories', 'g-DCA'),
    ('Duct Fittings', 'g-DCF'),
    ('Duct Insulations', 'g-DCI'),
    ('Duct Linings', 'g-DCL'),
    ('Ducts', 'g-DCT'),
    ('Electrical Equipment', 'g-ELE'),
    ('Electrical Fixtures', 'g-ELF'),
    ('Entourage', 'g-ENT'),
    ('Fire Alarm Devices', 'g-FIR'),
    ('Flex Ducts', 'g-FXD'),
    ('Flex Pipes', 'g-FXP'),
    ('Floors', 'g-FLR'),
    ('Furniture', 'g-FRN'),
    ('Furniture Systems', 'g-FRS'),
    ('Generic Models', 'g-GNM'),
    ('Lighting Devices', 'g-LGD'),
    ('Lighting Fixtures', 'g-LGF'),
    ('Mass', 'g-MAS'),
    ('Mechanical Equipment', 'g-MEC'),
    ('Nurse Call Devices', 'g-NRS'),
    ('Parking', 'g-PRK'),
    ('Pipe Accessories', 'g-PPA'),
    ('Pipe Fittings', 'g-PPF'),
    ('Pipes', 'g-PPS'),
    ('Planting', 'g-PLN'),
    ('Plumbing Fixtures', 'g-PLM'),
    ('Railings', 'g-RLG'),
    ('Ramps', 'g-RMP'),
    ('Roads', 'g-RDS'),
    ('Roofs', 'g-ROF'),
    ('Security Devices', 'g-SEC'),
    ('Shaft Openings', 'g-SFT'),
    ('Site', 'g-SIT'),
    ('Specialty Equipment', 'g-SPC'),
    ('Sprinklers', 'g-SPK'),
    ('Stairs', 'g-STA'),
    ('Structural Area Reinforcement', 'g-SAR'),
    ('Structural Beam System', 'g-SBS'),
    ('Structural Columns', 'g-SCL'),
    ('Structural Connections', 'g-SCO'),
    ('Structural Foundations', 'g-SFO'),
    ('Structural Framing', 'g-SFA'),
    ('Structural Load Cases', 'g-SLC'),
    ('Structural Path Reinforcement', 'g-SPR'),
    ('Structural Rebar', 'g-SRB'),
    ('Structural Stiffeners', 'g-SSF'),
    ('Structural Trusses', 'g-STR'),
    ('Telephone Devices', 'g-TEL'),
    ('Title Blocks', 'g-TTB'),
    ('Topography', 'g-TOP'),
    ('Walls', 'g-WLL'),
    ('Windows', 'g-WDW'),
    ('Wires', 'g-WIR'),
]

FIX_EXTRA_NOTES = {}      # eid -> reason; lets a fixer annotate rows other than the one it reports


def _norm_cat(n):
    k = re.sub(r'[^a-z]', '', (n or '').lower())
    return k[:-1] if k.endswith('s') else k        # Doors / Door, Pipe Fittings / Pipe Fitting ...


def load_prefix_table():
    """Built-in table, overridden by family_prefixes.csv next to this script (if present)."""
    table = {}
    for c, p in FAMILY_PREFIX_ROWS:
        table[_norm_cat(c)] = p
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'family_prefixes.csv')
        if os.path.exists(path):
            for line in open(path).read().splitlines():
                line = line.strip()
                if not line or line.startswith('#') or ',' not in line:
                    continue
                c, p = [x.strip().strip('"') for x in line.rsplit(',', 1)]
                if c.lower() != 'category' and p:
                    table[_norm_cat(c)] = p
    except Exception:
        pass
    return table


def build_family_name(prefix, old):
    """Put the category prefix in front of the existing name (the rest cannot be guessed)."""
    name = re.sub(r'[!,@#$%^&*()]', '', old or '')                     # sheet: avoid special characters
    m = re.match(r'(?i)^g[-_ ]?[a-z]{3}(?=[-_ ])[-_ ]*', name)          # an old / ill-formed g-XXX prefix
    if m:
        name = name[m.end():]
    name = re.sub(r'\s+', ' ', name).strip(' -_')
    return '%s-%s' % (prefix, name) if name else prefix


def ask_family_plan(ids):
    """Read-only: one entry per family (renaming a family fixes all of its instances)."""
    table = load_prefix_table()
    entries = {}
    order = []
    for i in ids:
        el = doc.GetElement(i)
        if el is None:
            continue
        fam = family_of(el)
        if fam is not None:
            key = ('f', eid_val(fam.Id))
        else:
            key = ('s', param_text(el, 'ELEM_FAMILY_PARAM') or safe_name(el))
        e = entries.get(key)
        if e is None:
            if fam is not None:
                try:
                    cat = fam.FamilyCategory.Name
                except Exception:
                    cat = ''
                pre = table.get(_norm_cat(cat))
                e = {'fam': fam, 'old': fam.Name, 'cat': cat, 'new': None, 'problem': None, 'ids': []}
                if pre is None:
                    e['problem'] = 'No prefix for category "%s" in the naming sheet - add a line to family_prefixes.csv' % cat
                else:
                    e['new'] = build_family_name(pre, fam.Name)
            else:
                e = {'fam': None, 'old': key[1], 'cat': '', 'new': None, 'ids': [],
                     'problem': 'System family - its family name cannot be renamed; rename or duplicate its types instead'}
            entries[key] = e
            order.append(key)
        e['ids'].append(i)
    return [entries[k] for k in order]


def plan_family_lines(ids, plan):
    out = []
    for e in sorted(plan, key=lambda x: (x['problem'] is not None, x['old'])):
        if e['problem']:
            out.append('%s   -> SKIPPED: %s' % (e['old'], e['problem']))
        else:
            out.append('%s  ->  %s   (%d instance(s))' % (e['old'], e['new'], len(e['ids'])))
    return out


def _entry_fail(failures, e, reason):
    failures.append((e['ids'][0], 'Family "%s"  (%d instance(s))' % (e['old'], len(e['ids'])), reason))
    for i in e['ids']:
        FIX_EXTRA_NOTES[eid_val(i)] = reason


def fx_family_prefix(ids, plan):
    failures = []
    renamed = 0
    affected = 0
    existing = set()
    for f in DB.FilteredElementCollector(doc).OfClass(clr_type(DB.Family)).ToElements():
        existing.add(safe_name(f).lower())
    for e in plan:
        if e['problem']:
            _entry_fail(failures, e, e['problem'])
            continue
        fam = e['fam']
        new = e['new']
        if new == e['old']:
            continue
        if new.lower() in existing and new.lower() != e['old'].lower():
            _entry_fail(failures, e, 'A family named "%s" already exists - rename one of them by hand' % new)
            continue
        r = blockers([fam.Id], check_pinned=False).get(eid_val(fam.Id))
        if r:
            _entry_fail(failures, e, r)
            continue
        try:
            fam.Name = new
            FIX_CHANGES.append(('renamed family', fam.Id, e['old'], '%s -> %s  (%d instance(s))' % (e['old'], new, len(e['ids']))))
            existing.discard(e['old'].lower())
            existing.add(new.lower())
            renamed += 1
            affected += len(e['ids'])
        except Exception as ex:
            _entry_fail(failures, e, 'Revit refused the rename: ' + ex_text(ex))
    return '%d family(ies) renamed (%d instance(s) affected), %d skipped' % (renamed, affected, len(failures)), failures


REVIEW_WORKSET = 'ZZ_UNHOSTED_REVIEW'


def ask_review_workset(ids):
    if not doc.IsWorkshared:
        WF.MessageBox.Show('This model is not workshared, so there is no workset to move them to.', 'Model Health Check')
        return None
    return REVIEW_WORKSET


def fx_move_review(ids, name):
    """Park the elements on a review workset instead of deleting them."""
    ws = None
    for w in user_worksets():
        if w.Name == name:
            ws = w
            break
    if ws is None:
        try:
            ws = DB.Workset.Create(doc, name)
        except Exception as ex:
            failures = []
            for i in ids:
                el = doc.GetElement(i)
                if el is not None:
                    add_fail(failures, el, 'Could not create workset "%s": %s' % (name, ex_text(ex)))
            return '0 element(s) moved', failures
    return fx_move_workset(ids, ws)


_REVIEW_CONFIRM = ('Move {n} element(s) to the workset "' + REVIEW_WORKSET + '" (created if missing) so they can be reviewed?\r\n\r\n'
                   'Nothing is deleted. Filter that workset in the Worksets dialog or in a view to find them again.')

FIXERS = {
    'Unhosted Family': [Fixer('Move to review workset (no delete)', fx_move_review, ask=ask_review_workset,
                              confirm=_REVIEW_CONFIRM, group_sensitive=True)],
    'Unhosted Interface Points': [Fixer('Move to review workset (no delete)', fx_move_review, ask=ask_review_workset,
                                        confirm=_REVIEW_CONFIRM, group_sensitive=True)],
    'Imported DWG file': [Fixer(
        'Delete imported DWGs', fx_delete,
        confirm='Delete {n} imported DWG instance(s)?\r\n\r\nThis removes the CAD geometry from the model. '
                'Run "Unused Elements" afterwards and purge to clear the left-over layers and styles. '
                'You can undo with Ctrl+Z.', group_sensitive=True, destructive=True)],
    'Link Model Workset': [Fixer(
        'Move links to *LINKED MODEL_ worksets', fx_link_workset,
        ask=ask_link_plan, plan=plan_link_lines,
        confirm='Move {n} link(s) as follows? Missing worksets will be created.')],
    'Revit Links overlay vs Attachment': [Fixer(
        'Set links to Overlay', fx_overlay,
        confirm='Change {n} link type(s) from Attachment to Overlay?')],
    'Elements in Link Workset': [Fixer(
        'Move to another workset...', fx_move_workset,
        ask=ask_workset_not_link,
        confirm='Move {n} element(s) off the link workset?', group_sensitive=True)],
    'WorksetPlacement': [Fixer(
        'Move to another workset...', fx_move_workset,
        ask=ask_workset_not_levels_grids,
        confirm='Move {n} element(s) off the Levels and Grids workset?', group_sensitive=True)],
    'PrefixMatchesCategory': [Fixer(
        'Correct the prefix', fx_family_prefix,
        ask=ask_family_plan, plan=plan_family_lines,
        confirm='Replace the prefix of these families with the one of their category? ({n} selected)\r\n\r\n'
                'Only the g-XXX part changes; the rest of the name is kept. Ctrl+Z undoes this.')],
    'Family Naming Convention': [Fixer(
        'Add category prefix (g-XXX-)', fx_family_prefix,
        ask=ask_family_plan, plan=plan_family_lines,
        confirm='Rename the families below so they start with the prefix of their category? '
                '({n} element(s) selected; each family is renamed once.)\r\n\r\n'
                'The sheet also wants [Element]-[Unique Identifier]-[OneGDC Acronym]-[Manufacturer] after the prefix. '
                'Those parts cannot be guessed, so the existing name is kept after the prefix. Ctrl+Z undoes this.')],
    'PinnedLinks': [Fixer('Pin all links', fx_pin)],
    'UnusedElements': [Fixer(
        'Purge unused', fx_delete,
        confirm='Delete {n} unused element(s) (similar to Manage > Purge Unused)?\r\n\r\n'
                'You can undo with Ctrl+Z.', destructive=True)],
}

# Manual guidance (from the Model QC guide) for checks that cannot be fixed safely by a script
HINTS = {
    'Unhosted Interface Points': u'Interface Point ที่ไม่มี Host: ห้ามลบ ปุ่ม Fix จะย้ายไป Workset ZZ_UNHOSTED_REVIEW เพื่อแยกตรวจเท่านั้น ไม่ลบอะไรทั้งสิ้น',
    'Family Prefix matches Category': u'Prefix g-XXX ของชื่อ Family ต้องตรงกับ Category ตามตารางในไฟล์ Excel เช่น ประตูต้องเป็น g-DOR ปุ่ม Fix จะเปลี่ยนเฉพาะส่วน g-XXX เช็คนี้เป็นเช็คเสริมของเครื่องมือ ไม่อยู่ใน XML และไม่นับในคะแนน',
    'Family Naming Convention': u'ชื่อ Family ต้องขึ้นต้นด้วย Prefix ตาม Category ของ Revit เช่น g-DOR-... (Doors), g-MEC-... (Mechanical Equipment) รูปแบบ [Prefix]-[Element]-[Unique Identifier]-[OneGDC Acronym]-[Manufacturer] ปุ่ม Fix จะเติม Prefix ให้ตาม Category ส่วน Element / Identifier / Acronym / Manufacturer ต้องตั้งเอง และห้ามใช้อักขระพิเศษ ! , @ # $ % ^ & * ( ) Category ที่ไม่มีในตาราง Prefix จะเปลี่ยนชื่อ Family อัตโนมัติไม่ได้ ส่วน System Family (Wall, Floor, Duct ฯลฯ) ไม่ถูกนับเป็น FAIL เพราะเปลี่ยนชื่อ Family ไม่ได้ (ปิดได้ที่ช่อง Skip system families)',
    'Generic Models': u'\u0e41\u0e01\u0e49\u0e40\u0e2d\u0e07: \u0e40\u0e1b\u0e34\u0e14 Family (Edit Family) \u0e41\u0e25\u0e49\u0e27\u0e40\u0e1b\u0e25\u0e35\u0e48\u0e22\u0e19 Category \u0e43\u0e2b\u0e49\u0e15\u0e23\u0e07\u0e0a\u0e19\u0e34\u0e14\u0e27\u0e31\u0e15\u0e16\u0e38 \u0e40\u0e0a\u0e48\u0e19 Mechanical Equipment / Electrical Fixtures \u0e16\u0e49\u0e32\u0e41\u0e01\u0e49\u0e44\u0e21\u0e48\u0e44\u0e14\u0e49\u0e43\u0e2b\u0e49\u0e2a\u0e23\u0e49\u0e32\u0e07 Family \u0e43\u0e2b\u0e21\u0e48\u0e41\u0e25\u0e49\u0e27\u0e27\u0e32\u0e07\u0e17\u0e31\u0e1a\u0e41\u0e17\u0e19 (\u0e2a\u0e04\u0e23\u0e34\u0e1b\u0e15\u0e4c\u0e40\u0e1b\u0e25\u0e35\u0e48\u0e22\u0e19 Category \u0e43\u0e2b\u0e49\u0e44\u0e21\u0e48\u0e44\u0e14\u0e49 \u0e40\u0e1e\u0e23\u0e32\u0e30\u0e15\u0e49\u0e2d\u0e07\u0e23\u0e39\u0e49\u0e27\u0e48\u0e32\u0e27\u0e31\u0e15\u0e16\u0e38\u0e04\u0e37\u0e2d\u0e2d\u0e30\u0e44\u0e23)',
    'In-Place Families': u'\u0e41\u0e01\u0e49\u0e40\u0e2d\u0e07: \u0e16\u0e49\u0e32\u0e43\u0e0a\u0e49\u0e2b\u0e25\u0e32\u0e22\u0e15\u0e33\u0e41\u0e2b\u0e19\u0e48\u0e07 \u0e43\u0e2b\u0e49\u0e2a\u0e23\u0e49\u0e32\u0e07\u0e40\u0e1b\u0e47\u0e19 Loadable Family (.rfa) \u0e41\u0e25\u0e49\u0e27\u0e42\u0e2b\u0e25\u0e14\u0e41\u0e17\u0e19\u0e02\u0e2d\u0e07\u0e40\u0e14\u0e34\u0e21 \u0e16\u0e49\u0e32\u0e40\u0e1b\u0e47\u0e19\u0e07\u0e32\u0e19\u0e1e\u0e34\u0e40\u0e28\u0e29\u0e0a\u0e34\u0e49\u0e19\u0e40\u0e14\u0e35\u0e22\u0e27\u0e04\u0e07\u0e44\u0e27\u0e49\u0e44\u0e14\u0e49 \u0e41\u0e15\u0e48\u0e43\u0e2b\u0e49\u0e19\u0e49\u0e2d\u0e22\u0e17\u0e35\u0e48\u0e2a\u0e38\u0e14',
    'Imported DWG file': u'\u0e41\u0e19\u0e30\u0e19\u0e33: \u0e25\u0e1a DWG \u0e17\u0e35\u0e48 Import \u0e17\u0e34\u0e49\u0e07 (\u0e1b\u0e38\u0e48\u0e21 Fix) \u0e41\u0e25\u0e49\u0e27 Purge Unused \u0e2d\u0e35\u0e01\u0e04\u0e23\u0e31\u0e49\u0e07 \u0e2b\u0e32\u0e01\u0e08\u0e33\u0e40\u0e1b\u0e47\u0e19\u0e15\u0e49\u0e2d\u0e07\u0e43\u0e0a\u0e49\u0e40\u0e1b\u0e47\u0e19 Reference \u0e43\u0e2b\u0e49\u0e43\u0e0a\u0e49 Link CAD \u0e41\u0e17\u0e19 Import CAD',
    'Link Model Workset': u'Workset ของ Link ตั้งชื่อรูปแบบ *LINKED MODEL_<ชื่อไฟล์ Link> เช่น *LINKED MODEL_CHB-SDT-PNS ปุ่ม Fix จะย้าย Link เข้า Workset ชื่อนี้ (ถ้ายังไม่มีจะสร้างให้) ตอน Link ไฟล์ใหม่ให้เลือก Place on: เป็น Workset นี้ตั้งแต่แรก',
    'Elements in Link Workset': u'\u0e27\u0e31\u0e15\u0e16\u0e38\u0e02\u0e2d\u0e07\u0e40\u0e23\u0e32\u0e44\u0e21\u0e48\u0e04\u0e27\u0e23\u0e2d\u0e22\u0e39\u0e48\u0e1a\u0e19 Workset \u0e02\u0e2d\u0e07 Link \u0e43\u0e0a\u0e49\u0e1b\u0e38\u0e48\u0e21 Move \u0e40\u0e1e\u0e37\u0e48\u0e2d\u0e22\u0e49\u0e32\u0e22\u0e44\u0e1b Workset \u0e17\u0e35\u0e48\u0e16\u0e39\u0e01\u0e15\u0e49\u0e2d\u0e07 \u0e16\u0e49\u0e32\u0e40\u0e1b\u0e47\u0e19\u0e27\u0e31\u0e15\u0e16\u0e38\u0e08\u0e32\u0e01\u0e44\u0e1f\u0e25\u0e4c Link \u0e08\u0e23\u0e34\u0e07 \u0e46 \u0e2d\u0e32\u0e08\u0e40\u0e01\u0e34\u0e14\u0e08\u0e32\u0e01\u0e01\u0e32\u0e23 Bind Link',
    'Unhosted Family': u'Family ที่ Host หายไป (Orphaned): ห้ามลบ ปุ่ม Fix จะย้ายไป Workset ZZ_UNHOSTED_REVIEW เพื่อแยกตรวจเท่านั้น ไม่ลบอะไรทั้งสิ้น',
}


EXTRA_HEADING = 'EXTRA - NOT IN THE CHECK SET (not counted in the score)'


def chk_prefix_category(ctx, chk):
    """Families that have a g-XXX prefix, but not the one their category should have."""
    table = load_prefix_table()
    items = []
    for fam in DB.FilteredElementCollector(doc).OfClass(clr_type(DB.Family)).ToElements():
        try:
            cat = fam.FamilyCategory.Name
            expected = table.get(_norm_cat(cat))
            if expected is None:
                continue
            m = re.match(r'^(g-[A-Z]{3})(?=[-_ ]|$)', fam.Name)
            if m is None:                 # no prefix at all: the "Family Naming Convention" check covers that
                continue
            if m.group(1) != expected:
                items.append((fam.Name, fam.Id, 'Category: %s | expected %s | found %s' % (cat, expected, m.group(1))))
        except Exception:
            continue
    items.sort(key=lambda x: x[0])
    if items:
        return Result('FAIL', '%d family(ies) use the prefix of a different category' % len(items), items)
    return Result('PASS', 'Every family prefix matches its category')


CHECK_FUNCS['PrefixMatchesCategory'] = chk_prefix_category


def make_extra_checks():
    d = CheckDef()
    d.heading = EXTRA_HEADING
    d.section = 'Family'
    d.name = 'Family Prefix matches Category'
    d.type = 'PrefixMatchesCategory'
    d.required = False
    d.description = 'Extra check: the g-XXX prefix must be the one the naming sheet assigns to the family\'s category (e.g. a door must be g-DOR).'
    d.extra = True
    return [d]


def fixers_for(chk):
    key = chk.name if chk.type == 'Custom' else chk.type
    return FIXERS.get(key, [])


# ----------------------------------------------------------------------------
# UI  (modeless window + ExternalEvent so Revit stays usable)
# ----------------------------------------------------------------------------
APPDOMAIN_KEY = 'NRK_ModelHealthCheck_Form'

STATUS_COLORS = {
    'PASS': SD.Color.FromArgb(198, 239, 206),
    'FAIL': SD.Color.FromArgb(255, 199, 206),
    'WARN': SD.Color.FromArgb(255, 235, 156),
    'INFO': SD.Color.FromArgb(221, 235, 247),
    'SKIP': SD.Color.FromArgb(230, 230, 230),
    'ERROR': SD.Color.FromArgb(230, 200, 240),
}
ISSUE_STATUSES = ('FAIL', 'WARN', 'ERROR')


def score_kind(chk):
    """Same split Model Checker uses: 'pf' = Pass/Fail check, 'cl' = Count/List check."""
    if chk.type == 'FileName':
        return 'pf'
    if chk.type == 'Custom' and chk.result_condition in ('FailMatchingElements', 'FailNoElements'):
        return 'pf'
    return 'cl'


def tally_checks(results, checks):
    t = {'pass': 0, 'fail': 0, 'cl': 0, 'notrun': 0, 'error': 0}
    for chk in checks:
        if getattr(chk, 'extra', False):
            continue
        res = results.get(id(chk))
        if res is None or res.status == 'SKIP':
            t['notrun'] += 1
        elif res.status == 'ERROR':
            t['error'] += 1
        elif score_kind(chk) == 'cl':
            t['cl'] += 1
        elif res.status == 'PASS':
            t['pass'] += 1
        else:
            t['fail'] += 1
    return t


def score_pct(t):
    """Model Checker score = Pass / (Pass + Fail), rounded; None when nothing pass/fail has run."""
    d = t['pass'] + t['fail']
    return int(100.0 * t['pass'] / d + 0.5) if d else None


def tally_text(t):
    txt = '%d Pass, %d Fail, %d Count/List, %d Not Run' % (t['pass'], t['fail'], t['cl'], t['notrun'])
    if t['error']:
        txt += ', %d Error' % t['error']
    return txt

LAST_XML_FILE = os.path.join(os.environ.get('APPDATA', ''), 'pyRevit', 'NRK_ModelHealthCheck_lastxml.txt')


def default_xml():
    saved = SETTINGS.get('xml_path')
    if saved and os.path.exists(saved):
        return saved
    here = os.path.dirname(os.path.abspath(__file__))
    for f in sorted(os.listdir(here)):
        if f.lower().endswith('.xml'):
            return os.path.join(here, f)
    try:
        if os.path.exists(LAST_XML_FILE):
            p = open(LAST_XML_FILE).read().strip()
            if os.path.exists(p):
                return p
    except Exception:
        pass
    return ''


def csv_cell(v):
    v = '' if v is None else str(v)
    return '"' + v.replace('"', '""') + '"'


class RevitWindow(WF.IWin32Window):
    """Makes the Revit main window the owner so our window stays above it."""
    def __init__(self, handle):
        self._handle = handle

    @property
    def Handle(self):
        return self._handle


def show_score_history(model):
    """Modal dialog: score over time for this model (line chart + list)."""
    rows = read_history(model)
    f = WF.Form()
    f.Text = 'Score history - %s' % (model or '')
    f.Width = 780
    f.Height = 580
    f.StartPosition = WF.FormStartPosition.CenterParent
    chart = WF.Panel()
    chart.Dock = WF.DockStyle.Top
    chart.Height = 230
    chart.BackColor = SD.Color.White

    def paint(s_, e_):
        try:
            g = e_.Graphics
            w = float(chart.ClientSize.Width)
            h = float(chart.ClientSize.Height)
            left, right, top, bottom = 46.0, 18.0, 16.0, 26.0
            pw = w - left - right
            ph = h - top - bottom
            grid = SD.Pen(SD.Color.LightGray)
            font = SD.Font('Segoe UI', 8.0)
            for v in (0, 25, 50, 75, 100):
                y = top + ph - ph * v / 100.0
                g.DrawLine(grid, left, y, left + pw, y)
                g.DrawString('%d%%' % v, font, SD.Brushes.Gray, 6.0, y - 7.0)
            if not rows:
                g.DrawString('No history yet - run all checks to record a score.', font, SD.Brushes.Gray, left + 10.0, top + 10.0)
                return
            n = len(rows)
            pts = []
            for k, r in enumerate(rows):
                x = left + (pw * k / float(n - 1) if n > 1 else pw / 2.0)
                y = top + ph - ph * r[1] / 100.0
                pts.append((float(x), float(y)))
            pen = SD.Pen(SD.Color.FromArgb(40, 120, 200), 2.0)
            for a, b in zip(pts, pts[1:]):
                g.DrawLine(pen, a[0], a[1], b[0], b[1])
            for (x, y), r in zip(pts, rows):
                g.FillEllipse(SD.Brushes.SteelBlue, x - 3.0, y - 3.0, 6.0, 6.0)
            x, y = pts[-1]
            g.DrawString('%d%%' % rows[-1][1], SD.Font('Segoe UI', 9.0, SD.FontStyle.Bold), SD.Brushes.Black, x - 14.0, y - 22.0)
        except Exception:
            pass
    chart.Paint += paint

    lv = WF.ListView()
    lv.Dock = WF.DockStyle.Fill
    lv.View = WF.View.Details
    lv.FullRowSelect = True
    lv.GridLines = True
    for name, width in (('Date', 150), ('Score', 70), ('Change', 70), ('Pass', 60), ('Fail', 60), ('Count/List', 80), ('Not Run', 70)):
        lv.Columns.Add(name, width)
    prev = None
    ordered = []
    for r in rows:
        ordered.append((r, None if prev is None else r[1] - prev))
        prev = r[1]
    for r, delta in reversed(ordered):                       # newest first
        it = WF.ListViewItem(r[0])
        it.SubItems.Add('%d%%' % r[1])
        it.SubItems.Add('' if delta is None else ('%+d' % delta))
        it.SubItems.Add(str(r[2]))
        it.SubItems.Add(str(r[3]))
        it.SubItems.Add(str(r[4]))
        it.SubItems.Add(str(r[5]))
        lv.Items.Add(it)
    bar = WF.Panel()
    bar.Dock = WF.DockStyle.Bottom
    bar.Height = 40
    b = WF.Button()
    b.Text = 'Close'
    b.Left = 8
    b.Top = 7
    b.DialogResult = WF.DialogResult.OK
    bar.Controls.Add(b)
    f.Controls.Add(lv)
    f.Controls.Add(chart)
    f.Controls.Add(bar)
    f.AcceptButton = b
    f.ShowDialog()


class _Handler(UI.IExternalEventHandler):
    """Runs queued jobs inside a valid Revit API context."""
    def __init__(self):
        self.queue = []
        self.on_error = None

    def Execute(self, uiapp_):
        global uidoc, doc, app
        uidoc = uiapp_.ActiveUIDocument
        doc = uidoc.Document if uidoc is not None else None
        app = uiapp_.Application
        while self.queue:
            job = self.queue.pop(0)
            try:
                job()
            except Exception as ex:
                if self.on_error:
                    self.on_error(ex)

    def GetName(self):
        return 'NRK Model Health Check'


def make_double_buffered(ctrl):
    try:
        flags = System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic
        ctrl.GetType().GetProperty('DoubleBuffered', flags).SetValue(ctrl, True, None)
    except Exception:
        pass


class HealthForm(WF.Form):
    def __init__(self, handler, event):
        WF.Form.__init__(self)
        self.handler = handler
        self.event = event
        self._appdomain = System.AppDomain          # kept on the instance: no global lookup at close time
        self._appdomain_key = APPDOMAIN_KEY
        handler.on_error = self.show_error
        self.checks = []
        self.results = {}
        self.rows = {}                  # id(chk) -> ListViewItem
        self.current = None             # CheckDef shown on the right
        self.cancel = False
        self.busy = False
        self.building = False
        self.model_title = None
        self.fail_notes = {}            # eid -> why the last fix could not change it
        self.groups = {}                # heading -> ListViewGroup (header shows that group's score)
        self._last_logged = None        # last score written to the history file
        self._run_i = 0                 # progress: checks finished so far
        self._run_n = 0                 # progress: checks in this run

        self.Text = 'Model Health Check'
        self.Width = 1300
        self.Height = 800
        self.MinimumSize = SD.Size(980, 560)
        self.StartPosition = WF.FormStartPosition.CenterScreen
        try:
            w = SETTINGS.get('win')
            if w and len(w) == 4:
                rect = SD.Rectangle(int(w[0]), int(w[1]), int(w[2]), int(w[3]))
                if any(sc.WorkingArea.IntersectsWith(rect) for sc in WF.Screen.AllScreens):   # still on a screen
                    self.StartPosition = WF.FormStartPosition.Manual
                    self.Location = SD.Point(rect.X, rect.Y)
                    self.Size = SD.Size(rect.Width, rect.Height)
        except Exception:
            pass
        self.Font = SD.Font('Segoe UI', 9.0)
        self.KeyPreview = True
        self.KeyDown += self.on_key

        self.build_top()
        self.build_main()
        self.build_status()
        # docking order: Fill first, then Top / Bottom
        self.Controls.Add(self.split)
        self.Controls.Add(self.top)
        self.Controls.Add(self.status)

        self.Shown += self.on_shown
        self.FormClosed += self.on_closed

    # ------------------------------------------------------------ layout
    def build_top(self):
        self.top = WF.Panel()
        self.top.Dock = WF.DockStyle.Top
        self.top.Height = 96
        self.top.Padding = WF.Padding(8, 6, 8, 4)

        l1 = WF.Label()
        l1.Text = 'Model:'
        l1.Left = 8
        l1.Top = 9
        l1.AutoSize = True
        self.top.Controls.Add(l1)
        self.lbl_model = WF.Label()
        self.lbl_model.Left = 90
        self.lbl_model.Top = 8
        self.lbl_model.AutoSize = True
        self.lbl_model.Font = SD.Font('Segoe UI', 10.0, SD.FontStyle.Bold)
        self.lbl_model.Text = '-'
        self.top.Controls.Add(self.lbl_model)

        l3 = WF.Label()
        l3.Text = 'Score:'
        l3.Left = 8
        l3.Top = 36
        l3.AutoSize = True
        self.top.Controls.Add(l3)
        self.lbl_score_sum = WF.Label()
        self.lbl_score_sum.Left = 90
        self.lbl_score_sum.Top = 36
        self.lbl_score_sum.AutoSize = True
        self.lbl_score_sum.Text = 'Run the checks to get the score'
        self.top.Controls.Add(self.lbl_score_sum)

        self.lbl_score = WF.Label()          # the big number, like Model Checker's "45%"
        self.lbl_score.Left = 1080
        self.lbl_score.Top = 2
        self.lbl_score.Width = 190
        self.lbl_score.Height = 54
        self.lbl_score.TextAlign = SD.ContentAlignment.MiddleRight
        self.lbl_score.Font = SD.Font('Segoe UI', 28.0, SD.FontStyle.Bold)
        self.lbl_score.Anchor = WF.AnchorStyles.Top | WF.AnchorStyles.Right
        self.lbl_score.ForeColor = SD.Color.Gray
        self.lbl_score.Text = '--'
        self.top.Controls.Add(self.lbl_score)

        l2 = WF.Label()
        l2.Text = 'Check set XML:'
        l2.Left = 8
        l2.Top = 68
        l2.AutoSize = True
        self.top.Controls.Add(l2)
        self.txt_xml = WF.TextBox()
        self.txt_xml.Left = 90
        self.txt_xml.Top = 65
        self.txt_xml.Width = 960
        self.txt_xml.ReadOnly = True
        self.txt_xml.Anchor = WF.AnchorStyles.Top | WF.AnchorStyles.Left | WF.AnchorStyles.Right
        self.top.Controls.Add(self.txt_xml)
        b = WF.Button()
        b.Text = 'Browse...'
        b.Left = 1060
        b.Top = 63
        b.Width = 80
        b.Anchor = WF.AnchorStyles.Top | WF.AnchorStyles.Right
        b.Click += self.on_browse
        self.top.Controls.Add(b)
        b2 = WF.Button()
        b2.Text = 'Reload'
        b2.Left = 1146
        b2.Top = 63
        b2.Width = 70
        b2.Anchor = WF.AnchorStyles.Top | WF.AnchorStyles.Right
        b2.Click += lambda s, e: self.load_checks()
        self.top.Controls.Add(b2)

    def build_main(self):
        self.split = WF.SplitContainer()
        self.split.Dock = WF.DockStyle.Fill
        self.split.SplitterDistance = 560

        # ---- left: filter bar, list, buttons
        lbar = WF.Panel()
        lbar.Dock = WF.DockStyle.Top
        lbar.Height = 32
        lbl = WF.Label()
        lbl.Text = 'Search:'
        lbl.Left = 4
        lbl.Top = 9
        lbl.AutoSize = True
        lbar.Controls.Add(lbl)
        self.txt_search = WF.TextBox()
        self.txt_search.Left = 56
        self.txt_search.Top = 5
        self.txt_search.Width = 190
        self.txt_search.TextChanged += lambda s, e: self.refresh_list()
        lbar.Controls.Add(self.txt_search)
        self.chk_issues = WF.CheckBox()
        self.chk_issues.Text = 'Issues only'
        self.chk_issues.Left = 256
        self.chk_issues.Top = 6
        self.chk_issues.AutoSize = True
        self.chk_issues.CheckedChanged += lambda s, e: self.refresh_list()
        lbar.Controls.Add(self.chk_issues)
        self.chk_sys = WF.CheckBox()
        self.chk_sys.Text = 'Skip system families'
        self.chk_sys.Left = 350
        self.chk_sys.Top = 6
        self.chk_sys.AutoSize = True
        self.chk_sys.Checked = True
        self.chk_sys.CheckedChanged += self.on_sys_changed
        lbar.Controls.Add(self.chk_sys)

        lbtm = WF.Panel()
        lbtm.Dock = WF.DockStyle.Bottom
        lbtm.Height = 40
        self.btn_run_sel = self.make_button(lbtm, 'Run selected (F5)', 4, 6, self.on_run_selected, 130)
        self.btn_run_all = self.make_button(lbtm, 'Run all ticked', 140, 6, self.on_run_ticked, 110)
        self.make_button(lbtm, 'Tick all', 258, 6, lambda s, e: self.tick_all(True), 65)
        self.make_button(lbtm, 'Tick none', 328, 6, lambda s, e: self.tick_all(False), 75)
        self.make_button(lbtm, 'XML defaults', 408, 6, self.on_xml_defaults, 95)

        self.lv = WF.ListView()
        self.lv.Dock = WF.DockStyle.Fill
        self.lv.View = WF.View.Details
        self.lv.CheckBoxes = True
        self.lv.FullRowSelect = True
        self.lv.GridLines = True
        self.lv.HideSelection = False
        self.lv.MultiSelect = False
        self.lv.Columns.Add('Check', 260)
        self.lv.Columns.Add('Section', 85)
        self.lv.Columns.Add('Status', 65)
        self.lv.Columns.Add('Count', 50)
        make_double_buffered(self.lv)
        self.lv.SelectedIndexChanged += self.on_check_selected
        self.lv.ItemChecked += self.on_item_checked
        self.lv.DoubleClick += self.on_run_selected
        self.split.Panel1.Controls.Add(self.lv)
        self.split.Panel1.Controls.Add(lbar)
        self.split.Panel1.Controls.Add(lbtm)

        # ---- right: title, message, items, buttons
        self.lbl_title = WF.Label()
        self.lbl_title.Dock = WF.DockStyle.Top
        self.lbl_title.Height = 28
        self.lbl_title.Font = SD.Font('Segoe UI', 11.0, SD.FontStyle.Bold)
        self.lbl_title.Padding = WF.Padding(4, 4, 0, 0)
        self.lbl_title.Text = 'Select a check on the left'

        self.msg = WF.TextBox()
        self.msg.Dock = WF.DockStyle.Top
        self.msg.Multiline = True
        self.msg.ReadOnly = True
        self.msg.Height = 92
        self.msg.BorderStyle = getattr(WF.BorderStyle, 'None')
        self.msg.BackColor = SD.Color.FromArgb(245, 245, 245)
        self.msg.ScrollBars = WF.ScrollBars.Vertical

        rbtm = WF.Panel()
        rbtm.Dock = WF.DockStyle.Bottom
        rbtm.Height = 74
        self.make_button(rbtm, 'Select in Revit', 4, 6, self.on_select_clicked, 105)
        self.make_button(rbtm, 'Zoom to', 114, 6, self.on_zoom_clicked, 75)
        self.chk_auto = WF.CheckBox()
        self.chk_auto.Text = 'Auto-select when I click a row'
        self.chk_auto.Left = 200
        self.chk_auto.Top = 11
        self.chk_auto.AutoSize = True
        self.chk_auto.Checked = True
        rbtm.Controls.Add(self.chk_auto)
        self.chk_group = WF.CheckBox()
        self.chk_group.Text = 'Group by family'
        self.chk_group.Left = 420
        self.chk_group.Top = 11
        self.chk_group.AutoSize = True
        self.chk_group.Checked = True
        self.chk_group.CheckedChanged += lambda s_, e_: self.show_details()
        rbtm.Controls.Add(self.chk_group)
        self.make_button(rbtm, 'Export CSV', 4, 40, self.on_export, 90)
        self.make_button(rbtm, 'Score history', 100, 40, self.on_history, 100)
        self.make_button(rbtm, 'Fix log', 206, 40, self.on_open_log, 80)

        self.dv = WF.ListView()
        self.dv.Dock = WF.DockStyle.Fill
        self.dv.View = WF.View.Details
        self.dv.FullRowSelect = True
        self.dv.GridLines = True
        self.dv.MultiSelect = True
        self.dv.HideSelection = False
        self.dv.Columns.Add('Item', 420)
        self.dv.Columns.Add('Element Id', 80)
        self.dv.Columns.Add('Info', 420)
        make_double_buffered(self.dv)
        self.dv.SelectedIndexChanged += self.on_item_selected
        self.dv.DoubleClick += self.on_zoom_clicked

        self.fix_panel = WF.FlowLayoutPanel()
        self.fix_panel.Dock = WF.DockStyle.Top
        self.fix_panel.AutoSize = True
        self.fix_panel.AutoSizeMode = WF.AutoSizeMode.GrowAndShrink
        self.fix_panel.Padding = WF.Padding(0, 4, 0, 4)
        self.fix_panel.Visible = False
        self.chk_delete = WF.CheckBox()
        self.chk_delete.Text = 'Allow delete fixes'
        self.chk_delete.AutoSize = True
        self.chk_delete.Checked = False
        self.chk_delete.CheckedChanged += self.on_delete_toggled

        self.split.Panel2.Controls.Add(self.dv)
        self.split.Panel2.Controls.Add(self.fix_panel)
        self.split.Panel2.Controls.Add(self.msg)
        self.split.Panel2.Controls.Add(self.lbl_title)
        self.split.Panel2.Controls.Add(rbtm)

    def build_status(self):
        self.status = WF.Panel()
        self.status.Dock = WF.DockStyle.Bottom
        self.status.Height = 34
        self.pb = WF.ProgressBar()
        self.pb.Left = 8
        self.pb.Top = 8
        self.pb.Width = 220
        self.pb.Height = 18
        self.pb.Minimum = 0
        self.pb.Maximum = 100
        self.status.Controls.Add(self.pb)
        self.lbl_pct = WF.Label()
        self.lbl_pct.Left = 234
        self.lbl_pct.Top = 8
        self.lbl_pct.Width = 46
        self.lbl_pct.Font = SD.Font('Segoe UI', 9.0, SD.FontStyle.Bold)
        self.lbl_pct.Text = '0%'
        self.status.Controls.Add(self.lbl_pct)
        self.btn_stop = self.make_button(self.status, 'Stop', 284, 4, self.on_stop, 60)
        self.btn_stop.Enabled = False
        self.lbl_sum = WF.Label()
        self.lbl_sum.Left = 356
        self.lbl_sum.Top = 9
        self.lbl_sum.AutoSize = True
        self.lbl_sum.Text = 'Ready'
        self.status.Controls.Add(self.lbl_sum)

    def make_button(self, parent, text, left, top, handler, width=85):
        b = WF.Button()
        b.Text = text
        b.Left = left
        b.Top = top
        b.Width = width
        b.Height = 26
        b.Click += handler
        parent.Controls.Add(b)
        return b

    # ------------------------------------------------------------ plumbing
    def post(self, job):
        """Queue a job that needs the Revit API and wake the external event."""
        self.handler.queue.append(job)
        self.event.Raise()

    def show_error(self, ex):
        self.busy = False
        self.btn_stop.Enabled = False
        self.btn_run_sel.Enabled = True
        self.btn_run_all.Enabled = True
        self.lbl_sum.Text = 'Error: %s' % ex
        WF.MessageBox.Show('Something went wrong:\r\n%s' % ex, 'Model Health Check')

    def sync_model(self):
        """Called inside a job: keep the title current, drop stale results."""
        title = doc.Title if doc is not None else '(no model open)'
        if self.model_title is not None and title != self.model_title:
            self.results = {}
            self.fail_notes = {}
            self.refresh_list()
            self.show_details()
        self.model_title = title
        self.lbl_model.Text = title

    def on_shown(self, s, e):
        self.txt_xml.Text = default_xml()
        try:
            self.chk_issues.Checked = bool(SETTINGS.get('issues_only', False))
            self.chk_auto.Checked = bool(SETTINGS.get('auto_select', True))
            self.chk_sys.Checked = bool(SETTINGS.get('skip_system_families', True))
            self.chk_group.Checked = bool(SETTINGS.get('group_by_family', True))
            self.chk_delete.Checked = bool(SETTINGS.get('allow_delete', False))
            if SETTINGS.get('splitter'):
                self.split.SplitterDistance = int(SETTINGS['splitter'])
            if SETTINGS.get('maximized'):
                self.WindowState = WF.FormWindowState.Maximized
        except Exception:
            pass
        self.txt_xml.Text = default_xml()
        self.load_checks()
        self.post(self.sync_model)

    def persist(self):
        """Write the current settings to disk (window, ticks, toggles, last XML)."""
        try:
            d = SETTINGS
            if self.txt_xml.Text.strip():
                d['xml_path'] = self.txt_xml.Text.strip()
            if self.checks:
                d['ticked'] = dict(('%s|%s' % (c.heading, c.name), bool(c.checked)) for c in self.checks)
            d['issues_only'] = bool(self.chk_issues.Checked)
            d['auto_select'] = bool(self.chk_auto.Checked)
            d['skip_system_families'] = bool(self.chk_sys.Checked)
            d['group_by_family'] = bool(self.chk_group.Checked)
            d['allow_delete'] = bool(self.chk_delete.Checked)
            normal = (self.WindowState == WF.FormWindowState.Normal)
            b = self.Bounds if normal else self.RestoreBounds
            d['win'] = [b.X, b.Y, b.Width, b.Height]
            d['maximized'] = (self.WindowState == WF.FormWindowState.Maximized)
            d['splitter'] = int(self.split.SplitterDistance)
            save_settings(d)
        except Exception:
            pass

    def on_closed(self, s, e):
        self.persist()
        self.cancel = True
        try:
            self._appdomain.CurrentDomain.SetData(self._appdomain_key, None)
        except Exception:
            pass

    def on_key(self, s, e):
        if e.KeyCode == WF.Keys.F5:
            self.on_run_selected(None, None)
            e.Handled = True

    # ------------------------------------------------------------ loading
    def on_browse(self, s, e):
        dlg = WF.OpenFileDialog()
        dlg.Filter = 'Check set XML (*.xml)|*.xml'
        if dlg.ShowDialog() == WF.DialogResult.OK:
            self.txt_xml.Text = dlg.FileName
            self.load_checks()

    def load_checks(self):
        path = self.txt_xml.Text.strip()
        self.results = {}
        self.checks = []
        self.current = None
        self.refresh_list()
        self.show_details()
        if not path or not os.path.exists(path):
            self.msg.Text = 'Select the check set XML file first (Browse...).'
            return
        try:
            self.checks, name = parse_checkset(path)
        except Exception as ex:
            self.msg.Text = 'Could not read XML: %s' % ex
            return
        self.checks = self.checks + make_extra_checks()
        try:
            open(LAST_XML_FILE, 'w').write(path)
        except Exception:
            pass
        ticked = SETTINGS.get('ticked') or {}
        for chk in self.checks:
            k = '%s|%s' % (chk.heading, chk.name)
            if k in ticked:
                chk.checked = bool(ticked[k])
        self.Text = 'Model Health Check  -  %s' % name
        self.refresh_list()
        self.persist()
        self.lbl_sum.Text = '%d checks loaded (your last tick settings applied)' % len(self.checks)
        self.msg.Text = ('Double-click a check (or press F5) to run just that one. '
                         'Use "Run all ticked" before you submit the model.')

    # ------------------------------------------------------------ left list
    def refresh_list(self):
        self.building = True
        keep = self.current
        text = self.txt_search.Text.strip().lower()
        issues_only = self.chk_issues.Checked
        self.lv.BeginUpdate()
        self.lv.Items.Clear()
        self.lv.Groups.Clear()
        self.rows = {}
        groups = {}
        for chk in self.checks:
            res = self.results.get(id(chk))
            if text and text not in chk.name.lower() and text not in chk.section.lower():
                continue
            if issues_only and res is not None and res.status not in ISSUE_STATUSES:
                continue
            if chk.heading not in groups:
                g = WF.ListViewGroup(chk.heading, self.group_header(chk.heading))
                self.lv.Groups.Add(g)
                groups[chk.heading] = g
            label = chk.name + ('  (slow)' if chk.type in SLOW_TYPES else '')
            it = WF.ListViewItem(label)
            it.SubItems.Add(chk.section)
            it.SubItems.Add(res.status if res else 'Not run')
            it.SubItems.Add(str(res.count) if (res and res.count) else '')
            if res:
                it.BackColor = STATUS_COLORS.get(res.status, SD.Color.White)
            it.Checked = chk.checked
            it.Tag = chk
            it.Group = groups[chk.heading]
            self.lv.Items.Add(it)
            self.rows[id(chk)] = it
            if chk is keep:
                it.Selected = True
        self.lv.EndUpdate()
        self.groups = groups
        self.building = False
        self.update_scores()

    def group_header(self, heading):
        if heading == EXTRA_HEADING:
            return heading
        t = tally_checks(self.results, [c for c in self.checks if c.heading == heading])
        p = score_pct(t)
        return '%s     %s%s' % (heading, ('%d%%   -   ' % p) if p is not None else '', tally_text(t))

    def update_scores(self):
        """Overall score (big number) and the per-priority headers, same maths as Model Checker."""
        t = tally_checks(self.results, self.checks)
        p = score_pct(t)
        if p is None:
            self.lbl_score.Text = '--'
            self.lbl_score.ForeColor = SD.Color.Gray
            self.lbl_score_sum.Text = '%d checks  |  %s' % (len([c for c in self.checks if not c.extra]), tally_text(t))
        else:
            self.lbl_score.Text = '%d%%' % p
            self.lbl_score.ForeColor = (SD.Color.FromArgb(200, 40, 40) if p < 50 else
                                        SD.Color.FromArgb(220, 130, 0) if p < 80 else
                                        SD.Color.FromArgb(40, 150, 60))
            self.lbl_score_sum.Text = '%d checks  |  %d Pass (%d%%)  |  %d Fail  |  %d Count/List  |  %d Not Run%s' % (
                len([c for c in self.checks if not c.extra]), t['pass'], p, t['fail'], t['cl'], t['notrun'],
                ('  |  %d Error' % t['error']) if t['error'] else '')
        for h, g in self.groups.items():
            try:
                g.Header = self.group_header(h)
            except Exception:
                pass

    def on_sys_changed(self, s, e):
        SETTINGS['skip_system_families'] = bool(self.chk_sys.Checked)
        self.lbl_sum.Text = 'Option changed - run "Family Naming Convention" again to apply it'

    def on_xml_defaults(self, s, e):
        for chk in self.checks:
            chk.checked = chk.xml_checked
        self.refresh_list()

    def on_item_checked(self, s, e):
        if not self.building and e.Item.Tag is not None:
            e.Item.Tag.checked = e.Item.Checked

    def tick_all(self, state):
        for chk in self.checks:
            chk.checked = state
        self.refresh_list()

    def on_check_selected(self, s, e):
        if self.building or self.lv.SelectedItems.Count == 0:
            return
        self.current = self.lv.SelectedItems[0].Tag
        self.show_details()

    # ------------------------------------------------------------ running
    def selected_check(self):
        if self.lv.SelectedItems.Count:
            return self.lv.SelectedItems[0].Tag
        return self.current

    def on_run_selected(self, s, e):
        chk = self.selected_check()
        if chk is None or self.busy:
            return
        self.set_busy(True)
        self.post(lambda: self.do_run([chk]))

    def on_run_ticked(self, s, e):
        chks = [c for c in self.checks if c.checked]
        if not chks or self.busy:
            return
        self.set_busy(True)
        self.post(lambda: self.do_run(chks))

    def on_stop(self, s, e):
        self.cancel = True

    def set_busy(self, state):
        self.busy = state
        self.btn_run_sel.Enabled = not state
        self.btn_run_all.Enabled = not state
        self.btn_stop.Enabled = state
        if state:
            self.cancel = False

    def set_progress(self, text, frac=None):
        """Overall % = (checks finished + fraction of the current one) / checks in this run."""
        if self._run_n:
            pos = self._run_i + (frac if frac is not None else 0.0)
            pct = max(0, min(100, int(100.0 * pos / self._run_n)))
            self.pb.Value = pct
            self.lbl_pct.Text = '%d%%' % pct
        self.lbl_sum.Text = text
        WF.Application.DoEvents()

    def do_run(self, chks):
        self.sync_model()
        t0 = time.time()
        ctx = Ctx(progress=self.set_progress, cancelled=lambda: self.cancel)
        self._run_n = len(chks)
        self._run_i = 0
        self.pb.Value = 0
        self.lbl_pct.Text = '0%'
        done = 0
        for chk in chks:
            if self.cancel:
                break
            row = self.rows.get(id(chk))
            if row is not None:
                row.SubItems[2].Text = 'Running'
            self.set_progress('Running: ' + chk.name)
            try:
                res = run_check(ctx, chk)
            except Exception as ex:
                res = Result('ERROR', str(ex))
            self.results[id(chk)] = res
            if row is not None:
                row.SubItems[2].Text = res.status
                row.SubItems[3].Text = str(res.count) if res.count else ''
                row.BackColor = STATUS_COLORS.get(res.status, SD.Color.White)
            done += 1
            self._run_i = done
            self.set_progress('Finished: ' + chk.name)
        if not self.cancel:
            self.set_progress('Done')
        self._run_n = 0
        self.set_busy(False)
        self.update_scores()
        self.persist()
        if not self.cancel:
            self.log_history()
        self.update_summary(time.time() - t0)
        if self.chk_issues.Checked:
            self.refresh_list()
        self.show_details()

    def update_summary(self, secs):
        tally = {}
        for r in self.results.values():
            tally[r.status] = tally.get(r.status, 0) + 1
        parts = ['%s %d' % (k, tally[k]) for k in ('FAIL', 'WARN', 'PASS', 'INFO', 'SKIP', 'ERROR') if k in tally]
        self.lbl_sum.Text = '%s    (%.0f s)%s' % ('  |  '.join(parts), secs, '   - stopped' if self.cancel else '')

    # ------------------------------------------------------------ details
    def show_details(self):
        chk = self.current
        self.dv.Items.Clear()
        self.fix_panel.Controls.Clear()
        self.fix_panel.Visible = False
        if chk is None:
            self.lbl_title.Text = 'Select a check on the left'
            return
        self.lbl_title.Text = chk.name
        res = self.results.get(id(chk))
        head = chk.description or ''
        hint = HINTS.get(chk.name)
        tail = (u'\r\n' + hint) if hint else u''
        if res is None:
            self.msg.Text = (head + '\r\n\r\n' if head else '') + 'Not run yet - double-click the check or press F5.' + tail
            return
        self.msg.Text = u'%s: %s\r\n%s%s' % (res.status, res.message, head, tail)
        self.dv.BeginUpdate()
        if res.groups and self.chk_group.Checked:
            # one row per family, with the number of instances
            for label, ids, extra in res.groups[:MAX_ROWS]:
                li = WF.ListViewItem('%s     (%d instance(s))' % (label, len(ids)))
                li.SubItems.Add(str(eid_val(ids[0])) + ('...' if len(ids) > 1 else ''))
                note = None
                for i_ in ids:
                    note = self.fail_notes.get(eid_val(i_))
                    if note:
                        break
                if note:
                    li.SubItems.Add('FIX FAILED: ' + note)
                    li.BackColor = STATUS_COLORS['FAIL']
                else:
                    li.SubItems.Add(extra or '')
                li.Tag = list(ids)
                self.dv.Items.Add(li)
        else:
            for text, eid, extra in res.items[:MAX_ROWS]:
                li = WF.ListViewItem(text)
                li.SubItems.Add(str(eid_val(eid)) if eid is not None else '')
                note = self.fail_notes.get(eid_val(eid)) if eid is not None else None
                if note:
                    li.SubItems.Add('FIX FAILED: ' + note)
                    li.BackColor = STATUS_COLORS['FAIL']
                else:
                    li.SubItems.Add(extra or '')
                li.Tag = eid
                self.dv.Items.Add(li)
            if len(res.items) > MAX_ROWS:
                self.dv.Items.Add(WF.ListViewItem('... %d more (see CSV export)' % (len(res.items) - MAX_ROWS)))
        self.dv.EndUpdate()
        if res.items:
            fixers = fixers_for(chk)
            if any(fx.destructive for fx in fixers):
                self.fix_panel.Controls.Add(self.chk_delete)
            allowed = SETTINGS.get('allow_delete', False)
            for fx in fixers:
                b = WF.Button()
                locked = fx.destructive and not allowed
                b.Text = 'Fix: ' + fx.label + ('   [locked]' if locked else '')
                b.AutoSize = True
                b.Enabled = not locked
                b.Click += (lambda s_, e_, fx_=fx: self.on_fix(chk, fx_))
                self.fix_panel.Controls.Add(b)
            self.fix_panel.Visible = self.fix_panel.Controls.Count > 0

    def log_history(self):
        """Record the score when every ticked check has a result (a complete picture)."""
        try:
            main = [c for c in self.checks if not c.extra]
            if not main or any(c.checked and id(c) not in self.results for c in main):
                return
            t = tally_checks(self.results, self.checks)
            p = score_pct(t)
            if p is None:
                return
            sig = (self.model_title, p, t['pass'], t['fail'], t['cl'], t['notrun'])
            if sig == self._last_logged:
                return
            self._last_logged = sig
            append_history(self.model_title, t, p)
        except Exception:
            pass

    def on_history(self, s, e):
        show_score_history(self.model_title)

    def on_open_log(self, s, e):
        try:
            path = os.path.join(data_dir(), 'FixLog_%s.csv' % file_safe(self.model_title))
            os.startfile(path if os.path.exists(path) else data_dir())
        except Exception as ex:
            WF.MessageBox.Show('Could not open the log: %s' % ex, 'Model Health Check')

    def on_delete_toggled(self, s, e):
        SETTINGS['allow_delete'] = bool(self.chk_delete.Checked)
        self.persist()
        self.show_details()

    def picked_ids(self, selected_only=True):
        def flat(tag):
            if tag is None:
                return []
            if isinstance(tag, (list, tuple)):
                return list(tag)
            return [tag]
        ids = []
        for li in self.dv.SelectedItems:
            ids.extend(flat(li.Tag))
        if not ids and not selected_only:
            for li in self.dv.Items:
                ids.extend(flat(li.Tag))
        return ids

    def on_item_selected(self, s, e):
        if self.chk_auto.Checked and not self.busy:
            ids = self.picked_ids()
            if ids:
                self.post(lambda: self.reveal(ids, False))

    def on_select_clicked(self, s, e):
        ids = self.picked_ids(False)
        if ids:
            self.post(lambda: self.reveal(ids, False))

    def on_zoom_clicked(self, s, e):
        ids = self.picked_ids(False)
        if ids:
            self.post(lambda: self.reveal(ids, True))

    def reveal(self, ids, zoom):
        """Runs inside a job: select (and optionally zoom to) elements."""
        self.sync_model()
        if doc is None:
            return
        els = [doc.GetElement(i) for i in ids]
        els = [x for x in els if x is not None]
        # a single view / sheet: open it
        if len(els) == 1 and isinstance(els[0], DB.View) and not els[0].IsTemplate:
            uidoc.ActiveView = els[0]
            return
        lst = List[DB.ElementId]()
        for x in els:
            if not isinstance(x, DB.View):
                lst.Add(x.Id)
        if lst.Count == 0:
            return
        try:
            uidoc.Selection.SetElementIds(lst)
            if zoom:
                uidoc.ShowElements(lst)
        except Exception:
            self.lbl_sum.Text = 'These elements cannot be selected in the current view'

    # ------------------------------------------------------------ fixing
    def on_fix(self, chk, fixer):
        if self.busy:
            return
        res = self.results.get(id(chk))
        if res is None:
            return
        ids = self.picked_ids(True)
        if not ids:
            ids = [i for _, i, _ in res.items if i is not None]
        if not ids:
            return
        self.set_busy(True)
        self.post(lambda: self.do_fix(chk, fixer, ids))

    def do_fix(self, chk, fixer, ids):
        """Runs inside a job (valid Revit API context)."""
        try:
            self.sync_model()
            if fixer.destructive and not SETTINGS.get('allow_delete', False):
                WF.MessageBox.Show('Delete fixes are locked. Tick "Allow delete fixes" first.', 'Model Health Check')
                return
            live = [i for i in ids if doc is not None and doc.GetElement(i) is not None]
            if not live:
                WF.MessageBox.Show('Nothing to fix - the elements no longer exist.', 'Model Health Check')
                return
            extra = None
            if fixer.ask is not None:
                extra = fixer.ask(live)
                if extra is None:
                    return
            ungroup_yes = False
            if fixer.group_sensitive:
                groups = {}
                for i in live:
                    g = group_of(doc.GetElement(i))
                    if g is not None:
                        k = eid_val(g.Id)
                        if k not in groups:
                            groups[k] = [g, 0]
                        groups[k][1] += 1
                if groups:
                    ans = ask_ungroup(groups)
                    if ans == 'cancel':
                        return
                    ungroup_yes = (ans == 'yes')
            if fixer.confirm:
                text = fixer.confirm.replace('{n}', str(len(live)))
                if ungroup_yes:
                    text += '\r\n\r\nThe groups listed earlier will be ungrouped first.'
                if fixer.plan is not None:
                    lines = fixer.plan(live, extra)
                    text += '\r\n\r\n' + '\r\n'.join(lines[:15])
                    if len(lines) > 15:
                        text += '\r\n... and %d more' % (len(lines) - 15)
                if WF.MessageBox.Show(text, 'Model Health Check - ' + fixer.label,
                                      WF.MessageBoxButtons.OKCancel,
                                      WF.MessageBoxIcon.Warning) != WF.DialogResult.OK:
                    return
            FIX_EXTRA_NOTES.clear()
            FIX_CHANGES[:] = []
            pre = FailLog()
            t = DB.Transaction(doc, 'Model Health Check: ' + fixer.label)
            t.Start()
            opts = t.GetFailureHandlingOptions()
            opts.SetFailuresPreprocessor(pre)
            t.SetFailureHandlingOptions(opts)
            failures = []
            summary = ''
            try:
                todo = live
                n_ungrouped = 0
                pre_failures = []
                if ungroup_yes:
                    self.set_progress('Ungrouping...')
                    n_ungrouped, pre_failures, failed_ids = ungroup_for(live)
                    todo = [i for i in live if eid_val(i) not in failed_ids]
                self.set_progress('Applying fix: ' + fixer.label)
                if todo:
                    summary, failures = fixer.fn(todo, extra)
                else:
                    summary, failures = 'Nothing left to change', []
                failures = pre_failures + failures
                if n_ungrouped:
                    summary = '%d group(s) ungrouped. %s' % (n_ungrouped, summary)
                status = t.Commit()
            except Exception:
                t.RollBack()
                raise
            self.fail_notes = {}
            if status != DB.TransactionStatus.Committed:
                why = '; '.join(pre.messages) if pre.messages else 'status: %s' % status
                show_fix_report('Model Health Check - fix rolled back',
                                'Revit rejected the change, so NOTHING was changed.',
                                [(None, '(whole fix)', why)])
            else:
                for e_, d_, r_ in failures:
                    if e_ is not None:
                        self.fail_notes[eid_val(e_)] = r_
                for k_, v_ in FIX_EXTRA_NOTES.items():
                    self.fail_notes[k_] = v_
                if pre.messages:
                    summary += '\r\nRevit warnings: ' + '; '.join(pre.messages[:3])
                logfile = write_fix_log(self.model_title, chk.name, fixer.label, summary, list(FIX_CHANGES), failures)
                if logfile:
                    summary += '\r\nLogged to: ' + logfile
                show_fix_report('Model Health Check - fix report', summary, failures)
        finally:
            self.set_busy(False)
        # re-run only this check so the list shows what is left
        self.set_busy(True)
        self.do_run([chk])

    # ------------------------------------------------------------ export
    def on_export(self, s, e):
        if not self.results:
            WF.MessageBox.Show('Run at least one check first.', 'Model Health Check')
            return
        dlg = WF.SaveFileDialog()
        dlg.Filter = 'CSV (*.csv)|*.csv'
        if SETTINGS.get('export_dir') and os.path.isdir(SETTINGS['export_dir']):
            dlg.InitialDirectory = SETTINGS['export_dir']
        title = os.path.splitext(self.model_title or 'model')[0]
        dlg.FileName = 'ModelHealthCheck_%s_%s.csv' % (re.sub(r'[^\w\-]+', '_', title), time.strftime('%Y%m%d_%H%M'))
        if dlg.ShowDialog() != WF.DialogResult.OK:
            return
        SETTINGS['export_dir'] = os.path.dirname(dlg.FileName)
        lines = [','.join(csv_cell(h) for h in
                          ('Priority', 'Section', 'Check', 'Status', 'Message', 'Item', 'ElementId', 'Info'))]
        for chk in self.checks:
            res = self.results.get(id(chk))
            if res is None:
                continue
            if not res.items:
                lines.append(','.join(csv_cell(x) for x in
                                      (chk.heading, chk.section, chk.name, res.status, res.message, '', '', '')))
            for text, eid, extra in res.items:
                lines.append(','.join(csv_cell(x) for x in
                                      (chk.heading, chk.section, chk.name, res.status, res.message,
                                       text, eid_val(eid) if eid is not None else '', extra)))
        System.IO.File.WriteAllText(dlg.FileName, '\r\n'.join(lines), System.Text.UTF8Encoding(True))
        self.lbl_sum.Text = 'Saved: ' + dlg.FileName



def _guard_class(cls):
    import types
    for name in list(cls.__dict__.keys()):
        if name.startswith('__'):
            continue
        member = cls.__dict__[name]
        if isinstance(member, types.FunctionType):
            setattr(cls, name, _guard(member))


_guard_class(HealthForm)
_guard_class(_Handler)
_guard_class(FailLog)

def main():
    SETTINGS.update(load_settings())
    existing = System.AppDomain.CurrentDomain.GetData(APPDOMAIN_KEY)
    try:
        if existing is not None and not existing.IsDisposed:
            existing.WindowState = WF.FormWindowState.Normal
            existing.Activate()
            existing.BringToFront()
            return
    except Exception:
        pass
    if doc is None:
        WF.MessageBox.Show('Open a model first.', 'Model Health Check')
        return
    handler = _Handler()
    event = UI.ExternalEvent.Create(handler)
    form = HealthForm(handler, event)
    System.AppDomain.CurrentDomain.SetData(APPDOMAIN_KEY, form)
    try:
        form.Show(RevitWindow(System.Diagnostics.Process.GetCurrentProcess().MainWindowHandle))
    except Exception:
        form.Show()
    _snapshot()


if __name__ == '__main__':
    main()
