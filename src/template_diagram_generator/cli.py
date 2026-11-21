"""Non-interactive CloudFormation architecture diagram CLI."""

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from .config import ConfigError, direct_config, load_config
from .loader import TemplateError
from .localization import REPORT_LOCALES
from .models import ResourceGraph, RootInput, TerraformInput, pointer
from .publication import VERSION, public_model
from .renderers import FORMATS, RenderError, render
from .resolver import ResolveError, resolve
from .semantics import analyze
from .views import build_views


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="template-diagram",
        description="Generate offline architecture diagrams from CloudFormation or Terraform.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate", help="Analyze templates and write report files")
    inputs = generate.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--config", type=Path, help="versioned YAML or JSON config file")
    inputs.add_argument(
        "--template", type=Path, action="append", help="root YAML or JSON template, repeatable"
    )
    inputs.add_argument(
        "--terraform",
        type=Path,
        action="append",
        help="Terraform directory or .tf file, repeatable",
    )
    generate.add_argument("--output", type=Path, help="override the output directory")
    generate.add_argument("--input-base", type=Path, help="input boundary for direct paths")
    generate.add_argument(
        "--format", choices=FORMATS, action="append", help="override output formats, repeatable"
    )
    generate.add_argument(
        "--locale", choices=REPORT_LOCALES, help="override the report display language"
    )
    generate.add_argument(
        "--localize-json", action="store_true", help="include translated JSON diagnostics"
    )
    generate.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="write artifacts and return 3 for analysis warnings",
    )
    return parser


def _publish(directory: Path, artifacts: dict[str, str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".template-diagram-", dir=directory) as staging:
        base = Path(staging)
        for name, text in artifacts.items():
            (base / name).write_text(text, encoding="utf-8", newline="\n")
        # The self-contained HTML is placed last, after every artifact has been staged.
        for name in sorted(artifacts, key=lambda item: (item == "index.html", item)):
            os.replace(base / name, directory / name)


def _error(message: str) -> None:
    print(f"template-diagram: {message}", file=sys.stderr)


def _resolve_inputs(config) -> ResourceGraph:
    cfn = tuple(root for root in config.roots if isinstance(root, RootInput))
    tf = tuple(root for root in config.roots if isinstance(root, TerraformInput))
    if not tf:
        return resolve(cfn, config.resolution)
    try:
        from .terraform import resolve_terraform
    except ModuleNotFoundError as exc:
        if exc.name not in {"hcl2", "lark"}:
            raise
        raise ConfigError(
            "Terraform input requires pip install 'template-diagram-generator[terraform]'."
        ) from exc
    terraform = resolve_terraform(tf, config.resolution.input_base)
    if not cfn:
        return terraform
    cloudformation = resolve(cfn, config.resolution)
    return ResourceGraph(
        {**cloudformation.stacks, **terraform.stacks},
        {**cloudformation.resources, **terraform.resources},
        {**cloudformation.references, **terraform.references},
        (*cloudformation.diagnostics, *terraform.diagnostics),
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.config and args.input_base is not None:
            raise ConfigError(
                "Set input.base_directory in the configuration instead of --input-base."
            )
        config = (
            load_config(args.config)
            if args.config
            else direct_config(
                args.terraform or args.template,
                Path.cwd(),
                terraform=args.terraform is not None,
                input_base=args.input_base,
            )
        )
        if args.output is not None:
            config = replace(config, directory=args.output)
        if args.format is not None:
            config = replace(config, formats=tuple(dict.fromkeys(args.format)))
        if args.locale is not None:
            config = replace(config, view=replace(config.view, locale=args.locale))
        graph = _resolve_inputs(config)
        model = public_model(analyze(graph, relationships=config.relationships))
        views = build_views(model, config.view)
        model = replace(model, diagnostics=views.diagnostics)
        artifacts = render(
            model,
            views,
            config.formats,
            diagnostic_locale=views.locale
            if args.localize_json or config.localize_json_diagnostics
            else None,
        )
        _publish(config.directory, artifacts)
    except (ConfigError, ResolveError) as exc:
        _error(str(exc))
        return 2
    except TemplateError as exc:
        position = f":{exc.source.line}" if exc.source.line else ""
        _error(f"{exc.code}: {exc} ({exc.source.template}{position})")
        return 1
    except RenderError as exc:
        _error(str(exc))
        return 1
    except (OSError, ValueError):
        _error("Could not access an input or output path.")
        return 1

    warnings = 0
    for item in model.diagnostics:
        warnings += item.severity == "warning"
        line = f":{item.source.line}" if item.source.line else ""
        print(
            f"{item.severity} {item.code} {item.source.template}{line} {pointer(item.path)} {item.message}",
            file=sys.stderr,
        )
    print(
        f"Wrote {len(artifacts)} file(s) to {config.directory} from {len(model.nodes)} resource(s). Warnings: {warnings}."
    )
    return 3 if args.fail_on_warning and warnings else 0
