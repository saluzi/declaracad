"""
Copyright (c) 2020, Jairus Martin.

Distributed under the terms of the GPL v3 License.

The full license is in the file LICENSE, distributed with this software.

Created on Aug 18, 2020

@author: jrm
"""
import os
import re
from collections import OrderedDict
from atom.api import (
    Atom, Property, List, Instance, Str, Int, Enum, Float, Bool, Dict, observe
)
from OCCT.BRep import BRep_Builder
from OCCT.TopoDS import TopoDS_Compound
from declaracad.occ.api import *


class Command(Atom):
    data = Instance(OrderedDict)
    comment = Str()
    source = Str()
    line = Int()

    def _get_id(self):
        if not self.data:
            return
        for k, v in self.data.items():
            vi = int(v)
            if v == vi:
                v = vi # Clip off .0
            return '{}{}'.format(k, v)

    id = Property(_get_id, cached=True)

    def _get_waypoint(self):
        d = self.data
        if not d:
            return
        axis = {}
        for k in GCode.AXIS:
            if k in d:
                axis[k] = d[k]
        if axis:
            return Waypoint(**axis)

    waypoint = Property(_get_waypoint, cached=True)

    def _get_feedrate(self):
        if not self.data:
            return
        return self.data.get('F')

    feedrate = Property(_get_feedrate, cached=True)

    def __repr__(self):
        return "Command<'{}' at line {}>".format(self.source, self.line)


class GCode(Atom):
    path = Str()
    commands = List(Command)

    AXIS = ('X', 'Y' 'Z' 'A' 'B' 'C' 'U' 'V' 'W')
    COLORMAP = {
        'G0': 'green',
        'G1': 'blue',
        'G2': 'green',
        'G3': 'green',
    }

    def __repr__(self):
        return "GCode<file='{} cmds=[\n    {}\n]>".format(
            self.path, ",\n    ".join(map(str, self.commands)))

    def max(self):
        """ Return max value of each axis """
        return Point(*(max(c.data[axis] for c in self.commands
                          if c.data and axis in c.data)
                     for axis in ('X', 'Y', 'Z')))
    def min(self):
        """ Return min value of each axis """
        return Point(*(min(c.data[axis] for c in self.commands
                          if c.data and axis in c.data)
                     for axis in ('X', 'Y', 'Z')))

def parse(path):
    """ Parse the file at the given path into a list of Commands

    Parameters
    ----------
    path: String
        The file path

    Notes
    -----
    This does not handle inline comments or multiple commands on a single line

    Returns
    -------
    gcode: GCode
        A GCode instance with the parsed commands

    """
    cmds = []
    with open(path) as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue

            # Strip comments
            parts = re.split(r';|\(|%', line, maxsplit=1)
            data = parts[0].strip()
            comment = "" if len(parts) == 1 else parts[1]
            if not data and not comment:
                continue

            cmd = Command(comment=comment, source=line, line=i)
            if data:
                try:
                    d = OrderedDict()
                    for c in re.findall(r'[A-z][\d.]+ *', data):
                        d[c[0].upper()] = float(c[1:])
                    cmd.data = d
                except ValueError as e:
                    filepath, filename = os.path.split(path)
                    msg = "Failed to parse '%s' at line %s: %s" % (
                        filename, i, e)
                    raise ValueError(msg)
            cmds.append(cmd)
    return GCode(path=path, commands=cmds)


class Waypoint(Atom):
    X = Float()
    Y = Float()
    Z = Float()
    A = Float()
    B = Float()
    C = Float()
    U = Float()
    V = Float()
    W = Float()


