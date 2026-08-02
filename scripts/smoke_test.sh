#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
BASE_PATHS="$(python -c 'import sys; print("\n".join(p for p in sys.path if "site-packages" in p))')"

python -m venv "$WORK/.venv"
# shellcheck disable=SC1091
source "$WORK/.venv/bin/activate"
site_dir="$(python -c 'import site; print(site.getsitepackages()[0])')"
printf '%s\n%s/src\n' "$BASE_PATHS" "$ROOT" > "$site_dir/tgv-smoke.pth"
cat > "$WORK/.venv/bin/tgv" <<EOF
#!/usr/bin/env bash
exec "$WORK/.venv/bin/python" -m tgv "\$@"
EOF
chmod +x "$WORK/.venv/bin/tgv"

mkdir -p "$WORK/repo/src"
printf 'def authenticate(user: str) -> bool:\n    return bool(user)\n' > "$WORK/repo/src/auth.py"
cp "$ROOT/config/tgv.example.yaml" "$WORK/repo/tgv.yaml"

cd "$WORK/repo"
tgv doctor --config tgv.yaml | grep -q '"config": "ok"'
tgv inspect . | grep -q '"file_count"'
tgv route "Where is authentication configured?" --repo . | grep -q '"route": "local_fast"'
tgv prepare "Fix refresh-token rotation" --repo . --output .tgv/bundles/auth
test -f .tgv/bundles/auth/bundle.json
test -f .tgv/bundles/auth/handoff.md

echo "TGV smoke test passed"
