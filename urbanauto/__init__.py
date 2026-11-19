# SPDX-License-Identifier: GPL-2.0-or-later
# Urban Auto — (c) 2025-2026 Руслан Каленов (Ekyla)
from .urban_auto_plugin import UrbanAutoPlugin

def classFactory(iface):
    return UrbanAutoPlugin(iface)