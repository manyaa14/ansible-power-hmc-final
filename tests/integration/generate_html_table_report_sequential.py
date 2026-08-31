#!/usr/bin/env python3
"""
Sequential HTML report generator for ansible-test integration output.

This version generates the same table-style HTML report but maps failures to
testcases by anchoring them to testcase block-start tasks found in the
completed log. This avoids shifting failures across adjacent testcase rows when
task names are repeated.
"""

import re
import sys
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET


class SequentialTableReportGenerator:
    def __init__(self, test_output_file=None):
        self.test_output_file = test_output_file
        self.start_time = datetime.now()
        self.report_dir = Path('tests/integration/test_reports')
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.module_results = {}
        self._registry_cache = {}
        self._signature_cache = {}

    def parse_ansible_test_output(self, output_text):
        lines = output_text.splitlines()
        module_signatures = {}

        for line in lines:
            marker_match = self._parse_testcase_marker(line)
            if marker_match:
                module_name = marker_match['module']
                self._ensure_module(module_name)
                if marker_match['event'] == 'start':
                    self._ensure_test_case(module_name, marker_match['playbook'])
                module_signatures.setdefault(module_name, self._get_module_signatures(module_name))
                continue

            if 'Running' in line and 'integration test role' in line:
                match = re.search(r'Running (\w+) integration test role', line)
                if match:
                    module_name = match.group(1)
                    self._ensure_module(module_name)
                    module_signatures.setdefault(module_name, self._get_module_signatures(module_name))
                continue

            if 'included:' in line and 'playbooks/' in line:
                module_match = re.search(r'targets/([^/]+)/', line)
                if module_match:
                    module_name = module_match.group(1)
                    self._ensure_module(module_name)
                    module_signatures.setdefault(module_name, self._get_module_signatures(module_name))
                continue

        module_boundaries = self._extract_testcase_boundaries(lines)
        module_failures = self._extract_failure_sequences(lines)

        for module_name, signatures in module_signatures.items():
            self._ensure_module(module_name)

            boundaries = module_boundaries.get(module_name, [])
            failures = module_failures.get(module_name, [])

            for failure in failures:
                testcase_name = self._find_testcase_for_failure(
                    module_name,
                    failure,
                    boundaries,
                    signatures,
                )
                if testcase_name:
                    self._mark_test_failed(module_name, testcase_name, failure['failure_text'])

        self._finalize_counts()

    def _initialize_module_testcases(self, module_name):
        for testcase in self._get_module_testcase_order(module_name):
            self._ensure_test_case(module_name, testcase)

    def _get_module_testcase_order(self, module_name):
        if module_name in self._registry_cache:
            return self._registry_cache[module_name]

        registry_path = Path('tests/integration/targets') / module_name / 'tasks' / 'testcaseRegistery.xml'
        if not registry_path.exists():
            self._registry_cache[module_name] = []
            return self._registry_cache[module_name]

        tree = ET.parse(registry_path)
        root = tree.getroot()
        self._registry_cache[module_name] = [
            node.text.strip() for node in root.findall('.//TestCase') if node.text and node.text.strip()
        ]
        return self._registry_cache[module_name]

    def _extract_testcase_boundaries(self, lines):
        boundaries_by_module = {}
        current_module = None
        module_signatures = {}
        has_marker_boundaries = False

        for line_number, line in enumerate(lines, start=1):
            marker_match = self._parse_testcase_marker(line)
            if marker_match:
                current_module = marker_match['module']
                if marker_match['event'] == 'start':
                    has_marker_boundaries = True
                    boundaries_by_module.setdefault(current_module, []).append({
                        'testcase_name': marker_match['playbook'],
                        'task_name': marker_match['playbook'],
                        'line_number': line_number,
                    })
                continue

            if 'Running' in line and 'integration test role' in line:
                match = re.search(r'Running (\w+) integration test role', line)
                if match:
                    current_module = match.group(1)
                    module_signatures.setdefault(current_module, self._get_module_signatures(current_module))
                continue

            if has_marker_boundaries:
                continue

            task_match = re.search(r'TASK \[([^\]]+)\]', line)
            if not task_match:
                continue

            task_text = task_match.group(1)
            task_parts = task_text.split(' : ', 1)
            if len(task_parts) < 2:
                continue

            detected_module = task_parts[0].strip()
            current_task_name = task_parts[1].strip()
            if not self._looks_like_module(detected_module):
                continue

            current_module = detected_module
            module_signatures.setdefault(current_module, self._get_module_signatures(current_module))
            testcase_name = self._match_block_start_task(
                current_module,
                current_task_name,
                module_signatures[current_module],
            )
            if testcase_name:
                boundaries_by_module.setdefault(current_module, []).append({
                    'testcase_name': testcase_name,
                    'task_name': current_task_name,
                    'line_number': line_number,
                })

        return boundaries_by_module

    def _match_block_start_task(self, module_name, task_name, module_signatures):
        if not task_name or 'mark the testcase as failed' in task_name.lower():
            return None

        normalized_task = self._normalize_name(task_name)
        best_match = None
        best_score = 0

        for testcase_name in self._get_module_testcase_order(module_name):
            signatures = module_signatures.get(testcase_name, [])
            if not signatures:
                continue

            block_start_signature = signatures[1] if len(signatures) > 1 else signatures[0]
            score = self._score_name_match(normalized_task, block_start_signature)
            if score > best_score:
                best_score = score
                best_match = testcase_name

        return best_match if best_score > 0 else None

    def _extract_failure_sequences(self, lines):
        failures_by_module = {}
        current_module = None
        current_testcase = None
        current_task_name = None
        current_task_line = 0
        current_failure_lines = []
        current_failure_module = None
        current_failure_testcase = None
        current_failure_task = None
        current_failure_line = 0

        for line_number, line in enumerate(lines, start=1):
            marker_match = self._parse_testcase_marker(line)
            if marker_match:
                if current_failure_lines:
                    failure_text = '\n'.join(current_failure_lines).strip()
                    if '...ignoring' not in failure_text and current_failure_module and current_failure_task:
                        failures_by_module.setdefault(current_failure_module, []).append({
                            'testcase_name': current_failure_testcase,
                            'task_name': current_failure_task,
                            'failure_text': failure_text,
                            'line_number': current_failure_line,
                        })
                    current_failure_lines = []
                    current_failure_module = None
                    current_failure_testcase = None
                    current_failure_task = None
                    current_failure_line = 0

                current_module = marker_match['module']
                if marker_match['event'] == 'start':
                    current_testcase = marker_match['playbook']
                elif marker_match['event'] == 'end' and current_testcase == marker_match['playbook']:
                    current_testcase = None
                continue

            task_match = re.search(r'TASK \[([^\]]+)\]', line)
            if task_match:
                if current_failure_lines:
                    failure_text = '\n'.join(current_failure_lines).strip()
                    if '...ignoring' not in failure_text and current_failure_module and current_failure_task:
                        failures_by_module.setdefault(current_failure_module, []).append({
                            'testcase_name': current_failure_testcase,
                            'task_name': current_failure_task,
                            'failure_text': failure_text,
                            'line_number': current_failure_line,
                        })
                    current_failure_lines = []
                    current_failure_module = None
                    current_failure_testcase = None
                    current_failure_task = None
                    current_failure_line = 0

                task_text = task_match.group(1)
                task_parts = task_text.split(' : ', 1)
                if len(task_parts) >= 2 and self._looks_like_module(task_parts[0].strip()):
                    current_module = task_parts[0].strip()
                    current_task_name = task_parts[1].strip()
                    current_task_line = line_number
                continue

            if re.match(r'^failed:', line) or 'fatal:' in line or 'FAILED!' in line:
                current_failure_lines = [line]
                current_failure_module = current_module
                current_failure_testcase = current_testcase
                current_failure_task = current_task_name
                current_failure_line = current_task_line
                continue

            if current_failure_lines:
                current_failure_lines.append(line)

        if current_failure_lines:
            failure_text = '\n'.join(current_failure_lines).strip()
            if '...ignoring' not in failure_text and current_failure_module and current_failure_task:
                failures_by_module.setdefault(current_failure_module, []).append({
                    'testcase_name': current_failure_testcase,
                    'task_name': current_failure_task,
                    'failure_text': failure_text,
                    'line_number': current_failure_line,
                })

        return failures_by_module

    def _find_testcase_for_failure(self, module_name, failure, boundaries, module_signatures):
        explicit_testcase = failure.get('testcase_name')
        if explicit_testcase:
            return explicit_testcase

        failure_line = failure.get('line_number', 0)
        best_boundary = None

        for boundary in boundaries:
            if boundary['line_number'] <= failure_line:
                if best_boundary is None or boundary['line_number'] > best_boundary['line_number']:
                    best_boundary = boundary

        if best_boundary:
            return best_boundary['testcase_name']

        return self._match_block_start_task(
            module_name,
            failure.get('task_name', ''),
            module_signatures,
        )

    def _parse_testcase_marker(self, line):
        marker_match = re.search(
            r'TESTCASE_(START|END)\|module=([^|]+)\|playbook=([^|]+)\|id=([^|\s]+)',
            line,
        )
        if not marker_match:
            return None

        return {
            'event': marker_match.group(1).lower(),
            'module': marker_match.group(2).strip(),
            'playbook': marker_match.group(3).strip(),
            'id': marker_match.group(4).strip(),
        }

    def _get_module_signatures(self, module_name):
        if module_name in self._signature_cache:
            return self._signature_cache[module_name]

        signatures = {}
        playbook_dir = Path('tests/integration/targets') / module_name / 'tasks' / 'playbooks'

        for testcase_name in self._get_module_testcase_order(module_name):
            testcase_signatures = [self._normalize_name(Path(testcase_name).stem)]
            playbook_path = playbook_dir / testcase_name
            if playbook_path.exists():
                for line in playbook_path.read_text(encoding='utf-8').splitlines():
                    stripped = line.strip()
                    if stripped.startswith('- name:'):
                        task_name = stripped.split(':', 1)[1].strip()
                        normalized = self._normalize_name(task_name)
                        if normalized and normalized not in testcase_signatures:
                            testcase_signatures.append(normalized)
            signatures[testcase_name] = testcase_signatures

        self._signature_cache[module_name] = signatures
        return signatures

    def _normalize_name(self, value):
        value = value.lower()
        value = value.replace('&', ' and ')
        value = re.sub(r'[^a-z0-9]+', ' ', value)
        value = re.sub(r'\s+', ' ', value).strip()
        return value

    def _score_name_match(self, left_name, right_name):
        left_tokens = {token for token in left_name.split() if token}
        right_tokens = {token for token in right_name.split() if token}
        if not left_tokens or not right_tokens:
            return 0

        overlap = len(left_tokens & right_tokens)
        if overlap == 0:
            return 0

        if left_name in right_name or right_name in left_name:
            overlap += 5

        return overlap

    def _looks_like_module(self, module_name):
        return (
            module_name.startswith('hmc_')
            or module_name.startswith('vios')
            or module_name.startswith('power')
            or module_name.startswith('powervm')
        )

    def _ensure_module(self, module_name):
        if module_name not in self.module_results:
            self.module_results[module_name] = {
                'test_cases': [],
                'test_case_index': {},
                'passed': 0,
                'failed': 0,
            }

    def _ensure_test_case(self, module_name, test_case_name):
        self._ensure_module(module_name)
        module_data = self.module_results[module_name]
        if test_case_name not in module_data['test_case_index']:
            test_case = {
                'test_case': test_case_name,
                'status': 'PASS',
                'rescue_task': '',
            }
            module_data['test_cases'].append(test_case)
            module_data['test_case_index'][test_case_name] = test_case

    def _mark_test_failed(self, module_name, test_case_name, failure_text):
        self._ensure_test_case(module_name, test_case_name)
        test_case = self.module_results[module_name]['test_case_index'][test_case_name]
        test_case['status'] = 'FAIL'
        if not test_case['rescue_task']:
            test_case['rescue_task'] = failure_text

    def _finalize_counts(self):
        for module_data in self.module_results.values():
            module_data['failed'] = sum(
                1 for test_case in module_data['test_cases'] if test_case['status'] == 'FAIL'
            )
            module_data['passed'] = sum(
                1 for test_case in module_data['test_cases'] if test_case['status'] == 'PASS'
            )

    def generate_reports(self):
        if not self.module_results:
            print('No test results to generate reports from.')
            return

        duration = (datetime.now() - self.start_time).total_seconds()

        for module_name, results in self.module_results.items():
            if results['test_cases']:
                self._generate_module_report(module_name, results, duration)

        self._generate_summary_report(duration)

        print(f"\n{'=' * 80}")
        print(f"📊 HTML Reports Generated in: {self.report_dir.absolute()}")
        print(f"{'=' * 80}\n")

    def _generate_module_report(self, module_name, results, duration):
        report_file = self.report_dir / f'{module_name}_sequential_table_report.html'

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Test Report - {module_name}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 20px;
            min-height: 100vh;
        }}
        .container {{
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            border-radius: 10px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
            overflow: hidden;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            text-align: center;
        }}
        .header h1 {{ font-size: 2.4em; margin-bottom: 10px; }}
        .summary {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            padding: 30px;
            background: #f8f9fa;
        }}
        .summary-card {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            text-align: center;
        }}
        .summary-card .number {{
            font-size: 2.5em;
            font-weight: bold;
            margin: 10px 0;
        }}
        .passed {{ color: #28a745; }}
        .failed {{ color: #dc3545; }}
        .total {{ color: #007bff; }}
        .content {{ padding: 30px; }}
        .back-link {{
            display: inline-block;
            margin-bottom: 20px;
            color: #667eea;
            text-decoration: none;
            font-weight: 600;
        }}
        .table-wrapper {{
            overflow-x: auto;
            border: 1px solid #dee2e6;
            border-radius: 8px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            background: white;
        }}
        thead {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
        }}
        th {{
            padding: 16px;
            text-align: left;
            font-size: 0.9em;
            text-transform: uppercase;
            letter-spacing: 0.4px;
        }}
        td {{
            padding: 16px;
            border-top: 1px solid #dee2e6;
            vertical-align: top;
        }}
        tbody tr:nth-child(even) {{
            background: #f8f9fa;
        }}
        .status-badge {{
            display: inline-block;
            padding: 6px 14px;
            border-radius: 999px;
            font-size: 0.85em;
            font-weight: bold;
        }}
        .status-pass {{
            background: #d4edda;
            color: #155724;
        }}
        .status-fail {{
            background: #f8d7da;
            color: #721c24;
        }}
        .details {{
            font-family: 'Courier New', monospace;
            font-size: 0.88em;
            white-space: pre-wrap;
            word-break: break-word;
            color: #333;
        }}
        .empty-details {{
            color: #999;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🧪 Integration Test Report</h1>
            <div>Module: {module_name}</div>
            <div>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
        </div>

        <div class="summary">
            <div class="summary-card">
                <div>Total Tests</div>
                <div class="number total">{results['passed'] + results['failed']}</div>
            </div>
            <div class="summary-card">
                <div>Passed</div>
                <div class="number passed">{results['passed']}</div>
            </div>
            <div class="summary-card">
                <div>Failed</div>
                <div class="number failed">{results['failed']}</div>
            </div>
            <div class="summary-card">
                <div>Duration</div>
                <div class="number">{duration:.2f}s</div>
            </div>
        </div>

        <div class="content">
            <a class="back-link" href="test_summary_sequential.html">← Back to Summary</a>
            <h2 style="margin-bottom: 20px;">Test Cases</h2>
            <div class="table-wrapper">
                <table>
                    <thead>
                        <tr>
                            <th>Testcase / Playbook</th>
                            <th>Status</th>
                            <th>Failure Details</th>
                        </tr>
                    </thead>
                    <tbody>
"""

        for test_case in results['test_cases']:
            escaped_name = self._escape_html(test_case['test_case'])
            escaped_rescue = self._escape_html(test_case['rescue_task']) if test_case['rescue_task'] else '-'
            status_class = 'status-fail' if test_case['status'] == 'FAIL' else 'status-pass'
            empty_class = ' empty-details' if escaped_rescue == '-' else ''

            html += f"""
                        <tr>
                            <td>{escaped_name}</td>
                            <td><span class="status-badge {status_class}">{test_case['status']}</span></td>
                            <td><div class="details{empty_class}">{escaped_rescue}</div></td>
                        </tr>
"""

        html += """
                    </tbody>
                </table>
            </div>
        </div>
    </div>
</body>
</html>
"""

        report_file.write_text(html, encoding='utf-8')
        print(f"✅ Generated: {report_file}")

    def _generate_summary_report(self, duration):
        report_file = self.report_dir / 'test_summary_sequential.html'

        total_passed = sum(r['passed'] for r in self.module_results.values())
        total_failed = sum(r['failed'] for r in self.module_results.values())

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Test Summary</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 20px;
            min-height: 100vh;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            border-radius: 10px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 40px;
            text-align: center;
        }}
        .header h1 {{ font-size: 3em; }}
        .summary {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            padding: 30px;
            background: #f8f9fa;
        }}
        .summary-card {{
            background: white;
            padding: 25px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            text-align: center;
        }}
        .summary-card .number {{
            font-size: 3em;
            font-weight: bold;
            margin: 10px 0;
        }}
        .passed {{ color: #28a745; }}
        .failed {{ color: #dc3545; }}
        .total {{ color: #007bff; }}
        .content {{ padding: 30px; }}
        .module-card {{
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            margin-bottom: 20px;
            padding: 20px;
        }}
        .module-name {{
            font-size: 1.3em;
            font-weight: bold;
            margin-bottom: 10px;
        }}
        .module-link a {{
            color: #007bff;
            text-decoration: none;
            font-weight: bold;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📊 Test Summary</h1>
            <div>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
        </div>

        <div class="summary">
            <div class="summary-card">
                <div>Total Tests</div>
                <div class="number total">{total_passed + total_failed}</div>
            </div>
            <div class="summary-card">
                <div>Passed</div>
                <div class="number passed">{total_passed}</div>
            </div>
            <div class="summary-card">
                <div>Failed</div>
                <div class="number failed">{total_failed}</div>
            </div>
            <div class="summary-card">
                <div>Duration</div>
                <div class="number">{duration:.2f}s</div>
            </div>
        </div>

        <div class="content">
            <h2 style="margin-bottom: 20px;">Module Results</h2>
"""

        for module_name, results in sorted(self.module_results.items()):
            html += f"""
            <div class="module-card">
                <div class="module-name">📦 {self._escape_html(module_name)}</div>
                <div>Passed: <span class="passed">{results['passed']}</span> | Failed: <span class="failed">{results['failed']}</span></div>
                <div class="module-link">
                    <a href="{module_name}_sequential_table_report.html">View Detailed Report →</a>
                </div>
            </div>
"""

        html += """
        </div>
    </div>
</body>
</html>
"""

        report_file.write_text(html, encoding='utf-8')
        print(f"✅ Generated: {report_file}")

    def _escape_html(self, text):
        text = str(text)
        text = text.replace('&', '&')
        text = text.replace('<', '<')
        text = text.replace('>', '>')
        text = text.replace('"', '"')
        return text


def main():
    if len(sys.argv) > 1:
        output_file = sys.argv[1]
        with open(output_file, 'r', encoding='utf-8') as file_obj:
            output_text = file_obj.read()
    else:
        print('Reading test output from stdin...')
        print('Paste the ansible-test output and press Ctrl+D when done:')
        output_text = sys.stdin.read()

    generator = SequentialTableReportGenerator()
    generator.parse_ansible_test_output(output_text)
    generator.generate_reports()


if __name__ == '__main__':
    main()

# Made with Bob
