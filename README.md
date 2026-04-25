# Lineage Cutout to gmobile

Generate `gmobile` display-panel JSON files from LineageOS Android device overlay repositories.

The script clones a LineageOS `android_device_<oem>_<codename>` repository, scans resource overlays and device metadata, then writes a JSON file with display resolution, rounded corners, and front-camera cutout data.

Generated files are a starting point. Always validate the output visually on the device or with `gmobile`/`phoc` debug tooling before upstreaming it.

## Requirements

- Python 3.9 or newer
- `git`
- Optional: `json-glib-validate` for output validation

There are no third-party Python package dependencies.

## Usage

```sh
python3 main.py <oem> <codename> --output-dir ./display-panels
```

Example for the Xiaomi Redmi Note 10 Pro/Pro Max (`sweet`):

```sh
python3 main.py xiaomi sweet \
  --branch lineage-23.2 \
  --workdir ./lineage-device-work \
  --output-dir ./display-panels \
  --force \
  --print-notes
```

This writes:

```text
display-panels/xiaomi,sweet.json
```

If `--branch` is omitted, the script tries to use the newest available `lineage-*` branch. If detection misses a value, provide overrides such as `--x-res`, `--y-res`, or `--density`.

## Useful Options

- `--repo-url URL`: use a specific device repository instead of `https://github.com/LineageOS/android_device_<oem>_<codename>.git`
- `--clone-dependencies`: also clone repositories listed in `lineage.dependencies`
- `--compatible STEM`: choose the output file stem, for example `xiaomi,sweet`
- `--output-file PATH`: write to an exact file path
- `--corner-format {auto,border-radius,corner-radii}`: choose how rounded corners are emitted
- `--use-rect-approx`: use Android's rectangular cutout approximation instead of the exact cutout path
- `--include-physical-size`: estimate physical display width and height from README diagonal size
- `--no-validate`: skip `json-glib-validate`

Run `python3 main.py --help` for the complete CLI reference.

