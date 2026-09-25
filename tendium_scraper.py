#!/usr/bin/env python3
"""
Tendium procurement scraper.

Uses a persistent Playwright browser context so a manual login only has to
happen once. Subsequent runs reuse the saved session (cookies/local storage)
stored in the SESSION_DIR folder.

First run (run directly in a terminal, or via manage.py's web dashboard):
    - The script detects there's no saved session yet and forces a real,
      visible Chromium window regardless of the "headless" config setting.
    - Log into your Tendium account manually in that window.
    - Once you land on the dashboard/watchlist page, confirm the login:
        - Run directly in a terminal: press Enter when prompted.
        - Run from manage.py: click "I've logged in" in the dashboard.
    - The session is then persisted to disk in tendium_session/.

Subsequent runs:
    - Set "headless": true in config.json (or via the manage.py dashboard)
      for unattended/cron use. The script reuses the saved session and
      skips the login step entirely.

Configuration lives in config.json (see scraper_config.py), not as constants
in this file, so the CLI script and the web management UI always agree on
the current settings.
"""

import argparse
import calendar
import csv
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from urllib.parse import urlparse, parse_qs

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from scraper_config import load_config
from credentials import load_credentials

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

SESSION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tendium_session")
OUTPUT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tendium_tenders.csv")
DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug")
FILTER_STATUS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "filter_status.json")
AVAILABLE_FILTERS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "available_filters.json")

# Tendium runs continuous background traffic (PostHog session recording,
# GTM, survey widgets, etc.), so the browser can go minutes without ever
# reporting true "networkidle". These waits are a best-effort "let the SPA
# render" pause, not a required gate — cap them short so a never-idle page
# doesn't stall (or, worse, get mistaken for a failure) at every navigation.
SETTLE_TIMEOUT_MS = 8_000

CSV_FIELDS = [
    "scraped_at",
    "title",
    "procurement_type",
    "tags",
    "deadline",
    "buyer",
    "region",
    "responsible_name",
    "responsible_email",
    "responsible_phone",
    "contracted_company",
    "contract_start",
    "contract_end",
    "extension_period",
    "description",
    "url",
    "contract_url",
]

# Fields only available from a tender's own detail page or a linked
# contract/avtal page (not the list/card view), keyed here so callers can
# build a "no detail fetched" placeholder.
DETAIL_FIELDS = [
    "procurement_type",
    "responsible_name",
    "responsible_email",
    "responsible_phone",
    "contracted_company",
    "contract_start",
    "contract_end",
    "extension_period",
    "tags",
    "contract_url",
]


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def human_delay(min_s=0.6, max_s=1.8):
    """Sleep a random short amount of time to look less robotic."""
    time.sleep(random.uniform(min_s, max_s))


def human_scroll(page, steps=6):
    """Scroll down the page gradually instead of jumping straight to the bottom."""
    try:
        for _ in range(steps):
            page.mouse.wheel(0, random.randint(300, 800))
            human_delay(0.3, 0.9)
    except Exception as exc:
        print(f"[warn] scrolling interaction failed (non-fatal): {exc}")


def is_session_initialized() -> bool:
    """A very rough check: does the session folder already contain browser state?"""
    return os.path.isdir(SESSION_DIR) and len(os.listdir(SESSION_DIR)) > 0


def save_debug_snapshot(page, name):
    """
    Save the current page's full HTML and a full-page screenshot to debug/
    so real selectors can be worked out (or extraction failures diagnosed)
    without needing to manually open devtools.
    """
    os.makedirs(DEBUG_DIR, exist_ok=True)
    html_path = os.path.join(DEBUG_DIR, f"{name}.html")
    png_path = os.path.join(DEBUG_DIR, f"{name}.png")
    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(page.content())
        page.screenshot(path=png_path, full_page=True)
        print(f"[info] Saved debug snapshot: {html_path} / {png_path}")
    except Exception as exc:
        print(f"[warn] Failed to save debug snapshot '{name}': {exc}")


LOGIN_EMAIL_SELECTORS = [
    "input[type='email']",
    "input[autocomplete='username']",
    "input[name='email']",
    "input[name='username']",
    "#email",
    "[data-testid='email-input']",
]
LOGIN_PASSWORD_SELECTORS = [
    "input[type='password']",
    "input[autocomplete='current-password']",
    "input[name='password']",
    "#password",
    "[data-testid='password-input']",
]
LOGIN_SUBMIT_SELECTORS = [
    "button[type='submit']",
    "[data-testid='login-submit']",
    "button:has-text('Logga in')",
    "button:has-text('Log in')",
    "button:has-text('Sign in')",
    "input[type='submit']",
]


def _first_locator(scope, selectors):
    """Return the first matching Playwright locator, or None."""
    for sel in selectors:
        try:
            loc = scope.locator(sel).first
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def page_appears_logged_out(page):
    """Heuristic: a password field on the page almost certainly means we're
    looking at a login form, not the authenticated app.
    """
    return _first_locator(page, LOGIN_PASSWORD_SELECTORS) is not None


def attempt_auto_login(page, email, password):
    """
    Fill and submit Tendium's login form directly via Playwright, bypassing
    the OS window entirely — this works even when the browser window itself
    doesn't respond to real clicks/keystrokes (a known issue with GUI windows
    opened by a background/subprocess-launched browser on some setups).

    NOTE: selectors are best-effort placeholders — adjust once the real
    login form markup is known. Never logs the credential values themselves.
    """
    email_field = _first_locator(page, LOGIN_EMAIL_SELECTORS)
    password_field = _first_locator(page, LOGIN_PASSWORD_SELECTORS)
    if not email_field or not password_field:
        print("[warn] Could not find email/password fields on the login page "
              "(LOGIN_*_SELECTORS in tendium_scraper.py may need updating).")
        return False

    try:
        email_field.fill(email)
        human_delay(0.3, 0.7)
        password_field.fill(password)
        human_delay(0.3, 0.7)

        submit_btn = _first_locator(page, LOGIN_SUBMIT_SELECTORS)
        if submit_btn:
            submit_btn.click()
        else:
            password_field.press("Enter")
    except Exception as exc:
        print(f"[warn] Automatic login attempt failed while filling/submitting the form: {exc}")
        return False

    # Best-effort settle only — do NOT let a networkidle timeout here read as
    # "login failed". Whether it actually worked is checked separately by the
    # caller via page_appears_logged_out(), which is the real signal.
    try:
        page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        pass
    return True


