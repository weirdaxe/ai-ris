import streamlit as st
import json
import html

# --- Example heavy worker (replace with your real logic) ---
def heavy_scrape_logic(country_name: str):
    # return list of tuples as requested
    return [
        (f"https://example.com/{country_name}/item1", "Item 1"),
        (f"https://example.com/{country_name}/item2", "Item 2"),
    ]

def scrape(country_name: str):
    return heavy_scrape_logic(country_name)

# --- Endpoint handling ---
params = st.experimental_get_query_params()
if params.get("action", [""])[0] == "scrape":
    country_name = params.get("country_name", [""])[0]
    raw_mode = params.get("raw", ["0"])[0] in ("1", "true", "True")

    if not country_name:
        st.json({"error": "Missing parameter: country_name"})
        st.stop()

    results = scrape(country_name)
    json_safe = [list(t) for t in results]
    payload = {"country": country_name, "count": len(json_safe), "results": json_safe}
    json_str = json.dumps(payload, indent=2)

    # If caller explicitly asked for raw output (for curl / programmatic)
    if raw_mode:
        # Display plain JSON text (easy for curl to read/parse)
        # st.text displays preformatted text (no extra markup like tables)
        st.text(json_str)
        st.stop()

    # Otherwise render a tiny HTML page that auto-downloads the JSON via JS.
    # Use st.components.v1.html to ensure the JS runs.
    # We HTML-escape the json_str for safe embedding inside a JS template string.
    escaped = html.escape(json_str)

    filename = f"{country_name}_results.json"

    auto_dl_html = f"""
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8"/>
        <title>Auto-download: {filename}</title>
      </head>
      <body>
        <p>If the download doesn't start automatically, use the link below.</p>
        <a id="dl" href="#" download="{filename}">Download JSON</a>

        <script>
          try {{
            // Recreate the JSON text from the escaped string:
            const jsonText = `{escaped}`;

            // Create a blob (safer than data URI for large payloads)
            const blob = new Blob([jsonText], {{ type: 'application/json' }});
            const url = URL.createObjectURL(blob);

            const a = document.getElementById('dl');
            a.href = url;
            a.download = "{filename}";

            // Auto-click to trigger download
            a.click();

            // Revoke object URL after a short delay
            setTimeout(() => {{
              URL.revokeObjectURL(url);
            }}, 1000);
          }} catch (err) {{
            // If anything fails, show an error link (user can click manually)
            console.error('Auto-download failed', err);
            const p = document.createElement('p');
            p.textContent = 'Automatic download failed — please click the link below.';
            document.body.insertBefore(p, document.getElementById('dl'));
          }}
        </script>

        <noscript>
          <p>JavaScript is disabled in your browser. Click the link to download the JSON.</p>
        </noscript>
      </body>
    </html>
    """

    # Render the HTML (allow scripts)
    import streamlit.components.v1 as components
    components.html(auto_dl_html, height=200)

    st.stop()

# Normal UI for interactive use
st.title("Scraper UI")
st.write("Use query params like ?action=scrape&country_name=France to trigger scraping.")
st.write("Add &raw=1 to get raw JSON text (useful for curl/programmatic callers).")
