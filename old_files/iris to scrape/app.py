# app.py
import streamlit as st
import os
import json
from typing import List

from scraper_utils import build_scraper, get_grouped_results, test_scraper, scrape_pages_collect_items, infer_field_mapping, assemble_items_from_grouped
from utils import (
    load_json_safe,
    ensure_file_exists,
    atomic_write_json,
    sanitize_site_name,
    config_paths_for_site,
)

LINKS_FILE = "links.json"
CONFIGS_DIR = "configs"

# Ensure necessary files/dirs
ensure_file_exists(LINKS_FILE)
os.makedirs(CONFIGS_DIR, exist_ok=True)

st.set_page_config(page_title="Scraping Settings Manager", layout="wide")
st.title("🔗 Scraping Settings Configurator")
st.write("Train AutoScraper rules and save them as config files under `configs/`. `links.json` maps site names → config files.")

# Load links.json mapping
links = load_json_safe(LINKS_FILE)

# --- Sidebar: select or create site ---
st.sidebar.header("Site selection")
site_choice = st.sidebar.selectbox("Choose existing site or create new", ["(New Site)"] + list(links.keys()))

if site_choice == "(New Site)":
    site_name = st.sidebar.text_input("New site name", "")
    url = st.sidebar.text_input("Base URL to scrape (example: https://pink.rs/politika)", "")
else:
    site_name = site_choice
    selected_config_file = links[site_choice]
    # Try load stored URL if exists in the saved config
    saved_config_path = os.path.join(CONFIGS_DIR, selected_config_file)
    saved_cfg = {}
    if os.path.exists(saved_config_path):
        try:
            with open(saved_config_path, "r", encoding="utf-8") as f:
                saved_cfg = json.load(f)
        except Exception:
            saved_cfg = {}
    url = st.sidebar.text_input("Base URL to scrape", saved_cfg.get("url", ""))

st.sidebar.markdown("---")
st.sidebar.write("Tip: provide example scraped elements (one per line) in the main area to train AutoScraper.")

# --- Main: training inputs ---
st.header("Train AutoScraper")
st.write("Provide sample elements found on the page (one per line). AutoScraper will learn rule groups from these.")

samples_text = st.text_area("Sample elements (one per line)", placeholder="https://pink.rs/politika/some-article\nSome article title\n01.11.2025")
samples = [s.strip() for s in samples_text.splitlines() if s.strip()]

# Session state for keeping the last-trained scraper & grouped results
if "last_grouped" not in st.session_state:
    st.session_state.last_grouped = {}
if "last_scraper_present" not in st.session_state:
    st.session_state.last_scraper_present = False
if "last_autoscraper_obj" not in st.session_state:
    st.session_state.last_autoscraper_obj = None
if "collected_items" not in st.session_state:
    st.session_state.collected_items = []

# new session state items for confirmed mapping and selection
if "confirmed_mapping" not in st.session_state:
    st.session_state.confirmed_mapping = None
if "confirmed_selected_rule_names" not in st.session_state:
    st.session_state.confirmed_selected_rule_names = None

col1, col2 = st.columns([2, 1])

