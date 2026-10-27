"""Single import point for the Qt binding.

pyqtgraph picks whichever binding is installed (PyQt5 on Raspberry Pi OS, PyQt6 or PySide6
on a development PC) and normalises ``Signal``/``Slot`` names. All PiSDR modules import
Qt from here so the binding can be swapped without touching the rest of the code. Enums are
always used in their scoped form (``Qt.AlignmentFlag.AlignCenter``) which both PyQt5 >= 5.15
and PyQt6 accept.
"""
from pyqtgraph.Qt import QT_LIB, QtCore, QtGui, QtWidgets  # noqa: F401

Qt = QtCore.Qt
# Older pyqtgraph releases (e.g. the Debian-packaged one) alias Signal but not Slot.
Signal = getattr(QtCore, "Signal", None) or QtCore.pyqtSignal
Slot = getattr(QtCore, "Slot", None) or getattr(QtCore, "pyqtSlot", None)
QObject = QtCore.QObject
QTimer = QtCore.QTimer
