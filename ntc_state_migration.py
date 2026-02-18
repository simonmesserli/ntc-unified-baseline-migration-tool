#!/usr/bin/env python3
"""
NTC Account Factory v1 → v2 State Migration Tool
==================================================
Generates baseline_moved_resources blocks for migrating from legacy
per-region baseline modules to the unified baseline module.

Usage:
  # Basic usage with templates (recommended):
  python3 ntc_state_migration.py \\
    --from-file legacy_state.txt \\
    --main-region eu-central-1 \\
    --templates /path/to/unified/templates/

  # Pipe from stdin:
  cat legacy_state.txt | python3 ntc_state_migration.py \\
    --main-region eu-central-1 \\
    --templates /path/to/unified/templates/

  # With validation against actual unified state:
  python3 ntc_state_migration.py \\
    --from-file legacy_state.txt \\
    --main-region eu-central-1 \\
    --templates /path/to/unified/templates/ \\
    --validate-file unified_state.txt

  # Without templates (heuristic mode — less accurate):
  python3 ntc_state_migration.py \\
    --from-file legacy_state.txt \\
    --main-region eu-central-1

Input format (one state address per line):
  module.baseline_eu_central_1[0].aws_iam_role.ntc_config[0]
  module.baseline_us_east_1[0].aws_config_configuration_recorder.ntc_config
  ...

Migration logic (3 cases):
  Case 1 - Global, count removed:
    from: module.baseline_<main_region>[0].<resource>[0]
    to:   module.baseline_unified[0].<resource>

  Case 2 - Global, count kept:
    from: module.baseline_<main_region>[0].<resource>[0]
    to:   module.baseline_unified[0].<resource>[0]

  Case 3 - Regional, for_each added:
    from: module.baseline_<region>[0].<resource>
    to:   module.baseline_unified[0].<resource>["<region>"]

  Data sources are skipped (re-computed, no moved blocks needed).
"""

import argparse
import glob
import os
import re
import sys
from collections import defaultdict

# =============================================================================
# PARSING
# =============================================================================

def parse_state_address(addr):
    """Parse a Terraform state address into its components.

    Handles addresses like:
      module.baseline_eu_central_1[0].aws_iam_role.ntc_config[0]
      module.baseline_eu_central_1[0].data.aws_iam_policy_document.ntc_config[0]
      module.baseline_unified[0].aws_config_configuration_recorder.ntc_config["eu-central-1"]
    """
    addr = addr.strip()
    if not addr:
        return None

    # Legacy/unified pattern with optional numeric index [N]
    pattern = (
        r'^module\.baseline_(\w+)\[0\]\.'          # module.baseline_<key>[0].
        r'(data\.)?'                                 # optional data.
        r'(\w+)\.'                                   # resource type
        r'([\w-]+)'                                  # resource name (may contain hyphens)
        r'(?:\[(\d+)\])?'                            # optional [N] index
        r'(?:\["([^"]+)"\])?'                        # optional ["key"] for_each index
        r'$'
    )
    m = re.match(pattern, addr)
    if not m:
        return None

    region_key = m.group(1)
    is_data = m.group(2) is not None
    resource_type = m.group(3)
    resource_name = m.group(4)
    count_index = m.group(5)
    foreach_key = m.group(6)

    # Convert module region key to AWS region
    # eu_central_1 → eu-central-1, us_east_1 → us-east-1
    if region_key == "unified":
        region = "unified"
    else:
        region = region_key.replace("_", "-")

    return {
        "original": addr,
        "region_key": region_key,
        "region": region,
        "is_data": is_data,
        "resource_type": resource_type,
        "resource_name": resource_name,
        "has_count_index": count_index is not None,
        "count_index": count_index,
        "foreach_key": foreach_key,
        "resource_id": f"{'data.' if is_data else ''}{resource_type}.{resource_name}",
    }