def wait_for_manual_login(page, signal_file=None, poll_interval_s=2.0, timeout_s=900):
    """Block until the user has finished logging in.

    - signal_file is None (interactive terminal run): block on input().
    - signal_file is set (e.g. launched by manage.py with no attached TTY):
      poll for that file's existence instead, since input() would just raise
      EOFError with no terminal to read from. The web dashboard creates the
      file when the user clicks "I've logged in".
    """
    print("\n" + "=" * 70)
    print("MANUAL LOGIN REQUIRED")
    print("A browser window has opened. Please log into your Tendium account.")
    print("Once you can see your dashboard / watchlist, come back here and")
    if signal_file:
        print("click \"I've logged in\" in the management dashboard to continue.")
    else:
        print("press Enter to continue and save the session.")
    print("=" * 70)

    if not signal_file:
        input("Press Enter once you are logged in... ")
        return

    waited = 0.0
    while not os.path.isfile(signal_file):
        time.sleep(poll_interval_s)
        waited += poll_interval_s
        if waited >= timeout_s:
            raise TimeoutError(
                f"Timed out after {int(waited)}s waiting for login confirmation "
                f"(signal file {signal_file} never appeared)."
            )
        if int(waited) % 30 == 0:
            print(f"[info] Still waiting for login confirmation... ({int(waited)}s elapsed)")

    try:
        os.remove(signal_file)
    except OSError:
        pass
    print("[info] Login confirmation received.")


TENDER_ROW_SELECTOR = "tr[data-testid^='tenders-table-item-']"


def extract_tenders_from_page(page):
    """
    Extract tender rows from Tendium's find/active-tenders table.

    Confirmed against a real, authenticated capture of
    https://app.tendium.com/find/active-tenders/tender-filter/<id>?tenderStatus=Awarded
    (see debug/list_page.html): each tender is a <tr data-testid="tenders-table-item-<id>">
    with title, buyer, and deadline visible directly in its cells. There is no
    <a href> on the row — the ".row_testid" captured here is used later to
    click the row open and read the resulting preview URL.
    """
    results = []
    rows = page.locator(TENDER_ROW_SELECTOR)
    try:
        count = rows.count()
    except Exception:
        count = 0

    if count == 0:
        print(f"[warn] No tender rows matched '{TENDER_ROW_SELECTOR}'. "
              "Tendium's markup may have changed — inspect debug/list_page.html.")
        return results

    print(f"[info] Found {count} tender row(s).")
    for i in range(count):
        row_el = rows.nth(i)
        try:
            row_testid = row_el.get_attribute("data-testid")
            title = _first_text(row_el, ["[class*='tenderTitle']"])
            buyer = _first_text(row_el, ["[class*='_buyerLink_']", "[class*='truncatedText']"])
            deadline = _first_text(row_el, [
                "[data-testid='deadline-active'] [class*='_date_']",
                "[data-testid='deadline-expired'] [class*='_date_']",
            ])

            row = {field: "" for field in DETAIL_FIELDS}
            row.update({
                "scraped_at": datetime.now().isoformat(timespec="seconds"),
                "title": title or "",
                "deadline": deadline or "",
                "buyer": buyer or "",
                "region": "",
                "description": "",
                "url": "",
                "_row_testid": row_testid,
            })
            results.append(row)
        except Exception as exc:
            print(f"[warn] Failed to parse row #{i}: {exc}")
            continue

    return results


def _first_text(scope, selectors):
    """Return the trimmed text content of the first selector that matches, or None."""
    for sel in selectors:
        try:
            loc = scope.locator(sel).first
            if loc.count() > 0:
                text = loc.inner_text(timeout=2000).strip()
                if text:
                    return text
        except Exception:
            continue
    return None


def _field_by_label(scope, labels):
    """
    Best-effort lookup of a "label: value" field on a detail page, trying a
    few common markup patterns (definition lists, label/value sibling
    elements, inline "Label: value" text). Tendium's real markup may need a
    different strategy once you can inspect it — this is a starting point.
    """
    for label in labels:
        safe_label = label.replace("'", "\\'")
        try:
            dt = scope.locator(f"xpath=.//dt[contains(normalize-space(.), '{safe_label}')]").first
            if dt.count() > 0:
                dd = dt.locator("xpath=following-sibling::dd[1]")
                if dd.count() > 0:
                    text = dd.inner_text(timeout=2000).strip()
                    if text:
                        return text
        except Exception:
            pass

        try:
            label_el = scope.locator(
                f"xpath=.//*[self::div or self::span or self::p or self::th or self::dt]"
                f"[contains(normalize-space(.), '{safe_label}') and string-length(normalize-space(.)) < 60]"
            ).first
            if label_el.count() > 0:
                sib = label_el.locator("xpath=following-sibling::*[1]")
                if sib.count() > 0:
                    text = sib.inner_text(timeout=2000).strip()
                    if text and text != label:
                        return text
        except Exception:
            pass

        try:
            el = scope.locator(f"xpath=.//*[contains(text(), '{safe_label}:')]").first
            if el.count() > 0:
                full = el.inner_text(timeout=2000).strip()
                if ":" in full:
                    value = full.split(":", 1)[1].strip()
                    if value:
                        return value
        except Exception:
            pass

    return None


def _extract_tags(scope):
    """Collect tag/chip-like elements (e.g. category labels such as
    'ekonomisystem'). Adjust candidate selectors once real markup is known.
    """
    candidate_tag_selectors = [
        "[data-testid='tag']",
        "[data-testid='category']",
        ".tag",
        ".chip",
        ".badge",
        ".label-pill",
    ]
    for selector in candidate_tag_selectors:
        try:
            loc = scope.locator(selector)
            count = loc.count()
        except Exception:
            count = 0
        if count == 0:
            continue
        tags = []
        for i in range(count):
            try:
                text = loc.nth(i).inner_text(timeout=1000).strip()
                if text and text not in tags:
                    tags.append(text)
            except Exception:
                continue
        if tags:
            return tags
    return []


