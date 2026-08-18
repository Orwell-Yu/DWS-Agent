#!/usr/bin/env python3
"""Validate DWS consumer readiness without reading event stdout/message bodies."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


async def run_consumer(
    config: Dict[str, Any],
    name: str,
    event_key: str,
    group: Optional[str],
    duration: int,
    verbose: bool = False,
) -> Tuple[str, bool, int, str]:
    dws = config["dws"]
    command = [
        dws["binary"],
        "event",
        "consume",
        event_key,
        "--flatten",
        "--format",
        "ndjson",
        "--duration",
        "%ss" % duration,
        "--ttl",
        "15m",
        "--ephemeral",
        "--name",
        "dws-auto-reply-smoke-%s" % name,
        "--profile",
        dws["profile"],
    ]
    if group:
        command.extend(["--group", group])
    if verbose:
        command.append("--verbose")
    env = os.environ.copy()
    env["DWS_CONFIG_DIR"] = dws["config_dir"]
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    ready = False
    diagnostics: List[str] = []
    assert process.stderr is not None
    while True:
        line = await process.stderr.readline()
        if not line:
            break
        text = line.decode("utf-8", "replace").strip()
        if "[event] ready" in text:
            ready = True
        elif text:
            diagnostics.append(text[:1000])
    return_code = await process.wait()
    return name, ready, return_code, "\n".join(diagnostics[-20:])


async def main(config_path: str, duration: int, only: Optional[str], verbose: bool) -> int:
    with open(config_path, "r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    event_keys = config["dws"]["event_keys"]
    specs = [
        ("private-all", event_keys["private"], None),
        ("at-me", event_keys["at"], None),
    ]
    for index, group in enumerate(config["groups"]["whitelist"], start=1):
        specs.append(("group-%s" % index, event_keys["group"], group["conversation_id"]))
    if only:
        specs = [spec for spec in specs if spec[0] == only]
        if not specs:
            raise ValueError("unknown listener name: %s" % only)
    results = await asyncio.gather(
        *(run_consumer(config, name, key, group, duration, verbose) for name, key, group in specs)
    )
    output = {
        name: {"ready": ready, "exit_code": code, "diagnostic": diagnostic}
        for name, ready, code, diagnostic in results
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if all(ready and code == 0 for _, ready, code, _ in results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config", default=str(Path(__file__).resolve().parent.parent / "config.yaml")
    )
    parser.add_argument("--duration", type=int, default=8)
    parser.add_argument("--only", choices=["private-all", "at-me", "group-1", "group-2"])
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.config, args.duration, args.only, args.verbose)))
