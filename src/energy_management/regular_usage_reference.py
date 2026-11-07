"""Download, summarize and benchmark external regular-electricity household profiles."""
import argparse
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tomllib
from urllib.request import urlopen
import zipfile

import numpy as np
import polars as pl
from scipy.optimize import least_squares

from energy_management.regular_usage import COMPONENTS, calendar_hours, generate_pair, validate

LIANDER = 'https://www.liander.nl/-/media/files/open-data/slimme-meter/'
FLUVIUS = ('https://fluvius.opendatasoft.com/api/explore/v2.1/catalog/datasets/'
           '1_50-verbruiksprofielen-dm-elek-kwartierwaarden-voor-een-volledig-jaar')


def cached_download(url, path, offline=False):
    path = Path(path)
    downloaded = not path.exists()
    if downloaded:
        if offline:
            raise ValueError(f'Missing cached source {path}; run without --offline first')
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(path.suffix+'.partial')
        with urlopen(url, timeout=60) as response, partial.open('wb') as output:
            shutil.copyfileobj(response, output)
        partial.replace(path)
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    manifest = path.with_suffix(path.suffix+'.source.json')
    if manifest.exists():
        record = json.loads(manifest.read_text())
        if record['sha256'] != digest:
            raise ValueError(f'Source checksum mismatch: {path}')
    else:
        record = dict(url=url, sha256=digest, bytes=path.stat().st_size,
                      recorded_at=datetime.now(timezone.utc).isoformat(),
                      retrieved_at=datetime.now(timezone.utc).isoformat() if downloaded else None,
                      note='Downloaded by workflow' if downloaded else 'Existing cache; original download time unknown')
        # File mtime is deliberately not presented as retrieval evidence.
        manifest.write_text(json.dumps(record, indent=2)+'\n')
    return path, record


def split_household(dataset, household):
    digest = hashlib.sha256(f'{dataset}:{household}'.encode()).digest()
    return 'test' if int.from_bytes(digest[:8], 'big') % 5 == 0 else 'train'


def assumption_signature(config):
    c = deepcopy(config)
    c.pop('annual_total_kwh', None)
    c.pop('variability', None)
    for name in COMPONENTS[-2:]:
        for key in ('fraction_moved', 'destination_hour_weights', 'destination_day_weights'):
            c[name].pop(key, None)
    return hashlib.sha256(json.dumps(c, sort_keys=True).encode()).hexdigest()


def transition_days(year):
    # The two European DST transition Sundays; used only for descriptive source summaries.
    result = []
    for month in (3, 10):
        d = date(year, month+1, 1)-timedelta(days=1)
        result.append((d-timedelta(days=(d.weekday()+1) % 7)).isoformat())
    return result


