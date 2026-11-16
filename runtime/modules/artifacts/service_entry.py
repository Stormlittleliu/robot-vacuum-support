# -*- coding: utf-8 -*-
"""Generate the stdlib ``serve.py`` entry for a service-capable artifact.

The generated file lives at the artifact root.  It adds the product
``artifact/runtime`` directory to ``sys.path`` and imports the real package
``modules.instances.runtime.bootstrap`` / ``.server``.  The product runtime root
is always ``artifact/runtime`` (never the editor repository).  ``core.paths`` is
imported only after ``bootstrap.configure_environment`` applies instance roots.
"""
from __future__ import annotations

from pathlib import Path

SERVICE_ENTRY = '''# -*- coding: utf-8 -*-
"""Auto-generated instance product entry (stdlib bootstrap).

Usage:
  python serve.py --instance-config <instances/id/instance.json> [--port N]
  python serve.py --instance-root <instances/id> [--port N]
"""
from __future__ import annotations

import sys

# The artifact tree is program source and must stay byte-for-byte immutable.
sys.dont_write_bytecode = True

import argparse
from pathlib import Path

ARTIFACT_ROOT = Path(__file__).resolve().parent
RUNTIME_ROOT = ARTIFACT_ROOT / "runtime"


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent Harness instance product")
    parser.add_argument("--instance-config")
    parser.add_argument("--instance-root")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if not RUNTIME_ROOT.is_dir():
        print(f"runtime root missing: {RUNTIME_ROOT}", file=sys.stderr)
        return 2
    if str(RUNTIME_ROOT) not in sys.path:
        sys.path.insert(0, str(RUNTIME_ROOT))
    # Package imports: bootstrap/server live under runtime/modules/instances/runtime.
    from modules.instances.runtime.bootstrap import (
        InstanceConfigError,
        configure_environment,
        load_instance_config,
        resolve_instance_config,
    )
    try:
        config_path = resolve_instance_config(
            instance_config=args.instance_config,
            instance_root=args.instance_root,
        )
        config = load_instance_config(config_path)
        if args.port is not None:
            config.port = args.port
        if args.host:
            config.host = args.host
        configure_environment(config)
    except InstanceConfigError as exc:
        print(f"instance config error: {exc}", file=sys.stderr)
        return 2

    # Only now may runtime modules import core.paths / scenario code.
    from modules.instances.runtime import server

    app = server.create_app(config)
    try:
        import uvicorn
    except ImportError:
        print("uvicorn is required to serve this artifact", file=sys.stderr)
        return 2
    uvicorn.run(app, host=config.host, port=config.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def write_service_entry(bundle: str | Path) -> Path:
    """Write ``<bundle>/serve.py``; returns the path.  Does not copy secrets."""
    root = Path(bundle).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"artifact root is not a directory: {root}")
    runtime = root / "runtime"
    if not runtime.is_dir():
        raise FileNotFoundError(f"artifact runtime root is missing: {runtime}")
    target = root / "serve.py"
    target.write_text(SERVICE_ENTRY, encoding="utf-8")
    return target
