"""CLI entry point for the Olist dispute-resolution workflow."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any
import zipfile

from src.dispute_resolution import (
    DisputeWorkflow,
    ModelAuditClient,
    OlistRepository,
    PolicyDefinition,
    RuntimeConfig,
)
from src.dispute_resolution.workflow import CaseError, TraceWriter


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_LOGGING_DIR = PROJECT_ROOT / "logging"
EXPECTED_CASE_FILENAMES = {f"EC_{number:03d}.json" for number in range(1, 51)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resolve Olist dispute cases using a versioned policy.")
    parser.add_argument("--case", help="Run exactly one case ID, for example EC_001.")
    parser.add_argument("--input-dir", type=Path, default=PROJECT_ROOT / "input")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "output")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument("--trace-path", type=Path, default=PROJECT_ROOT / "trace.jsonl")
    parser.add_argument("--metadata-path", type=Path, default=PROJECT_ROOT / "metadata.json")
    parser.add_argument(
        "--logging-dir",
        type=Path,
        default=DEFAULT_LOGGING_DIR,
        help="Directory receiving copies of the latest trace and metadata artifacts.",
    )
    parser.add_argument("--zip-path", type=Path, default=PROJECT_ROOT / "output.zip")
    parser.add_argument(
        "--policy-path",
        type=Path,
        default=PROJECT_ROOT / "policy" / "EC_POLICY_V1.json",
        help="Versioned JSON policy definition to load.",
    )
    parser.add_argument(
        "--no-zip-output",
        action="store_true",
        help="Do not create output.zip after a successful full 50-case run.",
    )
    parser.add_argument(
        "--no-model-audit",
        action="store_true",
        help="Disable provider audits for this run while keeping deterministic verification enabled.",
    )
    return parser.parse_args()


def input_files(input_dir: Path, case_id: str | None) -> list[Path]:
    if case_id:
        candidates = [input_dir / f"{case_id}.json", input_dir / "input" / f"{case_id}.json"]
        for path in candidates:
            if path.exists():
                return [path]
        raise FileNotFoundError(f"Case input does not exist: {candidates[0]}")

    files = sorted(input_dir.glob("EC_*.json"))
    if files:
        return files
    return sorted((input_dir / "input").glob("EC_*.json"))


def read_case(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise CaseError(f"Input must be a JSON object: {path}")
    return data


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")


def write_metadata(
    path: Path,
    config: RuntimeConfig,
    repository: OlistRepository,
    policy: PolicyDefinition,
    case_count: int,
) -> None:
    metadata = {
        "model": config.metadata(),
        "framework": "stdlib supervisor-specialist-verifier workflow",
        "runtime": config.runtime,
        "policy_version": policy.policy_version,
        "agents": ["coordinator", "order_seller", "payment", "delivery", "policy", "verifier"],
        "dataset_summary": repository.summary(),
        "latest_run_case_count": case_count,
    }
    write_json(path, metadata)


def create_submission_zip(output_dir: Path, zip_path: Path) -> None:
    output_files = {path.name: path for path in output_dir.glob("EC_*.json") if path.is_file()}
    if set(output_files) != EXPECTED_CASE_FILENAMES:
        missing = sorted(EXPECTED_CASE_FILENAMES - set(output_files))
        unexpected = sorted(set(output_files) - EXPECTED_CASE_FILENAMES)
        raise CaseError(
            "Cannot create submission zip: output must contain exactly EC_001.json through "
            f"EC_050.json. Missing={missing}; unexpected={unexpected}"
        )

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename in sorted(EXPECTED_CASE_FILENAMES):
            archive.write(output_files[filename], arcname=f"output/{filename}")


def publish_full_output(staging_dir: Path, output_dir: Path) -> None:
    """Publish a complete staged submission only after every case has passed."""
    staged_files = {path.name: path for path in staging_dir.glob("EC_*.json") if path.is_file()}
    if set(staged_files) != EXPECTED_CASE_FILENAMES:
        raise CaseError("Staging output does not contain the required 50 case files.")
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename in sorted(EXPECTED_CASE_FILENAMES):
        os.replace(staged_files[filename], output_dir / filename)


def sync_logging_artifacts(trace_path: Path, metadata_path: Path, logging_dir: Path) -> None:
    """Keep the latest trace and metadata together without changing README artifacts."""
    logging_dir.mkdir(parents=True, exist_ok=True)
    for source_path, filename in (
        (trace_path, "trace.jsonl"),
        (metadata_path, "metadata.json"),
    ):
        destination = logging_dir / filename
        if source_path.resolve() != destination.resolve():
            shutil.copy2(source_path, destination)


def run() -> int:
    args = parse_args()
    try:
        config = RuntimeConfig.from_project_root(PROJECT_ROOT)
    except ValueError as error:
        print(f"Invalid runtime configuration: {error}", file=sys.stderr)
        return 2
    if args.no_model_audit:
        config = replace(
            config,
            model_audit_enabled=False,
            model_proposal_enabled=False,
            model_output_mode="deterministic",
        )
    try:
        policy = PolicyDefinition.from_file(args.policy_path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
        print(f"Invalid policy definition: {error}", file=sys.stderr)
        return 2
    files = input_files(args.input_dir, args.case)
    if not files:
        print(f"No EC_*.json input cases found in {args.input_dir}", file=sys.stderr)
        return 2

    is_full_run = {path.name for path in files} == EXPECTED_CASE_FILENAMES
    staging_context: tempfile.TemporaryDirectory[str] | None = None
    output_target_dir = args.output_dir
    if is_full_run:
        args.output_dir.parent.mkdir(parents=True, exist_ok=True)
        staging_context = tempfile.TemporaryDirectory(
            prefix=".dispute-output-", dir=args.output_dir.parent
        )
        output_target_dir = Path(staging_context.name)

    repository = OlistRepository(args.data_dir)
    print("Loading Olist data indexes...")
    repository.load()
    try:
        trace = TraceWriter(args.trace_path)
        audit_client = ModelAuditClient(config)
        workflow = DisputeWorkflow(
            repository, trace, audit_client, policy, max_workers=config.max_workers
        )
        failures: list[str] = []
        try:
            for input_path in files:
                try:
                    case = read_case(input_path)
                    if case.get("case_id") != input_path.stem:
                        raise CaseError(
                            f"Input case_id {case.get('case_id')!r} does not match filename {input_path.name}."
                        )
                    output = workflow.process(case)
                    write_json(output_target_dir / input_path.name, output)
                    print(f"Resolved {case['case_id']} -> {args.output_dir / input_path.name}")
                except (CaseError, FileNotFoundError, json.JSONDecodeError) as error:
                    failures.append(f"{input_path.name}: {error}")
                    print(f"Failed {input_path.name}: {error}", file=sys.stderr)
        finally:
            trace.close()

        write_metadata(args.metadata_path, config, repository, policy, len(files) - len(failures))
        try:
            sync_logging_artifacts(args.trace_path, args.metadata_path, args.logging_dir)
        except OSError as error:
            print(f"Run completed, but logging artifacts were not synchronized: {error}", file=sys.stderr)
            return 1
        if failures:
            print(f"Run completed with {len(failures)} failed case(s).", file=sys.stderr)
            return 1
        if is_full_run:
            try:
                publish_full_output(output_target_dir, args.output_dir)
            except CaseError as error:
                print(f"Run completed, but output was not published: {error}", file=sys.stderr)
                return 1
        if is_full_run and not args.no_zip_output:
            try:
                create_submission_zip(args.output_dir, args.zip_path)
            except CaseError as error:
                print(f"Run completed, but submission zip was not created: {error}", file=sys.stderr)
                return 1
            print(f"Submission zip created: {args.zip_path}")
        elif not args.case:
            print("Skipping submission zip because this run did not contain all 50 expected case files.")
        print(f"Run completed: {len(files)} case(s) resolved.")
        return 0
    finally:
        if staging_context is not None:
            staging_context.cleanup()


if __name__ == "__main__":
    raise SystemExit(run())
