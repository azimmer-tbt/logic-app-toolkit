# Logic App Toolkit

Deterministic, checksummed patching pipeline for Azure Logic Apps (Consumption tier).

## The Toolkit

| Tool | Role | One-liner |
|------|------|-----------|
| **Cartographer** | Map | Fingerprints a Logic App JSON — every action, field, checksum |
| **Inquisitor** | Verify | Checks an app against declared expectations (vitals.yaml) |
| **Surgeon** | Cut | Applies checksummed patches from an orthodox.yaml file |
| **Confessor** | Examine | Compares patched app against desired_values — report or TUI |
| **Evangelist** | Orchestrate | Reads a manifest, calls Surgeon per target app |
| **Exorcist** | Diagnose | Scans for structural fatal flaws in app definitions |
| **Purifier** | Redact | Scrubs or restores credentials in Logic App JSON |

### Utilities

| Tool | Location | One-liner |
|------|----------|-----------|
| **generate_orthodox.py** | `utilities/generators/` | Diffs desired_values + PRIOR → orthodox YAML |
| **desired_values_parser.py** | `utilities/generators/` | Parses the v2 desired_values format |
| **manifest_from_csv.py** | `utilities/converters/` | Converts multi-app CSV to Evangelist manifests |

### Pipeline (shell wrappers)

| Verb | Script | What it does |
|------|--------|--------------|
| **check** | `pipeline/check_patch.sh` | Dry-run drift check — "do I need to patch?" |
| **generate** | `pipeline/generate_patch.sh` | Build the orthodox YAML |
| **apply** | `pipeline/apply_patch.sh` | Run surgeon + verify |
| **verify** | `pipeline/verify_patch.sh` | Confessor report or TUI |

## The Loop

```
1. Cartographer    → map the source app (baseline fingerprints)
2. Inquisitor      → verify source matches expectations
3. Human/AI        → write orthodox.yaml patch instructions
4. Surgeon         → apply patches (pre-op verify → cut → post-op verify)
5. Cartographer    → map the result (post-op fingerprints)
6. Inquisitor      → verify result matches expectations
```

If step 4 refuses (pre-op checksum mismatch), the source has drifted.
Re-run step 1 to find out what changed.

If step 6 fails, the patches produced the wrong result.
The orthodox.yaml needs correction.

## Orthodox YAML — Patch Instructions

Each orthodox file defines one source → one target conversion.
One file per target app. Never a multi-target manifest.

```yaml
source:
  file: FRESHMART-DEV-PRICES.json
  description: "FreshMart dev store — baseline"

target:
  name: FRESHMART-STAGING
  output: FRESHMART-STAGING-PRICES.json

patches:
  - section: config
    path: definition.actions.Initialize_API_Variables.inputs.variables[sharepoint_site].value
    from: "https://freshmart.sharepoint.com/sites/ops"
    to: "https://freshmart.sharepoint.com/sites/staging"
    from_sha: b15d6a765b0f
    to_sha: 99b8cb98ff19
```

### Patch Entry Fields

| Field | Required | Description |
|-------|----------|-------------|
| `section` | yes | Human-readable category (control_panel, identity, etc.) |
| `operation` | no | `replace_value` (default) or `rename_key` |
| `path` | yes | Dot-notation path to the field |
| `from` | yes | Expected current value (or key name for rename_key) |
| `to` | yes | Desired new value (or new key name for rename_key) |
| `from_sha` | yes | SHA256[:12] of the `from` string |
| `to_sha` | yes | SHA256[:12] of the `to` string |
| `note` | no | Human-readable context |

### Operations

**`replace_value`** (default): Navigate to path, replace the value at that
leaf, verify SHAs of the values before and after.

**`rename_key`**: Navigate to the dict at path, rename key `from` → `to`.
The value under the key is untouched. SHAs are of the key name strings.
Used when a JSON schema needs a different property name (e.g.
`stores` → `warehouses` in a Parse step schema).

## CRITICAL: Checksums Cover the Entire Field Value

Checksums (`from_sha`, `to_sha`) are computed against the **complete,
literal string value** of the field — not a substring, not a portion,
not just the "interesting part."

