#!/usr/bin/env python3
"""
Applies a stackup profile (kibot_resources/stackups/*.yaml) to a KiCad 10
project:
- PCB: number of copper layers, copper layer names/types and physical stackup
  (thicknesses, materials, dielectric constants, finish, colors)
- zones refilled with the new rules (KiCad Python API, when available)
- project: net classes (impedance, or raised to the profile minimums), Board
  Setup minimums, via sizes, design rules file (.kicad_dru) and fabrication
  notes (both only if not customized)
- impedance table of the fabrication document
- CNC milling: user layers, project footprint libraries, manufacturing
  configuration files (never overwritten) and a fixture footprint (stock and
  registration pins) placed around the board

The KiCad Python API doesn't expose the stackup, so the .kicad_pcb file is
edited as text. Close the project in KiCad before running it.

Usage: set_stackup.py PROFILE.yaml [-b BOARD.kicad_pcb] [--force]
       set_stackup.py --list [-d STACKUPS_DIR]
"""
import argparse
import copy
import filecmp
import glob
import json
import os
import re
import shutil
import sys

import yaml

NETCLASS_FIELDS = ('track_width', 'clearance', 'diff_pair_width', 'diff_pair_gap', 'diff_pair_via_gap', 'via_diameter',
                   'via_drill')


def install_file(dst, profiles_dir, name, pattern, label):
    """ Copies a profile file (design rules, fabrication notes) to the project, unless the project one was
        customized (differs from all the profiles files matching the pattern) """
    src = os.path.join(profiles_dir, name)
    known = glob.glob(os.path.join(profiles_dir, pattern))
    if not os.path.isfile(dst) or any(filecmp.cmp(dst, k, shallow=False) for k in known):
        shutil.copyfile(src, dst)
        return '{}: {} ({})'.format(label, name, dst)
    if not filecmp.cmp(dst, src, shallow=False):
        return 'WARNING: {} was customized, kept it. Compare it with {}'.format(os.path.basename(dst), src)
    return None


