"""`wiki-librarian` command-line entry point (the model-bearing half).

Kept deliberately separate from the `wiki` console script: `wiki` stays pure
code with zero model calls; this binary is the one that talks to a model.
"""
from __future__ import annotations

import argparse
import json
import sys

from wiki.db import Repo

from . import adjudicate as adjudicatemod
from . import extract as extractmod
from . import triage as triagemod
from .config import LibrarianConfig


def _emit(obj, as_json: bool) -> bool:
    if as_json:
        print(json.dumps(obj, indent=2, ensure_ascii=False))
        return True
    return False


def cmd_extract(args):
    cfg = LibrarianConfig.load()
    with Repo.open() as repo:
        try:
            rep = extractmod.run_one(repo, cfg, args.source)
        except extractmod.ExtractionFailed as e:
            sys.exit(f"error: {e}")
        if _emit(rep, args.json):
            return
        print(f"extracted source #{args.source}: {rep['claims']} claim(s), "
              f"{rep['contradictions']} contradiction(s); "
              f"gate promoted {rep['gate_promoted']}, held {rep['gate_held']}")


def cmd_catch_up(args):
    cfg = LibrarianConfig.load()
    with Repo.open() as repo:
        rep = extractmod.catch_up(repo, cfg)
        if _emit(rep, args.json):
            return
        if not rep["processed"] and not rep["failed"]:
            print("nothing pending — the brain is caught up")
            return
        print(f"catch-up: {len(rep['processed'])} extracted, "
              f"{len(rep['failed'])} failed")
        for d in rep["processed"]:
            print(f"  + source #{d['source_id']}: {d['claims']} claim(s)")
        for f in rep["failed"]:
            print(f"  ! source #{f['source_id']}: {f['error']}")
        if rep["processed"]:
            print(f"gate promoted {rep['gate_promoted']}, held {rep['gate_held']}; "
                  f"{rep['pages_rendered']} page(s) rendered")


def cmd_triage(args):
    cfg = LibrarianConfig.load()
    with Repo.open() as repo:
        rep = triagemod.run(repo, cfg, only_untriaged=not args.all)
        if _emit(rep, args.json):
            return
        if not rep["triaged"] and not rep["failed"]:
            print("nothing to triage — no pending claims (or all already triaged; "
                  "use --all to re-triage)")
            return
        print(f"triage: {len(rep['triaged'])} recommendation(s), "
              f"{len(rep['failed'])} failed")
        for d in rep["triaged"]:
            print(f"  {d['recommendation']:<7} claim #{d['claim_id']} "
                  f"(conf {d['confidence']:.2f}): {d['reason']}")
        for f in rep["failed"]:
            print(f"  ! claim #{f['claim_id']}: {f['error']}")
        print("\nRecommendations are advisory — act with `wiki promote/reject` "
              "(see `wiki triage`).")


def cmd_adjudicate(args):
    cfg = LibrarianConfig.load()
    with Repo.open() as repo:
        rep = adjudicatemod.run(repo, cfg, only_unproposed=not args.all)
        if _emit(rep, args.json):
            return
        if not rep["proposed"] and not rep["failed"]:
            print("nothing to adjudicate — no open contradictions or escalations "
                  "(or all already proposed; use --all to re-propose)")
            return
        print(f"adjudicate: {len(rep['proposed'])} proposal(s), "
              f"{len(rep['failed'])} failed")
        for d in rep["proposed"]:
            print(f"  {d['kind']:<13} #{d['id']} (conf {d['confidence']:.2f}): "
                  f"{d['proposal']}")
        for f in rep["failed"]:
            print(f"  ! {f['kind']} #{f['id']}: {f['error']}")
        print("\nProposals are advisory — resolve/supersede/close stay human gates "
              "(`wiki contradiction resolve`, `wiki escalation close`).")


def cmd_status(args):
    cfg = LibrarianConfig.load()
    with Repo.open() as repo:
        pending = repo.one("SELECT COUNT(*) n FROM sources WHERE status='new'")["n"]
    out = {
        "auto_extract": cfg.enabled,
        "base_url": cfg.get("base_url"),
        "model": cfg.get("model") or None,
        "models": cfg.get("models"),
        "api_key_env": cfg.get("api_key_env") or None,
        "pending_sources": pending,
    }
    if _emit(out, args.json):
        return
    print(f"auto_extract: {'on' if cfg.enabled else 'off'}")
    print(f"endpoint:     {out['base_url']}")
    print(f"model:        {out['model'] or '(not configured)'}")
    for task, m in (out["models"] or {}).items():
        print(f"  {task}: {m}")
    print(f"key env:      {out['api_key_env'] or '(none — local endpoint)'}")
    print(f"pending:      {pending} source(s) awaiting extraction")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wiki-librarian",
        description="wiki-brain librarian — event-driven model judgment "
                    "(the `wiki` CLI itself stays zero-model-call)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def addj(sp):
        sp.add_argument("--json", action="store_true", help="machine-readable output")

    se = sub.add_parser("extract", help="extract one pending source, then gate + render")
    se.add_argument("--source", type=int, required=True)
    addj(se)
    se.set_defaults(func=cmd_extract)

    sc = sub.add_parser("catch-up",
                        help="extract every pending source (idempotent), then gate + render")
    addj(sc)
    sc.set_defaults(func=cmd_catch_up)

    st = sub.add_parser("triage",
                        help="recommend promote/reject/hold for gate-held pending "
                             "claims (advisory; never promotes)")
    st.add_argument("--all", action="store_true",
                    help="re-triage every pending claim, not just untriaged ones")
    addj(st)
    st.set_defaults(func=cmd_triage)

    sa = sub.add_parser("adjudicate",
                        help="draft proposals for open contradictions + escalations "
                             "(advisory; never resolves/closes)")
    sa.add_argument("--all", action="store_true",
                    help="re-propose every open item, not just unproposed ones")
    addj(sa)
    sa.set_defaults(func=cmd_adjudicate)

    ss = sub.add_parser("status", help="show librarian config + pending backlog")
    addj(ss)
    ss.set_defaults(func=cmd_status)
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