This matters because Logic App field values often contain embedded
expressions that reference other variables. For example:

```
price_list_url value:
  @{variables('sharepoint_site')}/Shared Documents/price_list.csv
```

This value contains a runtime pointer (`@{variables('sharepoint_site')}`) that
resolves to a SharePoint URL at execution time. But the **stored value**
in the JSON is the entire expression including the pointer. The checksum
covers all of it:

```yaml
# WRONG — checksumming only the "interesting" suffix
from: "Shared Documents/price_list.csv"
from_sha: e179b3831fba   # ← SHA of the substring, NOT the actual field

# CORRECT — checksumming the complete field value as stored in JSON
from: "@{variables('sharepoint_site')}/Shared Documents/price_list.csv"
from_sha: 89a8ca9733bc   # ← SHA of the actual field value
```

The `to` value follows the same rule — it's the complete new value
including any embedded expressions:

```yaml
to: "@{variables('sharepoint_site')}/Shared Documents/price_list_v2.csv"
to_sha: b1bb61586043
```

**Why this matters:** If you checksum a substring, Surgeon's pre-op verify
will fail because it reads the full field value from the JSON and hashes
that. The hash of the full string ≠ the hash of a substring. Surgeon
refuses to operate. This is by design — Surgeon is capable, not creative.
It does not infer what you meant.

**Rule of thumb:** When writing an orthodox.yaml, always read the actual
field value from the source JSON (via Cartographer fingerprints or direct
inspection) and use that complete string as `from`. Never truncate, never
extract "just the part that changes."

## Path Notation

Paths use dot notation with bracket syntax for array variable lookup:

```
definition.actions.CONTROL_PANEL.inputs.variables[mobile_or_mac].value
```

Means:
1. Start at JSON root
2. Navigate: `definition` → `actions` → `CONTROL_PANEL` → `inputs` → `variables`
3. `variables` is an array of `{name, type, value}` objects
4. `[mobile_or_mac]` = find the object where `name == "mobile_or_mac"`
5. `.value` = read/write the `value` field of that object

Bracket notation is name-based, not index-based. Array indices are not
stable — Portal may reorder variables on save. Name-based lookup is
Portal-proof.

## Developing a New Orthodox File

### Step 1: Identify what changes

Compare source and target apps. Use Cartographer to fingerprint both,
then diff the fingerprints. Or use the Evangelist spreadsheet if one
exists for your project.

### Step 2: For each difference, determine the operation

- Value changes → `replace_value` (most cases)
- JSON key renames → `rename_key` (rare — parse schema changes)

### Step 3: Get the exact field values

Read the source JSON directly. Do NOT guess, truncate, or paraphrase
values. Use the complete string as stored in the JSON, including any
`@{variables(...)}` expressions.

### Step 4: Compute checksums

```python
import hashlib
def sha12(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:12]
```

### Step 5: Write the orthodox.yaml

One file per target. Flat `patches:` list. Every entry has `from_sha`
and `to_sha`.

### Step 6: Dry run

```
python3 surgeon.py \
  --input source.json \
  --patch-task my_orthodox.yaml \
  --output test_output.json \
  --log test_audit.log
```

Check the audit log. All pre-op and post-op checks should pass.

### Step 7: Verify

```
python3 cartographer.py --input test_output.json --output post_cart/
python3 inquisitor.py --fingerprints post_cart/fingerprints.json --vitals vitals.yaml
```

## CLI Reference

### Surgeon
```
python3 surgeon.py \
  --input source.json \
  --patch-task source_to_TARGET.orthodox.yaml \
  --output TARGET.json \
  --log surgeon_TARGET.log
```
All four arguments required. No defaults. Missing any → error.

### Cartographer
```
python3 cartographer.py \
  --input app.json \
  --output report_directory/ \
  [--catalog plugin_catalog.json] \
  [--detailed]
```

### Inquisitor
```
python3 inquisitor.py \
  --fingerprints fingerprints.json \
  --vitals vitals.yaml \
  --app APP_NAME \
  --mode vitals \
  --output-dir report_directory/
```