def update_project(pro_file, profile, profiles_dir, fab_notes=None, paper_target=None):
    """ Net classes, Board Setup rules and via sizes of the .kicad_pro.
        paper_target='A3': if the PCB worksheet is named `..._A<n>.kicad_wks`, switch it to the matching
        `..._<paper_target>.kicad_wks` (when that file exists) so the title block fits the sheet. """
    with open(pro_file, encoding='utf-8') as f:
        pro = json.load(f)
    msgs = []
    if paper_target:
        wks = pro.get('pcbnew', {}).get('page_layout_descr_file')
        new_wks = re.sub(r'_A[0-5](\.kicad_wks)$', '_' + paper_target + r'\1', wks) if wks else None
        if new_wks and new_wks != wks:
            if os.path.isfile(os.path.join(os.path.dirname(pro_file), new_wks)):
                pro['pcbnew']['page_layout_descr_file'] = new_wks
                msgs.append('PCB worksheet {} -> {}'.format(os.path.basename(wks), os.path.basename(new_wks)))
            else:
                msgs.append('WARNING: sheet is {} but worksheet {} not found, kept {}'.format(
                    paper_target, os.path.basename(new_wks), os.path.basename(wks)))
    classes = pro.setdefault('net_settings', {}).setdefault('classes', [])
    default = next((c for c in classes if c.get('name') == 'Default'), None)
    for nc in profile.get('netclasses', []):
        cur = next((c for c in classes if c.get('name') == nc['name']), None)
        if cur is None:
            cur = copy.deepcopy(default) if default else {}
            cur['name'] = nc['name']
            if 'priority' in cur or default is None:
                used = [c.get('priority', 0) for c in classes if c.get('name') != 'Default']
                cur['priority'] = max(used) + 1 if used else 0
            classes.append(cur)
            action = 'created'
        else:
            action = 'updated'
        for k in NETCLASS_FIELDS:
            if k in nc:
                cur[k] = nc[k]
        msgs.append('net class {} {} (width {} mm{})'.format(
            nc['name'], action, nc.get('track_width'),
            ', gap {} mm'.format(nc['diff_pair_gap']) if 'diff_pair_gap' in nc else ''))
    # Other net classes raised to the process minimums (i.e. CNC milling)
    mins = profile.get('netclass_minimums', {})
    own = {nc['name'] for nc in profile.get('netclasses', [])}
    for c in classes:
        if c.get('name') in own:
            continue
        raised = []
        for k, v in mins.items():
            if isinstance(c.get(k), (int, float)) and c[k] < v:
                raised.append('{} {}->{}'.format(k, c[k], v))
                c[k] = v
        if raised:
            msgs.append('net class {} raised to the minimums: {}'.format(c.get('name'), ', '.join(raised)))
    ds = pro.setdefault('board', {}).setdefault('design_settings', {})
    if profile.get('board_rules'):
        ds.setdefault('rules', {}).update(profile['board_rules'])
        msgs.append('Board Setup constraints updated')
    if profile.get('via_sizes'):
        ds['via_dimensions'] = [{'diameter': 0.0, 'drill': 0.0}] + [{'diameter': d, 'drill': h}
                                                                   for d, h in profile['via_sizes']]
        msgs.append('via sizes: ' + ', '.join('{}/{}'.format(d, h) for d, h in profile['via_sizes']))
    with open(pro_file, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(pro, f, indent=2, ensure_ascii=False)
        f.write('\n')
    # Design rules and fabrication notes: replaced only if they are one of the profiles files (not customized)
    if profile.get('design_rules'):
        msgs.append(install_file(pro_file[:-len('.kicad_pro')] + '.kicad_dru', profiles_dir, profile['design_rules'],
                                 '*.kicad_dru', 'design rules'))
    if profile.get('fabrication_notes') and fab_notes:
        msgs.append(install_file(fab_notes, profiles_dir, profile['fabrication_notes'], 'fabrication_notes_*.txt',
                                 'fabrication notes'))
    return [m for m in msgs if m]

COPPER_RE = re.compile(r'^(F|B|In\d+)\.Cu$')


def refill_zones(board, pro_file):
    """ Refills the zones with the new rules, the gerbers use the stored fills """
    try:
        import pcbnew
    except ImportError:
        return 'KiCad Python API not available, refill the zones in KiCad (Edit > Fill All Zones, B)'
    # pcbnew rewrites the project file when saving the board, keep ours
    pro_text = None
    if os.path.isfile(pro_file):
        with open(pro_file, 'rb') as f:
            pro_text = f.read()
    try:
        pcb = pcbnew.LoadBoard(board)
        zones = pcb.Zones()
        if not len(zones):
            return None
        pcbnew.ZONE_FILLER(pcb).Fill(zones)
        pcb.Save(board)
    finally:
        if pro_text is not None:
            with open(pro_file, 'wb') as f:
                f.write(pro_text)
    return '{} zone(s) refilled'.format(len(zones))


def user_layer_id(name):
    """ KiCad 10 layer IDs: User.1=39, User.2=41... """
    return 37 + 2 * int(name[len('User.'):])


def add_user_layers(text, layers_pos, user_layers):
    """ Enables the profile user layers (`User.N: name`) in the layers block, returns (text, messages).
        A layer already enabled keeps its name, unless it is unnamed. """
    block = text[layers_pos[0]:layers_pos[1]]
    lines = block.split('\n')
    msgs = []
    for layer, name in user_layers.items():
        if not re.match(r'^User\.\d+$', layer):
            raise ValueError('user_layers: {} is not a User.N layer'.format(layer))
        idx = next((i for i, ln in enumerate(lines) if '"{}"'.format(layer) in ln), None)
        if idx is None:
            lines.insert(len(lines) - 1, '\t\t({} "{}" user "{}")'.format(user_layer_id(layer), layer, name))
            msgs.append('layer {} "{}" added'.format(layer, name))
        elif re.search(r'"{}" user\)'.format(re.escape(layer)), lines[idx]):
            lines[idx] = lines[idx].replace('user)', 'user "{}")'.format(name))
            msgs.append('layer {} named "{}"'.format(layer, name))
        elif '"{}"'.format(name) not in lines[idx]:
            msgs.append('WARNING: layer {} is already used as {}, not renamed to "{}"'.format(
                layer, re.findall(r'"([^"]+)"', lines[idx])[-1], name))
    new_block = '\n'.join(lines)
    return text[:layers_pos[0]] + new_block + text[layers_pos[1]:], msgs


def add_footprint_libraries(project_dir, libraries):
    """ Adds the profile footprint libraries (`nickname: path from the repository root`) to the project
        fp-lib-table, with a ${KIPRJMOD} relative path (works for every board of a multi-board repository) """
    table = os.path.join(project_dir, 'fp-lib-table')
    text = '(fp_lib_table\n\t(version 7)\n)\n'
    if os.path.isfile(table):
        with open(table, encoding='utf-8') as f:
            text = f.read()
    msgs = []
    for nick, path in libraries.items():
        if re.search(r'\(name "{}"\)'.format(re.escape(nick)), text):
            continue
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(project_dir)).replace(os.sep, '/')
        entry = '\t(lib (name "{}")(type "KiCad")(uri "${{KIPRJMOD}}/{}")(options "")(descr ""))\n'.format(nick, rel)
        end = text.rstrip().rfind(')')
        text = text[:end] + entry + text[end:]
        msgs.append('footprint library {} ({})'.format(nick, rel))
    if msgs:
        with open(table, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
    return ['{}: {}'.format(table, m) for m in msgs]


def install_manufacturing_files(src_dir, project_dir):
    """ Copies the profile manufacturing files to <project>/manufacturing, never overwriting a project file """
    dst_dir = os.path.join(project_dir, 'manufacturing')
    msgs = []
    for name in sorted(os.listdir(src_dir)):
        src, dst = os.path.join(src_dir, name), os.path.join(dst_dir, name)
        if os.path.isfile(src) and not os.path.exists(dst):
            os.makedirs(dst_dir, exist_ok=True)
            shutil.copyfile(src, dst)
            msgs.append('{} installed'.format(dst))
    return msgs


def place_fixture(board, pro_file, lib_path, lib_id):
    """ Places the fixture footprint (stock outline + registration pins, origin = stock lower-left corner)
        locked, with the stock centred on the board outline. Kept if the board already has one. """
    try:
        import pcbnew
    except ImportError:
        return 'KiCad Python API not available, place {} by hand'.format(lib_id)
    nick, name = lib_id.split(':', 1)
    pro_text = None
    if os.path.isfile(pro_file):
        with open(pro_file, 'rb') as f:
            pro_text = f.read()
    try:
        pcb = pcbnew.LoadBoard(board)
        for fp in pcb.GetFootprints():
            if fp.HasField('pcbcam_fixture'):
                return 'fixture footprint {} kept ({})'.format(fp.GetReference(), fp.GetFPIDAsString())
        fp = pcbnew.FootprintLoad(lib_path, name)
        if fp is None:
            return 'WARNING: footprint {} not found in {}'.format(name, lib_path)
        fp.SetFPID(pcbnew.LIB_ID(nick, name))
        # Stock size from the footprint graphics: the widest rectangle
        rects = [s for s in fp.GraphicalItems() if s.GetClass() == 'PCB_SHAPE' and s.GetShape() == pcbnew.SHAPE_T_RECT]
        w, h = max(((abs(s.GetEnd().x - s.GetStart().x), abs(s.GetEnd().y - s.GetStart().y)) for s in rects),
                   key=lambda wh: wh[0] * wh[1])
        edges = pcb.GetBoardEdgesBoundingBox()
        if edges.GetWidth() > 0 and edges.GetHeight() > 0:
            cx, cy = edges.GetCenter().x, edges.GetCenter().y
        else:
            cx, cy = pcb.GetDesignSettings().GetAuxOrigin().x, pcb.GetDesignSettings().GetAuxOrigin().y
        grid = pcbnew.FromMM(0.5)
        # Origin = stock lower-left corner, the stock extends up (-Y) and right
        x = round((cx - w / 2) / grid) * grid
        y = round((cy + h / 2) / grid) * grid
        fp.SetPosition(pcbnew.VECTOR2I(int(x), int(y)))
        fp.SetLocked(True)
        pcb.Add(fp)
        pcb.Save(board)
    finally:
        if pro_text is not None:
            with open(pro_file, 'wb') as f:
                f.write(pro_text)
    return 'fixture footprint {} placed, locked, stock centred on the board'.format(lib_id)


def copper_names(n):
    """ KiCad names for a board with n copper layers, top to bottom """
    return ['F.Cu'] + ['In{}.Cu'.format(i) for i in range(1, n - 1)] + ['B.Cu']


def copper_id(name):
    """ KiCad 10 layer IDs: F.Cu=0, B.Cu=2, In1.Cu=4, In2.Cu=6... """
    if name == 'F.Cu':
        return 0
    if name == 'B.Cu':
        return 2
    return 2 + 2 * int(name[2:-3])


def fmt(v):
    return '{:g}'.format(round(float(v), 6))


def find_block(text, header, indent):
    """ Returns (start, end) of the block starting with `indent(header` up to its `indent)` line """
    m = re.search(r'^' + indent + re.escape('(' + header) + r'\b', text, re.M)
    if not m:
        return None
    end = text.find('\n' + indent + ')', m.end())
    if end < 0:
        return None
    return m.start(), end + len(indent) + 2


def build_layers(old_block, names, profile_coppers):
    lines = old_block.split('\n')
    head, body, tail = lines[0], lines[1:-1], lines[-1]
    others = [ln for ln in body if not COPPER_RE.match(re.search(r'"([^"]+)"', ln).group(1))]
    new = []
    for name, cu in zip(names, profile_coppers):
        new.append('\t\t({} "{}" {} "{}")'.format(copper_id(name), name, cu.get('type', 'signal'), cu['copper']))
    return '\n'.join([head] + new + others + [tail])


def build_stackup(profile, names, dielectric_constraints):
    t = '\t\t\t'
    out = ['\t\t(stackup']

    def layer(name, props):
        out.append(t + '(layer "{}"'.format(name))
        for k, v in props:
            out.append(t + '\t({} {})'.format(k, v))
        out.append(t + ')')

    mask = [('thickness', fmt(profile.get('mask_thickness', 0.01))), ('material', '"Solder Resist"'),
            ('epsilon_r', fmt(profile.get('mask_epsilon_r', 3.8))), ('loss_tangent', '0')]
    silk = profile.get('silkscreen', True)
    has_mask = profile.get('solder_mask', True)
    if silk:
        layer('F.SilkS', [('type', '"Top Silk Screen"'), ('color', '"{}"'.format(profile.get('silk_color', 'White'))),
                          ('material', '"Direct Printing"')])
    layer('F.Paste', [('type', '"Top Solder Paste"')])
    if has_mask:
        layer('F.Mask', [('type', '"Top Solder Mask"'), ('color', '"{}"'.format(profile.get('mask_color', 'Green')))] +
              mask)
    cu_index = 0
    diel_index = 0
    for item in profile['layers']:
        if 'copper' in item:
            layer(names[cu_index], [('type', '"copper"'), ('thickness', fmt(item['thickness']))])
            cu_index += 1
        else:
            diel_index += 1
            layer('dielectric {}'.format(diel_index),
                  [('type', '"{}"'.format(item.get('dielectric', 'core'))), ('color', '"FR4 natural"'),
                   ('thickness', fmt(item['thickness'])), ('material', '"{}"'.format(item.get('material', 'FR4'))),
                   ('epsilon_r', fmt(item.get('epsilon_r', 4.5))), ('loss_tangent', fmt(item.get('loss_tangent', 0.02)))])
    if has_mask:
        layer('B.Mask', [('type', '"Bottom Solder Mask"'), ('color', '"{}"'.format(profile.get('mask_color', 'Green')))] +
              mask)
    layer('B.Paste', [('type', '"Bottom Solder Paste"')])
    if silk:
        layer('B.SilkS', [('type', '"Bottom Silk Screen"'), ('color', '"{}"'.format(profile.get('silk_color', 'White'))),
                          ('material', '"Direct Printing"')])
    out.append(t + '(copper_finish "{}")'.format(profile.get('copper_finish', 'HAL lead-free')))
    out.append(t + '(dielectric_constraints {})'.format(dielectric_constraints))
    out.append('\t\t)')
    return '\n'.join(out)


def list_profiles(directory):
    for f in sorted(glob.glob(os.path.join(directory, '*.yaml'))):
        with open(f) as fh:
            p = yaml.safe_load(fh)
        n = sum(1 for la in p['layers'] if 'copper' in la)
        print('{:<16} {} copper layers  {:<18} {}'.format(os.path.splitext(os.path.basename(f))[0], n, p.get('name', ''),
                                                           p.get('description', '')))


PAPER_RANK = {'A5': 0, 'A4': 1, 'A3': 2, 'A2': 3, 'A1': 4, 'A0': 5}


def ensure_paper(text, target):
    """ Enlarge the drawing sheet to `target` (an A-series size) if it is currently smaller.
        Never shrinks, and leaves custom/unknown sizes (e.g. `(paper "User" w h)`) alone.
        Returns (new_text, message, old_size); old_size is None when nothing changed. """
    if target not in PAPER_RANK:
        return text, None, None
    m = re.search(r'\(paper "([^"]+)"', text)
    if not m:
        return text, None, None
    cur = m.group(1)
    if cur not in PAPER_RANK or PAPER_RANK[cur] >= PAPER_RANK[target]:
        return text, None, None
    return text[:m.start(1)] + target + text[m.end(1):], 'drawing sheet {} -> {}'.format(cur, target), cur


def main():
    parser = argparse.ArgumentParser(description='Apply a stackup profile to a KiCad 10 PCB')
    parser.add_argument('profile', nargs='?', help='Stackup profile (YAML file or name in the stackups dir)')
    parser.add_argument('-b', '--board', help='PCB file (default: the only *.kicad_pcb in the current dir)')
    parser.add_argument('-d', '--dir', default='kibot_resources/stackups', help='Stackup profiles dir')
    parser.add_argument('--force', action='store_true', help='Remove copper layers even if they are used')
    parser.add_argument('--list', action='store_true', help='List the available profiles')
    parser.add_argument('--no-fill', action='store_true', help="Don't refill the zones")
    parser.add_argument('--fab-notes', default='kibot_resources/templates/fabrication_notes.txt',
                        help='Fabrication notes replaced by the profile ones (`fabrication_notes` entry)')
    parser.add_argument('--impedance-table', default='kibot_resources/templates/impedance_table.txt',
                        help='Impedance table written from the profile (`impedance` entry)')
    args = parser.parse_args()

    if args.list or not args.profile:
        list_profiles(args.dir)
        return 0

    profile_file = args.profile
    if not os.path.isfile(profile_file):
        profile_file = os.path.join(args.dir, args.profile + '.yaml')
    with open(profile_file) as f:
        profile = yaml.safe_load(f)

    board = args.board
    if not board:
        boards = [b for b in glob.glob('*.kicad_pcb') if not re.match(r'kibot_.{8}\.kicad_pcb$', b)]
        if len(boards) != 1:
            print('Error: specify the PCB with -b, found: {}'.format(boards), file=sys.stderr)
            return 1
        board = boards[0]
    with open(board, encoding='utf-8') as f:
        text = f.read()

    coppers = [la for la in profile['layers'] if 'copper' in la]
    n = len(coppers)
    if n < 2 or n % 2:
        print('Error: the profile must have an even number of copper layers (got {})'.format(n), file=sys.stderr)
        return 1
    names = copper_names(n)

    layers_pos = find_block(text, 'layers', '\t')
    stackup_pos = find_block(text, 'stackup', '\t\t')
    if not layers_pos or not stackup_pos:
        print('Error: layers/stackup sections not found in {}'.format(board), file=sys.stderr)
        return 1

    # Refuse to remove inner layers that are in use
    used = set()
    for s, e in ((0, layers_pos[0]), (layers_pos[1], stackup_pos[0]), (stackup_pos[1], len(text))):
        used.update(int(x) for x in re.findall(r'"In(\d+)\.Cu"', text[s:e]))
    removed = sorted(i for i in used if i > n - 2)
    if removed and not args.force:
        print('Error: inner layers {} are used in the board, they would be removed. Move the items or use --force'.
              format(', '.join('In{}.Cu'.format(i) for i in removed)), file=sys.stderr)
        return 2

    old_stackup = text[stackup_pos[0]:stackup_pos[1]]
    m = re.search(r'\(dielectric_constraints (\w+)\)', old_stackup)
    new_stackup = build_stackup(profile, names, m.group(1) if m else 'no')
    new_layers = build_layers(text[layers_pos[0]:layers_pos[1]], names, coppers)
    # Replace from the end to keep the positions valid
    text = text[:stackup_pos[0]] + new_stackup + text[stackup_pos[1]:]
    text = text[:layers_pos[0]] + new_layers + text[layers_pos[1]:]
    layer_msgs = []
    if profile.get('user_layers'):
        text, layer_msgs = add_user_layers(text, find_block(text, 'layers', '	'), profile['user_layers'])

    thickness = sum(float(la['thickness']) for la in profile['layers'])
    if profile.get('solder_mask', True):
        thickness += 2 * float(profile.get('mask_thickness', 0.01))
    text = re.sub(r'(\n\t\(general\n\t\t\(thickness )[\d.]+\)', r'\g<1>{})'.format(fmt(round(thickness, 4))), text, count=1)

    # Drawing sheet: enlarge to at least `min_paper` (the fixture stock needs the room)
    paper_target = None
    paper_msg = None
    if profile.get('min_paper'):
        text, paper_msg, _ = ensure_paper(text, profile['min_paper'])
        m = re.search(r'\(paper "([^"]+)"', text)
        paper_target = m.group(1) if m else None

    with open(board, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    print('{}: applied {} ({}, {} copper layers, {} mm)'.format(board, profile.get('name', profile_file),
                                                               profile.get('description', ''), n, fmt(round(thickness, 3))))
    if paper_msg:
        print('{}: {}'.format(board, paper_msg))
    for msg in layer_msgs:
        print('{}: {}'.format(board, msg))

    # Impedance table of the fabrication document
    rows = profile.get('impedance')
    if rows is not None:
        lines = ['Transmission Line, Impedance [ohms], Tolerance, Layer, Trace Width [mm], Gap [mm], Gap to GND [mm], '
                 'Ref. Layers']
        if not rows:
            lines.append('No controlled impedance, -, -, -, -, -, -, -')
        for r in rows:
            lines.append(', '.join(str(r.get(k, '-')) for k in ('line', 'impedance', 'tolerance', 'layer', 'width',
                                                                 'gap', 'gnd_gap', 'ref')))
        with open(args.impedance_table, 'w', encoding='utf-8', newline='\n') as f:
            f.write('\n'.join(lines) + '\n')
        print('{}: impedance table for {}'.format(args.impedance_table, profile.get('name', profile_file)))

    # Project: net classes, Board Setup rules, via sizes, design rules
    pro_file = board[:-len('.kicad_pcb')] + '.kicad_pro'
    if os.path.isfile(pro_file):
        for msg in update_project(pro_file, profile, os.path.dirname(os.path.abspath(profile_file)), args.fab_notes,
                                  paper_target):
            print('{}: {}'.format(pro_file, msg))

    # CNC milling (pcbcam): footprint libraries, manufacturing configuration, fixture footprint
    project_dir = os.path.dirname(os.path.abspath(board))
    profiles_dir = os.path.dirname(os.path.abspath(profile_file))
    libraries = profile.get('footprint_libraries', {})
    for msg in add_footprint_libraries(project_dir, libraries):
        print(msg)
    if profile.get('manufacturing_files'):
        for msg in install_manufacturing_files(os.path.join(profiles_dir, profile['manufacturing_files']), project_dir):
            print(msg)
    if profile.get('fixture_footprint'):
        nick = profile['fixture_footprint'].split(':', 1)[0]
        if nick not in libraries:
            print('Error: fixture_footprint {} is not in a footprint_libraries entry'.format(profile['fixture_footprint']),
                  file=sys.stderr)
            return 1
        print('{}: {}'.format(board, place_fixture(board, pro_file, os.path.abspath(libraries[nick]),
                                                   profile['fixture_footprint'])))

    # After the project update, so the fills use the new clearances
    if not args.no_fill:
        msg = refill_zones(board, pro_file)
        if msg:
            print('{}: {}'.format(board, msg))
    return 0


if __name__ == '__main__':
    sys.exit(main())
