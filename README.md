# NTC Account Factory — State Migration Tool (v1 → v2)

Generates `baseline_moved_resources` blocks for migrating from legacy per-region baseline modules to the unified baseline module.

> **⚠️ High blast radius:** Incorrect moved blocks can destroy baseline resources across all accounts in a scope simultaneously. Always validate the output before applying.

## Prerequisites

- Python 3.8+
- No external dependencies (stdlib only)

## Quick Start

```bash
# 1. Export the legacy state list from a CodeBuild baseline execution output
#    (the state list is printed at the end of each baseline run)
#    Save it as legacy_state.txt — one address per line:
#
#    module.baseline_eu_central_1[0].aws_iam_role.ntc_config[0]
#    module.baseline_eu_central_1[0].aws_config_configuration_recorder.ntc_config
#    module.baseline_us_east_1[0].aws_config_configuration_recorder.ntc_config
#    ...

# 2. Run the tool with your unified templates
python3 ntc_state_migration.py \
  --from-file legacy_state.txt \
  --main-region eu-central-1 \
  --templates /path/to/unified/templates/

# 3. Copy the output into your baseline scope configuration
```

## How It Works

The tool reads legacy state addresses and translates them to unified addresses by applying three rules:

### Case 1 — Global resource, count removed

IAM roles, OIDC providers, S3 buckets — resources that had `count = var.is_current_region_main_region` in the legacy template but have **no count** in the unified template.

```
from: module.baseline_eu_central_1[0].aws_iam_role.ntc_config[0]
  to: module.baseline_unified[0].aws_iam_role.ntc_config          ← [0] removed
```

### Case 2 — Global resource, count kept

Resources that still use `count` in the unified template (e.g. KMS keys with `count = local.create_kms_key`, instance profiles with `count = is_instance_profile`).

```
from: module.baseline_eu_central_1[0].aws_kms_key.ntc_state_encryption[0]
  to: module.baseline_unified[0].aws_kms_key.ntc_state_encryption[0]    ← [0] stays
```

### Case 3 — Regional resource, for_each added

Resources that existed once per region workspace and now use `for_each = toset(var.baseline_regions)`.

```
from: module.baseline_eu_central_1[0].aws_config_configuration_recorder.ntc_config
  to: module.baseline_unified[0].aws_config_configuration_recorder.ntc_config["eu-central-1"]

from: module.baseline_us_east_1[0].aws_config_configuration_recorder.ntc_config
  to: module.baseline_unified[0].aws_config_configuration_recorder.ntc_config["us-east-1"]
```

### Skipped: Data sources

Data sources (`data.*`) are always skipped — they are re-computed by Terraform and don't need moved blocks.

## Usage

### With unified templates (recommended)

The tool parses the unified `.tftpl` templates to determine whether each resource uses `count`, `for_each`, or neither. This is the most accurate mode.

```bash
python3 ntc_state_migration.py \
  --from-file legacy_state.txt \
  --main-region eu-central-1 \
  --templates /path/to/unified/templates/
```

The `--templates` argument accepts:
- A **directory** — scans for all `unified_*.tftpl` files
- A **comma-separated list** of file paths
- A **glob pattern** — e.g. `./templates/unified_*.tftpl`

### With validation (strongly recommended)

Pass the actual unified state list to validate the generated `moved_to` addresses:

```bash
python3 ntc_state_migration.py \
  --from-file legacy_state.txt \
  --main-region eu-central-1 \
  --templates /path/to/unified/templates/ \
  --validate-file unified_state.txt
```

The validation catches two types of issues:
- **MISSING_IN_ACTUAL** — a generated `moved_to` address doesn't exist in the unified state (wrong index, wrong name)
- **NOT_COVERED** — a unified resource has no `moved_from` mapping (missing moved block)

### Without templates (heuristic mode)

If no templates are provided, the tool falls back to heuristics:
- Resources appearing in multiple region modules → regional (Case 3)
- Resources with `[0]` index in the main region → global, count removed (Case 1)

```bash
python3 ntc_state_migration.py \
  --from-file legacy_state.txt \
  --main-region eu-central-1
```

