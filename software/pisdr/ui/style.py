"""Colours, themes and the application stylesheet. Dark, high contrast, finger-sized targets.

Colour names are module globals so painting code can read ``style.ACCENT`` at draw time.
:func:`apply_theme` rewrites those globals and rebuilds :data:`STYLESHEET`; the main
window then re-applies the stylesheet and asks custom-painted widgets to ``retheme()``.
"""
from __future__ import annotations

THEMES: dict[str, dict[str, object]] = {
    "Amber": {
        "BG": "#0e1216", "PANEL": "#171d23", "PANEL_HI": "#222b34", "BORDER": "#2f3a45",
        "METER_BG": "#0a0d10",
        "TEXT": "#e8ecef", "TEXT_DIM": "#8a949c",
        "ACCENT": "#ffb000", "ACCENT_2": "#39c0ff", "REC": "#ff4040", "OK": "#5fd36a",
        "SELECT_FG": "#000000",
        "WATERFALL": [(0.0, (0, 0, 20)), (0.15, (0, 0, 110)), (0.35, (0, 120, 200)),
                      (0.55, (40, 200, 120)), (0.75, (255, 210, 0)), (1.0, (255, 255, 255))],
    },
    "Night": {
        "BG": "#050505", "PANEL": "#120808", "PANEL_HI": "#1e0c0c", "BORDER": "#3a1414",
        "METER_BG": "#080303",
        "TEXT": "#ff5a5a", "TEXT_DIM": "#8a3a3a",
        "ACCENT": "#ff3b3b", "ACCENT_2": "#ff8c42", "REC": "#ff2020", "OK": "#ff7a3a",
        "SELECT_FG": "#000000",
        "WATERFALL": [(0.0, (0, 0, 0)), (0.3, (60, 0, 0)), (0.55, (160, 10, 0)),
                      (0.75, (255, 80, 0)), (0.9, (255, 170, 60)), (1.0, (255, 230, 180))],
    },
    "Ice": {
        "BG": "#0b1020", "PANEL": "#121a30", "PANEL_HI": "#1b2645", "BORDER": "#2b3a66",
        "METER_BG": "#080c18",
        "TEXT": "#e6f0ff", "TEXT_DIM": "#7f93b8",
        "ACCENT": "#7fd0ff", "ACCENT_2": "#c9b3ff", "REC": "#ff5c7a", "OK": "#6ee7b7",
        "SELECT_FG": "#061020",
        "WATERFALL": [(0.0, (5, 8, 24)), (0.25, (20, 40, 110)), (0.5, (30, 120, 200)),
                      (0.75, (120, 210, 255)), (1.0, (255, 255, 255))],
    },
    "Daylight": {
        "BG": "#f2f4f6", "PANEL": "#ffffff", "PANEL_HI": "#e6eaef", "BORDER": "#c4ccd6",
        "METER_BG": "#d9dee5",
        "TEXT": "#141a20", "TEXT_DIM": "#5c6a78",
        "ACCENT": "#d97706", "ACCENT_2": "#0369a1", "REC": "#dc2626", "OK": "#15803d",
        "SELECT_FG": "#ffffff",
        "WATERFALL": [(0.0, (250, 250, 252)), (0.25, (190, 210, 240)), (0.5, (90, 150, 220)),
                      (0.75, (230, 120, 30)), (1.0, (120, 0, 0))],
    },
}
THEME_NAMES = tuple(THEMES.keys())
DEFAULT_THEME = "Amber"

# Populated by apply_theme(); defaults below keep static type checkers happy.
BG = PANEL = PANEL_HI = BORDER = METER_BG = TEXT = TEXT_DIM = ""
ACCENT = ACCENT_2 = REC = OK = SELECT_FG = ""
WATERFALL: list = []
CURRENT_THEME = DEFAULT_THEME
STYLESHEET = ""