def _extract_email(scope):
    try:
        link = scope.locator("a[href^='mailto:']").first
        if link.count() > 0:
            href = link.get_attribute("href")
            if href:
                return href.replace("mailto:", "").split("?")[0].strip()
    except Exception:
        pass
    return None


def _extract_phone(scope):
    try:
        link = scope.locator("a[href^='tel:']").first
        if link.count() > 0:
            href = link.get_attribute("href")
            if href:
                return href.replace("tel:", "").strip()
    except Exception:
        pass
    return None


def _parse_period(text):
    """Split a raw 'Avtalsperiod' string like '2024-01-01 - 2026-12-31' into
    (start, end). Falls back to (text, None) if it can't be split.
    """
    if not text:
        return None, None
    for sep in (" - ", " – ", "–", " till ", " to "):
        if sep in text:
            parts = [p.strip() for p in text.split(sep, 1)]
            if len(parts) == 2 and parts[0] and parts[1]:
                return parts[0], parts[1]
    return text, None


def extract_tender_detail(page):
    """
    Extract the fields that only live on a tender's own detail page:
    procurement type, responsible contact, awarded/contracted company,
    contract period + extension option, and tags.

    NOTE: label text and selectors here are best-effort placeholders — adjust
    them once you can inspect a real Tendium tender detail page.
    """
    procurement_type = _field_by_label(page, [
        "Typ av upphandling", "Upphandlingstyp", "Förfarande", "Typ",
    ])
    responsible_name = _field_by_label(page, [
        "Ansvarig", "Kontaktperson", "Handläggare", "Upphandlare",
    ])
    contracted_company = _field_by_label(page, [
        "Leverantör", "Avtalspart", "Vinnande leverantör", "Tilldelad leverantör",
    ])
    contract_period_raw = _field_by_label(page, [
        "Avtalsperiod", "Kontraktsperiod", "Avtalstid",
    ])
    extension_period = _field_by_label(page, [
        "Tilläggstid", "Förlängning", "Optioner", "Options", "Förlängningsoption",
    ])

    contract_start, contract_end = _parse_period(contract_period_raw)
    tags = _extract_tags(page)

    return {
        "procurement_type": procurement_type or "",
        "responsible_name": responsible_name or "",
        "responsible_email": _extract_email(page) or "",
        "responsible_phone": _extract_phone(page) or "",
        "contracted_company": contracted_company or "",
        "contract_start": contract_start or "",
        "contract_end": contract_end or "",
        "extension_period": extension_period or "",
        "tags": "|".join(tags),
    }


def _wait_for_text(locator, timeout_ms=6000, interval_s=0.4):
    """Poll a locator for non-empty text — the AI summary and outcome table
    render asynchronously after the preview container itself already exists,
    so a bare wait_for_selector (DOM presence) isn't enough.

    Uses text_content() rather than inner_text(): some summary blocks sit
    under a CSS blur/"limited preview" teaser style, which makes inner_text()
    (rendered/visible text only) come back empty even though the real text is
    already sitting in the DOM. text_content() reads it regardless of that
    styling — the data was already delivered to this authenticated session.
    """
    waited_ms = 0
    while waited_ms < timeout_ms:
        try:
            if locator.count() > 0:
                text = (locator.text_content(timeout=1000) or "").strip()
                if text:
                    return text
        except Exception:
            pass
        time.sleep(interval_s)
        waited_ms += int(interval_s * 1000)
    return ""


def _parse_duration_to_months(text):
    """Parse a Swedish duration phrase like '24 månader' or '2 år' into a
    number of months. Returns None if no recognizable duration is found."""
    if not text:
        return None
    match = re.search(r"(\d+)\s*(månad|mån|år)", text, re.IGNORECASE)
    if not match:
        return None
    count = int(match.group(1))
    unit = match.group(2).lower()
    return count * 12 if unit == "år" else count


def _add_months(date_obj, months):
    total = date_obj.month - 1 + months
    year = date_obj.year + total // 12
    month = total % 12 + 1
    day = min(date_obj.day, calendar.monthrange(year, month)[1])
    return date_obj.replace(year=year, month=month, day=day)


def extract_kontraktsperiod_facts(scope):
    """
    Read the "Kontraktsperiod" facts block from the full "Visa upphandlingen"
    modal (under Kontraktuella villkor). It's a
    <h3>Kontraktsperiod</h3><div>...<li><strong>Label:</strong> value</li>...
    structure — this returns {label: value} for whatever list items it finds
    (Startdatum, Avtalstid, Förlängningar, Uppsägningstid, in every tender
    seen so far, though additional/renamed labels are handled generically).

    Confirmed against a real Awarded tender (Trafikverket / "En projektledare
    fyra spår Lund") — see debug/annons_section.html.
    """
    heading = scope.locator("xpath=.//h3[normalize-space(text())='Kontraktsperiod']").first
    if heading.count() == 0:
        return {}

    container = heading.locator("xpath=following-sibling::div[1]")
    items = container.locator("li")
    facts = {}
    try:
        count = items.count()
    except Exception:
        return facts

    for i in range(count):
        li = items.nth(i)
        strong = li.locator("strong").first
        if strong.count() == 0:
            continue
        try:
            label = (strong.text_content(timeout=1000) or "").strip().rstrip(":")
            full_text = (li.text_content(timeout=1000) or "").strip()
        except Exception:
            continue
        value = full_text[len(label) + 1:].strip() if full_text.lower().startswith(label.lower()) else full_text
        if label and value:
            facts[label] = value

    return facts


def scroll_until_found(page, container, selector, max_steps=15, step_px=500, wait_ms=300):
    """Scroll `container`'s own scrollable area in steps until `selector`
    appears (content here lazy-renders as it scrolls into view) or the step
    budget runs out. Returns whether it was found."""
    for _ in range(max_steps):
        if page.locator(selector).count() > 0:
            return True
        try:
            container.evaluate(f"el => el.scrollBy(0, {step_px})")
        except Exception:
            break
        page.wait_for_timeout(wait_ms)
    return page.locator(selector).count() > 0


