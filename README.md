# energy_management

Explore household solar generation, electricity use, a heat pump, and car charging.
Build reusable Python component models before combining household energy flows.

## Environment

Use Pixi for the scientific Python environment:

```sh
pixi install --locked
pixi run smoke
```

Dependencies are declared in `pixi.toml` and pinned in `pixi.lock`.
The current environment targets Apple Silicon macOS.

Keep private measurements in `data/` and generated results in `outputs/`;
both directories are Git-ignored. Project instructions are in `AGENTS.md`.
