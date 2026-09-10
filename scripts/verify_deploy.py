"""Compare the live Cloud Run service against the deploy flags in cloudbuild.yaml.

Runs as the last Cloud Build step, so every deploy proves its own config took
effect rather than assuming it did. Exits 1 on any mismatch.

    gcloud run services describe scalpengine --project=chimera-v4 \\
        --region=europe-west2 --format=json | python3 scripts/verify_deploy.py

Never prints a value that is not declared in --set-env-vars: unexpected env
vars are reported by name only, so a secret that leaks into a plain env var
cannot leak into the build log as well.
"""
import json
import re
import sys
from pathlib import Path

yaml_path = Path(__file__).resolve().parent.parent / "cloudbuild.yaml"
text = yaml_path.read_text()
deploy = text[text.index("- id: deploy"):]
deploy = deploy[:deploy.index("\n  - id:")] if "\n  - id:" in deploy else deploy[:deploy.index("\nimages:")]
args = {m.group(1): m.group(2) for m in re.finditer(r"- --([a-z-]+)(?:=(\S+))?", deploy)}

svc = json.load(sys.stdin)
tmpl = svc["spec"]["template"]
ann = tmpl["metadata"].get("annotations", {})
spec = tmpl["spec"]
box = spec["containers"][0]
env = {e["name"]: e for e in box.get("env", [])}

checks = [
    ("min-instances", args["min-instances"], ann.get("autoscaling.knative.dev/minScale")),
    ("max-instances", args["max-instances"], ann.get("autoscaling.knative.dev/maxScale")),
    ("no-cpu-throttling", "false", ann.get("run.googleapis.com/cpu-throttling")),
    ("cpu-boost", "true", ann.get("run.googleapis.com/startup-cpu-boost")),
    ("memory", args["memory"], box["resources"]["limits"].get("memory")),
    ("concurrency", args["concurrency"], str(spec.get("containerConcurrency"))),
    ("port", args["port"], str(box["ports"][0]["containerPort"])),
    ("service-account", args["service-account"], spec.get("serviceAccountName")),
]
cpu_live = box["resources"]["limits"].get("cpu")
checks.append(("cpu", args["cpu"], "1" if cpu_live in ("1", "1000m") else cpu_live))

declared = {}
for pair in args["set-env-vars"].split(","):
    k, v = pair.split("=", 1)
    declared[k] = v
    checks.append((f"env {k}", v, env.get(k, {}).get("value")))

secret_vars = set()
for pair in args["set-secrets"].split(","):
    name, ref = pair.split("=", 1)
    secret, version = ref.split(":")
    secret_vars.add(name)
    e = env.get(name, {})
    got = e.get("valueFrom", {}).get("secretKeyRef", {})
    live = f"{got.get('name')}:{got.get('key')}" + (" +PLAIN VALUE" if "value" in e else "")
    checks.append((f"secret {name}", ref, live))

failed = 0
for label, want, got in checks:
    ok = want == got
    failed += not ok
    print(f"{'OK  ' if ok else 'FAIL'} {label:22} want={want}  live={got}")

unexpected = sorted(set(env) - set(declared) - secret_vars)
if unexpected:
    failed += 1
    print(f"FAIL unexpected env vars (names only): {', '.join(unexpected)}")

print("\nDEPLOY MATCHES cloudbuild.yaml" if not failed else f"\n{failed} MISMATCH(ES) — live service differs from cloudbuild.yaml")
sys.exit(1 if failed else 0)