def _extract_names_emails_phones(text):
    """
    Best-effort contact extraction from a signed evaluation/award-decision
    report's text (found under the "Utfall" tab's "Utvärderingsdokument"
    section — a short, e-signature-platform-generated PDF, much more
    reliably readable than a long contract). These reports commonly include
    a "Created by: NAME (email)" signature line and/or a "NAME\\nemail"
    letterhead-style block.

    Confirmed against a real Awarded tender (KTH / "Report of the
    procurement result...signed.pdf"): found "Bo Karlson,
    bo.karlson@indek.kth.se, Phone: +46-8-790 6052" and "Malahat Mousavi
    Tasouji (malahatm@kth.se)".
    """
    emails = sorted(set(re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text)))
    phones = sorted(set(
        m.strip() for m in re.findall(r"(?:Phone|Tel(?:efon)?|Mobile)\s*:\s*([+\d][\d\s\-]{6,20})", text)
    ))

    names = []
    for match in re.finditer(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text):
        before = text[max(0, match.start() - 150):match.start()]
        name_match = re.search(r"(?:Av|By|Created by)\s*:\s*([A-ZÅÄÖ][\w åäöÅÄÖ.]{2,60}?)\s*\($", before)
        if not name_match:
            name_match = re.search(r"([A-ZÅÄÖ][a-zåäö]+(?:\s[A-ZÅÄÖ][a-zåäö]+){0,3})\s*\n\s*$", before)
        if name_match:
            name = name_match.group(1).strip()
            if name not in names:
                names.append(name)

    return {"names": names, "emails": emails, "phones": phones[:3]}


def find_evaluation_document_link(preview):
    """
    Under the "Utfall" (outcome) tab, an awarded tender often lists an
    "Utvärderingsdokument" (evaluation document) — a signed award-decision
    report. The exact filename varies by buyer, but ends in .pdf/.docx;
    prefer one whose name hints at an award/evaluation report or signature.
    """
    candidates = preview.locator("text=/\\.(pdf|docx)/i")
    try:
        count = candidates.count()
    except Exception:
        count = 0

    preferred, fallback = None, None
    for i in range(count):
        try:
            text = candidates.nth(i).text_content(timeout=500) or ""
        except Exception:
            continue
        lowered = text.lower()
        if fallback is None:
            fallback = candidates.nth(i)
        if any(kw in lowered for kw in ("report", "beslut", "signed", "signerat", "tilldelning")):
            preferred = candidates.nth(i)
            break

    return preferred or fallback


def extract_evaluation_document_contact(page, preview):
    """Open the Utfall tab's evaluation/award document (if any) and pull out
    named contacts + email/phone. Best-effort — returns {} on anything
    unexpected."""
    link = find_evaluation_document_link(preview)
    if link is None:
        return {}

    try:
        link.click(timeout=5000)
        page.wait_for_selector("[data-testid='document-viewer']", timeout=SETTLE_TIMEOUT_MS)
        human_delay(0.8, 1.2)
        viewer = page.locator("[data-testid='document-viewer']")

        accumulated = ""
        for _ in range(20):
            text_layers = viewer.locator(".textLayer")
            try:
                count = text_layers.count()
            except Exception:
                count = 0
            for i in range(count):
                try:
                    accumulated += (text_layers.nth(i).text_content(timeout=1000) or "") + "\n"
                except Exception:
                    pass
            found = _extract_names_emails_phones(accumulated)
            if found["emails"]:
                return found
            try:
                page.mouse.wheel(0, 1200)
            except Exception:
                break
            page.wait_for_timeout(600)

        return _extract_names_emails_phones(accumulated)
    except Exception as exc:
        print(f"[info] No readable evaluation document found ({exc}).")
        return {}
    finally:
        try:
            page.keyboard.press("Escape")
            human_delay(0.3, 0.6)
        except Exception:
            pass


CONTRACT_CONTACT_LABELS = ["Ombud", "Projektledare", "Ansvarig inköpare", "Kontaktperson", "Uppdragsansvarig"]
_CONTACT_LABEL_ALT = "|".join(re.escape(lbl) for lbl in CONTRACT_CONTACT_LABELS)
_CONTACT_PATTERN = re.compile(
    rf"({_CONTACT_LABEL_ALT})\s*(?:är)?\s*:\s*(.+?)(?=(?:{_CONTACT_LABEL_ALT})\s*(?:är)?\s*:|//|§|$)",
    re.DOTALL,
)


def _parse_contract_contacts(text):
    """
    Pull named contacts out of a contract PDF's flattened text layer, from
    its "§ 3.1 Beställarens organisation" section (Ombud/Projektledare/
    Ansvarig inköpare + name — no email/phone appears in this document type,
    only names and roles). Returns {role: name}.

    Confirmed against a real Awarded tender's contract PDF (Trafikverket /
    KOM-420639_Uppdragskontrakt): "Ombud är: Ingela Olofsson Renström //
    Projektledare: Jesper Hjelmer Ansvarig inköpare är: Mathilda Ruus".
    """
    if not text or "beställarens organisation" not in text.lower():
        return {}
    idx = text.lower().find("beställarens organisation")
    end_idx = text.lower().find("leverantörens organisation", idx)
    segment = text[idx: end_idx if end_idx != -1 else idx + 800]

    contacts = {}
    for match in _CONTACT_PATTERN.finditer(segment):
        label = match.group(1).strip()
        value = re.sub(r"\s+", " ", match.group(2)).strip(" ?").strip()
        if value and len(value) < 80:  # guard against a runaway/garbled capture
            contacts[label] = value
    return contacts


def find_contract_contact_in_viewer(page, viewer, max_steps=30, step_px=1500, wait_ms=700):
    """
    Scroll through an open PDF document-viewer's pages, accumulating its
    text-layer content (PDF.js renders real, selectable text spans — no
    external download/OCR needed), stopping as soon as the organisation/
    contact section is found. Confirmed present in "§ 3.1 Beställarens
    organisation" of the contract PDF (not the "Administrativa
    föreskrifter" doc — that one just says "framgår av upphandlingssystemet",
    no named person).
    """
    accumulated = ""
    page.wait_for_timeout(800)  # let the first page fully render before reading it
    for _ in range(max_steps):
        text_layers = viewer.locator(".textLayer")
        try:
            count = text_layers.count()
        except Exception:
            count = 0
        for i in range(count):
            try:
                accumulated += (text_layers.nth(i).text_content(timeout=1000) or "") + "\n"
            except Exception:
                pass

        contacts = _parse_contract_contacts(accumulated)
        if contacts:
            return contacts

        try:
            page.mouse.wheel(0, step_px)
        except Exception:
            break
        page.wait_for_timeout(wait_ms)

    return _parse_contract_contacts(accumulated)


