import os
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon


class DroneCoregPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dialog = None

    def initGui(self):
        self.action = QAction("Drone Co-registration (AROSICS)", self.iface.mainWindow())
        self.action.setToolTip("Open the drone imagery co-registration tool")
        self.action.triggered.connect(self.run)
        self.iface.addPluginToRasterMenu("Drone Co-registration", self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        self.iface.removePluginRasterMenu("Drone Co-registration", self.action)
        self.iface.removeToolBarIcon(self.action)
        if self.dialog is not None:
            self.dialog.close()
            self.dialog = None

    def run(self):
        from .dialog import CoregDialog
        if self.dialog is None or not self.dialog.isVisible():
            self.dialog = CoregDialog(self.iface, parent=self.iface.mainWindow())
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
