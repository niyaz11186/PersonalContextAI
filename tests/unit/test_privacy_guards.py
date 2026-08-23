"""Privacy regression guards.

These exist because a dependency's default behaviour silently violated NFR-01.1.

graphiti_core ships PostHog analytics enabled by default. It was caught only
because a flush message happened to appear in stdout during an unrelated check.
A future version bump could reintroduce it, or add another endpoint, so the
protection is asserted rather than assumed.

For an application whose entire premise is a private personal-context store,
"we disabled it once" is not a control. This file is the control.
"""

from __future__ import annotations

import os
import subprocess
import sys


def test_graphiti_telemetry_is_disabled_on_import() -> None:
    """Importing our adapter must disable Graphiti telemetry.

    Checked in a fresh subprocess because the current process may already have the
    variable set from an earlier import, which would make this pass vacuously.
    """
    code = (
        "import sys; sys.path.insert(0,'src');"
        "import pca.adapters.graphiti.memory_graph;"
        "import os; print(os.environ.get('GRAPHITI_TELEMETRY_ENABLED'))"
    )
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "false"


def test_explicit_opt_in_is_still_honoured() -> None:
    """Disabling by default must not make the setting impossible to override.

    setdefault rather than a hard assignment, so a user who genuinely wants to
    share analytics can, deliberately, via the real environment.
    """
    env = dict(os.environ)
    env["GRAPHITI_TELEMETRY_ENABLED"] = "true"
    code = (
        "import sys; sys.path.insert(0,'src');"
        "import pca.adapters.graphiti.memory_graph;"
        "import os; print(os.environ.get('GRAPHITI_TELEMETRY_ENABLED'))"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )

    assert result.stdout.strip() == "true"


def test_no_openai_credential_is_configured() -> None:
    """Constraint C-2, enforced where it can actually be enforced.

    An earlier version of this test asserted the `openai` package was absent. That
    was wrong: `graphiti-core` declares a hard dependency on `openai>=1.91.0`, so
    the library is installed whether or not it is used. The package cannot be
    excluded while Graphiti is in the stack.

    What C-2 genuinely means, and what is checked here, is that **no OpenAI
    credential exists and no OpenAI call is ever made**. Without a key the library
    is inert. Combined with the source-level import check below, that is the real
    control.
    """
    from pathlib import Path

    for name in (".env", ".env.example"):
        path = Path(name)
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            if "OPENAI" in key.upper():
                assert not value.strip(), f"{name} configures {key}; C-2 excludes OpenAI"

    assert not os.environ.get("OPENAI_API_KEY"), (
        "OPENAI_API_KEY is set in the environment; constraint C-2 excludes OpenAI"
    )


def test_no_openai_import_anywhere_in_source() -> None:
    """Boundary rule 6, checked against the source tree.

    The package being absent is necessary but not sufficient — an import statement
    would be a latent failure waiting for someone to install it transitively.
    """
    from pathlib import Path

    offenders: list[str] = []
    for path in Path("src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import openai", "from openai")):
                offenders.append(f"{path}: {stripped}")

    assert not offenders, f"openai imports found: {offenders}"


def test_graphiti_is_confined_to_its_adapter() -> None:
    """Boundary rule 1.

    The framework-immaturity risk is only survivable if graphiti_core cannot leak
    past its adapter. Enforced by review now that the CI linter was dropped, so
    this test is the standing check.
    """
    from pathlib import Path

    allowed = Path("src/pca/adapters/graphiti")
    offenders: list[str] = []

    for path in Path("src").rglob("*.py"):
        if allowed in path.parents:
            continue
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import graphiti", "from graphiti")):
                offenders.append(f"{path}: {stripped}")

    assert not offenders, f"graphiti_core imported outside its adapter: {offenders}"


def test_langgraph_is_confined_to_orchestration() -> None:
    """Boundary rule 2. Same reasoning as boundary rule 1, for LangGraph."""
    from pathlib import Path

    allowed = Path("src/pca/orchestration")
    offenders: list[str] = []

    for path in Path("src").rglob("*.py"):
        if allowed in path.parents or path.parent == allowed:
            continue
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import langgraph", "from langgraph")):
                offenders.append(f"{path}: {stripped}")

    assert not offenders, f"langgraph imported outside orchestration: {offenders}"


def test_no_direct_datetime_now_outside_the_clock_adapter() -> None:
    """Boundary rule 4.

    Every timestamp must flow from ClockPort or temporal correctness becomes
    untestable — simulating "three months pass" is impossible if any component
    reads the wall clock directly.

    Uses AST rather than text matching. A text scan flagged a docstring in
    ports/clock.py that merely *mentions* datetime.now() while explaining the rule,
    which is a false positive that would train people to ignore this test.
    """
    import ast
    from pathlib import Path

    permitted = {Path("src/pca/adapters/clock/system_clock.py")}
    offenders: list[str] = []

    for path in Path("src").rglob("*.py"):
        if path in permitted:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in {"now", "utcnow"}:
                value = func.value
                if isinstance(value, ast.Name) and value.id == "datetime":
                    offenders.append(f"{path}:{node.lineno} datetime.{func.attr}()")

    assert not offenders, f"direct clock reads found: {offenders}"
