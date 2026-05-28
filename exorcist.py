#!/usr/bin/env python3
# filename: exorcist.py
"""
exorcist.py — Logic App structural diagnostic tool.

Scans for fatal flaws in Logic App definitions: dangling runAfter
references, orphaned actions, scope violations, and structural
problems that cause Portal rejections or silent runtime failures.

Wrapper around Cartographer outputs. Runs the 4 logical validators
on flow_model.json, app_structure.yaml, and variables.json.

Reads configuration from exorcist_rules.yaml for house rules, error
signatures, and cartographer failure modes.

Returns 0 (pass) or non-zero error code.

Usage:
  exorcist.py --flow-model flow_model.json
  exorcist.py --app-dir /path/to/cartographer/output
  exorcist.py --flow-model flow_model.json --verbose
  exorcist.py --flow-model flow_model.json --config /path/to/exorcist_rules.yaml

Exit Codes:
  0    = All checks passed
  1    = Cycle detected
  2    = Orphans found
  3    = Dangling references
  4    = Scope violations
  5    = Multiple errors (use --verbose to see all)
  10   = Cartographer failed (see stderr)
  11   = I/O error reading files
  20   = Invalid input
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

try:
    import yaml
except ImportError:
    yaml = None


class ConfigLoader:
    """Load and merge exorcist configuration."""
    
    DEFAULT_CONFIG = {
        'validation': {
            'cycles': {'enabled': True, 'severity': 'critical', 'exit_code': 1},
            'orphans': {'enabled': True, 'severity': 'high', 'exit_code': 2},
            'dangling_references': {'enabled': True, 'severity': 'critical', 'exit_code': 3},
            'scope_violations': {'enabled': True, 'severity': 'medium', 'exit_code': 4},
        },
        'house_rules': {
            'max_steps': 500,
            'max_scope_depth': 5,
            'max_variables': 100,
        },
        'cartographer': {
            'timeout': 60,
            'required_outputs': ['flow_model.json'],
        },
        'exit_codes': {
            'pass': 0,
            'cycles': 1,
            'orphans': 2,
            'dangling_refs': 3,
            'scope_violations': 4,
            'multiple_errors': 5,
            'cartographer_failed': 10,
        }
    }
    
    @staticmethod
    def load(config_path: Optional[str] = None) -> Dict[str, Any]:
        """Load config from YAML file or return defaults."""
        config = ConfigLoader.DEFAULT_CONFIG.copy()
        
        if not config_path:
            # Try to find default config in common locations
            for candidate in [
                'exorcist_rules.yaml',
                './exorcist_rules.yaml',
                Path.home() / '.exorcist_rules.yaml'
            ]:
                if Path(candidate).exists():
                    config_path = candidate
                    break
        
        if config_path and Path(config_path).exists():
            if yaml is None:
                print("WARNING: pyyaml not installed, using default config", file=sys.stderr)
                return config
            
            try:
                with open(config_path) as f:
                    user_config = yaml.safe_load(f) or {}
                # Merge user config (shallow merge, could be improved)
                for section, values in user_config.items():
                    if isinstance(values, dict) and section in config:
                        config[section].update(values)
                    else:
                        config[section] = values
                print(f"✓ Loaded config from {config_path}", file=sys.stderr)
            except Exception as e:
                print(f"WARNING: Failed to load config from {config_path}: {e}", file=sys.stderr)
        
        return config


class SanityChecker:
    """Validates Logic App logical flow using Cartographer outputs."""

    def __init__(self, verbose=False, config: Optional[Dict[str, Any]] = None):
        self.verbose = verbose
        self.config = config or ConfigLoader.DEFAULT_CONFIG
        self.errors = {
            'cycles': [],
            'orphans': [],
            'dangling': [],
            'scope_violations': []
        }

    def run(self, flow_model_path: str) -> Tuple[bool, int]:
        """
        Run all structural checks on a flow_model.json.
        
        Handles both v1 (complete parse only) and v2+ (partial parse capable).
        
        Returns:
            (passed: bool, exit_code: int)
        """
        try:
            with open(flow_model_path) as f:
                flow_model = json.load(f)
        except FileNotFoundError:
            print(f"ERROR: flow_model.json not found: {flow_model_path}", file=sys.stderr)
            return False, 10
        except json.JSONDecodeError as e:
            print(f"ERROR: Invalid JSON in flow_model.json: {e}", file=sys.stderr)
            return False, 10
        except Exception as e:
            print(f"ERROR: Failed to read flow_model.json: {e}", file=sys.stderr)
            return False, 11

        # Validate structure
        if 'nodes' not in flow_model:
            print("ERROR: flow_model.json missing 'nodes' key", file=sys.stderr)
            return False, 10

        # Check for partial parse (Cartographer v2+)
        parse_status = flow_model.get('parse_status', 'complete')
        parse_errors = flow_model.get('parse_errors', [])
        coverage = flow_model.get('coverage', {})

        if parse_status == 'partial':
            # Cartographer v2+ partial parse
            if self.verbose:
                print(f"\n[Cartographer v2+ Partial Parse]", file=sys.stderr)
                print(f"  Status: {parse_status}", file=sys.stderr)
                print(f"  Coverage: {coverage}", file=sys.stderr)
                if parse_errors:
                    print(f"  Parse errors: {len(parse_errors)}", file=sys.stderr)
            
            # Check if partial parse is acceptable
            partial_config = self.config.get('cartographer', {}).get('partial_parse', {})
            if not partial_config.get('accept_partial', False):
                print("ERROR: Partial parse not accepted (Cartographer v1 mode)", file=sys.stderr)
                return False, 10
            
            # Check coverage thresholds
            min_action = partial_config.get('min_action_coverage', 80)
            min_expr = partial_config.get('min_expression_coverage', 70)
            min_var = partial_config.get('min_variable_coverage', 90)
            
            actual_action = coverage.get('actions', 0)
            actual_expr = coverage.get('expressions', 0)
            actual_var = coverage.get('variables', 0)
            
            coverage_ok = (actual_action >= min_action and 
                          actual_expr >= min_expr and 
                          actual_var >= min_var)
            
            if not coverage_ok:
                severity = partial_config.get('coverage_failure_severity', 'warning')
                msg = f"Partial parse coverage insufficient: actions={actual_action}% (min {min_action}%), expressions={actual_expr}% (min {min_expr}%), variables={actual_var}% (min {min_var}%)"
                if self.verbose:
                    print(f"\n⚠️ {msg}", file=sys.stderr)
                
                if severity == 'critical':
                    return False, 10  # Cartographer failed (insufficient coverage)
                # Otherwise continue with warning

        nodes = flow_model['nodes']
        if not nodes:
            print("WARNING: flow_model.json contains no steps", file=sys.stderr)
            return True, 0

        # Run validators
        self._check_cycles(nodes)
        self._check_orphans(flow_model.get('root_children', []), nodes)
        self._check_dangling_references(nodes)
        self._check_scope_violations(nodes)

        # Determine exit code
        return self._get_verdict()

    def _check_cycles(self, nodes: Dict) -> None:
        """DFS cycle detection."""
        visited = set()
        rec_stack = set()
        cycles = []

        def dfs(node_name, path):
            visited.add(node_name)
            rec_stack.add(node_name)
            path = path + [node_name]

            node = nodes.get(node_name, {})
            for successor in node.get('successors', []):
                if successor not in visited:
                    if dfs(successor, path):
                        return True
                elif successor in rec_stack:
                    cycle_start_idx = path.index(successor)
                    cycle = path[cycle_start_idx:] + [successor]
                    cycles.append(cycle)
                    return True

            rec_stack.remove(node_name)
            return False

        for step_name in nodes:
            if step_name not in visited:
                dfs(step_name, [])

        if cycles:
            self.errors['cycles'] = cycles
            for cycle in cycles:
                if self.verbose:
                    print(f"  Cycle: {' → '.join(cycle)}", file=sys.stderr)

    def _check_orphans(self, root_children: List[str], nodes: Dict) -> None:
        """BFS orphan detection."""
        visited = set()
        queue = root_children.copy()

        while queue:
            step_name = queue.pop(0)
            if step_name in visited:
                continue
            visited.add(step_name)

            node = nodes.get(step_name, {})
            for successor in node.get('successors', []):
                if successor not in visited:
                    queue.append(successor)

        orphans = [name for name in nodes if name not in visited]
        if orphans:
            self.errors['orphans'] = orphans
            if self.verbose:
                for orphan in orphans:
                    print(f"  Orphan: {orphan}", file=sys.stderr)

    def _check_dangling_references(self, nodes: Dict) -> None:
        """Verify all runAfter references exist."""
        all_step_names = set(nodes.keys())
        dangling = []

        for step_name, node in nodes.items():
            for predecessor in node.get('predecessors', []):
                if predecessor not in all_step_names:
                    dangling.append({
                        'step': step_name,
                        'missing': predecessor
                    })

        if dangling:
            self.errors['dangling'] = dangling
            if self.verbose:
                for item in dangling:
                    print(f"  Dangling: {item['step']} runAfter {item['missing']}", file=sys.stderr)

    def _check_scope_violations(self, nodes: Dict) -> None:
        """Check for cross-scope runAfter violations."""
        violations = []

        for step_name, node in nodes.items():
            step_parent = node.get('parent')

            for predecessor in node.get('predecessors', []):
                pred_node = nodes.get(predecessor, {})
                pred_parent = pred_node.get('parent')

                # Simple check: if step is in a scope and predecessor is not in same scope,
                # it's a potential violation (simplified for MVP)
                if step_parent and pred_parent and step_parent != pred_parent:
                    # Could be valid (parent→child), but flag for review
                    violations.append({
                        'step': step_name,
                        'step_scope': step_parent,
                        'pred': predecessor,
                        'pred_scope': pred_parent
                    })

        if violations:
            self.errors['scope_violations'] = violations
            if self.verbose:
                for v in violations:
                    print(f"  Cross-scope: {v['step']} (in {v['step_scope']}) depends on {v['pred']} (in {v['pred_scope']})", file=sys.stderr)

    def _get_verdict(self) -> Tuple[bool, int]:
        """Determine overall pass/fail and exit code."""
        if self.errors['cycles']:
            return False, 1
        if self.errors['orphans']:
            return False, 2
        if self.errors['dangling']:
            return False, 3
        if self.errors['scope_violations']:
            return False, 4
        return True, 0

    def print_report(self) -> None:
        """Print full report if verbose."""
        if not self.verbose:
            return

        print("\n=== SANITY CHECK REPORT ===\n", file=sys.stderr)

        if self.errors['cycles']:
            print(f"❌ CYCLES ({len(self.errors['cycles'])}):", file=sys.stderr)
            for cycle in self.errors['cycles']:
                print(f"   {' → '.join(cycle)}", file=sys.stderr)

        if self.errors['orphans']:
            print(f"❌ ORPHANS ({len(self.errors['orphans'])}):", file=sys.stderr)
            for orphan in self.errors['orphans']:
                print(f"   {orphan}", file=sys.stderr)

        if self.errors['dangling']:
            print(f"❌ DANGLING REFS ({len(self.errors['dangling'])}):", file=sys.stderr)
            for item in self.errors['dangling']:
                print(f"   {item['step']} → {item['missing']}", file=sys.stderr)

        if self.errors['scope_violations']:
            print(f"⚠️  SCOPE VIOLATIONS ({len(self.errors['scope_violations'])}):", file=sys.stderr)
            for v in self.errors['scope_violations']:
                print(f"   {v['step']} (in {v['step_scope']}) depends on {v['pred']} (in {v['pred_scope']})", file=sys.stderr)

        if not any(self.errors.values()):
            print("✅ ALL CHECKS PASSED", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Exorcist — structural diagnostic for Logic App definitions"
    )
    parser.add_argument(
        '--flow-model',
        required=True,
        help='Path to flow_model.json (from Cartographer)'
    )
    parser.add_argument(
        '--config',
        help='Path to exorcist_rules.yaml config file (optional)'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Print detailed report'
    )
    parser.add_argument(
        '--json',
        action='store_true',
        help='Output result as JSON (for machine parsing)'
    )

    args = parser.parse_args()

    # Load config
    config = ConfigLoader.load(args.config)

    checker = SanityChecker(verbose=args.verbose, config=config)
    passed, exit_code = checker.run(args.flow_model)

    if args.json:
        result = {
            'passed': passed,
            'exit_code': exit_code,
            'errors': {k: v for k, v in checker.errors.items() if v}
        }
        print(json.dumps(result))
    else:
        if args.verbose:
            checker.print_report()

        if passed:
            print("✅ PASS")
        else:
            error_names = [k for k, v in checker.errors.items() if v]
            print(f"❌ FAIL: {', '.join(error_names)}", file=sys.stderr)

    sys.exit(exit_code)


if __name__ == '__main__':
    main()