with col1:
    if st.button("Train Scraper"):
        if not site_name or not url or not samples:
            st.error("Please set a site name, URL and provide at least one sample element.")
        else:
            with st.spinner("Training AutoScraper..."):
                try:
                    scraper = build_scraper(url, samples)
                    grouped = get_grouped_results(scraper, url)
                    st.session_state.last_grouped = grouped
                    st.session_state.last_scraper_present = True
                    st.session_state.last_autoscraper_obj = scraper
                    st.success("Training complete — review rule groups below.")
                    # Reset previously confirmed mapping because training changed groups
                    st.session_state.confirmed_mapping = None
                    st.session_state.confirmed_selected_rule_names = None
                except Exception as e:
                    st.error(f"Error while training scraper: {e}")
    st.markdown("### Latest trained rule groups")
    if st.session_state.last_grouped:
        st.write("AutoScraper produced the following groups. Check groups you want to persist.")
        # Show each group as checkbox (store selection into session_state keys)
        selected_local = {}
        for rule_name, values in st.session_state.last_grouped.items():
            label = f"{rule_name} ({len(values)} items) — preview: {values[:3]}"
            # store checkbox state under a per-rule key so it persists across reruns
            keyname = f"sel_{rule_name}"
            checked = st.checkbox(label, value=True, key=keyname)
            selected_local[rule_name] = checked
        # push a normalized list into session_state so other buttons can read it
        st.session_state.selected_rule_names = [k for k, v in selected_local.items() if v]

        # Also show an automatic mapping suggestion and allow override
        st.markdown("**Auto-detected mapping (heuristic)**")
        inferred = infer_field_mapping(st.session_state.last_grouped)
        colA, colB, colC = st.columns([1,1,1])
        with colA:
            st.write("Rule → guessed field")
            for r, f in inferred.items():
                st.write(f"- {r} → **{f}**")
        with colB:
            st.write("Pick the field for each rule (default is **other**):")
            manual_mapping = {}
            # choices list with 'other' as last; default index set to 4 (other)
            choices = ["auto", "title", "url", "date", "other"]
            for r in st.session_state.last_grouped.keys():
                # set default index 4 => 'other'
                sel_key = f"map_{r}"
                # if session already has manual mapping from a previous confirm, show that
                pre = st.session_state.get("manual_mapping", {}).get(r, "other")
                try:
                    default_index = choices.index(pre)
                except ValueError:
                    default_index = 4
                manual_mapping[r] = st.selectbox(f"Field for {r}", choices, index=default_index, key=sel_key)
            st.session_state.manual_mapping = manual_mapping

        # Confirm mapping button: this will save the selection + mapping to session_state
        st.markdown("**Confirm mapping and selected rules**")
        st.write("Click **Confirm mapping** to lock the selected rule groups and mapping. The confirmed selection will be used for pagination/collection and when saving later.")
        if st.button("Confirm mapping"):
            # Build the finalized mapping: for each rule, if manual == 'auto' use inferred, else use manual
            used_mapping = {}
            for r in inferred.keys():
                choice = st.session_state.manual_mapping.get(r, "other")
                if choice == "auto":
                    used_mapping[r] = inferred.get(r, "other")
                else:
                    used_mapping[r] = choice
            # Save confirmed mapping & selected rules
            st.session_state.confirmed_mapping = used_mapping
            st.session_state.confirmed_selected_rule_names = st.session_state.get("selected_rule_names", [])
            st.success("Mapping and selected rules confirmed and saved to session.")
            st.write("Confirmed mapping:", st.session_state.confirmed_mapping)
            st.write("Confirmed selected rule groups:", st.session_state.confirmed_selected_rule_names)

        # Allow preview assembled items from the mapping (uses current manual mapping if not confirmed)
        if st.button("Preview assembled items from mapping"):
            used_mapping_preview = {}
            for r in inferred.keys():
                choice = st.session_state.manual_mapping.get(r, "other")
                if choice == "auto":
                    used_mapping_preview[r] = inferred.get(r, "other")
                else:
                    used_mapping_preview[r] = choice
            assembled = assemble_items_from_grouped(st.session_state.last_grouped, used_mapping_preview)
            st.write(f"Previewing {len(assembled)} assembled items (first 20):")
            st.json(assembled[:20])
    else:
        st.write("_No trained groups to show yet — train the scraper first._")

with col2:
    st.markdown("### Quick test view")
    if st.button("Run test scrape now"):
        if not st.session_state.last_autoscraper_obj:
            st.error("No trained scraper in session. Train one first.")
        else:
            with st.spinner("Running test scrape..."):
                try:
                    grouped = test_scraper(st.session_state.last_autoscraper_obj, url, grouped=True)
                    st.json(grouped)
                except Exception as e:
                    st.error(f"Test failed: {e}")

# --- Pagination & collection options ---
st.markdown("---")
st.header("Pagination / Collection (optional)")

