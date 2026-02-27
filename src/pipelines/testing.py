"""
Infrastructure Testing Pipeline
════════════════════════════════════════════════════════════════

After a template deploys successfully, this pipeline:

  1. **Generates** Python test scripts via the Copilot SDK, tailored
     to the specific resource types that were deployed.
  2. **Executes** those tests against the live Azure environment.
  3. **Analyzes** any failures to determine root cause (template bug,
     test bug, transient Azure issue).
  4. **Feeds back** to the validation pipeline — requesting a template
     revision if tests reveal an infrastructure defect.

NDJSON event phases emitted:

  {"phase": "testing_start",       ...}
  {"phase": "testing_generate",    ...}   — test script being written
  {"phase": "testing_execute",     ...}   — tests running
  {"phase": "test_result",         ...}   — individual test pass/fail
  {"phase": "testing_analyze",     ...}   — analyzing failures
  {"phase": "testing_complete",    ...}   — all done
  {"phase": "testing_feedback",    ...}   — revision requested

These events are consumed by _renderDeployProgress() in the frontend
under the "Test" stage of the pipeline flowchart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import traceback
from typing import AsyncGenerator, Optional

logger = logging.getLogger("infraforge.pipeline.testing")


# ══════════════════════════════════════════════════════════════
# TEST GENERATION
# ══════════════════════════════════════════════════════════════

async def generate_test_script(
    arm_template: dict,
    resource_group: str,
    deployed_resources: list[dict],
    region: str = "eastus2",
) -> str:
    """Use the Copilot SDK to generate a Python test script.

    The LLM receives the ARM template and the list of actually-deployed
    resources (with types, names, properties) and writes test functions
    that verify the infrastructure is functional.

    Returns the raw Python test script as a string.
    """
    from src.agents import INFRA_TESTER
    from src.copilot_helpers import copilot_send
    from src.model_router import get_model_for_task

    # Build a concise resource summary for the LLM
    resource_summary = []
    for r in deployed_resources:
        entry = {
            "name": r.get("name", "unknown"),
            "type": r.get("type", "unknown"),
            "location": r.get("location", region),
        }
        # Include key properties that inform test generation
        props = r.get("properties", {})
        if props:
            # Extract useful testing info
            if "hostNames" in props:
                entry["hostNames"] = props["hostNames"]
            if "defaultHostName" in props:
                entry["defaultHostName"] = props["defaultHostName"]
            if "fullyQualifiedDomainName" in props:
                entry["fqdn"] = props["fullyQualifiedDomainName"]
            if "provisioningState" in props:
                entry["provisioningState"] = props["provisioningState"]
            if "httpsOnly" in props:
                entry["httpsOnly"] = props["httpsOnly"]
            if "siteConfig" in props:
                sc = props["siteConfig"]
                if isinstance(sc, dict):
                    entry["linuxFxVersion"] = sc.get("linuxFxVersion", "")
                    entry["minTlsVersion"] = sc.get("minTlsVersion", "")
            if "sku" in props:
                entry["sku"] = props["sku"]
            if "kind" in props:
                entry["kind"] = props["kind"]
        resource_summary.append(entry)

    # Build the prompt
    template_abbreviated = json.dumps(arm_template, indent=2)
    if len(template_abbreviated) > 12000:
        # Keep params + resources, trim the rest
        abbreviated = {
            "$schema": arm_template.get("$schema", ""),
            "parameters": {k: {"type": v.get("type", "string")} for k, v in arm_template.get("parameters", {}).items()},
            "resources": [
                {"type": r.get("type", ""), "name": r.get("name", ""), "apiVersion": r.get("apiVersion", "")}
                for r in arm_template.get("resources", [])
            ],
        }
        template_abbreviated = json.dumps(abbreviated, indent=2)

    prompt = (
        f"Generate a Python test script for the following deployed Azure infrastructure.\n\n"
        f"Resource Group: {resource_group}\n"
        f"Region: {region}\n\n"
        f"--- ARM TEMPLATE ---\n{template_abbreviated}\n--- END TEMPLATE ---\n\n"
        f"--- DEPLOYED RESOURCES ---\n{json.dumps(resource_summary, indent=2)}\n--- END RESOURCES ---\n\n"
        f"Generate tests that verify these resources are functional. "
        f"Focus on provisioning state, endpoint reachability, security config, "
        f"and tag compliance.\n\n"
        f"CRITICAL: You MUST generate an API version validation test for EVERY resource "
        f"in the ARM template. Query the Azure Resource Provider API to get valid API "
        f"versions and assert the template's apiVersion is in the valid list. "
        f"A wrong API version MUST cause a hard test failure.\n\n"
        f"Return ONLY the Python code."
    )

    from src.web import ensure_copilot_client
    client = await ensure_copilot_client()

    script = await copilot_send(
        client,
        model=get_model_for_task(INFRA_TESTER.task),
        system_prompt=INFRA_TESTER.system_prompt,
        prompt=prompt,
        timeout=INFRA_TESTER.timeout,
        agent_name="INFRA_TESTER",
    )

    # Strip markdown fences if present
    script = script.strip()
    if script.startswith("```"):
        lines = script.split("\n")
        # Remove first line (```python or ```)
        lines = lines[1:]
        # Remove last line if it's ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        script = "\n".join(lines).strip()

    return script


# ══════════════════════════════════════════════════════════════
# TEST EXECUTION
# ══════════════════════════════════════════════════════════════

def _extract_test_functions(script: str) -> list[str]:
    """Extract the names of all test_* functions from a Python script."""
    return re.findall(r'^def (test_\w+)\s*\(', script, re.MULTILINE)


async def execute_test_script(
    script: str,
    resource_group: str,
    timeout: float = 120.0,
) -> dict:
    """Execute a generated test script and collect per-test results.

    Runs the script in a subprocess with the correct environment variables.
    Parses output to determine which tests passed and which failed.

    Returns:
        {
            "status": "passed" | "failed" | "error",
            "total": int,
            "passed": int,
            "failed": int,
            "tests": [
                {"name": "test_xxx", "status": "passed"|"failed", "message": "..."},
                ...
            ],
            "stdout": str,
            "stderr": str,
        }
    """
    test_names = _extract_test_functions(script)
    if not test_names:
        return {
            "status": "error",
            "total": 0, "passed": 0, "failed": 0,
            "tests": [],
            "stdout": "",
            "stderr": "No test functions found in generated script",
        }

    # Write a runner wrapper that executes each test and reports JSON results
    runner_script = _build_test_runner(script, test_names)

    # Write to a temp file
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(runner_script)
        tmp_path = f.name

    try:
        # Set up environment
        env = dict(os.environ)
        env["TEST_RESOURCE_GROUP"] = resource_group
        env["PYTHONIOENCODING"] = "utf-8"

        # Use the same Python interpreter
        python_exe = sys.executable

        loop = asyncio.get_event_loop()
        proc = await loop.run_in_executor(None, lambda: _run_subprocess(
            python_exe, tmp_path, env, timeout
        ))

        return _parse_test_output(proc["stdout"], proc["stderr"], proc["returncode"], test_names)

    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _build_test_runner(script: str, test_names: list[str]) -> str:
    """Wrap the generated test script with a runner that outputs JSON results."""
    # Escape the script for embedding
    runner = f'''\
import json, sys, traceback, os

# ── Generated test code ──
{script}

# ── Runner ──
def main():
    results = []
    for name in {test_names!r}:
        fn = globals().get(name)
        if not fn:
            results.append({{"name": name, "status": "error", "message": "Function not found"}})
            continue
        try:
            fn()
            results.append({{"name": name, "status": "passed", "message": "OK"}})
        except AssertionError as e:
            results.append({{"name": name, "status": "failed", "message": str(e) or "Assertion failed"}})
        except Exception as e:
            results.append({{"name": name, "status": "failed", "message": f"{{type(e).__name__}}: {{e}}"}})

    passed = sum(1 for r in results if r["status"] == "passed")
    failed = len(results) - passed
    output = {{
        "status": "passed" if failed == 0 else "failed",
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "tests": results,
    }}
    print("__TEST_RESULTS__")
    print(json.dumps(output))

if __name__ == "__main__":
    main()
'''
    return runner


def _run_subprocess(python_exe: str, script_path: str, env: dict, timeout: float) -> dict:
    """Run a Python script in a subprocess with timeout."""
    import subprocess
    try:
        result = subprocess.run(
            [python_exe, script_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=os.path.dirname(script_path),
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": f"Test execution timed out after {timeout}s",
            "returncode": -1,
        }
    except Exception as e:
        return {
            "stdout": "",
            "stderr": str(e),
            "returncode": -1,
        }


def _parse_test_output(stdout: str, stderr: str, returncode: int, test_names: list[str]) -> dict:
    """Parse the JSON test results from the runner output."""
    # Look for our marker in stdout
    marker = "__TEST_RESULTS__"
    if marker in stdout:
        json_start = stdout.index(marker) + len(marker)
        json_str = stdout[json_start:].strip()
        try:
            first_line = json_str.split("\n")[0].strip()
            results = json.loads(first_line)
            results["stdout"] = stdout[:stdout.index(marker)].strip()
            results["stderr"] = stderr.strip()
            return results
        except (json.JSONDecodeError, IndexError):
            pass

    # Fallback — couldn't parse structured output
    return {
        "status": "error",
        "total": len(test_names),
        "passed": 0,
        "failed": len(test_names),
        "tests": [{"name": n, "status": "error", "message": "Could not parse test output"} for n in test_names],
        "stdout": stdout.strip(),
        "stderr": stderr.strip(),
    }


# ══════════════════════════════════════════════════════════════
# TEST FAILURE ANALYSIS
# ══════════════════════════════════════════════════════════════

async def analyze_test_failures(
    test_script: str,
    test_results: dict,
    arm_template: dict,
    deployed_resources: list[dict],
) -> dict:
    """Use the Copilot SDK to analyze test failures and recommend action.

    Returns a diagnosis dict:
        {
            "diagnosis": str,
            "root_cause": "template" | "test" | "transient" | "environment",
            "confidence": float,
            "action": "fix_template" | "fix_test" | "retry" | "skip",
            "fix_guidance": str,
            "affected_resources": [str],
        }
    """
    from src.agents import INFRA_TEST_ANALYZER
    from src.copilot_helpers import copilot_send
    from src.model_router import get_model_for_task

    failed_tests = [t for t in test_results.get("tests", []) if t["status"] != "passed"]
    if not failed_tests:
        return {
            "diagnosis": "All tests passed",
            "root_cause": "none",
            "confidence": 1.0,
            "action": "skip",
            "fix_guidance": "",
            "affected_resources": [],
        }

    # Build compact resource summary
    resource_names = [{"name": r.get("name"), "type": r.get("type")} for r in deployed_resources]

    prompt = (
        f"Analyze the following infrastructure test failures.\n\n"
        f"--- TEST SCRIPT ---\n{test_script[:6000]}\n--- END SCRIPT ---\n\n"
        f"--- TEST RESULTS ---\n{json.dumps(failed_tests, indent=2)}\n--- END RESULTS ---\n\n"
        f"--- ARM TEMPLATE (abbreviated) ---\n{json.dumps(arm_template, indent=2)[:6000]}\n--- END TEMPLATE ---\n\n"
        f"--- DEPLOYED RESOURCES ---\n{json.dumps(resource_names, indent=2)}\n--- END RESOURCES ---\n\n"
        f"Analyze the failures and return a JSON diagnosis object."
    )

    from src.web import ensure_copilot_client
    client = await ensure_copilot_client()

    raw = await copilot_send(
        client,
        model=get_model_for_task(INFRA_TEST_ANALYZER.task),
        system_prompt=INFRA_TEST_ANALYZER.system_prompt,
        prompt=prompt,
        timeout=INFRA_TEST_ANALYZER.timeout,
        agent_name="INFRA_TEST_ANALYZER",
    )

    # Parse JSON from response
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    try:
        diagnosis = json.loads(raw)
    except json.JSONDecodeError:
        # Try to find JSON in the response
        brace_start = raw.find("{")
        brace_end = raw.rfind("}")
        if brace_start >= 0 and brace_end > brace_start:
            try:
                diagnosis = json.loads(raw[brace_start:brace_end + 1])
            except json.JSONDecodeError:
                diagnosis = {
                    "diagnosis": "Could not parse LLM analysis",
                    "root_cause": "test",
                    "confidence": 0.3,
                    "action": "retry",
                    "fix_guidance": raw[:500],
                    "affected_resources": [],
                }
        else:
            diagnosis = {
                "diagnosis": raw[:500],
                "root_cause": "test",
                "confidence": 0.3,
                "action": "retry",
                "fix_guidance": "",
                "affected_resources": [],
            }

    return diagnosis


# ══════════════════════════════════════════════════════════════
# STREAMING INFRASTRUCTURE TESTING PIPELINE
# ══════════════════════════════════════════════════════════════

async def stream_infra_testing(
    *,
    arm_template: dict,
    resource_group: str,
    deployed_resources: list[dict],
    region: str = "eastus2",
    max_retries: int = 2,
) -> AsyncGenerator[str, None]:
    """Full infrastructure testing pipeline as an NDJSON async generator.

    Phases emitted:
      testing_start     — pipeline begins
      testing_generate  — test script being written by LLM
      testing_execute   — tests are running
      test_result       — individual test pass/fail
      testing_analyze   — LLM analyzing failures
      testing_feedback  — action recommendation (fix_template, retry, etc.)
      testing_complete  — all done

    This generator is called by validation.py after a successful deploy.
    """
    resource_types = list({r.get("type", "unknown") for r in deployed_resources})

    yield json.dumps({
        "phase": "testing_start",
        "detail": f"Let me write some tests to verify these {len(deployed_resources)} resources are actually working…",
        "resource_count": len(deployed_resources),
        "resource_types": resource_types,
    }) + "\n"

    # ── Step 1: Generate tests ──
    yield json.dumps({
        "phase": "testing_generate",
        "detail": f"Writing infrastructure tests for {', '.join(r.split('/')[-1] for r in resource_types[:5])}…",
        "status": "running",
    }) + "\n"

    try:
        test_script = await generate_test_script(
            arm_template=arm_template,
            resource_group=resource_group,
            deployed_resources=deployed_resources,
            region=region,
        )
    except Exception as e:
        logger.warning(f"Test generation failed: {e}")
        yield json.dumps({
            "phase": "testing_generate",
            "detail": f"Couldn't generate tests: {e}",
            "status": "error",
        }) + "\n"
        yield json.dumps({
            "phase": "testing_complete",
            "status": "skipped",
            "detail": "Test generation failed — skipping infrastructure tests. The deployment itself succeeded.",
            "tests_passed": 0,
            "tests_failed": 0,
        }) + "\n"
        return

    test_names = _extract_test_functions(test_script)
    yield json.dumps({
        "phase": "testing_generate",
        "detail": f"Generated {len(test_names)} test{'s' if len(test_names) != 1 else ''}: {', '.join(test_names[:8])}{'…' if len(test_names) > 8 else ''}",
        "status": "complete",
        "test_count": len(test_names),
        "test_names": test_names,
        "script_preview": test_script[:2000],
    }) + "\n"

    if not test_names:
        yield json.dumps({
            "phase": "testing_complete",
            "status": "skipped",
            "detail": "No test functions were generated — skipping.",
            "tests_passed": 0,
            "tests_failed": 0,
        }) + "\n"
        return

    # ── Step 2: Execute tests (with retries for transient issues) ──
    final_results = None
    for attempt in range(1, max_retries + 1):
        is_last = attempt == max_retries

        if attempt > 1:
            yield json.dumps({
                "phase": "testing_execute",
                "detail": f"Retrying tests (attempt {attempt}/{max_retries}) — some transient issues may have resolved…",
                "status": "running",
                "attempt": attempt,
            }) + "\n"
            # Brief wait for Azure propagation
            await asyncio.sleep(15)
        else:
            yield json.dumps({
                "phase": "testing_execute",
                "detail": f"Running {len(test_names)} infrastructure tests against live resources…",
                "status": "running",
                "attempt": attempt,
            }) + "\n"

        try:
            results = await execute_test_script(
                script=test_script,
                resource_group=resource_group,
                timeout=120.0,
            )
        except Exception as e:
            logger.warning(f"Test execution error: {e}")
            results = {
                "status": "error",
                "total": len(test_names),
                "passed": 0,
                "failed": len(test_names),
                "tests": [{"name": n, "status": "error", "message": str(e)} for n in test_names],
                "stdout": "",
                "stderr": str(e),
            }

        # Emit individual test results
        for test in results.get("tests", []):
            icon = "✅" if test["status"] == "passed" else "❌"
            yield json.dumps({
                "phase": "test_result",
                "test_name": test["name"],
                "status": test["status"],
                "message": test.get("message", ""),
                "detail": f"{icon} {test['name']}: {test.get('message', '')}",
            }) + "\n"

        final_results = results

        # If all passed, we're done
        if results.get("status") == "passed":
            break

        # If failures, analyze whether to retry
        if not is_last:
            yield json.dumps({
                "phase": "testing_analyze",
                "detail": f"{results.get('failed', 0)} test(s) failed — analyzing whether to retry or report…",
                "status": "running",
            }) + "\n"

            try:
                diagnosis = await analyze_test_failures(
                    test_script=test_script,
                    test_results=results,
                    arm_template=arm_template,
                    deployed_resources=deployed_resources,
                )
            except Exception as e:
                logger.warning(f"Test analysis failed: {e}")
                diagnosis = {
                    "diagnosis": str(e),
                    "root_cause": "test",
                    "confidence": 0.3,
                    "action": "skip",
                    "fix_guidance": "",
                    "affected_resources": [],
                }

            yield json.dumps({
                "phase": "testing_analyze",
                "detail": f"Diagnosis: {diagnosis.get('diagnosis', 'Unknown')}",
                "status": "complete",
                "root_cause": diagnosis.get("root_cause", "unknown"),
                "action": diagnosis.get("action", "skip"),
                "confidence": diagnosis.get("confidence", 0),
            }) + "\n"

            # Only retry if the analysis says transient
            if diagnosis.get("action") != "retry":
                # Emit feedback for template fixes
                if diagnosis.get("action") == "fix_template":
                    yield json.dumps({
                        "phase": "testing_feedback",
                        "detail": f"Infrastructure issue detected: {diagnosis.get('fix_guidance', 'Check template')}",
                        "action": "fix_template",
                        "fix_guidance": diagnosis.get("fix_guidance", ""),
                        "affected_resources": diagnosis.get("affected_resources", []),
                    }) + "\n"
                break

    # ── Final results ──
    if final_results:
        passed = final_results.get("passed", 0)
        failed = final_results.get("failed", 0)
        total = final_results.get("total", 0)
        status = "passed" if failed == 0 else "failed"

        yield json.dumps({
            "phase": "testing_complete",
            "status": status,
            "detail": (
                f"All {total} infrastructure tests passed — resources are functional!"
                if status == "passed"
                else f"{passed}/{total} tests passed, {failed} failed"
            ),
            "tests_passed": passed,
            "tests_failed": failed,
            "tests_total": total,
            "test_details": final_results.get("tests", []),
            "script": test_script,
        }) + "\n"
    else:
        yield json.dumps({
            "phase": "testing_complete",
            "status": "error",
            "detail": "Test execution produced no results",
            "tests_passed": 0,
            "tests_failed": 0,
        }) + "\n"