> **⚠️ Heuristic mode cannot distinguish Case 1 from Case 2** (it doesn't know if the unified template kept `count` or not). Always verify heuristic results manually or use `--validate-file`.

### Write output to file

```bash
python3 ntc_state_migration.py \
  --from-file legacy_state.txt \
  --main-region eu-central-1 \
  --templates ./templates/ \
  --output moved_blocks.tf
```

### Pipe from stdin

```bash
cat legacy_state.txt | python3 ntc_state_migration.py \
  --main-region eu-central-1 \
  --templates ./templates/
```

## CLI Reference

| Flag | Required | Description |
|---|---|---|
| `--from-file` | No | File with legacy state addresses, one per line. Default: stdin (`-`) |
| `--main-region` | **Yes** | Main baseline region (e.g. `eu-central-1`) |
| `--templates` | No | Path to unified templates (directory, file list, or glob) |
| `--validate-file` | No | File with unified state addresses for validation |
| `--output` | No | Output file. Default: stdout (`-`) |
| `--no-comments` | No | Omit comments in output |
| `--show-skipped` | No | Include skipped data sources as comments |

## Input File Format

One Terraform state address per line. Empty lines and lines starting with `#` are ignored.

```
# IAM roles (global)
module.baseline_eu_central_1[0].aws_iam_role.ntc_config[0]
module.baseline_eu_central_1[0].aws_iam_role_policy_attachment.ntc_config[0]

# Config recorder (regional)
module.baseline_eu_central_1[0].aws_config_configuration_recorder.ntc_config
module.baseline_us_east_1[0].aws_config_configuration_recorder.ntc_config
```

Where to get the state list: check the **CodeBuild logs** of a baseline execution — the state is listed at the end of each run.

## Output Format

The tool outputs a `baseline_moved_resources` block ready to paste into your baseline scope configuration:

```terraform
baseline_moved_resources = [
  {
    moved_from = "module.baseline_eu_central_1[0].aws_iam_role.ntc_config[0]"
    moved_to   = "module.baseline_unified[0].aws_iam_role.ntc_config"
  },
  {
    moved_from = "module.baseline_eu_central_1[0].aws_config_configuration_recorder.ntc_config"
    moved_to   = "module.baseline_unified[0].aws_config_configuration_recorder.ntc_config[\"eu-central-1\"]"
  },
]
```

## Summary Output

The tool prints a summary to stderr showing the classification breakdown and any validation issues:

```
============================================================
MIGRATION SUMMARY
============================================================
  Total addresses processed:    39
  Data sources (skipped):       9
  Global (count removed):       16
  Global (count kept):          6
  Regional (for_each):          8
  Global (heuristic):           0
  ---
  Moved blocks generated:       30
  Confidence: template-based:   28
  Confidence: heuristic:        2

  ✅ Validation passed — all moves match unified state.
============================================================
```

## Known Limitations

- **Template parsing is regex-based**, not a full HCL parser. It looks for `count` or `for_each` within the first 5 lines after a `resource` declaration. Unusual formatting (e.g. the keyword on line 8+) may cause misclassification.
- **Heuristic mode can't distinguish Case 1 from Case 2.** Without templates, it defaults to removing `[0]` — which is wrong for resources that keep `count` in the unified template (e.g. KMS keys, instance profiles, policy attachments).
- **Resources not in any template** (e.g. Access Analyzer from a separate module) fall back to heuristic classification.
- **No deduplication** of template patterns when the same template files exist under different names in the templates directory.

## Recommended Workflow

1. Export legacy state from CodeBuild logs → `legacy_state.txt`
2. Run the tool with `--templates` and `--validate-file`
3. Check the summary: **0 heuristic moves** and **✅ Validation passed** = good to go
4. If heuristic moves remain: verify manually against the unified template
5. Paste into `baseline_moved_resources` in your baseline scope config
6. Test on a sandbox scope first, then non-prod, then prod

## Related Documentation

- [NTC Account Factory v1 → v2 Migration Guide](https://docs.nuvibit.com/migration-guides/v1-to-v2/ntc-account-factory)
- [NTC Account Baseline Templates](https://docs.nuvibit.com/ntc-building-blocks/templates/ntc-account-baseline-templates/)