def extract_contract_contact_from_documents(page):
    """
    Open the tender's document tree, find the contract document (its name
    always contains "kontrakt" in every case seen so far), open its inline
    viewer, and read out the named contacts from its organisation section.
    Best-effort throughout — returns {} on anything unexpected rather than
    raising, since this is a "nice to have" on top of the core scrape.
    """
    docs_menu = page.locator("[data-testid='workflow-sidebar-menu-item-tender-documents']")
    if docs_menu.count() == 0:
        print("[info] No tender-documents menu item found; skipping contract contact lookup.")
        return {}

    # The Kontraktsperiod lookup just scrolled the content area a long way
    # down; reset it so the document tree renders from a clean, visible state
    # regardless of whether section switches reset scroll position on their own.
    content_container = page.locator("[class*='_mainContentContainer_']").first
    if content_container.count() > 0:
        try:
            content_container.evaluate("el => el.scrollTo(0, 0)")
        except Exception:
            pass

    docs_menu.first.click(timeout=8000)
    human_delay(1.0, 1.5)

    # Expand every folder in the document tree so individual files appear.
    for _ in range(4):
        switchers = page.locator(".rc-tree-switcher_close")
        try:
            n = switchers.count()
        except Exception:
            n = 0
        if n == 0:
            break
        for i in range(n):
            try:
                switchers.nth(i).click(timeout=1500)
                page.wait_for_timeout(200)
            except Exception:
                pass

    leaves = page.locator(".rc-tree-treenode-leaf")
    try:
        leaf_count = leaves.count()
    except Exception:
        leaf_count = 0
    print(f"[info] Document tree: {leaf_count} file(s) found.")

    # The tree lists the same files under more than one folder (e.g. a
    # "recently updated" section duplicating the main list) — dedupe by title.
    candidates = []
    seen_titles = set()
    for i in range(leaf_count):
        try:
            title = (leaves.nth(i).locator(".rc-tree-title").text_content(timeout=1000) or "").strip()
        except Exception:
            continue
        if "kontrakt" in title.lower() and title not in seen_titles:
            seen_titles.add(title)
            candidates.append((i, title))

    if not candidates:
        print(f"[info] No contract document found among {leaf_count} document(s); skipping contact lookup.")
        return {}

    # Prefer a signed contract over a draft ("utkast") when both exist — a
    # draft can leave the supplier side blank, a signed one won't.
    def _priority(entry):
        title_lower = entry[1].lower()
        if "signerat" in title_lower or "undertecknat" in title_lower:
            return 0
        if "utkast" in title_lower:
            return 2
        return 1

    candidates.sort(key=_priority)

    for attempt, (leaf_index, title) in enumerate(candidates):
        contacts = {}
        try:
            print(f"[info] Opening contract document: {title}")
            leaves.nth(leaf_index).click(timeout=8000)
            page.wait_for_selector("[data-testid='document-viewer']", timeout=SETTLE_TIMEOUT_MS)
            human_delay(0.8, 1.5)
            viewer = page.locator("[data-testid='document-viewer']")
            contacts = find_contract_contact_in_viewer(page, viewer)
            if not contacts:
                print(f"[info] '{title}' opened but no organisation/contact section found in it.")
                if attempt == 0:
                    save_debug_snapshot(page, "contract_document_viewer")
        except Exception as exc:
            print(f"[warn] Failed to read contract document '{title}': {exc}")
        finally:
            try:
                page.keyboard.press("Escape")
                human_delay(0.3, 0.6)
            except Exception:
                pass

        if contacts:
            return contacts

    return {}


def _condense_summary_to_one_line(text, max_len=180):
    """
    Tendium's AI summary is multiple markdown sections (Beskrivning, Syfte,
    Arbetsuppgifter, ...). For a CSV/table cell, pull just the "Beskrivning"
    paragraph out, then keep only its first sentence — a short, concrete
    one-liner rather than a full (if flattened) paragraph.
    """
    if not text:
        return ""

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""

    summary_line = None
    for i, line in enumerate(lines):
        if line.rstrip(":").strip().lower() == "beskrivning" and i + 1 < len(lines):
            summary_line = lines[i + 1]
            break

    if summary_line is None:
        # No "Beskrivning" heading found — skip a generic intro line like
        # "Här är en sammanfattning..." if present, then take what's left.
        summary_line = lines[0]
        if summary_line.lower().startswith("här är en sammanfattning") and len(lines) > 1:
            summary_line = lines[1]

    summary_line = " ".join(summary_line.split())  # collapse any stray whitespace

    # Keep just the first sentence — punchier and more concrete than a
    # multi-sentence paragraph.
    first_sentence = re.match(r"^(.*?[.!?])(\s|$)", summary_line)
    if first_sentence:
        summary_line = first_sentence.group(1)

    if len(summary_line) > max_len:
        summary_line = summary_line[:max_len].rstrip() + "…"
    return summary_line


