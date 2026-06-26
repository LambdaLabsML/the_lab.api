#!/bin/bash
# Build the dashboard and deploy it by updating compatibility symlinks
# so the running server picks up the new build without restart.
set -e
cd "$(dirname "$0")/dashboard"
npm run build

ASSETS="$(dirname "$0")/the_lab/static/assets"
HTML="$(dirname "$0")/the_lab/static/index.html"

# Read the new asset hashes from the freshly built index.html
NEW_JS=$(grep -o 'assets/index-[^"]*\.js' "$HTML" | head -1 | sed 's|assets/||')
NEW_CSS=$(grep -o 'assets/index-[^"]*\.css' "$HTML" | head -1 | sed 's|assets/||')

echo "Built: $NEW_JS  $NEW_CSS"

# Read the current JS/CSS being served by the live server (cached in HTML)
# We can't easily read the server's _SPA_HTML, but we know the symlink names
# by checking what the server currently serves

# Update symlinks for compatibility with cached HTML
for f in "$ASSETS"/index-*.js; do
  base=$(basename "$f")
  [[ "$base" == "$NEW_JS" ]] && continue
  [[ -L "$f" ]] && continue  # skip existing symlinks
  # This is a real old JS file - create a symlink from it to the new one
  ln -sf "$ASSETS/$NEW_JS" "$f"
  echo "Symlinked: $base -> $NEW_JS"
done

for f in "$ASSETS"/index-*.css; do
  base=$(basename "$f")
  [[ "$base" == "$NEW_CSS" ]] && continue
  [[ -L "$f" ]] && continue
  ln -sf "$ASSETS/$NEW_CSS" "$f"
  echo "Symlinked: $base -> $NEW_CSS"
done

echo "Done. Serving fresh build through existing symlinks."
