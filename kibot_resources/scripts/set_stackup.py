#!/usr/bin/env python3
"""
Applies a stackup profile (kibot_resources/stackups/*.yaml) to a KiCad 10
project:
- PCB: number of copper layers, copper layer names/types and physical stackup
  (thicknesses, materials, dielectric constants, finish, colors)
- project: impedance net classes, Board Setup minimums, via sizes, design
  rules file (.kicad_dru, only if not customized)
- impedance table of the fabrication document

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


def update_project(pro_file, profile, profiles_dir):
    """ Net classes, Board Setup rules and via sizes of the .kicad_pro """
    with open(pro_file, encoding='utf-8') as f:
        pro = json.load(f)
    msgs = []
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
    # Design rules: replace the project rules only if they are one of the profiles files (not customized)
    rules = profile.get('design_rules')
    if rules:
        src = os.path.join(profiles_dir, rules)
        dru = pro_file[:-len('.kicad_pro')] + '.kicad_dru'
        known = [os.path.join(profiles_dir, f) for f in os.listdir(profiles_dir) if f.endswith('.kicad_dru')]
        if not os.path.isfile(dru) or any(filecmp.cmp(dru, k, shallow=False) for k in known):
            shutil.copyfile(src, dru)
            msgs.append('design rules: {}'.format(rules))
        elif not filecmp.cmp(dru, src, shallow=False):
            msgs.append('WARNING: {} was customized, kept it. Compare it with {}'.format(
                os.path.basename(dru), src))
    return msgs

COPPER_RE = re.compile(r'^(F|B|In\d+)\.Cu$')


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
    layer('F.SilkS', [('type', '"Top Silk Screen"'), ('color', '"{}"'.format(profile.get('silk_color', 'White'))),
                      ('material', '"Direct Printing"')])
    layer('F.Paste', [('type', '"Top Solder Paste"')])
    layer('F.Mask', [('type', '"Top Solder Mask"'), ('color', '"{}"'.format(profile.get('mask_color', 'Green')))] + mask)
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
    layer('B.Mask', [('type', '"Bottom Solder Mask"'), ('color', '"{}"'.format(profile.get('mask_color', 'Green')))] + mask)
    layer('B.Paste', [('type', '"Bottom Solder Paste"')])
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


def main():
    parser = argparse.ArgumentParser(description='Apply a stackup profile to a KiCad 10 PCB')
    parser.add_argument('profile', nargs='?', help='Stackup profile (YAML file or name in the stackups dir)')
    parser.add_argument('-b', '--board', help='PCB file (default: the only *.kicad_pcb in the current dir)')
    parser.add_argument('-d', '--dir', default='kibot_resources/stackups', help='Stackup profiles dir')
    parser.add_argument('--force', action='store_true', help='Remove copper layers even if they are used')
    parser.add_argument('--list', action='store_true', help='List the available profiles')
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

    thickness = sum(float(la['thickness']) for la in profile['layers']) + 2 * float(profile.get('mask_thickness', 0.01))
    text = re.sub(r'(\n\t\(general\n\t\t\(thickness )[\d.]+\)', r'\g<1>{})'.format(fmt(round(thickness, 4))), text, count=1)

    with open(board, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    print('{}: applied {} ({}, {} copper layers, {} mm)'.format(board, profile.get('name', profile_file),
                                                               profile.get('description', ''), n, fmt(round(thickness, 3))))

    # Impedance table of the fabrication document
    rows = profile.get('impedance')
    if rows:
        lines = ['Transmission Line, Impedance [ohms], Tolerance, Layer, Trace Width [mm], Gap [mm], Gap to GND [mm], '
                 'Ref. Layers']
        for r in rows:
            lines.append(', '.join(str(r.get(k, '-')) for k in ('line', 'impedance', 'tolerance', 'layer', 'width',
                                                                 'gap', 'gnd_gap', 'ref')))
        with open(args.impedance_table, 'w', encoding='utf-8', newline='\n') as f:
            f.write('\n'.join(lines) + '\n')
        print('{}: impedance table for {}'.format(args.impedance_table, profile.get('name', profile_file)))

    # Project: net classes, Board Setup rules, via sizes, design rules
    pro_file = board[:-len('.kicad_pcb')] + '.kicad_pro'
    if os.path.isfile(pro_file):
        for msg in update_project(pro_file, profile, os.path.dirname(os.path.abspath(profile_file))):
            print('{}: {}'.format(pro_file, msg))
    return 0


if __name__ == '__main__':
    sys.exit(main())