def enrich_via_preview(page, rows, max_pages=0):
    """
    Tendium doesn't link to a separate tender/contract page — clicking a row
    opens an in-page preview sidebar (URL gains ?previewId=...&tenderId=...)
    with a "Sammanfattning" (summary) tab and, for Awarded tenders, an
    "Utfall" (outcome) tab listing the winning supplier. This re-locates each
    row by its captured data-testid, clicks it open, reads what's there, then
    closes it before moving to the next row.

    Contract start/end/extension come from the "Kontraktsperiod" facts block,
    which only exists in the full "Visa upphandlingen" modal (see
    extract_kontraktsperiod_facts) — reached via the preview's "Visa
    upphandlingen" button. A named responsible contact (person + email/phone)
    was NOT found anywhere across the list, preview, or full modal for any
    tender inspected — Tendium's own UI doesn't appear to expose one as
    structured data, so responsible_name/email/phone stay best-effort via
    _field_by_label() and will likely come back empty.
    """
    processed = 0
    for row in rows:
        row_testid = row.pop("_row_testid", None)
        if not row_testid:
            continue
        if max_pages and processed >= max_pages:
            print(f"[info] Reached max_detail_pages={max_pages}; skipping remaining tenders.")
            continue

        try:
            row_el = page.locator(f"[data-testid='{row_testid}']")
            if row_el.count() == 0:
                print(f"[warn] Row '{row_testid}' no longer present on the page; skipping.")
                continue

            title_el = row_el.locator("[class*='tenderTitle']").first
            (title_el if title_el.count() > 0 else row_el.first).click()
            page.wait_for_selector("[data-testid='tender-preview']", timeout=SETTLE_TIMEOUT_MS)
            human_delay(0.5, 1.2)

            row["url"] = page.url
            preview = page.locator("[data-testid='tender-preview']")

            if processed == 0:
                save_debug_snapshot(page, "tender_preview")

            # Which tab renders by default seems to follow the last tab used
            # in this browser session, not always "Sammanfattning" — click it
            # explicitly so summary extraction doesn't depend on that state.
            summary_tab = preview.locator("button[role='tab']:has-text('Sammanfattning')")
            if summary_tab.count() > 0:
                summary_tab.first.click()

            summary_el = preview.locator("[data-testid='-markdown']").first
            row["description"] = _condense_summary_to_one_line(_wait_for_text(summary_el))

            outcome_tab = preview.locator("button[role='tab']:has-text('Utfall')")
            if outcome_tab.count() > 0:
                outcome_tab.first.click()
                supplier_name = preview.locator("[class*='_bidderName_']").first
                row["contracted_company"] = _wait_for_text(supplier_name) or row.get("contracted_company", "")

                # A signed evaluation/award report under "Utvärderingsdokument"
                # (when present) reliably has a named contact + email/phone —
                # far more so than digging through the full contract PDF.
                found = extract_evaluation_document_contact(page, preview)
                if found.get("emails"):
                    parts = []
                    for i, email in enumerate(found["emails"]):
                        name = found["names"][i] if i < len(found["names"]) else None
                        parts.append(f"{name} <{email}>" if name else email)
                    row["responsible_name"] = "; ".join(parts)
                    row["responsible_email"] = found["emails"][0]
                    if found.get("phones"):
                        row["responsible_phone"] = found["phones"][0]

            # Contract start/end/extension live in the "Kontraktsperiod" facts
            # block, which is only reachable inside the full "Visa
            # upphandlingen" modal — not the sidebar preview above.
            open_btn = preview.locator("[data-testid='tender-preview-open-tender-button']")
            if open_btn.count() > 0:
                try:
                    open_btn.first.click(force=True, timeout=8000)
                    page.wait_for_selector("[data-testid='workflow-modal-container']", timeout=SETTLE_TIMEOUT_MS)
                    human_delay(0.5, 1.0)

                    content_container = page.locator("[class*='_mainContentContainer_']").first
                    if content_container.count() > 0:
                        scroll_until_found(
                            page, content_container, "h3:has-text('Kontraktsperiod')",
                            max_steps=15, step_px=600, wait_ms=300,
                        )

                    if processed == 0:
                        save_debug_snapshot(page, "tender_full_view")

                    facts = extract_kontraktsperiod_facts(page)
                    if facts.get("Förlängningar"):
                        row["extension_period"] = facts["Förlängningar"]
                    start_raw = facts.get("Startdatum")
                    if start_raw:
                        row["contract_start"] = start_raw
                        start_date = None
                        try:
                            start_date = datetime.strptime(start_raw.strip(), "%Y-%m-%d")
                        except ValueError:
                            pass
                        months = _parse_duration_to_months(facts.get("Avtalstid"))
                        if start_date and months:
                            row["contract_end"] = _add_months(start_date, months).strftime("%Y-%m-%d")

                    if not row.get("responsible_name"):
                        # Fallback: the evaluation report (checked earlier, from
                        # the Utfall tab) didn't yield a contact — try the named
                        # roles in the contract PDF itself (no email/phone there,
                        # only names, but better than nothing).
                        contacts = extract_contract_contact_from_documents(page)
                        if contacts:
                            row["responsible_name"] = "; ".join(
                                f"{role}: {name}" for role, name in contacts.items()
                            )

                    page.keyboard.press("Escape")
                    human_delay(0.3, 0.6)
                except Exception as exc:
                    print(f"[warn] Failed to open full tender view for '{row.get('title')}': {exc}")
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass

            detail = extract_tender_detail(preview)
            for key, value in detail.items():
                if value and not row.get(key):
                    row[key] = value

            close_btn = page.locator("[data-testid='side-bar-view-close']")
            if close_btn.count() > 0:
                close_btn.first.click()
            else:
                page.keyboard.press("Escape")
            human_delay(0.3, 0.7)
        except Exception as exc:
            print(f"[warn] Failed to open preview for '{row.get('title')}': {exc}")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass

        processed += 1

    return rows


def save_to_csv(rows, path, mode="append"):
    if not rows:
        print("[info] No rows to save.")
        return

    file_exists = os.path.isfile(path)

    if mode == "overwrite" or not file_exists:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        print(f"[info] Wrote {len(rows)} rows to {path} (mode={mode}).")
        return

    # append mode: dedupe against existing rows. A tender can have several
    # linked contracts (several rows sharing the same title/deadline/buyer/url),
    # so contract_url and contracted_company must be part of the key too —
    # otherwise all but the first contract for a tender would be dropped as
    # "duplicates".
    def _dedupe_key(r):
        return (
            r.get("title"), r.get("deadline"), r.get("buyer"), r.get("url"),
            r.get("contracted_company"), r.get("contract_url"),
        )

    existing_keys = set()
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            existing_keys.add(_dedupe_key(row))

    new_rows = [r for r in rows if _dedupe_key(r) not in existing_keys]

    if not new_rows:
        print("[info] No new rows to append (all rows already present).")
        return

    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writerows(new_rows)

    print(f"[info] Appended {len(new_rows)} new rows to {path}.")


