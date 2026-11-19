# SPDX-License-Identifier: GPL-2.0-or-later
"""Autonomous local-folder processing, shared by GUI, CLI and future workers."""
import csv
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
import numpy as np


def discover_indices(folder, years=None):
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError(f'Папка исходных индексов не найдена: {folder}')
    pattern = re.compile(r'^(ndbi|bsi|ndvi|mndwi(?:_max)?)_(\d{4})$', re.I)
    selected = [int(y) for y in years] if years is not None else None
    if selected is not None and (not selected or len(selected) != len(set(selected))):
        raise ValueError('Список лет пуст или содержит повторы.')
    wanted = set(selected) if selected is not None else None
    groups = {}
    for p in sorted(folder.rglob('*')):
        if p.suffix.lower() not in ('.tif', '.tiff', '.vrt'):
            continue
        m = pattern.fullmatch(p.stem)
        if not m:
            continue
        key, year = m.group(1).lower(), int(m.group(2))
        key = 'mndwi' if key.startswith('mndwi') else key
        if wanted is not None and year not in wanted:
            continue
        if key in groups.setdefault(year, {}):
            raise ValueError(f'Два файла {key} для {year}: {groups[year][key]} и {p}. '
                             'Уберите неоднозначность имён.')
        groups[year][key] = str(p.resolve())
    if not groups:
        raise ValueError('Не найдены файлы NDBI_ГОД, BSI_ГОД, NDVI_ГОД, MNDWI[_max]_ГОД (.tif/.tiff/.vrt).')
    for year in sorted(wanted or groups):
        missing = set(('ndbi', 'bsi', 'ndvi', 'mndwi')) - set(groups.get(year, {}))
        if missing:
            raise ValueError(f'{year}: отсутствуют индексы {", ".join(sorted(missing))}.')
    return dict(sorted(groups.items()))