def _build_stylesheet() -> str:
    return f"""
QWidget {{
    background: {BG};
    color: {TEXT};
    font-family: "DejaVu Sans", "Noto Sans", "Segoe UI", sans-serif;
    font-size: 15px;
}}
QFrame#topbar {{
    background: {PANEL};
}}
QFrame#topbar QLabel {{
    background: transparent;
    font-size: 14px;
}}
QFrame#topbar QLabel#chip {{
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 0 7px;
    font-size: 13px;
    font-weight: bold;
    color: {TEXT_DIM};
}}
QFrame#topbar QLabel#chip[active="true"] {{
    background: {ACCENT};
    border-color: {ACCENT};
    color: {SELECT_FG};
}}
QPushButton {{
    background: {PANEL_HI};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 18px;
    min-height: 44px;
}}
QPushButton:pressed {{
    background: {ACCENT};
    color: {SELECT_FG};
}}
QPushButton:checked {{
    background: {ACCENT_2};
    color: {SELECT_FG};
    border-color: {ACCENT_2};
}}
QPushButton:disabled {{
    color: {TEXT_DIM};
}}
QPushButton#softkey {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 0;
    font-size: 19px;
    font-weight: bold;
    min-height: 46px;
    max-height: 46px;
}}
QPushButton#softkey:pressed {{
    background: {ACCENT};
    color: {SELECT_FG};
}}
QPushButton#keypad {{
    font-size: 24px;
    min-height: 50px;
}}
QPushButton#tile {{
    font-size: 16px;
    padding: 4px 6px;
    min-height: 44px;
}}
QPushButton#small {{
    font-size: 14px;
    min-height: 30px;
    padding: 2px 8px;
}}
QLabel#toast {{
    background: {ACCENT};
    color: {SELECT_FG};
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
QLabel#section {{
    font-size: 13px;
    font-weight: bold;
    color: {TEXT_DIM};
    padding: 2px 4px 0 4px;
}}
QLabel#hint {{
    font-size: 17px;
    color: {TEXT_DIM};
}}
QFrame#row {{
    background: {PANEL_HI};
    border: 1px solid {BORDER};
    border-radius: 6px;
}}
QFrame#row QLabel {{
    background: transparent;
    font-size: 16px;
}}
QFrame#row QLabel#value {{
    color: {ACCENT};
    font-weight: bold;
}}
QFrame#row[selected="true"] {{
    background: {ACCENT_2};
    border-color: {ACCENT_2};
}}
QFrame#row[selected="true"] QLabel {{
    color: {SELECT_FG};
}}
QFrame#panel {{
    background: {PANEL};
    border-radius: 6px;
}}
QFrame#card {{
    background: {PANEL_HI};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QFrame#card QLabel {{
    background: transparent;
}}
QFrame#card QLabel#name {{
    font-size: 18px;
    font-weight: bold;
}}
QFrame#card QLabel#detail {{
    font-size: 14px;
    color: {TEXT_DIM};
}}
QFrame#card QLabel#badge {{
    font-size: 12px;
    font-weight: bold;
    color: {ACCENT};
}}
QFrame#card[selected="true"] {{
    background: {ACCENT_2};
    border-color: {ACCENT_2};
}}
QFrame#card[selected="true"] QLabel,
QFrame#card[selected="true"] QLabel#detail,
QFrame#card[selected="true"] QLabel#badge {{
    color: {SELECT_FG};
}}
QScrollArea {{ border: none; }}
QScrollBar:vertical {{ width: 0px; }}
"""


def apply_theme(name: str) -> str:
    """Set the module colour globals for *name* and rebuild the stylesheet."""
    global BG, PANEL, PANEL_HI, BORDER, METER_BG, TEXT, TEXT_DIM
    global ACCENT, ACCENT_2, REC, OK, SELECT_FG, WATERFALL, CURRENT_THEME, STYLESHEET
    theme = THEMES.get(name) or THEMES[DEFAULT_THEME]
    CURRENT_THEME = name if name in THEMES else DEFAULT_THEME
    BG = theme["BG"]; PANEL = theme["PANEL"]; PANEL_HI = theme["PANEL_HI"]           # noqa: E702
    BORDER = theme["BORDER"]; METER_BG = theme["METER_BG"]                           # noqa: E702
    TEXT = theme["TEXT"]; TEXT_DIM = theme["TEXT_DIM"]                               # noqa: E702
    ACCENT = theme["ACCENT"]; ACCENT_2 = theme["ACCENT_2"]                           # noqa: E702
    REC = theme["REC"]; OK = theme["OK"]; SELECT_FG = theme["SELECT_FG"]             # noqa: E702
    WATERFALL = list(theme["WATERFALL"])
    STYLESHEET = _build_stylesheet()
    return STYLESHEET


def repolish(widget) -> None:
    """Force a widget to re-evaluate stylesheet rules after a dynamic property change."""
    st = widget.style()
    st.unpolish(widget)
    st.polish(widget)
    widget.update()


apply_theme(DEFAULT_THEME)