# ---------------------------------------------------------------------------
# FILTER APPLY + VERIFICATION
#
# Toggling a "Sökord" checkbox alone leaves the page in an unsaved edit-mode
# state (an "Ångra"/"Spara ändringar" toolbar appears) that does NOT actually
# narrow results until "Spara ändringar" is clicked — skipping that step is
# what earlier produced thousands of unrelated results instead of a narrowed
# set. apply_and_save_keyword_filter() below does the full sequence: toggle
# to match desired_keywords, then click Save. After that (or if auto-apply is
# off), read_active_filters()/verify_filters_match() double-check the
# resulting state and can abort the run if it still doesn't match.
# ---------------------------------------------------------------------------

def discover_keyword_options(page):
    """
    Read every keyword in Tendium's "Sökord" panel (not just the checked
    ones) so the dashboard can render real checkboxes for them. Read-only —
    only reveals the "Se (N) mer" collapsed ones, never toggles anything.
    """
    see_more = page.locator("text=/Se \\(\\d+\\) mer/")
    try:
        if see_more.count() > 0:
            see_more.first.click()
            page.wait_for_timeout(500)
    except Exception:
        pass

    options = []
    items = page.locator("[class*='_keywordItem_']")
    try:
        count = items.count()
    except Exception:
        count = 0
    for i in range(count):
        item = items.nth(i)
        try:
            label = item.inner_text(timeout=1000).strip()
            checkbox = item.locator("button[role='checkbox']").first
            checked = checkbox.count() > 0 and checkbox.get_attribute("aria-checked") == "true"
            options.append({"label": label, "checked": checked})
        except Exception:
            continue
    return options


def write_available_filters(options):
    try:
        with open(AVAILABLE_FILTERS_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "checked_at": datetime.now().isoformat(timespec="seconds"),
                "keywords": options,
            }, f, indent=2)
    except OSError as exc:
        print(f"[warn] Failed to write {AVAILABLE_FILTERS_PATH}: {exc}")


def apply_and_save_keyword_filter(page, desired_keywords_csv):
    """
    Toggle Sökord checkboxes so only `desired_keywords_csv` (comma-separated)
    are checked, then click "Spara ändringar" to actually commit it — this
    permanently changes the account's saved bevakningsprofil on Tendium, not
    just the current view, which is why it's opt-in (auto_apply_filters).
    Returns (changed: bool, saved: bool).
    """
    desired = {k.strip().lower() for k in desired_keywords_csv.split(",") if k.strip()}

    see_more = page.locator("text=/Se \\(\\d+\\) mer/")
    try:
        if see_more.count() > 0:
            see_more.first.click()
            page.wait_for_timeout(500)
    except Exception:
        pass

    items = page.locator("[class*='_keywordItem_']")
    try:
        count = items.count()
    except Exception:
        count = 0

    changed = False
    for i in range(count):
        item = items.nth(i)
        try:
            label = item.inner_text(timeout=1000).strip()
        except Exception:
            continue
        checkbox = item.locator("button[role='checkbox']").first
        if checkbox.count() == 0:
            continue
        try:
            is_checked = checkbox.get_attribute("aria-checked") == "true"
        except Exception:
            continue
        want_checked = label.lower() in desired
        if is_checked != want_checked:
            try:
                checkbox.click(timeout=3000)
                human_delay(0.3, 0.6)
                changed = True
                print(f"[info] Sökord: {'checked' if want_checked else 'unchecked'} '{label}'.")
            except Exception as exc:
                print(f"[warn] Failed to toggle keyword '{label}': {exc}")

    if not changed:
        print("[info] Sökord already matched desired keywords; nothing to save.")
        return False, True

    save_btn = page.locator("button:has-text('Spara ändringar')")
    if save_btn.count() == 0:
        print("[warn] Toggled keywords but found no 'Spara ändringar' button — change may not be saved.")
        return True, False

    try:
        save_btn.first.click(timeout=5000)
        # Wait for the unsaved-changes toolbar to disappear, confirming the save went through.
        page.wait_for_selector("button:has-text('Spara ändringar')", state="hidden", timeout=SETTLE_TIMEOUT_MS)
        human_delay(0.8, 1.3)
        print("[info] Sökord changes saved on Tendium (this updates the account's bevakningsprofil).")
        return True, True
    except Exception as exc:
        print(f"[warn] Clicked 'Spara ändringar' but couldn't confirm it saved: {exc}")
        return True, False


def read_active_filters(page):
    """
    Read the tender-status filter (from the URL's tenderStatus param) and
    the Sökord/keyword checkboxes' checked state (read-only — never clicks),
    plus the visible result count, from the current page.
    """
    query = parse_qs(urlparse(page.url).query)
    status = (query.get("tenderStatus", [""])[0]) or "Active"  # Tendium's own default when unset

    keywords_checked = []
    items = page.locator("[class*='_keywordItem_']")
    try:
        count = items.count()
    except Exception:
        count = 0
    for i in range(count):
        item = items.nth(i)
        try:
            label = item.inner_text(timeout=1000).strip()
            checkbox = item.locator("button[role='checkbox']").first
            if checkbox.count() > 0 and checkbox.get_attribute("aria-checked") == "true":
                keywords_checked.append(label)
        except Exception:
            continue

    hits = None
    hits_el = page.locator("text=/[\\d\\s,]+träffar/")
    try:
        if hits_el.count() > 0:
            hits_text = hits_el.first.inner_text(timeout=1000)
            digits = re.sub(r"[^\d]", "", hits_text)
            hits = int(digits) if digits else None
    except Exception:
        pass

    return {"status": status, "keywords": sorted(keywords_checked), "hits": hits}


def verify_filters_match(actual, cfg):
    """
    Compare the page's actual filter state to what's expected in config.
    An empty expected_* value means "don't check this dimension". Returns
    (ok, list-of-mismatch-messages).
    """
    problems = []

    expected_status = (cfg.get("expected_status") or "").strip()
    if expected_status and actual["status"].lower() != expected_status.lower():
        problems.append(f"Status filter is '{actual['status']}', expected '{expected_status}'.")

    expected_keywords_raw = (cfg.get("desired_keywords") or "").strip()
    if expected_keywords_raw:
        expected_kw = sorted({k.strip() for k in expected_keywords_raw.split(",") if k.strip()}, key=str.lower)
        actual_kw = sorted(actual["keywords"], key=str.lower)
        if [k.lower() for k in expected_kw] != [k.lower() for k in actual_kw]:
            problems.append(
                f"Active Sökord keywords are {actual_kw or '(none)'}, expected {expected_kw}."
            )

    return (len(problems) == 0, problems)