collect_pages = st.checkbox("Collect across pages (enable pagination)", value=False)
page_url_template = st.text_input("Page URL template (use {page}), e.g. https://kapital.kz/news?page={page}", value=f"{url}?page={{page}}" if url else "")
start_page = st.number_input("Start page", value=1, min_value=1, step=1)
max_pages = st.number_input("Max pages to scrape (safety cap)", value=10, min_value=1, step=1)
cutoff_date = st.text_input("Cutoff date (YYYY-MM-DD) — stop when oldest on a page is older than this (optional)", value="")
if cutoff_date:
    try:
        # quick validation
        import datetime
        datetime.datetime.strptime(cutoff_date, "%Y-%m-%d")
        cutoff_valid = True
    except Exception:
        cutoff_valid = False
        st.warning("Cutoff date not in YYYY-MM-DD format; it will be ignored.")
else:
    cutoff_valid = False

# Option to rename site filename
default_final_filename = f"{sanitize_site_name(site_name)}_scrape_config.json" if site_name else ""
final_filename = st.text_input("Final config filename (saved under configs/)", default_final_filename)

st.markdown("When you save, the app will merge selected rule groups into items and store them (title, url, date). If you confirmed a mapping earlier it will be used automatically for collection and saving.")

# --- Collect (pagination) action ---
if st.button("🔎 Collect items (from selected groups / pages)"):
    if not st.session_state.last_autoscraper_obj:
        st.error("No trained scraper available. Train first.")
    else:
        scraper = st.session_state.last_autoscraper_obj
        # determine which selected rules to use: prefer confirmed selection if present
        sel_rule_names = st.session_state.confirmed_selected_rule_names if st.session_state.confirmed_selected_rule_names is not None else st.session_state.get("selected_rule_names", [])
        # determine which mapping to use: prefer confirmed mapping if present
        mapping_to_use = st.session_state.confirmed_mapping if st.session_state.confirmed_mapping is not None else None

        # if no mapping_to_use yet, build from manual/inferred as fallback
        if mapping_to_use is None and st.session_state.last_grouped:
            inferred = infer_field_mapping(st.session_state.last_grouped)
            manual = st.session_state.get("manual_mapping", {})
            used_mapping = {}
            for r in inferred.keys():
                choice = manual.get(r, "other")
                if choice == "auto":
                    used_mapping[r] = inferred.get(r, "other")
                else:
                    used_mapping[r] = choice
            mapping_to_use = used_mapping

        # if user enabled pagination, run paginate collector
        collected = []
        pages_scraped = 0
        if collect_pages and page_url_template:
            with st.spinner("Collecting across pages..."):
                try:
                    # paginate collector should already rely on the scraper to fetch and group items;
                    # we will run the scraper pagination and then translate grouped results into items using mapping_to_use.
                    # determine which selected rules to use: prefer confirmed selection if present
                    sel_rule_names = st.session_state.confirmed_selected_rule_names or st.session_state.get("selected_rule_names", [])

                    # determine which mapping to use: prefer confirmed mapping if present
                    mapping_to_use = st.session_state.confirmed_mapping or st.session_state.get("manual_mapping")
                    items, pages_scraped = scrape_pages_collect_items(scraper, page_url_template, start_page=start_page, max_pages=max_pages, cutoff_date_iso=cutoff_date if cutoff_valid else None, mapping=mapping_to_use, selected_rule_names=sel_rule_names) if "scrape_pages_collect_items" in globals() else ([], 0)
                    # NOTE: If your scrape_pages_collect_items doesn't accept mapping/selected_rule_names, fallback to assembling after the fact:
                    if not items:
                        # try a fallback approach: run test_scraper per-page and assemble groups
                        # (this is a conservative fallback; you can replace with improved collector)
                        collected = []
                        for p in range(start_page, start_page + int(max_pages)):
                            page_url = page_url_template.format(page=p)
                            try:
                                grouped = test_scraper(scraper, page_url, grouped=True)
                                assembled = assemble_items_from_grouped(grouped, mapping_to_use)
                                collected.extend(assembled)
                                pages_scraped += 1
                            except Exception:
                                break
                    else:
                        collected = items
                except Exception as e:
                    st.error(f"Pagination collection failed: {e}")
                    collected = []
        else:
            # single-page assemble using last_grouped and mapping
            if not st.session_state.last_grouped:
                st.error("No grouped data to assemble from — run a test scrape or train first.")
            else:
                used_mapping = mapping_to_use or {}
                collected = assemble_items_from_grouped(st.session_state.last_grouped, used_mapping)
                pages_scraped = 1

        # dedupe by URL preserving order
        seen = set()
        deduped = []
        for it in collected:
            u = it.get("url")
            if u and u in seen:
                continue
            if u:
                seen.add(u)
            deduped.append(it)
        st.session_state.collected_items = deduped
        st.success(f"Collected {len(deduped)} items across {pages_scraped} pages.")
        if len(deduped) > 0:
            st.write("Preview (first 50):")
            st.json(deduped[:50])
        else:
            st.info("No items were collected — check mapping and that pages contain expected elements.")

