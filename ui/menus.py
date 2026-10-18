# Portions of this file are derived from the QGIS Earth Engine Plugin
# (https://github.com/gee-community/qgis-earthengine-plugin),
# Copyright (c) 2019 Gennadii Donchyts, licensed under the MIT License.

from dataclasses import dataclass, field
from typing import Optional, List, Callable
from qgis.PyQt.QtWidgets import QMenu, QAction


MenuItem = Callable[[QMenu], None]


class Separator:
    """A separator in a menu, optionally labeled"""

    def __init__(self, label: Optional[str] = None):
        self.label = label

    def __call__(self, menu: QMenu):
        """Render the separator in the given QMenu."""
        if self.label is None:
            menu.addSeparator()  # plain divider
            return
        action = QAction(self.label, menu)
        action.setSeparator(True)  # non-clickable header line
        menu.addAction(action)


@dataclass
class SubMenu:
    """A submenu in a menu."""

    label: Optional[str] = None
    subitems: Optional[List[MenuItem]] = field(default_factory=list)

    def __call__(self, menu: QMenu):
        """Render the submenu in the given QMenu."""
        sub_menu = menu.addMenu(self.label or "")
        populate_menu(menu=sub_menu, items=self.subitems)


@dataclass
class Action:
    """An action in a menu."""

    action: Optional[QAction] = None

    def __call__(self, menu: QMenu):
        """Render the action in the given QMenu."""
        menu.addAction(self.action)


def populate_menu(*, menu: QMenu, items: List[MenuItem]):
    """Populate a QMenu with the given list of MenuItem objects."""
    for item in items:
        item(menu)
