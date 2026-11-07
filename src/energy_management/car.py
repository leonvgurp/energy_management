"""PHEV energy budgets; scenarios, not inferred driving or charging schedules."""
import argparse
import hashlib
import json
from pathlib import Path
import tomllib



from .components.phev import budget_scenarios as scenarios


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    raw = args.config.read_bytes()
    c = tomllib.loads(raw.decode())
    frame = scenarios(c)
    args.output.mkdir(parents=True, exist_ok=True)
    frame.write_csv(args.output / 'scenarios.tsv', separator='\t')
    (args.output / 'run.json').write_text(json.dumps(dict(
        method='phev_daily_energy_budget_v1', config=c,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        limitations=['No fitted seasonality or hourly schedule',
                     'Full battery each modeled day; no intermediate recharge',
                     'No petrol or engine-heating calculation',
                     'All modeled charging at home; flat import price']), indent=2)+'\n')
    print(frame)


if __name__ == '__main__':
    main()
