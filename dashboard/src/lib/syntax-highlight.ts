// ------------------------------------------------------------
// syntaxHighlight — extracted from dashboard.html (line ~2284).
// Returns HTML-highlighted JSON with colored spans.
// ------------------------------------------------------------

/**
 * Apply syntax highlighting to a pretty-printed JSON string.
 *
 * The input should already be the result of `JSON.stringify(obj, null, 2)`.
 * The returned string contains `<span style="color:...">` wrappers
 * suitable for use with `dangerouslySetInnerHTML`.
 */
export function syntaxHighlight(json: string): string {
  // Escape HTML entities first
  json = json.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  return json.replace(
    /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/g,
    function (match: string): string {
      let cls = 'color:#d2a8ff'; // number
      if (/^"/.test(match)) {
        if (/:$/.test(match)) {
          cls = 'color:#79c0ff'; // key
        } else {
          cls = 'color:#a5d6a7'; // string
        }
      } else if (/true|false/.test(match)) {
        cls = 'color:#d29922'; // boolean
      } else if (/null/.test(match)) {
        cls = 'color:#8b949e'; // null
      }
      return '<span style="' + cls + '">' + match + '</span>';
    },
  );
}