class CNC(Atom):
    """ A simulator

    """
    position = Instance(Waypoint, ())
    origin = Instance(Waypoint, ())
    stored_positions = Dict(int, Waypoint)

    rapid_feedrate = Float()
    move_feedrate = Float()

    cutter_compensation = Float()

    lathe_mode = Enum('radius', 'diameter')
    units = Enum('in', 'mm')
    plane = Enum('XY', 'ZX', 'YZ', 'UV', 'WU', 'VW')
    command = Instance(Command)
    last_command = Instance(Command)

    # Actions that ocurred
    actions = List()

    @observe('command', 'units', 'plane', 'position', 'origin',
             'stored_positions')
    def _update_action(self, change):
        """ Save what happens """
        self.actions.append(change)

    def rapid_to(self, pos):
        if not isinstance(pos, Waypoint):
            raise TypeError("Expected waypoint, got: %s" % pos)
        self.position = pos

    def move_to(self, pos):
        if not isinstance(pos, Waypoint):
            raise TypeError("Expected waypoint, got: %s" % pos)
        self.position = pos

    def dwell(self, P, **kwargs):
        # Dwell for P seconds
        if P is None or P < 0:
            msg = "Invalid dwell time %s" % P
            raise ValueError(msg)

    def cubic_to(self, **kwargs):
        pass

    def quad_to(self, **kwargs):
        pass


def load(path):
    """ Load GCode based on
    http://www.linuxcnc.org/docs/html/gcode/g-code.html#cha:g-codes

    """
    gcode = parse(path)

    # Split into separate wires
    wires = []

    cnc = State()
    toolpath = Part()
    for cmd in gcode.commands:
        cnc.command = cmd
        if not cmd.data:
            continue  # Comment

        pos = cnc.position
        d = cmd.data

        # Rapid move
        if cmd.id == 'G0':
            cnc.rapid_to(**cmd.data)

        # Linear move
        elif cmd.id == 'G1':
            cnc.move_to(**cmd.data)

        # Arc
        elif cmd.id in ('G2', 'G3'):
            cnc.arc_to(**cmd.data)

        # Dwell
        elif cmd.id == 'G4':
            cnc.dwell(**cmd.data)

        # Cubic spline
        elif cmd.id == 'G5':
            cnc.cubic_to(**cmd.data)

        # Quadratic spline
        elif cmd.id == 'G5.1':
            cnc.quad_to(**cmd.data)

        # Nurbs block
        elif cmd.id in ('G5.2', 'G5.3'):
            raise NotImplementedError

        # Lathe mode
        elif cmd.id == 'G7':
            cnc.lathe_mode = 'diameter'
        elif cmd.id == 'G8':
            cnc.lathe_mode = 'radius'

        # Set...
        elif cmd.id == 'G10':
            raise NotImplementedError

        # Plane select
        elif cmd.id == 'G17':
            cnc.plane = 'XY'
        elif cmd.id == 'G17.1':
            cnc.plane = 'UV'
        elif cmd.id == 'G18':
            cnc.plane = 'ZX'
        elif cmd.id == 'G18.1':
            cnc.plane = 'WU'
        elif cmd.id == 'G19':
            cnc.plane = 'YZ'
        elif cmd.id == 'G19.1':
            cnc.plane = 'VW'

        # Units
        elif cmd.id == 'G20':
            cnc.units = 'in'
        elif cmd.id == 'G21':
            cnc.units = 'mm'

        # Go/Set Predefined Position
        elif cmd.id == 'G28':
            if cmd.waypoint:
                cnc.rapid_to(cmd.waypoint)
            if cnc.stored_positions.get(0) is None:
                msg = "Attempted to restore unsaved position at %s" % cmd
                print(msg)
            cnc.rapid_to(cnc.stored_positions.get(0, cnc.origin_position))
        elif cmd.id == 'G28.1':
            cnc.stored_positions[0] = cnc.position
        elif cmd.id == 'G30':
            if cmd.waypoint:
                cnc.rapid_to(cmd.waypoint)
            if cnc.stored_positions.get(1) is None:
                msg = "Attempted to restore unsaved position at %s" % cmd
                print(msg)
            cnc.rapid_to(cnc.stored_positions.get(1, cnc.origin_position))
        elif cmd.id == 'G30.1':
            cnc.stored_positions[1] = cnc.position

        # Spindle Synchronized Motion
        elif cmd.id == 'G33':
            raise NotImplementedError

        # Rigid Tapping
        elif cmd.id == 'G33.1':
            raise NotImplementedError

        elif cmd.id in ('G33.2', 'G33.3', 'G33.4', 'G33.5'):
            raise NotImplementedError

        # Compensation Off
        elif cmd.id == 'G40':
            cnc.cutter_compensation = False
        cnc.last_command = cmd
    return toolpath


