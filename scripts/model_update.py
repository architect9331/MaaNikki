"""Update the self-trained recognition models after a game update; see BUILDING.md.

Offline only: this tool never starts or connects to the game. Install
build/requirements-model-training.txt in a development Python. Private paths come
from .local/model-update/config.json and all outputs stay under .build/model-update/.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from model_update_core.common import DEFAULT_CONFIG, REPOSITORY, RUNS, Run


def waivers(values):
    parsed = {}
    for value in values or ():
        name, separator, reason = value.partition("=")
        if not separator or not reason.strip():
            raise SystemExit('Use --waive "<check name>=<reason>"')
        parsed[name] = reason.strip()
    return parsed


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=Path, default=RUNS, help="Directory holding run state")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Start a run and record the baseline hashes")
    init.add_argument("--run", required=True)
    init.add_argument("--root", type=Path, default=REPOSITORY, help="Project tree to update")
    init.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    init.add_argument("--note")

    for name, text in (("discover", "Compare the decoded item table with deployed labels"),
                       ("package", "Assemble the candidate, sync the lock and probe the packaged runtime"),
                       ("promote", "Replace project models with a passing candidate")):
        command = commands.add_parser(name, help=text)
        command.add_argument("--run", required=True)
        if name == "promote":
            command.add_argument("--confirm", action="store_true", help="Required: a person reviewed package.json")

    assets = commands.add_parser("assets", help="List missing art and labels; stop on any gap")
    assets.add_argument("--run", required=True)
    assets.add_argument("--accept-gaps", help="Reason for shipping with the listed gaps excluded")

    train = commands.add_parser("train", help="Create candidate files")
    train.add_argument("--run", required=True)
    train.add_argument("--model", required=True, choices=["wardrobe", "identity", "level", "badge"])
    train.add_argument("--mode", default="head-train", choices=["head-train", "prototypes", "full"],
                       help="Wardrobe only. head-train and prototypes keep the deployed backbone")
    train.add_argument("--use-real-new", action="store_true", help="Identity: adapt on real cards of new classes too")

    evaluate = commands.add_parser("evaluate", help="Compare candidate with baseline and write the gate")
    evaluate.add_argument("--run", required=True)
    evaluate.add_argument("--model", required=True, choices=["wardrobe", "blessing"])
    evaluate.add_argument("--waive", action="append", help='"<check name>=<reason>"; only non-significant falls can be waived')

    args = parser.parse_args()
    if args.command == "init":
        run = Run.create(args.run, args.root, args.config, args.runs, args.note)
        print(run.dir)
        return
    run = Run(args.run, args.runs)
    if args.command == "discover":
        from model_update_core.discover import discover
        discover(run)
    elif args.command == "assets":
        from model_update_core.discover import assets as run_assets
        run_assets(run, args.accept_gaps)
    elif args.command == "train":
        if args.model == "wardrobe":
            from model_update_core import wardrobe
            wardrobe.train_full(run) if args.mode == "full" else wardrobe.train_heads(run, args.mode)
        else:
            from model_update_core import blessing
            blessing.train(run, args.use_real_new) if args.model == "identity" else blessing.retrain_attributes(run, args.model)
    elif args.command == "evaluate":
        if args.model == "wardrobe":
            from model_update_core.wardrobe import evaluate as run_evaluate
        else:
            from model_update_core.blessing import evaluate as run_evaluate
        result = run_evaluate(run, waivers(args.waive))
        if result["status"] != "pass":
            raise SystemExit(f"{args.model} gate blocked: {result['blocking']}")
    elif args.command == "package":
        from model_update_core.package import package
        package(run)
    elif args.command == "promote":
        if not args.confirm:
            raise SystemExit("Review package.json, then pass --confirm")
        from model_update_core.package import promote
        promote(run)


if __name__ == "__main__":
    main()
