#!/usr/bin/env python3
"""
Initializes a project created from the template:
- sets the metadata of kibot_yaml/kibot_main.yaml (PROJECT_NAME, BOARD_NAME,
  COMPANY, DESIGNER)
- optionally renames the KiCad project files (--name), updating the references
  inside the project, schematics and PCB.

Values not given on the command line are asked interactively (when possible).

Usage: init_project.py [--project TEXT] [--board TEXT] [--company TEXT]
                       [--designer TEXT] [--name NEW_FILE_NAME] [-y]
"""
import argparse
import glob
import os
import re
import sys

MAIN = 'kibot_yaml/kibot_main.yaml'
FIELDS = (('project', 'PROJECT_NAME', 'Project name'),
          ('board', 'BOARD_NAME', 'Board name'),
          ('company', 'COMPANY', 'Company'),
          ('designer', 'DESIGNER', 'Designer'))


def current(text, key):
    m = re.search(r'^  {}: (.*)$'.format(key), text, re.M)
    return m.group(1).strip().strip('\'"') if m else ''


def quote(value):
    return "'" + value.replace("'", "''") + "'"


def rename_project(new):
    pros = glob.glob('*.kicad_pro')
    if len(pros) != 1:
        print('Error: expected one .kicad_pro, found {}'.format(pros), file=sys.stderr)
        return 1
    old = pros[0][:-len('.kicad_pro')]
    if old == new:
        return 0
    if not re.match(r'^[\w.-]+$', new):
        print('Error: use only letters, digits, `.`, `_` and `-` in the file name', file=sys.stderr)
        return 1
    for ext in ('kicad_pro', 'kicad_sch', 'kicad_pcb', 'kicad_dru', 'kicad_prl'):
        src = '{}.{}'.format(old, ext)
        if os.path.isfile(src):
            os.rename(src, '{}.{}'.format(new, ext))
    # References to the project name inside the files
    for f in glob.glob('*.kicad_sch') + glob.glob('*.kicad_pro') + glob.glob('*.kicad_pcb'):
        with open(f, encoding='utf-8') as fh:
            text = fh.read()
        if f.endswith('.kicad_pro'):
            # File names stored in the project (netlist, STEP, BoM...)
            new_text = re.sub(r'\b{}(?=[.-])'.format(re.escape(old)), new, text)
        else:
            new_text = text.replace('(project "{}"'.format(old), '(project "{}"'.format(new))
        if new_text != text:
            with open(f, 'w', encoding='utf-8', newline='\n') as fh:
                fh.write(new_text)
    print('Renamed the project files: {} -> {}'.format(old, new))
    return 0


def main():
    parser = argparse.ArgumentParser(description='Initialize a project created from the template')
    for arg, _, label in FIELDS:
        parser.add_argument('--' + arg, help=label)
    parser.add_argument('--name', help='New name for the KiCad project files (i.e. my_board)')
    parser.add_argument('-y', '--yes', action='store_true', help="Don't ask, keep the current values")
    args = parser.parse_args()
    interactive = sys.stdin.isatty() and not args.yes

    with open(MAIN, encoding='utf-8') as f:
        text = f.read()
    for arg, key, label in FIELDS:
        value = getattr(args, arg)
        cur = current(text, key)
        if value is None and interactive:
            value = input('{} [{}]: '.format(label, cur)).strip() or None
        if value is not None:
            text = re.sub(r'^(  {}: ).*$'.format(key), lambda m: m.group(1) + quote(value), text, count=1,
                          flags=re.M)
            print('{}: {}'.format(key, value))
    with open(MAIN, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)

    name = args.name
    if name is None and interactive:
        pros = glob.glob('*.kicad_pro')
        cur = pros[0][:-len('.kicad_pro')] if pros else ''
        name = input('KiCad project file name [{}]: '.format(cur)).strip() or None
    if name:
        return rename_project(name)
    return 0


if __name__ == '__main__':
    sys.exit(main())
