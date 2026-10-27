"""Colours and the application stylesheet. Dark, high contrast, finger-sized targets."""

BG = "#0e1216"
PANEL = "#171d23"
PANEL_HI = "#222b34"
TEXT = "#e8ecef"
TEXT_DIM = "#8a949c"
ACCENT = "#ffb000"       # amber, used for the VFO and the active soft key
ACCENT_2 = "#39c0ff"     # cyan, passband and secondary highlights
REC = "#ff4040"
OK = "#5fd36a"

STYLESHEET = f"""
QWidget {{
    background: {BG};
    color: {TEXT};
    font-family: "DejaVu Sans", "Noto Sans", "Segoe UI", sans-serif;
    font-size: 15px;
}}
QLabel#topbar {{
    background: {PANEL};
    padding: 0 8px;
    font-size: 14px;
}}
QPushButton {{
    background: {PANEL_HI};
    border: 1px solid #2f3a45;
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 18px;
    min-height: 44px;
}}
QPushButton:pressed {{
    background: {ACCENT};
    color: #000;
}}
QPushButton:checked {{
    background: {ACCENT_2};
    color: #000;
    border-color: {ACCENT_2};
}}
QPushButton#softkey {{
    background: {PANEL};
    border: 1px solid #2f3a45;
    border-radius: 0;
    font-size: 19px;
    font-weight: bold;
    min-height: 46px;
    max-height: 46px;
}}
QPushButton#softkey:pressed {{
    background: {ACCENT};
    color: #000;
}}
QPushButton#keypad {{
    font-size: 24px;
    min-height: 50px;
}}
QLabel#toast {{
    background: {ACCENT};
    color: #000;
    border-radius: 8px;
    padding: 8px 18px;
    font-size: 18px;
    font-weight: bold;
}}
QLabel#entry {{
    font-family: "DejaVu Sans Mono", "Consolas", monospace;
    font-size: 40px;
    color: {ACCENT};
    background: {PANEL};
    padding: 6px 12px;
    border-radius: 6px;
}}
QLabel#title {{
    font-size: 20px;
    font-weight: bold;
    color: {TEXT_DIM};
}}
QScrollArea {{ border: none; }}
QScrollBar:vertical {{ width: 0px; }}
"""