def parse_unified_templates(template_paths):
    """Parse unified template files to determine resource count/for_each behavior.

    Returns a list of dicts with:
      - type: resource type (e.g., aws_iam_role)
      - name_pattern: regex pattern for resource name
      - mode: 'for_each', 'count', or 'none'
    """
    resource_patterns = []

    for path in template_paths:
        with open(path) as f:
            lines = f.readlines()

        i = 0
        while i < len(lines):
            line = lines[i].strip()

            # Match resource declarations
            m = re.match(
                r'^resource\s+"(\w+)"\s+"([\w${}./-]+)"\s*\{',
                line,
            )
            if m:
                rtype = m.group(1)
                rname_template = m.group(2)

                # Check next 5 lines for count or for_each
                block_preview = "\n".join(
                    l.strip() for l in lines[i : i + 6]
                )

                if re.search(r'\bfor_each\b', block_preview):
                    mode = "for_each"
                elif re.search(r'\bcount\b', block_preview):
                    mode = "count"
                else:
                    mode = "none"

                # Convert template variable syntax to regex
                # "ntc_baseline_iam_role__${iam_role_name}" → "ntc_baseline_iam_role__[\w-]+"
                # Split on ${...}, escape literal parts, rejoin with regex wildcard
                parts = re.split(r'\$\{[^}]+\}', rname_template)
                escaped_parts = [re.escape(p) for p in parts]
                name_regex = r'[\w-]+'.join(escaped_parts)

                resource_patterns.append({
                    "type": rtype,
                    "name_template": rname_template,
                    "name_pattern": f"^{name_regex}$",
                    "mode": mode,
                    "source_file": os.path.basename(path),
                })

            i += 1

    return resource_patterns


def find_unified_templates(templates_arg):
    """Find unified template files from a path argument.

    Accepts:
      - A directory path → finds all unified_*.tftpl files
      - A comma-separated list of file paths
      - A glob pattern
    """
    if not templates_arg:
        return []

    paths = []

    if os.path.isdir(templates_arg):
        # Directory: find all unified_*.tftpl files
        # Also handles upload prefix patterns like "12345_unified_*.tftpl"
        for f in sorted(os.listdir(templates_arg)):
            if f.endswith(".tftpl") and "unified_" in f:
                paths.append(os.path.join(templates_arg, f))
    elif "," in templates_arg:
        # Comma-separated list
        paths = [p.strip() for p in templates_arg.split(",")]
    else:
        # Glob pattern or single file
        paths = sorted(glob.glob(templates_arg))

    return [p for p in paths if os.path.isfile(p)]


# =============================================================================
# CLASSIFICATION
# =============================================================================