def summarize_profiles(frame, dataset, year, nominal_grid=False):
    """Validate each local-clock quarter grid; never infer UTC from source labels.

    Input: household, time (naive local datetime), kwh, excluded (technology/export).
    Ambiguous DST days are excluded from hourly comparisons, but not annual totals.
    """
    expected = (date(year+1, 1, 1)-date(year, 1, 1)).days*96
    frame = frame.with_columns(pl.col('time').dt.strftime('%Y-%m-%d').alias('date'))
    transitions = transition_days(year)
    quality = frame.group_by('household').agg(
        pl.len().alias('quarters'), pl.col('kwh').sum().alias('annual_kwh'),
        ((pl.col('kwh').is_null()) | (~pl.col('kwh').is_finite()) | (pl.col('kwh') < 0)).sum().alias('invalid'),
        pl.col('excluded').any().alias('technology_or_export'),
        ((pl.col('time').is_null()) | (pl.col('time').dt.year() != year) |
         (pl.col('time').dt.minute() % 15 != 0) | (pl.col('time').dt.second() != 0)).sum().alias('bad_time'))
    counts = frame.group_by('household', 'date').agg(pl.len().alias('n'), pl.col('time').n_unique().alias('unique'))
    expected_n = pl.when(pl.col('date') == transitions[0]).then(92).when(pl.col('date') == transitions[1]).then(100).otherwise(96)
    expected_unique = pl.when(pl.col('date') == transitions[0]).then(92).otherwise(96)
    if nominal_grid:
        expected_n = pl.lit(96); expected_unique = pl.lit(96)
    bad = counts.filter((pl.col('n') != expected_n) | (pl.col('unique') != expected_unique))['household'].to_list()
    quality = quality.with_columns(pl.col('household').is_in(bad).alias('bad_daily_grid'))
    quality = quality.with_columns(((pl.col('quarters') == expected) & (pl.col('invalid') == 0) &
                                   (pl.col('bad_time') == 0) & (~pl.col('bad_daily_grid')) &
                                   (~pl.col('technology_or_export')) & (pl.col('annual_kwh') > 0)).alias('included'))
    accepted = quality.filter(pl.col('included'))['household']
    if accepted.len() < 10:
        raise ValueError(f'{dataset}: only {accepted.len()} valid profiles; need at least 10 for comparison')
    frame = frame.filter(pl.col('household').is_in(accepted.implode()))
    annual = quality.select('household', 'annual_kwh')
    frame = frame.join(annual, on='household').with_columns(
        (pl.col('kwh')/(pl.col('annual_kwh')/(expected/4))).alias('relative_quarter'),
        pl.col('time').dt.month().alias('month'),
        (pl.col('time').dt.weekday() > 5).cast(pl.Int64).alias('weekend'),
        pl.col('time').dt.hour().alias('hour'))
    # Monthly shares retain all interval energy, including both autumn occurrences.
    monthly = frame.group_by('household', 'month').agg(
        (pl.col('kwh').sum()/pl.col('annual_kwh').first()).alias('annual_share'))
    normal = frame.filter(~pl.col('date').is_in(transitions))
    hourly = normal.group_by('household', 'month', 'weekend', 'date', 'hour').agg(
        pl.col('relative_quarter').sum().alias('relative_load'))
    cells = hourly.group_by('household', 'month', 'weekend', 'hour').agg(pl.col('relative_load').mean())
    daily = normal.group_by('household', 'date').agg((pl.col('relative_quarter').sum()/24).alias('relative_day'))
    variability = hourly.group_by('household').agg(
        pl.col('relative_load').quantile(.1).alias('hour_p10'),
        pl.col('relative_load').quantile(.9).alias('hour_p90'))
    daily_var = daily.group_by('household').agg((pl.col('relative_day').std()/pl.col('relative_day').mean()).alias('daily_cv'))
    metrics = variability.join(daily_var, on='household')
    def decorate(f):
        ids = f['household'].to_list()
        return f.with_columns(pl.lit(dataset).alias('dataset'),
                              pl.Series('split', [split_household(dataset, h) for h in ids]))
    return decorate(cells), decorate(monthly), decorate(metrics), quality.with_columns(pl.lit(dataset).alias('dataset'))


def prepare_liander(cache, offline):
    source, record = cached_download(LIANDER+'over-liander-slimme-meter-dataset-2013-levering.zip?v=1&d=20231027T074623Z', cache/'levering.zip', offline)
    export, export_record = cached_download(LIANDER+'over-liander-slimme-meter-dataset-2013-teruglevering.zip?v=1&d=20231027T074700Z', cache/'teruglevering.zip', offline)
    with zipfile.ZipFile(source) as z:
        frame = pl.read_csv(z.read(z.namelist()[0]), separator=';', infer_schema_length=0)
    frame = frame.filter(pl.col('datetime').is_not_null())
    columns = [c for c in frame.columns if c.startswith('Klant ')]
    times = frame['datetime'].str.to_datetime('%d-%m-%Y %H:%M', strict=True)
    with zipfile.ZipFile(export) as z:
        content = z.read(z.namelist()[0])
        # Publisher prepends a totals row before the actual header.
        lines = content.splitlines()
        header = next(i for i, line in enumerate(lines) if line.startswith(b'datetime;'))
        returns = pl.read_csv(b'\n'.join(lines[header:]), separator=';', infer_schema_length=0)
    # Export archive may list only exporting households. Absence is unknown, not proof of no PV.
    exporting = []
    for c in returns.columns:
        if c.startswith('Klant ') and returns[c].cast(pl.Float64, strict=False).fill_null(0).abs().sum() > 0:
            exporting.append(c)
    frames = [pl.DataFrame(dict(household=[c]*len(times), time=times,
                                kwh=frame[c].cast(pl.Float64, strict=False)/1000,
                                excluded=[c in exporting]*len(times))) for c in columns]
    return (*summarize_profiles(pl.concat(frames), 'liander', 2013, nominal_grid=True),
            dict(dataset='liander', sources=[record, export_record],
                 timezone='local Netherlands, nominal 96-quarter grid; UTC unavailable',
                 units='Wh/quarter converted to kWh', year=2013,
                 caveats=['Producer replaced some missing/unrealistic readings with averages',
                          'Positive recorded exports excluded; absent exports do not prove absence of solar',
                          'Heat pump, EV and appliance mix unknown; descriptive comparison, not clean regular load',
                          'Both DST days excluded from clock-hour comparisons; annual normalization uses nominal grid']))