# --- Save selected rules as final config (now saves items) ---
if st.button("💾 Save collected items as config"):
    if not site_name:
        st.error("A site name is required.")
    elif not st.session_state.get("collected_items"):
        st.error("No collected items to save. Use 'Collect items' first.")
    else:
        final_urls_items = st.session_state.collected_items

        # Build final config JSON structure we will persist and point to from links.json
        final_config = {
            "site_name": site_name,
            "url": url,
            "saved_from_rules": st.session_state.confirmed_selected_rule_names if st.session_state.confirmed_selected_rule_names is not None else st.session_state.get("selected_rule_names", []),
            "mapping": st.session_state.confirmed_mapping if st.session_state.confirmed_mapping is not None else st.session_state.get("manual_mapping", {}),
            "filter": None,
            "items": final_urls_items
        }

        # Determine final filename and write to configs/
        final_filename = final_filename.strip() or f"{sanitize_site_name(site_name)}_scrape_config.json"
        final_path = os.path.join(CONFIGS_DIR, final_filename)
        try:
            atomic_write_json(final_path, final_config)
        except Exception as e:
            st.error(f"Failed to write config file: {e}")
        else:
            # Update links.json mapping
            links = load_json_safe(LINKS_FILE)
            links[site_name] = final_filename
            try:
                atomic_write_json(LINKS_FILE, links)
            except Exception as e:
                st.error(f"Failed to update {LINKS_FILE}: {e}")
            else:
                st.success(f"Saved final config to `configs/{final_filename}` and updated `{LINKS_FILE}`.")
                st.json(final_config)

# --- Section: inspect or edit existing saved config ---
st.markdown("---")
st.header("Open / Edit existing saved config")
existing_site = st.selectbox("Open site", ["(pick one)"] + list(links.keys()), key="open_site_select")

if existing_site and existing_site != "(pick one)":
    cfg_filename = links[existing_site]
    cfg_path = os.path.join(CONFIGS_DIR, cfg_filename)
    if os.path.exists(cfg_path):
        cfg = load_json_safe(cfg_path)
        st.subheader(f"Loaded config: {cfg_filename}")
        st.write("URL:", cfg.get("url"))
        st.write("Saved from rules:", cfg.get("saved_from_rules"))
        st.write("Mapping:", cfg.get("mapping"))
        st.write("Filter:", cfg.get("filter"))
        st.write(f"Number of saved items: {len(cfg.get('items', []))}")
        if st.checkbox("Show saved items"):
            for itm in cfg.get("items", []):
                st.write(itm)

        st.markdown("### Edit saved items (manual changes)")
        edited_text = st.text_area("Edit items as JSON (list of dicts with title/url/date)", value=json.dumps(cfg.get("items", []), indent=2, ensure_ascii=False))
        if st.button("Save edits to existing config"):
            try:
                updated_items = json.loads(edited_text)
                cfg["items"] = updated_items
                atomic_write_json(cfg_path, cfg)
                st.success("Saved edits.")
            except Exception as e:
                st.error(f"Failed to save edits: {e}")
    else:
        st.error("Saved config file missing on disk.")

# --- Show current links.json mapping ---
st.markdown("---")
st.subheader("📂 Current `links.json` mappings")
st.json(load_json_safe(LINKS_FILE))

# # app.py
# import streamlit as st
# import os
# import json
# from typing import List