def classify_resources(from_addresses, main_region, template_patterns):
    """Classify each FROM address as data/global/regional and determine TO address.

    Returns a list of dicts with:
      - from_addr: original state address
      - to_addr: generated unified state address
      - category: 'skip_data', 'global_no_count', 'global_with_count', 'regional'
      - region: AWS region
      - confidence: 'template' or 'heuristic'
    """
    parsed = []
    for addr in from_addresses:
        p = parse_state_address(addr)
        if p:
            parsed.append(p)
        else:
            print(f"WARNING: Could not parse address: {addr}", file=sys.stderr)

    # Group by resource_id to detect which resources appear in multiple regions
    by_resource = defaultdict(list)
    for p in parsed:
        by_resource[p["resource_id"]].append(p)

    # Detect regional resources: appear in multiple region modules
    regional_resources = set()
    for rid, entries in by_resource.items():
        regions = set(e["region"] for e in entries)
        if len(regions) > 1:
            regional_resources.add(rid)

    # Build template lookup
    def match_template(resource_type, resource_name):
        """Find matching template pattern and return its mode."""
        for tp in template_patterns:
            if tp["type"] == resource_type:
                if re.match(tp["name_pattern"], resource_name):
                    return tp["mode"], tp["source_file"]
        return None, None

    results = []
    for p in parsed:
        # --- Case: Data source → skip ---
        if p["is_data"]:
            results.append({
                "from_addr": p["original"],
                "to_addr": None,
                "category": "skip_data",
                "region": p["region"],
                "resource_id": p["resource_id"],
                "confidence": "rule",
                "note": "Data sources are re-computed, no moved block needed",
            })
            continue

        # Determine unified mode from templates
        template_mode, source_file = match_template(
            p["resource_type"], p["resource_name"]
        )

        # --- Case: Regional resource ---
        is_regional = False

        if template_mode == "for_each":
            is_regional = True
            confidence = "template"
        elif p["resource_id"] in regional_resources:
            is_regional = True
            confidence = "heuristic" if not template_mode else "template"
        elif p["region"] != main_region and not p["has_count_index"]:
            # In non-main region without [0] → likely regional
            is_regional = True
            confidence = "heuristic"

        if is_regional:
            to_addr = (
                f'module.baseline_unified[0].'
                f'{p["resource_type"]}.{p["resource_name"]}'
                f'["{p["region"]}"]'
            )
            results.append({
                "from_addr": p["original"],
                "to_addr": to_addr,
                "category": "regional",
                "region": p["region"],
                "resource_id": p["resource_id"],
                "confidence": confidence,
                "note": f"Regional → for_each with region key",
            })
            continue

        # --- Case: Global resource ---
        if template_mode == "count":
            # Unified template still has count → keep [0]
            to_addr = (
                f'module.baseline_unified[0].'
                f'{p["resource_type"]}.{p["resource_name"]}[0]'
            )
            results.append({
                "from_addr": p["original"],
                "to_addr": to_addr,
                "category": "global_with_count",
                "region": p["region"],
                "resource_id": p["resource_id"],
                "confidence": "template",
                "note": f"Global, count kept (from {source_file})",
            })
        elif template_mode == "none":
            # Unified template has no count → remove [0]
            to_addr = (
                f'module.baseline_unified[0].'
                f'{p["resource_type"]}.{p["resource_name"]}'
            )
            results.append({
                "from_addr": p["original"],
                "to_addr": to_addr,
                "category": "global_no_count",
                "region": p["region"],
                "resource_id": p["resource_id"],
                "confidence": "template",
                "note": f"Global, count removed (from {source_file})",
            })
        elif template_mode is None:
            # No template match → use heuristic
            # Default: assume count is removed (most common case)
            if p["has_count_index"]:
                to_addr = (
                    f'module.baseline_unified[0].'
                    f'{p["resource_type"]}.{p["resource_name"]}'
                )
                note = "Global, count removed (HEURISTIC — verify!)"
            else:
                to_addr = (
                    f'module.baseline_unified[0].'
                    f'{p["resource_type"]}.{p["resource_name"]}'
                )
                note = "Global, no index (HEURISTIC — verify!)"

            results.append({
                "from_addr": p["original"],
                "to_addr": to_addr,
                "category": "global_heuristic",
                "region": p["region"],
                "resource_id": p["resource_id"],
                "confidence": "heuristic",
                "note": note,
            })
        else:
            # template_mode is for_each but we didn't classify as regional
            # This shouldn't happen, but handle gracefully
            to_addr = (
                f'module.baseline_unified[0].'
                f'{p["resource_type"]}.{p["resource_name"]}'
                f'["{p["region"]}"]'
            )
            results.append({
                "from_addr": p["original"],
                "to_addr": to_addr,
                "category": "regional",
                "region": p["region"],
                "resource_id": p["resource_id"],
                "confidence": "template",
                "note": "Regional (for_each from template)",
            })

    return results


# =============================================================================
# VALIDATION
# =============================================================================

