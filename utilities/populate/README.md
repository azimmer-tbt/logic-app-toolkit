# Attendants

Small, dumb, file-I/O utilities for the Logic Apps toolkit. No Logic App
awareness. No opinions about app structure. Just move bits around on disk.

Supporting things not specific to any one named role (Cartographer, Inquisitor,
Evangelist, etc.) live here. Examples of things that belong:

- File bundle unpackers/packers
- Checksummers
- Image size readers or format converters (for email header images)
- HTML linters
- Azure CLI helper scripts
- Anything that supports the workflow but doesn't have a role of its own

---

## Tools

### populate.py

Unpack file bundles to disk. Two modes:

**JSON bundle** — text files only. Keys are relative paths, values are content.
Good for scripts, configs, HTML — anything Claude can write inline.

```bash
python3 populate.py bundle.json --base ./output
```

**Tarball** — binary-safe, any file type. Unpacks the archive, then optionally
runs a postflight shell script. The postflight receives the resolved base
directory as `$1` and runs with that directory as cwd.

```bash
python3 populate.py kit.tar.gz --base ./output
python3 populate.py kit.tar.gz --base ./output --postflight postflight.sh
```

Populate exits with the postflight's exit code. Postflight non-zero = failure.

`--dry-run` shows what would happen without writing anything.

---

## Example postflights

| Script | Purpose |
|--------|---------|
| `postflight_verify_chmod.sh` | Verify required files exist, chmod +x scripts, zap cruft |
| `postflight_zap_cruft.sh` | Remove .DS_Store, __pycache__, .pyc, .AppleDouble |
| `postflight_trivial.sh` | No-op. For tarballs that just need unpacking (images, assets) |

Edit `REQUIRED_FILES` in `postflight_verify_chmod.sh` for your specific kit.