# from scraper_utils import build_scraper, get_grouped_results, test_scraper
# from utils import (
#     load_json_safe,
#     ensure_file_exists,
#     atomic_write_json,
#     sanitize_site_name,
#     config_paths_for_site,
# )

# LINKS_FILE = "links.json"
# CONFIGS_DIR = "configs"

# # Ensure necessary files/dirs
# ensure_file_exists(LINKS_FILE)
# os.makedirs(CONFIGS_DIR, exist_ok=True)

# st.set_page_config(page_title="Scraping Settings Manager", layout="wide")
# st.title("🔗 Scraping Settings Configurator")
# st.write("Train AutoScraper rules and save them as config files under `configs/`. `links.json` maps site names → config files.")

# # Load links.json mapping
# links = load_json_safe(LINKS_FILE)

# # --- Sidebar: select or create site ---
# st.sidebar.header("Site selection")
# site_choice = st.sidebar.selectbox("Choose existing site or create new", ["(New Site)"] + list(links.keys()))

# if site_choice == "(New Site)":
#     site_name = st.sidebar.text_input("New site name", "")
#     url = st.sidebar.text_input("Base URL to scrape (example: https://pink.rs/politika)", "")
# else:
#     site_name = site_choice
#     selected_config_file = links[site_choice]
#     # Try load stored URL if exists in the saved config
#     saved_config_path = os.path.join(CONFIGS_DIR, selected_config_file)
#     saved_cfg = {}
#     if os.path.exists(saved_config_path):
#         try:
#             with open(saved_config_path, "r", encoding="utf-8") as f:
#                 saved_cfg = json.load(f)
#         except Exception:
#             saved_cfg = {}
#     url = st.sidebar.text_input("Base URL to scrape", saved_cfg.get("url", ""))

# st.sidebar.markdown("---")
# st.sidebar.write("Tip: provide example scraped elements (one per line) in the main area to train AutoScraper.")

# # --- Main: training inputs ---
# st.header("Train AutoScraper")
# st.write("Provide sample elements found on the page (one per line). AutoScraper will learn rule groups from these.")

# samples_text = st.text_area("Sample elements (one per line)", placeholder="https://pink.rs/politika/some-article\nSome article title")
# samples = [s.strip() for s in samples_text.splitlines() if s.strip()]

# # Session state for keeping the last-trained scraper & grouped results
# if "last_grouped" not in st.session_state:
#     st.session_state.last_grouped = {}
# if "last_scraper_present" not in st.session_state:
#     st.session_state.last_scraper_present = False
# if "last_autoscraper_obj" not in st.session_state:
#     st.session_state.last_autoscraper_obj = None

# col1, col2 = st.columns([2, 1])

# with col1:
#     if st.button("Train Scraper"):
#         if not site_name or not url or not samples:
#             st.error("Please set a site name, URL and provide at least one sample element.")
#         else:
#             with st.spinner("Training AutoScraper..."):
#                 try:
#                     scraper = build_scraper(url, samples)
#                     grouped = get_grouped_results(scraper, url)
#                     st.session_state.last_grouped = grouped
#                     st.session_state.last_scraper_present = True
#                     st.session_state.last_autoscraper_obj = scraper
#                     st.success("Training complete — review rule groups below.")
#                 except Exception as e:
#                     st.error(f"Error while training scraper: {e}")
#     st.markdown("### Latest trained rule groups")
#     if st.session_state.last_grouped:
#         st.write("AutoScraper produced the following groups. Check groups you want to persist.")
#         # Show each group as checkbox
#         selected = {}
#         for rule_name, values in st.session_state.last_grouped.items():
#             # show small preview
#             label = f"{rule_name} ({len(values)} items) — preview: {values[:3]}"
#             selected[rule_name] = st.checkbox(label, value=True, key=f"sel_{rule_name}")
#         st.session_state.selected_rule_names = [k for k, v in selected.items() if v]
#     else:
#         st.write("_No trained groups to show yet — train the scraper first._")