def validate_against_to_state(results, to_addresses):
    """Validate generated TO addresses against actual unified state.

    Returns a list of issues found.
    """
    to_set = set(a.strip() for a in to_addresses if a.strip())
    generated_to = set(
        r["to_addr"] for r in results if r["to_addr"] is not None
    )

    issues = []

    # Check: generated TO addresses that don't exist in actual state
    missing_in_actual = generated_to - to_set
    for addr in sorted(missing_in_actual):
        issues.append({
            "type": "MISSING_IN_ACTUAL",
            "address": addr,
            "message": f"Generated TO address not found in unified state: {addr}",
        })

    # Check: actual TO addresses that we didn't generate a move for
    # (excluding data sources which we skip)
    actual_resources = set(
        a for a in to_set if not a.startswith("module.baseline_unified[0].data.")
    )
    covered = set(
        r["to_addr"] for r in results if r["to_addr"] is not None
    )
    not_covered = actual_resources - covered
    for addr in sorted(not_covered):
        issues.append({
            "type": "NOT_COVERED",
            "address": addr,
            "message": f"Unified resource has no FROM mapping: {addr}",
        })

    return issues


# =============================================================================
# OUTPUT
# =============================================================================

def format_hcl(results, include_comments=True):
    """Format results as HCL baseline_moved_resources block."""
    lines = []
    lines.append("  baseline_moved_resources = [")

    # Group by category for readable output
    categories = [
        ("global_no_count", "GLOBAL RESOURCES — [0] index removed (no count in unified)"),
        ("global_with_count", "GLOBAL RESOURCES — [0] index kept (count still in unified)"),
        ("regional", "REGIONAL RESOURCES — for_each with [\"<region>\"] key"),
        ("global_heuristic", "GLOBAL RESOURCES — heuristic (VERIFY MANUALLY!)"),
    ]

    for cat_key, cat_title in categories:
        cat_results = [r for r in results if r["category"] == cat_key]
        if not cat_results:
            continue

        if include_comments:
            lines.append("")
            lines.append(f"    # {'=' * 70}")
            lines.append(f"    # {cat_title}")
            lines.append(f"    # {'=' * 70}")

        # Group by resource_id for sub-headers
        current_resource = None
        for r in sorted(cat_results, key=lambda x: (x["resource_id"], x["region"])):
            if include_comments and r["resource_id"] != current_resource:
                current_resource = r["resource_id"]
                lines.append("")
                lines.append(f"    # {r['resource_id']}")

            lines.append("    {")
            lines.append(f'      moved_from = "{r["from_addr"]}"')
            lines.append(f'      moved_to   = "{r["to_addr"]}"')
            lines.append("    },")

    lines.append("  ]")
    return "\n".join(lines)


def format_skipped(results):
    """Format skipped data sources as a comment block."""
    skipped = [r for r in results if r["category"] == "skip_data"]
    if not skipped:
        return ""

    lines = [
        "",
        "  # Skipped data sources (re-computed, no moved blocks needed):",
    ]
    for r in sorted(skipped, key=lambda x: x["from_addr"]):
        lines.append(f"  #   {r['from_addr']}")

    return "\n".join(lines)


