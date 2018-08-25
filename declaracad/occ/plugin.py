# -*- coding: utf-8 -*-
"""
Copyright (c) 2017, Jairus Martin.

Distributed under the terms of the GPL v3 License.

The full license is in the file LICENSE, distributed with this software.

Created on Dec 13, 2017

@author: jrm
"""
import os
import sys
import json
import time
import enaml
import atexit
from types import ModuleType
from atom.api import (
    Atom, ContainerList, Unicode, Float, Dict, Bool, Int, Instance, observe
)
from declaracad.core.api import Plugin, Model, log
from declaracad.core.utils import ProcessLineReceiver, Deferred

from enaml.application import timed_call
from enaml.core.parser import parse
from enaml.core.import_hooks import EnamlCompiler
from enaml.compat import exec_

from .part import Part


def viewer_factory():
    with enaml.imports():
        from .view import ViewerDockItem
    return ViewerDockItem


def load_model(filename):
    """ Load a DeclaraCAD model from an enaml file. Ideally this should be
    used in a separate process but it's not required.
    
    Parameters
    ----------
    filename: String
        Path to the enaml file to load
        
    Returns
    -------
    result: List[occ.shape.Shape]
        A list of shapes that can be passed to the python-occ viewer.
    
    """
    
    # Parse the enaml file
    with open(filename, 'rU') as f:
        ast = parse(f.read())
        code = EnamlCompiler.compile(ast, filename)
    module = ModuleType(filename.rsplit('.', 1)[0])
    module.__file__ = filename
    namespace = module.__dict__
    with enaml.imports():
        exec_(code, namespace)
    Assembly = namespace['Assembly']
    return [Assembly()]


def export_model(options):
    """ Export a DeclaraCAD model from an enaml file to an STL based on the
    given options.
    
    Parameters
    ----------
    options: declaracad.occ.plugin.ExportOptions
    
    """
    print("Exporting {o.filename} to {o.path}...".format(o=options))
    sys.stdout.flush()
    if not isinstance(options, ExportOptions):
        raise TypeError("Expected ExportOptions got: {}".format(options))
    
    t0 = time.time()
    
    from OCC.TopoDS import TopoDS_Compound
    from OCC.BRep import BRep_Builder
    from OCC.StlAPI import StlAPI_Writer
    from OCC.BRepMesh import BRepMesh_IncrementalMesh
    
    exporter = StlAPI_Writer()
    exporter.SetASCIIMode(not options.binary)

    # Make a compound of compounds (if needed)
    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    
    # Load the enaml model file
    parts = load_model(options.filename)
    
    for part in parts:
        # Render the part from the declaration
        shape = part.render()
        
        # Must mesh the shape firsts
        if hasattr(shape, 'Shape'):
            builder.Add(compound, shape.Shape())
        else:
            builder.Add(compound, shape)

    #: Build the mesh
    mesh = BRepMesh_IncrementalMesh(
        compound,
        options.linear_deflection,
        options.relative,
        options.angular_deflection
    )
    mesh.Perform()
    if not mesh.IsDone():
        raise RuntimeError("Failed to create the mesh")

    exporter.Write(compound, options.path)
    if not os.path.exists(options.path):
        raise RuntimeError("Failed to write shape")
    print("Success! Took {} seconds.".format(round(time.time()-t0, 2)))


class ExportOptions(Atom):
    path = Unicode()
    filename = Unicode()
    linear_deflection = Float(0.05, strict=False)
    angular_deflection = Float(0.5, strict=False)
    relative = Bool()
    binary = Bool()
    
    def _default_path(self):
        return "{}.stl".format(os.path.splitext(self.filename)[0])
    
    def format(self):
        """ Return formatted option values for the exporter app to parse """
        return json.dumps(self.__getstate__())
    
    
class ScreenshotOptions(Atom):
    #: Path to save
    path = Unicode()
    
    #: Document file name
    filename = Unicode()
    
    #: Only screenshot this view
    target = Unicode()
    
    def _default_path(self):
        return "{}.png".format(os.path.splitext(self.filename)[0])
    
    def format(self):
        """ Return formatted option values for the exporter app to parse """
        return json.dumps(self.__getstate__())