def prepare_fluvius(cache, offline):
    metadata, metadata_record = cached_download(FLUVIUS, cache/'fluvius_metadata.json', offline)
    meta = json.loads(metadata.read_text())
    legend = next(a for a in meta['attachments'] if a['mimetype'].endswith('spreadsheetml.sheet'))
    _, legend_record = cached_download(legend['url'], cache/'fluvius_2.xlsx', offline)
    attachment = next(a for a in meta['attachments'] if 'deel_1_zip' in a['id'])
    archive, record = cached_download(attachment['url'], cache/'fluvius_0.zip', offline)
    csv_path = cache/'fluvius_regular.csv'
    if not csv_path.exists():
        with zipfile.ZipFile(archive) as z, z.open('P6269_Open_Data_geen_ZP.csv') as source, csv_path.open('wb') as output:
            shutil.copyfileobj(source, output)
    # Ensure any existing extracted cache is the exact archive member (CRC and length).
    import zlib
    with zipfile.ZipFile(archive) as z:
        info = z.getinfo('P6269_Open_Data_geen_ZP.csv')
    crc = 0
    with csv_path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            crc = zlib.crc32(block, crc)
    if csv_path.stat().st_size != info.file_size or crc != info.CRC:
        raise ValueError('Fluvius extracted CSV differs from cached archive')
    lf = pl.scan_csv(csv_path)
    frame = lf.select(
        pl.col('EAN_ID').cast(pl.String).alias('household'),
        pl.col('Datum_Startuur').str.slice(0, 19).str.to_datetime('%Y-%m-%dT%H:%M:%S').alias('time'),
        pl.col('Volume_Afname_KWh').alias('kwh'),
        ((pl.col('Warmtepomp_Indicator') != 0) | (pl.col('Elektrisch_Voertuig_Indicator') != 0) |
         (pl.col('PV_Installatie_Indicator') != 0) | (pl.col('Volume_Injectie_KWh') != 0) |
         (pl.col('Contract_Categorie') != 'Residentieel')).alias('excluded')).collect()
    return (*summarize_profiles(frame, 'fluvius', 2024),
            dict(dataset='fluvius', sources=[metadata_record, record, legend_record], year=2024, units='kWh/quarter',
                 timezone='CET/CEST clock labels, per legend; misleading Z suffix not treated as UTC',
                 license=meta['metas']['default'].get('license_url'),
                 caveats=['Only indicators zero and no exports; undetected EV/solar/heat pumps may remain',
                          'Known batteries excluded by publisher; unknown batteries may remain',
                          'Observed missing spring hour and repeated autumn hour; no fabricated UTC mapping',
                          'DST days excluded from clock-hour comparisons; actual 23/25-hour energy retained annually']))


def cell_summary(cells):
    return cells.group_by('dataset', 'split', 'month', 'weekend', 'hour').agg(
        pl.col('relative_load').mean().alias('mean'), pl.col('relative_load').median().alias('median'),
        pl.col('relative_load').quantile(.1).alias('p10'), pl.col('relative_load').quantile(.9).alias('p90'),
        pl.len().alias('households')).sort('dataset', 'split', 'month', 'weekend', 'hour')


def model_cells(config, year):
    frame, _ = generate_pair(config, year)
    local = [datetime.fromisoformat(t) for t in frame['interval_start_local']]
    result = pl.DataFrame(dict(date=[t.date().isoformat() for t in local], month=[t.month for t in local], weekend=[int(t.weekday() >= 5) for t in local],
                             hour=[t.hour for t in local],
                             relative_load=frame['energy_kwh'].to_numpy()/frame['energy_kwh'].mean()))
    return result.filter(~pl.col('date').is_in(transition_days(year))).group_by('month', 'weekend', 'hour').agg(pl.col('relative_load').mean()).sort('month', 'weekend', 'hour')


# Shape calibration only: all annual component shares stay at user-assumed values.
FIT = [('seasonality', 'amplitude', 0., .65), ('routine', 'morning_hour', 6., 11.),
       ('routine', 'morning_width', .7, 4.), ('routine', 'evening_hour', 17., 22.),
       ('routine', 'evening_width', .7, 4.), ('routine', 'evening_weight', .2, 4.),
       ('routine', 'daytime_weight', .1, 3.), ('routine', 'weekend_offset_hours', 0., 3.)]