# with col2:
#     st.markdown("### Quick test view")
#     if st.button("Run test scrape now"):
#         if not st.session_state.last_autoscraper_obj:
#             st.error("No trained scraper in session. Train one first.")
#         else:
#             with st.spinner("Running test scrape..."):
#                 try:
#                     grouped = test_scraper(st.session_state.last_autoscraper_obj, url, grouped=True)
#                     st.json(grouped)
#                 except Exception as e:
#                     st.error(f"Test failed: {e}")

# # --- Modifier / filter UI ---
# st.markdown("---")
# st.header("Filtering & persistence options (applies when saving)")

# filter_mode = st.selectbox("Filter mode (applies to selected URLs before saving)", ["none", "prefix", "contains", "regex"])
# filter_value = ""
# if filter_mode != "none":
#     filter_value = st.text_input(f"Filter value ({filter_mode})", "")

# # Option to rename site filename
# default_final_filename = f"{sanitize_site_name(site_name)}_scrape_config.json" if site_name else ""
# final_filename = st.text_input("Final config filename (saved under configs/)", default_final_filename)

# st.markdown("When you save, the app will merge selected rule groups into a single key `urls` and store the filter alongside so it can be re-applied later.")

# # --- Save selected rules as final config ---
# if st.button("💾 Save selected rules as config"):
#     if not site_name:
#         st.error("A site name is required.")
#     elif not st.session_state.get("selected_rule_names"):
#         st.error("No rule groups selected to save. Train and select groups first.")
#     else:
#         sel_names = st.session_state.selected_rule_names
#         grouped = st.session_state.last_grouped

#         # merge selected groups into a single set of URLs
#         merged = []
#         for name in sel_names:
#             merged.extend(grouped.get(name, []))
#         # unique while preserving order
#         seen = set()
#         merged_unique = [x for x in merged if not (x in seen or seen.add(x))]

#         # apply filter if set
#         def apply_filter(items: List[str], mode: str, pattern: str):
#             if not pattern or mode == "none":
#                 return items
#             import re
#             out = []
#             for it in items:
#                 try:
#                     if mode == "prefix" and it.startswith(pattern):
#                         out.append(it)
#                     elif mode == "contains" and pattern in it:
#                         out.append(it)
#                     elif mode == "regex" and re.search(pattern, it):
#                         out.append(it)
#                 except Exception:
#                     continue
#             return out

#         final_urls = apply_filter(merged_unique, filter_mode, filter_value)

#         # Build final config JSON structure we will persist and point to from links.json
#         final_config = {
#             "site_name": site_name,
#             "url": url,
#             "saved_from_rules": sel_names,
#             "filter": {"mode": filter_mode, "value": filter_value} if filter_mode != "none" else None,
#             "urls": final_urls
#         }

#         # Determine final filename and write to configs/
#         final_filename = final_filename.strip() or f"{sanitize_site_name(site_name)}_scrape_config.json"
#         final_path = os.path.join(CONFIGS_DIR, final_filename)
#         try:
#             atomic_write_json(final_path, final_config)
#         except Exception as e:
#             st.error(f"Failed to write config file: {e}")
#         else:
#             # Update links.json mapping
#             links = load_json_safe(LINKS_FILE)
#             links[site_name] = final_filename
#             try:
#                 atomic_write_json(LINKS_FILE, links)
#             except Exception as e:
#                 st.error(f"Failed to update {LINKS_FILE}: {e}")
#             else:
#                 st.success(f"Saved final config to `configs/{final_filename}` and updated `{LINKS_FILE}`.")
#                 st.json(final_config)

# # --- Section: inspect or edit existing saved config ---
# st.markdown("---")
# st.header("Open / Edit existing saved config")
# existing_site = st.selectbox("Open site", ["(pick one)"] + list(links.keys()), key="open_site_select")

# if existing_site and existing_site != "(pick one)":
#     cfg_filename = links[existing_site]
#     cfg_path = os.path.join(CONFIGS_DIR, cfg_filename)
#     if os.path.exists(cfg_path):
#         cfg = load_json_safe(cfg_path)
#         st.subheader(f"Loaded config: {cfg_filename}")
#         st.write("URL:", cfg.get("url"))
#         st.write("Saved from rules:", cfg.get("saved_from_rules"))
#         st.write("Filter:", cfg.get("filter"))
#         st.write(f"Number of saved urls: {len(cfg.get('urls', []))}")
#         if st.checkbox("Show saved urls"):
#             for u in cfg.get("urls", []):
#                 st.write(u)