def print_summary(results, issues=None):
    """Print a summary to stderr."""
    categories = defaultdict(int)
    confidences = defaultdict(int)
    for r in results:
        categories[r["category"]] += 1
        confidences[r["confidence"]] += 1

    print("\n" + "=" * 60, file=sys.stderr)
    print("MIGRATION SUMMARY", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print(f"  Total addresses processed:    {len(results)}", file=sys.stderr)
    print(f"  Data sources (skipped):       {categories.get('skip_data', 0)}", file=sys.stderr)
    print(f"  Global (count removed):       {categories.get('global_no_count', 0)}", file=sys.stderr)
    print(f"  Global (count kept):          {categories.get('global_with_count', 0)}", file=sys.stderr)
    print(f"  Regional (for_each):          {categories.get('regional', 0)}", file=sys.stderr)
    print(f"  Global (heuristic):           {categories.get('global_heuristic', 0)}", file=sys.stderr)
    print(f"  ---", file=sys.stderr)
    print(f"  Moved blocks generated:       {sum(1 for r in results if r['to_addr'])}", file=sys.stderr)
    print(f"  Confidence: template-based:   {confidences.get('template', 0)}", file=sys.stderr)
    print(f"  Confidence: heuristic:        {confidences.get('heuristic', 0)}", file=sys.stderr)

    if confidences.get("heuristic", 0) > 0:
        print(
            "\n  ⚠️  Some moves were generated via heuristic (no template match).",
            file=sys.stderr,
        )
        print(
            "     Review these carefully or provide unified templates with --templates.",
            file=sys.stderr,
        )

    if issues:
        print(f"\n  VALIDATION ISSUES: {len(issues)}", file=sys.stderr)
        for issue in issues:
            icon = "❌" if issue["type"] == "MISSING_IN_ACTUAL" else "⚠️"
            print(f"    {icon} [{issue['type']}] {issue['message']}", file=sys.stderr)
    elif issues is not None:
        print(f"\n  ✅ Validation passed — all moves match unified state.", file=sys.stderr)

    print("=" * 60 + "\n", file=sys.stderr)


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="NTC Account Factory v1→v2 State Migration Tool",
        epilog="See --help for usage examples.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--from-file",
        help="File with legacy state addresses (one per line). Use - for stdin.",
        default="-",
    )
    parser.add_argument(
        "--main-region",
        required=True,
        help="Main baseline region (e.g., eu-central-1).",
    )
    parser.add_argument(
        "--templates",
        help=(
            "Path to unified template files. Can be a directory "
            "(scans for unified_*.tftpl), comma-separated file list, or glob."
        ),
    )
    parser.add_argument(
        "--validate-file",
        help="File with unified state addresses for validation (one per line).",
    )
    parser.add_argument(
        "--output",
        help="Output file (default: stdout).",
        default="-",
    )
    parser.add_argument(
        "--no-comments",
        action="store_true",
        help="Omit comments in output.",
    )
    parser.add_argument(
        "--show-skipped",
        action="store_true",
        help="Include skipped data sources as comments in output.",
    )

    args = parser.parse_args()

    # --- Read FROM state list ---
    if args.from_file == "-":
        from_lines = sys.stdin.readlines()
    else:
        with open(args.from_file) as f:
            from_lines = f.readlines()

    from_addresses = [
        line.strip() for line in from_lines if line.strip() and not line.strip().startswith("#")
    ]

    if not from_addresses:
        print("ERROR: No state addresses provided.", file=sys.stderr)
        sys.exit(1)

    print(f"Read {len(from_addresses)} state addresses.", file=sys.stderr)

    # --- Parse unified templates ---
    template_patterns = []
    if args.templates:
        template_files = find_unified_templates(args.templates)
        if template_files:
            print(
                f"Parsing {len(template_files)} unified templates: "
                f"{', '.join(os.path.basename(f) for f in template_files)}",
                file=sys.stderr,
            )
            template_patterns = parse_unified_templates(template_files)
            print(
                f"Found {len(template_patterns)} resource patterns in templates.",
                file=sys.stderr,
            )
        else:
            print(
                f"WARNING: No unified templates found at '{args.templates}'.",
                file=sys.stderr,
            )
    else:
        print(
            "WARNING: No templates provided — using heuristic mode. "
            "Consider using --templates for accurate results.",
            file=sys.stderr,
        )

    # --- Classify and generate moves ---
    results = classify_resources(from_addresses, args.main_region, template_patterns)

    # --- Validate if TO state provided ---
    issues = None
    if args.validate_file:
        with open(args.validate_file) as f:
            to_lines = f.readlines()
        to_addresses = [
            line.strip()
            for line in to_lines
            if line.strip() and not line.strip().startswith("#")
        ]
        issues = validate_against_to_state(results, to_addresses)

    # --- Print summary ---
    print_summary(results, issues)

    # --- Generate output ---
    output = format_hcl(results, include_comments=not args.no_comments)
    if args.show_skipped:
        output += format_skipped(results)

    if args.output == "-":
        print(output)
    else:
        with open(args.output, "w") as f:
            f.write(output + "\n")
        print(f"Output written to: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