def write_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def file_signature(path):
    """Hashes full file bytes: a resume never trusts only its filename or mtime."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def run_folder(input_folder, output_folder, *, years=None, roi_path=None, center=None,
               radius_km=None, max_pixels=16000000, auto_align=True,
               temporal=True, min_confirm=2, last_mode='backward', min_area_ha=5.0, keep_going=False, resume=None,
               feedback=None, context=None, log=None, roi_identity=None):
    """Run on index rasters, without selecting layers or placing seed points.

    No imagery download/cloud removal: inputs must be scaled indices with their
    clouds/shadows marked NoData. Each new run owns a unique output directory.
    Resume accepts exactly one previous run and rejects changed inputs/options.
    """
    from qgis.core import (QgsProcessingContext, QgsProcessingFeedback, QgsCoordinateReferenceSystem,
                           QgsCoordinateTransform, QgsPointXY)
    from .urban_auto_core import UrbanConfig, run_urban_analysis_core, PLUGIN_VERSION
    from .urban_auto_inputs import open_raster, check_cancel
    from .urban_auto_temporal import (_grid, rasterize_layer, temporal_consistency,
                                      polygonize, pixel_area_km2, coverage_on_grid)
    log = log or print
    feedback = feedback or QgsProcessingFeedback()
    context = context or QgsProcessingContext()
    groups = discover_indices(input_folder, years)
    if isinstance(min_confirm, bool) or not isinstance(min_confirm, int) or min_confirm < 1:
        raise ValueError('min_confirm должен быть целым числом >= 1.')
    if last_mode == 'previous':
        last_mode = 'backward'
    if last_mode not in ('backward', 'accept'):
        raise ValueError('last_mode: backward или accept.')
    if not (isinstance(min_area_ha, (int, float)) and min_area_ha >= 0):
        raise ValueError('min_area_ha должен быть >= 0.')
    if not isinstance(max_pixels, int) or isinstance(max_pixels, bool) or max_pixels < 1:
        raise ValueError('max_pixels должен быть целым числом >= 1.')
    if temporal and len(groups) > 1:
        from scipy import ndimage  # noqa: F401 — fail before expensive work if unavailable
    if center is not None and radius_km is None:
        raise ValueError('При явном центре задайте radius_km.')
    if radius_km is not None and center is None:
        raise ValueError('При radius_km задайте center=(долгота, широта).')
    if roi_path and center is not None:
        raise ValueError('Задайте либо roi_path, либо центр с радиусом.')
    fingerprints = {}
    for year, paths in groups.items():
        fingerprints[str(year)] = {}
        for key, path in paths.items():
            check_cancel(feedback)
            fingerprints[str(year)][key] = dict(path=path, sha256=file_signature(path))
    # VRT files reference external datasets. A manifest hash alone cannot prove
    # that these dependencies did not change, so disable resume for them.
    if resume and any(Path(p).suffix.lower() == '.vrt' for ps in groups.values() for p in ps.values()):
        raise ValueError('Возобновление для VRT отключено: преобразуйте индексы в самостоятельные GeoTIFF.')
    options = dict(years=list(groups), roi_path=(roi_identity if roi_identity is not None else
                   str(Path(roi_path).resolve()) if roi_path else None),
                   center=list(center) if center is not None else None, radius_km=radius_km,
                   max_pixels=max_pixels, auto_align=auto_align, temporal=temporal,
                   min_confirm=min_confirm, last_mode=last_mode, min_area_ha=min_area_ha,
                   version=PLUGIN_VERSION)
    if roi_path and roi_identity is None:
        # Shapefile consists of multiple files, so hash sidecars as well.
        rp = Path(roi_path).resolve()
        components = sorted(rp.parent.glob(rp.stem + '.*')) if rp.suffix.lower() == '.shp' else [rp]
        options['roi_hashes'] = {str(p): file_signature(p) for p in components if p.is_file()}
    if resume:
        run_dir = Path(resume).expanduser().resolve()
        state = json.loads((run_dir / 'status.json').read_text(encoding='utf-8'))
        if state.get('options') != options or state.get('inputs') != fingerprints:
            raise ValueError('Исходные данные или параметры изменились; создайте новый запуск.')
    else:
        run_dir = Path(output_folder).expanduser().resolve() / (
            datetime.datetime.now().strftime('run_%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:8])
        run_dir.mkdir(parents=True, exist_ok=False)
        state = dict(options=options, inputs=fingerprints, years={}, status='running', errors=[])
    lock = run_dir / '.running'
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ValueError('Этот запуск уже выполняется. После аварии проверьте процесс и удалите .running вручную.')
    os.close(fd)
    try:
        state.update(status='running', errors=[])
        write_json(run_dir / 'status.json', state)
        ref = groups[min(groups)]['ndbi']
        if center is None:
            if roi_path:
                from .urban_auto_core import load_vector_to_memory
                roi_layer = load_vector_to_memory(str(roi_path), 'ROI')
                if not roi_layer.isValid() or roi_layer.featureCount() == 0:
                    raise ValueError('Не удалось открыть полигон ROI.')
                point = roi_layer.extent().center()
                xform = QgsCoordinateTransform(roi_layer.crs(), QgsCoordinateReferenceSystem('EPSG:4326'),
                                               context.transformContext())
                p = xform.transform(point)
                center = (p.x(), p.y())
            else:
                ds = open_raster(ref)
                gt = ds.GetGeoTransform()
                if gt[2] or gt[4]:
                    raise ValueError('Автоматический ROI требует north-up опорный растр.')
                corners = [(gt[0], gt[3]), (gt[0]+gt[1]*ds.RasterXSize, gt[3]),
                           (gt[0]+gt[1]*ds.RasterXSize, gt[3]+gt[5]*ds.RasterYSize),
                           (gt[0], gt[3]+gt[5]*ds.RasterYSize)]
                crs = QgsCoordinateReferenceSystem(); crs.createFromWkt(ds.GetProjection())
                xform = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem('EPSG:4326'), context.transformContext())
                points = [xform.transform(QgsPointXY(x, y)) for x, y in corners]
                center_pt = xform.transform(QgsPointXY(gt[0]+gt[1]*ds.RasterXSize/2,
                                                     gt[3]+gt[5]*ds.RasterYSize/2))
                center = (center_pt.x(), center_pt.y())
                ring = [[p.x(), p.y()] for p in points]; ring.append(ring[0])
                roi_path = str(run_dir / 'auto_roi.geojson')
                write_json(roi_path, {'type':'FeatureCollection', 'features':[
                    {'type':'Feature', 'properties':{}, 'geometry':{'type':'Polygon', 'coordinates':[ring]}}]})
                ds = None
                log('ROI автоматически взят по охвату первого NDBI. Это не административная граница города.')
        rows, results = [], {}
        for index, (year, paths) in enumerate(groups.items()):
            check_cancel(feedback)
            yd = run_dir / str(year)
            yd.mkdir(exist_ok=True)
            saved = state['years'].get(str(year), {})
            result_path = yd / f'Urban_Auto_{year}.gpkg'
            coverage = yd / f'Valid_{year}.tif'
            protocol_path = yd / f'run_protocol_{year}.json'
            expected_files = [result_path, coverage, protocol_path]
            reusable = (saved.get('status') == 'complete' and saved.get('output_hashes')
                        and all(p.is_file() and file_signature(p) == saved['output_hashes'].get(p.name)
                                for p in expected_files))
            if reusable:
                metrics = dict(saved['metrics'])
                log(f'{year}: проверенные результаты восстановлены.')
            else:
                state['years'][str(year)] = {'status':'running'}
                write_json(run_dir / 'status.json', state)
                try:
                    # Preserve stale/corrupt outputs rather than deleting them.
                    for p in expected_files:
                        if p.exists():
                            p.rename(p.with_name(p.stem + '.previous_' + uuid.uuid4().hex[:8] + p.suffix))
                    cfg = UrbanConfig(year, center, radius_km or 1.0,
                                      'polygon' if roi_path else 'circle', 8, None, '', str(yd),
                                      paths['ndbi'], paths['bsi'], paths['ndvi'], paths['mndwi'],
                                      False, '', threshold_options={'use_otsu':True, 'use_balance':True,
                                      'balance_ai':True}, reference_path=ref, polygon_path=str(roi_path) if roi_path else None,
                                      auto_align=auto_align, max_pixels=max_pixels)
                    metrics = {}
                    run_urban_analysis_core(cfg, log, context=context, feedback=feedback, metrics_sink=metrics)
                    state['years'][str(year)] = dict(status='complete', metrics=metrics,
                        output_hashes={p.name:file_signature(p) for p in expected_files})
                except Exception as ex:
                    check_cancel(feedback)
                    state['years'][str(year)] = dict(status='failed', error=str(ex))
                    state['errors'].append(f'{year}: {ex}')
                    write_json(run_dir / 'status.json', state)
                    if not keep_going:
                        raise
                    continue
            rows.append(dict(year=year, **metrics))
            results[year] = str(result_path)
            feedback.setProgress(100 * (index + 1) / len(groups))
            write_json(run_dir / 'status.json', state)
        if temporal and len(results) > 1 and not state['errors']:
            from scipy import ndimage as ndi
            grid = _grid(str(run_dir / str(min(results)) / f'Valid_{min(results)}.tif'))
            area = pixel_area_km2(grid)
            common = None
            for year in results:
                check_cancel(feedback)
                valid = coverage_on_grid(str(run_dir / str(year) / f'Valid_{year}.tif'), grid)
                common = valid if common is None else common & valid
            if common is None or not common.any():
                raise ValueError('Нет общей наблюдаемой территории между годами.')
            masks = {y:rasterize_layer(p, grid) & common for y,p in results.items()}
            cons = temporal_consistency(masks, list(results), min_confirm, last_mode)
            ordered = sorted(cons)
            for index, year in enumerate(ordered):
                check_cancel(feedback)
                m = cons[year]
                labels, count = ndi.label(m, np.ones((3, 3)))
                if count:
                    sizes = ndi.sum(area, labels, list(range(1,count+1)))
                    keep = np.zeros(count+1, bool); keep[1:] = sizes >= min_area_ha * 0.01
                    m = keep[labels]
                polygonize(m, grid, str(run_dir / f'Urban_Consistent_{year}.gpkg'), f'urban_{year}', year)
                row = next(r for r in rows if r['year'] == year)
                row['consistent_area_km2'] = float(area[m].sum())
                row['last_mode'] = last_mode if index == len(ordered)-1 else ''
                row['temporal_status'] = 'provisional_last' if index == len(ordered)-1 else (
                    'limited_future_support' if len(ordered)-index-1 < min_confirm else 'future_supported')
            state['common_observed_area_km2'] = float(area[common].sum())
        previous = None
        for row in rows:
            key = 'consistent_area_km2' if 'consistent_area_km2' in row else 'area_km2'
            row['growth_km2'] = None if previous is None else row[key] - previous[key]
            row['growth_basis'] = key
            previous = row
        with open(run_dir / 'summary.csv', 'w', newline='', encoding='utf-8-sig') as f:
            keys = ['year','area_km2','consistent_area_km2','growth_km2','growth_basis','temporal_status','last_mode','mode']
            w = csv.DictWriter(f, keys, delimiter=';', extrasaction='ignore'); w.writeheader(); w.writerows(rows)
        state.update(status='complete' if not state['errors'] else ('partial' if rows else 'failed'), rows=rows)
        write_json(run_dir / 'status.json', state)
        return dict(run_dir=str(run_dir), status=state['status'], rows=rows, errors=state['errors'])
    except Exception as ex:
        state['status'] = 'cancelled' if feedback.isCanceled() else 'failed'
        state['error'] = str(ex)
        write_json(run_dir / 'status.json', state)
        raise
    finally:
        lock.unlink(missing_ok=True)