#         st.markdown("### Edit saved urls (manual changes)")
#         edited_text = st.text_area("Edit the saved URLs (one per line)", value="\n".join(cfg.get("urls", [])))
#         if st.button("Save edits to existing config"):
#             updated_urls = [s.strip() for s in edited_text.splitlines() if s.strip()]
#             cfg["urls"] = updated_urls
#             try:
#                 atomic_write_json(cfg_path, cfg)
#                 st.success("Saved edits.")
#             except Exception as e:
#                 st.error(f"Failed to save edits: {e}")
#     else:
#         st.error("Saved config file missing on disk.")

# # --- Show current links.json mapping ---
# st.markdown("---")
# st.subheader("📂 Current `links.json` mappings")
# st.json(load_json_safe(LINKS_FILE))


# # import streamlit as st
# # import os
# # from scraper_utils import build_scraper, test_scraper, save_config, load_configs

# # CONFIG_FILE = "links.json"

# # st.set_page_config(page_title="Scraping Settings Manager", layout="wide")

# # st.title("🔗 Scraping Settings Configurator")
# # st.write("Train AutoScraper rules and save them as individual config files. `links.json` maps site names → config files.")

# # # Load configs at start
# # configs = load_configs(CONFIG_FILE)

# # # Sidebar
# # st.sidebar.header("Existing Configs")
# # site_choice = st.sidebar.selectbox("Select site", ["(New Site)"] + list(configs.keys()))

# # if site_choice == "(New Site)":
# #     site_name = st.text_input("Enter new site name")
# #     url = st.text_input("URL to scrape")
# # else:
# #     site_name = site_choice
# #     url = st.text_input("URL to scrape", configs[site_choice]["url"])

# # samples = st.text_area("Sample elements to train scraper", placeholder="One per line...").splitlines()
# # samples = [s.strip() for s in samples if s.strip()]

# # if "scraper" not in st.session_state:
# #     st.session_state.scraper = None
# # if "config_filename" not in st.session_state:
# #     st.session_state.config_filename = None
# # if "site_name" not in st.session_state:
# #     st.session_state.site_name = None
# # if "url" not in st.session_state:
# #     st.session_state.url = None

# # # Train scraper
# # if st.button("Train Scraper"):
# #     if not site_name or not url or not samples:
# #         st.error("Please provide all required fields.")
# #     else:
# #         with st.spinner("Training scraper..."):
# #             scraper = build_scraper(url, samples)
# #             results = test_scraper(scraper, url)

# #         st.session_state.scraper = scraper
# #         st.session_state.config_filename = f"{site_name}_scrape_config.json"
# #         st.session_state.site_name = site_name
# #         st.session_state.url = url

# #         scraper.save(st.session_state.config_filename)
# #         st.success(f"✅ Scraper saved as `{st.session_state.config_filename}`")
# #         st.json(results)

# #         # Download button
# #         with open(st.session_state.config_filename, "rb") as f:
# #             st.download_button(
# #                 label="⬇️ Download Config File",
# #                 data=f.read(),
# #                 file_name=st.session_state.config_filename,
# #                 mime="application/json"
# #             )

# # # Update links.json
# # if st.button("💾 Update links.json"):
# #     if not st.session_state.get("config_filename"):
# #         st.error("Please train a scraper first.")
# #     else:
# #         updated_configs = save_config(
# #             CONFIG_FILE,
# #             st.session_state.site_name,
# #             st.session_state.url,
# #             st.session_state.config_filename
# #         )
# #         st.success(f"✅ Added `{st.session_state.site_name}` → `{st.session_state.config_filename}`")
# #         st.write("Updated links.json:")
# #         st.json(updated_configs)

# # # Display current configs
# # st.subheader("📂 Current links.json mappings")
# # st.json(load_configs(CONFIG_FILE))