class ViewerProcess(ProcessLineReceiver):
    #: Window id obtained after starting the process
    window_id = Int()
    
    #: Process handle
    process = Instance(object)
    
    #: Filename
    filename = Unicode()
    
    #: View version
    version = Int()
    
    #: Rendering error
    errors = Unicode()
    
    #: Process terminated intentionally
    terminated = Bool(False)
    
    #: Count restarts so we can detect issues with startup s
    restarts = Int()
    
    #: ID count
    _id = Int()
    
    #: Holds responses temporarily
    _responses = Dict()
    
    #: Seconds to ping
    _ping_rate = Int(40)
    
    @observe('filename', 'version')
    def _update_viewer(self, change):
        self.send_message(change['name'], change['value'], _id=None)
        
    def send_message(self, method, *args, **kwargs):
        # Defer until it's ready
        if not self.transport:
            log.debug('renderer | message not ready deferring')
            timed_call(0, self.send_message, method, *args, **kwargs)
            return
        _id = kwargs.pop('_id')
        
        request = {'jsonrpc': '2.0', 'method': method, 'params': args or kwargs}
        if _id is not None:
            request['id'] = _id        
        
        log.debug(f'renderer | sent | {request}')
        self.transport.write(json.dumps(request).encode()+b'\r\n')
    
    def _default_process(self):
        from twisted.internet import reactor
        exe = sys.executable
        atexit.register(self.terminate)
        return reactor.spawnProcess(self, exe, 
                                    args=[exe, 'main.py', 'view', '-', '-f'],
                                    env=os.environ)
    
    def _default_window_id(self):
        # Spawn the process 
        timed_call(0, lambda: self.process)
        return 0
    
    def restart(self):
        self.window_id = 0
        self.restarts += 1
        
        # TODO: 100 is probably excessive
        if self.restarts > 100:
            raise RuntimeError(
                "renderer | Failed to successfully start renderer aborting!")
            
        self.process = self._default_process()
    
    def connectionMade(self):
        super(ViewerProcess, self).connectionMade()
        self.schedule_ping()
        self.terminated = False
    
    def lineReceived(self, line):
        try:
            line = line.decode()
            log.debug(f"render | out | {line}")
            response = json.loads(line)
        except Exception as e:
            response = {}
        
        #: Special case for startup
        response_id = response.get('id')
        if response_id == 'window_id':
            self.window_id = response['result']
            self.restarts = 0  # Clear the restart count
        elif response_id == 'render_error':
            self.errors = response['error']['message']
        elif response_id == 'render_ok':
            self.errors = ""
        elif response_id == 'capture_output':
            # Script output capture it
            self.output = response['result'].split("\n")
        elif response_id is not None:
            # Lookup the deferred object that should be stored for this id
            # when it is called and invoke the callback or errback based on the
            # result
            d = self._responses.get(response_id)
            if d is not None:
                del self._responses[response_id]
                error = response.get('error')
                if error is not None:
                    d.errback(error)
                else:
                    d.callback(response.get('result'))
        else:
            # Append to output
            self.output.append(line)
                
    def errReceived(self, data):
        if b'XCB error' in data:
            return
        for line in data.split(b"\n"):
            try:
                log.debug(f"render | err | {line.encode()}")
            except:
                pass
    
    def inConnectionLost(self):
        log.warning("renderer | stdio closed (we probably did it)")
    
    def outConnectionLost(self):
        if not self.terminated:
            self.restart()
        log.warning("renderer | stdout closed")
    
    def errConnectionLost(self):
        log.warning("renderer | stderr closed")
        
    def processEnded(self, reason):
        super(ViewerProcess, self).processEnded(reason)
        log.warning(f"renderer | process ended: {reason}")
        
    def terminate(self):
        super(ViewerProcess, self).terminate()
        self.terminated = True
        
    def schedule_ping(self):
        """ Ping perioidcally so the process stays awake """
        if self.terminated:
            return 
        # Ping the viewer to tell it to keep alive
        self.send_message("ping", _id="keep_alive")
        timed_call(self._ping_rate*1000, self.schedule_ping)
        
    def __getattr__(self, name):
        """ Proxy all calls """
        def remote_viewer_call(*args, **kwargs):
            d = Deferred()
            self._id += 1
            kwargs['_id'] = self._id
            self._responses[self._id] = d
            self.send_message(name, *args, **kwargs)
            return d
        return remote_viewer_call

class ViewerPlugin(Plugin):
    
    def get_viewers(self):
        ViewerDockItem = viewer_factory()
        dock = self.workbench.get_plugin('declaracad.ui').get_dock_area()
        for item in dock.dock_items():
            if isinstance(item, ViewerDockItem):
                yield item

    def fit_all(self, event=None):
        return
        viewer = self.get_viewer()
        viewer.proxy.display.FitAll()

    def get_viewer(self, name=None):
        for viewer in self.get_viewers():
            if name is None:
                return viewer
            elif viewer.name == name:
                return viewer

    def export(self, event):
        """ Export the current model to stl """
        from twisted.internet import reactor
        
        if 'options' not in event.parameters:
            editor = self.workbench.get_plugin('declaracad.editor')
            options = ExportOptions(filename=editor.active_document.name)
        else:
            options = event.parameters.get('options')
        cmd = [sys.executable, 'main.py', 'export', options.format()]
        log.debug(" ".join(cmd))
        protocol = ProcessLineReceiver()
        reactor.spawnProcess(
            protocol, sys.executable, args=cmd, env=os.environ)
        return protocol
        
    def screenshot(self, event):
        """ Export the views as a screenshot """
        if 'options' not in event.parameters:
            editor = self.workbench.get_plugin('declaracad.editor')
            options = ScreenshotOptions(filename=editor.active_document.name)
        else:
            options = event.parameters.get('options')
        results = []
        if options.target:
            viewer = self.get_viewer(options.target)
            if viewer:
                results.append(viewer.renderer.screenshot(options.path))
        else:
            for i, viewer in enumerate(self.get_viewers()):
                # Insert view number
                path, ext = os.path.splitext(options.path)
                filename = "{}-{}{}".format(path, i+1, ext)
                results.append(viewer.renderer.screenshot(filename))
        return results
        