def write_filter_status(actual, cfg, ok, problems):
    try:
        with open(FILTER_STATUS_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "checked_at": datetime.now().isoformat(timespec="seconds"),
                "actual": actual,
                "expected": {
                    "status": cfg.get("expected_status", ""),
                    "keywords": cfg.get("desired_keywords", ""),
                },
                "ok": ok,
                "problems": problems,
            }, f, indent=2)
    except OSError as exc:
        print(f"[warn] Failed to write {FILTER_STATUS_PATH}: {exc}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Tendium procurement scraper")
    parser.add_argument(
        "--login-signal-file",
        default=None,
        help=(
            "If set, wait for this file to appear instead of blocking on "
            "terminal input() during first-run manual login. Used by the "
            "manage.py web dashboard, which has no attached terminal."
        ),
    )
    return parser.parse_args()


def main():
    args = parse_args()
    cfg = load_config()
    creds = load_credentials()

    os.makedirs(SESSION_DIR, exist_ok=True)
    first_run = not is_session_initialized()

    with sync_playwright() as p:
        # Auto-login (via saved credentials) fills the login form directly
        # through Playwright, so it doesn't need a visible/interactive window
        # at all — honor the configured headless setting whenever credentials
        # are available. Without credentials, force a real window on first
        # run so a human can log in manually.
        effective_headless = cfg["headless"] if creds else (cfg["headless"] and not first_run)

        context = p.chromium.launch_persistent_context(
            SESSION_DIR,
            headless=effective_headless,
            viewport={"width": 1400, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        context.set_default_timeout(cfg["default_timeout_ms"])
        context.set_default_navigation_timeout(cfg["nav_timeout_ms"])

        page = context.pages[0] if context.pages else context.new_page()

        try:
            print(f"[info] Navigating to {cfg['target_url']} ...")
            page.goto(cfg["target_url"], wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            print("[warn] Timed out waiting for network idle; continuing anyway.")
        except Exception as exc:
            print(f"[error] Failed to navigate to {cfg['target_url']}: {exc}")
            context.close()
            sys.exit(1)

        # Detect the login form by its presence on the page rather than only
        # trusting first_run — this also self-heals an expired saved session
        # (e.g. cookies lapsed weeks later) instead of silently scraping 0
        # tenders from a login page.
        if page_appears_logged_out(page):
            logged_in_automatically = False
            if creds:
                print("[info] Login form detected; attempting automatic login with saved credentials.")
                if attempt_auto_login(page, creds["email"], creds["password"]):
                    logged_in_automatically = not page_appears_logged_out(page)
                    if not logged_in_automatically:
                        print("[warn] Still on a login-like page after auto-login "
                              "(wrong credentials, 2FA, or selectors need updating).")

            if not logged_in_automatically:
                try:
                    wait_for_manual_login(page, signal_file=args.login_signal_file)
                except TimeoutError as exc:
                    print(f"[error] {exc}")
                    context.close()
                    sys.exit(1)

            # Give the app a moment to settle after login redirects.
            try:
                page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                pass
            print(f"[info] Session saved to {SESSION_DIR}. "
                  f"Future runs can set headless=true in config.json.")

        # Basic human-like behaviour before scraping.
        human_delay()
        human_scroll(page)

        # Always discover the real, current keyword list (read-only) so the
        # dashboard can render live checkboxes for whatever exists on Tendium.
        try:
            write_available_filters(discover_keyword_options(page))
        except Exception as exc:
            print(f"[warn] Failed to discover Sökord options: {exc}")

        if cfg.get("auto_apply_filters") and (cfg.get("desired_keywords") or "").strip():
            try:
                apply_and_save_keyword_filter(page, cfg["desired_keywords"])
                try:
                    page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
                except PlaywrightTimeoutError:
                    pass
                human_delay()
            except Exception as exc:
                print(f"[warn] Failed to apply Sökord filter: {exc}")

        actual_filters = read_active_filters(page)
        filters_ok, filter_problems = verify_filters_match(actual_filters, cfg)
        write_filter_status(actual_filters, cfg, filters_ok, filter_problems)
        print(f"[info] Active filters on page: status='{actual_filters['status']}', "
              f"keywords={actual_filters['keywords']}, hits={actual_filters['hits']}.")
        if not filters_ok:
            for problem in filter_problems:
                print(f"[warn] Filter mismatch: {problem}")
            if cfg.get("require_filter_match", True):
                print("[error] Filter check failed and require_filter_match is on — "
                      "fix the filter on Tendium (and save it there) or update the "
                      "expected_status/desired_keywords settings, then rerun.")
                context.close()
                sys.exit(1)
            else:
                print("[warn] require_filter_match is off — continuing despite mismatch.")

        save_debug_snapshot(page, "list_page")

        all_rows = []
        try:
            all_rows = extract_tenders_from_page(page)
        except Exception as exc:
            print(f"[error] Extraction failed: {exc}")

        if not all_rows:
            print(f"[info] 0 tenders extracted — see {DEBUG_DIR}/list_page.html to work out the real selectors.")

        if all_rows and cfg.get("fetch_details", True):
            # Process oldest-first (list order reversed): if a run gets capped
            # by max_detail_pages or interrupted partway through, the tenders
            # most overdue for renewal still get their details fetched first.
            all_rows = list(reversed(all_rows))
            print(f"[info] Opening preview for {len(all_rows)} tender(s) (oldest first)...")
            all_rows = enrich_via_preview(page, all_rows, max_pages=cfg.get("max_detail_pages", 0))

        for row in all_rows:
            row.pop("_row_testid", None)

        save_to_csv(all_rows, OUTPUT_CSV, mode=cfg["csv_write_mode"])

        context.close()


if __name__ == "__main__":
    main()
