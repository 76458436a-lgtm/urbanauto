# SPDX-License-Identifier: GPL-2.0-or-later
# Urban Auto — (c) 2025-2026 Руслан Каленов (Ekyla)
"""5.5.1: проверка сторонних библиотек и старых копий модуля (без импорта тяжёлых модулей)."""
import importlib.util
import platform

REQUIRED = {'numpy': 'numpy', 'scipy': 'scipy', 'sklearn': 'scikit-learn', 'requests': 'requests'}
LEGACY_PLUGINS = ('urban_auto_exp54', 'UrbanAutoCalibrator', 'urban_auto_calibrator', 'ekyla', 'ekyla_3', 'ekyla_v4')


def missing():
    """Список pip-имён отсутствующих библиотек."""
    return [pip for mod, pip in REQUIRED.items() if importlib.util.find_spec(mod) is None]


def install_hint(names=None):
    names = names or missing() or list(REQUIRED.values())
    pkgs = ' '.join(names)
    if platform.system() == 'Windows':
        return ('Закройте QGIS, откройте «OSGeo4W Shell» из меню Пуск (папка QGIS) и выполните:\n'
                f'    python -m pip install {pkgs}\n'
                'Затем снова запустите QGIS.')
    if platform.system() == 'Darwin':
        return ('Выполните в терминале (путь — к Python внутри QGIS.app):\n'
                f'    /Applications/QGIS.app/Contents/MacOS/bin/python3 -m pip install {pkgs}')
    return ('Установите пакеты системным менеджером (Ubuntu/Debian: sudo apt install python3-sklearn python3-scipy '
            f'python3-requests) или: python3 -m pip install --user {pkgs}')


def require():
    """Исключение с понятной инструкцией, если чего-то не хватает."""
    m = missing()
    if m:
        raise RuntimeError('Urban Auto: для годового ряда не хватает библиотек Python: ' + ', '.join(m) + '.\n' + install_hint(m))


def active_legacy():
    try:
        from qgis.utils import active_plugins
    except Exception:
        return []
    return [p for p in active_plugins if p in LEGACY_PLUGINS]