def calibrate(config, cells):
    validate(config)
    if config['annual_total_kwh'] <= 0:
        raise ValueError('Calibration requires a positive annual total')
    # Liander is descriptive only (unknown technologies). Fit the screened Fluvius cohort.
    training = cells.filter((pl.col('dataset') == 'fluvius') & (pl.col('split') == 'train'))
    target = training.group_by('month', 'weekend', 'hour').agg(pl.col('relative_load').mean()).sort('month', 'weekend', 'hour')
    if target.height != 576:
        raise ValueError('Training data must cover all 12 x 2 x 24 comparison cells')
    base = deepcopy(config)
    for n in COMPONENTS[-2:]:
        base[n]['fraction_moved'] = 0
    def parameters(x):
        c = deepcopy(base)
        for (section, key, _, _), value in zip(FIT, x):
            c[section][key] = float(value)
        return c
    def residual(x):
        m = model_cells(parameters(x), 2024)
        return m['relative_load'].to_numpy()-target['relative_load'].to_numpy()
    initial = [np.clip(base[s][k], lo, hi) for s,k,lo,hi in FIT]
    result = least_squares(residual, initial, bounds=([p[2] for p in FIT], [p[3] for p in FIT]),
                           max_nfev=100, ftol=1e-6, xtol=1e-6, gtol=1e-6)
    if not result.success:
        raise ValueError('Shape calibration did not converge: '+result.message)
    fitted = parameters(result.x)
    fitted_params = {}
    for section, key, _, _ in FIT:
        fitted_params.setdefault(section, {})[key] = fitted[section][key]
    evaluations = []
    for dataset in cells['dataset'].unique().sort():
        for split in ('train', 'test'):
            actual = cells.filter((pl.col('dataset') == dataset) & (pl.col('split') == split))
            if actual.is_empty():
                raise ValueError(f'No {split} households for {dataset}')
            expected = actual.group_by('month', 'weekend', 'hour').agg(pl.col('relative_load').mean()).sort('month','weekend','hour')
            truth = expected['relative_load'].to_numpy()
            for label, c in [('initial', base), ('calibrated', fitted)]:
                pred = model_cells(c, 2013 if dataset == 'liander' else 2024)['relative_load'].to_numpy()
                evaluations.append(dict(dataset=dataset, split=split, model=label,
                                         households=actual['household'].n_unique(),
                                         cell_rmse=float(np.sqrt(np.mean((pred-truth)**2))),
                                         cell_mae=float(np.mean(np.abs(pred-truth)))))
            evaluations.append(dict(dataset=dataset, split=split, model='flat',
                                     households=actual['household'].n_unique(),
                                     cell_rmse=float(np.sqrt(np.mean((1-truth)**2))),
                                     cell_mae=float(np.mean(np.abs(1-truth)))))
    return fitted, dict(parameters=fitted_params, assumption_signature=assumption_signature(config),
                         training_dataset='fluvius', training_year=2024, training_split='train',
                         objective='Equal-weight RMSE of 576 monthly x weekday/weekend x clock-hour relative-load means',
                         normalization='Each household divided by its annual mean hourly energy',
                         fit_bounds=[dict(section=s, parameter=k, lower=lo, upper=hi) for s,k,lo,hi in FIT],
                         success=bool(result.success), nfev=result.nfev,
                         parameters_at_bounds=[f'{section}.{key}' for (section,key,lo,hi), value in zip(FIT,result.x)
                                               if min(abs(value-lo),abs(value-hi)) < 1e-4],
                         caveat='Shape fit only; neither component shares nor flexibility are identified'), pl.DataFrame(evaluations)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cache', type=Path, default=Path('data/regular_usage_source'))
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--offline', action='store_true')
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=True)
    config = tomllib.loads(a.config.read_text()); validate(config)
    results = []
    for name, prepare in [('liander', prepare_liander), ('fluvius', prepare_fluvius)]:
        print(f'Preparing {name} profiles...', flush=True)
        result = prepare(a.cache, a.offline); results.append(result)
        result[3].write_csv(a.output/f'{name}_quality.txt', separator='\t')
        print(f'{name}: {result[0]["household"].n_unique()} accepted households', flush=True)
    cells, monthly, variability = [pl.concat([r[i] for r in results]) for i in range(3)]
    for f, name in [(cells, 'household_cells'), (monthly, 'household_monthly'), (variability, 'household_variability'),
                    (cell_summary(cells), 'external_hourly_summary')]:
        f.write_csv(a.output/(name+'.txt'), separator='\t')
    fitted, calibration, evaluation = calibrate(config, cells)
    with (a.output/'household_cells.txt').open('rb') as stream:
        calibration['household_cells_sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
    calibration['sources'] = [r[4] for r in results]
    (a.output/'calibration.json').write_text(json.dumps(calibration, indent=2)+'\n')
    evaluation.write_csv(a.output/'evaluation.txt', separator='\t')
    (a.output/'sources.json').write_text(json.dumps(dict(datasets=[r[4] for r in results],
        split='SHA256(dataset:household) first 8 bytes modulo 5 == 0 selects held-out households',
        normalization='Per-household annual mean hourly energy; equal household weighting',
        summaries='Clock-hour descriptive patterns; no raw reference intervals exported as UTC'), indent=2)+'\n')
    from energy_management.regular_usage_plots import reference_report
    reference_report(cells, monthly, variability, fitted, evaluation, a.output)
    print(evaluation)
    print(f'Wrote reference evidence and calibration to {a.output}', flush=True)


if __name__ == '__main__':
    main()
