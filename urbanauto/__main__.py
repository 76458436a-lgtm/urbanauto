# SPDX-License-Identifier: GPL-2.0-or-later
"""Run with the Python shipped with QGIS, not a plain system Python."""
import argparse
import json
import os
import sys
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description='Автономный анализ папки спектральных индексов в среде QGIS.')
    parser.add_argument('--input', required=True, help='Папка NDBI_ГОД.tif, BSI_ГОД.tif, NDVI_ГОД.tif, MNDWI_max_ГОД.tif')
    parser.add_argument('--output', required=True, help='Родительская папка результатов')
    parser.add_argument('--years', help='Годы через запятую; по умолчанию все найденные')
    parser.add_argument('--roi', help='Путь к полигону GeoJSON/GeoPackage/Shapefile')
    parser.add_argument('--center', nargs=2, type=float, metavar=('LON','LAT'))
    parser.add_argument('--radius-km', type=float)
    parser.add_argument('--max-pixels', type=int, default=16000000)
    parser.add_argument('--min-confirm', type=int, default=2)
    parser.add_argument('--last-mode', choices=['backward', 'accept'], default='backward',
                        help='Последний год: backward — подтверждать предыдущим (нижняя оценка), accept — как есть')
    parser.add_argument('--min-area-ha', type=float, default=5.0)
    parser.add_argument('--strict-grid', action='store_true')
    parser.add_argument('--no-temporal', action='store_true')
    parser.add_argument('--keep-going', action='store_true')
    parser.add_argument('--resume', help='Папка предыдущего запуска run_...')
    args = parser.parse_args(argv)
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    try:
        from qgis.core import QgsApplication
    except ImportError:
        parser.exit(2, 'Нужна среда Python из QGIS (OSGeo4W Shell на Windows). Обычный Python не подходит.\n')
    app = QgsApplication([], False)
    app.initQgis()
    try:
        plugins = str(Path(QgsApplication.pkgDataPath()) / 'python' / 'plugins')
        if plugins not in sys.path:
            sys.path.insert(0, plugins)
        from processing.core.Processing import Processing
        Processing.initialize()
        from .urban_auto_batch import run_folder
        result = run_folder(args.input, args.output,
            years=[int(y.strip()) for y in args.years.split(',')] if args.years else None,
            roi_path=args.roi, center=args.center, radius_km=args.radius_km,
            max_pixels=args.max_pixels, min_confirm=args.min_confirm,
            last_mode=args.last_mode, min_area_ha=args.min_area_ha,
            auto_align=not args.strict_grid, temporal=not args.no_temporal,
            keep_going=args.keep_going, resume=args.resume,
            log=lambda message: print(message, file=sys.stderr, flush=True))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0 if result['status'] == 'complete' else 1
    except Exception as ex:
        print(f'ОШИБКА: {ex}', file=sys.stderr)
        return 1
    finally:
        app.exitQgis()


if __name__ == '__main__':
    sys.exit(main())
